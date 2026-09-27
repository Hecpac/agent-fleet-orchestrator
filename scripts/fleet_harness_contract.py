"""Explicit opt-in local contract; public repair packet and private acceptance.

This extends the owner journal without upgrading historical v1/v2 contracts or
enabling Herdr transport. Model configuration is requested, not observed identity.
"""
import copy
from pathlib import Path

import fleet_acceptance
import fleet_harness_acceptance as checker
import fleet_harness_budget as budget
import fleet_harness_mini as mini
import fleet_harness_read_scope as read_scope
import fleet_herdr_instructions as instructions
import fleet_herdr_owner_contract as owner
import fleet_herdr_scope as scope
import fleet_herdr_work_packet as work
import fleet_json

ENVELOPE = "owner-harness-envelope-v1"
VERSION = "owner-cycle-contract-v3"
RUNTIME = {"cli": "mini-swe-agent", "cli_version": "2.4.6", "provider": "deepseek",
           "model": "deepseek-flash", "effort": "max"}


def project(sources):
    checker.validate_suite(fleet_json.loads(sources["functional_tests"]))
    public = checker.public_projection(fleet_json.loads(sources["functional_tests"]))
    public["public_cases"] = [{"id":c["id"], "family":c["family"]} for c in public["public_cases"]]
    public["public_cases_file"] = "/bridge/public-checks.json"
    packet = {"contract_version": "owner-work-v2", "objective": sources["objective"],
        "workspace": {"root": "/candidate"}, "scope": copy.deepcopy(sources["scope"]),
        "requirements": {"artifact": sources["acceptance"]["requirements"], "functional": public},
        "instructions": work._instructions(sources["instructions"]),
        "result_protocol": work._response_contract(),
        "capabilities": {"shell": "bash in owned networkless executor; existing exact-file content updates only",
            "writable_temporaries": ["/tmp", "/work", *["/candidate/"+p for p in sources["scope"]["temporary_directories"]]],
            "network": False, "provider_credentials": False, "control_sockets": False,
            "rename_delete_editable_mountpoints": False, "delegation": False},
        "limits": {"deadline_seconds": sources["timeout_seconds"], "max_attempts": 3,
            "protocol_repairs_consume_attempt": True, "shared_budget_across_repairs": True},
        "acceptance_custody": "Private probes and acceptance store are CONTROL-only; the maintainer and reviewer can see them. Evaluation is not blind to them."}
    if "public_read" in sources:
        packet["contract_version"] = "owner-work-v3"
        packet["public_read"] = read_scope.validate(sources["public_read"], sources["scope"])
    return packet


def prepare(*, candidate_repo, base_sha, objective, scope_contract, acceptance_contract, suite, timeout_seconds=600, public_readonly_paths=()):
    candidate = str(Path(candidate_repo).resolve(strict=True))
    scope.validate(scope_contract); fleet_acceptance.validate(acceptance_contract); checker.validate_suite(suite)
    if type(timeout_seconds) is not int or not 60 <= timeout_seconds <= 3600: raise ValueError("invalid shared deadline")
    work.text(objective, "objective", maximum=16384)
    sources = {"objective": objective, "context": work.context(), "scope": copy.deepcopy(scope_contract),
        "acceptance": copy.deepcopy(acceptance_contract), "functional_tests": fleet_json.canonical_bytes(suite).decode(),
        "functional": {"version": checker.VERSION, "task": suite["task"], "tests": {"sha256": checker.pin(suite)}},
        "instructions": instructions.snapshot(Path(candidate), base_sha), "timeout_seconds": timeout_seconds,
        "permissions": {"cwd": candidate, "mechanism": "owned-executor-v2"},
        "public_read": read_scope.prepare(candidate, scope_contract, public_readonly_paths)}
    sources["compiled_digest"] = _compiled(sources, suite)
    return _prepared(sources)


def _compiled(sources, suite):
    value = {"profile": VERSION, "scope": sources["scope"], "suite": checker.pin(suite)}
    if "public_read" in sources: value["public_read"] = sources["public_read"]
    return work.digest(value)


def _prepared(sources):
    packet = project(sources)
    if len(fleet_json.canonical_bytes({"op":"init", "id":"mini-1", "task":fleet_json.canonical_bytes(packet).decode()})) > 56000:
        raise ValueError("public packet leaves insufficient bounded repair context")
    envelope = {"contract_version": ENVELOPE, "candidate_repo": sources["permissions"]["cwd"],
        "base_sha": sources["instructions"]["base_sha"], "compiled_digest": sources["compiled_digest"],
        "work_packet_sha256": work.digest(packet), "source_pins": {k:work.digest(v) for k,v in sources.items()},
        "profile": "harness_mini_local_v2" if "public_read" in sources else "harness_mini_local_v1",
        "dispatch_enabled": False, "lane": "herdr-owner-local-conformance"}
    return {"sources": copy.deepcopy(sources), "execution_envelope": envelope, "work_packet": packet}


def verify_prepared(value):
    owner.exact(value, {"sources", "execution_envelope", "work_packet"}, "harness preparation")
    s = value["sources"]
    modern = "public_read" in s
    owner.exact(s, {"objective", "context", "scope", "acceptance", "functional_tests", "functional", "instructions",
                   "timeout_seconds", "permissions", "compiled_digest"} | ({"public_read"} if modern else set()), "harness sources")
    scope.validate(s["scope"]); fleet_acceptance.validate(s["acceptance"])
    if type(s["timeout_seconds"]) is not int or not 60<=s["timeout_seconds"]<=3600: raise ValueError("invalid shared deadline")
    work.text(s["objective"],"objective",maximum=16384)
    if s["context"] != work.context(): raise ValueError("unsupported additional harness authority")
    owner.exact(s["permissions"],{"cwd","mechanism"},"harness permissions")
    if s["permissions"]["mechanism"] != ("owned-executor-v2" if modern else "owned-executor-v1") or not Path(s["permissions"]["cwd"]).is_absolute(): raise ValueError("invalid executor binding")
    if modern: read_scope.validate(s["public_read"], s["scope"])
    suite = checker.validate_suite(fleet_json.loads(s["functional_tests"]))
    if s["compiled_digest"] != _compiled(s, suite): raise ValueError("invalid compiled profile identity")
    if s["functional"] != {"version":checker.VERSION, "task":suite["task"], "tests":{"sha256":checker.pin(suite)}}:
        raise ValueError("private suite pin differs")
    if fleet_json.canonical_bytes(value) != fleet_json.canonical_bytes(_prepared(s)):
        raise ValueError("harness projection differs from creation")
    return value


def create(prepared, *, cycle_id, started_at, budget_limits):
    verify_prepared(prepared)
    owner.identity(cycle_id); owner.instant(started_at)
    seconds = prepared["sources"]["timeout_seconds"]
    expected_budget = budget.contract(**{k:v for k,v in budget_limits.items() if k != "version"})
    if (fleet_json.canonical_bytes(expected_budget) != fleet_json.canonical_bytes(budget_limits)
            or budget_limits["cycle_id"] != cycle_id or budget_limits["deadline_at"] != started_at+seconds):
        raise ValueError("request budget must share the original cycle and deadline")
    contract = {"version":VERSION, "profile":{"version":"owner-cycle-profile-v3", "profile_id":prepared["execution_envelope"]["profile"],
        "writer":"worker", "dispatch_enabled":False, "lane":"herdr-owner-local-conformance", "result_protocol":work.RESULT_VERSION},
        "runtime":copy.deepcopy(RUNTIME), "cycle_id":cycle_id, "started_at":started_at, "deadline_at":started_at+seconds,
        "limits":{"max_attempts":3, "deadline_seconds":seconds, "token_limit":None, "cost_limit_usd":None},
        "request_budget":copy.deepcopy(budget_limits), "prepared":copy.deepcopy(prepared)}
    return contract


def validate(value):
    owner.exact(value, {"version", "profile", "runtime", "cycle_id", "started_at", "deadline_at", "limits", "request_budget", "prepared"}, "harness cycle")
    expected = create(value["prepared"], cycle_id=value["cycle_id"], started_at=value["started_at"], budget_limits=value["request_budget"])
    if fleet_json.canonical_bytes(value) != fleet_json.canonical_bytes(expected): raise ValueError("harness creation changed")
    return value
