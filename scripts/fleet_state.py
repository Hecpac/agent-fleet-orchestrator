#!/usr/bin/env python3
"""Durable phase gate for CONTROL → RECON → BUILD → CHALLENGE → VERIFY."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys


PHASE_ORDER = ["CONTROL", "RECON", "BUILD", "CHALLENGE", "VERIFY"]


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "advance", "check", "show"))
    parser.add_argument("manifest")
    parser.add_argument("value", nargs="?")
    parser.add_argument("--evidence")
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
    state["active_phase"] = requested
    state["history"].append({
        "phase": requested,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "evidence": args.evidence,
    })
    write_atomic(path, state)
    print(f"advanced {current} -> {requested}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
