#!/usr/bin/env python3
"""Authenticated specialist MCP proxy for one exact Fleet Control socket."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import stat
import sys
from typing import Any
import uuid

import fleet_json

PROTOCOL_VERSION = "2024-11-05"
MAX_REQUEST_FRAME_BYTES = 2_000_000
# A 16 MiB artifact expands to about 21.4 MiB as base64 before JSON framing.
MAX_REPLY_FRAME_BYTES = 24 * 1024 * 1024
SOCKET_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0
MAX_WAIT_SECONDS = 1800
WAIT_GRACE_SECONDS = 15.0
UUID_PATTERN = (
    "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
INSTANCE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
CALLER_FIELDS = {
    "_caller_run_id": {"type": "string", "format": "uuid", "pattern": UUID_PATTERN},
    "_caller_token_id": {"type": "string", "format": "uuid", "pattern": UUID_PATTERN},
}
CALLER_REQUIRED = ["_caller_run_id", "_caller_token_id"]
RESERVED_ARGUMENT_KEYS = {
    "parent_run_id",
    "token_id",
    "token_path",
    "capability_token",
}


class AgentProxyError(RuntimeError):
    """A fail-closed proxy or transport validation error."""


def _loads_strict(raw: bytes) -> Any:
    return fleet_json.loads(raw)


def _dumps_strict(value: Any) -> bytes:
    return fleet_json.canonical_bytes(value)


def _schema(
    required: list[str] | None = None,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [*(required or []), *CALLER_REQUIRED],
        "properties": {**(properties or {}), **CALLER_FIELDS},
    }


DISPATCH_PROPERTIES: dict[str, Any] = {
    "recipient_instance": {"type": "string"},
    "capability": {"type": "string"},
    "objective": {"type": "string"},
    "idempotency_key": {"type": "string"},
    "input_artifact_ids": {"type": "array", "items": {"type": "string"}},
    "can_delegate": {"type": "boolean"},
    "allowed_capabilities": {"type": "array", "items": {"type": "string"}},
    "remaining_budget": {"type": "integer", "minimum": 0},
}
DISPATCH_REQUIRED = [
    "recipient_instance",
    "capability",
    "objective",
    "idempotency_key",
]
DISPATCH_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": DISPATCH_REQUIRED,
    "properties": DISPATCH_PROPERTIES,
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "dispatch",
        "description": "Dispatch one tracked child capability within the caller grant.",
        "inputSchema": _schema(DISPATCH_REQUIRED, DISPATCH_PROPERTIES),
    },
    {
        "name": "dispatch_many",
        "description": "Dispatch several tracked child capabilities within the caller grant.",
        "inputSchema": _schema(
            ["requests"],
            {
                "requests": {
                    "type": "array",
                    "items": DISPATCH_ITEM_SCHEMA,
                    "minItems": 1,
                }
            },
        ),
    },
    {
        "name": "wait",
        "description": "Wait for exact direct-child run IDs.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": _schema(
            ["run_ids"],
            {
                "run_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "timeout_seconds": {"type": "integer", "minimum": 1},
            },
        ),
    },
    {
        "name": "get_result",
        "description": "Read one exact content-addressed artifact in the caller grant.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": _schema(["artifact_id"], {"artifact_id": {"type": "string"}}),
    },
    {
        "name": "relay_result",
        "description": "Dispatch a child follow-up that references a granted artifact.",
        "inputSchema": _schema(
            [
                "artifact_id",
                "recipient_instance",
                "capability",
                "objective",
                "idempotency_key",
            ],
            {
                "artifact_id": {"type": "string"},
                "recipient_instance": {"type": "string"},
                "capability": {"type": "string"},
                "objective": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        ),
    },
    {
        "name": "request_assurance",
        "description": "Monotonically elevate mission risk and pause for assurance.",
        "inputSchema": _schema(
            ["risk", "categories", "reason", "idempotency_key"],
            {
                "risk": {"enum": ["high", "unknown"]},
                "categories": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        ),
    },
    {
        "name": "request_human",
        "description": "Record a durable scoped human decision request.",
        "inputSchema": _schema(
            ["reason", "scope", "idempotency_key"],
            {
                "reason": {"type": "string"},
                "scope": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        ),
    },
    {
        "name": "inspect_roster",
        "description": "Inspect the compiled live capabilities visible to the caller.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": _schema(),
    },
    {
        "name": "inspect_mission",
        "description": "Inspect durable mission state scoped to the caller.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": _schema(),
    },
]
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)
TOOL_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
GENERIC_FAILURE_MESSAGE = "specialist control request failed closed"
PREFLIGHT_RUN_ENV = "FLEET_PREFLIGHT_RUN_ID"
PREFLIGHT_TOKEN_ENV = "FLEET_PREFLIGHT_TOKEN_ID"
PREFLIGHT_SENTINEL_ID = "00000000-0000-4000-8000-000000000000"


def response(
    identifier: Any,
    *,
    result: Any = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier}
    if error is not None:
        value["error"] = error
    else:
        value["result"] = result
    return value


def _tool_error(identifier: Any, message: str) -> dict[str, Any]:
    return response(
        identifier,
        result={
            "content": [{"type": "text", "text": message}],
            "isError": True,
        },
    )


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise AgentProxyError(f"{field} must be a canonical UUID")
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise AgentProxyError(f"{field} must be a canonical UUID") from exc
    if canonical != value:
        raise AgentProxyError(f"{field} must be a canonical lowercase UUID")
    return canonical


def _validate_schema(value: Any, schema: dict[str, Any], where: str) -> None:
    if "enum" in schema and value not in schema["enum"]:
        raise AgentProxyError(f"{where} is outside the allowed values")
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise AgentProxyError(f"{where} must be an object")
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        if required - set(value):
            raise AgentProxyError(f"{where} is missing required fields")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise AgentProxyError(f"{where} contains unknown fields")
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, dict):
                _validate_schema(item, child, f"{where}.{key}")
        return
    if expected == "array":
        if not isinstance(value, list):
            raise AgentProxyError(f"{where} must be an array")
        minimum = schema.get("minItems")
        if type(minimum) is int and len(value) < minimum:
            raise AgentProxyError(f"{where} has too few items")
        child = schema.get("items")
        if isinstance(child, dict):
            for index, item in enumerate(value):
                _validate_schema(item, child, f"{where}[{index}]")
        return
    if expected == "string" and not isinstance(value, str):
        raise AgentProxyError(f"{where} must be a string")
    if expected == "boolean" and type(value) is not bool:
        raise AgentProxyError(f"{where} must be a boolean")
    if expected == "integer":
        if type(value) is not int:
            raise AgentProxyError(f"{where} must be an integer")
        minimum = schema.get("minimum")
        if type(minimum) is int and value < minimum:
            raise AgentProxyError(f"{where} is below the minimum")


def _sanitize_arguments(name: str, arguments: Any) -> tuple[str, str, dict[str, Any]]:
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        raise AgentProxyError("specialist tool is not authorized")
    _validate_schema(arguments, schema, f"{name} arguments")
    assert isinstance(arguments, dict)
    sanitized = dict(arguments)
    run_id = _canonical_uuid(sanitized.pop("_caller_run_id", None), "caller run ID")
    token_id = _canonical_uuid(
        sanitized.pop("_caller_token_id", None), "caller token ID"
    )
    if RESERVED_ARGUMENT_KEYS & sanitized.keys():
        raise AgentProxyError(
            "specialist tool arguments contain reserved identity fields"
        )
    if any(key.startswith("_caller_") for key in sanitized):
        raise AgentProxyError(
            "specialist tool arguments contain an unknown caller field"
        )
    return run_id, token_id, sanitized


def _bind_delegated_identity(
    name: str, arguments: dict[str, Any], run_id: str, token_id: str
) -> dict[str, Any]:
    bound = dict(arguments)
    if name in {"dispatch", "relay_result"}:
        bound["parent_run_id"] = run_id
        bound["token_id"] = token_id
    elif name == "dispatch_many":
        requests = bound.get("requests")
        if not isinstance(requests, list) or not requests:
            raise AgentProxyError("dispatch_many requires a non-empty request list")
        bound_requests: list[dict[str, Any]] = []
        for item in requests:
            if not isinstance(item, dict):
                raise AgentProxyError("dispatch_many requests must be objects")
            if RESERVED_ARGUMENT_KEYS & item.keys() or any(
                key.startswith("_caller_") for key in item
            ):
                raise AgentProxyError(
                    "dispatch_many request contains reserved identity fields"
                )
            value = dict(item)
            value["parent_run_id"] = run_id
            value["token_id"] = token_id
            bound_requests.append(value)
        bound["requests"] = bound_requests
    return bound


def _socket_path() -> str:
    path = os.environ.get("FLEET_CONTROL_SOCKET", "")
    if not path or not os.path.isabs(path):
        raise AgentProxyError(
            "an active absolute specialist control socket is required"
        )
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise AgentProxyError("the specialist control socket is unavailable") from exc
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise AgentProxyError(
            "the specialist control socket owner, type, or mode is invalid"
        )
    return path


def _read_socket_reply(connection: socket.socket) -> Any:
    chunks = bytearray()
    while True:
        chunk = connection.recv(min(65_536, MAX_REPLY_FRAME_BYTES + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_REPLY_FRAME_BYTES:
            raise AgentProxyError("specialist control reply exceeds the frame limit")
    if not chunks or len(chunks) > MAX_REPLY_FRAME_BYTES:
        raise AgentProxyError("specialist control returned an invalid frame")
    if chunks.count(b"\n") != 1 or not chunks.endswith(b"\n"):
        raise AgentProxyError("specialist control reply must be exactly one line")
    try:
        return _loads_strict(bytes(chunks[:-1]))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise AgentProxyError("specialist control returned invalid JSON") from exc


def _socket_request(envelope: dict[str, Any], *, read_timeout: float) -> Any:
    payload = _dumps_strict(envelope) + b"\n"
    if len(payload) > MAX_REQUEST_FRAME_BYTES:
        raise AgentProxyError("specialist control request exceeds the frame limit")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(SOCKET_CONNECT_TIMEOUT_SECONDS)
            connection.connect(_socket_path())
            connection.sendall(payload)
            connection.settimeout(read_timeout)
            return _read_socket_reply(connection)
    except AgentProxyError:
        raise
    except (OSError, TimeoutError) as exc:
        raise AgentProxyError("specialist control transport is unavailable") from exc


def _validate_tool_result(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"content", "isError"}:
        raise AgentProxyError("specialist control returned an invalid MCP tool result")
    if not isinstance(value["isError"], bool) or not isinstance(value["content"], list):
        raise AgentProxyError("specialist control returned an invalid MCP tool result")
    if any(
        not isinstance(item, dict)
        or set(item) != {"type", "text"}
        or item.get("type") != "text"
        or not isinstance(item.get("text"), str)
        for item in value["content"]
    ):
        raise AgentProxyError("specialist control returned invalid MCP content")


def _validate_rpc_response(value: Any, identifier: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise AgentProxyError(
            "specialist control returned an invalid JSON-RPC response"
        )
    if type(value.get("id")) is not type(identifier) or value.get("id") != identifier:
        raise AgentProxyError(
            "specialist control response ID does not match the request"
        )
    has_result = "result" in value
    has_error = "error" in value
    if has_result == has_error:
        raise AgentProxyError(
            "specialist control returned an ambiguous JSON-RPC response"
        )
    expected = {"jsonrpc", "id", "result" if has_result else "error"}
    if set(value) != expected:
        raise AgentProxyError("specialist control returned unexpected JSON-RPC fields")
    if has_result:
        _validate_tool_result(value["result"])
    else:
        error = value["error"]
        if (
            not isinstance(error, dict)
            or set(error) - {"code", "message", "data"}
            or type(error.get("code")) is not int
            or not isinstance(error.get("message"), str)
        ):
            raise AgentProxyError(
                "specialist control returned an invalid JSON-RPC error"
            )
    return value


def _forward_tool_call(
    identifier: Any, name: str, arguments: Any
) -> tuple[dict[str, Any], str, str]:
    run_id, token_id, sanitized = _sanitize_arguments(name, arguments)
    read_timeout = DEFAULT_TOOL_TIMEOUT_SECONDS
    if name == "wait":
        timeout_seconds = sanitized.get("timeout_seconds", MAX_WAIT_SECONDS)
        if (
            type(timeout_seconds) is not int
            or timeout_seconds < 1
            or timeout_seconds > MAX_WAIT_SECONDS
        ):
            raise AgentProxyError(
                f"wait timeout_seconds must be between 1 and {MAX_WAIT_SECONDS}"
            )
        read_timeout = float(timeout_seconds) + WAIT_GRACE_SECONDS
    bound = _bind_delegated_identity(name, sanitized, run_id, token_id)
    request = {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "tools/call",
        "params": {"name": name, "arguments": bound},
    }
    reply = _socket_request(
        {
            "schema_version": 2,
            "caller": {"run_id": run_id, "token_id": token_id},
            "request": request,
        },
        read_timeout=read_timeout,
    )
    if (
        not isinstance(reply, dict)
        or set(reply)
        not in (
            {"ok", "result"},
            {"ok", "error"},
        )
        or not isinstance(reply.get("ok"), bool)
    ):
        raise AgentProxyError("specialist control returned an invalid transport reply")
    if not reply["ok"]:
        if set(reply) != {"ok", "error"} or not isinstance(reply["error"], str):
            raise AgentProxyError("specialist control returned an invalid denial")
        # The trusted server's underlying filesystem exceptions can contain a
        # token-store path. Denials cross into model-visible MCP output only as
        # a generic status; durable CONTROL evidence retains the exact cause.
        raise AgentProxyError("specialist control denied the request")
    if set(reply) != {"ok", "result"} or not isinstance(reply["result"], dict):
        raise AgentProxyError("specialist control returned an invalid success reply")
    result = reply["result"]
    if set(result) != {"identity", "response"} or not isinstance(
        result["identity"], dict
    ):
        raise AgentProxyError(
            "specialist control returned an invalid identity envelope"
        )
    identity = result["identity"]
    if (
        set(identity) != {"kind", "instance", "run_id"}
        or identity.get("kind") != "specialist"
        or identity.get("run_id") != run_id
        or not isinstance(identity.get("instance"), str)
        or INSTANCE_PATTERN.fullmatch(identity["instance"]) is None
    ):
        raise AgentProxyError(
            "specialist control response identity does not match the caller"
        )
    validated = _validate_rpc_response(result["response"], identifier)
    if "error" in validated:
        validated = response(
            identifier,
            error={
                "code": validated["error"]["code"],
                "message": GENERIC_FAILURE_MESSAGE,
            },
        )
    elif validated["result"]["isError"]:
        validated = _tool_error(identifier, GENERIC_FAILURE_MESSAGE)
    return validated, run_id, token_id


def _redact_error(error: BaseException, run_id: str = "", token_id: str = "") -> str:
    del error, run_id, token_id
    return GENERIC_FAILURE_MESSAGE


def handle(request: Any) -> dict[str, Any] | None:
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return response(None, error={"code": -32600, "message": "Invalid Request"})
    identifier = request.get("id")
    method = request.get("method")
    if (
        identifier is None
        and isinstance(method, str)
        and method.startswith("notifications/")
    ):
        return None
    if type(identifier) not in {str, int}:
        return response(None, error={"code": -32600, "message": "Invalid Request"})
    if method == "initialize":
        return response(
            identifier,
            result={
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fleet-agent-control", "version": "1.0.0"},
            },
        )
    if method == "ping":
        return response(identifier, result={})
    if method == "tools/list":
        return response(identifier, result={"tools": TOOLS})
    if method != "tools/call":
        return response(
            identifier, error={"code": -32601, "message": "Method not found"}
        )
    params = request.get("params")
    if not isinstance(params, dict) or set(params) != {"name", "arguments"}:
        return _tool_error(identifier, "specialist tool call params are invalid")
    name = params.get("name")
    if not isinstance(name, str) or name not in TOOL_NAMES:
        return _tool_error(identifier, "specialist tool is not authorized")
    run_id = ""
    token_id = ""
    raw_arguments = params["arguments"]
    if isinstance(raw_arguments, dict):
        raw_run_id = raw_arguments.get("_caller_run_id")
        raw_token_id = raw_arguments.get("_caller_token_id")
        run_id = raw_run_id if isinstance(raw_run_id, str) else ""
        token_id = raw_token_id if isinstance(raw_token_id, str) else ""
    try:
        forwarded, run_id, token_id = _forward_tool_call(
            identifier, name, raw_arguments
        )
        return forwarded
    except (
        AgentProxyError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        return _tool_error(identifier, _redact_error(exc, run_id, token_id))


def _local_preflight() -> dict[str, Any]:
    initialized = handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    pinged = handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    listed = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    if (
        not isinstance(initialized, dict)
        or initialized.get("result", {}).get("protocolVersion") != PROTOCOL_VERSION
        or not isinstance(pinged, dict)
        or pinged.get("result") != {}
        or not isinstance(listed, dict)
        or not isinstance(listed.get("result", {}).get("tools"), list)
    ):
        raise AgentProxyError("local MCP protocol preflight failed")
    tools = listed["result"]["tools"]
    names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
    if len(names) != len(TOOL_NAMES) or set(names) != TOOL_NAMES:
        raise AgentProxyError("local MCP tool discovery preflight failed")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "tool_names": sorted(TOOL_NAMES),
    }


def _socket_preflight() -> str:
    run_value = os.environ.get(PREFLIGHT_RUN_ENV)
    token_value = os.environ.get(PREFLIGHT_TOKEN_ENV)
    if (run_value is None) != (token_value is None):
        raise AgentProxyError("preflight identity must be complete")
    authenticated = run_value is not None
    if authenticated:
        run_id = _canonical_uuid(run_value, "preflight run ID")
        token_id = _canonical_uuid(token_value, "preflight token ID")
    else:
        run_id = PREFLIGHT_SENTINEL_ID
        token_id = PREFLIGHT_SENTINEL_ID
    identifier = "fleet-agent-preflight-ping"
    reply = _socket_request(
        {
            "schema_version": 2,
            "caller": {"run_id": run_id, "token_id": token_id},
            "request": {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": "ping",
            },
        },
        read_timeout=DEFAULT_TOOL_TIMEOUT_SECONDS,
    )
    if (
        not isinstance(reply, dict)
        or set(reply)
        not in (
            {"ok", "result"},
            {"ok", "error"},
        )
        or type(reply.get("ok")) is not bool
    ):
        raise AgentProxyError("specialist control preflight returned an invalid reply")
    if not authenticated:
        if (
            reply.get("ok") is not False
            or set(reply) != {"ok", "error"}
            or not isinstance(reply.get("error"), str)
        ):
            raise AgentProxyError("specialist control authentication boundary is open")
        return "denial_ping"
    if reply.get("ok") is not True or set(reply) != {"ok", "result"}:
        raise AgentProxyError("authenticated specialist control ping was denied")
    result = reply.get("result")
    if not isinstance(result, dict) or set(result) != {"identity", "response"}:
        raise AgentProxyError("authenticated specialist control ping was invalid")
    identity = result.get("identity")
    rpc = result.get("response")
    if (
        not isinstance(identity, dict)
        or set(identity) != {"kind", "instance", "run_id"}
        or identity.get("kind") != "specialist"
        or identity.get("run_id") != run_id
        or not isinstance(identity.get("instance"), str)
        or INSTANCE_PATTERN.fullmatch(identity["instance"]) is None
        or not isinstance(rpc, dict)
        or set(rpc) != {"jsonrpc", "id", "result"}
        or rpc.get("jsonrpc") != "2.0"
        or rpc.get("id") != identifier
        or rpc.get("result") != {}
    ):
        raise AgentProxyError("authenticated specialist control ping was invalid")
    return "authenticated_ping"


def preflight() -> dict[str, Any]:
    local = _local_preflight()
    return {
        "schema_version": 1,
        "status": "ready",
        **local,
        "socket_probe": _socket_preflight(),
    }


def _discard_line_tail(stream: Any) -> None:
    while True:
        chunk = stream.readline(MAX_REQUEST_FRAME_BYTES + 1)
        if not chunk or chunk.endswith(b"\n"):
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)
    if args.preflight:
        try:
            print(_dumps_strict(preflight()).decode("utf-8"), flush=True)
            return 0
        except (AgentProxyError, OSError, TypeError, ValueError):
            print("fleet-agent-mcp: preflight failed closed", file=sys.stderr)
            return 2
    stdin = sys.stdin.buffer
    while True:
        raw = stdin.readline(MAX_REQUEST_FRAME_BYTES + 1)
        if not raw:
            return 0
        if len(raw) > MAX_REQUEST_FRAME_BYTES or not raw.endswith(b"\n"):
            if not raw.endswith(b"\n"):
                _discard_line_tail(stdin)
            value = response(None, error={"code": -32700, "message": "Parse error"})
        else:
            try:
                value = handle(_loads_strict(raw))
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                value = response(None, error={"code": -32700, "message": "Parse error"})
        if value is not None:
            print(
                _dumps_strict(value).decode("utf-8"),
                flush=True,
            )


if __name__ == "__main__":
    raise SystemExit(main())
