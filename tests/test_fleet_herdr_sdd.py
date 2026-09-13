"""Provider-free integration tests for the opt-in SDD stage-2 slice.

Uses the real ``fleet_mission.create_mission`` ledger path, the real Herdr
driver entry points and the existing fake backend/archive from
``tests/test_fleet_herdr_mission.py``. No providers, Docker or live Herdr.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_acceptance
import fleet_herdr_control
import fleet_herdr_mission as driver
import fleet_herdr_sdd
import fleet_mission
import fleet_mission_state as state
import workflow_config


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixture = _load("herdr_mission_fixture", ROOT / "tests/test_fleet_herdr_mission.py")
mission_run = _load("mission_run", ROOT / "scripts/mission-run.py")


class CorruptingBackend(fixture.FakeBackend):
    """Fake backend that corrupts the frozen blob after the Plan response."""

    def submit(self, run_id, prompt, *, instance_id):
        result = super().submit(run_id, prompt, instance_id=instance_id)
        task = json.loads(prompt)
        if task["stage"] == "plan":
            digest = task["sdd_plan"]["plan_sha256"]
            (self.runs / "sdd/blobs" / f"{digest}.json").write_bytes(b"corrupted mid-drive")
        return result


class VerifyCorruptingBackend(fixture.FakeBackend):
    """Fake backend that corrupts the frozen blob after Verify's valid result."""

    def submit(self, run_id, prompt, *, instance_id):
        result = super().submit(run_id, prompt, instance_id=instance_id)
        task = json.loads(prompt)
        if task["stage"] == "verify":
            digest = task["sdd_plan"]["plan_sha256"]
            (self.runs / "sdd/blobs" / f"{digest}.json").write_bytes(b"corrupted after verify")
        return result


class SddHerdrIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="herdr-sdd-test-")
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name).resolve()
        self.target = self.tmp / "target"
        self.target.mkdir()
        fixture.git(self.target, "init")
        (self.target / "README.md").write_text("baseline\n")
        fixture.git(self.target, "add", "README.md")
        fixture.git(self.target, "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid",
                    "commit", "--no-gpg-sign", "-m", "fixture")
        self.head = fixture.git(self.target, "rev-parse", "HEAD")
        self.runs = self.tmp / "runs"
        self.compiled = workflow_config.compile_path(ROOT / "workflows/herdr-implementation.yaml")
        self.plan = self.tmp / "plan.json"
        self.plan.write_bytes((ROOT / "examples/sdd/deny-before-effect.json").read_bytes())
        self._reset_backend()
        self.archive = fixture.FakeArchive()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(driver.fleet_herdr, "HerdrBackend", fixture.FakeBackend).start()
        mock.patch.object(driver, "_archive", return_value=self.archive).start()

    def _reset_backend(self) -> None:
        backend = fixture.FakeBackend
        backend.calls, backend.tasks, backend.observations, backend.results = [], {}, {}, {}
        backend.crash_stage = backend.missing_stage = backend.bad_result = None
        backend.bad_context = None
        backend.role_status, backend.mutate_review = "PASS", False
        backend.closed = backend.teardown_error = False

    def contract(self) -> dict:
        return {"schema_version": 1, "requirements": [{"id": "answer", "description": "answer exists",
            "checks": [{"kind": "text_contains", "path": "answer.txt", "expected": "implemented"}]}]}

    def create(self, sdd_plan_path, *, key: str = "sdd-fixture",
               objective: str = "Implement a local answer artifact",
               functional_spec: dict | None = None):
        contract = self.contract()
        opts = {"herdr_session": "sdd-fixture", "acceptance_contract": contract,
                "teardown": False, "timeout_seconds": 7200}
        if functional_spec is not None:
            opts["functional_contract"] = functional_spec
        return fleet_mission.create_mission(
            self.runs, compiled=self.compiled, feature="sdd-driver",
            objective=objective, target_repo=self.target, base_sha=self.head,
            idempotency_key=fleet_acceptance.bound_key(key, contract),
            runtime_options=opts, sdd_plan_path=sdd_plan_path)

    def plan_digest(self) -> str:
        return hashlib.sha256(self.plan.read_bytes()).hexdigest()

    def submitted(self) -> list:
        return [call[1] for call in fixture.FakeBackend.calls if call[0] == "submit"]

    def blob_path(self, digest: str) -> Path:
        return self.runs / "sdd/blobs" / f"{digest}.json"

    # -- creation, packet transfer and stage-2 boundary --------------------

    def test_freeze_pins_ledger_and_packet_for_every_role_then_completes(self) -> None:
        mid, created = self.create(self.plan)
        self.assertTrue(created)
        digest = self.plan_digest()
        current = fleet_mission.load_state(self.runs, mid)
        self.assertEqual(current["sdd_plan_sha256"], digest)
        result = driver.drive(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["acceptance"]["status"], "accepted")
        self.assertEqual(len(self.submitted()), 5)
        for task in fixture.FakeBackend.tasks.values():
            packet = task["sdd_plan"]
            self.assertEqual(packet["schema_version"], 1)
            self.assertEqual(packet["mission_id"], mid)
            self.assertEqual(packet["plan_sha256"], digest)
            self.assertEqual(packet["plan"]["schema"], "fleet.sdd.plan.v1")
        events = state.read_events(state.ledger_path(self.runs, mid))
        self.assertTrue(any(event["kind"] == "mission_terminal" for event in events))
        self.assertTrue((self.runs / "missions" / mid / "herdr-archive").exists())

    def test_recovery_in_fresh_process_after_source_removed(self) -> None:
        mid, _ = self.create(self.plan)
        digest = self.plan_digest()
        self.plan.unlink()
        code = (
            "import sys; sys.path.insert(0, %r); "
            "from pathlib import Path; import fleet_herdr_mission as m; "
            "d = m._Driver(Path(%r), %r); d.load(); "
            "assert d.sdd_error is None, d.sdd_error; "
            "print(d.sdd_packet['plan_sha256'])"
        ) % (str(ROOT / "scripts"), str(self.runs), mid)
        run = subprocess.run([sys.executable, "-B", "-c", code],
                             capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), digest)

    # -- fail-closed integrity --------------------------------------------

    def test_tampered_blob_blocks_before_backend_or_candidate(self) -> None:
        mid, _ = self.create(self.plan)
        digest = self.plan_digest()
        before = fleet_mission.load_state(self.runs, mid)
        self.blob_path(digest).write_bytes(b"tampered")
        with mock.patch.object(driver.fleet_herdr, "HerdrBackend",
                               side_effect=AssertionError("backend constructed")), \
                mock.patch.object(driver._Driver, "prepare_candidate",
                                  side_effect=AssertionError("candidate prepared")):
            result = driver.drive(self.runs, mid)
        self.assertIn(result["status"], {"compiled", "booting", "running"})
        self.assertIn("failed closed before new effects", result["next_action"])
        after = fleet_mission.load_state(self.runs, mid)
        self.assertEqual(after["admissions"], before["admissions"])
        self.assertEqual(fixture.FakeBackend.calls, [])

    def test_wrong_pin_and_foreign_mission_fail_closed(self) -> None:
        mid, _ = self.create(self.plan)
        current = fleet_mission.load_state(self.runs, mid)
        with self.assertRaises(state.MissionStateError):
            fleet_herdr_sdd.verify(self.runs, dict(current, sdd_plan_sha256="0" * 64))
        with self.assertRaises(state.MissionStateError):
            fleet_herdr_sdd.verify(self.runs, dict(current, mission_id=str(uuid.uuid4())))

    def test_tamper_does_not_block_active_run_cancellation(self) -> None:
        mid, _ = self.create(self.plan)
        digest = self.plan_digest()
        fixture.FakeBackend.crash_stage = "build"
        with self.assertRaises(RuntimeError):
            driver.drive(self.runs, mid)
        fixture.FakeBackend.crash_stage = None
        active = [a for a in fleet_mission.load_state(self.runs, mid)["admissions"].values() if a["active"]]
        self.assertTrue(active)
        submits_before = len(self.submitted())
        self.blob_path(digest).write_bytes(b"tampered after authorization")
        fleet_herdr_control.request(self.runs, mid, action="cancel", reason="cancel after tamper",
                                    idempotency_key="sdd-cancel")
        result = driver.drive(self.runs, mid)
        self.assertEqual(result["status"], "abandoned")
        self.assertNotIn("failed closed", str(result.get("next_action", "")))
        self.assertEqual(len(self.submitted()), submits_before)

    def test_creation_receipt_pin_removal_blocks_new_effects(self) -> None:
        mid, _ = self.create(self.plan)
        digest = self.plan_digest()
        creation_path = self.runs / "missions" / mid / "creation-request.json"
        creation = json.loads(creation_path.read_text())
        self.assertEqual(creation["request"]["sdd_plan_sha256"], digest)
        del creation["request"]["sdd_plan_sha256"]
        creation_path.write_text(json.dumps(creation, sort_keys=True))
        self.assertTrue(self.blob_path(digest).exists())
        with mock.patch.object(driver._Driver, "prepare_candidate",
                               side_effect=AssertionError("candidate prepared")), \
                mock.patch.object(driver.fleet_herdr, "HerdrBackend",
                                  side_effect=AssertionError("backend constructed")):
            result = driver.drive(self.runs, mid)
        self.assertIn("failed closed before new effects", result["next_action"])
        self.assertIn("creation-request SDD pin differs", result["next_action"])
        self.assertEqual(fixture.FakeBackend.calls, [])

    def test_mid_drive_corruption_stops_before_next_stage_admission(self) -> None:
        mid, _ = self.create(self.plan)
        with mock.patch.object(driver.fleet_herdr, "HerdrBackend", CorruptingBackend):
            result = driver.drive(self.runs, mid)
        self.assertEqual(self.submitted(), ["plan"])
        self.assertIn("failed closed before new stage work", result["next_action"])
        current = fleet_mission.load_state(self.runs, mid)
        self.assertNotIn("herdr:build", {a["request_key"] for a in current["admissions"].values()})
        self.assertEqual(result["status"], "running")

    def test_corruption_after_verify_blocks_new_functional_check(self) -> None:
        from tests.test_fleet_functional import synthetic_spec

        mid, _ = self.create(self.plan, key="functional", functional_spec=synthetic_spec())
        marker = mock.Mock(side_effect=AssertionError("fleet_functional.run must not be called"))
        with mock.patch.object(driver.fleet_herdr, "HerdrBackend", VerifyCorruptingBackend), \
                mock.patch.object(driver.fleet_functional, "run", marker):
            result = driver.drive(self.runs, mid)
        marker.assert_not_called()
        self.assertIn("failed closed", result["next_action"])
        self.assertEqual(self.submitted(), ["plan", "build", "review", "verify"])
        current = fleet_mission.load_state(self.runs, mid)
        self.assertNotIn("herdr:synthesis", {a["request_key"] for a in current["admissions"].values()})

    def test_opt_in_on_existing_legacy_identity_is_rejected_without_poisoning(self) -> None:
        mid, _ = self.create(None, key="legacy-id")
        creation_path = self.runs / "missions" / mid / "creation-request.json"
        creation_before = creation_path.read_bytes()
        self.assertFalse(fleet_herdr_sdd.binding_exists(self.runs, mid))
        with self.assertRaises(state.MissionConflict):
            self.create(self.plan, key="legacy-id")
        self.assertFalse((self.runs / "sdd/missions" / f"{mid}.json").exists())
        self.assertFalse(fleet_herdr_sdd.binding_exists(self.runs, mid))
        self.assertTrue(self.plan.exists())
        retried, created = self.create(None, key="legacy-id")
        self.assertEqual(retried, mid)
        self.assertFalse(created)
        self.assertEqual(creation_path.read_bytes(), creation_before)
        self.assertEqual(driver.drive(self.runs, mid)["status"], "succeeded")

    def test_ledger_legacy_with_missing_receipt_rejects_opt_in_without_binding(self) -> None:
        mid, _ = self.create(None, key="legacy-missing")
        creation_path = self.runs / "missions" / mid / "creation-request.json"
        saved = creation_path.read_bytes()
        creation_path.unlink()
        with self.assertRaises(state.MissionConflict):
            self.create(self.plan, key="legacy-missing")
        self.assertFalse((self.runs / "sdd/missions" / f"{mid}.json").exists())
        self.assertFalse(fleet_herdr_sdd.binding_exists(self.runs, mid))
        creation_path.write_bytes(saved)
        os.chmod(creation_path, 0o600)
        retried, created = self.create(None, key="legacy-missing")
        self.assertEqual(retried, mid)
        self.assertFalse(created)
        self.assertEqual(creation_path.read_bytes(), saved)

    # -- idempotency, conflicts, legacy -----------------------------------

    def test_same_key_retry_idempotent_and_different_plan_conflicts(self) -> None:
        first, created_first = self.create(self.plan, key="stable")
        second, created_second = self.create(self.plan, key="stable")
        self.assertEqual(first, second)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        events = state.read_events(state.ledger_path(self.runs, first))
        self.assertEqual(sum(e["kind"] == "mission_created" for e in events), 1)
        binding = self.runs / "sdd/missions" / f"{first}.json"
        before = binding.read_bytes()
        other = self.tmp / "other.json"
        document = json.loads(self.plan.read_text())
        document["objective"] = "A different plan for the same identity"
        other.write_text(json.dumps(document, sort_keys=True))
        with self.assertRaises(state.MissionConflict):
            self.create(other, key="stable")
        self.assertEqual(binding.read_bytes(), before)

    def test_omitted_plan_on_existing_opt_in_does_not_strip_binding(self) -> None:
        mid, _ = self.create(self.plan)
        binding = self.runs / "sdd/missions" / f"{mid}.json"
        before = binding.read_bytes()
        with self.assertRaises(state.MissionConflict):
            self.create(None, key="sdd-fixture")
        self.assertEqual(binding.read_bytes(), before)

    def test_legacy_mission_omits_field_and_completes(self) -> None:
        mid, _ = self.create(None, key="legacy")
        current = fleet_mission.load_state(self.runs, mid)
        self.assertNotIn("sdd_plan_sha256", current)
        self.assertIsNone(fleet_herdr_sdd.verify(self.runs, current))
        result = driver.drive(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(self.submitted()), 5)

    def test_malformed_plan_rejected_before_any_mission(self) -> None:
        bad = self.tmp / "bad.json"
        text = self.plan.read_text()
        bad.write_text(text[:-1] + ',"objective":"again"}')
        with self.assertRaises(state.MissionStateError):
            self.create(bad, key="bad")
        missions = self.runs / "missions"
        self.assertFalse(missions.exists() and any(p.is_dir() for p in missions.iterdir()))
        self.assertFalse((self.runs / "sdd").exists())

    # -- CLI propagation ---------------------------------------------------

    def test_parser_exposes_sdd_plan_on_run_only(self) -> None:
        args = mission_run._parser().parse_args(["run", "f", "o", "--sdd-plan", "/tmp/plan.json"])
        self.assertEqual(str(args.sdd_plan), "/tmp/plan.json")
        with self.assertRaises(SystemExit):
            mission_run._parser().parse_args(["dry", "f", "o", "--sdd-plan", "/tmp/plan.json"])

    def test_cli_run_propagates_sdd_plan_and_pins_digest(self) -> None:
        contract_path = self.tmp / "contract.json"
        contract_path.write_text(json.dumps(self.contract()))
        args = ["--runs-dir", str(self.runs), "run", "sdd-cli", "objective",
                "--workflow", "herdr-implementation", "--target-repo", str(self.target),
                "--herdr-session", "sdd-cli", "--acceptance-contract", str(contract_path),
                "--sdd-plan", str(self.plan), "--json"]
        with mock.patch.object(mission_run.fleet_herdr_mission, "drive",
                               return_value={"stub": True}), \
                mock.patch.object(mission_run, "emit"):
            code = mission_run.main(args)
        self.assertEqual(code, 0)
        mission_dirs = [p for p in (self.runs / "missions").iterdir() if p.is_dir()]
        self.assertEqual(len(mission_dirs), 1)
        mid = mission_dirs[0].name
        events = state.read_events(state.ledger_path(self.runs, mid))
        self.assertEqual(events[0]["payload"]["sdd_plan_sha256"], self.plan_digest())

    def test_non_herdr_profile_rejects_sdd_plan(self) -> None:
        fake = {"resolved": {"preset": "dan"}}
        with mock.patch.object(mission_run.workflow_config, "compile_path", return_value=fake):
            with self.assertRaises(mission_run.MissionRunError):
                mission_run.create_and_drive(
                    self.runs, feature="sdd", objective="objective", workflow_name="implementation",
                    target_repo=self.target, risk_override="auto", timeout_seconds=600,
                    allow_dirty_baseline=False, teardown=False, sdd_plan_path=self.plan)


if __name__ == "__main__":
    unittest.main()
