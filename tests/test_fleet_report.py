from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import uuid
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_export_trace
import fleet_mission
import fleet_mission_state as state
import fleet_report
import fleet_trace
import workflow_config


def synthetic_evidence() -> tuple[list[dict], dict, list[dict]]:
    compiled = workflow_config.compile_path(ROOT / "workflows" / "implementation.yaml")
    mission_id = str(uuid.uuid4())
    lead_run, challenge_run, verify_run = (str(uuid.uuid4()) for _ in range(3))
    challenge_delegation, verify_delegation = (str(uuid.uuid4()) for _ in range(2))
    request_sha = "1" * 64

    def event(sequence: int, kind: str, timestamp: str, payload: dict) -> dict:
        return {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "mission_id": mission_id,
            "sequence": sequence,
            "timestamp": timestamp,
            "kind": kind,
            "actor": "CONTROL",
            "idempotency_key": f"eval:{sequence}",
            "payload": payload,
            "previous_event_sha256": "0" * 64,
            "event_sha256": request_sha if kind == "assurance_requested" else f"{sequence:x}" * 64,
        }

    target = str(ROOT)
    events = [
        event(1, "mission_created", "2026-07-14T00:00:00Z", {
            "feature": "report-eval", "objective_sha256": "a" * 64,
            "target_repo": target, "base_sha": "b" * 40,
            "workflow_digest": compiled["workflow_digest"], "initial_risk": "low",
        }),
        event(2, "workflow_compiled", "2026-07-14T00:00:01Z", {
            "compiled_digest": compiled["compiled_digest"],
        }),
        event(3, "risk_escalated", "2026-07-14T00:00:02Z", {
            "from": "low", "to": "high", "categories": ["production"], "reason": "eval",
        }),
        event(4, "assurance_requested", "2026-07-14T00:00:03Z", {
            "risk": "high", "categories": ["production"], "scope": target,
            "workflow_digest": compiled["workflow_digest"],
        }),
        event(5, "assurance_approved", "2026-07-14T00:00:08Z", {
            "approval_id": str(uuid.uuid4()), "request_event_sha256": request_sha,
            "workflow_digest": compiled["workflow_digest"], "scope": target,
            "risk": "high", "expires_at": "2026-07-14T01:00:00Z",
            "approved_by_sha256": "c" * 64, "decision": "approved",
        }),
        event(6, "lead_dispatched", "2026-07-14T00:00:09Z", {
            "run_id": lead_run, "prompt_sha256": "d" * 64,
        }),
        event(7, "delegation_registered", "2026-07-14T00:00:10Z", {
            "delegation_id": challenge_delegation, "mission_id": mission_id,
            "run_id": challenge_run, "parent_run_id": lead_run, "delegated_by": "lead",
            "recipient_instance": "challenger", "capability": "challenge",
            "objective_sha256": "e" * 64, "input_artifact_ids": [],
            "expected_output_contract": {}, "deadline": "2026-07-14T01:00:00Z",
            "provider": "zai", "model": "glm-5.2", "variant": None,
            "depth": 1, "token_id": None,
        }),
        event(8, "delegation_registered", "2026-07-14T00:00:11Z", {
            "delegation_id": verify_delegation, "mission_id": mission_id,
            "run_id": verify_run, "parent_run_id": lead_run, "delegated_by": "lead",
            "recipient_instance": "verifier", "capability": "verify",
            "objective_sha256": "f" * 64, "input_artifact_ids": [],
            "expected_output_contract": {}, "deadline": "2026-07-14T01:00:00Z",
            "provider": "anthropic", "model": "claude-fable-5", "variant": None,
            "depth": 1, "token_id": None,
        }),
        event(9, "result_recorded", "2026-07-14T00:00:22Z", {
            "run_id": challenge_run, "delegation_id": challenge_delegation,
            "artifact_id": "2" * 64, "provider": "zai", "model": "glm-5.2",
            "variant": None,
        }),
        event(10, "result_recorded", "2026-07-14T00:00:23Z", {
            "run_id": verify_run, "delegation_id": verify_delegation,
            "artifact_id": "3" * 64, "provider": "anthropic",
            "model": "claude-fable-5", "variant": None,
        }),
        event(11, "result_relayed", "2026-07-14T00:00:24Z", {
            "artifact_id": "2" * 64, "recipient_run_id": verify_run,
            "recipient_instance": "verifier", "secret": "must-not-export",
        }),
    ]

    def run_event(
        run_id: str, instance: str, provider: str, model: str,
        started: str, ended: str, tokens: tuple[int, int],
    ) -> list[dict]:
        common = {
            "run_id": run_id, "feature": "report-eval", "instance": instance,
            "provider": provider, "model": model, "variant": None,
        }
        return [
            {**common, "timestamp": started, "dispatched_at": started, "status": "dispatched"},
            {
                **common, "timestamp": ended, "completed_at": ended, "status": "succeeded",
                "prompt_tokens": tokens[0], "completion_tokens": tokens[1],
            },
        ]

    legacy = [
        *run_event(lead_run, "lead", "openai", "gpt-5.6-sol", "2026-07-14T00:00:09Z", "2026-07-14T00:00:12Z", (10, 5)),
        *run_event(challenge_run, "challenger", "zai", "glm-5.2", "2026-07-14T00:00:10Z", "2026-07-14T00:00:20Z", (20, 10)),
        *run_event(verify_run, "verifier", "anthropic", "claude-fable-5", "2026-07-14T00:00:11Z", "2026-07-14T00:00:21Z", (30, 15)),
    ]
    return events, compiled, legacy


class FleetReportTests(unittest.TestCase):
    def test_orchestration_eval_cases_match_expected_metrics(self) -> None:
        fixtures = json.loads(
            (ROOT / "evals" / "fixtures" / "orchestration-cases.json").read_text()
        )
        expected = json.loads(
            (ROOT / "evals" / "expected" / "orchestration-cases.json").read_text()
        )["expected"]
        self.assertEqual(
            {case["focus"] for case in fixtures["cases"]},
            {"routing", "parallelism", "relay", "escalation"},
        )
        events, compiled, legacy = synthetic_evidence()
        report = fleet_report.derive_report(events, compiled, legacy)
        actual = {
            "routing-selection": {
                "selected": [item["instance_id"] for item in report["agents"]["selected"]],
                "omitted": [item["instance_id"] for item in report["agents"]["omitted"]],
                "decision_authority": report["decision_authority"],
            },
            "parallel-specialists": {
                "max_parallel_runs": report["delegation"]["max_parallel_runs"],
                "max_fan_out": report["delegation"]["max_fan_out"],
            },
            "relay-adoption": {
                "relayed_results": report["adoption"]["relayed_results"],
                "durable_result_sets": report["findings"]["durable_result_sets"],
            },
            "risk-escalation-human-wait": {
                "human_wait_seconds": report["timing"]["human_wait_seconds"],
                "raw_content_allowed": False,
            },
        }
        self.assertEqual(actual, expected)

    def test_report_derives_routing_parallelism_relay_and_escalation(self) -> None:
        events, compiled, legacy = synthetic_evidence()
        report = fleet_report.derive_report(events, compiled, legacy)
        self.assertEqual(report["decision_authority"], "lead")
        self.assertEqual(
            [item["instance_id"] for item in report["agents"]["selected"]],
            ["challenger", "lead", "verifier"],
        )
        self.assertEqual(
            [item["instance_id"] for item in report["agents"]["omitted"]],
            ["builder", "scout"],
        )
        self.assertEqual(report["delegation"]["max_fan_out"], 2)
        self.assertEqual(report["delegation"]["max_parallel_runs"], 3)
        self.assertEqual(report["timing"]["human_wait_seconds"], 5.0)
        self.assertEqual(report["timing"]["time_to_first_useful_result_seconds"], 22.0)
        self.assertEqual(report["adoption"]["relayed_results"], 1)
        self.assertEqual(report["findings"]["durable_result_sets"], 2)
        self.assertEqual(sum(item["prompt_tokens"] for item in report["providers"]), 60)
        self.assertTrue(all(item["cost_usd"] is None for item in report["providers"]))
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("must-not-export", serialized)
        self.assertNotIn("objective", serialized)

    def test_trace_parent_child_and_exporter_failure_are_non_authoritative(self) -> None:
        events, _, _ = synthetic_evidence()
        audit_event = {
            "event_id": str(uuid.uuid4()), "sequence": 1,
            "timestamp": "2026-07-14T00:00:25Z", "event_sha256": "9" * 64,
            "worm_backend": "s3-object-lock", "worm_compliance_mode": True,
            "worm_trust_scope": "local-development",
            "worm_retention_mode": "COMPLIANCE",
            "worm_object_key": "fleet-audits/mission/event.json",
        }
        envelope = fleet_export_trace.trace_envelope(events, [audit_event])
        spans = envelope["spans"]
        delegation = next(span for span in spans if span["name"] == "delegation")
        agent = next(
            span for span in spans
            if span["name"] == "agent_run" and span["parent_span_id"] == delegation["span_id"]
        )
        self.assertEqual(agent["attributes"]["recipient_instance"], "challenger")
        self.assertEqual(
            next(span for span in spans if span["name"] == "worm_anchor")["attributes"][
                "worm_backend"
            ],
            "s3-object-lock",
        )
        worm_attributes = next(
            span for span in spans if span["name"] == "worm_anchor"
        )["attributes"]
        self.assertEqual(worm_attributes["worm_trust_scope"], "local-development")
        self.assertTrue(worm_attributes["worm_compliance_mode"])
        self.assertEqual(worm_attributes["worm_retention_mode"], "COMPLIANCE")
        self.assertEqual(
            worm_attributes["worm_object_key"], "fleet-audits/mission/event.json"
        )
        self.assertNotIn("must-not-export", json.dumps(envelope))
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("offline")
        ):
            output = Path(temporary) / "trace.json"
            result = fleet_export_trace.export_trace(
                events, output=output, endpoint="http://127.0.0.1:9/traces"
            )
            self.assertTrue(result["file_exported"])
            self.assertFalse(result["endpoint_exported"])
            self.assertEqual(result["authority"], "observational_only")
            self.assertEqual(len(result["warnings"]), 1)
            self.assertEqual(json.loads(output.read_text())["authority"], "observational_only")

    def test_build_report_verifies_durable_ledgers_and_cli_redacts_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            runs = tmp / "runs"
            target = tmp / "target"
            target.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=target, check=True)
            subprocess.run(["git", "config", "user.email", "fleet@example.test"], cwd=target, check=True)
            subprocess.run(["git", "config", "user.name", "Fleet"], cwd=target, check=True)
            (target / "README").write_text("base\n")
            subprocess.run(["git", "add", "README"], cwd=target, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=target, check=True)
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=target, text=True,
                stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            compiled = workflow_config.compile_path(ROOT / "workflows" / "implementation.yaml")
            mission_id, _ = fleet_mission.create_mission(
                runs, compiled=compiled, feature="report-cli",
                objective="private objective that must never appear", target_repo=target.resolve(),
                base_sha=sha, idempotency_key="report:cli",
            )
            report = fleet_report.build_report(runs, mission_id)
            self.assertTrue(report["source"]["durable_only"])
            self.assertIsNone(report["source"]["legacy_ledger_sha256"])
            result = subprocess.run(
                [
                    "python3", str(ROOT / "scripts" / "fleet_report.py"),
                    "--runs-dir", str(runs), "--mission-id", mission_id, "--json",
                ],
                cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("private objective", result.stdout)
            legacy = runs / "fleet-report-cli.ledger.jsonl"
            legacy.write_text('{"run_id":"partial"}', encoding="utf-8")
            with self.assertRaisesRegex(fleet_report.ReportError, "partial"):
                fleet_report.build_report(runs, mission_id)

    def test_report_uses_verified_unified_archive_ledger_after_teardown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            runs = tmp / "runs"
            target = tmp / "target"
            target.mkdir()
            compiled = workflow_config.compile_path(ROOT / "workflows" / "implementation.yaml")
            mission_id, _ = fleet_mission.create_mission(
                runs,
                compiled=compiled,
                feature="post-teardown",
                objective="private",
                target_repo=target.resolve(),
                base_sha="a" * 40,
                idempotency_key="post:teardown",
            )
            archive = runs / "missions" / mission_id / "archive"
            archive.mkdir()
            (archive / "ledger.jsonl").write_text(
                json.dumps({
                    "timestamp": "2026-07-14T00:00:01Z",
                    "completed_at": "2026-07-14T00:00:02Z",
                    "run_id": str(uuid.uuid4()),
                    "instance": "lead",
                    "provider": "openai",
                    "model": "gpt-5.6-sol",
                    "status": "succeeded",
                }) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                fleet_report,
                "_archive_report",
                return_value={
                    "present": True, "verified": True, "bytes": 1, "entries": 1
                },
            ):
                report = fleet_report.build_report(runs, mission_id)
            self.assertEqual(report["source"]["legacy_ledger_source"], "unified_archive")
            self.assertEqual(report["outcomes"]["counts"]["succeeded"], 1)
            self.assertEqual(report["providers"][0]["provider"], "openai")


if __name__ == "__main__":
    unittest.main()
