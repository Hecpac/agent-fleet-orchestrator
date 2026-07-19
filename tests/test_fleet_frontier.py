from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
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
CODEX_SESSION_ID = "codex-session-frontier"
CLAUDE_SESSION_ID = "claude-session-frontier"
SESSION_ID = "opencode-ses_frontier"


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
        for session_id in (CODEX_SESSION_ID, CLAUDE_SESSION_ID, SESSION_ID):
            self.write_session(session_id, SURFACE_UUID)

    def write_session(self, session_id: str, surface_uuid: str) -> None:
        source, key = session_id.split("-", 1)
        session_file = self.hooks / f"{source}-hook-sessions.json"
        data = (
            json.loads(session_file.read_text())
            if session_file.exists()
            else {"sessions": {}}
        )
        transcript = self.tmp / f"{source}-{key}.jsonl"
        data["sessions"][key] = {
            "sessionId": key,
            "workspaceId": WORKSPACE_UUID,
            "surfaceId": surface_uuid,
            "transcriptPath": str(transcript),
            "updatedAt": 10,
        }
        session_file.write_text(json.dumps(data), encoding="utf-8")

    def write_transcript(self, session_id: str, rows: list[dict]) -> None:
        source, key = session_id.split("-", 1)
        data = json.loads((self.hooks / f"{source}-hook-sessions.json").read_text())
        transcript = Path(data["sessions"][key]["transcriptPath"])
        transcript.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

    @staticmethod
    def ambiguous_object_documents(valid: bytes) -> dict[str, bytes]:
        """Keep the expected object valid if a permissive parser accepts the extension."""
        document = valid.strip()
        if not document.startswith(b"{") or not document.endswith(b"}"):
            raise AssertionError("adversarial fixture must be a JSON object")
        prefix = document[:-1]
        return {
            "duplicate": prefix + b',"ambiguous":1,"ambiguous":2}',
            "nan": prefix + b',"ambiguous":NaN}',
            "infinity": prefix + b',"ambiguous":Infinity}',
            "overflow": prefix + b',"ambiguous":1e999}',
            "bom": b"\xef\xbb\xbf" + document,
            "bad_utf8": prefix + b',"ambiguous":"\xff"}',
            "surrogate": prefix + b',"ambiguous":"\\ud800"}',
            "trailing": document + b" trailing",
        }

    def filesystem_snapshot(self) -> dict[str, tuple]:
        snapshot: dict[str, tuple] = {}
        for path in sorted(self.tmp.rglob("*")):
            relative = str(path.relative_to(self.tmp))
            stat = path.lstat()
            if path.is_symlink():
                snapshot[relative] = (
                    "symlink",
                    os.readlink(path),
                    stat.st_mode,
                    stat.st_ino,
                    stat.st_mtime_ns,
                )
            elif path.is_file():
                snapshot[relative] = (
                    "file",
                    path.read_bytes(),
                    stat.st_mode,
                    stat.st_ino,
                    stat.st_mtime_ns,
                )
            else:
                snapshot[relative] = (
                    "directory",
                    stat.st_mode,
                    stat.st_ino,
                    stat.st_mtime_ns,
                )
        return snapshot

    def seed_run(
        self,
        run_id: str,
        *,
        boot_id: str = "boot-1",
        after_seq: int = 100,
        opencode: bool = False,
        hook_source: str = "codex",
        variant: str = "",
        tracking: bool = False,
    ):
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
        if opencode:
            provider, model, hook_source = "minimax", "MiniMax-M3", "opencode"
        elif hook_source == "claude":
            provider, model = "anthropic", "claude-fable-5"
        else:
            provider, model = "openai", "gpt-5.6-sol"
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
            "provider": provider,
            "model": model,
            "hook_source": hook_source,
        }
        if variant:
            event["variant"] = variant
        if tracking:
            event["tracking_protocol"] = "control-v1"
        append_event(self.runs / "fleet-frontier.ledger.jsonl", event)
        return event, lease

    def test_control_authorization_prevents_raw_cmux_submit_from_binding_tracked_run(
        self,
    ) -> None:
        run_id = "run-control-authorized"
        state, lease = self.seed_run(run_id, tracking=True)
        raw_submit = self.hook_event("agent.hook.UserPromptSubmit", 101)
        with self.events_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(raw_submit) + "\n")

        # A raw pane submit is visible but has no CONTROL authorization yet.
        self.assertIsNone(
            fleet_frontier.process_event(
                self.runs,
                state,
                raw_submit,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        )
        self.assertIsNone(state.get("session_id"))

        authorization = fleet_frontier.authorize_prompt_submission(
            self.runs,
            feature="frontier",
            instance="agent",
            run_id=run_id,
            workspace_uuid=WORKSPACE_UUID,
            hook_source="codex",
            since="2026-07-12T00:00:00",
            timeout_seconds=0,
        )
        self.assertEqual(authorization["submission_event_id"], raw_submit["id"])
        authorized_state = fleet_frontier.frontier_state(
            self.runs / "fleet-frontier.ledger.jsonl",
            run_id=run_id,
            instance="agent",
        )
        self.assertIsNone(
            fleet_frontier.process_event(
                self.runs,
                authorized_state,
                raw_submit,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        )
        self.assertEqual(authorized_state["session_id"], CODEX_SESSION_ID)

        unrelated_raw = self.hook_event(
            "agent.hook.UserPromptSubmit",
            102,
            occurred_at="2026-07-12T00:00:02+00:00",
        )
        self.assertIsNone(
            fleet_frontier.process_event(
                self.runs,
                authorized_state,
                unrelated_raw,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        )
        self.assertTrue(lease.exists())

    def test_control_authorization_filters_other_surface_before_ambiguity(self) -> None:
        run_id = "run-control-surface-filter"
        self.seed_run(run_id, tracking=True)
        other_session = "codex-other-surface"
        self.write_session(other_session, "00000000-0000-0000-0000-000000000999")
        events = [
            self.hook_event("agent.hook.UserPromptSubmit", 101),
            self.hook_event(
                "agent.hook.UserPromptSubmit",
                102,
                session_id=other_session,
                occurred_at="2026-07-12T00:00:02+00:00",
            ),
        ]
        self.events_log.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )
        authorized = fleet_frontier.authorize_prompt_submission(
            self.runs,
            feature="frontier",
            instance="agent",
            run_id=run_id,
            workspace_uuid=WORKSPACE_UUID,
            hook_source="codex",
            since="2026-07-12T00:00:00",
            timeout_seconds=0,
        )
        self.assertEqual(authorized["submission_event_id"], events[0]["id"])

    @staticmethod
    def hook_event(
        name: str,
        seq: int,
        *,
        boot_id: str = "boot-1",
        phase: str = "received",
        session_id: str | None = None,
        occurred_at: str = "2026-07-12T00:00:01+00:00",
        source: str = "codex",
        final_opencode_stop: bool = False,
    ) -> dict:
        if session_id is None:
            session_id = SESSION_ID if source == "opencode" else CODEX_SESSION_ID
        payload = {"phase": phase, "session_id": session_id}
        event = {
            "type": "event",
            "protocol": "cmux-events",
            "boot_id": boot_id,
            "seq": seq,
            "id": f"{boot_id}-{seq}",
            "name": name,
            "category": "agent",
            "occurred_at": occurred_at,
            "workspace_id": WORKSPACE_UUID,
            "payload": payload,
        }
        if source:
            event["source"] = source
            payload["_source"] = source
        if final_opencode_stop:
            payload.update({"_opencode_request_id": None, "context_length": 100})
        return event

    def test_exact_binding_ignores_old_stop_then_terminalizes_verified_sentinel(
        self,
    ) -> None:
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
            "codex_turn_evidence",
            return_value=(
                f"answer\nFLEET_RESULT:{run_id}:DONE",
                "openai",
                "gpt-5.6-sol",
            ),
        ):
            terminal = fleet_frontier.process_event(
                self.runs,
                state,
                stop,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertEqual(terminal["status"], "succeeded")
        self.assertEqual(terminal["session_id"], CODEX_SESSION_ID)
        result_file = Path(terminal["result_file"])
        self.assertEqual(
            result_file.read_text(encoding="utf-8"),
            f"answer\nFLEET_RESULT:{run_id}:DONE",
        )
        self.assertFalse(lease.exists())

    def test_event_snapshot_subscribes_to_session_end(self) -> None:
        ack = {
            "type": "ack",
            "protocol": "cmux-events",
            "version": 1,
            "boot_id": "boot-1",
            "replay_count": 0,
            "resume": {
                "gap": False,
                "oldest_seq": 1,
                "latest_seq": 10,
                "next_seq": 11,
            },
        }
        proc = mock.Mock()
        proc.stdout.readline.return_value = json.dumps(ack)
        with (
            mock.patch.object(
                fleet_frontier.subprocess, "Popen", return_value=proc
            ) as popen,
            mock.patch.object(
                fleet_frontier.select, "select", return_value=([proc.stdout], [], [])
            ),
        ):
            self.assertEqual(fleet_frontier.event_ack(), ack)
        command = popen.call_args.args[0]
        self.assertIn("agent.hook.SessionEnd", command)

    def test_event_snapshot_rejects_ambiguous_json_without_effects(self) -> None:
        ack = {
            "type": "ack",
            "protocol": "cmux-events",
            "version": 1,
            "boot_id": "boot-1",
            "replay_count": 0,
            "resume": {
                "gap": False,
                "oldest_seq": 1,
                "latest_seq": 10,
                "next_seq": 11,
            },
        }
        documents = self.ambiguous_object_documents(
            json.dumps(ack, separators=(",", ":")).encode("utf-8")
        )

        for name, raw in documents.items():
            with self.subTest(case=name):
                proc = mock.Mock()
                proc.stdout.readline.return_value = raw
                before = self.filesystem_snapshot()
                with (
                    mock.patch.object(
                        fleet_frontier.subprocess, "Popen", return_value=proc
                    ),
                    mock.patch.object(
                        fleet_frontier.select,
                        "select",
                        return_value=([proc.stdout], [], []),
                    ),
                ):
                    with self.assertRaisesRegex(
                        fleet_frontier.FrontierError,
                        "^cmux event snapshot returned invalid JSON$",
                    ):
                        fleet_frontier.event_ack()
                self.assertEqual(self.filesystem_snapshot(), before)
                proc.kill.assert_called_once_with()

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

    def test_structured_sentinel_requires_one_final_exact_run(self) -> None:
        self.assertEqual(
            fleet_frontier.structured_sentinel_status(
                "answer\nFLEET_RESULT:run-1:DONE", "run-1"
            ),
            ("succeeded", "frontier_sentinel_verified"),
        )
        self.assertEqual(
            fleet_frontier.structured_sentinel_status(
                "FLEET_RESULT:run-1:DONE\ntrailing", "run-1"
            ),
            ("indeterminate", "frontier_sentinel_not_final"),
        )

    def test_hook_session_lookup_requires_exact_source_prefix_and_file(self) -> None:
        shared = "shared-session"
        self.write_session(f"codex-{shared}", "codex-surface")
        self.write_session(f"claude-{shared}", "claude-surface")
        self.assertEqual(
            fleet_frontier.session_record(f"codex-{shared}", hook_source="codex")[
                "surfaceId"
            ],
            "codex-surface",
        )
        self.assertEqual(
            fleet_frontier.session_record(f"claude-{shared}", hook_source="claude")[
                "surfaceId"
            ],
            "claude-surface",
        )
        self.assertIsNone(
            fleet_frontier.session_record(f"claude-{shared}", hook_source="codex")
        )
        self.assertIsNone(
            fleet_frontier.session_record(f"codex-{shared}", hook_source="future")
        )
        codex_file = self.hooks / "codex-hook-sessions.json"
        data = json.loads(codex_file.read_text())
        data["sessions"][shared]["sessionId"] = "different-session"
        codex_file.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(
            fleet_frontier.session_record(f"codex-{shared}", hook_source="codex")
        )

    def test_hook_session_optional_json_rejects_ambiguity_without_effects(self) -> None:
        session_file = self.hooks / "codex-hook-sessions.json"
        valid = session_file.read_bytes()

        for name, raw in self.ambiguous_object_documents(valid).items():
            with self.subTest(case=name):
                session_file.write_bytes(raw)
                before = self.filesystem_snapshot()
                self.assertIsNone(
                    fleet_frontier.session_record(CODEX_SESSION_ID, hook_source="codex")
                )
                self.assertEqual(self.filesystem_snapshot(), before)

        session_file.write_bytes(valid)

    def test_transcript_json_rejects_ambiguity_without_effects(self) -> None:
        record = fleet_frontier.session_record(CODEX_SESSION_ID, hook_source="codex")
        transcript = Path(record["transcriptPath"])
        valid = json.dumps(
            {"type": "probe", "payload": {"status": "visible"}},
            separators=(",", ":"),
        ).encode("utf-8")

        for name, raw in self.ambiguous_object_documents(valid).items():
            with self.subTest(case=name):
                transcript.write_bytes(raw + b"\n")
                before = self.filesystem_snapshot()
                with self.assertRaisesRegex(
                    fleet_frontier.FrontierError,
                    "^codex transcript contains invalid JSON$",
                ):
                    fleet_frontier._transcript_rows(CODEX_SESSION_ID, "codex")
                self.assertEqual(self.filesystem_snapshot(), before)

    def test_transcript_json_keeps_explicit_blank_row_semantics(self) -> None:
        record = fleet_frontier.session_record(CODEX_SESSION_ID, hook_source="codex")
        transcript = Path(record["transcriptPath"])
        row = {"type": "probe", "payload": {"status": "visible"}}
        transcript.write_bytes(b"\n  \n" + json.dumps(row).encode("utf-8") + b"\n\t\n")

        self.assertEqual(
            fleet_frontier._transcript_rows(CODEX_SESSION_ID, "codex"),
            [row],
        )

    def test_codex_turn_evidence_binds_transcript_response_and_identity(self) -> None:
        run_id = "run-codex-transcript"
        raw = CODEX_SESSION_ID.removeprefix("codex-")
        self.write_transcript(
            CODEX_SESSION_ID,
            [
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-12T00:00:00Z",
                    "payload": {
                        "id": raw,
                        "model_provider": "openai",
                    },
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
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"task FLEET_RESULT:{run_id}:<STATUS>",
                            }
                        ],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-12T00:00:02Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": f"answer\nFLEET_RESULT:{run_id}:DONE",
                            }
                        ],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-12T00:00:04Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "next"}],
                    },
                },
            ],
        )
        self.assertEqual(
            fleet_frontier.codex_turn_evidence(
                CODEX_SESSION_ID, run_id, "2026-07-12T00:00:03Z"
            ),
            (
                f"answer\nFLEET_RESULT:{run_id}:DONE",
                "openai",
                "gpt-5.6-sol",
            ),
        )

    def test_claude_turn_evidence_binds_final_end_turn_and_model(self) -> None:
        run_id = "run-claude-transcript"
        raw = CLAUDE_SESSION_ID.removeprefix("claude-")
        self.write_transcript(
            CLAUDE_SESSION_ID,
            [
                {
                    "type": "user",
                    "sessionId": raw,
                    "isSidechain": False,
                    "origin": {"kind": "human"},
                    "promptSource": "typed",
                    "timestamp": "2026-07-12T00:00:01Z",
                    "message": {
                        "role": "user",
                        "content": f"task FLEET_RESULT:{run_id}:<STATUS>",
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": raw,
                    "isSidechain": False,
                    "timestamp": "2026-07-12T00:00:02Z",
                    "message": {
                        "role": "assistant",
                        "model": "claude-fable-5",
                        "stop_reason": "end_turn",
                        "content": [{"type": "thinking", "thinking": "hidden"}],
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": raw,
                    "isSidechain": False,
                    "timestamp": "2026-07-12T00:00:02.100Z",
                    "message": {
                        "role": "assistant",
                        "model": "claude-fable-5",
                        "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "name": "Read"}],
                    },
                },
                {
                    "type": "user",
                    "sessionId": raw,
                    "isSidechain": False,
                    "timestamp": "2026-07-12T00:00:02.200Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "content": "ok"}],
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": raw,
                    "isSidechain": True,
                    "timestamp": "2026-07-12T00:00:02.400Z",
                    "message": {
                        "role": "assistant",
                        "model": "wrong-sidechain-model",
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "sidechain"}],
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": raw,
                    "isSidechain": False,
                    "timestamp": "2026-07-12T00:00:02.500Z",
                    "message": {
                        "role": "assistant",
                        "model": "claude-fable-5",
                        "stop_reason": "end_turn",
                        "content": [
                            {
                                "type": "text",
                                "text": f"answer\nFLEET_RESULT:{run_id}:DONE",
                            }
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": raw,
                    "isSidechain": False,
                    "timestamp": "2026-07-12T00:00:03.500Z",
                    "message": {
                        "role": "assistant",
                        "model": "claude-fable-5",
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "too late"}],
                    },
                },
                {
                    "type": "user",
                    "sessionId": raw,
                    "isSidechain": False,
                    "origin": {"kind": "human"},
                    "promptSource": "typed",
                    "timestamp": "2026-07-12T00:00:04Z",
                    "message": {"role": "user", "content": "next"},
                },
            ],
        )
        self.assertEqual(
            fleet_frontier.claude_turn_evidence(
                CLAUDE_SESSION_ID, run_id, "2026-07-12T00:00:03Z"
            ),
            (
                f"answer\nFLEET_RESULT:{run_id}:DONE",
                "anthropic",
                "claude-fable-5",
            ),
        )

    def test_codex_and_claude_stops_use_structured_evidence_not_screen(self) -> None:
        cases = (
            (
                "codex",
                CODEX_SESSION_ID,
                "openai",
                "gpt-5.6-sol",
                "codex_turn_evidence",
            ),
            (
                "claude",
                CLAUDE_SESSION_ID,
                "anthropic",
                "claude-fable-5",
                "claude_turn_evidence",
            ),
        )
        for index, (source, session_id, provider, model, reader) in enumerate(cases):
            with self.subTest(source=source):
                run_id = f"run-{source}-structured"
                state, lease = self.seed_run(run_id, hook_source=source)
                binding = self.hook_event(
                    "agent.hook.UserPromptSubmit",
                    200 + index * 10,
                    source=source,
                    session_id=session_id,
                )
                fleet_frontier.process_event(
                    self.runs,
                    state,
                    binding,
                    workspace_ref="workspace:1",
                    surface_ref="surface:1",
                )
                stop = self.hook_event(
                    "agent.hook.Stop",
                    201 + index * 10,
                    phase="completed",
                    source=source,
                    session_id=session_id,
                    occurred_at="2026-07-12T00:00:03Z",
                )
                with (
                    mock.patch.object(
                        fleet_frontier,
                        reader,
                        return_value=(
                            f"answer\nFLEET_RESULT:{run_id}:DONE",
                            provider,
                            model,
                        ),
                    ),
                    mock.patch.object(fleet_frontier, "read_screen") as read_screen,
                ):
                    terminal = fleet_frontier.process_event(
                        self.runs,
                        state,
                        stop,
                        workspace_ref="workspace:1",
                        surface_ref="surface:1",
                    )
                self.assertEqual(terminal["status"], "succeeded")
                self.assertEqual(
                    Path(terminal["result_file"]).read_text(encoding="utf-8"),
                    f"answer\nFLEET_RESULT:{run_id}:DONE",
                )
                self.assertFalse(lease.exists())
                read_screen.assert_not_called()

    def test_claude_process_event_accepts_tool_using_main_turn(self) -> None:
        run_id = "run-claude-tool-turn"
        raw = CLAUDE_SESSION_ID.removeprefix("claude-")
        self.write_transcript(
            CLAUDE_SESSION_ID,
            [
                {
                    "type": "user",
                    "sessionId": raw,
                    "isSidechain": False,
                    "origin": {"kind": "human"},
                    "promptSource": "typed",
                    "timestamp": "2026-07-12T00:00:01Z",
                    "message": {
                        "role": "user",
                        "content": f"task FLEET_RESULT:{run_id}:<STATUS>",
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": raw,
                    "isSidechain": False,
                    "timestamp": "2026-07-12T00:00:01.500Z",
                    "message": {
                        "role": "assistant",
                        "model": "claude-fable-5",
                        "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "name": "Read"}],
                    },
                },
                {
                    "type": "user",
                    "sessionId": raw,
                    "isSidechain": False,
                    "timestamp": "2026-07-12T00:00:01.750Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "content": "ok"}],
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": raw,
                    "isSidechain": False,
                    "timestamp": "2026-07-12T00:00:02Z",
                    "message": {
                        "role": "assistant",
                        "model": "claude-fable-5",
                        "stop_reason": "end_turn",
                        "content": [
                            {
                                "type": "text",
                                "text": f"answer\nFLEET_RESULT:{run_id}:DONE",
                            }
                        ],
                    },
                },
            ],
        )
        state, lease = self.seed_run(run_id, hook_source="claude")
        fleet_frontier.process_event(
            self.runs,
            state,
            self.hook_event(
                "agent.hook.UserPromptSubmit",
                501,
                source="claude",
                session_id=CLAUDE_SESSION_ID,
            ),
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        with mock.patch.object(fleet_frontier, "read_screen") as read_screen:
            terminal = fleet_frontier.process_event(
                self.runs,
                state,
                self.hook_event(
                    "agent.hook.Stop",
                    502,
                    phase="completed",
                    source="claude",
                    session_id=CLAUDE_SESSION_ID,
                    occurred_at="2026-07-12T00:00:03Z",
                ),
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertEqual(terminal["status"], "succeeded")
        self.assertFalse(lease.exists())
        read_screen.assert_not_called()

    def test_claude_duplicate_run_marker_is_ambiguous(self) -> None:
        run_id = "run-claude-duplicate"
        raw = CLAUDE_SESSION_ID.removeprefix("claude-")
        prompt = {
            "type": "user",
            "sessionId": raw,
            "isSidechain": False,
            "origin": {"kind": "human"},
            "promptSource": "typed",
            "message": {
                "role": "user",
                "content": f"task FLEET_RESULT:{run_id}:<STATUS>",
            },
        }
        self.write_transcript(
            CLAUDE_SESSION_ID,
            [
                {**prompt, "timestamp": "2026-07-12T00:00:01Z"},
                {**prompt, "timestamp": "2026-07-12T00:00:02Z"},
            ],
        )
        with self.assertRaisesRegex(fleet_frontier.FrontierError, "ambiguous"):
            fleet_frontier.claude_turn_evidence(
                CLAUDE_SESSION_ID, run_id, "2026-07-12T00:00:03Z"
            )

    def test_codex_and_claude_identity_mismatch_is_indeterminate(self) -> None:
        cases = (
            ("codex", CODEX_SESSION_ID, "codex_turn_evidence"),
            ("claude", CLAUDE_SESSION_ID, "claude_turn_evidence"),
        )
        for index, (source, session_id, reader) in enumerate(cases):
            with self.subTest(source=source):
                run_id = f"run-{source}-identity"
                state, lease = self.seed_run(run_id, hook_source=source)
                fleet_frontier.process_event(
                    self.runs,
                    state,
                    self.hook_event(
                        "agent.hook.UserPromptSubmit",
                        300 + index * 10,
                        source=source,
                        session_id=session_id,
                    ),
                    workspace_ref="workspace:1",
                    surface_ref="surface:1",
                )
                stop = self.hook_event(
                    "agent.hook.Stop",
                    301 + index * 10,
                    phase="completed",
                    source=source,
                    session_id=session_id,
                )
                with mock.patch.object(
                    fleet_frontier,
                    reader,
                    return_value=(
                        f"answer\nFLEET_RESULT:{run_id}:DONE",
                        "wrong-provider",
                        "wrong-model",
                    ),
                ):
                    terminal = fleet_frontier.process_event(
                        self.runs,
                        state,
                        stop,
                        workspace_ref="workspace:1",
                        surface_ref="surface:1",
                    )
                self.assertEqual(terminal["status"], "indeterminate")
                self.assertEqual(
                    terminal["reason"], f"frontier_{source}_identity_mismatch"
                )
                self.assertTrue(terminal["lease_retained"])
                self.assertTrue(lease.exists())
                fleet_frontier.abandon_run(
                    self.runs,
                    feature="frontier",
                    instance="agent",
                    run_id=run_id,
                    reason="test_cleanup",
                )

    def test_claude_unavailable_transcript_is_indeterminate(self) -> None:
        run_id = "run-claude-no-transcript"
        state, lease = self.seed_run(run_id, hook_source="claude")
        fleet_frontier.process_event(
            self.runs,
            state,
            self.hook_event(
                "agent.hook.UserPromptSubmit",
                401,
                source="claude",
                session_id=CLAUDE_SESSION_ID,
            ),
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        stop = self.hook_event(
            "agent.hook.Stop",
            402,
            phase="completed",
            source="claude",
            session_id=CLAUDE_SESSION_ID,
        )
        with mock.patch.object(
            fleet_frontier,
            "claude_turn_evidence",
            side_effect=fleet_frontier.FrontierError("missing transcript"),
        ):
            terminal = fleet_frontier.process_event(
                self.runs,
                state,
                stop,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertEqual(terminal["status"], "indeterminate")
        self.assertEqual(terminal["reason"], "frontier_claude_evidence_unavailable")
        self.assertTrue(terminal["lease_retained"])
        self.assertTrue(lease.exists())

    def test_claude_session_end_without_stop_is_indeterminate_and_retains_lease(
        self,
    ) -> None:
        run_id = "run-claude-session-end"
        state, lease = self.seed_run(run_id, hook_source="claude")
        unbound = self.hook_event(
            "agent.hook.SessionEnd",
            401,
            phase="completed",
            source="claude",
            session_id=CLAUDE_SESSION_ID,
        )
        self.assertIsNone(
            fleet_frontier.process_event(
                self.runs,
                state,
                unbound,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        )
        fleet_frontier.process_event(
            self.runs,
            state,
            self.hook_event(
                "agent.hook.UserPromptSubmit",
                402,
                source="claude",
                session_id=CLAUDE_SESSION_ID,
            ),
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        received = self.hook_event(
            "agent.hook.SessionEnd",
            403,
            phase="received",
            source="claude",
            session_id=CLAUDE_SESSION_ID,
        )
        wrong_session = self.hook_event(
            "agent.hook.SessionEnd",
            404,
            phase="completed",
            source="claude",
            session_id="claude-other-session",
        )
        wrong_source = self.hook_event(
            "agent.hook.SessionEnd",
            405,
            phase="completed",
            source="codex",
            session_id=CLAUDE_SESSION_ID,
        )
        wrong_workspace = self.hook_event(
            "agent.hook.SessionEnd",
            406,
            phase="completed",
            source="claude",
            session_id=CLAUDE_SESSION_ID,
        )
        wrong_workspace["workspace_id"] = "00000000-0000-0000-0000-000000000999"
        for event in (received, wrong_session, wrong_source, wrong_workspace):
            self.assertIsNone(
                fleet_frontier.process_event(
                    self.runs,
                    state,
                    event,
                    workspace_ref="workspace:1",
                    surface_ref="surface:1",
                )
            )
        (self.hooks / "claude-hook-sessions.json").unlink()
        session_end = self.hook_event(
            "agent.hook.SessionEnd",
            407,
            phase="completed",
            source="claude",
            session_id=CLAUDE_SESSION_ID,
        )
        with mock.patch.object(fleet_frontier, "claude_turn_evidence") as evidence:
            terminal = fleet_frontier.process_event(
                self.runs,
                state,
                session_end,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertEqual(terminal["status"], "indeterminate")
        self.assertEqual(terminal["reason"], "frontier_session_ended_without_stop")
        self.assertEqual(terminal["completion_event_id"], session_end["id"])
        self.assertTrue(terminal["lease_retained"])
        self.assertTrue(lease.exists())
        evidence.assert_not_called()

    def test_claude_stop_then_session_end_keeps_verified_terminal_result(self) -> None:
        run_id = "run-claude-stop-before-session-end"
        state, lease = self.seed_run(run_id, hook_source="claude")
        fleet_frontier.process_event(
            self.runs,
            state,
            self.hook_event(
                "agent.hook.UserPromptSubmit",
                501,
                source="claude",
                session_id=CLAUDE_SESSION_ID,
            ),
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        stop = self.hook_event(
            "agent.hook.Stop",
            502,
            phase="completed",
            source="claude",
            session_id=CLAUDE_SESSION_ID,
        )
        with mock.patch.object(
            fleet_frontier,
            "claude_turn_evidence",
            return_value=(
                f"answer\nFLEET_RESULT:{run_id}:DONE",
                "anthropic",
                "claude-fable-5",
            ),
        ):
            succeeded = fleet_frontier.process_event(
                self.runs,
                state,
                stop,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        session_end = self.hook_event(
            "agent.hook.SessionEnd",
            503,
            phase="completed",
            source="claude",
            session_id=CLAUDE_SESSION_ID,
        )
        terminal = fleet_frontier.process_event(
            self.runs,
            succeeded,
            session_end,
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        self.assertEqual(terminal["status"], "succeeded")
        self.assertEqual(terminal["completion_event_id"], stop["id"])
        self.assertFalse(lease.exists())

    def test_transcript_evidence_retries_bounded_visibility_race(self) -> None:
        reader = mock.Mock(
            side_effect=[
                fleet_frontier.FrontierError("not flushed"),
                ("answer", "anthropic", "claude-fable-5"),
            ]
        )
        with mock.patch.object(fleet_frontier.time, "sleep") as sleep:
            evidence = fleet_frontier.transcript_turn_evidence(
                reader,
                CLAUDE_SESSION_ID,
                "run-retry",
                "2026-07-12T00:00:03Z",
            )
        self.assertEqual(evidence, ("answer", "anthropic", "claude-fable-5"))
        self.assertEqual(reader.call_count, 2)
        sleep.assert_called_once_with(fleet_frontier.TRANSCRIPT_EVIDENCE_RETRY_SECONDS)

    def test_opencode_ignores_intermediate_stops_and_uses_structured_final_response(
        self,
    ) -> None:
        statuses = {
            "DONE": "succeeded",
            "BLOCKED": "blocked",
            "FAILED": "failed",
        }
        for index, (sentinel, expected) in enumerate(statuses.items(), start=1):
            with self.subTest(sentinel=sentinel):
                run_id = f"run-opencode-{sentinel.lower()}"
                state, lease = self.seed_run(run_id, opencode=True)
                binding = self.hook_event(
                    "agent.hook.UserPromptSubmit",
                    100 + index * 10,
                    source="opencode",
                )
                fleet_frontier.process_event(
                    self.runs,
                    state,
                    binding,
                    workspace_ref="workspace:1",
                    surface_ref="surface:1",
                )
                intermediate = self.hook_event(
                    "agent.hook.Stop",
                    101 + index * 10,
                    phase="completed",
                    source="opencode",
                )
                self.assertIsNone(
                    fleet_frontier.process_event(
                        self.runs,
                        state,
                        intermediate,
                        workspace_ref="workspace:1",
                        surface_ref="surface:1",
                    )
                )
                self.assertTrue(lease.exists())
                final = self.hook_event(
                    "agent.hook.Stop",
                    102 + index * 10,
                    phase="completed",
                    source="opencode",
                    final_opencode_stop=True,
                )
                with (
                    mock.patch.object(
                        fleet_frontier,
                        "opencode_turn_evidence",
                        return_value=(
                            f"answer\nFLEET_RESULT:{run_id}:{sentinel}",
                            "minimax",
                            "MiniMax-M3",
                            None,
                        ),
                    ),
                    mock.patch.object(fleet_frontier, "read_screen") as read_screen,
                ):
                    terminal = fleet_frontier.process_event(
                        self.runs,
                        state,
                        final,
                        workspace_ref="workspace:1",
                        surface_ref="surface:1",
                    )
                self.assertEqual(terminal["status"], expected)
                self.assertEqual(terminal["completion_event_id"], final["id"])
                if expected == "succeeded":
                    self.assertEqual(
                        Path(terminal["result_file"]).read_text(encoding="utf-8"),
                        f"answer\nFLEET_RESULT:{run_id}:{sentinel}",
                    )
                else:
                    self.assertNotIn("result_file", terminal)
                self.assertFalse(lease.exists())
                read_screen.assert_not_called()

    def test_frontier_result_persistence_failure_is_indeterminate(self) -> None:
        run_id = "run-result-write-failure"
        state, lease = self.seed_run(run_id)
        fleet_frontier.process_event(
            self.runs,
            state,
            self.hook_event("agent.hook.UserPromptSubmit", 101),
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        stop = self.hook_event("agent.hook.Stop", 102, phase="completed")
        with (
            mock.patch.object(
                fleet_frontier,
                "codex_turn_evidence",
                return_value=(
                    f"answer\nFLEET_RESULT:{run_id}:DONE",
                    "openai",
                    "gpt-5.6-sol",
                ),
            ),
            mock.patch.object(
                fleet_frontier,
                "persist_frontier_result",
                side_effect=OSError("disk unavailable"),
            ),
        ):
            terminal = fleet_frontier.process_event(
                self.runs,
                state,
                stop,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertEqual(terminal["status"], "indeterminate")
        self.assertEqual(terminal["reason"], "frontier_result_persistence_failed")
        self.assertTrue(terminal["lease_retained"])
        self.assertNotIn("result_file", terminal)
        self.assertTrue(lease.exists())

    def test_frontier_result_rejects_symlinked_result_ancestor(self) -> None:
        outside = self.tmp / "outside-results"
        outside.mkdir(mode=0o700)
        (self.runs / "results").symlink_to(outside, target_is_directory=True)
        state = {"feature": "frontier", "run_id": "run-symlink-ancestor"}

        with self.assertRaisesRegex(
            fleet_frontier.FrontierError, "unsafe frontier result path"
        ):
            fleet_frontier.persist_frontier_result(
                self.runs, state, "must remain inside runs"
            )
        self.assertFalse((outside / "frontier" / "run-symlink-ancestor.txt").exists())

    @unittest.skipUnless(sys.platform == "darwin", "macOS physical root alias")
    def test_frontier_result_accepts_trusted_var_root_alias(self) -> None:
        if self.runs == self.runs.resolve():
            self.skipTest("temporary directory does not expose /var alias")
        state = {"feature": "frontier", "run_id": "run-var-alias"}
        fleet_frontier.persist_frontier_result(self.runs, state, "alias evidence")
        recorded = self.runs / "results" / "frontier" / "run-var-alias.txt"

        self.assertNotEqual(recorded.parents[2], recorded.parents[2].resolve())
        self.assertEqual(
            fleet_frontier.read_frontier_result(
                self.runs,
                feature="frontier",
                run_id="run-var-alias",
                recorded_path=recorded,
            ),
            b"alias evidence",
        )

    def test_opencode_identity_mismatch_is_indeterminate_and_retains_lease(
        self,
    ) -> None:
        run_id = "run-opencode-mismatch"
        state, lease = self.seed_run(run_id, opencode=True)
        fleet_frontier.process_event(
            self.runs,
            state,
            self.hook_event("agent.hook.UserPromptSubmit", 101, source="opencode"),
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        final = self.hook_event(
            "agent.hook.Stop",
            102,
            phase="completed",
            source="opencode",
            final_opencode_stop=True,
        )
        with mock.patch.object(
            fleet_frontier,
            "opencode_turn_evidence",
            return_value=(
                f"answer\nFLEET_RESULT:{run_id}:DONE",
                "zai",
                "glm-5.2",
                None,
            ),
        ):
            terminal = fleet_frontier.process_event(
                self.runs,
                state,
                final,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertEqual(terminal["status"], "indeterminate")
        self.assertEqual(terminal["reason"], "frontier_opencode_identity_mismatch")
        self.assertTrue(terminal["lease_retained"])
        self.assertTrue(lease.exists())

    def test_opencode_variant_mismatch_is_indeterminate_and_retains_lease(self) -> None:
        run_id = "run-opencode-variant-mismatch"
        state, lease = self.seed_run(run_id, opencode=True, variant="none")
        fleet_frontier.process_event(
            self.runs,
            state,
            self.hook_event("agent.hook.UserPromptSubmit", 101, source="opencode"),
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        final = self.hook_event(
            "agent.hook.Stop",
            102,
            phase="completed",
            source="opencode",
            final_opencode_stop=True,
        )
        with mock.patch.object(
            fleet_frontier,
            "opencode_turn_evidence",
            return_value=(
                f"answer\nFLEET_RESULT:{run_id}:DONE",
                "minimax",
                "MiniMax-M3",
                "thinking",
            ),
        ):
            terminal = fleet_frontier.process_event(
                self.runs,
                state,
                final,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertEqual(terminal["status"], "indeterminate")
        self.assertEqual(terminal["reason"], "frontier_opencode_variant_mismatch")
        self.assertTrue(terminal["lease_retained"])
        self.assertTrue(lease.exists())

    def test_opencode_missing_required_variant_is_indeterminate(self) -> None:
        run_id = "run-opencode-variant-missing"
        state, lease = self.seed_run(run_id, opencode=True, variant="none")
        fleet_frontier.process_event(
            self.runs,
            state,
            self.hook_event("agent.hook.UserPromptSubmit", 101, source="opencode"),
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        final = self.hook_event(
            "agent.hook.Stop",
            102,
            phase="completed",
            source="opencode",
            final_opencode_stop=True,
        )
        with mock.patch.object(
            fleet_frontier,
            "opencode_turn_evidence",
            return_value=(
                f"answer\nFLEET_RESULT:{run_id}:DONE",
                "minimax",
                "MiniMax-M3",
                None,
            ),
        ):
            terminal = fleet_frontier.process_event(
                self.runs,
                state,
                final,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertEqual(terminal["status"], "indeterminate")
        self.assertEqual(terminal["reason"], "frontier_opencode_variant_mismatch")
        self.assertTrue(terminal["lease_retained"])
        self.assertTrue(lease.exists())

    def test_opencode_unverifiable_final_evidence_is_indeterminate(self) -> None:
        run_id = "run-opencode-unverifiable"
        state, lease = self.seed_run(run_id, opencode=True)
        fleet_frontier.process_event(
            self.runs,
            state,
            self.hook_event("agent.hook.UserPromptSubmit", 101, source="opencode"),
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        final = self.hook_event(
            "agent.hook.Stop",
            102,
            phase="completed",
            source="opencode",
            final_opencode_stop=True,
        )
        with mock.patch.object(
            fleet_frontier,
            "opencode_turn_evidence",
            side_effect=fleet_frontier.FrontierError("invalid database evidence"),
        ):
            terminal = fleet_frontier.process_event(
                self.runs,
                state,
                final,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertEqual(terminal["status"], "indeterminate")
        self.assertEqual(terminal["reason"], "frontier_opencode_evidence_unavailable")
        self.assertTrue(lease.exists())

    def test_opencode_retries_bounded_database_visibility_race(self) -> None:
        run_id = "run-opencode-db-retry"
        state, lease = self.seed_run(run_id, opencode=True)
        fleet_frontier.process_event(
            self.runs,
            state,
            self.hook_event("agent.hook.UserPromptSubmit", 101, source="opencode"),
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        final = self.hook_event(
            "agent.hook.Stop",
            102,
            phase="completed",
            source="opencode",
            final_opencode_stop=True,
        )
        evidence = (
            f"answer\nFLEET_RESULT:{run_id}:DONE",
            "minimax",
            "MiniMax-M3",
            None,
        )
        with (
            mock.patch.object(
                fleet_frontier,
                "opencode_turn_evidence",
                side_effect=[
                    fleet_frontier.FrontierError("database not flushed"),
                    evidence,
                ],
            ) as reader,
            mock.patch.object(fleet_frontier.time, "sleep") as sleep,
        ):
            terminal = fleet_frontier.process_event(
                self.runs,
                state,
                final,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertEqual(terminal["status"], "succeeded")
        self.assertEqual(reader.call_count, 2)
        sleep.assert_called_once_with(fleet_frontier.TRANSCRIPT_EVIDENCE_RETRY_SECONDS)
        self.assertFalse(lease.exists())

    def test_opencode_turn_evidence_binds_full_final_message_before_stop(self) -> None:
        run_id = "run-db-evidence"
        full_response = f"{'x' * 2000}\nFLEET_RESULT:{run_id}:DONE"

        def row(
            message_id: str,
            created: int,
            role: str,
            text: str,
            *,
            completed: int | None = None,
            provider: str | None = None,
            model: str | None = None,
            variant: str | None = None,
        ) -> dict:
            data = {"role": role, "time": {"created": created}}
            if completed is not None:
                data["time"]["completed"] = completed
            if provider is not None:
                data["providerID"] = provider
            if model is not None:
                data["modelID"] = model
            if variant is not None:
                data["variant"] = variant
            return {
                "message_id": message_id,
                "message_created": created,
                "message_data": json.dumps(data),
                "part_id": f"part-{message_id}",
                "part_created": created + 1,
                "part_data": json.dumps({"type": "text", "text": text}),
            }

        rows = [
            row(
                "user-current",
                1000,
                "user",
                f"task FLEET_RESULT:{run_id}:<STATUS>",
            ),
            row(
                "assistant-tool-step",
                1100,
                "assistant",
                "intermediate",
                completed=1200,
                provider="minimax",
                model="MiniMax-M3",
            ),
            row(
                "assistant-final",
                1300,
                "assistant",
                full_response,
                completed=1400,
                provider="minimax",
                model="MiniMax-M3",
                variant="none",
            ),
            row("user-next", 1500, "user", "another turn"),
            row(
                "assistant-next",
                1600,
                "assistant",
                "must not bind",
                completed=1700,
                provider="zai",
                model="glm-5.2",
            ),
        ]
        result = subprocess.CompletedProcess(
            ["opencode", "db"], 0, stdout=json.dumps(rows), stderr=""
        )
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "opencode-state"
            data_home = state_root / SURFACE_UUID / "data"
            data_home.mkdir(parents=True)
            with (
                mock.patch.object(fleet_frontier, "OPENCODE_STATE_ROOT", state_root),
                mock.patch.object(
                    fleet_frontier.subprocess, "run", return_value=result
                ) as query,
            ):
                self.assertEqual(
                    fleet_frontier.opencode_turn_evidence(
                        SESSION_ID,
                        run_id,
                        "2026-07-12T00:00:03+00:00",
                        SURFACE_UUID,
                    ),
                    (
                        full_response,
                        "minimax",
                        "MiniMax-M3",
                        "none",
                    ),
                )
            self.assertEqual(
                query.call_args.kwargs["env"]["XDG_DATA_HOME"], str(data_home)
            )
            self.assertIn("--pure", query.call_args.args[0])

    def test_opencode_provider_json_rejects_ambiguity_without_effects(self) -> None:
        state_root = self.tmp / "opencode-strict-provider-state"
        (state_root / SURFACE_UUID / "data").mkdir(parents=True)
        documents = {
            "duplicate": b'[{"message_id":"one","message_id":"two"}]',
            "nan": b'[{"ambiguous":NaN}]',
            "infinity": b'[{"ambiguous":Infinity}]',
            "overflow": b'[{"ambiguous":1e999}]',
            "bom": b"\xef\xbb\xbf[]",
            "bad_utf8": b'["\xff"]',
            "surrogate": b'[{"ambiguous":"\\ud800"}]',
            "trailing": b"[] trailing",
        }

        for name, raw in documents.items():
            with self.subTest(case=name):
                result = subprocess.CompletedProcess(
                    ["opencode", "db"], 0, stdout=raw, stderr=b""
                )
                before = self.filesystem_snapshot()
                with (
                    mock.patch.object(
                        fleet_frontier, "OPENCODE_STATE_ROOT", state_root
                    ),
                    mock.patch.object(
                        fleet_frontier.subprocess, "run", return_value=result
                    ),
                ):
                    with self.assertRaisesRegex(
                        fleet_frontier.FrontierError,
                        "^OpenCode turn evidence query returned invalid JSON$",
                    ):
                        fleet_frontier.opencode_turn_evidence(
                            SESSION_ID,
                            "run-strict-provider",
                            "2026-07-12T00:00:03+00:00",
                            SURFACE_UUID,
                        )
                self.assertEqual(self.filesystem_snapshot(), before)

    def test_opencode_nested_json_rejects_ambiguity_without_effects(self) -> None:
        state_root = self.tmp / "opencode-strict-nested-state"
        (state_root / SURFACE_UUID / "data").mkdir(parents=True)
        run_id = "run-strict-nested"
        valid_message = {
            "role": "user",
            "time": {"created": 1000},
        }
        valid_part = {
            "type": "text",
            "text": f"task FLEET_RESULT:{run_id}:<STATUS>",
        }
        boundaries = (
            (
                "message_data",
                valid_message,
                "^OpenCode turn evidence has invalid message data$",
            ),
            (
                "part_data",
                valid_part,
                "^OpenCode turn evidence has invalid part data$",
            ),
        )

        for field, valid_value, expected_error in boundaries:
            valid_raw = json.dumps(valid_value, separators=(",", ":")).encode("utf-8")
            for name, raw in self.ambiguous_object_documents(valid_raw).items():
                if name == "bad_utf8":
                    continue
                with self.subTest(field=field, case=name):
                    row = {
                        "message_id": "message-current",
                        "message_created": 1000,
                        "message_data": json.dumps(valid_message),
                        "part_id": "part-current",
                        "part_created": 1001,
                        "part_data": json.dumps(valid_part),
                    }
                    row[field] = raw.decode("utf-8")
                    result = subprocess.CompletedProcess(
                        ["opencode", "db"],
                        0,
                        stdout=json.dumps([row]),
                        stderr="",
                    )
                    before = self.filesystem_snapshot()
                    with (
                        mock.patch.object(
                            fleet_frontier, "OPENCODE_STATE_ROOT", state_root
                        ),
                        mock.patch.object(
                            fleet_frontier.subprocess, "run", return_value=result
                        ),
                    ):
                        with self.assertRaisesRegex(
                            fleet_frontier.FrontierError, expected_error
                        ):
                            fleet_frontier.opencode_turn_evidence(
                                SESSION_ID,
                                run_id,
                                "2026-07-12T00:00:03+00:00",
                                SURFACE_UUID,
                            )
                    self.assertEqual(self.filesystem_snapshot(), before)

    def test_opencode_rejects_wrong_source_and_non_opencode_session_file(self) -> None:
        run_id = "run-opencode-source"
        state, lease = self.seed_run(run_id, opencode=True)
        raw = SESSION_ID.removeprefix("opencode-")
        (self.hooks / "claude-hook-sessions.json").write_text(
            json.dumps(
                {
                    "sessions": {
                        raw: {
                            "workspaceId": WORKSPACE_UUID,
                            "surfaceId": "wrong",
                            "updatedAt": 99,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        wrong_source = self.hook_event(
            "agent.hook.UserPromptSubmit", 101, source="codex"
        )
        self.assertIsNone(
            fleet_frontier.process_event(
                self.runs,
                state,
                wrong_source,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        )
        self.assertIsNone(state.get("session_id"))
        self.assertTrue(
            fleet_frontier.session_matches(
                SESSION_ID,
                workspace_uuid=WORKSPACE_UUID,
                surface_uuid=SURFACE_UUID,
                hook_source="opencode",
            )
        )
        self.assertTrue(lease.exists())

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
        with mock.patch.object(fleet_frontier, "codex_turn_evidence") as evidence:
            terminal = fleet_frontier.process_event(
                self.runs,
                state,
                earlier_stop,
                workspace_ref="workspace:1",
                surface_ref="surface:1",
            )
        self.assertIsNone(terminal)
        evidence.assert_not_called()
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
            self.assertEqual(events[-1]["variant"], "none")
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
                    provider="minimax",
                    model="MiniMax-M3",
                    hook_source="opencode",
                    variant="none",
                )
        events = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual(events[-1]["status"], "abandoned")

    def test_prepare_rejects_reused_durable_run_id_before_append(self) -> None:
        run_id = "00000000-0000-4000-8000-000000000099"
        append_event(
            self.runs / "fleet-frontier.ledger.jsonl",
            {"run_id": run_id, "instance": "agent", "status": "preparing"},
        )
        with self.assertRaisesRegex(fleet_frontier.FrontierError, "already durable"):
            fleet_frontier.prepare_run(
                self.runs,
                feature="frontier",
                instance="agent",
                role="minimax",
                phase="CHALLENGE",
                task="task",
                workspace_uuid=WORKSPACE_UUID,
                surface_uuid=SURFACE_UUID,
                provider="minimax",
                model="MiniMax-M3",
                hook_source="opencode",
                variant="none",
                run_id=run_id,
            )
        events = [
            json.loads(line)
            for line in (self.runs / "fleet-frontier.ledger.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual(len(events), 1)

    def test_prepare_rejects_unsafe_identifiers_before_any_effect(self) -> None:
        traversal_anchor = self.runs / "fleet-x"
        traversal_anchor.mkdir(mode=0o700)
        escaped = self.tmp / "escaped.ledger.jsonl"
        before = tuple(
            sorted(str(path.relative_to(self.runs)) for path in self.runs.rglob("*"))
        )
        valid = {
            "feature": "frontier",
            "instance": "agent",
            "role": "minimax",
            "phase": "CHALLENGE",
            "task": "task",
            "workspace_uuid": WORKSPACE_UUID,
            "surface_uuid": SURFACE_UUID,
            "provider": "minimax",
            "model": "MiniMax-M3",
            "hook_source": "opencode",
            "variant": "none",
        }
        cases = (
            ("feature", "x/../../escaped", "invalid frontier feature"),
            ("instance", "x/../../escaped", "invalid frontier instance"),
            ("role", "../minimax", "invalid frontier role"),
            ("phase", "../CHALLENGE", "invalid frontier phase"),
            ("workspace_uuid", "../workspace", "canonical UUID"),
            ("surface_uuid", "../surface", "canonical UUID"),
            ("run_id", "../run", "canonical UUID"),
            ("provider", "minimax\nforged", "adapter rejected identity"),
            ("model", "MiniMax-M3\nforged", "adapter rejected identity"),
            ("hook_source", "../opencode", "invalid frontier hook_source"),
            ("variant", "none\nforged", "forbidden control character"),
        )

        for field, value, error in cases:
            with self.subTest(field=field):
                supplied = {**valid, field: value}
                with self.assertRaisesRegex(fleet_frontier.FrontierError, error):
                    fleet_frontier.prepare_run(self.runs, **supplied)
                after = tuple(
                    sorted(
                        str(path.relative_to(self.runs))
                        for path in self.runs.rglob("*")
                    )
                )
                self.assertEqual(after, before)
                self.assertFalse(escaped.exists())
                self.assertEqual(list(self.runs.rglob("*.jsonl")), [])

    def test_prepare_persists_prompt_file_for_pointer_dispatch(self) -> None:
        lease = self.runs / "locks" / "frontier.agent.lock"
        with mock.patch.object(fleet_frontier, "acquire_frontier", return_value=lease):
            with mock.patch.object(
                fleet_frontier,
                "event_ack",
                return_value={
                    "boot_id": "boot-test",
                    "resume": {"latest_seq": 42, "oldest_seq": 1},
                },
            ):
                prepared = fleet_frontier.prepare_run(
                    self.runs,
                    feature="frontier",
                    instance="agent",
                    role="minimax",
                    phase="CHALLENGE",
                    task="line one\nline two",
                    workspace_uuid=WORKSPACE_UUID,
                    surface_uuid=SURFACE_UUID,
                    provider="minimax",
                    model="MiniMax-M3",
                    hook_source="opencode",
                    variant="none",
                )
        prompt_path = Path(prepared["prompt_path"])
        self.assertEqual(
            prompt_path,
            self.runs / "prompts" / "frontier" / f"{prepared['run_id']}.txt",
        )
        self.assertEqual(prompt_path.read_text(encoding="utf-8"), prepared["prompt"])
        self.assertIn(f"FLEET_RESULT:{prepared['run_id']}:<STATUS>", prepared["prompt"])
        self.assertEqual(prompt_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(prompt_path.stat().st_uid, os.geteuid())
        self.assertEqual((self.runs / "prompts").stat().st_mode & 0o777, 0o755)
        self.assertEqual(prompt_path.parent.stat().st_mode & 0o777, 0o700)

    def test_prepare_rejects_symlinked_prompt_ancestor_without_external_mutation(
        self,
    ) -> None:
        outside = self.tmp / "outside-prompts"
        outside.mkdir(mode=0o700)
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("untouched", encoding="utf-8")
        (self.runs / "prompts").symlink_to(outside, target_is_directory=True)
        fake_lease = self.runs / "locks" / "frontier.agent.lock"

        with (
            mock.patch.object(
                fleet_frontier, "acquire_frontier", return_value=fake_lease
            ),
            mock.patch.object(fleet_frontier, "release") as release,
            mock.patch.object(fleet_frontier, "event_ack") as event_ack,
        ):
            with self.assertRaisesRegex(
                fleet_frontier.FrontierError, "unsafe frontier prompt path"
            ):
                fleet_frontier.prepare_run(
                    self.runs,
                    feature="frontier",
                    instance="agent",
                    role="minimax",
                    phase="CHALLENGE",
                    task="must remain inside runs",
                    workspace_uuid=WORKSPACE_UUID,
                    surface_uuid=SURFACE_UUID,
                    provider="minimax",
                    model="MiniMax-M3",
                    hook_source="opencode",
                    variant="none",
                )

        event_ack.assert_not_called()
        release.assert_called_once()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")
        self.assertEqual(
            sorted(path.name for path in outside.iterdir()), ["sentinel.txt"]
        )
        ledger = self.runs / "fleet-frontier.ledger.jsonl"
        self.assertEqual(
            [json.loads(row)["status"] for row in ledger.read_text().splitlines()],
            ["preparing", "abandoned"],
        )

    def test_confirm_submit_requires_matching_post_dispatch_submission(self) -> None:
        def seed(
            occurred_at: str, source: str = "opencode", workspace: str = WORKSPACE_UUID
        ) -> None:
            event = {
                "id": f"evt-{occurred_at}-{source}-{workspace[:8]}",
                "type": "event",
                "name": "agent.hook.UserPromptSubmit",
                "source": source,
                "workspace_id": workspace,
                "occurred_at": occurred_at,
                "seq": 7,
                "boot_id": "boot-test",
                "payload": {
                    "phase": "received",
                    "_source": source,
                    "session_id": "ses_x",
                },
            }
            with self.events_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")

        with self.assertRaisesRegex(
            fleet_frontier.FrontierError, "transfer unconfirmed"
        ):
            fleet_frontier.confirm_prompt_submission(
                workspace_uuid=WORKSPACE_UUID,
                hook_source="opencode",
                since="2026-07-13T00:00:00",
                timeout_seconds=0,
            )
        seed("2026-07-12T23:59:59.000Z")
        seed("2026-07-13T00:00:01.000Z", source="codex")
        seed(
            "2026-07-13T00:00:01.000Z", workspace="11111111-1111-1111-1111-111111111111"
        )
        with self.assertRaisesRegex(
            fleet_frontier.FrontierError, "transfer unconfirmed"
        ):
            fleet_frontier.confirm_prompt_submission(
                workspace_uuid=WORKSPACE_UUID,
                hook_source="opencode",
                since="2026-07-13T00:00:00",
                timeout_seconds=0,
            )
        seed("2026-07-13T00:00:02.000Z")
        self.assertEqual(
            fleet_frontier.confirm_prompt_submission(
                workspace_uuid=WORKSPACE_UUID,
                hook_source="opencode",
                since="2026-07-13T00:00:00",
                timeout_seconds=0,
            ),
            1,
        )

    def test_prepare_rejects_legacy_manifest_identity_before_ledger_write(self) -> None:
        with self.assertRaisesRegex(
            fleet_frontier.FrontierError, "provider, model, and hook source"
        ):
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
        self.assertFalse((self.runs / "fleet-frontier.ledger.jsonl").exists())

    def test_prepare_rejects_unsupported_hook_source_before_ledger_write(self) -> None:
        with self.assertRaisesRegex(
            fleet_frontier.FrontierError, "unsupported frontier hook source"
        ):
            fleet_frontier.prepare_run(
                self.runs,
                feature="frontier",
                instance="agent",
                role="future-agent",
                phase="CHALLENGE",
                task="task",
                workspace_uuid=WORKSPACE_UUID,
                surface_uuid=SURFACE_UUID,
                provider="future-provider",
                model="future-model",
                hook_source="future",
            )
        self.assertFalse((self.runs / "fleet-frontier.ledger.jsonl").exists())

    def test_opencode_prompt_transport_is_one_submission_with_exact_logical_prompt(
        self,
    ) -> None:
        task = "first line\nsecond line with ñ"
        run_id = "run-opencode-single-submit"

        prompt = fleet_frontier.prompt_with_contract(
            task, run_id, hook_source="opencode"
        )

        self.assertNotIn("\n", prompt)
        marker = "FDP_PROMPT="
        logical_prompt = json.loads(prompt.rsplit(marker, 1)[1])
        self.assertTrue(
            logical_prompt.startswith(f"{task}\n\nFleet completion protocol:")
        )
        self.assertIn(f"FLEET_RESULT:{run_id}:<STATUS>", logical_prompt)

    def test_non_opencode_prompt_transport_keeps_multiline_contract(self) -> None:
        prompt = fleet_frontier.prompt_with_contract(
            "first line\nsecond line", "run-codex", hook_source="codex"
        )

        self.assertIn("first line\nsecond line\n\nFleet completion protocol:", prompt)

    def test_cmux_audit_json_rejects_ambiguity_without_effects(self) -> None:
        event = self.hook_event("agent.hook.UserPromptSubmit", 101)
        valid = json.dumps(event, separators=(",", ":")).encode("utf-8")

        for name, raw in self.ambiguous_object_documents(valid).items():
            with self.subTest(case=name):
                self.events_log.write_bytes(raw + b"\n")
                before = self.filesystem_snapshot()
                with self.assertRaisesRegex(
                    fleet_frontier.FrontierError,
                    "^cmux audit contains invalid JSON: ",
                ):
                    fleet_frontier.audit_events()
                self.assertEqual(self.filesystem_snapshot(), before)

    def test_cmux_audit_keeps_explicit_blank_row_semantics(self) -> None:
        event = self.hook_event("agent.hook.UserPromptSubmit", 101)
        self.events_log.write_bytes(
            b"\n \t\n" + json.dumps(event).encode("utf-8") + b"\n\t\n"
        )

        self.assertEqual(fleet_frontier.audit_events(), [event])

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
            "codex_turn_evidence",
            return_value=(
                f"FLEET_RESULT:{run_id}:BLOCKED",
                "openai",
                "gpt-5.6-sol",
            ),
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

    def test_cross_boot_audit_recovers_claude_session_end_without_stop(self) -> None:
        run_id = "run-replay-session-end"
        state, lease = self.seed_run(
            run_id, boot_id="boot-old", after_seq=900, hook_source="claude"
        )
        events = [
            self.hook_event(
                "surface.action",
                900,
                boot_id="boot-old",
                phase="completed",
                occurred_at="2026-07-12T00:00:00+00:00",
            ),
            self.hook_event(
                "agent.hook.UserPromptSubmit",
                901,
                boot_id="boot-old",
                phase="received",
                source="claude",
                session_id=CLAUDE_SESSION_ID,
                occurred_at="2026-07-12T00:00:01+00:00",
            ),
            self.hook_event(
                "agent.hook.SessionEnd",
                1,
                boot_id="boot-new",
                phase="completed",
                source="claude",
                session_id=CLAUDE_SESSION_ID,
                occurred_at="2026-07-12T00:00:02+00:00",
            ),
        ]
        self.events_log.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )
        terminal = fleet_frontier.recover_from_audit(
            self.runs,
            state,
            workspace_ref="workspace:1",
            surface_ref="surface:1",
        )
        self.assertEqual(terminal["status"], "indeterminate")
        self.assertEqual(terminal["reason"], "frontier_session_ended_without_stop")
        self.assertEqual(terminal["completion_boot_id"], "boot-new")
        self.assertEqual(terminal["completion_seq"], 1)
        self.assertTrue(terminal["lease_retained"])
        self.assertTrue(lease.exists())

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

    def test_unrecoverable_gap_is_indeterminate_and_retains_lease_until_abandon(
        self,
    ) -> None:
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
        malformed.mkdir(parents=True, mode=0o700)
        (self.runs / "locks").chmod(0o700)
        metadata = malformed / "lease.json"
        metadata.write_text("{not-json", encoding="utf-8")
        metadata.chmod(0o600)
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
