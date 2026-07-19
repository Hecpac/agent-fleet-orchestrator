from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "fleet_codex_home.py"


class FleetCodexHomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.controller = self.root / "controller-codex"
        self.isolated = self.root / "fleet-home"
        self.controller.mkdir(mode=0o700)
        self.isolated.mkdir(mode=0o700)
        self.auth = self.controller / "auth.json"
        self.auth.write_text(
            json.dumps({"OPENAI_API_KEY": "synthetic-placeholder"}) + "\n",
            encoding="utf-8",
        )
        self.auth.chmod(0o600)

    def run_helper(self, action: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(HELPER),
                action,
                str(self.controller),
                str(self.isolated),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_provisions_exact_symlink_and_secret_free_policy(self) -> None:
        result = self.run_helper("provision")
        self.assertEqual(result.returncode, 0, result.stderr)
        codex_home = self.isolated / ".codex"
        binding = codex_home / "auth.json"
        self.assertTrue(binding.is_symlink())
        self.assertEqual(os.readlink(binding), str(self.auth.resolve()))
        self.assertEqual(self.auth.read_text(encoding="utf-8").strip(), '{"OPENAI_API_KEY": "synthetic-placeholder"}')
        config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["cli_auth_credentials_store"], "file")
        self.assertEqual(config["history"]["persistence"], "none")
        self.assertEqual(config["shell_environment_policy"]["inherit"], "all")
        excluded = set(config["shell_environment_policy"]["exclude"])
        self.assertIn("CODEX_HOME", excluded)
        self.assertIn("CODEX_ACCESS_TOKEN", excluded)
        self.assertNotIn("synthetic-placeholder", (codex_home / "config.toml").read_text())
        self.assertEqual((codex_home.stat().st_mode & 0o777), 0o700)
        self.assertEqual((codex_home / "config.toml").stat().st_mode & 0o777, 0o600)
        verified = self.run_helper("verify")
        self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_rejects_source_symlink_without_mutating_target(self) -> None:
        target = self.root / "outside-auth.json"
        target.write_text("outside-intact\n", encoding="utf-8")
        target.chmod(0o600)
        self.auth.unlink()
        self.auth.symlink_to(target)
        result = self.run_helper("provision")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "outside-intact\n")
        self.assertFalse((self.isolated / ".codex" / "auth.json").exists())

    def test_rejects_mode_and_hardlink_drift_without_reading_or_mutating(self) -> None:
        self.auth.chmod(0o644)
        mode_result = self.run_helper("provision")
        self.assertEqual(mode_result.returncode, 2)
        self.auth.chmod(0o600)
        hardlink = self.root / "auth-hardlink.json"
        os.link(self.auth, hardlink)
        link_result = self.run_helper("provision")
        self.assertEqual(link_result.returncode, 2)
        self.assertEqual(hardlink.read_text(encoding="utf-8"), self.auth.read_text(encoding="utf-8"))

    def test_verify_fails_closed_on_retarget_or_regular_replacement(self) -> None:
        self.assertEqual(self.run_helper("provision").returncode, 0)
        binding = self.isolated / ".codex" / "auth.json"
        outside = self.root / "outside"
        outside.write_text("intact\n", encoding="utf-8")
        binding.unlink()
        binding.symlink_to(outside)
        retargeted = self.run_helper("verify")
        self.assertEqual(retargeted.returncode, 2)
        self.assertEqual(outside.read_text(encoding="utf-8"), "intact\n")
        binding.unlink()
        binding.write_text("replacement\n", encoding="utf-8")
        binding.chmod(0o600)
        regular = self.run_helper("verify")
        self.assertEqual(regular.returncode, 2)
        self.assertEqual(binding.read_text(encoding="utf-8"), "replacement\n")

    @unittest.skipUnless(shutil.which("codex"), "installed Codex is required")
    def test_installed_codex_file_store_preserves_symlink_on_save(self) -> None:
        self.assertEqual(self.run_helper("provision").returncode, 0)
        codex_home = self.isolated / ".codex"
        binding = codex_home / "auth.json"
        result = subprocess.run(
            [str(shutil.which("codex")), "login", "--with-api-key"],
            input="synthetic-save-key\n",
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(binding.is_symlink())
        saved = json.loads(self.auth.read_text(encoding="utf-8"))
        self.assertEqual(saved["OPENAI_API_KEY"], "synthetic-save-key")
        self.assertEqual(self.run_helper("verify").returncode, 0)


if __name__ == "__main__":
    unittest.main()
