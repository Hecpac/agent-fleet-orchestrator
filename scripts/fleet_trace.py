#!/usr/bin/env python3
"""Derive non-authoritative hierarchical trace spans from mission evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import fleet_mission_state as mission_state


SPAN_KINDS = {
    "workflow_compiled": "workflow_compile",
    "risk_escalated": "risk_escalation",
    "lead_dispatched": "agent_run",
    "delegation_registered": "delegation",
    "result_relayed": "result_relay",
    "human_approval_requested": "human_wait",
    "assurance_requested": "assurance_gate",
    "archive_created": "archive",
    "worm_anchored": "worm_anchor",
}
SAFE_ATTRIBUTE_FIELDS = {
    "approval_id", "approval_event_sha256", "artifact_id", "base_sha",
    "capability", "categories", "compiled_digest", "delegation_id", "depth",
    "event_sha256", "final_sha", "instance", "mode", "model", "parent_run_id",
    "objective_sha256", "preset", "provider", "recipient_instance", "recipient_run_id", "risk",
    "run_id", "sequence", "sha256", "status", "variant", "workflow_digest",
}


def _safe_attributes(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        key: value
        for key, value in payload.items()
        if key in SAFE_ATTRIBUTE_FIELDS
        and (
            isinstance(value, (str, int, float, bool))
            or isinstance(value, list)
            and all(isinstance(item, (str, int, float, bool)) for item in value)
        )
    }


def events_to_spans(
    events: list[dict[str, Any]], audit_events: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    if not events:
        return []
    mission_id = events[0]["mission_id"]
    root_id = f"mission:{mission_id}"
    spans: list[dict[str, Any]] = [
        {
            "span_id": root_id,
            "parent_span_id": None,
            "name": "mission",
            "mission_id": mission_id,
            "sequence": events[0]["sequence"],
            "timestamp": events[0]["timestamp"],
            "end_timestamp": events[-1]["timestamp"],
            "attributes": {
                "feature": events[0]["payload"]["feature"],
                "workflow_digest": events[0]["payload"]["workflow_digest"],
            },
        }
    ]
    run_spans: dict[str, str] = {}
    for event in events[1:]:
        name = SPAN_KINDS.get(event["kind"])
        if name is None:
            continue
        payload = event["payload"]
        event_identity = event.get("event_id") or f"sequence-{event['sequence']}"
        span_id = f"event:{event_identity}"
        parent = root_id
        run_id = payload.get("run_id") if isinstance(payload, dict) else None
        parent_run_id = payload.get("parent_run_id") if isinstance(payload, dict) else None
        if parent_run_id and parent_run_id in run_spans:
            parent = run_spans[parent_run_id]
        if event["kind"] == "result_relayed" and payload.get("recipient_run_id") in run_spans:
            parent = run_spans[payload["recipient_run_id"]]
        spans.append(
            {
                "span_id": span_id,
                "parent_span_id": parent,
                "name": name,
                "mission_id": mission_id,
                "sequence": event["sequence"],
                "timestamp": event["timestamp"],
                "attributes": _safe_attributes(payload),
            }
        )
        if event["kind"] == "lead_dispatched" and run_id:
            run_spans[run_id] = span_id
        elif event["kind"] == "delegation_registered" and run_id:
            agent_span = f"run:{run_id}"
            spans.append(
                {
                    "span_id": agent_span,
                    "parent_span_id": span_id,
                    "name": "agent_run",
                    "mission_id": mission_id,
                    "sequence": event["sequence"],
                    "timestamp": event["timestamp"],
                    "attributes": {
                        key: value for key, value in _safe_attributes(payload).items()
                        if key in {
                            "run_id", "recipient_instance", "provider", "model", "variant",
                            "capability", "depth",
                        }
                    },
                }
            )
            run_spans[run_id] = agent_span
    for event in audit_events or []:
        if event.get("worm_compliance_mode") is not True:
            continue
        spans.append(
            {
                "span_id": f"worm:{event.get('event_id')}",
                "parent_span_id": root_id,
                "name": "worm_anchor",
                "mission_id": mission_id,
                "sequence": event.get("sequence"),
                "timestamp": event.get("timestamp"),
                "attributes": {
                    "event_sha256": event.get("event_sha256"),
                    "worm_backend": event.get("worm_backend"),
                    "worm_compliance_mode": event.get("worm_compliance_mode"),
                    "worm_trust_scope": event.get("worm_trust_scope"),
                    "worm_retention_mode": event.get("worm_retention_mode"),
                    "worm_object_key": event.get("worm_object_key"),
                },
            }
        )
    return spans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger")
    args = parser.parse_args(argv)
    try:
        events = mission_state.read_events(Path(args.ledger))
        print(json.dumps(events_to_spans(events), indent=2, sort_keys=True))
        return 0
    except mission_state.MissionStateError as exc:
        print(f"trace error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
