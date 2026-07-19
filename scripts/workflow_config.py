#!/usr/bin/env python3
"""Validate and deterministically compile Mission Control workflows."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable

import fleet_compiled
import fleet_json
import fleet_manifest
import fleet_providers
import fleet_usage
import router_config


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROUTER = ROOT / "orchestration" / "router.yaml"
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
RISK_LEVELS = {"low", "medium", "high", "unknown"}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "unknown": 3}
GATES = {"fdp2", "human_build_exit", "fdp3"}
WORM_CATEGORIES = {
    "regulated",
    "production",
    "money",
    "credentials",
    "private_data",
    "destructive",
}
ROOT_FIELDS = {
    "schema_version",
    "name",
    "description",
    "preset",
    "autonomy",
    "capabilities",
    "risk",
    "assurance",
    "audit",
    "archive",
    "limits",
}


class WorkflowError(ValueError):
    """A workflow violates the frozen policy-only contract."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return fleet_json.canonical_bytes(value)
    except fleet_json.FleetJSONError as exc:
        raise WorkflowError(f"value is not canonical JSON: {exc}") from exc


def sha256(value: Any) -> str:
    try:
        return fleet_json.sha256(value)
    except fleet_json.FleetJSONError as exc:
        raise WorkflowError(f"value is not canonical JSON: {exc}") from exc


def load_workflow(path: str | os.PathLike[str]) -> dict[str, Any]:
    workflow_path = Path(path)
    try:
        value = fleet_json.load(workflow_path)
    except fleet_json.FleetJSONError as exc:
        raise WorkflowError(f"cannot load workflow {workflow_path}: {exc}") from exc
    validate_workflow(value)
    return value


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowError(f"{where} must be an object")
    return value


def _keys(
    value: dict[str, Any],
    required: Iterable[str],
    where: str,
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - value.keys())
    unknown = sorted(value.keys() - allowed)
    if missing:
        raise WorkflowError(f"{where} missing fields: {', '.join(missing)}")
    if unknown:
        raise WorkflowError(f"{where} unknown fields: {', '.join(unknown)}")


def _identifier(value: Any, where: str, *, allow_none: bool = False) -> str:
    if allow_none and value == "none":
        return value
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise WorkflowError(f"{where} must match {IDENTIFIER.pattern}")
    return value


def _string(value: Any, where: str, *, max_length: int = 1000) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or any(char in value for char in ("\x00", "\r"))
    ):
        raise WorkflowError(f"{where} must be a non-empty safe string")
    return value


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise WorkflowError(f"{where} must be boolean")
    return value


def _integer(
    value: Any, where: str, *, minimum: int, maximum: int | None = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WorkflowError(f"{where} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise WorkflowError(f"{where} must be <= {maximum}")
    return value


def _names(value: Any, where: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not IDENTIFIER.fullmatch(item) for item in value
    ):
        raise WorkflowError(f"{where} must be a list of identifiers")
    if not allow_empty and not value:
        raise WorkflowError(f"{where} must not be empty")
    if len(value) != len(set(value)):
        raise WorkflowError(f"{where} contains duplicates")
    return value


def _enum(value: Any, choices: set[str], where: str) -> str:
    if value not in choices:
        raise WorkflowError(f"{where} must be one of {sorted(choices)}")
    return value


def validate_workflow(value: Any) -> None:
    workflow = _object(value, "workflow")
    _keys(workflow, ROOT_FIELDS, "workflow")
    if type(workflow["schema_version"]) is not int or workflow["schema_version"] != 1:
        raise WorkflowError("workflow.schema_version must be 1")
    _identifier(workflow["name"], "workflow.name")
    _string(workflow["description"], "workflow.description")
    _identifier(workflow["preset"], "workflow.preset")

    autonomy = _object(workflow["autonomy"], "workflow.autonomy")
    _keys(
        autonomy,
        {"owner", "allow_parallel", "allow_subdelegation", "max_delegation_depth"},
        "workflow.autonomy",
    )
    if autonomy["owner"] != "lead":
        raise WorkflowError("workflow.autonomy.owner must be lead")
    _boolean(autonomy["allow_parallel"], "workflow.autonomy.allow_parallel")
    _boolean(autonomy["allow_subdelegation"], "workflow.autonomy.allow_subdelegation")
    depth = _integer(
        autonomy["max_delegation_depth"],
        "workflow.autonomy.max_delegation_depth",
        minimum=0,
        maximum=8,
    )
    if not autonomy["allow_subdelegation"] and depth != 0:
        raise WorkflowError(
            "max_delegation_depth must be 0 when subdelegation is disabled"
        )

    capabilities = _object(workflow["capabilities"], "workflow.capabilities")
    _keys(
        capabilities,
        {"available", "required_outcomes", "writer"},
        "workflow.capabilities",
    )
    _names(capabilities["available"], "workflow.capabilities.available")
    _names(capabilities["required_outcomes"], "workflow.capabilities.required_outcomes")
    _identifier(capabilities["writer"], "workflow.capabilities.writer", allow_none=True)

    risk = _object(workflow["risk"], "workflow.risk")
    _keys(risk, {"minimum", "allow_lead_escalation", "high_action"}, "workflow.risk")
    _enum(risk["minimum"], RISK_LEVELS, "workflow.risk.minimum")
    _boolean(risk["allow_lead_escalation"], "workflow.risk.allow_lead_escalation")
    _enum(risk["high_action"], {"confirm_assured", "fail"}, "workflow.risk.high_action")

    assurance = _object(workflow["assurance"], "workflow.assurance")
    _keys(assurance, {"profile", "preset", "minimum_gates"}, "workflow.assurance")
    profile = _enum(
        assurance["profile"],
        {"none", "proportional", "assured"},
        "workflow.assurance.profile",
    )
    _identifier(assurance["preset"], "workflow.assurance.preset")
    gates = _names(
        assurance["minimum_gates"], "workflow.assurance.minimum_gates", allow_empty=True
    )
    unknown_gates = sorted(set(gates) - GATES)
    if unknown_gates:
        raise WorkflowError(
            f"workflow.assurance.minimum_gates unknown values: {', '.join(unknown_gates)}"
        )
    if profile == "assured" and set(gates) != GATES:
        raise WorkflowError(
            "assured workflows require fdp2, human_build_exit, and fdp3"
        )
    if profile == "none" and gates:
        raise WorkflowError("assurance profile none cannot declare gates")

    audit = _object(workflow["audit"], "workflow.audit")
    _keys(audit, {"mode", "trust_scope", "worm_required_for"}, "workflow.audit")
    audit_mode = _enum(audit["mode"], {"signed", "worm"}, "workflow.audit.mode")
    trust_scope = _enum(
        audit["trust_scope"],
        {"local-development", "external-compliance"},
        "workflow.audit.trust_scope",
    )
    worm_for = _names(
        audit["worm_required_for"], "workflow.audit.worm_required_for", allow_empty=True
    )
    unknown_categories = sorted(set(worm_for) - WORM_CATEGORIES)
    if unknown_categories:
        raise WorkflowError(
            f"workflow.audit.worm_required_for unknown values: {', '.join(unknown_categories)}"
        )
    if audit_mode == "signed" and trust_scope != "local-development":
        raise WorkflowError("signed audit cannot claim external-compliance trust")
    if workflow["name"] == "regulated" and (
        audit_mode != "worm" or trust_scope != "external-compliance"
    ):
        raise WorkflowError(
            "regulated workflow requires worm mode and external-compliance trust"
        )
    if (
        "regulated" in worm_for
        and audit_mode == "worm"
        and trust_scope != "external-compliance"
    ):
        raise WorkflowError(
            "regulated WORM requirements need external-compliance trust"
        )

    archive = _object(workflow["archive"], "workflow.archive")
    _keys(
        archive,
        {"mode", "content_policy", "include_final_tree", "include_git_delta"},
        "workflow.archive",
    )
    if archive["mode"] != "incremental":
        raise WorkflowError("workflow.archive.mode must be incremental")
    _enum(
        archive["content_policy"],
        {"full", "redacted", "hash-only"},
        "workflow.archive.content_policy",
    )
    _boolean(archive["include_final_tree"], "workflow.archive.include_final_tree")
    _boolean(archive["include_git_delta"], "workflow.archive.include_git_delta")

    limits = _object(workflow["limits"], "workflow.limits")
    _keys(
        limits,
        {
            "deadline_seconds",
            "token_budget",
            "budget_mode",
            "delegation_credits",
            "max_active_delegations",
        },
        "workflow.limits",
    )
    _integer(
        limits["deadline_seconds"],
        "workflow.limits.deadline_seconds",
        minimum=60,
        maximum=604800,
    )
    _integer(limits["token_budget"], "workflow.limits.token_budget", minimum=0)
    _enum(limits["budget_mode"], {"soft", "hard"}, "workflow.limits.budget_mode")
    _integer(
        limits["delegation_credits"],
        "workflow.limits.delegation_credits",
        minimum=1,
        maximum=10000,
    )
    _integer(
        limits["max_active_delegations"],
        "workflow.limits.max_active_delegations",
        minimum=1,
        maximum=256,
    )


def _abstract_capabilities(plan: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    all_members = ([plan["lead"]] if plan.get("lead") else []) + list(plan["instances"])
    for member in all_members:
        result.update(member.get("capabilities", []))
        phase = member.get("phase")
        if phase == "RECON":
            result.add("recon")
        elif phase == "CHALLENGE":
            result.add("challenge")
        elif phase == "VERIFY":
            result.add("verify")
        if member.get("authority") == "write":
            result.add("build")
    return result


def _public_member(member: dict[str, Any]) -> dict[str, Any]:
    """Freeze the execution identity and authority used by a runtime roster."""
    hook_source = str(member.get("hook_source", ""))
    provider_adapter = fleet_providers.DEFAULT_REGISTRY.resolve(
        hook_source=hook_source, provider=str(member["provider"])
    ).name
    return {
        "instance_id": member["instance_id"],
        "role_type": member["role_type"],
        "runner": member["runner"],
        "phase": member["phase"],
        "authority": member["authority"],
        "provider": member["provider"],
        "provider_adapter": provider_adapter,
        "hook_source": hook_source,
        "model": member["model"],
        "variant": member.get("variant"),
        "capabilities": sorted(member.get("capabilities", [])),
    }


def compile_workflow(
    workflow: dict[str, Any],
    *,
    router: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        normalized = fleet_json.loads(fleet_json.canonical_bytes(workflow))
    except fleet_json.FleetJSONError as exc:
        raise WorkflowError(f"workflow is not canonical JSON: {exc}") from exc
    validate_workflow(normalized)

    try:
        router_source = (
            router if router is not None else router_config.load_router(DEFAULT_ROUTER)
        )
        # Freeze canonical bytes once. Each resolver gets a fresh detached view,
        # so neither the caller nor one resolver can mutate the other plan.
        router_snapshot = fleet_json.canonical_bytes(router_source)
        router_value = fleet_json.loads(router_snapshot)
        plan = router_config.build_plan(
            fleet_json.loads(router_snapshot),
            preset_name=normalized["preset"],
            run_healthcheck=False,
            check_runtime_availability=False,
        )
        assurance_plan = router_config.build_plan(
            fleet_json.loads(router_snapshot),
            preset_name=normalized["assurance"]["preset"],
            run_healthcheck=False,
            check_runtime_availability=False,
        )
    except router_config.RouterError as exc:
        raise WorkflowError(f"router resolution failed: {exc}") from exc
    except fleet_json.FleetJSONError as exc:
        raise WorkflowError(f"router is not canonical JSON: {exc}") from exc

    resolved_capabilities = _abstract_capabilities(plan)
    missing = sorted(
        set(normalized["capabilities"]["available"]) - resolved_capabilities
    )
    if missing:
        raise WorkflowError(
            "workflow capabilities are not provided by preset "
            f"{normalized['preset']}: {', '.join(missing)}"
        )
    writers = [
        member["instance_id"]
        for member in plan["instances"]
        if member.get("authority") == "write"
    ]
    writer_contract = normalized["capabilities"]["writer"]
    if writer_contract == "none" and writers:
        raise WorkflowError(
            "workflow declares writer=none but preset contains a writer"
        )
    if writer_contract != "none" and len(writers) != 1:
        raise WorkflowError("workflow requires exactly one resolved writer")
    if (
        normalized["assurance"]["profile"] != "none"
        and assurance_plan["mode"] != "assured"
    ):
        raise WorkflowError("assurance preset must resolve to mode=assured")

    # A token ceiling is only a real policy when every provider that this
    # workflow may launch can account for/enforce it.  Validate the complete
    # main + assurance provider set while the resolved plans are still local;
    # accepting a hard policy here and discovering an incapable provider after
    # boot would turn the compiled contract into a false guarantee.
    usage_providers = sorted(
        {
            fleet_providers.DEFAULT_REGISTRY.resolve(
                hook_source=str(member.get("hook_source") or ""),
                provider=str(member["provider"]),
            ).name
            for selected_plan in (plan, assurance_plan)
            for member in (
                ([selected_plan["lead"]] if selected_plan.get("lead") else [])
                + list(selected_plan["instances"])
            )
        }
    )
    try:
        fleet_usage.validate_policy(
            {
                "budget_mode": normalized["limits"]["budget_mode"],
                "token_budget": normalized["limits"]["token_budget"],
            },
            providers=usage_providers,
        )
    except fleet_usage.UsageError as exc:
        raise WorkflowError(f"workflow token budget is not enforceable: {exc}") from exc

    result = {
        "schema_version": 2,
        "workflow": normalized,
        "workflow_digest": sha256(normalized),
        "router_digest": sha256(router_value),
        "router_snapshot": router_value,
        "resolved": {
            "preset": plan["preset"],
            "mode": plan["mode"],
            "launch_digest": fleet_manifest.launch_digest(plan),
            "identity_groups": plan["identity_groups"],
            "lead": _public_member(plan["lead"]) if plan.get("lead") else None,
            "instances": [_public_member(item) for item in plan["instances"]],
            "available_capabilities": sorted(resolved_capabilities),
            "writer_instance": writers[0] if writers else None,
            "assurance_preset": assurance_plan["preset"],
            "assurance_mode": assurance_plan["mode"],
            "assurance_launch_digest": fleet_manifest.launch_digest(assurance_plan),
            "assurance_identity_groups": assurance_plan["identity_groups"],
            "assurance_lead": (
                _public_member(assurance_plan["lead"])
                if assurance_plan.get("lead")
                else None
            ),
            "assurance_instances": [
                _public_member(item) for item in assurance_plan["instances"]
            ],
        },
    }
    result["compiled_digest"] = sha256(result)
    # Compilation already resolved both plans from isolated copies and admitted
    # the provider-aware budget. A read-mode pass still closes the emitted
    # schema and checksum envelope without resolving those plans a second time;
    # effect consumers perform the strict snapshot-to-plan proof at their trust
    # boundary.
    try:
        return fleet_compiled.validate(result, mode="read")
    except fleet_compiled.CompiledError as exc:
        raise WorkflowError(f"compiled workflow contract failed: {exc}") from exc


def compile_path(
    path: str | os.PathLike[str],
    *,
    router_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    workflow = load_workflow(path)
    try:
        router = router_config.load_router(router_path or DEFAULT_ROUTER)
    except router_config.RouterError as exc:
        raise WorkflowError(str(exc)) from exc
    return compile_workflow(workflow, router=router)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--router", default=str(DEFAULT_ROUTER))
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate one or more workflows")
    validate.add_argument("paths", nargs="+")
    show = commands.add_parser("show", help="show normalized workflow JSON")
    show.add_argument("path")
    compile_command = commands.add_parser(
        "compile", help="compile canonical policy JSON"
    )
    compile_command.add_argument("path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            for path in args.paths:
                # Schema validation is intentionally portable and static. It
                # does not require the machine's router/provider availability;
                # `compile` is the effect-admission command.
                load_workflow(path)
                print(f"workflow valid: {path}")
        elif args.command == "show":
            workflow = load_workflow(args.path)
            print(json.dumps(workflow, indent=2, ensure_ascii=False, sort_keys=True))
        elif args.command == "compile":
            compiled = compile_path(args.path, router_path=args.router)
            sys.stdout.buffer.write(canonical_bytes(compiled) + b"\n")
        return 0
    except WorkflowError as exc:
        print(f"workflow error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
