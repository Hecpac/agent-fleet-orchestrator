from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from tests.mission_control_test_support import create_running_mission

import fleet_agent_mcp
import fleet_control
import fleet_control_service
import fleet_decisions
import fleet_ledger
import fleet_mcp
import fleet_mission_state as mission_state
import fleet_report


ROOT = Path(__file__).resolve().parents[1]


class FleetDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs, self.mission_id, _ = create_running_mission(
            self.tmp, feature="decision-control"
        )
        self.control = fleet_control.FleetControl(self.runs, self.mission_id)
        self.run_by_instance: dict[str, str] = {}
        self.prompt_by_instance: dict[str, str] = {}
        self.effect_calls: list[list[str]] = []

    def fake_run(self, command: list[str], *, runs_dir: Path, timeout=None):
        del timeout
        self.assertEqual(runs_dir.resolve(), self.runs.resolve())
        self.effect_calls.append(command)
        name = Path(command[0]).name
        if name == "fleet-send.sh":
            feature, instance, prompt = command[1:4]
            run_id = command[command.index("--run-id") + 1]
            self.run_by_instance[instance] = run_id
            self.prompt_by_instance[instance] = prompt
            self.assertTrue(
                fleet_ledger.append_event(
                    self.runs / f"fleet-{feature}.ledger.jsonl",
                    {
                        "timestamp": "2026-07-19T00:00:00Z",
                        "run_id": run_id,
                        "feature": feature,
                        "instance": instance,
                        "status": "dispatched",
                        "task_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    },
                )
            )
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
                command, 0, "".join(json.dumps(row) + "\n" for row in rows), ""
            )
        raise AssertionError(command)

    def mark_succeeded(self, instance: str, content: bytes) -> None:
        run_id = self.run_by_instance[instance]
        feature = self.control.state()["feature"]
        result = self.runs / "results" / feature / f"{run_id}.txt"
        result.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result.parent.chmod(0o700)
        result.write_bytes(content)
        result.chmod(0o600)
        member = self.control.members()[instance]
        self.assertTrue(
            fleet_ledger.append_event(
                self.runs / f"fleet-{feature}.ledger.jsonl",
                {
                    "timestamp": "2026-07-19T00:01:00Z",
                    "run_id": run_id,
                    "feature": feature,
                    "instance": instance,
                    "status": "succeeded",
                    "task_sha256": hashlib.sha256(
                        self.prompt_by_instance[instance].encode()
                    ).hexdigest(),
                    "result_file": str(result.resolve(strict=True)),
                    "provider": member["provider"],
                    "model": member["model"],
                    "variant": member.get("variant"),
                },
            )
        )

    def seed_evidence(self) -> tuple[str, str]:
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            dispatched = self.control.dispatch_many(
                [
                    {
                        "recipient_instance": "scout",
                        "capability": "recon",
                        "objective": "recommend one bounded implementation choice",
                        "idempotency_key": "decision:evidence:recommendation",
                    },
                    {
                        "recipient_instance": "challenger",
                        "capability": "challenge",
                        "objective": "challenge the recommendation with concrete evidence",
                        "idempotency_key": "decision:evidence:challenge",
                    },
                ]
            )
            self.mark_succeeded("scout", b"recommend option-a\n")
            self.mark_succeeded("challenger", b"challenge: option-b is safer\n")
            waited = self.control.wait(
                [item["run_id"] for item in dispatched["runs"]], timeout_seconds=30
            )
        by_instance = {item["instance"]: item["artifact_id"] for item in waited["results"]}
        return by_instance["scout"], by_instance["challenger"]

    @staticmethod
    def brief(recommendation: str, challenge: str) -> dict:
        return {
            "title": "Elegir estrategia de integración",
            "question": "¿Aplicamos el cambio aditivo o reemplazamos el camino actual?",
            "affected_instances": ["builder"],
            "impact": "blocking",
            "risk": "low",
            "reversible": True,
            "options": [
                {
                    "option_id": "additive",
                    "label": "Integración aditiva",
                    "tradeoffs": "Más compatibilidad; conserva temporalmente el camino viejo.",
                },
                {
                    "option_id": "replace",
                    "label": "Reemplazo directo",
                    "tradeoffs": "Menos código; mayor riesgo de regresión.",
                },
            ],
            "recommendation": {
                "option_id": "additive",
                "rationale": "Preserva control-v1 y permite rollback.",
                "artifact_id": recommendation,
            },
            "challenge": {
                "summary": "El challenger exige verificar que el camino viejo no publique doble.",
                "artifact_id": challenge,
            },
            "dissent": "El challenger prefiere reemplazo solo después del smoke.",
            "default_option_id": "additive",
        }

    def test_decision_brief_blocks_only_affected_then_human_cli_unblocks(self) -> None:
        recommendation, challenge = self.seed_evidence()
        requested = self.control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:integration-strategy",
        )
        decision = requested["decision"]
        self.assertTrue(requested["appended"])
        self.assertIn("DECISION", requested["brief"])
        self.assertEqual(decision["status"], "pending")
        self.assertEqual(
            datetime.fromisoformat(decision["deadline_at"].replace("Z", "+00:00"))
            - datetime.fromisoformat(decision["requested_at"].replace("Z", "+00:00")),
            timedelta(seconds=mission_state.DECISION_TIMEOUT_SECONDS),
        )
        repeated = self.control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:integration-strategy",
        )
        self.assertFalse(repeated["appended"])
        before_effects = len(self.effect_calls)
        with (
            mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run),
            self.assertRaisesRegex(
                mission_state.MissionConflict,
                "blocked by pending human decisions",
            ),
        ):
            self.control.dispatch(
                recipient_instance="builder",
                capability="build",
                objective="must wait for the human decision",
                idempotency_key="decision:blocked:builder",
            )
        self.assertEqual(len(self.effect_calls), before_effects)
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            unaffected = self.control.dispatch(
                recipient_instance="scout",
                capability="recon",
                objective="continue unrelated read-only reconnaissance",
                idempotency_key="decision:unaffected:scout",
            )
        self.assertEqual(unaffected["recipient_instance"], "scout")
        with self.assertRaisesRegex(
            mission_state.MissionConflict, "blocked by pending human decisions"
        ):
            self.control.complete(
                artifact_id=recommendation,
                summary="cannot complete around a pending decision",
                idempotency_key="decision:premature:complete",
            )

        cli = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "fleet-decision.py"),
                "--runs-dir",
                str(self.runs),
                "--mission-id",
                self.mission_id,
                "resolve",
                "--decision-id",
                decision["decision_id"],
                "--option-id",
                "additive",
                "--reason",
                "aprobado por el operador",
                "--idempotency-key",
                "human:decision:integration-strategy",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        resolved = self.control.state()["decisions"][decision["decision_id"]]
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["resolution"]["actor"], "HUMAN")
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            dispatched = self.control.dispatch(
                recipient_instance="builder",
                capability="build",
                objective="continue after exact human resolution",
                idempotency_key="decision:unblocked:builder",
            )
        self.assertEqual(dispatched["recipient_instance"], "builder")

    def test_durable_notification_is_secret_free_targeted_and_never_authoritative(
        self,
    ) -> None:
        recommendation, challenge = self.seed_evidence()
        brief = self.brief(recommendation, challenge)
        brief["title"] = "private title must not enter notification"
        brief["question"] = "private question must not enter notification"
        brief["dissent"] = "private dissent must not enter notification"
        notifying = fleet_control.FleetControl(
            self.runs,
            self.mission_id,
            decision_notifier=fleet_control.cmux_decision_notifier,
        )
        accepted = subprocess.CompletedProcess(["cmux", "notify"], 0, "", "")
        with mock.patch.object(
            fleet_control.subprocess, "run", return_value=accepted
        ) as notify:
            requested = notifying.request_decision(
                brief=brief,
                idempotency_key="decision:notify-once",
            )
            repeated = notifying.request_decision(
                brief=brief,
                idempotency_key="decision:notify-once",
            )

        self.assertTrue(requested["notification"]["attempted"])
        self.assertTrue(requested["notification"]["accepted"])
        self.assertEqual(requested["notification"]["outcome"], "accepted")
        self.assertEqual(
            requested["notification"]["guarantee"], "cmux_acceptance"
        )
        self.assertEqual(requested["notification"]["authority"], "wake_up_only")
        self.assertEqual(requested["decision"]["delivery"]["status"], "accepted")
        self.assertFalse(repeated["appended"])
        self.assertFalse(repeated["notification"]["attempted"])
        notify.assert_called_once()
        rendered_command = " ".join(notify.call_args.args[0])
        self.assertIn("--surface 00000000-0000-0000-0000-000000000001", rendered_command)
        self.assertIn(self.mission_id, rendered_command)
        self.assertIn(requested["decision"]["decision_id"], rendered_command)
        self.assertNotIn(brief["title"], rendered_command)
        self.assertNotIn(brief["question"], rendered_command)
        self.assertNotIn(brief["dissent"], rendered_command)

        before = len(notifying.events())
        with mock.patch.object(
            fleet_control.subprocess,
            "run",
            side_effect=OSError("cmux unavailable"),
        ):
            failed_notice = notifying.request_decision(
                brief=brief,
                idempotency_key="decision:notify-failure",
            )
        self.assertTrue(failed_notice["appended"])
        self.assertFalse(failed_notice["notification"]["attempted"])
        self.assertFalse(failed_notice["notification"]["accepted"])
        self.assertEqual(failed_notice["notification"]["outcome"], "rejected")
        self.assertEqual(
            failed_notice["notification"]["reason"], "command_unavailable"
        )
        self.assertEqual(
            failed_notice["decision"]["delivery"]["status"], "rejected"
        )
        self.assertEqual(len(notifying.events()), before + 4)

    def test_decision_request_and_outbox_enqueue_publish_atomically(self) -> None:
        recommendation, challenge = self.seed_evidence()
        before = self.control.events()

        def fail_before_publish(point: str) -> None:
            if point == "before_publish":
                raise RuntimeError("simulated crash before atomic publication")

        with (
            mock.patch.object(
                mission_state,
                "_mission_transaction_checkpoint",
                side_effect=fail_before_publish,
            ),
            self.assertRaisesRegex(RuntimeError, "simulated crash"),
        ):
            self.control.request_decision(
                brief=self.brief(recommendation, challenge),
                idempotency_key="decision:atomic-outbox",
            )
        self.assertEqual(self.control.events(), before)

        requested = self.control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:atomic-outbox",
        )
        self.assertEqual(
            [event["kind"] for event in self.control.events()[-2:]],
            ["human_decision_requested", "decision_notification_enqueued"],
        )
        self.assertEqual(requested["decision"]["delivery"]["status"], "pending")

    def test_rejection_retries_after_backoff_and_rebinds_current_mission_lead(
        self,
    ) -> None:
        recommendation, challenge = self.seed_evidence()
        targets: list[dict[str, object]] = []

        def notifier(current, decision, target):
            del current, decision
            targets.append(dict(target))
            if len(targets) == 1:
                return fleet_control._notification_result(
                    attempted=True,
                    outcome="rejected",
                    reason="cmux_rejected",
                    returncode=2,
                    delivery_status="rejected",
                )
            return fleet_control._notification_result(
                attempted=True,
                outcome="accepted",
                reason="cmux_accepted",
                returncode=0,
                delivery_status="accepted",
            )

        control = fleet_control.FleetControl(
            self.runs, self.mission_id, decision_notifier=notifier
        )
        requested = control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:retry-and-rebind",
        )
        decision_id = requested["decision"]["decision_id"]
        rejected = requested["decision"]["delivery"]
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["attempts"], 1)
        first_outcome_at = mission_state.parse_timestamp(
            rejected["last_outcome"]["outcome_at"], "first outcome"
        )
        retry_at = mission_state.parse_timestamp(
            rejected["next_attempt_at"], "retry at"
        )
        self.assertIn(int((retry_at - first_outcome_at).total_seconds()), {4, 5, 6})

        not_due = control.deliver_due_decision_notification(decision_id=decision_id)
        self.assertFalse(not_due["processed"])
        self.assertEqual(len(targets), 1)

        manifest_path = self.runs / "fleet-decision-control.manifest"
        manifest = manifest_path.read_text(encoding="utf-8")
        manifest_path.write_text(
            manifest.replace(
                "lead.uuid=00000000-0000-0000-0000-000000000001",
                "lead.uuid=00000000-0000-0000-0000-000000000009",
            ),
            encoding="utf-8",
        )
        claim_at = retry_at.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        outcome_at = (retry_at + timedelta(microseconds=1)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        clock = mock.Mock()
        clock.now.return_value = retry_at
        with (
            mock.patch.object(fleet_control, "datetime", clock),
            mock.patch.object(
                mission_state,
                "_next_timestamp",
                side_effect=[claim_at, outcome_at],
            ),
        ):
            retried = control.deliver_due_decision_notification(
                decision_id=decision_id
            )
        self.assertTrue(retried["processed"])
        self.assertEqual(retried["notification"]["outcome"], "accepted")
        self.assertEqual(targets[0]["surface_uuid"], "00000000-0000-0000-0000-000000000001")
        self.assertEqual(targets[1]["surface_uuid"], "00000000-0000-0000-0000-000000000009")
        delivery = control.state()["decisions"][decision_id]["delivery"]
        self.assertEqual(delivery["status"], "accepted")
        self.assertEqual(delivery["attempts"], 2)

    def test_retry_cadence_is_deterministic_jittered_and_capped(self) -> None:
        decision_id = "c8c1da00-d3b8-5620-87ed-b3d4b850f4fa"
        bases = [5, 30, 120, 600, 600, 600]
        observed = [
            mission_state.decision_notification_retry_seconds(decision_id, attempt)
            for attempt in range(1, len(bases) + 1)
        ]
        self.assertEqual(
            observed,
            [
                mission_state.decision_notification_retry_seconds(
                    decision_id, attempt
                )
                for attempt in range(1, len(bases) + 1)
            ],
        )
        for seconds, base in zip(observed, bases, strict=True):
            span = max(1, base // 5)
            self.assertGreaterEqual(seconds, base - span)
            self.assertLessEqual(seconds, base + span)

    def test_ambiguous_or_legacy_delivery_never_retries(self) -> None:
        recommendation, challenge = self.seed_evidence()
        calls = 0

        def timeout_notifier(current, decision, target):
            nonlocal calls
            del target
            calls += 1
            self.assertEqual(decision["delivery"]["status"], "in_flight")
            self.assertEqual(current["decisions"][decision["decision_id"]]["delivery"]["status"], "in_flight")
            return fleet_control._notification_result(
                attempted=True,
                outcome="indeterminate",
                reason="timeout",
                returncode=None,
                delivery_status="indeterminate",
            )

        control = fleet_control.FleetControl(
            self.runs, self.mission_id, decision_notifier=timeout_notifier
        )
        requested = control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:ambiguous-no-retry",
        )
        decision_id = requested["decision"]["decision_id"]
        self.assertEqual(requested["decision"]["delivery"]["status"], "indeterminate")
        self.assertIsNone(requested["decision"]["delivery"]["next_attempt_at"])
        self.assertFalse(
            control.deliver_due_decision_notification(
                decision_id=decision_id
            )["processed"]
        )
        self.assertEqual(calls, 1)

        legacy = self.control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:legacy-ambiguous",
        )
        legacy_id = legacy["decision"]["decision_id"]
        events = self.control.events()
        self.assertEqual(events[-1]["kind"], "decision_notification_enqueued")
        ledger = mission_state.ledger_path(self.runs, self.mission_id)
        ledger.write_bytes(
            b"".join(
                mission_state.canonical_bytes(event) + b"\n"
                for event in events[:-1]
            )
        )
        legacy_calls = mock.Mock()
        legacy_control = fleet_control.FleetControl(
            self.runs, self.mission_id, decision_notifier=legacy_calls
        )
        legacy_delivery = legacy_control.state()["decisions"][legacy_id]["delivery"]
        self.assertEqual(legacy_delivery["status"], "legacy_indeterminate")
        self.assertFalse(
            legacy_control.deliver_due_decision_notification(
                decision_id=legacy_id
            )["processed"]
        )
        legacy_calls.assert_not_called()

    def test_resolved_decision_closes_rejected_delivery(self) -> None:
        recommendation, challenge = self.seed_evidence()
        calls = 0

        def reject(current, decision, target):
            nonlocal calls
            del current, decision, target
            calls += 1
            return fleet_control._notification_result(
                attempted=True,
                outcome="rejected",
                reason="cmux_rejected",
                returncode=2,
                delivery_status="rejected",
            )

        control = fleet_control.FleetControl(
            self.runs, self.mission_id, decision_notifier=reject
        )
        requested = control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:close-delivery",
        )
        decision_id = requested["decision"]["decision_id"]
        fleet_decisions.resolve_human(
            self.runs,
            self.mission_id,
            decision_id=decision_id,
            option_id="additive",
            reason="operator closed the decision",
            idempotency_key="human:decision:close-delivery",
        )
        delivery = control.state()["decisions"][decision_id]["delivery"]
        self.assertEqual(delivery["status"], "closed")
        self.assertIsNone(delivery["next_attempt_at"])
        self.assertFalse(
            control.deliver_due_decision_notification(
                decision_id=decision_id
            )["processed"]
        )
        self.assertEqual(calls, 1)

    def test_parallel_drainers_claim_only_one_physical_attempt(self) -> None:
        recommendation, challenge = self.seed_evidence()
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def blocked_notifier(current, decision, target):
            nonlocal calls
            del current, decision, target
            calls += 1
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return fleet_control._notification_result(
                attempted=True,
                outcome="accepted",
                reason="cmux_accepted",
                returncode=0,
                delivery_status="accepted",
            )

        control = fleet_control.FleetControl(
            self.runs, self.mission_id, decision_notifier=blocked_notifier
        )
        key = "decision:single-physical-attempt"
        decision_id = str(uuid.uuid5(uuid.UUID(self.mission_id), f"decision:{key}"))
        outcome: dict[str, object] = {}

        def request() -> None:
            outcome["value"] = control.request_decision(
                brief=self.brief(recommendation, challenge),
                idempotency_key=key,
            )

        thread = threading.Thread(target=request)
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            raced = control.deliver_due_decision_notification(
                decision_id=decision_id
            )
            self.assertFalse(raced["processed"])
            self.assertEqual(
                control.state()["decisions"][decision_id]["delivery"]["status"],
                "in_flight",
            )
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIn("value", outcome)
        self.assertEqual(calls, 1)
        self.assertEqual(
            control.state()["decisions"][decision_id]["delivery"]["status"],
            "accepted",
        )

    def test_two_decisions_never_overlap_physical_notification(self) -> None:
        recommendation, challenge = self.seed_evidence()
        first = self.control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:serialized:first",
        )["decision"]["decision_id"]
        second = self.control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:serialized:second",
        )["decision"]["decision_id"]
        entered = threading.Event()
        release = threading.Event()
        guard = threading.Lock()
        active = 0
        maximum_active = 0
        calls: list[str] = []

        def blocked_notifier(current, decision, target):
            nonlocal active, maximum_active
            del current, target
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
                calls.append(decision["decision_id"])
            try:
                if decision["decision_id"] == first:
                    entered.set()
                    self.assertTrue(release.wait(timeout=5))
                return fleet_control._notification_result(
                    attempted=True,
                    outcome="accepted",
                    reason="cmux_accepted",
                    returncode=0,
                    delivery_status="accepted",
                )
            finally:
                with guard:
                    active -= 1

        first_control = fleet_control.FleetControl(
            self.runs, self.mission_id, decision_notifier=blocked_notifier
        )
        second_control = fleet_control.FleetControl(
            self.runs, self.mission_id, decision_notifier=blocked_notifier
        )
        outcome: dict[str, object] = {}

        def deliver_first() -> None:
            outcome["first"] = first_control.deliver_due_decision_notification(
                decision_id=first
            )

        thread = threading.Thread(target=deliver_first)
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            raced = second_control.deliver_due_decision_notification(
                decision_id=second
            )
            self.assertFalse(raced["processed"])
            pending = second_control.state()["decisions"][second]["delivery"]
            self.assertEqual(pending["status"], "pending")
            self.assertEqual(pending["attempts"], 0)
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIn("first", outcome)

        delivered = second_control.deliver_due_decision_notification(
            decision_id=second
        )
        self.assertTrue(delivered["processed"])
        self.assertEqual(maximum_active, 1)
        self.assertEqual(calls, [first, second])

    def test_running_control_service_drains_enqueued_notification(self) -> None:
        recommendation, challenge = self.seed_evidence()
        fake_bin = self.tmp / "fake-bin"
        fake_bin.mkdir(mode=0o700)
        notify_log = self.tmp / "cmux-notify.log"
        cmux = fake_bin / "cmux"
        cmux.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FLEET_TEST_NOTIFY_LOG\"\n",
            encoding="utf-8",
        )
        cmux.chmod(0o700)
        configured_short_root = os.environ.get("FLEET_TEST_SHORT_TMPDIR")
        candidate_short_root = Path(
            configured_short_root or os.environ.get("TMPDIR", "/tmp")
        )
        socket_parent = (
            candidate_short_root
            if configured_short_root is not None
            or len(os.fsencode(candidate_short_root)) <= 32
            else Path("/tmp")
        )
        socket_temp = tempfile.TemporaryDirectory(prefix="f2b-", dir=socket_parent)
        self.addCleanup(socket_temp.cleanup)
        socket_root = Path(socket_temp.name)
        environment = {
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "FLEET_CONTROL_SOCKET_DIR": str(socket_root),
            "FLEET_TEST_NOTIFY_LOG": str(notify_log),
        }
        with mock.patch.dict(os.environ, environment):
            lifecycle = fleet_control_service.ControlLifecycle(
                self.runs, self.mission_id
            )
            lifecycle.start()
            try:
                requested = self.control.request_decision(
                    brief=self.brief(recommendation, challenge),
                    idempotency_key="decision:service-drain",
                )
                decision_id = requested["decision"]["decision_id"]
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    delivery = self.control.state()["decisions"][decision_id][
                        "delivery"
                    ]
                    if delivery["status"] == "accepted":
                        break
                    time.sleep(0.02)
                else:
                    self.fail("running Fleet Control did not drain the outbox")
            finally:
                lifecycle.stop()
        rendered = notify_log.read_text(encoding="utf-8")
        self.assertIn("--surface 00000000-0000-0000-0000-000000000001", rendered)
        self.assertIn(decision_id, rendered)

    def test_notification_worker_telemetry_never_controls_liveness(self) -> None:
        with mock.patch("builtins.print", side_effect=OSError("stderr unavailable")):
            fleet_mcp._notification_worker_log("diagnostic only")

    def test_report_counts_durable_decisions_and_human_wait(self) -> None:
        recommendation, challenge = self.seed_evidence()
        requested = self.control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:report-metrics",
        )

        pending_report = fleet_report.build_report(self.runs, self.mission_id)
        self.assertEqual(
            pending_report["decisions"],
            {
                "total": 1,
                "pending": 1,
                "resolved": 0,
                "human_resolved": 0,
                "automatic_resolved": 0,
            },
        )
        self.assertIn(
            "Decisions: total=1 pending=1",
            fleet_report.human_report(pending_report),
        )

        requested_at = datetime.fromisoformat(
            requested["decision"]["requested_at"].replace("Z", "+00:00")
        )
        resolved_at = (requested_at + timedelta(seconds=5)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with mock.patch.object(
            mission_state, "_next_timestamp", return_value=resolved_at
        ):
            fleet_decisions.resolve_human(
                self.runs,
                self.mission_id,
                decision_id=requested["decision"]["decision_id"],
                option_id="additive",
                reason="operator selected the recommended option",
                idempotency_key="human:decision:report-metrics",
            )
        resolved_report = fleet_report.build_report(self.runs, self.mission_id)
        self.assertEqual(resolved_report["decisions"]["pending"], 0)
        self.assertEqual(resolved_report["decisions"]["resolved"], 1)
        self.assertEqual(resolved_report["decisions"]["human_resolved"], 1)
        self.assertEqual(resolved_report["timing"]["human_wait_seconds"], 5.0)

    def test_global_inventory_redacts_brief_and_marks_default_without_mutation(
        self,
    ) -> None:
        recommendation, challenge = self.seed_evidence()
        brief = self.brief(recommendation, challenge)
        brief["question"] = "operator-only question"
        brief["dissent"] = "operator-only dissent"
        requested = self.control.request_decision(
            brief=brief,
            idempotency_key="decision:global-inventory",
        )
        deadline = datetime.fromisoformat(
            requested["decision"]["deadline_at"].replace("Z", "+00:00")
        )
        ledger = mission_state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()

        inventory = fleet_decisions.decision_inventory(
            self.runs,
            now=deadline + timedelta(seconds=1),
        )

        self.assertEqual(len(inventory["decisions"]), 1)
        row = inventory["decisions"][0]
        self.assertEqual(row["mission_id"], self.mission_id)
        self.assertEqual(row["decision_id"], requested["decision"]["decision_id"])
        self.assertEqual(row["status"], "DEFAULT_ELIGIBLE")
        self.assertTrue(row["default_eligible"])
        self.assertEqual(row["delivery_status"], "pending")
        self.assertEqual(row["delivery_attempts"], 0)
        self.assertEqual(ledger.read_bytes(), before)
        serialized = json.dumps(inventory, sort_keys=True)
        self.assertNotIn(brief["question"], serialized)
        self.assertNotIn(brief["dissent"], serialized)
        self.assertNotIn(brief["title"], serialized)

    def test_production_cli_and_mcp_enable_the_durable_notifier(self) -> None:
        cli_control = mock.Mock()
        cli_control.state.return_value = {"status": "running"}
        with (
            mock.patch.object(
                fleet_control, "FleetControl", return_value=cli_control
            ) as cli_constructor,
            mock.patch("builtins.print"),
        ):
            result = fleet_control.main(
                [
                    "--runs-dir",
                    str(self.runs),
                    "--mission-id",
                    self.mission_id,
                    "inspect-mission",
                ]
            )
        self.assertEqual(result, 0)
        cli_constructor.assert_called_once_with(
            self.runs,
            self.mission_id,
            preset=None,
            decision_notifier=fleet_control.cmux_decision_notifier,
        )

        mcp_control = mock.Mock()
        with (
            mock.patch.object(
                fleet_mcp, "FleetControl", return_value=mcp_control
            ) as mcp_constructor,
            mock.patch.object(fleet_mcp.sys, "stdin", []),
        ):
            result = fleet_mcp.main(
                [
                    "--runs-dir",
                    str(self.runs),
                    "--mission-id",
                    self.mission_id,
                ]
            )
        self.assertEqual(result, 0)
        mcp_constructor.assert_called_once_with(
            self.runs,
            self.mission_id,
            preset=None,
            decision_notifier=fleet_control.cmux_decision_notifier,
        )

    def test_challenge_must_be_identity_distinct_and_from_review_phase(self) -> None:
        recommendation, challenge = self.seed_evidence()
        invalid = self.brief(recommendation, challenge)
        invalid["challenge"]["artifact_id"] = recommendation
        with self.assertRaisesRegex(
            fleet_control.FleetControlError, "distinct artifact IDs"
        ):
            self.control.request_decision(
                brief=invalid,
                idempotency_key="decision:same-artifact",
            )

        scout_result = next(
            value
            for value in self.control.state()["results"].values()
            if value["artifact_id"] == recommendation
        )
        # The state-level binding remains fail-closed even if a caller attempts
        # to label a RECON result as a challenge; Control checks roster phase.
        self.assertEqual(scout_result["provider"], "openai")
        invalid = self.brief(challenge, recommendation)
        with self.assertRaisesRegex(
            fleet_control.FleetControlError, "CHALLENGE or VERIFY"
        ):
            self.control.request_decision(
                brief=invalid,
                idempotency_key="decision:recon-as-challenge",
            )

    def test_durable_reducer_rejects_recon_evidence_forged_as_challenge(self) -> None:
        recommendation, challenge = self.seed_evidence()
        requested = self.control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:valid-source-for-forgery",
        )
        payload = json.loads(json.dumps(requested["event"]["payload"]))
        source_recommendation = payload["recommendation"]
        source_challenge = payload["challenge"]
        key = "decision:direct-reducer-forgery"
        payload["decision_id"] = str(
            uuid.uuid5(uuid.UUID(self.mission_id), f"decision:{key}")
        )
        payload["recommendation"] = {
            "option_id": "additive",
            "rationale": "the real challenger is presented as recommendation",
            **{
                field: source_challenge[field]
                for field in ("artifact_id", "delegation_id", "instance")
            },
        }
        payload["challenge"] = {
            "summary": "a RECON result is forged as contrary review",
            **{
                field: source_recommendation[field]
                for field in ("artifact_id", "delegation_id", "instance")
            },
        }
        before = self.control.events()
        with self.assertRaisesRegex(
            mission_state.MissionConflict,
            "requires challenge or verify capability",
        ):
            mission_state.append_event(
                self.runs,
                self.mission_id,
                kind="human_decision_requested",
                actor="lead",
                idempotency_key=key,
                payload=payload,
            )
        self.assertEqual(self.control.events(), before)

    def test_blocking_decision_requires_affected_scope_quiescent(self) -> None:
        recommendation, challenge = self.seed_evidence()
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            self.control.dispatch(
                recipient_instance="builder",
                capability="build",
                objective="active writer before decision request",
                idempotency_key="decision:active:builder",
            )
        before = self.control.events()
        with self.assertRaisesRegex(
            fleet_control.FleetControlError, "quiescent affected instances"
        ):
            self.control.request_decision(
                brief=self.brief(recommendation, challenge),
                idempotency_key="decision:while-active",
            )
        self.assertEqual(self.control.events(), before)

    def test_expired_low_reversible_default_auto_resolves_durably(self) -> None:
        recommendation, challenge = self.seed_evidence()
        requested = self.control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:auto-default",
        )
        deadline = datetime.fromisoformat(
            requested["decision"]["deadline_at"].replace("Z", "+00:00")
        )
        after_deadline = deadline + timedelta(seconds=1)
        after_text = after_deadline.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        clock = mock.Mock()
        clock.now.return_value = after_deadline
        with (
            mock.patch.object(fleet_decisions, "datetime", clock),
            mock.patch.object(
                mission_state, "_next_timestamp", return_value=after_text
            ),
        ):
            reconciled = fleet_decisions.reconcile_expired(
                self.runs, self.mission_id
            )
        self.assertEqual(reconciled["appended"], 1)
        decision = self.control.state()["decisions"][
            requested["decision"]["decision_id"]
        ]
        self.assertEqual(decision["status"], "resolved")
        self.assertEqual(decision["resolution"]["resolution_kind"], "automatic")
        self.assertEqual(decision["resolution"]["actor"], "CONTROL")
        self.assertEqual(decision["resolution"]["option_id"], "additive")

    def test_checkpoint_allows_scoped_work_but_still_blocks_completion(self) -> None:
        recommendation, challenge = self.seed_evidence()
        brief = self.brief(recommendation, challenge)
        brief["impact"] = "checkpoint"
        requested = self.control.request_decision(
            brief=brief,
            idempotency_key="decision:checkpoint",
        )
        with mock.patch.object(fleet_control, "run_process", side_effect=self.fake_run):
            dispatched = self.control.dispatch(
                recipient_instance="builder",
                capability="build",
                objective="checkpoint decisions do not pause in-scope work",
                idempotency_key="decision:checkpoint:builder",
            )
            self.mark_succeeded("builder", b"checkpoint-scoped work completed\n")
            self.control.wait([dispatched["run_id"]], timeout_seconds=30)
        self.assertEqual(dispatched["recipient_instance"], "builder")
        with self.assertRaisesRegex(
            mission_state.MissionConflict, "pending human decisions"
        ):
            self.control.complete(
                artifact_id=recommendation,
                summary="a checkpoint still needs closure",
                idempotency_key="decision:checkpoint:complete",
            )
        self.assertIn(
            requested["decision"]["decision_id"],
            self.control.state()["pending_decisions"],
        )

    def test_non_low_or_irreversible_decision_cannot_declare_default(self) -> None:
        recommendation, challenge = self.seed_evidence()
        for index, (risk, reversible) in enumerate(
            (("medium", True), ("high", False), ("low", False))
        ):
            with self.subTest(risk=risk, reversible=reversible):
                brief = self.brief(recommendation, challenge)
                brief["risk"] = risk
                brief["reversible"] = reversible
                with self.assertRaisesRegex(
                    fleet_control.FleetControlError,
                    "only low-risk reversible decisions",
                ):
                    self.control.request_decision(
                        brief=brief,
                        idempotency_key=f"decision:unsafe-default:{index}",
                    )

    def test_automatic_resolution_is_rejected_before_exact_deadline(self) -> None:
        recommendation, challenge = self.seed_evidence()
        requested = self.control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:early-auto",
        )
        decision = requested["decision"]
        before = self.control.events()
        with self.assertRaisesRegex(
            mission_state.MissionConflict,
            "cannot resolve automatically before its deadline",
        ):
            mission_state.append_event(
                self.runs,
                self.mission_id,
                kind="human_decision_resolved",
                actor="CONTROL",
                idempotency_key="decision:auto:too-early",
                payload={
                    "decision_id": decision["decision_id"],
                    "request_event_sha256": decision["request_event_sha256"],
                    "option_id": "additive",
                    "reason": "must not resolve before two hours",
                    "resolution_kind": "automatic",
                },
            )
        self.assertEqual(self.control.events(), before)

    def test_human_resolution_wins_cleanly_over_racing_auto_reconcile(self) -> None:
        recommendation, challenge = self.seed_evidence()
        requested = self.control.request_decision(
            brief=self.brief(recommendation, challenge),
            idempotency_key="decision:human-auto-race",
        )
        decision = requested["decision"]
        deadline = datetime.fromisoformat(
            decision["deadline_at"].replace("Z", "+00:00")
        )
        clock = mock.Mock()
        clock.now.return_value = deadline + timedelta(seconds=1)
        original_append_many = mission_state.append_events
        raced = False

        def human_wins(runs_dir, mission_id, requests):
            nonlocal raced
            if not raced:
                raced = True
                fleet_decisions.resolve_human(
                    runs_dir,
                    mission_id,
                    decision_id=decision["decision_id"],
                    option_id="replace",
                    reason="the operator won the concurrent ledger race",
                    idempotency_key="human:decision:race-winner",
                )
            return original_append_many(runs_dir, mission_id, requests)

        with (
            mock.patch.object(fleet_decisions, "datetime", clock),
            mock.patch.object(
                mission_state,
                "append_events",
                side_effect=human_wins,
            ),
        ):
            reconciled = fleet_decisions.reconcile_expired(
                self.runs, self.mission_id
            )
        self.assertEqual(reconciled["appended"], 0)
        resolution = self.control.state()["decisions"][decision["decision_id"]][
            "resolution"
        ]
        self.assertEqual(resolution["actor"], "HUMAN")
        self.assertEqual(resolution["option_id"], "replace")

    def test_decision_tool_is_management_only_not_specialist_discovery(self) -> None:
        self.assertIn("request_decision", fleet_mcp.TOOL_SCHEMAS)
        self.assertNotIn("request_decision", fleet_agent_mcp.TOOL_NAMES)
        with self.assertRaisesRegex(
            fleet_control.FleetControlError, "restricted to the mission Lead"
        ):
            fleet_mcp.call_tool(
                self.control,
                "request_decision",
                {
                    "brief": self.brief("a" * 64, "b" * 64),
                    "idempotency_key": "decision:forged-specialist",
                },
                identity={"kind": "specialist"},
            )


if __name__ == "__main__":
    unittest.main()
