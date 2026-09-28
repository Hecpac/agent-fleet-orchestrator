"""Provider-free layout/handoff regressions. Fake transport is not sandbox proof."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleet_herdr_runtime as runtime
import fleet_herdr_mission as driver
import fleet_herdr_binding_v2 as binding
import fleet_herdr_binding as legacy
import fleet_artifacts
import fleet_mission
import fleet_mission_state as state
import workflow_config
from tests import test_fleet_herdr_mission as fixtures
from tests.repo_outputs import repo_outputs


class RuntimeLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=repo_outputs(), prefix="runtime-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runs, self.target, self.runtime = (self.root / name for name in ("runs", "target", "runtime"))
        for path in (self.runs, self.target, self.runtime):
            path.mkdir(mode=0o700)
        self.mid = str(uuid.uuid4())
        self.options = {"herdr_session": "mission-fixture", "herdr_layout": {"version": 2, "runtime_root": str(self.runtime)},
                        "herdr_input_policy": "independent-v1"}

    def candidate(self, options=None):
        return runtime.candidate_path(self.runs, self.mid, self.options if options is None else options, self.target)

    def test_separated_candidate_and_legacy_path(self):
        self.assertEqual(self.candidate(), self.runtime / self.mid / "candidate")
        self.assertEqual(self.candidate({}), self.runs / "missions" / self.mid / "candidate")
        runtime.prepare_parent(self.candidate(), self.options)
        self.assertEqual(set(runtime.parent_identity(self.candidate(), self.options)), {"runtime_root", "runtime_mission"})
        with self.assertRaisesRegex(runtime.RuntimeContractError, "already exists"):
            runtime.prepare_parent(self.candidate(), self.options)

    def test_protected_overlap_alias_and_traversal_rejected(self):
        (self.root / "alias").symlink_to(self.runtime, target_is_directory=True)
        paths = (self.runs, self.target, self.root, self.root / "alias", self.runtime / ".." / "runtime")
        for path in paths:
            with self.subTest(path=path), self.assertRaises((runtime.RuntimeContractError, OSError)):
                self.candidate({"herdr_layout": {"version": 2, "runtime_root": str(path)}})

    def test_version_and_world_writable_root_rejected(self):
        for version in (True, 1, 3, "2"):
            with self.subTest(version=version), self.assertRaises(runtime.RuntimeContractError):
                self.candidate({"herdr_layout": {"version": version, "runtime_root": str(self.runtime)}})
        self.runtime.chmod(0o777)
        with self.assertRaises(runtime.RuntimeContractError):
            self.candidate()

    def test_parent_alias_after_preparation_rejected(self):
        candidate = self.candidate()
        runtime.prepare_parent(candidate, self.options)
        candidate.parent.rename(self.runtime / "moved")
        candidate.parent.symlink_to(self.runtime / "moved", target_is_directory=True)
        with self.assertRaisesRegex(runtime.RuntimeContractError, "alias"):
            runtime.parent_identity(candidate, self.options)

    def test_verifier_excludes_reviewer_and_synthesis_includes_all(self):
        completed = dict(plan="p", build="b", review="r", verify="v")
        self.assertEqual(runtime.role_inputs("verify", completed, "independent-v1"), ["p", "b"])
        self.assertEqual(runtime.role_inputs("synthesis", completed, "independent-v1"), ["p", "b", "r", "v"])
        self.assertEqual(runtime.role_inputs("verify", completed, None), ["p", "b", "r", "v"])

    def test_driver_layout_can_freeze_binding_v2(self):
        candidate = self.candidate()
        runtime.prepare_parent(candidate, self.options)
        candidate.mkdir(mode=0o700)
        binary = self.root / "codex-fixture"
        binary.write_bytes(b"synthetic image, never executed")
        ledger = self.runs / "missions" / self.mid
        ledger.mkdir(parents=True)
        cas = ledger / "artifacts"
        cas.mkdir()
        tmp = self.runtime / self.mid / "tmp"
        tmp.mkdir(mode=0o700)
        raw = binding.freeze_attempt(attempt={"mission_id": self.mid, "role": "worker",
            "generation": str(uuid.uuid4()), "attempt_id": str(uuid.uuid4())}, challenge="a" * 64,
            candidate=str(candidate), codex={"image": legacy.path_identity(str(binary), directory=False), "version": "fixture"},
            temporary_roots={"slash_tmp": str(Path('/tmp').resolve()), "tmpdir": str(tmp)},
            protected_roots={"runs": str(self.runs), "control": str(ledger), "ledger": str(ledger), "cas": str(cas)},
            router_sha256="b" * 64, workflow_sha256="c" * 64)
        self.assertEqual(json.loads(raw)["candidate"]["realpath"], str(candidate))


class RuntimeMissionTests(unittest.TestCase):
    create = fixtures.HerdrMissionTests.create

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=repo_outputs(), prefix="runtime-mission-")
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name).resolve()
        self.target, self.runs = self.tmp / "target", self.tmp / "runs"
        # Reuse existing local history, without a commit, provider or network.
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "clone", "--no-local", "--no-hardlinks",
                        str(ROOT), str(self.target)], check=True, capture_output=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "GIT_CONFIG_NOSYSTEM": "1",
                            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"})
        self.head = fixtures.git(self.target, "rev-parse", "HEAD")
        self.runtime = self.tmp / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.options = {"herdr_session": "mission-fixture", "herdr_layout": {"version": 2, "runtime_root": str(self.runtime)},
                        "herdr_input_policy": "independent-v1"}
        self.compiled = workflow_config.compile_path(ROOT / "workflows/herdr-implementation.yaml")
        self.options["acceptance_contract"] = {"schema_version": 1, "requirements": [
            {"id": "answer", "description": "answer exists", "checks": [
                {"kind": "text_contains", "path": "answer.txt", "expected": "implemented"}]}]}
        fake = fixtures.FakeBackend
        fake.calls, fake.tasks, fake.observations, fake.results = [], {}, {}, {}
        fake.crash_stage = fake.missing_stage = fake.bad_result = fake.bad_context = None
        fake.role_status, fake.mutate_review = "PASS", False
        fake.closed = fake.teardown_error = False
        self.archive = fixtures.FakeArchive()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(driver.fleet_herdr, "HerdrBackend", fake).start()
        mock.patch.object(driver, "_archive", return_value=self.archive).start()
        self.mid = self.create(options=self.options)

    def test_five_stages_use_separated_candidate_and_independent_inputs(self):
        result = driver.drive(self.runs, self.mid)
        self.assertEqual(result["status"], "succeeded")
        tasks = {t["stage"]: t for t in fixtures.FakeBackend.tasks.values()}
        self.assertEqual(set(tasks), {"plan", "build", "review", "verify", "synthesis"})
        self.assertEqual(tasks["verify"]["input_artifact_ids"], tasks["review"]["input_artifact_ids"])
        self.assertEqual(len(tasks["synthesis"]["input_artifact_ids"]), 4)
        self.assertEqual(tasks["build"]["candidate_repo"], str(self.runtime / self.mid / "candidate"))
        self.assertFalse((self.runs / "missions" / self.mid / "candidate").exists())
        self.assertEqual(fixtures.git(self.target, "status", "--porcelain"), "")
        self.assertEqual(fixtures.git(self.target, "rev-parse", "HEAD"), self.head)

    def test_recovery_reconstructs_same_independent_handoffs_without_resubmit(self):
        fixtures.FakeBackend.missing_stage = "verify"
        first = driver.drive(self.runs, self.mid)
        self.assertIn("next_action", first)
        before = copy.deepcopy(fixtures.FakeBackend.tasks)
        fixtures.FakeBackend.missing_stage = None
        result = driver.drive(self.runs, self.mid)
        self.assertEqual(result["status"], "succeeded")
        for run, task in before.items():
            self.assertEqual(fixtures.FakeBackend.tasks[run], task)
        self.assertEqual(len([c for c in fixtures.FakeBackend.calls if c[0] == "submit"]), 5)

    def test_runtime_parent_replacement_fails_before_next_role(self):
        fixtures.FakeBackend.missing_stage = "build"
        driver.drive(self.runs, self.mid)
        parent = self.runtime / self.mid
        parent.rename(self.runtime / "old")
        parent.symlink_to(self.runtime / "old", target_is_directory=True)
        with self.assertRaises(runtime.RuntimeContractError):
            driver.drive(self.runs, self.mid)

    def test_legacy_mission_retains_cumulative_handoffs_and_location(self):
        self.mid = self.create(key="legacy-runtime")
        result = driver.drive(self.runs, self.mid)
        self.assertEqual(result["status"], "succeeded")
        tasks = {t["stage"]: t for t in fixtures.FakeBackend.tasks.values()}
        self.assertEqual(len(tasks["verify"]["input_artifact_ids"]), 3)
        self.assertEqual(tasks["build"]["candidate_repo"], str(self.runs / "missions" / self.mid / "candidate"))

    def test_separated_candidate_uses_real_archive_cas_and_acceptance(self):
        import fleet_herdr_archive
        with mock.patch.object(driver, "_archive", return_value=fleet_herdr_archive):
            result = driver.drive(self.runs, self.mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["acceptance"]["status"], "accepted")
        candidate = self.runtime / self.mid / "candidate"
        self.assertEqual(fixtures.git(candidate, "rev-parse", "HEAD"), self.head)
        self.assertEqual(fixtures.git(candidate, "diff", "--cached", "--name-only"), "")
        candidate.rename(candidate.with_name("unavailable-candidate"))
        with mock.patch.object(driver, "_archive", return_value=fleet_herdr_archive):
            self.assertEqual(driver.drive(self.runs, self.mid)["status"], "succeeded")

    def test_functional_tests_inside_v2_candidate_block_before_container(self):
        import fleet_acceptance
        import fleet_herdr_archive
        import fleet_functional_runner
        from tests import test_fleet_functional
        key = "functional-separated-candidate"
        bound = fleet_acceptance.bound_key(key, self.options["acceptance_contract"])
        mid = str(uuid.uuid5(uuid.NAMESPACE_URL, "fleet-mission:" + bound))
        spec = test_fleet_functional.synthetic_spec()
        spec["tests"]["path"] = str(self.runtime / mid / "candidate" / "test_sample_stats.py")
        self.mid = self.create(options={**self.options, "functional_contract": spec}, key=key)
        self.assertEqual(self.mid, mid)
        with mock.patch.object(driver, "_archive", return_value=fleet_herdr_archive), mock.patch.object(
                fleet_functional_runner, "execute", side_effect=AssertionError("container must not execute")):
            result = driver.drive(self.runs, self.mid)
        self.assertEqual(result["functional"]["status"], "blocked")
        self.assertEqual(result["functional"]["reason"], "tests_must_be_external_to_candidate")
