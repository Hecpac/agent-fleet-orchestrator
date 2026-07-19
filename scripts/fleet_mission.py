#!/usr/bin/env python3
"""Create, inspect, verify, resume, and terminalize durable missions."""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import re
import sys
from typing import Any
import uuid

import fleet_admission
import fleet_compiled
import fleet_json
import fleet_mission_state as state
import fleet_safe_paths


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = ROOT / "orchestration" / "runs"
FEATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
MAX_COMPILED_WORKFLOW_BYTES = 32 * 1024 * 1024


class MissionError(state.MissionStateError):
    """Mission creation or compiled workflow validation failed."""


def validate_compiled(value: Any) -> dict[str, Any]:
    try:
        return fleet_compiled.validate(value, mode="effect")
    except fleet_compiled.CompiledError as exc:
        raise MissionError(f"compiled workflow is not effect-authorized: {exc}") from exc


def load_compiled(path: Path) -> dict[str, Any]:
    """Load an operator-selected source before it enters the Mission store."""

    try:
        return fleet_compiled.load(path, mode="effect")
    except fleet_compiled.CompiledError as exc:
        raise MissionError(f"cannot load compiled workflow {path}: {exc}") from exc


def load_mission_compiled(
    runs_dir: Path,
    mission_id: str,
    *,
    mode: fleet_compiled.LoadMode,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load one durable compiled workflow from its descriptor-bound Mission path.

    The caller selects only ``runs_dir`` and a canonical Mission UUID. Every
    descendant is opened without following symlinks and with the physical
    producer contract (0700/0700/0600, one link, bounded bytes). The parsed
    artifact must also retain both digest bindings published by the Mission
    ledger before it can authorize effects.
    """

    if mode not in {"read", "effect"}:
        raise MissionError("compiled load mode must be 'read' or 'effect'")
    normalized = state.normalize_uuid(mission_id, "mission_id")
    try:
        current = load_state(runs_dir, normalized)
    except state.MissionStateError as exc:
        raise MissionError(
            f"cannot bind durable compiled workflow to Mission state: {exc}"
        ) from exc
    return _load_compiled_for_snapshot(
        runs_dir,
        normalized,
        current,
        mode=mode,
    ), current


def _load_compiled_for_snapshot(
    runs_dir: Path,
    mission_id: str,
    current: dict[str, Any],
    *,
    mode: fleet_compiled.LoadMode,
) -> dict[str, Any]:
    """Load immutable compiled bytes against one caller-owned Mission snapshot."""

    normalized = state.normalize_uuid(mission_id, "mission_id")
    if not isinstance(current, dict) or current.get("mission_id") != normalized:
        raise MissionError("compiled workflow snapshot Mission binding mismatch")
    relative = Path("missions") / normalized / "compiled-workflow.json"
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            raw = rooted.read_regular(
                relative,
                directory_modes=(0o700, 0o700),
                file_mode=0o600,
                max_bytes=MAX_COMPILED_WORKFLOW_BYTES,
                require_single_link=True,
            )
            try:
                compiled = fleet_compiled.loads(raw, mode=mode)
            except fleet_compiled.CompiledError as exc:
                raise MissionError(
                    f"durable compiled workflow is not authorized for {mode}: {exc}"
                ) from exc
            if raw != state.canonical_bytes(compiled) + b"\n":
                raise MissionError(
                    "durable compiled workflow bytes are not canonical"
                )
            if (
                compiled["workflow_digest"] != current["workflow_digest"]
                or compiled["compiled_digest"] != current["compiled_digest"]
            ):
                raise MissionError("compiled workflow differs from mission ledger")
            rooted.assert_root_binding()
            return compiled
    except MissionError:
        raise
    except fleet_safe_paths.SafePathError as exc:
        raise MissionError(f"unsafe durable compiled workflow: {exc}") from exc
    except state.MissionStateError as exc:
        raise MissionError(
            f"cannot bind durable compiled workflow to Mission state: {exc}"
        ) from exc


def load_snapshot_compiled(
    runs_dir: Path,
    mission_id: str,
    current: dict[str, Any],
    *,
    mode: fleet_compiled.LoadMode,
) -> dict[str, Any]:
    """Bind compiled bytes without re-reading a caller-owned Mission snapshot."""

    if mode not in {"read", "effect"}:
        raise MissionError("compiled load mode must be 'read' or 'effect'")
    return _load_compiled_for_snapshot(
        runs_dir,
        mission_id,
        current,
        mode=mode,
    )


def load_transaction_compiled(
    transaction: state.MissionTransaction,
    *,
    mode: fleet_compiled.LoadMode,
) -> dict[str, Any]:
    """Bind compiled bytes to the exact snapshot of an active transaction."""

    current = transaction.current_state
    if current is None:
        raise MissionError("compiled workflow transaction has no Mission state")
    return load_snapshot_compiled(
        transaction.runs_dir,
        transaction.mission_id,
        current,
        mode=mode,
    )


def _created_request(
    *,
    feature: str,
    objective: str,
    target_repo: Path,
    base_sha: str,
    compiled: dict[str, Any],
) -> dict[str, Any]:
    return {
        "feature": feature,
        "objective_sha256": state.artifact_id(objective),
        "target_repo": str(target_repo),
        "base_sha": base_sha,
        "workflow_digest": compiled["workflow_digest"],
        "initial_risk": compiled["workflow"]["risk"]["minimum"],
    }


def _mission_id_for_key(idempotency_key: str) -> str:
    if not state.SAFE_KEY.fullmatch(idempotency_key):
        raise MissionError("invalid mission idempotency key")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fleet-mission:{idempotency_key}"))


def create_mission(
    runs_dir: Path,
    *,
    compiled: dict[str, Any],
    feature: str,
    objective: str,
    target_repo: Path,
    base_sha: str,
    idempotency_key: str,
    runtime_options: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    validate_compiled(compiled)
    if not FEATURE.fullmatch(feature):
        raise MissionError("invalid feature")
    if not objective.strip():
        raise MissionError("objective must be non-empty")
    try:
        state.validate_target_repo(str(target_repo))
    except state.MissionStateError as exc:
        raise MissionError(str(exc)) from exc
    if not GIT_SHA.fullmatch(base_sha):
        raise MissionError("base_sha must be a full Git object id")
    request = _created_request(
        feature=feature,
        objective=objective,
        target_repo=target_repo,
        base_sha=base_sha,
        compiled=compiled,
    )
    options = runtime_options or {}
    if not isinstance(options, dict):
        raise MissionError("runtime_options must be an object")
    mission_id = _mission_id_for_key(idempotency_key)
    if not runs_dir.exists():
        state.ensure_private_directory(runs_dir)
    mission_relative = Path("missions") / mission_id
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            with rooted.exclusive_lock(
                Path("missions") / ".lock",
                directory_modes=(0o700,),
                file_mode=0o600,
            ):
                existed = mission_id in rooted.list_directory(
                    "missions", directory_modes=(0o700,)
                )
                creation = state.canonical_bytes(
                    {
                        "schema_version": 1,
                        "mission_id": mission_id,
                        "idempotency_key": idempotency_key,
                        "request": request,
                        "runtime_options": options,
                    }
                ) + b"\n"
                for name, content in (
                    ("creation-request.json", creation),
                    (
                        "compiled-workflow.json",
                        state.canonical_bytes(compiled) + b"\n",
                    ),
                    ("objective.txt", objective.encode("utf-8")),
                    (
                        "runtime-options.json",
                        state.canonical_bytes(options) + b"\n",
                    ),
                ):
                    try:
                        rooted.atomic_write(
                            mission_relative / name,
                            content,
                            directory_modes=(0o700, 0o700),
                            file_mode=0o600,
                        )
                    except fleet_safe_paths.SafePathError as exc:
                        if "conflicts with requested bytes" in str(exc):
                            raise state.MissionConflict(
                                f"durable mission file conflicts: {name}"
                            ) from exc
                        raise
                created_event, first_appended = state.append_event(
                    runs_dir,
                    mission_id,
                    kind="mission_created",
                    actor="CONTROL",
                    idempotency_key=idempotency_key,
                    payload=request,
                )
                state.append_event(
                    runs_dir,
                    mission_id,
                    kind="workflow_compiled",
                    actor="CONTROL",
                    idempotency_key=f"{idempotency_key}:compiled",
                    payload={"compiled_digest": compiled["compiled_digest"]},
                )
                limits = compiled["workflow"]["limits"]
                deadline = state.parse_timestamp(
                    created_event["timestamp"], "mission creation timestamp"
                ) + timedelta(seconds=int(limits["deadline_seconds"]))
                fleet_admission.freeze_policy(
                    runs_dir,
                    mission_id,
                    workflow_digest=compiled["workflow_digest"],
                    compiled_digest=compiled["compiled_digest"],
                    deadline_at=deadline.isoformat(timespec="microseconds").replace(
                        "+00:00", "Z"
                    ),
                    delegation_credits=int(limits["delegation_credits"]),
                    max_active_delegations=int(limits["max_active_delegations"]),
                    idempotency_key=f"{idempotency_key}:admission-policy",
                )
                rooted.assert_root_binding()
                return mission_id, first_appended and not existed
    except fleet_safe_paths.SafePathError as exc:
        raise MissionError(f"unsafe mission store: {exc}") from exc


def load_state(runs_dir: Path, mission_id: str) -> dict[str, Any]:
    normalized = state.normalize_uuid(mission_id, "mission_id")
    events = state.read_events(
        state.ledger_path(runs_dir, normalized), expected_mission_id=normalized
    )
    state.verify_events(events)
    return state.derive_state(events)


def _payload(value: str) -> dict[str, Any]:
    try:
        result = fleet_json.loads(value)
    except fleet_json.FleetJSONError as exc:
        raise MissionError(f"invalid --payload-json: {exc}") from exc
    if not isinstance(result, dict):
        raise MissionError("--payload-json must be an object")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument("--compiled", required=True)
    create.add_argument("--feature", required=True)
    create.add_argument("--objective", required=True)
    create.add_argument("--target-repo", required=True)
    create.add_argument("--base-sha", required=True)
    create.add_argument("--idempotency-key", required=True)

    for name in ("show", "resume-plan", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--mission-id", required=True)

    append = commands.add_parser("append")
    append.add_argument("--mission-id", required=True)
    append.add_argument("--kind", required=True)
    append.add_argument("--actor", default="CONTROL")
    append.add_argument("--idempotency-key", required=True)
    append.add_argument("--payload-json", default="{}")

    terminal = commands.add_parser("mark-terminal")
    terminal.add_argument("--mission-id", required=True)
    terminal.add_argument("--status", choices=sorted(state.TERMINAL_STATUSES), required=True)
    terminal.add_argument("--reason", required=True)
    terminal.add_argument("--idempotency-key", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runs_dir = Path(args.runs_dir).expanduser().resolve()
    try:
        if args.command == "create":
            mission_id, created = create_mission(
                runs_dir,
                compiled=load_compiled(Path(args.compiled)),
                feature=args.feature,
                objective=args.objective,
                target_repo=Path(args.target_repo).expanduser().resolve(),
                base_sha=args.base_sha,
                idempotency_key=args.idempotency_key,
            )
            print(json.dumps({"mission_id": mission_id, "created": created}, sort_keys=True))
        elif args.command == "show":
            print(json.dumps(load_state(runs_dir, args.mission_id), indent=2, sort_keys=True))
        elif args.command == "resume-plan":
            print(json.dumps(state.resume_plan(load_state(runs_dir, args.mission_id)), sort_keys=True))
        elif args.command == "verify":
            normalized = state.normalize_uuid(args.mission_id, "mission_id")
            events = state.read_events(
                state.ledger_path(runs_dir, normalized), expected_mission_id=normalized
            )
            print(json.dumps(state.verify_events(events), sort_keys=True))
        elif args.command == "append":
            event, appended = state.append_event(
                runs_dir,
                args.mission_id,
                kind=args.kind,
                actor=args.actor,
                idempotency_key=args.idempotency_key,
                payload=_payload(args.payload_json),
            )
            print(json.dumps({"event": event, "appended": appended}, sort_keys=True))
        elif args.command == "mark-terminal":
            event, appended = state.append_terminal(
                runs_dir,
                args.mission_id,
                status=args.status,
                reason=args.reason,
                idempotency_key=args.idempotency_key,
            )
            print(json.dumps({"event": event, "appended": appended}, sort_keys=True))
        return 0
    except state.MissionStateError as exc:
        print(f"mission error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
