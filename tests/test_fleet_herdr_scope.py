"""Physical-scope acceptance: provider-free inventory and real archive fixtures."""
from __future__ import annotations

import copy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from tests.test_fleet_herdr_orchestration_closure import MinimalFixture, MinimalBackend, git
import fleet_artifacts
import fleet_herdr_archive as archive
import fleet_herdr_mission as mission
import fleet_herdr_scope as scope
import fleet_json
import fleet_mission
import fleet_mission_state as state


def contract():
    return {"schema_version": 1, "editable_paths": ["answer.txt"],
            "temporary_directories": [".fleet-scratch"], "max_entries": 1000,
            "max_bytes": 1024 * 1024}


class InventoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="fleet-scope-inventory-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "README.md").write_text("preexisting\n")
        (self.root / ".gitignore").write_text("*.tsbuildinfo\n")
        self.contract = contract()
        self.baseline = {"schema_version": 1, "mission_id": "fixture",
            "compiled_digest": "a" * 64, "base_sha": "b" * 40,
            "contract_sha256": scope.digest(self.contract),
            "tracked_paths": [".gitignore", "README.md"],
            "inventory": scope.capture(self.root, self.contract)}
        self.assertTrue(self.baseline["inventory"]["complete"])

    def receipt(self):
        return scope.evaluate(self.baseline, scope.capture(self.root, self.contract), self.contract)

    def test_ignored_residue_is_rejected_without_reading_git_ignore_rules(self):
        (self.root / "tsconfig.tsbuildinfo").write_bytes(b"cache")
        result = self.receipt()
        self.assertEqual(result["status"], "rejected")
        self.assertIn({"path": "tsconfig.tsbuildinfo", "reason": "undeclared_residue"}, result["issues"])

    def test_declared_temporary_is_permitted_and_not_delivered(self):
        (self.root / ".fleet-scratch").mkdir()
        (self.root / ".fleet-scratch" / "cache.bin").write_bytes(b"disposable")
        (self.root / "answer.txt").write_text("answer\n")
        result = self.receipt()
        self.assertEqual(result["status"], "accepted")
        self.assertNotIn(".fleet-scratch/cache.bin", result["delivery_paths"])
        self.assertIn(".fleet-scratch/cache.bin", result["temporary_paths"])

    def test_preexisting_outside_scope_change_and_deletion_reject(self):
        (self.root / "README.md").write_text("altered")
        self.assertEqual(self.receipt()["issues"], [{"path": "README.md", "reason": "preexisting_outside_scope_changed"}])
        (self.root / "README.md").unlink()
        self.assertEqual(self.receipt()["status"], "rejected")

    def test_preexisting_ignored_file_is_preserved_but_not_exported(self):
        (self.root / "old.tsbuildinfo").write_bytes(b"old")
        self.baseline["inventory"] = scope.capture(self.root, self.contract)
        self.assertEqual(self.receipt()["status"], "accepted")
        self.assertNotIn("old.tsbuildinfo", self.receipt()["delivery_paths"])
        (self.root / "old.tsbuildinfo").write_bytes(b"modified")
        self.assertEqual(self.receipt()["status"], "rejected")

    def test_preexisting_temporary_cannot_be_claimed(self):
        (self.root / ".fleet-scratch").mkdir()
        self.baseline["inventory"] = scope.capture(self.root, self.contract)
        self.assertIn({"path": ".fleet-scratch", "reason": "temporary_path_preexisted"}, self.receipt()["issues"])

    def test_temporary_root_must_be_a_directory(self):
        (self.root / ".fleet-scratch").write_text("not a directory")
        self.assertEqual(self.receipt()["issues"], [{"path": ".fleet-scratch", "reason": "temporary_root_not_directory"}])

    def test_entry_and_byte_limits_never_report_partial_pass(self):
        for field in ("max_entries", "max_bytes"):
            limited = {**self.contract, field: 1}
            observed = scope.capture(self.root, limited)
            self.assertFalse(observed["complete"])
            # The actual Mission keeps its original complete baseline/contract.
            result = scope.evaluate(self.baseline, observed, self.contract)
            self.assertEqual(result["status"], "incomplete")
            self.assertTrue(result["issues"])

    def test_read_error_or_race_cannot_emit_complete_receipt(self):
        with mock.patch.object(scope, "_scan", side_effect=PermissionError("unreadable")):
            self.assertEqual(self.receipt()["status"], "incomplete")
        original = scope._scan
        calls = []
        def mutate(root, spec):
            result = original(root, spec)
            if not calls:
                (root / "README.md").write_text("changed while capturing")
            calls.append(True)
            return result
        with mock.patch.object(scope, "_scan", side_effect=mutate):
            self.assertEqual(self.receipt()["status"], "incomplete")

    def test_symlink_and_hardlink_are_incomplete_without_following(self):
        import os
        link = self.root / "link"
        link.symlink_to(self.root / "README.md")
        self.assertEqual(self.receipt()["status"], "incomplete")
        link.unlink()
        os.link(self.root / "README.md", link)
        self.assertEqual(self.receipt()["status"], "incomplete")

    def test_invalid_or_overlapping_paths_are_not_authorized(self):
        for bad in (".", "../outside", "/tmp/file", ".git/config", "a//b", ":(glob)**/../x"):
            with self.subTest(path=bad), self.assertRaises(scope.ScopeError):
                scope.validate({**self.contract, "editable_paths": [bad]})
        with self.assertRaises(scope.ScopeError):
            scope.validate({**self.contract, "editable_paths": [".fleet-scratch/file"]})


class ScopeMissionTests(MinimalFixture):
    def setUp(self):
        super().setUp()
        (self.target / ".gitignore").write_text("*.tsbuildinfo\n")
        git(self.target, "add", ".gitignore")
        git(self.target, "commit", "-qm", "fixture ignore rule")
        self.head = git(self.target, "rev-parse", "HEAD")
        self.scope_contract = contract()

    def options(self, contract=None):
        return {**super().options(contract), "scope_contract": self.scope_contract}

    def mutate_during_build(self, mutation):
        original = MinimalBackend.submit
        def submit(backend, *args, **kwargs):
            result = original(backend, *args, **kwargs)
            mutation(backend.repo)
            return result
        return mock.patch.object(MinimalBackend, "submit", submit)

    def test_ignored_file_blocks_acceptance_with_durable_receipt(self):
        mid = self.create()
        with self.mutate_during_build(lambda repo: (repo / "tsconfig.tsbuildinfo").write_bytes(b"cache")):
            result = mission.supervise(self.runs, mid, seconds=2, poll_seconds=.05)
        self.assertEqual(result["supervision"], "blocked")
        self.assertEqual(result["iterations"], 1)
        receipt = result["scope_rejection"]
        self.assertEqual(receipt["status"], "rejected")
        stored = fleet_json.loads(fleet_artifacts.get_bytes(self.runs, mid, receipt["receipt_artifact_id"]))
        self.assertEqual(stored, {k: v for k, v in receipt.items() if k != "receipt_artifact_id"})
        self.assertFalse((self.runs / "missions" / mid / "candidate-freeze.json").exists())
        self.assertIsNone(fleet_mission.load_state(self.runs, mid)["terminal"])
        self.assertEqual(len(MinimalBackend.tasks), 1)

    def test_temporaries_pass_archive_v8_and_original_stays_unchanged(self):
        original_index = (self.target / ".git" / "index").read_bytes()
        mid = self.create()
        def scratch(repo):
            (repo / ".fleet-scratch").mkdir()
            (repo / ".fleet-scratch" / "cache.bin").write_bytes(b"scratch not ignored by Git")
        with self.mutate_during_build(scratch):
            result = mission.drive(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["archive"]["archive_schema_version"], 8)
        self.assertEqual(result["archive"]["physical_scope"]["status"], "accepted")
        current = fleet_mission.load_state(self.runs, mid)
        events = state.read_events(state.ledger_path(self.runs, mid))
        captured = next(e for e in events if e["kind"] == scope.BASELINE_EVENT)
        boot = next(e for e in events if e["kind"] == "fleet_boot_started")
        self.assertLess(captured["sequence"], boot["sequence"])
        candidate = self.runs / "missions" / mid / "candidate"
        self.assertTrue((candidate / ".fleet-scratch" / "cache.bin").exists())
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.head)
        self.assertEqual((self.target / ".git" / "index").read_bytes(), original_index)
        self.assertEqual(git(self.target, "status", "--porcelain=v1", "--ignored"), "")
        import tarfile
        root = self.runs / "missions" / mid / "herdr-archive"
        with tarfile.open(root / "writer/final-tree.tar") as tar:
            self.assertNotIn(".fleet-scratch/cache.bin", tar.getnames())
            self.assertEqual(tar.extractfile("answer.txt").read(), b"implemented\n")
        # Offline verification and terminal replay do not need the mutable tree.
        shutil.rmtree(candidate)
        self.assertTrue(archive.verify(self.runs, mid)["valid"])
        calls = list(MinimalBackend.calls)
        mission.drive(self.runs, mid)
        self.assertEqual(MinimalBackend.calls, calls)
        self.assertEqual(len(current["admissions"]), 1)

    def test_baseline_file_change_is_rejected(self):
        mid = self.create()
        with self.mutate_during_build(lambda repo: (repo / "README.md").write_text("altered")):
            result = mission.drive(self.runs, mid)
        self.assertEqual(result["scope_rejection"]["status"], "rejected")
        self.assertIn({"path": "README.md", "reason": "preexisting_outside_scope_changed"}, result["scope_rejection"]["issues"])

    def test_explicitly_editable_ignored_file_is_delivered(self):
        self.scope_contract["editable_paths"].append("wanted.tsbuildinfo")
        mid = self.create()
        with self.mutate_during_build(lambda repo: (repo / "wanted.tsbuildinfo").write_text("requested")):
            result = mission.drive(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertIn("wanted.tsbuildinfo", result["archive"]["physical_scope"]["delivery_paths"])

    def test_authorized_deletion_and_executable_file_match_the_delivered_tree(self):
        self.scope_contract["editable_paths"] += ["README.md", "check.sh"]
        mid = self.create()
        def change(repo):
            (repo / "README.md").unlink()
            (repo / "check.sh").write_text("#!/bin/sh\nexit 0\n")
            (repo / "check.sh").chmod(0o755)
        with self.mutate_during_build(change):
            result = mission.drive(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn("README.md", result["archive"]["physical_scope"]["delivery_paths"])
        self.assertEqual(result["archive"]["physical_scope"]["observed_inventory"]["entries"]["check.sh"]["mode"], 0o755)

    def test_scope_keeps_the_existing_functional_gate(self):
        from tests.test_fleet_herdr_orchestration_closure import synthetic_spec, research_fixtures
        original_options = self.options
        with mock.patch.object(self, "options", side_effect=lambda selected=None: {
                **original_options(selected), "functional_contract": synthetic_spec()}):
            mid = self.create()
        candidate = self.runs / "missions" / mid / "candidate"
        with mock.patch.object(mission.fleet_functional.runner, "execute",
                side_effect=lambda spec, _tree, _tests, attempt, **_kw:
                    research_fixtures.functional_outcome(spec, attempt, candidate)):
            result = mission.drive(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["archive"]["functional"]["status"], "passed")
        self.assertEqual(result["archive"]["physical_scope"]["status"], "accepted")

    def test_permitted_temporary_can_change_after_freeze_but_residue_cannot(self):
        original = archive.freeze
        calls = []
        def modify_after_freeze(runs, mid, candidate):
            frozen = original(runs, mid, candidate)
            if not calls:
                (candidate / ".fleet-scratch").mkdir()
                (candidate / ".fleet-scratch" / "late").write_text("check output")
            calls.append(True)
            return frozen
        mid = self.create(key="late-temporary")
        with mock.patch.object(archive, "freeze", side_effect=modify_after_freeze):
            result = mission.drive(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertIn(".fleet-scratch/late", result["archive"]["physical_scope"]["temporary_paths"])

        def residue_after_freeze(runs, mid, candidate):
            frozen = original(runs, mid, candidate)
            (candidate / "late.tsbuildinfo").write_text("unapproved")
            return frozen
        mid = self.create(key="late-residue")
        with mock.patch.object(archive, "freeze", side_effect=residue_after_freeze):
            result = mission.drive(self.runs, mid)
        self.assertEqual(result["scope_rejection"]["status"], "rejected")
        self.assertIsNone(fleet_mission.load_state(self.runs, mid)["terminal"])

    def test_cli_dry_and_creation_pass_the_opt_in_without_live_execution(self):
        import subprocess
        import sys
        from tests.test_mission_run import mission_run
        acceptance_path, scope_path = self.root / "acceptance.json", self.root / "scope.json"
        acceptance_path.write_bytes(fleet_json.canonical_bytes(self.contract))
        scope_path.write_bytes(fleet_json.canonical_bytes(self.scope_contract))
        script = Path(__file__).resolve().parents[1] / "scripts/mission-run.py"
        run = subprocess.run([sys.executable, "-B", str(script),
            "--runs-dir", str(self.runs), "dry", "scope-dry", "Implement the answer",
            "--workflow", "herdr-minimal-implementation", "--target-repo", str(self.target),
            "--acceptance-contract", str(acceptance_path), "--scope-contract", str(scope_path), "--json"],
            text=True, capture_output=True, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        dry = fleet_json.loads(run.stdout)
        self.assertEqual(dry["physical_scope"]["contract_sha256"], scope.digest(self.scope_contract))
        self.assertEqual(dry["effects"], [])
        self.assertFalse(self.runs.exists())
        with mock.patch.object(mission_run, "drive_mission", side_effect=lambda runs, mid: {"mission_id": mid}):
            created = mission_run.create_and_drive(self.runs, feature="scope-cli", objective="Implement the answer",
                workflow_name="herdr-minimal-implementation", target_repo=self.target,
                risk_override="auto", timeout_seconds=3600, allow_dirty_baseline=False, teardown=False,
                herdr_session="fixture", acceptance_contract=self.contract, scope_contract=self.scope_contract)
        current = fleet_mission.load_state(self.runs, created["mission_id"])
        self.assertEqual(current[scope.FIELD], scope.digest(self.scope_contract))
        self.assertEqual(current["status"], "compiled")
        self.assertFalse(MinimalBackend.calls)

    def test_incomplete_baseline_stops_before_agent_boot(self):
        self.scope_contract["max_entries"] = 1
        mid = self.create()
        result = mission.drive(self.runs, mid)
        self.assertEqual(result["scope_rejection"]["status"], "incomplete")
        self.assertEqual(MinimalBackend.calls, [])
        current = fleet_mission.load_state(self.runs, mid)
        self.assertNotIn("herdr_scope_baseline", current)
        self.assertFalse(current["admissions"])

    def test_incomplete_candidate_stops_before_acceptance(self):
        self.scope_contract["max_bytes"] = 4096
        mid = self.create()
        def oversized(repo):
            (repo / ".fleet-scratch").mkdir()
            (repo / ".fleet-scratch" / "large").write_bytes(b"x" * 4097)
        with self.mutate_during_build(oversized):
            result = mission.drive(self.runs, mid)
        self.assertEqual(result["scope_rejection"]["status"], "incomplete")
        self.assertFalse((self.runs / "missions" / mid / "herdr-archive").exists())

    def test_creation_contract_cannot_be_removed_or_changed_on_resume(self):
        mid = self.create()
        root = self.runs / "missions" / mid
        options = fleet_json.loads((root / "runtime-options.json").read_bytes())
        options.pop("scope_contract")
        creation = fleet_json.loads((root / "creation-request.json").read_bytes())
        creation["runtime_options"] = options
        for name, value in (("runtime-options.json", options), ("creation-request.json", creation)):
            (root / name).write_bytes(fleet_json.canonical_bytes(value) + b"\n")
        result = mission.drive(self.runs, mid)
        self.assertIn("scope binding failed", result["next_action"])
        self.assertFalse(MinimalBackend.calls)

    def test_baseline_receipt_and_tree_tampering_cannot_be_resealed(self):
        mid = self.create()
        mission.drive(self.runs, mid)
        current = fleet_mission.load_state(self.runs, mid)
        root = self.runs / "missions" / mid / "herdr-archive"
        baseline = fleet_json.loads((root / "scope/baseline.json").read_bytes())
        receipt = fleet_json.loads((root / "scope/result.json").read_bytes())
        frozen = fleet_json.loads((root / "candidate-freeze.json").read_bytes())
        tree = (root / "writer/final-tree.tar").read_bytes()
        altered = copy.deepcopy(baseline)
        altered["inventory"]["entries"]["README.md"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(scope.ScopeError, "baseline binding"):
            scope.verify_archived(current, self.options(), frozen, altered, receipt, tree)
        altered = copy.deepcopy(receipt)
        altered["observed_inventory"]["entries"]["answer.txt"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(scope.ScopeError, "receipt"):
            scope.verify_archived(current, self.options(), frozen, baseline, altered, tree)
        index = fleet_json.loads((root / "archive-index.json").read_bytes())
        index["schema_version"] = 7
        (root / "archive-index.json").write_bytes(archive._bytes(index))
        with self.assertRaisesRegex(archive.HerdrArchiveError, "scope archive version"):
            archive.verify(self.runs, mid)


if __name__ == "__main__":
    unittest.main()
