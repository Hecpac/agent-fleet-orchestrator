"""Admission tests: denied handoff is not evidence of an OS sandbox.

Canaries use only temporary files, synthetic credentials and loopback. No
Herdr server, Codex image, model, provider or global setting is exercised.
"""
import copy
from pathlib import Path
import socket
import subprocess
import sys
import unittest
import uuid
from unittest import mock

from tests import test_fleet_herdr as backend_fixtures
from tests import test_fleet_herdr_launch as launch_fixtures
import fleet_herdr as herdr
import fleet_herdr_launch as launch
import fleet_herdr_binding as identity
import fleet_herdr_native as native


class TransportAdmissionTests(unittest.TestCase):
    setUp = backend_fixtures.HerdrBackendTests.setUp
    backend = backend_fixtures.HerdrBackendTests.backend
    prompt = backend_fixtures.HerdrBackendTests.prompt
    write_active_transcript = backend_fixtures.HerdrBackendTests.write_active_transcript

    def real_backend(self, version=None):
        backend = self.backend()
        # The existing fixtures normally replace this real effect boundary.
        backend.run_command = backend._default_run
        if version is not None:
            backend.launch_manifest = {"version": version,
                "codex": {"image": {"realpath": "/pinned/codex"}},
                "herdr": {"image": {"realpath": "/pinned/herdr"}}}
        return backend

    def test_all_live_profiles_refuse_handoff_before_subprocess(self):
        operations = [
            ["agent", "start", "worker", "--kind", "codex", "--", "--sandbox", "workspace-write"],
            ["agent", "prompt", "worker", "filesystem: write candidate/result"],
            ["agent", "prompt", "worker", "network: connect to endpoint"],
            ["agent", "prompt", "worker", "processes: spawn child"],
            ["agent", "prompt", "worker", "credentials: read auth"],
            ["workspace", "create", "--cwd", str(self.target)],
            ["pane", "split", "pane-1"],
            ["agent", "send-keys", "worker", "enter"],
            ["agent", "future-effect", "worker"],
        ]
        for version in (None, 1, 2):
            backend = self.real_backend(version)
            for operation in operations:
                with self.subTest(version=version, operation=operation[:2]), mock.patch.object(
                        herdr.subprocess, "run") as spawn:
                    with self.assertRaisesRegex(herdr.HerdrBackendError,
                            "filesystem, network, processes, credentials"):
                        backend._command(["herdr", *operation])
                    spawn.assert_not_called()

    def test_observation_and_exact_cancellation_remain_available(self):
        commands = [
            ["codex", "--version"], ["herdr", "--version"],
            ["herdr", "agent", "get", "worker"],
            ["herdr", "agent", "read", "worker", "--source", "visible"],
            ["herdr", "agent", "wait", "worker", "--timeout", "5000"],
            ["herdr", "agent", "wait", "worker", "--until", "idle", "--until", "done",
             "--until", "blocked", "--timeout", "5000"],
            ["herdr", "agent", "send-keys", "worker", "ctrl+c"],
            ["herdr", "pane", "process-info", "--pane", "p1"],
            ["herdr", "pane", "get", "p1"],
            ["herdr", "workspace", "get", "w1"],
            ["herdr", "workspace", "close", "w1"],
        ]
        for version in (None, 1, 2):
            backend = self.real_backend(version)
            for command in commands:
                with self.subTest(version=version, command=command), mock.patch.object(
                        herdr.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as spawn:
                    backend._command(command)
                    spawn.assert_called_once()

    def test_unknown_arguments_routes_and_diagnostic_payload_fail_closed(self):
        backend = self.real_backend()
        malformed = [[], ["codex", "--version", "--", "payload"],
            ["herdr", "--session", "other", "agent", "get", "worker"],
            ["herdr", "--session", backend.session, "agent", "get", "--help"],
            ["herdr", "--session", backend.session, "agent", "get", "worker", "--exec", "payload"],
            ["herdr", "--session", backend.session, "agent", "send-keys", "worker", "ctrl+c", "enter"],
            ["herdr", "--session", backend.session, "agent", "wait", "worker", "--timeout", "-1"],
            ["herdr", "--session", backend.session, "agent", "get", "worker\0payload"]]
        for command in malformed:
            with self.subTest(command=command), mock.patch.object(herdr.subprocess, "run") as spawn:
                with self.assertRaises(herdr.HerdrBackendError):
                    backend._default_run(command, cwd=self.target, env={}, timeout=1)
                spawn.assert_not_called()

    def test_environment_flags_and_observer_claims_cannot_grant_execution(self):
        backend = self.real_backend(2)
        backend.environment.update(FLEET_NATIVE_ALLOW="1", FLEET_EXECUTION_PROFILE="native",
                                   FLEET_NATIVE_MEDIATION="verified")
        with mock.patch.object(herdr.subprocess, "run") as spawn:
            with self.assertRaises(herdr.HerdrBackendError):
                backend._command(["herdr", "agent", "start", "worker", "--", "--native-spawn-observer-fd=9"])
            spawn.assert_not_called()

    def test_wrapper_guard_precedes_broker_fork_and_native_exec(self):
        with mock.patch.object(sys, "argv", ["wrapper", "--fleet-launch-intent", "path", "pin"]), \
                mock.patch.object(launch, "consume", return_value=(["/native"], {})), \
                mock.patch.object(native.NativeLaunch, "start") as broker, \
                mock.patch.object(launch.os, "execve") as execute, \
                mock.patch("sys.stderr"):
            self.assertEqual(launch.main(), 126)
            broker.assert_not_called()
            execute.assert_not_called()

    def test_public_boot_and_retry_refuse_before_transport_or_start_intent(self):
        backend = self.real_backend()
        with mock.patch.object(herdr.subprocess, "run") as spawn:
            with self.assertRaisesRegex(herdr.HerdrBackendError, "native execution denied"):
                backend.boot()
            with self.assertRaisesRegex(herdr.HerdrBackendError, "native execution denied"):
                backend.retry_unsubmitted_start("worker")
            spawn.assert_not_called()
        self.assertFalse((self.runs / backend.relative).exists())

    def test_new_prompt_denied_without_ambiguous_submission_and_old_run_can_cancel(self):
        backend = self.backend()
        backend.boot()  # Explicit synthetic historical runtime.
        old_run = str(uuid.uuid4())
        prompt = self.prompt(old_run)
        backend.submit(old_run, prompt)
        self.write_active_transcript(member=backend.state()["members"][0], prompt=prompt)
        before = (self.runs / backend.relative).read_bytes()
        backend.run_command = backend._default_run
        new_run = str(uuid.uuid4())
        with mock.patch.object(herdr.subprocess, "run") as spawn:
            with self.assertRaisesRegex(herdr.HerdrBackendError, "native execution denied"):
                backend.submit(new_run, self.prompt(new_run))
            spawn.assert_not_called()
        self.assertEqual((self.runs / backend.relative).read_bytes(), before)
        self.fake.calls.clear()
        # Actual admission code, synthetic CLI receipts; no live cancellation.
        with mock.patch.object(herdr.subprocess, "run", side_effect=self.fake):
            self.assertEqual(backend.recover(old_run)["status"], "working")
            self.assertEqual(backend.cancel(old_run)["status"], "abandoned")
        commands = list(map(self.fake.operation, self.fake.calls))
        self.assertEqual(sum(c[1:3] == ["agent", "send-keys"] for c in commands), 1)
        self.assertFalse(any(c[1:3] in (["agent", "start"], ["agent", "prompt"]) for c in commands))


class WrapperCanaryTests(unittest.TestCase):
    setUp = launch_fixtures.LauncherTests.setUp
    create = launch_fixtures.LauncherTests.create
    save_backend = launch_fixtures.LauncherTests.save_backend
    run_wrapper = launch_fixtures.LauncherTests.run_wrapper

    def prepare_canary(self, source, version):
        self.binary.write_text(f"#!{sys.executable} -I\n" + source)
        self.manifest["codex"]["image"] = identity.path_identity(str(self.binary), directory=False)
        self.manifest["herdr"]["image"] = copy.deepcopy(self.manifest["codex"]["image"])
        self.manifest["version"] = version
        if version == 2:
            self.manifest["native_observer"] = "worker-v1"
        else:
            self.manifest.pop("native_observer", None)
        self.aid = str(uuid.uuid4())
        self.member["start_attempts"].append({"attempt_id": self.aid})
        self.intent = launch.prepare(self.runs, self.mid, self.candidate, self.compiled,
                                    self.member, self.manifest, self.argv)
        self.member["start_attempts"][-1]["launch_intent"] = self.intent
        self.save_backend()

    def test_executable_canaries_denied_before_each_effect_for_both_manifests(self):
        secret = self.tmp / "synthetic-credential"
        secret.write_text("SYNTHETIC-CREDENTIAL-ONLY")
        marker = self.tmp / "effect-marker"
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.05)
        endpoint = listener.getsockname()
        child_source = f"from pathlib import Path; Path({str(marker)!r}).write_text('child')"
        sources = {
            "filesystem": f"from pathlib import Path\nPath({str(marker)!r}).write_text('write')\n",
            "network": f"import socket\ns=socket.create_connection({endpoint!r}, timeout=1)\ns.close()\n",
            "processes": f"import subprocess\nsubprocess.run([{sys.executable!r}, '-I', '-c', {child_source!r}], check=True)\n",
            "credentials": f"from pathlib import Path\nPath({str(marker)!r}).write_text(Path({str(secret)!r}).read_text())\n",
        }
        for version in (1, 2):
            for effect, source in sources.items():
                with self.subTest(version=version, effect=effect):
                    self.prepare_canary(source, version)
                    # Positive control: the fixed local fixture actually causes
                    # this effect when the test harness executes it directly.
                    positive = subprocess.run([str(self.binary)], cwd=self.tmp,
                        env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=5)
                    self.assertEqual(positive.returncode, 0, positive.stderr)
                    if effect == "network":
                        connection, _ = listener.accept()
                        connection.close()
                    else:
                        expected = {"filesystem": "write", "processes": "child",
                                    "credentials": "SYNTHETIC-CREDENTIAL-ONLY"}[effect]
                        self.assertEqual(marker.read_text(), expected)
                        marker.unlink()
                    result = self.run_wrapper()
                    self.assertEqual(result.returncode, 126, result.stderr)
                    self.assertIn("native execution denied before effect", result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertFalse(marker.exists())
                    with self.assertRaises(socket.timeout):
                        listener.accept()
                    self.assertEqual(secret.read_text(), "SYNTHETIC-CREDENTIAL-ONLY")
                    self.assertNotIn("SYNTHETIC-CREDENTIAL-ONLY", result.stderr)
                    self.assertFalse((Path(self.intent["path"]).parent / "native").exists())


if __name__ == "__main__":
    unittest.main()
