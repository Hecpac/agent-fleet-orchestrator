#!/usr/bin/env python3
"""Derive a privacy-safe Mission report exclusively from durable evidence."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import stat
import sys
from typing import Any

import fleet_archive
import fleet_compiled
import fleet_json
import fleet_mission_state as mission_state
import fleet_trace


ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"succeeded", "blocked", "failed", "abandoned", "indeterminate"}
OUTCOMES = ("succeeded", "blocked", "failed", "indeterminate", "abandoned")


class ReportError(RuntimeError):
    """Durable report evidence is absent, malformed, or contradictory."""


def _timestamp(value: Any, where: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportError(f"invalid timestamp in {where}") from exc
    if parsed.tzinfo is None:
        raise ReportError(f"timestamp lacks timezone in {where}")
    return parsed.astimezone(timezone.utc)


def _seconds(start: datetime, end: datetime) -> float:
    return round(max(0.0, (end - start).total_seconds()), 6)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonl(path: Path, *, required: bool = False) -> list[dict[str, Any]]:
    if not path.exists():
        if required:
            raise ReportError(f"durable evidence is absent: {path.name}")
        return []
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ReportError(f"durable evidence must be a regular file: {path.name}")
    try:
        values = fleet_json.load_jsonl(
            path.read_bytes(),
            require_final_newline=True,
        )
    except fleet_json.FleetJSONError as exc:
        if "final newline" in str(exc):
            raise ReportError(f"partial durable JSONL at {path.name}") from exc
        raise ReportError(f"invalid durable JSONL at {path.name}: {exc}") from exc
    result: list[dict[str, Any]] = []
    for number, value in enumerate(values, 1):
        if not isinstance(value, dict):
            raise ReportError(f"durable JSONL row is not an object at {path.name}:{number}")
        result.append(value)
    return result


def _members(compiled: dict[str, Any]) -> dict[str, dict[str, Any]]:
    resolved = compiled["resolved"]
    result = {item["instance_id"]: item for item in resolved["instances"]}
    if resolved.get("lead"):
        result["lead"] = resolved["lead"]
    return result


def _run_records(
    legacy_events: list[dict[str, Any]],
    delegations: dict[str, dict[str, Any]],
    lead_result: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in legacy_events:
        run_id = event.get("run_id")
        if isinstance(run_id, str) and run_id:
            by_run[run_id].append(event)
    delegation_by_run = {item["run_id"]: item for item in delegations.values()}
    records: list[dict[str, Any]] = []
    for run_id, events in sorted(by_run.items()):
        first, latest = events[0], events[-1]
        stable: dict[str, set[str]] = defaultdict(set)
        for event in events:
            for field in ("feature", "instance", "provider", "model", "variant"):
                value = event.get(field)
                if value is not None and value != "":
                    stable[field].add(str(value))
        if any(len(values) > 1 for values in stable.values()):
            raise ReportError(f"legacy run identity drift: {run_id}")
        delegation = delegation_by_run.get(run_id, {})
        is_lead = bool(lead_result and lead_result.get("run_id") == run_id)
        provider = next(iter(stable["provider"]), "") or str(
            (lead_result if is_lead else delegation).get("provider", "")
        )
        model = next(iter(stable["model"]), "") or str(
            (lead_result if is_lead else delegation).get("model", "")
        )
        variant_values = stable["variant"]
        variant: str | None = next(iter(variant_values), None)
        if variant is None:
            raw_variant = (lead_result if is_lead else delegation).get("variant")
            variant = str(raw_variant) if raw_variant is not None else None
        start = _timestamp(
            first.get("dispatched_at") or first.get("timestamp"), f"legacy run {run_id} start"
        )
        end_value = latest.get("completed_at") or (
            latest.get("timestamp") if latest.get("status") in TERMINAL else None
        )
        end = _timestamp(end_value, f"legacy run {run_id} end") if end_value else None
        records.append(
            {
                "run_id": run_id,
                "instance": str(first.get("instance", "")),
                "provider": provider,
                "model": model,
                "variant": variant,
                "status": str(latest.get("status", "unknown")),
                "started_at": start,
                "ended_at": end,
                "duration_seconds": _seconds(start, end) if end else None,
                "prompt_tokens": int(latest.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(latest.get("completion_tokens", 0) or 0),
            }
        )
    return records


def _max_parallel(records: list[dict[str, Any]]) -> int:
    points: list[tuple[datetime, int]] = []
    for record in records:
        if record["ended_at"] is None:
            continue
        points.append((record["started_at"], 1))
        points.append((record["ended_at"], -1))
    current = maximum = 0
    for _, change in sorted(points, key=lambda item: (item[0], item[1])):
        current += change
        maximum = max(maximum, current)
    return maximum


def _human_wait(events: list[dict[str, Any]], end: datetime) -> float:
    starts: list[datetime] = []
    decision_starts: dict[str, datetime] = {}
    intervals: list[tuple[datetime, datetime]] = []
    for event in events:
        timestamp = _timestamp(event["timestamp"], "human wait event")
        if event["kind"] in {"assurance_requested", "human_approval_requested"}:
            starts.append(timestamp)
        elif event["kind"] == "assurance_approved" and starts:
            intervals.append((starts.pop(), timestamp))
        elif event["kind"] == "human_decision_requested":
            decision_starts[event["payload"]["decision_id"]] = timestamp
        elif event["kind"] == "human_decision_resolved":
            start = decision_starts.pop(event["payload"]["decision_id"], None)
            if start is not None:
                intervals.append((start, timestamp))
    intervals.extend((start, end) for start in starts)
    intervals.extend((start, end) for start in decision_starts.values())
    merged: list[tuple[datetime, datetime]] = []
    for start, stop in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append((start, stop))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
    return round(sum(_seconds(start, stop) for start, stop in merged), 6)


def _decision_metrics(decisions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    values = list(decisions.values())
    resolutions = [
        value["resolution"]
        for value in values
        if isinstance(value.get("resolution"), dict)
    ]
    return {
        "total": len(values),
        "pending": sum(value["status"] == "pending" for value in values),
        "resolved": sum(value["status"] == "resolved" for value in values),
        "human_resolved": sum(
            value["resolution_kind"] == "human" for value in resolutions
        ),
        "automatic_resolved": sum(
            value["resolution_kind"] == "automatic" for value in resolutions
        ),
    }


def _provider_metrics(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(record["provider"], record["model"], record["variant"])].append(record)
    result: list[dict[str, Any]] = []
    for (provider, model, variant), values in sorted(
        grouped.items(), key=lambda item: tuple(str(value) for value in item[0])
    ):
        outcomes = Counter(value["status"] for value in values)
        total = len(values)
        result.append(
            {
                "provider": provider,
                "model": model,
                "variant": variant,
                "runs": total,
                "outcomes": {name: outcomes[name] for name in OUTCOMES},
                "success_rate": round(outcomes["succeeded"] / total, 6),
                "prompt_tokens": sum(value["prompt_tokens"] for value in values),
                "completion_tokens": sum(value["completion_tokens"] for value in values),
                "cost_usd": None,
                "cost_reason": "no durable provider cost receipt",
            }
        )
    return result


def derive_report(
    events: list[dict[str, Any]],
    compiled: dict[str, Any],
    legacy_events: list[dict[str, Any]],
    *,
    archive: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not events or events[0].get("kind") != "mission_created":
        raise ReportError("report requires a mission_created root event")
    mission_id = str(events[0]["mission_id"])
    state = mission_state.derive_state(events)
    started = _timestamp(events[0]["timestamp"], "mission start")
    ended = _timestamp(events[-1]["timestamp"], "mission end")
    delegations = state["delegations"]
    records = _run_records(legacy_events, delegations, state.get("lead_result"))
    useful_events = [
        event for event in events if event["kind"] in {"result_recorded", "lead_result_recorded"}
    ]
    first_useful = (
        _timestamp(useful_events[0]["timestamp"], "first useful result")
        if useful_events
        else next(
            (record["ended_at"] for record in records if record["status"] == "succeeded"),
            None,
        )
    )
    members = _members(compiled)
    selected_ids = {
        delegation["recipient_instance"] for delegation in delegations.values()
    }
    if state.get("lead_run_id"):
        selected_ids.add("lead")
    selected = [
        {
            "instance_id": instance,
            "provider": member["provider"],
            "model": member["model"],
            "variant": member.get("variant"),
            "authority": member["authority"],
        }
        for instance, member in sorted(members.items())
        if instance in selected_ids
    ]
    omitted = [
        {"instance_id": instance, "reason": "no durable dispatch event"}
        for instance in sorted(set(members) - selected_ids)
    ]
    fan_out: Counter[str] = Counter(
        str(item.get("parent_run_id") or "mission") for item in delegations.values()
    )
    results_by_delegation = {
        item["delegation_id"]: item for item in state["results"].values()
    }
    challenge_verify = [
        item for item in delegations.values() if item["capability"] in {"challenge", "verify"}
    ]
    relayed_artifacts = {
        event["payload"]["artifact_id"]
        for event in events
        if event["kind"] == "result_relayed"
    }
    outcomes = Counter(record["status"] for record in records)
    run_total = len(records)
    return {
        "schema_version": 1,
        "mission_id": mission_id,
        "feature": state["feature"],
        "status": state["status"],
        "decision_authority": compiled["workflow"]["autonomy"]["owner"],
        "source": {
            "mission_events": len(events),
            "mission_head_sha256": events[-1]["event_sha256"],
            "legacy_events": len(legacy_events),
            "durable_only": True,
        },
        "timing": {
            "started_at": events[0]["timestamp"],
            "ended_at": events[-1]["timestamp"],
            "wall_time_seconds": _seconds(started, ended),
            "time_to_first_useful_result_seconds": (
                _seconds(started, first_useful) if first_useful else None
            ),
            "human_wait_seconds": _human_wait(events, ended),
        },
        "agents": {"selected": selected, "omitted": omitted},
        "delegation": {
            "count": len(delegations),
            "max_depth": max((item["depth"] for item in delegations.values()), default=0),
            "max_fan_out": max(fan_out.values(), default=0),
            "max_parallel_runs": _max_parallel(records),
        },
        "outcomes": {
            "runs": run_total,
            "counts": {name: outcomes[name] for name in OUTCOMES},
            "rates": {
                name: round(outcomes[name] / run_total, 6) if run_total else 0.0
                for name in OUTCOMES
            },
        },
        "decisions": _decision_metrics(state["decisions"]),
        "providers": _provider_metrics(records),
        "adoption": {
            "relayed_results": len(relayed_artifacts),
            "lead_result": 1 if state.get("lead_result") else 0,
        },
        "findings": {
            "challenge_verify_delegations": len(challenge_verify),
            "durable_result_sets": sum(
                item["delegation_id"] in results_by_delegation for item in challenge_verify
            ),
            "finding_count": None,
            "reason": "result contents are excluded from default metrics",
        },
        "recovery": {
            "durable_idempotency_keys": len({event["idempotency_key"] for event in events}),
            "resume_count": None,
            "replays_avoided": None,
            "reason": "attempts without a durable append are not observable",
        },
        "archive": archive or {
            "present": False, "verified": False, "bytes": 0, "entries": 0
        },
        "trace": {"spans": len(fleet_trace.events_to_spans(events))},
    }


def _archive_report(root: Path) -> dict[str, Any]:
    archive = root / "archive"
    if not archive.exists():
        return {"present": False, "verified": False, "bytes": 0, "entries": 0}
    try:
        verified = fleet_archive.verify_archive(archive)
        size = sum(
            path.stat().st_size for path in archive.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        return {
            "present": True,
            "verified": True,
            "bytes": size,
            "entries": verified["entries"],
            "content_policy": verified["content_policy"],
            "content_root_sha256": verified["content_root_sha256"],
        }
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        return {
            "present": True,
            "verified": False,
            "bytes": 0,
            "entries": 0,
            "error": type(exc).__name__,
        }


def build_report(runs_dir: Path, mission_id: str) -> dict[str, Any]:
    runs_dir = runs_dir.resolve()
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    root = mission_state.mission_root(runs_dir, mission_id)
    ledger = mission_state.ledger_path(runs_dir, mission_id)
    events = mission_state.read_events(ledger, expected_mission_id=mission_id)
    try:
        compiled = fleet_compiled.load(
            root / "compiled-workflow.json", mode="read"
        )
    except fleet_compiled.CompiledError as exc:
        raise ReportError(f"compiled workflow evidence is invalid: {exc}") from exc
    current = mission_state.derive_state(events)
    if (
        compiled["workflow_digest"] != current["workflow_digest"]
        or compiled["compiled_digest"] != current["compiled_digest"]
    ):
        raise ReportError("compiled workflow evidence is not bound to the mission ledger")
    feature = events[0]["payload"]["feature"]
    archive_report = _archive_report(root)
    legacy_path = runs_dir / f"fleet-{feature}.ledger.jsonl"
    legacy_source = "live"
    if not legacy_path.exists():
        archived_legacy = root / "archive" / "ledger.jsonl"
        if archive_report.get("verified") is True and archived_legacy.exists():
            legacy_path = archived_legacy
            legacy_source = "unified_archive"
        else:
            legacy_source = "none"
    legacy = _jsonl(legacy_path)
    report = derive_report(events, compiled, legacy, archive=archive_report)
    report["source"].update(
        {
            "mission_ledger_sha256": _sha256(ledger),
            "legacy_ledger_sha256": _sha256(legacy_path) if legacy_path.exists() else None,
            "legacy_ledger_source": legacy_source,
            "compiled_digest": compiled["compiled_digest"],
        }
    )
    return report


def human_report(report: dict[str, Any]) -> str:
    timing = report["timing"]
    delegation = report["delegation"]
    outcomes = report["outcomes"]["counts"]
    decisions = report["decisions"]
    archive = report["archive"]
    lines = [
        f"Mission {report['mission_id']} ({report['feature']}): {report['status']}",
        f"Authority: {report['decision_authority']} (provider comparisons are observational)",
        (
            f"Timing: wall={timing['wall_time_seconds']}s "
            f"first_useful={timing['time_to_first_useful_result_seconds']}s "
            f"human_wait={timing['human_wait_seconds']}s"
        ),
        (
            f"Delegation: count={delegation['count']} depth={delegation['max_depth']} "
            f"fan_out={delegation['max_fan_out']} parallel={delegation['max_parallel_runs']}"
        ),
        "Outcomes: " + " ".join(f"{name}={outcomes[name]}" for name in OUTCOMES),
        (
            f"Decisions: total={decisions['total']} pending={decisions['pending']} "
            f"resolved={decisions['resolved']} human={decisions['human_resolved']} "
            f"automatic={decisions['automatic_resolved']}"
        ),
        (
            f"Archive: present={str(archive['present']).lower()} "
            f"verified={str(archive['verified']).lower()} bytes={archive['bytes']}"
        ),
    ]
    for provider in report["providers"]:
        lines.append(
            f"Provider {provider['provider']}/{provider['model']}: "
            f"runs={provider['runs']} success_rate={provider['success_rate']} "
            f"tokens={provider['prompt_tokens'] + provider['completion_tokens']}"
        )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", default=str(ROOT / "orchestration" / "runs"))
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_report(Path(args.runs_dir), args.mission_id)
        if args.json:
            print(json.dumps(report, sort_keys=True))
        else:
            print(human_report(report))
        return 0
    except (
        ReportError,
        mission_state.MissionStateError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"fleet-report: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
