#!/usr/bin/env python3
"""Decision-queue radar: show every tracked agent session and who is blocked.

Reads ~/.cmuxterm/*-hook-sessions.json (written by cmux agent hooks) and maps
workspace UUIDs to names via `cmux tree`. Sessions in needsInput are the ones
silently waiting on a human — surface them first, with age.

Usage: fleet_status.py [--max-age-hours N]   (default 24)
"""

import argparse
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
import uuid

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
    parser.add_argument(
        "--max-age-hours",
        type=positive_finite_hours,
        default=24.0,
    )
    parser.add_argument("--show-hints", action="store_true")
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    ws_names = workspace_names()
    inventory = manifest_inventory()
    now = time.time()
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

    if not rows:
        print("Sin sesiones de agente registradas en la ventana de tiempo.")
        return 0

    blocked = [r for r in rows if r["state"] == "needsInput" and r["alive"]]
    if blocked:
        print(f"⚠️  {len(blocked)} agente(s) BLOQUEADOS esperando decisión humana:\n")
    for r in rows:
        mark = {
            "needsInput": "⚠️ ",
            "auth": "🔐",
            "running": "▶️ ",
            "idle": "· ",
            "completed": "✓ ",
            "untracked": "○ ",
        }.get(r["state"], "? ")
        dead = " [proceso muerto]" if r["alive"] is False else ""
        age = f"{r['age_min']:.0f}m" if r["age_min"] is not None else "?"
        print(
            f"{mark}{r['state']:<10} {age:>5}  {r['instance']:<14} "
            f"{r['phase']:<10} {r['agent']:<9} {r['ws']:<22} {r['hint']}{dead}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
