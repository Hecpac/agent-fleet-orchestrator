#!/usr/bin/env python3
"""Decision-queue radar: show every tracked agent session and who is blocked.

Reads ~/.cmuxterm/*-hook-sessions.json (written by cmux agent hooks) and maps
workspace UUIDs to names via `cmux tree`. Sessions in needsInput are the ones
silently waiting on a human — surface them first, with age.

Usage: fleet_status.py [--max-age-hours N]   (default 24)
"""

import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def workspace_names() -> dict:
    try:
        out = subprocess.run(
            ["cmux", "tree", "--all", "--id-format", "both"],
            capture_output=True, text=True, timeout=10,
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


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def manifest_inventory() -> dict[str, dict]:
    root = Path(__file__).resolve().parents[1]
    runs_dir = Path(os.environ.get("FLEET_RUNS_DIR", root / "orchestration" / "runs"))
    inventory: dict[str, dict] = {}
    for path in runs_dir.glob("fleet-*.manifest"):
        try:
            manifest = dict(
                line.split("=", 1)
                for line in path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
        except OSError:
            continue
        for key, value in manifest.items():
            if not value.startswith("surface:") or not manifest.get(f"{key}.uuid"):
                continue
            inventory[manifest[f"{key}.uuid"].upper()] = {
                "instance": key,
                "phase": manifest.get(f"{key}.phase", "?"),
                "feature": manifest.get("feature", path.stem),
                "workspace_uuid": manifest.get("workspace_uuid", ""),
            }
    return inventory


def normalized_state(state: str, hint: str) -> str:
    lowered = hint.lower()
    if "not logged in" in lowered or "login successful" in lowered:
        return "auth"
    if state == "running" and "waiting for your input" in lowered:
        return "needsInput"
    if state == "unknown" and "completed" in lowered:
        return "completed"
    return state


def main() -> int:
    max_age_h = 24.0
    if "--max-age-hours" in sys.argv:
        max_age_h = float(sys.argv[sys.argv.index("--max-age-hours") + 1])

    ws_names = workspace_names()
    inventory = manifest_inventory()
    show_hints = "--show-hints" in sys.argv
    now = time.time()
    rows = []
    seen_surfaces: set[str] = set()
    home = os.path.expanduser("~/.cmuxterm")
    for f in glob.glob(f"{home}/*-hook-sessions.json"):
        agent = os.path.basename(f).replace("-hook-sessions.json", "")
        try:
            d = json.load(open(f))
        except Exception:
            continue
        for s in d.get("sessions", {}).values():
            updated = float(s.get("updatedAt") or 0)
            age_min = (now - updated) / 60 if updated else None
            if age_min is not None and age_min > max_age_h * 60:
                continue
            surface = (s.get("surfaceId") or "").upper()
            meta = inventory.get(surface, {})
            hint = s.get("lastBody") or s.get("lastSubtitle") or ""
            state = normalized_state(s.get("agentLifecycle", "unknown"), hint)
            seen_surfaces.add(surface)
            rows.append({
                "agent": agent,
                "state": state,
                "ws": meta.get("feature") or ws_names.get(s.get("workspaceId", ""), "(cerrado)"),
                "instance": meta.get("instance", "unmapped"),
                "phase": meta.get("phase", "?"),
                "age_min": age_min,
                "alive": pid_alive(s.get("pid", 0)),
                "hint": hint[:70] if show_hints else "",
            })

    for surface, meta in inventory.items():
        if surface in seen_surfaces:
            continue
        rows.append({
            "agent": "hookless",
            "state": "untracked",
            "ws": meta["feature"],
            "instance": meta["instance"],
            "phase": meta["phase"],
            "age_min": None,
            "alive": None,
            "hint": "",
        })

    order = {"needsInput": 0, "auth": 1, "running": 2, "idle": 3, "completed": 4, "untracked": 5, "unknown": 6}
    rows.sort(key=lambda r: (order.get(r["state"], 9), r["age_min"] or 0))

    if not rows:
        print("Sin sesiones de agente registradas en la ventana de tiempo.")
        return 0

    blocked = [r for r in rows if r["state"] == "needsInput" and r["alive"]]
    if blocked:
        print(f"⚠️  {len(blocked)} agente(s) BLOQUEADOS esperando decisión humana:\n")
    for r in rows:
        mark = {"needsInput": "⚠️ ", "auth": "🔐", "running": "▶️ ", "idle": "· ", "completed": "✓ ", "untracked": "○ "}.get(r["state"], "? ")
        dead = " [proceso muerto]" if r["alive"] is False else ""
        age = f"{r['age_min']:.0f}m" if r["age_min"] is not None else "?"
        print(
            f"{mark}{r['state']:<10} {age:>5}  {r['instance']:<14} "
            f"{r['phase']:<10} {r['agent']:<9} {r['ws']:<22} {r['hint']}{dead}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
