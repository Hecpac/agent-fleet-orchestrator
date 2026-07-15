#!/usr/bin/env python3
"""Durable hash-chained Mission Control state and idempotent event appends."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterator
import uuid


SCHEMA_VERSION = 1
GENESIS_SHA256 = "0" * 64
EVENT_FIELDS = {
    "schema_version", "event_id", "mission_id", "sequence", "timestamp",
    "kind", "actor", "idempotency_key", "payload",
    "previous_event_sha256", "event_sha256",
}
TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "abandoned", "indeterminate"}
STATE_EVENT = {
    "mission_created": "created",
    "workflow_compiled": "compiled",
    "fleet_boot_started": "booting",
    "mission_running": "running",
    "assurance_requested": "awaiting_assurance_confirmation",
    "assurance_approved": "assurance_approved",
    "assurance_boot_started": "assured_booting",
    "assurance_started": "assured_running",
    "mission_completing": "completing",
    "archive_created": "archived",
}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "unknown": 3}
SAFE_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SAFE_ACTOR = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MissionStateError(RuntimeError):
    """Mission state is corrupt or violates a durable invariant."""


class MissionConflict(MissionStateError):
    """An idempotency key or immutable terminal conflicts with a request."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def sha256(value: Any) -> str:
    data = value if isinstance(value, bytes) else canonical_bytes(value)
    return hashlib.sha256(data).hexdigest()


def artifact_id(content: bytes | str) -> str:
    value = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(value).hexdigest()


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_uuid(value: str, where: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise MissionStateError(f"invalid {where}") from exc


def missions_root(runs_dir: Path) -> Path:
    return runs_dir / "missions"


def mission_root(runs_dir: Path, mission_id: str) -> Path:
    return missions_root(runs_dir) / normalize_uuid(mission_id, "mission_id")


def ledger_path(runs_dir: Path, mission_id: str) -> Path:
    return mission_root(runs_dir, mission_id) / "mission.jsonl"


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise MissionStateError(f"unsafe mission directory: {path}")
    if info.st_uid != os.geteuid():
        raise MissionStateError(f"mission directory has unexpected owner: {path}")
    os.chmod(path, 0o700)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    ensure_private_directory(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    ensure_private_directory(path.parent)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, mode)
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise MissionStateError(f"short write: {path}")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if path.is_symlink():
            raise MissionStateError(f"refusing symlink target: {path}")
        os.replace(temporary, path)
        os.chmod(path, mode)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_payload(kind: str, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise MissionStateError("event payload must be an object")
    if kind == "mission_created":
        required = {
            "feature", "objective_sha256", "target_repo", "base_sha",
            "workflow_digest", "initial_risk",
        }
        if set(payload) != required:
            raise MissionStateError("mission_created payload fields do not match schema")
        if not isinstance(payload["feature"], str) or not payload["feature"]:
            raise MissionStateError("mission feature must be non-empty")
        for field in ("objective_sha256", "workflow_digest"):
            if not SHA256.fullmatch(str(payload[field])):
                raise MissionStateError(f"{field} must be SHA-256")
        if payload["initial_risk"] not in RISK_ORDER:
            raise MissionStateError("invalid initial risk")
    elif kind == "risk_escalated":
        if set(payload) != {"from", "to", "categories", "reason"}:
            raise MissionStateError("risk_escalated payload fields do not match schema")
        if payload["from"] not in RISK_ORDER or payload["to"] not in RISK_ORDER:
            raise MissionStateError("invalid risk escalation level")
        if not isinstance(payload["categories"], list) or not all(
            isinstance(item, str) and item for item in payload["categories"]
        ):
            raise MissionStateError("risk categories must be a string list")
    elif kind == "mission_terminal":
        if set(payload) != {"status", "reason"} or payload["status"] not in TERMINAL_STATUSES:
            raise MissionStateError("invalid mission terminal payload")
    elif kind == "lead_dispatched":
        if set(payload) != {"run_id", "prompt_sha256"}:
            raise MissionStateError("lead_dispatched payload fields do not match schema")
        normalize_uuid(payload["run_id"], "lead run_id")
        if not SHA256.fullmatch(str(payload["prompt_sha256"])):
            raise MissionStateError("lead prompt_sha256 is invalid")
    elif kind == "lead_result_recorded":
        required = {"run_id", "artifact_id", "result_file", "provider", "model", "variant"}
        if set(payload) != required:
            raise MissionStateError("lead result payload fields do not match schema")
        normalize_uuid(payload["run_id"], "lead run_id")
        if not SHA256.fullmatch(str(payload["artifact_id"])):
            raise MissionStateError("lead result artifact_id is invalid")
    elif kind == "assurance_requested":
        required = {"risk", "categories", "scope", "workflow_digest"}
        if set(payload) != required or payload["risk"] not in {"high", "unknown"}:
            raise MissionStateError("assurance request payload is invalid")
        if not isinstance(payload["categories"], list) or not all(
            isinstance(item, str) and item for item in payload["categories"]
        ):
            raise MissionStateError("assurance categories must be a string list")
        if not isinstance(payload["scope"], str) or not payload["scope"]:
            raise MissionStateError("assurance scope must be non-empty")
        if not SHA256.fullmatch(str(payload["workflow_digest"])):
            raise MissionStateError("assurance workflow_digest is invalid")
    elif kind == "assurance_approved":
        required = {
            "approval_id", "request_event_sha256", "workflow_digest", "scope",
            "risk", "expires_at", "approved_by_sha256", "decision",
        }
        if set(payload) != required or payload["decision"] != "approved":
            raise MissionStateError("assurance approval payload is invalid")
        normalize_uuid(str(payload["approval_id"]), "approval_id")
        for field in ("request_event_sha256", "workflow_digest", "approved_by_sha256"):
            if not SHA256.fullmatch(str(payload[field])):
                raise MissionStateError(f"assurance approval {field} is invalid")
        if payload["risk"] not in {"high", "unknown"}:
            raise MissionStateError("assurance approval risk is invalid")
        try:
            expires = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise MissionStateError("assurance approval expiry is invalid") from exc
        if expires.tzinfo is None:
            raise MissionStateError("assurance approval expiry lacks timezone")
        if not isinstance(payload["scope"], str) or not payload["scope"]:
            raise MissionStateError("assurance approval scope is invalid")
    elif kind == "assurance_boot_started":
        if set(payload) != {"preset", "approval_event_sha256"}:
            raise MissionStateError("assurance boot payload is invalid")
        if not isinstance(payload["preset"], str) or not payload["preset"]:
            raise MissionStateError("assurance preset is invalid")
        if not SHA256.fullmatch(str(payload["approval_event_sha256"])):
            raise MissionStateError("assurance boot approval reference is invalid")
    elif kind == "assurance_started":
        if set(payload) != {"manifest", "approval_event_sha256"}:
            raise MissionStateError("assurance started payload is invalid")
        if not isinstance(payload["manifest"], str) or not payload["manifest"]:
            raise MissionStateError("assurance manifest is invalid")
        if not SHA256.fullmatch(str(payload["approval_event_sha256"])):
            raise MissionStateError("assurance started approval reference is invalid")
    elif kind == "delegation_registered":
        required = {
            "delegation_id", "mission_id", "run_id", "parent_run_id",
            "delegated_by", "recipient_instance", "capability", "objective_sha256",
            "input_artifact_ids", "expected_output_contract", "deadline",
            "provider", "model", "variant", "depth", "token_id",
        }
        if set(payload) != required:
            raise MissionStateError("delegation payload fields do not match schema")
        normalize_uuid(payload["delegation_id"], "delegation_id")
        normalize_uuid(payload["mission_id"], "mission_id")
        normalize_uuid(payload["run_id"], "run_id")
        if payload["parent_run_id"] is not None:
            normalize_uuid(payload["parent_run_id"], "parent_run_id")
        if not SHA256.fullmatch(str(payload["objective_sha256"])):
            raise MissionStateError("delegation objective_sha256 is invalid")
        if not isinstance(payload["input_artifact_ids"], list) or any(
            not SHA256.fullmatch(str(item)) for item in payload["input_artifact_ids"]
        ):
            raise MissionStateError("delegation input_artifact_ids are invalid")
        if len(payload["input_artifact_ids"]) != len(set(payload["input_artifact_ids"])):
            raise MissionStateError("delegation input_artifact_ids contain duplicates")
        if not isinstance(payload["expected_output_contract"], dict):
            raise MissionStateError("delegation expected_output_contract must be an object")
        if isinstance(payload["depth"], bool) or not isinstance(payload["depth"], int) or payload["depth"] < 1:
            raise MissionStateError("delegation depth is invalid")
        if payload["token_id"] is not None:
            normalize_uuid(payload["token_id"], "token_id")
        try:
            deadline = datetime.fromisoformat(str(payload["deadline"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise MissionStateError("delegation deadline is invalid") from exc
        if deadline.tzinfo is None:
            raise MissionStateError("delegation deadline lacks timezone")
        for field in ("delegated_by", "recipient_instance", "capability", "provider", "model"):
            if not isinstance(payload[field], str) or not payload[field]:
                raise MissionStateError(f"delegation {field} must be non-empty")
    elif kind == "result_recorded":
        required = {"run_id", "delegation_id", "artifact_id", "provider", "model", "variant"}
        if set(payload) != required:
            raise MissionStateError("result payload fields do not match schema")
        normalize_uuid(payload["run_id"], "run_id")
        normalize_uuid(payload["delegation_id"], "delegation_id")
        if not SHA256.fullmatch(str(payload["artifact_id"])):
            raise MissionStateError("result artifact_id is invalid")


def _validate_event(event: Any, previous: dict[str, Any] | None, mission_id: str) -> None:
    if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
        raise MissionStateError("mission event fields do not match schema_version=1")
    if event["schema_version"] != SCHEMA_VERSION:
        raise MissionStateError("unsupported mission event schema_version")
    if normalize_uuid(event["event_id"], "event_id") != event["event_id"]:
        raise MissionStateError("event_id is not canonical")
    if normalize_uuid(event["mission_id"], "mission_id") != mission_id:
        raise MissionStateError("event mission_id mismatch")
    expected_sequence = 1 if previous is None else previous["sequence"] + 1
    if event["sequence"] != expected_sequence:
        raise MissionStateError("mission event sequence gap")
    try:
        timestamp = datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MissionStateError("invalid mission event timestamp") from exc
    if timestamp.tzinfo is None:
        raise MissionStateError("mission event timestamp lacks timezone")
    if not SAFE_KIND.fullmatch(str(event["kind"])):
        raise MissionStateError("invalid mission event kind")
    if not SAFE_ACTOR.fullmatch(str(event["actor"])):
        raise MissionStateError("invalid mission event actor")
    if not SAFE_KEY.fullmatch(str(event["idempotency_key"])):
        raise MissionStateError("invalid mission idempotency key")
    expected_previous = GENESIS_SHA256 if previous is None else previous["event_sha256"]
    if event["previous_event_sha256"] != expected_previous:
        raise MissionStateError("mission event hash chain is broken")
    stored_hash = event["event_sha256"]
    if not SHA256.fullmatch(str(stored_hash)):
        raise MissionStateError("invalid mission event hash")
    unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
    if sha256(unsigned) != stored_hash:
        raise MissionStateError("mission event hash mismatch")
    _validate_payload(event["kind"], event["payload"])


def read_events(path: Path, *, expected_mission_id: str | None = None) -> list[dict[str, Any]]:
    mission_id = expected_mission_id
    events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, 1):
                if not raw.endswith("\n"):
                    raise MissionStateError(f"partial mission event at line {line_number}")
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise MissionStateError(f"invalid mission JSON at line {line_number}: {exc}") from exc
                if mission_id is None:
                    mission_id = normalize_uuid(str(event.get("mission_id", "")), "mission_id")
                _validate_event(event, events[-1] if events else None, mission_id)
                events.append(event)
    except FileNotFoundError:
        return []
    if expected_mission_id is not None and not events:
        raise MissionStateError("mission ledger is empty")
    return events


def derive_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events or events[0]["kind"] != "mission_created":
        raise MissionStateError("mission must begin with mission_created")
    created = events[0]["payload"]
    result: dict[str, Any] = {
        "mission_id": events[0]["mission_id"],
        "feature": created["feature"],
        "status": "created",
        "risk": created["initial_risk"],
        "risk_categories": [],
        "workflow_digest": created["workflow_digest"],
        "objective_sha256": created["objective_sha256"],
        "target_repo": created["target_repo"],
        "base_sha": created["base_sha"],
        "terminal": None,
        "lead_run_id": None,
        "lead_result": None,
        "approval": None,
        "delegations": {},
        "results": {},
        "last_sequence": 0,
        "head_sha256": GENESIS_SHA256,
    }
    terminal_seen = False
    idempotency_keys: set[str] = set()
    for event in events:
        if event["idempotency_key"] in idempotency_keys:
            raise MissionStateError("duplicate mission idempotency key")
        idempotency_keys.add(event["idempotency_key"])
        if terminal_seen:
            raise MissionStateError("event follows immutable mission terminal")
        kind = event["kind"]
        payload = event["payload"]
        if kind in STATE_EVENT:
            if kind == "workflow_compiled" and result["status"] != "created":
                raise MissionStateError("workflow_compiled requires created state")
            if kind == "fleet_boot_started" and result["status"] != "compiled":
                raise MissionStateError("fleet_boot_started requires compiled state")
            if kind == "mission_running" and result["status"] not in {"booting", "running"}:
                raise MissionStateError("mission_running requires booting state")
            if kind == "assurance_requested" and result["status"] not in {
                "compiled", "running", "assured_running"
            }:
                raise MissionStateError("assurance request requires compiled or running state")
            if kind == "assurance_requested":
                if not isinstance(payload["categories"], list) or not all(
                    isinstance(item, str) and item for item in payload["categories"]
                ) or not isinstance(payload["scope"], str) or not payload["scope"]:
                    raise MissionStateError("assurance request scope or categories are invalid")
                result["risk_categories"] = sorted(
                    set(result["risk_categories"]) | set(payload["categories"])
                )
            if kind == "assurance_approved":
                if result["status"] != "awaiting_assurance_confirmation":
                    raise MissionStateError("assurance approval requires confirmation state")
                request = next(
                    item for item in reversed(events[: event["sequence"] - 1])
                    if item["kind"] == "assurance_requested"
                )
                if payload["request_event_sha256"] != request["event_sha256"]:
                    raise MissionStateError("approval does not reference the active assurance request")
                if payload["workflow_digest"] != result["workflow_digest"]:
                    raise MissionStateError("approval workflow digest mismatch")
                if payload["scope"] != result["target_repo"] or payload["risk"] != result["risk"]:
                    raise MissionStateError("approval scope or risk mismatch")
                result["approval"] = {**payload, "event_sha256": event["event_sha256"]}
            if kind == "assurance_boot_started":
                if result["status"] != "assurance_approved" or result["approval"] is None:
                    raise MissionStateError("assurance boot requires approval")
                if payload["approval_event_sha256"] != result["approval"]["event_sha256"]:
                    raise MissionStateError("assurance boot approval reference mismatch")
                expires = datetime.fromisoformat(
                    result["approval"]["expires_at"].replace("Z", "+00:00")
                ).astimezone(timezone.utc)
                started = datetime.fromisoformat(
                    event["timestamp"].replace("Z", "+00:00")
                ).astimezone(timezone.utc)
                if started >= expires:
                    raise MissionStateError("assurance approval expired before boot")
            if kind == "assurance_started":
                if result["status"] != "assured_booting" or result["approval"] is None:
                    raise MissionStateError("assurance start requires assured boot state")
                if payload["approval_event_sha256"] != result["approval"]["event_sha256"]:
                    raise MissionStateError("assurance start approval reference mismatch")
            if kind == "mission_completing" and result["status"] not in {"running", "assured_running"}:
                raise MissionStateError("mission completing requires running state")
            if kind == "archive_created" and result["status"] != "completing":
                raise MissionStateError("archive creation requires completing state")
            result["status"] = STATE_EVENT[kind]
        elif kind == "risk_escalated":
            if payload["from"] != result["risk"]:
                raise MissionStateError("risk escalation does not start at current risk")
            if RISK_ORDER[payload["to"]] < RISK_ORDER[result["risk"]]:
                raise MissionStateError("mission risk cannot decrease")
            result["risk"] = payload["to"]
            result["risk_categories"] = sorted(
                set(result["risk_categories"]) | set(payload["categories"])
            )
        elif kind == "lead_dispatched":
            run_id = normalize_uuid(payload.get("run_id", ""), "lead run_id")
            if result["lead_run_id"] not in {None, run_id}:
                raise MissionStateError("mission has more than one lead run")
            result["lead_run_id"] = run_id
        elif kind == "lead_result_recorded":
            run_id = normalize_uuid(payload["run_id"], "lead run_id")
            if result["lead_run_id"] != run_id:
                raise MissionStateError("lead result run_id does not match dispatched lead")
            if result["lead_result"] is not None and result["lead_result"] != payload:
                raise MissionStateError("mission has conflicting lead results")
            result["lead_result"] = payload
        elif kind == "delegation_registered":
            delegation_id = payload["delegation_id"]
            if payload["mission_id"] != result["mission_id"]:
                raise MissionStateError("delegation mission_id mismatch")
            if delegation_id in result["delegations"]:
                raise MissionStateError("duplicate delegation_id")
            result["delegations"][delegation_id] = payload
        elif kind == "result_recorded":
            if payload["delegation_id"] not in result["delegations"]:
                raise MissionStateError("result references unknown delegation")
            delegation = result["delegations"][payload["delegation_id"]]
            if payload["run_id"] != delegation["run_id"]:
                raise MissionStateError("result run_id does not match delegation")
            result["results"][payload["delegation_id"]] = payload
        elif kind == "mission_terminal":
            if payload["status"] == "succeeded" and result["status"] != "archived":
                raise MissionStateError("succeeded terminal requires archived state")
            result["status"] = payload["status"]
            result["terminal"] = payload
            terminal_seen = True
        result["last_sequence"] = event["sequence"]
        result["head_sha256"] = event["event_sha256"]
    return result


def verify_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        raise MissionStateError("mission has no events")
    current = derive_state(events)
    return {
        "mission_id": events[0]["mission_id"],
        "events": len(events),
        "head_sha256": events[-1]["event_sha256"],
        "status": current["status"],
        "valid": True,
    }


def append_event(
    runs_dir: Path,
    mission_id: str,
    *,
    kind: str,
    actor: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    mission_id = normalize_uuid(mission_id, "mission_id")
    if not SAFE_KIND.fullmatch(kind):
        raise MissionStateError("invalid mission event kind")
    if not SAFE_ACTOR.fullmatch(actor):
        raise MissionStateError("invalid mission event actor")
    if not SAFE_KEY.fullmatch(idempotency_key):
        raise MissionStateError("invalid mission idempotency key")
    _validate_payload(kind, payload)
    root = mission_root(runs_dir, mission_id)
    ensure_private_directory(root)
    path = root / "mission.jsonl"
    with exclusive_lock(root / ".lock"):
        events = read_events(path, expected_mission_id=mission_id) if path.exists() else []
        for event in events:
            if event["idempotency_key"] != idempotency_key:
                continue
            request = {"kind": kind, "actor": actor, "payload": payload}
            stored = {key: event[key] for key in ("kind", "actor", "payload")}
            if stored != request:
                raise MissionConflict("idempotency key was already used for another mission request")
            return event, False
        if events and derive_state(events)["status"] in TERMINAL_STATUSES:
            raise MissionConflict("mission terminal is immutable")
        event: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "mission_id": mission_id,
            "sequence": len(events) + 1,
            "timestamp": utc_timestamp(),
            "kind": kind,
            "actor": actor,
            "idempotency_key": idempotency_key,
            "payload": payload,
            "previous_event_sha256": events[-1]["event_sha256"] if events else GENESIS_SHA256,
        }
        event["event_sha256"] = sha256(event)
        _validate_event(event, events[-1] if events else None, mission_id)
        derive_state(events + [event])
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(canonical_bytes(event) + b"\n")
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise MissionStateError("short mission ledger append")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return event, True


def append_terminal(
    runs_dir: Path,
    mission_id: str,
    *,
    status: str,
    reason: str,
    idempotency_key: str,
    actor: str = "CONTROL",
) -> tuple[dict[str, Any], bool]:
    return append_event(
        runs_dir,
        mission_id,
        kind="mission_terminal",
        actor=actor,
        idempotency_key=idempotency_key,
        payload={"status": status, "reason": reason},
    )


def resume_plan(current: dict[str, Any]) -> dict[str, Any]:
    action = {
        "created": "compile",
        "compiled": "boot",
        "booting": "reconcile_boot",
        "running": "reconcile_lead" if current.get("lead_run_id") else "dispatch_lead",
        "awaiting_assurance_confirmation": "await_human",
        "assurance_approved": "boot_assured",
        "assured_booting": "reconcile_assured_boot",
        "assured_running": "reconcile_assurance",
        "completing": "archive",
        "archived": "mark_succeeded",
        "succeeded": "none",
        "failed": "none",
        "blocked": "none",
        "abandoned": "none",
        "indeterminate": "none",
    }[current["status"]]
    return {
        "mission_id": current["mission_id"],
        "status": current["status"],
        "next_action": action,
        "last_sequence": current["last_sequence"],
        "head_sha256": current["head_sha256"],
    }
