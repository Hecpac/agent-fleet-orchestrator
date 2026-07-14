#!/usr/bin/env python3
"""Create, inspect, verify, resume, and terminalize durable missions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any
import uuid

import fleet_mission_state as state


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = ROOT / "orchestration" / "runs"
FEATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40,64}$")


class MissionError(state.MissionStateError):
    """Mission creation or compiled workflow validation failed."""


def validate_compiled(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MissionError("compiled workflow must be an object")
    required = {
        "schema_version", "workflow", "workflow_digest", "router_digest",
        "resolved", "compiled_digest",
    }
    if set(value) != required or value["schema_version"] != 1:
        raise MissionError("compiled workflow fields do not match schema_version=1")
    unsigned = {key: item for key, item in value.items() if key != "compiled_digest"}
    if state.sha256(unsigned) != value["compiled_digest"]:
        raise MissionError("compiled workflow digest mismatch")
    if state.sha256(value["workflow"]) != value["workflow_digest"]:
        raise MissionError("workflow digest mismatch")
    return value


def load_compiled(path: Path) -> dict[str, Any]:
    try:
        return validate_compiled(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise MissionError(f"cannot load compiled workflow {path}: {exc}") from exc


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


def _write_or_verify(path: Path, content: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise state.MissionConflict(f"durable mission file conflicts: {path.name}")
        return
    state.atomic_write(path, content)


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
    if not target_repo.is_absolute():
        raise MissionError("target_repo must be absolute")
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
    missions = state.missions_root(runs_dir)
    state.ensure_private_directory(missions)
    with state.exclusive_lock(missions / ".lock"):
        root = state.mission_root(runs_dir, mission_id)
        existed = root.exists()
        state.ensure_private_directory(root)
        creation = state.canonical_bytes(
            {
                "schema_version": 1,
                "mission_id": mission_id,
                "idempotency_key": idempotency_key,
                "request": request,
                "runtime_options": options,
            }
        ) + b"\n"
        _write_or_verify(root / "creation-request.json", creation)
        _write_or_verify(
            root / "compiled-workflow.json", state.canonical_bytes(compiled) + b"\n"
        )
        _write_or_verify(root / "objective.txt", objective.encode("utf-8"))
        _write_or_verify(
            root / "runtime-options.json", state.canonical_bytes(options) + b"\n"
        )
        _, first_appended = state.append_event(
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
        return mission_id, first_appended and not existed


def load_state(runs_dir: Path, mission_id: str) -> dict[str, Any]:
    normalized = state.normalize_uuid(mission_id, "mission_id")
    events = state.read_events(
        state.ledger_path(runs_dir, normalized), expected_mission_id=normalized
    )
    state.verify_events(events)
    return state.derive_state(events)


def _payload(value: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
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
