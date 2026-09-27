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
import tempfile
from typing import Any
import uuid

# Compatibility aliases keep historical callers and exception identity stable.
from fleet_archive_tree import (
    ArchiveError, MAX_FILE_BYTES, OBJECT_FORMATS, GIT_MODE_PAX, GIT_OID_PAX,
    _object_format, _git_oid, _hash_git_object, _git_name, _is_structural_empty_tar,
    safe_relative as _safe_relative, tree_hash_from_tar as _tree_hash_from_tar,
)
from fleet_archive_contract import (
    GIT_SHA, WRITER_FIELDS_LEGACY, WRITER_FIELDS_CURRENT, WRITER_COMMIT_FIELDS,
    MANIFEST_V2_LANE, MANIFEST_V2_NOT_ATTESTED, MANIFEST_V3_PUBLICATION_FIELDS,
    ROUTER_PRE_PROVIDER_LANE, ROUTER_PRE_PROVIDER_NOT_ATTESTED,
    _parse_manifest, _archive_manifest_binding, _is_manifest_contract_v2,
    _archive_manifest_binding_v2, _router_provider_contract, _strict_json, _strict_jsonl,
    _historical_json_document, _historical_compiled, _historical_state, _writer_metadata,
)
from fleet_git_snapshot import (
    _run, _run_bounded, _git_command, _git_run, _git_run_bounded, _git,
    _raw_tree_records, _batch_blob_header, _raw_tree_layout, _raw_blob_data, _tar_info,
    git_environment as _git_environment, raw_tree_tar as _raw_tree_tar,
)

import fleet_audit_client
import fleet_compiled
import fleet_json
import fleet_ledger
import fleet_mission
import fleet_mission_state as mission_state
import fleet_safe_paths
import fleet_state


ROOT = Path(__file__).resolve().parents[1]
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
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
ARCHIVE_RECEIPT_FIELDS_LEGACY = {
    "schema_version", "mission_id", "index_sha256", "content_root_sha256",
    "entry_count", "created_at",
}
ARCHIVE_RECEIPT_FIELDS_CURRENT = ARCHIVE_RECEIPT_FIELDS_LEGACY | {
    "audit_required", "audit_receipt_sha256",
}


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _manifest(
    runs_dir: Path, path: Path, *, feature: str
) -> tuple[dict[str, str], bytes]:
    """Read the exact active manifest through a no-follow root descriptor."""

    leaf = f"fleet-{feature}.manifest"
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            # Resolve only the trusted parent. Resolving the leaf itself would
            # turn a hostile manifest symlink into an apparently regular file.
            parent = path.parent.resolve(strict=True)
            if path.name != leaf or parent != rooted.root:
                raise ArchiveError("fleet manifest path is not feature-bound")
            content = rooted.read_regular(
                leaf,
                directory_modes=(),
                file_mode=0o600,
                max_bytes=MAX_MANIFEST_BYTES,
            )
            rooted.assert_root_binding()
    except (OSError, RuntimeError, fleet_safe_paths.SafePathError) as exc:
        raise ArchiveError(f"unsafe fleet manifest: {exc}") from exc
    return _parse_manifest(content), content


def _phase_state_snapshot(
    runs_dir: Path,
    *,
    feature: str,
    manifest: dict[str, str],
    manifest_bytes: bytes,
) -> bytes | None:
    """Read and bind the optional phase sidecar under one pinned runs root."""

    manifest_leaf = f"fleet-{feature}.manifest"
    state_leaf = f"fleet-{feature}.state.json"
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            current_manifest = rooted.read_regular(
                manifest_leaf,
                directory_modes=(),
                file_mode=0o600,
                max_bytes=MAX_MANIFEST_BYTES,
                require_single_link=True,
            )
            if current_manifest != manifest_bytes:
                raise ArchiveError(
                    "fleet manifest changed before phase-state collection"
                )
            state_bytes = rooted.read_regular_optional(
                state_leaf,
                directory_modes=(),
                file_mode=0o600,
                max_bytes=fleet_state.MAX_STATE_BYTES,
                require_single_link=True,
            )
            if state_bytes is None:
                rooted.assert_root_binding()
                return None
            try:
                value = fleet_json.loads(state_bytes)
                validated = fleet_state.validate_state(value, manifest)
                canonical = fleet_json.canonical_bytes(validated) + b"\n"
            except (
                fleet_json.FleetJSONError,
                fleet_state.PhaseStateError,
            ) as exc:
                raise ArchiveError(f"phase state is invalid: {exc}") from exc
            if state_bytes != canonical:
                raise ArchiveError("phase state bytes are not canonical")
            if (
                rooted.read_regular(
                    manifest_leaf,
                    directory_modes=(),
                    file_mode=0o600,
                    max_bytes=MAX_MANIFEST_BYTES,
                    require_single_link=True,
                )
                != manifest_bytes
            ):
                raise ArchiveError(
                    "fleet manifest changed during phase-state collection"
                )
            rooted.assert_root_binding()
            return state_bytes
    except fleet_safe_paths.SafePathError as exc:
        raise ArchiveError(f"unsafe phase state source: {exc}") from exc


def _write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    mission_state.atomic_write(path, content, mode=mode)


def _regular_bytes(path: Path) -> bytes:
    """Read one explicitly selected leaf through a pinned parent descriptor."""

    try:
        selected = path.expanduser()
        parent = selected.parent.resolve(strict=True)
        with fleet_safe_paths.RootedFS(parent) as rooted:
            last_error: fleet_safe_paths.SafePathError | None = None
            for mode in (0o600, 0o644, 0o700, 0o755):
                try:
                    return rooted.read_regular(
                        selected.name,
                        directory_modes=(),
                        file_mode=mode,
                        max_bytes=MAX_FILE_BYTES,
                        require_single_link=True,
                    )
                except fleet_safe_paths.SafePathError as exc:
                    last_error = exc
            assert last_error is not None
            raise last_error
    except (OSError, RuntimeError, fleet_safe_paths.SafePathError) as exc:
        raise ArchiveError(
            f"archive source must be a regular non-symlink single-link file: {path}"
        ) from exc


class _ArchiveView:
    """One immutable descriptor-pinned snapshot of a physical archive tree."""

    def __init__(self, path: Path) -> None:
        lexical = path.expanduser()
        try:
            before = lexical.lstat()
        except OSError as exc:
            raise ArchiveError(f"archive root is unavailable: {path}") from exc
        if stat.S_ISLNK(before.st_mode):
            raise ArchiveError("archive root must not be a symlink")
        if not stat.S_ISDIR(before.st_mode):
            raise ArchiveError("archive must be a regular directory")
        try:
            self.rooted = fleet_safe_paths.RootedFS(lexical, root_mode=0o700)
            after = lexical.lstat()
            pinned = self.rooted.root.lstat()
        except (OSError, RuntimeError, fleet_safe_paths.SafePathError) as exc:
            raise ArchiveError("archive root is unsafe") from exc
        fingerprints = {
            (item.st_dev, item.st_ino) for item in (before, after, pinned)
        }
        if len(fingerprints) != 1:
            self.rooted.close()
            raise ArchiveError("archive root binding changed while opening")
        self.root = self.rooted.root
        self.contents: dict[str, bytes] = {}
        self.total = 0

    def close(self) -> None:
        self.rooted.close()

    def __enter__(self) -> _ArchiveView:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _walk(
        self, relative: Path, directory_modes: tuple[int | None, ...]
    ) -> None:
        try:
            names = self.rooted.list_directory(
                relative,
                directory_modes=directory_modes,
            )
        except fleet_safe_paths.SafePathError as exc:
            raise ArchiveError("archive directory tree is unsafe") from exc
        for name in names:
            child = relative / name
            try:
                self.rooted.list_directory(
                    child,
                    directory_modes=directory_modes + (None,),
                )
            except fleet_safe_paths.SafePathError:
                try:
                    content = self.rooted.read_regular(
                        child,
                        directory_modes=directory_modes,
                        file_mode=0o600,
                        max_bytes=MAX_FILE_BYTES,
                        require_single_link=True,
                    )
                except fleet_safe_paths.SafePathError as exc:
                    raise ArchiveError(
                        f"archive entry is not a safe regular file: {child}"
                    ) from exc
                logical = child.as_posix()
                self.total += len(content)
                if self.total > MAX_ARCHIVE_BYTES:
                    raise ArchiveError("archive exceeds total size limit")
                self.contents[logical] = content
            else:
                self._walk(child, directory_modes + (None,))

    def snapshot(self) -> dict[str, bytes]:
        self._walk(Path(), ())
        self.rooted.assert_root_binding()
        return dict(self.contents)


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


class ArchiveBuilder:
    def __init__(self, runs_dir: Path, mission_id: str, output: Path | None = None) -> None:
        self.runs_dir = runs_dir.resolve()
        self.mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
        self.root = mission_state.mission_root(self.runs_dir, self.mission_id)
        requested_output = (output or self.root / "archive").expanduser()
        try:
            output_parent = requested_output.parent.resolve(strict=True)
        except OSError as exc:
            raise ArchiveError("archive output parent is unavailable") from exc
        self.output = output_parent / requested_output.name
        if self.output != self.root / "archive":
            raise ArchiveError("archive output is not bound to the exact mission root")
        try:
            self.compiled, self.state = fleet_mission.load_mission_compiled(
                self.runs_dir, self.mission_id, mode="effect"
            )
        except (fleet_mission.MissionError, mission_state.MissionStateError) as exc:
            raise ArchiveError(
                f"compiled workflow is not effect-authorized for archive creation: {exc}"
            ) from exc
        self.policy = self.compiled["workflow"]["archive"]["content_policy"]
        self.entries: list[dict[str, Any]] = []
        self.omissions: list[dict[str, str]] = []
        self.total = 0
        self.seen: set[str] = set()
        self.physical: set[str] = set()
        self._approval_loaded = False
        self._approval_value: dict[str, Any] | None = None
        self._approval_bytes: bytes | None = None

    def _approval_snapshot(self) -> tuple[dict[str, Any], bytes] | None:
        if self._approval_loaded:
            if self._approval_value is None or self._approval_bytes is None:
                return None
            return self._approval_value, self._approval_bytes
        relative = Path("missions") / self.mission_id / "archive-approval.json"
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                content = rooted.read_regular_optional(
                    relative,
                    directory_modes=(0o700, 0o700),
                    file_mode=0o600,
                    max_bytes=MAX_FILE_BYTES,
                    require_single_link=True,
                )
                rooted.assert_root_binding()
        except fleet_safe_paths.SafePathError as exc:
            raise ArchiveError("unsafe archive approval source") from exc
        self._approval_loaded = True
        if content is None:
            return None
        value = _strict_json(content, where="archive approval")
        if type(value) is not dict:
            raise ArchiveError("archive approval must be an object")
        self._approval_value = value
        self._approval_bytes = content
        return value, content

    def _validate_full_archive_approval(self) -> Path:
        snapshot = self._approval_snapshot()
        if snapshot is None:
            raise ArchiveError(
                "full archive is forbidden for credentials/private_data without archive approval"
            )
        value, _ = snapshot
        required = {
            "schema_version", "mission_id", "approval_id", "idempotency_key",
            "workflow_digest", "scope", "content_policy", "risk_categories",
            "expires_at", "approved_by_sha256", "decision",
        }
        if set(value) != required or any(
            (
                type(value["schema_version"]) is not int,
                value["schema_version"] != 1,
                value["mission_id"] != self.mission_id,
                value["workflow_digest"] != self.state["workflow_digest"],
                value["scope"] != self.state["target_repo"],
                value["content_policy"] != "full",
                type(value["risk_categories"]) is not list,
                value["risk_categories"]
                != sorted(set(self.state["risk_categories"]) & {"credentials", "private_data"}),
                value["decision"] != "approved",
                type(value["approved_by_sha256"]) is not str,
                not mission_state.SHA256.fullmatch(value["approved_by_sha256"]),
                type(value["idempotency_key"]) is not str,
                not value["idempotency_key"],
            )
        ):
            raise ArchiveError("sensitive full-archive approval is invalid")
        try:
            if type(value["approval_id"]) is not str:
                raise ValueError
            approval_id = mission_state.normalize_uuid(
                value["approval_id"], "archive approval_id"
            )
            if approval_id != value["approval_id"] or type(value["expires_at"]) is not str:
                raise ValueError
            expires = datetime.fromisoformat(
                value["expires_at"].replace("Z", "+00:00")
            )
        except (mission_state.MissionStateError, ValueError) as exc:
            raise ArchiveError("sensitive full-archive approval is invalid") from exc
        if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            raise ArchiveError("sensitive full-archive approval is expired")
        return self.root / "archive-approval.json"

    def _mission_snapshot(self) -> dict[str, bytes]:
        base = Path("missions") / self.mission_id
        selected: dict[str, bytes] = {}
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                for leaf in (
                    "mission.jsonl",
                    "compiled-workflow.json",
                    "runtime-options.json",
                    "objective.txt",
                ):
                    selected[leaf] = rooted.read_regular(
                        base / leaf,
                        directory_modes=(0o700, 0o700),
                        file_mode=0o600,
                        max_bytes=MAX_FILE_BYTES,
                        require_single_link=True,
                    )
                rooted.assert_root_binding()
        except fleet_safe_paths.SafePathError as exc:
            raise ArchiveError("mission archive source is unsafe") from exc
        try:
            events = mission_state._events_from_bytes(
                selected["mission.jsonl"],
                expected_mission_id=self.mission_id,
                require_nonempty=True,
            )
            if selected["mission.jsonl"] != fleet_json.canonical_jsonl(events):
                raise ArchiveError("mission source ledger is not canonical JSONL")
            source_state = mission_state.derive_state(events)
            if source_state != self.state:
                raise ArchiveError("mission source changed during archive preparation")
            compiled = fleet_compiled.loads(
                selected["compiled-workflow.json"], mode="effect"
            )
            if (
                compiled != self.compiled
                or selected["compiled-workflow.json"]
                != fleet_json.canonical_bytes(compiled) + b"\n"
            ):
                raise ArchiveError("compiled workflow source changed or is not canonical")
            runtime_options = _strict_json(
                selected["runtime-options.json"], where="runtime options"
            )
            if type(runtime_options) is not dict:
                raise ArchiveError("runtime options must be an object")
        except (
            mission_state.MissionStateError,
            fleet_compiled.CompiledError,
            fleet_json.FleetJSONError,
        ) as exc:
            raise ArchiveError(f"mission archive source is invalid: {exc}") from exc
        return selected

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

    def _rooted_tree_snapshot(
        self,
        source: Path,
        *,
        omit_audit_secrets: bool = False,
    ) -> tuple[list[tuple[PurePosixPath, bytes]], list[PurePosixPath]]:
        try:
            source_relative = source.relative_to(self.runs_dir)
        except ValueError as exc:
            raise ArchiveError("archive tree source is outside runs root") from exc
        files: list[tuple[PurePosixPath, bytes]] = []
        omitted: list[PurePosixPath] = []
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                root_modes: tuple[int | None, ...] = (None,) * len(
                    source_relative.parts
                )
                try:
                    rooted.list_directory(
                        source_relative,
                        directory_modes=root_modes,
                    )
                except fleet_safe_paths.SafePathError as exc:
                    if str(exc).startswith("rooted directory is missing:"):
                        return [], []
                    raise

                def walk(
                    rooted_relative: Path,
                    logical_relative: PurePosixPath,
                    modes: tuple[int | None, ...],
                ) -> None:
                    names = rooted.list_directory(
                        rooted_relative,
                        directory_modes=modes,
                    )
                    for name in names:
                        logical = logical_relative / name
                        child = rooted_relative / name
                        if omit_audit_secrets and (
                            name in SECRET_AUDIT_FILES or name.startswith(".audit-")
                        ):
                            omitted.append(logical)
                            continue
                        try:
                            rooted.list_directory(
                                child,
                                directory_modes=modes + (None,),
                            )
                        except fleet_safe_paths.SafePathError:
                            last_error: fleet_safe_paths.SafePathError | None = None
                            content: bytes | None = None
                            for file_mode in (0o600, 0o644, 0o700, 0o755):
                                try:
                                    content = rooted.read_regular(
                                        child,
                                        directory_modes=modes,
                                        file_mode=file_mode,
                                        max_bytes=MAX_FILE_BYTES,
                                        require_single_link=True,
                                    )
                                    break
                                except fleet_safe_paths.SafePathError as exc:
                                    last_error = exc
                            if content is None:
                                assert last_error is not None
                                raise last_error
                            files.append((logical, content))
                        else:
                            walk(child, logical, modes + (None,))

                walk(source_relative, PurePosixPath(), root_modes)
                rooted.assert_root_binding()
        except fleet_safe_paths.SafePathError as exc:
            raise ArchiveError(f"unsafe archive tree source: {source}") from exc
        return files, omitted

    def _add_tree(
        self, stage: Path, logical_root: str, source: Path, *, sensitive: bool
    ) -> None:
        files, _ = self._rooted_tree_snapshot(source)
        for relative, content in files:
            self._add_bytes(
                stage,
                PurePosixPath(logical_root) / relative,
                content,
                sensitive=sensitive or _sensitive(relative),
            )

    def _collect_standard(self, stage: Path, manifest_path: Path) -> dict[str, str]:
        feature = self.state["feature"]
        manifest, manifest_bytes = _manifest(
            self.runs_dir, manifest_path, feature=feature
        )
        _archive_manifest_binding(manifest, self.state)
        self._add_bytes(
            stage,
            PurePosixPath("manifest"),
            manifest_bytes,
            force_full=True,
        )
        files = self._mission_snapshot()
        approval = self._approval_snapshot()
        if approval is not None:
            _, files["archive-approval.json"] = approval
        for logical, content in files.items():
            self._add_bytes(
                stage,
                PurePosixPath(logical),
                content,
                sensitive=_sensitive(PurePosixPath(logical)),
                force_full=logical != "objective.txt",
            )
        phase_state = _phase_state_snapshot(
            self.runs_dir,
            feature=feature,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
        )
        if phase_state is not None:
            self._add_bytes(
                stage,
                PurePosixPath("state.json"),
                phase_state,
                force_full=True,
            )
        ledger_name = f"fleet-{feature}.ledger.jsonl"
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                ledger_bytes = rooted.read_regular_optional(
                    ledger_name,
                    directory_modes=(),
                    file_mode=0o600,
                    max_bytes=fleet_ledger.MAX_LEDGER_BYTES,
                )
                rooted.assert_root_binding()
        except fleet_safe_paths.SafePathError as exc:
            raise ArchiveError(f"unsafe lifecycle ledger source: {exc}") from exc
        if ledger_bytes is not None:
            try:
                fleet_ledger._records_from_bytes(ledger_bytes)
            except fleet_ledger.LedgerError as exc:
                raise ArchiveError(f"lifecycle ledger is corrupt: {exc}") from exc
            self._add_bytes(
                stage,
                PurePosixPath("ledger.jsonl"),
                ledger_bytes,
                sensitive=False,
                force_full=True,
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
        durable_sources = (
            ("dialogue.jsonl", f"fleet-{feature}.dialogue.jsonl", "jsonl"),
            (
                "dialogue-control.jsonl",
                f"fleet-{feature}.dialogue-control.jsonl",
                "jsonl",
            ),
            (
                "assurance-control.jsonl",
                f"fleet-{feature}.assurance-control.jsonl",
                "jsonl",
            ),
            (
                "verification-receipt.json",
                f"fleet-{feature}.verification-receipt.json",
                "json",
            ),
            (
                "assurance-receipt.json",
                f"fleet-{feature}.assurance-receipt.json",
                "json",
            ),
        )
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                durable: dict[str, tuple[bytes, str]] = {}
                for logical, leaf, kind in durable_sources:
                    content = rooted.read_regular_optional(
                        leaf,
                        directory_modes=(),
                        file_mode=0o600,
                        max_bytes=MAX_FILE_BYTES,
                        require_single_link=True,
                    )
                    if content is not None:
                        durable[logical] = content, kind
                rooted.assert_root_binding()
        except fleet_safe_paths.SafePathError as exc:
            raise ArchiveError("unsafe durable evidence source") from exc
        for logical, (content, kind) in durable.items():
            if kind == "jsonl":
                _strict_jsonl(
                    content,
                    where=logical,
                    require_canonical=False,
                )
            else:
                value = _historical_json_document(content, where=logical)
                if type(value) is not dict:
                    raise ArchiveError(f"{logical} must contain a JSON object")
            self._add_bytes(
                stage,
                PurePosixPath(logical),
                content,
                force_full=True,
            )
        return manifest

    def _writer(self, stage: Path, manifest: dict[str, str]) -> tuple[str, str]:
        repo = Path(self.state["target_repo"])
        base_sha = self.state["base_sha"]
        writer, branch, final_sha = _archive_manifest_binding(manifest, self.state)
        object_format = _git(repo, "rev-parse", "--show-object-format")
        _object_format(object_format)
        _git_oid(base_sha, object_format, where="mission base_sha")
        if writer:
            _git_oid(final_sha, object_format, where="writer final_sha")
            assert branch is not None
            branch_sha = _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
            _git(repo, "show-ref", "--verify", f"refs/heads/{branch}")
            if branch_sha != final_sha:
                raise ArchiveError("writer published branch drifted after quiescence")
            worktree = Path(manifest.get(f"{writer}.worktree", ""))
            if worktree.exists() or worktree.is_symlink():
                raise ArchiveError("isolated writer clone still exists after quiescence")
        _git(repo, "merge-base", "--is-ancestor", base_sha, final_sha)
        include_delta = self.compiled["workflow"]["archive"]["include_git_delta"]
        include_tree = self.compiled["workflow"]["archive"]["include_final_tree"]
        commit_rows: list[dict[str, Any]] = []
        if base_sha != final_sha:
            raw = _git_run_bounded(
                repo,
                "log",
                "--reverse",
                "--format=%H%x1f%P%x1f%T%x1f%s%x00",
                f"{base_sha}..{final_sha}",
            )
            for record in raw.split(b"\0"):
                record = record.strip(b"\n")
                if not record:
                    continue
                try:
                    sha_raw, parents_raw, tree_raw, subject_raw = record.split(
                        b"\x1f", 3
                    )
                    sha = sha_raw.decode("ascii")
                    parents = parents_raw.decode("ascii")
                    tree = tree_raw.decode("ascii")
                except (UnicodeDecodeError, ValueError) as exc:
                    raise ArchiveError("Git returned malformed commit metadata") from exc
                subject = subject_raw.decode("utf-8", "replace")
                commit_rows.append(
                    {"sha": sha, "parents": parents.split(), "tree": tree, "subject": subject}
                )
        final_tree_sha = _git(repo, "rev-parse", f"{final_sha}^{{tree}}")
        _git_oid(final_tree_sha, object_format, where="writer final tree")
        commits = {
            "schema_version": 1,
            "object_format": object_format,
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
            patch = _git_run_bounded(
                repo,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--binary",
                "--full-index",
                base_sha,
                final_sha,
            )
            self._add_bytes(stage, PurePosixPath("writer/change.patch"), patch, force_full=True)
        else:
            self.omissions.append({"path": "writer/change.patch", "reason": "workflow disables git delta"})
        if include_tree:
            tree_tar = _raw_tree_tar(repo, final_sha, object_format)
            if _tree_hash_from_tar(tree_tar, object_format) != final_tree_sha:
                raise ArchiveError("generated final tree does not match final Git tree")
            self._add_bytes(stage, PurePosixPath("writer/final-tree.tar"), tree_tar, force_full=True)
        else:
            self.omissions.append({"path": "writer/final-tree.tar", "reason": "workflow disables final tree"})
        return base_sha, final_sha

    def _collect_audit(self, stage: Path) -> None:
        audit_root = self.root / "audit"
        files, omitted = self._rooted_tree_snapshot(
            audit_root,
            omit_audit_secrets=True,
        )
        for relative in omitted:
            self.omissions.append(
                {
                    "path": f"audit/{relative.as_posix()}",
                    "reason": "secret or ephemeral audit material",
                }
            )
        for relative, content in files:
            self._add_bytes(
                stage,
                PurePosixPath("audit") / relative,
                content,
                force_full=True,
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
        if self.output.exists() or self.output.is_symlink():
            verified = verify_archive(self.output)
            if verified["mission_id"] != self.mission_id:
                raise ArchiveError("existing archive mission_id does not match requested mission")
            if (
                verified["target_repo"] != self.state["target_repo"]
                or verified["base_sha"] != self.state["base_sha"]
            ):
                raise ArchiveError("existing archive repository binding does not match mission")
            return {**verified, "created": False, "path": str(self.output)}
        temporary = self.output.parent / f".{self.output.name}.{uuid.uuid4().hex}.tmp"
        mission_state.ensure_private_directory(temporary)
        try:
            manifest = self._collect_standard(temporary, manifest_path)
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
            audit_receipt_path = temporary / "audit" / "audit-verification.json"
            audit_required = self.state.get("approval") is not None
            if audit_required and not audit_receipt_path.is_file():
                raise ArchiveError("assured archive requires signed audit evidence")
            audit_receipt_sha256 = (
                hashlib.sha256(_regular_bytes(audit_receipt_path)).hexdigest()
                if audit_receipt_path.exists() else None
            )
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
                "audit_required": audit_required,
                "audit_receipt_sha256": audit_receipt_sha256,
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


def verify_acceptance(path: Path, contract: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the exact verified snapshot; never extract or execute its files."""
    import fleet_acceptance

    with _ArchiveView(path) as view:
        contents = view.snapshot()
        verified = _verify_archive_snapshot(view, contents, repo=None)
        if verified.get("historical_lanes"):
            raise ArchiveError("acceptance requires the current archive contract, not a historical lane")
        options = _strict_json(contents.get("runtime-options.json", b"{}"), where="runtime options")
        if options.get("acceptance_contract") != contract:
            raise ArchiveError("acceptance contract differs from archived runtime options")
        if "writer/final-tree.tar" not in contents:
            raise ArchiveError("acceptance requires the archived final tree")
        result = fleet_acceptance.evaluate(
            contract, contents["writer/final-tree.tar"],
            mission_id=verified["mission_id"], final_sha=verified["final_sha"],
        )
        result["archive_content_root_sha256"] = verified["content_root_sha256"]
        view.rooted.assert_root_binding()
        return result


def verify_archive(path: Path, *, repo: Path | None = None) -> dict[str, Any]:
    with _ArchiveView(path) as view:
        contents = view.snapshot()
        result = _verify_archive_snapshot(view, contents, repo=repo)
        try:
            view.rooted.assert_root_binding()
        except fleet_safe_paths.SafePathError as exc:
            raise ArchiveError("archive root binding changed during verification") from exc
        return result


def _verify_archive_snapshot(
    view: _ArchiveView,
    contents: dict[str, bytes],
    *,
    repo: Path | None,
) -> dict[str, Any]:
    root = view.root
    try:
        index_bytes = contents["archive-index.json"]
        receipt_bytes = contents["archive-receipt.json"]
    except KeyError as exc:
        raise ArchiveError("archive is missing its index or receipt") from exc
    index = _strict_json(index_bytes, where="archive index")
    receipt = _strict_json(receipt_bytes, where="archive receipt")
    required = {
        "schema_version", "mission_id", "created_at", "content_policy", "base_sha",
        "final_sha", "entries", "omissions",
    }
    if (
        type(index) is not dict
        or set(index) != required
        or type(index["schema_version"]) is not int
        or index["schema_version"] != 1
    ):
        raise ArchiveError("archive index fields are invalid")
    if index["content_policy"] not in {"full", "redacted", "hash-only"}:
        raise ArchiveError("archive content policy is invalid")
    if type(index["mission_id"]) is not str:
        raise ArchiveError("archive mission_id is invalid")
    canonical_mission_id = mission_state.normalize_uuid(
        index["mission_id"], "archive mission_id"
    )
    if canonical_mission_id != index["mission_id"]:
        raise ArchiveError("archive mission_id is not canonical")
    if type(index["created_at"]) is not str:
        raise ArchiveError("archive created_at is invalid")
    try:
        created_at = datetime.fromisoformat(str(index["created_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArchiveError("archive created_at is invalid") from exc
    if created_at.tzinfo is None:
        raise ArchiveError("archive created_at lacks timezone")
    if type(index["base_sha"]) is not str or type(index["final_sha"]) is not str:
        raise ArchiveError("archive Git SHA is invalid")
    if not GIT_SHA.fullmatch(index["base_sha"]) or not GIT_SHA.fullmatch(
        index["final_sha"]
    ):
        raise ArchiveError("archive Git SHA is invalid")
    if not isinstance(index["entries"], list) or not isinstance(index["omissions"], list):
        raise ArchiveError("archive index lists are invalid")
    for omission in index["omissions"]:
        if (
            type(omission) is not dict
            or set(omission) != {"path", "reason"}
            or type(omission["path"]) is not str
            or type(omission["reason"]) is not str
            or not omission["reason"]
        ):
            raise ArchiveError("archive omission fields are invalid")
        _safe_relative(omission["path"])
    expected_physical = {"archive-index.json", "archive-receipt.json"}
    non_audit: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in index["entries"]:
        if (
            type(entry) is not dict
            or set(entry) != {"path", "sha256", "size", "policy"}
            or type(entry["path"]) is not str
        ):
            raise ArchiveError("archive entry fields are invalid")
        logical = _safe_relative(entry["path"])
        name = logical.as_posix()
        if name in seen:
            raise ArchiveError("archive index contains duplicate paths")
        seen.add(name)
        if entry["policy"] not in {"full", "redacted", "hash-only"}:
            raise ArchiveError("archive entry policy is invalid")
        if type(entry["size"]) is not int or entry["size"] < 0:
            raise ArchiveError("archive entry size is invalid")
        if not isinstance(entry["sha256"], str) or not SHA256.fullmatch(entry["sha256"]):
            raise ArchiveError("archive entry hash is invalid")
        if entry["policy"] != "hash-only":
            try:
                content = contents[name]
            except KeyError as exc:
                raise ArchiveError(f"archive entry is missing: {name}") from exc
            if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ArchiveError(f"archive entry hash mismatch: {name}")
            expected_physical.add(name)
        if not name.startswith("audit/"):
            non_audit.append(entry)
    actual_files = set(contents)
    if actual_files != expected_physical:
        extra = sorted(actual_files - expected_physical)
        missing = sorted(expected_physical - actual_files)
        raise ArchiveError(f"archive physical file set mismatch extra={extra} missing={missing}")
    try:
        mission_bytes = contents["mission.jsonl"]
        archive_events = mission_state._events_from_bytes(
            mission_bytes,
            expected_mission_id=index["mission_id"],
            require_nonempty=True,
        )
        if mission_bytes != fleet_json.canonical_jsonl(archive_events):
            raise ArchiveError("archived mission ledger is not canonical JSONL")
    except KeyError as exc:
        raise ArchiveError("archive is missing its mission ledger") from exc
    except mission_state.MissionStateError as exc:
        raise ArchiveError(f"archived mission ledger is invalid: {exc}") from exc
    archived_state = mission_state.derive_state(archive_events)
    archived_manifest = _parse_manifest(contents["manifest"])
    lanes: list[str] = []
    not_attested: list[str] = []
    provider_contract = _router_provider_contract(
        contents["compiled-workflow.json"], archived_manifest
    )
    if not provider_contract:
        lanes.append(ROUTER_PRE_PROVIDER_LANE)
        not_attested.extend(ROUTER_PRE_PROVIDER_NOT_ATTESTED)
    compiled = _historical_compiled(
        contents["compiled-workflow.json"],
        archived_state,
        str(index["content_policy"]),
        provider_contract=provider_contract,
    )
    if "state.json" in contents:
        _historical_state(contents["state.json"], manifest=archived_manifest)
    if _is_manifest_contract_v2(archived_manifest):
        writer, branch = _archive_manifest_binding_v2(archived_manifest, archived_state)
        # The v2 manifest never recorded the published final; the Git evidence
        # verified below is the only final-commit authority for this lane.
        manifest_final_sha = index["final_sha"]
        lanes.append(MANIFEST_V2_LANE)
        not_attested.extend(MANIFEST_V2_NOT_ATTESTED)
    else:
        writer, branch, manifest_final_sha = _archive_manifest_binding(
            archived_manifest, archived_state
        )
    if (
        index["mission_id"] != archived_state["mission_id"]
        or index["base_sha"] != archived_state["base_sha"]
        or index["final_sha"] != manifest_final_sha
    ):
        raise ArchiveError("archive index does not match manifest and mission binding")
    audit_required = archived_state.get("approval") is not None
    audit_receipt_name = "audit/audit-verification.json"
    audit_receipt_sha256 = (
        hashlib.sha256(contents[audit_receipt_name]).hexdigest()
        if audit_receipt_name in contents else None
    )
    receipt_fields = set(receipt) if type(receipt) is dict else set()
    legacy_receipt = receipt_fields == ARCHIVE_RECEIPT_FIELDS_LEGACY
    current_receipt = receipt_fields == ARCHIVE_RECEIPT_FIELDS_CURRENT
    if (
        type(receipt) is not dict
        or not (legacy_receipt or current_receipt)
        or type(receipt.get("schema_version")) is not int
        or receipt.get("schema_version") != 1
        or receipt["mission_id"] != index["mission_id"]
        or receipt["created_at"] != index["created_at"]
        or type(receipt["entry_count"]) is not int
        or receipt["entry_count"] != len(index["entries"])
        or receipt["index_sha256"] != hashlib.sha256(index_bytes).hexdigest()
        or receipt["content_root_sha256"] != mission_state.sha256(
            sorted(non_audit, key=lambda item: item["path"])
        )
    ):
        raise ArchiveError("archive receipt does not match index")
    if current_receipt and (
        type(receipt["audit_required"]) is not bool
        or receipt["audit_required"] is not audit_required
        or receipt["audit_receipt_sha256"] != audit_receipt_sha256
        or (
            receipt["audit_receipt_sha256"] is not None
            and (
                type(receipt["audit_receipt_sha256"]) is not str
                or not SHA256.fullmatch(receipt["audit_receipt_sha256"])
            )
        )
    ):
        raise ArchiveError("archive receipt does not match audit evidence")
    try:
        commits_bytes = contents["writer/commits.json"]
    except KeyError as exc:
        raise ArchiveError("archive is missing writer metadata") from exc
    commits, object_format, final_tree_sha, declared_object_format = _writer_metadata(
        commits_bytes,
        index=index,
        writer=writer,
        branch=branch,
    )
    tree_name = "writer/final-tree.tar"
    if (
        tree_name in contents
        and _tree_hash_from_tar(contents[tree_name], object_format)
        != final_tree_sha
    ):
        raise ArchiveError("final tree does not reproduce final commit tree")
    if repo is not None:
        repo = repo.resolve()
        if _git(repo, "rev-parse", "--show-object-format") != object_format:
            raise ArchiveError("archive Git object format differs from repository")
        if _git(repo, "rev-parse", "--verify", index["final_sha"]) != index["final_sha"]:
            raise ArchiveError("final commit is not reachable in repository")
        if tree_name in contents:
            if not declared_object_format:
                # Archives produced before raw-tree serialization used
                # `git archive`. Keep their repository-backed verification
                # valid while new archives use the attribute-independent raw
                # Git representation above.
                expected_tar = _git_run_bounded(
                    repo, "archive", "--format=tar", str(index["final_sha"])
                )
            else:
                expected_tar = _raw_tree_tar(
                    repo, index["final_sha"], object_format
                )
            if expected_tar != contents[tree_name]:
                raise ArchiveError("final tree tar differs from final commit")
        patch_name = "writer/change.patch"
        if patch_name in contents:
            expected_patch = _git_run_bounded(
                repo,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--binary",
                "--full-index",
                index["base_sha"],
                index["final_sha"],
            )
            if expected_patch != contents[patch_name]:
                raise ArchiveError("binary patch does not match base/final commits")
    audit_ledger_name = f"audit/ledgers/{index['mission_id']}/a2a_ledger.jsonl"
    audit_ledger = root / audit_ledger_name
    if audit_required and audit_ledger_name not in contents:
        raise ArchiveError("assured archive is missing required signed audit evidence")
    if audit_ledger_name in contents:
        try:
            audit_policy = compiled["workflow"]["audit"]
            audit_mode = audit_policy["mode"]
            if audit_mode not in {"signed", "worm"}:
                raise ArchiveError("archived audit policy mode is invalid")
            require_worm = audit_mode == "worm"
            required_trust_scope = audit_policy.get("trust_scope")
            if (
                required_trust_scope is not None
                and required_trust_scope
                not in fleet_audit_client.audit.TRUST_SCOPES
            ):
                raise ArchiveError("archived audit trust scope is invalid")
        except (KeyError, TypeError) as exc:
            raise ArchiveError("archived audit policy is invalid") from exc
        audit_result = fleet_audit_client.verify_offline(
            audit_ledger,
            root / "audit" / "audit-verification.json",
            root / "audit" / "audit-signing-public.pem",
            root / "audit" / "anchor-receipts",
            require_worm=require_worm,
            required_trust_scope=required_trust_scope,
            _rooted=view.rooted,
            _directory_mode=None,
        )
        archive_events = [
            event
            for event in fleet_audit_client._chain_without_secret(
                audit_ledger,
                rooted=view.rooted,
                directory_mode=None,
            )
            if event.get("event_type") == "ArchiveContentRoot"
        ]
        if len(archive_events) != 1 or archive_events[0].get("subject_sha256") != receipt["content_root_sha256"]:
            raise ArchiveError("signed audit does not bind the archive content root")
    else:
        audit_result = None
    try:
        view.rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise ArchiveError("archive root binding changed during verification") from exc
    return {
        "mission_id": index["mission_id"],
        "entries": len(index["entries"]),
        "content_policy": index["content_policy"],
        "object_format": object_format,
        "target_repo": archived_state["target_repo"],
        "base_sha": index["base_sha"],
        "final_sha": index["final_sha"],
        "content_root_sha256": receipt["content_root_sha256"],
        "audit": audit_result,
        "valid": True,
        **({"historical_lanes": lanes, "not_attested": sorted(not_attested)} if lanes else {}),
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
                Path(args.output) if args.output else None,
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
