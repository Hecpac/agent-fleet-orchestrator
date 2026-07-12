#!/usr/bin/env python3
"""Append redacted lifecycle events to a fleet JSONL ledger."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path


def main() -> int:
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
    args = parser.parse_args()

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

    path = Path(args.ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
