"""Fixed RPC adapter inside the container. It never decides test success."""
import importlib.util
import ctypes
import fcntl
import json
import os
from pathlib import Path
import resource
import socket
import signal
import struct
import sys

os.environ.clear()
os.environ.update(HOME="/tmp", TMPDIR="/tmp", LANG="C.UTF-8", PATH="/usr/local/bin:/usr/bin:/bin")
signal.signal(signal.SIGTERM, lambda *_: os._exit(143))


def deny_sockets():
    # Stack a stricter filter on Docker's default, never replace it. This fixed
    # profile is Linux AArch64 only. A forbidden socket kills the whole process.
    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]
    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]
    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xC00000B7), (0x06, 0, 0, 0x80000000),
                    (0x20, 0, 0, 0), (0x15, 0, 1, 198), (0x06, 0, 0, 0x80000000),
                    (0x15, 0, 1, 199), (0x06, 0, 0, 0x80000000), (0x06, 0, 0, 0x7FFF0000)]
    filters = (Filter * len(instructions))(*(Filter(*row) for row in instructions))
    program = Program(len(instructions), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot install mandatory socket filter")


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        interfaces = {name: struct.unpack("H", fcntl.ioctl(control.fileno(), 0x8913,
                      struct.pack("256s", name.encode()))[16:18])[0] for _, name in socket.if_nameindex()}
    deny_sockets()
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    hello = {"kind": "runtime", "python": ".".join(map(str, sys.version_info[:3])),
        "uid": os.getuid(), "cwd": os.getcwd(), "env": dict(os.environ),
        "cap_eff": status["CapEff"].strip(), "no_new_privs": status["NoNewPrivs"].strip(),
        "seccomp": status["Seccomp"].strip(),
        "seccomp_filters": int(status["Seccomp_filters"].strip()),
        "cgroup": {name: Path("/sys/fs/cgroup", name).read_text().strip()
                   for name in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")},
        "interfaces": interfaces,
        "ipv4_routes": Path("/proc/net/route").read_text().splitlines()[1:],
        "readonly": {name: bool(os.statvfs(name).f_flag & os.ST_RDONLY)
                     for name in ("/", "/candidate", "/harness")},
        "tmp_bytes": os.statvfs("/tmp").f_blocks * os.statvfs("/tmp").f_frsize,
        "shm_bytes": os.statvfs("/dev/shm").f_blocks * os.statvfs("/dev/shm").f_frsize,
        "rlimits": {name: list(resource.getrlimit(getattr(resource, name)))
                    for name in ("RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NOFILE", "RLIMIT_CORE")}}
    # This line is emitted before importing ANY candidate byte. Candidate output
    # cannot replace prior pipe bytes; the controller rejects extra protocol lines.
    print(json.dumps(hello, sort_keys=True), flush=True)
    os.closerange(3, 64)
    request = json.loads(sys.stdin.buffer.readline(32768))
    spec = importlib.util.spec_from_file_location("sample_stats", "/candidate/sample_stats.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    answers = []
    for values in request["calls"]:
        try:
            answer = {"value": module.stats(values), "error": None}
        except Exception as exc:
            answer = {"value": None, "error": type(exc).__name__}
        answer["input_after"] = values
        answers.append(answer)
    print(json.dumps({"kind": "answers", "answers": answers}, allow_nan=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
