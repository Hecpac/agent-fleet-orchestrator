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

import fleet_json  # noqa: E402

HARNESS = ROOT / "scripts" / "fusion" / "fusion_harness.py"


class FusionOpinionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.out = self.tmp / "fusion-out"
        self._executable(
            "claude",
            """
            #!/bin/sh
            printf '%s\\n' "$*" > "$FLEET_TEST_DIR/claude.argv"
            printf 'architect view of: %s\\n' "$2"
            """,
        )
        self._executable(
            "codex",
            """
            #!/bin/sh
            printf '%s\\n' "$*" > "$FLEET_TEST_DIR/codex.argv"
            printf 'builder view\\n'
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
            ["python3", str(HARNESS), "opinion", *args],
            env=self._env(**env),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def _summary(self) -> dict:
        runs = [item for item in self.out.iterdir() if item.is_dir()]
        self.assertEqual(len(runs), 1)
        return fleet_json.load(runs[0] / "opinion" / "summary.json")

    def test_opinion_runs_both_perspectives_and_writes_artifacts(self) -> None:
        result = self._run("pick a queue library")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self._summary()
        self.assertEqual(summary["command"], "opinion")
        self.assertEqual(summary["tier"], "workhorse")
        by_role = {item["role"]: item for item in summary["agents"]}
        self.assertEqual(set(by_role), {"architect", "builder"})
        for item in by_role.values():
            self.assertEqual(item["status"], "ok")
            artifact = Path(item["output_path"])
            self.assertTrue(artifact.is_file())
            self.assertGreater(item["output_chars"], 0)
        self.assertIn(
            "--model claude-sonnet-5",
            (self.tmp / "claude.argv").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "--model gpt-5.6-terra",
            (self.tmp / "codex.argv").read_text(encoding="utf-8"),
        )
        ledger = (self.out / "ledger.jsonl").read_bytes()
        self.assertEqual(
            fleet_json.loads(ledger.splitlines()[0])["run_id"], summary["run_id"]
        )

    def test_prompts_carry_role_split_and_question(self) -> None:
        result = self._run("should the ledger move to sqlite?")

        self.assertEqual(result.returncode, 0, result.stderr)
        claude_argv = (self.tmp / "claude.argv").read_text(encoding="utf-8")
        codex_argv = (self.tmp / "codex.argv").read_text(encoding="utf-8")
        self.assertIn("ARCHITECT perspective", claude_argv)
        self.assertIn("BUILDER perspective", codex_argv)
        for argv in (claude_argv, codex_argv):
            self.assertIn("should the ledger move to sqlite?", argv)
            self.assertNotIn("{{", argv)

    def test_sota_tier_rebinds_models_without_code_changes(self) -> None:
        result = self._run("q", "--tier", "sota")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "--model claude-fable-5",
            (self.tmp / "claude.argv").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "--model gpt-5.6-sol",
            (self.tmp / "codex.argv").read_text(encoding="utf-8"),
        )

    def test_panel_uses_overridable_local_command(self) -> None:
        self._executable(
            "panel-shim",
            """
            #!/bin/sh
            printf '%s\\n' "$1" > "$FLEET_TEST_DIR/panel.role"
            printf 'panel view\\n'
            """,
        )
        result = self._run(
            "q",
            "--panel",
            FLEET_FUSION_PANEL_CMD=str(self.bin / "panel-shim"),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self._summary()
        roles = {item["role"] for item in summary["agents"]}
        self.assertEqual(roles, {"architect", "builder", "panel"})
        self.assertEqual(
            (self.tmp / "panel.role").read_text(encoding="utf-8").strip(), "triage"
        )

    def test_failed_perspective_is_recorded_and_exit_is_partial(self) -> None:
        self._executable(
            "codex",
            """
            #!/bin/sh
            printf 'builder exploded\\n' >&2
            exit 9
            """,
        )
        result = self._run("q")

        self.assertEqual(result.returncode, 3, result.stderr)
        summary = self._summary()
        by_role = {item["role"]: item for item in summary["agents"]}
        self.assertEqual(by_role["architect"]["status"], "ok")
        self.assertEqual(by_role["builder"]["status"], "failed")
        self.assertEqual(by_role["builder"]["exit_code"], 9)
        self.assertIn("builder exploded", by_role["builder"]["stderr_tail"])

    def test_timeout_terminates_the_agent_and_is_recorded(self) -> None:
        self._executable(
            "codex",
            """
            #!/bin/sh
            sleep 20
            """,
        )
        result = self._run("q", FLEET_FUSION_TIMEOUT="1")

        self.assertEqual(result.returncode, 3, result.stderr)
        summary = self._summary()
        by_role = {item["role"]: item for item in summary["agents"]}
        self.assertEqual(by_role["builder"]["status"], "timeout")
        self.assertLess(by_role["builder"]["latency_seconds"], 10)

    def test_empty_question_fails_before_spawning_anything(self) -> None:
        result = self._run("   ")

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.out.exists())

    def test_spawned_agents_never_see_anthropic_api_key(self) -> None:
        self._executable(
            "claude",
            """
            #!/bin/sh
            printf '%s' "${ANTHROPIC_API_KEY:-ABSENT}" > "$FLEET_TEST_DIR/keycheck"
            printf 'ok\\n'
            """,
        )
        result = self._run("q", ANTHROPIC_API_KEY="leaked-key")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.tmp / "keycheck").read_text(encoding="utf-8"), "ABSENT"
        )


FUSION_TEMPLATE = ROOT / "scripts" / "fusion" / "prompts" / "fusion.md"


class FusionTemplateTests(unittest.TestCase):
    def test_guardrails_and_self_audit_map_one_to_one(self) -> None:
        text = FUSION_TEMPLATE.read_text(encoding="utf-8")
        import re

        guardrails = re.findall(r"^G(\d)\.", text, flags=re.MULTILINE)
        audits = re.findall(r"^A(\d) \(G(\d)\)", text, flags=re.MULTILINE)
        self.assertEqual(guardrails, [str(i) for i in range(1, 10)])
        self.assertEqual([a for a, _ in audits], [str(i) for i in range(1, 10)])
        for audit_index, guardrail_index in audits:
            self.assertEqual(audit_index, guardrail_index)

    def test_template_declares_every_variable_and_contract_section(self) -> None:
        text = FUSION_TEMPLATE.read_text(encoding="utf-8")
        import re

        for variable in (
            "{{QUESTION}}",
            "{{FUSION_INSTRUCTION}}",
            "{{ARCHITECT_MODEL}}",
            "{{BUILDER_MODEL}}",
            "{{BOUNDARY}}",
            "{{ARCHITECT_CONTENT}}",
            "{{BUILDER_CONTENT}}",
        ):
            self.assertIn(variable, text)
        for heading in ("# Fused Answer", "# Consensus & Divergence", "# Discarded"):
            self.assertRegex(text, rf"(?m)^{re.escape(heading)}$")
        for section in (
            "## Supported consensus",
            "## Genuine divergence",
            "## Uncertainty",
            "popularity-trap",
            "UNTRUSTED DATA",
        ):
            self.assertIn(section, text)


if __name__ == "__main__":
    unittest.main()
