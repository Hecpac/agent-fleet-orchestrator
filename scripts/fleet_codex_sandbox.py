"""CONTROL-owned Codex capsule with an external Seatbelt boundary.

Only the pinned CLI and its code-mode host can execute. This maintenance lane
uses an existing local Responses bridge; it does not grant ordinary Herdr boot
or infer that a simulated broker has performed real provider inference.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

import fleet_codex_responses as responses
import fleet_herdr_inference as inference
import fleet_json
import fleet_native_sandbox as sandbox

# Separate compatibility profile: never relabel historical Herdr 0.153 runs.
CODEX_VERSION = "0.154.0"


@dataclass(frozen=True)
class Result:
    report: dict
    stdout: bytes
    stderr: bytes
    files: dict[str, bytes]
    transcripts: dict[str, bytes]


def runtime(image: Path) -> dict[str, str]:
    if sys.platform != "darwin" or not sandbox.SANDBOX_EXEC.is_file():
        raise sandbox.SandboxError("macOS Seatbelt required; no unconfined fallback")
    image = image.resolve(strict=True)
    host = image.with_name("codex-code-mode-host")
    for path in (image, host):
        if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
            raise sandbox.SandboxError("installed Codex image and code-mode host required")
        sandbox._quote(path)
    observed = subprocess.run([str(image), "--version"], env={"PATH": "/usr/bin:/bin"},
        stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=False)
    if observed.returncode or observed.stdout.strip() != f"codex-cli {CODEX_VERSION}".encode():
        raise sandbox.SandboxError("Codex image differs from current registered version")
    return {"codex": str(image), "codex-code-mode-host": str(host)}


def profile(capsule: Path, role: str, port: int) -> str:
    if role not in sandbox.READERS | {"build"} or type(port) is not int or not 0 < port < 65536:
        raise sandbox.SandboxError("invalid role or broker port")
    q = sandbox._quote
    rules = ["(version 1)", "(deny default)",
        "(deny process-info* nvram* file-map-executable)",
        # libdispatch needs the current process's unique identity. This does
        # not grant KERN_PROCARGS2 or inspection of the parent/controller.
        "(allow process-info-pidinfo (target same-sandbox))",
        # Fork is needed by the trusted code-mode host. No shell, Python,
        # candidate executable or library is in the exec/mapping allowlist.
        "(allow process-fork)", "(allow signal (target same-sandbox))",
        '(allow file-read* file-map-executable (subpath "/usr/lib") (subpath "/System/Library"))',
        '(allow file-read-data (literal "/"))', "(allow file-read-metadata)",
        '(allow file-read* file-write* (literal "/dev/null"))',
        '(allow file-read* (literal "/dev/random") (literal "/dev/urandom"))',
        '(allow sysctl-read (sysctl-name "hw.pagesize") (sysctl-name "hw.pagesize_compat") '
        '(sysctl-name "kern.usrstack64") (sysctl-name "hw.ncpu") (sysctl-name "hw.activecpu") '
        '(sysctl-name "kern.ostype") (sysctl-name "kern.osrelease") (sysctl-name "kern.version") '
        '(sysctl-name "kern.hostname") (sysctl-name "hw.machine"))',
        f'(allow network-outbound (remote ip "localhost:{port}"))',
        f"(allow file-read* (subpath {q(capsule)}))"]
    for name in ("codex", "codex-code-mode-host"):
        rules.append(f"(allow process-exec file-map-executable (literal {q(capsule/'bin'/name)}))")
    for name in (("scratch", "home", "work") if role == "build" else ("scratch", "home")):
        rules.append(f"(allow file-write* (subpath {q(capsule/name)}))")
    return "\n".join(rules) + "\n"


class BSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in
        ("flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")] + [
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)] + [
        (name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid", "nice")] + [
        ("start_seconds", ctypes.c_uint64), ("start_microseconds", ctypes.c_uint64)]


class Processes:
    """Observe only processes mapped from this invocation's private images.

    Never clean by CLI name, inferred parentage or a saved PID alone. Host code
    and the installed runtime are trusted; this is not a hostile-host defense.
    """
    def __init__(self, images):
        self.images = {str(p) for p in images}
        self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self.lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]

    def identity(self, pid):
        info, path = BSDInfo(), ctypes.create_string_buffer(4096)
        if self.lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            return None
        if info.uid != os.geteuid() or info.status == 5:  # SZOMB cannot execute.
            return None
        if self.lib.proc_pidpath(pid, path, len(path)) <= 0:
            return None
        image = os.fsdecode(path.value)
        if image not in self.images:
            return None
        return {"pid": pid, "image": image, "start": [info.start_seconds, info.start_microseconds]}

    def snapshot(self):
        size = self.lib.proc_listallpids(None, 0)
        if size <= 0 or size > 100000:
            raise sandbox.SandboxError("process inventory unavailable")
        pids = (ctypes.c_int * (size + 1024))()
        count = self.lib.proc_listallpids(pids, ctypes.sizeof(pids))
        if count <= 0 or count >= len(pids):
            raise sandbox.SandboxError("process inventory incomplete")
        return [info for pid in pids[:count] if pid > 0 and (info := self.identity(pid)) is not None]

    def stop(self):
        deadline = time.monotonic() + 3
        while True:
            remaining = self.snapshot()
            if not remaining:
                return True
            for info in remaining:
                if self.identity(info["pid"]) == info:
                    try:
                        os.kill(info["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(.02)


BOOTSTRAP = """import json, os, resource, sys
with open(sys.argv[1]) as stream: spec = json.load(stream)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_FSIZE, (8388608, 8388608))
resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
resource.setrlimit(resource.RLIMIT_CPU, (spec['cpu'], spec['cpu']))
os.execve(spec['argv'][0], spec['argv'], dict(os.environ))
"""


def _transcripts(home, identity):
    # Validate every ancestor before CONTROL reads the quiescent guest's HOME.
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
        raise sandbox.SandboxError("private HOME identity changed")
    sessions = home/'sessions'
    if not sessions.exists() and not sessions.is_symlink():
        return {}
    info = sessions.lstat()
    files = sandbox._collect(sessions, (info.st_dev, info.st_ino))
    return {'sessions/'+name:raw for name,raw in files.items()
        if len(Path(name).parts) == 4 and name.endswith('.jsonl')}


def _capture(command, work, env, timeout, bridge, processes, max_output):
    buffers, observed = [bytearray(), bytearray()], {}
    deadline = min(time.monotonic() + timeout, bridge.broker.local_deadline)
    outcome, reason, quiescent = "exited", None, False
    # Serialize process birth with pause/cancel admission, then release the
    # ledger lock so control requests can interrupt the foreground supervisor.
    with bridge.broker.publisher.transaction():
        bridge.broker.active()
        if time.monotonic() >= deadline:
            raise sandbox.SandboxError('capsule deadline before process birth')
        child = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, start_new_session=True)
    try:
        with selectors.DefaultSelector() as poller:
            for i, stream in enumerate((child.stdout, child.stderr)):
                os.set_blocking(stream.fileno(), False)
                poller.register(stream, selectors.EVENT_READ, i)
            poller.register(bridge.listener, selectors.EVENT_READ, "http")
            while child.poll() is None or any(k.data != "http" for k in poller.get_map().values()):
                for info in processes.snapshot():
                    observed[(info["pid"], tuple(info["start"]))] = info
                if time.monotonic() >= deadline:
                    outcome = "timed_out"
                    break
                try:
                    bridge.broker.poll()
                except Exception:
                    outcome = "timed_out" if time.monotonic() >= deadline else "interrupted"
                    reason = "broker admission withdrawn" if outcome == "interrupted" else None
                    break
                if time.monotonic() >= deadline:
                    outcome = "timed_out"
                    break
                for key, _ in poller.select(.025):
                    if key.data == "http":
                        try:
                            bridge.serve_once(timeout=min(1, max(.001, deadline-time.monotonic())))
                        except Exception:
                            outcome = "timed_out" if time.monotonic() >= deadline else "protocol_rejected"
                            reason = "local bridge rejected request" if outcome == "protocol_rejected" else None
                            break
                    else:
                        data = os.read(key.fd, 65536)
                        if not data:
                            poller.unregister(key.fileobj)
                            continue
                        capacity = max_output-sum(map(len, buffers))
                        buffers[key.data].extend(data[:capacity])
                        if len(data) > capacity:
                            outcome = "output_limit"
                            break
                if outcome != "exited":
                    break
    finally:
        # The direct child can still be the trusted pre-exec bootstrap. Its
        # Popen identity is held until wait; descendants use unique image paths.
        if child.poll() is None:
            child.kill()
        try:
            quiescent = processes.stop()
        finally:
            child.wait(timeout=5)
            child.stdout.close(); child.stderr.close()
    return child.returncode, outcome, reason, bytes(buffers[0]), bytes(buffers[1]), list(observed.values()), quiescent


def execute(bridge, prompt: bytes, *, image: Path, files: dict[str, bytes],
            parent: Path, role: str = "build", timeout: float = 20,
            max_output: int = 256*1024) -> Result:
    """Run a bounded local maintenance capsule; never grant Mission acceptance.

    The caller owns/authorizes the bridge and supplied bytes. Returned files are
    not copied into a Mission candidate automatically. No provider keys, host
    directories, arbitrary argv, environment or shell are accepted.
    """
    if type(bridge) is not responses.Bridge:
        raise sandbox.SandboxError("a CONTROL-owned local Responses bridge is required")
    sandbox._inputs(prompt, files, role)
    prompt_text = prompt.decode("utf-8")
    if "\0" in prompt_text or type(timeout) not in {float, int} or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise sandbox.SandboxError("invalid prompt or duration")
    if type(max_output) is not int or not 1 <= max_output <= sandbox.MAX_BYTES:
        raise sandbox.SandboxError("invalid output limit")
    bridge.broker.poll()
    bound = getattr(bridge.broker.publisher, 'launch', None)
    if bridge.policy['schema_version'] == 3:
        if (bound is None or bound['prompt_sha256'] != sandbox._hash(prompt)
                or bound['stage'] != role or bound['inputs'] != {n:sandbox._hash(b) for n,b in files.items()}):
            raise sandbox.SandboxError('Mission capsule launch bytes differ')
    installed = runtime(image)
    parent = parent.resolve(strict=True)
    sandbox._quote(parent)
    if any(parent.is_relative_to(p) for p in (Path('/System'), Path('/usr/lib'))):
        raise sandbox.SandboxError("capsule must be separate from readable system roots")
    invocation = str(uuid.uuid4())
    previous_deadline = bridge.broker.local_deadline
    capsule = Path(tempfile.mkdtemp(prefix="codex-capsule-", dir=parent)).resolve(strict=True)
    processes = None
    try:
        for name in ("bin", "work", "home", "scratch"):
            (capsule/name).mkdir(mode=0o700)
        home = capsule/'home'
        home_identity = (home.stat().st_dev, home.stat().st_ino)
        copied, pins = [], {}
        for name, origin in installed.items():
            dest = capsule/'bin'/name
            shutil.copyfile(origin, dest); dest.chmod(0o500)
            if sandbox._hash(dest.read_bytes()) != sandbox._hash(Path(origin).read_bytes()):
                raise sandbox.SandboxError("runtime copy changed")
            copied.append(dest); pins[name] = sandbox._hash(dest.read_bytes())
        if bound is not None and pins != {k:v['sha256'] for k,v in bound['manifest']['images'].items()}:
            raise sandbox.SandboxError('Mission capsule image differs')
        work = capsule/'work'; identity = (work.stat().st_dev, work.stat().st_ino)
        for name, raw in files.items():
            target = work/name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
        if sandbox._collect(work, identity) != files:
            raise sandbox.SandboxError("input names normalize or collide")
        config = bridge.client_config()
        config["check_for_update_on_startup"] = False
        # CLI's inner sandbox cannot be initialized inside this outer sandbox.
        # This is recorded honestly; legacy permission acceptance is unchanged.
        config["approval_policy"] = "never"
        config["sandbox_mode"] = "danger-full-access"
        argv = [str(copied[0]), "exec", "--skip-git-repo-check", "--json"]
        def flatten(value, prefix=""):
            for key, item in value.items():
                dotted = prefix+key
                if type(item) is dict:
                    yield from flatten(item, dotted+".")
                else:
                    yield dotted+"="+fleet_json.canonical_bytes(item).decode()
        for arg in flatten(config):
            argv.extend(["-c", arg])
        argv.append(prompt_text)
        port = bridge.listener.getsockname()[1]
        policy = profile(capsule, role, port)
        spec = capsule/'bootstrap.json'
        spec.write_bytes(fleet_json.canonical_bytes({"cpu": max(1,math.ceil(timeout)),
            "argv": [str(sandbox.SANDBOX_EXEC), "-p", policy, *argv]}))
        env = {"HOME": str(capsule/'home'), "CODEX_HOME": str(capsule/'home'),
            "TMPDIR": str(capsule/'scratch'), "PATH": str(capsule/'bin'), "SHELL": "/bin/sh",
            "LANG": "en_US.UTF-8", **bridge.client_environment()}
        processes = Processes(copied)
        bridge.broker.local_deadline = min(time.monotonic()+timeout,
            previous_deadline if previous_deadline is not None else float("inf"))
        command = [sys.executable, "-I", "-S", "-B", "-c", BOOTSTRAP, str(spec)]
        code, outcome, reason, stdout, stderr, seen, quiet = _capture(
            command, work, env, timeout, bridge, processes, max_output)
        if not quiet:
            # Do not delete resources that still belong to a live process.
            # The trusted installed images are the supported process family.
            raise sandbox.SandboxError("Codex runtime quiescence was not confirmed")
        if any(sandbox._hash((capsule/'bin'/name).read_bytes()) != pin for name,pin in pins.items()):
            raise sandbox.SandboxError("runtime image changed during execution")
        successful = outcome == "exited" and code == 0
        if successful:
            try:
                bridge.broker.poll()
            except Exception:
                successful = False
                outcome, reason = "interrupted", "broker admission withdrawn before export"
        exported = sandbox._collect(work, identity) if successful else {}
        if successful and role in sandbox.READERS and exported != files:
            raise sandbox.SandboxError("read-only candidate changed")
        transcripts = _transcripts(home, home_identity)
        # The ephemeral broker token is never included in the persisted report.
        token = bridge.token.encode()
        if any(token in raw for raw in [stdout,stderr,*transcripts.values(),*exported.values()]):
            raise sandbox.SandboxError("local capability appeared in exported content")
        report = {"schema": "fleet.codex.capsule.v1", "invocation_id": invocation,
            "role": role, "cli_version": CODEX_VERSION,
            "capsule_root": str(capsule), "broker_port": port,
            "input_files": {n:sandbox._hash(b) for n,b in files.items()},
            "broker_policy_id": bridge.broker.policy_id, "attempt": bridge.policy["attempt"],
            "run_id": bridge.policy["run_id"], "prompt_sha256": sandbox._hash(prompt),
            "runtime_images": pins, "profile": policy, "profile_sha256": sandbox._hash(policy.encode()),
            "execution_status": outcome, "returncode": code, "reason": reason,
            "processes_observed": seen, "quiescence_confirmed": quiet,
            "files": {name:sandbox._hash(raw) for name,raw in exported.items()},
            "transcripts": {name:sandbox._hash(raw) for name,raw in transcripts.items()},
            "stdout_sha256": sandbox._hash(stdout), "stderr_sha256": sandbox._hash(stderr),
            "scope": "externally confined installed CLI and code-mode host; attempt-bound broker",
            "inner_sandbox": "danger-full-access", "provider_execution": inference.provider_execution(bridge.policy),
            "authority": "none", "mission_success": "NOT_VERIFIED"}
    finally:
        bridge.broker.local_deadline = previous_deadline
        bridge.close()  # Revoke this invocation's endpoint on every outcome.
        try:
            stopped = processes is None or processes.stop()
        except Exception as exc:
            raise sandbox.SandboxError(f"quiescence unknown; capsule retained: {capsule}") from exc
        if stopped:
            shutil.rmtree(capsule)
        else:
            raise sandbox.SandboxError(f"live capsule retained for reconciliation: {capsule}")
    report["cleanup_confirmed"] = not capsule.exists()
    return Result(report,stdout,stderr,exported,transcripts)
