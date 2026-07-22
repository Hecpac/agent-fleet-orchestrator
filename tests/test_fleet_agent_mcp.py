from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from tests.mission_control_test_support import create_running_mission


ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "scripts" / "fleet_agent_mcp.py"
sys.path.insert(0, str(ROOT / "scripts"))
import fleet_agent_mcp  # noqa: E402
import fleet_artifacts  # noqa: E402
import fleet_control  # noqa: E402
import fleet_control_service  # noqa: E402
import fleet_mission_state as mission_state  # noqa: E402


def tool_call(
    name: str,
    arguments: dict[str, object],
    *,
    identifier: object = 1,
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


class OneShotUnixServer:
    def __init__(self, path: Path, reply) -> None:
        self.path = path
        self.reply = reply
        self.request: dict[str, object] | None = None
        self.error: BaseException | None = None
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.server.bind(str(path))
        except BaseException:
            self.server.close()
            raise
        os.chmod(path, 0o600)
        self.server.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            connection, _ = self.server.accept()
            with connection:
                raw = bytearray()
                while not raw.endswith(b"\n"):
                    chunk = connection.recv(65_536)
                    if not chunk:
                        break
                    raw.extend(chunk)
                self.request = json.loads(raw)
                value = self.reply(self.request)
                payload = value if isinstance(value, bytes) else (
                    json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n"
                )
                connection.sendall(payload)
        except BaseException as exc:  # pragma: no cover - surfaced by __exit__
            self.error = exc

    def __enter__(self) -> OneShotUnixServer:
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.thread.join(timeout=5)
        self.server.close()
        self.path.unlink(missing_ok=True)
        if exc is None and self.thread.is_alive():
            raise AssertionError("one-shot Unix server did not finish")
        if exc is None and self.error is not None:
            raise self.error


class FleetAgentMCPProtocolTests(unittest.TestCase):
    def test_local_surface_lists_only_safe_tools_with_caller_ids(self) -> None:
        initialized = fleet_agent_mcp.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertEqual(initialized["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(
            fleet_agent_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})[
                "result"
            ],
            {},
        )
        listed = fleet_agent_mcp.handle(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
        )
        tools = listed["result"]["tools"]
        self.assertEqual(
            {tool["name"] for tool in tools},
            {
                "dispatch",
                "dispatch_many",
                "wait",
                "get_result",
                "relay_result",
                "request_assurance",
                "request_human",
                "inspect_roster",
                "inspect_mission",
            },
        )
        read_only = {"wait", "get_result", "inspect_roster", "inspect_mission"}
        for tool in tools:
            if tool["name"] in read_only:
                self.assertEqual(tool.get("annotations"), {"readOnlyHint": True})
            else:
                self.assertNotEqual(
                    (tool.get("annotations") or {}).get("readOnlyHint"), True
                )
            schema = tool["inputSchema"]
            self.assertTrue(
                {"_caller_run_id", "_caller_token_id"} <= set(schema["required"])
            )
            for property_schema in schema["properties"].values():
                if "enum" in property_schema:
                    self.assertEqual(property_schema.get("type"), "string")
            self.assertEqual(schema["properties"]["_caller_run_id"]["format"], "uuid")
            self.assertEqual(schema["properties"]["_caller_token_id"]["format"], "uuid")
        serialized = json.dumps(tools, sort_keys=True)
        self.assertNotIn('"token_id"', serialized)
        self.assertNotIn("token_path", serialized)
        self.assertNotIn("parent_run_id", serialized)
        self.assertNotIn('"cancel"', serialized)
        self.assertNotIn('"complete"', serialized)

    def test_proxy_injects_identity_and_rejects_reserved_identity_fields(self) -> None:
        run_id = str(uuid.uuid4())
        token_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "proxy.sock"

            def reply(envelope: dict[str, object]) -> dict[str, object]:
                request = envelope["request"]
                return {
                    "ok": True,
                    "result": {
                        "identity": {
                            "kind": "specialist",
                            "instance": "scout",
                            "run_id": run_id,
                        },
                        "response": {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "result": {
                                "content": [{"type": "text", "text": "{}"}],
                                "isError": False,
                            },
                        },
                    },
                }

            with OneShotUnixServer(socket_path, reply) as server, mock.patch.dict(
                os.environ, {"FLEET_CONTROL_SOCKET": str(socket_path)}
            ):
                result = fleet_agent_mcp.handle(
                    tool_call(
                        "dispatch",
                        {
                            "recipient_instance": "verifier",
                            "capability": "verify",
                            "objective": "verify proxy binding",
                            "idempotency_key": "proxy:binding",
                            "_caller_run_id": run_id,
                            "_caller_token_id": token_id,
                        },
                    )
                )
            self.assertFalse(result["result"]["isError"])
            self.assertEqual(server.request["schema_version"], 2)
            self.assertEqual(
                server.request["caller"], {"run_id": run_id, "token_id": token_id}
            )
            forwarded = server.request["request"]["params"]["arguments"]
            self.assertNotIn("_caller_run_id", forwarded)
            self.assertNotIn("_caller_token_id", forwarded)
            self.assertEqual(forwarded["parent_run_id"], run_id)
            self.assertEqual(forwarded["token_id"], token_id)

        denied = fleet_agent_mcp.handle(
            tool_call(
                "inspect_mission",
                {
                    "_caller_run_id": run_id,
                    "_caller_token_id": token_id,
                    "token_path": "/tmp/forged-token.json",
                },
            )
        )
        self.assertTrue(denied["result"]["isError"])
        serialized = json.dumps(denied)
        self.assertNotIn(run_id, serialized)
        self.assertNotIn(token_id, serialized)
        self.assertNotIn("/tmp/forged-token.json", serialized)

    def test_proxy_validates_exact_tool_and_nested_arguments_before_connecting(self) -> None:
        run_id = str(uuid.uuid4())
        token_id = str(uuid.uuid4())
        identity = {
            "_caller_run_id": run_id,
            "_caller_token_id": token_id,
        }
        cases = (
            ("inspect_mission", {**identity, "unknown": "field"}),
            (
                "dispatch",
                {
                    **identity,
                    "recipient_instance": "verifier",
                    "capability": "verify",
                    "objective": "closed schema",
                    "idempotency_key": "proxy:closed",
                    "remaining_budget": True,
                },
            ),
            (
                "dispatch_many",
                {
                    **identity,
                    "requests": [
                        {
                            "recipient_instance": "verifier",
                            "capability": "verify",
                            "objective": "closed nested schema",
                            "idempotency_key": "proxy:nested",
                            "token_path": "/tmp/artificial-token-secret",
                        }
                    ],
                },
            ),
            ("wait", {**identity, "run_ids": [], "timeout_seconds": 1}),
        )
        with mock.patch.object(fleet_agent_mcp, "_socket_request") as transport:
            for name, arguments in cases:
                with self.subTest(name=name, arguments=arguments):
                    denied = fleet_agent_mcp.handle(tool_call(name, arguments))
                    self.assertTrue(denied["result"]["isError"])
                    self.assertEqual(
                        denied["result"]["content"][0]["text"],
                        fleet_agent_mcp.GENERIC_FAILURE_MESSAGE,
                    )
            transport.assert_not_called()

    def test_proxy_sanitizes_local_and_remote_error_secrets(self) -> None:
        run_id = str(uuid.uuid4())
        token_id = str(uuid.uuid4())
        arguments = {
            "_caller_run_id": run_id,
            "_caller_token_id": token_id,
        }
        artificial = (
            f"/tmp/private-control.sock /tmp/token-store/{token_id}.json "
            "ARTIFICIAL_SECRET_VALUE"
        )
        with mock.patch.object(
            fleet_agent_mcp,
            "_forward_tool_call",
            side_effect=fleet_agent_mcp.AgentProxyError(artificial),
        ):
            local = fleet_agent_mcp.handle(tool_call("inspect_mission", arguments))
        self.assertEqual(
            local["result"]["content"][0]["text"],
            fleet_agent_mcp.GENERIC_FAILURE_MESSAGE,
        )
        self.assertNotIn(artificial, json.dumps(local, sort_keys=True))

        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "proxy.sock"

            def reply(envelope: dict[str, object]) -> dict[str, object]:
                request = envelope["request"]
                return {
                    "ok": True,
                    "result": {
                        "identity": {
                            "kind": "specialist",
                            "instance": "scout",
                            "run_id": run_id,
                        },
                        "response": {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "result": {
                                "content": [{"type": "text", "text": artificial}],
                                "isError": True,
                            },
                        },
                    },
                }

            with OneShotUnixServer(socket_path, reply), mock.patch.dict(
                os.environ, {"FLEET_CONTROL_SOCKET": str(socket_path)}
            ):
                remote = fleet_agent_mcp.handle(
                    tool_call("inspect_mission", arguments)
                )
        self.assertEqual(
            remote["result"]["content"][0]["text"],
            fleet_agent_mcp.GENERIC_FAILURE_MESSAGE,
        )
        serialized = json.dumps(remote, sort_keys=True)
        for secret in (artificial, token_id, "/tmp/private-control.sock"):
            self.assertNotIn(secret, serialized)

    def test_proxy_rejects_wrong_identity_typed_id_and_multiple_frames(self) -> None:
        run_id = str(uuid.uuid4())
        token_id = str(uuid.uuid4())
        arguments = {"_caller_run_id": run_id, "_caller_token_id": token_id}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def response_with(*, identity_run: str = run_id, identifier: object = 1):
                return {
                    "ok": True,
                    "result": {
                        "identity": {
                            "kind": "specialist",
                            "instance": "scout",
                            "run_id": identity_run,
                        },
                        "response": {
                            "jsonrpc": "2.0",
                            "id": identifier,
                            "result": {
                                "content": [{"type": "text", "text": "{}"}],
                                "isError": False,
                            },
                        },
                    },
                }

            cases = (
                ("identity.sock", lambda _: response_with(identity_run=str(uuid.uuid4()))),
                ("typed-id.sock", lambda _: response_with(identifier=True)),
                (
                    "two-lines.sock",
                    lambda _: (
                        json.dumps(response_with()).encode("utf-8")
                        + b"\n"
                        + json.dumps(response_with()).encode("utf-8")
                        + b"\n"
                    ),
                ),
                (
                    "duplicate-key.sock",
                    lambda _: (
                        json.dumps(response_with(), separators=(",", ":"))
                        .replace('"ok":true', '"ok":true,"ok":true', 1)
                        .encode("utf-8")
                        + b"\n"
                    ),
                ),
                ("nonfinite.sock", lambda _: b'{"ok":NaN}\n'),
            )
            for filename, reply in cases:
                with self.subTest(filename=filename):
                    socket_path = root / filename
                    with OneShotUnixServer(socket_path, reply), mock.patch.dict(
                        os.environ, {"FLEET_CONTROL_SOCKET": str(socket_path)}
                    ):
                        result = fleet_agent_mcp.handle(
                            tool_call("inspect_mission", arguments)
                        )
                    self.assertTrue(result["result"]["isError"])
                    self.assertNotIn(run_id, json.dumps(result))
                    self.assertNotIn(token_id, json.dumps(result))

    def test_stdio_parser_rejects_duplicate_keys_and_nonfinite_numbers(self) -> None:
        for raw in (
            '{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n',
            '{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n',
            '{"jsonrpc":"2.0","id":Infinity,"method":"ping"}\n',
            '{"jsonrpc":"2.0","id":-Infinity,"method":"ping"}\n',
        ):
            with self.subTest(raw=raw):
                result = subprocess.run(
                    [sys.executable, str(PROXY)],
                    input=raw,
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                reply = json.loads(result.stdout)
                self.assertEqual(reply["error"]["code"], -32700)

        with self.assertRaisesRegex(
            ValueError,
            "Out of range float values|non-finite JSON number",
        ):
            fleet_agent_mcp._dumps_strict({"unsafe": float("nan")})

    def test_preflight_proves_local_discovery_and_socket_callability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            denied_socket = root / "denied.sock"

            def deny(envelope: dict[str, object]) -> dict[str, object]:
                self.assertEqual(envelope["request"]["method"], "ping")
                return {"ok": False, "error": "generic denial"}

            with OneShotUnixServer(denied_socket, deny), mock.patch.dict(
                os.environ,
                {"FLEET_CONTROL_SOCKET": str(denied_socket)},
                clear=False,
            ):
                result = subprocess.run(
                    [sys.executable, str(PROXY), "--preflight"],
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                    env={
                        key: value
                        for key, value in os.environ.items()
                        if key
                        not in {
                            fleet_agent_mcp.PREFLIGHT_RUN_ENV,
                            fleet_agent_mcp.PREFLIGHT_TOKEN_ENV,
                        }
                    },
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            ready = json.loads(result.stdout)
            self.assertEqual(ready["status"], "ready")
            self.assertEqual(ready["socket_probe"], "denial_ping")
            self.assertEqual(set(ready["tool_names"]), fleet_agent_mcp.TOOL_NAMES)

            run_id = str(uuid.uuid4())
            token_id = str(uuid.uuid4())
            authenticated_socket = root / "authenticated.sock"

            def allow(envelope: dict[str, object]) -> dict[str, object]:
                request = envelope["request"]
                self.assertEqual(
                    envelope["caller"], {"run_id": run_id, "token_id": token_id}
                )
                return {
                    "ok": True,
                    "result": {
                        "identity": {
                            "kind": "specialist",
                            "instance": "scout",
                            "run_id": run_id,
                        },
                        "response": {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "result": {},
                        },
                    },
                }

            with OneShotUnixServer(authenticated_socket, allow), mock.patch.dict(
                os.environ,
                {
                    "FLEET_CONTROL_SOCKET": str(authenticated_socket),
                    fleet_agent_mcp.PREFLIGHT_RUN_ENV: run_id,
                    fleet_agent_mcp.PREFLIGHT_TOKEN_ENV: token_id,
                },
            ):
                ready = fleet_agent_mcp.preflight()
            self.assertEqual(ready["socket_probe"], "authenticated_ping")
            self.assertNotIn(run_id, json.dumps(ready, sort_keys=True))
            self.assertNotIn(token_id, json.dumps(ready, sort_keys=True))

    def test_reply_limit_covers_the_full_artifact_contract(self) -> None:
        largest_base64 = ((fleet_artifacts.MAX_ARTIFACT_BYTES + 2) // 3) * 4
        self.assertGreater(fleet_agent_mcp.MAX_REPLY_FRAME_BYTES, largest_base64)


class FleetAgentMCPUnixIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)
        self.runs, self.mission_id, self.lead_run_id = create_running_mission(
            self.tmp, feature="agent-mcp"
        )
        self.control = fleet_control.FleetControl(self.runs, self.mission_id)
        self.run_by_instance: dict[str, str] = {}
        self.prompt_by_instance: dict[str, str] = {}
        self.lifecycle = fleet_control_service.ControlLifecycle(
            self.runs, self.mission_id
        )
        self.lifecycle.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        if self.lifecycle.lifecycle_path.exists():
            try:
                self.lifecycle.stop()
            except fleet_control_service.ControlServiceError:
                pass

    def fake_run(self, command: list[str], *, runs_dir: Path, timeout=None):
        del timeout
        if Path(command[0]).name == "fleet-wait.sh":
            rows = [
                {"run_id": run_id, "status": "succeeded"}
                for run_id in self.run_by_instance.values()
                if any(run_id in item for item in command)
            ]
            return subprocess.CompletedProcess(
                command, 0, "".join(json.dumps(row) + "\n" for row in rows), ""
            )
        feature, instance, prompt = command[1:4]
        run_id = (
            command[command.index("--run-id") + 1]
            if "--run-id" in command
            else str(uuid.uuid4())
        )
        self.run_by_instance[instance] = run_id
        self.prompt_by_instance[instance] = prompt
        with (runs_dir / f"fleet-{feature}.ledger.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-16T00:00:00Z",
                        "run_id": run_id,
                        "feature": feature,
                        "instance": instance,
                        "task_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "status": "dispatched",
                    }
                )
                + "\n"
            )
        (runs_dir / f"fleet-{feature}.ledger.jsonl").chmod(0o600)
        self.write_frontier_lease(instance, run_id, prompt)
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"run_id": run_id}), ""
        )

    def write_frontier_lease(
        self,
        instance: str,
        run_id: str,
        prompt: str,
    ) -> None:
        feature = self.control.state()["feature"]
        manifest = self.control.manifest()
        member = self.control.members()[instance]
        name = f"{feature}.{instance}.lock"
        lock_root = self.runs / "locks"
        lock_root.mkdir(mode=0o700, exist_ok=True)
        lock_root.chmod(0o700)
        lease = lock_root / name
        lease.mkdir(mode=0o700, exist_ok=True)
        lease.chmod(0o700)
        metadata = {
            "schema_version": 1,
            "run_id": run_id,
            "feature": feature,
            "instance": instance,
            "role": member["role_type"],
            "phase": member["phase"],
            "resource_class": "remote",
            "runner": "interactive",
            "task_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "workspace_uuid": manifest["workspace_uuid"].upper(),
            "surface_uuid": manifest[f"{instance}.uuid"].upper(),
            "acquired_at": "2026-07-16T00:00:00+00:00",
            "pid": None,
            "pgid": None,
            "kind": name,
        }
        path = lease / "lease.json"
        path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def dispatch(self, instance: str = "scout", **overrides: object) -> dict:
        values: dict[str, object] = {
            "recipient_instance": instance,
            "capability": {
                "scout": "recon",
                "challenger": "challenge",
                "verifier": "verify",
            }[instance],
            "objective": f"exercise {instance} proxy authorization",
            "idempotency_key": f"agent-mcp:{instance}:{len(self.run_by_instance)}",
        }
        values.update(overrides)
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            return self.control.dispatch(**values)

    def mark_succeeded(self, instance: str, content: bytes) -> None:
        run_id = self.run_by_instance[instance]
        result = self.runs / "results" / "agent-mcp" / f"{run_id}.txt"
        result.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result.parent.chmod(0o700)
        result.write_bytes(content)
        result.chmod(0o600)
        result = result.resolve(strict=True)
        member = self.control.members()[instance]
        with (self.runs / "fleet-agent-mcp.ledger.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-16T00:01:00Z",
                        "run_id": run_id,
                        "feature": "agent-mcp",
                        "instance": instance,
                        "task_sha256": hashlib.sha256(
                            self.prompt_by_instance[instance].encode()
                        ).hexdigest(),
                        "status": "succeeded",
                        "result_file": str(result),
                        "provider": member["provider"],
                        "model": member["model"],
                        "variant": member.get("variant"),
                    }
                )
                + "\n"
            )
        (self.runs / "fleet-agent-mcp.ledger.jsonl").chmod(0o600)

    def proxy_call(
        self,
        socket_path: Path,
        name: str,
        arguments: dict[str, object],
        caller: dict[str, str],
    ) -> dict[str, object]:
        request = tool_call(
            name,
            {
                **arguments,
                "_caller_run_id": caller["run_id"],
                "_caller_token_id": caller["token_id"],
            },
        )
        result = subprocess.run(
            [sys.executable, str(PROXY)],
            input=json.dumps(request) + "\n",
            env={**os.environ, "FLEET_CONTROL_SOCKET": str(socket_path)},
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_authenticated_preflight_proves_real_socket_callability(self) -> None:
        scout = self.dispatch()
        endpoint = self.lifecycle.instance_socket_paths["scout"]
        with mock.patch.dict(
            os.environ,
            {
                "FLEET_CONTROL_SOCKET": str(endpoint),
                fleet_agent_mcp.PREFLIGHT_RUN_ENV: scout["run_id"],
                fleet_agent_mcp.PREFLIGHT_TOKEN_ENV: scout["token_id"],
            },
        ):
            ready = fleet_agent_mcp.preflight()
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["socket_probe"], "authenticated_ping")
        serialized = json.dumps(ready, sort_keys=True)
        self.assertNotIn(scout["run_id"], serialized)
        self.assertNotIn(scout["token_id"], serialized)

    def test_real_socket_grants_one_artifact_and_denies_sibling_base_and_terminal(self) -> None:
        granted_content = b"g" * 2_100_000
        own_artifact = fleet_artifacts.put_bytes(
            self.runs, self.mission_id, granted_content
        )
        lead = self.control.members()["lead"]
        mission_state.append_event(
            self.runs,
            self.mission_id,
            kind="lead_result_recorded",
            actor="CONTROL",
            idempotency_key="agent-mcp:lead-artifact",
            payload={
                "run_id": self.lead_run_id,
                "artifact_id": own_artifact["artifact_id"],
                "result_file": own_artifact["path"],
                "provider": lead["provider"],
                "model": lead["model"],
                "variant": lead.get("variant"),
            },
        )
        scout = self.dispatch(input_artifact_ids=[own_artifact["artifact_id"]])
        sibling = self.dispatch("challenger")
        self.mark_succeeded("challenger", b"sibling result must remain private\n")
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            waited = self.control.wait([sibling["run_id"]], timeout_seconds=1)
        sibling_artifact = waited["results"][0]["artifact_id"]
        caller = {"run_id": scout["run_id"], "token_id": scout["token_id"]}
        endpoint = self.lifecycle.instance_socket_paths["scout"]

        granted = self.proxy_call(
            endpoint,
            "get_result",
            {"artifact_id": own_artifact["artifact_id"]},
            caller,
        )
        self.assertFalse(granted["result"]["isError"])
        payload = json.loads(granted["result"]["content"][0]["text"])
        self.assertEqual(payload["artifact_id"], own_artifact["artifact_id"])
        self.assertEqual(payload["bytes"], len(granted_content))

        denied = self.proxy_call(
            endpoint,
            "get_result",
            {"artifact_id": sibling_artifact},
            caller,
        )
        self.assertTrue(denied["result"]["isError"])
        denied_text = json.dumps(denied, sort_keys=True)
        self.assertNotIn("sibling result must remain private", denied_text)
        self.assertNotIn(str(self.runs), denied_text)
        self.assertNotIn("token_path", denied_text)
        self.assertNotIn(scout["token_id"], denied_text)

        base_denied = self.proxy_call(
            self.lifecycle.socket_path,
            "inspect_mission",
            {},
            caller,
        )
        self.assertTrue(base_denied["result"]["isError"])

        with (self.runs / "fleet-agent-mcp.ledger.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-16T00:02:00Z",
                        "run_id": scout["run_id"],
                        "feature": "agent-mcp",
                        "instance": "scout",
                        "task_sha256": hashlib.sha256(
                            self.prompt_by_instance["scout"].encode()
                        ).hexdigest(),
                        "status": "failed",
                    }
                )
                + "\n"
            )
        terminal = self.proxy_call(endpoint, "inspect_mission", {}, caller)
        self.assertTrue(terminal["result"]["isError"])
        terminal_text = json.dumps(terminal, sort_keys=True)
        self.assertNotIn(scout["token_id"], terminal_text)
        self.assertNotIn(str(self.runs), terminal_text)


if __name__ == "__main__":
    unittest.main()
