#!/usr/bin/env python3
"""Validate and deterministically compile Mission Control workflows."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from fleet_workflow_contract import (
    WorkflowError, IDENTIFIER, RISK_LEVELS, RISK_ORDER, GATES, WORM_CATEGORIES,
    ROOT_FIELDS, canonical_bytes, sha256, validate_workflow,
    _object, _keys, _identifier, _string, _boolean, _integer, _names, _enum,
)

import fleet_compiled
import fleet_json
import fleet_manifest
import fleet_providers
import fleet_usage
import router_config


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROUTER = ROOT / "orchestration" / "router.yaml"


def load_workflow(path: str | os.PathLike[str]) -> dict[str, Any]:
    workflow_path = Path(path)
    try:
        value = fleet_json.load(workflow_path)
    except fleet_json.FleetJSONError as exc:
        raise WorkflowError(f"cannot load workflow {workflow_path}: {exc}") from exc
    validate_workflow(value)
    return value


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


def catalog(
    directory: str | os.PathLike[str] = ROOT / "workflows",
    *,
    router_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Report static compilation availability without admitting a run.

    Invalid workflow schemas remain errors. A valid policy whose effects cannot
    be compiled remains visible with the compiler's reason; it is never silently
    omitted or advertised as runtime-ready.
    """
    paths = sorted(Path(directory).glob("*.yaml"))
    if not paths:
        raise WorkflowError(f"no workflows found in {directory}")
    try:
        router = router_config.load_router(router_path or DEFAULT_ROUTER)
    except router_config.RouterError as exc:
        raise WorkflowError(str(exc)) from exc
    entries = []
    for path in paths:
        workflow = load_workflow(path)
        entry = {"name": workflow["name"], "path": str(path), "schema_valid": True}
        try:
            compiled = compile_workflow(workflow, router=router)
        except WorkflowError as exc:
            entry.update(effect_compilation="blocked", reason=str(exc))
        else:
            entry.update(effect_compilation="available", compiled_digest=compiled["compiled_digest"])
        entries.append(entry)
    return {"schema_version": 1, "runtime_checked": False, "workflows": entries}


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
    catalog_command = commands.add_parser(
        "catalog", help="show compilation availability; does not check or launch runtimes"
    )
    catalog_command.add_argument("--directory", default=str(ROOT / "workflows"))
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
        elif args.command == "catalog":
            value = catalog(args.directory, router_path=args.router)
            print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except WorkflowError as exc:
        print(f"workflow error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
