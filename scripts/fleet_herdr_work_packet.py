"""Owner work projection, staged locally while the owner cycle is disabled.

The public packet contains work, not Mission bookkeeping. Source contracts and
their pins remain in a controller envelope. This module neither admits a run nor
changes a Mission, provisions tools, executes checks or accepts a candidate.
"""
from __future__ import annotations

import copy
import inspect
from pathlib import Path

import fleet_acceptance
import fleet_artifacts
import fleet_functional
import fleet_functional_runner as runner
import fleet_herdr_instructions as instructions
import fleet_herdr_permissions as permissions
import fleet_herdr_profile as profiles
import fleet_herdr_role_guidance as guidance
import fleet_herdr_scope as scope
import fleet_herdr_versions as versions
import fleet_json
import fleet_mission_state as state


VERSION = "owner-work-v1"
RESULT_VERSION = "owner-result-v1"
ENVELOPE_VERSION = "owner-execution-envelope-v1"
MAX_PACKET_BYTES = 256 * 1024
MAX_CONTEXT_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 32 * 1024


class WorkPacketError(ValueError):
    pass


def digest(value):
    return state.artifact_id(fleet_json.canonical_bytes(value))


def text(value, name, *, empty=False, maximum=8192):
    try:
        size = len(value.encode("utf-8")) if isinstance(value, str) else maximum + 1
    except UnicodeError as exc:
        raise WorkPacketError("invalid UTF-8 text: " + name) from exc
    if (not isinstance(value, str) or (not empty and not value.strip())
            or size > maximum or "\x00" in value):
        raise WorkPacketError("invalid bounded text: " + name)
    return value


def paths(value, name, *, maximum=100):
    if not isinstance(value, list) or len(value) > maximum:
        raise WorkPacketError("invalid path list: " + name)
    for item in value:
        scope.path(item)
    if len(set(value)) != len(value):
        raise WorkPacketError("duplicate path: " + name)
    return value


def context(value=None):
    """Initial authorized context only. This is not an amendment mechanism."""
    defaults = {"summary": "", "entry_points": [], "evidence": [],
                "additional_requirements": [], "procedure_preferences": [],
                "constraints": [], "decisions": []}
    value = {} if value is None else value
    if (not isinstance(value, dict) or set(value) - set(defaults)
            or len(fleet_json.canonical_bytes(value)) > MAX_CONTEXT_BYTES):
        raise WorkPacketError("unsupported or oversized work context")
    value = copy.deepcopy({**defaults, **value})
    text(value["summary"], "context.summary", empty=True)
    paths(value["entry_points"], "context.entry_points")
    for field in ("additional_requirements", "procedure_preferences", "constraints"):
        if not isinstance(value[field], list) or len(value[field]) > 100:
            raise WorkPacketError("invalid context list: " + field)
        for item in value[field]:
            text(item, field)
    for field, fields in (("evidence", {"name", "content"}), ("decisions", {"decision", "reason"})):
        if not isinstance(value[field], list) or len(value[field]) > 100:
            raise WorkPacketError("invalid context list: " + field)
        for item in value[field]:
            if not isinstance(item, dict) or set(item) != fields:
                raise WorkPacketError("invalid context entry: " + field)
            for key, content in item.items():
                text(content, field + "." + key)
    names = [e["name"] for e in value["evidence"]]
    if len(set(names)) != len(names):
        raise WorkPacketError("duplicate evidence name")
    return value


def load_context(filename):
    return context(fleet_json.loads(fleet_artifacts.read_regular(
        Path(filename), max_bytes=MAX_CONTEXT_BYTES)))


def _functional(spec, tests, source_validator):
    if spec is None:
        if tests is not None or source_validator is not None:
            raise WorkPacketError("functional content without a contract")
        return {"configured": False, "coverage": "No controller functional check configured."}
    fleet_functional.validate(spec)
    if (not isinstance(tests, str) or state.artifact_id(tests) != spec["tests"]["sha256"]
            or not isinstance(source_validator, str) or not source_validator.strip()):
        raise WorkPacketError("functional projection lacks exact tests or source policy")
    # This mapping is deliberately limited to the byte-pinned canonical suite.
    # The full oracle and actual source validator accompany the readable summary.
    return {"configured": True, "check": spec["check_id"],
        "requirements": [
            {"requirement": "sample_stats.stats returns count, sum and mean for numeric input.",
             "tests": ["StatsTests.test_regular", "StatsTests.test_negative_and_fractional"]},
            {"requirement": "Empty input raises ValueError.", "tests": ["StatsTests.test_empty"]},
            {"requirement": "Invalid elements, including bool, raise TypeError.", "tests": ["StatsTests.test_invalid"]},
            {"requirement": "Do not mutate the input list.", "tests": ["StatsTests.test_input_not_mutated"]}],
        "oracle": {"content": tests, "editable": False, "execution_owner": "controller",
                   "coverage": "Five canonical tests and source admission only; not documentation quality or general Python correctness."},
        "source_policy": {"name": spec["source_policy"], "validator_python": source_validator,
            "summary": "sample_stats.py must use the restricted stats function subset below. Passing functional values cannot bypass source admission."},
        "execution": {"runner": spec["profile"], "argv": spec["argv"], "cwd": spec["cwd"],
            "environment": spec["environment"], "limits": spec["limits"],
            "runtime": {k: spec["runtime"][k] for k in
                        ("python_version", "architecture", "os", "dependencies")},
            "availability": "not_observed", "candidate_mount": "read-only",
            "note": "Configured controller check; this packet does not prove Docker availability or authorize agent Docker execution. Local checks are supplementary evidence."}}


def _instructions(snapshot):
    if (not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1
            or snapshot.get("discovery") != "tracked-candidate-baseline-v1"
            or not isinstance(snapshot.get("entries"), list)):
        raise WorkPacketError("unsupported instruction snapshot")
    entries = []
    total = 0
    for entry in snapshot["entries"]:
        if (not isinstance(entry, dict) or set(entry) != {"path", "scope", "sha256", "content"}
                or state.artifact_id(entry["content"]) != entry["sha256"]
                or str(Path(entry["path"]).parent) != entry["scope"]):
            raise WorkPacketError("instruction content/scope binding mismatch")
        scope.path(entry["path"])
        text(entry["content"], "instruction content", empty=True, maximum=instructions.MAX_FILE_BYTES)
        total += len(entry["content"].encode())
        entries.append({k: entry[k] for k in ("path", "scope", "content")})
    if (len(entries) > instructions.MAX_FILES or total > instructions.MAX_TOTAL_BYTES
            or len({e["path"] for e in entries}) != len(entries)):
        raise WorkPacketError("instruction snapshot exceeds bounds or repeats a path")
    return {"application": snapshot["application"], "entries": entries}


def _response_contract():
    return {"version": RESULT_VERSION,
        "format": "Exactly one raw JSON object. No Markdown fences or surrounding text. Put citations in text fields. Do not supply identities, hashes, PASS, or terminal status.",
        "submit_candidate": {"type": "submit_candidate", "summary": "What changed and what remains unverified",
            "paths": ["candidate-relative changed path (deletions allowed)"],
            "checks": [{"name": "check performed", "outcome": "passed|failed|not_run", "detail": "observed evidence or reason not run"}]},
        "request_decision": {"type": "request_decision", "question": "Concrete material decision",
            "why_needed": "Why existing requirements/evidence cannot resolve it",
            "options": [{"label": "choice", "consequence": "tradeoff"}],
            "recommendation": None, "work_completed": "Useful work preserved so far"},
        "bounds": {"max_bytes": MAX_RESPONSE_BYTES, "max_paths": 100, "max_checks": 50,
                   "max_options": 3, "max_text_bytes": 8192},
        "semantics": ["Candidate paths are suggestions, not authority or a complete inventory. Fleet computes file hashes and acceptance from its frozen candidate.",
            "Checks reported by the agent are claims, not controller verification results.",
            "A decision request needs no file, artifact, invented alternative or recommendation; options may be empty and recommendation null.",
            "Requesting a decision grants no permission. Submission does not mean accepted, applied, committed, pushed or deployed."]}


def _project(sources):
    brief = context(sources["context"])
    contract = scope.validate(sources["scope"])
    acceptance = fleet_acceptance.validate(sources["acceptance"])
    bundle = guidance.validate_bundle(sources["guidance"])
    snapshot = sources["instructions"]
    expected = sources["permissions"]
    if expected != permissions.policy("worker", expected.get("cwd"), version=permissions.MINIMAL_VERSION):
        raise WorkPacketError("owner packet requires the existing minimal permission configuration")
    functional = _functional(sources["functional"], sources["functional_tests"], sources["source_validator"])
    timeout = sources["timeout_seconds"]
    if type(timeout) is not int or not 60 <= timeout <= 3600:
        raise WorkPacketError("owner preview timeout must be 60..3600 seconds")
    text(sources["objective"], "objective", maximum=64 * 1024)
    packet = {"contract_version": VERSION, "objective": sources["objective"],
        "workspace": {"root": ".", "meaning": "Controller-owned candidate; never the source checkout."},
        "context": {k: brief[k] for k in ("summary", "entry_points", "evidence")},
        "requirements": {"additional": brief["additional_requirements"],
            "artifact": acceptance["requirements"], "functional": functional,
            "coverage": "Artifact checks test only their predicates. Objective and additional requirements still need relevant evidence; no functional or semantic coverage is inferred."},
        "scope": {"editable_paths": contract["editable_paths"],
            "temporary_directories": contract["temporary_directories"], "preserve_other_paths": True,
            "inventory_limits": {k: contract[k] for k in ("max_entries", "max_bytes")},
            "rules": ["Editable paths are exact relative files, not globs. Required new parent directories are allowed.",
                "Declared temporary directories must be new; their contents are inventoried and excluded from delivery.",
                "Preserve preexisting files outside editable_paths, including ignored files. Undeclared new residue prevents acceptance.",
                "An incomplete capture cannot pass. Only root .git is excluded; symlinks, hardlinks and special files are unsupported. Entries must belong to the controller user, without setuid/setgid/sticky bits, with directory depth at most 64.",
                "Scope is a snapshot acceptance check, not interception of shell effects or transient/outside writes."]},
        "procedure_preferences": brief["procedure_preferences"], "decisions": brief["decisions"],
        "constraints": brief["constraints"],
        "capabilities": {"native_tools": {"availability": "not_observed",
                "note": "Use only tools actually exposed by the executing CLI. This packet does not provision tools or dependencies."},
            "task_authority": {"read_candidate": True, "write": "Only editable_paths and declared new temporary directories.",
                "local_shell": "Only within the bound permissions, task constraints and prohibitions."},
            "permissions": {"configuration": {k: expected[k] for k in ("sandbox_policy", "approval_policy")},
                "attestation": "pending_recorded_turn_configuration", "effect_mediation": "not_provided",
                "note": "Configured workspace-write includes runtime temporary access; task scope is narrower. Configuration is not universal effect interception."},
            "delegation": False, "mcp_required": [],
            "controller_functional_check": functional["configured"]},
        "limits": {"deadline_seconds": timeout, "deadline_owner": "controller",
            "token_budget_enforced": False, "tokens_available": None,
            "repair_enabled": False, "amendments_enabled": False, "owner_cycle_enabled": False},
        "instructions": {"project": _instructions(snapshot),
            "responsibility": ["Deliver the objective as the sole candidate writer within the supplied scope.",
                "Choose planning, research, implementation and local verification methods as useful; no additional roles or turns are implied.",
                "Resolve reversible choices within scope; ask only for material decisions unresolved by existing evidence. Preserve useful independent work.",
                "Fleet owns session identity, evidence binding, physical inventory, checks and acceptance. Do not infer runtime identity from labels or copy bookkeeping into your result.",
                "Skills below apply only under their stated conditions and do not provision capabilities. The supplied owner result protocol governs their output."],
            "authority": bundle["common"]["authority"],
            "skills": [{"name": name, "when": when, "content": bundle["skills"][name]["content"]}
                       for name, when in sorted(bundle["roles"]["worker"]["skills"].items())]},
        "result_protocol": _response_contract()}
    if len(fleet_json.canonical_bytes(packet)) > MAX_PACKET_BYTES:
        raise WorkPacketError("work packet exceeds size limit; refusing incomplete projection")
    return packet


def prepare(*, objective, candidate_repo, base_sha, compiled, acceptance_contract,
            scope_contract, functional_contract=None, work_context=None, timeout_seconds=None):
    """Read-only preparation for preview/fixtures and later controller integration.

    No Mission/session/run is fabricated here. Those are injected by the actual
    controller when binding a response, never supplied by the model.
    """
    if profiles.resolve_profile(compiled) is not profiles.MINIMAL:
        raise WorkPacketError("owner work projection requires sol_minimal_v1")
    candidate_repo = Path(candidate_repo).resolve(strict=True)
    if not isinstance(base_sha, str) or not state.GIT_OID.fullmatch(base_sha):
        raise WorkPacketError("invalid baseline identity")
    workflow_limits = compiled["workflow"]["limits"]
    if workflow_limits["token_budget"] > 0:
        raise WorkPacketError("owner protocol cannot enforce a token budget")
    timeout = min(workflow_limits["deadline_seconds"], timeout_seconds if timeout_seconds is not None else workflow_limits["deadline_seconds"])
    tests, validator = None, None
    if functional_contract is not None:
        fleet_functional.validate(functional_contract)
        # Never summarize a different oracle or implementation of source admission.
        if (functional_contract["runtime"]["controller_sha256"] != runner.controller_sha()
                or functional_contract["runtime"]["guest_sha256"] != state.artifact_id(runner.GUEST.read_bytes())):
            raise WorkPacketError("functional projection runtime source differs from contract")
        tests_path = Path(functional_contract["tests"]["path"])
        if tests_path.resolve().is_relative_to(candidate_repo):
            raise WorkPacketError("functional oracle must be external to candidate")
        tests = fleet_artifacts.read_regular(tests_path, max_bytes=64 * 1024).decode("utf-8")
        validator = inspect.getsource(runner.check_bridge_source)
    sources = copy.deepcopy({"objective": objective, "context": context(work_context),
        "acceptance": acceptance_contract, "scope": scope_contract,
        "functional": functional_contract, "functional_tests": tests, "source_validator": validator,
        "instructions": instructions.snapshot(candidate_repo, base_sha),
        "guidance": guidance.validate_bundle(guidance.build_bundle()),
        "permissions": permissions.policy("worker", str(candidate_repo), version=permissions.MINIMAL_VERSION),
        "timeout_seconds": timeout, "compiled_digest": compiled["compiled_digest"]})
    packet = _project(sources)
    envelope = {"contract_version": ENVELOPE_VERSION, "result_protocol": RESULT_VERSION,
        "work_packet_sha256": digest(packet), "source_pins": {k: digest(v) for k, v in sources.items()},
        "candidate_repo": str(candidate_repo), "base_sha": base_sha,
        "compiled_digest": compiled["compiled_digest"], "profile": profiles.MINIMAL.profile_id,
        "runtime_contract": versions.TASK_CONTEXT_CONTRACT, "dispatch_enabled": False}
    return {"work_packet": packet, "execution_envelope": copy.deepcopy(envelope), "sources": sources}


def verify(prepared):
    """Check retained preparation without rereading mutable files or upgrading it."""
    try:
        if not isinstance(prepared, dict) or set(prepared) != {"work_packet", "execution_envelope", "sources"}:
            raise WorkPacketError("invalid prepared work packet")
        sources, envelope = prepared["sources"], prepared["execution_envelope"]
        if not isinstance(sources, dict) or set(sources) != {
                "objective", "context", "acceptance", "scope", "functional", "functional_tests",
                "source_validator", "instructions", "guidance", "permissions", "timeout_seconds", "compiled_digest"}:
            raise WorkPacketError("incomplete work packet sources")
        if (not isinstance(sources["compiled_digest"], str) or not state.SHA256.fullmatch(sources["compiled_digest"])
                or not isinstance(sources["instructions"]["base_sha"], str)
                or not state.GIT_OID.fullmatch(sources["instructions"]["base_sha"])):
            raise WorkPacketError("invalid work packet source identity")
        expected = {"contract_version": ENVELOPE_VERSION, "result_protocol": RESULT_VERSION,
            "work_packet_sha256": digest(prepared["work_packet"]),
            "source_pins": {k: digest(v) for k, v in sources.items()},
            "candidate_repo": sources["permissions"]["cwd"], "base_sha": sources["instructions"]["base_sha"],
            "compiled_digest": sources["compiled_digest"], "profile": profiles.MINIMAL.profile_id,
            "runtime_contract": versions.TASK_CONTEXT_CONTRACT, "dispatch_enabled": False}
        if envelope != expected or prepared["work_packet"] != _project(sources):
            raise WorkPacketError("work packet/source/envelope binding mismatch")
        return prepared
    except (KeyError, TypeError, AttributeError) as exc:
        raise WorkPacketError("incomplete work packet preparation") from exc
