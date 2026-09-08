"""Provider-free protocol tests; fixtures do not establish Worker binding."""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_native_spawn_channel as channel


class PrivateChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.server, self.client = socket.socketpair()
        self.owner = channel.ObservationChannel(self.server, self.root,
            process_id=os.getpid(), launch_sha256="12" * 32)

    def tearDown(self):
        self.owner.close()
        self.client.close()
        self.temp.cleanup()

    def request(self, **changes):
        value = {"schema_version":1, "authority":"none", "channel_id":self.owner.channel_id,
            "launch_sha256":self.owner.launch_sha256, "sequence":self.owner.sequence,
            "process_id":os.getpid(), "kind":"native-spawn-hello"}
        value.update(changes)
        return value

    def send(self, value, *, key=None):
        channel.write_frame(self.client, channel.signed(key or self.owner.key, channel.REQUEST, value),
                            time.monotonic() + 1)

    def test_receipt_is_durable_before_ack_and_keys_not_persisted(self):
        request = self.request()
        self.send(request)
        fsync = os.fsync
        observed = []
        def sync(fd):
            observed.append(fd)
            fsync(fd)
        def write(sock, value, deadline):
            receipt = (self.root / "0000000000000000.json").read_bytes()
            self.assertEqual(len(observed), 2)
            self.assertEqual(hashlib.sha256(receipt).hexdigest(), value["payload"]["durable_receipt_sha256"])
            original_write(sock, value, deadline)
        original_write = channel.write_frame
        with patch.object(channel.os, "fsync", side_effect=sync), patch.object(channel, "write_frame", side_effect=write):
            result = self.owner.receive(approve=lambda _: True)
        reply = channel.read_frame(self.client, time.monotonic() + 1)
        self.assertEqual(reply, channel.signed(self.owner.key, channel.ACK, reply["payload"]))
        self.assertEqual(result["request"], request)
        persisted = b"".join(path.read_bytes() for path in self.root.iterdir())
        self.assertNotIn(self.owner.key.hex().encode(), persisted)

    def test_wrong_authentication_never_persists_request(self):
        self.send(self.request(), key=b"wrong-key")
        with self.assertRaisesRegex(ValueError, "authentication"):
            self.owner.receive(approve=lambda _: True)
        self.assertEqual([p.name for p in self.root.iterdir()], ["owner.json"])
        self.assertTrue(self.owner.closed)

    def test_wrong_sequence_invalidates_channel_and_cannot_be_reused(self):
        self.send(self.request(sequence=1))
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            self.owner.receive(approve=lambda _: True)
        with self.assertRaisesRegex(RuntimeError, "consumed"):
            self.owner.receive(approve=lambda _: True)

    def test_wrong_launch_process_channel_and_bool_sequence_rejected(self):
        for field, value in [("launch_sha256", "34" * 32), ("process_id", os.getpid()+1),
                             ("channel_id", "34" * 32), ("sequence", False)]:
            with self.subTest(field=field):
                left, right = socket.socketpair()
                directory = self.root / field
                directory.mkdir(mode=0o700)
                owner = channel.ObservationChannel(left, directory, process_id=os.getpid(), launch_sha256="12"*32)
                payload = self.request(channel_id=owner.channel_id)
                payload[field] = value
                channel.write_frame(right, channel.signed(owner.key, channel.REQUEST, payload), time.monotonic()+1)
                with self.assertRaises(ValueError):
                    owner.receive(approve=lambda _: True)
                owner.close(); right.close()

    def test_recovery_cannot_reopen_consumed_journal(self):
        left, right = socket.socketpair()
        try:
            with self.assertRaises(FileExistsError):
                channel.ObservationChannel(left, self.root, process_id=os.getpid(), launch_sha256="12" * 32)
        finally:
            left.close(); right.close()

    def test_alias_and_symlink_journals_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        left, right = socket.socketpair()
        try:
            with self.assertRaisesRegex(ValueError, "path alias"):
                channel.ObservationChannel(left, alias, process_id=os.getpid(), launch_sha256="12" * 32)
            self.assertEqual(left.fileno(), -1)
        finally:
            left.close(); right.close()

    def test_durable_write_failure_sends_no_ack(self):
        self.send(self.request())
        with patch.object(channel.os, "fsync", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                self.owner.receive(approve=lambda _: True)
        self.assertEqual(self.client.recv(1), b"")

    def test_denial_is_durable_and_terminal(self):
        self.send(self.request())
        receipt = self.owner.receive(approve=lambda _: False)
        reply = channel.read_frame(self.client, time.monotonic()+1)
        self.assertEqual(receipt["decision"], "deny")
        self.assertEqual(reply["payload"]["decision"], "deny")
        self.assertTrue(self.owner.closed)

    def test_replay_does_not_create_second_receipt(self):
        request = self.request()
        self.send(request)
        self.owner.receive(approve=lambda _: True)
        channel.read_frame(self.client, time.monotonic()+1)
        self.send(request)
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            self.owner.receive(approve=lambda _: True)
        self.assertFalse((self.root / "0000000000000001.json").exists())

    def test_anchor_and_ack_share_the_trusted_transaction(self):
        active = []
        @contextmanager
        def transaction():
            active.append(True)
            yield
            active.clear()
        def commit(receipt):
            self.assertTrue(active)
            self.assertEqual(receipt["decision"], "allow")
            self.assertTrue((self.root / "0000000000000000.json").exists())
            return "ab"*32
        original = channel.write_frame
        def write(sock, frame, deadline):
            self.assertTrue(active)
            self.assertEqual(frame["payload"]["durable_receipt_sha256"], "ab"*32)
            self.assertEqual(json.loads((self.root / "0000000000000000-anchor.json").read_bytes()),
                             {"artifact_id": "ab"*32})
            original(sock, frame, deadline)
        self.owner.transaction, self.owner.commit = transaction, commit
        self.send(self.request())
        with patch.object(channel, "write_frame", side_effect=write):
            self.owner.receive(approve=lambda _: True)
        self.assertFalse(active)

    def test_failed_anchor_never_acknowledges_local_receipt(self):
        def fail(_):
            raise OSError("fixture anchor failure")
        self.owner.commit = fail
        self.send(self.request())
        with self.assertRaises(OSError):
            self.owner.receive(approve=lambda _: True)
        self.assertEqual(self.client.recv(1), b"")
        self.assertTrue((self.root / "0000000000000000.json").exists())
        self.assertFalse((self.root / "0000000000000000-anchor.json").exists())


if __name__ == "__main__":
    unittest.main()
