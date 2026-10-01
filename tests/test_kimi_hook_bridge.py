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
MISSION_ID = "00000000-0000-4000-8000-000000000201"
GENERATION_ID = "00000000-0000-4000-8000-000000000301"
WIRE_1_5_FIXTURE = ROOT / "tests" / "fixtures" / "kimi" / "wire-1.5-kimi-code-2.1.1.jsonl"


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
        self.hooks = self.root / "hooks"
        self.hooks.mkdir()
        self.state_root = self.root / "state"
        self.surface_state = self.state_root / SURFACE_UUID
        self.surface_state.mkdir(parents=True)
        self.share = self.surface_state / "share"
        self.share.mkdir()
        self.events = self.surface_state / "events.jsonl"
        # kimi-code layout: the CLI mints the session under the isolated
        # home; the test plays the CLI's role and creates it first.
        work_hash = hashlib.sha256(
            str(self.work.resolve()).encode("utf-8")
        ).hexdigest()[:12]
        self.session_root = (
            self.share / "sessions" / f"wd_{self.work.name}_{work_hash}"
        )
        cli_session = (
            self.session_root
            / "session_11111111-1111-4111-8111-111111111111"
            / "agents"
            / "main"
        )
        cli_session.mkdir(parents=True)
        for directory, _, _ in os.walk(self.root):
            Path(directory).chmod(0o700)
        self.transcript = kimi_hook_bridge.wire_path(
            self.share, self.work, SESSION_ID
        )
        self.assertEqual(self.transcript, (cli_session / "wire.jsonl").resolve())

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
            "--mission-id",
            MISSION_ID,
            "--generation-id",
            GENERATION_ID,
            "--hook-dir",
            str(self.hooks),
            "--events-file",
            str(self.events),
            "--provider",
            "moonshot-ai",
            "--model",
            "kimi-code/k3",
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

    def wait_for(self, predicate, *, timeout: float = 3.0,
                 process: subprocess.Popen[str] | None = None) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if process is not None and process.poll() is not None:
                _stdout, stderr = process.communicate(timeout=3)
                self.fail(f"Kimi bridge exited {process.returncode} before its evidence: {stderr.strip()}")
            time.sleep(0.02)
        self.fail("timed out waiting for Kimi bridge evidence")

    def test_bridge_records_bind_and_stop_without_copying_conversation(self) -> None:
        process = self.start_bridge()
        session_file = self.hooks / "kimi-hook-sessions.json"
        self.wait_for(session_file.exists, process=process)
        prompt = "private task\nFLEET_RESULT:run-1:<STATUS>"
        response = "review complete\nFLEET_RESULT:run-1:DONE"
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"type": "metadata", "protocol_version": "1.4"},
            wire_record(100.0, "TurnBegin", {"user_input": prompt}),
            wire_record(101.0, "ContentPart", {"type": "text", "text": response}),
            wire_record(102.0, "TurnEnd", {}),
        ]
        # Kimi creates its Wire transcript privately. Creating it at 0644 and
        # tightening it afterwards let the running bridge observe the unsafe
        # mode and exit, which made this test intermittent.
        descriptor = os.open(self.transcript,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8"))
        finally:
            os.close(descriptor)
        self.assertEqual(self.transcript.stat().st_mode & 0o777, 0o600)
        self.wait_for(
            lambda: self.events.exists()
            and len(self.events.read_text(encoding="utf-8").splitlines()) == 2,
            process=process,
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
        self.assertEqual(session["missionId"], MISSION_ID)
        self.assertEqual(session["generationId"], GENERATION_ID)
        self.assertEqual(session["provider"], "moonshot-ai")
        self.assertEqual(session["model"], "kimi-code/k3")

    def test_bridge_maps_wire_1_4_turn_lifecycle(self) -> None:
        begin = kimi_hook_bridge.hook_event(
            record_index=7,
            record={"type": "turn.prompt", "input": [], "time": 1785101249515},
            session_id=SESSION_ID,
            workspace_id=WORKSPACE_UUID,
            surface_id=SURFACE_UUID,
        )
        self.assertEqual(begin["name"], "agent.hook.UserPromptSubmit")
        self.assertEqual(begin["occurred_at"], "2026-07-26T21:27:29.515000+00:00")
        self.assertIsNone(
            kimi_hook_bridge.hook_event(
                record_index=15,
                record={
                    "type": "context.append_loop_event",
                    "event": {"type": "step.end", "finishReason": "tool_calls"},
                    "time": 1785101260000,
                },
                session_id=SESSION_ID,
                workspace_id=WORKSPACE_UUID,
                surface_id=SURFACE_UUID,
            )
        )
        end = kimi_hook_bridge.hook_event(
            record_index=16,
            record={
                "type": "context.append_loop_event",
                "event": {"type": "step.end", "finishReason": "end_turn"},
                "time": 1785101270265,
            },
            session_id=SESSION_ID,
            workspace_id=WORKSPACE_UUID,
            surface_id=SURFACE_UUID,
        )
        self.assertEqual(end["name"], "agent.hook.Stop")

    def test_frontier_extracts_wire_1_4_completed_turn(self) -> None:
        run_id = "run-wire-1-4"
        stopped_ms = 1785101270265
        rows = [
            {"type": "metadata", "protocol_version": "1.4"},
            {
                "type": "turn.prompt",
                "input": [
                    {"type": "text", "text": f"FLEET_RESULT:{run_id}:<STATUS>"}
                ],
                "time": stopped_ms - 1000,
            },
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "part": {
                        "type": "text",
                        "text": f"STATUS: DONE\nFLEET_RESULT:{run_id}:DONE",
                    },
                },
                "time": stopped_ms - 1,
            },
            {
                "type": "context.append_loop_event",
                "event": {"type": "step.end", "finishReason": "end_turn"},
                "time": stopped_ms,
            },
            {"type": "usage.record", "time": stopped_ms},
        ]
        with (
            mock.patch.object(
                fleet_frontier,
                "session_record",
                return_value={"provider": "moonshot-ai", "model": "kimi-code/k3"},
            ),
            mock.patch.object(fleet_frontier, "_transcript_rows", return_value=rows),
        ):
            response, provider, model = fleet_frontier.kimi_turn_evidence(
                f"kimi-{SESSION_ID}",
                run_id,
                "2026-07-26T21:27:50.265000+00:00",
            )
        self.assertIn(f"FLEET_RESULT:{run_id}:DONE", response)
        self.assertEqual((provider, model), ("moonshot-ai", "kimi-code/k3"))

    def write_private_transcript(self, raw: bytes) -> None:
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.transcript,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, raw)
        finally:
            os.close(descriptor)

    def test_bridge_maps_captured_kimi_code_2_1_1_wire_1_5(self) -> None:
        # Verbatim turn records from a provider-free kimi-code 2.1.1 run.
        rows = [json.loads(line) for line in WIRE_1_5_FIXTURE.read_text().splitlines()]
        self.assertEqual(rows[0]["protocol_version"], "1.5")
        process = self.start_bridge()
        self.wait_for((self.hooks / "kimi-hook-sessions.json").exists, process=process)
        self.write_private_transcript(WIRE_1_5_FIXTURE.read_bytes())
        self.wait_for(
            lambda: self.events.exists()
            and len(self.events.read_text(encoding="utf-8").splitlines()) == 2,
            process=process,
        )
        events = [json.loads(line) for line in self.events.read_text().splitlines()]
        self.assertEqual([e["name"] for e in events],
                         ["agent.hook.UserPromptSubmit", "agent.hook.Stop"])
        prompt = next(r for r in rows if r["type"] == "turn.prompt")
        stop = next(r for r in rows if r.get("event", {}).get("type") == "step.end")
        self.assertEqual(events[0]["occurred_at"], kimi_hook_bridge._occurred_at(prompt["time"]))
        self.assertEqual(events[1]["occurred_at"], kimi_hook_bridge._occurred_at(stop["time"]))
        self.assertIsNone(process.poll())

    def test_frontier_extracts_captured_wire_1_5_turn(self) -> None:
        rows = [json.loads(line) for line in WIRE_1_5_FIXTURE.read_text().splitlines()]
        prompt = next(r for r in rows if r["type"] == "turn.prompt")["input"][0]["text"]
        run_id = prompt.split("FLEET_RESULT:", 1)[1].split(":<STATUS>", 1)[0]
        stop = next(r for r in rows if r.get("event", {}).get("type") == "step.end")
        with (
            mock.patch.object(fleet_frontier, "session_record",
                              return_value={"provider": "moonshot-ai", "model": "kimi-code/k3"}),
            mock.patch.object(fleet_frontier, "_transcript_rows", return_value=rows),
        ):
            response, provider, model = fleet_frontier.kimi_turn_evidence(
                f"kimi-{SESSION_ID}", run_id, kimi_hook_bridge._occurred_at(stop["time"]))
        self.assertEqual(response, f"FLEET_RESULT:{run_id}:PASS fixture reply")
        self.assertEqual((provider, model), ("moonshot-ai", "kimi-code/k3"))

    def test_bridge_still_rejects_an_unknown_wire_protocol(self) -> None:
        process = self.start_bridge()
        self.wait_for((self.hooks / "kimi-hook-sessions.json").exists, process=process)
        self.write_private_transcript(
            (json.dumps({"type": "metadata", "protocol_version": "1.6"}) + "\n").encode())
        _stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 2)
        self.assertIn("unsupported Kimi Wire protocol", stderr)

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

    def test_bridge_rejects_lock_with_unsafe_mode(self) -> None:
        lock = self.surface_state / ".bridge.lock"
        lock.touch(mode=0o600)
        lock.chmod(0o644)
        with self.assertRaisesRegex(
            kimi_hook_bridge.KimiBridgeError, "owner-bound mode 0600"
        ):
            kimi_hook_bridge.acquire_bridge_lock(self.surface_state)

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
                    {"type": "metadata", "protocol_version": "1.10"},
                    wire_record(100.0, "TurnBegin", {"user_input": prompt}),
                    wire_record(
                        101.0, "ContentPart", {"type": "text", "text": response}
                    ),
                    wire_record(stopped_at, "TurnEnd", {}),
                )
            ),
            encoding="utf-8",
        )
        self.transcript.chmod(0o600)
        kimi_hook_bridge.record_session(
            self.hooks,
            share_dir=self.share,
            session_id=SESSION_ID,
            workspace_id=WORKSPACE_UUID,
            surface_id=SURFACE_UUID,
            mission_id=MISSION_ID,
            generation_id=GENERATION_ID,
            transcript_path=self.transcript,
            provider="moonshot-ai",
            model="kimi-code/k3",
        )
        with (
            mock.patch.object(fleet_frontier, "KIMI_STATE_ROOT", self.state_root),
            mock.patch.dict(os.environ, {"CMUX_HOOK_DIR": str(self.hooks)}),
        ):
            evidence = fleet_frontier.kimi_turn_evidence(
                f"kimi-{SESSION_ID}",
                run_id,
                kimi_hook_bridge._occurred_at(stopped_at),
            )
        self.assertEqual(
            evidence,
            (response, "moonshot-ai", "kimi-code/k3"),
        )

    def test_kimi_event_store_rejects_symlink(self) -> None:
        target = self.root / "outside.jsonl"
        target.write_text("", encoding="utf-8")
        self.events.symlink_to(target)
        with mock.patch.object(fleet_frontier, "KIMI_STATE_ROOT", self.state_root):
            with self.assertRaisesRegex(fleet_frontier.FrontierError, "safe Kimi"):
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
            mock.patch.object(
                fleet_frontier.fleet_kimi_state,
                "require_bridge_health",
                return_value={},
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
                model="kimi-code/k3",
                hook_source="kimi",
                mission_id=MISSION_ID,
                generation_id=GENERATION_ID,
                run_id=run_id,
            )
            now = time.time() + 1
            response = f"verified\nFLEET_RESULT:{run_id}:DONE"
            rows = [
                {"type": "metadata", "protocol_version": "1.10"},
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
            self.transcript.chmod(0o600)
            kimi_hook_bridge.record_session(
                self.hooks,
                share_dir=self.share,
                session_id=SESSION_ID,
                workspace_id=WORKSPACE_UUID,
                surface_id=SURFACE_UUID,
                mission_id=MISSION_ID,
                generation_id=GENERATION_ID,
                transcript_path=self.transcript,
                provider="moonshot-ai",
                model="kimi-code/k3",
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

    def test_wire_path_accepts_kimi_code_truncated_workdir_basename(self) -> None:
        import shutil

        shutil.rmtree(self.session_root)
        work_hash = hashlib.sha256(
            str(self.work.resolve()).encode("utf-8")
        ).hexdigest()[:12]
        truncated_root = (
            self.share
            / "sessions"
            / f"wd_{self.work.name[:1]}_{work_hash}"
        )
        main = (
            truncated_root
            / "session_22222222-2222-4222-8222-222222222222"
            / "agents"
            / "main"
        )
        main.mkdir(parents=True)
        for directory, _, _ in os.walk(truncated_root):
            Path(directory).chmod(0o700)

        resolved = kimi_hook_bridge.wire_path(
            self.share, self.work, SESSION_ID
        )

        self.assertEqual(resolved, (main / "wire.jsonl").resolve())

    def test_wire_path_rejects_ambiguous_hashed_work_roots(self) -> None:
        work_hash = hashlib.sha256(
            str(self.work.resolve()).encode("utf-8")
        ).hexdigest()[:12]
        second_root = self.share / "sessions" / f"wd_other_{work_hash}"
        main = (
            second_root
            / "session_22222222-2222-4222-8222-222222222222"
            / "agents"
            / "main"
        )
        main.mkdir(parents=True)
        for directory, _, _ in os.walk(second_root):
            Path(directory).chmod(0o700)

        with self.assertRaisesRegex(
            kimi_hook_bridge.KimiBridgeError, "multiple Kimi work roots"
        ):
            kimi_hook_bridge.wire_path(self.share, self.work, SESSION_ID)

    def test_wire_path_rejects_legacy_kimi_cli_layout(self) -> None:
        import shutil

        shutil.rmtree(self.session_root)
        legacy_hash = hashlib.md5(
            str(self.work.resolve()).encode("utf-8")
        ).hexdigest()
        (self.share / "sessions" / legacy_hash).mkdir(parents=True)
        (self.share / "sessions" / legacy_hash).chmod(0o700)
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
            for directory, _, _ in os.walk(self.session_root):
                Path(directory).chmod(0o700)

        thread = threading.Thread(target=mint)
        thread.start()
        try:
            resolved = kimi_hook_bridge.resolve_wire_path(
                self.share, self.work, SESSION_ID, timeout_seconds=5
            )
        finally:
            thread.join()
        self.assertEqual(resolved, (minted / "wire.jsonl").resolve())

    def test_resolve_wire_path_waits_for_agents_directory(self) -> None:
        import shutil
        import threading

        shutil.rmtree(self.session_root)
        partial = (
            self.session_root
            / "session_44444444-4444-4444-8444-444444444444"
        )
        partial.mkdir(parents=True)
        for directory, _, _ in os.walk(self.session_root):
            Path(directory).chmod(0o700)
        minted = partial / "agents" / "main"

        def mint_agents() -> None:
            time.sleep(0.4)
            minted.mkdir(parents=True)
            for directory, _, _ in os.walk(partial):
                Path(directory).chmod(0o700)

        thread = threading.Thread(target=mint_agents)
        thread.start()
        try:
            resolved = kimi_hook_bridge.resolve_wire_path(
                self.share, self.work, SESSION_ID, timeout_seconds=5
            )
        finally:
            thread.join()
        self.assertEqual(resolved, (minted / "wire.jsonl").resolve())

    def test_wire_path_rejects_symlinked_agents_directory(self) -> None:
        import shutil

        session = next(self.session_root.glob("session_*"))
        shutil.rmtree(session / "agents")
        outside = self.root / "outside-agents"
        (outside / "main").mkdir(parents=True)
        for directory, _, _ in os.walk(outside):
            Path(directory).chmod(0o700)
        (session / "agents").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(
            kimi_hook_bridge.KimiBridgeError, "session path is unsafe"
        ):
            kimi_hook_bridge.wire_path(self.share, self.work, SESSION_ID)


if __name__ == "__main__":
    unittest.main()
