"""FLEET-004 provider-free behavioral regressions: B01 through B06."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

from tests.test_fleet_herdr import FakeHerdr
from tests.test_fleet_herdr_mission import FakeBackend
from tests import test_fleet_herdr as backend_fixtures
from tests import test_fleet_herdr_control as control_fixtures
from tests import test_fleet_herdr_research as research_fixtures
from tests.test_fleet_functional import GOOD, synthetic_spec

import fleet_acceptance
import fleet_artifacts
import fleet_herdr
import fleet_herdr_archive
import fleet_herdr_control
import fleet_herdr_metrics
import fleet_herdr_mission
import fleet_herdr_personal
import fleet_herdr_profile
import fleet_herdr_runtime
import fleet_json
import fleet_mission
import fleet_mission_state as state
import workflow_config


ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   stderr=subprocess.PIPE, text=True).strip()


class MinimalBackend(FakeBackend):
    def __init__(self, runs_dir, mission_id, *, feature, target_repo, compiled,
                 session, **_shared_launch_options):
        super().__init__(runs_dir, mission_id, feature=feature,
                         target_repo=target_repo, compiled=compiled, session=session)


class MinimalFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fleet-004-minimal-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.target = self.root / "target"
        self.target.mkdir()
        git(self.target, "init", "-q")
        git(self.target, "config", "user.name", "Fixture")
        git(self.target, "config", "user.email", "fixture@example.invalid")
        (self.target / "README.md").write_text("baseline\n")
        (self.target / "sample_stats.py").write_text(GOOD)
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "fixture")
        self.head = git(self.target, "rev-parse", "HEAD")
        self.runs = self.root / "runs"
        self.compiled = workflow_config.compile_path(
            ROOT / "workflows" / "herdr-minimal-implementation.yaml")
        self.profile = fleet_herdr_profile.resolve_profile(self.compiled)
        self.contract = {"schema_version": 1, "requirements": [{
            "id": "answer", "description": "Build output exists", "checks": [{
                "kind": "text_contains", "path": "answer.txt", "expected": "implemented"}]}]}
        MinimalBackend.calls, MinimalBackend.tasks = [], {}
        MinimalBackend.observations, MinimalBackend.results = {}, {}
        MinimalBackend.crash_stage = MinimalBackend.missing_stage = None
        MinimalBackend.bad_result = MinimalBackend.bad_context = None
        MinimalBackend.role_status = "PASS"
        MinimalBackend.closed = MinimalBackend.teardown_error = False
        patcher = mock.patch.object(fleet_herdr_mission.fleet_herdr,
                                    "HerdrBackend", MinimalBackend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def options(self, contract=None):
        selected = self.contract if contract is None else contract
        return {"herdr_session": "minimal-fixture", "acceptance_contract": selected,
                "teardown": False, "timeout_seconds": 3600,
                "herdr_personal_cli": fleet_herdr_personal.PROFILE,
                **fleet_herdr_profile.runtime_binding(self.profile)}

    def create(self, *, contract=None, key="minimal"):
        selected = self.contract if contract is None else contract
        return fleet_mission.create_mission(self.runs, compiled=self.compiled,
            feature="minimal-test", objective="Implement a local answer",
            target_repo=self.target, base_sha=self.head,
            idempotency_key=fleet_acceptance.bound_key(key, selected),
            runtime_options=self.options(selected))[0]


class B01MinimalProfileTests(MinimalFixture):
    """B01: one real Build admission still freezes, accepts and archives."""

    def test_minimal_profile_runs_one_worker_and_offline_archive_v7(self):
        mid = self.create()
        result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual([call[1] for call in MinimalBackend.calls
                          if call[0] == "submit"], ["build"])
        current = fleet_mission.load_state(self.runs, mid)
        self.assertEqual(len(current["admissions"]), 1)
        self.assertEqual(sum(item["writer"] for item in current["admissions"].values()), 1)
        completing = next(event for event in state.read_events(state.ledger_path(self.runs, mid))
                          if event["kind"] == "mission_completing")
        self.assertEqual(set(completing["payload"]), {"completion_artifact_id"})
        verified = fleet_herdr_archive.verify(self.runs, mid)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["archive_schema_version"], 7)
        self.assertEqual(verified["permissions"]["policy_version"], 4)

    def test_worker_pass_cannot_replace_external_acceptance(self):
        rejected = copy.deepcopy(self.contract)
        rejected["requirements"][0]["checks"][0]["expected"] = "absent-value"
        mid = self.create(contract=rejected, key="minimal-rejected")
        result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["acceptance"]["status"], "rejected")
        self.assertEqual(next(iter(MinimalBackend.results.values()))["status"], "PASS")

    def test_minimal_functional_gate_runs_after_freeze_without_synthesis(self):
        options = self.options()
        options["functional_contract"] = synthetic_spec()
        mid = fleet_mission.create_mission(self.runs, compiled=self.compiled,
            feature="minimal-functional", objective="Implement and check",
            target_repo=self.target, base_sha=self.head,
            idempotency_key=fleet_acceptance.bound_key("minimal-functional", self.contract),
            runtime_options=options)[0]
        candidate = self.runs / "missions" / mid / "candidate"
        with mock.patch.object(fleet_herdr_mission.fleet_functional.runner, "execute",
                side_effect=lambda spec, _tree, _tests, attempt, **_kwargs:
                    research_fixtures.functional_outcome(spec, attempt, candidate)):
            result = fleet_herdr_mission.drive(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["archive"]["functional"]["status"], "passed")
        self.assertFalse(any(task["stage"] == "synthesis"
                             for task in MinimalBackend.tasks.values()))

    def test_pause_arriving_with_build_result_stops_before_minimal_freeze(self):
        options=self.options(); options["functional_contract"]=synthetic_spec()
        mid=fleet_mission.create_mission(self.runs,compiled=self.compiled,
            feature="minimal-build-pause",objective="Implement and check",
            target_repo=self.target,base_sha=self.head,
            idempotency_key=fleet_acceptance.bound_key("minimal-build-pause",self.contract),
            runtime_options=options)[0]
        collect=MinimalBackend.collect_result
        def pause_with_result(backend, run):
            result=collect(backend,run)
            if result is not None:
                fleet_herdr_control.request(self.runs,mid,action="pause",
                    reason="fixture pause while Build completes",idempotency_key="pause-with-build")
            return result
        with mock.patch.object(MinimalBackend,"collect_result",pause_with_result), \
                mock.patch.object(fleet_herdr_mission._Driver,"freeze") as freeze, \
                mock.patch.object(fleet_herdr_mission.fleet_functional.runner,"execute") as execute:
            fleet_herdr_mission.supervise(self.runs,mid,seconds=2,poll_seconds=.05)
        freeze.assert_not_called(); execute.assert_not_called()
        current=fleet_mission.load_state(self.runs,mid)
        self.assertEqual(fleet_herdr_control.view(current)["applied"],"paused")
        self.assertIsNone(current.get("functional_attempt"))
        self.assertFalse(any(event["kind"]=="functional_check_started"
            for event in state.read_events(state.ledger_path(self.runs,mid))))

    def test_pause_after_minimal_freeze_is_durably_reconciled_before_functional(self):
        options = self.options()
        options["functional_contract"] = synthetic_spec()
        mid = fleet_mission.create_mission(self.runs, compiled=self.compiled,
            feature="minimal-pause-race", objective="Implement and check",
            target_repo=self.target, base_sha=self.head,
            idempotency_key=fleet_acceptance.bound_key("minimal-pause-race", self.contract),
            runtime_options=options)[0]
        freeze = fleet_herdr_mission._Driver.freeze
        def pause_after_freeze(controller):
            frozen = freeze(controller)
            fleet_herdr_control.request(self.runs, mid, action="pause",
                reason="fixture concurrent pause before functional", idempotency_key="pause-at-freeze")
            return frozen
        with mock.patch.object(fleet_herdr_mission._Driver, "freeze", pause_after_freeze), \
                mock.patch.object(fleet_herdr_mission.fleet_functional.runner, "execute") as execute:
            result = fleet_herdr_mission.supervise(self.runs, mid, seconds=2, poll_seconds=0.05)
        execute.assert_not_called()
        current = fleet_mission.load_state(self.runs, mid)
        self.assertEqual(fleet_herdr_control.view(current)["applied"], "paused")
        self.assertIsNone(current.get("functional_attempt"))
        self.assertEqual(len(current["admissions"]),1)
        self.assertFalse(next(iter(current["admissions"].values()))["active"])
        calls = list(MinimalBackend.calls)
        fleet_herdr_mission.drive(self.runs, mid)
        self.assertEqual(MinimalBackend.calls,calls)

    def test_creation_requires_contract_and_profile_binding_is_immutable(self):
        options = self.options()
        options.pop("acceptance_contract")
        with self.assertRaisesRegex(fleet_mission.MissionError, "acceptance contract"):
            fleet_mission.create_mission(self.runs, compiled=self.compiled,
                feature="no-contract", objective="Build", target_repo=self.target,
                base_sha=self.head, idempotency_key="minimal-no-contract",
                runtime_options=options)
        changed = self.options()
        changed["herdr_profile_sha256"] = "0" * 64
        with self.assertRaisesRegex(fleet_herdr_profile.ProfileError, "changed"):
            fleet_herdr_profile.validate_profile_binding(self.compiled, changed)


class B02SharedStartupTests(unittest.TestCase):
    """B02: minimal boot traverses the shared backend and delivery guard."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fleet-004-startup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runs = self.root / "runs"
        self.runs.mkdir(mode=0o700)
        (self.runs / "missions").mkdir(mode=0o700)
        self.mid = str(uuid.uuid4())
        (self.runs / "missions" / self.mid).mkdir(mode=0o700)
        self.target = self.root / "target"
        self.target.mkdir()
        self.compiled = workflow_config.compile_path(
            ROOT / "workflows" / "herdr-minimal-implementation.yaml")
        self.fake = FakeHerdr()
        self.fake.codex_version = "0.159.3"
        self.fake.screen = "OpenAI Codex (v0.159.3)\n› Ask Codex to do anything\n"
        def transport(command, **kwargs):
            if command[0] == "codex" and command[-3:] == [
                    "debug", "prompt-input", "FLEET_LOCAL_CONTEXT_PROBE"]:
                value = [{"type": "message", "role": "developer", "content": [{
                    "type": "input_text", "text": "fixture runtime"}]},
                    {"type": "message", "role": "user", "content": [{
                        "type": "input_text", "text": "FLEET_LOCAL_CONTEXT_PROBE"}]}]
                return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
            return self.fake(command, **kwargs)
        self.backend = fleet_herdr.HerdrBackend(self.runs, self.mid,
            session="mission-control-test", feature="startup", target_repo=self.target,
            compiled=self.compiled, personal_cli=True,
            environment={"PATH": "/usr/bin:/bin", "CODEX_HOME": "/synthetic/codex"},
            run_command=transport, transcript_resolver=lambda _session: None)

    def test_shared_boot_has_one_sol_high_start_and_modal_blocks_delivery(self):
        backend_state = self.backend.boot()
        self.assertEqual([member["instance_id"] for member in backend_state["members"]],
                         ["worker"])
        starts = [self.fake.operation(call) for call in self.fake.calls
                  if self.fake.operation(call)[1:3] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        self.assertIn('model_reasoning_effort="high"', starts[0])
        member = backend_state["members"][0]
        prompts_before = sum(self.fake.operation(call)[1:3] == ["agent", "prompt"]
                             for call in self.fake.calls)
        self.fake.screen = "Hooks need review\n1 hook is new or changed\n› Ask Codex to do anything"
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "blocked before prompt"):
            self.backend._require_deliverable(member, backend_state, lambda: None)
        self.assertEqual(sum(self.fake.operation(call)[1:3] == ["agent", "prompt"]
                             for call in self.fake.calls), prompts_before)

    def test_late_session_identity_binds_without_resending_prompt(self):
        helper = backend_fixtures.HerdrBackendTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.fake.start_session_null = True
        helper.fake.lazy_session_until_prompt = True
        helper.fake.prompt_receipt_session_null = True
        backend = helper.booted()
        run_id = str(uuid.uuid4())
        backend.submit(run_id, helper.prompt(run_id))
        self.assertIsNone(backend.collect_result(run_id))
        bound = backend.state()["submissions"][run_id]["agent_session"]
        self.assertIsNotNone(bound)
        prompts = [helper.fake.operation(call) for call in helper.fake.calls
                   if helper.fake.operation(call)[1:3] == ["agent", "prompt"]]
        self.assertEqual(len(prompts), 1)


class B03ResearchHandoffTests(unittest.TestCase):
    """B03: Build receives verified bounded content, not opaque IDs alone."""

    def test_handoff_rejects_role_substitution_and_preserves_detail_pins(self):
        mid = str(uuid.uuid4())
        plan_run, research_run = str(uuid.uuid4()), str(uuid.uuid4())
        blobs = {}
        admissions = {}
        input_ids = []
        for stage_name, role, run in (("plan", "lead", plan_run),
                                      ("research", "research", research_run)):
            detail = (stage_name + " source").encode()
            detail_id = state.artifact_id(detail)
            blobs[detail_id] = detail
            result = {"mission_id": mid, "run_id": run, "instance_id": role,
                "status": "PASS", "summary": stage_name + " bounded finding",
                "artifacts": [{"path": "AGENTS.md", "sha256": "a" * 64}],
                "evidence_artifact_ids": [detail_id]}
            raw = fleet_json.canonical_bytes(result)
            artifact_id = state.artifact_id(raw)
            blobs[artifact_id] = raw
            input_ids.append(artifact_id)
            admissions[stage_name] = {"request_key": "herdr:" + stage_name,
                "phase": "finalized", "active": False,
                "terminal": {"status": "succeeded"}, "run_id": run,
                "recipient_instance": role, "result": {"artifact_id": artifact_id}}
        package = fleet_herdr_runtime.bounded_handoff(mission_id=mid, stage="build",
            input_artifact_ids=input_ids, current={"admissions": admissions},
            read_artifact=lambda pin: blobs[pin])
        self.assertEqual([item["summary"] for item in package["inputs"]],
                         ["plan bounded finding", "research bounded finding"])
        self.assertEqual(package["inputs"][1]["evidence_artifact_ids"],
                         [state.artifact_id(b"research source")])
        swapped = list(reversed(input_ids))
        with self.assertRaisesRegex(fleet_herdr_runtime.RuntimeContractError,
                                    "outside|binding"):
            fleet_herdr_runtime.bounded_handoff(mission_id=mid, stage="build",
                input_artifact_ids=swapped, current={"admissions": admissions},
                read_artifact=lambda pin: blobs[pin])


class B04RoleResponsibilityTests(unittest.TestCase):
    """B04: role dependencies retain independent Review and Verify inputs."""

    def test_verify_does_not_depend_on_review_and_minimal_omits_roles_at_creation(self):
        completed = {name: name + "-artifact" for name in
                     ("plan", "research", "build", "review")}
        self.assertEqual(fleet_herdr_runtime.role_inputs(
            "verify", completed, "independent-research-v1"),
            ["plan-artifact", "research-artifact", "build-artifact"])
        compiled = workflow_config.compile_path(
            ROOT / "workflows" / "herdr-minimal-implementation.yaml")
        self.assertIsNone(compiled["resolved"]["lead"])
        self.assertEqual([member["instance_id"] for member in compiled["resolved"]["instances"]],
                         ["worker"])
        self.assertEqual([stage[0] for stage in fleet_herdr_profile.resolve_profile(compiled).stages],
                         ["build"])

    def test_real_tasks_have_differentiated_role_criteria(self):
        helper = research_fixtures.ResearchProfileTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.drive_success()
        tasks = {task["stage"]: task for task in research_fixtures.ResearchBackend.tasks.values()}
        self.assertEqual(len(tasks["research"]["material_questions"]), 3)
        self.assertIn("actionable defects", tasks["review"]["stage_success_criteria"])
        self.assertIn("exact frozen candidate tree", tasks["verify"]["stage_success_criteria"])
        self.assertIn("reconcile", tasks["synthesis"]["stage_success_criteria"])
        review_id = next(result["result_artifact_id"] for result in
                         research_fixtures.ResearchBackend.results.values()
                         if result["instance_id"] == "reviewer")
        self.assertNotIn(review_id, tasks["verify"]["input_artifact_ids"])


class B05DurableMetricsTests(unittest.TestCase):
    """B05: cumulative use is assigned from a pinned pre-dispatch frontier."""

    @staticmethod
    def event(kind, **payload):
        return {"type": "event_msg", "payload": {"type": kind, **payload}}

    def test_second_turn_delta_uses_durable_baseline_and_validates_cached_subset(self):
        baseline = {"kind": "herdr_usage_baseline", "status": "known",
            "source": "pre_dispatch_session_counter",
            "counts": {"input_tokens": 100, "output_tokens": 10,
                       "cached_input_tokens": 20}}
        rows = [self.event("task_started", turn_id="synthesis"),
            self.event("token_count", info={"total_token_usage": {
                "input_tokens": 160, "output_tokens": 25, "cached_input_tokens": 35}}),
            self.event("task_complete", turn_id="synthesis")]
        usage = fleet_herdr_metrics.usage(rows, "synthesis", baseline)
        self.assertEqual((usage["prompt_tokens"], usage["completion_tokens"],
                          usage["cached_input_tokens"]), (60, 15, 15))
        rows[1]["payload"]["info"]["total_token_usage"] = {
            "input_tokens": 5, "output_tokens": 1, "cached_input_tokens": 10}
        invalid = fleet_herdr_metrics.usage(rows, "synthesis", baseline)
        self.assertIsNone(invalid["prompt_tokens"])
        self.assertEqual(invalid["usage_reason"], "invalid_usage_counter_snapshot")

    def test_absent_or_overlapping_baseline_stays_unknown_with_reason(self):
        rows = [self.event("task_started", turn_id="t"),
                self.event("token_count", info={"total_token_usage": {
                    "input_tokens": 2, "output_tokens": 1, "cached_input_tokens": 0}}),
                self.event("task_complete", turn_id="t")]
        unknown = {"kind": "herdr_usage_baseline", "status": "unknown",
                   "reason": "session_identity_not_available_before_dispatch"}
        self.assertEqual(fleet_herdr_metrics.usage(rows, "t", unknown)["usage_reason"],
                         "session_identity_not_available_before_dispatch")
        rows.insert(1, self.event("task_started", turn_id="other"))
        self.assertEqual(fleet_herdr_metrics.usage(rows, "t", unknown)["usage_reason"],
                         "overlapping_turn_usage")
        completed_between = [self.event("task_started", turn_id="foreign"),
            self.event("task_complete", turn_id="foreign"),
            self.event("task_started", turn_id="t"),
            self.event("token_count", info={"total_token_usage": {
                "input_tokens": 2, "output_tokens": 1, "cached_input_tokens": 0}}),
            self.event("task_complete", turn_id="t")]
        self.assertEqual(fleet_herdr_metrics.usage(
            completed_between, "t", unknown)["usage_reason"], "overlapping_turn_usage")
        changed = {"status": "unknown",
                   "reason": "usage_baseline_transcript_frontier_changed"}
        self.assertEqual(fleet_herdr_metrics.usage(
            rows[:1] + rows[2:], "t", unknown,
            baseline_frontier=changed)["usage_reason"],
            "usage_baseline_transcript_frontier_changed")


class B06ProportionalSupervisionTests(unittest.TestCase):
    """B06: one foreground observer is explicitly bounded to 3600 seconds."""

    def test_supervisor_rejects_unbounded_budget_before_mission_effects(self):
        with self.assertRaisesRegex(fleet_herdr_mission.HerdrMissionError,
                                    "0 < seconds <= 3600"):
            fleet_herdr_mission.supervise(Path("/unreached"), str(uuid.uuid4()),
                                          seconds=3600.01)

    def test_requested_pause_is_not_reported_as_applied(self):
        current = {"herdr_control": {"version": 1, "enabled_sequence": 1,
            "desired": "pause_requested", "latest": "pause",
            "requests": {"pause": {"action": "pause", "applied_at": None,
                                    "generation": None}},
            "dispatches": {}, "applied": None}, "admissions": {}}
        view = fleet_herdr_control.view(current)
        self.assertEqual(view["desired"], "pause_requested")
        self.assertIsNone(view["applied"])

    def test_budget_exhaustion_resumes_same_dispatch_to_durable_close(self):
        helper = control_fixtures.HerdrControlTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        control_fixtures.fixtures.FakeBackend.missing_stage = "plan"
        first = fleet_herdr_mission.supervise(helper.runs, helper.mid,
                                              seconds=0.15, poll_seconds=0.05)
        self.assertEqual(first["supervision"], "observation_budget_exhausted")
        before = [admission["run_id"] for admission in helper.current()["admissions"].values()]
        control_fixtures.fixtures.FakeBackend.missing_stage = None
        second = fleet_herdr_mission.supervise(helper.runs, helper.mid,
                                               seconds=10, poll_seconds=0.05)
        self.assertEqual(second["status"], "succeeded")
        after = [admission["run_id"] for admission in helper.current()["admissions"].values()]
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(len(helper.helper.submitted()), 5)
        self.assertEqual(sum(event["kind"] == "archive_created" for event in
                             state.read_events(state.ledger_path(helper.runs, helper.mid))), 1)


if __name__ == "__main__":
    unittest.main()
