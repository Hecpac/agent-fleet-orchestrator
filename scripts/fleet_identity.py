#!/usr/bin/env python3
"""Validate durable fleet UUIDs against the current cmux tree."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


UUID_RE = r"[0-9A-Fa-f-]{36}"


def read_manifest(path: str) -> dict[str, str]:
    return dict(
        line.strip().split("=", 1)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


def current_tree(workspace: str | None = None) -> str:
    command = ["cmux", "tree"]
    if workspace:
        command += ["--workspace", workspace]
    else:
        command += ["--all"]
    command += ["--id-format", "both"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cmux tree probe failed: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "cmux tree failed")
    return result.stdout


def mappings(tree: str) -> tuple[dict[str, str], dict[str, str]]:
    workspaces = dict(re.findall(rf"(workspace:\d+) ({UUID_RE})", tree))
    surfaces = dict(re.findall(rf"(surface:\d+) ({UUID_RE})", tree))
    return workspaces, surfaces


def validate(manifest: dict[str, str], instances: list[str]) -> list[str]:
    workspace = manifest.get("workspace", "")
    expected_workspace_uuid = manifest.get("workspace_uuid", "").upper()
    if not workspace or not expected_workspace_uuid:
        return ["manifest lacks workspace/workspace_uuid"]
    workspaces, surfaces = mappings(current_tree(workspace))
    errors: list[str] = []
    if workspaces.get(workspace, "").upper() != expected_workspace_uuid:
        errors.append(f"workspace identity mismatch: {workspace}")
    targets = instances or [
        key
        for key in manifest
        if key != "workspace" and not key.endswith(tuple([".uuid", ".role_type", ".runner", ".phase", ".authority", ".tool_access", ".provider", ".display_rank", ".resource_class"]))
        and re.fullmatch(r"(?:lead|[a-z][a-z0-9_-]{0,47})", key)
        and manifest[key].startswith("surface:")
    ]
    for instance in targets:
        ref = manifest.get(instance, "")
        expected_uuid = manifest.get(f"{instance}.uuid", "").upper()
        if not ref or not expected_uuid:
            errors.append(f"manifest lacks identity for instance: {instance}")
        elif surfaces.get(ref, "").upper() != expected_uuid:
            errors.append(f"surface identity mismatch: {instance} ({ref})")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "exists"))
    parser.add_argument("manifest")
    parser.add_argument("instances", nargs="*")
    args = parser.parse_args()
    manifest = read_manifest(args.manifest)
    if args.command == "exists":
        try:
            workspaces, _ = mappings(current_tree())
        except RuntimeError as exc:
            print(f"cannot probe cmux identity: {exc}", file=sys.stderr)
            return 2
        expected = manifest.get("workspace_uuid", "").upper()
        return 0 if expected and expected in {value.upper() for value in workspaces.values()} else 1
    errors = validate(manifest, args.instances)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 2
    print("identity valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
