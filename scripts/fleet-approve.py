#!/usr/bin/env python3
"""Record a scoped Mission approval or approve a legacy Claude permission."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import fleet_mission_state as mission_state
import fleet_mission


AUDIT_COMMON = Path.home() / ".claude" / "hooks" / "audit_common.py"
ARCHIVE_APPROVAL_FILE = "archive-approval.json"


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
    if expires_in < 60 or expires_in > 86400:
        raise RuntimeError("--expires-in must be between 60 and 86400 seconds")
    events = mission_state.read_events(
        mission_state.ledger_path(runs_dir, mission_id), expected_mission_id=mission_id
    )
    current = mission_state.derive_state(events)
    request = next(
        (event for event in reversed(events) if event["kind"] == "assurance_requested"), None
    )
    if request is None:
        raise RuntimeError("mission lacks an assurance request")
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
            )
        ):
            raise RuntimeError("approval idempotent replay identity conflicts")
        return {"event": existing, "appended": False}
    if current["status"] != "awaiting_assurance_confirmation":
        raise RuntimeError("mission is not awaiting assurance confirmation")
    actor = os.environ.get("USER", "fleet_controller")
    actor_sha256 = hashlib.sha256(actor.encode("utf-8")).hexdigest()
    approval_id = str(
        uuid.uuid5(uuid.UUID(mission_id), f"approval:{request['event_sha256']}:{idempotency_key}")
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
    if expires_in < 60 or expires_in > 86400:
        raise RuntimeError("--expires-in must be between 60 and 86400 seconds")
    current = fleet_mission.load_state(runs_dir, mission_id)
    if Path(scope).expanduser().resolve() != Path(current["target_repo"]).resolve():
        raise RuntimeError("archive approval scope does not match the mission target")
    sensitive = sorted(set(current["risk_categories"]) & {"credentials", "private_data"})
    if not sensitive:
        raise RuntimeError("mission does not require a sensitive full-archive approval")
    root = mission_state.mission_root(runs_dir, mission_id)
    compiled = fleet_mission.validate_compiled(
        json.loads((root / "compiled-workflow.json").read_text(encoding="utf-8"))
    )
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
    path = root / ARCHIVE_APPROVAL_FILE
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("idempotency_key") != idempotency_key:
            raise RuntimeError("mission already has a different archive approval")
        stable = set(value) - {"expires_at"}
        if any(existing.get(field) != value[field] for field in stable):
            raise RuntimeError("existing archive approval conflicts with the request")
        return {"approval": existing, "appended": False, "path": str(path)}
    mission_state.atomic_write(path, mission_state.canonical_bytes(value) + b"\n")
    return {"approval": value, "appended": True, "path": str(path)}


def approve_legacy(event_id: str, surface: str) -> str:
    audit = load_audit_common()
    with audit.AUDIT_PATH.open("r", encoding="utf-8") as handle:
        events = audit.load_and_verify(handle)
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
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
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
    args = parser.parse_args()
    if args.mission_id:
        if not args.runs_dir or not args.scope or args.event_id or args.surface:
            raise RuntimeError(
                "mission approval requires --runs-dir, --mission-id, and --scope only"
            )
        approve = approve_archive if args.archive else approve_mission
        value = approve(
            Path(args.runs_dir).expanduser().resolve(), args.mission_id,
            scope=args.scope, expires_in=args.expires_in,
            idempotency_key=args.idempotency_key,
        )
        print(json.dumps(value, sort_keys=True))
    else:
        if args.archive or not args.event_id or not args.surface or args.runs_dir or args.scope:
            raise RuntimeError("legacy approval requires --event-id and --surface")
        print(f"approval_event_id={approve_legacy(args.event_id, args.surface)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"approval failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
