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
from tests.mission_control_test_support import legacy_v1_compiled, write_compiled


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

    def test_historical_compiled_policy_is_rejected_before_audit_effects(self) -> None:
        path = self.runs / "missions" / self.mission_id / "compiled-workflow.json"
        compiled = client.fleet_compiled.load(path, mode="read")
        write_compiled(path, legacy_v1_compiled(compiled))
        with (
            mock.patch.object(state, "ensure_private_directory") as effect,
            mock.patch.object(client.subprocess, "Popen") as process,
            self.assertRaisesRegex(
                client.AuditClientError, "historical read-only.*require v2"
            ),
        ):
            client.AuditLifecycle(self.runs, self.mission_id)
        effect.assert_not_called()
        process.assert_not_called()
        self.assertFalse(self.lifecycle.root.exists())

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
        self.assertEqual(offline["trust_scope"], "local-development")
        self.assertNotIn("control-hmac", self.lifecycle.receipt_path.read_text(encoding="utf-8"))

    def test_stop_does_not_claim_success_while_service_remains_alive(self) -> None:
        self.lifecycle.start(self.manifest)
        with (
            mock.patch.object(self.lifecycle, "verify", return_value={"valid": True}),
            mock.patch.object(self.lifecycle, "health", return_value={"status": "ok"}),
            mock.patch.object(self.lifecycle, "_pid_alive", return_value=True),
            mock.patch.object(self.lifecycle, "_process", None),
            mock.patch.dict(client._LIVE_PROCESSES, {}, clear=True),
            mock.patch.object(client.os, "kill") as kill,
            mock.patch.object(client.time, "monotonic", side_effect=[0.0, 6.0]),
            self.assertRaisesRegex(client.AuditClientError, "did not stop"),
        ):
            self.lifecycle.stop()
        kill.assert_any_call(mock.ANY, signal.SIGTERM)
        kill.assert_any_call(mock.ANY, signal.SIGKILL)
        lifecycle = json.loads(self.lifecycle.lifecycle_path.read_text(encoding="utf-8"))
        self.assertIsNone(lifecycle["stopped_at"])

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

    def test_initial_anchor_failure_cleans_service_socket_and_lifecycle(self) -> None:
        live_before = set(client._LIVE_PROCESSES)
        real_send = client.audit.send_request

        def fail_run_started(socket_path, request):
            if request.get("operation") == "run_started":
                raise RuntimeError("synthetic anchor failure")
            return real_send(socket_path, request)

        with (
            mock.patch.object(client.audit, "send_request", side_effect=fail_run_started),
            self.assertRaisesRegex(client.AuditClientError, "synthetic anchor failure"),
        ):
            self.lifecycle.start(self.manifest)
        self.assertFalse(self.lifecycle.socket_path.exists())
        self.assertFalse(self.lifecycle.lifecycle_path.exists())
        self.assertEqual(set(client._LIVE_PROCESSES), live_before)

    def test_second_anchor_failure_also_cleans_service_state(self) -> None:
        live_before = set(client._LIVE_PROCESSES)
        real_send = client.audit.send_request

        def fail_control_event(socket_path, request):
            if request.get("operation") == "control_event":
                raise RuntimeError("synthetic second anchor failure")
            return real_send(socket_path, request)

        with (
            mock.patch.object(client.audit, "send_request", side_effect=fail_control_event),
            self.assertRaisesRegex(client.AuditClientError, "second anchor failure"),
        ):
            self.lifecycle.start(self.manifest)
        self.assertFalse(self.lifecycle.socket_path.exists())
        self.assertFalse(self.lifecycle.lifecycle_path.exists())
        self.assertEqual(set(client._LIVE_PROCESSES), live_before)

    def test_worm_profile_fails_closed_without_compliance_configuration(self) -> None:
        workflow_value = json.loads((ROOT / "workflows" / "local-worm.yaml").read_text())
        workflow_value["name"] = "worm-test"
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
            event["worm_trust_scope"] = "external-compliance"
            event["worm_retention_mode"] = "COMPLIANCE"
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
                "trust_scope": "external-compliance",
                "object_key": event["worm_object_key"],
                "event_sha256": event["event_sha256"],
                "retention_mode": "COMPLIANCE",
                "retained_until": "2030-01-01T00:00:00Z",
                "version_id": f"version-{index}",
            }
            (self.lifecycle.anchor_receipts / f"{event['event_id']}.json").write_bytes(
                state.canonical_bytes(anchor) + b"\n"
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
            "trust_scope": "external-compliance",
            "public_key_sha256": client.audit.file_sha256(self.lifecycle.public_key),
            "anchor_receipts_sha256": client._anchor_envelope(
                rows, self.lifecycle.anchor_receipts
            )[1],
            "verified_at": "2026-07-14T00:00:00Z",
        }
        receipt["ed25519_signature"] = client._sign_receipt(self.lifecycle.private_key, receipt)
        state.atomic_write(
            self.lifecycle.receipt_path,
            state.canonical_bytes(receipt) + b"\n",
        )
        verified = client.verify_offline(
            ledger,
            self.lifecycle.receipt_path,
            self.lifecycle.public_key,
            self.lifecycle.anchor_receipts,
            require_worm=True,
            required_trust_scope="external-compliance",
        )
        self.assertTrue(verified["worm"])
        self.assertEqual(verified["trust_scope"], "external-compliance")
        last_anchor_path = self.lifecycle.anchor_receipts / f"{rows[-1]['event_id']}.json"
        last_anchor = json.loads(last_anchor_path.read_text(encoding="utf-8"))
        last_anchor.pop("version_id")
        last_anchor_path.write_bytes(state.canonical_bytes(last_anchor) + b"\n")
        receipt["anchor_receipts_sha256"] = client._anchor_envelope(
            rows, self.lifecycle.anchor_receipts
        )[1]
        receipt["ed25519_signature"] = client._sign_receipt(
            self.lifecycle.private_key, receipt
        )
        state.atomic_write(
            self.lifecycle.receipt_path,
            state.canonical_bytes(receipt) + b"\n",
        )
        with self.assertRaisesRegex(client.AuditClientError, "incomplete"):
            client.verify_offline(
                ledger,
                self.lifecycle.receipt_path,
                self.lifecycle.public_key,
                self.lifecycle.anchor_receipts,
                require_worm=True,
                required_trust_scope="external-compliance",
            )

    def test_offline_receipts_must_match_ledger_backend_scope_and_object(self) -> None:
        self.lifecycle.start(self.manifest)
        self.lifecycle.verify()
        ledger = self.lifecycle.ledger_root / self.mission_id / "a2a_ledger.jsonl"
        events = client.read_verified_public_chain(ledger)
        anchor_path = self.lifecycle.anchor_receipts / f"{events[0]['event_id']}.json"
        anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
        anchor["trust_scope"] = "external-compliance"
        anchor_path.write_bytes(state.canonical_bytes(anchor) + b"\n")
        with self.assertRaisesRegex(client.AuditClientError, "envelope hash mismatch"):
            client.verify_offline(
                ledger,
                self.lifecycle.receipt_path,
                self.lifecycle.public_key,
                self.lifecycle.anchor_receipts,
                require_worm=False,
                required_trust_scope="local-development",
            )

    def test_live_verification_receipt_advances_with_a_valid_ledger_prefix(self) -> None:
        self.lifecycle.start(self.manifest)
        first = self.lifecycle.verify()
        prior = json.loads(self.lifecycle.receipt_path.read_text(encoding="utf-8"))
        self.lifecycle.record_control_event(
            event_type="MissionEvent",
            subject_id=self.mission_id,
            subject_sha256="e" * 64,
            metadata={"kind": "advanced", "sequence": 3},
            idempotency_key="receipt:advance",
        )
        second = self.lifecycle.verify()
        current = json.loads(self.lifecycle.receipt_path.read_text(encoding="utf-8"))
        self.assertGreater(second["records"], first["records"])
        self.assertGreater(current["records"], prior["records"])
        self.assertNotEqual(current["head_sha256"], prior["head_sha256"])
        self.assertNotEqual(
            current["anchor_receipts_sha256"], prior["anchor_receipts_sha256"]
        )

    def test_external_compliance_preflight_rejects_loopback_without_state(self) -> None:
        workflow_value = json.loads((ROOT / "workflows" / "regulated.yaml").read_text())
        workflow_value["name"] = "external-test"
        workflow_value["limits"]["budget_mode"] = "soft"
        path = self.tmp / "external.yaml"
        path.write_text(json.dumps(workflow_value), encoding="utf-8")
        compiled = workflow_config.compile_path(path)
        mission_id, _ = fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature="audit-external",
            objective="reject local endpoint for external trust",
            target_repo=self.target.resolve(),
            base_sha="a" * 40,
            idempotency_key="create:audit-external",
        )
        lifecycle = client.AuditLifecycle(self.runs, mission_id)
        env = {
            "FLEET_WORM_BUCKET": "bucket",
            "FLEET_WORM_REGION": "us-east-1",
            "FLEET_WORM_ENDPOINT": "https://localhost:9443",
            "AWS_ACCESS_KEY_ID": "ephemeral-access",
            "AWS_SECRET_ACCESS_KEY": "ephemeral-secret",
        }
        loopback = [
            (client.audit.socket.AF_INET, client.audit.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 9443))
        ]
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(client.audit.socket, "getaddrinfo", return_value=loopback),
            self.assertRaisesRegex(client.AuditClientError, "public global"),
        ):
            lifecycle.preflight()
        self.assertFalse(lifecycle.root.exists(), "rejected trust must not create audit state")


if __name__ == "__main__":
    unittest.main()
