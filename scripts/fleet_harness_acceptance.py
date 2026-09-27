"""Versioned D1/D2 oracle. CONTROL compares data; never imports candidates.

The suite is a controller-only artifact, not a WorkPacket field. Cases revealed
for repair are public. Reserved cases are neither mounted nor included in
feedback. General Python requires independent, hash-bound source admission;
this checker does not claim a blacklist makes arbitrary Python trustworthy.
"""
from __future__ import annotations

import copy
from contextlib import nullcontext
import base64
import hashlib
import itertools
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
import time

import fleet_json
import fleet_functional_runner as legacy
from fleet_harness_sandbox import Sandbox, SandboxError, IMAGE, digest, publish

VERSION = "d1d2-contract-rpc-v1"
GUEST = Path(__file__).with_name("fleet_harness_guest.py")
SOURCE_POLICY = "independent-source-admission-v1"


def pin(value):
    return digest(fleet_json.canonical_bytes(value))


def expected_receipt(path, content, generation):
    return {"path": path, "sha256": digest(content.encode()), "generation": generation}


def row(run="r", **changes):
    return {"mission_id": "m", "run_id": run, "sequence": 0, "provider": "p", "model": "model",
            "variant": None, "prompt_tokens": 1, "completion_tokens": 1, "cached_input_tokens": 1, **changes}


def summary(*, runs=1, observed=1, missing=0, prompt=1, completion=1, cached=1, known=1, groups=None):
    group = {"provider": "p", "model": "model", "variant": None, "runs": observed,
             "known_runs": known, "prompt_tokens": prompt, "completion_tokens": completion,
             "cached_input_tokens": cached}
    return {"total_runs": runs, "observed_runs": observed, "missing_runs": missing,
            "groups": [group] if groups is None else groups}


def public_cases(task):
    """Explicit expectations, including invalid types and genuinely conflicting ties."""
    cases = []
    def add(name, family, request, value=None, errors=None, **checks):
        cases.append({"id": name, "family": family, "visibility": "public", "request": request,
                      "expected": {"value": value, "errors": errors, **checks}})
    if task == "D2":
        def usage(name, family, rows, value=None, errors=None, admitted=None, mappings=False):
            add(name, family, {"op": "usage", "records": rows, "mission": "m",
                "admitted": ["r"] if admitted is None else admitted, "mappings": mappings}, value, errors)
        usage("regular", "aggregation", [row()], summary())
        usage("sum-complete-runs", "aggregation", [row(prompt_tokens=3, completion_tokens=2),
              row("s", prompt_tokens=7, completion_tokens=5, cached_input_tokens=2)],
              summary(runs=2, observed=2, known=2, prompt=10, completion=7, cached=3), admitted=["r", "s"])
        usage("empty", "selection", [], summary(runs=0, observed=0, missing=0, groups=[]), admitted=[])
        usage("missing", "selection", [], summary(runs=1, observed=0, missing=1, groups=[]))
        usage("filter-first", "selection", [row(mission_id="foreign", sequence=True),
            row(run="unadmitted", provider=[]), row()], summary(runs=2, missing=1),
            admitted=["r", "r", "missing"], mappings=True)
        usage("filter-unhashable-run", "selection", [{"mission_id": "m", "run_id": []},
            {"mission_id": "m", "run_id": {}}], summary(runs=1, observed=0, missing=1, groups=[]))
        for field in ("prompt_tokens", "completion_tokens", "cached_input_tokens"):
            usage("missing-" + field, "unknown-propagation", [{k: v for k, v in row().items() if k != field}],
                  summary(cached=None) if field == "cached_input_tokens" else summary(prompt=None, completion=None, cached=None, known=0))
            for invalid in (True, 1.0, "1", -1):
                usage(f"single-type-{field}-{type(invalid).__name__}", "latest-types", [row(**{field: invalid})], errors=["ValueError"])
                usage(f"old-type-{field}-{type(invalid).__name__}", "global-maximum",
                      [row(**{field: invalid}), row(sequence=1)], summary())
                for order, pair in enumerate(([row(), row(**{field: invalid})], [row(**{field: invalid}), row()])):
                    usage(f"type-{field}-{type(invalid).__name__}-{order}", "latest-types", pair, errors=["ValueError"])
        for index, records in enumerate(itertools.permutations([row(prompt_tokens=1), row(prompt_tokens=2),
                                                               row(sequence=1, prompt_tokens=3)])):
            usage(f"superseded-tie-{index}", "global-maximum", list(records), summary(prompt=3))
        for records in ([row(), row(extra=1)], [row(extra=1), row(extra=2)],
                        [row(), {k: v for k, v in row().items() if k != "variant"}]):
            for ordered in (records, list(reversed(records))):
                usage("mapping-tie-" + str(len(cases)), "mapping-equality", ordered, errors=["ValueError"])
        usage("unknown-latest", "unknown-propagation", [row(prompt_tokens=100), row(sequence=1, prompt_tokens=None)],
              summary(prompt=None, completion=None, cached=None, known=0))
        usage("incomplete-group", "unknown-propagation", [row(), row("s", completion_tokens=None)],
              summary(runs=2, observed=2, prompt=None, completion=None, cached=None), admitted=["r", "s"])
        usage("zeros", "aggregation", [row(prompt_tokens=0, completion_tokens=0, cached_input_tokens=0)],
              summary(prompt=0, completion=0, cached=0))
        usage("missing-cache", "aggregation", [row(cached_input_tokens=None)], summary(cached=None))
        usage("cache-subset", "aggregation", [row(cached_input_tokens=2)], errors=["ValueError"])
        for field in ("provider", "model", "variant"):
            usage("identity-drift-" + field, "identity", [row(**{field: "changed"}), row(sequence=1)], errors=["ValueError"])
            for index, invalid in enumerate(("", 1, True, [], {}) + (() if field == "variant" else (None,))):
                usage(f"invalid-identity-{field}-{index}", "identity", [row(**{field: invalid})], errors=["ValueError"])
        for invalid in (True, 1.0, -1, None):
            usage("sequence-" + type(invalid).__name__, "sequence", [row(sequence=invalid)], errors=["ValueError"])
        identities = [("z", "a", None), ("a", "z", None), ("a", "a", "high"), ("a", "a", None), ("a", "a", "low")]
        records = [row(str(i), provider=p, model=m, variant=v) for i, (p, m, v) in enumerate(identities)]
        groups = [dict(summary()["groups"][0], provider=p, model=m, variant=v)
                  for p, m, v in sorted(identities, key=lambda x: (x[0], x[1], x[2] is not None, x[2] or ""))]
        usage("identity-sort", "structure", records, summary(runs=5, observed=5, groups=groups), admitted=[str(i) for i in range(5)])
    elif task == "D1":
        def reset(name):
            add(name, "setup", {"op": "reset"})
        def record(name, args, value=None, errors=None, **kw):
            partial = kw.pop("partial_write", False)
            family = kw.pop("family", "ledger")
            add(name, family, {"op": "record", "args": args, "partial_write": partial}, value, errors, **kw)
        reset("start")
        first = expected_receipt("a", "before", 0)
        record("initial", ["r", 0, "a", "before"], first, files={"root/a": digest(b"before")})
        add("try-receipt-mutation", "immutability", {"op": "mutate_returned"}, {"path": True, "sha256": True, "generation": True})
        add("receipt-stays-immutable", "immutability", {"op": "latest", "run": "r"}, first)
        record("replay", ["r", 0, "a", "before"], first, same_fs=True, family="idempotence")
        record("same-path-different-content", ["r", 0, "a", "different"], errors=["ValueError"], same_fs=True, family="generations")
        record("different-path-same-content", ["r", 0, "other", "before"], errors=["ValueError"], same_fs=True, family="generations")
        record("partial-update", ["r", 1, "a", "long replacement"], errors=["OSError", "ValueError"],
               partial_write=True, same_fs=True, family="atomicity")
        add("receipt-after-failure", "atomicity", {"op": "latest", "run": "r"}, first)
        record("retry", ["r", 1, "a", "retry"], expected_receipt("a", "retry", 1), files={"root/a": digest(b"retry")}, family="atomicity")
        for generation in (0, 1):
            record(f"reject-generation-{generation}", ["r", generation, "new/nested/a", "conflict"],
                   errors=["ValueError"], same_fs=True, family="generations")
        record("move", ["r", 2, "b", "new"], expected_receipt("b", "new", 2))
        record("old-owner", ["s", 0, "a", "stolen"], errors=["ValueError"], same_fs=True, family="ownership")
        for i, generation in enumerate((True, 1.0, -1, {"constructor": "int_subclass", "value": 0}, {"constructor": "int_enum", "value": 0})):
            record(f"generation-type-{i}", ["type", generation, "types/new", "x"], errors=["ValueError"], same_fs=True, family="exact-types")
        for i, path in enumerate(("", "/tmp/a", "a//b", "./x", "a/../b", "a/./b", "a\\b", ".git/x", "a/.git/x")):
            record(f"path-{i}", ["path", 0, path, "x"], errors=["ValueError"], same_fs=True, family="paths")
        for i, args in enumerate((("", 0, "x", "x"), (1, 0, "x", "x"), ("r", 3, "x", 1))):
            record(f"identity-content-{i}", list(args), errors=["ValueError"], same_fs=True, family="exact-types")
        reset("unicode-reset")
        record("unicode", ["r", 0, "ñ/文", "é"], expected_receipt("ñ/文", "é", 0), files={"root/ñ/文": digest("é".encode())}, family="paths")
        add("replace-parent", "setup", {"op": "alter", "action": "parent_file", "path": "ñ"})
        record("replay-parent-file", ["r", 0, "ñ/文", "é"], errors=["ValueError"], same_fs=True, family="replay")
        for action in ("unlink", "write"):
            reset("drift-reset-" + action)
            record("drift-create-" + action, ["r", 0, "a", "before"], first)
            add("drift-alter-" + action, "setup", {"op": "alter", "action": action, "path": "a", "content": "tampered"})
            record("drift-replay-" + action, ["r", 0, "a", "before"], errors=["ValueError"], same_fs=True, family="replay")
        reset("links-reset")
        add("dangling-link", "setup", {"op": "alter", "action": "symlink", "path": "deep/link", "target": "/work/missing"})
        for path in ("deep/link", "deep/link/file"):
            record("link-" + path, ["r", 0, path, "x"], errors=["ValueError"], same_fs=True, family="symlinks")
        reset("new-write-reset")
        record("new-write-failure", ["r", 0, "new/nested/a", "long content"], errors=["OSError", "ValueError"], partial_write=True, family="failed-publication")
        add("new-write-unpublished", "failed-publication", {"op": "latest", "run": "r"})
        record("new-write-other-owner-retry", ["s", 0, "new/nested/a", "retry"], expected_receipt("new/nested/a", "retry", 0),
               files={"root/new/nested/a": digest(b"retry")}, family="failed-publication")
    else:
        raise ValueError("unsupported D1/D2 task")
    return cases


def suite(task, *, reserved=(), exposed_to=("maintainer", "independent-reviewer")):
    result = {"version": VERSION, "task": task, "public": public_cases(task), "reserved": list(reserved),
              "custody": {"visible_to": list(exposed_to), "worker_access": "forbidden",
                          "blind_to_maintainer": False, "repair_feedback": "public-only"}}
    validate_suite(result)
    return result


def validate_suite(value):
    if (not isinstance(value, dict) or set(value) != {"version", "task", "public", "reserved", "custody"}
            or value["version"] != VERSION or value["task"] not in {"D1", "D2"}
            or fleet_json.canonical_bytes(value["public"]) != fleet_json.canonical_bytes(public_cases(value["task"]))):
        raise ValueError("suite omits or changes required public contract cases")
    ids = set()
    for case in value["public"] + value["reserved"]:
        if (set(case) != {"id", "family", "visibility", "request", "expected"}
                or not isinstance(case["id"], str) or case["id"] in ids
                or case["visibility"] not in {"public", "reserved"}):
            raise ValueError("ambiguous acceptance case")
        ids.add(case["id"])
    if any(c["visibility"] != "reserved" for c in value["reserved"]):
        raise ValueError("private/public case confusion")
    custody=value["custody"]
    if (not isinstance(custody,dict) or set(custody)!={"visible_to","worker_access","blind_to_maintainer","repair_feedback"}
            or custody["worker_access"]!="forbidden" or custody["blind_to_maintainer"] is not False
            or custody["repair_feedback"]!="public-only" or not isinstance(custody["visible_to"],list)
            or not custody["visible_to"] or any(not isinstance(x,str) or not x for x in custody["visible_to"])):
        raise ValueError("private suite custody is not explicit")
    if not set(custody["visible_to"])<={"maintainer","independent-reviewer","CONTROL-verifier"}:
        raise ValueError("reserved cases exposed outside declared CONTROL roles")
    for case in value["reserved"]:
        if (not case["id"] or not isinstance(case["family"],str) or not case["family"]
                or not isinstance(case["request"],dict) or not isinstance(case["expected"],dict)
                or not {"value","errors"}<=set(case["expected"])):raise ValueError("invalid reserved probe")
    return value


def public_projection(value):
    validate_suite(value)
    return {"version": "owner-functional-public-v2", "check": VERSION, "task": value["task"],
            "public_cases": copy.deepcopy(value["public"]), "reserved_commitment": pin(value["reserved"]),
            "source_policy": SOURCE_POLICY, "custody": copy.deepcopy(value["custody"])}


def assess(case, response, before, after):
    expected, issues = case["expected"], []
    error = response["error"]
    if expected["errors"] is not None:
        if error not in expected["errors"]:
            issues.append("expected_error")
    elif error is not None or fleet_json.canonical_bytes(response["value"]) != fleet_json.canonical_bytes(expected["value"]):
        issues.append("value_or_type")
    if case["request"]["op"] == "usage":
        if fleet_json.canonical_bytes(response["input_after"]) != fleet_json.canonical_bytes(case["request"]["records"]):
            issues.append("input_mutated")
    if expected.get("same_fs") and before is not None and after is not None and before != after:
        issues.append("filesystem_changed")
    for name, sha in expected.get("files", {}).items():
        if after is None:continue  # An interrupted observation cannot prove PASS.
        if after.get(name, {}).get("kind") != "file" or after.get(name, {}).get("sha256") != sha:
            issues.append("file_content:" + name)
    return issues


def replay(value, observations, *, allow_prefix=False):
    validate_suite(value)
    cases = value["public"] + value["reserved"]
    if (not isinstance(observations, list) or len(observations)>len(cases)
            or not allow_prefix and len(observations) != len(cases)):
        raise ValueError("acceptance observation count differs")
    results, previous = [], {}
    for case, item in zip(cases, observations):
        if set(item) != {"id", "request", "response", "filesystem"} or item["id"] != case["id"]:
            raise ValueError("acceptance case binding differs")
        request = {**case["request"], "id": case["id"]}
        if fleet_json.canonical_bytes(item["request"]) != fleet_json.canonical_bytes(request):
            raise ValueError("acceptance request changed")
        response = item["response"]
        if (not isinstance(response, dict) or set(response) != {"id", "value", "error", "input_after"}
                or response["id"] != case["id"]):
            raise ValueError("acceptance response is not bound")
        issues = assess(case, response, previous, item["filesystem"])
        results.append({"id": case["id"], "family": case["family"], "visibility": case["visibility"],
                        "passed": not issues, "issues": issues})
        previous = item["filesystem"]
    return results


def public_feedback(value, results, observations):
    feedback = []
    for case, result, observation in zip(value["public"] + value["reserved"], results, observations):
        if not result["passed"] and case["visibility"] == "public":
            feedback.append({"id":case["id"], "family":case["family"], "request":case["request"],
                "expected":case["expected"], "observed":observation["response"], "issues":result["issues"]})
    return feedback[:10]


def source_manifest(candidate, task):
    return {name: digest(raw) for name, raw in source_bytes(candidate, task).items()}


def source_bytes(candidate, task):
    names = ("ledger.py", "paths.py") if task == "D1" else ("report.py",)
    from fleet_safe_paths import RootedFS
    with RootedFS(Path(candidate)) as fs:
        return {name: fs.read_regular(name, directory_modes=(), file_mode=0o644, max_bytes=4 * 1024 * 1024) for name in names}


def validate_source_admission(review, manifest):
    if (not isinstance(review, dict) or set(review) != {"version", "manifest", "reviewer", "scope", "accepted"}
            or review["version"] != SOURCE_POLICY or review["manifest"] != manifest
            or not isinstance(review["reviewer"], str) or not review["reviewer"].strip()
            or review["scope"] != "bridge-integrity-no-reflection-no-stdout-or-process-interference"
            or review["accepted"] is not True):
        raise ValueError("independent exact-source admission is required for general Python")
    return review


def run(candidate, value, store, *, binding, source_admission=None, deadline_at=None, cancelled=None, fault=None, recovery=()):
    """One bounded check; durable identity precedes each resource effect."""
    validate_suite(value)
    store = Path(store); store.mkdir(parents=True, mode=0o700, exist_ok=True)
    if (store / "contract.json").exists() or (store / "resource").exists():
        raise ValueError("existing check is immutable; reconcile its recorded resource")
    sources = source_bytes(candidate, value["task"])
    manifest = {name: digest(raw) for name, raw in sources.items()}
    contract = {"version": VERSION, "binding": binding, "sources": manifest, "suite_sha256": pin(value),
                "controller_sha256": digest(Path(__file__).read_bytes()), "guest_sha256": digest(GUEST.read_bytes()),
                "sandbox_sha256": digest(Path(__file__).with_name("fleet_harness_sandbox.py").read_bytes()),
                "image": IMAGE, "source_policy": SOURCE_POLICY, "source_admission": source_admission,
                "evidence_root": str(store.resolve()),"recovery_sha256":pin(list(recovery))}
    publish(store, "contract.json", contract)
    publish(store, "suite.json", value)
    publish(store,"recovery.json",list(recovery))
    observations, clean, error = [], None, None
    sandbox = None
    def remaining():
        if cancelled is not None and cancelled(): raise ValueError("acceptance cancelled")
        seconds = 8 if deadline_at is None else min(8,deadline_at-time.time())
        if seconds<=0: raise ValueError("shared acceptance deadline exhausted")
        return seconds
    try:
        remaining()
        sandbox = Sandbox(store / "resource", owner=str(uuid.uuid4()))
        # A dedicated bridge directory contains only this fixed RPC adapter.
        bridge = store / "bridge"; bridge.mkdir(mode=0o755)
        (bridge / GUEST.name).write_bytes(GUEST.read_bytes())
        (bridge / GUEST.name).chmod(0o644)
        # Only reviewed source bytes enter sys.path: no pyc, shadow modules,
        # symlinks or additional tree files can supply unreviewed code.
        staged = store / "sources"; staged.mkdir(mode=0o755)
        for name, raw in sources.items():
            (staged / name).write_bytes(raw); (staged / name).chmod(0o644)
        sandbox.create(candidate=staged, guest=bridge,
                       argv=["/usr/local/bin/python3", "-I", "-S", "-B", "/bridge/" + GUEST.name])
        remaining()
        sandbox.start()
        for case in value["public"] + value["reserved"]:
            request = {**case["request"], "id": case["id"]}
            response = sandbox.rpc(request,timeout=remaining())
            snapshot = sandbox.snapshot() if value["task"] == "D1" else {}
            observations.append({"id": case["id"], "request": request, "response": response, "filesystem": snapshot})
            publish(store, f"observations/{len(observations):04d}.json", observations[-1])
            if fault:fault("after_check_rpc")
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        error = type(exc).__name__ + ":" + str(exc)[:500]
    finally:
        if sandbox is not None and (store / "resource" / "resource.json").exists():
            try:
                clean = sandbox.cleanup()
                if clean["extra_stdout"] or not clean["bounded_output"]:
                    error = "unbound_or_excess_terminal_output"
            except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                error = "cleanup_unconfirmed:" + type(exc).__name__
    publish(store, "observations.json", observations)
    results = replay(value, observations) if len(observations) == len(value["public"] + value["reserved"]) else []
    pending = []
    try:
        validate_source_admission(source_admission, manifest)
    except ValueError:
        pending.append("independent_source_admission_required")
    if error or clean is None:
        pending.append(error or "cleanup_unconfirmed")
    failed = [r for r in results if not r["passed"]]
    status = "blocked" if not results or (pending and not failed) else "failed" if failed else "passed"
    feedback = public_feedback(value, results, observations)
    outcome = {"version": VERSION, "contract_sha256": pin(contract), "status": status, "results": results,
               "pending": pending, "public_feedback": feedback[:10], "reserved_failed": any(r["visibility"] == "reserved" for r in failed),
               "observations_sha256": pin(observations), "cleanup": clean is not None,
               "coverage": "finite contract probes; external filesystem snapshots, not universal transient-effect proof"}
    publish(store, "result.json", outcome)
    return outcome


def verify(store, *, expected_binding, originals=None):
    """Replay retained originals, without Docker, imports or mutable candidates."""
    from fleet_safe_paths import RootedFS
    store = Path(store)
    with RootedFS(store) if originals is None else nullcontext() as fs:
        def read(name):
            if originals is not None:
                if name not in originals or not isinstance(originals[name], bytes):
                    raise ValueError("missing original check evidence: " + name)
                return originals[name]
            return fs.read_regular(name, directory_modes=(0o700,) * (len(Path(name).parts) - 1), max_bytes=32 * 1024 * 1024)
        contract, value, observed, result = (fleet_json.loads(read(name)) for name in
            ("contract.json", "suite.json", "observations.json", "result.json"))
        validate_suite(value)
        recovery=fleet_json.loads(read("recovery.json"))
        if not isinstance(recovery,list) or len(recovery)>2 or pin(recovery)!=contract["recovery_sha256"]:
            raise ValueError("verifier recovery commitment differs")
        if set(contract["sources"]) != ({"ledger.py", "paths.py"} if value["task"] == "D1" else {"report.py"}):
            raise ValueError("checked source manifest omits required modules")
        if (pin(contract["binding"]) != pin(expected_binding) or result["contract_sha256"] != pin(contract)
                or contract["suite_sha256"] != pin(value) or result["observations_sha256"] != pin(observed)
                or contract["image"] != IMAGE or contract["version"] != VERSION
                or contract["controller_sha256"] != digest(Path(__file__).read_bytes())
                or contract["guest_sha256"] != digest(GUEST.read_bytes())
                or contract["sandbox_sha256"] != digest(Path(__file__).with_name("fleet_harness_sandbox.py").read_bytes())):
            raise ValueError("check contract/runtime differs from pinned originals")
        if contract["evidence_root"] != str(store.resolve()):
            raise ValueError("check evidence location differs from retained origin")
        if observed or result["status"] == "passed" or originals is None and (store / "sources").exists():
            manifest = (source_manifest(store / "sources", value["task"]) if originals is None else
                        {name: digest(read("sources/" + name)) for name in contract["sources"]})
            if manifest != contract["sources"]: raise ValueError("checked source bytes changed")
        resource = None
        if observed or result["status"] == "passed":
            resource = fleet_json.loads(read("resource/resource.json"))
            created = fleet_json.loads(read("resource/created.json"))
            if created["resource_sha256"] != pin(resource):
                raise ValueError("resource creation not bound to intent")
            resource["cid"] = created["cid"]
            start = fleet_json.loads(read("resource/start-intent.json"))
            if pin(start) != pin({"resource": resource, "action": "start"}):
                raise ValueError("start was for a different resource")
            if (resource["candidate"] != str((store / "sources").resolve()) or resource["guest"] != str((store / "bridge").resolve())
                    or resource["scratch"] != str((store / "resource/work").resolve()) or resource["image"] != IMAGE
                    or resource["argv"] != ["/usr/local/bin/python3", "-I", "-S", "-B", "/bridge/" + GUEST.name]
                    or resource["writable"] or resource["temporary"] or resource["dependencies"] is not None
                    or type(resource["processes"]) is not int or resource["processes"] != 1):
                raise ValueError("check resource has unintended access")
            attach = fleet_json.loads(read("resource/attach.json"))
            if attach["owner"] != resource["owner"] or attach["resource_id"] != resource["resource_id"]:
                raise ValueError("attach process belongs to another resource")
            Sandbox.validate_inspect(fleet_json.loads(read("resource/created-inspect.json")), resource)
        for index, item in enumerate(observed, 1):
            wire = fleet_json.loads(read(f"resource/rpc/{index:04d}.json"))
            if (pin(fleet_json.loads(base64.b64decode(wire["request_b64"], validate=True))) != pin(item["request"])
                    or pin(fleet_json.loads(base64.b64decode(wire["response_b64"], validate=True))) != pin(item["response"])):
                raise ValueError("derived RPC differs from original bytes")
            if value["task"] == "D1":
                if item["filesystem"] is None:
                    if result["status"]=="passed":raise ValueError("PASS lacks external filesystem observation")
                    continue
                snapshot = fleet_json.loads(read(f"resource/snapshots/{index:04d}.json"))
                Sandbox.validate_inspect(snapshot["paused_inspect"], resource)
                if snapshot["paused_inspect"]["State"].get("Paused") is not True or pin(snapshot["filesystem"]) != pin(item["filesystem"]):
                    raise ValueError("filesystem lacks confirmed external pause observation")
                for entry in item["filesystem"].values():
                    if entry["kind"] == "file":
                        raw = base64.b64decode(entry["bytes_b64"], validate=True)
                        if len(raw) != entry["size"] or digest(raw) != entry["sha256"]:
                            raise ValueError("filesystem bytes differ from digest")
        prefix_failure=result["status"]=="failed" and any(r["passed"] is False for r in result["results"])
        if result["results"] and pin(replay(value, observed,allow_prefix=prefix_failure)) != pin(result["results"]):
            raise ValueError("retained verdict does not reproduce")
        if (pin(result["public_feedback"]) != pin(public_feedback(value, result["results"], observed))
                or result["reserved_failed"] is not any(r["visibility"] == "reserved" and not r["passed"] for r in result["results"])):
            raise ValueError("repair feedback is not derived exclusively from public originals")
        if result["status"] == "passed":
            from fleet_harness_resource_evidence import verify as verify_resource
            proof_names=["resource.json","created.json","created-inspect.json","start-intent.json","attach.json",
                "cleanup.json","cleanup-observation.json","terminal-streams.json","stdout.raw","stderr.raw"]
            proof_names.extend(f"rpc/{i:04d}.json" for i in range(1,len(observed)+1))
            verify_resource({"resource/"+name:read("resource/"+name) for name in proof_names})
            validate_source_admission(contract["source_admission"], contract["sources"])
            cleanup = fleet_json.loads(read("resource/cleanup.json"))
            streams = fleet_json.loads(read("resource/terminal-streams.json"))
            stdout = base64.b64decode(streams["stdout_b64"], validate=True)
            stderr = base64.b64decode(streams["stderr_b64"], validate=True)
            if (streams["owner"] != resource["owner"] or streams["resource_id"] != resource["resource_id"]
                    or streams["bounded"] is not True or stdout or len(stderr) > 1024 * 1024):
                raise ValueError("PASS lacks original bounded terminal streams")
            if (pin(cleanup["resource"]) != pin(resource) or result["pending"] or not result["results"] or not all(r["passed"] is True for r in result["results"])
                    or cleanup["inactive"] is not True or cleanup["resources_clean"] is not True
                    or cleanup["extra_stdout"] or not cleanup["bounded_output"] or result["cleanup"] is not True):
                raise ValueError("PASS lacks complete checks/cleanup")
        return result


def verify_interrupted(originals, *, binding):
    """Validate a prefix using the same source/wire/snapshot guards as a final.

    The in-memory BLOCKED projection is validation scaffolding. It is never
    published or treated as an original result or evidence of acceptance.
    """
    if "result.json" in originals:raise ValueError("completed checker is immutable")
    contract=fleet_json.loads(originals["contract.json"]);value=fleet_json.loads(originals["suite.json"])
    names=sorted(n for n in originals if n.startswith("observations/"))
    if names!=[f"observations/{i:04d}.json" for i in range(1,len(names)+1)]:raise ValueError("interrupted observation gap")
    observations=[]
    wires=sorted(n for n in originals if n.startswith("resource/rpc/"))
    if wires!=[f"resource/rpc/{i:04d}.json" for i in range(1,len(wires)+1)]:raise ValueError("interrupted RPC gap")
    for index,name in enumerate(wires,1):
        wire=fleet_json.loads(originals[name]);request=fleet_json.loads(base64.b64decode(wire["request_b64"],validate=True))
        response=fleet_json.loads(base64.b64decode(wire["response_b64"],validate=True))
        snapshot=originals.get(f"resource/snapshots/{index:04d}.json")
        filesystem=(fleet_json.loads(snapshot)["filesystem"] if snapshot else None) if value["task"]=="D1" else {}
        observations.append({"id":request["id"],"request":request,"response":response,"filesystem":filesystem})
    for index,name in enumerate(names):
        if index>=len(observations) or pin(fleet_json.loads(originals[name]))!=pin(observations[index]):
            raise ValueError("interrupted derived observation differs from original RPC/snapshot")
    if "observations.json" in originals:
        aggregate=fleet_json.loads(originals["observations.json"])
        if not isinstance(aggregate,list) or len(aggregate)>len(observations) or pin(aggregate)!=pin(observations[:len(aggregate)]):
            raise ValueError("interrupted aggregate differs from original RPC/snapshot")
    result={"version":VERSION,"contract_sha256":pin(contract),"status":"blocked","results":[],"pending":["interrupted"],
        "public_feedback":[],"reserved_failed":False,"observations_sha256":pin(observations),"cleanup":False,"coverage":"validation only"}
    projected={**originals,"observations.json":fleet_json.canonical_bytes(observations),"result.json":fleet_json.canonical_bytes(result)}
    verify(contract["evidence_root"],expected_binding=binding,originals=projected)
    return observations,replay(value,observations,allow_prefix=True)


def finalize_failed_prefix(store, *, binding, originals):
    """Preserve a witnessed counterexample after interruption; never derive PASS."""
    store=Path(store)
    contract=fleet_json.loads((store/"contract.json").read_bytes())
    if pin(contract["binding"])!=pin(binding):raise ValueError("prefix belongs to another check")
    value=fleet_json.loads((store/"suite.json").read_bytes())
    observations,results=verify_interrupted(originals,binding=binding)
    failed=[r for r in results if not r["passed"]]
    if not failed:return None
    publish(store,"observations.json",observations)
    result={"version":VERSION,"contract_sha256":pin(contract),"status":"failed","results":results,
        "pending":["interrupted_after_observed_contract_failure"],"public_feedback":public_feedback(value,results,observations),
        "reserved_failed":any(r["visibility"]=="reserved" for r in failed),"observations_sha256":pin(observations),
        "cleanup":True,"coverage":"retained failing prefix; incomplete probes never yield PASS"}
    publish(store,"result.json",result)
    return verify(store,expected_binding=binding)
