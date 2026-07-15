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
        self.assertTrue(
            all(item["worm_trust_scope"] == "local-development" for item in events)
        )
        self.assertTrue(all(item["worm_retention_mode"] is None for item in events))
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
                audit.S3ObjectLockSink.from_environment("local-development")
        finally:
            for name, value in previous.items():
                if value is not None:
                    os.environ[name] = value

    def test_s3_anchor_requires_compliance_headers_and_versioned_receipt(self) -> None:
        addresses = [
            (audit.socket.AF_INET, audit.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ]
        with mock.patch.object(audit.socket, "getaddrinfo", return_value=addresses):
            sink = audit.S3ObjectLockSink(
                bucket="audit-bucket",
                region="us-east-1",
                credentials=audit.S3Credentials("AKIATEST", "not-a-real-secret"),
                endpoint="https://s3.us-east-1.amazonaws.com",
                retention_days=30,
                trust_scope="external-compliance",
            )
        requests = []

        class Response:
            def __init__(self, headers: dict[str, str]) -> None:
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

        def fake_open(_sink, request, *, timeout):
            self.assertEqual(timeout, 30)
            requests.append(request)
            if request.method == "PUT":
                return Response({"x-amz-version-id": "version-1"})
            retained_until = next(
                value
                for name, value in requests[0].headers.items()
                if name.lower() == "x-amz-object-lock-retain-until-date"
            )
            return Response(
                {
                    "x-amz-object-lock-mode": "COMPLIANCE",
                    "x-amz-object-lock-retain-until-date": retained_until,
                    "x-amz-meta-event-sha256": "a" * 64,
                    "x-amz-version-id": "version-1",
                }
            )

        with (
            mock.patch.object(audit.socket, "getaddrinfo", return_value=addresses),
            mock.patch.object(audit.S3ObjectLockSink, "_open", fake_open),
        ):
            receipt = sink.anchor("fleet-audits/run/1.json", b"{}", "a" * 64)

        self.assertEqual([request.method for request in requests], ["PUT", "HEAD"])
        put_headers = {name.lower(): value for name, value in requests[0].headers.items()}
        self.assertEqual(put_headers["x-amz-object-lock-mode"], "COMPLIANCE")
        self.assertIn("x-amz-object-lock-retain-until-date", put_headers)
        self.assertEqual(put_headers["x-amz-meta-event-sha256"], "a" * 64)
        self.assertIn("AWS4-HMAC-SHA256", put_headers["authorization"])
        self.assertNotIn("not-a-real-secret", put_headers["authorization"])
        self.assertEqual(
            audit.urllib.parse.parse_qs(audit.urllib.parse.urlsplit(requests[1].full_url).query),
            {"versionId": ["version-1"]},
        )
        self.assertEqual(receipt["version_id"], "version-1")
        self.assertEqual(receipt["trust_scope"], "external-compliance")

    def test_endpoint_trust_scopes_validate_resolved_addresses_not_hostname_text(self) -> None:
        credentials = audit.S3Credentials("AKIATEST", "not-a-real-secret")

        def result(*addresses: str):
            return [
                (audit.socket.AF_INET, audit.socket.SOCK_STREAM, 6, "", (address, 443))
                for address in addresses
            ]

        cases = (
            ("local-development", ("127.0.0.1",), True),
            ("local-development", ("8.8.8.8",), False),
            ("external-compliance", ("8.8.8.8",), True),
            ("external-compliance", ("127.0.0.1",), False),
            ("external-compliance", ("10.0.0.10",), False),
            ("external-compliance", ("169.254.1.1",), False),
            ("external-compliance", ("224.0.0.1",), False),
            ("external-compliance", ("8.8.8.8", "10.0.0.10"), False),
        )
        for trust_scope, addresses, accepted in cases:
            with self.subTest(trust_scope=trust_scope, addresses=addresses), mock.patch.object(
                audit.socket, "getaddrinfo", return_value=result(*addresses)
            ):
                if accepted:
                    sink = audit.S3ObjectLockSink(
                        "bucket", "us-east-1", credentials, "https://storage.example",
                        1, trust_scope,
                    )
                    self.assertEqual(sink.resolved_addresses, tuple(sorted(addresses)))
                else:
                    with self.assertRaisesRegex(RuntimeError, "resolve only"):
                        audit.S3ObjectLockSink(
                            "bucket", "us-east-1", credentials,
                            "https://storage.example", 1, trust_scope,
                        )

        with mock.patch.object(
            audit.socket, "getaddrinfo", return_value=result("8.8.8.8")
        ), self.assertRaisesRegex(RuntimeError, "public global"):
            audit.S3ObjectLockSink(
                "bucket", "us-east-1", credentials, "https://localhost", 1,
                "external-compliance",
            )

    def test_endpoint_and_ca_validation_fail_closed(self) -> None:
        credentials = audit.S3Credentials("AKIATEST", "not-a-real-secret")
        addresses = [
            (audit.socket.AF_INET, audit.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]
        with self.assertRaisesRegex(RuntimeError, "HTTPS"):
            audit.S3ObjectLockSink(
                "bucket", "us-east-1", credentials, "http://localhost:9000", 1,
                "local-development",
            )
        missing = Path(self.temp.name) / "missing-ca.pem"
        with mock.patch.object(audit.socket, "getaddrinfo", return_value=addresses):
            with self.assertRaisesRegex(RuntimeError, "missing, unreadable, or invalid"):
                audit.S3ObjectLockSink(
                    "bucket", "us-east-1", credentials, "https://localhost:9443", 1,
                    "local-development", ca_file=missing,
                )
            invalid = Path(self.temp.name) / "invalid-ca.pem"
            invalid.write_text("not a certificate\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "missing, unreadable, or invalid"):
                audit.S3ObjectLockSink(
                    "bucket", "us-east-1", credentials, "https://localhost:9443", 1,
                    "local-development", ca_file=invalid,
                )
            unreadable = Path(self.temp.name) / "unreadable-ca.pem"
            unreadable.write_text("placeholder\n", encoding="utf-8")
            with (
                mock.patch.object(Path, "open", side_effect=PermissionError("denied")),
                self.assertRaisesRegex(RuntimeError, "missing, unreadable, or invalid"),
            ):
                audit.S3ObjectLockSink(
                    "bucket", "us-east-1", credentials, "https://localhost:9443", 1,
                    "local-development", ca_file=unreadable,
                )

    def test_dns_drift_after_preflight_is_rejected_before_put(self) -> None:
        credentials = audit.S3Credentials("AKIATEST", "not-a-real-secret")
        first = [(audit.socket.AF_INET, audit.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        changed = [(audit.socket.AF_INET, audit.socket.SOCK_STREAM, 6, "", ("127.0.0.2", 443))]
        with mock.patch.object(audit.socket, "getaddrinfo", return_value=first):
            sink = audit.S3ObjectLockSink(
                "bucket", "us-east-1", credentials, "https://localhost:9443", 1,
                "local-development",
            )
        with (
            mock.patch.object(audit.socket, "getaddrinfo", return_value=changed),
            mock.patch.object(audit.urllib.request, "build_opener") as build_opener,
            self.assertRaisesRegex(RuntimeError, "DNS addresses changed"),
        ):
            sink.anchor("fleet-audits/run/1.json", b"{}", "a" * 64)
        build_opener.assert_not_called()

    def test_https_transport_connects_only_to_prevalidated_numeric_address(self) -> None:
        connection = mock.Mock()
        with (
            mock.patch.object(audit.socket, "getaddrinfo", side_effect=AssertionError("no DNS")),
            mock.patch.object(audit.socket, "socket", return_value=connection),
        ):
            result = audit._connect_pinned(("8.8.8.8",), 443, 5.0, None)
        self.assertIs(result, connection)
        connection.connect.assert_called_once_with(("8.8.8.8", 443))

    def test_s3_anchor_rejects_missing_version_retention_or_digest(self) -> None:
        addresses = [
            (audit.socket.AF_INET, audit.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ]
        with mock.patch.object(audit.socket, "getaddrinfo", return_value=addresses):
            sink = audit.S3ObjectLockSink(
                "bucket", "us-east-1",
                audit.S3Credentials("AKIATEST", "not-a-real-secret"),
                "https://storage.example", 30, "external-compliance",
            )

        class Response:
            def __init__(self, headers: dict[str, str]) -> None:
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

        valid_head = {
            "x-amz-version-id": "version-1",
            "x-amz-object-lock-mode": "COMPLIANCE",
            "x-amz-object-lock-retain-until-date": "2030-01-01T00:00:00Z",
            "x-amz-meta-event-sha256": "a" * 64,
        }
        cases = (
            ({}, valid_head, "lacks version id"),
            ({"x-amz-version-id": "version-1"}, {key: value for key, value in valid_head.items() if key != "x-amz-object-lock-retain-until-date"}, "did not retain"),
            ({"x-amz-version-id": "version-1"}, {key: value for key, value in valid_head.items() if key != "x-amz-meta-event-sha256"}, "did not retain"),
        )
        for put_headers, head_headers, message in cases:
            responses = iter((Response(put_headers), Response(head_headers)))
            with (
                self.subTest(missing=message, head=head_headers),
                mock.patch.object(audit.socket, "getaddrinfo", return_value=addresses),
                mock.patch.object(
                    audit.S3ObjectLockSink,
                    "_open",
                    side_effect=lambda *args, **kwargs: next(responses),
                ),
                self.assertRaisesRegex(RuntimeError, message),
            ):
                sink.anchor("fleet-audits/run/1.json", b"{}", "a" * 64)


if __name__ == "__main__":
    unittest.main()
