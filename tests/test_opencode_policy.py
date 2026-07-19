from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "opencode_policy.py"
SPEC = importlib.util.spec_from_file_location("opencode_policy", MODULE_PATH)
assert SPEC and SPEC.loader
opencode_policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(opencode_policy)


def resolved_agent(*, fleet_control_enabled: bool = False) -> dict:
    document = {
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
    if fleet_control_enabled:
        document["permission"].append(
            {
                "permission": "fleet_control_*",
                "pattern": "*",
                "action": "allow",
            }
        )
        document["tools"].update(
            {name: True for name in opencode_policy.FLEET_CONTROL_TOOL_NAMES}
        )
    return document


class OpenCodePolicyTests(unittest.TestCase):
    def test_exact_read_only_policy_passes_after_permissive_global_rules(self) -> None:
        result = opencode_policy.validate_resolved_agent(
            resolved_agent(), "fleet-reviewer"
        )
        self.assertEqual(result["enabled_tools"], ["glob", "grep", "read"])
        self.assertEqual(
            result["policy_probes"], len(opencode_policy.POLICY_PROBES) + 1
        )

    def test_any_enabled_process_tool_fails_closed(self) -> None:
        document = resolved_agent()
        document["tools"]["bash"] = True
        with self.assertRaisesRegex(opencode_policy.PolicyError, "exact specialist set"):
            opencode_policy.validate_resolved_agent(document, "fleet-reviewer")

    def test_exact_fleet_control_tools_are_callable_without_enabling_other_tools(self) -> None:
        document = resolved_agent(fleet_control_enabled=True)
        result = opencode_policy.validate_resolved_agent(
            document,
            "fleet-reviewer",
            fleet_control_enabled=True,
        )
        self.assertEqual(result["enabled_builtin_tools"], ["glob", "grep", "read"])
        self.assertEqual(
            set(result["enabled_fleet_control_tools"]),
            opencode_policy.FLEET_CONTROL_TOOL_NAMES,
        )
        self.assertEqual(
            opencode_policy.evaluate(document["permission"], "untrusted_mcp_tool", "*"),
            "deny",
        )
        deferred = resolved_agent(fleet_control_enabled=True)
        for name in opencode_policy.FLEET_CONTROL_TOOL_NAMES:
            deferred["tools"][name] = False
        deferred_result = opencode_policy.validate_resolved_agent(
            deferred,
            "fleet-reviewer",
            fleet_control_enabled=True,
        )
        self.assertEqual(
            deferred_result["fleet_control_dynamic_discovery"],
            "deferred_to_mcp_start",
        )
        missing = resolved_agent(fleet_control_enabled=True)
        missing["tools"]["fleet_control_get_result"] = False
        with self.assertRaisesRegex(opencode_policy.PolicyError, "exact specialist set"):
            opencode_policy.validate_resolved_agent(
                missing,
                "fleet-reviewer",
                fleet_control_enabled=True,
            )

    def test_specialist_socket_requires_absolute_owned_mode_0600_socket(self) -> None:
        metadata = SimpleNamespace(
            st_mode=stat.S_IFSOCK | 0o600,
            st_uid=os.geteuid(),
        )
        with mock.patch.object(opencode_policy.os, "lstat", return_value=metadata):
            self.assertTrue(opencode_policy.valid_specialist_socket("/tmp/control.sock"))
            self.assertFalse(opencode_policy.valid_specialist_socket("control.sock"))
        for mode, owner in (
            (stat.S_IFREG | 0o600, os.geteuid()),
            (stat.S_IFSOCK | 0o666, os.geteuid()),
            (stat.S_IFSOCK | 0o600, os.geteuid() + 1),
        ):
            with self.subTest(mode=mode, owner=owner), mock.patch.object(
                opencode_policy.os,
                "lstat",
                return_value=SimpleNamespace(st_mode=mode, st_uid=owner),
            ):
                self.assertFalse(
                    opencode_policy.valid_specialist_socket("/tmp/control.sock")
                )

    def test_resolved_config_requires_only_the_controller_proxy(self) -> None:
        command = [
            sys.executable,
            str(ROOT / "scripts" / "fleet_agent_mcp.py"),
        ]
        document = {
            "mcp": {
                "fleet_control": {
                    "type": "local",
                    "command": command,
                    "enabled": True,
                    "timeout": 10_000,
                }
            }
        }
        self.assertEqual(
            opencode_policy.validate_resolved_config(document)["mcp_servers"],
            ["fleet_control"],
        )
        document["mcp"]["controller_wide"] = {
            "type": "remote",
            "url": "https://example.invalid/mcp",
        }
        with self.assertRaisesRegex(opencode_policy.PolicyError, "exactly fleet_control"):
            opencode_policy.validate_resolved_config(document)

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

    def test_resolved_policy_json_is_unambiguous_before_validation(self) -> None:
        invalid = (
            '{"permission":[],"permission":[]}',
            '{"value":1e999}',
            '\ufeff{}',
            r'{"value":"\ud800"}',
            '{}{}',
        )
        for payload in invalid:
            completed = subprocess.CompletedProcess(
                ["opencode"], 0, payload, ""
            )
            for resolver, args in (
                (opencode_policy.resolve_agent, ("opencode", "fleet-reviewer")),
                (opencode_policy.resolve_config, ("opencode",)),
            ):
                with (
                    self.subTest(payload=payload, resolver=resolver.__name__),
                    mock.patch.object(
                        opencode_policy.subprocess, "run", return_value=completed
                    ),
                    self.assertRaisesRegex(
                        opencode_policy.PolicyError, "strict JSON"
                    ),
                ):
                    resolver(*args)

    def test_simple_wildcards_do_not_treat_brackets_as_character_classes(self) -> None:
        self.assertTrue(opencode_policy.wildcard_match("file[1]", "file[1]"))
        self.assertFalse(opencode_policy.wildcard_match("file1", "file[1]"))
        self.assertTrue(opencode_policy.wildcard_match("a/b/c", "a*c"))


if __name__ == "__main__":
    unittest.main()
