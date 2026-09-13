from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import uuid
from unittest import mock

from tests.mission_control_test_support import legacy_v1_compiled, write_compiled


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_export_trace  # noqa: E402
import fleet_admission  # noqa: E402
import fleet_archive  # noqa: E402
import fleet_ledger  # noqa: E402
import fleet_mission  # noqa: E402
import fleet_mission_state as state  # noqa: E402
import fleet_report  # noqa: E402
import workflow_config  # noqa: E402


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
            "recipient_instance": "verifier",
        }),
    ]
    events[-1]["secret"] = "must-not-export"

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
    def test_build_report_accepts_only_bound_historical_v1_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            runs = tmp / "runs"
            runs.mkdir(mode=0o700)
            target = tmp / "target"
            target.mkdir()
            mission_id = str(uuid.uuid4())
            compiled = legacy_v1_compiled(
                workflow_config.compile_path(
                    ROOT / "workflows" / "implementation.yaml"
                )
            )
            state.append_event(
                runs,
                mission_id,
                kind="mission_created",
                actor="CONTROL",
                idempotency_key="historical:create",
                payload={
                    "feature": "historical-report",
                    "objective_sha256": "a" * 64,
                    "target_repo": str(target.resolve()),
                    "base_sha": "b" * 40,
                    "workflow_digest": compiled["workflow_digest"],
                    "initial_risk": "low",
                },
            )
            state.append_event(
                runs,
                mission_id,
                kind="workflow_compiled",
                actor="CONTROL",
                idempotency_key="historical:compiled",
                payload={"compiled_digest": compiled["compiled_digest"]},
            )
            compiled_path = runs / "missions" / mission_id / "compiled-workflow.json"
            write_compiled(compiled_path, compiled)

            report = fleet_report.build_report(runs, mission_id)
            self.assertEqual(report["mission_id"], mission_id)
            self.assertEqual(report["status"], "compiled")

            foreign = legacy_v1_compiled(
                workflow_config.compile_path(ROOT / "workflows" / "research.yaml")
            )
            write_compiled(compiled_path, foreign)
            with self.assertRaisesRegex(fleet_report.ReportError, "not bound"):
                fleet_report.build_report(runs, mission_id)

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

    def test_durable_jsonl_is_strict_deterministic_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            runs = tmp / "runs"
            target = tmp / "target"
            target.mkdir()
            compiled = workflow_config.compile_path(
                ROOT / "workflows" / "implementation.yaml"
            )
            mission_id, _ = fleet_mission.create_mission(
                runs,
                compiled=compiled,
                feature="report-strict",
                objective="private",
                target_repo=target.resolve(),
                base_sha="a" * 40,
                idempotency_key="report:strict",
            )
            legacy = runs / "fleet-report-strict.ledger.jsonl"
            cases = {
                "duplicate": b'{"run_id":"first","run_id":"second"}\n',
                "nonfinite": b'{"run_id":"nan","tokens":NaN}\n',
                "overflow": b'{"run_id":"overflow","tokens":1e999}\n',
                "bom": b'\xef\xbb\xbf{"run_id":"bom"}\n',
                "invalid-utf8": b'{"run_id":"\xff"}\n',
                "surrogate": b'{"run_id":"\\ud800"}\n',
                "crlf": b'{"run_id":"crlf"}\r\n',
                "partial": b'{"run_id":"partial"}',
                "non-object": b'[]\n',
            }
            command = [
                sys.executable,
                str(ROOT / "scripts" / "fleet_report.py"),
                "--runs-dir",
                str(runs),
                "--mission-id",
                mission_id,
                "--json",
            ]
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            for name, raw in cases.items():
                with self.subTest(name=name):
                    legacy.write_bytes(raw)
                    before_files = {
                        path.relative_to(runs): path.read_bytes()
                        for path in runs.rglob("*")
                        if path.is_file()
                    }
                    before_paths = {
                        path.relative_to(runs) for path in runs.rglob("*")
                    }

                    with self.assertRaises(fleet_report.ReportError):
                        fleet_report.build_report(runs, mission_id)
                    first = subprocess.run(
                        command,
                        cwd=ROOT,
                        env=environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    second = subprocess.run(
                        command,
                        cwd=ROOT,
                        env=environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )

                    self.assertEqual(first.returncode, 2)
                    self.assertEqual(first.stdout, "")
                    self.assertEqual(first.stderr, second.stderr)
                    self.assertTrue(first.stderr.startswith("fleet-report: "))
                    self.assertNotIn("Traceback", first.stderr)
                    self.assertEqual(
                        {path.relative_to(runs) for path in runs.rglob("*")},
                        before_paths,
                    )
                    self.assertEqual(
                        {
                            path.relative_to(runs): path.read_bytes()
                            for path in runs.rglob("*")
                            if path.is_file()
                        },
                        before_files,
                    )

    def test_unproven_archive_run_is_not_attributed_after_teardown(self) -> None:
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
            self.assertEqual(report["outcomes"]["runs"], 0)
            self.assertEqual(report["source"]["legacy_events"], 0)
            self.assertEqual(report["providers"], [])


class ReportUsageTests(unittest.TestCase):
    @staticmethod
    def report(counts: list[dict]) -> dict:
        events, compiled, legacy = synthetic_evidence()
        # Two owned synthetic runs in the same provider/model/variant group.
        selected = legacy[:2 * len(counts)]
        for index, row in enumerate(selected):
            row.update(provider='openai', model='gpt-5.6-sol', variant=None)
            if index % 2:
                row.pop('prompt_tokens')
                row.pop('completion_tokens')
                row.update(counts[index // 2])
        return fleet_report.derive_report(events, compiled, selected)

    def test_missing_null_and_partial_counts_are_unknown(self) -> None:
        for counts in ({}, {'prompt_tokens': None, 'completion_tokens': None},
                       {'prompt_tokens': 10}, {'completion_tokens': 5},
                       {'prompt_tokens': 0, 'completion_tokens': None}):
            with self.subTest(counts=counts):
                report = self.report([counts])
                group = report['providers'][0]
                self.assertIsNone(group['prompt_tokens'])
                self.assertIsNone(group['completion_tokens'])
                self.assertEqual(group['usage_observed_runs'], 0)
                self.assertEqual(group['usage_total_runs'], 1)
                self.assertEqual(group['usage_reason'], 'some_runs_lack_assignable_usage')
                self.assertIsNone(group['usage_source'])
                self.assertIsNone(group['cost_usd'])
                self.assertIn('tokens=unknown', fleet_report.human_report(report))
                self.assertIn('usage=0/1 runs', fleet_report.human_report(report))

    def test_explicit_zero_and_known_counts_remain_observed(self) -> None:
        for counts in ({'prompt_tokens': 0, 'completion_tokens': 0},
                       {'prompt_tokens': 10, 'completion_tokens': 5}):
            with self.subTest(counts=counts):
                report = self.report([counts, counts])
                group = report['providers'][0]
                self.assertEqual(group['prompt_tokens'], 2 * counts['prompt_tokens'])
                self.assertEqual(group['completion_tokens'], 2 * counts['completion_tokens'])
                self.assertEqual(group['usage_observed_runs'], 2)
                self.assertEqual(group['usage_total_runs'], 2)
                self.assertIsNone(group['usage_reason'])
                self.assertEqual(group['usage_source'], 'latest_run_ledger_counters')
                self.assertIsNone(group['cost_usd'])
                self.assertIn(f"tokens={2 * sum(counts.values())}", fleet_report.human_report(report))

    def test_mixed_group_does_not_publish_known_subtotal_as_total(self) -> None:
        known = {'prompt_tokens': 10, 'completion_tokens': 5}
        for counts in ([known, {}], [{}, known], [known, {'prompt_tokens': 20}]):
            with self.subTest(counts=counts):
                report = self.report(counts)
                group = report['providers'][0]
                self.assertIsNone(group['prompt_tokens'])
                self.assertIsNone(group['completion_tokens'])
                self.assertEqual(group['usage_observed_runs'], 1)
                self.assertEqual(group['usage_total_runs'], 2)
                self.assertIn('tokens=unknown usage=1/2 runs', fleet_report.human_report(report))

    def test_unknown_group_does_not_erase_other_models_or_variants(self) -> None:
        events, compiled, legacy = synthetic_evidence()
        legacy[1].pop('prompt_tokens')
        report = fleet_report.derive_report(events, compiled, legacy)
        groups = {p['provider']: p for p in report['providers']}
        self.assertIsNone(groups['openai']['prompt_tokens'])
        self.assertIsNone(groups['openai']['completion_tokens'])
        self.assertEqual(groups['zai']['prompt_tokens'], 20)
        self.assertEqual(groups['anthropic']['completion_tokens'], 15)
        for row in legacy[2:4]:
            row.update(provider='openai', model='gpt-5.6-sol', variant='high')
        report = fleet_report.derive_report(events, compiled, legacy)
        groups = {p['variant']: p for p in report['providers'] if p['provider'] == 'openai'}
        self.assertIsNone(groups[None]['prompt_tokens'])
        self.assertEqual(groups['high']['prompt_tokens'], 20)

    def test_invalid_counts_are_rejected_without_numeric_coercion(self) -> None:
        for field in ('prompt_tokens', 'completion_tokens'):
            for invalid in (True, False, -1, 1.5, '12', '', [], {}):
                with self.subTest(field=field, invalid=invalid):
                    counts = {'prompt_tokens': 10, 'completion_tokens': 5, field: invalid}
                    with self.assertRaisesRegex(fleet_report.ReportError, field):
                        self.report([counts])

    def test_missing_final_counts_do_not_reuse_earlier_snapshot(self) -> None:
        events, compiled, legacy = synthetic_evidence()
        earlier = {**legacy[0], 'prompt_tokens': 10, 'completion_tokens': 5}
        latest = {k: v for k, v in legacy[1].items()
                  if k not in {'prompt_tokens', 'completion_tokens'}}
        report = fleet_report.derive_report(events, compiled, [earlier, latest])
        self.assertIsNone(report['providers'][0]['prompt_tokens'])
        self.assertEqual(report['providers'][0]['usage_observed_runs'], 0)

    def test_repeated_snapshots_are_not_added_as_separate_usage(self) -> None:
        events, compiled, legacy = synthetic_evidence()
        report = fleet_report.derive_report(events, compiled, [*legacy[:2], legacy[1]])
        group = report['providers'][0]
        self.assertEqual(group['prompt_tokens'], 10)
        self.assertEqual(group['completion_tokens'], 5)
        self.assertEqual(group['usage_observed_runs'], 1)
        self.assertEqual(group['usage_total_runs'], 1)


class ReportProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.runs = self.tmp / "runs"
        self.runs.mkdir(mode=0o700)
        self.feature = "shared-report"
        self.now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
        fixture = self

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixture.now.astimezone(tz)

        for module in (state, fleet_admission, fleet_archive):
            patcher = mock.patch.object(module, "datetime", FrozenDatetime)
            patcher.start()
            self.addCleanup(patcher.stop)
        raw = copy.deepcopy(workflow_config.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )["workflow"])
        # Archive verification uses only committed Git metadata from ROOT.
        # No repository files, commits, worktrees or provider processes are created.
        raw["archive"]["include_git_delta"] = False
        raw["archive"]["include_final_tree"] = False
        self.compiled = workflow_config.compile_workflow(raw)
        self.base_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        self.members = {m["instance_id"]: m for m in self.compiled["resolved"]["instances"]}
        self.members["lead"] = self.compiled["resolved"]["lead"]

    def create(self, key: str, *, feature: str | None = None) -> str:
        self.now += timedelta(seconds=1)
        feature = feature or self.feature
        mid, _ = fleet_mission.create_mission(
            self.runs, compiled=self.compiled, feature=feature, objective=key,
            target_repo=ROOT.resolve(), base_sha=self.base_sha,
            idempotency_key=f"report-provenance:{key}",
        )
        state.append_event(self.runs, mid, kind="fleet_boot_started", actor="CONTROL",
                           idempotency_key="boot", payload={"feature": feature, "preset": "dan"})
        state.append_event(self.runs, mid, kind="mission_running", actor="CONTROL",
                           idempotency_key="running", payload={"manifest": str(self.runs / f"fleet-{feature}.manifest")})
        manifest = self.runs / f"fleet-{feature}.manifest"
        manifest.write_text(
            f"feature={feature}\nmission_id={mid}\npreset=dan\nmode=autonomous\n"
            f"target_repo={ROOT.resolve()}\nbase_sha={self.base_sha}\nworkspace=workspace:1\n"
        )
        manifest.chmod(0o600)
        return mid

    def admit(self, mid: str, instance: str = "lead", *, start: bool = True,
              register: bool = True, key: str | None = None,
              commit: bool = True, actor: str = "CONTROL") -> dict:
        key = key or instance
        is_lead = instance == "lead" and actor == "CONTROL"
        capability = "lead" if is_lead else {"lead": "synthesis", "challenger": "challenge", "verifier": "verify", "scout": "recon"}[instance]
        admission = fleet_admission.reserve_many(
            self.runs, mid, requests=[dict(request_key=key,
                run_kind="lead" if is_lead else "specialist",
                recipient_instance=instance, capability=capability, delegated_budget=0,
                writer=False, effect_sha256=state.sha256({"effect": key}),
                task_sha256=state.sha256({"task": key}))], idempotency_key=f"{key}:reserve", actor=actor,
        )["admissions"][0]
        if not commit:
            return admission
        bound = {k: admission[k] for k in ("admission_id", "run_id", "request_digest",
                                           "effect_sha256", "recipient_instance", "writer")}
        committed = fleet_admission.commit(self.runs, mid, **bound, idempotency_key=f"{key}:commit", actor=actor)
        if not start:
            return admission
        approval = fleet_mission.load_state(self.runs, mid).get("approval")
        authorized = fleet_admission.authorize_launch(
            self.runs, mid, **bound, commit_event_sha256=committed["commit_event_sha256"],
            approval_event_sha256=approval["event_sha256"] if actor == "ASSURED" else None,
            idempotency_key=f"{key}:authorize", actor=actor)
        fleet_admission.mark_started(self.runs, mid, **bound,
            authorization_event_sha256=authorized["authorization_event_sha256"],
            idempotency_key=f"{key}:start", actor=actor)
        if register and is_lead:
            state.append_event(self.runs, mid, kind="lead_dispatched", actor="CONTROL",
                idempotency_key=f"{key}:dispatch",
                payload={"run_id": admission["run_id"], "prompt_sha256": admission["task_sha256"]})
        return admission

    def lifecycle(self, mid: str, admission: dict, *, status: str = "failed",
                  tokens: tuple[int, int] | None = (11, 7),
                  initial: str = "dispatched") -> dict:
        feature = fleet_mission.load_state(self.runs, mid)["feature"]
        member = self.members[admission["recipient_instance"]]
        self.now += timedelta(seconds=1)
        stamp = self.now.isoformat()
        common = dict(run_id=admission["run_id"], feature=feature,
                      instance=admission["recipient_instance"], provider=member["provider"],
                      model=member["model"], variant=member.get("variant"))
        path = self.runs / f"fleet-{feature}.ledger.jsonl"
        fleet_ledger.append_event(path, {**common, "timestamp": stamp,
            "dispatched_at": stamp, "status": initial}, runs_dir=self.runs)
        terminal = {**common, "timestamp": stamp, "completed_at": stamp, "status": status}
        if tokens is not None:
            terminal.update(prompt_tokens=tokens[0], completion_tokens=tokens[1])
        self.assertTrue(fleet_ledger.append_event(path, terminal, runs_dir=self.runs))
        return terminal

    def finalize(self, mid: str, admission: dict, terminal: dict,
                 *, actor: str = "CONTROL") -> None:
        fleet_admission.finalize(self.runs, mid, admission_id=admission["admission_id"],
            recipient_instance=admission["recipient_instance"], writer=False,
            terminal_evidence=dict(schema_version=1, run_id=admission["run_id"],
                task_sha256=admission["task_sha256"], status=terminal["status"],
                source_event_sha256=state.sha256(terminal)), reason="fixture completed",
            idempotency_key=f"{admission['admission_id']}:finalize", actor=actor)

    def delegate(self, mid: str, admission: dict) -> None:
        member = self.members[admission["recipient_instance"]]
        state.append_event(self.runs, mid, kind="delegation_registered", actor="CONTROL",
            idempotency_key=f"{admission['admission_id']}:register", payload={
                "delegation_id": admission["delegation_id"], "mission_id": mid,
                "run_id": admission["run_id"],
                "parent_run_id": fleet_mission.load_state(self.runs, mid)["lead_run_id"],
                "delegated_by": "lead", "recipient_instance": admission["recipient_instance"],
                "capability": admission["capability"], "objective_sha256": admission["task_sha256"],
                "input_artifact_ids": [], "expected_output_contract": {},
                "deadline": (self.now + timedelta(minutes=10)).isoformat(),
                "provider": member["provider"], "model": member["model"],
                "variant": member.get("variant"), "depth": 1, "token_id": None,
            })

    def assure(self, mid: str) -> None:
        current = fleet_mission.load_state(self.runs, mid)
        state.append_event(self.runs, mid, kind="risk_escalated", actor="CONTROL",
            idempotency_key="assure:risk", payload={"from": "low", "to": "high",
                "categories": ["production"], "reason": "fixture assurance"})
        request, _ = state.append_event(self.runs, mid, kind="assurance_requested", actor="CONTROL",
            idempotency_key="assure:request", payload={"risk": "high", "categories": ["production"],
                "scope": str(ROOT.resolve()), "workflow_digest": current["workflow_digest"]})
        approval, _ = state.append_event(self.runs, mid, kind="assurance_approved", actor="HUMAN",
            idempotency_key="assure:approve", payload={"approval_id": str(uuid.uuid4()),
                "request_event_sha256": request["event_sha256"],
                "workflow_digest": current["workflow_digest"], "scope": str(ROOT.resolve()),
                "risk": "high", "expires_at": (self.now + timedelta(seconds=600)).isoformat(),
                "expires_in_seconds": 600, "approved_by_sha256": "c" * 64, "decision": "approved"})
        state.append_event(self.runs, mid, kind="assurance_boot_started", actor="CONTROL",
            idempotency_key="assure:boot", payload={"preset": "fleet_dialogue",
                "approval_event_sha256": approval["event_sha256"]})
        state.append_event(self.runs, mid, kind="assurance_started", actor="CONTROL",
            idempotency_key="assure:start", payload={"manifest": str(self.runs / f"fleet-{self.feature}.manifest"),
                "approval_event_sha256": approval["event_sha256"]})

    def historical(self) -> str:
        self.now = datetime(2026, 7, 14, tzinfo=timezone.utc)
        mid = str(uuid.uuid4())
        compiled = legacy_v1_compiled(self.compiled)
        state.append_event(self.runs, mid, kind="mission_created", actor="CONTROL",
            idempotency_key="historical:create", payload={"feature": self.feature,
                "objective_sha256": "a" * 64, "target_repo": str(ROOT.resolve()),
                "base_sha": self.base_sha, "workflow_digest": compiled["workflow_digest"],
                "initial_risk": "low"})
        state.append_event(self.runs, mid, kind="workflow_compiled", actor="CONTROL",
            idempotency_key="historical:compiled", payload={"compiled_digest": compiled["compiled_digest"]})
        write_compiled(self.runs / "missions" / mid / "compiled-workflow.json", compiled)
        return mid

    def snapshot(self) -> dict:
        return {p.relative_to(self.runs): (p.stat().st_mode, p.read_bytes())
                for p in self.runs.rglob("*") if p.is_file()}

    def finish(self, mid: str) -> None:
        state.append_terminal(self.runs, mid, status="failed", reason="fixture complete",
                              idempotency_key="finish")

    def archive(self, mid: str) -> Path:
        feature = fleet_mission.load_state(self.runs, mid)["feature"]
        result = fleet_archive.ArchiveBuilder(self.runs, mid).create(
            self.runs / f"fleet-{feature}.manifest")
        self.assertTrue(result["valid"])
        path = self.runs / "missions" / mid / "archive"
        self.assertEqual(fleet_archive.verify_archive(path)["mission_id"], mid)
        return path

    @staticmethod
    def execution(report: dict) -> dict:
        return {key: report[key] for key in ("outcomes", "providers", "timing", "delegation")}

    def test_later_mission_same_feature_cannot_change_owned_live_metrics(self) -> None:
        first = self.create("first")
        a = self.admit(first)
        self.finalize(first, a, self.lifecycle(first, a))
        self.finish(first)
        before = fleet_report.build_report(self.runs, first)
        second = self.create("second")
        b = self.admit(second)
        self.lifecycle(second, b, tokens=(200, 100))
        after = fleet_report.build_report(self.runs, first)
        self.assertEqual(self.execution(after), self.execution(before))
        self.assertEqual(after["source"]["legacy_events"], before["source"]["legacy_events"])
        # This diagnostic hashes the physical feature container, not owned rows.
        self.assertNotEqual(after["source"]["legacy_ledger_sha256"], before["source"]["legacy_ledger_sha256"])
        self.assertEqual(after["source"]["legacy_ledger_sha256"], hashlib.sha256(
            (self.runs / f"fleet-{self.feature}.ledger.jsonl").read_bytes()).hexdigest())
        self.assertEqual(fleet_report.build_report(self.runs, first), after)

    def test_later_live_ledger_cannot_displace_verified_mission_archive(self) -> None:
        first = self.create("archived-first")
        a = self.admit(first)
        self.finalize(first, a, self.lifecycle(first, a))
        self.finish(first)
        archive = self.archive(first)
        ledger = self.runs / f"fleet-{self.feature}.ledger.jsonl"
        ledger.rename(self.tmp / "retired-first-ledger.jsonl")
        before = fleet_report.build_report(self.runs, first)
        self.assertEqual(before["source"]["legacy_ledger_source"], "unified_archive")
        frozen = {p.relative_to(archive): p.read_bytes() for p in archive.rglob("*") if p.is_file()}
        second = self.create("archived-second")
        b = self.admit(second)
        self.lifecycle(second, b, tokens=(200, 100))
        worker = self.admit(second, "challenger")
        self.lifecycle(second, worker, tokens=(300, 150))
        after = fleet_report.build_report(self.runs, first)
        self.assertEqual(after, before)
        self.assertEqual({p.relative_to(archive): p.read_bytes() for p in archive.rglob("*") if p.is_file()}, frozen)

    def test_lead_without_delegation_and_multiple_delegated_runs_remain_owned(self) -> None:
        mid = self.create("multiple")
        lead = self.admit(mid)
        self.lifecycle(mid, lead, tokens=None)
        self.assertEqual(fleet_mission.load_state(self.runs, mid)["delegations"], {})
        first = fleet_report.build_report(self.runs, mid)
        self.assertEqual(first["outcomes"]["runs"], 1)
        self.assertIsNone(first["providers"][0]["prompt_tokens"])
        for instance in ("challenger", "verifier"):
            worker = self.admit(mid, instance)
            self.delegate(mid, worker)
            self.finalize(mid, worker, self.lifecycle(mid, worker))
        report = fleet_report.build_report(self.runs, mid)
        self.assertEqual(report["delegation"]["count"], 2)
        self.assertEqual(report["outcomes"]["runs"], 3)
        self.assertEqual(report["source"]["legacy_events"], 6)
        observed = [p for p in report["providers"] if p["prompt_tokens"] is not None]
        self.assertEqual(len(observed), 2)
        self.assertEqual(sum(p["prompt_tokens"] for p in observed), 22)

    def test_aborted_admission_history_owns_prelaunch_failure_but_invents_no_run(self) -> None:
        mid = self.create("abort")
        for committed in (False, True):
            with self.subTest(committed=committed):
                item = self.admit(mid, "scout", commit=committed, start=False,
                                  key=f"abort-{committed}")
                if committed:
                    # Frontier preparation may append an abandoned terminal before launch.
                    self.lifecycle(mid, item, initial="preparing", status="abandoned", tokens=None)
                fleet_admission.abort_prelaunch(self.runs, mid,
                    **{k: item[k] for k in ("admission_id", "run_id", "request_digest",
                        "effect_sha256", "task_sha256", "recipient_instance", "writer")},
                    reason="fixture prelaunch failure", idempotency_key=f"abort:{committed}")
                current = fleet_mission.load_state(self.runs, mid)
                self.assertNotIn(item["run_id"], current["run_owners"])
                self.assertEqual(current["admissions"][item["admission_id"]]["phase"], "aborted")
                self.assertEqual(fleet_report.build_report(self.runs, mid)["outcomes"]["runs"], int(committed))
        self.finish(mid)
        report = fleet_report.build_report(self.runs, mid)
        self.assertEqual(report["outcomes"]["counts"]["abandoned"], 1)

    def test_current_assurance_and_synthesis_admissions_keep_mission_identity(self) -> None:
        mid = self.create("assured")
        main = self.admit(mid)
        self.finalize(mid, main, self.lifecycle(mid, main))
        self.assure(mid)
        for instance in ("challenger", "lead"):
            item = self.admit(mid, instance, key=f"assured-{instance}", actor="ASSURED", register=False)
            self.finalize(mid, item, self.lifecycle(mid, item), actor="ASSURED")
        before = fleet_report.build_report(self.runs, mid)
        self.assertEqual(before["outcomes"]["runs"], 3)
        self.assertEqual(before["delegation"]["count"], 0)
        self.finish(mid)
        before = fleet_report.build_report(self.runs, mid)
        other = self.create("after-assurance")
        self.lifecycle(other, self.admit(other), tokens=(900, 800))
        self.assertEqual(self.execution(fleet_report.build_report(self.runs, mid)), self.execution(before))

    def test_historical_lead_and_delegations_keep_only_proven_runs(self) -> None:
        mid = self.historical()
        source_events, _, records = synthetic_evidence()
        for event in source_events:
            if event["kind"] not in {"lead_dispatched", "delegation_registered"}:
                continue
            payload = dict(event["payload"])
            if event["kind"] == "delegation_registered":
                payload["mission_id"] = mid
            state.append_event(self.runs, mid, kind=event["kind"], actor="CONTROL",
                idempotency_key=event["idempotency_key"], payload=payload)
        path = self.runs / f"fleet-{self.feature}.ledger.jsonl"
        for record in records:
            fleet_ledger.append_event(path, {**record, "feature": self.feature}, runs_dir=self.runs)
        before = fleet_report.build_report(self.runs, mid)
        self.assertEqual(before["outcomes"]["runs"], 3)
        self.assertEqual(fleet_mission.load_state(self.runs, mid)["admissions"], {})
        foreign = self.create("after-historical")
        self.lifecycle(foreign, self.admit(foreign), tokens=(400, 500))
        self.assertEqual(self.execution(fleet_report.build_report(self.runs, mid)), self.execution(before))

    def test_historical_assurance_dispatch_owns_run_but_wait_does_not(self) -> None:
        mid = self.historical()
        self.assure(mid)
        item = dict(run_id=str(uuid.uuid4()), recipient_instance="challenger")
        payload = dict(action="dispatch", instance="challenger", prompt_sha256="d" * 64,
                       controller_sequence="1")
        state.append_event(self.runs, mid, kind="assured_action_intent", actor="ASSURED",
            idempotency_key="assured:intent", payload=payload)
        state.append_event(self.runs, mid, kind="assured_action_completed", actor="ASSURED",
            idempotency_key="assured:dispatch", payload={**payload, "run_id": item["run_id"]})
        self.lifecycle(mid, item)
        self.assertEqual(fleet_mission.load_state(self.runs, mid)["run_owners"], {})
        self.assertEqual(fleet_report.build_report(self.runs, mid)["outcomes"]["runs"], 1)
        other = self.create("wait-reference")
        foreign = self.admit(other)
        self.lifecycle(other, foreign, tokens=(400, 500))
        # A reference to another run is observational evidence, not a dispatch.
        state.append_event(self.runs, mid, kind="assured_action_completed", actor="ASSURED",
            idempotency_key="assured:wait", payload=dict(action="wait", instance="lead",
                run_id=foreign["run_id"], timeout_seconds=10, controller_sequence="2",
                status="failed", exit_code=1))
        report = fleet_report.build_report(self.runs, mid)
        self.assertEqual(report["outcomes"]["runs"], 1)
        self.assertEqual(sum(p["prompt_tokens"] for p in report["providers"]), 11)

    def test_verified_archive_filters_unrelated_runs_in_its_feature_snapshot(self) -> None:
        other = self.create("earlier")
        self.finalize(other, item := self.admit(other), self.lifecycle(other, item, tokens=(300, 200)))
        self.finish(other)
        mid = self.create("archive-with-mixed-container")
        self.finalize(mid, own := self.admit(mid), self.lifecycle(mid, own))
        self.finish(mid)
        archive = self.archive(mid)
        self.assertIn(item["run_id"], (archive / "ledger.jsonl").read_text())
        (self.runs / f"fleet-{self.feature}.ledger.jsonl").rename(self.tmp / "retired.jsonl")
        report = fleet_report.build_report(self.runs, mid)
        self.assertEqual(report["source"]["legacy_ledger_source"], "unified_archive")
        self.assertTrue(report["archive"]["verified"])
        self.assertEqual(report["outcomes"]["runs"], 1)
        self.assertEqual(sum(p["prompt_tokens"] for p in report["providers"]), 11)

    def test_own_live_evidence_after_early_archive_remains_authoritative(self) -> None:
        mid = self.create("early-archive")
        lead = self.admit(mid)
        common = dict(run_id=lead["run_id"], feature=self.feature, instance="lead",
            provider=self.members["lead"]["provider"], model=self.members["lead"]["model"],
            timestamp=self.now.isoformat(), dispatched_at=self.now.isoformat(), status="dispatched")
        path = self.runs / f"fleet-{self.feature}.ledger.jsonl"
        fleet_ledger.append_event(path, common, runs_dir=self.runs)
        archive = self.archive(mid)
        frozen = (archive / "ledger.jsonl").read_bytes()
        worker = self.admit(mid, "challenger")
        self.delegate(mid, worker)
        self.finalize(mid, worker, self.lifecycle(mid, worker))
        terminal = {**common, "status": "failed", "completed_at": self.now.isoformat()}
        fleet_ledger.append_event(path, terminal, runs_dir=self.runs)
        self.finalize(mid, lead, terminal)
        report = fleet_report.build_report(self.runs, mid)
        self.assertTrue(report["archive"]["verified"])
        self.assertEqual(report["source"]["legacy_ledger_source"], "live")
        self.assertEqual(report["outcomes"]["runs"], 2)
        self.assertEqual(report["outcomes"]["counts"]["failed"], 2)
        self.assertEqual((archive / "ledger.jsonl").read_bytes(), frozen)

    def test_invalid_archive_uses_only_owned_live_evidence_or_none(self) -> None:
        mid = self.create("invalid-archive")
        self.finalize(mid, own := self.admit(mid), self.lifecycle(mid, own))
        self.finish(mid)
        archive = self.archive(mid)
        (archive / "ledger.jsonl").write_bytes(b"{}\n")  # Invalid digest in this isolated fixture.
        before = fleet_report.build_report(self.runs, mid)
        self.assertFalse(before["archive"]["verified"])
        self.assertEqual(before["outcomes"]["runs"], 1)
        (self.runs / f"fleet-{self.feature}.ledger.jsonl").rename(self.tmp / "retired.jsonl")
        other = self.create("invalid-fallback-foreign")
        self.lifecycle(other, self.admit(other))
        after = fleet_report.build_report(self.runs, mid)
        self.assertEqual(after["outcomes"]["runs"], 0)
        self.assertEqual(after["source"]["legacy_ledger_source"], "none")
        self.assertFalse(after["archive"]["verified"])

    def test_valid_archive_from_different_mission_is_not_trusted(self) -> None:
        mid = self.create("wrong-archive")
        self.lifecycle(mid, self.admit(mid))
        other = self.create("archive-owner", feature="other-feature")
        self.finalize(other, item := self.admit(other), self.lifecycle(other, item))
        self.finish(other)
        other_archive = self.archive(other)
        archive = self.runs / "missions" / mid / "archive"
        shutil.copytree(other_archive, archive)
        self.assertEqual(fleet_archive.verify_archive(archive)["mission_id"], other)
        before = self.snapshot()
        report = fleet_report.build_report(self.runs, mid)
        self.assertFalse(report["archive"]["verified"])
        self.assertEqual(report["outcomes"]["runs"], 1)
        self.assertEqual(report["source"]["legacy_ledger_source"], "live")
        self.assertEqual(self.snapshot(), before)
        (self.runs / f"fleet-{self.feature}.ledger.jsonl").rename(self.tmp / "retired.jsonl")
        self.assertEqual(fleet_report.build_report(self.runs, mid)["outcomes"]["runs"], 0)

    def test_unproven_feature_records_are_excluded_and_reporting_is_read_only(self) -> None:
        mid = self.create("unknown")
        other = self.create("other")
        foreign = self.admit(other)
        self.lifecycle(other, foreign)
        before = self.snapshot()
        report = fleet_report.build_report(self.runs, mid)
        self.assertEqual(report["outcomes"]["runs"], 0)
        self.assertEqual(report["providers"], [])
        self.assertEqual(report["source"]["legacy_events"], 0)
        self.assertEqual(report["source"]["legacy_ledger_source"], "none")
        for _ in range(3):
            self.assertEqual(fleet_report.build_report(self.runs, mid), report)
        command = [sys.executable, "-B", str(ROOT / "scripts" / "fleet_report.py"),
                   "--runs-dir", str(self.runs), "--mission-id", mid, "--json"]
        cli = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout), report)
        self.assertEqual(self.snapshot(), before)

    def test_activity_for_other_feature_cannot_change_report(self) -> None:
        mid = self.create("stable")
        self.lifecycle(mid, self.admit(mid))
        before = fleet_report.build_report(self.runs, mid)
        other = self.create("unrelated", feature="unrelated-feature")
        self.lifecycle(other, self.admit(other), tokens=(400, 500))
        self.assertEqual(fleet_report.build_report(self.runs, mid), before)

    def test_direct_derive_interface_does_not_infer_ownership_from_feature(self) -> None:
        events, compiled, records = synthetic_evidence()
        before = fleet_report.derive_report(events, compiled, records)
        foreign_events, _, foreign = synthetic_evidence()
        self.assertNotEqual(events[0]["mission_id"], foreign_events[0]["mission_id"])
        self.assertEqual(events[0]["payload"]["feature"], foreign_events[0]["payload"]["feature"])
        self.assertEqual(fleet_report.derive_report(events, compiled, records + foreign), before)

    def test_run_record_claim_or_context_reference_is_not_ownership(self) -> None:
        events, compiled, records = synthetic_evidence()
        foreign = dict(records[0], run_id=str(uuid.uuid4()), mission_id=events[0]["mission_id"])
        # A relay may mention a recipient without establishing that it was dispatched here.
        events[-1]["payload"]["recipient_run_id"] = foreign["run_id"]
        before = fleet_report.derive_report(events, compiled, records)
        self.assertEqual(fleet_report.derive_report(events, compiled, records + [foreign]), before)

    def test_owned_run_identity_drift_is_still_rejected(self) -> None:
        events, compiled, records = synthetic_evidence()
        with self.assertRaisesRegex(fleet_report.ReportError, "identity drift"):
            fleet_report.derive_report(events, compiled, records + [dict(records[0], provider="other")])

    def test_real_frontier_missing_usage_stays_unknown_in_live_archive_and_cli(self) -> None:
        import fleet_frontier

        mid = self.create('frontier-unknown')
        admission = self.admit(mid)
        member = self.members['lead']
        started = dict(run_id=admission['run_id'], feature=self.feature, instance='lead',
            provider=member['provider'], model=member['model'], variant=member.get('variant'),
            timestamp=self.now.isoformat(), dispatched_at=self.now.isoformat(), status='dispatched')
        ledger = self.runs / f'fleet-{self.feature}.ledger.jsonl'
        fleet_ledger.append_event(ledger, started, runs_dir=self.runs)
        # Real producer and lease inspection; no runtime or provider invocation.
        with mock.patch('subprocess.run', side_effect=AssertionError('report fixture invoked runtime')):
            terminal = fleet_frontier.terminalize(self.runs, started, status='failed', reason='fixture')
        self.assertNotIn('prompt_tokens', terminal)
        self.assertNotIn('completion_tokens', terminal)
        self.finalize(mid, admission, terminal)
        self.finish(mid)
        before = self.snapshot()
        live = fleet_report.build_report(self.runs, mid)
        self.assertIsNone(live['providers'][0]['prompt_tokens'])
        self.assertIsNone(live['providers'][0]['completion_tokens'])
        self.assertEqual(live['providers'][0]['usage_observed_runs'], 0)
        self.assertEqual(live['providers'][0]['usage_total_runs'], 1)
        self.assertIsNone(live['providers'][0]['cost_usd'])
        self.assertEqual(fleet_report.build_report(self.runs, mid), live)
        self.assertEqual(self.snapshot(), before)

        archive = self.archive(mid)
        ledger.rename(self.tmp/'retired-unknown.jsonl')
        archived = fleet_report.build_report(self.runs, mid)
        self.assertEqual(archived['source']['legacy_ledger_source'], 'unified_archive')
        self.assertEqual(archived['providers'], live['providers'])
        frozen = {p.relative_to(archive): p.read_bytes() for p in archive.rglob('*') if p.is_file()}
        other = self.create('later-known')
        self.lifecycle(other, self.admit(other), tokens=(900, 800))
        self.assertEqual(fleet_report.build_report(self.runs, mid), archived)

        before = self.snapshot()
        for flags in ([], ['--json']):
            cli = subprocess.run([sys.executable, '-B', str(ROOT/'scripts/fleet_report.py'),
                '--runs-dir', str(self.runs), '--mission-id', mid, *flags],
                cwd=ROOT, text=True, capture_output=True, check=True)
            if flags:
                self.assertEqual(json.loads(cli.stdout), archived)
            else:
                self.assertIn('tokens=unknown usage=0/1 runs', cli.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual({p.relative_to(archive): p.read_bytes() for p in archive.rglob('*') if p.is_file()}, frozen)

    def test_historical_unknown_usage_and_foreign_invalid_counts_remain_isolated(self) -> None:
        mid = self.historical()
        owned = dict(run_id=str(uuid.uuid4()), recipient_instance='lead')
        state.append_event(self.runs, mid, kind='lead_dispatched', actor='CONTROL',
            idempotency_key='historical:lead', payload={'run_id': owned['run_id'], 'prompt_sha256': 'd'*64})
        self.lifecycle(mid, owned, tokens=None)
        report = fleet_report.build_report(self.runs, mid)
        self.assertIsNone(report['providers'][0]['prompt_tokens'])
        self.assertEqual(report['providers'][0]['usage_total_runs'], 1)
        other = self.create('foreign-invalid-usage')
        self.lifecycle(other, self.admit(other), tokens=('invalid', True))
        before = self.snapshot()
        self.assertEqual(self.execution(fleet_report.build_report(self.runs, mid)), self.execution(report))
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
