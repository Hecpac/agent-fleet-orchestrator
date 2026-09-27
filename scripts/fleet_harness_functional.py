"""D1/D2 functional receipts bound to immutable owner revisions and CAS bytes."""
import io
import base64
from pathlib import Path
import tarfile
import uuid

import fleet_harness_acceptance as checker
import fleet_herdr_owner_contract as contracts
import fleet_herdr_work_packet as work
import fleet_json

VERSION = "owner-harness-functional-v1"


def binding(contract, revision_pin, revision):
    return {"version":VERSION, "cycle_id":contract["cycle_id"], "revision_sha256":revision_pin,
        "admission_sha256":revision["admission_sha256"], "tree_sha":revision["tree_sha"], "tree":revision["tree"],
        "check_namespace":str(uuid.uuid5(uuid.UUID(contract["cycle_id"]), "harness-check:"+revision_pin)),
        "suite_sha256":contract["prepared"]["sources"]["functional"]["tests"]["sha256"],
        "deadline_at":contract["deadline_at"], "checker_version":checker.VERSION}


def resource(bound):
    return {"cycle_id":bound["cycle_id"], "revision_sha256":bound["revision_sha256"],
            "binding_sha256":work.digest(bound), "check_namespace":bound["check_namespace"]}


def receipt(bound, outcome, put):
    contracts.exact(outcome, {"status", "reason", "evidence"}, "harness check outcome")
    if not isinstance(outcome["evidence"], dict) or not outcome["evidence"]: raise ValueError("missing originals")
    pins = {}
    for name, raw in outcome["evidence"].items():
        from fleet_herdr_scope import path
        path(name)
        if not isinstance(raw, bytes): raise ValueError("check originals must be bytes")
        pins[name] = put(raw)
    return {"version":VERSION, "binding_sha256":work.digest(bound), "status":outcome["status"],
            "reason":outcome["reason"], "evidence":pins}


def verify(bound, observed, read, tests):
    contracts.exact(observed, {"version", "binding_sha256", "status", "reason", "evidence"}, "harness functional receipt")
    if (observed["version"] != VERSION or observed["binding_sha256"] != work.digest(bound)
            or checker.digest(tests) != bound["suite_sha256"]): raise ValueError("foreign functional receipt")
    originals = {name:read(pin) for name,pin in observed["evidence"].items()}
    contract = fleet_json.loads(originals["contract.json"])
    if originals["suite.json"] != tests: raise ValueError("private suite differs from creation")
    result = checker.verify(contract["evidence_root"], expected_binding=bound, originals=originals)
    if result["cleanup"] is True:
        from fleet_harness_resource_evidence import verify_abandoned
        verify_abandoned(originals)
    records=fleet_json.loads(originals["recovery.json"])
    if not isinstance(records,list) or len(records)>2 or work.digest(records)!=contract["recovery_sha256"]:
        raise ValueError("invalid verifier recovery history")
    expected_root=Path(contract["evidence_root"]).parent/"check"
    for index,record in enumerate(records,1):
            if record["binding_sha256"]!=work.digest(bound):raise ValueError("recovery belongs to another frozen revision")
            prior={n:base64.b64decode(raw,validate=True) for n,raw in record["originals"].items()}
            verify_recovery_original(bound,prior)
            old=fleet_json.loads(prior["contract.json"])
            checks={"origin":old["evidence_root"]==record["previous_root"],"sources":old["sources"]==contract["sources"],
                "suite":old["suite_sha256"]==contract["suite_sha256"],"order":record["previous_root"]==str(expected_root),
                "prefix":old["recovery_sha256"]==work.digest(records[:index-1])}
            if not all(checks.values()):raise ValueError("recovered check differs: "+",".join(k for k,v in checks.items() if not v))
            expected_root=expected_root.parent/f"check-recovery-{index:04d}"
    if str(expected_root)!=contract["evidence_root"]:raise ValueError("verifier generation omitted recovery history")
    # Prove the checked code came from this exact frozen tree, not the current
    # worktree or another revision that happens to have the same result.
    with tarfile.open(fileobj=io.BytesIO(read(bound["tree"])), mode="r:") as archive:
        for name, sha in contract["sources"].items():
            member = archive.getmember(name)
            if not member.isfile() or member.size > 4*1024*1024: raise ValueError("invalid frozen source")
            raw = archive.extractfile(member).read()
            if checker.digest(raw) != sha or raw != originals["sources/"+name]: raise ValueError("checked source differs from revision")
    status=reviewed_status(bound,result,contract,originals)
    if status != observed["status"]: raise ValueError("functional outcome was relabelled")
    return {"status":status, "reason":"versioned_contract_probes", "revision_sha256":bound["revision_sha256"],
            "public_feedback":result["public_feedback"], "private_cases_disclosed":False,
            "evidence":observed["evidence"], "coverage":result["coverage"]}


def verify_recovery_original(bound,originals):
    original=fleet_json.loads(originals["contract.json"])
    if work.digest(original["binding"])!=work.digest(bound) or "result.json" in originals:
        raise ValueError("completed or foreign check must not be restarted")
    value=fleet_json.loads(originals["suite.json"])
    if checker.pin(value)!=original["suite_sha256"]:raise ValueError("abandoned suite differs")
    _,results=checker.verify_interrupted(originals,binding=bound)
    if any(r["passed"] is False for r in results):raise ValueError("observed counterexample vetoes another PASS for the same tree")
    if "resource/resource.json" in originals:
        from fleet_harness_resource_evidence import verify_abandoned
        record=verify_abandoned(originals)
        if record["candidate"]!=str(Path(original["evidence_root"])/"sources"):
            raise ValueError("previous verifier resource belongs to another check")
    elif any(n.startswith(("resource/","observations/")) or n=="observations.json" for n in originals):
        raise ValueError("previous verifier omitted its resource intent")


def reviewed_status(bound,result,contract,originals):
    if "source-review.json" not in originals:return result["status"]
    review=fleet_json.loads(originals["source-review.json"])
    contracts.exact(review,{"version","binding_sha256","original_contract_sha256","review"},"independent source supplement")
    if (review["version"]!="source-review-supplement-v1" or review["binding_sha256"]!=work.digest(bound)
            or review["original_contract_sha256"]!=work.digest(contract)):
        raise ValueError("source supplement belongs to another frozen check")
    checker.validate_source_admission(review["review"],contract["sources"])
    if (result["status"]=="blocked" and result["pending"]==["independent_source_admission_required"]
            and result["results"] and all(r["passed"] is True for r in result["results"]) and result["cleanup"] is True):
        from fleet_harness_resource_evidence import verify as verify_resource
        # The original checker was blocked, so its PASS-only guards were not
        # applicable then. A supplement must establish them from originals now.
        record,clean,_=verify_resource(originals)
        if (record["candidate"]!=str(Path(contract["evidence_root"])/"sources")
                or record["image"]!=contract["image"] or clean["extra_stdout"] or not clean["bounded_output"]):
            raise ValueError("source supplement lacks original PASS resource proofs")
        return "passed"
    return result["status"]


def collect(store):
    """Only verifier originals; transient scratch and candidate workspace excluded."""
    store = Path(store)
    result = {}
    for root in (store, store/"sources", store/"observations", store/"native", store/"bridge", store/"resource", store/"resource/rpc",
                 store/"resource/rpc-intents", store/"resource/snapshots"):
        if not root.exists(): continue
        for item in sorted(root.iterdir()):
            if item.is_symlink(): raise ValueError("verifier original symlink")
            if item.is_file() and not item.name.startswith("."):
                result[str(item.relative_to(store))] = item.read_bytes()
    return result
