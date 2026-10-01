"""Pure offline validation of the exact Codex turn retained by Herdr."""
from __future__ import annotations

import hashlib
from typing import Any

import fleet_json
import fleet_herdr_skill_context
import fleet_herdr_permissions as permissions
import fleet_herdr_versions as versions


class EvidenceError(ValueError):
    pass


def _text(payload: dict[str, Any], kind: str) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        raise EvidenceError("message content is not a list")
    pieces = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != kind or not isinstance(block.get("text"), str):
            raise EvidenceError("message contains unsupported content")
        pieces.append(block["text"])
    return "\n".join(pieces)


def verify_transcript(raw: bytes, *, agent_session: str, model: str, turn_id: str,
                      prompt_sha256: str, final_bytes: bytes,
                      permission_policy: dict[str, Any] | None = None,
                      runtime_contract: dict[str, Any] | None = None,
                      expected_provider: str = "openai", expected_effort: str = "high") -> dict[str, Any]:
    """Accept only a uniquely bound completed turn; no live filesystem/UI reads."""
    return _inspect_transcript(raw, agent_session=agent_session, model=model, turn_id=turn_id,
        prompt_sha256=prompt_sha256, final_bytes=final_bytes, permission_policy=permission_policy,
        runtime_contract=runtime_contract, expected_provider=expected_provider,
        expected_effort=expected_effort)


def observe_rejected_transcript(raw: bytes, *, agent_session: str, model: str, turn_id: str,
                                prompt_sha256: str, final_bytes: bytes,
                                runtime_contract=None, expected_provider="openai"):
    """Telemetry only after a retained rejection; never admission or permission authority.

    Bind the unique completed turn and final CAS bytes even when its context or
    completion rendering violated the strict contract. All identity, ordering,
    model, version and ambiguity checks remain mandatory.
    """
    _inspect_transcript(raw, agent_session=agent_session, model=model, turn_id=turn_id,
        prompt_sha256=prompt_sha256, final_bytes=final_bytes, runtime_contract=runtime_contract,
        expected_provider=expected_provider, observation_only=True)
    return {"status": "observed_rejected_turn", "authority": "none"}


def _inspect_transcript(raw: bytes, *, agent_session: str, model: str, turn_id: str,
                      prompt_sha256: str, final_bytes: bytes,
                      permission_policy: dict[str, Any] | None = None,
                      runtime_contract: dict[str, Any] | None = None,
                      expected_provider: str = "openai",
                      observation_only: bool = False, expected_effort: str = "high") -> dict[str, Any]:
    """Accept only a uniquely bound completed turn; no live filesystem/UI reads."""
    if not raw or len(raw) > 32 * 1024 * 1024 or not raw.endswith(b"\n"):
        raise EvidenceError("transcript must be bounded complete JSONL")
    if not all(isinstance(value, str) and value for value in (agent_session, model, turn_id, prompt_sha256)):
        raise EvidenceError("expected identity is incomplete")
    try:
        rows = fleet_json.load_jsonl(raw, require_nonempty=True)
    except fleet_json.FleetJSONError as exc:
        raise EvidenceError("transcript JSONL is invalid") from exc
    if any(not isinstance(row, dict) or not isinstance(row.get("payload"), dict) for row in rows):
        raise EvidenceError("transcript row is invalid")
    metadata = [r["payload"] for r in rows if r.get("type") == "session_meta"]
    if len(metadata) != 1 or metadata[0].get("id") != agent_session or metadata[0].get("model_provider") != expected_provider:
        raise EvidenceError("session/provider binding mismatch")
    if runtime_contract is not None:
        try:
            contract = versions.validate(runtime_contract)
        except ValueError as exc:
            raise EvidenceError(str(exc)) from exc
        if metadata[0].get("cli_version") != contract["codex_version"]:
            raise EvidenceError("Codex CLI version differs from bound runtime contract")
    starts = []
    for index, row in enumerate(rows):
        p = row["payload"]
        if row.get("type") == "event_msg" and p.get("type") == "task_started":
            identifier = p.get("turn_id")
            if not isinstance(identifier, str) or not identifier:
                raise EvidenceError("task_started lacks turn id")
            if not starts or starts[-1][0] != identifier:
                starts.append((identifier, index))
    matching = []
    for position, (identifier, start) in enumerate(starts):
        end = starts[position + 1][1] if position + 1 < len(starts) else len(rows)
        matches = []
        for index in range(start, end):
            row, p = rows[index], rows[index]["payload"]
            if row.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                if hashlib.sha256(_text(p, "input_text").encode()).hexdigest() == prompt_sha256:
                    matches.append(index)
        if matches:
            if len(matches) != 1:
                raise EvidenceError("prompt binding is ambiguous")
            matching.append((identifier, start, end, matches[0]))
    if len(matching) != 1 or matching[0][0] != turn_id:
        raise EvidenceError("prompt/turn binding mismatch")
    _, start, end, prompt_index = matching[0]
    if end != len(rows):
        raise EvidenceError("evidence extends beyond the bound turn")
    if sum(r.get("type") == "event_msg" and r["payload"].get("type") == "task_started" for r in rows[start:end]) != 1:
        raise EvidenceError("task_started is ambiguous")
    if not observation_only and any(r.get("type") == "response_item" and r["payload"].get("type") == "message"
           and r["payload"].get("role") == "user" for r in rows[prompt_index + 1:end]):
        raise EvidenceError("additional user input after bound prompt")
    if not observation_only and versions.task_inline(runtime_contract):
        for row in rows[start:prompt_index]:
            payload = row["payload"]
            if (row.get("type") == "response_item" and payload.get("role") == "user"
                    and ("skills.selected_skill_instructions" in payload.get(
                        "internal_chat_message_metadata_passthrough", {}).get("content_item_kinds", [])
                         or "<skill>" in _text(payload, "input_text"))):
                raise EvidenceError("skill input outside frozen task")
    contexts = [r["payload"] for r in rows[start:end] if r.get("type") == "turn_context"]
    if not contexts or any(p.get("turn_id") != turn_id or p.get("model") != model or p.get("effort") != expected_effort for p in contexts):
        raise EvidenceError("model/effort/turn context mismatch")
    finals = []
    completions = []
    for index in range(start, end):
        row, p = rows[index], rows[index]["payload"]
        if row.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "assistant" and p.get("phase") == "final_answer":
            finals.append((index, _text(p, "output_text").encode()))
        if row.get("type") == "event_msg" and p.get("type") == "task_complete":
            completions.append((index, p))
    if len(finals) != 1 or finals[0][1] != final_bytes or finals[0][0] <= prompt_index:
        raise EvidenceError("final answer binding mismatch")
    if any(row.get("type") == "turn_context" for row in rows[finals[0][0] + 1:end]):
        raise EvidenceError("turn context follows final answer")
    if (len(completions) != 1 or completions[0][0] <= finals[0][0]
            or completions[0][1].get("turn_id") != turn_id
            or not isinstance(completions[0][1].get("last_agent_message"), str)
            or (not observation_only and completions[0][1].get("last_agent_message", "").encode() != final_bytes)
            or completions[0][0] != len(rows) - 1):
        raise EvidenceError("task_complete binding mismatch")
    if permission_policy is None:
        return {"status": "not_attested", "reason": "historical_identity_only_contract"}
    try:
        return permissions.attest(contexts, permission_policy)
    except permissions.PermissionError as exc:
        raise EvidenceError(str(exc)) from exc


def verify_result(result: dict[str, Any], *, read_artifact, role: str, cwd: str,
                  prompt_sha256: str, agent_session: str | None = None,
                  capsule_manifest=None, current=None,
                  permission_version: int = permissions.VERSION) -> dict[str, Any]:
    """Common strict rule for collection, cached recovery, driver and archive.

    The reader must check CAS identity. Expected role/cwd/prompt come from
    controller-owned bindings, never the model-authored result.
    """
    try:
        proof = result["evidence"]
        session = proof["agent_session"]
        if (result["instance_id"] != role or session["kind"] != "id"
                or (agent_session is not None and session["value"] != agent_session)
                or proof["prompt_sha256"] != prompt_sha256
                or proof["transcript_sha256"] != proof["transcript_artifact_id"]):
            raise EvidenceError("permission result identity binding mismatch")
        if capsule_manifest is not None:
            import fleet_mission_capsule
            return fleet_mission_capsule.verify_result(result, read=read_artifact, role=role, cwd=cwd,
                prompt_sha256=prompt_sha256, expected_manifest=capsule_manifest, current=current)
        if 'capsule_launch_artifact_id' in proof:
            raise EvidenceError('capsule evidence requires creation-bound permission policy')
        if versions.task_inline(proof.get("runtime_contract")):
            fleet_herdr_skill_context.validate(fleet_json.loads(read_artifact(proof["context_artifact_id"])))
            fleet_herdr_skill_context.verify_task(read_artifact(prompt_sha256), role, read_artifact)
        expected = permissions.policy(role, cwd, version=permission_version)
        return verify_transcript(read_artifact(proof["transcript_artifact_id"]),
            agent_session=session["value"], model=expected["model"],
            turn_id=result["turn_id"], prompt_sha256=prompt_sha256,
            final_bytes=read_artifact(result["artifact_id"]),
            permission_policy=expected,
            runtime_contract=proof.get("runtime_contract"))
    except EvidenceError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceError("permission result evidence is incomplete or incompatible") from exc
    except RuntimeError as exc:
        from fleet_herdr_inference import InferenceError
        if not isinstance(exc, InferenceError):
            raise
        raise EvidenceError('capsule permission evidence is incompatible') from exc
