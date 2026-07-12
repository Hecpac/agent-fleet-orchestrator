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

FAKE_CMUX = r'''#!/usr/bin/env python3
import json
import os
import sys
import time

with open(os.environ["CMUX_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")

if sys.argv[1:2] != ["events"]:
    raise SystemExit(0)

print(json.dumps({
    "type": "ack",
    "resume": {"boot_id": "boot-1", "gap": False, "next_seq": 1},
}), flush=True)

scenario = os.environ.get("FAKE_SCENARIO", "blocking")
ledger_path = os.environ.get("FAKE_LEDGER", "")

def append(instance, run_id, status, exit_code):
    with open(ledger_path, "a", encoding="utf-8") as ledger:
        ledger.write(json.dumps({
            "instance": instance,
            "run_id": run_id,
            "status": status,
            "exit_code": exit_code,
            "result_file": f"/tmp/{run_id}.txt",
            "timestamp": os.environ.get(f"FAKE_TIMESTAMP_{run_id}", "2026-07-12T00:00:00+00:00"),
        }) + "\n")

if scenario == "spurious_then_done":
    print(json.dumps({"type": "event", "name": "notification.requested"}), flush=True)
    time.sleep(0.1)
    append("triage", "r1", "succeeded", 0)
    print(json.dumps({"type": "heartbeat"}), flush=True)
elif scenario == "finish_r2":
    append("triage", "r2", "succeeded", 0)
    print(json.dumps({"type": "heartbeat"}), flush=True)
elif scenario == "any_first_success":
    append("triage", "r1", "failed", 1)
    print(json.dumps({"type": "heartbeat"}), flush=True)
    append("review", "r2", "succeeded", 0)
    print(json.dumps({"type": "heartbeat"}), flush=True)

time.sleep(10)
'''

UUID_1 = "00000000-0000-0000-0000-000000000101"
UUID_2 = "00000000-0000-0000-0000-000000000102"


class FleetWaitTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "cmux-log.jsonl"
        fake = self.bin / "cmux"
        fake.write_text(textwrap.dedent(FAKE_CMUX), encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.manifest = self.tmp / "fleet-esc.manifest"
        self.manifest.write_text(
            "feature=esc\n"
            "workspace=workspace:1\n"
            "triage=surface:1\n"
            f"triage.uuid={UUID_1}\n"
            "triage.runner=local\n",
            encoding="utf-8",
        )
        self.ledger = self.tmp / "fleet-esc.ledger.jsonl"
        self.env = os.environ.copy()
        self.env.update({
            "PATH": f"{self.bin}:{self.env['PATH']}",
            "CMUX_LOG": str(self.log),
            "FAKE_LEDGER": str(self.ledger),
            "TREE_BOTH": f"surface:1 {UUID_1}",
        })

    def add_review_instance(self) -> None:
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                "review=surface:2\n"
                f"review.uuid={UUID_2}\n"
                "review.runner=local\n"
            )
        self.env["TREE_BOTH"] += f"\nsurface:2 {UUID_2}"

    def append_ledger(
        self,
        instance: str,
        run_id: str,
        status: str,
        timestamp: str = "2026-07-12T00:00:00+00:00",
    ) -> None:
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "instance": instance,
                "run_id": run_id,
                "status": status,
                "timestamp": timestamp,
            }) + "\n")

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def run_wait(
        self,
        timeout_sec: str,
        *,
        roles: tuple[str, ...] = ("triage",),
        runs: tuple[str, ...] = ("triage=r1",),
        extra: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "python3", str(FLEET_WAIT), "esc", str(self.manifest), timeout_sec,
            *(f"--run={mapping}" for mapping in runs),
            *extra,
            *roles,
        ]
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=self.env,
        )


class FleetWaitEscalationTests(FleetWaitTestCase):
    def test_timeout_escalates_via_notify_and_uses_ack_stream(self) -> None:
        result = self.run_wait("1")
        self.assertEqual(result.returncode, 124, result.stderr)
        calls = self.calls()
        event_calls = [call for call in calls if call and call[0] == "events"]
        self.assertEqual(len(event_calls), 1)
        self.assertIn("--reconnect", event_calls[0])
        self.assertNotIn("--no-ack", event_calls[0])
        notifies = [call for call in calls if call and call[0] == "notify"]
        self.assertTrue(notifies, "timeout did not fire a cmux notify escalation")
        self.assertIn("ESCALATION", " ".join(notifies[0]))


class FleetWaitLedgerAuthorityTests(FleetWaitTestCase):
    def test_spurious_notification_is_ignored_until_exact_run_is_terminal(self) -> None:
        self.append_ledger("triage", "r1", "running")
        self.env["FAKE_SCENARIO"] = "spurious_then_done"
        result = self.run_wait("10")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("instance=triage run_id=r1 status=succeeded", result.stdout)

    def test_terminal_before_subscription_is_reconciled_after_ack(self) -> None:
        self.append_ledger("triage", "r1", "succeeded")
        ready = self.tmp / "wait.ready"
        self.env["FLEET_WAIT_READY_FILE"] = str(ready)
        result = self.run_wait("10")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("run_id=r1 status=succeeded", result.stdout)
        self.assertEqual(json.loads(ready.read_text())["status"], "ready")

    def test_wait_does_not_accept_an_older_terminal_run(self) -> None:
        self.append_ledger("triage", "r1", "succeeded")
        self.append_ledger("triage", "r2", "running")
        self.env["FAKE_SCENARIO"] = "finish_r2"
        result = self.run_wait("10", runs=("triage=r2",))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("run_id=r2 status=succeeded", result.stdout)
        self.assertNotIn("run_id=r1", result.stdout)

    def test_any_continues_after_failure_until_first_success_and_emits_json(self) -> None:
        self.add_review_instance()
        self.append_ledger("triage", "r1", "running")
        self.append_ledger("review", "r2", "running")
        self.env["FAKE_SCENARIO"] = "any_first_success"
        self.env["FAKE_TIMESTAMP_r1"] = "2026-07-12T00:00:01+00:00"
        self.env["FAKE_TIMESTAMP_r2"] = "2026-07-12T00:00:02+00:00"
        result = self.run_wait(
            "10",
            roles=("triage", "review"),
            runs=("triage=r1", "review=r2"),
            extra=("--any", "--json"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row["status"] for row in rows], ["failed", "succeeded"])
        self.assertEqual(rows[-1]["instance"], "review")

    def test_any_uses_ledger_time_when_successes_precede_subscription(self) -> None:
        self.add_review_instance()
        self.append_ledger(
            "triage", "r1", "succeeded", "2026-07-12T00:00:02+00:00"
        )
        self.append_ledger(
            "review", "r2", "succeeded", "2026-07-12T00:00:01+00:00"
        )
        result = self.run_wait(
            "10",
            roles=("triage", "review"),
            runs=("triage=r1", "review=r2"),
            extra=("--any", "--json"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row["instance"] for row in rows], ["review"])

    def test_local_wait_requires_run_id(self) -> None:
        result = self.run_wait("10", runs=())
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --run", result.stderr)


if __name__ == "__main__":
    unittest.main()
