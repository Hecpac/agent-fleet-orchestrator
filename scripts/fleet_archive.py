#!/usr/bin/env python3
"""Create and verify portable content-addressed Mission archives."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from typing import Any
import uuid

import fleet_audit_client
import fleet_mission
import fleet_mission_state as mission_state


ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
SECRET_AUDIT_FILES = {
    "control-hmac.key",
    "audit-signing-private.pem",
    "service.stdout.log",
    "service.stderr.log",
}
SENSITIVE_PARTS = {
    "objective.txt", "prompts", "tasks", "results", "artifacts", "payloads",
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40,64}$")


class ArchiveError(RuntimeError):
    """Mission archive content or verification is unsafe or inconsistent."""


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or any(not part for part in path.parts):
        raise ArchiveError(f"unsafe archive path: {value}")
    return path


def _manifest(path: Path) -> dict[str, str]:
    try:
        return dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
    except OSError as exc:
        raise ArchiveError("cannot read fleet manifest") from exc


def _run(command: list[str], *, cwd: Path = ROOT) -> bytes:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
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


def _write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    mission_state.atomic_write(path, content, mode=mode)


def _regular_bytes(path: Path) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ArchiveError(f"archive source is missing: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ArchiveError(f"archive source must be a regular non-symlink file: {path}")
    if info.st_size > MAX_FILE_BYTES:
        raise ArchiveError(f"archive source exceeds per-file limit: {path}")
    return path.read_bytes()


def _iter_regular_tree(root: Path) -> list[tuple[PurePosixPath, Path]]:
    if root.is_symlink() or not root.is_dir():
        raise ArchiveError(f"archive source tree is unsafe: {root}")
    result: list[tuple[PurePosixPath, Path]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in list(directories):
            path = base / name
            if path.is_symlink():
                raise ArchiveError(f"archive source directory is a symlink: {path}")
        for name in files:
            path = base / name
            relative = PurePosixPath(path.relative_to(root).as_posix())
            _regular_bytes(path)
            result.append((relative, path))
    return sorted(result, key=lambda item: item[0].as_posix())


def _sensitive(logical: PurePosixPath) -> bool:
    return any(part in SENSITIVE_PARTS for part in logical.parts)


def _git(repo: Path, *args: str) -> str:
    return _run(["git", "-C", str(repo), *args]).decode("utf-8").strip()


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


def _tree_hash_from_tar(content: bytes) -> str:
    # git archive represents an empty tree as a standards-compliant sequence of
    # zero blocks. Python 3.14's tarfile rejects that representation before it
    # can expose an empty member list, so recognize only the exact structural
    # empty-tar case and reproduce Git's canonical empty-tree object ID.
    if _is_structural_empty_tar(content):
        body = b""
        return hashlib.sha1(f"tree {len(body)}\0".encode("ascii") + body).hexdigest()
    nodes: dict[tuple[str, ...], list[tuple[str, str, bytes]]] = {(): []}
    seen: set[tuple[str, ...]] = set()
    try:
        archive = tarfile.open(fileobj=io.BytesIO(content), mode="r:")
    except tarfile.TarError as exc:
        raise ArchiveError("final-tree.tar is invalid") from exc
    with archive:
        for member in archive.getmembers():
            path = _safe_relative(member.name.rstrip("/"))
            parts = tuple(path.parts)
            if parts in seen:
                raise ArchiveError("final tree contains duplicate paths")
            seen.add(parts)
            for index in range(1, len(parts)):
                nodes.setdefault(parts[:index], [])
            if member.isdir():
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
                target = member.linkname
                target_path = PurePosixPath(target)
                if target_path.is_absolute() or ".." in target_path.parts:
                    raise ArchiveError("final tree contains an escaping symlink")
                data = target.encode("utf-8")
                mode = "120000"
            else:
                raise ArchiveError("final tree contains an unsupported special entry")
            header = f"blob {len(data)}\0".encode("ascii")
            nodes.setdefault(parent, []).append((name, mode, hashlib.sha1(header + data).digest()))

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
        entries.sort(key=lambda item: item[0] + ("/" if item[1] == "40000" else ""))
        body = b"".join(
            mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + object_id
            for name, mode, object_id in entries
        )
        hashes[directory] = hashlib.sha1(f"tree {len(body)}\0".encode("ascii") + body).digest()
    return hashes[()].hex()


class ArchiveBuilder:
    def __init__(self, runs_dir: Path, mission_id: str, output: Path | None = None) -> None:
        self.runs_dir = runs_dir.resolve()
        self.mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
        self.root = mission_state.mission_root(self.runs_dir, self.mission_id)
        self.output = (output or self.root / "archive").resolve()
        self.state = fleet_mission.load_state(self.runs_dir, self.mission_id)
        self.compiled = fleet_mission.validate_compiled(
            json.loads((self.root / "compiled-workflow.json").read_text(encoding="utf-8"))
        )
        self.policy = self.compiled["workflow"]["archive"]["content_policy"]
        self.entries: list[dict[str, Any]] = []
        self.omissions: list[dict[str, str]] = []
        self.total = 0
        self.seen: set[str] = set()
        self.physical: set[str] = set()

    def _validate_full_archive_approval(self) -> Path:
        path = self.root / "archive-approval.json"
        try:
            info = path.lstat()
            value = json.loads(_regular_bytes(path))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArchiveError(
                "full archive is forbidden for credentials/private_data without archive approval"
            ) from exc
        required = {
            "schema_version", "mission_id", "approval_id", "idempotency_key",
            "workflow_digest", "scope", "content_policy", "risk_categories",
            "expires_at", "approved_by_sha256", "decision",
        }
        if set(value) != required or any(
            (
                value["schema_version"] != 1,
                value["mission_id"] != self.mission_id,
                value["workflow_digest"] != self.state["workflow_digest"],
                value["scope"] != self.state["target_repo"],
                value["content_policy"] != "full",
                value["risk_categories"]
                != sorted(set(self.state["risk_categories"]) & {"credentials", "private_data"}),
                value["decision"] != "approved",
                not mission_state.SHA256.fullmatch(str(value["approved_by_sha256"])),
                info.st_uid != os.geteuid(),
                stat.S_IMODE(info.st_mode) != 0o600,
            )
        ):
            raise ArchiveError("sensitive full-archive approval is invalid")
        try:
            mission_state.normalize_uuid(value["approval_id"], "archive approval_id")
            expires = datetime.fromisoformat(str(value["expires_at"]).replace("Z", "+00:00"))
        except (mission_state.MissionStateError, ValueError) as exc:
            raise ArchiveError("sensitive full-archive approval is invalid") from exc
        if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            raise ArchiveError("sensitive full-archive approval is expired")
        return path

    def _add_bytes(
        self,
        stage: Path,
        logical: PurePosixPath,
        content: bytes,
        *,
        sensitive: bool = False,
        force_full: bool = False,
    ) -> None:
        name = logical.as_posix()
        _safe_relative(name)
        if name in self.seen:
            raise ArchiveError(f"duplicate archive logical path: {name}")
        self.seen.add(name)
        original_sha = hashlib.sha256(content).hexdigest()
        original_size = len(content)
        if original_size > MAX_FILE_BYTES:
            raise ArchiveError(f"archive entry exceeds per-file limit: {name}")
        selected = "full" if force_full or not sensitive else self.policy
        if selected == "hash-only":
            self.entries.append(
                {"path": name, "sha256": original_sha, "size": original_size, "policy": "hash-only"}
            )
            self.omissions.append({"path": name, "reason": "content policy hash-only"})
            return
        if selected == "redacted":
            redacted_name = PurePosixPath("redacted") / PurePosixPath(name + ".json")
            placeholder = mission_state.canonical_bytes(
                {
                    "schema_version": 1,
                    "original_path": name,
                    "original_sha256": original_sha,
                    "original_size": original_size,
                    "reason": "content redacted by archive policy",
                }
            ) + b"\n"
            name = redacted_name.as_posix()
            content = placeholder
            self.omissions.append({"path": logical.as_posix(), "reason": "content policy redacted"})
        if name in self.physical:
            raise ArchiveError(f"duplicate archive physical path: {name}")
        self.physical.add(name)
        target = stage / Path(*PurePosixPath(name).parts)
        _write(target, content)
        self.total += len(content)
        if self.total > MAX_ARCHIVE_BYTES:
            raise ArchiveError("archive exceeds total size limit")
        self.entries.append(
            {
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "policy": selected,
            }
        )

    def _add_file(
        self, stage: Path, logical: str, source: Path, *, sensitive: bool = False, force_full: bool = False
    ) -> None:
        self._add_bytes(
            stage, _safe_relative(logical), _regular_bytes(source),
            sensitive=sensitive, force_full=force_full,
        )

    def _add_tree(self, stage: Path, logical_root: str, source: Path, *, sensitive: bool) -> None:
        if not source.exists():
            return
        for relative, path in _iter_regular_tree(source):
            self._add_file(
                stage,
                (PurePosixPath(logical_root) / relative).as_posix(),
                path,
                sensitive=sensitive or _sensitive(relative),
            )

    def _collect_standard(self, stage: Path, manifest_path: Path) -> dict[str, str]:
        feature = self.state["feature"]
        manifest = _manifest(manifest_path)
        files = {
            "manifest": manifest_path,
            "mission.jsonl": self.root / "mission.jsonl",
            "compiled-workflow.json": self.root / "compiled-workflow.json",
            "runtime-options.json": self.root / "runtime-options.json",
            "objective.txt": self.root / "objective.txt",
        }
        archive_approval = self.root / "archive-approval.json"
        if archive_approval.exists():
            files["archive-approval.json"] = archive_approval
        state_path = self.runs_dir / f"fleet-{feature}.state.json"
        ledger_path = self.runs_dir / f"fleet-{feature}.ledger.jsonl"
        if state_path.exists():
            files["state.json"] = state_path
        if ledger_path.exists():
            files["ledger.jsonl"] = ledger_path
        for logical, path in files.items():
            self._add_file(
                stage, logical, path,
                sensitive=_sensitive(PurePosixPath(logical)),
                force_full=logical != "objective.txt",
            )
        for logical, source in (
            ("prompts", self.runs_dir / "prompts" / feature),
            ("tasks", self.runs_dir / "tasks" / feature),
            ("results", self.runs_dir / "results" / feature),
            ("artifacts", self.root / "artifacts"),
            ("dialogue", self.runs_dir / "dialogue" / feature),
            ("assurance", self.runs_dir / "assurance" / feature),
        ):
            self._add_tree(stage, logical, source, sensitive=True)
        for logical, source in (
            ("dialogue.jsonl", self.runs_dir / f"fleet-{feature}.dialogue.jsonl"),
            ("dialogue-control.jsonl", self.runs_dir / f"fleet-{feature}.dialogue-control.jsonl"),
            ("assurance-control.jsonl", self.runs_dir / f"fleet-{feature}.assurance-control.jsonl"),
            ("verification-receipt.json", self.runs_dir / f"fleet-{feature}.verification-receipt.json"),
            ("assurance-receipt.json", self.runs_dir / f"fleet-{feature}.assurance-receipt.json"),
        ):
            if source.exists():
                self._add_file(stage, logical, source, force_full=True)
        return manifest

    def _writer(self, stage: Path, manifest: dict[str, str]) -> tuple[str, str]:
        repo = Path(self.state["target_repo"])
        base_sha = self.state["base_sha"]
        final_sha = base_sha
        writers = [
            key[:-10]
            for key, value in manifest.items()
            if key.endswith(".authority") and value == "write"
        ]
        if len(writers) > 1:
            raise ArchiveError("manifest contains more than one writer")
        writer = writers[0] if writers else None
        branch = manifest.get(f"{writer}.branch", "") if writer else ""
        if branch:
            final_sha = _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
            _git(repo, "show-ref", "--verify", f"refs/heads/{branch}")
            worktree = Path(manifest.get(f"{writer}.worktree", ""))
            if not worktree.is_dir() or _git(worktree, "status", "--porcelain"):
                raise ArchiveError("writer worktree is absent or dirty")
            if _git(worktree, "rev-parse", "HEAD") != final_sha:
                raise ArchiveError("writer worktree and branch HEAD differ")
        _git(repo, "merge-base", "--is-ancestor", base_sha, final_sha)
        include_delta = self.compiled["workflow"]["archive"]["include_git_delta"]
        include_tree = self.compiled["workflow"]["archive"]["include_final_tree"]
        commit_rows: list[dict[str, Any]] = []
        if base_sha != final_sha:
            raw = _git(
                repo, "log", "--reverse", "--format=%H%x1f%P%x1f%T%x1f%s", f"{base_sha}..{final_sha}"
            )
            for line in raw.splitlines():
                sha, parents, tree, subject = line.split("\x1f", 3)
                commit_rows.append(
                    {"sha": sha, "parents": parents.split(), "tree": tree, "subject": subject}
                )
        final_tree_sha = _git(repo, "rev-parse", f"{final_sha}^{{tree}}")
        commits = {
            "schema_version": 1,
            "base_sha": base_sha,
            "final_sha": final_sha,
            "final_tree_sha": final_tree_sha,
            "writer_instance": writer,
            "branch": branch or None,
            "commits": commit_rows,
        }
        self._add_bytes(
            stage, PurePosixPath("writer/commits.json"),
            mission_state.canonical_bytes(commits) + b"\n", force_full=True,
        )
        if include_delta:
            patch = _run(
                ["git", "-C", str(repo), "diff", "--binary", "--full-index", base_sha, final_sha]
            )
            self._add_bytes(stage, PurePosixPath("writer/change.patch"), patch, force_full=True)
        else:
            self.omissions.append({"path": "writer/change.patch", "reason": "workflow disables git delta"})
        if include_tree:
            tree_tar = _run(["git", "-C", str(repo), "archive", "--format=tar", final_sha])
            if _tree_hash_from_tar(tree_tar) != final_tree_sha:
                raise ArchiveError("generated final tree does not match final Git tree")
            self._add_bytes(stage, PurePosixPath("writer/final-tree.tar"), tree_tar, force_full=True)
        else:
            self.omissions.append({"path": "writer/final-tree.tar", "reason": "workflow disables final tree"})
        return base_sha, final_sha

    def _collect_audit(self, stage: Path) -> None:
        audit_root = self.root / "audit"
        if not audit_root.exists():
            return
        for relative, path in _iter_regular_tree(audit_root):
            if relative.name in SECRET_AUDIT_FILES or relative.name.startswith(".audit-"):
                self.omissions.append(
                    {"path": f"audit/{relative.as_posix()}", "reason": "secret or ephemeral audit material"}
                )
                continue
            self._add_file(
                stage, (PurePosixPath("audit") / relative).as_posix(), path, force_full=True
            )

    def create(self, manifest_path: Path) -> dict[str, Any]:
        # Reconciliation must enforce the current disclosure authorization too.
        # An already-built archive is not a capability that survives approval
        # removal or expiry.
        if self.policy == "full" and set(self.state["risk_categories"]) & {
            "credentials",
            "private_data",
        }:
            self._validate_full_archive_approval()
        if self.output.exists():
            verified = verify_archive(self.output)
            return {**verified, "created": False, "path": str(self.output)}
        temporary = self.output.parent / f".{self.output.name}.{uuid.uuid4().hex}.tmp"
        mission_state.ensure_private_directory(temporary)
        try:
            manifest = self._collect_standard(temporary, manifest_path.resolve())
            base_sha, final_sha = self._writer(temporary, manifest)
            non_audit_entries = sorted(self.entries, key=lambda item: item["path"])
            content_root = mission_state.sha256(non_audit_entries)
            if self.state.get("approval") is not None:
                if not (self.root / "audit").is_dir():
                    raise ArchiveError("assured archive requires a live signed audit store")
                lifecycle = fleet_audit_client.AuditLifecycle(self.runs_dir, self.mission_id)
                lifecycle.record_control_event(
                    event_type="ArchiveContentRoot",
                    subject_id=self.mission_id,
                    subject_sha256=content_root,
                    metadata={"archive_policy": self.policy, "entry_count": len(non_audit_entries)},
                    idempotency_key=f"archive-root:{content_root}",
                )
                lifecycle.verify()
            self._collect_audit(temporary)
            index = {
                "schema_version": 1,
                "mission_id": self.mission_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "content_policy": self.policy,
                "base_sha": base_sha,
                "final_sha": final_sha,
                "entries": sorted(self.entries, key=lambda item: item["path"]),
                "omissions": sorted(self.omissions, key=lambda item: (item["path"], item["reason"])),
            }
            index_bytes = mission_state.canonical_bytes(index) + b"\n"
            _write(temporary / "archive-index.json", index_bytes)
            receipt = {
                "schema_version": 1,
                "mission_id": self.mission_id,
                "index_sha256": hashlib.sha256(index_bytes).hexdigest(),
                "content_root_sha256": content_root,
                "entry_count": len(index["entries"]),
                "created_at": index["created_at"],
            }
            _write(
                temporary / "archive-receipt.json",
                mission_state.canonical_bytes(receipt) + b"\n",
            )
            self.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(temporary, self.output)
            result = verify_archive(self.output)
            return {**result, "created": True, "path": str(self.output)}
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def verify_archive(path: Path, *, repo: Path | None = None) -> dict[str, Any]:
    root = path.resolve()
    if root.is_symlink() or not root.is_dir():
        raise ArchiveError("archive must be a regular directory")
    index_path = root / "archive-index.json"
    receipt_path = root / "archive-receipt.json"
    index = json.loads(_regular_bytes(index_path))
    receipt = json.loads(_regular_bytes(receipt_path))
    required = {
        "schema_version", "mission_id", "created_at", "content_policy", "base_sha",
        "final_sha", "entries", "omissions",
    }
    if not isinstance(index, dict) or set(index) != required or index["schema_version"] != 1:
        raise ArchiveError("archive index fields are invalid")
    if index["content_policy"] not in {"full", "redacted", "hash-only"}:
        raise ArchiveError("archive content policy is invalid")
    mission_state.normalize_uuid(index["mission_id"], "archive mission_id")
    try:
        created_at = datetime.fromisoformat(str(index["created_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArchiveError("archive created_at is invalid") from exc
    if created_at.tzinfo is None:
        raise ArchiveError("archive created_at lacks timezone")
    if not GIT_SHA.fullmatch(str(index["base_sha"])) or not GIT_SHA.fullmatch(
        str(index["final_sha"])
    ):
        raise ArchiveError("archive Git SHA is invalid")
    if not isinstance(index["entries"], list) or not isinstance(index["omissions"], list):
        raise ArchiveError("archive index lists are invalid")
    for omission in index["omissions"]:
        if (
            not isinstance(omission, dict)
            or set(omission) != {"path", "reason"}
            or not isinstance(omission["reason"], str)
            or not omission["reason"]
        ):
            raise ArchiveError("archive omission fields are invalid")
        _safe_relative(str(omission["path"]))
    expected_physical = {"archive-index.json", "archive-receipt.json"}
    non_audit: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in index["entries"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size", "policy"}:
            raise ArchiveError("archive entry fields are invalid")
        logical = _safe_relative(str(entry["path"]))
        name = logical.as_posix()
        if name in seen:
            raise ArchiveError("archive index contains duplicate paths")
        seen.add(name)
        if entry["policy"] not in {"full", "redacted", "hash-only"}:
            raise ArchiveError("archive entry policy is invalid")
        if not isinstance(entry["size"], int) or entry["size"] < 0:
            raise ArchiveError("archive entry size is invalid")
        if not isinstance(entry["sha256"], str) or not SHA256.fullmatch(entry["sha256"]):
            raise ArchiveError("archive entry hash is invalid")
        if entry["policy"] != "hash-only":
            source = root / Path(*logical.parts)
            content = _regular_bytes(source)
            if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ArchiveError(f"archive entry hash mismatch: {name}")
            expected_physical.add(name)
        if not name.startswith("audit/"):
            non_audit.append(entry)
    actual_files = {
        relative.as_posix()
        for relative, _ in _iter_regular_tree(root)
    }
    if actual_files != expected_physical:
        extra = sorted(actual_files - expected_physical)
        missing = sorted(expected_physical - actual_files)
        raise ArchiveError(f"archive physical file set mismatch extra={extra} missing={missing}")
    index_bytes = _regular_bytes(index_path)
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {
            "schema_version", "mission_id", "index_sha256", "content_root_sha256",
            "entry_count", "created_at",
        }
        or receipt["schema_version"] != 1
        or receipt["mission_id"] != index["mission_id"]
        or receipt["created_at"] != index["created_at"]
        or receipt["entry_count"] != len(index["entries"])
        or receipt["index_sha256"] != hashlib.sha256(index_bytes).hexdigest()
        or receipt["content_root_sha256"] != mission_state.sha256(
            sorted(non_audit, key=lambda item: item["path"])
        )
    ):
        raise ArchiveError("archive receipt does not match index")
    commits_path = root / "writer" / "commits.json"
    commits = json.loads(_regular_bytes(commits_path))
    if commits.get("base_sha") != index["base_sha"] or commits.get("final_sha") != index["final_sha"]:
        raise ArchiveError("writer metadata does not match archive index")
    tree_path = root / "writer" / "final-tree.tar"
    if tree_path.exists() and _tree_hash_from_tar(_regular_bytes(tree_path)) != commits.get("final_tree_sha"):
        raise ArchiveError("final tree does not reproduce final commit tree")
    if repo is not None:
        repo = repo.resolve()
        if _git(repo, "rev-parse", "--verify", index["final_sha"]) != index["final_sha"]:
            raise ArchiveError("final commit is not reachable in repository")
        if tree_path.exists():
            expected_tar = _run(
                ["git", "-C", str(repo), "archive", "--format=tar", index["final_sha"]]
            )
            if expected_tar != _regular_bytes(tree_path):
                raise ArchiveError("final tree tar differs from final commit")
        patch_path = root / "writer" / "change.patch"
        if patch_path.exists():
            expected_patch = _run(
                [
                    "git", "-C", str(repo), "diff", "--binary", "--full-index",
                    index["base_sha"], index["final_sha"],
                ]
            )
            if expected_patch != _regular_bytes(patch_path):
                raise ArchiveError("binary patch does not match base/final commits")
    audit_ledger = root / "audit" / "ledgers" / index["mission_id"] / "a2a_ledger.jsonl"
    if audit_ledger.exists():
        compiled = json.loads(_regular_bytes(root / "compiled-workflow.json"))
        try:
            require_worm = compiled["workflow"]["audit"]["mode"] == "worm"
            required_trust_scope = compiled["workflow"]["audit"]["trust_scope"]
        except (KeyError, TypeError) as exc:
            raise ArchiveError("archived audit policy is invalid") from exc
        audit_result = fleet_audit_client.verify_offline(
            audit_ledger,
            root / "audit" / "audit-verification.json",
            root / "audit" / "audit-signing-public.pem",
            root / "audit" / "anchor-receipts",
            require_worm=require_worm,
            required_trust_scope=required_trust_scope,
        )
        archive_events = [
            event for event in fleet_audit_client._chain_without_secret(audit_ledger)
            if event.get("event_type") == "ArchiveContentRoot"
        ]
        if len(archive_events) != 1 or archive_events[0].get("subject_sha256") != receipt["content_root_sha256"]:
            raise ArchiveError("signed audit does not bind the archive content root")
    else:
        audit_result = None
    return {
        "mission_id": index["mission_id"],
        "entries": len(index["entries"]),
        "content_policy": index["content_policy"],
        "base_sha": index["base_sha"],
        "final_sha": index["final_sha"],
        "content_root_sha256": receipt["content_root_sha256"],
        "audit": audit_result,
        "valid": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--runs-dir", required=True)
    create.add_argument("--mission-id", required=True)
    create.add_argument("--manifest", required=True)
    create.add_argument("--output")
    verify = commands.add_parser("verify")
    verify.add_argument("archive")
    verify.add_argument("--repo")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            value = ArchiveBuilder(
                Path(args.runs_dir), args.mission_id,
                Path(args.output).resolve() if args.output else None,
            ).create(Path(args.manifest))
        else:
            value = verify_archive(
                Path(args.archive), repo=Path(args.repo) if args.repo else None
            )
        print(json.dumps(value, sort_keys=True))
        return 0
    except (
        ArchiveError,
        mission_state.MissionStateError,
        fleet_audit_client.AuditClientError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"fleet-archive: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
