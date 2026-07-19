from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fleet-run.py"
if str(SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("fleet_run", SCRIPT)
assert SPEC and SPEC.loader
fleet_run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet_run)


class FleetRunTests(unittest.TestCase):
    def test_last_json_object_accepts_pretty_printed_json(self) -> None:
        value = fleet_run.last_json_object('{\n  "run_id": "run-1"\n}\n', source="test")

        self.assertEqual(value, {"run_id": "run-1"})

    def test_last_json_object_rejects_ambiguous_or_prefixed_output(self) -> None:
        invalid = (
            '{"run_id":"one","run_id":"two"}',
            '{"usage":1e999}',
            '\ufeff{}',
            r'{"value":"\ud800"}',
            'log line\n{"run_id":"run-1"}\n',
            '{}{}',
            '[]',
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(fleet_run.MissionError):
                fleet_run.last_json_object(value, source="test")

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.repo = self.tmp / "target"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "fleet@example.test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Fleet Test"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=self.repo, check=True)
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def test_dry_run_renders_exact_objective_without_cmux_effects(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "preview",
                "inspect and improve the retry loop",
                "--target-repo",
                str(self.repo),
                "--dry-run",
            ],
            cwd=ROOT,
            env={**os.environ, "FLEET_RUNS_DIR": str(self.runs)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Execution mode: `autonomous`", result.stdout)
        self.assertIn("inspect and improve the retry loop", result.stdout)
        self.assertFalse((self.runs / "fleet-preview.manifest").exists())

    def test_existing_autonomous_fleet_runs_lead_to_durable_result(self) -> None:
        manifest = self.runs / "fleet-demo.manifest"
        manifest.write_text(
            "feature=demo\n"
            "preset=dan\n"
            "mode=autonomous\n"
            f"target_repo={self.repo}\n"
            "workspace=workspace:9\n"
            "lead=surface:10\n"
            "lead.runner=interactive\n",
            encoding="utf-8",
        )
        result_file = self.tmp / "lead-result.md"
        result_file.write_text("STATUS: DONE\nDECISION: shipped\n", encoding="utf-8")

        def fake_run(command: list[str], *, timeout: int | None = None):
            del timeout
            name = Path(command[0]).name
            if name == "git" and "status" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            if name == "git":
                return subprocess.CompletedProcess(command, 0, ".git\n", "")
            if name == "fleet-send.sh":
                return subprocess.CompletedProcess(command, 0, '{"run_id":"run-123"}\n', "")
            if name == "fleet-wait.sh":
                payload = {
                    "instance": "lead",
                    "run_id": "run-123",
                    "status": "succeeded",
                    "result_file": str(result_file),
                }
                return subprocess.CompletedProcess(command, 0, json.dumps(payload) + "\n", "")
            raise AssertionError(command)

        output = io.StringIO()
        with mock.patch.dict(os.environ, {"FLEET_RUNS_DIR": str(self.runs)}), mock.patch.object(
            fleet_run, "run_command", side_effect=fake_run
        ), mock.patch.object(fleet_run, "cmux_signal") as signal, redirect_stdout(output):
            code = fleet_run.main(
                ["demo", "complete the change", "--target-repo", str(self.repo), "--json"]
            )
        self.assertEqual(code, 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value["status"], "succeeded")
        self.assertEqual(value["lead_run_id"], "run-123")
        self.assertIn("DECISION: shipped", value["result"])
        self.assertGreaterEqual(signal.call_count, 3)


if __name__ == "__main__":
    unittest.main()
