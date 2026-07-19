#!/usr/bin/env python3
"""Ownership-safe local worker leases and crash reconciliation."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Callable, Iterator
import uuid

from fleet_identity import current_tree, mappings
import fleet_json
from fleet_ledger import append_event, latest_event
import fleet_safe_paths


TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "abandoned", "indeterminate"}


class LeaseError(RuntimeError):
    pass


class LeaseBusy(LeaseError):
    pass


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_MAX_METADATA_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_LOCAL_SLOT_RE = re.compile(r"local-slot-[1-9][0-9]*\.lock")


def _strict_json_object(raw: bytes) -> dict[str, Any] | None:
    try:
        value = fleet_json.loads(raw)
        if raw != fleet_json.canonical_bytes(value) + b"\n":
            return None
    except fleet_json.FleetJSONError:
        return None
    return value if isinstance(value, dict) else None


def _plain_string(value: Any, *, maximum: int = 4096) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and all(ord(character) >= 0x20 and character != "\x7f" for character in value)
    )


def _safe_string(value: Any) -> bool:
    if not _plain_string(value, maximum=255):
        return False
    try:
        _safe_component(value, "metadata")
    except LeaseError:
        return False
    return True


def _canonical_upper_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)).upper() == value
    except (ValueError, AttributeError):
        return False


def _aware_timestamp(value: Any) -> bool:
    if not _plain_string(value, maximum=64):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _valid_lease_metadata(value: dict[str, Any], name: str) -> bool:
    required = {
        "schema_version",
        "run_id",
        "feature",
        "instance",
        "role",
        "phase",
        "resource_class",
        "task_sha256",
        "workspace_uuid",
        "surface_uuid",
        "acquired_at",
        "pid",
        "pgid",
        "kind",
    }
    optional = {"runner", "started_at"}
    if set(value) - required - optional or not required.issubset(value):
        return False
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        return False
    if not all(
        _safe_string(value[field])
        for field in ("run_id", "feature", "instance", "role", "phase", "kind")
    ):
        return False
    if value["kind"] != name:
        return False
    resource_class = value["resource_class"]
    if resource_class not in {"local_light", "local_heavy", "remote"}:
        return False
    if not isinstance(value["task_sha256"], str) or not _SHA256_RE.fullmatch(
        value["task_sha256"]
    ):
        return False
    if not _canonical_upper_uuid(value["workspace_uuid"]) or not _canonical_upper_uuid(
        value["surface_uuid"]
    ):
        return False
    if not _aware_timestamp(value["acquired_at"]):
        return False

    pid = value["pid"]
    pgid = value["pgid"]
    inactive = pid is None and pgid is None
    active = type(pid) is int and pid > 0 and type(pgid) is int and pgid > 0
    if not (inactive or active):
        return False
    if active != ("started_at" in value):
        return False
    if "started_at" in value and not _aware_timestamp(value["started_at"]):
        return False

    instance_kind = f"{value['feature']}.{value['instance']}.lock"
    role_slot = re.fullmatch(
        rf"role-{re.escape(value['role'])}-[1-9][0-9]*\.lock", name
    )
    local_kind = (
        name == instance_kind
        or bool(_LOCAL_SLOT_RE.fullmatch(name))
        or bool(role_slot)
        or (name == "local-heavy.lock" and resource_class == "local_heavy")
    )
    if resource_class == "remote":
        remote_keys = required | {"runner"}
        if active:
            remote_keys.add("started_at")
        return (
            name == instance_kind
            and set(value) == remote_keys
            and value.get("runner") == "interactive"
        )
    return local_kind and "runner" not in value


def _valid_reconcile_summary(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "active",
        "quarantined",
        "unknown",
        "probe_error",
    }:
        return False
    if (
        not _plain_string(value["probe_error"], maximum=4096)
        and value["probe_error"] != ""
    ):
        return False
    for field in ("active", "quarantined", "unknown"):
        items = value[field]
        if not isinstance(items, list) or len(items) > 100_000:
            return False
        if not all(_plain_string(item) for item in items):
            return False
    return True


def _valid_closing_marker(value: dict[str, Any], name: str) -> bool:
    if set(value) != {
        "schema_version",
        "feature",
        "close_id",
        "workspace_uuid",
        "acquired_at",
        "reconcile",
    }:
        return False
    return (
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and _safe_string(value["feature"])
        and name == f"{value['feature']}.closing"
        and _safe_string(value["close_id"])
        and _canonical_upper_uuid(value["workspace_uuid"])
        and _aware_timestamp(value["acquired_at"])
        and _valid_reconcile_summary(value["reconcile"])
    )


def _safe_component(value: str, where: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\0" in value
        or (os.altsep and os.altsep in value)
    ):
        raise LeaseError(f"unsafe {where} path component")
    return value


def _validate_directory(info: os.stat_result, where: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise LeaseError(f"lease path is not a directory: {where}")
    if info.st_uid != os.geteuid():
        raise LeaseError(f"lease directory owner mismatch: {where}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise LeaseError(f"lease directory mode mismatch: {where}")


def _validate_regular(info: os.stat_result, where: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise LeaseError(f"lease path is not a regular file: {where}")
    if info.st_uid != os.geteuid():
        raise LeaseError(f"lease file owner mismatch: {where}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise LeaseError(f"lease file mode mismatch: {where}")


class _LeaseStore:
    """Descriptor-pinned lease storage beneath one trusted runs directory."""

    def __init__(self, runs_dir: Path, *, create_locks: bool) -> None:
        self._rooted: fleet_safe_paths.RootedFS | None = None
        self._locks_fd: int | None = None
        try:
            rooted = fleet_safe_paths.RootedFS(runs_dir)
            locks_fd = rooted._open_directory_chain(  # noqa: SLF001
                ("locks",), (0o700,), create=create_locks
            )
        except fleet_safe_paths.SafePathError as exc:
            if "rooted" in locals():
                rooted.close()
            if "rooted directory is missing: locks" in str(exc) and not create_locks:
                raise FileNotFoundError("lease lock directory is missing") from exc
            raise LeaseError(f"unsafe lease storage: {exc}") from exc
        except Exception:
            if "rooted" in locals():
                rooted.close()
            raise
        self._rooted = rooted
        self._locks_fd = locks_fd

    @property
    def root(self) -> Path:
        assert self._rooted is not None
        return self._rooted.root

    @property
    def locks_fd(self) -> int:
        if self._locks_fd is None:
            raise LeaseError("lease store is closed")
        return self._locks_fd

    def __enter__(self) -> _LeaseStore:
        self.assert_binding()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._locks_fd is not None:
            os.close(self._locks_fd)
            self._locks_fd = None
        if self._rooted is not None:
            self._rooted.close()
            self._rooted = None

    def assert_binding(self) -> None:
        if self._rooted is None:
            raise LeaseError("lease store is closed")
        try:
            self._rooted.assert_root_binding()
            _validate_directory(os.fstat(self.locks_fd), "locks")
            current = os.stat(
                "locks", dir_fd=self._rooted._root_fd, follow_symlinks=False
            )  # noqa: SLF001
        except (OSError, fleet_safe_paths.SafePathError) as exc:
            raise LeaseError("lease storage binding changed") from exc
        pinned = os.fstat(self.locks_fd)
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise LeaseError("lease lock directory binding changed")
        _validate_directory(current, "locks")

    def path_for(self, name: str) -> Path:
        return self.root / "locks" / _safe_component(name, "lease")

    def name_for(self, lease: Path) -> str:
        candidate = Path(lease)
        if ".." in candidate.parts or candidate.parent.name != "locks":
            raise LeaseError("lease path is outside the pinned lock directory")
        name = _safe_component(candidate.name, "lease")
        try:
            candidate_root = fleet_safe_paths.canonical_root(candidate.parent.parent)
        except fleet_safe_paths.SafePathError as exc:
            raise LeaseError("lease path has an unsafe root") from exc
        if candidate_root != self.root:
            raise LeaseError("lease path is outside the pinned lock directory")
        return name

    def exists_name(self, name: str) -> bool:
        name = _safe_component(name, "lease")
        self.assert_binding()
        try:
            os.stat(name, dir_fd=self.locks_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise LeaseError(f"cannot inspect lease path: {name}") from exc
        return True

    def lease_paths(self) -> list[Path]:
        self.assert_binding()
        try:
            names = sorted(os.listdir(self.locks_fd))
        except OSError as exc:
            raise LeaseError("cannot list lease lock directory") from exc
        return [self.path_for(name) for name in names if name.endswith(".lock")]

    def _open_lease(self, name: str) -> int:
        name = _safe_component(name, "lease")
        self.assert_binding()
        try:
            fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=self.locks_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise LeaseError(f"cannot open physical lease directory: {name}") from exc
        try:
            _validate_directory(os.fstat(fd), name)
        except Exception:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _read_file(directory_fd: int, name: str, where: str) -> bytes | None:
        try:
            fd = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise LeaseError(f"cannot open physical lease file: {where}") from exc
        try:
            info = os.fstat(fd)
            _validate_regular(info, where)
            if info.st_size > _MAX_METADATA_BYTES:
                raise LeaseError(f"lease metadata exceeds size limit: {where}")
            remaining = info.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    raise LeaseError(f"lease metadata changed during read: {where}")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise LeaseError(f"lease metadata grew during read: {where}")
            return b"".join(chunks)
        finally:
            os.close(fd)

    def read_metadata_name(self, name: str) -> dict[str, Any] | None:
        try:
            lease_fd = self._open_lease(name)
        except FileNotFoundError:
            return None
        try:
            raw = self._read_file(lease_fd, "lease.json", f"{name}/lease.json")
        finally:
            os.close(lease_fd)
        if raw is None:
            return None
        value = _strict_json_object(raw)
        if value is None or not _valid_lease_metadata(value, name):
            return None
        return value

    @staticmethod
    def _write_file(directory_fd: int, name: str, content: bytes, where: str) -> None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except OSError as exc:
            raise LeaseError(f"cannot create physical lease file: {where}") from exc
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise LeaseError(f"short lease metadata write: {where}")
                view = view[written:]
            os.fsync(fd)
            _validate_regular(os.fstat(fd), where)
        finally:
            os.close(fd)

    @staticmethod
    def _serialized(value: dict[str, Any]) -> bytes:
        try:
            return fleet_json.canonical_bytes(value) + b"\n"
        except fleet_json.FleetJSONError as exc:
            raise LeaseError("lease metadata is not canonical JSON") from exc

    def create_lease(self, name: str, value: dict[str, Any]) -> None:
        name = _safe_component(name, "lease")
        if not _valid_lease_metadata(value, name):
            raise LeaseError(f"invalid lease metadata: {name}")
        self.assert_binding()
        try:
            os.mkdir(name, 0o700, dir_fd=self.locks_fd)
            os.fsync(self.locks_fd)
        except OSError as exc:
            raise LeaseError(f"cannot create physical lease directory: {name}") from exc
        lease_fd: int | None = None
        try:
            lease_fd = self._open_lease(name)
            os.fchmod(lease_fd, 0o700)
            self._write_file(
                lease_fd,
                "lease.json",
                self._serialized(value),
                f"{name}/lease.json",
            )
            os.fsync(lease_fd)
        except Exception:
            if lease_fd is not None:
                try:
                    os.unlink("lease.json", dir_fd=lease_fd)
                except OSError:
                    pass
            try:
                os.rmdir(name, dir_fd=self.locks_fd)
                os.fsync(self.locks_fd)
            except OSError:
                pass
            raise
        finally:
            if lease_fd is not None:
                os.close(lease_fd)

    def update_lease(self, name: str, value: dict[str, Any]) -> None:
        name = _safe_component(name, "lease")
        if not _valid_lease_metadata(value, name):
            raise LeaseError(f"invalid lease metadata: {name}")
        lease_fd = self._open_lease(name)
        temporary = f".lease.{uuid.uuid4().hex}.tmp"
        try:
            if self._read_file(lease_fd, "lease.json", f"{name}/lease.json") is None:
                raise LeaseError(f"lease metadata is missing: {name}")
            self._write_file(
                lease_fd,
                temporary,
                self._serialized(value),
                f"{name}/{temporary}",
            )
            try:
                os.replace(
                    temporary,
                    "lease.json",
                    src_dir_fd=lease_fd,
                    dst_dir_fd=lease_fd,
                )
                os.fsync(lease_fd)
            except OSError as exc:
                raise LeaseError(f"cannot replace lease metadata: {name}") from exc
        finally:
            try:
                os.unlink(temporary, dir_fd=lease_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                os.close(lease_fd)
                raise LeaseError(f"cannot clean lease temporary: {name}") from exc
            os.close(lease_fd)
        self.assert_binding()

    def remove_owned(self, name: str, run_id: str) -> bool:
        try:
            lease_fd = self._open_lease(name)
        except FileNotFoundError:
            return False
        try:
            metadata = self.read_metadata_name(name)
            if not metadata or metadata.get("run_id") != run_id:
                raise LeaseError(f"lease owner mismatch: {self.path_for(name)}")
            entries = sorted(os.listdir(lease_fd))
            if entries != ["lease.json"]:
                raise LeaseError(
                    f"lease contains unexpected files: {self.path_for(name)}"
                )
            if self._read_file(lease_fd, "lease.json", f"{name}/lease.json") is None:
                raise LeaseError(f"lease metadata is missing: {name}")
            os.unlink("lease.json", dir_fd=lease_fd)
            os.fsync(lease_fd)
        except OSError as exc:
            raise LeaseError(f"cannot remove physical lease: {name}") from exc
        finally:
            os.close(lease_fd)
        try:
            os.rmdir(name, dir_fd=self.locks_fd)
            os.fsync(self.locks_fd)
        except OSError as exc:
            raise LeaseError(f"cannot remove physical lease directory: {name}") from exc
        self.assert_binding()
        return True

    def _directory_chain(
        self, parts: tuple[str, ...], modes: tuple[int | None, ...]
    ) -> int:
        assert self._rooted is not None
        try:
            return self._rooted._open_directory_chain(parts, modes, create=True)  # noqa: SLF001
        except fleet_safe_paths.SafePathError as exc:
            raise LeaseError(f"unsafe lease archive storage: {exc}") from exc

    def quarantine_lease(self, name: str, run_id: str) -> Path:
        name = _safe_component(name, "lease")
        run_id = _safe_component(run_id, "run_id")
        metadata = self.read_metadata_name(name)
        if not metadata or metadata.get("run_id") != run_id:
            raise LeaseError(
                f"lease owner mismatch during quarantine: {self.path_for(name)}"
            )
        lease_fd = self._open_lease(name)
        try:
            if sorted(os.listdir(lease_fd)) != ["lease.json"]:
                raise LeaseError(
                    f"lease contains unexpected files: {self.path_for(name)}"
                )
        finally:
            os.close(lease_fd)
        archive_fd = self._directory_chain(
            ("archive", "leases", run_id), (0o700, 0o700, 0o700)
        )
        destination = f"{time.time_ns()}-{name}"
        try:
            os.replace(
                name,
                destination,
                src_dir_fd=self.locks_fd,
                dst_dir_fd=archive_fd,
            )
            os.fsync(self.locks_fd)
            os.fsync(archive_fd)
        except OSError as exc:
            raise LeaseError(f"cannot quarantine physical lease: {name}") from exc
        finally:
            os.close(archive_fd)
        self.assert_binding()
        return self.root / "archive" / "leases" / run_id / destination

    @contextmanager
    def coordinated(self) -> Iterator[None]:
        name = ".coordinator"
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        created = False
        try:
            fd = os.open(
                name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.locks_fd
            )
            created = True
        except FileExistsError:
            try:
                fd = os.open(name, flags, dir_fd=self.locks_fd)
            except OSError as exc:
                raise LeaseError("cannot open physical coordinator lock") from exc
        except OSError as exc:
            raise LeaseError("cannot create physical coordinator lock") from exc
        try:
            if created:
                os.fchmod(fd, 0o600)
                os.fsync(fd)
                os.fsync(self.locks_fd)
            _validate_regular(os.fstat(fd), "locks/.coordinator")
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                self.assert_binding()
                yield
                self.assert_binding()
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def read_marker(self, name: str) -> dict[str, Any] | None:
        name = _safe_component(name, "closing marker")
        raw = self._read_file(self.locks_fd, name, name)
        if raw is None:
            return None
        value = _strict_json_object(raw)
        if value is None or not _valid_closing_marker(value, name):
            return None
        return value

    def create_marker(self, name: str, value: dict[str, Any]) -> Path:
        name = _safe_component(name, "closing marker")
        if not _valid_closing_marker(value, name):
            raise LeaseError(f"invalid closing marker: {name}")
        self._write_file(self.locks_fd, name, self._serialized(value), name)
        os.fsync(self.locks_fd)
        self.assert_binding()
        return self.path_for(name)

    def remove_marker(self, name: str) -> None:
        name = _safe_component(name, "closing marker")
        if self._read_file(self.locks_fd, name, name) is None:
            raise LeaseError(f"closing marker is missing: {name}")
        try:
            os.unlink(name, dir_fd=self.locks_fd)
            os.fsync(self.locks_fd)
        except OSError as exc:
            raise LeaseError(f"cannot remove closing marker: {name}") from exc
        self.assert_binding()

    def archive_marker(self, name: str, feature: str) -> Path:
        name = _safe_component(name, "closing marker")
        feature = _safe_component(feature, "feature")
        if self._read_file(self.locks_fd, name, name) is None:
            raise LeaseError(f"closing marker is missing: {name}")
        archive_fd = self._directory_chain(
            ("archive", "closing", feature), (0o700, 0o700, 0o700)
        )
        destination = f"{time.time_ns()}-closing.json"
        try:
            os.replace(
                name,
                destination,
                src_dir_fd=self.locks_fd,
                dst_dir_fd=archive_fd,
            )
            os.fsync(self.locks_fd)
            os.fsync(archive_fd)
        except OSError as exc:
            raise LeaseError(f"cannot archive closing marker: {name}") from exc
        finally:
            os.close(archive_fd)
        self.assert_binding()
        return self.root / "archive" / "closing" / feature / destination


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def coordinator(runs_dir: Path) -> Iterator[_LeaseStore]:
    with _LeaseStore(runs_dir, create_locks=True) as store:
        with store.coordinated():
            yield store


def _metadata_path(lease: Path) -> Path:
    return lease / "lease.json"


def _store_for_lease(lease: Path) -> _LeaseStore:
    candidate = Path(lease)
    if ".." in candidate.parts or candidate.parent.name != "locks":
        raise LeaseError("lease path is outside a lock directory")
    return _LeaseStore(candidate.parent.parent, create_locks=False)


def read_metadata(
    lease: Path, *, _store: _LeaseStore | None = None
) -> dict[str, Any] | None:
    if _store is not None:
        return _store.read_metadata_name(_store.name_for(lease))
    try:
        with _store_for_lease(lease) as store:
            return store.read_metadata_name(store.name_for(lease))
    except FileNotFoundError:
        return None


def _write_metadata(
    lease: Path, value: dict[str, Any], *, _store: _LeaseStore | None = None
) -> None:
    if _store is not None:
        _store.create_lease(_store.name_for(lease), value)
        return
    with _store_for_lease(lease) as store:
        store.create_lease(store.name_for(lease), value)


def _update_metadata(
    lease: Path, value: dict[str, Any], *, _store: _LeaseStore | None = None
) -> None:
    if _store is not None:
        _store.update_lease(_store.name_for(lease), value)
        return
    with _store_for_lease(lease) as store:
        store.update_lease(store.name_for(lease), value)


def _lease_dirs(runs_dir: Path, *, _store: _LeaseStore | None = None) -> list[Path]:
    if _store is not None:
        return _store.lease_paths()
    try:
        with _LeaseStore(runs_dir, create_locks=False) as store:
            return store.lease_paths()
    except FileNotFoundError:
        return []


def closing_path(runs_dir: Path, feature: str) -> Path:
    return runs_dir / "locks" / f"{feature}.closing"


def _remove_owned(
    lease: Path, run_id: str, *, _store: _LeaseStore | None = None
) -> bool:
    if _store is not None:
        return _store.remove_owned(_store.name_for(lease), run_id)
    try:
        with _store_for_lease(lease) as store:
            return store.remove_owned(store.name_for(lease), run_id)
    except FileNotFoundError:
        return False


def _quarantine(
    runs_dir: Path,
    lease: Path,
    run_id: str,
    *,
    _store: _LeaseStore | None = None,
) -> Path:
    if _store is not None:
        return _store.quarantine_lease(_store.name_for(lease), run_id)
    with _LeaseStore(runs_dir, create_locks=False) as store:
        return store.quarantine_lease(store.name_for(lease), run_id)


def _run_groups(
    runs_dir: Path,
    feature: str | None = None,
    *,
    _store: _LeaseStore | None = None,
) -> tuple[dict[str, list[Path]], list[Path]]:
    groups: dict[str, list[Path]] = {}
    unknown: list[Path] = []
    for lease in _lease_dirs(runs_dir, _store=_store):
        metadata = read_metadata(lease, _store=_store)
        if not metadata or not isinstance(metadata.get("run_id"), str):
            unknown.append(lease)
            continue
        if feature is not None and metadata.get("feature") != feature:
            continue
        groups.setdefault(metadata["run_id"], []).append(lease)
    return groups, unknown


def _surface_owner(
    runs_dir: Path,
    surface_uuid: str,
    *,
    excluding_run_id: str = "",
    _store: _LeaseStore | None = None,
) -> dict[str, Any] | None:
    target = surface_uuid.upper()
    for lease in _lease_dirs(runs_dir, _store=_store):
        metadata = read_metadata(lease, _store=_store)
        if not metadata or metadata.get("run_id") == excluding_run_id:
            continue
        if str(metadata.get("surface_uuid") or "").upper() == target:
            return metadata
    return None


def _reject_surface_owner(
    runs_dir: Path,
    surface_uuid: str,
    run_id: str,
    *,
    _store: _LeaseStore | None = None,
) -> None:
    owner = _surface_owner(
        runs_dir, surface_uuid, excluding_run_id=run_id, _store=_store
    )
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
        raise LeaseError(
            "cmux tree output does not match the expected id-format=both protocol"
        )
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
    _store: _LeaseStore,
) -> dict[str, Any]:
    groups, unknown = _run_groups(runs_dir, feature, _store=_store)
    quarantined: list[str] = []
    active: list[str] = []
    probe_error = ""
    tree_text: str | None = None

    for run_id, leases in groups.items():
        metadata = read_metadata(leases[0], _store=_store) or {}
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
                quarantined.append(
                    str(_quarantine(runs_dir, lease, run_id, _store=_store))
                )
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
    with coordinator(runs_dir) as store:
        return _reconcile_locked(
            runs_dir, feature=feature, tree_reader=tree_reader, _store=store
        )


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
    created: list[Path] = []
    with coordinator(runs_dir) as store:
        reconciled = _reconcile_locked(runs_dir, tree_reader=tree_reader, _store=store)
        _reject_unknown_leases(reconciled)
        feature = _safe_component(feature, "feature")
        instance = _safe_component(instance, "instance")
        role = _safe_component(role, "role")
        if store.exists_name(f"{feature}.closing"):
            raise LeaseBusy(f"fleet '{feature}' is closing")
        _reject_surface_owner(runs_dir, surface_uuid, run_id, _store=store)
        instance_lock = store.path_for(f"{feature}.{instance}.lock")
        if store.exists_name(instance_lock.name):
            raise LeaseBusy(f"instance '{instance}' is busy")
        heavy_lock = store.path_for("local-heavy.lock")
        if resource_class == "local_heavy" and store.exists_name(heavy_lock.name):
            raise LeaseBusy("a heavy local worker already holds the global lease")
        local_slot = next(
            (
                store.path_for(f"local-slot-{slot}.lock")
                for slot in range(1, max_local + 1)
                if not store.exists_name(f"local-slot-{slot}.lock")
            ),
            None,
        )
        if local_slot is None:
            raise LeaseBusy("all local-worker slots are busy")
        role_slot = next(
            (
                store.path_for(f"role-{role}-{slot}.lock")
                for slot in range(1, role_limit + 1)
                if not store.exists_name(f"role-{role}-{slot}.lock")
            ),
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
                _write_metadata(path, metadata, _store=store)
                created.append(path)
        except Exception:
            for path in reversed(created):
                _remove_owned(path, run_id, _store=store)
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
    with coordinator(runs_dir) as store:
        reconciled = _reconcile_locked(runs_dir, tree_reader=tree_reader, _store=store)
        _reject_unknown_leases(reconciled)
        feature = _safe_component(feature, "feature")
        instance = _safe_component(instance, "instance")
        if store.exists_name(f"{feature}.closing"):
            raise LeaseBusy(f"fleet '{feature}' is closing")
        _reject_surface_owner(runs_dir, surface_uuid, run_id, _store=store)
        instance_lock = store.path_for(f"{feature}.{instance}.lock")
        if store.exists_name(instance_lock.name):
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
            _store=store,
        )
    return instance_lock


def validate(runs_dir: Path, run_id: str, leases: list[Path]) -> None:
    with coordinator(runs_dir) as store:
        for lease in leases:
            metadata = read_metadata(lease, _store=store)
            if not metadata or metadata.get("run_id") != run_id:
                raise LeaseError(f"lease missing or owned by another run: {lease}")


def activate(
    runs_dir: Path, run_id: str, leases: list[Path], pid: int, pgid: int
) -> None:
    with coordinator(runs_dir) as store:
        all_metadata: list[tuple[Path, dict[str, Any]]] = []
        for lease in leases:
            metadata = read_metadata(lease, _store=store)
            if not metadata or metadata.get("run_id") != run_id:
                raise LeaseError(f"cannot activate unowned lease: {lease}")
            all_metadata.append((lease, metadata))
        for lease, metadata in all_metadata:
            metadata.update({"pid": pid, "pgid": pgid, "started_at": _utc_now()})
            _update_metadata(lease, metadata, _store=store)


def release(runs_dir: Path, run_id: str, leases: list[Path]) -> None:
    with coordinator(runs_dir) as store:
        for lease in leases:
            metadata = read_metadata(lease, _store=store)
            if not metadata or metadata.get("run_id") != run_id:
                raise LeaseError(f"lease missing or owned by another run: {lease}")
        for lease in leases:
            _remove_owned(lease, run_id, _store=store)


def check_active(runs_dir: Path, feature: str) -> list[str]:
    with coordinator(runs_dir) as store:
        groups, unknown = _run_groups(runs_dir, _store=store)
        active = [
            str(lease)
            for leases in groups.values()
            for lease in leases
            if (read_metadata(lease, _store=store) or {}).get("feature") == feature
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
    with coordinator(runs_dir) as store:
        feature = _safe_component(feature, "feature")
        result = _reconcile_locked(
            runs_dir,
            feature=feature,
            tree_reader=tree_reader,
            _store=store,
        )
        groups, unknown = _run_groups(runs_dir, _store=store)
        active = [
            lease
            for leases in groups.values()
            for lease in leases
            if (read_metadata(lease, _store=store) or {}).get("feature") == feature
        ]
        if active or unknown:
            raise LeaseBusy("active or malformed leases prevent teardown")
        marker_name = f"{feature}.closing"
        if store.exists_name(marker_name):
            previous = store.read_marker(marker_name)
            if previous is None:
                raise LeaseError(
                    f"closing marker is malformed: {store.path_for(marker_name)}"
                )
            try:
                tree_text = tree_reader()
            except RuntimeError as exc:
                raise LeaseError(
                    f"cannot validate existing closing owner: {exc}"
                ) from exc
            absent, _ = _confirmed_absent(previous, tree_text)
            if not absent:
                raise LeaseBusy(f"fleet '{feature}' already has a closing owner")
            store.archive_marker(marker_name, feature)
        return store.create_marker(
            marker_name,
            {
                "schema_version": 1,
                "feature": feature,
                "close_id": close_id,
                "workspace_uuid": workspace_uuid.upper(),
                "acquired_at": _utc_now(),
                "reconcile": result,
            },
        )


def end_close(runs_dir: Path, *, feature: str, close_id: str) -> None:
    with coordinator(runs_dir) as store:
        feature = _safe_component(feature, "feature")
        marker_name = f"{feature}.closing"
        metadata = store.read_marker(marker_name)
        if metadata is None:
            raise LeaseError(
                f"closing marker missing or malformed: {store.path_for(marker_name)}"
            )
        if metadata.get("close_id") != close_id:
            raise LeaseError(
                f"closing marker owner mismatch: {store.path_for(marker_name)}"
            )
        store.remove_marker(marker_name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("reconcile", "check"):
        command = sub.add_parser(name)
        command.add_argument("runs_dir")
        command.add_argument("--feature")
    command = sub.add_parser("acquire")
    command.add_argument("runs_dir")
    for name in (
        "run-id",
        "feature",
        "instance",
        "role",
        "phase",
        "resource-class",
        "task-sha256",
        "workspace-uuid",
        "surface-uuid",
    ):
        command.add_argument(f"--{name}", required=True)
    command.add_argument("--max-local", type=int, required=True)
    command.add_argument("--role-limit", type=int, required=True)
    command = sub.add_parser("acquire-frontier")
    command.add_argument("runs_dir")
    for name in (
        "run-id",
        "feature",
        "instance",
        "role",
        "phase",
        "task-sha256",
        "workspace-uuid",
        "surface-uuid",
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
            sys.stdout.buffer.write(fleet_json.canonical_bytes(result) + b"\n")
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
            result = reconcile(runs_dir, feature=args.feature)
            sys.stdout.buffer.write(fleet_json.canonical_bytes(result) + b"\n")
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
