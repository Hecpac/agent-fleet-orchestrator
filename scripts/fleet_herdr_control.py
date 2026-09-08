"""Durable single-Mission control requests, independent of the driver lock."""
from __future__ import annotations

from pathlib import Path
import uuid

import fleet_json
import fleet_mission
import fleet_mission_state as state
import fleet_safe_paths

KINDS = {"herdr_supervision_enabled", "herdr_control_requested", "herdr_control_applied", "herdr_dispatch_intent"}


def validate_payload(kind, payload):
    fields = {"herdr_supervision_enabled": {"version"},
        "herdr_control_requested": {"request_id", "action", "reason", "run_id", "generation"},
        "herdr_control_applied": {"request_id", "action"},
        "herdr_dispatch_intent": {"run_id", "task_sha256", "generation"}}[kind]
    if kind == "herdr_control_applied" and "functional_cleanup_artifact_id" in payload:
        fields = fields | {"functional_cleanup_artifact_id"}
        state._require_sha(payload["functional_cleanup_artifact_id"], "functional cleanup proof")
    state._require_fields(kind, payload, fields)
    if kind == "herdr_supervision_enabled":
        if type(payload["version"]) is not int or payload["version"] != 1:
            raise state.MissionStateError("unsupported Herdr supervision version")
        return
    if "request_id" in payload:
        state._require_uuid(payload["request_id"], "control request")
        if payload["action"] not in {"pause", "resume", "cancel"}:
            raise state.MissionStateError("invalid Herdr control action")
    if "reason" in payload:
        state._require_nonempty(payload["reason"], "control reason")
        if len(payload["reason"]) > 1024:
            raise state.MissionStateError("control reason exceeds limit")
    for key in ("run_id", "generation"):
        if payload.get(key) is not None:
            state._require_uuid(payload[key], key)
    if "task_sha256" in payload:
        state._require_sha(payload["task_sha256"], "dispatch task")
        if payload["run_id"] is None or payload["generation"] is None:
            raise state.MissionStateError("dispatch requires exact run and generation")
    if kind == "herdr_control_requested" and payload["action"] != "cancel" and (payload["run_id"] or payload["generation"]):
        raise state.MissionStateError("pause/resume target the Mission")


def view(current):
    return current.get("herdr_control", {"version": 1, "desired": "running", "applied": "running",
        "latest": None, "requests": {}, "dispatches": {}, "enabled_sequence": None})


def reduce(current, event):
    if event["actor"] != "CONTROL":
        raise state.MissionConflict("Herdr supervision requires CONTROL")
    kind, payload = event["kind"], event["payload"]
    if kind == "herdr_supervision_enabled":
        if "herdr_control" in current:
            raise state.MissionConflict("Herdr supervision policy is immutable")
        current["herdr_control"] = {**view(current), "enabled_sequence": event["sequence"]}
        return
    if "herdr_control" not in current:
        raise state.MissionConflict("Herdr supervision policy is missing")
    control = current["herdr_control"]
    if kind == "herdr_control_requested":
        if control["desired"] == "cancel_requested":
            raise state.MissionConflict("an outstanding cancellation cannot be superseded")
        if payload["run_id"] and not any(a["run_id"] == payload["run_id"] and a["active"] for a in current["admissions"].values()):
            raise state.MissionConflict("control cancellation does not own an active run")
        control["requests"][payload["request_id"]] = {**payload, "requested_at": event["timestamp"],
            "event_sha256": event["event_sha256"], "applied_at": None}
        control["latest"] = payload["request_id"]
        control["desired"] = {"pause": "pause_requested", "resume": "running", "cancel": "cancel_requested"}[payload["action"]]
    elif kind == "herdr_control_applied":
        request = control["requests"].get(payload["request_id"])
        if not request or request["action"] != payload["action"] or request["applied_at"] or control["latest"] != payload["request_id"]:
            raise state.MissionConflict("control acknowledgement differs from current request")
        if payload["action"] in {"pause", "cancel"}:
            active = [a for a in current["admissions"].values() if a["active"]]
            if payload["action"] == "cancel" and active:
                raise state.MissionConflict("cancel confirmation requires quiescent admissions")
            if payload["action"] == "pause" and any(a["phase"] == "started" or a["run_id"] in control["dispatches"] or
                    (a["phase"] == "authorized" and not fresh_authorization(current, a)) for a in active):
                raise state.MissionConflict("pause confirmation requires active turn reconciliation")
            attempt = current.get("functional_attempt")
            if attempt and not attempt.get("result"):
                raise state.MissionConflict("control confirmation requires functional attempt reconciliation")
        request["applied_at"] = event["timestamp"]
        if payload.get("functional_cleanup_artifact_id"):
            request["functional_cleanup_artifact_id"] = payload["functional_cleanup_artifact_id"]
        control["applied"] = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[payload["action"]]
    elif kind == "herdr_dispatch_intent":
        if control["desired"] != "running":
            raise state.MissionConflict("new dispatch blocked by durable control request")
        admission = next((a for a in current["admissions"].values() if a["run_id"] == payload["run_id"]), None)
        if (not admission or admission["phase"] != "authorized" or admission["task_sha256"] != payload["task_sha256"]
                or payload["run_id"] in control["dispatches"] or not fresh_authorization(current, admission)
                or payload["run_id"] in current["cancelled_runs"]):
            raise state.MissionConflict("dispatch lacks a fresh exact authorization")
        control["dispatches"][payload["run_id"]] = {**payload, "event_sha256": event["event_sha256"], "timestamp": event["timestamp"]}


def fresh_authorization(current, admission):
    # Admission stores its authorization event hash, not sequence. The enable
    # event freezes exactly which already-authorized runs must only recover.
    return admission["run_id"] not in current.get("herdr_legacy_authorizations", [])


def enable(runs, mid):
    with state.MissionTransaction(runs, mid) as transaction:
        current = transaction.current_state
        if current["status"] in state.TERMINAL_STATUSES or "herdr_control" in current:
            return current
        transaction.append_event(kind="herdr_supervision_enabled", actor="CONTROL",
            idempotency_key="herdr:supervision:v1", payload={"version": 1})
        return transaction.current_state


def backend_generation(runs, mid):
    with fleet_safe_paths.RootedFS(runs) as fs:
        raw = fs.read_regular_optional(Path("missions") / mid / "herdr-backend.json",
            directory_modes=(0o700, 0o700), file_mode=0o600, max_bytes=16 * 1024 * 1024)
    if raw is None:
        return None
    backend = fleet_json.loads(raw)
    if backend.get("mission_id") != mid:
        raise state.MissionConflict("backend belongs to another Mission")
    return state.normalize_uuid(backend.get("generation"), "backend generation")


def request(runs, mid, *, action, reason, idempotency_key, run_id=None, generation=None):
    mid = state.normalize_uuid(mid, "mission_id")
    compiled, initial = fleet_mission.load_mission_compiled(runs, mid, mode="read")
    if compiled["resolved"]["preset"] != "astra_sol":
        raise state.MissionConflict("Herdr control requires astra_sol")
    if initial["status"] in state.TERMINAL_STATUSES:
        return {"mission_id": mid, "status": initial["status"], "recorded": False, "reason": "terminal Mission unchanged"}
    state._require_nonempty(idempotency_key, "control idempotency key")
    if run_id is not None:
        run_id = state.normalize_uuid(run_id, "run_id")
    actual = backend_generation(runs, mid) if action == "cancel" else None
    if generation is not None and generation != actual:
        raise state.MissionConflict("cancellation generation does not match owned backend")
    generation = actual
    rid = str(uuid.uuid5(uuid.UUID(mid), "control:" + idempotency_key))
    payload = {"request_id": rid, "action": action, "reason": reason, "run_id": run_id, "generation": generation}
    validate_payload("herdr_control_requested", payload)
    with state.MissionTransaction(runs, mid) as transaction:
        current = transaction.current_state
        if current["status"] in state.TERMINAL_STATUSES:
            return {"mission_id": mid, "status": current["status"], "recorded": False, "reason": "terminal Mission unchanged"}
        if run_id and not any(a["active"] and a["run_id"] == run_id for a in current["admissions"].values()):
            if any(a["run_id"] == run_id for a in current["admissions"].values()):
                return {"mission_id": mid, "status": current["status"], "recorded": False, "reason": "run already terminal"}
            raise state.MissionConflict("unknown owned run")
        if "herdr_control" not in current:
            transaction.append_event(kind="herdr_supervision_enabled", actor="CONTROL",
                idempotency_key="herdr:supervision:v1", payload={"version": 1})
        event, appended = transaction.append_event(kind="herdr_control_requested", actor="CONTROL",
            idempotency_key="herdr:control:" + idempotency_key, payload=payload)
        return {"mission_id": mid, "request_id": rid, "recorded": appended,
                "control": view(transaction.current_state), "request_event_sha256": event["event_sha256"]}


def acknowledge(runs, mid, request, *, cleanup_proof=None):
    return state.append_event(runs, mid, kind="herdr_control_applied", actor="CONTROL",
        idempotency_key="herdr:control-applied:" + request["request_id"],
        payload={"request_id": request["request_id"], "action": request["action"],
                 **({"functional_cleanup_artifact_id": cleanup_proof} if cleanup_proof else {})})
