from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest import mock

from tests.mission_control_test_support import legacy_v1_compiled, write_compiled


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_export_trace
import fleet_mission_state as mission_state
import workflow_config


class FleetExportTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)
        self.runs = self.tmp / "runs"
        self.runs.mkdir(mode=0o700)
        self.mission_id = str(uuid.uuid4())
        self.compiled = legacy_v1_compiled(
            workflow_config.compile_path(
                ROOT / "workflows" / "implementation.yaml"
            )
        )
        mission_state.append_event(
            self.runs,
            self.mission_id,
            kind="mission_created",
            actor="CONTROL",
            idempotency_key="historical:create",
            payload={
                "feature": "historical-trace",
                "objective_sha256": "a" * 64,
                "target_repo": str((self.tmp / "target").resolve()),
                "base_sha": "b" * 40,
                "workflow_digest": self.compiled["workflow_digest"],
                "initial_risk": "low",
            },
        )
        mission_state.append_event(
            self.runs,
            self.mission_id,
            kind="workflow_compiled",
            actor="CONTROL",
            idempotency_key="historical:compiled",
            payload={"compiled_digest": self.compiled["compiled_digest"]},
        )
        self.root = self.runs / "missions" / self.mission_id
        write_compiled(self.root / "compiled-workflow.json", self.compiled)
        audit = self.root / "audit"
        ledger = audit / "ledgers" / self.mission_id / "a2a_ledger.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("{}\n", encoding="utf-8")
        (audit / "audit-verification.json").write_text("{}\n", encoding="utf-8")
        (audit / "audit-signing-public.pem").write_text("public\n", encoding="utf-8")
        (audit / "anchor-receipts").mkdir()

    def test_historical_v1_without_trust_scope_is_verified_as_read_only(self) -> None:
        verified = [{"event_id": str(uuid.uuid4())}]
        with (
            mock.patch.object(
                fleet_export_trace.fleet_audit_client,
                "verify_offline",
            ) as verify,
            mock.patch.object(
                fleet_export_trace.fleet_audit_client,
                "read_verified_public_chain",
                return_value=verified,
            ),
        ):
            result = fleet_export_trace.verified_audit_events(
                self.root, self.mission_id
            )
        self.assertEqual(result, verified)
        self.assertFalse(verify.call_args.kwargs["require_worm"])
        self.assertIsNone(verify.call_args.kwargs["required_trust_scope"])

    def test_foreign_historical_policy_is_rejected_before_offline_verify(self) -> None:
        foreign = legacy_v1_compiled(
            workflow_config.compile_path(ROOT / "workflows" / "research.yaml")
        )
        write_compiled(self.root / "compiled-workflow.json", foreign)
        with (
            mock.patch.object(
                fleet_export_trace.fleet_audit_client, "verify_offline"
            ) as effect,
            self.assertRaisesRegex(
                fleet_export_trace.TraceExportError, "not bound"
            ),
        ):
            fleet_export_trace.verified_audit_events(self.root, self.mission_id)
        effect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
