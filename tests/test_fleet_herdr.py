from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_herdr  # noqa: E402
import fleet_json  # noqa: E402
import fleet_artifacts  # noqa: E402
import router_config  # noqa: E402
import workflow_config  # noqa: E402


def completed(
    command: list[str],
    *,
    value: dict | None = None,
    returncode: int = 0,
    error: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        command,
        returncode,
        json.dumps(value) + "\n" if value is not None else "",
        json.dumps(error) + "\n" if error is not None else "",
    )


class FakeHerdr:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.next_pane = 2
        self.agent_states: dict[str, str] = {}
        self.agent_bindings: dict[str, dict] = {}
        self.prompt_error: str | None = None
        self.get_error: str | None = None
        self.get_error_once: str | None = None
        self.raise_on_prompt = False
        self.lose_prompt_ack = False
        self.lose_cancel_ack = False
        self.close_error: str | None = None
        self.wait_timeout = False
        self.cancel_quiescent = True
        self.workspace_label = ""
        self.version = fleet_herdr.HERDR_VERSION
        self.codex_version = "0.159.3"
        self.screen = "OpenAI Codex (v0.159.3)\n› Ask Codex to do anything\n"
        self.start_session_null = False
        self.start_status = "idle"
        self.raise_start_once = False
        self.lazy_session_until_prompt = False
        self.prompt_receipt_session_null = False
        self.prompted_agents: set[str] = set()
        self.raise_get_once = False
        self.get_not_ready_count = 0
        self.workspace_exists = True
        self.shell_cwd = ""

    @staticmethod
    def operation(command: list[str]) -> list[str]:
        if command == ["codex", "--version"]:
            return command
        if command[:3] == ["herdr", "--session", "mission-control-test"]:
            return ["herdr", *command[3:]]
        raise AssertionError(f"missing explicit Herdr session: {command}")

    def agent_info(self, name: str, *, include_session: bool = True) -> dict:
        binding = self.agent_bindings[name]
        include_session = include_session and (
            not self.lazy_session_until_prompt or name in self.prompted_agents
        )
        return {
            "name": name,
            "agent": "codex",
            "agent_status": self.agent_states[name],
            "agent_session": (
                {
                    "agent": "codex",
                    "source": "codex",
                    "kind": "id",
                    "value": f"session-{name}",
                }
                if include_session
                else None
            ),
            "workspace_id": "w-test",
            "tab_id": binding["tab_id"],
            "pane_id": binding["pane_id"],
            "terminal_id": binding["terminal_id"],
            "revision": len(self.calls),
        }

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        self.assert_environment(kwargs)
        command = self.operation(command)
        if command == ["codex", "--version"]:
            return subprocess.CompletedProcess(command, 0, f"codex-cli {self.codex_version}\n", "")
        if command == ["herdr", "--version"]:
            return subprocess.CompletedProcess(command, 0, f"herdr {self.version}\n", "")
        if command[:3] == ["herdr", "workspace", "create"]:
            label = command[command.index("--label") + 1]
            self.workspace_label = label
            self.shell_cwd = command[command.index("--cwd") + 1]
            self.workspace_exists = True
            return completed(
                command,
                value={
                    "result": {
                        "type": "workspace_created",
                        "workspace": {"workspace_id": "w-test", "label": label},
                        "tab": {"workspace_id": "w-test", "tab_id": "w-test:t1"},
                        "root_pane": {
                            "workspace_id": "w-test",
                            "tab_id": "w-test:t1",
                            "pane_id": "w-test:p1",
                            "terminal_id": "term-1",
                        },
                    }
                },
            )
        if command[:3] == ["herdr", "workspace", "get"]:
            if not self.workspace_exists:
                return completed(
                    command,
                    returncode=1,
                    error={"error": {"code": "workspace_not_found"}},
                )
            return completed(
                command,
                value={
                    "result": {
                        "type": "workspace_info",
                        "workspace": {
                            "workspace_id": "w-test",
                            "label": self.workspace_label,
                        },
                    }
                },
            )
        if command[:3] == ["herdr", "pane", "get"]:
            pane_id = command[3]
            name, binding = next(
                (name, binding)
                for name, binding in self.agent_bindings.items()
                if binding["pane_id"] == pane_id
            )
            return completed(
                command,
                value={
                    "result": {
                        "type": "pane_info",
                        "pane": {
                            "workspace_id": "w-test",
                            "tab_id": binding["tab_id"],
                            "pane_id": pane_id,
                            "terminal_id": binding["terminal_id"],
                            "cwd": self.shell_cwd,
                            "agent": name,
                        },
                    }
                },
            )
        if command[:3] == ["herdr", "pane", "process-info"]:
            pane_id = command[command.index("--pane") + 1]
            return completed(
                command,
                value={
                    "result": {
                        "type": "pane_process_info",
                        "process_info": {
                            "pane_id": pane_id,
                            "shell_pid": 87638,
                            "foreground_process_group_id": 87638,
                            "foreground_processes": [
                                {
                                    "pid": 87638,
                                    "name": "zsh",
                                    "argv0": "zsh",
                                    "argv": ["-zsh"],
                                    "cwd": self.shell_cwd,
                                }
                            ],
                        },
                    }
                },
            )
        if command[:3] == ["herdr", "pane", "split"]:
            pane_id = f"w-test:p{self.next_pane}"
            tab_id = f"w-test:t{self.next_pane}"
            terminal_id = f"term-{self.next_pane}"
            self.next_pane += 1
            return completed(
                command,
                value={
                    "result": {
                        "type": "pane_split",
                        "pane": {
                            "workspace_id": "w-test",
                            "tab_id": tab_id,
                            "pane_id": pane_id,
                            "terminal_id": terminal_id,
                        },
                    }
                },
            )
        if command[:3] == ["herdr", "agent", "start"]:
            name = command[3]
            self.agent_states[name] = self.start_status
            pane_id = command[command.index("--pane") + 1]
            pane_number = int(pane_id.rsplit("p", 1)[1])
            self.agent_bindings[name] = {
                "pane_id": pane_id,
                "tab_id": f"w-test:t{pane_number}",
                "terminal_id": f"term-{pane_number}",
            }
            if self.raise_start_once:
                self.raise_start_once = False
                raise RuntimeError("simulated loss after agent start")
            return completed(
                command,
                value={
                    "result": {
                        "type": "agent_started",
                        "agent": self.agent_info(
                            name, include_session=not self.start_session_null
                        ),
                        "argv": ["codex", *command[command.index("--") + 1 :]],
                    }
                },
            )
        if command[:3] == ["herdr", "agent", "prompt"]:
            if self.raise_on_prompt:
                raise RuntimeError("simulated process loss after durable intent")
            if self.prompt_error:
                return completed(
                    command,
                    returncode=1,
                    error={"error": {"code": self.prompt_error}},
                )
            self.agent_states[command[3]] = "working"
            self.prompted_agents.add(command[3])
            if self.lose_prompt_ack:
                raise RuntimeError("ACK lost after runtime accepted prompt")
            return completed(
                command,
                value={
                    "result": {
                        "type": "agent_prompted",
                        "agent": self.agent_info(
                            command[3],
                            include_session=not self.prompt_receipt_session_null,
                        ),
                    }
                },
            )
        if command[:3] == ["herdr", "agent", "read"]:
            return subprocess.CompletedProcess(command, 0, self.screen, "")
        if command[:3] == ["herdr", "agent", "get"]:
            if self.raise_get_once:
                self.raise_get_once = False
                raise RuntimeError("simulated loss after agent start")
            get_error = self.get_error_once or self.get_error
            self.get_error_once = None
            if get_error:
                return completed(
                    command,
                    returncode=1,
                    error={"error": {"code": get_error}},
                )
            agent = self.agent_info(command[3])
            if self.get_not_ready_count:
                self.get_not_ready_count -= 1
                agent["agent_status"] = "unknown"
            return completed(
                command,
                value={
                    "result": {
                        "type": "agent_info",
                        "agent": agent,
                    }
                },
            )
        if command[:3] == ["herdr", "agent", "wait"]:
            if self.wait_timeout:
                return completed(
                    command,
                    returncode=1,
                    error={"error": {"code": "timeout"}},
                )
            return completed(
                command,
                value={"result": {"type": "wait_matched", "event": {}}},
            )
        if command[:3] == ["herdr", "agent", "send-keys"]:
            if self.cancel_quiescent:
                self.agent_states[command[3]] = "idle"
            if self.lose_cancel_ack:
                raise RuntimeError("ACK lost after runtime accepted cancellation")
            return completed(command, value={"result": {"sent": True}})
        if command[:3] == ["herdr", "workspace", "close"]:
            if self.close_error:
                return completed(
                    command,
                    returncode=1,
                    error={"error": {"code": self.close_error}},
                )
            self.workspace_exists = False
            return completed(command, value={"result": {"closed": True}})
        raise AssertionError(f"unexpected Herdr command: {command}")

    def assert_environment(self, kwargs: dict) -> None:
        environment = kwargs.get("env")
        if not isinstance(environment, dict):
            raise AssertionError("Herdr command environment missing")
        if environment.get("HERDR_SESSION") != "mission-control-test":
            raise AssertionError("Herdr session environment mismatch")
        for forbidden in ("HERDR_SOCKET_PATH", "HERDR_CLIENT_SOCKET_PATH"):
            if forbidden in environment:
                raise AssertionError(f"inherited routing survived: {forbidden}")


class HerdrBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name).resolve()
        self.runs = self.tmp / "runs"
        self.runs.mkdir(mode=0o700)
        self.mission_id = str(uuid.uuid4())
        mission_root = self.runs / "missions" / self.mission_id
        mission_root.mkdir(parents=True, mode=0o700)
        (self.runs / "missions").chmod(0o700)
        mission_root.chmod(0o700)
        self.target = self.tmp / "target"
        self.target.mkdir(mode=0o700)
        workflow = workflow_config.load_workflow(
            ROOT / "workflows" / "implementation.yaml"
        )
        workflow["preset"] = "astra_sol"
        self.compiled = workflow_config.compile_workflow(
            workflow,
            router=router_config.load_router(ROOT / "orchestration" / "router.yaml"),
        )
        self.fake = FakeHerdr()
        self.transcripts: dict[str, Path] = {}

    def backend(self, *, runner: FakeHerdr | None = None) -> fleet_herdr.HerdrBackend:
        return fleet_herdr.HerdrBackend(
            self.runs,
            self.mission_id,
            session="mission-control-test",
            feature="herdr-test",
            target_repo=self.target,
            compiled=copy.deepcopy(self.compiled),
            environment={
                "PATH": "/usr/bin:/bin",
                "HERDR_SOCKET_PATH": "/wrong/inherited.sock",
                "HERDR_CLIENT_SOCKET_PATH": "/wrong/client.sock",
            },
            run_command=runner or self.fake,
            transcript_resolver=self.transcripts.get,
        )

    def prompt(
        self, run_id: str, instance_id: str = "lead", candidate_tree_sha: str | None = None
    ) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "mission_id": self.mission_id,
                "run_id": run_id,
                "instance_id": instance_id,
                "result_contract": {
                    "schema_version": 1,
                    "mission_id": self.mission_id,
                    "run_id": run_id,
                    "instance_id": instance_id,
                    "candidate_tree_sha": candidate_tree_sha,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def write_transcript(
        self,
        *,
        agent_session: str,
        member: dict,
        prompt: str,
        final: dict,
        turn_id: str = "turn-herdr-test",
        context_after_user: bool = False,
    ) -> str:
        final_text = json.dumps(final, sort_keys=True, separators=(",", ":"))
        task_started = {
            "type": "event_msg",
            "timestamp": "2026-09-06T00:00:01Z",
            "payload": {"type": "task_started", "turn_id": turn_id},
        }
        contexts = [
            {
                "type": "turn_context",
                "timestamp": "2026-09-06T00:00:01Z",
                "payload": {
                    "turn_id": turn_id,
                    "model": member["model"],
                    "effort": "high",
                },
            },
            {
                "type": "turn_context",
                "timestamp": "2026-09-06T00:00:01Z",
                "payload": {
                    "turn_id": turn_id,
                    "model": member["model"],
                    "effort": "high",
                    "summary": {"compacted": True},
                },
            },
        ]
        for context in contexts:
            context["payload"].update(cwd=str(self.target), approval_policy="never", sandbox_policy=(
                {"type": "workspace-write", "network_access": False,
                 "exclude_tmpdir_env_var": False, "exclude_slash_tmp": False}
                if member["instance_id"] == "worker" else {"type": "read-only"}))
        user = {
            "type": "response_item",
            "timestamp": "2026-09-06T00:00:02Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        }
        rows = [
            {
                "type": "session_meta",
                "timestamp": "2026-09-06T00:00:00Z",
                "payload": {"id": agent_session, "model_provider": "openai", "cli_version": self.fake.codex_version},
            },
            task_started,
            *([user, *contexts] if context_after_user else [*contexts, user]),
            {
                "type": "response_item",
                "timestamp": "2026-09-06T00:00:03Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": final_text}],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-09-06T00:00:04Z",
                "payload": {
                    "type": "task_complete",
                    "turn_id": turn_id,
                    "last_agent_message": final_text,
                },
            },
        ]
        path = self.tmp / f"{agent_session}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        path.chmod(0o600)
        self.transcripts[agent_session] = path
        return final_text

    def write_active_transcript(
        self, *, member: dict, prompt: str, turn_id: str = "turn-active"
    ) -> Path:
        agent_session = member["agent_session"]["value"]
        rows = [
            {
                "type": "session_meta",
                "payload": {"id": agent_session, "model_provider": "openai"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": turn_id},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            },
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": turn_id,
                    "model": member["model"],
                    "effort": "high",
                },
            },
        ]
        path = self.tmp / f"{agent_session}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        path.chmod(0o600)
        self.transcripts[agent_session] = path
        return path

    def booted(self) -> fleet_herdr.HerdrBackend:
        backend = self.backend()
        backend.boot()
        return backend

    def test_boot_uses_exact_compiled_identity_without_cmux(self) -> None:
        state = self.booted().state()
        self.assertEqual(state["phase"], "ready")
        self.assertEqual(
            [
                (member["instance_id"], member["role_type"], member["model"])
                for member in state["members"]
            ],
            [
                ("lead", "astra_lead", "gpt-6-astra"),
                ("worker", "sol_worker", "gpt-5.6-sol"),
                ("reviewer", "sol_reviewer", "gpt-5.6-sol"),
                ("verifier", "sol_verifier", "gpt-5.6-sol"),
            ],
        )
        operations = [self.fake.operation(call) for call in self.fake.calls]
        starts = [call for call in operations if call[1:3] == ["agent", "start"]]
        self.assertEqual(len(starts), 4)
        self.assertEqual(
            [call[call.index("--model") + 1] for call in starts],
            ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-sol", "gpt-5.6-sol"],
        )
        self.assertTrue(
            all(
                call[call.index("-c") + 1] == 'model_reasoning_effort="high"'
                for call in starts
            )
        )
        expected_project = (
            "projects="
            + "{"
            + json.dumps(str(self.target))
            + '={trust_level="untrusted"}}'
        )
        self.assertTrue(
            all(
                [call[index + 1] for index, value in enumerate(call) if value == "-c"]
                == ['model_reasoning_effort="high"', expected_project] + (
                    ["sandbox_workspace_write.network_access=false",
                     "sandbox_workspace_write.exclude_tmpdir_env_var=false",
                     "sandbox_workspace_write.exclude_slash_tmp=false",
                     "sandbox_workspace_write.writable_roots=[]"]
                    if call[call.index("--sandbox") + 1] == "workspace-write" else [])
                for call in starts
            )
        )
        self.assertEqual(
            [call[call.index("--sandbox") + 1] for call in starts],
            ["read-only", "workspace-write", "read-only", "read-only"],
        )
        splits = [call for call in operations if call[1:3] == ["pane", "split"]]
        self.assertEqual(
            [
                (call[3], call[call.index("--direction") + 1])
                for call in splits
            ],
            [("w-test:p1", "right"), ("w-test:p1", "down"), ("w-test:p2", "down")],
        )
        create = next(
            call for call in operations if call[1:3] == ["workspace", "create"]
        )
        propagated = [
            create[index + 1]
            for index, value in enumerate(create[:-1])
            if value == "--env"
        ]
        self.assertTrue(any(value.startswith("PATH=") and value != "PATH=" for value in propagated))
        self.assertFalse(any("cmux" in argument for call in self.fake.calls for argument in call))

    def test_start_allows_ready_surface_with_null_session(self) -> None:
        self.fake.start_session_null = True
        state = self.backend().boot()
        self.assertTrue(all(member["agent_session"] is None for member in state["members"]))
        operations = [self.fake.operation(call) for call in self.fake.calls]
        self.assertEqual(
            len([call for call in operations if call[1:3] == ["agent", "get"]]),
            4,
        )

    def test_starting_recovery_gets_exact_agent_without_duplicate_start(self) -> None:
        self.fake.start_session_null = True
        self.fake.raise_start_once = True
        with self.assertRaisesRegex(RuntimeError, "loss after agent start"):
            self.backend().boot()
        state = self.backend().boot()
        operations = [self.fake.operation(call) for call in self.fake.calls]
        lead_starts = [
            call
            for call in operations
            if call[1:3] == ["agent", "start"] and call[3].endswith("_lead")
        ]
        self.assertEqual(len(lead_starts), 1)
        self.assertEqual(state["phase"], "ready")
        self.assertIsNotNone(state["members"][0]["agent_session"])

    def test_starting_recovery_waits_for_idle_without_duplicate_start(self) -> None:
        self.fake.start_session_null = True
        self.fake.start_status = "unknown"
        with self.assertRaisesRegex(
            fleet_herdr.HerdrBackendError, "not observed ready"
        ):
            self.backend().boot()
        lead = self.backend().state()["members"][0]
        self.fake.agent_states[lead["agent_name"]] = "idle"
        self.fake.start_status = "idle"
        state = self.backend().boot()
        operations = [self.fake.operation(call) for call in self.fake.calls]
        lead_starts = [
            call
            for call in operations
            if call[1:3] == ["agent", "start"] and call[3].endswith("_lead")
        ]
        self.assertEqual(len(lead_starts), 1)
        self.assertEqual(state["phase"], "ready")

    def test_retry_unsubmitted_start_reuses_owned_shell_and_records_history(self) -> None:
        self.fake.start_session_null = True
        self.fake.start_status = "unknown"
        with self.assertRaisesRegex(
            fleet_herdr.HerdrBackendError, "not observed ready"
        ):
            self.backend().boot()
        self.fake.start_status = "idle"
        self.fake.get_error_once = "agent_name_not_found"
        state = self.backend().retry_unsubmitted_start("lead")
        lead = state["members"][0]
        operations = [self.fake.operation(call) for call in self.fake.calls]
        lead_starts = [
            call
            for call in operations
            if call[1:3] == ["agent", "start"] and call[3] == lead["agent_name"]
        ]
        self.assertEqual(len(lead_starts), 2)
        self.assertEqual(state["phase"], "ready")
        self.assertEqual(len(lead["start_attempts"]), 2)
        first, retried = lead["start_attempts"]
        self.assertEqual(first["outcome"], "agent_absent")
        self.assertEqual(first["terminal_id"], lead["terminal_id"])
        self.assertEqual(first["retry_id"], retried["retry_id"])
        self.assertEqual(retried["reason"], "agent_absent_shell_foreground")
        self.assertEqual(retried["outcome"], "ready")

    def test_retry_unsubmitted_start_refuses_existing_agent(self) -> None:
        self.fake.start_session_null = True
        self.fake.start_status = "unknown"
        with self.assertRaisesRegex(
            fleet_herdr.HerdrBackendError, "not observed ready"
        ):
            self.backend().boot()
        with self.assertRaisesRegex(
            fleet_herdr.HerdrBackendError, "agent still exists"
        ):
            self.backend().retry_unsubmitted_start("lead")
        operations = [self.fake.operation(call) for call in self.fake.calls]
        lead_starts = [
            call
            for call in operations
            if call[1:3] == ["agent", "start"] and call[3].endswith("_lead")
        ]
        self.assertEqual(len(lead_starts), 1)

    def test_router_snapshot_launch_is_digest_bound(self) -> None:
        compiled = copy.deepcopy(self.compiled)
        compiled["router_snapshot"]["roles"]["astra_lead"]["command"][-1] = (
            'model_reasoning_effort="low"'
        )
        with self.assertRaisesRegex(
            fleet_herdr.HerdrBackendError, "invalid Herdr Mission binding"
        ):
            fleet_herdr.HerdrBackend(
                self.runs,
                self.mission_id,
                session="mission-control-test",
                feature="herdr-test",
                target_repo=self.target,
                compiled=compiled,
                environment={},
                run_command=self.fake,
                transcript_resolver=self.transcripts.get,
            )

    def test_boot_recovery_does_not_recreate_owned_resources(self) -> None:
        self.booted()
        self.fake.calls.clear()
        recovered = self.backend().boot()
        self.assertEqual(recovered["phase"], "ready")
        operations = [self.fake.operation(call) for call in self.fake.calls]
        self.assertEqual(operations[0], ["herdr", "--version"])
        self.assertEqual(len([call for call in operations if call[1:3] == ["agent", "get"]]), 8)
        self.assertFalse(any(call[1:3] == ["workspace", "create"] for call in operations))

    def test_submit_once_and_recover_agent_state(self) -> None:
        backend = self.booted()
        self.fake.calls.clear()
        run_id = str(uuid.uuid4())
        prompt = self.prompt(run_id)
        first = backend.submit(run_id, prompt)
        repeated = self.backend().submit(run_id, prompt)
        prompts = [
            call
            for call in map(self.fake.operation, self.fake.calls)
            if call[1:3] == ["agent", "prompt"]
        ]
        self.assertEqual(first["status"], "working")
        self.assertEqual(repeated, first)
        self.assertEqual(len(prompts), 1)

        self.fake.agent_states[first["agent_name"]] = "done"
        recovered = self.backend().recover(run_id)
        self.assertEqual(recovered["status"], "settled")

    def test_lazy_session_binds_after_one_admitted_prompt(self) -> None:
        self.fake.start_session_null = True
        self.fake.lazy_session_until_prompt = True
        self.fake.prompt_receipt_session_null = True
        backend = self.booted()
        self.assertIsNone(backend.state()["members"][0]["agent_session"])
        run_id = str(uuid.uuid4())
        submitted = backend.submit(run_id, self.prompt(run_id))
        self.assertIsNone(submitted["agent_session"])
        self.assertIsNone(backend.collect_result(run_id))
        state = backend.state()
        member = state["members"][0]
        bound = state["submissions"][run_id]
        self.assertIsNotNone(member["agent_session"])
        self.assertEqual(bound["agent_session"], member["agent_session"])
        self.assertEqual(bound["workspace_id"], member["workspace_id"])
        self.assertEqual(bound["tab_id"], member["tab_id"])
        self.assertEqual(bound["pane_id"], member["pane_id"])
        self.assertEqual(bound["terminal_id"], member["terminal_id"])
        operations = [self.fake.operation(call) for call in self.fake.calls]
        prompts = [call for call in operations if call[1:3] == ["agent", "prompt"]]
        self.assertEqual(len(prompts), 1)
        self.assertIsNone(backend.collect_result(run_id))

    def test_uncertain_submit_is_never_replayed(self) -> None:
        backend = self.booted()
        self.fake.calls.clear()
        self.fake.raise_on_prompt = True
        run_id = str(uuid.uuid4())
        with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
            backend.submit(run_id, self.prompt(run_id))
        self.fake.raise_on_prompt = False
        recovered = self.backend().submit(run_id, self.prompt(run_id))
        self.assertEqual(recovered["status"], "indeterminate")
        prompts = [
            call
            for call in map(self.fake.operation, self.fake.calls)
            if call[1:3] == ["agent", "prompt"]
        ]
        self.assertEqual(len(prompts), 1)

    def test_identity_drift_blocks_prompt_before_effect(self) -> None:
        backend = self.booted()
        run_id = str(uuid.uuid4())
        lead = backend.state()["members"][0]
        self.fake.agent_bindings[lead["agent_name"]]["terminal_id"] = "term-other"
        self.fake.calls.clear()
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "identity mismatch"):
            backend.submit(run_id, self.prompt(run_id))
        operations = [self.fake.operation(call) for call in self.fake.calls]
        self.assertFalse(any(call[1:3] == ["agent", "prompt"] for call in operations))

    def uncertain_submission(self):
        backend = self.booted()
        run_id = str(uuid.uuid4())
        prompt = self.prompt(run_id)
        self.fake.lose_prompt_ack = True
        with self.assertRaisesRegex(RuntimeError, "ACK lost after runtime accepted prompt"):
            backend.submit(run_id, prompt)
        backend = self.backend()
        recovered = backend.recover(run_id)
        self.assertEqual(recovered["status"], "indeterminate")
        self.assertFalse(recovered["cancel_attempted"])
        self.assertEqual(self.fake.agent_states[recovered["agent_name"]], "working")
        member = backend.state()["members"][0]
        self.write_active_transcript(member=member, prompt=prompt)
        return backend, run_id, prompt, member

    def test_uncertain_submit_recover_then_cancel_reconciles_exact_run(self) -> None:
        backend, run_id, _, member = self.uncertain_submission()
        cancelled = backend.cancel(run_id)
        self.assertEqual(cancelled["status"], "abandoned")
        self.assertEqual(cancelled["run_id"], run_id)
        self.assertEqual(cancelled["cancel_turn_id"], "turn-active")
        self.assertEqual(cancelled["agent_session"], member["agent_session"])
        self.assertEqual(cancelled["generation"], backend.state()["generation"])
        self.assertEqual(self.fake.agent_states[member["agent_name"]], "idle")
        self.assertEqual(self.backend().cancel(run_id), cancelled)
        operations = [self.fake.operation(call) for call in self.fake.calls]
        self.assertEqual(sum(call[1:3] == ["agent", "prompt"] for call in operations), 1)
        self.assertEqual(
            [call for call in operations if call[1:3] == ["agent", "send-keys"]],
            [["herdr", "agent", "send-keys", member["agent_name"], "ctrl+c"]],
        )

    def test_uncertain_cancel_refuses_missing_runtime(self) -> None:
        backend, run_id, _, member = self.uncertain_submission()
        self.fake.get_error = "agent_not_found"
        before_cancel = len(self.fake.calls)
        for _ in range(2):
            with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "agent_not_found"):
                backend.cancel(run_id)
            observed = backend.state()["submissions"][run_id]
            self.assertEqual(observed["status"], "indeterminate")
            self.assertFalse(observed["cancel_attempted"])
        operations = [self.fake.operation(c) for c in self.fake.calls]
        self.assertFalse(any(c[1:3] == ["agent", "send-keys"] for c in operations))
        self.assertEqual(sum(c[1:3] == ["agent", "prompt"] for c in operations), 1)
        observations = [self.fake.operation(c) for c in self.fake.calls[before_cancel:]]
        self.assertEqual([c[3] for c in observations if c[1:3] == ["agent", "get"]],
                         [member["agent_name"], member["agent_name"]])

    def test_uncertain_cancel_requires_exact_active_transcript(self) -> None:
        backend, run_id, _, member = self.uncertain_submission()
        path = self.transcripts[member["agent_session"]["value"]]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        foreign = copy.deepcopy(rows[1:])
        foreign[0]["payload"]["turn_id"] = "turn-foreign"
        foreign[1]["payload"]["content"][0]["text"] = "another prompt"
        foreign[2]["payload"]["turn_id"] = "turn-foreign"
        complete = {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-active"}}
        for label, transcript in (("missing", []), ("newer turn", rows + foreign), ("complete", rows + [complete])):
            with self.subTest(transcript=label):
                path.write_text("".join(json.dumps(row) + "\n" for row in transcript))
                observed = backend.cancel(run_id)
                self.assertEqual(observed["status"], "indeterminate")
                self.assertFalse(observed["cancel_attempted"])
                self.assertEqual(self.fake.agent_states[member["agent_name"]], "working")
        operations = [self.fake.operation(c) for c in self.fake.calls]
        self.assertEqual(sum(c[1:3] == ["agent", "prompt"] for c in operations), 1)
        self.assertFalse(any(c[1:3] == ["agent", "send-keys"] for c in operations))

    def test_uncertain_cancel_refuses_runtime_identity_drift(self) -> None:
        backend, run_id, _, _ = self.uncertain_submission()
        agent_info = self.fake.agent_info
        for field in ("name", "workspace_id", "tab_id", "pane_id", "terminal_id", "agent_session"):
            with self.subTest(field=field):
                def changed_identity(name, **kwargs):
                    receipt = agent_info(name, **kwargs)
                    receipt[field] = ({**receipt[field], "value": "session-other"}
                                      if field == "agent_session" else "other")
                    return receipt

                with mock.patch.object(self.fake, "agent_info", side_effect=changed_identity):
                    with self.assertRaises(fleet_herdr.HerdrBackendError):
                        backend.cancel(run_id)
                observed = backend.state()["submissions"][run_id]
                self.assertEqual(observed["status"], "indeterminate")
                self.assertFalse(observed["cancel_attempted"])
        operations = [self.fake.operation(c) for c in self.fake.calls]
        self.assertEqual(sum(c[1:3] == ["agent", "prompt"] for c in operations), 1)
        self.assertFalse(any(c[1:3] == ["agent", "send-keys"] for c in operations))

    def test_uncertain_cancel_uses_existing_quiescence_contract_without_signal(self) -> None:
        for runtime_state in ("idle", "done", "blocked"):
            with self.subTest(runtime_state=runtime_state):
                fixture = HerdrBackendTests()
                fixture.setUp()
                try:
                    backend, run_id, _, member = fixture.uncertain_submission()
                    fixture.fake.agent_states[member["agent_name"]] = runtime_state
                    observed = backend.cancel(run_id)
                    self.assertEqual(observed["status"], "abandoned")
                    self.assertTrue(observed["cancel_attempted"])
                    self.assertEqual(fixture.fake.agent_states[member["agent_name"]], runtime_state)
                    operations = [fixture.fake.operation(c) for c in fixture.fake.calls]
                    self.assertEqual(sum(c[1:3] == ["agent", "prompt"] for c in operations), 1)
                    self.assertFalse(any(c[1:3] == ["agent", "send-keys"] for c in operations))
                finally:
                    fixture.doCleanups()

    def test_wait_timeout_keeps_working_without_resubmit(self) -> None:
        backend = self.booted()
        run_id = str(uuid.uuid4())
        backend.submit(run_id, self.prompt(run_id))
        self.fake.calls.clear()
        self.fake.wait_timeout = True
        observed = backend.wait(run_id, timeout_ms=25)
        operations = [self.fake.operation(call) for call in self.fake.calls]
        self.assertEqual(observed["status"], "working")
        self.assertFalse(any(call[1:3] == ["agent", "prompt"] for call in operations))

    def test_reused_session_baseline_requires_retained_prior_turn_history(self):
        backend = self.booted()
        run_id = str(uuid.uuid4())
        prompt = self.prompt(run_id)
        backend.submit(run_id, prompt, instance_id="lead")
        current = backend.state()
        member = next(m for m in current["members"] if m["instance_id"] == "lead")
        session = member["agent_session"]["value"]
        final = {**json.loads(prompt)["result_contract"], "status":"PASS",
                 "summary":"fixture prior Plan", "artifacts":[]}
        self.write_transcript(agent_session=session,member=member,prompt=prompt,final=final)
        path = self.transcripts[session]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        counts={"input_tokens":100,"output_tokens":10,"cached_input_tokens":20}
        rows.insert(-2,{"type":"event_msg","payload":{"type":"token_count",
            "info":{"total_token_usage":counts,"last_token_usage":counts}}})
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        previous = backend.collect_result(run_id)
        def capture():
            pin = backend._capture_usage_baseline(run_id=str(uuid.uuid4()),
                prompt_sha256="f"*64,generation=current["generation"],member=member)
            return fleet_json.loads(fleet_artifacts.get_bytes(self.runs,self.mission_id,pin))
        good=capture()
        self.assertEqual(good["counts"],counts)
        self.assertEqual(good["prior_result_artifact_ids"],[previous["result_artifact_id"]])
        reset = {"type":"event_msg","payload":{"type":"token_count",
            "info":{"total_token_usage":dict.fromkeys(counts,0)}}}
        invalid = copy.deepcopy(reset)
        invalid["payload"]["info"]["total_token_usage"]["cached_input_tokens"] = 999
        for suffix, reason in (([reset],"usage_counter_reset_or_regression"),
                ([invalid,rows[-3]],"invalid_usage_counter_snapshot")):
            path.write_text(''.join(json.dumps(row)+'\n' for row in rows+suffix))
            bad=capture()
            self.assertEqual(bad["status"],"unknown")
            self.assertEqual(bad["reason"],reason)
        for replacement in ([rows[0]], [rows[0],rows[-3]]):
            path.write_text(''.join(json.dumps(row)+'\n' for row in replacement))
            missing=capture()
            self.assertEqual(missing["status"],"unknown")
            self.assertEqual(missing["reason"],"prior_session_history_unavailable_or_changed")
        path.unlink()
        self.assertEqual(backend.collect_result(run_id),previous)

    def test_collect_result_binds_transcript_turn_prompt_and_cas(self) -> None:
        backend = self.booted()
        run_id = str(uuid.uuid4())
        candidate_tree = "a" * 40
        prompt = self.prompt(run_id, "worker", candidate_tree)
        backend.submit(run_id, prompt, instance_id="worker")
        self.assertIsNone(backend.collect_result(run_id))
        state = backend.state()
        member = next(item for item in state["members"] if item["instance_id"] == "worker")
        agent_session = member["agent_session"]["value"]
        artifact_path = self.target / "answer.txt"
        artifact_path.write_text("implemented\n")
        artifact_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        final = {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "run_id": run_id,
            "instance_id": "worker",
            "status": "PASS",
            "summary": "focused tests passed",
            "artifacts": [{"path": "answer.txt", "sha256": artifact_sha}],
            "candidate_tree_sha": candidate_tree,
        }
        final_text = self.write_transcript(
            agent_session=agent_session,
            member=member,
            prompt=prompt,
            final=final,
        )
        transcript_path = self.transcripts[agent_session]
        complete_transcript = transcript_path.read_bytes()
        lines = complete_transcript.splitlines()
        transcript_path.write_bytes(b"\n".join(lines[:-1]) + b"\n{\"type\":")
        self.assertIsNone(backend.collect_result(run_id))
        transcript_path.write_bytes(complete_transcript)
        result = backend.collect_result(run_id)
        assert result is not None
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["candidate_tree_sha"], candidate_tree)
        self.assertEqual(result["turn_id"], "turn-herdr-test")
        self.assertEqual(
            result["artifact_id"], hashlib.sha256(final_text.encode()).hexdigest()
        )
        self.assertNotEqual(result["result_artifact_id"], result["artifact_id"])
        self.assertEqual(
            fleet_artifacts.get_bytes(
                self.runs, self.mission_id, result["artifact_id"]
            ),
            final_text.encode(),
        )
        self.assertEqual(result["evidence"]["herdr_session"], "mission-control-test")
        self.assertEqual(result["evidence"]["prompt_sha256"], hashlib.sha256(prompt.encode()).hexdigest())
        transcript_segment = fleet_artifacts.get_bytes(
            self.runs,
            self.mission_id,
            result["evidence"]["transcript_artifact_id"],
        )
        self.assertEqual(
            result["evidence"]["transcript_sha256"],
            hashlib.sha256(transcript_segment).hexdigest(),
        )
        segment_rows = [json.loads(line) for line in transcript_segment.splitlines()]
        self.assertEqual(segment_rows[0]["type"], "session_meta")
        self.assertEqual(segment_rows[1]["payload"]["type"], "task_started")
        self.assertEqual(segment_rows[-1]["payload"]["type"], "task_complete")
        self.assertEqual(backend.collect_result(run_id), result)

    def permission_result_fixture(self, role="lead"):
        backend = self.booted()
        run_id = str(uuid.uuid4())
        prompt = self.prompt(run_id, role)
        backend.submit(run_id, prompt, instance_id=role)
        member = next(m for m in backend.state()["members"] if m["instance_id"] == role)
        session = member["agent_session"]["value"]
        self.write_transcript(agent_session=session, member=member, prompt=prompt, final={
            "schema_version": 1, "mission_id": self.mission_id, "run_id": run_id,
            "instance_id": role, "status": "PASS", "summary": "fixture",
            "artifacts": [], "candidate_tree_sha": None})
        return backend, run_id, self.transcripts[session]

    def test_live_collection_rejects_missing_permissions_before_receipt(self):
        backend, run_id, path = self.permission_result_fixture()
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        next(r["payload"] for r in rows if r["type"] == "turn_context").pop("sandbox_policy")
        path.write_bytes(b"".join(fleet_json.canonical_bytes(r) + b"\n" for r in rows))
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "sandbox_policy"):
            backend.collect_result(run_id)
        self.assertFalse((self.runs / backend._result_relative(run_id)).exists())
        self.assertNotEqual(backend.state()["submissions"][run_id]["status"], "succeeded")

    def test_cached_result_rechecks_resealed_transcript_without_live_runtime(self):
        backend, run_id, _ = self.permission_result_fixture("worker")
        result = backend.collect_result(run_id)
        rows = fleet_json.load_jsonl(fleet_artifacts.get_bytes(self.runs, self.mission_id,
            result["evidence"]["transcript_artifact_id"]))
        contexts = [r["payload"] for r in rows if r["type"] == "turn_context"]
        contexts[-1]["sandbox_policy"]["network_access"] = True
        transcript_id = fleet_artifacts.put_bytes(self.runs, self.mission_id,
            b"".join(fleet_json.canonical_bytes(r) + b"\n" for r in rows))["artifact_id"]
        result["evidence"].update(transcript_artifact_id=transcript_id, transcript_sha256=transcript_id)
        # Preserve the stale positive attestation to prove it is never trusted.
        result.pop("result_artifact_id")
        envelope = fleet_artifacts.put_bytes(self.runs, self.mission_id, fleet_json.canonical_bytes(result))
        result["result_artifact_id"] = envelope["artifact_id"]
        (self.runs / backend._result_relative(run_id)).write_bytes(fleet_json.canonical_bytes(result) + b"\n")
        calls = len(self.fake.calls)
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "cached result permission evidence"):
            backend.collect_result(run_id)
        self.assertEqual(len(self.fake.calls), calls)

    def test_collect_result_accepts_user_before_turn_context(self) -> None:
        backend = self.booted()
        run_id = str(uuid.uuid4())
        prompt = self.prompt(run_id, "reviewer")
        backend.submit(run_id, prompt, instance_id="reviewer")
        member = next(
            item for item in backend.state()["members"] if item["instance_id"] == "reviewer"
        )
        readme = self.target / "README.md"
        readme.write_text("fixture\n")
        readme_sha = hashlib.sha256(readme.read_bytes()).hexdigest()
        final = {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "run_id": run_id,
            "instance_id": "reviewer",
            "status": "BLOCKED",
            "summary": "review needs more evidence",
            "artifacts": [{"path": "README.md", "sha256": readme_sha}],
            "candidate_tree_sha": None,
        }
        self.write_transcript(
            agent_session=member["agent_session"]["value"],
            member=member,
            prompt=prompt,
            final=final,
            context_after_user=True,
        )
        result = backend.collect_result(run_id)
        assert result is not None
        self.assertEqual(result["status"], "BLOCKED")

    def test_cancel_requires_quiescence_and_never_resends(self) -> None:
        backend = self.booted()
        run_id = str(uuid.uuid4())
        prompt = self.prompt(run_id)
        submitted = backend.submit(run_id, prompt)
        self.write_active_transcript(member=backend.state()["members"][0], prompt=prompt)
        self.fake.calls.clear()
        self.fake.cancel_quiescent = False
        self.fake.wait_timeout = True
        first = backend.cancel(run_id)
        self.assertEqual(first["status"], "indeterminate")
        self.fake.agent_states[submitted["agent_name"]] = "idle"
        recovered = self.backend().cancel(run_id)
        self.assertEqual(recovered["status"], "abandoned")
        sends = [
            call
            for call in map(self.fake.operation, self.fake.calls)
            if call[1:3] == ["agent", "send-keys"]
        ]
        self.assertEqual(len(sends), 1)

    def test_cancel_refuses_external_newer_turn(self) -> None:
        backend = self.booted()
        run_id = str(uuid.uuid4())
        prompt = self.prompt(run_id)
        backend.submit(run_id, prompt)
        member = backend.state()["members"][0]
        agent_session = member["agent_session"]["value"]
        final = {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "run_id": run_id,
            "instance_id": "lead",
            "status": "PASS",
            "summary": "turn A complete",
            "artifacts": [{"path": "README.md", "sha256": "b" * 64}],
            "candidate_tree_sha": None,
        }
        self.write_transcript(
            agent_session=agent_session,
            member=member,
            prompt=prompt,
            final=final,
            turn_id="turn-a",
        )
        path = self.transcripts[agent_session]
        external_rows = [
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "manual turn B"}],
                },
            },
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-b",
                    "model": member["model"],
                    "effort": "high",
                },
            },
        ]
        with path.open("a", encoding="utf-8") as handle:
            for row in external_rows:
                handle.write(json.dumps(row) + "\n")
        self.fake.calls.clear()
        observed = backend.cancel(run_id)
        operations = [self.fake.operation(call) for call in self.fake.calls]
        self.assertEqual(observed["status"], "indeterminate")
        self.assertFalse(observed["cancel_attempted"])
        self.assertFalse(any(call[1:3] == ["agent", "send-keys"] for call in operations))

    def test_blocked_and_failed_results_are_terminal(self) -> None:
        backend = self.booted()
        self.fake.prompt_error = "agent_blocked"
        blocked_run = str(uuid.uuid4())
        blocked = backend.submit(blocked_run, self.prompt(blocked_run))
        self.assertEqual(blocked["status"], "blocked")

        self.fake.prompt_error = None
        failed_run = str(uuid.uuid4())
        submitted = backend.submit(failed_run, self.prompt(failed_run))
        self.fake.get_error = "agent_not_found"
        failed = backend.recover(failed_run)
        self.assertEqual(submitted["status"], "working")
        self.assertEqual(failed["status"], "failed")

    def test_terminal_recover_and_wait_do_not_require_live_herdr(self) -> None:
        backend = self.booted()
        run_id = str(uuid.uuid4())
        self.fake.prompt_error = "agent_blocked"
        blocked = backend.submit(run_id, self.prompt(run_id))
        self.fake.calls.clear()
        self.fake.version = "9.9.9"
        self.assertEqual(backend.recover(run_id), blocked)
        self.assertEqual(backend.wait(run_id, timeout_ms=1), blocked)
        self.assertEqual(backend.cancel(run_id), blocked)
        self.assertEqual(self.fake.calls, [])

    def test_cancel_is_sent_once_and_becomes_abandoned(self) -> None:
        backend = self.booted()
        run_id = str(uuid.uuid4())
        prompt = self.prompt(run_id)
        backend.submit(run_id, prompt)
        self.write_active_transcript(member=backend.state()["members"][0], prompt=prompt)
        self.fake.calls.clear()
        cancelled = backend.cancel(run_id)
        repeated = self.backend().cancel(run_id)
        sends = [
            call
            for call in map(self.fake.operation, self.fake.calls)
            if call[1:3] == ["agent", "send-keys"]
        ]
        self.assertEqual(cancelled["status"], "abandoned")
        self.assertEqual(repeated, cancelled)
        self.assertEqual(sends, [["herdr", "agent", "send-keys", cancelled["agent_name"], "ctrl+c"]])

    def test_teardown_closes_only_the_owned_workspace_once(self) -> None:
        unbooted = self.backend()
        self.assertFalse(unbooted.teardown())
        self.assertEqual(self.fake.calls, [])

        backend = self.booted()
        self.fake.calls.clear()
        self.assertTrue(backend.teardown())
        self.assertFalse(self.backend().teardown())
        closes = [
            call
            for call in map(self.fake.operation, self.fake.calls)
            if call[1:3] == ["workspace", "close"]
        ]
        self.assertEqual(closes, [["herdr", "workspace", "close", "w-test"]])

    def test_teardown_refuses_working_or_blocked_agents(self) -> None:
        backend = self.booted()
        lead = backend.state()["members"][0]
        for status in ("working", "blocked"):
            with self.subTest(status=status):
                self.fake.agent_states[lead["agent_name"]] = status
                self.fake.calls.clear()
                with self.assertRaisesRegex(
                    fleet_herdr.HerdrBackendError, "not safely quiescent"
                ):
                    backend.teardown()
                operations = [self.fake.operation(call) for call in self.fake.calls]
                self.assertFalse(
                    any(call[1:3] == ["workspace", "close"] for call in operations)
                )

    def test_teardown_refuses_non_terminal_submission(self) -> None:
        backend = self.booted()
        run_id = str(uuid.uuid4())
        backend.submit(run_id, self.prompt(run_id))
        self.fake.calls.clear()
        with self.assertRaisesRegex(
            fleet_herdr.HerdrBackendError, "non-terminal submissions"
        ):
            backend.teardown()
        operations = [self.fake.operation(call) for call in self.fake.calls]
        self.assertFalse(any(call[1:3] == ["workspace", "close"] for call in operations))

    def test_uncertain_teardown_is_never_replayed(self) -> None:
        backend = self.booted()
        self.fake.calls.clear()
        self.fake.close_error = "transport_lost"
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "close failed"):
            backend.teardown()
        self.fake.close_error = None
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "indeterminate"):
            self.backend().teardown()
        closes = [
            call
            for call in map(self.fake.operation, self.fake.calls)
            if call[1:3] == ["workspace", "close"]
        ]
        self.assertEqual(closes, [["herdr", "workspace", "close", "w-test"]])

    def test_close_attempt_reconciles_only_proven_workspace_absence(self) -> None:
        backend = self.booted()
        self.fake.calls.clear()
        self.fake.close_error = "transport_lost"
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "close failed"):
            backend.teardown()
        self.fake.close_error = None
        self.fake.workspace_exists = False
        self.assertTrue(self.backend().teardown())
        self.assertTrue(self.backend().state()["workspace"]["closed"])
        closes = [
            call
            for call in map(self.fake.operation, self.fake.calls)
            if call[1:3] == ["workspace", "close"]
        ]
        self.assertEqual(closes, [["herdr", "workspace", "close", "w-test"]])


if __name__ == "__main__":
    unittest.main()
