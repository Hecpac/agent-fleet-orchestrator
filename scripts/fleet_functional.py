"""Versioned controller-owned functional contracts, attempts and offline receipts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import fleet_artifacts
import fleet_json
import fleet_mission_state as state
import fleet_safe_paths
import fleet_functional_runner as runner

STATUSES = {"passed", "failed", "blocked", "indeterminate"}
SHA = re.compile(r"[0-9a-f]{64}")
RUNTIME_FIELDS = {"image_id", "python_version", "engine_version", "architecture", "os", "dependencies",
                  "docker_endpoint_sha256", "controller_sha256", "guest_sha256", "controller_python"}


class FunctionalError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(fleet_json.canonical_bytes(value)).hexdigest()


def validate(spec):
    if (not isinstance(spec, dict) or set(spec) != {"schema_version", "check_id", "tests", "runtime", "argv", "cwd", "environment", "profile", "limits", "source_policy"}
            or type(spec["schema_version"]) is not int or spec["schema_version"] != 1
            or spec["check_id"] != runner.CHECK or spec["argv"] != runner.ARGV
            or spec["cwd"] != "/candidate" or spec["environment"] != runner.ENV or spec["profile"] != runner.PROFILE
            or spec["source_policy"] != runner.SOURCE_POLICY):
        raise FunctionalError("unsupported functional contract, check, argv or environment")
    tests = spec["tests"]
    if (not isinstance(tests, dict) or set(tests) != {"path", "sha256"}
            or tests["sha256"] != runner.TEST_SHA or not isinstance(tests["path"], str)):
        raise FunctionalError("functional tests must be the canonical original stats suite")
    p = PurePosixPath(tests["path"])
    if not p.is_absolute() or str(p) != tests["path"] or ".." in p.parts or "\x00" in tests["path"]:
        raise FunctionalError("tests path must be canonical and absolute")
    runtime = spec["runtime"]
    if (not isinstance(runtime, dict) or set(runtime) != RUNTIME_FIELDS
            or any(not isinstance(v, str) or not v or len(v) > 256 for v in runtime.values())
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", runtime["image_id"])
            or any(not SHA.fullmatch(runtime[k]) for k in ("docker_endpoint_sha256", "controller_sha256", "guest_sha256"))
            or runtime["dependencies"] != "stdlib-only"):
        raise FunctionalError("functional runtime identity is incomplete")
    limits = spec["limits"]
    if (not isinstance(limits, dict) or set(limits) != set(runner.LIMITS)
            or any(type(limits[k]) is not type(v) or (limits[k] != v if k != "wall_seconds" else not 1 <= limits[k] <= 30)
                   for k, v in runner.LIMITS.items())):
        raise FunctionalError("functional isolation limits cannot be weakened")
    return spec


def load(path):
    return validate(fleet_json.loads(fleet_artifacts.read_regular(Path(path))))


def make_spec(tests, image="python:3.12-slim"):
    return validate({"schema_version": 1, "check_id": runner.CHECK,
        "tests": {"path": str(Path(tests).resolve()), "sha256": runner.TEST_SHA},
        "runtime": runner.Docker().runtime(image), "argv": runner.ARGV, "cwd": "/candidate",
        "environment": runner.ENV, "profile": runner.PROFILE, "source_policy": runner.SOURCE_POLICY, "limits": dict(runner.LIMITS)})


def freeze_policy(runs, mid, compiled_digest, spec):
    spec = validate(spec)
    stored = fleet_artifacts.put_bytes(runs, mid, fleet_json.canonical_bytes(spec))
    return state.append_event(runs, mid, kind="functional_policy_frozen", actor="CONTROL",
        idempotency_key="functional:policy", payload={"compiled_digest": compiled_digest,
            "spec_artifact_id": stored["artifact_id"], "tests_sha256": spec["tests"]["sha256"]})[0]


def contract_for(mid, frozen, spec):
    spec = validate(spec)
    environment = {"runtime": spec["runtime"], "profile": spec["profile"], "source_policy": spec["source_policy"], "limits": spec["limits"],
                   "environment": spec["environment"], "argv": spec["argv"], "cwd": spec["cwd"]}
    contract = {"schema_version": 1, "scope": "canonical_stats_rpc_functional_tests", "mission_id": mid,
        "candidate_identity_sha256": digest(frozen.get("candidate_repo")),
        "spec_sha256": digest(spec), "tree_sha": frozen["tree_sha"], "tree_artifact_id": frozen["tree_artifact_id"],
        "tests_sha256": spec["tests"]["sha256"], "check_id": spec["check_id"], "environment": environment,
        "environment_sha256": digest(environment)}
    contract["attempt_id"] = str(uuid.uuid5(uuid.UUID(mid), "functional:" + digest(contract)))
    return contract


def verify_receipt(contract, receipt, read):
    """Offline integrity/provenance validation; never claims to rerun tests."""
    expected = {"schema_version", "scope", "contract_sha256", "mission_id", "tree_sha", "tests_sha256",
                "environment_sha256", "attempt_id", "status", "reason", "evidence"}
    if (not isinstance(receipt, dict) or set(receipt) != expected
            or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
            or receipt["scope"] != "fleet_functional_execution_v1" or receipt["contract_sha256"] != digest(contract)
            or any(receipt[k] != contract[k] for k in ("mission_id", "tree_sha", "tests_sha256", "environment_sha256", "attempt_id"))
            or receipt["status"] not in STATUSES or not isinstance(receipt["reason"], str)
            or not isinstance(receipt["evidence"], dict)):
        raise FunctionalError("functional receipt contract/tree/environment/attempt binding mismatch")
    evidence = {}
    for name, identifier in receipt["evidence"].items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z-]+\.(json|txt)", name) or not isinstance(identifier, str) or not SHA.fullmatch(identifier):
            raise FunctionalError("invalid functional evidence reference")
        raw = read(identifier)
        if hashlib.sha256(raw).hexdigest() != identifier:
            raise FunctionalError("functional evidence CAS mismatch")
        evidence[name] = raw
    if receipt["status"] == "passed":
        try:
            runner.check_bridge_source(read(contract["tree_artifact_id"]))
        except (runner.RunnerBlocked, KeyError) as exc:
            raise FunctionalError("passed receipt lacks a protocol-compatible candidate source") from exc
        required = {"test-result.json", "test-output.txt", "stdout.txt", "stderr.txt", "container-config.json", "container-state.json"}
        if set(evidence) != required:
            raise FunctionalError("passed receipt lacks controller/runtime evidence")
        result = fleet_json.loads(evidence["test-result.json"])
        if result != {"tests_run": 5, "failures": 0, "errors": 0, "passed": True}:
            raise FunctionalError("passed receipt lacks five successful original tests")
        ended = fleet_json.loads(evidence["container-state.json"])
        if ended.get("ExitCode") != 0 or ended.get("Running") is not False or ended.get("OOMKilled"):
            raise FunctionalError("passed receipt has incompatible runtime completion")
        lines = evidence["stdout.txt"].splitlines()
        if len(lines) != 2:
            raise FunctionalError("passed receipt lacks exact RPC exchange")
        # Validate retained effective limits using the pinned execution contract.
        try:
            runner.check_hello(fleet_json.loads(lines[0]), {**contract["environment"]})
            config = fleet_json.loads(evidence["container-config.json"])
            sources = {m["Destination"]: Path(m["Source"]) for m in config["Mounts"] if m["Type"] == "bind"}
            if config["Config"].get("Labels", {}).get("fleet.functional.attempt") != contract["attempt_id"]:
                raise FunctionalError("runtime evidence belongs to another attempt")
            runner.check_config(config, contract["environment"], "fleet-functional-" + contract["attempt_id"],
                                sources["/candidate"], sources["/harness"])
        except (runner.RunnerBlocked, KeyError, TypeError) as exc:
            raise FunctionalError("functional runtime provenance is incompatible") from exc
    return receipt


def load_policy(runs, mid):
    import fleet_mission
    current = fleet_mission.load_state(runs, mid)
    policy = current.get("functional_policy")
    if not policy:
        return current, None
    spec = validate(fleet_json.loads(fleet_artifacts.get_bytes(runs, mid, policy["spec_artifact_id"])))
    if digest(spec) != policy["spec_artifact_id"] or spec["tests"]["sha256"] != policy["tests_sha256"]:
        raise FunctionalError("functional policy CAS mismatch")
    return current, spec


def bind_attempt(runs, mid, frozen):
    """Bind the frozen tree to its functional contract; returns (current, spec, contract, id)."""
    current, spec = load_policy(runs, mid)
    if spec is None:
        raise FunctionalError("functional policy is required before execution")
    import fleet_archive_tree
    tree = fleet_artifacts.get_bytes(runs, mid, frozen["tree_artifact_id"])
    if (frozen.get("mission_id") != mid or frozen.get("compiled_digest") != current["compiled_digest"]
            or fleet_archive_tree.tree_hash_from_tar(tree, "sha1" if len(frozen["tree_sha"]) == 40 else "sha256") != frozen["tree_sha"]):
        raise FunctionalError("functional frozen tree binding mismatch")
    contract = contract_for(mid, frozen, spec)
    return current, spec, contract, digest(contract)


def attempt_keys(current):
    """Idempotency keys of the functional check for the open attempt.

    Missions without a repair policy, and a repair Mission's first attempt,
    keep the historical keys; later repair attempts are keyed by ordinal.
    """
    attempts = current.get("repair_attempts")
    if attempts is None:
        return "functional:started", "functional:finished"
    if not attempts or attempts[-1]["settled"] is not None:
        raise FunctionalError("functional execution requires an open repair attempt")
    suffix = "" if attempts[-1]["ordinal"] == 1 else f":repair-{attempts[-1]['ordinal']}"
    return "functional:started" + suffix, "functional:finished" + suffix


def run(runs, mid, frozen, *, interrupt=None):
    current, spec, contract, identifier = bind_attempt(runs, mid, frozen)
    started_key, finished_key = attempt_keys(current)
    read = lambda key: fleet_artifacts.get_bytes(runs, mid, key)
    prior = current.get("functional_attempt")
    def stop_requested():
        if interrupt and (reason := interrupt()):
            return reason
        latest, _ = load_policy(runs, mid)
        if latest.get("herdr_control", {}).get("desired") == "cancel_requested":
            return "mission_cancel_requested"
        if datetime.now(timezone.utc) >= state.parse_timestamp(latest["admission_policy"]["deadline_at"], "functional deadline"):
            return "mission_deadline_expired"
        return None
    if prior:
        if prior["contract_artifact_id"] != identifier or prior["attempt_id"] != contract["attempt_id"]:
            raise FunctionalError("functional attempt cannot switch tree/environment or silently retry")
        if prior.get("result"):
            return verify_receipt(contract, fleet_json.loads(read(prior["result"]["receipt_artifact_id"])), read)
        outcome = {"status": "indeterminate", "reason": "interrupted_attempt_not_replayed", "evidence": {}}
        try:
            docker = runner.Docker()
            if runner.sha(docker.endpoint.encode()) != spec["runtime"]["docker_endpoint_sha256"]:
                raise runner.RunnerBlocked("functional cleanup endpoint changed")
            if not docker.cleanup("fleet-functional-" + contract["attempt_id"], contract["attempt_id"]):
                outcome["reason"] = "interrupted_attempt_cleanup_unconfirmed"
        except (runner.RunnerBlocked, OSError, subprocess.TimeoutExpired):
            outcome["reason"] = "interrupted_attempt_cleanup_unconfirmed"
    else:
        fleet_artifacts.put_bytes(runs, mid, fleet_json.canonical_bytes(contract))
        state.append_event(runs, mid, kind="functional_check_started", actor="CONTROL", idempotency_key=started_key,
            payload={"contract_artifact_id": identifier, "attempt_id": contract["attempt_id"], "tree_sha": frozen["tree_sha"]})
        try:
            tests_path = Path(spec["tests"]["path"])
            candidate = Path(frozen["candidate_repo"])
            if tests_path.resolve().is_relative_to(candidate.resolve()) or tests_path.is_symlink():
                raise runner.RunnerBlocked("tests_must_be_external_to_candidate")
            tests = fleet_artifacts.read_regular(tests_path)
            if hashlib.sha256(tests).hexdigest() != spec["tests"]["sha256"]:
                raise runner.RunnerBlocked("original_tests_changed")
            fleet_artifacts.put_bytes(runs, mid, tests)
            outcome = runner.execute(spec, read(frozen["tree_artifact_id"]), tests, contract["attempt_id"], interrupt=stop_requested)
        except (runner.RunnerBlocked, fleet_artifacts.ArtifactError, OSError) as exc:
            outcome = {"status": "blocked", "reason": str(exc), "evidence": {}}
    receipt = {"schema_version": 1, "scope": "fleet_functional_execution_v1", "contract_sha256": identifier,
        **{k: contract[k] for k in ("mission_id", "tree_sha", "tests_sha256", "environment_sha256", "attempt_id")},
        "status": outcome["status"], "reason": outcome["reason"],
        "evidence": {name: fleet_artifacts.put_bytes(runs, mid, raw)["artifact_id"] for name, raw in outcome["evidence"].items()}}
    verify_receipt(contract, receipt, read)
    stored = fleet_artifacts.put_bytes(runs, mid, fleet_json.canonical_bytes(receipt))
    state.append_event(runs, mid, kind="functional_check_finished", actor="CONTROL", idempotency_key=finished_key,
        payload={"contract_artifact_id": identifier, "attempt_id": contract["attempt_id"],
                 "receipt_artifact_id": stored["artifact_id"], "status": receipt["status"]})
    return receipt


def archived_receipt(current, frozen, read):
    policy = current.get("functional_policy")
    if not policy:
        return None
    spec = validate(fleet_json.loads(read(policy["spec_artifact_id"])))
    if digest(spec) != policy["spec_artifact_id"]:
        raise FunctionalError("archived functional specification mismatch")
    contract = contract_for(current["mission_id"], frozen, spec)
    attempt = current.get("functional_attempt")
    if not attempt or not attempt.get("result") or attempt["contract_artifact_id"] != digest(contract):
        raise FunctionalError("archive lacks a bound functional attempt result")
    if fleet_json.loads(read(attempt["contract_artifact_id"])) != contract:
        raise FunctionalError("archived execution contract differs")
    receipt = verify_receipt(contract, fleet_json.loads(read(attempt["result"]["receipt_artifact_id"])), read)
    if receipt["status"] != attempt["result"]["status"] or receipt["status"] != "passed":
        raise FunctionalError("functional contract did not pass")
    if hashlib.sha256(read(contract["tests_sha256"])).hexdigest() != contract["tests_sha256"]:
        raise FunctionalError("archived canonical tests mismatch")
    return receipt


def report(runs, mid, current):
    if not current.get("functional_policy"):
        return None
    attempt = current.get("functional_attempt")
    if not attempt or not attempt.get("result"):
        return {"required": True, "status": "indeterminate" if attempt else "pending",
                "reason": "no_durable_functional_result"}
    read = lambda key: fleet_artifacts.get_bytes(runs, mid, key)
    contract = fleet_json.loads(read(attempt["contract_artifact_id"]))
    receipt = verify_receipt(contract, fleet_json.loads(read(attempt["result"]["receipt_artifact_id"])), read)
    if (contract["spec_sha256"] != current["functional_policy"]["spec_artifact_id"]
            or receipt["status"] != attempt["result"]["status"]):
        raise FunctionalError("reported functional result differs from ledger policy")
    return {"required": True, **{k: receipt[k] for k in ("status", "reason", "attempt_id", "tree_sha", "scope")},
            "receipt_sha256": attempt["result"]["receipt_artifact_id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    spec = commands.add_parser("spec")
    spec.add_argument("--tests", required=True, type=Path)
    spec.add_argument("--image", default="python:3.12-slim")
    execute = commands.add_parser("run")
    execute.add_argument("--runs-dir", required=True, type=Path)
    execute.add_argument("--mission-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "spec":
            result = make_spec(args.tests, args.image)
        else:
            import fleet_herdr_mission
            with fleet_safe_paths.RootedFS(args.runs_dir) as fs:
                mid = state.normalize_uuid(args.mission_id, "mission_id")
                with fs.exclusive_lock(Path("missions") / mid / "herdr-driver.lock", directory_modes=(0o700, 0o700), blocking=False) as acquired:
                    if not acquired:
                        raise FunctionalError("mission driver is busy")
                    driver = fleet_herdr_mission._Driver(args.runs_dir, mid)
                    frozen = driver.read(fleet_herdr_mission.freeze_name(driver.current()))
                    result = run(args.runs_dir, mid, frozen)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("status", "passed") == "passed" else 3
    except (FunctionalError, runner.RunnerBlocked, RuntimeError, OSError) as exc:
        print(f"fleet-functional: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
