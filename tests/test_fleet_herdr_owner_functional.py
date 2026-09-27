"""Functional journal conformance. All container observations are synthetic."""
import copy
import os
import traceback
import uuid
from unittest import mock

from tests.test_fleet_herdr_owner_cycle import CycleFixture, Backend
from tests.test_fleet_herdr_research import functional_outcome
import fleet_functional_runner as runner
import fleet_herdr_owner_contract as contracts
import fleet_herdr_owner_cycle as cycle
import fleet_herdr_owner_functional as functional
import fleet_herdr_owner_runtime as runtime
import fleet_herdr_work_packet as work
import fleet_json
import fleet_safe_paths as safe

GOOD = '''def stats(values):
    if not values: raise ValueError()
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in values): raise TypeError()
    return {"count": len(values), "sum": sum(values), "mean": sum(values) / len(values)}
'''


class FunctionalBackend(Backend):
    supports_functional = True

    def __init__(self, *args):
        super().__init__(*args)
        self.checks, self.check_results, self.check_cancels = [], {}, []
        self.check_active = False
        self.hide_check = False
        self.check_crash = False
        self.check_actions = ["passed"]

    def send(self, admission, task):
        (self.candidate / "sample_stats.py").write_text(GOOD)
        return super().send(admission, task)

    def start_check(self, binding, tree, tests):
        self.checks.append(binding)
        outcome = functional_outcome(self.contract["prepared"]["sources"]["functional"],
                                     binding["native_contract"]["attempt_id"], self.candidate)
        answers = []
        for values in runner.CALLS:
            error = "ValueError" if not values else "TypeError" if any(type(v) not in (int, float) for v in values) else None
            value = None if error else {"count": len(values), "sum": sum(values), "mean": sum(values)/len(values)}
            answers.append({"value": value, "error": error, "input_after": list(values)})
        hello = outcome["evidence"]["stdout.txt"].splitlines()[0]
        outcome["evidence"]["stdout.txt"] = hello+b"\n"+fleet_json.canonical_bytes({"kind": "answers", "answers": answers})+b"\n"
        mode = self.check_actions[len(self.checks)-1]
        if mode == "failed":
            outcome["status"] = "failed"
            outcome["reason"] = "synthetic failed check"
        elif mode == "false_pass":
            answers[-1]["value"]["sum"] = -999
            outcome["evidence"]["stdout.txt"] = hello+b"\n"+fleet_json.canonical_bytes({"kind": "answers", "answers": answers})+b"\n"
        self.check_results[work.digest(binding)] = {"binding_sha256": work.digest(binding), "outcome": outcome}
        if self.check_crash:
            raise RuntimeError("post-check-send loss")

    def poll_check(self, binding):
        retained = self.check_results.get(work.digest(binding), {"binding_sha256": work.digest(binding), "outcome": None})
        return {"binding_sha256": retained["binding_sha256"], "outcome": None if self.hide_check else retained["outcome"],
                "quiescence": None if self.check_active else {"resource": functional.resource(binding),
                                                              "inactive": True, "resources_clean": True}}

    def cancel_check(self, resource):
        self.check_cancels.append(resource)
        return True


class OwnerFunctionalTests(CycleFixture):
    def make_functional(self):
        prepared = self.prepare(functional_contract=self.spec(), work_context={})
        contract = contracts.create(prepared, cycle_id=str(uuid.uuid4()), started_at=1000,
            budget={"max_attempts": 3, "deadline_seconds": 600, "token_limit": None, "cost_limit_usd": None})
        store = self.root / "functional-owner-runs"
        store.mkdir(exist_ok=True)
        return cycle.Cycle.create(store, contract), FunctionalBackend(contract, self.target)

    def start(self, owner, backend):
        self.assertEqual(owner.tick(backend, now=1001)["status"], "dispatched")
        self.assertEqual(owner.tick(backend, now=1002)["status"], "functional_started")

    def test_functional_repair_new_revision_and_native_attempt_without_docker(self):
        owner, backend = self.make_functional()
        backend.actions = ["good", "good"]
        backend.check_actions = ["failed", "passed"]
        with mock.patch.object(runner, "Docker", side_effect=AssertionError("Docker invoked")):
            result = self.drive(owner, backend)
            self.assertEqual(result["status"], "accepted_contract")
            self.assertTrue(owner.verify()["offline_valid"])
        self.assertEqual(len(backend.checks), 2)
        first, second = backend.checks
        self.assertEqual(first["native_contract"]["tree_sha"], second["native_contract"]["tree_sha"])
        self.assertNotEqual(first["revision_sha256"], second["revision_sha256"])
        self.assertNotEqual(first["native_contract"]["attempt_id"], second["native_contract"]["attempt_id"])
        attempts = owner.load()["attempts"]
        first_receipt = owner.json(attempts[0]["functional"]["receipt"])
        with self.assertRaises(ValueError):
            functional.verify(second, first_receipt, owner.get, backend.contract["prepared"]["sources"]["functional_tests"].encode())

    def test_false_pass_does_not_reproduce_retained_rpc(self):
        owner, backend = self.make_functional()
        backend.check_actions = ["false_pass"]
        result = self.drive(owner, backend)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("invalid_functional_evidence" in x for x in result["dependency"]))
        self.assertIsNone(owner.load()["terminal"])

    def test_ambiguous_check_send_is_not_repeated(self):
        owner, backend = self.make_functional()
        backend.check_crash = True
        owner.tick(backend, now=1001)
        with self.assertRaisesRegex(RuntimeError, "post-check"):
            owner.tick(backend, now=1002)
        self.assertEqual(owner.tick(backend, now=1003)["status"], "accepted_contract")
        self.assertEqual(len(backend.checks), 1)

    def test_malformed_native_evidence_blocks_without_recovery_crash(self):
        for name, value in (("container-state.json", []), ("container-state.json", None),
                            ("container-config.json", {"Mounts": [], "Config": []})):
            with self.subTest(name=name, value=value):
                owner, backend = self.make_functional()
                self.start(owner, backend)
                evidence = backend.check_results[work.digest(backend.checks[0])]["outcome"]["evidence"]
                evidence[name] = fleet_json.canonical_bytes(value)
                first = owner.tick(backend, now=1003)
                self.assertEqual(first["status"], "blocked")
                self.assertTrue(any("invalid_functional_evidence" in x for x in first["dependency"]))
                owner = cycle.Cycle(owner.runs, owner.cycle_id, contract_sha256=owner.pin)
                self.assertEqual(owner.tick(backend, now=1004), first)
                self.assertIsNone(owner.load()["terminal"])

    def test_interruption_before_check_send_does_not_replay(self):
        owner, backend = self.make_functional()
        owner.tick(backend, now=1001)
        with mock.patch.object(backend, "start_check", side_effect=RuntimeError("before send")):
            with self.assertRaises(RuntimeError):
                owner.tick(backend, now=1002)
        self.assertEqual(owner.tick(backend, now=1003)["status"], "blocked")
        self.assertFalse(backend.checks)

    def test_check_result_after_cleanup_is_reconciled(self):
        owner, backend = self.make_functional()
        self.start(owner, backend)
        backend.hide_check = True
        self.assertEqual(owner.tick(backend, now=1003)["status"], "blocked")
        backend.hide_check = False
        self.assertEqual(owner.tick(backend, now=1004)["status"], "accepted_contract")
        self.assertEqual(len(backend.checks), 1)

    def test_cancel_and_pause_require_independent_check_cleanup(self):
        for action in ("cancel", "pause"):
            with self.subTest(action=action):
                owner, backend = self.make_functional()
                backend.check_active = True
                self.start(owner, backend)
                request = str(uuid.uuid4())
                owner.control(action, request_id=request, target=runtime.resource(backend.sent[0]), reason="check ongoing", now=1003)
                self.assertEqual(owner.tick(backend, now=1004)["status"], action+"_requested")
                self.assertIsNone(owner.load()["terminal"])
                if action == "cancel":
                    self.assertEqual(backend.check_cancels, [functional.resource(backend.checks[0])])
                backend.check_active = False
                self.assertEqual(owner.tick(backend, now=1005)["status"], "cancelled" if action == "cancel" else "paused")
                if action == "pause":
                    owner.resume(request, now=1006)
                    self.assertEqual(owner.tick(backend, now=1007)["status"], "accepted_contract")

    def test_check_deadline_drains_its_own_resource(self):
        owner, backend = self.make_functional()
        backend.check_active = True
        self.start(owner, backend)
        self.assertEqual(owner.tick(backend, now=1600)["status"], "deadline_cleanup_pending")
        self.assertEqual(backend.check_cancels, [functional.resource(backend.checks[0])])
        backend.check_active = False
        self.assertEqual(owner.tick(backend, now=1601)["status"], "exhausted")

    def test_pending_pause_cannot_bypass_functional_deadline(self):
        owner, backend = self.make_functional()
        backend.check_active = True
        self.start(owner, backend)
        owner.control("pause", request_id=str(uuid.uuid4()), target=runtime.resource(backend.sent[0]), reason="pause", now=1003)
        self.assertEqual(owner.tick(backend, now=1600)["status"], "deadline_cleanup_pending")
        self.assertEqual(backend.check_cancels, [functional.resource(backend.checks[0])])
        backend.check_active = False
        self.assertEqual(owner.tick(backend, now=1601)["status"], "exhausted")

    def test_foreign_failed_check_is_not_rebound_to_new_revision(self):
        owner, backend = self.make_functional()
        backend.actions = ["good", "good"]
        backend.check_actions = ["failed", "passed"]
        self.start(owner, backend)
        first = copy.deepcopy(backend.check_results[work.digest(backend.checks[0])])
        self.assertEqual(owner.tick(backend, now=1003)["status"], "repair_ready")
        owner.tick(backend, now=1004)
        owner.tick(backend, now=1005)
        backend.check_results[work.digest(backend.checks[1])] = first
        self.assertEqual(owner.tick(backend, now=1006)["status"], "blocked")
        latest = owner.load()["attempts"][-1]["functional"]
        self.assertIsNone(latest["receipt"])
        self.assertTrue(latest["errors"])

    def test_error_after_checked_cannot_use_stale_pass_to_close(self):
        owner, backend = self.make_functional()
        self.start(owner, backend)
        with mock.patch.object(owner, "_terminal", side_effect=RuntimeError("before close")):
            with self.assertRaises(RuntimeError):
                owner.tick(backend, now=1003)
        attempt = owner.load()["attempts"][-1]
        self.assertTrue(owner.json(attempt["checks"])["accepted"])
        owner._append("functional_observation_failed", {"binding_sha256": attempt["functional"]["binding"],
            "kind": "retention", "detail": "conflicting later evidence"}, now=1004)
        self.assertEqual(owner.tick(backend, now=1005)["status"], "blocked")
        self.assertIsNone(owner.load()["terminal"])

    def test_bad_check_observation_cannot_prevent_exact_cancel(self):
        owner, backend = self.make_functional()
        backend.check_active = True
        self.start(owner, backend)
        owner.control("cancel", request_id=str(uuid.uuid4()), target=runtime.resource(backend.sent[0]), reason="stop", now=1003)
        invalid = [None] + [{**backend.poll_check(backend.checks[0]), "binding_sha256": value}
                            for value in (b"wrong-type", None, 12, [], {}, "invalid", "f" * 64)]
        for observation in invalid:
            with self.subTest(observation=observation and observation["binding_sha256"]):
                with mock.patch.object(backend, "poll_check", return_value=observation):
                    self.assertEqual(owner.tick(backend, now=1004)["status"], "cancel_requested")
        self.assertEqual(backend.check_cancels, [functional.resource(backend.checks[0])] * len(invalid))
        self.assertIsNone(owner.load()["terminal"])

    def test_retention_error_after_settled_vetoes_replayed_admission(self):
        owner, backend = self.make_functional()
        backend.check_actions = ["failed"]
        self.start(owner, backend)
        self.assertEqual(owner.tick(backend, now=1003)["status"], "repair_ready")
        current = owner.load()
        previous = current["attempts"][-1]
        owner._append("functional_observation_failed", {"binding_sha256": previous["functional"]["binding"],
            "kind": "retention", "detail": "late conflicting evidence after settlement"}, now=1004)
        task = contracts.task(current["contract"], attempt=2, feedback=current["feedback"], decisions=current["decisions"])
        admission = contracts.admission(current["contract"], ordinal=2, run_id=str(uuid.uuid4()),
            admission_id=str(uuid.uuid4()), generation=str(uuid.uuid4()), agent_session=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()), prompt_sha256=owner.put_json(task), parent_revision=previous["revision"])
        with self.assertRaisesRegex(cycle.CycleError, "prior evidence"):
            owner._append("admitted", {"admission": admission, "task": admission["prompt_sha256"]}, now=1005)
        self.assertEqual(owner.tick(backend, now=1006)["status"], "blocked")
        self.assertEqual(len(backend.sent), 1)
        self.assertIsNone(owner.load()["terminal"])

    def test_changed_check_delivery_cannot_be_accepted(self):
        owner, backend = self.make_functional()
        backend.check_active = True
        self.start(owner, backend)
        self.assertEqual(owner.tick(backend, now=1003)["status"], "functional_reconciling")
        backend.check_results[work.digest(backend.checks[0])]["outcome"]["reason"] = "changed result"
        backend.check_active = False
        self.assertEqual(owner.tick(backend, now=1004)["status"], "blocked")
        self.assertTrue(owner.load()["attempts"][0]["functional"]["conflicts"])

    def test_late_functional_result_after_cancel_never_reopens(self):
        owner, backend = self.make_functional()
        self.start(owner, backend)
        backend.hide_check = True
        owner.control("cancel", request_id=str(uuid.uuid4()), target=runtime.resource(backend.sent[0]), reason="stop", now=1003)
        result = owner.tick(backend, now=1004)
        self.assertEqual(result["status"], "cancelled")
        head = owner.load()["head"]
        binding = owner.load()["attempts"][0]["functional"]["binding"]
        late = owner.retain_late_check(binding, backend.check_results[binding]["outcome"])
        self.assertEqual(late["disposition"], "late_unaccepted")
        self.assertEqual(owner.load()["head"], head)
        self.assertEqual(owner.verify(), result)

    def test_functional_publication_crashes_recover_without_second_start(self):
        for boundary in ("after_atomic_partial_write", "after_atomic_pending_fsync", "after_atomic_rename"):
            for kind in ("functional_intent", "functional_observed", "functional_quiescent"):
                with self.subTest(boundary=boundary, kind=kind):
                    owner, backend = self.make_functional()
                    owner.tick(backend, now=1001)
                    if kind != "functional_intent":
                        owner.tick(backend, now=1002)
                    pid = os.fork()
                    if pid == 0:
                        try:
                            original = owner._write
                            def write(path, value):
                                if path.startswith("events/") and value["kind"] == kind:
                                    with mock.patch.object(safe, "_atomic_write_checkpoint", side_effect=lambda name: os._exit(73) if name == boundary else None):
                                        return original(path, value)
                                return original(path, value)
                            with mock.patch.object(owner, "_write", side_effect=write):
                                owner.tick(backend, now=1003)
                            os._exit(74)
                        except BaseException:
                            traceback.print_exc()
                        finally:
                            # A failed child must never return into the parent's test runner.
                            os._exit(75)
                    _, code = os.waitpid(pid, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(code), 73)
                    owner.recover()
                    result = owner.tick(backend, now=1004)
                    if kind == "functional_intent":
                        self.assertEqual(result["status"], "blocked")
                        self.assertFalse(backend.checks)
                    else:
                        self.assertEqual(result["status"], "accepted_contract")
                        self.assertEqual(len(backend.checks), 1)
