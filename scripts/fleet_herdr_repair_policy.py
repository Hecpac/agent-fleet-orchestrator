"""Stage 1 repair policy: validated, admitted and frozen before a Mission launches.

The policy is an explicit opt-in (spec E2). It is bound to the Mission at
creation by its digest and frozen in CAS by CONTROL; resuming never rereads it
from the caller. Admission refuses combinations the repair loop cannot honour
before any candidate, session or agent exists.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

import fleet_artifacts
import fleet_json
import fleet_mission_state as state

SCHEMA = "fleet.repair-policy.v1"
FIELD = "repair_policy_sha256"
EVENT = "repair_policy_frozen"
FIELDS = frozenset({"schema", "max_attempts", "closure_policy", "delivery_root"})
# Same ceiling as Owner Cycle contract limits; a Mission sets its own value.
MAX_ATTEMPTS = 20
CLOSURE_POLICIES = frozenset({"automatic"})


class RepairPolicyError(state.MissionStateError):
    pass


def validate(policy):
    if not isinstance(policy, dict) or set(policy) != FIELDS or policy["schema"] != SCHEMA:
        raise RepairPolicyError("unsupported repair policy")
    if type(policy["max_attempts"]) is not int or not 1 <= policy["max_attempts"] <= MAX_ATTEMPTS:
        raise RepairPolicyError("repair max_attempts must be an integer from 1 to 20")
    if not isinstance(policy["closure_policy"], str) or policy["closure_policy"] not in CLOSURE_POLICIES:
        raise RepairPolicyError("unsupported repair closure policy")
    root = policy["delivery_root"]
    if (not isinstance(root, str) or not 1 < len(root) <= 4096
            or any(ord(c) < 32 for c in root) or "\\" in root):
        raise RepairPolicyError("delivery root must be a bounded absolute path")
    canonical = PurePosixPath(root)
    if not canonical.is_absolute() or str(canonical) != root or ".." in canonical.parts:
        raise RepairPolicyError("delivery root must be a canonical absolute path")
    return policy


def digest(policy):
    return fleet_json.sha256(validate(policy))


def load(filename):
    with Path(filename).open("rb") as stream:
        raw = stream.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise RepairPolicyError("repair policy exceeds size limit")
    return validate(fleet_json.loads(raw))


def admit(policy, *, compiled, runs_dir, functional_contract, scope_contract):
    """Refuse, before any effect, a policy the stage 1 loop cannot honour."""
    import fleet_herdr_profile

    validate(policy)
    preset = fleet_herdr_profile.MINIMAL.preset
    if compiled["resolved"]["preset"] != preset:
        raise RepairPolicyError(f"repair policy requires {preset}")
    if functional_contract is None:
        raise RepairPolicyError("repair policy requires a functional contract")
    if scope_contract is None:
        raise RepairPolicyError("repair policy requires a physical scope contract")
    if compiled["workflow"]["limits"]["token_budget"] != 0:
        raise RepairPolicyError("repair policy cannot promise an unenforceable token budget")
    root = Path(policy["delivery_root"]).resolve(strict=False)
    runs = Path(runs_dir).resolve(strict=False)
    if root == runs or runs in root.parents or root in runs.parents:
        raise RepairPolicyError("delivery root must lie outside the runs directory")
    return policy


def validate_binding(current, options):
    """The runtime options must carry exactly the policy bound and frozen at creation."""
    policy = options.get("repair_policy")
    pin = digest(policy) if policy is not None else None
    if current.get(FIELD) != pin:
        raise RepairPolicyError("repair policy differs from creation ledger")
    if pin is not None and (current.get("repair_policy") or {}).get("policy_artifact_id") != pin:
        raise RepairPolicyError("repair policy is not frozen as bound at creation")
    return policy


def freeze_policy(runs, mid, compiled_digest, policy):
    stored = fleet_artifacts.put_bytes(runs, mid, fleet_json.canonical_bytes(validate(policy)))
    return state.append_event(runs, mid, kind=EVENT, actor="CONTROL", idempotency_key="repair:policy",
        payload={"compiled_digest": compiled_digest, "policy_artifact_id": stored["artifact_id"]})[0]


def preview(policy):
    policy = validate(policy)
    return {"schema": SCHEMA, "policy_sha256": digest(policy), "max_attempts": policy["max_attempts"],
            "closure_policy": policy["closure_policy"], "delivery_root": policy["delivery_root"],
            "execution": "not_available"}
