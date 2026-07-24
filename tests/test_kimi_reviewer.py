from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run-kimi-reviewer.sh"
CONTRACT = ROOT / "orchestration" / "prompts" / "kimi_reviewer_agents.md"


class KimiReviewerTests(unittest.TestCase):
    def test_legacy_kimi_cli_artifacts_are_gone(self) -> None:
        self.assertFalse((ROOT / ".kimi").exists(), "legacy agent dir must not return")
        self.assertFalse(
            (ROOT / "scripts" / "kimi_fleet_tools.py").exists(),
            "kimi_cli Python tooling died with kimi-cli; kimi-code is a native binary",
        )

    def test_reviewer_contract_keeps_the_load_bearing_language(self) -> None:
        contract = " ".join(CONTRACT.read_text(encoding="utf-8").split())
        for required in (
            "read-only fleet reviewer",
            "never modify",
            "Do not spawn subagents",
            "one tracked Fleet identity",
            "fleet_control MCP tools",
            "FLEET_RESULT",
            "final visible line",
        ):
            self.assertIn(required, contract)

    def test_runner_pins_model_plan_mode_and_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            capture = root / "args.json"
            fake_kimi = root / "kimi"
            fake_kimi.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['KIMI_TEST_CAPTURE'], 'w', encoding='utf-8') as f:\n"
                "    json.dump({'args': sys.argv[1:], 'cwd': os.getcwd(),\n"
                "               'agents_md': os.environ.get('KIMI_AGENTS_MD', '')}, f)\n",
                encoding="utf-8",
            )
            fake_kimi.chmod(0o700)
            result = subprocess.run(
                ["bash", str(RUNNER), str(target)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": f"{root}:{os.environ['PATH']}",
                    "KIMI_TEST_CAPTURE": str(capture),
                },
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            captured = json.loads(capture.read_text(encoding="utf-8"))
            args = captured["args"]
            self.assertEqual(args[args.index("--model") + 1], "kimi-code/k3")
            self.assertEqual(args.count("--plan"), 1)
            self.assertEqual(Path(captured["cwd"]).resolve(), target.resolve())
            self.assertEqual(
                Path(captured["agents_md"]).resolve(), CONTRACT.resolve()
            )
            for dead_flag in ("--agent-file", "--thinking", "--work-dir"):
                self.assertNotIn(dead_flag, args)

    def test_runner_rejects_unsafe_targets(self) -> None:
        for bad in ("relative/path", "/nonexistent-fleet-target"):
            result = subprocess.run(
                ["bash", str(RUNNER), bad],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 2, bad)


if __name__ == "__main__":
    unittest.main()
