"""Local Docker/stdlib runner. Only the controller evaluates the original tests."""
from __future__ import annotations

import copy
import ast
import hashlib
import io
import json
import os
import platform
from pathlib import Path, PurePosixPath
import selectors
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import unittest

import fleet_json

ROOT = Path(__file__).resolve().parents[1]
GUEST = ROOT / "scripts/fleet_stats_guest.py"
TEST_SHA = "8ca7116f8c856fa09d6aff64e3fbf0d0f88f826f8f607aaaffca619adb18d512"
CHECK = "python-stats-rpc-v1"
PROFILE = "docker-local-stdlib-v1"
SOURCE_POLICY = "stats-python-subset-v1"
ARGV = ["/usr/local/bin/python3", "-I", "-S", "-B", "/harness/fleet_stats_guest.py"]
ENV = {"HOME": "/tmp", "TMPDIR": "/tmp", "LANG": "C.UTF-8", "PATH": "/usr/local/bin:/usr/bin:/bin"}
LIMITS = {"memory_bytes": 134217728, "processes": 1, "cpu_seconds": 8,
          "cpus": 1, "output_bytes": 65536, "tmp_bytes": 8388608,
          "shm_bytes": 1048576, "file_bytes": 1048576, "open_files": 64,
          "wall_seconds": 15, "network": "none", "root_readonly": True}
# Invocation order of the byte-pinned original unittest suite.
CALLS = [[], [3, 2, 1], [1, "2"], [True, 1], [None], [-2, 0.5, 3], [1, 2, 3, 4]]


class RunnerBlocked(RuntimeError):
    pass


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def controller_sha():
    return sha(Path(__file__).read_bytes())


class Docker:
    def __init__(self):
        self.binary = shutil.which("docker")
        if not self.binary:
            raise RunnerBlocked("docker_cli_missing")
        p = subprocess.run([self.binary, "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
                           capture_output=True, timeout=10, shell=False)
        try:
            self.endpoint = json.loads(p.stdout)
        except (ValueError, TypeError) as exc:
            raise RunnerBlocked("docker_context_unavailable") from exc
        if p.returncode or not isinstance(self.endpoint, str) or not self.endpoint.startswith("unix:///"):
            raise RunnerBlocked("runner_requires_local_unix_docker_endpoint")

    def call(self, args, *, timeout=10):
        p = subprocess.run([self.binary, "--host", self.endpoint, *args], capture_output=True,
                           timeout=timeout, shell=False)
        if p.returncode:
            raise RunnerBlocked("docker_operation_failed:" + args[0])
        return p.stdout

    def inspect(self, name):
        return json.loads(self.call(["container", "inspect", name]))[0]

    def runtime(self, image):
        info = json.loads(self.call(["info", "--format", '{{json .}}']))
        if info.get("CgroupVersion") != "2" or info.get("OSType") != "linux":
            raise RunnerBlocked("linux_cgroup_v2_required")
        value = json.loads(self.call(["image", "inspect", image]))[0]
        if value.get("Os") != "linux" or value.get("Architecture") != "arm64":
            raise RunnerBlocked("runtime_architecture_incompatible")
        variables = dict(v.split("=", 1) for v in value.get("Config", {}).get("Env", []))
        return {"image_id": value["Id"], "python_version": variables.get("PYTHON_VERSION"),
                "engine_version": info["ServerVersion"], "architecture": "arm64", "os": "linux",
                "docker_endpoint_sha256": sha(self.endpoint.encode()), "dependencies": "stdlib-only",
                "controller_sha256": controller_sha(), "controller_python": platform.python_version(),
                "guest_sha256": sha(GUEST.read_bytes())}

    def cleanup(self, name, attempt):
        # Never clean by a broad label/filter. Name, label and ID must all agree.
        try:
            info = self.inspect(name)
        except RunnerBlocked:
            # Distinguish an absent exact container from an unavailable daemon.
            ids = self.call(["container", "ls", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.ID}}"])
            return not ids.strip()
        if info.get("Name") != "/" + name or info.get("Config", {}).get("Labels", {}).get("fleet.functional.attempt") != attempt:
            raise RunnerBlocked("container_ownership_mismatch")
        self.call(["container", "rm", "--force", info["Id"]])
        return not self.call(["container", "ls", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.ID}}"]).strip()


def unpack(tree, destination):
    if len(tree) > 32 * 1024 * 1024:
        raise RunnerBlocked("tree_too_large")
    seen, total = set(), 0
    with tarfile.open(fileobj=io.BytesIO(tree), mode="r:") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or str(path) != member.name.rstrip("/") or ".." in path.parts
                    or ".git" in path.parts or member.name in seen or len(seen) >= 1000
                    or not (member.isfile() or member.isdir())):
                raise RunnerBlocked("unsafe_tree_entry")
            seen.add(member.name)
            total += member.size
            if total > 32 * 1024 * 1024 or member.size > 4 * 1024 * 1024:
                raise RunnerBlocked("tree_expansion_limit")
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())
                target.chmod(0o644)
    for path in destination.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)


def check_bridge_source(tree):
    """Narrow stats source grammar: accepted code cannot forge the guest RPC.

    This is an acceptance constraint, not the OS sandbox. Unsupported programs
    may fail earlier under the real resource controls but can never pass.
    """
    with tarfile.open(fileobj=io.BytesIO(tree), mode="r:") as archive:
        source = archive.extractfile("sample_stats.py").read(4 * 1024 * 1024 + 1)
    try:
        program = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise RunnerBlocked("unsupported_candidate_bridge_source") from exc
    body = [node for node in program.body if not (isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))]
    if body and isinstance(body[0], ast.ImportFrom):
        imported = body.pop(0)
        if imported.module != "numbers" or imported.level != 0 or len(imported.names) != 1 or imported.names[0].name != "Number" or imported.names[0].asname:
            raise RunnerBlocked("unsupported_candidate_bridge_source")
    if len(body) != 1 or not isinstance(body[0], ast.FunctionDef):
        raise RunnerBlocked("unsupported_candidate_bridge_source")
    function = body[0]
    args = function.args
    if (function.name != "stats" or function.decorator_list or function.returns or getattr(function, "type_params", [])
            or args.posonlyargs or len(args.args) != 1 or args.args[0].annotation
            or args.vararg or args.kwarg or args.kwonlyargs or args.defaults or args.kw_defaults):
        raise RunnerBlocked("unsupported_candidate_bridge_source")
    allowed = {ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AugAssign,
        ast.Expr, ast.If, ast.For, ast.Break, ast.Continue, ast.Pass, ast.Raise,
        ast.Name, ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set, ast.Subscript, ast.Slice,
        ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.keyword,
        ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp, ast.comprehension,
        ast.Load, ast.Store, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
        ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
        ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn}
    calls = {"any", "all", "bool", "float", "int", "isinstance", "len", "list", "max", "min",
             "sum", "tuple", "range", "enumerate", "ValueError", "TypeError", "stats"}
    nodes = list(ast.walk(function))
    if len(nodes) > 10000:
        raise RunnerBlocked("unsupported_candidate_bridge_source")
    for node in nodes:
        if (type(node) not in allowed or (isinstance(node, (ast.Name, ast.arg))
                and (node.id if isinstance(node, ast.Name) else node.arg).startswith("_"))
                or (isinstance(node, ast.FunctionDef) and node is not function)
                or (isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in calls))
                or (isinstance(node, ast.comprehension) and node.is_async)):
            raise RunnerBlocked("unsupported_candidate_bridge_source")


def check_config(info, spec, name, candidate, harness):
    h, c, limits = info["HostConfig"], info["Config"], spec["limits"]
    expected = {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
        "Memory": limits["memory_bytes"], "MemorySwap": limits["memory_bytes"],
        "PidsLimit": limits["processes"], "NanoCpus": 1000000000, "ShmSize": limits["shm_bytes"]}
    if (any(h.get(k) != v for k, v in expected.items()) or h.get("CapDrop") != ["ALL"]
            or h.get("CapAdd") or h.get("Devices") or h.get("PidMode") or h.get("IpcMode") not in {"private", ""}
            or h.get("SecurityOpt") != ["no-new-privileges=true"] or h.get("PortBindings")
            or h.get("LogConfig", {}).get("Type") != "none"
            or c.get("User") != "65534:65534" or c.get("WorkingDir") != "/candidate"
            or c.get("Entrypoint") != [ARGV[0]] or c.get("Cmd") != ARGV[1:]
            or info.get("Image") != spec["runtime"]["image_id"] or info.get("Name") != "/" + name):
        raise RunnerBlocked("container_configuration_mismatch")
    mounts = {(m["Destination"], m["Source"], m["RW"]) for m in info["Mounts"] if m["Type"] == "bind"}
    if mounts != {("/candidate", str(candidate), False), ("/harness", str(harness), False)}:
        raise RunnerBlocked("container_mounts_mismatch")
    if any(m["Type"] not in {"bind", "tmpfs"} for m in info["Mounts"]):
        raise RunnerBlocked("unexpected_container_mount")


def check_hello(hello, spec):
    l = spec["limits"]
    interfaces = hello.get("interfaces", {}) if isinstance(hello, dict) else {}
    if (not isinstance(interfaces, dict) or not interfaces or len(interfaces) > 32
            or {name for name, flags in interfaces.items() if type(flags) is int and flags & 1} != {"lo"}
            or any(type(flags) is not int for flags in interfaces.values())
            or type(hello.get("seccomp_filters")) is not int or hello["seccomp_filters"] < 2):
        raise RunnerBlocked("network_namespace_or_socket_filter_not_attested")
    expected = {"kind": "runtime", "python": spec["runtime"]["python_version"], "uid": 65534,
        "cwd": "/candidate", "env": ENV, "cap_eff": "0000000000000000", "no_new_privs": "1", "seccomp": "2",
        "cgroup": {"memory.max": str(l["memory_bytes"]), "memory.swap.max": "0",
                   "pids.max": str(l["processes"]), "cpu.max": "100000 100000"},
        "interfaces": interfaces, "seccomp_filters": hello["seccomp_filters"], "ipv4_routes": [],
        "readonly": {"/": True, "/candidate": True, "/harness": True},
        "tmp_bytes": l["tmp_bytes"], "shm_bytes": l["shm_bytes"],
        "rlimits": {"RLIMIT_CPU": [l["cpu_seconds"]] * 2, "RLIMIT_FSIZE": [l["file_bytes"]] * 2,
                    "RLIMIT_NOFILE": [l["open_files"]] * 2, "RLIMIT_CORE": [0, 0]}}
    if hello != expected:
        raise RunnerBlocked("effective_isolation_or_runtime_mismatch")


def evaluate_original(tests, answers):
    if sha(tests) != TEST_SHA or not isinstance(answers, list) or len(answers) != len(CALLS):
        raise ValueError("canonical_tests_or_response_count_mismatch")
    pending = list(zip(copy.deepcopy(CALLS), answers))
    def stats(values):
        expected, response = pending.pop(0)
        if values != expected or set(response) != {"value", "error", "input_after"} or not isinstance(response["input_after"], list):
            raise ValueError("RPC response binding mismatch")
        values[:] = response["input_after"]
        if response["error"]:
            raise {"TypeError": TypeError, "ValueError": ValueError}.get(response["error"], RuntimeError)("candidate exception")
        return response["value"]
    module = types.ModuleType("sample_stats")
    module.stats = stats
    original = sys.modules.get("sample_stats")
    sys.modules["sample_stats"] = module
    try:
        namespace = {"__name__": "fleet_original_stats_tests"}
        exec(compile(tests, "original_test_sample_stats.py", "exec"), namespace)
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream).run(unittest.defaultTestLoader.loadTestsFromTestCase(namespace["StatsTests"]))
        return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                "passed": result.wasSuccessful() and result.testsRun == 5 and not pending}, stream.getvalue().encode()
    finally:
        if original is None:
            del sys.modules["sample_stats"]
        else:
            sys.modules["sample_stats"] = original


def execute(spec, tree, tests, attempt, *, interrupt=None):
    name = "fleet-functional-" + attempt
    evidence, status, reason = {}, "blocked", "preflight_incomplete"
    docker, process, created = None, None, False
    with tempfile.TemporaryDirectory(prefix="fleet-functional-") as temporary:
        root = Path(temporary).resolve()
        candidate, harness = root / "candidate", root / "harness"
        candidate.mkdir(mode=0o755)
        harness.mkdir(mode=0o755)
        try:
            if interrupt and (interruption := interrupt()):
                raise InterruptedError(interruption)
            if sha(tests) != TEST_SHA:
                raise RunnerBlocked("canonical_tests_missing_or_changed")
            unpack(tree, candidate)
            if not (candidate / "sample_stats.py").is_file():
                raise RunnerBlocked("candidate_module_missing")
            shutil.copyfile(GUEST, harness / GUEST.name)
            (harness / GUEST.name).chmod(0o644)
            docker = Docker()
            runtime = docker.runtime(spec["runtime"]["image_id"])
            if runtime != spec["runtime"]:
                raise RunnerBlocked("runtime_or_controller_incompatible")
            l = spec["limits"]
            command = ["container", "create", "--pull=never", "--name", name,
                "--label", "fleet.functional.attempt=" + attempt, "--interactive", "--network=none",
                "--read-only", "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges=true",
                "--memory=" + str(l["memory_bytes"]), "--memory-swap=" + str(l["memory_bytes"]),
                "--pids-limit=" + str(l["processes"]), "--cpus=1", "--shm-size=" + str(l["shm_bytes"]),
                "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=" + str(l["tmp_bytes"]), "--log-driver=none",
                "--ulimit=cpu=" + str(l["cpu_seconds"]) + ":" + str(l["cpu_seconds"]),
                "--ulimit=fsize=" + str(l["file_bytes"]) + ":" + str(l["file_bytes"]),
                "--ulimit=nofile=" + str(l["open_files"]) + ":" + str(l["open_files"]), "--ulimit=core=0:0",
                "--mount", f"type=bind,source={candidate},target=/candidate,readonly",
                "--mount", f"type=bind,source={harness},target=/harness,readonly",
                "--workdir=/candidate", "--entrypoint=" + ARGV[0], spec["runtime"]["image_id"], *ARGV[1:]]
            # The attempt was already recorded by Fleet before this physical effect.
            created = True
            container_id = docker.call(command).decode().strip()
            info = docker.inspect(name)
            check_config(info, spec, name, candidate, harness)
            evidence["container-config.json"] = fleet_json.canonical_bytes(info)
            process = subprocess.Popen([docker.binary, "--host", docker.endpoint, "container", "start", "--attach", "--interactive", container_id],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
            process.stdin.write(fleet_json.canonical_bytes({"calls": CALLS}) + b"\n")
            process.stdin.close()
            chunks = {"stdout": bytearray(), "stderr": bytearray()}
            deadline = time.monotonic() + l["wall_seconds"]
            with selectors.DefaultSelector() as selector:
                for key in chunks:
                    selector.register(getattr(process, key), selectors.EVENT_READ, key)
                while selector.get_map():
                    if interrupt and (interruption := interrupt()):
                        raise InterruptedError(interruption)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("wall_timeout")
                    for item, _ in selector.select(min(0.1, max(0, deadline-time.monotonic()))):
                        raw = os.read(item.fileobj.fileno(), 4096)
                        if not raw:
                            selector.unregister(item.fileobj)
                            continue
                        remaining = l["output_bytes"] - sum(map(len, chunks.values()))
                        chunks[item.data] += raw[:remaining]
                        if len(raw) > remaining:
                            raise OverflowError("output_limit")
            exit_code = process.wait(timeout=2)
            evidence.update({key + ".txt": bytes(value) for key, value in chunks.items()})
            info_after = docker.inspect(name)
            evidence["container-state.json"] = fleet_json.canonical_bytes(info_after["State"])
            lines = bytes(chunks["stdout"]).splitlines()
            if not lines:
                raise RunnerBlocked("effective_runtime_not_attested")
            check_hello(fleet_json.loads(lines[0]), spec)
            if exit_code in {130, 143, 137} and not info_after["State"].get("OOMKilled"):
                status, reason = "indeterminate", "candidate_interrupted"
            elif exit_code or info_after["State"].get("OOMKilled"):
                status, reason = "failed", "candidate_process_failed"
            elif len(lines) != 2:
                status, reason = "failed", "missing_or_ambiguous_candidate_response"
            else:
                check_bridge_source(tree)
                answers = fleet_json.loads(lines[1])
                if not isinstance(answers, dict) or set(answers) != {"kind", "answers"} or answers["kind"] != "answers":
                    raise ValueError("invalid_candidate_response")
                result, log = evaluate_original(tests, answers["answers"])
                evidence["test-result.json"] = fleet_json.canonical_bytes(result)
                evidence["test-output.txt"] = log
                status, reason = ("passed", "original_tests_passed") if result["passed"] else ("failed", "original_tests_failed")
        except InterruptedError as exc:
            status, reason = "indeterminate", str(exc)
        except (TimeoutError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            status, reason = "indeterminate", type(exc).__name__
        except OverflowError:
            status, reason = "failed", "output_limit"
        except (RunnerBlocked, OSError, tarfile.TarError) as exc:
            status, reason = "blocked", str(exc)
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            status, reason = "failed", "invalid_candidate_protocol:" + type(exc).__name__
        finally:
            if "chunks" in locals():
                evidence.update({key + ".txt": bytes(value) for key, value in chunks.items()})
            # Cancel an unresolved attach/start RPC before asking the daemon to
            # remove its exact container; otherwise those operations can contend.
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if created:
                try:
                    if not docker.cleanup(name, attempt):
                        raise RunnerBlocked("cleanup_unconfirmed")
                except (RunnerBlocked, OSError, subprocess.TimeoutExpired):
                    status, reason = "indeterminate", "container_cleanup_unconfirmed"
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()
    return {"status": status, "reason": reason, "evidence": evidence}
