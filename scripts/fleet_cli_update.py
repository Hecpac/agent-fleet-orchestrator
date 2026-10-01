#!/usr/bin/env python3
"""Managed automatic updates for the CLIs the fleet drives.

Each CLI has an explicit policy (``POLICIES``):

- ``auto``: update in place to the exact latest version and verify it. A
  failed npm update reinstalls the previous version; a Kimi release is probed
  with a real loopback turn before it replaces the installed binary.
- ``certify``: Codex. The operator's global ``codex`` updates like ``auto``.
  Herdr Missions do not use it: the latest release is also installed side by
  side, certified by ``fleet_codex_certify`` (real Herdr + TUI, loopback, no
  model) and registered in ``FLEET_CODEX_ROOT``; new Missions pin the latest
  certified install, running ones keep theirs. A failed certification keeps
  the previous certified version and is reported once (a new dialog needs a
  reviewed guard change).
- ``report``: Herdr is the orchestrator itself; only its version is reported.

Nothing here commits, edits runtime contracts, sends prompts or calls a model
provider. Kimi is installed natively (manifest + SHA-256 of the binary), never
by executing a downloaded script.

CLI::

    python3 -B scripts/fleet_cli_update.py check
    python3 -B scripts/fleet_cli_update.py run
    python3 -B scripts/fleet_cli_update.py install-agent [--hour H --minute M]
    python3 -B scripts/fleet_cli_update.py uninstall-agent
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable
import urllib.request

import fleet_json

REPO = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("FLEET_CLI_UPDATE_HOME", Path.home() / ".local/share/fleet-cli-updates"))
KIMI_BASE = "https://code.kimi.com/kimi-code"
AGENT_LABEL = "com.agent-fleet.cli-update"
VERSION = re.compile(r"(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)")

POLICIES: dict[str, dict[str, str]] = {
    "codex": {"policy": "certify", "source": "npm", "package": "@openai/codex"},
    "claude": {"policy": "auto", "source": "npm", "package": "@anthropic-ai/claude-code"},
    "opencode": {"policy": "auto", "source": "brew", "formula": "opencode"},
    "ollama": {"policy": "auto", "source": "brew", "formula": "ollama"},
    "kimi": {"policy": "auto", "source": "kimi"},
    "herdr": {"policy": "report", "source": "contract"},
}


class UpdateError(RuntimeError):
    pass


@dataclass
class Env:
    """Injectable effects: tests replace commands and downloads."""
    run: Callable[..., subprocess.CompletedProcess[str]]
    fetch: Callable[[str], bytes]
    state: Path
    kimi_binary: Path
    kimi_probe: Callable[[Path], list[dict[str, Any]]] | None = None
    codex_registry: Any = None
    certify: Callable[[Path], dict[str, Any]] | None = None

    @classmethod
    def real(cls) -> "Env":
        def run(argv: list[str], *, timeout: int = 600, env: dict[str, str] | None = None):
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                  env=env, check=False)

        def fetch(url: str) -> bytes:
            if not url.startswith(KIMI_BASE + "/"):
                raise UpdateError("download outside the pinned Kimi release origin")
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read(256 * 1024 * 1024)

        import fleet_codex_certify
        import fleet_codex_registry
        store = fleet_codex_registry.from_environment(os.environ)
        herdr = shutil.which("herdr")

        def certify(link: Path) -> dict[str, Any]:
            if herdr is None:
                raise UpdateError("herdr is required to certify Codex")
            return fleet_codex_certify.certify(link, root=store.root, herdr=Path(herdr).resolve())

        return cls(run=run, fetch=fetch, state=STATE, kimi_binary=Path.home() / ".kimi-code/bin/kimi",
                   kimi_probe=kimi_turn_records, codex_registry=store, certify=certify)


def _version(text: str) -> str | None:
    found = VERSION.findall(text or "")
    return found[-1] if found else None


def installed(env: Env, name: str) -> str | None:
    binary = str(env.kimi_binary) if name == "kimi" else name
    try:
        result = env.run([binary, "--version"], timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _version(result.stdout) if result.returncode == 0 else None


def latest(env: Env, name: str) -> str | None:
    policy = POLICIES[name]
    if policy["source"] == "npm":
        result = env.run(["npm", "view", policy["package"], "version"], timeout=120)
        return _version(result.stdout) if result.returncode == 0 else None
    if policy["source"] == "brew":
        result = env.run(["brew", "info", "--json=v2", policy["formula"]], timeout=120)
        if result.returncode:
            return None
        formulae = fleet_json.loads(result.stdout).get("formulae") or []
        return formulae[0]["versions"]["stable"] if formulae else None
    if policy["source"] == "kimi":
        return _version(env.fetch(f"{KIMI_BASE}/latest").decode())
    return None


def _newer(candidate: str | None, current: str | None) -> bool:
    def key(value: str) -> tuple[int, ...]:
        return tuple(int(part) for part in value.split("-", 1)[0].split("."))
    return bool(candidate and current and key(candidate) > key(current))


def kimi_turn_records(binary: Path, *, timeout: int = 120) -> list[dict[str, Any]]:
    """Run one real kimi-code turn against a loopback fixture; return its Wire.

    Isolated HOME/KIMI_CODE_HOME, sandbox-exec allows only the fixture port,
    no credentials and no provider: the binary's own records are the evidence,
    because version strings and embedded code do not show what it writes.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    reply = "FLEET_RESULT:kimi-probe:PASS"

    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in ({"choices": [{"index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": None}]},
                          {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}):
                self.wfile.write(b"data: " + json.dumps({"id": "fx", "object": "chat.completion.chunk",
                                                          "model": "fixture-model", **chunk}).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="fleet-kimi-probe-") as tmp:
            root = Path(tmp).resolve()
            home, kimi_home, work = root / "home", root / "kimi-home", root / "work"
            for path in (home, kimi_home, work):
                path.mkdir(mode=0o700)
            (kimi_home / "config.toml").write_text(
                'default_model = "fixture/model"\n\n[providers.fixture]\ntype = "openai"\n'
                f'base_url = "http://127.0.0.1:{server.server_port}/v1"\napi_key = "synthetic"\n\n'
                '[models."fixture/model"]\nprovider = "fixture"\nmodel = "fixture-model"\n'
                'max_context_size = 131072\ncapabilities = [ "tool_use" ]\n')
            policy = root / "net.sb"
            policy.write_text(f'(version 1)\n(allow default)\n(deny network*)\n'
                              f'(allow network-outbound (remote ip "localhost:{server.server_port}"))\n'
                              '(allow network-outbound (remote unix-socket))\n')
            subprocess.run(["/usr/bin/sandbox-exec", "-f", str(policy), str(binary), "-p",
                            "FLEET_RESULT:kimi-probe:<STATUS>", "--output-format", "stream-json",
                            "-m", "fixture/model"], cwd=work, capture_output=True, timeout=timeout, check=False,
                           env={"PATH": "/usr/bin:/bin", "HOME": str(home), "KIMI_CODE_HOME": str(kimi_home),
                                "TMPDIR": str(root), "LANG": "en_US.UTF-8", "KIMI_CODE_NO_AUTO_UPDATE": "1"})
            wires = list((kimi_home / "sessions").glob("wd_*/session_*/agents/main/wire.jsonl"))
            if len(wires) != 1:
                return []
            return [fleet_json.loads(line) for line in wires[0].read_bytes().splitlines() if line.strip()]
    finally:
        server.shutdown()
        server.server_close()


def judge_kimi_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The bridge and frontier must both accept what the binary actually wrote."""
    import fleet_frontier
    import kimi_hook_bridge
    metadata = [r for r in records if r.get("type") == "metadata"]
    protocol = metadata[0].get("protocol_version") if len(metadata) == 1 else None
    names = []
    for index, record in enumerate(records):
        if record.get("type") == "metadata":
            continue
        try:
            event = kimi_hook_bridge.hook_event(record_index=index, record=record,
                                                session_id="00000000-0000-0000-0000-000000000001",
                                                workspace_id="00000000-0000-0000-0000-000000000001",
                                                surface_id="00000000-0000-0000-0000-000000000001")
        except kimi_hook_bridge.KimiBridgeError:
            event = None
        if event:
            names.append(event["name"])
    supported = (protocol in kimi_hook_bridge.SUPPORTED_WIRE_PROTOCOLS
                 and protocol in fleet_frontier.KIMI_WIRE_PROTOCOLS
                 and names == ["agent.hook.UserPromptSubmit", "agent.hook.Stop"])
    return {"wire_protocol": protocol, "hook_events": names, "bridge_supported": supported}


def _backup_dir(env: Env, stamp: str) -> Path:
    path = env.state / "backups" / stamp
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def install_kimi(env: Env, version: str, backup: Path) -> tuple[Path, dict[str, Any]]:
    """Verify the release, probe the new binary in place of nothing, then swap.

    Manifest and binary share one origin: the checksum proves integrity of the
    download, not publisher authenticity (the same trust as the official
    installer). A binary whose Wire the bridge rejects is never made live.
    """
    manifest = fleet_json.loads(env.fetch(f"{KIMI_BASE}/binaries/{version}/manifest.json"))
    target = f"darwin-{'arm64' if platform.machine() in {'arm64', 'aarch64'} else 'x64'}"
    entry = (manifest.get("platforms") or {}).get(target) or {}
    filename, checksum = entry.get("filename"), entry.get("checksum")
    if manifest.get("version") != version or not isinstance(filename, str) or "/" in filename \
            or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise UpdateError("Kimi manifest is not a single exact release for this platform")
    payload = env.fetch(f"{KIMI_BASE}/binaries/{version}/{filename}")
    if hashlib.sha256(payload).hexdigest() != checksum:
        raise UpdateError("Kimi binary checksum differs from its manifest")
    saved = backup / "kimi"
    descriptor, temporary = tempfile.mkstemp(prefix=".kimi-update-", dir=env.kimi_binary.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
        os.chmod(temporary, 0o755)
        checks = judge_kimi_records((env.kimi_probe or kimi_turn_records)(Path(temporary)))
        if not checks["bridge_supported"]:
            return saved, checks
        if env.kimi_binary.exists():
            shutil.copy2(env.kimi_binary, saved)
        os.replace(temporary, env.kimi_binary)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return saved, checks


def update_codex(env: Env, row: dict[str, Any], current: str | None, target: str | None,
                 package: str) -> dict[str, Any]:
    """Global binary follows the release; Missions follow the certified registry."""
    import fleet_codex_registry
    import fleet_herdr_versions as versions
    row["action"] = "none"
    if _newer(target, current):
        result = env.run(["npm", "install", "-g", f"{package}@{target}"], timeout=900)
        after = installed(env, "codex")
        row.update(action="updated" if result.returncode == 0 and after == target else "error", installed_after=after)
        if row["action"] == "error":
            row["error"] = (result.stderr or result.stdout or f"installed {after}")[-400:]
            if current and after != current:
                env.run(["npm", "install", "-g", f"{package}@{current}"], timeout=900)
                row.update(action="rolled_back", installed_after=installed(env, "codex"))
    store = env.codex_registry
    if store is None:
        row["certification"] = "disabled (FLEET_CODEX_ROOT unset)"
        return row
    certified = store.latest_certified(herdr_version=versions.HERDR_VERSION)
    if target is None or certified and not _newer(target, certified["codex_version"]):
        row["certified"] = certified["codex_version"] if certified else None
        return row
    failed = [a for a in store.load()["attempts"] if a["codex_version"] == target and a["status"] != "PASS"]
    if failed:
        row.update(certification="failed_before", certified=certified["codex_version"] if certified else None,
                   certification_sha256=failed[-1]["certification_sha256"])
        return row
    try:
        record = env.certify(store.install(target, env.run))
    except (fleet_codex_registry.RegistryError, UpdateError, OSError) as exc:
        row.update(certification="install_failed", error=str(exc)[:400])
        return row
    if record.get("status") == "PASS":
        entry = store.register(record)
        row.update(certification="PASS", certified=entry["codex_version"], run_dir=record.get("run_dir"))
    else:
        store.note_attempt(record)
        reason = record.get("failure") or (record.get("result") or {}).get("failure")
        row.update(certification="FAIL", certified=certified["codex_version"] if certified else None,
                   failure=reason, run_dir=record.get("run_dir"))
    return row


def update_one(env: Env, name: str, stamp: str) -> dict[str, Any]:
    policy = POLICIES[name]
    current = installed(env, name)
    row: dict[str, Any] = {"cli": name, "policy": policy["policy"], "installed": current}
    if policy["policy"] == "report":
        import fleet_herdr_versions as versions
        row.update(pinned=versions.CURRENT_CONTRACT["herdr_version"], action="none")
        return row
    try:
        target = latest(env, name)
    except Exception as exc:  # network or registry failure: report, never guess
        row.update(action="error", error=f"latest version unavailable: {type(exc).__name__}")
        return row
    row["latest"] = target
    if policy["policy"] == "certify":
        return update_codex(env, row, current, target, policy["package"])
    if not _newer(target, current):
        row["action"] = "none"
        return row
    backup = _backup_dir(env, stamp)
    saved = None
    if policy["source"] == "npm":
        result = env.run(["npm", "install", "-g", f"{policy['package']}@{target}"], timeout=900)
    elif policy["source"] == "brew":
        result = env.run(["brew", "upgrade", policy["formula"]], timeout=1800)
    else:
        try:
            saved, kimi_checks = install_kimi(env, target, backup)
            result = subprocess.CompletedProcess([], 0, "", "")
        except (UpdateError, OSError, ValueError) as exc:
            row.update(action="error", error=str(exc))
            return row
        row["checks"] = kimi_checks
        if not kimi_checks["bridge_supported"]:
            row.update(action="rejected", installed_after=installed(env, name),
                       error="new Kimi Wire is not accepted by kimi_hook_bridge; binary not installed")
            return row
    after = installed(env, name)
    row.update(action="updated" if result.returncode == 0 else "error", installed_after=after)
    if result.returncode:
        row["error"] = (result.stderr or result.stdout)[-400:]
        return row
    if after != target:
        row.update(action="error", error=f"installed version {after} differs from {target}")
        if policy["source"] == "npm" and current:
            env.run(["npm", "install", "-g", f"{policy['package']}@{current}"], timeout=900)
            row.update(action="rolled_back", installed_after=installed(env, name))
    return row


def run(env: Env) -> dict[str, Any]:
    env.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (env.state / ".lock").open("a")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("another CLI update run is active") from exc
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if any(p["source"] == "brew" for p in POLICIES.values()):
            env.run(["brew", "update", "--quiet"], timeout=900)
        report = {"schema_version": "fleet.cli-update.v1", "at": stamp,
                  "results": [update_one(env, name, stamp) for name in POLICIES]}
        runs = env.state / "runs"
        runs.mkdir(exist_ok=True, mode=0o700)
        (runs / f"{stamp}.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        lock.close()


def check(env: Env) -> dict[str, Any]:
    rows = []
    for name, policy in POLICIES.items():
        row = {"cli": name, "policy": policy["policy"], "installed": installed(env, name)}
        if policy["policy"] != "report":
            try:
                row["latest"] = latest(env, name)
            except Exception as exc:
                row["latest_error"] = type(exc).__name__
        rows.append(row)
    return {"schema_version": "fleet.cli-update.v1", "results": rows}


def agent_plist(*, hour: int, minute: int, python: str | None = None) -> bytes:
    import plistlib
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Same precedence as the operator shell: Homebrew/npm before ~/.local/bin,
    # which also holds a shadowed standalone Codex that must not be measured.
    path = ":".join(["/opt/homebrew/bin", str(Path.home() / ".kimi-code/bin"), str(Path.home() / ".local/bin"),
                     "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    return plistlib.dumps({
        "Label": AGENT_LABEL,
        # env resolves python3 from PATH, so a Homebrew Python upgrade keeps the job valid.
        "ProgramArguments": [*([python] if python else ["/usr/bin/env", "python3"]), "-B",
                             str(REPO / "scripts/fleet_cli_update.py"), "run"],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "EnvironmentVariables": {"PATH": path, "HOMEBREW_NO_ENV_HINTS": "1", "KIMI_CODE_NO_AUTO_UPDATE": "1",
                                 "FLEET_CODEX_ROOT": str(Path.home() / ".local/share/fleet-codex")},
        "StandardOutPath": str(STATE / "agent.log"),
        "StandardErrorPath": str(STATE / "agent.log"),
        "WorkingDirectory": str(REPO),
    })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("check")
    sub.add_parser("run")
    install = sub.add_parser("install-agent")
    install.add_argument("--hour", type=int, default=9)
    install.add_argument("--minute", type=int, default=30)
    sub.add_parser("uninstall-agent")
    args = parser.parse_args(argv)
    plist = Path.home() / "Library/LaunchAgents" / f"{AGENT_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    try:
        if args.action == "check":
            result = check(Env.real())
        elif args.action == "run":
            result = run(Env.real())
        elif args.action == "install-agent":
            if not (0 <= args.hour <= 23 and 0 <= args.minute <= 59):
                raise UpdateError("invalid schedule")
            plist.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True, check=False)
            plist.write_bytes(agent_plist(hour=args.hour, minute=args.minute))
            loaded = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
            if loaded.returncode:
                raise UpdateError(f"launchctl bootstrap failed: {loaded.stderr.strip()}")
            result = {"installed": str(plist), "schedule": f"{args.hour:02d}:{args.minute:02d}"}
        else:
            subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True, check=False)
            if plist.exists():
                plist.unlink()
            result = {"removed": str(plist)}
    except UpdateError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
