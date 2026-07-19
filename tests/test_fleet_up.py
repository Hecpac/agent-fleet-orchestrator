from __future__ import annotations

from datetime import datetime, timedelta
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FLEET_UP = ROOT / "scripts" / "fleet-up.sh"
FLEET_DISPATCH = ROOT / "scripts" / "fleet-dispatch.sh"
FLEET_SEND = ROOT / "scripts" / "fleet-send.sh"
FLEET_RACE = ROOT / "scripts" / "fleet-race.sh"
FLEET_DOWN = ROOT / "scripts" / "fleet-down.sh"
CMUX_LAUNCHER = ROOT / "scripts" / "fleet_cmux_launcher.py"
MANIFEST_GUARD = ROOT / "scripts" / "fleet_manifest_guard.py"
ROUTER = ROOT / "orchestration" / "router.yaml"
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_control_service  # noqa: E402
import fleet_cmux_launcher  # noqa: E402
import fleet_mission  # noqa: E402
import fleet_mission_state  # noqa: E402
from fleet_safe_paths import SafePathError  # noqa: E402
import workflow_config  # noqa: E402


FAKE_CMUX = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import subprocess
import shlex
import time

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
elif (
    command == "send"
    and os.environ.get("CMUX_FAIL_PROTOCOL_SURFACE") == arg_value("--surface")
    and args[-1].startswith("FLEET_RUN ")
):
    raise SystemExit(1)
elif command == "send-key" and os.environ.get("CMUX_FAIL_SEND_KEY") == "1":
    raise SystemExit(1)
elif command == "send":
    surface = arg_value("--surface")
    payload = args[-1]
    for pane in state["panes"]:
        if pane["surface"] != surface:
            continue
        threshold = int(os.environ.get("CMUX_TRUNCATE_LONG_SEND_THRESHOLD", "0"))
        delivered = payload
        if threshold > 0 and len(payload) >= threshold:
            delivered = payload[:threshold]
            pane["transport_truncated"] = True
        pane["typed_command"] = delivered
        pane["literal_backslash_n"] = payload.endswith("\\n")
        if "fleet_cmux_launcher.py run " in delivered:
            pane["launch_pending"] = True
            pane["launch_executed"] = False
        if payload.startswith("FLEET_RUN ") or "FDP_PROMPT=" in payload:
            pane["pending_submit"] = True
    save(state)
elif command == "send-key":
    surface = arg_value("--surface")
    for pane in state["panes"]:
        if pane["surface"] != surface:
            continue
        terminal_command = pane.pop("typed_command", "")
        if pane.pop("launch_pending", False):
            if os.environ.get("CMUX_SKIP_LAUNCH_EXECUTION") != "1":
                launch_args = shlex.split(terminal_command)
                runs_dir = Path(launch_args[-3])
                if not runs_dir.is_absolute():
                    runs_dir = Path.cwd() / runs_dir
                launch_id = launch_args[-2]
                spec = json.loads(
                    (runs_dir / "cmux-launches" / launch_id / "spec.json").read_text()
                )
                provider_args = spec["argv"]
                if "opencode" in provider_args:
                    pane["source"] = "opencode"
                elif "claude" in provider_args:
                    pane["source"] = "claude"
                else:
                    pane["source"] = "codex"
                subprocess.run(
                    launch_args,
                    cwd=Path.cwd(),
                    env=os.environ.copy(),
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                pane["launch_executed"] = True
        if not pane.pop("pending_submit", False):
            continue
        source = pane.get("source", "codex")
        session_key = "test_" + surface.replace(":", "_")
        hook_dir = Path(os.environ["CMUX_HOOK_DIR"])
        hook_dir.mkdir(parents=True, exist_ok=True)
        session_path = hook_dir / f"{source}-hook-sessions.json"
        sessions = json.loads(session_path.read_text()) if session_path.exists() else {"sessions": {}}
        sessions["sessions"][session_key] = {
            "sessionId": session_key,
            "workspaceId": state["workspace_uuid"],
            "surfaceId": pane["uuid"],
            "updatedAt": 1,
        }
        session_path.write_text(json.dumps(sessions))
        state["event_seq"] = state.get("event_seq", 42) + 1
        event = {
            "id": f"submit-{state['event_seq']}",
            "type": "event",
            "name": "agent.hook.UserPromptSubmit",
            "source": source,
            "workspace_id": state["workspace_uuid"],
            "occurred_at": "2099-01-01T00:00:01.000Z",
            "seq": state["event_seq"],
            "boot_id": "boot-test",
            "payload": {
                "phase": "received",
                "_source": source,
                "session_id": f"{source}-{session_key}",
            },
        }
        with Path(os.environ["CMUX_EVENTS_LOG"]).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
    save(state)
elif command in {"workspace-action", "set-status"}:
    pass
elif command == "read-screen":
    ready = os.environ.get("CMUX_READY", "1")
    surface = arg_value("--surface")
    pane = next((item for item in state["panes"] if item["surface"] == surface), {})
    if ready == "trust":
        print("Do you trust the contents of this directory?\n› 1. Yes, continue\n  2. No, quit")
    elif ready == "claude-login" and pane.get("source") == "claude":
        print("Not logged in · Please run /login\n❯")
    elif pane.get("launch_pending") and not pane.get("launch_executed"):
        print("booting")
    else:
        print("› ready\n❯\nctrl+p commands" if ready == "1" else "booting")
elif command == "events":
    print(json.dumps({
        "type": "ack",
        "protocol": "cmux-events",
        "version": 1,
        "boot_id": "boot-test",
        "replay_count": 0,
        "resume": {"oldest_seq": 1, "latest_seq": 42, "next_seq": 43, "gap": False},
    }), flush=True)
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
    tamper_manifest = os.environ.get("CMUX_TAMPER_MANIFEST_ON_CLOSE")
    if tamper_manifest:
        with Path(tamper_manifest).open("a", encoding="utf-8") as handle:
            handle.write("external_drift=1\n")
    dirty_reader = os.environ.get("CMUX_DIRTY_READER_ON_CLOSE")
    if dirty_reader:
        reader = Path(dirty_reader)
        reader.chmod(reader.stat().st_mode | 0o200)
        (reader / "late-reader-change.txt").write_text("late mutation\n")
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
"""


class FleetUpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.codex_home = self.home / ".codex"
        self.codex_home.mkdir()
        (self.codex_home / "auth.json").write_text(
            '{"test":"authentication-placeholder"}\n', encoding="utf-8"
        )
        (self.codex_home / "auth.json").chmod(0o600)
        self.state = self.tmp / "cmux-state.json"
        self.log = self.tmp / "cmux-log.jsonl"
        self.make_executable("cmux", FAKE_CMUX)
        self.make_executable("codex", "#!/bin/sh\nexit 0\n")
        self.make_executable("claude", "#!/bin/sh\nexit 0\n")
        self.make_executable("opencode", "#!/bin/sh\nexit 0\n")
        self.make_executable("ollama", "#!/bin/sh\nexit 0\n")
        self.make_executable(
            "ps",
            "#!/bin/sh\n"
            'if [ "${CMUX_PS_NO_AGENT:-0}" = 1 ]; then echo shell; '
            "else echo codex claude opencode; fi\n",
        )
        self.events_log = self.tmp / "events.jsonl"
        self.hook_dir = self.tmp / "hooks"
        self.hook_dir.mkdir()
        self.events_log.write_text(
            "".join(
                json.dumps(
                    {
                        "id": f"seed-{source}",
                        "type": "event",
                        "name": "agent.hook.UserPromptSubmit",
                        "source": source,
                        "workspace_id": "00000000-0000-0000-0000-000000000001",
                        "occurred_at": "2099-01-01T00:00:00.000Z",
                        "seq": index + 1,
                        "boot_id": "boot-test",
                        "payload": {
                            "phase": "received",
                            "_source": source,
                            "session_id": "ses_seed",
                        },
                    }
                )
                + "\n"
                for index, source in enumerate(("codex", "opencode", "claude"))
            ),
            encoding="utf-8",
        )
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.bin}:{self.env['PATH']}",
                "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
                "CMUX_STATE": str(self.state),
                "CMUX_LOG": str(self.log),
                "CMUX_EVENTS_LOG": str(self.events_log),
                "CMUX_HOOK_DIR": str(self.hook_dir),
                "FLEET_RUNS_DIR": str(self.runs),
                "FLEET_WORKTREES_DIR": str(self.tmp / "worktrees"),
                "FLEET_ROUTER_PATH": str(ROUTER),
                "FLEET_BOOT_WAIT_ATTEMPTS": "1",
                "FLEET_BOOT_WAIT_DELAY": "0",
                "FLEET_SEND_KEY_DELAY": "0",
                "FLEET_CONFIRM_SUBMIT_TIMEOUT": "1",
                "ZHIPU_API_KEY": "test-only",
                "MINIMAX_API_KEY": "test-only",
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

    def test_dispatch_rejects_noncanonical_run_id_before_any_effect(self) -> None:
        before = sorted(path.relative_to(self.runs) for path in self.runs.rglob("*"))
        result = subprocess.run(
            [
                "bash",
                str(FLEET_DISPATCH),
                "never-started",
                "worker",
                "must not execute",
                "--run-id",
                "00000000-0000-4000-8000-00000000000A",
                "--json",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("canonical lowercase UUID", result.stderr)
        self.assertEqual(
            sorted(path.relative_to(self.runs) for path in self.runs.rglob("*")),
            before,
        )
        self.assertEqual(self.calls(), [])

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def launch_specs(self) -> list[dict[str, object]]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.runs.glob("cmux-launches/*/spec.json"))
        ]

    def launch_commands(self) -> list[str]:
        commands: list[str] = []
        for spec in self.launch_specs():
            environment = " ".join(
                f"{name}={shlex.quote(value)}"
                for name, value in spec["environment"].items()
            )
            command = shlex.join(spec["argv"])
            commands.append(
                f"cd {shlex.quote(spec['cwd'])} && {environment} {command}".strip()
            )
        return commands

    def test_preset_renders_ranked_visual_order(self) -> None:
        result = self.run_fleet("order", "--preset", "implementation_review")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual(
            [pane["title"] for pane in state["panes"]], ["lead", "build", "verify"]
        )

        rename_titles = [
            call[-1] for call in self.calls() if call and call[0] == "rename-tab"
        ]
        self.assertEqual(rename_titles, ["lead", "verify", "build"])

        manifest = (self.runs / "fleet-order.manifest").read_text()
        self.assertLess(
            manifest.index("build=surface:"), manifest.index("verify=surface:")
        )
        self.assertIn("lead.role_type=codex", manifest)
        self.assertIn("build.role_type=codex", manifest)
        self.assertIn("verify.role_type=reviewer", manifest)
        self.assertIn("workspace_uuid=", manifest)
        self.assertIn("build.phase=BUILD", manifest)
        self.assertIn("verify.phase=VERIFY", manifest)
        self.assertIn("identity_group.count=1", manifest)
        self.assertIn("identity_group.1=build,verify", manifest)
        state_file = self.runs / "fleet-order.state.json"
        self.assertTrue(state_file.exists())
        self.assertEqual(json.loads(state_file.read_text())["active_phase"], "CONTROL")

    def test_no_lead_monitor_publishes_a_valid_non_model_manifest(self) -> None:
        self.env["FLEET_NO_LEAD"] = "1"

        result = self.run_fleet("monitor-only", "triage=triage")

        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = (self.runs / "fleet-monitor-only.manifest").read_text()
        self.assertIn("lead.role_type=monitor\n", manifest)
        self.assertIn("lead.runner=monitor\n", manifest)
        self.assertIn("lead.phase=CONTROL\n", manifest)
        self.assertNotIn("lead.tool_access=", manifest)
        self.assertNotIn("lead.provider=", manifest)
        self.assertNotIn("lead.model=", manifest)
        self.assertNotIn("\nlead.hook_source=", manifest)
        empty_keys = {line[:-1] for line in manifest.splitlines() if line.endswith("=")}
        self.assertTrue(
            all(key.endswith(".hook_source") for key in empty_keys), empty_keys
        )
        self.assertIn("triage.runner=local\n", manifest)

    def test_long_launch_uses_short_spec_launcher_and_bound_receipt(self) -> None:
        threshold = 300
        self.env["CMUX_TRUNCATE_LONG_SEND_THRESHOLD"] = str(threshold)

        result = self.run_fleet("async-paste", "--preset", "implementation_review")

        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())
        interactive = [
            pane for pane in state["panes"] if pane["title"] in {"lead", "build"}
        ]
        self.assertEqual(len(interactive), 2)
        self.assertTrue(all(pane.get("launch_executed") for pane in interactive))
        calls = self.calls()
        launch_indexes = [
            index
            for index, call in enumerate(calls)
            if call and call[0] == "send" and "fleet_cmux_launcher.py run " in call[-1]
        ]
        self.assertEqual(len(launch_indexes), 2)
        for index in launch_indexes:
            send = calls[index]
            self.assertLess(len(send[-1]), threshold)
            self.assertFalse(send[-1].endswith("\\n"))
            self.assertEqual(calls[index + 1][0], "send-key")
            self.assertEqual(calls[index + 1][1:], send[1:-1] + ["enter"])

        specs = self.launch_specs()
        self.assertEqual(len(specs), 2)
        self.assertTrue(
            all(len(command) >= threshold for command in self.launch_commands())
        )
        for spec in specs:
            launch_dir = self.runs / "cmux-launches" / spec["launch_id"]
            receipt_path = launch_dir / "accepted.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["state"], "accepted")
            self.assertEqual(receipt["launch_id"], spec["launch_id"])
            self.assertEqual(receipt["spec_sha256"], spec["spec_sha256"])
            self.assertEqual(stat.S_IMODE(launch_dir.stat().st_mode), 0o700)
            for private_file in (launch_dir / "spec.json", receipt_path):
                self.assertEqual(stat.S_IMODE(private_file.stat().st_mode), 0o600)
                self.assertEqual(private_file.stat().st_uid, os.geteuid())
                self.assertEqual(private_file.stat().st_nlink, 1)

    def test_missing_launch_receipt_fails_and_rolls_back_workspace(self) -> None:
        self.env.update(
            {
                "CMUX_SKIP_LAUNCH_EXECUTION": "1",
                "FLEET_LAUNCH_RECEIPT_ATTEMPTS": "1",
                "FLEET_LAUNCH_RECEIPT_DELAY": "0",
            }
        )

        result = self.run_fleet("missing-launch-receipt", "--preset", "small")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not accept its durable launch spec", result.stderr)
        self.assertTrue(json.loads(self.state.read_text()).get("closed"))

    def test_launch_send_key_failure_rolls_back_without_receipt(self) -> None:
        self.env["CMUX_FAIL_SEND_KEY"] = "1"

        result = self.run_fleet("launch-enter-failure", "--preset", "small")

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(json.loads(self.state.read_text()).get("closed"))
        self.assertEqual(list(self.runs.glob("cmux-launches/*/accepted.json")), [])

    def test_accepted_launch_without_live_agent_is_not_ready(self) -> None:
        self.env["CMUX_PS_NO_AGENT"] = "1"

        result = self.run_fleet("accepted-not-ready", "--preset", "small")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not reach a recognized input prompt", result.stderr)
        receipts = list(self.runs.glob("cmux-launches/*/accepted.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())["state"], "accepted")
        self.assertTrue(json.loads(self.state.read_text()).get("closed"))

    def test_dan_preset_publishes_autonomous_visible_roster(self) -> None:
        result = self.run_fleet("dan-mode", "--preset", "dan")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual(
            [pane["title"] for pane in state["panes"]],
            ["lead", "scout", "builder", "challenger", "verifier"],
        )
        manifest = (self.runs / "fleet-dan-mode.manifest").read_text()
        self.assertIn("preset=dan\n", manifest)
        self.assertIn("mode=autonomous\n", manifest)
        self.assertIn("scout.phase=RECON\n", manifest)
        self.assertIn("builder.phase=BUILD\n", manifest)
        self.assertIn("challenger.phase=CHALLENGE\n", manifest)
        self.assertIn("verifier.phase=VERIFY\n", manifest)
        sends = [call[-1] for call in self.calls() if call and call[0] == "send"]
        self.assertFalse(any("No despaches todavía" in payload for payload in sends))
        self.assertTrue(
            any(
                call and call[0] == "set-status" and "ready" in call
                for call in self.calls()
            )
        )

    def test_mission_boot_binds_canonical_mission_id_in_manifest(self) -> None:
        target = self.make_target_repo()
        (target / "linked-file.txt").symlink_to("file.txt")
        subprocess.run(["git", "-C", str(target), "add", "linked-file.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(target), "commit", "-q", "-m", "add tracked symlink"],
            check=True,
        )
        base_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        compiled = workflow_config.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )
        mission_id, _ = fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature="mission-bound",
            objective="verify exact instance control endpoints",
            target_repo=target.resolve(),
            base_sha=base_sha,
            idempotency_key="test:mission-bound",
        )
        self.env["FLEET_MISSION_ID"] = mission_id
        compiled_path = self.runs / "missions" / mission_id / "compiled-workflow.json"
        socket_root = Path(tempfile.mkdtemp(prefix="fleet-control-test-", dir="/tmp"))
        self.env["FLEET_CONTROL_SOCKET_DIR"] = str(socket_root)
        with mock.patch.dict(
            os.environ, {"FLEET_CONTROL_SOCKET_DIR": str(socket_root)}
        ):
            lifecycle = fleet_control_service.ControlLifecycle(
                self.runs, mission_id, preset="dan"
            )
            lifecycle.start()
        self.addCleanup(shutil.rmtree, socket_root, True)
        self.addCleanup(
            lambda: lifecycle.stop() if lifecycle.lifecycle_path.exists() else None
        )
        result = self.run_fleet(
            "mission-bound",
            "--preset",
            "dan",
            "--target-repo",
            str(target),
            "--expected-base-sha",
            base_sha,
            "--compiled-workflow",
            str(compiled_path),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = (self.runs / "fleet-mission-bound.manifest").read_text()
        self.assertIn(f"mission_id={mission_id}\n", manifest)
        self.assertIn(f"base_sha={base_sha}\n", manifest)
        reader_workspaces: dict[str, Path] = {}
        for instance in ("scout", "challenger", "verifier"):
            match = re.search(rf"^{instance}\.workspace=(.+)$", manifest, re.M)
            self.assertIsNotNone(match, manifest)
            workspace = Path(match.group(1))
            reader_workspaces[instance] = workspace
            self.assertEqual(
                workspace,
                (self.tmp / "worktrees" / f"mission-bound-{instance}").resolve(),
            )
            self.assertIn(f"{instance}.workspace_kind=isolated-read-clone\n", manifest)
            self.assertIn(f"{instance}.workspace_base_sha={base_sha}\n", manifest)
            self.assertIn(f"{instance}.workspace_publication=none\n", manifest)
            self.assertNotIn(f"{instance}.branch=", manifest)
            self.assertNotIn(f"{instance}.publication_state=", manifest)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(workspace), "rev-parse", "HEAD"],
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip(),
                base_sha,
            )
            self.assertNotEqual(
                subprocess.run(
                    ["git", "-C", str(workspace), "symbolic-ref", "-q", "HEAD"],
                    text=True,
                    capture_output=True,
                    check=False,
                ).returncode,
                0,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(workspace), "remote"],
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip(),
                "",
            )
            self.assertEqual(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(workspace),
                        "rev-parse",
                        "--path-format=absolute",
                        "--git-common-dir",
                    ],
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip(),
                str(workspace / ".git"),
            )
            for current, directories, files in os.walk(workspace, followlinks=False):
                for name in directories + files:
                    path = Path(current) / name
                    if path.is_symlink():
                        continue
                    self.assertEqual(path.stat().st_mode & 0o222, 0, str(path))
            self.assertEqual(workspace.stat().st_mode & 0o222, 0)
        lifecycle_value = lifecycle._read_lifecycle()
        base_socket = lifecycle_value["socket"]
        scout_socket = lifecycle_value["instance_sockets"]["scout"]
        builder_socket = lifecycle_value["instance_sockets"]["builder"]
        self.assertNotEqual(scout_socket, builder_socket)
        self.assertIn(f"lead.control_socket={base_socket}\n", manifest)
        self.assertIn(f"scout.control_socket={scout_socket}\n", manifest)
        self.assertIn(f"builder.control_socket={builder_socket}\n", manifest)
        sends = self.launch_commands()
        scout_launches = [
            payload
            for payload in sends
            if "run-interactive-agent.sh" in payload and " codex_candidate " in payload
        ]
        self.assertEqual(len(scout_launches), 1)
        scout_launch = scout_launches[0]
        self.assertNotIn("--sandbox read-only", scout_launch)
        self.assertIn("default_permissions", scout_launch)
        self.assertIn("fleet_reader", scout_launch)
        self.assertIn(":minimal", scout_launch)
        self.assertNotIn(":root", scout_launch)
        self.assertIn(str(self.runs), scout_launch)
        self.assertIn("deny", scout_launch)
        self.assertIn("unix_sockets", scout_launch)
        self.assertIn("mode=", scout_launch)
        self.assertIn("limited", scout_launch)
        self.assertIn("trust_level", scout_launch)
        self.assertIn("untrusted", scout_launch)
        self.assertIn("--dangerously-bypass-hook-trust", scout_launch)
        self.assertIn(mission_id, scout_launch)
        self.assertIn(scout_socket, scout_launch)
        self.assertNotIn(builder_socket, scout_launch)
        self.assertNotIn(base_socket, scout_launch)
        builder_launch = next(
            payload
            for payload in sends
            if "run-interactive-agent.sh" in payload
            and " codex " in payload
            and builder_socket in payload
        )
        self.assertIn("fleet_writer", builder_launch)
        self.assertIn(":minimal", builder_launch)
        self.assertNotIn(":root", builder_launch)
        self.assertIn(str(self.runs), builder_launch)
        self.assertIn("workspace_roots", builder_launch)
        self.assertIn(builder_socket, builder_launch)
        self.assertNotIn(scout_socket, builder_launch)
        self.assertNotIn(base_socket, builder_launch)
        self.assertNotIn("sandbox_workspace_write.writable_roots", builder_launch)
        builder_match = re.search(r"^builder\.worktree=(.+)$", manifest, re.M)
        self.assertIsNotNone(builder_match, manifest)
        specialist_workspaces = {
            **reader_workspaces,
            "builder": Path(builder_match.group(1)),
        }
        specialist_launches = [
            payload for payload in sends if "run-interactive-agent.sh" in payload
        ]
        for instance, workspace in specialist_workspaces.items():
            self.assertTrue(
                any(
                    payload.startswith(f"cd {workspace} && ")
                    for payload in specialist_launches
                ),
                f"{instance} did not launch from {workspace}",
            )
        self.assertIn(str(reader_workspaces["scout"]), scout_launch)
        self.assertTrue(scout_launch.startswith(f"cd {reader_workspaces['scout']} && "))
        for forbidden_cwd in (target.resolve(), ROOT, self.runs):
            self.assertFalse(scout_launch.startswith(f"cd {forbidden_cwd} && "))
        lead_launch = next(
            payload
            for payload in specialist_launches
            if base_socket in payload and builder_socket not in payload
        )
        self.assertFalse(
            any(
                lead_launch.startswith(f"cd {workspace} && ")
                for workspace in specialist_workspaces.values()
            )
        )

        # A legacy, non-mission fleet keeps the established read-only flag.
        self.state.unlink(missing_ok=True)
        self.env.pop("FLEET_MISSION_ID")
        legacy = self.run_fleet("mission-unbound", "--preset", "dan")
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        legacy_sends = self.launch_commands()
        legacy_scouts = [
            payload
            for payload in legacy_sends
            if "run-interactive-agent.sh" in payload and " codex_candidate " in payload
        ]
        self.assertTrue(
            any(
                "--sandbox read-only" in payload
                and "--dangerously-bypass-hook-trust" not in payload
                for payload in legacy_scouts
            )
        )

        self.env["FLEET_MISSION_ID"] = "not-a-uuid"
        invalid = self.run_fleet("mission-invalid", "--preset", "dan")
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("Invalid FLEET_MISSION_ID", invalid.stderr)

    def test_mission_local_dispatch_uses_controller_runner_from_reader_snapshot(
        self,
    ) -> None:
        target = self.make_target_repo("target-mission-local")
        base_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        compiled = workflow_config.compile_path(ROOT / "workflows" / "research.yaml")
        mission_id, _ = fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature="mission-local",
            objective="dispatch a prompt-only worker from an isolated snapshot",
            target_repo=target.resolve(),
            base_sha=base_sha,
            idempotency_key="test:mission-local-dispatch",
        )
        self.env["FLEET_MISSION_ID"] = mission_id
        compiled_path = self.runs / "missions" / mission_id / "compiled-workflow.json"
        socket_root = Path(tempfile.mkdtemp(prefix="fleet-control-test-", dir="/tmp"))
        self.env["FLEET_CONTROL_SOCKET_DIR"] = str(socket_root)
        with mock.patch.dict(
            os.environ, {"FLEET_CONTROL_SOCKET_DIR": str(socket_root)}
        ):
            lifecycle = fleet_control_service.ControlLifecycle(
                self.runs, mission_id, preset="research"
            )
            lifecycle.start()
        self.addCleanup(shutil.rmtree, socket_root, True)
        self.addCleanup(
            lambda: lifecycle.stop() if lifecycle.lifecycle_path.exists() else None
        )
        result = self.run_fleet(
            "mission-local",
            "--preset",
            "research",
            "--target-repo",
            str(target),
            "--expected-base-sha",
            base_sha,
            "--compiled-workflow",
            str(compiled_path),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for spec in self.launch_specs():
            self.assertEqual(
                spec["environment"].get("FLEET_COMPILED_WORKFLOW"),
                str(compiled_path.resolve()),
            )
            self.assertEqual(
                spec["environment"].get("FLEET_COMPILED_DIGEST"),
                compiled["compiled_digest"],
            )
        manifest_path = self.runs / "fleet-mission-local.manifest"
        manifest = manifest_path.read_text(encoding="utf-8")
        workspace = Path(
            re.search(r"^triage_scope\.workspace=(.+)$", manifest, re.M).group(1)
        )
        self.assertIn("triage_scope.workspace_kind=isolated-read-clone\n", manifest)
        idle_payload = next(
            call[-1]
            for call in self.calls()
            if call[:1] == ["send"] and "worker: triage_scope" in call[-1]
        )
        self.assertTrue(idle_payload.startswith(f"cd {workspace} && "), idle_payload)
        advance = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_state.py"),
                "advance",
                str(manifest_path),
                "RECON",
                "--evidence",
                "mission-local-scope",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(advance.returncode, 0, advance.stderr)
        self.env["FLEET_ROUTER_PATH"] = str(self.tmp / "missing-live-router.json")
        outside_compiled = self.tmp / "identical-external-compiled.json"
        outside_compiled.write_bytes(compiled_path.read_bytes())
        outside_compiled.chmod(0o600)
        calls_before_rejection = list(self.calls())
        rejected_authority = subprocess.run(
            [
                "bash",
                str(FLEET_DISPATCH),
                "mission-local",
                "triage_scope",
                "must not dispatch from an external compiled path",
                "--json",
            ],
            cwd=ROOT,
            env={
                **self.env,
                "FLEET_COMPILED_WORKFLOW": str(outside_compiled),
                "FLEET_COMPILED_DIGEST": compiled["compiled_digest"],
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(rejected_authority.returncode, 2)
        self.assertIn("non-canonical compiled authority", rejected_authority.stderr)
        self.assertEqual(self.calls(), calls_before_rejection)

        deterministic_run = "00000000-0000-4000-8000-00000000000a"
        dispatched = subprocess.run(
            [
                "bash",
                str(FLEET_DISPATCH),
                "mission-local",
                "triage_scope",
                "summarize supplied evidence",
                "--run-id",
                deterministic_run,
                "--json",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
        self.assertEqual(json.loads(dispatched.stdout)["run_id"], deterministic_run)
        runner_payload = next(
            call[-1]
            for call in reversed(self.calls())
            if call[:1] == ["send"] and "run-local-task.sh" in call[-1]
        )
        self.assertIn(str(ROOT / "scripts" / "run-local-task.sh"), runner_payload)
        self.assertNotIn("./scripts/run-local-task.sh", runner_payload)
        self.assertIn(
            f"FLEET_COMPILED_WORKFLOW={compiled_path.resolve()}", runner_payload
        )
        self.assertIn(
            f"FLEET_COMPILED_DIGEST={compiled['compiled_digest']}", runner_payload
        )
        self.assertNotIn("FLEET_ROUTER_PATH=", runner_payload)

    def test_execution_profiles_are_manifested_without_changing_declared_tools(
        self,
    ) -> None:
        native = self.run_fleet("profile-native", "--preset", "implementation_review")
        self.assertEqual(native.returncode, 0, native.stderr)
        native_manifest = (self.runs / "fleet-profile-native.manifest").read_text()
        self.assertIn("manifest_contract_version=3\n", native_manifest)
        self.assertIn("execution_profile=native\n", native_manifest)
        self.assertIn("tracking_protocol=control-v1\n", native_manifest)
        native_tools = sorted(
            line for line in native_manifest.splitlines() if ".tool_access=" in line
        )

        # Use a fresh fake-cmux state after the independent native fleet.
        self.state.unlink(missing_ok=True)
        sandboxed = self.run_fleet(
            "profile-sandboxed",
            "--preset",
            "implementation_review",
            "--execution-profile",
            "sandboxed",
        )
        self.assertEqual(sandboxed.returncode, 0, sandboxed.stderr)
        sandboxed_manifest = (
            self.runs / "fleet-profile-sandboxed.manifest"
        ).read_text()
        self.assertIn("execution_profile=sandboxed\n", sandboxed_manifest)
        sandboxed_tools = sorted(
            line.replace("profile-sandboxed", "profile-native")
            for line in sandboxed_manifest.splitlines()
            if ".tool_access=" in line
        )
        self.assertEqual(native_tools, sandboxed_tools)
        launches = self.launch_commands()
        self.assertTrue(
            any("FLEET_EXECUTION_PROFILE=sandboxed" in value for value in launches)
        )

    def test_regulated_profile_rejects_unbound_legacy_boot(self) -> None:
        result = self.run_fleet(
            "profile-regulated",
            "--preset",
            "implementation_review",
            "--execution-profile",
            "regulated",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("regulated execution requires", result.stderr)
        self.assertFalse(
            self.calls(), "profile validation must happen before CMUX mutation"
        )

    def test_default_lead_does_not_fallback_to_claude_without_opt_in(self) -> None:
        self.make_executable("codex", "#!/bin/sh\nexit 1\n")
        refused = self.run_fleet("fallback-refused", "--preset", "small")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("no lead provider available: codex", refused.stderr)
        self.assertNotIn("claude", refused.stderr)
        self.assertFalse(self.calls(), "fallback refusal must precede CMUX")

        accepted = self.run_fleet(
            "fallback-opt-in", "--preset", "small", "--allow-fallback"
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        manifest = (self.runs / "fleet-fallback-opt-in.manifest").read_text()
        self.assertIn("lead.role_type=claude\n", manifest)

    def test_compiled_router_snapshot_ignores_live_drift_and_rejects_tamper_pre_effect(
        self,
    ) -> None:
        compiled_path = self.tmp / "compiled-workflow.json"
        compiled = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "workflow_config.py"),
                "compile",
                str(ROOT / "workflows" / "implementation.yaml"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        compiled_path.write_text(compiled, encoding="utf-8")
        result = self.run_fleet(
            "compiled-bound",
            "--preset",
            "dan",
            "--compiled-workflow",
            str(compiled_path),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = (self.runs / "fleet-compiled-bound.manifest").read_text(
            encoding="utf-8"
        )
        compiled_value = json.loads(compiled)
        self.assertIn(
            f"compiled_digest={compiled_value['compiled_digest']}\n", manifest
        )
        self.assertIn(f"router_digest={compiled_value['router_digest']}\n", manifest)
        self.assertRegex(manifest, r"(?m)^roster_digest=[0-9a-f]{64}$")
        self.assertIn(
            f"launch_digest={compiled_value['resolved']['launch_digest']}\n", manifest
        )

        drifted_router = json.loads(ROUTER.read_text(encoding="utf-8"))
        drifted_router["defaults"]["preset"] = "research"
        drifted_path = self.tmp / "router-drift.json"
        drifted_path.write_text(json.dumps(drifted_router), encoding="utf-8")
        self.state.unlink(missing_ok=True)
        self.log.unlink(missing_ok=True)
        self.env["FLEET_ROUTER_PATH"] = str(drifted_path)
        from_snapshot = self.run_fleet(
            "compiled-drift",
            "--preset",
            "dan",
            "--compiled-workflow",
            str(compiled_path),
        )
        self.assertEqual(from_snapshot.returncode, 0, from_snapshot.stderr)
        drift_manifest = (self.runs / "fleet-compiled-drift.manifest").read_text()
        self.assertIn(
            f"router_digest={compiled_value['router_digest']}\n", drift_manifest
        )
        self.assertIn("preset=dan\n", drift_manifest)

        tampered = json.loads(compiled)
        tampered["router_snapshot"]["roles"]["codex"]["command"].append(
            "--tampered-after-compile"
        )
        tampered["router_digest"] = workflow_config.sha256(tampered["router_snapshot"])
        tampered["compiled_digest"] = workflow_config.sha256(
            {key: value for key, value in tampered.items() if key != "compiled_digest"}
        )
        tampered_path = self.tmp / "compiled-tampered.json"
        tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
        self.state.unlink(missing_ok=True)
        self.log.unlink(missing_ok=True)
        rejected = self.run_fleet(
            "compiled-tamper",
            "--preset",
            "dan",
            "--compiled-workflow",
            str(tampered_path),
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("snapshot plan binding", rejected.stderr)
        self.assertFalse(self.calls(), "snapshot tamper must fail before CMUX mutation")

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
        self.assertIn("identity_group.count=0", manifest)
        self.assertNotIn("identity_group.1=", manifest)

        advance = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_state.py"),
                "advance",
                str(self.runs / "fleet-research-local.manifest"),
                "RECON",
                "--evidence",
                "test-scope-approved",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(advance.returncode, 0, advance.stderr)

        dispatch = subprocess.run(
            [
                "bash",
                str(FLEET_DISPATCH),
                "research-local",
                "triage_scope",
                "summarize this",
                "--json",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(dispatch.returncode, 0, dispatch.stderr)
        dispatch_payload = json.loads(dispatch.stdout)
        self.assertEqual(dispatch_payload["feature"], "research-local")
        self.assertEqual(dispatch_payload["instance"], "triage_scope")
        self.assertEqual(dispatch_payload["runner"], "local")
        self.assertIn("local token spend:", dispatch.stderr)
        ledger = [
            json.loads(line)
            for line in (self.runs / "fleet-research-local.ledger.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual(ledger[-1]["provider"], "ollama")
        self.assertEqual(ledger[-1]["model"], "gemma3:4b")
        self.assertIsNone(ledger[-1]["variant"])
        sends = [call for call in self.calls() if call and call[0] == "send"]
        local_send = next(
            call[-1]
            for call in sends
            if "run-local-task.sh research-local triage_scope triage" in call[-1]
        )
        self.assertIn(f"FLEET_RUNS_DIR={self.runs}", local_send)
        self.assertIn(f"FLEET_ROUTER_PATH={ROUTER}", local_send)
        self.assertIn(str(ROOT / "scripts" / "run-local-task.sh"), local_send)
        self.assertNotIn("./scripts/run-local-task.sh", local_send)
        self.assertTrue(any("ollama gemma3:4b" in call[-1] for call in sends))
        duplicate_dispatch = subprocess.run(
            [
                "bash",
                str(FLEET_DISPATCH),
                "research-local",
                "triage_scope",
                "second task",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(duplicate_dispatch.returncode, 75)
        self.assertIn("is busy", duplicate_dispatch.stderr)

        wait_env = self.env.copy()
        wait_env["TREE_BOTH"] = "surface:1 00000000-0000-0000-0000-000000000001"
        bad_wait = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_wait.py"),
                "research-local",
                str(self.runs / "fleet-research-local.manifest"),
                "1",
                "missing_instance",
            ],
            cwd=ROOT,
            env=wait_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
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
        self.assertIn("lead.model=gpt-5.6-sol", manifest)
        self.assertIn("build.model=gpt-5.6-sol", manifest)
        self.assertIn("challenge.role_type=glm", manifest)
        self.assertIn("challenge.provider=zai", manifest)
        self.assertIn("challenge.model=glm-5.2", manifest)
        self.assertIn("challenge.hook_source=opencode", manifest)
        self.assertIn("verify.role_type=claude_reviewer", manifest)
        self.assertIn("verify.model=claude-fable-5", manifest)
        sends = self.launch_commands()
        self.assertTrue(any("run-interactive-agent.sh" in payload for payload in sends))
        codex_launches = [payload for payload in sends if " codex " in payload]
        self.assertTrue(codex_launches)
        self.assertTrue(
            all("--model gpt-5.6-sol" in payload for payload in codex_launches)
        )

    def test_fleet_dialogue_preset_materializes_one_writer_and_three_identity_diverse_gates(
        self,
    ) -> None:
        target = self.make_target_repo()
        result = self.run_fleet(
            "fdp2-roster",
            "--preset",
            "fleet_dialogue",
            "--target-repo",
            str(target),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual(
            [pane["title"] for pane in state["panes"]],
            ["lead", "maker", "checker", "challenge", "verify"],
        )
        manifest = (self.runs / "fleet-fdp2-roster.manifest").read_text()
        required = (
            "preset=fleet_dialogue",
            "maker.role_type=codex",
            "maker.authority=write",
            "maker.phase=BUILD",
            "checker.role_type=minimax_checker",
            "checker.authority=advisory",
            "checker.phase=BUILD",
            "checker.variant=none",
            "challenge.role_type=glm",
            "challenge.phase=CHALLENGE",
            "verify.role_type=claude_reviewer",
            "verify.phase=VERIFY",
            "identity_group.count=1",
            "identity_group.1=maker,checker,challenge,verify",
        )
        for contract in required:
            self.assertIn(contract, manifest)
        self.assertIn("maker.worktree=", manifest)
        self.assertNotIn("checker.worktree=", manifest)
        sends = self.launch_commands()
        checker_boots = [
            payload for payload in sends if "--agent minimax-checker" in payload
        ]
        self.assertTrue(checker_boots)
        for payload in checker_boots:
            self.assertNotIn("--variant", payload)
            self.assertNotIn("minimax/MiniMax-M3", payload)
        maker_boots = [
            payload
            for payload in sends
            if "codex" in payload and "--sandbox workspace-write" in payload
        ]
        self.assertTrue(maker_boots)
        for payload in maker_boots:
            self.assertNotIn("sandbox_workspace_write.writable_roots", payload)
            self.assertIn("trust_level", payload)
            self.assertIn("untrusted", payload)
            self.assertNotIn("danger-full-access", payload)

        state_advance = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_state.py"),
                "advance",
                str(self.runs / "fleet-fdp2-roster.manifest"),
                "BUILD",
                "--evidence",
                "variant-propagation-test",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(state_advance.returncode, 0, state_advance.stderr)
        sent = subprocess.run(
            ["bash", str(FLEET_SEND), "fdp2-roster", "checker", "check contract"],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        lifecycle = [
            json.loads(line)
            for line in (self.runs / "fleet-fdp2-roster.ledger.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(lifecycle[-1]["variant"], "none")

    def test_project_trust_gate_is_not_mistaken_for_codex_readiness(self) -> None:
        self.env["CMUX_READY"] = "trust"
        result = self.run_fleet("trust-gate", "--preset", "implementation_review")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unresolved project trust gate", result.stderr)
        self.assertFalse((self.runs / "fleet-trust-gate.manifest").exists())
        state = json.loads(self.state.read_text())
        self.assertTrue(
            state.get("closed"), "failed boot must close its CMUX workspace"
        )

    def test_non_lead_isolated_healthcheck_fails_before_workspace_creation(
        self,
    ) -> None:
        self.make_executable(
            "claude",
            '#!/bin/sh\ncase "$*" in *"auth status"*) exit 9;; esac\nexit 0\n',
        )
        result = self.run_fleet("claude-auth-fail", "--preset", "frontier_verification")
        self.assertEqual(result.returncode, 2)
        self.assertIn("verify/claude_reviewer", result.stderr)
        self.assertIn("isolated runtime healthcheck", result.stderr)
        self.assertFalse(
            self.state.exists(), "preflight failure must not create a workspace"
        )

    def test_provider_auth_screen_is_not_mistaken_for_readiness(self) -> None:
        self.env["CMUX_READY"] = "claude-login"
        result = self.run_fleet(
            "claude-login-screen", "--preset", "frontier_verification"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fatal provider/authentication screen", result.stderr)
        self.assertIn("Not logged in", result.stderr)
        self.assertFalse((self.runs / "fleet-claude-login-screen.manifest").exists())
        state = json.loads(self.state.read_text())
        self.assertTrue(
            state.get("closed"), "failed boot must close its CMUX workspace"
        )

    def test_frontier_send_returns_exact_run_and_rejects_second_active_turn(
        self,
    ) -> None:
        result = self.run_fleet("frontier-send", "agent=codex_candidate")
        self.assertEqual(result.returncode, 0, result.stderr)
        advance = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_state.py"),
                "advance",
                str(self.runs / "fleet-frontier-send.manifest"),
                "BUILD",
                "--evidence",
                "frontier-send-test",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(advance.returncode, 0, advance.stderr)
        sent = subprocess.run(
            ["bash", str(FLEET_SEND), "frontier-send", "agent", "bounded task"],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        run_id = re.search(r"sent run_id=([^ ]+)", sent.stdout).group(1)
        ledger = [
            json.loads(line)
            for line in (self.runs / "fleet-frontier-send.ledger.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual(ledger[-1]["run_id"], run_id)
        self.assertEqual(ledger[-1]["event_boot_id"], "boot-test")
        self.assertEqual(ledger[-1]["after_seq"], 42)
        lock = self.runs / "locks" / "frontier-send.agent.lock"
        self.assertEqual(
            json.loads((lock / "lease.json").read_text())["run_id"], run_id
        )
        sends = [call[-1] for call in self.calls() if call and call[0] == "send"]
        pointers = [
            payload for payload in sends if payload.startswith(f"FLEET_RUN {run_id}: ")
        ]
        self.assertEqual(len(pointers), 1)
        pointer = pointers[0]
        self.assertNotIn("\n", pointer)
        self.assertNotIn("\\", pointer)
        prompt_path = Path(re.search(r"open the file (\S+) ", pointer).group(1))
        self.assertTrue(prompt_path.is_file())
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn(f"FLEET_RESULT:{run_id}:<STATUS>", prompt_text)
        self.assertIn(f"FLEET_RESULT:{run_id}:<STATUS>", pointer)
        self.assertLess(len(pointer), 600)

        duplicate = subprocess.run(
            ["bash", str(FLEET_SEND), "frontier-send", "agent", "second task"],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(duplicate.returncode, 75, duplicate.stderr)

        abandoned = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts" / "fleet-abandon.sh"),
                "frontier-send",
                "agent",
                run_id,
                "test_cleanup",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(abandoned.returncode, 0, abandoned.stderr)
        self.assertFalse(lock.exists())

    def test_frontier_send_key_failure_retains_indeterminate_lease(self) -> None:
        result = self.run_fleet("frontier-send-fail", "agent=codex_candidate")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.runs / "fleet-frontier-send-fail.manifest"
        advance = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_state.py"),
                "advance",
                str(manifest),
                "BUILD",
                "--evidence",
                "frontier-send-failure-test",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(advance.returncode, 0, advance.stderr)
        failed_env = {**self.env, "CMUX_FAIL_SEND_KEY": "1"}
        sent = subprocess.run(
            ["bash", str(FLEET_SEND), "frontier-send-fail", "agent", "bounded task"],
            cwd=ROOT,
            env=failed_env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(sent.returncode, 1, sent.stderr)
        ledger = [
            json.loads(line)
            for line in (self.runs / "fleet-frontier-send-fail.ledger.jsonl")
            .read_text()
            .splitlines()
        ]
        terminal = ledger[-1]
        self.assertEqual(terminal["status"], "indeterminate")
        self.assertEqual(terminal["reason"], "frontier_send_transfer_unconfirmed")
        self.assertTrue(terminal["lease_retained"])
        lock = self.runs / "locks" / "frontier-send-fail.agent.lock"
        self.assertTrue(lock.exists())
        abandoned = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts" / "fleet-abandon.sh"),
                "frontier-send-fail",
                "agent",
                terminal["run_id"],
                "test_cleanup",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(abandoned.returncode, 0, abandoned.stderr)
        self.assertFalse(lock.exists())

    def test_race_partial_dispatch_reports_exact_runs_and_retains_leases(self) -> None:
        race_env = {**self.env, "CMUX_FAIL_PROTOCOL_SURFACE": "surface:2"}
        result = subprocess.run(
            [
                "bash",
                str(FLEET_RACE),
                "partial-dispatch",
                "bounded task",
                "first=codex_candidate",
                "second=codex_candidate",
                "--timeout",
                "1",
            ],
            cwd=ROOT,
            env=race_env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("frontier transfer indeterminate run_id=", result.stderr)
        self.assertIn(
            "Race dispatch stopped after starting these exact runs", result.stderr
        )
        self.assertRegex(
            result.stderr,
            r"fleet-abandon\.sh race-partial-dispatch first [0-9a-f-]+",
        )
        ledger_path = self.runs / "fleet-race-partial-dispatch.ledger.jsonl"
        events = [json.loads(line) for line in ledger_path.read_text().splitlines()]
        latest = {}
        for event in events:
            latest[event["run_id"]] = event
        self.assertEqual(len(latest), 2)
        self.assertEqual(
            sorted(event["status"] for event in latest.values()),
            ["authorized", "indeterminate"],
        )
        for event in latest.values():
            lock = (
                self.runs / "locks" / f"race-partial-dispatch.{event['instance']}.lock"
            )
            self.assertTrue(lock.exists())
            cleanup = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts" / "fleet-abandon.sh"),
                    "race-partial-dispatch",
                    event["instance"],
                    event["run_id"],
                    "test_cleanup",
                ],
                cwd=ROOT,
                env=self.env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
            self.assertFalse(lock.exists())

    def test_dispatch_rejects_surface_uuid_mismatch(self) -> None:
        result = self.run_fleet("identity", "triage")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())
        state["panes"][1]["uuid"] = "99999999-9999-9999-9999-999999999999"
        self.state.write_text(json.dumps(state))
        dispatch = subprocess.run(
            ["bash", str(FLEET_DISPATCH), "identity", "triage", "task"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(dispatch.returncode, 2)
        self.assertIn("surface identity mismatch", dispatch.stderr)

    def test_heavy_contention_cleans_untransferred_instance_lease(self) -> None:
        result = self.run_fleet("heavy-lock", "worker=code_worker")
        self.assertEqual(result.returncode, 0, result.stderr)
        advance = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_state.py"),
                "advance",
                str(self.runs / "fleet-heavy-lock.manifest"),
                "BUILD",
                "--evidence",
                "test-build-approved",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(advance.returncode, 0, advance.stderr)
        heavy = self.runs / "locks" / "local-heavy.lock"
        heavy.mkdir(parents=True)
        (heavy / "owner").write_text("existing-run\n")
        dispatch = subprocess.run(
            ["bash", str(FLEET_DISPATCH), "heavy-lock", "worker", "task"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
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
                "python3",
                str(ROOT / "scripts" / "fleet_state.py"),
                "advance",
                str(self.runs / "fleet-budget.manifest"),
                "RECON",
                "--evidence",
                "test-scope-approved",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(advance.returncode, 0, advance.stderr)

        seed = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_ledger.py"),
                str(self.runs / "fleet-budget.ledger.jsonl"),
                "--run-id",
                "spent",
                "--feature",
                "budget",
                "--instance",
                "triage",
                "--role",
                "triage",
                "--phase",
                "RECON",
                "--status",
                "succeeded",
                "--task-sha256",
                "0" * 64,
                "--exit-code",
                "0",
                "--prompt-tokens",
                "900",
                "--completion-tokens",
                "200",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(seed.returncode, 0, seed.stderr)

        dispatch = subprocess.run(
            ["bash", str(FLEET_DISPATCH), "budget", "triage", "one more task"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(dispatch.returncode, 3, dispatch.stderr)
        self.assertIn("exhausted", dispatch.stderr)

    def test_teardown_deletes_manifest_only_after_workspace_disappears(self) -> None:
        result = self.run_fleet("teardown", "--preset", "small")
        self.assertEqual(result.returncode, 0, result.stderr)
        down = subprocess.run(
            ["bash", str(FLEET_DOWN), "teardown"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(down.returncode, 0, down.stderr)
        self.assertFalse((self.runs / "fleet-teardown.manifest").exists())
        archives = list((self.runs / "archive").glob("teardown-*/manifest"))
        self.assertEqual(len(archives), 1)

    def test_teardown_archives_dialogue_ledger_and_payloads(self) -> None:
        result = self.run_fleet("dialogue-archive", "--preset", "small")
        self.assertEqual(result.returncode, 0, result.stderr)
        dialogue_ledger = self.runs / "fleet-dialogue-archive.dialogue.jsonl"
        dialogue_ledger.write_text('{"message_id":"message-1"}\n', encoding="utf-8")
        payload_store = self.runs / "dialogue" / "dialogue-archive" / "payloads"
        payload_store.mkdir(parents=True)
        (payload_store / ("a" * 64)).write_bytes(b"durable payload")

        down = subprocess.run(
            ["bash", str(FLEET_DOWN), "dialogue-archive"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(down.returncode, 0, down.stderr)
        archive = next((self.runs / "archive").glob("dialogue-archive-*"))
        self.assertEqual(
            (archive / "dialogue.jsonl").read_text(encoding="utf-8"),
            '{"message_id":"message-1"}\n',
        )
        self.assertEqual(
            (archive / "dialogue" / "payloads" / ("a" * 64)).read_bytes(),
            b"durable payload",
        )

    def test_fdp2_teardown_requires_terminal_and_archives_offline_receipt(self) -> None:
        target = self.make_target_repo()
        result = self.run_fleet(
            "fdp2-archive",
            "--preset",
            "fleet_dialogue",
            "--target-repo",
            str(target),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.runs / "fleet-fdp2-archive.manifest"
        advance = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_state.py"),
                "advance",
                str(manifest),
                "BUILD",
                "--evidence",
                "fdp2-scope-approved",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(advance.returncode, 0, advance.stderr)
        task_spec = Path(self.tempdir.name) / "fdp2-archive-task-spec.json"
        task_spec.write_text(
            json.dumps(
                {
                    "objective": "exercise FDP-2 teardown",
                    "negative_scope": ["do not modify the target repository"],
                    "acceptance_criteria": [
                        "terminal teardown archives a verifiable receipt"
                    ],
                }
            ),
            encoding="utf-8",
        )
        started = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_dialogue_controller.py"),
                "start",
                str(self.runs),
                "--feature",
                "fdp2-archive",
                "--idempotency-key",
                "start-archive-test",
                "--spec-file",
                str(task_spec),
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(started.returncode, 0, started.stderr)

        active_down = subprocess.run(
            ["bash", str(FLEET_DOWN), "fdp2-archive"],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(active_down.returncode, 75, active_down.stderr)
        self.assertIn("active FDP-2 conversation", active_down.stderr)

        abandoned = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_dialogue_controller.py"),
                "abandon",
                str(self.runs),
                "--feature",
                "fdp2-archive",
                "--idempotency-key",
                "abandon-archive-test",
                "--reason",
                "test teardown",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(abandoned.returncode, 0, abandoned.stderr)
        dialogue_ledger = self.runs / "fleet-fdp2-archive.dialogue.jsonl"
        dialogue_ledger.touch()
        dialogue_ledger.chmod(0o600)

        down = subprocess.run(
            ["bash", str(FLEET_DOWN), "fdp2-archive"],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(down.returncode, 0, down.stderr)
        archive = next((self.runs / "archive").glob("fdp2-archive-*"))
        for relative in (
            "dialogue-control.jsonl",
            "dialogue.jsonl",
            "dialogue/control",
            "verification-receipt.json",
        ):
            self.assertTrue((archive / relative).exists(), relative)
        verified = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_dialogue_controller.py"),
                "verify",
                "--archive",
                str(archive),
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(verified.stdout)["latest_status"], "abandoned")

    def test_teardown_recovers_only_after_confirmed_workspace_absence(self) -> None:
        result = self.run_fleet("recover-absent", "triage")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.runs / "fleet-recover-absent.manifest"
        manifest_text = manifest.read_text()
        workspace = re.search(r"^workspace=(.+)$", manifest_text, re.M).group(1)
        workspace_uuid = re.search(r"^workspace_uuid=(.+)$", manifest_text, re.M).group(
            1
        )
        surface_uuid = re.search(r"^triage\.uuid=(.+)$", manifest_text, re.M).group(1)
        run_id = "recover-absent-run"
        acquired = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_leases.py"),
                "acquire",
                str(self.runs),
                "--run-id",
                run_id,
                "--feature",
                "recover-absent",
                "--instance",
                "triage",
                "--role",
                "triage",
                "--phase",
                "RECON",
                "--resource-class",
                "local_light",
                "--task-sha256",
                "a" * 64,
                "--workspace-uuid",
                workspace_uuid,
                "--surface-uuid",
                surface_uuid,
                "--max-local",
                "3",
                "--role-limit",
                "2",
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
                "python3",
                str(ROOT / "scripts" / "fleet_ledger.py"),
                str(self.runs / "fleet-recover-absent.ledger.jsonl"),
                "--run-id",
                run_id,
                "--feature",
                "recover-absent",
                "--instance",
                "triage",
                "--role",
                "triage",
                "--phase",
                "RECON",
                "--status",
                "running",
                "--task-sha256",
                "a" * 64,
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

    def make_target_repo(self, name: str = "target-repo") -> Path:
        target = self.tmp / name
        target.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=target, check=True)
        subprocess.run(
            ["git", "-C", str(target), "config", "user.email", "t@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "config", "user.name", "t"], check=True
        )
        (target / "file.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(target), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(target), "commit", "-q", "-m", "init"], check=True
        )
        return target

    def boot_completing_mission(
        self, feature: str
    ) -> tuple[Path, str, str, fleet_control_service.ControlLifecycle]:
        target = self.make_target_repo(f"target-{feature}")
        base_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        compiled = workflow_config.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )
        mission_id, _ = fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature=feature,
            objective="exercise isolated reader retirement",
            target_repo=target.resolve(),
            base_sha=base_sha,
            idempotency_key=f"test:reader-retirement:{feature}",
        )
        self.env["FLEET_MISSION_ID"] = mission_id
        compiled_path = self.runs / "missions" / mission_id / "compiled-workflow.json"
        socket_root = Path(tempfile.mkdtemp(prefix="fleet-control-test-", dir="/tmp"))
        self.env["FLEET_CONTROL_SOCKET_DIR"] = str(socket_root)
        with mock.patch.dict(
            os.environ, {"FLEET_CONTROL_SOCKET_DIR": str(socket_root)}
        ):
            lifecycle = fleet_control_service.ControlLifecycle(
                self.runs, mission_id, preset="dan"
            )
            lifecycle.start()
        self.addCleanup(shutil.rmtree, socket_root, True)
        self.addCleanup(
            lambda: lifecycle.stop() if lifecycle.lifecycle_path.exists() else None
        )
        result = self.run_fleet(
            feature,
            "--preset",
            "dan",
            "--target-repo",
            str(target),
            "--expected-base-sha",
            base_sha,
            "--compiled-workflow",
            str(compiled_path),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.runs / f"fleet-{feature}.manifest"
        fleet_mission_state.append_event(
            self.runs,
            mission_id,
            kind="fleet_boot_started",
            actor="CONTROL",
            idempotency_key=f"test:{feature}:boot",
            payload={"feature": feature, "preset": "dan"},
        )
        fleet_mission_state.append_event(
            self.runs,
            mission_id,
            kind="mission_running",
            actor="CONTROL",
            idempotency_key=f"test:{feature}:running",
            payload={"manifest": str(manifest)},
        )
        fleet_mission_state.append_event(
            self.runs,
            mission_id,
            kind="mission_completing",
            actor="CONTROL",
            idempotency_key=f"test:{feature}:completing",
            payload={"lead_artifact_id": "a" * 64},
        )
        return target, base_sha, mission_id, lifecycle

    def test_prepare_archive_then_terminal_down_is_clean_and_preserves_archive(
        self,
    ) -> None:
        feature = "prepared-final-down"
        _, _, mission_id, _ = self.boot_completing_mission(feature)
        manifest = self.runs / f"fleet-{feature}.manifest"

        prepared = subprocess.run(
            ["bash", str(FLEET_DOWN), feature, "--prepare-archive"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertEqual(prepared.stderr, "")
        self.assertTrue(manifest.exists())
        prepared_manifest = manifest.read_bytes()
        self.assertIn(b"workspace.quiesced=1\n", prepared_manifest)
        self.assertIn(b"workspace.handoff_state=quiesced\n", prepared_manifest)

        fleet_mission_state.append_terminal(
            self.runs,
            mission_id,
            status="failed",
            reason="prepared archive regression",
            idempotency_key=f"test:{feature}:terminal",
        )
        finalized = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )

        self.assertEqual(finalized.returncode, 0, finalized.stderr)
        self.assertEqual(finalized.stderr, "")
        self.assertNotIn("Traceback", finalized.stdout)
        self.assertFalse(manifest.exists())
        archives = list((self.runs / "archive").glob(f"{feature}-*/manifest"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), prepared_manifest)

    def test_reader_retirement_recovers_after_stage_and_remove_crashes(self) -> None:
        for checkpoint in (
            "after_reader_stage_chmod",
            "after_reader_stage_rename",
            "after_reader_stage_restore",
            "after_reader_stage",
            "after_reader_retirement_delete_entry",
            "after_reader_remove",
        ):
            with self.subTest(checkpoint=checkpoint):
                feature = f"reader-crash-{checkpoint.rsplit('_', 1)[-1]}"
                target, base_sha, _, _ = self.boot_completing_mission(feature)
                manifest_path = self.runs / f"fleet-{feature}.manifest"
                manifest = manifest_path.read_text(encoding="utf-8")
                reader_paths = [
                    Path(value)
                    for value in re.findall(
                        r"^(?:scout|challenger|verifier)\.workspace=(.+)$",
                        manifest,
                        re.M,
                    )
                ]
                self.assertEqual(len(reader_paths), 3, manifest)
                self.env["FLEET_TEST_PUBLICATION_CRASH_AT"] = checkpoint
                crashed = subprocess.run(
                    ["bash", str(FLEET_DOWN), feature, "--prepare-archive"],
                    cwd=ROOT,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=False,
                )
                self.assertNotEqual(crashed.returncode, 0)
                self.env.pop("FLEET_TEST_PUBLICATION_CRASH_AT")
                self.assertNotEqual(
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(target),
                            "show-ref",
                            "--verify",
                            f"refs/heads/fleet/{feature}/builder",
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    ).returncode,
                    0,
                )

                recovered = subprocess.run(
                    ["bash", str(FLEET_DOWN), feature, "--prepare-archive"],
                    cwd=ROOT,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                for path in reader_paths:
                    self.assertFalse(path.exists() or path.is_symlink(), str(path))
                self.assertFalse(
                    list(
                        (self.tmp / "worktrees" / ".fleet-control-staging").glob(
                            f"{feature}--*--*"
                        )
                    )
                )
                updated = manifest_path.read_text(encoding="utf-8")
                self.assertIn("workspace.quiesced=1\n", updated)
                self.assertIn("builder.publication_state=published\n", updated)
                self.assertEqual(
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(target),
                            "rev-parse",
                            "--verify",
                            f"refs/heads/fleet/{feature}/builder",
                        ],
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout.strip(),
                    base_sha,
                )
                for reader in ("scout", "challenger", "verifier"):
                    self.assertNotEqual(
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                str(target),
                                "show-ref",
                                "--verify",
                                f"refs/heads/fleet/{feature}/{reader}",
                            ],
                            text=True,
                            capture_output=True,
                            check=False,
                        ).returncode,
                        0,
                    )

    def test_reader_late_mutation_blocks_writer_publication(self) -> None:
        feature = "reader-late-mutation"
        target, _, _, _ = self.boot_completing_mission(feature)
        manifest = (self.runs / f"fleet-{feature}.manifest").read_text(encoding="utf-8")
        reader = Path(re.search(r"^scout\.workspace=(.+)$", manifest, re.M).group(1))
        self.env["CMUX_DIRTY_READER_ON_CLOSE"] = str(reader)
        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), feature, "--prepare-archive"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.env.pop("CMUX_DIRTY_READER_ON_CLOSE")
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("staged snapshot changed", refused.stderr)
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/builder",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

    def test_reader_hardlink_is_rejected_before_cmux_close(self) -> None:
        feature = "reader-hardlink"
        target, _, _, _ = self.boot_completing_mission(feature)
        manifest = (self.runs / f"fleet-{feature}.manifest").read_text(encoding="utf-8")
        reader = Path(re.search(r"^scout\.workspace=(.+)$", manifest, re.M).group(1))
        os.link(reader / "file.txt", self.tmp / "reader-hardlink-outside.txt")
        close_calls = sum(call[:1] == ["close-workspace"] for call in self.calls())
        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), feature, "--prepare-archive"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("unsafe Git metadata", refused.stderr)
        self.assertEqual(
            sum(call[:1] == ["close-workspace"] for call in self.calls()),
            close_calls,
        )
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/builder",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

    def test_writer_working_tree_hardlink_is_rejected_before_cmux_close(self) -> None:
        feature = "writer-hardlink"
        target = self.make_target_repo("target-writer-hardlink")
        booted = self.run_fleet(
            feature,
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )
        self.assertEqual(booted.returncode, 0, booted.stderr)
        writer = self.writer_worktree(feature)
        os.link(writer / "file.txt", self.tmp / "writer-hardlink-outside.txt")
        close_calls = sum(call[:1] == ["close-workspace"] for call in self.calls())

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("hardlinks", refused.stderr)
        self.assertEqual(
            sum(call[:1] == ["close-workspace"] for call in self.calls()),
            close_calls,
        )
        self.assertTrue(writer.exists())
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

    def writer_worktree(self, feature: str) -> Path:
        manifest = (self.runs / f"fleet-{feature}.manifest").read_text()
        match = re.search(r"^build\.worktree=(.+)$", manifest, re.M)
        self.assertIsNotNone(match, f"no build.worktree entry in manifest:\n{manifest}")
        return Path(match.group(1))

    def test_writer_gets_named_branch_from_target_head(self) -> None:
        target = self.make_target_repo()
        base_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        result = self.run_fleet(
            "wt", "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wt")
        self.assertTrue(worktree.is_dir())
        head = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(head.stdout.strip(), "fleet/wt/build")
        manifest = (self.runs / "fleet-wt.manifest").read_text()
        self.assertIn(f"target_repo={target.resolve()}", manifest)
        self.assertIn("build.branch=fleet/wt/build", manifest)
        self.assertIn(f"build.base_sha={base_sha}", manifest)
        self.assertIn(f"build.final_sha={base_sha}", manifest)
        self.assertIn("build.git_isolation=isolated-clone", manifest)
        self.assertIn("build.publication_state=private", manifest)
        self.assertNotIn("verify.worktree=", manifest)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(worktree), "remote"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip(),
            "",
        )
        writer_git_dir = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        target_git_dir = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(writer_git_dir, str(worktree / ".git"))
        self.assertNotEqual(writer_git_dir, target_git_dir)
        target_object = Path(target_git_dir) / "objects" / base_sha[:2] / base_sha[2:]
        writer_object = Path(writer_git_dir) / "objects" / base_sha[:2] / base_sha[2:]
        self.assertTrue(target_object.is_file())
        self.assertTrue(writer_object.is_file())
        self.assertNotEqual(target_object.stat().st_ino, writer_object.stat().st_ino)
        target_branch = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "show-ref",
                "--verify",
                "refs/heads/fleet/wt/build",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(target_branch.returncode, 0)
        sends = self.launch_commands()
        writer_launch = next(
            payload
            for payload in sends
            if "cd " in payload and str(worktree) in payload
        )
        self.assertNotIn("sandbox_workspace_write.writable_roots", writer_launch)
        self.assertNotIn(target_git_dir, writer_launch)

    def test_teardown_rejects_symlinked_active_manifest_before_cmux_or_git(
        self,
    ) -> None:
        target = self.make_target_repo("target-manifest-symlink")
        feature = "manifest-symlink"
        result = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree(feature)
        manifest = self.runs / f"fleet-{feature}.manifest"
        backing = self.runs / f".{manifest.name}.backing"
        manifest.rename(backing)
        manifest.symlink_to(backing.name)
        close_calls = sum(call[:1] == ["close-workspace"] for call in self.calls())

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn(
            "phase state is missing, corrupt, or path-unsafe", refused.stderr
        )
        self.assertEqual(
            sum(call[:1] == ["close-workspace"] for call in self.calls()),
            close_calls,
        )
        self.assertTrue(worktree.exists())
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

    def test_teardown_rejects_duplicate_manifest_key_before_cmux_or_git(self) -> None:
        target = self.make_target_repo("target-manifest-duplicate")
        feature = "manifest-duplicate"
        result = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree(feature)
        manifest = self.runs / f"fleet-{feature}.manifest"
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(f"target_repo={target.resolve()}\n")
        close_calls = sum(call[:1] == ["close-workspace"] for call in self.calls())

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("duplicate manifest key", refused.stderr)
        self.assertEqual(
            sum(call[:1] == ["close-workspace"] for call in self.calls()),
            close_calls,
        )
        self.assertTrue(worktree.exists())
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

    def test_teardown_rejects_missing_corrupt_symlinked_or_hardlinked_phase_state(
        self,
    ) -> None:
        for mutation in ("missing", "corrupt", "symlink", "hardlink"):
            with self.subTest(mutation=mutation):
                feature = f"phase-state-{mutation}"
                result = self.run_fleet(feature, "--preset", "small")
                self.assertEqual(result.returncode, 0, result.stderr)
                state_path = self.runs / f"fleet-{feature}.state.json"
                original = state_path.read_bytes()
                outside = self.runs / f".{feature}.outside-state.json"
                if mutation in {"symlink", "hardlink"}:
                    outside.write_bytes(original)
                    outside.chmod(0o600)
                if mutation == "missing":
                    state_path.unlink()
                elif mutation == "corrupt":
                    state_path.write_bytes(b'{"schema_version":2,"active_phase":')
                elif mutation == "symlink":
                    state_path.unlink()
                    state_path.symlink_to(outside.name)
                else:
                    state_path.unlink()
                    os.link(outside, state_path)
                manifest_path = self.runs / f"fleet-{feature}.manifest"
                manifest_before = manifest_path.read_bytes()
                for idle_lock in (
                    self.runs / f".fleet-{feature}.manifest.lock",
                    self.runs / ".fleet-boot-locks" / f"{feature}.lock",
                ):
                    if idle_lock.exists():
                        idle_lock.unlink()

                def lock_snapshot() -> dict[str, tuple[int, int, bytes]]:
                    snapshot: dict[str, tuple[int, int, bytes]] = {}
                    for candidate in self.runs.rglob("*"):
                        relative = str(candidate.relative_to(self.runs))
                        if feature not in relative or not (
                            "lock" in relative or "close" in relative
                        ):
                            continue
                        info = candidate.lstat()
                        if stat.S_ISREG(info.st_mode):
                            snapshot[relative] = (
                                stat.S_IMODE(info.st_mode),
                                info.st_nlink,
                                candidate.read_bytes(),
                            )
                    return snapshot

                locks_before = lock_snapshot()
                close_calls = sum(
                    call[:1] == ["close-workspace"] for call in self.calls()
                )

                refused = subprocess.run(
                    ["bash", str(FLEET_DOWN), feature],
                    cwd=ROOT,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=20,
                    check=False,
                )

                self.assertEqual(refused.returncode, 2, refused.stderr)
                self.assertIn(
                    "phase state is missing, corrupt, or path-unsafe", refused.stderr
                )
                self.assertEqual(
                    sum(call[:1] == ["close-workspace"] for call in self.calls()),
                    close_calls,
                )
                self.assertEqual(manifest_path.read_bytes(), manifest_before)
                self.assertEqual(lock_snapshot(), locks_before)
                self.assertFalse(
                    list(self.runs.glob(f".fleet-{feature}.teardown.*.snapshot"))
                )
                if outside.exists():
                    self.assertEqual(outside.read_bytes(), original)
                if state_path.exists() or state_path.is_symlink():
                    state_path.unlink()
                if outside.exists():
                    outside.unlink()
                state_path.write_bytes(original)
                state_path.chmod(0o600)
                closed = subprocess.run(
                    ["bash", str(FLEET_DOWN), feature],
                    cwd=ROOT,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(closed.returncode, 0, closed.stderr)

    def test_symlink_cannot_downgrade_challenge_state_to_skip_assurance(self) -> None:
        feature = "phase-assurance-bypass"
        target = self.make_target_repo("target-phase-assurance-bypass")
        result = self.run_fleet(
            feature,
            "--preset",
            "fleet_dialogue",
            "--target-repo",
            str(target),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.runs / f"fleet-{feature}.manifest"
        state_path = self.runs / f"fleet-{feature}.state.json"
        control_state = state_path.read_bytes()
        advanced = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_state.py"),
                "advance",
                str(manifest),
                "BUILD",
                "--evidence",
                "scope-approved",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        task_spec = self.tmp / "phase-assurance-task.json"
        task_spec.write_text(
            json.dumps(
                {
                    "objective": "prove phase state cannot suppress assurance",
                    "negative_scope": ["no target changes"],
                    "acceptance_criteria": ["teardown fails before evidence mutation"],
                }
            ),
            encoding="utf-8",
        )
        started = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_dialogue_controller.py"),
                "start",
                str(self.runs),
                "--feature",
                feature,
                "--idempotency-key",
                "phase-assurance-start",
                "--spec-file",
                str(task_spec),
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        abandoned = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "fleet_dialogue_controller.py"),
                "abandon",
                str(self.runs),
                "--feature",
                feature,
                "--idempotency-key",
                "phase-assurance-abandon",
                "--reason",
                "phase state bypass fixture",
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(abandoned.returncode, 0, abandoned.stderr)
        dialogue_ledger = self.runs / f"fleet-{feature}.dialogue.jsonl"
        dialogue_ledger.touch()
        dialogue_ledger.chmod(0o600)

        authoritative = json.loads(state_path.read_text(encoding="utf-8"))
        authoritative["active_phase"] = "CHALLENGE"
        build_timestamp = datetime.fromisoformat(
            authoritative["history"][-1]["timestamp"]
        )
        authoritative["history"].append(
            {
                "phase": "CHALLENGE",
                "timestamp": (build_timestamp + timedelta(microseconds=1)).isoformat(),
                "evidence": "accepted-fdp2-head",
                "approved_by": "operator",
            }
        )
        authoritative_path = self.runs / f".{feature}.authoritative-state.json"
        authoritative_path.write_text(
            json.dumps(authoritative) + "\n", encoding="utf-8"
        )
        authoritative_path.chmod(0o600)
        fake_control = self.runs / f".{feature}.fake-control-state.json"
        fake_control.write_bytes(control_state)
        fake_control.chmod(0o600)
        state_path.unlink()
        state_path.symlink_to(fake_control.name)
        close_calls = sum(call[:1] == ["close-workspace"] for call in self.calls())

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("phase state is missing, corrupt, or path-unsafe", refused.stderr)
        self.assertEqual(
            sum(call[:1] == ["close-workspace"] for call in self.calls()), close_calls
        )
        self.assertFalse(
            (self.runs / f"fleet-{feature}.verification-receipt.json").exists()
        )
        self.assertFalse(
            (self.runs / f"fleet-{feature}.assurance-receipt.json").exists()
        )
        self.assertEqual(
            json.loads(authoritative_path.read_text(encoding="utf-8"))["active_phase"],
            "CHALLENGE",
        )

    def test_teardown_rejects_writer_base_drift_before_cmux_or_git(self) -> None:
        target = self.make_target_repo("target-writer-base-drift")
        (target / "second.txt").write_text("second\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(target), "add", "second.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(target), "commit", "-q", "-m", "second"], check=True
        )
        ancestor = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD^"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        feature = "writer-base-drift"
        result = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.runs / f"fleet-{feature}.manifest"
        manifest.write_text(
            re.sub(
                r"^build\.base_sha=.*$",
                f"build.base_sha={ancestor}",
                manifest.read_text(encoding="utf-8"),
                flags=re.M,
            ),
            encoding="utf-8",
        )
        close_calls = sum(call[:1] == ["close-workspace"] for call in self.calls())

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("immutable manifest binding changed", refused.stderr)
        self.assertEqual(
            sum(call[:1] == ["close-workspace"] for call in self.calls()),
            close_calls,
        )
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

    def test_teardown_rejects_missing_or_extra_writer_tuple_before_effects(
        self,
    ) -> None:
        for index, mutation in enumerate(("missing", "extra")):
            with self.subTest(mutation=mutation):
                feature = f"writer-tuple-{index}"
                target = self.make_target_repo(f"target-writer-tuple-{index}")
                result = self.run_fleet(
                    feature,
                    "--preset",
                    "implementation_review",
                    "--target-repo",
                    str(target),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = self.runs / f"fleet-{feature}.manifest"
                content = manifest.read_text(encoding="utf-8")
                if mutation == "missing":
                    content = re.sub(r"^build\.worktree=.*\n", "", content, flags=re.M)
                else:
                    worktree = self.writer_worktree(feature)
                    content += (
                        f"verify.worktree={worktree.parent / (feature + '--verify--00000000-0000-0000-0000-000000000001')}\n"
                        f"verify.branch=fleet/{feature}/verify\n"
                        + re.search(r"^base_sha=(.+)$", content, re.M).expand(
                            "verify.base_sha=\\1\nverify.final_sha=\\1\n"
                        )
                        + "verify.git_isolation=isolated-clone\n"
                        + "verify.publication_state=private\n"
                    )
                manifest.write_text(content, encoding="utf-8")
                close_calls = sum(
                    call[:1] == ["close-workspace"] for call in self.calls()
                )

                refused = subprocess.run(
                    ["bash", str(FLEET_DOWN), feature],
                    cwd=ROOT,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=20,
                    check=False,
                )

                self.assertEqual(refused.returncode, 2, refused.stderr)
                self.assertIn("immutable manifest binding changed", refused.stderr)
                self.assertEqual(
                    sum(call[:1] == ["close-workspace"] for call in self.calls()),
                    close_calls,
                )
                self.assertNotEqual(
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(target),
                            "show-ref",
                            "--verify",
                            f"refs/heads/fleet/{feature}/build",
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    ).returncode,
                    0,
                )

    def test_teardown_rejects_compiled_binding_drift_before_cmux_or_git(self) -> None:
        target = self.make_target_repo("target-compiled-drift")
        base_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        compiled = workflow_config.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )
        feature = "compiled-drift"
        mission_id, _ = fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature=feature,
            objective="reject drift before publication",
            target_repo=target.resolve(),
            base_sha=base_sha,
            idempotency_key="test:compiled-drift",
        )
        self.env["FLEET_MISSION_ID"] = mission_id
        compiled_path = self.runs / "missions" / mission_id / "compiled-workflow.json"
        socket_root = Path(tempfile.mkdtemp(prefix="fleet-control-test-", dir="/tmp"))
        self.env["FLEET_CONTROL_SOCKET_DIR"] = str(socket_root)
        with mock.patch.dict(
            os.environ, {"FLEET_CONTROL_SOCKET_DIR": str(socket_root)}
        ):
            lifecycle = fleet_control_service.ControlLifecycle(
                self.runs, mission_id, preset="dan"
            )
            lifecycle.start()
        self.addCleanup(shutil.rmtree, socket_root, True)
        self.addCleanup(
            lambda: lifecycle.stop() if lifecycle.lifecycle_path.exists() else None
        )
        result = self.run_fleet(
            feature,
            "--preset",
            "dan",
            "--target-repo",
            str(target),
            "--expected-base-sha",
            base_sha,
            "--compiled-workflow",
            str(compiled_path),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.runs / f"fleet-{feature}.manifest"
        manifest_text = manifest.read_text(encoding="utf-8")
        manifest.write_text(
            re.sub(
                r"^compiled_digest=.*$",
                f"compiled_digest={'0' * 64}",
                manifest_text,
                flags=re.M,
            ),
            encoding="utf-8",
        )
        writer_match = re.search(r"^builder\.worktree=(.+)$", manifest_text, re.M)
        self.assertIsNotNone(writer_match)
        writer = Path(writer_match.group(1))
        close_calls = sum(call[:1] == ["close-workspace"] for call in self.calls())

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("immutable manifest binding changed", refused.stderr)
        self.assertEqual(
            sum(call[:1] == ["close-workspace"] for call in self.calls()),
            close_calls,
        )
        self.assertTrue(writer.exists())
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/builder",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

    def test_teardown_rejects_creation_binding_and_duplicate_json_before_effects(
        self,
    ) -> None:
        target = self.make_target_repo("target-creation-drift")
        base_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        compiled = workflow_config.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )
        feature = "creation-drift"
        mission_id, _ = fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature=feature,
            objective="bind every immutable creation input",
            target_repo=target.resolve(),
            base_sha=base_sha,
            idempotency_key="test:creation-drift",
        )
        self.env["FLEET_MISSION_ID"] = mission_id
        mission_root = self.runs / "missions" / mission_id
        compiled_path = mission_root / "compiled-workflow.json"
        socket_root = Path(tempfile.mkdtemp(prefix="fleet-control-test-", dir="/tmp"))
        self.env["FLEET_CONTROL_SOCKET_DIR"] = str(socket_root)
        with mock.patch.dict(
            os.environ, {"FLEET_CONTROL_SOCKET_DIR": str(socket_root)}
        ):
            lifecycle = fleet_control_service.ControlLifecycle(
                self.runs, mission_id, preset="dan"
            )
            lifecycle.start()
        self.addCleanup(shutil.rmtree, socket_root, True)
        self.addCleanup(
            lambda: lifecycle.stop() if lifecycle.lifecycle_path.exists() else None
        )
        result = self.run_fleet(
            feature,
            "--preset",
            "dan",
            "--target-repo",
            str(target),
            "--expected-base-sha",
            base_sha,
            "--compiled-workflow",
            str(compiled_path),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        creation_path = mission_root / "creation-request.json"
        original = creation_path.read_bytes()
        original_value = json.loads(original)
        mutations: list[tuple[str, bytes, str]] = []
        workflow_drift = json.loads(original)
        workflow_drift["request"]["workflow_digest"] = "0" * 64
        mutations.append(
            (
                "workflow",
                json.dumps(
                    workflow_drift,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
                + b"\n",
                "workflow digest drift",
            )
        )
        risk_drift = json.loads(original)
        current_risk = risk_drift["request"]["initial_risk"]
        risk_drift["request"]["initial_risk"] = (
            "low" if current_risk != "low" else "unknown"
        )
        mutations.append(
            (
                "risk",
                json.dumps(
                    risk_drift,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
                + b"\n",
                "risk binding drift",
            )
        )
        duplicate = original.rstrip()
        self.assertTrue(duplicate.endswith(b"}"))
        mutations.append(
            (
                "duplicate-json",
                duplicate[:-1] + b',"schema_version":1}\n',
                "duplicate key",
            )
        )
        mutations.extend(
            (
                (
                    "overflow-json",
                    duplicate[:-1] + b',"ambiguous":1e999}\n',
                    "non-finite JSON number",
                ),
                (
                    "surrogate-json",
                    duplicate[:-1] + b',"ambiguous":"\\ud800"}\n',
                    "not valid Unicode",
                ),
            )
        )
        close_calls = sum(call[:1] == ["close-workspace"] for call in self.calls())
        for label, content, expected in mutations:
            with self.subTest(mutation=label):
                creation_path.write_bytes(content)
                refused = subprocess.run(
                    ["bash", str(FLEET_DOWN), feature],
                    cwd=ROOT,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(refused.returncode, 75, refused.stderr)
                self.assertIn(expected, refused.stderr)
                self.assertEqual(
                    sum(call[:1] == ["close-workspace"] for call in self.calls()),
                    close_calls,
                )
                self.assertNotEqual(
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(target),
                            "show-ref",
                            "--verify",
                            f"refs/heads/fleet/{feature}/builder",
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    ).returncode,
                    0,
                )
                creation_path.write_bytes(original)
        self.assertEqual(json.loads(original), original_value)

    def test_teardown_cas_rejects_manifest_drift_after_cmux_close(self) -> None:
        target = self.make_target_repo("target-manifest-cas")
        feature = "manifest-cas"
        result = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.runs / f"fleet-{feature}.manifest"
        worktree = self.writer_worktree(feature)
        self.env["CMUX_TAMPER_MANIFEST_ON_CLOSE"] = str(manifest)

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("active manifest changed outside teardown CAS", refused.stderr)
        self.assertTrue(manifest.exists())
        self.assertTrue(worktree.exists())
        self.assertFalse(
            list(
                (self.tmp / "worktrees" / ".fleet-control-staging").glob(
                    f"{feature}--build--*"
                )
            )
        )
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

    def test_manifest_cas_allows_exactly_one_concurrent_writer(self) -> None:
        feature = "manifest-cas-race"
        result = self.run_fleet(feature, "--preset", "implementation_review")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.runs / f"fleet-{feature}.manifest"
        expected = hashlib.sha256(manifest.read_bytes()).hexdigest()
        base_command = [
            sys.executable,
            str(MANIFEST_GUARD),
            "set-active",
            "--runs-dir",
            str(self.runs),
            "--feature",
            feature,
            "--digest",
            expected,
        ]
        processes = [
            subprocess.Popen(
                base_command + ["--key", key, "--value", value],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for key, value in (("cas.writer_a", "a"), ("cas.writer_b", "b"))
        ]
        results = [process.communicate(timeout=20) for process in processes]
        returncodes = sorted(process.returncode for process in processes)

        self.assertEqual(returncodes, [0, 75], results)
        content = manifest.read_text(encoding="utf-8")
        winners = sum(
            row in content for row in ("cas.writer_a=a\n", "cas.writer_b=b\n")
        )
        self.assertEqual(winners, 1, content)

    def test_teardown_refuses_dirty_worktree_then_removes_clean_one(self) -> None:
        target = self.make_target_repo()
        result = self.run_fleet(
            "wtdown", "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wtdown")

        (worktree / "scratch.txt").write_text("dirty\n", encoding="utf-8")
        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), "wtdown"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("uncommitted", refused.stderr)
        self.assertTrue((self.runs / "fleet-wtdown.manifest").exists())

        (worktree / "scratch.txt").unlink()
        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), "wtdown"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertFalse(worktree.exists())
        self.assertFalse((self.runs / "fleet-wtdown.manifest").exists())
        branch = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "show-ref",
                "--verify",
                "refs/heads/fleet/wtdown/build",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(
            branch.returncode, 0, "unchanged writer branch should be removed"
        )

    def test_teardown_preserves_advanced_writer_branch_and_archives_final_sha(
        self,
    ) -> None:
        target = self.make_target_repo()
        result = self.run_fleet(
            "wtcommit",
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wtcommit")
        (worktree / "result.txt").write_text("durable\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "result.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(worktree), "commit", "-q", "-m", "agent result"],
            check=True,
        )
        final_sha = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), "wtcommit"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertFalse(worktree.exists())
        branch_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "refs/heads/fleet/wtcommit/build"],
            text=True,
            capture_output=True,
            check=True,
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
            "wtdetached",
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wtdetached")
        subprocess.run(
            ["git", "-C", str(worktree), "switch", "--detach", "-q"], check=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "detached",
            ],
            check=True,
        )

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), "wtdetached"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("not attached", refused.stderr)
        self.assertTrue(worktree.exists())
        self.assertTrue((self.runs / "fleet-wtdetached.manifest").exists())
        branch = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "show-ref",
                "--verify",
                "refs/heads/fleet/wtdetached/build",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(branch.returncode, 0)

    def test_teardown_rejects_executable_writer_git_metadata_without_running_it(
        self,
    ) -> None:
        target = self.make_target_repo("target-hostile-git")
        result = self.run_fleet(
            "wthostile",
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wthostile")
        marker = self.tmp / "hostile-git-ran"
        canary = self.tmp / "hostile-fsmonitor"
        canary.write_text(
            f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 0\n",
            encoding="utf-8",
        )
        canary.chmod(0o700)
        subprocess.run(
            ["git", "-C", str(worktree), "config", "core.fsmonitor", str(canary)],
            check=True,
        )
        hook = worktree / ".git" / "hooks" / "post-checkout"
        hook.write_text(
            f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n",
            encoding="utf-8",
        )
        hook.chmod(0o700)

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), "wthostile"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("Git metadata", refused.stderr)
        self.assertFalse(marker.exists(), "hostile fsmonitor/hook canary executed")
        self.assertTrue(worktree.exists())

    def test_teardown_rechecks_writer_after_workspace_shutdown(self) -> None:
        target = self.make_target_repo()
        result = self.run_fleet(
            "wtlate", "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wtlate")
        self.env["CMUX_DETACH_WORKTREE_ON_CLOSE"] = str(worktree)

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), "wtlate"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("after workspace shutdown", refused.stderr)
        self.assertFalse(worktree.exists())
        staged = list(
            (self.tmp / "worktrees" / ".fleet-control-staging").glob("wtlate--build--*")
        )
        self.assertEqual(len(staged), 1)
        self.assertTrue((self.runs / "fleet-wtlate.manifest").exists())

    def test_teardown_publishes_late_commit_only_after_workspace_quiescence(
        self,
    ) -> None:
        target = self.make_target_repo()
        base_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        result = self.run_fleet(
            "wtlatecommit",
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wtlatecommit")
        target_ref = "refs/heads/fleet/wtlatecommit/build"
        self.assertNotEqual(
            subprocess.run(
                ["git", "-C", str(target), "show-ref", "--verify", target_ref],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )
        self.env["CMUX_COMMIT_WORKTREE_ON_CLOSE"] = str(worktree)

        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), "wtlatecommit"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)
        final_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", target_ref],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertNotEqual(final_sha, base_sha)
        archived_manifest = next(
            (self.runs / "archive").glob("wtlatecommit-*/manifest")
        ).read_text(encoding="utf-8")
        self.assertIn(f"build.final_sha={final_sha}\n", archived_manifest)
        self.assertIn(f"build.published_sha={final_sha}\n", archived_manifest)
        self.assertIn("workspace.quiesced=1\n", archived_manifest)

    def test_sha256_repository_writer_publication_uses_native_oid_width(self) -> None:
        target = self.tmp / "target-sha256"
        target.mkdir()
        init = subprocess.run(
            ["git", "init", "-q", "--object-format=sha256", "-b", "main"],
            cwd=target,
            text=True,
            capture_output=True,
            check=False,
        )
        if init.returncode != 0:
            self.skipTest("installed Git does not support SHA-256 repositories")
        subprocess.run(
            ["git", "-C", str(target), "config", "user.email", "t@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "config", "user.name", "t"], check=True
        )
        (target / "file.txt").write_text("sha256\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(target), "add", "file.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(target), "commit", "-q", "-m", "init"], check=True
        )
        feature = "sha256-publish"
        result = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree(feature)
        (worktree / "result.txt").write_text("native width\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "result.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(worktree), "commit", "-q", "-m", "result"],
            check=True,
        )

        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(closed.returncode, 0, closed.stderr)
        branch_sha = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "rev-parse",
                f"refs/heads/fleet/{feature}/build",
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(len(branch_sha), 64)

    def test_target_git_ignores_poisoned_environment_and_reference_hook(self) -> None:
        target = self.make_target_repo("target-git-poison")
        hook_dir = self.tmp / "target-hooks"
        hook_dir.mkdir()
        marker = self.tmp / "target-reference-hook-ran"
        hook = hook_dir / "reference-transaction"
        hook.write_text(
            f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 99\n",
            encoding="utf-8",
        )
        hook.chmod(0o700)
        subprocess.run(
            ["git", "-C", str(target), "config", "core.hooksPath", str(hook_dir)],
            check=True,
        )
        self.env.update(
            {
                "GIT_DIR": str(self.tmp / "poison-git-dir"),
                "GIT_WORK_TREE": str(self.tmp / "poison-work-tree"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": str(hook_dir),
                "GIT_CONFIG_PARAMETERS": "'core.hooksPath'='" + str(hook_dir) + "'",
                "GIT_CONFIG_SYSTEM": str(self.tmp / "poison-system-config"),
            }
        )
        feature = "git-poison"
        result = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree(feature)
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "result",
            ],
            check=True,
        )

        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertFalse(marker.exists(), "target reference-transaction hook executed")
        self.assertEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "rev-parse",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=True,
            ).returncode,
            0,
        )

    def test_teardown_recovers_sigkill_after_manifest_archive_commit(self) -> None:
        target = self.make_target_repo("target-archive-crash")
        feature = "archive-crash"
        result = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.env["FLEET_TEST_PUBLICATION_CRASH_AT"] = "after_manifest_archive"

        crashed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.env.pop("FLEET_TEST_PUBLICATION_CRASH_AT")

        self.assertNotEqual(crashed.returncode, 0)
        self.assertFalse((self.runs / f"fleet-{feature}.manifest").exists())
        self.assertTrue(list(self.runs.glob(f".fleet-{feature}.*.archive-intent.json")))
        self.assertTrue((self.runs / "locks" / f"{feature}.closing").exists())
        self.assertEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "rev-parse",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=True,
            ).returncode,
            0,
        )

        recovered = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn("recovered archived teardown", recovered.stdout)
        self.assertFalse(
            list(self.runs.glob(f".fleet-{feature}.*.archive-intent.json"))
        )
        self.assertFalse(list(self.runs.glob(f".fleet-{feature}.teardown.*.snapshot")))
        self.assertFalse((self.runs / "locks" / f"{feature}.closing").exists())
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

    def test_publication_recovers_crashes_after_ref_and_between_manifest_fields(
        self,
    ) -> None:
        checkpoints = (
            "after_update_ref",
            "after_final_sha",
            "after_published_sha",
            "after_publication_state",
        )
        for index, checkpoint in enumerate(checkpoints):
            with self.subTest(checkpoint=checkpoint):
                feature = f"wtcrash{index}"
                target = self.make_target_repo(f"target-{checkpoint}")
                result = self.run_fleet(
                    feature,
                    "--preset",
                    "implementation_review",
                    "--target-repo",
                    str(target),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                worktree = self.writer_worktree(feature)
                (worktree / "result.txt").write_text(
                    f"{checkpoint}\n", encoding="utf-8"
                )
                subprocess.run(
                    ["git", "-C", str(worktree), "add", "result.txt"], check=True
                )
                subprocess.run(
                    ["git", "-C", str(worktree), "commit", "-q", "-m", checkpoint],
                    check=True,
                )
                final_sha = subprocess.run(
                    ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                self.env["FLEET_TEST_PUBLICATION_CRASH_AT"] = checkpoint
                crashed = subprocess.run(
                    ["bash", str(FLEET_DOWN), feature],
                    cwd=ROOT,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=20,
                    check=False,
                )
                self.env.pop("FLEET_TEST_PUBLICATION_CRASH_AT")
                self.assertNotEqual(crashed.returncode, 0)
                self.assertTrue((self.runs / f"fleet-{feature}.manifest").exists())
                self.assertFalse(worktree.exists())
                self.assertEqual(
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(target),
                            "rev-parse",
                            f"refs/heads/fleet/{feature}/build",
                        ],
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout.strip(),
                    final_sha,
                )

                recovered = subprocess.run(
                    ["bash", str(FLEET_DOWN), feature],
                    cwd=ROOT,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                self.assertFalse((self.runs / f"fleet-{feature}.manifest").exists())
                self.assertFalse(
                    list(
                        (self.tmp / "worktrees" / ".fleet-control-staging").glob(
                            f"{feature}--build--*"
                        )
                    )
                )
                self.assertFalse(
                    list(
                        self.runs.glob(
                            f".fleet-{feature}.build.*.publication-intent.json"
                        )
                    )
                )
                archived_manifest = next(
                    (self.runs / "archive").glob(f"{feature}-*/manifest")
                ).read_text(encoding="utf-8")
                self.assertIn(f"build.final_sha={final_sha}\n", archived_manifest)
                self.assertIn(f"build.published_sha={final_sha}\n", archived_manifest)
                self.assertIn("build.publication_state=published\n", archived_manifest)
                self.assertIn("workspace.quiesced=1\n", archived_manifest)

    def test_writer_retirement_recovers_partial_descriptor_safe_deletion(self) -> None:
        feature = "writer-partial-remove"
        target = self.make_target_repo("target-writer-partial-remove")
        booted = self.run_fleet(
            feature,
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )
        self.assertEqual(booted.returncode, 0, booted.stderr)
        writer = self.writer_worktree(feature)
        self.env["FLEET_TEST_PUBLICATION_CRASH_AT"] = (
            "after_writer_retirement_delete_entry"
        )

        crashed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.env.pop("FLEET_TEST_PUBLICATION_CRASH_AT")

        self.assertEqual(crashed.returncode, 75, crashed.stderr)
        manifest = self.runs / f"fleet-{feature}.manifest"
        self.assertTrue(manifest.exists())
        self.assertIn("build.publication_state=published\n", manifest.read_text())
        self.assertFalse(writer.exists())
        self.assertTrue(
            list(
                self.runs.glob(
                    f".fleet-{feature}.build.*.clone-retirement-tombstone.json"
                )
            )
        )
        staged = list(
            (self.tmp / "worktrees" / ".fleet-control-staging").glob(
                f"{feature}--build--*"
            )
        )
        self.assertEqual(len(staged), 1)

        recovered = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse(manifest.exists())
        self.assertFalse(staged[0].exists())
        self.assertFalse(list(self.runs.glob(f".fleet-{feature}.build.*.*intent.json")))
        self.assertFalse(
            list(
                self.runs.glob(
                    f".fleet-{feature}.build.*.clone-retirement-tombstone.json"
                )
            )
        )

    def test_publication_intent_hardlink_is_rejected_descriptor_safely(self) -> None:
        target = self.make_target_repo("target-intent-hardlink")
        feature = "intent-hardlink"
        booted = self.run_fleet(
            feature,
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )
        self.assertEqual(booted.returncode, 0, booted.stderr)
        writer = self.writer_worktree(feature)
        subprocess.run(
            [
                "git",
                "-C",
                str(writer),
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "advanced",
            ],
            check=True,
        )
        self.env["FLEET_TEST_PUBLICATION_CRASH_AT"] = "after_update_ref"
        crashed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.env.pop("FLEET_TEST_PUBLICATION_CRASH_AT")
        self.assertNotEqual(crashed.returncode, 0)
        intent = next(
            self.runs.glob(f".fleet-{feature}.build.*.publication-intent.json")
        )
        os.link(intent, self.tmp / "publication-intent-hardlink.json")

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("intent drifted", refused.stderr)
        self.assertTrue(intent.exists())
        self.assertEqual(intent.stat().st_nlink, 2)

    def test_publication_rejects_exact_target_ref_without_control_intent(self) -> None:
        target = self.make_target_repo("target-no-intent")
        feature = "wtnointent"
        result = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree(feature)
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "advanced",
            ],
            check=True,
        )
        self.env["FLEET_TEST_PUBLICATION_CRASH_AT"] = "after_update_ref"
        crashed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.env.pop("FLEET_TEST_PUBLICATION_CRASH_AT")
        self.assertNotEqual(crashed.returncode, 0)
        intent = next(
            self.runs.glob(f".fleet-{feature}.build.*.publication-intent.json")
        )
        intent.unlink()

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("cannot be reconciled", refused.stderr)
        self.assertTrue((self.runs / f"fleet-{feature}.manifest").exists())
        self.assertTrue(
            list(
                (self.tmp / "worktrees" / ".fleet-control-staging").glob(
                    f"{feature}--build--*"
                )
            )
        )

    def test_teardown_preserves_state_when_shutdown_probe_fails(self) -> None:
        target = self.make_target_repo()
        result = self.run_fleet(
            "wtprobe", "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        worktree = self.writer_worktree("wtprobe")
        self.env["CMUX_TREE_PROBE_ERROR"] = "1"

        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), "wtprobe"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
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
            "wtcollision",
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Writer branch already exists", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.runs / "fleet-wtcollision.manifest").exists())

    def test_failed_boot_removes_unchanged_writer_branch(self) -> None:
        target = self.make_target_repo()
        self.env["CMUX_READY"] = "0"
        result = self.run_fleet(
            "wtfailed",
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )
        self.assertEqual(result.returncode, 1)
        branch = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "show-ref",
                "--verify",
                "refs/heads/fleet/wtfailed/build",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(branch.returncode, 0)
        self.assertFalse((self.tmp / "worktrees" / "wtfailed-build").exists())

    def test_failed_boot_journals_advanced_writer_without_publishing_ref(self) -> None:
        target = self.make_target_repo()
        worktree = self.tmp / "worktrees" / "wtfailedcommit-build"
        self.env["CMUX_READY"] = "0"
        self.env["CMUX_COMMIT_WORKTREE_ON_CLOSE"] = str(worktree)
        result = self.run_fleet(
            "wtfailedcommit",
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )
        self.assertEqual(result.returncode, 1)
        self.assertFalse(worktree.exists())
        self.assertIn("no public ref created", result.stderr)
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    "refs/heads/fleet/wtfailedcommit/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )
        base_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "main"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        staged = list(
            (self.tmp / "worktrees" / ".fleet-control-staging").glob(
                "wtfailedcommit--build--*"
            )
        )
        self.assertEqual(len(staged), 1)
        staged_sha = subprocess.run(
            ["git", "-C", str(staged[0]), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertNotEqual(staged_sha, base_sha)
        self.assertFalse((self.runs / "fleet-wtfailedcommit.manifest").exists())
        self.assertTrue(
            list(
                self.runs.glob(".fleet-wtfailedcommit.build.*.clone-stage-intent.json")
            )
        )
        self.env.pop("CMUX_COMMIT_WORKTREE_ON_CLOSE")
        retry = self.run_fleet(
            "wtfailedcommit",
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )
        self.assertEqual(retry.returncode, 2, retry.stderr)
        self.assertIn("advanced writer is preserved", retry.stderr)
        self.assertTrue(staged[0].exists())

    def test_failed_boot_partial_retirement_journal_is_reconciled_on_retry(
        self,
    ) -> None:
        feature = "wtfailedcrash"
        target = self.make_target_repo("target-failed-crash")
        self.env["CMUX_READY"] = "0"
        self.env["FLEET_TEST_PUBLICATION_CRASH_AT"] = (
            "after_writer_retirement_delete_entry"
        )

        failed = self.run_fleet(
            feature,
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )

        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertFalse((self.runs / f"fleet-{feature}.manifest").exists())
        self.assertTrue(
            list(
                self.runs.glob(
                    f".fleet-{feature}.build.*.clone-retirement-tombstone.json"
                )
            )
        )
        self.assertTrue(
            list(
                (self.tmp / "worktrees" / ".fleet-control-staging").glob(
                    f"{feature}--build--*"
                )
            )
        )
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

        self.env.pop("CMUX_READY")
        self.env.pop("FLEET_TEST_PUBLICATION_CRASH_AT")
        retried = self.run_fleet(
            feature,
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )

        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertFalse(
            list(
                self.runs.glob(
                    f".fleet-{feature}.build.*.clone-retirement-tombstone.json"
                )
            )
        )
        self.assertEqual(
            subprocess.run(
                ["bash", str(FLEET_DOWN), feature],
                cwd=ROOT,
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            ).returncode,
            0,
        )

    def test_partial_writer_clone_is_journaled_and_never_published_or_deleted(
        self,
    ) -> None:
        feature = "partial-writer-clone"
        target = self.make_target_repo("target-partial-writer")
        self.env["FLEET_TEST_FAIL_CLONE_INSTANCE"] = "build"

        failed = self.run_fleet(
            feature,
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )

        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertFalse((self.tmp / "worktrees" / f"{feature}-build").exists())
        staged = list(
            (self.tmp / "worktrees" / ".fleet-control-staging").glob(
                f"{feature}--build--*"
            )
        )
        self.assertEqual(len(staged), 1)
        self.assertTrue(
            list(self.runs.glob(f".fleet-{feature}.build.*.clone-stage-intent.json"))
        )
        self.assertFalse((self.runs / f"fleet-{feature}.manifest").exists())
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )
        self.env.pop("FLEET_TEST_FAIL_CLONE_INSTANCE")
        retry = self.run_fleet(
            feature,
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )
        self.assertEqual(retry.returncode, 2, retry.stderr)
        self.assertIn("partial clone is preserved", retry.stderr)
        self.assertTrue(staged[0].exists())

    def test_creation_plan_only_sigkill_is_recovered_before_git(self) -> None:
        feature = "creation-plan-crash"
        target = self.make_target_repo("target-creation-plan")
        self.env["FLEET_TEST_BOOT_CRASH_AT"] = "after_clone_creation_plan"

        crashed = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )

        self.assertEqual(crashed.returncode, -signal.SIGKILL, crashed.stderr)
        source = self.tmp / "worktrees" / f"{feature}-build"
        self.assertFalse(source.exists())
        self.assertEqual(
            len(
                list(
                    self.runs.glob(
                        f".fleet-{feature}.build.*.clone-creation-intent.json"
                    )
                )
            ),
            1,
        )
        self.assertFalse(
            list(
                self.runs.glob(f".fleet-{feature}.build.*.clone-creation-binding.json")
            )
        )
        self.assertFalse((self.runs / f"fleet-{feature}.manifest").exists())

        self.env.pop("FLEET_TEST_BOOT_CRASH_AT")
        recovered = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse(
            list(self.runs.glob(f".fleet-{feature}.build.*.clone-creation-*.json"))
        )
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )
        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)

    def test_creation_plan_atomic_pending_is_recovered_before_source_creation(
        self,
    ) -> None:
        feature = "creation-plan-pending"
        target = self.make_target_repo("target-creation-plan-pending")
        self.env["FLEET_TEST_SAFE_PATH_CRASH_AT"] = "after_atomic_pending_fsync"

        interrupted = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )

        self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
        self.assertFalse((self.tmp / "worktrees" / f"{feature}-build").exists())
        self.assertFalse(
            list(self.runs.glob(f".fleet-{feature}.build.*.clone-creation-intent.json"))
        )
        self.assertTrue(list(self.runs.glob(".fleet-atomic-*.tmp")))

        self.env.pop("FLEET_TEST_SAFE_PATH_CRASH_AT")
        recovered = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)

    def test_creation_recovers_sigkill_between_source_mkdir_and_binding(self) -> None:
        feature = "creation-mkdir-crash"
        target = self.make_target_repo("target-creation-mkdir")
        self.env["FLEET_TEST_BOOT_CRASH_AT"] = "after_clone_source_mkdir_before_binding"

        crashed = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )

        self.assertEqual(crashed.returncode, -signal.SIGKILL, crashed.stderr)
        source = self.tmp / "worktrees" / f"{feature}-build"
        self.assertTrue(source.is_dir())
        self.assertEqual(list(source.iterdir()), [])
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o700)
        plans = list(
            self.runs.glob(f".fleet-{feature}.build.*.clone-creation-intent.json")
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].stat().st_nlink, 1)
        self.assertFalse(
            list(
                self.runs.glob(f".fleet-{feature}.build.*.clone-creation-binding.json")
            )
        )

        self.env.pop("FLEET_TEST_BOOT_CRASH_AT")
        recovered = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse(
            list(self.runs.glob(f".fleet-{feature}.build.*.clone-creation-*.json"))
        )
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )
        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)

    def test_creation_binding_atomic_pending_is_recovered_before_git(self) -> None:
        feature = "creation-binding-pending"
        target = self.make_target_repo("target-creation-binding-pending")
        self.env["FLEET_TEST_CREATION_BINDING_SAFE_CRASH_AT"] = (
            "after_atomic_pending_fsync"
        )

        interrupted = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )

        self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
        source = self.tmp / "worktrees" / f"{feature}-build"
        self.assertTrue(source.is_dir())
        self.assertEqual(list(source.iterdir()), [])
        self.assertTrue(
            list(self.runs.glob(f".fleet-{feature}.build.*.clone-creation-intent.json"))
        )
        self.assertFalse(
            list(
                self.runs.glob(f".fleet-{feature}.build.*.clone-creation-binding.json")
            )
        )
        self.assertTrue(list(self.runs.glob(".fleet-atomic-*.tmp")))

        self.env.pop("FLEET_TEST_CREATION_BINDING_SAFE_CRASH_AT")
        recovered = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)

    def test_creation_recovers_complete_clone_sigkill_before_registration(self) -> None:
        feature = "creation-ready-crash"
        target = self.make_target_repo("target-creation-ready")
        self.env["FLEET_TEST_BOOT_CRASH_AT"] = (
            "after_writer_clone_ready_before_registration"
        )

        crashed = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )

        self.assertEqual(crashed.returncode, -signal.SIGKILL, crashed.stderr)
        source = self.tmp / "worktrees" / f"{feature}-build"
        self.assertTrue((source / ".git").is_dir())
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(source), "symbolic-ref", "--short", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip(),
            f"fleet/{feature}/build",
        )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(source), "remote"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip(),
            "",
        )
        self.assertTrue(
            list(self.runs.glob(f".fleet-{feature}.build.*.clone-creation-intent.json"))
        )
        self.assertTrue(
            list(
                self.runs.glob(f".fleet-{feature}.build.*.clone-creation-binding.json")
            )
        )
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

        self.env.pop("FLEET_TEST_BOOT_CRASH_AT")
        recovered = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)

    def test_manifest_publish_crash_keeps_live_clone_out_of_reconciliation(
        self,
    ) -> None:
        feature = "creation-manifest-crash"
        target = self.make_target_repo("target-creation-manifest")
        self.env["FLEET_TEST_BOOT_CRASH_AT"] = (
            "after_manifest_publish_before_creation_clear"
        )

        crashed = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )

        self.assertEqual(crashed.returncode, -signal.SIGKILL, crashed.stderr)
        source = self.tmp / "worktrees" / f"{feature}-build"
        source_binding = (source.stat().st_dev, source.stat().st_ino)
        self.assertTrue((self.runs / f"fleet-{feature}.manifest").is_file())
        self.assertTrue(
            list(self.runs.glob(f".fleet-{feature}.build.*.clone-creation-intent.json"))
        )
        self.assertTrue(
            list(
                self.runs.glob(f".fleet-{feature}.build.*.clone-creation-binding.json")
            )
        )

        self.env.pop("FLEET_TEST_BOOT_CRASH_AT")
        retry = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(retry.returncode, 2, retry.stderr)
        self.assertIn("Manifest already exists", retry.stderr)
        self.assertEqual((source.stat().st_dev, source.stat().st_ino), source_binding)
        self.assertFalse(
            list(
                (self.tmp / "worktrees" / ".fleet-control-staging").glob(
                    f"{feature}--build--*"
                )
            )
        )

        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertFalse(
            list(self.runs.glob(f".fleet-{feature}.build.*.clone-creation-*.json"))
        )

    def test_creation_clear_midpoint_crash_is_owned_by_manifest_and_fleet_down(
        self,
    ) -> None:
        feature = "creation-clear-crash"
        target = self.make_target_repo("target-creation-clear")
        self.env["FLEET_TEST_BOOT_CRASH_AT"] = "after_creation_binding_clear"

        crashed = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )

        self.assertEqual(crashed.returncode, -signal.SIGKILL, crashed.stderr)
        source = self.tmp / "worktrees" / f"{feature}-build"
        self.assertTrue(source.is_dir())
        self.assertTrue((self.runs / f"fleet-{feature}.manifest").is_file())
        self.assertTrue(
            list(self.runs.glob(f".fleet-{feature}.build.*.clone-creation-intent.json"))
        )
        self.assertFalse(
            list(
                self.runs.glob(f".fleet-{feature}.build.*.clone-creation-binding.json")
            )
        )

        self.env.pop("FLEET_TEST_BOOT_CRASH_AT")
        retry = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(retry.returncode, 2, retry.stderr)
        self.assertIn("Manifest already exists", retry.stderr)
        self.assertTrue(source.is_dir())

        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertFalse(
            list(self.runs.glob(f".fleet-{feature}.build.*.clone-creation-*.json"))
        )

        restarted = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        reclosed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(reclosed.returncode, 0, reclosed.stderr)

    def test_clone_source_without_creation_plan_fails_closed(self) -> None:
        feature = "creation-orphan"
        target = self.make_target_repo("target-creation-orphan")
        root = self.tmp / "worktrees"
        root.mkdir(mode=0o700)
        root.chmod(0o700)
        source = root / f"{feature}-build"
        source.mkdir(mode=0o700)
        marker = source / "do-not-adopt"
        marker.write_text("preserve\n", encoding="utf-8")
        binding = (source.stat().st_dev, source.stat().st_ino)

        refused = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("without its exact creation plan", refused.stderr)
        self.assertEqual((source.stat().st_dev, source.stat().st_ino), binding)
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve\n")
        self.assertFalse(list(self.runs.glob(f".fleet-{feature}.build.*.clone-*.json")))
        self.assertFalse(
            any(call and call[0] == "new-workspace" for call in self.calls())
        )

    def test_concurrent_boot_cannot_reconcile_live_creation_owner(self) -> None:
        feature = "creation-concurrent"
        target = self.make_target_repo("target-creation-concurrent")
        resume = self.tmp / "resume-concurrent-boot"
        first_env = self.env.copy()
        first_env["FLEET_TEST_BOOT_PAUSE_AT"] = (
            "after_writer_clone_ready_before_registration"
        )
        first_env["FLEET_TEST_BOOT_RESUME_FILE"] = str(resume)
        first = subprocess.Popen(
            [
                "bash",
                str(FLEET_UP),
                feature,
                "--preset",
                "implementation_review",
                "--target-repo",
                str(target),
            ],
            cwd=ROOT,
            env=first_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            source = self.tmp / "worktrees" / f"{feature}-build"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if (source / ".git").is_dir() and list(
                    self.runs.glob(
                        f".fleet-{feature}.build.*.clone-creation-binding.json"
                    )
                ):
                    break
                if first.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertIsNone(first.poll(), "first fleet-up exited before pause")
            self.assertTrue((source / ".git").is_dir())
            source_binding = (source.stat().st_dev, source.stat().st_ino)

            second = self.run_fleet(
                feature,
                "--preset",
                "implementation_review",
                "--target-repo",
                str(target),
            )

            self.assertEqual(second.returncode, 2, second.stderr)
            self.assertIn("boot admission", second.stderr)
            self.assertEqual(
                (source.stat().st_dev, source.stat().st_ino), source_binding
            )
            self.assertFalse(
                list(
                    (self.tmp / "worktrees" / ".fleet-control-staging").glob(
                        f"{feature}--build--*"
                    )
                )
            )
            self.assertFalse(
                list(
                    self.runs.glob(f".fleet-{feature}.build.*.clone-stage-intent.json")
                )
            )
            self.assertFalse(
                any(call and call[0] == "new-workspace" for call in self.calls())
            )

            resume.write_text("continue\n", encoding="utf-8")
            first_stdout, first_stderr = first.communicate(timeout=20)
            self.assertEqual(first.returncode, 0, first_stderr + first_stdout)
        finally:
            if first.poll() is None:
                resume.write_text("continue\n", encoding="utf-8")
                first.kill()
                first.communicate(timeout=5)

        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)

    def test_boot_cannot_race_paused_teardown_archive(self) -> None:
        feature = "teardown-boot-lock"
        first_target = self.make_target_repo("target-teardown-lock-a")
        second_target = self.make_target_repo("target-teardown-lock-b")
        started = self.run_fleet(
            feature,
            "--preset",
            "implementation_review",
            "--target-repo",
            str(first_target),
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        resume = self.tmp / "resume-teardown-lock"
        down_env = self.env.copy()
        down_env["FLEET_TEST_PUBLICATION_PAUSE_AT"] = "after_manifest_archive"
        down_env["FLEET_TEST_PUBLICATION_RESUME_FILE"] = str(resume)
        closing = subprocess.Popen(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=down_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if not (self.runs / f"fleet-{feature}.manifest").exists() and list(
                    self.runs.glob(f".fleet-{feature}.*.archive-intent.json")
                ):
                    break
                if closing.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertIsNone(closing.poll(), "fleet-down exited before archive pause")
            new_workspace_before = sum(
                1 for call in self.calls() if call and call[0] == "new-workspace"
            )

            refused = self.run_fleet(
                feature,
                "--preset",
                "implementation_review",
                "--target-repo",
                str(second_target),
            )

            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn("boot admission", refused.stderr)
            self.assertEqual(
                sum(1 for call in self.calls() if call and call[0] == "new-workspace"),
                new_workspace_before,
            )
            self.assertFalse((self.tmp / "worktrees" / f"{feature}-build").exists())

            resume.write_text("continue\n", encoding="utf-8")
            down_stdout, down_stderr = closing.communicate(timeout=20)
            self.assertEqual(closing.returncode, 0, down_stderr + down_stdout)
        finally:
            if closing.poll() is None:
                resume.write_text("continue\n", encoding="utf-8")
                closing.kill()
                closing.communicate(timeout=5)

    def test_teardown_cannot_race_live_boot_after_manifest_publish(self) -> None:
        feature = "boot-down-lock"
        target = self.make_target_repo("target-boot-down-lock")
        resume = self.tmp / "resume-live-boot"
        up_env = self.env.copy()
        up_env["FLEET_TEST_BOOT_PAUSE_AT"] = (
            "after_manifest_publish_before_creation_clear"
        )
        up_env["FLEET_TEST_BOOT_RESUME_FILE"] = str(resume)
        starting = subprocess.Popen(
            [
                "bash",
                str(FLEET_UP),
                feature,
                "--preset",
                "implementation_review",
                "--target-repo",
                str(target),
            ],
            cwd=ROOT,
            env=up_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            manifest = self.runs / f"fleet-{feature}.manifest"
            source = self.tmp / "worktrees" / f"{feature}-build"
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if manifest.is_file() and source.is_dir():
                    break
                if starting.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertIsNone(starting.poll(), "fleet-up exited before manifest pause")
            source_binding = (source.stat().st_dev, source.stat().st_ino)
            manifest_content = manifest.read_bytes()

            refused = subprocess.run(
                ["bash", str(FLEET_DOWN), feature],
                cwd=ROOT,
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )

            self.assertEqual(refused.returncode, 75, refused.stderr)
            self.assertIn("concurrent teardown", refused.stderr)
            self.assertEqual(
                (source.stat().st_dev, source.stat().st_ino), source_binding
            )
            self.assertEqual(manifest.read_bytes(), manifest_content)
            self.assertFalse(json.loads(self.state.read_text()).get("closed", False))

            resume.write_text("continue\n", encoding="utf-8")
            up_stdout, up_stderr = starting.communicate(timeout=20)
            self.assertEqual(starting.returncode, 0, up_stderr + up_stdout)
        finally:
            if starting.poll() is None:
                resume.write_text("continue\n", encoding="utf-8")
                starting.kill()
                starting.communicate(timeout=5)

        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)

    def test_boot_refuses_crashed_archive_until_fleet_down_recovers(self) -> None:
        feature = "teardown-archive-recovery"
        first_target = self.make_target_repo("target-archive-recovery-a")
        second_target = self.make_target_repo("target-archive-recovery-b")
        started = self.run_fleet(
            feature,
            "--preset",
            "implementation_review",
            "--target-repo",
            str(first_target),
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        self.env["FLEET_TEST_PUBLICATION_CRASH_AT"] = "after_manifest_archive"

        crashed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(crashed.returncode, -signal.SIGKILL, crashed.stderr)
        self.assertFalse((self.runs / f"fleet-{feature}.manifest").exists())
        self.assertTrue(list(self.runs.glob(f".fleet-{feature}.*.archive-intent.json")))
        self.env.pop("FLEET_TEST_PUBLICATION_CRASH_AT")
        new_workspace_before = sum(
            1 for call in self.calls() if call and call[0] == "new-workspace"
        )

        refused = self.run_fleet(
            feature,
            "--preset",
            "implementation_review",
            "--target-repo",
            str(second_target),
        )

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("pending recovery", refused.stderr)
        self.assertEqual(
            sum(1 for call in self.calls() if call and call[0] == "new-workspace"),
            new_workspace_before,
        )
        self.assertFalse((self.tmp / "worktrees" / f"{feature}-build").exists())

        recovered = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse(
            list(self.runs.glob(f".fleet-{feature}.*.archive-intent.json"))
        )

        restarted = self.run_fleet(
            feature,
            "--preset",
            "implementation_review",
            "--target-repo",
            str(second_target),
        )
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        reclosed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(reclosed.returncode, 0, reclosed.stderr)

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

    def test_specialist_clone_root_cannot_overlap_controller_or_target_state(
        self,
    ) -> None:
        target = self.make_target_repo("target-overlap")
        original_root = self.env["FLEET_WORKTREES_DIR"]
        for index, unsafe_root in enumerate(
            (
                target / "reader-clones",
                self.runs / "reader-clones",
                ROOT / ".reader-clones",
            )
        ):
            with self.subTest(root=unsafe_root):
                self.env["FLEET_WORKTREES_DIR"] = str(unsafe_root)
                result = self.run_fleet(
                    f"overlap-{index}",
                    "--preset",
                    "implementation_review",
                    "--target-repo",
                    str(target),
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unsafe worktree root", result.stderr)
                self.assertFalse(unsafe_root.exists())
                self.assertFalse(
                    (self.runs / f"fleet-overlap-{index}.manifest").exists()
                )
                self.assertFalse(
                    any(call[:1] == ["new-workspace"] for call in self.calls())
                )
        self.env["FLEET_WORKTREES_DIR"] = original_root

    def test_specialist_clone_root_requires_owner_0700_before_git_or_cmux(self) -> None:
        target = self.make_target_repo("target-root-mode")
        unsafe_root = self.tmp / "unsafe-mode-worktrees"
        unsafe_root.mkdir(mode=0o755)
        unsafe_root.chmod(0o755)
        self.env["FLEET_WORKTREES_DIR"] = str(unsafe_root)

        refused = self.run_fleet(
            "root-mode",
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
        )

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("0700", refused.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(stat.S_IMODE(unsafe_root.stat().st_mode), 0o755)
        self.assertFalse((self.runs / "fleet-root-mode.manifest").exists())
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    "refs/heads/fleet/root-mode/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

    def test_teardown_rejects_changed_clone_root_overlapping_target(self) -> None:
        target = self.make_target_repo("target-down-overlap")
        feature = "down-overlap"
        result = self.run_fleet(
            feature, "--preset", "implementation_review", "--target-repo", str(target)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        unsafe_root = target / "reader-clones"
        unsafe_root.mkdir()
        self.env["FLEET_WORKTREES_DIR"] = str(unsafe_root)
        close_calls = sum(call[:1] == ["close-workspace"] for call in self.calls())
        refused = subprocess.run(
            ["bash", str(FLEET_DOWN), feature],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertIn("isolated specialist root", refused.stderr)
        self.assertEqual(
            sum(call[:1] == ["close-workspace"] for call in self.calls()),
            close_calls,
        )
        self.assertTrue((self.runs / f"fleet-{feature}.manifest").exists())
        self.assertNotEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show-ref",
                    "--verify",
                    f"refs/heads/fleet/{feature}/build",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode,
            0,
        )

    def test_target_repo_subdirectory_is_rejected_before_cmux(self) -> None:
        target = self.make_target_repo("target-subdir")
        subdir = target / "docs"
        subdir.mkdir()
        result = self.run_fleet(
            "wtsubdir",
            "--preset",
            "implementation_review",
            "--target-repo",
            str(subdir),
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("exact physical Git toplevel", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.runs / "fleet-wtsubdir.manifest").exists())

    def test_expected_base_sha_drift_is_rejected_before_cmux(self) -> None:
        target = self.make_target_repo("target-base-drift")
        frozen = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(target), "commit", "--allow-empty", "-q", "-m", "drift"],
            check=True,
        )
        result = self.run_fleet(
            "wtbasedrift",
            "--preset",
            "implementation_review",
            "--target-repo",
            str(target),
            "--expected-base-sha",
            frozen,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("HEAD drifted", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.runs / "fleet-wtbasedrift.manifest").exists())

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


class FleetCmuxLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs = self.tmp / "runs"
        self.runs.mkdir(mode=0o700)
        self.runs.chmod(0o700)

    def create(self, *, command_shell: str | None = None) -> dict[str, str]:
        return fleet_cmux_launcher.create_spec(
            self.runs,
            label="lead/codex",
            cwd=self.tmp,
            environment={"FLEET_EXECUTION_PROFILE": "native"},
            command_shell=command_shell or shlex.join(["/usr/bin/true"]),
        )

    def paths(self, descriptor: dict[str, str]) -> tuple[Path, Path, Path]:
        launch_dir = self.runs / "cmux-launches" / descriptor["launch_id"]
        return launch_dir, launch_dir / "spec.json", launch_dir / "accepted.json"

    def test_private_spec_and_receipt_are_exact_and_second_run_cannot_replay(
        self,
    ) -> None:
        marker = self.tmp / "executions.txt"
        command = shlex.join(
            [
                sys.executable,
                "-c",
                f"open({str(marker)!r}, 'a', encoding='utf-8').write('executed\\n')",
            ]
        )
        descriptor = self.create(command_shell=command)
        launch_dir, spec_path, receipt_path = self.paths(descriptor)
        invocation = [
            sys.executable,
            str(CMUX_LAUNCHER),
            "run",
            str(self.runs),
            descriptor["launch_id"],
            descriptor["spec_sha256"],
        ]

        first = subprocess.run(invocation, text=True, capture_output=True, check=False)
        second = subprocess.run(invocation, text=True, capture_output=True, check=False)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 2)
        self.assertEqual(marker.read_text(encoding="utf-8"), "executed\n")
        receipt = fleet_cmux_launcher.verify_acceptance(
            self.runs,
            descriptor["launch_id"],
            expected_digest=descriptor["spec_sha256"],
        )
        self.assertEqual(receipt["state"], "accepted")
        self.assertEqual(receipt["spec_sha256"], descriptor["spec_sha256"])
        self.assertEqual(
            stat.S_IMODE((self.runs / "cmux-launches").stat().st_mode), 0o700
        )
        self.assertEqual(stat.S_IMODE(launch_dir.stat().st_mode), 0o700)
        for path in (spec_path, receipt_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_uid, os.geteuid())
            self.assertEqual(path.stat().st_nlink, 1)

    def test_strict_json_rejects_duplicate_nonfinite_and_naive_timestamp(self) -> None:
        descriptor = self.create()
        _, spec_path, _ = self.paths(descriptor)
        original = spec_path.read_bytes()

        spec_path.write_bytes(original.replace(b"{", b'{"schema_version":1,', 1))
        with self.assertRaisesRegex(fleet_cmux_launcher.LaunchError, "duplicate field"):
            fleet_cmux_launcher.read_spec(
                self.runs,
                descriptor["launch_id"],
                expected_digest=descriptor["spec_sha256"],
            )

        spec_path.write_bytes(
            original.replace(b'"schema_version":1', b'"schema_version":NaN')
        )
        with self.assertRaisesRegex(fleet_cmux_launcher.LaunchError, "non-finite"):
            fleet_cmux_launcher.read_spec(
                self.runs,
                descriptor["launch_id"],
                expected_digest=descriptor["spec_sha256"],
            )

        value = json.loads(original)
        value["created_at"] = "2026-07-17T12:00:00"
        value["spec_sha256"] = fleet_cmux_launcher._digest(value)
        spec_path.write_bytes(fleet_cmux_launcher._canonical_bytes(value))
        with self.assertRaisesRegex(
            fleet_cmux_launcher.LaunchError, "explicit UTC timezone"
        ):
            fleet_cmux_launcher.read_spec(
                self.runs,
                descriptor["launch_id"],
                expected_digest=value["spec_sha256"],
            )

    def test_spec_path_mode_link_and_replacement_fail_before_execution(self) -> None:
        for attack in ("mode", "hardlink", "replacement"):
            with self.subTest(attack=attack):
                isolated = self.tmp / attack
                isolated.mkdir(mode=0o700)
                marker = isolated / "must-not-execute"
                descriptor = fleet_cmux_launcher.create_spec(
                    self.runs,
                    label=f"agent/{attack}",
                    cwd=isolated,
                    environment={},
                    command_shell=shlex.join(["/usr/bin/touch", str(marker)]),
                )
                _, spec_path, _ = self.paths(descriptor)
                if attack == "mode":
                    spec_path.chmod(0o644)
                elif attack == "hardlink":
                    os.link(spec_path, isolated / "spec-hardlink.json")
                else:
                    replacement = isolated / "replacement.json"
                    replacement.write_text("{}\n", encoding="utf-8")
                    replacement.chmod(0o600)
                    os.replace(replacement, spec_path)

                with self.assertRaises(
                    (fleet_cmux_launcher.LaunchError, SafePathError)
                ):
                    fleet_cmux_launcher.execute_spec(
                        self.runs,
                        descriptor["launch_id"],
                        expected_digest=descriptor["spec_sha256"],
                    )
                self.assertFalse(marker.exists())

    def test_receipt_mode_link_and_replacement_are_rejected(self) -> None:
        for attack in ("mode", "hardlink", "replacement"):
            with self.subTest(attack=attack):
                descriptor = self.create()
                fleet_cmux_launcher.publish_acceptance(
                    self.runs,
                    descriptor["launch_id"],
                    expected_digest=descriptor["spec_sha256"],
                )
                launch_dir, _, receipt_path = self.paths(descriptor)
                if attack == "mode":
                    receipt_path.chmod(0o644)
                elif attack == "hardlink":
                    os.link(receipt_path, launch_dir / "receipt-hardlink.json")
                else:
                    replacement = self.tmp / f"{descriptor['launch_id']}.json"
                    replacement.write_text("{}\n", encoding="utf-8")
                    replacement.chmod(0o600)
                    os.replace(replacement, receipt_path)

                with self.assertRaises(
                    (fleet_cmux_launcher.LaunchError, SafePathError)
                ):
                    fleet_cmux_launcher.verify_acceptance(
                        self.runs,
                        descriptor["launch_id"],
                        expected_digest=descriptor["spec_sha256"],
                    )

    def test_symlinked_ancestor_or_receipt_leaf_fails_closed(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir(mode=0o700)
        symlink_runs = self.tmp / "symlink-runs"
        symlink_runs.mkdir(mode=0o700)
        (symlink_runs / "cmux-launches").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(SafePathError):
            fleet_cmux_launcher.create_spec(
                symlink_runs,
                label="lead/codex",
                cwd=self.tmp,
                environment={},
                command_shell="/usr/bin/true",
            )

        descriptor = self.create()
        _, _, receipt_path = self.paths(descriptor)
        outside_receipt = outside / "accepted.json"
        outside_receipt.write_text("{}\n", encoding="utf-8")
        outside_receipt.chmod(0o600)
        receipt_path.symlink_to(outside_receipt)
        with self.assertRaises(SafePathError):
            fleet_cmux_launcher.publish_acceptance(
                self.runs,
                descriptor["launch_id"],
                expected_digest=descriptor["spec_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
