"""Regressions from the real FitScan startup failures; no model or provider."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fleet_herdr_startup import codex_startup_blocker, validate_local_socket_path


class StartupTests(unittest.TestCase):
    def test_observed_input_prompt_is_deliverable(self):
        self.assertIsNone(codex_startup_blocker(
            "OpenAI Codex (v0.153.0)\n› Ask Codex to do anything\n"))

    def test_ready_process_with_update_or_trust_dialog_is_not_deliverable(self):
        screens = [
            "Update available! 0.153.0 -> 0.153.4\n› 1. Update now\n2. Skip",
            "Hooks need review\n1 hook is new or changed.\n2. Trust all and continue",
            "Hooks\n1 hook needs review before it can run.\nPress t to trust all",
            "Hooks\nSessionStart 1 1\nPress enter to view hooks; esc to close",
        ]
        for screen in screens:
            with self.subTest(screen=screen):
                self.assertIsNotNone(codex_startup_blocker(screen))
                self.assertIsNotNone(codex_startup_blocker(
                    "Ask Codex to do anything\n" + screen))

    def test_empty_shell_and_unknown_surface_fail_closed(self):
        for screen in ["", "  ", "sh-3.2$", "Working", "unrecognized CLI"]:
            with self.subTest(screen=screen):
                self.assertIsNotNone(codex_startup_blocker(screen))

    def test_client_socket_suffix_can_exceed_limit_when_api_socket_fits(self):
        root = "/" + "a" * 89
        validate_local_socket_path(root + "/herdr.sock")
        with self.assertRaises(ValueError):
            validate_local_socket_path(root + "/herdr-client.sock")

    def test_socket_limit_counts_bytes_and_rejects_relative_and_nul(self):
        for path in ["relative.sock", "/bad\0.sock", "/" + "ñ" * 52]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_local_socket_path(path)


if __name__ == "__main__":
    unittest.main()
