from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

from tests.mission_control_test_support import create_running_mission

import fleet_agent_mcp
import fleet_control
import fleet_decisions
import fleet_ledger
import fleet_mcp
import fleet_mission_state as mission_state


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
