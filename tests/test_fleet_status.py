from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_json  # noqa: E402
import fleet_status  # noqa: E402


class FleetStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runs = self.root / "runs"
        self.hooks = self.root / "home" / ".cmuxterm"
        self.runs.mkdir()
        self.hooks.mkdir(parents=True)
        self.surface = str(uuid.uuid4()).upper()

    def write_file(self, path: Path, payload: bytes, mode: int = 0o600) -> None:
        path.write_bytes(payload)
        path.chmod(mode)

    def write_manifest(
        self,
        feature: str,
        surface: str | None = None,
        *,
        filename_feature: str | None = None,
        mode: int = 0o600,
    ) -> Path:
        actual_surface = surface or self.surface
        payload = (
            f"feature={feature}\n"
            f"workspace_uuid={uuid.uuid4()}\n"
            "maker=surface:1\n"
            f"maker.uuid={actual_surface}\n"
            "maker.phase=BUILD\n"
        ).encode()
        path = self.runs / f"fleet-{filename_feature or feature}.manifest"
        self.write_file(path, payload, mode)
        return path

    def write_hooks(
        self,
        agent: str,
        value: object,
        *,
        mode: int = 0o600,
    ) -> Path:
        path = self.hooks / f"{agent}-hook-sessions.json"
        self.write_file(path, fleet_json.canonical_bytes(value), mode)
        return path

    def test_live_schema_maps_one_strict_session_without_mixing_indexes(self) -> None:
        self.write_manifest("alpha")
        self.write_hooks(
            "codex",
            {
                "version": 1,
                "activeSessionsBySurface": {"misleading": "evil"},
                "activeSessionsByWorkspace": {},
                "sessions": {
                    "session-1": {
                        "surfaceId": self.surface.lower(),
                        "workspaceId": str(uuid.uuid4()),
                        "updatedAt": 990.0,
                        "agentLifecycle": "running",
                        "pid": 123,
                        "lastBody": "Waiting for your input\nplease choose",
                    }
                },
            },
        )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(
                os.environ, {"FLEET_RUNS_DIR": str(self.runs)}, clear=False
            ),
            mock.patch.object(
                fleet_status.os.path, "expanduser", return_value=str(self.hooks)
            ),
            mock.patch.object(fleet_status, "workspace_names", return_value={}),
            mock.patch.object(fleet_status.time, "time", return_value=1000.0),
            mock.patch.object(fleet_status, "pid_alive", return_value=True),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = fleet_status.main(["--show-hints"])

        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "")
        output = stdout.getvalue()
        self.assertIn("1 agente(s) BLOQUEADOS", output)
        self.assertIn("maker", output)
        self.assertIn("BUILD", output)
        self.assertIn("codex", output)
        self.assertIn("alpha", output)
        self.assertIn("Waiting for your input please choose", output)
        self.assertNotIn("misleading", output)

    def test_hook_reader_omits_ambiguous_or_unsafe_files_deterministically(
        self,
    ) -> None:
        valid_session = {
            "surfaceId": self.surface,
            "workspaceId": str(uuid.uuid4()),
            "updatedAt": 1.0,
        }
        self.write_hooks(
            "valid",
            {
                "version": 1,
                "sessions": {"only": valid_session, "scalar": "ignored"},
            },
        )
        invalid_payloads = {
            "duplicate": b'{"sessions":{},"sessions":{"evil":{}}}',
            "nan": b'{"sessions":{"evil":{"updatedAt":NaN}}}',
            "infinity": b'{"sessions":{"evil":{"updatedAt":1e999}}}',
            "bom": b'\xef\xbb\xbf{"sessions":{}}',
            "utf8": b'{"sessions":{"\xff":{}}}',
            "surrogate": b'{"sessions":{"evil":{"x":"\\ud800"}}}',
            "trailing": b'{"sessions":{}} {}',
            "scalar": b"[]",
            "wrong-sessions": b'{"sessions":[]}',
        }
        for name, payload in invalid_payloads.items():
            self.write_file(self.hooks / f"{name}-hook-sessions.json", payload)
        self.write_hooks("unsafe-mode", {"sessions": {}}, mode=0o666)

        outside = self.root / "outside-hook.json"
        self.write_file(outside, fleet_json.canonical_bytes({"sessions": {}}))
        (self.hooks / "symlink-hook-sessions.json").symlink_to(outside)
        os.link(outside, self.hooks / "hardlink-hook-sessions.json")

        with mock.patch("builtins.open", side_effect=AssertionError("loose open")):
            first = fleet_status.hook_sessions(self.hooks)
            second = fleet_status.hook_sessions(self.hooks)

        self.assertEqual(first, second)
        self.assertEqual(first, [("valid", valid_session)])

    def test_manifest_inventory_rejects_duplicates_links_and_collisions(self) -> None:
        self.write_manifest("alpha")
        duplicate = self.runs / "fleet-duplicate.manifest"
        self.write_file(
            duplicate,
            (
                "feature=duplicate\nfeature=evil\n"
                "maker=surface:2\n"
                f"maker.uuid={uuid.uuid4()}\n"
                "maker.phase=VERIFY\n"
            ).encode(),
        )
        self.write_manifest("wrong", filename_feature="mismatch")
        self.write_manifest("unsafe-mode", mode=0o644)

        colliding_surface = str(uuid.uuid4())
        self.write_manifest("collision-a", colliding_surface)
        self.write_manifest("collision-b", colliding_surface)

        outside = self.root / "outside.manifest"
        self.write_file(
            outside,
            (
                "feature=symlink\nmaker=surface:3\n"
                f"maker.uuid={uuid.uuid4()}\nmaker.phase=BUILD\n"
            ).encode(),
        )
        (self.runs / "fleet-symlink.manifest").symlink_to(outside)
        os.link(outside, self.runs / "fleet-hardlink.manifest")

        with mock.patch("builtins.open", side_effect=AssertionError("loose open")):
            first = fleet_status.manifest_inventory(self.runs)
            second = fleet_status.manifest_inventory(self.runs)

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            {
                self.surface: {
                    "instance": "maker",
                    "phase": "BUILD",
                    "feature": "alpha",
                    "workspace_uuid": mock.ANY,
                }
            },
        )
        self.assertNotIn(colliding_surface.upper(), first)

    def test_manifest_fields_are_not_synthesized_across_instances(self) -> None:
        payload = (
            "feature=split\n"
            "maker=surface:1\n"
            f"other.uuid={self.surface}\n"
            "other.phase=BUILD\n"
        ).encode()
        self.write_file(self.runs / "fleet-split.manifest", payload)

        self.assertEqual(fleet_status.manifest_inventory(self.runs), {})

    def test_invalid_max_age_values_exit_cleanly_and_deterministically(self) -> None:
        outputs: dict[str, str] = {}
        for raw in ("nan", "inf", "-inf", "0", "-1", "1e999", "invalid"):
            with self.subTest(raw=raw):
                stderr = io.StringIO()
                stdout = io.StringIO()
                with (
                    redirect_stderr(stderr),
                    redirect_stdout(stdout),
                    self.assertRaises(SystemExit) as raised,
                ):
                    fleet_status.main([f"--max-age-hours={raw}"])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn("Traceback", stderr.getvalue())
                self.assertIn("finite positive number", stderr.getvalue())
                outputs[raw] = stderr.getvalue()

        repeat = io.StringIO()
        with redirect_stderr(repeat), self.assertRaises(SystemExit):
            fleet_status.main(["--max-age-hours=nan"])
        self.assertEqual(repeat.getvalue(), outputs["nan"])

    def test_malformed_session_fields_are_omitted_without_traceback(self) -> None:
        self.write_hooks(
            "codex",
            {
                "sessions": {
                    "bad-surface": {
                        "surfaceId": "not-a-uuid",
                        "updatedAt": 1,
                    },
                    "bad-time": {
                        "surfaceId": self.surface,
                        "updatedAt": "infinity",
                    },
                    "bad-types": {
                        "surfaceId": self.surface,
                        "updatedAt": True,
                    },
                }
            },
        )
        stdout = io.StringIO()
        with (
            mock.patch.object(
                fleet_status.os.path, "expanduser", return_value=str(self.hooks)
            ),
            mock.patch.object(fleet_status, "workspace_names", return_value={}),
            mock.patch.object(fleet_status, "manifest_inventory", return_value={}),
            redirect_stdout(stdout),
        ):
            self.assertEqual(fleet_status.main([]), 0)

        self.assertEqual(
            stdout.getvalue(),
            "Sin sesiones de agente registradas en la ventana de tiempo.\n",
        )

    @unittest.skipUnless(Path("/dev/fd").exists(), "descriptor view unavailable")
    def test_repeated_reads_do_not_leak_descriptors(self) -> None:
        self.write_manifest("alpha")
        self.write_hooks("codex", {"sessions": {}})

        baseline = len(os.listdir("/dev/fd"))
        for _ in range(40):
            fleet_status.manifest_inventory(self.runs)
            fleet_status.hook_sessions(self.hooks)
        self.assertEqual(len(os.listdir("/dev/fd")), baseline)

    def test_pid_validation_rejects_non_positive_and_boolean_values(self) -> None:
        for value in (None, False, True, 0, -1, "invalid"):
            with self.subTest(value=value):
                self.assertFalse(fleet_status.pid_alive(value))


if __name__ == "__main__":
    unittest.main()
