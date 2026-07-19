#!/usr/bin/env python3
"""Strict, versioned loader for immutable compiled Fleet workflows."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable, Literal

import fleet_json
import fleet_providers


LoadMode = Literal["read", "effect"]
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
EXECUTION_MODES = {"autonomous", "guided", "assured"}
RUNNERS = {"interactive", "local", "api"}
PHASES = {"CONTROL", "RECON", "BUILD", "CHALLENGE", "VERIFY"}
AUTHORITIES = {"control", "advisory", "write", "verification"}
TOP_FIELDS_V1 = {
    "schema_version",
    "workflow",
    "workflow_digest",
    "router_digest",
    "resolved",
    "compiled_digest",
}
TOP_FIELDS_V2 = TOP_FIELDS_V1 | {"router_snapshot"}
MEMBER_FIELDS_V1 = {
    "instance_id",
    "role_type",
    "phase",
    "authority",
    "provider",
    "provider_adapter",
    "hook_source",
    "model",
    "variant",
    "capabilities",
}
MEMBER_FIELDS_V2 = MEMBER_FIELDS_V1 | {"runner"}
RESOLVED_FIELDS_V1 = {
    "preset",
    "mode",
    "identity_groups",
    "launch_digest",
    "lead",
    "instances",
    "available_capabilities",
    "writer_instance",
    "assurance_preset",
    "assurance_mode",
    "assurance_launch_digest",
    "assurance_identity_groups",
    "assurance_lead",
    "assurance_instances",
}
RESOLVED_REQUIRED_V1 = {
    "preset",
    "mode",
    "lead",
    "instances",
    "available_capabilities",
    "writer_instance",
    "assurance_preset",
}
RESOLVED_FIELDS_V2 = RESOLVED_FIELDS_V1


class CompiledError(ValueError):
    """A compiled workflow is malformed, drifted, or unsafe for its mode."""


def _keys(
    value: dict[str, Any],
    required: Iterable[str],
    where: str,
    *,
    optional: Iterable[str] = (),
) -> None:
    if any(type(key) is not str for key in value):
        raise CompiledError(f"{where} object keys must be strings")
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - value.keys())
    unknown = sorted(value.keys() - allowed)
    if missing:
        raise CompiledError(f"{where} missing fields: {', '.join(missing)}")
    if unknown:
        raise CompiledError(f"{where} unknown fields: {', '.join(unknown)}")


def _object(value: Any, where: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise CompiledError(f"{where} must be an object")
    return value


def _text(value: Any, where: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not SAFE_TEXT.fullmatch(value)):
        raise CompiledError(f"{where} must be a safe string")
    if allow_empty and value and not SAFE_TEXT.fullmatch(value):
        raise CompiledError(f"{where} must be a safe string")
    return value


def _digest(value: Any, where: str) -> str:
    if type(value) is not str or not SHA256.fullmatch(value):
        raise CompiledError(f"{where} must be a lowercase SHA-256 digest")
    return value


def _string_list(value: Any, where: str, *, require_sorted: bool = True) -> list[str]:
    if type(value) is not list:
        raise CompiledError(f"{where} must be a string list")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_text(item, f"{where}[{index}]"))
    if len(result) != len(set(result)):
        raise CompiledError(f"{where} contains duplicates")
    if require_sorted and result != sorted(result):
        raise CompiledError(f"{where} must be sorted")
    return result


def _member(value: Any, where: str, *, version: int) -> dict[str, Any]:
    member = _object(value, where)
    if version == 2:
        _keys(member, MEMBER_FIELDS_V2, where)
    else:
        _keys(
            member,
            MEMBER_FIELDS_V1,
            where,
            optional=MEMBER_FIELDS_V2 - MEMBER_FIELDS_V1,
        )
    for field in (
        "instance_id",
        "role_type",
        "phase",
        "authority",
        "provider",
        "provider_adapter",
        "model",
    ):
        _text(member[field], f"{where}.{field}")
    _text(member["hook_source"], f"{where}.hook_source", allow_empty=True)
    if "runner" in member:
        _text(member["runner"], f"{where}.runner")
        if version == 2 and member["runner"] not in RUNNERS:
            raise CompiledError(f"{where}.runner is invalid")
    if version == 2 and member["phase"] not in PHASES:
        raise CompiledError(f"{where}.phase is invalid")
    if version == 2 and member["authority"] not in AUTHORITIES:
        raise CompiledError(f"{where}.authority is invalid")
    variant = member["variant"]
    if variant is not None:
        _text(variant, f"{where}.variant")
    _string_list(member["capabilities"], f"{where}.capabilities")
    if version == 2:
        try:
            adapter = fleet_providers.DEFAULT_REGISTRY.resolve(
                hook_source=member["hook_source"], provider=member["provider"]
            ).name
        except fleet_providers.ProviderError as exc:
            raise CompiledError(
                f"{where} has an invalid provider binding: {exc}"
            ) from exc
        if member["provider_adapter"] != adapter:
            raise CompiledError(f"{where}.provider_adapter binding mismatch")
    return member


def _roster(
    lead_value: Any,
    instances_value: Any,
    where: str,
    *,
    version: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    lead = (
        None
        if lead_value is None
        else _member(lead_value, f"{where}.lead", version=version)
    )
    if type(instances_value) is not list:
        raise CompiledError(f"{where}.instances must be a list")
    instances = [
        _member(item, f"{where}.instances[{index}]", version=version)
        for index, item in enumerate(instances_value)
    ]
    identifiers = ([lead["instance_id"]] if lead is not None else []) + [
        member["instance_id"] for member in instances
    ]
    if len(identifiers) != len(set(identifiers)):
        raise CompiledError(f"{where} contains duplicate instance_id values")
    return lead, instances


def _groups(value: Any, instance_ids: set[str] | None, where: str) -> list[list[str]]:
    if type(value) is not list:
        raise CompiledError(f"{where} must be a list of identity groups")
    result: list[list[str]] = []
    grouped: set[str] = set()
    for group_index, raw_group in enumerate(value):
        if type(raw_group) is not list or not raw_group:
            raise CompiledError(f"{where}[{group_index}] must be a non-empty list")
        group = [
            _text(item, f"{where}[{group_index}][{item_index}]")
            for item_index, item in enumerate(raw_group)
        ]
        if len(group) != len(set(group)):
            raise CompiledError(f"{where}[{group_index}] contains duplicates")
        if instance_ids is not None:
            unknown = sorted(set(group) - instance_ids)
            if unknown:
                raise CompiledError(
                    f"{where}[{group_index}] references unknown instances: "
                    + ", ".join(unknown)
                )
        overlap = sorted(grouped & set(group))
        if overlap:
            raise CompiledError(
                f"{where} repeats instances across groups: {', '.join(overlap)}"
            )
        grouped.update(group)
        result.append(group)
    return result


def _abstract_capabilities(
    lead: dict[str, Any] | None, instances: list[dict[str, Any]]
) -> list[str]:
    result: set[str] = set()
    for member in ([lead] if lead is not None else []) + instances:
        result.update(member["capabilities"])
        phase = member["phase"]
        if phase == "RECON":
            result.add("recon")
        elif phase == "CHALLENGE":
            result.add("challenge")
        elif phase == "VERIFY":
            result.add("verify")
        if member["authority"] == "write":
            result.add("build")
    return sorted(result)


def _legacy_workflow(value: Any) -> dict[str, Any]:
    workflow = _object(value, "compiled.workflow")
    if (
        type(workflow.get("schema_version")) is not int
        or workflow["schema_version"] != 1
    ):
        raise CompiledError("compiled.workflow.schema_version must be 1")
    _text(workflow.get("preset"), "compiled.workflow.preset")
    risk = _object(workflow.get("risk"), "compiled.workflow.risk")
    if risk.get("minimum") not in {"low", "medium", "high", "unknown"}:
        raise CompiledError("compiled.workflow.risk.minimum is invalid")
    assurance = _object(workflow.get("assurance"), "compiled.workflow.assurance")
    _text(assurance.get("preset"), "compiled.workflow.assurance.preset")
    capabilities = _object(
        workflow.get("capabilities"), "compiled.workflow.capabilities"
    )
    _string_list(
        capabilities.get("available"),
        "compiled.workflow.capabilities.available",
        require_sorted=False,
    )
    return workflow


def _validate_v1(value: dict[str, Any]) -> None:
    """Validate known v1 envelopes without inventing fields in old archives."""

    workflow = _legacy_workflow(value["workflow"])
    resolved = _object(value["resolved"], "compiled.resolved")
    _keys(
        resolved,
        RESOLVED_REQUIRED_V1,
        "compiled.resolved",
        optional=RESOLVED_FIELDS_V1 - RESOLVED_REQUIRED_V1,
    )
    preset = _text(resolved["preset"], "compiled.resolved.preset")
    mode = _text(resolved["mode"], "compiled.resolved.mode")
    if mode not in EXECUTION_MODES:
        raise CompiledError("compiled.resolved.mode is invalid")
    assurance_preset = _text(
        resolved["assurance_preset"], "compiled.resolved.assurance_preset"
    )
    if preset != workflow["preset"]:
        raise CompiledError("compiled.resolved.preset binding mismatch")
    if assurance_preset != workflow["assurance"]["preset"]:
        raise CompiledError("compiled.resolved.assurance_preset binding mismatch")
    _, instances = _roster(
        resolved["lead"], resolved["instances"], "compiled.resolved", version=1
    )
    available = _string_list(
        resolved["available_capabilities"],
        "compiled.resolved.available_capabilities",
    )
    if not set(workflow["capabilities"]["available"]) <= set(available):
        raise CompiledError("compiled workflow capability binding mismatch")
    writer = resolved["writer_instance"]
    if writer is not None:
        _text(writer, "compiled.resolved.writer_instance")
        if writer not in {member["instance_id"] for member in instances}:
            raise CompiledError("compiled.resolved.writer_instance binding mismatch")
    if "identity_groups" in resolved:
        _groups(
            resolved["identity_groups"],
            {member["instance_id"] for member in instances},
            "compiled.resolved.identity_groups",
        )
    if "launch_digest" in resolved:
        _digest(resolved["launch_digest"], "compiled.resolved.launch_digest")
    assurance_roster_fields = {
        "assurance_mode",
        "assurance_lead",
        "assurance_instances",
    }
    present = assurance_roster_fields & resolved.keys()
    if present and present != assurance_roster_fields:
        missing = sorted(assurance_roster_fields - present)
        raise CompiledError(
            "compiled.resolved historical assurance roster is incomplete: "
            + ", ".join(missing)
        )
    if present:
        assurance_mode = _text(
            resolved["assurance_mode"], "compiled.resolved.assurance_mode"
        )
        if assurance_mode not in EXECUTION_MODES:
            raise CompiledError("compiled.resolved.assurance_mode is invalid")
        _, assurance_instances = _roster(
            resolved["assurance_lead"],
            resolved["assurance_instances"],
            "compiled.resolved.assurance",
            version=1,
        )
        if "assurance_identity_groups" in resolved:
            _groups(
                resolved["assurance_identity_groups"],
                {member["instance_id"] for member in assurance_instances},
                "compiled.resolved.assurance_identity_groups",
            )
    elif "assurance_identity_groups" in resolved:
        # An intermediate historical v1 froze group identities but did not yet
        # publish the assurance roster needed to bind those names exactly.
        _groups(
            resolved["assurance_identity_groups"],
            None,
            "compiled.resolved.assurance_identity_groups",
        )
    if "assurance_launch_digest" in resolved:
        _digest(
            resolved["assurance_launch_digest"],
            "compiled.resolved.assurance_launch_digest",
        )


def _validate_v2(value: dict[str, Any], *, effectful: bool) -> None:
    # Import lazily so workflow_config can use this validator on its own output.
    import workflow_config

    router = _object(value["router_snapshot"], "compiled.router_snapshot")
    try:
        router_digest = fleet_json.sha256(router)
    except fleet_json.FleetJSONError as exc:
        raise CompiledError(
            f"compiled.router_snapshot is not canonical JSON: {exc}"
        ) from exc
    if router_digest != value["router_digest"]:
        raise CompiledError("compiled router_snapshot digest mismatch")
    try:
        workflow_config.router_config.validate_router(router)
    except workflow_config.router_config.RouterError as exc:
        raise CompiledError(f"compiled.router_snapshot is invalid: {exc}") from exc

    raw_workflow = value["workflow"]
    raw_risk = raw_workflow.get("risk") if isinstance(raw_workflow, dict) else None
    if not isinstance(raw_risk, dict) or raw_risk.get("minimum") not in {
        "low",
        "medium",
        "high",
        "unknown",
    }:
        raise CompiledError("compiled.workflow.risk.minimum is invalid")
    try:
        workflow_config.validate_workflow(raw_workflow)
    except workflow_config.WorkflowError as exc:
        raise CompiledError(f"compiled.workflow is invalid: {exc}") from exc
    workflow = raw_workflow
    resolved = _object(value["resolved"], "compiled.resolved")
    _keys(resolved, RESOLVED_FIELDS_V2, "compiled.resolved")

    preset = _text(resolved["preset"], "compiled.resolved.preset")
    mode = _text(resolved["mode"], "compiled.resolved.mode")
    assurance_preset = _text(
        resolved["assurance_preset"], "compiled.resolved.assurance_preset"
    )
    assurance_mode = _text(
        resolved["assurance_mode"], "compiled.resolved.assurance_mode"
    )
    if mode not in EXECUTION_MODES:
        raise CompiledError("compiled.resolved.mode is invalid")
    if assurance_mode not in EXECUTION_MODES:
        raise CompiledError("compiled.resolved.assurance_mode is invalid")
    if preset != workflow["preset"]:
        raise CompiledError("compiled.resolved.preset binding mismatch")
    if assurance_preset != workflow["assurance"]["preset"]:
        raise CompiledError("compiled.resolved.assurance_preset binding mismatch")
    if workflow["assurance"]["profile"] != "none" and assurance_mode != "assured":
        raise CompiledError("compiled assurance mode binding mismatch")
    lead, instances = _roster(
        resolved["lead"], resolved["instances"], "compiled.resolved", version=2
    )
    assurance_lead, assurance_instances = _roster(
        resolved["assurance_lead"],
        resolved["assurance_instances"],
        "compiled.resolved.assurance",
        version=2,
    )
    _groups(
        resolved["identity_groups"],
        {member["instance_id"] for member in instances},
        "compiled.resolved.identity_groups",
    )
    _groups(
        resolved["assurance_identity_groups"],
        {member["instance_id"] for member in assurance_instances},
        "compiled.resolved.assurance_identity_groups",
    )
    _digest(resolved["launch_digest"], "compiled.resolved.launch_digest")
    _digest(
        resolved["assurance_launch_digest"],
        "compiled.resolved.assurance_launch_digest",
    )

    available = _string_list(
        resolved["available_capabilities"],
        "compiled.resolved.available_capabilities",
    )
    if available != _abstract_capabilities(lead, instances):
        raise CompiledError("compiled.resolved.available_capabilities binding mismatch")
    if not set(workflow["capabilities"]["available"]) <= set(available):
        raise CompiledError("compiled workflow capability binding mismatch")

    writers = [
        member["instance_id"] for member in instances if member["authority"] == "write"
    ]
    writer_contract = workflow["capabilities"]["writer"]
    expected_writer: str | None
    if writer_contract == "none":
        if writers:
            raise CompiledError("compiled writer=none binding mismatch")
        expected_writer = None
    else:
        if len(writers) != 1:
            raise CompiledError("compiled workflow requires exactly one writer")
        expected_writer = writers[0]
    if resolved["writer_instance"] != expected_writer:
        raise CompiledError("compiled.resolved.writer_instance binding mismatch")

    if not effectful:
        return

    # Checksums alone cannot prove that the published rosters and launch
    # digests were actually derived from the embedded router. Re-resolve both
    # plans from fresh detached copies so a mutable resolver can never leak
    # state from the main plan into the assurance plan (or vice versa).
    try:
        router_bytes = fleet_json.canonical_bytes(router)
        for selected_preset in (preset, assurance_preset):
            plan_router = fleet_json.loads(router_bytes)
            binding_router = fleet_json.loads(router_bytes)
            plan = workflow_config.router_config.build_plan(
                plan_router,
                preset_name=selected_preset,
                run_healthcheck=False,
                check_runtime_availability=False,
            )
            workflow_config.fleet_manifest.bind_plan(binding_router, plan, value)
    except (
        fleet_json.FleetJSONError,
        workflow_config.router_config.RouterError,
        workflow_config.fleet_manifest.ManifestError,
    ) as exc:
        raise CompiledError(
            f"compiled router snapshot plan binding failed: {exc}"
        ) from exc

    # A resealed artifact must not turn an unenforceable hard ceiling into an
    # admitted effect. The provider set includes every member that either the
    # primary or assurance roster can launch.
    providers = sorted(
        {
            member["provider_adapter"]
            for member in (
                ([lead] if lead is not None else [])
                + instances
                + ([assurance_lead] if assurance_lead is not None else [])
                + assurance_instances
            )
        }
    )
    try:
        workflow_config.fleet_usage.validate_policy(
            {
                "budget_mode": workflow["limits"]["budget_mode"],
                "token_budget": workflow["limits"]["token_budget"],
            },
            providers=providers,
        )
    except workflow_config.fleet_usage.UsageError as exc:
        raise CompiledError(
            f"compiled workflow token budget is not enforceable: {exc}"
        ) from exc



def _mode(value: str) -> LoadMode:
    if value == "read":
        return "read"
    if value == "effect":
        return "effect"
    raise CompiledError("compiled load mode must be 'read' or 'effect'")


def validate(value: Any, *, mode: LoadMode = "read") -> dict[str, Any]:
    """Validate one parsed compiled workflow for historical or effectful use."""

    selected_mode = _mode(mode)
    compiled = _object(value, "compiled")
    version = compiled.get("schema_version")
    if type(version) is not int or version not in {1, 2}:
        raise CompiledError("compiled.schema_version must be 1 or 2")
    _keys(
        compiled,
        TOP_FIELDS_V1 if version == 1 else TOP_FIELDS_V2,
        "compiled",
    )
    _digest(compiled["workflow_digest"], "compiled.workflow_digest")
    _digest(compiled["router_digest"], "compiled.router_digest")
    _digest(compiled["compiled_digest"], "compiled.compiled_digest")
    try:
        workflow_digest = fleet_json.sha256(compiled["workflow"])
        unsigned = {
            key: item for key, item in compiled.items() if key != "compiled_digest"
        }
        compiled_digest = fleet_json.sha256(unsigned)
    except fleet_json.FleetJSONError as exc:
        raise CompiledError(f"compiled workflow is not canonical JSON: {exc}") from exc
    if workflow_digest != compiled["workflow_digest"]:
        raise CompiledError("compiled workflow_digest mismatch")
    if compiled_digest != compiled["compiled_digest"]:
        raise CompiledError("compiled compiled_digest mismatch")
    if version == 1:
        _validate_v1(compiled)
        if selected_mode == "effect":
            raise CompiledError(
                "compiled schema_version=1 is historical read-only; effects require v2"
            )
    else:
        _validate_v2(compiled, effectful=selected_mode == "effect")
    return compiled


def loads(
    raw: bytes | bytearray | memoryview | str, *, mode: LoadMode = "read"
) -> dict[str, Any]:
    """Strictly parse and validate one compiled workflow JSON value."""

    selected_mode = _mode(mode)
    try:
        value = fleet_json.loads(raw)
    except fleet_json.FleetJSONError as exc:
        raise CompiledError(f"cannot parse compiled workflow: {exc}") from exc
    return validate(value, mode=selected_mode)


def load(path: str | Path, *, mode: LoadMode = "read") -> dict[str, Any]:
    """Load a compiled workflow; effect mode rejects every historical v1 plan."""

    selected_mode = _mode(mode)
    try:
        value = fleet_json.load(path)
    except fleet_json.FleetJSONError as exc:
        raise CompiledError(
            f"cannot load compiled workflow {Path(path)}: {exc}"
        ) from exc
    return validate(value, mode=selected_mode)
