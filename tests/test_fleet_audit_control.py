from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "fleet_audit_control.py"
sys.path.insert(0, str(ROOT / "scripts"))
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

    @staticmethod
    def tree_snapshot(
        root: Path,
    ) -> dict[Path, tuple[str, int, int, bytes | str]]:
        snapshot: dict[Path, tuple[str, int, int, bytes | str]] = {}
        for path in root.rglob("*"):
            info = path.lstat()
            relative = path.relative_to(root)
            if stat.S_ISLNK(info.st_mode):
                kind = "symlink"
                payload: bytes | str = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
                payload = path.read_bytes()
            elif stat.S_ISDIR(info.st_mode):
                kind = "directory"
                payload = b""
            else:
                kind = "special"
                payload = b""
            snapshot[relative] = (
                kind,
                stat.S_IMODE(info.st_mode),
                info.st_nlink,
                payload,
            )
        return snapshot

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

    def test_peer_credentials_support_linux_so_peercred(self) -> None:
        connection = mock.Mock(spec=["getsockopt"])
        connection.getsockopt.return_value = struct.pack("3i", 321, 654, 987)
        with mock.patch.object(audit.socket, "SO_PEERCRED", 17, create=True):
            self.assertEqual(audit.peer_credentials(connection), (654, 987))
        connection.getsockopt.assert_called_once_with(audit.socket.SOL_SOCKET, 17, 12)

    def test_tampering_breaks_signature_verification(self) -> None:
        self.start_run()
        path = self.root / self.run_id / "a2a_ledger.jsonl"
        event = json.loads(path.read_text())
        event["model_version"] = "tampered"
        path.write_text(json.dumps(event, separators=(",", ":")) + "\n")
        with self.assertRaisesRegex(RuntimeError, "signature"):
            self.ledger.read_verified(self.run_id)

    def test_durable_ledger_jsonl_is_strict_canonical_and_read_only(self) -> None:
        self.start_run()
        path = self.root / self.run_id / "a2a_ledger.jsonl"
        valid = path.read_bytes()
        cases = {
            "duplicate-key": b'{"event_type":"duplicate",' + valid[1:],
            "nan": b'{"ambiguous":NaN,' + valid[1:],
            "infinity": b'{"ambiguous":Infinity,' + valid[1:],
            "overflow": b'{"ambiguous":1e999,' + valid[1:],
            "bom": b"\xef\xbb\xbf" + valid,
            "invalid-utf8": b'{"ambiguous":"\xff",' + valid[1:],
            "surrogate": b'{"ambiguous":"\\ud800",' + valid[1:],
            "trailing-data": valid.rstrip(b"\n") + b" trailing\n",
            "missing-final-lf": valid.rstrip(b"\n"),
            "crlf": valid.rstrip(b"\n") + b"\r\n",
            "blank-row": valid + b"\n",
            "non-object": b"[]\n",
            "non-canonical": b" " + valid,
        }
        for name, corrupt in cases.items():
            with self.subTest(name=name):
                path.write_bytes(corrupt)
                before = self.tree_snapshot(Path(self.temp.name))
                errors = []
                for _ in range(2):
                    with self.assertRaises(audit.AuditControlError) as raised:
                        self.ledger.read_verified(self.run_id)
                    errors.append(str(raised.exception))
                    self.assertEqual(
                        self.tree_snapshot(Path(self.temp.name)),
                        before,
                    )
                self.assertEqual(errors[0], errors[1])
                path.write_bytes(valid)

        self.assertEqual(self.ledger.read_verified(self.run_id)[0]["sequence"], 1)

    def test_pending_anchor_requires_strict_canonical_json_before_unlink(self) -> None:
        ledger_root = Path(self.temp.name) / "pending-ledger"
        anchor_root = Path(self.temp.name) / "pending-anchor"
        receipts = Path(self.temp.name) / "pending-receipts"
        ledger_root.mkdir(mode=0o700)
        anchor_root.mkdir(mode=0o700)
        ledger = audit.AuditLedger(
            ledger_root,
            self.key,
            audit.DirectoryTestSink(anchor_root),
            receipt_root=receipts,
        )
        event_id = str(uuid.uuid4())
        event = ledger.append(
            self.run_id,
            {"event_id": event_id, "event_type": "RunStarted"},
        )
        pending = {
            "schema_version": 1,
            "event_id": event["event_id"],
            "object_key": event["worm_object_key"],
            "event_sha256": event["event_sha256"],
            "payload_sha256": audit.hashlib.sha256(
                audit.canonical(event) + b"\n"
            ).hexdigest(),
        }
        pending_path = receipts / f".{event_id}.pending.json"
        valid = audit.canonical(pending) + b"\n"
        corrupt_cases = {
            "duplicate-key": b'{"schema_version":1,' + valid[1:],
            "non-canonical": b" " + valid,
            "partial": valid.rstrip(b"\n") + b" trailing",
        }
        for name, corrupt in corrupt_cases.items():
            with self.subTest(name=name):
                pending_path.write_bytes(corrupt)
                pending_path.chmod(0o600)
                before = self.tree_snapshot(Path(self.temp.name))
                with self.assertRaises(audit.AuditControlError):
                    ledger._complete_existing_pending(event)
                self.assertEqual(
                    self.tree_snapshot(Path(self.temp.name)),
                    before,
                )

        pending_path.write_bytes(valid)
        ledger._complete_existing_pending(event)
        self.assertFalse(pending_path.exists())

    def test_socket_request_framing_is_strict_and_invalid_input_has_no_effect(
        self,
    ) -> None:
        def exchange(raw: bytes) -> dict[str, object]:
            handler = object.__new__(audit.AuditRequestHandler)
            handler.request = mock.Mock(spec=["settimeout"])
            handler.rfile = io.BytesIO(raw)
            handler.wfile = io.BytesIO()
            handler.server = mock.Mock(audit_service=self.service)
            with mock.patch.object(
                audit,
                "peer_credentials",
                return_value=(os.geteuid(), os.getegid()),
            ):
                handler.handle()
            return audit._strict_json_frame(
                handler.wfile.getvalue(),
                where="test audit response",
            )

        valid = b'{"operation":"health"}\n'
        success = exchange(valid)
        self.assertIs(success["ok"], True)
        self.assertEqual(success["result"]["status"], "ok")
        cases = {
            "duplicate-key": b'{"operation":"health","operation":"verify"}\n',
            "nan": b'{"operation":"health","bad":NaN}\n',
            "infinity": b'{"operation":"health","bad":Infinity}\n',
            "overflow": b'{"operation":"health","bad":1e999}\n',
            "bom": b"\xef\xbb\xbf" + valid,
            "invalid-utf8": b'{"operation":"health","bad":"\xff"}\n',
            "surrogate": b'{"operation":"health","bad":"\\ud800"}\n',
            "trailing-data": b'{"operation":"health"} trailing\n',
            "missing-lf": valid.rstrip(b"\n"),
            "crlf": valid.rstrip(b"\n") + b"\r\n",
            "blank": b"\n",
            "two-records": valid + valid,
            "non-object": b"[]\n",
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                before = self.tree_snapshot(self.root)
                first = exchange(raw)
                second = exchange(raw)
                self.assertIs(first["ok"], False)
                self.assertEqual(first, second)
                self.assertEqual(self.tree_snapshot(self.root), before)

    def test_socket_response_and_outbound_request_are_strict_before_effects(
        self,
    ) -> None:
        with mock.patch.object(audit.socket, "socket") as constructor:
            with self.assertRaises(audit.AuditControlError):
                audit.send_request(
                    Path(self.temp.name) / "unused.sock",
                    {"operation": "health", "bad": float("nan")},
                )
        constructor.assert_not_called()

        valid = b'{"ok":true,"result":{"status":"ok"}}\n'
        responses = {
            "duplicate-key": b'{"ok":true,"ok":false,"result":{}}\n',
            "nan": b'{"ok":true,"result":{"bad":NaN}}\n',
            "infinity": b'{"ok":true,"result":{"bad":Infinity}}\n',
            "overflow": b'{"ok":true,"result":{"bad":1e999}}\n',
            "bom": b"\xef\xbb\xbf" + valid,
            "invalid-utf8": b'{"ok":true,"result":{"bad":"\xff"}}\n',
            "surrogate": b'{"ok":true,"result":{"bad":"\\ud800"}}\n',
            "trailing": valid.rstrip(b"\n") + b" trailing\n",
            "missing-lf": valid.rstrip(b"\n"),
            "crlf": valid.rstrip(b"\n") + b"\r\n",
            "blank": b"\n",
            "two-records": valid + valid,
            "non-object": b"[]\n",
        }

        class FakeSocket:
            def __init__(self, response: bytes) -> None:
                self.response = response
                self.sent = b""
                self.shutdown_mode = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def settimeout(self, _timeout):
                return None

            def connect(self, _path):
                return None

            def sendall(self, payload):
                self.sent = payload

            def shutdown(self, mode):
                self.shutdown_mode = mode

            def makefile(self, _mode):
                return io.BytesIO(self.response)

        for name, response in responses.items():
            with self.subTest(name=name):
                fake = FakeSocket(response)
                with mock.patch.object(audit.socket, "socket", return_value=fake):
                    with self.assertRaises(audit.AuditControlError):
                        audit.send_request(
                            Path(self.temp.name) / "fake.sock",
                            {"operation": "health"},
                        )
                self.assertEqual(fake.sent, b'{"operation":"health"}\n')
                self.assertEqual(fake.shutdown_mode, audit.socket.SHUT_WR)

    def test_stdin_json_is_strict_deterministic_and_never_connects_when_invalid(
        self,
    ) -> None:
        cases = {
            "duplicate-key": b'{"operation":"health","operation":"verify"}',
            "nan": b'{"operation":"health","bad":NaN}',
            "infinity": b'{"operation":"health","bad":Infinity}',
            "overflow": b'{"operation":"health","bad":1e999}',
            "bom": b'\xef\xbb\xbf{"operation":"health"}',
            "invalid-utf8": b'{"operation":"health","bad":"\xff"}',
            "surrogate": b'{"operation":"health","bad":"\\ud800"}',
            "trailing": b'{"operation":"health"} true',
            "blank": b"",
            "non-object": b"[]",
        }
        socket_path = Path(self.temp.name) / "must-not-be-created.sock"
        command = [
            sys.executable,
            str(MODULE_PATH),
            "request",
            "--socket",
            str(socket_path),
        ]
        for name, raw in cases.items():
            with self.subTest(name=name):
                before = self.tree_snapshot(Path(self.temp.name))
                first = subprocess.run(
                    command,
                    cwd=ROOT,
                    input=raw,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    check=False,
                )
                second = subprocess.run(
                    command,
                    cwd=ROOT,
                    input=raw,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    check=False,
                )
                self.assertEqual((first.returncode, second.returncode), (2, 2))
                self.assertEqual(first.stdout, b"")
                self.assertEqual(first.stderr, second.stderr)
                self.assertTrue(first.stderr.startswith(b"audit request failed: "))
                self.assertNotIn(b"Traceback", first.stderr)
                self.assertEqual(
                    self.tree_snapshot(Path(self.temp.name)),
                    before,
                )

    def test_canonical_hash_contract_rejects_non_json_values(self) -> None:
        value = {"unicode": "caf\u00e9", "a": 1}
        encoded = b'{"a":1,"unicode":"caf\xc3\xa9"}'
        self.assertEqual(audit.canonical(value), encoded)
        self.assertEqual(
            audit.digest(value),
            audit.hashlib.sha256(encoded).hexdigest(),
        )
        for invalid in (float("nan"), float("inf"), "\ud800", {1: "value"}):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises(audit.AuditControlError):
                    audit.canonical(invalid)

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
        self.assertEqual(put_headers["if-none-match"], "*")
        self.assertIn("AWS4-HMAC-SHA256", put_headers["authorization"])
        self.assertNotIn("not-a-real-secret", put_headers["authorization"])
        self.assertEqual(
            audit.urllib.parse.parse_qs(audit.urllib.parse.urlsplit(requests[1].full_url).query),
            {"versionId": ["version-1"]},
        )
        self.assertEqual(receipt["version_id"], "version-1")
        self.assertEqual(receipt["trust_scope"], "external-compliance")

    def test_s3_anchor_recovers_exact_conditional_object_without_new_version(self) -> None:
        addresses = [
            (audit.socket.AF_INET, audit.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ]
        with mock.patch.object(audit.socket, "getaddrinfo", return_value=addresses):
            sink = audit.S3ObjectLockSink(
                "bucket", "us-east-1", audit.S3Credentials("AKIATEST", "secret"),
                "https://storage.example", 1, "external-compliance",
            )
        payload = b'{"durable":true}\n'
        requests = []

        class Response:
            def __init__(self, headers=None, body=b""):
                self.headers = headers or {}
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, size=-1):
                return self.body[:size]

        def fake_open(_sink, request, *, timeout):
            self.assertEqual(timeout, 30)
            requests.append(request)
            if request.method == "PUT":
                raise audit.urllib.error.HTTPError(
                    request.full_url, 412, "Precondition Failed", {}, io.BytesIO()
                )
            if request.method == "HEAD":
                return Response({
                    "x-amz-version-id": "version-existing",
                    "x-amz-object-lock-mode": "COMPLIANCE",
                    "x-amz-object-lock-retain-until-date": "2099-01-01T00:00:00Z",
                    "x-amz-meta-event-sha256": "a" * 64,
                })
            return Response(body=payload)

        with (
            mock.patch.object(audit.socket, "getaddrinfo", return_value=addresses),
            mock.patch.object(audit.S3ObjectLockSink, "_open", fake_open),
        ):
            receipt = sink.anchor("fleet-audits/run/1.json", payload, "a" * 64)
        self.assertEqual([request.method for request in requests], ["PUT", "HEAD", "GET"])
        self.assertEqual(receipt["version_id"], "version-existing")

    def test_anchor_journal_recovers_crash_between_sink_and_ledger_append(self) -> None:
        receipts = Path(self.temp.name) / "receipts"
        object_store = Path(self.temp.name) / "objects"
        object_store.mkdir(mode=0o700)
        delegate = audit.DirectoryTestSink(object_store)

        class CrashOnceSink:
            backend_name = delegate.backend_name
            compliance_mode = delegate.compliance_mode
            trust_scope = delegate.trust_scope
            retention_mode = delegate.retention_mode

            def __init__(self):
                self.crashed = False

            def object_key(self, run_id, sequence, event_id):
                return delegate.object_key(run_id, sequence, event_id)

            def anchor(self, object_key, payload, event_sha256):
                receipt = delegate.anchor(object_key, payload, event_sha256)
                if not self.crashed:
                    self.crashed = True
                    raise RuntimeError("crash after durable anchor")
                return receipt

        ledger = audit.AuditLedger(
            self.root, self.key, CrashOnceSink(), receipt_root=receipts
        )
        event_id = str(uuid.uuid4())
        payload = {"event_id": event_id, "event_type": "RunStarted"}
        with self.assertRaisesRegex(RuntimeError, "crash after durable anchor"):
            ledger.append(self.run_id, payload)
        self.assertEqual(len(list(receipts.glob(".*.pending.json"))), 1)
        recovered = ledger.append(self.run_id, payload)
        self.assertEqual(recovered["event_id"], event_id)
        self.assertEqual(len(ledger.read_verified(self.run_id)), 1)
        self.assertEqual(list(receipts.glob(".*.pending.json")), [])

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
