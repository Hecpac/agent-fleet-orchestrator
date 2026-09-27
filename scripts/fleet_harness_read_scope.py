"""Explicit public files, independent from the complete physical write scope."""
import copy
import os
from pathlib import Path

import fleet_herdr_scope as physical
import fleet_harness_sandbox as sandbox
import fleet_json
from fleet_safe_paths import RootedFS

VERSION = "public-read-manifest-v1"


class InputBindingError(ValueError):
    """An admitted input changed; repair must not silently renew its authority."""
    pass


def require_files(actual, expected):
    # Canonical JSON preserves exact types (True must not equal byte length 1).
    if fleet_json.canonical_bytes(actual)!=fleet_json.canonical_bytes(expected):
        raise InputBindingError("executor input differs from admitted command predecessor")


def editable_files(inventory, scope, *, mode=0o644):
    """Comparable exact bytes at an admission, projection or frozen revision."""
    physical._inventory(inventory, scope)
    if inventory["complete"] is not True: raise ValueError("incomplete editable input inventory")
    result={}
    for name in scope["editable_paths"]:
        entry=inventory["entries"].get(name,{})
        if entry.get("kind")!="file" or entry["mode"]!=mode: raise ValueError("editable input type/mode changed")
        result[name]={k:entry[k] for k in ("sha256","bytes")}
    return result


def validate(manifest, scope):
    physical.validate(scope)
    if (not isinstance(manifest, dict) or set(manifest) != {"version", "editable_paths", "read_only"}
            or manifest["version"] != VERSION or manifest["editable_paths"] != scope["editable_paths"]
            or not isinstance(manifest["read_only"], dict) or len(manifest["read_only"]) > 256):
        raise ValueError("public read manifest differs from declared write scope")
    names = list(manifest["editable_paths"]) + list(manifest["read_only"])
    if len(set(names)) != len(names): raise ValueError("public read/write paths overlap")
    for name in names:
        physical.path(name)
        if any(physical.under(name, t) or physical.under(t, name) for t in scope["temporary_directories"]):
            raise ValueError("public file overlaps a temporary directory")
        if any(other != name and physical.under(other, name) for other in names):
            raise ValueError("public file conflicts with a parent directory")
    for value in manifest["read_only"].values():
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("readonly public file lacks exact content pin")
    return copy.deepcopy(manifest)


def prepare(candidate, scope, readonly_paths=()):
    if not isinstance(readonly_paths, (tuple, list)) or len(set(readonly_paths)) != len(readonly_paths):
        raise ValueError("public readonly paths must be an explicit unique list")
    manifest = {"version": VERSION, "editable_paths": list(scope["editable_paths"]),
                "read_only": {name: "0"*64 for name in readonly_paths}}
    validate(manifest, scope)
    inventory = physical.capture(candidate, scope)
    if not inventory["complete"]: raise ValueError("incomplete candidate public-read capture")
    with RootedFS(candidate) as fs:
        for name in readonly_paths:
            entry = inventory["entries"].get(name, {})
            if entry.get("kind") != "file": raise ValueError("public readonly path is not a regular file")
            raw = fs.read_regular(name, directory_modes=(None,)*(len(Path(name).parts)-1),
                                  file_mode=entry["mode"], max_bytes=1024*1024)
            manifest["read_only"][name] = sandbox.digest(raw)
    return validate(manifest, scope)


def directories(manifest, scope):
    result = set(scope["temporary_directories"])
    for name in [*manifest["editable_paths"], *manifest["read_only"], *scope["temporary_directories"]]:
        result.update(str(parent) for parent in Path(name).parents if str(parent) != ".")
    return result


def verify_snapshot(snapshot, manifest, scope, *, editable_hashes=None):
    manifest = validate(manifest, scope)
    if (not isinstance(snapshot, dict) or set(snapshot) != {"version", "root_git_absent", "inventory"}
            or snapshot["version"] != "public-projection-v1" or snapshot["root_git_absent"] is not True):
        raise ValueError("invalid public projection attestation")
    inventory = snapshot["inventory"]
    physical._inventory(inventory, scope)
    if inventory["complete"] is not True: raise ValueError("incomplete public projection")
    if editable_hashes is not None and set(editable_hashes) != set(manifest["editable_paths"]):
        raise ValueError("projection does not bind all editable bytes")
    entries = inventory["entries"]
    names = set(manifest["editable_paths"]) | set(manifest["read_only"])
    dirs = directories(manifest, scope)
    if set(entries) != names | dirs: raise ValueError("projection contains undeclared or missing public paths")
    for name, entry in entries.items():
        if name in dirs:
            if entry["kind"] != "directory" or entry["mode"] != 0o755: raise ValueError("public projection directory changed")
        else:
            editable = name in manifest["editable_paths"]
            if entry["kind"] != "file" or entry["mode"] != (0o666 if editable else 0o644):
                raise ValueError("public projection file mode/type changed")
            expected = editable_hashes.get(name) if editable and editable_hashes is not None else manifest["read_only"].get(name)
            if expected is not None and entry["sha256"] != expected: raise ValueError("public projection content differs from pinned read authority")
    return snapshot


def observe(workspace, manifest, scope, *, editable_hashes=None):
    # Physical candidate capture intentionally excludes root .git. A projection
    # has no such exclusion: even a dangling .git symlink is forbidden.
    if os.path.lexists(Path(workspace)/".git"): raise ValueError("undeclared .git in public projection")
    inventory = physical.capture(workspace, scope)
    if os.path.lexists(Path(workspace)/".git"): raise ValueError("undeclared .git in public projection")
    return verify_snapshot({"version":"public-projection-v1", "root_git_absent":True, "inventory":inventory},
                           manifest, scope, editable_hashes=editable_hashes)
