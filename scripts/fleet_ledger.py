#!/usr/bin/env python3
"""Append redacted lifecycle events to a fleet JSONL ledger."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
from typing import Any


TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "abandoned"}


def append_event(path: Path, event: dict[str, Any]) -> bool:
    """Append under an exclusive lock; the first terminal event is immutable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    handle = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        run_id = event.get("run_id")
        for raw in handle:
            try:
                previous = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if (
                previous.get("run_id") == run_id
                and previous.get("status") in TERMINAL_STATUSES
            ):
                return False
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        if created:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return True
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def latest_event(
    path: Path,
    *,
    run_id: str,
    instance: str | None = None,
) -> dict[str, Any] | None:
    latest = None
    try:
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event.get("run_id") != run_id:
                    continue
                if instance is not None and event.get("instance") != instance:
                    continue
                latest = event
    except OSError:
        return None
    return latest


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
    parser.add_argument("--reason")
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
    if args.reason:
        event["reason"] = args.reason

    path = Path(args.ledger)
    appended = append_event(path, event)
    if not appended and args.status not in TERMINAL_STATUSES:
        print(f"run already terminal: {args.run_id}", file=sys.stderr)
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
