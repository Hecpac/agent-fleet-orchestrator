"""Provider-free CONTROL component tests; no fixture establishes Worker binding."""
import copy
import json
import os
from pathlib import Path
import socket
import time
import unittest
import uuid
from unittest import mock

from tests import test_fleet_herdr_launch as launch_fixtures
from fleet_herdr import HerdrBackend, HerdrBackendError
import fleet_artifacts as artifacts
import fleet_herdr_launch as launch
import fleet_herdr_native as native
import fleet_json
import fleet_mission_state as state
import fleet_native_spawn_channel as protocol
import fleet_safe_paths as paths


class NativeLaunchTests(unittest.TestCase):
    create = launch_fixtures.LauncherTests.create
    save_backend = launch_fixtures.LauncherTests.save_backend

    def setUp(self):
        launch_fixtures.LauncherTests.setUp(self)
        self.manifest.update(version=2, native_observer="worker-v1")
        self.aid = str(uuid.uuid4())
        self.member["start_attempts"].append({"attempt_id": self.aid})
        self.intent = launch.prepare(self.runs, self.mid, self.candidate, self.compiled,
                                     self.member, self.manifest, self.argv)
        self.member["start_attempts"][-1]["launch_intent"] = self.intent
        self.save_backend()
        self.adapter = native.NativeLaunch()
        self.addCleanup(self.adapter.close)
        previous = Path.cwd()
        try:
            os.chdir(self.candidate)
            self.actual_argv, self.env = launch.consume(self.intent["path"], self.intent["sha256"],
                                                       self.argv, native=self.adapter)
        finally:
            os.chdir(previous)
        self.launch_pin = self.adapter.launch_pin
        self.capsule = json.loads(Path(self.intent["path"]).read_bytes())
        self.publisher = native.Publisher(self.capsule, self.intent["path"], self.intent["sha256"],
                                          self.launch_pin, os.getpid())
        self.journal = Path(self.intent["path"]).parent / "fixture-journal"
        self.journal.mkdir(mode=0o700)
        self.server, self.client = socket.socketpair()
        self.addCleanup(self.client.close)
        self.owner = protocol.ObservationChannel(self.server, self.journal, process_id=os.getpid(),
            launch_sha256=self.launch_pin, transaction=self.publisher.transaction, commit=self.publisher.commit)
        self.addCleanup(self.owner.close)
        self.addCleanup(mock.patch.stopall)
        # Synthetic executable identity is explicitly a component fixture.
        mock.patch.object(native, "process_image", return_value=str(self.binary)).start()

    def current(self):
        return state.derive_state(state.read_events(state.ledger_path(self.runs, self.mid)))

    def exchange(self, *, allow=True, **extra):
        payload = {"schema_version": 1, "authority": "none", "channel_id": self.owner.channel_id,
            "launch_sha256": self.launch_pin, "sequence": self.owner.sequence,
            "process_id": os.getpid(), "kind": "native-spawn-hello" if self.owner.sequence == 0 else "native-spawn-request"}
        payload.update(extra)
        protocol.write_frame(self.client, protocol.signed(self.owner.key, protocol.REQUEST, payload), time.monotonic()+1)
        result = self.owner.receive(approve=lambda _: allow)
        ack = protocol.read_frame(self.client, time.monotonic()+1)["payload"]
        return result, ack

    def test_same_argv_channel_launch_cas_ledger_ack_remains_non_authoritative(self):
        before = self.current()
        lock = Path("missions") / self.mid / "herdr-backend.lock"
        # Regression for real boot holding its backend lock during HELLO.
        with paths.RootedFS(self.runs) as fs, fs.exclusive_lock(lock, directory_modes=(0o700, 0o700)):
            receipt, ack = self.exchange()
        observed = self.current()["herdr_native_observations"]
        self.assertEqual(observed[0]["artifact_id"], ack["durable_receipt_sha256"])
        record = json.loads(artifacts.get_bytes(self.runs, self.mid, observed[0]["artifact_id"]))
        self.assertEqual(record["launch_observation_artifact_id"], self.launch_pin)
        self.assertEqual(record["receipt"], receipt)
        self.assertEqual(record["INTEGRATION_BINDING"], "NOT_VERIFIED")
        self.assertIsNone(record["run_id"])
        self.assertEqual(self.current()["status"], before["status"])
        self.assertEqual(self.current()["admissions"], before["admissions"])
        launch_record = json.loads(artifacts.get_bytes(self.runs, self.mid, self.launch_pin))
        self.assertEqual(launch_record["argv_sha256"], launch.digest(self.actual_argv))
        self.assertEqual(launch_record["sanitized_environment_sha256"], launch.digest(self.env))
        self.assertTrue(self.actual_argv[1].startswith("--native-spawn-observer-fd="))
        self.assertEqual(native.require_bootstrap(self.runs, self.mid, self.member, self.launch_pin), observed[0]["artifact_id"])

    def test_missing_and_different_generation_bootstrap_refused(self):
        with self.assertRaises(native.NativeError):
            native.require_bootstrap(self.runs, self.mid, self.member, self.launch_pin)
        self.exchange()
        member = copy.deepcopy(self.member)
        for key in ("generation", "attempt_id"):
            altered = copy.deepcopy(member)
            target = altered if key == "generation" else altered["start_attempts"][-1]
            target[key] = str(uuid.uuid4())
            with self.assertRaises(native.NativeError):
                native.require_bootstrap(self.runs, self.mid, altered, self.launch_pin)

    def test_modified_cas_receipt_is_not_a_bootstrap(self):
        _, ack = self.exchange()
        # Resolve the actual CAS path from its writer instead of guessing layout.
        raw = artifacts.get_bytes(self.runs, self.mid, ack["durable_receipt_sha256"])
        path = Path(artifacts.put_bytes(self.runs, self.mid, raw)["path"])
        path.write_bytes(raw.replace(b'"authority":"none"', b'"authority":"forged"', 1))
        with self.assertRaises(artifacts.ArtifactError):
            native.require_bootstrap(self.runs, self.mid, self.member, self.launch_pin)

    def test_recovery_invalidates_existing_channel_before_ack(self):
        self.exchange()
        self.member["start_attempts"].append({"attempt_id": str(uuid.uuid4()), "launch_intent": self.intent})
        self.save_backend()
        with self.assertRaisesRegex(native.NativeError, "inactive"):
            self.exchange(allow=False)
        self.assertEqual(self.client.recv(1), b"")
        self.assertEqual(len(self.current()["herdr_native_observations"]), 1)

    def test_native_process_image_mismatch_sends_no_ack(self):
        with mock.patch.object(native, "process_image", return_value="/different/codex"):
            with self.assertRaisesRegex(native.NativeError, "image differs"):
                self.exchange()
        self.assertEqual(self.client.recv(1), b"")

    def test_extra_roots_or_environment_cannot_enable_tool_authority(self):
        self.exchange()
        receipt, ack = self.exchange(allow=False,
            resolved_policy={"writable_roots": [str(self.runs)]}, effective_environment={"unexpected": True})
        self.assertEqual((receipt["decision"], ack["decision"]), ("deny", "deny"))
        self.assertTrue(self.owner.closed)
        record = json.loads(artifacts.get_bytes(self.runs, self.mid, ack["durable_receipt_sha256"]))
        self.assertIsNone(record["run_id"])
        self.assertIn("native_effective_environment", record["missing"])
        with self.assertRaises(native.NativeError):
            native.require_bootstrap(self.runs, self.mid, self.member, self.launch_pin)

    def test_even_trusted_callback_cannot_authorize_incomplete_tool(self):
        self.exchange()
        with self.assertRaisesRegex(native.NativeError, "authorization is unavailable"):
            self.exchange(allow=True)
        self.assertEqual(self.client.recv(1), b"")
        self.assertEqual(len(self.current()["herdr_native_observations"]), 1)

    def test_native_run_link_rejects_other_run_and_missing_admission(self):
        self.exchange()
        self.member["start_attempts"][-1]["launch_observation_artifact_id"] = self.launch_pin
        run_id = str(uuid.uuid4())
        pin = launch.link_run(self.runs, self.mid, self.member, run_id, "ab"*32)
        submission = {"run_id": str(uuid.uuid4()), "generation": self.generation, "instance_id": "worker",
            "phase": "prepared", "launch_run_link_artifact_id": pin, "prompt_sha256": "ab"*32}
        self.backend["submissions"] = {run_id: submission}
        self.save_backend()
        with self.publisher.transaction():
            with self.assertRaisesRegex(native.NativeError, "run link differs"):
                self.publisher.run_link()
        submission["run_id"] = run_id
        self.save_backend()
        with self.publisher.transaction():
            with self.assertRaisesRegex(native.NativeError, "writer admission"):
                self.publisher.run_link()

    def test_ledger_rejects_duplicate_channel_other_mission_and_worker_author(self):
        self.exchange()
        payload = dict(self.current()["herdr_native_observations"][0])
        payload.pop("event_sha256")
        for change in ("replay", "mission", "actor"):
            altered = copy.deepcopy(payload)
            if change == "mission":
                altered["attempt"]["mission_id"] = str(uuid.uuid4())
            with self.assertRaises(state.MissionStateError):
                with state.MissionTransaction(self.runs, self.mid) as tx:
                    tx.append_event(kind="herdr_native_observed", actor="worker" if change == "actor" else "CONTROL",
                        idempotency_key="invalid-"+change, payload=altered)

    def test_real_backend_gate_requires_bootstrap_only_for_opt_in_worker(self):
        backend = HerdrBackend(self.runs, self.mid, session="fixture", feature="driver-test",
            target_repo=self.candidate, compiled=self.compiled, launch_manifest=self.manifest,
            environment={"PATH": "/usr/bin:/bin"})
        with self.assertRaises(HerdrBackendError):
            backend._require_native_bootstrap(self.member, self.launch_pin)
        self.exchange()
        self.assertIsInstance(backend._require_native_bootstrap(self.member, self.launch_pin), str)
        self.assertIsNone(backend._require_native_bootstrap({"instance_id": "reviewer"}, None))
        backend.launch_manifest = {**self.manifest, "version": 1}
        self.assertIsNone(backend._require_native_bootstrap(self.member, None))

    def test_cas_lock_contention_is_bounded_and_never_acknowledged(self):
        lock = Path("missions") / self.mid / ".artifacts.lock"
        started = time.monotonic()
        with paths.RootedFS(self.runs) as fs, fs.exclusive_lock(lock, directory_modes=(0o700, 0o700)):
            with self.assertRaises(TimeoutError):
                self.exchange()
        self.assertLess(time.monotonic()-started, 4)
        self.assertEqual(self.client.recv(1), b"")
        self.assertNotIn("herdr_native_observations", self.current())


if __name__ == "__main__":
    unittest.main()
