from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run-interactive-agent.sh"


class InteractiveAgentEnvironmentTests(unittest.TestCase):
    def run_role(self, role: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CODEX_HOME"] = "/tmp/controller-codex-home"
        return subprocess.run(
            [
                "bash",
                str(RUNNER),
                role,
                "advisory",
                "-",
                "/bin/sh",
                "-c",
                'printf "%s" "${CODEX_HOME-unset}"',
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def test_codex_roles_use_default_codex_home(self) -> None:
        for role in ("codex", "codex_candidate"):
            with self.subTest(role=role):
                result = self.run_role(role)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "unset")

    def test_non_codex_role_preserves_codex_home(self) -> None:
        result = self.run_role("claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "/tmp/controller-codex-home")


if __name__ == "__main__":
    unittest.main()
