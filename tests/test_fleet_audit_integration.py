from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest import mock


import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_audit_client as client
import fleet_mission
import fleet_mission_state as state
import workflow_config


class FleetAuditIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs = self.tmp / "runs"
        self.target = self.tmp / "target"
        self.target.mkdir()
        compiled = workflow_config.compile_path(ROOT / "workflows" / "implementation.yaml")
        self.mission_id, _ = fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature="audit-live",
            objective="verify signed audit",
            target_repo=self.target.resolve(),
            base_sha="a" * 40,
            idempotency_key="create:audit-live",
        )
        self.manifest = self.runs / "fleet-audit-live.manifest"
        self.manifest.write_text(
            f"feature=audit-live\nmission_id={self.mission_id}\n"
            f"target_repo={self.target.resolve()}\npreset=fleet_dialogue\nmode=assured\n",
            encoding="utf-8",
        )
        self.lifecycle = client.AuditLifecycle(self.runs, self.mission_id)
        self.addCleanup(self.force_cleanup)

    def force_cleanup(self) -> None:
        try:
            value = json.loads(self.lifecycle.lifecycle_path.read_text(encoding="utf-8"))
            if value.get("stopped_at") is None:
                pid = int(value["pid"])
                os.kill(pid, signal.SIGTERM)
                process = client._LIVE_PROCESSES.pop(pid, None)
                if process is not None:
                    process.wait(timeout=2)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    def test_signed_lifecycle_is_idempotent_and_verifies_offline(self) -> None:
        first = self.lifecycle.start(self.manifest)
        second = self.lifecycle.start(self.manifest)
        self.assertTrue(first["started"])
        self.assertFalse(second["started"])
        current = fleet_mission.load_state(self.runs, self.mission_id)
        recorded = self.lifecycle.record_control_event(
            event_type="MissionEvent",
            subject_id=self.mission_id,
            subject_sha256=current["head_sha256"],
            metadata={"kind": "workflow_compiled", "sequence": current["last_sequence"]},
            idempotency_key="mission:event:2",
        )
        repeated = self.lifecycle.record_control_event(
            event_type="MissionEvent",
            subject_id=self.mission_id,
            subject_sha256=current["head_sha256"],
            metadata={"kind": "workflow_compiled", "sequence": current["last_sequence"]},
            idempotency_key="mission:event:2",
        )
        self.assertEqual(recorded, repeated)
        verified = self.lifecycle.verify()
        self.assertTrue(verified["valid"])
        self.assertFalse(verified["worm"])
        stopped = self.lifecycle.stop()
        self.assertTrue(stopped["stopped"])
        offline = client.verify_offline(
            self.lifecycle.ledger_root / self.mission_id / "a2a_ledger.jsonl",
            self.lifecycle.receipt_path,
            self.lifecycle.public_key,
            self.lifecycle.anchor_receipts,
            require_worm=False,
        )
        self.assertEqual(offline["records"], 3)
        self.assertFalse(offline["worm"])
        self.assertNotIn("control-hmac", self.lifecycle.receipt_path.read_text(encoding="utf-8"))

    def test_raw_metadata_is_rejected_and_tampering_breaks_offline_verify(self) -> None:
        self.lifecycle.start(self.manifest)
        secret = "raw-secret-value"
        with self.assertRaisesRegex(client.AuditClientError, "unsafe"):
            self.lifecycle.record_control_event(
                event_type="MissionEvent",
                subject_id=self.mission_id,
                subject_sha256="b" * 64,
                metadata={"raw_payload": secret},
                idempotency_key="raw:rejected",
            )
        self.lifecycle.verify()
        ledger = self.lifecycle.ledger_root / self.mission_id / "a2a_ledger.jsonl"
        self.assertNotIn(secret, ledger.read_text(encoding="utf-8"))
        rows = ledger.read_text(encoding="utf-8").splitlines()
        value = json.loads(rows[-1])
        value["metadata"]["mission_status"] = "tampered"
        rows[-1] = json.dumps(value, separators=(",", ":"), sort_keys=True)
        ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(client.AuditClientError, "digest mismatch"):
            client.verify_offline(
                ledger,
                self.lifecycle.receipt_path,
                self.lifecycle.public_key,
                self.lifecycle.anchor_receipts,
                require_worm=False,
            )

    def test_worm_profile_fails_closed_without_compliance_configuration(self) -> None:
        workflow_value = json.loads((ROOT / "workflows" / "implementation.yaml").read_text())
        workflow_value["name"] = "worm-test"
        workflow_value["audit"]["mode"] = "worm"
        path = self.tmp / "worm.yaml"
        path.write_text(json.dumps(workflow_value), encoding="utf-8")
        compiled = workflow_config.compile_path(path)
        worm_id, _ = fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature="audit-worm",
            objective="verify worm startup",
            target_repo=self.target.resolve(),
            base_sha="a" * 40,
            idempotency_key="create:audit-worm",
        )
        manifest = self.runs / "fleet-audit-worm.manifest"
        manifest.write_text(
            f"feature=audit-worm\nmission_id={worm_id}\ntarget_repo={self.target.resolve()}\n",
            encoding="utf-8",
        )
        lifecycle = client.AuditLifecycle(self.runs, worm_id)
        names = {
            "FLEET_WORM_BUCKET": "",
            "FLEET_WORM_REGION": "",
            "AWS_REGION": "",
            "AWS_DEFAULT_REGION": "",
            "AWS_ACCESS_KEY_ID": "",
            "AWS_SECRET_ACCESS_KEY": "",
        }
        with mock.patch.dict(os.environ, names), self.assertRaisesRegex(
            client.AuditClientError, "missing S3 Object Lock"
        ):
            lifecycle.preflight()
        self.assertFalse(lifecycle.socket_path.exists())
        self.assertFalse(lifecycle.lifecycle_path.exists())
        self.assertFalse(lifecycle.root.exists(), "preflight must not create audit state")

    def test_offline_worm_verification_rejects_partial_anchor_receipt(self) -> None:
        self.lifecycle.start(self.manifest)
        ledger = self.lifecycle.ledger_root / self.mission_id / "a2a_ledger.jsonl"
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        previous = client.audit.GENESIS_SHA256
        for index, event in enumerate(rows, 1):
            event["worm_compliance_mode"] = True
            event["worm_backend"] = "s3-object-lock-compliance"
            event["worm_object_key"] = f"fleet-audits/{self.mission_id}/{index}.json"
            event["previous_event_sha256"] = previous
            unsigned = {
                key: value for key, value in event.items()
                if key not in {"event_sha256", "control_signature"}
            }
            event["event_sha256"] = client.audit.digest(unsigned)
            event["control_signature"] = "historical-live-signature"
            previous = event["event_sha256"]
            anchor = {
                "schema_version": 1,
                "worm": True,
                "backend": "s3-object-lock-compliance",
                "object_key": event["worm_object_key"],
                "event_sha256": event["event_sha256"],
                "retention_mode": "COMPLIANCE",
                "retained_until": "2030-01-01T00:00:00Z",
            }
            (self.lifecycle.anchor_receipts / f"{event['event_id']}.json").write_text(
                json.dumps(anchor, sort_keys=True) + "\n", encoding="utf-8"
            )
        ledger.write_text(
            "".join(json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n" for item in rows),
            encoding="utf-8",
        )
        receipt = {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "records": len(rows),
            "head_sha256": rows[-1]["event_sha256"],
            "ledger_sha256": client.audit.file_sha256(ledger),
            "worm": True,
            "backend": "s3-object-lock-compliance",
            "public_key_sha256": client.audit.file_sha256(self.lifecycle.public_key),
            "verified_at": "2026-07-14T00:00:00Z",
        }
        receipt["ed25519_signature"] = client._sign_receipt(self.lifecycle.private_key, receipt)
        self.lifecycle.receipt_path.write_text(
            json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(client.AuditClientError, "incomplete"):
            client.verify_offline(
                ledger,
                self.lifecycle.receipt_path,
                self.lifecycle.public_key,
                self.lifecycle.anchor_receipts,
                require_worm=True,
            )


if __name__ == "__main__":
    unittest.main()
