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

# `events` emits one notification while the run is still `running` in the
# ledger (spurious), appends the terminal ledger event, then re-emits.
FAKE_CMUX_SPURIOUS_THEN_DONE = r'''#!/usr/bin/env python3
import json
import os
import sys
import time

with open(os.environ["CMUX_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:2] == ["events"]:
    event = {
        "name": "notification.requested",
        "payload": {"params": {
            "preferred_surface_id": os.environ["FAKE_SURFACE_UUID"],
            "title_length": int(os.environ["FAKE_TITLE_LEN"]),
        }},
    }
    print(json.dumps(event), flush=True)
    time.sleep(0.3)
    with open(os.environ["FAKE_LEDGER"], "a", encoding="utf-8") as ledger:
        ledger.write(json.dumps(
            {"instance": "triage", "status": "succeeded", "run_id": "r1"}
        ) + "\n")
    print(json.dumps(event), flush=True)
    time.sleep(10)
'''

UUID = "00000000-0000-0000-0000-000000000101"


class FleetWaitTestCase(unittest.TestCase):
    fake_cmux = FAKE_CMUX_BLOCKING

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "cmux-log.jsonl"
        fake = self.bin / "cmux"
        fake.write_text(textwrap.dedent(self.fake_cmux), encoding="utf-8")
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
        self.ledger = self.tmp / "fleet-esc.ledger.jsonl"
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

    def run_wait(self, timeout_sec: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(FLEET_WAIT), "esc", str(self.manifest), timeout_sec, "triage"],
            text=True, capture_output=True, timeout=30, check=False, env=self.env,
        )


class FleetWaitEscalationTests(FleetWaitTestCase):
    fake_cmux = FAKE_CMUX_BLOCKING

    def test_timeout_escalates_via_notify(self) -> None:
        result = self.run_wait("1")
        self.assertEqual(result.returncode, 124, result.stderr)
        notifies = [call for call in self.calls() if call and call[0] == "notify"]
        self.assertTrue(notifies, "timeout did not fire a cmux notify escalation")
        payload = " ".join(notifies[0])
        self.assertIn("ESCALATION", payload)
        self.assertIn("triage", payload)


class FleetWaitLedgerAuthorityTests(FleetWaitTestCase):
    fake_cmux = FAKE_CMUX_SPURIOUS_THEN_DONE

    def test_spurious_notification_is_ignored_until_ledger_is_terminal(self) -> None:
        self.ledger.write_text(
            json.dumps({"instance": "triage", "status": "running", "run_id": "r1"}) + "\n",
            encoding="utf-8",
        )
        self.env.update({
            "FAKE_SURFACE_UUID": UUID,
            "FAKE_TITLE_LEN": str(len("fleet-esc:triage")),
            "FAKE_LEDGER": str(self.ledger),
        })
        result = self.run_wait("10")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("triage=done", result.stdout)


if __name__ == "__main__":
    unittest.main()
