"""Disabled owner-cycle controller: durable offline conformance, no live dispatch.

One writer, immutable event publications and CAS revisions. The original
Mission driver/profile/archives are deliberately not activated or reinterpreted.
Control requests take the short journal lock, never the runtime driver lock.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import re
import time
import uuid

import fleet_acceptance as acceptance
import fleet_artifacts as artifacts
import fleet_herdr_archive as archive
import fleet_herdr_evidence as evidence
import fleet_herdr_owner_contract as contracts
import fleet_herdr_owner_functional as functional
import fleet_herdr_owner_protocol as protocol
import fleet_herdr_owner_runtime as runtime
import fleet_herdr_owner_native as native
import fleet_herdr_scope as scope
import fleet_herdr_work_packet as work
import fleet_json
import fleet_safe_paths as safe


class CycleError(ValueError):
    pass


def _error_detail(exc):
    try:
        description = str(exc)
    except Exception:
        description = "unrenderable backend error"
    detail = (type(exc).__name__ + ": " + description).replace("\x00", "\\0")
    return detail.encode("utf-8", errors="backslashreplace")[:8000].decode("utf-8", errors="ignore")


class OfflineBackend:
    """Test boundary only. No real CLI or provider implementation is registered."""
    supports_functional = False
    def send(self, admission, task):
        raise NotImplementedError

    def poll(self, admission):
        raise NotImplementedError

    def cancel(self, resource):
        raise NotImplementedError

    def start_check(self, binding, tree, tests):
        raise NotImplementedError

    def poll_check(self, binding):
        raise NotImplementedError

    def cancel_check(self, resource):
        raise NotImplementedError


class Cycle:
    def __init__(self, runs, cycle_id, *, contract_sha256):
        self.runs = Path(runs)
        self.cycle_id = contracts.identity(cycle_id)
        self.pin = contracts.pin(contract_sha256)
        self.prefix = f"missions/{self.cycle_id}/owner-cycle"

    def _read(self, relative, *, optional=False):
        path = f"{self.prefix}/{relative}"
        with safe.RootedFS(self.runs) as fs:
            fn = fs.read_regular_optional if optional else fs.read_regular
            raw = fn(path, directory_modes=(0o700,) * (len(Path(path).parts) - 1),
                     max_bytes=artifacts.MAX_ARTIFACT_BYTES)
        return None if raw is None else fleet_json.loads(raw)

    def _write(self, relative, value):
        path = f"{self.prefix}/{relative}"
        with safe.RootedFS(self.runs) as fs:
            fs.atomic_write(path, fleet_json.canonical_bytes(value),
                            directory_modes=(0o700,) * (len(Path(path).parts) - 1))

    def put(self, raw):
        return artifacts.put_bytes(self.runs, self.cycle_id, raw)["artifact_id"]

    def get(self, pin):
        return artifacts.get_bytes(self.runs, self.cycle_id, contracts.pin(pin))

    def put_json(self, value):
        return self.put(fleet_json.canonical_bytes(value))

    def json(self, pin):
        return fleet_json.loads(self.get(pin))

    @classmethod
    def create(cls, runs, contract):
        contracts.validate(contract)
        result = cls(runs, contract["cycle_id"], contract_sha256=work.digest(contract))
        # A separate namespace must never attach to an existing historical Mission.
        with safe.RootedFS(result.runs) as fs:
            with fs.exclusive_lock(".owner-cycles-create.lock", directory_modes=()):
                try:
                    legacy = fs.read_regular_optional(f"missions/{result.cycle_id}/mission.jsonl",
                        directory_modes=(0o700, 0o700), max_bytes=64 * 1024 * 1024)
                except safe.SafePathError as exc:
                    if not str(exc).startswith("rooted directory is missing:"):
                        raise
                    legacy = None
                if legacy is not None:
                    raise CycleError("cannot reinterpret an existing Mission")
                try:
                    existing = result._read("creation.json", optional=True)
                except safe.SafePathError as exc:
                    if not str(exc).startswith("rooted directory is missing:"):
                        raise
                    existing = None
                if existing is not None:
                    if existing.get("contract_sha256") != result.pin:
                        raise CycleError("creation identity already belongs to another contract")
                    current = result.recover()
                    if current["seq"] == 0:
                        result._append("created", existing, now=contract["started_at"])
                    return result
                candidate = Path(contract["prepared"]["execution_envelope"]["candidate_repo"])
                if result.runs.resolve().is_relative_to(candidate):
                    raise CycleError("controller store must be outside the candidate")
                spec = contract["prepared"]["sources"]["scope"]
                observed = scope.capture(candidate, spec)
                tracked = archive._git(candidate, "ls-tree", "-r", "--name-only", "-z", contract["prepared"]["execution_envelope"]["base_sha"])
                baseline = {"schema_version": 1, "mission_id": result.cycle_id,
                    "compiled_digest": contract["prepared"]["sources"]["compiled_digest"],
                    "base_sha": contract["prepared"]["execution_envelope"]["base_sha"],
                    "contract_sha256": work.digest(spec), "inventory": observed,
                    "tracked_paths": sorted(p.decode() for p in tracked.split(b"\0") if p)}
                if scope.evaluate(baseline, observed, spec)["status"] != "accepted":
                    raise CycleError("initial physical scope is not complete and accepted")
                result.put_json(contract)
                result._write("creation.json", {"contract_sha256": result.pin,
                    "baseline_sha256": result.put_json(baseline)})
                result._append("created", {"contract_sha256": result.pin,
                    "baseline_sha256": work.digest(baseline)}, now=contract["started_at"])
        return result

    def _initial(self):
        creation = self._read("creation.json")
        contracts.exact(creation, {"contract_sha256", "baseline_sha256"}, "creation anchor")
        if creation["contract_sha256"] != self.pin:
            raise CycleError("creation differs from externally supplied contract identity")
        contract = contracts.validate(self.json(self.pin))
        if contract["cycle_id"] != self.cycle_id:
            raise CycleError("cycle identity mismatch")
        baseline = self.json(creation["baseline_sha256"])
        envelope = contract["prepared"]["execution_envelope"]
        if (baseline["mission_id"] != self.cycle_id or baseline["base_sha"] != envelope["base_sha"]
                or baseline["compiled_digest"] != envelope["compiled_digest"]
                or baseline["inventory"]["root"] != envelope["candidate_repo"]):
            raise CycleError("baseline is not bound to creation")
        if scope.evaluate(baseline, baseline["inventory"], contract["prepared"]["sources"]["scope"])["status"] != "accepted":
            raise CycleError("invalid baseline")
        return {"contract": contract, "baseline": baseline, "creation": creation,
                "attempts": [], "revisions": [], "decisions": [], "feedback": None,
                "control": None, "control_requests": {}, "paused": False, "terminal": None,
                "head": None, "seq": 0, "last_at": contract["started_at"]}

    def load(self, *, _pending=False):
        current = self._initial()
        with safe.RootedFS(self.runs) as fs:
            try:
                names = fs.list_directory(f"{self.prefix}/events", directory_modes=(0o700,) * 4)
            except safe.SafePathError as exc:
                if not str(exc).startswith("rooted directory is missing:"):
                    raise
                names = []
        # atomic_write may leave a content-addressed pending publication after
        # a crash. Fail closed; never skip a pending intent and resend.
        if _pending:
            names = [name for name in names if not re.fullmatch(r"\.fleet-atomic-[a-f0-9]{64}-[a-f0-9]{64}\.tmp", name)]
        for number, name in enumerate(names, 1):
            if name != f"{number:06d}.json":
                raise CycleError("event gap or pending publication requires reconciliation")
            event = self._read("events/" + name)
            self._apply(current, event)
        return current

    def _recover_locked(self):
        current = self.load(_pending=True)
        with safe.RootedFS(self.runs) as fs:
            try:
                names = fs.list_directory(f"{self.prefix}/events", directory_modes=(0o700,) * 4)
            except safe.SafePathError as exc:
                if str(exc).startswith("rooted directory is missing:"):
                    return current
                raise
        pending = [n for n in names if n.startswith(".fleet-atomic-")]
        if len(pending) > 1:
            raise CycleError("conflicting pending journal publications")
        for name in pending:
            if not re.fullmatch(r"\.fleet-atomic-[a-f0-9]{64}-[a-f0-9]{64}\.tmp", name):
                raise CycleError("unknown pending publication")
            raw = self.get(name[-68:-4])
            event = fleet_json.loads(raw)
            leaf = f"{event['seq']:06d}.json"
            if name != safe._atomic_pending_name(leaf, raw):
                raise CycleError("pending event identity mismatch")
            self._apply(current, event)
            # The CAS copy survives even a partial pending-file write. The
            # rooted publisher validates/reconciles this exact owned intent.
            self._write("events/" + leaf, event)
        return current

    def recover(self):
        """Finish a durable publication; never repeats a runtime send."""
        with safe.RootedFS(self.runs) as fs:
            with fs.exclusive_lock(f"{self.prefix}/.journal.lock", directory_modes=(0o700,) * 3):
                current = self._recover_locked()
                if current["terminal"]:
                    self._publish_seal(current)
        return current

    def _classify(self, current, attempt):
        response = attempt["response"]
        raw, final = self.get(response["transcript"]), self.get(response["final"])
        try:
            if attempt["admission"]["version"] in {"owner-cycle-admission-v3", "owner-cycle-admission-v4"}:
                from fleet_harness_delivery import verify_continuity
                prior=[]
                for earlier in current["attempts"]:
                    if earlier["admission"]==attempt["admission"]:break
                    if earlier["response"] is None:raise ValueError("previous request history unavailable")
                    prior.append((self.get(earlier["response"]["transcript"]),earlier["admission"]))
                verify_continuity(raw,prior,limits=current["contract"]["request_budget"],admission=attempt["admission"])
            attestation = runtime.verify_terminal(raw, final, contract=current["contract"], admission=attempt["admission"],
                                                  native_binding=attempt.get("native_binding"))
            if "public_read" in current["contract"]["prepared"]["sources"]:
                from fleet_harness_read_scope import editable_files
                initial=editable_files(self.json(attempt["input_snapshot"]),current["contract"]["prepared"]["sources"]["scope"])
                if attestation["effects"]["before"]!=initial:
                    raise CycleError("delivery input differs from immutable admission snapshot")
            if attempt["admission"]["version"] == "owner-cycle-admission-v2":
                attestation["usage"] = native.usage(raw, attempt["native_binding"], self.json(attempt["native_baseline"]),
                                                   attempt["admission"], self.get, contract=current["contract"])
            message = protocol.parse_response(final, current["contract"]["prepared"]["work_packet"])
            return {"valid": True, "message": message, "runtime": attestation}
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            return {"valid": False, "error": str(exc), "message": None, "runtime": None}

    def _checks(self, current, revision_pin):
        revision = self.json(revision_pin)
        contracts.exact(revision, {"version", "admission_sha256", "parent_revision", "inventory",
                                  "tree_sha", "tree", "patch"}, "revision")
        if revision["version"] != "owner-revision-v1":
            raise CycleError("unsupported revision")
        spec = current["contract"]["prepared"]["sources"]
        if "public_read" in spec:
            from fleet_harness_read_scope import editable_files
            delivered=next((a for a in current["attempts"] if work.digest(a["admission"])==revision["admission_sha256"]),None)
            if (delivered is None or not delivered["classified"] or not delivered["classified"]["valid"]
                    or delivered["classified"]["runtime"]["effects"]["after"]!=editable_files(revision["inventory"],spec["scope"])):
                raise CycleError("frozen revision differs from delivered executor bytes")
        tree = self.get(revision["tree"])
        self.get(revision["patch"])
        # Verify tree identity independently of the mutable Git repository.
        import fleet_archive_tree
        object_format = "sha1" if len(revision["tree_sha"]) == 40 else "sha256"
        if fleet_archive_tree.tree_hash_from_tar(tree, object_format) != revision["tree_sha"]:
            raise CycleError("revision tree identity mismatch")
        physical = scope.evaluate(current["baseline"], revision["inventory"], spec["scope"],
                                  tree_sha=revision["tree_sha"], tree=tree)
        artifact = acceptance.evaluate(spec["acceptance"], tree, mission_id=self.cycle_id, final_sha=revision["tree_sha"])
        pending, functional_result = [], None
        if spec["functional"] is not None:
            attempt = next((a for a in current["attempts"] if work.digest(a["admission"]) == revision["admission_sha256"]), None)
            check = attempt["functional"] if attempt else None
            if check is None or not check["quiescent"] or check["receipt"] is None:
                pending.append("functional_delivery_and_cleanup_required")
            elif check["conflicts"] or any(e["kind"] == "retention" for e in check["errors"]):
                pending.append("functional_evidence_incomplete_or_conflicting")
            else:
                bound = functional.binding(current["contract"], revision_pin, revision)
                try:
                    functional_result = functional.verify(bound, self.json(check["receipt"]), self.get,
                        spec["functional_tests"].encode())
                    if functional_result["status"] in {"blocked", "indeterminate"}:
                        pending.append("functional_" + functional_result["status"])
                except (ValueError, KeyError, TypeError) as exc:
                    pending.append("invalid_functional_evidence:" + type(exc).__name__)
        if spec["context"]["additional_requirements"]:
            pending.append("additional_requirements_need_independent_evidence")
        return {"version": "owner-check-v1", "revision_sha256": revision_pin,
                "scope": physical, "artifacts": artifact, "pending": pending,
                **({"functional": functional_result} if spec["functional"] is not None else {}),
                "accepted": (physical["status"] == artifact["status"] == "accepted" and not pending
                             and (functional_result is None or functional_result["status"] == "passed")),
                "semantic_success": "NOT_VERIFIED"}

    def _apply(self, current, event):
        contracts.exact(event, {"seq", "previous", "at", "kind", "payload"}, "event")
        if (type(event["seq"]) is not int or event["seq"] != current["seq"] + 1
                or event["previous"] != current["head"] or contracts.instant(event["at"]) < current["last_at"]):
            raise CycleError("event chain mismatch")
        if current["terminal"] is not None:
            raise CycleError("terminal cycles are immutable")
        kind, p = event["kind"], event["payload"]
        a = current["attempts"][-1] if current["attempts"] else None
        contract = current["contract"]
        if kind == "created":
            if current["seq"] != 0 or p != current["creation"]:
                raise CycleError("invalid creation event")
        elif current["seq"] == 0:
            raise CycleError("creation event is missing")
        elif kind == "admitted":
            contracts.exact(p, {"admission", "task"}, "admitted")
            admission = contracts.validate_admission(p["admission"], contract)
            if (current["control"] or current["paused"] or event["at"] >= contract["deadline_at"]
                    or admission["ordinal"] != len(current["attempts"]) + 1
                    or (a is not None and not a["settled"])):
                raise CycleError("admission is not authorized in the current state")
            if a is not None and (self._delivery_veto(a) or
                    (a["checks"] is not None and self.json(a["checks"]) != self._checks(current, a["revision"]))):
                raise CycleError("prior evidence no longer authorizes continuation")
            parent = current["revisions"][-1] if current["revisions"] else None
            task = contracts.task(contract, attempt=admission["ordinal"], feedback=current["feedback"], decisions=current["decisions"])
            if (p["task"] != work.digest(task) or self.json(p["task"]) != task
                    or admission["prompt_sha256"] != p["task"] or admission["parent_revision"] != parent):
                raise CycleError("task or parent revision differs from continuation")
            for old in current["attempts"]:
                for key in (("run_id", "admission_id", "generation") if admission["version"] == "owner-cycle-admission-v2"
                            else ("run_id", "admission_id", "generation", "agent_session", "turn_id")):
                    if old["admission"][key] == admission[key]:
                        raise CycleError("execution identity reused across attempts")
            current["attempts"].append({"admission": admission, "task": p["task"], "intent": False,
                "response": None, "conflicts": [], "observation_errors": [], "classified": None, "quiescent": False,
                "revision": None, "functional": None, "checks": None, "settled": False, "decision": None})
            if "public_read" in contract["prepared"]["sources"]:
                current["attempts"][-1]["input_snapshot"]=None
            if admission["version"] == "owner-cycle-admission-v2":
                current["attempts"][-1].update(native_baseline=None, native_observations=[], native_binding=None,
                                               native_frontier=None, native_binding_snapshot=None,
                                               cancel_intent=False, cancel_signal=None)
        elif kind == "input_snapshot":
            contracts.exact(p,{"admission_sha256","snapshot"},"harness admission input")
            sources=contract["prepared"]["sources"]
            if ("public_read" not in sources or a is None or a["intent"] or a.get("input_snapshot") is not None
                    or p["admission_sha256"]!=work.digest(a["admission"]) or current["control"] or current["paused"]
                    or event["at"]>=contract["deadline_at"]):
                raise CycleError("input snapshot must uniquely precede this admission's dispatch")
            observed=self.json(p["snapshot"])
            from fleet_harness_read_scope import editable_files
            editable_files(observed,sources["scope"])
            if (scope.evaluate(current["baseline"],observed,sources["scope"])["status"]!="accepted"
                    or any(observed["entries"].get(name,{}).get("sha256")!=sha for name,sha in sources["public_read"]["read_only"].items())):
                raise CycleError("admission input differs from physical scope or public readonly authority")
            a["input_snapshot"]=p["snapshot"]
        elif kind == "native_baseline":
            contracts.exact(p, {"admission_sha256", "snapshot"}, "native baseline")
            if (a is None or a["admission"]["version"] != "owner-cycle-admission-v2" or a["intent"]
                    or a["native_baseline"] is not None or p["admission_sha256"] != work.digest(a["admission"])):
                raise CycleError("native baseline must precede this admission's dispatch")
            snapshot = self.json(p["snapshot"])
            native.before_dispatch(snapshot, a["admission"], self.get, contract=contract)
            receipt, _, _ = native.inspect_snapshot(snapshot, a["admission"], self.get)
            if receipt["agent_session"] is not None and any(old["native_binding"] and
                    old["native_binding"]["agent_session"] == receipt["agent_session"]["value"] for old in current["attempts"][:-1]):
                raise CycleError("pre-dispatch native session was already used")
            a["native_baseline"] = p["snapshot"]
        elif kind == "native_observed":
            contracts.exact(p, {"admission_sha256", "snapshot", "final"}, "native observation")
            if (a is None or a["admission"]["version"] != "owner-cycle-admission-v2" or not a["intent"]
                    or p["admission_sha256"] != work.digest(a["admission"])):
                raise CycleError("native observation has no admitted dispatch")
            snapshot = self.json(p["snapshot"])
            self.get(snapshot["transcript"])
            if p["final"] is not None:
                self.get(p["final"])
            try:
                observation = native.observe(snapshot, contract=contract, admission=a["admission"],
                                             baseline=self.json(a["native_baseline"]), read=self.get)
                if a["native_frontier"] is not None:
                    previous = self.json(a["native_frontier"])
                    old_receipt, old_raw, _ = native.inspect_snapshot(previous, a["admission"], self.get)
                    receipt, raw, _ = native.inspect_snapshot(snapshot, a["admission"], self.get)
                    if receipt["revision"] < old_receipt["revision"] or not raw.startswith(old_raw):
                        raise CycleError("native snapshot regressed or was rebound")
                binding = observation["binding"]
                if binding is None and self.get(snapshot["transcript"]) and not self.get(snapshot["transcript"]).endswith(b"\n"):
                    # A retained partial append cannot remove the binding
                    # already attested by its complete earlier prefix.
                    binding = a["native_binding"]
                if a["native_binding"] is not None and binding != a["native_binding"]:
                    raise CycleError("observed native binding cannot change or disappear")
                if binding is not None:
                    for old in current["attempts"][:-1]:
                        if old["native_binding"] and any(old["native_binding"][k] == binding[k] for k in ("agent_session", "turn_id")):
                            raise CycleError("native session/turn was reused across attempts")
                response = observation["response"]
                if p["final"] != (hashlib.sha256(response["final"]).hexdigest() if response else None):
                    raise CycleError("native final differs from preserved bytes")
            except (ValueError, KeyError, TypeError) as exc:
                a["observation_errors"].append({"admission_sha256": work.digest(a["admission"]), "kind": "retention",
                                                "detail": _error_detail(exc)})
                a["quiescent"] = False
            else:
                a["native_frontier"] = p["snapshot"]
                if binding is not None and a["native_binding"] is None:
                    a["native_binding_snapshot"] = p["snapshot"]
                a["native_binding"] = binding
                if response is not None:
                    delivery = {"admission_sha256": work.digest(a["admission"]), "transcript": response["transcript"], "final": p["final"]}
                    if a["response"] is None:
                        a["response"] = delivery
                    elif delivery != a["response"] and delivery not in a["conflicts"]:
                        a["conflicts"].append(delivery)
                a["quiescent"] = observation["quiescence"] is not None
            a["native_observations"].append(p["snapshot"])
        elif kind == "dispatch_intent":
            if (a is None or a["intent"] or current["control"] or current["paused"]
                    or event["at"] >= contract["deadline_at"] or p != runtime.resource(a["admission"])):
                raise CycleError("dispatch is not authorized")
            if a["admission"]["version"] == "owner-cycle-admission-v2" and a["native_baseline"] is None:
                raise CycleError("native dispatch lacks a preserved frontier")
            if "public_read" in contract["prepared"]["sources"] and a.get("input_snapshot") is None:
                raise CycleError("harness dispatch lacks its immutable input snapshot")
            a["intent"] = True
        elif kind == "cancel_intent":
            if (a is None or a["admission"]["version"] != "owner-cycle-admission-v2" or not a["intent"]
                    or a["cancel_intent"] or p != runtime.resource(a["admission"])
                    or not ((current["control"] and current["control"]["action"] == "cancel")
                            or event["at"] >= contract["deadline_at"])):
                raise CycleError("cancel intent has no owned cancellation authority")
            a["cancel_intent"] = True
        elif kind == "cancel_signal_intent":
            contracts.exact(p, {"resource", "snapshot", "binding"}, "native cancel signal")
            if (a is None or a["admission"]["version"] != "owner-cycle-admission-v2" or not a["cancel_intent"]
                    or a["cancel_signal"] is not None or p["resource"] != runtime.resource(a["admission"])
                    or not a["native_binding"] or p["binding"] != a["native_binding"]
                    or not a["native_observations"] or p["snapshot"] != a["native_observations"][-1]):
                raise CycleError("cancel signal lacks its unique owned frontier")
            observed = native.observe(self.json(p["snapshot"]), contract=contract, admission=a["admission"],
                                      baseline=self.json(a["native_baseline"]), read=self.get)
            if observed["binding"] != p["binding"] or not observed["active"]:
                raise CycleError("cannot signal a completed or unbound turn")
            a["cancel_signal"] = p
        elif kind == "response":
            contracts.exact(p, {"admission_sha256", "transcript", "final"}, "response")
            if (a is None or a["admission"]["version"] not in contracts.DELIVERED_ADMISSION_VERSIONS or not a["intent"]
                    or a["response"] is not None or p["admission_sha256"] != work.digest(a["admission"])):
                raise CycleError("response is not for the pending admission")
            self.get(p["transcript"]); self.get(p["final"])
            a["response"] = p
        elif kind == "classified":
            if a is None or a["response"] is None or a["classified"] is not None or p != self._classify(current, a):
                raise CycleError("classification differs from preserved response")
            a["classified"] = p
        elif kind == "response_conflict":
            contracts.exact(p, {"admission_sha256", "transcript", "final"}, "conflicting response")
            if (a is None or a["response"] is None or p == a["response"] or p in a["conflicts"]
                    or p["admission_sha256"] != work.digest(a["admission"])):
                raise CycleError("invalid conflicting delivery")
            self.get(p["transcript"]); self.get(p["final"])
            a["conflicts"].append(p)
        elif kind == "observation_failed":
            contracts.exact(p, {"admission_sha256", "kind", "detail"}, "observation failure")
            if a is None or not a["intent"] or p["admission_sha256"] != work.digest(a["admission"]) or p["kind"] not in {"transport", "retention"}:
                raise CycleError("unbound observation failure")
            work.text(p["detail"], "observation error")
            a["observation_errors"].append(p)
        elif kind == "quiescent":
            if a is None or a["admission"]["version"] not in contracts.DELIVERED_ADMISSION_VERSIONS or not a["intent"] or a["quiescent"]:
                raise CycleError("unexpected quiescence")
            runtime.verify_quiescence(p, a["admission"])
            a["quiescent"] = True
        elif kind == "revision":
            contracts.exact(p, {"revision_sha256"}, "revision event")
            if (a is None or not a["quiescent"] or not a["classified"] or not a["classified"]["valid"]
                    or a["classified"]["message"]["type"] != "submit_candidate" or a["revision"] is not None
                    or self._delivery_veto(a) or current["control"] or current["paused"]):
                raise CycleError("candidate cannot be frozen yet")
            revision = self.json(p["revision_sha256"])
            if (revision["admission_sha256"] != work.digest(a["admission"])
                    or revision["parent_revision"] != a["admission"]["parent_revision"]):
                raise CycleError("revision/admission binding mismatch")
            self._checks(current, p["revision_sha256"])
            a["revision"] = p["revision_sha256"]
            current["revisions"].append(a["revision"])
        elif kind == "checked":
            contracts.exact(p, {"checks_sha256"}, "check event")
            if (a is None or a["revision"] is None or a["checks"] is not None
                    or self.json(p["checks_sha256"]) != self._checks(current, a["revision"])):
                raise CycleError("check result is not for this exact revision")
            a["checks"] = p["checks_sha256"]
        elif kind == "functional_intent":
            contracts.exact(p, {"binding_sha256"}, "functional intent")
            if (a is None or a["revision"] is None or a["functional"] is not None or a["settled"] or a["checks"] is not None
                    or current["control"] or current["paused"] or event["at"] >= contract["deadline_at"]):
                raise CycleError("functional check is not authorized")
            bound = functional.binding(contract, a["revision"], self.json(a["revision"]))
            if self.json(p["binding_sha256"]) != bound:
                raise CycleError("functional intent differs from revision binding")
            a["functional"] = {"binding": p["binding_sha256"], "receipt": None, "quiescent": False,
                               "conflicts": [], "errors": []}
        elif kind == "functional_observed":
            contracts.exact(p, {"binding_sha256", "receipt_sha256"}, "functional observation")
            if a is None or a["functional"] is None or p["binding_sha256"] != a["functional"]["binding"]:
                raise CycleError("functional observation belongs to another revision")
            self.json(p["receipt_sha256"])
            check = a["functional"]
            if check["receipt"] is None:
                check["receipt"] = p["receipt_sha256"]
            elif p["receipt_sha256"] != check["receipt"] and p["receipt_sha256"] not in check["conflicts"]:
                check["conflicts"].append(p["receipt_sha256"])
            else:
                raise CycleError("duplicate functional observation event")
        elif kind == "functional_quiescent":
            if a is None or a["functional"] is None or a["functional"]["quiescent"]:
                raise CycleError("unexpected functional cleanup")
            functional.verify_quiescence(p, self.json(a["functional"]["binding"]))
            a["functional"]["quiescent"] = True
        elif kind == "functional_observation_failed":
            contracts.exact(p, {"binding_sha256", "kind", "detail"}, "functional observation failure")
            if (a is None or a["functional"] is None or p["binding_sha256"] != a["functional"]["binding"]
                    or p["kind"] not in {"transport", "retention"}):
                raise CycleError("unbound functional error")
            work.text(p["detail"], "functional observation error")
            a["functional"]["errors"].append(p)
        elif kind == "settled":
            if (a is None or not a["quiescent"] or a["classified"] is None or a["settled"]
                    or self._delivery_veto(a) or current["control"] or current["paused"]):
                raise CycleError("attempt is not ready for continuation")
            classification = a["classified"]
            if not classification["valid"]:
                expected = {"reason": "invalid_delivery", "detail": classification["error"]}
            elif classification["message"]["type"] == "request_decision":
                if a["decision"] is None:
                    raise CycleError("decision still required")
                expected = {"reason": "decision_resolved", "detail": a["decision"]["answer"]}
            else:
                if not a["checks"]:
                    raise CycleError("check still required")
                checks = self.json(a["checks"])
                if checks != self._checks(current, a["revision"]) or checks["accepted"] or checks["pending"]:
                    raise CycleError("cannot repair accepted work or hide an unmet dependency")
                expected = self._repair_feedback(a["checks"], checks)
            if p != expected:
                raise CycleError("repair feedback was altered")
            a["settled"] = True
            current["feedback"] = p
        elif kind == "decision":
            contracts.exact(p, {"request_id", "answer", "authority"}, "decision")
            if (a is None or not a["quiescent"] or not a["classified"] or not a["classified"]["valid"]
                    or a["classified"]["message"]["type"] != "request_decision" or a["decision"] is not None
                    or p["request_id"] != self._decision_id(a) or p["authority"] != "within_existing_contract"):
                raise CycleError("decision is not for the pending request or changes authority")
            work.text(p["answer"], "decision answer")
            a["decision"] = p
            current["decisions"].append(p)
        elif kind == "control_requested":
            contracts.exact(p, {"request_id", "action", "target", "reason"}, "control request")
            contracts.identity(p["request_id"])
            if p["request_id"] in current["control_requests"]:
                raise CycleError("control request identity reused")
            work.text(p["reason"], "control reason")
            target = runtime.resource(a["admission"]) if a else None
            if (p["action"] not in {"pause", "cancel"} or p["target"] != target
                    or (current["control"] is not None and p["action"] != "cancel")):
                raise CycleError("control request changed target or superseded cancellation")
            if current["control"] and current["control"]["action"] == "cancel":
                raise CycleError("cancellation scope is immutable")
            current["control"] = p
            current["control_requests"][p["request_id"]] = p
        elif kind == "paused":
            if (not current["control"] or current["control"]["action"] != "pause" or current["paused"]
                    or (a and a["intent"] and not a["quiescent"]) or p != {"request_id": current["control"]["request_id"]}):
                raise CycleError("pause lacks quiescence")
            if a and a["functional"] and not a["functional"]["quiescent"]:
                raise CycleError("pause lacks functional cleanup")
            current["paused"] = True
        elif kind == "resumed":
            if not current["paused"] or p != {"request_id": current["control"]["request_id"]} or current["control"]["action"] != "pause":
                raise CycleError("no confirmed pause to resume")
            current["paused"], current["control"] = False, None
        elif kind == "terminal":
            contracts.exact(p, {"status", "revision", "checks"}, "terminal")
            if a and a["intent"] and not a["quiescent"]:
                raise CycleError("terminal requires confirmed quiescence")
            if a and a["functional"] and not a["functional"]["quiescent"]:
                raise CycleError("terminal requires independent functional cleanup")
            if p["status"] == "cancelled":
                valid = current["control"] and current["control"]["action"] == "cancel"
            elif p["status"] == "exhausted":
                valid = (event["at"] >= contract["deadline_at"] or
                         (a and a["settled"] and len(current["attempts"]) >= contract["limits"]["max_attempts"]))
            elif p["status"] == "accepted_contract":
                valid = (a and a["checks"] and self.json(a["checks"])["accepted"]
                         and not self._delivery_veto(a)
                         and self.json(a["checks"]) == self._checks(current, a["revision"])
                         and not current["control"] and not current["paused"]
                         and event["at"] < contract["deadline_at"])
            else:
                valid = False
            if (not valid or p["revision"] != (a["revision"] if a else None)
                    or p["checks"] != (a["checks"] if a else None)):
                raise CycleError("terminal criteria not demonstrated")
            current["terminal"] = p
        else:
            raise CycleError("unknown owner event: " + str(kind))
        current.update(seq=event["seq"], head=work.digest(event), last_at=event["at"])

    def _append(self, kind, payload, *, now):
        with safe.RootedFS(self.runs) as fs:
            with fs.exclusive_lock(f"{self.prefix}/.journal.lock", directory_modes=(0o700,) * 3):
                current = self._recover_locked()
                previous = None
                if kind == "control_requested":
                    previous = current["control_requests"].get(payload.get("request_id"))
                if kind == "decision":
                    previous = next((d for d in current["decisions"] if d["request_id"] == payload.get("request_id")), None)
                if previous is not None:
                    if fleet_json.canonical_bytes(previous) != fleet_json.canonical_bytes(payload):
                        raise CycleError("idempotency identity reused with different content")
                    return current
                event = {"seq": current["seq"] + 1, "previous": current["head"],
                         "at": max(contracts.instant(now() if callable(now) else now), current["last_at"]),
                         "kind": kind, "payload": copy.deepcopy(payload)}
                if (kind == "terminal" and event["payload"]["status"] == "accepted_contract"
                        and event["at"] >= current["contract"]["deadline_at"]):
                    event["payload"]["status"] = "exhausted"
                self._apply(current, event)
                self.put_json(event)
                self._write(f"events/{event['seq']:06d}.json", event)
                return current

    @staticmethod
    def _repair_feedback(pin, checks):
        return {"reason": "checks_rejected", "detail": {"checks_sha256": pin,
            "failed_requirements": [r["id"] for r in checks["artifacts"]["requirements"] if not r["passed"]],
            "scope_issues": checks["scope"]["issues"][:10],
            "scope_issue_count": len(checks["scope"]["issues"]),
            **({"functional": {k: checks["functional"][k] for k in ("status", "reason", "public_feedback", "revision_sha256") if k in checks["functional"]}}
               if checks.get("functional") is not None else {})}}

    @staticmethod
    def _delivery_veto(attempt):
        check = attempt["functional"]
        return bool(attempt["conflicts"] or any(e["kind"] == "retention" for e in attempt["observation_errors"])
                    or (check and (check["conflicts"] or any(e["kind"] == "retention" for e in check["errors"]))))

    def control(self, action, *, request_id, target, reason, now=None):
        contract=self.json(self.pin)
        if action=="cancel" and contract["version"]=="owner-cycle-contract-v4":
            from fleet_harness_control import cancel_under_barrier, pin
            plan=contract["request_budget"]["control_plan"]
            # Owner cancellation commits at the journal pathname. Before that,
            # an orphan CAS event is not an acknowledged request. New sends use
            # this same barrier; the watchdog observes the journal without locks.
            with safe.RootedFS(plan["root"],root_mode=0o700) as fs:
                with fs.exclusive_lock(".control-send.lock",directory_modes=()):
                    current=self._append("control_requested",{"request_id":request_id,"action":action,"target":target,"reason":reason},
                        now=time.time if now is None else now)
                    cancel_under_barrier(fs,pin(plan),"cycle_cancel_requested")
                    return current
        current = self._append("control_requested", {"request_id": request_id, "action": action,
            "target": target, "reason": reason}, now=time.time if now is None else now)
        if action == "cancel" and current["contract"]["version"] == "owner-cycle-contract-v3":
            from fleet_harness_budget import Ledger
            Ledger(self.runs/self.prefix/"local-backend/budget",current["contract"]["request_budget"]).revoke("cycle_cancel_requested")
        return current

    def resume(self, request_id, *, now=None):
        return self._append("resumed", {"request_id": request_id}, now=time.time if now is None else now)

    @staticmethod
    def _decision_id(attempt):
        return work.digest({"admission_sha256": work.digest(attempt["admission"]),
                            "response_sha256": attempt["response"]["final"]})

    def decide(self, request_id, answer, *, now=None):
        return self._append("decision", {"request_id": request_id, "answer": answer,
            "authority": "within_existing_contract"}, now=time.time if now is None else now)

    def _freeze(self, current, attempt):
        spec = current["contract"]["prepared"]["sources"]["scope"]
        candidate = Path(current["baseline"]["inventory"]["root"])
        before = scope.capture(candidate, spec)
        if not before["complete"]:
            raise CycleError("physical capture incomplete: " + str(before["errors"]))
        selected = sorted(set(current["baseline"]["tracked_paths"]) | set(scope.delivery_paths(current["baseline"], before, spec)))
        tree_sha, tree, patch = archive.snapshot(candidate, expected_base=current["baseline"]["base_sha"], selected_paths=selected)
        after = scope.capture(candidate, spec)
        if before != after:
            raise CycleError("candidate changed across freeze")
        revision = {"version": "owner-revision-v1", "admission_sha256": work.digest(attempt["admission"]),
            "parent_revision": attempt["admission"]["parent_revision"], "inventory": after,
            "tree_sha": tree_sha, "tree": self.put(tree), "patch": self.put(patch)}
        return self.put_json(revision)

    def _poll(self, backend, attempt, clock):
        if attempt["admission"]["version"] == "owner-cycle-admission-v2":
            return self._poll_native(backend, attempt, clock)
        def failed(kind, exc):
            failure = {"admission_sha256": work.digest(attempt["admission"]), "kind": kind,
                       "detail": _error_detail(exc)}
            if failure not in attempt["observation_errors"]:
                self._append("observation_failed", failure, now=clock)
        try:
            observed = backend.poll(copy.deepcopy(attempt["admission"]))
        except Exception as exc:
            failed("transport", exc)
            return
        try:
            contracts.exact(observed, {"response", "quiescence"}, "backend observation")
        except ValueError as exc:
            failed("retention", exc)
            return
        if observed["response"] is not None:
            raw = observed["response"]
            # Persist unmodified original bytes BEFORE parsing or attestation.
            try:
                contracts.exact(raw, {"transcript", "final"}, "raw delivery")
                if not all(isinstance(raw[k], bytes) for k in ("transcript", "final")):
                    raise CycleError("native delivery must supply original bytes")
                delivery = {"admission_sha256": work.digest(attempt["admission"]),
                    "transcript": self.put(raw["transcript"]), "final": self.put(raw["final"])}
            except (artifacts.ArtifactError, ValueError, TypeError) as exc:
                failed("retention", exc)
            else:
                if attempt["response"] is None:
                    self._append("response", delivery, now=clock)
                elif delivery != attempt["response"] and delivery not in attempt["conflicts"]:
                    self._append("response_conflict", delivery, now=clock)
        if observed["quiescence"] is not None and not attempt["quiescent"]:
            try:
                runtime.verify_quiescence(observed["quiescence"], attempt["admission"])
            except (ValueError, TypeError) as exc:
                failed("retention", exc)
            else:
                self._append("quiescent", observed["quiescence"], now=clock)

    def _poll_native(self, backend, attempt, clock):
        try:
            raw = backend.poll(copy.deepcopy(attempt["admission"]))
        except Exception as exc:
            self._append("observation_failed", {"admission_sha256": work.digest(attempt["admission"]),
                "kind": "transport", "detail": _error_detail(exc)}, now=clock)
            return
        try:
            snapshot = native.retain(raw, self.put)
            pin = self.put_json(snapshot)  # Retained before interpreting a native identity or final.
        except (ValueError, TypeError, artifacts.ArtifactError) as exc:
            self._append("observation_failed", {"admission_sha256": work.digest(attempt["admission"]),
                "kind": "retention", "detail": _error_detail(exc)}, now=clock)
            return
        if attempt["native_observations"] and pin == attempt["native_observations"][-1]:
            return
        final = None
        try:
            observed = native.observe(snapshot, contract=self.json(self.pin), admission=attempt["admission"],
                                      baseline=self.json(attempt["native_baseline"]), read=self.get)
            if observed["response"] is not None:
                final = self.put(observed["response"]["final"])
        except (ValueError, KeyError, TypeError):
            pass  # The reducer records the retained rejection deterministically.
        self._append("native_observed", {"admission_sha256": work.digest(attempt["admission"]),
                                        "snapshot": pin, "final": final}, now=clock)

    def _cancel_worker(self, backend, attempt, clock):
        if attempt["admission"]["version"] == "owner-cycle-admission-v2":
            if not attempt["cancel_intent"]:
                current = self._append("cancel_intent", runtime.resource(attempt["admission"]), now=clock)
                attempt = current["attempts"][-1]
            if attempt["cancel_signal"] is not None or not attempt["native_binding"] or not attempt["native_observations"]:
                return  # Reconcile an ambiguous signal; never repeat it.
            try:
                observed = native.observe(self.json(attempt["native_observations"][-1]), contract=self.json(self.pin),
                    admission=attempt["admission"], baseline=self.json(attempt["native_baseline"]), read=self.get)
            except (ValueError, KeyError, TypeError):
                return
            if observed["binding"] != attempt["native_binding"] or not observed["active"]:
                return
            self._append("cancel_signal_intent", {"resource": runtime.resource(attempt["admission"]),
                "snapshot": attempt["native_observations"][-1], "binding": attempt["native_binding"]}, now=clock)
        backend.cancel(runtime.resource(attempt["admission"]))

    def _poll_functional(self, backend, check, clock):
        bound = self.json(check["binding"])
        def failed(kind, exc):
            failure = {"binding_sha256": check["binding"], "kind": kind, "detail": _error_detail(exc)}
            if failure not in check["errors"]:
                self._append("functional_observation_failed", failure, now=clock)
        try:
            observed = backend.poll_check(copy.deepcopy(bound))
        except Exception as exc:
            failed("transport", exc)
            return
        try:
            contracts.exact(observed, {"binding_sha256", "outcome", "quiescence"}, "functional backend observation")
            contracts.pin(observed["binding_sha256"])
        except ValueError as exc:
            failed("retention", exc)
            return
        if observed["outcome"] is not None:
            try:
                receipt = functional.receipt(bound, observed["outcome"], self.put)
                pin = self.put_json(receipt)
                if observed["binding_sha256"] != check["binding"]:
                    preserved = self.put_json({"observed_binding_sha256": observed["binding_sha256"], "receipt_sha256": pin})
            except (ValueError, TypeError, artifacts.ArtifactError) as exc:
                failed("retention", exc)
            else:
                if observed["binding_sha256"] != check["binding"]:
                    failed("retention", CycleError("foreign functional delivery retained: " + preserved))
                elif pin != check["receipt"] and pin not in check["conflicts"]:
                    self._append("functional_observed", {"binding_sha256": check["binding"], "receipt_sha256": pin}, now=clock)
        elif observed["binding_sha256"] != check["binding"]:
            failed("retention", CycleError("foreign functional observation"))
        if observed["quiescence"] is not None and not check["quiescent"]:
            try:
                functional.verify_quiescence(observed["quiescence"], bound)
            except (ValueError, TypeError) as exc:
                failed("retention", exc)
            else:
                self._append("functional_quiescent", observed["quiescence"], now=clock)

    def tick(self, backend, *, now=None):
        contract = self.json(self.pin)
        if contract["version"] == "owner-cycle-contract-v4":
            from fleet_harness_live_backend import ControlHarnessBackend
            if type(backend) is not ControlHarnessBackend or backend.cycle.pin != self.pin:
                raise CycleError("CONTROL profile requires its exact owned backend")
            backend.guard.assert_owned()
        elif contract["version"] == "owner-cycle-contract-v3":
            from fleet_harness_backend import LocalHarnessBackend
            if not isinstance(backend, LocalHarnessBackend) or backend.cycle.pin != self.pin:
                raise CycleError("harness local profile requires its exact bound backend")
        elif not isinstance(backend, OfflineBackend):
            raise CycleError("owner_cycle_v1 has no live backend; dispatch disabled")
        if now is not None:
            contracts.instant(now)
        clock = time.time if now is None else lambda: now
        with safe.RootedFS(self.runs) as fs:
            with fs.exclusive_lock(f"{self.prefix}/.driver.lock", directory_modes=(0o700,) * 3, blocking=False) as locked:
                if not locked:
                    return {"status": "driver_busy"}
                self.recover()
                return self._tick(backend, clock)

    def _tick(self, backend, clock):
        current = self.load()
        if current["terminal"]:
            return self.verify()
        a = current["attempts"][-1] if current["attempts"] else None
        if a and a["intent"] and (a["admission"]["version"] == "owner-cycle-admission-v2"
                                  or not a["quiescent"] or a["response"] is None):
            self._poll(backend, a, clock)
            current = self.load(); a = current["attempts"][-1]
        if a and a["functional"] and (not a["functional"]["quiescent"] or a["functional"]["receipt"] is None):
            self._poll_functional(backend, a["functional"], clock)
            current = self.load(); a = current["attempts"][-1]
        now = clock()
        if a and a["response"] is not None and a["classified"] is None:
            current = self._append("classified", self._classify(current, a), now=clock)
            a = current["attempts"][-1]
        control = current["control"]
        if control and control["action"] == "cancel":
            if a and a["functional"] and not a["functional"]["quiescent"]:
                backend.cancel_check(functional.resource(self.json(a["functional"]["binding"])))
                return {"status": "cancel_requested"}
            if a and a["intent"] and not a["quiescent"]:
                self._cancel_worker(backend, a, clock)  # ACK has no acceptance meaning.
                return {"status": "cancel_requested"}
            return self._terminal(current, "cancelled", clock)
        if current["paused"]:
            return {"status": "paused"}
        if clock() >= current["contract"]["deadline_at"]:
            if a and a["functional"] and not a["functional"]["quiescent"]:
                backend.cancel_check(functional.resource(self.json(a["functional"]["binding"])))
                return {"status": "deadline_cleanup_pending"}
            if a and a["intent"] and not a["quiescent"]:
                self._cancel_worker(backend, a, clock)
                return {"status": "deadline_cleanup_pending"}
            return self._terminal(current, "exhausted", clock)
        if control and control["action"] == "pause":
            if a and a["functional"] and not a["functional"]["quiescent"]:
                return {"status": "pause_requested"}
            if a and a["intent"] and not a["quiescent"]:
                return {"status": "pause_requested"}
            self._append("paused", {"request_id": control["request_id"]}, now=clock)
            return {"status": "paused"}
        if a and a["intent"] and not a["quiescent"]:
            return {"status": "reconciling", "run_id": a["admission"]["run_id"]}
        if a and a["intent"] and a["classified"] is None:
            return {"status": "blocked", "dependency": "original_delivery_missing; reconcile_same_run_without_resending"}
        if a and self._delivery_veto(a):
            return {"status": "blocked", "dependency": "conflicting_or_unretained_delivery; no_acceptance"}
        if a and a["classified"] is not None and not a["settled"]:
            classified = a["classified"]
            if not classified["valid"]:
                feedback = {"reason": "invalid_delivery", "detail": classified["error"]}
            elif classified["message"]["type"] == "request_decision":
                if a["decision"] is None:
                    return {"status": "decision_required", "request": classified["message"],
                            "request_id": self._decision_id(a), "response_sha256": a["response"]["final"]}
                feedback = {"reason": "decision_resolved", "detail": a["decision"]["answer"]}
            else:
                if a["revision"] is None:
                    revision = self._freeze(current, a)
                    current = self._append("revision", {"revision_sha256": revision}, now=clock)
                    a = current["attempts"][-1]
                spec = current["contract"]["prepared"]["sources"]
                if spec["functional"] is not None:
                    if a["functional"] is None:
                        if backend.supports_functional is not True:
                            return {"status": "blocked", "dependency": "offline_backend_has_no_functional_adapter"}
                        revision = self.json(a["revision"])
                        bound = functional.binding(current["contract"], a["revision"], revision)
                        tests = spec["functional_tests"].encode()
                        if self.put(tests) != spec["functional"]["tests"]["sha256"]:
                            raise CycleError("frozen functional oracle identity mismatch")
                        self._append("functional_intent", {"binding_sha256": self.put_json(bound)}, now=clock)
                        backend.start_check(copy.deepcopy(bound), self.get(revision["tree"]), tests)
                        return {"status": "functional_started"}
                    if not a["functional"]["quiescent"]:
                        return {"status": "functional_reconciling"}
                    if a["functional"]["receipt"] is None:
                        return {"status": "blocked", "dependency": "original_functional_delivery_missing; reconcile_same_check"}
                if a["checks"] is None:
                    checks = self.put_json(self._checks(current, a["revision"]))
                    current = self._append("checked", {"checks_sha256": checks}, now=clock)
                    a = current["attempts"][-1]
                checks = self.json(a["checks"])
                if checks != self._checks(current, a["revision"]):
                    return {"status": "blocked", "dependency": "evidence_changed_after_immutable_check"}
                if checks["accepted"]:
                    return self._terminal(current, "accepted_contract", clock)
                if checks["pending"]:
                    return {"status": "blocked", "dependency": checks["pending"], "revision": a["revision"]}
                feedback = self._repair_feedback(a["checks"], checks)
            self._append("settled", feedback, now=clock)
            return {"status": "repair_ready"}
        if a is None or a["settled"]:
            ordinal = len(current["attempts"]) + 1
            if ordinal > current["contract"]["limits"]["max_attempts"]:
                return self._terminal(current, "exhausted", clock)
            task = contracts.task(current["contract"], attempt=ordinal, feedback=current["feedback"], decisions=current["decisions"])
            admission = contracts.admission(current["contract"], ordinal=ordinal,
                run_id=str(uuid.uuid4()), admission_id=str(uuid.uuid4()), generation=str(uuid.uuid4()),
                agent_session=None if "surfaces" in current["contract"] else str(uuid.uuid4()),
                turn_id=None if "surfaces" in current["contract"] else str(uuid.uuid4()), prompt_sha256=self.put_json(task),
                parent_revision=current["revisions"][-1] if current["revisions"] else None)
            current = self._append("admitted", {"admission": admission, "task": admission["prompt_sha256"]}, now=clock)
            a = current["attempts"][-1]
        if a["admission"]["version"] == "owner-cycle-admission-v2" and a["native_baseline"] is None:
            snapshot = native.retain(backend.before(copy.deepcopy(a["admission"])), self.put)
            current = self._append("native_baseline", {"admission_sha256": work.digest(a["admission"]),
                                   "snapshot": self.put_json(snapshot)}, now=clock)
            a = current["attempts"][-1]
        if "public_read" in current["contract"]["prepared"]["sources"] and a["input_snapshot"] is None:
            snapshot=scope.capture(Path(current["baseline"]["inventory"]["root"]),current["contract"]["prepared"]["sources"]["scope"])
            current=self._append("input_snapshot",{"admission_sha256":work.digest(a["admission"]),"snapshot":self.put_json(snapshot)},now=clock)
            a=current["attempts"][-1]
        # Intent is the linearization boundary. A recovered intent only polls.
        self._append("dispatch_intent", runtime.resource(a["admission"]), now=clock)
        backend.send(copy.deepcopy(a["admission"]), self.json(a["task"]))
        return {"status": "dispatched", "run_id": a["admission"]["run_id"]}

    def _terminal(self, current, status, now):
        a = current["attempts"][-1] if current["attempts"] else None
        current = self._append("terminal", {"status": status, "revision": a["revision"] if a else None,
            "checks": a["checks"] if a else None}, now=now)
        self._publish_seal(current)
        return self.verify()

    def _seal(self, current):
        return {"version": "owner-cycle-archive-v1", "contract_sha256": self.pin,
                "head": current["head"], "terminal": current["terminal"]}

    def _publish_seal(self, current):
        self._write("seal.json", self._seal(current))

    def verify(self):
        """Reproduce every binding/check from immutable artifacts, no candidate reads."""
        current = self.load()
        if not current["terminal"]:
            raise CycleError("cycle is not terminal")
        seal = self._read("seal.json", optional=True)
        if seal != self._seal(current):
            raise CycleError("archive seal missing or changed; recover its original publication")
        return {"status": current["terminal"]["status"], "offline_valid": True,
                "contract_sha256": self.pin, "archive_sha256": work.digest(seal),
                "attempts": len(current["attempts"]), "revisions": len(current["revisions"]),
                "live_execution": "NOT_VERIFIED", "semantic_success": "NOT_VERIFIED"}

    def retain_late(self, admission_sha256, *, transcript, final):
        """Quarantine a late message; never mutate or reopen the terminal journal."""
        current = self.load()
        if not current["terminal"] or admission_sha256 not in {work.digest(a["admission"]) for a in current["attempts"]}:
            raise CycleError("late evidence is not for a closed owned admission")
        late = {"admission_sha256": admission_sha256, "transcript": self.put(transcript),
                "final": self.put(final), "disposition": "late_unaccepted", "authority": "none"}
        self._write("late/" + work.digest(late) + ".json", late)
        return late

    def retain_late_check(self, binding_sha256, outcome):
        current = self.load()
        owned = {a["functional"]["binding"] for a in current["attempts"] if a["functional"]}
        if not current["terminal"] or binding_sha256 not in owned:
            raise CycleError("late functional evidence is not for a closed owned check")
        receipt = functional.receipt(self.json(binding_sha256), outcome, self.put)
        late = {"binding_sha256": binding_sha256, "receipt_sha256": self.put_json(receipt),
                "disposition": "late_unaccepted", "authority": "none"}
        self._write("late-functional/" + work.digest(late) + ".json", late)
        return late
