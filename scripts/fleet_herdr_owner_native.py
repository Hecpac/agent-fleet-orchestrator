"""Pure, replayable bindings for fresh Codex sessions on owned Herdr surfaces.

Observations come from a trusted injected transport. These checks do not grant
native effects, establish OS isolation, or turn a Herdr status into quiescence.
"""
from __future__ import annotations

import copy

import fleet_herdr_evidence as evidence
import fleet_herdr_metrics as metrics
import fleet_herdr_owner_contract as contracts
import fleet_herdr_owner_runtime as runtime
import fleet_herdr_work_packet as work
import fleet_json
import fleet_mission_state as state


def retain(observation, put):
    contracts.exact(observation, {"resource", "receipt", "transcript", "inactive", "resources_clean"}, "native observation")
    if not isinstance(observation["transcript"], bytes):
        raise contracts.ContractError("native transcript must be original bytes")
    return {**copy.deepcopy(observation), "transcript": put(observation["transcript"])}


def inspect_snapshot(snapshot, admission, read):
    from fleet_herdr import _agent_receipt
    contracts.exact(snapshot, {"resource", "receipt", "transcript", "inactive", "resources_clean"}, "retained native snapshot")
    if snapshot["resource"] != runtime.resource(admission):
        raise contracts.ContractError("native observation belongs to another owned generation")
    for key in ("inactive", "resources_clean"):
        if type(snapshot[key]) is not bool:
            raise contracts.ContractError("resource observation must be explicit")
    if not isinstance(snapshot["receipt"], dict):
        raise contracts.ContractError("invalid Herdr receipt")
    try:
        receipt = _agent_receipt(snapshot["receipt"], allow_missing_session=True)
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        raise contracts.ContractError("invalid Herdr receipt: " + str(exc)) from exc
    if any(receipt[k] != admission["surface"][k] for k in receipt if k in admission["surface"]):
        raise contracts.ContractError("Herdr receipt names another owned surface")
    session = receipt["agent_session"]
    if session is not None and (session["agent"] != "codex" or session["source"] != "codex"):
        raise contracts.ContractError("session receipt is not native Codex identity")
    raw = read(snapshot["transcript"])
    if not isinstance(raw, bytes) or len(raw) > 32 * 1024 * 1024:
        raise contracts.ContractError("unbounded native snapshot")
    rows = fleet_json.load_jsonl(raw) if raw and raw.endswith(b"\n") else []
    if any(not isinstance(r, dict) or not isinstance(r.get("payload"), dict) for r in rows):
        raise contracts.ContractError("invalid native snapshot rows")
    return receipt, raw, rows


def before_dispatch(snapshot, admission, read, *, contract):
    receipt, raw, rows = inspect_snapshot(snapshot, admission, read)
    if (receipt["agent_status"] != "idle" or not snapshot["inactive"] or not snapshot["resources_clean"]
            or (raw and not raw.endswith(b"\n")) or len(rows) > 1
            or any(r.get("type") != "session_meta" for r in rows)):
        raise contracts.ContractError("attempt requires a fresh inactive native session")
    if rows and (receipt["agent_session"] is None or receipt["agent_session"]["value"] != rows[0]["payload"].get("id")):
        raise contracts.ContractError("fresh session metadata differs from receipt")
    if not rows and receipt["agent_session"] is not None:
        raise contracts.ContractError("existing native session lacks its complete transcript")
    if rows and (rows[0]["payload"].get("cli_version") != contract["runtime"]["cli_version"]
                 or rows[0]["payload"].get("model_provider") != contract["runtime"]["provider"]):
        raise contracts.ContractError("pre-dispatch native runtime differs from admission")
    return {"kind": "herdr_usage_baseline", "status": "known",
            "source": "observed_empty_session_before_first_turn",
            "counts": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}}


def observe(snapshot, *, contract, admission, baseline, read):
    """No slicing at task_complete: later activity remains visible and rejects."""
    receipt, raw, rows = inspect_snapshot(snapshot, admission, read)
    prior_receipt, prefix, _ = inspect_snapshot(baseline, admission, read)
    before_dispatch(baseline, admission, read, contract=contract)
    if receipt["revision"] < prior_receipt["revision"] or not raw.startswith(prefix):
        raise contracts.ContractError("native evidence regressed behind dispatch frontier")
    if prior_receipt["agent_session"] is not None and receipt["agent_session"] != prior_receipt["agent_session"]:
        raise contracts.ContractError("pre-dispatch native session changed")
    result = {"binding": None, "response": None, "quiescence": None, "active": False}
    if raw and not raw.endswith(b"\n"):
        return result  # Original partial bytes retained; cannot attest a prefix.
    metadata = [r["payload"] for r in rows if r.get("type") == "session_meta"]
    starts = [(i, r["payload"]) for i, r in enumerate(rows)
              if r.get("type") == "event_msg" and r["payload"].get("type") == "task_started"]
    users = [(i, r["payload"]) for i, r in enumerate(rows)
             if r.get("type") == "response_item" and r["payload"].get("role") == "user"]
    if len(metadata) > 1 or len(starts) > 1 or len(users) > 1:
        raise contracts.ContractError("native snapshot contains more than the admitted turn")
    expected = runtime.validate_binding(contract["runtime"])
    if metadata and (rows[0].get("type") != "session_meta"
                     or metadata[0].get("cli_version") != expected["cli_version"]
                     or metadata[0].get("model_provider") != expected["provider"]):
        raise contracts.ContractError("native CLI/provider differs from admission")
    if any(r.get("type") == "response_item" and r["payload"].get("role") in {"system", "developer"} for r in rows):
        raise contracts.ContractError("unbound native instructions")
    if metadata and receipt["agent_session"] is not None and receipt["agent_session"]["value"] != metadata[0].get("id"):
        raise contracts.ContractError("native transcript and Herdr receipt disagree")
    if metadata and starts and users and receipt["agent_session"] is not None:
        sid, tid = metadata[0].get("id"), starts[0][1].get("turn_id")
        work.text(sid, "observed native session", maximum=256)
        work.text(tid, "observed native turn", maximum=256)
        if (users[0][0] <= starts[0][0]
                or state.artifact_id(evidence._text(users[0][1], "input_text").encode()) != admission["prompt_sha256"]):
            raise contracts.ContractError("observed prompt does not belong to admission")
        contexts = [r["payload"] for r in rows if r.get("type") == "turn_context"]
        if contexts:
            policy = contract["prepared"]["sources"]["permissions"]
            for context in contexts:
                if (context.get("turn_id") != tid or context.get("model") != expected["model"]
                        or context.get("effort") != expected["effort"]):
                    raise contracts.ContractError("observed native runtime differs from admission")
                for key in ("cwd", "approval_policy", "sandbox_policy"):
                    actual = copy.deepcopy(context.get(key))
                    if key == "sandbox_policy" and isinstance(actual, dict) and actual.get("writable_roots") == []:
                        actual.pop("writable_roots")
                    if fleet_json.canonical_bytes(actual) != fleet_json.canonical_bytes(policy[key]):
                        raise contracts.ContractError("observed native permission drift: " + key)
            result["binding"] = {"resource": runtime.resource(admission), "agent_session": sid,
                                 "turn_id": tid, "runtime": expected}
            ends = [r["payload"] for r in rows if r.get("type") == "event_msg"
                    and r["payload"].get("type") in {"task_complete", "task_aborted", "turn_aborted"}]
            if any(p.get("turn_id") != tid for p in ends) or len(ends) > 1:
                raise contracts.ContractError("ambiguous native terminal identity")
            result["active"] = not ends
            finals = [r["payload"] for r in rows if r.get("type") == "response_item"
                      and r["payload"].get("phase") == "final_answer"]
            if len(finals) > 1:
                raise contracts.ContractError("ambiguous native final answer")
            if ends and finals:
                final = evidence._text(finals[0], "output_text").encode()
                result["response"] = {"transcript": snapshot["transcript"], "final": final}
    # An empty read after an ambiguous send is not proof of absence. Explicit
    # resource attestation and a bound terminal are both required in this lane.
    pending, seen, unsafe, terminal_seen = {}, set(), False, False
    for row in rows:
        p = row["payload"]
        if row.get("type") == "response_item":
            if terminal_seen:
                unsafe = True
            kind, call = p.get("type"), p.get("call_id")
            if kind in {"function_call", "custom_tool_call"}:
                if not isinstance(call, str) or not call or call in seen:
                    unsafe = True
                else:
                    seen.add(call)
                    pending[call] = kind + "_output"
            if kind in {"function_call_output", "custom_tool_call_output"}:
                if not isinstance(call, str) or pending.get(call) != kind:
                    unsafe = True
                else:
                    del pending[call]
            if p.get("phase") == "final_answer":
                terminal_seen = True
        elif row.get("type") == "event_msg" and p.get("type") in {"task_complete", "task_aborted", "turn_aborted"}:
            terminal_seen = True
    if (snapshot["inactive"] and snapshot["resources_clean"] and receipt["agent_status"] in {"idle", "done", "blocked"}
            and result["binding"] is not None and not result["active"] and not pending and not unsafe):
        result["quiescence"] = {"resource": runtime.resource(admission), "inactive": True, "resources_clean": True}
    return result


def usage(raw, binding, baseline, admission, read, *, contract):
    return {**metrics.usage(fleet_json.load_jsonl(raw), binding["turn_id"],
                           before_dispatch(baseline, admission, read, contract=contract), baseline_frontier={"status": "verified"}),
            "cost_usd": None}
