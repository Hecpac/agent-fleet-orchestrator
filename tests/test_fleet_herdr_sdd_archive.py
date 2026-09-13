"""Provider-free SDD stage-2 archive closure tests.

Reuses the real-archive fixture from ``test_fleet_herdr_archive`` and exercises
archive schema v5: frozen plan + binding bound to the ledger pin and to the
durable pre-anchor snapshot selection.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fleet_herdr_archive as archive
import fleet_herdr_sdd
import fleet_json
import fleet_mission
import fleet_mission_state as state
from tests.test_fleet_herdr_archive import HerdrSnapshotTests


ROOT = Path(__file__).resolve().parents[1]


class SddArchiveTests(HerdrSnapshotTests):
    def setUp(self):
        super().setUp()
        self.plan_dir = tempfile.TemporaryDirectory(prefix="sdd-plan-")
        self.addCleanup(self.plan_dir.cleanup)
        self.plan = Path(self.plan_dir.name) / "plan.json"
        self.plan.write_bytes((ROOT / "examples/sdd/deny-before-effect.json").read_bytes())

    def archive_dir(self, mission_id):
        return self.runs / "missions" / mission_id / "herdr-archive"

    def test_sdd_archive_v5_roundtrip_and_offline_verify_without_live_store(self):
        mission_id, roles, backend = self.archive_fixture(sdd_plan_path=self.plan)
        result = archive.create(self.runs, mission_id, self.repo, roles, backend)
        self.assertEqual(result["archive_schema_version"], 5)
        self.assertTrue(result["staged_valid"])
        self.assertEqual(result["acceptance"]["status"], "accepted")
        self.anchor(mission_id, result)
        verified = archive.verify(self.runs, mission_id)
        self.assertTrue(verified["valid"])
        self.assertTrue(verified["anchored"])
        self.assertEqual(verified["index_sha256"], result["index_sha256"])
        # Offline: remove candidate content and the live SDD store entirely.
        (self.repo / "result.txt").unlink()
        shutil.rmtree(self.runs / "sdd")
        recovered = archive.verify(self.runs, mission_id)
        self.assertTrue(recovered["valid"])
        self.assertEqual(recovered["index_sha256"], result["index_sha256"])

    def test_sdd_archive_staging_rejects_missing_live_snapshot(self):
        mission_id, roles, backend = self.archive_fixture(sdd_plan_path=self.plan)
        (self.runs / "sdd/missions" / f"{mission_id}.json").unlink()
        with self.assertRaises(archive.HerdrArchiveError):
            archive.create(self.runs, mission_id, self.repo, roles, backend)

    def test_sdd_archive_rejects_tampered_plan_and_binding(self):
        mission_id, roles, backend = self.archive_fixture(sdd_plan_path=self.plan)
        archive.create(self.runs, mission_id, self.repo, roles, backend)
        plan_path = self.archive_dir(mission_id) / "sdd/plan.json"
        original = plan_path.read_bytes()
        plan_path.write_bytes(original + b" ")
        with self.assertRaises(archive.HerdrArchiveError):
            archive.verify(self.runs, mission_id, require_anchor=False)
        plan_path.write_bytes(original)
        binding_path = self.archive_dir(mission_id) / "sdd/binding.json"
        binding = fleet_json.loads(binding_path.read_bytes())
        binding["plan_sha256"] = "0" * 64
        binding_path.write_bytes(archive._bytes(binding))
        with self.assertRaises(archive.HerdrArchiveError):
            archive.verify(self.runs, mission_id, require_anchor=False)

    def test_sdd_archive_rejects_downgrade_and_index_rewrite(self):
        mission_id, roles, backend = self.archive_fixture(sdd_plan_path=self.plan)
        result = archive.create(self.runs, mission_id, self.repo, roles, backend)
        index_path = self.archive_dir(mission_id) / "archive-index.json"
        index = fleet_json.loads(index_path.read_bytes())
        rewritten = {**index, "schema_version": 3}
        index_path.write_bytes(archive._bytes(rewritten))
        with self.assertRaises(archive.HerdrArchiveError):
            archive.verify(self.runs, mission_id, require_anchor=False)
        index_path.write_bytes(archive._bytes({**index, "unexpected": "field"}))
        with self.assertRaisesRegex(archive.HerdrArchiveError, "durable snapshot selection"):
            archive.verify(self.runs, mission_id, require_anchor=False)
        self.assertEqual(result["archive_schema_version"], 5)

    def test_preanchor_resealed_v5_without_sdd_is_rejected(self):
        mission_id, roles, backend = self.archive_fixture()
        archive.create(self.runs, mission_id, self.repo, roles, backend)
        archive_dir = self.archive_dir(mission_id)
        index_path = archive_dir / "archive-index.json"
        index = fleet_json.loads(index_path.read_bytes())
        index_path.write_bytes(archive._bytes({**index, "schema_version": 5}))
        archived_events = [
            fleet_json.loads(line)
            for line in (archive_dir / "ledger.jsonl").read_bytes().splitlines()
        ]
        with mock.patch.object(archive.state, "read_events", return_value=archived_events):
            with self.assertRaisesRegex(archive.HerdrArchiveError, "ledger SDD plan pin"):
                archive.verify(self.runs, mission_id, require_anchor=False)


    def test_preanchor_missing_durable_selection_cannot_authorize(self):
        mission_id, roles, backend = self.archive_fixture(sdd_plan_path=self.plan)
        result = archive.create(self.runs, mission_id, self.repo, roles, backend)
        self.anchor(mission_id, result)
        archived_ledger = (self.archive_dir(mission_id) / "ledger.jsonl").read_bytes()
        (self.runs / "missions" / mission_id / "mission.jsonl").write_bytes(archived_ledger)
        with self.assertRaisesRegex(archive.HerdrArchiveError, "durable snapshot selection"):
            archive.verify(self.runs, mission_id, require_anchor=False, for_completion=True)

    def test_selected_producer_creation_pin_mismatch_rejected(self):
        mission_id, roles, backend = self.archive_fixture(sdd_plan_path=self.plan)
        original = archive._capture_contents

        def tamper(runs_dir, mid, role_results, backend_state, compiled, current, frozen, ledger):
            contents, index = original(runs_dir, mid, role_results, backend_state,
                                       compiled, current, frozen, ledger)
            creation = fleet_json.loads(contents["creation-request.json"])
            creation["request"]["sdd_plan_sha256"] = "f" * 64
            raw = archive._bytes(creation)
            contents["creation-request.json"] = raw
            index["entries"]["creation-request.json"] = {
                "sha256": archive._sha(raw), "bytes": len(raw)}
            return contents, index

        with mock.patch.object(archive, "_capture_contents", side_effect=tamper):
            with self.assertRaises(archive.HerdrArchiveError):
                archive.create(self.runs, mission_id, self.repo, roles, backend)


    def test_legacy_nonsdd_resealed_archive_ignores_selection_pin(self):
        mission_id, roles, backend = self.archive_fixture()
        proof = archive.create(self.runs, mission_id, self.repo, roles, backend)
        path = Path(proof["path"])
        index = fleet_json.loads(path.read_bytes())
        index["schema_version"] = 2
        index.pop("permissions_policy_version", None)
        raw = archive._bytes(index)
        path.write_bytes(raw)
        state.append_event(self.runs, mission_id, kind="archive_created", actor="CONTROL",
            idempotency_key="legacy-anchor",
            payload={"path": str(path), "sha256": state.artifact_id(raw), "mode": "herdr"})
        self.assertTrue(archive.verify(self.runs, mission_id)["valid"])


_SDD_TESTS = (
    "test_sdd_archive_v5_roundtrip_and_offline_verify_without_live_store",
    "test_sdd_archive_staging_rejects_missing_live_snapshot",
    "test_sdd_archive_rejects_tampered_plan_and_binding",
    "test_sdd_archive_rejects_downgrade_and_index_rewrite",
    "test_preanchor_missing_durable_selection_cannot_authorize",
    "test_selected_producer_creation_pin_mismatch_rejected",
    "test_legacy_nonsdd_resealed_archive_ignores_selection_pin",
)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(SddArchiveTests(name) for name in _SDD_TESTS)


if __name__ == "__main__":
    unittest.main()
