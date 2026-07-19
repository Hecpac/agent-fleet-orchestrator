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
        self.assertEqual(migrated["manifest_contract_version"], "3")
        self.assertEqual(migrated["execution_profile"], "native")
        self.assertEqual(migrated["tracking_protocol"], "legacy-cmux")

    def test_new_contract_validates_profiles_and_control_tracking(self) -> None:
        # Contract v2 stays readable for standalone fleets during the cutover.
        self.path.write_text(
            "schema_version=3\nmanifest_contract_version=2\n"
            "execution_profile=sandboxed\ntracking_protocol=control-v1\n",
            encoding="utf-8",
        )
        loaded = fleet_manifest.load(self.path)
        self.assertEqual(loaded["execution_profile"], "sandboxed")
        self.assertEqual(loaded["tracking_protocol"], "control-v1")

        self.path.write_text(
            "schema_version=3\nmanifest_contract_version=3\n"
            "execution_profile=native\ntracking_protocol=control-v1\n",
            encoding="utf-8",
        )
        self.assertEqual(fleet_manifest.load(self.path)["manifest_contract_version"], "3")

    def test_mission_bound_v2_requires_restart_and_v3_exact_binding(self) -> None:
        self.path.write_text(
            "manifest_contract_version=2\nmission_id=mission-old\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(fleet_manifest.ManifestError, "restart.*v3 cutover"):
            fleet_manifest.load(self.path)
        with self.assertRaisesRegex(fleet_manifest.ManifestError, "restart.*v3 cutover"):
            fleet_manifest.migrate(self.path)

        binding = "".join(f"{field}={'a' * 64}\n" for field in fleet_manifest.BINDING_FIELDS)
        self.path.write_text(
            "manifest_contract_version=3\nmission_id=mission-current\n"
            "tracking_protocol=control-v1\n" + binding,
            encoding="utf-8",
        )
        self.assertEqual(fleet_manifest.load(self.path)["launch_digest"], "a" * 64)

        self.path.write_text(
            "manifest_contract_version=3\nmission_id=mission-missing-launch\n"
            "tracking_protocol=control-v1\n"
            f"compiled_digest={'a' * 64}\nrouter_digest={'a' * 64}\n"
            f"roster_digest={'a' * 64}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(fleet_manifest.ManifestError, "requires valid launch_digest"):
            fleet_manifest.load(self.path)

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
