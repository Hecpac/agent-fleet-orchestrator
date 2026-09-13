"""Attempt-bound inference broker with separately frozen provider routes.

CONTROL supplies the Publisher and a provider: historical anonymous fixture IPC
or the explicit ChatGPT subscription egress adapter. Credentials remain outside
the agent; a v2 policy pins the subscription route/account without secrets.
No Codex launch or tool grant exists here. The separate Responses profile
validates its wire data through fleet_codex_responses; the text profile is unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import select
import socket
import struct
import time

import fleet_artifacts as artifacts
import fleet_herdr_native as native
import fleet_json
import fleet_mission_state as state

KINDS = {"inference_policy_frozen", "inference_request_reserved", "inference_request_finished"}
LIMIT = 256 * 1024
PROFILE = "local-inference-fixture-v1"
RESPONSES_PROFILE = "local-codex-responses-v1"


class InferenceError(RuntimeError):
    pass


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def check(condition, reason):
    if not condition:
        raise InferenceError(reason)


def provider_execution(policy):
    if policy["schema_version"] == 1:
        return "SIMULATED"
    import fleet_chatgpt_provider
    return fleet_chatgpt_provider.execution(policy)


def validate_policy(policy):
    fields = {"schema_version", "profile", "attempt", "run_id", "run_link_artifact_id",
        "launch_artifact_id", "frozen_policy_sha256", "model", "effort", "deadline_at",
        "max_requests", "max_input_bytes", "max_output_tokens", "call_timeout_ms"}
    check(type(policy) is dict, "inference policy must be an object")
    if policy.get("schema_version") in {2, 3}:
        fields.add("provider")
    state._require_fields("inference policy", policy, fields)
    check(type(policy["schema_version"]) is int and policy["schema_version"] in {1, 2, 3}
          and policy["profile"] in {PROFILE, RESPONSES_PROFILE}, "unsupported inference profile")
    if policy["schema_version"] in {2, 3}:
        import fleet_chatgpt_provider
        check(policy["profile"] == RESPONSES_PROFILE, "subscription requires Responses")
        fleet_chatgpt_provider.validate_descriptor(policy["provider"])
    attempt = policy["attempt"]
    check(type(attempt) is dict, "inference attempt must be an object")
    state._require_fields("inference attempt", attempt, {"mission_id", "generation", "role", "attempt_id"})
    check(attempt["role"] in ({"lead", "worker", "reviewer", "verifier"} if policy["schema_version"] == 3 else {"worker"}), "inference role is unsupported")
    for key in ("mission_id", "generation", "attempt_id"):
        state._require_uuid(attempt[key], key)
    state._require_uuid(policy["run_id"], "inference run")
    for key in ("run_link_artifact_id", "launch_artifact_id", "frozen_policy_sha256"):
        state._require_sha(policy[key], key)
    for key in ("model", "effort"):
        state._require_nonempty(policy[key], key)
    state.parse_timestamp(policy["deadline_at"], "inference deadline")
    for key, maximum in (("max_requests", 128), ("max_input_bytes", LIMIT),
                         ("max_output_tokens", 32768), ("call_timeout_ms", 30000)):
        check(type(policy[key]) is int and 1 <= policy[key] <= maximum, "invalid inference limit")


def validate_payload(kind, payload):
    fields = {"inference_policy_frozen": {"policy_id", "policy"},
        "inference_request_reserved": {"policy_id", "request_id", "request_sha256", "input_bytes", "output_tokens"},
        "inference_request_finished": {"policy_id", "request_id", "receipt_artifact_id", "status"}}[kind]
    state._require_fields(kind, payload, fields)
    state._require_sha(payload["policy_id"], "inference policy id")
    if kind == "inference_policy_frozen":
        validate_policy(payload["policy"])
        check(sha(fleet_json.canonical_bytes(payload["policy"])) == payload["policy_id"], "inference policy digest")
    else:
        state._require_uuid(payload["request_id"], "inference request")
        if kind == "inference_request_reserved":
            state._require_sha(payload["request_sha256"], "inference request digest")
            for key in ("input_bytes", "output_tokens"):
                check(type(payload[key]) is int and 0 < payload[key] <= LIMIT, "invalid reservation")
        else:
            state._require_sha(payload["receipt_artifact_id"], "inference receipt")
            check(payload["status"] in {"completed", "withheld", "indeterminate"}, "invalid inference status")


def admitted(current, policy, instant):
    check(current["status"] == "running"
          and current.get("herdr_control", {}).get("desired", "running") == "running"
          and policy["attempt"]["mission_id"] == current["mission_id"]
          and policy["deadline_at"] == current["admission_policy"]["deadline_at"]
          and instant < state.parse_timestamp(policy["deadline_at"], "inference deadline")
          and policy["run_id"] not in current["cancelled_runs"], "inference no longer admitted")
    matches = [a for a in current["admissions"].values() if a["run_id"] == policy["run_id"]
        and a["recipient_instance"] == policy["attempt"]["role"] and a["active"]
        and a["writer"] is (policy["attempt"]["role"] == "worker")
        and a["phase"] in {"authorized", "started"} and a.get("result") is None]
    check(len(matches) == 1 and (not matches[0]["writer"] or matches[0]["admission_id"] == current["active_writer"]),
          "inference lacks its active writer admission")


def reduce(current, event):
    check(event["actor"] == "CONTROL", "inference requires CONTROL")
    payload, kind = event["payload"], event["kind"]
    policies = current.setdefault("inference_policies", {})
    pid = payload["policy_id"]
    if kind == "inference_policy_frozen":
        policy = payload["policy"]
        admitted(current, policy, state.parse_timestamp(event["timestamp"], "inference event"))
        require_settled(current)
        check(pid not in policies and not any(p["policy"]["attempt"] == policy["attempt"]
              for p in policies.values()), "inference attempt policy is immutable")
        policies[pid] = {"policy": policy, "requests": {}}
        return
    check(pid in policies, "inference policy is missing")
    entry = policies[pid]
    requests, rid = entry["requests"], payload["request_id"]
    if kind == "inference_request_reserved":
        policy = entry["policy"]
        admitted(current, policy, state.parse_timestamp(event["timestamp"], "inference event"))
        reservation_allowed(entry, payload)
        requests[rid] = {**payload, "result": None}
    else:
        check(rid in requests and requests[rid]["result"] is None, "inference result lacks unique reservation")
        if payload["status"] == "completed":
            admitted(current, entry["policy"], state.parse_timestamp(event["timestamp"], "inference result"))
        # Result durability is allowed after pause/revocation. It is not delivery
        # authority, and the generic ledger forbids mutations after termination.
        requests[rid]["result"] = payload


def reservation_allowed(entry, payload):
    requests, policy = entry["requests"], entry["policy"]
    check(payload["request_id"] not in requests, "inference request already reserved")
    check(all(r["result"] and r["result"]["status"] == "completed" for r in requests.values()),
          "unresolved inference request; automatic resend forbidden")
    check(len(requests) < policy["max_requests"]
          and sum(r["input_bytes"] for r in requests.values()) + payload["input_bytes"] <= policy["max_input_bytes"]
          and sum(r["output_tokens"] for r in requests.values()) + payload["output_tokens"] <= policy["max_output_tokens"],
          "inference reservation budget exhausted")


def require_settled(current):
    """Unknown upstream outcome is not proof of quiescence."""
    check(all(r["result"] and r["result"]["status"] in {"completed", "withheld"}
        for p in current.get("inference_policies", {}).values() for r in p["requests"].values()),
        "inference request requires exact reconciliation before quiescence")


def anonymous(sock):
    check(sock.family == socket.AF_UNIX and sock.type == socket.SOCK_STREAM
          and not sock.getsockname() and not sock.getpeername(), "anonymous local socketpair required")


def write_frame(sock, raw, deadline, poll=lambda: None):
    check(type(raw) is bytes and 0 < len(raw) <= LIMIT, "inference frame size")
    data = memoryview(struct.pack("!I", len(raw)) + raw)
    sock.setblocking(False)
    while data:
        poll()
        remaining = deadline - time.monotonic()
        check(remaining > 0, "inference transport deadline")
        if not select.select([], [sock], [], min(.05, remaining))[1]:
            continue
        try:
            count = sock.send(data)
        except BlockingIOError:
            continue
        check(count > 0, "inference transport closed")
        data = data[count:]


def read_frame(sock, deadline, poll=lambda: None):
    def read(size):
        result = bytearray()
        while len(result) < size:
            poll()
            remaining = deadline - time.monotonic()
            check(remaining > 0, "inference transport deadline")
            if not select.select([sock], [], [], min(0.05, remaining))[0]:
                continue
            block = sock.recv(size - len(result))
            check(bool(block), "inference transport closed")
            result.extend(block)
        return bytes(result)
    size = struct.unpack("!I", read(4))[0]
    check(0 < size <= LIMIT, "inference frame size")
    return read(size)


class LocalProvider:
    """Already connected CONTROL-owned fixture peer; no endpoint resolution."""
    def __init__(self, sock):
        anonymous(sock)
        self.sock = sock

    def close(self):
        self.sock.close()

    def exchange(self, raw, deadline, poll):
        try:
            poll()
            write_frame(self.sock, raw, deadline, poll)
            return read_frame(self.sock, deadline, poll)
        except BaseException:
            self.sock.close()  # Never continue a partial or ambiguous stream.
            raise


def require_prompt_binding(publisher, run_id, link_pin):
    link = fleet_json.loads(artifacts.get_bytes(publisher.runs, publisher.mid, link_pin))
    admissions = [a for a in publisher.tx.current_state["admissions"].values() if a["run_id"] == run_id]
    check(len(admissions) == 1 and link["prompt_sha256"] == admissions[0]["task_sha256"],
          "inference prompt differs from admission")


def freeze(publisher, *, max_requests=8, max_input_bytes=65536, max_output_tokens=4096,
           call_timeout_ms=5000, profile=PROFILE, provider_binding=None):
    """Explicit fixture entry point; never called by the real Herdr launcher."""
    with publisher.transaction():
        run, link = publisher.run_link()
        check(run is not None, "inference requires a linked run")
        require_prompt_binding(publisher, run, link)
        capsule = publisher.capsule
        policy = {"schema_version": 1, "profile": profile, "attempt": publisher.attempt,
            "run_id": run, "run_link_artifact_id": link, "launch_artifact_id": publisher.launch_pin,
            "frozen_policy_sha256": capsule["frozen_policy_sha256"],
            "model": capsule["requested_policy"]["model"], "effort": capsule["requested_policy"]["effort"],
            "deadline_at": publisher.tx.current_state["admission_policy"]["deadline_at"],
            "max_requests": max_requests, "max_input_bytes": max_input_bytes,
            "max_output_tokens": max_output_tokens, "call_timeout_ms": call_timeout_ms}
        if provider_binding is not None:
            policy.update(schema_version=3 if getattr(publisher, "mission_capsule", False) else 2, provider=dict(provider_binding))
        validate_policy(policy)
        raw = fleet_json.canonical_bytes(policy)
        pin = sha(raw)
        previous = publisher.tx.current_state.get("inference_policies", {}).get(pin)
        if not previous:
            require_settled(publisher.tx.current_state)
            artifacts.put_bytes(publisher.runs, publisher.mid, raw)
            publisher.tx.append_event(kind="inference_policy_frozen", actor="CONTROL",
                idempotency_key="inference:policy:" + pin, payload={"policy_id": pin, "policy": policy})
        return pin


def request_bytes(raw, policy_id):
    check(type(raw) is bytes and 0 < len(raw) <= LIMIT, "inference request size")
    value = fleet_json.loads(raw)
    check(type(value) is dict and set(value) == {"policy_id", "request_id", "input", "max_output_tokens"},
          "inference request fields")
    check(value["policy_id"] == policy_id, "inference request policy differs")
    state._require_uuid(value["request_id"], "inference request")
    check(type(value["input"]) is str and 0 < len(value["input"].encode()) <= LIMIT,
          "inference input must be bounded text")
    check(type(value["max_output_tokens"]) is int and 1 <= value["max_output_tokens"] <= 32768,
          "inference output reservation")
    check(fleet_json.canonical_bytes(value) == raw, "inference request must be canonical JSON")
    return value


def response_bytes(raw, envelope):
    check(type(raw) is bytes and 0 < len(raw) <= LIMIT, "inference response size")
    value = fleet_json.loads(raw)
    check(type(value) is dict and set(value) == {"policy_id", "request_id", "request_sha256", "model", "output", "output_tokens"},
          "inference response fields")
    check(all(value[k] == envelope[k] for k in ("policy_id", "request_id", "request_sha256", "model")),
          "inference response binding differs")
    check(type(value["output"]) is str and type(value["output_tokens"]) is int
          and 0 <= value["output_tokens"] <= envelope["max_output_tokens"], "inference response usage")
    check(fleet_json.canonical_bytes(value) == raw, "inference response must be canonical JSON")
    if envelope.get("profile") == RESPONSES_PROFILE:
        import fleet_codex_responses
        body = fleet_json.loads(envelope["input"])
        stream = fleet_codex_responses.events(value["output"].encode(), body, envelope["model"], envelope["max_output_tokens"])
        check(stream[-1]["response"]["usage"]["output_tokens"] == value["output_tokens"], "Responses usage differs from broker receipt")
    return value


class Broker:
    def __init__(self, publisher, policy_id, provider):
        import fleet_chatgpt_provider
        check(type(provider) in {LocalProvider, fleet_chatgpt_provider.ChatGPTProvider}, "unsupported provider transport")
        self.publisher, self.policy_id, self.provider = publisher, policy_id, provider
        self.local_deadline = None  # Optional, tighter CONTROL-owned process deadline.

    def active(self):
        owner = self.publisher
        entry = owner.tx.current_state.get("inference_policies", {}).get(self.policy_id)
        check(entry is not None, "inference policy is missing")
        policy = entry["policy"]
        import fleet_chatgpt_provider
        if policy["schema_version"] == 1:
            check(type(self.provider) is LocalProvider, "historical policy requires local fixture transport")
        else:
            check(type(self.provider) is fleet_chatgpt_provider.ChatGPTProvider
                  and self.provider.binding == policy["provider"], "subscription provider differs from frozen policy")
        check(artifacts.get_bytes(owner.runs, owner.mid, self.policy_id) == fleet_json.canonical_bytes(policy),
              "inference policy CAS differs")
        check(policy["attempt"] == owner.attempt and policy["launch_artifact_id"] == owner.launch_pin
              and policy["frozen_policy_sha256"] == owner.capsule["frozen_policy_sha256"]
              and (policy["run_id"], policy["run_link_artifact_id"]) == owner.run_link(), "inference attempt binding differs")
        check(all(policy[k] == owner.capsule["requested_policy"][k] for k in ("model", "effort")),
              "inference model differs from launch policy")
        require_prompt_binding(owner, policy["run_id"], policy["run_link_artifact_id"])
        admitted(owner.tx.current_state, policy, datetime.now(timezone.utc))
        return entry

    def poll(self):
        check(self.local_deadline is None or time.monotonic() < self.local_deadline,
              "local process deadline expired")
        with self.publisher.transaction():
            self.active()

    def handle(self, raw):
        self.poll()
        request = request_bytes(raw, self.policy_id)
        rid, owner = request["request_id"], self.publisher
        with owner.transaction():
            entry = self.active()
            policy = entry["policy"]
            if policy["profile"] == RESPONSES_PROFILE:
                import fleet_codex_responses
                fleet_codex_responses.request(request["input"].encode(), policy)
            envelope = {**request, "request_sha256": sha(raw), "profile": policy["profile"],
                "model": policy["model"], "effort": policy["effort"]}
            envelope_raw = fleet_json.canonical_bytes(envelope)
            check(len(envelope_raw) <= LIMIT, "inference envelope size")
            previous = entry["requests"].get(rid)
            if previous:
                check(previous["request_sha256"] == sha(raw), "inference request id reused with different bytes")
                check(previous["result"] and previous["result"]["status"] == "completed",
                      "unresolved inference request; automatic resend forbidden")
                return self._retained(previous, envelope)
            if policy["schema_version"] in {2, 3}:
                self.provider.ready()
            # Reserve before contacting the peer. A crash here is conservatively
            # indeterminate, even if no physical send subsequently happened.
            reservation = {"policy_id": self.policy_id, "request_id": rid, "request_sha256": sha(raw),
                "input_bytes": len(request["input"].encode()), "output_tokens": request["max_output_tokens"]}
            # Reject budget exhaustion before CAS writes as well as before IPC:
            # repeated rejected requests must not fill the durable artifact store.
            reservation_allowed(entry, reservation)
            artifacts.put_bytes(owner.runs, owner.mid, raw)
            owner.tx.append_event(kind="inference_request_reserved", actor="CONTROL",
                idempotency_key="inference:reserve:" + self.policy_id + ":" + rid,
                payload=reservation)
            seconds = min(policy["call_timeout_ms"] / 1000,
                (state.parse_timestamp(policy["deadline_at"], "inference deadline") - datetime.now(timezone.utc)).total_seconds())
            deadline = time.monotonic() + seconds
            if self.local_deadline is not None:
                deadline = min(deadline, self.local_deadline)
        outcome, response_pin, response = "indeterminate", None, None
        try:
            response = self.provider.exchange(envelope_raw, deadline, self.poll)
            response_bytes(response, envelope)
            outcome = "completed"
        except Exception:
            # Error strings, invalid frames and provider diagnostics may contain
            # secrets. Store only the fixed status, never exception text/raw bytes.
            self.provider.close()
            response = None
        # Use a normal bounded ledger transaction to retain a result even when
        # active() rejects a revoked attempt. Terminal records stay read-only.
        with native.bounded_transaction(), state.MissionTransaction(owner.runs, owner.mid) as tx:
            check(tx.current_state["status"] not in state.TERMINAL_STATUSES, "terminal inference record is read-only")
            if response is not None:
                response_pin = artifacts.put_bytes(owner.runs, owner.mid, response)["artifact_id"]
            try:
                owner.active(tx.current_state)
                admitted(tx.current_state, policy, datetime.now(timezone.utc))
                check(time.monotonic() < deadline, "inference transport deadline")
            except Exception:
                outcome = "withheld" if response_pin else "indeterminate"
            receipt = {"schema_version": policy["schema_version"], "profile": policy["profile"], "policy_id": self.policy_id,
                "request_id": rid, "request_sha256": sha(raw), "response_artifact_id": response_pin,
                "status": outcome, "authority": "none", "provider_execution": provider_execution(policy)}
            pin = artifacts.put_bytes(owner.runs, owner.mid, fleet_json.canonical_bytes(receipt))["artifact_id"]
            tx.append_event(kind="inference_request_finished", actor="CONTROL",
                idempotency_key="inference:finish:" + self.policy_id + ":" + rid,
                payload={"policy_id": self.policy_id, "request_id": rid, "receipt_artifact_id": pin, "status": outcome})
        check(outcome == "completed", "inference result unavailable; no automatic resend")
        # Recheck both the process and current run link immediately before return.
        self.poll()
        return response

    def _retained(self, previous, envelope):
        owner = self.publisher
        receipt = fleet_json.loads(artifacts.get_bytes(owner.runs, owner.mid, previous["result"]["receipt_artifact_id"]))
        policy = self.publisher.tx.current_state["inference_policies"][self.policy_id]["policy"]
        expected = {"schema_version": policy["schema_version"], "profile": envelope["profile"], "policy_id": self.policy_id,
            "request_id": envelope["request_id"], "request_sha256": envelope["request_sha256"],
            "response_artifact_id": receipt.get("response_artifact_id"), "status": "completed",
            "authority": "none", "provider_execution": provider_execution(policy)}
        check(fleet_json.canonical_bytes(receipt) == fleet_json.canonical_bytes(expected), "inference receipt binding differs")
        response = artifacts.get_bytes(owner.runs, owner.mid, receipt["response_artifact_id"])
        response_bytes(response, envelope)
        return response

    def serve_once(self, client):
        """One preconnected attempt-scoped client, closed on every outcome.

        Returning a completed response is not an acknowledgement that the client
        received it. A reconnect may retrieve the same durable response by ID.
        """
        try:
            anonymous(client)
            raw = read_frame(client, time.monotonic() + 2, self.poll)
            response = self.handle(raw)
            with self.publisher.transaction():
                policy = self.active()["policy"]
                remaining = (state.parse_timestamp(policy["deadline_at"], "inference deadline")
                             - datetime.now(timezone.utc)).total_seconds()
                write_frame(client, response, time.monotonic() + min(1, remaining))
        finally:
            client.close()


def verify_evidence(current, read):
    """Offline integrity of the broker chain, never inference or Mission success.

    The caller derives current from the ledger and supplies retained CAS bytes.
    No live candidate, image, peer, environment, key or endpoint is consulted.
    """
    def raw(pin):
        state._require_sha(pin, "inference evidence")
        data = read(pin)
        check(type(data) is bytes and sha(data) == pin, "inference evidence digest differs")
        return data

    def record(pin):
        data = raw(pin)
        value = fleet_json.loads(data)
        check(type(value) is dict and fleet_json.canonical_bytes(value) == data, "inference evidence JSON")
        return value

    counts = {"completed": 0, "withheld": 0, "indeterminate": 0, "pending": 0}
    policies = current.get("inference_policies", {})
    for pid, entry in policies.items():
        policy = record(pid)
        validate_policy(policy)
        check(policy == entry["policy"], "inference policy differs from ledger")
        if policy['schema_version'] == 3:
            import fleet_mission_capsule
            launch = fleet_mission_capsule.verify_launch(record(policy['launch_artifact_id']), current, raw)
            check(policy['launch_artifact_id'] == policy['frozen_policy_sha256'] == policy['run_link_artifact_id']
                  and launch['attempt'] == policy['attempt'] and launch['run_id'] == policy['run_id']
                  and launch['manifest']['provider'] == policy['provider']
                  and launch['requested_policy'] == {k:policy[k] for k in ('model','effort')},
                  'capsule inference launch differs')
        else:
            frozen = record(policy["frozen_policy_sha256"])
            launched = record(policy["launch_artifact_id"])
            link = record(policy["run_link_artifact_id"])
            check(frozen["kind"] == "frozen-attempt-policy" and frozen["schema_version"] == 2
                  and frozen["attempt"] == policy["attempt"]
                  and frozen["workflow_sha256"] == current["workflow_digest"]
                  and all(frozen["requested_policy"]["policy"][k] == policy[k] for k in ("model", "effort")),
                  "inference frozen model or attempt differs")
            check(launched["kind"] == "launcher-observation" and launched["attempt"] == policy["attempt"]
                  and launched["frozen_policy_sha256"] == policy["frozen_policy_sha256"]
                  and launched["requested_policy"] == frozen["requested_policy"]["policy"]
                  and launched["authority"] == "none", "inference launch evidence differs")
            admissions = [a for a in current["admissions"].values() if a["run_id"] == policy["run_id"]
                          and a["recipient_instance"] == "worker" and a["writer"]]
            check(len(admissions) == 1, "inference archived admission missing")
            expected_link = {"schema_version": 1, "kind": "launch-run-link", "attempt": policy["attempt"],
                "run_id": policy["run_id"], "prompt_sha256": admissions[0]["task_sha256"],
                "launch_observation_sha256": policy["launch_artifact_id"], "authority": "none",
                "INTEGRATION_BINDING": "NOT_VERIFIED"}
            check(fleet_json.canonical_bytes(link) == fleet_json.canonical_bytes(expected_link), "inference archived run link differs")
        for rid, reservation in entry["requests"].items():
            request = request_bytes(raw(reservation["request_sha256"]), pid)
            if policy["profile"] == RESPONSES_PROFILE:
                import fleet_codex_responses
                fleet_codex_responses.request(request["input"].encode(), policy)
            check(request["request_id"] == rid and request["max_output_tokens"] == reservation["output_tokens"]
                  and len(request["input"].encode()) == reservation["input_bytes"], "inference reservation differs")
            result = reservation["result"]
            if result is None:
                counts["pending"] += 1
                continue
            receipt = record(result["receipt_artifact_id"])
            response_pin = receipt.get("response_artifact_id")
            expected = {"schema_version": policy["schema_version"], "profile": policy["profile"], "policy_id": pid,
                "request_id": rid, "request_sha256": reservation["request_sha256"],
                "response_artifact_id": response_pin, "status": result["status"],
                "authority": "none", "provider_execution": provider_execution(policy)}
            check(fleet_json.canonical_bytes(receipt) == fleet_json.canonical_bytes(expected), "inference archived receipt differs")
            check((response_pin is None) == (result["status"] == "indeterminate"), "inference response disposition differs")
            if response_pin is not None:
                response_bytes(raw(response_pin), {**request, "request_sha256": reservation["request_sha256"],
                    "model": policy["model"], "profile": policy["profile"]})
            counts[result["status"]] += 1
    modes = {provider_execution(p["policy"]) for p in policies.values()}
    return {"valid": True, "scope": "offline_broker_integrity", "policies": len(policies),
        "requests": counts, "provider_execution": next(iter(modes)) if len(modes) == 1 else "MIXED" if modes else "NOT_VERIFIED",
        "mission_success": "NOT_VERIFIED", "authority": "none"}


def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Read-only offline inference evidence verification")
    parser.add_argument("--runs-dir", required=True, type=Path)
    parser.add_argument("--mission-id", required=True)
    args = parser.parse_args()
    try:
        current = state.derive_state(state.read_events(state.ledger_path(args.runs_dir, args.mission_id),
                                                       expected_mission_id=args.mission_id))
        result = verify_evidence(current, lambda pin: artifacts.get_bytes(args.runs_dir, args.mission_id, pin))
    except (InferenceError, state.MissionStateError, fleet_json.FleetJSONError,
            artifacts.ArtifactError, OSError, KeyError, TypeError, ValueError):
        # Avoid copying untrusted provider data to the CLI's diagnostic stream.
        print("inference evidence invalid or unavailable", file=sys.stderr)
        return 1
    print(fleet_json.canonical_bytes(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
