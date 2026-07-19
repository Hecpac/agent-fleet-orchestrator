#!/usr/bin/env python3
"""Descriptor-anchored manifest snapshots, validation, and CAS updates."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
from typing import Iterator
import uuid

import fleet_json
import fleet_manifest
import fleet_mission
import fleet_safe_paths


FEATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SNAPSHOT_TOKEN = re.compile(r"^[0-9a-f]{32}$")
ARCHIVE_TIMESTAMP = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_MISSION_FILE_BYTES = 32 * 1024 * 1024
DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class GuardError(RuntimeError):
    """A manifest or one of its frozen bindings violated the teardown contract."""


class ActiveManifestMissing(GuardError):
    """The exact active manifest does not exist."""


def _feature(value: str) -> str:
    if not FEATURE.fullmatch(value):
        raise GuardError("invalid feature")
    return value


def _active_leaf(feature: str) -> str:
    return f"fleet-{_feature(feature)}.manifest"


def _snapshot_leaf(feature: str, token: str | None = None) -> str:
    feature = _feature(feature)
    if token is None:
        token = uuid.uuid4().hex
    if not SNAPSHOT_TOKEN.fullmatch(token):
        raise GuardError("invalid manifest snapshot token")
    return f".fleet-{feature}.teardown.{token}.snapshot"


def _validate_snapshot_leaf(feature: str, leaf: str) -> str:
    prefix = f".fleet-{_feature(feature)}.teardown."
    suffix = ".snapshot"
    if not leaf.startswith(prefix) or not leaf.endswith(suffix):
        raise GuardError("manifest snapshot name is not feature-bound")
    token = leaf[len(prefix) : -len(suffix)]
    return _snapshot_leaf(feature, token)


@contextmanager
def _manifest_lock(root_fd: int, feature: str, *, exclusive: bool) -> Iterator[None]:
    leaf = f".fleet-{_feature(feature)}.manifest.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(leaf, flags, 0o600, dir_fd=root_fd)
    except OSError as exc:
        raise GuardError("cannot open manifest CAS lock") from exc
    try:
        before = os.fstat(fd)
        _validate_regular(before, leaf)
        current = os.stat(leaf, dir_fd=root_fd, follow_symlinks=False)
        if _fingerprint(current) != _fingerprint(before):
            raise GuardError("manifest CAS lock pathname drift")
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            after = os.fstat(fd)
            current = os.stat(leaf, dir_fd=root_fd, follow_symlinks=False)
            if _fingerprint(after) != _fingerprint(current):
                raise GuardError("manifest CAS lock changed while held")
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _directory_info(info: os.stat_result, where: str, *, mode: int | None) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise GuardError(f"not a directory: {where}")
    if info.st_uid != os.geteuid():
        raise GuardError(f"directory has unexpected owner: {where}")
    actual = stat.S_IMODE(info.st_mode)
    if mode is None:
        if actual & 0o022:
            raise GuardError(f"directory is group/world writable: {where}")
    elif actual != mode:
        raise GuardError(f"directory mode mismatch: {where}: {oct(actual)}")


@contextmanager
def _runs_root(raw: str) -> Iterator[tuple[Path, int]]:
    try:
        root = Path(raw).expanduser().resolve(strict=True)
        before = root.lstat()
        _directory_info(before, str(root), mode=None)
        fd = os.open(root, DIRECTORY_FLAGS)
    except (OSError, RuntimeError) as exc:
        raise GuardError(f"cannot anchor runs directory: {raw}") from exc
    try:
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise GuardError("runs directory changed while being opened")
        _directory_info(after, str(root), mode=None)
        yield root, fd
        current = root.lstat()
        if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            raise GuardError("runs directory pathname binding changed")
        _directory_info(current, str(root), mode=None)
    finally:
        os.close(fd)


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        stat.S_IMODE(info.st_mode),
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _validate_regular(info: os.stat_result, where: str, *, mode: int = 0o600) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise GuardError(f"not a regular file: {where}")
    if info.st_uid != os.geteuid():
        raise GuardError(f"file has unexpected owner: {where}")
    if stat.S_IMODE(info.st_mode) != mode:
        raise GuardError(f"file mode mismatch: {where}")
    if info.st_nlink != 1:
        raise GuardError(f"hard-linked control file is forbidden: {where}")


def _read_regular(
    parent_fd: int,
    leaf: str,
    *,
    max_bytes: int,
    missing_is_active: bool = False,
) -> tuple[bytes, tuple[int, ...]]:
    if not leaf or "/" in leaf or leaf in {".", ".."}:
        raise GuardError("unsafe rooted filename")
    try:
        fd = os.open(leaf, READ_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        if missing_is_active:
            raise ActiveManifestMissing("active manifest is absent") from exc
        raise GuardError(f"rooted file is absent: {leaf}") from exc
    except OSError as exc:
        raise GuardError(f"cannot open rooted file without following links: {leaf}") from exc
    try:
        before = os.fstat(fd)
        _validate_regular(before, leaf)
        if before.st_size > max_bytes:
            raise GuardError(f"rooted file exceeds size limit: {leaf}")
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise GuardError(f"rooted file changed during read: {leaf}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise GuardError(f"rooted file grew during read: {leaf}")
        after = os.fstat(fd)
        current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        expected = _fingerprint(before)
        if _fingerprint(after) != expected or _fingerprint(current) != expected:
            raise GuardError(f"rooted file changed or was replaced during read: {leaf}")
        return b"".join(chunks), expected
    finally:
        os.close(fd)


def _parse_manifest(content: bytes) -> tuple[dict[str, str], list[str]]:
    try:
        rows = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise GuardError("manifest is not valid UTF-8") from exc
    values: dict[str, str] = {}
    for line_number, row in enumerate(rows, 1):
        if not row or row.startswith("#"):
            continue
        if "=" not in row:
            raise GuardError(f"malformed manifest row {line_number}")
        key, value = row.split("=", 1)
        if not fleet_manifest.SAFE_KEY.fullmatch(key):
            raise GuardError(f"invalid manifest key at row {line_number}")
        if key in values:
            raise GuardError(f"duplicate manifest key: {key}")
        try:
            fleet_manifest._validate_value(key, value)
        except fleet_manifest.ManifestError as exc:
            raise GuardError(str(exc)) from exc
        values[key] = value
    if not values:
        raise GuardError("manifest is empty")
    try:
        return fleet_manifest.normalize(values), rows
    except fleet_manifest.ManifestError as exc:
        raise GuardError(str(exc)) from exc


def _open_directory(parent_fd: int, leaf: str, *, mode: int) -> int:
    if not leaf or "/" in leaf or leaf in {".", ".."}:
        raise GuardError("unsafe rooted directory name")
    try:
        fd = os.open(leaf, DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise GuardError(f"cannot open rooted directory: {leaf}") from exc
    try:
        _directory_info(os.fstat(fd), leaf, mode=mode)
    except Exception:
        os.close(fd)
        raise
    return fd


def _mission_bytes(root_fd: int, mission_id: str, leaf: str) -> bytes:
    missions_fd = _open_directory(root_fd, "missions", mode=0o700)
    try:
        mission_fd = _open_directory(missions_fd, mission_id, mode=0o700)
        try:
            content, _ = _read_regular(
                mission_fd,
                leaf,
                max_bytes=MAX_MISSION_FILE_BYTES,
            )
        finally:
            os.close(mission_fd)
    finally:
        os.close(missions_fd)
    return content


def _decode_canonical_json(content: bytes, where: str) -> object:
    try:
        value = fleet_json.loads(content)
        canonical = fleet_json.canonical_bytes(value) + b"\n"
    except fleet_json.FleetJSONError as exc:
        detail = str(exc).replace("duplicate JSON object key", "duplicate key")
        raise GuardError(
            f"control file is not canonical JSON: {where}: {detail}"
        ) from exc
    if content != canonical:
        raise GuardError(f"control file bytes are not canonical JSON: {where}")
    return value


def _mission_json(root_fd: int, mission_id: str, leaf: str) -> object:
    return _decode_canonical_json(
        _mission_bytes(root_fd, mission_id, leaf), leaf
    )


def _validate_repository_binding(
    values: dict[str, str],
    creation: object,
    compiled: dict[str, object],
    root_fd: int,
) -> None:
    if not isinstance(creation, dict):
        raise GuardError("mission creation request is malformed")
    required = {"schema_version", "mission_id", "idempotency_key", "request", "runtime_options"}
    if set(creation) != required or creation.get("schema_version") != 1:
        raise GuardError("mission creation request fields are invalid")
    request = creation.get("request")
    request_fields = {
        "feature",
        "objective_sha256",
        "target_repo",
        "base_sha",
        "workflow_digest",
        "initial_risk",
    }
    if not isinstance(request, dict) or set(request) != request_fields:
        raise GuardError("mission creation repository binding is malformed")
    for field in ("feature", "target_repo", "base_sha"):
        if request.get(field) != values.get(field):
            raise GuardError(f"manifest {field} drifted from mission creation")
    if creation.get("mission_id") != values.get("mission_id"):
        raise GuardError("manifest mission_id drifted from mission creation")
    mission_id = values["mission_id"]
    if request.get("workflow_digest") != compiled.get("workflow_digest"):
        raise GuardError("mission creation workflow digest drift")
    workflow = compiled.get("workflow")
    risk = workflow.get("risk") if isinstance(workflow, dict) else None
    if not isinstance(risk, dict) or request.get("initial_risk") != risk.get("minimum"):
        raise GuardError("mission creation risk binding drift")
    objective = _mission_bytes(root_fd, mission_id, "objective.txt")
    if request.get("objective_sha256") != hashlib.sha256(objective).hexdigest():
        raise GuardError("mission objective digest drift")
    runtime_options = _mission_json(root_fd, mission_id, "runtime-options.json")
    if creation.get("runtime_options") != runtime_options:
        raise GuardError("mission runtime options drift")
    idempotency_key = creation.get("idempotency_key")
    if not isinstance(idempotency_key, str):
        raise GuardError("mission idempotency key is invalid")
    expected_mission_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"fleet-mission:{idempotency_key}")
    )
    if expected_mission_id != mission_id:
        raise GuardError("mission idempotency binding drift")


def _validate_writer_bindings(values: dict[str, str], feature: str) -> None:
    git_fields = {
        "worktree",
        "branch",
        "base_sha",
        "final_sha",
        "git_isolation",
        "publication_state",
        "published_sha",
    }
    authorities = {
        key.rsplit(".", 1)[0]: value
        for key, value in values.items()
        if key.endswith(".authority")
    }
    metadata_ids = {
        key.rsplit(".", 1)[0]
        for key in values
        if "." in key and key.rsplit(".", 1)[1] in git_fields
    }
    target_repo = values.get("target_repo")
    if target_repo is None:
        if metadata_ids:
            raise GuardError("writer Git metadata exists without a target repository")
        return
    writer_ids = {
        instance_id
        for instance_id, authority in authorities.items()
        if authority == "write"
    }
    if metadata_ids != writer_ids:
        raise GuardError("writer Git metadata does not match the write-authority roster")
    base_sha = values["base_sha"]
    for instance_id in sorted(writer_ids):
        prefix = f"{instance_id}."
        required = {
            "worktree", "branch", "base_sha", "final_sha",
            "git_isolation", "publication_state",
        }
        if any(prefix + field not in values for field in required):
            raise GuardError(f"writer Git tuple is incomplete: {instance_id}")
        worktree = Path(values[prefix + "worktree"])
        expected_name = f"{feature}-{instance_id}"
        if not worktree.is_absolute() or worktree.name != expected_name:
            raise GuardError(f"writer worktree binding is invalid: {instance_id}")
        if values[prefix + "branch"] != f"fleet/{feature}/{instance_id}":
            raise GuardError(f"writer branch binding is invalid: {instance_id}")
        if values[prefix + "base_sha"] != base_sha:
            raise GuardError(f"writer base_sha drift: {instance_id}")
        final_sha = values[prefix + "final_sha"]
        if not GIT_SHA.fullmatch(final_sha) or len(final_sha) != len(base_sha):
            raise GuardError(f"writer final_sha is invalid: {instance_id}")
        if values[prefix + "git_isolation"] != "isolated-clone":
            raise GuardError(f"writer Git isolation drift: {instance_id}")
        publication_state = values[prefix + "publication_state"]
        if publication_state not in {"private", "published"}:
            raise GuardError(f"writer publication state is invalid: {instance_id}")
        published_sha = values.get(prefix + "published_sha")
        if published_sha is not None and published_sha != final_sha:
            raise GuardError(f"writer published_sha drift: {instance_id}")
        if publication_state == "published" and published_sha is None:
            raise GuardError(f"published writer lacks published_sha: {instance_id}")


def _validate_reader_bindings(
    values: dict[str, str], feature: str, *, mission_bound: bool
) -> None:
    """Bind every mission specialist without write authority to one snapshot.

    Reader workspace metadata intentionally uses a tuple distinct from the
    writer publication tuple.  In particular, a reader never acquires a
    branch, final SHA, or publication state merely because it has a private
    Git clone.
    """

    fields = {
        "workspace",
        "workspace_kind",
        "workspace_base_sha",
        "workspace_publication",
    }
    authorities = {
        key.rsplit(".", 1)[0]: value
        for key, value in values.items()
        if key.endswith(".authority")
    }
    metadata_ids = {
        key.rsplit(".", 1)[0]
        for key in values
        if "." in key and key.rsplit(".", 1)[1] in fields
    }
    if not mission_bound:
        if metadata_ids:
            raise GuardError("reader workspace metadata exists outside a mission")
        return

    reader_ids = {
        instance_id
        for instance_id, authority in authorities.items()
        if authority not in {"write", "control"}
    }
    if metadata_ids != reader_ids:
        raise GuardError(
            "reader workspace metadata does not match the mission reader roster"
        )
    base_sha = values.get("base_sha", "")
    for instance_id in sorted(reader_ids):
        prefix = f"{instance_id}."
        if any(prefix + field not in values for field in fields):
            raise GuardError(f"reader workspace tuple is incomplete: {instance_id}")
        workspace = Path(values[prefix + "workspace"])
        expected_name = f"{feature}-{instance_id}"
        if not workspace.is_absolute() or workspace.name != expected_name:
            raise GuardError(f"reader workspace binding is invalid: {instance_id}")
        if values[prefix + "workspace_kind"] != "isolated-read-clone":
            raise GuardError(f"reader workspace kind drift: {instance_id}")
        if values[prefix + "workspace_base_sha"] != base_sha:
            raise GuardError(f"reader workspace base_sha drift: {instance_id}")
        if values[prefix + "workspace_publication"] != "none":
            raise GuardError(f"reader workspace publication drift: {instance_id}")


def _validate_manifest(values: dict[str, str], root_fd: int, feature: str) -> None:
    if values.get("feature") != feature:
        raise GuardError("manifest feature does not match its active filename")
    workspace_uuid = values.get("workspace_uuid", "")
    _cmux_uuid(workspace_uuid, "manifest workspace_uuid")

    target_repo = values.get("target_repo")
    base_sha = values.get("base_sha")
    if (target_repo is None) != (base_sha is None):
        raise GuardError("manifest target_repo/base_sha binding is incomplete")
    if target_repo is not None:
        target = Path(target_repo)
        try:
            physical = target.resolve(strict=True)
            info = physical.lstat()
        except (OSError, RuntimeError) as exc:
            raise GuardError("manifest target repository is unavailable") from exc
        if not target.is_absolute() or str(physical) != target_repo:
            raise GuardError("manifest target repository is not a physical absolute path")
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise GuardError("manifest target repository is not a directory")
        if not GIT_SHA.fullmatch(base_sha or ""):
            raise GuardError("manifest base_sha is invalid")
    mission_id = values.get("mission_id")
    _validate_writer_bindings(values, feature)
    _validate_reader_bindings(values, feature, mission_bound=mission_id is not None)
    if mission_id is None:
        return
    try:
        canonical_mission = str(uuid.UUID(mission_id))
    except ValueError as exc:
        raise GuardError("manifest mission_id is invalid") from exc
    if canonical_mission != mission_id:
        raise GuardError("manifest mission_id is not canonical")
    compiled_raw = _mission_json(root_fd, mission_id, "compiled-workflow.json")
    try:
        compiled = fleet_mission.validate_compiled(compiled_raw)
        fleet_manifest.verify_compiled_binding(values, compiled)
    except (fleet_mission.MissionError, fleet_manifest.ManifestError) as exc:
        raise GuardError(f"compiled workflow binding failed: {exc}") from exc
    creation = _mission_json(root_fd, mission_id, "creation-request.json")
    _validate_repository_binding(values, creation, compiled, root_fd)


def _active(
    root_fd: int,
    feature: str,
) -> tuple[bytes, dict[str, str], list[str], tuple[int, ...]]:
    content, fingerprint = _read_regular(
        root_fd,
        _active_leaf(feature),
        max_bytes=MAX_MANIFEST_BYTES,
        missing_is_active=True,
    )
    values, rows = _parse_manifest(content)
    _validate_manifest(values, root_fd, feature)
    return content, values, rows, fingerprint


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _expected_digest(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise GuardError("invalid expected manifest digest")
    return value


def _write_snapshot(root_fd: int, leaf: str, content: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(leaf, flags, 0o600, dir_fd=root_fd)
    except OSError as exc:
        raise GuardError("cannot create immutable manifest snapshot") from exc
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise GuardError("short manifest snapshot write")
            view = view[written:]
        os.fsync(fd)
        _validate_regular(os.fstat(fd), leaf)
    except Exception:
        os.close(fd)
        try:
            os.unlink(leaf, dir_fd=root_fd)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    os.fsync(root_fd)


def command_snapshot(args: argparse.Namespace) -> None:
    with _runs_root(args.runs_dir) as (_, root_fd):
        with _manifest_lock(root_fd, args.feature, exclusive=False):
            content, _, _, _ = _active(root_fd, args.feature)
            leaf = _snapshot_leaf(args.feature)
            _write_snapshot(root_fd, leaf, content)
            print(f"{leaf}\t{_digest(content)}")


def _snapshot(
    root_fd: int,
    feature: str,
    leaf: str,
    expected: str,
) -> tuple[bytes, dict[str, str]]:
    leaf = _validate_snapshot_leaf(feature, leaf)
    content, _ = _read_regular(root_fd, leaf, max_bytes=MAX_MANIFEST_BYTES)
    if _digest(content) != _expected_digest(expected):
        raise GuardError("manifest snapshot digest drift")
    values, _ = _parse_manifest(content)
    if values.get("feature") != feature:
        raise GuardError("manifest snapshot feature drift")
    return content, values


def command_get(args: argparse.Namespace) -> None:
    if not fleet_manifest.SAFE_KEY.fullmatch(args.key):
        raise GuardError("invalid manifest key")
    with _runs_root(args.runs_dir) as (_, root_fd):
        _, values = _snapshot(
            root_fd, args.feature, args.snapshot, args.digest
        )
        print(values.get(args.key, ""))


def command_list_suffix(args: argparse.Namespace) -> None:
    if not args.suffix or any(character in args.suffix for character in "\0\r\n="):
        raise GuardError("invalid manifest key suffix")
    with _runs_root(args.runs_dir) as (_, root_fd):
        _, values = _snapshot(
            root_fd, args.feature, args.snapshot, args.digest
        )
        for key, value in values.items():
            if key.endswith(args.suffix):
                print(f"{key}={value}")


def command_assert_active(args: argparse.Namespace) -> None:
    with _runs_root(args.runs_dir) as (_, root_fd):
        with _manifest_lock(root_fd, args.feature, exclusive=False):
            content, _, _, _ = _active(root_fd, args.feature)
            if _digest(content) != _expected_digest(args.digest):
                raise GuardError("active manifest changed outside teardown CAS")


def _updated_content(rows: list[str], key: str, value: str) -> bytes:
    found = False
    updated: list[str] = []
    for row in rows:
        if row.startswith(f"{key}="):
            if found:
                raise GuardError(f"duplicate manifest key: {key}")
            updated.append(f"{key}={value}")
            found = True
        else:
            updated.append(row)
    if not found:
        updated.append(f"{key}={value}")
    return ("\n".join(updated) + "\n").encode("utf-8")


def _set_active_locked(
    args: argparse.Namespace,
    root_fd: int,
    expected_digest: str,
) -> str:
    content, _, rows, fingerprint = _active(root_fd, args.feature)
    if _digest(content) != expected_digest:
        raise GuardError("active manifest changed outside teardown CAS")
    updated = _updated_content(rows, args.key, args.value)
    updated_values, _ = _parse_manifest(updated)
    _validate_manifest(updated_values, root_fd, args.feature)
    active_leaf = _active_leaf(args.feature)
    temporary = f".{active_leaf}.{uuid.uuid4().hex}.cas"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(temporary, flags, 0o600, dir_fd=root_fd)
    published = False
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(updated)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise GuardError("short manifest CAS write")
            view = view[written:]
        os.fsync(fd)
        _validate_regular(os.fstat(fd), temporary)
        current = os.stat(active_leaf, dir_fd=root_fd, follow_symlinks=False)
        if _fingerprint(current) != fingerprint:
            raise GuardError("active manifest changed before CAS replacement")
        os.replace(
            temporary,
            active_leaf,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        published = True
        os.fsync(root_fd)
    finally:
        os.close(fd)
        if not published:
            try:
                os.unlink(temporary, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileNotFoundError:
                pass
    return _digest(updated)


def command_set_active(args: argparse.Namespace) -> None:
    if not fleet_manifest.SAFE_KEY.fullmatch(args.key):
        raise GuardError("invalid manifest key")
    try:
        fleet_manifest._validate_value(args.key, args.value)
    except fleet_manifest.ManifestError as exc:
        raise GuardError(str(exc)) from exc
    expected_digest = _expected_digest(args.digest)
    with _runs_root(args.runs_dir) as (_, root_fd):
        with _manifest_lock(root_fd, args.feature, exclusive=True):
            new_digest = _set_active_locked(args, root_fd, expected_digest)
    print(new_digest)


def command_clear_snapshot(args: argparse.Namespace) -> None:
    with _runs_root(args.runs_dir) as (_, root_fd):
        leaf = _validate_snapshot_leaf(args.feature, args.snapshot)
        content, fingerprint = _read_regular(
            root_fd, leaf, max_bytes=MAX_MANIFEST_BYTES
        )
        if _digest(content) != _expected_digest(args.digest):
            raise GuardError("refusing to clear drifted manifest snapshot")
        current = os.stat(leaf, dir_fd=root_fd, follow_symlinks=False)
        if _fingerprint(current) != fingerprint:
            raise GuardError("manifest snapshot changed before cleanup")
        os.unlink(leaf, dir_fd=root_fd)
        os.fsync(root_fd)


def _cmux_uuid(value: str, where: str) -> str:
    """Accept the two stable case forms emitted by CMUX and reject mixtures."""

    try:
        normalized = str(uuid.UUID(value))
        if value not in {normalized, normalized.upper()}:
            raise ValueError
    except ValueError as exc:
        raise GuardError(f"{where} is invalid") from exc
    return value


def _workspace_uuid(value: str) -> str:
    return _cmux_uuid(value, "archive workspace_uuid")


def _archive_dir(feature: str, workspace_uuid: str, digest: str) -> str:
    return (
        f"{_feature(feature)}-{_workspace_uuid(workspace_uuid)}-"
        f"{_expected_digest(digest)}"
    )


def _validate_archive_dir(
    feature: str,
    archive_dir: str,
    *,
    workspace_uuid: str | None = None,
    digest: str | None = None,
) -> str:
    prefix = f"{_feature(feature)}-"
    if not archive_dir.startswith(prefix):
        raise GuardError("archive directory is not feature-bound")
    suffix = archive_dir[len(prefix) :]
    if ARCHIVE_TIMESTAMP.fullmatch(suffix):
        # Historical archives used a timestamp-only suffix.  They remain
        # readable, while every newly prepared archive uses the exact
        # workspace-and-manifest binding below.
        return archive_dir
    if len(suffix) <= 37 or suffix[36] != "-":
        raise GuardError("archive directory binding is invalid")
    bound_workspace_uuid = _workspace_uuid(suffix[:36])
    bound_digest = _expected_digest(suffix[37:])
    if workspace_uuid is not None and bound_workspace_uuid != _workspace_uuid(
        workspace_uuid
    ):
        raise GuardError("archive directory workspace binding drift")
    if digest is not None and bound_digest != _expected_digest(digest):
        raise GuardError("archive directory manifest binding drift")
    return archive_dir


def _archived_manifest(
    root_fd: int,
    feature: str,
    archive_dir: str,
    digest: str,
) -> tuple[bytes, dict[str, str]]:
    archive_dir = _validate_archive_dir(feature, archive_dir, digest=digest)
    archive_root_fd = _open_directory(root_fd, "archive", mode=0o700)
    try:
        archive_fd = _open_directory(archive_root_fd, archive_dir, mode=0o700)
        try:
            content, _ = _read_regular(
                archive_fd, "manifest", max_bytes=MAX_MANIFEST_BYTES
            )
        finally:
            os.close(archive_fd)
    finally:
        os.close(archive_root_fd)
    if _digest(content) != _expected_digest(digest):
        raise GuardError("archived manifest digest drift")
    values, _ = _parse_manifest(content)
    _validate_manifest(values, root_fd, feature)
    _validate_archive_dir(
        feature,
        archive_dir,
        workspace_uuid=values["workspace_uuid"],
        digest=digest,
    )
    return content, values


def command_get_archived(args: argparse.Namespace) -> None:
    if not fleet_manifest.SAFE_KEY.fullmatch(args.key):
        raise GuardError("invalid manifest key")
    with _runs_root(args.runs_dir) as (_, root_fd):
        _, values = _archived_manifest(
            root_fd, args.feature, args.archive_dir, args.digest
        )
        print(values.get(args.key, ""))


def command_list_archived_suffix(args: argparse.Namespace) -> None:
    if not args.suffix or any(character in args.suffix for character in "\0\r\n="):
        raise GuardError("invalid manifest key suffix")
    with _runs_root(args.runs_dir) as (_, root_fd):
        _, values = _archived_manifest(
            root_fd, args.feature, args.archive_dir, args.digest
        )
        for key, value in values.items():
            if key.endswith(args.suffix):
                print(f"{key}={value}")


def _ensure_archive_root(root_fd: int) -> int:
    try:
        return _open_directory(root_fd, "archive", mode=0o700)
    except GuardError:
        try:
            os.mkdir("archive", 0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
        return _open_directory(root_fd, "archive", mode=0o700)


def _intent_leaf(feature: str, workspace_uuid: str) -> str:
    workspace_uuid = _workspace_uuid(workspace_uuid)
    return f".fleet-{_feature(feature)}.{workspace_uuid}.archive-intent.json"


def _archive_intent(
    root_fd: int,
    leaf: str,
    *,
    feature: str,
) -> dict[str, object]:
    content, _ = _read_regular(root_fd, leaf, max_bytes=64 * 1024)
    value = _decode_canonical_json(content, leaf)
    required = {
        "schema_version", "feature", "workspace_uuid",
        "archive_dir", "manifest_digest",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise GuardError("archive intent fields are invalid")
    if value.get("schema_version") != 1 or value.get("feature") != feature:
        raise GuardError("archive intent binding is invalid")
    if _intent_leaf(feature, str(value.get("workspace_uuid", ""))) != leaf:
        raise GuardError("archive intent filename binding drift")
    manifest_digest = _expected_digest(str(value.get("manifest_digest", "")))
    _validate_archive_dir(
        feature,
        str(value.get("archive_dir", "")),
        workspace_uuid=str(value.get("workspace_uuid", "")),
        digest=manifest_digest,
    )
    return value


def _control_json_checkpoint(name: str) -> None:
    if os.environ.get("FLEET_TEST_MANIFEST_GUARD_CRASH_AT") == name:
        os.kill(os.getpid(), signal.SIGKILL)


def _canonical_control_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise GuardError("control JSON is not canonically serializable") from exc


def _create_control_json(root_fd: int, leaf: str, value: object) -> None:
    """Install one immutable control file without ever creating a hardlink.

    The deterministic pending name lets an identical retry recover a process
    crash before publication.  A different pending or published value remains
    a durable conflict; it is never overwritten or silently discarded.
    """

    if not leaf or "/" in leaf or leaf in {".", ".."}:
        raise GuardError("unsafe control JSON filename")
    content = _canonical_control_json(value)
    if len(content) > 64 * 1024:
        raise GuardError("control JSON exceeds size limit")
    temporary_prefix = fleet_safe_paths._atomic_pending_prefix(leaf)
    temporary = fleet_safe_paths._atomic_pending_name(leaf, content)
    temporary_owned = False
    locked = False
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX)
        locked = True
        try:
            existing_names = os.listdir(root_fd)
        except OSError as exc:
            raise GuardError("cannot enumerate pending control JSON writes") from exc
        conflicting = sorted(
            name
            for name in existing_names
            if name.startswith(temporary_prefix)
            and name.endswith(".tmp")
            and name != temporary
        )
        for name in conflicting:
            _read_regular(root_fd, name, max_bytes=64 * 1024)
        if conflicting:
            raise GuardError("pending control JSON conflicts with requested bytes")

        try:
            os.stat(leaf, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise GuardError("cannot inspect control JSON destination") from exc
        else:
            existing, _ = _read_regular(root_fd, leaf, max_bytes=64 * 1024)

        try:
            os.stat(temporary, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pending = None
        except OSError as exc:
            raise GuardError("cannot inspect pending control JSON") from exc
        else:
            pending, _ = _read_regular(root_fd, temporary, max_bytes=64 * 1024)
            if pending != content:
                # The directory lock proves that no cooperating writer still
                # owns this deterministic inode.  Its content-addressed name
                # therefore identifies an interrupted write of this request.
                os.unlink(temporary, dir_fd=root_fd)
                os.fsync(root_fd)
                pending = None

        if existing is not None:
            if existing != content:
                raise GuardError("control JSON conflicts with requested bytes")
            if pending is not None:
                os.unlink(temporary, dir_fd=root_fd)
                os.fsync(root_fd)
            return

        if pending is None:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                fd = os.open(temporary, flags, 0o600, dir_fd=root_fd)
            except OSError as exc:
                raise GuardError("cannot create pending control JSON") from exc
            temporary_owned = True
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(content)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise GuardError("short control JSON write")
                    view = view[written:]
                os.fsync(fd)
                _validate_regular(os.fstat(fd), temporary)
            finally:
                os.close(fd)
        else:
            temporary_owned = True
        os.fsync(root_fd)
        _control_json_checkpoint(
            "after_archive_intent_pending_fsync_before_install"
        )

        try:
            fleet_safe_paths._rename_noreplace(
                temporary,
                leaf,
                source_fd=root_fd,
                destination_fd=root_fd,
            )
            temporary_owned = False
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise GuardError("cannot publish control JSON exclusively") from exc
            installed, _ = _read_regular(root_fd, leaf, max_bytes=64 * 1024)
            if installed != content:
                raise GuardError("control JSON conflicts with requested bytes")
        os.fsync(root_fd)
        _control_json_checkpoint("after_archive_intent_install_before_cleanup")
        installed, _ = _read_regular(root_fd, leaf, max_bytes=64 * 1024)
        if installed != content:
            raise GuardError("published control JSON bytes drifted")
    finally:
        if temporary_owned:
            try:
                os.unlink(temporary, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileNotFoundError:
                pass
        if locked:
            fcntl.flock(root_fd, fcntl.LOCK_UN)


def command_prepare_archive(args: argparse.Namespace) -> None:
    with _runs_root(args.runs_dir) as (_, root_fd):
        content, values, _, _ = _active(root_fd, args.feature)
        digest = _digest(content)
        if digest != _expected_digest(args.digest):
            raise GuardError("active manifest changed before archive intent")
        workspace_uuid = values["workspace_uuid"]
        intent_leaf = _intent_leaf(args.feature, workspace_uuid)
        try:
            intent = _archive_intent(root_fd, intent_leaf, feature=args.feature)
            if intent.get("manifest_digest") != digest:
                raise GuardError("archive intent manifest digest drift")
            archive_dir = str(intent["archive_dir"])
            existing = True
        except GuardError:
            try:
                os.stat(intent_leaf, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = False
            else:
                raise
            archive_dir = _archive_dir(args.feature, workspace_uuid, digest)
            intent = {
                "schema_version": 1,
                "feature": args.feature,
                "workspace_uuid": workspace_uuid,
                "archive_dir": archive_dir,
                "manifest_digest": digest,
            }
        archive_root_fd = _ensure_archive_root(root_fd)
        try:
            if existing:
                archive_fd = _open_directory(
                    archive_root_fd, archive_dir, mode=0o700
                )
                os.close(archive_fd)
            else:
                try:
                    os.mkdir(archive_dir, 0o700, dir_fd=archive_root_fd)
                    os.fsync(archive_root_fd)
                except FileExistsError:
                    archive_fd = _open_directory(
                        archive_root_fd, archive_dir, mode=0o700
                    )
                    try:
                        if os.listdir(archive_fd):
                            raise GuardError(
                                "uncommitted archive destination is not empty"
                            )
                    finally:
                        os.close(archive_fd)
                archive_fd = _open_directory(
                    archive_root_fd, archive_dir, mode=0o700
                )
                os.close(archive_fd)
        finally:
            os.close(archive_root_fd)
        if not existing:
            _create_control_json(root_fd, intent_leaf, intent)
        print(f"{archive_dir}\t{intent_leaf}")


def _find_recovery_intent(root_fd: int, feature: str) -> tuple[str, dict[str, object]]:
    prefix = f".fleet-{_feature(feature)}."
    suffix = ".archive-intent.json"
    candidates = sorted(
        name
        for name in os.listdir(root_fd)
        if name.startswith(prefix) and name.endswith(suffix)
    )
    if not candidates:
        raise ActiveManifestMissing("active manifest and archive intent are absent")
    if len(candidates) != 1:
        raise GuardError("multiple archive recovery intents exist")
    leaf = candidates[0]
    return leaf, _archive_intent(root_fd, leaf, feature=feature)


def command_recover_archived(args: argparse.Namespace) -> None:
    with _runs_root(args.runs_dir) as (_, root_fd):
        leaf, intent = _find_recovery_intent(root_fd, args.feature)
        _, values = _archived_manifest(
            root_fd,
            args.feature,
            str(intent["archive_dir"]),
            str(intent["manifest_digest"]),
        )
        if values.get("workspace_uuid") != intent.get("workspace_uuid"):
            raise GuardError("archive recovery workspace binding drift")
        print(
            f"{leaf}\t{intent['archive_dir']}\t{intent['manifest_digest']}\t"
            f"{intent['workspace_uuid']}"
        )


def command_clear_archive(args: argparse.Namespace) -> None:
    with _runs_root(args.runs_dir) as (_, root_fd):
        intent = _archive_intent(root_fd, args.intent, feature=args.feature)
        if intent.get("archive_dir") != args.archive_dir \
                or intent.get("manifest_digest") != _expected_digest(args.digest):
            raise GuardError("archive completion intent drift")
        _archived_manifest(root_fd, args.feature, args.archive_dir, args.digest)
        os.unlink(args.intent, dir_fd=root_fd)
        if args.clear_snapshots:
            prefix = f".fleet-{args.feature}.teardown."
            for name in os.listdir(root_fd):
                if not name.startswith(prefix) or not name.endswith(".snapshot"):
                    continue
                _validate_snapshot_leaf(args.feature, name)
                _read_regular(root_fd, name, max_bytes=MAX_MANIFEST_BYTES)
                os.unlink(name, dir_fd=root_fd)
        os.fsync(root_fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    canonical = subparsers.add_parser("canonical-root")
    canonical.add_argument("--runs-dir", required=True)

    for name, handler in (
        ("snapshot", command_snapshot),
        ("get", command_get),
        ("list-suffix", command_list_suffix),
        ("assert-active", command_assert_active),
        ("set-active", command_set_active),
        ("clear-snapshot", command_clear_snapshot),
        ("get-archived", command_get_archived),
        ("list-archived-suffix", command_list_archived_suffix),
        ("prepare-archive", command_prepare_archive),
        ("recover-archived", command_recover_archived),
        ("clear-archive", command_clear_archive),
    ):
        command = subparsers.add_parser(name)
        command.add_argument("--runs-dir", required=True)
        command.add_argument("--feature", required=True)
        command.set_defaults(handler=handler)
        if name in {"get", "list-suffix", "clear-snapshot"}:
            command.add_argument("--snapshot", required=True)
            command.add_argument("--digest", required=True)
        elif name in {"assert-active", "set-active"}:
            command.add_argument("--digest", required=True)
        elif name in {"get-archived", "list-archived-suffix"}:
            command.add_argument("--archive-dir", required=True)
            command.add_argument("--digest", required=True)
        elif name == "prepare-archive":
            command.add_argument("--digest", required=True)
        elif name == "clear-archive":
            command.add_argument("--intent", required=True)
            command.add_argument("--archive-dir", required=True)
            command.add_argument("--digest", required=True)
            command.add_argument("--clear-snapshots", action="store_true")
        if name == "get":
            command.add_argument("--key", required=True)
        elif name == "list-suffix":
            command.add_argument("--suffix", required=True)
        elif name == "set-active":
            command.add_argument("--key", required=True)
            command.add_argument("--value", required=True)
        elif name == "get-archived":
            command.add_argument("--key", required=True)
        elif name == "list-archived-suffix":
            command.add_argument("--suffix", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "canonical-root":
            with _runs_root(args.runs_dir) as (root, _):
                print(root)
        else:
            args.handler(args)
        return 0
    except ActiveManifestMissing as exc:
        print(f"manifest guard: {exc}", file=sys.stderr)
        return 1
    except (GuardError, OSError) as exc:
        print(f"manifest guard: {exc}", file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
