#!/usr/bin/env python3
"""Record a scoped Mission approval or approve a legacy Claude permission."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import uuid

import fleet_json
import fleet_mission_state as mission_state
import fleet_mission
import fleet_safe_paths


AUDIT_COMMON = Path.home() / ".claude" / "hooks" / "audit_common.py"
ARCHIVE_APPROVAL_FILE = "archive-approval.json"
MAX_ARCHIVE_APPROVAL_BYTES = 64 * 1024
MAX_MISSION_LEDGER_BYTES = 64 * 1024 * 1024
MAX_LEGACY_AUDIT_BYTES = 64 * 1024 * 1024
ARCHIVE_APPROVAL_FIELDS = {
    "schema_version",
    "mission_id",
    "approval_id",
    "idempotency_key",
    "workflow_digest",
    "scope",
    "content_policy",
    "risk_categories",
    "expires_at",
    "approved_by_sha256",
    "decision",
}


def _mission_relative(mission_id: str, leaf: str) -> Path:
    return Path("missions") / mission_id / leaf


def _strict_mission_ledger(runs_dir: Path, mission_id: str) -> None:
    """Prevalidate exact durable bytes before an approval can mutate the ledger."""

    relative = _mission_relative(mission_id, "mission.jsonl")
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            raw = rooted.read_regular(
                relative,
                directory_modes=(0o700, 0o700),
                file_mode=0o600,
                max_bytes=MAX_MISSION_LEDGER_BYTES,
                require_single_link=True,
            )
            events = fleet_json.load_jsonl(raw, require_nonempty=True)
            if raw != fleet_json.canonical_jsonl(events):
                raise fleet_json.FleetJSONError(
                    "mission ledger bytes are not canonical JSONL"
                )
            rooted.assert_root_binding()
    except (fleet_json.FleetJSONError, fleet_safe_paths.SafePathError) as exc:
        raise RuntimeError("mission approval JSON is unsafe or invalid") from exc


def _read_archive_approval(
    runs_dir: Path, mission_id: str
) -> tuple[dict | None, bytes | None]:
    relative = _mission_relative(mission_id, ARCHIVE_APPROVAL_FILE)
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            raw = rooted.read_regular_optional(
                relative,
                directory_modes=(0o700, 0o700),
                file_mode=0o600,
                max_bytes=MAX_ARCHIVE_APPROVAL_BYTES,
                require_single_link=True,
            )
            if raw is None:
                return None, None
            value = fleet_json.loads(raw)
            if (
                not isinstance(value, dict)
                or set(value) != ARCHIVE_APPROVAL_FIELDS
                or raw != fleet_json.canonical_bytes(value) + b"\n"
            ):
                raise fleet_json.FleetJSONError(
                    "archive approval is not canonical closed-schema JSON"
                )
            rooted.assert_root_binding()
            return value, raw
    except (fleet_json.FleetJSONError, fleet_safe_paths.SafePathError) as exc:
        raise RuntimeError("existing archive approval is unsafe or invalid") from exc


def _publish_archive_approval(runs_dir: Path, mission_id: str, value: dict) -> Path:
    relative = _mission_relative(mission_id, ARCHIVE_APPROVAL_FILE)
    content = fleet_json.canonical_bytes(value) + b"\n"
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            path = rooted.atomic_write(
                relative,
                content,
                directory_modes=(0o700, 0o700),
                file_mode=0o600,
                require_absent=True,
            )
            rooted.assert_root_binding()
            return path
    except fleet_safe_paths.SafePathError as exc:
        raise RuntimeError("archive approval publication conflicted") from exc


def _strict_legacy_audit_events(audit: object) -> list[dict]:
    path = Path(audit.AUDIT_PATH)
    try:
        with fleet_safe_paths.RootedFS(path.parent, root_mode=0o700) as rooted:
            raw = rooted.read_regular(
                path.name,
                directory_modes=(),
                file_mode=0o600,
                max_bytes=MAX_LEGACY_AUDIT_BYTES,
                require_single_link=True,
            )
            parsed = fleet_json.load_jsonl(raw)
            if any(not isinstance(event, dict) for event in parsed):
                raise fleet_json.FleetJSONError(
                    "legacy audit records must be JSON objects"
                )
            events = audit.load_and_verify(
                io.StringIO(raw.decode("utf-8", errors="strict"))
            )
            rooted.assert_root_binding()
    except (
        fleet_json.FleetJSONError,
        fleet_safe_paths.SafePathError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise RuntimeError("legacy approval JSON is unsafe or invalid") from exc
    if events != parsed:
        raise RuntimeError("legacy approval JSON verification changed its value")
    return events


def _effect_compiled(runs_dir: Path, mission_id: str) -> dict:
    try:
        compiled, _ = fleet_mission.load_mission_compiled(
            runs_dir, mission_id, mode="effect"
        )
        return compiled
    except (fleet_mission.MissionError, mission_state.MissionStateError) as exc:
        raise RuntimeError(
            f"compiled workflow is not effect-authorized for approval: {exc}"
        ) from exc


def load_audit_common():
    spec = importlib.util.spec_from_file_location("fleet_audit_common", AUDIT_COMMON)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {AUDIT_COMMON}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def approve_mission(
    runs_dir: Path,
    mission_id: str,
    *,
    scope: str,
    expires_in: int,
    idempotency_key: str,
) -> dict:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    if type(expires_in) is not int or expires_in < 60 or expires_in > 86400:
        raise RuntimeError("--expires-in must be between 60 and 86400 seconds")
    if not isinstance(idempotency_key, str) or not mission_state.SAFE_KEY.fullmatch(
        idempotency_key
    ):
        raise RuntimeError("approval idempotency key is invalid")
    _strict_mission_ledger(runs_dir, mission_id)
    compiled = _effect_compiled(runs_dir, mission_id)
    events = mission_state.read_events(
        mission_state.ledger_path(runs_dir, mission_id), expected_mission_id=mission_id
    )
    current = mission_state.derive_state(events)
    request = next(
        (event for event in reversed(events) if event["kind"] == "assurance_requested"),
        None,
    )
    if request is None:
        raise RuntimeError("mission lacks an assurance request")
    if (
        compiled["workflow_digest"] != current["workflow_digest"]
        or compiled["compiled_digest"] != current["compiled_digest"]
    ):
        raise RuntimeError("compiled workflow digest differs from mission state")
    if Path(scope).expanduser().resolve() != Path(current["target_repo"]).resolve():
        raise RuntimeError("approval scope does not match the mission target")
    existing = next(
        (event for event in events if event["idempotency_key"] == idempotency_key), None
    )
    if existing is not None:
        if existing["kind"] != "assurance_approved":
            raise RuntimeError("approval idempotency key is already used")
        payload = existing["payload"]
        if any(
            (
                payload.get("request_event_sha256") != request["event_sha256"],
                payload.get("workflow_digest") != current["workflow_digest"],
                Path(str(payload.get("scope", ""))).resolve()
                != Path(current["target_repo"]).resolve(),
                payload.get("risk") != request["payload"].get("risk"),
                payload.get("expires_in_seconds") != expires_in,
            )
        ):
            raise RuntimeError("approval idempotent replay identity conflicts")
        return {"event": existing, "appended": False}
    if current["status"] != "awaiting_assurance_confirmation":
        raise RuntimeError("mission is not awaiting assurance confirmation")
    actor = os.environ.get("USER", "fleet_controller")
    actor_sha256 = hashlib.sha256(actor.encode("utf-8")).hexdigest()
    approval_id = str(
        uuid.uuid5(
            uuid.UUID(mission_id),
            f"approval:{request['event_sha256']}:{idempotency_key}",
        )
    )
    expires_at = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + expires_in, timezone.utc
    ).isoformat()
    event, appended = mission_state.append_event(
        runs_dir,
        mission_id,
        kind="assurance_approved",
        actor="HUMAN",
        idempotency_key=idempotency_key,
        payload={
            "approval_id": approval_id,
            "request_event_sha256": request["event_sha256"],
            "workflow_digest": current["workflow_digest"],
            "scope": current["target_repo"],
            "risk": current["risk"],
            "expires_at": expires_at,
            "expires_in_seconds": expires_in,
            "approved_by_sha256": actor_sha256,
            "decision": "approved",
        },
    )
    return {"event": event, "appended": appended}


def renew_mission_approval(
    runs_dir: Path,
    mission_id: str,
    *,
    scope: str,
    expires_in: int,
    idempotency_key: str,
) -> dict:
    """Replace one expired approval without changing mission lifecycle state."""

    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    if type(expires_in) is not int or expires_in < 60 or expires_in > 86400:
        raise RuntimeError("--expires-in must be between 60 and 86400 seconds")
    if not isinstance(idempotency_key, str) or not mission_state.SAFE_KEY.fullmatch(
        idempotency_key
    ):
        raise RuntimeError("approval idempotency key is invalid")
    _strict_mission_ledger(runs_dir, mission_id)
    compiled = _effect_compiled(runs_dir, mission_id)
    events = mission_state.read_events(
        mission_state.ledger_path(runs_dir, mission_id),
        expected_mission_id=mission_id,
    )
    current = mission_state.derive_state(events)
    if (
        compiled["workflow_digest"] != current["workflow_digest"]
        or compiled["compiled_digest"] != current["compiled_digest"]
    ):
        raise RuntimeError("compiled workflow digest differs from mission state")
    if Path(scope).expanduser().resolve() != Path(current["target_repo"]).resolve():
        raise RuntimeError("approval scope does not match the mission target")
    actor = os.environ.get("USER", "fleet_controller")
    actor_sha256 = hashlib.sha256(actor.encode("utf-8")).hexdigest()
    existing = next(
        (event for event in events if event["idempotency_key"] == idempotency_key),
        None,
    )
    if existing is not None:
        if existing["kind"] != "assurance_approval_renewed":
            raise RuntimeError("approval renewal idempotency key is already used")
        payload = existing["payload"]
        prior = next(
            (
                event
                for event in events
                if event["event_sha256"] == payload.get("prior_approval_event_sha256")
            ),
            None,
        )
        if (
            prior is None
            or prior["kind"] not in {"assurance_approved", "assurance_approval_renewed"}
            or payload.get("request_event_sha256")
            != prior["payload"].get("request_event_sha256")
            or payload.get("workflow_digest") != current["workflow_digest"]
            or Path(str(payload.get("scope", ""))).resolve()
            != Path(current["target_repo"]).resolve()
            or payload.get("risk") != current["risk"]
            or payload.get("expires_in_seconds") != expires_in
            or payload.get("approved_by_sha256") != actor_sha256
        ):
            raise RuntimeError("approval renewal idempotent replay conflicts")
        return {"event": existing, "appended": False}
    approval = current.get("approval")
    if current["status"] not in {
        "assurance_approved",
        "assured_running",
    } or not isinstance(approval, dict):
        raise RuntimeError(
            "mission approval can renew only while approved or assured-running"
        )
    if current["status"] == "assured_running" and (
        current["run_claims"]
        or current["active_writer"] is not None
        or any(item["active"] for item in current["admissions"].values())
    ):
        raise RuntimeError(
            "assured-running approval renewal requires inactive admissions"
        )
    now = datetime.now(timezone.utc)
    if now < mission_state.parse_timestamp(
        approval["expires_at"], "prior assurance approval expiry"
    ):
        raise RuntimeError("mission approval cannot renew before expiry")
    approval_id = str(
        uuid.uuid5(
            uuid.UUID(mission_id),
            f"approval-renewal:{approval['event_sha256']}:{idempotency_key}",
        )
    )
    expires_at = (now + timedelta(seconds=expires_in)).isoformat()
    event, appended = mission_state.append_event(
        runs_dir,
        mission_id,
        kind="assurance_approval_renewed",
        actor="HUMAN",
        idempotency_key=idempotency_key,
        payload={
            "approval_id": approval_id,
            "prior_approval_event_sha256": approval["event_sha256"],
            "request_event_sha256": approval["request_event_sha256"],
            "workflow_digest": current["workflow_digest"],
            "scope": current["target_repo"],
            "risk": current["risk"],
            "expires_at": expires_at,
            "expires_in_seconds": expires_in,
            "approved_by_sha256": actor_sha256,
            "decision": "approved",
        },
    )
    return {"event": event, "appended": appended}


def approve_archive(
    runs_dir: Path,
    mission_id: str,
    *,
    scope: str,
    expires_in: int,
    idempotency_key: str,
) -> dict:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    if type(expires_in) is not int or expires_in < 60 or expires_in > 86400:
        raise RuntimeError("--expires-in must be between 60 and 86400 seconds")
    if not isinstance(idempotency_key, str) or not mission_state.SAFE_KEY.fullmatch(
        idempotency_key
    ):
        raise RuntimeError("approval idempotency key is invalid")
    _strict_mission_ledger(runs_dir, mission_id)
    compiled = _effect_compiled(runs_dir, mission_id)
    current = fleet_mission.load_state(runs_dir, mission_id)
    if (
        compiled["workflow_digest"] != current["workflow_digest"]
        or compiled["compiled_digest"] != current["compiled_digest"]
    ):
        raise RuntimeError("compiled workflow digest differs from mission state")
    if Path(scope).expanduser().resolve() != Path(current["target_repo"]).resolve():
        raise RuntimeError("archive approval scope does not match the mission target")
    sensitive = sorted(
        set(current["risk_categories"]) & {"credentials", "private_data"}
    )
    if not sensitive:
        raise RuntimeError("mission does not require a sensitive full-archive approval")
    if compiled["workflow"]["archive"]["content_policy"] != "full":
        raise RuntimeError("archive approval applies only to content_policy=full")
    actor = os.environ.get("USER", "fleet_controller")
    actor_sha256 = hashlib.sha256(actor.encode("utf-8")).hexdigest()
    approval_id = str(uuid.uuid5(uuid.UUID(mission_id), f"archive:{idempotency_key}"))
    expires_at = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + expires_in, timezone.utc
    ).isoformat()
    value = {
        "schema_version": 1,
        "mission_id": mission_id,
        "approval_id": approval_id,
        "idempotency_key": idempotency_key,
        "workflow_digest": current["workflow_digest"],
        "scope": current["target_repo"],
        "content_policy": "full",
        "risk_categories": sensitive,
        "expires_at": expires_at,
        "approved_by_sha256": actor_sha256,
        "decision": "approved",
    }
    existing, _ = _read_archive_approval(runs_dir, mission_id)
    path = mission_state.mission_root(runs_dir, mission_id) / ARCHIVE_APPROVAL_FILE
    if existing is not None:
        if existing.get("idempotency_key") != idempotency_key:
            raise RuntimeError("mission already has a different archive approval")
        stable = set(value) - {"expires_at"}
        if any(existing.get(field) != value[field] for field in stable):
            raise RuntimeError("existing archive approval conflicts with the request")
        return {"approval": existing, "appended": False, "path": str(path)}
    path = _publish_archive_approval(runs_dir, mission_id, value)
    return {"approval": value, "appended": True, "path": str(path)}


def approve_legacy(event_id: str, surface: str) -> str:
    audit = load_audit_common()
    events = _strict_legacy_audit_events(audit)
    request = next(
        (event for event in events if event.get("event_id") == event_id),
        None,
    )
    if request is None or request.get("event_type") != "PermissionRequest":
        raise RuntimeError("event-id is not a PermissionRequest")
    if any(
        event.get("event_type") == "HumanApproval"
        and event.get("approval_request_reference") == event_id
        for event in events
    ):
        raise RuntimeError("permission request already has a CONTROL decision")

    actor = os.environ.get("USER", "fleet_controller")
    actor_sha256 = hashlib.sha256(actor.encode("utf-8")).hexdigest()
    timestamp = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    decision = {
        "event_id": str(uuid.uuid4()),
        "timestamp": timestamp,
        "event_type": "HumanApproval",
        "tool_name": request["tool_name"],
        "tool_use_id": request.get("tool_use_id", ""),
        "session_id": request["session_id"],
        "human_uid": f"sha256:{actor_sha256}",
        "agent_uid": "CONTROL",
        "os_uid": os.getuid(),
        "model_version": request["model_version"],
        "data_classification": request["data_classification"],
        "args_sha256": request["args_sha256"],
        "correlation_sha256": request["correlation_sha256"],
        "approval_status": "approved",
        "approval_request_reference": request["event_id"],
        "approval_surface": surface,
        "decision_source": "cmux_control",
    }
    fleet_json.canonical_bytes(decision)
    recorded = audit.append_event(decision)
    subprocess.run(["cmux", "send", "--surface", surface, "1"], check=True)
    subprocess.run(["cmux", "send-key", "--surface", surface, "enter"], check=True)
    return str(recorded["event_id"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-id")
    parser.add_argument("--surface")
    parser.add_argument("--runs-dir")
    parser.add_argument("--mission-id")
    parser.add_argument("--scope")
    parser.add_argument("--expires-in", type=int, default=3600)
    parser.add_argument("--idempotency-key", default="human:assurance:approved")
    parser.add_argument("--archive", action="store_true")
    parser.add_argument("--renew", action="store_true")
    args = parser.parse_args()
    if args.mission_id:
        if not args.runs_dir or not args.scope or args.event_id or args.surface:
            raise RuntimeError(
                "mission approval requires --runs-dir, --mission-id, and --scope only"
            )
        if args.archive and args.renew:
            raise RuntimeError("mission approval cannot combine --archive and --renew")
        approve = (
            approve_archive
            if args.archive
            else renew_mission_approval
            if args.renew
            else approve_mission
        )
        idempotency_key = args.idempotency_key
        if args.renew and idempotency_key == "human:assurance:approved":
            idempotency_key = "human:assurance:renewed"
        value = approve(
            Path(args.runs_dir).expanduser().resolve(),
            args.mission_id,
            scope=args.scope,
            expires_in=args.expires_in,
            idempotency_key=idempotency_key,
        )
        sys.stdout.buffer.write(fleet_json.canonical_bytes(value) + b"\n")
    else:
        if (
            args.archive
            or args.renew
            or not args.event_id
            or not args.surface
            or args.runs_dir
            or args.scope
        ):
            raise RuntimeError("legacy approval requires --event-id and --surface")
        print(f"approval_event_id={approve_legacy(args.event_id, args.surface)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"approval failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
