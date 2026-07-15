from __future__ import annotations

import importlib.util
import io
import json
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

    def test_cleanup_and_teardown_refuse_foreign_docker_objects(self) -> None:
        with (
            mock.patch.object(local_worm, "_docker_exists", return_value=True),
            mock.patch.object(local_worm, "_docker_owner", return_value="foreign"),
            mock.patch.object(local_worm.subprocess, "run") as run,
        ):
            local_worm._remove_owned_quietly("container", "same-name", "expected")
        run.assert_not_called()

        state_dir = self.root / "state"
        state_dir.mkdir(mode=0o700)
        container, volume = local_worm._names(state_dir)
        state = {
            "schema_version": 1,
            "marker": "agent-fleet-local-worm",
            "state_dir": str(state_dir),
            "container": container,
            "volume": volume,
            "image": local_worm.IMAGE,
            "backend_name": local_worm.BACKEND_NAME,
            "endpoint": "https://localhost:9443",
            "bucket": "fleet-worm-local",
            "region": "us-east-1",
            "access_key": "local",
            "secret_key": "local-secret",
            "ca_file": str(state_dir / "ca.pem"),
            "retention_days": 1,
            "docker_owner": "expected",
        }
        (state_dir / local_worm.STATE_FILE).write_text(
            json.dumps(state), encoding="utf-8"
        )
        (state_dir / local_worm.STATE_FILE).chmod(0o600)
        commands: list[list[str]] = []

        def fake_run(command, *, timeout=180):
            del timeout
            commands.append(command)
            return mock.Mock(stdout="")

        with (
            mock.patch.object(local_worm, "_run", side_effect=fake_run),
            mock.patch.object(local_worm, "_docker_exists", return_value=True),
            mock.patch.object(local_worm, "_docker_owner", return_value="foreign"),
            self.assertRaisesRegex(local_worm.LocalWormError, "ownership label"),
        ):
            local_worm.teardown(state_dir)
        self.assertFalse(any("rm" in command for command in commands))

    def test_state_loader_rejects_symlink_and_broad_permissions(self) -> None:
        state_dir = self.root / "private-state"
        state_dir.mkdir(mode=0o700)
        state_file = state_dir / local_worm.STATE_FILE
        state_file.write_text("{}", encoding="utf-8")
        state_file.chmod(0o644)
        with self.assertRaisesRegex(local_worm.LocalWormError, "private regular file"):
            local_worm._load_state(state_dir)
        state_file.unlink()
        outside = self.root / "outside-state"
        outside.write_text("{}", encoding="utf-8")
        state_file.symlink_to(outside)
        with self.assertRaisesRegex(local_worm.LocalWormError, "private regular file"):
            local_worm._load_state(state_dir)

    def test_failed_cleanup_retains_the_only_ownership_state(self) -> None:
        state_dir = self.root / "retained-state"
        with (
            mock.patch.object(local_worm, "_run", return_value=mock.Mock(stdout="")),
            mock.patch.object(local_worm, "_docker_exists", return_value=False),
            mock.patch.object(local_worm, "_generate_certificates"),
            mock.patch.object(local_worm, "_write") as write,
            mock.patch.object(local_worm, "_remove_owned_quietly", return_value=False),
            self.assertRaisesRegex(local_worm.LocalWormError, "state retained"),
        ):
            def persist_state(path, content, mode):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                path.chmod(mode)

            write.side_effect = persist_state
            # Fail after the ownership token has been persisted.
            local_worm._run.side_effect = [mock.Mock(stdout=""), RuntimeError("pull failed")]
            local_worm.setup(state_dir, 9443)
        self.assertTrue((state_dir / local_worm.STATE_FILE).exists())

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
