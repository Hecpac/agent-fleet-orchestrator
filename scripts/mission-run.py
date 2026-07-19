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

import fleet_admission
import fleet_artifacts
import fleet_archive
import fleet_audit_client
import fleet_assured_runner
import fleet_control
import fleet_control_service
import fleet_json
import fleet_ledger
import fleet_manifest
import fleet_mission
import fleet_mission_state as mission_state
import fleet_providers
import fleet_safe_paths
import fleet_tracking
import workflow_config


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = ROOT / "orchestration" / "runs"
PROMPT_TEMPLATE = ROOT / "orchestration" / "prompts" / "mission_lead.md"
FEATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WORKFLOW = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
HANDOFF_READY_TIMEOUT_ENV = "FLEET_HANDOFF_READY_TIMEOUT_SECONDS"
HANDOFF_COMMIT_TIMEOUT_ENV = "FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS"
BOOT_LOCK_READY_TIMEOUT_ENV = "FLEET_BOOT_LOCK_READY_TIMEOUT_SECONDS"
BOOT_LOCK_READY_TIMEOUT_DEFAULT = 60
HANDOFF_READY_TIMEOUT_DEFAULT = 60
HANDOFF_COMMIT_TIMEOUT_DEFAULT = 300
BOOT_LOCK_READY_TIMEOUT_MAX = 600
HANDOFF_READY_TIMEOUT_MAX = 600
HANDOFF_COMMIT_TIMEOUT_MAX = 3600
# READY and COMMIT bound only the child protocol.  fleet-down still needs time
# for its descriptor checks, CMUX quiescence, publication, and exact cleanup.
HANDOFF_TEARDOWN_GRACE_SECONDS = 180
RISK_SPEC = importlib.util.spec_from_file_location(
    "fleet_risk", ROOT / "scripts" / "fleet-risk.py"
)
assert RISK_SPEC and RISK_SPEC.loader
fleet_risk = importlib.util.module_from_spec(RISK_SPEC)
RISK_SPEC.loader.exec_module(fleet_risk)


class MissionRunError(RuntimeError):
    """Mission runner reconciliation cannot proceed safely."""


def _handoff_timeout_value(
    environment: Mapping[str, str],
    *,
    name: str,
    default: int,
    maximum: int,
) -> int:
    raw = environment.get(name, str(default))
    if re.fullmatch(r"[1-9][0-9]{0,3}", raw) is None:
        raise MissionRunError(f"invalid {name} {raw!r}")
    value = int(raw)
    if value > maximum:
        raise MissionRunError(f"invalid {name} {raw!r}")
    return value


def assurance_handoff_command_timeout(environment: Mapping[str, str]) -> int:
    """Keep the outer supervisor strictly beyond all internal deadlines."""

    boot = _handoff_timeout_value(
        environment,
        name=BOOT_LOCK_READY_TIMEOUT_ENV,
        default=BOOT_LOCK_READY_TIMEOUT_DEFAULT,
        maximum=BOOT_LOCK_READY_TIMEOUT_MAX,
    )
    ready = _handoff_timeout_value(
        environment,
        name=HANDOFF_READY_TIMEOUT_ENV,
        default=HANDOFF_READY_TIMEOUT_DEFAULT,
        maximum=HANDOFF_READY_TIMEOUT_MAX,
    )
    commit = _handoff_timeout_value(
        environment,
        name=HANDOFF_COMMIT_TIMEOUT_ENV,
        default=HANDOFF_COMMIT_TIMEOUT_DEFAULT,
        maximum=HANDOFF_COMMIT_TIMEOUT_MAX,
    )
    return boot + ready + commit + HANDOFF_TEARDOWN_GRACE_SECONDS


def _approval_expires_at(current: dict[str, Any]) -> datetime:
    approval = current.get("approval")
    if not isinstance(approval, dict):
        raise MissionRunError("assurance transition lacks scoped approval")
    try:
        return mission_state.parse_timestamp(
            approval["expires_at"], "assurance approval expiry"
        )
    except (KeyError, mission_state.MissionStateError) as exc:
        raise MissionRunError("assurance approval expiry is invalid") from exc


def _require_live_approval(
    current: dict[str, Any], *, minimum_remaining_seconds: int = 0
) -> None:
    expires_at = _approval_expires_at(current)
    threshold = datetime.now(timezone.utc) + timedelta(
        seconds=minimum_remaining_seconds
    )
    if threshold >= expires_at:
        raise MissionRunError(
            "assurance approval is expired or too close to expiry for this effect"
        )


def enforce_audit_trust(
    compiled: dict[str, Any], categories: list[str], execution_profile: str
) -> None:
    """Prevent runtime risk/profile requirements from accepting weaker audit policy."""
    policy = compiled["workflow"]["audit"]
    category_set = set(categories)
    worm_categories = category_set & set(policy["worm_required_for"])
    external_required = execution_profile == "regulated" or "regulated" in category_set
    if worm_categories and policy["mode"] != "worm":
        raise MissionRunError(
            "mission risk requires WORM audit for categories: "
            + ", ".join(sorted(worm_categories))
        )
    if external_required and (
        policy["mode"] != "worm" or policy["trust_scope"] != "external-compliance"
    ):
        raise MissionRunError(
            "regulated execution or risk requires worm mode with external-compliance trust"
        )


def run_process(
    command: list[str],
    *,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env or os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise MissionRunError(
            f"command failed to run: {Path(command[0]).name}: {exc}"
        ) from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        raise MissionRunError(
            f"command timed out: {Path(command[0]).name}: {exc}"
        ) from exc
    except BaseException:
        _terminate_process_group(process)
        raise
    return subprocess.CompletedProcess(
        command,
        int(process.returncode),
        stdout,
        stderr,
    )


def _terminate_process_group(
    process: subprocess.Popen[str], *, grace_seconds: float = 5.0
) -> None:
    """Terminate and reap the exact session created by :func:`run_process`."""

    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            process.communicate()
            return
        time.sleep(0.05)
    # A reaped leader or closed pipe does not prove its process group is empty:
    # a descendant may have closed stdio and ignored SIGTERM.  Always target the
    # original PGID after grace while it still exists.
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate()


def require_success(
    command: list[str],
    *,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = run_process(command, timeout=timeout, env=env)
    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit {result.returncode}"
        )
        raise MissionRunError(f"{Path(command[0]).name} failed: {detail}")
    return result


def last_json_object(text: str, source: str) -> dict[str, Any]:
    try:
        value = fleet_json.loads(text)
    except fleet_json.FleetJSONError as exc:
        raise MissionRunError(f"{source} returned invalid strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise MissionRunError(f"{source} returned a non-object JSON value")
    return value


def parse_manifest(path: Path) -> dict[str, str]:
    try:
        parent = path.parent.resolve(strict=True)
        with fleet_safe_paths.RootedFS(parent) as rooted:
            raw = rooted.read_regular(
                path.name,
                directory_modes=(),
                file_mode=0o600,
                max_bytes=16 * 1024 * 1024,
                require_single_link=True,
            )
            rooted.assert_root_binding()
        return fleet_manifest.normalize(fleet_manifest.parse_bytes(raw))
    except (
        OSError,
        RuntimeError,
        fleet_manifest.ManifestError,
        fleet_safe_paths.SafePathError,
    ) as exc:
        raise MissionRunError(f"cannot read fleet manifest {path}: {exc}") from exc


def verify_manifest_binding(manifest: dict[str, str], compiled: dict[str, Any]) -> None:
    try:
        fleet_manifest.verify_compiled_binding(manifest, compiled)
    except fleet_manifest.ManifestError as exc:
        raise MissionRunError(f"compiled manifest binding drift: {exc}") from exc


def verify_manifest_repository_binding(
    manifest: dict[str, str], *, target_repo: Path, base_sha: str
) -> None:
    if manifest.get("target_repo") != str(target_repo):
        raise MissionRunError("fleet manifest target_repo drift")
    if manifest.get("base_sha") != base_sha:
        raise MissionRunError("fleet manifest base_sha drift")
    writers = {
        key.removesuffix(".authority")
        for key, value in manifest.items()
        if key.endswith(".authority") and value == "write"
    }
    for writer in writers:
        if manifest.get(f"{writer}.base_sha") != base_sha:
            raise MissionRunError(f"fleet manifest writer base_sha drift: {writer}")


def load_durable_json(
    runs_dir: Path,
    relative: Path,
    *,
    directory_modes: tuple[int, ...],
) -> dict[str, Any]:
    """Read one mission-owned JSON object through its descriptor-bound root."""

    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            raw = rooted.read_regular(
                relative,
                directory_modes=directory_modes,
                file_mode=0o600,
                max_bytes=16 * 1024 * 1024,
                require_single_link=True,
            )
            rooted.assert_root_binding()
        value = fleet_json.loads(raw)
    except (fleet_safe_paths.SafePathError, fleet_json.FleetJSONError) as exc:
        raise MissionRunError(f"cannot load durable JSON {relative}: {exc}") from exc
    if not isinstance(value, dict):
        raise MissionRunError(f"expected durable JSON object: {relative}")
    if raw != fleet_json.canonical_bytes(value) + b"\n":
        raise MissionRunError(f"durable JSON bytes are not canonical: {relative}")
    return value


def load_mission_text(
    runs_dir: Path,
    mission_id: str,
    leaf: str,
    *,
    max_bytes: int,
) -> str:
    relative = Path("missions") / mission_id / leaf
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            raw = rooted.read_regular(
                relative,
                directory_modes=(0o700, 0o700),
                file_mode=0o600,
                max_bytes=max_bytes,
                require_single_link=True,
            )
            rooted.assert_root_binding()
        return raw.decode("utf-8", errors="strict")
    except (fleet_safe_paths.SafePathError, UnicodeDecodeError) as exc:
        raise MissionRunError(
            f"cannot load durable mission text {leaf}: {exc}"
        ) from exc


def git_value(repo: Path, *args: str) -> str:
    return require_success(["git", "-C", str(repo), *args]).stdout.strip()


def git_is_dirty(repo: Path) -> bool:
    return bool(git_value(repo, "status", "--porcelain"))


def git_read(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise MissionRunError(result.stderr.strip() or "Git repository read failed")
    return result.stdout.strip()


def exact_git_toplevel(repo: Path) -> Path:
    try:
        physical = repo.expanduser().resolve(strict=True)
    except OSError as exc:
        raise MissionRunError(f"target repository is unavailable: {repo}") from exc
    top = Path(
        git_read(physical, "rev-parse", "--path-format=absolute", "--show-toplevel")
    )
    try:
        physical_top = top.resolve(strict=True)
    except OSError as exc:
        raise MissionRunError("Git toplevel is unavailable") from exc
    if physical != physical_top:
        raise MissionRunError(
            f"target_repo must be the exact physical Git toplevel: {physical_top}"
        )
    return physical_top


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
        [
            "cmux",
            "set-status",
            "mission",
            label,
            "--workspace",
            workspace,
            "--icon",
            "sparkles",
        ],
        [
            "cmux",
            "set-progress",
            f"{progress:.2f}",
            "--label",
            label,
            "--workspace",
            workspace,
        ],
        [
            "cmux",
            "log",
            "--level",
            "info",
            "--source",
            "mission-control",
            "--workspace",
            workspace,
            f"mission_id={mission_id} {message}",
        ],
    ]
    if notify:
        commands.append(
            [
                "cmux",
                "notify",
                "--title",
                f"Mission {mission_id[:8]}: {status}",
                "--workspace",
                workspace,
            ]
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
    try:
        return fleet_ledger.read_records(path)
    except fleet_ledger.LedgerError as exc:
        raise MissionRunError(
            f"legacy fleet ledger is unsafe or corrupt: {exc}"
        ) from exc


def reconcile_lead_run(
    runs_dir: Path,
    feature: str,
    prompt_sha256: str,
) -> dict[str, Any] | None:
    events = legacy_events(runs_dir, feature)
    candidates: set[str] = set()
    for event in events:
        if event.get("instance") != "lead" or event.get("task_sha256") != prompt_sha256:
            continue
        run_id = str(event.get("run_id", ""))
        try:
            uuid.UUID(run_id)
        except ValueError:
            raise MissionRunError("legacy lead run has invalid run_id")
        candidates.add(run_id)
    if len(candidates) > 1:
        raise MissionRunError(
            "multiple legacy lead runs match one mission dispatch intent"
        )
    if not candidates:
        return None
    run_id = next(iter(candidates))
    matches = [
        event
        for event in events
        if event.get("instance") == "lead" and event.get("run_id") == run_id
    ]
    manifest = parse_manifest(runs_dir / f"fleet-{feature}.manifest")
    try:
        return fleet_tracking.verify_run_events(
            matches,
            required_protocol=manifest.get("tracking_protocol", "legacy-cmux"),
        )
    except fleet_tracking.TrackingError as exc:
        raise MissionRunError(f"reconciled lead provenance invalid: {exc}") from exc


def exact_legacy_run(
    runs_dir: Path,
    feature: str,
    run_id: str,
    *,
    prompt_sha256: str | None = None,
) -> dict[str, Any]:
    matches = [
        event
        for event in legacy_events(runs_dir, feature)
        if event.get("instance") == "lead" and event.get("run_id") == run_id
    ]
    if not matches:
        raise MissionRunError(f"missing legacy evidence for lead run {run_id}")
    if prompt_sha256 is not None and any(
        event.get("task_sha256") != prompt_sha256 for event in matches
    ):
        raise MissionRunError(
            "Lead task_sha256 differs from its dispatch prompt_sha256"
        )
    manifest = parse_manifest(runs_dir / f"fleet-{feature}.manifest")
    try:
        return fleet_tracking.verify_run_events(
            matches,
            required_protocol=manifest.get("tracking_protocol", "legacy-cmux"),
        )
    except fleet_tracking.TrackingError as exc:
        raise MissionRunError(f"lead tracked result provenance invalid: {exc}") from exc


def terminal_evidence(
    terminal_event: dict[str, Any],
    *,
    run_id: str,
    task_sha256: str,
    status: str,
) -> dict[str, Any]:
    """Bind a verified external terminal event to one exact admission task."""

    if (
        terminal_event.get("run_id") != run_id
        or terminal_event.get("task_sha256") != task_sha256
        or terminal_event.get("status") != status
        or not mission_state.SHA256.fullmatch(task_sha256)
    ):
        raise MissionRunError(
            "terminal lifecycle evidence differs from its exact run/task/status"
        )
    return {
        "schema_version": 1,
        "source_event_sha256": mission_state.sha256(terminal_event),
        "run_id": run_id,
        "task_sha256": task_sha256,
        "status": status,
    }


def _lead_admission(
    runs_dir: Path,
    mission_id: str,
    *,
    effect_sha256: str,
    task_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reserve and return the one deterministic Mission Lead admission."""

    request = {
        "request_key": "mission-lead",
        "run_kind": "lead",
        "recipient_instance": "lead",
        "capability": "control",
        "parent_admission_id": None,
        "parent_run_id": None,
        "delegated_budget": 0,
        "writer": False,
        "effect_sha256": effect_sha256,
        "task_sha256": task_sha256,
    }
    try:
        reserved = fleet_admission.reserve_many(
            runs_dir,
            mission_id,
            requests=[request],
            idempotency_key="admission:lead:reserve",
        )
    except mission_state.MissionStateError as exc:
        raise MissionRunError(f"Lead admission reservation failed: {exc}") from exc
    return request, reserved["admissions"][0]


def _start_lead_after_effect(
    runs_dir: Path,
    mission_id: str,
    admission: dict[str, Any],
    *,
    authorization_event_sha256: str,
) -> None:
    """Reconcile the accepted wrapper effect into a durable started owner."""

    current = fleet_mission.load_state(runs_dir, mission_id)
    durable = current["admissions"].get(admission["admission_id"])
    if durable is None:
        raise MissionRunError("accepted Lead effect lost admission ownership")
    if durable["phase"] == "started":
        return
    if durable["phase"] != "authorized":
        raise MissionRunError(
            f"accepted Lead effect has terminal admission phase {durable['phase']}"
        )
    try:
        fleet_admission.mark_started(
            runs_dir,
            mission_id,
            admission_id=durable["admission_id"],
            authorization_event_sha256=authorization_event_sha256,
            request_digest=durable["request_digest"],
            effect_sha256=durable["effect_sha256"],
            recipient_instance="lead",
            writer=False,
            run_id=durable["run_id"],
            idempotency_key="admission:lead:start",
        )
    except mission_state.MissionStateError as exc:
        raise MissionRunError(
            "Lead wrapper accepted but durable start failed; the exact "
            "authorization remains active for reconciliation"
        ) from exc


def _finalize_lead(
    runs_dir: Path,
    mission_id: str,
    *,
    reason: str,
    terminal_evidence: dict[str, Any],
) -> None:
    current = fleet_mission.load_state(runs_dir, mission_id)
    admission_id = current.get("lead_admission_id")
    if not isinstance(admission_id, str):
        raise MissionRunError("Lead terminal evidence lacks admission ownership")
    durable = current["admissions"].get(admission_id)
    if durable is None:
        raise MissionRunError("Lead admission disappeared before finalization")
    if durable["phase"] == "finalized":
        if durable["terminal"].get("terminal_evidence") != terminal_evidence:
            raise MissionRunError("Lead terminal admission is immutable")
        return
    try:
        fleet_admission.finalize(
            runs_dir,
            mission_id,
            admission_id=admission_id,
            recipient_instance=durable["recipient_instance"],
            writer=bool(durable["writer"]),
            terminal_evidence=terminal_evidence,
            reason=reason,
            idempotency_key="admission:lead:finalize",
        )
    except mission_state.MissionStateError as exc:
        raise MissionRunError(f"Lead admission finalization failed: {exc}") from exc


def _finalize_run_admission(
    runs_dir: Path,
    mission_id: str,
    *,
    run_id: str,
    idempotency_key: str,
    terminal_evidence: dict[str, Any],
) -> None:
    current = fleet_mission.load_state(runs_dir, mission_id)
    owner = current["run_owners"].get(run_id)
    if owner is None or owner.get("owner_kind") != "admission":
        raise MissionRunError("terminal run lacks admission ownership")
    admission = current["admissions"].get(owner["owner_id"])
    if admission is None:
        raise MissionRunError("terminal run admission disappeared")
    if admission["phase"] == "finalized":
        if admission["terminal"].get("terminal_evidence") != terminal_evidence:
            raise MissionRunError("run admission terminal is immutable")
        return
    try:
        fleet_admission.finalize(
            runs_dir,
            mission_id,
            admission_id=admission["admission_id"],
            recipient_instance=admission["recipient_instance"],
            writer=bool(admission["writer"]),
            terminal_evidence=terminal_evidence,
            reason=(f"durable run terminal status: {terminal_evidence['status']}"),
            idempotency_key=idempotency_key,
            actor=str(admission.get("lane_actor") or "CONTROL"),
        )
    except mission_state.MissionStateError as exc:
        raise MissionRunError(f"run admission finalization failed: {exc}") from exc


def exact_lead_result_relative(
    runs_dir: Path, feature: str, run_id: str, recorded: Any
) -> Path:
    relative = Path("results") / feature / f"{run_id}.txt"
    expected = runs_dir / relative
    if not isinstance(recorded, str) or recorded != str(expected):
        raise MissionRunError(
            f"lead result_file must equal the exact fleet result path: {expected}"
        )
    return relative


def terminal_lead_result(
    runs_dir: Path, mission_id: str, current: dict[str, Any]
) -> tuple[str, str]:
    """Return the terminal Lead result only after re-binding it to CAS."""
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    recorded = current.get("synthesis_result") or current.get("lead_result")
    if recorded is None:
        if current.get("status") == "succeeded":
            raise MissionRunError("succeeded mission lacks a recorded synthesis result")
        return "", ""
    if not isinstance(recorded, dict):
        raise MissionRunError("terminal mission has invalid Lead result metadata")
    artifact_id = recorded.get("artifact_id")
    if not isinstance(artifact_id, str) or not mission_state.SHA256.fullmatch(
        artifact_id
    ):
        raise MissionRunError("terminal mission has invalid Lead artifact_id")
    relative = Path("missions") / mission_id / "lead-result.txt"
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            if rooted.root != runs_dir:
                raise fleet_safe_paths.SafePathError(
                    "runs_dir is not its exact physical root"
                )
            content = rooted.read_regular(
                relative,
                directory_modes=(0o700, 0o700),
                file_mode=0o600,
                max_bytes=fleet_artifacts.MAX_ARTIFACT_BYTES,
            )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise MissionRunError(f"unsafe terminal Lead result: {exc}") from exc
    if hashlib.sha256(content).hexdigest() != artifact_id:
        raise MissionRunError("terminal Lead result does not match its artifact_id")
    try:
        canonical = fleet_artifacts.get_bytes(runs_dir, mission_id, artifact_id)
    except fleet_artifacts.ArtifactError as exc:
        raise MissionRunError(f"cannot verify terminal Lead artifact: {exc}") from exc
    if canonical != content:
        raise MissionRunError("terminal Lead result disagrees with its CAS artifact")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MissionRunError("terminal Lead result is not UTF-8") from exc
    return str(runs_dir / relative), text


def _write_exact(path: Path, content: bytes) -> None:
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise MissionRunError(f"durable mission artifact conflicts: {path.name}")
        return
    mission_state.atomic_write(path, content)


def effect_compiled(
    runs_dir: Path, mission_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return fleet_mission.load_mission_compiled(runs_dir, mission_id, mode="effect")
    except (fleet_mission.MissionError, mission_state.MissionStateError) as exc:
        raise MissionRunError(
            f"compiled workflow is not effect-authorized: {exc}"
        ) from exc


def request_assurance(
    runs_dir: Path,
    mission_id: str,
    *,
    requested_risk: str,
    categories: list[str],
    reason: str,
) -> dict[str, Any]:
    _, current = effect_compiled(runs_dir, mission_id)
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
    compiled, initial = effect_compiled(runs_dir, mission_id)
    options = load_durable_json(
        runs_dir,
        Path("missions") / mission_id / "runtime-options.json",
        directory_modes=(0o700, 0o700),
    )
    objective = load_mission_text(
        runs_dir,
        mission_id,
        "objective.txt",
        max_bytes=16 * 1024 * 1024,
    )
    target_repo = exact_git_toplevel(Path(initial["target_repo"]))
    if str(target_repo) != initial["target_repo"]:
        raise MissionRunError(
            "mission target_repo is not the exact physical Git toplevel"
        )
    frozen_base_sha = str(initial["base_sha"])
    if git_read(target_repo, "rev-parse", "--verify", "HEAD") != frozen_base_sha:
        raise MissionRunError("target HEAD drifted from the mission frozen base_sha")
    manifest_path = runs_dir / f"fleet-{initial['feature']}.manifest"
    timeout_seconds = int(
        options.get("timeout_seconds")
        or compiled["workflow"]["limits"]["deadline_seconds"]
    )
    execution_profile = fleet_manifest.validate_profile(
        str(options.get("execution_profile", "native"))
    )
    enforce_audit_trust(compiled, [], execution_profile)
    main_preset = str(compiled["resolved"]["preset"])
    assurance_preset = str(compiled["resolved"]["assurance_preset"])

    def control_lifecycle_for(preset: str) -> fleet_control_service.ControlLifecycle:
        return fleet_control_service.ControlLifecycle(
            runs_dir, mission_id, preset=preset
        )

    def abort_assurance_boot(
        control_lifecycle: fleet_control_service.ControlLifecycle,
        *,
        reason: str,
    ) -> None:
        mission_state.append_terminal(
            runs_dir,
            mission_id,
            status="indeterminate",
            reason=reason,
            idempotency_key="controller:terminal:assurance-boot",
        )
        if manifest_path.exists():
            require_success(
                [str(ROOT / "scripts" / "fleet-down.sh"), feature],
                timeout=180,
                env={**os.environ, "FLEET_RUNS_DIR": str(runs_dir)},
            )
        else:
            control_lifecycle.stop_if_present()

    while True:
        current = fleet_mission.load_state(runs_dir, mission_id)
        enforce_audit_trust(
            compiled, list(current.get("risk_categories") or []), execution_profile
        )
        feature = current["feature"]
        if current["status"] in mission_state.TERMINAL_STATUSES:
            result_file, result_text = terminal_lead_result(
                runs_dir, mission_id, current
            )
            terminal_preset = main_preset
            if manifest_path.exists():
                terminal_preset = parse_manifest(manifest_path).get(
                    "preset", main_preset
                )
            control_lifecycle = control_lifecycle_for(terminal_preset)
            if control_lifecycle.lifecycle_path.exists():
                lifecycle = load_durable_json(
                    runs_dir,
                    control_lifecycle.lifecycle_path.relative_to(runs_dir),
                    directory_modes=(0o700, 0o700, 0o700),
                )
                if lifecycle.get("stopped_at") is None:
                    control_lifecycle.stop()
            if options.get("teardown") and manifest_path.exists():
                require_success(
                    [str(ROOT / "scripts" / "fleet-down.sh"), feature],
                    timeout=180,
                    env={**os.environ, "FLEET_RUNS_DIR": str(runs_dir)},
                )
            else:
                audit_lifecycle = fleet_audit_client.AuditLifecycle(
                    runs_dir, mission_id
                )
                if audit_lifecycle.lifecycle_path.exists():
                    lifecycle = load_durable_json(
                        runs_dir,
                        Path("missions") / mission_id / "audit" / "lifecycle.json",
                        directory_modes=(0o700, 0o700, 0o700),
                    )
                    if lifecycle.get("stopped_at") is None:
                        audit_lifecycle.stop()
            return {
                "mission_id": mission_id,
                "feature": feature,
                "status": current["status"],
                "result_file": result_file,
                "result": result_text,
                "head_sha256": current["head_sha256"],
            }

        if current["status"] == "compiled":
            assessment = fleet_risk.assess(
                workflow_minimum=compiled["workflow"]["risk"]["minimum"],
                objective=objective,
                target=str(target_repo),
                repository_root=str(target_repo),
                override=str(options.get("risk_override", "auto")),
            )
            enforce_audit_trust(
                compiled, list(assessment["categories"]), execution_profile
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
            if datetime.now(timezone.utc) >= _approval_expires_at(current):
                return {
                    "mission_id": mission_id,
                    "feature": feature,
                    "status": current["status"],
                    "next_action": (
                        f"python3 scripts/fleet-approve.py --runs-dir {runs_dir} "
                        f"--mission-id {mission_id} --scope {target_repo} --renew; "
                        "then resume"
                    ),
                }
            fleet_audit_client.AuditLifecycle(runs_dir, mission_id).preflight()
            lead_admission_id = current.get("lead_admission_id")
            if isinstance(lead_admission_id, str):
                lead_admission = current["admissions"].get(lead_admission_id)
                if not isinstance(lead_admission, dict):
                    raise MissionRunError(
                        "assurance handoff lost the principal Lead admission"
                    )
                if lead_admission["phase"] != "finalized":
                    if any(
                        admission.get("active")
                        and admission.get("parent_admission_id") == lead_admission_id
                        for admission in current["admissions"].values()
                    ):
                        return {
                            "mission_id": mission_id,
                            "feature": feature,
                            "status": current["status"],
                            "next_action": (
                                "wait for the principal Lead's exact child runs "
                                "to become terminal, then resume"
                            ),
                        }
                    lead_intents = [
                        event
                        for event in mission_state.read_events(
                            mission_state.ledger_path(runs_dir, mission_id),
                            expected_mission_id=mission_id,
                        )
                        if event["kind"] == "lead_dispatch_intent"
                    ]
                    if len(lead_intents) != 1:
                        raise MissionRunError(
                            "assurance handoff lacks one principal Lead intent"
                        )
                    lead_prompt_sha256 = lead_intents[0]["payload"]["prompt_sha256"]
                    lead_terminal = exact_legacy_run(
                        runs_dir,
                        feature,
                        lead_admission["run_id"],
                        prompt_sha256=lead_prompt_sha256,
                    )
                    lead_status = str(lead_terminal.get("status", ""))
                    if lead_status not in mission_state.TERMINAL_STATUSES:
                        return {
                            "mission_id": mission_id,
                            "feature": feature,
                            "status": current["status"],
                            "next_action": (
                                "wait for the principal Lead's exact run to "
                                "become terminal, then resume"
                            ),
                        }
                    _finalize_lead(
                        runs_dir,
                        mission_id,
                        reason="principal Lead retired for assured handoff",
                        terminal_evidence=terminal_evidence(
                            lead_terminal,
                            run_id=lead_admission["run_id"],
                            task_sha256=lead_prompt_sha256,
                            status=lead_status,
                        ),
                    )
                    current = fleet_mission.load_state(runs_dir, mission_id)
            active = [
                admission["admission_id"]
                for admission in current["admissions"].values()
                if admission.get("active")
            ]
            if active:
                raise MissionRunError(
                    "assurance handoff requires every main-fleet admission inactive"
                )
            if datetime.now(timezone.utc) >= _approval_expires_at(current):
                return {
                    "mission_id": mission_id,
                    "feature": feature,
                    "status": current["status"],
                    "next_action": (
                        f"python3 scripts/fleet-approve.py --runs-dir {runs_dir} "
                        f"--mission-id {mission_id} --scope {target_repo} --renew; "
                        "then resume from the durable assurance handoff receipt"
                    ),
                }
            needs_handoff = isinstance(lead_admission_id, str) or manifest_path.exists()
            if needs_handoff:
                handoff_environment = {
                    **os.environ,
                    "FLEET_RUNS_DIR": str(runs_dir),
                    "FLEET_MISSION_ID": mission_id,
                }
                handoff = require_success(
                    [
                        str(ROOT / "scripts" / "fleet-down.sh"),
                        feature,
                        "--handoff-assurance",
                    ],
                    timeout=assurance_handoff_command_timeout(handoff_environment),
                    env=handoff_environment,
                )
                handoff_value = last_json_object(
                    handoff.stdout, "fleet-down --handoff-assurance"
                )
                if any(
                    (
                        handoff_value.get("status") != "ready",
                        handoff_value.get("mission_id") != mission_id,
                        handoff_value.get("feature") != feature,
                    )
                ):
                    raise MissionRunError(
                        "assurance handoff receipt does not match this mission"
                    )
                for suffix in (
                    "manifest",
                    "state.json",
                    "ledger.jsonl",
                    "dialogue.jsonl",
                    "dialogue-control.jsonl",
                    "assurance-control.jsonl",
                    "verification-receipt.json",
                    "assurance-receipt.json",
                ):
                    path = runs_dir / f"fleet-{feature}.{suffix}"
                    if path.exists() or path.is_symlink():
                        raise MissionRunError(
                            "assurance handoff left active main-runtime state: "
                            f"{path.name}"
                        )
            if datetime.now(timezone.utc) >= _approval_expires_at(current):
                return {
                    "mission_id": mission_id,
                    "feature": feature,
                    "status": current["status"],
                    "next_action": (
                        f"python3 scripts/fleet-approve.py --runs-dir {runs_dir} "
                        f"--mission-id {mission_id} --scope {target_repo} --renew; "
                        "then resume from the durable assurance handoff receipt"
                    ),
                }
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
            preset = assurance_preset
            boot_env = {
                **os.environ,
                "FLEET_RUNS_DIR": str(runs_dir),
                "FLEET_MISSION_ID": mission_id,
            }
            control_lifecycle = control_lifecycle_for(preset)
            try:
                _require_live_approval(current)
            except MissionRunError:
                abort_assurance_boot(
                    control_lifecycle,
                    reason="assurance approval expired before assured boot effect",
                )
                continue
            control_lifecycle.start()
            boot_env["FLEET_CONTROL_SOCKET_DIR"] = str(control_lifecycle.socket_root)
            if not manifest_path.exists():
                require_success(
                    [
                        str(ROOT / "scripts" / "fleet-up.sh"),
                        feature,
                        "--preset",
                        preset,
                        "--target-repo",
                        str(target_repo),
                        "--expected-base-sha",
                        frozen_base_sha,
                        "--execution-profile",
                        execution_profile,
                        "--compiled-workflow",
                        str(root / "compiled-workflow.json"),
                    ],
                    timeout=300,
                    env=boot_env,
                )
            manifest = parse_manifest(manifest_path)
            verify_manifest_binding(manifest, compiled)
            verify_manifest_repository_binding(
                manifest, target_repo=target_repo, base_sha=frozen_base_sha
            )
            if manifest.get("mission_id") != mission_id:
                raise MissionRunError("assured manifest mission_id mismatch")
            if manifest.get("preset") != preset or manifest.get("mode") != "assured":
                raise MissionRunError("assured manifest preset or mode mismatch")
            if manifest.get("execution_profile") != execution_profile:
                raise MissionRunError("assured manifest execution_profile drift")
            audit_lifecycle = fleet_audit_client.AuditLifecycle(runs_dir, mission_id)
            audit_lifecycle.start(manifest_path)
            try:
                _require_live_approval(fleet_mission.load_state(runs_dir, mission_id))
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
            except mission_state.MissionStateError as exc:
                abort_assurance_boot(
                    control_lifecycle,
                    reason=(f"assurance start rejected after external boot: {exc}"),
                )
                continue
            except MissionRunError:
                abort_assurance_boot(
                    control_lifecycle,
                    reason="assurance approval expired during assured boot",
                )
                continue
            started = fleet_mission.load_state(runs_dir, mission_id)
            audit_lifecycle.record_control_event(
                event_type="MissionEvent",
                subject_id=started["head_sha256"][:32],
                subject_sha256=started["head_sha256"],
                metadata={
                    "kind": "assurance_started",
                    "sequence": started["last_sequence"],
                },
                idempotency_key="mission:assurance-started",
            )
            cmux_signal(
                manifest,
                mission_id,
                status="assured",
                progress=0.20,
                message="assured fleet boot reconciled",
            )
            continue

        if current["status"] == "booting":
            preset = main_preset
            boot_env = {
                **os.environ,
                "FLEET_RUNS_DIR": str(runs_dir),
                "FLEET_MISSION_ID": mission_id,
            }
            control_lifecycle = control_lifecycle_for(preset)
            control_lifecycle.start()
            boot_env["FLEET_CONTROL_SOCKET_DIR"] = str(control_lifecycle.socket_root)
            if not manifest_path.exists():
                require_success(
                    [
                        str(ROOT / "scripts" / "fleet-up.sh"),
                        feature,
                        "--preset",
                        preset,
                        "--target-repo",
                        str(target_repo),
                        "--expected-base-sha",
                        frozen_base_sha,
                        "--execution-profile",
                        execution_profile,
                        "--compiled-workflow",
                        str(root / "compiled-workflow.json"),
                    ],
                    timeout=300,
                    env=boot_env,
                )
            manifest = parse_manifest(manifest_path)
            verify_manifest_binding(manifest, compiled)
            verify_manifest_repository_binding(
                manifest, target_repo=target_repo, base_sha=frozen_base_sha
            )
            if manifest.get("mission_id") != mission_id:
                raise MissionRunError("fleet manifest is not bound to this mission_id")
            if manifest.get("preset") != preset:
                raise MissionRunError("fleet manifest preset drift")
            if manifest.get("mode") != "autonomous":
                raise MissionRunError(
                    "canonical autonomous mission resolved a non-autonomous fleet"
                )
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
            cmux_signal(
                manifest,
                mission_id,
                status="running",
                progress=0.15,
                message="fleet boot reconciled",
            )
            continue

        if current["status"] == "running":
            control_lifecycle = control_lifecycle_for(main_preset)
            control_lifecycle.start()
            manifest = parse_manifest(manifest_path)
            verify_manifest_binding(manifest, compiled)
            verify_manifest_repository_binding(
                manifest, target_repo=target_repo, base_sha=frozen_base_sha
            )
            if (
                manifest.get("mission_id") != mission_id
                or manifest.get("preset") != main_preset
            ):
                raise MissionRunError("running fleet manifest binding drift")
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
            lead_member = compiled["resolved"]["lead"]
            lead_hook_source = (
                lead_member.get("hook_source")
                or manifest.get("lead.hook_source")
                or lead_member["provider_adapter"]
            )
            try:
                lead_provider_identity = fleet_providers.identity(
                    lead_member["provider"],
                    lead_member["model"],
                    lead_member.get("variant"),
                    lead_hook_source,
                )
            except fleet_providers.ProviderError as exc:
                raise MissionRunError(
                    "Lead provider identity is invalid before admission"
                ) from exc
            lead_effect_sha256 = mission_state.sha256(
                {
                    "schema_version": 1,
                    "objective_sha256": mission_state.artifact_id(objective),
                    "prompt_sha256": prompt_sha,
                    "input_artifact_ids": [],
                    "expected_output_contract": {
                        "type": "mission-lead-result",
                        "required": [
                            "STATUS",
                            "DECISION",
                            "ARTIFACTS",
                            "VERIFICATION",
                            "RISKS",
                            "NEXT_ACTION",
                        ],
                    },
                    "grants": {
                        "capability": "control",
                        "delegated_budget": 0,
                        "writer": False,
                    },
                    "provider_identity": {
                        "provider": lead_provider_identity.provider,
                        "model": lead_provider_identity.model,
                        "variant": lead_provider_identity.variant,
                        "hook_source": lead_provider_identity.hook_source,
                    },
                    "runner": manifest.get("lead.runner"),
                }
            )
            if current["lead_run_id"] is None:
                _, lead_admission = _lead_admission(
                    runs_dir,
                    mission_id,
                    effect_sha256=lead_effect_sha256,
                    task_sha256=prompt_sha,
                )
                mission_state.append_event(
                    runs_dir,
                    mission_id,
                    kind="lead_dispatch_intent",
                    actor="CONTROL",
                    idempotency_key="controller:lead:intent",
                    payload={"prompt_sha256": prompt_sha},
                )
                current = fleet_mission.load_state(runs_dir, mission_id)
                durable_lead = current["admissions"].get(lead_admission["admission_id"])
                if durable_lead is None:
                    raise MissionRunError("Lead admission disappeared before commit")
                if durable_lead["phase"] == "reserved":
                    committed = fleet_admission.commit(
                        runs_dir,
                        mission_id,
                        admission_id=durable_lead["admission_id"],
                        request_digest=durable_lead["request_digest"],
                        effect_sha256=durable_lead["effect_sha256"],
                        recipient_instance="lead",
                        writer=False,
                        run_id=durable_lead["run_id"],
                        idempotency_key="admission:lead:commit",
                    )
                    commit_event_sha256 = committed["commit_event_sha256"]
                elif durable_lead["phase"] in {
                    "committed",
                    "authorized",
                    "started",
                }:
                    proof = durable_lead.get("commit")
                    if proof is None:
                        raise MissionRunError("Lead admission lacks its commit proof")
                    commit_event_sha256 = proof["event_sha256"]
                else:
                    raise MissionRunError(
                        f"Lead cannot dispatch from terminal admission phase {durable_lead['phase']}"
                    )
                legacy = reconcile_lead_run(runs_dir, feature, prompt_sha)
                if legacy is None:
                    if durable_lead["phase"] == "started":
                        raise MissionRunError(
                            "started Lead admission has no reconcilable effect; refusing relaunch"
                        )
                    if mission_state.sha256(
                        parse_manifest(manifest_path)
                    ) != mission_state.sha256(manifest):
                        raise MissionRunError(
                            "fleet manifest changed between Lead preflight and launch"
                        )
                    wrapper = {
                        "interactive": "fleet-send.sh",
                        "local": "fleet-dispatch.sh",
                    }.get(manifest.get("lead.runner"))
                    if wrapper is None:
                        raise MissionRunError("Lead runner is unsupported")
                    command = [
                        str(ROOT / "scripts" / wrapper),
                        feature,
                        "lead",
                        prompt,
                        "--run-id",
                        durable_lead["run_id"],
                        "--json",
                    ]
                    try:
                        fleet_control.require_usage_launch(
                            runs_dir,
                            compiled,
                            feature=feature,
                            provider=lead_member["provider"],
                            hook_source=lead_hook_source,
                        )
                        authorization = fleet_admission.authorize_launch(
                            runs_dir,
                            mission_id,
                            admission_id=durable_lead["admission_id"],
                            commit_event_sha256=commit_event_sha256,
                            request_digest=durable_lead["request_digest"],
                            effect_sha256=durable_lead["effect_sha256"],
                            recipient_instance="lead",
                            writer=False,
                            run_id=durable_lead["run_id"],
                            approval_event_sha256=None,
                            idempotency_key="admission:lead:authorize",
                        )
                    except (
                        fleet_control.FleetControlError,
                        mission_state.MissionStateError,
                    ) as exc:
                        raise MissionRunError(
                            f"Lead launch is not admission-authorized: {exc}"
                        ) from exc
                    authorization_event_sha256 = authorization[
                        "authorization_event_sha256"
                    ]
                    # Launch authorization is the final fallible mission
                    # operation before the external wrapper effect.
                    sent = require_success(
                        command,
                        timeout=120,
                        env={**os.environ, "FLEET_RUNS_DIR": str(runs_dir)},
                    )
                    send_value = last_json_object(sent.stdout, wrapper)
                    run_id = str(send_value.get("run_id", ""))
                else:
                    run_id = str(legacy["run_id"])
                    authorization_proof = durable_lead.get("launch_authorization")
                    if not isinstance(authorization_proof, dict):
                        raise MissionRunError(
                            "reconciled Lead effect lacks launch authorization"
                        )
                    authorization_event_sha256 = authorization_proof["event_sha256"]
                try:
                    run_id = str(uuid.UUID(run_id))
                except ValueError as exc:
                    raise MissionRunError(
                        "Lead runner returned invalid run_id"
                    ) from exc
                if run_id != durable_lead["run_id"]:
                    raise MissionRunError(
                        "Lead effect does not match its deterministic admission run_id"
                    )
                _start_lead_after_effect(
                    runs_dir,
                    mission_id,
                    durable_lead,
                    authorization_event_sha256=authorization_event_sha256,
                )
                mission_state.append_event(
                    runs_dir,
                    mission_id,
                    kind="lead_dispatched",
                    actor="CONTROL",
                    idempotency_key="controller:lead:dispatched",
                    payload={"run_id": run_id, "prompt_sha256": prompt_sha},
                )
                cmux_signal(
                    manifest,
                    mission_id,
                    status="delegated",
                    progress=0.30,
                    message=f"lead_run_id={run_id}",
                )
                continue

            if current["lead_result"] is None:
                try:
                    wait = run_process(
                        [
                            str(ROOT / "scripts" / "fleet-wait.sh"),
                            feature,
                            "lead",
                            "--run",
                            f"lead={current['lead_run_id']}",
                            "--timeout",
                            str(timeout_seconds),
                            "--json",
                        ],
                        timeout=timeout_seconds + 30,
                        env={**os.environ, "FLEET_RUNS_DIR": str(runs_dir)},
                    )
                    wait_value = last_json_object(wait.stdout, "fleet-wait")
                except MissionRunError as exc:
                    raise MissionRunError(
                        "fleet-wait failed without durable Lead terminal evidence"
                    ) from exc
                status_value = str(wait_value.get("status", "indeterminate"))
                if wait.returncode != 0 or status_value != "succeeded":
                    legacy_terminal = exact_legacy_run(
                        runs_dir,
                        feature,
                        current["lead_run_id"],
                        prompt_sha256=prompt_sha,
                    )
                    exact_status = str(legacy_terminal.get("status", ""))
                    if exact_status not in mission_state.TERMINAL_STATUSES:
                        raise MissionRunError(
                            "Lead wait ended without durable terminal lifecycle evidence"
                        )
                    terminal = exact_status
                    _finalize_lead(
                        runs_dir,
                        mission_id,
                        reason=f"durable Lead terminal status: {terminal}",
                        terminal_evidence=terminal_evidence(
                            legacy_terminal,
                            run_id=current["lead_run_id"],
                            task_sha256=prompt_sha,
                            status=terminal,
                        ),
                    )
                    mission_state.append_terminal(
                        runs_dir,
                        mission_id,
                        status=terminal,
                        reason=(
                            f"lead run ended status={terminal} "
                            f"wait_status={status_value} exit={wait.returncode}"
                        ),
                        idempotency_key="controller:terminal:lead",
                    )
                    continue
                legacy = exact_legacy_run(
                    runs_dir,
                    feature,
                    current["lead_run_id"],
                    prompt_sha256=prompt_sha,
                )
                if legacy.get("status") != "succeeded":
                    raise MissionRunError(
                        "fleet-wait success disagrees with durable lead ledger"
                    )
                result_relative = exact_lead_result_relative(
                    runs_dir,
                    feature,
                    current["lead_run_id"],
                    legacy.get("result_file"),
                )
                try:
                    with fleet_safe_paths.RootedFS(runs_dir) as result_store:
                        if result_store.root != runs_dir:
                            raise fleet_safe_paths.SafePathError(
                                "runs_dir is not its exact physical root"
                            )
                        content = result_store.read_regular(
                            result_relative,
                            directory_modes=(0o755, 0o700),
                            file_mode=0o600,
                            max_bytes=16 * 1024 * 1024,
                        )
                        artifact_record = fleet_artifacts.put_bytes(
                            runs_dir, mission_id, content
                        )
                        artifact = artifact_record["artifact_id"]
                        result_store.atomic_write(
                            Path("missions") / mission_id / "lead-result.txt",
                            content,
                            directory_modes=(0o700, 0o700),
                            file_mode=0o600,
                        )
                        result_store.assert_root_binding()
                        mission_state.append_event(
                            runs_dir,
                            mission_id,
                            kind="lead_result_recorded",
                            actor="CONTROL",
                            idempotency_key="controller:lead:result",
                            payload={
                                "run_id": current["lead_run_id"],
                                "artifact_id": artifact,
                                "result_file": str(runs_dir / result_relative),
                                "provider": str(legacy.get("provider", "")),
                                "model": str(legacy.get("model", "")),
                                "variant": legacy.get("variant"),
                            },
                        )
                except fleet_safe_paths.SafePathError as exc:
                    raise MissionRunError(f"unsafe lead result store: {exc}") from exc
                _finalize_lead(
                    runs_dir,
                    mission_id,
                    reason="Lead result attested",
                    terminal_evidence=terminal_evidence(
                        legacy,
                        run_id=current["lead_run_id"],
                        task_sha256=prompt_sha,
                        status="succeeded",
                    ),
                )
                continue

            if (
                current["risk"] in {"high", "unknown"}
                and compiled["workflow"]["risk"]["high_action"] == "confirm_assured"
            ):
                active_admissions = [
                    admission["admission_id"]
                    for admission in current["admissions"].values()
                    if admission.get("active")
                ]
                if active_admissions:
                    raise MissionRunError(
                        "live assurance transition requires every admission finalized"
                    )
                mission_state.append_event(
                    runs_dir,
                    mission_id,
                    kind="assurance_requested",
                    actor="CONTROL",
                    idempotency_key="controller:assurance:live-requested",
                    payload={
                        "risk": current["risk"],
                        "categories": sorted(set(current.get("risk_categories") or [])),
                        "scope": current["target_repo"],
                        "workflow_digest": current["workflow_digest"],
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
            require_success(
                [
                    str(ROOT / "scripts" / "fleet-down.sh"),
                    feature,
                    "--prepare-archive",
                ],
                timeout=180,
                env={**os.environ, "FLEET_RUNS_DIR": str(runs_dir)},
            )
            archive = fleet_archive.ArchiveBuilder(runs_dir, mission_id).create(
                manifest_path
            )
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
                audit_lifecycle = fleet_audit_client.AuditLifecycle(
                    runs_dir, mission_id
                )
                audit_lifecycle.verify()
            mission_state.append_terminal(
                runs_dir,
                mission_id,
                status="succeeded",
                reason="lead result accepted and unified archive verified",
                idempotency_key="controller:terminal:succeeded",
            )
            manifest = parse_manifest(manifest_path)
            cmux_signal(
                manifest,
                mission_id,
                status="complete",
                progress=1.0,
                message="mission succeeded",
                notify=True,
            )
            continue

        if current["status"] == "assured_running":
            control_lifecycle = control_lifecycle_for(assurance_preset)
            control_lifecycle.start()
            assured_manifest = parse_manifest(manifest_path)
            verify_manifest_binding(assured_manifest, compiled)
            verify_manifest_repository_binding(
                assured_manifest, target_repo=target_repo, base_sha=frozen_base_sha
            )
            if (
                assured_manifest.get("mission_id") != mission_id
                or assured_manifest.get("preset") != assurance_preset
            ):
                raise MissionRunError("assured running manifest binding drift")
            if current["synthesis_result"] is not None:
                synthesis_run_id = current["synthesis_result"]["run_id"]
                synthesis_owner = current["run_owners"].get(synthesis_run_id)
                if (
                    synthesis_owner is None
                    or synthesis_owner.get("owner_kind") != "admission"
                ):
                    raise MissionRunError(
                        "recorded assured synthesis lacks admission ownership"
                    )
                synthesis_admission = current["admissions"].get(
                    synthesis_owner["owner_id"]
                )
                if not isinstance(synthesis_admission, dict):
                    raise MissionRunError(
                        "recorded assured synthesis admission disappeared"
                    )
                synthesis_task_sha256 = synthesis_admission["task_sha256"]
                synthesis_terminal = exact_legacy_run(
                    runs_dir,
                    feature,
                    synthesis_run_id,
                    prompt_sha256=synthesis_task_sha256,
                )
                if synthesis_terminal.get("status") != "succeeded":
                    raise MissionRunError(
                        "recorded assured synthesis lacks succeeded terminal evidence"
                    )
                _finalize_run_admission(
                    runs_dir,
                    mission_id,
                    run_id=synthesis_run_id,
                    idempotency_key="admission:assured-synthesis:finalize",
                    terminal_evidence=terminal_evidence(
                        synthesis_terminal,
                        run_id=synthesis_run_id,
                        task_sha256=synthesis_task_sha256,
                        status="succeeded",
                    ),
                )
                refreshed = fleet_mission.load_state(runs_dir, mission_id)
                prior_lead_id = refreshed.get("lead_admission_id")
                if isinstance(prior_lead_id, str):
                    prior_lead = refreshed["admissions"].get(prior_lead_id)
                    if prior_lead is not None and prior_lead["phase"] != "finalized":
                        prior_terminal = exact_legacy_run(
                            runs_dir,
                            feature,
                            prior_lead["run_id"],
                            prompt_sha256=prior_lead["task_sha256"],
                        )
                        prior_status = str(prior_terminal.get("status", ""))
                        if prior_status not in mission_state.TERMINAL_STATUSES:
                            raise MissionRunError(
                                "assured completion cannot retire a nonterminal prior Lead"
                            )
                        _finalize_lead(
                            runs_dir,
                            mission_id,
                            reason=(
                                "prior Lead terminal reconciled after assured synthesis"
                            ),
                            terminal_evidence=terminal_evidence(
                                prior_terminal,
                                run_id=prior_lead["run_id"],
                                task_sha256=prior_lead["task_sha256"],
                                status=prior_status,
                            ),
                        )
                mission_state.append_event(
                    runs_dir,
                    mission_id,
                    kind="mission_completing",
                    actor="CONTROL",
                    idempotency_key="controller:mission:completing",
                    payload={
                        "lead_artifact_id": current["synthesis_result"]["artifact_id"]
                    },
                )
                continue
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
                assured = fleet_assured_runner.AssuredRunner(
                    runs_dir, mission_id
                ).drive(spec_path)
            except fleet_assured_runner.AssuredApprovalRenewalRequired as exc:
                paused = fleet_mission.load_state(runs_dir, mission_id)
                active = [
                    admission["admission_id"]
                    for admission in paused["admissions"].values()
                    if admission.get("active")
                ]
                if active:
                    raise MissionRunError(
                        "assured approval renewal pause retained active admissions"
                    ) from exc
                return {
                    "mission_id": mission_id,
                    "feature": feature,
                    "status": "assured_running",
                    "reason": str(exc),
                    "next_action": exc.next_action,
                }
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
            synthesis_prompt_sha256 = synthesis.get("prompt_sha256")
            if not isinstance(
                synthesis_prompt_sha256, str
            ) or not mission_state.SHA256.fullmatch(synthesis_prompt_sha256):
                raise MissionRunError("assured synthesis lacks its exact prompt_sha256")
            if current["synthesis_result"] is None:
                legacy = exact_legacy_run(
                    runs_dir,
                    feature,
                    run_id,
                    prompt_sha256=synthesis_prompt_sha256,
                )
                if legacy.get("status") != "succeeded":
                    raise MissionRunError(
                        "assured synthesis lacks durable success evidence"
                    )
                result_relative = exact_lead_result_relative(
                    runs_dir, feature, run_id, synthesis.get("result_file")
                )
                exact_lead_result_relative(
                    runs_dir, feature, run_id, legacy.get("result_file")
                )
                try:
                    with fleet_safe_paths.RootedFS(runs_dir) as result_store:
                        if result_store.root != runs_dir:
                            raise fleet_safe_paths.SafePathError(
                                "runs_dir is not its exact physical root"
                            )
                        content = result_store.read_regular(
                            result_relative,
                            directory_modes=(0o755, 0o700),
                            file_mode=0o600,
                            max_bytes=16 * 1024 * 1024,
                        )
                        artifact = fleet_artifacts.put_bytes(
                            runs_dir, mission_id, content
                        )
                        result_store.atomic_write(
                            Path("missions") / mission_id / "lead-result.txt",
                            content,
                            directory_modes=(0o700, 0o700),
                            file_mode=0o600,
                        )
                        result_store.assert_root_binding()
                        owner = fleet_mission.load_state(runs_dir, mission_id)[
                            "run_owners"
                        ].get(run_id)
                        if owner is None or owner.get("owner_kind") != "admission":
                            raise MissionRunError(
                                "assured synthesis lacks admission ownership"
                            )
                        mission_state.append_event(
                            runs_dir,
                            mission_id,
                            kind="synthesis_result_recorded",
                            actor="CONTROL",
                            idempotency_key="controller:assured:lead-result",
                            payload={
                                "run_id": run_id,
                                "admission_id": owner["owner_id"],
                                "artifact_id": artifact["artifact_id"],
                                "result_file": str(runs_dir / result_relative),
                                "provider": str(legacy.get("provider", "")),
                                "model": str(legacy.get("model", "")),
                                "variant": legacy.get("variant"),
                            },
                        )
                except fleet_safe_paths.SafePathError as exc:
                    raise MissionRunError(f"unsafe lead result store: {exc}") from exc
                _finalize_run_admission(
                    runs_dir,
                    mission_id,
                    run_id=run_id,
                    idempotency_key="admission:assured-synthesis:finalize",
                    terminal_evidence=terminal_evidence(
                        legacy,
                        run_id=run_id,
                        task_sha256=synthesis_prompt_sha256,
                        status="succeeded",
                    ),
                )
                continue
            mission_state.append_event(
                runs_dir,
                mission_id,
                kind="mission_completing",
                actor="CONTROL",
                idempotency_key="controller:mission:completing",
                payload={
                    "lead_artifact_id": current["synthesis_result"]["artifact_id"]
                },
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
    target_repo = exact_git_toplevel(target_repo)
    if not runs_dir.exists():
        mission_state.ensure_private_directory(runs_dir)
    try:
        runs_dir = fleet_safe_paths.canonical_root(runs_dir)
    except fleet_safe_paths.SafePathError as exc:
        raise MissionRunError("unsafe mission runs root") from exc
    compiled = workflow_config.compile_path(workflow_path(workflow_name))
    enforce_audit_trust(compiled, [], execution_profile)
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
    target_repo = exact_git_toplevel(target_repo)
    try:
        execution_profile = fleet_manifest.validate_profile(execution_profile)
    except fleet_manifest.ManifestError as exc:
        raise MissionRunError(str(exc)) from exc
    compiled = workflow_config.compile_path(workflow_path(workflow_name))
    assessment = fleet_risk.assess(
        workflow_minimum=compiled["workflow"]["risk"]["minimum"],
        objective=objective,
        target=str(target_repo),
        repository_root=str(target_repo),
        override=risk_override,
    )
    enforce_audit_trust(compiled, list(assessment["categories"]), execution_profile)
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
        command.add_argument(
            "--risk", default="auto", choices=("auto", *fleet_risk.RISK_ORDER)
        )
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
            return 3 if "next_action" in value else 0
        if args.command == "resume":
            value = drive_mission(runs_dir, args.mission_id)
            emit(value, json_mode=args.json)
            return 3 if "next_action" in value else 0
        if args.command == "show":
            emit(fleet_mission.load_state(runs_dir, args.mission_id), json_mode=False)
            return 0
        if args.command == "request-assurance":
            categories = sorted(
                {item.strip() for item in args.categories.split(",") if item.strip()}
            )
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
