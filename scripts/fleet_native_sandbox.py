"""Provider-free Python capsule enforced by macOS Seatbelt.

This trusted maintenance API does not grant Herdr admission or accept provider
credentials. The caller supplies explicit input bytes, not host read/write
roots. Arbitrary Python (including native calls) runs inside the OS boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

import fleet_json

MAX_BYTES = 8 * 1024 * 1024
MAX_FILES = 256
READERS = frozenset({"research", "plan", "review", "verify", "synthesis"})
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")


class SandboxError(ValueError):
    """No fallback to an unconfined process is permitted."""


@dataclass(frozen=True)
class Result:
    report: dict
    stdout: bytes
    stderr: bytes
    files: dict[str, bytes]


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _quote(path: Path) -> str:
    # SBPL is Scheme, not JSON. Reject syntax instead of guessing escaping.
    value = str(path)
    if any(ord(c) < 32 or ord(c) == 127 or c in '\\"' for c in value):
        raise SandboxError("unsupported character in sandbox path")
    return '"' + value + '"'


def _inputs(source: bytes, files: dict[str, bytes], role: str) -> None:
    if role not in READERS | {"build"}:
        raise SandboxError("unknown stage role")
    if type(source) is not bytes or not source or len(source) > 256 * 1024:
        raise SandboxError("source must contain 1..262144 bytes")
    if type(files) is not dict or len(files) > MAX_FILES:
        raise SandboxError("too many input files")
    size = 0
    for name, raw in files.items():
        if type(name) is not str or not name or len(name) > 1024:
            raise SandboxError("invalid relative input path")
        parts = name.split("/")
        if any(p in {"", ".", ".."} for p in parts) or "\0" in name or "\\" in name:
            raise SandboxError("input paths must be normalized relative paths")
        if type(raw) is not bytes:
            raise SandboxError("input contents must be bytes")
        size += len(raw)
    if size > MAX_BYTES:
        raise SandboxError("input size exceeds 8 MiB")
    names = set(files)
    if any(str(parent) in names for name in names for parent in PurePosixPath(name).parents):
        raise SandboxError("input file conflicts with directory")


def runtime() -> tuple[Path, Path]:
    """Resolve this installed Python; never install or search alternate runtimes."""
    if sys.platform != "darwin" or not SANDBOX_EXEC.is_file():
        raise SandboxError("macOS sandbox-exec is required; no unsandboxed fallback")
    prefix = Path(sys.base_prefix).resolve(strict=True)
    if (prefix.parent.name != "Versions"
            or prefix.parent.parent.name not in {"Python.framework", "Python3.framework"}):
        raise SandboxError("offline profile requires a dedicated Python framework prefix")
    # Homebrew's bin/python is a trampoline that posix_spawns Python.app.
    # Execute the actual image so process-fork can remain denied from birth.
    framework = prefix / "Resources/Python.app/Contents/MacOS/Python"
    image = framework if framework.is_file() else Path(sys.executable).resolve(strict=True)
    if not image.is_relative_to(prefix):
        raise SandboxError("Python image must be inside its runtime prefix")
    _quote(prefix)
    _quote(image)
    return image, prefix


def profile(capsule: Path, image: Path, prefix: Path, role: str) -> str:
    """Only the controller-created capsule is writable; all network is denied."""
    if role not in READERS | {"build"}:
        raise SandboxError("unknown stage role")
    writable = [capsule / "scratch"]
    if role == "build":
        writable.append(capsule / "work")
    rules = [
        "(version 1)", "(deny default)",
        # These optional operations need an explicit denial. In particular,
        # deny default + a narrow sysctl allowlist did NOT stop KERN_PROCARGS2
        # from exposing another same-user process's environment in our canary.
        "(deny process-info* nvram* file-map-executable)",
        f"(allow process-exec (literal {_quote(image)}))",
        # No process-fork: fork and posix_spawn, including self-spawn, fail.
        # Self exec may replace the process but inherits the same OS policy.
        f"(allow file-read* file-map-executable (subpath {_quote(prefix)}) "
        '(subpath "/usr/lib") (subpath "/System/Library"))',
        # dyld opens / during startup. This does not grant recursive reads.
        '(allow file-read-data (literal "/"))',
        "(allow file-read-metadata)",
        # uname is used by ctypes. Do not allow kern.procargs2 (process env)
        # or turn this into an unrestricted sysctl-read rule.
        '(allow sysctl-read (sysctl-name "kern.ostype") '
        '(sysctl-name "kern.osrelease") (sysctl-name "kern.version") '
        '(sysctl-name "kern.hostname") (sysctl-name "hw.machine"))',
        f"(allow file-read* (subpath {_quote(capsule)}))",
    ]
    rules += [f"(allow file-write* (subpath {_quote(path)}))" for path in writable]
    return "\n".join(rules) + "\n"


BOOTSTRAP = """import resource, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_FSIZE, (8388608, 8388608))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
cpu = int(sys.argv[2])
resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
path = sys.argv[1]
sys.argv = [path]
with open(path, 'rb') as source:
    code = compile(source.read(), path, 'exec')
exec(code, {'__name__': '__main__', '__file__': path})
"""


def _capture(command: list[str], cwd: Path, env: dict, timeout: float,
             max_output: int, interrupt=None) -> tuple[int, str, bytes, bytes, str | None]:
    """Bound output/time; close inherited FDs and reap the exact owned process."""
    buffers = [bytearray(), bytearray()]
    deadline = time.monotonic() + timeout
    state = "exited"
    interruption = None
    if interrupt and (interruption := interrupt()):
        raise InterruptedError(interruption)
    with subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          close_fds=True, start_new_session=True) as child:
        def stop() -> None:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            with selectors.DefaultSelector() as poller:
                for index, stream in enumerate((child.stdout, child.stderr)):
                    os.set_blocking(stream.fileno(), False)
                    poller.register(stream, selectors.EVENT_READ, index)
                while poller.get_map() or child.poll() is None:
                    if interrupt and (interruption := interrupt()):
                        state = "interrupted"
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        state = "timed_out"
                        break
                    for key, _ in poller.select(min(remaining, 0.05)):
                        data = os.read(key.fd, 65536)
                        if not data:
                            poller.unregister(key.fileobj)
                            continue
                        capacity = max_output - sum(map(len, buffers))
                        buffers[key.data].extend(data[:capacity])
                        if len(data) > capacity:
                            state = "output_limit"
                            break
                    if state != "exited":
                        break
            if state != "exited":
                stop()
            try:
                child.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                state = "timed_out"
                stop()
                child.wait(timeout=5)
        finally:
            stop()
            child.wait(timeout=5)
    return child.returncode, state, bytes(buffers[0]), bytes(buffers[1]), interruption


def _collect(root: Path, identity: tuple[int, int]) -> dict[str, bytes]:
    """Never export symlinks, special files, hardlinks, or unbounded output."""
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
        raise SandboxError("candidate root was replaced")
    files = {}
    size = 0
    entries = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs + names):
            entries += 1
            if entries > MAX_FILES * 4:
                raise SandboxError("too many output entries")
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SandboxError("unsafe output entry")
            size += info.st_size
            if size > MAX_BYTES or len(files) >= MAX_FILES:
                raise SandboxError("output files exceed capsule limit")
            # The only untrusted process has been reaped before export.
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def execute(source: bytes, *, files: dict[str, bytes], role: str,
            parent: Path, timeout: float = 5, max_output: int = 256 * 1024,
            interrupt=None, execution_id: str | None = None) -> Result:
    """Run an offline maintenance capsule, never a Mission or live model.

    Caller input bytes are intentionally disclosed to the workload. Credentials
    must not be supplied as inputs. No host environment, stdin or extra FD is
    inherited. The returned report is evidence, never an admission grant.
    """
    _inputs(source, files, role)
    if execution_id is not None:
        if type(execution_id) is not str or str(uuid.UUID(execution_id)) != execution_id:
            raise SandboxError("execution identity must be a canonical UUID")
    if (type(timeout) not in {int, float} or not math.isfinite(timeout)
            or not 0 < timeout <= 60 or type(max_output) is not int
            or not 1 <= max_output <= MAX_BYTES):
        raise SandboxError("invalid execution bounds")
    image, prefix = runtime()
    parent = parent.resolve(strict=True)
    _quote(parent)
    if parent.is_relative_to(prefix) or parent.is_relative_to(Path("/System")):
        raise SandboxError("capsule must be separate from readable system/runtime roots")
    invocation = str(uuid.uuid4())
    request = {"source_sha256": _hash(source), "role": role,
               "files": {name: _hash(raw) for name, raw in sorted(files.items())},
               "timeout": timeout, "max_output": max_output}
    if execution_id is not None:
        request["execution_id"] = execution_id
    image_hash = _hash(image.read_bytes())
    with tempfile.TemporaryDirectory(prefix="native-capsule-", dir=parent) as temp:
        capsule = Path(temp).resolve(strict=True)
        work, scratch = capsule / "work", capsule / "scratch"
        work.mkdir(mode=0o700)
        work_info = work.stat()
        work_identity = (work_info.st_dev, work_info.st_ino)
        scratch.mkdir(mode=0o700)
        home = capsule / "home"
        home.mkdir(mode=0o700)
        program = capsule / "program.py"
        program.write_bytes(source)
        for name, raw in files.items():
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        if _collect(work, work_identity) != files:
            raise SandboxError("input names collide or normalize on this filesystem")
        policy = profile(capsule, image, prefix, role)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(scratch),
               "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        command = [str(SANDBOX_EXEC), "-p", policy, str(image), "-I", "-S", "-B",
                   "-c", BOOTSTRAP, str(program), str(max(1, math.ceil(timeout)))]
        returncode, outcome, stdout, stderr, interruption = _capture(
            command, work, env, timeout, max_output, interrupt=interrupt)
        # A failed/timed-out program never produces accepted output artifacts.
        successful = outcome == "exited" and returncode == 0
        exported = _collect(work, work_identity) if successful else {}
        if successful and role in READERS and exported != files:
            raise SandboxError("read-only candidate changed")
        report = {"schema": "fleet.native.offline.v1", "invocation_id": invocation,
            "request_sha256": fleet_json.sha256(request), "request": request,
            "profile_sha256": _hash(policy.encode()), "profile": policy,
            "runtime_image": str(image), "runtime_image_sha256": image_hash,
            "runtime_prefix": str(prefix), "platform": sys.platform,
            "capsule_root": str(capsule), "environment": env,
            "argv_sha256": fleet_json.sha256(command),
            "execution_status": outcome, "returncode": returncode,
            "interruption_reason": interruption,
            "stdout_sha256": _hash(stdout), "stderr_sha256": _hash(stderr),
            "files": {name: _hash(raw) for name, raw in sorted(exported.items())},
            "authority": "none", "mission_success": "NOT_VERIFIED",
            "scope": "offline Python capsule; no Herdr or provider execution"}
    report["cleanup_confirmed"] = not capsule.exists()
    return Result(report, stdout, stderr, exported)
