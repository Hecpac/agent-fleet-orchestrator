#!/usr/bin/env python3
"""Certify one side-by-side Codex install for Herdr; provider-free and model-free.

Repeats, for any candidate binary, the 0.153.4/0.159.3 maintenance rehearsal
(outputs/vcxdp5z3c, outputs/c6_xyujij): real Herdr server and real Codex TUI in
a private profile, loopback Responses fixture, network limited to loopback by
sandbox-exec, the bundled Herdr SessionStart hook trusted only by its reviewed
hash. The real HerdrBackend boots Lead, Worker, Reviewer and Verifier under a
candidate contract with the *current* startup guard, delivers two Worker turns,
recovers through a new controller, rejects the fixture provider in the real
collector and rejects a silent rebind after a server restart.

PASS certifies those operations for the candidate with that guard. A new dialog
or model migration shows up as a failure here, never as a lost prompt in a
Mission; adapting the guard stays a reviewed human change.

    python3 -B scripts/fleet_codex_certify.py --codex BIN_DIR/codex [--register]
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_codex_registry as registry  # noqa: E402
import fleet_json  # noqa: E402
import fleet_herdr_startup as startup  # noqa: E402
import fleet_herdr_versions as versions  # noqa: E402

# Bundled hook of Herdr 0.9.0, reviewed on 2026-09-08; a change needs review.
HOOK_SHA256 = {"0.9.0": "38a90a2a99872d06dcbc978e7613406cdd852dbb07503abd7a113f518d5951f3"}
# Migrations Codex would otherwise prompt for, for every model a profile binds.
MODEL_MIGRATIONS = {"gpt-5.6-sol": "gpt-6-sol"}
TITLE_PROMPT = "Generate a concise, single-line task title"
WORKER_MODEL = "gpt-5.6-sol"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save(out: Path, name: str, value) -> None:
    (out / name).write_text(json.dumps(value, indent=2) + "\n")


def environment(out: Path) -> dict[str, str]:
    return {"HOME": str(out / "h"), "CODEX_HOME": str(out / "codex"), "XDG_CONFIG_HOME": str(out / "c"),
            "XDG_STATE_HOME": str(out / "s"), "XDG_CACHE_HOME": str(out / "x"), "TMPDIR": str(out / "tmp"),
            "PATH": str(out / "bin") + ":/usr/bin:/bin:/usr/sbin:/sbin", "SHELL": "/bin/sh",
            "TERM": "xterm-256color", "LANG": "en_US.UTF-8", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1"}


def sandbox_profile(out: Path) -> str:
    home = Path.home()
    denied = "\n".join(f'(deny file-read* (subpath "{home / name}"))'
                       for name in (".codex", ".codex-cli", ".claude", ".ssh", "Library/Keychains"))
    return f"""(version 1)
(allow default)
(deny network*)
(allow network-bind (local ip "localhost:*"))
(allow network-inbound (local ip "localhost:*"))
(allow network-outbound (remote ip "localhost:*"))
{denied}
(allow network-bind (local unix-socket (subpath "{out}")))
(allow network-outbound (remote unix-socket (subpath "{out}")))
"""


def table() -> dict[int, dict]:
    rows = {}
    for line in subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid=,comm="], text=True).splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            rows[int(parts[0])] = {"parent": int(parts[1]), "image": parts[2]}
    return rows


class Conductor:
    """Deterministic loopback Responses endpoint; no inference, no credentials."""

    def __init__(self, out: Path):
        self.out, self.phase, self.requests, self.owner_key = out, 0, [], None
        self.prompts: list[str] = []
        self.answers: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, data, kind="text/plain"):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply(404, b"No GET in this fixture")

            def do_POST(self):
                raw = b""
                try:
                    assert self.path == "/v1/responses", "Unexpected endpoint"
                    assert not self.headers.get("Authorization"), "Credentials forbidden"
                    size = int(self.headers.get("Content-Length", 0))
                    assert 0 < size <= 4 * 1024 * 1024
                    raw = self.rfile.read(size)
                    body = fleet_json.loads(raw)
                    users = [i for i in body.get("input", []) if i.get("type") == "message" and i.get("role") == "user"]
                    latest = "\n".join(p.get("text", "") for p in users[-1]["content"]
                                       if p.get("type") == "input_text") if users else ""
                    if latest.startswith(TITLE_PROMPT):
                        # Codex >= 0.159 asks for a task title after each turn: an
                        # extra billed request in real use; recorded, not served.
                        owner.requests.append({"title_request": True, "sha256": hashlib.sha256(raw).hexdigest()})
                        return self.reply(400, b"Title generation is not part of this fixture")
                    assert body.get("model") == WORKER_MODEL and body.get("stream") is True
                    assert owner.phase < len(owner.prompts), "Request budget exhausted"
                    assert latest == owner.prompts[owner.phase], "Wrong latest prompt"
                    key = body.get("prompt_cache_key")
                    assert isinstance(key, str) and key
                    if owner.phase == 1:
                        assert key == owner.owner_key, "Session identity changed after recovery"
                        texts = [part["text"] for item in body["input"] if item.get("type") == "message"
                                 for part in item.get("content", []) if part.get("type") in {"input_text", "output_text"}]
                        assert owner.prompts[0] in texts and owner.answers[0] in texts, "Prior conversation missing"
                    else:
                        owner.owner_key = key
                    phase = owner.phase
                    save(owner.out, f"http-request-{phase}.json", body)
                    message = {"type": "message", "id": f"compat-final-{phase}", "role": "assistant",
                               "phase": "final_answer",
                               "content": [{"type": "output_text", "text": owner.answers[phase], "annotations": []}]}
                    events = [{"type": "response.created", "response": {"id": f"compat-response-{phase}"}},
                              {"type": "response.output_item.done", "item": message},
                              {"type": "response.completed", "response": {"id": f"compat-response-{phase}",
                               "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}}]
                    owner.requests.append({"phase": phase, "sha256": hashlib.sha256(raw).hexdigest()})
                    owner.phase += 1
                    data = b"".join(("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n").encode() for e in events)
                    self.reply(200, data, "text/event-stream")
                except Exception as exc:
                    owner.requests.append({"rejected": type(exc).__name__, "reason": str(exc)})
                    if raw:
                        (owner.out / f"http-rejected-{len(owner.requests)}.json").write_bytes(raw)
                    self.reply(400, b"Deterministic fixture contract rejected")

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)

    def config(self) -> str:
        migrations = "\n".join(f'"{old}" = "{new}"' for old, new in MODEL_MIGRATIONS.items())
        return (f'model_provider="fleet_loopback"\ncheck_for_update_on_startup=false\n'
                f'[model_providers.fleet_loopback]\nname="Deterministic compatibility fixture"\n'
                f'base_url="http://127.0.0.1:{self.server.server_port}/v1"\nwire_api="responses"\n'
                f'requires_openai_auth=false\nsupports_websockets=false\nrequest_max_retries=0\n'
                f'stream_max_retries=0\n'), f"\n[notice.model_migrations]\n{migrations}\n"

    def start(self):
        self.thread.start()

    def close(self):
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()


def trust_bundled_hook(out: Path) -> dict:
    """Trust exactly the reviewed bundled hook through Codex's own config API."""
    process = subprocess.Popen([str(out / "bin/codex"), "app-server", "--listen", "stdio://"], cwd=out / "candidate",
                               env=environment(out), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=(out / "hook-api.stderr").open("w"), text=True, bufsize=1)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)

    def call(ident, method, params):
        process.stdin.write(json.dumps({"id": ident, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not selector.select(timeout=.5):
                continue
            line = process.stdout.readline()
            if not line:
                raise RuntimeError("app-server ended unexpectedly")
            value = fleet_json.loads(line)
            if value.get("id") == ident:
                if "error" in value:
                    raise RuntimeError(value["error"])
                return value["result"]
        raise TimeoutError(method)

    try:
        call(1, "initialize", {"clientInfo": {"name": "fleet_codex_certify", "version": "1"},
                               "capabilities": {"experimentalApi": True}})
        process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        process.stdin.flush()
        listing = call(2, "hooks/list", {"cwds": [str(out / "candidate")]})
        hooks = [h for entry in listing["data"] for h in entry["hooks"]]
        expected = "bash '" + str(out / "codex/herdr-agent-state.sh") + "' session"
        if (len(hooks) != 1 or hooks[0]["handlerType"] != "command" or hooks[0]["command"] != expected
                or hooks[0]["eventName"] != "sessionStart"):
            raise RuntimeError(f"unexpected hook inventory: {hooks}")
        call(3, "config/batchWrite", {"edits": [{"keyPath": "hooks.state", "value": {
            hooks[0]["key"]: {"trusted_hash": hooks[0]["currentHash"]}}, "mergeStrategy": "upsert"}],
            "filePath": str(out / "codex/config.toml"), "reloadUserConfig": True})
        after = call(4, "hooks/list", {"cwds": [str(out / "candidate")]})
        if after["data"][0]["hooks"][0]["trustStatus"] != "trusted":
            raise RuntimeError("bundled hook not trusted")
        return {"hook_key": hooks[0]["key"], "current_hash": hooks[0]["currentHash"]}
    finally:
        process.stdin.close()
        process.wait(timeout=10)
        selector.close()


def inner(out: Path, candidate: dict) -> int:
    import fleet_herdr
    import fleet_herdr_permissions as permissions
    import workflow_config

    env = environment(out)
    conductor = Conductor(out)
    mid, runs = str(uuid.uuid4()), [str(uuid.uuid4()), str(uuid.uuid4())]
    nonce = uuid.uuid4().hex
    for index, run in enumerate(runs):
        contract = {"schema_version": 1, "mission_id": mid, "run_id": run, "instance_id": "worker",
                    "candidate_tree_sha": None}
        conductor.prompts.append(json.dumps({**contract, "result_contract": contract,
            "task": f"Fixed response transport phase {index}: {nonce}"}, sort_keys=True, separators=(",", ":")))
        conductor.answers.append(json.dumps({**contract, "status": "PASS", "artifacts": [],
            "summary": f"Fixed loopback response {index}: {nonce}"}, sort_keys=True, separators=(",", ":")))
    calls, server = [], None
    result = {"mission_id": mid, "run_ids": runs, "codex_version": candidate["codex_version"],
              "startup_guard": candidate["startup_guard"], "external_model_turns": 0}
    compiled = workflow_config.compile_path(ROOT / "workflows/herdr-implementation.yaml")

    def run_command(command, **kwargs):
        r = subprocess.run(command, cwd=kwargs["cwd"], env=kwargs["env"], timeout=kwargs["timeout"],
                           capture_output=True, text=True)
        calls.append({"argv": command, "exit_code": r.returncode, "stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:]})
        save(out, "cli-calls.json", calls)
        return r

    def backend():
        return fleet_herdr.HerdrBackend(out / "runs", mid, session="p", feature="codex-certification",
                                        target_repo=out / "candidate", compiled=compiled, environment=env,
                                        run_command=run_command, codex_candidate=candidate)

    def start_server(attempt):
        nonlocal server
        with (out / f"server-{attempt}.log").open("w") as log:
            server = subprocess.Popen([str(out / "bin/herdr"), "--session", "p", "server"], cwd=out / "candidate",
                                      env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True)
        deadline = time.monotonic() + 15
        while not (out / "c/herdr/sessions/p/herdr.sock").is_socket():
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("private Herdr server did not start")
            time.sleep(.1)

    def stop_server():
        assert run_command([str(out / "bin/herdr"), "--session", "p", "server", "stop"], cwd=out / "candidate",
                           env=env, timeout=15).returncode == 0
        server.wait(timeout=15)

    def completed(sid, phase):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            found = list((out / "codex/sessions").glob(f"*/*/*/*{sid}.jsonl"))
            if len(found) == 1:
                raw = found[0].read_bytes()
                try:
                    rows = [fleet_json.loads(line) for line in raw.splitlines()]
                except fleet_json.FleetJSONError:
                    time.sleep(.2)
                    continue
                ends = [i for i, r in enumerate(rows) if r.get("type") == "event_msg"
                        and r["payload"].get("type") == "task_complete"
                        and r["payload"].get("last_agent_message") == conductor.answers[phase]]
                if ends:
                    rows = rows[:ends[-1] + 1]
                    meta = [r["payload"] for r in rows if r["type"] == "session_meta"]
                    assert len(meta) == 1 and meta[0]["id"] == sid
                    assert meta[0]["cli_version"] == candidate["codex_version"], meta[0]["cli_version"]
                    assert meta[0]["model_provider"] == "fleet_loopback"
                    contexts = [r["payload"] for r in rows if r["type"] == "turn_context"]
                    record = permissions.attest(contexts, permissions.policy("worker", str(out / "candidate")))
                    assert not any(r.get("type") == "response_item" and r["payload"].get("type") in
                                   {"function_call", "custom_tool_call"} for r in rows)
                    (out / f"transcript-{phase}.jsonl").write_bytes(raw)
                    return {"session_id": sid, "turn_id": rows[-1]["payload"]["turn_id"],
                            "sha256": hashlib.sha256(raw).hexdigest(), "permissions": record}
            time.sleep(.2)
        raise TimeoutError("native completed turn not observed")

    try:
        conductor.start()
        head, tail = conductor.config()
        config = out / "codex/config.toml"
        config.write_text(head + "\n" + config.read_text() + tail)
        start_server(1)
        b = backend()
        state = b.boot()
        assert versions.certified(state["runtime_contract"])
        assert state["runtime_contract"]["codex_version"] == candidate["codex_version"]
        acknowledgments = {m["instance_id"]: m.get("startup_acknowledgments") for m in state["members"]}
        result["boot"] = {"status": "PASS", "roles": [m["instance_id"] for m in state["members"]],
                          "startup_acknowledgments": acknowledgments}
        first = b.submit(runs[0], conductor.prompts[0], instance_id="worker")
        deadline = time.monotonic() + 15
        while not first["agent_session"] and time.monotonic() < deadline:
            time.sleep(.2)
            first = b.recover(runs[0])
        sid = first["agent_session"]["value"]
        result["first_turn"] = completed(sid, 0)
        try:
            b.collect_result(runs[0])
            raise AssertionError("fixture provider unexpectedly accepted")
        except fleet_herdr.HerdrBackendError as exc:
            assert "session identity mismatch" in str(exc), str(exc)
            result["provider_acceptance_guard"] = {"status": "PASS_REJECTED", "reason": str(exc)}
        b = backend()
        deadline = time.monotonic() + 15
        while True:
            recovered = b.recover(runs[0])
            assert recovered["agent_session"]["value"] == sid
            if recovered["status"] != "working" or time.monotonic() > deadline:
                break
            time.sleep(.2)
        assert recovered["status"] == "settled"
        second = b.submit(runs[1], conductor.prompts[1], instance_id="worker")
        assert second["phase"] == "submitted" and second["agent_session"]["value"] == sid
        result["second_turn"] = completed(sid, 1)
        assert conductor.phase == 2
        result["controller_recovery"] = "PASS"
        stop_server()
        state_path = out / "runs" / b.relative
        before = state_path.read_bytes()
        start_server(2)
        try:
            b.boot()
            raise AssertionError("restored terminal silently rebound")
        except fleet_herdr.HerdrBackendError as exc:
            assert "identity mismatch" in str(exc) or "agent_not_found" in str(exc), str(exc)
            result["server_restore_guard"] = {"status": "PASS_REJECTED", "reason": str(exc)}
        assert state_path.read_bytes() == before
        result["status"] = "PASS"
    except Exception as exc:
        result["status"] = "FAIL"
        result["failure"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}
        (out / "failure.log").write_text(traceback.format_exc())
    finally:
        if server and server.poll() is None:
            try:
                stop_server()
            except Exception as exc:
                result["stop_error"] = str(exc)
        conductor.close()
        result["loopback_records"] = conductor.requests
        result["title_requests"] = sum(1 for r in conductor.requests if r.get("title_request"))
        result["remaining_sockets"] = [str(p) for p in (out / "c").rglob("*.sock") if p.exists()]
        save(out, "result.json", result)
    return 0 if result["status"] == "PASS" else 2


def prepare(out: Path, codex: Path, herdr: Path) -> dict:
    for name in ("h", "c/herdr", "s", "x", "tmp", "codex", "bin", "candidate", "runs"):
        (out / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    startup.validate_local_socket_path(str(out / "c/herdr/sessions/p/herdr-client.sock"))
    (out / "candidate/marker.txt").write_text("certification fixture\n")
    (out / "c/herdr/config.toml").write_text('[terminal]\ndefault_shell="/bin/sh"\nshell_mode="non_login"\n'
                                             '[update]\nversion_check=false\nmanifest_check=false\n')
    (out / "maintenance.sb").write_text(sandbox_profile(out))
    (out / "bin/codex").symlink_to(codex)
    (out / "bin/herdr").symlink_to(herdr)
    env = environment(out)
    herdr_version = subprocess.check_output([str(herdr), "--version"], text=True).strip().split()[-1]
    install = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(out / "maintenance.sb"), str(out / "bin/herdr"),
                              "integration", "install", "codex"], cwd=out / "candidate", env=env,
                             capture_output=True, text=True, timeout=60)
    if install.returncode:
        raise RuntimeError(f"herdr integration install failed: {install.stderr or install.stdout}")
    hook = hashlib.sha256((out / "codex/herdr-agent-state.sh").read_bytes()).hexdigest()
    if HOOK_SHA256.get(herdr_version) != hook:
        raise RuntimeError("bundled Herdr hook changed; review required before certification")
    trust = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(out / "maintenance.sb"), sys.executable, "-B",
                            str(Path(__file__).resolve()), "--trust", str(out)], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=120)
    if trust.returncode:
        raise RuntimeError(f"bundled hook trust failed: {(trust.stderr or trust.stdout)[-600:]}")
    return {"herdr_version": herdr_version, "hook_sha256": hook,
            "hook_trust": fleet_json.loads((out / "hook-trust.json").read_bytes())}


def certify(codex: Path, *, root: Path, herdr: Path) -> dict:
    """Run the full rehearsal for one binary; return the certification record."""
    codex = codex.absolute()
    binary = codex.resolve(strict=True)
    version = subprocess.check_output([str(codex), "--version"], text=True).strip().split()[-1]
    guard = versions.CURRENT_CONTRACT["startup_guard"]
    (root / "runs").mkdir(parents=True, exist_ok=True, mode=0o700)
    out = Path(tempfile.mkdtemp(prefix="c", dir=root / "runs"))
    started = now()
    record = {"schema_version": registry.RECORD_SCHEMA, "codex_version": version,
              "binary_sha256": registry.file_sha256(binary), "bin_dir": str(codex.parent),
              "startup_guard": guard, "started_at": started, "run_dir": str(out),
              "model_migrations": MODEL_MIGRATIONS}
    try:
        record.update(prepare(out, codex, herdr))
        candidate = {"codex_version": version, "binary_sha256": record["binary_sha256"],
                     "bin_dir": str(out / "bin"), "startup_guard": guard,
                     "herdr_version": record["herdr_version"]}
        save(out, "candidate.json", candidate)
        command = ["/usr/bin/sandbox-exec", "-f", str(out / "maintenance.sb"), sys.executable, "-B",
                   str(Path(__file__).resolve()), "--inner", str(out)]
        with (out / "probe.log").open("w") as log:
            child = subprocess.Popen(command, cwd=ROOT, env=environment(out), stdout=log, stderr=subprocess.STDOUT)
        tracked, deadline = {child.pid}, time.monotonic() + 300
        while child.poll() is None and time.monotonic() < deadline:
            rows = table()
            while True:
                more = {p for p, v in rows.items() if v["parent"] in tracked} - tracked
                if not more:
                    break
                tracked.update(more)
            time.sleep(.2)
        if child.poll() is None:
            child.kill()
            raise TimeoutError("certification deadline exceeded")
        rows = table()
        remaining = {p: rows[p] for p in tracked if p in rows}
        result = fleet_json.loads((out / "result.json").read_text())
        record.update(result=result, remaining_processes=len(remaining),
                      status="PASS" if child.returncode == 0 and result.get("status") == "PASS" and not remaining
                      else "FAIL")
    except Exception as exc:
        record.update(status="FAIL", failure={"type": type(exc).__name__, "message": str(exc)[:2000]})
    record["finished_at"] = now()
    save(out, "certification.json", record)
    return record


def main(argv: list[str] | None = None) -> int:
    if argv is None and len(sys.argv) == 3 and sys.argv[1] == "--inner":
        out = Path(sys.argv[2])
        return inner(out, fleet_json.loads((out / "candidate.json").read_text()))
    if argv is None and len(sys.argv) == 3 and sys.argv[1] == "--trust":
        out = Path(sys.argv[2])
        save(out, "hook-trust.json", trust_bundled_hook(out))
        return 0
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--codex", required=True, type=Path, help="bin/codex of a side-by-side install")
    parser.add_argument("--herdr", type=Path, default=Path(shutil.which("herdr") or "/usr/local/bin/herdr"))
    parser.add_argument("--register", action="store_true", help="record a PASS in FLEET_CODEX_ROOT")
    args = parser.parse_args(argv)
    store = registry.from_environment(os.environ)
    if store is None:
        print(json.dumps({"ok": False, "error": "FLEET_CODEX_ROOT is required"}))
        return 1
    record = certify(args.codex, root=store.root, herdr=args.herdr.resolve())
    if args.register:
        if record["status"] == "PASS":
            record["registered"] = store.register(record)
        else:
            store.note_attempt(record)
    print(json.dumps({k: record.get(k) for k in ("status", "codex_version", "startup_guard", "run_dir",
                                                 "failure", "registered")}, indent=2))
    return 0 if record["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
