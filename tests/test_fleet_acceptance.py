from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleet_acceptance as acceptance
import fleet_archive


def contract():
    return {"schema_version": 1, "requirements": [{"id": "R1", "description": "Exact data value",
        "checks": [{"kind": "json_equals", "path": "result.json", "keys": ["count"], "expected": 3}]}]}


def tree(content=b'{"count":3}', *, link=False, duplicate=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for _ in range(2 if duplicate else 1):
            item = tarfile.TarInfo("result.json")
            if link:
                item.type = tarfile.SYMTYPE
                item.linkname = "/tmp/foreign"
                archive.addfile(item)
            else:
                item.size = len(content)
                archive.addfile(item, io.BytesIO(content))
    return output.getvalue()


from tests.mission_control_test_support import LegacyRuntimeGuard  # noqa: E402


_LEGACY_GUARD = LegacyRuntimeGuard()


def setUpModule() -> None:
    _LEGACY_GUARD.start()


def tearDownModule() -> None:
    _LEGACY_GUARD.stop()


class AcceptanceTests(unittest.TestCase):
    def test_binding_prevents_downgrade_and_changed_contract(self):
        spec = contract()
        key = acceptance.bound_key("mission:" + "x" * 180, spec)
        self.assertLessEqual(len(key) + len(":compiled"), 192)
        acceptance.check_binding(key, spec)
        changed = copy.deepcopy(spec)
        changed["requirements"][0]["checks"][0]["expected"] = 99
        for candidate in (None, changed):
            with self.assertRaises(acceptance.AcceptanceError):
                acceptance.check_binding(key, candidate)
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.check_binding("old-mission", spec)
        acceptance.check_binding("old-mission", None)

    def evaluate(self, data, specification=None):
        return acceptance.evaluate(specification or contract(), data, mission_id="test", final_sha="a" * 40)

    def test_matching_and_false_completion(self):
        self.assertEqual(self.evaluate(tree())["status"], "accepted")
        result = self.evaluate(tree(b'{"count":2,"status":"DONE"}'))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["requirements"][0]["checks"][0]["reason"], "predicate_mismatch")

    def test_missing_artifact_and_incomplete_requirement_reject(self):
        spec = contract()
        spec["requirements"].append({"id": "R2", "description": "Second artifact", "checks": [
            {"kind": "text_contains", "path": "missing.txt", "expected": "ready"}]})
        self.assertEqual(self.evaluate(tree(), spec)["status"], "rejected")

    def test_empty_duplicate_or_unsafe_contract_rejected(self):
        cases = [{"schema_version": 1, "requirements": []}]
        for transform in (
            lambda c: c["requirements"].append(copy.deepcopy(c["requirements"][0])),
            lambda c: c["requirements"][0].update(checks=[]),
            lambda c: c["requirements"][0]["checks"][0].update(path="../result.json"),
            lambda c: c.update(schema_version=True),
            lambda c: c["requirements"][0]["checks"][0].update(kind=[]),
        ):
            spec = contract()
            transform(spec)
            cases.append(spec)
        for spec in cases:
            with self.subTest(spec=spec), self.assertRaises(acceptance.AcceptanceError):
                acceptance.validate(spec)

    def test_symlink_duplicate_and_invalid_json_cannot_pass(self):
        for data in (tree(link=True), tree(duplicate=True)):
            with self.assertRaises(acceptance.AcceptanceError):
                self.evaluate(data)
        for raw in (b'{"count":3,"count":2}', b'{"count":true}', b'not json'):
            self.assertEqual(self.evaluate(tree(raw))["status"], "rejected")

    def test_receipt_binds_contract_tree_and_commit(self):
        result = self.evaluate(tree())
        self.assertEqual(result["contract_sha256"], acceptance.digest(contract()))
        self.assertEqual(result["final_sha"], "a" * 40)
        self.assertEqual(result, self.evaluate(tree()))
        self.assertNotEqual(result["tree_sha256"], self.evaluate(tree(b'{"count":4}'))["tree_sha256"])

    def test_cli_exit_code_rejects_false_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "contract.json").write_text(json.dumps(contract()))
            (root / "tree.tar").write_bytes(tree(b'{"count":0}'))
            result = subprocess.run([sys.executable, str(ROOT / "scripts/fleet_acceptance.py"),
                "--contract", str(root / "contract.json"), "--tree", str(root / "tree.tar")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "rejected")

    def test_real_archive_acceptance_uses_frozen_tree_and_contract(self):
        from tests.test_fleet_archive import FleetArchiveTests
        fixture = FleetArchiveTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        spec = {"schema_version": 1, "requirements": [{"id": "R1", "description": "Final README",
            "checks": [{"kind": "text_contains", "path": "README.md", "expected": "final"}]}]}
        original = fixture.create_mission
        # Inject the operator contract at creation, before archive collection.
        import fleet_mission
        create = fleet_mission.create_mission
        def with_contract(*args, **kwargs):
            kwargs["runtime_options"]["acceptance_contract"] = spec
            return create(*args, **kwargs)
        with mock.patch.object(fleet_mission, "create_mission", side_effect=with_contract):
            mission_id, manifest = original("acceptance")
        final_sha = fixture.add_writer_commit(manifest)
        archive = fixture.runs / "missions" / mission_id / "archive"
        fleet_archive.ArchiveBuilder(fixture.runs, mission_id).create(manifest)
        (fixture.repo / "README.md").write_text("changed after archive")
        result = fleet_archive.verify_acceptance(archive, spec)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["final_sha"], final_sha)
        other = copy.deepcopy(spec)
        other["requirements"][0]["checks"][0]["expected"] = "changed"
        with self.assertRaises(fleet_archive.ArchiveError):
            fleet_archive.verify_acceptance(archive, other)

    def test_mission_closure_requires_contract_even_when_lead_says_done(self):
        from tests.test_mission_run import MissionRunTests, mission_run
        for expected, terminal in (("baseline", "succeeded"), ("missing feature", "failed")):
            with self.subTest(terminal=terminal):
                fixture = MissionRunTests()
                fixture.setUp()
                try:
                    calls, runtime = fixture.fake_runtime()
                    spec = {"schema_version": 1, "requirements": [{"id": "R1", "description": "README requirement",
                        "checks": [{"kind": "text_contains", "path": "README.md", "expected": expected}]}]}
                    with mock.patch.object(mission_run.fleet_legacy_mission, "run_process", side_effect=runtime), mock.patch.object(mission_run.fleet_legacy_mission, "cmux_signal"):
                        result = mission_run.create_and_drive(fixture.runs, feature="verified",
                            objective="inspect the parser", workflow_name="implementation",
                            target_repo=fixture.target.resolve(), risk_override="auto",
                            timeout_seconds=300, allow_dirty_baseline=False, teardown=False,
                            acceptance_contract=spec)
                        self.assertEqual(result["status"], terminal)
                        self.assertEqual(result["acceptance"]["status"], "accepted" if terminal == "succeeded" else "rejected")
                        again = mission_run.drive_mission(fixture.runs, result["mission_id"])
                        self.assertEqual(again["acceptance"], result["acceptance"])
                        self.assertEqual(calls.count("fleet-send.sh"), 1)
                    root = fixture.runs / "missions" / result["mission_id"]
                    receipt_path = root / "acceptance-result.json"
                    original_receipt = receipt_path.read_bytes()
                    receipt = json.loads(original_receipt)
                    receipt["final_sha"] = "b" * 40
                    receipt_path.write_bytes(acceptance.fleet_json.canonical_bytes(receipt) + b"\n")
                    with self.assertRaisesRegex(mission_run.MissionRunError, "receipt binding mismatch"):
                        mission_run.drive_mission(fixture.runs, result["mission_id"])
                    receipt_path.write_bytes(original_receipt)
                    options = json.loads((root / "runtime-options.json").read_text())
                    options.pop("acceptance_contract")
                    (root / "runtime-options.json").write_bytes(acceptance.fleet_json.canonical_bytes(options) + b"\n")
                    with self.assertRaises(acceptance.AcceptanceError):
                        mission_run.drive_mission(fixture.runs, result["mission_id"])
                finally:
                    fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
