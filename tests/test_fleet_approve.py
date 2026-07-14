from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("fleet_approve", ROOT / "scripts" / "fleet-approve.py")
assert SPEC and SPEC.loader
fleet_approve = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet_approve)

import fleet_mission
import fleet_mission_state as state
import workflow_config


class FleetApproveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs = self.tmp / "runs"
        self.target = self.tmp / "target"
        self.target.mkdir()
        self.compiled = workflow_config.compile_path(ROOT / "workflows" / "implementation.yaml")
        self.mission_id, _ = fleet_mission.create_mission(
            self.runs,
            compiled=self.compiled,
            feature="approve",
            objective="operate on the production service",
            target_repo=self.target.resolve(),
            base_sha="a" * 40,
            idempotency_key="create:approve",
        )
        state.append_event(
            self.runs, self.mission_id, kind="risk_escalated", actor="CONTROL",
            idempotency_key="risk", payload={
                "from": "low", "to": "high", "categories": ["production"], "reason": "risk",
            },
        )
        state.append_event(
            self.runs, self.mission_id, kind="assurance_requested", actor="CONTROL",
            idempotency_key="request", payload={
                "risk": "high", "categories": ["production"],
                "scope": str(self.target.resolve()),
                "workflow_digest": self.compiled["workflow_digest"],
            },
        )

    def test_mission_approval_is_scoped_expiring_and_idempotent(self) -> None:
        first = fleet_approve.approve_mission(
            self.runs, self.mission_id, scope=str(self.target), expires_in=600,
            idempotency_key="human:approve",
        )
        second = fleet_approve.approve_mission(
            self.runs, self.mission_id, scope=str(self.target), expires_in=600,
            idempotency_key="human:approve",
        )
        self.assertTrue(first["appended"])
        self.assertFalse(second["appended"])
        self.assertEqual(first["event"], second["event"])
        current = fleet_mission.load_state(self.runs, self.mission_id)
        self.assertEqual(current["status"], "assurance_approved")
        self.assertEqual(current["approval"]["scope"], str(self.target.resolve()))

    def test_mission_approval_rejects_scope_drift(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "scope"):
            fleet_approve.approve_mission(
                self.runs, self.mission_id, scope=str(self.tmp / "other"), expires_in=600,
                idempotency_key="human:wrong",
            )

    def test_sensitive_full_archive_approval_is_scoped_and_idempotent(self) -> None:
        # The assurance request made this mission high risk; add the independent
        # data category that specifically gates a full archive.
        state.append_event(
            self.runs, self.mission_id, kind="risk_escalated", actor="CONTROL",
            idempotency_key="archive-risk", payload={
                "from": "high", "to": "high", "categories": ["credentials"],
                "reason": "archive policy test",
            },
        )
        first = fleet_approve.approve_archive(
            self.runs, self.mission_id, scope=str(self.target), expires_in=600,
            idempotency_key="human:archive:approve",
        )
        second = fleet_approve.approve_archive(
            self.runs, self.mission_id, scope=str(self.target), expires_in=600,
            idempotency_key="human:archive:approve",
        )
        self.assertTrue(first["appended"])
        self.assertFalse(second["appended"])
        self.assertEqual(first["approval"], second["approval"])
        self.assertEqual(first["approval"]["risk_categories"], ["credentials"])
        self.assertEqual(Path(first["path"]).stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
