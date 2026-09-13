"""Versioned runtime paths and role inputs; no launch or acceptance authority."""
from __future__ import annotations

import os
from pathlib import Path
import stat

import fleet_safe_paths
import fleet_json


HANDOFF_POLICY = "bounded-cas-v1"
HANDOFF_MAX_BYTES = 128 * 1024
HANDOFF_SUMMARY_MAX_BYTES = 48 * 1024


class RuntimeContractError(RuntimeError):
    """A runtime contract is unsafe or unsupported."""


def candidate_path(runs: Path, mission_id: str, options: dict, target: Path) -> Path:
    layout = options.get("herdr_layout")
    if layout is None:
        return runs / "missions" / mission_id / "candidate"
    if (not isinstance(layout, dict) or set(layout) != {"version", "runtime_root"}
            or type(layout["version"]) is not int or layout["version"] != 2
            or not isinstance(layout["runtime_root"], str)):
        raise RuntimeContractError("unsupported Herdr runtime layout")
    root = Path(layout["runtime_root"])
    # Reject aliases rather than normalize away evidence of them.
    if not root.is_absolute() or str(root) != layout["runtime_root"]:
        raise RuntimeContractError("runtime root must be an exact absolute path")
    try:
        canonical = fleet_safe_paths.canonical_root(root)
        if canonical != root:
            raise RuntimeContractError("runtime root alias")
        with fleet_safe_paths.RootedFS(root, root_mode=0o700):
            pass
    except (OSError, fleet_safe_paths.SafePathError) as exc:
        raise RuntimeContractError("runtime root must be an existing owned private directory") from exc
    for protected in (runs, target):
        protected = protected.resolve(strict=True)
        if root.is_relative_to(protected) or protected.is_relative_to(root):
            raise RuntimeContractError("runtime root overlaps protected runs or source checkout")
    return root / mission_id / "candidate"


def prepare_parent(candidate: Path, options: dict) -> None:
    """Claim a fresh Mission directory; never reuse an ambiguous partial launch."""
    if options.get("herdr_layout") is None:
        return
    root = candidate.parent.parent
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(root, flags)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeContractError("runtime root ownership or mode changed")
        if (info.st_dev, info.st_ino) != (root.stat().st_dev, root.stat().st_ino):
            raise RuntimeContractError("runtime root identity changed")
        os.mkdir(candidate.parent.name, mode=0o700, dir_fd=fd)
        os.fsync(fd)
    except FileExistsError as exc:
        raise RuntimeContractError("runtime Mission directory already exists; reconcile without reuse") from exc
    finally:
        os.close(fd)


def parent_identity(candidate: Path, options: dict) -> dict:
    if options.get("herdr_layout") is None:
        return {}
    identities = {}
    for name, path in (("runtime_root", candidate.parent.parent), ("runtime_mission", candidate.parent)):
        if path.resolve(strict=True) != path or path.is_symlink():
            raise RuntimeContractError("runtime directory alias")
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeContractError("runtime directory ownership or mode changed")
        identities[name] = {"dev": info.st_dev, "ino": info.st_ino}
    return identities


def role_inputs(stage: str, completed: dict[str, str], policy: str | None) -> list[str]:
    if policy is None:
        return list(completed.values())
    if policy == "independent-research-v1":
        dependencies = {"plan": (), "research": ("plan",),
                        "build": ("plan", "research"),
                        "review": ("plan", "research", "build"),
                        "verify": ("plan", "research", "build"),
                        "synthesis": ("plan", "research", "build", "review", "verify")}
        try:
            return [completed[name] for name in dependencies[stage]]
        except KeyError as exc:
            raise RuntimeContractError("Research stage dependencies are incomplete") from exc
    if policy == "minimal-build-v1":
        if stage != "build" or completed:
            raise RuntimeContractError("minimal Build must have no prior role inputs")
        return []
    if policy != "independent-v1":
        raise RuntimeContractError("unsupported Herdr input policy")
    dependencies = {"plan": (), "build": ("plan",), "review": ("plan", "build"),
                    "verify": ("plan", "build"),
                    "synthesis": ("plan", "build", "review", "verify")}
    try:
        return [completed[name] for name in dependencies[stage]]
    except KeyError as exc:
        raise RuntimeContractError("Herdr stage dependencies are incomplete") from exc


def bounded_handoff(*, mission_id: str, stage: str, input_artifact_ids: list[str],
                    current: dict, read_artifact) -> dict:
    """Resolve a small, role-bound Plan/Research handoff from admitted CAS.

    The task still carries the historical ID list.  This additive package makes
    the useful content observable for new missions without rewriting an already
    admitted task during recovery.
    """
    if stage != "build":
        raise RuntimeContractError("bounded handoff is currently defined only for Build")
    expected = {"herdr:plan": "plan", "herdr:research": "research"}
    admissions = {}
    for admission in current.get("admissions", {}).values():
        name = expected.get(admission.get("request_key"))
        if name is not None:
            if name in admissions:
                raise RuntimeContractError("handoff role has multiple admissions")
            admissions[name] = admission
    if set(admissions) != set(expected.values()) or len(input_artifact_ids) != 2:
        raise RuntimeContractError("Build handoff requires exactly Plan and Research")
    entries = []
    for name, artifact_id in zip(("plan", "research"), input_artifact_ids):
        admission = admissions[name]
        recorded = admission.get("result")
        if (admission.get("phase") != "finalized" or admission.get("active")
                or admission.get("terminal", {}).get("status") != "succeeded"
                or not isinstance(recorded, dict) or recorded.get("artifact_id") != artifact_id):
            raise RuntimeContractError("handoff input is outside its finalized role admission")
        raw = read_artifact(artifact_id)
        result = fleet_json.loads(raw)
        if (not isinstance(result, dict) or result.get("mission_id") != mission_id
                or result.get("run_id") != admission.get("run_id")
                or result.get("instance_id") != admission.get("recipient_instance")
                or result.get("status") != "PASS"):
            raise RuntimeContractError("handoff result binding differs from its admission")
        summary = result.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise RuntimeContractError("handoff result has no usable summary")
        if len(summary.encode("utf-8")) > HANDOFF_SUMMARY_MAX_BYTES:
            raise RuntimeContractError("handoff summary exceeds its explicit bound")
        references = result.get("evidence_artifact_ids", [])
        checks = result.get("artifacts", [])
        if (not isinstance(references, list) or any(not isinstance(pin, str) for pin in references)
                or not isinstance(checks, list)):
            raise RuntimeContractError("handoff evidence references are invalid")
        # Reading every pin now proves availability and hash identity in this CAS;
        # detailed bytes remain available from artifact_store without bulk injection.
        for pin in references:
            read_artifact(pin)
        entries.append({"stage": name, "instance_id": admission["recipient_instance"],
            "run_id": admission["run_id"], "result_artifact_id": artifact_id,
            "summary": summary, "artifact_checks": checks,
            "evidence_artifact_ids": references})
    package = {"schema_version": 1, "policy": HANDOFF_POLICY,
               "mission_id": mission_id, "target_stage": stage, "inputs": entries}
    if len(fleet_json.canonical_bytes(package)) > HANDOFF_MAX_BYTES:
        raise RuntimeContractError("bounded handoff package exceeds its explicit limit")
    return package
