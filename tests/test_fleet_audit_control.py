from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "fleet_audit_control.py"
SPEC = importlib.util.spec_from_file_location("fleet_audit_control", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


class FleetAuditControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fleet-audit-control-")
        self.root = Path(self.temp.name) / "ledger"
        self.anchor = Path(self.temp.name) / "anchor"
        self.root.mkdir(mode=0o700)
        self.anchor.mkdir(mode=0o700)
        self.key = b"test-control-key-32-bytes-minimum!!"
        self.ledger = audit.AuditLedger(
            self.root,
            self.key,
            audit.DirectoryTestSink(self.anchor),
        )
        self.service = audit.AuditService(
            self.ledger,
            control_uid=os.geteuid(),
            maker_uid=65002,
            human_subject="human-low-entropy-name",
        )
        self.run_id = str(uuid.uuid4())
        self.artifact = Path(self.temp.name) / "hook.sh"
        self.artifact.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def start_run(self) -> dict[str, object]:
        return self.service.handle(
            {
                "operation": "run_started",
                "run_id": self.run_id,
                "model_version": "claude-test-model",
                "data_classification": "restricted",
                "artifacts": {"pre_hook": str(self.artifact)},
            },
            os.geteuid(),
        )

    def test_control_writer_hashes_payloads_and_signs_complete_chain(self) -> None:
        started = self.start_run()
        secret = "raw-secret-must-not-enter-ledger"
        hook_common = {
            "session_id": "session-1",
            "tool_name": "Bash",
            "tool_use_id": "tool-1",
            "tool_input": {"command": f"printf {secret}"},
        }
        self.service.handle(
            {
                "operation": "hook_event",
                "run_id": self.run_id,
                "model_version": "claude-test-model",
                "hook": {**hook_common, "hook_event_name": "PreToolUse"},
            },
            65002,
        )
        self.service.handle(
            {
                "operation": "hook_event",
                "run_id": self.run_id,
                "model_version": "claude-test-model",
                "hook": {
                    **hook_common,
                    "hook_event_name": "PostToolUse",
                    "tool_response": {"output": secret},
                },
            },
            65002,
        )

        events = self.ledger.read_verified(self.run_id)
        self.assertEqual([item["event_type"] for item in events], [
            "RunStarted",
            "PreToolUse",
            "PostToolUse",
        ])
        self.assertEqual(events[0]["human_uid"], started["human_uid"])
        self.assertRegex(str(events[0]["human_uid"]), r"^hmac-sha256:[0-9a-f]{64}$")
        self.assertEqual(events[0]["maker_uid"], 65002)
        self.assertEqual(events[1]["args_sha256"], events[2]["args_sha256"])
        self.assertIn("response_sha256", events[2])
        self.assertTrue(all(item["control_signature"] for item in events))
        self.assertTrue(all(item["worm_compliance_mode"] is False for item in events))
        self.assertNotIn(secret, (self.root / self.run_id / "a2a_ledger.jsonl").read_text())
        anchor_text = "".join(path.read_text() for path in self.anchor.rglob("*.json"))
        self.assertNotIn(secret, anchor_text)

    def test_peer_uid_contract_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "CONTROL peer UID"):
            self.service.handle(
                {
                    "operation": "run_started",
                    "run_id": self.run_id,
                    "artifacts": {"pre_hook": str(self.artifact)},
                },
                65002,
            )
        self.start_run()
        with self.assertRaisesRegex(RuntimeError, "Maker peer UID"):
            self.service.handle(
                {
                    "operation": "hook_event",
                    "run_id": self.run_id,
                    "hook": {"hook_event_name": "PreToolUse"},
                },
                os.geteuid(),
            )

    def test_tampering_breaks_signature_verification(self) -> None:
        self.start_run()
        path = self.root / self.run_id / "a2a_ledger.jsonl"
        event = json.loads(path.read_text())
        event["model_version"] = "tampered"
        path.write_text(json.dumps(event, separators=(",", ":")) + "\n")
        with self.assertRaisesRegex(RuntimeError, "signature"):
            self.ledger.read_verified(self.run_id)

    def test_verify_marks_test_sink_non_compliant(self) -> None:
        self.start_run()
        result = self.service.handle(
            {"operation": "verify", "run_id": self.run_id}, os.geteuid()
        )
        self.assertEqual(result["records"], 1)
        self.assertFalse(result["worm_compliance_mode"])

    def test_s3_environment_is_fail_closed_when_configuration_is_missing(self) -> None:
        names = (
            "FLEET_WORM_BUCKET",
            "FLEET_WORM_REGION",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        )
        previous = {name: os.environ.pop(name, None) for name in names}
        try:
            with self.assertRaisesRegex(RuntimeError, "missing S3 Object Lock"):
                audit.S3ObjectLockSink.from_environment()
        finally:
            for name, value in previous.items():
                if value is not None:
                    os.environ[name] = value

    def test_s3_anchor_requires_compliance_headers_and_versioned_receipt(self) -> None:
        sink = audit.S3ObjectLockSink(
            bucket="audit-bucket",
            region="us-east-1",
            credentials=audit.S3Credentials("AKIATEST", "not-a-real-secret"),
            endpoint="https://s3.us-east-1.amazonaws.com",
            retention_days=30,
        )
        requests = []

        class Response:
            def __init__(self, headers: dict[str, str]) -> None:
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

        def fake_urlopen(request, timeout):
            requests.append(request)
            if request.method == "PUT":
                return Response({"x-amz-version-id": "version-1"})
            return Response(
                {
                    "x-amz-object-lock-mode": "COMPLIANCE",
                    "x-amz-object-lock-retain-until-date": "2030-01-01T00:00:00Z",
                    "x-amz-meta-event-sha256": "a" * 64,
                }
            )

        with mock.patch.object(audit.urllib.request, "urlopen", fake_urlopen):
            sink.anchor("fleet-audits/run/1.json", b"{}", "a" * 64)

        self.assertEqual([request.method for request in requests], ["PUT", "HEAD"])
        put_headers = {name.lower(): value for name, value in requests[0].headers.items()}
        self.assertEqual(put_headers["x-amz-object-lock-mode"], "COMPLIANCE")
        self.assertIn("x-amz-object-lock-retain-until-date", put_headers)
        self.assertEqual(put_headers["x-amz-meta-event-sha256"], "a" * 64)
        self.assertIn("AWS4-HMAC-SHA256", put_headers["authorization"])
        self.assertNotIn("not-a-real-secret", put_headers["authorization"])


if __name__ == "__main__":
    unittest.main()
