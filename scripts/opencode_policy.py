#!/usr/bin/env python3
"""Fail closed unless an OpenCode reviewer resolves to the read-only tool set."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


EXPECTED_ENABLED_TOOLS = {"read", "glob", "grep"}
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


def validate_resolved_agent(
    document: dict[str, Any],
    agent_name: str,
    *,
    allowed_external_patterns: tuple[str, ...] = (),
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
    if enabled != EXPECTED_ENABLED_TOOLS:
        raise PolicyError(
            "resolved OpenCode tools are not the exact read-only set: "
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
    for permission, pattern, expected in POLICY_PROBES:
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
        "external_exceptions": sorted(allowed_external_patterns),
        "policy_probes": len(POLICY_PROBES),
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
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"OpenCode agent debug output is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise PolicyError("OpenCode agent debug output must be an object")
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
    try:
        result = validate_resolved_agent(
            resolve_agent(args.executable, args.agent),
            args.agent,
            allowed_external_patterns=allowed_external_patterns,
        )
    except (OSError, PolicyError) as exc:
        print(f"OpenCode policy check failed: {exc}", file=sys.stderr)
        return 2
    if not args.quiet:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
