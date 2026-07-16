from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "opencode_policy.py"
SPEC = importlib.util.spec_from_file_location("opencode_policy", MODULE_PATH)
assert SPEC and SPEC.loader
opencode_policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(opencode_policy)


def resolved_agent() -> dict:
    return {
        "name": "fleet-reviewer",
        "permission": [
            {"permission": "*", "pattern": "*", "action": "allow"},
            {"permission": "external_directory", "pattern": "*", "action": "allow"},
            {"permission": "*", "pattern": "*", "action": "deny"},
            {"permission": "read", "pattern": "*", "action": "allow"},
            {"permission": "read", "pattern": "*.env", "action": "deny"},
            {"permission": "read", "pattern": "*.env.*", "action": "deny"},
            {"permission": "read", "pattern": "*.env.example", "action": "allow"},
            {"permission": "glob", "pattern": "*", "action": "allow"},
            {"permission": "grep", "pattern": "*", "action": "allow"},
            {"permission": "external_directory", "pattern": "*", "action": "deny"},
        ],
        "tools": {
            "invalid": False,
            "bash": False,
            "read": True,
            "glob": True,
            "grep": True,
            "edit": False,
            "write": False,
            "task": False,
        },
    }


class OpenCodePolicyTests(unittest.TestCase):
    def test_exact_read_only_policy_passes_after_permissive_global_rules(self) -> None:
        result = opencode_policy.validate_resolved_agent(
            resolved_agent(), "fleet-reviewer"
        )
        self.assertEqual(result["enabled_tools"], ["glob", "grep", "read"])
        self.assertEqual(result["policy_probes"], len(opencode_policy.POLICY_PROBES))

    def test_any_enabled_process_tool_fails_closed(self) -> None:
        document = resolved_agent()
        document["tools"]["bash"] = True
        with self.assertRaisesRegex(opencode_policy.PolicyError, "exact read-only set"):
            opencode_policy.validate_resolved_agent(document, "fleet-reviewer")

    def test_late_external_allow_fails_closed(self) -> None:
        document = resolved_agent()
        document["permission"].append(
            {"permission": "external_directory", "pattern": "*", "action": "allow"}
        )
        with self.assertRaisesRegex(opencode_policy.PolicyError, "permission probes failed"):
            opencode_policy.validate_resolved_agent(document, "fleet-reviewer")

    def test_only_exact_isolated_tool_output_exception_is_allowed(self) -> None:
        document = resolved_agent()
        pattern = "/tmp/fleet/xdg/data/opencode/tool-output/*"
        document["permission"].append(
            {"permission": "external_directory", "pattern": pattern, "action": "allow"}
        )
        result = opencode_policy.validate_resolved_agent(
            document,
            "fleet-reviewer",
            allowed_external_patterns=(pattern,),
        )
        self.assertEqual(result["external_exceptions"], [pattern])

    def test_late_bash_glob_fails_even_when_tool_map_claims_disabled(self) -> None:
        document = resolved_agent()
        document["permission"].append(
            {"permission": "bash", "pattern": "find *", "action": "allow"}
        )
        with self.assertRaisesRegex(opencode_policy.PolicyError, "permission probes failed"):
            opencode_policy.validate_resolved_agent(document, "fleet-reviewer")

    def test_missing_global_catch_all_deny_fails_closed(self) -> None:
        document = resolved_agent()
        document["permission"] = [
            rule
            for rule in document["permission"]
            if not (
                rule["permission"] == "*"
                and rule["pattern"] == "*"
                and rule["action"] == "deny"
            )
        ]
        with self.assertRaisesRegex(opencode_policy.PolicyError, "global catch-all deny"):
            opencode_policy.validate_resolved_agent(document, "fleet-reviewer")

    def test_agent_resolution_timeout_fails_closed(self) -> None:
        with mock.patch.object(
            opencode_policy.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="opencode", timeout=30),
        ):
            with self.assertRaisesRegex(opencode_policy.PolicyError, "timed out"):
                opencode_policy.resolve_agent("opencode", "fleet-reviewer")

    def test_simple_wildcards_do_not_treat_brackets_as_character_classes(self) -> None:
        self.assertTrue(opencode_policy.wildcard_match("file[1]", "file[1]"))
        self.assertFalse(opencode_policy.wildcard_match("file1", "file[1]"))
        self.assertTrue(opencode_policy.wildcard_match("a/b/c", "a*c"))


if __name__ == "__main__":
    unittest.main()
