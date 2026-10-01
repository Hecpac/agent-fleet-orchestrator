"""Regressions from the real FitScan startup failures; no model or provider."""
import sys
from pathlib import Path
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fleet_herdr_startup import (FOLDER_ACCESS_BODY, GUARD_0154, GUARD_0159, codex_startup_blocker,
                                 folder_access_notice, validate_local_socket_path)

# Real Codex 0.159.3 surfaces (outputs/cex91_aoh, outputs/cgsta7oq9); only the
# private run path was replaced by /fleet/candidate and the splash art dropped.
CODEX_0159 = Path(__file__).resolve().parent / "fixtures" / "codex-0.159.3"
CWD = "/fleet/candidate"


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
            "Do you trust the contents of this directory?\n› 1. Yes, continue\n2. No, quit",
            "Trusting the directory allows project-local config, hooks, and exec policies to load.",
            "Resuming session…\n› Ask Codex to do anything",
            "Sign in with ChatGPT\n› Ask Codex to do anything",
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

    def test_codex_0159_ready_surface_is_deliverable_only_under_its_guard(self):
        ready = (CODEX_0159 / "ready.txt").read_text()
        self.assertIsNone(codex_startup_blocker(ready, GUARD_0159))
        self.assertEqual(codex_startup_blocker(ready, "codex-9.9-v1"), "unknown Codex startup guard")

    def test_codex_0159_dialogs_block_delivery(self):
        notice = (CODEX_0159 / "folder-access.txt").read_text()
        migration = (CODEX_0159 / "migration.txt").read_text()
        self.assertEqual(codex_startup_blocker(notice, GUARD_0159), "Codex folder access notice")
        self.assertEqual(codex_startup_blocker(migration, GUARD_0159), "Codex model migration prompt")
        trust = "Trust this folder?\n› 1. Trust and continue\n2. Quit\n› Ask Codex to do anything"
        self.assertEqual(codex_startup_blocker(trust, GUARD_0159), "Codex project trust dialog")
        # Without the ready prompt the older guards also fail closed.
        self.assertIsNotNone(codex_startup_blocker(notice, GUARD_0154))

    def test_folder_access_acknowledgment_requires_the_exact_notice(self):
        notice = (CODEX_0159 / "folder-access.txt").read_text()
        self.assertTrue(folder_access_notice(notice, guard=GUARD_0159, cwd=CWD))
        self.assertFalse(folder_access_notice(notice, guard=GUARD_0154, cwd=CWD))
        self.assertFalse(folder_access_notice(notice, guard=GUARD_0159, cwd="/fleet/other"))
        for altered in (
            notice.replace("› 1. Open restricted", "  1. Open restricted").replace("  2. Quit", "› 2. Quit"),
            notice.replace("Open restricted", "Trust and continue"),
            notice.replace("stay disabled", "may load"),
            notice.replace("  enter continue · esc quit", ""),
            notice + "\n› Ask Codex to do anything\n",
            (CODEX_0159 / "migration.txt").read_text(),
            (CODEX_0159 / "ready.txt").read_text(),
        ):
            with self.subTest(altered=altered[:60]):
                self.assertFalse(folder_access_notice(altered, guard=GUARD_0159, cwd=CWD))

    def test_folder_access_matching_survives_narrow_panes_and_home_paths(self):
        body = " ".join(FOLDER_ACCESS_BODY.split())
        wrapped = "\n".join(textwrap.wrap(body, 37))
        cwd = "/Users/someone/projects/a-rather-long-candidate-directory-name"
        screen = ("Folder access\n/Users/someone/projects/a-rather-\nlong-candidate-directory-name\n"
                  + wrapped + "\n› 1. Open restricted\n  2. Quit\n  enter continue · esc quit\n")
        self.assertTrue(folder_access_notice(screen, guard=GUARD_0159, cwd=cwd))
        home_form = screen.replace("/Users/someone/projects/a-rather-\n", "~/projects/a-rather-\n")
        self.assertFalse(folder_access_notice(home_form, guard=GUARD_0159, cwd=cwd))
        self.assertTrue(folder_access_notice(home_form, guard=GUARD_0159, cwd=cwd, home="/Users/someone"))

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
