#!/usr/bin/env python3
"""Declarative Herdr six-role fleet roster contract and offline validator.

FLEET-01 declares six supervised-maintenance posts for the Herdr lane. This
module validates the manifest shape and the exact v1 identities only. It never
launches a CLI, provider, Herdr session or agent, grants no runtime permission
and asserts no OS isolation: ``workspace_access`` is a contractual declaration,
not an attested sandbox. ``reasoning_requested`` and ``model`` are requested
identities, not observed evidence.

In the canonical repository the manifest lives at
``orchestration/fleet/herdr-six-role-v1.json``. This contribution ships the
manifest, validator and tests standalone in one directory.

CLI::

    python3 -B scripts/fleet_herdr_roster.py PATH

prints one JSON object and exits 0 for a valid roster, or prints an error JSON
object and exits 1 for an invalid roster or usage error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import fleet_json

SCHEMA_VERSION = "herdr.fleet.roster.v1"
MODE = "supervised-maintenance"
INPUT_POLICY = "independent-v1"
CLOSURE_AUTHORITY = "controller"
CANONICAL_WRITER = "worker_sol"
MAX_CANONICAL_WRITERS = 1

TOP_FIELDS = (
    "schema_version",
    "mode",
    "mission_enabled",
    "canonical_writer",
    "input_policy",
    "closure_authority",
    "max_canonical_writers",
    "roles",
)
ROLE_FIELDS = (
    "id",
    "role",
    "cli",
    "provider",
    "model",
    "reasoning_requested",
    "workspace_access",
    "deliverable",
)

ROLE_KINDS = {"lead", "research", "worker", "reviewer", "verifier"}
READER_KINDS = {"lead", "research", "reviewer", "verifier"}
CLIS = {"codex", "opencode"}
PROVIDERS = {"openai", "deepseek"}
MODELS = {"gpt-6-astra", "gpt-5.6-sol", "deepseek-flash"}
REASONING = {"high", "thinking"}
WORKSPACE_ACCESS = {"read-only", "canonical-candidate", "isolated-contribution"}

# The single canonical v1 roster. Declared, not inferred from configuration.
CANONICAL_ROLES = (
    {
        "id": "lead",
        "role": "lead",
        "cli": "codex",
        "provider": "openai",
        "model": "gpt-6-astra",
        "reasoning_requested": "high",
        "workspace_access": "read-only",
        "deliverable": "plan-and-synthesis",
    },
    {
        "id": "research",
        "role": "research",
        "cli": "codex",
        "provider": "openai",
        "model": "gpt-6-astra",
        "reasoning_requested": "high",
        "workspace_access": "read-only",
        "deliverable": "evidence-report",
    },
    {
        "id": "worker_sol",
        "role": "worker",
        "cli": "codex",
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "reasoning_requested": "high",
        "workspace_access": "canonical-candidate",
        "deliverable": "canonical-candidate",
    },
    {
        "id": "worker_deepseek",
        "role": "worker",
        "cli": "opencode",
        "provider": "deepseek",
        "model": "deepseek-flash",
        "reasoning_requested": "thinking",
        "workspace_access": "isolated-contribution",
        "deliverable": "isolated-contribution",
    },
    {
        "id": "reviewer",
        "role": "reviewer",
        "cli": "codex",
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "reasoning_requested": "high",
        "workspace_access": "read-only",
        "deliverable": "review-findings",
    },
    {
        "id": "verifier",
        "role": "verifier",
        "cli": "codex",
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "reasoning_requested": "high",
        "workspace_access": "read-only",
        "deliverable": "verification-result",
    },
)
EXPECTED_BY_ID = {row["id"]: row for row in CANONICAL_ROLES}


def loads_strict(raw: str | bytes) -> Any:
    """Parse JSON rejecting duplicate keys and ``NaN``/``Infinity`` values."""
    try:
        return fleet_json.loads(raw)
    except fleet_json.FleetJSONError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc


def _require_text(value: Any, where: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{where} must be a nonempty string")
    return value


def _require_exact_keys(value: Any, expected: tuple[str, ...], where: str) -> None:
    if type(value) is not dict:
        raise ValueError(f"{where} must be an object")
    keys = set(value)
    wanted = set(expected)
    if keys != wanted:
        missing = sorted(wanted - keys)
        extra = sorted(keys - wanted)
        raise ValueError(f"{where} fields mismatch: missing={missing} extra={extra}")


def _require_enum(value: Any, allowed: set[str], where: str) -> str:
    text = _require_text(value, where)
    if text not in allowed:
        raise ValueError(f"{where} has unsupported value: {text!r}")
    return text


def validate_roster(value: Any) -> dict[str, Any]:
    """Return an equivalent copy of a valid v1 roster or raise ``ValueError``.

    Enforces strict types, closed fields, six unique posts, the exact v1
    identities, a single canonical writer, read-only readers and a disabled
    Mission. It does not execute anything or attest OS enforcement.
    """
    _require_exact_keys(value, TOP_FIELDS, "roster")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    if value["mode"] != MODE:
        raise ValueError("unsupported mode")
    if type(value["mission_enabled"]) is not bool or value["mission_enabled"] is not False:
        raise ValueError("mission_enabled must be the boolean false")
    canonical_writer = _require_text(value["canonical_writer"], "canonical_writer")
    if canonical_writer != CANONICAL_WRITER:
        raise ValueError(f"canonical_writer must be {CANONICAL_WRITER}")
    if value["input_policy"] != INPUT_POLICY:
        raise ValueError(f"input_policy must be {INPUT_POLICY}")
    if value["closure_authority"] != CLOSURE_AUTHORITY:
        raise ValueError(f"closure_authority must be {CLOSURE_AUTHORITY}")
    max_writers = value["max_canonical_writers"]
    if type(max_writers) is not int or max_writers != MAX_CANONICAL_WRITERS:
        raise ValueError(f"max_canonical_writers must be {MAX_CANONICAL_WRITERS}")

    roles_value = value["roles"]
    if type(roles_value) is not list or len(roles_value) != len(CANONICAL_ROLES):
        raise ValueError(f"roles must list exactly {len(CANONICAL_ROLES)} posts")

    roles: list[dict[str, Any]] = []
    seen: set[str] = set()
    canonical: list[str] = []
    for index, row in enumerate(roles_value):
        where = f"roles[{index}]"
        _require_exact_keys(row, ROLE_FIELDS, where)
        ident = _require_text(row["id"], f"{where}.id")
        if ident in seen:
            raise ValueError(f"duplicate role id: {ident}")
        seen.add(ident)
        role = _require_enum(row["role"], ROLE_KINDS, f"{where}.role")
        cli = _require_enum(row["cli"], CLIS, f"{where}.cli")
        provider = _require_enum(row["provider"], PROVIDERS, f"{where}.provider")
        model = _require_enum(row["model"], MODELS, f"{where}.model")
        reasoning = _require_enum(
            row["reasoning_requested"], REASONING, f"{where}.reasoning_requested"
        )
        access = _require_enum(
            row["workspace_access"], WORKSPACE_ACCESS, f"{where}.workspace_access"
        )
        deliverable = _require_text(row["deliverable"], f"{where}.deliverable")
        if role in READER_KINDS and access != "read-only":
            raise ValueError(f"{where}: reader role cannot hold write access")
        if role == "worker" and access == "read-only":
            raise ValueError(f"{where}: worker role requires write access")
        if access == "canonical-candidate":
            canonical.append(ident)
            if role != "worker":
                raise ValueError(f"{where}: canonical writer must be a worker")
        roles.append(
            {
                "id": ident,
                "role": role,
                "cli": cli,
                "provider": provider,
                "model": model,
                "reasoning_requested": reasoning,
                "workspace_access": access,
                "deliverable": deliverable,
            }
        )

    if len(canonical) != 1 or canonical[0] != canonical_writer:
        raise ValueError(
            "exactly one canonical writer is required and must match canonical_writer"
        )
    if max_writers != len(canonical):
        raise ValueError("max_canonical_writers must match the canonical writer count")
    if len(seen) != len(CANONICAL_ROLES):
        raise ValueError(f"exactly {len(CANONICAL_ROLES)} unique role ids are required")

    actual = {row["id"]: row for row in roles}
    if actual != EXPECTED_BY_ID:
        drifted = sorted(
            ident
            for ident in set(actual) | set(EXPECTED_BY_ID)
            if actual.get(ident) != EXPECTED_BY_ID.get(ident)
        )
        raise ValueError(f"roster identities do not match v1: {drifted}")

    return {
        "schema_version": value["schema_version"],
        "mode": value["mode"],
        "mission_enabled": value["mission_enabled"],
        "canonical_writer": value["canonical_writer"],
        "input_policy": value["input_policy"],
        "closure_authority": value["closure_authority"],
        "max_canonical_writers": value["max_canonical_writers"],
        "roles": roles,
    }


def load_roster(path: str | Path) -> dict[str, Any]:
    """Load, strictly parse and validate a roster manifest from ``path``.

    A missing or unreadable file is reported as ``ValueError`` so callers and
    the CLI handle every rejection uniformly.
    """
    location = Path(path)
    try:
        raw = location.read_bytes()
    except OSError as exc:
        raise ValueError(f"{location}: cannot read roster: {exc}") from exc
    try:
        return validate_roster(loads_strict(raw))
    except ValueError as exc:
        raise ValueError(f"{location}: {exc}") from exc


def _summary(roster: dict[str, Any]) -> dict[str, Any]:
    return {
        "valid": True,
        "mission_enabled": roster["mission_enabled"],
        "schema_version": roster["schema_version"],
        "mode": roster["mode"],
        "canonical_writer": roster["canonical_writer"],
        "max_canonical_writers": roster["max_canonical_writers"],
        "input_policy": roster["input_policy"],
        "closure_authority": roster["closure_authority"],
        "role_count": len(roster["roles"]),
        "role_ids": [row["id"] for row in roster["roles"]],
    }


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        _emit({"valid": False, "error": "usage: fleet_herdr_roster.py PATH"})
        return 1
    try:
        roster = load_roster(args[0])
    except (OSError, ValueError) as exc:
        _emit({"valid": False, "error": str(exc)})
        return 1
    _emit(_summary(roster))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
