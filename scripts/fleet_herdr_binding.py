"""Offline receiver contract for a future trusted Codex pre-exec hook.

This module does NOT capture effective policy, launch Codex, or grant acceptance.
Even a consistent record remains NOT_VERIFIED: the installed Herdr/Codex path
does not supply the authenticated pre-exec/observer channel this contract needs.
Never build `expected` or the pinned digest from the submitted record itself.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

import fleet_artifacts
import fleet_herdr_permissions as permissions
import fleet_json
import fleet_mission_state as state


VERSION = 1
KIND = "effective-policy-attestation"
BINDING_FIELDS = {"mission_id", "run_id", "generation", "role", "attempt_id"}
OBSERVED_FIELDS = {"binding", "challenge", "candidate", "codex", "invocation",
                   "environment", "process", "operation_sha256", "observer_artifact_id",
                   "ledger_event_sha256", "os_sandbox"}
RECORD_FIELDS = OBSERVED_FIELDS | {"schema_version", "kind", "requested_policy_sha256",
                                   "effective_policy"}
SAFE_FLAGS = {"--model", "-m", "--sandbox", "-s", "--ask-for-approval", "-a",
              "--config", "-c", "--cd", "-C"}
SAFE_CONFIG_KEYS = {"sandbox_mode", "sandbox_workspace_write", "approval_policy",
                    "model_reasoning_effort", "projects"}


class BindingError(ValueError):
    """Inconsistent or unsafe candidate evidence; never a verified binding."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise BindingError(reason)


def _sha(value: Any) -> str:
    return hashlib.sha256(fleet_json.canonical_bytes(value)).hexdigest()


def _digest(value: Any, label: str) -> None:
    _require(isinstance(value, str) and state.SHA256.fullmatch(value) is not None,
             f"invalid {label}")


def _same(left: Any, right: Any, label: str) -> None:
    # JSON comparison distinguishes false from 0 and true from 1.
    _require(fleet_json.canonical_bytes(left) == fleet_json.canonical_bytes(right),
             f"{label} mismatch")


def _commit(value: Any, key: bytes, domain: bytes) -> str:
    _require(isinstance(key, bytes) and len(key) >= 32, "controller commitment key too short")
    return hmac.new(key, domain + fleet_json.canonical_bytes(value), hashlib.sha256).hexdigest()


def environment_commitment(environment: Mapping[str, str], key: bytes) -> dict[str, Any]:
    """Commit all values without persisting them or a guessable unkeyed hash.

    The future controller holds `key`; do not pass it to the Worker environment
    or store it beside a record. A sanitized digest alone cannot detect changes
    to redacted values. A matching HMAC alone does not authenticate the emitter.
    """
    values = dict(environment)
    _require(all(isinstance(k, str) and isinstance(v, str) for k, v in values.items()),
             "environment must contain string names and values")
    redacted = {name: "<redacted>" for name in sorted(values)}
    return {"names": sorted(values), "sanitized_sha256": _sha(redacted),
            "values_hmac_sha256": _commit(values, key, b"fleet-environment-v1\0")}


def invocation_commitment(argv: list[str], config: dict[str, Any], key: bytes) -> dict[str, Any]:
    """Conservative redaction; commit the complete resolved inputs separately."""
    _require(isinstance(argv, list) and bool(argv) and all(isinstance(x, str) for x in argv),
             "invalid argv")
    _require(isinstance(config, dict) and all(isinstance(k, str) for k in config), "invalid config")
    redacted = {"argv": [x if x in SAFE_FLAGS else "<redacted>" for x in argv],
                "config": {(k if k in SAFE_CONFIG_KEYS else f"redacted_key_{i}"): "<redacted>"
                           for i, k in enumerate(sorted(config))}}
    return {"redacted": redacted, "sanitized_sha256": _sha(redacted),
            "values_hmac_sha256": _commit({"argv": argv, "config": config}, key,
                                          b"fleet-invocation-v1\0")}


def path_identity(value: str, *, directory: bool) -> dict[str, Any]:
    """Online identity check, not proof that another process used this inode.

    Require the caller to supply a canonical realpath; silently resolving aliases
    would let an attestation hide a different launch path. Binary files are read,
    never executed, and both inode and bytes are pinned.
    """
    _require(isinstance(value, str), "path must be a string")
    path = Path(value)
    _require(path.is_absolute() and str(path) == value and not value.startswith("//"),
             "path is not canonical")
    try:
        _require(str(path.resolve(strict=True)) == value, "symlink or path alias")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if directory:
            flags |= os.O_DIRECTORY
        fd = os.open(value, flags)
        try:
            before = os.fstat(fd)
            _require(stat.S_ISDIR(before.st_mode) if directory else stat.S_ISREG(before.st_mode),
                     "path type mismatch")
            result = {"realpath": value, "device": before.st_dev, "inode": before.st_ino}
            if not directory:
                digest = hashlib.sha256()
                while chunk := os.read(fd, 1024 * 1024):
                    digest.update(chunk)
                result["sha256"] = digest.hexdigest()
            after = os.fstat(fd)
            named = path.stat(follow_symlinks=False)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            _require(all(getattr(before, f) == getattr(after, f) == getattr(named, f)
                         for f in fields) and str(path.resolve(strict=True)) == value,
                     "path changed during observation")
            return result
        finally:
            os.close(fd)
    except (OSError, RuntimeError) as exc:
        raise BindingError("path unavailable or unsafe") from exc


def requested_policy(candidate: str) -> dict[str, Any]:
    """Requested contract only. NEVER use this to emit effective-policy evidence."""
    return {"policy": permissions.policy("worker", candidate),
            "launch_flags": permissions.launch_flags("worker", candidate)}


def validate_record(raw: bytes, *, expected: dict[str, Any], pinned_sha256: str) -> dict[str, Any]:
    """Check a candidate record against independently held CONTROL observations.

    `expected` contains OBSERVED_FIELDS plus requested_policy, temporary_roots,
    and protected_roots. It is an online observation bundle, not model input.
    Structural/hash consistency is deliberately insufficient for VERIFIED.
    """
    try:
        _digest(pinned_sha256, "pinned record digest")
        _require(hashlib.sha256(raw).hexdigest() == pinned_sha256, "record digest mismatch")
        record = fleet_json.loads(raw)
        _require(isinstance(record, dict) and set(record) == RECORD_FIELDS, "record fields mismatch")
        _require(type(record["schema_version"]) is int and record["schema_version"] == VERSION
                 and record["kind"] == KIND, "unsupported attestation schema")
        _require(set(expected) == OBSERVED_FIELDS | {"requested_policy", "temporary_roots", "protected_roots"},
                 "incomplete independent observations")
        binding = expected["binding"]
        _require(isinstance(binding, dict) and set(binding) == BINDING_FIELDS
                 and binding["role"] == "worker", "invalid Worker binding")
        for field in BINDING_FIELDS - {"role"}:
            _require(isinstance(binding[field], str)
                     and state.normalize_uuid(binding[field], field) == binding[field],
                     f"noncanonical {field}")
        for field in OBSERVED_FIELDS:
            _same(record[field], expected[field], field)
        for field in ("challenge", "operation_sha256", "observer_artifact_id", "ledger_event_sha256"):
            _digest(record[field], field)
        candidate = expected["candidate"]["realpath"]
        _same(path_identity(candidate, directory=True), expected["candidate"], "candidate inode")
        binary = expected["codex"]
        _require(set(binary) == {"image", "version"} and isinstance(binary["version"], str)
                 and re.fullmatch(r"codex-cli [0-9]+\.[0-9]+\.[0-9]+", binary["version"]) is not None,
                 "incomplete Codex identity")
        _same(path_identity(binary["image"]["realpath"], directory=False), binary["image"], "Codex image")
        policy = requested_policy(candidate)
        _same(expected["requested_policy"], policy, "requested Fleet policy")
        _same(record["requested_policy_sha256"], _sha(policy), "policy digest")
        temporary = expected["temporary_roots"]
        _require(isinstance(temporary, dict) and set(temporary) == {"slash_tmp", "tmpdir"},
                 "temporary roots missing")
        _same(temporary["slash_tmp"], str(Path("/tmp").resolve(strict=True)), "slash_tmp root")
        for root in temporary.values():
            if root is not None:
                path_identity(root, directory=True)
        effective = {"sandbox_policy": {**policy["policy"]["sandbox_policy"], "writable_roots": []},
                     "approval_policy": "never", "product_writable_roots": [candidate],
                     "temporary_roots": temporary}
        _same(record["effective_policy"], effective, "effective policy")
        roots = expected["protected_roots"]
        _require(isinstance(roots, dict) and set(roots) == {"control", "cas", "ledger", "runs"},
                 "protected roots incomplete")
        writable = [Path(candidate), *(Path(p) for p in temporary.values() if p is not None)]
        for value in roots.values():
            path_identity(value, directory=True)
            _require(not any(Path(value).is_relative_to(p) or p.is_relative_to(Path(value))
                             for p in writable), "protected/writable roots overlap")
        process = record["process"]
        _require(set(process) == {"codex_pid", "codex_start_identity", "exec_pid", "exec_start_identity"},
                 "process identity incomplete")
        for field in ("codex_pid", "exec_pid"):
            _require(type(process[field]) is int and process[field] > 0, "invalid PID")
        for field in ("codex_start_identity", "exec_start_identity"):
            _require(isinstance(process[field], str) and bool(process[field]), "process birth identity missing")
        _require(set(record["os_sandbox"]) == {"kind", "profile_sha256"}
                 and record["os_sandbox"]["kind"] == "seatbelt", "unsupported OS sandbox")
        _digest(record["os_sandbox"]["profile_sha256"], "OS profile digest")
        env = record["environment"]
        _require(set(env) == {"names", "sanitized_sha256", "values_hmac_sha256"}
                 and isinstance(env["names"], list) and all(isinstance(n, str) for n in env["names"])
                 and env["names"] == sorted(set(env["names"])), "invalid environment commitment")
        _same(env["sanitized_sha256"], _sha({n: "<redacted>" for n in env["names"]}), "sanitized environment")
        _digest(env["values_hmac_sha256"], "environment commitment")
        invocation = record["invocation"]
        _require(set(invocation) == {"redacted", "sanitized_sha256", "values_hmac_sha256"}, "invalid invocation")
        redacted = invocation["redacted"]
        _require(isinstance(redacted, dict) and set(redacted) == {"argv", "config"}
                 and isinstance(redacted["argv"], list) and bool(redacted["argv"])
                 and all(isinstance(x, str) and x in SAFE_FLAGS | {"<redacted>"} for x in redacted["argv"])
                 and isinstance(redacted["config"], dict)
                 and all(isinstance(k, str) and (k in SAFE_CONFIG_KEYS or re.fullmatch(r"redacted_key_[0-9]+", k))
                         and v == "<redacted>" for k, v in redacted["config"].items()),
                 "invocation contains unredacted values")
        _same(invocation["sanitized_sha256"], _sha(invocation["redacted"]), "sanitized invocation")
        _digest(invocation["values_hmac_sha256"], "invocation commitment")
    except (KeyError, TypeError, fleet_json.FleetJSONError, state.MissionStateError) as exc:
        raise BindingError("malformed or incomplete binding evidence") from exc
    return {"schema_version": VERSION, "record_sha256": pinned_sha256, "record_consistent": True,
            "INTEGRATION_BINDING": "NOT_VERIFIED", "reason": "trusted_exec_hook_unavailable"}


def quarantine_record(runs_dir: Path, raw: bytes, *, expected: dict[str, Any],
                      pinned_sha256: str) -> dict[str, Any]:
    """Store consistent but UNAUTHENTICATED evidence in existing CAS.

    This is not called by boot/submit/finalization and never appends acceptance
    events. The ledger link is one-way; a future trusted hook must anchor the
    receipt independently. Filesystem placement is checked, OS non-writability
    by the actual Worker remains unobserved. Do not call with secret-bearing raw
    records: only commitment/redacted fields are part of this contract.
    """
    result = validate_record(raw, expected=expected, pinned_sha256=pinned_sha256)
    _same(str(runs_dir), expected["protected_roots"]["runs"], "runs root")
    mid = expected["binding"]["mission_id"]
    _same(str(fleet_artifacts.store_path(runs_dir, mid)), expected["protected_roots"]["cas"], "CAS root")
    _same(str(state.mission_root(runs_dir, mid)), expected["protected_roots"]["ledger"], "ledger directory")
    events = state.read_events(state.ledger_path(runs_dir, mid), expected_mission_id=mid)
    _require(any(e["event_sha256"] == expected["ledger_event_sha256"] for e in events),
             "ledger link missing")
    fleet_artifacts.get_bytes(runs_dir, mid, expected["observer_artifact_id"])
    receipt = fleet_artifacts.put_bytes(runs_dir, mid, raw)
    return {**result, "artifact_id": receipt["artifact_id"],
            "ledger_event_sha256": expected["ledger_event_sha256"], "authority": "none"}
