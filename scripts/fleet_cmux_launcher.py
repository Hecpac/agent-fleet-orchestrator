#!/usr/bin/env python3
"""Publish and execute private, digest-bound CMUX launch specifications."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Any
import uuid

import fleet_json
from fleet_safe_paths import RootedFS, SafePathError


SCHEMA_VERSION = 1
MAX_SPEC_BYTES = 1024 * 1024
MAX_RECEIPT_BYTES = 16 * 1024
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_SPEC_KEYS = {
    "schema_version",
    "launch_id",
    "label",
    "created_at",
    "cwd",
    "environment",
    "argv",
    "spec_sha256",
}
_RECEIPT_KEYS = {
    "schema_version",
    "launch_id",
    "label",
    "spec_sha256",
    "state",
    "accepted_at",
    "launcher_pid",
}


class LaunchError(RuntimeError):
    """A launch specification or receipt violated its exact contract."""


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    try:
        return fleet_json.canonical_bytes(value) + b"\n"
    except fleet_json.FleetJSONError as exc:
        raise LaunchError("launch JSON cannot be canonicalized") from exc


def _digest(value: dict[str, Any]) -> str:
    projected = dict(value)
    projected.pop("spec_sha256", None)
    return hashlib.sha256(_canonical_bytes(projected)).hexdigest()


def _normalize_launch_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise LaunchError("launch_id must be a canonical lowercase UUID") from exc
    normalized = str(parsed)
    if value != normalized:
        raise LaunchError("launch_id must be a canonical lowercase UUID")
    return normalized


def _paths(launch_id: str) -> tuple[str, str]:
    normalized = _normalize_launch_id(launch_id)
    prefix = f"cmux-launches/{normalized}"
    return f"{prefix}/spec.json", f"{prefix}/accepted.json"


def _parse_object(raw: bytes, *, where: str) -> dict[str, Any]:
    try:
        value = fleet_json.loads(raw)
    except fleet_json.FleetJSONError as exc:
        detail = str(exc)
        if detail.startswith("duplicate JSON object key: "):
            field = detail.removeprefix("duplicate JSON object key: ")
            raise LaunchError(f"{where} contains duplicate field: {field}") from exc
        if "non-finite JSON number" in detail:
            raise LaunchError(f"{where} contains non-finite JSON") from exc
        raise LaunchError(f"{where} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise LaunchError(f"{where} must be a JSON object")
    return value


def _validate_string(value: object, *, where: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or (not allow_empty and not value):
        raise LaunchError(f"{where} must be a NUL-free string")
    return value


def _validate_timestamp(value: object, *, where: str) -> str:
    text = _validate_string(value, where=where)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LaunchError(f"{where} must be an ISO-8601 timestamp") from exc
    if not text.endswith("Z") or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LaunchError(f"{where} must carry an explicit UTC timezone")
    return text


def _validate_spec(
    value: dict[str, Any], *, expected_digest: str | None = None
) -> dict[str, Any]:
    if set(value) != _SPEC_KEYS:
        raise LaunchError("launch spec fields do not match schema v1")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
    ):
        raise LaunchError("unsupported launch spec schema_version")
    launch_id = _normalize_launch_id(value["launch_id"])
    label = _validate_string(value["label"], where="label")
    if not _LABEL_RE.fullmatch(label) or ".." in label.split("/"):
        raise LaunchError("label is not canonical")
    _validate_timestamp(value["created_at"], where="created_at")
    cwd = _validate_string(value["cwd"], where="cwd")
    if not Path(cwd).is_absolute():
        raise LaunchError("cwd must be absolute")
    environment = value["environment"]
    if not isinstance(environment, dict) or len(environment) > 64:
        raise LaunchError("environment must be a bounded object")
    for name, env_value in environment.items():
        if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
            raise LaunchError("environment contains an invalid name")
        _validate_string(env_value, where=f"environment[{name}]", allow_empty=True)
    argv = value["argv"]
    if not isinstance(argv, list) or not argv or len(argv) > 512:
        raise LaunchError("argv must be a non-empty bounded list")
    for index, argument in enumerate(argv):
        _validate_string(argument, where=f"argv[{index}]")
    digest = value["spec_sha256"]
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise LaunchError("spec_sha256 is not canonical")
    if digest != _digest(value):
        raise LaunchError("launch spec digest mismatch")
    if expected_digest is not None and digest != expected_digest:
        raise LaunchError("launch spec does not match the expected digest")
    if launch_id != value["launch_id"]:
        raise LaunchError("launch_id normalization drift")
    return value


def _validate_receipt(
    value: dict[str, Any], *, launch_id: str, label: str, spec_sha256: str
) -> dict[str, Any]:
    if set(value) != _RECEIPT_KEYS:
        raise LaunchError("launch receipt fields do not match schema v1")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
    ):
        raise LaunchError("unsupported launch receipt schema_version")
    if value["launch_id"] != launch_id or value["label"] != label:
        raise LaunchError("launch receipt identity mismatch")
    if value["spec_sha256"] != spec_sha256:
        raise LaunchError("launch receipt digest mismatch")
    if value["state"] != "accepted":
        raise LaunchError("launch receipt is not accepted")
    _validate_timestamp(value["accepted_at"], where="accepted_at")
    pid = value["launcher_pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise LaunchError("launch receipt launcher_pid is invalid")
    return value


def _parse_environment(values: list[str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise LaunchError("--env requires NAME=VALUE")
        name, value = item.split("=", 1)
        if not _ENV_NAME_RE.fullmatch(name) or "\0" in value:
            raise LaunchError("--env contains an invalid entry")
        if name in environment:
            raise LaunchError(f"duplicate --env name: {name}")
        environment[name] = value
    return environment


def create_spec(
    runs_dir: Path | str,
    *,
    label: str,
    cwd: Path | str,
    environment: dict[str, str],
    command_shell: str,
) -> dict[str, str]:
    if not _LABEL_RE.fullmatch(label) or ".." in label.split("/"):
        raise LaunchError("label is not canonical")
    try:
        canonical_cwd = Path(cwd).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LaunchError("launch cwd is unavailable") from exc
    if not canonical_cwd.is_dir():
        raise LaunchError("launch cwd is not a directory")
    try:
        argv = shlex.split(command_shell, posix=True)
    except ValueError as exc:
        raise LaunchError("launch command is not valid shell quoting") from exc
    if not argv:
        raise LaunchError("launch command is empty")
    launch_id = str(uuid.uuid4())
    spec_relative, receipt_relative = _paths(launch_id)
    spec: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "launch_id": launch_id,
        "label": label,
        "created_at": _now(),
        "cwd": str(canonical_cwd),
        "environment": dict(sorted(environment.items())),
        "argv": argv,
    }
    spec["spec_sha256"] = _digest(spec)
    _validate_spec(spec)
    content = _canonical_bytes(spec)
    if len(content) > MAX_SPEC_BYTES:
        raise LaunchError("launch spec exceeds its size limit")
    with RootedFS(runs_dir) as rooted:
        published = rooted.atomic_write(
            spec_relative,
            content,
            directory_modes=(0o700, 0o700),
            file_mode=0o600,
        )
        rooted.stat_regular(
            spec_relative,
            directory_modes=(0o700, 0o700),
            file_mode=0o600,
            require_single_link=True,
        )
    return {
        "launch_id": launch_id,
        "spec_sha256": spec["spec_sha256"],
        "spec_path": str(published),
        "receipt_path": str(Path(runs_dir).expanduser().resolve() / receipt_relative),
    }


def read_spec(
    runs_dir: Path | str, launch_id: str, *, expected_digest: str | None = None
) -> dict[str, Any]:
    spec_relative, _ = _paths(launch_id)
    if expected_digest is not None and not _DIGEST_RE.fullmatch(expected_digest):
        raise LaunchError("expected digest is not canonical")
    with RootedFS(runs_dir) as rooted:
        raw = rooted.read_regular(
            spec_relative,
            directory_modes=(0o700, 0o700),
            file_mode=0o600,
            max_bytes=MAX_SPEC_BYTES,
            require_single_link=True,
        )
    spec = _validate_spec(
        _parse_object(raw, where="launch spec"), expected_digest=expected_digest
    )
    if raw != _canonical_bytes(spec):
        raise LaunchError("launch spec bytes are not canonical")
    return spec


def publish_acceptance(
    runs_dir: Path | str, launch_id: str, *, expected_digest: str
) -> dict[str, Any]:
    spec = read_spec(runs_dir, launch_id, expected_digest=expected_digest)
    _, receipt_relative = _paths(launch_id)
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "launch_id": launch_id,
        "label": spec["label"],
        "spec_sha256": expected_digest,
        "state": "accepted",
        "accepted_at": _now(),
        "launcher_pid": os.getpid(),
    }
    _validate_receipt(
        receipt,
        launch_id=launch_id,
        label=spec["label"],
        spec_sha256=expected_digest,
    )
    with RootedFS(runs_dir) as rooted:
        rooted.atomic_write(
            receipt_relative,
            _canonical_bytes(receipt),
            directory_modes=(0o700, 0o700),
            file_mode=0o600,
        )
        rooted.stat_regular(
            receipt_relative,
            directory_modes=(0o700, 0o700),
            file_mode=0o600,
            require_single_link=True,
        )
    return receipt


def verify_acceptance(
    runs_dir: Path | str, launch_id: str, *, expected_digest: str
) -> dict[str, Any] | None:
    spec = read_spec(runs_dir, launch_id, expected_digest=expected_digest)
    _, receipt_relative = _paths(launch_id)
    with RootedFS(runs_dir) as rooted:
        raw = rooted.read_regular_optional(
            receipt_relative,
            directory_modes=(0o700, 0o700),
            file_mode=0o600,
            max_bytes=MAX_RECEIPT_BYTES,
            require_single_link=True,
        )
    if raw is None:
        return None
    receipt = _validate_receipt(
        _parse_object(raw, where="launch receipt"),
        launch_id=launch_id,
        label=spec["label"],
        spec_sha256=expected_digest,
    )
    if raw != _canonical_bytes(receipt):
        raise LaunchError("launch receipt bytes are not canonical")
    return receipt


def execute_spec(runs_dir: Path | str, launch_id: str, *, expected_digest: str) -> None:
    spec = read_spec(runs_dir, launch_id, expected_digest=expected_digest)
    publish_acceptance(runs_dir, launch_id, expected_digest=expected_digest)
    environment = os.environ.copy()
    environment.update(spec["environment"])
    os.chdir(spec["cwd"])
    os.execvpe(spec["argv"][0], spec["argv"], environment)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--runs-dir", required=True)
    create.add_argument("--label", required=True)
    create.add_argument("--cwd", required=True)
    create.add_argument("--env", action="append", default=[])
    create.add_argument("--command-shell", required=True)

    for name in ("run", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("runs_dir")
        command.add_argument("launch_id")
        command.add_argument("spec_sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            descriptor = create_spec(
                args.runs_dir,
                label=args.label,
                cwd=args.cwd,
                environment=_parse_environment(args.env),
                command_shell=args.command_shell,
            )
            print(descriptor["launch_id"] + "\x1f" + descriptor["spec_sha256"])
            return 0
        if args.command == "run":
            execute_spec(
                args.runs_dir, args.launch_id, expected_digest=args.spec_sha256
            )
            raise AssertionError("exec unexpectedly returned")
        receipt = verify_acceptance(
            args.runs_dir, args.launch_id, expected_digest=args.spec_sha256
        )
        if receipt is None:
            return 1
        sys.stdout.buffer.write(fleet_json.canonical_bytes(receipt) + b"\n")
        return 0
    except (LaunchError, SafePathError, OSError) as exc:
        print(f"fleet_cmux_launcher: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
