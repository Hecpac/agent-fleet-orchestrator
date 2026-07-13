#!/usr/bin/env python3
"""Validate and resolve the capability-first fleet router.

``orchestration/router.yaml`` is intentionally JSON-compatible YAML so the
runtime can use Python's standard library and still reject duplicate keys.
This module performs all planning before cmux is allowed to mutate state.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROUTER = ROOT / "orchestration" / "router.yaml"
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
RESERVED_INSTANCE_IDS = {"workspace", "lead", "monitor"}
RUNNERS = {"interactive", "local"}
AUTHORITIES = {"control", "write", "advisory", "verification"}
RESOURCE_CLASSES = {"remote", "local_light", "local_heavy"}
PHASES = {"CONTROL", "RECON", "BUILD", "CHALLENGE", "VERIFY"}
US = "\x1f"


class RouterError(ValueError):
    """Configuration or planning error that must fail before cmux effects."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RouterError(f"duplicate key: {key}")
        result[key] = value
    return result


def load_router(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    router_path = Path(path or os.environ.get("FLEET_ROUTER_PATH", DEFAULT_ROUTER))
    try:
        with router_path.open(encoding="utf-8") as handle:
            config = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except RouterError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RouterError(f"cannot load router {router_path}: {exc}") from exc
    validate_router(config)
    return config


def _expect_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RouterError(f"{where} must be an object")
    return value


def _expect_keys(
    value: dict[str, Any],
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    where: str,
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - value.keys())
    unknown = sorted(value.keys() - allowed)
    if missing:
        raise RouterError(f"{where} missing fields: {', '.join(missing)}")
    if unknown:
        raise RouterError(f"{where} unknown fields: {', '.join(unknown)}")


def _expect_identifier(value: Any, where: str, *, allow_reserved: bool = False) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise RouterError(f"{where} must match {IDENTIFIER_RE.pattern}")
    if not allow_reserved and value in RESERVED_INSTANCE_IDS:
        raise RouterError(f"{where} uses reserved id: {value}")
    return value


def _expect_string_list(value: Any, where: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise RouterError(f"{where} must be a list of non-empty strings")
    if nonempty and not value:
        raise RouterError(f"{where} must not be empty")
    if any("\n" in item or US in item for item in value):
        raise RouterError(f"{where} contains a forbidden control character")
    return value


def _expect_command(value: Any, where: str) -> list[str]:
    command = _expect_string_list(value, where, nonempty=True)
    if any("\x00" in item for item in command):
        raise RouterError(f"{where} contains NUL")
    return command


def _expect_rank(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RouterError(f"{where} must be a non-negative integer")
    return value


def _expect_manifest_scalar(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise RouterError(f"{where} must be a non-empty string")
    if any(character in value for character in ("\n", "\r", "\x00", US)):
        raise RouterError(f"{where} contains a forbidden control character")
    return value


def validate_router(config: dict[str, Any]) -> None:
    config = _expect_mapping(config, "router")
    _expect_keys(
        config,
        required={"schema_version", "defaults", "limits", "lead", "roles", "presets"},
        where="router",
    )
    if config["schema_version"] != 3:
        raise RouterError("router.schema_version must be 3")

    defaults = _expect_mapping(config["defaults"], "router.defaults")
    _expect_keys(defaults, required={"preset", "race_roles"}, where="router.defaults")
    _expect_identifier(defaults["preset"], "router.defaults.preset", allow_reserved=True)
    race_roles = _expect_string_list(defaults["race_roles"], "router.defaults.race_roles", nonempty=True)

    limits = _expect_mapping(config["limits"], "router.limits")
    _expect_keys(
        limits,
        required={"max_local_heavy_in_flight", "max_parallel_local_workers", "avoid_local_models"},
        optional={"local_token_budget_per_feature"},
        where="router.limits",
    )
    for key in ("max_local_heavy_in_flight", "max_parallel_local_workers"):
        if isinstance(limits[key], bool) or not isinstance(limits[key], int) or limits[key] < 1:
            raise RouterError(f"router.limits.{key} must be a positive integer")
    if "local_token_budget_per_feature" in limits:
        budget = limits["local_token_budget_per_feature"]
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise RouterError(
                "router.limits.local_token_budget_per_feature must be a positive integer"
            )
    _expect_string_list(limits["avoid_local_models"], "router.limits.avoid_local_models")

    roles = _expect_mapping(config["roles"], "router.roles")
    if not roles:
        raise RouterError("router.roles must not be empty")
    role_required = {
        "phase",
        "runner",
        "provider",
        "enabled",
        "requires_env",
        "capabilities",
        "tool_access",
        "authority",
        "resource_class",
        "display_rank",
        "concurrency",
    }
    role_optional = {
        "command",
        "healthcheck",
        "ready_pattern",
        "model",
        "instructions",
        "hook_source",
    }
    for role_type, raw_role in roles.items():
        _expect_identifier(role_type, f"router.roles.{role_type}", allow_reserved=True)
        role = _expect_mapping(raw_role, f"router.roles.{role_type}")
        _expect_keys(
            role,
            required=role_required,
            optional=role_optional,
            where=f"router.roles.{role_type}",
        )
        if role["runner"] not in RUNNERS:
            raise RouterError(f"router.roles.{role_type}.runner must be one of {sorted(RUNNERS)}")
        if role["phase"] not in PHASES - {"CONTROL"}:
            raise RouterError(
                f"router.roles.{role_type}.phase must be one of {sorted(PHASES - {'CONTROL'})}"
            )
        _expect_manifest_scalar(role["provider"], f"router.roles.{role_type}.provider")
        if not isinstance(role["enabled"], bool):
            raise RouterError(f"router.roles.{role_type}.enabled must be boolean")
        env_names = _expect_string_list(role["requires_env"], f"router.roles.{role_type}.requires_env")
        for env_name in env_names:
            if not ENV_RE.fullmatch(env_name):
                raise RouterError(f"router.roles.{role_type}.requires_env has invalid name: {env_name}")
        _expect_string_list(role["capabilities"], f"router.roles.{role_type}.capabilities", nonempty=True)
        _expect_string_list(role["tool_access"], f"router.roles.{role_type}.tool_access", nonempty=True)
        if role["authority"] not in AUTHORITIES - {"control"}:
            raise RouterError(
                f"router.roles.{role_type}.authority must be one of {sorted(AUTHORITIES - {'control'})}"
            )
        if role["phase"] in {"RECON", "CHALLENGE", "VERIFY"} and role["authority"] == "write":
            raise RouterError(f"router.roles.{role_type} phase {role['phase']} cannot write")
        if role["phase"] == "RECON" and role["authority"] != "advisory":
            raise RouterError(f"router.roles.{role_type} RECON requires advisory authority")
        if role["phase"] == "CHALLENGE" and role["authority"] != "advisory":
            raise RouterError(f"router.roles.{role_type} CHALLENGE requires advisory authority")
        if role["phase"] == "VERIFY" and role["authority"] not in {"verification", "advisory"}:
            raise RouterError(f"router.roles.{role_type} VERIFY requires verification/advisory authority")
        if role["phase"] in {"RECON", "CHALLENGE", "VERIFY"}:
            forbidden = [
                tool for tool in role["tool_access"]
                if tool not in {"prompt_only", "filesystem_read", "shell_read", "git_read"}
            ]
            if forbidden:
                raise RouterError(
                    f"router.roles.{role_type} phase {role['phase']} has write-capable tools: {forbidden}"
                )
        if role["resource_class"] not in RESOURCE_CLASSES:
            raise RouterError(
                f"router.roles.{role_type}.resource_class must be one of {sorted(RESOURCE_CLASSES)}"
            )
        _expect_rank(role["display_rank"], f"router.roles.{role_type}.display_rank")
        if isinstance(role["concurrency"], bool) or not isinstance(role["concurrency"], int) or role["concurrency"] < 1:
            raise RouterError(f"router.roles.{role_type}.concurrency must be a positive integer")

        if role["runner"] == "interactive":
            _expect_command(role.get("command"), f"router.roles.{role_type}.command")
            if "healthcheck" in role:
                _expect_command(role["healthcheck"], f"router.roles.{role_type}.healthcheck")
            if not isinstance(role.get("ready_pattern"), str) or not role["ready_pattern"]:
                raise RouterError(f"router.roles.{role_type}.ready_pattern must be a non-empty string")
            if "\n" in role["ready_pattern"] or US in role["ready_pattern"]:
                raise RouterError(f"router.roles.{role_type}.ready_pattern contains a forbidden control character")
            if "instructions" in role:
                raise RouterError(
                    f"router.roles.{role_type} interactive roles cannot set instructions"
                )
            model = _expect_manifest_scalar(
                role.get("model"), f"router.roles.{role_type}.model"
            )
            hook_source = _expect_manifest_scalar(
                role.get("hook_source"), f"router.roles.{role_type}.hook_source"
            )
            _expect_identifier(
                hook_source,
                f"router.roles.{role_type}.hook_source",
                allow_reserved=True,
            )
            if hook_source not in {"codex", "claude", "opencode"}:
                raise RouterError(
                    f"router.roles.{role_type}.hook_source is not supported: {hook_source}"
                )
            if hook_source == "opencode":
                if role["command"][0] != "opencode" or "-m" not in role["command"]:
                    raise RouterError(
                        f"router.roles.{role_type} OpenCode role must use opencode -m"
                    )
                model_index = role["command"].index("-m") + 1
                if model_index >= len(role["command"]):
                    raise RouterError(
                        f"router.roles.{role_type} OpenCode role lacks command model"
                    )
                command_model = role["command"][model_index]
                expected_model = f"{role['provider']}/{role.get('model', '')}"
                if command_model != expected_model:
                    raise RouterError(
                        f"router.roles.{role_type} provider/model must match command -m"
                    )
            elif hook_source == "codex":
                if role["provider"] != "openai" or role["command"][0] != "codex":
                    raise RouterError(
                        f"router.roles.{role_type} codex source requires openai/codex"
                    )
                if "--model" not in role["command"]:
                    raise RouterError(
                        f"router.roles.{role_type} codex role lacks command model"
                    )
                model_index = role["command"].index("--model") + 1
                if model_index >= len(role["command"]) or role["command"][model_index] != model:
                    raise RouterError(
                        f"router.roles.{role_type} model must match command --model"
                    )
            elif hook_source == "claude":
                if role["provider"] != "anthropic" or role["command"][0] != "claude":
                    raise RouterError(
                        f"router.roles.{role_type} claude source requires anthropic/claude"
                    )
                if "--model" not in role["command"]:
                    raise RouterError(
                        f"router.roles.{role_type} claude role lacks command model"
                    )
                model_index = role["command"].index("--model") + 1
                if model_index >= len(role["command"]) or role["command"][model_index] != model:
                    raise RouterError(
                        f"router.roles.{role_type} model must match command --model"
                    )
            if role["command"][0] == "opencode" and hook_source != "opencode":
                raise RouterError(
                    f"router.roles.{role_type} opencode command requires hook_source opencode"
                )
            if role["resource_class"] != "remote":
                raise RouterError(f"router.roles.{role_type} interactive role must use resource_class remote")
        else:
            if "command" in role or "healthcheck" in role or "ready_pattern" in role:
                raise RouterError(f"router.roles.{role_type} local roles cannot set command/healthcheck/ready_pattern")
            _expect_manifest_scalar(role.get("model"), f"router.roles.{role_type}.model")
            if not isinstance(role.get("instructions"), str) or not role["instructions"]:
                raise RouterError(f"router.roles.{role_type}.instructions must be a non-empty string")
            if "\n" in role["instructions"] or US in role["instructions"]:
                raise RouterError(f"router.roles.{role_type}.instructions contains a forbidden control character")
            if role["resource_class"] == "remote":
                raise RouterError(f"router.roles.{role_type} local role cannot use resource_class remote")

    for role_type in race_roles:
        if role_type not in roles:
            raise RouterError(f"router.defaults.race_roles references unknown role: {role_type}")
        if roles[role_type]["runner"] != "interactive":
            raise RouterError(f"router.defaults.race_roles must reference interactive roles: {role_type}")

    lead = _expect_mapping(config["lead"], "router.lead")
    _expect_keys(
        lead,
        required={
            "instance_id",
            "phase",
            "display_rank",
            "candidates",
            "fallback_policy",
            "authority",
            "tool_access",
            "capabilities",
        },
        where="router.lead",
    )
    if lead["instance_id"] != "lead":
        raise RouterError("router.lead.instance_id must be lead")
    if lead["phase"] != "CONTROL":
        raise RouterError("router.lead.phase must be CONTROL")
    _expect_rank(lead["display_rank"], "router.lead.display_rank")
    candidates = _expect_string_list(lead["candidates"], "router.lead.candidates", nonempty=True)
    if len(set(candidates)) != len(candidates):
        raise RouterError("router.lead.candidates contains duplicates")
    for candidate in candidates:
        if candidate not in roles:
            raise RouterError(f"router.lead.candidates references unknown role: {candidate}")
        if roles[candidate]["runner"] != "interactive":
            raise RouterError(f"router.lead candidate must be interactive: {candidate}")
    if lead["fallback_policy"] != "first_available":
        raise RouterError("router.lead.fallback_policy must be first_available")
    if lead["authority"] != "control":
        raise RouterError("router.lead.authority must be control")
    _expect_string_list(lead["tool_access"], "router.lead.tool_access", nonempty=True)
    _expect_string_list(lead["capabilities"], "router.lead.capabilities", nonempty=True)

    presets = _expect_mapping(config["presets"], "router.presets")
    if defaults["preset"] not in presets:
        raise RouterError(f"router.defaults.preset references unknown preset: {defaults['preset']}")
    for preset_name, raw_preset in presets.items():
        _expect_identifier(preset_name, f"router.presets.{preset_name}", allow_reserved=True)
        preset = _expect_mapping(raw_preset, f"router.presets.{preset_name}")
        _expect_keys(
            preset,
            required={"description", "include_lead", "instances"},
            optional={"lead_provider"},
            where=f"router.presets.{preset_name}",
        )
        if not isinstance(preset["description"], str) or not preset["description"]:
            raise RouterError(f"router.presets.{preset_name}.description must be a non-empty string")
        if not isinstance(preset["include_lead"], bool):
            raise RouterError(f"router.presets.{preset_name}.include_lead must be boolean")
        if "lead_provider" in preset and preset["lead_provider"] not in candidates:
            raise RouterError(f"router.presets.{preset_name}.lead_provider is not a lead candidate")
        if not isinstance(preset["instances"], list):
            raise RouterError(f"router.presets.{preset_name}.instances must be a list")
        _validate_instances(config, preset["instances"], f"router.presets.{preset_name}.instances")


def _validate_instances(config: dict[str, Any], instances: list[dict[str, Any]], where: str) -> None:
    seen: set[str] = set()
    writers = 0
    for index, raw_instance in enumerate(instances):
        instance = _expect_mapping(raw_instance, f"{where}[{index}]")
        _expect_keys(
            instance,
            required={"instance_id", "role_type"},
            optional={"display_rank", "authority", "tool_access", "phase"},
            where=f"{where}[{index}]",
        )
        instance_id = _expect_identifier(instance["instance_id"], f"{where}[{index}].instance_id")
        if instance_id in seen:
            raise RouterError(f"{where} has duplicate instance_id: {instance_id}")
        seen.add(instance_id)
        role_type = instance["role_type"]
        if role_type not in config["roles"]:
            raise RouterError(f"{where}[{index}] references unknown role_type: {role_type}")
        role = config["roles"][role_type]
        if not role["enabled"]:
            raise RouterError(f"{where}[{index}] references disabled role_type: {role_type}")
        if "display_rank" in instance:
            _expect_rank(instance["display_rank"], f"{where}[{index}].display_rank")
        phase = instance.get("phase", role["phase"])
        if phase not in PHASES - {"CONTROL"}:
            raise RouterError(f"{where}[{index}].phase is invalid: {phase}")
        authority = instance.get("authority", role["authority"])
        if authority not in AUTHORITIES - {"control"}:
            raise RouterError(f"{where}[{index}].authority is invalid: {authority}")
        if authority == "write":
            writers += 1
        if phase in {"RECON", "CHALLENGE", "VERIFY"} and authority == "write":
            raise RouterError(f"{where}[{index}] phase {phase} cannot have write authority")
        if phase == "RECON" and authority != "advisory":
            raise RouterError(f"{where}[{index}] RECON requires advisory authority")
        if phase == "CHALLENGE" and authority != "advisory":
            raise RouterError(f"{where}[{index}] CHALLENGE requires advisory authority")
        if phase == "VERIFY" and authority not in {"verification", "advisory"}:
            raise RouterError(f"{where}[{index}] VERIFY requires verification/advisory authority")
        if "tool_access" in instance:
            _expect_string_list(instance["tool_access"], f"{where}[{index}].tool_access", nonempty=True)
        effective_tools = instance.get("tool_access", role["tool_access"])
        if phase in {"RECON", "CHALLENGE", "VERIFY"}:
            forbidden = [tool for tool in effective_tools if tool not in {"prompt_only", "filesystem_read", "shell_read", "git_read"}]
            if forbidden:
                raise RouterError(f"{where}[{index}] phase {phase} has write-capable tools: {forbidden}")
    if writers > 1:
        raise RouterError(f"{where} has {writers} writers; at most one writer is allowed")


def parse_instance_specs(config: dict[str, Any], specs: list[str]) -> list[dict[str, Any]]:
    instances: list[dict[str, Any]] = []
    for spec in specs:
        if "=" in spec:
            instance_id, role_type = spec.split("=", 1)
        else:
            instance_id = role_type = spec
        instances.append({"instance_id": instance_id, "role_type": role_type})
    _validate_instances(config, instances, "custom instances")
    return instances


def role_available(role: dict[str, Any], *, run_healthcheck: bool = True) -> tuple[bool, str]:
    if not role["enabled"]:
        return False, "disabled"
    if role["runner"] != "interactive":
        return False, "not interactive"
    missing_env = [name for name in role["requires_env"] if not os.environ.get(name)]
    if missing_env:
        return False, f"missing env: {','.join(missing_env)}"
    executable = role["command"][0]
    if shutil.which(executable) is None:
        return False, f"missing executable: {executable}"
    if run_healthcheck and role.get("healthcheck"):
        try:
            result = subprocess.run(
                role["healthcheck"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"healthcheck failed: {exc}"
        if result.returncode != 0:
            return False, f"healthcheck exit={result.returncode}"
    return True, "available"


def select_lead(
    config: dict[str, Any],
    requested: str | None = None,
    *,
    allow_fallback: bool = False,
    run_healthcheck: bool = True,
) -> dict[str, Any]:
    candidates = list(config["lead"]["candidates"])
    if requested:
        if requested not in candidates:
            raise RouterError(f"requested lead is not a configured candidate: {requested}")
        candidates = [requested] + ([item for item in candidates if item != requested] if allow_fallback else [])
    failures: list[str] = []
    for role_type in candidates:
        role = config["roles"][role_type]
        available, reason = role_available(role, run_healthcheck=run_healthcheck)
        if available:
            command = list(role["command"])
            if role["provider"] == "openai":
                # CONTROL must reach the cmux Unix socket. The environment is
                # still allowlisted by run-interactive-agent.sh; other Codex
                # instances remain workspace-write or read-only.
                command += ["--sandbox", "danger-full-access", "--ask-for-approval", "never"]
            return {
                "instance_id": config["lead"]["instance_id"],
                "role_type": role_type,
                "display_rank": config["lead"]["display_rank"],
                "runner": role["runner"],
                "provider": role["provider"],
                "model": role["model"],
                "hook_source": role["hook_source"],
                "command": command,
                "command_shell": shlex.join(command),
                "executable": role["command"][0],
                "requires_env": list(role["requires_env"]),
                "ready_pattern": role["ready_pattern"],
                "resource_class": role["resource_class"],
                "authority": config["lead"]["authority"],
                "tool_access": list(config["lead"]["tool_access"]),
                "capabilities": list(config["lead"]["capabilities"]),
                "phase": config["lead"]["phase"],
            }
        failures.append(f"{role_type} ({reason})")
    raise RouterError(f"no lead provider available: {', '.join(failures)}")


def _materialize_instances(config: dict[str, Any], instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for instance in instances:
        role = config["roles"][instance["role_type"]]
        effective = copy.deepcopy(role)
        effective.update(instance)
        effective["role_type"] = instance["role_type"]
        effective["command"] = list(role.get("command", []))
        effective["command_shell"] = shlex.join(role.get("command", []))
        effective["executable"] = role.get("command", [""])[0] if role.get("command") else ""
        effective["requires_env"] = list(role["requires_env"])
        effective["ready_pattern"] = role.get("ready_pattern", "")
        effective["tool_access"] = list(instance.get("tool_access", role["tool_access"]))
        if effective["provider"] == "openai" and effective["runner"] == "interactive":
            sandbox = "workspace-write" if effective["authority"] == "write" else "read-only"
            effective["command"] = list(role["command"]) + [
                "--sandbox", sandbox, "--ask-for-approval", "never"
            ]
            effective["command_shell"] = shlex.join(effective["command"])
        result.append(effective)
    return sorted(result, key=lambda item: (item["display_rank"], item["instance_id"]))


def build_plan(
    config: dict[str, Any],
    *,
    preset_name: str | None = None,
    instance_specs: list[str] | None = None,
    lead_provider: str | None = None,
    allow_fallback: bool = False,
    no_lead: bool = False,
    run_healthcheck: bool = True,
) -> dict[str, Any]:
    specs = list(instance_specs or [])
    if preset_name and specs:
        raise RouterError("--preset cannot be combined with explicit instances")
    if specs:
        source = "custom"
        instances = parse_instance_specs(config, specs)
        include_lead = True
        preset_lead = None
    else:
        source = preset_name or config["defaults"]["preset"]
        if source not in config["presets"]:
            available = ", ".join(sorted(config["presets"]))
            raise RouterError(f"unknown preset: {source}; available: {available}")
        preset = config["presets"][source]
        instances = copy.deepcopy(preset["instances"])
        include_lead = preset["include_lead"]
        preset_lead = preset.get("lead_provider")

    lead = None
    if include_lead and not no_lead:
        lead = select_lead(
            config,
            lead_provider or preset_lead,
            allow_fallback=allow_fallback,
            run_healthcheck=run_healthcheck,
        )
    resolved = _materialize_instances(config, instances)
    warnings: list[str] = []
    local_count = sum(item["runner"] == "local" for item in resolved)
    heavy_count = sum(item["resource_class"] == "local_heavy" for item in resolved)
    if local_count > config["limits"]["max_parallel_local_workers"]:
        warnings.append(
            f"roster contains {local_count} local roles; never dispatch more than "
            f"{config['limits']['max_parallel_local_workers']} concurrently"
        )
    if heavy_count > config["limits"]["max_local_heavy_in_flight"]:
        warnings.append(
            f"roster contains {heavy_count} heavy local roles; dispatch them sequentially "
            f"(limit {config['limits']['max_local_heavy_in_flight']})"
        )
    return {
        "schema_version": config["schema_version"],
        "preset": source,
        "lead": lead,
        "instances": resolved,
        "limits": copy.deepcopy(config["limits"]),
        "warnings": warnings,
    }


def _records(plan: dict[str, Any]) -> str:
    lines = [US.join(["META", str(plan["schema_version"]), plan["preset"]])]
    if plan["lead"]:
        lead = plan["lead"]
        lines.append(
            US.join(
                [
                    "LEAD",
                    lead["instance_id"],
                    lead["role_type"],
                    str(lead["display_rank"]),
                    lead["runner"],
                    lead["command_shell"],
                    lead["executable"],
                    ",".join(lead["requires_env"]),
                    lead["resource_class"],
                    lead["authority"],
                    lead["ready_pattern"],
                    lead["phase"],
                    ",".join(lead["tool_access"]),
                    lead["provider"],
                    lead["hook_source"],
                    lead["model"],
                ]
            )
        )
    for instance in plan["instances"]:
        lines.append(
            US.join(
                [
                    "INSTANCE",
                    instance["instance_id"],
                    instance["role_type"],
                    str(instance["display_rank"]),
                    instance["runner"],
                    instance["command_shell"],
                    instance["executable"],
                    ",".join(instance["requires_env"]),
                    instance["resource_class"],
                    instance.get("model", ""),
                    instance["authority"],
                    instance["ready_pattern"],
                    instance["phase"],
                    ",".join(instance["tool_access"]),
                    instance["provider"],
                    instance.get("hook_source", ""),
                ]
            )
        )
    for warning in plan["warnings"]:
        lines.append(US.join(["WARNING", warning]))
    return "\n".join(lines)


def verify_layout(expected: list[str], tree: dict[str, Any]) -> list[str]:
    try:
        workspaces = tree["windows"][0]["workspaces"]
        workspace = workspaces[0]
        actual = [pane["surfaces"][0]["title"] for pane in workspace["panes"]]
    except (KeyError, IndexError, TypeError) as exc:
        raise RouterError(f"cannot read cmux tree JSON: {exc}") from exc
    if actual != expected:
        raise RouterError(f"cmux layout mismatch: expected {expected}, got {actual}")
    return actual


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--router", help="router path (default: FLEET_ROUTER_PATH or repository router)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate", help="validate the router")
    subparsers.add_parser("presets", help="list preset names")
    subparsers.add_parser("local-models", help="list enabled local models")

    role_field = subparsers.add_parser("role-field", help="print one scalar role field")
    role_field.add_argument("role_type")
    role_field.add_argument("field")

    defaults_field = subparsers.add_parser("defaults-field", help="print a defaults field")
    defaults_field.add_argument("field")

    limits_field = subparsers.add_parser("limits-field", help="print a limits field")
    limits_field.add_argument("field")

    plan = subparsers.add_parser("plan", help="resolve a preset or explicit role instances")
    plan.add_argument("--preset")
    plan.add_argument("--lead-provider")
    plan.add_argument("--allow-fallback", action="store_true")
    plan.add_argument("--no-lead", action="store_true")
    plan.add_argument("--skip-healthcheck", action="store_true")
    plan.add_argument("--format", choices=("json", "records"), default="json")
    plan.add_argument("instances", nargs="*")

    layout = subparsers.add_parser("verify-layout", help="verify cmux tree JSON from stdin")
    layout.add_argument("--expected", required=True, help="comma-separated pane titles")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = load_router(args.router)
        if args.command == "validate":
            print(f"router schema {config['schema_version']} valid")
        elif args.command == "presets":
            print("\n".join(sorted(config["presets"])))
        elif args.command == "local-models":
            models = sorted(
                {
                    role["model"]
                    for role in config["roles"].values()
                    if role["runner"] == "local" and role["enabled"]
                }
            )
            print("\n".join(models))
        elif args.command == "role-field":
            if args.role_type not in config["roles"]:
                raise RouterError(f"unknown role_type: {args.role_type}")
            role = config["roles"][args.role_type]
            if args.field not in role:
                raise RouterError(f"role {args.role_type} has no field: {args.field}")
            value = role[args.field]
            if isinstance(value, (dict, list)):
                print(json.dumps(value, separators=(",", ":")))
            elif isinstance(value, bool):
                print("true" if value else "false")
            else:
                print(value)
        elif args.command == "defaults-field":
            if args.field not in config["defaults"]:
                raise RouterError(f"defaults has no field: {args.field}")
            value = config["defaults"][args.field]
            if isinstance(value, list):
                print("\n".join(value))
            elif isinstance(value, dict):
                print(json.dumps(value, separators=(",", ":")))
            else:
                print(value)
        elif args.command == "limits-field":
            if args.field not in config["limits"]:
                raise RouterError(f"limits has no field: {args.field}")
            value = config["limits"][args.field]
            if isinstance(value, (list, dict)):
                print(json.dumps(value, separators=(",", ":")))
            else:
                print(value)
        elif args.command == "plan":
            resolved = build_plan(
                config,
                preset_name=args.preset,
                instance_specs=args.instances,
                lead_provider=args.lead_provider,
                allow_fallback=args.allow_fallback,
                no_lead=args.no_lead,
                run_healthcheck=not args.skip_healthcheck,
            )
            if args.format == "records":
                print(_records(resolved))
            else:
                print(json.dumps(resolved, indent=2, sort_keys=True))
        elif args.command == "verify-layout":
            expected = [item for item in args.expected.split(",") if item]
            actual = verify_layout(expected, json.load(sys.stdin))
            print("layout valid: " + ",".join(actual))
        return 0
    except RouterError as exc:
        print(f"router error: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"router error: invalid cmux tree JSON: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
