"""Pure archive path and raw Git tree verification.

This module consumes bytes only. It never imports Mission state, runtimes,
providers, subprocess or the legacy archive builder. Historical callers retain
aliases in fleet_archive; new consumers use the public functions here.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import PurePosixPath
import re
import tarfile
from typing import Any


MAX_FILE_BYTES = 32 * 1024 * 1024
OBJECT_FORMATS = {
    "sha1": (hashlib.sha1, 40),
    "sha256": (hashlib.sha256, 64),
}
GIT_MODE_PAX = "FLEET.git.mode"
GIT_OID_PAX = "FLEET.git.oid"


class ArchiveError(RuntimeError):
    """Mission archive content or verification is unsafe or inconsistent."""


def safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or any(not part for part in path.parts):
        raise ArchiveError(f"unsafe archive path: {value}")
    return path


def _object_format(value: str) -> tuple[Any, int]:
    try:
        return OBJECT_FORMATS[value]
    except KeyError as exc:
        raise ArchiveError(f"unsupported Git object format: {value}") from exc


def _git_oid(value: str, object_format: str, *, where: str) -> str:
    _, width = _object_format(object_format)
    if not re.fullmatch(rf"[0-9a-f]{{{width}}}", value):
        raise ArchiveError(f"{where} is not a {object_format} object id")
    return value


def _hash_git_object(kind: str, content: bytes, object_format: str) -> bytes:
    hasher, _ = _object_format(object_format)
    return hasher(f"{kind} {len(content)}\0".encode("ascii") + content).digest()


def _git_name(value: str) -> bytes:
    try:
        return value.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError as exc:
        raise ArchiveError("final tree contains an unencodable Git path") from exc


def _is_structural_empty_tar(content: bytes) -> bool:
    if not content or len(content) % 512 != 0:
        return False
    if content == b"\0" * len(content):
        return True
    header = content[:512]
    if header[:100].rstrip(b"\0") != b"pax_global_header" or header[156:157] != b"g":
        return False
    try:
        size = int(header[124:136].rstrip(b"\0 ") or b"0", 8)
        stored_checksum = int(header[148:156].rstrip(b"\0 ") or b"0", 8)
    except ValueError:
        return False
    checksum_header = bytearray(header)
    checksum_header[148:156] = b" " * 8
    if sum(checksum_header) != stored_checksum:
        return False
    end = 512 + ((size + 511) // 512) * 512
    return end <= len(content) and content[end:] == b"\0" * (len(content) - end)


def tree_hash_from_tar(content: bytes, object_format: str = "sha1") -> str:
    # git archive represents an empty tree as a standards-compliant sequence of
    # zero blocks. Python 3.14's tarfile rejects that representation before it
    # can expose an empty member list, so recognize only the exact structural
    # empty-tar case and reproduce Git's canonical empty-tree object ID.
    if _is_structural_empty_tar(content):
        return _hash_git_object("tree", b"", object_format).hex()
    nodes: dict[tuple[str, ...], list[tuple[str, str, bytes]]] = {(): []}
    seen: set[tuple[str, ...]] = set()
    try:
        archive = tarfile.open(fileobj=io.BytesIO(content), mode="r:")
    except tarfile.TarError as exc:
        raise ArchiveError("final-tree.tar is invalid") from exc
    with archive:
        for member in archive.getmembers():
            path = safe_relative(member.name.rstrip("/"))
            parts = tuple(path.parts)
            if parts in seen:
                raise ArchiveError("final tree contains duplicate paths")
            seen.add(parts)
            for index in range(1, len(parts)):
                nodes.setdefault(parts[:index], [])
            git_mode = member.pax_headers.get(GIT_MODE_PAX)
            git_oid = member.pax_headers.get(GIT_OID_PAX)
            if (git_mode is None) != (git_oid is None):
                raise ArchiveError("final tree contains an incomplete raw Git binding")
            if git_mode == "160000":
                if not member.isdir():
                    raise ArchiveError("final tree gitlink is not a directory marker")
                oid = bytes.fromhex(
                    _git_oid(str(git_oid), object_format, where="gitlink oid")
                )
                parent, name = parts[:-1], parts[-1]
                nodes.setdefault(parent, []).append((name, git_mode, oid))
                continue
            if member.isdir():
                if git_mode is not None:
                    raise ArchiveError("final tree directory has a leaf Git binding")
                nodes.setdefault(parts, [])
                continue
            parent, name = parts[:-1], parts[-1]
            if member.isreg():
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ArchiveError("final tree regular file has no content")
                data = extracted.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise ArchiveError("final tree file exceeds limit")
                mode = "100755" if member.mode & 0o111 else "100644"
            elif member.issym():
                data = _git_name(member.linkname)
                mode = "120000"
            else:
                raise ArchiveError("final tree contains an unsupported special entry")
            oid = _hash_git_object("blob", data, object_format)
            if git_mode is not None:
                if git_mode != mode:
                    raise ArchiveError("final tree raw Git mode does not match tar entry")
                if _git_oid(str(git_oid), object_format, where="blob oid") != oid.hex():
                    raise ArchiveError("final tree raw Git oid does not match tar content")
            nodes.setdefault(parent, []).append((name, mode, oid))

    hashes: dict[tuple[str, ...], bytes] = {}
    for directory in sorted(nodes, key=len, reverse=True):
        entries = list(nodes[directory])
        children = {
            key[len(directory)]
            for key in nodes
            if len(key) == len(directory) + 1 and key[:-1] == directory
        }
        for name in children:
            entries.append((name, "40000", hashes[directory + (name,)]))
        names: set[bytes] = set()
        for name, _, _ in entries:
            encoded = _git_name(name)
            if encoded in names:
                raise ArchiveError("final tree contains duplicate entry names")
            names.add(encoded)
        entries.sort(
            key=lambda item: _git_name(item[0])
            + (b"/" if item[1] == "40000" else b"")
        )
        body = b"".join(
            mode.encode("ascii") + b" " + _git_name(name) + b"\0" + object_id
            for name, mode, object_id in entries
        )
        hashes[directory] = _hash_git_object("tree", body, object_format)
    return hashes[()].hex()
