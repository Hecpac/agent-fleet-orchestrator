from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_frontier  # noqa: E402
import fleet_tracking  # noqa: E402
import kimi_hook_bridge  # noqa: E402
from fleet_ledger import events_for_run  # noqa: E402


WORKSPACE_UUID = "00000000-0000-0000-0000-000000000001"
SURFACE_UUID = "00000000-0000-0000-0000-000000000101"
SESSION_ID = "00000000-0000-0000-0000-000000000101"


def wire_record(timestamp: float, message_type: str, payload: dict) -> dict:
    return {
        "timestamp": timestamp,
        "message": {"type": message_type, "payload": payload},
    }


class KimiHookBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.share = self.root / "share"
        self.share.mkdir()
        self.hooks = self.root / "hooks"
        self.hooks.mkdir()
        self.state_root = self.root / "state"
        self.surface_state = self.state_root / SURFACE_UUID
        self.surface_state.mkdir(parents=True)
        self.events = self.surface_state / "events.jsonl"
        # kimi-code layout: the CLI mints the session under the isolated
        # home; the test plays the CLI's role and creates it first.
        work_hash = hashlib.sha256(
            str(self.work.resolve()).encode("utf-8")
        ).hexdigest()[:12]
        self.session_root = self.share / "sessions" / f"wd_cwd_{work_hash}"
        cli_session = (
            self.session_root
            / "session_11111111-1111-4111-8111-111111111111"
            / "agents"
            / "main"
        )
        cli_session.mkdir(parents=True)
        self.transcript = kimi_hook_bridge.wire_path(
            self.share, self.work, SESSION_ID
        )
        self.assertEqual(self.transcript, cli_session / "wire.jsonl")

    def bridge_command(self) -> list[str]:
        return [
            sys.executable,
            str(ROOT / "scripts" / "kimi_hook_bridge.py"),
            "--share-dir",
            str(self.share),
            "--work-dir",
            str(self.work),
            "--session-id",
            SESSION_ID,
            "--workspace-id",
            WORKSPACE_UUID,
            "--surface-id",
            SURFACE_UUID,
            "--hook-dir",
            str(self.hooks),
            "--events-file",
            str(self.events),
            "--provider",
            "moonshot-ai",
            "--model",
            "moonshot-ai/kimi-k3",
            "--poll-interval",
            "0.02",
        ]

    def start_bridge(self) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            self.bridge_command(),
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self._stop, process)
        return process

    @staticmethod
    def _stop(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
        else:
            process.communicate(timeout=3)

    def wait_for(self, predicate, *, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for Kimi bridge evidence")

    def test_bridge_records_bind_and_stop_without_copying_conversation(self) -> None:
        process = self.start_bridge()
        session_file = self.hooks / "kimi-hook-sessions.json"
        self.wait_for(session_file.exists)
        prompt = "private task\nFLEET_RESULT:run-1:<STATUS>"
        response = "review complete\nFLEET_RESULT:run-1:DONE"
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"type": "metadata", "protocol_version": "1.10"},
            wire_record(100.0, "TurnBegin", {"user_input": prompt}),
            wire_record(101.0, "ContentPart", {"type": "text", "text": response}),
            wire_record(102.0, "TurnEnd", {}),
        ]
        self.transcript.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        self.wait_for(
            lambda: self.events.exists()
            and len(self.events.read_text(encoding="utf-8").splitlines()) == 2
        )
        self.assertIsNone(process.poll())
        events_text = self.events.read_text(encoding="utf-8")
        events = [json.loads(line) for line in events_text.splitlines()]
        self.assertEqual(
            [event["name"] for event in events],
            ["agent.hook.UserPromptSubmit", "agent.hook.Stop"],
        )
        self.assertNotIn(prompt, events_text)
        self.assertNotIn(response, events_text)
        session = json.loads(session_file.read_text(encoding="utf-8"))["sessions"][
            SESSION_ID
        ]
        self.assertEqual(
            Path(session["transcriptPath"]).resolve(), self.transcript.resolve()
        )
        self.assertEqual(session["workspaceId"], WORKSPACE_UUID)
        self.assertEqual(session["surfaceId"], SURFACE_UUID)
        self.assertEqual(session["provider"], "moonshot-ai")
        self.assertEqual(session["model"], "moonshot-ai/kimi-k3")

    def test_bridge_rejects_a_second_watcher_for_the_same_surface(self) -> None:
        self.start_bridge()
        self.wait_for((self.hooks / "kimi-hook-sessions.json").exists)
        duplicate = subprocess.run(
            self.bridge_command(),
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("another Kimi bridge owns this surface", duplicate.stderr)

    def test_frontier_extracts_exact_completed_kimi_turn_and_identity(self) -> None:
        run_id = "run-kimi-evidence"
        prompt = f"inspect\nFLEET_RESULT:{run_id}:<STATUS>"
        response = f"evidence\nFLEET_RESULT:{run_id}:DONE"
        stopped_at = 1784419234.4319842
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in (
                    {"type": "metadata", "protocol_version": "1.3"},
                    wire_record(100.0, "TurnBegin", {"user_input": prompt}),
                    wire_record(
                        101.0, "ContentPart", {"type": "text", "text": response}
                    ),
                    wire_record(stopped_at, "TurnEnd", {}),
                )
            ),
            encoding="utf-8",
        )
        kimi_hook_bridge.record_session(
            self.hooks,
            session_id=SESSION_ID,
            workspace_id=WORKSPACE_UUID,
            surface_id=SURFACE_UUID,
            transcript_path=self.transcript,
            provider="moonshot-ai",
            model="moonshot-ai/kimi-k3",
        )
        with mock.patch.dict(os.environ, {"CMUX_HOOK_DIR": str(self.hooks)}):
            evidence = fleet_frontier.kimi_turn_evidence(
                f"kimi-{SESSION_ID}",
                run_id,
                kimi_hook_bridge._occurred_at(stopped_at),
            )
        self.assertEqual(
            evidence,
            (response, "moonshot-ai", "moonshot-ai/kimi-k3"),
        )

    def test_kimi_event_store_rejects_symlink(self) -> None:
        target = self.root / "outside.jsonl"
        target.write_text("", encoding="utf-8")
        self.events.symlink_to(target)
        with mock.patch.object(fleet_frontier, "KIMI_STATE_ROOT", self.state_root):
            with self.assertRaisesRegex(fleet_frontier.FrontierError, "symlink"):
                fleet_frontier.kimi_hook_events(SURFACE_UUID)

    def test_kimi_events_complete_a_control_v1_frontier_run(self) -> None:
        runs = self.root / "runs"
        runs.mkdir()
        run_id = "00000000-0000-4000-8000-000000000777"
        ack = {
            "type": "ack",
            "protocol": "cmux-events",
            "version": 1,
            "boot_id": "cmux-boot",
            "replay_count": 0,
            "resume": {
                "gap": False,
                "oldest_seq": 1,
                "latest_seq": 10,
                "next_seq": 11,
            },
        }
        fake_lease = runs / "locks" / "kimi.lock"
        with (
            mock.patch.object(fleet_frontier, "KIMI_STATE_ROOT", self.state_root),
            mock.patch.object(fleet_frontier, "event_ack", return_value=ack),
            mock.patch.object(
                fleet_frontier, "acquire_frontier", return_value=fake_lease
            ),
            mock.patch.dict(os.environ, {"CMUX_HOOK_DIR": str(self.hooks)}),
        ):
            prepared = fleet_frontier.prepare_run(
                runs,
                feature="kimi-e2e",
                instance="verify",
                role="kimi",
                phase="VERIFY",
                task="Review the repository.",
                workspace_uuid=WORKSPACE_UUID,
                surface_uuid=SURFACE_UUID,
                provider="moonshot-ai",
                model="moonshot-ai/kimi-k3",
                hook_source="kimi",
                run_id=run_id,
            )
            now = time.time() + 1
            response = f"verified\nFLEET_RESULT:{run_id}:DONE"
            rows = [
                {"type": "metadata", "protocol_version": "1.3"},
                wire_record(now, "TurnBegin", {"user_input": prepared["prompt"]}),
                wire_record(
                    now + 1,
                    "ContentPart",
                    {"type": "text", "text": response},
                ),
                wire_record(now + 2, "TurnEnd", {}),
            ]
            self.transcript.parent.mkdir(parents=True, exist_ok=True)
            self.transcript.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            kimi_hook_bridge.record_session(
                self.hooks,
                session_id=SESSION_ID,
                workspace_id=WORKSPACE_UUID,
                surface_id=SURFACE_UUID,
                transcript_path=self.transcript,
                provider="moonshot-ai",
                model="moonshot-ai/kimi-k3",
            )
            for index, row in ((2, rows[1]), (4, rows[3])):
                event = kimi_hook_bridge.hook_event(
                    record_index=index,
                    record=row,
                    session_id=SESSION_ID,
                    workspace_id=WORKSPACE_UUID,
                    surface_id=SURFACE_UUID,
                )
                assert event is not None
                kimi_hook_bridge.append_event(self.events, event)
            fleet_frontier.authorize_prompt_submission(
                runs,
                feature="kimi-e2e",
                instance="verify",
                run_id=run_id,
                workspace_uuid=WORKSPACE_UUID,
                hook_source="kimi",
                since=prepared["dispatched_at"],
            )
            state = fleet_frontier.frontier_state(
                runs / "fleet-kimi-e2e.ledger.jsonl",
                run_id=run_id,
                instance="verify",
                runs_dir=runs,
            )
            assert state is not None
            terminal = fleet_frontier.reconcile_kimi_events(
                runs,
                state,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal["status"], "succeeded")
        result = Path(terminal["result_file"])
        self.assertEqual(result.read_text(encoding="utf-8"), response)
        run_events = events_for_run(
            runs / "fleet-kimi-e2e.ledger.jsonl",
            run_id=run_id,
            instance="verify",
            runs_dir=runs,
        )
        verified = fleet_tracking.verify_run_events(
            run_events, required_protocol="control-v1"
        )
        self.assertEqual(verified["status"], "succeeded")

    def test_wire_path_fails_closed_on_ambiguous_sessions(self) -> None:
        second = (
            self.session_root
            / "session_22222222-2222-4222-8222-222222222222"
            / "agents"
            / "main"
        )
        second.mkdir(parents=True)
        with self.assertRaisesRegex(
            kimi_hook_bridge.KimiBridgeError, "ambiguous"
        ):
            kimi_hook_bridge.wire_path(self.share, self.work, SESSION_ID)

    def test_wire_path_rejects_legacy_kimi_cli_layout(self) -> None:
        import shutil

        shutil.rmtree(self.session_root)
        legacy_hash = hashlib.md5(
            str(self.work.resolve()).encode("utf-8")
        ).hexdigest()
        (self.share / "sessions" / legacy_hash).mkdir(parents=True)
        with self.assertRaisesRegex(
            kimi_hook_bridge.KimiBridgeError, "legacy kimi-cli"
        ):
            kimi_hook_bridge.wire_path(self.share, self.work, SESSION_ID)

    def test_resolve_wire_path_waits_for_session_minting(self) -> None:
        import shutil
        import threading

        shutil.rmtree(self.session_root)
        minted = (
            self.session_root
            / "session_33333333-3333-4333-8333-333333333333"
            / "agents"
            / "main"
        )

        def mint() -> None:
            time.sleep(0.4)
            minted.mkdir(parents=True)

        thread = threading.Thread(target=mint)
        thread.start()
        try:
            resolved = kimi_hook_bridge.resolve_wire_path(
                self.share, self.work, SESSION_ID, timeout_seconds=5
            )
        finally:
            thread.join()
        self.assertEqual(resolved, minted / "wire.jsonl")


if __name__ == "__main__":
    unittest.main()
