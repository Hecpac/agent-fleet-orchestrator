"""Observed controller intervals and conservative Codex usage normalization."""
from __future__ import annotations

from datetime import datetime
import time
import uuid

import fleet_mission
import fleet_mission_state as state

KINDS = {"controller_operation", "controller_wait", "functional_execution", "supervisor_idle"}


def validate_payload(kind, payload):
    if kind == "herdr_interval_started":
        state._require_fields(kind, payload, {"interval_id", "kind", "run_id"})
        if payload["kind"] not in KINDS:
            raise state.MissionStateError("unsupported controller interval")
        if payload["run_id"] is not None:
            state._require_uuid(payload["run_id"], "observed run")
    else:
        state._require_fields(kind, payload, {"interval_id", "elapsed_ns", "outcome"})
        state._require_uint(payload["elapsed_ns"], "monotonic duration")
        if payload["outcome"] not in {"returned", "raised"}:
            raise state.MissionStateError("invalid interval outcome")
    state._require_uuid(payload["interval_id"], "interval identity")


def reduce(current, event):
    if event["actor"] != "CONTROL":
        raise state.MissionConflict("controller interval requires CONTROL")
    intervals = current.setdefault("herdr_intervals", {})
    payload = event["payload"]
    identifier = payload["interval_id"]
    if event["kind"] == "herdr_interval_started":
        if identifier in intervals:
            raise state.MissionConflict("interval identity cannot be reused")
        intervals[identifier] = {**payload, "started_at": event["timestamp"], "ended_at": None,
                                "elapsed_ns": None, "outcome": None}
    else:
        interval = intervals.get(identifier)
        if not interval or interval["ended_at"] is not None:
            raise state.MissionConflict("interval completion lacks its unique start")
        interval.update(ended_at=event["timestamp"], elapsed_ns=payload["elapsed_ns"], outcome=payload["outcome"])


def observe(runs, mid, kind, callback, run_id=None):
    identifier = str(uuid.uuid4())
    current = fleet_mission.load_state(runs, mid)
    if current["status"] in state.TERMINAL_STATUSES:
        return callback()
    state.append_event(runs, mid, kind="herdr_interval_started", actor="CONTROL",
        idempotency_key="herdr:interval:" + identifier,
        payload={"interval_id": identifier, "kind": kind, "run_id": run_id})
    started, outcome = time.monotonic_ns(), "raised"
    try:
        result = callback()
        outcome = "returned"
        return result
    finally:
        elapsed = time.monotonic_ns() - started
        if fleet_mission.load_state(runs, mid)["status"] not in state.TERMINAL_STATUSES:
            state.append_event(runs, mid, kind="herdr_interval_finished", actor="CONTROL",
                idempotency_key="herdr:interval-end:" + identifier,
                payload={"interval_id": identifier, "elapsed_ns": elapsed, "outcome": outcome})


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except ValueError:
        return None


def timing(current, events, runs):
    intervals = list(current.get("herdr_intervals", {}).values())
    instrumented = "herdr_control" in current or bool(intervals)
    result = {"scope": "observed_intervals; categories_can_overlap", "intervals": intervals,
        "open_intervals": sum(i["ended_at"] is None for i in intervals),
        "open_interval_reason": "missing_end_is_unknown_not_elapsed_wait" if any(i["ended_at"] is None for i in intervals) else None}
    for kind in KINDS:
        known = [i for i in intervals if i["kind"] == kind and i["elapsed_ns"] is not None]
        incomplete = any(i["kind"] == kind and i["elapsed_ns"] is None for i in intervals)
        measured = round(sum(i["elapsed_ns"] for i in known) / 1e9, 6) if instrumented else None
        result[kind + "_seconds"] = measured if not incomplete else None
        result[kind + "_observed_seconds"] = measured
        result[kind + "_reason"] = "one_or_more_intervals_are_unknown" if incomplete else None if instrumented else "not_instrumented"
        result[kind + "_completed_intervals"] = len(known)
    start = timestamp(events[0]["timestamp"])
    end = timestamp(events[-1]["timestamp"]) if current["status"] in state.TERMINAL_STATUSES else None
    pause_start, paused_start = None, None
    pause_ranges, paused_ranges = [], []
    for event in events:
        instant = timestamp(event["timestamp"])
        if event["kind"] == "herdr_control_requested":
            action = event["payload"]["action"]
            if action == "pause" and pause_start is None:
                pause_start = instant
            if action in {"resume", "cancel"}:
                if pause_start is not None:
                    pause_ranges.append((pause_start, instant))
                    pause_start = None
                if paused_start is not None:
                    paused_ranges.append((paused_start, instant))
                    paused_start = None
        elif event["kind"] == "herdr_control_applied" and event["payload"]["action"] == "pause":
            if paused_start is None:
                paused_start = instant
    if end is not None:
        if pause_start is not None:
            pause_ranges.append((pause_start, end))
        if paused_start is not None:
            paused_ranges.append((paused_start, end))
    result.update(requested_pause_seconds=round(sum(b-a for a,b in pause_ranges), 6) if instrumented and (end is not None or pause_start is None) else None,
        paused_seconds=round(sum(b-a for a,b in paused_ranges), 6) if instrumented and (end is not None or paused_start is None) else None,
        pause_interval_open=end is None and pause_start is not None,
        reason=None if instrumented else "no_complete_controller_and_pause_interval_evidence")
    # Partition only a terminal Mission window. Monotonic durations above are
    # measurement; timestamp intervals below are coverage, not additive totals.
    ranges = []
    for interval in intervals:
        a, b = timestamp(interval["started_at"]), timestamp(interval["ended_at"])
        if a is not None and b is not None and b >= a:
            ranges.append((a, b, interval["kind"]))
    for run in runs:
        a, b = timestamp(run.get("started_at")), timestamp(run.get("ended_at"))
        if a is not None and b is not None and b >= a:
            ranges.append((a, b, "agent_execution"))
    ranges.extend((a,b,"paused") for a,b in paused_ranges)
    if end is None or start is None or end < start:
        result.update(unclassified_seconds=None, classification_reason="Mission window is open or invalid", wall_partition=None)
        return result
    clipped = [(max(a,start),min(b,end),kind) for a,b,kind in ranges if max(a,start) < min(b,end)]
    points = sorted({start,end,*[v for a,b,_ in clipped for v in (a,b)]})
    order = ["functional_execution", "agent_execution", "controller_wait", "controller_operation", "supervisor_idle", "paused", "unknown"]
    totals = dict.fromkeys(order, 0.0)
    for a,b in zip(points, points[1:]):
        active = {kind for low,high,kind in clipped if low <= a and high >= b}
        selected = next((kind for kind in order if kind in active), "unknown")
        totals[selected] += b-a
    result.update(wall_partition={k:round(v,6) for k,v in totals.items()}, partition_priority=order,
        agent_intervals_outside_mission_window=sum(timestamp(r.get("ended_at")) is not None and
            (timestamp(r.get("ended_at")) < start or timestamp(r.get("started_at")) is not None and timestamp(r.get("started_at")) > end) for r in runs),
        unclassified_seconds=round(totals["unknown"],6), classification_reason="timestamp coverage; requested pause is an overlapping overlay")
    return result


def usage(rows, turn_id):
    unknown = {"prompt_tokens": None, "completion_tokens": None, "cached_input_tokens": None,
               "usage_source": None, "usage_reason": "no_assignable_turn_usage"}
    starts = [i for i,r in enumerate(rows) if r.get("type") == "event_msg" and r.get("payload", {}).get("type") == "task_started" and r["payload"].get("turn_id") == turn_id]
    ends = [i for i,r in enumerate(rows) if r.get("type") == "event_msg" and r.get("payload", {}).get("type") == "task_complete" and r["payload"].get("turn_id") == turn_id]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return unknown
    first, last = starts[0], ends[0]
    active_before = set()
    for row in rows[:first]:
        payload = row.get("payload", {})
        if row.get("type") != "event_msg":
            continue
        if payload.get("type") == "task_started":
            active_before.add(payload.get("turn_id"))
        elif payload.get("type") == "task_complete":
            active_before.discard(payload.get("turn_id"))
    if active_before:
        return {**unknown, "usage_reason": "overlapping_turn_usage"}
    if any(r.get("type") == "event_msg" and r.get("payload", {}).get("type") == "task_started" for r in rows[first+1:last]):
        return {**unknown, "usage_reason": "overlapping_turn_usage"}
    def counts(value):
        keys = ("input_tokens", "output_tokens", "cached_input_tokens")
        return {k:value[k] for k in keys} if isinstance(value,dict) and all(type(value.get(k)) is int and value[k]>=0 for k in keys) else None
    snapshots = []
    for index,row in enumerate(rows[:last]):
        p = row.get("payload", {})
        if row.get("type") == "event_msg" and p.get("type") == "token_count":
            info = p.get("info") or {}
            total = counts(info.get("total_token_usage"))
            if total is not None:
                snapshots.append((index,total,counts(info.get("last_token_usage"))))
    inside = [s for s in snapshots if s[0] > first]
    before = [s for s in snapshots if s[0] < first]
    if not inside:
        return unknown
    if before:
        baseline, source = before[-1][1], "prior_session_counter"
    elif inside[0][1] == inside[0][2] and not any(r.get("payload", {}).get("type") == "task_started" for r in rows[:first]):
        baseline, source = dict.fromkeys(inside[0][1],0), "first_total_equals_last_in_first_session_turn"
    else:
        return {**unknown, "usage_reason": "session_cumulative_usage_lacks_turn_baseline"}
    prior = baseline
    for _,total,_ in inside:
        if any(total[k] < prior[k] for k in prior):
            return {**unknown, "usage_reason": "usage_counter_reset_or_regression"}
        prior = total
    delta = {k:prior[k]-baseline[k] for k in prior}
    return {"prompt_tokens":delta["input_tokens"], "completion_tokens":delta["output_tokens"],
        "cached_input_tokens":delta["cached_input_tokens"], "usage_source":"bound_codex_transcript_cumulative_delta",
        "usage_baseline":source, "usage_reason":None, "usage_scope":"observed_runtime_counters_not_billing"}
