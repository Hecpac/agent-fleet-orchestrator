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
import fleet_safe_paths


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


def _artifact_relative(mission_id: str, artifact_id: str) -> str:
    if not mission_state.SHA256.fullmatch(artifact_id):
        raise ArtifactError("invalid artifact_id")
    return f"missions/{mission_id}/artifacts/{artifact_id}"


def _lock_relative(mission_id: str) -> str:
    return f"missions/{mission_id}/.artifacts.lock"


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
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            with rooted.exclusive_lock(
                _lock_relative(mission_id),
                directory_modes=(0o700, 0o700),
            ):
                path = rooted.atomic_write(
                    _artifact_relative(mission_id, digest),
                    content,
                    directory_modes=(0o700, 0o700, 0o700),
                )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise ArtifactError(f"unsafe artifact store: {exc}") from exc
    return {"artifact_id": digest, "bytes": len(content), "path": str(path)}


def put_file(runs_dir: Path, mission_id: str, source: Path) -> dict[str, Any]:
    return put_bytes(runs_dir, mission_id, read_regular(source))


def get_bytes(runs_dir: Path, mission_id: str, artifact_id: str) -> bytes:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    relative = _artifact_relative(mission_id, artifact_id)
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            content = rooted.read_regular(
                relative,
                directory_modes=(0o700, 0o700, 0o700),
                max_bytes=MAX_ARTIFACT_BYTES,
            )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise ArtifactError(f"unsafe artifact store: {exc}") from exc
    if hashlib.sha256(content).hexdigest() != artifact_id:
        raise ArtifactError("artifact bytes do not match artifact_id")
    return content


def verify_store(runs_dir: Path, mission_id: str) -> dict[str, Any]:
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    store_relative = f"missions/{mission_id}/artifacts"
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            with rooted.exclusive_lock(
                _lock_relative(mission_id),
                directory_modes=(0o700, 0o700),
            ):
                try:
                    names = rooted.list_directory(
                        store_relative,
                        directory_modes=(0o700, 0o700, 0o700),
                    )
                except fleet_safe_paths.SafePathError as exc:
                    if not str(exc).startswith("rooted directory is missing:"):
                        raise
                    names = []
                count = 0
                total = 0
                for name in names:
                    if name.startswith("."):
                        raise ArtifactError(
                            "artifact store contains unexpected hidden entry"
                        )
                    if not mission_state.SHA256.fullmatch(name):
                        raise ArtifactError(
                            "artifact store contains an invalid artifact name"
                        )
                    content = rooted.read_regular(
                        _artifact_relative(mission_id, name),
                        directory_modes=(0o700, 0o700, 0o700),
                        max_bytes=MAX_ARTIFACT_BYTES,
                    )
                    if hashlib.sha256(content).hexdigest() != name:
                        raise ArtifactError("artifact bytes do not match artifact_id")
                    count += 1
                    total += len(content)
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise ArtifactError(f"unsafe artifact store: {exc}") from exc
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
