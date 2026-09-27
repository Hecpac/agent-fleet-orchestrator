"""Synthetic transport; real journal, CAS, snapshot and archive reproduction."""
from __future__ import annotations

import copy
import os
import shutil
import uuid
from unittest import mock

from tests.test_fleet_herdr_owner_cycle import CycleFixture, terminal
import fleet_herdr_effects as effects
import fleet_herdr_owner_contract as contracts
import fleet_herdr_owner_cycle as cycle
import fleet_herdr_owner_runtime as runtime
import fleet_herdr_owner_transport as transport
import fleet_herdr_work_packet as work
import fleet_json
import fleet_safe_paths as safe


def rows(raw):
    return fleet_json.load_jsonl(raw)


def encoded(value):
    return b"".join(fleet_json.canonical_bytes(r) + b"\n" for r in value)


class Runner:
    def __init__(self, owner, candidate):
        self.owner, self.candidate = owner, candidate
        self.sent, self.stopped, self.observations, self.complete = [], [], {}, {}
        self.actions = ["good"] * 4
        self.active, self.crash_after_send, self.race = False, False, None

    def observe(self, surface, resource):
        key = resource["run_id"]
        if key not in self.observations:
            self.observations[key] = {"resource": copy.deepcopy(resource), "receipt": {"agent": {
                "name": surface["agent_name"], **{k: surface[k] for k in ("workspace_id", "tab_id", "pane_id", "terminal_id")},
                "agent_session": None, "agent_status": "idle", "revision": 0}},
                "transcript": b"", "inactive": True, "resources_clean": True}
        return copy.deepcopy(self.observations[key])

    def send_if_unchanged(self, admission, argv, prompt, expected):
        current = self.observe(admission["surface"], runtime.resource(admission))
        if current != expected:
            return False
        self.sent.append((copy.deepcopy(admission), argv, prompt))
        bound = {**admission, "agent_session": str(uuid.uuid4()), "turn_id": str(uuid.uuid4())}
        action = self.actions[len(self.sent)-1]
        if action in {"good", "wrong"}:
            (self.candidate / "answer.txt").write_text("implemented\n" if action == "good" else "wrong\n")
        final = (b"not JSON" if action == "invalid" else fleet_json.canonical_bytes(
            {"type": "submit_candidate", "summary": "Synthetic result", "paths": ["answer.txt"], "checks": []}))
        raw = terminal(self.owner.json(self.owner.pin), bound, fleet_json.loads(prompt), final)
        self.complete[admission["run_id"]] = raw
        current["transcript"] = encoded(rows(raw)[:-2]) if self.active else raw
        current["receipt"]["agent"].update(agent_session={"agent": "codex", "source": "codex", "kind": "id", "value": bound["agent_session"]},
            agent_status="working" if self.active else "done", revision=1)
        current.update(inactive=not self.active, resources_clean=not self.active)
        self.observations[admission["run_id"]] = current
        if self.crash_after_send:
            raise RuntimeError("post-send transport loss")
        return True

    def stop_if_unchanged(self, admission, binding, expected):
        if self.race:
            self.race()
        current = self.observe(admission["surface"], runtime.resource(admission))
        if current != expected:
            return False
        self.stopped.append(copy.deepcopy(binding))
        return True  # ACK does not change transcript/process cleanup.

    def finish(self, admission, *, clean=True):
        current = self.observations[admission["run_id"]]
        current["transcript"] = self.complete[admission["run_id"]]
        current["receipt"]["agent"].update(agent_status="done", revision=2)
        current.update(inactive=True, resources_clean=clean)


class ObservedTransportTests(CycleFixture):
    def setup_cycle(self, *, attempts=3):
        surfaces = [{"herdr_session": "fixture", "agent_name": "worker-" + str(i), "workspace_id": "w1",
                     "tab_id": "t1", "pane_id": "p" + str(i), "terminal_id": "term" + str(i)} for i in range(attempts)]
        owner, _ = self.make(attempts=attempts, surfaces=surfaces, runtime={"cli": "codex", "cli_version": "0.155.1",
            "provider": "openai", "model": "gpt-6-astra", "effort": "max"})
        runner = Runner(owner, self.target)
        return owner, runner, transport.InjectedHerdrBackend(owner, runner=runner)

    def snapshot(self, owner, runner):
        admission = owner.load()["attempts"][-1]["admission"]
        return admission, runner.observations[admission["run_id"]]

    def test_lazy_native_binding_cycle_repair_and_portable_archive(self):
        owner, runner, backend = self.setup_cycle()
        runner.actions = ["invalid", "wrong", "good"]
        result = self.drive(owner, backend)
        self.assertEqual(result["status"], "accepted_contract")
        attempts = owner.load()["attempts"]
        self.assertEqual(len(attempts), 3)
        for attempt in attempts:
            self.assertIsNone(attempt["admission"]["agent_session"])
            self.assertIsNone(attempt["admission"]["turn_id"])
            self.assertTrue(attempt["native_binding"]["agent_session"])
            self.assertEqual(attempt["native_binding"]["resource"], runtime.resource(attempt["admission"]))
        self.assertEqual(owner.get(attempts[0]["response"]["final"]), b"not JSON")
        self.assertEqual(len({a["native_binding"]["agent_session"] for a in attempts}), 3)
        self.assertIn('model_reasoning_effort="max"', runner.sent[0][1])
        shutil.rmtree(self.target)
        self.assertEqual(owner.verify(), result)

    def test_real_transport_denied_before_effect(self):
        owner, _, _ = self.setup_cycle()
        with self.assertRaises(effects.EffectMediationDenied):
            transport.InjectedHerdrBackend(owner)

    def test_post_send_loss_reconciles_without_new_send(self):
        owner, runner, backend = self.setup_cycle()
        runner.crash_after_send = True
        with self.assertRaisesRegex(RuntimeError, "post-send"):
            owner.tick(backend, now=1001)
        resumed = cycle.Cycle(owner.runs, owner.cycle_id, contract_sha256=owner.pin)
        self.assertEqual(self.drive(resumed, transport.InjectedHerdrBackend(resumed, runner=runner))["status"], "accepted_contract")
        self.assertEqual(len(runner.sent), 1)

    def test_intent_without_send_is_not_empty_quiescence_or_retry(self):
        owner, runner, backend = self.setup_cycle()
        with mock.patch.object(backend, "send", side_effect=RuntimeError("before-send")):
            with self.assertRaises(RuntimeError):
                owner.tick(backend, now=1001)
        self.assertEqual(owner.tick(backend, now=1002)["status"], "reconciling")
        self.assertEqual(owner.tick(backend, now=1600)["status"], "deadline_cleanup_pending")
        self.assertFalse(runner.sent)
        self.assertFalse(owner.load()["attempts"][0]["quiescent"])

    def test_cancel_before_binding_keeps_target_and_accounts_late_result(self):
        owner, runner, backend = self.setup_cycle()
        runner.active = True
        owner.tick(backend, now=1001)
        admission, snapshot = self.snapshot(owner, runner)
        target = runtime.resource(admission)
        rid = str(uuid.uuid4())
        owner.control("cancel", request_id=rid, target=target, reason="fixture", now=1002)
        self.assertEqual(owner.tick(backend, now=1003)["status"], "cancel_requested")
        self.assertTrue(owner.load()["attempts"][0]["native_binding"])
        self.assertEqual(len(runner.stopped), 1)
        self.assertEqual(owner.load()["control"]["target"], target)
        self.assertIsNone(owner.load()["terminal"])
        runner.finish(admission)
        resumed = cycle.Cycle(owner.runs, owner.cycle_id, contract_sha256=owner.pin)
        self.assertEqual(resumed.tick(transport.InjectedHerdrBackend(resumed, runner=runner), now=1004)["status"], "cancelled")
        self.assertTrue(resumed.load()["attempts"][0]["classified"]["valid"])
        self.assertFalse(resumed.load()["revisions"])
        self.assertEqual(len(runner.sent), 1)

    def test_cancel_after_binding_does_not_signal_manual_turn(self):
        owner, runner, backend = self.setup_cycle()
        runner.active = True
        owner.tick(backend, now=1001)
        owner.tick(backend, now=1002)
        admission, snapshot = self.snapshot(owner, runner)
        owner.control("cancel", request_id=str(uuid.uuid4()), target=runtime.resource(admission), reason="fixture", now=1003)
        def manual_turn():
            snapshot["transcript"] += encoded([{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "manual"}}])
            snapshot["receipt"]["agent"]["revision"] += 1
        runner.race = manual_turn
        self.assertEqual(owner.tick(backend, now=1004)["status"], "cancel_requested")
        self.assertFalse(runner.stopped)
        self.assertEqual(owner.tick(backend, now=1005)["status"], "cancel_requested")
        self.assertFalse(runner.stopped)
        self.assertIsNone(owner.load()["terminal"])

    def test_cancel_signal_lost_ack_is_never_repeated_after_recovery(self):
        owner, runner, backend = self.setup_cycle()
        runner.active = True
        owner.tick(backend, now=1001)
        admission, _ = self.snapshot(owner, runner)
        owner.control("cancel", request_id=str(uuid.uuid4()), target=runtime.resource(admission), reason="fixture", now=1002)
        original = runner.stop_if_unchanged
        def lost_ack(*args):
            original(*args)
            raise RuntimeError("lost cancel ACK")
        with mock.patch.object(runner, "stop_if_unchanged", side_effect=lost_ack):
            with self.assertRaisesRegex(RuntimeError, "lost cancel ACK"):
                owner.tick(backend, now=1003)
        resumed = cycle.Cycle(owner.runs, owner.cycle_id, contract_sha256=owner.pin)
        self.assertEqual(resumed.tick(transport.InjectedHerdrBackend(resumed, runner=runner), now=1004)["status"], "cancel_requested")
        self.assertEqual(len(runner.stopped), 1)
        runner.finish(admission)
        self.assertEqual(resumed.tick(transport.InjectedHerdrBackend(resumed, runner=runner), now=1005)["status"], "cancelled")

    def test_invalid_receipt_after_cancel_is_retained_without_signalling(self):
        for value in (None, {"agent": {"revision": True}}, "foreign_terminal"):
            with self.subTest(value=value):
                owner, runner, backend = self.setup_cycle()
                runner.active = True
                owner.tick(backend, now=1001)
                owner.tick(backend, now=1002)
                admission, snapshot = self.snapshot(owner, runner)
                owner.control("cancel", request_id=str(uuid.uuid4()), target=runtime.resource(admission), reason="fixture", now=1003)
                original = copy.deepcopy(snapshot["receipt"])
                if value == "foreign_terminal":
                    snapshot["receipt"]["agent"]["terminal_id"] = "foreign"
                else:
                    snapshot["receipt"] = value
                rejected = copy.deepcopy(snapshot["receipt"])
                self.assertEqual(owner.tick(backend, now=1004)["status"], "cancel_requested")
                attempt = owner.load()["attempts"][0]
                self.assertTrue(attempt["observation_errors"])
                self.assertEqual(owner.json(attempt["native_observations"][-1])["receipt"], rejected)
                self.assertFalse(runner.stopped)
                snapshot["receipt"] = original
                runner.finish(admission)
                self.assertEqual(owner.tick(backend, now=1005)["status"], "cancelled")
                self.assertTrue(owner.load()["attempts"][0]["observation_errors"])

    def test_resume_reobserves_quiescent_surface_before_accepting(self):
        owner, runner, backend = self.setup_cycle()
        owner.tick(backend, now=1001)
        admission, snapshot = self.snapshot(owner, runner)
        rid = str(uuid.uuid4())
        owner.control("pause", request_id=rid, target=runtime.resource(admission), reason="fixture", now=1002)
        self.assertEqual(owner.tick(backend, now=1003)["status"], "paused")
        snapshot["transcript"] += encoded([{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "manual"}}])
        owner.resume(rid, now=1004)
        self.assertEqual(owner.tick(backend, now=1005)["status"], "reconciling")
        self.assertFalse(owner.load()["attempts"][0]["quiescent"])
        self.assertIsNone(owner.load()["terminal"])

    def test_receipt_identity_permission_and_resource_drift_rejected_and_retained(self):
        for field in ("terminal", "session", "source", "generation", "permissions", "prompt", "model"):
            with self.subTest(field=field):
                owner, runner, backend = self.setup_cycle()
                owner.tick(backend, now=1001)
                admission, snapshot = self.snapshot(owner, runner)
                if field == "terminal":
                    snapshot["receipt"]["agent"]["terminal_id"] = "foreign"
                elif field in {"session", "source"}:
                    snapshot["receipt"]["agent"]["agent_session"]["value" if field == "session" else "source"] = "foreign"
                elif field == "generation":
                    snapshot["resource"]["generation"] = str(uuid.uuid4())
                else:
                    data = rows(snapshot["transcript"])
                    if field == "permissions":
                        data[2]["payload"]["sandbox_policy"]["network_access"] = True
                    elif field == "model":
                        data[2]["payload"]["model"] = "foreign"
                    else:
                        data[3]["payload"]["content"][0]["text"] = "foreign prompt"
                    snapshot["transcript"] = encoded(data)
                owner.tick(backend, now=1002)
                attempt = owner.load()["attempts"][0]
                self.assertTrue(attempt["observation_errors"])
                self.assertIsNone(attempt["native_binding"])
                self.assertIsNone(owner.load()["terminal"])
                retained = owner.json(attempt["native_observations"][0])
                self.assertEqual(owner.get(retained["transcript"]), snapshot["transcript"])

    def test_snapshot_cannot_replace_session_after_binding(self):
        owner, runner, backend = self.setup_cycle()
        runner.active = True
        owner.tick(backend, now=1001)
        owner.tick(backend, now=1002)
        original = owner.load()["attempts"][0]["native_binding"]
        admission, snapshot = self.snapshot(owner, runner)
        sid = str(uuid.uuid4())
        data = rows(snapshot["transcript"])
        data[0]["payload"]["id"] = sid
        snapshot["transcript"] = encoded(data)
        snapshot["receipt"]["agent"]["agent_session"]["value"] = sid
        owner.tick(backend, now=1003)
        attempt = owner.load()["attempts"][0]
        self.assertEqual(attempt["native_binding"], original)
        self.assertTrue(attempt["observation_errors"])

    def test_partial_snapshot_retained_then_completed_without_guessing(self):
        owner, runner, backend = self.setup_cycle()
        owner.tick(backend, now=1001)
        admission, snapshot = self.snapshot(owner, runner)
        full = snapshot["transcript"]
        snapshot["transcript"] = full[:100]
        owner.tick(backend, now=1002)
        self.assertIsNone(owner.load()["attempts"][0]["native_binding"])
        snapshot["transcript"] = full
        self.assertEqual(owner.tick(backend, now=1003)["status"], "accepted_contract")

    def test_partial_append_does_not_erase_an_observed_binding(self):
        owner, runner, backend = self.setup_cycle()
        runner.active = True
        owner.tick(backend, now=1001)
        owner.tick(backend, now=1002)
        admission, snapshot = self.snapshot(owner, runner)
        original = owner.load()["attempts"][0]["native_binding"]
        snapshot["transcript"] = runner.complete[admission["run_id"]][:-5]
        owner.tick(backend, now=1003)
        attempt = owner.load()["attempts"][0]
        self.assertEqual(attempt["native_binding"], original)
        self.assertFalse(attempt["observation_errors"])
        runner.finish(admission)
        self.assertEqual(owner.tick(backend, now=1004)["status"], "accepted_contract")

    def test_idle_and_ack_do_not_replace_native_terminal_and_cleanup(self):
        owner, runner, backend = self.setup_cycle()
        runner.active = True
        owner.tick(backend, now=1001)
        admission, snapshot = self.snapshot(owner, runner)
        snapshot["receipt"]["agent"]["agent_status"] = "idle"
        snapshot.update(inactive=True, resources_clean=True)
        self.assertEqual(owner.tick(backend, now=1002)["status"], "reconciling")
        runner.finish(admission, clean=False)
        self.assertEqual(owner.tick(backend, now=1003)["status"], "reconciling")
        snapshot["resources_clean"] = True
        self.assertEqual(owner.tick(backend, now=1004)["status"], "accepted_contract")

    def test_post_final_tool_activity_not_cut_from_snapshot(self):
        owner, runner, backend = self.setup_cycle()
        owner.tick(backend, now=1001)
        admission, snapshot = self.snapshot(owner, runner)
        snapshot["transcript"] += encoded([{"type": "response_item", "payload": {"type": "function_call", "call_id": "late", "name": "write"}}])
        result = owner.tick(backend, now=1002)
        attempt = owner.load()["attempts"][0]
        self.assertFalse(attempt["classified"]["valid"])
        self.assertFalse(attempt["quiescent"])
        self.assertFalse(owner.load()["revisions"])
        self.assertEqual(owner.get(attempt["response"]["transcript"]), snapshot["transcript"])
        self.assertNotEqual(result["status"], "accepted_contract")

    def test_pending_tool_cannot_be_declared_clean_by_terminal_status(self):
        owner, runner, backend = self.setup_cycle()
        owner.tick(backend, now=1001)
        admission, snapshot = self.snapshot(owner, runner)
        data = rows(snapshot["transcript"])
        data[4:4] = [{"type": "response_item", "payload": {"type": "function_call", "call_id": "active", "name": "write"}}]
        snapshot["transcript"] = encoded(data)
        self.assertEqual(owner.tick(backend, now=1002)["status"], "reconciling")
        self.assertFalse(owner.load()["attempts"][0]["quiescent"])

    def test_usage_counters_absent_known_and_reset_keep_billing_unknown(self):
        for mode in ("absent", "known", "reset"):
            with self.subTest(mode=mode):
                owner, runner, backend = self.setup_cycle()
                owner.tick(backend, now=1001)
                admission, snapshot = self.snapshot(owner, runner)
                if mode != "absent":
                    data = rows(snapshot["transcript"])
                    def count(n):
                        value = {"input_tokens": n, "output_tokens": 4, "cached_input_tokens": 0}
                        return {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": value, "last_token_usage": value}}}
                    data[-1:-1] = [count(20)] + ([count(10)] if mode == "reset" else [])
                    snapshot["transcript"] = encoded(data)
                self.assertEqual(owner.tick(backend, now=1002)["status"], "accepted_contract")
                usage = owner.load()["attempts"][0]["classified"]["runtime"]["usage"]
                self.assertEqual(usage["prompt_tokens"], 20 if mode == "known" else None)
                self.assertIsNone(usage["cost_usd"])

    def test_duplicate_surfaces_and_predetermined_native_identity_rejected(self):
        owner, runner, backend = self.setup_cycle()
        spec = copy.deepcopy(owner.json(owner.pin))
        spec["surfaces"][1]["pane_id"] = spec["surfaces"][0]["pane_id"]
        with self.assertRaises(contracts.ContractError):
            contracts.validate(spec)
        owner.tick(backend, now=1001)
        admission, _ = self.snapshot(owner, runner)
        admission = {**admission, "turn_id": "invented"}
        with self.assertRaises(contracts.ContractError):
            contracts.validate_admission(admission, owner.json(owner.pin))

    def test_known_foreign_runtime_is_rejected_before_send(self):
        for field in ("cli_version", "model_provider"):
            with self.subTest(field=field):
                owner, runner, backend = self.setup_cycle()
                original = runner.observe
                def foreign(surface, resource):
                    value = original(surface, resource)
                    sid = "already-started-native-session"
                    value["receipt"]["agent"]["agent_session"] = {"agent":"codex","source":"codex","kind":"id","value":sid}
                    meta = {"id":sid,"model_provider":"openai","cli_version":"0.155.1", field:"foreign"}
                    value["transcript"] = encoded([{"type":"session_meta","payload":meta}])
                    return value
                with mock.patch.object(runner, "observe", side_effect=foreign):
                    with self.assertRaisesRegex(contracts.ContractError, "pre-dispatch native runtime"):
                        owner.tick(backend, now=1001)
                self.assertFalse(runner.sent)
                self.assertFalse(owner.load()["attempts"][0]["intent"])

    def test_known_reused_session_is_rejected_before_second_send(self):
        owner, runner, backend = self.setup_cycle()
        runner.actions = ["invalid", "good"]
        owner.tick(backend, now=1001)
        owner.tick(backend, now=1002)
        first = owner.load()["attempts"][0]
        sid = first["native_binding"]["agent_session"]
        original = runner.observe
        def reused(surface, resource):
            value = original(surface, resource)
            if resource["run_id"] != first["admission"]["run_id"]:
                value["receipt"]["agent"]["agent_session"] = {"agent":"codex","source":"codex","kind":"id","value":sid}
                value["transcript"] = encoded([{"type":"session_meta","payload":{"id":sid,"model_provider":"openai","cli_version":"0.155.1"}}])
            return value
        with mock.patch.object(runner, "observe", side_effect=reused):
            with self.assertRaisesRegex(cycle.CycleError, "already used"):
                owner.tick(backend, now=1003)
        self.assertEqual(len(runner.sent), 1)
        self.assertFalse(owner.load()["attempts"][-1]["intent"])

    def test_native_publications_recover_after_actual_process_exit(self):
        for kind in ("native_baseline", "native_observed", "cancel_signal_intent"):
            for boundary in ("after_atomic_partial_write", "after_atomic_pending_fsync", "after_atomic_rename"):
                with self.subTest(kind=kind, boundary=boundary):
                    owner, runner, backend = self.setup_cycle()
                    if kind != "native_baseline":
                        runner.active = kind == "cancel_signal_intent"
                        owner.tick(backend, now=1001)
                    if kind == "cancel_signal_intent":
                        admission, _ = self.snapshot(owner, runner)
                        owner.control("cancel", request_id=str(uuid.uuid4()), target=runtime.resource(admission), reason="fixture", now=1002)
                    pid = os.fork()
                    if pid == 0:
                        original_write = owner._write
                        def interrupted(relative, value):
                            if relative.startswith("events/") and value["kind"] == kind:
                                with mock.patch.object(safe, "_atomic_write_checkpoint", side_effect=lambda name: os._exit(73) if name == boundary else None):
                                    return original_write(relative, value)
                            return original_write(relative, value)
                        with mock.patch.object(owner, "_write", side_effect=interrupted):
                            owner.tick(backend, now=1003)
                        os._exit(74)
                    _, status = os.waitpid(pid, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(status), 73)
                    owner.recover()
                    outcome = owner.tick(backend, now=1004)
                    if kind == "native_baseline":
                        self.assertEqual(outcome["status"], "dispatched")
                        self.assertEqual(owner.tick(backend, now=1005)["status"], "accepted_contract")
                    elif kind == "native_observed":
                        self.assertEqual(outcome["status"], "accepted_contract")
                    else:
                        self.assertEqual(outcome["status"], "cancel_requested")
                        self.assertFalse(runner.stopped)
                        runner.finish(admission)
                        self.assertEqual(owner.tick(backend, now=1005)["status"], "cancelled")
                    self.assertEqual(len(runner.sent), 1)
