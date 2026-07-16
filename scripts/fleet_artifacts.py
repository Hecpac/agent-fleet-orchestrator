#!/usr/bin/env python3
"""Content-addressed, tamper-evident artifact storage for one mission."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

import fleet_mission_state as mission_state


MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


class ArtifactError(RuntimeError):
    """Artifact content or storage is unsafe or inconsistent."""


def store_path(runs_dir: Path, mission_id: str) -> Path:
    return mission_state.mission_root(runs_dir, mission_id) / "artifacts"


def lock_path(runs_dir: Path, mission_id: str) -> Path:
    return mission_state.mission_root(runs_dir, mission_id) / ".artifacts.lock"


def artifact_path(runs_dir: Path, mission_id: str, artifact_id: str) -> Path:
    if not mission_state.SHA256.fullmatch(artifact_id):
        raise ArtifactError("invalid artifact_id")
    return store_path(runs_dir, mission_id) / artifact_id


def read_regular(path: Path, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ArtifactError(f"cannot open artifact source: {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactError("artifact source is not a regular file")
        if info.st_size > max_bytes:
            raise ArtifactError(f"artifact exceeds {max_bytes} bytes")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise ArtifactError("artifact source changed during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise ArtifactError("artifact source grew during read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def put_bytes(runs_dir: Path, mission_id: str, content: bytes) -> dict[str, Any]:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    if len(content) > MAX_ARTIFACT_BYTES:
        raise ArtifactError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
    digest = hashlib.sha256(content).hexdigest()
    path = artifact_path(runs_dir, mission_id, digest)
    with mission_state.exclusive_lock(lock_path(runs_dir, mission_id)):
        if path.exists():
            if path.is_symlink() or read_regular(path) != content:
                raise ArtifactError("content-addressed artifact conflicts with stored bytes")
        else:
            mission_state.atomic_write(path, content)
    return {"artifact_id": digest, "bytes": len(content), "path": str(path)}


def put_file(runs_dir: Path, mission_id: str, source: Path) -> dict[str, Any]:
    return put_bytes(runs_dir, mission_id, read_regular(source))


def get_bytes(runs_dir: Path, mission_id: str, artifact_id: str) -> bytes:
    path = artifact_path(runs_dir, mission_id, artifact_id)
    content = read_regular(path)
    if hashlib.sha256(content).hexdigest() != artifact_id:
        raise ArtifactError("artifact bytes do not match artifact_id")
    return content


def verify_store(runs_dir: Path, mission_id: str) -> dict[str, Any]:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    with mission_state.exclusive_lock(lock_path(runs_dir, mission_id)):
        root = store_path(runs_dir, mission_id)
        if not root.exists():
            return {"mission_id": mission_id, "artifacts": 0, "bytes": 0, "valid": True}
        if root.is_symlink() or not root.is_dir():
            raise ArtifactError("artifact store is not a safe directory")
        count = 0
        total = 0
        for path in sorted(root.iterdir()):
            if path.name.startswith("."):
                raise ArtifactError("artifact store contains unexpected hidden entry")
            content = get_bytes(runs_dir, mission_id, path.name)
            count += 1
            total += len(content)
    return {"mission_id": mission_id, "artifacts": count, "bytes": total, "valid": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--mission-id", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    put = commands.add_parser("put")
    put.add_argument("source")
    get = commands.add_parser("get")
    get.add_argument("artifact_id")
    commands.add_parser("verify")
    args = parser.parse_args(argv)
    runs_dir = Path(args.runs_dir).resolve()
    try:
        if args.command == "put":
            print(json.dumps(put_file(runs_dir, args.mission_id, Path(args.source)), sort_keys=True))
        elif args.command == "get":
            sys.stdout.buffer.write(get_bytes(runs_dir, args.mission_id, args.artifact_id))
        else:
            print(json.dumps(verify_store(runs_dir, args.mission_id), sort_keys=True))
        return 0
    except (ArtifactError, mission_state.MissionStateError) as exc:
        print(f"artifact error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
