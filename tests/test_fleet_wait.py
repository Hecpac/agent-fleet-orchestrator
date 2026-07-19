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

if sys.argv[1:2] == ["read-screen"]:
    print(os.environ.get("FAKE_SCREEN", ""))
    raise SystemExit(0)

if sys.argv[1:2] != ["events"]:
    raise SystemExit(0)

print(json.dumps({
    "type": "ack",
    "protocol": os.environ.get("FAKE_ACK_PROTOCOL", "cmux-events"),
    "version": 1,
    "resume": {
        "boot_id": os.environ.get("FAKE_BOOT_ID", "boot-1"),
        "gap": os.environ.get("FAKE_GAP", "0") == "1",
        "oldest_seq": int(os.environ.get("FAKE_OLDEST_SEQ", "1")),
        "latest_seq": int(os.environ.get("FAKE_LATEST_SEQ", "1")),
        "next_seq": int(os.environ.get("FAKE_LATEST_SEQ", "1")) + 1,
    },
    "boot_id": os.environ.get("FAKE_BOOT_ID", "boot-1"),
    "replay_count": int(os.environ.get("FAKE_REPLAY_COUNT", "0")),
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
elif scenario == "drain_replay":
    print(json.dumps({
        "type": "event",
        "boot_id": "boot-1",
        "seq": 1,
        "name": "notification.requested",
        "occurred_at": "2026-07-12T00:00:03+00:00",
        "payload": {},
    }), flush=True)
elif scenario == "frontier_finishes_during_replay":
    for seq, name, phase, when in (
        (1, "agent.hook.UserPromptSubmit", "received", "2026-07-12T00:00:01Z"),
        (2, "agent.hook.Stop", "completed", "2026-07-12T00:00:02Z"),
    ):
        print(json.dumps({
            "type": "event",
            "boot_id": "boot-1",
            "seq": seq,
            "id": f"boot-1-{seq}",
            "name": name,
            "source": "codex",
            "occurred_at": when,
            "workspace_id": "",
            "payload": {
                "phase": phase,
                "session_id": "codex-s1",
                "_source": "codex",
            },
        }), flush=True)

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

    def add_frontier_instance(self) -> None:
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                "frontier=surface:2\n"
                f"frontier.uuid={UUID_2}\n"
                "frontier.runner=interactive\n"
            )
        self.env["TREE_BOTH"] += f"\nsurface:2 {UUID_2}"

    def configure_frontier_hooks(self) -> Path:
        hooks = self.tmp / "hooks"
        hooks.mkdir(exist_ok=True)
        transcript = self.tmp / "codex-s1.jsonl"
        transcript.write_text("".join(json.dumps(row) + "\n" for row in (
            {
                "type": "session_meta",
                "timestamp": "2026-07-12T00:00:00Z",
                "payload": {"id": "s1", "model_provider": "openai"},
            },
            {
                "type": "turn_context",
                "timestamp": "2026-07-12T00:00:01Z",
                "payload": {"model": "gpt-5.6-sol"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-07-12T00:00:01Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": "task FLEET_RESULT:r-frontier:<STATUS>",
                    }],
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-07-12T00:00:01.500Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": "answer\nFLEET_RESULT:r-frontier:DONE",
                    }],
                },
            },
        )), encoding="utf-8")
        (hooks / "codex-hook-sessions.json").write_text(json.dumps({
            "sessions": {
                "s1": {
                    "sessionId": "s1",
                    "workspaceId": "",
                    "surfaceId": UUID_2,
                    "transcriptPath": str(transcript),
                    "updatedAt": 10,
                }
            }
        }))
        self.env["CMUX_HOOK_DIR"] = str(hooks)
        return hooks

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
        self.ledger.chmod(0o600)

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
        self.assertIn("agent.hook.SessionEnd", event_calls[0])
        self.assertNotIn("--no-ack", event_calls[0])
        notifies = [call for call in calls if call and call[0] == "notify"]
        self.assertTrue(notifies, "timeout did not fire a cmux notify escalation")
        self.assertIn("ESCALATION", " ".join(notifies[0]))

    def test_malformed_protocol_ack_fails_closed(self) -> None:
        self.env["FAKE_ACK_PROTOCOL"] = "other-protocol"
        result = self.run_wait("10")
        self.assertEqual(result.returncode, 5)
        self.assertIn("invalid cmux-events ACK", result.stderr)


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

    def test_mixed_any_drains_replay_then_uses_durable_completion_time(self) -> None:
        self.add_frontier_instance()
        self.append_ledger(
            "triage", "r-local", "succeeded", "2026-07-12T00:00:01+00:00"
        )
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "timestamp": "2026-07-12T00:00:00+00:00",
                "dispatched_at": "2026-07-12T00:00:00+00:00",
                "run_id": "r-frontier",
                "feature": "esc",
                "instance": "frontier",
                "role": "codex",
                "phase": "CHALLENGE",
                "runner": "interactive",
                "status": "dispatched",
                "task_sha256": "a" * 64,
                "workspace_uuid": "",
                "surface_uuid": UUID_2,
                "event_boot_id": "boot-1",
                "after_seq": 0,
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "hook_source": "codex",
            }) + "\n")
            handle.write(json.dumps({
                "timestamp": "2026-07-12T00:00:01Z",
                "completed_at": "2026-07-12T00:00:01Z",
                "run_id": "r-frontier",
                "feature": "esc",
                "instance": "frontier",
                "status": "succeeded",
                "exit_code": 0,
            }) + "\n")
        self.env["FAKE_SCENARIO"] = "drain_replay"
        self.env["FAKE_REPLAY_COUNT"] = "1"
        result = self.run_wait(
            "10",
            roles=("triage", "frontier"),
            runs=("triage=r-local", "frontier=r-frontier"),
            extra=("--any", "--json"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row["instance"] for row in rows], ["frontier"])

    def test_mixed_any_frontier_terminal_during_replay_competes_by_durable_time(self) -> None:
        self.add_frontier_instance()
        self.configure_frontier_hooks()
        self.append_ledger(
            "triage", "r-local", "succeeded", "2026-07-12T00:00:03+00:00"
        )
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "timestamp": "2026-07-12T00:00:00+00:00",
                "dispatched_at": "2026-07-12T00:00:00+00:00",
                "run_id": "r-frontier",
                "feature": "esc",
                "instance": "frontier",
                "role": "codex",
                "phase": "CHALLENGE",
                "runner": "interactive",
                "status": "dispatched",
                "task_sha256": "a" * 64,
                "workspace_uuid": "",
                "surface_uuid": UUID_2,
                "event_boot_id": "boot-1",
                "after_seq": 0,
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "hook_source": "codex",
            }) + "\n")
        self.env.update({
            "FAKE_SCENARIO": "frontier_finishes_during_replay",
            "FAKE_REPLAY_COUNT": "2",
            "FAKE_SCREEN": "answer\nFLEET_RESULT:r-frontier:DONE\n",
        })
        result = self.run_wait(
            "10",
            roles=("triage", "frontier"),
            runs=("triage=r-local", "frontier=r-frontier"),
            extra=("--any", "--json"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row["instance"] for row in rows], ["frontier"])

    def test_waiter_recovers_frontier_from_audit_on_boot_change(self) -> None:
        self.add_frontier_instance()
        self.configure_frontier_hooks()
        self.append_ledger(
            "triage", "r-local", "succeeded", "2026-07-12T00:00:03+00:00"
        )
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "timestamp": "2026-07-12T00:00:00+00:00",
                "dispatched_at": "2026-07-12T00:00:00+00:00",
                "run_id": "r-frontier",
                "feature": "esc",
                "instance": "frontier",
                "role": "codex",
                "phase": "CHALLENGE",
                "runner": "interactive",
                "status": "dispatched",
                "task_sha256": "a" * 64,
                "workspace_uuid": "",
                "surface_uuid": UUID_2,
                "event_boot_id": "boot-old",
                "after_seq": 900,
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "hook_source": "codex",
            }) + "\n")
        audit = self.tmp / "events.jsonl"
        audit.write_text("".join(json.dumps(event) + "\n" for event in (
            {
                "type": "event", "boot_id": "boot-old", "seq": 900,
                "id": "boot-old-900", "name": "surface.action",
                "occurred_at": "2026-07-12T00:00:00Z", "workspace_id": "",
                "payload": {},
            },
            {
                "type": "event", "boot_id": "boot-new", "seq": 1,
                "id": "boot-new-1", "name": "agent.hook.UserPromptSubmit",
                "source": "codex",
                "occurred_at": "2026-07-12T00:00:01Z", "workspace_id": "",
                "payload": {
                    "phase": "received", "session_id": "codex-s1",
                    "_source": "codex",
                },
            },
            {
                "type": "event", "boot_id": "boot-new", "seq": 2,
                "id": "boot-new-2", "name": "agent.hook.Stop",
                "source": "codex",
                "occurred_at": "2026-07-12T00:00:02Z", "workspace_id": "",
                "payload": {
                    "phase": "completed", "session_id": "codex-s1",
                    "_source": "codex",
                },
            },
        )))
        self.env.update({
            "CMUX_EVENTS_LOG": str(audit),
            "FAKE_BOOT_ID": "boot-new",
            "FAKE_SCREEN": "answer\nFLEET_RESULT:r-frontier:DONE\n",
        })
        result = self.run_wait(
            "10",
            roles=("triage", "frontier"),
            runs=("triage=r-local", "frontier=r-frontier"),
            extra=("--any", "--json"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row["instance"] for row in rows], ["frontier"])


if __name__ == "__main__":
    unittest.main()
