import concurrent.futures
import json
import threading
import time
import unittest
import uuid
from unittest import mock

from tests import test_fleet_herdr_mission as fixtures
import fleet_herdr_control as control
import fleet_herdr_mission as driver
import fleet_mission
import fleet_mission_state as state
import fleet_safe_paths


class HerdrControlTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.HerdrMissionTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.runs, self.mid = self.helper.runs, self.helper.mid

    def request(self, action, key=None, **kwargs):
        return control.request(self.runs, self.mid, action=action, reason="synthetic control test",
                               idempotency_key=key or str(uuid.uuid4()), **kwargs)

    def current(self):
        return fleet_mission.load_state(self.runs, self.mid)

    def test_pause_before_boot_has_no_effect_and_resume_preserves_deadline(self):
        before = self.current()["admission_policy"]["deadline_at"]
        self.request("pause")
        response = self.helper.run_driver()
        self.assertEqual(response["control"]["applied"], "paused")
        self.assertFalse(fixtures.FakeBackend.calls)
        self.request("resume")
        self.assertEqual(self.helper.run_driver()["status"], "succeeded")
        self.assertEqual(self.current()["admission_policy"]["deadline_at"], before)
        self.assertEqual(len(self.helper.submitted()), 5)

    def test_crash_after_cancellation_ack_closes_without_runtime_even_after_deadline(self):
        self.request("cancel", "cancel-before-boot")
        with mock.patch.object(state, "append_terminal", side_effect=RuntimeError("lost after ack")):
            with self.assertRaisesRegex(RuntimeError, "lost after ack"):
                self.helper.run_driver()
        self.assertEqual(control.view(self.current())["applied"], "cancelled")
        self.assertNotIn(self.current()["status"], state.TERMINAL_STATUSES)
        with mock.patch.object(driver._Driver, "remaining_seconds", return_value=-1):
            self.assertEqual(self.helper.run_driver()["status"], "abandoned")
        self.assertFalse(fixtures.FakeBackend.calls)

    def test_pause_is_persisted_during_locked_wait_and_reconciles_active_turn(self):
        entered, release = threading.Event(), threading.Event()
        fixtures.FakeBackend.missing_stage = "plan"
        def wait(_backend, run_id, *, timeout_ms):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"status": "settled"}
        with mock.patch.object(fixtures.FakeBackend, "wait", wait), concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.helper.run_driver)
            try:
                self.assertTrue(entered.wait(5))
                start = time.monotonic()
                self.request("pause")
                self.assertLess(time.monotonic() - start, 1)
                self.assertEqual(control.view(self.current())["desired"], "pause_requested")
                self.assertEqual(control.view(self.current())["applied"], "running")
                fixtures.FakeBackend.missing_stage = None
            finally:
                release.set()
            result = running.result(timeout=5)
        self.assertEqual(result["control"]["applied"], "paused")
        self.assertEqual(self.helper.submitted(), ["plan"])
        self.request("resume")
        self.assertEqual(self.helper.run_driver()["status"], "succeeded")
        self.assertEqual(self.helper.submitted(), ["plan", "build", "review", "verify", "synthesis"])

    def test_crash_before_dispatch_intent_can_send_once_on_resume(self):
        with mock.patch.object(driver._Driver, "dispatch_or_recover", side_effect=RuntimeError("before intent")):
            with self.assertRaisesRegex(RuntimeError, "before intent"):
                self.helper.run_driver()
        self.assertFalse(self.helper.submitted())
        self.assertFalse(control.view(self.current())["dispatches"])
        self.assertEqual(self.helper.run_driver()["status"], "succeeded")
        self.assertEqual(len(self.helper.submitted()), 5)

    def test_crash_after_intent_before_transport_is_ambiguous_and_never_replayed(self):
        with mock.patch.object(fixtures.FakeBackend, "submit", side_effect=RuntimeError("lost before transport")):
            with self.assertRaises(RuntimeError):
                self.helper.run_driver()
        self.assertEqual(len(control.view(self.current())["dispatches"]), 1)
        self.assertEqual(self.helper.run_driver()["status"], "running")
        self.assertFalse(self.helper.submitted())
        self.assertTrue(any(c[0] == "recover" for c in fixtures.FakeBackend.calls))

    def test_crash_after_send_pause_and_recovery_do_not_resend_recognized_prompt(self):
        fixtures.FakeBackend.crash_stage = "build"
        with self.assertRaises(RuntimeError):
            self.helper.run_driver()
        self.request("pause")
        fixtures.FakeBackend.crash_stage = None
        result = self.helper.run_driver()
        self.assertEqual(result["control"]["applied"], "paused")
        self.assertEqual(self.helper.submitted(), ["plan", "build"])
        self.request("resume")
        self.assertEqual(self.helper.run_driver()["status"], "succeeded")
        self.assertEqual(len(self.helper.submitted()), 5)

    def test_cancellation_is_exact_repeated_and_result_can_precede_signal(self):
        fixtures.FakeBackend.crash_stage = "build"
        with self.assertRaises(RuntimeError):
            self.helper.run_driver()
        admission = next(a for a in self.current()["admissions"].values() if a["active"])
        first = self.request("cancel", "same-cancel", run_id=admission["run_id"])
        second = self.request("cancel", "same-cancel", run_id=admission["run_id"])
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertFalse(second["recorded"])
        with mock.patch.object(fixtures.FakeBackend, "cancel", side_effect=AssertionError("result already arrived")):
            result = self.helper.run_driver()
        self.assertEqual(result["status"], "abandoned")
        self.assertEqual(control.view(self.current())["applied"], "cancelled")
        self.assertEqual(self.current()["admissions"][admission["admission_id"]]["terminal"]["status"], "succeeded")

    def test_pending_cancel_requires_matching_generation_and_terminal_proof(self):
        fixtures.FakeBackend.missing_stage = "build"
        self.helper.run_driver()
        active = next(a for a in self.current()["admissions"].values() if a["active"])
        with self.assertRaisesRegex(state.MissionConflict, "generation"):
            self.request("cancel", generation=str(uuid.uuid4()), run_id=active["run_id"])
        with self.assertRaisesRegex(state.MissionConflict, "unknown owned run"):
            self.request("cancel", run_id=str(uuid.uuid4()))
        self.request("cancel", run_id=active["run_id"])
        generation = control.backend_generation(self.runs, self.mid)
        bad = {"run_id": active["run_id"], "instance_id": "worker", "prompt_sha256": active["task_sha256"],
               "status": "abandoned", "cancel_attempted": True, "generation": str(uuid.uuid4())}
        with mock.patch.object(fixtures.FakeBackend, "cancel", return_value=bad):
            self.assertEqual(self.helper.run_driver()["status"], "running")
        self.assertTrue(self.current()["admissions"][active["admission_id"]]["active"])
        with mock.patch.object(fixtures.FakeBackend, "cancel", return_value={**bad, "generation": generation}):
            self.assertEqual(self.helper.run_driver()["status"], "abandoned")

    def test_exact_run_cancel_skips_other_active_admissions_without_closing_mission(self):
        fixtures.FakeBackend.missing_stage = "plan"
        self.helper.run_driver()
        other = next(a for a in self.current()["admissions"].values() if a["active"])
        task_sha = fixtures.fleet_artifacts.put_bytes(self.runs, self.mid, b"unsent review task")["artifact_id"]
        target = fixtures.fleet_admission.reserve_many(self.runs, self.mid, requests=[{
            "request_key": "herdr:review", "run_kind": "specialist", "recipient_instance": "reviewer",
            "capability": "challenge", "effect_sha256": task_sha, "task_sha256": task_sha,
            "delegated_budget": 0, "writer": False}], idempotency_key="reserve-review")["admissions"][0]
        self.assertEqual([a["run_id"] for a in self.current()["admissions"].values() if a["active"]],
                         [other["run_id"], target["run_id"]])
        calls = list(fixtures.FakeBackend.calls)
        deadline = self.current()["admission_policy"]["deadline_at"]
        self.request("cancel", run_id=target["run_id"])

        result = self.helper.run_driver()

        current = self.current()
        self.assertEqual(current["admissions"][target["admission_id"]]["phase"], "aborted")
        self.assertFalse(current["admissions"][target["admission_id"]]["active"])
        self.assertEqual(current["admissions"][other["admission_id"]], other)
        self.assertEqual(fixtures.FakeBackend.calls, calls)
        self.assertEqual(result["status"], "running")
        self.assertIn("outside cancellation scope", result["next_action"])
        self.assertEqual(control.view(current)["desired"], "cancel_requested")
        self.assertEqual(control.view(current)["applied"], "running")
        self.assertEqual(current["admission_policy"]["deadline_at"], deadline)
        before = state.ledger_path(self.runs, self.mid).read_bytes()
        self.helper.run_driver()
        self.assertEqual(state.ledger_path(self.runs, self.mid).read_bytes(), before)
        self.assertEqual(fixtures.FakeBackend.calls, calls)

    def test_exact_run_cancel_retains_durable_pass_with_another_reserved_run(self):
        fixtures.FakeBackend.missing_stage = "plan"
        self.helper.run_driver()
        target = next(a for a in self.current()["admissions"].values() if a["active"])
        task_sha = fixtures.fleet_artifacts.put_bytes(self.runs, self.mid, b"unsent review task")["artifact_id"]
        other = fixtures.fleet_admission.reserve_many(self.runs, self.mid, requests=[{
            "request_key": "herdr:review", "run_kind": "specialist", "recipient_instance": "reviewer",
            "capability": "challenge", "effect_sha256": task_sha, "task_sha256": task_sha,
            "delegated_budget": 0, "writer": False}], idempotency_key="reserve-review")["admissions"][0]
        other = self.current()["admissions"][other["admission_id"]]
        self.request("cancel", run_id=target["run_id"])
        fixtures.FakeBackend.missing_stage = None
        calls = len(fixtures.FakeBackend.calls)

        result = self.helper.run_driver()

        current = self.current()
        settled = current["admissions"][target["admission_id"]]
        self.assertEqual(settled["phase"], "finalized")
        self.assertEqual(settled["terminal"]["status"], "succeeded")
        self.assertEqual(current["admissions"][other["admission_id"]], other)
        self.assertEqual(result["status"], "running")
        self.assertEqual(control.view(current)["applied"], "running")
        self.assertEqual(fixtures.FakeBackend.calls[calls:], [("collect", target["run_id"])])
        self.assertEqual(self.helper.submitted(), ["plan"])

    def test_pause_after_authorization_before_intent_allows_safe_later_send(self):
        original = driver._Driver.dispatch_or_recover
        called = False
        def pause_once(instance, *args, **kwargs):
            nonlocal called
            if not called:
                called = True
                self.request("pause")
            return original(instance, *args, **kwargs)
        with mock.patch.object(driver._Driver, "dispatch_or_recover", pause_once):
            self.helper.run_driver()
        self.assertEqual(self.helper.run_driver()["control"]["applied"], "paused")
        self.assertFalse(self.helper.submitted())
        self.request("resume")
        self.assertEqual(self.helper.run_driver()["status"], "succeeded")

    def test_two_supervisors_cannot_own_one_mission(self):
        with fleet_safe_paths.RootedFS(self.runs) as fs:
            with fs.exclusive_lock(self.helper.runs.relative_to(self.helper.runs) / "missions" / self.mid / "herdr-supervisor.lock",
                                   directory_modes=(0o700, 0o700), blocking=False):
                result = driver.supervise(self.runs, self.mid, seconds=0.2)
        self.assertEqual(result["supervision"], "already_owned")
        self.assertFalse(fixtures.FakeBackend.calls)

    def test_paused_deadline_does_not_extend_and_historical_terminal_is_unchanged(self):
        self.request("pause")
        self.helper.run_driver()
        with mock.patch.object(driver._Driver, "remaining_seconds", return_value=-1):
            self.assertEqual(self.helper.run_driver()["status"], "failed")
        before = state.ledger_path(self.runs, self.mid).read_bytes()
        self.assertFalse(self.request("resume")["recorded"])
        self.helper.run_driver()
        self.assertEqual(state.ledger_path(self.runs, self.mid).read_bytes(), before)

    def test_deadline_releases_fresh_authorization_without_dispatch_or_signal(self):
        with mock.patch.object(driver._Driver,"dispatch_or_recover",side_effect=RuntimeError("before dispatch")):
            with self.assertRaises(RuntimeError):
                self.helper.run_driver()
        with mock.patch.object(driver._Driver,"remaining_seconds",return_value=-1), \
             mock.patch.object(fixtures.FakeBackend,"cancel",side_effect=AssertionError("nothing was sent")):
            self.assertEqual(self.helper.run_driver()["status"],"failed")
        self.assertFalse(self.helper.submitted())
        self.assertFalse(any(a["active"] for a in self.current()["admissions"].values()))

    def test_supervision_budget_returns_pending_without_duplicate_prompts(self):
        fixtures.FakeBackend.missing_stage="plan"
        first=driver.supervise(self.runs,self.mid,seconds=0.3,poll_seconds=0.05)
        self.assertEqual(first["supervision"],"observation_budget_exhausted")
        fixtures.FakeBackend.missing_stage=None
        self.assertEqual(driver.supervise(self.runs,self.mid,seconds=10)["status"],"succeeded")
        self.assertEqual(len(self.helper.submitted()),5)

    def test_pause_between_staging_and_closure_resumes_without_more_turns(self):
        original = driver._Driver.complete_archive
        def pause_then_close(instance, *args, **kwargs):
            self.request("pause")
            return original(instance, *args, **kwargs)
        with mock.patch.object(driver._Driver, "complete_archive", pause_then_close):
            result = self.helper.run_driver()
        self.assertEqual(result["control"]["applied"], "paused")
        self.assertEqual(self.current()["status"], "completing")
        self.request("resume")
        self.assertEqual(self.helper.run_driver()["status"], "succeeded")
        self.assertEqual(len(self.helper.submitted()), 5)
