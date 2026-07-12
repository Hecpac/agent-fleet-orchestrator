#!/usr/bin/env python3
"""Wait for exact local and frontier run IDs through durable evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any

from fleet_frontier import (
    FrontierError,
    frontier_state,
    process_event,
    recover_from_audit,
    validate_event_ack,
)
from fleet_ledger import TERMINAL_STATUSES, latest_event


STATUS_CODES = {
    "succeeded": 0,
    "failed": 1,
    "blocked": 3,
    "abandoned": 4,
    "indeterminate": 5,
}


def parse_args(argv: list[str]) -> tuple[str, str, int, bool, bool, dict[str, str], list[str]]:
    if len(argv) < 5:
        raise ValueError(
            "usage: fleet_wait.py <feature> <manifest> <timeout> "
            "[--any] [--json] [--run=<instance>=<run_id>] <instance>..."
        )
    feature, manifest_path = argv[1], argv[2]
    timeout_sec = int(argv[3])
    if timeout_sec < 1:
        raise ValueError("timeout must be a positive integer")
    any_mode = False
    json_mode = False
    run_map: dict[str, str] = {}
    roles: list[str] = []
    for arg in argv[4:]:
        if arg == "--any":
            any_mode = True
        elif arg == "--json":
            json_mode = True
        elif arg.startswith("--run="):
            value = arg.removeprefix("--run=")
            if "=" not in value:
                raise ValueError(f"invalid --run mapping: {value}")
            instance, run_id = value.split("=", 1)
            if not instance or not run_id or instance in run_map:
                raise ValueError(f"invalid or duplicate --run mapping: {value}")
            run_map[instance] = run_id
        else:
            roles.append(arg)
    if not roles:
        raise ValueError("at least one instance is required")
    return feature, manifest_path, timeout_sec, any_mode, json_mode, run_map, roles


def read_manifest(path: str) -> dict[str, str]:
    return dict(
        line.strip().split("=", 1)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


def emit_result(
    *,
    instance: str,
    run_id: str,
    status: str,
    event: dict[str, Any] | None,
    json_mode: bool,
) -> None:
    value = {
        "instance": instance,
        "run_id": run_id,
        "status": status,
        "exit_code": (event or {}).get("exit_code", STATUS_CODES[status]),
        "result_file": (event or {}).get("result_file", ""),
        "completed_at": (event or {}).get("completed_at", (event or {}).get("timestamp", "")),
    }
    if json_mode:
        print(json.dumps(value, sort_keys=True), flush=True)
    else:
        print(
            f"instance={instance} run_id={run_id} status={status} "
            f"exit_code={value['exit_code']} result_file={value['result_file'] or '-'} "
            f"completed_at={value['completed_at'] or '-'}",
            flush=True,
        )


def aggregate_exit(statuses: list[str]) -> int:
    if not statuses or all(status == "succeeded" for status in statuses):
        return 0
    for status in ("indeterminate", "abandoned", "blocked", "failed"):
        if status in statuses:
            return STATUS_CODES[status]
    return 5


def signal_ready(frame: dict[str, Any]) -> bool:
    path_value = os.environ.get("FLEET_WAIT_READY_FILE", "")
    if not path_value:
        return True
    path = Path(path_value)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"status": "ready", "resume": frame.get("resume", {})}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        print(f"cannot publish event subscription readiness: {exc}", file=sys.stderr)
        return False
    return True


def completion_key(instance: str, event: dict[str, Any]) -> tuple[datetime, str]:
    raw = str(event.get("completed_at") or event.get("timestamp") or "")
    try:
        completed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        completed = completed.astimezone(timezone.utc)
    except ValueError:
        completed = datetime.max.replace(tzinfo=timezone.utc)
    return completed, instance


def main(argv: list[str] | None = None) -> int:
    try:
        feature, manifest_path, timeout_sec, any_mode, json_mode, run_map, roles = parse_args(
            list(argv or sys.argv)
        )
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        manifest = read_manifest(manifest_path)
    except OSError as exc:
        print(f"cannot read manifest: {exc}", file=sys.stderr)
        return 2

    tree_text = os.environ.get("TREE_BOTH", "")
    workspace_ref = manifest.get("workspace", "")
    workspace_uuid = manifest.get("workspace_uuid", "").upper()
    runs_dir = Path(manifest_path).parent
    ledger = runs_dir / f"fleet-{feature}.ledger.jsonl"
    pending: dict[str, dict[str, Any]] = {}
    for role in roles:
        if role in pending:
            print(f"duplicate instance: {role}", file=sys.stderr)
            return 2
        if role not in manifest:
            print(f"unknown instance in manifest: {role}", file=sys.stderr)
            return 2
        surface_ref = manifest.get(role, "")
        surface_uuid = manifest.get(f"{role}.uuid", "").upper()
        if (
            not surface_ref
            or not surface_uuid
            or f"{surface_ref} {surface_uuid}".upper() not in tree_text.upper()
        ):
            print(f"instance identity mismatch: {role}", file=sys.stderr)
            return 2
        runner = manifest.get(f"{role}.runner", "")
        if runner not in {"local", "interactive"}:
            print(f"instance is not waitable: {role}", file=sys.stderr)
            return 2
        run_id = run_map.get(role, "")
        if not run_id:
            print(f"instance requires --run {role}=<run_id>", file=sys.stderr)
            return 2
        metadata: dict[str, Any] = {
            "runner": runner,
            "run_id": run_id,
            "surface_ref": surface_ref,
            "surface_uuid": surface_uuid,
        }
        if runner == "interactive":
            state = frontier_state(ledger, run_id=run_id, instance=role)
            if not state or state.get("runner") != "interactive":
                print(f"unknown frontier run: {role}={run_id}", file=sys.stderr)
                return 2
            if (
                str(state.get("workspace_uuid", "")).upper() != workspace_uuid
                or str(state.get("surface_uuid", "")).upper() != surface_uuid
            ):
                print(f"frontier run identity mismatch: {role}={run_id}", file=sys.stderr)
                return 2
            metadata["state"] = state
        pending[role] = metadata
    unknown_runs = sorted(set(run_map) - set(pending))
    if unknown_runs:
        print(f"--run references unknown instances: {', '.join(unknown_runs)}", file=sys.stderr)
        return 2

    statuses: list[str] = []
    replay_remaining = 0

    def finish(role: str, event: dict[str, Any]) -> int | None:
        metadata = pending.pop(role)
        status = str(event["status"])
        emit_result(
            instance=role,
            run_id=str(metadata["run_id"]),
            status=status,
            event=event,
            json_mode=json_mode,
        )
        statuses.append(status)
        if any_mode and status == "succeeded":
            return 0
        if not pending:
            return aggregate_exit(statuses)
        return None

    def terminal_records() -> list[tuple[str, dict[str, Any]]]:
        records: list[tuple[str, dict[str, Any]]] = []
        for role, metadata in pending.items():
            event = latest_event(
                ledger,
                run_id=str(metadata["run_id"]),
                instance=role,
            )
            if event and event.get("status") in TERMINAL_STATUSES:
                records.append((role, event))
        records.sort(key=lambda item: completion_key(item[0], item[1]))
        return records

    def arbitrate() -> int | None:
        if replay_remaining > 0:
            return None
        for role, event in terminal_records():
            outcome = finish(role, event)
            if outcome is not None:
                return outcome
        return None

    frontier_after = [
        int(metadata["state"]["after_seq"])
        for metadata in pending.values()
        if metadata["runner"] == "interactive"
        and metadata["state"].get("status") not in TERMINAL_STATUSES
    ]
    command = [
        "cmux",
        "events",
        "--name",
        "agent.hook.UserPromptSubmit",
        "--name",
        "agent.hook.Stop",
        "--name",
        "notification.requested",
        "--reconnect",
    ]
    if frontier_after:
        command += ["--after", str(min(frontier_after))]
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "CMUX_QUIET": "1"},
        )
    except OSError as exc:
        print(f"cannot start cmux event stream: {exc}", file=sys.stderr)
        return 5
    assert proc.stdout is not None

    def on_timeout(signum: int, frame: object) -> None:
        proc.kill()
        stuck = ", ".join(sorted(pending))
        try:
            subprocess.run(
                [
                    "cmux",
                    "notify",
                    "--title",
                    f"ESCALATION: fleet-{feature} wait timeout",
                    "--body",
                    f"pending={stuck} timeout={timeout_sec}s",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        print(f"timeout; escalated pending: {stuck}", file=sys.stderr, flush=True)
        raise SystemExit(124)

    def on_terminate(signum: int, frame: object) -> None:
        proc.kill()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGALRM, on_timeout)
    signal.signal(signal.SIGINT, on_terminate)
    signal.signal(signal.SIGTERM, on_terminate)
    signal.alarm(timeout_sec)

    try:
        acknowledged = False
        for line in proc.stdout:
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "ack":
                try:
                    validate_event_ack(frame)
                except FrontierError as exc:
                    print(str(exc), file=sys.stderr)
                    return 5
                if not acknowledged and not signal_ready(frame):
                    return 5
                acknowledged = True
                replay_remaining = int(frame.get("replay_count") or 0)
                resume = frame.get("resume") or {}
                ack_boot = str(frame.get("boot_id") or "")
                oldest = int(resume.get("oldest_seq") or 0)
                latest = int(resume.get("latest_seq") or 0)
                for role, metadata in list(pending.items()):
                    if metadata["runner"] != "interactive":
                        continue
                    state = frontier_state(
                        ledger, run_id=str(metadata["run_id"]), instance=role
                    ) or metadata["state"]
                    metadata["state"] = state
                    if state.get("status") in TERMINAL_STATUSES:
                        continue
                    after_seq = int(state["after_seq"])
                    recover = ack_boot != state.get("event_boot_id") or (
                        bool(resume.get("gap"))
                        and not (oldest - 1 <= after_seq <= latest)
                    )
                    if recover:
                        recover_from_audit(
                            runs_dir,
                            state,
                            workspace_ref=workspace_ref,
                            surface_ref=str(metadata["surface_ref"]),
                        )
                outcome = arbitrate()
                if outcome is not None:
                    return outcome
                continue
            if not acknowledged:
                print("cmux event received before subscription ACK", file=sys.stderr)
                return 5

            if frame.get("type") == "event":
                for role, metadata in list(pending.items()):
                    if metadata["runner"] != "interactive":
                        continue
                    state = frontier_state(
                        ledger, run_id=str(metadata["run_id"]), instance=role
                    ) or metadata["state"]
                    metadata["state"] = state
                    if state.get("status") in TERMINAL_STATUSES:
                        continue
                    try:
                        process_event(
                            runs_dir,
                            state,
                            frame,
                            workspace_ref=workspace_ref,
                            surface_ref=str(metadata["surface_ref"]),
                        )
                    except FrontierError as exc:
                        print(f"frontier protocol error for {role}: {exc}", file=sys.stderr)
                if replay_remaining > 0:
                    replay_remaining -= 1

            outcome = arbitrate()
            if outcome is not None:
                return outcome

        outcome = arbitrate()
        if outcome is not None:
            return outcome
        for role in list(pending):
            event = {
                "status": "indeterminate",
                "exit_code": 5,
                "timestamp": "",
            }
            outcome = finish(role, event)
            if outcome is not None and not pending:
                return outcome
        return 5
    finally:
        signal.alarm(0)
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
