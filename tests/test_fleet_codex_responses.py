"""HTTP/SSE, real broker ledger and a provider-free tool round trip."""
import copy
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import unittest

from tests import test_fleet_herdr_inference as fixtures
import fleet_artifacts as artifacts
import fleet_codex_responses as responses
import fleet_herdr_inference as inference
import fleet_json


def bundle(body, output, *, response_id="resp_fixture"):
    final = {"id": response_id, "model": body["model"], "status": "completed", "output": output,
        "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}
    return [{"type": "response.created", "response": {"id": response_id}},
        *[{"type": "response.output_item.done", "output_index": i, "item": item} for i, item in enumerate(output)],
        {"type": "response.completed", "response": final}]


def message(text="synthetic final"):
    return {"id": "msg_fixture", "type": "message", "role": "assistant", "phase": "final_answer",
        "content": [{"type": "output_text", "text": text}]}


class ResponsesTests(unittest.TestCase):
    profile = responses.PROFILE
    create = fixtures.BrokerTests.create
    save_backend = fixtures.BrokerTests.save_backend
    current = fixtures.BrokerTests.current
    exchange = fixtures.BrokerTests.exchange
    join_peers = fixtures.BrokerTests.join_peers
    peer_once = fixtures.BrokerTests.peer_once
    requests = fixtures.BrokerTests.requests
    pause = fixtures.BrokerTests.pause

    def setUp(self):
        fixtures.BrokerTests.setUp(self)
        self.bridge = responses.Bridge(self.broker)
        self.addCleanup(self.bridge.close)
        self.body = {"model": self.bridge.policy["model"], "instructions": "Synthetic protocol test only.",
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Inspect fixture"}]}],
            "tools": [{"type": "function", "name": "exec_command", "description": "fixture", "strict": False,
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}],
            "tool_choice": "auto", "parallel_tool_calls": True, "reasoning": {"effort": self.bridge.policy["effort"], "summary": "auto"},
            "store": False, "stream": True, "include": ["reasoning.encrypted_content"], "prompt_cache_key": "fixture-thread"}

    def response(self, envelope, output=None):
        body = fleet_json.loads(envelope["input"])
        values = bundle(body, [message()] if output is None else output)
        return self.peer_response(envelope, values)

    def peer_response(self, envelope, values):
        return fleet_json.canonical_bytes({**{k: envelope[k] for k in ("policy_id", "request_id", "request_sha256", "model")},
            "output": fleet_json.canonical_bytes(values).decode(), "output_tokens": 3})

    def http(self, body=None, *, token=None, path="/v1/responses", headers=None):
        result = {}
        def client():
            try:
                conn = http.client.HTTPConnection(self.bridge.host, timeout=5)
                raw = json.dumps(self.body if body is None else body).encode()
                conn.request("POST", path, body=raw, headers={"Content-Type": "application/json",
                    "Authorization": "Bearer " + (self.bridge.token if token is None else token),
                    "x-client-request-id": "same-thread-on-every-call", **(headers or {})})
                reply = conn.getresponse()
                result.update(status=reply.status, content_type=reply.getheader("Content-Type"), raw=reply.read())
                conn.close()
            except Exception as exc:
                result["error"] = type(exc).__name__
        thread = threading.Thread(target=client)
        thread.start()
        try:
            self.bridge.serve_once()
        except inference.InferenceError:
            pass
        finally:
            thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        return result

    def decode(self, result):
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["content_type"], "text/event-stream")
        return [json.loads(block.split(b"\ndata: ", 1)[1]) for block in result["raw"].split(b"\n\n") if block]

    def test_http_to_private_broker_sse_and_offline_receipt(self):
        self.peer_once()
        values = self.decode(self.http())
        self.assertEqual(values[-1]["response"]["output"], [message()])
        self.assertEqual(json.loads(self.calls[0]["input"]), self.body)
        self.assertNotIn("authorization", self.calls[0])
        checked = inference.verify_evidence(self.current(), lambda pin: artifacts.get_bytes(self.runs, self.mid, pin))
        self.assertEqual(checked["requests"]["completed"], 1)
        for path in (self.runs / "missions" / self.mid / "artifacts").iterdir():
            self.assertNotIn(self.bridge.token.encode(), path.read_bytes())

    def test_capability_echo_is_rejected_before_provider_and_cas(self):
        body = copy.deepcopy(self.body)
        body["input"][0]["content"][0]["text"] = self.bridge.token
        self.assertEqual(self.http(body)["status"], 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.requests(), {})
        for path in (self.runs / "missions" / self.mid / "artifacts").iterdir():
            self.assertNotIn(self.bridge.token.encode(), path.read_bytes())

    def test_tool_call_result_round_trip_and_same_thread_header_is_not_request_identity(self):
        call = {"id": "fc_fixture", "type": "function_call", "name": "exec_command", "call_id": "call_fixture",
            "arguments": '{"cmd":"touch SHOULD_NEVER_EXIST"}'}
        self.peer_once(lambda e: self.response(e, [call]))
        values = self.decode(self.http())
        self.assertEqual(values[1]["item"], call)
        continued = copy.deepcopy(self.body)
        continued["input"].extend([call, {"type": "function_call_output", "call_id": "call_fixture", "output": "synthetic result"}])
        self.peer_once()
        result = self.decode(self.http(continued))
        self.assertEqual(result[-1]["response"]["output"], [message()])
        self.assertEqual(json.loads(self.calls[1]["input"]), continued)
        self.assertEqual(len(self.requests()), 2)
        self.assertFalse((self.candidate / "SHOULD_NEVER_EXIST").exists())
        self.assertFalse((Path.cwd() / "SHOULD_NEVER_EXIST").exists())

    def test_repeated_http_body_reuses_durable_result_without_provider_send(self):
        self.peer_once()
        first = self.http()
        second = self.http()
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.requests()), 1)

    def test_captured_current_cli_shape_round_trips_content_tools_and_history(self):
        fixture = fleet_json.load(Path(__file__).parent / "fixtures/codex_responses_0_153_4.json")
        self.body = fixture["body"]
        self.assertEqual(self.body["model"], self.bridge.policy["model"])
        call = {"id": "ct_current", "type": "custom_tool_call", "call_id": "current_call",
            "namespace": "functions", "name": "exec", "input": "synthetic; never executed"}
        self.peer_once(lambda e: self.response(e, [call, message()]))
        first = self.http()
        values = self.decode(first)
        self.assertEqual(values[-1]["response"]["output"], [call, message()])
        self.assertEqual(fleet_json.loads(self.calls[0]["input"]), self.body)
        self.assertEqual(self.http(), first)
        self.assertEqual(len(self.calls), 1)
        self.body["input"].extend([call, {"type": "custom_tool_call_output",
            "call_id": "current_call", "output": "Synthetic result"}])
        self.peer_once()
        self.decode(self.http())
        self.assertEqual(fleet_json.loads(self.calls[1]["input"]), self.body)
        checked = inference.verify_evidence(self.current(), lambda pin: artifacts.get_bytes(self.runs, self.mid, pin))
        self.assertEqual(checked["requests"]["completed"], 2)

    def test_invalid_content_tools_do_not_reserve_or_contact_provider(self):
        prefix = {"type": "additional_tools", "role": "developer", "tools": self.body["tools"]}
        original = copy.deepcopy(self.body)
        original.pop("tools")
        cases = [
            {**original, "input": [{**prefix, "role": "user"}, *original["input"]]},
            {**original, "input": [*original["input"], prefix]},
            {**original, "input": [prefix, prefix, *original["input"]]},
            {**self.body, "input": [prefix, *original["input"]]},
            {**original, "input": [{**prefix, "tools": [{"type": "web_search"}]}, *original["input"]]},
            {**original, "input": [{**prefix, "tools": prefix["tools"] * 2}, *original["input"]]},
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.http(body)["status"], 400)
        self.assertEqual(self.requests(), {})
        self.assertEqual(self.calls, [])

    def test_content_tool_prefix_does_not_authorize_undeclared_namespace(self):
        tools = self.body.pop("tools")
        self.body["input"].insert(0, {"type": "additional_tools", "role": "developer", "tools": tools})
        call = {"id": "fc_wrong_prefix", "type": "function_call", "call_id": "wrong_prefix",
            "namespace": "untrusted", "name": "exec_command", "arguments": "{}"}
        self.peer_once(lambda e: self.response(e, [call]))
        self.assertEqual(self.http()["status"], 400)

    def test_namespaced_custom_tool_and_opaque_reasoning_survive_round_trip(self):
        self.body["tools"] = [{"type": "namespace", "name": "functions", "description": "fixture",
            "tools": [{"type": "custom", "name": "apply_patch", "description": "fixture", "format": {"type": "text"}}]}]
        reasoning = {"id": "rs_fixture", "type": "reasoning", "summary": [], "encrypted_content": "opaque-synthetic-data"}
        call = {"id": "ct_fixture", "type": "custom_tool_call", "call_id": "custom_fixture",
            "namespace": "functions", "name": "apply_patch", "input": "synthetic patch; never executed"}
        self.peer_once(lambda e: self.response(e, [reasoning, call]))
        values = self.decode(self.http())
        self.assertEqual(values[-1]["response"]["output"], [reasoning, call])
        self.body["input"].extend([reasoning, call, {"type": "custom_tool_call_output", "call_id": "custom_fixture", "output": "synthetic result"}])
        self.peer_once()
        self.decode(self.http())
        self.assertEqual(json.loads(self.calls[-1]["input"]), self.body)

    def test_cancellation_before_cached_delivery_does_not_emit_sse(self):
        self.peer_once()
        self.decode(self.http())
        self.pause("cancel")
        self.assertEqual(self.http()["status"], 400)
        self.assertEqual(len(self.calls), 1)

    def test_forged_tool_namespace_is_rejected_before_http_success(self):
        call = {"id": "fc_wrong", "type": "function_call", "namespace": "untrusted", "name": "exec_command",
            "call_id": "call_wrong", "arguments": "{}"}
        self.peer_once(lambda e: self.response(e, [call]))
        self.assertEqual(self.http()["status"], 400)

    def test_broker_itself_rejects_invalid_protocol_input_without_the_http_adapter(self):
        body = {**self.body, "store": True}
        import uuid
        raw = fleet_json.canonical_bytes({"policy_id": self.policy_id, "request_id": str(uuid.uuid4()),
            "input": json.dumps(body), "max_output_tokens": 32})
        with self.assertRaises(inference.InferenceError):
            self.broker.handle(raw)
        self.assertEqual(self.requests(), {})

    def test_text_delta_cannot_disagree_with_retained_output(self):
        def invalid(e):
            values = bundle(self.body, [message()])
            values.insert(1, {"type": "response.output_text.delta", "item_id": "msg_fixture", "content_index": 0, "delta": "different text"})
            return self.peer_response(e, values)
        self.peer_once(invalid)
        self.assertEqual(self.http()["status"], 400)

    def test_added_tool_cannot_bypass_declaration_checks(self):
        def invalid(e):
            values = bundle(self.body, [message()])
            values.insert(1, {"type": "response.output_item.added", "item": {"id": "fc_untrusted", "type": "function_call",
                "name": "undeclared", "call_id": "bad", "arguments": ""}})
            return self.peer_response(e, values)
        self.peer_once(invalid)
        self.assertEqual(self.http()["status"], 400)

    def test_consistent_text_deltas_are_preserved_as_sse(self):
        def fragmented(e):
            values = bundle(self.body, [message()])
            values[1:1] = [{"type": "response.output_item.added", "item": {**message(), "content": []}},
                {"type": "response.output_text.delta", "item_id": "msg_fixture", "content_index": 0, "delta": "synthetic "},
                {"type": "response.output_text.delta", "item_id": "msg_fixture", "content_index": 0, "delta": "final"},
                {"type": "response.output_text.done", "item_id": "msg_fixture", "content_index": 0, "text": "synthetic final"}]
            return self.peer_response(e, values)
        self.peer_once(fragmented)
        values = self.decode(self.http())
        self.assertEqual("".join(e["delta"] for e in values if e["type"] == "response.output_text.delta"), "synthetic final")

    def test_missing_capability_foreign_host_and_unsupported_routes_never_reserve(self):
        for opts in ({"token": "wrong"}, {"path": "/v1/responses?url=https://example.invalid"},
                {"path": "/v1/models"}, {"headers": {"Host": "elsewhere"}}, {"headers": {"Origin": "http://elsewhere"}},
                {"headers": {"Content-Encoding": "zstd"}}, {"headers": {"Upgrade": "websocket"}}):
            self.assertEqual(self.http(**opts)["status"], 400)
        self.assertEqual(self.requests(), {})

    def test_model_hosted_tools_and_weakened_persistence_rejected_before_peer(self):
        for change in ({"model": "other"}, {"store": True}, {"stream": False},
                {"tools": [{"type": "web_search"}]}, {"service_tier": "priority"}, {"reasoning": {"effort": "low"}}):
            self.assertEqual(self.http({**self.body, **change})["status"], 400)
        self.assertEqual(self.requests(), {})

    def test_truncated_provider_stream_never_becomes_http_success_and_never_retries(self):
        self.peer_once(lambda e: self.peer_response(e, bundle(self.body, [message()])[:-1]))
        self.assertEqual(self.http()["status"], 400)
        self.assertEqual(self.http()["status"], 400)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(next(iter(self.requests().values()))["result"]["status"], "indeterminate")

    def test_terminal_output_mismatch_rejected_before_sse_delivery(self):
        def invalid(e):
            values = bundle(self.body, [message()])
            values[-1]["response"]["output"] = [message("forged")]
            return self.peer_response(e, values)
        self.peer_once(invalid)
        self.assertEqual(self.http()["status"], 400)

    def test_rehashed_sse_corruption_is_rejected_by_offline_verification(self):
        self.peer_once()
        self.decode(self.http())
        current = self.current()
        saved = {p.name: p.read_bytes() for p in (self.runs / "missions" / self.mid / "artifacts").iterdir()}
        outcome = next(iter(current["inference_policies"][self.policy_id]["requests"].values()))["result"]
        receipt = fleet_json.loads(saved[outcome["receipt_artifact_id"]])
        reply = fleet_json.loads(saved[receipt["response_artifact_id"]])
        values = fleet_json.loads(reply["output"])
        values[-1]["response"]["output"] = [message("forged after delivery")]
        reply["output"] = fleet_json.canonical_bytes(values).decode()
        reply_raw = fleet_json.canonical_bytes(reply)
        receipt["response_artifact_id"] = inference.sha(reply_raw)
        saved[receipt["response_artifact_id"]] = reply_raw
        receipt_raw = fleet_json.canonical_bytes(receipt)
        outcome["receipt_artifact_id"] = inference.sha(receipt_raw)
        saved[outcome["receipt_artifact_id"]] = receipt_raw
        with self.assertRaisesRegex(inference.InferenceError, "terminal output differs"):
            inference.verify_evidence(current, saved.__getitem__)

    def test_invalid_json_types_and_non_text_input_fail_before_reservation(self):
        for changes in ({"tools": False}, {"input": [{"type": "function_call", "name": "x", "call_id": "c", "arguments": {}}]},
                {"input": [{"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "https://example.invalid"}]}]}):
            self.assertEqual(self.http({**self.body, **changes})["status"], 400)
        self.assertEqual(self.requests(), {})

    def test_provider_configuration_uses_custom_identity_and_no_ambient_auth(self):
        config = self.bridge.client_config()
        self.assertEqual(config["model_provider"], "fleet-local")
        provider = config["model_providers"]["fleet-local"]
        self.assertEqual(provider["base_url"], "http://"+self.bridge.host+"/v1")
        self.assertFalse(provider["requires_openai_auth"])
        self.assertFalse(provider["supports_websockets"])
        self.assertEqual(provider["request_max_retries"], 0)
        self.assertEqual(provider["stream_max_retries"], 0)
        self.assertNotIn(self.bridge.token, json.dumps(config))
        self.assertEqual(self.bridge.client_environment(), {responses.CAPABILITY_ENV: self.bridge.token})

    @unittest.skipUnless(os.environ.get("FLEET_CODEX_PROTOCOL_PROBE"), "explicit prebuilt local codex-api probe required")
    def test_real_codex_rust_client_consumes_tool_and_final_sse(self):
        call = {"id": "fc_rust", "type": "function_call", "name": "exec_command", "call_id": "call_rust", "arguments": '{"cmd":"true"}'}
        self.peer_once(lambda e: self.response(e, [call, message()]))
        child = subprocess.Popen([os.environ["FLEET_CODEX_PROTOCOL_PROBE"]], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"}, cwd=self.tmp)
        def stop():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)
        self.addCleanup(stop)
        child.stdin.write(fleet_json.canonical_bytes({"port": self.bridge.listener.getsockname()[1],
            "capability": self.bridge.token, "body": self.body}))
        child.stdin.close()
        child.stdin = None
        self.bridge.serve_once()
        out, err = child.communicate(timeout=5)
        self.assertEqual((child.returncode, err), (0, b""))
        parsed = fleet_json.loads(out)
        self.assertEqual(parsed["items"], [call, message()])
        self.assertEqual(parsed["completed"], "resp_fixture")
        self.assertEqual(parsed["output_tokens"], 3)
        self.assertEqual(len(self.calls), 1)
        self.rust_observation = {"returncode": child.returncode, "stdout": parsed,
            "stderr": err.decode(), "scope": "codex-api HTTP client and SSE parser; no agent or tool execution"}


if __name__ == "__main__":
    unittest.main()
