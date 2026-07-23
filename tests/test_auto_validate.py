from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import textwrap
import unittest

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "fusion"))

import fleet_json  # noqa: E402

HARNESS = ROOT / "scripts" / "fusion" / "fusion_harness.py"
PROMPTS = ROOT / "scripts" / "fusion" / "prompts"

TOY_GATE = '''\
# /// script
# requires-python = ">=3.10"
# ///
import sys
from pathlib import Path

target = Path(sys.argv[1]) / "hello.txt"
if target.is_file() and "hello" in target.read_text(encoding="utf-8"):
    print("PASS: hello.txt present with greeting")
    raise SystemExit(0)
print(
    f"FAIL: expected hello.txt containing 'hello', found missing, "
    f"at {target} — create it"
)
raise SystemExit(1)
'''

ALWAYS_GREEN_GATE = '''\
# /// script
# requires-python = ">=3.10"
# ///
print("PASS: trivially green")
raise SystemExit(0)
'''


class AutoValidateBase(unittest.TestCase):
    """Shared shim scaffolding for every auto-validate test class."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.out = self.tmp / "fusion-out"
        (self.tmp / "gate-fixture.py").write_text(TOY_GATE, encoding="utf-8")
        (self.tmp / "triage-fixture.txt").write_text(
            "diagnosis text\nTRIAGE_VERDICT: BUILDER_DEFECT — write the file\n",
            encoding="utf-8",
        )
        # claude shim: VALIDATOR writes the gate fixture into its cwd;
        # TRIAGE prints the triage fixture. Distinguished by prompt content.
        self._executable(
            "claude",
            """
            #!/bin/sh
            n=$(cat "$FLEET_TEST_DIR/claude.count" 2>/dev/null || echo 0)
            n=$((n+1)); echo $n > "$FLEET_TEST_DIR/claude.count"
            printf '%s' "$*" > "$FLEET_TEST_DIR/claude.call.$n"
            case "$*" in
              *"# VALIDATOR"*)
                cp "$FLEET_TEST_DIR/gate-fixture.py" gate.py
                [ "${FLEET_TEST_VALIDATOR_EXTRA:-0}" = 1 ] && printf 'x' > extra.txt
                printf 'gate written\\n'
                ;;
              *"# TRIAGE"*)
                cat "$FLEET_TEST_DIR/triage-fixture.txt"
                ;;
              *)
                printf 'unexpected claude call\\n' >&2
                exit 3
                ;;
            esac
            """,
        )
        # codex shim: BUILDER. Succeeds (writes hello.txt into the -C
        # workspace) once the round counter reaches FLEET_TEST_SUCCEED_ON.
        # Emits a session id unless FLEET_TEST_NO_SESSION=1.
        self._executable(
            "codex",
            """
            #!/bin/sh
            n=$(cat "$FLEET_TEST_DIR/codex.count" 2>/dev/null || echo 0)
            n=$((n+1)); echo $n > "$FLEET_TEST_DIR/codex.count"
            printf '%s' "$*" > "$FLEET_TEST_DIR/codex.call.$n"
            ws=""
            prev=""
            for arg in "$@"; do
              [ "$prev" = "-C" ] && ws="$arg"
              prev="$arg"
            done
            if [ -n "$ws" ] && [ "$n" -ge "${FLEET_TEST_SUCCEED_ON:-1}" ]; then
              printf 'hello world\\n' > "$ws/hello.txt"
            fi
            [ "${FLEET_TEST_NO_SESSION:-0}" = 1 ] || \\
              printf 'session id: test-sess-%s\\n' "$n"
            printf 'builder round done\\n'
            """,
        )

    def _executable(self, name: str, source: str) -> None:
        path = self.bin / name
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _env(self, **extra: str) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FLEET_FUSION_OUTPUT_DIR": str(self.out),
            "FLEET_TEST_DIR": str(self.tmp),
            **extra,
        }

    def _run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(HARNESS), "auto-validate", *args],
            env=self._env(**env),
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )

    def _summary(self) -> dict:
        runs = [item for item in self.out.iterdir() if item.is_dir()]
        self.assertEqual(len(runs), 1)
        return fleet_json.load(runs[0] / "autovalidate" / "summary.json")


class AutoValidateTemplateTests(unittest.TestCase):
    def test_templates_declare_their_variables(self) -> None:
        expected = {
            "validator.md": {"{{TASK}}"},
            "builder_round.md": {
                "{{ROUND}}",
                "{{MAX_ROUNDS}}",
                "{{TASK_BLOCK}}",
                "{{GATE_SOURCE}}",
                "{{ROUND_FEEDBACK}}",
            },
            "triage.md": {
                "{{ROUND}}",
                "{{TASK}}",
                "{{GATE_SOURCE}}",
                "{{GATE_OUTPUT}}",
                "{{WORKSPACE}}",
            },
        }
        for name, variables in expected.items():
            text = (PROMPTS / name).read_text(encoding="utf-8")
            for variable in variables:
                self.assertIn(variable, text, f"{name} missing {variable}")

    def test_contract_language_is_present(self) -> None:
        validator = (PROMPTS / "validator.md").read_text(encoding="utf-8")
        self.assertIn("EXACTLY ONE file named gate.py", validator)
        self.assertIn("FAIL on an empty workspace", validator)
        self.assertIn("Do not write any other file", validator)
        self.assertIn('"FAIL: expected <X>, found <Y>, at <path>', validator)
        builder = (PROMPTS / "builder_round.md").read_text(encoding="utf-8")
        self.assertIn("IMMUTABLE", builder)
        triage = (PROMPTS / "triage.md").read_text(encoding="utf-8")
        self.assertIn("TRIAGE_VERDICT: BUILDER_DEFECT", triage)
        self.assertIn("TRIAGE_VERDICT: GATE_DEFECT", triage)


class GatePrimitiveTests(AutoValidateBase):
    def setUp(self) -> None:
        super().setUp()
        import auto_validate

        self.av = auto_validate
        self.av_dir = self.tmp / "avdir"
        self.av_dir.mkdir()
        self.workspace = self.av_dir / "workspace"
        self.workspace.mkdir()

    def test_render_av_template_is_single_pass_and_checks_unknowns(self) -> None:
        rendered = self.av.render_av_template(
            "triage.md",
            {
                "{{ROUND}}": "3",
                "{{TASK}}": "make {{GATE_SOURCE}} appear literally",
                "{{GATE_SOURCE}}": "print('gate')",
                "{{GATE_OUTPUT}}": "FAIL: x",
                "{{WORKSPACE}}": "/tmp/ws",
            },
        )
        self.assertIn("make {{GATE_SOURCE}} appear literally", rendered)
        self.assertEqual(rendered.count("print('gate')"), 1)

    def test_seal_and_run_gate_red_and_green(self) -> None:
        (self.av_dir / "gate.py").write_text(TOY_GATE, encoding="utf-8")
        digest = self.av.seal_gate(self.av_dir)
        self.assertEqual(len(digest), 64)
        result = self.av.run_gate(self.av_dir, self.workspace, timeout_seconds=60)
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(len(result["fail_lines"]), 1)
        self.assertFalse(result["tampered"])
        (self.workspace / "hello.txt").write_text("hello", encoding="utf-8")
        result = self.av.run_gate(self.av_dir, self.workspace, timeout_seconds=60)
        self.assertEqual(result["verdict"], "pass")

    def test_run_gate_restores_sealed_copy_and_records_tampering(self) -> None:
        (self.av_dir / "gate.py").write_text(TOY_GATE, encoding="utf-8")
        self.av.seal_gate(self.av_dir)
        (self.av_dir / "gate.py").write_text(ALWAYS_GREEN_GATE, encoding="utf-8")
        result = self.av.run_gate(self.av_dir, self.workspace, timeout_seconds=60)
        self.assertTrue(result["tampered"])
        self.assertEqual(result["verdict"], "fail")  # sealed toy gate ran, not the green one
        self.assertEqual(
            (self.av_dir / "gate.py").read_text(encoding="utf-8"), TOY_GATE
        )

    def test_run_gate_harness_errors_are_typed(self) -> None:
        (self.av_dir / "gate.py").write_text(TOY_GATE, encoding="utf-8")
        self.av.seal_gate(self.av_dir)
        slow = self.av_dir / "gate.py"
        slow.write_text(
            TOY_GATE.replace(
                "import sys", "import sys, time\ntime.sleep(30)"
            ),
            encoding="utf-8",
        )
        self.av.seal_gate(self.av_dir)  # reseal the slow gate deliberately
        result = self.av.run_gate(self.av_dir, self.workspace, timeout_seconds=1)
        self.assertEqual(result["verdict"], "harness-error")
        self.assertIn("timeout", result["harness_error"])

    def test_run_agent_accepts_cwd_override(self) -> None:
        import fusion_harness

        self._executable(
            "claude",
            """
            #!/bin/sh
            pwd > "$FLEET_TEST_DIR/agent.cwd"
            printf 'ok\\n'
            """,
        )
        env_patch = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FLEET_TEST_DIR": str(self.tmp)}
        old = {k: os.environ.get(k) for k in env_patch}
        os.environ.update(env_patch)
        try:
            fusion_harness.run_agent("probe", ["claude", "-p", "x"], 30, cwd=self.av_dir)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(
            Path((self.tmp / "agent.cwd").read_text(encoding="utf-8").strip()).resolve(),
            self.av_dir.resolve(),
        )

    def test_render_av_template_unknown_variable_fails_closed(self) -> None:
        with self.assertRaisesRegex(self.av.FusionError, "unresolved variables"):
            self.av.render_av_template("triage.md", {"{{ROUND}}": "1"})

    def test_run_gate_missing_uv_is_typed_harness_error(self) -> None:
        (self.av_dir / "gate.py").write_text(TOY_GATE, encoding="utf-8")
        self.av.seal_gate(self.av_dir)
        import unittest.mock as mock

        with mock.patch.object(self.av.subprocess, "run", side_effect=FileNotFoundError):
            result = self.av.run_gate(self.av_dir, self.workspace, timeout_seconds=5)
        self.assertEqual(result["verdict"], "harness-error")
        self.assertIn("uv is not available", result["harness_error"])

    def test_extract_session_id_takes_the_last_match(self) -> None:
        text = "I created session id: fake-prose\nwork...\nsession id: real-final\n"
        self.assertEqual(self.av.extract_session_id(text), "real-final")
        self.assertEqual(self.av.extract_session_id("no ids here"), "")


class AgentLegTests(AutoValidateBase):
    def setUp(self) -> None:
        super().setUp()
        import auto_validate

        self.av = auto_validate
        self.av_dir = self.tmp / "avdir"
        self.av_dir.mkdir()
        self.workspace = self.av_dir / "workspace"
        self.workspace.mkdir()
        self._patch_env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FLEET_TEST_DIR": str(self.tmp),
        }

    def _with_env(self, fn, *args, **kwargs):
        old = {k: os.environ.get(k) for k in self._patch_env}
        os.environ.update(self._patch_env)
        try:
            return fn(*args, **kwargs)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_validator_leg_accepts_exactly_gate_py(self) -> None:
        outcome = self._with_env(
            self.av.validator_leg, "workhorse", "make hello.txt", self.av_dir, 60
        )
        self.assertEqual(outcome["status"], "ok")
        self.assertTrue((self.av_dir / "gate.py").is_file())

    def test_validator_leg_rejects_extra_files(self) -> None:
        os.environ["FLEET_TEST_VALIDATOR_EXTRA"] = "1"
        self.addCleanup(os.environ.pop, "FLEET_TEST_VALIDATOR_EXTRA", None)
        with self.assertRaisesRegex(self.av.FusionError, "exactly gate.py"):
            self._with_env(
                self.av.validator_leg, "workhorse", "make hello.txt", self.av_dir, 60
            )

    def test_validator_leg_rejects_writes_into_workspace_subdir(self) -> None:
        self._executable(
            "claude",
            """
            #!/bin/sh
            cp "$FLEET_TEST_DIR/gate-fixture.py" gate.py
            printf 'seed' > workspace/seed.py
            printf 'gate written\\n'
            """,
        )
        (self.av_dir / "workspace").mkdir(exist_ok=True)
        with self.assertRaisesRegex(self.av.FusionError, "exactly gate.py"):
            self._with_env(
                self.av.validator_leg, "workhorse", "make hello.txt", self.av_dir, 60
            )

    def test_validator_leg_ignores_known_cli_droppings(self) -> None:
        self._executable(
            "claude",
            """
            #!/bin/sh
            cp "$FLEET_TEST_DIR/gate-fixture.py" gate.py
            printf '{}' > .claude.json
            printf 'gate written\\n'
            """,
        )
        outcome = self._with_env(
            self.av.validator_leg, "workhorse", "make hello.txt", self.av_dir, 60
        )
        self.assertEqual(outcome["status"], "ok")

    def test_builder_leg_first_round_then_resume_then_fallback(self) -> None:
        outcome, session, memory = self._with_env(
            self.av.builder_leg,
            "workhorse", 1, "make hello.txt", "gate src", "", self.workspace, "", 60,
        )
        self.assertEqual(memory, "first")
        self.assertEqual(session, "test-sess-1")
        outcome, session, memory = self._with_env(
            self.av.builder_leg,
            "workhorse", 2, "make hello.txt", "gate src", "FAIL: x", self.workspace, session, 60,
        )
        self.assertEqual(memory, "resume")
        argv2 = (self.tmp / "codex.call.2").read_text(encoding="utf-8")
        self.assertIn("resume test-sess-1", argv2)
        os.environ["FLEET_TEST_NO_SESSION"] = "1"
        self.addCleanup(os.environ.pop, "FLEET_TEST_NO_SESSION", None)
        outcome, session, memory = self._with_env(
            self.av.builder_leg,
            "workhorse", 3, "make hello.txt", "gate src", "FAIL: x", self.workspace, "", 60,
        )
        self.assertEqual(memory, "stateless-fallback")
        self.assertEqual(session, "")
        argv3 = (self.tmp / "codex.call.3").read_text(encoding="utf-8")
        self.assertIn("make hello.txt", argv3)

    def test_triage_leg_parses_verdicts(self) -> None:
        outcome, verdict, guidance = self._with_env(
            self.av.triage_leg,
            "workhorse", 3, "task", "gate src", "FAIL: x", self.workspace, 60,
        )
        self.assertEqual(verdict, "BUILDER_DEFECT")
        self.assertIn("write the file", guidance)
        (self.tmp / "triage-fixture.txt").write_text(
            "bad gate\nTRIAGE_VERDICT: GATE_DEFECT — checks wrong path\n",
            encoding="utf-8",
        )
        outcome, verdict, guidance = self._with_env(
            self.av.triage_leg,
            "workhorse", 4, "task", "gate src", "FAIL: x", self.workspace, 60,
        )
        self.assertEqual(verdict, "GATE_DEFECT")

    def test_triage_leg_malformed_verdict_is_none(self) -> None:
        (self.tmp / "triage-fixture.txt").write_text(
            "rambling with no verdict line\n", encoding="utf-8"
        )
        outcome, verdict, guidance = self._with_env(
            self.av.triage_leg,
            "workhorse", 3, "task", "gate src", "FAIL: x", self.workspace, 60,
        )
        self.assertIsNone(verdict)

    def test_triage_leg_takes_last_verdict_and_tolerates_indent(self) -> None:
        (self.tmp / "triage-fixture.txt").write_text(
            "TRIAGE_VERDICT: GATE_DEFECT — early draft\n"
            "reasoning...\n"
            "  TRIAGE_VERDICT: BUILDER_DEFECT — final call\n",
            encoding="utf-8",
        )
        outcome, verdict, guidance = self._with_env(
            self.av.triage_leg,
            "workhorse", 3, "task", "gate src", "FAIL: x", self.workspace, 60,
        )
        self.assertEqual(verdict, "BUILDER_DEFECT")
        self.assertIn("final call", guidance)


class AutoValidateCommandTests(AutoValidateBase):
    def test_green_round_one(self) -> None:
        result = self._run("make hello.txt", FLEET_TEST_SUCCEED_ON="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self._summary()
        self.assertEqual(summary["command"], "auto-validate")
        self.assertEqual(summary["status"], "green")
        self.assertEqual(len(summary["rounds"]), 1)
        self.assertEqual(summary["rounds"][0]["gate_verdict"], "pass")
        self.assertEqual(summary["gate_repairs"], 0)

    def test_green_round_three_feeds_fail_lines_forward(self) -> None:
        result = self._run("make hello.txt", FLEET_TEST_SUCCEED_ON="3")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self._summary()
        self.assertEqual(len(summary["rounds"]), 3)
        self.assertEqual(
            [item["gate_verdict"] for item in summary["rounds"]],
            ["fail", "fail", "pass"],
        )
        argv2 = (self.tmp / "codex.call.2").read_text(encoding="utf-8")
        self.assertIn("FAIL: expected hello.txt", argv2)

    def test_baseline_not_red_exits_6(self) -> None:
        (self.tmp / "gate-fixture.py").write_text(
            ALWAYS_GREEN_GATE, encoding="utf-8"
        )
        result = self._run("make hello.txt")

        self.assertEqual(result.returncode, 6, result.stderr)
        summary = self._summary()
        self.assertEqual(summary["status"], "baseline-not-red")
        self.assertEqual(summary["rounds"], [])
        self.assertFalse((self.tmp / "codex.count").exists(), "no builder round")

    def test_halt_after_five_rounds_exits_7(self) -> None:
        result = self._run("make hello.txt", FLEET_TEST_SUCCEED_ON="99")

        self.assertEqual(result.returncode, 7, result.stderr)
        summary = self._summary()
        self.assertEqual(summary["status"], "halted")
        self.assertEqual(len(summary["rounds"]), 5)
        ledger = (self.out / "ledger.jsonl").read_bytes()
        self.assertEqual(
            fleet_json.loads(ledger.splitlines()[0])["run_id"], summary["run_id"]
        )

    def test_validator_extra_file_exits_5(self) -> None:
        result = self._run(
            "make hello.txt", FLEET_TEST_VALIDATOR_EXTRA="1"
        )

        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertEqual(self._summary()["status"], "invalid-validator")

    def test_gate_tampering_is_recorded_and_sealed_copy_runs(self) -> None:
        self._executable(
            "codex",
            """
            #!/bin/sh
            n=$(cat "$FLEET_TEST_DIR/codex.count" 2>/dev/null || echo 0)
            n=$((n+1)); echo $n > "$FLEET_TEST_DIR/codex.count"
            printf '%s' "$*" > "$FLEET_TEST_DIR/codex.call.$n"
            ws=""; prev=""
            for arg in "$@"; do [ "$prev" = "-C" ] && ws="$arg"; prev="$arg"; done
            printf 'print("PASS: forged")\\nraise SystemExit(0)\\n' > "$ws/../gate.py"
            printf 'session id: s-%s\\n' "$n"
            """,
        )
        result = self._run("make hello.txt")

        self.assertEqual(result.returncode, 7, result.stderr)
        summary = self._summary()
        self.assertTrue(all(item["gate_tampered"] for item in summary["rounds"]))
        self.assertTrue(
            all(item["gate_verdict"] == "fail" for item in summary["rounds"])
        )

    def test_harness_error_round_is_not_charged(self) -> None:
        # Break uv for the FIRST gate run after baseline by shadowing it with
        # a one-shot failing shim, then restore. Simplest deterministic proxy:
        # break uv entirely -> baseline itself is a harness error, retried
        # once, then exit 2 with status harness-error and zero rounds charged.
        self._executable("uv", "#!/bin/sh\nexit 127\n")
        result = self._run("make hello.txt")

        self.assertEqual(result.returncode, 2, result.stderr)
        summary = self._summary()
        self.assertEqual(summary["status"], "harness-error")
        self.assertEqual(summary["rounds"], [])

    def test_memory_fallback_is_recorded(self) -> None:
        result = self._run(
            "make hello.txt",
            FLEET_TEST_SUCCEED_ON="2",
            FLEET_TEST_NO_SESSION="1",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self._summary()
        self.assertEqual(
            [item["memory"] for item in summary["rounds"]],
            ["first", "stateless-fallback"],
        )

    def test_empty_task_exits_2_cleanly_without_traceback(self) -> None:
        result = self._run("   ")

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.out.exists())

    def test_repair_rerun_harness_error_is_never_charged(self) -> None:
        wrong_gate = TOY_GATE.replace("hello.txt", "wrong.txt")
        (self.tmp / "gate-fixture.py").write_text(wrong_gate, encoding="utf-8")
        (self.tmp / "gate-fixture-fixed.py").write_text(TOY_GATE, encoding="utf-8")
        (self.tmp / "triage-fixture.txt").write_text(
            "gate checks wrong.txt\nTRIAGE_VERDICT: GATE_DEFECT — checks wrong.txt\n",
            encoding="utf-8",
        )
        self._executable(
            "claude",
            """
            #!/bin/sh
            case "$*" in
              *"corrected gate"*) cp "$FLEET_TEST_DIR/gate-fixture-fixed.py" gate.py; printf 'repaired\\n' ;;
              *"# VALIDATOR"*) cp "$FLEET_TEST_DIR/gate-fixture.py" gate.py; printf 'gate written\\n' ;;
              *"# TRIAGE"*) cat "$FLEET_TEST_DIR/triage-fixture.txt" ;;
            esac
            """,
        )
        real_uv = subprocess.run(["which", "uv"], capture_output=True, text=True).stdout.strip()
        self._executable(
            "uv",
            f"""
            #!/bin/sh
            n=$(cat "$FLEET_TEST_DIR/uv.count" 2>/dev/null || echo 0)
            n=$((n+1)); echo $n > "$FLEET_TEST_DIR/uv.count"
            if [ "$n" = 5 ]; then exit 127; fi
            exec {real_uv} "$@"
            """,
        )
        result = self._run("make hello.txt", FLEET_TEST_SUCCEED_ON="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self._summary()
        self.assertEqual(summary["status"], "green")
        self.assertEqual(summary["gate_repairs"], 1)
        harness_rounds = [r for r in summary["rounds"] if r["gate_verdict"] == "harness-error"]
        self.assertEqual(len(harness_rounds), 1)
        self.assertTrue(harness_rounds[0]["harness_errors"])
        he = harness_rounds[0]
        passing = [r for r in summary["rounds"] if r["gate_verdict"] == "pass"]
        self.assertEqual(he["n"], passing[-1]["n"], "repeat must not charge the round")

    def test_mid_round_harness_error_repeats_the_same_round(self) -> None:
        real_uv = subprocess.run(["which", "uv"], capture_output=True, text=True).stdout.strip()
        self._executable(
            "uv",
            f"""
            #!/bin/sh
            n=$(cat "$FLEET_TEST_DIR/uv.count" 2>/dev/null || echo 0)
            n=$((n+1)); echo $n > "$FLEET_TEST_DIR/uv.count"
            if [ "$n" = 2 ]; then exit 127; fi
            exec {real_uv} "$@"
            """,
        )
        result = self._run("make hello.txt", FLEET_TEST_SUCCEED_ON="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self._summary()
        self.assertEqual(summary["status"], "green")
        self.assertEqual(len(summary["rounds"]), 2)
        self.assertEqual([r["n"] for r in summary["rounds"]], [1, 1])
        self.assertEqual(
            [r["gate_verdict"] for r in summary["rounds"]],
            ["harness-error", "pass"],
        )
        self.assertTrue(summary["rounds"][0]["harness_errors"])


if __name__ == "__main__":
    unittest.main()
