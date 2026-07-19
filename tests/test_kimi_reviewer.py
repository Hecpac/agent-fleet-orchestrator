from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run-kimi-reviewer.sh"
AGENT_DIR = ROOT / ".kimi" / "agents" / "fleet-reviewer"


class KimiReviewerTests(unittest.TestCase):
    def test_agent_specs_expose_only_read_only_tools(self) -> None:
        main = (AGENT_DIR / "agent.yaml").read_text(encoding="utf-8")
        sub = (AGENT_DIR / "sub.yaml").read_text(encoding="utf-8")
        for spec in (main, sub):
            for allowed in ("ReadFile", "ReadMediaFile", "Glob", "Grep"):
                self.assertIn(f'"kimi_cli.tools.file:{allowed}"', spec)
            for forbidden in ("Shell", "WriteFile", "StrReplaceFile"):
                self.assertNotIn(forbidden, spec)
        self.assertIn("kimi_cli.tools.multiagent:Task", main)
        self.assertNotIn("kimi_cli.tools.multiagent:Task", sub)

    def test_runner_pins_model_target_and_read_only_agent(self) -> None:
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
                "    json.dump(sys.argv[1:], f)\n",
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
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(capture.read_text(encoding="utf-8"))

        self.assertEqual(
            args,
            [
                "--work-dir",
                str(target),
                "--model",
                "moonshot-ai/kimi-k3",
                "--thinking",
                "--agent-file",
                str(AGENT_DIR / "agent.yaml"),
            ],
        )
        self.assertNotIn("--yolo", args)
        self.assertNotIn("--afk", args)
        self.assertNotIn("--print", args)

    def test_runner_rejects_relative_target(self) -> None:
        result = subprocess.run(
            ["bash", str(RUNNER), "relative/path"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("absolute, non-symlink directory", result.stderr)


if __name__ == "__main__":
    unittest.main()
