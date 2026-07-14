from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fleet_risk", ROOT / "scripts" / "fleet-risk.py")
assert SPEC and SPEC.loader
risk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(risk)


class FleetRiskTests(unittest.TestCase):
    def test_repository_local_is_low_and_workflow_floor_wins(self) -> None:
        local = risk.assess(
            workflow_minimum="low",
            objective="refactor the parser and run tests",
            target="/tmp/repository",
        )
        self.assertEqual(local["level"], "low")
        self.assertFalse(local["requires_confirmation"])
        medium = risk.assess(
            workflow_minimum="medium",
            objective="inspect documentation",
            target="/tmp/repository",
        )
        self.assertEqual(medium["level"], "medium")

    def test_high_categories_pause_before_effect(self) -> None:
        for phrase, category in (
            ("deploy this to production", "production"),
            ("refund the customer payment", "money"),
            ("rotate the API key", "credentials"),
            ("delete the customer data", "destructive"),
            ("process PII", "private_data"),
            ("prepare the HIPAA archive", "regulated"),
        ):
            with self.subTest(category=category):
                result = risk.assess(
                    workflow_minimum="low", objective=phrase, target="/tmp/repository"
                )
                self.assertEqual(result["level"], "high")
                self.assertIn(category, result["categories"])
                self.assertTrue(result["requires_confirmation"])

    def test_override_is_floor_and_risk_never_decreases(self) -> None:
        result = risk.assess(
            workflow_minimum="low",
            objective="inspect files",
            target="/tmp/repository",
            override="unknown",
        )
        self.assertEqual(result["level"], "unknown")
        with self.assertRaisesRegex(risk.RiskError, "cannot decrease"):
            risk.escalate("high", "medium")


if __name__ == "__main__":
    unittest.main()
