from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
workflow_config = importlib.import_module("workflow_config")
fleet_compiled = importlib.import_module("fleet_compiled")
fleet_manifest = importlib.import_module("fleet_manifest")


class WorkflowConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = workflow_config.load_workflow(
            ROOT / "workflows" / "implementation.yaml"
        )
        self.router = workflow_config.router_config.load_router()

    def test_repository_workflows_compile_against_router(self) -> None:
        expected = {"hotfix", "implementation", "local-worm", "research"}
        paths = sorted((ROOT / "workflows").glob("*.yaml"))
        compiled = [
            workflow_config.compile_path(path)
            for path in paths
            if path.name != "regulated.yaml"
        ]
        self.assertEqual({item["workflow"]["name"] for item in compiled}, expected)
        implementation = next(
            item for item in compiled if item["workflow"]["name"] == "implementation"
        )
        self.assertEqual(implementation["resolved"]["writer_instance"], "builder")
        self.assertEqual(implementation["resolved"]["mode"], "autonomous")
        self.assertEqual(
            implementation["resolved"]["identity_groups"],
            [["builder", "challenger", "verifier"]],
        )
        self.assertEqual(
            implementation["resolved"]["assurance_identity_groups"],
            [["maker", "checker", "challenge", "verify"]],
        )
        local_worm = next(
            item for item in compiled if item["workflow"]["name"] == "local-worm"
        )
        self.assertEqual(
            local_worm["workflow"]["audit"],
            {
                "mode": "worm",
                "trust_scope": "local-development",
                "worm_required_for": [],
            },
        )

        # The repository deliberately keeps the requested regulated hard
        # policy visible, but S0 has no provider capable of enforcing a hard
        # total-token ceiling.  Compilation must reject that false guarantee.
        with self.assertRaisesRegex(
            workflow_config.WorkflowError, "hard token_budget must be positive"
        ):
            workflow_config.compile_path(ROOT / "workflows" / "regulated.yaml")

    def test_positive_hard_budget_is_rejected_for_current_s0_providers(self) -> None:
        regulated = workflow_config.load_workflow(ROOT / "workflows" / "regulated.yaml")
        regulated["limits"]["token_budget"] = 1000
        with self.assertRaisesRegex(
            workflow_config.WorkflowError, "hard_total.*unsupported providers"
        ):
            workflow_config.compile_workflow(regulated, router=self.router)

    def test_compile_is_canonical_deterministic_and_has_no_cmux_effects(self) -> None:
        with mock.patch.object(
            workflow_config.router_config.subprocess,
            "run",
            side_effect=AssertionError("compile must not run a process"),
        ):
            first = workflow_config.compile_workflow(self.workflow, router=self.router)
            second = workflow_config.compile_workflow(
                copy.deepcopy(self.workflow), router=self.router
            )
        self.assertEqual(
            workflow_config.canonical_bytes(first),
            workflow_config.canonical_bytes(second),
        )
        self.assertEqual(first["schema_version"], 2)
        self.assertEqual(fleet_compiled.validate(first, mode="effect"), first)
        self.assertEqual(
            first["workflow_digest"], workflow_config.sha256(self.workflow)
        )
        resolved = workflow_config.canonical_bytes(first["resolved"])
        self.assertNotIn(b"command_shell", resolved)
        self.assertNotIn(b"tool_access", resolved)
        self.assertEqual(first["router_snapshot"], self.router)
        self.assertEqual(
            first["router_digest"], workflow_config.sha256(first["router_snapshot"])
        )
        self.assertRegex(first["resolved"]["launch_digest"], r"^[0-9a-f]{64}$")
        self.assertRegex(
            first["resolved"]["assurance_launch_digest"], r"^[0-9a-f]{64}$"
        )

    def test_compile_is_independent_of_runtime_path_availability(self) -> None:
        with mock.patch.object(
            workflow_config.router_config.shutil, "which", return_value=None
        ) as which:
            compiled = workflow_config.compile_workflow(
                self.workflow, router=self.router
            )
        which.assert_not_called()
        self.assertEqual(compiled["resolved"]["lead"]["role_type"], "codex")

    def test_compile_freezes_one_router_snapshot_before_both_resolutions(self) -> None:
        mutable_router = copy.deepcopy(self.router)
        original_router = copy.deepcopy(mutable_router)
        original_build_plan = workflow_config.router_config.build_plan
        calls = 0

        def build_from_frozen_snapshot(*args: object, **kwargs: object) -> dict:
            nonlocal calls
            result = original_build_plan(*args, **kwargs)
            calls += 1
            if calls == 1:
                mutable_router.clear()
                args[0].clear()
            return result

        with mock.patch.object(
            workflow_config.router_config,
            "build_plan",
            side_effect=build_from_frozen_snapshot,
        ):
            compiled = workflow_config.compile_workflow(
                self.workflow, router=mutable_router
            )

        self.assertEqual(calls, 2)
        self.assertEqual(
            compiled["router_digest"], workflow_config.sha256(original_router)
        )
        self.assertEqual(compiled["router_snapshot"], original_router)
        self.assertEqual(compiled["schema_version"], 2)

    def test_compiled_binding_rejects_live_router_and_roster_drift(self) -> None:
        compiled = workflow_config.compile_workflow(self.workflow, router=self.router)
        plan = workflow_config.router_config.build_plan(
            self.router,
            preset_name=compiled["resolved"]["preset"],
            run_healthcheck=False,
            check_runtime_availability=False,
        )
        binding = fleet_manifest.bind_plan(self.router, plan, compiled)
        self.assertEqual(binding["compiled_digest"], compiled["compiled_digest"])
        self.assertEqual(
            binding["launch_digest"], compiled["resolved"]["launch_digest"]
        )
        drifted = copy.deepcopy(self.router)
        drifted["defaults"]["preset"] = "research"
        with self.assertRaisesRegex(
            fleet_manifest.ManifestError, "live router digest drifted"
        ):
            fleet_manifest.bind_plan(drifted, plan, compiled)
        command_drifted = copy.deepcopy(plan)
        command_drifted["instances"][0]["command"].append("--unapproved-launch-flag")
        with self.assertRaisesRegex(
            fleet_manifest.ManifestError, "resolved router launch drifted"
        ):
            fleet_manifest.bind_plan(self.router, command_drifted, compiled)
        capability_drifted = copy.deepcopy(plan)
        capability_drifted["instances"][0]["capabilities"].append(
            "unapproved_capability"
        )
        with self.assertRaisesRegex(
            fleet_manifest.ManifestError, "resolved router launch drifted"
        ):
            fleet_manifest.bind_plan(self.router, capability_drifted, compiled)
        control_tool_drifted = copy.deepcopy(plan)
        control_tool_drifted["instances"][0]["tool_access"].remove("fleet_control")
        with self.assertRaisesRegex(
            fleet_manifest.ManifestError, "resolved router launch drifted"
        ):
            fleet_manifest.bind_plan(self.router, control_tool_drifted, compiled)
        limit_drifted = copy.deepcopy(plan)
        limit_drifted["limits"]["max_parallel_local_workers"] += 1
        with self.assertRaisesRegex(
            fleet_manifest.ManifestError, "resolved router launch drifted"
        ):
            fleet_manifest.bind_plan(self.router, limit_drifted, compiled)
        plan["instances"][0]["model"] = "unbound-model"
        with self.assertRaisesRegex(
            fleet_manifest.ManifestError, "resolved router roster drifted"
        ):
            fleet_manifest.bind_plan(self.router, plan, compiled)

        research_workflow = workflow_config.load_workflow(
            ROOT / "workflows" / "research.yaml"
        )
        research_compiled = workflow_config.compile_workflow(
            research_workflow, router=self.router
        )
        research_plan = workflow_config.router_config.build_plan(
            self.router,
            preset_name=research_compiled["resolved"]["preset"],
            run_healthcheck=False,
            check_runtime_availability=False,
        )
        research_binding = fleet_manifest.bind_plan(
            self.router, research_plan, research_compiled
        )
        self.assertEqual(
            research_binding["compiled_digest"], research_compiled["compiled_digest"]
        )
        assurance_binding = fleet_manifest.binding_for_preset(
            compiled, compiled["resolved"]["assurance_preset"]
        )
        self.assertEqual(
            assurance_binding["launch_digest"],
            compiled["resolved"]["assurance_launch_digest"],
        )

    def test_unknown_duplicate_shell_and_authority_keys_fail(self) -> None:
        fixtures = ROOT / "tests" / "fixtures" / "mission_control"
        for name, pattern in (
            ("invalid-duplicate-workflow.json", "duplicate.*key"),
            ("invalid-shell-workflow.json", "unknown fields: command"),
            ("invalid-authority-workflow.json", "unknown fields: authority"),
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(workflow_config.WorkflowError, pattern),
            ):
                workflow_config.load_workflow(fixtures / name)

    def test_unprovided_capability_and_writer_drift_fail_before_boot(self) -> None:
        invalid = copy.deepcopy(self.workflow)
        invalid["capabilities"]["available"].append("teleport")
        with self.assertRaisesRegex(workflow_config.WorkflowError, "not provided"):
            workflow_config.compile_workflow(invalid, router=self.router)

        invalid = copy.deepcopy(self.workflow)
        invalid["capabilities"]["writer"] = "none"
        with self.assertRaisesRegex(workflow_config.WorkflowError, "contains a writer"):
            workflow_config.compile_workflow(invalid, router=self.router)

    def test_assured_contract_and_subdelegation_depth_fail_closed(self) -> None:
        invalid = copy.deepcopy(self.workflow)
        invalid["assurance"]["profile"] = "assured"
        invalid["assurance"]["minimum_gates"] = ["fdp2"]
        with self.assertRaisesRegex(workflow_config.WorkflowError, "require fdp2"):
            workflow_config.validate_workflow(invalid)

        invalid = copy.deepcopy(self.workflow)
        invalid["autonomy"]["allow_subdelegation"] = False
        with self.assertRaisesRegex(workflow_config.WorkflowError, "must be 0"):
            workflow_config.validate_workflow(invalid)

    def test_audit_trust_scope_contract_fails_closed(self) -> None:
        invalid = copy.deepcopy(self.workflow)
        invalid["audit"].pop("trust_scope")
        with self.assertRaisesRegex(
            workflow_config.WorkflowError, "missing fields: trust_scope"
        ):
            workflow_config.validate_workflow(invalid)

        invalid = copy.deepcopy(self.workflow)
        invalid["audit"]["trust_scope"] = "external-compliance"
        with self.assertRaisesRegex(
            workflow_config.WorkflowError, "signed audit cannot claim"
        ):
            workflow_config.validate_workflow(invalid)

        invalid = copy.deepcopy(self.workflow)
        invalid["audit"]["trust_scope"] = "ambiguous"
        with self.assertRaisesRegex(workflow_config.WorkflowError, "must be one of"):
            workflow_config.validate_workflow(invalid)

        regulated = workflow_config.load_workflow(ROOT / "workflows" / "regulated.yaml")
        regulated["audit"]["trust_scope"] = "local-development"
        with self.assertRaisesRegex(
            workflow_config.WorkflowError, "regulated workflow requires"
        ):
            workflow_config.validate_workflow(regulated)

    def test_cli_validate_show_and_compile(self) -> None:
        script = SCRIPTS / "workflow_config.py"
        workflow = ROOT / "workflows" / "implementation.yaml"
        for command in ("validate", "show", "compile"):
            result = subprocess.run(
                ["python3", str(script), command, str(workflow)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.strip())
        compiled = json.loads(
            subprocess.run(
                ["python3", str(script), "compile", str(workflow)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout
        )
        self.assertEqual(compiled["schema_version"], 2)
        self.assertEqual(fleet_compiled.validate(compiled, mode="effect"), compiled)

    def test_cli_validate_is_static_but_compile_admits_effect_policy(self) -> None:
        script = SCRIPTS / "workflow_config.py"
        regulated = ROOT / "workflows" / "regulated.yaml"
        validated = subprocess.run(
            ["python3", str(script), "validate", str(regulated)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(validated.returncode, 0, validated.stderr)
        compiled = subprocess.run(
            ["python3", str(script), "compile", str(regulated)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(compiled.returncode, 2)
        self.assertIn("hard token_budget must be positive", compiled.stderr)


if __name__ == "__main__":
    unittest.main()
