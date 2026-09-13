#!/usr/bin/env python3
"""Create a clean private target from current source bytes, preserving the source."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys


class SnapshotError(ValueError):
    pass


def git(repo: Path, *args: str, extra_env=None) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", **(extra_env or {}))
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                             "-C", str(repo), *args], env=env, capture_output=True, timeout=120)
    if result.returncode:
        raise SnapshotError(f"git {args[0]} failed")
    return result.stdout


def names(raw: bytes) -> list[str]:
    return sorted(set(filter(None, raw.decode("utf-8").split("\0"))))


def inventory(source: Path) -> dict:
    head = git(source, "rev-parse", "HEAD").decode().strip()
    if any(row.startswith(b"160000 ") for row in git(source, "ls-tree", "-r", "HEAD").splitlines()):
        raise SnapshotError("submodules require an explicit snapshot adapter")
    if any(row and not row.startswith(b"H ") for row in git(source, "ls-files", "-v", "-z").split(b"\0")):
        raise SnapshotError("index flags may hide source changes")
    selected = names(git(source, "ls-files", "-co", "--exclude-standard", "-z"))
    excluded = names(git(source, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"))
    files, removed = {}, []
    for name in selected:
        path = source / name
        if not path.exists() and not path.is_symlink():
            removed.append(name)
            continue
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or path.resolve() != path or info.st_nlink != 1):
            raise SnapshotError("snapshot requires regular non-aliased source files: " + name)
        raw = path.read_bytes()
        files[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
                       "executable": bool(info.st_mode & stat.S_IXUSR)}
    return {"head": head, "files": files, "removed": removed, "excluded_ignored": excluded}


def create(source: Path, output: Path, *, exclude_ignored: bool = False) -> dict:
    source = source.resolve(strict=True)
    output = output.absolute()
    if output.resolve() != output or output.exists() or output.is_symlink():
        raise SnapshotError("output must be a new physical directory")
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise SnapshotError("snapshot output must be outside the source repository")
    if git(source, "rev-parse", "--show-toplevel").decode().strip() != str(source):
        raise SnapshotError("source must be the exact Git toplevel")
    before = inventory(source)
    if before["excluded_ignored"] and not exclude_ignored:
        raise SnapshotError("ignored inputs exist; explicitly select --exclude-ignored or supply a clean source")
    output.mkdir(mode=0o700)
    target = output / "target"
    # The receipt stays outside target, so the ordinary clean-target driver can use it.
    intent = {"schema_version": 1, "source": str(source), "target": str(target), "baseline": before}
    (output / "intent.json").write_text(json.dumps(intent, indent=2) + "\n")
    git(source, "clone", "--no-local", "--no-hardlinks", "--", str(source), str(target))
    target.chmod(0o700)
    # Replace clone contents with the selected physical bytes, including tracked deletions.
    for entry in target.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for name, expected in before["files"].items():
        path = source / name
        if path.resolve() != path or not stat.S_ISREG(path.lstat().st_mode):
            raise SnapshotError("source file identity changed during snapshot")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected["sha256"]:
            raise SnapshotError("source bytes changed during snapshot")
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        destination.chmod(0o755 if expected["executable"] else 0o644)
    if inventory(source) != before:
        raise SnapshotError("source changed during snapshot; partial output retained for inspection")
    # Git history is created only in this new private target; source refs/index are untouched.
    git(target, "add", "-A", "--", ".")
    tree = git(target, "write-tree").decode().strip()
    identity = {"GIT_AUTHOR_NAME": "Fleet Snapshot", "GIT_AUTHOR_EMAIL": "snapshot@localhost.invalid",
                "GIT_COMMITTER_NAME": "Fleet Snapshot", "GIT_COMMITTER_EMAIL": "snapshot@localhost.invalid"}
    commit = git(target, "commit-tree", tree, "-p", before["head"], "-m", "Private source snapshot",
                 extra_env=identity).decode().strip()
    git(target, "symbolic-ref", "HEAD", "refs/heads/fleet-personal-snapshot")
    git(target, "update-ref", "HEAD", commit)
    git(target, "remote", "remove", "origin")
    if git(target, "status", "--porcelain=v1", "--untracked-files=all", "--ignored"):
        raise SnapshotError("snapshot target is not clean")
    actual = inventory(target)
    if actual["files"] != before["files"] or inventory(source) != before:
        raise SnapshotError("snapshot content or source preservation check failed")
    receipt = {**intent, "snapshot_head": commit, "snapshot_tree": tree,
               "status": "VERIFIED", "scope": "selected_source_bytes_and_source_preservation",
               "ignored_policy": "excluded_explicitly" if exclude_ignored else "none_present"}
    path = output / "snapshot.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    path.chmod(0o600)
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exclude-ignored", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = create(args.source, args.output, exclude_ignored=args.exclude_ignored)
    except (SnapshotError, OSError, subprocess.SubprocessError) as exc:
        print(f"personal-snapshot: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({k: result[k] for k in ("status", "target", "snapshot_head", "ignored_policy")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
