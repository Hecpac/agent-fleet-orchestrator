"""Model orchestration structure: manifest contract, resolver and code anchors.

Provider-free and offline. Nothing here launches a CLI, Herdr session, provider
or agent; the CLI smoke tests run only this repository's own entry point.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "fusion"))

import fleet_json  # noqa: E402
import fleet_model_orchestration as orchestration  # noqa: E402

MANIFEST = ROOT / "orchestration" / "fleet" / "model-orchestration-v1.json"
SCRIPT = ROOT / "scripts" / "fleet_model_orchestration.py"


def raw_manifest() -> dict:
    return fleet_json.loads(MANIFEST.read_bytes())


def binding(manifest: dict, ident: str) -> dict:
    return next(b for b in manifest["bindings"] if b["id"] == ident)


def by_slot(result: dict) -> dict:
    return {a["slot"]: a for a in result["assignments"]}


class ManifestContractTests(unittest.TestCase):
    def test_manifest_is_valid_hypothesis_without_activation(self):
        manifest = orchestration.load_manifest(MANIFEST)
        self.assertEqual(manifest["status"], "hypothesis")
        self.assertEqual(manifest["activation"], "none")
        self.assertEqual(manifest["catalog"], "docs/model-capability-catalog.md")
        self.assertEqual([t["id"] for t in manifest["teams"]],
                         ["minimal", "standard", "research", "frontier", "open_weight"])

    def test_validation_returns_a_copy(self):
        value = raw_manifest()
        result = orchestration.validate_manifest(value)
        self.assertEqual(result, orchestration.validate_manifest(raw_manifest()))
        self.assertIsNot(result["bindings"][0], value["bindings"][0])

    def assert_rejected(self, mutate, message):
        value = raw_manifest()
        mutate(value)
        with self.assertRaisesRegex(ValueError, message):
            orchestration.validate_manifest(value)

    def test_rejects_unknown_fields(self):
        self.assert_rejected(lambda v: v.update(extra=True), "fields differ")
        self.assert_rejected(lambda v: v["bindings"][0].update(score=99), "fields differ")
        self.assert_rejected(lambda v: v["slots"][0].update(threshold=0.5), "fields differ")

    def test_rejects_activation_and_status_changes(self):
        self.assert_rejected(lambda v: v.update(activation="router"), "no activation")
        self.assert_rejected(lambda v: v.update(status="decided"), "no activation")

    def test_rejects_duplicate_ids(self):
        self.assert_rejected(lambda v: v["bindings"].append(copy.deepcopy(v["bindings"][0])), "duplicate binding")
        self.assert_rejected(lambda v: v["slots"].append(copy.deepcopy(v["slots"][0])), "duplicate slot")
        self.assert_rejected(lambda v: v["teams"].append(copy.deepcopy(v["teams"][0])), "duplicate team")

    def test_blocked_lane_requires_blockers_and_admitted_lane_forbids_them(self):
        def unblock(v):
            del binding(v, "codex-gpt-6.1-sol-xhigh")["blockers"]["herdr"]
        self.assert_rejected(unblock, "blocked without blockers")

        def contradict(v):
            binding(v, "codex-gpt-6-astra-high")["blockers"]["herdr"] = ["stale"]
        self.assert_rejected(contradict, "admitted but lists blockers")

    def test_admission_must_respect_each_lane_filter(self):
        def admit(ident, lane):
            def mutate(v):
                row = binding(v, ident)
                row["lanes"][lane] = "admitted"
                row["blockers"].pop(lane, None)
            return mutate
        self.assert_rejected(admit("claude-opus-5-5-high", "herdr"), "outside lane herdr")
        self.assert_rejected(admit("codex-gpt-6.1-sol-xhigh", "herdr"), "Herdr admits only")
        self.assert_rejected(admit("codex-gpt-6-astra-max", "herdr"), "Herdr admits only")
        self.assert_rejected(admit("codex-gpt-6-astra-high", "harness_mini"), "outside lane harness_mini")
        self.assert_rejected(admit("claude-zai-glm-5.3-max", "fusion"), "native provider")
        self.assert_rejected(admit("opencode-glm-5.3-max", "fusion"), "outside lane fusion")

    def test_slots_and_teams_reference_known_ids_with_one_writer(self):
        self.assert_rejected(lambda v: v["slots"][0]["candidates"].append("missing"), "unknown binding")
        self.assert_rejected(lambda v: v["teams"][0]["slots"].append("missing"), "unknown slot")
        self.assert_rejected(lambda v: v["teams"][1]["slots"].append("writer_frontier"), "exactly one writer")
        self.assert_rejected(lambda v: v["teams"][1].update(slots=["lead", "reviewer"]), "exactly one writer")

        def diverse_writer(v):
            next(s for s in v["slots"] if s["id"] == "writer_daily")["diversity"] = "preferred"
        self.assert_rejected(diverse_writer, "diversity reference")

    def test_rejects_wrong_types(self):
        self.assert_rejected(lambda v: v["bindings"][0].update(context_tokens=True), "positive integer")
        self.assert_rejected(lambda v: v["bindings"][0].update(price_usd_per_mtok={"input": -1, "output": 1}),
                             "non-negative")
        self.assert_rejected(lambda v: v["bindings"][0].update(effort="ultra"), "unsupported value")

    def test_strict_json_rejects_duplicate_keys_and_non_finite_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text('{"schema_version": "a", "schema_version": "b"}')
            with self.assertRaises(ValueError):
                orchestration.load_manifest(path)
            path.write_text('{"x": NaN}')
            with self.assertRaises(ValueError):
                orchestration.load_manifest(path)
            with self.assertRaisesRegex(ValueError, "cannot read"):
                orchestration.load_manifest(Path(tmp) / "absent.json")


class ResolverTests(unittest.TestCase):
    def resolve(self, lane, team, manifest=None):
        return orchestration.resolve(manifest or raw_manifest(), lane=lane, team=team)

    def test_herdr_resolves_to_the_admitted_codex_baseline(self):
        result = self.resolve("herdr", "standard")
        slots = by_slot(result)
        self.assertTrue(result["complete"])
        self.assertEqual(slots["lead"]["binding"], "codex-gpt-6-astra-high")
        self.assertEqual([r["binding"] for r in slots["lead"]["rejected"]], ["claude-opus-5-5-high"])
        self.assertEqual(slots["writer_daily"]["binding"], "codex-gpt-5.6-sol-high")
        self.assertEqual(slots["writer_daily"]["status"], "superseded")
        self.assertEqual([r["binding"] for r in slots["writer_daily"]["rejected"]],
                         ["codex-gpt-6.1-sol-xhigh", "claude-sonnet-5-5-xhigh"])
        self.assertEqual(slots["reviewer"]["binding"], "codex-gpt-6-astra-high")
        self.assertEqual(slots["reviewer"]["warnings"],
                         ["no admitted candidate outside the writer family openai"])

    def test_herdr_frontier_has_no_admissible_writer(self):
        result = self.resolve("herdr", "frontier")
        writer = by_slot(result)["writer_frontier"]
        self.assertFalse(result["complete"])
        self.assertIsNone(writer["binding"])
        self.assertEqual(len(writer["rejected"]), 3)
        self.assertTrue(all(r["reasons"] for r in writer["rejected"]))
        self.assertIn("writer unresolved; family diversity not evaluated", by_slot(result)["reviewer"]["warnings"])

    def test_fusion_frontier_prefers_a_distinct_family_reviewer(self):
        result = self.resolve("fusion", "frontier")
        slots = by_slot(result)
        self.assertTrue(result["complete"])
        self.assertEqual(slots["writer_frontier"]["binding"], "claude-sonnet-5-5-max")
        self.assertEqual(result["writer_family"], "anthropic")
        self.assertEqual(slots["reviewer"]["binding"], "codex-gpt-6-astra-high")
        same_family = slots["reviewer"]["rejected"][0]
        self.assertEqual(same_family["binding"], "claude-opus-5-5-high")
        self.assertIn("same family as the writer (anthropic)", same_family["reasons"][0])

    def test_required_diversity_leaves_the_slot_unresolved(self):
        manifest = raw_manifest()
        next(s for s in manifest["slots"] if s["id"] == "reviewer")["diversity"] = "required"
        result = self.resolve("herdr", "standard", manifest)
        reviewer = by_slot(result)["reviewer"]
        self.assertFalse(result["complete"])
        self.assertIsNone(reviewer["binding"])
        self.assertIn("distinct family required", reviewer["rejected"][-1]["reasons"][0])

    def test_open_weight_team_runs_only_in_the_mini_harness(self):
        self.assertTrue(self.resolve("harness_mini", "open_weight")["complete"])
        self.assertFalse(self.resolve("herdr", "open_weight")["complete"])

    def test_every_lane_and_team_resolves_deterministically(self):
        manifest = orchestration.load_manifest(MANIFEST)
        for lane in orchestration.LANES:
            for team in manifest["teams"]:
                first = self.resolve(lane, team["id"])
                self.assertEqual(first, self.resolve(lane, team["id"]))
                self.assertEqual([a["slot"] for a in first["assignments"]], team["slots"])

    def test_unknown_lane_or_team_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown lane"):
            self.resolve("cmux", "standard")
        with self.assertRaisesRegex(ValueError, "unknown team"):
            self.resolve("herdr", "everyone")


class CodeAnchorTests(unittest.TestCase):
    """Lane constants mirror the modules that enforce them."""

    def test_herdr_models_match_the_owner_evidence_adapter(self):
        import fleet_herdr_owner_contract as contracts
        import fleet_herdr_owner_runtime as runtime
        for model in orchestration.HERDR_MODELS:
            runtime.validate_binding({"cli": "codex", "cli_version": "0.154.0", "provider": "openai",
                                      "model": model, "effort": orchestration.HERDR_EFFORT})
        for model in ("gpt-6.1-sol", "gpt-6-luna", "claude-opus-5-5"):
            with self.assertRaises(contracts.ContractError):
                runtime.validate_binding({"cli": "codex", "cli_version": "0.154.0", "provider": "openai",
                                          "model": model, "effort": "high"})

    def test_herdr_profiles_use_only_admitted_models_at_high_effort(self):
        import fleet_herdr_profile as profiles
        router = fleet_json.loads((ROOT / "orchestration" / "router.yaml").read_bytes())
        for profile in profiles.BY_PRESET.values():
            for _, role_type, model, _, _ in profile.members:
                self.assertIn(model, orchestration.HERDR_MODELS)
                command = router["roles"][role_type]["command"]
                self.assertEqual(command, ["codex", "--model", model, "-c",
                                           f'model_reasoning_effort="{orchestration.HERDR_EFFORT}"'])

    def test_mini_harness_model_matches_the_budget_policy(self):
        import fleet_harness_budget as budget
        policy = budget.contract(cycle_id=str(uuid.uuid4()), deadline_at=time.time() + 60)["request_policy"]
        self.assertEqual(policy["model"], orchestration.MINI_MODEL)

    def test_legacy_admitted_bindings_exist_in_the_router(self):
        router = fleet_json.loads((ROOT / "orchestration" / "router.yaml").read_bytes())["roles"]
        for row in orchestration.load_manifest(MANIFEST)["bindings"]:
            if row["lanes"]["legacy_router"] != "admitted":
                continue
            matches = [role for role in router.values() if role.get("model") == row["model"]
                       and ((role.get("command") or [None])[0] == row["cli"]
                            or (row["cli"] == "ollama" and role.get("provider") == "ollama"))]
            self.assertTrue(matches, row["id"])

    def test_kimi_legacy_admission_matches_the_bridge_protocol(self):
        import kimi_hook_bridge
        row = binding(raw_manifest(), "kimi-k3")
        fixture = ROOT / "tests" / "fixtures" / "kimi" / "wire-1.5-kimi-code-2.1.1.jsonl"
        protocol = fleet_json.loads(fixture.read_bytes().splitlines()[0])["protocol_version"]
        self.assertEqual(row["lanes"]["legacy_router"], "admitted")
        self.assertIn(protocol, kimi_hook_bridge.SUPPORTED_WIRE_PROTOCOLS)

    def test_fusion_tiers_launch_only_native_provider_clis(self):
        import fusion_harness
        for tier in fusion_harness.TIERS.values():
            for role in tier.values():
                self.assertIn(role["argv"][0], orchestration.FUSION_NATIVE_PROVIDER)

    def test_every_model_is_described_in_the_catalog(self):
        catalog = (ROOT / "docs" / "model-capability-catalog.md").read_text()
        for row in orchestration.load_manifest(MANIFEST)["bindings"]:
            self.assertIn(row["model"], catalog, row["id"])


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        completed = subprocess.run([sys.executable, "-B", str(SCRIPT), *args],
                                   capture_output=True, text=True, timeout=60)
        return completed.returncode, json.loads(completed.stdout)

    def test_validate_and_resolve(self):
        code, payload = self.run_cli("validate", str(MANIFEST))
        self.assertEqual((code, payload["valid"], payload["bindings"]), (0, True, 16))
        code, payload = self.run_cli("resolve", str(MANIFEST), "--lane", "fusion", "--team", "standard")
        self.assertEqual(code, 0)
        self.assertEqual(by_slot(payload)["lead"]["binding"], "claude-opus-5-5-high")

    def test_invalid_usage_and_manifest_exit_one(self):
        code, payload = self.run_cli("validate")
        self.assertEqual((code, payload["valid"]), (1, False))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{}")
            code, payload = self.run_cli("validate", str(path))
            self.assertEqual((code, payload["valid"]), (1, False))
            code, payload = self.run_cli("resolve", str(MANIFEST), "--lane", "nowhere", "--team", "standard")
            self.assertEqual((code, payload["valid"]), (1, False))


if __name__ == "__main__":
    unittest.main()
