from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_frontier  # noqa: E402
import fleet_leases  # noqa: E402
from fleet_ledger import append_event, latest_event  # noqa: E402


WORKSPACE_UUID = "00000000-0000-0000-0000-000000000001"
SURFACE_UUID = "00000000-0000-0000-0000-000000000101"
SESSION_ID = "opencode-ses-frontier"


class FleetFrontierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.hooks = self.tmp / "hooks"
        self.hooks.mkdir()
        self.events_log = self.tmp / "events.jsonl"
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "CMUX_HOOK_DIR": str(self.hooks),
                "CMUX_EVENTS_LOG": str(self.events_log),
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.write_session(SESSION_ID, SURFACE_UUID)

    def write_session(self, session_id: str, surface_uuid: str) -> None:
        key = session_id.removeprefix("opencode-")
        (self.hooks / "opencode-hook-sessions.json").write_text(
            json.dumps(
                {
                    "sessions": {
                        key: {
                            "sessionId": key,
                            "workspaceId": WORKSPACE_UUID,
                            "surfaceId": surface_uuid,
                            "updatedAt": 10,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def seed_run(self, run_id: str, *, boot_id: str = "boot-1", after_seq: int = 100):
        lease = fleet_leases.acquire_frontier(
            self.runs,
            run_id=run_id,
            feature="frontier",
            instance="agent",
            role="minimax",
            phase="CHALLENGE",
            task_sha256="a" * 64,
            workspace_uuid=WORKSPACE_UUID,
            surface_uuid=SURFACE_UUID,
            tree_reader=lambda: "",
        )
        event = {
            "timestamp": "2026-07-12T00:00:00+00:00",
            "dispatched_at": "2026-07-12T00:00:00+00:00",
            "run_id": run_id,
            "feature": "frontier",
            "instance": "agent",
            "role": "minimax",
            "phase": "CHALLENGE",
            "runner": "interactive",
            "status": "dispatched",
            "task_sha256": "a" * 64,
            "workspace_uuid": WORKSPACE_UUID,
            "surface_uuid": SURFACE_UUID,
            "event_boot_id": boot_id,
            "after_seq": after_seq,
        }
        append_event(self.runs / "fleet-frontier.ledger.jsonl", event)
        return event, lease

    @staticmethod
    def hook_event(
        name: str,
        seq: int,
        *,
        boot_id: str = "boot-1",
        phase: str = "received",
        session_id: str = SESSION_ID,
        occurred_at: str = "2026-07-12T00:00:01+00:00",
    ) -> dict:
        return {
            "type": "event",
            "protocol": "cmux-events",
            "boot_id": boot_id,
            "seq": seq,
            "id": f"{boot_id}-{seq}",
            "name": name,
            "category": "agent",
            "occurred_at": occurred_at,
            "workspace_id": WORKSPACE_UUID,
            "payload": {"phase": phase, "session_id": session_id},
        }

    def test_exact_binding_ignores_old_stop_then_terminalizes_verified_sentinel(self) -> None:
        run_id = "run-exact"
        state, lease = self.seed_run(run_id)
        old_stop = self.hook_event("agent.hook.Stop", 99, phase="completed")
        self.assertIsNone(
            fleet_frontier.process_event(
                self.runs,
                state,
                old_stop,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        )
        stale_duplicate = self.hook_event("agent.hook.Stop", 101, phase="completed")
        self.assertIsNone(
            fleet_frontier.process_event(
                self.runs,
                state,
                stale_duplicate,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        )
        binding = self.hook_event("agent.hook.UserPromptSubmit", 102)
        fleet_frontier.process_event(
            self.runs,
            state,
            binding,
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        stop = self.hook_event("agent.hook.Stop", 103, phase="completed")
        with mock.patch.object(
            fleet_frontier,
            "read_screen",
            return_value=f"answer\nFLEET_RESULT:{run_id}:DONE\n",
        ):
            terminal = fleet_frontier.process_event(
                self.runs,
                state,
                stop,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertEqual(terminal["status"], "succeeded")
        self.assertEqual(terminal["session_id"], SESSION_ID)
        self.assertFalse(lease.exists())

    def test_missing_or_duplicate_sentinel_is_indeterminate(self) -> None:
        self.assertEqual(
            fleet_frontier.sentinel_status("no sentinel", "run-1"),
            ("indeterminate", "frontier_sentinel_missing"),
        )
        duplicate = "FLEET_RESULT:run-1:DONE\nFLEET_RESULT:run-1:DONE\n"
        self.assertEqual(
            fleet_frontier.sentinel_status(duplicate, "run-1"),
            ("indeterminate", "frontier_sentinel_ambiguous"),
        )
        self.assertEqual(
            fleet_frontier.sentinel_status(
                "FLEET_RESULT:run-1:DONE\nmore output\n", "run-1"
            ),
            ("indeterminate", "frontier_sentinel_not_final"),
        )
        self.assertEqual(
            fleet_frontier.sentinel_status(
                "answer\nFLEET_RESULT:run-1:DONE\n\n› next prompt\n"
                "  gpt-5.6-sol high · ~/repo\n",
                "run-1",
            ),
            ("succeeded", "frontier_sentinel_verified"),
        )
        self.assertEqual(
            fleet_frontier.sentinel_status(
                "FLEET_RESULT:run-1:DONE\nmore output\n› next prompt\n", "run-1"
            ),
            ("indeterminate", "frontier_sentinel_not_final"),
        )
        self.assertEqual(
            fleet_frontier.sentinel_status(
                "FLEET_RESULT:run-1:DONE\n› idle\nresponse after prompt\n",
                "run-1",
            ),
            ("indeterminate", "frontier_sentinel_not_final"),
        )

    def test_second_submit_in_same_session_is_ambiguous_and_retains_lease(self) -> None:
        state, lease = self.seed_run("run-two-submits")
        first = self.hook_event("agent.hook.UserPromptSubmit", 101)
        fleet_frontier.process_event(
            self.runs,
            state,
            first,
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        duplicate_replay = fleet_frontier.process_event(
            self.runs,
            state,
            first,
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        self.assertIsNone(duplicate_replay)
        second = self.hook_event(
            "agent.hook.UserPromptSubmit",
            102,
            occurred_at="2026-07-12T00:00:02+00:00",
        )
        terminal = fleet_frontier.process_event(
            self.runs,
            state,
            second,
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        self.assertEqual(terminal["status"], "indeterminate")
        self.assertEqual(terminal["reason"], "frontier_session_binding_ambiguous")
        self.assertTrue(terminal["lease_retained"])
        self.assertTrue(lease.exists())

    def test_stop_must_be_after_binding(self) -> None:
        state, lease = self.seed_run("run-stop-order")
        binding = self.hook_event(
            "agent.hook.UserPromptSubmit",
            103,
            occurred_at="2026-07-12T00:00:03+00:00",
        )
        fleet_frontier.process_event(
            self.runs,
            state,
            binding,
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        earlier_stop = self.hook_event(
            "agent.hook.Stop",
            102,
            phase="completed",
            occurred_at="2026-07-12T00:00:02+00:00",
        )
        with mock.patch.object(fleet_frontier, "read_screen") as read:
            terminal = fleet_frontier.process_event(
                self.runs,
                state,
                earlier_stop,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertIsNone(terminal)
        read.assert_not_called()
        self.assertTrue(lease.exists())

    def test_losing_terminalizer_cannot_release_retained_lease(self) -> None:
        state, lease = self.seed_run("run-terminal-race")
        first = fleet_frontier.terminalize(
            self.runs,
            dict(state),
            status="indeterminate",
            reason="first_terminal",
            release_lease=False,
        )
        self.assertEqual(first["status"], "indeterminate")
        stale = fleet_frontier.terminalize(
            self.runs,
            dict(state),
            status="succeeded",
            reason="stale_terminal",
        )
        self.assertEqual(stale["status"], "indeterminate")
        self.assertTrue(lease.exists())

    def test_prepare_identity_is_durable_before_lease_acquisition(self) -> None:
        ledger = self.runs / "fleet-frontier.ledger.jsonl"

        def reject_after_observing_state(*args, **kwargs):
            events = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertEqual(events[-1]["status"], "preparing")
            self.assertEqual(events[-1]["instance"], "agent")
            raise fleet_leases.LeaseBusy("occupied")

        with mock.patch.object(
            fleet_frontier, "acquire_frontier", side_effect=reject_after_observing_state
        ):
            with self.assertRaises(fleet_leases.LeaseBusy):
                fleet_frontier.prepare_run(
                    self.runs,
                    feature="frontier",
                    instance="agent",
                    role="minimax",
                    phase="CHALLENGE",
                    task="task",
                    workspace_uuid=WORKSPACE_UUID,
                    surface_uuid=SURFACE_UUID,
                )
        events = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual(events[-1]["status"], "abandoned")

    def test_cross_boot_audit_recovers_binding_stop_and_status(self) -> None:
        run_id = "run-replay"
        state, lease = self.seed_run(run_id, boot_id="boot-old", after_seq=900)
        events = [
            self.hook_event(
                "surface.action",
                900,
                boot_id="boot-old",
                phase="completed",
                occurred_at="2026-07-12T00:00:00+00:00",
            ),
            self.hook_event(
                "agent.hook.UserPromptSubmit", 1, boot_id="boot-new", phase="received"
            ),
            self.hook_event(
                "agent.hook.Stop", 2, boot_id="boot-new", phase="completed"
            ),
        ]
        self.events_log.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )
        with mock.patch.object(
            fleet_frontier,
            "read_screen",
            return_value=f"FLEET_RESULT:{run_id}:BLOCKED\n",
        ):
            terminal = fleet_frontier.recover_from_audit(
                self.runs,
                state,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertEqual(terminal["status"], "blocked")
        self.assertEqual(terminal["completion_boot_id"], "boot-new")
        self.assertFalse(lease.exists())

    def test_truncated_audit_cannot_bind_a_later_submit(self) -> None:
        run_id = "run-truncated"
        state, lease = self.seed_run(run_id, boot_id="boot-old", after_seq=900)
        events = [
            self.hook_event(
                "agent.hook.UserPromptSubmit", 1, boot_id="boot-new", phase="received"
            ),
            self.hook_event(
                "agent.hook.Stop", 2, boot_id="boot-new", phase="completed"
            ),
        ]
        self.events_log.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )
        with mock.patch.object(
            fleet_frontier,
            "read_screen",
            return_value=f"FLEET_RESULT:{run_id}:DONE\n",
        ):
            terminal = fleet_frontier.recover_from_audit(
                self.runs,
                state,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertEqual(terminal["status"], "indeterminate")
        self.assertEqual(terminal["reason"], "frontier_event_gap_unrecoverable")
        self.assertTrue(terminal["lease_retained"])
        self.assertTrue(lease.exists())

    def test_unrecoverable_gap_is_indeterminate_and_retains_lease_until_abandon(self) -> None:
        run_id = "run-gap"
        state, lease = self.seed_run(run_id, boot_id="boot-old", after_seq=900)
        terminal = fleet_frontier.recover_from_audit(
            self.runs,
            state,
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        self.assertEqual(terminal["status"], "indeterminate")
        self.assertTrue(terminal["lease_retained"])
        self.assertTrue(lease.exists())
        with self.assertRaises(fleet_leases.LeaseBusy):
            fleet_leases.acquire_frontier(
                self.runs,
                run_id="run-after-gap",
                feature="frontier",
                instance="agent",
                role="minimax",
                phase="CHALLENGE",
                task_sha256="b" * 64,
                workspace_uuid=WORKSPACE_UUID,
                surface_uuid=SURFACE_UUID,
                tree_reader=lambda: (
                    f"workspace workspace:1 {WORKSPACE_UUID}\n"
                    f"surface surface:1 {SURFACE_UUID}\n"
                ),
            )
        fleet_frontier.abandon_run(
            self.runs,
            feature="frontier",
            instance="agent",
            run_id=run_id,
            reason="operator_confirmed",
        )
        self.assertFalse(lease.exists())
        self.assertEqual(
            latest_event(
                self.runs / "fleet-frontier.ledger.jsonl",
                run_id=run_id,
                instance="agent",
            )["status"],
            "indeterminate",
        )

    def test_frontier_instance_lease_rejects_second_active_run(self) -> None:
        self.seed_run("run-owner")
        with self.assertRaises(fleet_leases.LeaseBusy):
            fleet_leases.acquire_frontier(
                self.runs,
                run_id="run-second",
                feature="frontier",
                instance="agent",
                role="minimax",
                phase="CHALLENGE",
                task_sha256="b" * 64,
                workspace_uuid=WORKSPACE_UUID,
                surface_uuid=SURFACE_UUID,
                tree_reader=lambda: (
                    f"workspace workspace:1 {WORKSPACE_UUID}\n"
                    f"surface surface:1 {SURFACE_UUID}\n"
                ),
            )

    def test_frontier_surface_rejects_alias_from_another_feature(self) -> None:
        self.seed_run("run-surface-owner")
        with self.assertRaisesRegex(fleet_leases.LeaseBusy, "surface is busy"):
            fleet_leases.acquire_frontier(
                self.runs,
                run_id="run-alias",
                feature="other-feature",
                instance="other-agent",
                role="codex",
                phase="BUILD",
                task_sha256="c" * 64,
                workspace_uuid=WORKSPACE_UUID,
                surface_uuid=SURFACE_UUID,
                tree_reader=lambda: (
                    f"workspace workspace:1 {WORKSPACE_UUID}\n"
                    f"surface surface:1 {SURFACE_UUID}\n"
                ),
            )

    def test_malformed_lease_blocks_frontier_acquisition(self) -> None:
        malformed = self.runs / "locks" / "malformed.lock"
        malformed.mkdir(parents=True)
        (malformed / "lease.json").write_text("{not-json", encoding="utf-8")
        with self.assertRaisesRegex(
            fleet_leases.LeaseBusy, "unknown or malformed leases block acquisition"
        ):
            fleet_leases.acquire_frontier(
                self.runs,
                run_id="run-after-malformed",
                feature="frontier",
                instance="agent",
                role="codex",
                phase="BUILD",
                task_sha256="d" * 64,
                workspace_uuid=WORKSPACE_UUID,
                surface_uuid=SURFACE_UUID,
                tree_reader=lambda: "",
            )


if __name__ == "__main__":
    unittest.main()
