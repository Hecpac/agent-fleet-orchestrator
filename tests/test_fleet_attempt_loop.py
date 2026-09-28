from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_attempt_loop as loop
import fleet_herdr_owner_cycle as cycle
import fleet_json


def legacy_feedback(pin, checks):
    """The inline Owner Cycle shape before S2; kept to pin byte equivalence."""
    return {"reason": "checks_rejected", "detail": {"checks_sha256": pin,
        "failed_requirements": [r["id"] for r in checks["artifacts"]["requirements"] if not r["passed"]],
        "scope_issues": checks["scope"]["issues"][:10],
        "scope_issue_count": len(checks["scope"]["issues"]),
        **({"functional": {k: checks["functional"][k] for k in ("status", "reason", "public_feedback", "revision_sha256") if k in checks["functional"]}}
           if checks.get("functional") is not None else {})}}


class AttemptAccountingTests(unittest.TestCase):
    def test_initial_attempt_counts_against_the_limit(self):
        self.assertEqual(loop.next_ordinal(0, 3), 1)
        self.assertEqual(loop.next_ordinal(2, 3), 3)
        self.assertIsNone(loop.next_ordinal(3, 3))
        self.assertFalse(loop.attempts_exhausted(2, 3))
        self.assertTrue(loop.attempts_exhausted(3, 3))
        self.assertIsNone(loop.next_ordinal(1, 1))

    def test_reasons_are_the_closed_owner_set(self):
        self.assertEqual(loop.FEEDBACK_REASONS, {"invalid_delivery", "decision_resolved", "checks_rejected"})


class FeedbackTests(unittest.TestCase):
    def checks(self, *, issues=12, functional=True):
        value = {"artifacts": {"requirements": [{"id": "a", "passed": False}, {"id": "b", "passed": True},
                                                {"id": "c", "passed": False}]},
                 "scope": {"issues": [{"path": f"f{i}"} for i in range(issues)]}}
        if functional:
            value["functional"] = {"status": "failed", "reason": "tests", "public_feedback": ["x"],
                                   "revision_sha256": "0" * 64, "private": "never forwarded"}
        return value

    def test_owner_cycle_feedback_is_byte_identical_to_the_previous_shape(self):
        for issues in (0, 3, 12):
            for functional in (True, False):
                checks = self.checks(issues=issues, functional=functional)
                with self.subTest(issues=issues, functional=functional):
                    self.assertEqual(fleet_json.canonical_bytes(cycle.Cycle._repair_feedback("1" * 64, checks)),
                                     fleet_json.canonical_bytes(legacy_feedback("1" * 64, checks)))

    def test_feedback_is_bounded_and_drops_unlisted_functional_fields(self):
        feedback = loop.checks_rejected("1" * 64, failed_requirements=["a"],
            scope_issues=[{"path": str(i)} for i in range(25)],
            functional=loop.functional_feedback({"status": "failed", "private": "x"}))
        self.assertEqual(len(feedback["detail"]["scope_issues"]), loop.SCOPE_ISSUE_LIMIT)
        self.assertEqual(feedback["detail"]["scope_issue_count"], 25)
        self.assertEqual(feedback["detail"]["functional"], {"status": "failed"})
        self.assertNotIn("functional", loop.checks_rejected("1" * 64, failed_requirements=[], scope_issues=[])["detail"])


if __name__ == "__main__":
    unittest.main()
