"""Explicit runtime compatibility; historical readability is not an upgrade.

The experimental instrumented launcher remains pinned to its original binaries.
This contract describes the official CLI lane, not OS sandbox conformance.
"""
from __future__ import annotations

from pathlib import Path

import fleet_json
import re

HERDR_VERSION = "0.9.0"
CODEX_VERSION = "0.153.4"
LEGACY_HERDR_VERSION = "0.8.2"
OFFICIAL_CONTRACT = {
    "version": 1,
    "lane": "official-cli",
    "herdr_version": HERDR_VERSION,
    "codex_version": CODEX_VERSION,
    "startup_guard": "codex-0.153-v1",
}


PERSONAL_CONTRACT = {**OFFICIAL_CONTRACT, "codex_version": "0.154.0", "startup_guard": "codex-0.154-v1"}
TASK_CONTEXT_CONTRACT = {**PERSONAL_CONTRACT, "skill_delivery": "task-inline-skills-v1"}
# Codex 0.159.3, certified against Herdr 0.9.0 on 2026-10-01. Its startup guard
# also judges the 0.159 Folder access notice and model migration prompt.
CODEX_0159_CONTRACT = {**OFFICIAL_CONTRACT, "codex_version": "0.159.3", "startup_guard": "codex-0.159-v1"}
TASK_CONTEXT_0159_CONTRACT = {**CODEX_0159_CONTRACT, "skill_delivery": "task-inline-skills-v1"}
KNOWN_CONTRACTS = (OFFICIAL_CONTRACT, PERSONAL_CONTRACT, TASK_CONTEXT_CONTRACT,
                   CODEX_0159_CONTRACT, TASK_CONTEXT_0159_CONTRACT)
# New Missions in both lanes; historical records keep their frozen contract.
CURRENT_CONTRACT = CODEX_0159_CONTRACT
CURRENT_TASK_CONTEXT_CONTRACT = TASK_CONTEXT_0159_CONTRACT


def task_inline(contract: object) -> bool:
    """A contract that delivers role skills inline in the frozen task."""
    return isinstance(contract, dict) and contract.get("skill_delivery") == "task-inline-skills-v1"


CERTIFIED_CONTRACT_VERSION = 2
# Placeholder certification of a candidate under certification; never registered.
CANDIDATE_CERTIFICATION = "0" * 64
_CERTIFIED_KEYS = frozenset(OFFICIAL_CONTRACT) | {"certification"}
_SEMVER = re.compile(r"\d+\.\d+\.\d+")


def certified_contract(entry: dict, *, task_inline_skills: bool = False) -> dict:
    """A version-2 contract bound to one registered Codex certification.

    It carries the certification id (sha256 of the record, also its CAS id),
    so an archived Mission stays self-describing without the machine registry.
    """
    contract = {"version": CERTIFIED_CONTRACT_VERSION, "lane": OFFICIAL_CONTRACT["lane"],
                "herdr_version": entry["herdr_version"], "codex_version": entry["codex_version"],
                "startup_guard": entry["startup_guard"], "certification": entry["certification_sha256"]}
    if task_inline_skills:
        contract["skill_delivery"] = "task-inline-skills-v1"
    return validate(contract)


def certified(contract: object) -> bool:
    return isinstance(contract, dict) and contract.get("version") == CERTIFIED_CONTRACT_VERSION


def _valid_certified(value: dict) -> bool:
    import fleet_herdr_startup
    keys = set(value) - {"skill_delivery"}
    return (keys == _CERTIFIED_KEYS and value.get("version") == CERTIFIED_CONTRACT_VERSION
            and value.get("lane") == OFFICIAL_CONTRACT["lane"] and value.get("herdr_version") == HERDR_VERSION
            and isinstance(value.get("codex_version"), str) and bool(_SEMVER.fullmatch(value["codex_version"]))
            and value.get("startup_guard") in fleet_herdr_startup.GUARDS
            and isinstance(value.get("certification"), str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", value["certification"]))
            and ("skill_delivery" not in value or value["skill_delivery"] == "task-inline-skills-v1"))


def validate(value: object) -> dict:
    if (not isinstance(value, dict) or type(value.get("version")) is not int
            or not (any(value == known for known in KNOWN_CONTRACTS) or _valid_certified(value))):
        raise ValueError("unsupported Herdr/Codex runtime contract")
    return dict(value)


def state_contract(state: dict) -> dict | None:
    """Read legacy records unchanged; never silently turn them into new runs."""
    schema = state.get("schema_version")
    if type(schema) is not int:
        raise ValueError("invalid Herdr state schema")
    if schema == 2:
        if state.get("backend_version") != LEGACY_HERDR_VERSION or "runtime_contract" in state:
            raise ValueError("invalid historical Herdr runtime binding")
        return None
    if schema == 3:
        contract = validate(state.get("runtime_contract"))
        if task_inline(contract):
            if not isinstance(state.get("context_artifact_id"), str) or not re.fullmatch(r"[0-9a-f]{64}", state["context_artifact_id"]):
                raise ValueError("task-inline runtime requires frozen context identity")
        elif "context_artifact_id" in state:
            raise ValueError("historical runtime cannot acquire task-inline context")
        if state.get("backend_version") != contract["herdr_version"] or state.get("launch_manifest_sha256") is not None:
            raise ValueError("Herdr runtime lane/version mismatch")
        return contract
    raise ValueError("unsupported Herdr state schema")


def anchor_bytes(state: dict) -> bytes | None:
    contract = state_contract(state)
    if contract is None:
        return None
    return fleet_json.canonical_bytes({"mission_id": state["mission_id"],
        "compiled_digest": state["compiled_digest"], "runtime_contract": contract,
        **({"context_artifact_id": state["context_artifact_id"]} if task_inline(contract) else {})}) + b"\n"


def read_state(rooted, relative: Path, *, mission_id: str, compiled_digest: str,
               directory_modes=(0o700, 0o700)) -> dict | None:
    """Offline shared runtime binding; no CLI call or historical record mutation.

    The backend additionally validates its complete roster and run identities.
    Readers must supply the Mission/compiled identity from their trusted input.
    """
    anchor = rooted.read_regular_optional(relative.parent / "herdr-runtime-contract.json",
        directory_modes=directory_modes, file_mode=0o600, max_bytes=4096)
    raw = rooted.read_regular_optional(relative, directory_modes=directory_modes,
        file_mode=0o600, max_bytes=4 * 1024 * 1024)
    if raw is None:
        if anchor is not None:
            raise ValueError("Herdr state missing after runtime contract was frozen; reconcile without a duplicate boot")
        return None
    value = fleet_json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Herdr backend state must be an object")
    if raw != fleet_json.canonical_bytes(value) + b"\n":
        raise ValueError("Herdr backend state bytes are not canonical")
    if value.get("mission_id") != mission_id or value.get("compiled_digest") != compiled_digest:
        raise ValueError("Herdr backend durable binding drift")
    if value.get("executor") == "fleet.mission.capsule.v2":
        # A distinct creation-bound executor; never reinterpret native state.
        import uuid
        import fleet_mission_capsule
        def read(name):
            return fleet_json.loads(rooted.read_regular(relative.parent / name,
                directory_modes=directory_modes, file_mode=0o600, max_bytes=16*1024*1024))
        options, creation = read("runtime-options.json"), read("creation-request.json")
        manifest = fleet_mission_capsule.validate_manifest(options.get("herdr_capsule_manifest"))
        generation = str(uuid.uuid5(uuid.UUID(mission_id),
            "capsule:" + fleet_json.sha256(manifest)))
        expected = {"schema_version": 3, "executor": "fleet.mission.capsule.v2",
            "mission_id": mission_id, "compiled_digest": compiled_digest,
            "generation": generation, "session": options.get("herdr_session"),
            "workspace": value.get("workspace")}
        if (anchor is not None or value != expected or options != creation.get("runtime_options")
                or creation.get("mission_id") != mission_id
                or options.get("herdr_launch_manifest") is not None
                or type(value.get("workspace")) is not dict or set(value["workspace"]) != {"closed"}
                or type(value["workspace"]["closed"]) is not bool):
            raise ValueError("capsule backend differs from creation-bound executor")
        return value
    if anchor != anchor_bytes(value):
        raise ValueError("Herdr frozen runtime contract changed, is missing, or was downgraded")
    return value
