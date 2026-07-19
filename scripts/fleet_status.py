#!/usr/bin/env python3
"""Durable Decision Brief radar plus tracked CMUX agent session status.

Pending decisions come from hash-chained Mission ledgers and are shown before
advisory hook state. Invalid Mission ledgers remain visible and make the command
exit non-zero. Reading status never reconciles or appends Mission events.

Usage: fleet_status.py [--runs-dir PATH] [--json] [--max-age-hours N]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
import uuid

import fleet_decisions
import fleet_json
import fleet_manifest
import fleet_safe_paths


MAX_HOOK_SESSION_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
SAFE_HOOK_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}-hook-sessions\.json$")
SAFE_MANIFEST_FILE = re.compile(r"^fleet-([A-Za-z0-9][A-Za-z0-9._-]{0,63})\.manifest$")


def workspace_names() -> dict[str, str]:
    try:
        out = subprocess.run(
            ["cmux", "tree", "--all", "--id-format", "both"],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "CMUX_QUIET": "1"},
        ).stdout
    except Exception:
        return {}
    return {
        uuid: name
        for _, uuid, name in re.findall(
            r'workspace (workspace:\d+) ([0-9A-Fa-f-]{36}) "([^"]*)"', out
        )
    }


def pid_alive(pid: Any) -> bool:
    try:
        if isinstance(pid, bool):
            return False
        value = int(pid)
        if value < 1:
            return False
        os.kill(value, 0)
        return True
    except (OSError, TypeError, ValueError, OverflowError):
        return False


def _manifest_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    return Path(os.environ.get("FLEET_RUNS_DIR", root / "orchestration" / "runs"))


def manifest_inventory(runs_dir: Path | None = None) -> dict[str, dict[str, str]]:
    selected_root = Path(runs_dir) if runs_dir is not None else _manifest_root()
    inventory: dict[str, dict[str, str]] = {}
    ambiguous: set[str] = set()
    try:
        with fleet_safe_paths.RootedFS(selected_root) as rooted:
            names = rooted.list_directory("", directory_modes=())
            for name in names:
                match = SAFE_MANIFEST_FILE.fullmatch(name)
                if match is None:
                    continue
                try:
                    payload = rooted.read_regular(
                        name,
                        directory_modes=(),
                        file_mode=0o600,
                        max_bytes=MAX_MANIFEST_BYTES,
                        require_single_link=True,
                    )
                    manifest = fleet_manifest.normalize(
                        fleet_manifest.parse_bytes(payload)
                    )
                except (
                    fleet_manifest.ManifestError,
                    fleet_safe_paths.SafePathError,
                ):
                    continue
                feature = match.group(1)
                if manifest.get("feature") != feature:
                    continue
                for key, value in sorted(manifest.items()):
                    phase = manifest.get(f"{key}.phase")
                    if "." in key or not value.startswith("surface:") or not phase:
                        continue
                    raw_uuid = manifest.get(f"{key}.uuid")
                    try:
                        surface_uuid = str(uuid.UUID(str(raw_uuid))).upper()
                    except (ValueError, AttributeError):
                        continue
                    if surface_uuid in ambiguous:
                        continue
                    if surface_uuid in inventory:
                        inventory.pop(surface_uuid, None)
                        ambiguous.add(surface_uuid)
                        continue
                    inventory[surface_uuid] = {
                        "instance": key,
                        "phase": phase,
                        "feature": feature,
                        "workspace_uuid": manifest.get("workspace_uuid", ""),
                    }
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError:
        return {}
    return inventory


def _read_hook_file(
    rooted: fleet_safe_paths.RootedFS,
    name: str,
) -> bytes:
    last_error: fleet_safe_paths.SafePathError | None = None
    for mode in (0o600, 0o644):
        try:
            return rooted.read_regular(
                name,
                directory_modes=(),
                file_mode=mode,
                max_bytes=MAX_HOOK_SESSION_BYTES,
                require_single_link=True,
            )
        except fleet_safe_paths.SafePathError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def hook_sessions(home: Path) -> list[tuple[str, dict[str, Any]]]:
    records: list[tuple[str, dict[str, Any]]] = []
    try:
        with fleet_safe_paths.RootedFS(home) as rooted:
            for name in rooted.list_directory("", directory_modes=()):
                if not SAFE_HOOK_FILE.fullmatch(name):
                    continue
                try:
                    value = fleet_json.loads(_read_hook_file(rooted, name))
                except (
                    fleet_json.FleetJSONError,
                    fleet_safe_paths.SafePathError,
                ):
                    continue
                if type(value) is not dict:
                    continue
                sessions = value.get("sessions")
                if type(sessions) is not dict:
                    continue
                agent = name.removesuffix("-hook-sessions.json")
                for session_id in sorted(sessions):
                    session = sessions[session_id]
                    if type(session) is dict:
                        records.append((agent, session))
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError:
        return []
    return records


def normalized_state(state: str, hint: str) -> str:
    lowered = hint.lower()
    if "not logged in" in lowered or "login successful" in lowered:
        return "auth"
    if state == "running" and "waiting for your input" in lowered:
        return "needsInput"
    if state == "unknown" and "completed" in lowered:
        return "completed"
    return state


def positive_finite_hours(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--max-age-hours must be a finite positive number"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(
            "--max-age-hours must be a finite positive number"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", default=str(_manifest_root()))
    parser.add_argument(
        "--max-age-hours",
        type=positive_finite_hours,
        default=24.0,
    )
    parser.add_argument("--show-hints", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def _updated_at(value: Any) -> float | None:
    if value in (None, "", 0, 0.0):
        return None
    if isinstance(value, bool):
        raise ValueError("invalid hook timestamp")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError("invalid hook timestamp")
    return parsed


def _surface_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid hook surface")
    return str(uuid.UUID(value)).upper()


def _age_text(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _print_decisions(
    decisions: list[dict[str, Any]], invalid_missions: list[dict[str, Any]]
) -> None:
    if decisions:
        print(f"⚠️  {len(decisions)} DECISION BRIEF(S) PENDIENTES:")
        project_key: tuple[str, str] | None = None
        for decision in decisions:
            key = (decision["project"], decision["project_id"])
            if key != project_key:
                print(f"\nProyecto {key[0]} [{key[1][:12]}]")
                project_key = key
            affected = ",".join(decision["affected_instances"])
            print(
                f"  {decision['status']:<16} {_age_text(decision['age_seconds']):>6} "
                f"{decision['risk']}/{decision['impact']} {decision['feature']} "
                f"deadline={decision['deadline_at']} affected={affected}"
            )
            print(
                f"    mission={decision['mission_id']} decision={decision['decision_id']}"
            )
    if invalid_missions:
        if decisions:
            print()
        print(
            f"❌ {len(invalid_missions)} MISSION LEDGER(S) INVALID/UNREADABLE:"
        )
        for invalid in invalid_missions:
            mission_id = invalid["mission_id"] or "mission-store"
            print(f"  {mission_id}: {invalid['reason']}")


def _print_sessions(rows: list[dict[str, Any]]) -> None:
    blocked = [r for r in rows if r["state"] == "needsInput" and r["alive"]]
    if blocked:
        print(f"⚠️  {len(blocked)} agente(s) BLOQUEADOS esperando interacción humana:\n")
    for row in rows:
        mark = {
            "needsInput": "⚠️ ",
            "auth": "🔐",
            "running": "▶️ ",
            "idle": "· ",
            "completed": "✓ ",
            "untracked": "○ ",
        }.get(row["state"], "? ")
        dead = " [proceso muerto]" if row["alive"] is False else ""
        age = f"{row['age_min']:.0f}m" if row["age_min"] is not None else "?"
        print(
            f"{mark}{row['state']:<10} {age:>5}  {row['instance']:<14} "
            f"{row['phase']:<10} {row['agent']:<9} {row['ws']:<22} "
            f"{row['hint']}{dead}"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    runs_dir = Path(args.runs_dir)
    now = time.time()
    decision_inventory = fleet_decisions.decision_inventory(
        runs_dir,
        now=datetime.fromtimestamp(now, tz=timezone.utc),
    )
    ws_names = workspace_names()
    inventory = manifest_inventory(runs_dir)
    rows: list[dict[str, Any]] = []
    seen_surfaces: set[str] = set()
    home = Path(os.path.expanduser("~/.cmuxterm"))
    for agent, session in hook_sessions(home):
        try:
            updated = _updated_at(session.get("updatedAt"))
            surface = _surface_uuid(session.get("surfaceId"))
        except (TypeError, ValueError, OverflowError):
            continue
        age_min = (now - updated) / 60 if updated is not None else None
        if age_min is not None and age_min > args.max_age_hours * 60:
            continue
        meta = inventory.get(surface, {})
        raw_hint = session.get("lastBody") or session.get("lastSubtitle") or ""
        hint = raw_hint if isinstance(raw_hint, str) else ""
        hint = re.sub(r"\s+", " ", hint).strip()
        raw_state = session.get("agentLifecycle", "unknown")
        state = raw_state if isinstance(raw_state, str) else "unknown"
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", state):
            state = "unknown"
        state = normalized_state(state, hint)
        workspace_id = session.get("workspaceId", "")
        if not isinstance(workspace_id, str):
            workspace_id = ""
        seen_surfaces.add(surface)
        rows.append(
            {
                "agent": agent,
                "state": state,
                "ws": meta.get("feature") or ws_names.get(workspace_id, "(cerrado)"),
                "instance": meta.get("instance", "unmapped"),
                "phase": meta.get("phase", "?"),
                "age_min": age_min,
                "alive": pid_alive(session.get("pid", 0)),
                "hint": hint[:70] if args.show_hints else "",
            }
        )

    for surface, meta in inventory.items():
        if surface in seen_surfaces:
            continue
        rows.append(
            {
                "agent": "hookless",
                "state": "untracked",
                "ws": meta["feature"],
                "instance": meta["instance"],
                "phase": meta["phase"],
                "age_min": None,
                "alive": None,
                "hint": "",
            }
        )

    order = {
        "needsInput": 0,
        "auth": 1,
        "running": 2,
        "idle": 3,
        "completed": 4,
        "untracked": 5,
        "unknown": 6,
    }
    rows.sort(
        key=lambda row: (
            order.get(row["state"], 9),
            row["age_min"] if row["age_min"] is not None else 0,
            row["ws"],
            row["instance"],
            row["agent"],
        )
    )

    invalid_missions = decision_inventory["invalid_missions"]
    decisions = decision_inventory["decisions"]
    if args.json:
        payload = {
            "schema_version": 1,
            "authority": "mission_ledger",
            "read_only": True,
            "decisions": decisions,
            "invalid_missions": invalid_missions,
            "sessions": rows,
        }
        print(fleet_json.canonical_bytes(payload).decode("utf-8"))
        return 2 if invalid_missions else 0

    if not rows and not decisions and not invalid_missions:
        print("Sin sesiones de agente registradas en la ventana de tiempo.")
        return 0

    _print_decisions(decisions, invalid_missions)
    if (decisions or invalid_missions) and rows:
        print("\nSesiones CMUX (estado auxiliar):")
    _print_sessions(rows)
    return 2 if invalid_missions else 0


if __name__ == "__main__":
    sys.exit(main())
