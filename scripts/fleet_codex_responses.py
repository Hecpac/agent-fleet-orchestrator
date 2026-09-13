"""Bounded Codex HTTP/SSE adapter; local fixture provider, no tool execution.

Wire shape covers the local 0.153.0 client and captured 0.153.4 requests.
The broker retains complete SSE events before any successful HTTP response.
This module never starts Codex, resolves upstream URLs or loads credentials.
"""
from __future__ import annotations

import hmac
import re
import secrets
import select
import socket
import time
import uuid

import fleet_herdr_inference as broker
import fleet_json

PROFILE = "local-codex-responses-v1"
CAPABILITY_ENV = "FLEET_INFERENCE_CAPABILITY"
MAX_HTTP = 128 * 1024


def check(condition, message):
    broker.check(condition, message)


def tool_declarations(tools):
    check(type(tools) is list and len(tools) <= 128, "Responses tools list")
    declarations = {}
    def add(tool, namespace):
        check(type(tool) is dict and tool.get("type") in {"function", "custom"}
              and type(tool.get("name")) is str and 0 < len(tool["name"]) <= 128,
              "hosted tools are not supported by the local protocol")
        key = (namespace, tool["name"])
        check(key not in declarations and len(declarations) < 128, "duplicate or excessive tool declarations")
        declarations[key] = tool["type"]
    for tool in tools:
        check(type(tool) is dict, "Responses tool declaration")
        if tool.get("type") == "namespace":
            check(type(tool.get("name")) is str and 0 < len(tool["name"]) <= 128
                  and type(tool.get("tools")) is list, "Responses namespace")
            for child in tool["tools"]:
                add(child, tool["name"])
        else:
            add(tool, None)
    return declarations


def response_item(item):
    allowed = {"message", "reasoning", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}
    check(type(item) is dict and item.get("type", "message") in allowed, "unsupported Responses item")
    kind = item.get("type", "message")
    if kind == "message":
        check(item.get("role") in {"system", "developer", "user", "assistant"}
              and type(item.get("content")) is list, "Responses message content")
        for part in item["content"]:
            check(type(part) is dict and part.get("type") in {"input_text", "output_text"}
                  and type(part.get("text")) is str, "only text content is supported")
    elif kind == "reasoning":
        check(type(item.get("summary")) is list and (item.get("encrypted_content") is None
              or type(item["encrypted_content"]) is str), "Responses reasoning item")
    elif kind in {"function_call", "custom_tool_call"}:
        check(type(item.get("call_id")) is str and type(item.get("name")) is str
              and (item.get("namespace") is None or type(item["namespace"]) is str)
              and type(item.get("arguments" if kind == "function_call" else "input")) is str, "Responses call item")
    else:
        check(type(item.get("call_id")) is str and type(item.get("output")) in {str, list}, "Responses tool result")
        if type(item["output"]) is list:
            for part in item["output"]:
                check(type(part) is dict and part.get("type") in {"input_text", "output_text"}
                      and type(part.get("text")) is str, "only text tool results are supported")


def request_tools(body):
    """Support Codex's content-item tool prefix without changing wire bytes.

    A single developer prefix is the observed content_item_kinds shape. Mixed
    or repeated declarations are ambiguous and stay outside this profile.
    These are declarations only; they do not authorize local tool execution.
    """
    tools = [] if body.get("tools") is None else body["tools"]
    declarations = tool_declarations(tools)
    for index, item in enumerate(body["input"]):
        if type(item) is not dict or item.get("type") != "additional_tools":
            continue
        check(index == 0 and not tools and item.get("role") == "developer"
              and {"type", "role", "tools"} <= set(item) <= {"type", "id", "role", "tools"}
              and (item.get("id") is None or type(item["id"]) is str),
              "unsupported or ambiguous additional_tools prefix")
        declarations = tool_declarations(item["tools"])
    return declarations


def request(raw, policy):
    check(type(raw) is bytes and 0 < len(raw) <= MAX_HTTP, "Responses request size")
    value = fleet_json.loads(raw)
    required = {"model", "input", "tool_choice", "parallel_tool_calls", "reasoning", "store", "stream", "include"}
    optional = {"instructions", "tools", "stream_options", "prompt_cache_key", "text", "client_metadata", "max_output_tokens"}
    check(type(value) is dict and required <= set(value) <= required | optional, "unsupported Responses fields")
    check(value["model"] == policy["model"] and value["store"] is False and value["stream"] is True,
          "Responses model or persistence differs")
    reasoning = value["reasoning"]
    check(type(reasoning) is dict and set(reasoning) <= {"effort", "summary", "context"}
          and reasoning.get("effort") == policy["effort"], "Responses reasoning differs")
    check(type(value["input"]) is list and 1 <= len(value["input"]) <= 512, "Responses input list")
    request_tools(value)
    for item in value["input"]:
        if type(item) is dict and item.get("type") == "additional_tools":
            continue
        response_item(item)
    check(type(value["parallel_tool_calls"]) is bool and value["tool_choice"] in {"auto", "none"}, "Responses tool selection")
    check(type(value["include"]) is list and all(v == "reasoning.encrypted_content" for v in value["include"]), "Responses include")
    if "max_output_tokens" in value:
        check(type(value["max_output_tokens"]) is int and 1 <= value["max_output_tokens"] <= policy["max_output_tokens"], "Responses output reservation")
    # Preserve instructions, all conversation items, schemas and opaque reasoning.
    # No item is interpreted as a local command or fetched as an external URL.
    return value


def events(raw, body, model, output_limit):
    check(type(raw) is bytes and 0 < len(raw) <= broker.LIMIT, "Responses event bundle size")
    values = fleet_json.loads(raw)
    check(type(values) is list and 2 <= len(values) <= 1024 and all(type(e) is dict for e in values), "Responses event bundle")
    check(values[0].get("type") == "response.created" and values[-1].get("type") == "response.completed",
          "Responses stream lacks terminal completion")
    response_id = values[0].get("response", {}).get("id")
    check(type(response_id) is str and 0 < len(response_id) <= 128, "Responses response identity")
    done, ids, text, added, arguments = [], set(), {}, {}, {}
    allowed = {"response.created", "response.in_progress", "response.output_item.added", "response.output_text.delta",
        "response.output_text.done", "response.content_part.added", "response.content_part.done",
        "response.function_call_arguments.delta", "response.function_call_arguments.done",
        "response.custom_tool_call_input.delta", "response.custom_tool_call_input.done",
        "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
        "response.output_item.done", "response.completed"}
    tools = request_tools(body)
    call_ids = set()
    for index, event in enumerate(values):
        check(type(event) is dict and event.get("type") in allowed, "unsupported Responses event")
        kind = event["type"]
        if "sequence_number" in event:
            check(type(event["sequence_number"]) is int and event["sequence_number"] == index, "Responses event sequence")
        if kind == "response.created":
            check(index == 0, "duplicate Responses creation")
        if kind == "response.in_progress":
            check(event.get("response", {}).get("id") == response_id
                  and event["response"].get("status") == "in_progress", "Responses progress identity differs")
        if kind == "response.completed":
            check(index == len(values)-1, "premature Responses completion")
        if "response_id" in event:
            check(event["response_id"] == response_id, "Responses event identity differs")
        if kind in {"response.output_text.delta", "response.output_text.done"}:
            field = "delta" if kind.endswith(".delta") else "text"
            check(type(event.get(field)) is str and type(event.get("item_id")) is str
                  and type(event.get("content_index", 0)) is int and event.get("content_index", 0) >= 0,
                  "Responses text delta")
            key = (event["item_id"], event.get("content_index", 0))
            if field == "text":
                check(key not in text or text[key] == event[field], "Responses completed text differs from deltas")
                text[key] = event[field]
            else:
                text[key] = text.get(key, "") + event[field]
        if kind in {"response.function_call_arguments.delta", "response.function_call_arguments.done",
                    "response.custom_tool_call_input.delta", "response.custom_tool_call_input.done"}:
            field = "arguments" if "function_call" in kind else "input"
            key = (event.get("item_id"), field)
            value = event.get("delta" if kind.endswith(".delta") else field)
            check(type(key[0]) is str and type(value) is str, "Responses argument delta")
            if kind.endswith(".done"):
                check(key not in arguments or arguments[key] == value, "Responses completed arguments differ from deltas")
                arguments[key] = value
            else:
                arguments[key] = arguments.get(key, "") + value
        if kind in {"response.output_item.added", "response.output_item.done"}:
            item = event.get("item")
            response_item(item)
            check(type(item) is dict and item.get("type") in {"message", "function_call", "custom_tool_call", "reasoning"}, "Responses output item")
            item_id = item.get("id")
            check(type(item_id) is str and item_id not in ids, "Responses duplicate output item")
            if item["type"] in {"function_call", "custom_tool_call"}:
                expected_kind = "function" if item["type"] == "function_call" else "custom"
                check(body["tool_choice"] != "none" and tools.get((item.get("namespace"), item.get("name"))) == expected_kind,
                      "Responses call is not a declared tool")
            identity = tuple(item.get(k) for k in ("type", "name", "namespace", "call_id", "role"))
            if kind.endswith(".added"):
                check(item_id not in added, "duplicate Responses added item")
                added[item_id] = identity
                continue
            check(item_id not in added or added[item_id] == identity, "Responses added/done item differs")
            ids.add(item_id)
            if item["type"] == "message":
                check(item.get("role") == "assistant" and type(item.get("content")) is list, "Responses assistant item")
                for content_index, part in enumerate(item["content"]):
                    check(type(part) is dict and part.get("type") == "output_text" and type(part.get("text")) is str,
                          "Responses output text")
                    if (item_id, content_index) in text:
                        check(text[(item_id, content_index)] == part["text"], "Responses text deltas differ from final item")
            if item["type"] in {"function_call", "custom_tool_call"}:
                expected_kind = "function" if item["type"] == "function_call" else "custom"
                check(body["tool_choice"] != "none" and tools.get((item.get("namespace"), item.get("name"))) == expected_kind
                      and type(item.get("call_id")) is str and item["call_id"] not in call_ids
                      and type(item.get("arguments" if item["type"] == "function_call" else "input")) is str,
                      "Responses call is not a declared tool")
                call_ids.add(item["call_id"])
            done.append(item)
    final = values[-1].get("response", {})
    check(final.get("id") == response_id and final.get("model") == model and final.get("status") == "completed"
          and final.get("output") == done and bool(done), "Responses terminal output differs")
    check(all(item_id in ids for item_id, _ in text), "Responses text delta lacks a final item")
    check(set(added) <= ids, "Responses added item lacks completion")
    final_text = {(i["id"], n): p["text"] for i in done if i["type"] == "message" for n, p in enumerate(i["content"])}
    check(all(final_text.get(k) == v for k, v in text.items()), "Responses text delta differs from final content")
    final_args = {(i["id"], field): i[field] for i in done for field in ("arguments", "input") if field in i}
    check(all(final_args.get(k) == v for k, v in arguments.items()), "Responses argument delta differs from final call")
    usage = final.get("usage")
    check(type(usage) is dict and all(type(usage.get(k)) is int and usage[k] >= 0
          for k in ("input_tokens", "output_tokens", "total_tokens"))
          and usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
          and usage["output_tokens"] <= output_limit, "Responses terminal usage")
    return values


def sse(values):
    return b"".join(b"event: " + e["type"].encode("ascii") + b"\ndata: " + fleet_json.canonical_bytes(e) + b"\n\n" for e in values)


class Bridge:
    """One CONTROL-owned foreground listener; no background daemon or global config."""
    def __init__(self, owner):
        self.broker = owner
        with owner.publisher.transaction():
            policy = owner.active()["policy"]
            check(policy["profile"] == PROFILE, "Responses requires its frozen broker profile")
            self.policy = dict(policy)
        self.token = secrets.token_urlsafe(32)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.host = "127.0.0.1:" + str(self.listener.getsockname()[1])

    def close(self):
        self.listener.close()

    def client_config(self):
        # 0.153.0 reserves built-in provider IDs. Do not impersonate "openai"
        # or weaken transcript acceptance to make this fixture appear real.
        return {"model_provider": "fleet-local", "model": self.policy["model"],
            "model_reasoning_effort": self.policy["effort"],
            "features": {"enable_request_compression": False},
            "model_providers": {"fleet-local": {"name": "Fleet local fixture", "wire_api": "responses",
                "base_url": "http://" + self.host + "/v1", "env_key": CAPABILITY_ENV,
                "requires_openai_auth": False, "supports_websockets": False,
                "supports_standalone_web_search": False, "request_max_retries": 0, "stream_max_retries": 0}}}

    def client_environment(self):
        return {CAPABILITY_ENV: self.token}

    def execute_confined(self, prompt, **options):
        """CONTROL-only local capsule; this never grants ordinary Herdr boot."""
        import fleet_codex_sandbox
        return fleet_codex_sandbox.execute(self, prompt, **options)

    def _read(self, conn, deadline):
        def recv():
            while True:
                self.broker.poll()
                remaining = deadline-time.monotonic()
                check(remaining > 0, "HTTP request deadline")
                if select.select([conn], [], [], min(.05, remaining))[0]:
                    block = conn.recv(4096)
                    check(bool(block), "HTTP request closed")
                    return block
        data = bytearray()
        while b"\r\n\r\n" not in data:
            data.extend(recv())
            check(len(data) <= 16384, "HTTP headers too large")
        head, rest = bytes(data).split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        check(lines[0] == "POST /v1/responses HTTP/1.1", "unsupported HTTP route")
        headers = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            check(separator and re.fullmatch(r"[A-Za-z0-9-]+", name) and name.lower() not in headers, "ambiguous HTTP headers")
            headers[name.lower()] = value.strip()
        check(headers.get("host") == self.host and not any(k in headers for k in
            ("transfer-encoding", "content-encoding", "upgrade", "expect", "origin")), "unsupported HTTP transport")
        check(headers.get("content-type") == "application/json", "HTTP content type")
        check(hmac.compare_digest(headers.get("authorization", ""), "Bearer " + self.token), "HTTP capability denied")
        length = headers.get("content-length", "")
        check(re.fullmatch(r"[0-9]{1,6}", length) and 0 < int(length) <= MAX_HTTP, "HTTP content length")
        while len(rest) < int(length):
            rest += recv()
        check(len(rest) == int(length), "HTTP pipelining is not supported")
        return rest

    def serve_once(self, timeout=3):
        check(type(timeout) in {int, float} and 0 < timeout <= 30, "HTTP accept timeout")
        self.listener.settimeout(timeout)
        conn, address = self.listener.accept()
        with conn:
            started = False
            try:
                check(address[0] == "127.0.0.1", "HTTP peer must be loopback")
                raw = self._read(conn, time.monotonic()+3)
                check(self.token.encode() not in raw, "local capability cannot enter inference payload")
                body = request(raw, self.policy)
                check(self.token not in fleet_json.canonical_bytes(body).decode(), "local capability cannot enter inference payload")
                canonical = fleet_json.canonical_bytes(body)
                # Thread/session headers are metadata, never a request identity.
                rid = str(uuid.uuid5(uuid.UUID(self.policy["attempt"]["attempt_id"]),
                    self.broker.policy_id + ":" + broker.sha(canonical)))
                limit = body.get("max_output_tokens", min(1024, self.policy["max_output_tokens"]))
                inner = fleet_json.canonical_bytes({"policy_id": self.broker.policy_id, "request_id": rid,
                    "input": canonical.decode(), "max_output_tokens": limit})
                result = fleet_json.loads(self.broker.handle(inner))
                stream = sse(events(result["output"].encode(), body, self.policy["model"], limit))
                response = ("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                    "Connection: close\r\nContent-Length: " + str(len(stream)) + "\r\n\r\n").encode() + stream
                with self.broker.publisher.transaction():
                    self.broker.active()
                    conn.settimeout(1)
                    started = True
                    conn.sendall(response)
                return {"request_id": rid, "policy_id": self.broker.policy_id, "scope": "local_responses_protocol", "authority": "none"}
            except Exception:
                if not started:
                    try:
                        conn.settimeout(.5)
                        conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    except OSError:
                        pass
                # Fixed error only; never echo input, provider diagnostics or token.
                raise broker.InferenceError("local Responses request rejected") from None
