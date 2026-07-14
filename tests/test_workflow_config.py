from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
workflow_config = importlib.import_module("workflow_config")


class WorkflowConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = workflow_config.load_workflow(ROOT / "workflows" / "implementation.yaml")
        self.router = workflow_config.router_config.load_router()

    def test_repository_workflows_compile_against_router(self) -> None:
        expected = {"hotfix", "implementation", "regulated", "research"}
        paths = sorted((ROOT / "workflows").glob("*.yaml"))
        compiled = [workflow_config.compile_path(path) for path in paths]
        self.assertEqual({item["workflow"]["name"] for item in compiled}, expected)
        implementation = next(item for item in compiled if item["workflow"]["name"] == "implementation")
        self.assertEqual(implementation["resolved"]["writer_instance"], "builder")
        self.assertEqual(implementation["resolved"]["mode"], "autonomous")

    def test_compile_is_canonical_deterministic_and_has_no_cmux_effects(self) -> None:
        with mock.patch.object(
            workflow_config.router_config.subprocess,
            "run",
            side_effect=AssertionError("compile must not run a process"),
        ):
            first = workflow_config.compile_workflow(self.workflow, router=self.router)
            second = workflow_config.compile_workflow(copy.deepcopy(self.workflow), router=self.router)
        self.assertEqual(workflow_config.canonical_bytes(first), workflow_config.canonical_bytes(second))
        self.assertEqual(first["workflow_digest"], workflow_config.sha256(self.workflow))
        encoded = workflow_config.canonical_bytes(first)
        self.assertNotIn(b"command_shell", encoded)
        self.assertNotIn(b"tool_access", encoded)

    def test_unknown_duplicate_shell_and_authority_keys_fail(self) -> None:
        fixtures = ROOT / "tests" / "fixtures" / "mission_control"
        for name, pattern in (
            ("invalid-duplicate-workflow.json", "duplicate key"),
            ("invalid-shell-workflow.json", "unknown fields: command"),
            ("invalid-authority-workflow.json", "unknown fields: authority"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(
                workflow_config.WorkflowError, pattern
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
        self.assertEqual(compiled["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
