"""Real private IPC and Mission ledger; provider and executable are fixtures."""
import copy
from datetime import datetime, timedelta, timezone
import hmac
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
import uuid
from unittest import mock

from tests import test_fleet_herdr_native as fixtures
import fleet_admission as admission
import fleet_artifacts as artifacts
import fleet_herdr_control as control
import fleet_herdr_effects as effects
import fleet_herdr_inference as inference
import fleet_herdr_launch as launch
import fleet_herdr_native as native
import fleet_json
import fleet_mission_state as state


class BrokerTests(unittest.TestCase):
    create = fixtures.NativeLaunchTests.create
    save_backend = fixtures.NativeLaunchTests.save_backend
    current = fixtures.NativeLaunchTests.current
    exchange = fixtures.NativeLaunchTests.exchange

    def setUp(self):
        fixtures.NativeLaunchTests.setUp(self)
        self.exchange()
        state.append_event(self.runs, self.mid, kind="mission_running", actor="CONTROL",
            idempotency_key="fixture:running", payload={"manifest": "fixture"})
        a = admission.reserve_many(self.runs, self.mid, requests=[{
            "request_key": "herdr:build", "run_kind": "specialist", "recipient_instance": "worker",
            "capability": "build", "effect_sha256": "ab"*32, "task_sha256": "cd"*32,
            "delegated_budget": 0, "writer": True}], idempotency_key="fixture:reserve")["admissions"][0]
        self.admission = a
        bound = {k: a[k] for k in ("admission_id", "request_digest", "effect_sha256", "recipient_instance", "writer", "run_id")}
        c = admission.commit(self.runs, self.mid, **bound, idempotency_key="fixture:commit")
        admission.authorize_launch(self.runs, self.mid, **bound,
            commit_event_sha256=c["commit_event_sha256"], idempotency_key="fixture:authorize")
        self.member["start_attempts"][-1]["launch_observation_artifact_id"] = self.launch_pin
        self.run_id = a["run_id"]
        link = launch.link_run(self.runs, self.mid, self.member, self.run_id, "cd"*32)
        self.backend["submissions"] = {self.run_id: {"run_id": self.run_id, "generation": self.generation,
            "instance_id": "worker", "phase": "submitted", "prompt_sha256": "cd"*32,
            "launch_run_link_artifact_id": link}}
        self.backend.update(schema_version=2, backend_version="0.8.2",
                            compiled_digest=self.compiled["compiled_digest"])
        self.save_backend()
        control.enable(self.runs, self.mid)
        self.policy_id = inference.freeze(self.publisher, call_timeout_ms=getattr(self, "call_timeout_ms", 1000),
            max_input_bytes=getattr(self, "max_input_bytes", 65536),
            provider_binding=getattr(self, "provider_binding", None),
            profile=getattr(self, "profile", inference.PROFILE))
        self.provider_socket, self.peer = socket.socketpair()
        self.addCleanup(self.peer.close)
        self.addCleanup(self.provider_socket.close)
        self.provider = self.provider_factory() if hasattr(self, "provider_factory") else inference.LocalProvider(self.provider_socket)
        self.broker = inference.Broker(self.publisher, self.policy_id, self.provider)
        self.calls = []
        self.threads = []
        self.peer_errors = []
        self.addCleanup(self.join_peers)

    def join_peers(self):
        for thread in self.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive(), "fixture peer did not stop")
        self.assertEqual(self.peer_errors, [])

    def raw(self, **extra):
        return fleet_json.canonical_bytes({"policy_id": self.policy_id,
            "request_id": str(uuid.uuid4()), "input": "synthetic prompt", "max_output_tokens": 32, **extra})

    def response(self, envelope, **extra):
        return fleet_json.canonical_bytes({**{k: envelope[k] for k in
            ("policy_id", "request_id", "request_sha256", "model")},
            "output": "synthetic answer", "output_tokens": 3, **extra})

    def peer_once(self, callback=None):
        def run():
            try:
                raw = inference.read_frame(self.peer, time.monotonic()+getattr(self, "peer_timeout", 2))
                envelope = fleet_json.loads(raw)
                self.calls.append(envelope)
                result = self.response(envelope) if callback is None else callback(envelope)
                if result is not None:
                    inference.write_frame(self.peer, result, time.monotonic()+getattr(self, "peer_timeout", 2))
            except Exception as exc:
                self.peer_errors.append(type(exc).__name__)
        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()

    def requests(self):
        return self.current()["inference_policies"][self.policy_id]["requests"]

    def pause(self, action="pause"):
        return control.request(self.runs, self.mid, action=action,
            reason="fixture", idempotency_key="fixture:"+action)

    def test_private_client_provider_cas_replay_and_no_native_grant(self):
        self.peer_once()
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        request = self.raw()
        inference.write_frame(client, request, time.monotonic()+2)
        self.broker.serve_once(server)
        response = inference.read_frame(client, time.monotonic()+2)
        self.assertEqual(fleet_json.loads(response)["output"], "synthetic answer")
        original_head = self.current()["head_sha256"]
        # Recreate the broker to model loss of all transient request state.
        recreated = inference.Broker(self.publisher, self.policy_id, self.provider)
        self.assertEqual(recreated.handle(request), response)
        self.assertEqual(self.current()["head_sha256"], original_head)
        self.assertEqual(len(self.calls), 1)
        entry = next(iter(self.requests().values()))
        receipt = fleet_json.loads(artifacts.get_bytes(self.runs, self.mid, entry["result"]["receipt_artifact_id"]))
        self.assertEqual(artifacts.get_bytes(self.runs, self.mid, receipt["response_artifact_id"]), response)
        self.assertEqual(receipt["provider_execution"], "SIMULATED")
        self.assertEqual(receipt["authority"], "none")
        self.assertEqual(self.current()["status"], "running")
        with self.assertRaises(effects.EffectMediationDenied):
            effects.require_native_mediation()

    def test_agent_cannot_select_endpoint_credentials_model_effort_or_operation(self):
        for key, value in (("url", "http://127.0.0.1:1"), ("headers", {"Authorization": "fake"}),
                ("model", "other"), ("effort", "low"), ("operation", "exec"), ("tools", [])):
            with self.subTest(key=key), self.assertRaises(inference.InferenceError):
                self.broker.handle(self.raw(**{key: value}))
        with self.assertRaises(inference.InferenceError):
            self.broker.handle(self.raw(policy_id="ab"*32))
        self.assertEqual(self.requests(), {})
        self.peer.settimeout(.03)
        with self.assertRaises(TimeoutError):
            self.peer.recv(1)

    def test_duplicate_json_keys_noncanonical_and_bool_limits_fail_before_reservation(self):
        raw = self.raw()
        malformed = [raw+b" ", raw.replace(b'"input":', b'"input":"x","input":'),
            self.raw(max_output_tokens=True), self.raw(input=[]), self.raw(input=""), b"[1]"]
        for body in malformed:
            with self.subTest(body=body[:50]), self.assertRaises((ValueError, inference.InferenceError)):
                self.broker.handle(body)
        self.assertEqual(self.requests(), {})

    def test_same_request_id_different_content_and_budget_changes_rejected(self):
        raw = self.raw()
        self.peer_once()
        self.broker.handle(raw)
        changed = {**fleet_json.loads(raw), "input": "different"}
        with self.assertRaisesRegex(inference.InferenceError, "different bytes"):
            self.broker.handle(fleet_json.canonical_bytes(changed))
        self.assertEqual(inference.freeze(self.publisher, call_timeout_ms=1000), self.policy_id)
        with self.assertRaisesRegex(state.MissionStateError, "immutable"):
            inference.freeze(self.publisher, max_requests=9, call_timeout_ms=1000)
        self.assertEqual(len(self.calls), 1)

    def test_budget_reserves_full_output_limit_without_refunding_reported_usage(self):
        self.peer_once()
        self.broker.handle(self.raw(max_output_tokens=4096))
        store = self.runs / "missions" / self.mid / "artifacts"
        before = set(store.iterdir())
        with self.assertRaisesRegex(inference.InferenceError, "budget exhausted"):
            self.broker.handle(self.raw(max_output_tokens=1))
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(set(store.iterdir()), before)

    def test_pause_cancel_and_replaced_attempt_prevent_any_provider_send(self):
        self.pause()
        with self.assertRaises((native_error(), inference.InferenceError)):
            self.broker.handle(self.raw())
        self.pause("resume")
        original = copy.deepcopy(self.member["start_attempts"])
        self.member["start_attempts"].append({"attempt_id": str(uuid.uuid4()), "launch_intent": self.intent})
        self.save_backend()
        with self.assertRaises(native_error()):
            self.broker.handle(self.raw())
        self.member["start_attempts"] = original
        self.save_backend()
        self.pause("cancel")
        with self.assertRaises(native_error()):
            self.broker.handle(self.raw())
        self.assertEqual(self.requests(), {})

    def test_missing_or_other_run_link_and_cancelled_run_rejected(self):
        submission = self.backend["submissions"][self.run_id]
        submission["run_id"] = str(uuid.uuid4())
        self.save_backend()
        with self.assertRaises(native_error()):
            self.broker.handle(self.raw())
        submission["run_id"] = self.run_id
        self.save_backend()
        state.append_event(self.runs, self.mid, kind="run_cancel_requested", actor="CONTROL",
            idempotency_key="fixture:run-cancel", payload={"run_id": self.run_id, "reason": "fixture"})
        with self.assertRaisesRegex(inference.InferenceError, "no longer admitted"):
            self.broker.handle(self.raw())
        self.assertEqual(self.requests(), {})

    def test_self_consistent_backend_link_cannot_substitute_the_admitted_prompt(self):
        pin = launch.link_run(self.runs, self.mid, self.member, self.run_id, "ee"*32)
        self.backend["submissions"][self.run_id].update(prompt_sha256="ee"*32, launch_run_link_artifact_id=pin)
        self.save_backend()
        # Existing policy blocks the changed link; a fresh freeze must also
        # reject it against the ledger, even though backend/link agree mutually.
        with self.assertRaises(inference.InferenceError):
            self.broker.handle(self.raw())
        with self.assertRaisesRegex(inference.InferenceError, "prompt differs from admission"):
            inference.freeze(self.publisher)
        self.assertEqual(self.requests(), {})

    def test_ambiguous_send_never_retried_even_under_new_request_id(self):
        raw = self.raw()
        self.peer_once(lambda _: self.peer.close())
        with self.assertRaisesRegex(inference.InferenceError, "no automatic resend"):
            self.broker.handle(raw)
        self.assertEqual(next(iter(self.requests().values()))["result"]["status"], "indeterminate")
        for retry in (raw, self.raw()):
            with self.assertRaises((inference.InferenceError, state.MissionStateError)):
                self.broker.handle(retry)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.requests()), 1)

    def test_crash_after_reservation_before_send_is_not_replayed(self):
        raw = self.raw()
        with mock.patch.object(self.provider, "exchange", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.broker.handle(raw)
        self.assertIsNone(next(iter(self.requests().values()))["result"])
        with self.assertRaisesRegex(inference.InferenceError, "automatic resend forbidden"):
            self.broker.handle(raw)
        with self.assertRaisesRegex(inference.InferenceError, "automatic resend forbidden"):
            self.broker.handle(self.raw())
        self.assertEqual(self.calls, [])

    def test_cancellation_interrupts_pending_peer_and_never_delivers_answer(self):
        def cancel(_):
            self.pause("cancel")
            time.sleep(.15)
        self.peer_once(cancel)
        started = time.monotonic()
        with self.assertRaisesRegex(inference.InferenceError, "no automatic resend"):
            self.broker.handle(self.raw())
        self.assertLess(time.monotonic()-started, .8)
        self.assertEqual(next(iter(self.requests().values()))["result"]["status"], "indeterminate")

    def test_request_deadline_is_bounded_and_result_is_indeterminate(self):
        self.peer_once(lambda _: None)
        started = time.monotonic()
        with self.assertRaisesRegex(inference.InferenceError, "no automatic resend"):
            self.broker.handle(self.raw())
        self.assertLess(time.monotonic()-started, 1.8)
        self.assertEqual(next(iter(self.requests().values()))["result"]["status"], "indeterminate")

    def test_expired_process_deadline_rejects_before_reservation_or_ipc(self):
        self.broker.local_deadline = time.monotonic()-1
        before = self.current()["head_sha256"]
        with mock.patch.object(self.provider, "exchange") as exchange:
            with self.assertRaisesRegex(inference.InferenceError, "local process deadline"):
                self.broker.handle(self.raw())
            exchange.assert_not_called()
        self.assertEqual(self.current()["head_sha256"], before)
        self.assertEqual(self.requests(), {})

    def test_process_deadline_bounds_pending_inference_without_resend(self):
        self.peer_once(lambda _: None)
        self.broker.local_deadline = time.monotonic()+.15
        started = time.monotonic()
        with self.assertRaisesRegex(inference.InferenceError, "no automatic resend"):
            self.broker.handle(self.raw())
        self.assertLess(time.monotonic()-started, .8)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(next(iter(self.requests().values()))["result"]["status"], "indeterminate")

    def test_wrong_response_binding_is_never_persisted_or_returned(self):
        self.peer_once(lambda e: self.response(e, request_id=str(uuid.uuid4()), output="synthetic-secret"))
        with self.assertRaises(inference.InferenceError):
            self.broker.handle(self.raw())
        for path in (self.runs / "missions" / self.mid / "artifacts").iterdir():
            if path.is_file():
                self.assertNotIn(b"synthetic-secret", path.read_bytes())

    def test_response_retained_before_lost_client_ack_replays_without_provider_call(self):
        client, server = socket.socketpair()
        def close_client(envelope):
            client.close()
            return self.response(envelope)
        self.peer_once(close_client)
        raw = self.raw()
        inference.write_frame(client, raw, time.monotonic()+2)
        with self.assertRaises(BrokenPipeError):
            self.broker.serve_once(server)
        self.assertEqual(fleet_json.loads(self.broker.handle(raw))["output"], "synthetic answer")
        self.assertEqual(len(self.calls), 1)

    def test_mission_deadline_rejects_before_reservation(self):
        future = datetime.now(timezone.utc) + timedelta(days=1)
        with mock.patch.object(inference, "datetime") as clock:
            clock.now.return_value = future
            with self.assertRaisesRegex(inference.InferenceError, "no longer admitted"):
                self.broker.handle(self.raw())
        self.assertEqual(self.requests(), {})

    def test_response_arriving_at_pause_is_retained_as_withheld(self):
        self.peer_once()
        validate = inference.response_bytes
        def pause_after_parse(raw, envelope):
            result = validate(raw, envelope)
            self.pause()
            return result
        with mock.patch.object(inference, "response_bytes", side_effect=pause_after_parse):
            with self.assertRaisesRegex(inference.InferenceError, "no automatic resend"):
                self.broker.handle(self.raw())
        result = next(iter(self.requests().values()))["result"]
        self.assertEqual(result["status"], "withheld")
        receipt = fleet_json.loads(artifacts.get_bytes(self.runs, self.mid, result["receipt_artifact_id"]))
        self.assertIsInstance(receipt["response_artifact_id"], str)

    def test_unresolved_request_prevents_false_quiescence(self):
        with mock.patch.object(self.provider, "exchange", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.broker.handle(self.raw())
        self.pause()
        requested = control.view(self.current())
        with self.assertRaisesRegex(state.MissionStateError, "inference request"):
            control.acknowledge(self.runs, self.mid, requested["requests"][requested["latest"]])
        with self.assertRaisesRegex(state.MissionStateError, "inference request"):
            state.append_terminal(self.runs, self.mid, status="failed", reason="fixture",
                idempotency_key="fixture:false-terminal")

    def test_new_attempt_cannot_reset_an_unknown_upstream_outcome(self):
        with mock.patch.object(self.provider, "exchange", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.broker.handle(self.raw())
        policy = copy.deepcopy(self.current()["inference_policies"][self.policy_id]["policy"])
        policy["attempt"]["attempt_id"] = str(uuid.uuid4())
        raw = fleet_json.canonical_bytes(policy)
        with self.assertRaisesRegex(state.MissionStateError, "exact reconciliation"):
            state.append_event(self.runs, self.mid, kind="inference_policy_frozen", actor="CONTROL",
                idempotency_key="fixture:unknown-new-attempt",
                payload={"policy_id": inference.sha(raw), "policy": policy})

    def test_completed_response_is_not_released_after_pause(self):
        self.peer_once()
        raw = self.raw()
        self.broker.handle(raw)
        self.pause()
        with self.assertRaises(native_error()):
            self.broker.handle(raw)
        self.assertEqual(len(self.calls), 1)

    def test_ledger_rejects_worker_authorship_and_duplicate_completion(self):
        self.peer_once()
        self.broker.handle(self.raw())
        payload = next(iter(self.requests().values()))["result"]
        for actor in ("CONTROL", "worker"):
            with self.assertRaises(state.MissionStateError):
                state.append_event(self.runs, self.mid, kind="inference_request_finished", actor=actor,
                    idempotency_key="fixture:forged:"+actor, payload=payload)

    def test_offline_evidence_uses_retained_bytes_without_runtime_or_provider(self):
        self.peer_once()
        self.broker.handle(self.raw())
        current = self.current()
        retained = {p.name: p.read_bytes() for p in (self.runs / "missions" / self.mid / "artifacts").iterdir() if p.is_file()}
        self.binary.unlink()
        self.provider_socket.close()
        self.peer.close()
        verified = inference.verify_evidence(current, retained.__getitem__)
        self.assertEqual(verified["requests"], {"completed": 1, "withheld": 0, "indeterminate": 0, "pending": 0})
        self.assertEqual(verified["mission_success"], "NOT_VERIFIED")
        self.assertEqual(verified["scope"], "offline_broker_integrity")

    def test_rehashed_wrong_attempt_response_fails_offline_binding(self):
        self.peer_once()
        self.broker.handle(self.raw())
        current = self.current()
        retained = {p.name: p.read_bytes() for p in (self.runs / "missions" / self.mid / "artifacts").iterdir() if p.is_file()}
        result = next(iter(current["inference_policies"][self.policy_id]["requests"].values()))["result"]
        receipt = fleet_json.loads(retained[result["receipt_artifact_id"]])
        response = fleet_json.loads(retained[receipt["response_artifact_id"]])
        response["policy_id"] = "ee"*32
        bad_raw = fleet_json.canonical_bytes(response)
        bad_pin = inference.sha(bad_raw)
        retained[bad_pin] = bad_raw
        receipt["response_artifact_id"] = bad_pin
        bad_receipt = fleet_json.canonical_bytes(receipt)
        result["receipt_artifact_id"] = inference.sha(bad_receipt)
        retained[result["receipt_artifact_id"]] = bad_receipt
        with self.assertRaisesRegex(inference.InferenceError, "response binding differs"):
            inference.verify_evidence(current, retained.__getitem__)

    def test_real_provider_process_keeps_its_synthetic_credential_out_of_client_and_cas(self):
        # This local executable is trusted fixture code, not a model campaign.
        code = '''import hmac,os,socket,sys,time
sys.path.insert(0, sys.argv[1])
import fleet_herdr_inference as i, fleet_json as j
s=socket.socket(fileno=int(sys.argv[2]))
secret=os.urandom(32)
e=j.loads(i.read_frame(s,time.monotonic()+3))
r={k:e[k] for k in ("policy_id","request_id","request_sha256","model")}
r.update(output=hmac.new(secret,e["input"].encode(),"sha256").hexdigest(),output_tokens=4)
i.write_frame(s,j.canonical_bytes(r),time.monotonic()+3)
s.close()
sys.stdout.buffer.write(secret)  # Separate CONTROL-only test oracle, never client IPC.
'''
        child = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", code,
            str(Path(inference.__file__).parent), str(self.peer.fileno())],
            env={"PATH": "/usr/bin:/bin"}, cwd=self.tmp, pass_fds=(self.peer.fileno(),),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def stop():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)
        self.addCleanup(stop)
        raw = self.raw()
        response = self.broker.handle(raw)
        out, err = child.communicate(timeout=3)
        self.assertEqual((child.returncode, len(out), err), (0, 32, b""))
        self.assertEqual(fleet_json.loads(response)["output"],
            hmac.new(out, fleet_json.loads(raw)["input"].encode(), "sha256").hexdigest())
        self.assertNotIn(out, response)
        for path in (self.runs / "missions" / self.mid).rglob("*"):
            if path.is_file():
                self.assertNotIn(out, path.read_bytes())


def native_error():
    return native.NativeError


class TransportTests(unittest.TestCase):
    def test_provider_endpoint_cannot_be_tcp(self):
        with socket.socket() as tcp:
            with self.assertRaisesRegex(inference.InferenceError, "socketpair"):
                inference.LocalProvider(tcp)

    def test_blocked_write_observes_cancellation_without_waiting_for_deadline(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        started = time.monotonic()
        def poll():
            if time.monotonic() - started > .1:
                raise inference.InferenceError("cancelled")
        with self.assertRaisesRegex(inference.InferenceError, "cancelled"):
            inference.LocalProvider(a).exchange(b"x"*inference.LIMIT, started+5, poll)
        self.assertLess(time.monotonic()-started, .5)
        self.assertEqual(a.fileno(), -1)

    def test_large_frame_header_rejected_before_allocating_body(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.sendall(b"\xff\xff\xff\xff")
        with self.assertRaisesRegex(inference.InferenceError, "frame size"):
            inference.read_frame(b, time.monotonic()+1)


if __name__ == "__main__":
    unittest.main()
