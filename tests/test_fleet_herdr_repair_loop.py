"""Stage 1 S4: the repair loop of the MINIMAL driver, offline and provider-free."""
from __future__ import annotations

import json
from unittest import mock

from tests import test_fleet_herdr_research as research_fixtures
from tests.test_fleet_functional import synthetic_spec
from tests.test_fleet_herdr_orchestration_closure import MinimalBackend, MinimalFixture
from tests.test_fleet_herdr_repair_attempts import failed_outcome
from tests.test_fleet_herdr_scope import contract as scope_contract

import fleet_acceptance
import fleet_functional
import fleet_herdr_control
import fleet_herdr_mission
import fleet_herdr_repair
import fleet_herdr_repair_policy
import fleet_mission
import fleet_mission_state as state


class RepairLoopTests(MinimalFixture):
    def setUp(self):
        super().setUp()
        self.outcomes, self.physical_runs = [], 0
        self.contents = {}
        self.crash_on_attempt = None
        patcher = mock.patch.object(fleet_functional.runner, "execute", side_effect=self.execute)
        patcher.start()
        self.addCleanup(patcher.stop)
        original = MinimalBackend.submit
        test = self

        def submit(backend, run_id, prompt, *, instance_id):
            attempt = json.loads(prompt).get("repair", {}).get("attempt", 1)
            if attempt in test.contents:
                # A separate editable file varies the revision without touching
                # the result artifact the fake backend hashes.
                (backend.repo / "notes.txt").write_text(test.contents[attempt])
            observation = original(backend, run_id, prompt, instance_id=instance_id)
            if test.crash_on_attempt == attempt:
                test.crash_on_attempt = None
                raise RuntimeError("process lost after submit")
            return observation

        patcher = mock.patch.object(MinimalBackend, "submit", submit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def execute(self, spec, _tree, _tests, attempt, **_kwargs):
        self.physical_runs += 1
        outcome = self.outcomes.pop(0)
        return research_fixtures.functional_outcome(spec, attempt, self.target) if outcome == "passed" else outcome

    def create_repair(self, *, max_attempts=3, key="repair-loop", policy=True):
        scope = scope_contract()
        scope["editable_paths"].append("notes.txt")
        options = {**self.options(), "functional_contract": synthetic_spec(), "scope_contract": scope}
        if policy:
            options["repair_policy"] = {"schema": fleet_herdr_repair_policy.SCHEMA, "max_attempts": max_attempts,
                                        "closure_policy": "automatic", "delivery_root": str(self.root / "deliveries")}
        return fleet_mission.create_mission(self.runs, compiled=self.compiled, feature="repair-loop",
            objective="Implement sample_stats.stats", target_repo=self.target, base_sha=self.head,
            idempotency_key=fleet_acceptance.bound_key(key, self.contract), runtime_options=options)[0]

    def submits(self):
        return [call for call in MinimalBackend.calls if call[0] == "submit"]

    def attempts(self, mid):
        return fleet_mission.load_state(self.runs, mid)["repair_attempts"]

    def test_without_policy_a_failed_check_still_ends_the_mission(self):
        mid = self.create_repair(policy=False, key="no-policy")
        self.outcomes = [failed_outcome()]
        result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fleet_mission.load_state(self.runs, mid)["terminal"]["reason"], "required functional tests failed")
        self.assertEqual(len(self.submits()), 1)
        self.assertNotIn("repair_attempts", fleet_mission.load_state(self.runs, mid))

    def test_failed_check_repairs_in_the_same_mission_until_accepted(self):
        mid = self.create_repair()
        deadline = fleet_mission.load_state(self.runs, mid)["admission_policy"]["deadline_at"]
        self.outcomes, self.contents = [failed_outcome(), "passed"], {2: "implemented v2\n"}
        result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertEqual(result["status"], "running")
        self.assertIn("repair attempt accepted", result["next_action"])
        self.assertEqual([a["status"] for a in result["repair"]], ["failed", "passed"])
        submits = self.submits()
        self.assertEqual([call[1] for call in submits], ["build", "build"])
        self.assertEqual(len({call[2] for call in submits}), 2)
        second = MinimalBackend.tasks[submits[1][2]]
        attempts = self.attempts(mid)
        self.assertEqual(second["repair"]["attempt"], 2)
        self.assertEqual(second["repair"]["feedback_artifact_id"], attempts[1]["feedback_artifact_id"])
        self.assertIn(attempts[1]["feedback_artifact_id"], second["input_artifact_ids"])
        self.assertTrue(second["writer"])
        current = fleet_mission.load_state(self.runs, mid)
        self.assertEqual(sorted(a["request_key"] for a in current["admissions"].values()),
                         ["herdr:build", "herdr:build-repair-2"])
        self.assertEqual(current["admission_policy"]["deadline_at"], deadline)
        self.assertEqual(self.physical_runs, 2)

    def test_repeated_failures_exhaust_without_a_fourth_turn(self):
        mid = self.create_repair(max_attempts=3)
        self.outcomes = [failed_outcome()] * 3
        self.contents = {2: "implemented v2\n", 3: "implemented v3\n"}
        result = fleet_herdr_mission.drive(self.runs, mid)
        current = fleet_mission.load_state(self.runs, mid)
        self.assertEqual((result["status"], current["terminal"]["reason"]), ("failed", fleet_herdr_repair.EXHAUSTED))
        self.assertEqual(len(self.submits()), 3)
        self.assertEqual(self.physical_runs, 3)
        self.assertEqual([a["settled"]["status"] for a in current["repair_attempts"]], ["failed"] * 3)

    def test_unchanged_revisions_consume_attempts_with_one_physical_check(self):
        mid = self.create_repair(max_attempts=3)
        self.outcomes = [failed_outcome()]
        result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.submits()), 3)
        self.assertEqual(self.physical_runs, 1)
        self.assertEqual([a["settled"]["unchanged_from"] for a in self.attempts(mid)], [None, 1, 1])

    def test_deadline_after_a_failed_attempt_opens_no_new_attempt(self):
        mid = self.create_repair()
        self.outcomes = [failed_outcome()]
        original = fleet_herdr_mission._Driver.remaining_seconds

        def remaining(driver):
            attempts = driver.current().get("repair_attempts") or []
            return 0 if attempts and attempts[-1]["settled"] else original(driver)

        with mock.patch.object(fleet_herdr_mission._Driver, "remaining_seconds", remaining):
            result = fleet_herdr_mission.drive(self.runs, mid)
        current = fleet_mission.load_state(self.runs, mid)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(current["terminal"]["reason"], "Herdr mission deadline expired")
        self.assertEqual(len(current["repair_attempts"]), 1)
        self.assertEqual(len(self.submits()), 1)

    def test_cancel_request_between_attempts_confirms_without_opening_another(self):
        mid = self.create_repair()
        self.outcomes = [failed_outcome()]
        evaluate = fleet_herdr_repair.evaluate

        def evaluate_then_cancel(runs, mission_id, frozen, **kwargs):
            outcome = evaluate(runs, mission_id, frozen, **kwargs)
            fleet_herdr_control.request(runs, mission_id, action="cancel", reason="operator stop", idempotency_key="stop")
            return outcome

        with mock.patch.object(fleet_herdr_repair, "evaluate", evaluate_then_cancel):
            result = fleet_herdr_mission.drive(self.runs, mid)
        current = fleet_mission.load_state(self.runs, mid)
        self.assertEqual((result["status"], current["status"]), ("abandoned", "abandoned"))
        self.assertEqual(len(current["repair_attempts"]), 1)
        self.assertEqual(len(self.submits()), 1)
        self.assertEqual(fleet_herdr_control.view(current)["applied"], "cancelled")

    def test_restart_after_a_lost_check_does_not_replay_it(self):
        mid = self.create_repair()
        self.outcomes = [failed_outcome()]
        self.contents = {2: "implemented v2\n"}

        def lost(*_args, **_kwargs):
            raise KeyboardInterrupt("controller lost during the functional check")

        run = fleet_functional.runner.execute
        with mock.patch.object(fleet_functional.runner, "execute",
                               side_effect=lambda *a, **k: self.execute(*a, **k) if self.physical_runs == 0 else lost()):
            with self.assertRaises(KeyboardInterrupt):
                fleet_herdr_mission.drive(self.runs, mid)
        self.assertIs(fleet_functional.runner.execute, run)
        with mock.patch.object(fleet_functional.runner, "Docker", side_effect=fleet_functional.runner.RunnerBlocked("no docker")):
            result = fleet_herdr_mission.drive(self.runs, mid)
        attempts = self.attempts(mid)
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(attempts[1]["settled"])
        self.assertIn("reconcile without automatic replay", result["next_action"])
        self.assertEqual(self.physical_runs, 1)
        self.assertEqual(len(self.submits()), 2)

    def test_ambiguous_repair_send_is_reconciled_not_resubmitted(self):
        mid = self.create_repair()
        self.outcomes, self.contents, self.crash_on_attempt = [failed_outcome(), "passed"], {2: "implemented v2\n"}, 2
        with self.assertRaises(RuntimeError):
            fleet_herdr_mission.drive(self.runs, mid)
        result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertIn("repair attempt accepted", result["next_action"])
        self.assertEqual(len(self.submits()), 2)

    def test_offline_loop_uses_only_the_simulated_backend_and_runner(self):
        mid = self.create_repair()
        self.outcomes, self.contents = [failed_outcome(), "passed"], {2: "implemented v2\n"}
        with mock.patch.object(fleet_functional.runner, "Docker", side_effect=AssertionError("no Docker")), \
             mock.patch.object(fleet_herdr_mission.fleet_herdr, "HerdrBackend", MinimalBackend):
            result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertIn("repair attempt accepted", result["next_action"])
        self.assertEqual({call[0] for call in MinimalBackend.calls} - {"boot", "submit", "collect", "wait", "recover"},
                         set())
        self.assertEqual((self.physical_runs, self.outcomes), (2, []))

    def test_repair_policy_buys_one_credit_per_attempt(self):
        mid = self.create_repair(max_attempts=3)
        self.assertEqual(fleet_mission.load_state(self.runs, mid)["admission_policy"]["delegation_credits"], 3)
        plain = self.create_repair(policy=False, key="plain-credits")
        self.assertEqual(fleet_mission.load_state(self.runs, plain)["admission_policy"]["delegation_credits"], 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
