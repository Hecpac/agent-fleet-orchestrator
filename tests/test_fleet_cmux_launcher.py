from __future__ import annotations

from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_cmux_launcher  # noqa: E402


class FleetCmuxLauncherJSONTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs = self.tmp / "runs"
        self.runs.mkdir(mode=0o700)
        self.runs.chmod(0o700)

    def create(self, *, command_shell: str = "/usr/bin/true") -> dict[str, str]:
        return fleet_cmux_launcher.create_spec(
            self.runs,
            label="lead/codex",
            cwd=self.tmp,
            environment={"FLEET_EXECUTION_PROFILE": "native"},
            command_shell=command_shell,
        )

    def paths(self, descriptor: dict[str, str]) -> tuple[Path, Path]:
        launch_dir = self.runs / "cmux-launches" / descriptor["launch_id"]
        return launch_dir / "spec.json", launch_dir / "accepted.json"

    @staticmethod
    def invalid_json(valid: bytes) -> dict[str, bytes]:
        return {
            "duplicate": valid.replace(b"{", b'{"schema_version":1,', 1),
            "nan": b'{"value":NaN}\n',
            "infinity": b'{"value":Infinity}\n',
            "overflow": b'{"value":1e999}\n',
            "bom": b"\xef\xbb\xbf" + valid,
            "invalid-utf8": b'{"value":"\xff"}\n',
            "surrogate": b'{"value":"\\ud800"}\n',
            "trailing": valid.rstrip(b"\n") + b" trailing\n",
            "missing-lf": valid.rstrip(b"\n"),
            "noncanonical-whitespace": b" " + valid,
        }

    def test_invalid_spec_json_fails_before_receipt_or_execution(self) -> None:
        marker = self.tmp / "must-not-execute"
        descriptor = self.create(
            command_shell=shlex.join(["/usr/bin/touch", str(marker)])
        )
        spec_path, receipt_path = self.paths(descriptor)
        original = spec_path.read_bytes()
        for name, invalid in self.invalid_json(original).items():
            with self.subTest(name=name):
                spec_path.write_bytes(invalid)
                spec_path.chmod(0o600)
                with (
                    mock.patch.object(
                        fleet_cmux_launcher.os,
                        "execvpe",
                        side_effect=AssertionError("invalid spec reached execution"),
                    ),
                    self.assertRaises(fleet_cmux_launcher.LaunchError),
                ):
                    fleet_cmux_launcher.execute_spec(
                        self.runs,
                        descriptor["launch_id"],
                        expected_digest=descriptor["spec_sha256"],
                    )
                self.assertEqual(spec_path.read_bytes(), invalid)
                self.assertFalse(receipt_path.exists())
                self.assertFalse(marker.exists())

    def test_invalid_receipt_json_is_read_only(self) -> None:
        descriptor = self.create()
        fleet_cmux_launcher.publish_acceptance(
            self.runs,
            descriptor["launch_id"],
            expected_digest=descriptor["spec_sha256"],
        )
        _, receipt_path = self.paths(descriptor)
        original = receipt_path.read_bytes()
        for name, invalid in self.invalid_json(original).items():
            with self.subTest(name=name):
                receipt_path.write_bytes(invalid)
                receipt_path.chmod(0o600)
                with self.assertRaises(fleet_cmux_launcher.LaunchError):
                    fleet_cmux_launcher.verify_acceptance(
                        self.runs,
                        descriptor["launch_id"],
                        expected_digest=descriptor["spec_sha256"],
                    )
                self.assertEqual(receipt_path.read_bytes(), invalid)

    def test_canonical_bytes_digest_and_lf_remain_exact(self) -> None:
        descriptor = self.create()
        spec_path, _ = self.paths(descriptor)
        spec = fleet_cmux_launcher.read_spec(
            self.runs,
            descriptor["launch_id"],
            expected_digest=descriptor["spec_sha256"],
        )
        self.assertEqual(
            spec_path.read_bytes(), fleet_cmux_launcher._canonical_bytes(spec)
        )
        self.assertTrue(spec_path.read_bytes().endswith(b"\n"))
        self.assertEqual(descriptor["spec_sha256"], fleet_cmux_launcher._digest(spec))

    def test_non_utf8_environment_fails_before_launch_store_creation(self) -> None:
        with self.assertRaisesRegex(
            fleet_cmux_launcher.LaunchError, "cannot be canonicalized"
        ):
            fleet_cmux_launcher.create_spec(
                self.runs,
                label="lead/codex",
                cwd=self.tmp,
                environment={"INVALID": "\ud800"},
                command_shell="/usr/bin/true",
            )
        self.assertFalse((self.runs / "cmux-launches").exists())


if __name__ == "__main__":
    unittest.main()
