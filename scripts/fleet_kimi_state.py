#!/usr/bin/env python3
"""Descriptor-bound lifecycle for persistent, surface-scoped Kimi state."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid
from typing import Any

import fleet_json
import fleet_safe_paths


SCHEMA_VERSION = 1
MAX_BINDING_BYTES = 64 * 1024
SESSION_FILE = "kimi-hook-sessions.json"
SESSION_LOCK = ".kimi-hook-sessions.lock"
SURFACE_BINDING = "binding.json"
SURFACE_LOCK = ".bridge.lock"
BRIDGE_HEALTH = "bridge-health.json"
BRIDGE_RECEIPT = "bridge-exit.json"
LIFECYCLE_LOCK = ".lifecycle.lock"
RETIRED_DIR = ".retired"
RETIRED_SENSITIVE_FILES = (
    ("share/config.toml", (0o700, 0o700)),
    ("share/credentials/kimi-code.json", (0o700, 0o700, 0o700)),
    ("share/mcp.json", (0o700, 0o700)),
)
_SAFE_MODEL_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_SESSION_KEYS = {
    "sessionId", "workspaceId", "surfaceId", "missionId", "generationId",
    "transcriptPath", "provider", "model", "updatedAt",
}
_LEGACY_SESSION_KEYS = _SESSION_KEYS - {"missionId", "generationId"}


class KimiStateError(RuntimeError):
    """Persistent Kimi state violated its physical or logical binding."""


def canonical_uuid(value: str, field: str, *, upper: bool = False) -> str:
    try:
        normalized = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise KimiStateError(f"{field} must be a canonical UUID") from exc
    if value.lower() != normalized:
        raise KimiStateError(f"{field} must be a canonical UUID")
    return normalized.upper() if upper else normalized


def canonical_mission(value: str) -> str:
    return canonical_uuid(value, "mission_id") if value else ""


def _canonical_json(value: dict[str, Any]) -> bytes:
    try:
        return fleet_json.canonical_bytes(value) + b"\n"
    except fleet_json.FleetJSONError as exc:
        raise KimiStateError("Kimi state cannot be canonicalized") from exc


def _parse_object(raw: bytes, where: str) -> dict[str, Any]:
    try:
        value = fleet_json.loads(raw)
    except fleet_json.FleetJSONError as exc:
        raise KimiStateError(f"{where} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise KimiStateError(f"{where} must be a JSON object")
    return value


def ensure_private_root(path: Path) -> Path:
    if not path.is_absolute():
        raise KimiStateError("Kimi state root must be absolute")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise KimiStateError("cannot create Kimi state root") from exc
    try:
        return fleet_safe_paths.canonical_root(path, required_mode=0o700)
    except fleet_safe_paths.SafePathError as exc:
        raise KimiStateError(f"unsafe Kimi state root: {exc}") from exc


def _read_source(path: Path, field: str, *, required: bool) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if not required:
            return None
        raise KimiStateError(f"{field} is unavailable")
    except OSError as exc:
        raise KimiStateError(f"cannot open {field}") from exc
    try:
        opened = os.fstat(fd)
        current = path.lstat()
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid():
            raise KimiStateError(f"{field} must be an owner-bound regular file")
        if opened.st_nlink != 1:
            raise KimiStateError(f"{field} must have one link")
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise KimiStateError(f"{field} binding changed")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        if os.fstat(fd).st_size != sum(map(len, chunks)):
            raise KimiStateError(f"{field} changed during read")
        return b"".join(chunks)
    except OSError as exc:
        raise KimiStateError(f"cannot read {field}") from exc
    finally:
        os.close(fd)


def binding_document(
    *, surface_id: str, workspace_id: str, mission_id: str, generation_id: str
) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "surfaceId": canonical_uuid(surface_id, "surface_id", upper=True),
        "workspaceId": canonical_uuid(workspace_id, "workspace_id", upper=True),
        "missionId": canonical_mission(mission_id),
        "generationId": canonical_uuid(generation_id, "generation_id"),
        "state": "active",
    }


def _validate_binding(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "schemaVersion", "surfaceId", "workspaceId", "missionId",
        "generationId", "state",
    } or value.get("schemaVersion") != SCHEMA_VERSION or value.get("state") != "active":
        raise KimiStateError("Kimi surface binding fields do not match schema v1")
    expected = binding_document(
        surface_id=value.get("surfaceId"),
        workspace_id=value.get("workspaceId"),
        mission_id=value.get("missionId"),
        generation_id=value.get("generationId"),
    )
    if value != expected:
        raise KimiStateError("Kimi surface binding is not canonical")
    return value


def read_surface_binding(root: Path, surface_id: str) -> dict[str, Any]:
    root = ensure_private_root(root)
    surface = canonical_uuid(surface_id, "surface_id", upper=True)
    try:
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            raw = rooted.read_regular(
                f"{surface}/{SURFACE_BINDING}",
                directory_modes=(0o700,),
                file_mode=0o600,
                max_bytes=MAX_BINDING_BYTES,
            )
    except fleet_safe_paths.SafePathError as exc:
        raise KimiStateError(f"unsafe Kimi surface binding: {exc}") from exc
    value = _validate_binding(_parse_object(raw, "Kimi surface binding"))
    if raw != _canonical_json(value):
        raise KimiStateError("Kimi surface binding bytes are not canonical")
    return value


def bridge_document(
    *,
    surface_id: str,
    workspace_id: str,
    mission_id: str,
    generation_id: str,
    launch_id: str,
    pid: int,
    started_at: int,
    status: str,
    ended_at: int | None = None,
    exit_code: int | None = None,
    stderr_sha256: str | None = None,
    stderr_tail: str | None = None,
) -> dict[str, Any]:
    """Build canonical, owner-bound bridge health or terminal evidence."""
    if (
        status not in {"ready", "exited"}
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
    ):
        raise KimiStateError("Kimi bridge status is invalid")
    if (
        isinstance(started_at, bool)
        or not isinstance(started_at, int)
        or started_at <= 0
    ):
        raise KimiStateError("Kimi bridge start timestamp is invalid")
    value: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "surfaceId": canonical_uuid(surface_id, "surface_id", upper=True),
        "workspaceId": canonical_uuid(workspace_id, "workspace_id", upper=True),
        "missionId": canonical_mission(mission_id),
        "generationId": canonical_uuid(generation_id, "generation_id"),
        "launchId": canonical_uuid(launch_id, "launch_id"),
        "pid": pid,
        "startedAt": started_at,
        "status": status,
    }
    if status == "exited":
        if (
            isinstance(ended_at, bool)
            or not isinstance(ended_at, int)
            or ended_at < started_at
        ):
            raise KimiStateError("Kimi bridge end timestamp is invalid")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise KimiStateError("Kimi bridge exit code is invalid")
        if not isinstance(stderr_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", stderr_sha256
        ):
            raise KimiStateError("Kimi bridge stderr digest is invalid")
        if (
            not isinstance(stderr_tail, str)
            or len(stderr_tail.encode("utf-8")) > 1024
            or "\x00" in stderr_tail
        ):
            raise KimiStateError("Kimi bridge stderr summary is invalid")
        value.update(
            {
                "endedAt": ended_at,
                "exitCode": exit_code,
                "stderrSha256": stderr_sha256,
                "stderrTail": stderr_tail,
            }
        )
    return value


def _read_bridge_document(
    rooted: fleet_safe_paths.RootedFS,
    surface: str,
    name: str,
) -> tuple[bytes, dict[str, Any]] | None:
    raw = rooted.read_regular_optional(
        f"{surface}/{name}",
        directory_modes=(0o700,),
        file_mode=0o600,
        max_bytes=MAX_BINDING_BYTES,
    )
    if raw is None:
        return None
    value = _parse_object(raw, f"Kimi {name}")
    return raw, value


def publish_bridge_status(root: Path, document: dict[str, Any]) -> None:
    """Publish one canonical health/exit record; conflicts fail closed."""
    expected = bridge_document(
        surface_id=document.get("surfaceId"),
        workspace_id=document.get("workspaceId"),
        mission_id=document.get("missionId"),
        generation_id=document.get("generationId"),
        launch_id=document.get("launchId"),
        pid=document.get("pid"),
        started_at=document.get("startedAt"),
        status=document.get("status"),
        ended_at=document.get("endedAt"),
        exit_code=document.get("exitCode"),
        stderr_sha256=document.get("stderrSha256"),
        stderr_tail=document.get("stderrTail"),
    )
    if document != expected:
        raise KimiStateError("Kimi bridge status is not canonical")
    root = ensure_private_root(root)
    surface = expected["surfaceId"]
    name = BRIDGE_HEALTH if expected["status"] == "ready" else BRIDGE_RECEIPT
    try:
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            binding_raw = rooted.read_regular(
                f"{surface}/{SURFACE_BINDING}",
                directory_modes=(0o700,),
                file_mode=0o600,
                max_bytes=MAX_BINDING_BYTES,
            )
            binding = _validate_binding(
                _parse_object(binding_raw, "Kimi surface binding")
            )
            expected_binding = binding_document(
                surface_id=expected["surfaceId"],
                workspace_id=expected["workspaceId"],
                mission_id=expected["missionId"],
                generation_id=expected["generationId"],
            )
            if binding_raw != _canonical_json(binding) or binding != expected_binding:
                raise KimiStateError("Kimi bridge lifecycle identity drift")
            if expected["status"] == "exited":
                health_record = _read_bridge_document(rooted, surface, BRIDGE_HEALTH)
                if health_record is not None:
                    health_raw, health = health_record
                    canonical_health = bridge_document(
                        surface_id=health.get("surfaceId"),
                        workspace_id=health.get("workspaceId"),
                        mission_id=health.get("missionId"),
                        generation_id=health.get("generationId"),
                        launch_id=health.get("launchId"),
                        pid=health.get("pid"),
                        started_at=health.get("startedAt"),
                        status="ready",
                    )
                    if (
                        health_raw != _canonical_json(canonical_health)
                        or health != canonical_health
                        or any(
                            expected[key] != canonical_health[key]
                            for key in (
                                "surfaceId",
                                "workspaceId",
                                "missionId",
                                "generationId",
                                "launchId",
                                "pid",
                                "startedAt",
                            )
                        )
                    ):
                        raise KimiStateError("Kimi bridge lifecycle identity drift")
            rooted.atomic_write(
                f"{surface}/{name}",
                _canonical_json(expected),
                directory_modes=(0o700,),
                file_mode=0o600,
                require_absent=True,
            )
    except fleet_safe_paths.SafePathError as exc:
        raise KimiStateError(f"unsafe Kimi bridge status: {exc}") from exc


def require_bridge_health(
    root: Path,
    *,
    surface_id: str,
    workspace_id: str,
    mission_id: str,
    generation_id: str,
) -> dict[str, Any]:
    """Return only a live exact bridge with no terminal receipt."""
    root = ensure_private_root(root)
    surface = canonical_uuid(surface_id, "surface_id", upper=True)
    try:
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            if _read_bridge_document(rooted, surface, BRIDGE_RECEIPT) is not None:
                raise KimiStateError("Kimi bridge has already exited")
            record = _read_bridge_document(rooted, surface, BRIDGE_HEALTH)
            if record is None:
                raise KimiStateError("Kimi bridge health is unavailable")
            raw, value = record
        expected = bridge_document(
            surface_id=surface_id,
            workspace_id=workspace_id,
            mission_id=mission_id,
            generation_id=generation_id,
            launch_id=value.get("launchId"),
            pid=value.get("pid"),
            started_at=value.get("startedAt"),
            status="ready",
        )
        if raw != _canonical_json(expected) or value != expected:
            raise KimiStateError("Kimi bridge health is not canonical")
        # A pid proves nothing once the supervisor dies without writing its exit
        # receipt: the operating system recycles the number and `os.kill(pid, 0)`
        # then reports an unrelated process as the bridge. The live hook bridge
        # holds SURFACE_LOCK for its whole lifetime and the kernel releases it on
        # exit, so acquiring that lock here means no bridge is running.
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            with rooted.exclusive_lock(
                f"{surface}/{SURFACE_LOCK}",
                directory_modes=(0o700,),
                create_directories=False,
                blocking=False,
            ) as acquired:
                if acquired:
                    raise KimiStateError("Kimi bridge is not alive")
        return expected
    except fleet_safe_paths.SafePathError as exc:
        raise KimiStateError(f"unsafe Kimi bridge health: {exc}") from exc


def provision(
    root: Path,
    *,
    surface_id: str,
    workspace_id: str,
    mission_id: str,
    generation_id: str,
    config_source: Path,
    credential_source: Path | None,
    mcp_command: str,
    mcp_proxy: str,
) -> Path:
    root = ensure_private_root(root)
    binding = binding_document(
        surface_id=surface_id,
        workspace_id=workspace_id,
        mission_id=mission_id,
        generation_id=generation_id,
    )
    surface = binding["surfaceId"]
    config = _read_source(config_source, "Kimi controller config", required=True)
    credential = (
        _read_source(credential_source, "Kimi controller credential", required=False)
        if credential_source is not None else None
    )
    if not mcp_command or "\0" in mcp_command or not mcp_proxy or "\0" in mcp_proxy:
        raise KimiStateError("Kimi MCP binding is invalid")
    mcp = _canonical_json({"mcpServers": {"fleet_control": {
        "command": mcp_command, "args": [mcp_proxy]
    }}})
    try:
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            with rooted.exclusive_lock(
                LIFECYCLE_LOCK, directory_modes=(), file_mode=0o600
            ) as locked:
                if not locked:
                    raise KimiStateError("Kimi lifecycle lock is unavailable")
                try:
                    rooted.list_directory(surface, directory_modes=(0o700,))
                    surface_exists = True
                except fleet_safe_paths.SafePathError as exc:
                    if "is missing" not in str(exc):
                        raise
                    surface_exists = False
                existing = (
                    rooted.read_regular_optional(
                        f"{surface}/{SURFACE_BINDING}",
                        directory_modes=(0o700,), file_mode=0o600,
                        max_bytes=MAX_BINDING_BYTES,
                    )
                    if surface_exists else None
                )
                if surface_exists and existing is None:
                    raise KimiStateError(
                        "existing Kimi surface lacks a lifecycle binding"
                    )
                if existing is not None:
                    current = _validate_binding(
                        _parse_object(existing, "Kimi surface binding")
                    )
                    if existing != _canonical_json(current) or current != binding:
                        raise KimiStateError(
                            "Kimi surface belongs to another lifecycle generation"
                        )
                rooted.atomic_write(
                    f"{surface}/{SURFACE_BINDING}", _canonical_json(binding),
                    directory_modes=(0o700,), file_mode=0o600,
                    require_absent=existing is None,
                )
                rooted.atomic_write(
                    f"{surface}/share/config.toml", config or b"",
                    directory_modes=(0o700, 0o700), file_mode=0o600,
                )
                if credential is not None:
                    rooted.atomic_write(
                        f"{surface}/share/credentials/kimi-code.json", credential,
                        directory_modes=(0o700, 0o700, 0o700), file_mode=0o600,
                    )
                rooted.atomic_write(
                    f"{surface}/share/mcp.json", mcp,
                    directory_modes=(0o700, 0o700), file_mode=0o600,
                )
                rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise KimiStateError(f"unsafe Kimi state destination: {exc}") from exc
    return root / surface / "share"


def validate_wire_path(share_dir: Path, relative: str) -> Path:
    try:
        root = fleet_safe_paths.canonical_root(share_dir, required_mode=0o700)
        parts = Path(relative).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise KimiStateError("Kimi transcript path is unsafe")
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            rooted.stat_regular(
                relative,
                directory_modes=tuple(0o700 for _ in parts[:-1]),
                file_mode=0o600,
                require_single_link=True,
            )
    except fleet_safe_paths.SafePathError as exc:
        raise KimiStateError(f"unsafe Kimi transcript: {exc}") from exc
    return root.joinpath(*parts)


def validate_wire_destination(share_dir: Path, transcript: Path) -> Path:
    try:
        root = fleet_safe_paths.canonical_root(share_dir, required_mode=0o700)
        relative = transcript.relative_to(root)
        parts = relative.parts
        if len(parts) < 2 or parts[-1] != "wire.jsonl":
            raise KimiStateError("Kimi transcript destination is invalid")
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            rooted.list_directory(
                Path(*parts[:-1]),
                directory_modes=tuple(0o700 for _ in parts[:-1]),
            )
        return root.joinpath(*parts)
    except (ValueError, fleet_safe_paths.SafePathError) as exc:
        raise KimiStateError(f"unsafe Kimi transcript destination: {exc}") from exc


def read_wire(
    share_dir: Path, transcript: Path, *, missing_ok: bool = False
) -> bytes | None:
    try:
        root = fleet_safe_paths.canonical_root(share_dir, required_mode=0o700)
        relative = transcript.relative_to(root)
        parts = relative.parts
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            raw = rooted.read_regular_optional(
                relative,
                directory_modes=tuple(0o700 for _ in parts[:-1]),
                file_mode=0o600,
                max_bytes=64 * 1024 * 1024,
                require_single_link=True,
            )
            if raw is None and not missing_ok:
                raise KimiStateError("Kimi transcript is unavailable")
            return raw
    except (ValueError, fleet_safe_paths.SafePathError) as exc:
        raise KimiStateError(f"unsafe Kimi transcript: {exc}") from exc


def update_session_binding(
    hook_dir: Path,
    *,
    share_dir: Path,
    session_id: str,
    workspace_id: str,
    surface_id: str,
    mission_id: str,
    generation_id: str,
    transcript_path: Path,
    provider: str,
    model: str,
) -> None:
    hook_root = fleet_safe_paths.canonical_root(hook_dir, required_mode=0o700)
    session = canonical_uuid(session_id, "session_id")
    if not _SAFE_MODEL_VALUE.fullmatch(provider) or not _SAFE_MODEL_VALUE.fullmatch(model):
        raise KimiStateError("Kimi provider identity is invalid")
    transcript_path = validate_wire_destination(share_dir, transcript_path)
    record = {
        "sessionId": session,
        "workspaceId": canonical_uuid(workspace_id, "workspace_id", upper=True),
        "surfaceId": canonical_uuid(surface_id, "surface_id", upper=True),
        "missionId": canonical_mission(mission_id),
        "generationId": canonical_uuid(generation_id, "generation_id"),
        "transcriptPath": str(transcript_path),
        "provider": provider,
        "model": model,
        "updatedAt": int(time.time() * 1000),
    }
    try:
        with fleet_safe_paths.RootedFS(hook_root, root_mode=0o700) as rooted:
            with rooted.exclusive_lock(SESSION_LOCK, directory_modes=()) as locked:
                if not locked:
                    raise KimiStateError("Kimi session lock is unavailable")
                raw = rooted.read_regular_optional(
                    SESSION_FILE, directory_modes=(), file_mode=0o600,
                    max_bytes=MAX_BINDING_BYTES,
                )
                document = {"sessions": {}} if raw is None else _parse_object(
                    raw, "Kimi session binding"
                )
                if set(document) != {"sessions"} or not isinstance(document["sessions"], dict):
                    raise KimiStateError("Kimi session binding has invalid fields")
                for key, value in document["sessions"].items():
                    _validate_session_record(key, value)
                current = document["sessions"].get(session)
                if isinstance(current, dict) and any(
                    current.get(key) != record[key]
                    for key in ("sessionId", "workspaceId", "surfaceId", "missionId", "generationId", "transcriptPath")
                ):
                    raise KimiStateError("Kimi session belongs to another lifecycle generation")
                document["sessions"][session] = record
                content = _canonical_json(document)
                if raw is None:
                    rooted.atomic_write(
                        SESSION_FILE, content, directory_modes=(), file_mode=0o600
                    )
                else:
                    rooted.replace_regular(
                        SESSION_FILE, content, directory_modes=(), file_mode=0o600
                    )
    except fleet_safe_paths.SafePathError as exc:
        raise KimiStateError(f"unsafe Kimi session binding: {exc}") from exc


def remove_session_binding(
    hook_dir: Path, *, session_id: str, expected: dict[str, Any]
) -> None:
    hook_root = fleet_safe_paths.canonical_root(hook_dir, required_mode=0o700)
    session = canonical_uuid(session_id, "session_id")
    try:
        with fleet_safe_paths.RootedFS(hook_root, root_mode=0o700) as rooted:
            with rooted.exclusive_lock(SESSION_LOCK, directory_modes=()) as locked:
                if not locked:
                    raise KimiStateError("Kimi session lock is unavailable")
                raw = rooted.read_regular_optional(
                    SESSION_FILE, directory_modes=(), file_mode=0o600,
                    max_bytes=MAX_BINDING_BYTES,
                )
                if raw is None:
                    return
                document = _parse_object(raw, "Kimi session binding")
                sessions = document.get("sessions")
                if set(document) != {"sessions"} or not isinstance(sessions, dict):
                    raise KimiStateError("Kimi session binding has invalid fields")
                for key, value in sessions.items():
                    _validate_session_record(key, value)
                current = sessions.get(session)
                if current is None:
                    return
                if not isinstance(current, dict) or any(
                    current.get(key) != expected[key]
                    for key in ("workspaceId", "surfaceId", "missionId", "generationId")
                ):
                    raise KimiStateError("refusing to remove a different Kimi session binding")
                del sessions[session]
                rooted.replace_regular(
                    SESSION_FILE, _canonical_json(document), directory_modes=(), file_mode=0o600
                )
    except fleet_safe_paths.SafePathError as exc:
        raise KimiStateError(f"unsafe Kimi session binding: {exc}") from exc


def _validate_session_record(session_id: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) not in {
        frozenset(_SESSION_KEYS),
        frozenset(_LEGACY_SESSION_KEYS),
    }:
        raise KimiStateError("Kimi session record fields do not match schema v1")
    if value.get("sessionId") != canonical_uuid(session_id, "session_id"):
        raise KimiStateError("Kimi session record id mismatch")
    canonical_uuid(value.get("workspaceId"), "workspace_id", upper=True)
    canonical_uuid(value.get("surfaceId"), "surface_id", upper=True)
    if set(value) == _SESSION_KEYS:
        canonical_mission(value.get("missionId"))
        canonical_uuid(value.get("generationId"), "generation_id")
    transcript = value.get("transcriptPath")
    if not isinstance(transcript, str) or not Path(transcript).is_absolute() or "\0" in transcript:
        raise KimiStateError("Kimi session transcript path is invalid")
    if not _SAFE_MODEL_VALUE.fullmatch(value.get("provider", "")) or not _SAFE_MODEL_VALUE.fullmatch(value.get("model", "")):
        raise KimiStateError("Kimi session provider identity is invalid")
    updated = value.get("updatedAt")
    if isinstance(updated, bool) or not isinstance(updated, int) or updated <= 0:
        raise KimiStateError("Kimi session timestamp is invalid")
    return value


def read_session_binding(hook_dir: Path, session_id: str) -> dict[str, Any] | None:
    session = canonical_uuid(session_id, "session_id")
    try:
        hook_root = fleet_safe_paths.canonical_root(hook_dir, required_mode=0o700)
        with fleet_safe_paths.RootedFS(hook_root, root_mode=0o700) as rooted:
            raw = rooted.read_regular_optional(
                SESSION_FILE, directory_modes=(), file_mode=0o600,
                max_bytes=MAX_BINDING_BYTES,
            )
    except fleet_safe_paths.SafePathError as exc:
        raise KimiStateError(f"unsafe Kimi session binding: {exc}") from exc
    if raw is None:
        return None
    document = _parse_object(raw, "Kimi session binding")
    sessions = document.get("sessions")
    if set(document) != {"sessions"} or not isinstance(sessions, dict):
        raise KimiStateError("Kimi session binding has invalid fields")
    for key, value in sessions.items():
        _validate_session_record(key, value)
    if raw != _canonical_json(document):
        raise KimiStateError("Kimi session binding bytes are not canonical")
    value = sessions.get(session)
    return value if isinstance(value, dict) else None


def _retire_directory_noreplace(
    root: Path, surface: str, retired_parent: Path, retired_name: str
) -> None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    root_fd = os.open(root, directory_flags)
    retired_fd = os.open(retired_parent, directory_flags)
    try:
        source = os.stat(surface, dir_fd=root_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(source.st_mode)
            or source.st_uid != os.geteuid()
            or stat.S_IMODE(source.st_mode) != 0o700
        ):
            raise KimiStateError("Kimi retirement source is not an owner-bound directory")
        try:
            os.stat(retired_name, dir_fd=retired_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise KimiStateError("Kimi retirement destination already exists")
        try:
            fleet_safe_paths._rename_noreplace(  # type: ignore[attr-defined]
                surface,
                retired_name,
                source_fd=root_fd,
                destination_fd=retired_fd,
            )
            os.fsync(root_fd)
            os.fsync(retired_fd)
        except OSError as exc:
            raise KimiStateError("Kimi retirement binding changed") from exc
    finally:
        os.close(retired_fd)
        os.close(root_fd)


def _scrub_retired_sensitive_files(retired_parent: Path, retired_name: str) -> None:
    """Remove ephemeral credentials/config after the generation is quarantined."""

    with fleet_safe_paths.RootedFS(retired_parent, root_mode=0o700) as rooted:
        for relative, directory_modes in RETIRED_SENSITIVE_FILES:
            rooted.unlink_regular(
                f"{retired_name}/{relative}",
                directory_modes=directory_modes,
                file_mode=0o600,
                missing_ok=True,
            )


def retire_surface(
    root: Path,
    *,
    hook_dir: Path,
    surface_id: str,
    workspace_id: str,
    mission_id: str,
    generation_id: str,
) -> Path:
    root = ensure_private_root(root)
    expected = binding_document(
        surface_id=surface_id, workspace_id=workspace_id,
        mission_id=mission_id, generation_id=generation_id,
    )
    surface = expected["surfaceId"]
    retired_name = f"{surface}--{expected['generationId']}"
    retired_parent = root / RETIRED_DIR
    try:
        retired_parent.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            rooted.list_directory(RETIRED_DIR, directory_modes=(0o700,))
            with rooted.exclusive_lock(LIFECYCLE_LOCK, directory_modes=()) as locked:
                if not locked:
                    raise KimiStateError("Kimi lifecycle lock is unavailable")
                active = root / surface
                retired = retired_parent / retired_name
                source = active if active.exists() else retired
                if source not in (active, retired) or not source.exists():
                    raise KimiStateError("Kimi surface state is unavailable")
                binding_root = root if source == active else retired_parent
                binding_relative = (
                    f"{surface}/{SURFACE_BINDING}" if source == active
                    else f"{retired_name}/{SURFACE_BINDING}"
                )
                binding_modes = (0o700,)
                with fleet_safe_paths.RootedFS(binding_root, root_mode=0o700) as state_fs:
                    raw = state_fs.read_regular(
                        binding_relative, directory_modes=binding_modes,
                        file_mode=0o600, max_bytes=MAX_BINDING_BYTES,
                    )
                current = _validate_binding(_parse_object(raw, "Kimi surface binding"))
                if raw != _canonical_json(current) or current != expected:
                    raise KimiStateError("refusing to retire a different Kimi lifecycle generation")
                if source == active:
                    with rooted.exclusive_lock(
                        f"{surface}/{SURFACE_LOCK}", directory_modes=(0o700,),
                        create_directories=False, blocking=False,
                    ) as bridge_quiescent:
                        if not bridge_quiescent:
                            raise KimiStateError("Kimi bridge is still active")
                        _retire_directory_noreplace(
                            root, surface, retired_parent, retired_name
                        )
                        rooted.assert_root_binding()
                _scrub_retired_sensitive_files(retired_parent, retired_name)
                remove_session_binding(
                    hook_dir,
                    session_id=canonical_uuid(surface_id, "surface_id"),
                    expected=expected,
                )
                return retired
    except fleet_safe_paths.SafePathError as exc:
        raise KimiStateError(f"unsafe Kimi retirement path: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    provision_parser = sub.add_parser("provision")
    retire_parser = sub.add_parser("retire")
    for current in (provision_parser, retire_parser):
        current.add_argument("--root", required=True)
        current.add_argument("--surface-id", required=True)
        current.add_argument("--workspace-id", required=True)
        current.add_argument("--mission-id", default="")
        current.add_argument("--generation-id", required=True)
    provision_parser.add_argument("--config-source", required=True)
    provision_parser.add_argument("--credential-source")
    provision_parser.add_argument("--mcp-command", required=True)
    provision_parser.add_argument("--mcp-proxy", required=True)
    retire_parser.add_argument("--hook-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "provision":
            path = provision(
                Path(args.root), surface_id=args.surface_id,
                workspace_id=args.workspace_id, mission_id=args.mission_id,
                generation_id=args.generation_id,
                config_source=Path(args.config_source),
                credential_source=(Path(args.credential_source) if args.credential_source else None),
                mcp_command=args.mcp_command, mcp_proxy=args.mcp_proxy,
            )
        else:
            path = retire_surface(
                Path(args.root), hook_dir=Path(args.hook_dir),
                surface_id=args.surface_id, workspace_id=args.workspace_id,
                mission_id=args.mission_id, generation_id=args.generation_id,
            )
        print(path)
        return 0
    except (KimiStateError, fleet_safe_paths.SafePathError, OSError) as exc:
        print(f"fleet-kimi-state: {exc}", file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
