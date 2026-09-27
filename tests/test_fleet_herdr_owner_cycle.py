"""Real journal/CAS/freeze/acceptance, explicitly synthetic provider-free backend."""
from __future__ import annotations

import copy
import json
import os
import shutil
import uuid
from unittest import mock

from tests.test_fleet_herdr_work_packet import PacketFixture
import fleet_herdr_owner_contract as contracts
import fleet_herdr_owner_cycle as cycle
import fleet_herdr_owner_runtime as runtime
import fleet_herdr_work_packet as work
import fleet_json
import fleet_safe_paths as safe
import fleet_artifacts as artifacts


def terminal(contract, admission, task, final):
    policy = contract["prepared"]["sources"]["permissions"]
    expected = contract["runtime"]
    rows = [
        {"type": "session_meta", "payload": {"id": admission["agent_session"],
            "model_provider": expected["provider"], "cli_version": expected["cli_version"]}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": admission["turn_id"]}},
        {"type": "turn_context", "payload": {"turn_id": admission["turn_id"],
            **{k: policy[k] for k in ("cwd", "approval_policy", "sandbox_policy")},
            "model": expected["model"], "effort": expected["effort"]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": fleet_json.canonical_bytes(task).decode()}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": final.decode()}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": admission["turn_id"],
            "last_agent_message": final.decode()}}]
    return b"".join(fleet_json.canonical_bytes(r) + b"\n" for r in rows)


class Backend(cycle.OfflineBackend):
    def __init__(self, contract, candidate, actions=("good",)):
        self.contract, self.candidate, self.actions = contract, candidate, list(actions)
        self.sent, self.cancelled, self.results = [], [], {}
        self.active = False
        self.hide_response = False
        self.crash_after_send = False

    def send(self, admission, task):
        self.sent.append(copy.deepcopy(admission))
        action = self.actions[len(self.sent) - 1]
        if action in {"good", "wrong"}:
            (self.candidate / "answer.txt").write_text("implemented\n" if action == "good" else "wrong\n")
        message = {"type": "submit_candidate", "summary": "Synthetic submission", "paths": ["answer.txt"], "checks": []}
        if action == "decision":
            message = {"type": "request_decision", "question": "Which permitted wording?",
                "why_needed": "A material ambiguity", "options": [], "recommendation": None, "work_completed": "baseline read"}
        final = b"not JSON" if action == "invalid" else fleet_json.canonical_bytes(message)
        self.results[admission["run_id"]] = {"final": final, "transcript": terminal(self.contract, admission, task, final)}
        if self.crash_after_send:
            raise RuntimeError("synthetic post-send loss")

    def poll(self, admission):
        return {"response": None if self.hide_response else self.results.get(admission["run_id"]),
            "quiescence": None if self.active else {"resource": runtime.resource(admission), "inactive": True, "resources_clean": True}}

    def cancel(self, target):
        self.cancelled.append(target)
        return True  # An ACK deliberately does not change active or prove cleanup.


class CycleFixture(PacketFixture):
    def make(self, *, attempts=4, **changes):
        prepared = self.prepare(functional_contract=None, work_context={})
        contract = contracts.create(prepared, cycle_id=str(uuid.uuid4()), started_at=1000,
            budget={"max_attempts": attempts, "deadline_seconds": 600, "token_limit": None, "cost_limit_usd": None}, **changes)
        store = self.root / "owner-runs"
        store.mkdir(exist_ok=True)
        result = cycle.Cycle.create(store, contract)
        return result, Backend(contract, self.target)

    def drive(self, owner, backend):
        start = max(1001, owner.load()["last_at"] + 1)
        for i in range(30):
            result = owner.tick(backend, now=start+i)
            if result["status"] in {"accepted_contract", "exhausted", "cancelled", "blocked", "decision_required", "paused"}:
                return result
        self.fail("cycle failed to reach a bounded outcome")


class OwnerCycleTests(CycleFixture):
    def test_submit_check_repair_deliver_and_verify_without_mutable_candidate(self):
        owner, backend = self.make()
        backend.actions = ["wrong", "good"]
        result = self.drive(owner, backend)
        self.assertEqual(result["status"], "accepted_contract")
        current = owner.load()
        self.assertEqual(len(current["attempts"]), 2)
        self.assertEqual(len(current["revisions"]), 2)
        first, second = map(owner.json, current["revisions"])
        self.assertEqual(second["parent_revision"], current["revisions"][0])
        self.assertNotEqual(first["tree"], second["tree"])
        self.assertFalse(owner.json(current["attempts"][0]["checks"])["accepted"])
        self.assertTrue(owner.json(current["attempts"][1]["checks"])["accepted"])
        shutil.rmtree(self.target)
        self.assertEqual(owner.verify(), result)
        self.assertEqual(owner.tick(backend, now=2000), result)
        self.assertEqual(len(backend.sent), 2)

    def test_invalid_delivery_retains_original_and_repair_is_new_attempt(self):
        owner, backend = self.make()
        backend.actions = ["invalid", "good"]
        self.assertEqual(self.drive(owner, backend)["status"], "accepted_contract")
        attempts = owner.load()["attempts"]
        self.assertEqual(owner.get(attempts[0]["response"]["final"]), b"not JSON")
        self.assertFalse(attempts[0]["classified"]["valid"])
        self.assertNotEqual(attempts[0]["admission"]["run_id"], attempts[1]["admission"]["run_id"])
        self.assertEqual(attempts[0]["admission"]["deadline_at"], attempts[1]["admission"]["deadline_at"])

    def test_post_send_crash_reconciles_without_duplicate_dispatch(self):
        owner, backend = self.make()
        backend.crash_after_send = True
        with self.assertRaisesRegex(RuntimeError, "post-send"):
            owner.tick(backend, now=1001)
        resumed = cycle.Cycle(owner.runs, owner.cycle_id, contract_sha256=owner.pin)
        self.assertEqual(self.drive(resumed, backend)["status"], "accepted_contract")
        self.assertEqual(len(backend.sent), 1)

    def test_crash_before_send_after_intent_never_resends(self):
        owner, backend = self.make()
        with mock.patch.object(backend, "send", side_effect=RuntimeError("before send")):
            with self.assertRaises(RuntimeError):
                owner.tick(backend, now=1001)
        self.assertEqual(owner.tick(backend, now=1002)["status"], "blocked")
        self.assertEqual(backend.sent, [])
        self.assertEqual(owner.tick(backend, now=1600)["status"], "exhausted")

    def test_late_delivery_after_quiescence_reconciles_same_attempt(self):
        owner, backend = self.make()
        owner.tick(backend, now=1001)
        backend.hide_response = True
        self.assertEqual(owner.tick(backend, now=1002)["status"], "blocked")
        backend.hide_response = False
        self.assertEqual(owner.tick(backend, now=1003)["status"], "accepted_contract")
        self.assertEqual(len(backend.sent), 1)

    def test_changed_delivery_before_quiescence_is_retained_and_blocks_acceptance(self):
        owner, backend = self.make()
        backend.active = True
        owner.tick(backend, now=1001)
        self.assertEqual(owner.tick(backend, now=1002)["status"], "reconciling")
        admission = backend.sent[0]
        final = b"not JSON"
        task = owner.json(owner.load()["attempts"][0]["task"])
        backend.results[admission["run_id"]] = {"final": final, "transcript": terminal(backend.contract, admission, task, final)}
        backend.active = False
        result = owner.tick(backend, now=1003)
        self.assertEqual(result["status"], "blocked")
        attempt = owner.load()["attempts"][0]
        self.assertEqual(owner.get(attempt["conflicts"][0]["final"]), final)
        self.assertIsNone(owner.load()["terminal"])
        self.assertFalse(owner.load()["revisions"])

    def test_create_is_idempotent_and_does_not_recapture_changed_candidate(self):
        owner, backend = self.make()
        before = owner.load()
        (self.target / "answer.txt").write_text("later candidate")
        again = cycle.Cycle.create(owner.runs, backend.contract)
        self.assertEqual(again.load(), before)

    def test_pause_before_dispatch_and_resume_preserve_deadline(self):
        owner, backend = self.make()
        request = str(uuid.uuid4())
        owner.control("pause", request_id=request, target=None, reason="test", now=1001)
        self.assertEqual(owner.tick(backend, now=1002)["status"], "paused")
        self.assertFalse(backend.sent)
        owner.resume(request, now=1003)
        self.assertEqual(self.drive(owner, backend)["status"], "accepted_contract")
        self.assertEqual(backend.sent[0]["deadline_at"], 1600)

    def test_pause_request_does_not_claim_inflight_has_stopped(self):
        owner, backend = self.make()
        backend.active = True
        owner.tick(backend, now=1001)
        request = str(uuid.uuid4())
        owner.control("pause", request_id=request, target=runtime.resource(backend.sent[0]), reason="test", now=1002)
        self.assertEqual(owner.tick(backend, now=1003)["status"], "pause_requested")
        self.assertFalse(owner.load()["paused"])
        backend.active = False
        self.assertEqual(owner.tick(backend, now=1004)["status"], "paused")
        self.assertFalse(owner.load()["revisions"])
        owner.resume(request, now=1005)
        self.assertEqual(owner.tick(backend, now=1006)["status"], "accepted_contract")

    def test_cancellation_requires_exact_resource_and_cleanup_not_ack(self):
        owner, backend = self.make()
        backend.active = True
        owner.tick(backend, now=1001)
        target = runtime.resource(backend.sent[0])
        wrong = {**target, "generation": str(uuid.uuid4())}
        with self.assertRaises(cycle.CycleError):
            owner.control("cancel", request_id=str(uuid.uuid4()), target=wrong, reason="test", now=1002)
        owner.control("cancel", request_id=str(uuid.uuid4()), target=target, reason="test", now=1002)
        self.assertEqual(owner.tick(backend, now=1003)["status"], "cancel_requested")
        self.assertIsNone(owner.load()["terminal"])
        self.assertEqual(backend.cancelled, [target])
        backend.active = False
        self.assertEqual(owner.tick(backend, now=1004)["status"], "cancelled")
        self.assertTrue(owner.load()["attempts"][0]["classified"]["valid"])
        self.assertFalse(owner.load()["revisions"])

    def test_late_result_after_cancellation_is_retained_without_reopening(self):
        owner, backend = self.make()
        owner.tick(backend, now=1001)
        a = backend.sent[0]
        backend.hide_response = True
        owner.control("cancel", request_id=str(uuid.uuid4()), target=runtime.resource(a), reason="test", now=1002)
        result = owner.tick(backend, now=1003)
        before = owner.load()["head"]
        late = owner.retain_late(work.digest(a), **backend.results[a["run_id"]])
        self.assertEqual(late["disposition"], "late_unaccepted")
        self.assertEqual(owner.load()["head"], before)
        self.assertEqual(owner.verify(), result)
        with self.assertRaises(cycle.CycleError):
            owner.control("pause", request_id=str(uuid.uuid4()), target=runtime.resource(a), reason="test", now=1004)

    def test_attempt_exhaustion_and_deadline_never_renew(self):
        owner, backend = self.make(attempts=2)
        backend.actions = ["invalid", "invalid"]
        self.assertEqual(self.drive(owner, backend)["status"], "exhausted")
        self.assertEqual(len(backend.sent), 2)
        self.assertEqual({a["deadline_at"] for a in backend.sent}, {1600})

    def test_deadline_requires_quiescence_before_exhaustion(self):
        owner, backend = self.make()
        backend.active = True
        owner.tick(backend, now=1001)
        self.assertEqual(owner.tick(backend, now=1600)["status"], "deadline_cleanup_pending")
        self.assertIsNone(owner.load()["terminal"])
        backend.active = False
        self.assertEqual(owner.tick(backend, now=1601)["status"], "exhausted")

    def test_decision_pins_request_and_does_not_amend_scope(self):
        owner, backend = self.make()
        backend.actions = ["decision", "good"]
        result = self.drive(owner, backend)
        self.assertEqual(result["status"], "decision_required")
        with self.assertRaises(cycle.CycleError):
            owner.decide("a"*64, "Use existing wording", now=1003)
        owner.decide(result["request_id"], "Use existing wording", now=1003)
        self.assertEqual(self.drive(owner, backend)["status"], "accepted_contract")
        self.assertEqual(owner.load()["contract"]["prepared"]["sources"]["scope"], self.scope)

    def test_old_revision_checks_cannot_accept_new_revision(self):
        owner, backend = self.make()
        backend.actions = ["wrong", "good"]
        owner.tick(backend, now=1001)
        owner.tick(backend, now=1002)
        first = owner.load()["attempts"][0]["checks"]
        owner.tick(backend, now=1003)
        original = owner._append
        def interrupt(kind, *args, **kwargs):
            if kind == "checked":
                raise RuntimeError("before second check publication")
            return original(kind, *args, **kwargs)
        with mock.patch.object(owner, "_append", side_effect=interrupt):
            with self.assertRaises(RuntimeError):
                owner.tick(backend, now=1004)
        pending = owner.load()["attempts"][-1]
        self.assertIsNotNone(pending["revision"])
        self.assertIsNone(pending["checks"])
        with self.assertRaises(cycle.CycleError):
            owner._append("checked", {"checks_sha256": first}, now=1005)
        self.assertEqual(owner.tick(backend, now=1006)["status"], "accepted_contract")

    def test_deadline_crossed_during_poll_or_freeze_cannot_accept(self):
        for operation in ("poll", "freeze"):
            with self.subTest(operation=operation):
                owner, backend = self.make()
                owner.tick(backend, now=1001)
                clock = [1599]
                obj, method = (backend, "poll") if operation == "poll" else (owner, "_freeze")
                original = getattr(obj, method)
                def slow(*args, **kwargs):
                    value = original(*args, **kwargs)
                    clock[0] = 1601
                    return value
                with mock.patch.object(obj, method, side_effect=slow), mock.patch.object(cycle.time, "time", side_effect=lambda: clock[0]):
                    result = owner.tick(backend)
                self.assertEqual(result["status"], "exhausted")
                self.assertEqual(owner.load()["last_at"], 1601)

    def test_partial_and_fsynced_event_publications_recover_exact_bytes(self):
        for boundary in ("after_atomic_partial_write", "after_atomic_pending_fsync", "after_atomic_rename"):
            for kind in ("admitted", "dispatch_intent", "response", "revision", "checked", "terminal"):
                with self.subTest(boundary=boundary, kind=kind):
                    owner, backend = self.make()
                    if kind not in {"admitted", "dispatch_intent"}:
                        owner.tick(backend, now=1001)
                    pid = os.fork()
                    if pid == 0:
                        original_write = owner._write
                        def write(relative, value):
                            if relative.startswith("events/") and value["kind"] == kind:
                                with mock.patch.object(safe, "_atomic_write_checkpoint", side_effect=lambda name: os._exit(73) if name == boundary else None):
                                    return original_write(relative, value)
                            return original_write(relative, value)
                        with mock.patch.object(owner, "_write", side_effect=write):
                            owner.tick(backend, now=1002)
                        os._exit(74)
                    _, result = os.waitpid(pid, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(result), 73)
                    # Reconciliation must recover complete CAS bytes even when
                    # the journal pending file is only half-written.
                    recovered = owner.recover()
                    self.assertGreater(recovered["seq"], 1)
                    if kind == "admitted":
                        self.assertEqual(owner.tick(backend, now=1003)["status"], "dispatched")
                        self.assertEqual(len(backend.sent), 1)
                    elif kind == "dispatch_intent":
                        self.assertEqual(owner.tick(backend, now=1003)["status"], "blocked")
                        self.assertFalse(backend.sent)
                        target = runtime.resource(recovered["attempts"][-1]["admission"])
                        owner.control("cancel", request_id=str(uuid.uuid4()), target=target, reason="ambiguous send", now=1004)
                        self.assertEqual(owner.tick(backend, now=1005)["status"], "cancelled")
                    else:
                        self.assertEqual(owner.tick(backend, now=1003)["status"], "accepted_contract")
                        self.assertEqual(len(backend.sent), 1)

    def test_verify_is_read_only_and_missing_seal_requires_explicit_recovery(self):
        owner, backend = self.make()
        original = self.drive(owner, backend)
        seal = owner.runs / owner.prefix / "seal.json"
        raw = seal.read_bytes()
        seal.unlink()  # Deliberate crash-state injection in this owned fixture.
        with self.assertRaises(cycle.CycleError):
            owner.verify()
        self.assertFalse(seal.exists())
        owner.recover()
        self.assertEqual(seal.read_bytes(), raw)
        with mock.patch.object(owner, "_write", side_effect=AssertionError("verification wrote")):
            self.assertEqual(owner.verify(), original)

    def test_control_request_is_durable_while_driver_lock_is_held(self):
        owner, backend = self.make()
        with safe.RootedFS(owner.runs) as fs:
            with fs.exclusive_lock(f"{owner.prefix}/.driver.lock", directory_modes=(0o700,)*3):
                owner.control("cancel", request_id=str(uuid.uuid4()), target=None, reason="stop before send", now=1001)
        self.assertEqual(owner.tick(backend, now=1002)["status"], "cancelled")
        self.assertFalse(backend.sent)

    def test_delayed_request_timestamp_does_not_lose_exact_cancellation(self):
        owner, backend = self.make()
        backend.active = True
        owner.tick(backend, now=1001)
        owner.tick(backend, now=1003)
        owner.control("cancel", request_id=str(uuid.uuid4()), target=runtime.resource(backend.sent[0]), reason="delayed scheduling", now=1002)
        self.assertEqual(owner.load()["last_at"], 1003)
        self.assertEqual(owner.load()["control"]["action"], "cancel")
        self.assertEqual(owner.tick(backend, now=1004)["status"], "cancel_requested")

    def test_failed_observation_or_retention_does_not_prevent_exact_cancel(self):
        for failure in ("transport", "oversized", "bad\x00reply", "😀"*4000, "\ud800"):
            with self.subTest(failure=failure):
                owner, backend = self.make()
                backend.active = True
                owner.tick(backend, now=1001)
                admission = backend.sent[0]
                owner.control("cancel", request_id=str(uuid.uuid4()), target=runtime.resource(admission), reason="stop", now=1002)
                if failure == "oversized":
                    backend.results[admission["run_id"]]["transcript"] = b"x"*(artifacts.MAX_ARTIFACT_BYTES+1)
                    self.assertEqual(owner.tick(backend, now=1003)["status"], "cancel_requested")
                    backend.active = False
                    self.assertEqual(owner.tick(backend, now=1004)["status"], "cancelled")
                else:
                    with mock.patch.object(backend, "poll", side_effect=RuntimeError(failure)):
                        self.assertEqual(owner.tick(backend, now=1003)["status"], "cancel_requested")
                self.assertEqual(backend.cancelled, [runtime.resource(admission)])
                self.assertTrue(owner.load()["attempts"][0]["observation_errors"])

    def test_malformed_observations_cannot_prevent_cancel_or_attest_quiescence(self):
        for observed in (None, {"response": {"transcript": "not bytes", "final": b"{}"}, "quiescence": None},
                         {"response": None, "quiescence": {"resource": {}, "inactive": True, "resources_clean": True}}):
            with self.subTest(observed=observed):
                owner, backend = self.make()
                backend.active = True
                owner.tick(backend, now=1001)
                owner.control("cancel", request_id=str(uuid.uuid4()), target=runtime.resource(backend.sent[0]), reason="stop", now=1002)
                with mock.patch.object(backend, "poll", return_value=observed):
                    self.assertEqual(owner.tick(backend, now=1003)["status"], "cancel_requested")
                self.assertEqual(len(backend.cancelled), 1)
                attempt = owner.load()["attempts"][0]
                self.assertFalse(attempt["quiescent"])
                self.assertTrue(attempt["observation_errors"])

    def test_existing_mission_is_not_reinterpreted_or_written(self):
        owner, backend = self.make()
        contract = copy.deepcopy(backend.contract)
        contract["cycle_id"] = str(uuid.uuid4())
        legacy_root = owner.runs / "missions" / contract["cycle_id"]
        legacy_root.mkdir(mode=0o700)
        ledger = legacy_root / "mission.jsonl"
        ledger.write_bytes(b"historical terminal bytes\n")
        ledger.chmod(0o600)
        with self.assertRaisesRegex(cycle.CycleError, "existing Mission"):
            cycle.Cycle.create(owner.runs, contract)
        self.assertEqual([p.name for p in legacy_root.iterdir()], ["mission.jsonl"])
        self.assertEqual(ledger.read_bytes(), b"historical terminal bytes\n")

    def test_reused_control_id_cannot_pause_again_or_change_target(self):
        owner, backend = self.make()
        request = str(uuid.uuid4())
        args = {"request_id": request, "target": None, "reason": "first pause"}
        owner.control("pause", **args, now=1001)
        owner.tick(backend, now=1002)
        owner.resume(request, now=1003)
        head = owner.load()["head"]
        owner.control("pause", **args, now=1004)
        self.assertEqual(owner.load()["head"], head)
        self.assertIsNone(owner.load()["control"])
        with self.assertRaises(cycle.CycleError):
            owner.control("pause", **{**args, "reason": "different pause"}, now=1004)
        next_request = str(uuid.uuid4())
        owner.control("pause", **{**args, "request_id": next_request}, now=1004)
        owner.tick(backend, now=1005)
        with self.assertRaises(cycle.CycleError):
            owner.resume(request, now=1006)
        self.assertTrue(owner.load()["paused"])

    def test_identical_questions_in_different_attempts_need_distinct_decisions(self):
        owner, backend = self.make()
        backend.actions = ["decision", "decision", "good"]
        first = self.drive(owner, backend)
        owner.decide(first["request_id"], "answer one", now=1004)
        second = self.drive(owner, backend)
        self.assertEqual(first["response_sha256"], second["response_sha256"])
        self.assertNotEqual(first["request_id"], second["request_id"])
        owner.decide(first["request_id"], "answer one", now=1010)
        self.assertIsNone(owner.load()["attempts"][-1]["decision"])
        self.assertEqual(owner.tick(backend, now=1011)["status"], "decision_required")
        owner.decide(second["request_id"], "answer two", now=1012)
        self.assertEqual(self.drive(owner, backend)["status"], "accepted_contract")

    def test_additional_requirements_and_functional_contract_do_not_silently_pass(self):
        for functional in (False, True):
            with self.subTest(functional=functional):
                prepared = self.prepare(functional_contract=self.spec() if functional else None,
                                        work_context={} if functional else {"additional_requirements": ["independently verify prose quality"]})
                contract = contracts.create(prepared, cycle_id=str(uuid.uuid4()), started_at=1000,
                    budget={"max_attempts": 2, "deadline_seconds": 600, "token_limit": None, "cost_limit_usd": None})
                store = self.root / "owner-runs"
                store.mkdir(exist_ok=True)
                owner = cycle.Cycle.create(store, contract)
                backend = Backend(contract, self.target)
                result = self.drive(owner, backend)
                self.assertEqual(result["status"], "blocked")
                self.assertIsNone(owner.load()["terminal"])
                self.assertEqual(len(backend.sent), 1)

    def test_wrong_native_turn_and_cross_cycle_rebinding_fail(self):
        owner, backend = self.make(attempts=1)
        owner.tick(backend, now=1001)
        response = backend.results[backend.sent[0]["run_id"]]
        rows = fleet_json.load_jsonl(response["transcript"])
        rows[2]["payload"]["turn_id"] = "another-turn"
        response["transcript"] = b"".join(fleet_json.canonical_bytes(r)+b"\n" for r in rows)
        self.assertEqual(self.drive(owner, backend)["status"], "exhausted")
        other = copy.deepcopy(backend.contract)
        other["cycle_id"] = str(uuid.uuid4())
        self.assertNotEqual(contracts.task(other, attempt=1, feedback=None, decisions=[]),
                            contracts.task(backend.contract, attempt=1, feedback=None, decisions=[]))

    def test_scope_ignored_residue_is_not_accepted_and_is_preserved(self):
        owner, backend = self.make(attempts=1)
        owner.tick(backend, now=1001)
        (self.target / "rogue.cache").write_text("not in permitted scope")
        self.assertEqual(self.drive(owner, backend)["status"], "exhausted")
        checks = owner.json(owner.load()["attempts"][0]["checks"])
        self.assertFalse(checks["accepted"])
        self.assertEqual(checks["scope"]["status"], "rejected")
        self.assertTrue((self.target / "rogue.cache").exists())

    def test_live_backend_and_nonobservable_budgets_are_rejected(self):
        owner, backend = self.make()
        with self.assertRaises(cycle.CycleError):
            owner.tick(object(), now=1001)
        for field in ("token_limit", "cost_limit_usd"):
            modified = copy.deepcopy(backend.contract)
            modified["limits"][field] = 100
            with self.assertRaises(contracts.ContractError):
                contracts.validate(modified)
        for malformed in ({}, False):
            with self.assertRaises(contracts.ContractError):
                contracts.create(backend.contract["prepared"], cycle_id=str(uuid.uuid4()), started_at=1000,
                                 budget=backend.contract["limits"], runtime=malformed)

    def test_wrong_contract_anchor_and_nonboolean_dispatch_rejected(self):
        owner, backend = self.make()
        wrong = cycle.Cycle(owner.runs, owner.cycle_id, contract_sha256="a"*64)
        with self.assertRaises(cycle.CycleError):
            wrong.load()
        contract = copy.deepcopy(backend.contract)
        contract["profile"]["dispatch_enabled"] = 0
        with self.assertRaises(contracts.ContractError):
            contracts.validate(contract)

    def test_unbounded_continuation_rejected(self):
        owner, backend = self.make()
        with self.assertRaises(contracts.ContractError):
            contracts.task(backend.contract, attempt=1, feedback={"reason": "invalid_delivery", "detail": "x"*work.MAX_PACKET_BYTES}, decisions=[])


class RuntimeTests(CycleFixture):
    def setup_delivery(self):
        owner, backend = self.make(runtime={"cli": "codex", "cli_version": "0.155.1", "provider": "openai", "model": "gpt-6-astra", "effort": "max"})
        owner.tick(backend, now=1001)
        admission = backend.sent[0]
        response = backend.results[admission["run_id"]]
        return owner, backend, admission, response

    def test_observed_identity_original_bytes_and_unknown_consumption(self):
        owner, backend, admission, response = self.setup_delivery()
        result = runtime.verify_terminal(response["transcript"], response["final"], contract=backend.contract, admission=admission)
        self.assertEqual(result["observed"], backend.contract["runtime"])
        self.assertEqual(result["transcript_sha256"], owner.put(response["transcript"]))
        self.assertIsNone(result["usage"]["tokens"])
        self.assertIsNone(result["usage"]["cost_usd"])
        self.assertEqual(result["effective_isolation"], "NOT_VERIFIED")

    def test_terminal_counterexamples_are_rejected(self):
        _, backend, admission, response = self.setup_delivery()
        inserted = [
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "withdrawn"}]}},
            {"type": "response_item", "payload": {"type": "function_call", "call_id": "tool"}},
            {"type": "event_msg", "payload": {"type": "task_aborted"}},
            {"type": "turn_context", "payload": {"turn_id": admission["turn_id"]}}]
        for row in inserted:
            rows = fleet_json.load_jsonl(response["transcript"])
            rows.insert(-1, row)
            with self.subTest(row=row), self.assertRaises(ValueError):
                runtime.verify_terminal(b"".join(fleet_json.canonical_bytes(r)+b"\n" for r in rows), response["final"], contract=backend.contract, admission=admission)

    def test_extra_instructions_pending_tools_and_false_permissions_rejected(self):
        _, backend, admission, response = self.setup_delivery()
        for variant in ("skill", "tool", "permissions", "effort", "provider"):
            rows = fleet_json.load_jsonl(response["transcript"])
            if variant == "skill":
                rows.insert(3, {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<skill>new instructions</skill>"}]}})
            elif variant == "tool":
                rows.insert(4, {"type": "response_item", "payload": {"type": "function_call", "call_id": "unfinished"}})
            elif variant == "permissions":
                rows[2]["payload"]["sandbox_policy"]["network_access"] = True
            elif variant == "effort":
                rows[2]["payload"]["effort"] = "high"
            else:
                rows[0]["payload"]["model_provider"] = "other"
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                runtime.verify_terminal(b"".join(fleet_json.canonical_bytes(r)+b"\n" for r in rows), response["final"], contract=backend.contract, admission=admission)

    def test_forged_admission_is_rejected_at_adapter_boundary(self):
        _, backend, admission, response = self.setup_delivery()
        for key, value in (("writer", "reviewer"), ("contract_sha256", "a"*64), ("deadline_at", 99999)):
            bad = {**admission, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                runtime.verify_terminal(response["transcript"], response["final"], contract=backend.contract, admission=bad)

    def test_reused_tool_ids_cross_type_outputs_and_unknown_tools_rejected(self):
        _, backend, admission, response = self.setup_delivery()
        call = {"type": "response_item", "payload": {"type": "function_call", "call_id": "c"}}
        output = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c"}}
        variants = [[call, output, call, output],
            [call, {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c"}}],
            [{"type": "response_item", "payload": {"type": "local_shell_call", "status": "in_progress"}}]]
        for extra in variants:
            rows = fleet_json.load_jsonl(response["transcript"])
            rows[4:4] = extra
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                runtime.verify_terminal(b"".join(fleet_json.canonical_bytes(r)+b"\n" for r in rows), response["final"], contract=backend.contract, admission=admission)

    def test_claude_inspection_does_not_infer_provider_or_grant_admission(self):
        rows = [{"type": "system", "subtype": "init", "session_id": "s", "claude_code_version": "2.1.270"},
                {"type": "result", "subtype": "success", "session_id": "s", "is_error": False,
                 "stop_reason": "end_turn", "result": "original prose", "total_cost_usd": 0}]
        raw = lambda r: b"".join(fleet_json.canonical_bytes(x)+b"\n" for x in r)
        result = runtime.inspect_claude(raw(rows))
        self.assertFalse(result["admissible"])
        self.assertIsNone(result["provider"])
        self.assertIsNone(result["cost_usd"])
        for mutated in (rows+rows[-1:], [*rows[:1], {"type": "assistant", "session_id": "alien"}, *rows[1:]]):
            with self.assertRaises(ValueError):
                runtime.inspect_claude(raw(mutated))
