#!/usr/bin/env python3
"""Strict, backward-compatible fleet manifest reader and migrator."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid


CURRENT_CONTRACT_VERSION = 2
EXECUTION_PROFILES = ("native", "sandboxed", "regulated")
TRACKING_PROTOCOLS = ("legacy-cmux", "control-v1")
SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ManifestError(RuntimeError):
    """A fleet manifest is malformed, unsafe, or internally inconsistent."""


def _validate_value(key: str, value: str) -> None:
    if not value and not key.endswith(".hook_source"):
        raise ManifestError(f"empty manifest value: {key}")
    if any(character in value for character in ("\x00", "\r", "\n", "\x1f")):
        raise ManifestError(f"manifest value contains a forbidden control character: {key}")


def read(path: Path, *, require_regular: bool = True) -> dict[str, str]:
    """Read a manifest without silently accepting duplicates or malformed rows."""
    try:
        info = path.lstat()
        if require_regular and (not stat.S_ISREG(info.st_mode) or path.is_symlink()):
            raise ManifestError(f"manifest must be a regular non-symlink file: {path}")
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
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


def normalize(values: dict[str, str]) -> dict[str, str]:
    """Return runtime defaults while preserving whether provenance is legacy."""
    result = dict(values)
    raw_contract = result.get("manifest_contract_version", "1")
    try:
        contract = int(raw_contract)
    except ValueError as exc:
        raise ManifestError("manifest_contract_version must be an integer") from exc
    if contract not in (1, CURRENT_CONTRACT_VERSION):
        raise ManifestError(f"unsupported manifest contract version: {contract}")
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
    result["manifest_contract_version"] = str(contract)
    result["execution_profile"] = profile
    result["tracking_protocol"] = tracking
    return result


def load(path: Path) -> dict[str, str]:
    return normalize(read(path))


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

    A legacy live fleet remains ``legacy-cmux`` after migration. Only a fresh
    contract-v2 boot may claim ``control-v1`` tracking.
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
