"""Stage 1 local delivery of an accepted revision (spec E8-E12, E19, §3.3).

The accepted frozen tree is written into a private staging directory beside
its final path, synced, and published with a rename that fails when the
destination exists. The final path is then read back and compared, file by
file, with the frozen tree. Nothing is ever replaced: an existing destination
is either this exact revision (already delivered) or a collision. Only files
are written; no commit or push happens. This module has no Mission state; the
driver records its receipts.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import io
import os
from pathlib import Path
import shutil
import sys
import tarfile

import fleet_archive_tree
import fleet_json

FILE_MODES = {"100644": 0o644, "100755": 0o755}
DIRECTORY_MODE = 0o755


class DeliveryError(ValueError):
    """The delivery cannot proceed; nothing was published."""


class PrimitiveUnavailable(DeliveryError):
    pass


def final_path(root, mission_id, ordinal, tree_sha):
    return Path(root) / mission_id / f"{ordinal}-{tree_sha}"


def staging_path(root, mission_id, ordinal, tree_sha):
    return Path(root) / mission_id / f".staging-{ordinal}-{tree_sha}"


def check_root(root, runs_dir):
    """Resolve the frozen root again at delivery time (the S1 admission was earlier)."""
    declared = Path(root)
    resolved = declared.resolve(strict=False)
    if resolved != declared:
        raise DeliveryError("delivery root no longer resolves to itself")
    runs = Path(runs_dir).resolve(strict=False)
    if resolved == runs or runs in resolved.parents or resolved in runs.parents:
        raise DeliveryError("delivery root must lie outside the runs directory")
    return resolved


def tree_files(tree):
    """Regular files of a frozen tree tar as {relative path: (mode, bytes)}."""
    files = {}
    try:
        archive = tarfile.open(fileobj=io.BytesIO(tree), mode="r:")
    except tarfile.TarError as exc:
        raise DeliveryError("frozen tree is not a valid tar") from exc
    with archive:
        for member in archive.getmembers():
            try:
                path = str(fleet_archive_tree.safe_relative(member.name.rstrip("/")))
            except fleet_archive_tree.ArchiveError as exc:
                raise DeliveryError("frozen tree contains an unsafe path") from exc
            if member.isdir():
                continue
            mode = member.pax_headers.get(fleet_archive_tree.GIT_MODE_PAX)
            if not member.isreg() or mode not in FILE_MODES:
                raise DeliveryError("frozen tree contains an entry that is not a regular file")
            if path in files:
                raise DeliveryError("frozen tree contains duplicate paths")
            files[path] = (FILE_MODES[mode], archive.extractfile(member).read())
    return files


def manifest(files):
    return {path: {"mode": oct(mode), "sha256": hashlib.sha256(raw).hexdigest()}
            for path, (mode, raw) in sorted(files.items())}


def read_back(path):
    """Manifest of a published directory, without following symbolic links."""
    observed = {}
    base = Path(path)
    for directory, names, filenames in os.walk(base, followlinks=False):
        for name in names + filenames:
            entry = Path(directory) / name
            info = entry.lstat()
            if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
                raise DeliveryError("published path contains an entry that is not a file or directory")
            if entry.is_file():
                observed[str(entry.relative_to(base))] = (info.st_mode & 0o777, entry.read_bytes())
    return manifest(observed)


def _fsync(path, *, directory=False):
    descriptor = os.open(path, os.O_RDONLY | (os.O_DIRECTORY if directory else 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_noreplace(source, destination):
    """Atomic rename that fails with EEXIST when ``destination`` exists."""
    libc = ctypes.CDLL(None, use_errno=True)
    old, new = os.fsencode(source), os.fsencode(destination)
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        result = libc.renamex_np(old, new, ctypes.c_uint(0x00000004))  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        result = libc.renameat2(-100, old, -100, new, ctypes.c_uint(1))  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise PrimitiveUnavailable("no rename-without-replacement primitive on this platform")
    if result != 0:
        error = ctypes.get_errno()
        if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise PrimitiveUnavailable("the filesystem rejects rename without replacement")
        raise OSError(error, os.strerror(error), str(destination))


def _remove_own_staging(staging):
    if staging.is_symlink():
        raise DeliveryError("staging path was replaced by a symbolic link")
    if staging.exists():
        shutil.rmtree(staging)


def _receipt(status, final, expected, *, written, reason=None):
    value = {"status": status, "path": str(final), "files": len(expected),
             "manifest_sha256": hashlib.sha256(fleet_json.canonical_bytes(expected)).hexdigest(),
             "written": written}
    if reason is not None:
        value["reason"] = reason
    return value


def _existing(final, expected):
    """Classify an existing final path: this exact revision or a collision."""
    try:
        identical = not final.is_symlink() and final.is_dir() and read_back(final) == expected
    except (DeliveryError, OSError):
        identical = False
    if identical:
        return _receipt("delivered", final, expected, written=False)
    return _receipt("collision", final, expected, written=False, reason="delivery_collision")


def deliver(root, mission_id, ordinal, tree_sha, tree, *, runs_dir, before_publish=None):
    """Publish the accepted tree once; never replace an existing destination."""
    root = check_root(root, runs_dir)
    files = tree_files(tree)
    expected = manifest(files)
    final, staging = final_path(root, mission_id, ordinal, tree_sha), staging_path(root, mission_id, ordinal, tree_sha)
    if final.exists() or final.is_symlink():
        return _existing(final, expected)
    final.parent.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    _remove_own_staging(staging)
    staging.mkdir(mode=0o700)
    try:
        for path, (mode, raw) in files.items():
            target = staging / path
            target.parent.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            try:
                os.write(descriptor, raw)
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for directory, _names, _files in os.walk(staging):
            _fsync(directory, directory=True)
        staging.chmod(DIRECTORY_MODE)
        if before_publish is not None:
            before_publish(final)
        try:
            _rename_noreplace(staging, final)
        except PrimitiveUnavailable as exc:
            return _receipt("blocked", final, expected, written=False, reason="delivery_primitive_unavailable")
        except OSError as exc:
            if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                return _existing(final, expected)
            raise
    finally:
        _remove_own_staging(staging)
    _fsync(final.parent, directory=True)
    if read_back(final) != expected:
        return _receipt("indeterminate", final, expected, written=True, reason="published_content_differs")
    return _receipt("delivered", final, expected, written=True)


def reconcile(root, mission_id, ordinal, tree_sha, tree, *, runs_dir):
    """After a restart that left ``delivery_started`` without a receipt (E12)."""
    root = check_root(root, runs_dir)
    expected = manifest(tree_files(tree))
    final, staging = final_path(root, mission_id, ordinal, tree_sha), staging_path(root, mission_id, ordinal, tree_sha)
    if final.exists() or final.is_symlink():
        existing = _existing(final, expected)
        if existing["status"] == "delivered":
            return existing
        return _receipt("indeterminate", final, expected, written=False, reason="destination_changed_after_start")
    _remove_own_staging(staging)
    return _receipt("retry", final, expected, written=False)
