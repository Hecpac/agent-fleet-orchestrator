from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
mission = importlib.import_module("fleet_mission")
state = importlib.import_module("fleet_mission_state")
workflow = importlib.import_module("workflow_config")


class MissionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs = self.tmp / "runs"
        self.target = self.tmp / "target"
        self.target.mkdir()
        self.compiled = workflow.compile_path(ROOT / "workflows" / "implementation.yaml")
        self.mission_id, created = mission.create_mission(
            self.runs,
            compiled=self.compiled,
            feature="demo",
            objective="implement durable state",
            target_repo=self.target.resolve(),
            base_sha="a" * 40,
            idempotency_key="create:demo",
        )
        self.assertTrue(created)

    def create_again(self, *, objective: str = "implement durable state") -> tuple[str, bool]:
        return mission.create_mission(
            self.runs,
            compiled=copy.deepcopy(self.compiled),
            feature="demo",
            objective=objective,
            target_repo=self.target.resolve(),
            base_sha="a" * 40,
            idempotency_key="create:demo",
        )

    def test_create_is_durable_and_idempotent_across_restart(self) -> None:
        second, created = self.create_again()
        self.assertEqual(second, self.mission_id)
        self.assertFalse(created)
        reloaded = mission.load_state(self.runs, self.mission_id)
        self.assertEqual(reloaded["status"], "compiled")
        self.assertEqual(reloaded["last_sequence"], 2)

    def test_create_recovers_kill_between_files_and_first_append(self) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        ledger.unlink()
        recovered, created = self.create_again()
        self.assertEqual(recovered, self.mission_id)
        self.assertFalse(created)
        self.assertEqual(mission.load_state(self.runs, recovered)["status"], "compiled")

    def test_atomic_write_rejects_broken_symlink_target(self) -> None:
        path = self.tmp / "private" / "state.json"
        path.parent.mkdir(mode=0o700)
        missing = self.tmp / "missing.json"
        path.symlink_to(missing)
        with self.assertRaisesRegex(state.MissionStateError, "refusing symlink"):
            state.atomic_write(path, b"{}\n")
        self.assertTrue(path.is_symlink())
        self.assertFalse(missing.exists())

    def test_create_key_with_different_payload_conflicts(self) -> None:
        with self.assertRaisesRegex(state.MissionConflict, "conflicts"):
            self.create_again(objective="different objective")

    def test_retry_same_key_does_not_duplicate_and_payload_drift_fails(self) -> None:
        event, appended = state.append_event(
            self.runs,
            self.mission_id,
            kind="fleet_boot_started",
            actor="CONTROL",
            idempotency_key="boot:1",
            payload={"feature": "demo"},
        )
        repeated, appended_again = state.append_event(
            self.runs,
            self.mission_id,
            kind="fleet_boot_started",
            actor="CONTROL",
            idempotency_key="boot:1",
            payload={"feature": "demo"},
        )
        self.assertTrue(appended)
        self.assertFalse(appended_again)
        self.assertEqual(repeated, event)
        with self.assertRaisesRegex(state.MissionConflict, "another mission request"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="fleet_boot_started",
                actor="CONTROL",
                idempotency_key="boot:1",
                payload={"feature": "other"},
            )

    def test_risk_is_monotonic_and_invalid_event_is_not_persisted(self) -> None:
        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_escalated",
            actor="lead",
            idempotency_key="risk:1",
            payload={"from": "low", "to": "high", "categories": ["production"], "reason": "deploy"},
        )
        current = mission.load_state(self.runs, self.mission_id)
        self.assertEqual(current["risk"], "high")
        before = current["last_sequence"]
        with self.assertRaisesRegex(state.MissionStateError, "cannot decrease"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="risk_escalated",
                actor="lead",
                idempotency_key="risk:2",
                payload={"from": "high", "to": "low", "categories": [], "reason": "retry"},
            )
        self.assertEqual(mission.load_state(self.runs, self.mission_id)["last_sequence"], before)

    def test_lineage_and_artifact_identity_are_derived(self) -> None:
        delegation_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        artifact = state.artifact_id("exact result")
        state.append_event(
            self.runs,
            self.mission_id,
            kind="delegation_registered",
            actor="lead",
            idempotency_key="delegation:1",
            payload={
                "delegation_id": delegation_id,
                "mission_id": self.mission_id,
                "run_id": run_id,
                "parent_run_id": None,
                "delegated_by": "lead",
                "recipient_instance": "scout",
                "capability": "recon",
                "objective_sha256": state.artifact_id("inspect"),
                "input_artifact_ids": [],
                "expected_output_contract": {"type": "text"},
                "deadline": "2026-07-14T01:00:00Z",
                "provider": "openai",
                "model": "gpt-test",
                "variant": None,
                "depth": 1,
                "token_id": None,
            },
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="result_recorded",
            actor="CONTROL",
            idempotency_key="result:1",
            payload={
                "run_id": run_id,
                "delegation_id": delegation_id,
                "artifact_id": artifact,
                "provider": "openai",
                "model": "gpt-test",
                "variant": None,
            },
        )
        second_delegation = str(uuid.uuid4())
        second_run = str(uuid.uuid4())
        state.append_event(
            self.runs,
            self.mission_id,
            kind="delegation_registered",
            actor="lead",
            idempotency_key="delegation:2",
            payload={
                "delegation_id": second_delegation,
                "mission_id": self.mission_id,
                "run_id": second_run,
                "parent_run_id": None,
                "delegated_by": "lead",
                "recipient_instance": "challenger",
                "capability": "challenge",
                "objective_sha256": state.artifact_id("challenge"),
                "input_artifact_ids": [],
                "expected_output_contract": {"type": "text"},
                "deadline": "2026-07-14T01:00:00Z",
                "provider": "anthropic",
                "model": "claude-test",
                "variant": None,
                "depth": 1,
                "token_id": None,
            },
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="result_recorded",
            actor="CONTROL",
            idempotency_key="result:2",
            payload={
                "run_id": second_run,
                "delegation_id": second_delegation,
                "artifact_id": artifact,
                "provider": "anthropic",
                "model": "claude-test",
                "variant": None,
            },
        )
        current = mission.load_state(self.runs, self.mission_id)
        self.assertEqual(current["results"][delegation_id]["run_id"], run_id)
        self.assertEqual(current["results"][second_delegation]["run_id"], second_run)

    def test_first_terminal_is_immutable_and_success_requires_archive(self) -> None:
        before = mission.load_state(self.runs, self.mission_id)["last_sequence"]
        with self.assertRaisesRegex(state.MissionStateError, "requires archived"):
            state.append_terminal(
                self.runs,
                self.mission_id,
                status="succeeded",
                reason="too early",
                idempotency_key="terminal:early",
            )
        self.assertEqual(mission.load_state(self.runs, self.mission_id)["last_sequence"], before)
        state.append_terminal(
            self.runs,
            self.mission_id,
            status="failed",
            reason="expected fixture",
            idempotency_key="terminal:1",
        )
        with self.assertRaisesRegex(state.MissionConflict, "immutable"):
            state.append_terminal(
                self.runs,
                self.mission_id,
                status="blocked",
                reason="second",
                idempotency_key="terminal:2",
            )

    def test_tampering_and_partial_append_break_verification(self) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        original = ledger.read_text(encoding="utf-8")
        ledger.write_text(original.replace('"initial_risk":"low"', '"initial_risk":"high"'), encoding="utf-8")
        with self.assertRaisesRegex(state.MissionStateError, "hash mismatch"):
            state.read_events(ledger, expected_mission_id=self.mission_id)
        ledger.write_text(original + '{"partial":', encoding="utf-8")
        with self.assertRaisesRegex(state.MissionStateError, "partial"):
            state.read_events(ledger, expected_mission_id=self.mission_id)

    def test_resume_plan_survives_new_process(self) -> None:
        script = ROOT / "scripts" / "fleet_mission.py"
        result = subprocess.run(
            [
                "python3", str(script), "--runs-dir", str(self.runs),
                "resume-plan", "--mission-id", self.mission_id,
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["next_action"], "boot")

    def test_scoped_approval_drives_assured_boot_transitions(self) -> None:
        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_escalated",
            actor="CONTROL",
            idempotency_key="approval:risk",
            payload={
                "from": "low", "to": "high", "categories": ["production"],
                "reason": "test assurance",
            },
        )
        request, _ = state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_requested",
            actor="CONTROL",
            idempotency_key="approval:request",
            payload={
                "risk": "high", "categories": ["production"],
                "scope": str(self.target.resolve()),
                "workflow_digest": self.compiled["workflow_digest"],
            },
        )
        approval, _ = state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_approved",
            actor="HUMAN",
            idempotency_key="approval:decision",
            payload={
                "approval_id": str(uuid.uuid4()),
                "request_event_sha256": request["event_sha256"],
                "workflow_digest": self.compiled["workflow_digest"],
                "scope": str(self.target.resolve()),
                "risk": "high",
                "expires_at": "2099-07-15T00:00:00Z",
                "approved_by_sha256": "c" * 64,
                "decision": "approved",
            },
        )
        self.assertEqual(state.resume_plan(mission.load_state(self.runs, self.mission_id))["next_action"], "boot_assured")
        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_boot_started",
            actor="CONTROL",
            idempotency_key="approval:boot",
            payload={"preset": "fleet_dialogue", "approval_event_sha256": approval["event_sha256"]},
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_started",
            actor="CONTROL",
            idempotency_key="approval:started",
            payload={
                "manifest": str(self.runs / "fleet-demo.manifest"),
                "approval_event_sha256": approval["event_sha256"],
            },
        )
        self.assertEqual(mission.load_state(self.runs, self.mission_id)["status"], "assured_running")

    def test_assurance_request_types_and_expired_boot_fail_closed(self) -> None:
        before = mission.load_state(self.runs, self.mission_id)["last_sequence"]
        with self.assertRaisesRegex(state.MissionStateError, "categories"):
            state.append_event(
                self.runs, self.mission_id, kind="assurance_requested", actor="CONTROL",
                idempotency_key="bad:categories",
                payload={
                    "risk": "high", "categories": [{}], "scope": str(self.target.resolve()),
                    "workflow_digest": self.compiled["workflow_digest"],
                },
            )
        self.assertEqual(mission.load_state(self.runs, self.mission_id)["last_sequence"], before)

        state.append_event(
            self.runs, self.mission_id, kind="risk_escalated", actor="CONTROL",
            idempotency_key="expired:risk",
            payload={
                "from": "low", "to": "high", "categories": ["production"],
                "reason": "exercise expiry",
            },
        )
        request, _ = state.append_event(
            self.runs, self.mission_id, kind="assurance_requested", actor="CONTROL",
            idempotency_key="expired:request",
            payload={
                "risk": "high", "categories": ["production"],
                "scope": str(self.target.resolve()),
                "workflow_digest": self.compiled["workflow_digest"],
            },
        )
        approval, _ = state.append_event(
            self.runs, self.mission_id, kind="assurance_approved", actor="HUMAN",
            idempotency_key="expired:approval",
            payload={
                "approval_id": str(uuid.uuid4()),
                "request_event_sha256": request["event_sha256"],
                "workflow_digest": self.compiled["workflow_digest"],
                "scope": str(self.target.resolve()), "risk": "high",
                "expires_at": "2000-01-01T00:00:00Z",
                "approved_by_sha256": "d" * 64, "decision": "approved",
            },
        )
        with self.assertRaisesRegex(state.MissionStateError, "expired before boot"):
            state.append_event(
                self.runs, self.mission_id, kind="assurance_boot_started", actor="CONTROL",
                idempotency_key="expired:boot",
                payload={
                    "preset": "fleet_dialogue",
                    "approval_event_sha256": approval["event_sha256"],
                },
            )


if __name__ == "__main__":
    unittest.main()
