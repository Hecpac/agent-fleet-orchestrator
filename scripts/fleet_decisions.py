#!/usr/bin/env python3
"""Durable human decision lifecycle helpers for Mission Control."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fleet_mission
import fleet_mission_state as mission_state


class DecisionError(RuntimeError):
    """A human decision operation violates its durable Mission binding."""


def _load_state(runs_dir: Path, mission_id: str) -> tuple[str, dict[str, Any]]:
    try:
        normalized = mission_state.normalize_uuid(mission_id, "mission_id")
        return normalized, fleet_mission.load_state(runs_dir, normalized)
    except mission_state.MissionStateError as exc:
        raise DecisionError(str(exc)) from exc


def _decision(current: dict[str, Any], decision_id: str) -> dict[str, Any]:
    try:
        decision_id = mission_state.normalize_uuid(decision_id, "decision_id")
    except mission_state.MissionStateError as exc:
        raise DecisionError(str(exc)) from exc
    value = current.get("decisions", {}).get(decision_id)
    if not isinstance(value, dict):
        raise DecisionError(f"unknown human decision: {decision_id}")
    return value


def _resolution_payload(
    decision: dict[str, Any],
    *,
    option_id: str,
    reason: str,
    resolution_kind: str,
) -> dict[str, Any]:
    payload = {
        "decision_id": decision["decision_id"],
        "request_event_sha256": decision["request_event_sha256"],
        "option_id": option_id,
        "reason": reason,
        "resolution_kind": resolution_kind,
    }
    try:
        mission_state.validate_decision_resolution_payload(payload)
    except mission_state.MissionStateError as exc:
        raise DecisionError(str(exc)) from exc
    return payload


def resolve_human(
    runs_dir: Path,
    mission_id: str,
    *,
    decision_id: str,
    option_id: str,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Append one exact HUMAN resolution and return the derived decision."""

    mission_id, current = _load_state(runs_dir, mission_id)
    decision = _decision(current, decision_id)
    payload = _resolution_payload(
        decision,
        option_id=option_id,
        reason=reason,
        resolution_kind="human",
    )
    try:
        event, appended = mission_state.append_event(
            runs_dir,
            mission_id,
            kind="human_decision_resolved",
            actor="HUMAN",
            idempotency_key=idempotency_key,
            payload=payload,
        )
    except mission_state.MissionStateError as exc:
        raise DecisionError(str(exc)) from exc
    _, refreshed = _load_state(runs_dir, mission_id)
    resolved = _decision(refreshed, decision_id)
    return {"event": event, "appended": appended, "decision": resolved}


def reconcile_expired(runs_dir: Path, mission_id: str) -> dict[str, Any]:
    """Lazily and durably resolve every eligible expired default decision."""

    mission_id, current = _load_state(runs_dir, mission_id)
    now = datetime.now(timezone.utc)
    # A human CLI can win the ledger lock after this snapshot but before the
    # automatic batch append. Re-read once on that exact conflict so normal
    # operator action never turns the next dispatch into a spurious failure.
    for attempt in range(2):
        requests: list[dict[str, Any]] = []
        try:
            for decision_id, decision in sorted(
                current.get("pending_decisions", {}).items()
            ):
                if not isinstance(decision, dict):
                    raise DecisionError("pending decision state is invalid")
                request = decision.get("request")
                if not isinstance(request, dict):
                    raise DecisionError("pending decision request is invalid")
                deadline = mission_state.parse_timestamp(
                    decision.get("deadline_at"), "decision deadline"
                )
                default_option = request.get("default_option_id")
                if (
                    deadline <= now
                    and request.get("risk") == "low"
                    and request.get("reversible") is True
                    and isinstance(default_option, str)
                ):
                    requests.append(
                        {
                            "kind": "human_decision_resolved",
                            "actor": "CONTROL",
                            "idempotency_key": f"decision:auto:{decision_id}",
                            "payload": _resolution_payload(
                                decision,
                                option_id=default_option,
                                reason="two-hour low-risk reversible default elapsed",
                                resolution_kind="automatic",
                            ),
                        }
                    )
        except mission_state.MissionStateError as exc:
            raise DecisionError(str(exc)) from exc
        if not requests:
            return {"mission_id": mission_id, "resolved": [], "appended": 0}
        try:
            results = mission_state.append_events(runs_dir, mission_id, requests)
        except mission_state.MissionConflict as exc:
            if attempt == 0:
                _, current = _load_state(runs_dir, mission_id)
                continue
            raise DecisionError(str(exc)) from exc
        except mission_state.MissionStateError as exc:
            raise DecisionError(str(exc)) from exc
        return {
            "mission_id": mission_id,
            "resolved": [
                request["payload"]["decision_id"] for request in requests
            ],
            "appended": sum(appended for _, appended in results),
        }
    raise DecisionError("decision reconciliation retry was exhausted")


def evidence_identity(
    current: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, Any]:
    result = current.get("results", {}).get(evidence["delegation_id"])
    if not isinstance(result, dict) or result.get("artifact_id") != evidence["artifact_id"]:
        raise DecisionError("decision evidence lineage is unavailable")
    return {
        "instance": evidence["instance"],
        "provider": result["provider"],
        "model": result["model"],
        "variant": result.get("variant"),
        "artifact_id": result["artifact_id"],
    }


def _identity_text(identity: dict[str, Any]) -> str:
    parts = [identity["provider"], identity["model"]]
    if identity.get("variant") is not None:
        parts.append(identity["variant"])
    return "/".join(str(part) for part in parts)


def format_brief(current: dict[str, Any], decision: dict[str, Any]) -> str:
    """Render one compact operator-facing Decision Brief."""

    request = decision["request"]
    recommendation = request["recommendation"]
    challenge = request["challenge"]
    recommendation_identity = evidence_identity(current, recommendation)
    challenge_identity = evidence_identity(current, challenge)
    lines = [
        f"DECISION {decision['decision_id']} [{request['impact'].upper()} / {request['risk'].upper()}]",
        request["title"],
        f"Pregunta: {request['question']}",
        f"Alcance afectado: {', '.join(request['affected_instances'])}",
        f"Reversible: {'sí' if request['reversible'] else 'no'}",
        "Opciones:",
    ]
    lines.extend(
        f"  {option['option_id']}: {option['label']} — {option['tradeoffs']}"
        for option in request["options"]
    )
    lines.extend(
        [
            "Recomendación: "
            f"{recommendation['option_id']} — {recommendation['rationale']}",
            "  evidencia: "
            f"{recommendation_identity['instance']} "
            f"({_identity_text(recommendation_identity)}) "
            f"{recommendation_identity['artifact_id']}",
            f"Challenge: {challenge['summary']}",
            "  evidencia: "
            f"{challenge_identity['instance']} "
            f"({_identity_text(challenge_identity)}) "
            f"{challenge_identity['artifact_id']}",
            f"Disenso: {request['dissent']}",
            f"Plazo: {decision['deadline_at']}",
            "Default tras 2h: "
            + (request["default_option_id"] or "ninguno; requiere decisión humana"),
            "Resolver: python3 scripts/fleet-decision.py --mission-id "
            f"{current['mission_id']} resolve --decision-id {decision['decision_id']} "
            "--option-id <id> --reason '<motivo>' --idempotency-key <clave>",
        ]
    )
    if decision["status"] == "resolved":
        resolution = decision["resolution"]
        lines.append(
            f"RESUELTA: {resolution['option_id']} por {resolution['actor']} "
            f"({resolution['resolution_kind']}) — {resolution['reason']}"
        )
    return "\n".join(lines)


def list_decisions(
    runs_dir: Path, mission_id: str, *, pending_only: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _, current = _load_state(runs_dir, mission_id)
    source = current["pending_decisions"] if pending_only else current["decisions"]
    return current, [source[key] for key in sorted(source)]
