#!/usr/bin/env python3
"""Check a feature's local-worker token spend against the router budget.

Usage: fleet_budget.py check <ledger.jsonl> <budget>

Aggregates closed usage receipts from terminal local-worker ledger events. Exit
codes: 0 launch admitted (a WARNING is printed at >= 70%), 3 launch refused,
2 usage error.  The CLI shape remains compatible with the legacy dispatch path.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import fleet_ledger
import fleet_usage


WARN_RATIO = 0.7
NONTERMINAL_STATUSES = frozenset(
    {"preparing", "authorized", "dispatched", "running"}
)
KNOWN_STATUSES = frozenset(fleet_ledger.TERMINAL_STATUSES) | NONTERMINAL_STATUSES
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _ledger_binding(ledger_path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        info = ledger_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise fleet_usage.UsageError(f"cannot inspect lifecycle ledger: {exc}") from exc
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _event_receipt(event: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, int] = {}
    for field in TOKEN_FIELDS:
        if field not in event:
            continue
        value = event[field]
        if type(value) is not int or value < 0:
            raise fleet_usage.UsageError(
                f"terminal ledger {field} must be a non-negative integer"
            )
        values[field] = value

    if event.get("reason") == "dispatch_not_transferred":
        if event.get("status") != "abandoned":
            raise fleet_usage.UsageError(
                "dispatch_not_transferred requires abandoned status"
            )
        if values:
            raise fleet_usage.UsageError(
                "dispatch_not_transferred cannot include token counts"
            )
        return fleet_usage.receipt("ollama", "not_incurred")
    if "prompt_tokens" not in values or "completion_tokens" not in values:
        return fleet_usage.receipt("ollama", "unknown")
    total = values.get(
        "total_tokens", values["prompt_tokens"] + values["completion_tokens"]
    )
    return fleet_usage.receipt(
        "ollama",
        "observed",
        input_tokens=values["prompt_tokens"],
        output_tokens=values["completion_tokens"],
        total_tokens=total,
    )


def _validate_event_state(event: dict[str, Any], status: str) -> None:
    has_token_counts = any(field in event for field in TOKEN_FIELDS)
    if event.get("reason") == "dispatch_not_transferred":
        if status != "abandoned":
            raise fleet_usage.UsageError(
                "dispatch_not_transferred requires abandoned status"
            )
        if has_token_counts:
            raise fleet_usage.UsageError(
                "dispatch_not_transferred cannot include token counts"
            )
    if status in NONTERMINAL_STATUSES and has_token_counts:
        raise fleet_usage.UsageError(
            f"nonterminal ledger status {status!r} cannot include token counts"
        )


def usage_receipts(ledger_path: Path) -> list[dict[str, Any]] | None:
    """Translate terminal local-worker events into one receipt per run."""

    before = _ledger_binding(ledger_path)
    if before is None:
        return None
    records = fleet_ledger.read_records(ledger_path)
    after = _ledger_binding(ledger_path)
    if after is None:
        return None
    if after != before:
        raise fleet_usage.UsageError("lifecycle ledger changed during budget read")

    terminals: dict[str, dict[str, Any]] = {}
    for event in records:
        status = event.get("status")
        if type(status) is not str:
            raise fleet_usage.UsageError("ledger event status must be a string")
        if status not in KNOWN_STATUSES:
            raise fleet_usage.UsageError(
                f"ledger event has unknown status {status!r}"
            )
        _validate_event_state(event, status)
        if status not in fleet_ledger.TERMINAL_STATUSES:
            continue
        # This compatibility budget is only for local Ollama workers.  Other
        # providers may share the lifecycle ledger but do not consume it.
        provider = event.get("provider")
        if provider is not None and type(provider) is not str:
            raise fleet_usage.UsageError("terminal ledger provider must be a string")
        if provider not in (None, "ollama"):
            if any(field in event for field in TOKEN_FIELDS):
                raise fleet_usage.UsageError(
                    f"non-local provider {provider!r} claims local token counts"
                )
            continue
        run_id = event.get("run_id")
        if type(run_id) is not str or not run_id:
            raise fleet_usage.UsageError("terminal ledger event has no valid run_id")
        if run_id in terminals:
            raise fleet_usage.UsageError(
                f"terminal ledger has duplicate closure for run_id {run_id}"
            )
        terminals[run_id] = event
    return [_event_receipt(terminals[run_id]) for run_id in sorted(terminals)]


def usage_summary(ledger_path: Path) -> dict[str, Any]:
    return fleet_usage.summarize(usage_receipts(ledger_path))


def spent_tokens(ledger_path: Path) -> int:
    summary = usage_summary(ledger_path)
    if summary["total_tokens"] is None:
        raise fleet_usage.UsageError("local token usage is unknown")
    return summary["total_tokens"]


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] != "check":
        print("Usage: fleet_budget.py check <ledger.jsonl> <budget>", file=sys.stderr)
        return 2
    try:
        budget = int(sys.argv[3])
    except ValueError:
        print(f"budget must be an integer: {sys.argv[3]}", file=sys.stderr)
        return 2
    if budget < 1:
        print(f"budget must be positive: {budget}", file=sys.stderr)
        return 2

    try:
        decision = fleet_usage.admit(
            {"budget_mode": "soft", "token_budget": budget},
            "ollama",
            usage_receipts(Path(sys.argv[2])),
        )
    except (fleet_ledger.LedgerError, fleet_usage.UsageError) as exc:
        print(f"cannot read lifecycle ledger: {exc}", file=sys.stderr)
        return 2
    spent = decision["summary"]["total_tokens"]
    # Diagnostics belong on stderr so callers that promise canonical JSON can
    # reserve stdout for their machine-readable contract.
    shown_spend = "unknown" if spent is None else str(spent)
    print(f"local token spend: {shown_spend}/{budget}", file=sys.stderr)
    if decision["reason"] == "usage_unknown":
        print(
            "local token usage is unknown; refusing a new dispatch until every "
            "closed run has a trustworthy receipt",
            file=sys.stderr,
        )
        return 3
    if not decision["admitted"]:
        print(
            f"local token budget exhausted: {spent}/{budget}; "
            "raise limits.local_token_budget_per_feature or start a new feature",
            file=sys.stderr,
        )
        return 3
    if spent * 10 >= budget * 7:
        print(f"WARNING: local token spend at {spent}/{budget}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
