#!/usr/bin/env python3
"""Durable hash-chained Mission Control state and idempotent event appends."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Iterator, Sequence
import uuid

import fleet_json
import fleet_safe_paths


SCHEMA_VERSION = 1
GENESIS_SHA256 = "0" * 64
DECISION_TIMEOUT_SECONDS = 2 * 60 * 60
DECISION_IMPACTS = frozenset({"blocking", "checkpoint"})
DECISION_RISKS = frozenset({"low", "medium", "high", "unknown"})
DECISION_RESOLUTION_KINDS = frozenset({"human", "automatic"})
EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "mission_id",
    "sequence",
    "timestamp",
    "kind",
    "actor",
    "idempotency_key",
    "payload",
    "previous_event_sha256",
    "event_sha256",
}
TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "abandoned", "indeterminate"}
ADMISSION_PHASES = {
    "reserved",
    "committed",
    "authorized",
    "started",
    "finalized",
    "aborted",
}
ACTIVE_ADMISSION_PHASES = {"reserved", "committed", "authorized", "started"}
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
SAFE_FEATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_DECISION_OPTION = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
MISSION_LEDGER_TEMP = re.compile(
    r"^\.mission\.jsonl\.(?:[0-9a-f]{32}|[0-9a-f]{64})\.tmp$"
)
LEGACY_MISSION_LEDGER_TEMP = re.compile(r"^\.mission\.jsonl\.[0-9a-f]{32}\.tmp$")
MISSION_SNAPSHOT_ATTEMPTS = 8
MISSION_SNAPSHOT_RETRY_SECONDS = 0.001
MUTATION_DRAIN_TIMEOUT_SECONDS = 30.0
MUTATION_DRAIN_POLL_SECONDS = 0.01


class MissionStateError(RuntimeError):
    """Mission state is corrupt or violates a durable invariant."""


class MissionConflict(MissionStateError):
    """An idempotency key or immutable terminal conflicts with a request."""


class _MissionSnapshotChanged(RuntimeError):
    """A cooperative atomic ledger publication crossed one snapshot open."""


def _mission_snapshot_checkpoint(name: str) -> None:
    """Test-only scheduling hook for deterministic publication races."""


def _mission_transaction_checkpoint(name: str) -> None:
    if os.environ.get("FLEET_TEST_MISSION_TRANSACTION_CRASH_AT") == name:
        os._exit(137)


def validate_target_repo(value: Any) -> str:
    """Validate the portable lexical form of one physical target binding.

    Mission ledgers and archives must remain verifiable when the original
    repository is offline, so this check deliberately does not resolve or stat
    the path.  Online boot code separately proves that the same canonical
    string names the exact physical Git toplevel.
    """

    if (
        not isinstance(value, str)
        or not value
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise MissionStateError("mission target_repo must be a canonical absolute path")
    target = Path(value)
    if (
        not target.is_absolute()
        or value.startswith("//")
        or str(target) != value
        or any(part in {".", ".."} for part in target.parts)
    ):
        raise MissionStateError("mission target_repo must be a canonical absolute path")
    return value


def loads_strict(raw: bytes | str) -> Any:
    return fleet_json.loads(raw)


def canonical_bytes(value: Any) -> bytes:
    return fleet_json.canonical_bytes(value)


def sha256(value: Any) -> str:
    data = value if isinstance(value, bytes) else canonical_bytes(value)
    return hashlib.sha256(data).hexdigest()


def artifact_id(content: bytes | str) -> str:
    value = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(value).hexdigest()


def utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def parse_timestamp(value: Any, where: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MissionStateError(f"invalid {where}") from exc
    if parsed.tzinfo is None:
        raise MissionStateError(f"{where} lacks timezone")
    return parsed.astimezone(timezone.utc)


def _next_timestamp(previous: dict[str, Any] | None) -> str:
    current = datetime.now(timezone.utc)
    if previous is not None:
        minimum = parse_timestamp(
            previous["timestamp"], "previous event timestamp"
        ) + timedelta(microseconds=1)
        current = max(current, minimum)
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z")


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


def _require_fields(kind: str, payload: dict[str, Any], required: set[str]) -> None:
    if set(payload) != required:
        raise MissionStateError(f"{kind} payload fields do not match schema")


def _require_nonempty(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise MissionStateError(f"{where} must be non-empty")
    return value


def _require_sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise MissionStateError(f"{where} must be SHA-256")
    return value


def _require_uuid(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise MissionStateError(f"{where} must be a canonical UUID string")
    normalized = normalize_uuid(value, where)
    if normalized != value:
        raise MissionStateError(f"{where} is not canonical")
    return normalized


def _require_string_list(value: Any, where: str, *, unique: bool = False) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise MissionStateError(f"{where} must be a string list")
    if unique and len(value) != len(set(value)):
        raise MissionStateError(f"{where} must contain unique values")
    return value


def _require_uint(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MissionStateError(f"{where} must be a non-negative integer")
    return value


def decision_deadline(requested_at: str) -> str:
    """Return the deterministic two-hour deadline for one decision request."""

    value = parse_timestamp(requested_at, "decision request timestamp") + timedelta(
        seconds=DECISION_TIMEOUT_SECONDS
    )
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_decision_evidence(value: Any, where: str, *, recommendation: bool) -> None:
    fields = {"artifact_id", "delegation_id", "instance"}
    fields |= {"option_id", "rationale"} if recommendation else {"summary"}
    if not isinstance(value, dict) or set(value) != fields:
        raise MissionStateError(f"{where} fields do not match schema")
    _require_sha(value["artifact_id"], f"{where} artifact_id")
    _require_uuid(value["delegation_id"], f"{where} delegation_id")
    if not SAFE_FEATURE.fullmatch(_require_nonempty(value["instance"], f"{where} instance")):
        raise MissionStateError(f"{where} instance is invalid")
    if recommendation:
        if not SAFE_DECISION_OPTION.fullmatch(
            _require_nonempty(value["option_id"], f"{where} option_id")
        ):
            raise MissionStateError(f"{where} option_id is invalid")
        _require_nonempty(value["rationale"], f"{where} rationale")
    else:
        _require_nonempty(value["summary"], f"{where} summary")


def validate_decision_request_payload(payload: dict[str, Any]) -> None:
    """Validate the closed durable Decision Brief v1 request contract."""

    kind = "human_decision_requested"
    _require_fields(
        kind,
        payload,
        {
            "decision_id",
            "title",
            "question",
            "affected_instances",
            "impact",
            "risk",
            "reversible",
            "options",
            "recommendation",
            "challenge",
            "dissent",
            "default_option_id",
        },
    )
    _require_uuid(payload["decision_id"], "decision_id")
    _require_nonempty(payload["title"], "decision title")
    _require_nonempty(payload["question"], "decision question")
    affected = _require_string_list(
        payload["affected_instances"], "decision affected_instances", unique=True
    )
    if not affected or affected != sorted(affected) or any(
        not SAFE_FEATURE.fullmatch(instance) for instance in affected
    ):
        raise MissionStateError(
            "decision affected_instances must be a non-empty sorted instance list"
        )
    if payload["impact"] not in DECISION_IMPACTS:
        raise MissionStateError("decision impact is invalid")
    if payload["risk"] not in DECISION_RISKS:
        raise MissionStateError("decision risk is invalid")
    if payload["risk"] in {"high", "unknown"} and payload["impact"] != "blocking":
        raise MissionStateError("high or unknown decision risk must be blocking")
    if not isinstance(payload["reversible"], bool):
        raise MissionStateError("decision reversible must be boolean")

    options = payload["options"]
    if not isinstance(options, list) or not 2 <= len(options) <= 3:
        raise MissionStateError("decision options must contain two or three choices")
    option_ids: list[str] = []
    for index, option in enumerate(options):
        if not isinstance(option, dict) or set(option) != {
            "option_id",
            "label",
            "tradeoffs",
        }:
            raise MissionStateError(f"decision option[{index}] fields do not match schema")
        option_id = _require_nonempty(
            option["option_id"], f"decision option[{index}].option_id"
        )
        if not SAFE_DECISION_OPTION.fullmatch(option_id):
            raise MissionStateError(f"decision option[{index}].option_id is invalid")
        _require_nonempty(option["label"], f"decision option[{index}].label")
        _require_nonempty(option["tradeoffs"], f"decision option[{index}].tradeoffs")
        option_ids.append(option_id)
    if len(option_ids) != len(set(option_ids)):
        raise MissionStateError("decision option IDs must be unique")

    _validate_decision_evidence(
        payload["recommendation"], "decision recommendation", recommendation=True
    )
    _validate_decision_evidence(
        payload["challenge"], "decision challenge", recommendation=False
    )
    if payload["recommendation"]["option_id"] not in option_ids:
        raise MissionStateError("decision recommendation references an unknown option")
    if (
        payload["recommendation"]["artifact_id"]
        == payload["challenge"]["artifact_id"]
        or payload["recommendation"]["delegation_id"]
        == payload["challenge"]["delegation_id"]
        or payload["recommendation"]["instance"]
        == payload["challenge"]["instance"]
    ):
        raise MissionStateError("decision challenger must be evidence-distinct")
    _require_nonempty(payload["dissent"], "decision dissent")
    default_option = payload["default_option_id"]
    if default_option is not None:
        if not isinstance(default_option, str) or default_option not in option_ids:
            raise MissionStateError("decision default_option_id is invalid")
        if payload["risk"] != "low" or not payload["reversible"]:
            raise MissionStateError(
                "only low-risk reversible decisions may declare a default option"
            )


def validate_decision_resolution_payload(payload: dict[str, Any]) -> None:
    """Validate the closed durable Decision Brief v1 resolution contract."""

    kind = "human_decision_resolved"
    _require_fields(
        kind,
        payload,
        {
            "decision_id",
            "request_event_sha256",
            "option_id",
            "reason",
            "resolution_kind",
        },
    )
    _require_uuid(payload["decision_id"], "decision resolution decision_id")
    _require_sha(
        payload["request_event_sha256"], "decision resolution request_event_sha256"
    )
    if not SAFE_DECISION_OPTION.fullmatch(
        _require_nonempty(payload["option_id"], "decision resolution option_id")
    ):
        raise MissionStateError("decision resolution option_id is invalid")
    _require_nonempty(payload["reason"], "decision resolution reason")
    if payload["resolution_kind"] not in DECISION_RESOLUTION_KINDS:
        raise MissionStateError("decision resolution kind is invalid")


def _validate_assured_action(kind: str, payload: dict[str, Any]) -> None:
    action = payload.get("action")
    dispatch = {"action", "instance", "prompt_sha256", "controller_sequence"}
    wait = {
        "action",
        "instance",
        "run_id",
        "timeout_seconds",
        "controller_sequence",
    }
    publish = {
        "action",
        "kind",
        "recipient",
        "source_instance",
        "source_run_id",
        "reply_to",
        "payload_sha256",
    }
    if kind == "assured_action_completed":
        if action == "dispatch":
            dispatch |= {"run_id"}
        elif action == "wait":
            wait |= {"status", "exit_code"}
        elif action == "publish":
            publish = {"action", "message_id", "payload_sha256"}
    required = {"dispatch": dispatch, "wait": wait, "publish": publish}.get(action)
    if required is None or set(payload) != required:
        raise MissionStateError(f"{kind} payload fields do not match schema")
    if action == "dispatch":
        _require_nonempty(payload["instance"], "assured dispatch instance")
        _require_sha(payload["prompt_sha256"], "assured dispatch prompt_sha256")
        _require_nonempty(payload["controller_sequence"], "assured controller_sequence")
        if kind == "assured_action_completed":
            _require_uuid(payload["run_id"], "assured dispatch run_id")
    elif action == "wait":
        _require_nonempty(payload["instance"], "assured wait instance")
        _require_uuid(payload["run_id"], "assured wait run_id")
        _require_uint(payload["timeout_seconds"], "assured wait timeout_seconds")
        _require_nonempty(payload["controller_sequence"], "assured controller_sequence")
        if kind == "assured_action_completed":
            _require_nonempty(payload["status"], "assured wait status")
            if isinstance(payload["exit_code"], bool) or not isinstance(
                payload["exit_code"], int
            ):
                raise MissionStateError("assured wait exit_code must be an integer")
    else:
        _require_sha(payload["payload_sha256"], "assured publish payload_sha256")
        if kind == "assured_action_intent":
            for field in ("kind", "recipient", "source_instance", "source_run_id"):
                _require_nonempty(payload[field], f"assured publish {field}")
            if payload["reply_to"] is not None:
                _require_nonempty(payload["reply_to"], "assured publish reply_to")
        else:
            _require_uuid(payload["message_id"], "assured publish message_id")


def _validate_payload(kind: str, payload: dict[str, Any]) -> None:
    """Validate the closed mission-event registry before any durable mutation."""

    if not isinstance(payload, dict):
        raise MissionStateError("event payload must be an object")
    if kind == "mission_created":
        _require_fields(
            kind,
            payload,
            {
                "feature",
                "objective_sha256",
                "target_repo",
                "base_sha",
                "workflow_digest",
                "initial_risk",
            },
        )
        if not isinstance(payload["feature"], str) or not SAFE_FEATURE.fullmatch(
            payload["feature"]
        ):
            raise MissionStateError("mission feature must be a canonical component")
        _require_sha(payload["objective_sha256"], "objective_sha256")
        _require_sha(payload["workflow_digest"], "workflow_digest")
        validate_target_repo(payload["target_repo"])
        if not isinstance(payload["base_sha"], str) or not GIT_OID.fullmatch(
            payload["base_sha"]
        ):
            raise MissionStateError("mission base_sha must be a full Git object id")
        if payload["initial_risk"] not in RISK_ORDER:
            raise MissionStateError("invalid initial risk")
    elif kind == "workflow_compiled":
        _require_fields(kind, payload, {"compiled_digest"})
        _require_sha(payload["compiled_digest"], "workflow compiled_digest")
    elif kind == "risk_assessed":
        _require_fields(
            kind,
            payload,
            {
                "level",
                "categories",
                "workflow_minimum",
                "override",
                "requires_confirmation",
            },
        )
        if (
            payload["level"] not in RISK_ORDER
            or payload["workflow_minimum"] not in RISK_ORDER
        ):
            raise MissionStateError("risk assessment level is invalid")
        if payload["override"] != "auto" and payload["override"] not in RISK_ORDER:
            raise MissionStateError("risk assessment override is invalid")
        _require_string_list(payload["categories"], "risk categories", unique=True)
        if not isinstance(payload["requires_confirmation"], bool):
            raise MissionStateError("risk assessment confirmation flag is invalid")
    elif kind == "risk_escalated":
        _require_fields(kind, payload, {"from", "to", "categories", "reason"})
        if payload["from"] not in RISK_ORDER or payload["to"] not in RISK_ORDER:
            raise MissionStateError("invalid risk escalation level")
        _require_string_list(payload["categories"], "risk categories")
        _require_nonempty(payload["reason"], "risk escalation reason")
    elif kind == "fleet_boot_started":
        if set(payload) not in ({"feature"}, {"feature", "preset"}):
            raise MissionStateError(f"{kind} payload fields do not match schema")
        if not isinstance(payload["feature"], str) or not SAFE_FEATURE.fullmatch(
            payload["feature"]
        ):
            raise MissionStateError("fleet boot feature is invalid")
        if "preset" in payload:
            _require_nonempty(payload["preset"], "fleet boot preset")
    elif kind == "mission_running":
        _require_fields(kind, payload, {"manifest"})
        _require_nonempty(payload["manifest"], "mission manifest")
    elif kind == "assurance_requested":
        _require_fields(
            kind, payload, {"risk", "categories", "scope", "workflow_digest"}
        )
        if payload["risk"] not in {"high", "unknown"}:
            raise MissionStateError("assurance request risk is invalid")
        _require_string_list(payload["categories"], "assurance categories")
        _require_nonempty(payload["scope"], "assurance scope")
        _require_sha(payload["workflow_digest"], "assurance workflow_digest")
    elif kind == "assurance_approved":
        historical_fields = {
            "approval_id",
            "request_event_sha256",
            "workflow_digest",
            "scope",
            "risk",
            "expires_at",
            "approved_by_sha256",
            "decision",
        }
        if set(payload) not in {
            frozenset(historical_fields),
            frozenset(historical_fields | {"expires_in_seconds"}),
        }:
            raise MissionStateError(f"{kind} payload fields do not match schema")
        if payload["decision"] != "approved" or payload["risk"] not in {
            "high",
            "unknown",
        }:
            raise MissionStateError("assurance approval payload is invalid")
        _require_uuid(payload["approval_id"], "approval_id")
        for field in ("request_event_sha256", "workflow_digest", "approved_by_sha256"):
            _require_sha(payload[field], f"assurance approval {field}")
        parse_timestamp(payload["expires_at"], "assurance approval expiry")
        if (
            "expires_in_seconds" in payload
            and not 60
            <= _require_uint(
                payload["expires_in_seconds"], "assurance approval expires_in_seconds"
            )
            <= 86400
        ):
            raise MissionStateError(
                "assurance approval expires_in_seconds is outside policy"
            )
        _require_nonempty(payload["scope"], "assurance approval scope")
    elif kind == "assurance_approval_renewed":
        _require_fields(
            kind,
            payload,
            {
                "approval_id",
                "prior_approval_event_sha256",
                "request_event_sha256",
                "workflow_digest",
                "scope",
                "risk",
                "expires_at",
                "expires_in_seconds",
                "approved_by_sha256",
                "decision",
            },
        )
        if payload["decision"] != "approved" or payload["risk"] not in {
            "high",
            "unknown",
        }:
            raise MissionStateError("assurance approval renewal payload is invalid")
        _require_uuid(payload["approval_id"], "renewed approval_id")
        for field in (
            "prior_approval_event_sha256",
            "request_event_sha256",
            "workflow_digest",
            "approved_by_sha256",
        ):
            _require_sha(payload[field], f"assurance approval renewal {field}")
        parse_timestamp(payload["expires_at"], "assurance approval renewal expiry")
        if (
            not 60
            <= _require_uint(
                payload["expires_in_seconds"], "assurance renewal expires_in_seconds"
            )
            <= 86400
        ):
            raise MissionStateError(
                "assurance renewal expires_in_seconds is outside policy"
            )
        _require_nonempty(payload["scope"], "assurance approval renewal scope")
    elif kind == "assurance_boot_started":
        _require_fields(kind, payload, {"preset", "approval_event_sha256"})
        _require_nonempty(payload["preset"], "assurance preset")
        _require_sha(
            payload["approval_event_sha256"], "assurance boot approval reference"
        )
    elif kind == "assurance_started":
        _require_fields(kind, payload, {"manifest", "approval_event_sha256"})
        _require_nonempty(payload["manifest"], "assurance manifest")
        _require_sha(
            payload["approval_event_sha256"], "assurance started approval reference"
        )
    elif kind == "mission_completing":
        _require_fields(kind, payload, {"lead_artifact_id"})
        _require_sha(payload["lead_artifact_id"], "mission lead_artifact_id")
    elif kind == "archive_created":
        _require_fields(kind, payload, {"path", "sha256", "mode"})
        _require_nonempty(payload["path"], "archive path")
        _require_sha(payload["sha256"], "archive sha256")
        _require_nonempty(payload["mode"], "archive mode")
    elif kind == "mission_terminal":
        _require_fields(kind, payload, {"status", "reason"})
        if payload["status"] not in TERMINAL_STATUSES:
            raise MissionStateError("invalid mission terminal status")
        _require_nonempty(payload["reason"], "mission terminal reason")
    elif kind == "lead_dispatch_intent":
        _require_fields(kind, payload, {"prompt_sha256"})
        _require_sha(payload["prompt_sha256"], "lead prompt_sha256")
    elif kind == "lead_dispatched":
        _require_fields(kind, payload, {"run_id", "prompt_sha256"})
        _require_uuid(payload["run_id"], "lead run_id")
        _require_sha(payload["prompt_sha256"], "lead prompt_sha256")
    elif kind == "lead_result_recorded":
        _require_fields(
            kind,
            payload,
            {"run_id", "artifact_id", "result_file", "provider", "model", "variant"},
        )
        _require_uuid(payload["run_id"], "lead run_id")
        _require_sha(payload["artifact_id"], "lead result artifact_id")
        _require_nonempty(payload["result_file"], "lead result file")
        for field in ("provider", "model"):
            _require_nonempty(payload[field], f"lead result {field}")
        if payload["variant"] is not None and not isinstance(payload["variant"], str):
            raise MissionStateError("lead result variant is invalid")
    elif kind == "synthesis_result_recorded":
        _require_fields(
            kind,
            payload,
            {
                "run_id",
                "admission_id",
                "artifact_id",
                "result_file",
                "provider",
                "model",
                "variant",
            },
        )
        _require_uuid(payload["run_id"], "synthesis run_id")
        _require_uuid(payload["admission_id"], "synthesis admission_id")
        _require_sha(payload["artifact_id"], "synthesis artifact_id")
        _require_nonempty(payload["result_file"], "synthesis result file")
        for field in ("provider", "model"):
            _require_nonempty(payload[field], f"synthesis result {field}")
        if payload["variant"] is not None and not isinstance(payload["variant"], str):
            raise MissionStateError("synthesis result variant is invalid")
    elif kind == "capability_token_issued":
        _require_fields(kind, payload, {"token_id", "delegation_id", "token_sha256"})
        _require_uuid(payload["token_id"], "token_id")
        _require_uuid(payload["delegation_id"], "delegation_id")
        _require_sha(payload["token_sha256"], "capability token sha256")
    elif kind == "capability_token_bound":
        _require_fields(kind, payload, {"token_id", "delegation_id", "run_id"})
        for field in ("token_id", "delegation_id", "run_id"):
            _require_uuid(payload[field], field)
    elif kind == "delegation_budget_allocated":
        _require_fields(
            kind, payload, {"token_id", "delegation_id", "parent_run_id", "allocations"}
        )
        for field in ("token_id", "delegation_id", "parent_run_id"):
            _require_uuid(payload[field], field)
        allocations = payload["allocations"]
        if not isinstance(allocations, list) or not allocations:
            raise MissionStateError("delegation budget allocations must be non-empty")
        allocation_fields = {
            "requested_delegation_id",
            "capability",
            "child_can_delegate",
            "child_allowed_capabilities",
            "requested_artifact_ids",
            "edge_cost",
            "delegated_budget",
            "total_cost",
        }
        ids: set[str] = set()
        for allocation in allocations:
            if not isinstance(allocation, dict) or set(allocation) != allocation_fields:
                raise MissionStateError(
                    "delegation budget allocation fields do not match schema"
                )
            child_id = _require_uuid(
                allocation["requested_delegation_id"], "requested_delegation_id"
            )
            if child_id in ids:
                raise MissionStateError(
                    "delegation budget allocations contain duplicate IDs"
                )
            ids.add(child_id)
            _require_nonempty(allocation["capability"], "delegation budget capability")
            if not isinstance(allocation["child_can_delegate"], bool):
                raise MissionStateError("child_can_delegate must be boolean")
            _require_string_list(
                allocation["child_allowed_capabilities"],
                "child_allowed_capabilities",
                unique=True,
            )
            artifacts = allocation["requested_artifact_ids"]
            if (
                not isinstance(artifacts, list)
                or any(
                    not isinstance(item, str) or not SHA256.fullmatch(item)
                    for item in artifacts
                )
                or len(artifacts) != len(set(artifacts))
            ):
                raise MissionStateError(
                    "requested_artifact_ids must be unique SHA-256 values"
                )
            for field in ("edge_cost", "delegated_budget", "total_cost"):
                _require_uint(allocation[field], field)
            if allocation["edge_cost"] != 1 or allocation["total_cost"] != (
                1 + allocation["delegated_budget"]
            ):
                raise MissionStateError("delegation budget allocation cost is invalid")
    elif kind == "delegation_dispatch_intent":
        _require_fields(
            kind, payload, {"delegation_id", "recipient_instance", "prompt_sha256"}
        )
        _require_uuid(payload["delegation_id"], "delegation_id")
        _require_nonempty(
            payload["recipient_instance"], "delegation recipient_instance"
        )
        _require_sha(payload["prompt_sha256"], "delegation prompt_sha256")
    elif kind == "delegation_registered":
        _require_fields(
            kind,
            payload,
            {
                "delegation_id",
                "mission_id",
                "run_id",
                "parent_run_id",
                "delegated_by",
                "recipient_instance",
                "capability",
                "objective_sha256",
                "input_artifact_ids",
                "expected_output_contract",
                "deadline",
                "provider",
                "model",
                "variant",
                "depth",
                "token_id",
            },
        )
        for field in ("delegation_id", "mission_id", "run_id"):
            _require_uuid(payload[field], field)
        for field in ("parent_run_id", "token_id"):
            if payload[field] is not None:
                _require_uuid(payload[field], field)
        _require_sha(payload["objective_sha256"], "delegation objective_sha256")
        artifacts = payload["input_artifact_ids"]
        if (
            not isinstance(artifacts, list)
            or any(
                not isinstance(item, str) or not SHA256.fullmatch(item)
                for item in artifacts
            )
            or len(artifacts) != len(set(artifacts))
        ):
            raise MissionStateError("delegation input_artifact_ids are invalid")
        if not isinstance(payload["expected_output_contract"], dict):
            raise MissionStateError(
                "delegation expected_output_contract must be an object"
            )
        _require_uint(payload["depth"], "delegation depth")
        parse_timestamp(payload["deadline"], "delegation deadline")
        for field in (
            "delegated_by",
            "recipient_instance",
            "capability",
            "provider",
            "model",
        ):
            _require_nonempty(payload[field], f"delegation {field}")
        if payload["variant"] is not None and not isinstance(payload["variant"], str):
            raise MissionStateError("delegation variant is invalid")
    elif kind == "result_recorded":
        _require_fields(
            kind,
            payload,
            {"run_id", "delegation_id", "artifact_id", "provider", "model", "variant"},
        )
        _require_uuid(payload["run_id"], "run_id")
        _require_uuid(payload["delegation_id"], "delegation_id")
        _require_sha(payload["artifact_id"], "result artifact_id")
        for field in ("provider", "model"):
            _require_nonempty(payload[field], f"result {field}")
        if payload["variant"] is not None and not isinstance(payload["variant"], str):
            raise MissionStateError("result variant is invalid")
    elif kind == "result_relayed":
        _require_fields(
            kind, payload, {"artifact_id", "recipient_run_id", "recipient_instance"}
        )
        _require_sha(payload["artifact_id"], "relayed artifact_id")
        _require_uuid(payload["recipient_run_id"], "recipient_run_id")
        _require_nonempty(payload["recipient_instance"], "relay recipient_instance")
    elif kind == "human_approval_requested":
        _require_fields(kind, payload, {"reason", "scope"})
        _require_nonempty(payload["reason"], "human approval reason")
        _require_nonempty(payload["scope"], "human approval scope")
    elif kind == "human_decision_requested":
        validate_decision_request_payload(payload)
    elif kind == "human_decision_resolved":
        validate_decision_resolution_payload(payload)
    elif kind == "run_cancel_requested":
        _require_fields(kind, payload, {"run_id", "reason"})
        _require_uuid(payload["run_id"], "cancel run_id")
        _require_nonempty(payload["reason"], "run cancel reason")
    elif kind == "lead_completion_requested":
        _require_fields(kind, payload, {"artifact_id", "summary"})
        _require_sha(payload["artifact_id"], "lead completion artifact_id")
        _require_nonempty(payload["summary"], "lead completion summary")
    elif kind in {"assured_action_intent", "assured_action_completed"}:
        _validate_assured_action(kind, payload)
    elif kind == "mission_admission_policy_frozen":
        _require_fields(
            kind,
            payload,
            {
                "workflow_digest",
                "compiled_digest",
                "deadline_at",
                "delegation_credits",
                "max_active_delegations",
            },
        )
        _require_sha(payload["workflow_digest"], "admission policy workflow_digest")
        _require_sha(payload["compiled_digest"], "admission policy compiled_digest")
        parse_timestamp(payload["deadline_at"], "admission policy deadline_at")
        _require_uint(
            payload["delegation_credits"], "admission policy delegation_credits"
        )
        if (
            _require_uint(
                payload["max_active_delegations"],
                "admission policy max_active_delegations",
            )
            < 1
        ):
            raise MissionStateError(
                "admission policy max_active_delegations must be positive"
            )
    elif kind == "delegations_reserved":
        legacy_payload_fields = {"batch_id", "batch_sha256", "admissions"}
        current_payload_fields = legacy_payload_fields | {"request_sha256"}
        if set(payload) not in {
            frozenset(legacy_payload_fields),
            frozenset(current_payload_fields),
        }:
            raise MissionStateError(f"{kind} payload fields do not match schema")
        _require_uuid(payload["batch_id"], "admission batch_id")
        _require_sha(payload["batch_sha256"], "admission batch_sha256")
        current_schema = "request_sha256" in payload
        if current_schema:
            _require_sha(payload["request_sha256"], "admission request_sha256")
        admissions = payload["admissions"]
        if not isinstance(admissions, list) or not admissions:
            raise MissionStateError("admission batch must be non-empty")
        legacy_expected = {
            "admission_id",
            "delegation_id",
            "run_id",
            "run_kind",
            "request_key",
            "request_digest",
            "recipient_instance",
            "capability",
            "parent_admission_id",
            "parent_run_id",
            "delegated_budget",
            "credit_cost",
            "global_credit_debit",
            "parent_credit_debit",
            "writer",
        }
        historical_current = legacy_expected | {"effect_sha256"}
        expected_schemas = (
            {frozenset(legacy_expected)}
            if not current_schema
            else {
                frozenset(historical_current),
                frozenset(historical_current | {"task_sha256"}),
            }
        )
        seen_admissions: set[str] = set()
        seen_runs: set[str] = set()
        seen_recipients: set[str] = set()
        for admission in admissions:
            if (
                not isinstance(admission, dict)
                or frozenset(admission) not in expected_schemas
            ):
                raise MissionStateError("admission fields do not match schema")
            admission_id = _require_uuid(admission["admission_id"], "admission_id")
            run_id = _require_uuid(admission["run_id"], "admission run_id")
            if admission_id in seen_admissions or run_id in seen_runs:
                raise MissionStateError("admission batch contains duplicate IDs")
            seen_admissions.add(admission_id)
            seen_runs.add(run_id)
            if admission["run_kind"] not in {"lead", "specialist"}:
                raise MissionStateError("admission run_kind is invalid")
            if admission["run_kind"] == "lead":
                if admission["delegation_id"] is not None:
                    raise MissionStateError("Lead admission cannot have delegation_id")
            else:
                _require_uuid(admission["delegation_id"], "delegation_id")
            for field in ("parent_admission_id", "parent_run_id"):
                if admission[field] is not None:
                    _require_uuid(admission[field], field)
            if not isinstance(admission["request_key"], str) or not SAFE_KEY.fullmatch(
                admission["request_key"]
            ):
                raise MissionStateError("admission request_key is invalid")
            _require_sha(admission["request_digest"], "admission request_digest")
            if current_schema:
                _require_sha(admission["effect_sha256"], "admission effect_sha256")
                if "task_sha256" in admission:
                    _require_sha(admission["task_sha256"], "admission task_sha256")
            for field in ("recipient_instance", "capability"):
                if not isinstance(admission[field], str) or not SAFE_FEATURE.fullmatch(
                    admission[field]
                ):
                    raise MissionStateError(f"admission {field} is invalid")
            recipient = admission["recipient_instance"]
            if recipient in seen_recipients:
                raise MissionStateError("admission batch contains duplicate recipients")
            seen_recipients.add(recipient)
            for field in (
                "delegated_budget",
                "credit_cost",
                "global_credit_debit",
                "parent_credit_debit",
            ):
                _require_uint(admission[field], f"admission {field}")
            if not isinstance(admission["writer"], bool):
                raise MissionStateError("admission writer must be boolean")
    elif kind == "delegation_committed":
        legacy_fields = {
            "admission_id",
            "commit_id",
            "reservation_event_sha256",
            "request_digest",
            "recipient_instance",
            "writer",
            "run_id",
        }
        if set(payload) not in {
            frozenset(legacy_fields),
            frozenset(legacy_fields | {"effect_sha256"}),
        }:
            raise MissionStateError(f"{kind} payload fields do not match schema")
        for field in ("admission_id", "commit_id", "run_id"):
            _require_uuid(payload[field], field)
        _require_sha(
            payload["reservation_event_sha256"], "delegation reservation reference"
        )
        _require_sha(payload["request_digest"], "delegation request_digest")
        if "effect_sha256" in payload:
            _require_sha(payload["effect_sha256"], "delegation effect_sha256")
        if not isinstance(
            payload["recipient_instance"], str
        ) or not SAFE_FEATURE.fullmatch(payload["recipient_instance"]):
            raise MissionStateError("delegation commit recipient_instance is invalid")
        if not isinstance(payload["writer"], bool):
            raise MissionStateError("delegation commit writer must be boolean")
    elif kind == "delegation_launch_authorized":
        historical_fields = {
            "admission_id",
            "commit_event_sha256",
            "request_digest",
            "effect_sha256",
            "recipient_instance",
            "writer",
            "run_id",
        }
        if set(payload) not in {
            frozenset(historical_fields),
            frozenset(historical_fields | {"approval_event_sha256"}),
        }:
            raise MissionStateError(f"{kind} payload fields do not match schema")
        _require_uuid(payload["admission_id"], "admission_id")
        _require_uuid(payload["run_id"], "run_id")
        _require_sha(
            payload["commit_event_sha256"], "delegation launch commit reference"
        )
        _require_sha(payload["request_digest"], "delegation launch request_digest")
        _require_sha(payload["effect_sha256"], "delegation launch effect_sha256")
        if payload.get("approval_event_sha256") is not None:
            _require_sha(
                payload["approval_event_sha256"],
                "delegation launch approval reference",
            )
        if not isinstance(
            payload["recipient_instance"], str
        ) or not SAFE_FEATURE.fullmatch(payload["recipient_instance"]):
            raise MissionStateError("delegation launch recipient_instance is invalid")
        if not isinstance(payload["writer"], bool):
            raise MissionStateError("delegation launch writer must be boolean")
    elif kind == "delegation_started":
        legacy_fields = {
            "admission_id",
            "commit_event_sha256",
            "expected_head_sha256",
            "request_digest",
            "recipient_instance",
            "writer",
            "run_id",
        }
        current_fields = {
            "admission_id",
            "authorization_event_sha256",
            "request_digest",
            "effect_sha256",
            "recipient_instance",
            "writer",
            "run_id",
        }
        if set(payload) not in {frozenset(legacy_fields), frozenset(current_fields)}:
            raise MissionStateError(f"{kind} payload fields do not match schema")
        _require_uuid(payload["admission_id"], "admission_id")
        _require_uuid(payload["run_id"], "run_id")
        _require_sha(payload["request_digest"], "delegation started request_digest")
        if "authorization_event_sha256" in payload:
            _require_sha(
                payload["authorization_event_sha256"],
                "delegation started authorization reference",
            )
            _require_sha(payload["effect_sha256"], "delegation started effect_sha256")
        else:
            _require_sha(
                payload["commit_event_sha256"], "delegation started commit reference"
            )
            _require_sha(
                payload["expected_head_sha256"], "delegation started head reference"
            )
        if not isinstance(
            payload["recipient_instance"], str
        ) or not SAFE_FEATURE.fullmatch(payload["recipient_instance"]):
            raise MissionStateError("delegation started recipient_instance is invalid")
        if not isinstance(payload["writer"], bool):
            raise MissionStateError("delegation started writer must be boolean")
    elif kind == "delegation_finalized":
        legacy_fields = {"admission_id", "status", "reason"}
        historical_current_fields = legacy_fields | {
            "run_id",
            "recipient_instance",
            "writer",
            "terminal_evidence_sha256",
        }
        source_bound_fields = historical_current_fields | {"terminal_source_sha256"}
        current_fields = historical_current_fields | {"terminal_evidence"}
        if set(payload) not in {
            frozenset(legacy_fields),
            frozenset(historical_current_fields),
            frozenset(source_bound_fields),
            frozenset(current_fields),
        }:
            raise MissionStateError(f"{kind} payload fields do not match schema")
        _require_uuid(payload["admission_id"], "admission_id")
        if "run_id" in payload:
            _require_uuid(payload["run_id"], "run_id")
            if not isinstance(
                payload["recipient_instance"], str
            ) or not SAFE_FEATURE.fullmatch(payload["recipient_instance"]):
                raise MissionStateError(
                    "delegation final recipient_instance is invalid"
                )
            if not isinstance(payload["writer"], bool):
                raise MissionStateError("delegation final writer must be boolean")
            _require_sha(
                payload["terminal_evidence_sha256"],
                "delegation terminal evidence",
            )
            if "terminal_source_sha256" in payload:
                _require_sha(
                    payload["terminal_source_sha256"],
                    "delegation terminal source",
                )
            if "terminal_evidence" in payload:
                evidence = payload["terminal_evidence"]
                if not isinstance(evidence, dict) or set(evidence) != {
                    "schema_version",
                    "source_event_sha256",
                    "run_id",
                    "task_sha256",
                    "status",
                }:
                    raise MissionStateError(
                        "delegation terminal evidence fields do not match schema"
                    )
                if type(evidence["schema_version"]) is not int or (
                    evidence["schema_version"] != 1
                ):
                    raise MissionStateError(
                        "delegation terminal evidence schema_version is invalid"
                    )
                _require_sha(
                    evidence["source_event_sha256"],
                    "delegation terminal source event",
                )
                _require_uuid(evidence["run_id"], "delegation terminal evidence run_id")
                _require_sha(
                    evidence["task_sha256"],
                    "delegation terminal evidence task_sha256",
                )
                if evidence["status"] not in TERMINAL_STATUSES:
                    raise MissionStateError(
                        "delegation terminal evidence status is invalid"
                    )
        if payload["status"] not in TERMINAL_STATUSES:
            raise MissionStateError("delegation finalized status is invalid")
        _require_nonempty(payload["reason"], "delegation finalized reason")
    elif kind == "delegation_reservation_aborted":
        legacy_fields = {"admission_id", "reason"}
        current_fields = legacy_fields | {
            "run_id",
            "request_digest",
            "effect_sha256",
            "task_sha256",
            "recipient_instance",
            "writer",
        }
        if set(payload) not in {
            frozenset(legacy_fields),
            frozenset(current_fields),
        }:
            raise MissionStateError(f"{kind} payload fields do not match schema")
        _require_uuid(payload["admission_id"], "admission_id")
        if "run_id" in payload:
            _require_uuid(payload["run_id"], "prelaunch abort run_id")
            for field in ("request_digest", "effect_sha256", "task_sha256"):
                _require_sha(payload[field], f"prelaunch abort {field}")
            if not isinstance(
                payload["recipient_instance"], str
            ) or not SAFE_FEATURE.fullmatch(payload["recipient_instance"]):
                raise MissionStateError("prelaunch abort recipient_instance is invalid")
            if not isinstance(payload["writer"], bool):
                raise MissionStateError("prelaunch abort writer must be boolean")
        _require_nonempty(payload["reason"], "delegation abort reason")
    else:
        raise MissionStateError(f"unsupported mission event kind: {kind}")


def _validate_event(
    event: Any, previous: dict[str, Any] | None, mission_id: str
) -> None:
    if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
        raise MissionStateError("mission event fields do not match schema_version=1")
    if (
        isinstance(event["schema_version"], bool)
        or not isinstance(event["schema_version"], int)
        or event["schema_version"] != SCHEMA_VERSION
    ):
        raise MissionStateError("unsupported mission event schema_version")
    _require_uuid(event["event_id"], "event_id")
    if _require_uuid(event["mission_id"], "mission_id") != mission_id:
        raise MissionStateError("event mission_id mismatch")
    expected_sequence = 1 if previous is None else previous["sequence"] + 1
    if (
        isinstance(event["sequence"], bool)
        or not isinstance(event["sequence"], int)
        or event["sequence"] != expected_sequence
    ):
        raise MissionStateError("mission event sequence gap")
    if not isinstance(event["timestamp"], str):
        raise MissionStateError("mission event timestamp must be a string")
    timestamp = parse_timestamp(event["timestamp"], "mission event timestamp")
    if previous is not None and timestamp <= parse_timestamp(
        previous["timestamp"], "previous mission event timestamp"
    ):
        raise MissionStateError("mission event timestamps must be strictly increasing")
    if not isinstance(event["kind"], str) or not SAFE_KIND.fullmatch(event["kind"]):
        raise MissionStateError("invalid mission event kind")
    if not isinstance(event["actor"], str) or not SAFE_ACTOR.fullmatch(event["actor"]):
        raise MissionStateError("invalid mission event actor")
    if not isinstance(event["idempotency_key"], str) or not SAFE_KEY.fullmatch(
        event["idempotency_key"]
    ):
        raise MissionStateError("invalid mission idempotency key")
    expected_previous = GENESIS_SHA256 if previous is None else previous["event_sha256"]
    if (
        not isinstance(event["previous_event_sha256"], str)
        or event["previous_event_sha256"] != expected_previous
    ):
        raise MissionStateError("mission event hash chain is broken")
    stored_hash = event["event_sha256"]
    if not isinstance(stored_hash, str) or not SHA256.fullmatch(stored_hash):
        raise MissionStateError("invalid mission event hash")
    unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
    if sha256(unsigned) != stored_hash:
        raise MissionStateError("mission event hash mismatch")
    _validate_payload(event["kind"], event["payload"])
    if event["kind"] == "human_decision_requested":
        expected_decision_id = str(
            uuid.uuid5(
                uuid.UUID(mission_id),
                f"decision:{event['idempotency_key']}",
            )
        )
        if event["payload"]["decision_id"] != expected_decision_id:
            raise MissionStateError("human decision deterministic identity mismatch")


def _events_from_bytes(
    content: bytes,
    *,
    expected_mission_id: str | None,
    require_nonempty: bool,
) -> list[dict[str, Any]]:
    mission_id = expected_mission_id
    events: list[dict[str, Any]] = []
    if content and not content.endswith(b"\n"):
        raise MissionStateError("partial mission event at final line")
    try:
        parsed = fleet_json.load_jsonl(
            content,
            require_nonempty=require_nonempty,
            require_final_newline=True,
        )
    except fleet_json.FleetJSONError as exc:
        raise MissionStateError(f"invalid mission JSON: {exc}") from exc
    try:
        if content != fleet_json.canonical_jsonl(parsed):
            raise MissionStateError("mission JSONL bytes are not canonical")
    except fleet_json.FleetJSONError as exc:
        raise MissionStateError(f"invalid mission JSON: {exc}") from exc
    for event in parsed:
        if mission_id is None:
            mission_id = normalize_uuid(str(event.get("mission_id", "")), "mission_id")
        _validate_event(event, events[-1] if events else None, mission_id)
        events.append(event)
    if require_nonempty and not events:
        raise MissionStateError("mission ledger is empty")
    return events


def _rooted_mission_ledger(
    path: Path, expected_mission_id: str | None
) -> tuple[Path, Path] | None:
    if expected_mission_id is None:
        return None
    mission_id = normalize_uuid(expected_mission_id, "mission_id")
    if (
        path.name != "mission.jsonl"
        or path.parent.name != mission_id
        or path.parent.parent.name != "missions"
    ):
        return None
    return path.parent.parent.parent, Path("missions") / mission_id / "mission.jsonl"


def _validate_snapshot_regular(
    info: os.stat_result,
    *,
    where: str,
    link_counts: set[int],
) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink not in link_counts
    ):
        raise MissionStateError(f"unsafe {where}")


def _read_rooted_mission_snapshot(
    rooted: fleet_safe_paths.RootedFS,
    *,
    mission_id: str,
) -> bytes:
    """Read one linearizable inode snapshot without delaying ledger writers.

    A writer publishes only by atomic rename while holding the Mission lock.  If
    publication crosses this read, the opened old inode is still one complete,
    previously-current snapshot and has link count zero after replacement.
    """

    mission_fd = rooted._open_directory_chain(
        ("missions", mission_id),
        (0o700, 0o700),
        create=False,
    )
    try:
        mission_info = os.fstat(mission_fd)
        mission_identity = (mission_info.st_dev, mission_info.st_ino)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            ledger_fd = os.open("mission.jsonl", flags, dir_fd=mission_fd)
        except FileNotFoundError as exc:
            raise _MissionSnapshotChanged("mission ledger is not published") from exc
        except OSError as exc:
            raise MissionStateError("cannot open pinned mission ledger") from exc
        try:
            _mission_snapshot_checkpoint("after_fd_open")
            opened = os.fstat(ledger_fd)
            _validate_snapshot_regular(
                opened,
                where="mission ledger snapshot",
                link_counts={0, 1},
            )
            if opened.st_nlink == 0:
                raise _MissionSnapshotChanged(
                    "mission ledger publication crossed descriptor open"
                )
            _mission_snapshot_checkpoint("after_open")
            try:
                bound = os.stat(
                    "mission.jsonl", dir_fd=mission_fd, follow_symlinks=False
                )
            except FileNotFoundError as exc:
                raise _MissionSnapshotChanged(
                    "mission ledger publication crossed open"
                ) from exc
            if (bound.st_dev, bound.st_ino) != (opened.st_dev, opened.st_ino):
                raise _MissionSnapshotChanged(
                    "mission ledger publication crossed open"
                )

            remaining = opened.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(ledger_fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise MissionStateError("mission ledger changed during snapshot read")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(ledger_fd, 1):
                raise MissionStateError("mission ledger grew during snapshot read")
            final = os.fstat(ledger_fd)
            if (
                (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino)
                or final.st_size != opened.st_size
            ):
                raise MissionStateError("mission ledger changed during snapshot read")
            _validate_snapshot_regular(
                final,
                where="mission ledger snapshot",
                link_counts={0, 1},
            )
            try:
                current = os.stat(
                    "mission.jsonl", dir_fd=mission_fd, follow_symlinks=False
                )
            except FileNotFoundError as exc:
                raise _MissionSnapshotChanged(
                    "mission ledger publication did not stabilize"
                ) from exc
            if (current.st_dev, current.st_ino) == (final.st_dev, final.st_ino):
                _validate_snapshot_regular(
                    current,
                    where="mission ledger snapshot",
                    link_counts={1},
                )
                if final.st_nlink != 1:
                    raise _MissionSnapshotChanged(
                        "mission ledger publication crossed validation"
                    )
            else:
                # os.replace unlinks the prior directory entry but an open
                # descriptor preserves its complete bytes until this read ends.
                final = os.fstat(ledger_fd)
                _validate_snapshot_regular(
                    final,
                    where="replaced mission ledger snapshot",
                    link_counts={0},
                )
                _validate_snapshot_regular(
                    current,
                    where="current mission ledger",
                    link_counts={1},
                )

            rooted.assert_root_binding()
            try:
                current_mission_fd = rooted._open_directory_chain(
                    ("missions", mission_id),
                    (0o700, 0o700),
                    create=False,
                )
            except fleet_safe_paths.SafePathError as exc:
                raise MissionStateError("mission ledger directory path changed") from exc
            try:
                current_mission = os.fstat(current_mission_fd)
                if (current_mission.st_dev, current_mission.st_ino) != mission_identity:
                    raise MissionStateError("mission ledger directory path changed")
            finally:
                os.close(current_mission_fd)
            return b"".join(chunks)
        finally:
            os.close(ledger_fd)
    finally:
        os.close(mission_fd)


def read_events(
    path: Path, *, expected_mission_id: str | None = None
) -> list[dict[str, Any]]:
    rooted_binding = _rooted_mission_ledger(path, expected_mission_id)
    if rooted_binding is not None:
        runs_dir, _ = rooted_binding
        try:
            with fleet_safe_paths.RootedFS(runs_dir) as rooted:
                for attempt in range(MISSION_SNAPSHOT_ATTEMPTS):
                    try:
                        content = _read_rooted_mission_snapshot(
                            rooted,
                            mission_id=normalize_uuid(
                                expected_mission_id, "mission_id"
                            ),
                        )
                        break
                    except _MissionSnapshotChanged as exc:
                        if attempt + 1 == MISSION_SNAPSHOT_ATTEMPTS:
                            raise MissionStateError(
                                "mission ledger snapshot did not stabilize"
                            ) from exc
                        time.sleep(MISSION_SNAPSHOT_RETRY_SECONDS)
        except fleet_safe_paths.SafePathError as exc:
            raise MissionStateError(f"unsafe mission ledger path: {exc}") from exc
        return _events_from_bytes(
            content,
            expected_mission_id=expected_mission_id,
            require_nonempty=expected_mission_id is not None,
        )
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return []
    return _events_from_bytes(
        content,
        expected_mission_id=expected_mission_id,
        require_nonempty=expected_mission_id is not None,
    )


def _admission_request_binding(admission: dict[str, Any]) -> dict[str, Any]:
    binding = {
        field: admission[field]
        for field in (
            "request_key",
            "run_kind",
            "recipient_instance",
            "capability",
            "parent_admission_id",
            "parent_run_id",
            "delegated_budget",
            "writer",
        )
    }
    if "effect_sha256" in admission:
        binding["effect_sha256"] = admission["effect_sha256"]
    if "task_sha256" in admission:
        binding["task_sha256"] = admission["task_sha256"]
    return binding


def _release_admission(result: dict[str, Any], admission: dict[str, Any]) -> None:
    if not admission["active"]:
        return
    admission["active"] = False
    if admission["run_kind"] == "specialist":
        result["active_delegations"] -= 1
    recipient = admission["recipient_instance"]
    if result["active_recipients"].get(recipient) == admission["admission_id"]:
        del result["active_recipients"][recipient]
    if result["active_writer"] == admission["admission_id"]:
        result["active_writer"] = None


def _require_admission_deadline(result: dict[str, Any], event: dict[str, Any]) -> None:
    policy = result["admission_policy"]
    if policy is None:
        raise MissionStateError("admission policy is not frozen")
    if parse_timestamp(
        event["timestamp"], "admission event timestamp"
    ) >= parse_timestamp(policy["deadline_at"], "admission deadline"):
        raise MissionConflict("mission admission deadline has passed")


def _require_live_assurance_approval(
    result: dict[str, Any], event: dict[str, Any], where: str
) -> str:
    approval = result.get("approval")
    if not isinstance(approval, dict):
        raise MissionConflict(f"{where} requires an active assurance approval")
    if any(
        (
            approval["workflow_digest"] != result["workflow_digest"],
            approval["scope"] != result["target_repo"],
            approval["risk"] != result["risk"],
        )
    ):
        raise MissionConflict(f"{where} approval no longer matches mission authority")
    if parse_timestamp(event["timestamp"], f"{where} timestamp") >= parse_timestamp(
        approval["expires_at"], "assurance approval expiry"
    ):
        raise MissionConflict(f"{where} assurance approval has expired")
    return approval["event_sha256"]


def _require_admission_lane(result: dict[str, Any], event: dict[str, Any]) -> str:
    """Return the only actor allowed to mutate admissions in this lifecycle lane."""

    status = result["status"]
    if status in {"compiled", "booting", "running"}:
        expected_actor = "CONTROL"
    elif status in {"assured_booting", "assured_running"}:
        expected_actor = "ASSURED"
    else:
        raise MissionConflict("mission no longer accepts admission activity")
    if event["actor"] != expected_actor:
        raise MissionConflict(
            f"{status} admission lane requires {expected_actor} actor"
        )
    if expected_actor == "ASSURED":
        _require_live_assurance_approval(result, event, "ASSURED admission")
    return expected_actor


def _require_launch_authorization_lane(
    result: dict[str, Any], event: dict[str, Any]
) -> str | None:
    status = result["status"]
    expected_actor = {
        "running": "CONTROL",
        "assured_running": "ASSURED",
    }.get(status)
    if expected_actor is None:
        raise MissionConflict("launch authorization requires a running admission lane")
    if event["actor"] != expected_actor:
        raise MissionConflict(
            f"{status} launch authorization requires {expected_actor} actor"
        )
    if expected_actor == "ASSURED":
        return _require_live_assurance_approval(
            result, event, "ASSURED launch authorization"
        )
    return None


def _require_admission_actor(admission: dict[str, Any], event: dict[str, Any]) -> None:
    if admission.get("lane_actor") != event["actor"]:
        raise MissionConflict("admission actor does not match its sealed lane")


def _require_no_active_admissions(result: dict[str, Any], where: str) -> None:
    if (
        result["run_claims"]
        or result["active_writer"] is not None
        or any(admission["active"] for admission in result["admissions"].values())
    ):
        raise MissionConflict(f"{where} requires all admissions to be inactive")


def _blocking_decision_ids(
    result: dict[str, Any], *, recipient_instance: str | None = None
) -> list[str]:
    values: list[str] = []
    for decision_id, decision in result.get("pending_decisions", {}).items():
        request = decision.get("request") if isinstance(decision, dict) else None
        if not isinstance(request, dict) or request.get("impact") != "blocking":
            continue
        if recipient_instance is None or recipient_instance in request.get(
            "affected_instances", []
        ):
            values.append(str(decision_id))
    return sorted(values)


def _require_no_pending_decisions(result: dict[str, Any], where: str) -> None:
    pending = sorted(str(value) for value in result.get("pending_decisions", {}))
    if pending:
        raise MissionConflict(
            f"{where} is blocked by pending human decisions: {', '.join(pending)}"
        )


def _decision_evidence_identity(
    result: dict[str, Any], evidence: dict[str, Any], where: str
) -> tuple[str, str, str | None]:
    delegation_id = evidence["delegation_id"]
    recorded = result["results"].get(delegation_id)
    delegation = result["delegations"].get(delegation_id)
    if (
        not isinstance(recorded, dict)
        or not isinstance(delegation, dict)
        or recorded.get("artifact_id") != evidence["artifact_id"]
        or delegation.get("recipient_instance") != evidence["instance"]
        or recorded.get("run_id") != delegation.get("run_id")
    ):
        raise MissionConflict(f"{where} lacks exact attested result lineage")
    return (
        str(recorded["provider"]),
        str(recorded["model"]),
        recorded.get("variant"),
    )


def _reserve_admissions(
    result: dict[str, Any], event: dict[str, Any], payload: dict[str, Any]
) -> None:
    lane_actor = _require_admission_lane(result, event)
    _require_admission_deadline(result, event)
    admissions = payload["admissions"]
    if payload["batch_sha256"] != sha256(admissions):
        raise MissionStateError("admission batch digest mismatch")
    batch_id = payload["batch_id"]
    if batch_id in result["admission_batches"]:
        raise MissionConflict("admission batch is immutable")

    prospective: dict[str, dict[str, Any]] = {}
    mission_namespace = uuid.UUID(result["mission_id"])
    for admission in admissions:
        admission_id = admission["admission_id"]
        run_id = admission["run_id"]
        request_key = admission["request_key"]
        expected_admission_id = str(
            uuid.uuid5(
                mission_namespace, f"admission:{admission['run_kind']}:{request_key}"
            )
        )
        expected_run_id = str(
            uuid.uuid5(mission_namespace, f"run:{admission['run_kind']}:{request_key}")
        )
        if admission_id != expected_admission_id or run_id != expected_run_id:
            raise MissionStateError("admission deterministic identity mismatch")
        expected_delegation_id = (
            None
            if admission["run_kind"] == "lead"
            else str(uuid.uuid5(mission_namespace, f"delegation:{request_key}"))
        )
        if admission["delegation_id"] != expected_delegation_id:
            raise MissionStateError("admission delegation identity mismatch")
        if admission["request_digest"] != sha256(_admission_request_binding(admission)):
            raise MissionStateError("admission request digest mismatch")
        if admission_id in result["admissions"] or admission_id in prospective:
            raise MissionConflict("admission_id was already reserved")
        if run_id in result["run_claims"] or run_id in result["run_owners"]:
            raise MissionConflict("run_id is already owned or reserved")
        recipient = admission["recipient_instance"]
        blocked = _blocking_decision_ids(result, recipient_instance=recipient)
        if admission["run_kind"] == "specialist" and blocked:
            raise MissionConflict(
                "recipient is blocked by pending human decisions: "
                + ", ".join(blocked)
            )
        if recipient in result["active_recipients"]:
            raise MissionConflict("recipient already has an active delegation")
        if admission["writer"] and result["active_writer"] is not None:
            raise MissionConflict("mission already has an active writer")
        prospective[admission_id] = admission

    if len([item for item in admissions if item["writer"]]) > 1:
        raise MissionConflict("admission batch requests more than one writer")
    lead_admissions = [item for item in admissions if item["run_kind"] == "lead"]
    if len(lead_admissions) > 1 or (
        lead_admissions
        and (
            result["lead_admission_id"] is not None or result["lead_run_id"] is not None
        )
    ):
        raise MissionConflict("mission can reserve only one Lead admission")
    new_active = sum(item["run_kind"] == "specialist" for item in admissions)
    policy = result["admission_policy"]
    if result["active_delegations"] + new_active > policy["max_active_delegations"]:
        raise MissionConflict("maximum active delegations would be exceeded")

    global_debit = 0
    parent_debits: dict[str, int] = {}
    for admission in admissions:
        if admission["run_kind"] == "lead":
            if any(
                admission[field] not in {None, 0}
                for field in (
                    "parent_admission_id",
                    "parent_run_id",
                    "delegated_budget",
                    "credit_cost",
                    "global_credit_debit",
                    "parent_credit_debit",
                )
            ):
                raise MissionStateError(
                    "Lead admission must have zero cost and no parent"
                )
            continue
        cost = 1 + admission["delegated_budget"]
        if admission["credit_cost"] != cost:
            raise MissionStateError("specialist admission credit cost is invalid")
        parent_id = admission["parent_admission_id"]
        parent = prospective.get(parent_id) or result["admissions"].get(parent_id)
        if parent is not None and parent["run_kind"] == "specialist":
            if parent.get("lane_actor", lane_actor) != lane_actor:
                raise MissionConflict("admission cannot cross authority lanes")
            parent_is_prospective = parent_id in prospective
            if not parent_is_prospective and (
                parent["phase"] not in ACTIVE_ADMISSION_PHASES or not parent["active"]
            ):
                raise MissionConflict("parent specialist admission is not active")
            if parent["run_id"] in result["cancelled_runs"]:
                raise MissionConflict("parent specialist run was cancelled")
            if admission["parent_run_id"] != parent["run_id"]:
                raise MissionStateError("subdelegation parent run binding mismatch")
            if (
                admission["global_credit_debit"] != 0
                or admission["parent_credit_debit"] != cost
            ):
                raise MissionStateError(
                    "subdelegation must debit only its parent budget"
                )
            parent_debits[parent_id] = parent_debits.get(parent_id, 0) + cost
        else:
            if parent_id is not None:
                if parent is None or parent["run_kind"] != "lead":
                    raise MissionStateError("admission references an unknown parent")
                if parent.get("lane_actor", lane_actor) != lane_actor:
                    raise MissionConflict("admission cannot cross authority lanes")
                parent_is_prospective = parent_id in prospective
                if not parent_is_prospective and (
                    parent["phase"] not in ACTIVE_ADMISSION_PHASES
                    or not parent["active"]
                ):
                    raise MissionConflict(
                        "root delegation Lead admission is not active"
                    )
                if parent["run_id"] in result["cancelled_runs"]:
                    raise MissionConflict("root delegation Lead run was cancelled")
                if admission["parent_run_id"] != parent["run_id"]:
                    raise MissionStateError("root delegation Lead binding mismatch")
            elif lane_actor == "ASSURED":
                if admission["parent_run_id"] is not None:
                    raise MissionStateError(
                        "ASSURED-root specialist cannot inherit a historical Lead run"
                    )
            elif result["lead_run_id"] is None:
                if admission["parent_run_id"] is not None:
                    raise MissionStateError(
                        "CONTROL-root specialist cannot invent a parent run"
                    )
            elif admission["parent_run_id"] != result["lead_run_id"]:
                raise MissionStateError(
                    "root specialist is not bound to the mission Lead"
                )
            if (
                admission["global_credit_debit"] != cost
                or admission["parent_credit_debit"] != 0
            ):
                raise MissionStateError(
                    "root specialist must debit the global credit pool once"
                )
            global_debit += cost

    if result["delegation_credits_spent"] + global_debit > policy["delegation_credits"]:
        raise MissionConflict("mission delegation credits would be exceeded")
    for parent_id, debit in parent_debits.items():
        parent = prospective.get(parent_id) or result["admissions"][parent_id]
        if parent.get("child_credits_spent", 0) + debit > parent["delegated_budget"]:
            raise MissionConflict("parent delegated budget would be exceeded")

    for admission in admissions:
        seen: set[str] = set()
        parent_id = admission["parent_admission_id"]
        while parent_id in prospective:
            if parent_id in seen:
                raise MissionStateError("admission batch contains a parent cycle")
            seen.add(parent_id)
            parent_id = prospective[parent_id]["parent_admission_id"]

    result["delegation_credits_spent"] += global_debit
    result["delegation_credits_remaining"] = (
        policy["delegation_credits"] - result["delegation_credits_spent"]
    )
    for parent_id, debit in parent_debits.items():
        parent = result["admissions"].get(parent_id)
        if parent is not None:
            parent["child_credits_spent"] += debit
    for admission in admissions:
        stored = {
            **admission,
            "lane_actor": lane_actor,
            "phase": "reserved",
            "active": True,
            "child_credits_spent": parent_debits.get(admission["admission_id"], 0),
            "reservation_event_sha256": event["event_sha256"],
            "commit": None,
            "launch_authorization": None,
            "started_event_sha256": None,
            "terminal": None,
            "result": None,
        }
        result["admissions"][admission["admission_id"]] = stored
        result["run_claims"][admission["run_id"]] = admission["admission_id"]
        result["active_recipients"][admission["recipient_instance"]] = admission[
            "admission_id"
        ]
        if admission["writer"]:
            result["active_writer"] = admission["admission_id"]
        if admission["run_kind"] == "specialist":
            result["active_delegations"] += 1
        else:
            result["lead_admission_id"] = admission["admission_id"]
    result["admission_batches"][batch_id] = {
        "batch_sha256": payload["batch_sha256"],
        "admission_ids": [item["admission_id"] for item in admissions],
        "event_sha256": event["event_sha256"],
    }


def derive_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events or events[0]["kind"] != "mission_created":
        raise MissionStateError("mission must begin with mission_created")
    created = events[0]["payload"]
    _validate_payload("mission_created", created)
    result: dict[str, Any] = {
        "mission_id": events[0]["mission_id"],
        "feature": created["feature"],
        "status": "created",
        "risk": created["initial_risk"],
        "risk_categories": [],
        "workflow_digest": created["workflow_digest"],
        "compiled_digest": None,
        "objective_sha256": created["objective_sha256"],
        "target_repo": created["target_repo"],
        "base_sha": created["base_sha"],
        "terminal": None,
        "lead_run_id": None,
        "lead_admission_id": None,
        "lead_result": None,
        "synthesis_result": None,
        "approval": None,
        "decisions": {},
        "pending_decisions": {},
        "delegations": {},
        "results": {},
        "admission_policy": None,
        "admission_batches": {},
        "admissions": {},
        "run_claims": {},
        "run_owners": {},
        "cancelled_runs": {},
        "active_recipients": {},
        "active_writer": None,
        "active_delegations": 0,
        "delegation_credits_spent": 0,
        "delegation_credits_remaining": None,
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
        _validate_payload(kind, payload)
        if kind in STATE_EVENT:
            if kind == "workflow_compiled":
                if result["status"] != "created":
                    raise MissionStateError("workflow_compiled requires created state")
                result["compiled_digest"] = payload["compiled_digest"]
            if kind == "fleet_boot_started" and result["status"] != "compiled":
                raise MissionStateError("fleet_boot_started requires compiled state")
            if kind == "mission_running" and result["status"] not in {
                "booting",
                "running",
            }:
                raise MissionStateError("mission_running requires booting state")
            if kind == "assurance_requested" and result["status"] not in {
                "compiled",
                "running",
                "assured_running",
            }:
                raise MissionStateError(
                    "assurance request requires compiled or running state"
                )
            if kind == "assurance_requested":
                _require_no_active_admissions(result, "assurance request")
                result["risk_categories"] = sorted(
                    set(result["risk_categories"]) | set(payload["categories"])
                )
            if kind == "assurance_approved":
                if result["status"] != "awaiting_assurance_confirmation":
                    raise MissionStateError(
                        "assurance approval requires confirmation state"
                    )
                request = next(
                    item
                    for item in reversed(events[: event["sequence"] - 1])
                    if item["kind"] == "assurance_requested"
                )
                if payload["request_event_sha256"] != request["event_sha256"]:
                    raise MissionStateError(
                        "approval does not reference the active assurance request"
                    )
                if payload["workflow_digest"] != result["workflow_digest"]:
                    raise MissionStateError("approval workflow digest mismatch")
                if (
                    payload["scope"] != result["target_repo"]
                    or payload["risk"] != result["risk"]
                ):
                    raise MissionStateError("approval scope or risk mismatch")
                if "expires_in_seconds" in payload:
                    approved_at = parse_timestamp(
                        event["timestamp"], "assurance approval timestamp"
                    )
                    expires_at = parse_timestamp(
                        payload["expires_at"], "assurance approval expiry"
                    )
                    if expires_at <= approved_at:
                        raise MissionStateError(
                            "assurance approval must expire in the future"
                        )
                    if expires_at > approved_at + timedelta(
                        seconds=payload["expires_in_seconds"]
                    ):
                        raise MissionStateError(
                            "assurance approval exceeds its declared duration"
                        )
                result["approval"] = {**payload, "event_sha256": event["event_sha256"]}
            if kind == "assurance_boot_started":
                if (
                    result["status"] != "assurance_approved"
                    or result["approval"] is None
                ):
                    raise MissionStateError("assurance boot requires approval")
                if (
                    payload["approval_event_sha256"]
                    != result["approval"]["event_sha256"]
                ):
                    raise MissionStateError(
                        "assurance boot approval reference mismatch"
                    )
                if any(
                    (
                        result["approval"]["workflow_digest"]
                        != result["workflow_digest"],
                        result["approval"]["scope"] != result["target_repo"],
                        result["approval"]["risk"] != result["risk"],
                    )
                ):
                    raise MissionStateError(
                        "assurance boot approval no longer matches mission authority"
                    )
                if parse_timestamp(
                    event["timestamp"], "assurance boot timestamp"
                ) >= parse_timestamp(
                    result["approval"]["expires_at"], "assurance approval expiry"
                ):
                    raise MissionStateError("assurance approval expired before boot")
            if kind == "assurance_started":
                if result["status"] != "assured_booting" or result["approval"] is None:
                    raise MissionStateError(
                        "assurance start requires assured boot state"
                    )
                if (
                    payload["approval_event_sha256"]
                    != result["approval"]["event_sha256"]
                ):
                    raise MissionStateError(
                        "assurance start approval reference mismatch"
                    )
                if any(
                    (
                        result["approval"]["workflow_digest"]
                        != result["workflow_digest"],
                        result["approval"]["scope"] != result["target_repo"],
                        result["approval"]["risk"] != result["risk"],
                    )
                ):
                    raise MissionStateError(
                        "assurance start approval no longer matches mission authority"
                    )
                if parse_timestamp(
                    event["timestamp"], "assurance start timestamp"
                ) >= parse_timestamp(
                    result["approval"]["expires_at"], "assurance approval expiry"
                ):
                    raise MissionStateError("assurance approval expired before start")
            if kind == "mission_completing" and result["status"] not in {
                "running",
                "assured_running",
            }:
                raise MissionStateError("mission completing requires running state")
            if kind == "mission_completing":
                _require_no_active_admissions(result, "mission completing")
                _require_no_pending_decisions(result, "mission completing")
            if kind == "archive_created" and result["status"] != "completing":
                raise MissionStateError("archive creation requires completing state")
            if kind == "archive_created":
                _require_no_active_admissions(result, "archive creation")
            result["status"] = STATE_EVENT[kind]
        elif kind == "assurance_approval_renewed":
            approval = result["approval"]
            if (
                result["status"]
                not in {
                    "assurance_approved",
                    "assured_running",
                }
                or approval is None
            ):
                raise MissionStateError(
                    "assurance approval renewal requires approved or assured-running state"
                )
            if result["status"] == "assured_running":
                _require_no_active_admissions(
                    result, "assured-running approval renewal"
                )
            if event["actor"] != "HUMAN":
                raise MissionStateError(
                    "assurance approval renewal requires HUMAN actor"
                )
            renewed_at = parse_timestamp(
                event["timestamp"], "assurance approval renewal timestamp"
            )
            if renewed_at < parse_timestamp(
                approval["expires_at"], "prior assurance approval expiry"
            ):
                raise MissionConflict("assurance approval cannot renew before expiry")
            if (
                parse_timestamp(
                    payload["expires_at"], "renewed assurance approval expiry"
                )
                <= renewed_at
            ):
                raise MissionStateError(
                    "renewed assurance approval must expire in the future"
                )
            if parse_timestamp(
                payload["expires_at"], "renewed assurance approval expiry"
            ) > renewed_at + timedelta(seconds=payload["expires_in_seconds"]):
                raise MissionStateError(
                    "renewed assurance approval exceeds its declared duration"
                )
            expected_approval_id = str(
                uuid.uuid5(
                    uuid.UUID(result["mission_id"]),
                    "approval-renewal:"
                    f"{approval['event_sha256']}:{event['idempotency_key']}",
                )
            )
            bindings = {
                "approval_id": expected_approval_id,
                "prior_approval_event_sha256": approval["event_sha256"],
                "request_event_sha256": approval["request_event_sha256"],
                "workflow_digest": result["workflow_digest"],
                "scope": result["target_repo"],
                "risk": result["risk"],
            }
            if any(payload[field] != value for field, value in bindings.items()):
                raise MissionConflict("assurance approval renewal binding mismatch")
            if approval["risk"] != result["risk"]:
                raise MissionConflict("assurance approval renewal cannot widen risk")
            result["approval"] = {**payload, "event_sha256": event["event_sha256"]}
        elif kind == "human_decision_requested":
            if event["actor"] != "lead":
                raise MissionStateError("human decision request requires lead actor")
            if result["status"] not in {"running", "assured_running"}:
                raise MissionConflict(
                    "human decision request requires a running mission"
                )
            if result["lead_run_id"] is None:
                raise MissionConflict("human decision request requires a bound Lead")
            decision_id = payload["decision_id"]
            expected_decision_id = str(
                uuid.uuid5(
                    uuid.UUID(result["mission_id"]),
                    f"decision:{event['idempotency_key']}",
                )
            )
            if decision_id != expected_decision_id:
                raise MissionStateError("human decision deterministic identity mismatch")
            if decision_id in result["decisions"]:
                raise MissionConflict("human decision identity is immutable")
            recommendation_identity = _decision_evidence_identity(
                result, payload["recommendation"], "decision recommendation"
            )
            challenge_identity = _decision_evidence_identity(
                result, payload["challenge"], "decision challenge"
            )
            challenge_delegation = result["delegations"][
                payload["challenge"]["delegation_id"]
            ]
            if challenge_delegation.get("capability") not in {"challenge", "verify"}:
                raise MissionConflict(
                    "decision challenge requires challenge or verify capability"
                )
            challenge_owner = result["run_owners"].get(
                challenge_delegation.get("run_id")
            )
            challenge_admission = (
                result["admissions"].get(challenge_owner.get("owner_id"))
                if isinstance(challenge_owner, dict)
                and challenge_owner.get("owner_kind") == "admission"
                else None
            )
            if not isinstance(challenge_admission, dict) or challenge_admission.get(
                "writer"
            ):
                raise MissionConflict(
                    "decision challenge requires a non-writing admission"
                )
            if recommendation_identity == challenge_identity:
                raise MissionConflict(
                    "decision challenger must use a distinct provider/model identity"
                )
            if payload["impact"] == "blocking":
                active = sorted(
                    set(payload["affected_instances"])
                    & set(result["active_recipients"])
                )
                if active:
                    raise MissionConflict(
                        "blocking human decision requires quiescent affected instances: "
                        + ", ".join(active)
                    )
            decision = {
                "decision_id": decision_id,
                "status": "pending",
                "request": payload,
                "request_event_sha256": event["event_sha256"],
                "requested_at": event["timestamp"],
                "deadline_at": decision_deadline(event["timestamp"]),
                "resolution": None,
            }
            result["decisions"][decision_id] = decision
            result["pending_decisions"][decision_id] = decision
        elif kind == "human_decision_resolved":
            decision_id = payload["decision_id"]
            decision = result["pending_decisions"].get(decision_id)
            if not isinstance(decision, dict):
                raise MissionConflict("human decision is not pending")
            request = decision["request"]
            expected_actor = (
                "HUMAN" if payload["resolution_kind"] == "human" else "CONTROL"
            )
            if event["actor"] != expected_actor:
                raise MissionStateError(
                    f"{payload['resolution_kind']} decision resolution requires {expected_actor} actor"
                )
            if payload["request_event_sha256"] != decision["request_event_sha256"]:
                raise MissionConflict(
                    "decision resolution does not reference the active request"
                )
            option_ids = {item["option_id"] for item in request["options"]}
            if payload["option_id"] not in option_ids:
                raise MissionConflict("decision resolution references an unknown option")
            if payload["resolution_kind"] == "automatic":
                if (
                    request["risk"] != "low"
                    or not request["reversible"]
                    or request["default_option_id"] is None
                    or payload["option_id"] != request["default_option_id"]
                ):
                    raise MissionConflict(
                        "decision is not eligible for automatic resolution"
                    )
                if parse_timestamp(
                    event["timestamp"], "automatic decision resolution timestamp"
                ) < parse_timestamp(decision["deadline_at"], "decision deadline"):
                    raise MissionConflict(
                        "decision cannot resolve automatically before its deadline"
                    )
            resolution = {
                **payload,
                "event_sha256": event["event_sha256"],
                "resolved_at": event["timestamp"],
                "actor": event["actor"],
            }
            decision["status"] = "resolved"
            decision["resolution"] = resolution
            del result["pending_decisions"][decision_id]
        elif kind == "risk_escalated":
            if payload["from"] != result["risk"]:
                raise MissionStateError(
                    "risk escalation does not start at current risk"
                )
            if RISK_ORDER[payload["to"]] < RISK_ORDER[result["risk"]]:
                raise MissionStateError("mission risk cannot decrease")
            result["risk"] = payload["to"]
            result["risk_categories"] = sorted(
                set(result["risk_categories"]) | set(payload["categories"])
            )
        elif kind == "mission_admission_policy_frozen":
            if result["admission_policy"] is not None:
                raise MissionConflict("mission admission policy is immutable")
            if payload["workflow_digest"] != result["workflow_digest"]:
                raise MissionStateError("admission policy workflow digest mismatch")
            if payload["compiled_digest"] != result["compiled_digest"]:
                raise MissionStateError("admission policy compiled digest mismatch")
            result["admission_policy"] = {
                **payload,
                "event_sha256": event["event_sha256"],
            }
            result["delegation_credits_remaining"] = payload["delegation_credits"]
        elif kind == "delegation_budget_allocated":
            # This event is part of the pre-admission-control ledger format.
            # It may be replayed when it predates the frozen policy, but once
            # the policy exists only admissions may create/debit authority.
            if result["admission_policy"] is not None:
                raise MissionConflict(
                    "legacy delegation budget allocation is disabled after policy freeze"
                )
        elif kind == "delegations_reserved":
            _reserve_admissions(result, event, payload)
        elif kind == "delegation_committed":
            _require_admission_lane(result, event)
            _require_admission_deadline(result, event)
            admission = result["admissions"].get(payload["admission_id"])
            if admission is None or admission["phase"] != "reserved":
                raise MissionConflict("only a reserved admission can be committed")
            _require_admission_actor(admission, event)
            expected_commit_id = str(
                uuid.uuid5(
                    uuid.UUID(result["mission_id"]),
                    f"commit:{admission['admission_id']}",
                )
            )
            if payload["commit_id"] != expected_commit_id:
                raise MissionStateError("delegation commit identity mismatch")
            bindings = {
                "reservation_event_sha256": admission["reservation_event_sha256"],
                "request_digest": admission["request_digest"],
                "recipient_instance": admission["recipient_instance"],
                "writer": admission["writer"],
                "run_id": admission["run_id"],
            }
            admission_effect = admission.get("effect_sha256")
            if admission_effect is not None:
                if "effect_sha256" not in payload:
                    raise MissionConflict("delegation commit lacks effect binding")
                bindings["effect_sha256"] = admission_effect
            elif "effect_sha256" in payload:
                raise MissionConflict(
                    "legacy admission cannot acquire an effect binding"
                )
            if any(payload[field] != value for field, value in bindings.items()):
                raise MissionConflict("delegation commit binding mismatch")
            run_id = admission["run_id"]
            if result["run_claims"].get(run_id) != admission["admission_id"]:
                raise MissionConflict("delegation run reservation is missing")
            if run_id in result["run_owners"]:
                raise MissionConflict("delegation run already has an owner")
            parent_id = admission["parent_admission_id"]
            if parent_id is not None:
                parent = result["admissions"].get(parent_id)
                if (
                    parent is None
                    or parent["phase"] != "started"
                    or not parent["active"]
                    or parent["run_id"] in result["cancelled_runs"]
                ):
                    raise MissionConflict(
                        "delegation parent must be started and active"
                    )
            del result["run_claims"][run_id]
            result["run_owners"][run_id] = {
                "owner_kind": "admission",
                "owner_id": admission["admission_id"],
            }
            admission["phase"] = "committed"
            admission["commit"] = {**payload, "event_sha256": event["event_sha256"]}
        elif kind == "delegation_launch_authorized":
            expected_approval_event_sha256 = _require_launch_authorization_lane(
                result, event
            )
            _require_admission_deadline(result, event)
            admission = result["admissions"].get(payload["admission_id"])
            if (
                admission is None
                or admission["phase"] != "committed"
                or not admission["active"]
                or admission["result"] is not None
            ):
                raise MissionConflict("only a committed admission can authorize launch")
            _require_admission_actor(admission, event)
            if admission["run_id"] in result["cancelled_runs"]:
                raise MissionConflict("cancelled admission cannot authorize launch")
            if admission.get("effect_sha256") is None:
                raise MissionConflict("legacy admission cannot authorize a new effect")
            commit = admission["commit"]
            bindings = {
                "request_digest": admission["request_digest"],
                "effect_sha256": admission["effect_sha256"],
                "recipient_instance": admission["recipient_instance"],
                "writer": admission["writer"],
                "run_id": admission["run_id"],
            }
            if (
                commit is None
                or payload["commit_event_sha256"] != commit["event_sha256"]
                or any(payload[field] != value for field, value in bindings.items())
            ):
                raise MissionConflict(
                    "delegation launch authorization binding mismatch"
                )
            if "approval_event_sha256" in payload and (
                payload["approval_event_sha256"] != expected_approval_event_sha256
            ):
                raise MissionConflict("delegation launch approval reference mismatch")
            parent_id = admission["parent_admission_id"]
            if parent_id is not None:
                parent = result["admissions"].get(parent_id)
                if (
                    parent is None
                    or parent["phase"] != "started"
                    or not parent["active"]
                    or parent["run_id"] in result["cancelled_runs"]
                ):
                    raise MissionConflict(
                        "delegation parent must be started and active"
                    )
            admission["phase"] = "authorized"
            admission["launch_authorization"] = {
                **payload,
                "event_sha256": event["event_sha256"],
                "authorized_head_sha256": event["previous_event_sha256"],
            }
        elif kind == "delegation_started":
            admission = result["admissions"].get(payload["admission_id"])
            current_start = "authorization_event_sha256" in payload
            required_phase = "authorized" if current_start else "committed"
            if (
                admission is None
                or admission["phase"] != required_phase
                or not admission["active"]
                or admission["result"] is not None
            ):
                raise MissionConflict(
                    f"only an {required_phase} admission can be started"
                )
            _require_admission_actor(admission, event)
            if current_start:
                authorization = admission.get("launch_authorization")
                bindings = {
                    "request_digest": admission["request_digest"],
                    "effect_sha256": admission.get("effect_sha256"),
                    "recipient_instance": admission["recipient_instance"],
                    "writer": admission["writer"],
                    "run_id": admission["run_id"],
                }
                if (
                    authorization is None
                    or payload["authorization_event_sha256"]
                    != authorization["event_sha256"]
                    or any(payload[field] != value for field, value in bindings.items())
                ):
                    raise MissionConflict(
                        "delegation start authorization reference mismatch"
                    )
            else:
                _require_admission_deadline(result, event)
                if admission.get("effect_sha256") is not None:
                    raise MissionConflict(
                        "current admission requires launch authorization"
                    )
                commit = admission["commit"]
                bindings = {
                    "request_digest": admission["request_digest"],
                    "recipient_instance": admission["recipient_instance"],
                    "writer": admission["writer"],
                    "run_id": admission["run_id"],
                }
                if (
                    payload["commit_event_sha256"] != commit["event_sha256"]
                    or payload["expected_head_sha256"] != event["previous_event_sha256"]
                    or any(payload[field] != value for field, value in bindings.items())
                ):
                    raise MissionConflict("delegation start commit reference mismatch")
            parent_id = admission["parent_admission_id"]
            if parent_id is not None and not current_start:
                parent = result["admissions"].get(parent_id)
                if (
                    parent is None
                    or parent["phase"] != "started"
                    or not parent["active"]
                    or parent["run_id"] in result["cancelled_runs"]
                ):
                    raise MissionConflict(
                        "delegation parent must be started and active"
                    )
            admission["phase"] = "started"
            admission["started_event_sha256"] = event["event_sha256"]
        elif kind == "delegation_finalized":
            admission = result["admissions"].get(payload["admission_id"])
            if admission is None or admission["phase"] not in {
                "committed",
                "authorized",
                "started",
            }:
                raise MissionConflict(
                    "only a committed, authorized, or started admission can be finalized"
                )
            _require_admission_actor(admission, event)
            historical_current_final = "terminal_evidence_sha256" in payload
            source_bound_final = "terminal_source_sha256" in payload
            current_final = "terminal_evidence" in payload
            if (
                admission.get("effect_sha256") is not None
                and not historical_current_final
            ):
                raise MissionConflict(
                    "current admission requires exact terminal evidence"
                )
            if historical_current_final:
                bindings = {
                    "run_id": admission["run_id"],
                    "recipient_instance": admission["recipient_instance"],
                    "writer": admission["writer"],
                }
                if any(payload[field] != value for field, value in bindings.items()):
                    raise MissionConflict(
                        "delegation finalization ownership binding mismatch"
                    )
            if source_bound_final:
                terminal_binding = {
                    "mission_id": result["mission_id"],
                    "admission_id": admission["admission_id"],
                    "run_id": admission["run_id"],
                    "recipient_instance": admission["recipient_instance"],
                    "writer": admission["writer"],
                    "status": payload["status"],
                    "terminal_source_sha256": payload["terminal_source_sha256"],
                }
                if payload["terminal_evidence_sha256"] != sha256(terminal_binding):
                    raise MissionConflict(
                        "delegation terminal evidence binding mismatch"
                    )
            if current_final:
                evidence = payload["terminal_evidence"]
                evidence_bindings = {
                    "run_id": admission["run_id"],
                    "task_sha256": admission.get("task_sha256"),
                    "status": payload["status"],
                }
                if any(
                    evidence[field] != value
                    for field, value in evidence_bindings.items()
                ):
                    raise MissionConflict(
                        "delegation structured terminal evidence binding mismatch"
                    )
                terminal_binding = {
                    "mission_id": result["mission_id"],
                    "admission_id": admission["admission_id"],
                    "recipient_instance": admission["recipient_instance"],
                    "writer": admission["writer"],
                    "terminal_evidence": evidence,
                }
                if payload["terminal_evidence_sha256"] != sha256(terminal_binding):
                    raise MissionConflict(
                        "delegation terminal evidence digest mismatch"
                    )
            if any(
                child["active"]
                and child["parent_admission_id"] == admission["admission_id"]
                for child in result["admissions"].values()
            ):
                raise MissionConflict("admission cannot finalize with active children")
            admission["phase"] = "finalized"
            admission["terminal"] = {**payload, "event_sha256": event["event_sha256"]}
            _release_admission(result, admission)
        elif kind == "delegation_reservation_aborted":
            admission = result["admissions"].get(payload["admission_id"])
            if admission is None or admission["phase"] not in {
                "reserved",
                "committed",
            }:
                raise MissionConflict(
                    "only a reserved or committed admission can be aborted prelaunch"
                )
            _require_admission_actor(admission, event)
            current_abort = "run_id" in payload
            if admission.get("task_sha256") is not None and not current_abort:
                raise MissionConflict(
                    "current admission requires exact prelaunch abort"
                )
            if current_abort:
                bindings = {
                    "run_id": admission["run_id"],
                    "request_digest": admission["request_digest"],
                    "effect_sha256": admission.get("effect_sha256"),
                    "task_sha256": admission.get("task_sha256"),
                    "recipient_instance": admission["recipient_instance"],
                    "writer": admission["writer"],
                }
                if any(payload[field] != value for field, value in bindings.items()):
                    raise MissionConflict("prelaunch abort binding mismatch")
            if any(
                child["active"]
                and child["parent_admission_id"] == admission["admission_id"]
                for child in result["admissions"].values()
            ):
                raise MissionConflict("admission cannot abort with active children")
            admission["phase"] = "aborted"
            admission["terminal"] = {**payload, "event_sha256": event["event_sha256"]}
            run_id = admission["run_id"]
            if admission["commit"] is None:
                result["run_claims"].pop(run_id, None)
            elif result["run_owners"].get(run_id) == {
                "owner_kind": "admission",
                "owner_id": admission["admission_id"],
            }:
                del result["run_owners"][run_id]
            _release_admission(result, admission)
        elif kind == "lead_dispatched":
            run_id = normalize_uuid(payload["run_id"], "lead run_id")
            if result["lead_run_id"] is not None:
                raise MissionStateError("mission has more than one lead dispatch event")
            claim = result["run_claims"].get(run_id)
            owner = result["run_owners"].get(run_id)
            if claim is not None:
                raise MissionConflict(
                    "Lead run admission must be committed before dispatch"
                )
            if owner is None:
                if result["admission_policy"] is not None:
                    raise MissionConflict(
                        "Lead dispatch requires a committed admission"
                    )
                result["run_owners"][run_id] = {
                    "owner_kind": "legacy_lead",
                    "owner_id": "lead",
                }
            else:
                admission = result["admissions"].get(owner["owner_id"])
                if (
                    admission is None
                    or admission["run_kind"] != "lead"
                    or admission["phase"] != "started"
                    or admission["terminal"] is not None
                ):
                    raise MissionConflict("Lead run_id is owned by another delegation")
            result["lead_run_id"] = run_id
        elif kind == "lead_result_recorded":
            run_id = normalize_uuid(payload["run_id"], "lead run_id")
            if result["lead_run_id"] != run_id:
                raise MissionStateError(
                    "lead result run_id does not match dispatched lead"
                )
            if result["lead_result"] is not None:
                raise MissionConflict("first lead result is immutable")
            result["lead_result"] = payload
            owner = result["run_owners"].get(run_id)
            if owner and owner["owner_kind"] == "admission":
                admission = result["admissions"][owner["owner_id"]]
                if admission["terminal"] is not None:
                    raise MissionConflict(
                        "Lead result follows immutable admission terminal"
                    )
                admission["result"] = {**payload, "event_sha256": event["event_sha256"]}
        elif kind == "synthesis_result_recorded":
            if result["synthesis_result"] is not None:
                raise MissionConflict("first assured synthesis result is immutable")
            admission = result["admissions"].get(payload["admission_id"])
            owner = result["run_owners"].get(payload["run_id"])
            if (
                admission is None
                or owner
                != {"owner_kind": "admission", "owner_id": payload["admission_id"]}
                or admission["run_kind"] != "specialist"
                or admission["recipient_instance"] != "lead"
                or admission["capability"] != "synthesis"
                or admission["phase"] != "started"
                or admission["terminal"] is not None
                or admission["result"] is not None
            ):
                raise MissionConflict(
                    "synthesis result lacks exact active synthesis admission"
                )
            result["synthesis_result"] = payload
            admission["result"] = {**payload, "event_sha256": event["event_sha256"]}
        elif kind == "delegation_registered":
            delegation_id = payload["delegation_id"]
            run_id = payload["run_id"]
            if payload["mission_id"] != result["mission_id"]:
                raise MissionStateError("delegation mission_id mismatch")
            if delegation_id in result["delegations"]:
                raise MissionConflict("duplicate delegation_id")
            if run_id in result["run_claims"]:
                raise MissionConflict(
                    "delegation admission must be committed before registration"
                )
            owner = result["run_owners"].get(run_id)
            if owner is None:
                if result["admission_policy"] is not None:
                    raise MissionConflict(
                        "delegation registration requires a committed admission"
                    )
                result["run_owners"][run_id] = {
                    "owner_kind": "legacy_delegation",
                    "owner_id": delegation_id,
                }
                recipient = payload["recipient_instance"]
                if recipient in result["active_recipients"]:
                    raise MissionConflict("recipient already has an active delegation")
                result["active_recipients"][recipient] = delegation_id
            else:
                admission = result["admissions"].get(owner["owner_id"])
                if (
                    admission is None
                    or admission["delegation_id"] != delegation_id
                    or admission["phase"] != "started"
                    or admission["terminal"] is not None
                ):
                    raise MissionConflict(
                        "run_id is globally owned by another delegation"
                    )
                if admission["recipient_instance"] != payload["recipient_instance"]:
                    raise MissionConflict("registered recipient differs from admission")
            result["delegations"][delegation_id] = payload
        elif kind == "result_recorded":
            delegation_id = payload["delegation_id"]
            if delegation_id not in result["delegations"]:
                raise MissionStateError("result references unknown delegation")
            delegation = result["delegations"][delegation_id]
            if payload["run_id"] != delegation["run_id"]:
                raise MissionStateError("result run_id does not match delegation")
            if delegation_id in result["results"]:
                raise MissionConflict("first delegation result is immutable")
            result["results"][delegation_id] = payload
            owner = result["run_owners"].get(payload["run_id"])
            if owner and owner["owner_kind"] == "admission":
                admission = result["admissions"][owner["owner_id"]]
                if admission["terminal"] is not None:
                    raise MissionConflict(
                        "delegation result follows immutable admission terminal"
                    )
                admission["result"] = {**payload, "event_sha256": event["event_sha256"]}
            elif (
                result["active_recipients"].get(delegation["recipient_instance"])
                == delegation_id
            ):
                del result["active_recipients"][delegation["recipient_instance"]]
        elif kind == "lead_completion_requested":
            if event["actor"] != "lead":
                raise MissionStateError("lead completion request requires lead actor")
            _require_no_pending_decisions(result, "lead completion")
        elif kind == "run_cancel_requested":
            run_id = payload["run_id"]
            if run_id not in result["run_owners"]:
                raise MissionConflict("cancel request references an unowned run")
            prior = result["cancelled_runs"].get(run_id)
            if prior is not None and prior != payload:
                raise MissionConflict("first run cancellation request is immutable")
            result["cancelled_runs"][run_id] = payload
        elif kind == "mission_terminal":
            if result["terminal"] is not None:
                raise MissionConflict("first mission terminal is immutable")
            if (
                result["run_claims"]
                or result["active_writer"] is not None
                or any(item["active"] for item in result["admissions"].values())
            ):
                raise MissionConflict(
                    "mission terminal requires all admissions to be inactive"
                )
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


def _normalized_event_request(
    *, kind: str, actor: str, idempotency_key: str, payload: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(kind, str) or not SAFE_KIND.fullmatch(kind):
        raise MissionStateError("invalid mission event kind")
    if not isinstance(actor, str) or not SAFE_ACTOR.fullmatch(actor):
        raise MissionStateError("invalid mission event actor")
    if not isinstance(idempotency_key, str) or not SAFE_KEY.fullmatch(idempotency_key):
        raise MissionStateError("invalid mission idempotency key")
    # Canonicalization is intentionally performed before the closed-registry
    # check so NaN/Infinity and other non-JSON values fail before any lock or
    # directory is created.
    canonical_payload = canonical_bytes(payload)
    normalized_payload = loads_strict(canonical_payload)
    _validate_payload(kind, normalized_payload)
    return {
        "kind": kind,
        "actor": actor,
        "idempotency_key": idempotency_key,
        "payload": normalized_payload,
    }


def _require_current_admission_append_schema(request: dict[str, Any]) -> None:
    """Keep historical admission events readable but never mint them anew."""

    kind = request["kind"]
    payload = request["payload"]
    if kind == "delegations_reserved":
        if "request_sha256" not in payload or any(
            "effect_sha256" not in admission or "task_sha256" not in admission
            for admission in payload["admissions"]
        ):
            raise MissionConflict("legacy admission reservation schema is read-only")
    elif kind == "delegation_committed" and "effect_sha256" not in payload:
        raise MissionConflict("legacy admission commit schema is read-only")
    elif kind == "delegation_launch_authorized" and (
        "approval_event_sha256" not in payload
    ):
        raise MissionConflict("legacy launch authorization schema is read-only")
    elif kind == "delegation_started" and "authorization_event_sha256" not in payload:
        raise MissionConflict("legacy admission start schema is read-only")
    elif kind == "delegation_finalized" and (
        "terminal_evidence_sha256" not in payload or "terminal_evidence" not in payload
    ):
        raise MissionConflict("legacy admission finalization schema is read-only")
    elif kind == "delegation_reservation_aborted" and "run_id" not in payload:
        raise MissionConflict("legacy admission abort schema is read-only")


def _require_current_authority_append_schema(request: dict[str, Any]) -> None:
    """Require human provenance for newly minted approval decisions."""

    if request["kind"] in {"assurance_approved", "assurance_approval_renewed"}:
        if request["actor"] != "HUMAN":
            raise MissionConflict("assurance approval requires HUMAN actor")
    if request["kind"] == "assurance_approved" and (
        "expires_in_seconds" not in request["payload"]
    ):
        raise MissionConflict("legacy assurance approval schema is read-only")


class MissionMutationBarrier:
    """Block Mission mutations while allowing descriptor-safe snapshot readers."""

    def __init__(
        self,
        runs_dir: Path,
        mission_id: str,
        *,
        drain_timeout_seconds: float = MUTATION_DRAIN_TIMEOUT_SECONDS,
        drain_poll_seconds: float = MUTATION_DRAIN_POLL_SECONDS,
    ) -> None:
        if (
            isinstance(drain_timeout_seconds, bool)
            or not isinstance(drain_timeout_seconds, (int, float))
            or not 0 <= drain_timeout_seconds <= 300
        ):
            raise MissionStateError("invalid mutation drain timeout")
        if (
            isinstance(drain_poll_seconds, bool)
            or not isinstance(drain_poll_seconds, (int, float))
            or not 0 < drain_poll_seconds <= 1
        ):
            raise MissionStateError("invalid mutation drain poll interval")
        self.runs_dir = Path(runs_dir)
        self.mission_id = normalize_uuid(mission_id, "mission_id")
        self.drain_timeout_seconds = float(drain_timeout_seconds)
        self.drain_poll_seconds = float(drain_poll_seconds)
        self._stack: ExitStack | None = None
        self._rooted: fleet_safe_paths.RootedFS | None = None
        self._mission_fd: int | None = None
        self._mission_identity: tuple[int, int] | None = None

    def __enter__(self) -> MissionMutationBarrier:
        if self._stack is not None:
            raise MissionStateError("mission mutation barrier is already active")
        stack = ExitStack()
        try:
            rooted = stack.enter_context(fleet_safe_paths.RootedFS(self.runs_dir))
            mission_fd = rooted._open_directory_chain(
                ("missions", self.mission_id),
                (0o700, 0o700),
                create=False,
            )
            stack.callback(os.close, mission_fd)
            mission_info = os.fstat(mission_fd)
            mission_identity = (mission_info.st_dev, mission_info.st_ino)
            gate_fd = MissionTransaction._acquire_pinned_lock(
                mission_fd,
                ".mutation.gate.lock",
                fcntl.LOCK_EX | fcntl.LOCK_NB,
                where="mission mutation gate",
                conflict_message="mission mutation barrier is already active",
            )
            stack.callback(MissionTransaction._release_pinned_lock, gate_fd)
            if MissionTransaction._quiescing_marker_exists(mission_fd):
                MissionTransaction._remove_quiescing_marker(
                    mission_fd,
                    require_present=True,
                )
            MissionTransaction._publish_quiescing_marker(mission_fd)
            stack.callback(
                MissionTransaction._remove_quiescing_marker,
                mission_fd,
                require_present=True,
            )

            deadline = time.monotonic() + self.drain_timeout_seconds
            while True:
                try:
                    mutation_fd = MissionTransaction._acquire_pinned_lock(
                        mission_fd,
                        ".mutation.lock",
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                        where="mission mutation lock",
                        conflict_message="mission mutations are still draining",
                    )
                    break
                except MissionConflict as exc:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MissionConflict(
                            "timed out draining mission mutations"
                        ) from exc
                    time.sleep(min(self.drain_poll_seconds, remaining))
            stack.callback(MissionTransaction._release_pinned_lock, mutation_fd)
            rooted.assert_root_binding()
        except fleet_safe_paths.SafePathError as exc:
            stack.close()
            raise MissionStateError(f"unsafe mission mutation barrier: {exc}") from exc
        except Exception:
            stack.close()
            raise
        self._stack = stack
        self._rooted = rooted
        self._mission_fd = mission_fd
        self._mission_identity = mission_identity
        try:
            self._assert_mission_binding()
        except Exception:
            self._stack = None
            self._rooted = None
            self._mission_fd = None
            self._mission_identity = None
            stack.close()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        stack, self._stack = self._stack, None
        self._rooted = None
        self._mission_fd = None
        self._mission_identity = None
        if stack is not None:
            stack.close()

    def _assert_mission_binding(self) -> None:
        if (
            self._stack is None
            or self._rooted is None
            or self._mission_fd is None
            or self._mission_identity is None
        ):
            raise MissionStateError("mission mutation barrier is not active")
        self._rooted.assert_root_binding()
        pinned = os.fstat(self._mission_fd)
        if (
            (pinned.st_dev, pinned.st_ino) != self._mission_identity
            or not stat.S_ISDIR(pinned.st_mode)
            or pinned.st_uid != os.geteuid()
            or stat.S_IMODE(pinned.st_mode) != 0o700
        ):
            raise MissionStateError("mutation barrier mission binding changed")
        try:
            current_fd = self._rooted._open_directory_chain(
                ("missions", self.mission_id),
                (0o700, 0o700),
                create=False,
            )
        except fleet_safe_paths.SafePathError as exc:
            raise MissionStateError(
                "mutation barrier mission path changed"
            ) from exc
        try:
            current = os.fstat(current_fd)
            if (current.st_dev, current.st_ino) != self._mission_identity:
                raise MissionStateError("mutation barrier mission path changed")
        finally:
            os.close(current_fd)

    def assert_binding(self, root: Path, mission_id: str) -> None:
        if self._stack is None or self._rooted is None:
            raise MissionStateError("mission mutation barrier is not active")
        if self._rooted.root != root or self.mission_id != mission_id:
            raise MissionStateError("mission mutation barrier binding mismatch")
        self._assert_mission_binding()


class MissionTransaction:
    """One descriptor-rooted, mission-locked, crash-atomic ledger transaction."""

    def __init__(
        self,
        runs_dir: Path,
        mission_id: str,
        *,
        mutation_barrier: MissionMutationBarrier | None = None,
    ) -> None:
        self.runs_dir = Path(runs_dir)
        self.mission_id = normalize_uuid(mission_id, "mission_id")
        self.ledger_relative = Path("missions") / self.mission_id / "mission.jsonl"
        self.lock_relative = Path("missions") / self.mission_id / ".lock"
        self._stack: ExitStack | None = None
        self._rooted: fleet_safe_paths.RootedFS | None = None
        self._mission_fd: int | None = None
        self._mission_identity: tuple[int, int] | None = None
        self._original = b""
        self._ledger_exists = False
        self._events: list[dict[str, Any]] = []
        self._state: dict[str, Any] | None = None
        self._mutation_barrier = mutation_barrier

    @classmethod
    def _acquire_pinned_lock(
        cls,
        directory_fd: int,
        name: str,
        operation: int,
        *,
        where: str,
        conflict_message: str = "mission mutations are quiesced",
    ) -> int:
        flags = (
            os.O_RDWR
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        created = False
        try:
            lock_fd = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            created = True
        except FileExistsError:
            try:
                lock_fd = os.open(name, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise MissionStateError(f"cannot open {where}") from exc
        except OSError as exc:
            raise MissionStateError(f"cannot create {where}") from exc
        try:
            if created:
                os.fchmod(lock_fd, 0o600)
                os.fsync(lock_fd)
                os.fsync(directory_fd)
            cls._validate_pinned_regular(
                directory_fd,
                name,
                lock_fd,
                where=where,
            )
            try:
                fcntl.flock(lock_fd, operation)
            except BlockingIOError as exc:
                raise MissionConflict(conflict_message) from exc
            cls._validate_pinned_regular(
                directory_fd,
                name,
                lock_fd,
                where=where,
            )
            return lock_fd
        except Exception:
            os.close(lock_fd)
            raise

    @staticmethod
    def _release_pinned_lock(lock_fd: int) -> None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    @classmethod
    def _quiescing_marker_exists(cls, directory_fd: int) -> bool:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            marker_fd = os.open(".mutation.quiescing", flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise MissionStateError("cannot open mission quiescing marker") from exc
        try:
            info = os.fstat(marker_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size != 0
            ):
                raise MissionStateError("unsafe mission quiescing marker")
            try:
                bound = os.stat(
                    ".mutation.quiescing",
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            if (bound.st_dev, bound.st_ino) != (info.st_dev, info.st_ino):
                raise MissionStateError("mission quiescing marker binding changed")
            return True
        finally:
            os.close(marker_fd)

    @classmethod
    def _publish_quiescing_marker(cls, directory_fd: int) -> None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            marker_fd = os.open(
                ".mutation.quiescing",
                flags,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise MissionStateError("cannot publish mission quiescing marker") from exc
        try:
            os.fchmod(marker_fd, 0o600)
            os.fsync(marker_fd)
            info = cls._validate_pinned_regular(
                directory_fd,
                ".mutation.quiescing",
                marker_fd,
                where="mission quiescing marker",
            )
            if info.st_size != 0:
                raise MissionStateError("unsafe mission quiescing marker")
            os.fsync(directory_fd)
        finally:
            os.close(marker_fd)

    @classmethod
    def _remove_quiescing_marker(
        cls,
        directory_fd: int,
        *,
        require_present: bool,
    ) -> None:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            marker_fd = os.open(".mutation.quiescing", flags, dir_fd=directory_fd)
        except FileNotFoundError as exc:
            if require_present:
                raise MissionStateError("mission quiescing marker disappeared") from exc
            return
        except OSError as exc:
            raise MissionStateError("cannot open mission quiescing marker") from exc
        try:
            info = cls._validate_pinned_regular(
                directory_fd,
                ".mutation.quiescing",
                marker_fd,
                where="mission quiescing marker",
            )
            if info.st_size != 0:
                raise MissionStateError("unsafe mission quiescing marker")
            os.unlink(".mutation.quiescing", dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(marker_fd)

    def __enter__(self) -> MissionTransaction:
        if self._stack is not None:
            raise MissionStateError("mission transaction is already active")
        stack = ExitStack()
        try:
            rooted = stack.enter_context(fleet_safe_paths.RootedFS(self.runs_dir))
            mission_fd = rooted._open_directory_chain(
                ("missions", self.mission_id),
                (0o700, 0o700),
                create=True,
            )
            stack.callback(os.close, mission_fd)
            mission_info = os.fstat(mission_fd)
            mission_identity = (mission_info.st_dev, mission_info.st_ino)
            if self._mutation_barrier is None:
                if self._quiescing_marker_exists(mission_fd):
                    gate_fd = self._acquire_pinned_lock(
                        mission_fd,
                        ".mutation.gate.lock",
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                        where="mission mutation gate",
                    )
                    try:
                        if self._quiescing_marker_exists(mission_fd):
                            self._remove_quiescing_marker(
                                mission_fd,
                                require_present=True,
                            )
                    finally:
                        self._release_pinned_lock(gate_fd)
                mutation_fd = self._acquire_pinned_lock(
                    mission_fd,
                    ".mutation.lock",
                    fcntl.LOCK_SH | fcntl.LOCK_NB,
                    where="mission mutation lock",
                )
                stack.callback(self._release_pinned_lock, mutation_fd)
                if self._quiescing_marker_exists(mission_fd):
                    raise MissionConflict("mission mutations are quiesced")
            else:
                self._mutation_barrier.assert_binding(rooted.root, self.mission_id)

            lock_fd = self._acquire_pinned_lock(
                mission_fd,
                ".lock",
                fcntl.LOCK_EX,
                where="mission lock",
            )
            stack.callback(self._release_pinned_lock, lock_fd)
            self._assert_pinned_mission_binding(rooted, mission_fd, mission_identity)
            if self._mutation_barrier is None:
                self._recover_legacy_initial_hardlink(mission_fd)
            exists, original = self._read_pinned_ledger(mission_fd)
            if exists:
                # Also closes the crash window where an exclusive rename
                # reached the directory cache but the prior process died
                # before syncing the directory entry.
                os.fsync(mission_fd)
            events = _events_from_bytes(
                original,
                expected_mission_id=self.mission_id,
                require_nonempty=exists,
            )
            derived = derive_state(events) if events else None
            if self._mutation_barrier is not None:
                self._mutation_barrier.assert_binding(rooted.root, self.mission_id)
            rooted.assert_root_binding()
        except fleet_safe_paths.SafePathError as exc:
            stack.close()
            raise MissionStateError(f"unsafe mission ledger path: {exc}") from exc
        except Exception:
            stack.close()
            raise
        self._stack = stack
        self._rooted = rooted
        self._mission_fd = mission_fd
        self._mission_identity = mission_identity
        self._ledger_exists = exists
        self._original = original
        self._events = events
        self._state = derived
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        stack, self._stack = self._stack, None
        self._rooted = None
        self._mission_fd = None
        self._mission_identity = None
        if stack is not None:
            stack.close()

    @staticmethod
    def _validate_pinned_regular(
        directory_fd: int, name: str, file_fd: int, *, where: str
    ) -> os.stat_result:
        info = os.fstat(file_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise MissionStateError(f"unsafe {where}")
        bound = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (bound.st_dev, bound.st_ino) != (info.st_dev, info.st_ino):
            raise MissionStateError(f"{where} binding changed")
        return info

    @staticmethod
    def _read_pinned_ledger(directory_fd: int) -> tuple[bool, bytes]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            ledger_fd = os.open("mission.jsonl", flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return False, b""
        except OSError as exc:
            raise MissionStateError("cannot open pinned mission ledger") from exc
        try:
            info = MissionTransaction._validate_pinned_regular(
                directory_fd,
                "mission.jsonl",
                ledger_fd,
                where="mission ledger",
            )
            remaining = info.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(ledger_fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise MissionStateError("mission ledger changed during read")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(ledger_fd, 1):
                raise MissionStateError("mission ledger grew during read")
            final = MissionTransaction._validate_pinned_regular(
                directory_fd,
                "mission.jsonl",
                ledger_fd,
                where="mission ledger",
            )
            if final.st_size != info.st_size:
                raise MissionStateError("mission ledger changed during read")
            return True, b"".join(chunks)
        finally:
            os.close(ledger_fd)

    @classmethod
    def _recover_legacy_initial_hardlink(cls, directory_fd: int) -> None:
        """Repair only the old link/unlink crash shape under the ledger lock."""

        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            ledger_fd = os.open("mission.jsonl", flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise MissionStateError("cannot open pinned mission ledger") from exc
        try:
            ledger_info = os.fstat(ledger_fd)
            if (
                not stat.S_ISREG(ledger_info.st_mode)
                or ledger_info.st_uid != os.geteuid()
                or stat.S_IMODE(ledger_info.st_mode) != 0o600
                or ledger_info.st_nlink not in {1, 2}
            ):
                raise MissionStateError("unsafe mission ledger")
            bound = os.stat(
                "mission.jsonl", dir_fd=directory_fd, follow_symlinks=False
            )
            if (bound.st_dev, bound.st_ino) != (
                ledger_info.st_dev,
                ledger_info.st_ino,
            ):
                raise MissionStateError("mission ledger binding changed")
            if ledger_info.st_nlink == 1:
                return

            aliases: list[str] = []
            for name in os.listdir(directory_fd):
                if not LEGACY_MISSION_LEDGER_TEMP.fullmatch(name):
                    continue
                try:
                    alias_fd = os.open(name, flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise MissionStateError(
                        "cannot open legacy mission ledger temporary"
                    ) from exc
                try:
                    alias = os.fstat(alias_fd)
                    if (
                        stat.S_ISREG(alias.st_mode)
                        and alias.st_uid == os.geteuid()
                        and stat.S_IMODE(alias.st_mode) == 0o600
                        and (alias.st_dev, alias.st_ino)
                        == (ledger_info.st_dev, ledger_info.st_ino)
                    ):
                        aliases.append(name)
                finally:
                    os.close(alias_fd)
            if len(aliases) != 1:
                raise MissionStateError(
                    "legacy mission ledger hardlink is not uniquely recoverable"
                )
            os.unlink(aliases[0], dir_fd=directory_fd)
            os.fsync(directory_fd)
            cls._validate_pinned_regular(
                directory_fd,
                "mission.jsonl",
                ledger_fd,
                where="mission ledger",
            )
        finally:
            os.close(ledger_fd)

    @classmethod
    def _cleanup_ledger_temporaries(cls, directory_fd: int) -> None:
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            raise MissionStateError("cannot enumerate mission ledger temporaries") from exc
        removed = False
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        for name in names:
            if not MISSION_LEDGER_TEMP.fullmatch(name):
                continue
            try:
                temporary_fd = os.open(name, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise MissionStateError(
                    "cannot open mission ledger temporary"
                ) from exc
            try:
                cls._validate_pinned_regular(
                    directory_fd,
                    name,
                    temporary_fd,
                    where="mission ledger temporary",
                )
                os.unlink(name, dir_fd=directory_fd)
                removed = True
            finally:
                os.close(temporary_fd)
        if removed:
            os.fsync(directory_fd)

    def _assert_pinned_mission_binding(
        self,
        rooted: fleet_safe_paths.RootedFS,
        mission_fd: int,
        expected: tuple[int, int],
    ) -> None:
        rooted.assert_root_binding()
        pinned = os.fstat(mission_fd)
        if (
            (pinned.st_dev, pinned.st_ino) != expected
            or not stat.S_ISDIR(pinned.st_mode)
            or pinned.st_uid != os.geteuid()
            or stat.S_IMODE(pinned.st_mode) != 0o700
        ):
            raise MissionStateError("pinned mission directory binding changed")
        try:
            current_fd = rooted._open_directory_chain(
                ("missions", self.mission_id),
                (0o700, 0o700),
                create=False,
            )
        except fleet_safe_paths.SafePathError as exc:
            raise MissionStateError("pinned mission directory path changed") from exc
        try:
            current = os.fstat(current_fd)
            if (current.st_dev, current.st_ino) != expected:
                raise MissionStateError("pinned mission directory path changed")
        finally:
            os.close(current_fd)

    def _publish_pinned_ledger(self, content: bytes) -> None:
        rooted, mission_fd = self._require_active()
        identity = self._mission_identity
        assert identity is not None
        self._assert_pinned_mission_binding(rooted, mission_fd, identity)
        exists, current = self._read_pinned_ledger(mission_fd)
        if exists != self._ledger_exists or current != self._original:
            raise MissionConflict("mission ledger changed during transaction")
        self._cleanup_ledger_temporaries(mission_fd)
        temporary = f".mission.jsonl.{hashlib.sha256(content).hexdigest()}.tmp"
        temporary_created = False
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            temporary_fd = os.open(temporary, flags, 0o600, dir_fd=mission_fd)
            temporary_created = True
            try:
                os.fchmod(temporary_fd, 0o600)
                view = memoryview(content)
                while view:
                    written = os.write(temporary_fd, view)
                    if written <= 0:
                        raise MissionStateError(
                            "short mission ledger transaction write"
                        )
                    view = view[written:]
                os.fsync(temporary_fd)
                self._validate_pinned_regular(
                    mission_fd,
                    temporary,
                    temporary_fd,
                    where="mission ledger temporary",
                )
            finally:
                os.close(temporary_fd)
            _mission_transaction_checkpoint("after_pending_fsync")
            self._assert_pinned_mission_binding(rooted, mission_fd, identity)
            if self._ledger_exists:
                os.replace(
                    temporary,
                    "mission.jsonl",
                    src_dir_fd=mission_fd,
                    dst_dir_fd=mission_fd,
                )
                temporary_created = False
                _mission_transaction_checkpoint("after_existing_publish_rename")
            else:
                fleet_safe_paths._rename_noreplace(
                    temporary,
                    "mission.jsonl",
                    source_fd=mission_fd,
                    destination_fd=mission_fd,
                )
                temporary_created = False
                _mission_transaction_checkpoint("after_initial_publish_rename")
            os.fsync(mission_fd)
            self._assert_pinned_mission_binding(rooted, mission_fd, identity)
            published, exact = self._read_pinned_ledger(mission_fd)
            if not published or exact != content:
                raise MissionStateError("published mission ledger bytes differ")
        except (OSError, fleet_safe_paths.SafePathError) as exc:
            raise MissionStateError("cannot publish pinned mission ledger") from exc
        finally:
            if temporary_created:
                try:
                    os.unlink(temporary, dir_fd=mission_fd)
                    os.fsync(mission_fd)
                except FileNotFoundError:
                    pass

    def _require_active(self) -> tuple[fleet_safe_paths.RootedFS, int]:
        if self._stack is None or self._rooted is None or self._mission_fd is None:
            raise MissionStateError("mission transaction is not active")
        return self._rooted, self._mission_fd

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        self._require_active()
        return tuple(loads_strict(canonical_bytes(event)) for event in self._events)

    @property
    def current_state(self) -> dict[str, Any] | None:
        self._require_active()
        if self._state is None:
            return None
        return loads_strict(canonical_bytes(self._state))

    @property
    def head_sha256(self) -> str:
        self._require_active()
        return self._events[-1]["event_sha256"] if self._events else GENESIS_SHA256

    def append_event(
        self,
        *,
        kind: str,
        actor: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        request = _normalized_event_request(
            kind=kind,
            actor=actor,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        return self.append_events([request])[0]

    def append_events(
        self, requests: Sequence[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], bool]]:
        self._require_active()
        if self._mutation_barrier is not None:
            raise MissionConflict("mutation barrier snapshot is read-only")
        if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes)):
            raise MissionStateError("mission transaction requests must be a sequence")
        normalized: list[dict[str, Any]] = []
        for request in requests:
            if not isinstance(request, dict) or set(request) != {
                "kind",
                "actor",
                "idempotency_key",
                "payload",
            }:
                raise MissionStateError(
                    "mission transaction request fields do not match schema"
                )
            normalized.append(_normalized_event_request(**request))
        if not normalized:
            return []

        by_key: dict[str, dict[str, Any]] = {
            event["idempotency_key"]: event for event in self._events
        }
        results: list[tuple[dict[str, Any], bool]] = []
        new_events: list[dict[str, Any]] = []
        prior = self._events[-1] if self._events else None
        for request in normalized:
            key = request["idempotency_key"]
            existing = by_key.get(key)
            if existing is not None:
                stored = {
                    field: existing[field] for field in ("kind", "actor", "payload")
                }
                expected = {
                    field: request[field] for field in ("kind", "actor", "payload")
                }
                if stored != expected:
                    raise MissionConflict(
                        "idempotency key was already used for another mission request"
                    )
                results.append((existing, False))
                continue
            _require_current_admission_append_schema(request)
            _require_current_authority_append_schema(request)
            if self._state is not None and self._state["status"] in TERMINAL_STATUSES:
                raise MissionConflict("mission terminal is immutable")
            event: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "event_id": str(uuid.uuid4()),
                "mission_id": self.mission_id,
                "sequence": len(self._events) + len(new_events) + 1,
                "timestamp": _next_timestamp(prior),
                "kind": request["kind"],
                "actor": request["actor"],
                "idempotency_key": key,
                "payload": request["payload"],
                "previous_event_sha256": (
                    prior["event_sha256"] if prior is not None else GENESIS_SHA256
                ),
            }
            event["event_sha256"] = sha256(event)
            _validate_event(event, prior, self.mission_id)
            new_events.append(event)
            by_key[key] = event
            results.append((event, True))
            prior = event

        if not new_events:
            return [
                (loads_strict(canonical_bytes(event)), appended)
                for event, appended in results
            ]
        prospective = [*self._events, *new_events]
        derived = derive_state(prospective)
        content = self._original + b"".join(
            canonical_bytes(event) + b"\n" for event in new_events
        )
        _mission_transaction_checkpoint("before_publish")
        self._publish_pinned_ledger(content)
        _mission_transaction_checkpoint("after_publish")
        self._events = prospective
        self._state = derived
        self._original = content
        self._ledger_exists = True
        return [
            (loads_strict(canonical_bytes(event)), appended)
            for event, appended in results
        ]


def append_events(
    runs_dir: Path,
    mission_id: str,
    requests: Sequence[dict[str, Any]],
) -> list[tuple[dict[str, Any], bool]]:
    normalized: list[dict[str, Any]] = []
    for request in requests:
        if not isinstance(request, dict) or set(request) != {
            "kind",
            "actor",
            "idempotency_key",
            "payload",
        }:
            raise MissionStateError(
                "mission transaction request fields do not match schema"
            )
        normalized.append(_normalized_event_request(**request))
    try:
        with MissionTransaction(runs_dir, mission_id) as transaction:
            return transaction.append_events(normalized)
    except fleet_safe_paths.SafePathError as exc:
        raise MissionStateError(f"unsafe mission ledger path: {exc}") from exc


def append_event(
    runs_dir: Path,
    mission_id: str,
    *,
    kind: str,
    actor: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    request = _normalized_event_request(
        kind=kind,
        actor=actor,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    try:
        with MissionTransaction(runs_dir, mission_id) as transaction:
            return transaction.append_events([request])[0]
    except fleet_safe_paths.SafePathError as exc:
        raise MissionStateError(f"unsafe mission ledger path: {exc}") from exc


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
