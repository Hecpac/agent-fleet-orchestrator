#!/usr/bin/env python3
"""Wait for exact local run IDs and legacy frontier turns via cmux events."""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from typing import Any

from fleet_ledger import latest_event


TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "abandoned"}
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


def session_surface(session_id: str) -> str:
    candidates = {session_id, re.sub(r"^[a-z]+-", "", session_id, count=1)}
    home = os.path.expanduser("~/.cmuxterm")
    for path in glob.glob(f"{home}/*-hook-sessions.json"):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sessions = value.get("sessions", {})
        for candidate in candidates:
            session = sessions.get(candidate)
            if session and session.get("surfaceId"):
                return str(session["surfaceId"]).upper()
    return ""


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
    }
    if json_mode:
        print(json.dumps(value, sort_keys=True), flush=True)
    else:
        print(
            f"instance={instance} run_id={run_id or '-'} status={status} "
            f"exit_code={value['exit_code']} result_file={value['result_file'] or '-'}",
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
    ref_uuid = dict(re.findall(r"(surface:\d+) ([0-9A-Fa-f-]{36})", tree_text))
    pending: dict[str, dict[str, str]] = {}
    for role in roles:
        if role in pending:
            print(f"duplicate instance: {role}", file=sys.stderr)
            return 2
        if role not in manifest:
            print(f"unknown instance in manifest: {role}", file=sys.stderr)
            return 2
        ref = manifest.get(role, "")
        uuid = ref_uuid.get(ref, "")
        expected_uuid = manifest.get(f"{role}.uuid", "").upper()
        runner = manifest.get(f"{role}.runner", "")
        if not ref or not uuid or not expected_uuid or uuid.upper() != expected_uuid:
            print(f"instance identity mismatch: {role}", file=sys.stderr)
            return 2
        if runner == "local" and not run_map.get(role):
            print(f"local instance requires --run {role}=<run_id>", file=sys.stderr)
            return 2
        if runner != "local" and run_map.get(role):
            print(f"--run is only valid for local instances: {role}", file=sys.stderr)
            return 2
        pending[role] = {
            "surface": uuid.upper(),
            "runner": runner,
            "run_id": run_map.get(role, ""),
            "notify_title": f"fleet-{feature}:{role}",
        }
    unknown_runs = sorted(set(run_map) - set(pending))
    if unknown_runs:
        print(f"--run references unknown instances: {', '.join(unknown_runs)}", file=sys.stderr)
        return 2

    ledger_path = Path(manifest_path).parent / f"fleet-{feature}.ledger.jsonl"
    statuses: list[str] = []

    def finish(role: str, status: str, event: dict[str, Any] | None = None) -> int | None:
        metadata = pending.pop(role)
        emit_result(
            instance=role,
            run_id=metadata["run_id"],
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

    def reconcile_local() -> int | None:
        terminal: list[tuple[int, str, dict[str, Any]]] = []
        for order, role in enumerate(list(pending)):
            metadata = pending[role]
            if metadata["runner"] != "local":
                continue
            event = latest_event(ledger_path, run_id=metadata["run_id"], instance=role)
            if event and event.get("status") in TERMINAL_STATUSES:
                terminal.append((order, role, event))
        if any_mode:
            terminal.sort(
                key=lambda item: (
                    str(item[2].get("timestamp") or "9999"),
                    item[0],
                )
            )
        for _, role, event in terminal:
            outcome = finish(role, str(event["status"]), event)
            if outcome is not None:
                return outcome
        return None

    command = [
        "cmux",
        "events",
        "--name",
        "agent.hook.Stop",
        "--name",
        "notification.requested",
        "--reconnect",
    ]
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
                if not acknowledged and not signal_ready(frame):
                    return 5
                acknowledged = True
                if (frame.get("resume") or {}).get("gap"):
                    print("cmux event replay gap; relying on durable local ledger", file=sys.stderr)
                outcome = reconcile_local()
                if outcome is not None:
                    return outcome
                continue
            if not acknowledged:
                print("cmux event received before subscription ACK", file=sys.stderr)
                return 5

            name = frame.get("name")
            outcome = reconcile_local()
            if outcome is not None:
                return outcome
            if name == "agent.hook.Stop":
                session_id = (frame.get("payload") or {}).get("session_id")
                surface = session_surface(session_id) if session_id else ""
                for role, metadata in list(pending.items()):
                    if metadata["runner"] != "local" and metadata["surface"] == surface:
                        outcome = finish(role, "succeeded")
                        if outcome is not None:
                            return outcome
                        break

            # Notifications and heartbeats are wake-ups only. Exact local
            # completion always comes from the durable run_id ledger event.
            outcome = reconcile_local()
            if outcome is not None:
                return outcome

        outcome = reconcile_local()
        if outcome is not None:
            return outcome
        for role in list(pending):
            outcome = finish(role, "indeterminate")
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
