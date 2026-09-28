"""Stage 1 repair attempts inside one Mission (spec E3, E5-E7, E18).

The Mission ledger is the authority: CONTROL opens and settles each ordinal.
This module evaluates the open attempt against its frozen revision, reuses the
receipt of an identical earlier revision without a second physical run,
derives bounded feedback only from controller evidence, and names the next
step. The driver dispatches the Worker between these steps.
"""
from __future__ import annotations

import re

import fleet_artifacts
import fleet_attempt_loop as attempt_loop
import fleet_functional
import fleet_herdr_repair_policy as repair_policy
import fleet_json
import fleet_mission_state as state

EXHAUSTED = "repair_attempts_exhausted"
FEEDBACK_MAX_BYTES = 8 * 1024
PUBLIC_FEEDBACK_ENTRIES = 10
PUBLIC_FEEDBACK_CHARS = 400
SETTLING = frozenset({"passed", "failed"})


class RepairError(state.MissionStateError):
    pass


def _current(runs, mid):
    import fleet_mission
    return fleet_mission.load_state(runs, mid)


def load_policy(runs, mid, current):
    frozen = current.get("repair_policy")
    if not frozen or current.get("repair_attempts") is None:
        raise RepairError("repair attempts require a frozen repair policy")
    policy = repair_policy.validate(fleet_json.loads(
        fleet_artifacts.get_bytes(runs, mid, frozen["policy_artifact_id"])))
    if repair_policy.digest(policy) != frozen["policy_artifact_id"]:
        raise RepairError("frozen repair policy CAS mismatch")
    return policy


def next_step(runs, mid, current=None):
    """Return ``open``, ``evaluate``, ``accepted`` or ``exhausted``."""
    current = current or _current(runs, mid)
    policy = load_policy(runs, mid, current)
    attempts = current["repair_attempts"]
    if not attempts:
        return "open"
    last = attempts[-1]["settled"]
    if last is None:
        return "evaluate"
    if last["status"] == "passed":
        return "accepted"
    return "open" if attempt_loop.next_ordinal(len(attempts), policy["max_attempts"]) else "exhausted"


def open_attempt(runs, mid):
    current = _current(runs, mid)
    policy = load_policy(runs, mid, current)
    attempts = current["repair_attempts"]
    if attempts and attempts[-1]["settled"] is None:
        raise RepairError("the open repair attempt must settle before another opens")
    if attempts and attempts[-1]["settled"]["status"] != "failed":
        raise RepairError("only a failed repair attempt is followed by another")
    ordinal = attempt_loop.next_ordinal(len(attempts), policy["max_attempts"])
    if ordinal is None:
        raise RepairError("repair attempts exhausted")
    feedback = attempts[-1]["settled"]["feedback_artifact_id"] if attempts else None
    return state.append_event(runs, mid, kind="repair_attempt_opened", actor="CONTROL",
        idempotency_key=f"repair:attempt:{ordinal}:opened",
        payload={"ordinal": ordinal, "feedback_artifact_id": feedback})[0]


def exhaust(runs, mid):
    if next_step(runs, mid) != "exhausted":
        raise RepairError("repair attempts are not exhausted")
    return state.append_terminal(runs, mid, status="failed", reason=EXHAUSTED, idempotency_key="repair:exhausted")


def _public_line(value):
    text = "".join(c if c.isprintable() else " " for c in value).strip()
    return text[:PUBLIC_FEEDBACK_CHARS]


def feedback_from_receipt(receipt, read, *, receipt_artifact_id, unchanged_from=None):
    """Bounded feedback built only from the controller's receipt evidence.

    Assertion text may quote values the candidate returned; the Worker receives
    it as untrusted data, never as instructions.
    """
    evidence = receipt["evidence"]
    output = read(evidence["test-output.txt"]).decode("utf-8", "replace") if "test-output.txt" in evidence else ""
    blocks = [block for block in re.split(r"^=+$", output, flags=re.M) if block.strip()]
    failed, public = [], []
    for block in blocks:
        heading = re.search(r"^(FAIL|ERROR): (test_[A-Za-z0-9_]+)", block, flags=re.M)
        if not heading:
            continue
        failed.append(heading.group(2))
        lines = [line for line in block.splitlines() if line.strip() and not set(line.strip()) <= {"-"}]
        public.append(_public_line(f"{heading.group(2)}: {lines[-1] if lines else heading.group(1)}"))
    if "test-result.json" in evidence:
        result = fleet_json.loads(read(evidence["test-result.json"]))
        public.insert(0, _public_line(f"tests_run={result.get('tests_run')} failures={result.get('failures')} "
                                      f"errors={result.get('errors')}"))
    if not failed:
        public.append(_public_line(f"reason: {receipt['reason']}"))
    feedback = attempt_loop.checks_rejected(receipt_artifact_id, failed_requirements=sorted(set(failed)),
        scope_issues=[], unchanged_from=unchanged_from,
        functional={"status": receipt["status"], "reason": _public_line(receipt["reason"]),
                    "public_feedback": public[:PUBLIC_FEEDBACK_ENTRIES]})
    while len(fleet_json.canonical_bytes(feedback)) > FEEDBACK_MAX_BYTES and feedback["detail"]["functional"]["public_feedback"]:
        feedback["detail"]["functional"]["public_feedback"].pop()
    if len(fleet_json.canonical_bytes(feedback)) > FEEDBACK_MAX_BYTES:
        raise RepairError("repair feedback exceeds its byte limit")
    return feedback


def evaluate(runs, mid, frozen, *, interrupt=None):
    """Evaluate the open attempt; settle it only on a passed or failed check."""
    current, _spec, contract, _identifier = fleet_functional.bind_attempt(runs, mid, frozen)
    attempts = current.get("repair_attempts")
    if not attempts or attempts[-1]["settled"] is not None:
        raise RepairError("functional evaluation requires an open repair attempt")
    ordinal = attempts[-1]["ordinal"]
    read = lambda key: fleet_artifacts.get_bytes(runs, mid, key)
    source = None
    if current.get("functional_attempt") is None:
        source = next((a for a in attempts[:-1] if a["settled"]
                       and a["settled"]["functional_attempt_id"] == contract["attempt_id"]), None)
    if source is not None:
        receipt_id = source["settled"]["receipt_artifact_id"]
        receipt = fleet_functional.verify_receipt(contract, fleet_json.loads(read(receipt_id)), read)
        unchanged = source["ordinal"]
    else:
        receipt = fleet_functional.run(runs, mid, frozen, interrupt=interrupt)
        receipt_id = fleet_artifacts.put_bytes(runs, mid, fleet_json.canonical_bytes(receipt))["artifact_id"]
        unchanged = None
    if receipt["status"] not in SETTLING:
        # Blocked and indeterminate checks need reconciliation, never repair (E5).
        return {"ordinal": ordinal, "status": receipt["status"], "settled": False, "unchanged_from": None}
    feedback_id = None
    if receipt["status"] == "failed":
        feedback = feedback_from_receipt(receipt, read, receipt_artifact_id=receipt_id, unchanged_from=unchanged)
        feedback_id = fleet_artifacts.put_bytes(runs, mid, fleet_json.canonical_bytes(feedback))["artifact_id"]
    state.append_event(runs, mid, kind="repair_attempt_settled", actor="CONTROL",
        idempotency_key=f"repair:attempt:{ordinal}:settled",
        payload={"ordinal": ordinal, "tree_sha": frozen["tree_sha"], "functional_attempt_id": contract["attempt_id"],
                 "receipt_artifact_id": receipt_id, "status": receipt["status"], "unchanged_from": unchanged,
                 "feedback_artifact_id": feedback_id})
    return {"ordinal": ordinal, "status": receipt["status"], "settled": True, "unchanged_from": unchanged,
            "feedback_artifact_id": feedback_id}
