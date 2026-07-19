from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_GUARD = ROOT / "scripts" / "fleet_manifest_guard.py"
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_manifest_guard  # noqa: E402


class FleetManifestGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runs = Path(self.temporary.name) / "runs"
        self.runs.mkdir(mode=0o700)
        self.runs.chmod(0o700)

    def test_cmux_uuid_accepts_stable_lower_or_upper_case_only(self) -> None:
        lower = "abcdef01-2345-4678-8abc-def012345678"
        upper = lower.upper()
        self.assertEqual(fleet_manifest_guard._workspace_uuid(lower), lower)
        self.assertEqual(fleet_manifest_guard._workspace_uuid(upper), upper)
        with self.assertRaisesRegex(
            fleet_manifest_guard.GuardError, "workspace_uuid is invalid"
        ):
            fleet_manifest_guard._workspace_uuid("Abcdef01-2345-4678-8abc-def012345678")

    def command(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(MANIFEST_GUARD), *arguments],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                **(environment or {}),
            },
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

    def test_control_json_identical_retry_is_idempotent_and_conflict_fails_closed(
        self,
    ) -> None:
        leaf = "archive-intent.json"
        first = {"schema_version": 1, "value": "first"}
        different = {"schema_version": 1, "value": "different"}
        root_fd = os.open(self.runs, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            fleet_manifest_guard._create_control_json(root_fd, leaf, first)
            before = (self.runs / leaf).lstat()
            fleet_manifest_guard._create_control_json(root_fd, leaf, first)
            after = (self.runs / leaf).lstat()
            self.assertEqual(
                (before.st_dev, before.st_ino),
                (after.st_dev, after.st_ino),
            )
            self.assertEqual(after.st_nlink, 1)
            with self.assertRaisesRegex(
                fleet_manifest_guard.GuardError,
                "conflicts with requested bytes",
            ):
                fleet_manifest_guard._create_control_json(root_fd, leaf, different)
        finally:
            os.close(root_fd)

        expected = json.dumps(
            first,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        self.assertEqual((self.runs / leaf).read_bytes(), expected)

    def test_canonical_json_reader_rejects_ambiguous_numbers_unicode_and_framing(
        self,
    ) -> None:
        self.assertEqual(
            fleet_manifest_guard._decode_canonical_json(b'{"value":1}\n', "control.json"),
            {"value": 1},
        )
        attacks = {
            "duplicate key": b'{"value":1,"value":2}\n',
            "non-finite JSON number": b'{"value":1e999}\n',
            "not valid Unicode": b'{"value":"\\ud800"}\n',
            "BOM is not allowed": b'\xef\xbb\xbf{"value":1}\n',
            "bytes are not canonical": b'{"value":1}\r\n',
            "not valid UTF-8": b'{"value":"\xff"}\n',
        }
        for expected, content in attacks.items():
            with self.subTest(expected=expected), self.assertRaisesRegex(
                fleet_manifest_guard.GuardError, expected
            ):
                fleet_manifest_guard._decode_canonical_json(content, "control.json")

    def test_sigkill_after_intent_install_keeps_prepare_and_recovery_readable(
        self,
    ) -> None:
        feature = "intent-crash"
        workspace_uuid = str(uuid.uuid4())
        manifest = self.runs / f"fleet-{feature}.manifest"
        content = (
            "schema_version=3\n"
            f"feature={feature}\n"
            f"workspace_uuid={workspace_uuid}\n"
        ).encode("utf-8")
        manifest.write_bytes(content)
        manifest.chmod(0o600)
        digest = hashlib.sha256(content).hexdigest()
        prepare = (
            "prepare-archive",
            "--runs-dir",
            str(self.runs),
            "--feature",
            feature,
            "--digest",
            digest,
        )

        killed = self.command(
            *prepare,
            environment={
                "FLEET_TEST_MANIFEST_GUARD_CRASH_AT": (
                    "after_archive_intent_install_before_cleanup"
                )
            },
        )
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stderr)

        intents = list(
            self.runs.glob(f".fleet-{feature}.*.archive-intent.json")
        )
        self.assertEqual(len(intents), 1)
        intent_path = intents[0]
        intent_info = intent_path.lstat()
        self.assertTrue(stat.S_ISREG(intent_info.st_mode))
        self.assertEqual(stat.S_IMODE(intent_info.st_mode), 0o600)
        self.assertEqual(intent_info.st_nlink, 1)
        self.assertEqual(intent_info.st_uid, os.geteuid())
        self.assertFalse(
            list(self.runs.glob(".fleet-atomic-*.tmp")),
            "exclusive rename must not leave a second link or pending alias",
        )
        intent = json.loads(intent_path.read_bytes())
        expected_bytes = json.dumps(
            intent,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        self.assertEqual(intent_path.read_bytes(), expected_bytes)
        self.assertEqual(intent["manifest_digest"], digest)
        archive_dir = str(intent["archive_dir"])
        self.assertTrue((self.runs / "archive" / archive_dir).is_dir())

        before_retry = intent_path.lstat()
        retried = self.command(*prepare)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(
            retried.stdout.strip(), f"{archive_dir}\t{intent_path.name}"
        )
        after_retry = intent_path.lstat()
        self.assertEqual(
            (before_retry.st_dev, before_retry.st_ino),
            (after_retry.st_dev, after_retry.st_ino),
        )
        self.assertEqual(after_retry.st_nlink, 1)

        archived_manifest = self.runs / "archive" / archive_dir / "manifest"
        manifest.rename(archived_manifest)
        recovered = self.command(
            "recover-archived",
            "--runs-dir",
            str(self.runs),
            "--feature",
            feature,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(
            recovered.stdout.strip(),
            f"{intent_path.name}\t{archive_dir}\t{digest}\t{workspace_uuid}",
        )

        cleared = self.command(
            "clear-archive",
            "--runs-dir",
            str(self.runs),
            "--feature",
            feature,
            "--intent",
            intent_path.name,
            "--archive-dir",
            archive_dir,
            "--digest",
            digest,
        )
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertFalse(intent_path.exists())

    def test_sigkill_after_pending_fsync_recovers_after_clock_tick(self) -> None:
        feature = "pending-crash"
        workspace_uuid = str(uuid.uuid4())
        manifest = self.runs / f"fleet-{feature}.manifest"
        content = (
            "schema_version=3\n"
            f"feature={feature}\n"
            f"workspace_uuid={workspace_uuid}\n"
        ).encode("utf-8")
        manifest.write_bytes(content)
        manifest.chmod(0o600)
        digest = hashlib.sha256(content).hexdigest()
        prepare = (
            "prepare-archive",
            "--runs-dir",
            str(self.runs),
            "--feature",
            feature,
            "--digest",
            digest,
        )

        killed = self.command(
            *prepare,
            environment={
                "FLEET_TEST_MANIFEST_GUARD_CRASH_AT": (
                    "after_archive_intent_pending_fsync_before_install"
                )
            },
        )
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stderr)
        self.assertFalse(
            list(self.runs.glob(f".fleet-{feature}.*.archive-intent.json"))
        )

        pending_paths = list(self.runs.glob(".fleet-atomic-*.tmp"))
        self.assertEqual(len(pending_paths), 1)
        pending_path = pending_paths[0]
        pending_bytes = pending_path.read_bytes()
        pending = json.loads(pending_bytes)
        expected_archive_dir = f"{feature}-{workspace_uuid}-{digest}"
        self.assertEqual(pending["archive_dir"], expected_archive_dir)
        pending_info = pending_path.lstat()
        self.assertEqual(pending_info.st_nlink, 1)
        self.assertEqual(stat.S_IMODE(pending_info.st_mode), 0o600)
        archive = self.runs / "archive" / expected_archive_dir
        self.assertTrue(archive.is_dir())
        self.assertFalse(list(archive.iterdir()))

        time.sleep(1.1)
        retried = self.command(*prepare)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        intent_paths = list(
            self.runs.glob(f".fleet-{feature}.*.archive-intent.json")
        )
        self.assertEqual(len(intent_paths), 1)
        intent_path = intent_paths[0]
        self.assertEqual(intent_path.read_bytes(), pending_bytes)
        intent_info = intent_path.lstat()
        self.assertEqual(
            (intent_info.st_dev, intent_info.st_ino),
            (pending_info.st_dev, pending_info.st_ino),
        )
        self.assertEqual(intent_info.st_nlink, 1)
        self.assertFalse(list(self.runs.glob(".fleet-atomic-*.tmp")))
        self.assertEqual(
            retried.stdout.strip(),
            f"{expected_archive_dir}\t{intent_path.name}",
        )

        manifest.rename(archive / "manifest")
        recovered = self.command(
            "recover-archived",
            "--runs-dir",
            str(self.runs),
            "--feature",
            feature,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(
            recovered.stdout.strip(),
            (
                f"{intent_path.name}\t{expected_archive_dir}\t{digest}\t"
                f"{workspace_uuid}"
            ),
        )

    def test_historical_timestamp_archive_remains_recoverable(self) -> None:
        feature = "legacy-archive"
        workspace_uuid = str(uuid.uuid4())
        content = (
            "schema_version=3\n"
            f"feature={feature}\n"
            f"workspace_uuid={workspace_uuid}\n"
        ).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        archive_dir = f"{feature}-20260717T120000Z"
        archive_root = self.runs / "archive"
        archive_root.mkdir(mode=0o700)
        archive_root.chmod(0o700)
        archive = archive_root / archive_dir
        archive.mkdir(mode=0o700)
        archive.chmod(0o700)
        archived_manifest = archive / "manifest"
        archived_manifest.write_bytes(content)
        archived_manifest.chmod(0o600)

        intent_path = (
            self.runs
            / f".fleet-{feature}.{workspace_uuid}.archive-intent.json"
        )
        intent = {
            "schema_version": 1,
            "feature": feature,
            "workspace_uuid": workspace_uuid,
            "archive_dir": archive_dir,
            "manifest_digest": digest,
        }
        intent_path.write_bytes(
            json.dumps(
                intent,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        intent_path.chmod(0o600)

        recovered = self.command(
            "recover-archived",
            "--runs-dir",
            str(self.runs),
            "--feature",
            feature,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(
            recovered.stdout.strip(),
            (
                f"{intent_path.name}\t{archive_dir}\t{digest}\t"
                f"{workspace_uuid}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
