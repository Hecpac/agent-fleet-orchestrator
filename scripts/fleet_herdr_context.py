#!/usr/bin/env python3
"""Read-only Codex context observations; optional expiring Herdr UI badges.

Last-call usage is a proxy, not exact current occupancy, quality or authority.
No prompts, lifecycle transitions, config edits or admission decisions here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import uuid

MAX_BYTES = 64 * 1024 * 1024
SOURCE = "fleet-context"
TOKEN_KEYS = ("fleet_role", "fleet_context", "fleet_context_detail")


def unknown(reason):
    return dict(status="UNKNOWN", reason=reason, used_tokens=None,
                available_tokens=None, limit_tokens=None, used_percent=None,
                observed_at=None, age_seconds=None, pressure="UNKNOWN")


def stamp(value):
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.timestamp() if date.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def measure(rows, session_id, cwd, *, now, stale_after=300):
    """Never use cumulative token totals or cached tokens as context occupancy."""
    metas = [r.get("payload") for r in rows if r.get("type") == "session_meta"]
    if len(metas) != 1 or not isinstance(metas[0], dict):
        return unknown("missing_or_ambiguous_session")
    if metas[0].get("id") != session_id or metas[0].get("cwd") != cwd:
        return unknown("session_or_cwd_mismatch")
    last, context, turn_id, compactions = None, {}, None, 0
    for row in rows:
        p = row.get("payload", {})
        if not isinstance(p, dict):
            return unknown("invalid_event")
        kind = p.get("type") if row.get("type") == "event_msg" else None
        if row.get("type") == "compacted" or kind == "context_compacted":
            last = None
            compactions += 1
        elif kind == "task_started":
            last = None
            turn_id = p.get("turn_id")
        elif row.get("type") == "turn_context":
            if context and (context.get("model"), context.get("turn_id")) != (p.get("model"), p.get("turn_id")):
                last = None
            context = p
        elif kind == "token_count" and p.get("info") is not None:
            last = row
    if last is None:
        return unknown("awaiting_usage_after_start_or_compaction")
    if not context or context.get("turn_id") != turn_id:
        return unknown("usage_turn_unbound")
    info = last["payload"]["info"]
    if not isinstance(info, dict) or not isinstance(info.get("last_token_usage"), dict):
        return unknown("missing_last_call_usage")
    usage = info["last_token_usage"]
    values = [usage.get(k) for k in ("input_tokens", "output_tokens", "total_tokens")]
    limit = info.get("model_context_window")
    if any(type(v) is not int or v < 0 for v in values) or type(limit) is not int or limit <= 0:
        return unknown("invalid_usage_or_window")
    inputs, outputs, used = values
    if inputs + outputs != used:
        return unknown("inconsistent_last_call_total")
    observed = stamp(last.get("timestamp"))
    if observed is None or observed > now + 5:
        return unknown("invalid_observation_time")
    age = max(0, now - observed)
    percent = 100 * used / limit
    return dict(status="STALE" if age > stale_after else "OBSERVED", reason=None,
                used_tokens=used, available_tokens=max(0, limit-used), limit_tokens=limit,
                used_percent=round(percent, 2), input_tokens=inputs, output_tokens=outputs,
                observed_at=last["timestamp"], age_seconds=round(age, 1),
                pressure="EXCEEDED" if percent > 100 else "HIGH" if percent >= 85 else "WATCH" if percent >= 70 else "NORMAL",
                model=context.get("model"), effort=context.get("effort"), turn_id=turn_id,
                compactions_observed=compactions)


class Reader:
    def __init__(self):
        self.cache = {}

    def read(self, home, session_id, cwd, *, now, stale_after):
        try:
            if str(uuid.UUID(session_id)) != session_id:
                return unknown("invalid_session_id")
            root = (Path(home) / "sessions").resolve(strict=True)
            paths = list(root.glob(f"*/*/*/*{session_id}.jsonl"))
            if len(paths) != 1:
                return unknown("missing_or_ambiguous_transcript")
            path = paths[0]
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                return unknown("transcript_path_alias")
            stat = path.stat()
            if stat.st_size > MAX_BYTES or not path.is_file():
                return unknown("transcript_size_or_type")
            key = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)
            rows = self.cache.get(key)
            if rows is None:
                with path.open("rb") as handle:
                    raw = handle.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
                    return unknown("transcript_incomplete_or_oversized")
                rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
                if any(not isinstance(r, dict) for r in rows):
                    return unknown("invalid_transcript_row")
                # Keep one version per path, never an old counter after a rewrite.
                self.cache = {k:v for k,v in self.cache.items() if k[0] != str(path)}
                self.cache[key] = rows
            return measure(rows, session_id, cwd, now=now, stale_after=stale_after)
        except (OSError, ValueError, TypeError, KeyError):
            return unknown("transcript_unavailable_or_invalid")


def command(config, args):
    result = subprocess.run(config["command"] + args, env=config["environment"],
                            capture_output=True, text=True, timeout=5, check=False)
    if result.returncode:
        raise ValueError("herdr_command_failed")
    # Herdr's report-metadata CLI succeeds silently. Publication is checked
    # through pane.get below; an empty read response is still an error.
    if not result.stdout.strip() and args[:2] == ["pane", "report-metadata"]:
        return {}
    payload = json.loads(result.stdout)
    return payload["result"]


def live_binding(agent):
    session = agent.get("agent_session") or {}
    if (agent.get("agent") != "codex" or session.get("agent") != "codex"
            or session.get("source") != "herdr:codex" or session.get("kind") != "id"):
        return None
    return (agent.get("name"), agent.get("pane_id"), session.get("value"), agent.get("cwd"))


def snapshot(config, roster, reader, *, now, stale_after=300, run=command):
    try:
        agents = run(config, ["agent", "list"])["agents"]
        if not isinstance(agents, list):
            raise ValueError("invalid agent list")
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return [dict(name=m["name"], label=m["label"], **unknown("live_binding_unavailable")) for m in roster]
    result = []
    for member in roster:
        matches = [a for a in agents if isinstance(a, dict) and a.get("name") == member["name"]]
        agent = matches[0] if len(matches) == 1 else {}
        binding = live_binding(agent)
        item = dict(name=member["name"], label=member["label"])
        if not binding or agent.get("cwd") != member["cwd"]:
            item.update(unknown("live_agent_or_cwd_mismatch"))
        else:
            item.update(reader.read(member["codex_home"], binding[2], binding[3], now=now, stale_after=stale_after))
            item.update(binding=list(binding), agent_status=agent.get("agent_status"))
        result.append(item)
    return result


def compact(number):
    return f"{number/1000:.1f}k" if number >= 1000 else str(number)


def badge(item):
    if item["used_tokens"] is None:
        return "ctx ? SIN DATO", item["reason"]
    suffix = " ANT" if item["status"] == "STALE" else ""
    pressure = {"HIGH":" ALTO", "WATCH":" AVISO", "EXCEEDED":" EXCESO", "NORMAL":""}[item["pressure"]]
    return (f"ctx~{item['used_percent']:.0f}%{pressure}{suffix}",
            f"~{item['used_tokens']/1000:.0f}/{item['limit_tokens']/1000:.0f}k L{item['available_tokens']/1000:.0f}k")


def publish(config, item, *, run=command, ttl_ms=15000):
    """Presentation only. Recheck live occupant; old badges expire without us."""
    binding = item.get("binding")
    if not binding:
        return False
    current = run(config, ["agent", "get", item["name"]])["agent"]
    if live_binding(current) != tuple(binding):
        return False
    title, detail = badge(item)
    args = ["pane", "report-metadata", binding[1], "--source", SOURCE,
            "--agent", "codex", "--applies-to-source", "herdr:codex",
            "--seq", str(time.time_ns()), "--ttl-ms", str(ttl_ms)]
    for key, value in zip(TOKEN_KEYS, (item["label"], title, detail)):
        args.extend(["--token", key + "=" + value])
    run(config, args)
    observed = run(config, ["pane", "get", binding[1]])["pane"]
    return (observed.get("pane_id") == binding[1]
            and observed.get("agent_session", {}).get("value") == binding[2]
            and observed.get("cwd") == binding[3]
            and all(observed.get("tokens", {}).get(k) == v
                    for k, v in zip(TOKEN_KEYS, (item["label"], title, detail))))


def load_inputs(config_path, roster_path):
    config = json.loads(Path(config_path).read_text())
    cmd = config.get("command")
    if (not isinstance(cmd, list) or len(cmd) != 3 or cmd[1] != "--session"
            or not all(isinstance(v, str) and v for v in cmd)
            or not Path(cmd[0]).is_absolute() or not isinstance(config.get("environment"), dict)):
        raise ValueError("explicit herdr executable/session/environment required")
    roster = json.loads(Path(roster_path).read_text())
    if roster.get("schema_version") != 1 or not isinstance(roster.get("members"), list) or not 1 <= len(roster["members"]) <= 16:
        raise ValueError("roster requires 1 to 16 members")
    members = roster["members"]
    if len({m["name"] for m in members}) != len(members):
        raise ValueError("duplicate role binding")
    for m in members:
        if any(not isinstance(m.get(k), str) or not m[k] or any(ord(c)<32 for c in m[k]) for k in ("name", "label", "cwd", "codex_home")):
            raise ValueError("invalid roster member")
        if len(m["label"]) > 32 or not Path(m["cwd"]).is_absolute() or not Path(m["codex_home"]).is_absolute():
            raise ValueError("absolute cwd and codex_home required")
    return config, members


def display(items, *, ansi=False):
    def styled(text, code):
        return f"\033[{code}m{text}\033[0m" if ansi else text
    print(styled("  CONTEXTO DE LOS AGENTES", "1;97"))
    print("  Última llamada registrada · estimación de uso, no de calidad\n")
    for item in items:
        color = "1;91" if item["pressure"] in {"HIGH", "EXCEEDED"} else "1;93" if item["pressure"] == "WATCH" else "1;96"
        print(styled("  " + item["label"], "1;97"))
        if item["used_tokens"] is None:
            print(styled("  ? SIN DATO — " + item["reason"], "1;93"))
        else:
            percent = item["used_percent"]
            blocks = min(30, max(0, round(percent * .3)))
            bar = "█" * blocks + "░" * (30-blocks)
            pressure = " — ALTO: preparar relevo" if item["pressure"] in {"HIGH", "EXCEEDED"} else ""
            print(styled(f"  [{bar}]  {percent:.1f}% usado{pressure}", color))
            print(f"  Usado: {item['used_tokens']:,}   Límite: {item['limit_tokens']:,}   Disponible aprox.: {item['available_tokens']:,} tokens")
            age = int(item['age_seconds']//60)
            print(f"  Última medida: hace {age} min" + (" · DATO ANTERIOR" if item['status']=='STALE' else ""))
        if item.get('continuity'):
            print("  Memoria: " + item['continuity'])
        print()
    print("  Actualiza cada 5 s. Las medidas cambian tras una llamada del agente.")
    if any(item.get('continuity') for item in items):
        print("  Este panel solo observa; el controlador separado realiza los relevos.")
    print("  Ctrl+C detiene este panel. La observación tiene una duración limitada.", flush=True)


def attach_continuity(items, directory):
    """Read a controller journal for display only; never trigger lifecycle here."""
    if not directory:
        return
    try:
        root = Path(directory)
        if not root.is_absolute() or root.resolve(strict=True) != root:
            raise ValueError('alias')
        head = root/'head.json'
        if head.is_symlink() or head.stat().st_size > 1024:
            raise ValueError('head')
        sha = json.loads(head.read_bytes())['sha256']
        if not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):
            raise ValueError('digest')
        path = root/'objects'/sha
        if path.resolve(strict=True)!=path or path.stat().st_size>1024*1024:
            raise ValueError('object')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:
            raise ValueError('modified')
        state=json.loads(raw)
        phases={'checkpoint_sent':'guardando resumen', 'prepared':'resumen listo',
                'new_sent':'abriendo sesión', 'status_sent':'comprobando sesión', 'restore_sent':'recuperando memoria',
                'lead_pending':'esperando al Lead', 'lead_sent':'informando al Lead', 'blocked':'requiere atención'}
        for item in items:
            role=state.get('roles',{}).get(item['name'],{})
            active=state.get('active') or {}
            if active.get('member')==item['name']:
                item['continuity']=phases.get(active.get('phase'),'sin confirmar')
            elif role.get('generation',0)>0:
                expected=role.get('binding',{})
                if item.get('binding')==[expected.get(k) for k in ('name','pane','session','cwd')]:
                    item['continuity']=f"generación {role['generation']} · recuperada y recibida por Lead"
                else:
                    item['continuity']='sin memoria verificada para esta sesión'
            elif role.get('milestone_sha256'):
                item['continuity']='hito guardado · sesión original'
            else:
                item['continuity']='sin hito registrado'
    except (OSError,ValueError,TypeError,KeyError):
        for item in items:
            item['continuity']='SIN DATO VERIFICABLE'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-config", required=True)
    parser.add_argument("--roster", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--publish", action="store_true", help="publish expiring sidebar tokens only")
    parser.add_argument("--continuity-dir", help="read-only continuity journal for the panel")
    parser.add_argument("--watch-seconds", type=int, default=0, help="bounded foreground observation, max 3600")
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--stale-after", type=int, default=300)
    args = parser.parse_args(argv)
    if not 0 <= args.watch_seconds <= 3600 or not math.isfinite(args.interval) or not 1 <= args.interval <= 5 or args.stale_after < 1:
        parser.error("invalid observation budget, interval or freshness limit")
    try:
        config, roster = load_inputs(args.session_config, args.roster)
        reader = Reader()
        deadline = time.monotonic() + args.watch_seconds
        while True:
            items = snapshot(config, roster, reader, now=time.time(), stale_after=args.stale_after)
            attach_continuity(items, args.continuity_dir)
            if args.publish:
                for item in items:
                    try:
                        item["published"] = publish(config, item)
                    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                        item["published"] = False
            report = dict(schema_version=1, observed_at=datetime.now(timezone.utc).isoformat(),
                          metric="last_call_input_plus_output_over_effective_window",
                          authority="none", agents=items)
            if args.json:
                print(json.dumps(report, ensure_ascii=False), flush=True)
            else:
                if args.watch_seconds and sys.stdout.isatty():
                    print("\033[2J\033[H", end="")
                display(items, ansi=sys.stdout.isatty())
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(args.interval, remaining))
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Context observer unavailable ({type(exc).__name__}); no agent action taken.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
