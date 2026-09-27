"""Creation-bound contracts for the disabled, provider-free owner cycle.

The v1 WorkPacket remains readable and unchanged. This is a new task/profile,
not an activation flag for a historical sol_minimal_v1 Mission.
"""
from __future__ import annotations

import copy
import math

import fleet_herdr_work_packet as work
import fleet_json
import fleet_mission_state as state

PROFILE = {"version": "owner-cycle-profile-v1", "profile_id": "owner_cycle_v1",
           "writer": "worker", "dispatch_enabled": False,
           "lane": "offline-conformance", "result_protocol": work.RESULT_VERSION}


class ContractError(ValueError):
    pass


def exact(value, fields, where):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ContractError("invalid fields: " + where)


def pin(value):
    if not isinstance(value, str) or not state.SHA256.fullmatch(value):
        raise ContractError("invalid content identity")
    return value


def identity(value):
    if not isinstance(value, str) or state.normalize_uuid(value, "owner identity") != value:
        raise ContractError("noncanonical owner identity")
    return value


def instant(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ContractError("invalid controller time")
    return value


def limits(value):
    exact(value, {"max_attempts", "deadline_seconds", "token_limit", "cost_limit_usd"}, "limits")
    for field, maximum in (("max_attempts", 20), ("deadline_seconds", 3600)):
        if type(value[field]) is not int or not 1 <= value[field] <= maximum:
            raise ContractError("invalid limit: " + field)
    # No runtime in this lane can currently enforce these across auxiliary calls.
    if value["token_limit"] is not None or value["cost_limit_usd"] is not None:
        raise ContractError("unobservable token/cost budgets cannot be enforced")
    return copy.deepcopy(value)


def surface(value):
    exact(value, {"herdr_session", "agent_name", "workspace_id", "tab_id", "pane_id", "terminal_id"}, "owned Herdr surface")
    for item in value.values():
        work.text(item, "surface identity", maximum=256)
        if item.startswith("-") or any(c.isspace() for c in item):
            raise ContractError("invalid surface identity")
    return copy.deepcopy(value)


def create(prepared, *, cycle_id, started_at, budget, runtime=None, surfaces=None):
    import fleet_herdr_owner_runtime as adapters
    work.verify(prepared)
    if prepared["execution_envelope"]["contract_version"] == "owner-harness-envelope-v1":
        raise ContractError("harness preparation requires its v3 creation contract; no legacy downgrade")
    budget = limits(budget)
    if budget["deadline_seconds"] > prepared["sources"]["timeout_seconds"]:
        raise ContractError("cycle cannot extend the prepared deadline")
    policy = prepared["sources"]["permissions"]
    runtime = adapters.validate_binding({"cli": "codex", "cli_version": "0.154.0",
        "provider": "openai", "model": policy["model"], "effort": policy["effort"]} if runtime is None else runtime)
    result = {"version": "owner-cycle-contract-v1", "profile": copy.deepcopy(PROFILE), "runtime": runtime,
            "cycle_id": identity(cycle_id), "started_at": instant(started_at),
            "deadline_at": started_at + budget["deadline_seconds"],
            "limits": budget, "prepared": copy.deepcopy(prepared)}
    if surfaces is not None:
        if not isinstance(surfaces, list) or len(surfaces) != budget["max_attempts"]:
            raise ContractError("one fresh owned surface per attempt is required")
        targets = [surface(s) for s in surfaces]
        for i, target in enumerate(targets):
            for other in targets[:i]:
                if ((target["herdr_session"], target["agent_name"]) == (other["herdr_session"], other["agent_name"])
                        or any(target[k] == other[k] for k in ("pane_id", "terminal_id"))):
                    raise ContractError("owner attempts cannot reuse a surface")
        result.update(version="owner-cycle-contract-v2", surfaces=targets)
        result["profile"].update(version="owner-cycle-profile-v2", profile_id="owner_cycle_observed_v2")
    return result


def validate(value):
    if isinstance(value, dict) and value.get("version") == "owner-cycle-contract-v3":
        from fleet_harness_contract import validate as validate_harness
        return validate_harness(value)
    fields = {"version", "profile", "runtime", "cycle_id", "started_at", "deadline_at", "limits", "prepared"}
    observed = isinstance(value, dict) and value.get("version") == "owner-cycle-contract-v2"
    exact(value, fields | ({"surfaces"} if observed else set()), "cycle contract")
    expected = create(value["prepared"], cycle_id=value["cycle_id"],
                      started_at=value["started_at"], budget=value["limits"], runtime=value["runtime"],
                      surfaces=value.get("surfaces"))
    if fleet_json.canonical_bytes(value) != fleet_json.canonical_bytes(expected):
        raise ContractError("creation contract was changed")
    return value


def task(contract, *, attempt, feedback, decisions):
    validate(contract)
    if type(attempt) is not int or not 1 <= attempt <= contract["limits"]["max_attempts"]:
        raise ContractError("attempt budget exhausted")
    if feedback is not None:
        exact(feedback, {"reason", "detail"}, "continuation feedback")
        if feedback["reason"] not in {"invalid_delivery", "decision_resolved", "checks_rejected"}:
            raise ContractError("unknown continuation reason")
    if not isinstance(decisions, list) or len(decisions) > contract["limits"]["max_attempts"]:
        raise ContractError("unbounded decision context")
    for decision in decisions:
        exact(decision, {"request_id", "answer", "authority"}, "decision context")
        pin(decision["request_id"])
        work.text(decision["answer"], "decision answer")
        if decision["authority"] != "within_existing_contract":
            raise ContractError("decision cannot amend authority")
    packet = copy.deepcopy(contract["prepared"]["work_packet"])
    packet["contract_version"] = "owner-cycle-task-v1"
    packet["limits"] = {**packet["limits"], "owner_cycle_enabled": True,
                        "repair_enabled": True, "amendments_enabled": False,
                        "max_attempts": contract["limits"]["max_attempts"],
                        "deadline_at": contract["deadline_at"],
                        "runtime_lane": PROFILE["lane"]}
    if contract["version"] == "owner-cycle-contract-v3":
        packet["contract_version"] = "owner-cycle-task-v2"
        packet["limits"]["runtime_lane"] = contract["profile"]["lane"]
    packet["continuation"] = {"attempt": attempt, "feedback": copy.deepcopy(feedback),
                              "decisions": copy.deepcopy(decisions)}
    # Controller-authored nonce disambiguates byte-identical work across cycles.
    # The model need not calculate or return it; identities stay in the envelope.
    packet["delivery_nonce"] = work.digest({"cycle": contract["cycle_id"], "attempt": attempt})
    if len(fleet_json.canonical_bytes(packet)) > work.MAX_PACKET_BYTES:
        raise ContractError("continuation exceeds bounded task context")
    return packet


def admission(contract, *, ordinal, run_id, admission_id, generation,
              agent_session, turn_id, prompt_sha256, parent_revision):
    validate(contract)
    if type(ordinal) is not int or not 1 <= ordinal <= contract["limits"]["max_attempts"]:
        raise ContractError("attempt outside creation budget")
    observed = contract["version"] == "owner-cycle-contract-v2"
    if observed:
        if agent_session is not None or turn_id is not None:
            raise ContractError("native identities must be observed after dispatch")
    else:
        for value in (agent_session, turn_id):
            work.text(value, "session/turn identity", maximum=256)
    if parent_revision is not None:
        pin(parent_revision)
    result = {"version": "owner-cycle-admission-v1", "cycle_id": contract["cycle_id"],
            "contract_sha256": work.digest(contract), "ordinal": ordinal,
            "run_id": identity(run_id), "admission_id": identity(admission_id),
            "generation": identity(generation), "writer": "worker",
            "agent_session": agent_session, "turn_id": turn_id,
            "prompt_sha256": pin(prompt_sha256), "parent_revision": parent_revision,
            "deadline_at": contract["deadline_at"]}
    if observed:
        result.update(version="owner-cycle-admission-v2", surface=copy.deepcopy(contract["surfaces"][ordinal-1]))
    elif contract["version"] == "owner-cycle-contract-v3":
        result["version"] = "owner-cycle-admission-v3"
    return result


def validate_admission(value, contract):
    fields = {"version", "cycle_id", "contract_sha256", "ordinal", "run_id", "admission_id",
              "generation", "writer", "agent_session", "turn_id", "prompt_sha256",
              "parent_revision", "deadline_at"}
    if contract["version"] == "owner-cycle-contract-v2":
        fields.add("surface")
    exact(value, fields, "admission")
    expected = admission(contract, **{k: value[k] for k in (
        "ordinal", "run_id", "admission_id", "generation", "agent_session", "turn_id",
        "prompt_sha256", "parent_revision")})
    if fleet_json.canonical_bytes(value) != fleet_json.canonical_bytes(expected):
        raise ContractError("admission differs from creation authority")
    return value
