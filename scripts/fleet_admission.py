#!/usr/bin/env python3
"""Crash-safe admission control for mission Lead and specialist runs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
import uuid

import fleet_mission_state as mission_state


REQUEST_REQUIRED_FIELDS = {
    "request_key",
    "run_kind",
    "recipient_instance",
    "capability",
    "effect_sha256",
    "task_sha256",
    "delegated_budget",
    "writer",
}
REQUEST_OPTIONAL_FIELDS = {"parent_admission_id", "parent_run_id"}


class AdmissionError(mission_state.MissionStateError):
    """An admission request is malformed or cannot be proven safe."""


def _require_sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or not mission_state.SHA256.fullmatch(value):
        raise AdmissionError(f"{where} must be SHA-256")
    return value


def _require_key(value: Any, where: str = "idempotency_key") -> str:
    if not isinstance(value, str) or not mission_state.SAFE_KEY.fullmatch(value):
        raise AdmissionError(f"invalid {where}")
    return value


def _require_actor(value: Any) -> str:
    if not isinstance(value, str) or not mission_state.SAFE_ACTOR.fullmatch(value):
        raise AdmissionError("invalid admission actor")
    return value


def _require_component(value: Any, where: str) -> str:
    if not isinstance(value, str) or not mission_state.SAFE_FEATURE.fullmatch(value):
        raise AdmissionError(f"invalid {where}")
    return value


def _require_uint(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdmissionError(f"{where} must be a non-negative integer")
    return value


def _canonical_copy(value: Any) -> Any:
    return mission_state.loads_strict(mission_state.canonical_bytes(value))


def _normalize_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise AdmissionError("admission request must be an object")
    request = _canonical_copy(request)
    fields = set(request)
    if not REQUEST_REQUIRED_FIELDS <= fields or not fields <= (
        REQUEST_REQUIRED_FIELDS | REQUEST_OPTIONAL_FIELDS
    ):
        raise AdmissionError("admission request fields do not match schema")
    request_key = _require_key(request["request_key"], "admission request_key")
    run_kind = request["run_kind"]
    if run_kind not in {"lead", "specialist"}:
        raise AdmissionError("admission run_kind must be lead or specialist")
    recipient = _require_component(request["recipient_instance"], "recipient_instance")
    capability = _require_component(request["capability"], "capability")
    effect_sha256 = _require_sha(request["effect_sha256"], "effect_sha256")
    task_sha256 = _require_sha(request["task_sha256"], "task_sha256")
    delegated_budget = _require_uint(request["delegated_budget"], "delegated_budget")
    writer = request["writer"]
    if not isinstance(writer, bool):
        raise AdmissionError("admission writer must be boolean")
    parent_admission_id = request.get("parent_admission_id")
    parent_run_id = request.get("parent_run_id")
    if parent_admission_id is not None:
        parent_admission_id = mission_state.normalize_uuid(
            str(parent_admission_id), "parent_admission_id"
        )
    if parent_run_id is not None:
        parent_run_id = mission_state.normalize_uuid(
            str(parent_run_id), "parent_run_id"
        )
    if run_kind == "lead" and (
        parent_admission_id is not None
        or parent_run_id is not None
        or delegated_budget != 0
    ):
        raise AdmissionError("Lead admission must have zero budget and no parent")
    return {
        "request_key": request_key,
        "run_kind": run_kind,
        "recipient_instance": recipient,
        "capability": capability,
        "effect_sha256": effect_sha256,
        "task_sha256": task_sha256,
        "parent_admission_id": parent_admission_id,
        "parent_run_id": parent_run_id,
        "delegated_budget": delegated_budget,
        "writer": writer,
    }


def deterministic_ids(
    mission_id: str, *, request_key: str, run_kind: str
) -> dict[str, str | None]:
    """Return stable run/delegation/admission identities for one logical request."""

    normalized_mission = mission_state.normalize_uuid(mission_id, "mission_id")
    request_key = _require_key(request_key, "admission request_key")
    if run_kind not in {"lead", "specialist"}:
        raise AdmissionError("admission run_kind must be lead or specialist")
    namespace = uuid.UUID(normalized_mission)
    return {
        "admission_id": str(
            uuid.uuid5(namespace, f"admission:{run_kind}:{request_key}")
        ),
        "run_id": str(uuid.uuid5(namespace, f"run:{run_kind}:{request_key}")),
        "delegation_id": (
            None
            if run_kind == "lead"
            else str(uuid.uuid5(namespace, f"delegation:{request_key}"))
        ),
    }


def freeze_policy(
    runs_dir: Path,
    mission_id: str,
    *,
    workflow_digest: str,
    compiled_digest: str,
    deadline_at: str,
    delegation_credits: int,
    max_active_delegations: int,
    idempotency_key: str = "admission:policy",
    actor: str = "CONTROL",
) -> dict[str, Any]:
    """Freeze the immutable mission-wide admission envelope."""

    _require_sha(workflow_digest, "workflow_digest")
    _require_sha(compiled_digest, "compiled_digest")
    mission_state.parse_timestamp(deadline_at, "admission deadline_at")
    _require_uint(delegation_credits, "delegation_credits")
    if _require_uint(max_active_delegations, "max_active_delegations") < 1:
        raise AdmissionError("max_active_delegations must be positive")
    _require_key(idempotency_key)
    _require_actor(actor)
    event, appended = mission_state.append_event(
        runs_dir,
        mission_id,
        kind="mission_admission_policy_frozen",
        actor=actor,
        idempotency_key=idempotency_key,
        payload={
            "workflow_digest": workflow_digest,
            "compiled_digest": compiled_digest,
            "deadline_at": deadline_at,
            "delegation_credits": delegation_credits,
            "max_active_delegations": max_active_delegations,
        },
    )
    return {"event": event, "appended": appended, "policy": event["payload"]}


def _build_admissions(
    mission_id: str,
    requests: list[dict[str, Any]],
    current: dict[str, Any],
    actor: str,
) -> list[dict[str, Any]]:
    bases: list[dict[str, Any]] = []
    prospective: dict[str, dict[str, Any]] = {}
    seen_keys: set[str] = set()
    for request in requests:
        if request["request_key"] in seen_keys:
            raise AdmissionError("admission request_keys must be unique within a batch")
        seen_keys.add(request["request_key"])
        identities = deterministic_ids(
            mission_id,
            request_key=request["request_key"],
            run_kind=request["run_kind"],
        )
        base = {**request, **identities}
        bases.append(base)
        prospective[str(identities["admission_id"])] = base

    admissions: list[dict[str, Any]] = []
    for base in bases:
        parent_id = base["parent_admission_id"]
        if (
            actor == "CONTROL"
            and base["run_kind"] == "specialist"
            and parent_id is None
        ):
            prospective_leads = [
                item for item in prospective.values() if item["run_kind"] == "lead"
            ]
            durable_lead_id = current.get("lead_admission_id")
            if durable_lead_id is not None:
                parent_id = str(durable_lead_id)
                base["parent_admission_id"] = parent_id
            elif len(prospective_leads) == 1:
                parent_id = str(prospective_leads[0]["admission_id"])
                base["parent_admission_id"] = parent_id
            elif (
                current.get("lead_run_id") is not None and base["parent_run_id"] is None
            ):
                base["parent_run_id"] = current["lead_run_id"]
        parent = prospective.get(parent_id) or current.get("admissions", {}).get(
            parent_id
        )
        if parent is not None and base["parent_run_id"] is None:
            base["parent_run_id"] = parent["run_id"]
        if base["run_kind"] == "lead":
            credit_cost = 0
            global_debit = 0
            parent_debit = 0
        else:
            credit_cost = 1 + base["delegated_budget"]
            if parent is not None and parent["run_kind"] == "specialist":
                global_debit = 0
                parent_debit = credit_cost
            else:
                global_debit = credit_cost
                parent_debit = 0
        binding = {
            field: base[field]
            for field in (
                "request_key",
                "run_kind",
                "recipient_instance",
                "capability",
                "effect_sha256",
                "task_sha256",
                "parent_admission_id",
                "parent_run_id",
                "delegated_budget",
                "writer",
            )
        }
        admissions.append(
            {
                "admission_id": base["admission_id"],
                "delegation_id": base["delegation_id"],
                "run_id": base["run_id"],
                **binding,
                "request_digest": mission_state.sha256(binding),
                "credit_cost": credit_cost,
                "global_credit_debit": global_debit,
                "parent_credit_debit": parent_debit,
            }
        )
    return sorted(admissions, key=lambda item: item["admission_id"])


def _request_batch_sha256(requests: list[dict[str, Any]]) -> str:
    """Bind an idempotency key to the caller's pre-resolution request."""

    ordered = sorted(
        requests,
        key=lambda item: (item["run_kind"], item["request_key"]),
    )
    return mission_state.sha256(ordered)


def reserve_many(
    runs_dir: Path,
    mission_id: str,
    *,
    requests: Sequence[dict[str, Any]],
    idempotency_key: str,
    actor: str = "CONTROL",
) -> dict[str, Any]:
    """Atomically reserve a deterministic batch or append nothing."""

    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    _require_key(idempotency_key)
    _require_actor(actor)
    if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes)):
        raise AdmissionError("admission requests must be a sequence")
    normalized = [_normalize_request(request) for request in requests]
    if not normalized:
        raise AdmissionError("admission reservation batch must be non-empty")
    batch_id = str(
        uuid.uuid5(uuid.UUID(mission_id), f"admission-batch:{idempotency_key}")
    )
    request_sha256 = _request_batch_sha256(normalized)
    with mission_state.MissionTransaction(runs_dir, mission_id) as transaction:
        current = transaction.current_state
        if current is None:
            raise AdmissionError("mission does not exist")
        prior = next(
            (
                event
                for event in transaction.events
                if event["idempotency_key"] == idempotency_key
            ),
            None,
        )
        if prior is not None:
            if (
                prior["kind"] != "delegations_reserved"
                or prior["actor"] != actor
                or prior["payload"].get("batch_id") != batch_id
                or prior["payload"].get("request_sha256") != request_sha256
            ):
                raise mission_state.MissionConflict(
                    "idempotency key was already used for another admission request"
                )
            event = prior
            appended = False
            derived = current
            durable_admissions = [
                derived["admissions"][item["admission_id"]]
                for item in event["payload"]["admissions"]
            ]
            return {
                "batch_id": batch_id,
                "batch_sha256": event["payload"]["batch_sha256"],
                "admissions": _canonical_copy(durable_admissions),
                "event": event,
                "appended": appended,
            }
        admissions = _build_admissions(mission_id, normalized, current, actor)
        payload = {
            "batch_id": batch_id,
            "batch_sha256": mission_state.sha256(admissions),
            "request_sha256": request_sha256,
            "admissions": admissions,
        }
        event, appended = transaction.append_event(
            kind="delegations_reserved",
            actor=actor,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        derived = transaction.current_state
        if derived is None:
            raise AdmissionError("mission disappeared after admission reservation")
        durable_admissions = [
            derived["admissions"][item["admission_id"]]
            for item in event["payload"]["admissions"]
        ]
    return {
        "batch_id": batch_id,
        "batch_sha256": payload["batch_sha256"],
        "admissions": _canonical_copy(durable_admissions),
        "event": event,
        "appended": appended,
    }


def commit(
    runs_dir: Path,
    mission_id: str,
    *,
    admission_id: str,
    request_digest: str,
    effect_sha256: str,
    recipient_instance: str,
    writer: bool,
    run_id: str,
    idempotency_key: str,
    actor: str = "CONTROL",
) -> dict[str, Any]:
    """Bind one reservation to the exact process request before effects."""

    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    admission_id = mission_state.normalize_uuid(admission_id, "admission_id")
    run_id = mission_state.normalize_uuid(run_id, "run_id")
    _require_sha(request_digest, "request_digest")
    _require_sha(effect_sha256, "effect_sha256")
    _require_component(recipient_instance, "recipient_instance")
    if not isinstance(writer, bool):
        raise AdmissionError("writer must be boolean")
    _require_key(idempotency_key)
    _require_actor(actor)
    commit_id = str(uuid.uuid5(uuid.UUID(mission_id), f"commit:{admission_id}"))
    with mission_state.MissionTransaction(runs_dir, mission_id) as transaction:
        current = transaction.current_state
        admission = None if current is None else current["admissions"].get(admission_id)
        if admission is None:
            raise AdmissionError("unknown admission_id")
        payload = {
            "admission_id": admission_id,
            "commit_id": commit_id,
            "reservation_event_sha256": admission["reservation_event_sha256"],
            "request_digest": request_digest,
            "effect_sha256": effect_sha256,
            "recipient_instance": recipient_instance,
            "writer": writer,
            "run_id": run_id,
        }
        event, appended = transaction.append_event(
            kind="delegation_committed",
            actor=actor,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        head_sha256 = transaction.head_sha256
    return {
        "admission_id": admission_id,
        "commit_id": commit_id,
        "event": event,
        "commit_event_sha256": event["event_sha256"],
        "head_sha256": head_sha256,
        "appended": appended,
    }


def verify_commit(
    runs_dir: Path,
    mission_id: str,
    *,
    admission_id: str,
    commit_event_sha256: str,
    expected_head_sha256: str,
    request_digest: str,
    effect_sha256: str,
    recipient_instance: str,
    writer: bool,
    run_id: str,
) -> dict[str, Any]:
    """Prove exact commit bindings at the exact current ledger head."""

    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    admission_id = mission_state.normalize_uuid(admission_id, "admission_id")
    run_id = mission_state.normalize_uuid(run_id, "run_id")
    for value, where in (
        (commit_event_sha256, "commit_event_sha256"),
        (expected_head_sha256, "expected_head_sha256"),
        (request_digest, "request_digest"),
        (effect_sha256, "effect_sha256"),
    ):
        _require_sha(value, where)
    _require_component(recipient_instance, "recipient_instance")
    if not isinstance(writer, bool):
        raise AdmissionError("writer must be boolean")
    with mission_state.MissionTransaction(runs_dir, mission_id) as transaction:
        if transaction.head_sha256 != expected_head_sha256:
            raise mission_state.MissionConflict(
                "mission head changed after delegation commit"
            )
        current = transaction.current_state
        admission = None if current is None else current["admissions"].get(admission_id)
        if (
            admission is None
            or admission["phase"] != "committed"
            or admission["commit"] is None
        ):
            raise mission_state.MissionConflict("delegation admission is not committed")
        commit_payload = admission["commit"]
        expected = {
            "request_digest": request_digest,
            "effect_sha256": effect_sha256,
            "recipient_instance": recipient_instance,
            "writer": writer,
            "run_id": run_id,
        }
        if commit_payload["event_sha256"] != commit_event_sha256 or any(
            commit_payload[field] != value for field, value in expected.items()
        ):
            raise mission_state.MissionConflict(
                "delegation commit proof does not match bindings"
            )
        policy = current["admission_policy"]
        if policy is None or datetime.now(
            timezone.utc
        ) >= mission_state.parse_timestamp(policy["deadline_at"], "admission deadline"):
            raise mission_state.MissionConflict("mission admission deadline has passed")
        proof = {
            "mission_id": mission_id,
            "admission_id": admission_id,
            "commit_id": commit_payload["commit_id"],
            "commit_event_sha256": commit_event_sha256,
            "head_sha256": expected_head_sha256,
            **expected,
        }
    return _canonical_copy(proof)


def authorize_launch(
    runs_dir: Path,
    mission_id: str,
    *,
    admission_id: str,
    commit_event_sha256: str,
    request_digest: str,
    effect_sha256: str,
    recipient_instance: str,
    writer: bool,
    run_id: str,
    approval_event_sha256: str | None = None,
    idempotency_key: str,
    actor: str = "CONTROL",
) -> dict[str, Any]:
    """Durably linearize one external launch at the live ledger head."""

    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    admission_id = mission_state.normalize_uuid(admission_id, "admission_id")
    run_id = mission_state.normalize_uuid(run_id, "run_id")
    _require_sha(commit_event_sha256, "commit_event_sha256")
    _require_sha(request_digest, "request_digest")
    _require_sha(effect_sha256, "effect_sha256")
    if approval_event_sha256 is not None:
        _require_sha(approval_event_sha256, "approval_event_sha256")
    _require_component(recipient_instance, "recipient_instance")
    if not isinstance(writer, bool):
        raise AdmissionError("writer must be boolean")
    _require_key(idempotency_key)
    _require_actor(actor)
    payload = {
        "admission_id": admission_id,
        "commit_event_sha256": commit_event_sha256,
        "request_digest": request_digest,
        "effect_sha256": effect_sha256,
        "recipient_instance": recipient_instance,
        "writer": writer,
        "run_id": run_id,
        "approval_event_sha256": approval_event_sha256,
    }
    with mission_state.MissionTransaction(runs_dir, mission_id) as transaction:
        event, appended = transaction.append_event(
            kind="delegation_launch_authorized",
            actor=actor,
            idempotency_key=idempotency_key,
            payload=payload,
        )
    return {
        "event": event,
        "appended": appended,
        "admission_id": admission_id,
        "authorization_event_sha256": event["event_sha256"],
    }


def mark_started(
    runs_dir: Path,
    mission_id: str,
    *,
    admission_id: str,
    authorization_event_sha256: str,
    request_digest: str,
    effect_sha256: str,
    recipient_instance: str,
    writer: bool,
    run_id: str,
    idempotency_key: str,
    actor: str = "CONTROL",
) -> dict[str, Any]:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    admission_id = mission_state.normalize_uuid(admission_id, "admission_id")
    run_id = mission_state.normalize_uuid(run_id, "run_id")
    _require_sha(authorization_event_sha256, "authorization_event_sha256")
    _require_sha(request_digest, "request_digest")
    _require_sha(effect_sha256, "effect_sha256")
    _require_component(recipient_instance, "recipient_instance")
    if not isinstance(writer, bool):
        raise AdmissionError("writer must be boolean")
    _require_key(idempotency_key)
    _require_actor(actor)
    payload = {
        "admission_id": admission_id,
        "authorization_event_sha256": authorization_event_sha256,
        "request_digest": request_digest,
        "effect_sha256": effect_sha256,
        "recipient_instance": recipient_instance,
        "writer": writer,
        "run_id": run_id,
    }
    with mission_state.MissionTransaction(runs_dir, mission_id) as transaction:
        prior = next(
            (
                event
                for event in transaction.events
                if event["idempotency_key"] == idempotency_key
            ),
            None,
        )
        if prior is None:
            current = transaction.current_state
            durable = (
                None if current is None else current["admissions"].get(admission_id)
            )
            if durable is None or durable["phase"] != "authorized":
                raise mission_state.MissionConflict(
                    "only an authorized admission can be started"
                )
            authorization = durable.get("launch_authorization")
            expected = {
                "request_digest": request_digest,
                "effect_sha256": effect_sha256,
                "recipient_instance": recipient_instance,
                "writer": writer,
                "run_id": run_id,
            }
            if (
                authorization is None
                or authorization["event_sha256"] != authorization_event_sha256
                or any(
                    authorization[field] != value for field, value in expected.items()
                )
            ):
                raise mission_state.MissionConflict(
                    "delegation start proof does not match launch authorization"
                )
        event, appended = transaction.append_event(
            kind="delegation_started",
            actor=actor,
            idempotency_key=idempotency_key,
            payload=payload,
        )
    return {"event": event, "appended": appended, "admission_id": admission_id}


def finalize(
    runs_dir: Path,
    mission_id: str,
    *,
    admission_id: str,
    recipient_instance: str,
    writer: bool,
    terminal_evidence: dict[str, Any],
    reason: str,
    idempotency_key: str,
    actor: str = "CONTROL",
) -> dict[str, Any]:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    admission_id = mission_state.normalize_uuid(admission_id, "admission_id")
    _require_component(recipient_instance, "recipient_instance")
    if not isinstance(writer, bool):
        raise AdmissionError("writer must be boolean")
    if not isinstance(terminal_evidence, dict):
        raise AdmissionError("terminal_evidence must be an object")
    terminal_evidence = _canonical_copy(terminal_evidence)
    if set(terminal_evidence) != {
        "schema_version",
        "source_event_sha256",
        "run_id",
        "task_sha256",
        "status",
    }:
        raise AdmissionError("terminal_evidence fields do not match schema")
    if type(terminal_evidence["schema_version"]) is not int or (
        terminal_evidence["schema_version"] != 1
    ):
        raise AdmissionError("terminal_evidence schema_version must be 1")
    _require_sha(
        terminal_evidence["source_event_sha256"],
        "terminal_evidence source_event_sha256",
    )
    run_id = mission_state.normalize_uuid(
        terminal_evidence["run_id"], "terminal_evidence run_id"
    )
    if terminal_evidence["run_id"] != run_id:
        raise AdmissionError("terminal_evidence run_id must be canonical")
    _require_sha(terminal_evidence["task_sha256"], "terminal_evidence task_sha256")
    status = terminal_evidence["status"]
    if status not in mission_state.TERMINAL_STATUSES:
        raise AdmissionError("invalid delegation terminal status")
    if not isinstance(reason, str) or not reason:
        raise AdmissionError("delegation final reason must be non-empty")
    _require_key(idempotency_key)
    _require_actor(actor)
    terminal_evidence_sha256 = mission_state.sha256(
        {
            "mission_id": mission_id,
            "admission_id": admission_id,
            "recipient_instance": recipient_instance,
            "writer": writer,
            "terminal_evidence": terminal_evidence,
        }
    )
    event, appended = mission_state.append_event(
        runs_dir,
        mission_id,
        kind="delegation_finalized",
        actor=actor,
        idempotency_key=idempotency_key,
        payload={
            "admission_id": admission_id,
            "run_id": run_id,
            "recipient_instance": recipient_instance,
            "writer": writer,
            "terminal_evidence": terminal_evidence,
            "terminal_evidence_sha256": terminal_evidence_sha256,
            "status": status,
            "reason": reason,
        },
    )
    return {"event": event, "appended": appended, "admission_id": admission_id}


def abort_prelaunch(
    runs_dir: Path,
    mission_id: str,
    *,
    admission_id: str,
    run_id: str,
    request_digest: str,
    effect_sha256: str,
    task_sha256: str,
    recipient_instance: str,
    writer: bool,
    reason: str,
    idempotency_key: str,
    actor: str = "CONTROL",
) -> dict[str, Any]:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    admission_id = mission_state.normalize_uuid(admission_id, "admission_id")
    run_id = mission_state.normalize_uuid(run_id, "run_id")
    for value, where in (
        (request_digest, "request_digest"),
        (effect_sha256, "effect_sha256"),
        (task_sha256, "task_sha256"),
    ):
        _require_sha(value, where)
    _require_component(recipient_instance, "recipient_instance")
    if not isinstance(writer, bool):
        raise AdmissionError("writer must be boolean")
    if not isinstance(reason, str) or not reason:
        raise AdmissionError("delegation abort reason must be non-empty")
    _require_key(idempotency_key)
    _require_actor(actor)
    event, appended = mission_state.append_event(
        runs_dir,
        mission_id,
        kind="delegation_reservation_aborted",
        actor=actor,
        idempotency_key=idempotency_key,
        payload={
            "admission_id": admission_id,
            "run_id": run_id,
            "request_digest": request_digest,
            "effect_sha256": effect_sha256,
            "task_sha256": task_sha256,
            "recipient_instance": recipient_instance,
            "writer": writer,
            "reason": reason,
        },
    )
    return {"event": event, "appended": appended, "admission_id": admission_id}
