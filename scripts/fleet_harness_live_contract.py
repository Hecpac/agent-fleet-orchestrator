"""New Herdr CONTROL profile; no reinterpretation of local/historical v3."""
import copy

import fleet_harness_contract as local
import fleet_harness_live_budget as budget
import fleet_herdr_owner_contract as owner
import fleet_herdr_work_packet as work
import fleet_json

VERSION="owner-cycle-contract-v4"
ENVELOPE="owner-harness-envelope-v2"
PROFILE="harness_mini_herdr_v1"


def _prepared(sources):
    result=local._prepared(sources)
    result["execution_envelope"].update(contract_version=ENVELOPE,profile=PROFILE,dispatch_enabled=True,lane="herdr-control-owned-v1")
    return result


def prepare(**kwargs):
    prepared=local.prepare(**kwargs)
    sources=prepared["sources"]
    sources["compiled_digest"]=work.digest({"profile":VERSION,"local_contract":sources["compiled_digest"]})
    return _prepared(sources)


def verify_prepared(value):
    owner.exact(value,{"sources","execution_envelope","work_packet"},"CONTROL preparation")
    sources=copy.deepcopy(value["sources"])
    local_digest=local._compiled(sources,fleet_json.loads(sources["functional_tests"]))
    if "public_read" not in sources or sources["compiled_digest"]!=work.digest({"profile":VERSION,"local_contract":local_digest}):
        raise ValueError("CONTROL preparation has invalid source/profile identity")
    sources["compiled_digest"]=local_digest
    local.verify_prepared(local._prepared(sources))
    if work.digest(value)!=work.digest(_prepared(value["sources"])):raise ValueError("CONTROL projection differs")
    return value


def create(prepared, *, cycle_id, started_at, budget_limits):
    verify_prepared(prepared);owner.identity(cycle_id);owner.instant(started_at);budget.validate(budget_limits)
    seconds=prepared["sources"]["timeout_seconds"]
    financial=budget_limits["financial"]
    if financial["cycle_id"]!=cycle_id or financial["deadline_at"]!=started_at+seconds:
        raise ValueError("CONTROL cycle and ledger do not share original deadline")
    return {"version":VERSION,"profile":{"version":"owner-cycle-profile-v4","profile_id":PROFILE,"writer":"worker",
        "dispatch_enabled":True,"lane":"herdr-control-owned-v1","result_protocol":work.RESULT_VERSION},
        "runtime":copy.deepcopy(local.RUNTIME),"cycle_id":cycle_id,"started_at":started_at,"deadline_at":started_at+seconds,
        "limits":{"max_attempts":3,"deadline_seconds":seconds,"token_limit":None,"cost_limit_usd":None},
        "request_budget":copy.deepcopy(budget_limits),"prepared":copy.deepcopy(prepared)}


def validate(value):
    owner.exact(value,{"version","profile","runtime","cycle_id","started_at","deadline_at","limits","request_budget","prepared"},"CONTROL cycle")
    expected=create(value["prepared"],cycle_id=value["cycle_id"],started_at=value["started_at"],budget_limits=value["request_budget"])
    if work.digest(value)!=work.digest(expected):raise ValueError("CONTROL creation changed")
    return value
