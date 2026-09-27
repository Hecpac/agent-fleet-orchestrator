"""Strict owner messages and controller-bound, offline-verifiable interaction.

This is not a scheduler or an acceptance receipt. The caller supplies trusted
admission/session/turn bindings; no identity is taken from the authored message.
No ledger, CAS, candidate, runtime or decision is mutated by these functions.
"""
from __future__ import annotations

import copy

import fleet_herdr_evidence as evidence
import fleet_herdr_work_packet as work
import fleet_json
import fleet_mission_state as state


class OwnerProtocolError(ValueError):
    pass


def parse_response(raw, packet):
    """Only content belongs to the owner; paths/checks are unverified claims."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= work.MAX_RESPONSE_BYTES:
        raise OwnerProtocolError("owner response must be bounded UTF-8 JSON bytes")
    try:
        value = fleet_json.loads(raw)
        if not isinstance(value, dict):
            raise OwnerProtocolError("owner response must be an object")
        kind = value.get("type")
        if kind == "submit_candidate":
            if set(value) != {"type", "summary", "paths", "checks"}:
                raise OwnerProtocolError("candidate requires only type, summary, paths and checks")
            work.text(value["summary"], "candidate summary")
            work.paths(value["paths"], "candidate paths")
            if not set(value["paths"]).issubset(packet["scope"]["editable_paths"]):
                raise OwnerProtocolError("candidate path is outside the authorized editable scope")
            if not isinstance(value["checks"], list) or len(value["checks"]) > 50:
                raise OwnerProtocolError("invalid owner check list")
            for check in value["checks"]:
                if (not isinstance(check, dict) or set(check) != {"name", "outcome", "detail"}
                        or not isinstance(check["outcome"], str)
                        or check["outcome"] not in {"passed", "failed", "not_run"}):
                    raise OwnerProtocolError("invalid owner check claim")
                work.text(check["name"], "check name")
                work.text(check["detail"], "check detail")
        elif kind == "request_decision":
            if set(value) != {"type", "question", "why_needed", "options", "recommendation", "work_completed"}:
                raise OwnerProtocolError("decision requires only question, reason, options, recommendation and preserved work")
            work.text(value["question"], "decision question")
            work.text(value["why_needed"], "decision reason")
            work.text(value["work_completed"], "preserved work", empty=True)
            if not isinstance(value["options"], list) or len(value["options"]) > 3:
                raise OwnerProtocolError("decision options must contain 0..3 alternatives")
            labels = []
            for option in value["options"]:
                if not isinstance(option, dict) or set(option) != {"label", "consequence"}:
                    raise OwnerProtocolError("invalid decision alternative")
                labels.append(work.text(option["label"], "option label"))
                work.text(option["consequence"], "option consequence")
            if len(set(labels)) != len(labels):
                raise OwnerProtocolError("duplicate decision alternative")
            if value["recommendation"] is not None and value["recommendation"] not in labels:
                raise OwnerProtocolError("recommendation must name an option or be null")
        else:
            raise OwnerProtocolError("unsupported owner response type")
        return value
    except (work.WorkPacketError, fleet_json.FleetJSONError, state.MissionStateError) as exc:
        raise OwnerProtocolError(str(exc)) from exc


def _execution(value, packet_sha):
    fields = {"mission_id", "run_id", "admission_id", "instance_id", "herdr_session",
              "agent_session", "turn_id", "task_sha256"}
    if (not isinstance(value, dict) or set(value) != fields
            or value["instance_id"] != "worker" or value["task_sha256"] != packet_sha):
        raise OwnerProtocolError("owner execution binding is incomplete or refers to another task")
    for field in ("mission_id", "run_id", "admission_id"):
        try:
            if state.normalize_uuid(value[field], field) != value[field]:
                raise OwnerProtocolError("noncanonical owner execution identity")
        except state.MissionStateError as exc:
            raise OwnerProtocolError("invalid owner execution identity") from exc
    for field in ("herdr_session", "agent_session", "turn_id"):
        work.text(value[field], field, maximum=256)
    return copy.deepcopy(value)


def bind_response(prepared, execution, *, expected_envelope_sha256, transcript, final_bytes):
    """Bind a completed response to controller authority without claiming acceptance.

    `execution` and the expected envelope pin MUST come from the controller, not
    the model or a caller-supplied receipt. C will persist their ledger linkage;
    until then only preview/provider-free fixtures can use this staged protocol.
    """
    work.verify(prepared)
    envelope = prepared["execution_envelope"]
    if work.digest(envelope) != expected_envelope_sha256:
        raise OwnerProtocolError("owner execution envelope differs from controller pin")
    execution = _execution(execution, envelope["work_packet_sha256"])
    message = parse_response(final_bytes, prepared["work_packet"])
    policy = prepared["sources"]["permissions"]
    attestation = evidence.verify_transcript(transcript,
        agent_session=execution["agent_session"], model=policy["model"],
        turn_id=execution["turn_id"], prompt_sha256=execution["task_sha256"],
        final_bytes=final_bytes, permission_policy=policy, runtime_contract=envelope["runtime_contract"])
    # Retain byte-exact authored JSON separately. Controller fields never pretend
    # to have been authored by the agent; no fabricated legacy PASS/artifacts.
    prepared_bytes = fleet_json.canonical_bytes(prepared)
    blobs = {state.artifact_id(raw): raw for raw in (prepared_bytes, transcript, final_bytes)}
    receipt = {"contract_version": "owner-interaction-receipt-v1", "protocol": work.RESULT_VERSION,
        "execution_envelope_sha256": expected_envelope_sha256,
        "prepared_work_sha256": state.artifact_id(prepared_bytes), "execution": execution,
        "transcript_sha256": state.artifact_id(transcript), "authored_response_sha256": state.artifact_id(final_bytes),
        "permission_attestation": attestation, "message": message,
        "disposition": "candidate_proposed" if message["type"] == "submit_candidate" else "decision_requested",
        "authority": "none", "candidate_accepted": False, "decision_applied": False,
        "owner_cycle_enabled": False}
    return {"receipt": receipt, "artifacts": blobs}


def verify_receipt(receipt, *, read_artifact, expected_execution, expected_envelope_sha256):
    """Offline interaction proof; never an archive or Mission-success predicate."""
    try:
        def read(field):
            pin = receipt[field]
            if not isinstance(pin, str) or not state.SHA256.fullmatch(pin):
                raise OwnerProtocolError("invalid owner evidence pin")
            raw = read_artifact(pin)
            if state.artifact_id(raw) != pin:
                raise OwnerProtocolError("owner evidence CAS mismatch")
            return raw
        prepared = fleet_json.loads(read("prepared_work_sha256"))
        bound = bind_response(prepared, expected_execution,
            expected_envelope_sha256=expected_envelope_sha256,
            transcript=read("transcript_sha256"), final_bytes=read("authored_response_sha256"))
        if receipt != bound["receipt"]:
            raise OwnerProtocolError("owner interaction receipt differs from bound evidence")
        return receipt
    except (KeyError, TypeError, fleet_json.FleetJSONError) as exc:
        raise OwnerProtocolError("owner interaction receipt is incomplete") from exc
