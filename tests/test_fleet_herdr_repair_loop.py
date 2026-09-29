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
import fleet_artifacts
import fleet_functional
import fleet_herdr_delivery
import fleet_herdr_control
import fleet_herdr_mission
import fleet_herdr_repair
import fleet_herdr_repair_policy
import fleet_mission
import fleet_mission_state as state


class RepairFixture(MinimalFixture):
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


class RepairLoopTests(RepairFixture):
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
        self.assertIn("delivered; archive and closure pending", result["next_action"])
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
        self.assertIn("delivered; archive and closure pending", result["next_action"])
        self.assertEqual(len(self.submits()), 2)

    def test_offline_loop_uses_only_the_simulated_backend_and_runner(self):
        mid = self.create_repair()
        self.outcomes, self.contents = [failed_outcome(), "passed"], {2: "implemented v2\n"}
        with mock.patch.object(fleet_functional.runner, "Docker", side_effect=AssertionError("no Docker")), \
             mock.patch.object(fleet_herdr_mission.fleet_herdr, "HerdrBackend", MinimalBackend):
            result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertIn("delivered; archive and closure pending", result["next_action"])
        self.assertEqual({call[0] for call in MinimalBackend.calls} - {"boot", "submit", "collect", "wait", "recover"},
                         set())
        self.assertEqual((self.physical_runs, self.outcomes), (2, []))

    def test_repair_policy_buys_one_credit_per_attempt(self):
        mid = self.create_repair(max_attempts=3)
        self.assertEqual(fleet_mission.load_state(self.runs, mid)["admission_policy"]["delegation_credits"], 3)
        plain = self.create_repair(policy=False, key="plain-credits")
        self.assertEqual(fleet_mission.load_state(self.runs, plain)["admission_policy"]["delegation_credits"], 1)


class RepairDeliveryTests(RepairFixture):
    """Stage 1 S6: delivery of the accepted revision and the P0-P4 matrix of §3.4."""

    def accepted_mission(self, key="delivery"):
        mid = self.create_repair(key=key)
        self.outcomes, self.contents = [failed_outcome(), "passed"], {2: "implemented v2\n"}
        return mid

    def final(self, mid):
        current = fleet_mission.load_state(self.runs, mid)
        accepted = current["repair_attempts"][-1]["settled"]
        return fleet_herdr_delivery.final_path(self.root / "deliveries", mid, accepted["ordinal"], accepted["tree_sha"])

    def terminal(self, mid):
        return fleet_mission.load_state(self.runs, mid).get("terminal")

    def test_accepted_revision_is_delivered_and_read_back(self):
        mid = self.accepted_mission()
        result = fleet_herdr_mission.drive(self.runs, mid)
        final = self.final(mid)
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["delivery"], [{"started": {"ordinal": 2, "tree_sha": final.name.split("-", 1)[1]},
                                               "status": "delivered"}])
        self.assertEqual((final / "notes.txt").read_text(), "implemented v2\n")
        self.assertEqual((final / "answer.txt").read_text(), "implemented\n")
        self.assertIsNone(self.terminal(mid))
        self.assertEqual(sorted(p.name for p in final.parent.iterdir()), [final.name])

    def test_collision_blocks_only_the_delivery_and_keeps_the_result(self):
        mid = self.accepted_mission("collision")
        original = fleet_herdr_delivery.deliver

        def foreign_first(root, mission_id, ordinal, tree_sha, tree, **kwargs):
            final = fleet_herdr_delivery.final_path(root, mission_id, ordinal, tree_sha)
            final.mkdir(parents=True)
            (final / "foreign.txt").write_text("keep\n")
            return original(root, mission_id, ordinal, tree_sha, tree, **kwargs)

        with mock.patch.object(fleet_herdr_delivery, "deliver", foreign_first):
            result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.terminal(mid)["reason"], "delivery_collision")
        self.assertEqual([p.name for p in self.final(mid).iterdir()], ["foreign.txt"])
        self.assertEqual(fleet_mission.load_state(self.runs, mid)["repair_attempts"][-1]["settled"]["status"], "passed")

    def crash_after(self, publish):
        original = fleet_herdr_delivery.deliver

        def crash(*args, **kwargs):
            if publish:
                original(*args, **kwargs)
            raise KeyboardInterrupt("controller lost during delivery")

        return mock.patch.object(fleet_herdr_delivery, "deliver", crash)

    def test_restart_after_publication_records_it_without_a_second_write(self):
        mid = self.accepted_mission("after-publish")
        with self.crash_after(publish=True), self.assertRaises(KeyboardInterrupt):
            fleet_herdr_mission.drive(self.runs, mid)
        written = (self.final(mid) / "notes.txt").stat().st_mtime_ns
        with mock.patch.object(fleet_herdr_delivery, "deliver", side_effect=AssertionError("no second write")):
            result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertIn("delivered; archive and closure pending", result["next_action"])
        self.assertEqual((self.final(mid) / "notes.txt").stat().st_mtime_ns, written)

    def test_restart_before_publication_delivers_once(self):
        mid = self.accepted_mission("before-publish")
        with self.crash_after(publish=False), self.assertRaises(KeyboardInterrupt):
            fleet_herdr_mission.drive(self.runs, mid)
        self.assertFalse(self.final(mid).exists())
        result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertIn("delivered; archive and closure pending", result["next_action"])
        self.assertEqual([d["status"] for d in result["delivery"]], ["delivered"])

    def test_foreign_destination_after_restart_is_indeterminate(self):
        mid = self.accepted_mission("indeterminate")
        with self.crash_after(publish=False), self.assertRaises(KeyboardInterrupt):
            fleet_herdr_mission.drive(self.runs, mid)
        self.final(mid).mkdir(parents=True)
        (self.final(mid) / "foreign.txt").write_text("unknown writer\n")
        result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertEqual((result["status"], self.terminal(mid)["reason"]),
                         ("indeterminate", "destination_changed_after_start"))

    def cancel_when(self, target, *, before=True):
        original = getattr(fleet_herdr_delivery, target) if target != "evaluate" else fleet_herdr_repair.evaluate
        module = fleet_herdr_repair if target == "evaluate" else fleet_herdr_delivery

        def wrapped(runs_or_root, mission_id, *args, **kwargs):
            if before:
                fleet_herdr_control.request(self.runs, mission_id, action="cancel", reason="stop", idempotency_key="stop")
            value = original(runs_or_root, mission_id, *args, **kwargs)
            if not before:
                fleet_herdr_control.request(self.runs, mission_id, action="cancel", reason="stop", idempotency_key="stop")
            return value

        return mock.patch.object(module, target, wrapped)

    def test_cancel_in_each_delivery_phase(self):
        cases = (("p0", "evaluate", False, False), ("p1-p2", "deliver", True, False), ("p3-p4", "deliver", False, True))
        for label, target, before, delivered in cases:
            with self.subTest(phase=label):
                mid = self.accepted_mission("cancel-" + label)
                if label == "p0":
                    self.outcomes = [failed_outcome(), "passed"]
                with self.cancel_when(target, before=before):
                    result = fleet_herdr_mission.drive(self.runs, mid)
                current = fleet_mission.load_state(self.runs, mid)
                self.assertEqual((result["status"], current["status"]), ("abandoned", "abandoned"))
                self.assertEqual(self.final(mid).exists(), delivered)
                statuses = [(d["finished"] or {}).get("status") for d in current["deliveries"]]
                self.assertEqual(statuses, {"p0": [], "p1-p2": ["aborted"], "p3-p4": ["delivered"]}[label])
                self.assertFalse(any(p.name.startswith(".staging") for p in self.final(mid).parent.glob("*"))
                                 if self.final(mid).parent.exists() else False)

    def test_deadline_in_each_delivery_phase(self):
        original = fleet_herdr_mission._Driver.remaining_seconds
        for label in ("p0", "p1-p2", "p4"):
            with self.subTest(phase=label):
                mid = self.accepted_mission("deadline-" + label)
                calls = {"n": 0}

                def remaining(driver):
                    current = driver.current()
                    accepted = next((a for a in current.get("repair_attempts") or []
                                     if (a["settled"] or {}).get("status") == "passed"), None)
                    started = current.get("deliveries")
                    if label == "p0" and accepted:
                        return 0
                    if label == "p1-p2" and started:
                        calls["n"] += 1
                        return 0 if calls["n"] > 1 else original(driver)
                    return original(driver)

                finish = fleet_herdr_mission._Driver.finish_delivery

                def late_finish(driver, attempt, receipt):
                    deadline = driver.current()["admission_policy"]["deadline_at"]
                    with mock.patch.object(state, "_next_timestamp", return_value=deadline):
                        return finish(driver, attempt, receipt)

                with mock.patch.object(fleet_herdr_mission._Driver, "remaining_seconds", remaining), \
                     mock.patch.object(fleet_herdr_mission._Driver, "finish_delivery",
                                       late_finish if label == "p4" else finish):
                    result = fleet_herdr_mission.drive(self.runs, mid)
                self.assertEqual((result["status"], self.terminal(mid)["reason"]),
                                 ("failed", "Herdr mission deadline expired"))
                self.assertEqual(self.final(mid).exists(), label == "p4")

    def test_control_confirmation_waits_for_an_inflight_delivery(self):
        mid = self.accepted_mission("confirm")
        with self.crash_after(publish=False), self.assertRaises(KeyboardInterrupt):
            fleet_herdr_mission.drive(self.runs, mid)
        fleet_herdr_control.request(self.runs, mid, action="cancel", reason="stop", idempotency_key="stop")
        view = fleet_herdr_control.view(fleet_mission.load_state(self.runs, mid))
        with self.assertRaisesRegex(state.MissionConflict, "delivery reconciliation"):
            fleet_herdr_control.acknowledge(self.runs, mid, view["requests"][view["latest"]])
        result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertEqual(result["status"], "abandoned")
        self.assertEqual([(d["finished"] or {}).get("status")
                          for d in fleet_mission.load_state(self.runs, mid)["deliveries"]], ["aborted"])

if __name__ == "__main__":
    import unittest
    unittest.main()
