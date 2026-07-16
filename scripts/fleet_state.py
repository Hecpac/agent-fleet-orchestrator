#!/usr/bin/env python3
"""Durable phase gate for CONTROL → RECON → BUILD → CHALLENGE → VERIFY."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import fleet_mission_state as mission_state


PHASE_ORDER = ["CONTROL", "RECON", "BUILD", "CHALLENGE", "VERIFY"]


class PhaseApprovalError(RuntimeError):
    """A BUILD-exit approval is absent, stale, or bound to another Mission."""


def manifest_values(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


def state_path(manifest_path: Path) -> Path:
    return manifest_path.with_suffix(".state.json")


def write_atomic(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def configured_phases(manifest: dict[str, str]) -> list[str]:
    phases = {value for key, value in manifest.items() if key.endswith(".phase")}
    phases.add("CONTROL")
    return [phase for phase in PHASE_ORDER if phase in phases]


def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_mission_approval(
    manifest_path: Path,
    manifest: dict[str, str],
    approval_event_sha256: str,
    *,
    now: datetime | None = None,
) -> dict:
    mission_id = manifest.get("mission_id", "")
    if not mission_id:
        raise PhaseApprovalError("manifest is not bound to a Mission")
    if manifest.get("mode") != "assured":
        raise PhaseApprovalError("Mission-bound BUILD approval requires mode=assured")
    if not mission_state.SHA256.fullmatch(approval_event_sha256):
        raise PhaseApprovalError("approval event reference must be SHA-256")
    try:
        events = mission_state.read_events(
            mission_state.ledger_path(manifest_path.parent, mission_id),
            expected_mission_id=mission_id,
        )
        current = mission_state.derive_state(events)
    except (mission_state.MissionStateError, OSError) as exc:
        raise PhaseApprovalError(f"cannot verify Mission approval: {exc}") from exc
    if current.get("status") != "assured_running":
        raise PhaseApprovalError("Mission is not in assured_running state")
    if current.get("feature") != manifest.get("feature"):
        raise PhaseApprovalError("Mission feature does not match the fleet manifest")
    mission_target_raw = current.get("target_repo")
    manifest_target_raw = manifest.get("target_repo")
    if not isinstance(mission_target_raw, str) or not mission_target_raw:
        raise PhaseApprovalError("Mission target repository is invalid")
    if not manifest_target_raw:
        raise PhaseApprovalError("fleet manifest target repository is missing")
    try:
        mission_target = Path(mission_target_raw).resolve()
        manifest_target = Path(manifest_target_raw).resolve()
    except (OSError, RuntimeError) as exc:
        raise PhaseApprovalError("Mission target repository is invalid") from exc
    if mission_target != manifest_target:
        raise PhaseApprovalError("Mission target does not match the fleet manifest")
    approval = current.get("approval")
    if not isinstance(approval, dict):
        raise PhaseApprovalError("Mission lacks a scoped assurance approval")
    if approval.get("event_sha256") != approval_event_sha256:
        raise PhaseApprovalError("approval event is not the active Mission approval")
    try:
        expires = datetime.fromisoformat(
            str(approval["expires_at"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError) as exc:
        raise PhaseApprovalError("Mission approval expiry is invalid") from exc
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current_time >= expires:
        raise PhaseApprovalError("Mission approval expired before BUILD exit")
    return approval


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "advance", "check", "show"))
    parser.add_argument("manifest")
    parser.add_argument("value", nargs="?")
    parser.add_argument("--evidence")
    parser.add_argument("--approved-by")
    parser.add_argument("--approval-event-sha256")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = manifest_values(manifest_path)
    path = state_path(manifest_path)

    if args.command == "init":
        if path.exists():
            print(f"state already exists: {path}", file=sys.stderr)
            return 2
        now = datetime.now(timezone.utc).isoformat()
        write_atomic(path, {
            "schema_version": 1,
            "feature": manifest.get("feature", ""),
            "active_phase": "CONTROL",
            "history": [{"phase": "CONTROL", "timestamp": now, "evidence": "fleet-created"}],
        })
        print(path)
        return 0

    if not path.exists():
        print(f"missing fleet state: {path}", file=sys.stderr)
        return 2
    state = load_state(path)
    if args.command == "show":
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0
    if args.command == "check":
        instance = args.value or ""
        phase = manifest.get(f"{instance}.phase")
        if not phase:
            print(f"unknown instance phase: {instance}", file=sys.stderr)
            return 2
        if manifest.get("mode", "guided") == "autonomous":
            print(f"autonomous mode open: {instance} ({phase})")
            return 0
        if phase != "CONTROL" and phase != state["active_phase"]:
            print(
                f"phase gate closed: {instance} is {phase}, active phase is {state['active_phase']}",
                file=sys.stderr,
            )
            return 3
        print(f"phase gate open: {instance} ({phase})")
        return 0

    requested = args.value or ""
    phases = configured_phases(manifest)
    current = state["active_phase"]
    try:
        expected = phases[phases.index(current) + 1]
    except (ValueError, IndexError):
        print(f"no phase follows {current}", file=sys.stderr)
        return 2
    if requested != expected:
        print(f"invalid transition: {current} -> {requested}; expected {expected}", file=sys.stderr)
        return 2
    if not args.evidence:
        print("phase transition requires --evidence <path|sha|gate-id>", file=sys.stderr)
        return 2
    mission_bound = bool(manifest.get("mission_id"))
    if current == "BUILD" and manifest.get("mode", "guided") != "autonomous":
        if mission_bound:
            if args.approved_by:
                print(
                    "Mission-bound BUILD exit rejects --approved-by text; "
                    "use --approval-event-sha256 <exact assurance_approved event>",
                    file=sys.stderr,
                )
                return 2
            if not args.approval_event_sha256:
                print(
                    "Mission-bound BUILD exit requires --approval-event-sha256 "
                    "<exact assurance_approved event>",
                    file=sys.stderr,
                )
                return 2
            try:
                validate_mission_approval(
                    manifest_path,
                    manifest,
                    args.approval_event_sha256,
                )
            except PhaseApprovalError as exc:
                print(f"Mission approval gate closed: {exc}", file=sys.stderr)
                return 3
        else:
            if args.approval_event_sha256:
                print(
                    "--approval-event-sha256 requires a Mission-bound manifest",
                    file=sys.stderr,
                )
                return 2
            if not args.approved_by:
                print(
                    "leaving BUILD requires --approved-by <operator-attestation>: "
                    "legacy guided fleets record a label, not cryptographic human presence",
                    file=sys.stderr,
                )
                return 2
    if current == "BUILD" and manifest.get("preset") == "fleet_dialogue":
        try:
            from fleet_dialogue_controller import ControllerError, accepted_build_gate

            accepted_build_gate(manifest_path.parent, manifest.get("feature", ""), manifest)
        except ControllerError as exc:
            print(f"FDP-2 BUILD gate closed: {exc}", file=sys.stderr)
            return 3
    if current == "CHALLENGE" and manifest.get("preset") == "fleet_dialogue":
        try:
            from fleet_assurance_controller import AssuranceError, challenge_phase_gate

            challenge_phase_gate(
                manifest_path.parent,
                manifest.get("feature", ""),
                manifest,
                args.evidence,
            )
        except AssuranceError as exc:
            print(f"FDP-3 CHALLENGE gate closed: {exc}", file=sys.stderr)
            return 3
    state["active_phase"] = requested
    entry = {
        "phase": requested,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "evidence": args.evidence,
    }
    if current == "BUILD" and mission_bound and args.approval_event_sha256:
        entry["approval_event_sha256"] = args.approval_event_sha256
    elif args.approved_by:
        entry["approved_by"] = args.approved_by
    state["history"].append(entry)
    write_atomic(path, state)
    print(f"advanced {current} -> {requested}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
