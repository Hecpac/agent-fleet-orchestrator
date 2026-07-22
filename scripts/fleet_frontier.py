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
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any

import fleet_json
import fleet_providers
import fleet_safe_paths
from fleet_leases import (
    LeaseError,
    acquire_frontier,
    coordinator,
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
    adapter.hook_source: adapter.session_file
    for adapter in fleet_providers.default_adapters()
    if adapter.hook_source and adapter.session_file
}
TRANSCRIPT_EVIDENCE_ATTEMPTS = 4
TRANSCRIPT_EVIDENCE_RETRY_SECONDS = 0.1
OPENCODE_STATE_ROOT = Path("/tmp/agent-fleet-orchestrator-opencode")
KIMI_STATE_ROOT = Path(
    os.environ.get("FLEET_KIMI_STATE_ROOT", "/tmp/agent-fleet-orchestrator-kimi")
)
KIMI_WIRE_PROTOCOLS = {"1.2", "1.3"}
SAFE_FEATURE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_RESULT_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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


def _safe_component(value: str, field: str, *, feature: bool = False) -> str:
    pattern = SAFE_FEATURE_COMPONENT if feature else SAFE_RESULT_COMPONENT
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise FrontierError(f"invalid frontier {field}")
    return value


def _canonical_uuid(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise FrontierError(f"frontier {field} must be a canonical UUID")
    try:
        parsed = str(uuid.UUID(value))
    except ValueError as exc:
        raise FrontierError(f"frontier {field} must be a canonical UUID") from exc
    if parsed != value.lower():
        raise FrontierError(f"frontier {field} must be a canonical UUID")
    return parsed.upper()


def ledger_path(runs_dir: Path, feature: str) -> Path:
    safe_feature = _safe_component(feature, "feature", feature=True)
    return runs_dir / f"fleet-{safe_feature}.ledger.jsonl"


def result_path(runs_dir: Path, feature: str, run_id: str) -> Path:
    return runs_dir / _result_relative(feature, run_id)


def _result_relative(feature: str, run_id: str) -> str:
    if not isinstance(feature, str) or not SAFE_FEATURE_COMPONENT.fullmatch(feature):
        raise FrontierError("invalid result feature")
    if not isinstance(run_id, str) or not SAFE_RESULT_COMPONENT.fullmatch(run_id):
        raise FrontierError("invalid result run_id")
    return f"results/{feature}/{run_id}.txt"


def _prompt_relative(feature: str, run_id: str) -> str:
    if not isinstance(feature, str) or not SAFE_FEATURE_COMPONENT.fullmatch(feature):
        raise FrontierError("invalid prompt feature")
    if not isinstance(run_id, str) or not SAFE_RESULT_COMPONENT.fullmatch(run_id):
        raise FrontierError("invalid prompt run_id")
    return f"prompts/{feature}/{run_id}.txt"


def read_frontier_result(
    runs_dir: Path,
    *,
    feature: str,
    run_id: str,
    recorded_path: Path | None = None,
) -> bytes:
    """Read only the exact nominal result beneath a descriptor-pinned runs root."""

    relative = _result_relative(feature, run_id)
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            if recorded_path is not None:
                if not recorded_path.is_absolute() or len(recorded_path.parents) < 3:
                    raise FrontierError(
                        "result_file is outside the exact fleet result store"
                    )
                # Accept a trusted root alias such as macOS /var -> /private/var,
                # while keeping every descendant comparison lexical.  Resolving
                # the complete recorded path would silently follow a hostile
                # symlink below the selected runs root.
                recorded_root = recorded_path.parents[2]
                if recorded_path != recorded_root / relative:
                    raise FrontierError(
                        "result_file is outside the exact fleet result store"
                    )
                if fleet_safe_paths.canonical_root(recorded_root) != rooted.root:
                    raise FrontierError(
                        "result_file is outside the exact fleet result store"
                    )
            content = rooted.read_regular(
                relative,
                directory_modes=(0o755, 0o700),
                max_bytes=16 * 1024 * 1024,
            )
            rooted.assert_root_binding()
            return content
    except fleet_safe_paths.SafePathError as exc:
        raise FrontierError(f"unsafe frontier result path: {exc}") from exc


def persist_frontier_result(
    runs_dir: Path,
    state: dict[str, Any],
    response: str,
) -> Path:
    """Persist the exact structured response before a succeeded terminal event."""
    if not response:
        raise FrontierError("frontier result is empty")
    payload = response.encode("utf-8")
    relative = _result_relative(str(state["feature"]), str(state["run_id"]))
    try:
        with coordinator(runs_dir):
            with fleet_safe_paths.RootedFS(runs_dir) as rooted:
                path = rooted.atomic_write(
                    relative,
                    payload,
                    directory_modes=(0o755, 0o700),
                )
                rooted.assert_root_binding()
                return path
    except fleet_safe_paths.SafePathError as exc:
        raise FrontierError(f"unsafe frontier result path: {exc}") from exc


def frontier_state(
    ledger: Path,
    *,
    run_id: str,
    instance: str,
    runs_dir: Path | None = None,
) -> dict[str, Any] | None:
    events = events_for_run(
        ledger,
        run_id=run_id,
        instance=instance,
        runs_dir=runs_dir,
    )
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
        try:
            raw = proc.stdout.readline()
            frame = fleet_json.loads(raw)
        except (UnicodeError, fleet_json.FleetJSONError) as exc:
            raise FrontierError("cmux event snapshot returned invalid JSON") from exc
        validate_event_ack(frame)
        return frame
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def validate_event_ack(frame: Any) -> None:
    if not isinstance(frame, dict):
        raise FrontierError("invalid cmux-events ACK")
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


def session_record(session_id: str, *, hook_source: str = "") -> dict[str, Any] | None:
    hook_dir = (
        os.environ.get("CMUX_HOOK_DIR")
        or os.environ.get("FLEET_CONTROLLER_HOOK_DIR")
        or os.path.expanduser("~/.cmuxterm")
    )
    filename = HOOK_SESSION_FILES.get(hook_source)
    prefix = f"{hook_source}-"
    if filename is None or not session_id.startswith(prefix):
        return None
    try:
        data = fleet_json.loads((Path(hook_dir) / filename).read_bytes())
    except (OSError, fleet_json.FleetJSONError):
        return None
    if not isinstance(data, dict):
        return None
    sessions = data.get("sessions") or {}
    if not isinstance(sessions, dict):
        return None
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


def prompt_with_contract(task: str, run_id: str, *, hook_source: str = "") -> str:
    if hook_source not in HOOK_SESSION_FILES:
        return fleet_providers.BaseAdapter.logical_prompt(task, run_id)
    defaults = {
        "codex": ("openai", "compatibility-model"),
        "claude": ("anthropic", "compatibility-model"),
        "kimi": ("moonshot-ai", "compatibility-model"),
        "opencode": ("compatibility-provider", "compatibility-model"),
    }
    provider, model = defaults[hook_source]
    configured = fleet_providers.identity(provider, model, None, hook_source)
    adapter = fleet_providers.DEFAULT_REGISTRY.resolve(
        hook_source=hook_source, provider=provider
    )
    return adapter.prepare_submission(configured, task, run_id, Path("/prompt"))[
        "prompt"
    ]


def sentinel_status(screen: str, run_id: str) -> tuple[str, str]:
    pattern = re.compile(rf"FLEET_RESULT:{re.escape(run_id)}:(DONE|BLOCKED|FAILED)")
    lines = screen.splitlines()
    matches = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := pattern.fullmatch(line.strip()))
    ]
    if len(matches) != 1:
        reason = (
            "frontier_sentinel_missing"
            if not matches
            else "frontier_sentinel_ambiguous"
        )
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
    pattern = re.compile(rf"FLEET_RESULT:{re.escape(run_id)}:(DONE|BLOCKED|FAILED)")
    lines = response.splitlines()
    matches = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := pattern.fullmatch(line.strip()))
    ]
    if len(matches) != 1:
        reason = (
            "frontier_sentinel_missing"
            if not matches
            else "frontier_sentinel_ambiguous"
        )
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


def opencode_data_home(surface_uuid: str) -> Path:
    try:
        canonical_surface = str(uuid.UUID(surface_uuid)).upper()
    except ValueError as exc:
        raise FrontierError("OpenCode evidence surface id is invalid") from exc
    surface_root = OPENCODE_STATE_ROOT / canonical_surface
    data_home = surface_root / "data"
    if (
        OPENCODE_STATE_ROOT.is_symlink()
        or surface_root.is_symlink()
        or data_home.is_symlink()
    ):
        raise FrontierError("OpenCode evidence state must not use symlinks")
    try:
        resolved_root = OPENCODE_STATE_ROOT.resolve(strict=True)
        resolved_surface = surface_root.resolve(strict=True)
        resolved_data = data_home.resolve(strict=True)
        resolved_surface.relative_to(resolved_root)
        resolved_data.relative_to(resolved_surface)
    except (OSError, ValueError) as exc:
        raise FrontierError("OpenCode evidence data home is unavailable") from exc
    return data_home


def cleanup_opencode_data_home(surface_uuid: str) -> bool:
    """Remove one OpenCode evidence home after its frontier run is terminal."""
    try:
        canonical_surface = str(uuid.UUID(surface_uuid)).upper()
    except ValueError as exc:
        raise FrontierError("OpenCode evidence surface id is invalid") from exc
    if OPENCODE_STATE_ROOT.is_symlink():
        raise FrontierError("OpenCode evidence state root must not be a symlink")
    surface_root = OPENCODE_STATE_ROOT / canonical_surface
    if surface_root.is_symlink():
        raise FrontierError("OpenCode evidence surface state must not be a symlink")
    if not surface_root.exists():
        return False
    if not surface_root.is_dir():
        raise FrontierError("OpenCode evidence surface state is unsafe")
    try:
        resolved_root = OPENCODE_STATE_ROOT.resolve(strict=True)
        resolved_surface = surface_root.resolve(strict=True)
        resolved_surface.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise FrontierError("OpenCode evidence surface state is unsafe") from exc
    shutil.rmtree(surface_root)
    try:
        OPENCODE_STATE_ROOT.rmdir()
    except OSError:
        pass
    return True


def opencode_turn_evidence(
    session_id: str, run_id: str, stop_occurred_at: str, surface_uuid: str
) -> tuple[str, str, str, str | None]:
    raw_session_id = session_id.removeprefix("opencode-")
    if not re.fullmatch(r"ses_[A-Za-z0-9]+", raw_session_id):
        raise FrontierError("OpenCode session id is invalid")
    stop_time = timestamp_value(stop_occurred_at)
    if stop_time is None:
        raise FrontierError("OpenCode Stop has no valid timestamp")
    stop_millis = int(stop_time.timestamp() * 1000)
    data_home = opencode_data_home(surface_uuid)
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
            ["opencode", "db", "--pure", "--format", "json", query],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env={**os.environ, "XDG_DATA_HOME": str(data_home)},
        )
    except UnicodeError as exc:
        raise FrontierError(
            "OpenCode turn evidence query returned invalid JSON"
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FrontierError(f"cannot query OpenCode turn evidence: {exc}") from exc
    if result.returncode != 0:
        raise FrontierError("OpenCode turn evidence query failed")
    try:
        rows = fleet_json.loads(result.stdout)
    except fleet_json.FleetJSONError as exc:
        raise FrontierError(
            "OpenCode turn evidence query returned invalid JSON"
        ) from exc
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
            message_data = fleet_json.loads(row.get("message_data") or "")
        except fleet_json.FleetJSONError as exc:
            raise FrontierError(
                "OpenCode turn evidence has invalid message data"
            ) from exc
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
            part_data = fleet_json.loads(part_data_raw)
        except fleet_json.FleetJSONError as exc:
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

    ordered = sorted(messages.values(), key=lambda item: (item["created"], item["id"]))
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
        raise FrontierError(
            "OpenCode turn evidence has no completed assistant response"
        )
    _, _, _, assistant = max(candidates)
    assistant_data = assistant["data"]
    provider = assistant_data.get("providerID")
    model = assistant_data.get("modelID")
    variant = assistant_data.get("variant")
    if (
        not isinstance(provider, str)
        or not provider
        or not isinstance(model, str)
        or not model
    ):
        raise FrontierError("OpenCode turn evidence lacks provider/model identity")
    if variant is not None and not isinstance(variant, str):
        raise FrontierError("OpenCode turn evidence has invalid variant identity")
    response = "\n".join(
        part[2]
        for part in sorted(assistant["parts"], key=lambda part: (part[0], part[1]))
    )
    return response, provider, model, variant


def _transcript_rows(session_id: str, hook_source: str) -> list[dict[str, Any]]:
    record = session_record(session_id, hook_source=hook_source)
    transcript_path = record.get("transcriptPath") if record else None
    if not isinstance(transcript_path, str) or not transcript_path:
        raise FrontierError(f"{hook_source} session lacks transcript path")
    try:
        lines = Path(transcript_path).expanduser().read_bytes().splitlines()
    except OSError as exc:
        raise FrontierError(f"cannot read {hook_source} transcript: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = fleet_json.loads(line)
        except fleet_json.FleetJSONError as exc:
            raise FrontierError(
                f"{hook_source} transcript contains invalid JSON"
            ) from exc
        if not isinstance(row, dict):
            raise FrontierError(f"{hook_source} transcript row is not an object")
        rows.append(row)
    return rows


def kimi_state_dir(surface_uuid: str) -> Path:
    try:
        canonical_surface = str(uuid.UUID(surface_uuid)).upper()
    except ValueError as exc:
        raise FrontierError("Kimi evidence surface id is invalid") from exc
    if not KIMI_STATE_ROOT.is_absolute():
        raise FrontierError("Kimi evidence state root must be absolute")
    surface_root = KIMI_STATE_ROOT / canonical_surface
    if KIMI_STATE_ROOT.is_symlink() or surface_root.is_symlink():
        raise FrontierError("Kimi evidence state must not use symlinks")
    try:
        resolved_root = KIMI_STATE_ROOT.resolve(strict=True)
        resolved_surface = surface_root.resolve(strict=True)
        resolved_surface.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise FrontierError("Kimi evidence state is unavailable") from exc
    return resolved_surface


def kimi_hook_events(surface_uuid: str) -> list[dict[str, Any]]:
    events_path = kimi_state_dir(surface_uuid) / "events.jsonl"
    if events_path.is_symlink():
        raise FrontierError("Kimi event evidence must not be a symlink")
    try:
        lines = events_path.read_bytes().splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise FrontierError("cannot read Kimi event evidence") from exc
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in lines:
        if not raw.strip():
            continue
        try:
            event = fleet_json.loads(raw)
        except fleet_json.FleetJSONError as exc:
            raise FrontierError("Kimi event evidence contains invalid JSON") from exc
        if not isinstance(event, dict):
            raise FrontierError("Kimi event evidence row is not an object")
        payload = event.get("payload")
        event_id = event.get("id")
        valid = bool(
            event.get("type") == "event"
            and isinstance(event_id, str)
            and event_id
            and event_id not in seen
            and event.get("source") == "kimi"
            and event.get("name")
            in {"agent.hook.UserPromptSubmit", "agent.hook.Stop"}
            and isinstance(event.get("boot_id"), str)
            and str(event["boot_id"]).startswith("kimi-")
            and isinstance(event.get("seq"), int)
            and not isinstance(event.get("seq"), bool)
            and int(event["seq"]) > 0
            and str(event.get("surface_id") or "").upper()
            == str(uuid.UUID(surface_uuid)).upper()
            and timestamp_value(event.get("occurred_at")) is not None
            and isinstance(payload, dict)
            and payload.get("_source") == "kimi"
            and payload.get("phase") in {"received", "completed"}
            and isinstance(payload.get("session_id"), str)
            and str(payload["session_id"]).startswith("kimi-")
        )
        if not valid:
            raise FrontierError("Kimi event evidence contains an invalid event")
        seen.add(str(event_id))
        events.append(event)
    return events


def _kimi_wire_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, list):
        return ""
    parts: list[str] = []
    for part in payload:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def kimi_turn_evidence(
    session_id: str, run_id: str, stop_occurred_at: str
) -> tuple[str, str, str]:
    raw_session_id = session_id.removeprefix("kimi-")
    try:
        if str(uuid.UUID(raw_session_id)) != raw_session_id.lower():
            raise ValueError
    except ValueError as exc:
        raise FrontierError("Kimi session id is invalid") from exc
    stop_time = timestamp_value(stop_occurred_at)
    if stop_time is None:
        raise FrontierError("Kimi Stop has no valid timestamp")
    record = session_record(session_id, hook_source="kimi")
    provider = record.get("provider") if record else None
    model = record.get("model") if record else None
    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
        raise FrontierError("Kimi session lacks provider/model identity")
    rows = _transcript_rows(session_id, "kimi")
    metadata = [
        row for row in rows
        if row.get("type") == "metadata"
        and row.get("protocol_version") in KIMI_WIRE_PROTOCOLS
    ]
    if len(metadata) != 1:
        raise FrontierError("Kimi transcript has invalid Wire metadata")
    marker = f"FLEET_RESULT:{run_id}:<STATUS>"
    matching_turns: list[int] = []
    for index, row in enumerate(rows):
        message = row.get("message")
        if (
            isinstance(message, dict)
            and message.get("type") == "TurnBegin"
            and isinstance(message.get("payload"), dict)
            and marker in _kimi_wire_text(message["payload"].get("user_input"))
        ):
            matching_turns.append(index)
    if len(matching_turns) != 1:
        raise FrontierError("Kimi transcript has ambiguous user binding")
    begin = matching_turns[0]
    end = len(rows)
    for index in range(begin + 1, len(rows)):
        message = rows[index].get("message")
        if isinstance(message, dict) and message.get("type") == "TurnBegin":
            end = index
            break
    turn_rows = rows[begin + 1 : end]
    turn_end_indexes: list[int] = []
    for index, row in enumerate(turn_rows):
        timestamp = row.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise FrontierError("Kimi transcript has an invalid timestamp")
        message = row.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("payload"), dict):
            raise FrontierError("Kimi transcript has an invalid message")
        if message.get("type") == "TurnEnd":
            try:
                wire_stop = datetime.fromtimestamp(
                    float(timestamp), timezone.utc
                ).isoformat()
            except (OSError, OverflowError, ValueError) as exc:
                raise FrontierError("Kimi transcript has an invalid timestamp") from exc
            if wire_stop == stop_time.isoformat():
                turn_end_indexes.append(index)
    if len(turn_end_indexes) != 1 or turn_end_indexes[0] != len(turn_rows) - 1:
        raise FrontierError("Kimi transcript lacks one completed TurnEnd")
    visible: list[str] = []
    for row in turn_rows[: turn_end_indexes[0]]:
        message = row["message"]
        if message.get("type") == "ContentPart":
            payload = message["payload"]
            if payload.get("type") == "text" and isinstance(payload.get("text"), str):
                visible.append(payload["text"])
    response = "".join(visible)
    if not response:
        raise FrontierError("Kimi transcript has no final assistant response")
    return response, provider, model


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
        isinstance(part, dict) and part.get("type") == "tool_result" for part in content
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
            and marker
            in _message_text(payload.get("content"), text_types={"input_text"})
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
        if _claude_human_prompt(row) and row.get("sessionId") == raw_session_id:
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
    variant: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    # Validate every durable/path-bearing identifier before consulting or
    # mutating the ledger.  In particular, feature must never reach
    # ``ledger_path`` or the lease store with traversal components.
    feature = _safe_component(feature, "feature", feature=True)
    instance = _safe_component(instance, "instance")
    role = _safe_component(role, "role")
    phase = _safe_component(phase, "phase")
    workspace_uuid = _canonical_uuid(workspace_uuid, "workspace_uuid")
    surface_uuid = _canonical_uuid(surface_uuid, "surface_uuid")
    if not isinstance(task, str) or not task:
        raise FrontierError("frontier task must be a non-empty string")
    if not provider or not model or not hook_source:
        raise FrontierError(
            "frontier runs require provider, model, and hook source identity"
        )
    _safe_component(hook_source, "hook_source")
    if hook_source not in HOOK_SESSION_FILES:
        raise FrontierError(f"unsupported frontier hook source: {hook_source}")
    if variant and hook_source != "opencode":
        raise FrontierError("frontier variant identity is supported only for OpenCode")
    if not isinstance(variant, str):
        raise FrontierError("frontier variant identity must be a string")
    if any(character in variant for character in ("\n", "\r", "\x00", "\x1f")):
        raise FrontierError(
            "frontier variant identity contains a forbidden control character"
        )
    try:
        configured_provider = fleet_providers.identity(
            provider, model, variant or None, hook_source
        )
        adapter = fleet_providers.adapter_for(configured_provider)
    except fleet_providers.ProviderError as exc:
        raise FrontierError(
            f"frontier provider adapter rejected identity: {exc}"
        ) from exc
    if run_id:
        try:
            canonical_run_id = str(uuid.UUID(run_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise FrontierError("frontier run_id must be a canonical UUID") from exc
        if canonical_run_id != run_id:
            raise FrontierError("frontier run_id must be a canonical UUID")
        run_id = canonical_run_id
    else:
        run_id = str(uuid.uuid4())
    task_sha256 = hashlib.sha256(task.encode("utf-8")).hexdigest()
    ledger = ledger_path(runs_dir, feature)
    if events_for_run(ledger, run_id=run_id, runs_dir=runs_dir):
        raise FrontierError(f"frontier run_id is already durable: {run_id}")
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
        "workspace_uuid": workspace_uuid,
        "surface_uuid": surface_uuid,
        "provider": provider,
        "model": model,
        "hook_source": hook_source,
        "tracking_protocol": "control-v1",
        "preparing_at": preparing_at,
    }
    if variant:
        preparing["variant"] = variant
    if not append_event(ledger, preparing, runs_dir=runs_dir):
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
        prompt_relative = _prompt_relative(feature, run_id)
        prompt_file = runs_dir / prompt_relative
        try:
            with fleet_safe_paths.RootedFS(runs_dir) as rooted:
                submission = adapter.prepare_submission(
                    configured_provider, task, run_id, prompt_file
                )
                prompt = submission["prompt"]
                rooted.atomic_write(
                    prompt_relative,
                    prompt.encode("utf-8"),
                    directory_modes=(0o755, 0o700),
                )
                rooted.assert_root_binding()
        except fleet_safe_paths.SafePathError as exc:
            raise FrontierError(f"unsafe frontier prompt path: {exc}") from exc
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
            "workspace_uuid": workspace_uuid,
            "surface_uuid": surface_uuid,
            "provider": provider,
            "model": model,
            "hook_source": hook_source,
            "provider_adapter": adapter.name,
            "tracking_protocol": "control-v1",
            "event_boot_id": ack["boot_id"],
            "after_seq": resume["latest_seq"],
            "event_oldest_seq": resume.get("oldest_seq"),
            "dispatched_at": dispatched_at,
        }
        if variant:
            event["variant"] = variant
        if not append_event(ledger, event, runs_dir=runs_dir):
            raise FrontierError(f"frontier run unexpectedly already terminal: {run_id}")
        return {
            **event,
            "lease": str(lease),
            "prompt": prompt,
            "prompt_path": str(prompt_file),
            "submission_payload": submission["payload"],
            "submission_transport": submission["transport"],
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
            runs_dir=runs_dir,
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
        "provider_adapter",
        "tracking_protocol",
        "variant",
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
    result_file: Path | None = None,
) -> dict[str, Any]:
    if status not in STATUS_CODES:
        raise FrontierError(f"invalid frontier terminal status: {status}")
    ledger = ledger_path(runs_dir, str(state["feature"]))
    existing = latest_event(
        ledger,
        run_id=str(state["run_id"]),
        instance=str(state["instance"]),
        runs_dir=runs_dir,
    )
    if existing and existing.get("status") in TERMINAL_STATUSES:
        if existing.get("hook_source") == "opencode" and existing.get("surface_uuid"):
            try:
                cleanup_opencode_data_home(str(existing["surface_uuid"]))
            except FrontierError:
                pass
        return existing
    if status == "succeeded" and result_file is None:
        raise FrontierError(
            "succeeded frontier terminal requires a durable result file"
        )
    if status == "succeeded":
        read_frontier_result(
            runs_dir,
            feature=str(state["feature"]),
            run_id=str(state["run_id"]),
            recorded_path=result_file,
        )
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
    if result_file is not None:
        terminal["result_file"] = str(result_file)
    appended = append_event(ledger, terminal, runs_dir=runs_dir)
    if appended and release_lease:
        _release_frontier_lease(runs_dir, state)
    result = (
        latest_event(
            ledger,
            run_id=str(state["run_id"]),
            instance=str(state["instance"]),
            runs_dir=runs_dir,
        )
        or terminal
    )
    if result.get("hook_source") == "opencode" and result.get("surface_uuid"):
        try:
            cleanup_opencode_data_home(str(result["surface_uuid"]))
        except FrontierError:
            pass
    return result


def terminalize_response(
    runs_dir: Path,
    state: dict[str, Any],
    *,
    response: str,
    status: str,
    reason: str,
    completed_at: str,
    event: dict[str, Any],
) -> dict[str, Any]:
    result_file: Path | None = None
    if status == "succeeded":
        try:
            result_file = persist_frontier_result(runs_dir, state, response)
        except (OSError, FrontierError):
            status = "indeterminate"
            reason = "frontier_result_persistence_failed"
    return terminalize(
        runs_dir,
        state,
        status=status,
        reason=reason,
        completed_at=completed_at,
        event=event,
        release_lease=status != "indeterminate",
        result_file=result_file,
    )


def _event_after_dispatch(
    state: dict[str, Any], event: dict[str, Any], *, allow_cross_boot: bool
) -> bool:
    occurred_at = timestamp_value(event.get("occurred_at"))
    dispatched_at = timestamp_value(state.get("dispatched_at"))
    if occurred_at and dispatched_at and occurred_at < dispatched_at:
        return False
    if event.get("boot_id") == state.get("event_boot_id"):
        return isinstance(event.get("seq"), int) and event["seq"] > int(
            state["after_seq"]
        )
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
    if (
        str(event.get("workspace_id") or "").upper()
        != str(state.get("workspace_uuid") or "").upper()
    ):
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

    try:
        expected_identity = fleet_providers.identity(
            str(state.get("provider") or ""),
            str(state.get("model") or ""),
            state.get("variant"),
            expected_source,
        )
        adapter = fleet_providers.adapter_for(expected_identity)
    except fleet_providers.ProviderError:
        return terminalize(
            runs_dir,
            state,
            status="indeterminate",
            reason="frontier_provider_adapter_mismatch",
            completed_at=str(event.get("occurred_at") or utc_now()),
            event=event,
            release_lease=False,
        )
    observation = adapter.observe(event, state)

    ledger = ledger_path(runs_dir, str(state["feature"]))
    if observation == "bind":
        if state.get("tracking_protocol") == "control-v1":
            if not state.get("submission_event_id"):
                return None
            if any(
                (
                    state.get("submission_event_id") != event.get("id"),
                    state.get("submission_boot_id") != event.get("boot_id"),
                    state.get("submission_seq") != event.get("seq"),
                )
            ):
                return None
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
        append_event(ledger, binding, runs_dir=runs_dir)
        state.update(binding)
        return None

    if observation == "session_end":
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

    if observation != "stop":
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
    response = ""
    try:
        readers = {
            "codex": lambda current_session, current_run, stopped: (
                transcript_turn_evidence(
                    codex_turn_evidence, current_session, current_run, stopped
                )
            ),
            "claude": lambda current_session, current_run, stopped: (
                transcript_turn_evidence(
                    claude_turn_evidence, current_session, current_run, stopped
                )
            ),
            "kimi": lambda current_session, current_run, stopped: (
                transcript_turn_evidence(
                    kimi_turn_evidence, current_session, current_run, stopped
                )
            ),
            "opencode": lambda current_session, current_run, stopped: (
                transcript_turn_evidence(
                    lambda session, run, occurred_at: opencode_turn_evidence(
                        session,
                        run,
                        occurred_at,
                        str(state["surface_uuid"]),
                    ),
                    current_session,
                    current_run,
                    stopped,
                )
            ),
        }
        evidence = adapter.extract_final_response(
            expected_identity,
            session_id,
            str(state["run_id"]),
            str(event.get("occurred_at") or ""),
            readers,
        )
        response = evidence.response
        status, reason = structured_sentinel_status(response, str(state["run_id"]))
        try:
            adapter.verify_identity(expected_identity, evidence)
        except fleet_providers.ProviderIdentityError as exc:
            suffix = (
                "variant_mismatch" if exc.field == "variant" else "identity_mismatch"
            )
            status, reason = "indeterminate", f"frontier_{expected_source}_{suffix}"
    except (FrontierError, fleet_providers.ProviderError):
        status, reason = (
            "indeterminate",
            f"frontier_{expected_source}_evidence_unavailable",
        )
    return terminalize_response(
        runs_dir,
        state,
        response=response,
        status=status,
        reason=reason,
        completed_at=str(event.get("occurred_at") or utc_now()),
        event=event,
    )


def confirm_prompt_submission(
    *,
    workspace_uuid: str,
    hook_source: str,
    since: str,
    timeout_seconds: float = 10.0,
) -> int:
    """Fail closed unless at least one UserPromptSubmit landed after dispatch.

    Workspace-scoped: callers dispatch one frontier turn at a time per fleet,
    so any matching submission after `since` confirms the physical transfer.
    Multiple submissions stay guarded by the completion-side
    frontier_session_binding_ambiguous check.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        count = 0
        for event in audit_events():
            if event.get("name") != "agent.hook.UserPromptSubmit":
                continue
            payload = event.get("payload") or {}
            if payload.get("phase") != "received":
                continue
            if (
                event.get("source") != hook_source
                or payload.get("_source") != hook_source
            ):
                continue
            if str(event.get("workspace_id") or "").upper() != workspace_uuid.upper():
                continue
            if str(event.get("occurred_at") or "") < since:
                continue
            count += 1
        if count >= 1:
            return count
        if time.monotonic() >= deadline:
            raise FrontierError(
                "no UserPromptSubmit observed after dispatch; prompt transfer unconfirmed"
            )
        time.sleep(0.25)


def authorize_prompt_submission(
    runs_dir: Path,
    *,
    feature: str,
    instance: str,
    run_id: str,
    workspace_uuid: str,
    hook_source: str,
    since: str,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Bind exactly one physical submit to a CONTROL-prepared tracked run."""
    ledger = ledger_path(runs_dir, feature)
    state = frontier_state(
        ledger,
        run_id=run_id,
        instance=instance,
        runs_dir=runs_dir,
    )
    if not state or state.get("runner") != "interactive":
        raise FrontierError(f"unknown frontier run: {instance}={run_id}")
    if state.get("tracking_protocol") != "control-v1":
        raise FrontierError("submission authorization requires control-v1 tracking")
    if state.get("submission_event_id"):
        return state
    deadline = time.monotonic() + timeout_seconds
    while True:
        matches: list[dict[str, Any]] = []
        source_events = (
            kimi_hook_events(str(state["surface_uuid"]))
            if hook_source == "kimi"
            else audit_events()
        )
        for event in source_events:
            payload = event.get("payload") or {}
            session_id = str(payload.get("session_id") or "")
            if (
                event.get("name") == "agent.hook.UserPromptSubmit"
                and payload.get("phase") == "received"
                and event.get("source") == hook_source
                and payload.get("_source") == hook_source
                and str(event.get("workspace_id") or "").upper()
                == workspace_uuid.upper()
                and str(event.get("occurred_at") or "") >= since
                and _event_after_dispatch(state, event, allow_cross_boot=True)
                and session_id
                and session_matches(
                    session_id,
                    workspace_uuid=str(state["workspace_uuid"]),
                    surface_uuid=str(state["surface_uuid"]),
                    hook_source=hook_source,
                )
            ):
                matches.append(event)
        if len(matches) == 1:
            event = matches[0]
            session_id = str((event.get("payload") or {}).get("session_id") or "")
            authorized = {
                **_common_event(state),
                "timestamp": utc_now(),
                "status": "authorized",
                "submission_event_id": event["id"],
                "submission_boot_id": event["boot_id"],
                "submission_seq": event["seq"],
                "submission_session_id": session_id,
                "submission_authorized_at": utc_now(),
            }
            if not append_event(ledger, authorized, runs_dir=runs_dir):
                raise FrontierError(
                    "frontier run became terminal before submission authorization"
                )
            return authorized
        if len(matches) > 1:
            raise FrontierError(
                "multiple UserPromptSubmit events make transfer authorization ambiguous"
            )
        if time.monotonic() >= deadline:
            raise FrontierError(
                "no UserPromptSubmit observed after dispatch; prompt transfer unconfirmed"
            )
        time.sleep(0.25)


def reconcile_kimi_events(
    runs_dir: Path,
    state: dict[str, Any],
    *,
    workspace_ref: str,
    surface_ref: str,
) -> dict[str, Any] | None:
    """Apply durable Kimi bridge events without claiming cmux-native hooks."""
    if state.get("hook_source") != "kimi" or state.get("status") in TERMINAL_STATUSES:
        return state if state.get("status") in TERMINAL_STATUSES else None
    current = dict(state)
    events = kimi_hook_events(str(state["surface_uuid"]))
    events.sort(
        key=lambda event: (
            timestamp_value(event.get("occurred_at"))
            or datetime.max.replace(tzinfo=timezone.utc),
            int(event.get("seq") or 0),
        )
    )
    for event in events:
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
            runs_dir=runs_dir,
        )
        if refreshed:
            current = refreshed
        if terminal and terminal.get("status") in TERMINAL_STATUSES:
            return terminal
    return None


def audit_events() -> list[dict[str, Any]]:
    configured = os.environ.get("CMUX_EVENTS_LOG") or os.environ.get(
        "FLEET_CONTROLLER_EVENTS_LOG"
    )
    current = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".cmuxterm/events.jsonl"
    )
    paths = [Path(f"{current}.1"), current]
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        try:
            lines = path.read_bytes().splitlines()
        except OSError:
            continue
        for raw in lines:
            if not raw.strip():
                continue
            try:
                event = fleet_json.loads(raw)
            except fleet_json.FleetJSONError as exc:
                raise FrontierError(
                    f"cmux audit contains invalid JSON: {path}"
                ) from exc
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
            if (
                event.get("boot_id") == baseline_boot
                and event.get("seq") == baseline_seq
            ):
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
            runs_dir=runs_dir,
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
        ledger_path(runs_dir, feature),
        run_id=run_id,
        instance=instance,
        runs_dir=runs_dir,
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
        ledger_path(runs_dir, feature),
        run_id=run_id,
        instance=instance,
        runs_dir=runs_dir,
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
        "feature",
        "instance",
        "role",
        "phase",
        "task",
        "workspace-uuid",
        "surface-uuid",
    ):
        prepare.add_argument(f"--{name}", required=True)
    for name in ("provider", "model", "hook-source", "variant"):
        prepare.add_argument(f"--{name}", default="")
    prepare.add_argument("--run-id", default="")
    abandon = sub.add_parser("abandon")
    abandon.add_argument("runs_dir")
    for name in ("feature", "instance", "run-id", "reason"):
        abandon.add_argument(f"--{name}", required=True)
    indeterminate = sub.add_parser("mark-indeterminate")
    indeterminate.add_argument("runs_dir")
    for name in ("feature", "instance", "run-id", "reason"):
        indeterminate.add_argument(f"--{name}", required=True)
    confirm = sub.add_parser("confirm-submit")
    confirm.add_argument("runs_dir")
    for name in ("workspace-uuid", "hook-source", "since"):
        confirm.add_argument(f"--{name}", required=True)
    confirm.add_argument("--timeout", type=float, default=10.0)
    for name in ("feature", "instance", "run-id"):
        confirm.add_argument(f"--{name}")
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
                variant=args.variant,
                run_id=args.run_id,
            )
        elif args.command == "abandon":
            result = abandon_run(
                runs_dir,
                feature=args.feature,
                instance=args.instance,
                run_id=args.run_id,
                reason=args.reason,
            )
        elif args.command == "confirm-submit":
            supplied = [args.feature, args.instance, args.run_id]
            if any(supplied) and not all(supplied):
                raise FrontierError(
                    "--feature, --instance, and --run-id must be supplied together"
                )
            if all(supplied):
                result = authorize_prompt_submission(
                    runs_dir,
                    feature=args.feature,
                    instance=args.instance,
                    run_id=args.run_id,
                    workspace_uuid=args.workspace_uuid,
                    hook_source=args.hook_source,
                    since=args.since,
                    timeout_seconds=args.timeout,
                )
            else:
                result = {
                    "confirmed_submissions": confirm_prompt_submission(
                        workspace_uuid=args.workspace_uuid,
                        hook_source=args.hook_source,
                        since=args.since,
                        timeout_seconds=args.timeout,
                    )
                }
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
