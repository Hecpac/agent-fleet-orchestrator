from __future__ import annotations

from pathlib import Path
import tempfile
import unittest


import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_manifest


class FleetManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "fleet-demo.manifest"

    def test_legacy_defaults_are_explicit_and_migration_does_not_invent_provenance(self) -> None:
        self.path.write_text(
            "schema_version=3\nfeature=demo\nworkspace=workspace:1\n",
            encoding="utf-8",
        )
        loaded = fleet_manifest.load(self.path)
        self.assertEqual(loaded["manifest_contract_version"], "1")
        self.assertEqual(loaded["execution_profile"], "native")
        self.assertEqual(loaded["tracking_protocol"], "legacy-cmux")

        fleet_manifest.migrate(self.path, in_place=True)
        migrated = fleet_manifest.load(self.path)
        self.assertEqual(migrated["manifest_contract_version"], "2")
        self.assertEqual(migrated["execution_profile"], "native")
        self.assertEqual(migrated["tracking_protocol"], "legacy-cmux")

    def test_new_contract_validates_profiles_and_control_tracking(self) -> None:
        self.path.write_text(
            "schema_version=3\nmanifest_contract_version=2\n"
            "execution_profile=sandboxed\ntracking_protocol=control-v1\n",
            encoding="utf-8",
        )
        loaded = fleet_manifest.load(self.path)
        self.assertEqual(loaded["execution_profile"], "sandboxed")
        self.assertEqual(loaded["tracking_protocol"], "control-v1")

    def test_duplicates_unknown_profiles_and_false_legacy_claim_fail_closed(self) -> None:
        cases = (
            "feature=demo\nfeature=other\n",
            "manifest_contract_version=2\nexecution_profile=unsafe\n",
            "manifest_contract_version=1\ntracking_protocol=control-v1\n",
        )
        for content in cases:
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(fleet_manifest.ManifestError):
                    fleet_manifest.load(self.path)


if __name__ == "__main__":
    unittest.main()
