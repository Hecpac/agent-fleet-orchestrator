#!/usr/bin/env python3
"""Deterministic artifact acceptance against an immutable Git tree.

These predicates verify the declared artifact contract, not arbitrary program
behavior or the truth of a model-authored test report. No candidate code runs.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile

import fleet_json

MAX_BYTES = 32 * 1024 * 1024


class AcceptanceError(ValueError):
    pass


def digest(value: object) -> str:
    return hashlib.sha256(fleet_json.canonical_bytes(value)).hexdigest()


def bound_key(base_key: str, contract: dict) -> str:
    return "mission-b:" + digest(base_key) + ":acceptance:" + digest(validate(contract))


def check_binding(key: str, contract: dict | None) -> None:
    if key.startswith("mission-b:"):
        if contract is None or key.rsplit(":acceptance:", 1)[-1] != digest(validate(contract)):
            raise AcceptanceError("acceptance contract missing or changed since mission creation")
    elif contract is not None:
        raise AcceptanceError("acceptance cannot be added to a legacy mission")


def validate(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema_version", "requirements"}:
        raise AcceptanceError("contract requires schema_version and requirements only")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise AcceptanceError("unsupported acceptance schema")
    requirements = value["requirements"]
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 100:
        raise AcceptanceError("contract requires 1..100 requirements")
    ids = set()
    for item in requirements:
        if not isinstance(item, dict) or set(item) != {"id", "description", "checks"}:
            raise AcceptanceError("invalid requirement fields")
        name = item["id"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) or name in ids:
            raise AcceptanceError("requirement IDs must be unique and nonempty")
        ids.add(name)
        if not isinstance(item["description"], str) or not item["description"].strip():
            raise AcceptanceError("requirement description is empty")
        checks = item["checks"]
        if not isinstance(checks, list) or not 1 <= len(checks) <= 100:
            raise AcceptanceError("each requirement needs 1..100 checks")
        for check in checks:
            if not isinstance(check, dict):
                raise AcceptanceError("check must be an object")
            kind = check.get("kind")
            fields = {"kind", "path", "expected"}
            if kind == "json_equals":
                fields.add("keys")
            if not isinstance(kind, str) or kind not in {"text_contains", "sha256", "json_equals"} or set(check) != fields:
                raise AcceptanceError("unknown check kind or fields")
            path = check["path"]
            if (not isinstance(path, str) or not path or "\x00" in path
                    or PurePosixPath(path).is_absolute() or ".." in path.split("/")
                    or str(PurePosixPath(path)) != path or path == "."):
                raise AcceptanceError("artifact path must be a canonical relative path")
            expected = check["expected"]
            if kind == "text_contains" and (not isinstance(expected, str) or not expected.strip()):
                raise AcceptanceError("text predicate must not be empty")
            if kind == "sha256" and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
                raise AcceptanceError("invalid expected sha256")
            if kind == "json_equals" and (not isinstance(check["keys"], list)
                    or any(not isinstance(key, str) for key in check["keys"])):
                raise AcceptanceError("JSON keys must be an array of object keys")
    fleet_json.canonical_bytes(value)
    return value


def load(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise AcceptanceError("contract exceeds 1 MiB")
    return validate(fleet_json.loads(raw))


def evaluate(contract: dict, tree: bytes, *, mission_id: str, final_sha: str) -> dict:
    contract = validate(contract)
    if len(tree) > MAX_BYTES:
        raise AcceptanceError("tree exceeds acceptance size limit")
    wanted = {check["path"] for item in contract["requirements"] for check in item["checks"]}
    files = {}
    seen = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(tree), mode="r:") as archive:
            for member in archive:
                if member.name in seen:
                    raise AcceptanceError("duplicate tree entry")
                seen.add(member.name)
                if member.name not in wanted:
                    continue
                if not member.isfile() or member.size > MAX_BYTES:
                    raise AcceptanceError("required artifact is not a bounded regular file")
                stream = archive.extractfile(member)
                if stream is None:
                    raise AcceptanceError("required artifact is unreadable")
                files[member.name] = stream.read(MAX_BYTES + 1)
    except tarfile.TarError as exc:
        raise AcceptanceError("invalid final tree") from exc
    results = []
    for item in contract["requirements"]:
        checks = []
        for check in item["checks"]:
            raw = files.get(check["path"])
            passed = False
            reason = "artifact_missing"
            if raw is not None:
                reason = "predicate_mismatch"
                try:
                    if check["kind"] == "sha256":
                        passed = hashlib.sha256(raw).hexdigest() == check["expected"]
                    elif check["kind"] == "text_contains":
                        passed = check["expected"] in raw.decode("utf-8")
                    else:
                        actual = fleet_json.loads(raw)
                        for key in check["keys"]:
                            if not isinstance(actual, dict) or key not in actual:
                                raise KeyError(key)
                            actual = actual[key]
                        passed = fleet_json.canonical_bytes(actual) == fleet_json.canonical_bytes(check["expected"])
                except (UnicodeError, fleet_json.FleetJSONError, KeyError):
                    reason = "invalid_artifact_value"
            checks.append({"path": check["path"], "kind": check["kind"],
                           "passed": passed, "reason": "matched" if passed else reason,
                           "artifact_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None})
        results.append({"id": item["id"], "passed": all(c["passed"] for c in checks), "checks": checks})
    return {"schema_version": 1, "mission_id": mission_id, "final_sha": final_sha,
            "contract_sha256": digest(contract), "tree_sha256": hashlib.sha256(tree).hexdigest(),
            "status": "accepted" if all(r["passed"] for r in results) else "rejected",
            "scope": "declared_artifact_predicates", "requirements": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--tree", type=Path, help="Git archive tar; local predicate smoke only")
    args = parser.parse_args()
    try:
        contract = load(args.contract)
        if args.tree is None:
            result = {"valid": True, "contract_sha256": digest(contract)}
        else:
            with args.tree.open("rb") as stream:
                tree = stream.read(MAX_BYTES + 1)
            result = evaluate(contract, tree, mission_id="standalone", final_sha="unbound")
        print(json.dumps(result, sort_keys=True))
        return 1 if result.get("status") == "rejected" else 0
    except (AcceptanceError, fleet_json.FleetJSONError, OSError) as exc:
        print(f"fleet-acceptance: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
