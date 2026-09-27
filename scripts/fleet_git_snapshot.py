"""Bounded raw Git snapshot production, independent of Mission orchestration.

Git processes and temporary blob spools belong to this adapter. Verification
of the emitted bytes lives in fleet_archive_tree and has no process effects.
"""
from __future__ import annotations

import io
import os
from pathlib import Path, PurePosixPath
import selectors
import subprocess
import tarfile
import tempfile
import time

from fleet_archive_tree import (
    ArchiveError, MAX_FILE_BYTES, GIT_MODE_PAX, GIT_OID_PAX,
    _git_oid, _git_name, _hash_git_object, safe_relative,
)


ROOT = Path(__file__).resolve().parents[1]


def _run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
) -> bytes:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            input=input_bytes,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArchiveError(f"cannot run {Path(command[0]).name}: {exc}") from exc
    if result.returncode != 0:
        raise ArchiveError(
            f"{Path(command[0]).name} failed: {result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout


def _run_bounded(
    command: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    max_stdout_bytes: int,
    max_stderr_bytes: int = 1024 * 1024,
    timeout_seconds: float = 120,
) -> bytes:
    """Capture a command incrementally and kill it before output can exceed bounds."""

    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ArchiveError(f"cannot run {Path(command[0]).name}: {exc}") from exc
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout_seconds
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ArchiveError(f"{Path(command[0]).name} timed out")
            events = selector.select(remaining)
            if not events:
                raise ArchiveError(f"{Path(command[0]).name} timed out")
            for key, _ in events:
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = stdout if key.data == "stdout" else stderr
                limit = max_stdout_bytes if key.data == "stdout" else max_stderr_bytes
                if len(target) + len(chunk) > limit:
                    stream = "output" if key.data == "stdout" else "error output"
                    raise ArchiveError(
                        f"{Path(command[0]).name} {stream} exceeds limit"
                    )
                target.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ArchiveError(f"{Path(command[0]).name} timed out")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise ArchiveError(f"{Path(command[0]).name} timed out") from exc
        if returncode != 0:
            raise ArchiveError(
                f"{Path(command[0]).name} failed: "
                + bytes(stderr).decode("utf-8", "replace").strip()
            )
        return bytes(stdout)
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def git_environment() -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_GRAFT_FILE": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "PAGER": "cat",
        }
    )
    return env


def _git_command(repo: Path, *args: str) -> list[str]:
    return [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "submodule.recurse=false",
        "-C",
        str(repo),
        *args,
    ]


def _git_run(
    repo: Path, *args: str, input_bytes: bytes | None = None
) -> bytes:
    return _run(
        _git_command(repo, *args),
        input_bytes=input_bytes,
        env=git_environment(),
    )


def _git_run_bounded(
    repo: Path, *args: str, max_bytes: int = MAX_FILE_BYTES
) -> bytes:
    return _run_bounded(
        _git_command(repo, *args),
        env=git_environment(),
        max_stdout_bytes=max_bytes,
    )


def _git(repo: Path, *args: str) -> str:
    return _git_run(repo, *args).decode("utf-8").strip()


def _raw_tree_records(repo: Path, commit: str, object_format: str) -> list[tuple[str, str, str, str]]:
    content = _git_run_bounded(repo, "ls-tree", "-rz", "--full-tree", commit)
    records: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for raw in content.split(b"\0"):
        if not raw:
            continue
        try:
            metadata, raw_path = raw.split(b"\t", 1)
            raw_mode, raw_type, raw_oid = metadata.split(b" ", 2)
            mode = raw_mode.decode("ascii")
            kind = raw_type.decode("ascii")
            oid = raw_oid.decode("ascii")
            path = raw_path.decode("utf-8", "surrogateescape")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ArchiveError("Git returned a malformed raw tree record") from exc
        safe_relative(path)
        if path in seen:
            raise ArchiveError("Git returned a duplicate raw tree path")
        seen.add(path)
        if (mode, kind) not in {
            ("100644", "blob"),
            ("100755", "blob"),
            ("120000", "blob"),
            ("160000", "commit"),
        }:
            raise ArchiveError(f"unsupported raw Git tree entry: {mode} {kind}")
        records.append((path, mode, kind, _git_oid(oid, object_format, where="tree oid")))
    return records


def _batch_blob_header(
    raw: bytes, object_format: str, *, where: str
) -> tuple[str, int]:
    if not raw.endswith(b"\n") or len(raw) > 256:
        raise ArchiveError(f"{where} returned a malformed blob header")
    fields = raw[:-1].split(b" ")
    if len(fields) != 3:
        raise ArchiveError(f"{where} returned a malformed blob header")
    try:
        oid = fields[0].decode("ascii")
        kind = fields[1].decode("ascii")
        size_text = fields[2].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ArchiveError(f"{where} returned a malformed blob header") from exc
    if kind != "blob" or not size_text.isdecimal():
        raise ArchiveError(f"{where} did not return a Git blob")
    return _git_oid(oid, object_format, where=f"{where} oid"), int(size_text)


def _raw_tree_layout(
    records: list[tuple[str, str, str, str]],
) -> tuple[list[str], int]:
    directories: set[str] = set()
    # tarfile pads every archive to a record. Account for leaf and implicit
    # directory metadata before blob materialization so deep paths and
    # gitlink-only trees cannot allocate past the final-tree entry limit.
    estimated_size = tarfile.RECORDSIZE
    for path, _, _, _ in records:
        path_size = len(_git_name(path))
        estimated_size += 1536
        if path_size > 100:
            estimated_size += ((path_size - 100 + 511) // 512) * 512
        if estimated_size > MAX_FILE_BYTES:
            raise ArchiveError("generated final tree exceeds per-file limit")
        prefix = ""
        for part in PurePosixPath(path).parts[:-1]:
            prefix = part if not prefix else f"{prefix}/{part}"
            if prefix in directories:
                continue
            directories.add(prefix)
            prefix_size = len(_git_name(prefix))
            estimated_size += 512
            if prefix_size > 100:
                estimated_size += 1024
                estimated_size += ((prefix_size - 100 + 511) // 512) * 512
            if estimated_size > MAX_FILE_BYTES:
                raise ArchiveError("generated final tree exceeds per-file limit")
    return (
        sorted(
            directories,
            key=lambda value: (len(PurePosixPath(value).parts), _git_name(value)),
        ),
        estimated_size,
    )


def _raw_blob_data(
    repo: Path,
    records: list[tuple[str, str, str, str]],
    object_format: str,
    *,
    estimated_size: int | None = None,
) -> dict[str, bytes]:
    if estimated_size is None:
        _, estimated_size = _raw_tree_layout(records)
    oids = list(dict.fromkeys(oid for _, mode, _, oid in records if mode != "160000"))
    if not oids:
        return {}
    request = b"".join(oid.encode("ascii") + b"\n" for oid in oids)
    checked = _git_run(
        repo,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_bytes=request,
    ).splitlines(keepends=True)
    if len(checked) != len(oids):
        raise ArchiveError("Git batch-check returned an incomplete blob set")
    sizes: dict[str, int] = {}
    for expected_oid, header in zip(oids, checked, strict=True):
        oid, size = _batch_blob_header(header, object_format, where="Git batch-check")
        if oid != expected_oid:
            raise ArchiveError("Git batch-check returned blobs out of order")
        if size > MAX_FILE_BYTES:
            raise ArchiveError("raw Git blob exceeds per-file limit")
        sizes[oid] = size
    for _, mode, _, oid in records:
        # Count symlink target bytes conservatively too: long targets become a
        # PAX linkpath and must not bypass the aggregate preflight merely
        # because tar stores them as metadata.
        if mode in {"100644", "100755", "120000"}:
            estimated_size += ((sizes[oid] + 511) // 512) * 512
            if estimated_size > MAX_FILE_BYTES:
                raise ArchiveError("generated final tree exceeds per-file limit")

    with tempfile.TemporaryFile() as spool:
        try:
            result = subprocess.run(
                _git_command(repo, "cat-file", "--batch"),
                input=request,
                env=git_environment(),
                stdout=spool,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArchiveError(f"cannot read raw Git blobs: {exc}") from exc
        if result.returncode != 0:
            raise ArchiveError(
                "git failed: "
                + result.stderr.decode("utf-8", "replace").strip()
            )
        spool.seek(0)
        blobs: dict[str, bytes] = {}
        for expected_oid in oids:
            oid, size = _batch_blob_header(
                spool.readline(257), object_format, where="Git batch"
            )
            if oid != expected_oid or size != sizes[expected_oid]:
                raise ArchiveError("Git batch blob differs from its size preflight")
            data = spool.read(size)
            if len(data) != size or spool.read(1) != b"\n":
                raise ArchiveError("Git batch returned truncated blob content")
            if _hash_git_object("blob", data, object_format).hex() != oid:
                raise ArchiveError("raw Git blob content does not match its object id")
            blobs[oid] = data
        if spool.read(1):
            raise ArchiveError("Git batch returned unexpected trailing content")
    return blobs


def _tar_info(name: str, *, mode: int, entry_type: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = entry_type
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def raw_tree_tar(repo: Path, commit: str, object_format: str) -> bytes:
    records = _raw_tree_records(repo, commit, object_format)
    directories, estimated_size = _raw_tree_layout(records)
    blobs = _raw_blob_data(
        repo,
        records,
        object_format,
        estimated_size=estimated_size,
    )
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for directory in directories:
            archive.addfile(
                _tar_info(directory + "/", mode=0o755, entry_type=tarfile.DIRTYPE)
            )
        for path, mode, kind, oid in records:
            if mode == "160000":
                info = _tar_info(path + "/", mode=0o755, entry_type=tarfile.DIRTYPE)
                info.pax_headers = {GIT_MODE_PAX: mode, GIT_OID_PAX: oid}
                archive.addfile(info)
                continue
            data = blobs[oid]
            entry_type = tarfile.SYMTYPE if mode == "120000" else tarfile.REGTYPE
            info = _tar_info(
                path,
                mode=0o755 if mode == "100755" else 0o644,
                entry_type=entry_type,
            )
            info.pax_headers = {GIT_MODE_PAX: mode, GIT_OID_PAX: oid}
            if mode == "120000":
                if b"\0" in data:
                    raise ArchiveError("raw Git symlink target contains NUL")
                info.linkname = data.decode("utf-8", "surrogateescape")
            else:
                info.size = len(data)
            archive.addfile(info, None if mode == "120000" else io.BytesIO(data))
    content = output.getvalue()
    if len(content) > MAX_FILE_BYTES:
        raise ArchiveError("generated final tree exceeds per-file limit")
    return content
