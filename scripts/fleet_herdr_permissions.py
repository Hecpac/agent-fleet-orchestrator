"""Versioned Codex permission attestation for the bounded Herdr roster.

This attests recorded turn configuration, not hostile-code containment. Version 1
preserves Codex's existing temporary-directory access for the sole writer.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

VERSION = 1
MINIMUM_ARCHIVE_SCHEMA_VERSION = 3
REQUIRED_TURNS = 5
MODELS = {"lead": "gpt-6-astra", "worker": "gpt-5.6-sol",
          "reviewer": "gpt-5.6-sol", "verifier": "gpt-5.6-sol"}


class PermissionError(ValueError):
    pass


def finalization_policy(compiled_digest: str) -> dict[str, Any]:
    """Controller policy frozen in the Mission ledger, never inferred from an index."""
    return {"compiled_digest": compiled_digest,
            "minimum_archive_schema_version": MINIMUM_ARCHIVE_SCHEMA_VERSION,
            "permissions_policy_version": VERSION, "required_turns": REQUIRED_TURNS}


def policy(role: str, cwd: str, *, version: int = VERSION) -> dict[str, Any]:
    if type(version) is not int or version != VERSION or not isinstance(role, str) or role not in MODELS:
        raise PermissionError("unsupported Herdr permission policy/role")
    if (not isinstance(cwd, str) or not PurePosixPath(cwd).is_absolute()
            or str(PurePosixPath(cwd)) != cwd or ".." in PurePosixPath(cwd).parts):
        raise PermissionError("permission policy cwd must be an exact absolute path")
    sandbox: dict[str, Any] = {"type": "read-only"}
    if role == "worker":
        sandbox = {"type": "workspace-write", "network_access": False,
                   "exclude_tmpdir_env_var": False, "exclude_slash_tmp": False}
    return {"version": version, "role": role, "cwd": cwd, "model": MODELS[role],
            "effort": "high", "approval_policy": "never", "sandbox_policy": sandbox}


def launch_flags(role: str, cwd: str) -> list[str]:
    expected = policy(role, cwd)
    flags = ["--sandbox", expected["sandbox_policy"]["type"], "--ask-for-approval", "never"]
    if role == "worker":
        for key in ("network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp"):
            flags += ["-c", f"sandbox_workspace_write.{key}=false"]
        flags += ["-c", "sandbox_workspace_write.writable_roots=[]"]
    return flags


def attest(contexts: list[dict[str, Any]], expected: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(expected, dict):
        raise PermissionError("permission policy must be an object")
    if expected != policy(expected.get("role"), expected.get("cwd"), version=expected.get("version")):
        raise PermissionError("permission policy differs from its versioned definition")
    if not contexts:
        raise PermissionError("permission evidence has no turn_context")
    for number, context in enumerate(contexts):
        if not isinstance(context, dict):
            raise PermissionError(f"permission context {number}: context must be an object")
        for field in ("cwd", "model", "effort", "approval_policy"):
            if context.get(field) != expected[field]:
                raise PermissionError(f"permission context {number}: {field} missing or incompatible")
        observed = context.get("sandbox_policy")
        if not isinstance(observed, dict):
            raise PermissionError(f"permission context {number}: sandbox_policy missing or incompatible")
        # Codex omits empty writable_roots in some versions. No nonempty roots,
        # implicit booleans, or new unrecognized permission fields are accepted.
        observed = dict(observed)
        if expected["role"] == "worker" and "writable_roots" in observed:
            if observed.pop("writable_roots") != []:
                raise PermissionError(f"permission context {number}: unauthorized writable_roots")
        if (set(observed) != set(expected["sandbox_policy"])
                or any(type(observed[k]) is not type(v) or observed[k] != v
                       for k, v in expected["sandbox_policy"].items())):
            raise PermissionError(f"permission context {number}: sandbox_policy missing or incompatible")
    return {"status": "attested", "policy_version": expected["version"],
            "scope": "recorded_codex_turn_configuration", "role": expected["role"],
            "contexts": len(contexts)}
