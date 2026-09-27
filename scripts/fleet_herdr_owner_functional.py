"""Pure revision-bound adapter for the existing canonical functional protocol.

No Docker/runner launch. The offline backend supplies explicitly synthetic
observations. Native receipt integrity and replaying the retained RPC answers
remain different from proving a real container executed the candidate.
"""
from __future__ import annotations

import uuid

import fleet_functional as functional
import fleet_functional_runner as runner
import fleet_herdr_owner_contract as contracts
import fleet_herdr_work_packet as work
import fleet_json


def binding(contract, revision_pin, revision):
    if contract["version"] == "owner-cycle-contract-v3":
        from fleet_harness_functional import binding as harness_binding
        return harness_binding(contract, revision_pin, revision)
    spec = contract["prepared"]["sources"]["functional"]
    if spec is None:
        raise contracts.ContractError("no configured functional check")
    # A check namespace is not a Mission admission. It prevents reusing native
    # receipts between two revisions that happen to contain identical trees.
    namespace = str(uuid.uuid5(uuid.UUID(contract["cycle_id"]), "owner-functional:" + contracts.pin(revision_pin)))
    frozen = {"candidate_repo": contract["prepared"]["execution_envelope"]["candidate_repo"],
              "tree_sha": revision["tree_sha"], "tree_artifact_id": revision["tree"]}
    native = functional.contract_for(namespace, frozen, spec)
    return {"version": "owner-functional-binding-v1", "cycle_id": contract["cycle_id"],
            "revision_sha256": revision_pin, "admission_sha256": revision["admission_sha256"],
            "check_namespace": namespace, "native_contract": native}


def receipt(bound, outcome, put):
    if bound["version"] == "owner-harness-functional-v1":
        from fleet_harness_functional import receipt as harness_receipt
        return harness_receipt(bound, outcome, put)
    contracts.exact(outcome, {"status", "reason", "evidence"}, "functional outcome")
    if not isinstance(outcome["evidence"], dict):
        raise contracts.ContractError("functional evidence must be a mapping of original bytes")
    pins = {}
    for name, raw in outcome["evidence"].items():
        if not isinstance(name, str) or not isinstance(raw, bytes):
            raise contracts.ContractError("functional evidence lacks original bytes")
        pins[name] = put(raw)
    native = bound["native_contract"]
    return {"schema_version": 1, "scope": "fleet_functional_execution_v1",
            "contract_sha256": work.digest(native),
            **{k: native[k] for k in ("mission_id", "tree_sha", "tests_sha256", "environment_sha256", "attempt_id")},
            "status": outcome["status"], "reason": outcome["reason"], "evidence": pins}


def verify(bound, observed, read, tests):
    if bound["version"] == "owner-harness-functional-v1":
        from fleet_harness_functional import verify as harness_verify
        try:return harness_verify(bound, observed, read, tests)
        except (AttributeError,IndexError) as exc:raise contracts.ContractError("malformed harness runtime evidence") from exc
    try:
        result = functional.verify_receipt(bound["native_contract"], observed, read)
    except AttributeError as exc:
        # The legacy reader assumes Docker-shaped mappings in nested evidence.
        # A syntactically valid JSON scalar/list is an invalid retained receipt,
        # not a controller crash that should repeat forever during recovery.
        raise contracts.ContractError("malformed functional runtime evidence") from exc
    if result["status"] == "passed":
        rows = read(result["evidence"]["stdout.txt"]).splitlines()
        response = fleet_json.loads(rows[1])
        contracts.exact(response, {"kind", "answers"}, "functional RPC")
        if response["kind"] != "answers":
            raise contracts.ContractError("functional RPC response kind mismatch")
        # Re-evaluate only the pinned trusted oracle on retained answers, never
        # import or execute the candidate. No Docker is needed for this check.
        replay, _ = runner.evaluate_original(tests, response["answers"])
        claimed = fleet_json.loads(read(result["evidence"]["test-result.json"]))
        if not replay["passed"] or fleet_json.canonical_bytes(replay) != fleet_json.canonical_bytes(claimed):
            raise contracts.ContractError("functional PASS does not reproduce from retained RPC answers")
    return result


def resource(bound):
    if bound["version"] == "owner-harness-functional-v1":
        from fleet_harness_functional import resource as harness_resource
        return harness_resource(bound)
    native = bound["native_contract"]
    return {"cycle_id": bound["cycle_id"], "revision_sha256": bound["revision_sha256"],
            "binding_sha256": work.digest(bound), "attempt_id": native["attempt_id"],
            "docker_endpoint_sha256": native["environment"]["runtime"]["docker_endpoint_sha256"]}


def verify_quiescence(observation, bound):
    contracts.exact(observation, {"resource", "inactive", "resources_clean"}, "functional quiescence")
    if (observation["resource"] != resource(bound) or observation["inactive"] is not True
            or observation["resources_clean"] is not True):
        raise contracts.ContractError("functional resource cleanup is not confirmed")
    return observation
