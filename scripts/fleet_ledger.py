#!/usr/bin/env python3
"""Append redacted lifecycle events to a fleet JSONL ledger."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
from typing import Any

import fleet_json
import fleet_safe_paths


TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "abandoned", "indeterminate"}
MAX_LEDGER_BYTES = 64 * 1024 * 1024
SAFE_LEDGER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,248}\.jsonl$")


class LedgerError(RuntimeError):
    """A lifecycle ledger failed its physical provenance contract."""


def _ledger_binding(
    path: Path,
    *,
    runs_dir: Path | None = None,
) -> tuple[Path, str]:
    """Bind one top-level ledger leaf to an explicit trusted runs root.

    ``runs_dir`` is required for callers that derive ``path`` from untrusted
    identifiers.  The path-only form remains for compatibility with existing
    callers, but it rejects lexical traversal instead of treating the escaped
    parent as a new trusted root.
    """

    candidate = Path(path)
    leaf = candidate.name
    if (
        not SAFE_LEDGER_NAME.fullmatch(leaf)
        or candidate != candidate.parent / leaf
        or (runs_dir is None and ".." in candidate.parts)
    ):
        raise LedgerError(
            "ledger must be one safe JSONL file directly beneath its runs root"
        )
    if runs_dir is None:
        return candidate.parent, leaf

    root = Path(runs_dir)
    if candidate != root / leaf:
        raise LedgerError("ledger is outside the selected runs root")
    return root, leaf


def _records_from_bytes(payload: bytes) -> list[dict[str, Any]]:
    try:
        values = fleet_json.load_jsonl(payload)
    except fleet_json.FleetJSONError as exc:
        if "duplicate JSON object key" in str(exc):
            raise LedgerError(f"duplicate ledger key: {exc}") from exc
        raise LedgerError(f"invalid ledger JSONL: {exc}") from exc
    records: list[dict[str, Any]] = []
    for line_number, value in enumerate(values, start=1):
        if type(value) is not dict:
            raise LedgerError(f"ledger row is not an object at line {line_number}")
        records.append(value)
    return records


def _read_records(
    path: Path,
    *,
    runs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    root, leaf = _ledger_binding(path, runs_dir=runs_dir)
    try:
        with fleet_safe_paths.RootedFS(root) as rooted:
            payload = rooted.read_regular_optional(
                leaf,
                directory_modes=(),
                file_mode=0o600,
                max_bytes=MAX_LEDGER_BYTES,
            )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise LedgerError(f"unsafe lifecycle ledger path: {exc}") from exc
    if payload is None:
        return []
    return _records_from_bytes(payload)


def read_records(
    path: Path,
    *,
    runs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Read and strictly decode an exact top-level lifecycle ledger."""

    return _read_records(path, runs_dir=runs_dir)


def initialize_empty(
    path: Path,
    *,
    runs_dir: Path | None = None,
) -> None:
    """Publish a new trusted empty ledger without adopting a prior pathname."""

    root, leaf = _ledger_binding(path, runs_dir=runs_dir)
    try:
        with fleet_safe_paths.RootedFS(root) as rooted:
            rooted.atomic_write(
                leaf,
                b"",
                directory_modes=(),
                file_mode=0o600,
                require_absent=True,
            )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise LedgerError(f"cannot initialize trusted lifecycle ledger: {exc}") from exc


def remove_empty(
    path: Path,
    *,
    runs_dir: Path | None = None,
    missing_ok: bool = False,
) -> bool:
    """Remove only the still-empty ledger created by an aborted pre-dispatch boot."""

    root, leaf = _ledger_binding(path, runs_dir=runs_dir)
    try:
        with fleet_safe_paths.RootedFS(root) as rooted:
            removed = rooted.unlink_regular_if_content(
                leaf,
                b"",
                directory_modes=(),
                file_mode=0o600,
                missing_ok=missing_ok,
            )
            rooted.assert_root_binding()
            return removed
    except fleet_safe_paths.SafePathError as exc:
        raise LedgerError(f"cannot remove empty lifecycle ledger: {exc}") from exc


def append_record(
    path: Path,
    record: dict[str, Any],
    *,
    reject_if: Any = None,
    runs_dir: Path | None = None,
) -> bool:
    """Append one JSON object under a descriptor-bound exclusive file lock."""

    if type(record) is not dict:
        raise LedgerError("ledger record must be an object")
    root, leaf = _ledger_binding(path, runs_dir=runs_dir)
    try:
        content = fleet_json.canonical_bytes(record) + b"\n"
    except fleet_json.FleetJSONError as exc:
        raise LedgerError("ledger record is not strict JSON") from exc

    def rejected(snapshot: bytes) -> bool:
        if reject_if is None:
            return False
        return any(reject_if(previous) for previous in _records_from_bytes(snapshot))

    try:
        with fleet_safe_paths.RootedFS(root) as rooted:
            _, appended = rooted.guarded_append_regular(
                leaf,
                content,
                directory_modes=(),
                reject_if=rejected,
                file_mode=0o600,
                max_existing_bytes=MAX_LEDGER_BYTES,
                require_single_link=True,
            )
            rooted.assert_root_binding()
            return appended
    except fleet_safe_paths.SafePathError as exc:
        raise LedgerError(f"unsafe lifecycle ledger path: {exc}") from exc


def append_event(
    path: Path,
    event: dict[str, Any],
    *,
    runs_dir: Path | None = None,
) -> bool:
    """Append a lifecycle event; the first terminal event is immutable."""
    run_id = event.get("run_id")
    return append_record(
        path,
        event,
        reject_if=lambda previous: (
            previous.get("run_id") == run_id
            and previous.get("status") in TERMINAL_STATUSES
        ),
        runs_dir=runs_dir,
    )


def latest_event(
    path: Path,
    *,
    run_id: str,
    instance: str | None = None,
    runs_dir: Path | None = None,
) -> dict[str, Any] | None:
    latest = None
    for event in _read_records(path, runs_dir=runs_dir):
        if event.get("run_id") != run_id:
            continue
        if instance is not None and event.get("instance") != instance:
            continue
        latest = event
    return latest


def events_for_run(
    path: Path,
    *,
    run_id: str,
    instance: str | None = None,
    runs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for event in _read_records(path, runs_dir=runs_dir):
        if event.get("run_id") != run_id:
            continue
        if instance is not None and event.get("instance") != instance:
            continue
        events.append(event)
    return events


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in {"init-empty", "remove-empty"}:
        lifecycle = argparse.ArgumentParser()
        lifecycle.add_argument("command", choices=("init-empty", "remove-empty"))
        lifecycle.add_argument("ledger")
        lifecycle.add_argument("--runs-dir", required=True)
        args = lifecycle.parse_args()
        try:
            if args.command == "init-empty":
                initialize_empty(Path(args.ledger), runs_dir=Path(args.runs_dir))
            else:
                remove_empty(
                    Path(args.ledger),
                    runs_dir=Path(args.runs_dir),
                    missing_ok=True,
                )
        except LedgerError as exc:
            print(str(exc), file=sys.stderr)
            return 74
        return 0

    parser = argparse.ArgumentParser()
    parser.add_argument("ledger")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--task-sha256", required=True)
    parser.add_argument("--exit-code", type=int)
    parser.add_argument("--result-file")
    parser.add_argument("--prompt-tokens", type=int)
    parser.add_argument("--completion-tokens", type=int)
    parser.add_argument("--reason")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--variant")
    args = parser.parse_args()
    if (args.provider is None) != (args.model is None):
        parser.error("--provider and --model must be supplied together")
    if args.provider is not None and (not args.provider or not args.model):
        parser.error("--provider and --model must be non-empty")

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": args.run_id,
        "feature": args.feature,
        "instance": args.instance,
        "role": args.role,
        "phase": args.phase,
        "status": args.status,
        "task_sha256": args.task_sha256,
    }
    if args.exit_code is not None:
        event["exit_code"] = args.exit_code
    if args.result_file:
        event["result_file"] = args.result_file
    if args.prompt_tokens is not None:
        event["prompt_tokens"] = args.prompt_tokens
    if args.completion_tokens is not None:
        event["completion_tokens"] = args.completion_tokens
    if args.reason:
        event["reason"] = args.reason
    if args.provider is not None:
        event["provider"] = args.provider
        event["model"] = args.model
        event["variant"] = args.variant or None

    path = Path(args.ledger)
    try:
        appended = append_event(path, event)
    except LedgerError as exc:
        print(str(exc), file=sys.stderr)
        return 74
    if not appended and args.status not in TERMINAL_STATUSES:
        print(f"run already terminal: {args.run_id}", file=sys.stderr)
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
