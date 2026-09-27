#!/usr/bin/env python3
"""Retain reproducible owner-cycle fixtures; never launch Herdr, Docker or models."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fleet_herdr_owner_contract as contracts
import fleet_herdr_owner_cycle as cycle
import fleet_herdr_owner_runtime as runtime
import fleet_herdr_work_packet as work
import workflow_config
from scripts.fleet_herdr_campaign import git
from tests.test_fleet_herdr_owner_cycle import Backend
from tests.test_fleet_herdr_owner_functional import FunctionalBackend
from tests.test_fleet_functional import synthetic_spec
import fleet_functional_runner as runner
import fleet_mission_state as state


SCENARIOS = {"repair": "accepted_contract", "protocol-repair": "accepted_contract",
             "post-send-recovery": "accepted_contract", "pause-resume": "accepted_contract",
             "cancel": "cancelled", "decision": "accepted_contract",
             "physical-rejection": "exhausted", "deadline": "exhausted",
             "functional-repair": "accepted_contract", "functional-post-send": "accepted_contract",
             "functional-cancel": "cancelled"}


def save(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def run(output):
    output = Path(output)
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ValueError("requires a new absolute output directory")
    output.mkdir(parents=True, mode=0o700)
    compiled = workflow_config.compile_path(ROOT / "workflows/herdr-minimal-implementation.yaml")
    records = []
    for scenario, expected in SCENARIOS.items():
        case = output / scenario
        candidate = case / "candidate"
        candidate.mkdir(parents=True)
        (candidate / "README.md").write_text("Explicitly synthetic offline conformance fixture.\n")
        (candidate / ".gitignore").write_text("*.cache\n")
        (candidate / "preexisting.cache").write_text("must remain untouched\n")
        git(candidate, "init", "-q")
        git(candidate, "add", ".")
        git(candidate, "-c", "user.name=Offline Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "--no-gpg-sign", "-qm", "synthetic baseline")
        artifact = {"schema_version": 1, "requirements": [{"id": "answer", "description": "Include the agreed literal",
                    "checks": [{"kind": "text_contains", "path": "answer.txt", "expected": "implemented"}]}]}
        physical = {"schema_version": 1, "editable_paths": ["answer.txt"],
                    "temporary_directories": [".scratch"], "max_entries": 100, "max_bytes": 1048576}
        functional_spec = None
        if scenario.startswith("functional-"):
            physical["editable_paths"].append("sample_stats.py")
            functional_spec = synthetic_spec()
            functional_spec["runtime"]["controller_sha256"] = runner.controller_sha()
            functional_spec["runtime"]["guest_sha256"] = state.artifact_id(runner.GUEST.read_bytes())
        prepared = work.prepare(objective="Produce answer.txt containing implemented; preserve other files.",
            candidate_repo=candidate, base_sha=git(candidate, "rev-parse", "HEAD"), compiled=compiled,
            acceptance_contract=artifact, scope_contract=physical, work_context={}, functional_contract=functional_spec)
        contract = contracts.create(prepared, cycle_id=str(uuid.uuid4()), started_at=1000,
            budget={"max_attempts": 2, "deadline_seconds": 600, "token_limit": None, "cost_limit_usd": None},
            runtime={"cli": "codex", "cli_version": "0.155.1", "provider": "openai", "model": "gpt-6-astra", "effort": "max"})
        runs = case / "runs"
        runs.mkdir(mode=0o700)
        owner = cycle.Cycle.create(runs, contract)
        backend = (FunctionalBackend if functional_spec else Backend)(contract, candidate)
        backend.actions = {"repair": ["wrong", "good"], "protocol-repair": ["invalid", "good"],
                           "decision": ["decision", "good"], "physical-rejection": ["good", "good"]}.get(scenario, ["good"])
        trace = []
        if scenario == "functional-repair":
            backend.actions = ["good", "good"]
            backend.check_actions = ["failed", "passed"]
        if scenario == "functional-post-send":
            owner.tick(backend, now=1001)
            backend.check_crash = True
            try:
                owner.tick(backend, now=1002)
            except RuntimeError as exc:
                trace.append({"injected_loss": str(exc)})
            owner = cycle.Cycle(runs, owner.cycle_id, contract_sha256=owner.pin)
        if scenario == "functional-cancel":
            backend.check_active = True
            owner.tick(backend, now=1001)
            owner.tick(backend, now=1002)
            owner.control("cancel", request_id=str(uuid.uuid4()), target=runtime.resource(backend.sent[0]),
                          reason="synthetic functional cancellation", now=1003)
            trace.append(owner.tick(backend, now=1004))
            backend.check_active = False
            trace.append(owner.tick(backend, now=1005))
        if scenario == "post-send-recovery":
            backend.crash_after_send = True
            try:
                owner.tick(backend, now=1001)
            except RuntimeError as exc:
                trace.append({"injected_loss": str(exc)})
            owner = cycle.Cycle(runs, owner.cycle_id, contract_sha256=owner.pin)
        if scenario in {"pause-resume", "cancel", "deadline"}:
            backend.active = True
            trace.append(owner.tick(backend, now=1001))
            if scenario != "deadline":
                request = str(uuid.uuid4())
                owner.control("cancel" if scenario == "cancel" else "pause", request_id=request,
                              target=runtime.resource(backend.sent[0]), reason="synthetic control request", now=1002)
                trace.append(owner.tick(backend, now=1003))
                backend.active = False
                trace.append(owner.tick(backend, now=1004))
                if scenario == "pause-resume":
                    owner.resume(request, now=1005)
            else:
                trace.append(owner.tick(backend, now=1600))
                backend.active = False
                trace.append(owner.tick(backend, now=1601))
        if scenario == "physical-rejection":
            (candidate / "rogue.cache").write_text("undeclared residue\n")
        for step in range(20):
            if owner.load()["terminal"]:
                break
            result = owner.tick(backend, now=max(1010+step, owner.load()["last_at"]+1))
            trace.append(result)
            if result["status"] == "decision_required":
                owner.decide(result["request_id"], "Use the existing permitted wording.", now=owner.load()["last_at"]+1)
        result = owner.verify()
        if result["status"] != expected:
            raise AssertionError((scenario, expected, result))
        record = {"scenario": scenario, "expected": expected, "result": result,
                  "runs": str(runs), "cycle_id": owner.cycle_id, "contract_sha256": owner.pin,
                  "sends": len(backend.sent), "trace": trace,
                  "functional_starts": len(backend.checks) if functional_spec else 0,
                  "preexisting_ignored_preserved": (candidate / "preexisting.cache").read_text() == "must remain untouched\n"}
        save(case / "result.json", record)
        records.append(record)
    result = {"schema_version": 1, "lane": "offline-conformance", "synthetic_clock_and_transcripts": True,
              "provider_calls": 0, "live_execution": "NOT_VERIFIED", "semantic_success": "NOT_VERIFIED",
              "cost_usd": None, "cases": records, "passed": len(records)}
    save(output / "summary.json", result)
    return result


def verify_report(path):
    report = json.loads(Path(path).read_text())
    for record in report["cases"]:
        owner = cycle.Cycle(record["runs"], record["cycle_id"], contract_sha256=record["contract_sha256"])
        if owner.verify() != record["result"]:
            raise ValueError("retained archive differs from report")
    return {"offline_valid": True, "cases": len(report["cases"]), "live_execution": "NOT_VERIFIED"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path)
    group.add_argument("--verify", type=Path, help="read-only verification of a retained summary.json")
    args = parser.parse_args()
    result = run(args.output) if args.output else verify_report(args.verify)
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, sort_keys=True))


if __name__ == "__main__":
    main()
