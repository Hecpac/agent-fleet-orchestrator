"""Evidence adapters, never launchers or permission grants.

Codex native transcripts can attest recorded configuration. Other CLI event
formats lack that evidence here and cannot enter the owner writer lane. No
adapter turns an acknowledgement into quiescence or unknown usage into zero.
"""
from __future__ import annotations

import copy

import fleet_herdr_evidence as evidence
import fleet_herdr_owner_contract as contracts
import fleet_herdr_work_packet as work
import fleet_json
import fleet_mission_state as state


CAPABILITIES = {
    "codex": {"delivery": "strict-terminal-json-with-recorded-repair",
              "configuration": "native-turn-context", "live_dispatch": False,
              "effective_isolation": "NOT_VERIFIED", "cancellation": "exact-resource-observation-required"},
    "claude": {"delivery": "json-schema-flag-observed; native-result-inspection-only",
               "configuration": "NOT_VERIFIED", "live_dispatch": False,
               "effective_isolation": "NOT_VERIFIED", "cancellation": "NOT_VERIFIED"},
    "opencode": {"delivery": "native-events-inspection-only",
                 "configuration": "NOT_VERIFIED", "live_dispatch": False,
                 "effective_isolation": "NOT_VERIFIED", "cancellation": "NOT_VERIFIED"},
    "dsh": {"delivery": "NOT_VERIFIED", "configuration": "NOT_VERIFIED",
            "live_dispatch": False, "effective_isolation": "NOT_VERIFIED", "cancellation": "NOT_VERIFIED"},
    "mini-swe-agent": {"delivery": "NOT_VERIFIED", "configuration": "NOT_VERIFIED",
                       "live_dispatch": False, "effective_isolation": "NOT_VERIFIED", "cancellation": "NOT_VERIFIED"}}


def validate_binding(value):
    contracts.exact(value, {"cli", "cli_version", "provider", "model", "effort"}, "runtime")
    if (value["cli"] != "codex" or value["cli_version"] not in {"0.154.0", "0.155.1"}
            or value["provider"] != "openai" or value["model"] not in {"gpt-6-astra", "gpt-5.6-sol"}
            or value["effort"] not in {"high", "max"}):
        raise contracts.ContractError("runtime has no owner evidence adapter")
    return copy.deepcopy(value)


def verify_terminal(raw, final, *, contract, admission, native_binding=None):
    """Called only against an existing admission in the controller journal."""
    contracts.validate(contract)
    contracts.validate_admission(admission, contract)
    if contract["version"] in {"owner-cycle-contract-v3", "owner-cycle-contract-v4"}:
        from fleet_harness_delivery import verify_terminal as verify_mini
        from fleet_herdr_scope import ScopeError
        try:return verify_mini(raw, final, contract=contract, admission=admission)
        except (AttributeError,IndexError,KeyError,TypeError,ScopeError) as exc:
            raise contracts.ContractError("malformed Mini runtime evidence") from exc
    expected = validate_binding(contract["runtime"])
    identity = admission
    if admission["version"] == "owner-cycle-admission-v2":
        contracts.exact(native_binding, {"resource", "agent_session", "turn_id", "runtime"}, "native binding")
        if native_binding["resource"] != resource(admission) or native_binding["runtime"] != expected:
            raise evidence.EvidenceError("native binding differs from CONTROL admission")
        identity = native_binding
    rows = fleet_json.load_jsonl(raw, require_nonempty=True)
    if any(not isinstance(r, dict) or not isinstance(r.get("payload"), dict) for r in rows):
        raise evidence.EvidenceError("invalid native transcript row")
    metadata = [r["payload"] for r in rows if r.get("type") == "session_meta"]
    if len(metadata) != 1 or metadata[0].get("cli_version") != expected["cli_version"]:
        raise evidence.EvidenceError("CLI version differs from admitted runtime")
    # A complete isolated turn segment is required. Old turns cannot be relabelled.
    starts = [i for i, r in enumerate(rows) if r.get("type") == "event_msg" and r["payload"].get("type") == "task_started"]
    if len(starts) != 1:
        raise evidence.EvidenceError("owner evidence must contain exactly one turn")
    users = [r for r in rows if r.get("type") == "response_item" and r["payload"].get("role") == "user"]
    if len(users) != 1 or any(r.get("type") == "response_item" and r["payload"].get("role") in {"system", "developer"} for r in rows):
        raise evidence.EvidenceError("instructions outside the single frozen task")
    pending, seen_calls = {}, set()
    for row in rows:
        payload = row["payload"]
        if row.get("type") != "response_item":
            continue
        kind, call = payload.get("type"), payload.get("call_id")
        if kind not in {"message", "reasoning", "function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output"}:
            raise evidence.EvidenceError("unsupported native response item")
        if kind in {"function_call", "custom_tool_call"}:
            if not isinstance(call, str) or not call or call in seen_calls:
                raise evidence.EvidenceError("ambiguous native tool call")
            seen_calls.add(call)
            pending[call] = kind + "_output"
        if kind in {"function_call_output", "custom_tool_call_output"}:
            if not isinstance(call, str) or pending.get(call) != kind:
                raise evidence.EvidenceError("tool output has no bound call")
            del pending[call]
    if pending:
        raise evidence.EvidenceError("native tools still pending at terminal")
    finals = [i for i, r in enumerate(rows) if r.get("type") == "response_item" and r["payload"].get("phase") == "final_answer"]
    if len(finals) != 1:
        raise evidence.EvidenceError("ambiguous owner terminal")
    for r in rows[finals[0] + 1:]:
        if (r.get("type") != "event_msg"
                or r["payload"].get("type") not in {"token_count", "task_complete"}):
            raise evidence.EvidenceError("activity follows owner final answer")
    if any(r.get("type") == "event_msg" and r["payload"].get("type") in {"task_aborted", "turn_aborted", "error"} for r in rows):
        raise evidence.EvidenceError("aborted owner turn cannot deliver a candidate")
    evidence.verify_transcript(raw, agent_session=identity["agent_session"],
        turn_id=identity["turn_id"], prompt_sha256=admission["prompt_sha256"],
        final_bytes=final, model=expected["model"], expected_provider=expected["provider"],
        expected_effort=expected["effort"])
    policy = contract["prepared"]["sources"]["permissions"]
    contexts = [r["payload"] for r in rows if r.get("type") == "turn_context"]
    for context in contexts:
        for key in ("cwd", "approval_policy", "sandbox_policy"):
            actual = copy.deepcopy(context.get(key))
            if key == "sandbox_policy" and isinstance(actual, dict) and actual.get("writable_roots") == []:
                actual.pop("writable_roots")
            # Canonical JSON also distinguishes booleans from integers.
            if fleet_json.canonical_bytes(actual) != fleet_json.canonical_bytes(policy[key]):
                raise evidence.EvidenceError("recorded permissions differ from admission: " + key)
    return {"version": "owner-runtime-evidence-v1", "observed": expected,
            "agent_session": identity["agent_session"], "turn_id": identity["turn_id"],
            "transcript_sha256": state.artifact_id(raw),
            "permissions": "recorded_configuration_attested", "effective_isolation": "NOT_VERIFIED",
            "usage": {"tokens": None, "cost_usd": None, "reason": "no_admission_bound_usage_baseline"},
            "authority": "none"}


def resource(binding):
    result = {k: binding[k] for k in ("cycle_id", "run_id", "admission_id", "generation", "agent_session", "turn_id", "prompt_sha256")}
    if binding["version"] == "owner-cycle-admission-v2":
        result["surface"] = copy.deepcopy(binding["surface"])
    return result


def verify_quiescence(observation, admission):
    contracts.exact(observation, {"resource", "inactive", "resources_clean"}, "quiescence")
    if (observation["resource"] != resource(admission) or observation["inactive"] is not True
            or observation["resources_clean"] is not True):
        raise contracts.ContractError("exact owned resource is not confirmed inactive and clean")
    return observation


def inspect_claude(raw):
    """Inspect preserved native events without admitting a writer or provider."""
    rows = fleet_json.load_jsonl(raw, require_nonempty=True)
    init = [r for r in rows if r.get("type") == "system" and r.get("subtype") == "init"]
    terminal = [r for r in rows if r.get("type") == "result"]
    if (len(init) != 1 or len(terminal) != 1 or rows[-1] != terminal[0]
            or terminal[0].get("session_id") != init[0].get("session_id")
            or not init[0].get("session_id") or terminal[0].get("subtype") != "success"
            or terminal[0].get("is_error") is not False or terminal[0].get("stop_reason") != "end_turn"
            or not isinstance(terminal[0].get("result"), str)):
        raise contracts.ContractError("ambiguous or unsuccessful Claude terminal")
    for key in ("session_id", "claude_code_version"):
        work.text(init[0].get(key), "Claude " + key, maximum=256)
    work.text(terminal[0]["result"], "Claude final", maximum=work.MAX_RESPONSE_BYTES)
    if rows[0] != init[0] or any(r.get("session_id", init[0]["session_id"]) != init[0]["session_id"] for r in rows):
        raise contracts.ContractError("Claude events cross sessions or precede initialization")
    return {"cli": "claude", "cli_version": init[0].get("claude_code_version"),
            "session": init[0]["session_id"], "final": terminal[0]["result"],
            "provider": None, "effort": None, "task_binding": "NOT_VERIFIED",
            "permissions": "NOT_VERIFIED", "cost_usd": None,
            "admissible": False, "authority": "none"}
