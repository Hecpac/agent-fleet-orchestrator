from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

from tests.mission_control_test_support import create_running_mission

import fleet_control
import fleet_control_service


class FleetControlSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)
        self.runs, self.mission_id, self.lead_run_id = create_running_mission(
            self.tmp, feature="socket"
        )
        self.control = fleet_control.FleetControl(self.runs, self.mission_id)
        self.lifecycle = fleet_control_service.ControlLifecycle(self.runs, self.mission_id)
        self.lifecycle.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        if self.lifecycle.lifecycle_path.exists():
            try:
                self.lifecycle.stop()
            except fleet_control_service.ControlServiceError:
                pass

    def request(self, caller: dict[str, object], method: str, **request: object) -> dict:
        return fleet_control_service.send_request(
            self.lifecycle.socket_path,
            {
                "schema_version": 1,
                "caller": caller,
                "request": {"jsonrpc": "2.0", "id": 1, "method": method, **request},
            },
        )

    def test_lead_identity_is_required_for_operational_requests(self) -> None:
        valid = self.request(
            {"instance": "lead", "run_id": self.lead_run_id},
            "tools/call",
            params={"name": "inspect_mission", "arguments": {}},
        )
        self.assertTrue(valid["ok"], valid)
        self.assertEqual(valid["result"]["identity"]["kind"], "lead")
        payload = json.loads(
            valid["result"]["response"]["result"]["content"][0]["text"]
        )
        self.assertEqual(payload["mission_id"], self.mission_id)

        wrong = self.request(
            {"instance": "lead", "run_id": str(uuid.uuid4())}, "tools/list"
        )
        self.assertFalse(wrong["ok"])
        self.assertIn("not bound", wrong["error"])

    def test_bound_specialist_token_limits_tools_and_delegated_scope(self) -> None:
        def fake_run(command: list[str], *, runs_dir: Path, timeout=None):
            del timeout
            run_id = command[command.index("--run-id") + 1]
            prompt = command[3]
            with (runs_dir / "fleet-socket.ledger.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "run_id": run_id,
                    "instance": "scout",
                    "task_sha256": __import__("hashlib").sha256(prompt.encode()).hexdigest(),
                    "status": "dispatched",
                }) + "\n")
            return subprocess.CompletedProcess(command, 0, json.dumps({"run_id": run_id}), "")

        with mock.patch.object(fleet_control, "run_process", side_effect=fake_run):
            delegated = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="inspect exact evidence",
                idempotency_key="socket:scout",
                can_delegate=True,
                allowed_capabilities=["challenge"],
                remaining_budget=1,
            )
        caller = {
            "instance": "scout",
            "run_id": delegated["run_id"],
            "token_id": delegated["token_id"],
        }
        listed = self.request(caller, "tools/list")
        self.assertTrue(listed["ok"], listed)

        denied = self.request(
            caller,
            "tools/call",
            params={
                "name": "complete",
                "arguments": {"artifact_id": "a" * 64, "summary": "x", "idempotency_key": "x"},
            },
        )
        self.assertFalse(denied["ok"])
        self.assertIn("not authorized", denied["error"])

        wrong_scope = self.request(
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
        self.assertFalse(wrong_scope["ok"])
        self.assertIn("exceeds token", wrong_scope["error"])

    def test_socket_permissions_and_health_are_kernel_scoped(self) -> None:
        self.assertEqual(self.lifecycle.socket_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.lifecycle.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.lifecycle.socket_root.stat().st_mode & 0o777, 0o700)
        health = fleet_control_service.send_request(
            self.lifecycle.socket_path, {"operation": "health"}
        )
        self.assertTrue(health["ok"])
        self.assertEqual(health["result"]["mission_id"], self.mission_id)


if __name__ == "__main__":
    unittest.main()
