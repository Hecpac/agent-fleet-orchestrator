#!/usr/bin/env python3
"""Durable frontier-run dispatch metadata and cmux event reconciliation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time
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
HOOK_SESSION_FILES = {
    "codex": "codex-hook-sessions.json",
    "claude": "claude-hook-sessions.json",
    "opencode": "opencode-hook-sessions.json",
}
TRANSCRIPT_EVIDENCE_ATTEMPTS = 4
TRANSCRIPT_EVIDENCE_RETRY_SECONDS = 0.1


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
        "--name",
        "agent.hook.SessionEnd",
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


def session_record(
    session_id: str, *, hook_source: str = ""
) -> dict[str, Any] | None:
    hook_dir = os.environ.get("CMUX_HOOK_DIR", os.path.expanduser("~/.cmuxterm"))
    filename = HOOK_SESSION_FILES.get(hook_source)
    prefix = f"{hook_source}-"
    if filename is None or not session_id.startswith(prefix):
        return None
    try:
        data = json.loads((Path(hook_dir) / filename).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    sessions = data.get("sessions") or {}
    raw_session_id = session_id.removeprefix(prefix)
    value = sessions.get(raw_session_id)
    if not isinstance(value, dict) or value.get("sessionId") != raw_session_id:
        return None
    return value


def session_matches(
    session_id: str,
    *,
    workspace_uuid: str,
    surface_uuid: str,
    hook_source: str = "",
) -> bool:
    record = session_record(session_id, hook_source=hook_source)
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


def structured_sentinel_status(response: str, run_id: str) -> tuple[str, str]:
    pattern = re.compile(
        rf"FLEET_RESULT:{re.escape(run_id)}:(DONE|BLOCKED|FAILED)"
    )
    lines = response.splitlines()
    matches = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := pattern.fullmatch(line.strip()))
    ]
    if len(matches) != 1:
        reason = "frontier_sentinel_missing" if not matches else "frontier_sentinel_ambiguous"
        return "indeterminate", reason
    sentinel_index, match = matches[0]
    if any(line.strip() for line in lines[sentinel_index + 1 :]):
        return "indeterminate", "frontier_sentinel_not_final"
    return SENTINEL_STATUSES[match.group(1)], "frontier_sentinel_verified"


def _opencode_final_stop(payload: dict[str, Any]) -> bool:
    return bool(
        "_opencode_request_id" in payload
        and payload.get("_opencode_request_id") is None
        and isinstance(payload.get("context_length"), int)
        and int(payload["context_length"]) > 0
    )


def opencode_turn_evidence(
    session_id: str, run_id: str, stop_occurred_at: str
) -> tuple[str, str, str]:
    raw_session_id = session_id.removeprefix("opencode-")
    if not re.fullmatch(r"ses_[A-Za-z0-9]+", raw_session_id):
        raise FrontierError("OpenCode session id is invalid")
    stop_time = timestamp_value(stop_occurred_at)
    if stop_time is None:
        raise FrontierError("OpenCode Stop has no valid timestamp")
    stop_millis = int(stop_time.timestamp() * 1000)
    query = (
        "SELECT m.id AS message_id, m.time_created AS message_created, "
        "m.data AS message_data, p.id AS part_id, "
        "p.time_created AS part_created, p.data AS part_data "
        "FROM message m LEFT JOIN part p ON p.message_id = m.id "
        "AND json_extract(p.data, '$.type') = 'text' "
        f"WHERE m.session_id = '{raw_session_id}' "
        "ORDER BY m.time_created, p.time_created, p.id"
    )
    try:
        result = subprocess.run(
            ["opencode", "db", "--format", "json", query],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FrontierError(f"cannot query OpenCode turn evidence: {exc}") from exc
    if result.returncode != 0:
        raise FrontierError("OpenCode turn evidence query failed")
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FrontierError("OpenCode turn evidence query returned invalid JSON") from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise FrontierError("OpenCode turn evidence query returned invalid rows")

    messages: dict[str, dict[str, Any]] = {}
    for row in rows:
        message_id = row.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise FrontierError("OpenCode turn evidence has invalid message id")
        message_created = row.get("message_created")
        if isinstance(message_created, bool) or not isinstance(message_created, int):
            raise FrontierError("OpenCode turn evidence has invalid message timestamp")
        try:
            message_data = json.loads(row.get("message_data") or "")
        except json.JSONDecodeError as exc:
            raise FrontierError("OpenCode turn evidence has invalid message data") from exc
        if not isinstance(message_data, dict):
            raise FrontierError("OpenCode turn evidence message is not an object")
        message = messages.setdefault(
            message_id,
            {
                "id": message_id,
                "created": message_created,
                "data": message_data,
                "parts": [],
            },
        )
        if message["data"] != message_data or message["created"] != message_created:
            raise FrontierError("OpenCode turn evidence message rows disagree")
        part_data_raw = row.get("part_data")
        if part_data_raw is None:
            continue
        try:
            part_data = json.loads(part_data_raw)
        except json.JSONDecodeError as exc:
            raise FrontierError("OpenCode turn evidence has invalid part data") from exc
        if not isinstance(part_data, dict) or part_data.get("type") != "text":
            raise FrontierError("OpenCode turn evidence has invalid text part")
        part_id = row.get("part_id")
        part_created = row.get("part_created")
        if (
            not isinstance(part_id, str)
            or not part_id
            or isinstance(part_created, bool)
            or not isinstance(part_created, int)
        ):
            raise FrontierError("OpenCode turn evidence has invalid part identity")
        text = part_data.get("text")
        if not isinstance(text, str):
            raise FrontierError("OpenCode turn evidence text part lacks text")
        message["parts"].append((part_created, part_id, text))

    ordered = sorted(
        messages.values(), key=lambda item: (item["created"], item["id"])
    )
    contract_marker = f"FLEET_RESULT:{run_id}:<STATUS>"
    matching_users = [
        (index, message)
        for index, message in enumerate(ordered)
        if message["data"].get("role") == "user"
        and contract_marker in "\n".join(part[2] for part in message["parts"])
    ]
    if len(matching_users) != 1:
        raise FrontierError("OpenCode turn evidence has ambiguous user binding")
    user_index, user_message = matching_users[0]
    next_user_created = None
    for message in ordered[user_index + 1 :]:
        if message["data"].get("role") == "user":
            next_user_created = message["created"]
            break

    candidates: list[tuple[int, int, str, dict[str, Any]]] = []
    user_created = user_message["created"]
    for message in ordered[user_index + 1 :]:
        created = message["created"]
        if next_user_created is not None and created >= next_user_created:
            break
        data = message["data"]
        time_data = data.get("time") or {}
        completed = time_data.get("completed") if isinstance(time_data, dict) else None
        if (
            data.get("role") == "assistant"
            and created >= user_created
            and isinstance(completed, int)
            and not isinstance(completed, bool)
            and completed <= stop_millis
            and message["parts"]
        ):
            candidates.append((completed, created, message["id"], message))
    if not candidates:
        raise FrontierError("OpenCode turn evidence has no completed assistant response")
    _, _, _, assistant = max(candidates)
    assistant_data = assistant["data"]
    provider = assistant_data.get("providerID")
    model = assistant_data.get("modelID")
    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
        raise FrontierError("OpenCode turn evidence lacks provider/model identity")
    response = "\n".join(
        part[2]
        for part in sorted(
            assistant["parts"], key=lambda part: (part[0], part[1])
        )
    )
    return response, provider, model


def _transcript_rows(session_id: str, hook_source: str) -> list[dict[str, Any]]:
    record = session_record(session_id, hook_source=hook_source)
    transcript_path = record.get("transcriptPath") if record else None
    if not isinstance(transcript_path, str) or not transcript_path:
        raise FrontierError(f"{hook_source} session lacks transcript path")
    try:
        lines = Path(transcript_path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FrontierError(f"cannot read {hook_source} transcript: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FrontierError(f"{hook_source} transcript contains invalid JSON") from exc
        if not isinstance(row, dict):
            raise FrontierError(f"{hook_source} transcript row is not an object")
        rows.append(row)
    return rows


def _message_text(content: Any, *, text_types: set[str]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in text_types:
            continue
        text = part.get("text")
        if isinstance(text, str):
            texts.append(text)
    return "\n".join(texts)


def _claude_human_prompt(row: dict[str, Any]) -> bool:
    if row.get("type") != "user" or row.get("isSidechain") is True:
        return False
    message = row.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    origin = row.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == "human":
        return True
    if row.get("promptSource"):
        return True
    content = message.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    return bool(content) and not any(
        isinstance(part, dict) and part.get("type") == "tool_result"
        for part in content
    )


def codex_turn_evidence(
    session_id: str, run_id: str, stop_occurred_at: str
) -> tuple[str, str, str]:
    raw_session_id = session_id.removeprefix("codex-")
    if not raw_session_id or raw_session_id == session_id:
        raise FrontierError("Codex session id is invalid")
    stop_time = timestamp_value(stop_occurred_at)
    if stop_time is None:
        raise FrontierError("Codex Stop has no valid timestamp")
    rows = _transcript_rows(session_id, "codex")
    metadata = [
        row.get("payload")
        for row in rows
        if row.get("type") == "session_meta"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("id") == raw_session_id
    ]
    if len(metadata) != 1:
        raise FrontierError("Codex transcript has ambiguous session metadata")
    provider = metadata[0].get("model_provider")
    if not isinstance(provider, str) or not provider:
        raise FrontierError("Codex transcript lacks provider identity")

    marker = f"FLEET_RESULT:{run_id}:<STATUS>"
    matching_users: list[int] = []
    for index, row in enumerate(rows):
        payload = row.get("payload")
        if (
            row.get("type") == "response_item"
            and isinstance(payload, dict)
            and payload.get("type") == "message"
            and payload.get("role") == "user"
            and marker in _message_text(payload.get("content"), text_types={"input_text"})
        ):
            matching_users.append(index)
    if len(matching_users) != 1:
        raise FrontierError("Codex transcript has ambiguous user binding")
    user_index = matching_users[0]
    next_user_index = len(rows)
    for index in range(user_index + 1, len(rows)):
        payload = rows[index].get("payload")
        if (
            rows[index].get("type") == "response_item"
            and isinstance(payload, dict)
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            next_user_index = index
            break

    model = ""
    for row in rows[: user_index + 1]:
        payload = row.get("payload")
        if row.get("type") == "turn_context" and isinstance(payload, dict):
            candidate = payload.get("model")
            if isinstance(candidate, str) and candidate:
                model = candidate
    if not model:
        raise FrontierError("Codex transcript lacks model identity")

    candidates: list[tuple[datetime, int, str]] = []
    for index in range(user_index + 1, next_user_index):
        row = rows[index]
        payload = row.get("payload")
        if (
            row.get("type") != "response_item"
            or not isinstance(payload, dict)
            or payload.get("type") != "message"
            or payload.get("role") != "assistant"
        ):
            continue
        occurred_at = timestamp_value(row.get("timestamp"))
        response = _message_text(payload.get("content"), text_types={"output_text"})
        if occurred_at is not None and occurred_at <= stop_time and response:
            candidates.append((occurred_at, index, response))
    if not candidates:
        raise FrontierError("Codex transcript has no final assistant response")
    _, _, response = max(candidates)
    return response, provider, model


def claude_turn_evidence(
    session_id: str, run_id: str, stop_occurred_at: str
) -> tuple[str, str, str]:
    raw_session_id = session_id.removeprefix("claude-")
    if not raw_session_id or raw_session_id == session_id:
        raise FrontierError("Claude session id is invalid")
    stop_time = timestamp_value(stop_occurred_at)
    if stop_time is None:
        raise FrontierError("Claude Stop has no valid timestamp")
    rows = _transcript_rows(session_id, "claude")
    marker = f"FLEET_RESULT:{run_id}:<STATUS>"
    matching_users: list[int] = []
    for index, row in enumerate(rows):
        message = row.get("message")
        if (
            _claude_human_prompt(row)
            and row.get("sessionId") == raw_session_id
            and isinstance(message, dict)
            and marker in _message_text(message.get("content"), text_types={"text"})
        ):
            matching_users.append(index)
    if len(matching_users) != 1:
        raise FrontierError("Claude transcript has ambiguous user binding")
    user_index = matching_users[0]
    next_user_index = len(rows)
    for index in range(user_index + 1, len(rows)):
        row = rows[index]
        message = row.get("message")
        if (
            _claude_human_prompt(row)
            and row.get("sessionId") == raw_session_id
        ):
            next_user_index = index
            break

    candidates: list[tuple[datetime, int, str, str]] = []
    for index in range(user_index + 1, next_user_index):
        row = rows[index]
        message = row.get("message")
        if (
            row.get("type") != "assistant"
            or row.get("sessionId") != raw_session_id
            or row.get("isSidechain") is True
            or not isinstance(message, dict)
            or message.get("role") != "assistant"
            or message.get("stop_reason") != "end_turn"
        ):
            continue
        occurred_at = timestamp_value(row.get("timestamp"))
        response = _message_text(message.get("content"), text_types={"text"})
        model = message.get("model")
        if (
            occurred_at is not None
            and occurred_at <= stop_time
            and response
            and isinstance(model, str)
            and model
        ):
            candidates.append((occurred_at, index, response, model))
    if not candidates:
        raise FrontierError("Claude transcript has no final assistant response")
    _, _, response, model = max(candidates)
    return response, "anthropic", model


def transcript_turn_evidence(
    reader: Any, session_id: str, run_id: str, stop_occurred_at: str
) -> tuple[str, str, str]:
    last_error: FrontierError | None = None
    for attempt in range(TRANSCRIPT_EVIDENCE_ATTEMPTS):
        try:
            return reader(session_id, run_id, stop_occurred_at)
        except FrontierError as exc:
            last_error = exc
            if attempt + 1 < TRANSCRIPT_EVIDENCE_ATTEMPTS:
                time.sleep(TRANSCRIPT_EVIDENCE_RETRY_SECONDS)
    assert last_error is not None
    raise last_error


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
    provider: str = "",
    model: str = "",
    hook_source: str = "",
) -> dict[str, Any]:
    if not provider or not model or not hook_source:
        raise FrontierError("frontier runs require provider, model, and hook source identity")
    if hook_source not in HOOK_SESSION_FILES:
        raise FrontierError(f"unsupported frontier hook source: {hook_source}")
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
        "provider": provider,
        "model": model,
        "hook_source": hook_source,
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
            "provider": provider,
            "model": model,
            "hook_source": hook_source,
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
        "provider",
        "model",
        "hook_source",
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
    expected_source = str(state.get("hook_source") or "")
    if expected_source and (
        event.get("source") != expected_source
        or payload.get("_source") != expected_source
    ):
        return None
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
            hook_source=expected_source,
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

    if name == "agent.hook.SessionEnd" and payload.get("phase") == "completed":
        if (
            expected_source != "claude"
            or not state.get("session_id")
            or not _event_after_binding(state, event)
            or state["session_id"] != session_id
        ):
            return None
        return terminalize(
            runs_dir,
            state,
            status="indeterminate",
            reason="frontier_session_ended_without_stop",
            completed_at=str(event.get("occurred_at") or utc_now()),
            event=event,
            release_lease=False,
        )

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
        hook_source=expected_source,
    ):
        return None
    if expected_source == "opencode":
        if not _opencode_final_stop(payload):
            return None
        try:
            response, actual_provider, actual_model = opencode_turn_evidence(
                session_id,
                str(state["run_id"]),
                str(event.get("occurred_at") or ""),
            )
            status, reason = structured_sentinel_status(response, str(state["run_id"]))
            if (
                actual_provider != str(state.get("provider") or "")
                or actual_model != str(state.get("model") or "")
            ):
                status, reason = "indeterminate", "frontier_opencode_identity_mismatch"
        except FrontierError:
            status, reason = "indeterminate", "frontier_opencode_evidence_unavailable"
        return terminalize(
            runs_dir,
            state,
            status=status,
            reason=reason,
            completed_at=str(event.get("occurred_at") or utc_now()),
            event=event,
            release_lease=status != "indeterminate",
        )
    evidence_readers = {
        "codex": codex_turn_evidence,
        "claude": claude_turn_evidence,
    }
    evidence_reader = evidence_readers.get(expected_source)
    if evidence_reader is None:
        return None
    try:
        response, actual_provider, actual_model = transcript_turn_evidence(
            evidence_reader,
            session_id,
            str(state["run_id"]),
            str(event.get("occurred_at") or ""),
        )
        status, reason = structured_sentinel_status(response, str(state["run_id"]))
        if (
            actual_provider != str(state.get("provider") or "")
            or actual_model != str(state.get("model") or "")
        ):
            status, reason = (
                "indeterminate",
                f"frontier_{expected_source}_identity_mismatch",
            )
    except FrontierError:
        status, reason = (
            "indeterminate",
            f"frontier_{expected_source}_evidence_unavailable",
        )
    return terminalize(
        runs_dir,
        state,
        status=status,
        reason=reason,
        completed_at=str(event.get("occurred_at") or utc_now()),
        event=event,
        release_lease=status != "indeterminate",
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
        if event.get("name")
        in {
            "agent.hook.UserPromptSubmit",
            "agent.hook.Stop",
            "agent.hook.SessionEnd",
        }
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
    for name in ("provider", "model", "hook-source"):
        prepare.add_argument(f"--{name}", default="")
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
                provider=args.provider,
                model=args.model,
                hook_source=args.hook_source,
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
