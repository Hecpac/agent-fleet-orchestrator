import copy
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_acceptance
import fleet_artifacts
import fleet_functional as functional
import fleet_functional_runner as runner
import fleet_herdr_archive as archive
import fleet_herdr_mission as driver
import fleet_herdr_control as control
import fleet_json
import fleet_mission
import fleet_mission_state as state
from tests import test_fleet_herdr_archive, test_fleet_herdr_mission

TESTS = Path(__file__).parent / "fixtures/functional_stats/test_sample_stats.py"
GOOD = '''def stats(values):
    if not values: raise ValueError()
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in values): raise TypeError()
    return {"count": len(values), "sum": sum(values), "mean": sum(values) / len(values)}
'''
BROKEN = 'def stats(values):\n    return {"count": 7, "sum": 42, "mean": 6}\n'


def synthetic_spec():
    return {"schema_version": 1, "check_id": runner.CHECK,
        "tests": {"path": str(TESTS.resolve()), "sha256": runner.TEST_SHA},
        "runtime": {"image_id": "sha256:" + "a" * 64, "python_version": "3.12.13",
            "engine_version": "fixture", "architecture": "arm64", "os": "linux", "dependencies": "stdlib-only",
            "docker_endpoint_sha256": "b" * 64, "controller_sha256": "c" * 64,
            "guest_sha256": "d" * 64, "controller_python": "3.13.0"},
        "argv": list(runner.ARGV), "cwd": "/candidate", "environment": dict(runner.ENV),
        "profile": runner.PROFILE, "source_policy": runner.SOURCE_POLICY, "limits": dict(runner.LIMITS)}


class FunctionalContractTests(unittest.TestCase):
    def policy_fixture(self):
        helper = test_fleet_herdr_archive.HerdrSnapshotTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        mid, roles, backend = helper.archive_fixture(runtime_options={"functional_contract": synthetic_spec(), "herdr_session": "fixture"})
        return helper, mid, roles, backend

    def test_required_functional_policy_cannot_be_skipped_at_archive_creation(self):
        helper, mid, roles, backend = self.policy_fixture()
        with self.assertRaisesRegex(functional.FunctionalError, "bound functional attempt"):
            archive.create(helper.runs, mid, helper.repo, roles, backend)
        self.assertEqual(fleet_mission.load_state(helper.runs, mid)["status"], "completing")

    def test_removing_runtime_option_cannot_remove_durable_functional_requirement(self):
        helper, mid, _roles, _backend = self.policy_fixture()
        root = helper.runs / "missions" / mid
        options = fleet_json.loads((root / "runtime-options.json").read_bytes())
        creation = fleet_json.loads((root / "creation-request.json").read_bytes())
        options.pop("functional_contract")
        creation["runtime_options"] = options
        for name, value in (("runtime-options.json", options), ("creation-request.json", creation)):
            (root / name).write_bytes(state.canonical_bytes(value) + b"\n")
        with self.assertRaisesRegex(driver.HerdrMissionError, "functional policy/runtime options"):
            driver._Driver(helper.runs, mid).load()
    def test_rejects_unknown_commands_paths_env_and_weakened_limits(self):
        valid = synthetic_spec()
        functional.validate(valid)
        mutations = [lambda s: s.update(check_id="shell"), lambda s: s.update(argv=["sh", "-c", "true"]),
            lambda s: s["tests"].update(path="/tmp/../secret"), lambda s: s["tests"].update(sha256="0" * 64),
            lambda s: s["environment"].update(API_KEY="synthetic"), lambda s: s["limits"].update(network="host"),
            lambda s: s["limits"].update(processes=100), lambda s: s["limits"].update(memory_bytes=True),
            lambda s: s.update(schema_version=2), lambda s: s["runtime"].update(image_id="python:latest")]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(functional.FunctionalError):
                spec = copy.deepcopy(valid)
                mutate(spec)
                functional.validate(spec)

    def test_tree_unpack_rejects_escaping_symlink_and_duplicate_entries(self):
        for name, kind, duplicate in [("../escape", tarfile.REGTYPE, False), ("/escape", tarfile.REGTYPE, False),
                                      ("link", tarfile.SYMTYPE, False), ("file", tarfile.REGTYPE, True)]:
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w") as tar:
                for _ in range(2 if duplicate else 1):
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    tar.addfile(info, io.BytesIO())
            with tempfile.TemporaryDirectory() as temporary, self.assertRaises(runner.RunnerBlocked):
                runner.unpack(raw.getvalue(), Path(temporary))

    def test_environment_tree_and_attempt_are_bound_before_execution(self):
        spec = synthetic_spec()
        mid = str(uuid.uuid4())
        frozen = {"tree_sha": "a" * 40, "tree_artifact_id": "b" * 64}
        contract = functional.contract_for(mid, frozen, spec)
        self.assertEqual(contract, functional.contract_for(mid, frozen, spec))
        for key in ("tree_sha", "tree_artifact_id"):
            different = functional.contract_for(mid, {**frozen, key: "c" * len(frozen[key])}, spec)
            self.assertNotEqual(contract["attempt_id"], different["attempt_id"])
        other = copy.deepcopy(spec)
        other["runtime"]["python_version"] = "3.12.99"
        self.assertNotEqual(contract["attempt_id"], functional.contract_for(mid, frozen, other)["attempt_id"])

    def test_lost_attempt_does_not_clean_a_different_docker_endpoint(self):
        helper, mid, *_ = self.policy_fixture()
        frozen = fleet_json.loads((helper.runs / "missions" / mid / "candidate-freeze.json").read_bytes())
        contract = functional.contract_for(mid, frozen, synthetic_spec())
        key = fleet_artifacts.put_bytes(helper.runs, mid, state.canonical_bytes(contract))["artifact_id"]
        state.append_event(helper.runs, mid, kind="functional_check_started", actor="CONTROL", idempotency_key="functional:started",
            payload={"contract_artifact_id":key,"attempt_id":contract["attempt_id"],"tree_sha":frozen["tree_sha"]})
        with mock.patch.object(runner,"Docker") as constructor:
            constructor.return_value.endpoint="unix:///foreign-docker.sock"
            receipt=functional.run(helper.runs,mid,frozen)
            constructor.return_value.cleanup.assert_not_called()
        self.assertEqual(receipt["status"],"indeterminate")
        self.assertIn("cleanup_unconfirmed",receipt["reason"])


@unittest.skipUnless(os.environ.get("FLEET_FUNCTIONAL_DOCKER_TESTS") == "1", "explicit local Docker verification lane")
class FunctionalDockerTests(unittest.TestCase):
    def test_control_pause_and_cancel_reconcile_functional_container(self):
        for action in ("pause", "cancel"):
            with self.subTest(action=action):
                code = GOOD if action == "pause" else "import time\ntime.sleep(5)\n" + GOOD
                helper, mid, frozen, *_ = self.fixture(code)
                count = 0
                def interrupt():
                    nonlocal count
                    count += 1
                    if count == (1 if action == "pause" else 3):
                        control.request(helper.runs, mid, action=action, reason="functional synthetic control",
                                        idempotency_key="functional-control")
                    return "mission_cancel_requested" if control.view(fleet_mission.load_state(helper.runs, mid))["desired"] == "cancel_requested" else None
                receipt = functional.run(helper.runs, mid, frozen, interrupt=interrupt)
                self.assertEqual(receipt["status"], "passed" if action == "pause" else "indeterminate", receipt)
                result = driver._Driver(helper.runs, mid).control_stop()
                self.assertEqual(result["control"]["applied"], "paused" if action == "pause" else "cancelled")
                self.assertTrue(runner.Docker().cleanup("fleet-functional-" + receipt["attempt_id"], receipt["attempt_id"]))

    def fixture(self, code=GOOD, modify_spec=None):
        helper = test_fleet_herdr_archive.HerdrSnapshotTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        (helper.repo / "sample_stats.py").write_text(code)
        (helper.repo / "result.json").write_text('{"status":"PASS","summary":"all tests passed"}\n')
        spec = functional.make_spec(TESTS)
        if modify_spec:
            modify_spec(spec)
        contract = {"schema_version": 1, "requirements": [{"id": "reported", "description": "reported output",
            "checks": [{"kind": "json_equals", "path": "result.json", "keys": ["status"], "expected": "PASS"}]}]}
        mid, roles, backend = helper.archive_fixture(runtime_options={"functional_contract": spec,
            "herdr_session": "functional-fixture"}, contract_override=contract)
        frozen = fleet_json.loads((helper.runs / "missions" / mid / "candidate-freeze.json").read_bytes())
        return helper, mid, frozen, roles, backend, spec

    def execute(self, code=GOOD, modify_spec=None):
        fixture = self.fixture(code, modify_spec)
        helper, mid, frozen, *_ = fixture
        return functional.run(helper.runs, mid, frozen), fixture

    def test_correct_code_passes_through_fleet_and_archives_with_idempotent_recovery(self):
        receipt, (helper, mid, frozen, roles, backend, spec) = self.execute()
        self.assertEqual(receipt["status"], "passed", receipt)
        with mock.patch.object(runner, "execute", side_effect=AssertionError("no second physical run")):
            self.assertEqual(functional.run(helper.runs, mid, frozen), receipt)
        staged = archive.create(helper.runs, mid, helper.repo, roles, backend)
        # A v4 -> v3 downgrade cannot hide the functional requirement frozen in
        # the Mission ledger, even before an archive anchor exists.
        index_path = Path(staged["path"])
        raw = index_path.read_bytes()
        downgraded = fleet_json.loads(raw)
        downgraded["schema_version"] = 3
        downgraded["entries"].pop("functional-result.json")
        index_path.write_bytes(state.canonical_bytes(downgraded) + b"\n")
        with self.assertRaisesRegex(archive.HerdrArchiveError, "functional contract/schema"):
            driver._Driver(helper.runs, mid).complete_archive(archive, staged)
        index_path.write_bytes(raw)
        result = driver._Driver(helper.runs, mid).complete_archive(archive, staged)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["archive"]["archive_schema_version"], 4)
        self.assertEqual(result["archive"]["functional"], receipt)
        self.assertIn("not_reexecution", result["archive"]["functional_verification_scope"])
        self.assertEqual(archive.verify(helper.runs, mid)["functional"]["status"], "passed")

    def test_broken_code_fails_despite_correct_result_json_and_role_summaries(self):
        receipt, (helper, mid, frozen, roles, backend, spec) = self.execute(BROKEN)
        self.assertEqual(receipt["status"], "failed", receipt)
        options = fleet_json.loads((helper.runs / "missions" / mid / "runtime-options.json").read_bytes())
        predicate = fleet_acceptance.evaluate(options["acceptance_contract"],
            fleet_artifacts.get_bytes(helper.runs, mid, frozen["tree_artifact_id"]), mission_id=mid, final_sha=frozen["tree_sha"])
        self.assertEqual(predicate["status"], "accepted")
        self.assertTrue(all(r["status"] == "PASS" for r in roles.values()))
        with self.assertRaisesRegex(functional.FunctionalError, "did not pass"):
            archive.create(helper.runs, mid, helper.repo, roles, backend)
        self.assertEqual(fleet_mission.load_state(helper.runs, mid)["status"], "completing")

    def test_missing_tests_or_incompatible_runtime_are_blocked(self):
        for change in (lambda s: s["tests"].update(path="/nonexistent/fleet-original-tests.py"),
                       lambda s: s["runtime"].update(python_version="0.0.0")):
            receipt, _ = self.execute(modify_spec=change)
            self.assertEqual(receipt["status"], "blocked", receipt)

    def test_interruption_and_timeout_are_indeterminate(self):
        for code in ("import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n", "import time\ntime.sleep(5)\n"):
            receipt, _ = self.execute(code, lambda s: s["limits"].update(wall_seconds=1))
            self.assertEqual(receipt["status"], "indeterminate", receipt)

    def test_network_syscall_is_killed_and_not_accepted(self):
        receipt, (helper, mid, *_rest) = self.execute("import socket\nsocket.socket()\n" + GOOD)
        self.assertEqual(receipt["status"], "failed", receipt)
        raw = fleet_artifacts.get_bytes(helper.runs, mid, receipt["evidence"]["container-state.json"])
        self.assertEqual(fleet_json.loads(raw)["ExitCode"], 159)  # SIGSYS from the stacked socket filter.

    def test_zero_exit_without_results_is_not_passed_and_host_env_is_not_inherited(self):
        receipt, _ = self.execute("import os\nos._exit(0)\n")
        self.assertEqual(receipt["status"], "failed")
        with mock.patch.dict(os.environ, {"FLEET_SYNTHETIC_SECRET": "SYNTHETIC_ONLY"}):
            receipt, (helper, mid, *_rest) = self.execute(GOOD)
        self.assertEqual(receipt["status"], "passed", receipt)
        hello = fleet_json.loads(fleet_artifacts.get_bytes(helper.runs, mid, receipt["evidence"]["stdout.txt"]).splitlines()[0])
        self.assertNotIn("FLEET_SYNTHETIC_SECRET", hello["env"])

    def test_forged_rpc_answers_cannot_pass_without_executing_stats(self):
        answers = []
        for values in runner.CALLS:
            invalid = any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values)
            error = "TypeError" if invalid else "ValueError" if not values else None
            answers.append({"error": error, "input_after": values, "value": None if error else
                {"count": len(values), "sum": sum(values), "mean": sum(values) / len(values)}})
        forged = "import os\nprint(" + repr(json.dumps({"kind": "answers", "answers": answers})) + ", flush=True)\nos._exit(0)\n" + BROKEN
        receipt, _ = self.execute(forged)
        self.assertEqual(receipt["status"], "blocked", receipt)
        self.assertEqual(receipt["reason"], "unsupported_candidate_bridge_source")

    def test_lost_attempt_is_indeterminate_and_never_replayed(self):
        helper, mid, frozen, _roles, _backend, spec = self.fixture()
        contract = functional.contract_for(mid, frozen, spec)
        key = fleet_artifacts.put_bytes(helper.runs, mid, state.canonical_bytes(contract))["artifact_id"]
        state.append_event(helper.runs, mid, kind="functional_check_started", actor="CONTROL", idempotency_key="functional:started",
            payload={"contract_artifact_id": key, "attempt_id": contract["attempt_id"], "tree_sha": frozen["tree_sha"]})
        with mock.patch.object(runner, "execute", side_effect=AssertionError("lost attempt must not restart")):
            receipt = functional.run(helper.runs, mid, frozen)
            self.assertEqual(receipt["status"], "indeterminate")
            self.assertEqual(functional.run(helper.runs, mid, frozen), receipt)

    def test_host_decoys_and_external_writes_are_denied(self):
        with tempfile.TemporaryDirectory(prefix="fleet-synthetic-secrets-") as temporary:
            decoy = Path(temporary) / "config-credentials-ledger.txt"
            decoy.write_text("SYNTHETIC_ONLY_DO_NOT_EXPOSE")
            for operation in (f"open({str(decoy)!r}).read()", f"open({str(decoy)!r}, 'w').write('forged')",
                              "open('/candidate/sample_stats.py', 'w').write('forged')",
                              "open('/harness/fleet_stats_guest.py', 'w').write('forged')"):
                with self.subTest(operation=operation):
                    receipt, (helper, mid, *_rest) = self.execute(operation + "\n" + GOOD)
                    self.assertEqual(receipt["status"], "failed", receipt)
                    stderr = fleet_artifacts.get_bytes(helper.runs, mid, receipt["evidence"]["stderr.txt"])
                    self.assertNotIn(b"SYNTHETIC_ONLY_DO_NOT_EXPOSE", stderr)
                    self.assertTrue(b"FileNotFoundError" in stderr or b"Read-only file system" in stderr)
                    self.assertEqual(decoy.read_text(), "SYNTHETIC_ONLY_DO_NOT_EXPOSE")

    def test_memory_process_file_and_output_limits_are_effective(self):
        cases = {"memory": "x = bytearray(512 * 1024 * 1024)\n",
            "processes": "import os, time\nfor _ in range(16):\n    if os.fork() == 0:\n        time.sleep(5)\n        os._exit(0)\n",
            "file": "open('/tmp/large', 'wb').write(b'x' * (2 * 1024 * 1024))\n",
            "tmp": "for i in range(10):\n    open('/tmp/f'+str(i),'wb').write(b'x' * 1048576)\n",
            "output": "print('x' * 1000000)\n"}
        for label, code in cases.items():
            with self.subTest(label=label):
                receipt, (helper, mid, *_rest) = self.execute(code + GOOD)
                self.assertEqual(receipt["status"], "failed", receipt)
                if label == "memory":
                    self.assertTrue(fleet_json.loads(fleet_artifacts.get_bytes(helper.runs, mid,
                        receipt["evidence"]["container-state.json"]))["OOMKilled"])
                if label == "output":
                    self.assertEqual(receipt["reason"], "output_limit")
                self.assertLessEqual(sum(len(fleet_artifacts.get_bytes(helper.runs, mid, key)) for name, key in receipt["evidence"].items()
                                         if name in {"stdout.txt", "stderr.txt"}), runner.LIMITS["output_bytes"])

    def test_borrowed_tree_environment_attempt_or_runtime_receipts_are_rejected(self):
        receipt, (helper, mid, frozen, _roles, _backend, spec) = self.execute()
        contract = functional.contract_for(mid, frozen, spec)
        read = lambda key: fleet_artifacts.get_bytes(helper.runs, mid, key)
        for field in ("tree_sha", "environment_sha256", "attempt_id", "contract_sha256"):
            altered = {**receipt, field: "foreign"}
            with self.subTest(field=field), self.assertRaises(functional.FunctionalError):
                functional.verify_receipt(contract, altered, read)
        altered = copy.deepcopy(receipt)
        config = fleet_json.loads(read(receipt["evidence"]["container-config.json"]))
        config["Config"]["Labels"]["fleet.functional.attempt"] = str(uuid.uuid4())
        altered["evidence"]["container-config.json"] = fleet_artifacts.put_bytes(helper.runs, mid, state.canonical_bytes(config))["artifact_id"]
        with self.assertRaises(functional.FunctionalError):
            functional.verify_receipt(contract, altered, read)

    def test_driver_requires_functional_pass_before_synthesis(self):
        for code, expected in ((GOOD, "succeeded"), (BROKEN, "failed")):
            with self.subTest(expected=expected):
                helper = test_fleet_herdr_mission.HerdrMissionTests()
                helper.setUp()
                try:
                    (helper.target / "sample_stats.py").write_text(code)
                    test_fleet_herdr_mission.git(helper.target, "add", "sample_stats.py")
                    test_fleet_herdr_mission.git(helper.target, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                               "commit", "--no-gpg-sign", "-m", "functional fixture")
                    helper.head = test_fleet_herdr_mission.git(helper.target, "rev-parse", "HEAD")
                    options = fleet_json.loads((helper.runs / "missions" / helper.mid / "runtime-options.json").read_bytes())
                    options["functional_contract"] = functional.make_spec(TESTS)
                    helper.mid = helper.create(options=options, key="functional-driver")
                    with mock.patch.object(driver, "_archive", return_value=archive):
                        result = helper.run_driver()
                    self.assertEqual(result["status"], expected, result)
                    if expected == "failed":
                        self.assertEqual(helper.submitted(), ["plan", "build", "review", "verify"])
                    else:
                        self.assertEqual(result["archive"]["functional"]["status"], "passed")
                finally:
                    helper.doCleanups()
