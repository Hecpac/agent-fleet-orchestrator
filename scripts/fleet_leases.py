#!/usr/bin/env python3
"""Ownership-safe local worker leases and crash reconciliation."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Iterator

from fleet_identity import current_tree, mappings
from fleet_ledger import append_event, latest_event


TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "abandoned", "indeterminate"}


class LeaseError(RuntimeError):
    pass


class LeaseBusy(LeaseError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def coordinator(runs_dir: Path) -> Iterator[None]:
    lock_dir = runs_dir / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = lock_dir / ".coordinator"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _metadata_path(lease: Path) -> Path:
    return lease / "lease.json"


def read_metadata(lease: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(_metadata_path(lease).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_metadata(lease: Path, value: dict[str, Any]) -> None:
    lease.mkdir(mode=0o700)
    path = _metadata_path(lease)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")


def _update_metadata(lease: Path, value: dict[str, Any]) -> None:
    temporary = lease / ".lease.json.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, _metadata_path(lease))


def _lease_dirs(runs_dir: Path) -> list[Path]:
    lock_dir = runs_dir / "locks"
    if not lock_dir.exists():
        return []
    return sorted(path for path in lock_dir.glob("*.lock") if path.is_dir())


def _closing_path(runs_dir: Path, feature: str) -> Path:
    return runs_dir / "locks" / f"{feature}.closing"


def _remove_owned(lease: Path, run_id: str) -> bool:
    if not lease.exists():
        return False
    metadata = read_metadata(lease)
    if not metadata or metadata.get("run_id") != run_id:
        raise LeaseError(f"lease owner mismatch: {lease}")
    entries = list(lease.iterdir())
    if entries != [_metadata_path(lease)]:
        raise LeaseError(f"lease contains unexpected files: {lease}")
    _metadata_path(lease).unlink()
    lease.rmdir()
    return True


def _quarantine(runs_dir: Path, lease: Path, run_id: str) -> Path:
    metadata = read_metadata(lease)
    if not metadata or metadata.get("run_id") != run_id:
        raise LeaseError(f"lease owner mismatch during quarantine: {lease}")
    destination_dir = runs_dir / "archive" / "leases" / run_id
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = destination_dir / f"{time.time_ns()}-{lease.name}"
    os.replace(lease, destination)
    return destination


def _run_groups(runs_dir: Path, feature: str | None = None) -> tuple[dict[str, list[Path]], list[Path]]:
    groups: dict[str, list[Path]] = {}
    unknown: list[Path] = []
    for lease in _lease_dirs(runs_dir):
        metadata = read_metadata(lease)
        if not metadata or not isinstance(metadata.get("run_id"), str):
            unknown.append(lease)
            continue
        if feature is not None and metadata.get("feature") != feature:
            continue
        groups.setdefault(metadata["run_id"], []).append(lease)
    return groups, unknown


def _surface_owner(
    runs_dir: Path, surface_uuid: str, *, excluding_run_id: str = ""
) -> dict[str, Any] | None:
    target = surface_uuid.upper()
    for lease in _lease_dirs(runs_dir):
        metadata = read_metadata(lease)
        if not metadata or metadata.get("run_id") == excluding_run_id:
            continue
        if str(metadata.get("surface_uuid") or "").upper() == target:
            return metadata
    return None


def _reject_surface_owner(runs_dir: Path, surface_uuid: str, run_id: str) -> None:
    owner = _surface_owner(runs_dir, surface_uuid, excluding_run_id=run_id)
    if owner:
        raise LeaseBusy(
            "surface is busy: "
            f"{surface_uuid.upper()} owned by "
            f"{owner.get('feature')}.{owner.get('instance')} "
            f"run_id={owner.get('run_id')}"
        )


def _reject_unknown_leases(reconciled: dict[str, Any]) -> None:
    unknown = reconciled.get("unknown") or []
    if unknown:
        raise LeaseBusy(
            "unknown or malformed leases block acquisition: " + ", ".join(unknown)
        )


def _confirmed_absent(metadata: dict[str, Any], tree_text: str) -> tuple[bool, str]:
    if tree_text.strip() and not re.search(
        r"(?:window|workspace|pane|surface) "
        r"(?:window|workspace|pane|surface):\d+ [0-9A-Fa-f-]{36}",
        tree_text,
    ):
        raise LeaseError("cmux tree output does not match the expected id-format=both protocol")
    workspaces, surfaces = mappings(tree_text)
    workspace_uuids = {value.upper() for value in workspaces.values()}
    surface_uuids = {value.upper() for value in surfaces.values()}
    workspace_uuid = str(metadata.get("workspace_uuid", "")).upper()
    surface_uuid = str(metadata.get("surface_uuid", "")).upper()
    if workspace_uuid and workspace_uuid not in workspace_uuids:
        return True, f"workspace_uuid_absent:{workspace_uuid}"
    if surface_uuid and surface_uuid not in surface_uuids:
        return True, f"surface_uuid_absent:{surface_uuid}"
    return False, ""


def _abandon(runs_dir: Path, metadata: dict[str, Any], reason: str) -> None:
    ledger = runs_dir / f"fleet-{metadata['feature']}.ledger.jsonl"
    previous = latest_event(ledger, run_id=metadata["run_id"])
    if previous and previous.get("status") in TERMINAL_STATUSES:
        return
    append_event(
        ledger,
        {
            "timestamp": _utc_now(),
            "run_id": metadata["run_id"],
            "feature": metadata["feature"],
            "instance": metadata["instance"],
            "role": metadata["role"],
            "phase": metadata["phase"],
            "status": "abandoned",
            "task_sha256": metadata["task_sha256"],
            "exit_code": 4,
            "reason": reason,
        },
    )


def _reconcile_locked(
    runs_dir: Path,
    *,
    feature: str | None = None,
    tree_reader: Callable[[], str] = current_tree,
) -> dict[str, Any]:
    groups, unknown = _run_groups(runs_dir, feature)
    quarantined: list[str] = []
    active: list[str] = []
    probe_error = ""
    tree_text: str | None = None

    for run_id, leases in groups.items():
        metadata = read_metadata(leases[0]) or {}
        ledger = runs_dir / f"fleet-{metadata.get('feature', '')}.ledger.jsonl"
        event = latest_event(ledger, run_id=run_id)
        retained_indeterminate = bool(
            event
            and event.get("status") == "indeterminate"
            and event.get("lease_retained")
        )
        terminal = bool(
            event
            and event.get("status") in TERMINAL_STATUSES
            and not retained_indeterminate
        )
        reason = "terminal_ledger" if terminal else ""
        if not terminal:
            if tree_text is None and not probe_error:
                try:
                    tree_text = tree_reader()
                except RuntimeError as exc:
                    probe_error = str(exc)
            if tree_text is not None:
                terminal, reason = _confirmed_absent(metadata, tree_text)
                if terminal:
                    _abandon(runs_dir, metadata, reason)
        if terminal:
            for lease in leases:
                quarantined.append(str(_quarantine(runs_dir, lease, run_id)))
        else:
            active.extend(str(lease) for lease in leases)

    return {
        "active": active,
        "quarantined": quarantined,
        "unknown": [str(path) for path in unknown],
        "probe_error": probe_error,
    }


def reconcile(
    runs_dir: Path,
    *,
    feature: str | None = None,
    tree_reader: Callable[[], str] = current_tree,
) -> dict[str, Any]:
    with coordinator(runs_dir):
        return _reconcile_locked(runs_dir, feature=feature, tree_reader=tree_reader)


def acquire(
    runs_dir: Path,
    *,
    run_id: str,
    feature: str,
    instance: str,
    role: str,
    phase: str,
    resource_class: str,
    task_sha256: str,
    workspace_uuid: str,
    surface_uuid: str,
    max_local: int,
    role_limit: int,
    tree_reader: Callable[[], str] = current_tree,
) -> dict[str, str]:
    lock_dir = runs_dir / "locks"
    created: list[Path] = []
    with coordinator(runs_dir):
        reconciled = _reconcile_locked(runs_dir, tree_reader=tree_reader)
        _reject_unknown_leases(reconciled)
        if _closing_path(runs_dir, feature).exists():
            raise LeaseBusy(f"fleet '{feature}' is closing")
        _reject_surface_owner(runs_dir, surface_uuid, run_id)
        instance_lock = lock_dir / f"{feature}.{instance}.lock"
        if instance_lock.exists():
            raise LeaseBusy(f"instance '{instance}' is busy")
        heavy_lock = lock_dir / "local-heavy.lock"
        if resource_class == "local_heavy" and heavy_lock.exists():
            raise LeaseBusy("a heavy local worker already holds the global lease")
        local_slot = next(
            (lock_dir / f"local-slot-{slot}.lock" for slot in range(1, max_local + 1)
             if not (lock_dir / f"local-slot-{slot}.lock").exists()),
            None,
        )
        if local_slot is None:
            raise LeaseBusy("all local-worker slots are busy")
        role_slot = next(
            (lock_dir / f"role-{role}-{slot}.lock" for slot in range(1, role_limit + 1)
             if not (lock_dir / f"role-{role}-{slot}.lock").exists()),
            None,
        )
        if role_slot is None:
            raise LeaseBusy(f"role '{role}' reached concurrency limit {role_limit}")

        paths = [instance_lock, local_slot, role_slot]
        if resource_class == "local_heavy":
            paths.insert(1, heavy_lock)
        base = {
            "schema_version": 1,
            "run_id": run_id,
            "feature": feature,
            "instance": instance,
            "role": role,
            "phase": phase,
            "resource_class": resource_class,
            "task_sha256": task_sha256,
            "workspace_uuid": workspace_uuid.upper(),
            "surface_uuid": surface_uuid.upper(),
            "acquired_at": _utc_now(),
            "pid": None,
            "pgid": None,
        }
        try:
            for path in paths:
                metadata = {**base, "kind": path.name}
                _write_metadata(path, metadata)
                created.append(path)
        except Exception:
            for path in reversed(created):
                _remove_owned(path, run_id)
            raise

    return {
        "instance_lock": str(instance_lock),
        "heavy_lock": str(heavy_lock) if resource_class == "local_heavy" else "",
        "local_slot": str(local_slot),
        "role_slot": str(role_slot),
    }


def acquire_frontier(
    runs_dir: Path,
    *,
    run_id: str,
    feature: str,
    instance: str,
    role: str,
    phase: str,
    task_sha256: str,
    workspace_uuid: str,
    surface_uuid: str,
    tree_reader: Callable[[], str] = current_tree,
) -> Path:
    lock_dir = runs_dir / "locks"
    with coordinator(runs_dir):
        reconciled = _reconcile_locked(runs_dir, tree_reader=tree_reader)
        _reject_unknown_leases(reconciled)
        if _closing_path(runs_dir, feature).exists():
            raise LeaseBusy(f"fleet '{feature}' is closing")
        _reject_surface_owner(runs_dir, surface_uuid, run_id)
        instance_lock = lock_dir / f"{feature}.{instance}.lock"
        if instance_lock.exists():
            raise LeaseBusy(f"instance '{instance}' is busy")
        _write_metadata(
            instance_lock,
            {
                "schema_version": 1,
                "run_id": run_id,
                "feature": feature,
                "instance": instance,
                "role": role,
                "phase": phase,
                "resource_class": "remote",
                "runner": "interactive",
                "task_sha256": task_sha256,
                "workspace_uuid": workspace_uuid.upper(),
                "surface_uuid": surface_uuid.upper(),
                "acquired_at": _utc_now(),
                "pid": None,
                "pgid": None,
                "kind": instance_lock.name,
            },
        )
    return instance_lock


def validate(runs_dir: Path, run_id: str, leases: list[Path]) -> None:
    with coordinator(runs_dir):
        for lease in leases:
            metadata = read_metadata(lease)
            if not metadata or metadata.get("run_id") != run_id:
                raise LeaseError(f"lease missing or owned by another run: {lease}")


def activate(runs_dir: Path, run_id: str, leases: list[Path], pid: int, pgid: int) -> None:
    with coordinator(runs_dir):
        for lease in leases:
            metadata = read_metadata(lease)
            if not metadata or metadata.get("run_id") != run_id:
                raise LeaseError(f"cannot activate unowned lease: {lease}")
            metadata.update({"pid": pid, "pgid": pgid, "started_at": _utc_now()})
            _update_metadata(lease, metadata)


def release(runs_dir: Path, run_id: str, leases: list[Path]) -> None:
    with coordinator(runs_dir):
        for lease in leases:
            metadata = read_metadata(lease)
            if not metadata or metadata.get("run_id") != run_id:
                raise LeaseError(f"lease missing or owned by another run: {lease}")
        for lease in leases:
            _remove_owned(lease, run_id)


def check_active(runs_dir: Path, feature: str) -> list[str]:
    with coordinator(runs_dir):
        groups, unknown = _run_groups(runs_dir)
        active = [
            str(lease)
            for leases in groups.values()
            for lease in leases
            if (read_metadata(lease) or {}).get("feature") == feature
        ]
        # Malformed global locks cannot be attributed safely; fail closed.
        active.extend(str(path) for path in unknown)
        return active


def begin_close(
    runs_dir: Path,
    *,
    feature: str,
    close_id: str,
    workspace_uuid: str,
    tree_reader: Callable[[], str] = current_tree,
) -> Path:
    with coordinator(runs_dir):
        result = _reconcile_locked(runs_dir, feature=feature, tree_reader=tree_reader)
        groups, unknown = _run_groups(runs_dir)
        active = [
            lease
            for leases in groups.values()
            for lease in leases
            if (read_metadata(lease) or {}).get("feature") == feature
        ]
        if active or unknown:
            raise LeaseBusy("active or malformed leases prevent teardown")
        marker = _closing_path(runs_dir, feature)
        if marker.exists():
            try:
                previous = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LeaseError(f"closing marker is malformed: {marker}") from exc
            try:
                tree_text = tree_reader()
            except RuntimeError as exc:
                raise LeaseError(f"cannot validate existing closing owner: {exc}") from exc
            absent, _ = _confirmed_absent(previous, tree_text)
            if not absent:
                raise LeaseBusy(f"fleet '{feature}' already has a closing owner")
            archive = runs_dir / "archive" / "closing" / feature
            archive.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(marker, archive / f"{time.time_ns()}-closing.json")
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "feature": feature,
                    "close_id": close_id,
                    "workspace_uuid": workspace_uuid.upper(),
                    "acquired_at": _utc_now(),
                    "reconcile": result,
                },
                handle,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return marker


def end_close(runs_dir: Path, *, feature: str, close_id: str) -> None:
    with coordinator(runs_dir):
        marker = _closing_path(runs_dir, feature)
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LeaseError(f"closing marker missing or malformed: {marker}") from exc
        if metadata.get("close_id") != close_id:
            raise LeaseError(f"closing marker owner mismatch: {marker}")
        marker.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("reconcile", "check"):
        command = sub.add_parser(name)
        command.add_argument("runs_dir")
        command.add_argument("--feature")
    command = sub.add_parser("acquire")
    command.add_argument("runs_dir")
    for name in ("run-id", "feature", "instance", "role", "phase", "resource-class",
                 "task-sha256", "workspace-uuid", "surface-uuid"):
        command.add_argument(f"--{name}", required=True)
    command.add_argument("--max-local", type=int, required=True)
    command.add_argument("--role-limit", type=int, required=True)
    command = sub.add_parser("acquire-frontier")
    command.add_argument("runs_dir")
    for name in (
        "run-id", "feature", "instance", "role", "phase", "task-sha256",
        "workspace-uuid", "surface-uuid",
    ):
        command.add_argument(f"--{name}", required=True)
    for name in ("validate", "release", "activate"):
        command = sub.add_parser(name)
        command.add_argument("runs_dir")
        command.add_argument("--run-id", required=True)
        command.add_argument("--lease", action="append", default=[])
        if name == "activate":
            command.add_argument("--pid", type=int, required=True)
            command.add_argument("--pgid", type=int, required=True)
    command = sub.add_parser("begin-close")
    command.add_argument("runs_dir")
    command.add_argument("--feature", required=True)
    command.add_argument("--close-id", required=True)
    command.add_argument("--workspace-uuid", required=True)
    command = sub.add_parser("end-close")
    command.add_argument("runs_dir")
    command.add_argument("--feature", required=True)
    command.add_argument("--close-id", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    runs_dir = Path(args.runs_dir).resolve()
    try:
        if args.command == "acquire":
            result = acquire(
                runs_dir,
                run_id=args.run_id,
                feature=args.feature,
                instance=args.instance,
                role=args.role,
                phase=args.phase,
                resource_class=args.resource_class,
                task_sha256=args.task_sha256,
                workspace_uuid=args.workspace_uuid,
                surface_uuid=args.surface_uuid,
                max_local=args.max_local,
                role_limit=args.role_limit,
            )
            print(json.dumps(result, sort_keys=True))
        elif args.command == "acquire-frontier":
            lease = acquire_frontier(
                runs_dir,
                run_id=args.run_id,
                feature=args.feature,
                instance=args.instance,
                role=args.role,
                phase=args.phase,
                task_sha256=args.task_sha256,
                workspace_uuid=args.workspace_uuid,
                surface_uuid=args.surface_uuid,
            )
            print(lease)
        elif args.command == "reconcile":
            print(json.dumps(reconcile(runs_dir, feature=args.feature), sort_keys=True))
        elif args.command == "check":
            if not args.feature:
                raise LeaseError("check requires --feature")
            active = check_active(runs_dir, args.feature)
            if active:
                print("active leases: " + ", ".join(active), file=sys.stderr)
                return 75
            print("no active leases")
        elif args.command == "begin-close":
            marker = begin_close(
                runs_dir,
                feature=args.feature,
                close_id=args.close_id,
                workspace_uuid=args.workspace_uuid,
            )
            print(marker)
        elif args.command == "end-close":
            end_close(runs_dir, feature=args.feature, close_id=args.close_id)
        else:
            leases = [Path(value) for value in args.lease if value]
            if args.command == "validate":
                validate(runs_dir, args.run_id, leases)
            elif args.command == "activate":
                activate(runs_dir, args.run_id, leases, args.pid, args.pgid)
            elif args.command == "release":
                release(runs_dir, args.run_id, leases)
        return 0
    except LeaseBusy as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except LeaseError as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except RuntimeError as exc:
        print(f"lease reconciliation failed closed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
