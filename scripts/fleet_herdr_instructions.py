"""Bounded target instruction snapshots from the immutable candidate baseline."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import subprocess

import fleet_artifacts
import fleet_mission_state as state

MAX_FILES = 32
MAX_FILE_BYTES = 32 * 1024
MAX_TOTAL_BYTES = 64 * 1024


def _git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-C", str(repo), *args], env=env, capture_output=True, timeout=30, check=True)
    return result.stdout


def snapshot(candidate: Path, base_sha: str):
    if not state.GIT_OID.fullmatch(base_sha):
        raise ValueError("instruction baseline must be a Git object id")
    selected = {}
    for record in _git(candidate, "ls-tree", "-r", "-z", base_sha).split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        # Non-instruction filenames need not be UTF-8 to discover instructions.
        if raw_path.rsplit(b"/", 1)[-1] not in {b"AGENTS.md", b"AGENTS.override.md"}:
            continue
        path = PurePosixPath(raw_path.decode("utf-8"))
        mode, kind, oid = metadata.decode("ascii").split()
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError("target instructions must be regular tracked files")
        scope = str(path.parent)
        previous = selected.get(scope)
        if previous is None or path.name == "AGENTS.override.md":
            selected[scope] = (path, oid)
    if len(selected) > MAX_FILES:
        raise ValueError("target instruction scope count exceeds supported limit")
    entries, total = [], 0
    for scope, (path, oid) in sorted(selected.items()):
        size = int(_git(candidate, "cat-file", "-s", oid))
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("target instructions exceed supported byte limit")
        raw = _git(candidate, "cat-file", "blob", oid)
        if len(raw) != size:
            raise ValueError("instruction object size changed")
        entries.append({"path": str(path), "scope": scope, "sha256": state.artifact_id(raw),
                        "content": raw.decode("utf-8")})
    return {"schema_version": 1, "discovery": "tracked-candidate-baseline-v1", "base_sha": base_sha,
        "application": "Root scope applies throughout the candidate. A nested scope applies only inside that directory; nearer scopes refine broader ones. AGENTS.override.md replaces AGENTS.md in the same directory. Task authority and result protocol still apply. No ancestor/global instructions are claimed by this snapshot.",
        "entries": entries}


def packet(runs, mid, candidate, current):
    first = next((a for a in current["admissions"].values() if a["request_key"] == "herdr:plan"), None)
    if first:
        task = state.loads_strict(fleet_artifacts.get_bytes(runs, mid, first["task_sha256"]))
        prior = task.get("project_instructions")
        if prior is not None:
            raw = fleet_artifacts.get_bytes(runs, mid, prior["snapshot_artifact_id"])
            if raw != state.canonical_bytes(prior["snapshot"]):
                raise ValueError("instruction snapshot differs from task CAS")
        return prior  # Historical tasks are never silently upgraded mid-Mission.
    value = snapshot(candidate, current["base_sha"])
    artifact = fleet_artifacts.put_bytes(runs, mid, state.canonical_bytes(value))
    return {"snapshot_artifact_id": artifact["artifact_id"], "snapshot": value}
