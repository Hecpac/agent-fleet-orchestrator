from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "fleet_worm_local.py"
SPEC = importlib.util.spec_from_file_location("fleet_worm_local", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
local_worm = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = local_worm
SPEC.loader.exec_module(local_worm)


class FleetWormLocalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fleet-worm-local-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_image_is_pinned_and_public_state_excludes_credentials(self) -> None:
        self.assertEqual(
            local_worm.IMAGE,
            "rustfs/rustfs:1.0.0-beta.3@sha256:378642b05b7dcb4849fb77ebe6aca4ced1c3f66e7e504247df95a5c9018d3358",
        )
        state = {
            "backend_name": "rustfs:1.0.0-beta.3",
            "bucket": "bucket",
            "ca_file": "/tmp/ca.pem",
            "container": "container",
            "endpoint": "https://localhost:9443",
            "image": local_worm.IMAGE,
            "state_dir": "/tmp/state",
            "volume": "volume",
            "access_key": "must-not-escape",
            "secret_key": "must-not-escape",
        }
        public = local_worm._public_state(state)
        self.assertNotIn("access_key", public)
        self.assertNotIn("secret_key", public)
        self.assertNotIn("must-not-escape", str(public))

    def test_docker_identity_probe_distinguishes_absent_from_unknown_failure(self) -> None:
        absent = mock.Mock(returncode=1, stderr="Error: No such container: owned")
        present = mock.Mock(returncode=0, stderr="")
        unknown = mock.Mock(returncode=1, stderr="permission denied")
        with mock.patch.object(local_worm.subprocess, "run", return_value=absent):
            self.assertFalse(local_worm._docker_exists("container", "owned"))
        with mock.patch.object(local_worm.subprocess, "run", return_value=present):
            self.assertTrue(local_worm._docker_exists("volume", "owned"))
        with (
            mock.patch.object(local_worm.subprocess, "run", return_value=unknown),
            self.assertRaisesRegex(local_worm.LocalWormError, "permission denied"),
        ):
            local_worm._docker_exists("container", "owned")

    def test_generated_custom_ca_is_valid_for_sink_preflight(self) -> None:
        certs = self.root / "certs"
        local_worm._generate_certificates(certs)
        addresses = [
            (
                local_worm.audit.socket.AF_INET,
                local_worm.audit.socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1", 9443),
            )
        ]
        with mock.patch.object(
            local_worm.audit.socket, "getaddrinfo", return_value=addresses
        ):
            sink = local_worm.audit.S3ObjectLockSink(
                "bucket",
                "us-east-1",
                local_worm.audit.S3Credentials("access", "secret"),
                "https://localhost:9443",
                1,
                "local-development",
                ca_file=certs / "ca.pem",
            )
        self.assertEqual(sink.resolved_addresses, ("127.0.0.1",))
        self.assertTrue((certs / "rustfs_cert.pem").is_file())
        self.assertTrue((certs / "rustfs_key.pem").is_file())

    def test_delete_proof_requires_retention_error_and_exact_version_head(self) -> None:
        body = (
            b"<Error><Code>InvalidRequest</Code>"
            b"<Message>Object is WORM protected and cannot be deleted</Message></Error>"
        )
        error = urllib.error.HTTPError(
            "https://localhost/object", 403, "Forbidden", {}, io.BytesIO(body)
        )
        head = (
            200,
            {
                "x-amz-version-id": "version-1",
                "x-amz-object-lock-mode": "COMPLIANCE",
                "x-amz-object-lock-retain-until-date": "2030-01-01T00:00:00Z",
                "x-amz-meta-event-sha256": "a" * 64,
            },
            b"",
        )
        with mock.patch.object(local_worm, "_s3_call", side_effect=(error, head)):
            result = local_worm._prove_delete(
                mock.Mock(), "object.json", "version-1", "a" * 64
            )
        self.assertEqual(result["status"], 403)
        self.assertTrue(result["version_present"])

    def test_delete_proof_does_not_misclassify_permission_failure(self) -> None:
        body = b"<Error><Code>AccessDenied</Code><Message>Bad credentials</Message></Error>"
        error = urllib.error.HTTPError(
            "https://localhost/object", 403, "Forbidden", {}, io.BytesIO(body)
        )
        with (
            mock.patch.object(local_worm, "_s3_call", side_effect=error),
            self.assertRaisesRegex(local_worm.LocalWormError, "not attributable"),
        ):
            local_worm._prove_delete(mock.Mock(), "object.json", "version-1", "a" * 64)


if __name__ == "__main__":
    unittest.main()
