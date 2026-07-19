from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_ledger_bootstrap  # noqa: E402


class FleetLedgerBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.runs = Path(self.tempdir.name) / "runs"
        self.runs.mkdir(mode=0o700)
        self.runs.chmod(0o700)
        self.feature = "bootstrap-test"
        self.ledger = self.runs / f"fleet-{self.feature}.ledger.jsonl"
        self.intent = self.runs / f".fleet-{self.feature}.ledger-bootstrap.json"

    def write_pending(
        self,
        *,
        feature: str,
        value: dict[str, object],
        digest: str | None = None,
    ) -> Path:
        _, intent_leaf = fleet_ledger_bootstrap._names(feature)
        payload = fleet_ledger_bootstrap._intent_bytes(value)
        content_digest = digest or fleet_ledger_bootstrap.hashlib.sha256(
            payload
        ).hexdigest()
        path = self.runs / (
            fleet_ledger_bootstrap._pending_prefix(intent_leaf)
            + content_digest
            + ".tmp"
        )
        path.write_bytes(payload)
        path.chmod(0o600)
        return path

    def test_fresh_ensure_binds_empty_private_ledger_and_retry_keeps_inode(self) -> None:
        attempt = fleet_ledger_bootstrap.ensure(self.runs, self.feature)
        before = self.ledger.stat()
        self.assertEqual(self.ledger.read_bytes(), b"")
        self.assertEqual(before.st_mode & 0o777, 0o600)
        self.assertEqual(before.st_nlink, 1)
        self.assertEqual(fleet_ledger_bootstrap.ensure(self.runs, self.feature), attempt)
        after = self.ledger.stat()
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
        intent = json.loads(self.intent.read_text(encoding="utf-8"))
        self.assertEqual(intent["state"], "bound")
        self.assertEqual(intent["ledger_dev"], before.st_dev)
        self.assertEqual(intent["ledger_ino"], before.st_ino)

    def test_arbitrary_prior_empty_ledger_is_never_adopted(self) -> None:
        self.ledger.write_bytes(b"")
        self.ledger.chmod(0o600)
        before = self.ledger.stat()
        with self.assertRaisesRegex(
            fleet_ledger_bootstrap.BootstrapError, "must be absent"
        ):
            fleet_ledger_bootstrap.ensure(self.runs, self.feature)
        after = self.ledger.stat()
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
        self.assertFalse(self.intent.exists())

    def test_sigkill_after_ledger_publication_recovers_only_through_intent(self) -> None:
        env = os.environ.copy()
        env["FLEET_TEST_LEDGER_BOOT_CRASH_AT"] = "after_ledger_create"
        crashed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "fleet_ledger_bootstrap.py"),
                "ensure",
                "--runs-dir",
                str(self.runs),
                "--feature",
                self.feature,
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(crashed.returncode, -9, crashed.stderr)
        self.assertTrue(self.intent.exists())
        self.assertEqual(self.ledger.read_bytes(), b"")
        before = self.ledger.stat()
        fleet_ledger_bootstrap.ensure(self.runs, self.feature)
        after = self.ledger.stat()
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))

    def test_sigkill_after_pending_intent_fsync_adopts_exact_prepared_payload(
        self,
    ) -> None:
        env = os.environ.copy()
        env["FLEET_TEST_SAFE_PATH_CRASH_AT"] = "after_atomic_pending_fsync"
        crashed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "fleet_ledger_bootstrap.py"),
                "ensure",
                "--runs-dir",
                str(self.runs),
                "--feature",
                self.feature,
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(crashed.returncode, -9, crashed.stderr)
        self.assertFalse(self.intent.exists())
        self.assertFalse(self.ledger.exists())
        pending = list(
            self.runs.glob(
                fleet_ledger_bootstrap._pending_prefix(self.intent.name) + "*.tmp"
            )
        )
        self.assertEqual(len(pending), 1)
        pending_payload = pending[0].read_bytes()
        pending_value = fleet_ledger_bootstrap._decode_intent(
            pending_payload,
            feature=self.feature,
            ledger=self.ledger.name,
        )

        attempt = fleet_ledger_bootstrap.ensure(self.runs, self.feature)

        self.assertEqual(attempt, pending_value["attempt_id"])
        self.assertFalse(pending[0].exists())
        self.assertTrue(self.intent.exists())
        self.assertTrue(self.ledger.exists())
        self.assertEqual(
            json.loads(self.intent.read_text(encoding="utf-8"))["attempt_id"],
            pending_value["attempt_id"],
        )

    def test_pending_intent_recovery_rejects_every_ambiguous_or_unbound_claim(
        self,
    ) -> None:
        cases = ("ambiguous", "hash-mismatch", "wrong-feature", "not-prepared")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                feature = f"pending-{index}"
                ledger_leaf, intent_leaf = fleet_ledger_bootstrap._names(feature)
                value = fleet_ledger_bootstrap._new_intent(feature, ledger_leaf)
                if case == "ambiguous":
                    self.write_pending(feature=feature, value=value)
                    second = fleet_ledger_bootstrap._new_intent(feature, ledger_leaf)
                    self.write_pending(feature=feature, value=second)
                    expected = "ambiguous"
                elif case == "hash-mismatch":
                    self.write_pending(feature=feature, value=value, digest="0" * 64)
                    expected = "name/content hash mismatch"
                elif case == "wrong-feature":
                    value["feature"] = "another-feature"
                    self.write_pending(feature=feature, value=value)
                    expected = "binding mismatch"
                else:
                    value.update({"state": "bound", "ledger_dev": 1, "ledger_ino": 1})
                    self.write_pending(feature=feature, value=value)
                    expected = "not prepared"
                before = {
                    path.name: path.read_bytes()
                    for path in self.runs.iterdir()
                    if path.is_file()
                }

                with self.assertRaisesRegex(
                    fleet_ledger_bootstrap.BootstrapError, expected
                ):
                    fleet_ledger_bootstrap.ensure(self.runs, feature)

                after = {
                    path.name: path.read_bytes()
                    for path in self.runs.iterdir()
                    if path.is_file()
                }
                self.assertEqual(after, before)
                self.assertFalse((self.runs / intent_leaf).exists())
                self.assertFalse((self.runs / ledger_leaf).exists())
                for path in list(self.runs.iterdir()):
                    if path.is_file():
                        path.unlink()

    def test_pending_intent_is_never_adopted_over_an_existing_empty_ledger(
        self,
    ) -> None:
        value = fleet_ledger_bootstrap._new_intent(self.feature, self.ledger.name)
        pending = self.write_pending(feature=self.feature, value=value)
        self.ledger.write_bytes(b"")
        self.ledger.chmod(0o600)
        before = {
            pending.name: pending.read_bytes(),
            self.ledger.name: self.ledger.read_bytes(),
        }

        with self.assertRaisesRegex(
            fleet_ledger_bootstrap.BootstrapError, "must be absent"
        ):
            fleet_ledger_bootstrap.ensure(self.runs, self.feature)

        self.assertFalse(self.intent.exists())
        self.assertEqual(pending.read_bytes(), before[pending.name])
        self.assertEqual(self.ledger.read_bytes(), before[self.ledger.name])

    def test_cleanup_is_bound_and_never_removes_changed_evidence(self) -> None:
        fleet_ledger_bootstrap.ensure(self.runs, self.feature)
        self.ledger.write_text('{"status":"running"}\n', encoding="utf-8")
        with self.assertRaises(fleet_ledger_bootstrap.BootstrapError):
            fleet_ledger_bootstrap.cleanup(self.runs, self.feature)
        self.assertTrue(self.ledger.exists())
        self.assertTrue(self.intent.exists())

    def test_cleanup_and_manifest_handoff_retire_intent(self) -> None:
        fleet_ledger_bootstrap.ensure(self.runs, self.feature)
        fleet_ledger_bootstrap.cleanup(self.runs, self.feature)
        self.assertFalse(self.ledger.exists())
        self.assertFalse(self.intent.exists())

        fleet_ledger_bootstrap.ensure(self.runs, self.feature)
        fleet_ledger_bootstrap.handoff(self.runs, self.feature)
        fleet_ledger_bootstrap.complete(self.runs, self.feature)
        self.assertTrue(self.ledger.exists())
        self.assertFalse(self.intent.exists())

    def test_stale_handoff_after_completed_teardown_can_start_fresh(self) -> None:
        fleet_ledger_bootstrap.ensure(self.runs, self.feature)
        fleet_ledger_bootstrap.handoff(self.runs, self.feature)
        old_descriptor = os.open(self.ledger, os.O_RDONLY)
        self.addCleanup(os.close, old_descriptor)
        self.ledger.unlink()

        fleet_ledger_bootstrap.ensure(self.runs, self.feature)
        self.assertTrue(self.intent.exists())
        self.assertTrue(self.ledger.exists())
        # ext4 reuses freed inode numbers immediately, so st_ino inequality
        # cannot prove freshness; the old object holding zero links while the
        # path exists again is the platform-neutral proof of a new ledger.
        self.assertEqual(os.fstat(old_descriptor).st_nlink, 0)


if __name__ == "__main__":
    unittest.main()
