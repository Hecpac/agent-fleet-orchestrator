#!/usr/bin/env python3
"""Durable frontier-run dispatch metadata and cmux event reconciliation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import uuid
from typing import Any

from fleet_leases import (
    LeaseError,
    acquire_frontier,
    read_metadata,
    release,
)
from fleet_ledger import TERMINAL_STATUSES, append_event, events_for_run, latest_event


STATUS_CODES = {
    "succeeded": 0,
    "failed": 1,
    "blocked": 3,
    "abandoned": 4,
    "indeterminate": 5,
}
SENTINEL_STATUSES = {
    "DONE": "succeeded",
    "BLOCKED": "blocked",
    "FAILED": "failed",
}


class FrontierError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp_value(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ledger_path(runs_dir: Path, feature: str) -> Path:
    return runs_dir / f"fleet-{feature}.ledger.jsonl"


def frontier_state(
    ledger: Path,
    *,
    run_id: str,
    instance: str,
) -> dict[str, Any] | None:
    events = events_for_run(ledger, run_id=run_id, instance=instance)
    if not events:
        return None
    merged: dict[str, Any] = {}
    for event in events:
        merged.update(event)
    return merged


def event_ack(timeout: float = 10.0) -> dict[str, Any]:
    command = [
        "cmux",
        "events",
        "--name",
        "agent.hook.UserPromptSubmit",
        "--name",
        "agent.hook.Stop",
        "--no-heartbeat",
    ]
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "CMUX_QUIET": "1"},
        )
    except OSError as exc:
        raise FrontierError(f"cannot start cmux event snapshot: {exc}") from exc
    assert proc.stdout is not None
    try:
        readable, _, _ = select.select([proc.stdout], [], [], timeout)
        if not readable:
            raise FrontierError("cmux event snapshot ACK timed out")
        raw = proc.stdout.readline()
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FrontierError("cmux event snapshot returned invalid JSON") from exc
        validate_event_ack(frame)
        return frame
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def validate_event_ack(frame: dict[str, Any]) -> None:
    resume = frame.get("resume")
    valid = bool(
        frame.get("type") == "ack"
        and frame.get("protocol") == "cmux-events"
        and frame.get("version") == 1
        and isinstance(frame.get("boot_id"), str)
        and frame.get("boot_id")
        and isinstance(frame.get("replay_count"), int)
        and int(frame["replay_count"]) >= 0
        and isinstance(resume, dict)
        and isinstance(resume.get("gap"), bool)
        and (
            resume.get("oldest_seq") is None
            or isinstance(resume.get("oldest_seq"), int)
        )
        and isinstance(resume.get("latest_seq"), int)
        and isinstance(resume.get("next_seq"), int)
    )
    if not valid:
        raise FrontierError("invalid cmux-events ACK")


def session_record(session_id: str) -> dict[str, Any] | None:
    hook_dir = os.environ.get("CMUX_HOOK_DIR", os.path.expanduser("~/.cmuxterm"))
    candidates = {session_id, re.sub(r"^[a-z]+-", "", session_id, count=1)}
    matches: list[dict[str, Any]] = []
    for filename in glob.glob(str(Path(hook_dir) / "*-hook-sessions.json")):
        try:
            data = json.loads(Path(filename).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sessions = data.get("sessions") or {}
        for candidate in candidates:
            value = sessions.get(candidate)
            if isinstance(value, dict):
                matches.append(value)
    if not matches:
        return None
    matches.sort(key=lambda value: float(value.get("updatedAt") or 0), reverse=True)
    return matches[0]


def session_matches(
    session_id: str,
    *,
    workspace_uuid: str,
    surface_uuid: str,
) -> bool:
    record = session_record(session_id)
    return bool(
        record
        and str(record.get("workspaceId", "")).upper() == workspace_uuid.upper()
        and str(record.get("surfaceId", "")).upper() == surface_uuid.upper()
    )


def prompt_with_contract(task: str, run_id: str) -> str:
    return (
        f"{task}\n\n"
        "Fleet completion protocol: in the final answer, include exactly one final line "
        f"using FLEET_RESULT:{run_id}:<STATUS>, where STATUS is DONE, BLOCKED, or FAILED. "
        "Do not emit that line before the final answer."
    )


def sentinel_status(screen: str, run_id: str) -> tuple[str, str]:
    pattern = re.compile(
        rf"FLEET_RESULT:{re.escape(run_id)}:(DONE|BLOCKED|FAILED)"
    )
    lines = screen.splitlines()
    matches = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := pattern.fullmatch(line.strip()))
    ]
    if len(matches) != 1:
        reason = "frontier_sentinel_missing" if not matches else "frontier_sentinel_ambiguous"
        return "indeterminate", reason
    sentinel_index, match = matches[0]
    trailing = [line.strip() for line in lines[sentinel_index + 1 :] if line.strip()]
    # read-screen includes the interactive client's idle prompt/status after the
    # assistant answer. It is UI chrome, not answer text. Anything before that
    # prompt is still post-sentinel agent output and invalidates finality.
    if trailing:
        if trailing[0].startswith("› "):
            valid_chrome = len(trailing) == 1 or (
                len(trailing) == 2
                and " · " in trailing[1]
                and re.match(r"^(?:gpt-|o[0-9]|codex)", trailing[1], re.IGNORECASE)
            )
        else:
            valid_chrome = trailing == ["❯"]
        if not valid_chrome:
            return "indeterminate", "frontier_sentinel_not_final"
    return SENTINEL_STATUSES[match.group(1)], "frontier_sentinel_verified"


def read_screen(workspace_ref: str, surface_ref: str) -> str:
    try:
        result = subprocess.run(
            [
                "cmux",
                "read-screen",
                "--surface",
                surface_ref,
                "--workspace",
                workspace_ref,
                "--scrollback",
                "--lines",
                "240",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={**os.environ, "CMUX_QUIET": "1"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FrontierError(f"cannot read frontier screen: {exc}") from exc
    if result.returncode != 0:
        raise FrontierError(result.stderr.strip() or "cmux read-screen failed")
    return result.stdout


def prepare_run(
    runs_dir: Path,
    *,
    feature: str,
    instance: str,
    role: str,
    phase: str,
    task: str,
    workspace_uuid: str,
    surface_uuid: str,
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    task_sha256 = hashlib.sha256(task.encode("utf-8")).hexdigest()
    ledger = ledger_path(runs_dir, feature)
    preparing_at = utc_now()
    preparing = {
        "timestamp": preparing_at,
        "run_id": run_id,
        "feature": feature,
        "instance": instance,
        "role": role,
        "phase": phase,
        "runner": "interactive",
        "status": "preparing",
        "task_sha256": task_sha256,
        "workspace_uuid": workspace_uuid.upper(),
        "surface_uuid": surface_uuid.upper(),
        "preparing_at": preparing_at,
    }
    if not append_event(ledger, preparing):
        raise FrontierError(f"frontier run unexpectedly already terminal: {run_id}")
    lease: Path | None = None
    try:
        lease = acquire_frontier(
            runs_dir,
            run_id=run_id,
            feature=feature,
            instance=instance,
            role=role,
            phase=phase,
            task_sha256=task_sha256,
            workspace_uuid=workspace_uuid,
            surface_uuid=surface_uuid,
        )
        ack = event_ack()
        resume = ack["resume"]
        dispatched_at = utc_now()
        event = {
            "timestamp": dispatched_at,
            "run_id": run_id,
            "feature": feature,
            "instance": instance,
            "role": role,
            "phase": phase,
            "runner": "interactive",
            "status": "dispatched",
            "task_sha256": task_sha256,
            "workspace_uuid": workspace_uuid.upper(),
            "surface_uuid": surface_uuid.upper(),
            "event_boot_id": ack["boot_id"],
            "after_seq": resume["latest_seq"],
            "event_oldest_seq": resume.get("oldest_seq"),
            "dispatched_at": dispatched_at,
        }
        if not append_event(ledger, event):
            raise FrontierError(f"frontier run unexpectedly already terminal: {run_id}")
        return {
            **event,
            "lease": str(lease),
            "prompt": prompt_with_contract(task, run_id),
        }
    except Exception:
        append_event(
            ledger,
            {
                **preparing,
                "timestamp": utc_now(),
                "completed_at": utc_now(),
                "status": "abandoned",
                "exit_code": STATUS_CODES["abandoned"],
                "reason": "frontier_prepare_failed",
            },
        )
        if lease is not None:
            release(runs_dir, run_id, [lease])
        raise


def _common_event(state: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "run_id",
        "feature",
        "instance",
        "role",
        "phase",
        "runner",
        "task_sha256",
        "workspace_uuid",
        "surface_uuid",
        "event_boot_id",
        "after_seq",
        "event_oldest_seq",
        "dispatched_at",
    )
    return {key: state[key] for key in keys if key in state}


def _release_frontier_lease(
    runs_dir: Path, state: dict[str, Any], *, required: bool = False
) -> bool:
    lease = runs_dir / "locks" / f"{state['feature']}.{state['instance']}.lock"
    metadata = read_metadata(lease)
    if metadata and metadata.get("run_id") == state.get("run_id"):
        release(runs_dir, str(state["run_id"]), [lease])
        return True
    if required:
        raise FrontierError(f"owned frontier lease is missing: {lease}")
    return False


def terminalize(
    runs_dir: Path,
    state: dict[str, Any],
    *,
    status: str,
    reason: str,
    completed_at: str | None = None,
    event: dict[str, Any] | None = None,
    release_lease: bool = True,
) -> dict[str, Any]:
    if status not in STATUS_CODES:
        raise FrontierError(f"invalid frontier terminal status: {status}")
    terminal = {
        **_common_event(state),
        "timestamp": utc_now(),
        "completed_at": completed_at or utc_now(),
        "status": status,
        "exit_code": STATUS_CODES[status],
        "reason": reason,
    }
    if state.get("session_id"):
        terminal["session_id"] = state["session_id"]
    if event:
        terminal.update(
            {
                "completion_boot_id": event.get("boot_id"),
                "completion_seq": event.get("seq"),
                "completion_event_id": event.get("id"),
            }
        )
    if not release_lease:
        terminal["lease_retained"] = True
    ledger = ledger_path(runs_dir, str(state["feature"]))
    appended = append_event(ledger, terminal)
    if appended and release_lease:
        _release_frontier_lease(runs_dir, state)
    return latest_event(
        ledger,
        run_id=str(state["run_id"]),
        instance=str(state["instance"]),
    ) or terminal


def _event_after_dispatch(
    state: dict[str, Any], event: dict[str, Any], *, allow_cross_boot: bool
) -> bool:
    occurred_at = timestamp_value(event.get("occurred_at"))
    dispatched_at = timestamp_value(state.get("dispatched_at"))
    if occurred_at and dispatched_at and occurred_at < dispatched_at:
        return False
    if event.get("boot_id") == state.get("event_boot_id"):
        return isinstance(event.get("seq"), int) and event["seq"] > int(state["after_seq"])
    return allow_cross_boot


def _event_after_binding(state: dict[str, Any], event: dict[str, Any]) -> bool:
    if event.get("boot_id") == state.get("binding_boot_id"):
        return (
            isinstance(event.get("seq"), int)
            and isinstance(state.get("binding_seq"), int)
            and int(event["seq"]) > int(state["binding_seq"])
        )
    occurred_at = timestamp_value(event.get("occurred_at"))
    submitted_at = timestamp_value(state.get("submitted_at"))
    return bool(occurred_at and submitted_at and occurred_at > submitted_at)


def process_event(
    runs_dir: Path,
    state: dict[str, Any],
    event: dict[str, Any],
    *,
    workspace_ref: str,
    surface_ref: str,
    allow_cross_boot: bool = False,
) -> dict[str, Any] | None:
    if state.get("status") in TERMINAL_STATUSES:
        return state
    if event.get("type") != "event" or not _event_after_dispatch(
        state, event, allow_cross_boot=allow_cross_boot
    ):
        return None
    if str(event.get("workspace_id") or "").upper() != str(
        state.get("workspace_uuid") or ""
    ).upper():
        return None
    payload = event.get("payload") or {}
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return None

    name = event.get("name")
    ledger = ledger_path(runs_dir, str(state["feature"]))
    if name == "agent.hook.UserPromptSubmit" and payload.get("phase") == "received":
        if not session_matches(
            session_id,
            workspace_uuid=str(state["workspace_uuid"]),
            surface_uuid=str(state["surface_uuid"]),
        ):
            return None
        if state.get("session_id"):
            if state.get("binding_event_id") == event.get("id"):
                return None
            return terminalize(
                runs_dir,
                state,
                status="indeterminate",
                reason="frontier_session_binding_ambiguous",
                completed_at=str(event.get("occurred_at") or utc_now()),
                event=event,
                release_lease=False,
            )
        binding = {
            **_common_event(state),
            "timestamp": utc_now(),
            "status": "running",
            "session_id": session_id,
            "binding_boot_id": event.get("boot_id"),
            "binding_seq": event.get("seq"),
            "binding_event_id": event.get("id"),
            "submitted_at": event.get("occurred_at"),
        }
        append_event(ledger, binding)
        state.update(binding)
        return None

    if name != "agent.hook.Stop" or payload.get("phase") != "completed":
        return None
    if not state.get("session_id"):
        return None
    if not _event_after_binding(state, event):
        return None
    if state["session_id"] != session_id or not session_matches(
        session_id,
        workspace_uuid=str(state["workspace_uuid"]),
        surface_uuid=str(state["surface_uuid"]),
    ):
        return None
    try:
        screen = read_screen(workspace_ref, surface_ref)
        status, reason = sentinel_status(screen, str(state["run_id"]))
    except FrontierError:
        status, reason = "indeterminate", "frontier_screen_unavailable"
    return terminalize(
        runs_dir,
        state,
        status=status,
        reason=reason,
        completed_at=str(event.get("occurred_at") or utc_now()),
        event=event,
    )


def audit_events() -> list[dict[str, Any]]:
    configured = os.environ.get("CMUX_EVENTS_LOG")
    current = Path(configured).expanduser() if configured else Path.home() / ".cmuxterm/events.jsonl"
    paths = [Path(f"{current}.1"), current]
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise FrontierError(f"cmux audit contains invalid JSON: {path}") from exc
            if not isinstance(event, dict) or not isinstance(event.get("id"), str):
                raise FrontierError(f"cmux audit contains an invalid event: {path}")
            event_id = str(event.get("id") or "")
            if event_id and event_id in seen:
                continue
            if event_id:
                seen.add(event_id)
            events.append(event)
    return events


def _continuous_audit_suffix(
    events: list[dict[str, Any]], state: dict[str, Any]
) -> list[dict[str, Any]]:
    baseline_boot = str(state.get("event_boot_id") or "")
    baseline_seq = int(state.get("after_seq") or 0)
    anchor = -1
    if baseline_seq > 0:
        for index, event in enumerate(events):
            if event.get("boot_id") == baseline_boot and event.get("seq") == baseline_seq:
                anchor = index
                break
        if anchor < 0:
            raise FrontierError("cmux audit does not contain the dispatch baseline")

    suffix = events[anchor + 1 :]
    active_boot = baseline_boot
    last_seq = baseline_seq
    seen_boots = {baseline_boot}
    for event in suffix:
        boot_id = event.get("boot_id")
        seq = event.get("seq")
        if not isinstance(boot_id, str) or not boot_id or not isinstance(seq, int):
            raise FrontierError("cmux audit event lacks boot_id/seq")
        if boot_id == active_boot:
            if seq != last_seq + 1:
                raise FrontierError("cmux audit sequence is discontinuous")
        else:
            if boot_id in seen_boots or seq != 1:
                raise FrontierError("cmux audit boot transition is discontinuous")
            seen_boots.add(boot_id)
            active_boot = boot_id
        last_seq = seq
    return suffix


def recover_from_audit(
    runs_dir: Path,
    state: dict[str, Any],
    *,
    workspace_ref: str,
    surface_ref: str,
) -> dict[str, Any]:
    current = dict(state)
    dispatched_at = timestamp_value(state.get("dispatched_at"))
    try:
        suffix = _continuous_audit_suffix(audit_events(), state)
    except FrontierError:
        return terminalize(
            runs_dir,
            current,
            status="indeterminate",
            reason="frontier_event_gap_unrecoverable",
            release_lease=False,
        )
    relevant = [
        event
        for event in suffix
        if event.get("name") in {"agent.hook.UserPromptSubmit", "agent.hook.Stop"}
        and timestamp_value(event.get("occurred_at")) is not None
        and (
            dispatched_at is None
            or timestamp_value(event.get("occurred_at")) >= dispatched_at
        )
    ]
    relevant.sort(
        key=lambda event: (
            timestamp_value(event.get("occurred_at"))
            or datetime.max.replace(tzinfo=timezone.utc),
            int(event.get("seq") or 0),
        )
    )
    for event in relevant:
        terminal = process_event(
            runs_dir,
            current,
            event,
            workspace_ref=workspace_ref,
            surface_ref=surface_ref,
            allow_cross_boot=True,
        )
        refreshed = frontier_state(
            ledger_path(runs_dir, str(state["feature"])),
            run_id=str(state["run_id"]),
            instance=str(state["instance"]),
        )
        if refreshed:
            current = refreshed
        if terminal and terminal.get("status") in TERMINAL_STATUSES:
            return terminal
    return terminalize(
        runs_dir,
        current,
        status="indeterminate",
        reason="frontier_event_gap_unrecoverable",
        release_lease=False,
    )


def abandon_run(
    runs_dir: Path,
    *,
    feature: str,
    instance: str,
    run_id: str,
    reason: str,
) -> dict[str, Any]:
    state = frontier_state(
        ledger_path(runs_dir, feature), run_id=run_id, instance=instance
    )
    if not state or state.get("runner") != "interactive":
        raise FrontierError(f"unknown frontier run: {instance}={run_id}")
    if state.get("status") in TERMINAL_STATUSES:
        if state.get("status") == "indeterminate" and state.get("lease_retained"):
            _release_frontier_lease(runs_dir, state, required=True)
        return state
    return terminalize(
        runs_dir,
        state,
        status="abandoned",
        reason=reason,
    )


def mark_indeterminate(
    runs_dir: Path,
    *,
    feature: str,
    instance: str,
    run_id: str,
    reason: str,
) -> dict[str, Any]:
    state = frontier_state(
        ledger_path(runs_dir, feature), run_id=run_id, instance=instance
    )
    if not state or state.get("runner") != "interactive":
        raise FrontierError(f"unknown frontier run: {instance}={run_id}")
    if state.get("status") in TERMINAL_STATUSES:
        return state
    return terminalize(
        runs_dir,
        state,
        status="indeterminate",
        reason=reason,
        release_lease=False,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("runs_dir")
    for name in (
        "feature", "instance", "role", "phase", "task", "workspace-uuid", "surface-uuid"
    ):
        prepare.add_argument(f"--{name}", required=True)
    abandon = sub.add_parser("abandon")
    abandon.add_argument("runs_dir")
    for name in ("feature", "instance", "run-id", "reason"):
        abandon.add_argument(f"--{name}", required=True)
    indeterminate = sub.add_parser("mark-indeterminate")
    indeterminate.add_argument("runs_dir")
    for name in ("feature", "instance", "run-id", "reason"):
        indeterminate.add_argument(f"--{name}", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    runs_dir = Path(args.runs_dir).resolve()
    try:
        if args.command == "prepare":
            result = prepare_run(
                runs_dir,
                feature=args.feature,
                instance=args.instance,
                role=args.role,
                phase=args.phase,
                task=args.task,
                workspace_uuid=args.workspace_uuid,
                surface_uuid=args.surface_uuid,
            )
        elif args.command == "abandon":
            result = abandon_run(
                runs_dir,
                feature=args.feature,
                instance=args.instance,
                run_id=args.run_id,
                reason=args.reason,
            )
        else:
            result = mark_indeterminate(
                runs_dir,
                feature=args.feature,
                instance=args.instance,
                run_id=args.run_id,
                reason=args.reason,
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (FrontierError, LeaseError) as exc:
        print(str(exc), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
