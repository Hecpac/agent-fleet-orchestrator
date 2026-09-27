#!/usr/bin/env python3
"""Neutral Mission Control helpers shared by the modern entry point and the legacy driver.

Manifest and durable-file readers, exact Git reads, the audit-trust policy check and
the shared ``MissionRunError``. Nothing here selects a lane or starts a runtime.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
from typing import Any

import fleet_json
import fleet_manifest
import fleet_mission
import fleet_mission_state as mission_state
import fleet_safe_paths

ROOT = Path(__file__).resolve().parents[1]
RISK_SPEC = importlib.util.spec_from_file_location(
    "fleet_risk", ROOT / "scripts" / "fleet-risk.py"
)
assert RISK_SPEC and RISK_SPEC.loader
fleet_risk = importlib.util.module_from_spec(RISK_SPEC)
RISK_SPEC.loader.exec_module(fleet_risk)


class MissionRunError(RuntimeError):
    """Mission runner reconciliation cannot proceed safely."""


def enforce_audit_trust(
    compiled: dict[str, Any], categories: list[str], execution_profile: str
) -> None:
    """Prevent runtime risk/profile requirements from accepting weaker audit policy."""
    policy = compiled["workflow"]["audit"]
    category_set = set(categories)
    worm_categories = category_set & set(policy["worm_required_for"])
    external_required = execution_profile == "regulated" or "regulated" in category_set
    if worm_categories and policy["mode"] != "worm":
        raise MissionRunError(
            "mission risk requires WORM audit for categories: "
            + ", ".join(sorted(worm_categories))
        )
    if external_required and (
        policy["mode"] != "worm" or policy["trust_scope"] != "external-compliance"
    ):
        raise MissionRunError(
            "regulated execution or risk requires worm mode with external-compliance trust"
        )


def parse_manifest(path: Path) -> dict[str, str]:
    try:
        parent = path.parent.resolve(strict=True)
        with fleet_safe_paths.RootedFS(parent) as rooted:
            raw = rooted.read_regular(
                path.name,
                directory_modes=(),
                file_mode=0o600,
                max_bytes=16 * 1024 * 1024,
                require_single_link=True,
            )
            rooted.assert_root_binding()
        return fleet_manifest.normalize(fleet_manifest.parse_bytes(raw))
    except (
        OSError,
        RuntimeError,
        fleet_manifest.ManifestError,
        fleet_safe_paths.SafePathError,
    ) as exc:
        raise MissionRunError(f"cannot read fleet manifest {path}: {exc}") from exc


def verify_manifest_binding(manifest: dict[str, str], compiled: dict[str, Any]) -> None:
    try:
        fleet_manifest.verify_compiled_binding(manifest, compiled)
    except fleet_manifest.ManifestError as exc:
        raise MissionRunError(f"compiled manifest binding drift: {exc}") from exc


def verify_manifest_repository_binding(
    manifest: dict[str, str], *, target_repo: Path, base_sha: str
) -> None:
    if manifest.get("target_repo") != str(target_repo):
        raise MissionRunError("fleet manifest target_repo drift")
    if manifest.get("base_sha") != base_sha:
        raise MissionRunError("fleet manifest base_sha drift")
    writers = {
        key.removesuffix(".authority")
        for key, value in manifest.items()
        if key.endswith(".authority") and value == "write"
    }
    for writer in writers:
        if manifest.get(f"{writer}.base_sha") != base_sha:
            raise MissionRunError(f"fleet manifest writer base_sha drift: {writer}")


def load_durable_json(
    runs_dir: Path,
    relative: Path,
    *,
    directory_modes: tuple[int, ...],
) -> dict[str, Any]:
    """Read one mission-owned JSON object through its descriptor-bound root."""

    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            raw = rooted.read_regular(
                relative,
                directory_modes=directory_modes,
                file_mode=0o600,
                max_bytes=16 * 1024 * 1024,
                require_single_link=True,
            )
            rooted.assert_root_binding()
        value = fleet_json.loads(raw)
    except (fleet_safe_paths.SafePathError, fleet_json.FleetJSONError) as exc:
        raise MissionRunError(f"cannot load durable JSON {relative}: {exc}") from exc
    if not isinstance(value, dict):
        raise MissionRunError(f"expected durable JSON object: {relative}")
    if raw != fleet_json.canonical_bytes(value) + b"\n":
        raise MissionRunError(f"durable JSON bytes are not canonical: {relative}")
    return value


def load_mission_text(
    runs_dir: Path,
    mission_id: str,
    leaf: str,
    *,
    max_bytes: int,
) -> str:
    relative = Path("missions") / mission_id / leaf
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            raw = rooted.read_regular(
                relative,
                directory_modes=(0o700, 0o700),
                file_mode=0o600,
                max_bytes=max_bytes,
                require_single_link=True,
            )
            rooted.assert_root_binding()
        return raw.decode("utf-8", errors="strict")
    except (fleet_safe_paths.SafePathError, UnicodeDecodeError) as exc:
        raise MissionRunError(
            f"cannot load durable mission text {leaf}: {exc}"
        ) from exc


def git_read(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise MissionRunError(result.stderr.strip() or "Git repository read failed")
    return result.stdout.strip()


def exact_git_toplevel(repo: Path) -> Path:
    try:
        physical = repo.expanduser().resolve(strict=True)
    except OSError as exc:
        raise MissionRunError(f"target repository is unavailable: {repo}") from exc
    top = Path(
        git_read(physical, "rev-parse", "--path-format=absolute", "--show-toplevel")
    )
    try:
        physical_top = top.resolve(strict=True)
    except OSError as exc:
        raise MissionRunError("Git toplevel is unavailable") from exc
    if physical != physical_top:
        raise MissionRunError(
            f"target_repo must be the exact physical Git toplevel: {physical_top}"
        )
    return physical_top


def effect_compiled(
    runs_dir: Path, mission_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return fleet_mission.load_mission_compiled(runs_dir, mission_id, mode="effect")
    except (fleet_mission.MissionError, mission_state.MissionStateError) as exc:
        raise MissionRunError(
            f"compiled workflow is not effect-authorized: {exc}"
        ) from exc
