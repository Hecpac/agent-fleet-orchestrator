#!/usr/bin/env python3
"""Durable capability tokens bound to the verified mission ledger."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any
import uuid

import fleet_mission
import fleet_mission_state as mission_state
import fleet_safe_paths


TOKEN_FIELDS_V1 = {
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
TOKEN_FIELDS_V2 = TOKEN_FIELDS_V1 | {"allowed_artifact_ids"}
TOKEN_ADMISSION_PROOF_FIELDS_LEGACY = {
    "run_id",
    "admission_id",
    "reservation_event_sha256",
    "commit_id",
    "request_digest",
}
TOKEN_ADMISSION_PROOF_FIELDS = TOKEN_ADMISSION_PROOF_FIELDS_LEGACY | {"effect_sha256"}
TOKEN_FIELDS_V3_LEGACY = TOKEN_FIELDS_V2 | TOKEN_ADMISSION_PROOF_FIELDS_LEGACY
TOKEN_FIELDS = TOKEN_FIELDS_V2 | TOKEN_ADMISSION_PROOF_FIELDS
BUDGET_EVENT_KIND = "delegation_budget_allocated"
BUDGET_PAYLOAD_FIELDS = {
    "token_id",
    "delegation_id",
    "parent_run_id",
    "allocations",
}
BUDGET_ALLOCATION_FIELDS = {
    "requested_delegation_id",
    "capability",
    "child_can_delegate",
    "child_allowed_capabilities",
    "requested_artifact_ids",
    "edge_cost",
    "delegated_budget",
    "total_cost",
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


def _token_relative(mission_id: str, name: str) -> Path:
    return Path("missions") / mission_id / "capability-tokens" / name


def _events(runs_dir: Path, mission_id: str) -> list[dict[str, Any]]:
    return mission_state.read_events(
        mission_state.ledger_path(runs_dir, mission_id), expected_mission_id=mission_id
    )


def _canonical_capabilities(value: Any, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise DelegationError("allowed_capabilities must be a string list")
    if len(value) != len(set(value)):
        raise DelegationError("allowed_capabilities must be unique")
    canonical = sorted(value)
    if not allow_empty and not canonical:
        raise DelegationError(
            "allowed_capabilities must be non-empty when delegation is allowed"
        )
    return canonical


def _canonical_artifact_ids(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not mission_state.SHA256.fullmatch(item)
        for item in value
    ):
        raise DelegationError(f"{where} must be a SHA-256 list")
    if len(value) != len(set(value)):
        raise DelegationError(f"{where} must be unique")
    return sorted(value)


def _validate_token_shape(token: Any) -> dict[str, Any]:
    if not isinstance(token, dict):
        raise DelegationError("capability token must be an object")
    schema_version = token.get("schema_version")
    expected_fields = {1: TOKEN_FIELDS_V1, 2: TOKEN_FIELDS_V2}.get(schema_version)
    if schema_version == 3 and set(token) in (TOKEN_FIELDS_V3_LEGACY, TOKEN_FIELDS):
        expected_fields = set(token)
    if expected_fields is None or isinstance(schema_version, bool):
        raise DelegationError("unsupported capability token schema")
    if set(token) != expected_fields:
        raise DelegationError(
            f"capability token fields do not match schema_version={schema_version}"
        )
    for field in ("token_id", "mission_id", "delegation_id"):
        normalized = mission_state.normalize_uuid(str(token[field]), field)
        if normalized != token[field]:
            raise DelegationError(f"{field} is not canonical")
    if token["parent_run_id"] is not None:
        normalized = mission_state.normalize_uuid(
            str(token["parent_run_id"]), "parent_run_id"
        )
        if normalized != token["parent_run_id"]:
            raise DelegationError("parent_run_id is not canonical")
    if not isinstance(token["can_delegate"], bool):
        raise DelegationError("can_delegate must be boolean")
    capabilities = _canonical_capabilities(
        token["allowed_capabilities"], allow_empty=not token["can_delegate"]
    )
    if schema_version in {2, 3} and capabilities != token["allowed_capabilities"]:
        raise DelegationError("allowed_capabilities are not canonical")
    if schema_version == 3 and not token["can_delegate"] and capabilities:
        raise DelegationError(
            "non-delegating capability token cannot retain capability grants"
        )
    for field in ("current_depth", "max_depth", "remaining_budget"):
        if (
            isinstance(token[field], bool)
            or not isinstance(token[field], int)
            or token[field] < 0
        ):
            raise DelegationError(f"{field} must be a non-negative integer")
    if token["current_depth"] > token["max_depth"]:
        raise DelegationError("capability token depth exceeds maximum")
    if schema_version == 3 and token["can_delegate"] and token["remaining_budget"] < 1:
        raise DelegationError("delegating capability token requires positive budget")
    if (
        schema_version == 3
        and not token["can_delegate"]
        and token["remaining_budget"] != 0
    ):
        raise DelegationError("non-delegating capability token budget must be 0")
    if not isinstance(token["writer_instance"], str) or not token["writer_instance"]:
        raise DelegationError("writer_instance must be non-empty")
    if not isinstance(token["issued_at"], str):
        raise DelegationError("issued_at must be a timestamp string")
    mission_state.parse_timestamp(token["issued_at"], "capability token issued_at")
    if schema_version == 3:
        for field in ("run_id", "admission_id", "commit_id"):
            normalized = mission_state.normalize_uuid(str(token[field]), field)
            if normalized != token[field]:
                raise DelegationError(f"{field} is not canonical")
        for field in (
            "reservation_event_sha256",
            "request_digest",
            *(("effect_sha256",) if "effect_sha256" in token else ()),
        ):
            if not isinstance(token[field], str) or not mission_state.SHA256.fullmatch(
                token[field]
            ):
                raise DelegationError(f"{field} must be SHA-256")
    unsigned = {key: value for key, value in token.items() if key != "token_sha256"}
    if mission_state.sha256(unsigned) != token["token_sha256"]:
        raise DelegationError("capability token hash mismatch")
    if schema_version >= 2:
        artifact_ids = _canonical_artifact_ids(
            token["allowed_artifact_ids"], "allowed_artifact_ids"
        )
        if artifact_ids != token["allowed_artifact_ids"]:
            raise DelegationError("allowed_artifact_ids are not canonical")
    return token


def _load_token_bytes(raw: bytes) -> dict[str, Any]:
    """Parse one token without JSON ambiguity and bind it to canonical bytes."""
    try:
        value = mission_state.loads_strict(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DelegationError(f"cannot load capability token: {exc}") from exc
    token = _validate_token_shape(value)
    if raw != mission_state.canonical_bytes(token) + b"\n":
        raise DelegationError("capability token bytes are not canonical")
    return token


def _read_token_from_root(
    rooted: fleet_safe_paths.RootedFS,
    mission_id: str,
    token_id: str,
) -> dict[str, Any]:
    """Read one immutable token file from an already pinned runs root."""

    token = _load_token_bytes(
        rooted.read_regular(
            _token_relative(mission_id, f"{token_id}.json"),
            directory_modes=(0o700, 0o700, 0o700),
            file_mode=0o600,
            max_bytes=1024 * 1024,
        )
    )
    rooted.assert_root_binding()
    if token["mission_id"] != mission_id:
        raise DelegationError("capability token mission_id mismatch")
    if token["token_id"] != token_id:
        raise DelegationError("capability token token_id mismatch")
    return token


def _read_token_file(
    runs_dir: Path,
    mission_id: str,
    token_id: str,
) -> dict[str, Any]:
    """Read a token file without opening or re-entering a mission transaction."""

    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            return _read_token_from_root(rooted, mission_id, token_id)
    except fleet_safe_paths.SafePathError as exc:
        raise DelegationError(f"unsafe capability token store: {exc}") from exc


def _frozen_workflow(
    current: dict[str, Any],
    compiled: dict[str, Any],
) -> dict[str, Any]:
    policy = current.get("admission_policy")
    if not isinstance(policy, dict):
        raise DelegationError("schema_version=3 token requires frozen admission policy")
    if policy.get("workflow_digest") != compiled.get("workflow_digest") or policy.get(
        "compiled_digest"
    ) != compiled.get("compiled_digest"):
        raise DelegationError("capability token frozen workflow policy mismatch")
    workflow = compiled.get("workflow")
    if not isinstance(
        workflow, dict
    ):  # pragma: no cover - compiled loader closes this.
        raise DelegationError("capability token compiled workflow is malformed")
    return workflow


def _compiled_writer_instance(compiled: dict[str, Any]) -> str:
    resolved = compiled.get("resolved")
    if not isinstance(
        resolved, dict
    ):  # pragma: no cover - compiled loader closes this.
        raise DelegationError("capability token compiled roster is malformed")
    members = ([resolved["lead"]] if resolved.get("lead") else []) + list(
        resolved.get("instances") or []
    )
    writers = [
        member.get("instance_id")
        for member in members
        if isinstance(member, dict) and member.get("authority") == "write"
    ]
    if len(writers) > 1 or any(not isinstance(writer, str) for writer in writers):
        raise DelegationError("capability token compiled writer policy is ambiguous")
    return writers[0] if writers else "none"


def _compiled_snapshot(
    runs_dir: Path,
    mission_id: str,
    current: dict[str, Any],
) -> dict[str, Any]:
    try:
        return fleet_mission.load_snapshot_compiled(
            runs_dir,
            mission_id,
            current,
            mode="effect",
        )
    except fleet_mission.MissionError as exc:
        raise DelegationError(
            "capability token cannot bind its compiled workflow policy"
        ) from exc


def _token_issuance_snapshot(
    events: list[dict[str, Any]],
    token: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    matches = [
        (index, event)
        for index, event in enumerate(events)
        if event["kind"] == "capability_token_issued"
        and event["payload"].get("token_id") == token["token_id"]
    ]
    if len(matches) != 1:
        raise DelegationError("capability token has no unique issuance frontier")
    index, event = matches[0]
    if (
        event["payload"].get("delegation_id") != token["delegation_id"]
        or event["payload"].get("token_sha256") != token["token_sha256"]
    ):
        raise DelegationError("capability token issuance frontier mismatch")
    issuance_events = events[: index + 1]
    return issuance_events, mission_state.derive_state(issuance_events)


def _validate_bound_token_snapshot(
    runs_dir: Path,
    mission_id: str,
    *,
    token_id: str,
    delegation_id: str,
    run_id: str,
    events: list[dict[str, Any]],
    current: dict[str, Any],
    compiled: dict[str, Any] | None,
    require_v3: bool,
    validation_stack: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Validate one token entirely against a caller-owned ledger snapshot."""

    if token_id in validation_stack:
        raise DelegationError("capability token authority lineage is cyclic")
    token = _read_token_file(runs_dir, mission_id, token_id)
    issuance_events, issuance_state = _token_issuance_snapshot(events, token)
    if require_v3 and (token["schema_version"] != 3 or "effect_sha256" not in token):
        raise DelegationError("capability token schema_version=3 is required")
    if token["delegation_id"] != delegation_id:
        raise DelegationError("capability token delegation_id mismatch")
    if token["schema_version"] == 3 and token["run_id"] != run_id:
        raise DelegationError("capability token run_id mismatch")
    bindings = [
        event
        for event in events
        if event["kind"] == "capability_token_bound"
        and event["payload"].get("token_id") == token_id
    ]
    expected = {
        "token_id": token_id,
        "delegation_id": delegation_id,
        "run_id": run_id,
    }
    if len(bindings) != 1 or bindings[0]["payload"] != expected:
        raise DelegationError(
            "capability token is not bound to the exact delegation and run"
        )
    if token["schema_version"] == 3 and "effect_sha256" in token:
        if compiled is None:
            compiled = _compiled_snapshot(runs_dir, mission_id, current)
        _validate_v3_authority(
            runs_dir,
            mission_id,
            token=token,
            events=events,
            current=current,
            issuance_events=issuance_events,
            issuance_state=issuance_state,
            compiled=compiled,
            effect_stage=False,
            validation_stack=validation_stack | {token_id},
        )
        return token
    registered = current["delegations"].get(delegation_id)
    if registered is not None and (
        registered["run_id"] != run_id
        or registered["token_id"] != token_id
        or registered["parent_run_id"] != token["parent_run_id"]
        or registered["depth"] != token["current_depth"]
        or (
            token["schema_version"] >= 2
            and sorted(registered["input_artifact_ids"])
            != token["allowed_artifact_ids"]
        )
    ):
        raise DelegationError(
            "capability token conflicts with registered delegation authority"
        )
    return token


def _attested_direct_child_results(
    runs_dir: Path,
    mission_id: str,
    events: list[dict[str, Any]],
    current: dict[str, Any],
    compiled: dict[str, Any],
    *,
    parent_run_id: str,
    parent_admission_id: str,
    validation_stack: frozenset[str] = frozenset(),
) -> set[str]:
    artifacts: set[str] = set()
    for delegation_id, child in current.get("delegations", {}).items():
        if (
            not isinstance(child, dict)
            or child.get("parent_run_id") != parent_run_id
            or child.get("delegated_by") != parent_run_id
        ):
            continue
        run_id = child.get("run_id")
        owner = current.get("run_owners", {}).get(run_id)
        if not isinstance(owner, dict) or owner.get("owner_kind") != "admission":
            continue
        admission = current.get("admissions", {}).get(owner.get("owner_id"))
        result = current.get("results", {}).get(delegation_id)
        durable_result = (
            admission.get("result") if isinstance(admission, dict) else None
        )
        if (
            not isinstance(admission, dict)
            or admission.get("parent_admission_id") != parent_admission_id
            or admission.get("delegation_id") != delegation_id
            or admission.get("run_id") != run_id
            or not isinstance(result, dict)
            or not isinstance(durable_result, dict)
            or durable_result.get("artifact_id") != result.get("artifact_id")
            or not mission_state.SHA256.fullmatch(str(result.get("artifact_id", "")))
        ):
            continue
        child_token_id = child.get("token_id")
        if child_token_id != _token_id(mission_id, str(delegation_id)):
            continue
        try:
            child_token = _validate_bound_token_snapshot(
                runs_dir,
                mission_id,
                token_id=str(child_token_id),
                delegation_id=str(delegation_id),
                run_id=str(run_id),
                events=events,
                current=current,
                compiled=compiled,
                require_v3=True,
                validation_stack=validation_stack,
            )
        except DelegationError:
            continue
        if child_token.get("admission_id") != admission.get("admission_id"):
            continue
        artifacts.add(str(result["artifact_id"]))
    return artifacts


def _validate_v3_authority(
    runs_dir: Path,
    mission_id: str,
    *,
    token: dict[str, Any],
    events: list[dict[str, Any]],
    current: dict[str, Any],
    issuance_events: list[dict[str, Any]],
    issuance_state: dict[str, Any],
    compiled: dict[str, Any],
    effect_stage: bool,
    validation_stack: frozenset[str] = frozenset(),
) -> None:
    """Re-derive every v3 grant from frozen policy and admission lineage."""

    if token.get("schema_version") != 3:
        raise DelegationError("capability token schema_version=3 is required")
    if "effect_sha256" not in token:
        raise DelegationError(
            "historical schema_version=3 token is read-only without effect_sha256"
        )
    workflow = _frozen_workflow(current, compiled)
    issuance_policy = issuance_state.get("admission_policy")
    current_policy = current.get("admission_policy")
    if not isinstance(issuance_policy, dict) or issuance_policy.get(
        "event_sha256"
    ) != current_policy.get("event_sha256"):
        raise DelegationError("capability token issuance predates frozen policy")

    durable = current.get("admissions", {}).get(token["admission_id"])
    issuance_admission = issuance_state.get("admissions", {}).get(token["admission_id"])
    exact = {
        "run_kind": "specialist",
        "delegation_id": token["delegation_id"],
        "run_id": token["run_id"],
        "parent_run_id": token["parent_run_id"],
        "delegated_budget": token["remaining_budget"],
        "reservation_event_sha256": token["reservation_event_sha256"],
        "request_digest": token["request_digest"],
        "effect_sha256": token["effect_sha256"],
    }
    if (
        not isinstance(durable, dict)
        or not isinstance(issuance_admission, dict)
        or any(durable.get(field) != value for field, value in exact.items())
        or any(issuance_admission.get(field) != value for field, value in exact.items())
    ):
        raise DelegationError("capability token admission proof is not exact")
    task_sha256 = durable.get("task_sha256")
    if (
        not isinstance(task_sha256, str)
        or not mission_state.SHA256.fullmatch(task_sha256)
        or issuance_admission.get("task_sha256") != task_sha256
    ):
        raise DelegationError("capability token admission task proof is not exact")
    if issuance_admission.get("phase") not in {
        "reserved",
        "committed",
        "authorized",
        "started",
    }:
        raise DelegationError("capability token admission was not issuable")
    if effect_stage and (
        durable.get("phase") not in {"reserved", "committed", "authorized", "started"}
        or not durable.get("active")
        or token["run_id"] in current.get("cancelled_runs", {})
    ):
        raise DelegationError("capability token admission is not effect-capable")
    if durable.get("phase") in {"aborted"}:
        raise DelegationError("capability token admission was aborted")

    expected_commit_id = str(
        uuid.uuid5(uuid.UUID(mission_id), f"commit:{token['admission_id']}")
    )
    if token["commit_id"] != expected_commit_id:
        raise DelegationError("capability token commit proof is invalid")
    commit = durable.get("commit")
    if commit is not None and (
        not isinstance(commit, dict)
        or commit.get("commit_id") != token["commit_id"]
        or commit.get("run_id") != token["run_id"]
        or commit.get("request_digest") != token["request_digest"]
        or commit.get("effect_sha256") != token["effect_sha256"]
        or commit.get("reservation_event_sha256") != token["reservation_event_sha256"]
    ):
        raise DelegationError("capability token durable commit proof is invalid")
    if durable.get("phase") != "reserved" and commit is None:
        raise DelegationError("capability token durable commit proof is missing")
    authorization = durable.get("launch_authorization")
    if authorization is not None and (
        not isinstance(authorization, dict)
        or not isinstance(commit, dict)
        or authorization.get("commit_event_sha256") != commit.get("event_sha256")
        or authorization.get("request_digest") != token["request_digest"]
        or authorization.get("effect_sha256") != token["effect_sha256"]
        or authorization.get("run_id") != token["run_id"]
        or authorization.get("recipient_instance") != durable.get("recipient_instance")
        or authorization.get("writer") != durable.get("writer")
    ):
        raise DelegationError(
            "capability token launch authorization frontier is invalid"
        )
    if durable.get("phase") in {"authorized", "started"} and authorization is None:
        raise DelegationError(
            "capability token launch authorization frontier is missing"
        )
    if durable.get("phase") != "reserved" and current.get("run_owners", {}).get(
        token["run_id"]
    ) != {"owner_kind": "admission", "owner_id": token["admission_id"]}:
        raise DelegationError("capability token run ownership proof is invalid")

    parent_id = durable.get("parent_admission_id")
    if parent_id != issuance_admission.get("parent_admission_id"):
        raise DelegationError("capability token parent admission changed")
    parent = issuance_state.get("admissions", {}).get(parent_id)
    if (
        not isinstance(parent, dict)
        or parent.get("run_id") != token["parent_run_id"]
        or parent.get("phase") != "started"
        or not parent.get("active")
    ):
        raise DelegationError(
            "capability token requires an exact started parent admission"
        )
    durable_parent = current.get("admissions", {}).get(parent_id)
    if effect_stage and (
        not isinstance(durable_parent, dict)
        or durable_parent.get("run_id") != token["parent_run_id"]
        or durable_parent.get("phase") != "started"
        or not durable_parent.get("active")
        or durable_parent.get("run_id") in current.get("cancelled_runs", {})
    ):
        raise DelegationError(
            "capability token requires a currently active parent admission"
        )

    capabilities = set(workflow["capabilities"]["available"])
    autonomy = workflow["autonomy"]
    if durable.get("capability") not in capabilities:
        raise DelegationError("capability token admission capability is outside policy")
    if not set(token["allowed_capabilities"]) <= capabilities:
        raise DelegationError("capability token scope exceeds frozen workflow")
    expected_max_depth = int(autonomy["max_delegation_depth"])
    expected_delegated_by: str
    if parent.get("run_kind") == "lead":
        expected_depth = 0
        expected_delegated_by = "lead"
    elif parent.get("run_kind") == "specialist":
        parent_delegation_id = parent.get("delegation_id")
        if not isinstance(parent_delegation_id, str):
            raise DelegationError("capability token parent lacks delegation identity")
        parent_token = _validate_bound_token_snapshot(
            runs_dir,
            mission_id,
            token_id=_token_id(mission_id, parent_delegation_id),
            delegation_id=parent_delegation_id,
            run_id=str(parent["run_id"]),
            events=events,
            current=current,
            compiled=compiled,
            require_v3=True,
            validation_stack=validation_stack,
        )
        if parent_token.get("admission_id") != parent_id:
            raise DelegationError("capability token parent admission proof mismatch")
        if not parent_token["can_delegate"]:
            raise DelegationError("capability token parent cannot subdelegate")
        if durable.get("capability") not in set(parent_token["allowed_capabilities"]):
            raise DelegationError(
                "capability token admission capability exceeds parent token"
            )
        if not set(token["allowed_capabilities"]) <= set(
            parent_token["allowed_capabilities"]
        ):
            raise DelegationError("capability token scope exceeds parent token")
        parent_artifacts = set(parent_token["allowed_artifact_ids"])
        parent_artifacts.update(
            _attested_direct_child_results(
                runs_dir,
                mission_id,
                issuance_events,
                issuance_state,
                compiled,
                parent_run_id=str(parent["run_id"]),
                parent_admission_id=parent_id,
                validation_stack=validation_stack,
            )
        )
        if not set(token["allowed_artifact_ids"]) <= parent_artifacts:
            raise DelegationError(
                "capability token artifacts exceed parent grants and attested children"
            )
        expected_depth = int(parent_token["current_depth"]) + 1
        expected_delegated_by = str(parent["run_id"])
    else:  # pragma: no cover - admission validation closes this registry.
        raise DelegationError("capability token parent kind is unsupported")

    if (
        token["current_depth"] != expected_depth
        or token["max_depth"] != expected_max_depth
    ):
        raise DelegationError(
            "capability token depth differs from frozen admission lineage"
        )
    if token["can_delegate"] and (
        not autonomy["allow_subdelegation"]
        or token["current_depth"] >= token["max_depth"]
    ):
        raise DelegationError("frozen workflow disables this token subdelegation")
    if token["writer_instance"] != _compiled_writer_instance(compiled):
        raise DelegationError(
            "capability token writer scope differs from frozen roster"
        )

    registered = current.get("delegations", {}).get(token["delegation_id"])
    if registered is not None:
        registered_exact = {
            "run_id": token["run_id"],
            "token_id": token["token_id"],
            "parent_run_id": token["parent_run_id"],
            "delegated_by": expected_delegated_by,
            "recipient_instance": durable["recipient_instance"],
            "capability": durable["capability"],
            "depth": token["current_depth"],
            "input_artifact_ids": token["allowed_artifact_ids"],
        }
        if not isinstance(registered, dict) or any(
            registered.get(field) != value for field, value in registered_exact.items()
        ):
            raise DelegationError(
                "capability token conflicts with registered delegation authority"
            )


def issue_token(
    runs_dir: Path,
    mission_id: str,
    *,
    delegation_id: str,
    parent_run_id: str | None,
    can_delegate: bool,
    allowed_capabilities: list[str],
    allowed_artifact_ids: list[str],
    current_depth: int,
    max_depth: int,
    remaining_budget: int,
    writer_instance: str,
    idempotency_key: str,
    run_id: str | None = None,
    admission_id: str | None = None,
    reservation_event_sha256: str | None = None,
    commit_id: str | None = None,
    request_digest: str | None = None,
) -> dict[str, Any]:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    delegation_id = mission_state.normalize_uuid(delegation_id, "delegation_id")
    if parent_run_id is not None:
        parent_run_id = mission_state.normalize_uuid(parent_run_id, "parent_run_id")
    canonical_capabilities = _canonical_capabilities(
        allowed_capabilities, allow_empty=not can_delegate
    )
    if canonical_capabilities != allowed_capabilities:
        raise DelegationError("allowed_capabilities are not canonical")
    canonical_artifacts = _canonical_artifact_ids(
        allowed_artifact_ids, "allowed_artifact_ids"
    )
    if canonical_artifacts != allowed_artifact_ids:
        raise DelegationError("allowed_artifact_ids are not canonical")
    proof_values = (
        run_id,
        admission_id,
        reservation_event_sha256,
        commit_id,
        request_digest,
    )
    if any(value is not None for value in proof_values) and any(
        value is None for value in proof_values
    ):
        raise DelegationError("schema_version=3 admission proof must be complete")
    schema_version = 3 if all(value is not None for value in proof_values) else 2
    if schema_version == 3:
        run_id = mission_state.normalize_uuid(str(run_id), "run_id")
        admission_id = mission_state.normalize_uuid(str(admission_id), "admission_id")
        commit_id = mission_state.normalize_uuid(str(commit_id), "commit_id")
        for value, where in (
            (reservation_event_sha256, "reservation_event_sha256"),
            (request_digest, "request_digest"),
        ):
            if not isinstance(value, str) or not mission_state.SHA256.fullmatch(value):
                raise DelegationError(f"{where} must be SHA-256")
    token_id = _token_id(mission_id, delegation_id)
    token_name = f"{token_id}.json"
    token_relative = _token_relative(mission_id, token_name)
    lock_relative = _token_relative(mission_id, f".{token_id}.lock")
    try:
        # Global order: mission lock, then token lock. Every authority check,
        # file reconciliation, and ledger publication uses this one snapshot.
        with mission_state.MissionTransaction(runs_dir, mission_id) as transaction:
            events = list(transaction.events)
            current = transaction.current_state
            if current is None:
                raise DelegationError("capability token mission does not exist")
            compiled = (
                _compiled_snapshot(runs_dir, mission_id, current)
                if schema_version == 3
                else None
            )
            if schema_version != 3 and current["admission_policy"] is not None:
                raise DelegationError(
                    "frozen admission policy requires schema_version=3 token proof"
                )
            effect_sha256: str | None = None
            if schema_version == 3:
                durable = current["admissions"].get(admission_id)
                effect_sha256 = (
                    durable.get("effect_sha256") if isinstance(durable, dict) else None
                )
                if not isinstance(
                    effect_sha256, str
                ) or not mission_state.SHA256.fullmatch(effect_sha256):
                    raise DelegationError(
                        "schema_version=3 token admission lacks effect_sha256"
                    )
            token: dict[str, Any] = {
                "schema_version": schema_version,
                "token_id": token_id,
                "mission_id": mission_id,
                "delegation_id": delegation_id,
                "parent_run_id": parent_run_id,
                "can_delegate": can_delegate,
                "allowed_capabilities": canonical_capabilities,
                "allowed_artifact_ids": canonical_artifacts,
                "current_depth": current_depth,
                "max_depth": max_depth,
                "remaining_budget": remaining_budget,
                "writer_instance": writer_instance,
                "issued_at": datetime.now(timezone.utc).isoformat(),
            }
            if schema_version == 3:
                token.update(
                    {
                        "run_id": run_id,
                        "admission_id": admission_id,
                        "reservation_event_sha256": reservation_event_sha256,
                        "commit_id": commit_id,
                        "request_digest": request_digest,
                        "effect_sha256": effect_sha256,
                    }
                )
            token["token_sha256"] = mission_state.sha256(token)
            _validate_token_shape(token)
            if schema_version == 3:
                _validate_v3_authority(
                    runs_dir,
                    mission_id,
                    token=token,
                    events=events,
                    current=current,
                    issuance_events=events,
                    issuance_state=current,
                    compiled=compiled,
                    effect_stage=True,
                    validation_stack=frozenset({token_id}),
                )

            issued = [
                event
                for event in events
                if event["kind"] == "capability_token_issued"
                and event["payload"].get("token_id") == token_id
            ]
            if len(issued) > 1:
                raise DelegationError("capability token has multiple issuance events")

            with fleet_safe_paths.RootedFS(runs_dir) as rooted:
                with rooted.exclusive_lock(
                    lock_relative,
                    directory_modes=(0o700, 0o700, 0o700),
                    file_mode=0o600,
                ):
                    names = rooted.list_directory(
                        token_relative.parent,
                        directory_modes=(0o700, 0o700, 0o700),
                    )
                    exists = token_name in names
                    if issued and not exists:
                        raise DelegationError(
                            "capability token issuance has no durable token file"
                        )
                    if exists:
                        existing = _read_token_from_root(rooted, mission_id, token_id)
                        if existing["schema_version"] != schema_version:
                            raise DelegationError(
                                "existing capability token schema conflicts with request"
                            )
                        if schema_version == 3 and set(existing) != TOKEN_FIELDS:
                            raise DelegationError(
                                "existing historical v3 token is read-only"
                            )
                        token_fields = (
                            TOKEN_FIELDS if schema_version == 3 else TOKEN_FIELDS_V2
                        )
                        stable_fields = token_fields - {"issued_at", "token_sha256"}
                        if any(
                            existing[field] != token[field] for field in stable_fields
                        ):
                            raise DelegationError(
                                "existing capability token conflicts with request"
                            )
                        token = existing
                    if issued:
                        payload = issued[0]["payload"]
                        if (
                            payload.get("delegation_id") != delegation_id
                            or payload.get("token_sha256") != token["token_sha256"]
                        ):
                            raise DelegationError(
                                "capability token issuance conflicts with request"
                            )
                    else:
                        # File-first publication permits exact recovery after a
                        # process crash, while the mission lock prevents any
                        # authority transition from interleaving here.
                        if not exists:
                            rooted.atomic_write(
                                token_relative,
                                mission_state.canonical_bytes(token) + b"\n",
                                directory_modes=(0o700, 0o700, 0o700),
                                file_mode=0o600,
                            )
                            if (
                                os.environ.get("FLEET_TEST_DELEGATION_CRASH_AT")
                                == "after_token_publish"
                            ):
                                os._exit(137)
                        if schema_version == 3:
                            _validate_v3_authority(
                                runs_dir,
                                mission_id,
                                token=token,
                                events=events,
                                current=current,
                                issuance_events=events,
                                issuance_state=current,
                                compiled=compiled,
                                effect_stage=True,
                                validation_stack=frozenset({token_id}),
                            )
                        transaction.append_event(
                            kind="capability_token_issued",
                            actor="CONTROL",
                            idempotency_key=idempotency_key,
                            payload={
                                "token_id": token_id,
                                "delegation_id": delegation_id,
                                "token_sha256": token["token_sha256"],
                            },
                        )
                    rooted.assert_root_binding()
                    path = rooted.root / token_relative
    except fleet_safe_paths.SafePathError as exc:
        raise DelegationError(f"unsafe capability token store: {exc}") from exc
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
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    token_id = mission_state.normalize_uuid(token_id, "token_id")
    delegation_id = mission_state.normalize_uuid(delegation_id, "delegation_id")
    run_id = mission_state.normalize_uuid(run_id, "run_id")
    payload = {
        "token_id": token_id,
        "delegation_id": delegation_id,
        "run_id": run_id,
    }
    try:
        # Keep the same mission -> token order used by issuance and budget
        # allocation so no path can form a lock cycle.
        with mission_state.MissionTransaction(runs_dir, mission_id) as transaction:
            events = list(transaction.events)
            current = transaction.current_state
            if current is None:
                raise DelegationError("capability token mission does not exist")
            with fleet_safe_paths.RootedFS(runs_dir) as rooted:
                with rooted.exclusive_lock(
                    _token_relative(mission_id, f".{token_id}.lock"),
                    directory_modes=(0o700, 0o700, 0o700),
                    file_mode=0o600,
                ):
                    token = _read_token_from_root(rooted, mission_id, token_id)
                    compiled = (
                        _compiled_snapshot(runs_dir, mission_id, current)
                        if token["schema_version"] == 3
                        and "effect_sha256" in token
                        else None
                    )
                    issuance_events, issuance_state = _token_issuance_snapshot(
                        events, token
                    )
                    if token["delegation_id"] != delegation_id:
                        raise DelegationError("capability token delegation_id mismatch")
                    if token["schema_version"] == 3 and token["run_id"] != run_id:
                        raise DelegationError("capability token run_id mismatch")
                    bindings = [
                        event
                        for event in events
                        if event["kind"] == "capability_token_bound"
                        and event["payload"].get("token_id") == token_id
                    ]
                    if len(bindings) > 1:
                        raise DelegationError(
                            "capability token has multiple binding events"
                        )
                    if token["schema_version"] == 3 and "effect_sha256" in token:
                        _validate_v3_authority(
                            runs_dir,
                            mission_id,
                            token=token,
                            events=events,
                            current=current,
                            issuance_events=issuance_events,
                            issuance_state=issuance_state,
                            compiled=compiled,
                            effect_stage=True,
                            validation_stack=frozenset({token_id}),
                        )
                    if bindings:
                        if bindings[0]["payload"] != payload:
                            raise DelegationError(
                                "capability token is already bound to another delegation or run"
                            )
                        return
                    if current.get("admission_policy") is not None and (
                        token["schema_version"] != 3 or "effect_sha256" not in token
                    ):
                        raise DelegationError(
                            "frozen admission policy makes legacy token binding read-only"
                        )
                    transaction.append_event(
                        kind="capability_token_bound",
                        actor="CONTROL",
                        idempotency_key=idempotency_key,
                        payload=payload,
                    )
                    rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise DelegationError(f"unsafe capability token store: {exc}") from exc


def load_token(runs_dir: Path, mission_id: str, token_id: str) -> dict[str, Any]:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    token_id = mission_state.normalize_uuid(token_id, "token_id")
    with mission_state.MissionTransaction(runs_dir, mission_id) as transaction:
        token = _read_token_file(runs_dir, mission_id, token_id)
        try:
            _token_issuance_snapshot(list(transaction.events), token)
        except DelegationError as exc:
            raise DelegationError(
                "capability token is not bound to the mission ledger"
            ) from exc
        return token


def validate_bound_token(
    runs_dir: Path,
    mission_id: str,
    *,
    token_id: str,
    delegation_id: str,
    run_id: str,
    require_v3: bool = False,
) -> dict[str, Any]:
    """Load a token and require its one exact durable delegation/run binding."""
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    token_id = mission_state.normalize_uuid(token_id, "token_id")
    delegation_id = mission_state.normalize_uuid(delegation_id, "delegation_id")
    run_id = mission_state.normalize_uuid(run_id, "run_id")
    with mission_state.MissionTransaction(runs_dir, mission_id) as transaction:
        events = list(transaction.events)
        current = transaction.current_state
        if current is None:
            raise DelegationError("capability token mission does not exist")
        return _validate_bound_token_snapshot(
            runs_dir,
            mission_id,
            token_id=token_id,
            delegation_id=delegation_id,
            run_id=run_id,
            events=events,
            current=current,
            compiled=None,
            require_v3=require_v3,
        )


def _require_admission_ownership_snapshot(
    token: dict[str, Any],
    current: dict[str, Any],
    *,
    delegation_id: str,
    run_id: str,
    require_started: bool,
) -> None:
    durable = current["admissions"].get(token["admission_id"])
    allowed_phases = {"started"} if require_started else {"authorized", "started"}
    commit = None if durable is None else durable.get("commit")
    if (
        durable is None
        or durable["phase"] not in allowed_phases
        or not durable["active"]
        or durable["run_kind"] != "specialist"
        or durable["delegation_id"] != delegation_id
        or durable["run_id"] != run_id
        or durable["reservation_event_sha256"] != token["reservation_event_sha256"]
        or durable["request_digest"] != token["request_digest"]
        or commit is None
        or commit["commit_id"] != token["commit_id"]
        or commit["run_id"] != run_id
        or run_id in current.get("cancelled_runs", {})
        or current["run_owners"].get(run_id)
        != {"owner_kind": "admission", "owner_id": token["admission_id"]}
    ):
        raise DelegationError("capability token admission ownership proof is invalid")


def validate_admission_token(
    runs_dir: Path,
    mission_id: str,
    *,
    token_id: str,
    delegation_id: str,
    run_id: str,
    require_started: bool,
) -> dict[str, Any]:
    """Require a v3 token and its exact live admission ownership proof."""

    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    token_id = mission_state.normalize_uuid(token_id, "token_id")
    delegation_id = mission_state.normalize_uuid(delegation_id, "delegation_id")
    run_id = mission_state.normalize_uuid(run_id, "run_id")
    with mission_state.MissionTransaction(runs_dir, mission_id) as transaction:
        events = list(transaction.events)
        current = transaction.current_state
        if current is None:
            raise DelegationError("capability token mission does not exist")
        token = _validate_bound_token_snapshot(
            runs_dir,
            mission_id,
            token_id=token_id,
            delegation_id=delegation_id,
            run_id=run_id,
            events=events,
            current=current,
            compiled=None,
            require_v3=True,
        )
        _require_admission_ownership_snapshot(
            token,
            current,
            delegation_id=delegation_id,
            run_id=run_id,
            require_started=require_started,
        )
        return token


def authorized_artifact_ids(
    runs_dir: Path,
    mission_id: str,
    *,
    token_id: str,
    delegation_id: str,
    run_id: str,
) -> list[str]:
    """Return exact token inputs plus attested results of this run's direct children."""
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    token_id = mission_state.normalize_uuid(token_id, "token_id")
    delegation_id = mission_state.normalize_uuid(delegation_id, "delegation_id")
    run_id = mission_state.normalize_uuid(run_id, "run_id")
    with mission_state.MissionTransaction(runs_dir, mission_id) as transaction:
        events = list(transaction.events)
        current = transaction.current_state
        if current is None:
            raise DelegationError("capability token mission does not exist")
        token = _validate_bound_token_snapshot(
            runs_dir,
            mission_id,
            token_id=token_id,
            delegation_id=delegation_id,
            run_id=run_id,
            events=events,
            current=current,
            compiled=None,
            require_v3=False,
        )
        if token["schema_version"] == 1:
            raise DelegationError(
                "schema_version=1 token cannot authorize artifact access"
            )
        if token["schema_version"] == 2:
            # Historical v2 grants remain readable so an already-running legacy
            # child can finish, but they can never accumulate fresh authority from
            # descendants after the admission-control migration.
            if current.get("admission_policy") is not None:
                raise DelegationError(
                    "legacy capability token is read-only after policy freeze"
                )
            return list(token["allowed_artifact_ids"])
        compiled = _compiled_snapshot(runs_dir, mission_id, current)
        _require_admission_ownership_snapshot(
            token,
            current,
            delegation_id=delegation_id,
            run_id=run_id,
            require_started=True,
        )
        child_results = _attested_direct_child_results(
            runs_dir,
            mission_id,
            events,
            current,
            compiled,
            parent_run_id=run_id,
            parent_admission_id=token["admission_id"],
        )
        return sorted(set(token["allowed_artifact_ids"]) | child_results)


def _validate_budget_event(
    event: dict[str, Any],
    *,
    token_id: str,
    delegation_id: str,
    parent_run_id: str,
) -> list[dict[str, Any]]:
    payload = event["payload"]
    if not isinstance(payload, dict) or set(payload) != BUDGET_PAYLOAD_FIELDS:
        raise DelegationError("delegation budget allocation payload is invalid")
    if (
        payload["token_id"] != token_id
        or payload["delegation_id"] != delegation_id
        or payload["parent_run_id"] != parent_run_id
    ):
        raise DelegationError("delegation budget allocation authority mismatch")
    allocations = payload["allocations"]
    if not isinstance(allocations, list) or not allocations:
        raise DelegationError("delegation budget allocations must be non-empty")
    canonical: list[dict[str, Any]] = []
    for allocation in allocations:
        if (
            not isinstance(allocation, dict)
            or set(allocation) != BUDGET_ALLOCATION_FIELDS
        ):
            raise DelegationError("delegation budget allocation fields are invalid")
        child_id = mission_state.normalize_uuid(
            str(allocation["requested_delegation_id"]), "requested_delegation_id"
        )
        if child_id != allocation["requested_delegation_id"]:
            raise DelegationError("requested_delegation_id is not canonical")
        if (
            not isinstance(allocation["capability"], str)
            or not allocation["capability"]
        ):
            raise DelegationError("delegation budget capability must be non-empty")
        if not isinstance(allocation["child_can_delegate"], bool):
            raise DelegationError("child_can_delegate must be boolean")
        capabilities = _canonical_capabilities(
            allocation["child_allowed_capabilities"],
            allow_empty=not allocation["child_can_delegate"],
        )
        if capabilities != allocation["child_allowed_capabilities"]:
            raise DelegationError("child_allowed_capabilities are not canonical")
        if not allocation["child_can_delegate"] and capabilities:
            raise DelegationError(
                "non-delegating child has a delegated capability scope"
            )
        artifacts = _canonical_artifact_ids(
            allocation["requested_artifact_ids"], "requested_artifact_ids"
        )
        if artifacts != allocation["requested_artifact_ids"]:
            raise DelegationError("requested_artifact_ids are not canonical")
        for field in ("edge_cost", "delegated_budget", "total_cost"):
            value = allocation[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DelegationError(f"{field} must be a non-negative integer")
        if allocation["edge_cost"] != 1:
            raise DelegationError("delegation budget allocation cost is invalid")
        if allocation["child_can_delegate"] and allocation["delegated_budget"] < 1:
            raise DelegationError("delegating child budget must be at least 1")
        if not allocation["child_can_delegate"] and allocation["delegated_budget"] != 0:
            raise DelegationError("non-delegating child budget must be 0")
        if allocation["total_cost"] != 1 + allocation["delegated_budget"]:
            raise DelegationError("delegation budget allocation total is invalid")
        canonical.append(allocation)
    if canonical != sorted(canonical, key=lambda item: item["requested_delegation_id"]):
        raise DelegationError("delegation budget allocations are not canonical")
    if len({item["requested_delegation_id"] for item in canonical}) != len(canonical):
        raise DelegationError("delegation budget allocations contain duplicates")
    return canonical


def _reserve_budget_allocations(
    runs_dir: Path,
    mission_id: str,
    *,
    token: dict[str, Any],
    parent_run_id: str,
    requested: list[dict[str, Any]],
) -> None:
    token_id = token["token_id"]
    delegation_id = token["delegation_id"]
    try:
        with mission_state.MissionTransaction(runs_dir, mission_id) as transaction:
            events = list(transaction.events)
            current = transaction.current_state
            if current is None:
                raise DelegationError("capability token mission does not exist")
            if current["admission_policy"] is not None:
                raise DelegationError(
                    "legacy delegation budget allocation is disabled after policy freeze"
                )
            snapshot_token = _validate_bound_token_snapshot(
                runs_dir,
                mission_id,
                token_id=token_id,
                delegation_id=delegation_id,
                run_id=parent_run_id,
                events=events,
                current=current,
                compiled=None,
                require_v3=False,
            )
            if snapshot_token != token:
                raise DelegationError(
                    "delegation budget token changed before allocation"
                )
            with fleet_safe_paths.RootedFS(runs_dir) as rooted:
                with rooted.exclusive_lock(
                    _token_relative(mission_id, f".{token_id}.budget.lock"),
                    directory_modes=(0o700, 0o700, 0o700),
                    file_mode=0o600,
                ):
                    existing: dict[str, dict[str, Any]] = {}
                    for event in events:
                        if event["kind"] != BUDGET_EVENT_KIND:
                            continue
                        payload = event["payload"]
                        if (
                            not isinstance(payload, dict)
                            or payload.get("token_id") != token_id
                        ):
                            continue
                        for allocation in _validate_budget_event(
                            event,
                            token_id=token_id,
                            delegation_id=delegation_id,
                            parent_run_id=parent_run_id,
                        ):
                            child_id = allocation["requested_delegation_id"]
                            if child_id in existing:
                                raise DelegationError(
                                    "delegation budget was allocated more than once"
                                )
                            existing[child_id] = allocation
                    new: list[dict[str, Any]] = []
                    for allocation in requested:
                        child_id = allocation["requested_delegation_id"]
                        if child_id in existing:
                            if existing[child_id] != allocation:
                                raise DelegationError(
                                    "delegation budget allocation conflicts with prior request"
                                )
                        else:
                            new.append(allocation)
                    used = sum(item["total_cost"] for item in existing.values())
                    required = sum(item["total_cost"] for item in new)
                    if used + required > token["remaining_budget"]:
                        raise DelegationError(
                            "requested allocations exceed delegated budget"
                        )
                    if not new:
                        return
                    new.sort(key=lambda item: item["requested_delegation_id"])
                    digest = mission_state.sha256(new)
                    transaction.append_event(
                        kind=BUDGET_EVENT_KIND,
                        actor="CONTROL",
                        idempotency_key=f"budget:{token_id}:{digest}",
                        payload={
                            "token_id": token_id,
                            "delegation_id": delegation_id,
                            "parent_run_id": parent_run_id,
                            "allocations": new,
                        },
                    )
                    rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise DelegationError(f"unsafe capability token store: {exc}") from exc


def _normalized_subdelegation_request(
    request: Any,
    *,
    token: dict[str, Any],
    authorized_artifacts: set[str],
) -> dict[str, Any]:
    required = {
        "requested_delegation_id",
        "capability",
        "requested_budget",
        "child_can_delegate",
        "child_allowed_capabilities",
        "requested_artifact_ids",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise DelegationError("subdelegation request fields do not match schema")
    child_id = mission_state.normalize_uuid(
        str(request["requested_delegation_id"]), "requested_delegation_id"
    )
    if child_id != request["requested_delegation_id"]:
        raise DelegationError("requested_delegation_id is not canonical")
    capability = request["capability"]
    if (
        not isinstance(capability, str)
        or capability not in token["allowed_capabilities"]
    ):
        raise DelegationError("capability is outside delegated scope")
    child_can_delegate = request["child_can_delegate"]
    if not isinstance(child_can_delegate, bool):
        raise DelegationError("child_can_delegate must be boolean")
    requested_budget = request["requested_budget"]
    if isinstance(requested_budget, bool) or not isinstance(requested_budget, int):
        raise DelegationError("requested budget must be an integer")
    if child_can_delegate and requested_budget < 1:
        raise DelegationError("delegating child budget must be at least 1")
    if not child_can_delegate and requested_budget != 0:
        raise DelegationError("non-delegating child budget must be 0")
    raw_child_capabilities = request["child_allowed_capabilities"]
    child_capabilities = _canonical_capabilities(
        raw_child_capabilities, allow_empty=not child_can_delegate
    )
    if child_capabilities != raw_child_capabilities:
        raise DelegationError("child_allowed_capabilities are not canonical")
    if not set(child_capabilities) <= set(token["allowed_capabilities"]):
        raise DelegationError("child capability scope exceeds parent token")
    if not child_can_delegate and child_capabilities:
        raise DelegationError(
            "non-delegating child cannot receive child capability scope"
        )
    if child_can_delegate and token["current_depth"] + 1 >= token["max_depth"]:
        raise DelegationError("delegation depth leaves no room for child subdelegation")
    raw_artifacts = request["requested_artifact_ids"]
    artifacts = _canonical_artifact_ids(raw_artifacts, "requested_artifact_ids")
    if artifacts != raw_artifacts:
        raise DelegationError("requested_artifact_ids are not canonical")
    if not set(artifacts) <= authorized_artifacts:
        raise DelegationError("requested artifacts are outside delegated scope")
    delegated_budget = requested_budget if child_can_delegate else 0
    return {
        "requested_delegation_id": child_id,
        "capability": capability,
        "child_can_delegate": child_can_delegate,
        "child_allowed_capabilities": child_capabilities,
        "requested_artifact_ids": artifacts,
        "edge_cost": 1,
        "delegated_budget": delegated_budget,
        "total_cost": 1 + delegated_budget,
    }


def validate_for_subdelegations(
    runs_dir: Path,
    mission_id: str,
    *,
    token_id: str,
    delegation_id: str,
    parent_run_id: str,
    requests: list[dict[str, Any]],
    reserve_budget: bool = True,
) -> dict[str, Any]:
    """Validate and durably reserve one atomic batch of child delegations."""
    token = validate_admission_token(
        runs_dir,
        mission_id,
        token_id=token_id,
        delegation_id=delegation_id,
        run_id=parent_run_id,
        require_started=True,
    )
    if not token["can_delegate"]:
        raise DelegationError("capability token does not allow subdelegation")
    if token["current_depth"] >= token["max_depth"]:
        raise DelegationError("maximum delegation depth reached")
    if not isinstance(requests, list) or not requests:
        raise DelegationError("subdelegation batch must be a non-empty list")
    authorized = set(
        authorized_artifact_ids(
            runs_dir,
            mission_id,
            token_id=token_id,
            delegation_id=delegation_id,
            run_id=parent_run_id,
        )
    )
    normalized = [
        _normalized_subdelegation_request(
            request, token=token, authorized_artifacts=authorized
        )
        for request in requests
    ]
    child_ids = [item["requested_delegation_id"] for item in normalized]
    if len(child_ids) != len(set(child_ids)):
        raise DelegationError("subdelegation batch contains duplicate delegation ids")
    normalized.sort(key=lambda item: item["requested_delegation_id"])
    if not isinstance(reserve_budget, bool):
        raise DelegationError("reserve_budget must be boolean")
    # Admission reservations debit the parent's immutable delegated budget.
    # The legacy delegation_budget_allocated stream remains readable for old
    # ledgers but may never create new authority for a v3 mission.
    if (
        reserve_budget
        and mission_state.derive_state(_events(runs_dir, mission_id))[
            "admission_policy"
        ]
        is None
    ):
        _reserve_budget_allocations(
            runs_dir,
            mission_id,
            token=token,
            parent_run_id=parent_run_id,
            requested=normalized,
        )
    return token


def validate_for_subdelegation(
    runs_dir: Path,
    mission_id: str,
    *,
    token_id: str,
    delegation_id: str,
    parent_run_id: str,
    capability: str,
    requested_budget: int,
    requested_delegation_id: str,
    child_can_delegate: bool = False,
    child_allowed_capabilities: list[str] | None = None,
    requested_artifact_ids: list[str] | None = None,
    reserve_budget: bool = True,
) -> dict[str, Any]:
    return validate_for_subdelegations(
        runs_dir,
        mission_id,
        token_id=token_id,
        delegation_id=delegation_id,
        parent_run_id=parent_run_id,
        requests=[
            {
                "requested_delegation_id": requested_delegation_id,
                "capability": capability,
                "requested_budget": requested_budget,
                "child_can_delegate": child_can_delegate,
                "child_allowed_capabilities": list(child_allowed_capabilities or []),
                "requested_artifact_ids": list(requested_artifact_ids or []),
            }
        ],
        reserve_budget=reserve_budget,
    )
