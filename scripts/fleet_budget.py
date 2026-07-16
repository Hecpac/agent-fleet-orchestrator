#!/usr/bin/env python3
"""Check a feature's local-worker token spend against the router budget.

Usage: fleet_budget.py check <ledger.jsonl> <budget>

Sums prompt_tokens + completion_tokens across ledger events. Exit codes:
0 spend below budget (a WARNING is printed to stderr at >= 70%),
3 budget exhausted (dispatch must refuse), 2 usage error.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


WARN_RATIO = 0.7


def spent_tokens(ledger_path: Path) -> int:
    total = 0
    try:
        with ledger_path.open(encoding="utf-8") as handle:
            for raw in handle:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                for field in ("prompt_tokens", "completion_tokens"):
                    value = event.get(field)
                    if isinstance(value, int) and not isinstance(value, bool):
                        total += value
    except OSError:
        return 0
    return total


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

    spent = spent_tokens(Path(sys.argv[2]))
    # Diagnostics belong on stderr so callers that promise canonical JSON can
    # reserve stdout for their machine-readable contract.
    print(f"local token spend: {spent}/{budget}", file=sys.stderr)
    if spent >= budget:
        print(
            f"local token budget exhausted: {spent}/{budget}; "
            "raise limits.local_token_budget_per_feature or start a new feature",
            file=sys.stderr,
        )
        return 3
    if spent >= budget * WARN_RATIO:
        print(f"WARNING: local token spend at {spent}/{budget}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
