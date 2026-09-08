"""Staged, unauthoritative binding contract for trusted runtime hooks.

This is a receiver, not a launcher or an authentication implementation. Pins,
the active attempt, and observations must come from CONTROL, never stdout.
Even a complete consistent chain remains NOT_VERIFIED. Version 1 stays separate.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any

import fleet_artifacts
import fleet_herdr_binding as legacy
import fleet_json
import fleet_mission_state as state

BindingError = legacy.BindingError
VERSION = 2
LAUNCH_FIELDS = legacy.BINDING_FIELDS - {"run_id"}
PHASES = ("launch", "pre_exec", "ack", "spawn", "effects")
COMMON = {"schema_version", "kind", "attempt", "challenge", "frozen_policy_sha256"}
FIELDS = {
    "launch": {"candidate", "codex", "invocation", "environment", "process"},
    "pre_exec": {"run_id", "launch_sha256", "sequence", "operation_id", "process",
                 "candidate", "effective_policy", "os_sandbox", "invocation",
                 "environment", "input_hmac_sha256", "fd_policy_sha256"},
    "ack": {"pre_exec_sha256", "operation_id", "sequence"},
    "spawn": {"ack_sha256", "pre_exec_sha256", "operation_id", "process"},
    "effects": {"spawn_sha256", "pre_exec_sha256", "operation_id", "before_sha256",
                "after_sha256", "protected_before", "protected_after", "checks",
                "quiescence_sha256"},
}
CHECKS = {"candidate_write", "own_temporary_write", "symlink_denied", "absolute_denied",
          "traversal_denied", "network_bind_denied", "stdout_non_authoritative",
          "descendants_quiescent", "git_preserved", "dirty_worktree_preserved"}
FROZEN_FIELDS = COMMON - {"frozen_policy_sha256"} | {
    "policy_schema_version", "requested_policy", "effective_constraint", "candidate", "codex",
    "temporary_roots", "protected_roots", "router_sha256", "workflow_sha256"}


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _check(condition: bool, reason: str) -> None:
    if not condition:
        raise BindingError(reason)


def _equal(actual: Any, expected: Any, label: str) -> None:
    _check(fleet_json.canonical_bytes(actual) == fleet_json.canonical_bytes(expected),
           f"{label} mismatch")


def _digest(value: Any) -> None:
    _check(isinstance(value, str) and state.SHA256.fullmatch(value) is not None, "invalid digest")


def _uuid(value: Any, label: str) -> None:
    _check(isinstance(value, str) and state.normalize_uuid(value, label) == value,
           f"invalid {label}")


def _pinned(raw: bytes, pin: str) -> dict[str, Any]:
    _digest(pin)
    _check(type(raw) is bytes and len(raw) <= 1024 * 1024, "invalid record size/type")
    _check(sha(raw) == pin, "record digest mismatch")
    result = fleet_json.loads(raw)
    _check(isinstance(result, dict), "record must be an object")
    _check(fleet_json.canonical_bytes(result) == raw, "record is not canonical JSON")
    return result


def _identity(identity: dict[str, Any], *, directory: bool) -> None:
    _equal(legacy.path_identity(identity["realpath"], directory=directory), identity, "physical identity")


def _commitments(record: dict[str, Any]) -> None:
    env, invocation = record["environment"], record["invocation"]
    _check(set(env) == {"names", "sanitized_sha256", "values_hmac_sha256"}, "environment fields")
    names = env["names"]
    _check(isinstance(names, list) and all(isinstance(n, str) for n in names)
           and names == sorted(set(names)), "environment names")
    _equal(env["sanitized_sha256"], sha(fleet_json.canonical_bytes({n: "<redacted>" for n in names})),
           "sanitized environment")
    _digest(env["values_hmac_sha256"])
    _check(set(invocation) == {"redacted", "sanitized_sha256", "values_hmac_sha256"}, "invocation fields")
    redacted = invocation["redacted"]
    _check(set(redacted) == {"argv", "config"} and isinstance(redacted["argv"], list)
           and bool(redacted["argv"]) and all(isinstance(x, str) and x in legacy.SAFE_FLAGS | {"<redacted>"}
               for x in redacted["argv"]), "unredacted argv")
    _check(isinstance(redacted["config"], dict) and all(isinstance(k, str)
           and (k in legacy.SAFE_CONFIG_KEYS or re.fullmatch(r"redacted_key_[0-9]+", k))
           and v == "<redacted>" for k, v in redacted["config"].items()), "unredacted config")
    _equal(invocation["sanitized_sha256"], sha(fleet_json.canonical_bytes(redacted)), "sanitized invocation")
    _digest(invocation["values_hmac_sha256"])


def freeze_attempt(*, attempt: dict[str, str], challenge: str, candidate: str,
                   codex: dict[str, Any], temporary_roots: dict[str, str | None],
                   protected_roots: dict[str, str], router_sha256: str,
                   workflow_sha256: str) -> bytes:
    """Construct requested policy ONCE, before launch; caller must pin durably.

    run_id intentionally does not exist at boot. Temporary roots are explicit
    CONTROL inputs, not an observation that Codex granted access to those roots.
    """
    requested = legacy.requested_policy(candidate)
    frozen = {"schema_version": VERSION, "kind": "frozen-attempt-policy", "attempt": attempt,
              "challenge": challenge, "policy_schema_version": 1, "requested_policy": requested,
              "candidate": legacy.path_identity(candidate, directory=True), "codex": codex,
              "temporary_roots": temporary_roots, "protected_roots": protected_roots,
              "router_sha256": router_sha256, "workflow_sha256": workflow_sha256,
              "effective_constraint": {
                  "sandbox_policy": {**requested["policy"]["sandbox_policy"], "writable_roots": []},
                  "approval_policy": "never", "product_writable_roots": [candidate],
                  "temporary_roots": temporary_roots}}
    raw = fleet_json.canonical_bytes(frozen)
    validate_frozen(raw, pinned_sha256=sha(raw), active_attempt=attempt)
    return raw


def validate_frozen(raw: bytes, *, pinned_sha256: str,
                    active_attempt: dict[str, str]) -> dict[str, Any]:
    """Validate the pinned historical bytes WITHOUT invoking current permissions.

    Physical identities are checked online. This cannot prevent path replacement
    after the check; the future launcher must independently bind the actual exec.
    """
    try:
        frozen = _pinned(raw, pinned_sha256)
        _check(set(frozen) == FROZEN_FIELDS, "frozen policy fields mismatch")
        _equal(frozen["schema_version"], VERSION, "schema")
        _equal(frozen["kind"], "frozen-attempt-policy", "kind")
        _equal(frozen["policy_schema_version"], 1, "policy schema")
        _check(set(active_attempt) == LAUNCH_FIELDS, "attempt fields mismatch")
        _equal(active_attempt["role"], "worker", "role")
        for field in LAUNCH_FIELDS - {"role"}:
            _uuid(active_attempt[field], field)
        _equal(frozen["attempt"], active_attempt, "active attempt")
        for field in ("challenge", "router_sha256", "workflow_sha256"):
            _digest(frozen[field])
        _identity(frozen["candidate"], directory=True)
        codex = frozen["codex"]
        _check(set(codex) == {"image", "version"}, "Codex fields mismatch")
        _check(isinstance(codex["version"], str) and bool(codex["version"]), "Codex version missing")
        _identity(codex["image"], directory=False)
        temps, protected = frozen["temporary_roots"], frozen["protected_roots"]
        _check(set(temps) == {"slash_tmp", "tmpdir"}, "temporary roots mismatch")
        _check(set(protected) == {"control", "cas", "ledger", "runs"}, "protected roots mismatch")
        writable = [Path(frozen["candidate"]["realpath"])]
        for root in temps.values():
            if root is not None:
                legacy.path_identity(root, directory=True)
                writable.append(Path(root))
        for root in protected.values():
            legacy.path_identity(root, directory=True)
            _check(not any(Path(root).is_relative_to(w) or w.is_relative_to(Path(root))
                           for w in writable), "protected/writable roots overlap")
        constraint = frozen["effective_constraint"]
        _equal(constraint, {"sandbox_policy": {"type": "workspace-write", "network_access": False,
            "exclude_tmpdir_env_var": False, "exclude_slash_tmp": False, "writable_roots": []},
            "approval_policy": "never", "product_writable_roots": [frozen["candidate"]["realpath"]],
            "temporary_roots": temps}, "version-1 frozen constraint")
        requested = frozen["requested_policy"]
        _check(set(requested) == {"policy", "launch_flags"}, "requested policy fields")
        policy = requested["policy"]
        _check(set(policy) == {"version", "role", "cwd", "model", "effort", "approval_policy", "sandbox_policy"},
               "requested policy schema")
        for field, value in {"version": 1, "role": "worker", "cwd": frozen["candidate"]["realpath"],
                             "approval_policy": "never"}.items():
            _equal(policy[field], value, f"requested {field}")
        _equal(policy["sandbox_policy"], {k: v for k, v in constraint["sandbox_policy"].items()
                                       if k != "writable_roots"}, "requested sandbox")
        _check(all(isinstance(policy[k], str) and bool(policy[k]) for k in ("model", "effort"))
               and isinstance(requested["launch_flags"], list) and bool(requested["launch_flags"])
               and all(isinstance(arg, str) for arg in requested["launch_flags"]), "requested launch values")
    except (KeyError, TypeError, fleet_json.FleetJSONError, state.MissionStateError) as exc:
        raise BindingError("malformed frozen policy") from exc
    return frozen


def validate_chain(frozen_raw: bytes, records: dict[str, bytes], *, pins: dict[str, str],
                   active_attempt: dict[str, str], active_run_id: str,
                   observations: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Check A/B/ACK/spawn/C against independently held observations and pins.

    ACK is evidence received here, NEVER an authorization issued by this module.
    One chain describes one operation. Replay exclusion needs a live trusted
    controller journal; this offline function deliberately grants no authority.
    """
    try:
        _check(set(records) == set(PHASES) and set(pins) == {"frozen", *PHASES}, "incomplete chain")
        _check(set(observations) == set(PHASES), "independent observations incomplete")
        frozen = validate_frozen(frozen_raw, pinned_sha256=pins["frozen"], active_attempt=active_attempt)
        _uuid(active_run_id, "run_id")
        parsed = {}
        for phase in PHASES:
            record = _pinned(records[phase], pins[phase])
            _check(set(record) == COMMON | FIELDS[phase], f"{phase} fields mismatch")
            _check(set(observations[phase]) == FIELDS[phase], f"{phase} observations incomplete")
            for field, value in {"schema_version": VERSION, "kind": phase, "attempt": active_attempt,
                                 "challenge": frozen["challenge"], "frozen_policy_sha256": pins["frozen"]}.items():
                _equal(record[field], value, f"{phase}.{field}")
            for field in FIELDS[phase]:
                _equal(record[field], observations[phase][field], f"{phase}.{field}")
            parsed[phase] = record
        launch, pre, ack, spawn, effects = (parsed[p] for p in PHASES)
        for record in (launch, pre):
            _equal(record["candidate"], frozen["candidate"], "candidate")
            _commitments(record)
        _equal(launch["codex"], frozen["codex"], "Codex image/version")
        _equal(pre["process"], launch["process"], "Codex process")
        _equal(pre["run_id"], active_run_id, "active run")
        _equal(pre["effective_policy"], frozen["effective_constraint"], "effective policy")
        _check(type(pre["sequence"]) is int and pre["sequence"] > 0, "invalid sequence")
        _uuid(pre["operation_id"], "operation_id")
        _equal(ack["sequence"], pre["sequence"], "ACK sequence")
        for phase in ("ack", "spawn", "effects"):
            _equal(parsed[phase]["operation_id"], pre["operation_id"], "operation id")
        for phase, field, target in (("pre_exec", "launch_sha256", "launch"),
            ("ack", "pre_exec_sha256", "pre_exec"), ("spawn", "pre_exec_sha256", "pre_exec"),
            ("spawn", "ack_sha256", "ack"), ("effects", "spawn_sha256", "spawn"),
            ("effects", "pre_exec_sha256", "pre_exec")):
            _equal(parsed[phase][field], pins[target], f"{phase} predecessor")
        for record in (launch, spawn):
            process = record["process"]
            _check(set(process) == {"pid", "birth_identity"} and type(process["pid"]) is int
                   and process["pid"] > 0 and isinstance(process["birth_identity"], str)
                   and bool(process["birth_identity"]), "invalid process identity")
        _check(set(pre["os_sandbox"]) == {"kind", "profile_sha256", "parameters_sha256"},
               "incomplete OS sandbox")
        _equal(pre["os_sandbox"]["kind"], "seatbelt", "OS sandbox")
        for field in ("profile_sha256", "parameters_sha256"):
            _digest(pre["os_sandbox"][field])
        for field in ("input_hmac_sha256", "fd_policy_sha256"):
            _digest(pre[field])
        for field in ("before_sha256", "after_sha256", "quiescence_sha256"):
            _digest(effects[field])
        _check(set(effects["protected_before"]) == set(frozen["protected_roots"]), "protected snapshot incomplete")
        for digest in effects["protected_before"].values():
            _digest(digest)
        _equal(effects["protected_after"], effects["protected_before"], "protected roots changed")
        _equal(effects["checks"], {name: True for name in CHECKS}, "canary checks")
    except (KeyError, TypeError, fleet_json.FleetJSONError, state.MissionStateError) as exc:
        raise BindingError("malformed binding chain") from exc
    return {"schema_version": VERSION, "record_consistent": True, "authority": "none",
            "INTEGRATION_BINDING": "NOT_VERIFIED", "reason": "authenticated_runtime_channel_unavailable",
            "artifacts": dict(pins)}


def quarantine_chain(runs_dir: Path, frozen_raw: bytes, records: dict[str, bytes], *,
                     ledger_event_sha256: str, **validation: Any) -> dict[str, Any]:
    """Persist phase records and a one-way ledger reference, never acceptance.

    These files are tamper-evident CAS objects, not proof of Worker non-writability
    or authenticated origin. No ACK is sent and no operation can be released.
    """
    result = validate_chain(frozen_raw, records, **validation)
    frozen = fleet_json.loads(frozen_raw)
    mid = frozen["attempt"]["mission_id"]
    roots = frozen["protected_roots"]
    _equal(str(runs_dir), roots["runs"], "runs root")
    _equal(str(fleet_artifacts.store_path(runs_dir, mid)), roots["cas"], "CAS root")
    _equal(str(state.mission_root(runs_dir, mid)), roots["ledger"], "ledger root")
    _digest(ledger_event_sha256)
    events = state.read_events(state.ledger_path(runs_dir, mid), expected_mission_id=mid)
    _check(any(e["event_sha256"] == ledger_event_sha256 for e in events), "ledger predecessor missing")
    effects = fleet_json.loads(records["effects"])
    for field in ("before_sha256", "after_sha256", "quiescence_sha256"):
        fleet_artifacts.get_bytes(runs_dir, mid, effects[field])
    for raw in (frozen_raw, *records.values()):
        fleet_artifacts.put_bytes(runs_dir, mid, raw)
    envelope = {**result, "kind": "controller-binding-quarantine", "attempt": frozen["attempt"],
                "ledger_predecessor_sha256": ledger_event_sha256}
    stored = fleet_artifacts.put_bytes(runs_dir, mid, fleet_json.canonical_bytes(envelope))
    return {**envelope, "artifact_id": stored["artifact_id"]}
