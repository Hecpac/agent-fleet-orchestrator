"""Owned local Docker resources for the opt-in harness test lane.

The controller and its evidence store never enter the sandbox. No image pulls,
host command execution, provider client, broad cleanup or Herdr activation.
"""
from __future__ import annotations

import hashlib
import base64
import io
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import tarfile
import time
import uuid

import fleet_functional_runner as legacy
import fleet_json
import fleet_safe_paths as safe
import fleet_control_runtime as process_runtime

IMAGE = "sha256:3a1883e6bd3272095de9407f33bcb0119dcc664e6cff2952cd0931caa8e0bf9f"
MAX_OUTPUT = 1024 * 1024


class SandboxError(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def publish(root, name, value):
    """Use the same fsynced no-follow publisher as the owner journal."""
    with safe.RootedFS(Path(root)) as fs:
        fs.atomic_write(name, fleet_json.canonical_bytes(value), directory_modes=(0o700,) * (len(Path(name).parts) - 1))


class Sandbox:
    def __init__(self, store, *, owner, image=IMAGE):
        self.store = Path(store).resolve()
        self.store.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.owner = str(uuid.UUID(owner))
        if self.owner != owner or image != IMAGE:
            raise SandboxError("unsupported owner/image")
        self.image = image
        self.docker = legacy.Docker()
        self.process = None
        self.buffer = b""
        self.snapshots = 0
        self.calls = 0
        self.stderr = b""
        self.rpc_ambiguous = False
        self.read_offset = 0

    def record(self):
        with safe.RootedFS(self.store) as fs:
            record = fleet_json.loads(fs.read_regular("resource.json", directory_modes=(), max_bytes=65536))
            created = fs.read_regular_optional("created.json", directory_modes=(), max_bytes=65536)
            if created is not None:
                value = fleet_json.loads(created)
                if set(value) != {"resource_sha256", "cid"} or value["resource_sha256"] != digest(fleet_json.canonical_bytes(record)):
                    raise SandboxError("created resource differs from intent")
                record["cid"] = value["cid"]
            return record

    def _observed(self):
        record = self.record()
        if record["endpoint_sha256"] != digest(self.docker.endpoint.encode()):
            raise SandboxError("Docker endpoint changed")
        info = self.docker.inspect(record.get("cid") or record["name"])
        if (info.get("Name") != "/" + record["name"] or info.get("Image") != self.image
                or info.get("Config", {}).get("Labels", {}).get("fleet.harness.owner") != self.owner
                or info["Config"]["Labels"].get("fleet.harness.resource") != record["resource_id"]
                or (record.get("cid") and info.get("Id") != record["cid"])):
            raise SandboxError("exact owned resource identity mismatch")
        return info

    def create(self, *, candidate, guest, argv, writable=(), temporary=(), processes=1, dependencies=None):
        candidate, guest = Path(candidate).resolve(strict=True), Path(guest).resolve(strict=True)
        if self.store.is_relative_to(candidate) or guest.is_relative_to(candidate):
            raise SandboxError("CONTROL/bridge must be outside candidate")
        if (self.store / "resource.json").exists():
            raise SandboxError("resource already has intent; reconcile without creating again")
        # Inspect an existing pinned image; docker create is forbidden to pull.
        self.docker.call(["image", "inspect", self.image])
        rid = str(uuid.uuid4())
        scratch = self.store / "work"
        scratch.mkdir(mode=0o777)
        scratch.chmod(0o777)
        record = {"version": "harness-resource-v1", "owner": self.owner, "resource_id": rid,
                  "name": "fleet-harness-" + rid, "cid": None, "image": self.image,
                  "endpoint_sha256": digest(self.docker.endpoint.encode()), "phase": "create_intent",
                  "candidate": str(candidate), "guest": str(guest), "argv": argv,
                  "writable": list(writable), "temporary": list(temporary), "processes": processes,
                  "scratch": str(scratch), "dependencies": str(Path(dependencies).resolve(strict=True)) if dependencies else None}
        publish(self.store, "resource.json", record)
        args = ["container", "create", "--pull=never", "--name", record["name"],
                "--label", "fleet.harness.owner=" + self.owner,
                "--label", "fleet.harness.resource=" + rid,
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges=true", "--user", "65534:65534",
                "--pids-limit", str(processes), "--memory", "128m", "--memory-swap", "128m",
                "--cpus", "1", "--ulimit", "nofile=64:64", "--ulimit", "fsize=1048576:1048576",
                "--shm-size", "1m", "--log-driver", "none", "-i", "--workdir", "/candidate",
                "--mount", f"type=bind,src={scratch},dst=/work",
                "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=8388608,mode=0777",
                "--mount", f"type=bind,src={candidate},dst=/candidate,readonly",
                "--mount", f"type=bind,src={guest},dst=/bridge,readonly",
                "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp",
                "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPYCACHEPREFIX=/tmp/pycache",
                "--env", "PATH=/usr/local/bin:/usr/bin:/bin"]
        if dependencies:
            args += ["--mount", f"type=bind,src={record['dependencies']},dst=/deps,readonly"]
        for relative in writable:
            from fleet_herdr_scope import path
            path(relative)
            source = candidate / relative
            if source.is_symlink() or not source.is_file() or source.resolve() != source:
                raise SandboxError("editable file must be a physical existing file")
            args += ["--mount", f"type=bind,src={source},dst=/candidate/{relative}"]
        for relative in temporary:
            from fleet_herdr_scope import path
            path(relative)
            args += ["--tmpfs", f"/candidate/{relative}:rw,nosuid,nodev,noexec,size=8388608,mode=0777"]
        args += ["--entrypoint", argv[0], self.image, *argv[1:]]
        cid = self.docker.call(args).decode().strip()
        publish(self.store, "created.json", {"resource_sha256": digest(fleet_json.canonical_bytes(record)), "cid": cid})
        publish(self.store, "created-inspect.json", self.validate())
        return record

    def validate(self):
        return self.validate_inspect(self._observed(), self.record())

    @staticmethod
    def validate_inspect(info, record):
        if (info.get("Name") != "/" + record["name"] or info.get("Image") != IMAGE
                or info.get("Id") != record["cid"]
                or info.get("Config", {}).get("Labels", {}).get("fleet.harness.owner") != record["owner"]
                or info["Config"]["Labels"].get("fleet.harness.resource") != record["resource_id"]):
            raise SandboxError("resource observation has foreign identity")
        h, c = info["HostConfig"], info["Config"]
        exact = {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
                 "Memory": 134217728, "MemorySwap": 134217728, "PidsLimit": record["processes"],
                 "NanoCpus": 1000000000, "ShmSize": 1048576}
        if (fleet_json.canonical_bytes({k: h.get(k) for k in exact}) != fleet_json.canonical_bytes(exact) or h.get("CapDrop") != ["ALL"]
                or h.get("CapAdd") or h.get("Devices") or h.get("PidMode")
                or h.get("IpcMode") not in {"private", ""} or h.get("PortBindings")
                or h.get("SecurityOpt") != ["no-new-privileges=true"]
                or c.get("User") != "65534:65534" or c.get("WorkingDir") != "/candidate"
                or c.get("Entrypoint") != record["argv"][:1] or c.get("Cmd") != record["argv"][1:]
                or h.get("LogConfig", {}).get("Type") != "none"):
            raise SandboxError("sandbox configuration differs from creation")
        expected = {("/candidate", record["candidate"], False), ("/bridge", record["guest"], False),
                    ("/work", record["scratch"], True)}
        expected |= {("/candidate/" + p, str(Path(record["candidate"]) / p), True) for p in record["writable"]}
        if record["dependencies"]:
            expected.add(("/deps", record["dependencies"], False))
        binds = {(m["Destination"], m["Source"], m["RW"]) for m in info["Mounts"] if m["Type"] == "bind"}
        if (binds != expected or any(m["Type"] not in {"bind", "tmpfs"} or type(m.get("RW")) is not bool for m in info["Mounts"])
                or len([m for m in info["Mounts"] if m["Type"] == "bind"]) != len(expected)
                or set(h.get("Tmpfs", {})) != {"/tmp", *("/candidate/" + p for p in record["temporary"])}):
            raise SandboxError("unexpected sandbox mount")
        allowed_env = {"PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256", "HOME", "TMPDIR",
                       "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"}
        if any(v.split("=", 1)[0] not in allowed_env for v in c.get("Env", [])):
            raise SandboxError("unexpected environment variable")
        return info

    def start(self):
        info = self.validate()
        if info["State"]["Status"] != "created":
            raise SandboxError("ambiguous start; reconcile existing resource")
        record = self.record()
        publish(self.store, "start-intent.json", {"resource": record, "action": "start"})
        # File-backed original streams survive loss of the observer. The trusted
        # launcher imposes an OS file-size limit before execing the Docker CLI.
        with open(self.store / "stdout.raw", "xb") as stdout, open(self.store / "stderr.raw", "xb") as stderr:
            os.chmod(stdout.name, 0o600); os.chmod(stderr.name, 0o600)
            self.process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).with_name("fleet_harness_attach.py")),
                self.docker.binary, "--host", self.docker.endpoint, "container", "start", "-ai", info["Id"]],
                stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
        os.set_blocking(self.process.stdin.fileno(), False)
        birth, zombie = process_runtime.process_observation(self.process.pid)
        publish(self.store, "attach.json", {"pid": self.process.pid, "owner": self.owner, "birth": birth,
                "resource_id": record["resource_id"], "identity": "direct-unreaped-child-of-controller"})
        return self

    def rpc(self, request, *, timeout=8):
        if self.process is None or self.process.poll() is not None or self.rpc_ambiguous or self.buffer:
            raise SandboxError("sandbox process is not attached")
        raw = fleet_json.canonical_bytes(request)
        if len(raw) > MAX_OUTPUT:
            raise SandboxError("RPC request exceeds bound")
        self.calls += 1
        self.rpc_ambiguous = True
        publish(self.store, f"rpc-intents/{self.calls:04d}.json", {"request_b64": base64.b64encode(raw + b"\n").decode()})
        pending = raw + b"\n"
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            if time.monotonic() >= deadline:
                raise SandboxError("RPC deadline exceeded")
            if pending:
                try: pending = pending[os.write(self.process.stdin.fileno(), pending[:65536]):]
                except BlockingIOError: pass
            output = self._stream("stdout.raw")
            self.buffer += output[self.read_offset:]
            self.read_offset = len(output)
            self.stderr = self._stream("stderr.raw")
            if len(self.buffer) > MAX_OUTPUT or len(self.stderr) > MAX_OUTPUT:
                raise SandboxError("RPC output exceeds bound")
            if b"\n" not in self.buffer:
                if self.process.poll() is not None: raise SandboxError("RPC closed before response")
                time.sleep(min(.01, max(0, deadline-time.monotonic())))
        if pending:
            raise SandboxError("response preceded complete request")
        line, self.buffer = self.buffer.split(b"\n", 1)
        publish(self.store, f"rpc/{self.calls:04d}.json", {"request_b64": base64.b64encode(raw + b"\n").decode(),
            "response_b64": base64.b64encode(line + b"\n").decode()})
        response = fleet_json.loads(line)
        if (not isinstance(response, dict) or set(response) != {"id", "value", "error", "input_after"}
                or response["id"] != request["id"]):
            raise SandboxError("unbound or forged RPC response")
        self.rpc_ambiguous = False
        return response

    def snapshot(self):
        """Host no-follow observation while every container process is frozen.

        Docker cp does not observe runtime tmpfs reliably. Use only the exact
        private bind from creation, with no traversal through candidate links.
        """
        cid = self.validate()["Id"]
        self.docker.call(["container", "pause", cid])
        try:
            info = self._observed()
            if info["State"].get("Paused") is not True:
                raise SandboxError("container pause not confirmed")
            snapshot = inspect_directory(self.record()["scratch"])
            self.snapshots += 1
            publish(self.store, f"snapshots/{self.snapshots:04d}.json", {"paused_inspect": info, "filesystem": snapshot})
            return snapshot
        finally:
            self.docker.call(["container", "unpause", cid])

    def _remove_exact(self):
        record = self.record()
        info = None
        try:
            info = self._observed()
        except legacy.RunnerBlocked:
            ids = self.docker.call(["container", "ls", "-a", "--no-trunc", "--filter",
                "name=^/" + record["name"] + "$", "--format", "{{.ID}}"])
            if ids.strip():
                raise SandboxError("cannot confirm exact resource absent")
        else:
            self.docker.call(["container", "rm", "--force", info["Id"]])
        remaining = self.docker.call(["container", "ls", "-a", "--no-trunc", "--filter",
            "label=fleet.harness.resource=" + record["resource_id"], "--format", "{{.ID}}"])
        if remaining.strip():
            raise SandboxError("owned resource cleanup incomplete")
        names = self.docker.call(["container", "ls", "-a", "--no-trunc", "--filter",
            "name=^/" + record["name"] + "$", "--format", "{{.ID}}"])
        if names.strip(): raise SandboxError("owned resource name remains")
        if not (self.store / "cleanup-observation.json").exists():
            publish(self.store, "cleanup-observation.json", {"resource":record, "before":info,
                "remaining_name":names.decode(), "remaining_resource_label":remaining.decode()})
        return record

    def _stream(self, name):
        with safe.RootedFS(self.store) as fs:
            parent,parts=fs._parent_descriptor(name,directory_modes=(),create=False)
            try:
                fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
                try:
                    info=os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid()
                            or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>64*1024*1024):
                        raise SandboxError("unsafe or oversized original attach stream")
                    # The active append-only Docker client may grow the file.
                    # Capture only the observed prefix; later polls read the rest.
                    raw=b""
                    while len(raw)<info.st_size:
                        block=os.read(fd,min(65536,info.st_size-len(raw)))
                        if not block:raise SandboxError("original attach stream shrank")
                        raw+=block
                    os.fsync(fd)
                finally:os.close(fd)
            finally:os.close(parent)
            return raw

    def _reap(self, *, terminate=False):
        if self.process is not None:
            try: self.process.stdin.close()
            except BrokenPipeError: pass
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()  # Exact direct child, never recovered PID signalling.
                self.process.wait(timeout=5)
            self.process = None
        elif (self.store / "start-intent.json").exists():
            if not (self.store / "attach.json").exists():
                raise SandboxError("ambiguous attach start; client identity missing")
            with safe.RootedFS(self.store) as fs:
                attach = fleet_json.loads(fs.read_regular("attach.json", directory_modes=(), max_bytes=65536))
            if attach["owner"] != self.owner or attach["resource_id"] != self.record()["resource_id"]:
                raise SandboxError("foreign recovered attach")
            deadline = time.monotonic() + 5
            while True:
                observed, zombie = process_runtime.process_observation(attach["pid"])
                if observed is None or zombie or observed != attach["birth"]: break
                if time.monotonic() >= deadline:
                    raise SandboxError("recovered attach still active; no safe signal capability")
                time.sleep(.02)
        if (self.store / "attach.json").exists():
            output, stderr = self._stream("stdout.raw"), self._stream("stderr.raw")
            # Every consumed byte must match the retained original RPC prefix.
            prefix = b""
            rpc_dir = self.store / "rpc"
            for index, item in enumerate(sorted(rpc_dir.glob("*.json")) if rpc_dir.exists() else [], 1):
                if item.name != f"{index:04d}.json": raise SandboxError("RPC retention gap")
                with safe.RootedFS(rpc_dir) as fs:
                    wire = fleet_json.loads(fs.read_regular(item.name, directory_modes=(), max_bytes=4*MAX_OUTPUT))
                response_raw=base64.b64decode(wire["response_b64"], validate=True)
                if len(response_raw)>MAX_OUTPUT: raise SandboxError("retained RPC exceeds response bound")
                prefix += response_raw
            if not output.startswith(prefix): raise SandboxError("original attach bytes differ from RPC evidence")
            # An RPC may have completed before its observer died. Reconcile its
            # exact retained intent with one complete original line; never resend.
            next_index = len(list(rpc_dir.glob("*.json"))) + 1 if rpc_dir.exists() else 1
            pending = self.store / "rpc-intents" / f"{next_index:04d}.json"
            remaining = output[len(prefix):]
            if pending.exists() and b"\n" in remaining:
                with safe.RootedFS(pending.parent) as fs:
                    intent = fleet_json.loads(fs.read_regular(pending.name, directory_modes=(), max_bytes=2*MAX_OUTPUT))
                request = fleet_json.loads(base64.b64decode(intent["request_b64"], validate=True))
                line = remaining.split(b"\n", 1)[0] + b"\n"
                if len(line)>MAX_OUTPUT: raise SandboxError("recovered RPC exceeds response bound")
                response = fleet_json.loads(line)
                if not isinstance(response, dict) or set(response) != {"id","value","error","input_after"} or response["id"] != request["id"]:
                    raise SandboxError("outstanding RPC cannot be reconciled from original output")
                publish(self.store, f"rpc/{next_index:04d}.json", {**intent,"response_b64":base64.b64encode(line).decode()})
                prefix += line
            tail = output[len(prefix):]
            streams = {"stdout_b64":base64.b64encode(tail).decode(), "stderr_b64":base64.b64encode(stderr).decode(),
                "bounded":len(tail)<=MAX_OUTPUT and len(stderr)<=MAX_OUTPUT,
                "owner":self.owner, "resource_id":self.record()["resource_id"]}
            publish(self.store, "terminal-streams.json", streams)
        else:
            tail, stderr = b"", b""
        return tail, stderr

    def cleanup(self):
        try:
            record = self._remove_exact()
        except BaseException:
            self._reap(terminate=True)
            raise
        tail, stderr = self._reap()
        receipt = {"resource": record, "inactive": True, "resources_clean": True,
                   "extra_stdout": bool(tail), "bounded_output": len(tail) <= MAX_OUTPUT and len(stderr) <= MAX_OUTPUT,
                   "observation": "successful exact name/resource queries after conditional ID removal"}
        publish(self.store, "cleanup.json", receipt)
        return receipt


def inspect_archive(raw):
    result, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
        for member in archive:
            name = member.name.removeprefix("./").rstrip("/")
            if name in {"", "."}:
                continue
            if name.startswith("/") or ".." in Path(name).parts or name in result or len(result) >= 2000:
                raise SandboxError("unsafe filesystem observation")
            total += member.size
            if total > 16 * 1024 * 1024 or member.size > 1048576:
                raise SandboxError("filesystem observation exceeds bounds")
            if member.isfile():
                row = {"kind": "file", "sha256": digest(archive.extractfile(member).read()), "size": member.size,
                       "mtime": str(member.pax_headers.get("mtime", member.mtime))}
            elif member.isdir():
                row = {"kind": "directory"}
            elif member.issym():
                row = {"kind": "symlink", "target": member.linkname}
            else:
                raise SandboxError("unsupported filesystem entry")
            result[name] = row
    return result


def inspect_directory(root):
    result, total = {}, 0
    def walk(fd, prefix, depth):
        nonlocal total
        if depth > 32:
            raise SandboxError("filesystem depth exceeds bound")
        for name in sorted(os.listdir(fd)):
            relative = prefix + name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if len(result) >= 2000 or info.st_nlink > 1 and stat.S_ISREG(info.st_mode):
                raise SandboxError("filesystem entry limit or hardlink")
            if stat.S_ISLNK(info.st_mode):
                result[relative] = {"kind": "symlink", "target": os.readlink(name, dir_fd=fd)}
            elif stat.S_ISDIR(info.st_mode):
                result[relative] = {"kind": "directory"}
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try: walk(child, relative + "/", depth + 1)
                finally: os.close(child)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if info.st_size > 1048576 or total > 16 * 1024 * 1024:
                    raise SandboxError("filesystem bytes exceed bound")
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    data = b""
                    while len(data) <= 1048576:
                        block = os.read(child, 65536)
                        if not block: break
                        data += block
                    after = os.fstat(child)
                    if len(data) > 1048576 or (info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                        raise SandboxError("filesystem observation raced")
                    result[relative] = {"kind": "file", "sha256": digest(data), "size": len(data), "mtime": str(info.st_mtime_ns),
                                        "bytes_b64": base64.b64encode(data).decode()}
                finally: os.close(child)
            else:
                raise SandboxError("unsupported filesystem entry")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: walk(fd, "", 0)
    finally: os.close(fd)
    return result
