from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUDGET = ROOT / "scripts" / "fleet_budget.py"
LEDGER = ROOT / "scripts" / "fleet_ledger.py"


class FleetLedgerTokenTests(unittest.TestCase):
    def test_ledger_records_token_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.jsonl"
            result = subprocess.run(
                [
                    "python3", str(LEDGER), str(ledger),
                    "--run-id", "r1", "--feature", "f", "--instance", "i",
                    "--role", "triage", "--phase", "RECON", "--status", "succeeded",
                    "--task-sha256", "0" * 64, "--exit-code", "0",
                    "--prompt-tokens", "42", "--completion-tokens", "7",
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            event = json.loads(ledger.read_text().splitlines()[0])
            self.assertEqual(event["prompt_tokens"], 42)
            self.assertEqual(event["completion_tokens"], 7)


class FleetBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.ledger = Path(self.tempdir.name) / "ledger.jsonl"

    def seed(self, token_pairs: list[tuple[int, int]]) -> None:
        lines = []
        for index, (prompt_tokens, completion_tokens) in enumerate(token_pairs):
            lines.append(json.dumps({
                "run_id": f"r{index}",
                "status": "succeeded",
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }))
        self.ledger.write_text("\n".join(lines) + "\n")

    def check(self, budget: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(BUDGET), "check", str(self.ledger), str(budget)],
            text=True, capture_output=True, check=False,
        )

    def test_spend_under_warning_threshold_passes(self) -> None:
        self.seed([(100, 50), (200, 100)])
        result = self.check(1000)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("WARNING", result.stderr)

    def test_spend_over_seventy_percent_warns(self) -> None:
        self.seed([(500, 250)])
        result = self.check(1000)
        self.assertEqual(result.returncode, 0)
        self.assertIn("WARNING", result.stderr)

    def test_exhausted_budget_exits_three(self) -> None:
        self.seed([(900, 200)])
        result = self.check(1000)
        self.assertEqual(result.returncode, 3)
        self.assertIn("exhausted", result.stderr)

    def test_missing_ledger_counts_as_zero_spend(self) -> None:
        result = self.check(1000)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
