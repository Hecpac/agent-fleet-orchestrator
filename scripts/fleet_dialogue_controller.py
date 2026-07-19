#!/usr/bin/env python3
"""Bounded, durable Maker/Checker dialogue controlled one explicit step at a time."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from typing import Any
import uuid

import fleet_dialogue
import fleet_json
import fleet_manifest
import fleet_safe_paths
import fleet_state
from fleet_identity import validate as validate_identity
from fleet_leases import LeaseError, coordinator
from fleet_ledger import LedgerError, append_record, events_for_run, read_records


SCHEMA_VERSION = 1
FDP2_PRESET = "fleet_dialogue"
MAKER_INSTANCE = "maker"
CHECKER_INSTANCE = "checker"
MAX_REVISION_ROUNDS = 3
RUN_TIMEOUT_SECONDS = 30 * 60
DIALOGUE_DEADLINE_SECONDS = 4 * 60 * 60
MAX_CHECKER_EVIDENCE_BYTES = fleet_dialogue.MAX_PAYLOAD_BYTES
MAX_CONTROL_FILE_BYTES = fleet_dialogue.MAX_PAYLOAD_BYTES
MAX_CONTROL_LEDGER_BYTES = 64 * 1024 * 1024
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SAFE_CONTROL_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TERMINAL_STATES = {
    "accepted",
    "rejected",
    "failed",
    "blocked",
    "abandoned",
    "indeterminate",
}
LIFECYCLE_TERMINALS = {
    "succeeded",
    "failed",
    "blocked",
    "abandoned",
    "indeterminate",
}
ACTIVE_STATES = {
    "awaiting_proposal_run",
    "awaiting_proposal_message",
    "awaiting_checker_run",
    "awaiting_checker_message",
    "awaiting_rebuttal_message",
    "awaiting_revision_message",
    "awaiting_revision_run",
}
EVENT_FIELDS = {
    "schema_version",
    "sequence",
    "timestamp",
    "event_id",
    "feature",
    "conversation_id",
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
    "max_revision_rounds",
    "run_timeout_seconds",
    "revision_round",
    "maker_instance",
    "checker_instance",
    "task_spec_file",
    "task_spec_sha256",
    "start_head_sha",
    "current_head_sha",
    "accepted_head_sha",
    "last_message_id",
    "maker_verification_ids",
    "open_finding_ids",
    "expected",
    "terminal_reason",
}
RUN_EXPECTED_FIELDS = {
    "type",
    "stage",
    "instance",
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
ROSTER = {
    "maker": {
        "role_type": "codex",
        "phase": "BUILD",
        "authority": "write",
        "provider": "openai",
        "model": "gpt-5.6-sol",
    },
    "checker": {
        "role_type": "minimax_checker",
        "phase": "BUILD",
        "authority": "advisory",
        "provider": "minimax",
        "model": "MiniMax-M3",
        "variant": "none",
    },
    "challenge": {
        "role_type": "glm",
        "phase": "CHALLENGE",
        "authority": "advisory",
        "provider": "zai",
        "model": "glm-5.2",
    },
    "verify": {
        "role_type": "claude_reviewer",
        "phase": "VERIFY",
        "authority": "verification",
        "provider": "anthropic",
        "model": "claude-fable-5",
    },
}


class ControllerError(RuntimeError):
    pass


class ControllerConflict(ControllerError):
    pass


class ContractError(ControllerError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_timestamp(value: Any, where: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ControllerError(f"{where} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControllerError(f"{where} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ControllerError(f"{where} must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonical(value: Any) -> bytes:
    try:
        return fleet_json.canonical_bytes(value)
    except fleet_json.FleetJSONError as exc:
        raise ControllerError("FDP-2 value is not strict JSON") from exc


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical(value)
    return hashlib.sha256(raw).hexdigest()


def _feature(value: str) -> str:
    if not isinstance(value, str) or not fleet_dialogue.SAFE_FEATURE.fullmatch(value):
        raise ControllerError("invalid feature")
    return value


def ledger_path(runs_dir: Path, feature: str) -> Path:
    return runs_dir / f"fleet-{_feature(feature)}.dialogue-control.jsonl"


def prompt_root(runs_dir: Path, feature: str, conversation_id: str) -> Path:
    return runs_dir / _prompt_directory_relative(feature, conversation_id)


def _uuid(value: Any, where: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise ControllerError(f"invalid {where}") from exc


def _control_directory_relative(feature: str, conversation_id: str) -> Path:
    return (
        Path("dialogue")
        / _feature(feature)
        / "control"
        / _uuid(conversation_id, "conversation_id")
    )


def _prompt_directory_relative(feature: str, conversation_id: str) -> Path:
    return _control_directory_relative(feature, conversation_id) / "prompts"


def _task_spec_relative(feature: str, conversation_id: str) -> Path:
    return _control_directory_relative(feature, conversation_id) / "task-spec.json"


def _prompt_relative(
    feature: str, conversation_id: str, filename: str
) -> Path:
    if not isinstance(filename, str) or not SAFE_CONTROL_FILE.fullmatch(filename):
        raise ControllerError("invalid FDP-2 prompt filename")
    return _prompt_directory_relative(feature, conversation_id) / filename


def _control_directory_modes(relative: Path) -> tuple[int, ...]:
    modes = (0o755, 0o755, 0o755, 0o700)
    if relative.parts[-1:] == ("prompts",) or relative.parts[-2:-1] == ("prompts",):
        return (*modes, 0o700)
    return modes


def _validate_idempotency_key(value: str) -> None:
    if not fleet_dialogue.SAFE_IDEMPOTENCY_KEY.fullmatch(value):
        raise ControllerError("invalid idempotency key")


def _lease_exists(store: Any, name: str) -> bool:
    try:
        return bool(store.exists_name(name))
    except LeaseError as exc:
        raise ControllerError(f"cannot inspect FDP-2 lease state: {exc}") from exc


def _validate_expected(value: Any) -> None:
    if not isinstance(value, dict):
        raise ControllerError("active dialogue snapshot lacks expected action")
    expected_type = value.get("type")
    fields = RUN_EXPECTED_FIELDS if expected_type == "run" else MESSAGE_EXPECTED_FIELDS
    if expected_type not in {"run", "message"} or set(value) != fields:
        raise ControllerError("dialogue expected action has invalid fields")
    if not isinstance(value.get("stage"), str) or not value["stage"]:
        raise ControllerError("dialogue expected stage is invalid")
    if expected_type == "run":
        if value.get("instance") not in {MAKER_INSTANCE, CHECKER_INSTANCE}:
            raise ControllerError("dialogue expected run instance is invalid")
        if not SHA256.fullmatch(str(value.get("prompt_sha256") or "")):
            raise ControllerError("dialogue expected prompt hash is invalid")
        if not isinstance(value.get("prompt_file"), str) or not value["prompt_file"]:
            raise ControllerError("dialogue expected prompt file is invalid")
    else:
        if value.get("kind") not in fleet_dialogue.MESSAGE_KINDS:
            raise ControllerError("dialogue expected message kind is invalid")
        if not fleet_dialogue.SAFE_RUN_ID.fullmatch(str(value.get("source_run_id") or "")):
            raise ControllerError("dialogue expected source run is invalid")
        if not SHA256.fullmatch(str(value.get("payload_sha256") or "")):
            raise ControllerError("dialogue expected payload hash is invalid")


def _validate_snapshot(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != SNAPSHOT_FIELDS:
        raise ControllerError("dialogue snapshot fields do not match schema_version=1")
    if value.get("status") not in ACTIVE_STATES | TERMINAL_STATES:
        raise ControllerError("dialogue snapshot status is invalid")
    parse_timestamp(value.get("created_at"), "snapshot.created_at")
    deadline = parse_timestamp(value.get("deadline_at"), "snapshot.deadline_at")
    if deadline <= parse_timestamp(value["created_at"], "snapshot.created_at"):
        raise ControllerError("dialogue deadline must follow creation")
    if value.get("max_revision_rounds") != MAX_REVISION_ROUNDS:
        raise ControllerError("dialogue max_revision_rounds is unsupported")
    if value.get("run_timeout_seconds") != RUN_TIMEOUT_SECONDS:
        raise ControllerError("dialogue run_timeout_seconds is unsupported")
    round_number = value.get("revision_round")
    if isinstance(round_number, bool) or not isinstance(round_number, int) or not 0 <= round_number <= MAX_REVISION_ROUNDS:
        raise ControllerError("dialogue revision_round is invalid")
    if value.get("maker_instance") != MAKER_INSTANCE or value.get("checker_instance") != CHECKER_INSTANCE:
        raise ControllerError("dialogue participant identities are invalid")
    if not isinstance(value.get("task_spec_file"), str) or not value["task_spec_file"]:
        raise ControllerError("dialogue task_spec_file is invalid")
    if not SHA256.fullmatch(str(value.get("task_spec_sha256") or "")):
        raise ControllerError("dialogue task_spec_sha256 is invalid")
    for field in ("start_head_sha", "current_head_sha"):
        if not GIT_SHA.fullmatch(str(value.get(field) or "")):
            raise ControllerError(f"dialogue {field} is invalid")
    accepted = value.get("accepted_head_sha")
    if accepted is not None and not GIT_SHA.fullmatch(str(accepted)):
        raise ControllerError("dialogue accepted_head_sha is invalid")
    for field in ("maker_verification_ids", "open_finding_ids"):
        items = value.get(field)
        if not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items):
            raise ControllerError(f"dialogue {field} is invalid")
        if len(items) != len(set(items)):
            raise ControllerError(f"dialogue {field} contains duplicates")
    if value["status"] in TERMINAL_STATES:
        if value.get("expected") is not None:
            raise ControllerError("terminal dialogue cannot expect another action")
        if not isinstance(value.get("terminal_reason"), str) or not value["terminal_reason"]:
            raise ControllerError("terminal dialogue lacks a reason")
        if value["status"] == "accepted" and value.get("accepted_head_sha") != value.get("current_head_sha"):
            raise ControllerError("accepted dialogue head is inconsistent")
    else:
        _validate_expected(value.get("expected"))
        if value.get("terminal_reason") is not None or value.get("accepted_head_sha") is not None:
            raise ControllerError("active dialogue contains terminal fields")


def _validated_events(
    records: list[dict[str, Any]], feature: str | None = None
) -> list[dict[str, Any]]:
    if feature is not None:
        feature = _feature(feature)
    events: list[dict[str, Any]] = []
    previous_sha: str | None = None
    event_ids: set[str] = set()
    idempotency_keys: set[str] = set()
    for line_number, event in enumerate(records, start=1):
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise ControllerError("dialogue control event fields do not match schema_version=1")
        if event.get("schema_version") != SCHEMA_VERSION or event.get("sequence") != line_number:
            raise ControllerError("dialogue control sequence/schema is invalid")
        _uuid(event.get("event_id"), "event_id")
        _uuid(event.get("conversation_id"), "conversation_id")
        if feature is not None and event.get("feature") != feature:
            raise ControllerError("dialogue control event belongs to another feature")
        if not isinstance(event.get("feature"), str) or not fleet_dialogue.SAFE_FEATURE.fullmatch(event["feature"]):
            raise ControllerError("dialogue control feature is invalid")
        parse_timestamp(event.get("timestamp"), "event.timestamp")
        _validate_idempotency_key(str(event.get("idempotency_key") or ""))
        if not SHA256.fullmatch(str(event.get("request_sha256") or "")):
            raise ControllerError("dialogue control request hash is invalid")
        if event.get("previous_sha256") != previous_sha:
            raise ControllerError("dialogue control hash chain is broken")
        stored_sha = event.get("event_sha256")
        unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
        if stored_sha != digest(unsigned):
            raise ControllerError("dialogue control event hash is invalid")
        if event["event_id"] in event_ids or event["idempotency_key"] in idempotency_keys:
            raise ControllerError("dialogue control ledger contains duplicate identity")
        _validate_snapshot(event.get("snapshot"))
        event_ids.add(event["event_id"])
        idempotency_keys.add(event["idempotency_key"])
        previous_sha = str(stored_sha)
        events.append(event)
    return events


def _load_events_rooted(
    root: Path, ledger_leaf: str, feature: str | None = None
) -> list[dict[str, Any]]:
    if feature is not None:
        feature = _feature(feature)
        allowed = {"dialogue-control.jsonl", f"fleet-{feature}.dialogue-control.jsonl"}
        if ledger_leaf not in allowed:
            raise ControllerError("dialogue control ledger is outside its selected root")
    elif ledger_leaf != "dialogue-control.jsonl":
        prefix = "fleet-"
        suffix = ".dialogue-control.jsonl"
        candidate = (
            ledger_leaf[len(prefix) : -len(suffix)]
            if ledger_leaf.startswith(prefix) and ledger_leaf.endswith(suffix)
            else ""
        )
        if not fleet_dialogue.SAFE_FEATURE.fullmatch(candidate):
            raise ControllerError("dialogue control ledger is outside its selected root")
    if Path(ledger_leaf).name != ledger_leaf:
        raise ControllerError("dialogue control ledger is outside its selected root")
    try:
        records = read_records(root / ledger_leaf, runs_dir=root)
    except LedgerError as exc:
        raise ControllerError(f"cannot read dialogue control ledger: {exc}") from exc
    return _validated_events(records, feature)


def load_events_from_path(path: Path, feature: str | None = None) -> list[dict[str, Any]]:
    if feature is not None:
        feature = _feature(feature)
    path = Path(path)
    root = path.parent
    if path != root / path.name:
        raise ControllerError("dialogue control ledger is outside its selected root")
    return _load_events_rooted(root, path.name, feature)


def load_events(runs_dir: Path, feature: str) -> list[dict[str, Any]]:
    feature = _feature(feature)
    return _load_events_rooted(runs_dir, ledger_path(runs_dir, feature).name, feature)


def publication_state_locked(
    runs_dir: Path, feature: str
) -> dict[str, Any] | None:
    """Return the controller publication authority while coordinator is held.

    The caller owns the fleet coordinator lock.  A present control ledger is
    authoritative even after it reaches a terminal state; legacy publication
    is allowed only when no controller ledger exists at all.
    """

    feature = _feature(feature)
    path = ledger_path(runs_dir, feature)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ControllerError("cannot inspect FDP-2 publication state") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ControllerError("FDP-2 publication state must be a regular file")
    events = load_events(runs_dir, feature)
    if not events:
        raise ControllerError("FDP-2 publication state ledger is empty")
    current = events[-1]
    snapshot = current["snapshot"]
    return {
        "controller": "FDP-2",
        "controller_id": current["conversation_id"],
        "status": snapshot["status"],
        "active": snapshot["status"] in ACTIVE_STATES,
        "expected": copy.deepcopy(snapshot["expected"]),
    }


def _active_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not events:
        return None
    latest = events[-1]
    return latest if latest["snapshot"]["status"] in ACTIVE_STATES else None


def _current_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return events[-1] if events else None


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
        raise ControllerConflict("idempotency key was already used for another control request")
    return existing


def _append_event_locked(
    runs_dir: Path,
    feature: str,
    events: list[dict[str, Any]],
    *,
    conversation_id: str,
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
        "conversation_id": conversation_id,
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
        runs_dir=runs_dir,
    )
    if not appended:
        raise ControllerConflict("dialogue control ledger changed during append")
    events.append(event)
    return event


def _terminal(snapshot: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    value = copy.deepcopy(snapshot)
    value["status"] = status
    value["expected"] = None
    value["terminal_reason"] = reason
    value["accepted_head_sha"] = value["current_head_sha"] if status == "accepted" else None
    return value


def _expire_locked(
    runs_dir: Path,
    feature: str,
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    active = _active_event(events)
    current_time = now or utc_now()
    if active is None or current_time < parse_timestamp(active["snapshot"]["deadline_at"], "deadline"):
        return active
    conversation_id = active["conversation_id"]
    request = {"command": "system_deadline", "conversation_id": conversation_id}
    key = f"system:deadline:{conversation_id}"
    return _append_event_locked(
        runs_dir,
        feature,
        events,
        conversation_id=conversation_id,
        event_type="deadline_expired",
        idempotency_key=key,
        request=request,
        snapshot=_terminal(active["snapshot"], "indeterminate", "dialogue_deadline_exceeded"),
        now=current_time,
    )


def _read_rooted_regular(
    root: Path,
    relative: Path | str,
    *,
    directory_modes: tuple[int, ...],
    max_bytes: int,
    where: str,
) -> bytes:
    try:
        with fleet_safe_paths.RootedFS(root) as rooted:
            payload = rooted.read_regular(
                relative,
                directory_modes=directory_modes,
                file_mode=0o600,
                max_bytes=max_bytes,
            )
            rooted.assert_root_binding()
            return payload
    except fleet_safe_paths.SafePathError as exc:
        raise ControllerError(f"unsafe {where}: {exc}") from exc


def _manifest_values_from_bytes(payload: bytes) -> dict[str, str]:
    try:
        return fleet_manifest.parse_bytes(payload)
    except fleet_manifest.ManifestError as exc:
        raise ControllerError(f"invalid fleet manifest: {exc}") from exc


def _manifest_values(path: Path) -> dict[str, str]:
    path = Path(path)
    root = path.parent
    leaf = path.name
    if path != root / leaf:
        raise ControllerError("fleet manifest is outside its selected root")
    if leaf != "manifest":
        prefix = "fleet-"
        suffix = ".manifest"
        feature = (
            leaf[len(prefix) : -len(suffix)]
            if leaf.startswith(prefix) and leaf.endswith(suffix)
            else ""
        )
        if not fleet_dialogue.SAFE_FEATURE.fullmatch(feature):
            raise ControllerError("fleet manifest is outside its selected root")
    payload = _read_rooted_regular(
        root,
        leaf,
        directory_modes=(),
        max_bytes=MAX_CONTROL_FILE_BYTES,
        where="fleet manifest path",
    )
    return _manifest_values_from_bytes(payload)


def _load_live_manifest(runs_dir: Path, feature: str) -> dict[str, str]:
    feature = _feature(feature)
    leaf = f"fleet-{feature}.manifest"
    payload = _read_rooted_regular(
        runs_dir,
        leaf,
        directory_modes=(),
        max_bytes=MAX_CONTROL_FILE_BYTES,
        where="live fleet manifest path",
    )
    manifest = _manifest_values_from_bytes(payload)
    if manifest.get("feature") != feature:
        raise ControllerError("manifest feature mismatch")
    instances = sorted(
        key
        for key, value in manifest.items()
        if value.startswith("surface:") and manifest.get(f"{key}.uuid")
    )
    try:
        errors = validate_identity(manifest, instances)
    except RuntimeError as exc:
        raise ControllerError(f"cannot validate live fleet identity: {exc}") from exc
    if errors:
        raise ControllerError("; ".join(errors))
    return manifest


def _validate_roster(manifest: dict[str, str]) -> None:
    if manifest.get("preset") != FDP2_PRESET:
        raise ControllerError(f"FDP-2 requires preset={FDP2_PRESET}")
    actual_instances = {
        key
        for key, value in manifest.items()
        if value.startswith("surface:") and manifest.get(f"{key}.uuid")
    }
    if actual_instances != {"lead", *ROSTER} or manifest.get("lead.phase") != "CONTROL":
        raise ControllerError("FDP-2 manifest does not contain the exact CONTROL/roster instances")
    for instance, expected in ROSTER.items():
        if not manifest.get(instance) or not manifest.get(f"{instance}.uuid"):
            raise ControllerError(f"FDP-2 manifest lacks instance: {instance}")
        for field, value in expected.items():
            if manifest.get(f"{instance}.{field}") != value:
                raise ControllerError(
                    f"FDP-2 roster mismatch: {instance}.{field} must be {value}"
                )
    if not manifest.get("target_repo"):
        raise ControllerError("FDP-2 requires --target-repo")
    for field in ("worktree", "branch", "base_sha", "git_isolation", "publication_state"):
        if not manifest.get(f"{MAKER_INSTANCE}.{field}"):
            raise ControllerError(f"FDP-2 maker lacks durable Git metadata: {field}")
    if manifest[f"{MAKER_INSTANCE}.git_isolation"] != "isolated-clone":
        raise ControllerError("FDP-2 maker must use an isolated Git clone")
    if manifest[f"{MAKER_INSTANCE}.publication_state"] not in {"private", "published"}:
        raise ControllerError("FDP-2 maker has an invalid publication state")


def _state_phase(
    runs_dir: Path,
    feature: str,
    manifest: dict[str, str] | None = None,
) -> str:
    feature = _feature(feature)
    manifest_path = runs_dir / f"fleet-{feature}.manifest"
    try:
        live_manifest, state = fleet_state.load_live(manifest_path)
    except (
        OSError,
        fleet_safe_paths.SafePathError,
        fleet_state.PhaseStateError,
    ) as exc:
        raise ControllerError(
            f"cannot read fleet state: {manifest_path.with_suffix('.state.json')}"
        ) from exc
    if manifest is not None and live_manifest != manifest:
        raise ControllerError("fleet manifest changed before phase validation")
    phase = state.get("active_phase")
    if phase != "BUILD":
        raise ControllerError(f"FDP-2 start requires active_phase=BUILD, got {phase}")
    return str(phase)


def _writer_git_root(manifest: dict[str, str]) -> str:
    isolation = manifest.get(f"{MAKER_INSTANCE}.git_isolation")
    publication_state = manifest.get(f"{MAKER_INSTANCE}.publication_state")
    if isolation != "isolated-clone":
        raise ContractError("maker Git store is not isolated")
    if publication_state == "private":
        if manifest.get(f"{MAKER_INSTANCE}.published_sha") or manifest.get("workspace.quiesced") == "1":
            raise ContractError("private maker claims published or quiescent state")
        branch = manifest[f"{MAKER_INSTANCE}.branch"]
        target_ref = subprocess.run(
            [
                "git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                "-c", "submodule.recurse=false", "-C", manifest["target_repo"],
                "show-ref", "--verify", "--quiet",
                f"refs/heads/{branch}",
            ],
            env=_safe_git_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if target_ref.returncode == 0:
            raise ContractError("private maker branch already exists in target")
        if target_ref.returncode not in {0, 1}:
            raise ContractError("cannot verify private maker target branch absence")
        return manifest[f"{MAKER_INSTANCE}.worktree"]
    if publication_state == "published":
        final_sha = manifest.get(f"{MAKER_INSTANCE}.final_sha", "")
        if (
            manifest.get("workspace.quiesced") != "1"
            or not GIT_SHA.fullmatch(final_sha)
            or manifest.get(f"{MAKER_INSTANCE}.published_sha") != final_sha
        ):
            raise ContractError("published maker Git metadata is inconsistent")
        return manifest["target_repo"]
    raise ContractError("maker publication state is invalid")


def _safe_git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
        }
    )
    return environment


def _validate_private_git_metadata(root: Path) -> None:
    try:
        physical_root = root.resolve(strict=True)
        git_dir = physical_root / ".git"
        info = git_dir.lstat()
    except OSError as exc:
        raise ContractError("maker private Git store is unavailable") from exc
    if physical_root != root or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ContractError("maker private Git store is not an exact physical directory")
    for directory, directories, files in os.walk(git_dir, followlinks=False):
        for name in [*directories, *files]:
            path = Path(directory) / name
            entry = path.lstat()
            if stat.S_ISLNK(entry.st_mode):
                raise ContractError("maker Git metadata contains a symlink")
            if name in directories:
                if not stat.S_ISDIR(entry.st_mode):
                    raise ContractError("maker Git metadata contains a special directory")
            elif not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                raise ContractError("maker Git metadata contains a special or shared file")
    alternates = git_dir / "objects" / "info" / "alternates"
    if alternates.exists() or alternates.is_symlink():
        raise ContractError("maker Git object alternates are forbidden")
    hooks = git_dir / "hooks"
    if hooks.exists():
        for hook in hooks.iterdir():
            entry = hook.lstat()
            if stat.S_ISLNK(entry.st_mode) or (
                stat.S_ISREG(entry.st_mode)
                and entry.st_mode & 0o111
                and not hook.name.endswith(".sample")
            ):
                raise ContractError("maker Git metadata contains an executable hook")
    config = subprocess.run(
        [
            "git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
            "-c", "submodule.recurse=false", "config", "--no-includes", "--file",
            str(git_dir / "config"), "--name-only", "--list",
        ],
        env=_safe_git_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    allowed = {
        "core.repositoryformatversion", "core.filemode", "core.bare",
        "core.logallrefupdates", "core.ignorecase", "core.precomposeunicode",
        "core.symlinks", "user.name", "user.email",
    }
    if config.returncode != 0 or any(
        key not in allowed for key in config.stdout.splitlines()
    ):
        raise ContractError("maker Git config contains executable or indirect settings")


def _git(manifest: dict[str, str], *args: str, worktree: bool = False) -> str:
    root = (
        manifest[f"{MAKER_INSTANCE}.worktree"]
        if worktree
        else _writer_git_root(manifest)
    )
    if worktree or manifest.get(f"{MAKER_INSTANCE}.publication_state") == "private":
        _validate_private_git_metadata(Path(root))
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "submodule.recurse=false",
            "-C",
            root,
            *args,
        ],
        env=_safe_git_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise ContractError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_bytes(manifest: dict[str, str], *args: str, worktree: bool = False) -> bytes:
    root = (
        manifest[f"{MAKER_INSTANCE}.worktree"]
        if worktree
        else _writer_git_root(manifest)
    )
    if worktree or manifest.get(f"{MAKER_INSTANCE}.publication_state") == "private":
        _validate_private_git_metadata(Path(root))
    result = subprocess.run(
        [
            "git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
            "-c", "submodule.recurse=false", "-C", root, *args,
        ],
        env=_safe_git_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _clean_writer_head(manifest: dict[str, str]) -> str:
    branch = manifest[f"{MAKER_INSTANCE}.branch"]
    worktree = Path(manifest[f"{MAKER_INSTANCE}.worktree"])
    publication_state = manifest[f"{MAKER_INSTANCE}.publication_state"]
    if publication_state == "published":
        if worktree.exists() or worktree.is_symlink():
            raise ContractError("published maker still has a mutable clone")
        branch_head = _git(manifest, "rev-parse", "--verify", f"refs/heads/{branch}")
        if branch_head != manifest.get(f"{MAKER_INSTANCE}.final_sha"):
            raise ContractError("published maker branch differs from final_sha")
        return branch_head
    if not worktree.is_dir():
        raise ContractError("maker worktree is missing")
    git_common_dir = _git(
        manifest,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        worktree=True,
    )
    git_dir = _git(
        manifest,
        "rev-parse",
        "--path-format=absolute",
        "--git-dir",
        worktree=True,
    )
    top_level = _git(
        manifest,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        worktree=True,
    )
    if (
        Path(git_common_dir) != worktree / ".git"
        or Path(git_dir) != worktree / ".git"
        or Path(top_level) != worktree
        or _git(manifest, "remote", worktree=True)
    ):
        raise ContractError("maker worktree does not have an isolated Git store")
    if _git(manifest, "symbolic-ref", "--quiet", "--short", "HEAD", worktree=True) != branch:
        raise ContractError("maker worktree is not attached to its durable branch")
    if _git(manifest, "status", "--porcelain", worktree=True):
        raise ContractError("maker worktree is not clean")
    worktree_head = _git(manifest, "rev-parse", "--verify", "HEAD", worktree=True)
    branch_head = _git(manifest, "rev-parse", "--verify", f"refs/heads/{branch}")
    if worktree_head != branch_head:
        raise ContractError("maker worktree HEAD differs from durable branch")
    if not GIT_SHA.fullmatch(branch_head):
        raise ContractError("maker branch HEAD is not a SHA-1 identity")
    return branch_head


def _validate_maker_commit(
    manifest: dict[str, str], *, base_sha: str, head_sha: str
) -> None:
    if not GIT_SHA.fullmatch(base_sha) or not GIT_SHA.fullmatch(head_sha):
        raise ContractError("maker base_sha/head_sha is invalid")
    current = _clean_writer_head(manifest)
    if current != head_sha:
        raise ContractError("maker head_sha does not match the durable branch")
    try:
        _git(manifest, "merge-base", "--is-ancestor", base_sha, head_sha)
    except ContractError:
        raise ContractError("maker revision is not append-only from base_sha")
    if _git(manifest, "rev-list", "--count", f"{base_sha}..{head_sha}") != "1":
        raise ContractError("each Maker result must add exactly one commit")


def _template_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "orchestration" / "prompts" / name


def _render_template(name: str, replacements: dict[str, str]) -> str:
    try:
        text = _template_path(name).read_text(encoding="utf-8")
    except OSError as exc:
        raise ControllerError(f"missing FDP-2 prompt template: {name}") from exc
    placeholders = set(re.findall(r"\{\{([A-Z0-9_]+)\}\}", text))
    missing = sorted(placeholders - replacements.keys())
    if missing:
        raise ControllerError(
            f"unresolved placeholder in FDP-2 prompt {name}: {', '.join(missing)}"
        )
    # Substitute only tokens that were present in the trusted template. Replacement
    # values are untrusted evidence and must remain opaque even when a diff contains a
    # token such as ``{{FEATURE}}`` that is also meaningful to this template.
    text = re.sub(
        r"\{\{([A-Z0-9_]+)\}\}",
        lambda match: replacements[match.group(1)],
        text,
    )
    # Shell command substitution preserves this payload byte-for-byte only when
    # it has no trailing newline. The lifecycle ledger hashes the task argument,
    # so prompt files deliberately end on the final visible character.
    return text.rstrip()


def _write_prompt(
    runs_dir: Path,
    feature: str,
    conversation_id: str,
    filename: str,
    content: str,
) -> tuple[str, str]:
    relative = _prompt_relative(feature, conversation_id, filename)
    payload = content.encode("utf-8")
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            rooted.atomic_write(
                relative,
                payload,
                directory_modes=_control_directory_modes(relative),
                file_mode=0o600,
            )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise ControllerConflict(f"unsafe or changed durable FDP-2 prompt: {exc}") from exc
    return str(runs_dir / relative), hashlib.sha256(payload).hexdigest()


def _parse_task_spec_payload(payload: bytes) -> tuple[dict[str, Any], bytes, str]:
    try:
        value = fleet_json.loads(payload)
    except fleet_json.FleetJSONError as exc:
        raise ControllerError("FDP-2 task spec is not valid UTF-8 JSON") from exc
    item = _strict_object(
        value,
        {"objective", "negative_scope", "acceptance_criteria"},
        "task spec",
    )
    _nonempty_string(item.get("objective"), "task spec.objective")
    for field in ("negative_scope", "acceptance_criteria"):
        values = item.get(field)
        if not isinstance(values, list) or not values or any(
            not isinstance(entry, str) or not entry.strip() for entry in values
        ):
            raise ControllerError(f"task spec.{field} must contain non-empty strings")
    return item, payload, hashlib.sha256(payload).hexdigest()


def _read_task_spec(path: Path) -> tuple[dict[str, Any], bytes, str]:
    try:
        payload = fleet_dialogue._read_bounded_regular_file(path)
    except fleet_dialogue.DialogueError as exc:
        raise ControllerError("FDP-2 task spec is unavailable or invalid") from exc
    return _parse_task_spec_payload(payload)


def _write_task_spec(
    runs_dir: Path,
    feature: str,
    conversation_id: str,
    payload: bytes,
) -> str:
    relative = _task_spec_relative(feature, conversation_id)
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            rooted.atomic_write(
                relative,
                payload,
                directory_modes=_control_directory_modes(relative),
                file_mode=0o600,
            )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise ControllerConflict(
            f"unsafe or changed durable FDP-2 task spec: {exc}"
        ) from exc
    return str(runs_dir / relative)


def _run_expected(
    stage: str,
    instance: str,
    prompt_file: str,
    prompt_sha256: str,
    reply_to: str | None,
) -> dict[str, Any]:
    return {
        "type": "run",
        "stage": stage,
        "instance": instance,
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


def _proposal_prompt(
    runs_dir: Path,
    feature: str,
    conversation_id: str,
    manifest: dict[str, str],
    base_sha: str,
    task_spec_file: str,
    task_spec_sha256: str,
    task_spec: dict[str, Any],
) -> tuple[str, str]:
    content = _render_template(
        "fdp2_maker_proposal.md",
        {
            "FEATURE": feature,
            "CONVERSATION_ID": conversation_id,
            "TARGET_REPO": manifest["target_repo"],
            "WORKTREE": manifest[f"{MAKER_INSTANCE}.worktree"],
            "BRANCH": manifest[f"{MAKER_INSTANCE}.branch"],
            "BASE_SHA": base_sha,
            "TASK_SPEC_FILE": task_spec_file,
            "TASK_SPEC_SHA256": task_spec_sha256,
            "TASK_SPEC_JSON": json.dumps(task_spec, indent=2, sort_keys=True),
        },
    )
    return _write_prompt(runs_dir, feature, conversation_id, "proposal.txt", content)


def _checker_prompt(
    runs_dir: Path,
    feature: str,
    conversation_id: str,
    *,
    manifest: dict[str, str],
    task_spec_file: str,
    task_spec_sha256: str,
    task_spec: dict[str, Any],
    message: dict[str, Any],
    payload: bytes,
    head_sha: str,
    round_number: int,
) -> tuple[str, str]:
    evidence_json, evidence_sha256 = _checker_evidence_pack(
        manifest=manifest,
        message=message,
        payload=payload,
        head_sha=head_sha,
    )
    content = _render_template(
        "fdp2_checker.md",
        {
            "FEATURE": feature,
            "CONVERSATION_ID": conversation_id,
            "TARGET_REPO": manifest["target_repo"],
            "WORKTREE": manifest[f"{MAKER_INSTANCE}.worktree"],
            "BRANCH": manifest[f"{MAKER_INSTANCE}.branch"],
            "TASK_SPEC_FILE": task_spec_file,
            "TASK_SPEC_SHA256": task_spec_sha256,
            "TASK_SPEC_JSON": json.dumps(task_spec, indent=2, sort_keys=True),
            "MESSAGE_ID": message["message_id"],
            "HEAD_SHA": head_sha,
            "ROUND": str(round_number),
            "EVIDENCE_SHA256": evidence_sha256,
            "EVIDENCE_JSON": evidence_json,
        },
    )
    return _write_prompt(
        runs_dir,
        feature,
        conversation_id,
        f"checker-{round_number}.txt",
        content,
    )


def _checker_evidence_pack(
    *,
    manifest: dict[str, str],
    message: dict[str, Any],
    payload: bytes,
    head_sha: str,
) -> tuple[str, str]:
    source_result = _parse_result(payload, str(message["source_run_id"]))
    if not isinstance(source_result, dict):
        raise ContractError("Checker evidence source result must be an object")
    base_sha = source_result.get("base_sha")
    source_head = source_result.get("head_sha")
    if not GIT_SHA.fullmatch(str(base_sha or "")):
        raise ContractError("Checker evidence base_sha is invalid")
    if source_head != head_sha or not GIT_SHA.fullmatch(str(source_head or "")):
        raise ContractError("Checker evidence head_sha does not match the bound conversation head")
    status = _git(
        manifest,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
        worktree=True,
    )
    if status:
        raise ContractError("Checker evidence requires a clean Maker worktree")
    commit_count = _git(manifest, "rev-list", "--count", f"{base_sha}..{head_sha}")
    if commit_count != "1":
        raise ContractError("Checker evidence requires exactly one Maker commit")
    evidence = {
        "schema_version": 1,
        "integrity": {
            "message_id": message["message_id"],
            "payload_sha256": message["payload_sha256"],
            "payload_bytes": message["payload_bytes"],
            "base_sha": base_sha,
            "head_sha": head_sha,
            "branch": manifest[f"{MAKER_INSTANCE}.branch"],
            "worktree_status_porcelain": status,
            "commit_count": int(commit_count),
        },
        "message_envelope": message,
        "source_result": source_result,
        "git": {
            "commit": _git(
                manifest,
                "show",
                "--no-ext-diff",
                "--no-textconv",
                "-s",
                "--format=fuller",
                head_sha,
            ),
            "name_status": _git(
                manifest,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--name-status",
                base_sha,
                head_sha,
                "--",
            ),
            "diff": _git(
                manifest,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--binary",
                "--full-index",
                "--unified=40",
                base_sha,
                head_sha,
                "--",
            ),
        },
    }
    serialized = json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=True)
    payload_bytes = serialized.encode("utf-8")
    if len(payload_bytes) > MAX_CHECKER_EVIDENCE_BYTES:
        raise ContractError(
            f"Checker evidence exceeds {MAX_CHECKER_EVIDENCE_BYTES} bytes"
        )
    return serialized, hashlib.sha256(payload_bytes).hexdigest()


def _revision_prompt(
    runs_dir: Path,
    feature: str,
    conversation_id: str,
    *,
    manifest: dict[str, str],
    task_spec_file: str,
    task_spec_sha256: str,
    task_spec: dict[str, Any],
    message_id: str,
    base_sha: str,
    round_number: int,
) -> tuple[str, str]:
    content = _render_template(
        "fdp2_maker_revision.md",
        {
            "FEATURE": feature,
            "CONVERSATION_ID": conversation_id,
            "TARGET_REPO": manifest["target_repo"],
            "WORKTREE": manifest[f"{MAKER_INSTANCE}.worktree"],
            "BRANCH": manifest[f"{MAKER_INSTANCE}.branch"],
            "TASK_SPEC_FILE": task_spec_file,
            "TASK_SPEC_SHA256": task_spec_sha256,
            "TASK_SPEC_JSON": json.dumps(task_spec, indent=2, sort_keys=True),
            "MESSAGE_ID": message_id,
            "BASE_SHA": base_sha,
            "ROUND": str(round_number),
        },
    )
    return _write_prompt(
        runs_dir,
        feature,
        conversation_id,
        f"revision-{round_number}.txt",
        content,
    )


def _assert_recorded_rooted_path(
    rooted: fleet_safe_paths.RootedFS,
    recorded: Any,
    relative: Path,
    where: str,
) -> None:
    if not isinstance(recorded, str) or not recorded:
        raise ControllerError(f"{where} is missing")
    recorded_path = Path(recorded)
    try:
        recorded_root = recorded_path.parents[len(relative.parts) - 1]
    except IndexError as exc:
        raise ControllerError(f"{where} is outside its selected root") from exc
    if recorded_path != recorded_root / relative:
        raise ControllerError(f"{where} is outside its selected root")
    try:
        physical_root = fleet_safe_paths.canonical_root(recorded_root)
    except fleet_safe_paths.SafePathError as exc:
        raise ControllerError(f"unsafe {where} root: {exc}") from exc
    if physical_root != rooted.root:
        raise ControllerError(f"{where} is outside its selected root")


def _bound_task_spec(
    runs_dir: Path,
    feature: str,
    conversation_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    relative = _task_spec_relative(feature, conversation_id)
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            _assert_recorded_rooted_path(
                rooted,
                snapshot["task_spec_file"],
                relative,
                "durable FDP-2 task spec",
            )
            payload = rooted.read_regular(
                relative,
                directory_modes=_control_directory_modes(relative),
                file_mode=0o600,
                max_bytes=MAX_CONTROL_FILE_BYTES,
            )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise ControllerError(f"unsafe durable FDP-2 task spec: {exc}") from exc
    task_spec, _, actual_sha = _parse_task_spec_payload(payload)
    if actual_sha != snapshot["task_spec_sha256"]:
        raise ControllerError("durable FDP-2 task spec hash changed")
    return task_spec


def _strict_object(value: Any, fields: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError(f"{where} fields do not match schema_version=1")
    return value


def _nonempty_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{where} must be a non-empty string")
    return value


def _parse_result(payload: bytes, run_id: str) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("FDP-2 result is not UTF-8") from exc
    lines = text.splitlines()
    sentinel = f"FLEET_RESULT:{run_id}:DONE"
    if not lines or lines[-1] != sentinel or lines.count(sentinel) != 1:
        raise ContractError("FDP-2 result must end with one exact DONE sentinel")
    body = "\n".join(lines[:-1])
    try:
        value = fleet_json.loads(body)
    except fleet_json.FleetJSONError as exc:
        raise ContractError("FDP-2 result body is not one JSON document") from exc
    if not isinstance(value, dict):
        raise ContractError("FDP-2 result JSON must be an object")
    return value


def _evidence_list(value: Any, where: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{where} must be a non-empty evidence list")
    result: list[dict[str, str]] = []
    for index, raw in enumerate(value):
        item = _strict_object(raw, {"kind", "ref"}, f"{where}[{index}]")
        if item.get("kind") not in {"file", "test", "run", "message"}:
            raise ContractError(f"{where}[{index}].kind is invalid")
        _nonempty_string(item.get("ref"), f"{where}[{index}].ref")
        result.append({"kind": item["kind"], "ref": item["ref"]})
    return result


def _verification_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ContractError("verification must not be empty")
    values: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw in enumerate(value):
        item = _strict_object(
            raw,
            {"id", "command", "status", "evidence", "reason"},
            f"verification[{index}]",
        )
        identifier = _nonempty_string(item.get("id"), f"verification[{index}].id")
        if identifier in ids:
            raise ContractError("verification contains duplicate ids")
        ids.add(identifier)
        _nonempty_string(item.get("command"), f"verification[{index}].command")
        if item.get("status") not in {"passed", "failed", "not_applicable"}:
            raise ContractError(f"verification[{index}].status is invalid")
        reason = item.get("reason")
        if not isinstance(reason, str):
            raise ContractError(f"verification[{index}].reason must be a string")
        if item["status"] == "not_applicable" and not reason.strip():
            raise ContractError("not_applicable verification requires a reason")
        _evidence_list(item.get("evidence"), f"verification[{index}].evidence")
        values.append(item)
    return values


def _proposal_contract(value: Any) -> dict[str, Any]:
    item = _strict_object(
        value,
        {"schema_version", "summary", "base_sha", "head_sha", "changes", "verification"},
        "proposal",
    )
    if item.get("schema_version") != 1:
        raise ContractError("proposal schema_version must be 1")
    _nonempty_string(item.get("summary"), "proposal.summary")
    if not GIT_SHA.fullmatch(str(item.get("base_sha") or "")) or not GIT_SHA.fullmatch(str(item.get("head_sha") or "")):
        raise ContractError("proposal SHAs are invalid")
    changes = item.get("changes")
    if not isinstance(changes, list) or not changes or any(not isinstance(change, str) or not change.strip() for change in changes):
        raise ContractError("proposal.changes must contain non-empty strings")
    _verification_list(item.get("verification"))
    return item


def _revision_contract(value: Any, finding_ids: list[str]) -> dict[str, Any]:
    item = _strict_object(
        value,
        {
            "schema_version",
            "rebuttal",
            "revision_summary",
            "base_sha",
            "head_sha",
            "changes",
            "verification",
        },
        "revision",
    )
    if item.get("schema_version") != 1:
        raise ContractError("revision schema_version must be 1")
    _nonempty_string(item.get("revision_summary"), "revision.revision_summary")
    if not GIT_SHA.fullmatch(str(item.get("base_sha") or "")) or not GIT_SHA.fullmatch(str(item.get("head_sha") or "")):
        raise ContractError("revision SHAs are invalid")
    changes = item.get("changes")
    if not isinstance(changes, list) or not changes or any(not isinstance(change, str) or not change.strip() for change in changes):
        raise ContractError("revision.changes must contain non-empty strings")
    verifications = _verification_list(item.get("verification"))
    rebuttal = item.get("rebuttal")
    if not isinstance(rebuttal, list):
        raise ContractError("revision.rebuttal must be a list")
    seen: set[str] = set()
    verification_ids = [entry["id"] for entry in verifications]
    for index, raw in enumerate(rebuttal):
        entry = _strict_object(
            raw,
            {"finding_id", "disposition", "reason", "evidence"},
            f"rebuttal[{index}]",
        )
        finding_id = _nonempty_string(entry.get("finding_id"), f"rebuttal[{index}].finding_id")
        if finding_id in seen or finding_id not in finding_ids:
            raise ContractError("revision rebuttal has duplicate or unknown finding_id")
        seen.add(finding_id)
        if entry.get("disposition") not in {"accepted", "disputed"}:
            raise ContractError("revision rebuttal disposition is invalid")
        _nonempty_string(entry.get("reason"), f"rebuttal[{index}].reason")
        evidence = _evidence_list(entry.get("evidence"), f"rebuttal[{index}].evidence")
        for reference in evidence:
            if reference["kind"] == "test" and reference["ref"] not in verification_ids:
                raise ContractError("rebuttal references an unknown verification id")
    if seen != set(finding_ids):
        raise ContractError("revision rebuttal must cover every Checker finding")
    return item


def _checker_contract(value: Any) -> dict[str, Any]:
    item = _strict_object(
        value,
        {"schema_version", "verdict", "summary", "findings"},
        "checker",
    )
    if item.get("schema_version") != 1:
        raise ContractError("checker schema_version must be 1")
    verdict = item.get("verdict")
    if verdict not in {"ACCEPT", "REVISE", "REJECT"}:
        raise ContractError("checker verdict is invalid")
    _nonempty_string(item.get("summary"), "checker.summary")
    findings = item.get("findings")
    if not isinstance(findings, list):
        raise ContractError("checker.findings must be a list")
    ids: set[str] = set()
    has_critical = False
    for index, raw in enumerate(findings):
        finding = _strict_object(
            raw,
            {"id", "severity", "description", "evidence"},
            f"findings[{index}]",
        )
        identifier = _nonempty_string(finding.get("id"), f"findings[{index}].id")
        if identifier in ids:
            raise ContractError("checker findings contain duplicate ids")
        ids.add(identifier)
        severity = finding.get("severity")
        if severity not in {"low", "medium", "high", "critical"}:
            raise ContractError("checker finding severity is invalid")
        has_critical = has_critical or severity == "critical"
        _nonempty_string(finding.get("description"), f"findings[{index}].description")
        _evidence_list(finding.get("evidence"), f"findings[{index}].evidence")
    if verdict == "ACCEPT" and findings:
        raise ContractError("ACCEPT requires zero findings")
    if verdict == "REVISE" and (not findings or has_critical):
        raise ContractError("REVISE requires non-critical findings")
    if verdict == "REJECT" and not has_critical:
        raise ContractError("REJECT requires a critical finding")
    return item


def _file_reference(value: str) -> tuple[str, int | None]:
    match = re.fullmatch(r"(.+?)(?::([1-9][0-9]*))?", value)
    if not match:
        raise ContractError("file evidence reference is invalid")
    path_text = match.group(1)
    path = PurePosixPath(path_text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError("file evidence must be a safe repository-relative path")
    return path.as_posix(), int(match.group(2)) if match.group(2) else None


def _validate_evidence_refs(
    evidence: list[dict[str, str]],
    *,
    manifest: dict[str, str],
    head_sha: str,
    run_ids: set[str],
    message_ids: set[str],
    verification_ids: set[str],
) -> None:
    for reference in evidence:
        kind = reference["kind"]
        value = reference["ref"]
        if kind == "file":
            path, line_number = _file_reference(value)
            try:
                blob = _git_bytes(manifest, "show", f"{head_sha}:{path}")
            except ContractError:
                raise ContractError(f"file evidence does not exist at accepted head: {path}")
            if line_number is not None and line_number > len(blob.splitlines()):
                raise ContractError(f"file evidence line is outside the blob: {value}")
        elif kind == "run" and value not in run_ids:
            raise ContractError("run evidence does not belong to this conversation")
        elif kind == "message" and value not in message_ids:
            raise ContractError("message evidence does not belong to this conversation")
        elif kind == "test" and value not in verification_ids:
            raise ContractError("test evidence references an unknown verification id")


def _contract_evidence(value: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for verification in value.get("verification", []):
        result.extend(verification["evidence"])
    for rebuttal in value.get("rebuttal", []):
        result.extend(rebuttal["evidence"])
    for finding in value.get("findings", []):
        result.extend(finding["evidence"])
    return result


def _bound_ids(events: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    run_ids: set[str] = set()
    message_ids: set[str] = set()
    for event in events:
        expected = event["snapshot"].get("expected")
        if isinstance(expected, dict) and expected.get("type") == "message":
            run_ids.add(expected["source_run_id"])
        message_id = event["snapshot"].get("last_message_id")
        if isinstance(message_id, str):
            message_ids.add(message_id)
    return run_ids, message_ids


def start(
    runs_dir: Path,
    *,
    feature: str,
    idempotency_key: str,
    spec_file: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    feature = _feature(feature)
    task_spec, task_spec_payload, task_spec_sha256 = _read_task_spec(spec_file)
    request = {
        "command": "start",
        "feature": feature,
        "task_spec_sha256": task_spec_sha256,
    }
    current_time = now or utc_now()
    with coordinator(runs_dir) as lease_store:
        events = load_events(runs_dir, feature)
        _expire_locked(runs_dir, feature, events, now=current_time)
        existing = _idempotent_event(events, idempotency_key, request)
        if existing is not None:
            _verify_live_control_artifacts(runs_dir, feature, events)
            return existing
        if _active_event(events) is not None:
            raise ControllerConflict("feature already has an active FDP-2 conversation")
        manifest = _load_live_manifest(runs_dir, feature)
        _validate_roster(manifest)
        _state_phase(runs_dir, feature, manifest)
        if _lease_exists(lease_store, f"{feature}.closing"):
            raise ControllerConflict(f"fleet '{feature}' is closing")
        for instance in (MAKER_INSTANCE, CHECKER_INSTANCE):
            if _lease_exists(lease_store, f"{feature}.{instance}.lock"):
                raise ControllerConflict(f"FDP-2 participant is busy: {instance}")
        head_sha = _clean_writer_head(manifest)
        conversation_id = str(uuid.uuid4())
        task_spec_file = _write_task_spec(
            runs_dir,
            feature,
            conversation_id,
            task_spec_payload,
        )
        prompt_file, prompt_sha256 = _proposal_prompt(
            runs_dir,
            feature,
            conversation_id,
            manifest,
            head_sha,
            task_spec_file,
            task_spec_sha256,
            task_spec,
        )
        created_at = timestamp(current_time)
        snapshot = {
            "status": "awaiting_proposal_run",
            "created_at": created_at,
            "deadline_at": timestamp(current_time + timedelta(seconds=DIALOGUE_DEADLINE_SECONDS)),
            "max_revision_rounds": MAX_REVISION_ROUNDS,
            "run_timeout_seconds": RUN_TIMEOUT_SECONDS,
            "revision_round": 0,
            "maker_instance": MAKER_INSTANCE,
            "checker_instance": CHECKER_INSTANCE,
            "task_spec_file": task_spec_file,
            "task_spec_sha256": task_spec_sha256,
            "start_head_sha": head_sha,
            "current_head_sha": head_sha,
            "accepted_head_sha": None,
            "last_message_id": None,
            "maker_verification_ids": [],
            "open_finding_ids": [],
            "expected": _run_expected(
                "proposal", MAKER_INSTANCE, prompt_file, prompt_sha256, None
            ),
            "terminal_reason": None,
        }
        return _append_event_locked(
            runs_dir,
            feature,
            events,
            conversation_id=conversation_id,
            event_type="conversation_started",
            idempotency_key=idempotency_key,
            request=request,
            snapshot=snapshot,
            now=current_time,
        )


def _run_terminal(
    runs_dir: Path,
    feature: str,
    run_id: str,
    expected: dict[str, Any],
    snapshot: dict[str, Any],
    manifest: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    instance = expected["instance"]
    try:
        events = events_for_run(
            runs_dir / f"fleet-{feature}.ledger.jsonl",
            run_id=run_id,
            instance=instance,
            runs_dir=runs_dir,
        )
    except LedgerError as exc:
        raise ControllerError(f"cannot read lifecycle ledger: {exc}") from exc
    if not events:
        raise ControllerError("run_id is absent from the lifecycle ledger")
    for event in events:
        if event.get("feature") != feature or event.get("instance") != instance:
            raise ControllerError("run identity does not match the expected participant")
        if event.get("role") != manifest.get(f"{instance}.role_type") or event.get("phase") != "BUILD":
            raise ControllerError("run role/phase does not match the FDP-2 roster")
        if event.get("task_sha256") != expected["prompt_sha256"]:
            raise ControllerError("run task hash does not match the durable FDP-2 prompt")
    terminal_events = [event for event in events if event.get("status") in LIFECYCLE_TERMINALS]
    if not terminal_events:
        raise ControllerError("run is not terminal")
    if len(terminal_events) != 1:
        raise ControllerError("run has ambiguous terminal lifecycle evidence")
    first_at = min(parse_timestamp(event.get("timestamp"), "run.timestamp") for event in events)
    terminal = terminal_events[0]
    terminal_at = parse_timestamp(terminal.get("timestamp"), "run.terminal.timestamp")
    if terminal is not events[-1] or terminal_at < first_at:
        raise ControllerError("run lifecycle ordering is invalid")
    if first_at < parse_timestamp(snapshot["created_at"], "conversation.created_at"):
        raise ControllerError("run predates the FDP-2 conversation")
    return terminal, events, {
        "started_at": first_at,
        "terminal_at": terminal_at,
    }


def _ingest_run_locked(
    runs_dir: Path,
    feature: str,
    controller_events: list[dict[str, Any]],
    current: dict[str, Any],
    run_id: str,
    manifest: dict[str, str],
) -> tuple[dict[str, Any], str]:
    snapshot = current["snapshot"]
    expected = snapshot["expected"]
    if expected.get("type") != "run":
        raise ControllerError("dialogue is not awaiting a run")
    bound_runs, bound_messages = _bound_ids(controller_events)
    if run_id in bound_runs:
        raise ControllerConflict("run_id is already bound to FDP-2")
    terminal, _, times = _run_terminal(
        runs_dir, feature, run_id, expected, snapshot, manifest
    )
    status = str(terminal.get("status"))
    if times["terminal_at"] - times["started_at"] > timedelta(seconds=snapshot["run_timeout_seconds"]):
        return _terminal(snapshot, "indeterminate", f"run_timeout_exceeded:{run_id}"), "run_timed_out"
    if times["terminal_at"] > parse_timestamp(snapshot["deadline_at"], "deadline"):
        return _terminal(snapshot, "indeterminate", f"run_after_dialogue_deadline:{run_id}"), "run_after_deadline"
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
            runs_dir, feature, expected["instance"], run_id
        )
        value = _parse_result(payload, run_id)
        next_snapshot = copy.deepcopy(snapshot)
        stage = expected["stage"]
        if stage == "proposal":
            contract = _proposal_contract(value)
            if contract["base_sha"] != snapshot["current_head_sha"]:
                raise ContractError("proposal base_sha does not match conversation head")
            _validate_maker_commit(
                manifest,
                base_sha=contract["base_sha"],
                head_sha=contract["head_sha"],
            )
            next_snapshot["current_head_sha"] = contract["head_sha"]
            next_snapshot["maker_verification_ids"] = [
                entry["id"] for entry in contract["verification"]
            ]
            next_snapshot["status"] = "awaiting_proposal_message"
            next_snapshot["expected"] = _message_expected(
                "proposal",
                "proposal",
                CHECKER_INSTANCE,
                MAKER_INSTANCE,
                run_id,
                expected["reply_to"],
                digest(payload),
            )
        elif stage == "revision":
            contract = _revision_contract(value, snapshot["open_finding_ids"])
            if contract["base_sha"] != snapshot["current_head_sha"]:
                raise ContractError("revision base_sha does not match conversation head")
            _validate_maker_commit(
                manifest,
                base_sha=contract["base_sha"],
                head_sha=contract["head_sha"],
            )
            next_snapshot["current_head_sha"] = contract["head_sha"]
            next_snapshot["maker_verification_ids"] = [
                entry["id"] for entry in contract["verification"]
            ]
            next_snapshot["open_finding_ids"] = []
            next_snapshot["status"] = "awaiting_rebuttal_message"
            next_snapshot["expected"] = _message_expected(
                "rebuttal",
                "rebuttal",
                CHECKER_INSTANCE,
                MAKER_INSTANCE,
                run_id,
                expected["reply_to"],
                digest(payload),
            )
        elif stage == "checker":
            contract = _checker_contract(value)
            _validate_evidence_refs(
                _contract_evidence(contract),
                manifest=manifest,
                head_sha=snapshot["current_head_sha"],
                run_ids=bound_runs | {run_id},
                message_ids=bound_messages,
                verification_ids=set(snapshot["maker_verification_ids"]),
            )
            next_snapshot["status"] = "awaiting_checker_message"
            next_snapshot["expected"] = _message_expected(
                "checker",
                "challenge",
                MAKER_INSTANCE,
                CHECKER_INSTANCE,
                run_id,
                expected["reply_to"],
                digest(payload),
            )
        else:
            raise ControllerError(f"unsupported expected run stage: {stage}")
        _validate_evidence_refs(
            _contract_evidence(contract),
            manifest=manifest,
            head_sha=next_snapshot["current_head_sha"],
            run_ids=bound_runs | {run_id},
            message_ids=bound_messages,
            verification_ids=set(next_snapshot["maker_verification_ids"]),
        )
        return next_snapshot, "run_ingested"
    except ContractError as exc:
        return _terminal(snapshot, "indeterminate", f"invalid_{expected['stage']}_contract:{exc}"), "contract_rejected"


def _message_by_id(
    runs_dir: Path, feature: str, message_id: str
) -> tuple[dict[str, Any], bytes]:
    messages = fleet_dialogue.load_messages(runs_dir, feature)
    message = next((item for item in messages if item["message_id"] == message_id), None)
    if message is None:
        raise ControllerError("message_id is absent from the FDP-1 dialogue ledger")
    _, payload = fleet_dialogue._verified_payload(runs_dir, feature, message)
    return message, payload


def _ingest_message_locked(
    runs_dir: Path,
    feature: str,
    controller_events: list[dict[str, Any]],
    current: dict[str, Any],
    message_id: str,
    manifest: dict[str, str],
) -> tuple[dict[str, Any], str]:
    snapshot = current["snapshot"]
    expected = snapshot["expected"]
    if expected.get("type") != "message":
        raise ControllerError("dialogue is not awaiting a message")
    _, bound_messages = _bound_ids(controller_events)
    if message_id in bound_messages:
        raise ControllerConflict("message_id is already bound to FDP-2")
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
            raise ControllerError(f"FDP-1 message does not match expected {field}")
    next_snapshot = copy.deepcopy(snapshot)
    next_snapshot["last_message_id"] = message_id
    stage = expected["stage"]
    if stage == "proposal":
        task_spec = _bound_task_spec(
            runs_dir, feature, current["conversation_id"], snapshot
        )
        prompt_file, prompt_sha = _checker_prompt(
            runs_dir,
            feature,
            current["conversation_id"],
            manifest=manifest,
            task_spec_file=snapshot["task_spec_file"],
            task_spec_sha256=snapshot["task_spec_sha256"],
            task_spec=task_spec,
            message=message,
            payload=payload,
            head_sha=snapshot["current_head_sha"],
            round_number=snapshot["revision_round"],
        )
        next_snapshot["status"] = "awaiting_checker_run"
        next_snapshot["expected"] = _run_expected(
            "checker", CHECKER_INSTANCE, prompt_file, prompt_sha, message_id
        )
        return next_snapshot, "proposal_published"
    if stage == "rebuttal":
        next_snapshot["status"] = "awaiting_revision_message"
        next_snapshot["expected"] = _message_expected(
            "revision",
            "revision",
            CHECKER_INSTANCE,
            MAKER_INSTANCE,
            expected["source_run_id"],
            message_id,
            expected["payload_sha256"],
        )
        return next_snapshot, "rebuttal_published"
    if stage == "revision":
        task_spec = _bound_task_spec(
            runs_dir, feature, current["conversation_id"], snapshot
        )
        prompt_file, prompt_sha = _checker_prompt(
            runs_dir,
            feature,
            current["conversation_id"],
            manifest=manifest,
            task_spec_file=snapshot["task_spec_file"],
            task_spec_sha256=snapshot["task_spec_sha256"],
            task_spec=task_spec,
            message=message,
            payload=payload,
            head_sha=snapshot["current_head_sha"],
            round_number=snapshot["revision_round"],
        )
        next_snapshot["status"] = "awaiting_checker_run"
        next_snapshot["expected"] = _run_expected(
            "checker", CHECKER_INSTANCE, prompt_file, prompt_sha, message_id
        )
        return next_snapshot, "revision_published"
    if stage != "checker":
        raise ControllerError(f"unsupported expected message stage: {stage}")
    try:
        contract = _checker_contract(_parse_result(payload, expected["source_run_id"]))
    except ContractError as exc:
        return _terminal(snapshot, "indeterminate", f"invalid_checker_publication:{exc}"), "contract_rejected"
    verdict = contract["verdict"]
    if verdict == "ACCEPT":
        return _terminal(next_snapshot, "accepted", "checker_accept"), "conversation_accepted"
    if verdict == "REJECT":
        return _terminal(next_snapshot, "rejected", "checker_reject"), "conversation_rejected"
    if snapshot["revision_round"] >= snapshot["max_revision_rounds"]:
        return _terminal(next_snapshot, "indeterminate", "max_revision_rounds_exhausted"), "rounds_exhausted"
    round_number = snapshot["revision_round"] + 1
    task_spec = _bound_task_spec(
        runs_dir, feature, current["conversation_id"], snapshot
    )
    prompt_file, prompt_sha = _revision_prompt(
        runs_dir,
        feature,
        current["conversation_id"],
        manifest=manifest,
        task_spec_file=snapshot["task_spec_file"],
        task_spec_sha256=snapshot["task_spec_sha256"],
        task_spec=task_spec,
        message_id=message_id,
        base_sha=snapshot["current_head_sha"],
        round_number=round_number,
    )
    next_snapshot["revision_round"] = round_number
    next_snapshot["open_finding_ids"] = [finding["id"] for finding in contract["findings"]]
    next_snapshot["status"] = "awaiting_revision_run"
    next_snapshot["expected"] = _run_expected(
        "revision", MAKER_INSTANCE, prompt_file, prompt_sha, message_id
    )
    return next_snapshot, "revision_round_opened"


def step(
    runs_dir: Path,
    *,
    feature: str,
    idempotency_key: str,
    run_id: str | None = None,
    message_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    feature = _feature(feature)
    if (run_id is None) == (message_id is None):
        raise ControllerError("step requires exactly one of run_id or message_id")
    request = {
        "command": "step",
        "run_id": run_id,
        "message_id": message_id,
    }
    current_time = now or utc_now()
    with coordinator(runs_dir) as lease_store:
        events = load_events(runs_dir, feature)
        _expire_locked(runs_dir, feature, events, now=current_time)
        existing = _idempotent_event(events, idempotency_key, request)
        if existing is not None:
            _verify_live_control_artifacts(runs_dir, feature, events)
            return existing
        current = _current_event(events)
        if current is None or current["snapshot"]["status"] in TERMINAL_STATES:
            raise ControllerConflict("feature has no active FDP-2 conversation")
        manifest = _load_live_manifest(runs_dir, feature)
        _validate_roster(manifest)
        if _lease_exists(lease_store, f"{feature}.closing"):
            raise ControllerConflict(f"fleet '{feature}' is closing")
        if run_id is not None:
            next_snapshot, event_type = _ingest_run_locked(
                runs_dir, feature, events, current, run_id, manifest
            )
        else:
            next_snapshot, event_type = _ingest_message_locked(
                runs_dir, feature, events, current, str(message_id), manifest
            )
        return _append_event_locked(
            runs_dir,
            feature,
            events,
            conversation_id=current["conversation_id"],
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
    feature = _feature(feature)
    _nonempty_string(reason, "abandon.reason")
    request = {"command": "abandon", "reason": reason}
    current_time = now or utc_now()
    with coordinator(runs_dir):
        events = load_events(runs_dir, feature)
        _expire_locked(runs_dir, feature, events, now=current_time)
        existing = _idempotent_event(events, idempotency_key, request)
        if existing is not None:
            return existing
        active = _active_event(events)
        if active is None:
            raise ControllerConflict("feature has no active FDP-2 conversation")
        return _append_event_locked(
            runs_dir,
            feature,
            events,
            conversation_id=active["conversation_id"],
            event_type="conversation_abandoned",
            idempotency_key=idempotency_key,
            request=request,
            snapshot=_terminal(active["snapshot"], "abandoned", f"operator_abandoned:{reason}"),
            now=current_time,
        )


def show(
    runs_dir: Path,
    *,
    feature: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    feature = _feature(feature)
    with coordinator(runs_dir):
        events = load_events(runs_dir, feature)
        _expire_locked(runs_dir, feature, events, now=now)
        current = _current_event(events)
        if current is None:
            raise ControllerError("feature has no FDP-2 conversation")
        _verify_live_control_artifacts(runs_dir, feature, events)
        return current


def _action(event: dict[str, Any]) -> dict[str, Any]:
    snapshot = event["snapshot"]
    expected = snapshot["expected"]
    if expected is None:
        return {"action": "terminal", "status": snapshot["status"], "reason": snapshot["terminal_reason"]}
    if expected["type"] == "run":
        return {
            "action": "dispatch",
            "instance": expected["instance"],
            "stage": expected["stage"],
            "prompt_file": expected["prompt_file"],
            "prompt_sha256": expected["prompt_sha256"],
            "timeout_seconds": snapshot["run_timeout_seconds"],
        }
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


def public_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversation_id": event["conversation_id"],
        "sequence": event["sequence"],
        "event_type": event["event_type"],
        "event_sha256": event["event_sha256"],
        "snapshot": event["snapshot"],
        "next_action": _action(event),
    }


def accepted_build_gate(runs_dir: Path, feature: str, manifest: dict[str, str]) -> str:
    feature = _feature(feature)
    with coordinator(runs_dir):
        live_manifest = _load_live_manifest(runs_dir, feature)
        _validate_roster(live_manifest)
        if manifest != live_manifest:
            raise ControllerError("caller manifest differs from the rooted live manifest")
        events = load_events(runs_dir, feature)
        if not events:
            raise ControllerError("FDP-2 BUILD gate requires a conversation")
        latest = events[-1]
        snapshot = latest["snapshot"]
        if snapshot["status"] != "accepted":
            raise ControllerError(
                f"FDP-2 BUILD gate requires latest status accepted, got {snapshot['status']}"
            )
        messages = fleet_dialogue.load_messages(runs_dir, feature)
        fleet_dialogue.verify_storage(
            fleet_dialogue.ledger_path(runs_dir, feature),
            fleet_dialogue.store_path(runs_dir, feature),
            feature=feature,
            instances=_manifest_instances(live_manifest),
        )
        _verify_live_control_artifacts(runs_dir, feature, events)
        _verify_message_bindings(events, messages)
        head = _clean_writer_head(live_manifest)
        if head != snapshot["accepted_head_sha"]:
            raise ControllerError("maker branch drifted after FDP-2 acceptance")
        return head


def _manifest_instances(manifest: dict[str, str]) -> set[str]:
    return {
        key
        for key, value in manifest.items()
        if value.startswith("surface:") and manifest.get(f"{key}.uuid")
    }


def _verify_message_bindings(
    events: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> None:
    bound = {
        event["snapshot"]["last_message_id"]
        for event in events
        if event["snapshot"].get("last_message_id") is not None
    }
    # FDP-1 is shared plumbing. FDP-2 owns only Maker/Checker publications;
    # later assurance slices bind their own Challenge/Verify messages.
    actual = {
        message["message_id"]
        for message in messages
        if message.get("source_instance") in {MAKER_INSTANCE, CHECKER_INSTANCE}
    }
    if bound != actual:
        raise ControllerError("FDP-2 controller/message bindings are incomplete")


def _control_artifact_expectations(
    events: list[dict[str, Any]],
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    immutable: dict[str, tuple[str, str]] = {}
    for event in events:
        conversation_id = event["conversation_id"]
        snapshot = event["snapshot"]
        identity = (snapshot["task_spec_file"], snapshot["task_spec_sha256"])
        previous = immutable.setdefault(conversation_id, identity)
        if previous != identity:
            raise ControllerError("FDP-2 task spec binding changed during a conversation")
        task_logical = f"dialogue/control/{conversation_id}/task-spec.json"
        artifacts[task_logical] = snapshot["task_spec_sha256"]

        expected = snapshot.get("expected")
        if not isinstance(expected, dict) or expected.get("type") != "run":
            continue
        stage = expected["stage"]
        round_number = snapshot["revision_round"]
        names = {
            "proposal": "proposal.txt",
            "checker": f"checker-{round_number}.txt",
            "revision": f"revision-{round_number}.txt",
        }
        expected_name = names.get(stage)
        if expected_name is None or Path(expected["prompt_file"]).name != expected_name:
            raise ControllerError("FDP-2 prompt binding has an invalid durable name")
        logical = f"dialogue/control/{conversation_id}/prompts/{expected_name}"
        prior_hash = artifacts.setdefault(logical, expected["prompt_sha256"])
        if prior_hash != expected["prompt_sha256"]:
            raise ControllerError("FDP-2 prompt binding changed after creation")
    return artifacts


def _verify_live_control_artifacts(
    runs_dir: Path, feature: str, events: list[dict[str, Any]]
) -> None:
    feature = _feature(feature)
    artifacts = _control_artifact_expectations(events)
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            for logical, expected_sha in artifacts.items():
                archived_relative = PurePosixPath(logical).relative_to("dialogue")
                relative = Path("dialogue") / feature / Path(*archived_relative.parts)
                payload = rooted.read_regular(
                    relative,
                    directory_modes=_control_directory_modes(relative),
                    file_mode=0o600,
                    max_bytes=MAX_CONTROL_FILE_BYTES,
                )
                if hashlib.sha256(payload).hexdigest() != expected_sha:
                    raise ControllerError(
                        f"FDP-2 durable control artifact hash mismatch: {logical}"
                    )

            for event in events:
                conversation_id = event["conversation_id"]
                snapshot = event["snapshot"]
                task_relative = _task_spec_relative(feature, conversation_id)
                _assert_recorded_rooted_path(
                    rooted,
                    snapshot["task_spec_file"],
                    task_relative,
                    "FDP-2 task spec",
                )
                expected = snapshot.get("expected")
                if isinstance(expected, dict) and expected.get("type") == "run":
                    prompt_relative = _prompt_relative(
                        feature,
                        conversation_id,
                        Path(expected["prompt_file"]).name,
                    )
                    _assert_recorded_rooted_path(
                        rooted,
                        expected["prompt_file"],
                        prompt_relative,
                        "FDP-2 prompt",
                    )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise ControllerError(f"unsafe live FDP-2 control artifact: {exc}") from exc


def _verify_archived_control_artifacts(
    archive: Path, events: list[dict[str, Any]]
) -> None:
    try:
        with fleet_safe_paths.RootedFS(archive) as rooted:
            for logical, expected_sha in _control_artifact_expectations(events).items():
                relative = Path(*PurePosixPath(logical).parts)
                directory_modes = (0o755, 0o755, 0o700)
                if relative.parts[-2:-1] == ("prompts",):
                    directory_modes = (*directory_modes, 0o700)
                payload = rooted.read_regular(
                    relative,
                    directory_modes=directory_modes,
                    file_mode=0o600,
                    max_bytes=MAX_CONTROL_FILE_BYTES,
                )
                if hashlib.sha256(payload).hexdigest() != expected_sha:
                    raise ControllerError(
                        f"archived FDP-2 control artifact hash mismatch: {logical}"
                    )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise ControllerError(f"unsafe archived FDP-2 control artifact: {exc}") from exc


def _manifest_identity_sha256(manifest: dict[str, str]) -> str:
    mutable = {"workspace.handoff_state", "workspace.quiesced"}
    projection = {
        key: value
        for key, value in manifest.items()
        if key not in mutable
        and not key.endswith((".final_sha", ".published_sha", ".publication_state"))
    }
    return digest(projection)


def _control_summary(
    events: list[dict[str, Any]],
    dialogue_summary: dict[str, int],
    manifest: dict[str, str],
) -> dict[str, Any]:
    return {
        "events": len(events),
        "conversations": len({event["conversation_id"] for event in events}),
        "latest_status": events[-1]["snapshot"]["status"] if events else None,
        "dialogue_messages": dialogue_summary["messages"],
        "payloads": dialogue_summary["payloads"],
        "payload_bytes": dialogue_summary["payload_bytes"],
        "control_head_sha256": events[-1]["event_sha256"] if events else None,
        "manifest_identity_sha256": _manifest_identity_sha256(manifest),
    }


def _verify_live_locked(
    runs_dir: Path,
    feature: str,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    events = load_events(runs_dir, feature)
    _expire_locked(runs_dir, feature, events, now=now)
    manifest = _load_live_manifest(runs_dir, feature)
    _validate_roster(manifest)
    messages = fleet_dialogue.load_messages(runs_dir, feature)
    dialogue_summary = fleet_dialogue.verify_storage(
        fleet_dialogue.ledger_path(runs_dir, feature),
        fleet_dialogue.store_path(runs_dir, feature),
        feature=feature,
        instances=_manifest_instances(manifest),
    )
    _verify_live_control_artifacts(runs_dir, feature, events)
    _verify_message_bindings(events, messages)
    summary = {
        "feature": feature,
        **_control_summary(events, dialogue_summary, manifest),
    }
    return summary, events, messages


def verify_live(
    runs_dir: Path,
    *,
    feature: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    feature = _feature(feature)
    with coordinator(runs_dir):
        summary, _, _ = _verify_live_locked(runs_dir, feature, now=now)
        return summary


def _file_hash(payload: bytes) -> dict[str, Any]:
    return {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}


def _regular_file_hash(path: Path) -> dict[str, Any]:
    """Compatibility leaf reader for FDP-3 callers with an already selected parent."""
    path = Path(path)
    if path != path.parent / path.name:
        raise ControllerError("verification receipt path is unsafe")
    return _file_hash(
        _read_rooted_regular(
            path.parent,
            path.name,
            directory_modes=(),
            max_bytes=MAX_CONTROL_FILE_BYTES,
            where="verification receipt source",
        )
    )


def _write_atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Compatibility writer for a caller-authorized receipt parent directory."""
    path = Path(path)
    if (
        path != path.parent / path.name
        or not SAFE_CONTROL_FILE.fullmatch(path.name)
    ):
        raise ControllerError("verification receipt path is unsafe")
    try:
        payload = fleet_json.canonical_bytes(value) + b"\n"
    except fleet_json.FleetJSONError as exc:
        raise ControllerError("verification receipt is not strict JSON") from exc
    if len(payload) > MAX_RECEIPT_BYTES:
        raise ControllerError(
            f"verification receipt exceeds {MAX_RECEIPT_BYTES} bytes"
        )
    try:
        with fleet_safe_paths.RootedFS(path.parent) as rooted:
            rooted.replace_regular(
                path.name,
                payload,
                directory_modes=(),
                file_mode=0o600,
            )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise ControllerError(f"unsafe verification receipt path: {exc}") from exc


def _validate_receipt_lifecycle(
    records: list[dict[str, Any]],
    feature: str,
    events: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> None:
    bindings: dict[str, str] = {}
    for event in events:
        expected = event["snapshot"].get("expected")
        if isinstance(expected, dict) and expected.get("type") == "message":
            run_id = str(expected["source_run_id"])
            instance = str(expected["source_instance"])
            previous = bindings.setdefault(run_id, instance)
            if previous != instance:
                raise ControllerError("FDP-2 run identity binding is ambiguous")
    for message in messages:
        run_id = str(message["source_run_id"])
        instance = str(message["source_instance"])
        previous = bindings.setdefault(run_id, instance)
        if previous != instance:
            raise ControllerError("FDP-2 message run identity binding is ambiguous")
    for run_id, instance in bindings.items():
        run_records = [record for record in records if record.get("run_id") == run_id]
        if not run_records:
            raise ControllerError("FDP-2 receipt lacks lifecycle evidence for a bound run")
        if any(
            record.get("feature") != feature or record.get("instance") != instance
            for record in run_records
        ):
            raise ControllerError("FDP-2 receipt lifecycle run identity changed")
        terminals = [
            record
            for record in run_records
            if record.get("status") in LIFECYCLE_TERMINALS
        ]
        if (
            len(terminals) != 1
            or terminals[0] is not run_records[-1]
            or terminals[0].get("status") != "succeeded"
        ):
            raise ControllerError("FDP-2 receipt lacks one terminal successful bound run")


def _receipt_file_hashes(
    root: Path,
    feature: str,
    events: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    *,
    archived: bool,
) -> dict[str, dict[str, Any]]:
    feature = _feature(feature)
    files: dict[str, dict[str, Any]] = {}
    artifacts = _control_artifact_expectations(events)
    digests = sorted({str(message["payload_sha256"]) for message in messages})
    conversation_ids = sorted({event["conversation_id"] for event in events})
    prompt_names: dict[str, set[str]] = {item: set() for item in conversation_ids}
    for logical in artifacts:
        parts = PurePosixPath(logical).parts
        if len(parts) == 5 and parts[3] == "prompts":
            prompt_names[parts[2]].add(parts[4])

    try:
        with fleet_safe_paths.RootedFS(root) as rooted:
            control_leaf = (
                "dialogue-control.jsonl"
                if archived
                else f"fleet-{feature}.dialogue-control.jsonl"
            )
            files["dialogue-control.jsonl"] = _file_hash(
                rooted.read_regular(
                    control_leaf,
                    directory_modes=(),
                    file_mode=0o600,
                    max_bytes=MAX_CONTROL_LEDGER_BYTES,
                )
            )

            dialogue_leaf = (
                "dialogue.jsonl" if archived else f"fleet-{feature}.dialogue.jsonl"
            )
            dialogue_payload = rooted.read_regular_optional(
                dialogue_leaf,
                directory_modes=(),
                file_mode=0o600,
                max_bytes=MAX_CONTROL_LEDGER_BYTES,
            )
            if dialogue_payload is not None:
                files["dialogue.jsonl"] = _file_hash(dialogue_payload)
            elif messages:
                raise ControllerError(
                    "FDP-2 dialogue ledger is missing while messages exist"
                )

            lifecycle_leaf = "ledger.jsonl" if archived else f"fleet-{feature}.ledger.jsonl"
            lifecycle_payload = rooted.read_regular_optional(
                lifecycle_leaf,
                directory_modes=(),
                file_mode=0o600,
                max_bytes=MAX_CONTROL_LEDGER_BYTES,
            )
            lifecycle_required = bool(messages) or any(
                event["event_type"]
                in {"run_ingested", "run_terminal", "run_timed_out", "run_after_deadline"}
                for event in events
            )
            if lifecycle_payload is not None:
                try:
                    lifecycle_records = read_records(
                        root / lifecycle_leaf, runs_dir=root
                    )
                except LedgerError as exc:
                    raise ControllerError(
                        f"cannot validate FDP-2 receipt lifecycle ledger: {exc}"
                    ) from exc
                if lifecycle_required and not lifecycle_records:
                    raise ControllerError(
                        "FDP-2 lifecycle ledger is empty while run evidence exists"
                    )
                _validate_receipt_lifecycle(
                    lifecycle_records, feature, events, messages
                )
                files["ledger.jsonl"] = _file_hash(lifecycle_payload)
            elif lifecycle_required:
                raise ControllerError(
                    "FDP-2 lifecycle ledger is missing while run evidence exists"
                )

            for logical in sorted(artifacts):
                if archived:
                    relative = Path(*PurePosixPath(logical).parts)
                    modes: tuple[int, ...] = (0o755, 0o755, 0o700)
                    if relative.parts[-2:-1] == ("prompts",):
                        modes = (*modes, 0o700)
                else:
                    suffix = PurePosixPath(logical).relative_to("dialogue")
                    relative = Path("dialogue") / feature / Path(*suffix.parts)
                    modes = _control_directory_modes(relative)
                files[logical] = _file_hash(
                    rooted.read_regular(
                        relative,
                        directory_modes=modes,
                        file_mode=0o600,
                        max_bytes=MAX_CONTROL_FILE_BYTES,
                    )
                )

            payload_root = (
                Path("dialogue") / "payloads"
                if archived
                else Path("dialogue") / feature / "payloads"
            )
            payload_modes = (0o755, 0o700) if archived else (0o755, 0o755, 0o700)
            for payload_sha in digests:
                files[f"dialogue/payloads/{payload_sha}"] = _file_hash(
                    rooted.read_regular(
                        payload_root / payload_sha,
                        directory_modes=payload_modes,
                        file_mode=0o600,
                        max_bytes=fleet_dialogue.MAX_PAYLOAD_BYTES,
                    )
                )

            base = Path("dialogue") if archived else Path("dialogue") / feature
            base_modes = (0o755,) if archived else (0o755, 0o755)
            expected_base = {"control"}
            if digests:
                expected_base.add("payloads")
            actual_base = set(rooted.list_directory(base, directory_modes=base_modes))
            if not digests and "payloads" in actual_base:
                raise ControllerError(
                    "FDP-2 dialogue ledger is missing while published payloads exist"
                )
            if actual_base != expected_base:
                raise ControllerError("FDP-2 dialogue store contains unexpected entries")

            control = base / "control"
            control_modes = (*base_modes, 0o755)
            if set(rooted.list_directory(control, directory_modes=control_modes)) != set(
                conversation_ids
            ):
                raise ControllerError("FDP-2 control store contains unexpected conversations")
            for conversation_id in conversation_ids:
                conversation = control / conversation_id
                conversation_modes = (*control_modes, 0o700)
                if set(
                    rooted.list_directory(
                        conversation, directory_modes=conversation_modes
                    )
                ) != {"task-spec.json", "prompts"}:
                    raise ControllerError(
                        "FDP-2 conversation store contains unexpected entries"
                    )
                prompts = conversation / "prompts"
                if set(
                    rooted.list_directory(
                        prompts, directory_modes=(*conversation_modes, 0o700)
                    )
                ) != prompt_names[conversation_id]:
                    raise ControllerError(
                        "FDP-2 prompt store contains unexpected entries"
                    )
            if digests and set(
                rooted.list_directory(payload_root, directory_modes=payload_modes)
            ) != set(digests):
                raise ControllerError("FDP-2 payload store contains unexpected entries")
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise ControllerError(f"unsafe FDP-2 receipt source: {exc}") from exc
    return files


def _write_live_receipt(
    runs_dir: Path,
    feature: str,
    receipt_path: Path,
    receipt: dict[str, Any],
) -> None:
    relative = Path(f"fleet-{feature}.verification-receipt.json")
    try:
        payload = fleet_json.canonical_bytes(receipt) + b"\n"
    except fleet_json.FleetJSONError as exc:
        raise ControllerError("FDP-2 verification receipt is not strict JSON") from exc
    if len(payload) > MAX_RECEIPT_BYTES:
        raise ControllerError(
            f"FDP-2 verification receipt exceeds {MAX_RECEIPT_BYTES} bytes"
        )
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            _assert_recorded_rooted_path(
                rooted,
                str(receipt_path),
                relative,
                "FDP-2 verification receipt",
            )
            rooted.replace_regular(
                relative,
                payload,
                directory_modes=(),
                file_mode=0o600,
            )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise ControllerError(f"unsafe FDP-2 verification receipt path: {exc}") from exc


def create_live_receipt(
    runs_dir: Path,
    *,
    feature: str,
    receipt_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    feature = _feature(feature)
    with coordinator(runs_dir):
        summary, events, messages = _verify_live_locked(
            runs_dir, feature, now=now
        )
        if summary["latest_status"] not in TERMINAL_STATES:
            raise ControllerConflict("fleet-down refuses an active FDP-2 conversation")
        receipt = {
            "schema_version": 1,
            "feature": feature,
            "created_at": timestamp(now or utc_now()),
            "summary": summary,
            "files": _receipt_file_hashes(
                runs_dir,
                feature,
                events,
                messages,
                archived=False,
            ),
        }
        _write_live_receipt(runs_dir, feature, receipt_path, receipt)
        return receipt


def verify_archive(archive: Path) -> dict[str, Any]:
    archive = Path(archive)
    manifest = _manifest_values(archive / "manifest")
    feature = manifest.get("feature", "")
    if not fleet_dialogue.SAFE_FEATURE.fullmatch(feature):
        raise ControllerError("archived manifest has an invalid feature")
    _validate_roster(manifest)
    events = load_events_from_path(archive / "dialogue-control.jsonl", feature)
    if not events or events[-1]["snapshot"]["status"] not in TERMINAL_STATES:
        raise ControllerError("archived FDP-2 conversation is not terminal")
    instances = _manifest_instances(manifest)
    dialogue_summary = fleet_dialogue.verify_storage(
        archive / "dialogue.jsonl",
        archive / "dialogue" / "payloads",
        feature=feature,
        instances=instances,
    )
    messages = fleet_dialogue.load_messages_from_path(
        archive / "dialogue.jsonl", feature
    )
    _verify_archived_control_artifacts(archive, events)
    _verify_message_bindings(events, messages)
    try:
        receipt = fleet_json.loads(
            _read_rooted_regular(
                archive,
                "verification-receipt.json",
                directory_modes=(),
                max_bytes=MAX_RECEIPT_BYTES,
                where="archived FDP-2 verification receipt",
            )
        )
    except fleet_json.FleetJSONError as exc:
        raise ControllerError("archived FDP-2 verification receipt is missing or invalid") from exc
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "feature",
        "created_at",
        "summary",
        "files",
    }:
        raise ControllerError("archived FDP-2 verification receipt fields are invalid")
    if receipt.get("schema_version") != 1 or receipt.get("feature") != feature:
        raise ControllerError("archived FDP-2 verification receipt identity is invalid")
    parse_timestamp(receipt.get("created_at"), "receipt.created_at")
    files = receipt.get("files")
    if not isinstance(files, dict) or not files:
        raise ControllerError("archived FDP-2 verification receipt has no file hashes")
    for logical, expected in files.items():
        if (
            not isinstance(logical, str)
            or logical.startswith("/")
            or ".." in PurePosixPath(logical).parts
        ):
            raise ControllerError("archived FDP-2 receipt path is unsafe")
        if not isinstance(expected, dict) or set(expected) != {"sha256", "bytes"}:
            raise ControllerError("archived FDP-2 receipt hash entry is invalid")
        if (
            not SHA256.fullmatch(str(expected.get("sha256", "")))
            or type(expected.get("bytes")) is not int
            or expected["bytes"] < 0
        ):
            raise ControllerError("archived FDP-2 receipt hash entry is invalid")
    actual_files = _receipt_file_hashes(
        archive,
        feature,
        events,
        messages,
        archived=True,
    )
    if files != actual_files:
        raise ControllerError("archived FDP-2 receipt file set or hash changed")
    summary = {
        "feature": feature,
        **_control_summary(events, dialogue_summary, manifest),
    }
    if receipt.get("summary") != summary:
        raise ControllerError("archived FDP-2 verification summary changed")
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
        if name == "start":
            command.add_argument("--spec-file", required=True)
        if name == "step":
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--run-id")
            source.add_argument("--message-id")
        if name == "abandon":
            command.add_argument("--reason", required=True)
        if name == "verify":
            command.add_argument("--archive")
            command.add_argument("--write-receipt")
            command.add_argument("--require-terminal", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "verify" and args.archive:
            if args.runs_dir or args.feature or args.write_receipt:
                raise ControllerError("verify --archive cannot use runs_dir, feature, or write-receipt")
            print(json.dumps(verify_archive(Path(args.archive).expanduser().resolve()), sort_keys=True))
            return 0
        if not args.runs_dir:
            raise ControllerError("live FDP-2 command requires runs_dir")
        runs_dir = Path(args.runs_dir).expanduser().resolve()
        if not args.feature:
            raise ControllerError("live FDP-2 command requires --feature")
        if args.command == "start":
            value = start(
                runs_dir,
                feature=args.feature,
                idempotency_key=args.idempotency_key,
                spec_file=Path(args.spec_file).expanduser().resolve(),
            )
        elif args.command == "step":
            value = step(
                runs_dir,
                feature=args.feature,
                idempotency_key=args.idempotency_key,
                run_id=args.run_id,
                message_id=args.message_id,
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
                raise ControllerConflict("FDP-2 conversation is still active")
            print(json.dumps(verified, sort_keys=True))
            return 0
    except ControllerConflict as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except (ControllerError, fleet_dialogue.DialogueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(public_event(value), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
