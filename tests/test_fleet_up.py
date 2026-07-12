from __future__ import annotations

import json
import os
from pathlib import Path
import re
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
import subprocess

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
    commit_worktree = os.environ.get("CMUX_COMMIT_WORKTREE_ON_CLOSE")
    if commit_worktree:
        subprocess.run(
            ["git", "-C", commit_worktree, "commit", "--allow-empty", "-q", "-m", "late branch commit"],
            check=True,
        )
    detach_worktree = os.environ.get("CMUX_DETACH_WORKTREE_ON_CLOSE")
    if detach_worktree:
        subprocess.run(["git", "-C", detach_worktree, "switch", "--detach", "-q"], check=True)
        subprocess.run(
            ["git", "-C", detach_worktree, "commit", "--allow-empty", "-q", "-m", "late detached"],
            check=True,
        )
    state["closed"] = True
    save(state)
elif command == "tree":
    if state.get("closed") and os.environ.get("CMUX_TREE_PROBE_ERROR") == "1" and "--all" in args:
        print("probe failed", file=sys.stderr)
        raise SystemExit(2)
    if state.get("closed"):
        if "--all" in args:
            print("window window:1 00000000-0000-0000-0000-000000009999")
            raise SystemExit(0)
        if "--workspace" in args:
            raise SystemExit(1)
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

    def test_teardown_recovers_only_after_confirmed_workspace_absence(self) -> None:
        result = self.run_fleet("recover-absent", "triage")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.runs / "fleet-recover-absent.manifest"
        manifest_text = manifest.read_text()
        workspace = re.search(r"^workspace=(.+)$", manifest_text, re.M).group(1)
        workspace_uuid = re.search(
            r"^workspace_uuid=(.+)$", manifest_text, re.M
        ).group(1)
        surface_uuid = re.search(
            r"^triage\.uuid=(.+)$", manifest_text, re.M
        ).group(1)
        run_id = "recover-absent-run"
        acquired = subprocess.run(
            [
                "python3", str(ROOT / "scripts" / "fleet_leases.py"),
                "acquire", str(self.runs), "--run-id", run_id,
                "--feature", "recover-absent", "--instance", "triage",
                "--role", "triage", "--phase", "RECON",
                "--resource-class", "local_light", "--task-sha256", "a" * 64,
                "--workspace-uuid", workspace_uuid, "--surface-uuid", surface_uuid,
                "--max-local", "3", "--role-limit", "2",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        running = subprocess.run(
            [
                "python3", str(ROOT / "scripts" / "fleet_ledger.py"),
                str(self.runs / "fleet-recover-absent.ledger.jsonl"),
                "--run-id", run_id, "--feature", "recover-absent",
                "--instance", "triage", "--role", "triage", "--phase", "RECON",
                "--status", "running", "--task-sha256", "a" * 64,
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(running.returncode, 0, running.stderr)
        closed = subprocess.run(
            ["cmux", "close-workspace", "--workspace", workspace],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), "recover-absent"],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertTrue(manifest.exists())

        recovered = subprocess.run(
            ["bash", str(FLEET_DOWN), "recover-absent", "--recover-absent"],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse(manifest.exists())
        archived_ledger = next(
            (self.runs / "archive").glob("recover-absent-*/ledger.jsonl")
        )
        events = [json.loads(line) for line in archived_ledger.read_text().splitlines()]
        self.assertEqual(events[-1]["run_id"], run_id)
        self.assertEqual(events[-1]["status"], "abandoned")
        self.assertTrue(
            list((self.runs / "archive" / "leases" / run_id).glob("*.lock"))
        )

    def make_target_repo(self) -> Path:
        target = self.tmp / "target-repo"
        target.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=target, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "t"], check=True)
        (target / "file.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(target), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-q", "-m", "init"], check=True)
        return target

    def writer_worktree(self, feature: str) -> Path:
        manifest = (self.runs / f"fleet-{feature}.manifest").read_text()
        match = re.search(r"^build\.worktree=(.+)$", manifest, re.M)
        self.assertIsNotNone(match, f"no build.worktree entry in manifest:\n{manifest}")
        return Path(match.group(1))

    def test_writer_gets_named_branch_from_target_head(self) -> None:
        target = self.make_target_repo()
        base_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        result = self.run_fleet(
            "wt", "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wt")
        self.assertTrue(worktree.is_dir())
        head = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(head.stdout.strip(), "fleet/wt/build")
        manifest = (self.runs / "fleet-wt.manifest").read_text()
        self.assertIn(f"target_repo={target}", manifest)
        self.assertIn("build.branch=fleet/wt/build", manifest)
        self.assertIn(f"build.base_sha={base_sha}", manifest)
        self.assertIn(f"build.final_sha={base_sha}", manifest)
        self.assertNotIn("verify.worktree=", manifest)
        sends = [call[-1] for call in self.calls() if call and call[0] == "send"]
        self.assertTrue(
            any("cd " in payload and str(worktree) in payload for payload in sends),
            "writer launch command does not enter its worktree",
        )

    def test_teardown_refuses_dirty_worktree_then_removes_clean_one(self) -> None:
        target = self.make_target_repo()
        result = self.run_fleet(
            "wtdown", "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wtdown")

        (worktree / "scratch.txt").write_text("dirty\n", encoding="utf-8")
        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), "wtdown"], cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
        )
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("uncommitted", refused.stderr)
        self.assertTrue((self.runs / "fleet-wtdown.manifest").exists())

        (worktree / "scratch.txt").unlink()
        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), "wtdown"], cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertFalse(worktree.exists())
        self.assertFalse((self.runs / "fleet-wtdown.manifest").exists())
        branch = subprocess.run(
            ["git", "-C", str(target), "show-ref", "--verify", "refs/heads/fleet/wtdown/build"],
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(branch.returncode, 0, "unchanged writer branch should be removed")

    def test_teardown_preserves_advanced_writer_branch_and_archives_final_sha(self) -> None:
        target = self.make_target_repo()
        result = self.run_fleet(
            "wtcommit", "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wtcommit")
        (worktree / "result.txt").write_text("durable\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "result.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(worktree), "commit", "-q", "-m", "agent result"], check=True
        )
        final_sha = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()

        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), "wtcommit"], cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertFalse(worktree.exists())
        branch_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "refs/heads/fleet/wtcommit/build"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.assertEqual(branch_sha, final_sha)
        archived = list((self.runs / "archive").glob("wtcommit-*/manifest"))
        self.assertEqual(len(archived), 1)
        archived_manifest = archived[0].read_text()
        self.assertIn("build.branch=fleet/wtcommit/build", archived_manifest)
        self.assertIn(f"build.final_sha={final_sha}", archived_manifest)

    def test_teardown_refuses_writer_detached_from_durable_branch(self) -> None:
        target = self.make_target_repo()
        result = self.run_fleet(
            "wtdetached", "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wtdetached")
        subprocess.run(["git", "-C", str(worktree), "switch", "--detach", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(worktree), "commit", "--allow-empty", "-q", "-m", "detached"],
            check=True,
        )

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), "wtdetached"], cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
        )
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("not attached", refused.stderr)
        self.assertTrue(worktree.exists())
        self.assertTrue((self.runs / "fleet-wtdetached.manifest").exists())
        branch = subprocess.run(
            ["git", "-C", str(target), "show-ref", "--verify", "refs/heads/fleet/wtdetached/build"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(branch.returncode, 0)

    def test_teardown_rechecks_writer_after_workspace_shutdown(self) -> None:
        target = self.make_target_repo()
        result = self.run_fleet(
            "wtlate", "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wtlate")
        self.env["CMUX_DETACH_WORKTREE_ON_CLOSE"] = str(worktree)

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), "wtlate"], cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
        )
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("after workspace shutdown", refused.stderr)
        self.assertTrue(worktree.exists())
        self.assertTrue((self.runs / "fleet-wtlate.manifest").exists())

    def test_teardown_preserves_state_when_shutdown_probe_fails(self) -> None:
        target = self.make_target_repo()
        result = self.run_fleet(
            "wtprobe", "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wtprobe")
        self.env["CMUX_TREE_PROBE_ERROR"] = "1"

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), "wtprobe"], cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
        )
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn("identity probes failed", refused.stderr)
        self.assertTrue(worktree.exists())
        self.assertTrue((self.runs / "fleet-wtprobe.manifest").exists())

    def test_existing_writer_branch_fails_before_cmux(self) -> None:
        target = self.make_target_repo()
        subprocess.run(
            ["git", "-C", str(target), "branch", "fleet/wtcollision/build"], check=True
        )
        result = self.run_fleet(
            "wtcollision", "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Writer branch already exists", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.runs / "fleet-wtcollision.manifest").exists())

    def test_failed_boot_removes_unchanged_writer_branch(self) -> None:
        target = self.make_target_repo()
        self.env["CMUX_READY"] = "0"
        result = self.run_fleet(
            "wtfailed", "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 1)
        branch = subprocess.run(
            ["git", "-C", str(target), "show-ref", "--verify", "refs/heads/fleet/wtfailed/build"],
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(branch.returncode, 0)
        self.assertFalse((self.runs / "worktrees" / "wtfailed-build").exists())

    def test_failed_boot_preserves_writer_branch_that_advanced_during_shutdown(self) -> None:
        target = self.make_target_repo()
        worktree = self.runs / "worktrees" / "wtfailedcommit-build"
        self.env["CMUX_READY"] = "0"
        self.env["CMUX_COMMIT_WORKTREE_ON_CLOSE"] = str(worktree)
        result = self.run_fleet(
            "wtfailedcommit", "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 1)
        self.assertFalse(worktree.exists())
        branch_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "refs/heads/fleet/wtfailedcommit/build"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        base_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "main"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.assertNotEqual(branch_sha, base_sha)

    def test_target_repo_must_be_a_git_repository(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        result = self.run_fleet(
            "wtbad", "--preset", "implementation_review", "--target-repo", str(plain)
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a git repository", result.stderr.lower())
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.runs / "fleet-wtbad.manifest").exists())

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
