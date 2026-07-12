from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "scripts" / "fleet_state.py"


class FleetStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.manifest = Path(self.tempdir.name) / "fleet-test.manifest"
        self.manifest.write_text(
            "feature=test\n"
            "lead=surface:1\nlead.phase=CONTROL\n"
            "build=surface:2\nbuild.phase=BUILD\n"
            "verify=surface:3\nverify.phase=VERIFY\n",
            encoding="utf-8",
        )

    def run_state(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(STATE), *args, str(self.manifest)] if args[0] == "init" else
            ["python3", str(STATE), args[0], str(self.manifest), *args[1:]],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_future_phase_is_closed_until_evidence_backed_advance(self) -> None:
        self.assertEqual(self.run_state("init").returncode, 0)
        closed = self.run_state("check", "build")
        self.assertEqual(closed.returncode, 3)
        advanced = self.run_state("advance", "BUILD", "--evidence", "scope-approved")
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        self.assertEqual(self.run_state("check", "build").returncode, 0)
        self.assertEqual(self.run_state("check", "verify").returncode, 3)
        self.assertEqual(
            self.run_state("advance", "VERIFY", "--evidence", "build-frozen").returncode, 0
        )
        self.assertEqual(self.run_state("check", "build").returncode, 3)
        self.assertEqual(self.run_state("check", "verify").returncode, 0)

    def test_skipping_configured_phase_is_rejected(self) -> None:
        self.assertEqual(self.run_state("init").returncode, 0)
        skipped = self.run_state("advance", "VERIFY", "--evidence", "bad")
        self.assertEqual(skipped.returncode, 2)
        state = json.loads(self.manifest.with_suffix(".state.json").read_text())
        self.assertEqual(state["active_phase"], "CONTROL")


if __name__ == "__main__":
    unittest.main()
