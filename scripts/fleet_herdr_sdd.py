#!/usr/bin/env python3
"""Opt-in SDD plan binding for Herdr Mission creation, recovery and archive.

This is the stage-2 integration: it binds the existing ``fleet_sdd_snapshot``
primitive to one Mission's ledger-pinned digest, exposes the verified frozen
plan as a versioned stage packet, and supplies exact archived evidence for the
Herdr archive schema v5. Stage-2 integrity/membership acceptance is
implemented; scenario/functional acceptance (stage 5) remains NOT_VERIFIED.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import fleet_mission_state as state
import fleet_safe_paths
import fleet_sdd_snapshot as snapshot


SDD_PLAN_FIELD = "sdd_plan_sha256"
SDD_PLAN_PACKET_SCHEMA = 1
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_PLAN_BYTES = 16 * 1024 * 1024


def freeze(runs_dir: Path, mission_id: str, plan_path: Path) -> str:
    """Freeze one plan under the runs store and return its exact digest."""
    try:
        manifest = snapshot.freeze_plan(Path(runs_dir), mission_id, Path(plan_path))
    except snapshot.SnapshotError as exc:
        text = str(exc)
        if "already bound" in text:
            raise state.MissionConflict(
                f"SDD plan conflicts with the frozen Mission binding: {text}"
            ) from exc
        raise state.MissionStateError(f"SDD plan snapshot rejected: {text}") from exc
    return manifest["plan_sha256"]


def binding_exists(runs_dir: Path, mission_id: str) -> bool:
    """Read the optional binding manifest with no-follow and report presence."""
    relative = Path("sdd") / "missions" / f"{mission_id}.json"
    try:
        with fleet_safe_paths.RootedFS(Path(runs_dir)) as rooted:
            raw = rooted.read_regular_optional(
                relative,
                directory_modes=(0o700, 0o700),
                max_bytes=_MAX_MANIFEST_BYTES,
            )
    except fleet_safe_paths.SafePathError as exc:
        if str(exc).startswith("rooted directory is missing:"):
            return False
        raise state.MissionStateError(f"SDD binding store is unsafe: {exc}") from exc
    return raw is not None


def verify(runs_dir: Path, current: dict) -> dict | None:
    """Verify a ledger-pinned plan and return its versioned stage packet.

    The digest authority is the Mission ledger projection, never runtime
    options or the snapshot manifest. A legacy Mission without the optional
    field returns ``None``.
    """
    digest = current.get(SDD_PLAN_FIELD)
    if digest is None:
        return None
    if not isinstance(digest, str) or not state.SHA256.fullmatch(digest):
        raise state.MissionStateError("ledger sdd_plan_sha256 is not a SHA-256 digest")
    mission_id = current.get("mission_id")
    if not isinstance(mission_id, str):
        raise state.MissionStateError("SDD verification requires a canonical Mission UUID")
    try:
        mission_id = state.normalize_uuid(mission_id, "mission_id")
    except state.MissionStateError as exc:
        raise state.MissionStateError("SDD verification requires a canonical Mission UUID") from exc
    try:
        plan = snapshot.load_plan(Path(runs_dir), mission_id, digest)
    except snapshot.SnapshotError as exc:
        raise state.MissionStateError(f"SDD plan binding failed closed: {exc}") from exc
    return {
        "schema_version": SDD_PLAN_PACKET_SCHEMA,
        "mission_id": mission_id,
        "plan_sha256": digest,
        "plan": plan,
    }


def archived_evidence(runs_dir: Path, current: dict) -> dict | None:
    """Return exact frozen plan + binding bytes for archiving, or ``None``.

    The ledger pin is the authority. Any missing/corrupt/foreign live snapshot
    fails closed here, before staging publishes archive bytes.
    """
    packet = verify(runs_dir, current)
    if packet is None:
        return None
    digest = packet["plan_sha256"]
    mission_id = packet["mission_id"]
    try:
        with fleet_safe_paths.RootedFS(Path(runs_dir)) as rooted:
            plan = rooted.read_regular(
                Path("sdd") / "blobs" / f"{digest}.json",
                directory_modes=(0o700, 0o700), file_mode=0o600,
                max_bytes=_MAX_PLAN_BYTES,
            )
            binding = rooted.read_regular(
                Path("sdd") / "missions" / f"{mission_id}.json",
                directory_modes=(0o700, 0o700), file_mode=0o600,
                max_bytes=_MAX_MANIFEST_BYTES,
            )
    except fleet_safe_paths.SafePathError as exc:
        raise state.MissionStateError(f"SDD archive evidence unavailable: {exc}") from exc
    if hashlib.sha256(plan).hexdigest() != digest:
        raise state.MissionStateError("SDD archive evidence digest mismatch")
    return {"plan": plan, "binding": binding}


def verify_archived(mission_id: str, ledger_digest: str, plan_raw: bytes,
                    binding_raw: bytes) -> dict:
    """Offline verify archived SDD evidence against the ledger pin.

    Uses only the durable archive bytes and the ledger-derived digest; never
    the live SDD store, source plan, candidate or runtime files.
    """
    if not isinstance(ledger_digest, str) or not state.SHA256.fullmatch(ledger_digest):
        raise state.MissionStateError("archived SDD ledger pin is not a SHA-256 digest")
    if hashlib.sha256(plan_raw).hexdigest() != ledger_digest:
        raise state.MissionStateError("archived SDD plan does not match the ledger pin")
    try:
        binding = snapshot.parse_manifest_bytes(binding_raw)
    except snapshot.SnapshotError as exc:
        raise state.MissionStateError(f"archived SDD binding is invalid: {exc}") from exc
    if binding["mission_id"] != mission_id or binding["plan_sha256"] != ledger_digest:
        raise state.MissionStateError("archived SDD binding is foreign or substituted")
    try:
        plan = snapshot.parse_plan_bytes(plan_raw, "archived SDD plan")
    except snapshot.SnapshotError as exc:
        raise state.MissionStateError(f"archived SDD plan is invalid: {exc}") from exc
    return {
        "schema_version": SDD_PLAN_PACKET_SCHEMA,
        "mission_id": mission_id,
        "plan_sha256": ledger_digest,
        "plan": plan,
    }
