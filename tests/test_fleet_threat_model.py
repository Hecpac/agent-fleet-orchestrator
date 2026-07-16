from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "fleet-threat-model.md"
ROUTER = ROOT / "orchestration" / "router.yaml"
CLAUDE_SETTINGS = ROOT / "orchestration" / "claude-fleet-settings.json"
OPENCODE_AGENTS = (
    ROOT / ".opencode" / "agents" / "fleet-reviewer.md",
    ROOT / ".opencode" / "agents" / "minimax-checker.md",
    ROOT / ".opencode" / "agents" / "glm-challenger.md",
)
RUN_LOCAL_WORKER = ROOT / "scripts" / "run-local-worker.sh"
FLEET_WAIT = ROOT / "scripts" / "fleet_wait.py"
FLEET_RACE = ROOT / "scripts" / "fleet-race.sh"

sys.path.insert(0, str(ROOT / "scripts"))
import router_config  # noqa: E402


class FleetThreatModelTests(unittest.TestCase):
    """Lock the selected boundary, current controls, and explicit gaps."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.router = json.loads(ROUTER.read_text(encoding="utf-8"))

    def test_prompt_injection_is_bounded_by_role_controls(self) -> None:
        plan = router_config.build_plan(
            self.router,
            instance_specs=["writer=codex", "candidate=codex_candidate"],
            no_lead=True,
            run_healthcheck=False,
            check_runtime_availability=False,
        )
        instances = {item["instance_id"]: item for item in plan["instances"]}
        self.assertEqual(instances["writer"]["authority"], "write")
        self.assertIn("workspace-write", instances["writer"]["command"])
        self.assertEqual(instances["candidate"]["authority"], "advisory")
        self.assertIn("read-only", instances["candidate"]["command"])
        for instance in instances.values():
            self.assertIn("--ask-for-approval", instance["command"])
            approval_index = instance["command"].index("--ask-for-approval")
            self.assertEqual(instance["command"][approval_index + 1], "never")

    def test_claude_policy_does_not_auto_allow_cmux_or_mutation(self) -> None:
        settings = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
        self.assertTrue(settings["sandbox"]["enabled"])
        self.assertTrue(settings["sandbox"]["failIfUnavailable"])
        self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
        self.assertEqual(
            settings["permissions"]["allow"],
            ["Read(/__FLEET_REPO_ROOT__/orchestration/runs/prompts/**)"],
        )
        self.assertFalse(
            any("cmux" in permission.lower() for permission in settings["permissions"]["allow"])
        )

    def test_opencode_reviewers_default_deny_process_and_external_access(self) -> None:
        for path in OPENCODE_AGENTS:
            policy = path.read_text(encoding="utf-8")
            self.assertIn('  "*": deny', policy)
            self.assertIn('    "*": allow', policy)
            self.assertIn('    "*.env": deny', policy)
            self.assertIn('    "*.env.*": deny', policy)
            self.assertIn("  external_directory: deny", policy)
            self.assertNotIn("\n  bash:", policy)
            self.assertNotRegex(policy, r'(?m)^\s+"cmux(?:\s|\*)')
        for role_name in ("glm", "minimax", "minimax_candidate", "minimax_checker"):
            self.assertEqual(self.router["roles"][role_name]["tool_access"], ["filesystem_read"])
        self.assertIn("P1-OC1", self.doc)
        self.assertNotIn("P1 OPEN — does not yet satisfy boundary A", self.doc)

    def test_same_uid_process_can_rewrite_private_file_and_is_out_of_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.jsonl"
            evidence.write_text("original\n", encoding="utf-8")
            evidence.chmod(0o600)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path(__import__('sys').argv[1]).write_text('rewritten\\n')",
                    str(evidence),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o600)
            self.assertEqual(evidence.read_text(encoding="utf-8"), "rewritten\n")

    def test_control_is_trusted_and_can_reach_direct_entrypoints(self) -> None:
        lead = self.router["lead"]
        self.assertEqual(lead["authority"], "control")
        self.assertIn("shell", lead["tool_access"])
        self.assertIn("cmux", lead["tool_access"])
        direct = RUN_LOCAL_WORKER.read_text(encoding="utf-8")
        self.assertIn("orchestration/agents/local_worker.py", direct)
        self.assertNotIn("fleet_leases.py", direct)

    def test_same_model_custom_race_is_permitted_but_never_claims_assurance(self) -> None:
        default_roles = self.router["defaults"]["race_roles"]
        default_identities = {
            (
                self.router["roles"][role]["provider"],
                self.router["roles"][role]["model"],
                self.router["roles"][role].get("variant"),
            )
            for role in default_roles
        }
        self.assertEqual(len(default_identities), len(default_roles))
        custom = router_config.parse_instance_specs(
            self.router,
            ["candidate_a=codex_candidate", "candidate_b=codex_candidate"],
        )
        self.assertEqual([item["role_type"] for item in custom], ["codex_candidate"] * 2)
        race = FLEET_RACE.read_text(encoding="utf-8")
        self.assertIn("FIRST CANDIDATE (NOT VERIFIED)", race)
        self.assertIn("verify surface", race)
        self.assertNotIn("ASSURANCE PASSED", race)

    @unittest.skipUnless(hasattr(signal, "SIGSTOP"), "requires POSIX process signals")
    def test_wait_uses_posix_alarm_and_alarm_is_pending_after_process_stop(self) -> None:
        waiter_source = FLEET_WAIT.read_text(encoding="utf-8")
        self.assertIn("signal.alarm(timeout_sec)", waiter_source)
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,sys,time\n"
                "started=time.monotonic()\n"
                "def timeout(*_):\n"
                " print(f'ALARM {time.monotonic()-started:.3f}', flush=True)\n"
                " raise SystemExit(124)\n"
                "signal.signal(signal.SIGALRM, timeout)\n"
                "signal.alarm(1)\n"
                "print('READY', flush=True)\n"
                "while True: signal.pause()\n",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        assert child.stdout is not None
        self.assertEqual(child.stdout.readline().strip(), "READY")
        os.kill(child.pid, signal.SIGSTOP)
        time.sleep(1.25)
        os.kill(child.pid, signal.SIGCONT)
        stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 124, stderr)
        self.assertRegex(stdout.strip(), r"^ALARM [0-9]+\.[0-9]{3}$")
        self.assertGreaterEqual(float(stdout.split()[1]), 1.0)


if __name__ == "__main__":
    unittest.main()
