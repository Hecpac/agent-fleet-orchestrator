#!/usr/bin/env python3
"""Run and resume the canonical durable Mission Control entry point."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Any, Mapping
import uuid

# Some imports are unused here since S3: tests and fleet_personal_pool address these
# modules as `mission_run.<module>`, so they remain part of this entry point's surface.
import fleet_admission
import fleet_acceptance
import fleet_functional
import fleet_herdr_scope
import fleet_herdr_work_packet
import fleet_herdr_control
import fleet_artifacts
import fleet_archive
import fleet_audit_client
import fleet_assured_runner
import fleet_control
import fleet_control_service
import fleet_json
import fleet_herdr
import fleet_herdr_launch
import fleet_herdr_profile
import fleet_herdr_runtime
import fleet_herdr_archive
import fleet_herdr_mission
import fleet_herdr_versions
import fleet_ledger
import fleet_manifest
import fleet_mission
import fleet_mission_state as mission_state
import fleet_providers
import fleet_safe_paths
import fleet_tracking
import workflow_config
# Neutral helpers shared by the modern composition root and the legacy driver.
import fleet_mission_run_support as mission_support
from fleet_mission_run_support import (
    MissionRunError, parse_manifest, verify_manifest_binding,
    verify_manifest_repository_binding, load_durable_json, load_mission_text,
    git_read, exact_git_toplevel,
)
from fleet_mission_run_support import RISK_SPEC, effect_compiled, fleet_risk
# Explicit compatibility lane: the CMUX Mission driver and its process/CMUX effects.
# Herdr presets are dispatched before it and never fall back to it (AGENTS.md).
import fleet_legacy_mission
from fleet_legacy_mission import (
    assurance_handoff_command_timeout, render_prompt, terminal_lead_result,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = ROOT / "orchestration" / "runs"
FEATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WORKFLOW = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")


def git_value(repo: Path, *args: str) -> str:
    return fleet_legacy_mission.require_success(["git", "-C", str(repo), *args]).stdout.strip()


def git_is_dirty(repo: Path) -> bool:
    return bool(git_value(repo, "status", "--porcelain"))


def workflow_path(name: str) -> Path:
    if not WORKFLOW.fullmatch(name):
        raise MissionRunError("invalid workflow name")
    path = ROOT / "workflows" / f"{name}.yaml"
    if not path.is_file():
        raise MissionRunError(f"unknown workflow: {name}")
    return path


def drive_mission(runs_dir: Path, mission_id: str) -> dict[str, Any]:
    root = mission_state.mission_root(runs_dir, mission_id)
    compiled, initial = effect_compiled(runs_dir, mission_id)
    if fleet_herdr_profile.is_herdr_preset(compiled["resolved"]["preset"]):
        return fleet_herdr_mission.drive(runs_dir, mission_id)
    return fleet_legacy_mission.drive_legacy_mission(
        runs_dir, mission_id, root=root, compiled=compiled, initial=initial
    )


def create_and_drive(
    runs_dir: Path,
    *,
    feature: str,
    objective: str,
    workflow_name: str,
    target_repo: Path,
    risk_override: str,
    timeout_seconds: int | None,
    allow_dirty_baseline: bool,
    teardown: bool,
    execution_profile: str = "native",
    acceptance_contract: dict[str, Any] | None = None,
    router_path: Path | None = None,
    herdr_session: str | None = None,
    herdr_runtime_root: str | None = None,
    herdr_launch_manifest: dict[str, Any] | None = None,
    herdr_capsule_manifest: dict[str, Any] | None = None,
    functional_contract: dict[str, Any] | None = None,
    sdd_plan_path: Path | None = None,
    scope_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not FEATURE.fullmatch(feature):
        raise MissionRunError("invalid feature")
    try:
        execution_profile = fleet_manifest.validate_profile(execution_profile)
    except fleet_manifest.ManifestError as exc:
        raise MissionRunError(str(exc)) from exc
    target_repo = exact_git_toplevel(target_repo)
    if not runs_dir.exists():
        mission_state.ensure_private_directory(runs_dir)
    try:
        runs_dir = fleet_safe_paths.canonical_root(runs_dir)
    except fleet_safe_paths.SafePathError as exc:
        raise MissionRunError("unsafe mission runs root") from exc
    compiled = workflow_config.compile_path(
        workflow_path(workflow_name), router_path=router_path
    )
    if (fleet_herdr_profile.is_herdr_preset(compiled["resolved"]["preset"])
            and fleet_herdr_profile.resolve_profile(compiled) is fleet_herdr_profile.MINIMAL
            and acceptance_contract is None):
        raise MissionRunError("minimal Herdr profile requires --acceptance-contract")
    is_herdr = fleet_herdr_profile.is_herdr_preset(compiled["resolved"]["preset"])
    profile = fleet_herdr_profile.resolve_profile(compiled) if is_herdr else None
    if scope_contract is not None:
        fleet_herdr_scope.validate(scope_contract)
        scope_profile = fleet_herdr_profile.PHYSICAL_SCOPE_PROFILE
        if profile is not scope_profile:
            raise MissionRunError(f"--scope-contract requires {scope_profile.preset}")
    if sdd_plan_path is not None and not is_herdr:
        raise MissionRunError("--sdd-plan requires a supported Herdr workflow")
    herdr_runtime_options = {}
    if herdr_runtime_root is not None:
        if not is_herdr:
            raise MissionRunError("Herdr runtime layout requires a supported Herdr profile")
        herdr_runtime_options = {"herdr_layout": {"version": 2, "runtime_root": herdr_runtime_root},
                                 **fleet_herdr_profile.runtime_binding(profile)}
        fleet_herdr_runtime.candidate_path(runs_dir, str(uuid.UUID(int=0)),
                                          herdr_runtime_options, target_repo)
    if herdr_capsule_manifest is not None:
        import fleet_mission_capsule
        if not herdr_runtime_options or herdr_launch_manifest is not None:
            raise MissionRunError("capsule requires --herdr-runtime-root and excludes legacy launch")
        if profile is not None and not profile.allow_capsule:
            raise MissionRunError("Research Herdr profile does not support capsule execution")
        herdr_runtime_options["herdr_capsule_manifest"] = fleet_mission_capsule.validate_manifest(herdr_capsule_manifest, live=True)
    if herdr_launch_manifest is not None:
        if not herdr_runtime_options:
            raise MissionRunError("observed launch requires --herdr-runtime-root")
        if profile is not None and not profile.allow_experimental_launch:
            raise MissionRunError("Research Herdr profile does not support experimental launch")
        herdr_runtime_options["herdr_launch_manifest"] = fleet_herdr_launch.validate_manifest(herdr_launch_manifest)
    if is_herdr:
        herdr_runtime_options = {**fleet_herdr_profile.runtime_binding(profile), **herdr_runtime_options}
        if profile is fleet_herdr_profile.RESEARCH:
            # New official Research missions freeze useful CAS-backed transfer.
            # Existing missions recover their already persisted runtime options.
            herdr_runtime_options["herdr_handoff_policy"] = fleet_herdr_runtime.HANDOFF_POLICY
        if herdr_capsule_manifest is None and herdr_launch_manifest is None:
            from fleet_herdr_personal import PROFILE
            herdr_runtime_options["herdr_personal_cli"] = PROFILE
        herdr_session = herdr_session or os.environ.get("HERDR_SESSION")
        if not isinstance(herdr_session, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", herdr_session):
            raise MissionRunError("Herdr requires an explicit --herdr-session or inherited HERDR_SESSION")
        if acceptance_contract is None:
            raise MissionRunError("Herdr run requires --acceptance-contract before launching agents")
        if compiled["workflow"]["limits"]["token_budget"] > 0:
            raise MissionRunError("Herdr token-budget enforcement is not available; refusing bounded launch")
    if acceptance_contract is not None:
        fleet_acceptance.validate(acceptance_contract)
        if compiled["workflow"]["archive"]["content_policy"] != "full" or not compiled["workflow"]["archive"]["include_final_tree"]:
            raise MissionRunError("artifact acceptance requires a full archive with final tree")
    mission_support.enforce_audit_trust(compiled, [], execution_profile)
    timeout = timeout_seconds or int(compiled["workflow"]["limits"]["deadline_seconds"])
    if timeout < 60:
        raise MissionRunError("timeout must be at least 60 seconds")
    objective_hash = mission_state.artifact_id(objective)
    target_hash = mission_state.artifact_id(str(target_repo))
    runs_hash = mission_state.artifact_id(str(runs_dir))
    key = (
        f"mission:{feature}:{compiled['workflow_digest'][:16]}:"
        f"{objective_hash[:16]}:{target_hash[:16]}:{runs_hash[:16]}"
    )
    manifest_path = runs_dir / f"fleet-{feature}.manifest"
    if herdr_runtime_options:
        if scope_contract is not None:
            herdr_runtime_options["scope_contract"] = scope_contract
        key += ":herdr-runtime:" + mission_state.artifact_id(mission_state.canonical_bytes(herdr_runtime_options))
    if functional_contract is not None:
        fleet_functional.validate(functional_contract)
        if not is_herdr:
            raise MissionRunError("functional v1 requires Herdr")
        key += ":functional:" + fleet_functional.digest(functional_contract)
    if acceptance_contract is not None:
        key = fleet_acceptance.bound_key(key, acceptance_contract)
    if (
        git_is_dirty(target_repo)
        and not allow_dirty_baseline
        and not manifest_path.exists()
    ):
        raise MissionRunError(
            "target checkout is dirty; commit/stash it or pass --allow-dirty-baseline"
        )
    mission_id, _ = fleet_mission.create_mission(
        runs_dir,
        compiled=compiled,
        feature=feature,
        objective=objective,
        target_repo=target_repo,
        base_sha=git_value(target_repo, "rev-parse", "HEAD"),
        idempotency_key=key,
        runtime_options={
            **herdr_runtime_options,
            "risk_override": risk_override,
            "timeout_seconds": timeout,
            "teardown": teardown,
            "allow_dirty_baseline": allow_dirty_baseline,
            "execution_profile": execution_profile,
            **({"herdr_session": herdr_session} if herdr_session else {}),
            **({"acceptance_contract": acceptance_contract} if acceptance_contract is not None else {}),
            **({"functional_contract": functional_contract} if functional_contract is not None else {}),
        },
        sdd_plan_path=sdd_plan_path,
    )
    try:
        return drive_mission(runs_dir, mission_id)
    except Exception as exc:
        # The Mission is already durable; let main report its live state.
        exc.fleet_mission_id = mission_id
        raise


def dry_run(
    *,
    feature: str,
    objective: str,
    workflow_name: str,
    target_repo: Path,
    risk_override: str,
    timeout_seconds: int | None,
    execution_profile: str = "native",
    acceptance_contract: dict[str, Any] | None = None,
    router_path: Path | None = None,
    functional_contract: dict[str, Any] | None = None,
    scope_contract: dict[str, Any] | None = None,
    work_packet: bool = False,
    work_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if work_context is not None and not work_packet:
        raise MissionRunError("--work-context requires --work-packet")
    target_repo = exact_git_toplevel(target_repo)
    if functional_contract is not None:
        fleet_functional.validate(functional_contract)
    try:
        execution_profile = fleet_manifest.validate_profile(execution_profile)
    except fleet_manifest.ManifestError as exc:
        raise MissionRunError(str(exc)) from exc
    compiled = workflow_config.compile_path(
        workflow_path(workflow_name), router_path=router_path
    )
    if (fleet_herdr_profile.is_herdr_preset(compiled["resolved"]["preset"])
            and fleet_herdr_profile.resolve_profile(compiled) is fleet_herdr_profile.MINIMAL
            and acceptance_contract is None):
        raise MissionRunError("minimal Herdr profile requires --acceptance-contract")
    if functional_contract is not None and not fleet_herdr_profile.is_herdr_preset(compiled["resolved"]["preset"]):
        raise MissionRunError("functional contracts require a supported Herdr workflow")
    if scope_contract is not None:
        fleet_herdr_scope.validate(scope_contract)
        scope_preset = fleet_herdr_profile.PHYSICAL_SCOPE_PROFILE.preset
        if compiled["resolved"]["preset"] != scope_preset:
            raise MissionRunError(f"--scope-contract requires {scope_preset}")
    if acceptance_contract is not None:
        fleet_acceptance.validate(acceptance_contract)
        if compiled["workflow"]["archive"]["content_policy"] != "full" or not compiled["workflow"]["archive"]["include_final_tree"]:
            raise MissionRunError("artifact acceptance requires a full archive with final tree")
    assessment = fleet_risk.assess(
        workflow_minimum=compiled["workflow"]["risk"]["minimum"],
        objective=objective,
        target=str(target_repo),
        repository_root=str(target_repo),
        override=risk_override,
    )
    mission_support.enforce_audit_trust(compiled, list(assessment["categories"]), execution_profile)
    timeout = timeout_seconds or int(compiled["workflow"]["limits"]["deadline_seconds"])
    preview = {}
    if work_packet:
        if scope_contract is None:
            raise MissionRunError("--work-packet requires an explicit --scope-contract")
        prepared = fleet_herdr_work_packet.prepare(objective=objective,
            candidate_repo=target_repo, base_sha=git_read(target_repo, "rev-parse", "HEAD"),
            compiled=compiled, acceptance_contract=acceptance_contract, scope_contract=scope_contract,
            functional_contract=functional_contract, work_context=work_context,
            timeout_seconds=timeout_seconds)
        preview = {"owner_work_packet": prepared["work_packet"],
            "owner_protocol": {"mode": "preview_only", "dispatch_enabled": False,
                "instruction_source": "tracked HEAD, including on a dirty checkout",
                "note": "No Mission, admission, session or candidate is created. B stages information and interaction; repair, decisions lifecycle and amendments remain disabled."}}
    return {
        "feature": feature,
        "objective_sha256": mission_state.artifact_id(objective),
        "workflow": workflow_name,
        "workflow_digest": compiled["workflow_digest"],
        "compiled_digest": compiled["compiled_digest"],
        "resolved": compiled["resolved"],
        "risk": assessment,
        "target_repo": str(target_repo),
        "timeout_seconds": timeout,
        "execution_profile": execution_profile,
        "effects": [],
        **preview,
        **({"physical_scope": {"contract_sha256": fleet_herdr_scope.digest(scope_contract),
                               "archive_schema_version": 8}} if scope_contract is not None else {}),
        **({"functional": {"schema_version": 1, "spec_sha256": fleet_functional.digest(functional_contract),
                            "check_id": functional_contract["check_id"]}} if functional_contract is not None else {}),
        "backend": "herdr" if fleet_herdr_profile.is_herdr_preset(compiled["resolved"]["preset"]) else "cmux-legacy",
        "acceptance": {"mode": "artifact_contract", "contract_sha256": fleet_acceptance.digest(acceptance_contract)} if acceptance_contract is not None else {"mode": "legacy_not_evaluated"},
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", default=os.environ.get("FLEET_RUNS_DIR", str(DEFAULT_RUNS_DIR)))
    commands = parser.add_subparsers(dest="command", required=True)

    for name in ("run", "dry"):
        command = commands.add_parser(name)
        command.add_argument("feature")
        command.add_argument("objective")
        command.add_argument("--workflow", default="herdr-implementation")
        command.add_argument("--herdr-session", help="explicit Herdr session frozen in mission runtime options")
        if name == "run":
            command.add_argument("--herdr-runtime-root", help="existing private physical directory outside runs/source; enables layout v2 and independent role inputs")
            command.add_argument("--herdr-launch-manifest", type=Path, help="pinned local Herdr/Codex images; records launch inputs only, not effective sandbox binding")
            command.add_argument("--herdr-capsule-manifest", type=Path, help="explicit external Seatbelt executor; no unconstrained Herdr fallback")
        command.add_argument("--target-repo", default=os.getcwd())
        command.add_argument(
            "--risk", default="auto", choices=("auto", *fleet_risk.RISK_ORDER)
        )
        command.add_argument("--timeout", type=int)
        command.add_argument("--json", action="store_true")
        command.add_argument(
            "--router",
            type=Path,
            help="router path used to compile the durable mission authority",
        )
        command.add_argument("--acceptance-contract", type=Path)
        command.add_argument("--functional-contract", type=Path, help="versioned required functional check frozen before launch")
        command.add_argument("--scope-contract", type=Path, help=f"opt-in physical candidate acceptance ({fleet_herdr_profile.PHYSICAL_SCOPE_PROFILE.preset} only)")
        if name == "dry":
            command.add_argument("--work-packet", action="store_true", help="preview owner-work-v1; owner cycle and dispatch remain disabled")
            command.add_argument("--work-context", type=Path, help="initial context, requirements, preferences and decisions for the work packet")
        command.add_argument(
            "--execution-profile",
            default="native",
            choices=fleet_manifest.EXECUTION_PROFILES,
        )
        if name == "run":
            command.add_argument("--allow-dirty-baseline", action="store_true")
            command.add_argument("--teardown", action="store_true")
            command.add_argument("--sdd-plan", type=Path, help="freeze an SDD plan snapshot before Mission creation (supported Herdr profiles only)")

    resume = commands.add_parser("resume")
    resume.add_argument("--mission-id", required=True)
    resume.add_argument("--json", action="store_true")
    for name in ("pause", "cancel-mission"):
        command = commands.add_parser(name, help="persist a Mission control request without waiting for the driver lock")
        command.add_argument("--mission-id", required=True)
        command.add_argument("--reason", required=True)
        command.add_argument("--idempotency-key", required=True)
        command.add_argument("--generation")
        command.add_argument("--json", action="store_true")
    supervise = commands.add_parser("supervise", help="bounded foreground supervision of one explicit Herdr Mission")
    supervise.add_argument("--mission-id", required=True)
    supervise.add_argument("--seconds", type=float, default=60)
    supervise.add_argument("--poll-seconds", type=float, default=0.25)
    supervise.add_argument("--json", action="store_true")
    show = commands.add_parser("show")
    show.add_argument("--mission-id", required=True)
    status = commands.add_parser("status", help="read durable Mission status without contacting a runtime")
    status.add_argument("--mission-id", required=True)
    status.add_argument("--json", action="store_true")

    cancel = commands.add_parser("cancel", help="cancel an exact admitted Herdr run; never a focused pane")
    cancel.add_argument("--mission-id", required=True)
    cancel.add_argument("--run-id", required=True)
    cancel.add_argument("--generation")
    cancel.add_argument("--reason", required=True)
    cancel.add_argument("--idempotency-key", required=True)
    cancel.add_argument("--json", action="store_true")
    retry = commands.add_parser("retry-start", help="recover an exited, unsubmitted startup in its exact owned pane")
    retry.add_argument("--mission-id", required=True)
    retry.add_argument("--instance", choices=("lead", "research", "worker", "reviewer", "verifier"), required=True)
    retry.add_argument("--json", action="store_true")

    assurance = commands.add_parser("request-assurance")
    assurance.add_argument("--mission-id", required=True)
    assurance.add_argument("--risk", choices=("high", "unknown"), required=True)
    assurance.add_argument("--categories", required=True)
    assurance.add_argument("--reason", required=True)
    return parser


def cancel_herdr_run(runs_dir: Path, mission_id: str, *, run_id: str,
                     reason: str, idempotency_key: str, generation: str | None = None) -> dict[str, Any]:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    try:
        value = fleet_herdr_control.request(runs_dir, mission_id, action="cancel", run_id=run_id,
            reason=reason, idempotency_key=idempotency_key, generation=generation)
    except mission_state.MissionStateError as exc:
        raise MissionRunError(str(exc)) from exc
    if "request_id" not in value:
        return {**value, "cancelled": False}
    return {**drive_mission(runs_dir, mission_id), "control_request": value, "cancel_requested": True}


def mission_status(runs_dir: Path, mission_id: str) -> dict[str, Any]:
    compiled, current = fleet_mission.load_mission_compiled(runs_dir, mission_id, mode="read")
    admissions = [{k: a.get(k) for k in ("run_id", "recipient_instance", "phase", "active", "writer", "terminal")}
                  for a in current["admissions"].values()]
    cancelled = current.get("cancelled_runs", {})
    result = {"mission_id": current["mission_id"], "feature": current["feature"],
        **({"control": fleet_herdr_control.view(current)} if "herdr_control" in current else {}),
        "backend": "herdr" if fleet_herdr_profile.is_herdr_preset(compiled["resolved"]["preset"]) else "cmux-legacy",
        "status": current["status"], "terminal": current.get("terminal"), "admissions": admissions,
        "cancellations": [{"run_id": run, "state": "confirmed" if any(
            a["run_id"] == run and a.get("terminal", {}).get("status") == "abandoned"
            for a in admissions if isinstance(a.get("terminal"), dict)) else "requested"} for run in cancelled],
        "head_sha256": current["head_sha256"]}
    root = Path("missions") / current["mission_id"]
    with fleet_safe_paths.RootedFS(runs_dir) as fs:
        entries = fs.list_directory(root, directory_modes=(0o700, 0o700))
        backend = fleet_herdr_versions.read_state(fs, root / "herdr-backend.json",
            mission_id=current["mission_id"], compiled_digest=compiled["compiled_digest"])
        if backend is not None and backend.get("executor") == "fleet.mission.capsule.v2":
            result["runtime"] = {"executor": backend["executor"], "session": backend["session"],
                "generation": backend["generation"], "workspace": backend["workspace"],
                "visibility": "headless_confined_cli", "status_source": "mission_ledger"}
        elif backend is not None:
            result["runtime"] = {"phase": backend["phase"], "session": backend["session"],
                "workspace": backend["workspace"], "members": [{k: m.get(k) for k in (
                    "instance_id", "model", "start_phase", "pane_id", "agent_session")}
                    for m in backend["members"]],
                "submissions": [{k: s.get(k) for k in ("run_id", "instance_id", "status", "cancel_attempted")}
                    for s in backend["submissions"].values()]}
        if "herdr-archive" in entries:
            try:
                result["archive"] = fleet_herdr_archive.verify(runs_dir, current["mission_id"])
                result["acceptance"] = result["archive"]["acceptance"]
            except (fleet_herdr_archive.HerdrArchiveError, fleet_safe_paths.SafePathError) as exc:
                result["archive"] = {"valid": False, "error": str(exc)}
        if "herdr-teardown.json" in entries:
            result["cleanup"] = "complete"
    if current["status"] not in mission_state.TERMINAL_STATUSES:
        result["next_action"] = "resume to reconcile durable results, pending decisions or cancellation"
    return result


def emit(value: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


CONTROLLER_ERROR_EXIT = 4


def live_mission_status(runs_dir: Path, mission_id: Any) -> str | None:
    """Non-terminal durable status after a controller error, or None when unknown/terminal."""
    if not isinstance(mission_id, str):
        return None
    try:
        current = fleet_mission.load_state(runs_dir, mission_state.normalize_uuid(mission_id, "mission_id"))
    except (mission_state.MissionStateError, fleet_safe_paths.SafePathError, OSError, ValueError):
        return None
    return None if current["status"] in mission_state.TERMINAL_STATUSES else current["status"]


def response_exit_code(value: Mapping[str, Any]) -> int:
    if "next_action" in value or value.get("status") == "blocked":
        return 3
    if value.get("status") in {"failed", "indeterminate", "abandoned"}:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runs_dir = Path(args.runs_dir).expanduser().resolve()
    try:
        if args.command == "dry":
            value = dry_run(
                feature=args.feature,
                objective=args.objective,
                workflow_name=args.workflow,
                target_repo=Path(args.target_repo).expanduser().resolve(),
                risk_override=args.risk,
                timeout_seconds=args.timeout,
                execution_profile=args.execution_profile,
                acceptance_contract=fleet_acceptance.load(args.acceptance_contract) if args.acceptance_contract else None,
                functional_contract=fleet_functional.load(args.functional_contract) if args.functional_contract else None,
                scope_contract=fleet_herdr_scope.load(args.scope_contract) if args.scope_contract else None,
                work_packet=args.work_packet,
                work_context=fleet_herdr_work_packet.load_context(args.work_context) if args.work_context else None,
                router_path=args.router,
            )
            emit(value, json_mode=args.json)
            return 0
        if args.command == "run":
            value = create_and_drive(
                runs_dir,
                feature=args.feature,
                objective=args.objective,
                workflow_name=args.workflow,
                target_repo=Path(args.target_repo).expanduser().resolve(),
                risk_override=args.risk,
                timeout_seconds=args.timeout,
                allow_dirty_baseline=args.allow_dirty_baseline,
                teardown=args.teardown,
                sdd_plan_path=Path(args.sdd_plan).expanduser() if args.sdd_plan else None,
                herdr_session=args.herdr_session,
                herdr_runtime_root=args.herdr_runtime_root,
                herdr_launch_manifest=mission_state.loads_strict(args.herdr_launch_manifest.read_bytes()) if args.herdr_launch_manifest else None,
                herdr_capsule_manifest=mission_state.loads_strict(args.herdr_capsule_manifest.read_bytes()) if args.herdr_capsule_manifest else None,
                execution_profile=args.execution_profile,
                acceptance_contract=fleet_acceptance.load(args.acceptance_contract) if args.acceptance_contract else None,
                functional_contract=fleet_functional.load(args.functional_contract) if args.functional_contract else None,
                scope_contract=fleet_herdr_scope.load(args.scope_contract) if args.scope_contract else None,
                router_path=args.router,
            )
            emit(value, json_mode=args.json)
            return response_exit_code(value)
        if args.command == "resume":
            current = fleet_mission.load_state(runs_dir, args.mission_id)
            if current["status"] not in mission_state.TERMINAL_STATUSES and fleet_herdr_control.view(current)["desired"] == "pause_requested":
                fleet_herdr_control.request(runs_dir, args.mission_id, action="resume", reason="explicit resume",
                    idempotency_key="resume-" + str(uuid.uuid4()))
            value = drive_mission(runs_dir, args.mission_id)
            emit(value, json_mode=args.json)
            return response_exit_code(value)
        if args.command == "show":
            emit(fleet_mission.load_state(runs_dir, args.mission_id), json_mode=False)
            return 0
        if args.command == "status":
            value = mission_status(runs_dir, args.mission_id)
            emit(value, json_mode=args.json)
            return 1 if value.get("archive", {}).get("valid") is False else 0
        if args.command == "cancel":
            value = cancel_herdr_run(runs_dir, args.mission_id, run_id=args.run_id,
                reason=args.reason, idempotency_key=args.idempotency_key, generation=args.generation)
            emit(value, json_mode=args.json)
            return response_exit_code(value)
        if args.command in {"pause", "cancel-mission"}:
            value = fleet_herdr_control.request(runs_dir, args.mission_id,
                action="pause" if args.command == "pause" else "cancel", reason=args.reason,
                idempotency_key=args.idempotency_key, generation=args.generation)
            emit(value, json_mode=args.json)
            return 0
        if args.command == "supervise":
            value = fleet_herdr_mission.supervise(runs_dir, args.mission_id,
                seconds=args.seconds, poll_seconds=args.poll_seconds)
            emit(value, json_mode=args.json)
            return response_exit_code(value)
        if args.command == "retry-start":
            mission_id = mission_state.normalize_uuid(args.mission_id, "mission_id")
            with fleet_safe_paths.RootedFS(runs_dir) as fs:
                with fs.exclusive_lock(Path("missions") / mission_id / "herdr-driver.lock",
                        directory_modes=(0o700, 0o700), blocking=False) as acquired:
                    if not acquired:
                        raise MissionRunError("driver busy; startup retry was not attempted")
                    driver = fleet_herdr_mission._Driver(runs_dir, mission_id)
                    driver.load()
                    if driver.sdd_error is not None:
                        raise MissionRunError("SDD plan binding failed closed: " + driver.sdd_error)
                    if fleet_herdr_control.view(driver.current())["desired"] != "running":
                        raise MissionRunError("control request blocks startup retry; resume the Mission explicitly")
                    if driver.current()["status"] != "booting":
                        raise MissionRunError("startup retry requires a booting Herdr mission")
                    driver.check_candidate()
                    driver.backend().retry_unsubmitted_start(args.instance)
            value = drive_mission(runs_dir, mission_id)
            emit(value, json_mode=args.json)
            return response_exit_code(value)
        if args.command == "request-assurance":
            categories = sorted(
                {item.strip() for item in args.categories.split(",") if item.strip()}
            )
            if not categories:
                raise MissionRunError("--categories must contain at least one value")
            value = fleet_legacy_mission.request_assurance(
                runs_dir,
                args.mission_id,
                requested_risk=args.risk,
                categories=categories,
                reason=args.reason,
            )
            emit(value, json_mode=True)
            return 0
        raise MissionRunError("unknown command")
    except (
        fleet_herdr.HerdrBackendError,
        fleet_herdr_launch.LaunchError,
        fleet_herdr_runtime.RuntimeContractError,
        fleet_herdr_archive.HerdrArchiveError,
        fleet_herdr_mission.HerdrMissionError,
        fleet_safe_paths.SafePathError,
        fleet_acceptance.AcceptanceError,
        fleet_functional.FunctionalError,
        fleet_herdr_work_packet.WorkPacketError,
        fleet_json.FleetJSONError,
        OSError,
        MissionRunError,
        mission_state.MissionStateError,
        workflow_config.WorkflowError,
        fleet_risk.RiskError,
        fleet_artifacts.ArtifactError,
        fleet_audit_client.AuditClientError,
        fleet_archive.ArchiveError,
        fleet_assured_runner.AssuredRunnerError,
        fleet_manifest.ManifestError,
        fleet_control_service.ControlServiceError,
    ) as exc:
        print(f"mission-run: {exc}", file=sys.stderr)
        mission_id = getattr(exc, "fleet_mission_id", None) or getattr(args, "mission_id", None)
        status = live_mission_status(runs_dir, mission_id)
        if status is None:
            return 1
        # Exit 1 means a terminal verdict; this Mission is still live and owned.
        emit({"mission_id": mission_state.normalize_uuid(mission_id, "mission_id"), "status": status,
              "controller_error": str(exc),
              "next_action": f"controller error; durable Mission remains {status}; resolve the error, then resume "
                             "or cancel the exact run"}, json_mode=getattr(args, "json", False))
        return CONTROLLER_ERROR_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
