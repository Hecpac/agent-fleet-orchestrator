#!/usr/bin/env python3
"""Wait on cmux events until all given fleet roles finish their turn.

Invoked by fleet-wait.sh:
    fleet_wait.py <feature> <manifest_path> <timeout_sec> <instance> [instance ...]
Env: TREE_BOTH holds `cmux tree --id-format both` output for ref->uuid mapping.

Matching rules:
- agent.hook.Stop: payload.session_id -> surfaceId (from ~/.cmuxterm
  *-hook-sessions.json) -> role whose manifest surface matches.
- notification.requested (emitted by fleet-dispatch.sh for local one-shot
  workers): titles are redacted in the event stream, so match by the caller's
  surface UUID plus title_length == len("fleet-<feature>:<instance>").

Prints "<instance>=done" per completion. Exit 0 all done, 124 timeout.
"""

import glob
import json
import os
import re
import signal
import subprocess
import sys


def main() -> int:
    feature, manifest_path = sys.argv[1], sys.argv[2]
    timeout_sec = int(sys.argv[3])
    args = sys.argv[4:]
    any_mode = "--any" in args           # race mode: first completion wins
    roles = [a for a in args if a != "--any"]
    tree_text = os.environ.get("TREE_BOTH", "")

    ref_uuid = dict(re.findall(r"(surface:\d+) ([0-9A-Fa-f-]{36})", tree_text))
    manifest = dict(
        line.strip().split("=", 1)
        for line in open(manifest_path)
        if "=" in line
    )

    pending = {}
    for role in roles:
        ref = manifest.get(role)
        uuid = ref_uuid.get(ref, "") if ref else ""
        if not ref:
            print(f"unknown instance in manifest: {role}", file=sys.stderr)
            return 2
        if not uuid:
            print(f"instance surface is absent from cmux tree: {role} ({ref})", file=sys.stderr)
            return 2
        expected_uuid = manifest.get(f"{role}.uuid", "").upper()
        if not expected_uuid or uuid.upper() != expected_uuid:
            print(f"instance UUID mismatch: {role} ({ref})", file=sys.stderr)
            return 2
        pending[role] = {
            "surface": uuid.upper(),
            "notify_title": f"fleet-{feature}:{role}",
            "runner": manifest.get(f"{role}.runner", ""),
        }

    print(f'waiting for: {", ".join(pending)}', flush=True)

    def session_surface(sid: str) -> str:
        # Event session_ids carry an agent prefix ("codex-019f...") that the
        # hook-session files' keys ("019f...") do not — try both forms.
        candidates = {sid, re.sub(r"^[a-z]+-", "", sid, count=1)}
        home = os.path.expanduser("~/.cmuxterm")
        for f in glob.glob(f"{home}/*-hook-sessions.json"):
            try:
                d = json.load(open(f))
            except Exception:
                continue
            sessions = d.get("sessions", {})
            for c in candidates:
                s = sessions.get(c)
                if s and s.get("surfaceId"):
                    return s["surfaceId"].upper()
        return ""

    def local_terminal_status(role: str) -> str:
        ledger_path = os.path.join(os.path.dirname(manifest_path), f"fleet-{feature}.ledger.jsonl")
        latest = ""
        try:
            with open(ledger_path, encoding="utf-8") as handle:
                for raw in handle:
                    event = json.loads(raw)
                    if event.get("instance") == role:
                        latest = event.get("status", "")
        except (OSError, json.JSONDecodeError):
            return ""
        return latest

    proc = subprocess.Popen(
        ["cmux", "events",
         "--name", "agent.hook.Stop",
         "--name", "notification.requested",
         "--no-ack", "--no-heartbeat"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "CMUX_QUIET": "1"},
    )

    def on_timeout(signum, frame):
        proc.kill()
        print("timeout", flush=True)
        sys.exit(124)

    signal.signal(signal.SIGALRM, on_timeout)
    signal.alarm(timeout_sec)

    try:
        for line in proc.stdout:
            try:
                e = json.loads(line)
            except Exception:
                continue
            name = e.get("name")
            done_role = None
            if name == "agent.hook.Stop":
                sid = (e.get("payload") or {}).get("session_id")
                surf = session_surface(sid) if sid else ""
                for role, m in pending.items():
                    if m["surface"] and surf == m["surface"]:
                        done_role = role
                        break
            elif name == "notification.requested":
                p = e.get("payload") or {}
                params = p.get("params") or {}
                surf = (
                    params.get("preferred_surface_id")
                    or (p.get("result") or {}).get("surface_id")
                    or e.get("surface_id")
                    or ""
                ).upper()
                tlen = params.get("title_length")
                for role, m in pending.items():
                    if (
                        m["surface"]
                        and surf == m["surface"]
                        and tlen == len(m["notify_title"])
                    ):
                        done_role = role
                        break
            if done_role:
                if pending[done_role]["runner"] == "local":
                    terminal = local_terminal_status(done_role)
                    if terminal != "succeeded":
                        print(f"{done_role}={terminal or 'failed'}", flush=True)
                        return 1
                print(f"{done_role}=done", flush=True)
                del pending[done_role]
                if any_mode or not pending:
                    return 0
        print("event stream closed unexpectedly", file=sys.stderr)
        return 1
    finally:
        proc.kill()


if __name__ == "__main__":
    sys.exit(main())
