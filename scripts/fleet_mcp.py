#!/usr/bin/env python3
"""Dependency-free local MCP stdio facade over the Fleet Control core."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import stat
import struct
import sys
from typing import Any

import fleet_artifacts
from fleet_control import FleetControl, FleetControlError
import fleet_delegation
import fleet_mission_state as mission_state


PROTOCOL_VERSION = "2024-11-05"
MAX_SOCKET_REQUEST_BYTES = 2_000_000
TOOLS = [
    {
        "name": "dispatch",
        "description": "Dispatch one tracked specialist capability without waiting.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["recipient_instance", "capability", "objective", "idempotency_key"],
            "properties": {
                "recipient_instance": {"type": "string"},
                "capability": {"type": "string"},
                "objective": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "parent_run_id": {"type": "string"},
                "token_id": {"type": "string"},
                "input_artifact_ids": {"type": "array", "items": {"type": "string"}},
                "can_delegate": {"type": "boolean"},
                "allowed_capabilities": {"type": "array", "items": {"type": "string"}},
                "remaining_budget": {"type": "integer", "minimum": 0},
            },
        },
    },
    {
        "name": "dispatch_many",
        "description": "Create several independent tracked runs before any wait.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["requests"],
            "properties": {"requests": {"type": "array", "items": {"type": "object"}, "minItems": 1}},
        },
    },
    {
        "name": "wait",
        "description": "Wait for exact run IDs and persist verified result artifacts.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["run_ids"],
            "properties": {
                "run_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "timeout_seconds": {"type": "integer", "minimum": 1},
            },
        },
    },
    {
        "name": "get_result",
        "description": "Read one exact content-addressed artifact.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "required": ["artifact_id"], "properties": {"artifact_id": {"type": "string"}},
        },
    },
    {
        "name": "relay_result",
        "description": "Dispatch a tracked follow-up that references an exact artifact ID.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["artifact_id", "recipient_instance", "capability", "objective", "idempotency_key"],
            "properties": {
                "artifact_id": {"type": "string"},
                "recipient_instance": {"type": "string"},
                "capability": {"type": "string"},
                "objective": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "parent_run_id": {"type": "string"},
                "token_id": {"type": "string"},
            },
        },
    },
    {
        "name": "request_assurance",
        "description": "Monotonically elevate mission risk and pause for assurance.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "required": ["risk", "categories", "reason", "idempotency_key"],
            "properties": {
                "risk": {"enum": ["high", "unknown"]},
                "categories": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
    },
    {
        "name": "request_human",
        "description": "Record a durable scoped human decision request.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "required": ["reason", "scope", "idempotency_key"],
            "properties": {
                "reason": {"type": "string"}, "scope": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
    },
    {"name": "inspect_roster", "description": "Inspect compiled live capabilities.", "inputSchema": {"type": "object", "additionalProperties": False}},
    {"name": "inspect_mission", "description": "Inspect derived durable mission state.", "inputSchema": {"type": "object", "additionalProperties": False}},
    {
        "name": "cancel", "description": "Request cancellation of one exact tracked run.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "required": ["run_id", "reason", "idempotency_key"],
            "properties": {
                "run_id": {"type": "string"}, "reason": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
    },
    {
        "name": "complete", "description": "Record a Lead completion request for an exact artifact.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "required": ["artifact_id", "summary", "idempotency_key"],
            "properties": {
                "artifact_id": {"type": "string"}, "summary": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
    },
]


def call_tool(
    control: FleetControl, name: str, arguments: dict[str, Any], *, actor: str = "lead"
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise FleetControlError("tool arguments must be an object")
    if name == "dispatch":
        return control.dispatch(**arguments)
    if name == "dispatch_many":
        return control.dispatch_many(arguments.get("requests"))
    if name == "wait":
        return control.wait(
            arguments.get("run_ids"), timeout_seconds=int(arguments.get("timeout_seconds", 1800))
        )
    if name == "get_result":
        return control.get_result(arguments.get("artifact_id", ""))
    if name == "relay_result":
        return control.relay_result(**arguments)
    if name == "request_assurance":
        return control.request_assurance(**arguments, actor=actor)
    if name == "request_human":
        return control.request_human(**arguments)
    if name == "inspect_roster":
        if arguments:
            raise FleetControlError("inspect_roster accepts no arguments")
        return control.inspect_roster()
    if name == "inspect_mission":
        if arguments:
            raise FleetControlError("inspect_mission accepts no arguments")
        return control.state()
    if name == "cancel":
        return control.cancel(**arguments)
    if name == "complete":
        return control.complete(**arguments)
    raise FleetControlError(f"unknown tool: {name}")


def response(identifier: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier}
    if error is not None:
        value["error"] = error
    else:
        value["result"] = result
    return value


def handle(
    control: FleetControl, request: Any, *, actor: str = "lead"
) -> dict[str, Any] | None:
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return response(None, error={"code": -32600, "message": "Invalid Request"})
    identifier = request.get("id")
    method = request.get("method")
    if identifier is None and isinstance(method, str) and method.startswith("notifications/"):
        return None
    if method == "initialize":
        return response(
            identifier,
            result={
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fleet-control", "version": "1.0.0"},
            },
        )
    if method == "ping":
        return response(identifier, result={})
    if method == "tools/list":
        return response(identifier, result={"tools": TOOLS})
    if method == "tools/call":
        params = request.get("params")
        if not isinstance(params, dict):
            return response(identifier, error={"code": -32602, "message": "Invalid params"})
        try:
            value = call_tool(
                control, str(params.get("name", "")), params.get("arguments") or {}, actor=actor
            )
            return response(
                identifier,
                result={
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, sort_keys=True)}],
                    "isError": False,
                },
            )
        except (
            FleetControlError,
            fleet_artifacts.ArtifactError,
            fleet_delegation.DelegationError,
            mission_state.MissionStateError,
            TypeError,
            ValueError,
        ) as exc:
            return response(
                identifier,
                result={"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
    return response(identifier, error={"code": -32601, "message": "Method not found"})


def _peer_uid(connection: socket.socket) -> int:
    """Read kernel-authenticated peer identity on BSD/macOS or Linux."""
    if hasattr(connection, "getpeereid"):
        return int(connection.getpeereid()[0])  # type: ignore[attr-defined]
    if hasattr(socket, "SO_PEERCRED"):
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _, uid, _ = struct.unpack("3i", raw)
        return int(uid)
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "getpeereid", None)
    if function is None:
        raise FleetControlError("platform cannot authenticate Unix socket peers")
    uid = ctypes.c_uint()
    gid = ctypes.c_uint()
    if function(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(uid.value)


def _validate_socket_caller(
    control: FleetControl,
    caller: Any,
    request: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(caller, dict) or set(caller) - {"instance", "run_id", "token_id"}:
        raise FleetControlError("invalid control caller envelope")
    instance = str(caller.get("instance") or "")
    run_id = mission_state.normalize_uuid(str(caller.get("run_id") or ""), "caller run_id")
    token_id = caller.get("token_id")
    current = control.state()
    if instance == "lead":
        raise FleetControlError(
            "Lead callers are not authorized on the specialist control socket"
        )
    if not isinstance(token_id, str) or not token_id:
        raise FleetControlError("specialist socket identity requires a capability token")
    token = fleet_delegation.load_token(control.runs_dir, control.mission_id, token_id)
    delegation = current.get("delegations", {}).get(token["delegation_id"])
    if isinstance(delegation, dict):
        if any(
            (
                delegation.get("run_id") != run_id,
                delegation.get("recipient_instance") != instance,
                delegation.get("token_id") != token_id,
            )
        ):
            raise FleetControlError("specialist socket identity does not match durable delegation")
    else:
        # CONTROL binds an interactive run before transferring its prompt so a
        # fast specialist cannot outrun delegation_registered. During that
        # narrow window, the immutable token plus exactly one dispatch intent
        # establishes the same delegation/recipient identity.
        intents = [
            event
            for event in control.events()
            if event["kind"] == "delegation_dispatch_intent"
            and event["payload"].get("delegation_id") == token["delegation_id"]
            and event["payload"].get("recipient_instance") == instance
        ]
        if len(intents) != 1:
            raise FleetControlError("specialist socket identity does not match durable delegation")
    bindings = [
        event
        for event in control.events()
        if event["kind"] == "capability_token_bound"
        and event["payload"].get("token_id") == token_id
        and event["payload"].get("run_id") == run_id
    ]
    if len(bindings) != 1:
        raise FleetControlError("specialist token is not uniquely bound to caller run")

    method = request.get("method")
    if method in {"initialize", "ping", "tools/list"}:
        return {"kind": "specialist", "instance": instance, "run_id": run_id, "token": token}
    if method != "tools/call":
        raise FleetControlError("specialist requested an unsupported control method")
    params = request.get("params")
    if not isinstance(params, dict):
        raise FleetControlError("specialist tool call has invalid params")
    name = str(params.get("name") or "")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise FleetControlError("specialist tool call arguments must be an object")
    read_scope = {
        "inspect_roster", "inspect_mission", "get_result", "wait",
        "request_assurance", "request_human",
    }
    delegated_scope = {"dispatch", "dispatch_many", "relay_result"}
    if name in read_scope:
        return {"kind": "specialist", "instance": instance, "run_id": run_id, "token": token}
    if name not in delegated_scope:
        raise FleetControlError(f"specialist is not authorized for control tool: {name}")
    requests = arguments.get("requests") if name == "dispatch_many" else [arguments]
    if not isinstance(requests, list) or not requests or any(
        not isinstance(value, dict) for value in requests
    ):
        raise FleetControlError("delegated control request list is invalid")
    for value in requests:
        if value.get("parent_run_id") != run_id or value.get("token_id") != token_id:
            raise FleetControlError("delegated control request is not bound to caller identity")
        capability = value.get("capability")
        if capability not in token["allowed_capabilities"]:
            raise FleetControlError("delegated control request exceeds token capability scope")
    return {"kind": "specialist", "instance": instance, "run_id": run_id, "token": token}


def handle_socket_envelope(control: FleetControl, envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise FleetControlError("control socket envelope must be an object")
    if envelope.get("operation") == "health" and set(envelope) == {"operation"}:
        return {
            "status": "ok",
            "mission_id": control.mission_id,
            "protocol": "fleet-control-unix-v1",
        }
    if set(envelope) != {"schema_version", "caller", "request"} or envelope.get(
        "schema_version"
    ) != 1:
        raise FleetControlError("invalid control socket envelope schema")
    request = envelope["request"]
    if not isinstance(request, dict):
        raise FleetControlError("control socket request must be an object")
    identity = _validate_socket_caller(control, envelope["caller"], request)
    result = handle(
        control, request,
        actor=f"specialist:{identity['instance']}:{identity['run_id']}",
    )
    return {"identity": {key: identity[key] for key in ("kind", "instance", "run_id")}, "response": result}


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class _ControlSocketHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _ThreadingUnixServer)
        try:
            if _peer_uid(self.request) != os.geteuid():
                raise FleetControlError("control socket peer uid mismatch")
            raw = self.rfile.readline(MAX_SOCKET_REQUEST_BYTES + 1)
            if not raw or len(raw) > MAX_SOCKET_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise FleetControlError("invalid control socket frame")
            value = handle_socket_envelope(server.control, json.loads(raw))  # type: ignore[attr-defined]
            reply = {"ok": True, "result": value}
        except (
            FleetControlError,
            fleet_artifacts.ArtifactError,
            fleet_delegation.DelegationError,
            mission_state.MissionStateError,
            json.JSONDecodeError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            reply = {"ok": False, "error": str(exc)}
        self.wfile.write(json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")


def serve_socket(control: FleetControl, path: Path) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid():
        raise FleetControlError("control socket directory owner/type mismatch")
    if stat.S_IMODE(parent.st_mode) != 0o700:
        raise FleetControlError("control socket directory must be mode 0700")
    if path.exists() or path.is_symlink():
        raise FleetControlError("refusing pre-existing control socket path")
    previous_umask = os.umask(0o077)
    try:
        server = _ThreadingUnixServer(str(path), _ControlSocketHandler)
    finally:
        os.umask(previous_umask)
    server.control = control  # type: ignore[attr-defined]
    os.chmod(path, 0o600)
    stopping = False

    def stop(signum: int, frame: object) -> None:
        nonlocal stopping
        del signum, frame
        if not stopping:
            stopping = True
            # shutdown must run outside the signal handler's serving thread.
            import threading

            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--socket")
    args = parser.parse_args(argv)
    try:
        control = FleetControl(Path(args.runs_dir), args.mission_id)
        if args.socket:
            serve_socket(control, Path(args.socket))
            return 0
        for raw in sys.stdin:
            try:
                request = json.loads(raw)
                value = handle(control, request)
            except json.JSONDecodeError:
                value = response(None, error={"code": -32700, "message": "Parse error"})
            if value is not None:
                print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)
        return 0
    except (
        OSError,
        json.JSONDecodeError,
        FleetControlError,
        fleet_artifacts.ArtifactError,
        fleet_delegation.DelegationError,
        mission_state.MissionStateError,
    ) as exc:
        print(f"fleet-mcp: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
