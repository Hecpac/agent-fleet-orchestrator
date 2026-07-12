from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
FLEET_WAIT = ROOT / "scripts" / "fleet_wait.py"

# `events` blocks so the deadline fires; every invocation is logged.
FAKE_CMUX_BLOCKING = r'''#!/usr/bin/env python3
import json
import os
import sys
import time

with open(os.environ["CMUX_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:2] == ["events"]:
    time.sleep(10)
'''

UUID = "00000000-0000-0000-0000-000000000101"


class FleetWaitEscalationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "cmux-log.jsonl"
        fake = self.bin / "cmux"
        fake.write_text(textwrap.dedent(FAKE_CMUX_BLOCKING), encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.manifest = self.tmp / "fleet-esc.manifest"
        self.manifest.write_text(
            "feature=esc\n"
            "workspace=workspace:1\n"
            "triage=surface:1\n"
            f"triage.uuid={UUID}\n"
            "triage.runner=local\n",
            encoding="utf-8",
        )
        self.env = os.environ.copy()
        self.env.update({
            "PATH": f"{self.bin}:{self.env['PATH']}",
            "CMUX_LOG": str(self.log),
            "TREE_BOTH": f"surface:1 {UUID}",
        })

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_timeout_escalates_via_notify(self) -> None:
        result = subprocess.run(
            ["python3", str(FLEET_WAIT), "esc", str(self.manifest), "1", "triage"],
            text=True, capture_output=True, timeout=30, check=False, env=self.env,
        )
        self.assertEqual(result.returncode, 124, result.stderr)
        notifies = [call for call in self.calls() if call and call[0] == "notify"]
        self.assertTrue(notifies, "timeout did not fire a cmux notify escalation")
        payload = " ".join(notifies[0])
        self.assertIn("ESCALATION", payload)
        self.assertIn("triage", payload)


if __name__ == "__main__":
    unittest.main()
