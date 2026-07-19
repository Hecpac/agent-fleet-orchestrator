#!/usr/bin/env python3
"""Descriptor-anchored filesystem operations for one trusted local root."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import signal
import stat
import sys
from typing import Callable, Iterator, Sequence
import uuid


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


class SafePathError(RuntimeError):
    """A rooted filesystem operation escaped or violated its ownership contract."""


def _rename_noreplace(
    source: str,
    destination: str,
    *,
    source_fd: int,
    destination_fd: int,
) -> None:
    """Atomically rename one leaf only if the destination remains absent."""

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        result = function(
            source_fd,
            source_bytes,
            destination_fd,
            destination_bytes,
            0x00000004,  # RENAME_EXCL
        )
    elif hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        result = function(
            source_fd,
            source_bytes,
            destination_fd,
            destination_bytes,
            0x00000001,  # RENAME_NOREPLACE
        )
    else:  # pragma: no cover - supported macOS/Linux hosts provide one primitive.
        raise SafePathError("native exclusive rename is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _atomic_write_checkpoint(name: str) -> None:
    if os.environ.get("FLEET_TEST_SAFE_PATH_CRASH_AT") == name:
        os.kill(os.getpid(), signal.SIGKILL)


def _atomic_pending_prefix(leaf: str) -> str:
    return ".fleet-atomic-" + hashlib.sha256(leaf.encode("utf-8")).hexdigest() + "-"


def _atomic_pending_name(leaf: str, content: bytes) -> str:
    return _atomic_pending_prefix(leaf) + hashlib.sha256(content).hexdigest() + ".tmp"


def _validate_mode(value: int | None, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0o777:
        raise SafePathError(f"invalid {where} mode")
    return value


def _validate_owner(value: int | None) -> int:
    owner_uid = os.geteuid() if value is None else value
    if isinstance(owner_uid, bool) or not isinstance(owner_uid, int) or owner_uid < 0:
        raise SafePathError("invalid owner_uid")
    return owner_uid


def _validate_directory(
    info: os.stat_result,
    *,
    owner_uid: int,
    required_mode: int | None,
    where: str,
) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise SafePathError(f"rooted component is not a directory: {where}")
    if info.st_uid != owner_uid:
        raise SafePathError(f"rooted directory has unexpected owner: {where}")
    actual_mode = stat.S_IMODE(info.st_mode)
    if required_mode is None:
        if actual_mode & 0o022:
            raise SafePathError(f"rooted directory is group/world writable: {where}")
    elif actual_mode != required_mode:
        raise SafePathError(
            f"rooted directory mode mismatch: {where}: {oct(actual_mode)}"
        )


def _validate_regular(
    info: os.stat_result,
    *,
    owner_uid: int,
    required_mode: int,
    where: str,
) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise SafePathError(f"rooted file is not regular: {where}")
    if info.st_uid != owner_uid:
        raise SafePathError(f"rooted file has unexpected owner: {where}")
    actual_mode = stat.S_IMODE(info.st_mode)
    if actual_mode != required_mode:
        raise SafePathError(f"rooted file mode mismatch: {where}: {oct(actual_mode)}")


def _validate_leaf_binding(
    parent_fd: int,
    leaf: str,
    opened: os.stat_result,
    *,
    owner_uid: int,
    required_mode: int,
    where: str,
    require_single_link: bool = True,
) -> None:
    try:
        current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise SafePathError(f"rooted file binding changed: {where}") from exc
    _validate_regular(
        current,
        owner_uid=owner_uid,
        required_mode=required_mode,
        where=where,
    )
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise SafePathError(f"rooted file binding changed: {where}")
    if require_single_link and (opened.st_nlink != 1 or current.st_nlink != 1):
        raise SafePathError(f"rooted file has unexpected link count: {where}")


def canonical_root(
    path: Path | str,
    *,
    owner_uid: int | None = None,
    required_mode: int | None = None,
) -> Path:
    """Resolve only the operator-selected root and validate its physical directory.

    Resolving here deliberately accepts trusted aliases such as macOS
    ``/var -> /private/var``. Descendants are never resolved by pathname.
    """

    owner = _validate_owner(owner_uid)
    mode = _validate_mode(required_mode, "root")
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        info = resolved.lstat()
    except (OSError, RuntimeError) as exc:
        raise SafePathError(f"cannot resolve trusted root: {path}") from exc
    _validate_directory(
        info,
        owner_uid=owner,
        required_mode=mode,
        where=str(resolved),
    )
    return resolved


def _relative_parts(
    relative: Path | str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    try:
        raw = os.fspath(relative)
    except TypeError as exc:
        raise SafePathError("rooted path must be path-like") from exc
    if not isinstance(raw, str) or "\0" in raw:
        raise SafePathError("rooted path must be a NUL-free string")
    value = Path(raw)
    if value.is_absolute():
        raise SafePathError("rooted path must be relative")
    parts = value.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise SafePathError("rooted path contains an unsafe component")
    if not parts and not allow_empty:
        raise SafePathError("rooted path must not be empty")
    return parts


def _directory_modes(
    value: Sequence[int | None],
    *,
    count: int,
) -> tuple[int | None, ...]:
    if isinstance(value, (str, bytes)):
        raise SafePathError("directory_modes must be a mode sequence")
    modes = tuple(
        _validate_mode(mode, f"directory_modes[{index}]")
        for index, mode in enumerate(value)
    )
    if len(modes) != count:
        raise SafePathError(
            f"directory_modes has {len(modes)} entries; expected {count}"
        )
    return modes


class RootedFS:
    """Pin a canonical root descriptor and perform no-follow operations beneath it."""

    def __init__(
        self,
        root: Path | str,
        *,
        owner_uid: int | None = None,
        root_mode: int | None = None,
    ) -> None:
        self.owner_uid = _validate_owner(owner_uid)
        self.root_mode = _validate_mode(root_mode, "root")
        self.root = canonical_root(
            root,
            owner_uid=self.owner_uid,
            required_mode=self.root_mode,
        )
        try:
            before = self.root.lstat()
            root_fd = os.open(self.root, _DIRECTORY_FLAGS)
        except OSError as exc:
            raise SafePathError(f"cannot open trusted root: {self.root}") from exc
        try:
            after = os.fstat(root_fd)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise SafePathError("trusted root changed while being opened")
            _validate_directory(
                after,
                owner_uid=self.owner_uid,
                required_mode=self.root_mode,
                where=str(self.root),
            )
        except Exception:
            os.close(root_fd)
            raise
        self._root_fd: int | None = root_fd

    def __enter__(self) -> RootedFS:
        self.assert_root_binding()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None

    def assert_root_binding(self) -> None:
        """Fail unless the canonical root pathname still names the pinned inode."""

        if self._root_fd is None:
            raise SafePathError("rooted filesystem is closed")
        try:
            current = self.root.lstat()
            pinned = os.fstat(self._root_fd)
        except OSError as exc:
            raise SafePathError("trusted root binding changed") from exc
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise SafePathError("trusted root binding changed")
        _validate_directory(
            current,
            owner_uid=self.owner_uid,
            required_mode=self.root_mode,
            where=str(self.root),
        )
        _validate_directory(
            pinned,
            owner_uid=self.owner_uid,
            required_mode=self.root_mode,
            where=str(self.root),
        )

    def _duplicate_root(self) -> int:
        if self._root_fd is None:
            raise SafePathError("rooted filesystem is closed")
        try:
            return os.dup(self._root_fd)
        except OSError as exc:
            raise SafePathError("cannot duplicate trusted root descriptor") from exc

    def _open_directory_chain(
        self,
        parts: tuple[str, ...],
        modes: tuple[int | None, ...],
        *,
        create: bool,
    ) -> int:
        current_fd = self._duplicate_root()
        traversed: list[str] = []
        try:
            for part, required_mode in zip(parts, modes, strict=True):
                traversed.append(part)
                where = "/".join(traversed)
                created = False
                try:
                    next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise SafePathError(f"rooted directory is missing: {where}")
                    if required_mode is None:
                        raise SafePathError(
                            f"cannot create rooted directory without exact mode: {where}"
                        )
                    try:
                        os.mkdir(part, required_mode, dir_fd=current_fd)
                        created = True
                        os.fsync(current_fd)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise SafePathError(
                            f"cannot create rooted directory: {where}"
                        ) from exc
                    try:
                        next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
                    except OSError as exc:
                        raise SafePathError(
                            f"cannot open rooted directory: {where}"
                        ) from exc
                except OSError as exc:
                    raise SafePathError(
                        f"cannot open rooted directory: {where}"
                    ) from exc
                try:
                    if created:
                        os.fchmod(next_fd, required_mode)
                        os.fsync(next_fd)
                    _validate_directory(
                        os.fstat(next_fd),
                        owner_uid=self.owner_uid,
                        required_mode=required_mode,
                        where=where,
                    )
                except Exception:
                    os.close(next_fd)
                    raise
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    def _parent_descriptor(
        self,
        relative: Path | str,
        *,
        directory_modes: Sequence[int | None],
        create: bool,
    ) -> tuple[int, tuple[str, ...]]:
        parts = _relative_parts(relative)
        parent_parts = parts[:-1]
        modes = _directory_modes(directory_modes, count=len(parent_parts))
        parent_fd = self._open_directory_chain(parent_parts, modes, create=create)
        return parent_fd, parts

    def _read_leaf(
        self,
        parent_fd: int,
        leaf: str,
        *,
        relative: str,
        file_mode: int,
        max_bytes: int | None,
        require_single_link: bool = True,
    ) -> bytes:
        try:
            fd = os.open(leaf, _FILE_READ_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise SafePathError(f"cannot open rooted file: {relative}") from exc
        try:
            info = os.fstat(fd)
            _validate_regular(
                info,
                owner_uid=self.owner_uid,
                required_mode=file_mode,
                where=relative,
            )
            _validate_leaf_binding(
                parent_fd,
                leaf,
                info,
                owner_uid=self.owner_uid,
                required_mode=file_mode,
                where=relative,
                require_single_link=require_single_link,
            )
            if max_bytes is not None and info.st_size > max_bytes:
                raise SafePathError(f"rooted file exceeds size limit: {relative}")
            remaining = info.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise SafePathError(f"rooted file changed during read: {relative}")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise SafePathError(f"rooted file grew during read: {relative}")
            _validate_leaf_binding(
                parent_fd,
                leaf,
                os.fstat(fd),
                owner_uid=self.owner_uid,
                required_mode=file_mode,
                where=relative,
                require_single_link=require_single_link,
            )
            return b"".join(chunks)
        finally:
            os.close(fd)

    def read_regular(
        self,
        relative: Path | str,
        *,
        directory_modes: Sequence[int | None],
        file_mode: int = 0o600,
        max_bytes: int | None = None,
        require_single_link: bool = True,
    ) -> bytes:
        """Read one regular file without following any descendant symlink."""

        self.assert_root_binding()
        if not isinstance(require_single_link, bool):
            raise SafePathError("require_single_link must be bool")
        mode = _validate_mode(file_mode, "file")
        assert mode is not None
        if max_bytes is not None and (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 0
        ):
            raise SafePathError("invalid max_bytes")
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=False,
        )
        try:
            content = self._read_leaf(
                parent_fd,
                parts[-1],
                relative="/".join(parts),
                file_mode=mode,
                max_bytes=max_bytes,
                require_single_link=require_single_link,
            )
            self.assert_root_binding()
            return content
        finally:
            os.close(parent_fd)

    def read_regular_optional(
        self,
        relative: Path | str,
        *,
        directory_modes: Sequence[int | None],
        file_mode: int = 0o600,
        max_bytes: int | None = None,
        require_single_link: bool = True,
    ) -> bytes | None:
        """Read a rooted regular file, returning ``None`` only for an absent leaf."""

        self.assert_root_binding()
        if not isinstance(require_single_link, bool):
            raise SafePathError("require_single_link must be bool")
        mode = _validate_mode(file_mode, "file")
        assert mode is not None
        if max_bytes is not None and (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 0
        ):
            raise SafePathError("invalid max_bytes")
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=False,
        )
        logical = "/".join(parts)
        try:
            try:
                fd = os.open(parts[-1], _FILE_READ_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                self.assert_root_binding()
                return None
            except OSError as exc:
                raise SafePathError(f"cannot open rooted file: {logical}") from exc
            try:
                info = os.fstat(fd)
                _validate_regular(
                    info,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                _validate_leaf_binding(
                    parent_fd,
                    parts[-1],
                    info,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                    require_single_link=require_single_link,
                )
                if max_bytes is not None and info.st_size > max_bytes:
                    raise SafePathError(f"rooted file exceeds maximum size: {logical}")
                remaining = info.st_size
                chunks: list[bytes] = []
                while remaining:
                    chunk = os.read(fd, min(1024 * 1024, remaining))
                    if not chunk:
                        raise SafePathError(
                            f"rooted file changed during read: {logical}"
                        )
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if os.read(fd, 1):
                    raise SafePathError(f"rooted file grew during read: {logical}")
                _validate_leaf_binding(
                    parent_fd,
                    parts[-1],
                    os.fstat(fd),
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                    require_single_link=require_single_link,
                )
                self.assert_root_binding()
                return b"".join(chunks)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    def assert_absent(
        self,
        relative: Path | str,
        *,
        directory_modes: Sequence[int | None],
    ) -> None:
        """Fail unless an exact rooted leaf is absent, without opening its target."""

        self.assert_root_binding()
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=False,
        )
        logical = "/".join(parts)
        try:
            try:
                os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                self.assert_root_binding()
                return
            except OSError as exc:
                raise SafePathError(f"cannot inspect rooted path: {logical}") from exc
            raise SafePathError(f"rooted path must be absent: {logical}")
        finally:
            os.close(parent_fd)

    def stat_regular(
        self,
        relative: Path | str,
        *,
        directory_modes: Sequence[int | None],
        file_mode: int = 0o600,
        require_single_link: bool = True,
    ) -> os.stat_result:
        """Validate and stat a regular leaf without reading any content."""

        self.assert_root_binding()
        mode = _validate_mode(file_mode, "file")
        assert mode is not None
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=False,
        )
        logical = "/".join(parts)
        try:
            try:
                fd = os.open(parts[-1], _FILE_READ_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise SafePathError(f"cannot open rooted file: {logical}") from exc
            try:
                opened = os.fstat(fd)
                _validate_regular(
                    opened,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                _validate_leaf_binding(
                    parent_fd,
                    parts[-1],
                    opened,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                    require_single_link=require_single_link,
                )
                self.assert_root_binding()
                return opened
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    def create_symlink(
        self,
        relative: Path | str,
        target: Path | str,
        *,
        directory_modes: Sequence[int | None],
    ) -> Path:
        """Create one exact rooted symlink without following either leaf."""

        self.assert_root_binding()
        raw_target = os.fspath(target)
        if (
            not isinstance(raw_target, str)
            or not raw_target
            or "\0" in raw_target
            or not Path(raw_target).is_absolute()
        ):
            raise SafePathError("symlink target must be an absolute NUL-free path")
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=True,
        )
        logical = "/".join(parts)
        try:
            try:
                os.symlink(raw_target, parts[-1], dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError as exc:
                raise SafePathError(f"cannot create rooted symlink: {logical}") from exc
            self.assert_symlink(
                relative,
                raw_target,
                directory_modes=directory_modes,
            )
            return self.root.joinpath(*parts)
        finally:
            os.close(parent_fd)

    def assert_symlink(
        self,
        relative: Path | str,
        target: Path | str,
        *,
        directory_modes: Sequence[int | None],
    ) -> None:
        """Fail unless a rooted leaf is the exact owner-bound symlink requested."""

        self.assert_root_binding()
        raw_target = os.fspath(target)
        if not isinstance(raw_target, str) or "\0" in raw_target:
            raise SafePathError("invalid expected symlink target")
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=False,
        )
        logical = "/".join(parts)
        try:
            try:
                info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                actual = os.readlink(parts[-1], dir_fd=parent_fd)
            except OSError as exc:
                raise SafePathError(
                    f"cannot inspect rooted symlink: {logical}"
                ) from exc
            if not stat.S_ISLNK(info.st_mode) or info.st_uid != self.owner_uid:
                raise SafePathError(
                    f"rooted leaf is not an owner-bound symlink: {logical}"
                )
            if actual != raw_target:
                raise SafePathError(f"rooted symlink target mismatch: {logical}")
            self.assert_root_binding()
        finally:
            os.close(parent_fd)

    def atomic_write(
        self,
        relative: Path | str,
        content: bytes,
        *,
        directory_modes: Sequence[int | None],
        file_mode: int = 0o600,
        require_absent: bool = False,
    ) -> Path:
        """Publish bytes without clobbering a conflicting existing rooted file."""

        self.assert_root_binding()
        if not isinstance(content, bytes):
            raise SafePathError("atomic content must be bytes")
        if not isinstance(require_absent, bool):
            raise SafePathError("require_absent must be bool")
        mode = _validate_mode(file_mode, "file")
        assert mode is not None
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=True,
        )
        leaf = parts[-1]
        logical = "/".join(parts)
        locked = False
        try:
            try:
                fcntl.flock(parent_fd, fcntl.LOCK_EX)
                locked = True
            except OSError as exc:
                raise SafePathError(
                    f"cannot lock rooted publication directory: {logical}"
                ) from exc
            try:
                existing = self._read_leaf(
                    parent_fd,
                    leaf,
                    relative=logical,
                    file_mode=mode,
                    max_bytes=len(content),
                )
            except SafePathError as exc:
                try:
                    os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    existing = None
                except OSError as stat_exc:
                    raise SafePathError(
                        f"cannot inspect rooted file: {logical}"
                    ) from stat_exc
                else:
                    raise exc
            if require_absent and existing is not None:
                raise SafePathError(f"rooted file must be absent: {logical}")
            temporary_prefix = _atomic_pending_prefix(leaf)
            temporary = _atomic_pending_name(leaf, content)
            temporary_created = False

            try:
                pending_names = sorted(
                    name
                    for name in os.listdir(parent_fd)
                    if name.startswith(temporary_prefix) and name.endswith(".tmp")
                )
            except OSError as exc:
                raise SafePathError(
                    f"cannot enumerate rooted pending publications: {logical}"
                ) from exc
            conflicting_pending = [name for name in pending_names if name != temporary]
            if conflicting_pending:
                # Never discard a content-addressed, fsync-complete intent for
                # another request. Its exact owner/type/link contract is still
                # checked so an attacker cannot turn the conflict into a path
                # traversal or hardlink cleanup primitive.
                for name in conflicting_pending:
                    self._read_leaf(
                        parent_fd,
                        name,
                        relative=f"{logical} (conflicting pending)",
                        file_mode=mode,
                        max_bytes=None,
                    )
                raise SafePathError(
                    f"rooted pending publication conflicts with requested bytes: {logical}"
                )

            def read_pending() -> tuple[bytes, os.stat_result] | None:
                try:
                    pending_info = os.stat(
                        temporary,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return None
                except OSError as stat_exc:
                    raise SafePathError(
                        f"cannot inspect rooted temporary file: {logical}"
                    ) from stat_exc
                pending = self._read_leaf(
                    parent_fd,
                    temporary,
                    relative=f"{logical} (pending)",
                    file_mode=mode,
                    max_bytes=len(content),
                )
                rebound = os.stat(
                    temporary,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (pending_info.st_dev, pending_info.st_ino) != (
                    rebound.st_dev,
                    rebound.st_ino,
                ):
                    raise SafePathError(
                        f"rooted temporary file binding changed: {logical}"
                    )
                return pending, rebound

            pending_record = read_pending()
            if pending_record is not None and pending_record[0] != content:
                # The parent-directory flock proves no live atomic writer can
                # still own this deterministic pending inode. A valid private,
                # single-link mismatch under this content-addressed pending
                # name is therefore an interrupted write of these exact
                # requested bytes, never another request's committed intent.
                _, pending_info = pending_record
                _validate_leaf_binding(
                    parent_fd,
                    temporary,
                    pending_info,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=f"{logical} (pending)",
                )
                os.unlink(temporary, dir_fd=parent_fd)
                os.fsync(parent_fd)
                pending_record = None
            if existing is not None:
                if existing != content:
                    raise SafePathError(
                        f"rooted file conflicts with requested bytes: {logical}"
                    )
                if pending_record is not None:
                    pending, pending_info = pending_record
                    if pending != content:
                        raise SafePathError(
                            f"rooted temporary file conflicts with requested bytes: {logical}"
                        )
                    _validate_leaf_binding(
                        parent_fd,
                        temporary,
                        pending_info,
                        owner_uid=self.owner_uid,
                        required_mode=mode,
                        where=f"{logical} (pending)",
                    )
                    os.unlink(temporary, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                else:
                    # A prior process may have completed the exclusive rename
                    # and died before syncing the containing directory.  An
                    # identical retry is the recovery boundary, so make the
                    # already-present directory entry durable before claiming
                    # success.
                    os.fsync(parent_fd)
                self.assert_root_binding()
                return self.root.joinpath(*parts)

            try:
                if pending_record is None:
                    flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                    )
                    fd = os.open(temporary, flags, mode, dir_fd=parent_fd)
                    temporary_created = True
                    try:
                        os.fchmod(fd, mode)
                        view = memoryview(content)
                        if view:
                            first_size = max(1, len(view) // 2)
                            first = view[:first_size]
                            written = os.write(fd, first)
                            if written != len(first):
                                raise SafePathError(f"short rooted write: {logical}")
                            view = view[written:]
                            _atomic_write_checkpoint("after_atomic_partial_write")
                        while view:
                            written = os.write(fd, view)
                            if written <= 0:
                                raise SafePathError(f"short rooted write: {logical}")
                            view = view[written:]
                        os.fsync(fd)
                        temporary_info = os.fstat(fd)
                        _validate_regular(
                            temporary_info,
                            owner_uid=self.owner_uid,
                            required_mode=mode,
                            where=logical,
                        )
                        if temporary_info.st_nlink != 1:
                            raise SafePathError(
                                f"rooted temporary file has unexpected link count: {logical}"
                            )
                    finally:
                        os.close(fd)
                    os.fsync(parent_fd)
                    _atomic_write_checkpoint("after_atomic_pending_fsync")
                else:
                    pending, temporary_info = pending_record
                    if pending != content:
                        raise SafePathError(
                            f"rooted temporary file conflicts with requested bytes: {logical}"
                        )
                    temporary_created = True
                try:
                    _rename_noreplace(
                        temporary,
                        leaf,
                        source_fd=parent_fd,
                        destination_fd=parent_fd,
                    )
                    temporary_created = False
                    _atomic_write_checkpoint("after_atomic_rename")
                    os.fsync(parent_fd)
                except OSError as exc:
                    if exc.errno != errno.EEXIST:
                        raise SafePathError(
                            f"cannot publish rooted file: {logical}"
                        ) from exc
                    if require_absent:
                        raise SafePathError(
                            f"rooted file appeared during exclusive publication: {logical}"
                        ) from exc
                    existing = self._read_leaf(
                        parent_fd,
                        leaf,
                        relative=logical,
                        file_mode=mode,
                        max_bytes=len(content),
                    )
                    if existing != content:
                        raise SafePathError(
                            f"rooted file conflicts with requested bytes: {logical}"
                        )
                published = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                _validate_regular(
                    published,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                if published.st_nlink != 1:
                    raise SafePathError(
                        f"rooted file has unexpected link count: {logical}"
                    )
                if not temporary_created and (
                    published.st_dev,
                    published.st_ino,
                ) != (temporary_info.st_dev, temporary_info.st_ino):
                    raise SafePathError(f"rooted file binding changed: {logical}")
            finally:
                if temporary_created:
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        raise SafePathError(
                            f"cannot clean rooted temporary file: {logical}"
                        ) from exc
            self.assert_root_binding()
            return self.root.joinpath(*parts)
        finally:
            if locked:
                fcntl.flock(parent_fd, fcntl.LOCK_UN)
            os.close(parent_fd)

    def replace_regular(
        self,
        relative: Path | str,
        content: bytes,
        *,
        directory_modes: Sequence[int | None],
        file_mode: int = 0o600,
    ) -> Path:
        """Durably replace one rooted regular file without following links."""

        self.assert_root_binding()
        if not isinstance(content, bytes):
            raise SafePathError("replacement content must be bytes")
        mode = _validate_mode(file_mode, "file")
        assert mode is not None
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=True,
        )
        leaf = parts[-1]
        logical = "/".join(parts)
        temporary = f".{leaf}.{uuid.uuid4().hex}.tmp"
        temporary_created = False
        try:
            try:
                existing = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            except OSError as exc:
                raise SafePathError(
                    f"cannot inspect rooted replacement file: {logical}"
                ) from exc
            if existing is not None:
                _validate_regular(
                    existing,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                if existing.st_nlink != 1:
                    raise SafePathError(
                        f"rooted replacement file has unexpected link count: {logical}"
                    )
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                fd = os.open(temporary, flags, mode, dir_fd=parent_fd)
                temporary_created = True
            except OSError as exc:
                raise SafePathError(
                    f"cannot create rooted replacement file: {logical}"
                ) from exc
            try:
                os.fchmod(fd, mode)
                view = memoryview(content)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise SafePathError(f"short rooted replacement: {logical}")
                    view = view[written:]
                os.fsync(fd)
                temporary_info = os.fstat(fd)
                _validate_regular(
                    temporary_info,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
            finally:
                os.close(fd)
            try:
                current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if existing is not None:
                    raise SafePathError(f"rooted replacement target changed: {logical}")
            except OSError as exc:
                raise SafePathError(
                    f"cannot recheck rooted replacement file: {logical}"
                ) from exc
            else:
                _validate_regular(
                    current,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                if current.st_nlink != 1:
                    raise SafePathError(
                        f"rooted replacement file has unexpected link count: {logical}"
                    )
                if existing is None or (
                    current.st_dev,
                    current.st_ino,
                ) != (existing.st_dev, existing.st_ino):
                    raise SafePathError(f"rooted replacement target changed: {logical}")
            try:
                os.replace(
                    temporary,
                    leaf,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temporary_created = False
                os.fsync(parent_fd)
                _validate_leaf_binding(
                    parent_fd,
                    leaf,
                    temporary_info,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
            except OSError as exc:
                raise SafePathError(
                    f"cannot publish rooted replacement file: {logical}"
                ) from exc
            self.assert_root_binding()
            return self.root.joinpath(*parts)
        finally:
            if temporary_created:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise SafePathError(
                        f"cannot clean rooted replacement file: {logical}"
                    ) from exc
            os.close(parent_fd)

    def unlink_regular(
        self,
        relative: Path | str,
        *,
        directory_modes: Sequence[int | None],
        file_mode: int = 0o600,
        missing_ok: bool = False,
    ) -> bool:
        """Unlink an exact rooted regular file after descriptor/inode validation."""

        self.assert_root_binding()
        mode = _validate_mode(file_mode, "file")
        assert mode is not None
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=False,
        )
        leaf = parts[-1]
        logical = "/".join(parts)
        try:
            try:
                fd = os.open(leaf, _FILE_READ_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                if missing_ok:
                    self.assert_root_binding()
                    return False
                raise SafePathError(f"rooted file is missing: {logical}")
            except OSError as exc:
                raise SafePathError(
                    f"cannot open rooted unlink file: {logical}"
                ) from exc
            try:
                opened = os.fstat(fd)
                _validate_regular(
                    opened,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                _validate_leaf_binding(
                    parent_fd,
                    leaf,
                    opened,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                try:
                    os.unlink(leaf, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except OSError as exc:
                    raise SafePathError(
                        f"cannot unlink rooted file: {logical}"
                    ) from exc
            finally:
                os.close(fd)
            self.assert_root_binding()
            return True
        finally:
            os.close(parent_fd)

    def unlink_regular_if_content(
        self,
        relative: Path | str,
        expected_content: bytes,
        *,
        directory_modes: Sequence[int | None],
        file_mode: int = 0o600,
        missing_ok: bool = False,
    ) -> bool:
        """Unlink a locked exact file only when all of its bytes still match."""

        self.assert_root_binding()
        if not isinstance(expected_content, bytes):
            raise SafePathError("expected unlink content must be bytes")
        mode = _validate_mode(file_mode, "file")
        assert mode is not None
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=False,
        )
        leaf = parts[-1]
        logical = "/".join(parts)
        try:
            try:
                fd = os.open(leaf, _FILE_READ_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                if missing_ok:
                    self.assert_root_binding()
                    return False
                raise SafePathError(f"rooted file is missing: {logical}")
            except OSError as exc:
                raise SafePathError(
                    f"cannot open rooted guarded unlink file: {logical}"
                ) from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                opened = os.fstat(fd)
                _validate_regular(
                    opened,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                _validate_leaf_binding(
                    parent_fd,
                    leaf,
                    opened,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                    require_single_link=True,
                )
                if opened.st_size != len(expected_content):
                    raise SafePathError(
                        f"rooted file content changed before unlink: {logical}"
                    )
                remaining = opened.st_size
                chunks: list[bytes] = []
                while remaining:
                    chunk = os.read(fd, min(1024 * 1024, remaining))
                    if not chunk:
                        raise SafePathError(
                            f"rooted file changed during guarded unlink: {logical}"
                        )
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if b"".join(chunks) != expected_content or os.read(fd, 1):
                    raise SafePathError(
                        f"rooted file content changed before unlink: {logical}"
                    )
                rebound = os.fstat(fd)
                if (rebound.st_dev, rebound.st_ino, rebound.st_size) != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                ):
                    raise SafePathError(
                        f"rooted file binding changed before unlink: {logical}"
                    )
                _validate_leaf_binding(
                    parent_fd,
                    leaf,
                    rebound,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                    require_single_link=True,
                )
                try:
                    os.unlink(leaf, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except OSError as exc:
                    raise SafePathError(
                        f"cannot unlink exact rooted file: {logical}"
                    ) from exc
            finally:
                os.close(fd)
            self.assert_root_binding()
            return True
        finally:
            os.close(parent_fd)

    @contextmanager
    def open_append_regular(
        self,
        relative: Path | str,
        *,
        directory_modes: Sequence[int | None],
        file_mode: int = 0o600,
        require_single_link: bool = True,
    ) -> Iterator[int]:
        """Open an exact rooted append descriptor suitable for a child process."""

        if not isinstance(require_single_link, bool):
            raise SafePathError("require_single_link must be bool")
        self.assert_root_binding()
        mode = _validate_mode(file_mode, "file")
        assert mode is not None
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=True,
        )
        leaf = parts[-1]
        logical = "/".join(parts)
        created = False
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            try:
                fd = os.open(
                    leaf,
                    flags | os.O_CREAT | os.O_EXCL,
                    mode,
                    dir_fd=parent_fd,
                )
                created = True
            except FileExistsError:
                try:
                    fd = os.open(leaf, flags, dir_fd=parent_fd)
                except OSError as exc:
                    raise SafePathError(
                        f"cannot open rooted append descriptor: {logical}"
                    ) from exc
            except OSError as exc:
                raise SafePathError(
                    f"cannot create rooted append descriptor: {logical}"
                ) from exc
            try:
                if created:
                    os.fchmod(fd, mode)
                    os.fsync(parent_fd)
                initial = os.fstat(fd)
                _validate_regular(
                    initial,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                _validate_leaf_binding(
                    parent_fd,
                    leaf,
                    initial,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                    require_single_link=require_single_link,
                )
                self.assert_root_binding()
                yield fd
                final = os.fstat(fd)
                _validate_regular(
                    final,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                _validate_leaf_binding(
                    parent_fd,
                    leaf,
                    final,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                    require_single_link=require_single_link,
                )
                self.assert_root_binding()
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    def guarded_append_regular(
        self,
        relative: Path | str,
        content: bytes,
        *,
        directory_modes: Sequence[int | None],
        reject_if: Callable[[bytes], bool],
        file_mode: int = 0o600,
        max_existing_bytes: int = 64 * 1024 * 1024,
        require_single_link: bool = True,
    ) -> tuple[Path, bool]:
        """Validate a locked snapshot, then durably append within the size cap."""

        if not isinstance(content, bytes) or not content:
            raise SafePathError("appended content must be non-empty bytes")
        if not callable(reject_if):
            raise SafePathError("reject_if must be callable")
        if not isinstance(require_single_link, bool):
            raise SafePathError("require_single_link must be bool")
        if (
            isinstance(max_existing_bytes, bool)
            or not isinstance(max_existing_bytes, int)
            or max_existing_bytes < 0
        ):
            raise SafePathError("invalid max_existing_bytes")
        if len(content) > max_existing_bytes:
            raise SafePathError("appended content exceeds maximum size")
        self.assert_root_binding()
        mode = _validate_mode(file_mode, "file")
        assert mode is not None
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=True,
        )
        leaf = parts[-1]
        logical = "/".join(parts)
        created = False
        flags = (
            os.O_RDWR
            | os.O_APPEND
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            try:
                fd = os.open(
                    leaf,
                    flags | os.O_CREAT | os.O_EXCL,
                    mode,
                    dir_fd=parent_fd,
                )
                created = True
            except FileExistsError:
                try:
                    fd = os.open(leaf, flags, dir_fd=parent_fd)
                except OSError as exc:
                    raise SafePathError(
                        f"cannot open rooted guarded append file: {logical}"
                    ) from exc
            except OSError as exc:
                raise SafePathError(
                    f"cannot create rooted guarded append file: {logical}"
                ) from exc
            try:
                if created:
                    os.fchmod(fd, mode)
                    os.fsync(parent_fd)
                _validate_regular(
                    os.fstat(fd),
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    info = os.fstat(fd)
                    _validate_regular(
                        info,
                        owner_uid=self.owner_uid,
                        required_mode=mode,
                        where=logical,
                    )
                    _validate_leaf_binding(
                        parent_fd,
                        leaf,
                        info,
                        owner_uid=self.owner_uid,
                        required_mode=mode,
                        where=logical,
                        require_single_link=require_single_link,
                    )
                    if info.st_size > max_existing_bytes:
                        raise SafePathError(
                            f"rooted guarded append file exceeds maximum size: {logical}"
                        )
                    if len(content) > max_existing_bytes - info.st_size:
                        raise SafePathError(
                            "rooted guarded append file would exceed maximum size: "
                            f"{logical}"
                        )
                    os.lseek(fd, 0, os.SEEK_SET)
                    remaining = info.st_size
                    chunks: list[bytes] = []
                    while remaining:
                        chunk = os.read(fd, min(1024 * 1024, remaining))
                        if not chunk:
                            raise SafePathError(
                                f"rooted guarded append file changed during read: {logical}"
                            )
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    snapshot = b"".join(chunks)
                    rejection = reject_if(snapshot)
                    if not isinstance(rejection, bool):
                        raise SafePathError("reject_if must return bool")
                    guarded = os.fstat(fd)
                    _validate_leaf_binding(
                        parent_fd,
                        leaf,
                        guarded,
                        owner_uid=self.owner_uid,
                        required_mode=mode,
                        where=logical,
                        require_single_link=require_single_link,
                    )
                    if guarded.st_size != info.st_size:
                        raise SafePathError(
                            f"rooted guarded append file changed during guard: {logical}"
                        )
                    if rejection:
                        self.assert_root_binding()
                        return self.root.joinpath(*parts), False
                    view = memoryview(content)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise SafePathError(
                                f"short rooted guarded append: {logical}"
                            )
                        view = view[written:]
                    os.fsync(fd)
                    final = os.fstat(fd)
                    _validate_regular(
                        final,
                        owner_uid=self.owner_uid,
                        required_mode=mode,
                        where=logical,
                    )
                    _validate_leaf_binding(
                        parent_fd,
                        leaf,
                        final,
                        owner_uid=self.owner_uid,
                        required_mode=mode,
                        where=logical,
                        require_single_link=require_single_link,
                    )
                    if final.st_size != info.st_size + len(content):
                        raise SafePathError(
                            f"rooted guarded append file changed during append: {logical}"
                        )
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            self.assert_root_binding()
            return self.root.joinpath(*parts), True
        finally:
            os.close(parent_fd)

    def append_regular(
        self,
        relative: Path | str,
        content: bytes,
        *,
        directory_modes: Sequence[int | None],
        file_mode: int = 0o600,
        require_single_link: bool = True,
    ) -> Path:
        """Append bytes durably to one rooted regular file without following links."""

        if not isinstance(content, bytes) or not content:
            raise SafePathError("appended content must be non-empty bytes")
        if not isinstance(require_single_link, bool):
            raise SafePathError("require_single_link must be bool")
        mode = _validate_mode(file_mode, "file")
        assert mode is not None
        self.assert_root_binding()
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=True,
        )
        leaf = parts[-1]
        logical = "/".join(parts)
        created = False
        try:
            flags = (
                os.O_WRONLY
                | os.O_APPEND
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                fd = os.open(
                    leaf,
                    flags | os.O_CREAT | os.O_EXCL,
                    mode,
                    dir_fd=parent_fd,
                )
                created = True
            except FileExistsError:
                try:
                    fd = os.open(leaf, flags, dir_fd=parent_fd)
                except OSError as exc:
                    raise SafePathError(
                        f"cannot open rooted append file: {logical}"
                    ) from exc
            except OSError as exc:
                raise SafePathError(
                    f"cannot create rooted append file: {logical}"
                ) from exc
            try:
                if created:
                    os.fchmod(fd, mode)
                    os.fsync(parent_fd)
                initial = os.fstat(fd)
                _validate_regular(
                    initial,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                _validate_leaf_binding(
                    parent_fd,
                    leaf,
                    initial,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                    require_single_link=require_single_link,
                )
                view = memoryview(content)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise SafePathError(f"short rooted append: {logical}")
                    view = view[written:]
                os.fsync(fd)
                final = os.fstat(fd)
                _validate_regular(
                    final,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                _validate_leaf_binding(
                    parent_fd,
                    leaf,
                    final,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                    require_single_link=require_single_link,
                )
            finally:
                os.close(fd)
            self.assert_root_binding()
            return self.root.joinpath(*parts)
        finally:
            os.close(parent_fd)

    @contextmanager
    def exclusive_lock(
        self,
        relative: Path | str,
        *,
        directory_modes: Sequence[int | None],
        file_mode: int = 0o600,
        require_single_link: bool = True,
        create_directories: bool = True,
    ) -> Iterator[None]:
        """Acquire an owner-bound flock beneath the pinned root descriptor."""

        if not isinstance(require_single_link, bool):
            raise SafePathError("require_single_link must be bool")
        if not isinstance(create_directories, bool):
            raise SafePathError("create_directories must be bool")
        self.assert_root_binding()
        mode = _validate_mode(file_mode, "lock file")
        assert mode is not None
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=create_directories,
        )
        leaf = parts[-1]
        logical = "/".join(parts)
        created = False
        try:
            flags = (
                os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                fd = os.open(
                    leaf,
                    flags | os.O_CREAT | os.O_EXCL,
                    mode,
                    dir_fd=parent_fd,
                )
                created = True
            except FileExistsError:
                try:
                    fd = os.open(leaf, flags, dir_fd=parent_fd)
                except OSError as exc:
                    raise SafePathError(f"cannot open rooted lock: {logical}") from exc
            except OSError as exc:
                raise SafePathError(f"cannot create rooted lock: {logical}") from exc
            try:
                if created:
                    os.fchmod(fd, mode)
                    os.fsync(fd)
                    os.fsync(parent_fd)
                opened = os.fstat(fd)
                _validate_regular(
                    opened,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                _validate_leaf_binding(
                    parent_fd,
                    leaf,
                    opened,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                    require_single_link=require_single_link,
                )
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    _validate_leaf_binding(
                        parent_fd,
                        leaf,
                        os.fstat(fd),
                        owner_uid=self.owner_uid,
                        required_mode=mode,
                        where=logical,
                        require_single_link=require_single_link,
                    )
                    self.assert_root_binding()
                    yield
                    _validate_leaf_binding(
                        parent_fd,
                        leaf,
                        os.fstat(fd),
                        owner_uid=self.owner_uid,
                        required_mode=mode,
                        where=logical,
                        require_single_link=require_single_link,
                    )
                    self.assert_root_binding()
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    @contextmanager
    def shared_lock(
        self,
        relative: Path | str,
        *,
        directory_modes: Sequence[int | None],
        file_mode: int = 0o600,
        require_single_link: bool = True,
    ) -> Iterator[None]:
        """Share an existing owner-bound flock without creating read state."""

        if not isinstance(require_single_link, bool):
            raise SafePathError("require_single_link must be bool")
        self.assert_root_binding()
        mode = _validate_mode(file_mode, "lock file")
        assert mode is not None
        parent_fd, parts = self._parent_descriptor(
            relative,
            directory_modes=directory_modes,
            create=False,
        )
        leaf = parts[-1]
        logical = "/".join(parts)
        try:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                fd = os.open(leaf, flags, dir_fd=parent_fd)
            except OSError as exc:
                raise SafePathError(
                    f"cannot open rooted shared lock: {logical}"
                ) from exc
            try:
                opened = os.fstat(fd)
                _validate_regular(
                    opened,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                )
                _validate_leaf_binding(
                    parent_fd,
                    leaf,
                    opened,
                    owner_uid=self.owner_uid,
                    required_mode=mode,
                    where=logical,
                    require_single_link=require_single_link,
                )
                fcntl.flock(fd, fcntl.LOCK_SH)
                try:
                    _validate_leaf_binding(
                        parent_fd,
                        leaf,
                        os.fstat(fd),
                        owner_uid=self.owner_uid,
                        required_mode=mode,
                        where=logical,
                        require_single_link=require_single_link,
                    )
                    self.assert_root_binding()
                    yield
                    _validate_leaf_binding(
                        parent_fd,
                        leaf,
                        os.fstat(fd),
                        owner_uid=self.owner_uid,
                        required_mode=mode,
                        where=logical,
                        require_single_link=require_single_link,
                    )
                    self.assert_root_binding()
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    def list_directory(
        self,
        relative: Path | str,
        *,
        directory_modes: Sequence[int | None],
    ) -> list[str]:
        """List names from a validated directory descriptor, never a pathname alias."""

        self.assert_root_binding()
        parts = _relative_parts(relative, allow_empty=True)
        modes = _directory_modes(directory_modes, count=len(parts))
        directory_fd = self._open_directory_chain(parts, modes, create=False)
        try:
            names = sorted(os.listdir(directory_fd))
            self.assert_root_binding()
            return names
        except OSError as exc:
            raise SafePathError("cannot list rooted directory") from exc
        finally:
            os.close(directory_fd)
