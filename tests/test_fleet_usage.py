from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fleet_json  # noqa: E402
import fleet_usage  # noqa: E402


PROVIDERS = ("codex", "claude", "opencode", "ollama")


class FleetUsagePolicyTests(unittest.TestCase):
    def test_provider_capability_and_budget_mode_matrix(self) -> None:
        expected = {
            "codex": "none",
            "claude": "none",
            "opencode": "none",
            "ollama": "hard_output_only",
        }
        self.assertEqual(dict(fleet_usage.PROVIDER_CAPABILITIES), expected)
        self.assertEqual(
            fleet_usage.CAPABILITIES,
            {"none", "observed_total", "hard_output_only", "hard_total"},
        )

        for provider in PROVIDERS:
            with self.subTest(provider=provider, mode="soft", budget=0):
                self.assertEqual(
                    fleet_usage.validate_policy(
                        {"budget_mode": "soft", "token_budget": 0},
                        providers=[provider],
                    ),
                    {"budget_mode": "soft", "token_budget": 0},
                )
            with self.subTest(provider=provider, mode="soft", budget=100):
                self.assertEqual(
                    fleet_usage.validate_policy(
                        {"budget_mode": "soft", "token_budget": 100},
                        providers=[provider],
                    ),
                    {"budget_mode": "soft", "token_budget": 100},
                )
            with (
                self.subTest(provider=provider, mode="hard", budget=0),
                self.assertRaisesRegex(fleet_usage.UsageError, "must be positive"),
            ):
                fleet_usage.validate_policy(
                    {"budget_mode": "hard", "token_budget": 0},
                    providers=[provider],
                )
            with (
                self.subTest(provider=provider, mode="hard", budget=100),
                self.assertRaisesRegex(fleet_usage.UsageError, "hard_total"),
            ):
                fleet_usage.validate_policy(
                    {"budget_mode": "hard", "token_budget": 100},
                    providers=[provider],
                )

    def test_hard_policy_needs_complete_capable_provider_set(self) -> None:
        with self.assertRaisesRegex(fleet_usage.UsageError, "complete provider set"):
            fleet_usage.validate_policy({"budget_mode": "hard", "token_budget": 1})
        with self.assertRaisesRegex(
            fleet_usage.UsageError,
            "unsupported providers: claude, codex, ollama, opencode",
        ):
            fleet_usage.validate_policy(
                {"budget_mode": "hard", "token_budget": 1},
                providers=list(reversed(PROVIDERS)),
            )

    def test_policy_is_a_closed_strict_json_contract(self) -> None:
        invalid = (
            None,
            [],
            {},
            {"budget_mode": "soft", "token_budget": 1, "extra": True},
            {"budget_mode": "disabled", "token_budget": 1},
            {"budget_mode": "soft", "token_budget": True},
            {"budget_mode": "soft", "token_budget": -1},
        )
        for value in invalid:
            with (
                self.subTest(value=value),
                self.assertRaises(fleet_usage.UsageError),
            ):
                fleet_usage.validate_policy(value)

        with self.assertRaisesRegex(fleet_usage.UsageError, "JSON array"):
            fleet_usage.validate_policy(
                {"budget_mode": "soft", "token_budget": 1},
                providers=("ollama",),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(
            fleet_usage.UsageError, "unsupported usage provider"
        ):
            fleet_usage.validate_policy(
                {"budget_mode": "soft", "token_budget": 1},
                providers=["future-provider"],
            )

        class StringSubclass(str):
            pass

        with self.assertRaises(fleet_usage.UsageError):
            fleet_usage.validate_policy(
                {
                    StringSubclass("budget_mode"): "soft",
                    StringSubclass("token_budget"): 1,
                }
            )


class FleetUsageReceiptTests(unittest.TestCase):
    def test_observed_receipt_is_consistent_and_canonical(self) -> None:
        value = fleet_usage.receipt(
            "ollama",
            "observed",
            input_tokens=42,
            output_tokens=7,
            total_tokens=49,
        )
        self.assertEqual(
            fleet_usage.canonical_bytes(value),
            b'{"capability":"hard_output_only","input_tokens":42,'
            b'"output_tokens":7,"provider":"ollama","schema_version":1,'
            b'"state":"observed","total_tokens":49}',
        )
        self.assertEqual(fleet_json.loads(fleet_usage.canonical_bytes(value)), value)

    def test_none_capability_cannot_claim_observed_totals(self) -> None:
        for provider in ("codex", "claude", "opencode"):
            with (
                self.subTest(provider=provider),
                self.assertRaisesRegex(fleet_usage.UsageError, "cannot claim observed"),
            ):
                fleet_usage.receipt(
                    provider,
                    "observed",
                    input_tokens=1,
                    output_tokens=1,
                    total_tokens=2,
                )

    def test_not_incurred_and_unknown_have_unambiguous_counts(self) -> None:
        not_incurred = fleet_usage.receipt("codex", "not_incurred")
        self.assertEqual(
            (
                not_incurred["input_tokens"],
                not_incurred["output_tokens"],
                not_incurred["total_tokens"],
            ),
            (0, 0, 0),
        )
        unknown = fleet_usage.receipt("opencode", "unknown")
        self.assertEqual(
            (
                unknown["input_tokens"],
                unknown["output_tokens"],
                unknown["total_tokens"],
            ),
            (None, None, None),
        )

    def test_counts_are_non_negative_exact_integers_and_total_is_consistent(
        self,
    ) -> None:
        cases = (
            {"input_tokens": -1, "output_tokens": 1, "total_tokens": 0},
            {"input_tokens": True, "output_tokens": 1, "total_tokens": 2},
            {"input_tokens": 1, "output_tokens": None, "total_tokens": 1},
            {"input_tokens": 1, "output_tokens": 2, "total_tokens": 4},
        )
        for counts in cases:
            with (
                self.subTest(counts=counts),
                self.assertRaises(fleet_usage.UsageError),
            ):
                fleet_usage.receipt("ollama", "observed", **counts)

        with self.assertRaises(fleet_usage.UsageError):
            fleet_usage.receipt("codex", "not_incurred", total_tokens=1)
        with self.assertRaises(fleet_usage.UsageError):
            fleet_usage.receipt("codex", "not_incurred", total_tokens=False)
        with self.assertRaises(fleet_usage.UsageError):
            fleet_usage.receipt(  # type: ignore[arg-type]
                "codex", "not_incurred", total_tokens=[]
            )
        with self.assertRaises(fleet_usage.UsageError):
            fleet_usage.receipt("codex", "unknown", total_tokens=0)

    def test_receipt_schema_and_capability_are_closed(self) -> None:
        valid = fleet_usage.receipt("ollama", "not_incurred")
        mutations = []
        extra = copy.deepcopy(valid)
        extra["num_predict"] = 768
        mutations.append(extra)
        wrong_schema = copy.deepcopy(valid)
        wrong_schema["schema_version"] = True
        mutations.append(wrong_schema)
        wrong_capability = copy.deepcopy(valid)
        wrong_capability["capability"] = "hard_total"
        mutations.append(wrong_capability)

        class StringSubclass(str):
            pass

        subclass_capability = copy.deepcopy(valid)
        subclass_capability["capability"] = StringSubclass("hard_output_only")
        mutations.append(subclass_capability)
        subclass_keys = {StringSubclass(key): value for key, value in valid.items()}
        mutations.append(subclass_keys)
        for value in mutations:
            with (
                self.subTest(value=value),
                self.assertRaises(fleet_usage.UsageError),
            ):
                fleet_usage.summarize([value])


class FleetUsageAggregationAndAdmissionTests(unittest.TestCase):
    @staticmethod
    def observed(input_tokens: int, output_tokens: int) -> dict[str, object]:
        return fleet_usage.receipt(
            "ollama",
            "observed",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )

    def test_mixed_receipts_aggregate_deterministically(self) -> None:
        receipts = [
            self.observed(10, 5),
            fleet_usage.receipt("codex", "not_incurred"),
            self.observed(20, 7),
        ]
        first = fleet_usage.summarize(receipts)
        second = fleet_usage.summarize(list(reversed(receipts)))
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "observed")
        self.assertEqual(first["providers"], ["codex", "ollama"])
        self.assertEqual(first["receipt_count"], 3)
        self.assertEqual(first["observed_receipts"], 2)
        self.assertEqual(first["not_incurred_receipts"], 1)
        self.assertEqual(first["unknown_receipts"], 0)
        self.assertEqual(first["input_tokens"], 30)
        self.assertEqual(first["output_tokens"], 12)
        self.assertEqual(first["total_tokens"], 42)

    def test_unknown_receipt_keeps_known_subtotal_but_never_claims_total(self) -> None:
        value = fleet_usage.summarize(
            [
                self.observed(10, 5),
                fleet_usage.receipt("claude", "unknown"),
                fleet_usage.receipt("codex", "not_incurred"),
            ]
        )
        self.assertEqual(value["state"], "unknown")
        self.assertEqual(value["unknown_receipts"], 1)
        self.assertEqual(value["observed_total_tokens"], 15)
        self.assertIsNone(value["input_tokens"])
        self.assertIsNone(value["output_tokens"])
        self.assertIsNone(value["total_tokens"])

    def test_missing_source_is_unknown_but_present_empty_source_is_zero(self) -> None:
        missing = fleet_usage.summarize(None)
        self.assertFalse(missing["source_present"])
        self.assertEqual(missing["state"], "unknown")
        self.assertIsNone(missing["total_tokens"])

        empty = fleet_usage.summarize([])
        self.assertTrue(empty["source_present"])
        self.assertEqual(empty["state"], "not_incurred")
        self.assertEqual(empty["total_tokens"], 0)

    def test_soft_zero_is_disabled_for_every_provider(self) -> None:
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                decision = fleet_usage.admit(
                    {"budget_mode": "soft", "token_budget": 0},
                    provider,
                    None,
                )
                self.assertTrue(decision["admitted"])
                self.assertEqual(decision["reason"], "budget_disabled")
                self.assertIsNone(decision["remaining_tokens"])

    def test_soft_positive_blocks_unknown_and_admits_known_empty_usage(self) -> None:
        policy = {"budget_mode": "soft", "token_budget": 100}
        first = fleet_usage.admit(policy, "codex", [])
        self.assertTrue(first["admitted"])
        self.assertEqual(first["reason"], "within_budget")
        self.assertEqual(first["remaining_tokens"], 100)

        for receipts in (
            None,
            [fleet_usage.receipt("codex", "unknown")],
            [self.observed(1, 1), fleet_usage.receipt("opencode", "unknown")],
        ):
            with self.subTest(receipts=receipts):
                blocked = fleet_usage.admit(policy, "ollama", receipts)
                self.assertFalse(blocked["admitted"])
                self.assertEqual(blocked["reason"], "usage_unknown")
                self.assertIsNone(blocked["remaining_tokens"])

    def test_exact_budget_boundary_and_overshoot_are_refused(self) -> None:
        policy = {"budget_mode": "soft", "token_budget": 100}
        below = fleet_usage.admit(policy, "ollama", [self.observed(60, 39)])
        exact = fleet_usage.admit(policy, "ollama", [self.observed(60, 40)])
        over = fleet_usage.admit(policy, "ollama", [self.observed(60, 41)])
        self.assertTrue(below["admitted"])
        self.assertEqual(below["remaining_tokens"], 1)
        self.assertFalse(exact["admitted"])
        self.assertEqual(exact["reason"], "budget_exhausted")
        self.assertEqual(exact["remaining_tokens"], 0)
        self.assertFalse(over["admitted"])
        self.assertEqual(over["remaining_tokens"], 0)

    def test_hard_output_cap_is_never_advertised_as_hard_total(self) -> None:
        with self.assertRaisesRegex(fleet_usage.UsageError, "hard_total"):
            fleet_usage.admit(
                {"budget_mode": "hard", "token_budget": 768},
                "ollama",
                object(),
            )
        self.assertEqual(
            fleet_usage.PROVIDER_CAPABILITIES["ollama"], "hard_output_only"
        )

    def test_disabled_policy_still_rejects_malformed_receipts(self) -> None:
        with self.assertRaises(fleet_usage.UsageError):
            fleet_usage.admit(
                {"budget_mode": "soft", "token_budget": 0},
                "ollama",
                [{"state": "unknown"}],
            )

    def test_json_parser_rejects_ambiguous_receipt_before_api_use(self) -> None:
        raw = (
            b'{"schema_version":1,"provider":"ollama",'
            b'"provider":"codex","capability":"hard_output_only",'
            b'"state":"unknown","input_tokens":null,"output_tokens":null,'
            b'"total_tokens":null}'
        )
        with self.assertRaises(fleet_json.FleetJSONError):
            fleet_json.loads(raw)


if __name__ == "__main__":
    unittest.main()
