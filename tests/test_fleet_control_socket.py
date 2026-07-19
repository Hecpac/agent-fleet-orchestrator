from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from tests.mission_control_test_support import create_running_mission

import fleet_control
import fleet_admission
import fleet_artifacts
import fleet_control_service
import fleet_ledger
import fleet_mcp
import fleet_mission_state as mission_state


class FleetControlSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)
        self.runs, self.mission_id, self.lead_run_id = create_running_mission(
            self.tmp, feature="socket"
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

    def test_assurance_control_uses_distinct_immutable_service_generation(self) -> None:
        assurance_preset = self.control.compiled["resolved"]["assurance_preset"]
        assured = fleet_control_service.ControlLifecycle(
            self.runs, self.mission_id, preset=assurance_preset
        )
        self.assertEqual(
            self.lifecycle.control_relative,
            Path("missions") / self.mission_id / "control",
        )
        self.assertEqual(
            assured.control_relative,
            Path("missions") / self.mission_id / "control-assured",
        )
        self.assertNotEqual(assured.lifecycle_path, self.lifecycle.lifecycle_path)

    def test_default_socket_namespace_isolates_same_mission_across_runs_roots(
        self,
    ) -> None:
        first_root = self.tmp / "parallel-first"
        second_root = self.tmp / "parallel-second"
        first_runs, first_mission_id, _ = create_running_mission(
            first_root, feature="socket"
        )
        second_runs, second_mission_id, _ = create_running_mission(
            second_root, feature="socket"
        )
        self.assertEqual(first_mission_id, second_mission_id)
        self.assertEqual(first_mission_id, self.mission_id)

        with mock.patch.dict(os.environ):
            os.environ.pop("FLEET_CONTROL_SOCKET_DIR", None)
            first = fleet_control_service.ControlLifecycle(first_runs, first_mission_id)
            second = fleet_control_service.ControlLifecycle(
                second_runs, second_mission_id
            )
            first_assured = fleet_control_service.ControlLifecycle(
                first_runs,
                first_mission_id,
                preset=self.control.compiled["resolved"]["assurance_preset"],
            )
            second_assured = fleet_control_service.ControlLifecycle(
                second_runs,
                second_mission_id,
                preset=self.control.compiled["resolved"]["assurance_preset"],
            )

        self.assertNotEqual(first.socket_root, second.socket_root)
        self.assertEqual(first.socket_root, first_assured.socket_root)
        self.assertEqual(second.socket_root, second_assured.socket_root)
        first_endpoints = {
            first.socket_path,
            *first.instance_socket_paths.values(),
        }
        second_endpoints = {
            second.socket_path,
            *second.instance_socket_paths.values(),
        }
        self.assertTrue(first_endpoints.isdisjoint(second_endpoints))
        self.assertTrue(
            {
                first_assured.socket_path,
                *first_assured.instance_socket_paths.values(),
            }.isdisjoint(
                {
                    second_assured.socket_path,
                    *second_assured.instance_socket_paths.values(),
                }
            )
        )
        self.addCleanup(first.stop_if_present)
        self.addCleanup(second.stop_if_present)
        previous_umask = os.umask(0o027)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                started = list(executor.map(lambda item: item.start(), (first, second)))
            observed_umask = os.umask(0o027)
            self.assertEqual(observed_umask, 0o027)
        finally:
            os.umask(previous_umask)
        self.assertTrue(all(item["started"] for item in started))
        self.assertEqual(first.health()["mission_id"], first_mission_id)
        self.assertEqual(second.health()["mission_id"], second_mission_id)

        first_live_endpoints = {
            first.socket_path,
            *first.instance_socket_paths.values(),
        }
        second_live_endpoints = {
            second.socket_path,
            *second.instance_socket_paths.values(),
        }
        staging_paths = {
            first.socket_root / f".fc-{'0' * 32}-000.stage",
            second.socket_root / f".fc-{'0' * 32}-000.stage",
        }
        self.assertTrue(
            all(
                len(os.fsencode(path)) < 100
                for path in first_live_endpoints | second_live_endpoints | staging_paths
            )
        )
        self.assertTrue(
            all(
                stat.S_IMODE(path.stat().st_mode) == 0o600
                for path in first_live_endpoints | second_live_endpoints
            )
        )

        first.stop()
        self.assertFalse(any(path.exists() for path in first_live_endpoints))
        self.assertTrue(all(path.exists() for path in second_live_endpoints))
        self.assertEqual(second.health()["mission_id"], second_mission_id)
        second.stop()
        self.assertFalse(any(path.exists() for path in second_live_endpoints))

    def request(
        self,
        endpoint_instance: str,
        caller: dict[str, object],
        method: str,
        **request: object,
    ) -> dict:
        return fleet_control_service.send_request(
            self.lifecycle.instance_socket_paths[endpoint_instance],
            {
                "schema_version": 2,
                "caller": caller,
                "request": {"jsonrpc": "2.0", "id": 1, "method": method, **request},
            },
        )

    @staticmethod
    def raw_request(path: Path, raw: bytes) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(path))
            connection.sendall(raw)
            connection.shutdown(socket.SHUT_WR)
            chunks = bytearray()
            while not chunks.endswith(b"\n"):
                chunk = connection.recv(65_536)
                if not chunk:
                    break
                chunks.extend(chunk)
        return json.loads(chunks)

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
        fleet_ledger.append_record(
            runs_dir / f"fleet-{feature}.ledger.jsonl",
            {
                "timestamp": "2026-07-16T00:00:00Z",
                "run_id": run_id,
                "feature": feature,
                "instance": instance,
                "task_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "status": "dispatched",
            },
        )
        self.write_frontier_lease(instance, run_id, prompt)
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"run_id": run_id}), ""
        )

    def write_frontier_lease(
        self,
        instance: str,
        run_id: str,
        prompt: str,
        *,
        overrides: dict[str, object] | None = None,
        raw: str | None = None,
    ) -> Path:
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
        metadata: dict[str, object] = {
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
        if overrides:
            metadata.update(overrides)
        path = lease / "lease.json"
        path.write_text(
            raw if raw is not None else json.dumps(metadata, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return lease

    def append_lifecycle(
        self,
        instance: str,
        run_id: str,
        prompt: str,
        status: str,
    ) -> None:
        feature = self.control.state()["feature"]
        fleet_ledger.append_record(
            self.runs / f"fleet-{feature}.ledger.jsonl",
            {
                "timestamp": "2026-07-16T00:00:00Z",
                "run_id": run_id,
                "feature": feature,
                "instance": instance,
                "task_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "status": status,
            },
        )

    def dispatch(self, instance: str = "scout", **overrides: object) -> dict:
        values: dict[str, object] = {
            "recipient_instance": instance,
            "capability": {
                "scout": "recon",
                "challenger": "challenge",
                "verifier": "verify",
            }[instance],
            "objective": f"exercise {instance} authorization",
            "idempotency_key": f"socket:{instance}:{len(self.run_by_instance)}",
        }
        values.update(overrides)
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            return self.control.dispatch(**values)

    def mark_succeeded(self, instance: str, content: bytes) -> None:
        run_id = self.run_by_instance[instance]
        feature = self.control.state()["feature"]
        result = self.runs / "results" / feature / f"{run_id}.txt"
        result.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result.parent.chmod(0o700)
        result.write_bytes(content)
        result.chmod(0o600)
        result = result.resolve(strict=True)
        member = self.control.members()[instance]
        fleet_ledger.append_record(
            self.runs / f"fleet-{feature}.ledger.jsonl",
            {
                "timestamp": "2026-07-16T00:01:00Z",
                "run_id": run_id,
                "feature": feature,
                "instance": instance,
                "task_sha256": hashlib.sha256(
                    self.prompt_by_instance[instance].encode()
                ).hexdigest(),
                "status": "succeeded",
                "result_file": str(result),
                "provider": member["provider"],
                "model": member["model"],
                "variant": member.get("variant"),
            },
        )

    @staticmethod
    def payload(reply: dict) -> dict:
        response = reply["result"]["response"]
        return json.loads(response["result"]["content"][0]["text"])

    def assert_specialist_denied(self, reply: dict) -> None:
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "specialist control request failed closed")
        self.assertNotIn(str(self.runs), json.dumps(reply, sort_keys=True))

    def test_specialist_socket_rejects_lead_impersonation(self) -> None:
        impersonated = self.request(
            "scout",
            {
                "instance": "lead",
                "run_id": self.lead_run_id,
                "token_id": str(uuid.uuid4()),
            },
            "tools/call",
            params={
                "name": "complete",
                "arguments": {
                    "artifact_id": "a" * 64,
                    "summary": "forged completion",
                    "idempotency_key": "forged:complete",
                },
            },
        )
        self.assert_specialist_denied(impersonated)

        wrong = fleet_control_service.send_request(
            self.lifecycle.socket_path,
            {
                "schema_version": 2,
                "caller": {"run_id": str(uuid.uuid4()), "token_id": str(uuid.uuid4())},
                "request": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            },
        )
        self.assertFalse(wrong["ok"])
        self.assertIn("health-only", wrong["error"])

    def test_specialist_requires_dispatched_lifecycle_and_exact_live_lease(
        self,
    ) -> None:
        # The checked-in orchestration/runs root is repository-readable (0755),
        # while every security-sensitive descendant remains private.
        self.runs.chmod(0o755)
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            delegated = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="bind identity only after frontier acceptance",
                idempotency_key="socket:lifecycle-lease",
            )
        instance = "scout"
        run_id = delegated["run_id"]
        prompt = self.prompt_by_instance[instance]
        envelope = {
            "schema_version": 2,
            "caller": {"run_id": run_id, "token_id": delegated["token_id"]},
            "request": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        }
        allowed = fleet_mcp.handle_socket_envelope(
            self.control, envelope, endpoint_instance=instance
        )
        self.assertEqual(allowed["identity"]["run_id"], run_id)

        lifecycle = self.runs / "fleet-socket.ledger.jsonl"
        durable_lifecycle = lifecycle.read_bytes()
        lifecycle.unlink()
        with self.assertRaisesRegex(
            fleet_control.FleetControlError, "lifecycle evidence"
        ):
            fleet_mcp.handle_socket_envelope(
                self.control, envelope, endpoint_instance=instance
            )
        lifecycle.write_bytes(durable_lifecycle.replace(b"dispatched", b"preparing"))
        lifecycle.chmod(0o600)
        with self.assertRaisesRegex(
            fleet_control.FleetControlError, "lifecycle evidence"
        ):
            fleet_mcp.handle_socket_envelope(
                self.control, envelope, endpoint_instance=instance
            )
        lifecycle.write_bytes(durable_lifecycle)
        lifecycle.chmod(0o600)

        lease = self.write_frontier_lease(instance, run_id, prompt)
        (lease / "lease.json").unlink()
        lease.rmdir()
        with self.assertRaisesRegex(
            fleet_control.FleetControlError, "active frontier lease"
        ):
            fleet_mcp.handle_socket_envelope(
                self.control, envelope, endpoint_instance=instance
            )
        self.write_frontier_lease(
            instance, run_id, prompt, overrides={"run_id": str(uuid.uuid4())}
        )
        with self.assertRaisesRegex(
            fleet_control.FleetControlError, "active frontier lease"
        ):
            fleet_mcp.handle_socket_envelope(
                self.control, envelope, endpoint_instance=instance
            )
        self.write_frontier_lease(instance, run_id, prompt, raw="{malformed\n")
        with self.assertRaisesRegex(
            fleet_control.FleetControlError, "active frontier lease"
        ):
            fleet_mcp.handle_socket_envelope(
                self.control, envelope, endpoint_instance=instance
            )
        self.write_frontier_lease(instance, run_id, prompt)
        self.append_lifecycle(
            instance,
            run_id,
            prompt,
            "failed",
        )
        with self.assertRaisesRegex(fleet_control.FleetControlError, "terminal"):
            fleet_mcp.handle_socket_envelope(
                self.control,
                envelope,
                endpoint_instance=instance,
            )

    def test_bound_specialist_token_limits_tools_and_delegated_scope(self) -> None:
        delegated = self.dispatch(
            can_delegate=True,
            allowed_capabilities=["challenge"],
            remaining_budget=1,
        )
        caller = {
            "instance": "scout",
            "run_id": delegated["run_id"],
            "token_id": delegated["token_id"],
        }
        listed = self.request("scout", caller, "tools/list")
        self.assertTrue(listed["ok"], listed)
        endpoint_spoof = self.request(
            "challenger",
            {"run_id": delegated["run_id"], "token_id": delegated["token_id"]},
            "tools/list",
        )
        self.assert_specialist_denied(endpoint_spoof)

        denied = self.request(
            "scout",
            caller,
            "tools/call",
            params={
                "name": "complete",
                "arguments": {
                    "artifact_id": "a" * 64,
                    "summary": "x",
                    "idempotency_key": "x",
                },
            },
        )
        self.assert_specialist_denied(denied)

        wrong_scope = self.request(
            "scout",
            caller,
            "tools/call",
            params={
                "name": "dispatch",
                "arguments": {
                    "recipient_instance": "verifier",
                    "capability": "verify",
                    "objective": "escape scope",
                    "idempotency_key": "socket:escape",
                    "parent_run_id": delegated["run_id"],
                    "token_id": delegated["token_id"],
                },
            },
        )
        self.assert_specialist_denied(wrong_scope)

        escalated = self.request(
            "scout",
            caller,
            "tools/call",
            params={
                "name": "request_assurance",
                "arguments": {
                    "risk": "high",
                    "categories": ["production"],
                    "reason": "specialist observed a production effect",
                    "idempotency_key": "socket:assurance",
                },
            },
        )
        self.assertTrue(escalated["ok"], escalated)
        escalated_payload = self.payload(escalated)
        self.assertEqual(
            set(escalated_payload),
            {"mission_id", "status", "risk", "risk_categories"},
        )
        self.assertEqual(escalated_payload["status"], "running")
        self.assertEqual(escalated_payload["risk"], "high")
        self.assertNotIn("token_id", json.dumps(escalated_payload, sort_keys=True))
        actor = f"specialist:scout:{delegated['run_id']}"
        assurance_events = [
            event
            for event in self.control.events()
            if event["kind"] in {"risk_escalated", "assurance_requested"}
        ]
        self.assertTrue(assurance_events)
        self.assertEqual(
            [event["kind"] for event in assurance_events], ["risk_escalated"]
        )
        self.assertTrue(all(event["actor"] == actor for event in assurance_events))

    def test_socket_strict_json_and_closed_nested_schema_have_zero_effects(
        self,
    ) -> None:
        parent = self.dispatch(
            can_delegate=True,
            allowed_capabilities=["verify"],
            remaining_budget=2,
        )
        caller = {"run_id": parent["run_id"], "token_id": parent["token_id"]}
        endpoint = self.lifecycle.instance_socket_paths["scout"]

        def snapshot() -> tuple[bytes, bytes, dict[str, bytes]]:
            mission_ledger = mission_state.ledger_path(
                self.runs, self.mission_id
            ).read_bytes()
            legacy = (self.runs / "fleet-socket.ledger.jsonl").read_bytes()
            token_root = (
                mission_state.mission_root(self.runs, self.mission_id) / "tokens"
            )
            tokens = {
                str(path.relative_to(token_root)): path.read_bytes()
                for path in token_root.rglob("*")
                if path.is_file()
            }
            return mission_ledger, legacy, tokens

        before = snapshot()
        invalid_nested = self.request(
            "scout",
            caller,
            "tools/call",
            params={
                "name": "dispatch_many",
                "arguments": {
                    "requests": [
                        {
                            "recipient_instance": "verifier",
                            "capability": "verify",
                            "objective": "must fail before dispatch",
                            "idempotency_key": "socket:closed-nested",
                            "parent_run_id": parent["run_id"],
                            "token_id": parent["token_id"],
                            "token_path": "/tmp/ARTIFICIAL_TOKEN_SECRET.json",
                        }
                    ]
                },
            },
        )
        self.assertTrue(invalid_nested["ok"], invalid_nested)
        tool_result = invalid_nested["result"]["response"]["result"]
        self.assertTrue(tool_result["isError"])
        self.assertEqual(
            tool_result["content"][0]["text"],
            fleet_mcp.SPECIALIST_DENIAL_MESSAGE,
        )
        self.assertNotIn("ARTIFICIAL_TOKEN_SECRET", json.dumps(invalid_nested))
        self.assertEqual(snapshot(), before)

        ping_envelope = {
            "schema_version": 2,
            "caller": caller,
            "request": {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        }
        encoded = json.dumps(ping_envelope, separators=(",", ":"))
        duplicate = (
            encoded.replace(
                '"schema_version":2',
                '"schema_version":2,"schema_version":2',
                1,
            ).encode("utf-8")
            + b"\n"
        )
        nonfinite_envelope = {
            **ping_envelope,
            "request": {
                "jsonrpc": "2.0",
                "id": float("nan"),
                "method": "ping",
            },
        }
        nonfinite = (
            json.dumps(nonfinite_envelope, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        for raw in (duplicate, nonfinite):
            with self.subTest(raw=raw[:100]):
                denied = self.raw_request(endpoint, raw)
                self.assert_specialist_denied(denied)
                self.assertEqual(snapshot(), before)

    def test_stdio_strict_json_and_specialist_errors_never_export_secrets(self) -> None:
        inputs = (
            '{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n'
            '{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n'
            '{"jsonrpc":"2.0","id":Infinity,"method":"ping"}\n'
            '{"jsonrpc":"2.0","id":-Infinity,"method":"ping"}\n'
        )
        result = subprocess.run(
            [
                sys.executable,
                str(Path(fleet_mcp.__file__)),
                "--runs-dir",
                str(self.runs),
                "--mission-id",
                self.mission_id,
            ],
            input=inputs,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            [reply["error"]["code"] for reply in replies],
            [-32700, -32700, -32700, -32700],
        )
        with self.assertRaisesRegex(
            ValueError,
            "Out of range float values|non-finite JSON number",
        ):
            fleet_mcp._dumps_strict({"unsafe": float("inf")})

        token_id = str(uuid.uuid4())
        secret = f"/tmp/private-control.sock token={token_id} ARTIFICIAL_SECRET"
        exploding = mock.Mock()
        exploding.dispatch.side_effect = fleet_control.FleetControlError(secret)
        response = fleet_mcp.handle(
            exploding,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "dispatch",
                    "arguments": {
                        "recipient_instance": "verifier",
                        "capability": "verify",
                        "objective": "exercise error projection",
                        "idempotency_key": "socket:error-projection",
                    },
                },
            },
            identity={"kind": "specialist"},
        )
        serialized = json.dumps(response, sort_keys=True)
        self.assertIn(fleet_mcp.SPECIALIST_DENIAL_MESSAGE, serialized)
        for value in (secret, token_id, "/tmp/private-control.sock"):
            self.assertNotIn(value, serialized)

    def test_specialist_dispatch_and_wait_redact_tokens_and_store_paths(self) -> None:
        parent = self.dispatch(
            can_delegate=True,
            allowed_capabilities=["verify"],
            remaining_budget=1,
        )
        caller = {"run_id": parent["run_id"], "token_id": parent["token_id"]}
        dispatch_envelope = {
            "schema_version": 2,
            "caller": caller,
            "request": {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "dispatch",
                    "arguments": {
                        "recipient_instance": "verifier",
                        "capability": "verify",
                        "objective": "produce a direct child result",
                        "idempotency_key": "socket:redaction:child",
                        "parent_run_id": parent["run_id"],
                        "token_id": parent["token_id"],
                    },
                },
            },
        }
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            dispatched = fleet_mcp.handle_socket_envelope(
                self.control, dispatch_envelope, endpoint_instance="scout"
            )
        dispatched_payload = json.loads(
            dispatched["response"]["result"]["content"][0]["text"]
        )
        serialized = json.dumps(dispatched_payload, sort_keys=True)
        self.assertNotIn("token_id", serialized)
        self.assertNotIn("token_path", serialized)
        child_run = dispatched_payload["run_id"]

        self.mark_succeeded("verifier", b"redacted direct child result\n")
        wait_envelope = {
            "schema_version": 2,
            "caller": caller,
            "request": {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "wait",
                    "arguments": {"run_ids": [child_run], "timeout_seconds": 1},
                },
            },
        }
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            waited = fleet_mcp.handle_socket_envelope(
                self.control, wait_envelope, endpoint_instance="scout"
            )
        waited_payload = json.loads(waited["response"]["result"]["content"][0]["text"])
        waited_serialized = json.dumps(waited_payload, sort_keys=True)
        self.assertNotIn("token_id", waited_serialized)
        self.assertNotIn("token_path", waited_serialized)
        self.assertNotIn('"path"', waited_serialized)
        self.assertNotIn(str(self.runs), waited_serialized)
        self.assertEqual(waited_payload["results"][0]["run_id"], child_run)
        self.assertIn("artifact_id", waited_payload["results"][0])

    def test_terminal_cancelled_and_expired_tokens_fail_closed(self) -> None:
        delegated = self.dispatch()
        caller = {"run_id": delegated["run_id"], "token_id": delegated["token_id"]}
        ledger = self.runs / "fleet-socket.ledger.jsonl"
        fleet_ledger.append_record(
            ledger,
            {
                "timestamp": "2026-07-16T00:02:00Z",
                "run_id": delegated["run_id"],
                "feature": "socket",
                "instance": "scout",
                "status": "failed",
                "task_sha256": hashlib.sha256(
                    self.prompt_by_instance["scout"].encode()
                ).hexdigest(),
            },
        )
        terminal = self.request("scout", caller, "tools/list")
        self.assert_specialist_denied(terminal)
        current = self.control.state()
        specialist_owner = current["run_owners"][delegated["run_id"]]
        specialist_admission = current["admissions"][specialist_owner["owner_id"]]
        fleet_admission.finalize(
            self.runs,
            self.mission_id,
            admission_id=specialist_owner["owner_id"],
            recipient_instance=specialist_admission["recipient_instance"],
            writer=bool(specialist_admission["writer"]),
            terminal_evidence={
                "schema_version": 1,
                "source_event_sha256": mission_state.sha256(
                    {"run_id": delegated["run_id"], "status": "failed"}
                ),
                "run_id": delegated["run_id"],
                "task_sha256": specialist_admission["task_sha256"],
                "status": "failed",
            },
            reason="test specialist terminal",
            idempotency_key="admission:test-specialist:finalize",
        )
        lead_admission = self.control.state()["admissions"][
            current["lead_admission_id"]
        ]
        fleet_admission.finalize(
            self.runs,
            self.mission_id,
            admission_id=current["lead_admission_id"],
            recipient_instance=lead_admission["recipient_instance"],
            writer=bool(lead_admission["writer"]),
            terminal_evidence={
                "schema_version": 1,
                "source_event_sha256": mission_state.sha256(
                    {"run_id": lead_admission["run_id"], "status": "failed"}
                ),
                "run_id": lead_admission["run_id"],
                "task_sha256": lead_admission["task_sha256"],
                "status": "failed",
            },
            reason="test Lead terminal",
            idempotency_key="admission:test-lead:finalize",
        )
        mission_state.append_terminal(
            self.runs,
            self.mission_id,
            status="failed",
            reason="test mission terminal",
            idempotency_key="terminal:mission",
        )
        mission_terminal = self.request("scout", caller, "tools/list")
        self.assert_specialist_denied(mission_terminal)

        other_root = self.tmp / "cancelled"
        runs, mission_id, _ = create_running_mission(
            other_root, feature="socket-cancel"
        )
        control = fleet_control.FleetControl(runs, mission_id)
        lifecycle = fleet_control_service.ControlLifecycle(runs, mission_id)
        lifecycle.start()
        self.addCleanup(
            lambda: lifecycle.stop() if lifecycle.lifecycle_path.exists() else None
        )
        original = self.control, self.runs, self.mission_id, self.lifecycle
        self.control, self.runs, self.mission_id, self.lifecycle = (
            control,
            runs,
            mission_id,
            lifecycle,
        )
        self.run_by_instance = {}
        self.prompt_by_instance = {}
        cancelled_run = self.dispatch()
        mission_state.append_event(
            runs,
            mission_id,
            kind="run_cancel_requested",
            actor="CONTROL",
            idempotency_key="cancel:scout",
            payload={"run_id": cancelled_run["run_id"], "reason": "test cancellation"},
        )
        cancelled = self.request(
            "scout",
            {"run_id": cancelled_run["run_id"], "token_id": cancelled_run["token_id"]},
            "tools/list",
        )
        self.assert_specialist_denied(cancelled)
        self.control, self.runs, self.mission_id, self.lifecycle = original

        expired_root = self.tmp / "expired"
        expired_runs, expired_mission, _ = create_running_mission(
            expired_root, feature="socket-expired"
        )
        expired_control = fleet_control.FleetControl(expired_runs, expired_mission)
        expired_lifecycle = fleet_control_service.ControlLifecycle(
            expired_runs, expired_mission
        )
        expired_lifecycle.start()
        self.addCleanup(
            lambda: (
                expired_lifecycle.stop()
                if expired_lifecycle.lifecycle_path.exists()
                else None
            )
        )
        original = self.control, self.runs, self.mission_id, self.lifecycle
        self.control, self.runs, self.mission_id, self.lifecycle = (
            expired_control,
            expired_runs,
            expired_mission,
            expired_lifecycle,
        )
        self.run_by_instance = {}
        self.prompt_by_instance = {}
        with mock.patch.object(
            fleet_control, "_deadline", return_value="2000-01-01T00:00:00+00:00"
        ):
            expired_run = self.dispatch()
        expired = self.request(
            "scout",
            {"run_id": expired_run["run_id"], "token_id": expired_run["token_id"]},
            "tools/list",
        )
        self.assert_specialist_denied(expired)
        self.control, self.runs, self.mission_id, self.lifecycle = original

    def test_mcp_revalidation_linearizes_cancel_and_finalize_before_execution(
        self,
    ) -> None:
        original_validate = fleet_mcp._validate_socket_caller

        cancelled = self.dispatch("scout")
        cancel_envelope = {
            "schema_version": 2,
            "caller": {
                "run_id": cancelled["run_id"],
                "token_id": cancelled["token_id"],
            },
            "request": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        }

        def cancel_after_auth(*args, **kwargs):
            identity = original_validate(*args, **kwargs)
            mission_state.append_event(
                self.runs,
                self.mission_id,
                kind="run_cancel_requested",
                actor="CONTROL",
                idempotency_key="race:cancel-after-auth",
                payload={
                    "run_id": cancelled["run_id"],
                    "reason": "linearize cancellation before MCP execution",
                },
            )
            return identity

        with (
            mock.patch.object(
                fleet_mcp,
                "_validate_socket_caller",
                side_effect=cancel_after_auth,
            ),
            mock.patch.object(fleet_mcp, "handle") as effect,
            self.assertRaisesRegex(
                fleet_control.FleetControlError,
                "revoked before control execution",
            ),
        ):
            fleet_mcp.handle_socket_envelope(
                self.control,
                cancel_envelope,
                endpoint_instance="scout",
            )
        effect.assert_not_called()

        finalized = self.dispatch("challenger")
        finalize_envelope = {
            "schema_version": 2,
            "caller": {
                "run_id": finalized["run_id"],
                "token_id": finalized["token_id"],
            },
            "request": {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        }

        def finalize_after_auth(*args, **kwargs):
            identity = original_validate(*args, **kwargs)
            current = self.control.state()
            owner = current["run_owners"][finalized["run_id"]]
            admission = current["admissions"][owner["owner_id"]]
            fleet_admission.finalize(
                self.runs,
                self.mission_id,
                admission_id=owner["owner_id"],
                recipient_instance=admission["recipient_instance"],
                writer=bool(admission["writer"]),
                terminal_evidence={
                    "schema_version": 1,
                    "source_event_sha256": mission_state.sha256(
                        {"run_id": finalized["run_id"], "status": "failed"}
                    ),
                    "run_id": finalized["run_id"],
                    "task_sha256": admission["task_sha256"],
                    "status": "failed",
                },
                reason="linearize finalization before MCP execution",
                idempotency_key="race:finalize-after-auth",
            )
            return identity

        with (
            mock.patch.object(
                fleet_mcp,
                "_validate_socket_caller",
                side_effect=finalize_after_auth,
            ),
            mock.patch.object(fleet_mcp, "handle") as effect,
            self.assertRaisesRegex(
                fleet_control.FleetControlError,
                "revoked before control execution",
            ),
        ):
            fleet_mcp.handle_socket_envelope(
                self.control,
                finalize_envelope,
                endpoint_instance="challenger",
            )
        effect.assert_not_called()

    def test_mutation_guard_rechecks_caller_after_socket_post_lock_barrier(
        self,
    ) -> None:
        delegated = self.dispatch("scout")
        envelope = {
            "schema_version": 2,
            "caller": {
                "run_id": delegated["run_id"],
                "token_id": delegated["token_id"],
            },
            "request": {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "request_human",
                    "arguments": {
                        "reason": "must not append after caller finalization",
                        "scope": "mission",
                        "idempotency_key": "race:post-lock-human",
                    },
                },
            },
        }
        original_handle = fleet_mcp.handle

        def finalize_after_post_lock(control, request, *, actor="lead", identity=None):
            current = self.control.state()
            owner = current["run_owners"][delegated["run_id"]]
            admission = current["admissions"][owner["owner_id"]]
            fleet_admission.finalize(
                self.runs,
                self.mission_id,
                admission_id=admission["admission_id"],
                recipient_instance=admission["recipient_instance"],
                writer=bool(admission["writer"]),
                terminal_evidence={
                    "schema_version": 1,
                    "source_event_sha256": mission_state.sha256(
                        {"run_id": delegated["run_id"], "status": "failed"}
                    ),
                    "run_id": delegated["run_id"],
                    "task_sha256": admission["task_sha256"],
                    "status": "failed",
                },
                reason="barrier finalized caller after socket lock",
                idempotency_key="race:post-lock-finalize",
            )
            return original_handle(control, request, actor=actor, identity=identity)

        with mock.patch.object(
            fleet_mcp, "handle", side_effect=finalize_after_post_lock
        ):
            reply = fleet_mcp.handle_socket_envelope(
                self.control, envelope, endpoint_instance="scout"
            )

        self.assertTrue(reply["response"]["result"]["isError"])
        self.assertEqual(
            reply["response"]["result"]["content"][0]["text"],
            fleet_mcp.SPECIALIST_DENIAL_MESSAGE,
        )
        self.assertFalse(
            any(
                event["kind"] == "human_approval_requested"
                and event["idempotency_key"] == "race:post-lock-human"
                for event in self.control.events()
            )
        )

    def test_dispatch_reservation_loses_race_to_caller_finalize_with_zero_effect(
        self,
    ) -> None:
        parent = self.dispatch(
            "scout",
            can_delegate=True,
            allowed_capabilities=["challenge"],
            remaining_budget=1,
        )
        child_key = "race:post-lock-dispatch"
        envelope = {
            "schema_version": 2,
            "caller": {
                "run_id": parent["run_id"],
                "token_id": parent["token_id"],
            },
            "request": {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "dispatch",
                    "arguments": {
                        "recipient_instance": "challenger",
                        "capability": "challenge",
                        "objective": "must lose to caller finalization",
                        "idempotency_key": child_key,
                        "parent_run_id": parent["run_id"],
                        "token_id": parent["token_id"],
                    },
                },
            },
        }
        original_reserve = fleet_admission.reserve_many
        finalized = False

        def finalize_then_reserve(*args, **kwargs):
            nonlocal finalized
            requests = kwargs.get("requests") or []
            if not finalized and any(
                request.get("request_key") == child_key for request in requests
            ):
                finalized = True
                current = self.control.state()
                owner = current["run_owners"][parent["run_id"]]
                admission = current["admissions"][owner["owner_id"]]
                fleet_admission.finalize(
                    self.runs,
                    self.mission_id,
                    admission_id=admission["admission_id"],
                    recipient_instance=admission["recipient_instance"],
                    writer=bool(admission["writer"]),
                    terminal_evidence={
                        "schema_version": 1,
                        "source_event_sha256": mission_state.sha256(
                            {"run_id": parent["run_id"], "status": "failed"}
                        ),
                        "run_id": parent["run_id"],
                        "task_sha256": admission["task_sha256"],
                        "status": "failed",
                    },
                    reason="caller finalized at child reservation barrier",
                    idempotency_key="race:dispatch-parent-finalized",
                )
            return original_reserve(*args, **kwargs)

        with (
            mock.patch.object(
                fleet_admission,
                "reserve_many",
                side_effect=finalize_then_reserve,
            ),
            mock.patch.object(fleet_control, "run_process") as effect,
        ):
            reply = fleet_mcp.handle_socket_envelope(
                self.control, envelope, endpoint_instance="scout"
            )

        self.assertTrue(finalized)
        self.assertTrue(reply["response"]["result"]["isError"])
        effect.assert_not_called()
        self.assertFalse(
            any(
                admission["request_key"] == child_key
                for admission in self.control.state()["admissions"].values()
            )
        )

    def test_specialist_acl_projects_only_caller_children_and_artifact_grants(
        self,
    ) -> None:
        own_artifact = fleet_artifacts.put_bytes(
            self.runs, self.mission_id, b"lead input granted to scout\n"
        )
        lead = self.control.members()["lead"]
        mission_state.append_event(
            self.runs,
            self.mission_id,
            kind="lead_result_recorded",
            actor="CONTROL",
            idempotency_key="lead:artifact",
            payload={
                "run_id": self.lead_run_id,
                "artifact_id": own_artifact["artifact_id"],
                "result_file": own_artifact["path"],
                "provider": lead["provider"],
                "model": lead["model"],
                "variant": lead.get("variant"),
            },
        )
        scout = self.dispatch(
            input_artifact_ids=[own_artifact["artifact_id"]],
            can_delegate=True,
            allowed_capabilities=["verify"],
            remaining_budget=2,
        )
        child = self.dispatch(
            "verifier",
            parent_run_id=scout["run_id"],
            token_id=scout["token_id"],
        )
        self.mark_succeeded("verifier", b"direct child result\n")
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            child_wait = self.control.wait([child["run_id"]], timeout_seconds=1)
        child_artifact = child_wait["results"][0]["artifact_id"]

        sibling = self.dispatch("challenger")
        self.mark_succeeded("challenger", b"unrelated sibling result\n")
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            sibling_wait = self.control.wait([sibling["run_id"]], timeout_seconds=1)
        sibling_artifact = sibling_wait["results"][0]["artifact_id"]
        caller = {"run_id": scout["run_id"], "token_id": scout["token_id"]}

        for artifact_id in (own_artifact["artifact_id"], child_artifact):
            granted = self.request(
                "scout",
                caller,
                "tools/call",
                params={
                    "name": "get_result",
                    "arguments": {"artifact_id": artifact_id},
                },
            )
            self.assertTrue(granted["ok"], granted)
            self.assertFalse(granted["result"]["response"]["result"]["isError"])

        denied = self.request(
            "scout",
            caller,
            "tools/call",
            params={
                "name": "get_result",
                "arguments": {"artifact_id": sibling_artifact},
            },
        )
        self.assert_specialist_denied(denied)

        inspected = self.request(
            "scout",
            caller,
            "tools/call",
            params={"name": "inspect_mission", "arguments": {}},
        )
        scoped = self.payload(inspected)
        self.assertIn(scout["delegation_id"], scoped["delegations"])
        self.assertIn(child["delegation_id"], scoped["delegations"])
        self.assertNotIn(sibling["delegation_id"], scoped["delegations"])
        self.assertNotIn(scout["token_id"], json.dumps(scoped, sort_keys=True))

        unrelated_wait = self.request(
            "scout",
            caller,
            "tools/call",
            params={"name": "wait", "arguments": {"run_ids": [sibling["run_id"]]}},
        )
        self.assert_specialist_denied(unrelated_wait)
        fleet_mcp._validate_socket_caller(
            self.control,
            "scout",
            caller,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "wait", "arguments": {"run_ids": [child["run_id"]]}},
            },
        )

        for name, arguments in (
            (
                "dispatch",
                {
                    "recipient_instance": "verifier",
                    "capability": "verify",
                    "objective": "must not receive sibling artifact",
                    "idempotency_key": "acl:dispatch-denied",
                    "parent_run_id": scout["run_id"],
                    "token_id": scout["token_id"],
                    "input_artifact_ids": [sibling_artifact],
                },
            ),
            (
                "relay_result",
                {
                    "artifact_id": sibling_artifact,
                    "recipient_instance": "verifier",
                    "capability": "verify",
                    "objective": "must not relay sibling artifact",
                    "idempotency_key": "acl:relay-denied",
                    "parent_run_id": scout["run_id"],
                    "token_id": scout["token_id"],
                },
            ),
        ):
            rejected = self.request(
                "scout",
                caller,
                "tools/call",
                params={"name": name, "arguments": arguments},
            )
            self.assert_specialist_denied(rejected)

    def test_socket_permissions_and_health_are_kernel_scoped(self) -> None:
        self.assertEqual(self.lifecycle.socket_path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.lifecycle.instance_socket_paths)
        for path in self.lifecycle.instance_socket_paths.values():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.lifecycle.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.lifecycle.socket_root.stat().st_mode & 0o777, 0o700)
        for path in (
            self.lifecycle.lifecycle_path,
            self.lifecycle.lock_path,
            self.lifecycle.root / "service.stdout.log",
            self.lifecycle.root / "service.stderr.log",
        ):
            info = path.lstat()
            self.assertTrue(stat.S_ISREG(info.st_mode))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual(info.st_uid, os.geteuid())
        health = fleet_control_service.send_request(
            self.lifecycle.socket_path, {"operation": "health"}
        )
        self.assertTrue(health["ok"])
        self.assertEqual(health["result"]["mission_id"], self.mission_id)
        self.assertEqual(health["result"]["preset"], self.lifecycle.preset)
        self.assertEqual(health["result"]["protocol"], "fleet-control-unix-v2")
        lifecycle = self.lifecycle._read_lifecycle()
        self.assertEqual(health["result"]["process_id"], lifecycle["pid"])
        with self.assertRaisesRegex(
            fleet_control_service.ControlServiceError,
            "kernel pid mismatch",
        ):
            fleet_control_service.send_request(
                self.lifecycle.socket_path,
                {"operation": "health"},
                expected_peer_pid=os.getpid(),
            )
        self.assertEqual(lifecycle["schema_version"], 3)
        self.assertEqual(lifecycle["preset"], self.lifecycle.preset)
        self.assertEqual(
            lifecycle["instance_sockets"],
            {
                instance: str(path)
                for instance, path in self.lifecycle.instance_socket_paths.items()
            },
        )

    def test_start_reconciles_the_same_healthy_process(self) -> None:
        first = self.lifecycle._read_lifecycle()
        reconciler = fleet_control_service.ControlLifecycle(self.runs, self.mission_id)

        result = reconciler.start()

        self.assertFalse(result["started"])
        self.assertEqual(result["lifecycle"]["pid"], first["pid"])
        self.assertEqual(result["health"]["mission_id"], self.mission_id)
        self.assertEqual(reconciler._read_lifecycle()["pid"], first["pid"])

    def test_start_reconciles_clean_exit_receipt_after_controller_crash(self) -> None:
        active = self.lifecycle._read_lifecycle()
        shutdown = fleet_control_service.send_request(
            self.lifecycle.socket_path,
            {
                "operation": "shutdown",
                "process_id": active["pid"],
                "launch_id": active["launch_id"],
            },
            expected_peer_pid=active["pid"],
        )
        self.assertTrue(shutdown["ok"], shutdown)

        deadline = time.monotonic() + 5
        while (
            not self.lifecycle.stopped_receipt_path.exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        self.assertTrue(self.lifecycle.stopped_receipt_path.exists())
        receipt_info = self.lifecycle.stopped_receipt_path.lstat()
        self.assertIsNone(self.lifecycle._read_lifecycle()["stopped_at"])

        reconciler = fleet_control_service.ControlLifecycle(self.runs, self.mission_id)
        with self.assertRaisesRegex(
            fleet_control_service.ControlServiceError,
            "already stopped",
        ):
            reconciler.start()

        terminal = reconciler._read_lifecycle()
        receipt = reconciler._read_stopped_receipt_optional(terminal)
        self.assertIsNotNone(terminal["stopped_at"])
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(terminal["stopped_at"], receipt["stopped_at"])
        current_receipt = reconciler.stopped_receipt_path.lstat()
        self.assertEqual(
            (current_receipt.st_dev, current_receipt.st_ino),
            (receipt_info.st_dev, receipt_info.st_ino),
        )

    def test_stop_uses_authenticated_socket_without_process_signals(self) -> None:
        original_kill = os.kill
        with mock.patch.object(
            fleet_control_service.os, "kill", wraps=original_kill
        ) as kill:
            stopped = self.lifecycle.stop()

        self.assertTrue(stopped["stopped"])
        self.assertIsNotNone(stopped["lifecycle"]["stopped_at"])
        kill.assert_not_called()
        self.assertFalse(self.lifecycle.socket_path.exists())
        receipt = self.lifecycle._read_stopped_receipt_optional(stopped["lifecycle"])
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(receipt["launch_id"], stopped["lifecycle"]["launch_id"])

    def test_owner_reaps_child_after_external_controller_marks_stopped(self) -> None:
        owner = self.lifecycle
        process = owner.process
        self.assertIsNotNone(process)
        assert process is not None
        pid = process.pid
        self.assertIs(
            fleet_control_service._LIVE_PROCESSES.pop(pid),
            process,
        )
        try:
            external = fleet_control_service.ControlLifecycle(
                self.runs, self.mission_id
            )
            stopped = external.stop()
            self.assertTrue(stopped["stopped"])
            self.assertIsNone(process.returncode)

            fleet_control_service._LIVE_PROCESSES[pid] = process
            again = owner.stop()
            self.assertFalse(again["stopped"])
            self.assertIsNotNone(process.returncode)
            self.assertNotIn(pid, fleet_control_service._LIVE_PROCESSES)
        finally:
            if process.returncode is None:
                fleet_control_service._LIVE_PROCESSES[pid] = process
                try:
                    owner.stop()
                except fleet_control_service.ControlServiceError:
                    pass

    def test_stop_cancels_stalled_handler_then_receipts_quiescence(self) -> None:
        blocker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        blocker.connect(str(next(iter(self.lifecycle.instance_socket_paths.values()))))
        blocker.settimeout(2)
        time.sleep(0.1)
        outcome: dict[str, object] = {}

        def stop() -> None:
            try:
                outcome["result"] = self.lifecycle.stop()
            except BaseException as exc:  # pragma: no cover - asserted below
                outcome["error"] = exc

        thread = threading.Thread(target=stop)
        started = time.monotonic()
        thread.start()
        try:
            thread.join(timeout=4)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 4)
            self.assertNotIn("error", outcome)
            self.assertTrue(outcome["result"]["stopped"])  # type: ignore[index]
            self.assertTrue(self.lifecycle.stopped_receipt_path.exists())
            while blocker.recv(65_536):
                pass
        finally:
            blocker.close()

    def test_resumed_stop_uses_durable_socket_root_not_current_environment(
        self,
    ) -> None:
        durable_root = self.lifecycle.socket_root
        alternate = self.tmp / "unrelated-socket-root"
        alternate.mkdir(mode=0o700)
        alternate.chmod(0o700)
        with mock.patch.dict(os.environ, {"FLEET_CONTROL_SOCKET_DIR": str(alternate)}):
            resumed = fleet_control_service.ControlLifecycle(self.runs, self.mission_id)
            self.assertEqual(resumed.socket_root, alternate)
            stopped = resumed.stop_if_present()

        self.assertTrue(stopped["stopped"])
        self.assertEqual(resumed.socket_root, durable_root)
        self.assertFalse((alternate / f"{self.mission_id}.sock").exists())

    def test_base_socket_substitution_is_rejected_and_not_unlinked(self) -> None:
        base = self.lifecycle.socket_path
        saved = base.with_name(f"{base.name}.saved")
        os.rename(base, saved)
        rogue = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            rogue.bind(str(base))
            os.chmod(base, 0o600)
            rogue_identity = base.lstat()
            with self.assertRaisesRegex(
                fleet_control_service.ControlServiceError,
                "endpoint identity mismatch",
            ):
                self.lifecycle.stop_if_present()
            current = base.lstat()
            self.assertEqual(
                (current.st_dev, current.st_ino),
                (rogue_identity.st_dev, rogue_identity.st_ino),
            )
        finally:
            rogue.close()
            base.unlink(missing_ok=True)
            os.rename(saved, base)

    def test_assurance_preset_selects_only_the_assurance_roster(self) -> None:
        resolved = self.control.compiled["resolved"]
        assurance = fleet_control.FleetControl(
            self.runs,
            self.mission_id,
            preset=resolved["assurance_preset"],
        )
        expected = {member["instance_id"] for member in resolved["assurance_instances"]}
        expected.add(resolved["assurance_lead"]["instance_id"])
        self.assertEqual(set(assurance.members()), expected)
        self.assertEqual(assurance.writer_instance(), "maker")

        lifecycle = fleet_control_service.ControlLifecycle(
            self.runs,
            self.mission_id,
            preset=resolved["assurance_preset"],
        )
        self.assertEqual(set(lifecycle.instance_socket_paths), expected - {"lead"})
        self.assertNotEqual(
            set(lifecycle.instance_socket_paths),
            set(self.lifecycle.instance_socket_paths),
        )


class ControlLifecyclePhysicalStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)
        self.runs, self.mission_id, _ = create_running_mission(
            self.tmp, feature="control-state"
        )
        # The repository's normal runs root is readable but not writable by peers.
        self.runs.chmod(0o755)
        self.lifecycle = fleet_control_service.ControlLifecycle(
            self.runs, self.mission_id
        )

    def create_control_root(self) -> None:
        self.lifecycle.root.mkdir(mode=0o700)
        self.lifecycle.root.chmod(0o700)

    def test_lifecycle_rejects_symlink_hardlink_and_wrong_mode(self) -> None:
        self.create_control_root()
        outside = self.tmp / "outside-lifecycle.json"
        outside.write_bytes(b"{}\n")
        outside.chmod(0o600)
        self.lifecycle.lifecycle_path.symlink_to(outside)
        with self.assertRaisesRegex(
            fleet_control_service.ControlServiceError, "unsafe Fleet Control state"
        ):
            self.lifecycle._read_lifecycle()

        self.lifecycle.lifecycle_path.unlink()
        os.link(outside, self.lifecycle.lifecycle_path)
        with self.assertRaisesRegex(
            fleet_control_service.ControlServiceError, "unsafe Fleet Control state"
        ):
            self.lifecycle._read_lifecycle()

        self.lifecycle.lifecycle_path.unlink()
        self.lifecycle.lifecycle_path.write_bytes(b"{}\n")
        self.lifecycle.lifecycle_path.chmod(0o644)
        with self.assertRaisesRegex(
            fleet_control_service.ControlServiceError, "unsafe Fleet Control state"
        ):
            self.lifecycle._read_lifecycle()

    def test_duplicate_lifecycle_key_blocks_stop_without_side_effects(self) -> None:
        started = self.lifecycle.start()
        self.assertTrue(started["started"])
        original = self.lifecycle.lifecycle_path.read_bytes()
        duplicate = original.replace(
            b'"schema_version":3',
            b'"schema_version":3,"schema_version":3',
            1,
        )
        self.assertNotEqual(duplicate, original)
        lifecycle = started["lifecycle"]
        endpoint_paths = [
            self.lifecycle.socket_path,
            *self.lifecycle.instance_socket_paths.values(),
        ]
        self.lifecycle.lifecycle_path.write_bytes(duplicate)
        try:
            with (
                mock.patch.object(
                    self.lifecycle, "_send_bound_request"
                ) as send_request,
                mock.patch.object(
                    self.lifecycle, "_unlink_bound_endpoints"
                ) as unlink_endpoints,
                mock.patch.object(
                    self.lifecycle, "_replace_lifecycle"
                ) as replace_lifecycle,
            ):
                with self.assertRaisesRegex(
                    fleet_control_service.ControlServiceError,
                    "cannot read control lifecycle",
                ):
                    self.lifecycle.stop()
            send_request.assert_not_called()
            unlink_endpoints.assert_not_called()
            replace_lifecycle.assert_not_called()
            self.assertTrue(all(path.exists() for path in endpoint_paths))
            observed, zombie = (
                fleet_control_service.control_runtime.process_observation(
                    lifecycle["pid"]
                )
            )
            self.assertEqual(observed, lifecycle["process_identity"])
            self.assertFalse(zombie)
        finally:
            self.lifecycle.lifecycle_path.write_bytes(original)
            self.lifecycle.stop()

    def test_control_ancestor_and_service_lock_symlinks_block_before_launch(
        self,
    ) -> None:
        outside_control = self.tmp / "outside-control"
        outside_control.mkdir(mode=0o700)
        outside_control.chmod(0o700)
        self.lifecycle.root.symlink_to(outside_control, target_is_directory=True)
        with mock.patch.object(fleet_control_service.subprocess, "Popen") as launched:
            with self.assertRaisesRegex(
                fleet_control_service.ControlServiceError, "unsafe Fleet Control state"
            ):
                self.lifecycle.start()
        launched.assert_not_called()

        self.lifecycle.root.unlink()
        self.create_control_root()
        outside_lock = self.tmp / "outside.lock"
        outside_lock.write_bytes(b"")
        outside_lock.chmod(0o600)
        self.lifecycle.lock_path.symlink_to(outside_lock)
        with mock.patch.object(fleet_control_service.subprocess, "Popen") as launched:
            with self.assertRaisesRegex(
                fleet_control_service.ControlServiceError, "unsafe Fleet Control state"
            ):
                self.lifecycle.start()
        launched.assert_not_called()

    def test_log_symlink_blocks_before_process_launch(self) -> None:
        self.create_control_root()
        outside = self.tmp / "outside.log"
        outside.write_bytes(b"outside\n")
        outside.chmod(0o600)
        (self.lifecycle.root / "service.stdout.log").symlink_to(outside)
        with mock.patch.object(fleet_control_service.subprocess, "Popen") as launched:
            with self.assertRaisesRegex(
                fleet_control_service.ControlServiceError, "unsafe Fleet Control state"
            ):
                self.lifecycle.start()
        launched.assert_not_called()
        self.assertEqual(outside.read_bytes(), b"outside\n")

    def test_nonpositive_lifecycle_pid_is_rejected_before_any_signal(self) -> None:
        self.create_control_root()
        socket_root, socket_root_identity = (
            self.lifecycle._prepare_configured_socket_root()
        )
        self.lifecycle._set_socket_binding(socket_root)
        process_identity, zombie = (
            fleet_control_service.control_runtime.process_observation(os.getpid())
        )
        self.assertFalse(zombie)
        self.assertIsNotNone(process_identity)
        lifecycle = {
            "schema_version": 3,
            "mission_id": self.mission_id,
            "preset": self.lifecycle.preset,
            "pid": 0,
            "process_identity": process_identity,
            "launch_id": str(uuid.uuid4()),
            "socket_root": str(socket_root),
            "socket_root_identity": socket_root_identity,
            "socket": str(self.lifecycle.socket_path),
            "instance_sockets": {
                instance: str(path)
                for instance, path in self.lifecycle.instance_socket_paths.items()
            },
            "endpoint_identities": None,
            "started_at": "2099-01-01T00:00:00+00:00",
            "stopped_at": None,
        }
        self.lifecycle._replace_lifecycle(lifecycle)
        with mock.patch.object(fleet_control_service.os, "kill") as kill:
            with self.assertRaisesRegex(
                fleet_control_service.ControlServiceError, "pid is invalid"
            ):
                self.lifecycle.stop_if_present()
        kill.assert_not_called()

    def test_lifecycle_rejects_replaced_durable_socket_root(self) -> None:
        self.create_control_root()
        socket_root = self.tmp / "bound-socket-root"
        socket_root.mkdir(mode=0o700)
        socket_root.chmod(0o700)
        with mock.patch.dict(
            os.environ, {"FLEET_CONTROL_SOCKET_DIR": str(socket_root)}
        ):
            lifecycle = fleet_control_service.ControlLifecycle(
                self.runs, self.mission_id
            )
        canonical, root_identity = lifecycle._prepare_configured_socket_root()
        lifecycle._set_socket_binding(canonical)
        process_identity, zombie = (
            fleet_control_service.control_runtime.process_observation(os.getpid())
        )
        self.assertFalse(zombie)
        value = {
            "schema_version": 3,
            "mission_id": self.mission_id,
            "preset": lifecycle.preset,
            "pid": os.getpid(),
            "process_identity": process_identity,
            "launch_id": str(uuid.uuid4()),
            "socket_root": str(canonical),
            "socket_root_identity": root_identity,
            "socket": str(lifecycle.socket_path),
            "instance_sockets": {
                instance: str(path)
                for instance, path in lifecycle.instance_socket_paths.items()
            },
            "endpoint_identities": None,
            "started_at": "2099-01-01T00:00:00+00:00",
            "stopped_at": None,
        }
        lifecycle._replace_lifecycle(value)
        saved = socket_root.with_name("bound-socket-root.saved")
        socket_root.rename(saved)
        socket_root.mkdir(mode=0o700)
        socket_root.chmod(0o700)
        try:
            with self.assertRaisesRegex(
                fleet_control_service.ControlServiceError,
                "runtime identity mismatch",
            ):
                lifecycle._read_lifecycle()
        finally:
            socket_root.rmdir()
            saved.rename(socket_root)

    def test_stop_if_present_rejects_endpoint_without_lifecycle(self) -> None:
        socket_root = self.tmp / "socket-root"
        socket_root.mkdir(mode=0o700)
        with mock.patch.dict(
            os.environ, {"FLEET_CONTROL_SOCKET_DIR": str(socket_root)}
        ):
            lifecycle = fleet_control_service.ControlLifecycle(
                self.runs, self.mission_id
            )
        outside = self.tmp / "outside"
        outside.write_text("outside\n", encoding="utf-8")
        lifecycle.socket_path.symlink_to(outside)
        with self.assertRaisesRegex(
            fleet_control_service.ControlServiceError,
            "endpoints exist without a durable lifecycle",
        ):
            lifecycle.stop_if_present()
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    def test_gated_launch_cannot_leave_an_unrecorded_socket_daemon(self) -> None:
        socket_root = Path(tempfile.mkdtemp(prefix="fcg-a-", dir="/tmp"))
        socket_root.chmod(0o700)
        self.addCleanup(shutil.rmtree, socket_root, True)
        command = [
            sys.executable,
            str(Path(fleet_control_service.__file__).resolve()),
            "--runs-dir",
            str(self.runs),
            "--mission-id",
            self.mission_id,
            "start",
        ]
        env = {
            **os.environ,
            "FLEET_CONTROL_SOCKET_DIR": str(socket_root),
            "FLEET_TEST_CONTROL_CRASH_AT": "after_popen_before_lifecycle",
        }
        crashed = subprocess.run(
            command, env=env, text=True, capture_output=True, check=False, timeout=10
        )
        self.assertNotEqual(crashed.returncode, 0)
        with mock.patch.dict(
            os.environ, {"FLEET_CONTROL_SOCKET_DIR": str(socket_root)}
        ):
            reconciler = fleet_control_service.ControlLifecycle(
                self.runs, self.mission_id
            )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(
            path.exists() or path.is_symlink()
            for path in [
                reconciler.socket_path,
                *reconciler.instance_socket_paths.values(),
            ]
        ):
            time.sleep(0.05)
        self.assertFalse(reconciler._lifecycle_exists())
        self.assertTrue(reconciler.stop_if_present()["absent"])

    def test_gated_launch_lifecycle_before_release_reconciles_dead_child(self) -> None:
        socket_root = Path(tempfile.mkdtemp(prefix="fcg-b-", dir="/tmp"))
        socket_root.chmod(0o700)
        self.addCleanup(shutil.rmtree, socket_root, True)
        command = [
            sys.executable,
            str(Path(fleet_control_service.__file__).resolve()),
            "--runs-dir",
            str(self.runs),
            "--mission-id",
            self.mission_id,
            "start",
        ]
        env = {
            **os.environ,
            "FLEET_CONTROL_SOCKET_DIR": str(socket_root),
            "FLEET_TEST_CONTROL_CRASH_AT": "after_lifecycle_before_release",
        }
        crashed = subprocess.run(
            command, env=env, text=True, capture_output=True, check=False, timeout=10
        )
        self.assertNotEqual(crashed.returncode, 0)
        with mock.patch.dict(
            os.environ, {"FLEET_CONTROL_SOCKET_DIR": str(socket_root)}
        ):
            reconciler = fleet_control_service.ControlLifecycle(
                self.runs, self.mission_id
            )
        self.assertTrue(
            reconciler._lifecycle_exists(),
            f"returncode={crashed.returncode}; stderr={crashed.stderr!r}",
        )
        stopped = reconciler.stop_if_present()
        self.assertTrue(stopped["stopped"])
        self.assertIsNotNone(stopped["lifecycle"]["stopped_at"])

    def test_first_start_creates_private_checkpoint_tree_and_stops_cleanly(
        self,
    ) -> None:
        self.assertFalse(self.lifecycle.root.exists())
        started = self.lifecycle.start()
        try:
            self.assertTrue(started["started"])
            startup_root = self.lifecycle.root / "startups"
            self.assertEqual(
                [path.suffix for path in startup_root.iterdir()], [".json"]
            )
            self.assertFalse(
                any(
                    path.name.startswith(".fleet-atomic-")
                    for path in startup_root.iterdir()
                )
            )
        finally:
            if self.lifecycle.lifecycle_path.exists():
                self.lifecycle.stop_if_present()

    def test_startup_checkpoint_recovers_all_atomic_sigkill_windows(self) -> None:
        for checkpoint in (
            "after_atomic_partial_write",
            "after_atomic_pending_fsync",
            "after_atomic_rename",
        ):
            with self.subTest(checkpoint=checkpoint):
                case_root = self.tmp / checkpoint
                runs, mission_id, _ = create_running_mission(
                    case_root,
                    feature="checkpoint-" + checkpoint.replace("_", "-"),
                )
                socket_root = case_root / "sockets"
                socket_root.mkdir(mode=0o700)
                socket_root.chmod(0o700)
                crashed = subprocess.run(
                    [
                        sys.executable,
                        str(Path(fleet_control_service.__file__).resolve()),
                        "--runs-dir",
                        str(runs),
                        "--mission-id",
                        mission_id,
                        "start",
                    ],
                    env={
                        **os.environ,
                        "FLEET_CONTROL_SOCKET_DIR": str(socket_root),
                        "FLEET_TEST_SAFE_PATH_CRASH_AT": checkpoint,
                    },
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertNotEqual(crashed.returncode, 0)
                with mock.patch.dict(
                    os.environ, {"FLEET_CONTROL_SOCKET_DIR": str(socket_root)}
                ):
                    reconciler = fleet_control_service.ControlLifecycle(
                        runs, mission_id
                    )
                    self.assertTrue(reconciler.stop_if_present()["absent"])
                self.assertFalse(
                    any(path.name.endswith(".stage") for path in socket_root.iterdir())
                )
                startup_root = reconciler.root / "startups"
                if startup_root.exists():
                    self.assertFalse(
                        any(
                            path.name == "startup.pending.json"
                            or path.name.startswith(".fleet-atomic-")
                            for path in startup_root.iterdir()
                        )
                    )

    def test_partial_endpoint_publish_crash_reconciles_exact_inodes(self) -> None:
        # A macOS per-test temp root exceeds the AF_UNIX path budget, which
        # fails the start before any rename and made this scenario vacuous;
        # a short root runs the real partial-publish crash on every platform.
        socket_root = Path(
            tempfile.mkdtemp(prefix="fc-partial-", dir=os.path.realpath("/tmp"))
        )
        self.addCleanup(shutil.rmtree, socket_root, ignore_errors=True)
        socket_root.chmod(0o700)
        crashed = subprocess.run(
            [
                sys.executable,
                str(Path(fleet_control_service.__file__).resolve()),
                "--runs-dir",
                str(self.runs),
                "--mission-id",
                self.mission_id,
                "start",
            ],
            env={
                **os.environ,
                "FLEET_CONTROL_SOCKET_DIR": str(socket_root),
                "FLEET_TEST_CONTROL_CRASH_AT": "after_partial_publish",
            },
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        # Exactly 137 proves the injected mid-publish crash actually ran;
        # any other nonzero exit is an earlier failure and a vacuous test.
        self.assertEqual(crashed.returncode, 137, crashed.stderr)
        with mock.patch.dict(
            os.environ, {"FLEET_CONTROL_SOCKET_DIR": str(socket_root)}
        ):
            reconciler = fleet_control_service.ControlLifecycle(
                self.runs, self.mission_id
            )
            stopped = reconciler.stop_if_present()
        self.assertTrue(stopped.get("stopped") or stopped.get("absent"))
        self.assertFalse(any(socket_root.iterdir()))

    def test_slow_operation_shutdown_terminates_and_reaps_owned_group(self) -> None:
        control = fleet_control.FleetControl(self.runs, self.mission_id)
        stopping = threading.Event()
        launch_id = str(uuid.uuid4())
        supervisor = fleet_mcp._OperationSupervisor(  # noqa: SLF001
            control, launch_id=launch_id, stopping=stopping
        )
        outcome: dict[str, object] = {}

        def run() -> None:
            try:
                outcome["result"] = supervisor.run(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    runs_dir=self.runs,
                    timeout=1800,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                outcome["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 6)
        self.addCleanup(supervisor.cancel_all)
        self.addCleanup(stopping.set)
        operation_root = self.lifecycle.root / "operations" / launch_id
        deadline = time.monotonic() + 5
        journal_path: Path | None = None
        while time.monotonic() < deadline:
            candidates = [
                path
                for path in operation_root.glob("*.json")
                if not path.name.endswith(".terminal.json")
                and path.name != "operation.pending.json"
            ]
            if candidates:
                journal_path = candidates[0]
                break
            time.sleep(0.02)
        self.assertIsNotNone(journal_path)
        assert journal_path is not None
        running_journal = json.loads(journal_path.read_text(encoding="utf-8"))
        worker_pid = running_journal["pid"]
        started = time.monotonic()
        stopping.set()
        supervisor.cancel_all()
        thread.join(timeout=6)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 6)
        supervisor.assert_quiescent()
        self.assertIn("error", outcome)
        observed, zombie = fleet_control_service.control_runtime.process_observation(
            worker_pid
        )
        self.assertTrue(
            observed is None
            or observed != running_journal["process_identity"]
            or zombie
        )
        journals = self.lifecycle._read_operation_journals(  # noqa: SLF001
            {"launch_id": launch_id}
        )
        self.assertEqual([value["state"] for _, value in journals], ["cancelled"])

    def test_cancel_kills_sigterm_ignoring_child_and_descendant(self) -> None:
        cases = {
            "direct": (
                "import pathlib,signal,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).write_text('ready\\n'); "
                "time.sleep(0.5); "
                "pathlib.Path(sys.argv[2]).write_text('late\\n')"
            ),
            "descendant": (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c',"
                "'import pathlib,signal,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                'pathlib.Path(sys.argv[1]).write_text(\\"ready\\\\n\\"); '
                "time.sleep(0.5); "
                'pathlib.Path(sys.argv[2]).write_text(\\"late\\\\n\\")\','
                "sys.argv[1],sys.argv[2]]); time.sleep(30)"
            ),
        }
        for label, program in cases.items():
            with self.subTest(label=label):
                control = fleet_control.FleetControl(self.runs, self.mission_id)
                stopping = threading.Event()
                launch_id = str(uuid.uuid4())
                supervisor = fleet_mcp._OperationSupervisor(  # noqa: SLF001
                    control, launch_id=launch_id, stopping=stopping
                )
                ready = self.tmp / f"cancel-{label}.ready"
                late = self.tmp / f"cancel-{label}.late"
                outcome: dict[str, object] = {}

                def run() -> None:
                    try:
                        supervisor.run(
                            [
                                sys.executable,
                                "-c",
                                program,
                                str(ready),
                                str(late),
                            ],
                            runs_dir=self.runs,
                            timeout=20,
                        )
                    except BaseException as exc:  # pragma: no cover - asserted below
                        outcome["error"] = exc

                thread = threading.Thread(target=run)
                thread.start()
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not ready.exists():
                        time.sleep(0.02)
                    self.assertTrue(ready.exists())
                    stopping.set()
                    supervisor.cancel_all()
                    thread.join(timeout=6)
                    self.assertFalse(thread.is_alive())
                    self.assertIn("error", outcome)
                    supervisor.assert_quiescent()
                    time.sleep(0.7)
                    self.assertFalse(late.exists())
                    journals = self.lifecycle._read_operation_journals(  # noqa: SLF001
                        {"launch_id": launch_id}
                    )
                    self.assertEqual(
                        [value["state"] for _, value in journals], ["cancelled"]
                    )
                finally:
                    stopping.set()
                    supervisor.cancel_all()
                    thread.join(timeout=6)

    def test_operation_startup_failures_close_every_descriptor(self) -> None:
        control = fleet_control.FleetControl(self.runs, self.mission_id)
        supervisor = fleet_mcp._OperationSupervisor(  # noqa: SLF001
            control, launch_id=str(uuid.uuid4()), stopping=threading.Event()
        )
        command = [sys.executable, "-c", "pass"]
        real_pipe = os.pipe

        def descriptor_count() -> int:
            return len(os.listdir("/dev/fd"))

        baseline = descriptor_count()
        with mock.patch.object(fleet_mcp.os, "pipe", wraps=real_pipe) as pipe:
            for _ in range(8):
                with self.assertRaisesRegex(
                    fleet_control.FleetControlError,
                    "invalid Fleet Control operation command",
                ):
                    supervisor.run(
                        [sys.executable, "bad\x00argument"],
                        runs_dir=self.runs,
                    )
        pipe.assert_not_called()
        self.assertEqual(descriptor_count() - baseline, 0)

        calls = 0

        def fail_every_second_pipe() -> tuple[int, int]:
            nonlocal calls
            calls += 1
            if calls % 2 == 0:
                raise OSError("simulated second pipe failure")
            return real_pipe()

        baseline = descriptor_count()
        with mock.patch.object(
            fleet_mcp.os, "pipe", side_effect=fail_every_second_pipe
        ):
            for _ in range(8):
                with self.assertRaisesRegex(
                    fleet_control.FleetControlError,
                    "simulated second pipe failure",
                ):
                    supervisor.run(command, runs_dir=self.runs)
        self.assertEqual(descriptor_count() - baseline, 0)

        baseline = descriptor_count()
        with mock.patch.object(
            fleet_mcp.subprocess,
            "Popen",
            side_effect=ValueError("simulated argv failure"),
        ):
            for _ in range(8):
                with self.assertRaisesRegex(
                    fleet_control.FleetControlError, "simulated argv failure"
                ):
                    supervisor.run(command, runs_dir=self.runs)
        self.assertEqual(descriptor_count() - baseline, 0)

        for target in ("_BoundedOperationOutput", "_OperationWorkerStatus"):
            with self.subTest(initializer=target):
                baseline = descriptor_count()
                with mock.patch.object(
                    fleet_mcp,
                    target,
                    side_effect=RuntimeError(f"simulated {target} failure"),
                ):
                    for _ in range(4):
                        with self.assertRaisesRegex(
                            fleet_control.FleetControlError,
                            f"simulated {target} failure",
                        ):
                            supervisor.run(command, runs_dir=self.runs)
                self.assertEqual(descriptor_count() - baseline, 0)
        supervisor.assert_quiescent()

    def test_excessive_operation_output_fails_closed_without_deadlock(self) -> None:
        half = fleet_mcp.MAX_OPERATION_OUTPUT_BYTES // 2
        cases = {
            "stdout": (
                "import sys; "
                f"sys.stdout.buffer.write(b'x' * {fleet_mcp.MAX_OPERATION_OUTPUT_BYTES + 1}); "
                "sys.stdout.buffer.flush()"
            ),
            "stderr": (
                "import sys; "
                f"sys.stderr.buffer.write(b'x' * {fleet_mcp.MAX_OPERATION_OUTPUT_BYTES + 1}); "
                "sys.stderr.buffer.flush()"
            ),
            "combined": (
                "import sys; "
                f"sys.stdout.buffer.write(b'x' * {half}); "
                "sys.stdout.buffer.flush(); "
                f"sys.stderr.buffer.write(b'y' * {fleet_mcp.MAX_OPERATION_OUTPUT_BYTES - half + 1}); "
                "sys.stderr.buffer.flush()"
            ),
        }
        for stream, program in cases.items():
            with self.subTest(stream=stream):
                control = fleet_control.FleetControl(self.runs, self.mission_id)
                launch_id = str(uuid.uuid4())
                supervisor = fleet_mcp._OperationSupervisor(  # noqa: SLF001
                    control, launch_id=launch_id, stopping=threading.Event()
                )
                started = time.monotonic()
                with self.assertRaisesRegex(
                    fleet_control.FleetControlError,
                    rf"output exceeds {fleet_mcp.MAX_OPERATION_OUTPUT_BYTES} bytes",
                ):
                    supervisor.run(
                        [sys.executable, "-c", program],
                        runs_dir=self.runs,
                        timeout=10,
                    )
                self.assertLess(time.monotonic() - started, 10)
                supervisor.assert_quiescent()
                journals = self.lifecycle._read_operation_journals(  # noqa: SLF001
                    {"launch_id": launch_id}
                )
                self.assertEqual(
                    [value["state"] for _, value in journals], ["cancelled"]
                )

    def test_exact_operation_output_limit_succeeds(self) -> None:
        control = fleet_control.FleetControl(self.runs, self.mission_id)
        launch_id = str(uuid.uuid4())
        supervisor = fleet_mcp._OperationSupervisor(  # noqa: SLF001
            control, launch_id=launch_id, stopping=threading.Event()
        )
        result = supervisor.run(
            [
                sys.executable,
                "-c",
                "import sys; "
                f"sys.stdout.buffer.write(b'x' * {fleet_mcp.MAX_OPERATION_OUTPUT_BYTES}); "
                "sys.stdout.buffer.flush()",
            ],
            runs_dir=self.runs,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            len(result.stdout.encode()), fleet_mcp.MAX_OPERATION_OUTPUT_BYTES
        )
        self.assertEqual(result.stderr, "")
        supervisor.assert_quiescent()
        journals = self.lifecycle._read_operation_journals(  # noqa: SLF001
            {"launch_id": launch_id}
        )
        self.assertEqual([value["state"] for _, value in journals], ["completed"])

    def test_cancel_between_admission_and_gate_never_launches_child(self) -> None:
        control = fleet_control.FleetControl(self.runs, self.mission_id)
        stopping = threading.Event()
        launch_id = str(uuid.uuid4())
        supervisor = fleet_mcp._OperationSupervisor(  # noqa: SLF001
            control, launch_id=launch_id, stopping=stopping
        )
        marker = self.tmp / "gate-race-effect.txt"
        at_gate = threading.Event()
        release_gate_write = threading.Event()
        original_write = fleet_mcp.os.write
        outcome: dict[str, object] = {}

        def paused_write(fd: int, value: bytes) -> int:
            if value == b"\x01":
                at_gate.set()
                if not release_gate_write.wait(timeout=5):
                    raise RuntimeError("test gate write was not released")
            return original_write(fd, value)

        def run() -> None:
            try:
                supervisor.run(
                    [
                        sys.executable,
                        "-c",
                        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('bad')",
                        str(marker),
                    ],
                    runs_dir=self.runs,
                    timeout=10,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                outcome["error"] = exc

        with mock.patch.object(fleet_mcp.os, "write", side_effect=paused_write):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(at_gate.wait(timeout=5))
                stopping.set()
                supervisor.cancel_all()
            finally:
                release_gate_write.set()
                thread.join(timeout=6)
                if thread.is_alive():
                    supervisor.cancel_all()
                    thread.join(timeout=6)
        self.assertFalse(thread.is_alive())
        self.assertIn("error", outcome)
        self.assertFalse(marker.exists())
        supervisor.assert_quiescent()
        journals = self.lifecycle._read_operation_journals(  # noqa: SLF001
            {"launch_id": launch_id}
        )
        self.assertEqual([value["state"] for _, value in journals], ["cancelled"])

    def test_completed_operation_kills_background_descendant_before_return(
        self,
    ) -> None:
        control = fleet_control.FleetControl(self.runs, self.mission_id)
        launch_id = str(uuid.uuid4())
        supervisor = fleet_mcp._OperationSupervisor(  # noqa: SLF001
            control, launch_id=launch_id, stopping=threading.Event()
        )
        marker = self.tmp / "completed-background-late.txt"
        descendant = (
            "import pathlib,signal,sys,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(0.5); pathlib.Path(sys.argv[1]).write_text('late')"
        )
        direct = (
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); "
            "print('direct-complete')"
        )
        result = supervisor.run(
            [sys.executable, "-c", direct, descendant, str(marker)],
            runs_dir=self.runs,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "direct-complete\n")
        self.assertEqual(result.stderr, "")
        supervisor.assert_quiescent()
        time.sleep(0.7)
        self.assertFalse(marker.exists())
        journals = self.lifecycle._read_operation_journals(  # noqa: SLF001
            {"launch_id": launch_id}
        )
        self.assertEqual([value["state"] for _, value in journals], ["completed"])

    def test_timeout_kills_sigterm_ignoring_child_before_late_effect(self) -> None:
        control = fleet_control.FleetControl(self.runs, self.mission_id)
        launch_id = str(uuid.uuid4())
        supervisor = fleet_mcp._OperationSupervisor(  # noqa: SLF001
            control, launch_id=launch_id, stopping=threading.Event()
        )
        marker = self.tmp / "timeout-late.txt"
        with self.assertRaisesRegex(fleet_control.FleetControlError, "timed out"):
            supervisor.run(
                [
                    sys.executable,
                    "-c",
                    "import pathlib,signal,sys,time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "time.sleep(1.25); "
                    "pathlib.Path(sys.argv[1]).write_text('late\\n'); "
                    "time.sleep(30)",
                    str(marker),
                ],
                runs_dir=self.runs,
                timeout=1,
            )
        supervisor.assert_quiescent()
        time.sleep(0.4)
        self.assertFalse(marker.exists())
        journals = self.lifecycle._read_operation_journals(  # noqa: SLF001
            {"launch_id": launch_id}
        )
        self.assertEqual([value["state"] for _, value in journals], ["cancelled"])

    def test_operation_crash_recovery_prevents_late_child_effects(self) -> None:
        helper = """
from pathlib import Path
import sys
import threading
from fleet_control import FleetControl
from fleet_mcp import _OperationSupervisor

runs = Path(sys.argv[1])
mission_id = sys.argv[2]
launch_id = sys.argv[3]
ready = sys.argv[4]
marker = sys.argv[5]
control = FleetControl(runs, mission_id)
supervisor = _OperationSupervisor(
    control, launch_id=launch_id, stopping=threading.Event()
)
descendant_program = (
    "import pathlib,signal,sys,time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8'); "
    "time.sleep(0.5); "
    "pathlib.Path(sys.argv[2]).write_text('late', encoding='utf-8')"
)
direct_program = (
    "import subprocess,sys; "
    "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]])"
)
supervisor.run(
    [
        sys.executable,
        "-c",
        direct_program,
        descendant_program,
        ready,
        marker,
    ],
    runs_dir=runs,
    timeout=1800,
)
"""
        cases = (
            ("atomic-partial", "after_atomic_partial_write", None),
            ("atomic-pending", "after_atomic_pending_fsync", None),
            ("atomic-rename", "after_atomic_rename", None),
            ("after-release", None, "after_release"),
        )
        for label, safe_path_crash, operation_crash in cases:
            with self.subTest(label=label):
                launch_id = str(uuid.uuid4())
                ready = self.tmp / f"ready-{label}.txt"
                marker = self.tmp / f"late-{label}.txt"
                environment = {
                    **os.environ,
                    "PYTHONPATH": str(Path(fleet_mcp.__file__).resolve().parent),
                }
                if safe_path_crash is not None:
                    environment["FLEET_TEST_SAFE_PATH_CRASH_AT"] = safe_path_crash
                if operation_crash is not None:
                    environment["FLEET_TEST_CONTROL_OPERATION_CRASH_AT"] = (
                        operation_crash
                    )
                crashed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        helper,
                        str(self.runs),
                        self.mission_id,
                        launch_id,
                        str(ready),
                        str(marker),
                    ],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertNotEqual(crashed.returncode, 0)
                lifecycle = {"launch_id": launch_id}
                try:
                    if operation_crash == "after_release":
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline and not ready.exists():
                            time.sleep(0.02)
                        self.assertTrue(
                            ready.exists(),
                            f"returncode={crashed.returncode}; "
                            f"stdout={crashed.stdout!r}; "
                            f"stderr={crashed.stderr!r}",
                        )
                    self.lifecycle._reconcile_operation_journals(  # noqa: SLF001
                        lifecycle
                    )
                finally:
                    # Idempotent cleanup also protects the test host if an
                    # assertion above interrupts the first reconciliation.
                    self.lifecycle._reconcile_operation_journals(  # noqa: SLF001
                        lifecycle
                    )
                time.sleep(0.7)
                self.assertFalse(marker.exists())
                operation_root = self.lifecycle.root / "operations" / launch_id
                if operation_root.exists():
                    self.assertFalse(
                        any(
                            path.name == "operation.pending.json"
                            or path.name.startswith(".fleet-atomic-")
                            for path in operation_root.iterdir()
                        )
                    )
                journals = self.lifecycle._read_operation_journals(  # noqa: SLF001
                    lifecycle
                )
                self.assertTrue(
                    not journals
                    or all(value["state"] == "reconciled" for _, value in journals)
                )

    def test_timeout_ceiling_and_management_admission_are_effect_free(self) -> None:
        control = fleet_control.FleetControl(self.runs, self.mission_id)
        with mock.patch.object(control, "wait") as wait:
            with self.assertRaisesRegex(fleet_control.FleetControlError, "maximum"):
                fleet_mcp.call_tool(
                    control,
                    "wait",
                    {"run_ids": [str(uuid.uuid4())], "timeout_seconds": 1801},
                )
        wait.assert_not_called()

        admission = fleet_mcp._AdmissionGate(maximum=2)  # noqa: SLF001
        self.assertTrue(admission.try_admit(management=False))
        self.assertTrue(admission.try_admit(management=False))
        self.assertFalse(admission.try_admit(management=False))
        self.assertTrue(admission.try_admit(management=True))
        admission.release(management=True)
        admission.release(management=False)
        admission.release(management=False)
        self.assertEqual(admission.active_count(), 0)
        admission.begin_shutdown()
        self.assertFalse(admission.try_admit(management=True))
        self.assertFalse(admission.try_admit(management=False))

    def test_server_owned_admission_release_survives_handler_init_failure(self) -> None:
        admission = fleet_mcp._AdmissionGate(maximum=1)  # noqa: SLF001
        server = object.__new__(fleet_mcp._ThreadingUnixServer)  # noqa: SLF001
        server.admission = admission
        server.management = False
        self.assertTrue(admission.try_admit(management=False))
        with mock.patch.object(
            socketserver.ThreadingMixIn,
            "process_request_thread",
            side_effect=RuntimeError("handler init failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "handler init failed"):
                server.process_request_thread(None, None)
        self.assertEqual(admission.active_count(), 0)


if __name__ == "__main__":
    unittest.main()
