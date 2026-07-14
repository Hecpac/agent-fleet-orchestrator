#!/usr/bin/env python3
"""Durable capability tokens bound to the verified mission ledger."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

import fleet_mission_state as mission_state


TOKEN_FIELDS = {
    "schema_version",
    "token_id",
    "mission_id",
    "delegation_id",
    "parent_run_id",
    "can_delegate",
    "allowed_capabilities",
    "current_depth",
    "max_depth",
    "remaining_budget",
    "writer_instance",
    "issued_at",
    "token_sha256",
}


class DelegationError(RuntimeError):
    """Capability delegation is invalid, out of scope, or tampered."""


def token_root(runs_dir: Path, mission_id: str) -> Path:
    return mission_state.mission_root(runs_dir, mission_id) / "capability-tokens"


def token_path(runs_dir: Path, mission_id: str, token_id: str) -> Path:
    token_id = mission_state.normalize_uuid(token_id, "token_id")
    return token_root(runs_dir, mission_id) / f"{token_id}.json"


def _token_id(mission_id: str, delegation_id: str) -> str:
    return str(uuid.uuid5(uuid.UUID(mission_id), f"capability-token:{delegation_id}"))


def _validate_token_shape(token: Any) -> dict[str, Any]:
    if not isinstance(token, dict) or set(token) != TOKEN_FIELDS:
        raise DelegationError("capability token fields do not match schema_version=1")
    if token["schema_version"] != 1:
        raise DelegationError("unsupported capability token schema")
    for field in ("token_id", "mission_id", "delegation_id"):
        normalized = mission_state.normalize_uuid(str(token[field]), field)
        if normalized != token[field]:
            raise DelegationError(f"{field} is not canonical")
    if token["parent_run_id"] is not None:
        mission_state.normalize_uuid(str(token["parent_run_id"]), "parent_run_id")
    if not isinstance(token["can_delegate"], bool):
        raise DelegationError("can_delegate must be boolean")
    capabilities = token["allowed_capabilities"]
    if not isinstance(capabilities, list) or not capabilities or any(
        not isinstance(item, str) or not item for item in capabilities
    ) or len(capabilities) != len(set(capabilities)):
        raise DelegationError("allowed_capabilities must be a unique non-empty string list")
    for field in ("current_depth", "max_depth", "remaining_budget"):
        if isinstance(token[field], bool) or not isinstance(token[field], int) or token[field] < 0:
            raise DelegationError(f"{field} must be a non-negative integer")
    if token["current_depth"] > token["max_depth"]:
        raise DelegationError("capability token depth exceeds maximum")
    if not isinstance(token["writer_instance"], str) or not token["writer_instance"]:
        raise DelegationError("writer_instance must be non-empty")
    unsigned = {key: value for key, value in token.items() if key != "token_sha256"}
    if mission_state.sha256(unsigned) != token["token_sha256"]:
        raise DelegationError("capability token hash mismatch")
    return token


def issue_token(
    runs_dir: Path,
    mission_id: str,
    *,
    delegation_id: str,
    parent_run_id: str | None,
    can_delegate: bool,
    allowed_capabilities: list[str],
    current_depth: int,
    max_depth: int,
    remaining_budget: int,
    writer_instance: str,
    idempotency_key: str,
) -> dict[str, Any]:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    delegation_id = mission_state.normalize_uuid(delegation_id, "delegation_id")
    if parent_run_id is not None:
        parent_run_id = mission_state.normalize_uuid(parent_run_id, "parent_run_id")
    token_id = _token_id(mission_id, delegation_id)
    token: dict[str, Any] = {
        "schema_version": 1,
        "token_id": token_id,
        "mission_id": mission_id,
        "delegation_id": delegation_id,
        "parent_run_id": parent_run_id,
        "can_delegate": can_delegate,
        "allowed_capabilities": sorted(set(allowed_capabilities)),
        "current_depth": current_depth,
        "max_depth": max_depth,
        "remaining_budget": remaining_budget,
        "writer_instance": writer_instance,
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    token["token_sha256"] = mission_state.sha256(token)
    _validate_token_shape(token)
    path = token_path(runs_dir, mission_id, token_id)
    content = mission_state.canonical_bytes(token) + b"\n"
    if path.exists():
        try:
            existing = _validate_token_shape(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise DelegationError(f"cannot verify existing capability token: {exc}") from exc
        stable_fields = TOKEN_FIELDS - {"issued_at", "token_sha256"}
        if any(existing[field] != token[field] for field in stable_fields):
            raise DelegationError("existing capability token conflicts with request")
        token = existing
        content = mission_state.canonical_bytes(token) + b"\n"
    else:
        mission_state.atomic_write(path, content)
    mission_state.append_event(
        runs_dir,
        mission_id,
        kind="capability_token_issued",
        actor="CONTROL",
        idempotency_key=idempotency_key,
        payload={
            "token_id": token_id,
            "delegation_id": delegation_id,
            "token_sha256": token["token_sha256"],
        },
    )
    return {**token, "path": str(path)}


def bind_token(
    runs_dir: Path,
    mission_id: str,
    *,
    token_id: str,
    delegation_id: str,
    run_id: str,
    idempotency_key: str,
) -> None:
    mission_state.append_event(
        runs_dir,
        mission_id,
        kind="capability_token_bound",
        actor="CONTROL",
        idempotency_key=idempotency_key,
        payload={
            "token_id": mission_state.normalize_uuid(token_id, "token_id"),
            "delegation_id": mission_state.normalize_uuid(delegation_id, "delegation_id"),
            "run_id": mission_state.normalize_uuid(run_id, "run_id"),
        },
    )


def load_token(runs_dir: Path, mission_id: str, token_id: str) -> dict[str, Any]:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    path = token_path(runs_dir, mission_id, token_id)
    if path.is_symlink():
        raise DelegationError("capability token must not be a symlink")
    try:
        token = _validate_token_shape(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise DelegationError(f"cannot load capability token: {exc}") from exc
    if token["mission_id"] != mission_id:
        raise DelegationError("capability token mission_id mismatch")
    events = mission_state.read_events(
        mission_state.ledger_path(runs_dir, mission_id), expected_mission_id=mission_id
    )
    issued = [
        event
        for event in events
        if event["kind"] == "capability_token_issued"
        and event["payload"].get("token_id") == token_id
    ]
    if len(issued) != 1 or issued[0]["payload"].get("token_sha256") != token["token_sha256"]:
        raise DelegationError("capability token is not bound to the mission ledger")
    return token


def validate_for_subdelegation(
    runs_dir: Path,
    mission_id: str,
    *,
    token_id: str,
    parent_run_id: str,
    capability: str,
    requested_delegation_id: str | None = None,
) -> dict[str, Any]:
    parent_run_id = mission_state.normalize_uuid(parent_run_id, "parent_run_id")
    token = load_token(runs_dir, mission_id, token_id)
    if not token["can_delegate"]:
        raise DelegationError("capability token does not allow subdelegation")
    if capability not in token["allowed_capabilities"]:
        raise DelegationError("capability is outside delegated scope")
    if token["current_depth"] >= token["max_depth"]:
        raise DelegationError("maximum delegation depth reached")
    if token["remaining_budget"] <= 0:
        raise DelegationError("delegated budget is exhausted")
    events = mission_state.read_events(
        mission_state.ledger_path(runs_dir, mission_id), expected_mission_id=mission_id
    )
    bindings = [
        event
        for event in events
        if event["kind"] == "capability_token_bound"
        and event["payload"].get("token_id") == token_id
    ]
    if len(bindings) != 1 or bindings[0]["payload"].get("run_id") != parent_run_id:
        raise DelegationError("capability token is not bound to the delegating run")
    uses = {
        event["payload"]["delegation_id"]
        for event in events
        if event["kind"] == "delegation_registered"
        and event["payload"].get("parent_run_id") == parent_run_id
        and event["payload"].get("delegated_by") == parent_run_id
    }
    if requested_delegation_id is not None:
        uses.discard(requested_delegation_id)
    if len(uses) >= token["remaining_budget"]:
        raise DelegationError("delegated budget is exhausted")
    return token
