from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

from tests.mission_control_test_support import create_running_mission

import fleet_control
import fleet_mcp
import fleet_mission_state as mission_state


class FleetControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs, self.mission_id, self.lead_run_id = create_running_mission(
            self.tmp, feature="fleet-control"
        )
        self.control = fleet_control.FleetControl(self.runs, self.mission_id)
        self.calls: list[list[str]] = []
        self.run_by_instance: dict[str, str] = {}
        self.prompt_by_instance: dict[str, str] = {}

    def fake_run(self, command: list[str], *, runs_dir: Path, timeout=None):
        del timeout
        self.assertEqual(runs_dir.resolve(), self.runs.resolve())
        self.calls.append(command)
        name = Path(command[0]).name
        if name in {"fleet-send.sh", "fleet-dispatch.sh"}:
            feature, instance, prompt = command[1:4]
            run_id = (
                command[command.index("--run-id") + 1]
                if "--run-id" in command
                else str(uuid.uuid4())
            )
            self.run_by_instance[instance] = run_id
            self.prompt_by_instance[instance] = prompt
            event = {
                "timestamp": "2026-07-14T00:00:00Z",
                "run_id": run_id,
                "feature": feature,
                "instance": instance,
                "status": "dispatched",
                "task_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            }
            ledger = self.runs / f"fleet-{feature}.ledger.jsonl"
            with ledger.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
            return subprocess.CompletedProcess(command, 0, json.dumps({"run_id": run_id}) + "\n", "")
        if name == "fleet-wait.sh":
            rows = [
                {"run_id": run_id, "status": "succeeded"}
                for run_id in self.run_by_instance.values()
                if any(run_id in item for item in command)
            ]
            return subprocess.CompletedProcess(
                command, 0, "".join(json.dumps(item) + "\n" for item in rows), ""
            )
        raise AssertionError(command)

    def mark_succeeded(self, instance: str, content: bytes) -> Path:
        run_id = self.run_by_instance[instance]
        result = self.runs / "results" / "fleet-control" / f"{run_id}.txt"
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_bytes(content)
        member = self.control.members()[instance]
        event = {
            "timestamp": "2026-07-14T00:01:00Z",
            "run_id": run_id,
            "feature": "fleet-control",
            "instance": instance,
            "status": "succeeded",
            "task_sha256": hashlib.sha256(self.prompt_by_instance[instance].encode()).hexdigest(),
            "result_file": str(result),
            "provider": member["provider"],
            "model": member["model"],
            "variant": member.get("variant"),
        }
        with (self.runs / "fleet-fleet-control.ledger.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
        return result

    def test_dispatch_many_creates_all_runs_without_waiting(self) -> None:
        requests = [
            {
                "recipient_instance": "scout",
                "capability": "recon",
                "objective": "inspect the parser",
                "idempotency_key": "many:scout",
            },
            {
                "recipient_instance": "challenger",
                "capability": "challenge",
                "objective": "challenge the architecture",
                "idempotency_key": "many:challenger",
            },
        ]
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            value = self.control.dispatch_many(requests)
        self.assertTrue(value["all_dispatched_before_wait"])
        self.assertEqual(len(value["runs"]), 2)
        self.assertEqual([Path(call[0]).name for call in self.calls], ["fleet-send.sh", "fleet-send.sh"])
        self.assertEqual(len(self.control.state()["delegations"]), 2)

    def test_delegating_run_is_socket_bound_before_interactive_transfer(self) -> None:
        observed: dict[str, str] = {}

        def observe_transfer(command: list[str], *, runs_dir: Path, timeout=None):
            self.assertIn("--run-id", command)
            run_id = command[command.index("--run-id") + 1]
            self.assertIn(f"RUN_ID={run_id}", command[3])
            events = self.control.events()
            issued = [event for event in events if event["kind"] == "capability_token_issued"]
            self.assertEqual(len(issued), 1)
            token_id = issued[0]["payload"]["token_id"]
            binding = [
                event
                for event in events
                if event["kind"] == "capability_token_bound"
                and event["payload"].get("run_id") == run_id
            ]
            self.assertEqual(len(binding), 1)
            self.assertFalse(
                any(event["kind"] == "delegation_registered" for event in events),
                "the prompt must not need delegation_registered to authenticate",
            )
            envelope = {
                "schema_version": 1,
                "caller": {"instance": "scout", "run_id": run_id, "token_id": token_id},
                "request": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            }
            reply = fleet_mcp.handle_socket_envelope(self.control, envelope)
            self.assertEqual(reply["identity"]["run_id"], run_id)
            observed.update(run_id=run_id, token_id=token_id)
            return self.fake_run(command, runs_dir=runs_dir, timeout=timeout)

        with mock.patch.object(fleet_control, "run_process", side_effect=observe_transfer):
            result = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="inspect then delegate",
                idempotency_key="prebind:scout",
                can_delegate=True,
                allowed_capabilities=["verify"],
                remaining_budget=1,
            )
        self.assertEqual(result["run_id"], observed["run_id"])
        self.assertEqual(result["token_id"], observed["token_id"])

    def test_dispatch_retry_reuses_run_and_request_drift_conflicts(self) -> None:
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            first = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="inspect the parser",
                idempotency_key="retry:scout",
            )
            second = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="inspect the parser",
                idempotency_key="retry:scout",
            )
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertTrue(second["reused"])
            with self.assertRaisesRegex(fleet_control.FleetControlError, "conflicts"):
                self.control.dispatch(
                    recipient_instance="scout",
                    capability="recon",
                    objective="different objective",
                    idempotency_key="retry:scout",
                )
        self.assertEqual(len(self.calls), 1)

    def test_dispatch_rejects_non_json_output_contract(self) -> None:
        with self.assertRaisesRegex(fleet_control.FleetControlError, "canonical JSON"):
            self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="inspect",
                idempotency_key="bad-contract",
                expected_output_contract={"bad": {"not", "json"}},
            )

    def test_authorized_specialist_can_subdelegate_but_never_to_writer(self) -> None:
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            scout = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="inspect and delegate a challenge",
                idempotency_key="root:scout",
                can_delegate=True,
                allowed_capabilities=["challenge", "build"],
                remaining_budget=2,
            )
            child = self.control.dispatch(
                recipient_instance="challenger",
                capability="challenge",
                objective="challenge the finding",
                idempotency_key="child:challenge",
                parent_run_id=scout["run_id"],
                token_id=scout["token_id"],
            )
            self.assertEqual(
                self.control.state()["delegations"][child["delegation_id"]]["depth"], 2
            )
            with self.assertRaisesRegex(fleet_control.FleetControlError, "never grant write"):
                self.control.dispatch(
                    recipient_instance="builder",
                    capability="build",
                    objective="write from a child",
                    idempotency_key="child:writer",
                    parent_run_id=scout["run_id"],
                    token_id=scout["token_id"],
                )

    def test_wait_persists_exact_artifact_and_relay_references_its_id(self) -> None:
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            scout = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="produce exact evidence",
                idempotency_key="wait:scout",
            )
            self.mark_succeeded("scout", b"exact scout result\n")
            waited = self.control.wait([scout["run_id"]], timeout_seconds=30)
            self.assertEqual(waited["status"], "succeeded")
            artifact_id = waited["results"][0]["artifact_id"]
            fetched = self.control.get_result(artifact_id)
            self.assertEqual(fetched["artifact_id"], artifact_id)
            relayed = self.control.relay_result(
                artifact_id=artifact_id,
                recipient_instance="challenger",
                capability="challenge",
                objective="challenge the exact Scout evidence",
                idempotency_key="relay:challenge",
            )
        self.assertIn(artifact_id, self.prompt_by_instance["challenger"])
        relay_events = [event for event in self.control.events() if event["kind"] == "result_relayed"]
        self.assertEqual(relay_events[0]["payload"]["recipient_run_id"], relayed["run_id"])

    def test_artifact_tampering_and_identity_drift_fail_closed(self) -> None:
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            scout = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="produce evidence",
                idempotency_key="drift:scout",
            )
            result = self.mark_succeeded("scout", b"result\n")
            ledger = self.runs / "fleet-fleet-control.ledger.jsonl"
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            rows[-1]["model"] = "wrong-model"
            ledger.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")
            with self.assertRaisesRegex(fleet_control.FleetControlError, "identity drift"):
                self.control.wait([scout["run_id"]], timeout_seconds=30)

    def test_cli_and_mcp_share_core_and_do_not_screen_scrape(self) -> None:
        source = (Path(fleet_control.__file__)).read_text(encoding="utf-8")
        self.assertNotIn("read-screen", source)
        listed = fleet_mcp.handle(
            self.control, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        self.assertGreaterEqual(len(listed["result"]["tools"]), 10)
        inspected = fleet_mcp.handle(
            self.control,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "inspect_mission", "arguments": {}},
            },
        )
        self.assertFalse(inspected["result"]["isError"])
        payload = json.loads(inspected["result"]["content"][0]["text"])
        self.assertEqual(payload["mission_id"], self.mission_id)

        cli = subprocess.run(
            [
                "python3", str(Path(fleet_control.__file__)), "--runs-dir", str(self.runs),
                "--mission-id", self.mission_id, "inspect-mission",
            ],
            cwd=Path(fleet_control.__file__).resolve().parents[1],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["mission_id"], self.mission_id)

        mcp_input = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            ]
        ) + "\n"
        mcp = subprocess.run(
            [
                "python3", str(Path(fleet_mcp.__file__)), "--runs-dir", str(self.runs),
                "--mission-id", self.mission_id,
            ],
            cwd=Path(fleet_control.__file__).resolve().parents[1],
            input=mcp_input,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(mcp.returncode, 0, mcp.stderr)
        frames = [json.loads(line) for line in mcp.stdout.splitlines()]
        self.assertEqual(frames[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertGreaterEqual(len(frames[1]["result"]["tools"]), 10)


if __name__ == "__main__":
    unittest.main()
