#!/usr/bin/env python3
"""Exact local process and Unix-socket identities for Fleet Control."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import os
from pathlib import Path
import stat
import sys
from typing import Any, Iterator


class RuntimeIdentityError(RuntimeError):
    """A process or socket binding cannot be proven exactly."""


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _exact_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeIdentityError(f"invalid {field}")
    return value


def directory_identity_from_stat(info: os.stat_result) -> dict[str, int]:
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeIdentityError("control socket root is not a directory")
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "uid": int(info.st_uid),
        "mode": stat.S_IMODE(info.st_mode),
    }


def validate_directory_identity(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"device", "inode", "uid", "mode"}:
        raise RuntimeIdentityError("invalid control socket root identity")
    identity = {
        "device": _exact_int(value["device"], "socket root device"),
        "inode": _exact_int(value["inode"], "socket root inode", minimum=1),
        "uid": _exact_int(value["uid"], "socket root uid"),
        "mode": _exact_int(value["mode"], "socket root mode"),
    }
    if identity["mode"] != 0o700 or identity["uid"] != os.geteuid():
        raise RuntimeIdentityError("control socket root owner/mode mismatch")
    return identity


def socket_identity_from_stat(info: os.stat_result) -> dict[str, int]:
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeIdentityError("control endpoint is not a Unix socket")
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "uid": int(info.st_uid),
        "mode": stat.S_IMODE(info.st_mode),
    }


def _validate_socket_binding(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {
        "device",
        "inode",
        "uid",
        "mode",
    }:
        raise RuntimeIdentityError("invalid control endpoint binding")
    identity = {
        "device": _exact_int(value["device"], "endpoint device"),
        "inode": _exact_int(value["inode"], "endpoint inode", minimum=1),
        "uid": _exact_int(value["uid"], "endpoint uid"),
        "mode": _exact_int(value["mode"], "endpoint mode"),
    }
    if identity["mode"] > 0o777 or identity["uid"] != os.geteuid():
        raise RuntimeIdentityError("control endpoint owner/mode mismatch")
    return identity


def validate_socket_identity(value: Any) -> dict[str, int]:
    identity = _validate_socket_binding(value)
    if identity["mode"] != 0o600:
        raise RuntimeIdentityError("control endpoint owner/mode mismatch")
    return identity


@contextmanager
def open_socket_root(
    path: Path, expected: dict[str, int] | None = None
) -> Iterator[tuple[int, dict[str, int]]]:
    """Pin an exact owner-private directory and verify its pathname binding."""

    try:
        if not path.is_absolute():
            raise RuntimeIdentityError("control socket root must be absolute")
        before = path.lstat()
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, RuntimeIdentityError):
            raise
        raise RuntimeIdentityError("cannot open control socket root") from exc
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        before_identity = directory_identity_from_stat(before)
        opened_identity = directory_identity_from_stat(opened)
        current_identity = directory_identity_from_stat(current)
        if before_identity != opened_identity or opened_identity != current_identity:
            raise RuntimeIdentityError("control socket root binding changed")
        if opened_identity["uid"] != os.geteuid() or opened_identity["mode"] != 0o700:
            raise RuntimeIdentityError("control socket root owner/mode mismatch")
        if expected is not None and opened_identity != validate_directory_identity(
            expected
        ):
            raise RuntimeIdentityError("control socket root durable identity mismatch")
        yield descriptor, opened_identity
        current = path.lstat()
        if directory_identity_from_stat(current) != opened_identity:
            raise RuntimeIdentityError("control socket root binding changed")
        if directory_identity_from_stat(os.fstat(descriptor)) != opened_identity:
            raise RuntimeIdentityError("control socket root descriptor changed")
    finally:
        os.close(descriptor)


def _validate_socket_leaf(leaf: str) -> None:
    if (
        not isinstance(leaf, str)
        or not leaf
        or leaf in {".", ".."}
        or "/" in leaf
        or "\0" in leaf
    ):
        raise RuntimeIdentityError("invalid control endpoint leaf")


def socket_binding_at(root_fd: int, leaf: str) -> dict[str, int]:
    """Inspect an owner-bound socket leaf before its final mode is applied."""

    _validate_socket_leaf(leaf)
    try:
        info = os.stat(leaf, dir_fd=root_fd, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeIdentityError("cannot inspect control endpoint") from exc
    identity = socket_identity_from_stat(info)
    if identity["uid"] != os.geteuid():
        raise RuntimeIdentityError("control endpoint owner mismatch")
    return identity


def chmod_socket_at(
    root_fd: int, leaf: str, expected: dict[str, int]
) -> dict[str, int]:
    """Apply mode 0600 to one exact socket before it begins listening."""

    expected_identity = _validate_socket_binding(expected)
    if socket_binding_at(root_fd, leaf) != expected_identity:
        raise RuntimeIdentityError("control endpoint binding changed before chmod")
    try:
        os.chmod(leaf, 0o600, dir_fd=root_fd, follow_symlinks=False)
    except (NotImplementedError, OSError) as exc:
        raise RuntimeIdentityError("cannot secure control endpoint mode") from exc
    current = socket_binding_at(root_fd, leaf)
    if any(
        current[field] != expected_identity[field]
        for field in ("device", "inode", "uid")
    ):
        raise RuntimeIdentityError("control endpoint binding changed during chmod")
    if current["mode"] != 0o600:
        raise RuntimeIdentityError("control endpoint owner/mode mismatch")
    return current


def socket_identity_at(root_fd: int, leaf: str) -> dict[str, int]:
    identity = socket_binding_at(root_fd, leaf)
    if identity["mode"] != 0o600:
        raise RuntimeIdentityError("control endpoint owner/mode mismatch")
    return identity


def assert_socket_at(root_fd: int, leaf: str, expected: dict[str, int]) -> None:
    if socket_identity_at(root_fd, leaf) != validate_socket_identity(expected):
        raise RuntimeIdentityError("control endpoint durable identity mismatch")


def assert_absent_at(root_fd: int, leaf: str) -> None:
    _validate_socket_leaf(leaf)
    try:
        os.stat(leaf, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeIdentityError("cannot inspect control endpoint") from exc
    raise RuntimeIdentityError("control endpoint must be absent")


def unlink_socket_at(root_fd: int, leaf: str, expected: dict[str, int]) -> None:
    assert_socket_at(root_fd, leaf, expected)
    try:
        os.unlink(leaf, dir_fd=root_fd)
        os.fsync(root_fd)
    except OSError as exc:
        raise RuntimeIdentityError("cannot remove bound control endpoint") from exc
    assert_absent_at(root_fd, leaf)


def unlink_socket_binding_at(root_fd: int, leaf: str, expected: dict[str, int]) -> None:
    """Remove an exact just-bound socket, including pre-0600 cleanup."""

    expected_identity = _validate_socket_binding(expected)
    if socket_binding_at(root_fd, leaf) != expected_identity:
        raise RuntimeIdentityError("control endpoint cleanup binding mismatch")
    try:
        os.unlink(leaf, dir_fd=root_fd)
        os.fsync(root_fd)
    except OSError as exc:
        raise RuntimeIdentityError("cannot remove bound control endpoint") from exc
    assert_absent_at(root_fd, leaf)


def _rename_socket_noreplace(
    root_fd: int,
    source: str,
    destination: str,
) -> None:
    """Rename one leaf inside a pinned directory without replacing a target."""

    library = ctypes.CDLL(None, use_errno=True)
    encoded_source = os.fsencode(source)
    encoded_destination = os.fsencode(destination)
    if sys.platform == "darwin":
        rename = getattr(library, "renameatx_np", None)
        if rename is None:
            raise RuntimeIdentityError("exclusive socket publication is unavailable")
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        # sys/stdio.h: RENAME_EXCL.  Descriptor pinning and the source socket
        # identity check below provide the no-follow boundary for both leaves.
        result = rename(
            root_fd,
            encoded_source,
            root_fd,
            encoded_destination,
            0x00000004,
        )
    elif sys.platform.startswith("linux"):
        rename = getattr(library, "renameat2", None)
        if rename is None:
            raise RuntimeIdentityError("exclusive socket publication is unavailable")
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        # linux/fs.h: RENAME_NOREPLACE.
        result = rename(
            root_fd,
            encoded_source,
            root_fd,
            encoded_destination,
            1,
        )
    else:
        raise RuntimeIdentityError("exclusive socket publication is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise RuntimeIdentityError("control endpoint publication target exists")
        raise RuntimeIdentityError("cannot publish exact control endpoint")


def publish_socket_at(
    root_fd: int,
    staging_leaf: str,
    final_leaf: str,
    expected: dict[str, int],
) -> None:
    """Publish one staged socket inode at its final leaf, exclusively."""

    assert_socket_at(root_fd, staging_leaf, expected)
    assert_absent_at(root_fd, final_leaf)
    _rename_socket_noreplace(root_fd, staging_leaf, final_leaf)
    os.fsync(root_fd)
    assert_absent_at(root_fd, staging_leaf)
    assert_socket_at(root_fd, final_leaf, expected)


def socket_location_at(
    root_fd: int,
    staging_leaf: str,
    final_leaf: str,
    expected: dict[str, int],
    *,
    allow_absent: bool = False,
) -> str:
    """Return the sole exact location of a journal-bound socket inode."""

    expected = validate_socket_identity(expected)

    def optional_identity(leaf: str) -> dict[str, int] | None:
        if (
            not isinstance(leaf, str)
            or not leaf
            or leaf in {".", ".."}
            or "/" in leaf
            or "\0" in leaf
        ):
            raise RuntimeIdentityError("invalid control endpoint leaf")
        try:
            info = os.stat(leaf, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RuntimeIdentityError("cannot inspect control endpoint") from exc
        identity = socket_identity_from_stat(info)
        if identity["uid"] != os.geteuid() or identity["mode"] != 0o600:
            raise RuntimeIdentityError("control endpoint owner/mode mismatch")
        if identity != expected:
            raise RuntimeIdentityError("control endpoint durable identity mismatch")
        return identity

    staged = optional_identity(staging_leaf)
    final = optional_identity(final_leaf)
    if staged is not None and final is not None:
        raise RuntimeIdentityError(
            "control endpoint exists at staging and final leaves"
        )
    if staged is not None:
        return "staging"
    if final is not None:
        return "final"
    if allow_absent:
        return "absent"
    raise RuntimeIdentityError("journal-bound control endpoint is absent")


class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def validate_process_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise RuntimeIdentityError("invalid control process identity")
    kind = value["kind"]
    if kind == "darwin-proc-bsdinfo-v1":
        if set(value) != {"kind", "uid", "start_seconds", "start_microseconds"}:
            raise RuntimeIdentityError("invalid Darwin control process identity")
        identity: dict[str, Any] = {
            "kind": kind,
            "uid": _exact_int(value["uid"], "process uid"),
            "start_seconds": _exact_int(
                value["start_seconds"], "process start seconds"
            ),
            "start_microseconds": _exact_int(
                value["start_microseconds"], "process start microseconds"
            ),
        }
        if identity["start_microseconds"] >= 1_000_000:
            raise RuntimeIdentityError("invalid process start microseconds")
    elif kind == "linux-proc-stat-v1":
        if set(value) != {"kind", "uid", "boot_id", "start_ticks"}:
            raise RuntimeIdentityError("invalid Linux control process identity")
        boot_id = value["boot_id"]
        if not isinstance(boot_id, str) or not boot_id or len(boot_id) > 128:
            raise RuntimeIdentityError("invalid Linux boot identity")
        identity = {
            "kind": kind,
            "uid": _exact_int(value["uid"], "process uid"),
            "boot_id": boot_id,
            "start_ticks": _exact_int(value["start_ticks"], "process start ticks"),
        }
    else:
        raise RuntimeIdentityError("unsupported control process identity kind")
    if identity["uid"] != os.geteuid():
        raise RuntimeIdentityError("control process owner mismatch")
    return identity


def process_observation(pid: int) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(identity, zombie)`` for one exact PID, or ``(None, False)``."""

    pid = _exact_int(pid, "control process pid", minimum=2)
    if sys.platform == "darwin":
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        library.proc_pidinfo.restype = ctypes.c_int
        value = _ProcBSDInfo()
        ctypes.set_errno(0)
        result = library.proc_pidinfo(
            pid, 3, 0, ctypes.byref(value), ctypes.sizeof(value)
        )
        if result <= 0:
            error = ctypes.get_errno()
            if error in {0, errno.ESRCH}:
                return None, False
            raise RuntimeIdentityError("cannot inspect exact Darwin process identity")
        if result != ctypes.sizeof(value) or value.pbi_pid != pid:
            raise RuntimeIdentityError("cannot read exact Darwin process identity")
        identity = validate_process_identity(
            {
                "kind": "darwin-proc-bsdinfo-v1",
                "uid": int(value.pbi_uid),
                "start_seconds": int(value.pbi_start_tvsec),
                "start_microseconds": int(value.pbi_start_tvusec),
            }
        )
        return identity, value.pbi_status == 5
    if sys.platform.startswith("linux"):
        stat_path = Path("/proc") / str(pid) / "stat"
        try:
            info = stat_path.stat()
            raw = stat_path.read_text(encoding="ascii")
            boot_id = (
                Path("/proc/sys/kernel/random/boot_id")
                .read_text(encoding="ascii")
                .strip()
            )
        except FileNotFoundError:
            return None, False
        except OSError as exc:
            raise RuntimeIdentityError(
                "cannot read exact Linux process identity"
            ) from exc
        close = raw.rfind(")")
        fields = raw[close + 2 :].split() if close >= 0 else []
        if len(fields) < 20:
            raise RuntimeIdentityError("invalid Linux process stat record")
        identity = validate_process_identity(
            {
                "kind": "linux-proc-stat-v1",
                "uid": int(info.st_uid),
                "boot_id": boot_id,
                "start_ticks": int(fields[19]),
            }
        )
        return identity, fields[0] == "Z"
    raise RuntimeIdentityError("platform lacks an exact control process identity")
