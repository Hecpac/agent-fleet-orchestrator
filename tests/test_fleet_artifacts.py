from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests.mission_control_test_support import create_running_mission

import fleet_artifacts


class FleetArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs, self.mission_id, _ = create_running_mission(self.tmp, feature="artifacts")

    def test_artifact_id_is_exact_sha_and_retry_is_idempotent(self) -> None:
        first = fleet_artifacts.put_bytes(self.runs, self.mission_id, b"exact bytes\n")
        second = fleet_artifacts.put_bytes(self.runs, self.mission_id, b"exact bytes\n")
        self.assertEqual(first, second)
        self.assertEqual(
            first["artifact_id"],
            "6a77ce4ad94636f6120bb985066c1d75ce65b73f264a35f9d5ac910e252f0355",
        )
        self.assertEqual(
            fleet_artifacts.get_bytes(self.runs, self.mission_id, first["artifact_id"]),
            b"exact bytes\n",
        )

    def test_tampered_artifact_and_symlink_are_rejected(self) -> None:
        artifact = fleet_artifacts.put_bytes(self.runs, self.mission_id, b"original")
        path = Path(artifact["path"])
        path.write_bytes(b"tampered")
        with self.assertRaisesRegex(fleet_artifacts.ArtifactError, "do not match"):
            fleet_artifacts.get_bytes(self.runs, self.mission_id, artifact["artifact_id"])

        path.unlink()
        source = self.tmp / "outside"
        source.write_bytes(b"original")
        path.symlink_to(source)
        with self.assertRaises(fleet_artifacts.ArtifactError):
            fleet_artifacts.get_bytes(self.runs, self.mission_id, artifact["artifact_id"])

    def test_store_verifies_every_content_address(self) -> None:
        fleet_artifacts.put_bytes(self.runs, self.mission_id, b"one")
        fleet_artifacts.put_bytes(self.runs, self.mission_id, b"two")
        receipt = fleet_artifacts.verify_store(self.runs, self.mission_id)
        self.assertEqual(receipt["artifacts"], 2)
        self.assertEqual(receipt["bytes"], 6)
        self.assertTrue(receipt["valid"])


if __name__ == "__main__":
    unittest.main()
