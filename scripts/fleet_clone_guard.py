#!/usr/bin/env python3
"""Descriptor-safe staging, retirement, and publication intents for Fleet clones."""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
from pathlib import Path
import re
import signal
import stat
import sys
from typing import Any

import fleet_json
from fleet_safe_paths import RootedFS, SafePathError


_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_UUID_RE = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_MAX_INTENT_BYTES = 16 * 1024


class CloneGuardError(RuntimeError):
    """One exact clone lifecycle binding could not be proven."""


class IntentAbsent(CloneGuardError):
    """The requested immutable intent does not exist."""


class BootLockBusy(CloneGuardError):
    """Another live fleet-up owns the exact feature boot admission lock."""


def _checkpoint(name: str) -> None:
    if os.environ.get("FLEET_TEST_PUBLICATION_CRASH_AT") == name:
        os.kill(os.getpid(), signal.SIGKILL)


def _ownership_transfer_checkpoint(name: str) -> None:
    configured = os.environ.get(
        "FLEET_TEST_BOOT_CRASH_AT",
        os.environ.get("FLEET_TEST_PUBLICATION_CRASH_AT", ""),
    )
    if configured == name:
        os.kill(os.getppid(), signal.SIGKILL)
        os.kill(os.getpid(), signal.SIGKILL)


def _decode_exact(content: bytes) -> dict[str, Any]:
    try:
        value = fleet_json.loads(content)
    except fleet_json.FleetJSONError as exc:
        raise CloneGuardError("invalid intent JSON") from exc
    if not isinstance(value, dict):
        raise CloneGuardError("intent must be a JSON object")
    try:
        canonical = fleet_json.canonical_bytes(value) + b"\n"
    except fleet_json.FleetJSONError as exc:  # pragma: no cover - loads owns this.
        raise CloneGuardError("invalid intent JSON") from exc
    if content != canonical:
        raise CloneGuardError("intent JSON bytes are not canonical")
    return value


def _encode_exact(value: dict[str, Any]) -> bytes:
    try:
        return fleet_json.canonical_bytes(value) + b"\n"
    except fleet_json.FleetJSONError as exc:
        raise CloneGuardError("invalid intent JSON value") from exc


def _validated_name(value: str, where: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise CloneGuardError(f"invalid {where}")
    return value


def _validated_uuid(value: str) -> str:
    if not _UUID_RE.fullmatch(value):
        raise CloneGuardError("invalid workspace UUID")
    return value.upper()


def _validated_oid(value: str) -> str:
    if not _OID_RE.fullmatch(value):
        raise CloneGuardError("invalid Git object id")
    return value


def _validated_kind(value: str) -> str:
    if value not in {"partial", "reader", "writer"}:
        raise CloneGuardError("invalid clone kind")
    return value


def _validated_creation_kind(value: str) -> str:
    if value not in {"reader", "writer"}:
        raise CloneGuardError("invalid clone creation kind")
    return value


def _validated_branch(kind: str, value: str) -> str:
    if kind in {"partial", "reader"}:
        if value not in {"", "-"}:
            raise CloneGuardError("reader clone cannot bind a branch")
        return ""
    if not value or "\0" in value or value.startswith("-"):
        raise CloneGuardError("invalid writer branch")
    return value


def _intent_names(
    feature: str, instance: str, workspace_uuid: str
) -> tuple[str, str, str]:
    prefix = f".fleet-{feature}.{instance}.{workspace_uuid}"
    return (
        f"{prefix}.clone-stage-intent.json",
        f"{prefix}.clone-retirement-tombstone.json",
        f"{prefix}.publication-intent.json",
    )


def _creation_intent_name(feature: str, instance: str, workspace_uuid: str) -> str:
    return f".fleet-{feature}.{instance}.{workspace_uuid}.clone-creation-intent.json"


def _creation_binding_name(feature: str, instance: str, workspace_uuid: str) -> str:
    return f".fleet-{feature}.{instance}.{workspace_uuid}.clone-creation-binding.json"


def _open_exact_root(path: Path) -> tuple[Path, int]:
    try:
        canonical = path.expanduser().resolve(strict=True)
        before = canonical.lstat()
        descriptor = os.open(canonical, _DIR_FLAGS)
    except OSError as exc:
        raise CloneGuardError("isolated specialist root is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise CloneGuardError("isolated specialist root binding changed")
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise CloneGuardError("isolated specialist root must be owner mode 0700")
        return canonical, descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_owned_directory(path: Path, where: str) -> tuple[Path, int]:
    """Pin one operator-selected owner directory without imposing root mode 0700."""

    try:
        canonical = path.expanduser().resolve(strict=True)
        before = canonical.lstat()
        descriptor = os.open(canonical, _DIR_FLAGS)
    except OSError as exc:
        raise CloneGuardError(f"{where} is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise CloneGuardError(f"{where} binding changed")
        _validate_directory(opened, where)
        return canonical, descriptor
    except Exception:
        os.close(descriptor)
        raise


def _stat_optional(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CloneGuardError("cannot inspect isolated clone binding") from exc


def _validate_directory(info: os.stat_result, where: str) -> None:
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise CloneGuardError(f"unsafe owner-bound directory: {where}")


def _open_bound_directory(parent_fd: int, name: str, expected: os.stat_result) -> int:
    _validate_directory(expected, name)
    try:
        descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise CloneGuardError("cannot open isolated clone directory") from exc
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(descriptor)
        raise CloneGuardError("isolated clone directory binding changed")
    _validate_directory(opened, name)
    return descriptor


def _assert_same_device_contents(directory_fd: int, expected_dev: int) -> None:
    """Reject mount-point and other cross-device descendants without following links."""

    opened = os.fstat(directory_fd)
    _validate_directory(opened, "clone device root")
    if opened.st_dev != expected_dev:
        raise CloneGuardError("clone root device binding drift")
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise CloneGuardError("cannot enumerate clone device tree") from exc
    for name in names:
        info = _stat_optional(directory_fd, name)
        if info is None:
            raise CloneGuardError("clone device tree changed during verification")
        if info.st_dev != expected_dev:
            raise CloneGuardError("clone contains a cross-device descendant")
        if stat.S_ISDIR(info.st_mode):
            child_fd = _open_bound_directory(directory_fd, name, info)
            try:
                _assert_same_device_contents(child_fd, expected_dev)
            finally:
                os.close(child_fd)


def assert_same_device_tree(path: Path) -> int:
    try:
        before = path.expanduser().lstat()
    except OSError as exc:
        raise CloneGuardError("clone device root is unavailable") from exc
    _validate_directory(before, str(path))
    try:
        descriptor = os.open(path, _DIR_FLAGS)
    except OSError as exc:
        raise CloneGuardError("cannot open clone device root") from exc
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise CloneGuardError("clone device root binding changed")
        _assert_same_device_contents(descriptor, opened.st_dev)
        return opened.st_dev
    finally:
        os.close(descriptor)


def _open_staging_root(root_fd: int, *, create: bool) -> int:
    name = ".fleet-control-staging"
    info = _stat_optional(root_fd, name)
    if info is None:
        if not create:
            raise CloneGuardError("CONTROL staging root is absent")
        try:
            os.mkdir(name, 0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except OSError as exc:
            raise CloneGuardError("cannot create CONTROL staging root") from exc
        info = _stat_optional(root_fd, name)
        assert info is not None
    _validate_directory(info, name)
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise CloneGuardError("CONTROL staging root must be owner mode 0700")
    return _open_bound_directory(root_fd, name, info)


def ensure_root(worktrees_root: Path, protected: list[Path]) -> Path:
    candidate = worktrees_root.expanduser()
    protected_roots = [path.expanduser().resolve(strict=False) for path in protected]
    unresolved = candidate.resolve(strict=False)
    for item in protected_roots:
        if (
            unresolved == item
            or item in unresolved.parents
            or unresolved in item.parents
        ):
            raise CloneGuardError("fleet worktree root overlaps protected state")
    if candidate.exists() or candidate.is_symlink():
        info = candidate.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise CloneGuardError("fleet worktree root must already be owner mode 0700")
    else:
        try:
            candidate.mkdir(parents=True, mode=0o700)
            os.chmod(candidate, 0o700)
        except OSError as exc:
            raise CloneGuardError("cannot create fleet worktree root") from exc
    canonical, descriptor = _open_exact_root(candidate)
    try:
        for item in protected_roots:
            if (
                canonical == item
                or item in canonical.parents
                or canonical in item.parents
            ):
                raise CloneGuardError("fleet worktree root overlaps protected state")
    finally:
        os.close(descriptor)
    return canonical


def hold_boot_lock(runs_dir: Path, feature_value: str) -> None:
    """Hold one descriptor-safe per-feature lock until the parent pipe closes."""

    feature = _validated_name(feature_value, "feature")
    with RootedFS(runs_dir, root_mode=None) as rooted:
        lock_dir_fd = rooted._open_directory_chain(  # noqa: SLF001
            (".fleet-boot-locks",),
            (0o700,),
            create=True,
        )
        try:
            leaf = f"{feature}.lock"
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                descriptor = os.open(leaf, flags, 0o600, dir_fd=lock_dir_fd)
            except OSError as exc:
                raise CloneGuardError("cannot open fleet boot admission lock") from exc
            try:
                opened = os.fstat(descriptor)
                current = os.stat(leaf, dir_fd=lock_dir_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.geteuid()
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino)
                    != (current.st_dev, current.st_ino)
                    or current.st_nlink != 1
                ):
                    raise CloneGuardError("unsafe fleet boot admission lock")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN}:
                        raise BootLockBusy(
                            f"another fleet-up owns boot admission for '{feature}'"
                        ) from exc
                    raise CloneGuardError(
                        "cannot acquire fleet boot admission lock"
                    ) from exc
                rebound = os.stat(leaf, dir_fd=lock_dir_fd, follow_symlinks=False)
                if (rebound.st_dev, rebound.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ) or rebound.st_nlink != 1:
                    raise CloneGuardError("fleet boot admission lock binding changed")
                rooted.assert_root_binding()
                print("READY", flush=True)
                sys.stdin.buffer.read()
            finally:
                os.close(descriptor)
        finally:
            os.close(lock_dir_fd)


def _read_intent_optional(rooted: RootedFS, name: str) -> dict[str, Any] | None:
    content = rooted.read_regular_optional(
        name,
        directory_modes=(),
        file_mode=0o600,
        max_bytes=_MAX_INTENT_BYTES,
        require_single_link=True,
    )
    return None if content is None else _decode_exact(content)


def _require_static(
    actual: dict[str, Any], expected: dict[str, Any], dynamic: set[str]
) -> None:
    if set(actual) != set(expected) | dynamic:
        raise CloneGuardError("intent field set drift")
    for key, value in expected.items():
        if actual.get(key) != value:
            raise CloneGuardError("intent binding drift")
    for key in dynamic:
        value = actual.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CloneGuardError("invalid intent inode binding")


def _creation_static(
    args: argparse.Namespace,
    canonical_root: Path,
    root_info: os.stat_result,
    canonical_target: Path,
    target_info: os.stat_result,
) -> dict[str, Any]:
    kind = _validated_creation_kind(args.kind)
    feature = _validated_name(args.feature, "feature")
    instance = _validated_name(args.instance, "instance")
    workspace_uuid = _validated_uuid(args.workspace_uuid)
    expected_sha = _validated_oid(args.expected_sha)
    branch = _validated_branch(kind, args.branch)
    return {
        "schema_version": 1,
        "intent_type": "clone_creation_plan",
        "feature": feature,
        "instance": instance,
        "workspace_uuid": workspace_uuid,
        "clone_kind": kind,
        "expected_sha": expected_sha,
        "branch": branch,
        "worktrees_root": str(canonical_root),
        "worktrees_root_dev": root_info.st_dev,
        "worktrees_root_ino": root_info.st_ino,
        "target_repo": str(canonical_target),
        "target_repo_dev": target_info.st_dev,
        "target_repo_ino": target_info.st_ino,
        "source_name": f"{feature}-{instance}",
        "staging_name": f"{feature}--{instance}--{workspace_uuid}",
    }


def _staging_info_optional(root_fd: int, staging_name: str) -> os.stat_result | None:
    staging_root = _stat_optional(root_fd, ".fleet-control-staging")
    if staging_root is None:
        return None
    _validate_directory(staging_root, ".fleet-control-staging")
    if stat.S_IMODE(staging_root.st_mode) != 0o700:
        raise CloneGuardError("CONTROL staging root must be owner mode 0700")
    if staging_root.st_dev != os.fstat(root_fd).st_dev:
        raise CloneGuardError("CONTROL staging root device drift")
    staging_fd = _open_bound_directory(
        root_fd,
        ".fleet-control-staging",
        staging_root,
    )
    try:
        return _stat_optional(staging_fd, staging_name)
    finally:
        os.close(staging_fd)


def _load_creation_plan(
    rooted: RootedFS,
    name: str,
    static: dict[str, Any],
) -> dict[str, Any]:
    value = _read_intent_optional(rooted, name)
    if value is None:
        raise IntentAbsent("clone creation plan is absent")
    _require_static(value, static, set())
    return value


def _creation_binding_static(static: dict[str, Any]) -> dict[str, Any]:
    return {**static, "intent_type": "clone_creation_binding"}


def _load_creation_binding(
    rooted: RootedFS,
    name: str,
    static: dict[str, Any],
) -> dict[str, Any]:
    expected = _creation_binding_static(static)
    value = _read_intent_optional(rooted, name)
    if value is None:
        raise IntentAbsent("clone creation binding is absent")
    _require_static(
        value,
        expected,
        {"source_dev", "source_ino", "source_mode"},
    )
    if value["source_mode"] != 0o700:
        raise CloneGuardError("invalid clone creation source mode")
    if value["source_dev"] != static["worktrees_root_dev"]:
        raise CloneGuardError("clone creation source device drift")
    return value


def _unbound_creation_locations(
    root_fd: int,
    static: dict[str, Any],
) -> tuple[os.stat_result | None, os.stat_result | None]:
    source = _stat_optional(root_fd, static["source_name"])
    staged = _staging_info_optional(root_fd, static["staging_name"])
    if source is not None and staged is not None:
        raise CloneGuardError("planned clone exists at source and staging paths")
    return source, staged


def _validate_unbound_creation_source(
    root_fd: int,
    static: dict[str, Any],
    source: os.stat_result,
) -> os.stat_result:
    _validate_directory(source, static["source_name"])
    if stat.S_IMODE(source.st_mode) != 0o700:
        raise CloneGuardError("unbound planned clone source must be owner mode 0700")
    if source.st_dev != static["worktrees_root_dev"]:
        raise CloneGuardError("unbound planned clone source crossed devices")
    source_fd = _open_bound_directory(root_fd, static["source_name"], source)
    try:
        if os.listdir(source_fd):
            raise CloneGuardError("unbound planned clone source is not empty")
        os.fsync(source_fd)
    finally:
        os.close(source_fd)
    return source


def _assert_creation_clone_state(
    root_fd: int,
    value: dict[str, Any],
) -> tuple[str, os.stat_result | None]:
    source = _stat_optional(root_fd, value["source_name"])
    staged = _staging_info_optional(root_fd, value["staging_name"])
    if source is not None and staged is not None:
        raise CloneGuardError("creation-bound clone exists at source and staging paths")
    location = "absent"
    current = source
    if source is not None:
        location = "source"
    elif staged is not None:
        location = "staged"
        current = staged
    if current is None:
        return location, None
    leaf = value["source_name"] if location == "source" else value["staging_name"]
    _validate_directory(current, leaf)
    if (current.st_dev, current.st_ino) != (
        value["source_dev"],
        value["source_ino"],
    ):
        raise CloneGuardError("clone creation inode binding drift")
    if current.st_dev != value["worktrees_root_dev"]:
        raise CloneGuardError("clone creation device binding drift")
    close_parent = False
    parent_fd = root_fd
    if location == "staged":
        staging_root = _stat_optional(root_fd, ".fleet-control-staging")
        assert staging_root is not None
        parent_fd = _open_bound_directory(
            root_fd,
            ".fleet-control-staging",
            staging_root,
        )
        close_parent = True
    try:
        clone_fd = _open_bound_directory(parent_fd, leaf, current)
        try:
            _assert_same_device_contents(clone_fd, value["source_dev"])
        finally:
            os.close(clone_fd)
    finally:
        if close_parent:
            os.close(parent_fd)
    return location, current


def _open_creation_context(
    args: argparse.Namespace,
) -> tuple[Path, int, Path, int, dict[str, Any]]:
    canonical_root, root_fd = _open_exact_root(args.worktrees_root)
    try:
        canonical_target, target_fd = _open_owned_directory(
            args.target_repo,
            "target repository",
        )
    except Exception:
        os.close(root_fd)
        raise
    static = _creation_static(
        args,
        canonical_root,
        os.fstat(root_fd),
        canonical_target,
        os.fstat(target_fd),
    )
    return canonical_root, root_fd, canonical_target, target_fd, static


def creation_intent(args: argparse.Namespace) -> str:
    canonical_root, root_fd, _, target_fd, static = _open_creation_context(args)
    try:
        plan_name = _creation_intent_name(
            static["feature"], static["instance"], static["workspace_uuid"]
        )
        binding_name = _creation_binding_name(
            static["feature"], static["instance"], static["workspace_uuid"]
        )
        with RootedFS(args.runs_dir, root_mode=None) as rooted:
            plan_optional = _read_intent_optional(rooted, plan_name)
            binding_optional = _read_intent_optional(rooted, binding_name)
            if args.mode == "plan":
                if plan_optional is None:
                    source, staged = _unbound_creation_locations(root_fd, static)
                    if (
                        source is not None
                        or staged is not None
                        or binding_optional is not None
                    ):
                        raise CloneGuardError(
                            "clone source exists without exact creation plan"
                        )
                    rooted.atomic_write(
                        plan_name,
                        _encode_exact(static),
                        directory_modes=(),
                        file_mode=0o600,
                    )
                _load_creation_plan(rooted, plan_name, static)
                return str(Path(args.runs_dir).expanduser().resolve() / plan_name)

            if args.mode == "clear":
                binding: dict[str, Any] | None = None
                if binding_optional is not None:
                    binding = _load_creation_binding(rooted, binding_name, static)
                if binding is not None and plan_optional is not None:
                    _load_creation_plan(rooted, plan_name, static)
                    _assert_creation_clone_state(root_fd, binding)
                    rooted.unlink_regular(
                        binding_name, directory_modes=(), file_mode=0o600
                    )
                    _ownership_transfer_checkpoint("after_creation_binding_clear")
                    rooted.unlink_regular(
                        plan_name, directory_modes=(), file_mode=0o600
                    )
                    return str(Path(args.runs_dir).expanduser().resolve() / plan_name)
                if plan_optional is not None:
                    _load_creation_plan(rooted, plan_name, static)
                    source, staged = _unbound_creation_locations(root_fd, static)
                    if source is not None or staged is not None:
                        raise CloneGuardError(
                            "cannot clear unbound creation plan while source exists"
                        )
                    rooted.unlink_regular(
                        plan_name, directory_modes=(), file_mode=0o600
                    )
                elif binding is not None:
                    location, _ = _assert_creation_clone_state(root_fd, binding)
                    if location != "absent":
                        raise CloneGuardError(
                            "cannot clear residual creation binding while clone exists"
                        )
                    rooted.unlink_regular(
                        binding_name, directory_modes=(), file_mode=0o600
                    )
                return str(Path(args.runs_dir).expanduser().resolve() / plan_name)

            _load_creation_plan(rooted, plan_name, static)
            if args.mode == "source":
                if binding_optional is not None:
                    binding = _load_creation_binding(rooted, binding_name, static)
                    location, _ = _assert_creation_clone_state(root_fd, binding)
                    if location != "source":
                        raise CloneGuardError("bound clone is not at its source path")
                    return str(canonical_root / static["source_name"])
                source, staged = _unbound_creation_locations(root_fd, static)
                if staged is not None:
                    raise CloneGuardError(
                        "unbound planned clone reached CONTROL staging"
                    )
                if source is None:
                    try:
                        os.mkdir(static["source_name"], 0o700, dir_fd=root_fd)
                        os.fsync(root_fd)
                    except OSError as exc:
                        raise CloneGuardError(
                            "cannot prepare planned isolated clone source"
                        ) from exc
                    source = _stat_optional(root_fd, static["source_name"])
                    if source is None:
                        raise CloneGuardError("planned clone source disappeared")
                _validate_unbound_creation_source(root_fd, static, source)
                return str(canonical_root / static["source_name"])

            if args.mode == "bind":
                if binding_optional is None:
                    source, staged = _unbound_creation_locations(root_fd, static)
                    if source is None or staged is not None:
                        raise CloneGuardError(
                            "planned clone source is unavailable for binding"
                        )
                    source = _validate_unbound_creation_source(root_fd, static, source)
                    binding = {
                        **_creation_binding_static(static),
                        "source_dev": source.st_dev,
                        "source_ino": source.st_ino,
                        "source_mode": stat.S_IMODE(source.st_mode),
                    }
                    rooted.atomic_write(
                        binding_name,
                        _encode_exact(binding),
                        directory_modes=(),
                        file_mode=0o600,
                    )
                binding = _load_creation_binding(rooted, binding_name, static)
                location, _ = _assert_creation_clone_state(root_fd, binding)
                if location != "source":
                    raise CloneGuardError("new clone source is not creation-bound")
                return str(canonical_root / static["source_name"])

            if args.mode == "require":
                binding = _load_creation_binding(rooted, binding_name, static)
                location, _ = _assert_creation_clone_state(root_fd, binding)
                return location

            raise CloneGuardError("invalid creation intent mode")
    finally:
        os.close(target_fd)
        os.close(root_fd)


def creation_clone_is_empty(args: argparse.Namespace) -> bool:
    _, root_fd, _, target_fd, static = _open_creation_context(args)
    try:
        plan_name = _creation_intent_name(
            static["feature"], static["instance"], static["workspace_uuid"]
        )
        binding_name = _creation_binding_name(
            static["feature"], static["instance"], static["workspace_uuid"]
        )
        with RootedFS(args.runs_dir, root_mode=None) as rooted:
            _load_creation_plan(rooted, plan_name, static)
            binding = _load_creation_binding(rooted, binding_name, static)
            location, current = _assert_creation_clone_state(root_fd, binding)
            if current is None:
                raise CloneGuardError("creation-bound clone is absent")
            parent_fd = root_fd
            close_parent = False
            if location == "staged":
                staging_root = _stat_optional(root_fd, ".fleet-control-staging")
                assert staging_root is not None
                parent_fd = _open_bound_directory(
                    root_fd, ".fleet-control-staging", staging_root
                )
                close_parent = True
            leaf = (
                binding["source_name"]
                if location == "source"
                else binding["staging_name"]
            )
            try:
                clone_fd = _open_bound_directory(parent_fd, leaf, current)
                try:
                    return not os.listdir(clone_fd)
                finally:
                    os.close(clone_fd)
            finally:
                if close_parent:
                    os.close(parent_fd)
    finally:
        os.close(target_fd)
        os.close(root_fd)


def stage_kind_for_creation(args: argparse.Namespace) -> str:
    canonical_root, root_fd, _, target_fd, static = _open_creation_context(args)
    try:
        plan_name = _creation_intent_name(
            static["feature"], static["instance"], static["workspace_uuid"]
        )
        binding_name = _creation_binding_name(
            static["feature"], static["instance"], static["workspace_uuid"]
        )
        stage_name, _, _ = _intent_names(
            static["feature"], static["instance"], static["workspace_uuid"]
        )
        with RootedFS(args.runs_dir, root_mode=None) as rooted:
            _load_creation_plan(rooted, plan_name, static)
            creation = _load_creation_binding(rooted, binding_name, static)
            _assert_creation_clone_state(root_fd, creation)
            value = _read_intent_optional(rooted, stage_name)
            if value is None:
                raise IntentAbsent("clone stage intent is absent")
            candidates = [(static["clone_kind"], static["branch"]), ("partial", "-")]
            for kind, branch in candidates:
                stage_args = argparse.Namespace(
                    **{**vars(args), "kind": kind, "branch": branch}
                )
                expected = _stage_static(stage_args, canonical_root)
                try:
                    _require_static(
                        value,
                        expected,
                        {"clone_dev", "clone_ino", "source_mode"},
                    )
                except CloneGuardError:
                    continue
                if value["source_mode"] > 0o777:
                    raise CloneGuardError("invalid creation-bound stage source mode")
                if (value["clone_dev"], value["clone_ino"]) != (
                    creation["source_dev"],
                    creation["source_ino"],
                ):
                    raise CloneGuardError("creation/stage inode binding drift")
                return kind
            raise CloneGuardError("creation-bound stage intent drift")
    finally:
        os.close(target_fd)
        os.close(root_fd)


def _stage_static(args: argparse.Namespace, canonical_root: Path) -> dict[str, Any]:
    kind = _validated_kind(args.kind)
    feature = _validated_name(args.feature, "feature")
    instance = _validated_name(args.instance, "instance")
    workspace_uuid = _validated_uuid(args.workspace_uuid)
    expected_sha = _validated_oid(args.expected_sha)
    branch = _validated_branch(kind, args.branch)
    return {
        "schema_version": 1,
        "intent_type": "clone_stage",
        "feature": feature,
        "instance": instance,
        "workspace_uuid": workspace_uuid,
        "clone_kind": kind,
        "expected_sha": expected_sha,
        "branch": branch,
        "worktrees_root": str(canonical_root),
        "source_name": f"{feature}-{instance}",
        "staging_name": f"{feature}--{instance}--{workspace_uuid}",
    }


def stage_clone(args: argparse.Namespace) -> Path:
    canonical_root, root_fd = _open_exact_root(args.worktrees_root)
    try:
        static = _stage_static(args, canonical_root)
        stage_name, _, _ = _intent_names(
            static["feature"], static["instance"], static["workspace_uuid"]
        )
        # Parse any durable authority before creating the CONTROL staging root.
        # The second descriptor-rooted read below closes the admission race before
        # an intent is consumed or installed.
        with RootedFS(args.runs_dir, root_mode=None) as rooted:
            _read_intent_optional(rooted, stage_name)
        staging_fd = _open_staging_root(root_fd, create=True)
        try:
            with RootedFS(args.runs_dir, root_mode=None) as rooted:
                intent = _read_intent_optional(rooted, stage_name)
                source_info = _stat_optional(root_fd, static["source_name"])
                destination_info = _stat_optional(staging_fd, static["staging_name"])
                if source_info is not None and destination_info is not None:
                    raise CloneGuardError("clone exists at source and staging paths")
                if intent is None:
                    if source_info is None:
                        raise CloneGuardError(
                            "clone disappeared before durable stage intent"
                        )
                    _validate_directory(source_info, static["source_name"])
                    source_mode = stat.S_IMODE(source_info.st_mode)
                    if static["clone_kind"] == "reader" and source_mode & 0o222:
                        raise CloneGuardError(
                            "reader root is writable before stage intent"
                        )
                    intent = {
                        **static,
                        "clone_dev": source_info.st_dev,
                        "clone_ino": source_info.st_ino,
                        "source_mode": source_mode,
                    }
                    rooted.atomic_write(
                        stage_name,
                        _encode_exact(intent),
                        directory_modes=(),
                        file_mode=0o600,
                    )
                    intent = _read_intent_optional(rooted, stage_name)
                    assert intent is not None
                _require_static(
                    intent,
                    static,
                    {"clone_dev", "clone_ino", "source_mode"},
                )
                original_mode = intent["source_mode"]
                if original_mode > 0o777:
                    raise CloneGuardError("invalid staged clone source mode")
                if static["clone_kind"] == "reader" and original_mode & 0o222:
                    raise CloneGuardError("durable reader source mode is writable")
                expected_binding = (intent["clone_dev"], intent["clone_ino"])

                source_info = _stat_optional(root_fd, static["source_name"])
                destination_info = _stat_optional(staging_fd, static["staging_name"])
                if source_info is not None and destination_info is not None:
                    raise CloneGuardError("clone exists at source and staging paths")
                if source_info is None and destination_info is None:
                    raise CloneGuardError("durably staged clone is unexpectedly absent")
                if source_info is not None:
                    if (source_info.st_dev, source_info.st_ino) != expected_binding:
                        raise CloneGuardError("source clone inode drift")
                    source_clone_fd = _open_bound_directory(
                        root_fd, static["source_name"], source_info
                    )
                    try:
                        _assert_same_device_contents(
                            source_clone_fd, source_info.st_dev
                        )
                        current_mode = stat.S_IMODE(os.fstat(source_clone_fd).st_mode)
                        transition_mode = original_mode | stat.S_IWUSR | stat.S_IXUSR
                        allowed_modes = {original_mode}
                        if static["clone_kind"] == "reader":
                            allowed_modes.add(transition_mode)
                        if current_mode not in allowed_modes:
                            raise CloneGuardError("source clone mode drift")
                        if (
                            static["clone_kind"] == "reader"
                            and current_mode != transition_mode
                        ):
                            os.fchmod(source_clone_fd, transition_mode)
                            os.fsync(source_clone_fd)
                            _checkpoint("after_reader_stage_chmod")
                        os.rename(
                            static["source_name"],
                            static["staging_name"],
                            src_dir_fd=root_fd,
                            dst_dir_fd=staging_fd,
                        )
                        os.fsync(root_fd)
                        os.fsync(staging_fd)
                        _checkpoint("after_clone_stage_rename")
                        if static["clone_kind"] == "reader":
                            _checkpoint("after_reader_stage_rename")
                            os.fchmod(source_clone_fd, original_mode)
                            os.fsync(source_clone_fd)
                            _checkpoint("after_reader_stage_restore")
                    finally:
                        os.close(source_clone_fd)
                else:
                    assert destination_info is not None
                    if (
                        destination_info.st_dev,
                        destination_info.st_ino,
                    ) != expected_binding:
                        raise CloneGuardError("staged clone inode drift")
                    destination_fd = _open_bound_directory(
                        staging_fd, static["staging_name"], destination_info
                    )
                    try:
                        _assert_same_device_contents(
                            destination_fd, destination_info.st_dev
                        )
                        current_mode = stat.S_IMODE(os.fstat(destination_fd).st_mode)
                        transition_mode = original_mode | stat.S_IWUSR | stat.S_IXUSR
                        allowed_modes = {original_mode}
                        if static["clone_kind"] == "reader":
                            allowed_modes.add(transition_mode)
                        if current_mode not in allowed_modes:
                            raise CloneGuardError("staged clone mode drift")
                        if (
                            static["clone_kind"] == "reader"
                            and current_mode != original_mode
                        ):
                            os.fchmod(destination_fd, original_mode)
                            os.fsync(destination_fd)
                    finally:
                        os.close(destination_fd)
                final_info = _stat_optional(staging_fd, static["staging_name"])
                if (
                    final_info is None
                    or (final_info.st_dev, final_info.st_ino) != expected_binding
                ):
                    raise CloneGuardError("staged clone binding was not preserved")
                if (
                    static["clone_kind"] == "reader"
                    and stat.S_IMODE(final_info.st_mode) != original_mode
                ):
                    raise CloneGuardError("reader root mode was not restored")
                rooted.assert_root_binding()
            return canonical_root / ".fleet-control-staging" / static["staging_name"]
        finally:
            os.close(staging_fd)
    finally:
        os.close(root_fd)


def _tombstone_static(args: argparse.Namespace, canonical_root: Path) -> dict[str, Any]:
    stage = _stage_static(args, canonical_root)
    return {
        **stage,
        "intent_type": "clone_retirement",
    }


def _load_tombstone(
    rooted: RootedFS,
    name: str,
    static: dict[str, Any],
) -> dict[str, Any]:
    value = _read_intent_optional(rooted, name)
    if value is None:
        raise IntentAbsent("clone retirement tombstone is absent")
    _require_static(value, static, {"clone_dev", "clone_ino", "staging_mode"})
    if value["staging_mode"] > 0o777:
        raise CloneGuardError("invalid retirement staging mode")
    return value


def _assert_tombstone_state(
    root_fd: int,
    staging_fd: int,
    value: dict[str, Any],
) -> os.stat_result | None:
    if _stat_optional(root_fd, value["source_name"]) is not None:
        raise CloneGuardError("retired clone returned to model-authorized path")
    staged = _stat_optional(staging_fd, value["staging_name"])
    if staged is None:
        return None
    _validate_directory(staged, value["staging_name"])
    if (staged.st_dev, staged.st_ino) != (value["clone_dev"], value["clone_ino"]):
        raise CloneGuardError("retirement staging inode drift")
    allowed = {
        value["staging_mode"],
        value["staging_mode"] | stat.S_IWUSR | stat.S_IXUSR,
    }
    if stat.S_IMODE(staged.st_mode) not in allowed:
        raise CloneGuardError("retirement staging mode drift")
    return staged


def tombstone(args: argparse.Namespace) -> Path:
    canonical_root, root_fd = _open_exact_root(args.worktrees_root)
    try:
        static = _tombstone_static(args, canonical_root)
        _, tombstone_name, _ = _intent_names(
            static["feature"], static["instance"], static["workspace_uuid"]
        )
        if args.mode == "ensure":
            # A corrupt tombstone must not cause even the staging root to appear.
            with RootedFS(args.runs_dir, root_mode=None) as rooted:
                _read_intent_optional(rooted, tombstone_name)
        staging_fd = _open_staging_root(root_fd, create=args.mode == "ensure")
        try:
            with RootedFS(args.runs_dir, root_mode=None) as rooted:
                if args.mode == "ensure":
                    existing = _read_intent_optional(rooted, tombstone_name)
                    if existing is None:
                        if _stat_optional(root_fd, static["source_name"]) is not None:
                            raise CloneGuardError(
                                "cannot retire a model-reachable clone"
                            )
                        staged = _stat_optional(staging_fd, static["staging_name"])
                        if staged is None:
                            raise CloneGuardError("verified staging clone is absent")
                        _validate_directory(staged, static["staging_name"])
                        staged_fd = _open_bound_directory(
                            staging_fd, static["staging_name"], staged
                        )
                        try:
                            _assert_same_device_contents(staged_fd, staged.st_dev)
                        finally:
                            os.close(staged_fd)
                        value = {
                            **static,
                            "clone_dev": staged.st_dev,
                            "clone_ino": staged.st_ino,
                            "staging_mode": stat.S_IMODE(staged.st_mode),
                        }
                        rooted.atomic_write(
                            tombstone_name,
                            _encode_exact(value),
                            directory_modes=(),
                            file_mode=0o600,
                        )
                    value = _load_tombstone(rooted, tombstone_name, static)
                    _assert_tombstone_state(root_fd, staging_fd, value)
                elif args.mode == "require":
                    value = _load_tombstone(rooted, tombstone_name, static)
                    _assert_tombstone_state(root_fd, staging_fd, value)
                elif args.mode == "remove":
                    value = _load_tombstone(rooted, tombstone_name, static)
                    staged = _assert_tombstone_state(root_fd, staging_fd, value)
                    if staged is not None:
                        clone_fd = _open_bound_directory(
                            staging_fd, static["staging_name"], staged
                        )
                        try:
                            removed = [0]
                            _remove_directory_contents(
                                clone_fd,
                                removed,
                                static["clone_kind"],
                                value["clone_dev"],
                            )
                        finally:
                            os.close(clone_fd)
                        current = _stat_optional(staging_fd, static["staging_name"])
                        if current is None or (
                            current.st_dev,
                            current.st_ino,
                        ) != (value["clone_dev"], value["clone_ino"]):
                            raise CloneGuardError("retirement root binding changed")
                        try:
                            os.rmdir(static["staging_name"], dir_fd=staging_fd)
                            os.fsync(staging_fd)
                        except OSError as exc:
                            raise CloneGuardError(
                                "cannot remove retired clone root"
                            ) from exc
                        _checkpoint("after_clone_retirement_remove")
                    _assert_tombstone_state(root_fd, staging_fd, value)
                elif args.mode == "clear":
                    value = _load_tombstone(rooted, tombstone_name, static)
                    if _assert_tombstone_state(root_fd, staging_fd, value) is not None:
                        raise CloneGuardError(
                            "cannot clear tombstone before clone removal"
                        )
                    rooted.unlink_regular(
                        tombstone_name,
                        directory_modes=(),
                        file_mode=0o600,
                    )
                else:
                    raise CloneGuardError("invalid tombstone mode")
            return Path(args.runs_dir).expanduser().resolve() / tombstone_name
        finally:
            os.close(staging_fd)
    finally:
        os.close(root_fd)


def _remove_directory_contents(
    directory_fd: int,
    removed: list[int],
    kind: str,
    expected_dev: int,
) -> None:
    opened = os.fstat(directory_fd)
    _validate_directory(opened, "retirement directory")
    if opened.st_dev != expected_dev:
        raise CloneGuardError("retirement directory device binding drift")
    writable_mode = stat.S_IMODE(opened.st_mode) | stat.S_IWUSR | stat.S_IXUSR
    if stat.S_IMODE(opened.st_mode) != writable_mode:
        os.fchmod(directory_fd, writable_mode)
        os.fsync(directory_fd)
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise CloneGuardError("cannot enumerate retired clone") from exc
    for name in names:
        if name in {"", ".", ".."} or "/" in name or "\0" in name:
            raise CloneGuardError("unsafe retired clone entry")
        info = _stat_optional(directory_fd, name)
        if info is None:
            raise CloneGuardError("retired clone changed during removal")
        if info.st_uid != os.geteuid():
            raise CloneGuardError("retired clone contains foreign-owned content")
        if info.st_dev != expected_dev:
            raise CloneGuardError("retired clone contains a cross-device descendant")
        if stat.S_ISDIR(info.st_mode):
            child_fd = _open_bound_directory(directory_fd, name, info)
            try:
                _remove_directory_contents(child_fd, removed, kind, expected_dev)
            finally:
                os.close(child_fd)
            rebound = _stat_optional(directory_fd, name)
            if rebound is None or (rebound.st_dev, rebound.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise CloneGuardError("retired directory binding changed")
            try:
                os.rmdir(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError as exc:
                raise CloneGuardError("cannot remove retired directory") from exc
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise CloneGuardError("retired clone contains hardlinked content")
            try:
                os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError as exc:
                raise CloneGuardError("cannot remove retired file") from exc
        elif stat.S_ISLNK(info.st_mode):
            try:
                os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError as exc:
                raise CloneGuardError("cannot remove retired symlink") from exc
        else:
            raise CloneGuardError("retired clone contains special filesystem content")
        removed[0] += 1
        if removed[0] == 1:
            _checkpoint("after_retirement_delete_entry")
            _checkpoint(f"after_{kind}_retirement_delete_entry")


def clear_stage_intent(args: argparse.Namespace) -> None:
    canonical_root, root_fd = _open_exact_root(args.worktrees_root)
    try:
        static = _stage_static(args, canonical_root)
        stage_name, _, _ = _intent_names(
            static["feature"], static["instance"], static["workspace_uuid"]
        )
        with RootedFS(args.runs_dir, root_mode=None) as rooted:
            value = _read_intent_optional(rooted, stage_name)
            if value is None:
                return
            _require_static(
                value,
                static,
                {"clone_dev", "clone_ino", "source_mode"},
            )
            rooted.unlink_regular(stage_name, directory_modes=(), file_mode=0o600)
    finally:
        os.close(root_fd)


def publication_intent(args: argparse.Namespace) -> Path:
    feature = _validated_name(args.feature, "feature")
    instance = _validated_name(args.instance, "instance")
    workspace_uuid = _validated_uuid(args.workspace_uuid)
    base_sha = _validated_oid(args.base_sha)
    candidate_sha = _validated_oid(args.candidate_sha)
    if len(base_sha) != len(candidate_sha):
        raise CloneGuardError("publication object format drift")
    target_repo = Path(args.target_repo).expanduser().resolve(strict=True)
    canonical_root, root_fd = _open_exact_root(args.worktrees_root)
    try:
        expected_staging = (
            canonical_root
            / ".fleet-control-staging"
            / f"{feature}--{instance}--{workspace_uuid}"
        )
        supplied_staging = Path(args.staging_path).expanduser()
        if supplied_staging != expected_staging:
            raise CloneGuardError("publication staging path drift")
        _, _, name = _intent_names(feature, instance, workspace_uuid)
        expected = {
            "schema_version": 1,
            "feature": feature,
            "instance": instance,
            "target_repo": str(target_repo),
            "branch": args.branch,
            "base_sha": base_sha,
            "candidate_sha": candidate_sha,
            "staging_path": str(expected_staging),
            "workspace_uuid": workspace_uuid,
        }
        content = _encode_exact(expected)
        with RootedFS(args.runs_dir, root_mode=None) as rooted:
            if args.mode == "ensure":
                actual = rooted.read_regular_optional(
                    name,
                    directory_modes=(),
                    file_mode=0o600,
                    max_bytes=_MAX_INTENT_BYTES,
                    require_single_link=True,
                )
                if actual is None:
                    rooted.atomic_write(
                        name, content, directory_modes=(), file_mode=0o600
                    )
                elif _decode_exact(actual) != expected:
                    raise CloneGuardError("publication intent drift")
            elif args.mode == "require":
                actual = rooted.read_regular(
                    name,
                    directory_modes=(),
                    file_mode=0o600,
                    max_bytes=_MAX_INTENT_BYTES,
                    require_single_link=True,
                )
                if _decode_exact(actual) != expected:
                    raise CloneGuardError("publication intent drift")
            elif args.mode == "clear":
                actual = rooted.read_regular(
                    name,
                    directory_modes=(),
                    file_mode=0o600,
                    max_bytes=_MAX_INTENT_BYTES,
                    require_single_link=True,
                )
                if _decode_exact(actual) != expected:
                    raise CloneGuardError("publication intent drift")
                rooted.unlink_regular(name, directory_modes=(), file_mode=0o600)
            else:
                raise CloneGuardError("invalid publication intent mode")
        return Path(args.runs_dir).expanduser().resolve() / name
    finally:
        os.close(root_fd)


def _common_clone_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--worktrees-root", type=Path, required=True)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--workspace-uuid", required=True)
    parser.add_argument(
        "--kind", choices=("partial", "reader", "writer"), required=True
    )
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--branch", default="-")


def _common_creation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--worktrees-root", type=Path, required=True)
    parser.add_argument("--target-repo", type=Path, required=True)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--workspace-uuid", required=True)
    parser.add_argument("--kind", choices=("reader", "writer"), required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--branch", default="-")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    root = subparsers.add_parser("ensure-root")
    root.add_argument("--worktrees-root", type=Path, required=True)
    root.add_argument("--protected", type=Path, action="append", default=[])

    device = subparsers.add_parser("verify-device")
    device.add_argument("--clone-path", type=Path, required=True)

    boot_lock = subparsers.add_parser("boot-lock")
    boot_lock.add_argument("--runs-dir", type=Path, required=True)
    boot_lock.add_argument("--feature", required=True)

    creation = subparsers.add_parser("creation")
    creation.add_argument(
        "mode", choices=("plan", "source", "bind", "require", "clear")
    )
    _common_creation_arguments(creation)

    creation_empty = subparsers.add_parser("creation-empty")
    _common_creation_arguments(creation_empty)

    creation_stage_kind = subparsers.add_parser("creation-stage-kind")
    _common_creation_arguments(creation_stage_kind)

    stage = subparsers.add_parser("stage")
    _common_clone_arguments(stage)

    retire = subparsers.add_parser("tombstone")
    retire.add_argument("mode", choices=("ensure", "require", "remove", "clear"))
    _common_clone_arguments(retire)

    clear_stage = subparsers.add_parser("clear-stage")
    _common_clone_arguments(clear_stage)

    publication = subparsers.add_parser("publication")
    publication.add_argument("mode", choices=("ensure", "require", "clear"))
    publication.add_argument("--runs-dir", type=Path, required=True)
    publication.add_argument("--worktrees-root", type=Path, required=True)
    publication.add_argument("--feature", required=True)
    publication.add_argument("--instance", required=True)
    publication.add_argument("--workspace-uuid", required=True)
    publication.add_argument("--target-repo", type=Path, required=True)
    publication.add_argument("--branch", required=True)
    publication.add_argument("--base-sha", required=True)
    publication.add_argument("--candidate-sha", required=True)
    publication.add_argument("--staging-path", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "ensure-root":
            print(ensure_root(args.worktrees_root, args.protected))
        elif args.command == "verify-device":
            print(assert_same_device_tree(args.clone_path))
        elif args.command == "boot-lock":
            hold_boot_lock(args.runs_dir, args.feature)
        elif args.command == "creation":
            print(creation_intent(args))
        elif args.command == "creation-empty":
            return 0 if creation_clone_is_empty(args) else 1
        elif args.command == "creation-stage-kind":
            print(stage_kind_for_creation(args))
        elif args.command == "stage":
            print(stage_clone(args))
        elif args.command == "tombstone":
            print(tombstone(args))
        elif args.command == "clear-stage":
            clear_stage_intent(args)
        elif args.command == "publication":
            print(publication_intent(args))
        else:  # pragma: no cover - argparse owns the command set.
            raise CloneGuardError("unknown clone guard command")
    except IntentAbsent as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except BootLockBusy as exc:
        print(str(exc), file=sys.stderr)
        return 73
    except (CloneGuardError, SafePathError, OSError, RuntimeError) as exc:
        print(f"fleet clone guard refused operation: {exc}", file=sys.stderr)
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
