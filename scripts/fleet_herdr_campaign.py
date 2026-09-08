#!/usr/bin/env python3
"""Reproducible local campaign: synthetic roles, real Fleet control and Docker checks."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fleet_acceptance
import fleet_artifacts
import fleet_functional
import fleet_herdr_archive
import fleet_herdr_control as control
import fleet_herdr_mission as driver
import fleet_mission
import fleet_mission_state as state
import fleet_report
import fleet_trace
import workflow_config
from tests.test_fleet_herdr_mission import FakeBackend

GOOD = '''def stats(values):
    if not values: raise ValueError()
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in values): raise TypeError()
    return {"count": len(values), "sum": sum(values), "mean": sum(values) / len(values)}
'''
BROKEN = 'def stats(values):\n    return {"count":7,"sum":42,"mean":6}\n'
SCENARIOS = {"correct": "succeeded", "functional-failure": "failed", "artifact-rejection": "failed",
             "pause-resume-recovery": "succeeded", "functional-cancellation": "abandoned"}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


class CampaignBackend(FakeBackend):
    """No Herdr/model invocation; timestamps measure this synthetic fixture only."""
    polls = {}

    def submit(self, run_id, prompt, *, instance_id):
        started = stamp()
        try:
            return super().submit(run_id, prompt, instance_id=instance_id)
        finally:
            if run_id in self.results:
                result = self.results[run_id]
                raw = fleet_artifacts.get_bytes(self.runs, self.mid, result["evidence"]["transcript_artifact_id"])
                rows = [json.loads(line) for line in raw.splitlines()]
                ended = stamp()
                for row in rows:
                    row["timestamp"] = ended if row.get("payload", {}).get("type") == "task_complete" else started
                identifier = fleet_artifacts.put_bytes(self.runs, self.mid,
                    b"".join(state.canonical_bytes(row)+b"\n" for row in rows))["artifact_id"]
                result["evidence"].update(transcript_artifact_id=identifier, transcript_sha256=identifier)
                result.pop("result_artifact_id", None)
                result["result_artifact_id"] = fleet_artifacts.put_bytes(self.runs, self.mid, state.canonical_bytes(result))["artifact_id"]

    def collect_result(self, run_id):
        self.polls[run_id] = self.polls.get(run_id, 0) + 1
        if self.polls[run_id] == 1:
            self.calls.append(("collect_pending", run_id))
            return None
        return super().collect_result(run_id)

    def wait(self, run_id, *, timeout_ms):
        self.calls.append(("wait", run_id, timeout_ms))
        time.sleep(min(0.01, timeout_ms / 1000))
        return {"status": "settled"}


def git(repo, *args):
    env={k:v for k,v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-c", "init.templateDir=", "-C", str(repo), *args],
        env=env, stderr=subprocess.PIPE, text=True).strip()


def campaign(output):
    output = Path(output)
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ValueError("campaign requires a new absolute output directory")
    spec = fleet_functional.make_spec(ROOT / "tests/fixtures/functional_stats/test_sample_stats.py")
    compiled = workflow_config.compile_path(ROOT / "workflows/herdr-implementation.yaml")
    output.mkdir(parents=True, mode=0o700)
    records = []
    for scenario, expected in SCENARIOS.items():
        case = output / scenario
        target = case / "target"
        target.mkdir(parents=True)
        git(target, "init", "-q")
        (target / "README.md").write_text("SYNTHETIC fixture; no provider or client data.\n")
        (target / "sample_stats.py").write_text(BROKEN if scenario == "functional-failure" else GOOD)
        git(target, "add", ".")
        git(target, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--no-gpg-sign", "-qm", "fixture")
        runs = case / "runs"
        acceptance = {"schema_version": 1, "requirements": [{"id": "answer", "description": "synthetic artifact predicate",
            "checks": [{"kind":"text_contains", "path":"answer.txt", "expected":"missing-required-content" if scenario == "artifact-rejection" else "implemented"}]}]}
        mid, _ = fleet_mission.create_mission(runs, compiled=compiled, feature=scenario,
            objective="Implement synthetic stats and answer artifacts", target_repo=target, base_sha=git(target,"rev-parse","HEAD"),
            idempotency_key=fleet_acceptance.bound_key("campaign:"+str(uuid.uuid4()), acceptance),
            runtime_options={"herdr_session":"SYNTHETIC_NO_PROVIDER", "timeout_seconds":120,
                "functional_contract":spec, "acceptance_contract":acceptance, "teardown":False})
        FakeBackend.calls, FakeBackend.tasks, FakeBackend.observations, FakeBackend.results = [], {}, {}, {}
        FakeBackend.crash_stage = "build" if scenario == "pause-resume-recovery" else None
        FakeBackend.missing_stage = FakeBackend.bad_result = FakeBackend.bad_context = None
        FakeBackend.role_status, FakeBackend.mutate_review = "PASS", False
        FakeBackend.closed = FakeBackend.teardown_error = False
        CampaignBackend.polls = {}
        actions, recoveries, injected_losses = [], 0, 0
        original_interrupt = driver._Driver.functional_interrupt
        def interrupt(instance):
            if scenario == "functional-cancellation" and not actions:
                control.request(runs,mid,action="cancel",reason="synthetic campaign cancellation",idempotency_key="campaign-cancel")
                actions.append("cancel")
            return original_interrupt(instance)
        started = time.monotonic_ns()
        with mock.patch.object(driver.fleet_herdr,"HerdrBackend",CampaignBackend), \
             mock.patch.object(driver,"_archive",return_value=fleet_herdr_archive), \
             mock.patch.object(driver._Driver,"functional_interrupt",interrupt):
            if scenario == "pause-resume-recovery":
                try:
                    driver.supervise(runs,mid,seconds=30)
                except RuntimeError as exc:
                    if str(exc) != "process lost after submit":
                        raise
                    injected_losses += 1
                else:
                    raise AssertionError("expected injected post-send controller loss")
                FakeBackend.crash_stage = None
                control.request(runs,mid,action="pause",reason="synthetic recovery pause",idempotency_key="campaign-pause")
                actions.append("pause")
                paused = driver.supervise(runs,mid,seconds=30)
                assert paused["control"]["applied"] == "paused", paused
                recoveries += 1
                time.sleep(0.05)  # explicitly synthetic requested-pause interval
                control.request(runs,mid,action="resume",reason="synthetic resume",idempotency_key="campaign-resume")
                actions.append("resume")
                recoveries += 1
            result = driver.supervise(runs,mid,seconds=30)
        elapsed = (time.monotonic_ns()-started)/1e9
        assert result["status"] == expected, (scenario,result)
        report = fleet_report.build_report(runs,mid)
        events = state.read_events(state.ledger_path(runs,mid))
        sends = [c[2] for c in FakeBackend.calls if c[0]=="submit"]
        assert len(sends) == len(set(sends)), "physical prompt retry in synthetic backend"
        functional = report["functional"]
        if scenario == "functional-failure":
            assert functional["status"] == "failed" and len(sends)==4 and not report["archive"]["verified"]
        if scenario == "artifact-rejection":
            assert functional["status"] == "passed" and report["acceptance"]["status"] == "rejected"
        if expected == "succeeded":
            assert report["acceptance"]["status"] == "accepted" and functional["status"] == "passed"
        record = {"scenario":scenario,"fixture":"synthetic_roles_real_controller_and_docker","mission_id":mid,
            "runs_dir":str(runs),"expected":expected,"observed":result["status"],"matches_expected":True,
            "functional":functional["status"],"artifact_acceptance":report["acceptance"]["status"] if report["acceptance"] else None,
            "wall_seconds":elapsed,"latencies":report["timing"],"operator_actions":actions,
            "injected_controller_losses":injected_losses,"recovery_driver_calls":recoveries,
            "distinct_sent_prompts":len(set(sends)),"physical_prompt_retries":len(sends)-len(set(sends)),
            "supervisor_iterations_final":result["iterations"],"provider_calls":0,
            "model_quality":"NOT_VERIFIED","model_tokens":None,"model_cost_usd":None,
            "usage_reason":"synthetic roles; no model/provider billing observations"}
        save(case/"result.json",result)
        save(case/"report.json",report)
        save(case/"trace.json",{"authority":"observational_only","fixture":True,"spans":fleet_trace.events_to_spans(events)})
        save(case/"measurement.json",record)
        records.append(record)
        print(scenario,expected,"observed",result["status"],f"{elapsed:.3f}s",flush=True)
    walls=[r["wall_seconds"] for r in records]
    summary={"schema_version":1,"scope":"local component baseline, not model benchmark","runtime":spec["runtime"],
        "source_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "expected_outcomes":{"numerator":sum(r["matches_expected"] for r in records),"denominator":len(records)},
        "expected_acceptance":{"numerator":sum(r["artifact_acceptance"]=="accepted" for r in records),"denominator":2},
        "wall_seconds":{"min":min(walls),"median":statistics.median(walls),"max":max(walls),"samples":len(walls)},
        "provider_calls":0,"model_quality":"NOT_VERIFIED","model_cost_usd":None,
        "model_cost_reason":"no provider campaign; local Docker/host resource costs were not measured", "cases":records}
    save(output/"campaign.json",summary)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    summary=campaign(args.output)
    print(json.dumps({"outcomes":summary["expected_outcomes"],"provider_calls":0,"model_quality":"NOT_VERIFIED"}))


if __name__ == "__main__":
    main()
