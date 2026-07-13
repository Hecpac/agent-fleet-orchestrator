from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "router_config.py"
SPEC = importlib.util.spec_from_file_location("router_config", MODULE_PATH)
assert SPEC and SPEC.loader
router_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router_config)


class RouterConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = router_config.load_router()

    def write_config(self, config: dict) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        with handle:
            json.dump(config, handle)
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return handle.name

    def test_repository_router_is_valid(self) -> None:
        self.assertEqual(self.config["schema_version"], 3)
        self.assertEqual(self.config["defaults"]["preset"], "small")
        self.assertEqual(
            set(self.config["presets"]),
            {"small", "audit", "frontier_verification", "implementation_review", "hotfix_validated", "research"},
        )

    def test_duplicate_json_key_is_rejected(self) -> None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        with handle:
            handle.write('{"schema_version": 2, "schema_version": 2}')
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        with self.assertRaisesRegex(router_config.RouterError, "duplicate key"):
            router_config.load_router(handle.name)

    def test_unknown_field_is_rejected(self) -> None:
        config = copy.deepcopy(self.config)
        config["roles"]["codex"]["display_rnak"] = 30
        with self.assertRaisesRegex(router_config.RouterError, "unknown fields"):
            router_config.load_router(self.write_config(config))

    def test_default_and_explicit_leads_are_available(self) -> None:
        lead = router_config.select_lead(self.config, run_healthcheck=False)
        self.assertEqual(lead["role_type"], "codex")
        self.assertIn("danger-full-access", lead["command"])
        claude = router_config.select_lead(self.config, "claude", run_healthcheck=False)
        self.assertEqual(claude["role_type"], "claude")

    def test_preset_order_is_capability_first(self) -> None:
        expected = {
            "small": [],
            "audit": ["analysis", "challenge", "verify"],
            "frontier_verification": ["build", "challenge", "verify"],
            "implementation_review": ["build", "verify"],
            "hotfix_validated": ["candidate_codex", "candidate_minimax", "verify"],
            "research": ["triage_scope", "triage_sources", "research", "challenge"],
        }
        for preset, instance_ids in expected.items():
            with self.subTest(preset=preset):
                plan = router_config.build_plan(
                    self.config,
                    preset_name=preset,
                    run_healthcheck=False,
                )
                self.assertEqual([item["instance_id"] for item in plan["instances"]], instance_ids)

    def test_non_writer_codex_is_read_only(self) -> None:
        plan = router_config.build_plan(self.config, preset_name="audit", run_healthcheck=False)
        analysis = next(item for item in plan["instances"] if item["instance_id"] == "analysis")
        self.assertEqual(analysis["phase"], "RECON")
        self.assertIn("read-only", analysis["command"])
        self.assertNotIn("workspace-write", analysis["command"])

    def test_frontier_verification_has_one_writer_and_read_only_reviewers(self) -> None:
        plan = router_config.build_plan(
            self.config, preset_name="frontier_verification", run_healthcheck=False
        )
        self.assertEqual(
            [(item["instance_id"], item["phase"], item["authority"]) for item in plan["instances"]],
            [
                ("build", "BUILD", "write"),
                ("challenge", "CHALLENGE", "advisory"),
                ("verify", "VERIFY", "verification"),
            ],
        )
        self.assertIn("--agent", plan["instances"][1]["command"])
        self.assertIn("plan", plan["instances"][2]["command"])
        self.assertEqual(plan["instances"][1]["hook_source"], "opencode")
        self.assertEqual(plan["instances"][1]["model"], "glm-5.2")

    def test_interactive_roles_declare_supported_source_and_model_identity(self) -> None:
        interactive = {
            name: role
            for name, role in self.config["roles"].items()
            if role["runner"] == "interactive"
        }
        self.assertTrue(all(role.get("hook_source") for role in interactive.values()))
        self.assertTrue(all(role.get("model") for role in interactive.values()))
        self.assertEqual(
            {role["hook_source"] for role in interactive.values()},
            {"codex", "claude", "opencode"},
        )
        for name in ("glm", "minimax", "minimax_candidate"):
            with self.subTest(role=name):
                self.assertEqual(interactive[name]["hook_source"], "opencode")
                self.assertTrue(interactive[name].get("model"))

    def test_codex_and_claude_model_must_match_command(self) -> None:
        for role_name in ("codex", "codex_candidate", "claude", "claude_reviewer"):
            with self.subTest(role=role_name):
                config = copy.deepcopy(self.config)
                config["roles"][role_name]["model"] = "wrong-model"
                with self.assertRaisesRegex(
                    router_config.RouterError, "must match command --model"
                ):
                    router_config.load_router(self.write_config(config))

    def test_unsupported_interactive_hook_source_is_rejected(self) -> None:
        config = copy.deepcopy(self.config)
        config["roles"]["codex"]["hook_source"] = "future"
        with self.assertRaisesRegex(router_config.RouterError, "not supported"):
            router_config.load_router(self.write_config(config))

    def test_interactive_role_without_hook_source_is_rejected(self) -> None:
        config = copy.deepcopy(self.config)
        del config["roles"]["minimax"]["hook_source"]
        with self.assertRaisesRegex(router_config.RouterError, "hook_source"):
            router_config.load_router(self.write_config(config))

    def test_manifest_identity_fields_reject_control_characters(self) -> None:
        for field, value in (
            ("hook_source", "opencode\x1finjected"),
            ("model", "MiniMax-M3\ninjected=value"),
            ("provider", "minimax\rmalformed"),
        ):
            with self.subTest(field=field):
                config = copy.deepcopy(self.config)
                config["roles"]["minimax"][field] = value
                with self.assertRaisesRegex(
                    router_config.RouterError, "forbidden control character"
                ):
                    router_config.load_router(self.write_config(config))

    def test_opencode_provider_model_must_match_command(self) -> None:
        config = copy.deepcopy(self.config)
        config["roles"]["minimax"]["model"] = "DifferentModel"
        with self.assertRaisesRegex(router_config.RouterError, "must match command -m"):
            router_config.load_router(self.write_config(config))

        config = copy.deepcopy(self.config)
        del config["roles"]["minimax"]["model"]
        with self.assertRaisesRegex(router_config.RouterError, "non-empty string"):
            router_config.load_router(self.write_config(config))

    def test_non_build_phases_cannot_write(self) -> None:
        config = copy.deepcopy(self.config)
        config["presets"]["invalid_verify_writer"] = {
            "description": "invalid",
            "include_lead": True,
            "instances": [
                {"instance_id": "verify", "role_type": "codex", "phase": "VERIFY"}
            ],
        }
        with self.assertRaisesRegex(router_config.RouterError, "cannot have write authority"):
            router_config.load_router(self.write_config(config))

    def test_default_race_candidates_share_build_phase_and_are_read_only(self) -> None:
        roles = self.config["defaults"]["race_roles"]
        plan = router_config.build_plan(self.config, instance_specs=roles, run_healthcheck=False)
        self.assertEqual({item["phase"] for item in plan["instances"]}, {"BUILD"})
        self.assertEqual({item["authority"] for item in plan["instances"]}, {"advisory"})
        for item in plan["instances"]:
            self.assertNotIn("workspace-write", item["command"])

    def test_duplicate_role_types_with_unique_instance_ids_are_allowed(self) -> None:
        plan = router_config.build_plan(
            self.config,
            instance_specs=["triage_scope=triage", "triage_sources=triage"],
            run_healthcheck=False,
        )
        self.assertEqual(
            [item["instance_id"] for item in plan["instances"]],
            ["triage_scope", "triage_sources"],
        )
        self.assertEqual({item["role_type"] for item in plan["instances"]}, {"triage"})

    def test_duplicate_instance_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(router_config.RouterError, "duplicate instance_id"):
            router_config.build_plan(
                self.config,
                instance_specs=["triage", "triage"],
                run_healthcheck=False,
            )

    def test_reserved_and_unsafe_instance_ids_are_rejected(self) -> None:
        for spec in ("lead=triage", "bad/id=triage", "UPPER=triage"):
            with self.subTest(spec=spec):
                with self.assertRaises(router_config.RouterError):
                    router_config.build_plan(
                        self.config,
                        instance_specs=[spec],
                        run_healthcheck=False,
                    )

    def test_optional_local_token_budget_is_validated(self) -> None:
        config = copy.deepcopy(self.config)
        config["limits"]["local_token_budget_per_feature"] = 200_000
        loaded = router_config.load_router(self.write_config(config))
        self.assertEqual(loaded["limits"]["local_token_budget_per_feature"], 200_000)
        for bad in (0, -5, True, "many"):
            with self.subTest(bad=bad):
                config["limits"]["local_token_budget_per_feature"] = bad
                with self.assertRaisesRegex(
                    router_config.RouterError, "local_token_budget_per_feature"
                ):
                    router_config.load_router(self.write_config(config))

    def test_two_writers_in_one_preset_are_rejected(self) -> None:
        config = copy.deepcopy(self.config)
        config["presets"]["two_writers"] = {
            "description": "invalid",
            "include_lead": True,
            "instances": [
                {"instance_id": "writer_a", "role_type": "codex"},
                {"instance_id": "writer_b", "role_type": "codex"},
            ],
        }
        with self.assertRaisesRegex(router_config.RouterError, "at most one writer"):
            router_config.load_router(self.write_config(config))

    def test_custom_input_is_sorted_by_rank_then_instance(self) -> None:
        plan = router_config.build_plan(
            self.config,
            instance_specs=["verify=reviewer", "challenge=minimax", "build=codex", "recon=triage"],
            run_healthcheck=False,
        )
        self.assertEqual(
            [item["instance_id"] for item in plan["instances"]],
            ["recon", "build", "challenge", "verify"],
        )

    def test_verify_layout_checks_titles_not_creation_order(self) -> None:
        tree = {
            "windows": [
                {
                    "workspaces": [
                        {
                            "panes": [
                                {"surfaces": [{"title": "lead"}]},
                                {"surfaces": [{"title": "build"}]},
                                {"surfaces": [{"title": "verify"}]},
                            ]
                        }
                    ]
                }
            ]
        }
        self.assertEqual(router_config.verify_layout(["lead", "build", "verify"], tree), ["lead", "build", "verify"])
        with self.assertRaisesRegex(router_config.RouterError, "layout mismatch"):
            router_config.verify_layout(["lead", "verify", "build"], tree)


if __name__ == "__main__":
    unittest.main()
