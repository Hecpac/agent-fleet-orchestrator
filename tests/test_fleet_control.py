from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from tests.mission_control_test_support import (
    create_running_mission,
    legacy_v1_compiled,
    write_compiled,
)

import fleet_control
import fleet_admission
import fleet_artifacts
import fleet_delegation
import fleet_ledger
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

    def test_historical_compiled_workflow_is_rejected_before_dispatch_effects(
        self,
    ) -> None:
        path = self.runs / "missions" / self.mission_id / "compiled-workflow.json"
        write_compiled(path, legacy_v1_compiled(self.control.compiled))
        with (
            mock.patch.object(fleet_control, "run_process") as effect,
            self.assertRaisesRegex(
                fleet_control.FleetControlError, "historical read-only.*require v2"
            ),
        ):
            fleet_control.FleetControl(self.runs, self.mission_id)
        effect.assert_not_called()

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
            self.assertTrue(fleet_ledger.append_event(ledger, event))
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"run_id": run_id}) + "\n", ""
            )
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
        feature = self.control.state()["feature"]
        result = self.runs / "results" / feature / f"{run_id}.txt"
        result.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result.parent.chmod(0o700)
        result.write_bytes(content)
        result.chmod(0o600)
        result = result.resolve(strict=True)
        member = self.control.members()[instance]
        event = {
            "timestamp": "2026-07-14T00:01:00Z",
            "run_id": run_id,
            "feature": feature,
            "instance": instance,
            "status": "succeeded",
            "task_sha256": hashlib.sha256(
                self.prompt_by_instance[instance].encode()
            ).hexdigest(),
            "result_file": str(result),
            "provider": member["provider"],
            "model": member["model"],
            "variant": member.get("variant"),
        }
        self.assertTrue(
            fleet_ledger.append_event(
                self.runs / f"fleet-{feature}.ledger.jsonl", event
            )
        )
        return result

    def test_research_local_worker_wait_accepts_frozen_execution_identity(self) -> None:
        self.runs, self.mission_id, self.lead_run_id = create_running_mission(
            self.tmp / "research", feature="research-control", workflow_name="research"
        )
        self.control = fleet_control.FleetControl(self.runs, self.mission_id)
        self.calls = []
        self.run_by_instance = {}
        self.prompt_by_instance = {}
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            dispatched = self.control.dispatch(
                recipient_instance="triage_scope",
                capability="recon",
                objective="bound local research",
                idempotency_key="research:local",
            )
            self.mark_succeeded("triage_scope", b"STATUS: DONE\nEVIDENCE: bound\n")
            waited = self.control.wait([dispatched["run_id"]], timeout_seconds=30)
            artifact_id = waited["results"][0]["artifact_id"]
            with self.assertRaisesRegex(
                fleet_control.FleetControlError, "authenticated artifact client"
            ):
                self.control.dispatch(
                    recipient_instance="triage_sources",
                    capability="recon",
                    objective="must not receive a raw CAS path",
                    idempotency_key="research:local-input",
                    input_artifact_ids=[artifact_id],
                )
        self.assertEqual(waited["status"], "succeeded")
        self.assertEqual(Path(self.calls[0][0]).name, "fleet-dispatch.sh")
        self.assertFalse(any("triage_sources" in call for call in self.calls))

    def test_manifest_execution_identity_drift_is_rejected(self) -> None:
        path = self.runs / "fleet-fleet-control.manifest"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "scout.model=gpt-5.6-sol", "scout.model=unbound-model"
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            fleet_control.FleetControlError, "compiled manifest binding drift"
        ):
            self.control.manifest()

    def test_legacy_lifecycle_read_rejects_symlink_without_external_mutation(
        self,
    ) -> None:
        ledger = self.runs / "fleet-fleet-control.ledger.jsonl"
        ledger.unlink(missing_ok=True)
        outside = self.tmp / "external-ledger.jsonl"
        outside.write_text('{"run_id":"external"}\n', encoding="utf-8")
        outside.chmod(0o600)
        ledger.symlink_to(outside)
        before = outside.read_bytes()

        with self.assertRaisesRegex(
            fleet_control.FleetControlError, "unsafe or corrupt"
        ):
            self.control._legacy_events()

        self.assertEqual(outside.read_bytes(), before)
        self.assertTrue(ledger.is_symlink())

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
        self.assertEqual(
            [Path(call[0]).name for call in self.calls],
            ["fleet-send.sh", "fleet-send.sh"],
        )
        self.assertEqual(len(self.control.state()["delegations"]), 2)

    def test_concurrent_commit_head_drift_is_recovered_before_launch(self) -> None:
        original_commit = fleet_admission.commit
        committed = threading.Barrier(2)

        def commit_then_release(*args, **kwargs):
            value = original_commit(*args, **kwargs)
            committed.wait(timeout=10)
            return value

        requests = (
            {
                "recipient_instance": "scout",
                "capability": "recon",
                "objective": "concurrent scout",
                "idempotency_key": "concurrent:scout",
            },
            {
                "recipient_instance": "challenger",
                "capability": "challenge",
                "objective": "concurrent challenger",
                "idempotency_key": "concurrent:challenger",
            },
        )
        with (
            mock.patch.object(
                fleet_admission, "commit", side_effect=commit_then_release
            ),
            mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = [
                executor.submit(self.control.dispatch, **item) for item in requests
            ]
            results = [future.result(timeout=20) for future in futures]

        self.assertEqual(len(results), 2)
        self.assertEqual(
            {result["recipient_instance"] for result in results},
            {"scout", "challenger"},
        )
        current = self.control.state()
        for result in results:
            owner = current["run_owners"][result["run_id"]]
            self.assertEqual(
                current["admissions"][owner["owner_id"]]["phase"], "started"
            )

    def test_deadline_barrier_denies_launch_before_wrapper_effect(self) -> None:
        at_authorization = threading.Event()
        release_authorization = threading.Event()

        def expire_at_boundary(*args, **kwargs):
            del args, kwargs
            at_authorization.set()
            if not release_authorization.wait(timeout=10):
                raise AssertionError("deadline barrier was not released")
            raise mission_state.MissionConflict("mission admission deadline has passed")

        with (
            mock.patch.object(
                fleet_admission,
                "authorize_launch",
                side_effect=expire_at_boundary,
            ),
            mock.patch.object(fleet_control, "run_process") as wrapper,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(
                self.control.dispatch,
                recipient_instance="scout",
                capability="recon",
                objective="must stop at the deadline boundary",
                idempotency_key="deadline:boundary",
            )
            self.assertTrue(at_authorization.wait(timeout=10))
            wrapper.assert_not_called()
            release_authorization.set()
            with self.assertRaisesRegex(
                fleet_control.FleetControlError,
                "authorization was denied before effect",
            ):
                future.result(timeout=10)
        wrapper.assert_not_called()

    def test_dispatch_many_duplicate_recipient_has_zero_effects(self) -> None:
        mission_root = self.runs / "missions" / self.mission_id
        before_events = self.control.events()
        before_files = sorted(
            str(path.relative_to(mission_root)) for path in mission_root.rglob("*")
        )
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            with self.assertRaisesRegex(
                fleet_control.FleetControlError,
                "recipient_instance values must be unique",
            ):
                self.control.dispatch_many(
                    [
                        {
                            "recipient_instance": "scout",
                            "capability": "recon",
                            "objective": "first identity for one pane",
                            "idempotency_key": "many:duplicate:one",
                        },
                        {
                            "recipient_instance": "scout",
                            "capability": "recon",
                            "objective": "second identity for the same pane",
                            "idempotency_key": "many:duplicate:two",
                        },
                    ]
                )
        self.assertEqual(self.control.events(), before_events)
        self.assertEqual(
            sorted(
                str(path.relative_to(mission_root)) for path in mission_root.rglob("*")
            ),
            before_files,
        )
        self.assertEqual(self.calls, [])

    def test_non_delegating_specialist_receives_read_scoped_token(self) -> None:
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            dispatched = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="inspect without subdelegating",
                idempotency_key="token:read-only",
            )

        self.assertIsNotNone(dispatched["token_id"])
        token = fleet_delegation.load_token(
            self.runs, self.mission_id, dispatched["token_id"]
        )
        self.assertFalse(token["can_delegate"])
        self.assertEqual(token["allowed_capabilities"], [])
        self.assertEqual(token["remaining_budget"], 0)
        self.assertEqual(token["allowed_artifact_ids"], [])
        prompt = self.prompt_by_instance["scout"]
        self.assertIn(f"CAPABILITY_TOKEN_ID={dispatched['token_id']}", prompt)
        self.assertIn("authenticated fleet_control MCP server", prompt)
        self.assertIn(f"_caller_token_id={dispatched['token_id']}", prompt)
        self.assertNotIn("CAPABILITY_TOKEN=", prompt)
        self.assertNotIn(str(self.runs), prompt)

    def test_hotfix_leaf_token_uses_zero_specialist_hop_depth(self) -> None:
        self.runs, self.mission_id, self.lead_run_id = create_running_mission(
            self.tmp / "hotfix", feature="hotfix-control", workflow_name="hotfix"
        )
        self.control = fleet_control.FleetControl(self.runs, self.mission_id)
        self.calls = []
        self.run_by_instance = {}
        self.prompt_by_instance = {}

        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            dispatched = self.control.dispatch(
                recipient_instance="candidate_codex",
                capability="candidate_generation",
                objective="produce one bounded hotfix candidate",
                idempotency_key="hotfix:leaf",
            )

        token = fleet_delegation.load_token(
            self.runs, self.mission_id, dispatched["token_id"]
        )
        self.assertEqual(token["current_depth"], 0)
        self.assertEqual(token["max_depth"], 0)
        self.assertFalse(token["can_delegate"])
        self.assertIn("DEPTH=0/0", self.prompt_by_instance["candidate_codex"])

    def test_delegating_run_is_token_bound_before_interactive_transfer(self) -> None:
        observed: dict[str, str] = {}

        def observe_transfer(command: list[str], *, runs_dir: Path, timeout=None):
            self.assertIn("--run-id", command)
            run_id = command[command.index("--run-id") + 1]
            self.assertIn(f"RUN_ID={run_id}", command[3])
            events = self.control.events()
            issued = [
                event for event in events if event["kind"] == "capability_token_issued"
            ]
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
                "schema_version": 2,
                "caller": {"instance": "scout", "run_id": run_id, "token_id": token_id},
                "request": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            }
            with self.assertRaisesRegex(
                fleet_control.FleetControlError,
                "durable delegation|lifecycle evidence",
            ):
                fleet_mcp.handle_socket_envelope(
                    self.control, envelope, endpoint_instance="scout"
                )
            observed.update(run_id=run_id, token_id=token_id)
            return self.fake_run(command, runs_dir=runs_dir, timeout=timeout)

        with mock.patch.object(
            fleet_control, "run_process", side_effect=observe_transfer
        ):
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

    def test_same_prompt_with_distinct_keys_uses_distinct_exact_runs(self) -> None:
        objective = "inspect the same parser surface"
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            first = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective=objective,
                idempotency_key="same-prompt:first",
            )
            self.mark_succeeded("scout", b"STATUS: DONE\nEVIDENCE: first\n")
            self.control.wait([first["run_id"]], timeout_seconds=30)

            second = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective=objective,
                idempotency_key="same-prompt:second",
            )
            self.mark_succeeded("scout", b"STATUS: DONE\nEVIDENCE: second\n")
            self.control.wait([second["run_id"]], timeout_seconds=30)

        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(
            [Path(call[0]).name for call in self.calls],
            ["fleet-send.sh", "fleet-wait.sh", "fleet-send.sh", "fleet-wait.sh"],
        )
        current = self.control.state()
        self.assertEqual(current["run_claims"], {})
        for run_id in (first["run_id"], second["run_id"]):
            owner = current["run_owners"][run_id]
            self.assertEqual(
                current["admissions"][owner["owner_id"]]["phase"], "finalized"
            )

    def test_wrapper_accept_then_start_failure_reconciles_without_relaunch(
        self,
    ) -> None:
        original_start = fleet_admission.mark_started
        attempts = 0

        def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise mission_state.MissionConflict(
                    "simulated crash after exact wrapper acceptance"
                )
            return original_start(*args, **kwargs)

        request = {
            "recipient_instance": "scout",
            "capability": "recon",
            "objective": "reconcile an accepted exact wrapper effect",
            "idempotency_key": "retry:accepted-before-start",
        }
        with (
            mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run),
            mock.patch.object(fleet_admission, "mark_started", side_effect=fail_once),
        ):
            with self.assertRaisesRegex(
                fleet_control.FleetControlError,
                "wrapper accepted the exact run but durable start failed",
            ):
                self.control.dispatch(**request)
            authorized = next(
                admission
                for admission in self.control.state()["admissions"].values()
                if admission["request_key"] == request["idempotency_key"]
            )
            self.assertEqual(authorized["phase"], "authorized")
            self.assertIsNotNone(authorized["launch_authorization"])

            retried = self.control.dispatch(**request)

        self.assertEqual(retried["run_id"], authorized["run_id"])
        self.assertTrue(retried["reused"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            self.control.state()["admissions"][authorized["admission_id"]]["phase"],
            "started",
        )

    def test_post_reserve_retry_cannot_change_effect_binding(self) -> None:
        with mock.patch.object(
            fleet_delegation,
            "issue_token",
            side_effect=fleet_delegation.DelegationError(
                "simulated crash immediately after reservation"
            ),
        ):
            with self.assertRaisesRegex(
                fleet_delegation.DelegationError, "immediately after reservation"
            ):
                self.control.dispatch(
                    recipient_instance="scout",
                    capability="recon",
                    objective="original immutable objective",
                    idempotency_key="retry:post-reserve-binding",
                    expected_output_contract={"type": "original"},
                    can_delegate=True,
                    allowed_capabilities=["verify"],
                    remaining_budget=1,
                )

        original = next(
            admission
            for admission in self.control.state()["admissions"].values()
            if admission["request_key"] == "retry:post-reserve-binding"
        )
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            with self.assertRaisesRegex(
                mission_state.MissionConflict,
                "idempotency|conflict|payload",
            ):
                self.control.dispatch(
                    recipient_instance="scout",
                    capability="recon",
                    objective="mutated objective after reserve",
                    idempotency_key="retry:post-reserve-binding",
                    expected_output_contract={"type": "mutated"},
                    can_delegate=True,
                    allowed_capabilities=["challenge"],
                    remaining_budget=1,
                )

        durable = self.control.state()["admissions"][original["admission_id"]]
        self.assertEqual(durable["effect_sha256"], original["effect_sha256"])
        self.assertEqual(durable["phase"], "reserved")
        self.assertEqual(self.calls, [])

    def test_dispatch_rejects_non_json_output_contract(self) -> None:
        with self.assertRaisesRegex(fleet_control.FleetControlError, "canonical JSON"):
            self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="inspect",
                idempotency_key="bad-contract",
                expected_output_contract={"bad": {"not", "json"}},
            )

    def test_duplicate_artifact_inputs_fail_before_token_intent_or_run(self) -> None:
        before = self.control.events()
        with self.assertRaisesRegex(fleet_control.FleetControlError, "must be unique"):
            self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="must fail before any effect",
                idempotency_key="duplicate:inputs",
                input_artifact_ids=["a" * 64, "a" * 64],
            )
        self.assertEqual(self.control.events(), before)
        self.assertEqual(self.calls, [])
        token_root = fleet_delegation.token_root(self.runs, self.mission_id)
        self.assertFalse(token_root.exists() and list(token_root.glob("*.json")))

    def test_retry_rejects_tokenless_registered_delegation(self) -> None:
        objective = "legacy tokenless delegation"
        key = "retry:tokenless"
        delegation_id = str(uuid.uuid5(uuid.UUID(self.mission_id), f"delegation:{key}"))
        member = self.control.members()["scout"]
        with self.assertRaisesRegex(
            mission_state.MissionConflict, "committed admission"
        ):
            mission_state.append_event(
                self.runs,
                self.mission_id,
                kind="delegation_registered",
                actor="CONTROL",
                idempotency_key="fixture:tokenless",
                payload={
                    "delegation_id": delegation_id,
                    "mission_id": self.mission_id,
                    "run_id": str(uuid.uuid4()),
                    "parent_run_id": self.lead_run_id,
                    "delegated_by": "lead",
                    "recipient_instance": "scout",
                    "capability": "recon",
                    "objective_sha256": hashlib.sha256(objective.encode()).hexdigest(),
                    "input_artifact_ids": [],
                    "expected_output_contract": {
                        "type": "text",
                        "required": ["status", "evidence"],
                    },
                    "deadline": "2099-01-01T00:00:00+00:00",
                    "provider": member["provider"],
                    "model": member["model"],
                    "variant": member.get("variant"),
                    "depth": 1,
                    "token_id": None,
                },
            )
        self.assertEqual(self.calls, [])

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
            with self.assertRaisesRegex(RuntimeError, "delegated budget"):
                self.control.dispatch(
                    recipient_instance="challenger",
                    capability="challenge",
                    objective="amplify the delegated budget",
                    idempotency_key="child:amplify",
                    parent_run_id=scout["run_id"],
                    token_id=scout["token_id"],
                    can_delegate=True,
                    allowed_capabilities=["challenge", "build"],
                    remaining_budget=3,
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
                self.control.state()["delegations"][child["delegation_id"]]["depth"], 1
            )
            with self.assertRaisesRegex(
                fleet_control.FleetControlError, "never grant write"
            ):
                self.control.dispatch(
                    recipient_instance="builder",
                    capability="build",
                    objective="write from a child",
                    idempotency_key="child:writer",
                    parent_run_id=scout["run_id"],
                    token_id=scout["token_id"],
                )

    def test_denied_subdelegation_does_not_consume_budget(self) -> None:
        def allocations() -> list[dict]:
            return [
                event
                for event in self.control.events()
                if event["kind"] == "delegation_budget_allocated"
            ]

        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            scout = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="hold one exact child credit",
                idempotency_key="denied-budget:parent",
                can_delegate=True,
                allowed_capabilities=["build", "challenge"],
                remaining_budget=1,
            )
            before = allocations()
            with self.assertRaisesRegex(
                fleet_control.FleetControlError, "never grant write"
            ):
                self.control.dispatch(
                    recipient_instance="builder",
                    capability="build",
                    objective="invalid writer child",
                    idempotency_key="denied-budget:writer",
                    parent_run_id=scout["run_id"],
                    token_id=scout["token_id"],
                )
            self.assertEqual(allocations(), before)
            child = self.control.dispatch(
                recipient_instance="challenger",
                capability="challenge",
                objective="valid child still has the credit",
                idempotency_key="denied-budget:valid",
                parent_run_id=scout["run_id"],
                token_id=scout["token_id"],
            )
        self.assertEqual(child["recipient_instance"], "challenger")
        self.assertEqual(len(self.calls), 2)

    def test_dispatch_many_static_failure_writes_no_budget_event(self) -> None:
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            scout = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="prepare an atomic batch",
                idempotency_key="static-batch:parent",
                can_delegate=True,
                allowed_capabilities=["build", "challenge"],
                remaining_budget=2,
            )
            before = [
                event
                for event in self.control.events()
                if event["kind"] == "delegation_budget_allocated"
            ]
            with self.assertRaisesRegex(
                fleet_control.FleetControlError, "never grant write"
            ):
                self.control.dispatch_many(
                    [
                        {
                            "recipient_instance": "challenger",
                            "capability": "challenge",
                            "objective": "valid first request",
                            "idempotency_key": "static-batch:valid",
                            "parent_run_id": scout["run_id"],
                            "token_id": scout["token_id"],
                        },
                        {
                            "recipient_instance": "builder",
                            "capability": "build",
                            "objective": "invalid writer request",
                            "idempotency_key": "static-batch:invalid",
                            "parent_run_id": scout["run_id"],
                            "token_id": scout["token_id"],
                        },
                    ]
                )
        after = [
            event
            for event in self.control.events()
            if event["kind"] == "delegation_budget_allocated"
        ]
        self.assertEqual(after, before)
        self.assertEqual(len(self.calls), 1)

    def test_child_capability_scope_cannot_exceed_parent(self) -> None:
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            scout = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="delegate only challenge",
                idempotency_key="scope:parent",
                can_delegate=True,
                allowed_capabilities=["challenge"],
                remaining_budget=2,
            )
            with self.assertRaisesRegex(RuntimeError, "capabilit"):
                self.control.dispatch(
                    recipient_instance="challenger",
                    capability="challenge",
                    objective="must not receive verify authority",
                    idempotency_key="scope:child",
                    parent_run_id=scout["run_id"],
                    token_id=scout["token_id"],
                    can_delegate=True,
                    allowed_capabilities=["verify"],
                    remaining_budget=1,
                )
        self.assertEqual(len(self.calls), 1)

    def test_dispatch_many_reserves_total_budget_before_any_child_launch(self) -> None:
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            scout = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="split a bounded descendant budget",
                idempotency_key="budget:parent",
                can_delegate=True,
                allowed_capabilities=["challenge", "verify"],
                remaining_budget=3,
            )
            requests = [
                {
                    "recipient_instance": "challenger",
                    "capability": "challenge",
                    "objective": "first child with one descendant",
                    "idempotency_key": "budget:child-one",
                    "parent_run_id": scout["run_id"],
                    "token_id": scout["token_id"],
                    "can_delegate": True,
                    "allowed_capabilities": ["challenge"],
                    "remaining_budget": 1,
                },
                {
                    "recipient_instance": "verifier",
                    "capability": "verify",
                    "objective": "second child with one descendant",
                    "idempotency_key": "budget:child-two",
                    "parent_run_id": scout["run_id"],
                    "token_id": scout["token_id"],
                    "can_delegate": True,
                    "allowed_capabilities": ["verify"],
                    "remaining_budget": 1,
                },
            ]
            with self.assertRaisesRegex(RuntimeError, "budget"):
                self.control.dispatch_many(requests)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.control.state()["delegations"]), 1)

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
        self.assertNotIn(
            str(self.runs / "missions" / self.mission_id / "artifacts"),
            self.prompt_by_instance["challenger"],
        )
        self.assertIn(
            "Retrieve artifact bytes only with fleet_control.get_result",
            self.prompt_by_instance["challenger"],
        )
        relay_events = [
            event
            for event in self.control.events()
            if event["kind"] == "result_relayed"
        ]
        self.assertEqual(
            relay_events[0]["payload"]["recipient_run_id"], relayed["run_id"]
        )

    def test_wait_rejects_task_hash_drift_before_cas_or_result_event(self) -> None:
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            dispatched = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="produce prompt-bound evidence",
                idempotency_key="wait:task-drift",
            )
            result = self.mark_succeeded("scout", b"must not enter CAS\n")
            ledger = self.runs / "fleet-fleet-control.ledger.jsonl"
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            for row in rows:
                if row.get("run_id") == dispatched["run_id"]:
                    row["task_sha256"] = "f" * 64
            ledger.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                fleet_control.FleetControlError,
                "task_sha256 differs.*prompt_sha256",
            ):
                self.control.wait([dispatched["run_id"]], timeout_seconds=30)

        artifact_id = hashlib.sha256(result.read_bytes()).hexdigest()
        artifact_path = fleet_artifacts.artifact_path(
            self.runs, self.mission_id, artifact_id
        )
        self.assertFalse(artifact_path.exists())
        self.assertFalse(
            any(
                event["kind"] == "result_recorded"
                and event["payload"].get("run_id") == dispatched["run_id"]
                for event in self.control.events()
            )
        )

    def test_local_cancel_rejects_without_signalling_reused_surface(self) -> None:
        self.runs, self.mission_id, self.lead_run_id = create_running_mission(
            self.tmp / "local-cancel",
            feature="local-cancel",
            workflow_name="research",
        )
        self.control = fleet_control.FleetControl(self.runs, self.mission_id)
        self.calls = []
        self.run_by_instance = {}
        self.prompt_by_instance = {}
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            dispatched = self.control.dispatch(
                recipient_instance="triage_scope",
                capability="recon",
                objective="run A on a reusable local surface",
                idempotency_key="cancel:local-a",
            )

        with mock.patch.object(fleet_control, "run_process") as effect:
            with self.assertRaisesRegex(
                fleet_control.FleetControlError,
                "exact run-scoped handle",
            ):
                self.control.cancel(
                    run_id=dispatched["run_id"],
                    reason="surface may now host run B",
                    idempotency_key="cancel:local-a:request",
                )
        effect.assert_not_called()
        self.assertFalse(
            any(
                event["kind"] == "run_cancel_requested"
                and event["payload"].get("run_id") == dispatched["run_id"]
                for event in self.control.events()
            )
        )

    def test_wait_rejects_symlinked_result_ancestor(self) -> None:
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            dispatched = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="produce evidence beneath a pinned result root",
                idempotency_key="result-path:ancestor",
            )
            result = self.mark_succeeded("scout", b"trusted result\n")
            feature_dir = result.parent
            original = feature_dir.with_name(feature_dir.name + "-original")
            feature_dir.rename(original)
            outside = self.tmp / "outside-results"
            outside.mkdir(mode=0o700)
            (outside / result.name).write_bytes(b"trusted result\n")
            feature_dir.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                fleet_control.FleetControlError, "unsafe frontier result path"
            ):
                self.control.wait([dispatched["run_id"]], timeout_seconds=30)

    def test_orphan_cas_object_is_not_an_authorized_result(self) -> None:
        orphan = fleet_artifacts.put_bytes(
            self.runs, self.mission_id, b"stored but never accepted\n"
        )

        with self.assertRaisesRegex(
            fleet_control.FleetControlError, "not an attested mission result"
        ):
            self.control.get_result(orphan["artifact_id"])
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            with self.assertRaisesRegex(
                fleet_control.FleetControlError, "not an attested mission result"
            ):
                self.control.relay_result(
                    artifact_id=orphan["artifact_id"],
                    recipient_instance="challenger",
                    capability="challenge",
                    objective="must not receive an orphan object",
                    idempotency_key="relay:orphan",
                )
        self.assertFalse(self.calls)
        self.assertFalse(
            any(event["kind"] == "result_relayed" for event in self.control.events())
        )

    def test_artifact_tampering_and_identity_drift_fail_closed(self) -> None:
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            scout = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="produce evidence",
                idempotency_key="drift:scout",
            )
            self.mark_succeeded("scout", b"result\n")
            ledger = self.runs / "fleet-fleet-control.ledger.jsonl"
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            rows[-1]["model"] = "wrong-model"
            ledger.write_text(
                "".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                fleet_control.FleetControlError, "identity drift"
            ):
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
                "python3",
                str(Path(fleet_control.__file__)),
                "--runs-dir",
                str(self.runs),
                "--mission-id",
                self.mission_id,
                "inspect-mission",
            ],
            cwd=Path(fleet_control.__file__).resolve().parents[1],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["mission_id"], self.mission_id)

        mcp_input = (
            "\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {},
                        }
                    ),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                ]
            )
            + "\n"
        )
        mcp = subprocess.run(
            [
                "python3",
                str(Path(fleet_mcp.__file__)),
                "--runs-dir",
                str(self.runs),
                "--mission-id",
                self.mission_id,
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
