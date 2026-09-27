#!/usr/bin/env python3
"""Pure decoders for retained Mission archive contents and their historical lanes.

Parses and binds manifests, strict JSON documents, phase state, compiled workflows and
writer metadata from bytes already read by the caller. No filesystem, Git or process
access happens here; ``fleet_archive`` owns reading, building and verification flow.
"""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any

from fleet_archive_tree import ArchiveError, _git_oid, _object_format
import fleet_compiled
import fleet_json
import fleet_manifest
import fleet_state

GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
WRITER_FIELDS_LEGACY = {
    "schema_version", "base_sha", "final_sha", "final_tree_sha",
    "writer_instance", "branch", "commits",
}
WRITER_FIELDS_CURRENT = WRITER_FIELDS_LEGACY | {"object_format"}
WRITER_COMMIT_FIELDS = {"sha", "parents", "tree", "subject"}
# Explicit historical read lanes. Each reproduces the contract its archives were
# created under (verified by the reader of that date) and reports what it cannot
# attest. They never apply to archive creation, compilation or acceptance.
MANIFEST_V2_LANE = "manifest-contract-v2"
MANIFEST_V2_NOT_ATTESTED = (
    "writer_git_isolation",
    "quiescent_publication",
    "manifest_final_sha_binding",
)
MANIFEST_V3_PUBLICATION_FIELDS = ("git_isolation", "publication_state", "published_sha")
ROUTER_PRE_PROVIDER_LANE = "router-pre-provider-contract"
ROUTER_PRE_PROVIDER_NOT_ATTESTED = ("provider_submit_contract", "role_transport")


def _parse_manifest(content: bytes) -> dict[str, str]:
    try:
        rows = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ArchiveError("fleet manifest is not valid UTF-8") from exc
    values: dict[str, str] = {}
    for line_number, row in enumerate(rows, 1):
        if not row or row.startswith("#"):
            continue
        if "=" not in row:
            raise ArchiveError(f"malformed fleet manifest row {line_number}")
        key, value = row.split("=", 1)
        if not fleet_manifest.SAFE_KEY.fullmatch(key):
            raise ArchiveError(f"invalid fleet manifest key at row {line_number}")
        if key in values:
            raise ArchiveError(f"duplicate fleet manifest key: {key}")
        try:
            fleet_manifest._validate_value(key, value)
        except fleet_manifest.ManifestError as exc:
            raise ArchiveError(str(exc)) from exc
        values[key] = value
    if not values:
        raise ArchiveError("fleet manifest is empty")
    return values


def _archive_manifest_binding(
    manifest: dict[str, str], state: dict[str, Any]
) -> tuple[str | None, str | None, str]:
    """Cross-bind one archived manifest to its Mission and publication tuple."""

    if manifest.get("feature") != state.get("feature"):
        raise ArchiveError("fleet manifest feature does not match mission state")
    if manifest.get("mission_id") != state.get("mission_id"):
        raise ArchiveError("fleet manifest mission_id does not match mission state")
    if manifest.get("target_repo") != state.get("target_repo"):
        raise ArchiveError("fleet manifest target repository does not match mission state")
    if manifest.get("base_sha") != state.get("base_sha"):
        raise ArchiveError("fleet manifest base_sha does not match mission state")

    base_sha = str(state["base_sha"])
    writers = sorted(
        key.rsplit(".", 1)[0]
        for key, value in manifest.items()
        if key.endswith(".authority") and value == "write"
    )
    if len(writers) > 1:
        raise ArchiveError("manifest contains more than one writer")
    if not writers:
        return None, None, base_sha

    writer = writers[0]
    branch = manifest.get(f"{writer}.branch", "")
    final_sha = manifest.get(f"{writer}.final_sha", "")
    if not branch:
        raise ArchiveError("writer has no published branch")
    if manifest.get(f"{writer}.base_sha") != base_sha:
        raise ArchiveError("writer base_sha does not match mission state")
    if manifest.get("workspace.quiesced") != "1":
        raise ArchiveError("writer workspace is not quiescent")
    if manifest.get(f"{writer}.git_isolation") != "isolated-clone":
        raise ArchiveError("writer did not use an isolated Git clone")
    if manifest.get(f"{writer}.publication_state") != "published":
        raise ArchiveError("writer branch is not published")
    if (
        not GIT_SHA.fullmatch(final_sha)
        or len(final_sha) != len(base_sha)
        or manifest.get(f"{writer}.published_sha") != final_sha
    ):
        raise ArchiveError("writer published SHA metadata is invalid")
    return writer, branch, final_sha


def _is_manifest_contract_v2(manifest: dict[str, str]) -> bool:
    return manifest.get("manifest_contract_version") == "2" and "base_sha" not in manifest


def _archive_manifest_binding_v2(
    manifest: dict[str, str], state: dict[str, Any]
) -> tuple[str, str]:
    """Bind a manifest v2 archive through its writer; no v3 publication claims.

    Contract v2 predates the global base, isolated clones and CONTROL publication:
    the writer records its base and a boot-time final equal to that base. The
    archived Git evidence, not the manifest, attests the final commit.
    """

    if manifest.get("feature") != state.get("feature"):
        raise ArchiveError("fleet manifest feature does not match mission state")
    if manifest.get("mission_id") != state.get("mission_id"):
        raise ArchiveError("fleet manifest mission_id does not match mission state")
    if manifest.get("target_repo") != state.get("target_repo"):
        raise ArchiveError("fleet manifest target repository does not match mission state")
    writers = sorted(
        key.rsplit(".", 1)[0]
        for key, value in manifest.items()
        if key.endswith(".authority") and value == "write"
    )
    if len(writers) != 1:
        raise ArchiveError("manifest contract v2 binds its base only through one writer")
    writer = writers[0]
    if "workspace.quiesced" in manifest or any(
        f"{writer}.{field}" in manifest for field in MANIFEST_V3_PUBLICATION_FIELDS
    ):
        raise ArchiveError("manifest contract v2 contains contract v3 publication fields")
    base_sha = str(state["base_sha"])
    branch = manifest.get(f"{writer}.branch", "")
    if not branch or not manifest.get(f"{writer}.worktree"):
        raise ArchiveError("manifest contract v2 writer has no branch or worktree")
    if manifest.get(f"{writer}.base_sha") != base_sha:
        raise ArchiveError("writer base_sha does not match mission state")
    if manifest.get(f"{writer}.final_sha") != base_sha:
        raise ArchiveError("manifest contract v2 writer final_sha is not its boot-time base")
    return writer, branch


def _router_provider_contract(content: bytes, manifest: dict[str, str]) -> bool:
    """Select the archived router contract; both artifacts must agree on its shape."""

    try:
        value = fleet_json.loads(content)
    except fleet_json.FleetJSONError as exc:
        raise ArchiveError("archived compiled workflow is invalid") from exc
    if type(value) is not dict:
        return True  # the compiled reader rejects it with its own diagnosis
    router = value.get("router_snapshot")
    if value.get("schema_version") != 2 or type(router) is not dict or "providers" in router:
        return True
    if any(key.startswith("provider.") for key in manifest):
        raise ArchiveError("archived router and manifest mix provider contracts")
    return False


def _strict_json(
    content: bytes,
    *,
    where: str,
    require_canonical: bool = True,
) -> Any:
    try:
        value = fleet_json.loads(content)
        canonical = fleet_json.canonical_bytes(value) + b"\n"
    except fleet_json.FleetJSONError as exc:
        raise ArchiveError(f"{where} is invalid strict JSON") from exc
    if require_canonical and content != canonical:
        raise ArchiveError(f"{where} is not canonical LF-terminated JSON")
    return value


def _strict_jsonl(
    content: bytes,
    *,
    where: str,
    require_canonical: bool,
    require_nonempty: bool = False,
) -> list[Any]:
    try:
        values = fleet_json.load_jsonl(
            content,
            require_nonempty=require_nonempty,
            require_final_newline=True,
        )
        canonical = fleet_json.canonical_jsonl(values)
    except fleet_json.FleetJSONError as exc:
        raise ArchiveError(f"{where} is invalid strict JSONL") from exc
    if require_canonical and content != canonical:
        raise ArchiveError(f"{where} is not canonical JSONL")
    return values


def _historical_json_document(content: bytes, *, where: str) -> Any:
    """Accept current canonical JSON and the explicit legacy pretty encoding."""

    value = _strict_json(content, where=where, require_canonical=False)
    encodings = {fleet_json.canonical_bytes(value) + b"\n"}
    for ensure_ascii in (True, False):
        encodings.add(
            json.dumps(
                value,
                ensure_ascii=ensure_ascii,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    if content not in encodings:
        raise ArchiveError(f"{where} encoding is not an accepted durable format")
    return value


def _historical_compiled(
    content: bytes,
    archived_state: dict[str, Any],
    content_policy: str,
    *,
    provider_contract: bool = True,
) -> dict[str, Any]:
    try:
        compiled = fleet_compiled.loads(content, mode="read", provider_contract=provider_contract)
        compiled_content_policy = compiled["workflow"]["archive"]["content_policy"]
    except (fleet_compiled.CompiledError, KeyError, TypeError) as exc:
        raise ArchiveError("archived compiled workflow is invalid") from exc
    if (
        compiled["workflow_digest"] != archived_state["workflow_digest"]
        or compiled["compiled_digest"] != archived_state["compiled_digest"]
    ):
        raise ArchiveError("archived compiled workflow differs from mission ledger")
    if compiled_content_policy != content_policy:
        raise ArchiveError("archive content policy differs from compiled workflow")
    try:
        canonical = fleet_json.canonical_bytes(compiled) + b"\n"
    except fleet_json.FleetJSONError as exc:  # pragma: no cover - validate above owns it.
        raise ArchiveError("archived compiled workflow is not canonical") from exc
    if content != canonical:
        raise ArchiveError("archived compiled workflow bytes are not canonical")
    return compiled


def _historical_state(
    content: bytes,
    *,
    manifest: dict[str, str],
) -> dict[str, Any]:
    """Validate current state and the one explicit historical V1 archive lane."""

    value = _strict_json(content, where="archived phase state", require_canonical=False)
    if type(value) is not dict:
        raise ArchiveError("archived phase state must be an object")
    schema_version = value.get("schema_version")
    if type(schema_version) is not int:
        raise ArchiveError("archived phase state schema_version is invalid")
    canonical = fleet_json.canonical_bytes(value) + b"\n"
    if schema_version == fleet_state.STATE_SCHEMA_VERSION:
        try:
            fleet_state.validate_state(value, manifest)
        except fleet_state.PhaseStateError as exc:
            raise ArchiveError(f"archived phase state is invalid: {exc}") from exc
        if content != canonical:
            raise ArchiveError("current archived phase state is not canonical")
        return value
    if schema_version != 1 or set(value) != fleet_state.STATE_FIELDS_V1:
        raise ArchiveError("archived phase state fields are invalid")
    historical_encodings = {canonical}
    for ensure_ascii in (True, False):
        historical_encodings.add(
            json.dumps(
                value,
                ensure_ascii=ensure_ascii,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    if content not in historical_encodings:
        raise ArchiveError("historical archived phase state encoding is invalid")
    if value.get("feature") != manifest.get("feature"):
        raise ArchiveError("archived phase state feature differs from manifest")
    try:
        phases = fleet_state.configured_phases(manifest)
    except fleet_state.PhaseStateError as exc:
        raise ArchiveError(f"archived phase state manifest is invalid: {exc}") from exc
    active_phase = value.get("active_phase")
    history = value.get("history")
    if (
        type(active_phase) is not str
        or active_phase not in phases
        or type(history) is not list
        or len(history) > len(phases)
    ):
        raise ArchiveError("historical archived phase state is invalid")
    seen: list[str] = []
    previous_timestamp: datetime | None = None
    try:
        for number, entry in enumerate(history, 1):
            entry_fields = set(entry) if type(entry) is dict else set()
            if "approval_event_sha256" in entry_fields:
                validation_mode, mission_bound = "assured", True
            elif "approved_by" in entry_fields:
                validation_mode, mission_bound = "guided", False
            else:
                validation_mode, mission_bound = "autonomous", False
            phase, timestamp = fleet_state._validate_history_entry(
                entry,
                number=number,
                phases=phases,
                mode=validation_mode,
                mission_bound=mission_bound,
            )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise fleet_state.PhaseStateError(
                    "fleet state history timestamps are not strictly monotonic"
                )
            previous_timestamp = timestamp
            seen.append(phase)
    except fleet_state.PhaseStateError as exc:
        raise ArchiveError(f"historical archived phase state is invalid: {exc}") from exc
    if history and seen[-1] != active_phase:
        raise ArchiveError("historical archived phase state head is inconsistent")
    if len(seen) != len(set(seen)):
        raise ArchiveError("historical archived phase state repeats a phase")
    indexes = [phases.index(phase) for phase in seen]
    if any(current != previous + 1 for previous, current in zip(indexes, indexes[1:])):
        raise ArchiveError("historical archived phase state skips a phase")
    return value


def _writer_metadata(
    content: bytes,
    *,
    index: dict[str, Any],
    writer: str | None,
    branch: str | None,
) -> tuple[dict[str, Any], str, str, bool]:
    commits = _strict_json(content, where="writer metadata")
    if type(commits) is not dict or set(commits) not in {
        frozenset(WRITER_FIELDS_LEGACY),
        frozenset(WRITER_FIELDS_CURRENT),
    }:
        raise ArchiveError("writer metadata fields are invalid")
    if type(commits.get("schema_version")) is not int or commits["schema_version"] != 1:
        raise ArchiveError("writer metadata fields are invalid")
    declared = "object_format" in commits
    object_format = commits.get("object_format")
    if not declared:
        object_format = "sha1" if len(index["final_sha"]) == 40 else "sha256"
    if type(object_format) is not str:
        raise ArchiveError("writer object format is invalid")
    _object_format(object_format)
    if (
        commits.get("base_sha") != index["base_sha"]
        or commits.get("final_sha") != index["final_sha"]
        or commits.get("writer_instance") != writer
        or commits.get("branch") != branch
    ):
        raise ArchiveError("writer metadata does not match archive binding")
    _git_oid(index["base_sha"], object_format, where="archive base_sha")
    _git_oid(index["final_sha"], object_format, where="archive final_sha")
    final_tree_sha = _git_oid(
        commits.get("final_tree_sha"),
        object_format,
        where="archive final tree",
    )
    rows = commits.get("commits")
    if type(rows) is not list:
        raise ArchiveError("writer commit list is invalid")
    commit_ids: list[str] = []
    for number, row in enumerate(rows, 1):
        if type(row) is not dict or set(row) != WRITER_COMMIT_FIELDS:
            raise ArchiveError(f"writer commit {number} fields are invalid")
        sha = _git_oid(row.get("sha"), object_format, where=f"writer commit {number}")
        _git_oid(row.get("tree"), object_format, where=f"writer commit {number} tree")
        parents = row.get("parents")
        if type(parents) is not list or any(type(parent) is not str for parent in parents):
            raise ArchiveError(f"writer commit {number} parents are invalid")
        for parent in parents:
            _git_oid(parent, object_format, where=f"writer commit {number} parent")
        if len(parents) != len(set(parents)) or type(row.get("subject")) is not str:
            raise ArchiveError(f"writer commit {number} metadata is invalid")
        commit_ids.append(sha)
    if len(commit_ids) != len(set(commit_ids)):
        raise ArchiveError("writer commit list repeats a commit")
    if index["base_sha"] == index["final_sha"]:
        if rows:
            raise ArchiveError("writer commit list is nonempty for an unchanged tree")
    elif not rows or commit_ids[-1] != index["final_sha"]:
        raise ArchiveError("writer commit list does not end at final_sha")
    return commits, object_format, final_tree_sha, declared
