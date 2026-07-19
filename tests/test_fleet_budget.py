from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BUDGET = ROOT / "scripts" / "fleet_budget.py"
LEDGER = ROOT / "scripts" / "fleet_ledger.py"
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fleet_budget  # noqa: E402
import fleet_usage  # noqa: E402


class FleetLedgerTokenTests(unittest.TestCase):
    def test_ledger_records_token_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.jsonl"
            result = subprocess.run(
                [
                    "python3",
                    str(LEDGER),
                    str(ledger),
                    "--run-id",
                    "r1",
                    "--feature",
                    "f",
                    "--instance",
                    "i",
                    "--role",
                    "triage",
                    "--phase",
                    "RECON",
                    "--status",
                    "succeeded",
                    "--task-sha256",
                    "0" * 64,
                    "--exit-code",
                    "0",
                    "--prompt-tokens",
                    "42",
                    "--completion-tokens",
                    "7",
                ],
                text=True,
                capture_output=True,
                check=False,
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
            lines.append(
                json.dumps(
                    {
                        "run_id": f"r{index}",
                        "status": "succeeded",
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                    }
                )
            )
        self.ledger.write_text("\n".join(lines) + "\n")
        self.ledger.chmod(0o600)

    def check(self, budget: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(BUDGET), "check", str(self.ledger), str(budget)],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_spend_under_warning_threshold_passes(self) -> None:
        self.seed([(100, 50), (200, 100)])
        result = self.check(1000)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("local token spend: 450/1000", result.stderr)
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

    def test_missing_ledger_is_unknown_and_refuses_dispatch(self) -> None:
        result = self.check(1000)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("local token spend: unknown/1000", result.stderr)
        self.assertIn("usage is unknown", result.stderr)

    def test_present_empty_ledger_is_known_zero_spend(self) -> None:
        self.ledger.write_bytes(b"")
        self.ledger.chmod(0o600)
        result = self.check(1000)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local token spend: 0/1000", result.stderr)

    def test_nonterminal_rows_do_not_invent_closed_usage_receipts(self) -> None:
        for status in ("preparing", "authorized", "dispatched", "running"):
            with self.subTest(status=status):
                self.ledger.write_text(
                    json.dumps(
                        {"provider": "ollama", "run_id": "r1", "status": status}
                    )
                    + "\n",
                    encoding="utf-8",
                )
                self.ledger.chmod(0o600)
                result = self.check(1000)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("local token spend: 0/1000", result.stderr)

    def test_unknown_status_fails_closed_instead_of_reporting_zero(self) -> None:
        for status in ("suceeded", "RUNNING", ""):
            with self.subTest(status=status):
                self.ledger.write_text(
                    json.dumps(
                        {
                            "provider": "ollama",
                            "run_id": "r1",
                            "status": status,
                            "prompt_tokens": 900,
                            "completion_tokens": 200,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                self.ledger.chmod(0o600)
                result = self.check(1000)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unknown status", result.stderr)
                self.assertNotIn("local token spend: 0/1000", result.stderr)

    def test_nonterminal_status_cannot_hide_token_counts(self) -> None:
        self.ledger.write_text(
            json.dumps(
                {
                    "provider": "ollama",
                    "run_id": "r1",
                    "status": "running",
                    "prompt_tokens": 900,
                    "completion_tokens": 200,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.ledger.chmod(0o600)
        result = self.check(1000)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("nonterminal ledger status", result.stderr)
        self.assertNotIn("local token spend: 0/1000", result.stderr)

    def test_terminal_row_with_missing_or_partial_counts_is_unknown(self) -> None:
        for label, row in (
            ("missing", {"run_id": "r1", "status": "succeeded"}),
            (
                "partial",
                {"run_id": "r1", "status": "succeeded", "prompt_tokens": 1},
            ),
        ):
            with self.subTest(label=label):
                self.ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
                self.ledger.chmod(0o600)
                result = self.check(1000)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("local token spend: unknown/1000", result.stderr)

    def test_dispatch_not_transferred_is_explicitly_not_incurred(self) -> None:
        self.ledger.write_text(
            json.dumps(
                {
                    "provider": "ollama",
                    "reason": "dispatch_not_transferred",
                    "run_id": "r1",
                    "status": "abandoned",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.ledger.chmod(0o600)
        result = self.check(1000)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local token spend: 0/1000", result.stderr)

    def test_dispatch_not_transferred_rejects_any_token_count(self) -> None:
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            with self.subTest(field=field):
                self.ledger.write_text(
                    json.dumps(
                        {
                            "provider": "ollama",
                            "reason": "dispatch_not_transferred",
                            "run_id": "r1",
                            "status": "abandoned",
                            field: 0,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                self.ledger.chmod(0o600)
                result = self.check(1000)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("cannot include token counts", result.stderr)

    def test_dispatch_not_transferred_requires_abandoned_status(self) -> None:
        for status in ("failed", "running"):
            with self.subTest(status=status):
                self.ledger.write_text(
                    json.dumps(
                        {
                            "provider": "ollama",
                            "reason": "dispatch_not_transferred",
                            "run_id": "r1",
                            "status": status,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                self.ledger.chmod(0o600)
                result = self.check(1000)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("requires abandoned status", result.stderr)

    def test_non_local_provider_cannot_hide_claimed_token_counts(self) -> None:
        self.ledger.write_text(
            json.dumps(
                {
                    "provider": "ollmaa",
                    "run_id": "r1",
                    "status": "succeeded",
                    "prompt_tokens": 900,
                    "completion_tokens": 200,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.ledger.chmod(0o600)
        result = self.check(1000)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("claims local token counts", result.stderr)

    def test_ledger_binding_change_during_read_fails_closed(self) -> None:
        self.ledger.write_bytes(b"")
        self.ledger.chmod(0o600)

        def mutate_during_read(_path: Path) -> list[dict[str, object]]:
            self.ledger.write_text(
                '{"run_id":"late","status":"succeeded"}\n',
                encoding="utf-8",
            )
            self.ledger.chmod(0o600)
            return []

        with (
            mock.patch.object(
                fleet_budget.fleet_ledger,
                "read_records",
                side_effect=mutate_during_read,
            ),
            self.assertRaisesRegex(fleet_usage.UsageError, "changed during"),
        ):
            fleet_budget.usage_receipts(self.ledger)

    def test_negative_boolean_and_inconsistent_counts_are_usage_errors(self) -> None:
        rows = (
            {
                "run_id": "negative",
                "status": "succeeded",
                "prompt_tokens": -1,
                "completion_tokens": 1,
            },
            {
                "run_id": "boolean",
                "status": "succeeded",
                "prompt_tokens": True,
                "completion_tokens": 1,
            },
            {
                "run_id": "inconsistent",
                "status": "succeeded",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 3,
            },
        )
        for row in rows:
            with self.subTest(run_id=row["run_id"]):
                self.ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")
                self.ledger.chmod(0o600)
                result = self.check(1000)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_duplicate_terminal_closure_is_rejected(self) -> None:
        terminal = {
            "run_id": "same-run",
            "status": "succeeded",
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }
        self.ledger.write_text(
            json.dumps(terminal) + "\n" + json.dumps(terminal) + "\n",
            encoding="utf-8",
        )
        self.ledger.chmod(0o600)
        result = self.check(1000)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("duplicate closure", result.stderr)

    def test_symlinked_ledger_fails_closed_without_target_mutation(self) -> None:
        outside = Path(self.tempdir.name) / "outside.jsonl"
        outside.write_text(
            '{"prompt_tokens":999,"completion_tokens":999}\n', encoding="utf-8"
        )
        outside.chmod(0o600)
        self.ledger.symlink_to(outside)
        before = outside.read_bytes()

        result = self.check(1000)

        self.assertEqual(result.returncode, 2)
        self.assertIn("unsafe lifecycle ledger", result.stderr)
        self.assertEqual(outside.read_bytes(), before)

    def test_non_strict_ledger_fails_closed(self) -> None:
        self.ledger.write_text(
            '{"prompt_tokens":1,"prompt_tokens":999}\n', encoding="utf-8"
        )
        self.ledger.chmod(0o600)
        result = self.check(1000)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate ledger key", result.stderr)


if __name__ == "__main__":
    unittest.main()
