from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
FLEET_UP = ROOT / "scripts" / "fleet-up.sh"
FLEET_DISPATCH = ROOT / "scripts" / "fleet-dispatch.sh"
FLEET_DOWN = ROOT / "scripts" / "fleet-down.sh"
ROUTER = ROOT / "orchestration" / "router.yaml"


FAKE_CMUX = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["CMUX_STATE"])
log_path = Path(os.environ["CMUX_LOG"])
argv = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv) + "\n")

json_mode = False
if argv and argv[0] == "--json":
    json_mode = True
    argv = argv[1:]
command = argv[0]
args = argv[1:]

def load():
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {"next_surface": 2, "workspace": "workspace:1", "panes": []}

def save(state):
    state_path.write_text(json.dumps(state))

def arg_value(name):
    return args[args.index(name) + 1]

def uuid(number):
    return f"00000000-0000-0000-0000-{number:012d}"

state = load()
if command == "ping":
    print("PONG")
elif command == "new-workspace":
    state = {
        "next_surface": 2,
        "workspace": "workspace:1",
        "workspace_uuid": uuid(1),
        "panes": [{"surface": "surface:1", "uuid": uuid(101), "title": "shell"}],
    }
    save(state)
    print("OK workspace:1")
elif command == "workspace-action":
    print("OK")
elif command == "new-pane":
    number = state["next_surface"]
    state["next_surface"] += 1
    pane = {"surface": f"surface:{number}", "uuid": uuid(100 + number), "title": "shell"}
    state["panes"].insert(1, pane)
    save(state)
    print(f"OK surface:{number} pane:{number} workspace:1")
elif command == "rename-tab":
    surface = arg_value("--surface")
    title = args[-1]
    for pane in state["panes"]:
        if pane["surface"] == surface:
            pane["title"] = title
    save(state)
elif command == "send" or command == "send-key" or command == "workspace-action":
    pass
elif command == "read-screen":
    print("› ready\n❯\nctrl+p commands" if os.environ.get("CMUX_READY", "1") == "1" else "booting")
elif command == "close-workspace":
    state["closed"] = True
    save(state)
elif command == "tree":
    if state.get("closed") and "--all" in args:
        print("window window:1 00000000-0000-0000-0000-000000009999")
        raise SystemExit(0)
    if json_mode:
        panes = []
        for index, pane in enumerate(state["panes"]):
            panes.append({
                "index": index,
                "surfaces": [{"title": pane["title"], "ref": pane["surface"]}],
            })
        print(json.dumps({"windows": [{"workspaces": [{"panes": panes}]}]}))
    elif "--id-format" in args:
        print(f"workspace {state['workspace']} {state['workspace_uuid']} fleet-test")
        for pane in state["panes"]:
            print(f"surface {pane['surface']} {pane['uuid']} {pane['title']}")
    else:
        print(f"workspace {state['workspace']}")
        for pane in state["panes"]:
            print(f"surface {pane['surface']} {pane['title']} tty=ttys001")
else:
    print(f"unsupported fake cmux command: {command}", file=sys.stderr)
    raise SystemExit(97)
'''


class FleetUpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.state = self.tmp / "cmux-state.json"
        self.log = self.tmp / "cmux-log.jsonl"
        self.make_executable("cmux", FAKE_CMUX)
        self.make_executable("codex", "#!/bin/sh\nexit 0\n")
        self.make_executable("claude", "#!/bin/sh\nexit 0\n")
        self.make_executable("opencode", "#!/bin/sh\nexit 0\n")
        self.make_executable("ollama", "#!/bin/sh\nexit 0\n")
        self.make_executable("ps", "#!/bin/sh\necho codex claude opencode\n")
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.bin}:{self.env['PATH']}",
                "CMUX_STATE": str(self.state),
                "CMUX_LOG": str(self.log),
                "FLEET_RUNS_DIR": str(self.runs),
                "FLEET_ROUTER_PATH": str(ROUTER),
                "FLEET_BOOT_WAIT_ATTEMPTS": "1",
                "FLEET_BOOT_WAIT_DELAY": "0",
                "FLEET_SEND_KEY_DELAY": "0",
                "ZHIPU_API_KEY": "test-only",
            }
        )

    def make_executable(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def run_fleet(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(FLEET_UP), *args],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_preset_renders_ranked_visual_order(self) -> None:
        result = self.run_fleet("order", "--preset", "implementation_review")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual([pane["title"] for pane in state["panes"]], ["lead", "build", "verify"])

        rename_titles = [call[-1] for call in self.calls() if call and call[0] == "rename-tab"]
        self.assertEqual(rename_titles, ["lead", "verify", "build"])

        manifest = (self.runs / "fleet-order.manifest").read_text()
        self.assertLess(manifest.index("build=surface:"), manifest.index("verify=surface:"))
        self.assertIn("lead.role_type=codex", manifest)
        self.assertIn("build.role_type=codex", manifest)
        self.assertIn("verify.role_type=reviewer", manifest)
        self.assertIn("workspace_uuid=", manifest)
        self.assertIn("build.phase=BUILD", manifest)
        self.assertIn("verify.phase=VERIFY", manifest)
        state_file = self.runs / "fleet-order.state.json"
        self.assertTrue(state_file.exists())
        self.assertEqual(json.loads(state_file.read_text())["active_phase"], "CONTROL")

    def test_named_duplicate_roles_are_addressable(self) -> None:
        result = self.run_fleet(
            "research-local",
            "triage_scope=triage",
            "triage_sources=triage",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual(
            [pane["title"] for pane in state["panes"]],
            ["lead", "triage_scope", "triage_sources"],
        )
        manifest = (self.runs / "fleet-research-local.manifest").read_text()
        self.assertIn("triage_scope.role_type=triage", manifest)
        self.assertIn("triage_sources.role_type=triage", manifest)

        advance = subprocess.run(
            [
                "python3", str(ROOT / "scripts" / "fleet_state.py"), "advance",
                str(self.runs / "fleet-research-local.manifest"), "RECON",
                "--evidence", "test-scope-approved",
            ],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
        self.assertEqual(advance.returncode, 0, advance.stderr)

        dispatch = subprocess.run(
            ["bash", str(FLEET_DISPATCH), "research-local", "triage_scope", "summarize this"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(dispatch.returncode, 0, dispatch.stderr)
        sends = [call for call in self.calls() if call and call[0] == "send"]
        self.assertTrue(any("run-local-task.sh research-local triage_scope triage" in call[-1] for call in sends))
        duplicate_dispatch = subprocess.run(
            ["bash", str(FLEET_DISPATCH), "research-local", "triage_scope", "second task"],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
        self.assertEqual(duplicate_dispatch.returncode, 75)
        self.assertIn("is busy", duplicate_dispatch.stderr)

        wait_env = self.env.copy()
        wait_env["TREE_BOTH"] = "surface:1 00000000-0000-0000-0000-000000000001"
        bad_wait = subprocess.run(
            [
                "python3", str(ROOT / "scripts" / "fleet_wait.py"), "research-local",
                str(self.runs / "fleet-research-local.manifest"), "1", "missing_instance",
            ],
            cwd=ROOT, env=wait_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
        self.assertEqual(bad_wait.returncode, 2)
        self.assertIn("unknown instance in manifest", bad_wait.stderr)

    def test_frontier_preset_is_reproducible_and_ordered(self) -> None:
        result = self.run_fleet("frontier", "--preset", "frontier_verification")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual(
            [pane["title"] for pane in state["panes"]],
            ["lead", "build", "challenge", "verify"],
        )
        manifest = (self.runs / "fleet-frontier.manifest").read_text()
        self.assertIn("challenge.role_type=glm", manifest)
        self.assertIn("verify.role_type=claude_reviewer", manifest)
        sends = [call[-1] for call in self.calls() if call and call[0] == "send"]
        self.assertTrue(any("run-interactive-agent.sh" in payload for payload in sends))

    def test_dispatch_rejects_surface_uuid_mismatch(self) -> None:
        result = self.run_fleet("identity", "triage")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())
        state["panes"][1]["uuid"] = "99999999-9999-9999-9999-999999999999"
        self.state.write_text(json.dumps(state))
        dispatch = subprocess.run(
            ["bash", str(FLEET_DISPATCH), "identity", "triage", "task"],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
        self.assertEqual(dispatch.returncode, 2)
        self.assertIn("surface identity mismatch", dispatch.stderr)

    def test_heavy_contention_cleans_untransferred_instance_lease(self) -> None:
        result = self.run_fleet("heavy-lock", "worker=code_worker")
        self.assertEqual(result.returncode, 0, result.stderr)
        advance = subprocess.run(
            [
                "python3", str(ROOT / "scripts" / "fleet_state.py"), "advance",
                str(self.runs / "fleet-heavy-lock.manifest"), "BUILD",
                "--evidence", "test-build-approved",
            ],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
        self.assertEqual(advance.returncode, 0, advance.stderr)
        heavy = self.runs / "locks" / "local-heavy.lock"
        heavy.mkdir(parents=True)
        (heavy / "owner").write_text("existing-run\n")
        dispatch = subprocess.run(
            ["bash", str(FLEET_DISPATCH), "heavy-lock", "worker", "task"],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
        self.assertEqual(dispatch.returncode, 75)
        self.assertFalse((self.runs / "locks" / "heavy-lock.worker.lock").exists())

    def test_dispatch_refuses_when_feature_token_budget_is_exhausted(self) -> None:
        router = json.loads(ROUTER.read_text())
        router["limits"]["local_token_budget_per_feature"] = 1000
        budget_router = self.tmp / "router-budget.yaml"
        budget_router.write_text(json.dumps(router))
        self.env["FLEET_ROUTER_PATH"] = str(budget_router)

        result = self.run_fleet("budget", "triage")
        self.assertEqual(result.returncode, 0, result.stderr)
        advance = subprocess.run(
            [
                "python3", str(ROOT / "scripts" / "fleet_state.py"), "advance",
                str(self.runs / "fleet-budget.manifest"), "RECON",
                "--evidence", "test-scope-approved",
            ],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
        self.assertEqual(advance.returncode, 0, advance.stderr)

        seed = subprocess.run(
            [
                "python3", str(ROOT / "scripts" / "fleet_ledger.py"),
                str(self.runs / "fleet-budget.ledger.jsonl"),
                "--run-id", "spent", "--feature", "budget", "--instance", "triage",
                "--role", "triage", "--phase", "RECON", "--status", "succeeded",
                "--task-sha256", "0" * 64, "--exit-code", "0",
                "--prompt-tokens", "900", "--completion-tokens", "200",
            ],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
        self.assertEqual(seed.returncode, 0, seed.stderr)

        dispatch = subprocess.run(
            ["bash", str(FLEET_DISPATCH), "budget", "triage", "one more task"],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
        self.assertEqual(dispatch.returncode, 3, dispatch.stderr)
        self.assertIn("exhausted", dispatch.stderr)

    def test_teardown_deletes_manifest_only_after_workspace_disappears(self) -> None:
        result = self.run_fleet("teardown", "--preset", "small")
        self.assertEqual(result.returncode, 0, result.stderr)
        down = subprocess.run(
            ["bash", str(FLEET_DOWN), "teardown"], cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
        )
        self.assertEqual(down.returncode, 0, down.stderr)
        self.assertFalse((self.runs / "fleet-teardown.manifest").exists())
        archives = list((self.runs / "archive").glob("teardown-*/manifest"))
        self.assertEqual(len(archives), 1)

    def test_duplicate_bare_role_fails_before_cmux(self) -> None:
        result = self.run_fleet("bad", "triage", "triage")
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate instance_id", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.runs / "fleet-bad.manifest").exists())

    def test_existing_manifest_fails_before_cmux(self) -> None:
        manifest = self.runs / "fleet-existing.manifest"
        manifest.write_text("workspace=workspace:old\n")
        result = self.run_fleet("existing", "--preset", "small")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Manifest already exists", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_unready_lead_closes_workspace_and_removes_manifest(self) -> None:
        self.env["CMUX_READY"] = "0"
        result = self.run_fleet("unready", "--preset", "small")
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not reach a recognized input prompt", result.stderr)
        self.assertTrue(json.loads(self.state.read_text()).get("closed"))
        self.assertFalse((self.runs / "fleet-unready.manifest").exists())

    def test_unready_secondary_agent_fails_before_manifest_publish(self) -> None:
        self.env["CMUX_READY"] = "0"
        result = self.run_fleet("unready-worker", "--preset", "implementation_review")
        self.assertEqual(result.returncode, 1)
        self.assertIn("build/codex", result.stderr)
        self.assertTrue(json.loads(self.state.read_text()).get("closed"))
        self.assertFalse((self.runs / "fleet-unready-worker.manifest").exists())


if __name__ == "__main__":
    unittest.main()
