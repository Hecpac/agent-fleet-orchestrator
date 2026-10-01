#!/usr/bin/env python3
"""Declarative model orchestration structure: slots, bindings, lanes and teams.

The manifest ``orchestration/fleet/model-orchestration-v1.json`` turns the
capability catalog (``docs/model-capability-catalog.md``) into a closed,
versioned structure. Its candidate order is a hypothesis for the project's own
evaluations: published benchmark results orient it but are never router
thresholds (C8), so the manifest carries catalog references, not scores.

This module validates the manifest and resolves a team for one lane offline.
It never launches a CLI, provider, Herdr session or agent and grants no
permission. ``model`` and ``effort`` are requested identities, not observed
evidence. Resolution is a static plan-time choice under each lane's mandatory
filters; nothing consumes it at runtime, so Herdr's rule against runtime
fallback is unaffected. Lane admission rules mirror the code that enforces
them; the tests anchor each constant to its source.

CLI::

    python3 -B scripts/fleet_model_orchestration.py validate PATH
    python3 -B scripts/fleet_model_orchestration.py resolve PATH --lane LANE --team TEAM

Each command prints one JSON object; exit 0 on success, 1 on any rejection.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import fleet_json

SCHEMA_VERSION = "fleet.model-orchestration.v1"
TOP_FIELDS = {"schema_version", "status", "activation", "catalog", "snapshot",
              "lanes", "bindings", "slots", "teams"}
BINDING_FIELDS = {"id", "family", "provider", "model", "cli", "effort", "status",
                  "context_tokens", "price_usd_per_mtok", "license", "lanes", "blockers"}
SLOT_FIELDS = {"id", "authority", "deliverable", "diversity", "candidates", "evidence"}
TEAM_FIELDS = {"id", "slots"}

LANES = ("herdr", "fusion", "harness_mini", "legacy_router")
CLIS = {"claude", "codex", "kimi", "mini-swe-agent", "ollama", "opencode"}
FAMILIES = {"anthropic", "deepseek", "local", "moonshot", "openai", "zai"}
PROVIDERS = {"anthropic", "deepseek", "moonshot", "ollama", "openai", "zai"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
STATUSES = {"current", "superseded"}
AUTHORITIES = {"write", "read-only", "advisory"}
DIVERSITY = {"none", "preferred", "required"}

# Herdr: the owner evidence adapter (fleet_herdr_owner_runtime.validate_binding)
# and the compiled launch (fleet_herdr.HerdrBackend._compiled_launch).
HERDR_MODELS = frozenset({"gpt-6-astra", "gpt-5.6-sol"})
HERDR_EFFORT = "high"
# Mini harness: the CONTROL budget request policy (fleet_harness_budget.contract).
MINI_PROVIDER, MINI_MODEL = "deepseek", "deepseek-flash"
# Fusion launches claude/codex with their native provider; no base URL override.
FUSION_NATIVE_PROVIDER = {"claude": "anthropic", "codex": "openai"}


def _text(value: Any, where: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{where}: expected a non-empty string")
    return value


def _enum(value: Any, allowed: set[str] | frozenset[str], where: str) -> str:
    if type(value) is not str or value not in allowed:
        raise ValueError(f"{where}: unsupported value {value!r}")
    return value


def _exact(value: Any, fields: set[str], where: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{where}: expected an object")
    if set(value) != fields:
        missing, extra = sorted(fields - set(value)), sorted(set(value) - fields)
        raise ValueError(f"{where}: fields differ (missing {missing}, unexpected {extra})")
    return value


def _positive_int(value: Any, where: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{where}: expected a positive integer")
    return value


def _price(value: Any, where: str) -> dict[str, float] | None:
    if value is None:
        return None
    _exact(value, {"input", "output"}, where)
    for key in ("input", "output"):
        if type(value[key]) not in (int, float) or value[key] < 0:
            raise ValueError(f"{where}.{key}: expected a non-negative number")
    return {"input": value["input"], "output": value["output"]}


def _lane_rule(lane: str, binding: dict[str, Any], clis: list[str]) -> str | None:
    """Return why an ``admitted`` mark contradicts the lane's enforced filter."""
    if binding["cli"] not in clis:
        return f"cli {binding['cli']} is outside lane {lane}"
    if lane == "herdr":
        if (binding["provider"] != "openai" or binding["model"] not in HERDR_MODELS
                or binding["effort"] != HERDR_EFFORT):
            return "Herdr admits only codex/openai with an adapter model at high effort"
    elif lane == "harness_mini":
        if binding["provider"] != MINI_PROVIDER or binding["model"] != MINI_MODEL:
            return "the Mini harness budget policy admits only deepseek-flash"
    elif lane == "fusion":
        if binding["provider"] != FUSION_NATIVE_PROVIDER[binding["cli"]]:
            return "Fusion runs each CLI only against its native provider"
    return None


def _binding(value: Any, where: str, lanes: dict[str, list[str]]) -> dict[str, Any]:
    _exact(value, BINDING_FIELDS, where)
    row = {
        "id": _text(value["id"], f"{where}.id"),
        "family": _enum(value["family"], FAMILIES, f"{where}.family"),
        "provider": _enum(value["provider"], PROVIDERS, f"{where}.provider"),
        "model": _text(value["model"], f"{where}.model"),
        "cli": _enum(value["cli"], CLIS, f"{where}.cli"),
        "effort": None if value["effort"] is None else _enum(value["effort"], EFFORTS, f"{where}.effort"),
        "status": _enum(value["status"], STATUSES, f"{where}.status"),
        "context_tokens": _positive_int(value["context_tokens"], f"{where}.context_tokens"),
        "price_usd_per_mtok": _price(value["price_usd_per_mtok"], f"{where}.price_usd_per_mtok"),
        "license": _text(value["license"], f"{where}.license"),
    }
    marks = _exact(value["lanes"], set(LANES), f"{where}.lanes")
    blockers = value["blockers"]
    if type(blockers) is not dict or not set(blockers) <= set(LANES):
        raise ValueError(f"{where}.blockers: expected an object keyed by lane")
    row["lanes"], row["blockers"] = {}, {}
    for lane in LANES:
        mark = _enum(marks[lane], {"admitted", "blocked"}, f"{where}.lanes.{lane}")
        reasons = blockers.get(lane)
        if mark == "admitted":
            if reasons is not None:
                raise ValueError(f"{where}: lane {lane} is admitted but lists blockers")
            reason = _lane_rule(lane, row, lanes[lane])
            if reason is not None:
                raise ValueError(f"{where}: lane {lane} admission contradicts its filter: {reason}")
        else:
            if type(reasons) is not list or not reasons:
                raise ValueError(f"{where}: lane {lane} is blocked without blockers")
            row["blockers"][lane] = [_text(r, f"{where}.blockers.{lane}") for r in reasons]
        row["lanes"][lane] = mark
    return row


def validate_manifest(value: Any) -> dict[str, Any]:
    """Return a validated copy of the manifest or raise ``ValueError``."""
    _exact(value, TOP_FIELDS, "manifest")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("manifest: unsupported schema_version")
    if value["status"] != "hypothesis" or value["activation"] != "none":
        raise ValueError("manifest: v1 is a hypothesis with no activation")
    result = {key: _text(value[key], f"manifest.{key}")
              for key in ("schema_version", "status", "activation", "catalog", "snapshot")}

    raw_lanes = _exact(value["lanes"], set(LANES), "manifest.lanes")
    lanes: dict[str, list[str]] = {}
    for lane in LANES:
        entry = _exact(raw_lanes[lane], {"clis"}, f"lanes.{lane}")
        clis = entry["clis"]
        if type(clis) is not list or not clis or len(set(clis)) != len(clis):
            raise ValueError(f"lanes.{lane}.clis: expected unique CLIs")
        lanes[lane] = [_enum(c, CLIS, f"lanes.{lane}.clis") for c in clis]
    result["lanes"] = {lane: {"clis": list(clis)} for lane, clis in lanes.items()}

    if type(value["bindings"]) is not list or not value["bindings"]:
        raise ValueError("manifest.bindings: expected a non-empty list")
    bindings: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value["bindings"]):
        row = _binding(raw, f"bindings[{index}]", lanes)
        if row["id"] in bindings:
            raise ValueError(f"duplicate binding id: {row['id']}")
        bindings[row["id"]] = row
    result["bindings"] = list(bindings.values())

    if type(value["slots"]) is not list or not value["slots"]:
        raise ValueError("manifest.slots: expected a non-empty list")
    slots: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value["slots"]):
        where = f"slots[{index}]"
        _exact(raw, SLOT_FIELDS, where)
        slot = {"id": _text(raw["id"], f"{where}.id"),
                "authority": _enum(raw["authority"], AUTHORITIES, f"{where}.authority"),
                "deliverable": _text(raw["deliverable"], f"{where}.deliverable"),
                "diversity": _enum(raw["diversity"], DIVERSITY, f"{where}.diversity")}
        if slot["id"] in slots:
            raise ValueError(f"duplicate slot id: {slot['id']}")
        if slot["authority"] == "write" and slot["diversity"] != "none":
            raise ValueError(f"{where}: a writer slot defines the diversity reference")
        candidates = raw["candidates"]
        if type(candidates) is not list or not candidates or len(set(candidates)) != len(candidates):
            raise ValueError(f"{where}.candidates: expected unique binding ids")
        for candidate in candidates:
            if candidate not in bindings:
                raise ValueError(f"{where}: unknown binding {candidate!r}")
        evidence = raw["evidence"]
        if type(evidence) is not list or not evidence:
            raise ValueError(f"{where}.evidence: expected catalog references")
        slot["candidates"] = list(candidates)
        slot["evidence"] = [_text(e, f"{where}.evidence") for e in evidence]
        slots[slot["id"]] = slot
    result["slots"] = list(slots.values())

    if type(value["teams"]) is not list or not value["teams"]:
        raise ValueError("manifest.teams: expected a non-empty list")
    teams: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value["teams"]):
        where = f"teams[{index}]"
        _exact(raw, TEAM_FIELDS, where)
        team_id = _text(raw["id"], f"{where}.id")
        if team_id in teams:
            raise ValueError(f"duplicate team id: {team_id}")
        members = raw["slots"]
        if type(members) is not list or not members or len(set(members)) != len(members):
            raise ValueError(f"{where}.slots: expected unique slot ids")
        for member in members:
            if member not in slots:
                raise ValueError(f"{where}: unknown slot {member!r}")
        writers = [m for m in members if slots[m]["authority"] == "write"]
        if len(writers) != 1:
            raise ValueError(f"{where}: a team needs exactly one writer slot")
        teams[team_id] = {"id": team_id, "slots": list(members)}
    result["teams"] = list(teams.values())
    return result


def load_manifest(path: str | Path) -> dict[str, Any]:
    location = Path(path)
    try:
        raw = location.read_bytes()
    except OSError as exc:
        raise ValueError(f"{location}: cannot read manifest: {exc}") from exc
    try:
        return validate_manifest(fleet_json.loads(raw))
    except ValueError as exc:
        raise ValueError(f"{location}: {exc}") from exc


def _choose(slot: dict[str, Any], lane: str, bindings: dict[str, dict[str, Any]],
            writer_family: str | None) -> dict[str, Any]:
    eligible = [bindings[c] for c in slot["candidates"] if bindings[c]["lanes"][lane] == "admitted"]
    chosen, warnings = None, []
    if slot["diversity"] == "none" or writer_family is None:
        chosen = eligible[0] if eligible else None
        if slot["diversity"] != "none" and chosen is not None:
            warnings.append("writer unresolved; family diversity not evaluated")
    else:
        distinct = [b for b in eligible if b["family"] != writer_family]
        if distinct:
            chosen = distinct[0]
        elif slot["diversity"] == "preferred" and eligible:
            chosen = eligible[0]
            warnings.append(f"no admitted candidate outside the writer family {writer_family}")
    # Explain every candidate ranked before the choice (all of them if none fits).
    rejected = []
    for candidate in slot["candidates"]:
        if chosen is not None and candidate == chosen["id"]:
            break
        binding = bindings[candidate]
        if binding["lanes"][lane] != "admitted":
            reasons = list(binding["blockers"][lane])
        else:
            reasons = [f"same family as the writer ({writer_family}); distinct family {slot['diversity']}"]
        rejected.append({"binding": candidate, "reasons": reasons})
    assignment = {"slot": slot["id"], "authority": slot["authority"], "binding": None,
                  "rejected": rejected, "warnings": warnings}
    if chosen is not None:
        assignment.update(binding=chosen["id"], family=chosen["family"], provider=chosen["provider"],
                          model=chosen["model"], cli=chosen["cli"], effort=chosen["effort"],
                          status=chosen["status"])
    return assignment


def resolve(manifest: dict[str, Any], *, lane: str, team: str) -> dict[str, Any]:
    """Statically assign each team slot to its first admissible candidate."""
    manifest = validate_manifest(manifest)
    if lane not in LANES:
        raise ValueError(f"unknown lane: {lane!r}")
    teams = {t["id"]: t for t in manifest["teams"]}
    if team not in teams:
        raise ValueError(f"unknown team: {team!r}")
    slots = {s["id"]: s for s in manifest["slots"]}
    bindings = {b["id"]: b for b in manifest["bindings"]}
    members = teams[team]["slots"]
    writer_slot = next(m for m in members if slots[m]["authority"] == "write")
    writer = _choose(slots[writer_slot], lane, bindings, None)
    writer_family = writer.get("family") if writer["binding"] else None
    assignments = []
    for member in members:
        assignments.append(writer if member == writer_slot
                           else _choose(slots[member], lane, bindings, writer_family))
    return {"schema_version": SCHEMA_VERSION, "status": manifest["status"], "activation": manifest["activation"],
            "lane": lane, "team": team, "complete": all(a["binding"] for a in assignments),
            "writer_family": writer_family, "assignments": assignments}


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    usage = ("usage: fleet_model_orchestration.py validate PATH | "
             "resolve PATH --lane LANE --team TEAM")
    try:
        if len(args) == 2 and args[0] == "validate":
            manifest = load_manifest(args[1])
            _emit({"valid": True, "schema_version": manifest["schema_version"],
                   "status": manifest["status"], "activation": manifest["activation"],
                   "bindings": len(manifest["bindings"]), "slots": [s["id"] for s in manifest["slots"]],
                   "teams": [t["id"] for t in manifest["teams"]], "lanes": list(manifest["lanes"])})
            return 0
        if len(args) == 6 and args[0] == "resolve" and args[2] == "--lane" and args[4] == "--team":
            _emit(resolve(load_manifest(args[1]), lane=args[3], team=args[5]))
            return 0
        raise ValueError(usage)
    except ValueError as exc:
        _emit({"valid": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
