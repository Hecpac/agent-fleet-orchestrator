"""Versioned runtime paths and role inputs; no launch or acceptance authority."""
from __future__ import annotations

import os
from pathlib import Path
import stat

import fleet_safe_paths


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
    if policy != "independent-v1":
        raise RuntimeContractError("unsupported Herdr input policy")
    dependencies = {"plan": (), "build": ("plan",), "review": ("plan", "build"),
                    "verify": ("plan", "build"),
                    "synthesis": ("plan", "build", "review", "verify")}
    try:
        return [completed[name] for name in dependencies[stage]]
    except KeyError as exc:
        raise RuntimeContractError("Herdr stage dependencies are incomplete") from exc
