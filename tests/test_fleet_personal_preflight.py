"""Diagnostics distinguish advertised tools, runtime readiness and generation."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_personal_preflight as preflight


class PreflightTests(unittest.TestCase):
    def probe(self, *, dirty=False, codex_version="0.154.0", login=True):
        calls = []
        def run(argv):
            calls.append(argv)
            code, out, err = 0, "", ""
            if argv[-1] == "--version":
                out = "herdr 0.9.0" if "herdr" in argv[0] else "codex-cli " + codex_version
            elif argv[1:] == ["login", "status"]:
                code = 0 if login else 1
                err = "Logged in using ChatGPT" if login else "synthetic secret must not escape"
            elif argv[-1] == "--help":
                out = "--sandbox --search --image mcp plugin"
            elif argv[0] == "git":
                out = " M answer.py\n!! cache/\n" if dirty else ""
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(argv, code, out, err)
        with tempfile.TemporaryDirectory() as temporary:
            result = preflight.inspect(Path(temporary), run=run, which=lambda name: "/fake/" + name)
        return result, calls

    def test_readiness_does_not_claim_model_or_tool_execution(self):
        result, calls = self.probe()
        self.assertEqual(result["runtime_preflight"], "PASS")
        self.assertEqual(result["model_access"], "NOT_VERIFIED")
        self.assertEqual(result["generation_requests"], 0)
        self.assertEqual(result["agents_started"], 0)
        self.assertTrue(all(c["session_availability"] == "NOT_VERIFIED" for c in result["capabilities"]))
        self.assertFalse(any("start" in c or "prompt" in c or "exec" in c for c in calls))

    def test_all_observed_blockers_are_reported_without_raw_login_output(self):
        result, _ = self.probe(dirty=True, codex_version="0.155.0", login=False)
        self.assertEqual(set(result["blockers"]), {"codex_incompatible", "personal_chatgpt_login_unavailable",
                                                 "target_requires_exact_snapshot"})
        self.assertNotIn("synthetic secret", str(result))
        self.assertEqual(result["target"]["changed_entries"], 2)


if __name__ == "__main__":
    unittest.main()
