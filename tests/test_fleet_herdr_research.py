from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_acceptance
import fleet_herdr_archive as archive
import fleet_herdr
import fleet_herdr_control as control
import fleet_herdr_mission as driver
import fleet_herdr_permissions as permissions
import fleet_herdr_profile as profiles
import fleet_herdr_report
import fleet_herdr_runtime
import fleet_json
import fleet_functional
import fleet_functional_runner
import fleet_mission
import fleet_mission_state as state
import workflow_config
from tests.test_fleet_herdr_mission import FakeBackend
from tests.test_fleet_herdr import FakeHerdr
from tests.test_fleet_functional import GOOD, synthetic_spec

REAL_BACKEND = fleet_herdr.HerdrBackend


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.PIPE, text=True
    ).strip()


def functional_outcome(spec, attempt_id, candidate):
    limits = spec["limits"]
    runner = fleet_functional_runner
    config = {"Image": spec["runtime"]["image_id"], "Name": "/fleet-functional-" + attempt_id,
        "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
            "Memory": limits["memory_bytes"], "MemorySwap": limits["memory_bytes"],
            "PidsLimit": limits["processes"], "NanoCpus": 1000000000,
            "ShmSize": limits["shm_bytes"], "CapDrop": ["ALL"], "IpcMode": "private",
            "SecurityOpt": ["no-new-privileges=true"], "LogConfig": {"Type": "none"}},
        "Config": {"User": "65534:65534", "WorkingDir": "/candidate", "Entrypoint": [runner.ARGV[0]],
            "Cmd": runner.ARGV[1:], "Labels": {"fleet.functional.attempt": attempt_id}},
        "Mounts": [{"Type": "bind", "Destination": dest, "Source": source, "RW": False}
                   for dest, source in (("/candidate", str(candidate)), ("/harness", "/synthetic-harness"))]}
    hello = {"kind": "runtime", "python": spec["runtime"]["python_version"], "uid": 65534,
        "cwd": "/candidate", "env": runner.ENV, "cap_eff": "0000000000000000",
        "no_new_privs": "1", "seccomp": "2",
        "cgroup": {"memory.max": str(limits["memory_bytes"]), "memory.swap.max": "0",
                   "pids.max": str(limits["processes"]), "cpu.max": "100000 100000"},
        "interfaces": {"lo": 1}, "seccomp_filters": 2, "ipv4_routes": [],
        "readonly": {"/": True, "/candidate": True, "/harness": True},
        "tmp_bytes": limits["tmp_bytes"], "shm_bytes": limits["shm_bytes"],
        "rlimits": {"RLIMIT_CPU": [limits["cpu_seconds"]] * 2,
                    "RLIMIT_FSIZE": [limits["file_bytes"]] * 2,
                    "RLIMIT_NOFILE": [limits["open_files"]] * 2, "RLIMIT_CORE": [0, 0]}}
    return {"status": "passed", "reason": "synthetic Research functional fixture", "evidence": {
        "container-config.json": state.canonical_bytes(config),
        "container-state.json": state.canonical_bytes({"ExitCode": 0, "Running": False, "OOMKilled": False}),
        "test-result.json": state.canonical_bytes({"tests_run": 5, "failures": 0, "errors": 0, "passed": True}),
        "test-output.txt": b"synthetic test output",
        "stdout.txt": state.canonical_bytes(hello) + b"\n{}\n", "stderr.txt": b""}}


class ResearchBackend(FakeBackend):
    mutate_during_research = False

    def __init__(self, runs_dir, mission_id, *, feature, target_repo, compiled,
                 session, **_launch_options):
        super().__init__(runs_dir, mission_id, feature=feature,
                         target_repo=target_repo, compiled=compiled, session=session)

    def artifact_path(self, stage):
        if stage == "plan":
            return "stable.txt"
        if stage == "research":
            return "README.md"
        return "answer.txt"

    def submit(self, run_id, prompt, *, instance_id):
        stage = json.loads(prompt)["stage"]
        if stage == "research" and self.mutate_during_research:
            (self.repo / "README.md").write_text("unauthorized research mutation\n")
        if stage == "build":
            (self.repo / "README.md").write_text("source legitimately changed by Build\n")
        return super().submit(run_id, prompt, instance_id=instance_id)


class ResearchProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="herdr-research-test-")
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name).resolve()
        self.target = self.tmp / "target"
        self.target.mkdir()
        git(self.target, "init", "-q")
        git(self.target, "config", "user.name", "Fixture")
        git(self.target, "config", "user.email", "fixture@example.invalid")
        (self.target / "README.md").write_text("investigated source\n")
        (self.target / "stable.txt").write_text("stable plan evidence\n")
        (self.target / "sample_stats.py").write_text(GOOD)
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "fixture")
        self.head = git(self.target, "rev-parse", "HEAD")
        self.runs = self.tmp / "runs"
        self.compiled = workflow_config.compile_path(
            ROOT / "workflows" / "herdr-research-implementation.yaml"
        )
        self.profile = profiles.resolve_profile(self.compiled)
        self.contract = {"schema_version": 1, "requirements": [{
            "id": "answer", "description": "answer exists", "checks": [{
                "kind": "text_contains", "path": "answer.txt", "expected": "implemented"
            }]
        }]}
        ResearchBackend.calls = []
        ResearchBackend.tasks = {}
        ResearchBackend.observations = {}
        ResearchBackend.results = {}
        ResearchBackend.crash_stage = None
        ResearchBackend.missing_stage = None
        ResearchBackend.bad_result = None
        ResearchBackend.bad_context = None
        ResearchBackend.role_status = "PASS"
        ResearchBackend.mutate_review = False
        ResearchBackend.mutate_during_research = False
        ResearchBackend.closed = False
        ResearchBackend.teardown_error = False
        mock.patch.object(driver.fleet_herdr, "HerdrBackend", ResearchBackend).start()
        self.addCleanup(mock.patch.stopall)

    def options(self, **extra):
        from fleet_herdr_personal import PROFILE
        return {"herdr_session": "research-fixture", "acceptance_contract": self.contract,
                "teardown": False, "timeout_seconds": 7200,
                "herdr_personal_cli": PROFILE,
                "herdr_handoff_policy": fleet_herdr_runtime.HANDOFF_POLICY,
                **profiles.runtime_binding(self.profile), **extra}

    def create(self, *, options=None, key="research-fixture", sdd_plan_path=None):
        return fleet_mission.create_mission(
            self.runs, compiled=self.compiled, feature="research-test",
            objective="Research then implement a local answer",
            target_repo=self.target, base_sha=self.head,
            idempotency_key=fleet_acceptance.bound_key(key, self.contract),
            runtime_options=options or self.options(), sdd_plan_path=sdd_plan_path,
        )[0]

    def drive_success(self):
        mid = self.create()
        result = driver.drive(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        return mid, result

    def test_end_to_end_has_six_turns_five_sessions_and_read_only_research(self):
        mid, result = self.drive_success()
        submissions = [call for call in ResearchBackend.calls if call[0] == "submit"]
        self.assertEqual([call[1] for call in submissions],
            ["plan", "research", "build", "review", "verify", "synthesis"])
        current = fleet_mission.load_state(self.runs, mid)
        self.assertEqual(len(current["admissions"]), 6)
        research = next(a for a in current["admissions"].values()
                        if a["request_key"] == "herdr:research")
        self.assertFalse(research["writer"])
        synthesis_task = next(task for task in ResearchBackend.tasks.values()
                              if task["stage"] == "synthesis")
        self.assertIn("Plan, Research, Build, Review and Verify",
                      synthesis_task["stage_success_criteria"])
        self.assertIn("Research source evidence", synthesis_task["instructions"])
        self.assertEqual(len(synthesis_task["input_artifact_ids"]), 5)
        self.assertIn(research["result"]["artifact_id"], synthesis_task["input_artifact_ids"])
        build_task = next(task for task in ResearchBackend.tasks.values()
                          if task["stage"] == "build")
        self.assertEqual(build_task["input_evidence"]["policy"], "bounded-cas-v1")
        self.assertEqual([entry["stage"] for entry in build_task["input_evidence"]["inputs"]],
                         ["plan", "research"])
        self.assertTrue(all(entry["summary"].startswith("Evidence")
                            for entry in build_task["input_evidence"]["inputs"]))
        sessions = {value["evidence"]["agent_session"]["value"]
                    for value in ResearchBackend.results.values()}
        self.assertEqual(len(sessions), 5)
        transcript = fleet_json.load_jsonl(archive.fleet_artifacts.get_bytes(
            self.runs, mid, ResearchBackend.results[research["run_id"]]["evidence"]["transcript_artifact_id"]))
        context = next(row["payload"] for row in transcript if row["type"] == "turn_context")
        self.assertEqual(context["sandbox_policy"], {"type": "read-only"})
        self.assertEqual(result["archive"]["archive_schema_version"], 6)
        self.assertEqual(result["archive"]["permissions"]["policy_version"], 3)
        self.assertIn("research", result["role_results"])

    def test_build_can_change_research_cited_source_and_archive_uses_investigated_tree(self):
        mid, result = self.drive_success()
        self.assertTrue(result["archive"]["valid"])
        root = self.runs / "missions" / mid / "herdr-archive"
        receipt = fleet_json.loads((root / "research-snapshot.json").read_bytes())
        roles = fleet_json.loads((root / "role-results.json").read_bytes())
        self.assertNotEqual(receipt["tree_sha"], result["archive"]["final_tree_sha"])
        self.assertEqual(roles["research"]["candidate_tree_sha"], receipt["tree_sha"])
        self.assertEqual(archive.verify(self.runs, mid)["archive_schema_version"], 6)
        candidate = self.runs / "missions" / mid / "candidate"
        shutil.rmtree(candidate)
        self.assertEqual(archive.verify(self.runs, mid)["archive_schema_version"], 6)

    def test_blocked_or_missing_research_never_admits_build(self):
        for case in ("missing", "blocked"):
            with self.subTest(case=case):
                mid = self.create(key="research-" + case)
                if case == "missing":
                    ResearchBackend.missing_stage = "research"
                else:
                    ResearchBackend.bad_result = staticmethod(lambda result: result.update(
                        status="BLOCKED") if result["instance_id"] == "research" else None)
                result = driver.drive(self.runs, mid)
                self.assertNotEqual(result["status"], "succeeded")
                current = fleet_mission.load_state(self.runs, mid)
                self.assertFalse(any(a["request_key"] == "herdr:build"
                                     for a in current["admissions"].values()))
                ResearchBackend.missing_stage = None
                ResearchBackend.bad_result = None

    def test_crash_after_build_submit_reuses_snapshot_and_same_run(self):
        mid = self.create()
        ResearchBackend.crash_stage = "build"
        with self.assertRaisesRegex(RuntimeError, "lost after submit"):
            driver.drive(self.runs, mid)
        before = copy.deepcopy(ResearchBackend.tasks)
        build_run = next(run for run, task in before.items() if task["stage"] == "build")
        ResearchBackend.crash_stage = None
        result = driver.drive(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(ResearchBackend.tasks[build_run], before[build_run])
        self.assertEqual(len([c for c in ResearchBackend.calls if c[0] == "submit"]), 6)

    def test_crash_after_research_submit_recovers_same_run_and_six_total_submits(self):
        mid = self.create()
        ResearchBackend.crash_stage = "research"
        with self.assertRaisesRegex(RuntimeError, "lost after submit"):
            driver.drive(self.runs, mid)
        before = copy.deepcopy(ResearchBackend.tasks)
        research_run = next(run for run, task in before.items() if task["stage"] == "research")
        ResearchBackend.crash_stage = None
        result = driver.drive(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(ResearchBackend.tasks[research_run], before[research_run])
        self.assertEqual(len([c for c in ResearchBackend.calls if c[0] == "submit"]), 6)

    def test_historical_creation_without_handoff_policy_keeps_id_only_task(self):
        options = self.options()
        options.pop("herdr_handoff_policy")
        mid = self.create(options=options, key="research-historical-handoff")
        self.assertEqual(driver.drive(self.runs, mid)["status"], "succeeded")
        build = next(task for task in ResearchBackend.tasks.values()
                     if task["stage"] == "build")
        self.assertNotIn("input_evidence", build)

    def test_research_mutation_blocks_first_build_before_admission(self):
        mid = self.create()
        ResearchBackend.mutate_during_research = True
        with self.assertRaisesRegex(driver.HerdrMissionError, "changed the candidate"):
            driver.drive(self.runs, mid)
        current = fleet_mission.load_state(self.runs, mid)
        self.assertFalse(any(a["request_key"] == "herdr:build"
                             for a in current["admissions"].values()))

    def test_offline_verify_rejects_v6_downgrade_and_rewritten_snapshot(self):
        mid, _ = self.drive_success()
        root = self.runs / "missions" / mid / "herdr-archive"
        index_path = root / "archive-index.json"
        original_index = index_path.read_bytes()
        index = fleet_json.loads(original_index)
        index["schema_version"] = 5
        index_path.write_bytes(fleet_json.canonical_bytes(index) + b"\n")
        with self.assertRaisesRegex(archive.HerdrArchiveError, "requires archive schema v6"):
            archive.verify(self.runs, mid, require_anchor=False)
        index_path.write_bytes(original_index)

        receipt_path = root / "research-snapshot.json"
        receipt = fleet_json.loads(receipt_path.read_bytes())
        receipt["candidate_repo"] += "-rewritten"
        receipt_raw = fleet_json.canonical_bytes(receipt) + b"\n"
        receipt_path.write_bytes(receipt_raw)
        index = fleet_json.loads(original_index)
        index["entries"]["research-snapshot.json"] = {
            "sha256": state.artifact_id(receipt_raw), "bytes": len(receipt_raw)}
        index_path.write_bytes(fleet_json.canonical_bytes(index) + b"\n")
        archived_events = fleet_json.load_jsonl((root / "ledger.jsonl").read_bytes())
        with mock.patch.object(archive.state, "read_events", return_value=archived_events):
            with self.assertRaisesRegex(archive.HerdrArchiveError,
                                        "does not authorize investigated snapshot"):
                archive.verify(self.runs, mid, require_anchor=False)

    def test_creation_rejects_opencode_capsule_launch_and_profile_drift(self):
        for extra in ({"executor": "opencode"},
                      {"herdr_capsule_manifest": {}},
                      {"herdr_launch_manifest": {}}):
            with self.subTest(extra=next(iter(extra))):
                with self.assertRaises((fleet_mission.MissionError, ValueError)):
                    self.create(options=self.options(**extra), key="reject-" + next(iter(extra)))
        changed = copy.deepcopy(self.compiled)
        changed["resolved"]["instances"][0]["hook_source"] = "opencode"
        with self.assertRaises(profiles.ProfileError):
            profiles.resolve_profile(changed)

    def test_creation_rejects_missing_or_wrong_personal_lane_before_effects(self):
        invalid = []
        missing = self.options()
        missing.pop("herdr_personal_cli")
        invalid.append(("missing", missing))
        invalid.append(("wrong", self.options(herdr_personal_cli="other-cli")))
        invalid.append(("executor", self.options(executor="opencode")))
        for label, options in invalid:
            with self.subTest(label=label):
                with self.assertRaises(fleet_mission.MissionError):
                    self.create(options=options, key="invalid-personal-" + label)
                self.assertFalse(self.runs.exists())
        mid = self.create(key="valid-personal")
        self.assertTrue((self.runs / "missions" / mid / "creation-request.json").is_file())

    def test_driver_rejects_tampered_creation_profile_pin_before_backend_effects(self):
        mid = self.create(key="tampered-creation-pin")
        root = self.runs / "missions" / mid
        options_before = (root / "runtime-options.json").read_bytes()
        ledger_before = (root / "mission.jsonl").read_bytes()
        creation = fleet_json.loads((root / "creation-request.json").read_bytes())
        creation["request"]["herdr_profile_sha256"] = "f" * 64
        (root / "creation-request.json").write_bytes(state.canonical_bytes(creation) + b"\n")
        with self.assertRaisesRegex(driver.HerdrMissionError, "creation Herdr profile binding"):
            driver.drive(self.runs, mid)
        self.assertEqual((root / "runtime-options.json").read_bytes(), options_before)
        self.assertEqual((root / "mission.jsonl").read_bytes(), ledger_before)
        self.assertEqual(ResearchBackend.calls, [])

    def test_control_rejects_tampered_creation_profile_pin_before_ledger_effects(self):
        mid = self.create(key="tampered-control-pin")
        root = self.runs / "missions" / mid
        creation = fleet_json.loads((root / "creation-request.json").read_bytes())
        creation["request"]["herdr_profile_sha256"] = "f" * 64
        (root / "creation-request.json").write_bytes(state.canonical_bytes(creation) + b"\n")
        ledger_before = (root / "mission.jsonl").read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "creation Herdr profile binding"):
            control.request(self.runs, mid, action="pause", reason="tamper fixture",
                            idempotency_key="tampered-control")
        self.assertEqual((root / "mission.jsonl").read_bytes(), ledger_before)

    def test_report_rejects_tampered_creation_profile_pin_read_only(self):
        mid = self.create(key="tampered-report-pin")
        root = self.runs / "missions" / mid
        creation = fleet_json.loads((root / "creation-request.json").read_bytes())
        creation["request"]["herdr_profile"] = "rewritten-profile"
        (root / "creation-request.json").write_bytes(state.canonical_bytes(creation) + b"\n")
        before = {path.relative_to(root): path.read_bytes()
                  for path in root.rglob("*") if path.is_file()}
        compiled, current = fleet_mission.load_mission_compiled(self.runs, mid, mode="read")
        events = state.read_events(state.ledger_path(self.runs, mid), expected_mission_id=mid)
        with self.assertRaisesRegex(profiles.ProfileError, "creation Herdr profile binding"):
            fleet_herdr_report.build_report(self.runs, current, compiled, events)
        self.assertEqual(before, {path.relative_to(root): path.read_bytes()
                                  for path in root.rglob("*") if path.is_file()})

    def test_mutating_runtime_and_creation_copies_cannot_override_ledger_profile(self):
        mid = self.create()
        root = self.runs / "missions" / mid
        options = fleet_json.loads((root / "runtime-options.json").read_bytes())
        creation = fleet_json.loads((root / "creation-request.json").read_bytes())
        for key in ("herdr_profile", "herdr_profile_sha256", "herdr_input_policy"):
            options.pop(key, None)
        creation["runtime_options"] = options
        creation["request"].pop("herdr_profile", None)
        creation["request"].pop("herdr_profile_sha256", None)
        (root / "runtime-options.json").write_bytes(state.canonical_bytes(options) + b"\n")
        (root / "creation-request.json").write_bytes(state.canonical_bytes(creation) + b"\n")
        with self.assertRaisesRegex(driver.HerdrMissionError, "profile binding"):
            driver.drive(self.runs, mid)

    def test_profile_policy_and_input_contract_are_versioned(self):
        self.assertEqual(self.profile.input_policy, "independent-research-v1")
        self.assertEqual(self.profile.permissions_policy_version, 3)
        self.assertEqual(permissions.policy("research", str(self.target), version=3)["sandbox_policy"],
                         {"type": "read-only"})
        self.assertEqual(permissions.finalization_policy(
            self.compiled["compiled_digest"], profile=self.profile)["required_turns"], 6)

    def test_dry_workflow_routes_to_herdr_without_effects(self):
        spec = importlib.util.spec_from_file_location("mission_run_research", ROOT / "scripts" / "mission-run.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.dry_run(feature="research-dry", objective="inspect", workflow_name="herdr-research-implementation",
            target_repo=self.target, risk_override="auto", timeout_seconds=None)
        self.assertEqual(result["backend"], "herdr")
        self.assertEqual(len([result["resolved"]["lead"], *result["resolved"]["instances"]]), 5)
        self.assertEqual(result["effects"], [])

    def test_retry_parser_and_dispatch_accept_research_without_runtime_effects(self):
        spec = importlib.util.spec_from_file_location("mission_run_retry", ROOT / "scripts" / "mission-run.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        mid = str(__import__("uuid").uuid4())
        root = self.runs / "missions" / mid
        root.mkdir(parents=True, mode=0o700)
        (self.runs / "missions").chmod(0o700)
        seen = []

        class Backend:
            def retry_unsubmitted_start(self, instance):
                seen.append(instance)

        class Driver:
            rel = Path("missions") / mid
            sdd_error = None
            def __init__(self, *_args):
                pass
            def load(self):
                pass
            def current(self):
                return {"status": "booting"}
            def check_candidate(self):
                pass
            def backend(self):
                return Backend()

        with mock.patch.object(module.fleet_herdr_mission, "_Driver", Driver), \
                mock.patch.object(module.fleet_herdr_control, "view", return_value={"desired": "running"}), \
                mock.patch.object(module, "drive_mission", return_value={"status": "succeeded"}):
            code = module.main(["--runs-dir", str(self.runs), "retry-start",
                "--mission-id", mid, "--instance", "research", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, ["research"])

    def test_real_backend_boots_five_member_grid_over_fake_transport(self):
        runs = self.tmp / "backend-runs"
        mission_id = str(__import__("uuid").uuid4())
        (runs / "missions" / mission_id).mkdir(parents=True, mode=0o700)
        (runs / "missions").chmod(0o700)
        fake = FakeHerdr()
        fake.codex_version = "0.159.3"
        fake.screen = "OpenAI Codex (v0.159.3)\n› Ask Codex to do anything\n"
        def local_preview(command, **kwargs):
            if command[0] == "codex" and command[-3:] == ["debug", "prompt-input", "FLEET_LOCAL_CONTEXT_PROBE"]:
                return subprocess.CompletedProcess(command, 0, json.dumps([
                    {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "fixture runtime"}]},
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": command[-1]}]}]), "")
            return fake(command, **kwargs)
        backend = REAL_BACKEND(runs, mission_id, session="mission-control-test",
            feature="research-backend", target_repo=self.target,
            compiled=copy.deepcopy(self.compiled), personal_cli=True,
            environment={"PATH": "/usr/bin:/bin"}, run_command=local_preview,
            transcript_resolver=lambda _session: None)
        state_value = backend.boot()
        self.assertEqual([m["instance_id"] for m in state_value["members"]],
                         ["lead", "research", "worker", "reviewer", "verifier"])
        starts = [FakeHerdr.operation(call) for call in fake.calls
                  if FakeHerdr.operation(call)[:3] == ["herdr", "agent", "start"]]
        self.assertEqual(len(starts), 5)
        research_start = next(call for call in starts if "_research" in call[3])
        self.assertIn("read-only", research_start)

    def test_research_sdd_and_functional_evidence_remain_optional_in_schema_v6(self):
        plan = self.tmp / "plan.json"
        plan.write_bytes((ROOT / "examples/sdd/deny-before-effect.json").read_bytes())
        spec = synthetic_spec()
        mid = self.create(options=self.options(functional_contract=spec),
                          key="research-sdd-functional", sdd_plan_path=plan)
        candidate = self.runs / "missions" / mid / "candidate"
        with mock.patch.object(fleet_functional.runner, "execute",
                side_effect=lambda spec, _tree, _tests, attempt, **_kwargs:
                    functional_outcome(spec, attempt, candidate)):
            result = driver.drive(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["archive"]["archive_schema_version"], 6)
        self.assertEqual(result["archive"]["functional"]["status"], "passed")
        root = self.runs / "missions" / mid / "herdr-archive"
        self.assertTrue((root / "sdd/plan.json").is_file())
        self.assertTrue((root / "functional-result.json").is_file())


if __name__ == "__main__":
    unittest.main()
