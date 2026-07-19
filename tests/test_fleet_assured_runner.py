from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

from tests.mission_control_test_support import create_running_mission


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleet_assured_runner as assured  # noqa: E402


def event(sequence: int, action: dict, *, status: str = "running") -> dict:
    return {
        "sequence": sequence,
        "event_sha256": f"{sequence:064x}",
        "snapshot": {
            "status": status,
            "accepted_head_sha": "a" * 40,
        },
        "next_action": action,
    }


class FleetAssuredRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runner = object.__new__(assured.AssuredRunner)
        self.runner.runs_dir = self.tmp
        self.runner.mission_id = "00000000-0000-4000-8000-000000000001"
        self.runner.feature = "assured"
        self.runner.root = self.tmp / "missions" / self.runner.mission_id
        self.runner.manifest_path = self.tmp / "fleet-assured.manifest"
        self.runner.manifest = {
            "lead": "surface:0",
            "lead.runner": "interactive",
            "lead.authority": "control",
            "lead.phase": "CONTROL",
            "lead.provider": "anthropic",
            "lead.model": "claude-fable-5",
            "maker": "surface:1",
            "maker.runner": "interactive",
            "maker.authority": "write",
            "maker.phase": "BUILD",
            "maker.provider": "anthropic",
            "maker.model": "claude-fable-5",
            "challenge": "surface:2",
            "challenge.runner": "interactive",
            "challenge.authority": "advisory",
            "challenge.phase": "CHALLENGE",
            "verify": "surface:3",
            "verify.runner": "interactive",
            "verify.authority": "verification",
            "verify.phase": "VERIFY",
            "verify.role_type": "verifier",
            "verify.provider": "anthropic",
            "verify.model": "claude-fable-5",
        }
        self.runner._phase_manifest = self.runner.manifest

    def phase_pair(self, name: str) -> tuple[assured.AssuredRunner, Path, Path]:
        root = self.tmp / name
        root.mkdir()
        manifest_path = root / "fleet-assured.manifest"
        manifest_path.write_text(
            "feature=assured\n"
            "mode=guided\n"
            "preset=fleet_dialogue\n"
            "lead=surface:1\n"
            "lead.uuid=00000000-0000-0000-0000-000000000101\n"
            "lead.phase=CONTROL\n"
            "maker=surface:2\n"
            "maker.phase=BUILD\n"
            "challenge=surface:3\n"
            "challenge.phase=CHALLENGE\n"
            "verify=surface:4\n"
            "verify.phase=VERIFY\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        manifest = assured.fleet_state.manifest_values(manifest_path)
        state_path = manifest_path.with_suffix(".state.json")
        state_path.write_text(
            json.dumps(
                assured.fleet_state._new_state(manifest),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        state_path.chmod(0o600)
        runner = object.__new__(assured.AssuredRunner)
        runner.manifest_path = manifest_path
        runner._phase_manifest = manifest
        return runner, manifest_path, state_path

    @staticmethod
    def tree_snapshot(root: Path) -> dict[Path, tuple[str, bytes | str]]:
        snapshot: dict[Path, tuple[str, bytes | str]] = {}
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if path.is_symlink():
                snapshot[relative] = ("symlink", os.readlink(path))
            elif path.is_file():
                snapshot[relative] = ("file", path.read_bytes())
            else:
                snapshot[relative] = ("directory", b"")
        return snapshot

    def activate_assured_mission(self) -> tuple[Path, str, dict]:
        runs, mission_id, lead_run_id = create_running_mission(
            self.tmp, feature="assured"
        )
        self.runner.runs_dir = runs
        self.runner.mission_id = mission_id
        self.runner.root = runs / "missions" / mission_id
        self.runner.manifest_path = runs / "fleet-assured.manifest"
        self.runner.compiled, self.runner.mission = (
            assured.fleet_mission.load_mission_compiled(
                runs,
                mission_id,
                mode="effect",
            )
        )
        current = assured.fleet_mission.load_state(runs, mission_id)
        lead = current["admissions"][current["lead_admission_id"]]
        assured.fleet_admission.finalize(
            runs,
            mission_id,
            admission_id=lead["admission_id"],
            recipient_instance=lead["recipient_instance"],
            writer=bool(lead["writer"]),
            terminal_evidence={
                "schema_version": 1,
                "source_event_sha256": assured.mission_state.sha256(
                    {"run_id": lead_run_id, "status": "succeeded"}
                ),
                "run_id": lead_run_id,
                "task_sha256": lead["task_sha256"],
                "status": "succeeded",
            },
            reason="retire the main Lead before the ASSURED lane",
            idempotency_key="test:lead:finalize",
            actor="CONTROL",
        )
        assured.mission_state.append_event(
            runs,
            mission_id,
            kind="risk_escalated",
            actor="CONTROL",
            idempotency_key="test:assured:risk",
            payload={
                "from": "low",
                "to": "high",
                "categories": ["production"],
                "reason": "exercise the assured dispatch lane",
            },
        )
        request, _ = assured.mission_state.append_event(
            runs,
            mission_id,
            kind="assurance_requested",
            actor="CONTROL",
            idempotency_key="test:assured:request",
            payload={
                "risk": "high",
                "categories": ["production"],
                "scope": current["target_repo"],
                "workflow_digest": current["workflow_digest"],
            },
        )
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        approval, _ = assured.mission_state.append_event(
            runs,
            mission_id,
            kind="assurance_approved",
            actor="HUMAN",
            idempotency_key="test:assured:approval",
            payload={
                "approval_id": "00000000-0000-4000-8000-000000000002",
                "request_event_sha256": request["event_sha256"],
                "workflow_digest": current["workflow_digest"],
                "scope": current["target_repo"],
                "risk": "high",
                "expires_at": expires_at.isoformat(),
                "expires_in_seconds": 3600,
                "approved_by_sha256": "c" * 64,
                "decision": "approved",
            },
        )
        assured.mission_state.append_event(
            runs,
            mission_id,
            kind="assurance_boot_started",
            actor="CONTROL",
            idempotency_key="test:assured:boot",
            payload={
                "preset": "fleet_dialogue",
                "approval_event_sha256": approval["event_sha256"],
            },
        )
        assured.mission_state.append_event(
            runs,
            mission_id,
            kind="assurance_started",
            actor="CONTROL",
            idempotency_key="test:assured:started",
            payload={
                "manifest": str(self.runner.manifest_path),
                "approval_event_sha256": approval["event_sha256"],
            },
        )
        self.runner.audit = mock.Mock()
        return runs, mission_id, approval

    def authorize_assured_action(
        self,
        *,
        instance: str,
        action_key: str,
        capability: str,
        prompt_sha256: str,
        approval_event_sha256: str,
    ) -> dict:
        admission = self.runner._reserve_effect(
            instance=instance,
            action_key=action_key,
            capability=capability,
            prompt_sha256=prompt_sha256,
            approval_event_sha256=approval_event_sha256,
        )
        committed = assured.fleet_admission.commit(
            self.runner.runs_dir,
            self.runner.mission_id,
            admission_id=admission["admission_id"],
            request_digest=admission["request_digest"],
            effect_sha256=admission["effect_sha256"],
            recipient_instance=instance,
            writer=bool(admission["writer"]),
            run_id=admission["run_id"],
            idempotency_key=f"admission:commit:{admission['request_key']}",
            actor="ASSURED",
        )
        assured.fleet_admission.authorize_launch(
            self.runner.runs_dir,
            self.runner.mission_id,
            admission_id=admission["admission_id"],
            commit_event_sha256=committed["commit_event_sha256"],
            request_digest=admission["request_digest"],
            effect_sha256=admission["effect_sha256"],
            recipient_instance=instance,
            writer=bool(admission["writer"]),
            run_id=admission["run_id"],
            approval_event_sha256=approval_event_sha256,
            idempotency_key=f"admission:authorize:{admission['request_key']}",
            actor="ASSURED",
        )
        return admission

    def finalize_assured_run(self, run_id: str, prompt_sha256: str) -> None:
        self.runner._finalize_effect(
            run_id,
            terminal_evidence={
                "schema_version": 1,
                "source_event_sha256": assured.mission_state.sha256(
                    {"run_id": run_id, "status": "succeeded"}
                ),
                "run_id": run_id,
                "task_sha256": prompt_sha256,
                "status": "succeeded",
            },
        )

    def renew_assured_approval(
        self, prior: dict, *, idempotency_key: str
    ) -> tuple[dict, datetime]:
        prior_expires = datetime.fromisoformat(prior["payload"]["expires_at"])
        renewed_at = prior_expires + timedelta(seconds=1)
        renewed_expires = renewed_at + timedelta(hours=1)

        class MissionClock:
            @classmethod
            def now(cls, tz=None):
                return renewed_at if tz is not None else renewed_at.replace(tzinfo=None)

            @staticmethod
            def fromisoformat(value):
                return datetime.fromisoformat(value)

        payload = prior["payload"]
        with mock.patch.object(assured.mission_state, "datetime", MissionClock):
            renewed, _ = assured.mission_state.append_event(
                self.runner.runs_dir,
                self.runner.mission_id,
                kind="assurance_approval_renewed",
                actor="HUMAN",
                idempotency_key=idempotency_key,
                payload={
                    "approval_id": str(
                        uuid.uuid5(
                            uuid.UUID(self.runner.mission_id),
                            "approval-renewal:"
                            f"{prior['event_sha256']}:{idempotency_key}",
                        )
                    ),
                    "prior_approval_event_sha256": prior["event_sha256"],
                    "request_event_sha256": payload["request_event_sha256"],
                    "workflow_digest": payload["workflow_digest"],
                    "scope": payload["scope"],
                    "risk": payload["risk"],
                    "expires_at": renewed_expires.isoformat(),
                    "expires_in_seconds": 3600,
                    "approved_by_sha256": "e" * 64,
                    "decision": "approved",
                },
            )
        return renewed, renewed_at

    @staticmethod
    def assured_phase_state(
        phase: str, *, approval: dict, accepted_event_sha256: str
    ) -> dict:
        approved_at = datetime.fromisoformat(
            approval["timestamp"].replace("Z", "+00:00")
        )
        history = [
            {
                "phase": "CONTROL",
                "timestamp": (approved_at - timedelta(seconds=2)).isoformat(),
                "evidence": "fleet-created",
            },
            {
                "phase": "BUILD",
                "timestamp": (approved_at - timedelta(seconds=1)).isoformat(),
                "evidence": approval["payload"]["request_event_sha256"],
            },
        ]
        if phase in {"CHALLENGE", "VERIFY"}:
            history.append(
                {
                    "phase": "CHALLENGE",
                    "timestamp": (approved_at + timedelta(seconds=1)).isoformat(),
                    "evidence": accepted_event_sha256,
                    "approval_event_sha256": approval["event_sha256"],
                }
            )
        if phase == "VERIFY":
            history.append(
                {
                    "phase": "VERIFY",
                    "timestamp": (approved_at + timedelta(seconds=2)).isoformat(),
                    "evidence": "verified-phase-receipt",
                }
            )
        return {"active_phase": phase, "history": history}

    def test_legacy_lifecycle_read_rejects_symlink_without_external_mutation(
        self,
    ) -> None:
        ledger = self.tmp / "fleet-assured.ledger.jsonl"
        outside = self.tmp / "outside-ledger.jsonl"
        outside.write_text('{"run_id":"external"}\n', encoding="utf-8")
        outside.chmod(0o600)
        ledger.symlink_to(outside)
        before = outside.read_bytes()

        with self.assertRaisesRegex(assured.AssuredRunnerError, "unsafe or corrupt"):
            self.runner._legacy_events()

        self.assertEqual(outside.read_bytes(), before)
        self.assertTrue(ledger.is_symlink())

    def test_manifest_read_rejects_symlink_and_duplicate_keys(self) -> None:
        manifest = self.tmp / "fleet-assured.manifest"
        outside = self.tmp / "outside.manifest"
        outside.write_text("feature=assured\n", encoding="utf-8")
        outside.chmod(0o600)
        manifest.symlink_to(outside)

        with self.assertRaisesRegex(
            assured.AssuredRunnerError, "cannot read assured manifest safely"
        ):
            assured.parse_manifest(manifest)

        manifest.unlink()
        manifest.write_text("feature=assured\nfeature=other\n", encoding="utf-8")
        manifest.chmod(0o600)
        with self.assertRaisesRegex(
            assured.AssuredRunnerError, "duplicate manifest key"
        ):
            assured.parse_manifest(manifest)

    def test_phase_reader_rejects_path_corruption_and_binding_drift_read_only(
        self,
    ) -> None:
        for scenario in ("symlink", "hardlink", "corrupt", "manifest-drift"):
            with self.subTest(scenario=scenario):
                runner, manifest, state = self.phase_pair(f"phase-{scenario}")
                self.assertEqual(runner._phase_state()["active_phase"], "CONTROL")
                if scenario == "symlink":
                    outside = state.with_name("outside-state.json")
                    state.rename(outside)
                    state.symlink_to(outside.name)
                elif scenario == "hardlink":
                    outside = state.with_name("outside-state.json")
                    os.link(state, outside)
                elif scenario == "corrupt":
                    state.write_bytes(b'{"schema_version":2,"schema_version":2}\n')
                else:
                    manifest.write_bytes(
                        manifest.read_bytes().replace(
                            b"preset=fleet_dialogue",
                            b"preset=changed",
                            1,
                        )
                    )
                before = self.tree_snapshot(manifest.parent)

                with self.assertRaisesRegex(
                    assured.AssuredRunnerError,
                    "cannot read assured fleet phase",
                ):
                    runner._phase_state()

                self.assertEqual(self.tree_snapshot(manifest.parent), before)

    def test_manifest_binding_requires_exact_mission_base_workflow_and_roster(
        self,
    ) -> None:
        mission = {
            "mission_id": self.runner.mission_id,
            "feature": "assured",
            "target_repo": "/private/tmp/target",
            "base_sha": "a" * 40,
            "workflow_digest": "b" * 64,
        }
        manifest = {
            "mission_id": self.runner.mission_id,
            "feature": "assured",
            "preset": "fleet_dialogue",
            "mode": "assured",
            "target_repo": mission["target_repo"],
            "base_sha": mission["base_sha"],
            "maker.authority": "write",
            "maker.base_sha": mission["base_sha"],
        }
        compiled = {"workflow_digest": mission["workflow_digest"]}
        with (
            mock.patch.object(
                assured.fleet_manifest, "verify_compiled_binding"
            ) as verify_compiled,
            mock.patch.object(assured.fdp2, "_validate_roster") as verify_roster,
        ):
            assured.validate_manifest_binding(manifest, mission, compiled)
        verify_compiled.assert_called_once_with(manifest, compiled)
        verify_roster.assert_called_once_with(manifest)

        drifts = (
            ("target_repo", "/private/tmp/other", "target repository"),
            ("base_sha", "c" * 40, "base_sha"),
            ("maker.base_sha", "d" * 40, "writer base_sha"),
        )
        for field, value, expected in drifts:
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(assured.AssuredRunnerError, expected),
            ):
                assured.validate_manifest_binding(
                    {**manifest, field: value}, mission, compiled
                )

        with self.assertRaisesRegex(
            assured.AssuredRunnerError, "compiled workflow digest"
        ):
            assured.validate_manifest_binding(
                manifest, mission, {"workflow_digest": "e" * 64}
            )
        with (
            mock.patch.object(
                assured.fleet_manifest,
                "verify_compiled_binding",
                side_effect=assured.fleet_manifest.ManifestError("roster drift"),
            ),
            self.assertRaisesRegex(
                assured.AssuredRunnerError, "workflow/roster mismatch"
            ),
        ):
            assured.validate_manifest_binding(manifest, mission, compiled)

    def test_compiled_v1_or_drift_blocks_assured_effects_before_audit(self) -> None:
        with (
            mock.patch.object(
                assured.mission_state,
                "normalize_uuid",
                return_value=self.runner.mission_id,
            ),
            mock.patch.object(
                assured.mission_state, "mission_root", return_value=self.runner.root
            ),
            mock.patch.object(assured, "parse_manifest", return_value={}),
            mock.patch.object(
                assured.fleet_mission,
                "load_mission_compiled",
                side_effect=assured.fleet_mission.MissionError(
                    "durable compiled workflow is not authorized for effect: "
                    "compiled schema_version=1 is historical read-only; effects require v2"
                ),
            ),
            mock.patch.object(assured, "validate_manifest_binding") as validate_binding,
            mock.patch.object(assured.fleet_audit_client, "AuditLifecycle") as audit,
            self.assertRaisesRegex(
                assured.AssuredRunnerError,
                "effects require a valid compiled workflow v2",
            ),
        ):
            assured.AssuredRunner(self.tmp, self.runner.mission_id)

        validate_binding.assert_not_called()
        audit.assert_not_called()

    def test_dispatch_retry_reconciles_the_same_run_without_resend(self) -> None:
        runs, mission_id, approval = self.activate_assured_mission()
        prompt = self.tmp / "prompt.txt"
        prompt.write_text("exact prompt", encoding="utf-8")
        action = {
            "instance": "maker",
            "prompt_file": str(prompt),
            "prompt_sha256": hashlib.sha256(b"exact prompt").hexdigest(),
        }
        request_key = (
            "assured-"
            + assured.mission_state.sha256(
                {
                    "action": "action:1",
                    "approval_event_sha256": approval["event_sha256"],
                }
            )[:40]
        )
        exact_run = assured.fleet_admission.deterministic_ids(
            mission_id,
            request_key=request_key,
            run_kind="specialist",
        )["run_id"]
        sent = subprocess.CompletedProcess(
            ["fleet-send.sh"], 0, json.dumps({"run_id": exact_run}) + "\n", ""
        )
        with (
            mock.patch.object(self.runner, "_record"),
            mock.patch.object(
                self.runner, "_phase_state", return_value={"active_phase": "BUILD"}
            ),
            mock.patch.object(
                self.runner, "_reconcile_run", side_effect=[None, exact_run]
            ),
            mock.patch.object(assured.fleet_control, "require_usage_launch"),
            mock.patch.object(assured, "run_process", return_value=sent) as run,
        ):
            self.assertEqual(self.runner._dispatch(action, "action:1"), exact_run)
            self.assertEqual(self.runner._dispatch(action, "action:1"), exact_run)
        run.assert_called_once()
        current = assured.fleet_mission.load_state(runs, mission_id)
        owner = current["run_owners"][exact_run]
        self.assertEqual(owner["owner_kind"], "admission")
        self.assertEqual(current["admissions"][owner["owner_id"]]["phase"], "started")

    def test_same_prompt_distinct_actions_use_distinct_exact_runs(self) -> None:
        runs, mission_id, _ = self.activate_assured_mission()
        prompt = self.tmp / "same-prompt.txt"
        prompt.write_text("same assured prompt", encoding="utf-8")
        prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
        action = {
            "instance": "maker",
            "prompt_file": str(prompt),
            "prompt_sha256": prompt_sha256,
        }

        def accept_exact(command, *, runs_dir, timeout=None):
            del timeout
            run_id = command[command.index("--run-id") + 1]
            assured.fleet_ledger.append_event(
                runs_dir / "fleet-assured.ledger.jsonl",
                {
                    "timestamp": "2026-07-17T00:00:00Z",
                    "run_id": run_id,
                    "feature": "assured",
                    "instance": "maker",
                    "status": "dispatched",
                    "task_sha256": prompt_sha256,
                },
            )
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"run_id": run_id}) + "\n", ""
            )

        with (
            mock.patch.object(self.runner, "_record"),
            mock.patch.object(
                self.runner, "_phase_state", return_value={"active_phase": "BUILD"}
            ),
            mock.patch.object(assured.fleet_control, "require_usage_launch"),
            mock.patch.object(
                assured, "run_process", side_effect=accept_exact
            ) as wrapper,
        ):
            first = self.runner._dispatch(action, "same-prompt:first")
            self.finalize_assured_run(first, prompt_sha256)
            second = self.runner._dispatch(action, "same-prompt:second")
            self.finalize_assured_run(second, prompt_sha256)

        self.assertNotEqual(first, second)
        self.assertEqual(wrapper.call_count, 2)
        current = assured.fleet_mission.load_state(runs, mission_id)
        self.assertEqual(current["run_claims"], {})
        for run_id in (first, second):
            owner = current["run_owners"][run_id]
            self.assertEqual(
                current["admissions"][owner["owner_id"]]["phase"], "finalized"
            )

    def test_same_action_concurrency_serializes_wrapper_but_distinct_actions_do_not(
        self,
    ) -> None:
        runs, mission_id, _ = self.activate_assured_mission()
        prompt = self.tmp / "concurrent-same-action.txt"
        prompt.write_text("one exact concurrent action", encoding="utf-8")
        prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
        action = {
            "instance": "maker",
            "prompt_file": str(prompt),
            "prompt_sha256": prompt_sha256,
        }
        wrapper_entered = threading.Event()
        release_wrapper = threading.Event()
        wrapper_calls = 0

        def serialized_wrapper(command, *, runs_dir, timeout=None):
            nonlocal wrapper_calls
            del runs_dir, timeout
            wrapper_calls += 1
            if wrapper_calls != 1:
                raise AssertionError("same action reached the wrapper twice")
            wrapper_entered.set()
            if not release_wrapper.wait(timeout=10):
                raise AssertionError("same-action wrapper was not released")
            run_id = command[command.index("--run-id") + 1]
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"run_id": run_id}) + "\n", ""
            )

        second_started = threading.Event()

        def dispatch(mark_started=None):
            if mark_started is not None:
                mark_started.set()
            return self.runner._dispatch(action, "concurrent:same")

        with (
            mock.patch.object(
                self.runner, "_phase_state", return_value={"active_phase": "BUILD"}
            ),
            mock.patch.object(assured.fleet_control, "require_usage_launch"),
            mock.patch.object(assured, "run_process", side_effect=serialized_wrapper),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(dispatch)
            self.assertTrue(wrapper_entered.wait(timeout=10))
            second = executor.submit(dispatch, second_started)
            self.assertTrue(second_started.wait(timeout=10))
            release_wrapper.set()
            first_run = first.result(timeout=10)
            second_run = second.result(timeout=10)

        self.assertEqual(first_run, second_run)
        self.assertEqual(wrapper_calls, 1)
        self.assertFalse(
            any(
                item.get("status") == "abandoned"
                for item in self.runner._legacy_events()
            )
        )
        self.finalize_assured_run(first_run, prompt_sha256)

        distinct_barrier = threading.Barrier(2)
        prompts = {}
        for instance in ("maker", "verify"):
            path = self.tmp / f"concurrent-{instance}.txt"
            path.write_text(f"distinct action for {instance}", encoding="utf-8")
            prompts[instance] = {
                "instance": instance,
                "prompt_file": str(path),
                "prompt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }

        def parallel_wrapper(command, *, runs_dir, timeout=None):
            del runs_dir, timeout
            distinct_barrier.wait(timeout=10)
            run_id = command[command.index("--run-id") + 1]
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"run_id": run_id}) + "\n", ""
            )

        with (
            mock.patch.object(
                self.runner, "_phase_state", return_value={"active_phase": "BUILD"}
            ),
            mock.patch.object(assured.fleet_control, "require_usage_launch"),
            mock.patch.object(assured, "run_process", side_effect=parallel_wrapper),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = {
                instance: executor.submit(
                    self.runner._dispatch,
                    value,
                    f"concurrent:{instance}",
                )
                for instance, value in prompts.items()
            }
            done, not_done = wait(futures.values(), timeout=20)
            self.assertFalse(not_done, "distinct assured actions did not terminate")
            failures = {
                instance: repr(future.exception())
                for instance, future in futures.items()
                if future.exception() is not None
            }
            self.assertFalse(
                failures,
                f"distinct assured actions failed before parallel dispatch: {failures}",
            )
            distinct_runs = {
                instance: future.result()
                for instance, future in futures.items()
            }

        self.assertEqual(len(set(distinct_runs.values())), 2)
        for instance, run_id in distinct_runs.items():
            self.finalize_assured_run(run_id, prompts[instance]["prompt_sha256"])
        current = assured.fleet_mission.load_state(runs, mission_id)
        self.assertFalse(any(item["active"] for item in current["admissions"].values()))

    def test_approval_expiry_before_authorization_aborts_without_wrapper(self) -> None:
        runs, mission_id, approval = self.activate_assured_mission()
        prompt = self.tmp / "expires-before-auth.txt"
        prompt.write_text("expires before authorization", encoding="utf-8")
        action = {
            "instance": "maker",
            "prompt_file": str(prompt),
            "prompt_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
        }
        expires = datetime.fromisoformat(approval["payload"]["expires_at"])

        class BarrierClock:
            current = expires - timedelta(minutes=1)

            @classmethod
            def now(cls, tz=None):
                return (
                    cls.current if tz is not None else cls.current.replace(tzinfo=None)
                )

            @staticmethod
            def fromisoformat(value):
                return datetime.fromisoformat(value)

        def expire_at_barrier(*args, **kwargs):
            del args, kwargs
            BarrierClock.current = expires + timedelta(seconds=1)

        with (
            mock.patch.object(assured, "datetime", BarrierClock),
            mock.patch.object(self.runner, "_record"),
            mock.patch.object(
                self.runner, "_phase_state", return_value={"active_phase": "BUILD"}
            ),
            mock.patch.object(self.runner, "_reconcile_run", return_value=None),
            mock.patch.object(
                assured.fleet_control,
                "require_usage_launch",
                side_effect=expire_at_barrier,
            ),
            mock.patch.object(assured, "run_process") as wrapper,
            self.assertRaises(assured.AssuredApprovalRenewalRequired) as raised,
        ):
            self.runner._dispatch(action, "expiry:before-auth")

        wrapper.assert_not_called()
        self.assertIn("fleet-approve.py", raised.exception.next_action)
        self.assertIn("--renew", raised.exception.next_action)
        current = assured.fleet_mission.load_state(runs, mission_id)
        admissions = [
            item
            for item in current["admissions"].values()
            if item["lane_actor"] == "ASSURED"
        ]
        self.assertEqual(len(admissions), 1)
        self.assertEqual(admissions[0]["phase"], "aborted")
        self.assertFalse(any(item["active"] for item in current["admissions"].values()))
        self.assertFalse(
            any(
                event["kind"] == "delegation_launch_authorized"
                and event["actor"] == "ASSURED"
                for event in assured.mission_state.read_events(
                    assured.mission_state.ledger_path(runs, mission_id),
                    expected_mission_id=mission_id,
                )
            )
        )

    def test_live_approval_retry_uses_next_attempt_after_prelaunch_abort(self) -> None:
        runs, mission_id, _ = self.activate_assured_mission()
        prompt = self.tmp / "transient-prelaunch.txt"
        prompt.write_text("retry after transient prelaunch failure", encoding="utf-8")
        prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
        action = {
            "instance": "maker",
            "prompt_file": str(prompt),
            "prompt_sha256": prompt_sha256,
        }

        def accept_exact(command, *, runs_dir, timeout=None):
            del runs_dir, timeout
            run_id = command[command.index("--run-id") + 1]
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"run_id": run_id}) + "\n", ""
            )

        with (
            mock.patch.object(self.runner, "_record"),
            mock.patch.object(
                self.runner, "_phase_state", return_value={"active_phase": "BUILD"}
            ),
            mock.patch.object(
                assured.fleet_control,
                "require_usage_launch",
                side_effect=[
                    assured.fleet_control.FleetControlError(
                        "transient usage service outage"
                    ),
                    None,
                ],
            ),
            mock.patch.object(
                assured, "run_process", side_effect=accept_exact
            ) as wrapper,
        ):
            with self.assertRaisesRegex(
                assured.AssuredRunnerError, "transient usage service outage"
            ):
                self.runner._dispatch(action, "transient:retry")
            run_id = self.runner._dispatch(action, "transient:retry")

        wrapper.assert_called_once()
        self.finalize_assured_run(run_id, prompt_sha256)
        current = assured.fleet_mission.load_state(runs, mission_id)
        admissions = [
            item
            for item in current["admissions"].values()
            if item["lane_actor"] == "ASSURED"
        ]
        self.assertEqual(len(admissions), 2)
        self.assertEqual(
            {item["phase"] for item in admissions}, {"aborted", "finalized"}
        )
        self.assertEqual(len({item["request_key"] for item in admissions}), 2)
        self.assertEqual(current["run_claims"], {})
        self.assertFalse(any(item["active"] for item in current["admissions"].values()))

    def test_authorized_before_expiry_starts_after_expiry(self) -> None:
        runs, mission_id, approval = self.activate_assured_mission()
        prompt = self.tmp / "authorized-before-expiry.txt"
        prompt.write_text("authorized before expiry", encoding="utf-8")
        prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
        action_key = "expiry:after-auth"
        action = {
            "instance": "maker",
            "prompt_file": str(prompt),
            "prompt_sha256": prompt_sha256,
        }
        approval_sha256 = approval["event_sha256"]
        expires = datetime.fromisoformat(approval["payload"]["expires_at"])
        request_key = (
            "assured-"
            + assured.mission_state.sha256(
                {
                    "action": action_key,
                    "approval_event_sha256": approval_sha256,
                }
            )[:40]
        )
        exact_run = assured.fleet_admission.deterministic_ids(
            mission_id,
            request_key=request_key,
            run_kind="specialist",
        )["run_id"]

        class BarrierClock:
            current = expires - timedelta(minutes=1)

            @classmethod
            def now(cls, tz=None):
                return (
                    cls.current if tz is not None else cls.current.replace(tzinfo=None)
                )

            @staticmethod
            def fromisoformat(value):
                return datetime.fromisoformat(value)

        authorize_launch = assured.fleet_admission.authorize_launch

        def authorize_then_expire(*args, **kwargs):
            self.assertEqual(kwargs["approval_event_sha256"], approval_sha256)
            value = authorize_launch(*args, **kwargs)
            BarrierClock.current = expires + timedelta(seconds=1)
            return value

        sent = subprocess.CompletedProcess(
            ["fleet-send.sh"], 0, json.dumps({"run_id": exact_run}) + "\n", ""
        )
        with (
            mock.patch.object(assured, "datetime", BarrierClock),
            mock.patch.object(self.runner, "_record"),
            mock.patch.object(
                self.runner, "_phase_state", return_value={"active_phase": "BUILD"}
            ),
            mock.patch.object(self.runner, "_reconcile_run", return_value=None),
            mock.patch.object(assured.fleet_control, "require_usage_launch"),
            mock.patch.object(
                assured.fleet_admission,
                "authorize_launch",
                side_effect=authorize_then_expire,
            ),
            mock.patch.object(
                assured,
                "run_process",
                side_effect=[
                    assured.AssuredRunnerError(
                        "simulated crash before wrapper acceptance"
                    ),
                    sent,
                ],
            ) as wrapper,
        ):
            with self.assertRaisesRegex(
                assured.AssuredRunnerError, "before wrapper acceptance"
            ):
                self.runner._dispatch(action, action_key)
            authorized = assured.fleet_mission.load_state(runs, mission_id)
            pending = authorized["admissions"][
                authorized["run_owners"][exact_run]["owner_id"]
            ]
            self.assertEqual(pending["phase"], "authorized")
            self.assertEqual(self.runner._dispatch(action, action_key), exact_run)

        self.assertEqual(wrapper.call_count, 2)
        current = assured.fleet_mission.load_state(runs, mission_id)
        admission = current["admissions"][current["run_owners"][exact_run]["owner_id"]]
        self.assertEqual(admission["phase"], "started")
        self.assertEqual(
            admission["launch_authorization"]["approval_event_sha256"],
            approval_sha256,
        )

    def test_renewal_creates_new_bound_identity_and_launch(self) -> None:
        runs, mission_id, approval = self.activate_assured_mission()
        prompt = self.tmp / "renewed-launch.txt"
        prompt.write_text("renewed launch", encoding="utf-8")
        prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
        action_key = "expiry:renewed"
        action = {
            "instance": "maker",
            "prompt_file": str(prompt),
            "prompt_sha256": prompt_sha256,
        }
        old_expires = datetime.fromisoformat(approval["payload"]["expires_at"])

        class RunnerClock:
            current = old_expires - timedelta(minutes=1)

            @classmethod
            def now(cls, tz=None):
                return (
                    cls.current if tz is not None else cls.current.replace(tzinfo=None)
                )

            @staticmethod
            def fromisoformat(value):
                return datetime.fromisoformat(value)

        def expire_at_barrier(*args, **kwargs):
            del args, kwargs
            RunnerClock.current = old_expires + timedelta(seconds=1)

        with (
            mock.patch.object(assured, "datetime", RunnerClock),
            mock.patch.object(self.runner, "_record"),
            mock.patch.object(
                self.runner, "_phase_state", return_value={"active_phase": "BUILD"}
            ),
            mock.patch.object(self.runner, "_reconcile_run", return_value=None),
            mock.patch.object(
                assured.fleet_control,
                "require_usage_launch",
                side_effect=expire_at_barrier,
            ),
            mock.patch.object(assured, "run_process") as old_wrapper,
            self.assertRaises(assured.AssuredApprovalRenewalRequired),
        ):
            self.runner._dispatch(action, action_key)
        old_wrapper.assert_not_called()
        expired = assured.fleet_mission.load_state(runs, mission_id)
        self.assertFalse(any(item["active"] for item in expired["admissions"].values()))
        old_admission = next(
            item
            for item in expired["admissions"].values()
            if item["lane_actor"] == "ASSURED"
        )
        self.assertEqual(old_admission["phase"], "aborted")

        renewed_at = old_expires + timedelta(seconds=1)
        renewed_expires = renewed_at + timedelta(hours=1)

        class MissionClock:
            @classmethod
            def now(cls, tz=None):
                return renewed_at if tz is not None else renewed_at.replace(tzinfo=None)

            @staticmethod
            def fromisoformat(value):
                return datetime.fromisoformat(value)

        prior = expired["approval"]
        renewal_key = "test:assured:renewal"
        with mock.patch.object(assured.mission_state, "datetime", MissionClock):
            renewed, _ = assured.mission_state.append_event(
                runs,
                mission_id,
                kind="assurance_approval_renewed",
                actor="HUMAN",
                idempotency_key=renewal_key,
                payload={
                    "approval_id": str(
                        uuid.uuid5(
                            uuid.UUID(mission_id),
                            f"approval-renewal:{prior['event_sha256']}:{renewal_key}",
                        )
                    ),
                    "prior_approval_event_sha256": prior["event_sha256"],
                    "request_event_sha256": prior["request_event_sha256"],
                    "workflow_digest": prior["workflow_digest"],
                    "scope": prior["scope"],
                    "risk": prior["risk"],
                    "expires_at": renewed_expires.isoformat(),
                    "expires_in_seconds": 3600,
                    "approved_by_sha256": "d" * 64,
                    "decision": "approved",
                },
            )

        renewed_sha256 = renewed["event_sha256"]
        renewed_request_key = (
            "assured-"
            + assured.mission_state.sha256(
                {
                    "action": action_key,
                    "approval_event_sha256": renewed_sha256,
                }
            )[:40]
        )
        exact_run = assured.fleet_admission.deterministic_ids(
            mission_id,
            request_key=renewed_request_key,
            run_kind="specialist",
        )["run_id"]
        RunnerClock.current = renewed_at + timedelta(minutes=1)
        sent = subprocess.CompletedProcess(
            ["fleet-send.sh"], 0, json.dumps({"run_id": exact_run}) + "\n", ""
        )
        with (
            mock.patch.object(assured, "datetime", RunnerClock),
            mock.patch.object(assured.mission_state, "datetime", MissionClock),
            mock.patch.object(self.runner, "_record"),
            mock.patch.object(
                self.runner, "_phase_state", return_value={"active_phase": "BUILD"}
            ),
            mock.patch.object(self.runner, "_reconcile_run", return_value=None),
            mock.patch.object(assured.fleet_control, "require_usage_launch"),
            mock.patch.object(assured, "run_process", return_value=sent) as wrapper,
        ):
            self.assertEqual(self.runner._dispatch(action, action_key), exact_run)

        wrapper.assert_called_once()
        current = assured.fleet_mission.load_state(runs, mission_id)
        new_admission = current["admissions"][
            current["run_owners"][exact_run]["owner_id"]
        ]
        self.assertEqual(new_admission["request_key"], renewed_request_key)
        self.assertNotEqual(
            new_admission["admission_id"], old_admission["admission_id"]
        )
        self.assertEqual(new_admission["phase"], "started")
        self.assertEqual(
            new_admission["launch_authorization"]["approval_event_sha256"],
            renewed_sha256,
        )

    def test_fdp2_executes_dispatch_wait_publish_then_accepts(self) -> None:
        dispatch = event(
            1,
            {
                "action": "dispatch",
                "instance": "maker",
                "prompt_file": "p",
                "prompt_sha256": "a" * 64,
                "timeout_seconds": 30,
            },
        )
        publish = event(
            2,
            {
                "action": "publish",
                "kind": "proposal",
                "recipient": "checker",
                "source_instance": "maker",
                "source_run_id": "run-one",
                "reply_to": None,
                "payload_sha256": "b" * 64,
            },
        )
        terminal = event(
            3,
            {"action": "terminal", "status": "accepted", "reason": "checker_accept"},
            status="accepted",
        )
        with (
            mock.patch.object(
                self.runner, "_dispatch", return_value="run-one"
            ) as dispatch_run,
            mock.patch.object(self.runner, "_wait") as wait,
            mock.patch.object(
                self.runner, "_publish", return_value="message-one"
            ) as publish_run,
            mock.patch.object(
                assured.fdp2, "step", side_effect=[publish, terminal]
            ) as step,
        ):
            result = self.runner._drive_fdp2(dispatch)
        self.assertEqual(result, terminal)
        dispatch_run.assert_called_once()
        wait.assert_called_once()
        publish_run.assert_called_once()
        self.assertEqual(step.call_count, 2)

    def exercise_post_finalize_step_retry(self, *, renew: bool) -> None:
        runs, mission_id, approval = self.activate_assured_mission()
        prompt = self.tmp / "post-finalize-step.txt"
        prompt.write_text("finish before controller step", encoding="utf-8")
        prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
        dispatch = event(
            21,
            {
                "action": "dispatch",
                "instance": "maker",
                "prompt_file": str(prompt),
                "prompt_sha256": prompt_sha256,
                "timeout_seconds": 30,
            },
        )
        dispatch["conversation_id"] = "00000000-0000-4000-8000-000000000021"
        terminal = event(
            22,
            {"action": "terminal", "status": "accepted", "reason": "accepted"},
            status="accepted",
        )
        terminal["conversation_id"] = dispatch["conversation_id"]

        def accept_exact(command, *, runs_dir, timeout=None):
            del timeout
            run_id = command[command.index("--run-id") + 1]
            assured.fleet_ledger.append_event(
                runs_dir / "fleet-assured.ledger.jsonl",
                {
                    "timestamp": "2026-07-17T00:00:00Z",
                    "run_id": run_id,
                    "feature": "assured",
                    "instance": "maker",
                    "status": "dispatched",
                    "task_sha256": prompt_sha256,
                },
            )
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"run_id": run_id}) + "\n", ""
            )

        def finish(action, run_id, action_key, *, finalize=True):
            del action, action_key
            if finalize:
                self.finalize_assured_run(run_id, prompt_sha256)

        with (
            mock.patch.object(
                self.runner, "_phase_state", return_value={"active_phase": "BUILD"}
            ),
            mock.patch.object(assured.fleet_control, "require_usage_launch"),
            mock.patch.object(
                assured, "run_process", side_effect=accept_exact
            ) as wrapper,
            mock.patch.object(self.runner, "_wait", side_effect=finish),
            mock.patch.object(
                assured.fdp2,
                "step",
                side_effect=[
                    assured.fdp2.ControllerError(
                        "simulated crash after finalize before controller step"
                    ),
                    terminal,
                ],
            ) as step,
        ):
            with self.assertRaisesRegex(assured.fdp2.ControllerError, "after finalize"):
                self.runner._drive_fdp2(dispatch)
            finalized = assured.fleet_mission.load_state(runs, mission_id)
            assured_admission = next(
                item
                for item in finalized["admissions"].values()
                if item["lane_actor"] == "ASSURED"
            )
            self.assertEqual(assured_admission["phase"], "finalized")
            if renew:
                renewed, _ = self.renew_assured_approval(
                    approval, idempotency_key="test:post-finalize:renew"
                )
                self.assertNotEqual(renewed["event_sha256"], approval["event_sha256"])
            result = self.runner._drive_fdp2(dispatch)

        self.assertEqual(result, terminal)
        wrapper.assert_called_once()
        self.assertEqual(step.call_count, 2)
        current = assured.fleet_mission.load_state(runs, mission_id)
        self.assertFalse(any(item["active"] for item in current["admissions"].values()))
        self.assertEqual(
            len(
                [
                    item
                    for item in current["admissions"].values()
                    if item["lane_actor"] == "ASSURED"
                ]
            ),
            1,
        )

    def test_post_finalize_step_retry_replays_completed_dispatch(self) -> None:
        self.exercise_post_finalize_step_retry(renew=False)

    def test_post_finalize_step_retry_survives_approval_renewal(self) -> None:
        self.exercise_post_finalize_step_retry(renew=True)

    def exercise_drive_fdp3_authorized_resume(self, phase: str) -> None:
        runs, mission_id, approval = self.activate_assured_mission()
        accepted = event(
            30,
            {"action": "terminal", "status": "accepted", "reason": "accepted"},
            status="accepted",
        )
        accepted["conversation_id"] = "00000000-0000-4000-8000-000000000030"
        prompt = self.tmp / f"fdp3-{phase.lower()}.txt"
        prompt.write_text("resume exact authorized verification", encoding="utf-8")
        prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
        dispatch = event(
            31,
            {
                "action": "dispatch",
                "instance": "verify",
                "prompt_file": str(prompt),
                "prompt_sha256": prompt_sha256,
                "timeout_seconds": 30,
                "phase": "VERIFY",
            },
        )
        dispatch["assurance_id"] = "00000000-0000-4000-8000-000000000031"
        terminal = event(
            32,
            {"action": "terminal", "status": "verified", "reason": "verified"},
            status="verified",
        )
        terminal["assurance_id"] = dispatch["assurance_id"]
        action_key = self.runner._action_key("fdp3", dispatch)
        admission = self.authorize_assured_action(
            instance="verify",
            action_key=action_key,
            capability="assured_verify",
            prompt_sha256=prompt_sha256,
            approval_event_sha256=approval["event_sha256"],
        )
        (runs / "fleet-assured.dialogue-control.jsonl").touch()
        (runs / "fleet-assured.assurance-control.jsonl").touch()
        spec = self.tmp / f"spec-{phase.lower()}.json"
        spec.write_text("{}\n", encoding="utf-8")
        phase_state = self.assured_phase_state(
            phase,
            approval=approval,
            accepted_event_sha256=accepted["event_sha256"],
        )
        expires = datetime.fromisoformat(approval["payload"]["expires_at"])

        class ExpiredClock:
            @classmethod
            def now(cls, tz=None):
                value = expires + timedelta(seconds=1)
                return value if tz is not None else value.replace(tzinfo=None)

            @staticmethod
            def fromisoformat(value):
                return datetime.fromisoformat(value)

        sent = subprocess.CompletedProcess(
            ["fleet-send.sh"],
            0,
            json.dumps({"run_id": admission["run_id"]}) + "\n",
            "",
        )

        def finish(action, run_id, action_key, *, finalize=True):
            del action, action_key
            if finalize:
                self.finalize_assured_run(run_id, prompt_sha256)

        with (
            mock.patch.object(assured, "datetime", ExpiredClock),
            mock.patch.object(self.runner, "_phase_state", return_value=phase_state),
            mock.patch.object(assured.fdp2, "show", return_value=accepted),
            mock.patch.object(assured.fdp3, "show", return_value=dispatch),
            mock.patch.object(assured.fdp3, "step", return_value=terminal) as step,
            mock.patch.object(self.runner, "_wait", side_effect=finish),
            mock.patch.object(assured.fleet_control, "require_usage_launch") as usage,
            mock.patch.object(assured, "run_process", return_value=sent) as wrapper,
        ):
            result = self.runner.drive(spec, synthesize=False)

        self.assertEqual(result["status"], "verified")
        wrapper.assert_called_once()
        usage.assert_not_called()
        self.assertEqual(step.call_args.kwargs["run_id"], admission["run_id"])
        current = assured.fleet_mission.load_state(runs, mission_id)
        self.assertEqual(
            current["admissions"][admission["admission_id"]]["phase"], "finalized"
        )
        self.assertFalse(any(item["active"] for item in current["admissions"].values()))

    def test_drive_replays_expired_authorized_fdp3_from_challenge(self) -> None:
        self.exercise_drive_fdp3_authorized_resume("CHALLENGE")

    def test_drive_replays_expired_authorized_fdp3_from_verify(self) -> None:
        self.exercise_drive_fdp3_authorized_resume("VERIFY")

    def test_drive_replays_expired_authorized_fdp2_then_blocks_new_phase(self) -> None:
        runs, mission_id, approval = self.activate_assured_mission()
        prompt = self.tmp / "fdp2-expired-authorized.txt"
        prompt.write_text("resume exact authorized build", encoding="utf-8")
        prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
        dispatch = event(
            41,
            {
                "action": "dispatch",
                "instance": "maker",
                "prompt_file": str(prompt),
                "prompt_sha256": prompt_sha256,
                "timeout_seconds": 30,
            },
        )
        dispatch["conversation_id"] = "00000000-0000-4000-8000-000000000041"
        accepted = event(
            42,
            {"action": "terminal", "status": "accepted", "reason": "accepted"},
            status="accepted",
        )
        accepted["conversation_id"] = dispatch["conversation_id"]
        action_key = self.runner._action_key("fdp2", dispatch)
        admission = self.authorize_assured_action(
            instance="maker",
            action_key=action_key,
            capability="assured_build",
            prompt_sha256=prompt_sha256,
            approval_event_sha256=approval["event_sha256"],
        )
        (runs / "fleet-assured.dialogue-control.jsonl").touch()
        spec = self.tmp / "fdp2-expired-spec.json"
        spec.write_text("{}\n", encoding="utf-8")
        phase_state = self.assured_phase_state(
            "BUILD",
            approval=approval,
            accepted_event_sha256=accepted["event_sha256"],
        )
        expires = datetime.fromisoformat(approval["payload"]["expires_at"])

        class ExpiredClock:
            @classmethod
            def now(cls, tz=None):
                value = expires + timedelta(seconds=1)
                return value if tz is not None else value.replace(tzinfo=None)

            @staticmethod
            def fromisoformat(value):
                return datetime.fromisoformat(value)

        sent = subprocess.CompletedProcess(
            ["fleet-send.sh"],
            0,
            json.dumps({"run_id": admission["run_id"]}) + "\n",
            "",
        )

        def finish(action, run_id, action_key, *, finalize=True):
            del action, action_key
            if finalize:
                self.finalize_assured_run(run_id, prompt_sha256)

        with (
            mock.patch.object(assured, "datetime", ExpiredClock),
            mock.patch.object(self.runner, "_phase_state", return_value=phase_state),
            mock.patch.object(assured.fdp2, "show", return_value=dispatch),
            mock.patch.object(assured.fdp2, "step", return_value=accepted),
            mock.patch.object(self.runner, "_wait", side_effect=finish),
            mock.patch.object(assured, "run_process", return_value=sent) as wrapper,
            self.assertRaises(assured.AssuredApprovalRenewalRequired),
        ):
            self.runner.drive(spec, synthesize=False)

        wrapper.assert_called_once()
        current = assured.fleet_mission.load_state(runs, mission_id)
        self.assertEqual(
            current["admissions"][admission["admission_id"]]["phase"], "finalized"
        )
        self.assertFalse(any(item["active"] for item in current["admissions"].values()))

    def test_drive_replays_expired_authorized_synthesis(self) -> None:
        runs, mission_id, approval = self.activate_assured_mission()
        accepted = event(
            50,
            {"action": "terminal", "status": "accepted", "reason": "accepted"},
            status="accepted",
        )
        accepted["conversation_id"] = "00000000-0000-4000-8000-000000000050"
        verified = event(
            51,
            {"action": "terminal", "status": "verified", "reason": "verified"},
            status="verified",
        )
        verified["assurance_id"] = "00000000-0000-4000-8000-000000000051"
        synthesis_prompt = (
            f"MISSION_ID={mission_id}\n"
            "Synthesize the completed assured mission from durable FDP-2/FDP-3 evidence. "
            "Do not dispatch more work or modify the repository. Report STATUS, DECISION, "
            "ARTIFACTS, VERIFICATION, RISKS, and NEXT_ACTION.\n"
            f"FDP2_CONTROL_HEAD={accepted['event_sha256']}\n"
            f"FDP2_ACCEPTED_HEAD={accepted['snapshot']['accepted_head_sha']}\n"
            f"FDP3_CONTROL_HEAD={verified['event_sha256']}\n"
            f"FDP3_STATUS={verified['snapshot']['status']}\n"
        )
        prompt_sha256 = hashlib.sha256(synthesis_prompt.encode()).hexdigest()
        admission = self.authorize_assured_action(
            instance="lead",
            action_key="runner:assured-synthesis",
            capability="synthesis",
            prompt_sha256=prompt_sha256,
            approval_event_sha256=approval["event_sha256"],
        )
        (runs / "fleet-assured.dialogue-control.jsonl").touch()
        (runs / "fleet-assured.assurance-control.jsonl").touch()
        spec = self.tmp / "synthesis-expired-spec.json"
        spec.write_text("{}\n", encoding="utf-8")
        phase_state = self.assured_phase_state(
            "VERIFY",
            approval=approval,
            accepted_event_sha256=accepted["event_sha256"],
        )
        result_file = runs / "results" / "assured" / f"{admission['run_id']}.txt"
        result_file.parent.mkdir(parents=True)
        result_file.write_text("verified synthesis", encoding="utf-8")
        result_file.chmod(0o600)
        lifecycle = {
            "timestamp": "2026-07-17T00:01:00Z",
            "run_id": admission["run_id"],
            "feature": "assured",
            "instance": "lead",
            "status": "succeeded",
            "task_sha256": prompt_sha256,
            "result_file": str(result_file),
            "provider": "anthropic",
            "model": "claude-fable-5",
            "variant": None,
        }
        expires = datetime.fromisoformat(approval["payload"]["expires_at"])

        class ExpiredClock:
            @classmethod
            def now(cls, tz=None):
                value = expires + timedelta(seconds=1)
                return value if tz is not None else value.replace(tzinfo=None)

            @staticmethod
            def fromisoformat(value):
                return datetime.fromisoformat(value)

        sent = subprocess.CompletedProcess(
            ["fleet-send.sh"],
            0,
            json.dumps({"run_id": admission["run_id"]}) + "\n",
            "",
        )
        with (
            mock.patch.object(assured, "datetime", ExpiredClock),
            mock.patch.object(self.runner, "_phase_state", return_value=phase_state),
            mock.patch.object(assured.fdp2, "show", return_value=accepted),
            mock.patch.object(assured.fdp3, "show", return_value=verified),
            mock.patch.object(
                self.runner, "_legacy_events", side_effect=[[], [lifecycle]]
            ),
            mock.patch.object(self.runner, "_wait"),
            mock.patch.object(assured, "run_process", return_value=sent) as wrapper,
        ):
            result = self.runner.drive(spec)

        wrapper.assert_called_once()
        self.assertEqual(result["synthesis"]["run_id"], admission["run_id"])
        current = assured.fleet_mission.load_state(runs, mission_id)
        self.assertEqual(
            current["admissions"][admission["admission_id"]]["phase"], "started"
        )

    def test_phase_resume_keeps_historical_approval_after_renewal(self) -> None:
        runs, _, approval = self.activate_assured_mission()
        renewed, _ = self.renew_assured_approval(
            approval, idempotency_key="test:phase-history:renew"
        )
        self.assertNotEqual(renewed["event_sha256"], approval["event_sha256"])
        accepted = event(
            60,
            {"action": "terminal", "status": "accepted", "reason": "accepted"},
            status="accepted",
        )
        accepted["conversation_id"] = "00000000-0000-4000-8000-000000000060"
        verified = event(
            61,
            {"action": "terminal", "status": "verified", "reason": "verified"},
            status="verified",
        )
        verified["assurance_id"] = "00000000-0000-4000-8000-000000000061"
        (runs / "fleet-assured.dialogue-control.jsonl").touch()
        (runs / "fleet-assured.assurance-control.jsonl").touch()
        spec = self.tmp / "phase-history-spec.json"
        spec.write_text("{}\n", encoding="utf-8")
        phase = {"value": "CHALLENGE"}

        def state():
            return self.assured_phase_state(
                phase["value"],
                approval=approval,
                accepted_event_sha256=accepted["event_sha256"],
            )

        with (
            mock.patch.object(self.runner, "_phase_state", side_effect=state),
            mock.patch.object(assured.fdp2, "show", return_value=accepted),
            mock.patch.object(assured.fdp3, "show", return_value=verified),
            mock.patch.object(assured, "run_process") as wrapper,
        ):
            first = self.runner.drive(spec, synthesize=False)
            phase["value"] = "VERIFY"
            second = self.runner.drive(spec, synthesize=False)

        self.assertEqual(first["status"], "verified")
        self.assertEqual(second["status"], "verified")
        wrapper.assert_not_called()

    def test_controller_internal_event_is_normalized_to_public_action(self) -> None:
        internal = {
            "sequence": 9,
            "event_sha256": "9" * 64,
            "snapshot": {"status": "accepted", "accepted_head_sha": "a" * 40},
        }
        public = {
            **internal,
            "next_action": {
                "action": "terminal",
                "status": "accepted",
                "reason": "checker_accept",
            },
        }
        with mock.patch.object(
            assured.fdp2, "public_event", return_value=public
        ) as normalize:
            result = self.runner._drive_fdp2(internal)
        self.assertEqual(result, public)
        normalize.assert_called_once_with(internal)

    def test_fdp3_reconciles_phase_advance_and_verifies(self) -> None:
        advance = event(4, {"action": "advance_phase", "phase": "VERIFY"})
        terminal = event(
            5,
            {"action": "terminal", "status": "verified", "reason": "claude_verified"},
            status="verified",
        )
        with (
            mock.patch.object(self.runner, "_phase", return_value="VERIFY"),
            mock.patch.object(self.runner, "_advance") as phase,
            mock.patch.object(assured.fdp3, "step", return_value=terminal) as step,
        ):
            result = self.runner._drive_fdp3(advance)
        self.assertEqual(result, terminal)
        phase.assert_called_once_with("VERIFY", advance["event_sha256"])
        self.assertTrue(step.call_args.kwargs["phase_advanced"])

    def test_build_exit_passes_exact_mission_approval_event(self) -> None:
        state = {
            "active_phase": "BUILD",
            "history": [{"phase": "BUILD", "evidence": "ready"}],
        }
        completed = subprocess.CompletedProcess(["fleet_state.py"], 0, "advanced\n", "")
        approval_sha = "d" * 64
        with (
            mock.patch.object(
                assured.fleet_state,
                "load_live",
                return_value=(self.runner._phase_manifest, state),
            ),
            mock.patch.object(assured, "run_process", return_value=completed) as run,
        ):
            self.runner._advance(
                "CHALLENGE",
                "accepted-head",
                approval_event_sha256=approval_sha,
            )
        command = run.call_args.args[0]
        self.assertIn("--approval-event-sha256", command)
        self.assertEqual(
            command[command.index("--approval-event-sha256") + 1], approval_sha
        )
        self.assertNotIn("--approved-by", command)

    def test_malformed_or_timed_out_controller_terminal_fails_closed(self) -> None:
        for status, reason in (
            ("indeterminate", "invalid_proposal_contract"),
            ("indeterminate", "run_timeout_exceeded:run-one"),
            ("rejected", "checker_reject"),
        ):
            terminal = event(
                8,
                {"action": "terminal", "status": status, "reason": reason},
                status=status,
            )
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(assured.AssuredRunnerError, status),
            ):
                self.runner._drive_fdp2(terminal)

    def test_wait_requires_exact_run_evidence(self) -> None:
        action = {
            "instance": "maker",
            "timeout_seconds": 30,
            "prompt_sha256": "a" * 64,
        }
        response = subprocess.CompletedProcess(
            ["fleet-wait.sh"],
            0,
            json.dumps({"run_id": "different", "status": "succeeded"}),
            "",
        )
        with (
            mock.patch.object(self.runner, "_record"),
            mock.patch.object(assured, "run_process", return_value=response),
            self.assertRaisesRegex(assured.AssuredRunnerError, "exact run"),
        ):
            self.runner._wait(action, "expected", "action:wait")

    def test_advance_is_noop_after_kill_between_advance_and_ack(self) -> None:
        state = {
            "active_phase": "VERIFY",
            "history": [{"phase": "VERIFY", "evidence": "evidence"}],
        }
        with (
            mock.patch.object(
                assured.fleet_state,
                "load_live",
                return_value=(self.runner._phase_manifest, state),
            ),
            mock.patch.object(assured, "run_process") as run,
        ):
            self.runner._advance("VERIFY", "evidence")
        run.assert_not_called()

        drifted = {
            "active_phase": "VERIFY",
            "history": [{"phase": "VERIFY", "evidence": "other"}],
        }
        with (
            mock.patch.object(
                assured.fleet_state,
                "load_live",
                return_value=(self.runner._phase_manifest, drifted),
            ),
            self.assertRaisesRegex(assured.AssuredRunnerError, "binding differs"),
        ):
            self.runner._advance("VERIFY", "evidence")

    def test_additional_advisory_is_phase_scoped_and_never_writer(self) -> None:
        with (
            mock.patch.object(self.runner, "_phase", return_value="BUILD"),
            self.assertRaisesRegex(assured.AssuredRunnerError, "writer"),
        ):
            self.runner.advisory(
                instance="maker", objective="review", idempotency_key="extra:writer"
            )

        run_id = "advisory-run"
        result = self.tmp / "results" / "assured" / f"{run_id}.txt"
        result.parent.mkdir(parents=True)
        result.write_text("read-only evidence", encoding="utf-8")
        lifecycle = [
            {
                "instance": "verify",
                "run_id": run_id,
                "status": "succeeded",
                "result_file": str(result),
                "phase": "VERIFY",
                "role": "verifier",
                "task_sha256": hashlib.sha256(
                    (
                        f"MISSION_ID={self.runner.mission_id}\nADVISORY_ONLY=true\n"
                        "ACTIVE_PHASE=VERIFY\n\nreview exact evidence\n\n"
                        "Return read-only analysis with exact evidence. Do not modify the repository."
                    ).encode("utf-8")
                ).hexdigest(),
                "provider": "anthropic",
                "model": "claude-fable-5",
                "variant": None,
            }
        ]
        with (
            mock.patch.object(self.runner, "_phase", return_value="VERIFY"),
            mock.patch.object(self.runner, "_dispatch", return_value=run_id),
            mock.patch.object(self.runner, "_wait"),
            mock.patch.object(self.runner, "_legacy_events", return_value=lifecycle),
        ):
            value = self.runner.advisory(
                instance="verify",
                objective="review exact evidence",
                idempotency_key="extra:verify",
            )
        self.assertEqual(value["status"], "succeeded")
        self.assertEqual(value["result_file"], str(result))

        lifecycle[0]["model"] = "unbound-model"
        with (
            mock.patch.object(self.runner, "_phase", return_value="VERIFY"),
            mock.patch.object(self.runner, "_dispatch", return_value=run_id),
            mock.patch.object(self.runner, "_wait"),
            mock.patch.object(self.runner, "_legacy_events", return_value=lifecycle),
            self.assertRaisesRegex(assured.AssuredRunnerError, "provenance"),
        ):
            self.runner.advisory(
                instance="verify",
                objective="review exact evidence",
                idempotency_key="extra:verify-drift",
            )

        lifecycle[0]["model"] = "claude-fable-5"
        target = self.tmp / "outside-advisory.txt"
        target.write_text("untrusted target", encoding="utf-8")
        result.unlink()
        result.symlink_to(target)
        with (
            mock.patch.object(self.runner, "_phase", return_value="VERIFY"),
            mock.patch.object(self.runner, "_dispatch", return_value=run_id),
            mock.patch.object(self.runner, "_wait"),
            mock.patch.object(self.runner, "_legacy_events", return_value=lifecycle),
            self.assertRaisesRegex(assured.AssuredRunnerError, "regular file"),
        ):
            self.runner.advisory(
                instance="verify",
                objective="review exact evidence",
                idempotency_key="extra:verify-symlink",
            )


if __name__ == "__main__":
    unittest.main()
