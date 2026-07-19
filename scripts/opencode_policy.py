#!/usr/bin/env python3
"""Fail closed unless an OpenCode reviewer resolves to the read-only tool set."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import fleet_json  # noqa: E402

EXPECTED_BUILTIN_TOOLS = {"read", "glob", "grep"}
FLEET_CONTROL_TOOL_NAMES = {
    "fleet_control_dispatch",
    "fleet_control_dispatch_many",
    "fleet_control_wait",
    "fleet_control_get_result",
    "fleet_control_relay_result",
    "fleet_control_request_assurance",
    "fleet_control_request_human",
    "fleet_control_inspect_roster",
    "fleet_control_inspect_mission",
}
FLEET_CONTROL_PERMISSION = "fleet_control_*"
RESOLVE_TIMEOUT_SECONDS = 30
POLICY_PROBES = (
    ("bash", "git status", "deny"),
    ("bash", "find . -exec cmux ping \\;", "deny"),
    ("external_directory", "/Users/fleet-controller/.ssh/id_ed25519", "deny"),
    ("read", "src/example.py", "allow"),
    ("read", "/tmp/secret.env", "deny"),
    ("read", "/tmp/secret.env.local", "deny"),
    ("read", "/tmp/example.env.example", "allow"),
    ("glob", "**/*.py", "allow"),
    ("grep", "FLEET_RESULT", "allow"),
    ("future_model_tool", "*", "deny"),
)
FLEET_CONTROL_PROBES = (
    ("fleet_control_get_result", "*", "allow"),
    ("fleet_control_future", "*", "allow"),
    ("untrusted_mcp_tool", "*", "deny"),
)


class PolicyError(RuntimeError):
    pass


def wildcard_match(value: str, pattern: str) -> bool:
    expression = "".join(
        ".*" if character == "*" else "." if character == "?" else re.escape(character)
        for character in pattern
    )
    return re.fullmatch(expression, value, flags=re.DOTALL) is not None


def evaluate(rules: list[dict[str, Any]], permission: str, pattern: str) -> str:
    for rule in reversed(rules):
        if (
            wildcard_match(permission, str(rule.get("permission", "")))
            and wildcard_match(pattern, str(rule.get("pattern", "")))
        ):
            return str(rule.get("action", ""))
    return "deny"


def valid_specialist_socket(path: str) -> bool:
    if not path or not os.path.isabs(path):
        return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISSOCK(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o600
    )


def validate_resolved_config(document: dict[str, Any]) -> dict[str, Any]:
    mcp = document.get("mcp")
    if not isinstance(mcp, dict) or set(mcp) != {"fleet_control"}:
        raise PolicyError("resolved OpenCode MCP set is not exactly fleet_control")
    server = mcp["fleet_control"]
    if not isinstance(server, dict) or set(server) != {
        "type",
        "command",
        "enabled",
        "timeout",
    }:
        raise PolicyError("resolved fleet_control MCP contract is malformed")
    command = server.get("command")
    expected_proxy = ROOT / "scripts" / "fleet_agent_mcp.py"
    if (
        server.get("type") != "local"
        or server.get("enabled") is not True
        or server.get("timeout") != 10_000
        or not isinstance(command, list)
        or len(command) != 2
        or any(not isinstance(value, str) or not os.path.isabs(value) for value in command)
        or Path(command[0]).resolve() != Path(sys.executable).resolve()
        or Path(command[1]) != expected_proxy
    ):
        raise PolicyError("resolved fleet_control MCP binding drifted")
    return {"mcp_servers": ["fleet_control"], "status": "ok"}


def validate_resolved_agent(
    document: dict[str, Any],
    agent_name: str,
    *,
    allowed_external_patterns: tuple[str, ...] = (),
    fleet_control_enabled: bool = False,
) -> dict[str, Any]:
    if document.get("name") != agent_name:
        raise PolicyError(f"resolved agent name mismatch: expected {agent_name}")
    raw_rules = document.get("permission")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise PolicyError("resolved agent lacks permission rules")
    rules = [rule for rule in raw_rules if isinstance(rule, dict)]
    if len(rules) != len(raw_rules):
        raise PolicyError("resolved agent contains malformed permission rules")
    tools = document.get("tools")
    if not isinstance(tools, dict) or not all(isinstance(value, bool) for value in tools.values()):
        raise PolicyError("resolved agent lacks a boolean tool map")
    enabled = {name for name, value in tools.items() if value}
    enabled_builtin = enabled & EXPECTED_BUILTIN_TOOLS
    enabled_fleet_control = enabled & FLEET_CONTROL_TOOL_NAMES
    unknown_enabled = enabled - EXPECTED_BUILTIN_TOOLS - FLEET_CONTROL_TOOL_NAMES
    # Current OpenCode releases omit dynamically discovered MCP tools from
    # `debug agent --pure` until the local MCP process starts. The exact MCP
    # server/config is validated separately above. If this debug surface does
    # enumerate any fleet_control tools, it must enumerate the complete set;
    # a partial dynamic surface is never accepted.
    dynamic_mcp_complete = enabled_fleet_control in (
        set(),
        FLEET_CONTROL_TOOL_NAMES if fleet_control_enabled else set(),
    )
    if (
        enabled_builtin != EXPECTED_BUILTIN_TOOLS
        or unknown_enabled
        or not dynamic_mcp_complete
        or (not fleet_control_enabled and enabled_fleet_control)
    ):
        raise PolicyError(
            "resolved OpenCode tools are not the exact specialist set: "
            f"enabled={','.join(sorted(enabled)) or '-'}"
        )
    failures = []
    if not any(
        rule.get("permission") == "*"
        and rule.get("pattern") == "*"
        and rule.get("action") == "deny"
        for rule in rules
    ):
        failures.append("permission policy lacks an explicit global catch-all deny")
    probes = list(POLICY_PROBES)
    if fleet_control_enabled:
        probes.extend(FLEET_CONTROL_PROBES)
    else:
        probes.append(("fleet_control_get_result", "*", "deny"))
    for permission, pattern, expected in probes:
        actual = evaluate(rules, permission, pattern)
        if actual != expected:
            failures.append(f"{permission}:{pattern!r}={actual}, expected {expected}")
    external_deny = max(
        (
            index
            for index, rule in enumerate(rules)
            if wildcard_match("external_directory", str(rule.get("permission", "")))
            and rule.get("pattern") == "*"
            and rule.get("action") == "deny"
        ),
        default=-1,
    )
    if external_deny < 0:
        failures.append("external_directory lacks a catch-all deny")
    else:
        for rule in rules[external_deny + 1 :]:
            if not wildcard_match(
                "external_directory", str(rule.get("permission", ""))
            ):
                continue
            action = str(rule.get("action", ""))
            pattern = str(rule.get("pattern", ""))
            if action == "deny":
                continue
            if action == "allow" and pattern in allowed_external_patterns:
                continue
            failures.append(
                f"external_directory has an unapproved late rule: {pattern!r}={action}"
            )
    if failures:
        raise PolicyError("resolved OpenCode permission probes failed: " + "; ".join(failures))
    return {
        "agent": agent_name,
        "enabled_tools": sorted(enabled),
        "enabled_builtin_tools": sorted(enabled_builtin),
        "enabled_fleet_control_tools": sorted(enabled_fleet_control),
        "fleet_control_dynamic_discovery": (
            "complete" if enabled_fleet_control else "deferred_to_mcp_start"
        ),
        "external_exceptions": sorted(allowed_external_patterns),
        "policy_probes": len(probes),
        "status": "ok",
    }


def resolve_agent(executable: str, agent_name: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [executable, "debug", "agent", agent_name, "--pure"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=RESOLVE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PolicyError(
            f"OpenCode agent resolution timed out after {RESOLVE_TIMEOUT_SECONDS}s"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise PolicyError(f"cannot resolve OpenCode agent {agent_name}: {detail}")
    try:
        document = fleet_json.loads(result.stdout)
    except fleet_json.FleetJSONError as exc:
        raise PolicyError(
            f"OpenCode agent debug output is not strict JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise PolicyError("OpenCode agent debug output must be an object")
    return document


def resolve_config(executable: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [executable, "debug", "config", "--pure"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=RESOLVE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PolicyError(
            f"OpenCode config resolution timed out after {RESOLVE_TIMEOUT_SECONDS}s"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise PolicyError(f"cannot resolve OpenCode config: {detail}")
    try:
        document = fleet_json.loads(result.stdout)
    except fleet_json.FleetJSONError as exc:
        raise PolicyError(
            f"OpenCode config debug output is not strict JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise PolicyError("OpenCode config debug output must be an object")
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("agent")
    parser.add_argument("--executable", default="opencode")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    xdg_data = os.environ.get("XDG_DATA_HOME")
    allowed_external_patterns = (
        (str(Path(xdg_data) / "opencode" / "tool-output" / "*"),)
        if xdg_data
        else ()
    )
    fleet_control_enabled = valid_specialist_socket(
        os.environ.get("FLEET_CONTROL_SOCKET", "")
    )
    try:
        if fleet_control_enabled:
            validate_resolved_config(resolve_config(args.executable))
        result = validate_resolved_agent(
            resolve_agent(args.executable, args.agent),
            args.agent,
            allowed_external_patterns=allowed_external_patterns,
            fleet_control_enabled=fleet_control_enabled,
        )
    except (OSError, PolicyError) as exc:
        print(f"OpenCode policy check failed: {exc}", file=sys.stderr)
        return 2
    if not args.quiet:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
