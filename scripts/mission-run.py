#!/usr/bin/env python3
"""Run and resume the canonical durable Mission Control entry point."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any
import uuid

import fleet_artifacts
import fleet_archive
import fleet_audit_client
import fleet_assured_runner
import fleet_control_service
import fleet_manifest
import fleet_mission
import fleet_mission_state as mission_state
import fleet_tracking
import workflow_config


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = ROOT / "orchestration" / "runs"
PROMPT_TEMPLATE = ROOT / "orchestration" / "prompts" / "mission_lead.md"
FEATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WORKFLOW = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
RISK_SPEC = importlib.util.spec_from_file_location("fleet_risk", ROOT / "scripts" / "fleet-risk.py")
assert RISK_SPEC and RISK_SPEC.loader
fleet_risk = importlib.util.module_from_spec(RISK_SPEC)
RISK_SPEC.loader.exec_module(fleet_risk)


class MissionRunError(RuntimeError):
    """Mission runner reconciliation cannot proceed safely."""


def run_process(
    command: list[str],
    *,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            env=env or os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MissionRunError(f"command failed to run: {Path(command[0]).name}: {exc}") from exc


def require_success(
    command: list[str],
    *,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = run_process(command, timeout=timeout, env=env)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise MissionRunError(f"{Path(command[0]).name} failed: {detail}")
    return result


def last_json_object(text: str, source: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise MissionRunError(f"{source} returned no JSON object")


def parse_manifest(path: Path) -> dict[str, str]:
    try:
        return fleet_manifest.load(path)
    except (OSError, fleet_manifest.ManifestError) as exc:
        raise MissionRunError(f"cannot read fleet manifest {path}: {exc}") from exc


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MissionRunError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MissionRunError(f"expected JSON object: {path}")
    return value


def git_value(repo: Path, *args: str) -> str:
    return require_success(["git", "-C", str(repo), *args]).stdout.strip()


def git_is_dirty(repo: Path) -> bool:
    return bool(git_value(repo, "status", "--porcelain"))


def workflow_path(name: str) -> Path:
    if not WORKFLOW.fullmatch(name):
        raise MissionRunError("invalid workflow name")
    path = ROOT / "workflows" / f"{name}.yaml"
    if not path.is_file():
        raise MissionRunError(f"unknown workflow: {name}")
    return path


def render_prompt(
    *,
    mission_id: str,
    feature: str,
    objective: str,
    compiled: dict[str, Any],
    risk: str,
    target_repo: Path,
    manifest: Path,
    timeout_seconds: int,
) -> str:
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "{{MISSION_ID}}": mission_id,
        "{{FEATURE}}": feature,
        "{{WORKFLOW}}": compiled["workflow"]["name"],
        "{{WORKFLOW_DIGEST}}": compiled["workflow_digest"],
        "{{RISK}}": risk,
        "{{TARGET_REPO}}": str(target_repo),
        "{{MANIFEST}}": str(manifest),
        "{{TIMEOUT_SECONDS}}": str(timeout_seconds),
        "{{OBJECTIVE}}": objective,
        "{{CAPABILITY_CATALOG}}": json.dumps(
            compiled["resolved"], ensure_ascii=False, indent=2, sort_keys=True
        ),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    if "{{" in template or "}}" in template:
        raise MissionRunError("mission prompt has unresolved template markers")
    return template


def cmux_signal(
    manifest: dict[str, str],
    mission_id: str,
    *,
    status: str,
    progress: float,
    message: str,
    notify: bool = False,
) -> None:
    workspace = manifest.get("workspace")
    if not workspace:
        return
    label = f"{status}:{mission_id[:8]}"
    commands = [
        ["cmux", "set-status", "mission", label, "--workspace", workspace, "--icon", "sparkles"],
        ["cmux", "set-progress", f"{progress:.2f}", "--label", label, "--workspace", workspace],
        ["cmux", "log", "--level", "info", "--source", "mission-control", "--workspace", workspace,
         f"mission_id={mission_id} {message}"],
    ]
    if notify:
        commands.append(
            ["cmux", "notify", "--title", f"Mission {mission_id[:8]}: {status}", "--workspace", workspace]
        )
    for command in commands:
        subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, "CMUX_QUIET": "1"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def legacy_events(runs_dir: Path, feature: str) -> list[dict[str, Any]]:
    path = runs_dir / f"fleet-{feature}.ledger.jsonl"
    events: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(raw)
            if isinstance(value, dict):
                events.append(value)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        raise MissionRunError(f"legacy fleet ledger is corrupt: {exc}") from exc
    return events


def reconcile_lead_run(
    runs_dir: Path,
    feature: str,
    prompt_sha256: str,
) -> dict[str, Any] | None:
    latest: dict[str, dict[str, Any]] = {}
    for event in legacy_events(runs_dir, feature):
        if event.get("instance") != "lead" or event.get("task_sha256") != prompt_sha256:
            continue
        run_id = str(event.get("run_id", ""))
        try:
            uuid.UUID(run_id)
        except ValueError:
            raise MissionRunError("legacy lead run has invalid run_id")
        latest[run_id] = event
    if len(latest) > 1:
        raise MissionRunError("multiple legacy lead runs match one mission dispatch intent")
    return next(iter(latest.values()), None)


def exact_legacy_run(runs_dir: Path, feature: str, run_id: str) -> dict[str, Any]:
    matches = [
        event
        for event in legacy_events(runs_dir, feature)
        if event.get("instance") == "lead" and event.get("run_id") == run_id
    ]
    if not matches:
        raise MissionRunError(f"missing legacy evidence for lead run {run_id}")
    manifest = parse_manifest(runs_dir / f"fleet-{feature}.manifest")
    try:
        return fleet_tracking.verify_run_events(
            matches,
            required_protocol=manifest.get("tracking_protocol", "legacy-cmux"),
        )
    except fleet_tracking.TrackingError as exc:
        raise MissionRunError(f"lead tracked result provenance invalid: {exc}") from exc


def _write_exact(path: Path, content: bytes) -> None:
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise MissionRunError(f"durable mission artifact conflicts: {path.name}")
        return
    mission_state.atomic_write(path, content)


def request_assurance(
    runs_dir: Path,
    mission_id: str,
    *,
    requested_risk: str,
    categories: list[str],
    reason: str,
) -> dict[str, Any]:
    current = fleet_mission.load_state(runs_dir, mission_id)
    target_risk = fleet_risk.escalate(current["risk"], requested_risk)
    key_base = f"lead:assurance:{mission_state.artifact_id(reason)[:24]}"
    if target_risk != current["risk"]:
        mission_state.append_event(
            runs_dir,
            mission_id,
            kind="risk_escalated",
            actor="lead",
            idempotency_key=f"{key_base}:risk",
            payload={
                "from": current["risk"],
                "to": target_risk,
                "categories": sorted(set(categories)),
                "reason": reason,
            },
        )
        current = fleet_mission.load_state(runs_dir, mission_id)
    mission_state.append_event(
        runs_dir,
        mission_id,
        kind="assurance_requested",
        actor="lead",
        idempotency_key=f"{key_base}:request",
        payload={
            "risk": target_risk,
            "categories": sorted(set(categories)),
            "scope": current["target_repo"],
            "workflow_digest": current["workflow_digest"],
        },
    )
    return fleet_mission.load_state(runs_dir, mission_id)


def drive_mission(runs_dir: Path, mission_id: str) -> dict[str, Any]:
    root = mission_state.mission_root(runs_dir, mission_id)
    compiled = fleet_mission.validate_compiled(load_json(root / "compiled-workflow.json"))
    options = load_json(root / "runtime-options.json")
    objective = (root / "objective.txt").read_text(encoding="utf-8")
    target_repo = Path(fleet_mission.load_state(runs_dir, mission_id)["target_repo"])
    manifest_path = runs_dir / f"fleet-{fleet_mission.load_state(runs_dir, mission_id)['feature']}.manifest"
    timeout_seconds = int(options.get("timeout_seconds") or compiled["workflow"]["limits"]["deadline_seconds"])
    execution_profile = fleet_manifest.validate_profile(
        str(options.get("execution_profile", "native"))
    )

    while True:
        current = fleet_mission.load_state(runs_dir, mission_id)
        feature = current["feature"]
        if current["status"] in mission_state.TERMINAL_STATUSES:
            control_lifecycle = fleet_control_service.ControlLifecycle(runs_dir, mission_id)
            if control_lifecycle.lifecycle_path.exists():
                lifecycle = load_json(control_lifecycle.lifecycle_path)
                if lifecycle.get("stopped_at") is None:
                    control_lifecycle.stop()
            if options.get("teardown") and manifest_path.exists():
                require_success([str(ROOT / "scripts" / "fleet-down.sh"), feature], timeout=180)
            result_path = root / "lead-result.txt"
            return {
                "mission_id": mission_id,
                "feature": feature,
                "status": current["status"],
                "result_file": str(result_path) if result_path.exists() else "",
                "result": result_path.read_text(encoding="utf-8") if result_path.exists() else "",
                "head_sha256": current["head_sha256"],
            }

        if current["status"] == "compiled":
            assessment = fleet_risk.assess(
                workflow_minimum=compiled["workflow"]["risk"]["minimum"],
                objective=objective,
                target=str(target_repo),
                override=str(options.get("risk_override", "auto")),
            )
            mission_state.append_event(
                runs_dir,
                mission_id,
                kind="risk_assessed",
                actor="CONTROL",
                idempotency_key="controller:risk:assessed",
                payload=assessment,
            )
            if assessment["level"] != current["risk"]:
                mission_state.append_event(
                    runs_dir,
                    mission_id,
                    kind="risk_escalated",
                    actor="CONTROL",
                    idempotency_key="controller:risk:escalated",
                    payload={
                        "from": current["risk"],
                        "to": assessment["level"],
                        "categories": assessment["categories"],
                        "reason": "deterministic mission risk assessment",
                    },
                )
                current = fleet_mission.load_state(runs_dir, mission_id)
            if assessment["requires_confirmation"]:
                mission_state.append_event(
                    runs_dir,
                    mission_id,
                    kind="assurance_requested",
                    actor="CONTROL",
                    idempotency_key="controller:assurance:requested",
                    payload={
                        "risk": assessment["level"],
                        "categories": assessment["categories"],
                        "scope": str(target_repo),
                        "workflow_digest": compiled["workflow_digest"],
                    },
                )
                continue
            mission_state.append_event(
                runs_dir,
                mission_id,
                kind="fleet_boot_started",
                actor="CONTROL",
                idempotency_key="controller:fleet:boot",
                payload={"feature": feature, "preset": compiled["resolved"]["preset"]},
            )
            continue

        if current["status"] == "awaiting_assurance_confirmation":
            return {
                "mission_id": mission_id,
                "feature": feature,
                "status": current["status"],
                "risk": current["risk"],
                "categories": current["risk_categories"],
                "next_action": (
                    f"python3 scripts/fleet-approve.py --runs-dir {runs_dir} "
                    f"--mission-id {mission_id} --scope {target_repo}; then resume"
                ),
            }

        if current["status"] == "assurance_approved":
            fleet_audit_client.AuditLifecycle(runs_dir, mission_id).preflight()
            approval = current["approval"]
            preset = compiled["workflow"]["assurance"]["preset"]
            mission_state.append_event(
                runs_dir,
                mission_id,
                kind="assurance_boot_started",
                actor="CONTROL",
                idempotency_key="controller:assurance:boot",
                payload={
                    "preset": preset,
                    "approval_event_sha256": approval["event_sha256"],
                },
            )
            continue

        if current["status"] == "assured_booting":
            approval = current["approval"]
            preset = compiled["workflow"]["assurance"]["preset"]
            if not manifest_path.exists():
                env = {**os.environ, "FLEET_RUNS_DIR": str(runs_dir), "FLEET_MISSION_ID": mission_id}
                require_success(
                    [
                        str(ROOT / "scripts" / "fleet-up.sh"), feature,
                        "--preset", preset, "--target-repo", str(target_repo),
                        "--execution-profile", execution_profile,
                    ],
                    timeout=300,
                    env=env,
                )
            manifest = parse_manifest(manifest_path)
            if manifest.get("mission_id") != mission_id:
                raise MissionRunError("assured manifest mission_id mismatch")
            if manifest.get("preset") != preset or manifest.get("mode") != "assured":
                raise MissionRunError("assured manifest preset or mode mismatch")
            if Path(manifest.get("target_repo", "")).resolve() != target_repo.resolve():
                raise MissionRunError("assured manifest target_repo drift")
            if manifest.get("execution_profile") != execution_profile:
                raise MissionRunError("assured manifest execution_profile drift")
            audit_lifecycle = fleet_audit_client.AuditLifecycle(runs_dir, mission_id)
            audit_lifecycle.start(manifest_path)
            mission_state.append_event(
                runs_dir,
                mission_id,
                kind="assurance_started",
                actor="CONTROL",
                idempotency_key="controller:assurance:started",
                payload={
                    "manifest": str(manifest_path),
                    "approval_event_sha256": approval["event_sha256"],
                },
            )
            started = fleet_mission.load_state(runs_dir, mission_id)
            audit_lifecycle.record_control_event(
                event_type="MissionEvent",
                subject_id=started["head_sha256"][:32],
                subject_sha256=started["head_sha256"],
                metadata={"kind": "assurance_started", "sequence": started["last_sequence"]},
                idempotency_key="mission:assurance-started",
            )
            cmux_signal(
                manifest, mission_id, status="assured", progress=0.20,
                message="assured fleet boot reconciled",
            )
            continue

        if current["status"] == "booting":
            if not manifest_path.exists():
                env = {**os.environ, "FLEET_RUNS_DIR": str(runs_dir), "FLEET_MISSION_ID": mission_id}
                require_success(
                    [
                        str(ROOT / "scripts" / "fleet-up.sh"),
                        feature,
                        "--preset",
                        compiled["resolved"]["preset"],
                        "--target-repo",
                        str(target_repo),
                        "--execution-profile",
                        execution_profile,
                    ],
                    timeout=300,
                    env=env,
                )
            manifest = parse_manifest(manifest_path)
            if manifest.get("mission_id") != mission_id:
                raise MissionRunError("fleet manifest is not bound to this mission_id")
            if Path(manifest.get("target_repo", "")).resolve() != target_repo.resolve():
                raise MissionRunError("fleet manifest target_repo drift")
            if manifest.get("preset") != compiled["resolved"]["preset"]:
                raise MissionRunError("fleet manifest preset drift")
            if manifest.get("mode") != "autonomous":
                raise MissionRunError("canonical autonomous mission resolved a non-autonomous fleet")
            if manifest.get("execution_profile") != execution_profile:
                raise MissionRunError("fleet manifest execution_profile drift")
            mission_state.append_event(
                runs_dir,
                mission_id,
                kind="mission_running",
                actor="CONTROL",
                idempotency_key="controller:mission:running",
                payload={"manifest": str(manifest_path)},
            )
            cmux_signal(manifest, mission_id, status="running", progress=0.15, message="fleet boot reconciled")
            continue

        if current["status"] == "running":
            fleet_control_service.ControlLifecycle(runs_dir, mission_id).start()
            manifest = parse_manifest(manifest_path)
            prompt = render_prompt(
                mission_id=mission_id,
                feature=feature,
                objective=objective,
                compiled=compiled,
                risk=current["risk"],
                target_repo=target_repo,
                manifest=manifest_path,
                timeout_seconds=timeout_seconds,
            )
            prompt_sha = mission_state.artifact_id(prompt)
            if current["lead_run_id"] is None:
                mission_state.append_event(
                    runs_dir,
                    mission_id,
                    kind="lead_dispatch_intent",
                    actor="CONTROL",
                    idempotency_key="controller:lead:intent",
                    payload={"prompt_sha256": prompt_sha},
                )
                legacy = reconcile_lead_run(runs_dir, feature, prompt_sha)
                if legacy is None:
                    sent = require_success(
                        [str(ROOT / "scripts" / "fleet-send.sh"), feature, "lead", prompt, "--json"],
                        timeout=120,
                        env={**os.environ, "FLEET_RUNS_DIR": str(runs_dir)},
                    )
                    send_value = last_json_object(sent.stdout, "fleet-send")
                    run_id = str(send_value.get("run_id", ""))
                else:
                    run_id = str(legacy["run_id"])
                try:
                    run_id = str(uuid.UUID(run_id))
                except ValueError as exc:
                    raise MissionRunError("fleet-send returned invalid run_id") from exc
                mission_state.append_event(
                    runs_dir,
                    mission_id,
                    kind="lead_dispatched",
                    actor="CONTROL",
                    idempotency_key="controller:lead:dispatched",
                    payload={"run_id": run_id, "prompt_sha256": prompt_sha},
                )
                cmux_signal(manifest, mission_id, status="delegated", progress=0.30, message=f"lead_run_id={run_id}")
                continue

            if current["lead_result"] is None:
                wait = run_process(
                    [
                        str(ROOT / "scripts" / "fleet-wait.sh"), feature, "lead",
                        "--run", f"lead={current['lead_run_id']}",
                        "--timeout", str(timeout_seconds), "--json",
                    ],
                    timeout=timeout_seconds + 30,
                    env={**os.environ, "FLEET_RUNS_DIR": str(runs_dir)},
                )
                wait_value = last_json_object(wait.stdout, "fleet-wait")
                status_value = str(wait_value.get("status", "indeterminate"))
                if wait.returncode != 0 or status_value != "succeeded":
                    terminal = status_value if status_value in mission_state.TERMINAL_STATUSES else "indeterminate"
                    mission_state.append_terminal(
                        runs_dir,
                        mission_id,
                        status=terminal,
                        reason=f"lead run ended status={status_value} exit={wait.returncode}",
                        idempotency_key="controller:terminal:lead",
                    )
                    continue
                legacy = exact_legacy_run(runs_dir, feature, current["lead_run_id"])
                if legacy.get("status") != "succeeded":
                    raise MissionRunError("fleet-wait success disagrees with durable lead ledger")
                result_path = Path(str(legacy.get("result_file", "")))
                info = result_path.lstat()
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise MissionRunError("lead result_file is not a safe regular file")
                content = result_path.read_bytes()
                if len(content) > 16 * 1024 * 1024:
                    raise MissionRunError("lead result exceeds 16 MiB")
                artifact = hashlib.sha256(content).hexdigest()
                _write_exact(root / "artifacts" / artifact, content)
                _write_exact(root / "lead-result.txt", content)
                mission_state.append_event(
                    runs_dir,
                    mission_id,
                    kind="lead_result_recorded",
                    actor="CONTROL",
                    idempotency_key="controller:lead:result",
                    payload={
                        "run_id": current["lead_run_id"],
                        "artifact_id": artifact,
                        "result_file": str(result_path),
                        "provider": str(legacy.get("provider", "")),
                        "model": str(legacy.get("model", "")),
                        "variant": legacy.get("variant"),
                    },
                )
                continue

            mission_state.append_event(
                runs_dir,
                mission_id,
                kind="mission_completing",
                actor="CONTROL",
                idempotency_key="controller:mission:completing",
                payload={"lead_artifact_id": current["lead_result"]["artifact_id"]},
            )
            continue

        if current["status"] == "completing":
            archive = fleet_archive.ArchiveBuilder(runs_dir, mission_id).create(manifest_path)
            index_path = root / "archive" / "archive-index.json"
            mission_state.append_event(
                runs_dir,
                mission_id,
                kind="archive_created",
                actor="CONTROL",
                idempotency_key="controller:archive:unified",
                payload={
                    "path": str(index_path.relative_to(root)),
                    "sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
                    "mode": "unified",
                },
            )
            if not archive["valid"]:
                raise MissionRunError("unified archive verifier returned invalid")
            continue

        if current["status"] == "archived":
            if current.get("approval") is not None:
                audit_lifecycle = fleet_audit_client.AuditLifecycle(runs_dir, mission_id)
                audit_lifecycle.verify()
            mission_state.append_terminal(
                runs_dir,
                mission_id,
                status="succeeded",
                reason="lead result accepted and unified archive verified",
                idempotency_key="controller:terminal:succeeded",
            )
            manifest = parse_manifest(manifest_path)
            cmux_signal(manifest, mission_id, status="complete", progress=1.0, message="mission succeeded", notify=True)
            continue

        if current["status"] == "assured_running":
            fleet_control_service.ControlLifecycle(runs_dir, mission_id).start()
            spec_path = root / "assured-task-spec.json"
            spec = {
                "objective": objective,
                "negative_scope": [
                    "Do not modify systems outside the registered target repository.",
                    "Do not expose credentials, private data, or untracked terminal content.",
                ],
                "acceptance_criteria": [
                    "The exact mission objective is satisfied by a clean committed writer branch.",
                    "Relevant tests and controller verification gates pass with durable evidence.",
                    "FDP-3 challenge and independent verification reach a terminal verified state.",
                ],
            }
            _write_exact(spec_path, mission_state.canonical_bytes(spec) + b"\n")
            try:
                assured = fleet_assured_runner.AssuredRunner(runs_dir, mission_id).drive(spec_path)
            except (
                fleet_assured_runner.AssuredRunnerError,
                fleet_assured_runner.fdp2.ControllerError,
                fleet_assured_runner.fdp3.AssuranceError,
                fleet_assured_runner.fleet_dialogue.DialogueError,
            ) as exc:
                mission_state.append_terminal(
                    runs_dir,
                    mission_id,
                    status="indeterminate",
                    reason=f"assured runner failed closed: {exc}",
                    idempotency_key="controller:terminal:assured",
                )
                continue
            synthesis = assured.get("synthesis")
            if not isinstance(synthesis, dict):
                raise MissionRunError("assured runner returned no Lead synthesis")
            run_id = str(synthesis.get("run_id", ""))
            prompt_sha = str(synthesis.get("prompt_sha256", ""))
            if current["lead_run_id"] is None:
                mission_state.append_event(
                    runs_dir,
                    mission_id,
                    kind="lead_dispatched",
                    actor="CONTROL",
                    idempotency_key="controller:assured:lead",
                    payload={"run_id": run_id, "prompt_sha256": prompt_sha},
                )
                current = fleet_mission.load_state(runs_dir, mission_id)
            if current["lead_run_id"] != run_id:
                raise MissionRunError("assured synthesis Lead run drift")
            if current["lead_result"] is None:
                legacy = exact_legacy_run(runs_dir, feature, run_id)
                if legacy.get("status") != "succeeded":
                    raise MissionRunError("assured synthesis lacks durable success evidence")
                result_path = Path(str(synthesis.get("result_file", "")))
                artifact = fleet_artifacts.put_file(runs_dir, mission_id, result_path)
                _write_exact(root / "lead-result.txt", result_path.read_bytes())
                mission_state.append_event(
                    runs_dir,
                    mission_id,
                    kind="lead_result_recorded",
                    actor="CONTROL",
                    idempotency_key="controller:assured:lead-result",
                    payload={
                        "run_id": run_id,
                        "artifact_id": artifact["artifact_id"],
                        "result_file": str(result_path),
                        "provider": str(legacy.get("provider", "")),
                        "model": str(legacy.get("model", "")),
                        "variant": legacy.get("variant"),
                    },
                )
                continue
            mission_state.append_event(
                runs_dir,
                mission_id,
                kind="mission_completing",
                actor="CONTROL",
                idempotency_key="controller:mission:completing",
                payload={"lead_artifact_id": current["lead_result"]["artifact_id"]},
            )
            continue
        raise MissionRunError(f"unsupported mission state: {current['status']}")


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
) -> dict[str, Any]:
    if not FEATURE.fullmatch(feature):
        raise MissionRunError("invalid feature")
    try:
        execution_profile = fleet_manifest.validate_profile(execution_profile)
    except fleet_manifest.ManifestError as exc:
        raise MissionRunError(str(exc)) from exc
    require_success(["git", "-C", str(target_repo), "rev-parse", "--git-dir"])
    compiled = workflow_config.compile_path(workflow_path(workflow_name))
    timeout = timeout_seconds or int(compiled["workflow"]["limits"]["deadline_seconds"])
    if timeout < 60:
        raise MissionRunError("timeout must be at least 60 seconds")
    objective_hash = mission_state.artifact_id(objective)
    key = f"mission:{feature}:{compiled['workflow_digest'][:16]}:{objective_hash[:16]}"
    manifest_path = runs_dir / f"fleet-{feature}.manifest"
    if git_is_dirty(target_repo) and not allow_dirty_baseline and not manifest_path.exists():
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
            "risk_override": risk_override,
            "timeout_seconds": timeout,
            "teardown": teardown,
            "allow_dirty_baseline": allow_dirty_baseline,
            "execution_profile": execution_profile,
        },
    )
    return drive_mission(runs_dir, mission_id)


def dry_run(
    *,
    feature: str,
    objective: str,
    workflow_name: str,
    target_repo: Path,
    risk_override: str,
    timeout_seconds: int | None,
    execution_profile: str = "native",
) -> dict[str, Any]:
    try:
        execution_profile = fleet_manifest.validate_profile(execution_profile)
    except fleet_manifest.ManifestError as exc:
        raise MissionRunError(str(exc)) from exc
    compiled = workflow_config.compile_path(workflow_path(workflow_name))
    assessment = fleet_risk.assess(
        workflow_minimum=compiled["workflow"]["risk"]["minimum"],
        objective=objective,
        target=str(target_repo),
        override=risk_override,
    )
    timeout = timeout_seconds or int(compiled["workflow"]["limits"]["deadline_seconds"])
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
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    commands = parser.add_subparsers(dest="command", required=True)

    for name in ("run", "dry"):
        command = commands.add_parser(name)
        command.add_argument("feature")
        command.add_argument("objective")
        command.add_argument("--workflow", default="implementation")
        command.add_argument("--target-repo", default=os.getcwd())
        command.add_argument("--risk", default="auto", choices=("auto", *fleet_risk.RISK_ORDER))
        command.add_argument("--timeout", type=int)
        command.add_argument("--json", action="store_true")
        command.add_argument(
            "--execution-profile",
            default="native",
            choices=fleet_manifest.EXECUTION_PROFILES,
        )
        if name == "run":
            command.add_argument("--allow-dirty-baseline", action="store_true")
            command.add_argument("--teardown", action="store_true")

    resume = commands.add_parser("resume")
    resume.add_argument("--mission-id", required=True)
    resume.add_argument("--json", action="store_true")
    show = commands.add_parser("show")
    show.add_argument("--mission-id", required=True)

    assurance = commands.add_parser("request-assurance")
    assurance.add_argument("--mission-id", required=True)
    assurance.add_argument("--risk", choices=("high", "unknown"), required=True)
    assurance.add_argument("--categories", required=True)
    assurance.add_argument("--reason", required=True)
    return parser


def emit(value: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


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
                execution_profile=args.execution_profile,
            )
            emit(value, json_mode=args.json)
            return 3 if value["status"] == "awaiting_assurance_confirmation" else 0
        if args.command == "resume":
            value = drive_mission(runs_dir, args.mission_id)
            emit(value, json_mode=args.json)
            return 3 if value["status"] == "awaiting_assurance_confirmation" else 0
        if args.command == "show":
            emit(fleet_mission.load_state(runs_dir, args.mission_id), json_mode=False)
            return 0
        if args.command == "request-assurance":
            categories = sorted({item.strip() for item in args.categories.split(",") if item.strip()})
            if not categories:
                raise MissionRunError("--categories must contain at least one value")
            value = request_assurance(
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
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
