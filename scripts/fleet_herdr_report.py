"""Read-only Herdr report; admission, transport and observed identity stay distinct."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import fleet_artifacts
import fleet_functional
import fleet_herdr_archive
import fleet_herdr
import fleet_herdr_evidence
import fleet_herdr_rejection
import fleet_herdr_control
import fleet_herdr_metrics
import fleet_herdr_profile
import fleet_herdr_versions
import fleet_json
import fleet_mission_state
import fleet_safe_paths

OUTCOMES = ("succeeded", "blocked", "failed", "indeterminate", "abandoned", "pending")


def _seconds(start, end):
    if not start or not end:
        return None
    try:
        first, last = (datetime.fromisoformat(t.replace("Z", "+00:00")) for t in (start, end))
        if first.tzinfo is None or last.tzinfo is None:
            return None
        value = (last - first).total_seconds()
        return round(value, 6) if value >= 0 else None
    except (ValueError, TypeError, AttributeError):
        return None


def _covered_seconds(ranges):
    parsed = []
    for start, end in ranges:
        try:
            a, b = (datetime.fromisoformat(value.replace("Z", "+00:00"))
                    for value in (start, end))
        except (ValueError, TypeError, AttributeError):
            continue
        if a.tzinfo is not None and b.tzinfo is not None and b >= a:
            parsed.append((a.timestamp(), b.timestamp()))
    merged = []
    for start, end in sorted(parsed):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return (round(sum(end - start for start, end in merged), 6) if parsed else None), len(parsed)


def _archive(runs: Path, mid: str, names: list[str]) -> dict[str, Any]:
    if "herdr-archive" not in names:
        return {"state": "absent", "present": False, "verified": False,
                "anchored": None, "bytes": None, "bytes_reason": "no_herdr_archive", "reason": "no_herdr_archive"}
    try:
        proof = fleet_herdr_archive.verify(runs, mid, require_anchor=False)
        return {**proof, "state": "verified" if proof["valid"] else "unverifiable",
                "present": True, "verified": proof["valid"], "bytes": None, "bytes_reason": "archive_size_not_aggregated",
                "reason": None if proof["valid"] else "archive_not_anchored"}
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, AttributeError) as exc:
        cause = exc
        while cause.__cause__ is not None:
            cause = cause.__cause__
        missing = isinstance(cause, FileNotFoundError)
        return {"state": "unverifiable" if missing else "corrupt", "present": True,
                "verified": False, "anchored": None, "bytes": None, "bytes_reason": "archive_not_verified",
                "reason": str(exc)}


def build_report(runs: Path, current: dict[str, Any], compiled: dict[str, Any],
                 events: list[dict[str, Any]]) -> dict[str, Any]:
    mid = current["mission_id"]
    relative = Path("missions") / mid
    with fleet_safe_paths.RootedFS(runs) as fs:
        names = fs.list_directory(relative, directory_modes=(0o700, 0o700))
        options = fleet_json.loads(fs.read_regular(relative / "runtime-options.json",
            directory_modes=(0o700, 0o700), file_mode=0o600, max_bytes=16*1024*1024))
        creation = fleet_json.loads(fs.read_regular(relative / "creation-request.json",
            directory_modes=(0o700, 0o700), file_mode=0o600, max_bytes=16*1024*1024))
        backend = fleet_herdr_versions.read_state(fs, relative / "herdr-backend.json",
            mission_id=mid, compiled_digest=compiled["compiled_digest"])
    profile = fleet_herdr_profile.validate_profile_binding(compiled, options, current)
    fleet_herdr_profile.validate_creation_binding(compiled, creation.get("request"))
    is_capsule = (backend or {}).get("executor") == "fleet.mission.capsule.v2"
    runtime_contract = fleet_herdr_versions.state_contract(backend) if backend is not None and not is_capsule else None
    capsule_manifest = None
    if is_capsule:
        with fleet_safe_paths.RootedFS(runs) as fs:
            capsule_manifest = fleet_json.loads(fs.read_regular(relative / "runtime-options.json",
                directory_modes=(0o700, 0o700), file_mode=0o600, max_bytes=16*1024*1024))["herdr_capsule_manifest"]
    members = {m["instance_id"]: m for m in
               [compiled["resolved"].get("lead"), *compiled["resolved"]["instances"]]
               if m is not None}
    records = []
    sessions = set()
    groups = defaultdict(list)
    for admission in current["admissions"].values():
        run, role = admission["run_id"], admission["recipient_instance"]
        terminal = admission.get("terminal")
        outcome = terminal["status"] if admission["phase"] == "finalized" and terminal else "pending"
        submission = (backend or {}).get("submissions", {}).get(run)
        record = {"run_id": run, "instance_id": role, "request_key": admission["request_key"],
            "admission_id": admission["admission_id"], "admission_phase": admission["phase"],
            "status": outcome, "writer": admission["writer"],
            "requested": {"provider": members[role]["provider"], "model": members[role]["model"],
                          "source": "compiled_policy"},
            "observed": None, "observation_reason": "no_bound_completed_turn",
            "transport_status": submission.get("status") if isinstance(submission, dict) else None,
            "transport_reason": None if isinstance(submission, dict) else "no_transport_receipt",
            "started_at": None, "ended_at": None, "execution_seconds": None,
            "timing_reason": "no_bound_turn_timestamps",
            "prompt_tokens": None, "completion_tokens": None,
            "usage_reason": "turn_usage_not_normalized", "cost_usd": None,
            "cost_reason": "no_durable_billing_receipt",
            "result_disposition": "not_observed",
            "prepared_at": submission.get("prepared_at") if isinstance(submission, dict) else None,
            "submitted_at": submission.get("submitted_at") if isinstance(submission, dict) else None,
            "preparation_seconds": _seconds(submission.get("prepared_at"), submission.get("submitted_at"))
                if isinstance(submission, dict) else None}
        result_receipt = admission.get("result")
        result_pin = result_receipt.get("artifact_id") if isinstance(result_receipt, dict) else None
        disposition = "admitted_result" if result_pin else None
        if result_pin is None and isinstance(backend, dict) and isinstance(submission, dict):
            try:
                proof = fleet_herdr_rejection.load(runs, mid, run, backend)
                if proof is None:
                    proof = fleet_herdr_rejection.load_delivery(runs, mid, run, backend)
                if proof is None:
                    proof = fleet_herdr.load_result_rejection(runs, mid, run)
                if proof is not None:
                    result_pin = proof["observed_result_artifact_id"]
                    disposition = {"herdr_execution_evidence_rejection": "rejected_execution_evidence",
                                   fleet_herdr_rejection.DELIVERY_KIND: "rejected_delivery"}.get(
                                       proof["kind"], "rejected_role_protocol")
            except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
                record["observation_reason"] = f"invalid_rejection_evidence: {exc}"
        if disposition is not None:
            record["result_disposition"] = disposition
        if result_pin:
            try:
                read = lambda digest: fleet_artifacts.get_bytes(runs, mid, digest)
                result = fleet_json.loads(read(result_pin))
                evidence = result["evidence"]
                evidence_contract = evidence.get("runtime_contract")
                if evidence_contract is not None:
                    evidence_contract = fleet_herdr_versions.validate(evidence_contract)
                if evidence_contract != runtime_contract:
                    raise ValueError("Herdr report result runtime contract differs from validated backend")
                session = evidence["agent_session"]["value"]
                observed_provider = (result_receipt.get("provider") if isinstance(result_receipt, dict)
                                     else "openai")
                observed_model = (result_receipt.get("model") if isinstance(result_receipt, dict)
                                  else members[role]["model"])
                if (result["mission_id"] != mid or result["run_id"] != run or result["instance_id"] != role
                        or observed_provider != "openai"
                        or observed_model != members[role]["model"]
                        or evidence["agent_session"].get("kind") != "id"
                        or evidence["prompt_sha256"] != admission["task_sha256"]
                        or evidence["transcript_sha256"] != evidence["transcript_artifact_id"]):
                    raise ValueError("Herdr report result identity mismatch")
                transcript = read(evidence["transcript_artifact_id"])
                if is_capsule:
                    fleet_herdr_evidence.verify_result(result, read_artifact=read, role=role,
                        cwd=fleet_json.loads(read(evidence["capsule_launch_artifact_id"]))["candidate"]["realpath"],
                        prompt_sha256=admission["task_sha256"], capsule_manifest=capsule_manifest, current=current)
                inspect_transcript = (fleet_herdr_evidence.observe_rejected_transcript
                    if disposition in {"rejected_execution_evidence", "rejected_delivery"}
                    else fleet_herdr_evidence.verify_transcript)
                inspect_transcript(transcript, agent_session=session,
                    model=observed_model, turn_id=result["turn_id"],
                    prompt_sha256=admission["task_sha256"], final_bytes=read(result["artifact_id"]),
                    runtime_contract=runtime_contract, expected_provider="fleet-local" if is_capsule else "openai")
                observed = {"provider": "fleet-local" if is_capsule else "openai", "model": observed_model, "effort": "high",
                            "agent_session": session, "source": "bound_codex_transcript"}
                if is_capsule:
                    observed["provider_execution"] = fleet_json.loads(read(evidence["capsule_report_artifact_id"]))["provider_execution"]
                rows = fleet_json.load_jsonl(transcript)
                baseline = None
                baseline_id = evidence.get("usage_baseline_artifact_id")
                if baseline_id is not None:
                    if (not isinstance(submission, dict)
                            or submission.get("usage_baseline_artifact_id") != baseline_id):
                        raise ValueError("usage baseline differs from durable submission")
                    baseline = fleet_json.loads(read(baseline_id))
                    if (baseline.get("mission_id") != mid or baseline.get("run_id") != run
                            or baseline.get("prompt_sha256") != admission["task_sha256"]
                            or baseline.get("generation") != evidence.get("generation")
                            or baseline.get("agent_session") not in (None, evidence["agent_session"])):
                        raise ValueError("usage baseline identity mismatch")
                record.update(fleet_herdr_metrics.usage(rows, result["turn_id"], baseline,
                    baseline_frontier=evidence.get("usage_baseline_frontier")))
                bound = [r for r in rows if r.get("type") == "event_msg" and r["payload"].get("turn_id") == result["turn_id"]]
                for kind, field in (("task_started", "started_at"), ("task_complete", "ended_at")):
                    match = [r for r in bound if r["payload"].get("type") == kind]
                    record[field] = match[0].get("timestamp") if len(match) == 1 else None
                record["execution_seconds"] = _seconds(record["started_at"], record["ended_at"])
                if record["execution_seconds"] is not None:
                    record["timing_reason"] = None
                record.update(observed=observed, observation_reason=None,
                              result_disposition=disposition)
                sessions.add(session)
                groups[(observed["provider"], observed["model"])].append(record)
            except (ValueError, RuntimeError, OSError, KeyError, TypeError, AttributeError) as exc:
                record["observation_reason"] = f"invalid_or_unavailable_evidence: {exc}"
        records.append(record)
    counts = Counter(r["status"] for r in records)
    providers = [{"provider": provider, "model": model, "source": "bound_codex_transcript",
        "runs": len(values), "outcomes": {k: sum(r["status"] == k for r in values) for k in OUTCOMES},
        "prompt_tokens": None, "completion_tokens": None, "usage_reason": "turn_usage_not_normalized",
        "cost_usd": None, "cost_reason": "no_durable_billing_receipt"}
        for (provider, model), values in sorted(groups.items())]
    archive = _archive(runs, mid, names)
    for group in providers:
        matching = groups[(group["provider"], group["model"])]
        observed = [r for r in matching if r["prompt_tokens"] is not None and r["completion_tokens"] is not None]
        complete = len(observed) == len(matching)
        sources = {r.get("usage_source") for r in observed}
        group.update(prompt_tokens=sum(r["prompt_tokens"] for r in observed) if complete else None,
            completion_tokens=sum(r["completion_tokens"] for r in observed) if complete else None,
            usage_observed_runs=len(observed), usage_total_runs=len(matching),
            usage_source=(next(iter(sources)) if complete and len(sources) == 1
                          else "mixed_verified_runtime_counter_deltas" if complete else None),
            usage_reason=None if complete else "some_runs_lack_assignable_usage")
    observed_runs = sum(r["observed"] is not None for r in records)
    completed = current["status"] in fleet_mission_state.TERMINAL_STATUSES
    startup_attempts = [attempt for member in (backend or {}).get("members", [])
                        if isinstance(member, dict)
                        for attempt in member.get("start_attempts", [])
                        if isinstance(attempt, dict)]
    startup_seconds, startup_known = _covered_seconds(
        (attempt.get("started_at"), attempt.get("ready_at")) for attempt in startup_attempts)
    preparation_seconds, preparation_known = _covered_seconds(
        (record.get("prepared_at"), record.get("submitted_at")) for record in records)
    try:
        functional = fleet_functional.report(runs, mid, current)
    except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
        functional = {"required": True, "status": "unverifiable", "reason": str(exc)}
    return {"schema_version": 2, "backend": "herdr", "mission_id": mid,
        **({"control": fleet_herdr_control.view(current)} if "herdr_control" in current else {}),
        **({"functional": functional} if functional is not None else {}),
        "feature": current["feature"], "status": current["status"], "status_source": "mission_ledger",
        "herdr_profile": profile.profile_id,
        "source": {"mission_events": len(events), "mission_head_sha256": current["head_sha256"],
                   "compiled_digest": compiled["compiled_digest"], "durable_only": True},
        "admissions": {"count": len(records), "active": sum(a["active"] for a in current["admissions"].values())},
        "runs": records,
        "outcomes": {"runs": len(records), "definition": "distinct_admitted_run_ids",
            "counts": {k: counts[k] for k in OUTCOMES},
            "rates": {k: round(counts[k] / len(records), 6) if records else None for k in OUTCOMES}},
        "agents": {"requested": [{"instance_id": role, "model": m["model"], "provider": m["provider"],
                    "source": "compiled_policy"} for role, m in members.items()],
            "observed_sessions": len(sessions) if observed_runs == len(records) else None,
            "observed_sessions_lower_bound": len(sessions),
            "reason": None if observed_runs == len(records) else "some_runs_lack_observed_identity"},
        "delegation": {"count": len(current["delegations"]),
            "synthesis_turns": sum(e["kind"] == "synthesis_result_recorded" for e in events)},
        "providers": providers, "provider_observation": {"observed_runs": observed_runs,
            "unobserved_runs": len(records) - observed_runs},
        "timing": {"started_at": events[0]["timestamp"], "ended_at": events[-1]["timestamp"] if completed else None,
            "wall_time_seconds": _seconds(events[0]["timestamp"], events[-1]["timestamp"]) if completed else None,
            "controller_wait_seconds": None, "requested_pause_seconds": None,
            "startup_observed_seconds": startup_seconds,
            "startup_completed_attempts": startup_known,
            "startup_reason": ("startup_intervals_not_observed" if not startup_attempts else
                None if startup_known == len(startup_attempts) else "one_or_more_startup_intervals_are_unknown"),
            "preparation_observed_seconds": preparation_seconds,
            "preparation_completed_intervals": preparation_known,
            "preparation_reason": ("preparation_intervals_not_observed" if not records else
                None if preparation_known == len(records) else "one_or_more_preparation_intervals_are_unknown"),
            "reason": "no_complete_controller_and_pause_interval_evidence",
            **fleet_herdr_metrics.timing(current, events, records)},
        "archive": archive, "acceptance": archive.get("acceptance") if archive["verified"] else None,
        "acceptance_reason": None if archive["verified"] else "archive_not_verified"}
