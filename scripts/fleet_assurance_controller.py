#!/usr/bin/env python3
"""Fail-closed FDP-3 assurance: GLM CHALLENGE followed by Claude VERIFY."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any
import uuid

import fleet_dialogue
import fleet_dialogue_controller as fdp2
from fleet_leases import closing_path, coordinator
from fleet_ledger import append_record, events_for_run


SCHEMA_VERSION = 1
FDP3_PRESET = "fleet_dialogue"
CHALLENGE_INSTANCE = "challenge"
VERIFY_INSTANCE = "verify"
RUN_TIMEOUT_SECONDS = 30 * 60
ASSURANCE_DEADLINE_SECONDS = 2 * 60 * 60
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
TERMINAL_STATES = {
    "verified",
    "rejected",
    "failed",
    "blocked",
    "abandoned",
    "indeterminate",
}
ACTIVE_STATES = {
    "awaiting_challenge_run",
    "awaiting_challenge_message",
    "awaiting_phase_advance",
    "awaiting_verification_run",
    "awaiting_verification_message",
}
LIFECYCLE_TERMINALS = {
    "succeeded",
    "failed",
    "blocked",
    "abandoned",
    "indeterminate",
}
EVENT_FIELDS = {
    "schema_version",
    "sequence",
    "timestamp",
    "event_id",
    "feature",
    "assurance_id",
    "event_type",
    "idempotency_key",
    "request_sha256",
    "previous_sha256",
    "snapshot",
    "event_sha256",
}
SNAPSHOT_FIELDS = {
    "status",
    "created_at",
    "deadline_at",
    "run_timeout_seconds",
    "challenge_instance",
    "verify_instance",
    "accepted_head_sha",
    "fdp2_conversation_id",
    "fdp2_control_head_sha256",
    "context_file",
    "context_sha256",
    "snapshots_file",
    "snapshots_sha256",
    "challenge_snapshot_path",
    "verification_snapshot_path",
    "context_run_ids",
    "context_message_ids",
    "maker_verification_ids",
    "challenge_message_id",
    "verification_message_id",
    "challenge_finding_ids",
    "expected",
    "terminal_reason",
}
RUN_EXPECTED_FIELDS = {
    "type",
    "stage",
    "instance",
    "phase",
    "prompt_file",
    "prompt_sha256",
    "reply_to",
}
MESSAGE_EXPECTED_FIELDS = {
    "type",
    "stage",
    "kind",
    "recipient",
    "source_instance",
    "source_run_id",
    "reply_to",
    "payload_sha256",
}
PHASE_EXPECTED_FIELDS = {"type", "stage", "phase"}


class AssuranceError(RuntimeError):
    pass


class AssuranceConflict(AssuranceError):
    pass


class AssuranceContractError(AssuranceError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_timestamp(value: Any, where: str) -> datetime:
    try:
        return fdp2.parse_timestamp(value, where)
    except fdp2.ControllerError as exc:
        raise AssuranceError(str(exc)) from exc


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    payload = value if isinstance(value, bytes) else canonical(value)
    return hashlib.sha256(payload).hexdigest()


def ledger_path(runs_dir: Path, feature: str) -> Path:
    return runs_dir / f"fleet-{feature}.assurance-control.jsonl"


def assurance_root(runs_dir: Path, feature: str, assurance_id: str) -> Path:
    return runs_dir / "assurance" / feature / assurance_id


def _uuid(value: Any, where: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise AssuranceError(f"invalid {where}") from exc


def _nonempty(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AssuranceContractError(f"{where} must be a non-empty string")
    return value


def _strict_object(value: Any, fields: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise AssuranceContractError(f"{where} fields do not match schema_version=1")
    return value


def _validate_idempotency_key(value: str) -> None:
    if not fleet_dialogue.SAFE_IDEMPOTENCY_KEY.fullmatch(value):
        raise AssuranceError("invalid idempotency key")


def _validate_expected(value: Any) -> None:
    if not isinstance(value, dict):
        raise AssuranceError("active assurance snapshot lacks expected action")
    expected_type = value.get("type")
    fields = {
        "run": RUN_EXPECTED_FIELDS,
        "message": MESSAGE_EXPECTED_FIELDS,
        "phase_advance": PHASE_EXPECTED_FIELDS,
    }.get(expected_type)
    if fields is None or set(value) != fields:
        raise AssuranceError("assurance expected action has invalid fields")
    if not isinstance(value.get("stage"), str) or not value["stage"]:
        raise AssuranceError("assurance expected stage is invalid")
    if expected_type == "run":
        instance = value.get("instance")
        phase = value.get("phase")
        if (instance, phase) not in {
            (CHALLENGE_INSTANCE, "CHALLENGE"),
            (VERIFY_INSTANCE, "VERIFY"),
        }:
            raise AssuranceError("assurance expected run identity is invalid")
        if not isinstance(value.get("prompt_file"), str) or not value["prompt_file"]:
            raise AssuranceError("assurance expected prompt file is invalid")
        if not SHA256.fullmatch(str(value.get("prompt_sha256") or "")):
            raise AssuranceError("assurance expected prompt hash is invalid")
    elif expected_type == "message":
        if value.get("kind") not in {"challenge", "verification"}:
            raise AssuranceError("assurance expected message kind is invalid")
        if not fleet_dialogue.SAFE_RUN_ID.fullmatch(str(value.get("source_run_id") or "")):
            raise AssuranceError("assurance expected source run is invalid")
        if not SHA256.fullmatch(str(value.get("payload_sha256") or "")):
            raise AssuranceError("assurance expected payload hash is invalid")
    elif value.get("phase") != "VERIFY":
        raise AssuranceError("assurance phase advance must target VERIFY")


def _validate_string_list(value: Any, where: str) -> None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise AssuranceError(f"{where} must be a string list")
    if len(value) != len(set(value)):
        raise AssuranceError(f"{where} contains duplicates")


def _validate_snapshot(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != SNAPSHOT_FIELDS:
        raise AssuranceError("assurance snapshot fields do not match schema_version=1")
    if value.get("status") not in ACTIVE_STATES | TERMINAL_STATES:
        raise AssuranceError("assurance snapshot status is invalid")
    created = parse_timestamp(value.get("created_at"), "snapshot.created_at")
    deadline = parse_timestamp(value.get("deadline_at"), "snapshot.deadline_at")
    if deadline <= created:
        raise AssuranceError("assurance deadline must follow creation")
    if value.get("run_timeout_seconds") != RUN_TIMEOUT_SECONDS:
        raise AssuranceError("assurance run timeout is unsupported")
    if value.get("challenge_instance") != CHALLENGE_INSTANCE:
        raise AssuranceError("assurance challenge identity is invalid")
    if value.get("verify_instance") != VERIFY_INSTANCE:
        raise AssuranceError("assurance verify identity is invalid")
    if not GIT_SHA.fullmatch(str(value.get("accepted_head_sha") or "")):
        raise AssuranceError("assurance accepted head is invalid")
    _uuid(value.get("fdp2_conversation_id"), "fdp2_conversation_id")
    for field in ("fdp2_control_head_sha256", "context_sha256", "snapshots_sha256"):
        if not SHA256.fullmatch(str(value.get(field) or "")):
            raise AssuranceError(f"assurance {field} is invalid")
    for field in (
        "context_file",
        "snapshots_file",
        "challenge_snapshot_path",
        "verification_snapshot_path",
    ):
        if not isinstance(value.get(field), str) or not value[field]:
            raise AssuranceError(f"assurance {field} is invalid")
    for field in (
        "context_run_ids",
        "context_message_ids",
        "maker_verification_ids",
        "challenge_finding_ids",
    ):
        _validate_string_list(value.get(field), f"snapshot.{field}")
    for field in ("challenge_message_id", "verification_message_id"):
        item = value.get(field)
        if item is not None:
            _uuid(item, field)
    if value["status"] in TERMINAL_STATES:
        if value.get("expected") is not None:
            raise AssuranceError("terminal assurance cannot expect another action")
        if not isinstance(value.get("terminal_reason"), str) or not value["terminal_reason"]:
            raise AssuranceError("terminal assurance lacks a reason")
    else:
        _validate_expected(value.get("expected"))
        if value.get("terminal_reason") is not None:
            raise AssuranceError("active assurance contains a terminal reason")


def load_events_from_path(path: Path, feature: str | None = None) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise AssuranceError(f"cannot read assurance control ledger: {path}") from exc
    events: list[dict[str, Any]] = []
    previous_sha: str | None = None
    event_ids: set[str] = set()
    idempotency_keys: set[str] = set()
    for line_number, raw in enumerate(lines, start=1):
        if not raw.strip():
            raise AssuranceError(f"blank assurance control row at line {line_number}")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AssuranceError(f"invalid assurance control JSON at line {line_number}") from exc
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise AssuranceError("assurance control event fields do not match schema_version=1")
        if event.get("schema_version") != SCHEMA_VERSION or event.get("sequence") != line_number:
            raise AssuranceError("assurance control sequence/schema is invalid")
        _uuid(event.get("event_id"), "event_id")
        _uuid(event.get("assurance_id"), "assurance_id")
        if feature is not None and event.get("feature") != feature:
            raise AssuranceError("assurance event belongs to another feature")
        if not isinstance(event.get("feature"), str) or not fleet_dialogue.SAFE_FEATURE.fullmatch(event["feature"]):
            raise AssuranceError("assurance feature is invalid")
        parse_timestamp(event.get("timestamp"), "event.timestamp")
        _validate_idempotency_key(str(event.get("idempotency_key") or ""))
        if not SHA256.fullmatch(str(event.get("request_sha256") or "")):
            raise AssuranceError("assurance request hash is invalid")
        if event.get("previous_sha256") != previous_sha:
            raise AssuranceError("assurance control hash chain is broken")
        stored_sha = event.get("event_sha256")
        unsigned = {key: item for key, item in event.items() if key != "event_sha256"}
        if stored_sha != digest(unsigned):
            raise AssuranceError("assurance control event hash is invalid")
        if event["event_id"] in event_ids or event["idempotency_key"] in idempotency_keys:
            raise AssuranceError("assurance ledger contains duplicate identity")
        _validate_snapshot(event.get("snapshot"))
        event_ids.add(event["event_id"])
        idempotency_keys.add(event["idempotency_key"])
        previous_sha = str(stored_sha)
        events.append(event)
    return events


def load_events(runs_dir: Path, feature: str) -> list[dict[str, Any]]:
    return load_events_from_path(ledger_path(runs_dir, feature), feature)


def _request_hash(request: dict[str, Any]) -> str:
    return digest(request)


def _idempotent_event(
    events: list[dict[str, Any]], idempotency_key: str, request: dict[str, Any]
) -> dict[str, Any] | None:
    wanted = _request_hash(request)
    existing = next(
        (event for event in events if event["idempotency_key"] == idempotency_key),
        None,
    )
    if existing is None:
        return None
    if existing["request_sha256"] != wanted:
        raise AssuranceConflict("idempotency key was already used for another assurance request")
    return existing


def _append_event_locked(
    runs_dir: Path,
    feature: str,
    events: list[dict[str, Any]],
    *,
    assurance_id: str,
    event_type: str,
    idempotency_key: str,
    request: dict[str, Any],
    snapshot: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    _validate_idempotency_key(idempotency_key)
    existing = _idempotent_event(events, idempotency_key, request)
    if existing is not None:
        return existing
    _validate_snapshot(snapshot)
    event = {
        "schema_version": SCHEMA_VERSION,
        "sequence": len(events) + 1,
        "timestamp": timestamp(now or utc_now()),
        "event_id": str(uuid.uuid4()),
        "feature": feature,
        "assurance_id": assurance_id,
        "event_type": event_type,
        "idempotency_key": idempotency_key,
        "request_sha256": _request_hash(request),
        "previous_sha256": events[-1]["event_sha256"] if events else None,
        "snapshot": snapshot,
    }
    event["event_sha256"] = digest(event)
    appended = append_record(
        ledger_path(runs_dir, feature),
        event,
        reject_if=lambda previous: (
            previous.get("sequence") == event["sequence"]
            or previous.get("event_id") == event["event_id"]
            or previous.get("idempotency_key") == idempotency_key
        ),
    )
    if not appended:
        raise AssuranceConflict("assurance control ledger changed during append")
    events.append(event)
    return event


def _terminal(snapshot: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    value = copy.deepcopy(snapshot)
    value["status"] = status
    value["expected"] = None
    value["terminal_reason"] = reason
    return value


def _expire_locked(
    runs_dir: Path,
    feature: str,
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if not events or events[-1]["snapshot"]["status"] in TERMINAL_STATES:
        return events[-1] if events else None
    current_time = now or utc_now()
    latest = events[-1]
    if current_time < parse_timestamp(latest["snapshot"]["deadline_at"], "deadline"):
        return latest
    assurance_id = latest["assurance_id"]
    request = {"command": "system_deadline", "assurance_id": assurance_id}
    return _append_event_locked(
        runs_dir,
        feature,
        events,
        assurance_id=assurance_id,
        event_type="deadline_expired",
        idempotency_key=f"system:deadline:{assurance_id}",
        request=request,
        snapshot=_terminal(latest["snapshot"], "indeterminate", "assurance_deadline_exceeded"),
        now=current_time,
    )


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise AssuranceConflict(f"durable FDP-3 artifact changed: {path}")
        return
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_json_immutable(path: Path, value: Any) -> str:
    payload = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _write_immutable(path, payload)
    return hashlib.sha256(payload).hexdigest()


def _git(root: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssuranceContractError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result


def _validate_snapshot_worktree(path: Path, head_sha: str) -> None:
    if not path.is_dir():
        raise AssuranceContractError(f"assurance snapshot is missing: {path}")
    if _git(path, "rev-parse", "--verify", "HEAD").stdout.strip() != head_sha:
        raise AssuranceContractError(f"assurance snapshot HEAD drifted: {path}")
    attached = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if attached.returncode == 0:
        raise AssuranceContractError(f"assurance snapshot is not detached: {path}")
    if _git(path, "status", "--porcelain").stdout.strip():
        raise AssuranceContractError(f"assurance snapshot is dirty: {path}")


def _ensure_snapshot(target_repo: str, path: Path, head_sha: str) -> None:
    if path.exists():
        _validate_snapshot_worktree(path, head_sha)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = _git(
        target_repo,
        "worktree",
        "add",
        "--detach",
        str(path),
        head_sha,
        check=False,
    )
    if result.returncode != 0:
        raise AssuranceContractError(
            f"cannot create detached assurance snapshot: {result.stderr.strip()}"
        )
    _validate_snapshot_worktree(path, head_sha)


def _template_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "orchestration" / "prompts" / name


def _render_template(name: str, replacements: dict[str, str]) -> str:
    try:
        text = _template_path(name).read_text(encoding="utf-8")
    except OSError as exc:
        raise AssuranceError(f"missing FDP-3 prompt template: {name}") from exc
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value)
    if re.search(r"\{\{[A-Z0-9_]+\}\}", text):
        raise AssuranceError(f"unresolved placeholder in FDP-3 prompt: {name}")
    return text.rstrip()


def _write_prompt(
    runs_dir: Path,
    feature: str,
    assurance_id: str,
    filename: str,
    content: str,
) -> tuple[str, str]:
    path = assurance_root(runs_dir, feature, assurance_id) / "prompts" / filename
    payload = content.encode("utf-8")
    _write_immutable(path, payload)
    return str(path), hashlib.sha256(payload).hexdigest()


def _run_expected(
    stage: str,
    instance: str,
    phase: str,
    prompt_file: str,
    prompt_sha256: str,
    reply_to: str | None,
) -> dict[str, Any]:
    return {
        "type": "run",
        "stage": stage,
        "instance": instance,
        "phase": phase,
        "prompt_file": prompt_file,
        "prompt_sha256": prompt_sha256,
        "reply_to": reply_to,
    }


def _message_expected(
    stage: str,
    kind: str,
    recipient: str,
    source_instance: str,
    source_run_id: str,
    reply_to: str | None,
    payload_sha256: str,
) -> dict[str, Any]:
    return {
        "type": "message",
        "stage": stage,
        "kind": kind,
        "recipient": recipient,
        "source_instance": source_instance,
        "source_run_id": source_run_id,
        "reply_to": reply_to,
        "payload_sha256": payload_sha256,
    }


def _phase_expected() -> dict[str, str]:
    return {"type": "phase_advance", "stage": "challenge_complete", "phase": "VERIFY"}


def _load_manifest(runs_dir: Path, feature: str, *, live_identity: bool) -> dict[str, str]:
    try:
        manifest = (
            fdp2._load_live_manifest(runs_dir, feature)
            if live_identity
            else fdp2._manifest_values(runs_dir / f"fleet-{feature}.manifest")
        )
        fdp2._validate_roster(manifest)
    except (fdp2.ControllerError, fdp2.ContractError) as exc:
        raise AssuranceError(str(exc).replace("FDP-2", "FDP-3")) from exc
    return manifest


def _state(runs_dir: Path, feature: str) -> dict[str, Any]:
    path = runs_dir / f"fleet-{feature}.state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssuranceError(f"cannot read fleet state: {path}") from exc
    if not isinstance(value, dict) or value.get("feature") != feature:
        raise AssuranceError("fleet state identity is invalid")
    if not isinstance(value.get("history"), list):
        raise AssuranceError("fleet state history is invalid")
    return value


def _require_challenge_start_state(runs_dir: Path, feature: str) -> dict[str, Any]:
    value = _state(runs_dir, feature)
    if value.get("active_phase") != "CHALLENGE":
        raise AssuranceError(
            f"FDP-3 start requires active_phase=CHALLENGE, got {value.get('active_phase')}"
        )
    challenge_entries = [
        entry
        for entry in value["history"]
        if isinstance(entry, dict) and entry.get("phase") == "CHALLENGE"
    ]
    if not challenge_entries or not isinstance(challenge_entries[-1].get("approved_by"), str) or not challenge_entries[-1]["approved_by"].strip():
        raise AssuranceError("FDP-3 start requires the recorded human BUILD approval")
    return value


def _require_verify_state(
    runs_dir: Path,
    feature: str,
    *,
    control_head_sha256: str,
) -> dict[str, Any]:
    value = _state(runs_dir, feature)
    if value.get("active_phase") != "VERIFY":
        raise AssuranceError(
            f"FDP-3 phase acknowledgement requires active_phase=VERIFY, got {value.get('active_phase')}"
        )
    verify_entries = [
        entry
        for entry in value["history"]
        if isinstance(entry, dict) and entry.get("phase") == "VERIFY"
    ]
    if not verify_entries or verify_entries[-1].get("evidence") != control_head_sha256:
        raise AssuranceError("VERIFY transition is not bound to the exact FDP-3 control head")
    return value


def _accepted_fdp2_context_locked(
    runs_dir: Path,
    feature: str,
    manifest: dict[str, str],
) -> dict[str, Any]:
    try:
        events = fdp2.load_events(runs_dir, feature)
        if not events:
            raise AssuranceError("FDP-3 requires a completed FDP-2 conversation")
        latest = events[-1]
        snapshot = latest["snapshot"]
        if snapshot["status"] != "accepted":
            raise AssuranceError(
                f"FDP-3 requires latest FDP-2 status accepted, got {snapshot['status']}"
            )
        messages = fleet_dialogue.load_messages(runs_dir, feature)
        fleet_dialogue.verify_storage(
            fleet_dialogue.ledger_path(runs_dir, feature),
            fleet_dialogue.store_path(runs_dir, feature),
            feature=feature,
            instances=fdp2._manifest_instances(manifest),
        )
        fdp2._verify_live_control_artifacts(runs_dir, feature, events)
        fdp2._verify_message_bindings(events, messages)
        head = fdp2._clean_writer_head(manifest)
    except (fdp2.ControllerError, fdp2.ContractError, fleet_dialogue.DialogueError) as exc:
        raise AssuranceError(str(exc)) from exc
    if head != snapshot["accepted_head_sha"]:
        raise AssuranceError("maker branch drifted after FDP-2 acceptance")
    conversation_id = latest["conversation_id"]
    conversation_events = [
        event for event in events if event["conversation_id"] == conversation_id
    ]
    message_ids = [
        event["snapshot"]["last_message_id"]
        for event in conversation_events
        if event["snapshot"].get("last_message_id") is not None
    ]
    message_ids = list(dict.fromkeys(message_ids))
    by_id = {message["message_id"]: message for message in messages}
    if not message_ids or snapshot.get("last_message_id") != message_ids[-1]:
        raise AssuranceError("accepted FDP-2 conversation lacks its final Checker message")
    if any(message_id not in by_id for message_id in message_ids):
        raise AssuranceError("accepted FDP-2 message binding is incomplete")
    final_message = by_id[message_ids[-1]]
    if final_message.get("source_instance") != fdp2.CHECKER_INSTANCE:
        raise AssuranceError("accepted FDP-2 terminal is not bound to the Checker")
    run_ids = [
        event["snapshot"]["expected"]["source_run_id"]
        for event in conversation_events
        if isinstance(event["snapshot"].get("expected"), dict)
        and event["snapshot"]["expected"].get("type") == "message"
    ]
    run_ids = list(dict.fromkeys(run_ids))
    return {
        "events": events,
        "conversation_events": conversation_events,
        "latest": latest,
        "snapshot": snapshot,
        "messages": [by_id[message_id] for message_id in message_ids],
        "message_ids": message_ids,
        "run_ids": run_ids,
        "reply_to": message_ids[-1],
        "accepted_head_sha": head,
    }


def _file_record(root: Path, path: Path) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    payload = path.read_bytes()
    return {
        "path": relative,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _write_context(
    runs_dir: Path,
    feature: str,
    assurance_id: str,
    accepted: dict[str, Any],
    snapshots_file: Path,
) -> tuple[str, str]:
    root = assurance_root(runs_dir, feature, assurance_id)
    context_dir = root / "context"
    source_task = Path(accepted["snapshot"]["task_spec_file"])
    try:
        task_payload = source_task.read_bytes()
    except OSError as exc:
        raise AssuranceError("cannot copy the accepted FDP-2 task spec") from exc
    if hashlib.sha256(task_payload).hexdigest() != accepted["snapshot"]["task_spec_sha256"]:
        raise AssuranceError("FDP-2 task spec changed before FDP-3 context copy")
    task_copy = context_dir / "fdp2-task-spec.json"
    _write_immutable(task_copy, task_payload)

    message_entries: list[dict[str, Any]] = []
    copied_paths: list[Path] = [task_copy, snapshots_file]
    for message in accepted["messages"]:
        try:
            _, payload = fleet_dialogue._verified_payload(runs_dir, feature, message)
        except fleet_dialogue.DialogueError as exc:
            raise AssuranceError(str(exc)) from exc
        payload_path = context_dir / "messages" / f"{message['message_id']}.result"
        _write_immutable(payload_path, payload)
        copied_paths.append(payload_path)
        message_entries.append(
            {
                "envelope": message,
                "payload_path": payload_path.relative_to(root).as_posix(),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "payload_bytes": len(payload),
            }
        )

    lifecycle_path = runs_dir / f"fleet-{feature}.ledger.jsonl"
    lifecycle_rows: list[dict[str, Any]] = []
    if lifecycle_path.exists():
        try:
            lifecycle_lines = lifecycle_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AssuranceError("cannot copy FDP-2 lifecycle evidence") from exc
        for raw in lifecycle_lines:
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AssuranceError("FDP-2 lifecycle ledger is invalid") from exc
            if isinstance(row, dict) and row.get("run_id") in accepted["run_ids"]:
                lifecycle_rows.append(row)
    if accepted["run_ids"] and {
        row.get("run_id") for row in lifecycle_rows
    } != set(accepted["run_ids"]):
        raise AssuranceError("FDP-2 context lacks lifecycle evidence for a bound run")
    lifecycle_copy = context_dir / "fdp2-lifecycle.jsonl"
    lifecycle_payload = b"".join(canonical(row) + b"\n" for row in lifecycle_rows)
    _write_immutable(lifecycle_copy, lifecycle_payload)
    copied_paths.append(lifecycle_copy)

    context = {
        "schema_version": 1,
        "feature": feature,
        "assurance_id": assurance_id,
        "accepted_head_sha": accepted["accepted_head_sha"],
        "fdp2": {
            "conversation_id": accepted["latest"]["conversation_id"],
            "control_head_sha256": accepted["latest"]["event_sha256"],
            "task_spec_sha256": accepted["snapshot"]["task_spec_sha256"],
            "task_spec_path": task_copy.relative_to(root).as_posix(),
            "message_ids": accepted["message_ids"],
            "run_ids": accepted["run_ids"],
            "maker_verification_ids": accepted["snapshot"]["maker_verification_ids"],
        },
        "messages": message_entries,
        "files": [_file_record(root, path) for path in sorted(copied_paths)],
    }
    context_file = context_dir / "fdp2-context.json"
    context_sha = _write_json_immutable(context_file, context)
    return str(context_file), context_sha


def _snapshot_records(
    *,
    target_repo: str,
    accepted_head_sha: str,
    challenge_path: Path,
    verify_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "accepted_head_sha": accepted_head_sha,
        "snapshots": [
            {
                "instance": CHALLENGE_INSTANCE,
                "phase": "CHALLENGE",
                "path": str(challenge_path),
                "target_repo": target_repo,
                "head_sha": accepted_head_sha,
                "detached": True,
            },
            {
                "instance": VERIFY_INSTANCE,
                "phase": "VERIFY",
                "path": str(verify_path),
                "target_repo": target_repo,
                "head_sha": accepted_head_sha,
                "detached": True,
            },
        ],
    }


def _evidence_list(value: Any, where: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise AssuranceContractError(f"{where} must be a non-empty evidence list")
    result: list[dict[str, str]] = []
    for index, raw in enumerate(value):
        item = _strict_object(raw, {"kind", "ref"}, f"{where}[{index}]")
        if item.get("kind") not in {"file", "test", "run", "message"}:
            raise AssuranceContractError(f"{where}[{index}].kind is invalid")
        reference = _nonempty(item.get("ref"), f"{where}[{index}].ref")
        result.append({"kind": item["kind"], "ref": reference})
    return result


def _finding_list(value: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AssuranceContractError(f"{where} must be a list")
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw in enumerate(value):
        item = _strict_object(
            raw,
            {"finding_id", "severity", "description", "evidence"},
            f"{where}[{index}]",
        )
        finding_id = _nonempty(item.get("finding_id"), f"{where}[{index}].finding_id")
        if finding_id in ids:
            raise AssuranceContractError(f"{where} contains duplicate finding_id")
        ids.add(finding_id)
        if item.get("severity") not in {"low", "medium", "high", "critical"}:
            raise AssuranceContractError(f"{where}[{index}].severity is invalid")
        _nonempty(item.get("description"), f"{where}[{index}].description")
        _evidence_list(item.get("evidence"), f"{where}[{index}].evidence")
        result.append(item)
    return result


def _challenge_contract(value: Any) -> dict[str, Any]:
    item = _strict_object(
        value,
        {"schema_version", "summary", "findings"},
        "challenge",
    )
    if item.get("schema_version") != 1:
        raise AssuranceContractError("challenge schema_version must be 1")
    _nonempty(item.get("summary"), "challenge.summary")
    _finding_list(item.get("findings"), "challenge.findings")
    return item


def _verification_contract(
    value: Any,
    challenge_finding_ids: list[str],
) -> dict[str, Any]:
    item = _strict_object(
        value,
        {"schema_version", "verdict", "summary", "adjudications", "new_findings"},
        "verification",
    )
    if item.get("schema_version") != 1:
        raise AssuranceContractError("verification schema_version must be 1")
    if item.get("verdict") not in {"VERIFIED", "REJECTED"}:
        raise AssuranceContractError("verification verdict is invalid")
    _nonempty(item.get("summary"), "verification.summary")
    adjudications = item.get("adjudications")
    if not isinstance(adjudications, list):
        raise AssuranceContractError("verification.adjudications must be a list")
    seen: set[str] = set()
    sustained = False
    for index, raw in enumerate(adjudications):
        entry = _strict_object(
            raw,
            {"finding_id", "disposition", "reason", "evidence"},
            f"verification.adjudications[{index}]",
        )
        finding_id = _nonempty(
            entry.get("finding_id"),
            f"verification.adjudications[{index}].finding_id",
        )
        if finding_id in seen or finding_id not in challenge_finding_ids:
            raise AssuranceContractError("verification has duplicate or unknown adjudication")
        seen.add(finding_id)
        if entry.get("disposition") not in {"dismissed", "sustained"}:
            raise AssuranceContractError("verification adjudication disposition is invalid")
        sustained = sustained or entry["disposition"] == "sustained"
        _nonempty(entry.get("reason"), f"verification.adjudications[{index}].reason")
        _evidence_list(entry.get("evidence"), f"verification.adjudications[{index}].evidence")
    if seen != set(challenge_finding_ids):
        raise AssuranceContractError("verification must adjudicate every GLM finding exactly once")
    new_findings = _finding_list(item.get("new_findings"), "verification.new_findings")
    if set(challenge_finding_ids) & {finding["finding_id"] for finding in new_findings}:
        raise AssuranceContractError("new finding IDs must not collide with GLM findings")
    rejected_condition = sustained or bool(new_findings)
    if item["verdict"] == "VERIFIED" and rejected_condition:
        raise AssuranceContractError("VERIFIED requires all GLM findings dismissed and no new findings")
    if item["verdict"] == "REJECTED" and not rejected_condition:
        raise AssuranceContractError("REJECTED requires a sustained or new finding")
    return item


def _contract_evidence(value: dict[str, Any]) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    for finding in value.get("findings", []):
        evidence.extend(finding["evidence"])
    for adjudication in value.get("adjudications", []):
        evidence.extend(adjudication["evidence"])
    for finding in value.get("new_findings", []):
        evidence.extend(finding["evidence"])
    return evidence


def _parse_result(payload: bytes, run_id: str) -> dict[str, Any]:
    try:
        return fdp2._parse_result(payload, run_id)
    except fdp2.ContractError as exc:
        raise AssuranceContractError(str(exc).replace("FDP-2", "FDP-3")) from exc


def _validate_evidence(
    value: dict[str, Any],
    *,
    snapshot: dict[str, Any],
    manifest: dict[str, str],
    run_id: str,
    message_ids: set[str],
) -> None:
    try:
        fdp2._validate_evidence_refs(
            _contract_evidence(value),
            manifest=manifest,
            head_sha=snapshot["accepted_head_sha"],
            run_ids=set(snapshot["context_run_ids"]) | {run_id},
            message_ids=set(snapshot["context_message_ids"]) | message_ids,
            verification_ids=set(snapshot["maker_verification_ids"]),
        )
    except fdp2.ContractError as exc:
        raise AssuranceContractError(str(exc)) from exc


def _challenge_prompt(
    runs_dir: Path,
    feature: str,
    assurance_id: str,
    *,
    snapshot_path: str,
    accepted_head_sha: str,
    context_file: str,
    context_sha256: str,
    reply_to: str,
) -> tuple[str, str]:
    content = _render_template(
        "fdp3_glm_challenge.md",
        {
            "FEATURE": feature,
            "ASSURANCE_ID": assurance_id,
            "SNAPSHOT": snapshot_path,
            "ACCEPTED_HEAD_SHA": accepted_head_sha,
            "CONTEXT_FILE": context_file,
            "CONTEXT_SHA256": context_sha256,
            "REPLY_TO": reply_to,
        },
    )
    return _write_prompt(runs_dir, feature, assurance_id, "challenge.txt", content)


def _verification_prompt(
    runs_dir: Path,
    feature: str,
    assurance_id: str,
    *,
    snapshot_path: str,
    accepted_head_sha: str,
    context_file: str,
    context_sha256: str,
    challenge_message_id: str,
    challenge_payload: bytes,
) -> tuple[str, str]:
    content = _render_template(
        "fdp3_claude_verify.md",
        {
            "FEATURE": feature,
            "ASSURANCE_ID": assurance_id,
            "SNAPSHOT": snapshot_path,
            "ACCEPTED_HEAD_SHA": accepted_head_sha,
            "CONTEXT_FILE": context_file,
            "CONTEXT_SHA256": context_sha256,
            "CHALLENGE_MESSAGE_ID": challenge_message_id,
            "CHALLENGE_JSON": challenge_payload.decode("utf-8"),
        },
    )
    return _write_prompt(runs_dir, feature, assurance_id, "verification.txt", content)


def start(
    runs_dir: Path,
    *,
    feature: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    request = {"command": "start", "feature": feature}
    current_time = now or utc_now()
    with coordinator(runs_dir):
        events = load_events(runs_dir, feature)
        _expire_locked(runs_dir, feature, events, now=current_time)
        existing = _idempotent_event(events, idempotency_key, request)
        if existing is not None:
            return existing
        if events:
            raise AssuranceConflict("feature already has an FDP-3 assurance ledger")
        manifest = _load_manifest(runs_dir, feature, live_identity=True)
        _require_challenge_start_state(runs_dir, feature)
        if closing_path(runs_dir, feature).exists():
            raise AssuranceConflict(f"fleet '{feature}' is closing")
        for instance in (CHALLENGE_INSTANCE, VERIFY_INSTANCE):
            if (runs_dir / "locks" / f"{feature}.{instance}.lock").exists():
                raise AssuranceConflict(f"FDP-3 participant is busy: {instance}")
        accepted = _accepted_fdp2_context_locked(runs_dir, feature, manifest)
        assurance_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"fdp3:{feature}:{accepted['latest']['event_sha256']}",
            )
        )
        short_id = assurance_id.split("-", 1)[0]
        challenge_path = runs_dir / "worktrees" / f"{feature}-fdp3-{short_id}-challenge"
        verify_path = runs_dir / "worktrees" / f"{feature}-fdp3-{short_id}-verify"
        _ensure_snapshot(manifest["target_repo"], challenge_path, accepted["accepted_head_sha"])
        _ensure_snapshot(manifest["target_repo"], verify_path, accepted["accepted_head_sha"])
        snapshots = _snapshot_records(
            target_repo=manifest["target_repo"],
            accepted_head_sha=accepted["accepted_head_sha"],
            challenge_path=challenge_path,
            verify_path=verify_path,
        )
        snapshots_file = assurance_root(runs_dir, feature, assurance_id) / "snapshots.json"
        snapshots_sha = _write_json_immutable(snapshots_file, snapshots)
        context_file, context_sha = _write_context(
            runs_dir,
            feature,
            assurance_id,
            accepted,
            snapshots_file,
        )
        prompt_file, prompt_sha = _challenge_prompt(
            runs_dir,
            feature,
            assurance_id,
            snapshot_path=str(challenge_path),
            accepted_head_sha=accepted["accepted_head_sha"],
            context_file=context_file,
            context_sha256=context_sha,
            reply_to=accepted["reply_to"],
        )
        snapshot = {
            "status": "awaiting_challenge_run",
            "created_at": timestamp(current_time),
            "deadline_at": timestamp(
                current_time + timedelta(seconds=ASSURANCE_DEADLINE_SECONDS)
            ),
            "run_timeout_seconds": RUN_TIMEOUT_SECONDS,
            "challenge_instance": CHALLENGE_INSTANCE,
            "verify_instance": VERIFY_INSTANCE,
            "accepted_head_sha": accepted["accepted_head_sha"],
            "fdp2_conversation_id": accepted["latest"]["conversation_id"],
            "fdp2_control_head_sha256": accepted["latest"]["event_sha256"],
            "context_file": context_file,
            "context_sha256": context_sha,
            "snapshots_file": str(snapshots_file),
            "snapshots_sha256": snapshots_sha,
            "challenge_snapshot_path": str(challenge_path),
            "verification_snapshot_path": str(verify_path),
            "context_run_ids": accepted["run_ids"],
            "context_message_ids": accepted["message_ids"],
            "maker_verification_ids": accepted["snapshot"]["maker_verification_ids"],
            "challenge_message_id": None,
            "verification_message_id": None,
            "challenge_finding_ids": [],
            "expected": _run_expected(
                "challenge",
                CHALLENGE_INSTANCE,
                "CHALLENGE",
                prompt_file,
                prompt_sha,
                accepted["reply_to"],
            ),
            "terminal_reason": None,
        }
        return _append_event_locked(
            runs_dir,
            feature,
            events,
            assurance_id=assurance_id,
            event_type="assurance_started",
            idempotency_key=idempotency_key,
            request=request,
            snapshot=snapshot,
            now=current_time,
        )


def _bound_ids(events: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    run_ids: set[str] = set()
    message_ids: set[str] = set()
    for event in events:
        expected = event["snapshot"].get("expected")
        if isinstance(expected, dict) and expected.get("type") == "message":
            run_ids.add(expected["source_run_id"])
        for field in ("challenge_message_id", "verification_message_id"):
            value = event["snapshot"].get(field)
            if isinstance(value, str):
                message_ids.add(value)
    return run_ids, message_ids


def _run_terminal(
    runs_dir: Path,
    feature: str,
    run_id: str,
    expected: dict[str, Any],
    snapshot: dict[str, Any],
    manifest: dict[str, str],
) -> tuple[dict[str, Any], dict[str, datetime]]:
    instance = expected["instance"]
    rows = events_for_run(
        runs_dir / f"fleet-{feature}.ledger.jsonl",
        run_id=run_id,
        instance=instance,
    )
    if not rows:
        raise AssuranceError("run_id is absent from the lifecycle ledger")
    for row in rows:
        if row.get("feature") != feature or row.get("instance") != instance:
            raise AssuranceError("run identity does not match the expected participant")
        if row.get("role") != manifest.get(f"{instance}.role_type"):
            raise AssuranceError("run role does not match the FDP-3 roster")
        if row.get("phase") != expected["phase"]:
            raise AssuranceError("run phase does not match the FDP-3 stage")
        if row.get("task_sha256") != expected["prompt_sha256"]:
            raise AssuranceError("run task hash does not match the durable FDP-3 prompt")
        for field in ("provider", "model"):
            if row.get(field) != manifest.get(f"{instance}.{field}"):
                raise AssuranceError(f"run {field} does not match the FDP-3 roster")
    terminal_rows = [row for row in rows if row.get("status") in LIFECYCLE_TERMINALS]
    if not terminal_rows:
        raise AssuranceError("run is not terminal")
    if len(terminal_rows) != 1:
        raise AssuranceError("run has ambiguous terminal lifecycle evidence")
    started_at = min(parse_timestamp(row.get("timestamp"), "run.timestamp") for row in rows)
    terminal = terminal_rows[0]
    terminal_at = parse_timestamp(terminal.get("timestamp"), "run.terminal.timestamp")
    if terminal is not rows[-1] or terminal_at < started_at:
        raise AssuranceError("run lifecycle ordering is invalid")
    if started_at < parse_timestamp(snapshot["created_at"], "assurance.created_at"):
        raise AssuranceError("run predates the FDP-3 assurance")
    return terminal, {"started_at": started_at, "terminal_at": terminal_at}


def _message_by_id(
    runs_dir: Path,
    feature: str,
    message_id: str,
) -> tuple[dict[str, Any], bytes]:
    try:
        return fdp2._message_by_id(runs_dir, feature, message_id)
    except (fdp2.ControllerError, fleet_dialogue.DialogueError) as exc:
        raise AssuranceError(str(exc)) from exc


def _ingest_run_locked(
    runs_dir: Path,
    feature: str,
    events: list[dict[str, Any]],
    current: dict[str, Any],
    run_id: str,
    manifest: dict[str, str],
) -> tuple[dict[str, Any], str]:
    snapshot = current["snapshot"]
    expected = snapshot["expected"]
    if expected.get("type") != "run":
        raise AssuranceError("assurance is not awaiting a run")
    bound_runs, bound_messages = _bound_ids(events)
    if run_id in bound_runs or run_id in set(snapshot["context_run_ids"]):
        raise AssuranceConflict("run_id is already bound to this assurance context")
    relevant_snapshot = Path(
        snapshot[
            "challenge_snapshot_path"
            if expected["instance"] == CHALLENGE_INSTANCE
            else "verification_snapshot_path"
        ]
    )
    _validate_snapshot_worktree(relevant_snapshot, snapshot["accepted_head_sha"])
    terminal, times = _run_terminal(
        runs_dir,
        feature,
        run_id,
        expected,
        snapshot,
        manifest,
    )
    if times["terminal_at"] - times["started_at"] > timedelta(
        seconds=snapshot["run_timeout_seconds"]
    ):
        return (
            _terminal(snapshot, "indeterminate", f"run_timeout_exceeded:{run_id}"),
            "run_timed_out",
        )
    if times["terminal_at"] > parse_timestamp(snapshot["deadline_at"], "deadline"):
        return (
            _terminal(snapshot, "indeterminate", f"run_after_assurance_deadline:{run_id}"),
            "run_after_deadline",
        )
    status = str(terminal.get("status"))
    if status != "succeeded":
        mapped = {
            "failed": "failed",
            "blocked": "blocked",
            "abandoned": "abandoned",
            "indeterminate": "indeterminate",
        }.get(status, "indeterminate")
        return _terminal(snapshot, mapped, f"run_{status}:{run_id}"), "run_terminal"
    try:
        payload = fleet_dialogue._source_payload(
            runs_dir,
            feature,
            expected["instance"],
            run_id,
        )
        value = _parse_result(payload, run_id)
        next_snapshot = copy.deepcopy(snapshot)
        if expected["stage"] == "challenge":
            contract = _challenge_contract(value)
            _validate_evidence(
                contract,
                snapshot=snapshot,
                manifest=manifest,
                run_id=run_id,
                message_ids=bound_messages,
            )
            next_snapshot["status"] = "awaiting_challenge_message"
            next_snapshot["expected"] = _message_expected(
                "challenge",
                "challenge",
                VERIFY_INSTANCE,
                CHALLENGE_INSTANCE,
                run_id,
                expected["reply_to"],
                digest(payload),
            )
        elif expected["stage"] == "verification":
            contract = _verification_contract(value, snapshot["challenge_finding_ids"])
            _validate_evidence(
                contract,
                snapshot=snapshot,
                manifest=manifest,
                run_id=run_id,
                message_ids=bound_messages,
            )
            next_snapshot["status"] = "awaiting_verification_message"
            next_snapshot["expected"] = _message_expected(
                "verification",
                "verification",
                "lead",
                VERIFY_INSTANCE,
                run_id,
                expected["reply_to"],
                digest(payload),
            )
        else:
            raise AssuranceError(f"unsupported expected run stage: {expected['stage']}")
        return next_snapshot, "run_ingested"
    except (AssuranceContractError, fleet_dialogue.DialogueError) as exc:
        return (
            _terminal(
                snapshot,
                "indeterminate",
                f"invalid_{expected['stage']}_contract:{exc}",
            ),
            "contract_rejected",
        )


def _ingest_message_locked(
    runs_dir: Path,
    feature: str,
    events: list[dict[str, Any]],
    current: dict[str, Any],
    message_id: str,
    manifest: dict[str, str],
) -> tuple[dict[str, Any], str]:
    snapshot = current["snapshot"]
    expected = snapshot["expected"]
    if expected.get("type") != "message":
        raise AssuranceError("assurance is not awaiting a message")
    _, bound_messages = _bound_ids(events)
    if message_id in bound_messages or message_id in set(snapshot["context_message_ids"]):
        raise AssuranceConflict("message_id is already bound to this assurance context")
    message, payload = _message_by_id(runs_dir, feature, message_id)
    for field in (
        "kind",
        "recipient",
        "source_instance",
        "source_run_id",
        "reply_to",
        "payload_sha256",
    ):
        if message.get(field) != expected.get(field):
            raise AssuranceError(f"FDP-1 message does not match expected {field}")
    next_snapshot = copy.deepcopy(snapshot)
    try:
        value = _parse_result(payload, expected["source_run_id"])
        if expected["stage"] == "challenge":
            contract = _challenge_contract(value)
            _validate_evidence(
                contract,
                snapshot=snapshot,
                manifest=manifest,
                run_id=expected["source_run_id"],
                message_ids=bound_messages,
            )
            next_snapshot["challenge_message_id"] = message_id
            next_snapshot["challenge_finding_ids"] = [
                finding["finding_id"] for finding in contract["findings"]
            ]
            next_snapshot["status"] = "awaiting_phase_advance"
            next_snapshot["expected"] = _phase_expected()
            return next_snapshot, "challenge_published"
        if expected["stage"] == "verification":
            contract = _verification_contract(value, snapshot["challenge_finding_ids"])
            _validate_evidence(
                contract,
                snapshot=snapshot,
                manifest=manifest,
                run_id=expected["source_run_id"],
                message_ids=bound_messages,
            )
            next_snapshot["verification_message_id"] = message_id
            if contract["verdict"] == "VERIFIED":
                return (
                    _terminal(next_snapshot, "verified", "claude_verified"),
                    "assurance_verified",
                )
            return (
                _terminal(next_snapshot, "rejected", "claude_rejected"),
                "assurance_rejected",
            )
        raise AssuranceError(f"unsupported expected message stage: {expected['stage']}")
    except AssuranceContractError as exc:
        return (
            _terminal(
                snapshot,
                "indeterminate",
                f"invalid_{expected['stage']}_publication:{exc}",
            ),
            "contract_rejected",
        )


def _ingest_phase_advance_locked(
    runs_dir: Path,
    feature: str,
    current: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    snapshot = current["snapshot"]
    expected = snapshot["expected"]
    if expected.get("type") != "phase_advance":
        raise AssuranceError("assurance is not awaiting a phase advance")
    _require_verify_state(
        runs_dir,
        feature,
        control_head_sha256=current["event_sha256"],
    )
    if not snapshot.get("challenge_message_id"):
        raise AssuranceError("VERIFY cannot start without the exact GLM message")
    message, payload = _message_by_id(
        runs_dir,
        feature,
        snapshot["challenge_message_id"],
    )
    contract = _challenge_contract(_parse_result(payload, message["source_run_id"]))
    if [finding["finding_id"] for finding in contract["findings"]] != snapshot["challenge_finding_ids"]:
        raise AssuranceError("GLM finding identity changed before VERIFY")
    _validate_snapshot_worktree(
        Path(snapshot["verification_snapshot_path"]),
        snapshot["accepted_head_sha"],
    )
    prompt_file, prompt_sha = _verification_prompt(
        runs_dir,
        feature,
        current["assurance_id"],
        snapshot_path=snapshot["verification_snapshot_path"],
        accepted_head_sha=snapshot["accepted_head_sha"],
        context_file=snapshot["context_file"],
        context_sha256=snapshot["context_sha256"],
        challenge_message_id=snapshot["challenge_message_id"],
        challenge_payload=json.dumps(contract, indent=2, sort_keys=True).encode("utf-8"),
    )
    next_snapshot = copy.deepcopy(snapshot)
    next_snapshot["status"] = "awaiting_verification_run"
    next_snapshot["expected"] = _run_expected(
        "verification",
        VERIFY_INSTANCE,
        "VERIFY",
        prompt_file,
        prompt_sha,
        snapshot["challenge_message_id"],
    )
    return next_snapshot, "verify_phase_acknowledged"


def step(
    runs_dir: Path,
    *,
    feature: str,
    idempotency_key: str,
    run_id: str | None = None,
    message_id: str | None = None,
    phase_advanced: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    supplied = sum((run_id is not None, message_id is not None, phase_advanced))
    if supplied != 1:
        raise AssuranceError(
            "step requires exactly one of run_id, message_id, or phase_advanced"
        )
    request = {
        "command": "step",
        "run_id": run_id,
        "message_id": message_id,
        "phase_advanced": phase_advanced,
    }
    current_time = now or utc_now()
    with coordinator(runs_dir):
        events = load_events(runs_dir, feature)
        _expire_locked(runs_dir, feature, events, now=current_time)
        existing = _idempotent_event(events, idempotency_key, request)
        if existing is not None:
            return existing
        if not events or events[-1]["snapshot"]["status"] in TERMINAL_STATES:
            raise AssuranceConflict("feature has no active FDP-3 assurance")
        current = events[-1]
        manifest = _load_manifest(runs_dir, feature, live_identity=True)
        if closing_path(runs_dir, feature).exists():
            raise AssuranceConflict(f"fleet '{feature}' is closing")
        if run_id is not None:
            next_snapshot, event_type = _ingest_run_locked(
                runs_dir,
                feature,
                events,
                current,
                run_id,
                manifest,
            )
        elif message_id is not None:
            next_snapshot, event_type = _ingest_message_locked(
                runs_dir,
                feature,
                events,
                current,
                message_id,
                manifest,
            )
        else:
            next_snapshot, event_type = _ingest_phase_advance_locked(
                runs_dir,
                feature,
                current,
            )
        return _append_event_locked(
            runs_dir,
            feature,
            events,
            assurance_id=current["assurance_id"],
            event_type=event_type,
            idempotency_key=idempotency_key,
            request=request,
            snapshot=next_snapshot,
            now=current_time,
        )


def abandon(
    runs_dir: Path,
    *,
    feature: str,
    idempotency_key: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    _nonempty(reason, "abandon.reason")
    request = {"command": "abandon", "reason": reason}
    current_time = now or utc_now()
    with coordinator(runs_dir):
        events = load_events(runs_dir, feature)
        _expire_locked(runs_dir, feature, events, now=current_time)
        existing = _idempotent_event(events, idempotency_key, request)
        if existing is not None:
            return existing
        if not events or events[-1]["snapshot"]["status"] in TERMINAL_STATES:
            raise AssuranceConflict("feature has no active FDP-3 assurance")
        current = events[-1]
        return _append_event_locked(
            runs_dir,
            feature,
            events,
            assurance_id=current["assurance_id"],
            event_type="assurance_abandoned",
            idempotency_key=idempotency_key,
            request=request,
            snapshot=_terminal(
                current["snapshot"],
                "abandoned",
                f"operator_abandoned:{reason}",
            ),
            now=current_time,
        )


def show(
    runs_dir: Path,
    *,
    feature: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    with coordinator(runs_dir):
        events = load_events(runs_dir, feature)
        _expire_locked(runs_dir, feature, events, now=now)
        if not events:
            raise AssuranceError("feature has no FDP-3 assurance")
        return events[-1]


def _action(event: dict[str, Any]) -> dict[str, Any]:
    snapshot = event["snapshot"]
    expected = snapshot["expected"]
    if expected is None:
        return {
            "action": "terminal",
            "status": snapshot["status"],
            "reason": snapshot["terminal_reason"],
        }
    if expected["type"] == "run":
        return {
            "action": "dispatch",
            "instance": expected["instance"],
            "phase": expected["phase"],
            "stage": expected["stage"],
            "prompt_file": expected["prompt_file"],
            "prompt_sha256": expected["prompt_sha256"],
            "timeout_seconds": snapshot["run_timeout_seconds"],
        }
    if expected["type"] == "message":
        return {
            "action": "publish",
            "stage": expected["stage"],
            "kind": expected["kind"],
            "recipient": expected["recipient"],
            "source_instance": expected["source_instance"],
            "source_run_id": expected["source_run_id"],
            "reply_to": expected["reply_to"],
            "payload_sha256": expected["payload_sha256"],
        }
    return {
        "action": "advance_phase",
        "phase": "VERIFY",
        "evidence": event["event_sha256"],
        "then": "step --phase-advanced",
    }


def public_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "assurance_id": event["assurance_id"],
        "sequence": event["sequence"],
        "event_type": event["event_type"],
        "event_sha256": event["event_sha256"],
        "snapshot": event["snapshot"],
        "next_action": _action(event),
    }


def challenge_phase_gate(
    runs_dir: Path,
    feature: str,
    manifest: dict[str, str],
    evidence: str,
) -> str:
    try:
        fdp2._validate_roster(manifest)
    except fdp2.ControllerError as exc:
        raise AssuranceError(str(exc)) from exc
    with coordinator(runs_dir):
        events = load_events(runs_dir, feature)
        if not events:
            raise AssuranceError("FDP-3 CHALLENGE gate requires an assurance ledger")
        latest = events[-1]
        if latest["snapshot"]["status"] != "awaiting_phase_advance":
            raise AssuranceError(
                "FDP-3 CHALLENGE gate requires a valid published GLM challenge"
            )
        if evidence != latest["event_sha256"]:
            raise AssuranceError("FDP-3 CHALLENGE gate evidence is not the exact control head")
        if _state(runs_dir, feature).get("active_phase") != "CHALLENGE":
            raise AssuranceError("FDP-3 CHALLENGE gate requires active_phase=CHALLENGE")
        try:
            messages = fleet_dialogue.load_messages(runs_dir, feature)
            fleet_dialogue.verify_storage(
                fleet_dialogue.ledger_path(runs_dir, feature),
                fleet_dialogue.store_path(runs_dir, feature),
                feature=feature,
                instances=fdp2._manifest_instances(manifest),
            )
            maker_head = fdp2._clean_writer_head(manifest)
        except (fleet_dialogue.DialogueError, fdp2.ContractError) as exc:
            raise AssuranceError(str(exc)) from exc
        if maker_head != latest["snapshot"]["accepted_head_sha"]:
            raise AssuranceError("maker branch drifted before the FDP-3 VERIFY gate")
        _verify_artifacts(
            runs_dir / "assurance" / feature,
            events,
            live_paths=True,
        )
        _verify_message_bindings(events, messages)
        _validate_snapshot_worktree(
            Path(latest["snapshot"]["challenge_snapshot_path"]),
            latest["snapshot"]["accepted_head_sha"],
        )
        _validate_snapshot_worktree(
            Path(latest["snapshot"]["verification_snapshot_path"]),
            latest["snapshot"]["accepted_head_sha"],
        )
        return latest["event_sha256"]


def _artifact_expectations(events: list[dict[str, Any]]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    immutable: tuple[Any, ...] | None = None
    for event in events:
        assurance_id = event["assurance_id"]
        snapshot = event["snapshot"]
        identity = (
            assurance_id,
            snapshot["accepted_head_sha"],
            snapshot["fdp2_conversation_id"],
            snapshot["fdp2_control_head_sha256"],
            snapshot["context_file"],
            snapshot["context_sha256"],
            snapshot["snapshots_file"],
            snapshot["snapshots_sha256"],
            tuple(snapshot["context_run_ids"]),
            tuple(snapshot["context_message_ids"]),
        )
        if immutable is None:
            immutable = identity
        elif immutable != identity:
            raise AssuranceError("FDP-3 immutable context changed during assurance")
        context_logical = f"{assurance_id}/context/fdp2-context.json"
        snapshots_logical = f"{assurance_id}/snapshots.json"
        artifacts[context_logical] = snapshot["context_sha256"]
        artifacts[snapshots_logical] = snapshot["snapshots_sha256"]
        expected = snapshot.get("expected")
        if not isinstance(expected, dict) or expected.get("type") != "run":
            continue
        names = {
            "challenge": "challenge.txt",
            "verification": "verification.txt",
        }
        filename = names.get(expected["stage"])
        if filename is None or Path(expected["prompt_file"]).name != filename:
            raise AssuranceError("FDP-3 prompt binding has an invalid durable name")
        logical = f"{assurance_id}/prompts/{filename}"
        previous = artifacts.setdefault(logical, expected["prompt_sha256"])
        if previous != expected["prompt_sha256"]:
            raise AssuranceError("FDP-3 prompt binding changed after creation")
    return artifacts


def _safe_relative(value: Any, where: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise AssuranceError(f"{where} is not a path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise AssuranceError(f"{where} is unsafe")
    return path


def _regular_file_hash(path: Path) -> dict[str, Any]:
    try:
        return fdp2._regular_file_hash(path)
    except fdp2.ControllerError as exc:
        raise AssuranceError(str(exc).replace("verification receipt", "assurance")) from exc


def _verify_context_tree(root: Path, assurance_id: str) -> dict[str, Any]:
    context_path = root / assurance_id / "context" / "fdp2-context.json"
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssuranceError("FDP-3 copied context is missing or invalid") from exc
    if not isinstance(context, dict) or set(context) != {
        "schema_version",
        "feature",
        "assurance_id",
        "accepted_head_sha",
        "fdp2",
        "messages",
        "files",
    }:
        raise AssuranceError("FDP-3 context fields are invalid")
    if context.get("schema_version") != 1 or context.get("assurance_id") != assurance_id:
        raise AssuranceError("FDP-3 context identity is invalid")
    if not GIT_SHA.fullmatch(str(context.get("accepted_head_sha") or "")):
        raise AssuranceError("FDP-3 context accepted head is invalid")
    fdp2_context = context.get("fdp2")
    if not isinstance(fdp2_context, dict) or set(fdp2_context) != {
        "conversation_id",
        "control_head_sha256",
        "task_spec_sha256",
        "task_spec_path",
        "message_ids",
        "run_ids",
        "maker_verification_ids",
    }:
        raise AssuranceError("FDP-3 copied FDP-2 identity is invalid")
    _uuid(fdp2_context.get("conversation_id"), "context.fdp2.conversation_id")
    for field in ("control_head_sha256", "task_spec_sha256"):
        if not SHA256.fullmatch(str(fdp2_context.get(field) or "")):
            raise AssuranceError(f"FDP-3 context {field} is invalid")
    for field in ("message_ids", "run_ids", "maker_verification_ids"):
        _validate_string_list(fdp2_context.get(field), f"context.fdp2.{field}")
    files = context.get("files")
    if not isinstance(files, list) or not files:
        raise AssuranceError("FDP-3 context file manifest is empty")
    file_records: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(files):
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "bytes"}:
            raise AssuranceError(f"FDP-3 context file record {index} is invalid")
        relative = _safe_relative(record.get("path"), f"context.files[{index}].path")
        logical = relative.as_posix()
        if logical in file_records:
            raise AssuranceError("FDP-3 context file manifest has duplicates")
        if not SHA256.fullmatch(str(record.get("sha256") or "")):
            raise AssuranceError("FDP-3 context file hash is invalid")
        if isinstance(record.get("bytes"), bool) or not isinstance(record.get("bytes"), int) or record["bytes"] < 0:
            raise AssuranceError("FDP-3 context file size is invalid")
        actual = _regular_file_hash(root / assurance_id / Path(*relative.parts))
        if actual != {"sha256": record["sha256"], "bytes": record["bytes"]}:
            raise AssuranceError(f"FDP-3 context file hash mismatch: {logical}")
        file_records[logical] = record
    task_path = _safe_relative(fdp2_context["task_spec_path"], "context.fdp2.task_spec_path")
    task_record = file_records.get(task_path.as_posix())
    if not task_record or task_record["sha256"] != fdp2_context["task_spec_sha256"]:
        raise AssuranceError("FDP-3 copied task spec is not hash-bound")
    snapshots_record = file_records.get("snapshots.json")
    if snapshots_record is None:
        raise AssuranceError("FDP-3 context does not bind snapshot metadata")
    messages = context.get("messages")
    if not isinstance(messages, list):
        raise AssuranceError("FDP-3 copied messages are invalid")
    copied_ids: list[str] = []
    for index, entry in enumerate(messages):
        if not isinstance(entry, dict) or set(entry) != {
            "envelope",
            "payload_path",
            "payload_sha256",
            "payload_bytes",
        }:
            raise AssuranceError(f"FDP-3 copied message {index} is invalid")
        envelope = entry.get("envelope")
        if not isinstance(envelope, dict):
            raise AssuranceError("FDP-3 copied message envelope is invalid")
        try:
            fleet_dialogue._validate_envelope(envelope, str(context["feature"]))
        except fleet_dialogue.DialogueError as exc:
            raise AssuranceError("FDP-3 copied message envelope is invalid") from exc
        relative = _safe_relative(entry.get("payload_path"), "context.message.payload_path")
        record = file_records.get(relative.as_posix())
        if not record or record["sha256"] != entry.get("payload_sha256") or record["bytes"] != entry.get("payload_bytes"):
            raise AssuranceError("FDP-3 copied message payload is not hash-bound")
        if entry["payload_sha256"] != envelope.get("payload_sha256"):
            raise AssuranceError("FDP-3 copied message differs from its envelope")
        copied_ids.append(str(envelope.get("message_id")))
    if copied_ids != fdp2_context["message_ids"]:
        raise AssuranceError("FDP-3 copied message order/identity changed")
    snapshots_path = root / assurance_id / "snapshots.json"
    try:
        snapshots = json.loads(snapshots_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssuranceError("FDP-3 snapshot metadata is invalid") from exc
    if not isinstance(snapshots, dict) or set(snapshots) != {
        "schema_version",
        "accepted_head_sha",
        "snapshots",
    }:
        raise AssuranceError("FDP-3 snapshot metadata fields are invalid")
    if snapshots.get("schema_version") != 1 or snapshots.get("accepted_head_sha") != context["accepted_head_sha"]:
        raise AssuranceError("FDP-3 snapshot metadata identity is invalid")
    records = snapshots.get("snapshots")
    if not isinstance(records, list) or len(records) != 2:
        raise AssuranceError("FDP-3 requires exactly two snapshot records")
    expected = [(CHALLENGE_INSTANCE, "CHALLENGE"), (VERIFY_INSTANCE, "VERIFY")]
    for record, (instance, phase) in zip(records, expected, strict=True):
        if not isinstance(record, dict) or set(record) != {
            "instance",
            "phase",
            "path",
            "target_repo",
            "head_sha",
            "detached",
        }:
            raise AssuranceError("FDP-3 snapshot record fields are invalid")
        if (
            record.get("instance") != instance
            or record.get("phase") != phase
            or record.get("head_sha") != context["accepted_head_sha"]
            or record.get("detached") is not True
        ):
            raise AssuranceError("FDP-3 snapshot record identity is invalid")
        for field in ("path", "target_repo"):
            if not isinstance(record.get(field), str) or not record[field]:
                raise AssuranceError(f"FDP-3 snapshot {field} is invalid")
    return context


def _verify_artifacts(
    root: Path,
    events: list[dict[str, Any]],
    *,
    live_paths: bool,
) -> None:
    expectations = _artifact_expectations(events)
    for logical, expected_sha in expectations.items():
        if _regular_file_hash(root / Path(*PurePosixPath(logical).parts))["sha256"] != expected_sha:
            raise AssuranceError(f"FDP-3 durable artifact hash mismatch: {logical}")
    assurance_ids = {event["assurance_id"] for event in events}
    for assurance_id in assurance_ids:
        context = _verify_context_tree(root, assurance_id)
        matching = [event for event in events if event["assurance_id"] == assurance_id]
        snapshot = matching[-1]["snapshot"]
        if (
            context["feature"] != matching[-1]["feature"]
            or context["accepted_head_sha"] != snapshot["accepted_head_sha"]
            or context["fdp2"]["conversation_id"] != snapshot["fdp2_conversation_id"]
            or context["fdp2"]["control_head_sha256"] != snapshot["fdp2_control_head_sha256"]
            or context["fdp2"]["message_ids"] != snapshot["context_message_ids"]
            or context["fdp2"]["run_ids"] != snapshot["context_run_ids"]
        ):
            raise AssuranceError("FDP-3 context does not match its control snapshot")
        if live_paths:
            expected_root = (root / assurance_id).resolve()
            if Path(snapshot["context_file"]).resolve() != (expected_root / "context" / "fdp2-context.json").resolve():
                raise AssuranceError("FDP-3 context file is outside its durable directory")
            if Path(snapshot["snapshots_file"]).resolve() != (expected_root / "snapshots.json").resolve():
                raise AssuranceError("FDP-3 snapshots file is outside its durable directory")
            for event in matching:
                expected = event["snapshot"].get("expected")
                if isinstance(expected, dict) and expected.get("type") == "run":
                    prompt = expected_root / "prompts" / Path(expected["prompt_file"]).name
                    if Path(expected["prompt_file"]).resolve() != prompt.resolve():
                        raise AssuranceError("FDP-3 prompt is outside its durable directory")


def _verify_message_bindings(
    events: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> None:
    if not events:
        return
    snapshot = events[-1]["snapshot"]
    bound = {
        message_id
        for message_id in (
            snapshot.get("challenge_message_id"),
            snapshot.get("verification_message_id"),
        )
        if isinstance(message_id, str)
    }
    actual_messages = [
        message
        for message in messages
        if message.get("source_instance") in {CHALLENGE_INSTANCE, VERIFY_INSTANCE}
    ]
    if {message["message_id"] for message in actual_messages} != bound:
        raise AssuranceError("FDP-3 controller/message bindings are incomplete")
    by_id = {message["message_id"]: message for message in actual_messages}
    challenge_id = snapshot.get("challenge_message_id")
    if challenge_id:
        challenge = by_id[challenge_id]
        if (
            challenge.get("kind") != "challenge"
            or challenge.get("source_instance") != CHALLENGE_INSTANCE
            or challenge.get("recipient") != VERIFY_INSTANCE
            or challenge.get("reply_to") != snapshot["context_message_ids"][-1]
        ):
            raise AssuranceError("FDP-3 challenge message chain is invalid")
    verification_id = snapshot.get("verification_message_id")
    if verification_id:
        verification = by_id[verification_id]
        if (
            verification.get("kind") != "verification"
            or verification.get("source_instance") != VERIFY_INSTANCE
            or verification.get("recipient") != "lead"
            or verification.get("reply_to") != challenge_id
        ):
            raise AssuranceError("FDP-3 verification message chain is invalid")


def _summary(
    events: list[dict[str, Any]],
    dialogue_summary: dict[str, int],
) -> dict[str, Any]:
    latest = events[-1] if events else None
    return {
        "events": len(events),
        "assurances": len({event["assurance_id"] for event in events}),
        "latest_status": latest["snapshot"]["status"] if latest else None,
        "accepted_head_sha": latest["snapshot"]["accepted_head_sha"] if latest else None,
        "dialogue_messages": dialogue_summary["messages"],
        "payloads": dialogue_summary["payloads"],
        "payload_bytes": dialogue_summary["payload_bytes"],
        "control_head_sha256": latest["event_sha256"] if latest else None,
    }


def verify_live(
    runs_dir: Path,
    *,
    feature: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    with coordinator(runs_dir):
        events = load_events(runs_dir, feature)
        _expire_locked(runs_dir, feature, events, now=now)
        if not events:
            raise AssuranceError("feature has no FDP-3 assurance")
        manifest = _load_manifest(runs_dir, feature, live_identity=True)
        messages = fleet_dialogue.load_messages(runs_dir, feature)
        dialogue_summary = fleet_dialogue.verify_storage(
            fleet_dialogue.ledger_path(runs_dir, feature),
            fleet_dialogue.store_path(runs_dir, feature),
            feature=feature,
            instances=fdp2._manifest_instances(manifest),
        )
        _verify_artifacts(
            runs_dir / "assurance" / feature,
            events,
            live_paths=True,
        )
        _verify_message_bindings(events, messages)
        snapshot = events[-1]["snapshot"]
        _validate_snapshot_worktree(
            Path(snapshot["challenge_snapshot_path"]),
            snapshot["accepted_head_sha"],
        )
        _validate_snapshot_worktree(
            Path(snapshot["verification_snapshot_path"]),
            snapshot["accepted_head_sha"],
        )
        return {"feature": feature, **_summary(events, dialogue_summary)}


def _receipt_sources_live(runs_dir: Path, feature: str) -> dict[str, Path]:
    sources = {
        "assurance-control.jsonl": ledger_path(runs_dir, feature),
        "dialogue.jsonl": fleet_dialogue.ledger_path(runs_dir, feature),
    }
    for logical, path in (
        ("dialogue-control.jsonl", fdp2.ledger_path(runs_dir, feature)),
        ("ledger.jsonl", runs_dir / f"fleet-{feature}.ledger.jsonl"),
    ):
        if path.exists():
            sources[logical] = path
    assurance_store = runs_dir / "assurance" / feature
    if not assurance_store.is_dir():
        raise AssuranceError("FDP-3 assurance store is missing")
    for path in sorted(assurance_store.rglob("*")):
        if path.is_file() or path.is_symlink():
            sources[f"assurance/{path.relative_to(assurance_store).as_posix()}"] = path
    dialogue_store = runs_dir / "dialogue" / feature
    if not dialogue_store.is_dir():
        raise AssuranceError("FDP-3 dialogue store is missing")
    for path in sorted(dialogue_store.rglob("*")):
        if path.is_file() or path.is_symlink():
            sources[f"dialogue/{path.relative_to(dialogue_store).as_posix()}"] = path
    return sources


def create_live_receipt(
    runs_dir: Path,
    *,
    feature: str,
    receipt_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    summary = verify_live(runs_dir, feature=feature, now=now)
    if summary["latest_status"] not in TERMINAL_STATES:
        raise AssuranceConflict("fleet-down refuses an active FDP-3 assurance")
    files = {
        logical: _regular_file_hash(path)
        for logical, path in _receipt_sources_live(runs_dir, feature).items()
    }
    receipt = {
        "schema_version": 1,
        "feature": feature,
        "created_at": timestamp(now or utc_now()),
        "summary": summary,
        "files": files,
    }
    try:
        fdp2._write_atomic_json(receipt_path, receipt)
    except fdp2.ControllerError as exc:
        raise AssuranceError(str(exc)) from exc
    return receipt


def cleanup_snapshots(runs_dir: Path, *, feature: str) -> dict[str, Any]:
    manifest = _load_manifest(runs_dir, feature, live_identity=False)
    events = load_events(runs_dir, feature)
    if not events or events[-1]["snapshot"]["status"] not in TERMINAL_STATES:
        raise AssuranceConflict("snapshot cleanup requires terminal FDP-3 assurance")
    snapshot = events[-1]["snapshot"]
    paths = [
        Path(snapshot["challenge_snapshot_path"]),
        Path(snapshot["verification_snapshot_path"]),
    ]
    registered = {
        Path(line.removeprefix("worktree ")).resolve()
        for line in _git(
            manifest["target_repo"],
            "worktree",
            "list",
            "--porcelain",
        ).stdout.splitlines()
        if line.startswith("worktree ")
    }
    existing: list[Path] = []
    for path in paths:
        if path.exists():
            _validate_snapshot_worktree(path, snapshot["accepted_head_sha"])
            existing.append(path)
        elif path.resolve() in registered:
            raise AssuranceConflict(
                f"missing assurance snapshot remains registered; retained: {path}"
            )
    removed: list[str] = []
    for path in existing:
        result = _git(
            manifest["target_repo"],
            "worktree",
            "remove",
            str(path),
            check=False,
        )
        if result.returncode != 0:
            raise AssuranceConflict(
                f"could not remove clean assurance snapshot; retained: {path}"
            )
        removed.append(str(path))
    _git(manifest["target_repo"], "worktree", "prune")
    return {
        "feature": feature,
        "removed_snapshots": removed,
        "already_absent": [str(path) for path in paths if path not in existing],
    }


def verify_archive(archive: Path) -> dict[str, Any]:
    try:
        manifest = fdp2._manifest_values(archive / "manifest")
        fdp2._validate_roster(manifest)
    except fdp2.ControllerError as exc:
        raise AssuranceError(str(exc)) from exc
    feature = manifest.get("feature", "")
    if not fleet_dialogue.SAFE_FEATURE.fullmatch(feature):
        raise AssuranceError("archived manifest has an invalid feature")
    events = load_events_from_path(archive / "assurance-control.jsonl", feature)
    if not events or events[-1]["snapshot"]["status"] not in TERMINAL_STATES:
        raise AssuranceError("archived FDP-3 assurance is not terminal")
    instances = fdp2._manifest_instances(manifest)
    dialogue_summary = fleet_dialogue.verify_storage(
        archive / "dialogue.jsonl",
        archive / "dialogue" / "payloads",
        feature=feature,
        instances=instances,
    )
    messages = fleet_dialogue.load_messages_from_path(
        archive / "dialogue.jsonl",
        feature,
    )
    _verify_artifacts(archive / "assurance", events, live_paths=False)
    _verify_message_bindings(events, messages)
    receipt_path = archive / "assurance-receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssuranceError("archived FDP-3 receipt is missing or invalid") from exc
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "feature",
        "created_at",
        "summary",
        "files",
    }:
        raise AssuranceError("archived FDP-3 receipt fields are invalid")
    if receipt.get("schema_version") != 1 or receipt.get("feature") != feature:
        raise AssuranceError("archived FDP-3 receipt identity is invalid")
    parse_timestamp(receipt.get("created_at"), "receipt.created_at")
    files = receipt.get("files")
    if not isinstance(files, dict) or not files:
        raise AssuranceError("archived FDP-3 receipt has no file hashes")
    for logical, expected in files.items():
        relative = _safe_relative(logical, "receipt path")
        if not isinstance(expected, dict) or set(expected) != {"sha256", "bytes"}:
            raise AssuranceError("archived FDP-3 receipt hash entry is invalid")
        if _regular_file_hash(archive / Path(*relative.parts)) != expected:
            raise AssuranceError(f"archived FDP-3 receipt hash mismatch: {logical}")
    summary = {"feature": feature, **_summary(events, dialogue_summary)}
    if receipt.get("summary") != summary:
        raise AssuranceError("archived FDP-3 verification summary changed")
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "step", "show", "verify", "abandon"):
        command = subparsers.add_parser(name)
        command.add_argument("runs_dir", nargs="?" if name == "verify" else None)
        command.add_argument("--feature", required=name != "verify")
        if name in {"start", "step", "abandon"}:
            command.add_argument("--idempotency-key", required=True)
        if name == "step":
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--run-id")
            source.add_argument("--message-id")
            source.add_argument("--phase-advanced", action="store_true")
        if name == "abandon":
            command.add_argument("--reason", required=True)
        if name == "verify":
            command.add_argument("--archive")
            command.add_argument("--write-receipt")
            command.add_argument("--require-terminal", action="store_true")
            command.add_argument("--cleanup-snapshots", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "verify" and args.archive:
            if args.runs_dir or args.feature or args.write_receipt or args.cleanup_snapshots:
                raise AssuranceError(
                    "verify --archive cannot use runs_dir, feature, receipt, or cleanup"
                )
            print(
                json.dumps(
                    verify_archive(Path(args.archive).expanduser().resolve()),
                    sort_keys=True,
                )
            )
            return 0
        if not args.runs_dir:
            raise AssuranceError("live FDP-3 command requires runs_dir")
        runs_dir = Path(args.runs_dir).expanduser().resolve()
        if not args.feature:
            raise AssuranceError("live FDP-3 command requires --feature")
        if args.command == "start":
            value = start(
                runs_dir,
                feature=args.feature,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "step":
            value = step(
                runs_dir,
                feature=args.feature,
                idempotency_key=args.idempotency_key,
                run_id=args.run_id,
                message_id=args.message_id,
                phase_advanced=args.phase_advanced,
            )
        elif args.command == "abandon":
            value = abandon(
                runs_dir,
                feature=args.feature,
                idempotency_key=args.idempotency_key,
                reason=args.reason,
            )
        elif args.command == "show":
            value = show(runs_dir, feature=args.feature)
        else:
            if args.cleanup_snapshots:
                if args.write_receipt:
                    raise AssuranceError("snapshot cleanup cannot write a receipt")
                print(
                    json.dumps(
                        cleanup_snapshots(runs_dir, feature=args.feature),
                        sort_keys=True,
                    )
                )
                return 0
            if args.write_receipt:
                verified = create_live_receipt(
                    runs_dir,
                    feature=args.feature,
                    receipt_path=Path(args.write_receipt).expanduser().resolve(),
                )
                print(json.dumps(verified, sort_keys=True))
                return 0
            verified = verify_live(runs_dir, feature=args.feature)
            if args.require_terminal and verified["latest_status"] not in TERMINAL_STATES:
                raise AssuranceConflict("FDP-3 assurance is still active")
            print(json.dumps(verified, sort_keys=True))
            return 0
    except AssuranceConflict as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except (
        AssuranceError,
        fdp2.ControllerError,
        fdp2.ContractError,
        fleet_dialogue.DialogueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(public_event(value), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
