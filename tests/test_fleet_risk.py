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
            repository_root="/tmp/repository",
        )
        self.assertEqual(local["level"], "low")
        self.assertFalse(local["requires_confirmation"])
        medium = risk.assess(
            workflow_minimum="medium",
            objective="inspect documentation",
            target="/tmp/repository",
            repository_root="/tmp/repository",
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
                    workflow_minimum="low",
                    objective=phrase,
                    target="/tmp/repository",
                    repository_root="/tmp/repository",
                )
                self.assertEqual(result["level"], "high")
                self.assertIn(category, result["categories"])
                self.assertTrue(result["requires_confirmation"])

    def test_spanish_and_accented_objectives_are_classified(self) -> None:
        result = risk.assess(
            workflow_minimum="low",
            objective=(
                "Borra la base de datos de producción y envía un correo a clientes"
            ),
            target="/tmp/repository",
            repository_root="/tmp/repository",
        )
        self.assertEqual(result["level"], "high")
        self.assertTrue(
            {"production", "destructive", "external_side_effect"}
            <= set(result["categories"])
        )

    def test_indirect_destructive_wording_is_classified(self) -> None:
        result = risk.assess(
            workflow_minimum="low",
            objective="Remove all records from the live database and notify customers",
            target="/tmp/repository",
            repository_root="/tmp/repository",
        )
        self.assertEqual(result["level"], "high")
        self.assertTrue(
            {"production", "destructive", "external_side_effect"}
            <= set(result["categories"])
        )

    def test_benign_spanish_repository_work_stays_low(self) -> None:
        result = risk.assess(
            workflow_minimum="low",
            objective="Refactoriza el analizador y ejecuta las pruebas unitarias",
            target="/tmp/repository",
            repository_root="/tmp/repository",
        )
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["categories"], ["repository_local"])

    def test_common_spanish_effect_imperatives_are_classified(self) -> None:
        cases = (
            ("Despliega el servicio", "external_side_effect"),
            ("Publica una nueva versión", "external_side_effect"),
            ("Cobra el pago pendiente", "money"),
            ("Paga la factura", "money"),
        )
        for objective, category in cases:
            with self.subTest(objective=objective):
                result = risk.assess(
                    workflow_minimum="low",
                    objective=objective,
                    target="/tmp/repository",
                    repository_root="/tmp/repository",
                )
                self.assertEqual(result["level"], "high")
                self.assertIn(category, result["categories"])

    def test_override_is_floor_and_risk_never_decreases(self) -> None:
        result = risk.assess(
            workflow_minimum="low",
            objective="inspect files",
            target="/tmp/repository",
            repository_root="/tmp/repository",
            override="unknown",
        )
        self.assertEqual(result["level"], "unknown")
        with self.assertRaisesRegex(risk.RiskError, "cannot decrease"):
            risk.escalate("high", "medium")

    def test_absolute_target_outside_authorized_repository_is_external(self) -> None:
        inside = risk.classify_categories(
            "inspect files", "/tmp/repository/src", "/tmp/repository"
        )
        outside = risk.classify_categories(
            "inspect files", "/etc/hosts", "/tmp/repository"
        )
        self.assertIn("repository_local", inside)
        self.assertNotIn("external_side_effect", inside)
        self.assertIn("external_side_effect", outside)
        self.assertNotIn("repository_local", outside)


if __name__ == "__main__":
    unittest.main()
