#!/usr/bin/env python3
"""Durable phase gate for CONTROL → RECON → BUILD → CHALLENGE → VERIFY."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterator

import fleet_json
import fleet_manifest
import fleet_mission_state as mission_state
import fleet_safe_paths


PHASE_ORDER = ["CONTROL", "RECON", "BUILD", "CHALLENGE", "VERIFY"]
STATE_SCHEMA_VERSION = 2
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_STATE_BYTES = 1024 * 1024
SAFE_FEATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STATE_FIELDS_V1 = {"schema_version", "feature", "active_phase", "history"}
STATE_FIELDS_V2 = STATE_FIELDS_V1 | {"binding"}
STATE_BINDING_FIELDS = {
    "manifest_contract_version",
    "manifest_digest",
    "mode",
    "preset",
    "phase_order",
    "mission",
}
MISSION_BINDING_FIELDS = {
    "mission_id",
    "compiled_digest",
    "router_digest",
    "roster_digest",
    "launch_digest",
    "target_repo",
}
HISTORY_FIELDS = {"phase", "timestamp", "evidence"}
HISTORY_APPROVAL_FIELDS = {"approved_by", "approval_event_sha256"}


class PhaseApprovalError(RuntimeError):
    """A BUILD-exit approval is absent, stale, or bound to another Mission."""


class PhaseStateError(RuntimeError):
    """A manifest or phase-state contract is malformed, unsafe, or inconsistent."""


_ACTIVE_MISSION_TRANSACTION: ContextVar[
    tuple[Path, str, mission_state.MissionTransaction] | None
] = ContextVar("fleet_state_active_mission_transaction", default=None)


def _safe_text(value: Any, where: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise PhaseStateError(f"{where} must be a non-empty bounded string")
    if any(character in value for character in ("\x00", "\r", "\n", "\x1f")):
        raise PhaseStateError(f"{where} contains a forbidden control character")
    return value


def _manifest_leaf_feature(leaf: str) -> str:
    prefix = "fleet-"
    suffix = ".manifest"
    feature = (
        leaf[len(prefix) : -len(suffix)]
        if leaf.startswith(prefix) and leaf.endswith(suffix)
        else ""
    )
    if not SAFE_FEATURE.fullmatch(feature):
        raise PhaseStateError("manifest must be a fleet-<feature>.manifest root leaf")
    return feature


def _parse_manifest(content: bytes, *, leaf: str) -> dict[str, str]:
    try:
        manifest = fleet_manifest.parse_bytes(content)
        # Normalization is a validation pass. Keep the exact parsed projection
        # for downstream FDP equality checks and legacy callers.
        fleet_manifest.normalize(manifest)
    except fleet_manifest.ManifestError as exc:
        raise PhaseStateError(f"invalid fleet manifest: {exc}") from exc
    feature = _manifest_leaf_feature(leaf)
    if manifest.get("feature") != feature:
        raise PhaseStateError("manifest feature does not match its rooted filename")
    mode = manifest.get("mode", "guided")
    if mode not in {"guided", "autonomous", "assured"}:
        raise PhaseStateError(f"unsupported fleet mode: {mode}")
    mission_id = manifest.get("mission_id")
    if mission_id:
        try:
            canonical_mission_id = mission_state.normalize_uuid(
                mission_id, "mission_id"
            )
        except mission_state.MissionStateError as exc:
            raise PhaseStateError(str(exc)) from exc
        if mission_id != canonical_mission_id:
            raise PhaseStateError("mission_id must use its canonical UUID spelling")
        if mode not in {"autonomous", "assured"}:
            raise PhaseStateError(
                "mission-bound phase state requires autonomous or assured mode"
            )
        target_repo = manifest.get("target_repo", "")
        if not target_repo or not Path(target_repo).is_absolute():
            raise PhaseStateError(
                "mission-bound manifest requires an absolute target_repo"
            )
    configured_phases(manifest)
    return manifest


def _read_manifest_bytes(rooted: fleet_safe_paths.RootedFS, leaf: str) -> bytes:
    return rooted.read_regular(
        leaf,
        directory_modes=(),
        file_mode=0o600,
        max_bytes=MAX_MANIFEST_BYTES,
        require_single_link=True,
    )


def _manifest_snapshot(
    rooted: fleet_safe_paths.RootedFS, leaf: str
) -> tuple[bytes, dict[str, str]]:
    content = _read_manifest_bytes(rooted, leaf)
    return content, _parse_manifest(content, leaf=leaf)


def _assert_manifest_unchanged(
    rooted: fleet_safe_paths.RootedFS, leaf: str, expected: bytes
) -> None:
    if _read_manifest_bytes(rooted, leaf) != expected:
        raise PhaseStateError("fleet manifest changed during the phase transaction")


@contextmanager
def mission_approval_lock(
    manifest_path: Path, manifest: dict[str, str]
) -> Iterator[None]:
    """Pin and lock the exact Mission ledger while consuming an approval."""

    mission_id = manifest.get("mission_id", "")
    if not mission_id:
        yield
        return
    runs_dir = manifest_path.parent
    try:
        transaction = mission_state.MissionTransaction(runs_dir, mission_id)
        with transaction:
            token = _ACTIVE_MISSION_TRANSACTION.set(
                (runs_dir, transaction.mission_id, transaction)
            )
            try:
                yield
            finally:
                _ACTIVE_MISSION_TRANSACTION.reset(token)
    except mission_state.MissionStateError as exc:
        raise PhaseApprovalError(f"Mission identity is invalid: {exc}") from exc


def manifest_values(path: Path) -> dict[str, str]:
    """Compatibility API backed by the strict descriptor-rooted manifest reader."""

    source = Path(path)
    with fleet_safe_paths.RootedFS(source.parent) as rooted:
        _, manifest = _manifest_snapshot(rooted, source.name)
        return manifest


def state_path(manifest_path: Path) -> Path:
    return manifest_path.with_suffix(".state.json")


def _state_leaf(manifest_leaf: str) -> str:
    return str(Path(manifest_leaf).with_suffix(".state.json"))


def _lock_leaf(feature: str) -> str:
    # Deliberately share the feature manifest CAS lock. This prevents a sanctioned
    # manifest update from racing a phase transition and avoids a second lock order.
    return f".fleet-{feature}.manifest.lock"


def configured_phases(manifest: dict[str, str]) -> list[str]:
    values = [value for key, value in manifest.items() if key.endswith(".phase")]
    invalid = sorted({value for value in values if value not in PHASE_ORDER})
    if invalid:
        raise PhaseStateError(
            f"manifest contains unsupported phases: {', '.join(invalid)}"
        )
    phases = set(values)
    phases.add("CONTROL")
    return [phase for phase in PHASE_ORDER if phase in phases]


def _mission_binding(manifest: dict[str, str]) -> dict[str, str] | None:
    mission_id = manifest.get("mission_id")
    if not mission_id:
        return None
    binding = {
        "mission_id": mission_id,
        "compiled_digest": manifest.get("compiled_digest", ""),
        "router_digest": manifest.get("router_digest", ""),
        "roster_digest": manifest.get("roster_digest", ""),
        "launch_digest": manifest.get("launch_digest", ""),
        "target_repo": manifest.get("target_repo", ""),
    }
    if set(binding) != MISSION_BINDING_FIELDS or any(
        not SHA256.fullmatch(binding[field])
        for field in (
            "compiled_digest",
            "router_digest",
            "roster_digest",
            "launch_digest",
        )
    ):
        raise PhaseStateError("mission phase binding is incomplete")
    return binding


def _phase_manifest_projection(manifest: dict[str, str]) -> dict[str, str]:
    """Exclude only fields that teardown legitimately advances after all phases."""

    mutable_fields = {"workspace.handoff_state", "workspace.quiesced"}
    mutable_suffixes = (".final_sha", ".published_sha", ".publication_state")
    return {
        key: value
        for key, value in manifest.items()
        if key not in mutable_fields and not key.endswith(mutable_suffixes)
    }


def _binding(manifest: dict[str, str]) -> dict[str, Any]:
    try:
        contract_version = int(manifest.get("manifest_contract_version", "1"))
    except ValueError as exc:
        raise PhaseStateError("manifest contract version is invalid") from exc
    return {
        "manifest_contract_version": contract_version,
        "manifest_digest": fleet_json.sha256(_phase_manifest_projection(manifest)),
        "mode": manifest.get("mode", "guided"),
        "preset": manifest.get("preset", ""),
        "phase_order": configured_phases(manifest),
        "mission": _mission_binding(manifest),
    }


def _parse_timestamp(value: Any, where: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise PhaseStateError(f"{where} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PhaseStateError(f"{where} timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PhaseStateError(f"{where} timestamp must be UTC")
    return parsed.astimezone(timezone.utc)


def _previous_configured_phase(phases: list[str], phase: str) -> str | None:
    index = phases.index(phase)
    return phases[index - 1] if index else None


def _validate_history_entry(
    entry: Any,
    *,
    number: int,
    phases: list[str],
    mode: str,
    mission_bound: bool,
) -> tuple[str, datetime]:
    if not isinstance(entry, dict):
        raise PhaseStateError(f"phase history entry {number} must be an object")
    extra = set(entry) - (HISTORY_FIELDS | HISTORY_APPROVAL_FIELDS)
    missing = HISTORY_FIELDS - set(entry)
    if missing or extra:
        raise PhaseStateError(
            f"phase history entry {number} fields do not match schema"
        )
    phase = entry.get("phase")
    if not isinstance(phase, str) or phase not in phases:
        raise PhaseStateError(f"phase history entry {number} has an invalid phase")
    _safe_text(
        entry.get("evidence"), f"phase history entry {number} evidence", maximum=8192
    )
    timestamp = _parse_timestamp(
        entry.get("timestamp"), f"phase history entry {number}"
    )
    approval_fields = set(entry) & HISTORY_APPROVAL_FIELDS
    previous = _previous_configured_phase(phases, phase)
    approval_required = previous == "BUILD" and mode != "autonomous"
    if phase == "CONTROL":
        if entry["evidence"] != "fleet-created" or approval_fields:
            raise PhaseStateError(
                "CONTROL history entry is not the initialization event"
            )
    elif approval_required and mission_bound:
        if approval_fields != {"approval_event_sha256"} or not SHA256.fullmatch(
            str(entry.get("approval_event_sha256", ""))
        ):
            raise PhaseStateError(
                f"phase history entry {number} lacks the exact Mission approval event"
            )
    elif approval_required:
        if approval_fields != {"approved_by"}:
            raise PhaseStateError(
                f"phase history entry {number} lacks the legacy operator attestation"
            )
        _safe_text(
            entry.get("approved_by"),
            f"phase history entry {number} approved_by",
            maximum=512,
        )
    elif approval_fields:
        raise PhaseStateError(
            f"phase history entry {number} contains an out-of-phase approval"
        )
    return phase, timestamp


def validate_state(value: Any, manifest: dict[str, str]) -> dict[str, Any]:
    """Validate the closed phase-state schema and its immutable manifest binding."""

    if not isinstance(value, dict):
        raise PhaseStateError("fleet state must be a JSON object")
    schema_version = value.get("schema_version")
    if type(schema_version) is not int or schema_version not in {
        1,
        STATE_SCHEMA_VERSION,
    }:
        raise PhaseStateError("unsupported fleet state schema_version")
    expected_fields = STATE_FIELDS_V1 if schema_version == 1 else STATE_FIELDS_V2
    if set(value) != expected_fields:
        raise PhaseStateError("fleet state fields do not match schema")
    feature = manifest["feature"]
    if value.get("feature") != feature:
        raise PhaseStateError("fleet state feature does not match the manifest")
    phases = configured_phases(manifest)
    mode = manifest.get("mode", "guided")
    mission_bound = bool(manifest.get("mission_id"))
    if schema_version == 1:
        # Historical standalone guided state remains readable/writable. It is
        # intentionally never accepted for a Mission or autonomous execution.
        if mission_bound or mode != "guided":
            raise PhaseStateError(
                "legacy phase state is allowed only for standalone guided fleets"
            )
    else:
        binding = value.get("binding")
        if not isinstance(binding, dict) or set(binding) != STATE_BINDING_FIELDS:
            raise PhaseStateError("fleet state binding fields do not match schema")
        mission = binding.get("mission")
        if mission is not None and (
            not isinstance(mission, dict) or set(mission) != MISSION_BINDING_FIELDS
        ):
            raise PhaseStateError(
                "fleet state Mission binding fields do not match schema"
            )
        if binding != _binding(manifest):
            raise PhaseStateError("fleet state immutable manifest binding changed")
    active_phase = value.get("active_phase")
    if not isinstance(active_phase, str) or active_phase not in phases:
        raise PhaseStateError("fleet state active_phase is invalid")
    history = value.get("history")
    if not isinstance(history, list) or len(history) > len(phases):
        raise PhaseStateError("fleet state history is invalid")
    if schema_version == STATE_SCHEMA_VERSION and not history:
        raise PhaseStateError(
            "fleet state history must contain its initialization event"
        )
    seen: list[str] = []
    previous_timestamp: datetime | None = None
    for number, entry in enumerate(history, 1):
        phase, timestamp = _validate_history_entry(
            entry,
            number=number,
            phases=phases,
            mode=mode,
            mission_bound=mission_bound,
        )
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise PhaseStateError(
                "fleet state history timestamps are not strictly monotonic"
            )
        previous_timestamp = timestamp
        seen.append(phase)
    if history and seen[-1] != active_phase:
        raise PhaseStateError("fleet state active_phase differs from its history head")
    if len(seen) != len(set(seen)):
        raise PhaseStateError("fleet state history repeats a phase")
    indexes = [phases.index(phase) for phase in seen]
    if any(current != previous + 1 for previous, current in zip(indexes, indexes[1:])):
        raise PhaseStateError("fleet state history skips a configured phase")
    if schema_version == STATE_SCHEMA_VERSION:
        expected = phases[: phases.index(active_phase) + 1]
        if seen != expected:
            raise PhaseStateError(
                "fleet state history is not the configured phase prefix"
            )
    return value


def _decode_state(content: bytes, manifest: dict[str, str]) -> dict[str, Any]:
    try:
        value = fleet_json.loads(content)
    except fleet_json.FleetJSONError as exc:
        raise PhaseStateError(f"invalid fleet state JSON: {exc}") from exc
    return validate_state(value, manifest)


def _state_bytes(value: dict[str, Any]) -> bytes:
    try:
        content = fleet_json.canonical_bytes(value) + b"\n"
    except fleet_json.FleetJSONError as exc:
        raise PhaseStateError(f"cannot encode fleet state: {exc}") from exc
    if len(content) > MAX_STATE_BYTES:
        raise PhaseStateError("fleet state exceeds its maximum size")
    return content


def _read_state(
    rooted: fleet_safe_paths.RootedFS,
    leaf: str,
    manifest: dict[str, str],
) -> tuple[bytes, dict[str, Any]]:
    content = rooted.read_regular(
        leaf,
        directory_modes=(),
        file_mode=0o600,
        max_bytes=MAX_STATE_BYTES,
        require_single_link=True,
    )
    return content, _decode_state(content, manifest)


def load_state(path: Path, manifest: dict[str, str] | None = None) -> dict[str, Any]:
    """Compatibility reader; callers should pass the already validated manifest."""

    source = Path(path)
    manifest_path = source.with_suffix("").with_suffix(".manifest")
    with fleet_safe_paths.RootedFS(source.parent) as rooted:
        if manifest is None:
            _, manifest = _manifest_snapshot(rooted, manifest_path.name)
        _, value = _read_state(rooted, source.name, manifest)
        return value


def load_live(manifest_path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    """Read one exact live manifest/state pair beneath a single pinned root."""

    live = probe_live(manifest_path)
    if live is None:
        raise PhaseStateError("active fleet manifest is absent")
    return live


def probe_live(
    manifest_path: Path,
) -> tuple[dict[str, str], dict[str, Any]] | None:
    """Read a live pair without effects, returning ``None`` only if no manifest exists."""

    source = Path(manifest_path)
    _manifest_leaf_feature(source.name)
    with fleet_safe_paths.RootedFS(source.parent) as rooted:
        manifest_content = rooted.read_regular_optional(
            source.name,
            directory_modes=(),
            file_mode=0o600,
            max_bytes=MAX_MANIFEST_BYTES,
            require_single_link=True,
        )
        if manifest_content is None:
            return None
        manifest = _parse_manifest(manifest_content, leaf=source.name)
        _, state = _read_state(rooted, _state_leaf(source.name), manifest)
        _assert_manifest_unchanged(rooted, source.name, manifest_content)
        return manifest, state


def _next_timestamp(history: list[dict[str, Any]]) -> str:
    current = datetime.now(timezone.utc)
    if history:
        previous = _parse_timestamp(
            history[-1].get("timestamp"), "latest phase history"
        )
        if current <= previous:
            current = previous + timedelta(microseconds=1)
    return current.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _new_state(manifest: dict[str, str]) -> dict[str, Any]:
    timestamp = _next_timestamp([])
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "feature": manifest["feature"],
        "binding": _binding(manifest),
        "active_phase": "CONTROL",
        "history": [
            {"phase": "CONTROL", "timestamp": timestamp, "evidence": "fleet-created"}
        ],
    }


def _test_pause(name: str) -> None:
    """Deterministic race checkpoint, inert unless an isolated test enables it."""

    if os.environ.get("FLEET_TEST_STATE_PAUSE_AT") != name:
        return
    ready_raw = os.environ.get("FLEET_TEST_STATE_READY")
    release_raw = os.environ.get("FLEET_TEST_STATE_RELEASE")
    if not ready_raw or not release_raw:
        raise PhaseStateError("phase test pause requires ready and release paths")
    ready = Path(ready_raw)
    release = Path(release_raw)
    ready.write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + 10.0
    while not release.exists():
        if time.monotonic() >= deadline:
            raise PhaseStateError("phase test pause timed out")
        time.sleep(0.01)


def _current_mission_state(runs_dir: Path, mission_id: str) -> dict[str, Any]:
    active = _ACTIVE_MISSION_TRANSACTION.get()
    if active is not None and active[0] == runs_dir and active[1] == mission_id:
        current = active[2].current_state
    else:
        with mission_state.MissionTransaction(runs_dir, mission_id) as transaction:
            current = transaction.current_state
    if not isinstance(current, dict):
        raise PhaseApprovalError("Mission ledger has no derived state")
    return current


def validate_mission_approval(
    manifest_path: Path,
    manifest: dict[str, str],
    approval_event_sha256: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    mission_id = manifest.get("mission_id", "")
    if not mission_id:
        raise PhaseApprovalError("manifest is not bound to a Mission")
    if manifest.get("mode") != "assured":
        raise PhaseApprovalError("Mission-bound BUILD approval requires mode=assured")
    if not mission_state.SHA256.fullmatch(approval_event_sha256):
        raise PhaseApprovalError("approval event reference must be SHA-256")
    try:
        current = _current_mission_state(manifest_path.parent, mission_id)
    except (mission_state.MissionStateError, OSError) as exc:
        raise PhaseApprovalError(f"cannot verify Mission approval: {exc}") from exc
    if current.get("status") != "assured_running":
        raise PhaseApprovalError("Mission is not in assured_running state")
    if current.get("feature") != manifest.get("feature"):
        raise PhaseApprovalError("Mission feature does not match the fleet manifest")
    mission_target_raw = current.get("target_repo")
    manifest_target_raw = manifest.get("target_repo")
    if not isinstance(mission_target_raw, str) or not mission_target_raw:
        raise PhaseApprovalError("Mission target repository is invalid")
    if not manifest_target_raw:
        raise PhaseApprovalError("fleet manifest target repository is missing")
    try:
        mission_target = Path(mission_target_raw).resolve()
        manifest_target = Path(manifest_target_raw).resolve()
    except (OSError, RuntimeError) as exc:
        raise PhaseApprovalError("Mission target repository is invalid") from exc
    if mission_target != manifest_target:
        raise PhaseApprovalError("Mission target does not match the fleet manifest")
    approval = current.get("approval")
    if not isinstance(approval, dict):
        raise PhaseApprovalError("Mission lacks a scoped assurance approval")
    if approval.get("event_sha256") != approval_event_sha256:
        raise PhaseApprovalError("approval event is not the active Mission approval")
    try:
        expires = datetime.fromisoformat(
            str(approval["expires_at"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError) as exc:
        raise PhaseApprovalError("Mission approval expiry is invalid") from exc
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current_time >= expires:
        raise PhaseApprovalError("Mission approval expired before BUILD exit")
    return approval


def _advance(
    rooted: fleet_safe_paths.RootedFS,
    manifest_path: Path,
    manifest_content: bytes,
    manifest: dict[str, str],
    state_leaf: str,
    *,
    requested: str,
    evidence: str | None,
    approved_by: str | None,
    approval_event_sha256: str | None,
) -> tuple[int, str, bool]:
    original_state, state = _read_state(rooted, state_leaf, manifest)
    phases = configured_phases(manifest)
    current = state["active_phase"]
    try:
        expected = phases[phases.index(current) + 1]
    except (ValueError, IndexError):
        return 2, f"no phase follows {current}", True
    if requested != expected:
        return (
            2,
            f"invalid transition: {current} -> {requested}; expected {expected}",
            True,
        )
    try:
        evidence_value = _safe_text(evidence, "phase transition evidence", maximum=8192)
    except PhaseStateError:
        return 2, "phase transition requires --evidence <path|sha|gate-id>", True
    mission_bound = bool(manifest.get("mission_id"))
    mode = manifest.get("mode", "guided")
    leaving_build = current == "BUILD" and mode != "autonomous"
    if not leaving_build and (approved_by or approval_event_sha256):
        return (
            2,
            "approval options are valid only when a guided BUILD phase exits",
            True,
        )
    approval_lock = (
        mission_approval_lock(manifest_path, manifest)
        if leaving_build and mission_bound
        else nullcontext()
    )
    with approval_lock:
        if leaving_build:
            if mission_bound:
                if approved_by:
                    return (
                        2,
                        "Mission-bound BUILD exit rejects --approved-by text; "
                        "use --approval-event-sha256 <exact assurance_approved event>",
                        True,
                    )
                if not approval_event_sha256:
                    return (
                        2,
                        "Mission-bound BUILD exit requires --approval-event-sha256 "
                        "<exact assurance_approved event>",
                        True,
                    )
                try:
                    validate_mission_approval(
                        manifest_path,
                        manifest,
                        approval_event_sha256,
                    )
                except PhaseApprovalError as exc:
                    return 3, f"Mission approval gate closed: {exc}", True
            else:
                if approval_event_sha256:
                    return (
                        2,
                        "--approval-event-sha256 requires a Mission-bound manifest",
                        True,
                    )
                if not approved_by:
                    return (
                        2,
                        "leaving BUILD requires --approved-by <operator-attestation>: "
                        "legacy guided fleets record a label, not cryptographic human presence",
                        True,
                    )
                try:
                    _safe_text(approved_by, "operator attestation", maximum=512)
                except PhaseStateError as exc:
                    return 2, str(exc), True
        if current == "BUILD" and manifest.get("preset") == "fleet_dialogue":
            try:
                from fleet_dialogue_controller import accepted_build_gate

                accepted_build_gate(manifest_path.parent, manifest["feature"], manifest)
            except RuntimeError as exc:
                return 3, f"FDP-2 BUILD gate closed: {exc}", True
        if current == "CHALLENGE" and manifest.get("preset") == "fleet_dialogue":
            try:
                from fleet_assurance_controller import challenge_phase_gate

                challenge_phase_gate(
                    manifest_path.parent,
                    manifest["feature"],
                    manifest,
                    evidence_value,
                )
            except RuntimeError as exc:
                return 3, f"FDP-3 CHALLENGE gate closed: {exc}", True
        _test_pause("before-state-write")
        _assert_manifest_unchanged(rooted, manifest_path.name, manifest_content)
        rebound_state, _ = _read_state(rooted, state_leaf, manifest)
        if rebound_state != original_state:
            raise PhaseStateError("fleet state changed during the phase transaction")
        state["active_phase"] = requested
        entry: dict[str, Any] = {
            "phase": requested,
            "timestamp": _next_timestamp(state["history"]),
            "evidence": evidence_value,
        }
        if leaving_build and mission_bound and approval_event_sha256:
            entry["approval_event_sha256"] = approval_event_sha256
        elif leaving_build and approved_by:
            entry["approved_by"] = approved_by
        state["history"].append(entry)
        validate_state(state, manifest)
        rooted.replace_regular(
            state_leaf,
            _state_bytes(state),
            directory_modes=(),
            file_mode=0o600,
        )
        _assert_manifest_unchanged(rooted, manifest_path.name, manifest_content)
    return 0, f"advanced {current} -> {requested}", False


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("init", "advance", "check", "show", "active", "probe-active"),
    )
    parser.add_argument("manifest")
    parser.add_argument("value", nargs="?")
    parser.add_argument("--evidence")
    parser.add_argument("--approved-by")
    parser.add_argument("--approval-event-sha256")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    try:
        if args.command == "probe-active":
            live = probe_live(manifest_path)
            if live is None:
                return 1
            print(live[1]["active_phase"])
            return 0
        with fleet_safe_paths.RootedFS(manifest_path.parent) as rooted:
            initial_content, initial_manifest = _manifest_snapshot(
                rooted, manifest_path.name
            )
            feature = initial_manifest["feature"]
            state_leaf = _state_leaf(manifest_path.name)
            if args.command in {"init", "advance"}:
                with rooted.exclusive_lock(
                    _lock_leaf(feature),
                    directory_modes=(),
                    file_mode=0o600,
                    require_single_link=True,
                ):
                    locked_content, manifest = _manifest_snapshot(
                        rooted, manifest_path.name
                    )
                    if (
                        locked_content != initial_content
                        or manifest != initial_manifest
                    ):
                        raise PhaseStateError(
                            "fleet manifest changed before the phase lock was acquired"
                        )
                    if args.command == "init":
                        existing = rooted.read_regular_optional(
                            state_leaf,
                            directory_modes=(),
                            file_mode=0o600,
                            max_bytes=MAX_STATE_BYTES,
                            require_single_link=True,
                        )
                        if existing is not None:
                            print(
                                f"state already exists: {state_path(manifest_path)}",
                                file=sys.stderr,
                            )
                            return 2
                        _test_pause("before-state-write")
                        _assert_manifest_unchanged(
                            rooted, manifest_path.name, locked_content
                        )
                        state = _new_state(manifest)
                        validate_state(state, manifest)
                        rooted.atomic_write(
                            state_leaf,
                            _state_bytes(state),
                            directory_modes=(),
                            file_mode=0o600,
                            require_absent=True,
                        )
                        _assert_manifest_unchanged(
                            rooted, manifest_path.name, locked_content
                        )
                        print(state_path(manifest_path))
                        return 0
                    code, message, error = _advance(
                        rooted,
                        manifest_path,
                        locked_content,
                        manifest,
                        state_leaf,
                        requested=args.value or "",
                        evidence=args.evidence,
                        approved_by=args.approved_by,
                        approval_event_sha256=args.approval_event_sha256,
                    )
                    print(message, file=sys.stderr if error else sys.stdout)
                    return code

            _, state = _read_state(rooted, state_leaf, initial_manifest)
            _assert_manifest_unchanged(rooted, manifest_path.name, initial_content)
            if args.command == "show":
                print(json.dumps(state, indent=2, sort_keys=True, allow_nan=False))
                return 0
            if args.command == "active":
                print(state["active_phase"])
                return 0
            instance = args.value or ""
            phase = initial_manifest.get(f"{instance}.phase")
            if not phase:
                print(f"unknown instance phase: {instance}", file=sys.stderr)
                return 2
            if initial_manifest.get("mode", "guided") == "autonomous":
                print(f"autonomous mode open: {instance} ({phase})")
                return 0
            if phase != "CONTROL" and phase != state["active_phase"]:
                print(
                    f"phase gate closed: {instance} is {phase}, "
                    f"active phase is {state['active_phase']}",
                    file=sys.stderr,
                )
                return 3
            print(f"phase gate open: {instance} ({phase})")
            return 0
    except (
        fleet_safe_paths.SafePathError,
        PhaseStateError,
        PhaseApprovalError,
        OSError,
    ) as exc:
        print(f"unsafe or invalid fleet phase state: {exc}", file=sys.stderr)
        return 2


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
