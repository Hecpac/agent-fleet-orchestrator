"""Launcher component tests; executable fixture is not Codex/sandbox evidence."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid
from unittest import mock

from tests import test_fleet_herdr_runtime as runtime_fixtures
from tests import test_fleet_herdr_mission as fixtures

import fleet_herdr_launch as launch
import fleet_herdr_binding as identity
import fleet_herdr_permissions as permissions
import fleet_artifacts
import fleet_json
import fleet_safe_paths
import fleet_herdr_mission as driver
from fleet_herdr import HerdrBackend


class LauncherTests(unittest.TestCase):
    create = fixtures.HerdrMissionTests.create

    def setUp(self):
        runtime_fixtures.RuntimeMissionTests.setUp(self)
        self.controller = driver._Driver(self.runs, self.mid)
        self.controller.load()
        self.controller.prepare_candidate()
        self.controller.event("fleet_boot_started", "boot", {"feature": "driver-test", "preset": "astra_sol"})
        self.candidate = self.controller.candidate
        self.binary = self.tmp / "codex-executable-fixture"
        self.binary.write_text("#!/bin/sh\nprintf 'status=accepted\\n'\nprintf '%s' \"${LAUNCH_SECRET-unset}\" > environment-marker\n")
        self.binary.chmod(0o700)
        self.manifest = {"version": 1,
            "codex": {"image": identity.path_identity(str(self.binary), directory=False), "version": "codex-cli 0.153.0"},
            "herdr": {"image": identity.path_identity(str(self.binary), directory=False), "version": "herdr 0.8.2"}}
        self.aid, self.generation = str(uuid.uuid4()), str(uuid.uuid4())
        self.member = {"instance_id": "worker", "generation": self.generation, "start_phase": "starting",
                       "start_attempts": [{"attempt_id": self.aid}]}
        self.argv = ["codex", *permissions.launch_flags("worker", str(self.candidate))]
        self.intent = launch.prepare(self.runs, self.mid, self.candidate, self.compiled, self.member, self.manifest, self.argv)
        self.member["start_attempts"][-1]["launch_intent"] = self.intent
        self.backend = {"mission_id": self.mid, "generation": self.generation, "members": [self.member]}
        self.save_backend()
        self.wrapper = launch.install_wrapper(self.runs, self.mid) / "codex"

    def save_backend(self):
        with fleet_safe_paths.RootedFS(self.runs) as fs:
            relative = Path("missions") / self.mid / "herdr-backend.json"
            write = fs.replace_regular if (self.runs / relative).exists() else fs.atomic_write
            write(relative, fleet_json.canonical_bytes(self.backend) + b"\n",
                  directory_modes=(0o700, 0o700), file_mode=0o600)

    def run_wrapper(self, argv=None, *, cwd=None, intent=None):
        intent = intent or self.intent
        return subprocess.run([str(self.wrapper), "--fleet-launch-intent", intent["path"], intent["sha256"],
                               *(self.argv[1:] if argv is None else argv)],
            cwd=cwd or self.candidate, env={"PATH": "/usr/bin:/bin", "LAUNCH_SECRET": "never-persist-this-sensitive-value",
                "PYTHONPATH": str(self.candidate), "HOME": str(self.tmp)}, text=True, capture_output=True, timeout=15)

    def test_real_exec_consumed_once_sanitized_and_non_authoritative(self):
        result = self.run_wrapper()
        self.assertEqual((result.returncode, result.stdout), (0, "status=accepted\n"), result.stderr)
        self.assertEqual((self.candidate / "environment-marker").read_text(), "unset")
        consumed = json.loads(Path(self.intent["path"]).with_name("consumed.json").read_bytes())
        raw = fleet_artifacts.get_bytes(self.runs, self.mid, consumed["artifact_id"])
        record = json.loads(raw)
        self.assertEqual(record["INTEGRATION_BINDING"], "NOT_VERIFIED")
        self.assertEqual(record["authority"], "none")
        self.assertIn("LAUNCH_SECRET", record["inherited_environment_names"])
        self.assertNotIn("LAUNCH_SECRET", record["environment_names"])
        self.assertNotIn(b"never-persist-this-sensitive-value", raw)
        self.assertEqual(self.run_wrapper().returncode, 126)
        self.member["start_attempts"][-1]["launch_observation_artifact_id"] = consumed["artifact_id"]
        run_id = str(uuid.uuid4())
        pin = launch.link_run(self.runs, self.mid, self.member, run_id, "a" * 64)
        linked = json.loads(fleet_artifacts.get_bytes(self.runs, self.mid, pin))
        self.assertEqual(linked["run_id"], run_id)
        self.assertEqual(linked["authority"], "none")
        self.member["generation"] = str(uuid.uuid4())
        with self.assertRaisesRegex(launch.LaunchError, "another attempt"):
            launch.link_run(self.runs, self.mid, self.member, run_id, "a" * 64)

    def test_changed_argv_extra_root_and_cwd_fail_before_exec(self):
        for args in (self.argv[1:] + ["--add-dir", str(self.runs)], self.argv[1:] + ["-c", "sandbox_workspace_write.network_access=true"]):
            self.assertEqual(self.run_wrapper(args).returncode, 126)
        self.assertEqual(self.run_wrapper(cwd=self.target).returncode, 126)
        self.assertFalse((self.candidate / "environment-marker").exists())

    def test_changed_intent_or_forged_pin_rejected(self):
        self.assertEqual(self.run_wrapper(intent={**self.intent, "sha256": "0" * 64}).returncode, 126)
        path = Path(self.intent["path"])
        path.write_bytes(path.read_bytes() + b" ")
        self.assertEqual(self.run_wrapper().returncode, 126)

    def test_previous_attempt_after_recovery_and_other_generation_rejected(self):
        self.member["start_attempts"].append({"attempt_id": str(uuid.uuid4()), "launch_intent": self.intent})
        self.save_backend()
        self.assertEqual(self.run_wrapper().returncode, 126)
        self.member["start_attempts"].pop()
        self.backend["generation"] = str(uuid.uuid4())
        self.save_backend()
        self.assertEqual(self.run_wrapper().returncode, 126)

    def test_other_mission_role_and_already_ready_rejected(self):
        original = copy.deepcopy(self.backend)
        for change in ("mission", "role", "phase"):
            self.backend = copy.deepcopy(original)
            if change == "mission": self.backend["mission_id"] = str(uuid.uuid4())
            if change == "role": self.backend["members"][0]["instance_id"] = "reviewer"
            if change == "phase": self.backend["members"][0]["start_phase"] = "started"
            self.save_backend()
            self.assertEqual(self.run_wrapper().returncode, 126)

    def test_binary_version_hash_and_alias_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["codex"]["version"] = "codex-cli 0.154.0"
        with self.assertRaises(launch.LaunchError): launch.validate_manifest(manifest)
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        self.assertEqual(self.run_wrapper().returncode, 126)
        with self.assertRaises(launch.LaunchError): launch.validate_manifest(self.manifest)
        alias = Path(self.intent["path"]).with_name("alias.json")
        alias.symlink_to(self.intent["path"])
        self.assertEqual(self.run_wrapper(intent={**self.intent, "path": str(alias)}).returncode, 126)

    def test_environment_path_alias_rejected(self):
        env = launch.role_environment(self.candidate, "worker", self.aid)
        tmp = Path(env["TMPDIR"])
        tmp.rename(tmp.with_name("old-tmp"))
        tmp.symlink_to(tmp.with_name("old-tmp"), target_is_directory=True)
        self.assertEqual(self.run_wrapper().returncode, 126)

    def test_caller_cannot_repin_modified_environment(self):
        path = Path(self.intent["path"])
        capsule = json.loads(path.read_bytes())
        capsule["environment"]["HOME"] = str(self.target)
        raw = fleet_json.canonical_bytes(capsule)
        path.write_bytes(raw)
        intent = {**self.intent, "sha256": launch.digest(capsule)}
        # Even a repin in backend cannot make an unexpected env policy valid.
        self.member["start_attempts"][-1]["launch_intent"] = intent
        self.save_backend()
        self.assertEqual(self.run_wrapper(intent=intent).returncode, 126)

    def test_policy_or_candidate_divergence_fails_even_after_repin(self):
        path = Path(self.intent["path"])
        original = json.loads(path.read_bytes())
        for change in ("policy", "candidate"):
            capsule = copy.deepcopy(original)
            if change == "policy":
                capsule["requested_policy"]["sandbox_policy"]["network_access"] = True
            else:
                capsule["candidate"] = identity.path_identity(str(self.target), directory=True)
            raw = fleet_json.canonical_bytes(capsule)
            path.write_bytes(raw)
            intent = {**self.intent, "sha256": launch.digest(capsule)}
            self.member["start_attempts"][-1]["launch_intent"] = intent
            self.save_backend()
            self.assertEqual(self.run_wrapper(intent=intent).returncode, 126)

    def test_manifest_downgrade_rejected_by_real_backend(self):
        backend = HerdrBackend(self.runs, self.mid, session="fixture", feature="driver-test",
            target_repo=self.candidate, compiled=self.compiled, launch_manifest=self.manifest,
            environment={"PATH": "/usr/bin:/bin"})
        current = backend._initial_state()
        backend.launch_manifest = None
        with self.assertRaisesRegex(Exception, "downgraded"):
            backend._validate_state(current)

    def test_worker_cannot_drop_frozen_policy_after_repin(self):
        path = Path(self.intent["path"])
        capsule = json.loads(path.read_bytes())
        capsule["frozen_policy_sha256"] = None
        path.write_bytes(fleet_json.canonical_bytes(capsule))
        intent = {**self.intent, "sha256": launch.digest(capsule)}
        self.member["start_attempts"][-1]["launch_intent"] = intent
        self.save_backend()
        self.assertEqual(self.run_wrapper(intent=intent).returncode, 126)
        self.assertFalse((self.candidate / "environment-marker").exists())

    def test_empty_explicit_environment_does_not_inherit_credentials_or_path(self):
        with mock.patch.dict(os.environ, {"LAUNCH_SECRET": "ambient-secret"}), self.assertRaisesRegex(Exception, "PATH"):
            HerdrBackend(self.runs, self.mid, session="fixture", feature="driver-test",
                         target_repo=self.candidate, compiled=self.compiled, environment={})
