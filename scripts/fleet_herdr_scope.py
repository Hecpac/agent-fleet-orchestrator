"""Opt-in physical candidate acceptance, not effect mediation or a repair loop.

The creation ledger pins the contract; a pre-admission event pins the baseline.
Inventory includes ignored files, excludes only root .git, and never follows
links. Receipts prove observed snapshots, not absence of transient/outside writes.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile

import fleet_artifacts
import fleet_json
import fleet_mission_state as state
import fleet_safe_paths


FIELD = "scope_contract_sha256"
BASELINE_EVENT = "herdr_scope_baseline_captured"
ARCHIVE_VERSION = 8


class ScopeError(state.MissionStateError):
    pass


class ScopeRejected(ScopeError):
    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__("physical scope " + receipt["status"])


def digest(value):
    return hashlib.sha256(fleet_json.canonical_bytes(value)).hexdigest()


def path(value):
    if (not isinstance(value, str) or not value or len(value) > 512
            or any(ord(c) < 32 for c in value) or "\\" in value):
        raise ScopeError("scope path must be a bounded canonical relative path")
    try:
        if len(value.encode("utf-8")) > 512:
            raise ScopeError("scope path exceeds 512 UTF-8 bytes")
    except UnicodeError as exc:
        raise ScopeError("scope path must be valid UTF-8") from exc
    p = PurePosixPath(value)
    if not p.parts or p.is_absolute() or str(p) != value or any(x in {".", "..", ".git"} for x in p.parts):
        raise ScopeError("unsafe scope path")
    return value


def under(name, directory):
    return name == directory or name.startswith(directory + "/")


def validate(contract):
    if (not isinstance(contract, dict) or set(contract) != {
            "schema_version", "editable_paths", "temporary_directories", "max_entries", "max_bytes"}
            or type(contract["schema_version"]) is not int or contract["schema_version"] != 1):
        raise ScopeError("unsupported scope contract")
    for field in ("editable_paths", "temporary_directories"):
        values = contract[field]
        if not isinstance(values, list) or len(values) > 256:
            raise ScopeError("scope paths must be bounded lists")
        for value in values:
            path(value)
        if len(set(values)) != len(values):
            raise ScopeError("duplicate scope path")
    all_paths = contract["editable_paths"] + contract["temporary_directories"]
    if any(a != b and (under(a, b) or under(b, a)) for i, a in enumerate(all_paths) for b in all_paths[i+1:]) or len(set(all_paths)) != len(all_paths):
        raise ScopeError("overlapping scope paths")
    for field, maximum in (("max_entries", 10000), ("max_bytes", 128 * 1024 * 1024)):
        if type(contract[field]) is not int or not 1 <= contract[field] <= maximum:
            raise ScopeError("invalid scope inventory limit: " + field)
    return contract


def validate_binding(current, options):
    contract = options.get("scope_contract")
    pin = digest(validate(contract)) if contract is not None else None
    if current.get(FIELD) != pin:
        raise ScopeError("scope contract differs from creation ledger")
    if pin is not None and current.get("herdr_profile") != "sol_minimal_v1":
        raise ScopeError("physical scope v1 requires sol_minimal_v1")
    return contract


def load(filename):
    with Path(filename).open("rb") as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ScopeError("scope contract exceeds size limit")
    return validate(fleet_json.loads(raw))


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _scan(root, contract):
    entries, stamps = {}, {}
    consumed = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(root, directory_flags)
    identity = {"dev": os.fstat(root_fd).st_dev, "ino": os.fstat(root_fd).st_ino}

    def walk(fd, prefix, depth):
        nonlocal consumed
        if depth > 64:
            raise ScopeError("inventory depth limit")
        before = os.fstat(fd)
        with os.scandir(fd) as iterator:
            for entry in iterator:
                name = prefix + entry.name
                info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                if not prefix and entry.name == ".git":
                    if not stat.S_ISDIR(info.st_mode):
                        raise ScopeError("root .git must be a physical directory")
                    continue
                path(name)
                if len(entries) >= contract["max_entries"]:
                    raise ScopeError("inventory entry limit")
                mode = stat.S_IMODE(info.st_mode)
                if mode & 0o7000 or info.st_uid != os.geteuid():
                    raise ScopeError("unsupported mode or owner: " + name)
                if stat.S_ISDIR(info.st_mode):
                    entries[name] = {"kind": "directory", "mode": mode}
                    child = os.open(entry.name, directory_flags, dir_fd=fd)
                    try:
                        if _stamp(os.fstat(child)) != _stamp(info):
                            raise ScopeError("directory changed during capture: " + name)
                        walk(child, name + "/", depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    if consumed + info.st_size > contract["max_bytes"]:
                        raise ScopeError("inventory byte limit")
                    source = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        if _stamp(os.fstat(source)) != _stamp(info):
                            raise ScopeError("file changed during capture: " + name)
                        h, size = hashlib.sha256(), 0
                        while True:
                            chunk = os.read(source, min(65536, info.st_size - size + 1))
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > info.st_size:
                                raise ScopeError("file grew during capture: " + name)
                            h.update(chunk)
                        if size != info.st_size or _stamp(os.fstat(source)) != _stamp(info):
                            raise ScopeError("file changed during capture: " + name)
                        consumed += size
                        entries[name] = {"kind": "file", "mode": mode, "bytes": size, "sha256": h.hexdigest()}
                    finally:
                        os.close(source)
                else:
                    raise ScopeError("unsupported link or file type: " + name)
                if _stamp(os.stat(entry.name, dir_fd=fd, follow_symlinks=False)) != _stamp(info):
                    raise ScopeError("path changed during capture: " + name)
                stamps[name] = _stamp(info)
        if _stamp(os.fstat(fd)) != _stamp(before):
            raise ScopeError("directory changed during capture: " + (prefix or "."))
        stamps[prefix or "."] = _stamp(before)

    try:
        walk(root_fd, "", 0)
        now = root.lstat()
        if identity != {"dev": now.st_dev, "ino": now.st_ino} or not stat.S_ISDIR(now.st_mode):
            raise ScopeError("inventory root changed")
        return entries, stamps, identity
    finally:
        os.close(root_fd)


def capture(root, contract):
    """Two bounded no-follow passes; an incomplete capture can never pass."""
    validate(contract)
    root = Path(root)
    result = {"schema_version": 1, "root": str(root), "identity": None,
              "complete": False, "entries": {}, "errors": []}
    try:
        root = fleet_safe_paths.canonical_root(root)
        first, stamps, identity = _scan(root, contract)
        second, again, next_identity = _scan(root, contract)
        if first != second or stamps != again or identity != next_identity:
            raise ScopeError("inventory changed between capture passes")
        result.update(identity=identity, complete=True, entries=first)
    except (OSError, ScopeError, fleet_safe_paths.SafePathError) as exc:
        result["errors"] = [str(exc)]
    return result


def _inventory(value, contract):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "root", "identity", "complete", "entries", "errors"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["complete"]) is not bool or not isinstance(value["entries"], dict)
            or not isinstance(value["errors"], list) or len(value["errors"]) > 1
            or any(not isinstance(e, str) for e in value["errors"])
            or not isinstance(value["root"], str) or not PurePosixPath(value["root"]).is_absolute()
            or len(value["entries"]) > contract["max_entries"]):
        raise ScopeError("invalid scope inventory")
    if value["complete"]:
        identity = value["identity"]
        if (value["errors"] or not isinstance(identity, dict) or set(identity) != {"dev", "ino"}
                or any(type(v) is not int or v < 0 for v in identity.values())):
            raise ScopeError("invalid complete inventory identity")
    elif not value["errors"]:
        raise ScopeError("incomplete inventory lacks cause")
    for name, entry in value["entries"].items():
        path(name)
        if not isinstance(entry, dict) or entry.get("kind") not in {"file", "directory"}:
            raise ScopeError("invalid inventory entry")
        fields = {"kind", "mode"} | ({"bytes", "sha256"} if entry["kind"] == "file" else set())
        if set(entry) != fields or type(entry["mode"]) is not int or not 0 <= entry["mode"] <= 0o777:
            raise ScopeError("invalid inventory mode/fields")
        if entry["kind"] == "file" and (type(entry["bytes"]) is not int or entry["bytes"] < 0
                or not isinstance(entry["sha256"], str) or not state.SHA256.fullmatch(entry["sha256"])):
            raise ScopeError("invalid inventory file")
        for parent in PurePosixPath(name).parents:
            if str(parent) != "." and value["entries"].get(str(parent), {}).get("kind") != "directory":
                raise ScopeError("inventory omitted a parent directory")
    if sum(e.get("bytes", 0) for e in value["entries"].values()) > contract["max_bytes"]:
        raise ScopeError("inventory exceeds contract byte limit")


def is_temporary(name, contract):
    return any(under(name, directory) for directory in contract["temporary_directories"])


def delivery_paths(baseline, observed, contract):
    return sorted((set(baseline["tracked_paths"]) | set(contract["editable_paths"]))
                  & {p for p, e in observed["entries"].items() if e["kind"] == "file"})


def evaluate(baseline, observed, contract, *, tree_sha=None, tree=None):
    validate(contract)
    _inventory(baseline["inventory"], contract)
    _inventory(observed, contract)
    before, after = baseline["inventory"], observed
    issues = []
    if baseline["contract_sha256"] != digest(contract):
        raise ScopeError("baseline contract changed")
    tracked = baseline["tracked_paths"]
    if (not isinstance(tracked, list) or len(tracked) != len(set(tracked))
            or any(before["entries"].get(p, {}).get("kind") != "file" for p in tracked)):
        raise ScopeError("baseline tracked files differ from inventory")
    if not before["complete"] or not after["complete"]:
        status = "incomplete"
        issues = [{"path": None, "reason": e} for e in before["errors"] + after["errors"]]
    else:
        if before["root"] != after["root"] or before["identity"] != after["identity"]:
            issues.append({"path": None, "reason": "candidate_identity_changed"})
        for name in sorted(set(before["entries"]) | set(after["entries"])):
            old, new = before["entries"].get(name), after["entries"].get(name)
            if is_temporary(name, contract):
                if old is not None:
                    issues.append({"path": name, "reason": "temporary_path_preexisted"})
                elif name in contract["temporary_directories"] and new is not None and new["kind"] != "directory":
                    issues.append({"path": name, "reason": "temporary_root_not_directory"})
                continue
            if old == new:
                continue
            if name in contract["editable_paths"] and (old is None or old["kind"] == "file") and (new is None or new["kind"] == "file"):
                continue
            if old is None and new["kind"] == "directory" and any(p.startswith(name + "/") for p in contract["editable_paths"] + contract["temporary_directories"]):
                continue
            issues.append({"path": name, "reason": "preexisting_outside_scope_changed" if old is not None else "undeclared_residue"})
        status = "rejected" if issues else "accepted"
    receipt = {"schema_version": 1, "scope": "candidate_physical_delta_v1",
        "mission_id": baseline["mission_id"], "base_sha": baseline["base_sha"],
        "contract_sha256": digest(contract), "baseline_artifact_id": digest(baseline),
        "observed_inventory": observed, "final_tree_sha": tree_sha,
        "status": status, "issues": issues,
        "temporary_paths": sorted(p for p in after["entries"] if is_temporary(p, contract)),
        "delivery_paths": delivery_paths(baseline, observed, contract)}
    if status == "accepted" and tree is not None:
        expected = {p: after["entries"][p] for p in receipt["delivery_paths"]}
        actual = {}
        with tarfile.open(fileobj=io.BytesIO(tree), mode="r:") as archive:
            for member in archive:
                if member.isdir():
                    continue
                if not member.isfile() or member.name in actual:
                    raise ScopeError("scope tree contains nonregular or duplicate member")
                path(member.name)
                raw = archive.extractfile(member).read(contract["max_bytes"] + 1)
                actual[member.name] = (hashlib.sha256(raw).hexdigest(), len(raw), bool(member.mode & 0o111))
        wanted = {p: (e["sha256"], e["bytes"], bool(e["mode"] & 0o111)) for p, e in expected.items()}
        if actual != wanted:
            receipt.update(status="rejected", issues=[{"path": None, "reason": "tree_inventory_mismatch"}])
    return receipt


def require_accepted(runs, mid, receipt):
    if receipt["status"] != "accepted":
        stored = fleet_artifacts.put_bytes(runs, mid, fleet_json.canonical_bytes(receipt))
        raise ScopeRejected({**receipt, "receipt_artifact_id": stored["artifact_id"]})


def establish(runs, current, candidate, contract, tracked_paths):
    """Pin a complete baseline once, before any admission or agent boot."""
    if current.get("herdr_scope_baseline"):
        return load_baseline(runs, current, candidate, contract)
    inventory = capture(candidate, contract)
    baseline = {"schema_version": 1, "mission_id": current["mission_id"],
        "compiled_digest": current["compiled_digest"], "base_sha": current["base_sha"],
        "contract_sha256": digest(contract), "inventory": inventory,
        "tracked_paths": sorted(tracked_paths)}
    # A failed initial capture has no usable tracked manifest yet.
    if not inventory["complete"]:
        baseline["tracked_paths"] = []
    require_accepted(runs, current["mission_id"], evaluate(baseline, inventory, contract))
    stored = fleet_artifacts.put_bytes(runs, current["mission_id"], fleet_json.canonical_bytes(baseline))
    state.append_event(runs, current["mission_id"], kind=BASELINE_EVENT, actor="CONTROL",
        idempotency_key="herdr:scope:baseline", payload={"compiled_digest": current["compiled_digest"],
            "contract_sha256": digest(contract), "baseline_artifact_id": stored["artifact_id"]})
    return baseline


def verify_baseline(current, contract, baseline, candidate):
    pin = current.get("herdr_scope_baseline")
    if (not pin or pin["contract_sha256"] != current.get(FIELD)
            or pin["baseline_artifact_id"] != digest(baseline)
            or set(baseline) != {"schema_version", "mission_id", "compiled_digest", "base_sha", "contract_sha256", "inventory", "tracked_paths"}
            or type(baseline["schema_version"]) is not int or baseline["schema_version"] != 1
            or any(baseline.get(k) != current.get(k) for k in ("mission_id", "compiled_digest", "base_sha"))
            or baseline["contract_sha256"] != digest(contract)
            or baseline["inventory"]["root"] != str(candidate)):
        raise ScopeError("scope baseline binding mismatch")
    initial = evaluate(baseline, baseline["inventory"], contract)
    if initial["status"] != "accepted":
        raise ScopeError("scope baseline is not complete or contains preexisting temporary paths")


def load_baseline(runs, current, candidate, contract):
    pin = current.get("herdr_scope_baseline")
    if not pin:
        raise ScopeError("scope baseline missing before candidate acceptance")
    baseline = fleet_json.loads(fleet_artifacts.get_bytes(runs, current["mission_id"], pin["baseline_artifact_id"]))
    verify_baseline(current, contract, baseline, candidate)
    return baseline


def inspect(runs, current, candidate, contract, *, tree_sha=None, tree=None):
    baseline = load_baseline(runs, current, candidate, contract)
    receipt = evaluate(baseline, capture(candidate, contract), contract, tree_sha=tree_sha, tree=tree)
    require_accepted(runs, current["mission_id"], receipt)
    return baseline, receipt


def verify_archived(current, options, frozen, baseline, receipt, tree):
    contract = validate_binding(current, options)
    if contract is None:
        raise ScopeError("scope archive requires its creation contract")
    verify_baseline(current, contract, baseline, frozen["candidate_repo"])
    if frozen.get(FIELD) != digest(contract) or frozen.get("scope_baseline_artifact_id") != digest(baseline):
        raise ScopeError("scope freeze binding mismatch")
    expected = evaluate(baseline, receipt["observed_inventory"], contract, tree_sha=frozen["tree_sha"], tree=tree)
    if receipt != expected or expected["status"] != "accepted":
        raise ScopeError("scope receipt does not accept the frozen tree")
    return receipt
