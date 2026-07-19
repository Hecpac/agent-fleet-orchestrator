#!/usr/bin/env python3
"""Quiesce one main Mission fleet and preserve it for assured-fleet boot.

The read-only ``preflight`` command is deliberately separate from ``hold``.
``hold`` captures one brief ledger-locked snapshot, then retains a pinned
Mission mutation barrier until descriptor-rooted commit finishes. Readers can
continue, but no new admission can race the main-fleet shutdown.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import signal
import sys
from typing import Any

import fleet_compiled
import fleet_json
import fleet_manifest
import fleet_mission
import fleet_mission_state
import fleet_safe_paths
import fleet_state


SCHEMA_VERSION = 1
SAFE_FEATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_METADATA_BYTES = 8 * 1024 * 1024
HANDOFF_FILES = (
    "state.json",
    "ledger.jsonl",
    "dialogue.jsonl",
    "dialogue-control.jsonl",
    "assurance-control.jsonl",
    "verification-receipt.json",
    "assurance-receipt.json",
    "manifest",
)
REQUIRED_KINDS = {"manifest", "state.json", "ledger.jsonl"}


class HandoffError(RuntimeError):
    """The requested handoff is invalid or unsafe."""


class HandoffTransient(HandoffError):
    """A valid handoff cannot yet be completed safely."""


class HandoffAbsent(HandoffError):
    """No live or recoverable handoff exists for this feature."""


def _canonical_line(value: Any) -> bytes:
    return fleet_json.canonical_bytes(value) + b"\n"


def _loads_canonical(raw: bytes, where: str) -> dict[str, Any]:
    try:
        value = fleet_json.loads(raw)
    except fleet_json.FleetJSONError as exc:
        raise HandoffError(f"invalid {where}: {exc}") from exc
    if not isinstance(value, dict) or raw != _canonical_line(value):
        raise HandoffError(f"{where} is not a canonical JSON object")
    return value


def _validate_feature(feature: str) -> str:
    if not isinstance(feature, str) or not SAFE_FEATURE.fullmatch(feature):
        raise HandoffError("invalid feature")
    return feature


def _manifest_leaf(feature: str) -> str:
    return f"fleet-{feature}.manifest"


def _source_leaf(feature: str, kind: str) -> str:
    if kind == "manifest":
        return _manifest_leaf(feature)
    return f"fleet-{feature}.{kind}"


def _destination_leaf(feature: str, mission_id: str, kind: str) -> Path:
    return (
        Path("missions")
        / mission_id
        / "assurance-handoff"
        / "main-runtime"
        / _source_leaf(feature, kind)
    )


def _intent_relative(mission_id: str) -> Path:
    return Path("missions") / mission_id / "assurance-handoff" / "intent.json"


def _receipt_relative(mission_id: str) -> Path:
    return Path("missions") / mission_id / "assurance-handoff" / "receipt.json"


def _mission_metadata_modes() -> tuple[int, int, int]:
    return (0o700, 0o700, 0o700)


def _read_metadata_optional(
    rooted: fleet_safe_paths.RootedFS, relative: Path
) -> dict[str, Any] | None:
    try:
        raw = rooted.read_regular_optional(
            relative,
            directory_modes=_mission_metadata_modes(),
            file_mode=0o600,
            max_bytes=MAX_METADATA_BYTES,
            require_single_link=True,
        )
    except fleet_safe_paths.SafePathError as exc:
        # A missing handoff directory is ordinary before the first handoff.
        if "rooted directory is missing" in str(exc):
            return None
        raise HandoffError(f"unsafe handoff metadata: {exc}") from exc
    return None if raw is None else _loads_canonical(raw, str(relative))


def _read_live_manifest_optional(
    rooted: fleet_safe_paths.RootedFS, feature: str
) -> dict[str, str] | None:
    leaf = _manifest_leaf(feature)
    try:
        raw = rooted.read_regular_optional(
            leaf,
            directory_modes=(),
            file_mode=0o600,
            max_bytes=fleet_state.MAX_MANIFEST_BYTES,
            require_single_link=True,
        )
    except fleet_safe_paths.SafePathError as exc:
        raise HandoffError(f"unsafe active manifest: {exc}") from exc
    if raw is None:
        return None
    try:
        manifest = fleet_manifest.normalize(fleet_manifest.parse_bytes(raw))
    except fleet_manifest.ManifestError as exc:
        raise HandoffError(f"invalid active manifest: {exc}") from exc
    if manifest.get("feature") != feature:
        raise HandoffError("active manifest feature binding mismatch")
    return manifest


def _discover_mission_id(
    rooted: fleet_safe_paths.RootedFS,
    feature: str,
    requested: str | None,
    manifest: dict[str, str] | None,
) -> str:
    if requested:
        try:
            normalized = fleet_mission_state.normalize_uuid(requested, "mission_id")
        except fleet_mission_state.MissionStateError as exc:
            raise HandoffError(str(exc)) from exc
        if normalized != requested:
            raise HandoffError("mission_id must use canonical UUID spelling")
        if manifest is not None and manifest.get("mission_id") != normalized:
            raise HandoffError("requested Mission differs from active manifest")
        return normalized
    if manifest is not None:
        mission_id = manifest.get("mission_id", "")
        try:
            normalized = fleet_mission_state.normalize_uuid(mission_id, "mission_id")
        except fleet_mission_state.MissionStateError as exc:
            raise HandoffError("active fleet is not Mission-bound") from exc
        if normalized != mission_id:
            raise HandoffError("active manifest Mission binding is not canonical")
        return normalized
    try:
        names = rooted.list_directory("missions", directory_modes=(0o700,))
    except fleet_safe_paths.SafePathError as exc:
        if "rooted directory is missing" in str(exc):
            raise HandoffAbsent("no Mission handoff exists") from exc
        raise HandoffError(f"unsafe Mission store: {exc}") from exc
    candidates: list[str] = []
    for name in names:
        try:
            if fleet_mission_state.normalize_uuid(name, "mission_id") != name:
                continue
        except fleet_mission_state.MissionStateError:
            continue
        intent = _read_metadata_optional(rooted, _intent_relative(name))
        receipt = _read_metadata_optional(rooted, _receipt_relative(name))
        if any(
            isinstance(record, dict) and record.get("feature") == feature
            for record in (intent, receipt)
        ):
            candidates.append(name)
    if not candidates:
        raise HandoffAbsent("no live or recoverable assurance handoff exists")
    if len(candidates) != 1:
        raise HandoffError("feature matches more than one Mission handoff")
    return candidates[0]


def _parse_expiry(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HandoffError("assurance approval expiry is invalid") from exc
    if parsed.tzinfo is None:
        raise HandoffError("assurance approval expiry lacks timezone")
    return parsed.astimezone(timezone.utc)


def _validate_mission_state(
    current: dict[str, Any], feature: str, mission_id: str
) -> dict[str, Any]:
    if current.get("mission_id") != mission_id or current.get("feature") != feature:
        raise HandoffError("Mission identity differs from the requested fleet")
    if current.get("status") != "assurance_approved":
        raise HandoffError("Mission must be exactly assurance_approved")
    if current.get("terminal") is not None:
        raise HandoffError("terminal Mission cannot enter assurance handoff")
    approval = current.get("approval")
    if not isinstance(approval, dict):
        raise HandoffError("Mission lacks an active assurance approval")
    if (
        approval.get("decision") != "approved"
        or approval.get("workflow_digest") != current.get("workflow_digest")
        or approval.get("scope") != current.get("target_repo")
        or not fleet_mission_state.SHA256.fullmatch(
            str(approval.get("event_sha256", ""))
        )
    ):
        raise HandoffError("active assurance approval binding is invalid")
    if datetime.now(timezone.utc) >= _parse_expiry(approval.get("expires_at")):
        raise HandoffError("assurance approval expired before handoff")
    admissions = current.get("admissions")
    lead_id = current.get("lead_admission_id")
    if not isinstance(admissions, dict) or not isinstance(lead_id, str):
        raise HandoffError("Mission has no finalized Lead admission")
    lead = admissions.get(lead_id)
    if (
        not isinstance(lead, dict)
        or lead.get("run_kind") != "lead"
        or lead.get("phase") != "finalized"
        or lead.get("active") is not False
    ):
        raise HandoffError("Lead admission must be finalized and inactive")
    if (
        current.get("active_writer") is not None
        or current.get("active_recipients") != {}
        or current.get("run_claims") != {}
        or current.get("active_delegations") != 0
        or any(
            not isinstance(admission, dict) or admission.get("active") is not False
            for admission in admissions.values()
        )
    ):
        raise HandoffError("assurance handoff requires zero active admissions")
    return approval


def _validate_manifest(
    manifest: dict[str, str],
    current: dict[str, Any],
    compiled: dict[str, Any],
    feature: str,
    mission_id: str,
) -> None:
    if (
        manifest.get("feature") != feature
        or manifest.get("mission_id") != mission_id
        or manifest.get("mode") != "autonomous"
    ):
        raise HandoffError("handoff requires the exact autonomous main fleet")
    if manifest.get("target_repo") != current.get("target_repo"):
        raise HandoffError("main fleet target differs from Mission target")
    if manifest.get("compiled_digest") != current.get("compiled_digest"):
        raise HandoffError("main fleet compiled workflow binding drifted")
    resolved = compiled.get("resolved")
    expected_preset = resolved.get("preset") if isinstance(resolved, dict) else None
    if (
        not isinstance(expected_preset, str)
        or manifest.get("preset") != expected_preset
    ):
        raise HandoffError("main fleet preset differs from compiled workflow")


def _validate_intent(
    intent: dict[str, Any],
    feature: str,
    mission_id: str,
    current: dict[str, Any],
    events: list[dict[str, Any]],
) -> None:
    base_fields = {
        "schema_version",
        "status",
        "feature",
        "mission_id",
        "mission_head_sha256",
        "approval_event_sha256",
        "workspace_uuid",
        "files",
    }
    status = intent.get("status")
    expected_fields = base_fields | ({"items"} if status == "committing" else set())
    if status not in {"prepared", "committing"} or set(intent) != expected_fields:
        raise HandoffError("handoff intent schema is invalid")
    if (
        intent.get("schema_version") != SCHEMA_VERSION
        or intent.get("feature") != feature
        or intent.get("mission_id") != mission_id
        or intent.get("files") != list(HANDOFF_FILES)
        or not isinstance(intent.get("workspace_uuid"), str)
        or not intent["workspace_uuid"]
    ):
        raise HandoffError("handoff intent binding is invalid")
    _validate_recorded_approval_binding(
        current,
        events,
        intent.get("mission_head_sha256"),
        intent.get("approval_event_sha256"),
        where="handoff intent",
    )
    if status == "committing":
        _validate_items(intent.get("items"), feature, mission_id)


def _validate_items(value: Any, feature: str, mission_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(HANDOFF_FILES):
        raise HandoffError("handoff item inventory is invalid")
    result: list[dict[str, Any]] = []
    for expected_kind, item in zip(HANDOFF_FILES, value, strict=True):
        if not isinstance(item, dict) or set(item) != {
            "kind",
            "source",
            "destination",
            "present",
            "sha256",
            "size",
        }:
            raise HandoffError("handoff item schema is invalid")
        present = item.get("present")
        if (
            item.get("kind") != expected_kind
            or item.get("source") != _source_leaf(feature, expected_kind)
            or item.get("destination")
            != str(_destination_leaf(feature, mission_id, expected_kind))
            or type(present) is not bool
        ):
            raise HandoffError("handoff item binding is invalid")
        if present:
            if (
                not fleet_mission_state.SHA256.fullmatch(str(item.get("sha256", "")))
                or type(item.get("size")) is not int
                or item["size"] < 0
            ):
                raise HandoffError("handoff item digest is invalid")
        elif item.get("sha256") is not None or item.get("size") is not None:
            raise HandoffError("absent handoff item carries content metadata")
        if expected_kind in REQUIRED_KINDS and not present:
            raise HandoffError(f"required handoff item is absent: {expected_kind}")
        result.append(item)
    return result


def _validate_receipt(
    receipt: dict[str, Any],
    feature: str,
    mission_id: str,
    current: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    if set(receipt) != {
        "schema_version",
        "status",
        "feature",
        "mission_id",
        "mission_head_sha256",
        "approval_event_sha256",
        "workspace_uuid",
        "completed_at",
        "items",
    }:
        raise HandoffError("handoff receipt schema is invalid")
    if (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("status") != "complete"
        or receipt.get("feature") != feature
        or receipt.get("mission_id") != mission_id
        or not isinstance(receipt.get("workspace_uuid"), str)
    ):
        raise HandoffError("handoff receipt binding is invalid")
    completed_at = _parse_expiry(receipt.get("completed_at"))
    _validate_recorded_approval_binding(
        current,
        events,
        receipt.get("mission_head_sha256"),
        receipt.get("approval_event_sha256"),
        where="handoff receipt",
        completed_at=completed_at,
    )
    _validate_items(receipt.get("items"), feature, mission_id)
    return _result(feature, mission_id)


def _validate_completed_layout(
    rooted: fleet_safe_paths.RootedFS,
    receipt: dict[str, Any],
    feature: str,
    mission_id: str,
) -> None:
    items = _validate_items(receipt["items"], feature, mission_id)
    runtime_relative = (
        Path("missions") / mission_id / "assurance-handoff" / "main-runtime"
    )
    try:
        runtime_entries = rooted.list_directory(
            runtime_relative,
            directory_modes=(0o700, 0o700, 0o700, 0o700),
        )
    except fleet_safe_paths.SafePathError as exc:
        raise HandoffError(f"unsafe completed handoff directory: {exc}") from exc
    expected_entries = sorted(item["source"] for item in items if item["present"])
    if runtime_entries != expected_entries:
        raise HandoffError("completed main-runtime entries differ from receipt")
    for item in items:
        try:
            source = rooted.read_regular_optional(
                item["source"],
                directory_modes=(),
                file_mode=0o600,
                max_bytes=None,
                require_single_link=True,
            )
            destination = rooted.read_regular_optional(
                Path(item["destination"]),
                directory_modes=(0o700, 0o700, 0o700, 0o700),
                file_mode=0o600,
                max_bytes=None,
                require_single_link=True,
            )
        except fleet_safe_paths.SafePathError as exc:
            raise HandoffError(f"unsafe completed handoff layout: {exc}") from exc
        if source is not None:
            raise HandoffError(f"completed handoff source returned: {item['kind']}")
        if not item["present"]:
            if destination is not None:
                raise HandoffError(
                    f"absent handoff destination appeared: {item['kind']}"
                )
            continue
        if destination is None:
            raise HandoffError(f"completed handoff item is absent: {item['kind']}")
        if (
            len(destination) != item["size"]
            or hashlib.sha256(destination).hexdigest() != item["sha256"]
        ):
            raise HandoffError(f"completed handoff item drifted: {item['kind']}")


def _validate_receipt_intent_pair(
    receipt: dict[str, Any], intent: dict[str, Any]
) -> None:
    if intent.get("status") != "committing":
        raise HandoffError("completed receipt lacks a committing intent")
    for field in (
        "feature",
        "mission_id",
        "mission_head_sha256",
        "approval_event_sha256",
        "workspace_uuid",
        "items",
    ):
        if receipt.get(field) != intent.get(field):
            raise HandoffError(f"handoff receipt differs from intent: {field}")


def _result(feature: str, mission_id: str) -> dict[str, Any]:
    return {
        "feature": feature,
        "mission_id": mission_id,
        "receipt": str(_receipt_relative(mission_id)),
        "status": "ready",
    }


def _load_mission_readonly(runs_dir: Path, mission_id: str) -> dict[str, Any]:
    try:
        return fleet_mission.load_state(runs_dir, mission_id)
    except (fleet_mission_state.MissionStateError, OSError) as exc:
        raise HandoffError(f"cannot verify Mission state: {exc}") from exc


def _load_events_readonly(runs_dir: Path, mission_id: str) -> list[dict[str, Any]]:
    try:
        events = fleet_mission_state.read_events(
            fleet_mission_state.ledger_path(runs_dir, mission_id),
            expected_mission_id=mission_id,
        )
        fleet_mission_state.verify_events(events)
        return events
    except (fleet_mission_state.MissionStateError, OSError) as exc:
        raise HandoffError(f"cannot verify Mission event lineage: {exc}") from exc


def _validate_recorded_approval_binding(
    current: dict[str, Any],
    events: list[dict[str, Any]],
    recorded_head: Any,
    recorded_approval: Any,
    *,
    where: str,
    completed_at: datetime | None = None,
) -> None:
    if not fleet_mission_state.SHA256.fullmatch(
        str(recorded_head or "")
    ) or not fleet_mission_state.SHA256.fullmatch(str(recorded_approval or "")):
        raise HandoffError(f"{where} approval binding is invalid")
    head_index = next(
        (
            index
            for index, event in enumerate(events)
            if event.get("event_sha256") == recorded_head
        ),
        None,
    )
    if head_index is None:
        raise HandoffError(f"{where} Mission head is not in the durable lineage")
    try:
        recorded_state = fleet_mission_state.derive_state(events[: head_index + 1])
    except fleet_mission_state.MissionStateError as exc:
        raise HandoffError(f"{where} Mission prefix is invalid: {exc}") from exc
    approval = recorded_state.get("approval")
    if (
        recorded_state.get("status") != "assurance_approved"
        or not isinstance(approval, dict)
        or approval.get("event_sha256") != recorded_approval
        or approval.get("workflow_digest") != current.get("workflow_digest")
        or approval.get("scope") != current.get("target_repo")
    ):
        raise HandoffError(f"{where} does not bind an approved Mission prefix")
    if any(
        event.get("kind") != "assurance_approval_renewed"
        for event in events[head_index + 1 :]
    ):
        raise HandoffError(f"{where} was followed by a non-renewal Mission mutation")
    if completed_at is not None:
        recorded_at = fleet_mission_state.parse_timestamp(
            events[head_index]["timestamp"], f"{where} Mission head timestamp"
        )
        if completed_at < recorded_at:
            raise HandoffError(f"{where} predates its recorded Mission head")
    if completed_at is not None and completed_at >= _parse_expiry(
        approval.get("expires_at")
    ):
        raise HandoffError(f"{where} completed after its approval expired")


def _load_compiled_readonly(
    runs_dir: Path, mission_id: str, current: dict[str, Any]
) -> dict[str, Any]:
    try:
        compiled, rebound = fleet_mission.load_mission_compiled(
            runs_dir, mission_id, mode="effect"
        )
    except (fleet_mission.MissionError, fleet_mission_state.MissionStateError) as exc:
        raise HandoffError(f"cannot verify compiled main fleet: {exc}") from exc
    if rebound.get("head_sha256") != current.get("head_sha256"):
        raise HandoffTransient("Mission changed during read-only handoff preflight")
    return compiled


def _load_compiled_locked(
    rooted: fleet_safe_paths.RootedFS,
    mission_id: str,
    current: dict[str, Any],
) -> dict[str, Any]:
    """Bind compiled bytes to the snapshot already held by MissionTransaction."""

    relative = Path("missions") / mission_id / "compiled-workflow.json"
    try:
        raw = rooted.read_regular(
            relative,
            directory_modes=(0o700, 0o700),
            file_mode=0o600,
            max_bytes=fleet_mission.MAX_COMPILED_WORKFLOW_BYTES,
            require_single_link=True,
        )
        compiled = fleet_compiled.loads(raw, mode="effect")
        if raw != fleet_mission_state.canonical_bytes(compiled) + b"\n":
            raise HandoffError("durable compiled workflow bytes are not canonical")
        if (
            compiled["workflow_digest"] != current.get("workflow_digest")
            or compiled["compiled_digest"] != current.get("compiled_digest")
        ):
            raise HandoffError("compiled workflow differs from Mission snapshot")
        rooted.assert_root_binding()
        return compiled
    except HandoffError:
        raise
    except (fleet_compiled.CompiledError, fleet_safe_paths.SafePathError) as exc:
        raise HandoffError(f"cannot verify locked compiled main fleet: {exc}") from exc


def _validate_runtime_sources(
    rooted: fleet_safe_paths.RootedFS,
    feature: str,
    live_state: dict[str, Any],
) -> None:
    for kind in HANDOFF_FILES:
        source = _source_leaf(feature, kind)
        try:
            content = rooted.read_regular_optional(
                source,
                directory_modes=(),
                file_mode=0o600,
                max_bytes=(
                    fleet_state.MAX_STATE_BYTES if kind == "state.json" else None
                ),
                require_single_link=True,
            )
        except fleet_safe_paths.SafePathError as exc:
            raise HandoffError(f"unsafe main runtime source {source}: {exc}") from exc
        if kind in REQUIRED_KINDS and content is None:
            raise HandoffError(f"required main runtime source is absent: {source}")
        if kind == "state.json" and content != fleet_state._state_bytes(live_state):
            raise HandoffError("main runtime phase state is not canonical JSON")


def _validate_empty_destination(
    rooted: fleet_safe_paths.RootedFS, mission_id: str
) -> None:
    parts = ("missions", mission_id, "assurance-handoff", "main-runtime")
    try:
        directory_fd = rooted._open_directory_chain(
            parts, (0o700, 0o700, 0o700, 0o700), create=False
        )
    except fleet_safe_paths.SafePathError as exc:
        if "rooted directory is missing" in str(exc):
            return
        raise HandoffError(f"unsafe main-runtime destination: {exc}") from exc
    try:
        if os.listdir(directory_fd):
            raise HandoffError("prepared main-runtime destination is not empty")
    finally:
        os.close(directory_fd)


def preflight(
    runs_dir: Path, feature: str, mission_id: str | None = None
) -> dict[str, Any]:
    """Perform an entirely read-only handoff eligibility/recovery probe."""

    feature = _validate_feature(feature)
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            manifest = _read_live_manifest_optional(rooted, feature)
            resolved = _discover_mission_id(rooted, feature, mission_id, manifest)
            current = _load_mission_readonly(Path(rooted.root), resolved)
            _validate_mission_state(current, feature, resolved)
            events = _load_events_readonly(Path(rooted.root), resolved)
            if events[-1]["event_sha256"] != current.get("head_sha256"):
                raise HandoffTransient("Mission changed during handoff preflight")
            compiled = _load_compiled_readonly(Path(rooted.root), resolved, current)
            receipt = _read_metadata_optional(rooted, _receipt_relative(resolved))
            intent = _read_metadata_optional(rooted, _intent_relative(resolved))
            if receipt is not None:
                if manifest is not None:
                    raise HandoffError(
                        "completed handoff conflicts with a live main manifest"
                    )
                if intent is None:
                    raise HandoffError("completed handoff intent is absent")
                _validate_intent(intent, feature, resolved, current, events)
                result = _validate_receipt(receipt, feature, resolved, current, events)
                _validate_receipt_intent_pair(receipt, intent)
                _validate_completed_layout(rooted, receipt, feature, resolved)
                return {
                    "feature": feature,
                    "mission_id": resolved,
                    "mode": "complete",
                    "result": result,
                    "workspace_uuid": receipt["workspace_uuid"],
                }
            if intent is not None:
                _validate_intent(intent, feature, resolved, current, events)
                if intent["status"] == "committing":
                    return {
                        "feature": feature,
                        "mission_id": resolved,
                        "mode": "recover",
                        "workspace_uuid": intent["workspace_uuid"],
                    }
            _validate_empty_destination(rooted, resolved)
            if manifest is None:
                raise HandoffError("prepared handoff has no active main manifest")
            try:
                live_manifest, live_state = fleet_state.load_live(
                    Path(rooted.root) / _manifest_leaf(feature)
                )
            except fleet_state.PhaseStateError as exc:
                raise HandoffError(f"invalid active fleet state: {exc}") from exc
            _validate_manifest(live_manifest, current, compiled, feature, resolved)
            _validate_runtime_sources(rooted, feature, live_state)
            return {
                "feature": feature,
                "mission_id": resolved,
                "mode": "active",
                "workspace_uuid": live_manifest["workspace_uuid"],
            }
    except HandoffError:
        raise
    except fleet_safe_paths.SafePathError as exc:
        raise HandoffError(f"unsafe fleet runtime: {exc}") from exc


def _prepared_intent(
    feature: str,
    mission_id: str,
    current: dict[str, Any],
    manifest: dict[str, str],
) -> dict[str, Any]:
    workspace_uuid = manifest.get("workspace_uuid", "")
    if not workspace_uuid:
        raise HandoffError("main fleet lacks workspace_uuid")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "prepared",
        "feature": feature,
        "mission_id": mission_id,
        "mission_head_sha256": current["head_sha256"],
        "approval_event_sha256": current["approval"]["event_sha256"],
        "workspace_uuid": workspace_uuid,
        "files": list(HANDOFF_FILES),
    }


def _write_intent(
    rooted: fleet_safe_paths.RootedFS,
    mission_id: str,
    intent: dict[str, Any],
    *,
    replace: bool,
) -> None:
    method = rooted.replace_regular if replace else rooted.atomic_write
    try:
        method(
            _intent_relative(mission_id),
            _canonical_line(intent),
            directory_modes=_mission_metadata_modes(),
            file_mode=0o600,
        )
    except fleet_safe_paths.SafePathError as exc:
        raise HandoffTransient(f"cannot publish durable handoff intent: {exc}") from exc


def _prepare_locked(
    rooted: fleet_safe_paths.RootedFS,
    current: dict[str, Any],
    events: list[dict[str, Any]],
    feature: str,
    mission_id: str,
) -> dict[str, Any]:
    approval = _validate_mission_state(current, feature, mission_id)
    del approval
    compiled = _load_compiled_locked(rooted, mission_id, current)
    try:
        live_manifest, live_state = fleet_state.load_live(
            Path(rooted.root) / _manifest_leaf(feature)
        )
    except fleet_state.PhaseStateError as exc:
        raise HandoffError(f"invalid active fleet state: {exc}") from exc
    _validate_manifest(live_manifest, current, compiled, feature, mission_id)
    _validate_runtime_sources(rooted, feature, live_state)
    existing = _read_metadata_optional(rooted, _intent_relative(mission_id))
    requested = _prepared_intent(feature, mission_id, current, live_manifest)
    if existing is None:
        _write_intent(rooted, mission_id, requested, replace=False)
        return requested
    _validate_intent(existing, feature, mission_id, current, events)
    if existing["status"] != "prepared":
        raise HandoffTransient("handoff commit already began; recover it instead")
    immutable_existing = {
        **existing,
        "mission_head_sha256": requested["mission_head_sha256"],
        "approval_event_sha256": requested["approval_event_sha256"],
    }
    if immutable_existing != requested:
        raise HandoffError("prepared handoff intent conflicts with live fleet")
    if existing != requested:
        _write_intent(rooted, mission_id, requested, replace=True)
        return requested
    return existing


def _snapshot_items(
    rooted: fleet_safe_paths.RootedFS, feature: str, mission_id: str
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for kind in HANDOFF_FILES:
        source = _source_leaf(feature, kind)
        try:
            content = rooted.read_regular_optional(
                source,
                directory_modes=(),
                file_mode=0o600,
                max_bytes=None,
                require_single_link=True,
            )
        except fleet_safe_paths.SafePathError as exc:
            raise HandoffError(f"unsafe handoff source {source}: {exc}") from exc
        present = content is not None
        if kind in REQUIRED_KINDS and not present:
            raise HandoffError(f"required handoff source is absent: {source}")
        destination = _destination_leaf(feature, mission_id, kind)
        items.append(
            {
                "kind": kind,
                "source": source,
                "destination": str(destination),
                "present": present,
                "sha256": hashlib.sha256(content).hexdigest() if present else None,
                "size": len(content) if present else None,
            }
        )
    return items


def _leaf_optional(
    rooted: fleet_safe_paths.RootedFS,
    relative: Path | str,
    directory_modes: tuple[int, ...],
    *,
    create_parent: bool,
) -> tuple[int, tuple[str, ...], bytes | None]:
    parent_fd, parts = rooted._parent_descriptor(
        relative, directory_modes=directory_modes, create=create_parent
    )
    logical = "/".join(parts)
    try:
        try:
            os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return parent_fd, parts, None
        content = rooted._read_leaf(
            parent_fd,
            parts[-1],
            relative=logical,
            file_mode=0o600,
            max_bytes=None,
            require_single_link=True,
        )
        return parent_fd, parts, content
    except Exception:
        os.close(parent_fd)
        raise


def _move_item(rooted: fleet_safe_paths.RootedFS, item: dict[str, Any]) -> None:
    source_fd = destination_fd = None
    try:
        source_fd, source_parts, source = _leaf_optional(
            rooted, item["source"], (), create_parent=False
        )
        destination_fd, destination_parts, destination = _leaf_optional(
            rooted,
            Path(item["destination"]),
            (0o700, 0o700, 0o700, 0o700),
            create_parent=True,
        )
        expected = item["present"]
        if not expected:
            if source is not None or destination is not None:
                raise HandoffError(f"absent handoff item appeared: {item['kind']}")
            return
        digest = item["sha256"]
        size = item["size"]
        if source is not None and destination is not None:
            raise HandoffError(
                f"handoff source and destination both exist: {item['kind']}"
            )
        content = source if source is not None else destination
        if content is None:
            raise HandoffError(f"handoff item was lost: {item['kind']}")
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise HandoffError(f"handoff item content drifted: {item['kind']}")
        if source is not None:
            try:
                fleet_safe_paths._rename_noreplace(
                    source_parts[-1],
                    destination_parts[-1],
                    source_fd=source_fd,
                    destination_fd=destination_fd,
                )
                os.fsync(source_fd)
                os.fsync(destination_fd)
            except OSError as exc:
                raise HandoffTransient(
                    f"cannot move handoff item {item['kind']}: {exc}"
                ) from exc
            rebound = rooted._read_leaf(
                destination_fd,
                destination_parts[-1],
                relative=item["destination"],
                file_mode=0o600,
                max_bytes=size,
                require_single_link=True,
            )
            if hashlib.sha256(rebound).hexdigest() != digest:
                raise HandoffError(f"moved handoff item drifted: {item['kind']}")
    except fleet_safe_paths.SafePathError as exc:
        raise HandoffError(f"unsafe handoff move: {exc}") from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)


def _checkpoint(name: str, *, leaf: str | None = None, first: bool = False) -> None:
    if os.environ.get("FLEET_TEST_HANDOFF_CRASH_AT") != name:
        return
    if name == "after_move":
        selected = os.environ.get("FLEET_TEST_HANDOFF_CRASH_FILE")
        if selected and selected != leaf:
            return
        if not selected and not first:
            return
    os.kill(os.getpid(), signal.SIGKILL)


def _commit_locked(
    rooted: fleet_safe_paths.RootedFS,
    current: dict[str, Any],
    events: list[dict[str, Any]],
    feature: str,
    mission_id: str,
) -> dict[str, Any]:
    try:
        _validate_mission_state(current, feature, mission_id)
    except HandoffError as exc:
        if "expired" in str(exc):
            raise HandoffTransient(
                "assurance approval expired during handoff commit; renew and retry"
            ) from exc
        raise
    receipt = _read_metadata_optional(rooted, _receipt_relative(mission_id))
    if receipt is not None:
        intent = _read_metadata_optional(rooted, _intent_relative(mission_id))
        if intent is None:
            raise HandoffError("completed handoff intent is absent")
        _validate_intent(intent, feature, mission_id, current, events)
        result = _validate_receipt(receipt, feature, mission_id, current, events)
        _validate_receipt_intent_pair(receipt, intent)
        _validate_completed_layout(rooted, receipt, feature, mission_id)
        return result
    intent = _read_metadata_optional(rooted, _intent_relative(mission_id))
    if intent is None:
        raise HandoffError("durable handoff intent is absent")
    _validate_intent(intent, feature, mission_id, current, events)
    if (
        intent["mission_head_sha256"] != current["head_sha256"]
        or intent["approval_event_sha256"] != current["approval"]["event_sha256"]
    ):
        intent = {
            **intent,
            "mission_head_sha256": current["head_sha256"],
            "approval_event_sha256": current["approval"]["event_sha256"],
        }
        _write_intent(rooted, mission_id, intent, replace=True)
    if intent["status"] == "prepared":
        compiled = _load_compiled_locked(rooted, mission_id, current)
        manifest = _read_live_manifest_optional(rooted, feature)
        if manifest is None:
            raise HandoffError("prepared handoff lost its active manifest")
        _validate_manifest(manifest, current, compiled, feature, mission_id)
        if (
            manifest.get("workspace.handoff_state") != "quiesced"
            or manifest.get("workspace.quiesced") != "1"
        ):
            raise HandoffTransient("main fleet workspace is not durably quiesced")
        _validate_empty_destination(rooted, mission_id)
        items = _snapshot_items(rooted, feature, mission_id)
        intent = {**intent, "status": "committing", "items": items}
        _write_intent(rooted, mission_id, intent, replace=True)
        _checkpoint("after_committing_intent")
    items = _validate_items(intent["items"], feature, mission_id)
    ordered = [item for item in items if item["kind"] != "manifest"] + [
        item for item in items if item["kind"] == "manifest"
    ]
    moved = 0
    for item in ordered:
        _move_item(rooted, item)
        if item["present"]:
            moved += 1
            _checkpoint(
                "after_move",
                leaf=item["source"],
                first=moved == 1,
            )
    if _read_live_manifest_optional(rooted, feature) is not None:
        raise HandoffError("active main manifest remains after handoff commit")
    # Moving the manifest is recoverable but not reversible.  Recheck wall-clock
    # validity before publishing success: if approval expired during the move,
    # leave the committing intent and moved evidence for an explicit renewal and
    # exact replay, rather than minting a receipt under an expired authority.
    try:
        _validate_mission_state(current, feature, mission_id)
    except HandoffError as exc:
        if "expired" in str(exc):
            raise HandoffTransient(
                "assurance approval expired during handoff commit; renew and retry"
            ) from exc
        raise
    completed_at = fleet_mission_state.utc_timestamp()
    if _parse_expiry(completed_at) >= _parse_expiry(current["approval"]["expires_at"]):
        raise HandoffTransient(
            "assurance approval expired during handoff commit; renew and retry"
        )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "feature": feature,
        "mission_id": mission_id,
        "mission_head_sha256": current["head_sha256"],
        "approval_event_sha256": current["approval"]["event_sha256"],
        "workspace_uuid": intent["workspace_uuid"],
        "completed_at": completed_at,
        "items": items,
    }
    try:
        rooted.atomic_write(
            _receipt_relative(mission_id),
            _canonical_line(receipt),
            directory_modes=_mission_metadata_modes(),
            file_mode=0o600,
        )
    except fleet_safe_paths.SafePathError as exc:
        raise HandoffTransient(f"cannot publish handoff receipt: {exc}") from exc
    _checkpoint("after_receipt")
    return _result(feature, mission_id)


def commit(runs_dir: Path, feature: str, mission_id: str) -> dict[str, Any]:
    """Recover or complete a prepared handoff under its mutation barrier."""

    feature = _validate_feature(feature)
    try:
        with fleet_mission_state.MissionMutationBarrier(
            runs_dir, mission_id
        ) as barrier:
            with fleet_mission_state.MissionTransaction(
                runs_dir,
                mission_id,
                mutation_barrier=barrier,
            ) as tx:
                current = tx.current_state
                events = list(tx.events)
                if not isinstance(current, dict):
                    raise HandoffError("Mission ledger has no state")
            with fleet_safe_paths.RootedFS(runs_dir) as rooted:
                barrier.assert_binding(rooted.root, tx.mission_id)
                result = _commit_locked(
                    rooted, current, events, feature, tx.mission_id
                )
                barrier.assert_binding(rooted.root, tx.mission_id)
                return result
    except HandoffError:
        raise
    except (
        fleet_mission_state.MissionStateError,
        fleet_safe_paths.SafePathError,
    ) as exc:
        raise HandoffTransient(f"cannot lock assurance handoff: {exc}") from exc


def _hold(runs_dir: Path, feature: str, mission_id: str) -> dict[str, Any] | None:
    try:
        with fleet_mission_state.MissionMutationBarrier(
            runs_dir, mission_id
        ) as barrier:
            with fleet_mission_state.MissionTransaction(
                runs_dir,
                mission_id,
                mutation_barrier=barrier,
            ) as tx:
                current = tx.current_state
                events = list(tx.events)
                normalized_mission_id = tx.mission_id
                if not isinstance(current, dict):
                    raise HandoffError("Mission ledger has no state")
            with fleet_safe_paths.RootedFS(runs_dir) as rooted:
                barrier.assert_binding(rooted.root, normalized_mission_id)
                _prepare_locked(
                    rooted,
                    current,
                    events,
                    feature,
                    normalized_mission_id,
                )
                print("READY", flush=True)
                command = sys.stdin.readline().strip()
                if command == "ABORT" or not command:
                    return None
                if command != "COMMIT":
                    raise HandoffError("invalid handoff control command")
                barrier.assert_binding(rooted.root, normalized_mission_id)
                result = _commit_locked(
                    rooted,
                    current,
                    events,
                    feature,
                    normalized_mission_id,
                )
                barrier.assert_binding(rooted.root, normalized_mission_id)
                return result
    except HandoffError:
        raise
    except (
        fleet_mission_state.MissionStateError,
        fleet_safe_paths.SafePathError,
    ) as exc:
        raise HandoffTransient(f"cannot hold assurance handoff lock: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "hold", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--runs-dir", required=True)
        command.add_argument("--feature", required=True)
        command.add_argument("--mission-id")
    return parser


def _main() -> int:
    args = _parser().parse_args()
    runs_dir = Path(args.runs_dir)
    try:
        if args.command == "preflight":
            result = preflight(runs_dir, args.feature, args.mission_id)
        elif args.command == "recover":
            if not args.mission_id:
                probe = preflight(runs_dir, args.feature)
                args.mission_id = probe["mission_id"]
            result = commit(runs_dir, args.feature, args.mission_id)
        else:
            if not args.mission_id:
                raise HandoffError("hold requires --mission-id")
            result = _hold(runs_dir, args.feature, args.mission_id)
            if result is None:
                return 75
        print(fleet_json.canonical_bytes(result).decode("utf-8"), flush=True)
        return 0
    except HandoffAbsent as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except HandoffTransient as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except HandoffError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
