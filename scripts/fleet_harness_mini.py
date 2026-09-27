"""Mini 2.4.6 adapter with a separate, networkless CONTROL process.

Only the controller's budget client can contact a provider. Commands returned
by Mini are data until the separately owned executor authorizes them. The
historical terminal consumer remains pure and its original bytes are retained.
"""
from __future__ import annotations

import copy
from pathlib import Path
import shutil

import fleet_json
from fleet_harness_sandbox import Sandbox, digest, publish
from fleet_harness_mini_terminal import recover_mini, InvalidTerminal

GUEST = Path(__file__).with_name("fleet_harness_mini_guest.py")
SENTINEL = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
SYSTEM = "You are the sole Worker. Use bash within the supplied scope. Return the exact owner JSON as the content accompanying the sole echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT tool call."
TEMPLATE = "{{task}}"
DIST = Path("/Users/hector/.local/share/fleet-harness-comparison/20260915-01/dist")
DEPENDENCIES = ("minisweagent", "pydantic", "pydantic_core", "jinja2", "markupsafe", "dotenv",
                "platformdirs", "rich", "pygments", "typing_extensions.py", "typing_inspection", "annotated_types")


def freeze_dependencies(source, destination):
    source, destination = Path(source), Path(destination)
    destination.mkdir(mode=0o755, parents=True, exist_ok=False)
    manifest = {}
    for package in DEPENDENCIES:
        origin = source / package
        files = sorted(origin.rglob("*")) if origin.is_dir() else [origin]
        for item in files:
            if "__pycache__" in item.parts or item.suffix == ".pyc": continue
            if item.is_symlink(): raise ValueError("runtime dependency symlink unsupported")
            if item.is_dir(): continue
            relative = item.relative_to(source)
            raw = item.read_bytes()
            target = destination / relative
            target.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
            target.write_bytes(raw); target.chmod(0o644)
            manifest[str(relative)] = digest(raw)
    if not manifest or "minisweagent/agents/default.py" not in manifest:
        raise ValueError("Mini runtime sources missing")
    return manifest


def dependency_manifest(root):
    root = Path(root)
    manifest = {}
    for item in sorted(root.rglob("*")):
        if item.is_symlink(): raise ValueError("runtime dependency symlink")
        if item.is_file(): manifest[str(item.relative_to(root))] = digest(item.read_bytes())
    if "minisweagent/agents/default.py" not in manifest: raise ValueError("Mini runtime missing")
    return manifest


def validate_batch(response, seen):
    """Validate the WHOLE batch before the first command has any effect."""
    if (not isinstance(response, dict) or not isinstance(response.get("choices"), list)
            or len(response["choices"]) != 1 or not isinstance(response["choices"][0], dict)):
        raise InvalidTerminal("ambiguous provider choices")
    message = response["choices"][0].get("message")
    if (not isinstance(message, dict) or message.get("role") != "assistant"
            or not isinstance(message.get("content"), str)):
        raise InvalidTerminal("missing assistant content")
    calls, actions, ids = message.get("tool_calls"), [], set()
    if not isinstance(calls, list) or not 1 <= len(calls) <= 16:
        raise InvalidTerminal("bounded native action batch required")
    for call in calls:
        if (not isinstance(call, dict) or set(call) != {"id", "type", "function"}
                or call["type"] != "function" or not isinstance(call["id"], str) or not call["id"]
                or call["id"] in seen or call["id"] in ids
                or not isinstance(call["function"], dict)
                or set(call["function"]) != {"name", "arguments"} or call["function"]["name"] != "bash"
                or not isinstance(call["function"]["arguments"], str)):
            raise InvalidTerminal("ambiguous/foreign native tool identity")
        args = fleet_json.loads(call["function"]["arguments"])
        if (not isinstance(args, dict) or set(args) != {"command"} or not isinstance(args["command"], str)
                or not args["command"].strip() or len(args["command"].encode()) > 32768 or "\x00" in args["command"]):
            raise InvalidTerminal("invalid bounded command")
        ids.add(call["id"])
        actions.append({"command": args["command"], "tool_call_id": call["id"]})
    if any(a["command"] == SENTINEL for a in actions) and len(actions) != 1:
        raise InvalidTerminal("native submit must be the sole action")
    return actions


class MiniControl:
    def __init__(self, root, *, owner, dependencies):
        self.root = Path(root); self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.owner, self.dependencies = owner, Path(dependencies)
        self.sandbox = None
        self.ordinal, self.seen, self.trajectory = 0, set(), None

    def start(self, task):
        task = copy.deepcopy(task)
        if len(fleet_json.canonical_bytes({"op": "init", "task": fleet_json.canonical_bytes(task).decode(), "id": "mini-1"})) > 256*1024:
            raise InvalidTerminal("Mini init exceeds the bounded RPC; task must be reduced before admission")
        if (self.root / "task.json").exists():
            raise ValueError("Mini admission already started; reconcile rather than run again")
        publish(self.root, "task.json", task)
        self.task = fleet_json.canonical_bytes(task).decode()
        publish(self.root, "runtime.json", {"dependencies": dependency_manifest(self.dependencies),
            "guest_sha256": digest(GUEST.read_bytes()), "adapter_sha256": digest(Path(__file__).read_bytes()),
            "consumer_sha256": digest(Path(__file__).with_name("fleet_harness_mini_terminal.py").read_bytes())})
        bridge, empty = self.root / "bridge", self.root / "empty"
        bridge.mkdir(mode=0o755); empty.mkdir(mode=0o755)
        (bridge / GUEST.name).write_bytes(GUEST.read_bytes()); (bridge / GUEST.name).chmod(0o644)
        self.sandbox = Sandbox(self.root / "resource", owner=self.owner)
        self.sandbox.create(candidate=empty, guest=bridge, dependencies=self.dependencies,
            argv=["/usr/local/bin/python3", "-I", "-S", "-B", "/bridge/" + GUEST.name], processes=4)
        self.sandbox.start()
        return self._rpc({"op": "init", "task": self.task})

    def _rpc(self, request):
        self.ordinal += 1
        result = self.sandbox.rpc({**request, "id": "mini-" + str(self.ordinal)}, timeout=20)
        publish(self.root, f"native/{self.ordinal:04d}.json", result)
        if result["error"] is not None:
            raise InvalidTerminal(result["error"])
        self.trajectory = copy.deepcopy(result["value"])
        if self.trajectory["info"]["mini_version"] != "2.4.6":
            raise InvalidTerminal("unreviewed native Mini version")
        return copy.deepcopy(self.trajectory)

    def query(self, response):
        response = copy.deepcopy(response)
        actions = validate_batch(response, self.seen)
        result = self._rpc({"op": "query", "response": response})
        if fleet_json.canonical_bytes(result["messages"][-1]["extra"]["actions"]) != fleet_json.canonical_bytes(actions):
            raise InvalidTerminal("native action parser differs from admission")
        self.seen.update(action["tool_call_id"] for action in actions)
        return actions

    def observe(self, outputs):
        actions = self.trajectory["messages"][-1].get("extra", {}).get("actions", [])
        if not isinstance(outputs, list) or len(outputs) != len(actions):
            raise InvalidTerminal("tool observation batch differs")
        for action, output in zip(actions, outputs):
            if (not isinstance(output, dict) or set(output) != {"tool_call_id", "output", "returncode"}
                    or output["tool_call_id"] != action["tool_call_id"] or type(output["returncode"]) is not int
                    or not isinstance(output["output"], str) or len(output["output"].encode()) > 32768):
                raise InvalidTerminal("invalid exact tool observation")
        return self._rpc({"op": "observe", "outputs": outputs})

    def restore(self, trajectory):
        value=self._rpc({"op":"restore","trajectory":copy.deepcopy(trajectory)})
        if fleet_json.canonical_bytes(value)!=fleet_json.canonical_bytes(trajectory):
            raise InvalidTerminal("pure Mini restore changed retained trajectory")
        self.seen={call["id"] for message in value["messages"] for call in message.get("tool_calls",[])}
        return value

    def terminal(self):
        from fleet_safe_paths import RootedFS
        with RootedFS(self.root) as fs:
            raw = fs.read_regular(f"native/{self.ordinal:04d}.json", directory_modes=(0o700,), max_bytes=4*1024*1024)
            task = fs.read_regular("task.json", directory_modes=(), max_bytes=65536).decode()
        retained = fleet_json.loads(raw)
        if retained["error"] is not None:
            raise InvalidTerminal("native terminal operation failed")
        trajectory = retained["value"]
        info = trajectory["info"]
        result = {"finish_reason": info["exit_status"], "final_response": info["submission"]}
        recovered = recover_mini(trajectory, result, task=task,
                                expected_template_sha256=digest(TEMPLATE.encode()))
        if (trajectory["messages"][0]["content"] != SYSTEM
                or trajectory["messages"][1]["content"] != task):
            raise InvalidTerminal("full rendered native prompt differs")
        return {"trajectory": trajectory, "runner": result, "extraction": recovered,
                "final": recovered["native_terminal_summary"]}

    def cleanup(self):
        if self.sandbox is None:
            return None
        cleaned = self.sandbox.cleanup()
        from fleet_safe_paths import RootedFS
        with RootedFS(self.root) as fs:
            runtime = fleet_json.loads(fs.read_regular("runtime.json", directory_modes=(), max_bytes=4*1024*1024))
        if runtime["dependencies"] != dependency_manifest(self.dependencies):
            raise InvalidTerminal("Mini dependency bytes changed during execution")
        return cleaned
