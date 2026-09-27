#!/usr/bin/env python3
"""Standalone SDD stage-2 primitive: Mission-bound immutable plan snapshots.

This module freezes one validated ``fleet.sdd.plan.v1`` document per Mission as
exact raw bytes plus an immutable binding, and recovers it without rereading the
original source. It is deliberately standalone: it does not touch the Mission
driver, the controller ledger, CAS acceptance or archive verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

try:  # imported as ``scripts.fleet_sdd_snapshot``
    from . import fleet_sdd_contract as _contract
    from . import fleet_safe_paths as _safe_paths
    from . import fleet_json
except ImportError:  # executed directly as ``python3 scripts/fleet_sdd_snapshot.py``
    import fleet_sdd_contract as _contract
    import fleet_safe_paths as _safe_paths
    import fleet_json


SCHEMA = "fleet.sdd.snapshot.v1"
FUNCTIONAL_STATUS = "NOT_VERIFIED"
MAX_PLAN_BYTES = 16 * 1024 * 1024
MANIFEST_FIELDS = ("schema", "mission_id", "plan_sha256", "functional_status")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIRECTORY_MODES = (0o700, 0o700)


class SnapshotError(ValueError):
    """A snapshot input, identifier or stored state was rejected."""


def _canonical_mission_id(value: object) -> str:
    if not isinstance(value, str):
        raise SnapshotError("mission_id must be canonical lowercase UUID text")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise SnapshotError("mission_id must be canonical lowercase UUID text") from exc
    if str(parsed) != value:
        raise SnapshotError("mission_id must be canonical lowercase UUID text")
    return value


def _canonical_sha256(value: object) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise SnapshotError("sha256 must be exactly 64 lowercase hex characters")
    return value


def _parse_json(raw: bytes, where: str) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"{where} is not valid UTF-8") from exc
    try:
        return fleet_json.loads(text)
    except fleet_json.FleetJSONError as exc:
        raise SnapshotError(f"{where} is not valid JSON: {exc}") from exc


def _validate_plan(document: object, where: str) -> None:
    try:
        _contract.validate(document)
    except ValueError as exc:
        raise SnapshotError(f"{where} failed SDD validation: {exc}") from exc


def parse_plan_bytes(raw: bytes, where: str = "plan") -> dict:
    """Strictly parse and SDD-validate exact frozen plan bytes."""
    document = _parse_json(raw, where)
    _validate_plan(document, where)
    return document


def parse_manifest_bytes(raw: bytes) -> dict:
    """Strictly parse a snapshot binding manifest."""
    return _parse_manifest(raw)


def _read_source(source: Path) -> bytes:
    # O_NONBLOCK keeps a writer-less FIFO/device from blocking the open; the
    # regular-file check below then rejects it without ever reading it.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(Path(source), flags)
    except OSError as exc:
        raise SnapshotError(f"cannot read source plan: {exc}") from exc
    try:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise SnapshotError(f"cannot inspect source plan: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotError("source plan is not a regular file")
        if info.st_size > MAX_PLAN_BYTES:
            raise SnapshotError("source plan exceeds size limit")
        remaining = info.st_size
        chunks = []
        try:
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise SnapshotError("source plan changed while reading")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise SnapshotError("source plan grew while reading")
        except OSError as exc:
            raise SnapshotError(f"cannot read source plan: {exc}") from exc
        return b"".join(chunks)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _store_root(store: Path, *, create: bool) -> Path:
    target = Path(store)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise SnapshotError(f"cannot inspect store: {exc}") from exc
    if info is not None:
        if stat.S_ISLNK(info.st_mode):
            raise SnapshotError("store must not be a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise SnapshotError("store is not a directory")
    else:
        if not create:
            raise SnapshotError("store is missing")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SnapshotError(f"cannot create store: {exc}") from exc
        info = os.lstat(target)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SnapshotError("store is not a plain directory")
    try:
        return _safe_paths.canonical_root(target)
    except _safe_paths.SafePathError as exc:
        raise SnapshotError(f"unsafe store: {exc}") from exc


def _blob_relative(digest: str) -> str:
    return f"sdd/blobs/{digest}.json"


def _binding_relative(mission_id: str) -> str:
    return f"sdd/missions/{mission_id}.json"


def _read_optional(root: Path, relative: str, where: str) -> bytes | None:
    try:
        with _safe_paths.RootedFS(root) as fs:
            return fs.read_regular_optional(
                relative, directory_modes=_DIRECTORY_MODES, max_bytes=MAX_PLAN_BYTES
            )
    except _safe_paths.SafePathError as exc:
        if str(exc).startswith("rooted directory is missing:"):
            return None
        raise SnapshotError(f"{where} unsafe or unavailable: {exc}") from exc


def _read_required(root: Path, relative: str, where: str) -> bytes:
    try:
        with _safe_paths.RootedFS(root) as fs:
            return fs.read_regular(
                relative, directory_modes=_DIRECTORY_MODES, max_bytes=MAX_PLAN_BYTES
            )
    except _safe_paths.SafePathError as exc:
        raise SnapshotError(f"{where} unsafe or unavailable: {exc}") from exc


def _atomic_write(root: Path, relative: str, content: bytes) -> None:
    try:
        with _safe_paths.RootedFS(root) as fs:
            fs.atomic_write(
                relative,
                content,
                directory_modes=_DIRECTORY_MODES,
                file_mode=0o600,
            )
    except _safe_paths.SafePathError as exc:
        raise SnapshotError(f"cannot publish snapshot: {exc}") from exc


def _parse_manifest(raw: bytes) -> dict:
    document = _parse_json(raw, "manifest")
    if not isinstance(document, dict) or set(document) != set(MANIFEST_FIELDS):
        raise SnapshotError("manifest has unexpected fields")
    if document["schema"] != SCHEMA:
        raise SnapshotError("unsupported snapshot schema")
    _canonical_mission_id(document["mission_id"])
    _canonical_sha256(document["plan_sha256"])
    if document["functional_status"] != FUNCTIONAL_STATUS:
        raise SnapshotError("invalid manifest functional_status")
    return document


def _manifest_bytes(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verified_blob(root: Path, digest: str) -> bytes:
    raw = _read_required(root, _blob_relative(digest), "plan blob")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise SnapshotError("plan blob content does not match its digest")
    return raw


def freeze_plan(store: Path, mission_id: str, source: Path) -> dict:
    """Freeze one validated plan for a Mission; return the immutable manifest."""
    mission_id = _canonical_mission_id(mission_id)
    raw = _read_source(source)
    digest = hashlib.sha256(raw).hexdigest()
    document = _parse_json(raw, "source plan")
    _validate_plan(document, "source plan")
    manifest = {
        "schema": SCHEMA,
        "mission_id": mission_id,
        "plan_sha256": digest,
        "functional_status": FUNCTIONAL_STATUS,
    }
    root = _store_root(store, create=True)
    existing = _read_optional(root, _binding_relative(mission_id), "Mission binding")
    if existing is not None:
        found = _parse_manifest(existing)
        if found["mission_id"] != mission_id:
            raise SnapshotError("stored binding belongs to another Mission")
        if found["plan_sha256"] != digest:
            raise SnapshotError("a different plan is already bound to this Mission")
        _verified_blob(root, digest)
        if _manifest_bytes(found) != _manifest_bytes(manifest):
            raise SnapshotError("stored binding is inconsistent with its manifest")
        return found
    _atomic_write(root, _blob_relative(digest), raw)
    _atomic_write(root, _binding_relative(mission_id), _manifest_bytes(manifest))
    return manifest


def load_plan(store: Path, mission_id: str, expected_sha256: str) -> dict:
    """Recover the frozen plan after verifying manifest, Mission, pin and blob."""
    mission_id = _canonical_mission_id(mission_id)
    expected_sha256 = _canonical_sha256(expected_sha256)
    root = _store_root(store, create=False)
    stored = _read_required(root, _binding_relative(mission_id), "Mission binding")
    manifest = _parse_manifest(stored)
    if manifest["mission_id"] != mission_id:
        raise SnapshotError("binding belongs to another Mission")
    if manifest["plan_sha256"] != expected_sha256:
        raise SnapshotError("binding does not match the expected digest")
    raw = _verified_blob(root, expected_sha256)
    document = _parse_json(raw, "stored plan")
    _validate_plan(document, "stored plan")
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="freeze a plan for a Mission")
    freeze.add_argument("--store", required=True)
    freeze.add_argument("--mission-id", required=True)
    freeze.add_argument("--plan", required=True)
    load = commands.add_parser("load", help="recover a frozen plan")
    load.add_argument("--store", required=True)
    load.add_argument("--mission-id", required=True)
    load.add_argument("--sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "freeze":
            result = freeze_plan(Path(args.store), args.mission_id, Path(args.plan))
        else:
            result = load_plan(Path(args.store), args.mission_id, args.sha256)
    except SnapshotError as exc:
        print(f"snapshot error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
