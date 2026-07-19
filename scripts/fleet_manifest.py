#!/usr/bin/env python3
"""Strict, backward-compatible fleet manifest reader and migrator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any
import uuid

import fleet_providers


CURRENT_CONTRACT_VERSION = 3
SUPPORTED_CONTRACT_VERSIONS = (1, 2, CURRENT_CONTRACT_VERSION)
EXECUTION_PROFILES = ("native", "sandboxed", "regulated")
TRACKING_PROTOCOLS = ("legacy-cmux", "control-v1")
SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
BINDING_FIELDS = (
    "compiled_digest",
    "router_digest",
    "roster_digest",
    "launch_digest",
)
ROSTER_FIELDS = (
    "role_type",
    "runner",
    "phase",
    "authority",
    "provider",
    "hook_source",
    "model",
    "variant",
)
class ManifestError(RuntimeError):
    """A fleet manifest is malformed, unsafe, or internally inconsistent."""


def _validate_value(key: str, value: str) -> None:
    if not value and not key.endswith(".hook_source"):
        raise ManifestError(f"empty manifest value: {key}")
    if any(character in value for character in ("\x00", "\r", "\n", "\x1f")):
        raise ManifestError(f"manifest value contains a forbidden control character: {key}")


def parse_bytes(content: bytes) -> dict[str, str]:
    """Parse exact manifest bytes without accepting ambiguous rows."""

    try:
        rows = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ManifestError("manifest is not valid UTF-8") from exc
    values: dict[str, str] = {}
    for line_number, row in enumerate(rows, 1):
        if not row or row.startswith("#"):
            continue
        if "=" not in row:
            raise ManifestError(f"malformed manifest row {line_number}")
        key, value = row.split("=", 1)
        if not SAFE_KEY.fullmatch(key):
            raise ManifestError(f"invalid manifest key at row {line_number}")
        if key in values:
            raise ManifestError(f"duplicate manifest key: {key}")
        _validate_value(key, value)
        values[key] = value
    if not values:
        raise ManifestError("manifest is empty")
    return values


def read(path: Path, *, require_regular: bool = True) -> dict[str, str]:
    """Read a manifest without silently accepting duplicates or malformed rows."""
    try:
        info = path.lstat()
        if require_regular and (not stat.S_ISREG(info.st_mode) or path.is_symlink()):
            raise ManifestError(f"manifest must be a regular non-symlink file: {path}")
        content = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    return parse_bytes(content)


def normalize(values: dict[str, str]) -> dict[str, str]:
    """Return runtime defaults while preserving whether provenance is legacy."""
    result = dict(values)
    raw_contract = result.get("manifest_contract_version", "1")
    try:
        contract = int(raw_contract)
    except ValueError as exc:
        raise ManifestError("manifest_contract_version must be an integer") from exc
    if contract not in SUPPORTED_CONTRACT_VERSIONS:
        raise ManifestError(f"unsupported manifest contract version: {contract}")
    mission_bound = "mission_id" in result
    if mission_bound and contract < CURRENT_CONTRACT_VERSION:
        raise ManifestError(
            f"mission-bound manifest contract v{contract} predates exact launch "
            "binding; restart the mission fleet for the v3 cutover"
        )
    profile = result.get("execution_profile", "native")
    if profile not in EXECUTION_PROFILES:
        raise ManifestError(f"unsupported execution_profile: {profile}")
    tracking = result.get(
        "tracking_protocol", "legacy-cmux" if contract == 1 else "control-v1"
    )
    if tracking not in TRACKING_PROTOCOLS:
        raise ManifestError(f"unsupported tracking_protocol: {tracking}")
    if contract == 1 and tracking != "legacy-cmux":
        raise ManifestError("legacy manifest cannot claim control-v1 tracking")
    if mission_bound and tracking != "control-v1":
        raise ManifestError("mission-bound manifest v3 requires control-v1 tracking")
    if mission_bound:
        for field in BINDING_FIELDS:
            if not SHA256.fullmatch(result.get(field, "")):
                raise ManifestError(
                    f"mission-bound manifest v3 requires valid {field} binding"
                )
    result["manifest_contract_version"] = str(contract)
    result["execution_profile"] = profile
    result["tracking_protocol"] = tracking
    return result


def load(path: Path) -> dict[str, str]:
    return normalize(read(path))


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_compiled_digest(compiled: dict[str, Any]) -> None:
    digest = compiled.get("compiled_digest")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ManifestError("compiled workflow has no valid compiled_digest")
    unsigned = {key: value for key, value in compiled.items() if key != "compiled_digest"}
    if _canonical_sha256(unsigned) != digest:
        raise ManifestError("compiled workflow digest mismatch")
    router_digest = compiled.get("router_digest")
    if not isinstance(router_digest, str) or len(router_digest) != 64:
        raise ManifestError("compiled workflow has no valid router_digest")


def _project_member(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "instance_id": str(member.get("instance_id", "")),
        **{
            field: (
                str(member.get(field, ""))
                if field == "hook_source"
                else member.get(field)
            )
            for field in ROSTER_FIELDS
        },
    }


def _project_launch_member(member: dict[str, Any]) -> dict[str, Any]:
    """Project the complete private member plan plus its resolved adapter."""
    if not isinstance(member, dict):
        raise ManifestError("resolved launch member is malformed")
    try:
        adapter = fleet_providers.DEFAULT_REGISTRY.resolve(
            hook_source=str(member.get("hook_source") or ""),
            provider=str(member.get("provider") or ""),
        ).name
    except fleet_providers.ProviderError as exc:
        raise ManifestError(f"resolved launch provider is invalid: {exc}") from exc
    return {
        "member": member,
        "provider_adapter": adapter,
    }


def launch_digest(plan: dict[str, Any]) -> str:
    """Hash the effective launch specification without publishing its contents."""
    members = ([plan["lead"]] if plan.get("lead") else []) + list(
        plan.get("instances") or []
    )
    return _canonical_sha256(
        {
            "schema_version": 1,
            "plan_schema_version": plan.get("schema_version"),
            "preset": str(plan.get("preset", "")),
            "mode": plan.get("mode"),
            "identity_groups": plan.get("identity_groups"),
            "limits": plan.get("limits"),
            "members": [_project_launch_member(member) for member in members],
        }
    )


def _compiled_roster(compiled: dict[str, Any], preset: str) -> dict[str, Any]:
    _validate_compiled_digest(compiled)
    resolved = compiled.get("resolved")
    if not isinstance(resolved, dict):
        raise ManifestError("compiled workflow has no resolved roster")
    if preset == resolved.get("preset"):
        mode = resolved.get("mode")
        groups = resolved.get("identity_groups")
        lead = resolved.get("lead")
        instances = resolved.get("instances")
    elif preset == resolved.get("assurance_preset"):
        mode = resolved.get("assurance_mode")
        groups = resolved.get("assurance_identity_groups")
        lead = resolved.get("assurance_lead")
        instances = resolved.get("assurance_instances")
    else:
        raise ManifestError(f"preset is not bound by compiled workflow: {preset}")
    if not isinstance(mode, str) or not isinstance(groups, list) or not isinstance(instances, list):
        raise ManifestError("compiled workflow lacks an exact roster for the selected preset")
    members = ([lead] if lead is not None else []) + instances
    projected: list[dict[str, Any]] = []
    for member in members:
        if not isinstance(member, dict):
            raise ManifestError("compiled roster member is malformed")
        if not member.get("instance_id"):
            raise ManifestError("compiled roster member lacks instance_id")
        projected.append(_project_member(member))
    return {
        "preset": preset,
        "mode": mode,
        "identity_groups": groups,
        "members": projected,
    }


def _compiled_launch_digest(compiled: dict[str, Any], preset: str) -> str:
    _validate_compiled_digest(compiled)
    resolved = compiled.get("resolved")
    if not isinstance(resolved, dict):
        raise ManifestError("compiled workflow has no resolved launch binding")
    if preset == resolved.get("preset"):
        value = resolved.get("launch_digest")
    elif preset == resolved.get("assurance_preset"):
        value = resolved.get("assurance_launch_digest")
    else:
        raise ManifestError(f"preset is not bound by compiled workflow: {preset}")
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ManifestError(
            "compiled workflow lacks a valid launch digest for the selected preset"
        )
    return value


def _binding(
    compiled: dict[str, Any], roster: dict[str, Any], expected_launch_digest: str
) -> dict[str, str]:
    return {
        "compiled_digest": str(compiled["compiled_digest"]),
        "router_digest": str(compiled["router_digest"]),
        "roster_digest": _canonical_sha256(roster),
        "launch_digest": expected_launch_digest,
    }


def bind_plan(
    router: dict[str, Any], plan: dict[str, Any], compiled: dict[str, Any]
) -> dict[str, str]:
    """Verify a resolved router plan against its frozen workflow before effects."""
    preset = str(plan.get("preset", ""))
    expected = _compiled_roster(compiled, preset)
    expected_launch_digest = _compiled_launch_digest(compiled, preset)
    actual_members = ([plan["lead"]] if plan.get("lead") else []) + list(
        plan.get("instances") or []
    )
    actual = {
        "preset": preset,
        "mode": plan.get("mode"),
        "identity_groups": plan.get("identity_groups"),
        "members": [_project_member(member) for member in actual_members],
    }
    if _canonical_sha256(router) != compiled["router_digest"]:
        raise ManifestError("live router digest drifted from compiled workflow")
    if actual != expected:
        raise ManifestError("resolved router roster drifted from compiled workflow")
    if launch_digest(plan) != expected_launch_digest:
        raise ManifestError("resolved router launch drifted from compiled workflow")
    return _binding(compiled, expected, expected_launch_digest)


def binding_for_preset(compiled: dict[str, Any], preset: str) -> dict[str, str]:
    """Return the immutable hashes used by deterministic test/runtime manifests."""
    return _binding(
        compiled,
        _compiled_roster(compiled, preset),
        _compiled_launch_digest(compiled, preset),
    )


def verify_compiled_binding(values: dict[str, str], compiled: dict[str, Any]) -> None:
    """Reject a published manifest whose frozen hashes or exact roster drifted."""
    preset = values.get("preset", "")
    expected = _compiled_roster(compiled, preset)
    binding = _binding(compiled, expected, _compiled_launch_digest(compiled, preset))
    for field in BINDING_FIELDS:
        if values.get(field) != binding[field]:
            raise ManifestError(f"manifest {field} drift")
    try:
        group_count = int(values.get("identity_group.count", "0"))
    except ValueError as exc:
        raise ManifestError("manifest identity_group.count is invalid") from exc
    groups = [
        [item for item in values.get(f"identity_group.{index}", "").split(",") if item]
        for index in range(1, group_count + 1)
    ]
    if values.get("mode") != expected["mode"] or groups != expected["identity_groups"]:
        raise ManifestError("manifest mode or identity groups drifted from compiled workflow")
    actual_ids = {
        key.rsplit(".", 1)[0]
        for key in values
        if "." in key and key.rsplit(".", 1)[1] in ROSTER_FIELDS
    }
    expected_ids = [member["instance_id"] for member in expected["members"]]
    if actual_ids != set(expected_ids):
        raise ManifestError("manifest roster membership drifted from compiled workflow")
    actual_members: list[dict[str, Any]] = []
    for instance_id in expected_ids:
        member: dict[str, Any] = {"instance_id": instance_id}
        for field in ROSTER_FIELDS:
            raw = values.get(f"{instance_id}.{field}")
            member[field] = None if field == "variant" and raw is None else raw
        actual_members.append(member)
    actual = {
        "preset": preset,
        "mode": values.get("mode"),
        "identity_groups": groups,
        "members": actual_members,
    }
    if actual != expected:
        raise ManifestError("manifest roster drifted from compiled workflow")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def migrate(path: Path, *, in_place: bool = False) -> dict[str, str]:
    """Add profile metadata without inventing control-plane provenance.

    A legacy standalone fleet remains ``legacy-cmux`` after migration. Only a
    fresh boot may claim ``control-v1`` tracking. Mission-bound v1/v2 manifests
    must restart at the v3 cutover because migration cannot invent launch binding.
    """
    original = read(path)
    normalized = normalize(original)
    migrated = dict(original)
    migrated["manifest_contract_version"] = str(CURRENT_CONTRACT_VERSION)
    migrated["execution_profile"] = normalized["execution_profile"]
    migrated["tracking_protocol"] = normalized["tracking_protocol"]
    if in_place:
        ordered = list(original)
        for key in ("manifest_contract_version", "execution_profile", "tracking_protocol"):
            if key not in ordered:
                ordered.append(key)
        _atomic_write(
            path,
            ("".join(f"{key}={migrated[key]}\n" for key in ordered)).encode("utf-8"),
        )
    return migrated


def validate_profile(profile: str) -> str:
    if profile not in EXECUTION_PROFILES:
        raise ManifestError(
            "execution profile must be one of: " + ", ".join(EXECUTION_PROFILES)
        )
    return profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("manifest")
    migrate_parser = sub.add_parser("migrate")
    migrate_parser.add_argument("manifest")
    migrate_parser.add_argument("--in-place", action="store_true")
    profile_parser = sub.add_parser("validate-profile")
    profile_parser.add_argument("profile")
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            result = load(Path(args.manifest))
        elif args.command == "migrate":
            result = migrate(Path(args.manifest), in_place=args.in_place)
        else:
            result = {"execution_profile": validate_profile(args.profile)}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ManifestError, OSError) as exc:
        print(f"fleet-manifest: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
