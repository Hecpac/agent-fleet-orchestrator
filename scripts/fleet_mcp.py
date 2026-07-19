#!/usr/bin/env python3
"""Dependency-free local MCP stdio facade over the Fleet Control core."""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timedelta, timezone
import errno
import json
import locale
import os
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import struct
import sys
import threading
import time
from typing import Any
import uuid

import fleet_artifacts
import fleet_control as fleet_control_module
from fleet_control import FleetControl, FleetControlError
import fleet_control_runtime as control_runtime
import fleet_delegation
import fleet_json
import fleet_ledger
import fleet_mission_state as mission_state
import fleet_safe_paths


PROTOCOL_VERSION = "2024-11-05"
MAX_SOCKET_REQUEST_BYTES = 2_000_000
MAX_WAIT_SECONDS = 1800
MAX_ACTIVE_SOCKET_HANDLERS = 16
MAX_ACTIVE_MANAGEMENT_HANDLERS = 4
MAX_OPERATION_OUTPUT_BYTES = 2_000_000
SPECIALIST_DENIAL_MESSAGE = "specialist control request failed closed"

DISPATCH_PROPERTIES: dict[str, Any] = {
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
DECISION_OPTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["option_id", "label", "tradeoffs"],
    "properties": {
        "option_id": {"type": "string"},
        "label": {"type": "string"},
        "tradeoffs": {"type": "string"},
    },
}
DECISION_BRIEF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title",
        "question",
        "affected_instances",
        "impact",
        "risk",
        "reversible",
        "options",
        "recommendation",
        "challenge",
        "dissent",
        "default_option_id",
    ],
    "properties": {
        "title": {"type": "string"},
        "question": {"type": "string"},
        "affected_instances": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        },
        "impact": {"enum": ["blocking", "checkpoint"]},
        "risk": {"enum": ["low", "medium", "high", "unknown"]},
        "reversible": {"type": "boolean"},
        "options": {
            "type": "array",
            "items": DECISION_OPTION_SCHEMA,
            "minItems": 2,
        },
        "recommendation": {
            "type": "object",
            "additionalProperties": False,
            "required": ["option_id", "rationale", "artifact_id"],
            "properties": {
                "option_id": {"type": "string"},
                "rationale": {"type": "string"},
                "artifact_id": {"type": "string"},
            },
        },
        "challenge": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "artifact_id"],
            "properties": {
                "summary": {"type": "string"},
                "artifact_id": {"type": "string"},
            },
        },
        "dissent": {"type": "string"},
        # Null is meaningful here (no automatic choice); Fleet Control applies
        # the strict nullable/option-membership contract after schema parsing.
        "default_option_id": {},
    },
}
TOOLS = [
    {
        "name": "dispatch",
        "description": "Dispatch one tracked specialist capability without waiting.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": DISPATCH_REQUIRED,
            "properties": DISPATCH_PROPERTIES,
        },
    },
    {
        "name": "dispatch_many",
        "description": "Create several independent tracked runs before any wait.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["requests"],
            "properties": {
                "requests": {
                    "type": "array",
                    "items": DISPATCH_ITEM_SCHEMA,
                    "minItems": 1,
                }
            },
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
                "run_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_WAIT_SECONDS,
                },
            },
        },
    },
    {
        "name": "get_result",
        "description": "Read one exact content-addressed artifact.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["artifact_id"],
            "properties": {"artifact_id": {"type": "string"}},
        },
    },
    {
        "name": "relay_result",
        "description": "Dispatch a tracked follow-up that references an exact artifact ID.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "artifact_id",
                "recipient_instance",
                "capability",
                "objective",
                "idempotency_key",
            ],
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
            "type": "object",
            "additionalProperties": False,
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
            "type": "object",
            "additionalProperties": False,
            "required": ["reason", "scope", "idempotency_key"],
            "properties": {
                "reason": {"type": "string"},
                "scope": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
    },
    {
        "name": "request_decision",
        "description": (
            "Publish a Lead-only evidence-bound Decision Brief for human resolution."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["brief", "idempotency_key"],
            "properties": {
                "brief": DECISION_BRIEF_SCHEMA,
                "idempotency_key": {"type": "string"},
            },
        },
    },
    {
        "name": "inspect_roster",
        "description": "Inspect compiled live capabilities.",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
    {
        "name": "inspect_mission",
        "description": "Inspect derived durable mission state.",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
    {
        "name": "cancel",
        "description": "Request cancellation of one exact tracked run.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["run_id", "reason", "idempotency_key"],
            "properties": {
                "run_id": {"type": "string"},
                "reason": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
    },
    {
        "name": "complete",
        "description": "Record a Lead completion request for an exact artifact.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["artifact_id", "summary", "idempotency_key"],
            "properties": {
                "artifact_id": {"type": "string"},
                "summary": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
    },
]
TOOL_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}


def _loads_strict(raw: bytes | str) -> Any:
    return fleet_json.loads(raw)


def _dumps_strict(value: Any, *, sort_keys: bool = False) -> str:
    del sort_keys
    return fleet_json.canonical_bytes(value).decode("utf-8")


def _validate_schema(value: Any, schema: dict[str, Any], where: str) -> None:
    """Validate the closed JSON-schema subset used by Fleet Control tools."""

    if "enum" in schema and value not in schema["enum"]:
        raise FleetControlError(f"{where} is outside the allowed values")
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise FleetControlError(f"{where} must be an object")
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        missing = required - set(value)
        if missing:
            raise FleetControlError(f"{where} is missing required fields")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise FleetControlError(f"{where} contains unknown fields")
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, dict):
                _validate_schema(item, child, f"{where}.{key}")
        return
    if expected == "array":
        if not isinstance(value, list):
            raise FleetControlError(f"{where} must be an array")
        minimum = schema.get("minItems")
        if type(minimum) is int and len(value) < minimum:
            raise FleetControlError(f"{where} has too few items")
        child = schema.get("items")
        if isinstance(child, dict):
            for index, item in enumerate(value):
                _validate_schema(item, child, f"{where}[{index}]")
        return
    if expected == "string" and not isinstance(value, str):
        raise FleetControlError(f"{where} must be a string")
    if expected == "boolean" and type(value) is not bool:
        raise FleetControlError(f"{where} must be a boolean")
    if expected == "integer":
        if type(value) is not int:
            raise FleetControlError(f"{where} must be an integer")
        minimum = schema.get("minimum")
        if type(minimum) is int and value < minimum:
            raise FleetControlError(f"{where} is below the minimum")
        maximum = schema.get("maximum")
        if type(maximum) is int and value > maximum:
            raise FleetControlError(f"{where} exceeds the maximum")


def _validate_tool_arguments(name: str, arguments: Any) -> dict[str, Any]:
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        raise FleetControlError("unknown control tool")
    _validate_schema(arguments, schema, f"{name} arguments")
    assert isinstance(arguments, dict)
    return arguments


def call_tool(
    control: FleetControl,
    name: str,
    arguments: dict[str, Any],
    *,
    actor: str = "lead",
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    arguments = _validate_tool_arguments(name, arguments)
    caller_identity = (
        identity
        if identity is not None and identity.get("kind") == "specialist"
        else None
    )
    guarded = (
        {"_caller_identity": caller_identity} if caller_identity is not None else {}
    )
    if name == "dispatch":
        return control.dispatch(**arguments, **guarded)
    if name == "dispatch_many":
        return control.dispatch_many(arguments["requests"], **guarded)
    if name == "wait":
        return control.wait(
            arguments["run_ids"],
            timeout_seconds=arguments.get("timeout_seconds", 1800),
            **guarded,
        )
    if name == "get_result":
        return control.get_result(arguments["artifact_id"])
    if name == "relay_result":
        return control.relay_result(**arguments, **guarded)
    if name == "request_assurance":
        return control.request_assurance(
            **arguments,
            actor=actor,
            **guarded,
        )
    if name == "request_human":
        return control.request_human(**arguments, **guarded)
    if name == "request_decision":
        if caller_identity is not None:
            raise FleetControlError("request_decision is restricted to the mission Lead")
        return control.request_decision(**arguments)
    if name == "inspect_roster":
        if arguments:
            raise FleetControlError("inspect_roster accepts no arguments")
        return control.inspect_roster()
    if name == "inspect_mission":
        if arguments:
            raise FleetControlError("inspect_mission accepts no arguments")
        if identity is not None and identity.get("kind") == "specialist":
            return _scoped_mission_state(control.state(), identity)
        return control.state()
    if name == "cancel":
        return control.cancel(**arguments)
    if name == "complete":
        return control.complete(**arguments)
    raise FleetControlError(f"unknown tool: {name}")


def response(
    identifier: Any, *, result: Any = None, error: dict[str, Any] | None = None
) -> dict[str, Any]:
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier}
    if error is not None:
        value["error"] = error
    else:
        value["result"] = result
    return value


def handle(
    control: FleetControl,
    request: Any,
    *,
    actor: str = "lead",
    identity: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
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
        if not isinstance(params, dict) or set(params) != {"name", "arguments"}:
            return response(
                identifier, error={"code": -32602, "message": "Invalid params"}
            )
        name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(name, str):
            return response(
                identifier, error={"code": -32602, "message": "Invalid params"}
            )
        try:
            value = call_tool(
                control,
                name,
                arguments,
                actor=actor,
                identity=identity,
            )
            if identity is not None and identity.get("kind") == "specialist":
                value = _specialist_tool_result(name, value)
            return response(
                identifier,
                result={
                    "content": [
                        {"type": "text", "text": _dumps_strict(value, sort_keys=True)}
                    ],
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
            message = (
                SPECIALIST_DENIAL_MESSAGE
                if identity is not None and identity.get("kind") == "specialist"
                else str(exc)
            )
            return response(
                identifier,
                result={
                    "content": [{"type": "text", "text": message}],
                    "isError": True,
                },
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


def _direct_children(current: dict[str, Any], run_id: str) -> dict[str, dict[str, Any]]:
    return {
        delegation_id: delegation
        for delegation_id, delegation in current.get("delegations", {}).items()
        if isinstance(delegation, dict)
        and delegation.get("parent_run_id") == run_id
        and delegation.get("delegated_by") == run_id
    }


def _public_delegation(delegation: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "delegation_id",
        "run_id",
        "parent_run_id",
        "delegated_by",
        "recipient_instance",
        "capability",
        "input_artifact_ids",
        "depth",
        "deadline",
    )
    return {field: delegation.get(field) for field in fields}


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    fields = ("run_id", "delegation_id", "artifact_id", "provider", "model", "variant")
    return {field: result.get(field) for field in fields}


def _specialist_tool_result(name: str, value: dict[str, Any]) -> dict[str, Any]:
    """Project tool output without leaking child tokens or filesystem paths."""
    dispatch_fields = (
        "mission_id",
        "delegation_id",
        "run_id",
        "recipient_instance",
        "reused",
    )
    if name in {"dispatch", "relay_result"}:
        return {field: value.get(field) for field in dispatch_fields}
    if name == "dispatch_many":
        return {
            "mission_id": value.get("mission_id"),
            "runs": [
                {field: run.get(field) for field in dispatch_fields}
                for run in value.get("runs", [])
                if isinstance(run, dict)
            ],
            "all_dispatched_before_wait": value.get("all_dispatched_before_wait"),
        }
    if name == "wait":
        result_fields = (
            "run_id",
            "delegation_id",
            "instance",
            "status",
            "artifact_id",
            "bytes",
        )
        return {
            "mission_id": value.get("mission_id"),
            "status": value.get("status"),
            "results": [
                {field: result.get(field) for field in result_fields if field in result}
                for result in value.get("results", [])
                if isinstance(result, dict)
            ],
            "wait_exit_code": value.get("wait_exit_code"),
        }
    if name == "request_assurance":
        return {
            "mission_id": value.get("mission_id"),
            "status": value.get("status"),
            "risk": value.get("risk"),
            "risk_categories": value.get("risk_categories"),
        }
    if name == "request_human":
        event = value.get("event") if isinstance(value.get("event"), dict) else {}
        return {
            "event_sha256": event.get("event_sha256"),
            "appended": value.get("appended"),
        }
    return value


def _scoped_mission_state(
    current: dict[str, Any], identity: dict[str, Any]
) -> dict[str, Any]:
    run_id = identity["run_id"]
    token = identity["token"]
    own = current.get("delegations", {}).get(token["delegation_id"])
    if not isinstance(own, dict) or own.get("run_id") != run_id:
        raise FleetControlError(
            "specialist delegation is not registered for inspection"
        )
    children = _direct_children(current, run_id)
    scoped_delegations = {token["delegation_id"]: _public_delegation(own)}
    scoped_delegations.update(
        {
            delegation_id: _public_delegation(value)
            for delegation_id, value in children.items()
        }
    )
    results = {
        delegation_id: _public_result(current["results"][delegation_id])
        for delegation_id in children
        if isinstance(current.get("results", {}).get(delegation_id), dict)
    }
    return {
        "mission_id": current["mission_id"],
        "feature": current["feature"],
        "status": current["status"],
        "risk": current["risk"],
        "risk_categories": current["risk_categories"],
        "caller": {
            "instance": identity["instance"],
            "run_id": run_id,
            "delegation_id": token["delegation_id"],
        },
        "delegations": scoped_delegations,
        "results": results,
    }


def _require_artifact_subset(value: Any, accessible: set[str], *, field: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise FleetControlError(f"{field} must be an artifact ID list")
    if len(value) != len(set(value)):
        raise FleetControlError(f"{field} contains duplicate artifact IDs")
    if not set(value) <= accessible:
        raise FleetControlError("specialist artifact access exceeds caller grant")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _delegation_deadline(
    control: FleetControl, delegation: dict[str, Any] | None
) -> datetime:
    raw = delegation.get("deadline") if isinstance(delegation, dict) else None
    if raw is None:
        created = datetime.fromisoformat(
            str(control.events()[0]["timestamp"]).replace("Z", "+00:00")
        )
        raw_deadline = created + timedelta(
            seconds=int(control.compiled["workflow"]["limits"]["deadline_seconds"])
        )
    else:
        raw_deadline = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if raw_deadline.tzinfo is None:
        raise FleetControlError("specialist delegation deadline lacks timezone")
    return raw_deadline.astimezone(timezone.utc)


def _require_active_frontier_lease(
    control: FleetControl,
    *,
    instance: str,
    run_id: str,
    task_sha256: str,
) -> None:
    """Require one descriptor-verified lease for the exact interactive run."""

    current = control.state()
    feature = current.get("feature")
    member = control.members().get(instance)
    if not isinstance(feature, str) or not isinstance(member, dict):
        raise FleetControlError(
            "specialist run identity lacks exact active frontier lease"
        )
    manifest = control.manifest()
    lease_name = f"{feature}.{instance}.lock"
    expected = {
        "schema_version": 1,
        "run_id": run_id,
        "feature": feature,
        "instance": instance,
        "role": member.get("role_type"),
        "phase": member.get("phase"),
        "resource_class": "remote",
        "runner": "interactive",
        "task_sha256": task_sha256,
        "workspace_uuid": str(manifest.get("workspace_uuid") or "").upper(),
        "surface_uuid": str(manifest.get(f"{instance}.uuid") or "").upper(),
        "pid": None,
        "pgid": None,
        "kind": lease_name,
    }
    try:
        # Existing repository-owned runs roots are commonly 0755. RootedFS
        # still requires the current owner and rejects group/world-writable
        # roots; the security boundary below it remains exact 0700/0600.
        with fleet_safe_paths.RootedFS(control.runs_dir) as rooted:
            raw = rooted.read_regular(
                Path("locks") / lease_name / "lease.json",
                directory_modes=(0o700, 0o700),
                file_mode=0o600,
                max_bytes=64 * 1024,
            )
        metadata = _loads_strict(raw)
        exact_keys = set(expected) | {"acquired_at"}
        if not isinstance(metadata, dict) or set(metadata) != exact_keys:
            raise ValueError("frontier lease schema mismatch")
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise ValueError("frontier lease binding mismatch")
        acquired_at = datetime.fromisoformat(
            str(metadata.get("acquired_at") or "").replace("Z", "+00:00")
        )
        if acquired_at.tzinfo is None:
            raise ValueError("frontier lease timestamp lacks timezone")
    except (
        fleet_safe_paths.SafePathError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        raise FleetControlError(
            "specialist run identity lacks exact active frontier lease"
        ) from exc


def _validate_socket_caller(
    control: FleetControl,
    endpoint_instance: str,
    caller: Any,
    request: dict[str, Any],
) -> dict[str, Any]:
    if endpoint_instance == "lead" or endpoint_instance not in control.members():
        raise FleetControlError("invalid specialist control endpoint binding")
    if not isinstance(caller, dict) or set(caller) not in (
        {"run_id", "token_id"},
        {"instance", "run_id", "token_id"},
    ):
        raise FleetControlError("invalid control caller envelope")
    if "instance" in caller and caller["instance"] != endpoint_instance:
        raise FleetControlError("caller instance does not match control endpoint")
    instance = endpoint_instance
    run_id = mission_state.normalize_uuid(
        str(caller.get("run_id") or ""), "caller run_id"
    )
    token_id = caller.get("token_id")
    if not isinstance(token_id, str) or not token_id:
        raise FleetControlError(
            "specialist socket identity requires a capability token"
        )
    token = fleet_delegation.load_token(control.runs_dir, control.mission_id, token_id)
    current = control.state()
    if current.get("status") in mission_state.TERMINAL_STATUSES:
        raise FleetControlError("specialist capability token mission is terminal")
    delegation = current.get("delegations", {}).get(token["delegation_id"])
    if not isinstance(delegation, dict) or any(
        (
            delegation.get("run_id") != run_id,
            delegation.get("recipient_instance") != instance,
            delegation.get("token_id") != token_id,
        )
    ):
        raise FleetControlError(
            "specialist socket identity does not match durable delegation"
        )
    intents = [
        event
        for event in control.events()
        if event["kind"] == "delegation_dispatch_intent"
        and event["payload"].get("delegation_id") == token["delegation_id"]
        and event["payload"].get("recipient_instance") == instance
    ]
    if len(intents) != 1:
        raise FleetControlError(
            "specialist socket identity does not match durable delegation"
        )
    prompt_sha256 = intents[0]["payload"].get("prompt_sha256")
    if not isinstance(prompt_sha256, str) or len(prompt_sha256) != 64:
        raise FleetControlError(
            "specialist socket identity does not match durable delegation"
        )
    # A published/bound token and even a durable commit are only preparation.
    # Socket authority begins after the wrapper accepted the exact run and
    # CONTROL durably marked that admission started.
    token = fleet_delegation.validate_admission_token(
        control.runs_dir,
        control.mission_id,
        token_id=token_id,
        delegation_id=token["delegation_id"],
        run_id=run_id,
        require_started=True,
    )
    admission = current.get("admissions", {}).get(token["admission_id"])
    if (
        not isinstance(admission, dict)
        or admission.get("recipient_instance") != instance
    ):
        raise FleetControlError("specialist admission does not own this endpoint")
    if any(
        event["kind"] == "run_cancel_requested"
        and event["payload"].get("run_id") == run_id
        for event in control.events()
    ):
        raise FleetControlError("specialist capability token run was cancelled")
    if _now_utc() >= _delegation_deadline(
        control, delegation if isinstance(delegation, dict) else None
    ):
        raise FleetControlError("specialist capability token deadline expired")
    run_events = [
        event
        for event in control._legacy_events()  # noqa: SLF001 - same trusted core boundary
        if event.get("run_id") == run_id
    ]
    if run_events and any(
        event.get("instance") != instance
        or event.get("feature") != current.get("feature")
        or event.get("task_sha256") != prompt_sha256
        for event in run_events
    ):
        raise FleetControlError(
            "specialist run identity lacks exact lifecycle evidence"
        )
    if len([event for event in run_events if event.get("status") == "dispatched"]) != 1:
        raise FleetControlError(
            "specialist run identity lacks exact lifecycle evidence"
        )
    if any(
        event.get("status") in fleet_ledger.TERMINAL_STATUSES for event in run_events
    ):
        raise FleetControlError("specialist capability token is terminal")
    _require_active_frontier_lease(
        control,
        instance=instance,
        run_id=run_id,
        task_sha256=prompt_sha256,
    )

    identity = {
        "kind": "specialist",
        "instance": instance,
        "run_id": run_id,
        "token": token,
    }
    method = request.get("method")
    if method in {"initialize", "ping", "tools/list"}:
        return identity
    if method != "tools/call":
        raise FleetControlError("specialist requested an unsupported control method")
    params = request.get("params")
    if not isinstance(params, dict):
        raise FleetControlError("specialist tool call has invalid params")
    name = str(params.get("name") or "")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise FleetControlError("specialist tool call arguments must be an object")
    if name in {
        "inspect_roster",
        "inspect_mission",
        "request_assurance",
        "request_human",
    }:
        return identity
    children = _direct_children(current, run_id)
    accessible = set(
        fleet_delegation.authorized_artifact_ids(
            control.runs_dir,
            control.mission_id,
            token_id=token_id,
            delegation_id=token["delegation_id"],
            run_id=run_id,
        )
    )
    if name == "wait":
        requested = arguments.get("run_ids")
        if (
            not isinstance(requested, list)
            or not requested
            or any(not isinstance(value, str) for value in requested)
            or len(requested) != len(set(requested))
        ):
            raise FleetControlError(
                "specialist wait requires unique direct child run IDs"
            )
        child_run_ids = {value.get("run_id") for value in children.values()}
        if not set(requested) <= child_run_ids:
            raise FleetControlError("specialist wait is limited to direct child runs")
        return identity
    if name == "get_result":
        artifact_id = arguments.get("artifact_id")
        if not isinstance(artifact_id, str) or artifact_id not in accessible:
            raise FleetControlError("specialist artifact access exceeds caller grant")
        return identity
    if name not in {"dispatch", "dispatch_many", "relay_result"}:
        raise FleetControlError(
            f"specialist is not authorized for control tool: {name}"
        )
    if not token.get("can_delegate"):
        raise FleetControlError(
            "specialist capability token does not allow subdelegation"
        )
    requests = arguments.get("requests") if name == "dispatch_many" else [arguments]
    if (
        not isinstance(requests, list)
        or not requests
        or any(not isinstance(value, dict) for value in requests)
    ):
        raise FleetControlError("delegated control request list is invalid")
    for value in requests:
        if value.get("parent_run_id") != run_id or value.get("token_id") != token_id:
            raise FleetControlError(
                "delegated control request is not bound to caller identity"
            )
        capability = value.get("capability")
        if capability not in token["allowed_capabilities"]:
            raise FleetControlError(
                "delegated control request exceeds token capability scope"
            )
        if name == "relay_result":
            artifact_id = value.get("artifact_id")
            if not isinstance(artifact_id, str) or artifact_id not in accessible:
                raise FleetControlError(
                    "specialist artifact access exceeds caller grant"
                )
        else:
            _require_artifact_subset(
                value.get("input_artifact_ids") or [],
                accessible,
                field="input_artifact_ids",
            )
    return identity


def handle_socket_envelope(
    control: FleetControl, envelope: Any, *, endpoint_instance: str
) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise FleetControlError("control socket envelope must be an object")
    if (
        set(envelope) != {"schema_version", "caller", "request"}
        or envelope.get("schema_version") != 2
    ):
        raise FleetControlError("invalid specialist control socket envelope schema")
    request = envelope["request"]
    if not isinstance(request, dict):
        raise FleetControlError("control socket request must be an object")
    identity = _validate_socket_caller(
        control, endpoint_instance, envelope["caller"], request
    )
    token = identity["token"]
    # Revalidate the revocable owner under the same Mission lock used by every
    # admission mutation. Dispatch then performs its own parent check inside
    # the reservation transaction, so finalize/cancel linearizes before it or
    # after a child reservation has made parent finalization impossible.
    with mission_state.MissionTransaction(
        control.runs_dir, control.mission_id
    ) as transaction:
        current = transaction.current_state
        admission = (
            None
            if current is None
            else current["admissions"].get(token["admission_id"])
        )
        delegation = (
            None
            if current is None
            else current["delegations"].get(token["delegation_id"])
        )
        if (
            admission is None
            or admission["phase"] != "started"
            or not admission["active"]
            or admission["run_id"] != identity["run_id"]
            or identity["run_id"] in current.get("cancelled_runs", {})
            or not isinstance(delegation, dict)
            or delegation.get("run_id") != identity["run_id"]
        ):
            raise FleetControlError(
                "specialist admission was revoked before control execution"
            )
    result = handle(
        control,
        request,
        actor=f"specialist:{identity['instance']}:{identity['run_id']}",
        identity=identity,
    )
    return {
        "identity": {key: identity[key] for key in ("kind", "instance", "run_id")},
        "response": result,
    }


class _AdmissionGate:
    """One atomic stopping gate shared by every endpoint and handler."""

    def __init__(self, maximum: int = MAX_ACTIVE_SOCKET_HANDLERS) -> None:
        self.maximum = maximum
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._active_specialists = 0
        self._active_management = 0

    def try_admit(self, *, management: bool) -> bool:
        with self._lock:
            if self.stopping.is_set():
                return False
            if management:
                if self._active_management >= MAX_ACTIVE_MANAGEMENT_HANDLERS:
                    return False
                self._active_management += 1
            else:
                if self._active_specialists >= self.maximum:
                    return False
                self._active_specialists += 1
            return True

    def release(self, *, management: bool) -> None:
        with self._lock:
            if management:
                if self._active_management <= 0:
                    raise FleetControlError("control management admission underflow")
                self._active_management -= 1
            else:
                if self._active_specialists <= 0:
                    raise FleetControlError("control handler admission underflow")
                self._active_specialists -= 1

    def begin_shutdown(self) -> None:
        # The lock makes closing admission atomic with every new admission.
        with self._lock:
            self.stopping.set()

    def active_count(self) -> int:
        with self._lock:
            return self._active_specialists + self._active_management


class _BoundedOperationOutput:
    """Drain process pipes without allowing attacker-sized memory growth."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdout is None or process.stderr is None:
            raise FleetControlError("Fleet Control operation output pipes are absent")
        self._lock = threading.Lock()
        self._buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self._errors: list[BaseException] = []
        self.exceeded = threading.Event()
        self.failed = threading.Event()
        self._streams = {
            "stdout": process.stdout,
            "stderr": process.stderr,
        }
        self._threads = tuple(
            threading.Thread(
                target=self._drain,
                args=(name, stream),
                name=f"fleet-control-{name}",
            )
            for name, stream in self._streams.items()
        )
        self._started_threads: list[threading.Thread] = []

    def start(self) -> None:
        for thread in self._threads:
            thread.start()
            self._started_threads.append(thread)

    def _drain(self, name: str, stream: Any) -> None:
        try:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                with self._lock:
                    used = sum(len(value) for value in self._buffers.values())
                    available = max(0, MAX_OPERATION_OUTPUT_BYTES - used)
                    self._buffers[name].extend(chunk[:available])
                    if len(chunk) > available:
                        self.exceeded.set()
        except BaseException as exc:  # pragma: no cover - OS pipe failure
            with self._lock:
                self._errors.append(exc)
            self.failed.set()
        finally:
            stream.close()

    def finish(self) -> tuple[str, str]:
        for thread in self._started_threads:
            thread.join(timeout=2)
        started = set(self._started_threads)
        for thread, stream in zip(self._threads, self._streams.values(), strict=True):
            if thread not in started and not stream.closed:
                stream.close()
        if any(thread.is_alive() for thread in started):
            raise FleetControlError("Fleet Control operation output did not quiesce")
        for stream in self._streams.values():
            if not stream.closed:
                stream.close()
        with self._lock:
            if self._errors:
                raise FleetControlError("cannot capture Fleet Control operation output")
            stdout = bytes(self._buffers["stdout"])
            stderr = bytes(self._buffers["stderr"])
        encoding = locale.getpreferredencoding(False)
        try:
            return stdout.decode(encoding), stderr.decode(encoding)
        except UnicodeDecodeError as exc:
            raise FleetControlError(
                "Fleet Control operation output is not valid text"
            ) from exc


class _OperationWorkerStatus:
    """Read one bounded direct-command result from the anchored worker."""

    def __init__(self, fd: int) -> None:
        self.ready = threading.Event()
        self._value: bytes | None = None
        self._error: BaseException | None = None
        self._started = False
        self._thread = threading.Thread(
            target=self._read,
            args=(fd,),
            name="fleet-control-operation-status",
        )

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def _read(self, fd: int) -> None:
        try:
            with os.fdopen(fd, "rb", buffering=0) as stream:
                value = stream.read(65)
            if not value or len(value) > 64:
                raise FleetControlError("invalid Fleet Control operation result")
            self._value = value
        except BaseException as exc:
            self._error = exc
        finally:
            self.ready.set()

    def returncode(self) -> int:
        if not self.ready.is_set():
            raise FleetControlError("Fleet Control operation result is not ready")
        if self._error is not None:
            raise FleetControlError(
                "invalid Fleet Control operation result"
            ) from self._error
        assert self._value is not None
        try:
            text = self._value.decode("ascii")
            if not text.endswith("\n") or text.count("\n") != 1:
                raise ValueError("result is not one line")
            value = int(text[:-1])
        except (UnicodeDecodeError, ValueError) as exc:
            raise FleetControlError("invalid Fleet Control operation result") from exc
        if value < 0 or value > 255:
            raise FleetControlError("invalid Fleet Control operation return code")
        return value

    def finish(self) -> None:
        if not self._started:
            return
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise FleetControlError("Fleet Control operation result did not quiesce")


class _OperationSupervisor:
    """Own, journal, cancel, and reap every external CONTROL operation."""

    _directory_modes = (0o700, 0o700, 0o700, 0o700, 0o700)

    def __init__(
        self,
        control: FleetControl,
        *,
        launch_id: str,
        stopping: threading.Event,
    ) -> None:
        self.control = control
        self.launch_id = str(uuid.UUID(launch_id))
        self.stopping = stopping
        self._lock = threading.Lock()
        self._publication_lock = threading.Lock()
        self._active: dict[str, tuple[subprocess.Popen[bytes], dict[str, Any]]] = {}
        self._cancelled: set[str] = set()
        self._base_relative = (
            Path("missions")
            / control.mission_id
            / "control"
            / "operations"
            / self.launch_id
        )

    def _relative(self, operation_id: str) -> Path:
        return self._base_relative / f"{operation_id}.json"

    def _checkpoint_relative(self) -> Path:
        return self._base_relative / "operation.pending.json"

    def _publish_running(self, journal: dict[str, Any]) -> None:
        content = mission_state.canonical_bytes(journal) + b"\n"
        try:
            with self._publication_lock:
                with fleet_safe_paths.RootedFS(self.control.runs_dir) as rooted:
                    checkpoint = self._checkpoint_relative()
                    rooted.atomic_write(
                        checkpoint,
                        content,
                        directory_modes=self._directory_modes,
                        file_mode=0o600,
                    )
                    rooted.atomic_write(
                        self._relative(journal["operation_id"]),
                        content,
                        directory_modes=self._directory_modes,
                        file_mode=0o600,
                    )
                    rooted.unlink_regular(
                        checkpoint,
                        directory_modes=self._directory_modes,
                        file_mode=0o600,
                    )
        except fleet_safe_paths.SafePathError as exc:
            raise FleetControlError(
                "cannot publish exact Fleet Control operation journal"
            ) from exc

    def _publish_terminal(
        self,
        journal: dict[str, Any],
        *,
        state: str,
        returncode: int,
    ) -> None:
        terminal = {
            **journal,
            "state": state,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "returncode": returncode,
        }
        try:
            with self._publication_lock:
                with fleet_safe_paths.RootedFS(self.control.runs_dir) as rooted:
                    rooted.atomic_write(
                        self._base_relative
                        / f"{journal['operation_id']}.terminal.json",
                        mission_state.canonical_bytes(terminal) + b"\n",
                        directory_modes=self._directory_modes,
                        file_mode=0o600,
                    )
        except fleet_safe_paths.SafePathError as exc:
            raise FleetControlError(
                "cannot terminalize exact Fleet Control operation journal"
            ) from exc

    @staticmethod
    def _is_exact_running(
        process: subprocess.Popen[bytes], journal: dict[str, Any]
    ) -> bool:
        if process.poll() is not None:
            return False
        try:
            observed, zombie = control_runtime.process_observation(process.pid)
        except control_runtime.RuntimeIdentityError as exc:
            raise FleetControlError(
                "cannot inspect exact Fleet Control operation"
            ) from exc
        return observed == journal["process_identity"] and not zombie

    def _verified_group(
        self, process: subprocess.Popen[bytes], journal: dict[str, Any]
    ) -> int | None:
        if not self._is_exact_running(process, journal):
            return None
        try:
            group = os.getpgid(process.pid)
        except ProcessLookupError:
            return None
        except OSError as exc:
            raise FleetControlError(
                "cannot inspect exact Fleet Control operation group"
            ) from exc
        if group != process.pid or group != journal["process_group_id"]:
            raise FleetControlError(
                "Fleet Control operation process group identity mismatch"
            )
        # Revalidate both bindings immediately before returning a signal target.
        if not self._is_exact_running(process, journal):
            return None
        try:
            if os.getpgid(process.pid) != group:
                raise FleetControlError(
                    "Fleet Control operation process group identity drift"
                )
        except ProcessLookupError:
            return None
        return group

    def _signal_exact(
        self,
        process: subprocess.Popen[bytes],
        journal: dict[str, Any],
        requested_signal: signal.Signals,
    ) -> bool:
        group = self._verified_group(process, journal)
        if group is None:
            return False
        try:
            os.killpg(group, requested_signal)
        except ProcessLookupError:
            return False
        return True

    def _terminate_exact(
        self, process: subprocess.Popen[bytes], journal: dict[str, Any]
    ) -> None:
        if not self._signal_exact(process, journal, signal.SIGTERM):
            return
        # Cancellation is a zero-late-effect boundary, not a graceful-shutdown
        # invitation.  The anchored worker ignores SIGTERM so the exact group
        # remains revalidatable for immediate SIGKILL escalation.
        self._signal_exact(process, journal, signal.SIGKILL)

    def cancel_all(self) -> None:
        with self._lock:
            active = tuple(self._active.items())
            self._cancelled.update(operation_id for operation_id, _ in active)
        for _, (process, journal) in active:
            self._signal_exact(process, journal, signal.SIGTERM)
        for _, (process, journal) in active:
            if process.poll() is None:
                self._signal_exact(process, journal, signal.SIGKILL)

    def _cancel_requested(self, operation_id: str) -> bool:
        with self._lock:
            return operation_id in self._cancelled

    def assert_quiescent(self) -> None:
        with self._lock:
            if self._active:
                raise FleetControlError(
                    "Fleet Control operation supervisor is not quiescent"
                )

    @staticmethod
    def _close_descriptor(descriptor: int) -> None:
        try:
            os.close(descriptor)
        except OSError:
            pass

    @classmethod
    def _abort_unreleased_worker(
        cls,
        process: subprocess.Popen[bytes],
        *,
        gate_write: int | None,
        output: _BoundedOperationOutput | None,
        status: _OperationWorkerStatus | None,
    ) -> None:
        """Close an unreleased gate and leave no worker-owned resources behind."""

        if gate_write is not None:
            cls._close_descriptor(gate_write)
        cleanup_error: BaseException | None = None
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=2)
            except BaseException as exc:  # pragma: no cover - hostile OS failure
                cleanup_error = exc
        except BaseException as exc:  # pragma: no cover - hostile OS failure
            cleanup_error = exc
        if output is not None:
            try:
                output.finish()
            except BaseException as exc:  # pragma: no cover - hostile pipe failure
                cleanup_error = cleanup_error or exc
        else:
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except BaseException as exc:
                        cleanup_error = cleanup_error or exc
        if status is not None:
            try:
                status.finish()
            except BaseException as exc:  # pragma: no cover - hostile pipe failure
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise FleetControlError(
                "cannot clean up failed Fleet Control operation startup"
            ) from cleanup_error

    def _launch_operation_worker(
        self, command: list[str]
    ) -> tuple[
        subprocess.Popen[bytes],
        int,
        _BoundedOperationOutput,
        _OperationWorkerStatus,
    ]:
        owned_descriptors: set[int] = set()
        process: subprocess.Popen[bytes] | None = None
        output: _BoundedOperationOutput | None = None
        status: _OperationWorkerStatus | None = None

        def pipe() -> tuple[int, int]:
            read_descriptor, write_descriptor = os.pipe()
            owned_descriptors.update((read_descriptor, write_descriptor))
            return read_descriptor, write_descriptor

        def close_owned(descriptor: int) -> None:
            if descriptor in owned_descriptors:
                owned_descriptors.remove(descriptor)
                self._close_descriptor(descriptor)

        try:
            gate_read, gate_write = pipe()
            status_read, status_write = pipe()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "_operation-worker",
                    str(gate_read),
                    str(status_write),
                    "--",
                    *command,
                ],
                cwd=Path(__file__).resolve().parents[1],
                env={
                    **os.environ,
                    "FLEET_RUNS_DIR": str(self.control.runs_dir),
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=(gate_read, status_write),
            )
            close_owned(gate_read)
            close_owned(status_write)
            output = _BoundedOperationOutput(process)
            output.start()
            status = _OperationWorkerStatus(status_read)
            status.start()
            owned_descriptors.discard(status_read)
            owned_descriptors.discard(gate_write)
            return process, gate_write, output, status
        except BaseException as exc:
            for descriptor in tuple(owned_descriptors):
                close_owned(descriptor)
            if process is not None:
                try:
                    self._abort_unreleased_worker(
                        process,
                        gate_write=None,
                        output=output,
                        status=status,
                    )
                except FleetControlError as cleanup_exc:
                    raise cleanup_exc from exc
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, FleetControlError):
                raise
            raise FleetControlError(
                f"command failed to run: {Path(command[0]).name}: {exc}"
            ) from exc

    def run(
        self,
        command: list[str],
        *,
        runs_dir: Path,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if Path(runs_dir).resolve() != self.control.runs_dir.resolve():
            raise FleetControlError("operation runs root does not match Fleet Control")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(value, str) or not value for value in command)
        ):
            raise FleetControlError("invalid Fleet Control operation command")
        try:
            encoded_command = [os.fsencode(value) for value in command]
        except (UnicodeEncodeError, ValueError) as exc:
            raise FleetControlError("invalid Fleet Control operation command") from exc
        if any(b"\x00" in value for value in encoded_command):
            raise FleetControlError("invalid Fleet Control operation command")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        ):
            raise FleetControlError("invalid Fleet Control operation timeout")
        if self.stopping.is_set():
            raise FleetControlError("Fleet Control is stopping")

        operation_id = str(uuid.uuid4())
        process, gate_write, output, status = self._launch_operation_worker(command)
        try:
            process_identity, zombie = control_runtime.process_observation(process.pid)
        except control_runtime.RuntimeIdentityError as exc:
            self._abort_unreleased_worker(
                process,
                gate_write=gate_write,
                output=output,
                status=status,
            )
            raise FleetControlError(
                "cannot bind exact Fleet Control operation process"
            ) from exc
        try:
            exact_group = os.getpgid(process.pid) == process.pid
        except OSError as exc:
            self._abort_unreleased_worker(
                process,
                gate_write=gate_write,
                output=output,
                status=status,
            )
            raise FleetControlError(
                "cannot bind exact Fleet Control operation process"
            ) from exc
        if process_identity is None or zombie or not exact_group:
            self._abort_unreleased_worker(
                process,
                gate_write=gate_write,
                output=output,
                status=status,
            )
            raise FleetControlError(
                "Fleet Control operation worker exited before journaling"
            )
        journal = {
            "schema_version": 1,
            "mission_id": self.control.mission_id,
            "preset": self.control.preset,
            "service_launch_id": self.launch_id,
            "operation_id": operation_id,
            "pid": process.pid,
            "process_identity": process_identity,
            "process_group_id": process.pid,
            "command_sha256": mission_state.sha256(command),
            "state": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "returncode": None,
        }
        try:
            self._publish_running(journal)
        except BaseException:
            os.close(gate_write)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._terminate_exact(process, journal)
                process.wait(timeout=2)
            output.finish()
            status.finish()
            raise
        if (
            os.environ.get("FLEET_TEST_CONTROL_OPERATION_CRASH_AT")
            == "after_journal_before_release"
        ):
            os._exit(137)
        with self._lock:
            if self.stopping.is_set():
                admitted = False
            else:
                self._active[operation_id] = (process, journal)
                admitted = True
        if not admitted:
            os.close(gate_write)
            process.wait(timeout=2)
            output.finish()
            status.finish()
            self._publish_terminal(
                journal, state="cancelled", returncode=int(process.returncode or 125)
            )
            raise FleetControlError("Fleet Control is stopping")
        released = False
        timed_out = False
        output_exceeded = False
        terminal_attempted = False
        command_returncode: int | None = None
        try:
            try:
                if self.stopping.is_set() or self._cancel_requested(operation_id):
                    raise FleetControlError("Fleet Control is stopping")
                if os.write(gate_write, b"\x01") != 1:
                    raise FleetControlError(
                        "Fleet Control operation gate was not released"
                    )
                released = True
            finally:
                os.close(gate_write)
            if (
                os.environ.get("FLEET_TEST_CONTROL_OPERATION_CRASH_AT")
                == "after_release"
            ):
                os._exit(137)
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                if self.stopping.is_set() or self._cancel_requested(operation_id):
                    self._terminate_exact(process, journal)
                    break
                if output.exceeded.is_set() or output.failed.is_set():
                    output_exceeded = True
                    self._terminate_exact(process, journal)
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    self._terminate_exact(process, journal)
                    break
                if status.ready.wait(0.05):
                    command_returncode = status.returncode()
                    # The worker deliberately remains as the verified process-group
                    # leader after reporting the direct command result.  Killing the
                    # exact group here prevents background descendants from escaping
                    # an otherwise successful operation.
                    self._signal_exact(process, journal, signal.SIGKILL)
                    break
                if process.poll() is not None:
                    raise FleetControlError(
                        "Fleet Control operation worker exited without a result"
                    )
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired as exc:
                self._signal_exact(process, journal, signal.SIGKILL)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired as final_exc:
                    raise FleetControlError(
                        "Fleet Control operation was not reaped"
                    ) from final_exc
                raise FleetControlError(
                    "Fleet Control operation ignored termination"
                ) from exc
            stdout, stderr = output.finish()
            status.finish()
            output_exceeded = (
                output_exceeded or output.exceeded.is_set() or output.failed.is_set()
            )
            if process.returncode is None:
                raise FleetControlError("Fleet Control operation was not reaped")
            cancelled = (
                self.stopping.is_set()
                or self._cancel_requested(operation_id)
                or timed_out
                or output_exceeded
                or not released
                or command_returncode is None
            )
            terminal_returncode = (
                int(process.returncode)
                if command_returncode is None
                else command_returncode
            )
            terminal_attempted = True
            self._publish_terminal(
                journal,
                state="cancelled" if cancelled else "completed",
                returncode=terminal_returncode,
            )
            if output_exceeded:
                raise FleetControlError(
                    f"command failed to run: {Path(command[0]).name}: "
                    f"output exceeds {MAX_OPERATION_OUTPUT_BYTES} bytes"
                )
            if timed_out:
                raise FleetControlError(
                    f"command failed to run: {Path(command[0]).name}: timed out"
                )
            if self.stopping.is_set():
                raise FleetControlError("Fleet Control stopped during operation")
            if self._cancel_requested(operation_id):
                raise FleetControlError("Fleet Control operation was cancelled")
            if command_returncode is None:
                raise FleetControlError("Fleet Control operation lacks a result")
            return subprocess.CompletedProcess(
                command, command_returncode, stdout, stderr
            )
        except BaseException:
            if process.returncode is None:
                self._terminate_exact(process, journal)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._signal_exact(process, journal, signal.SIGKILL)
                process.wait(timeout=2)
            try:
                output.finish()
            finally:
                status.finish()
            if process.returncode is not None and not terminal_attempted:
                self._publish_terminal(
                    journal,
                    state="cancelled",
                    returncode=int(process.returncode),
                )
            raise
        finally:
            with self._lock:
                self._active.pop(operation_id, None)
                self._cancelled.discard(operation_id)


def _operation_worker(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        return 125
    try:
        gate_fd = int(argv[0])
        status_fd = int(argv[1])
    except ValueError:
        return 125
    command = argv[3:]
    termination_requested = threading.Event()

    def retain_group_anchor(_signum: int, _frame: Any) -> None:
        # The worker is the journal-bound process-group leader.  It must not
        # disappear on SIGTERM while a child (or descendant) can ignore that
        # signal; the supervisor/recovery path immediately escalates the
        # still-verifiable exact group to SIGKILL.
        termination_requested.set()

    def anchor_forever() -> None:
        while True:
            termination_requested.wait(timeout=3600)
            termination_requested.clear()

    signal.signal(signal.SIGTERM, retain_group_anchor)
    try:
        released = os.read(gate_fd, 1)
    except OSError:
        return 125
    finally:
        try:
            os.close(gate_fd)
        except OSError:
            pass
    if released != b"\x01":
        os.close(status_fd)
        return 125
    if termination_requested.is_set():
        os.close(status_fd)
        anchor_forever()
    environment = os.environ.copy()
    environment.pop("FLEET_TEST_SAFE_PATH_CRASH_AT", None)
    environment.pop("FLEET_TEST_CONTROL_OPERATION_CRASH_AT", None)
    try:
        child = subprocess.Popen(command, env=environment)
        if termination_requested.is_set():
            # SIGTERM may have landed in the narrow interval between the
            # pre-launch check and Popen.  Do not allow the just-created child
            # to perform work while the supervisor escalates the exact group.
            child.kill()
        returncode = child.wait()
    except OSError as exc:
        print(f"operation worker: {exc}", file=sys.stderr)
        result = 125
    else:
        result = returncode if returncode >= 0 else 128 - returncode
    try:
        payload = f"{result}\n".encode("ascii")
        os.write(status_fd, payload)
    except OSError:
        # The controller may have crashed and closed the read end.  Once the
        # execution gate was released, even a failed result handoff must never
        # let the journal-bound group leader disappear before recovery signals
        # the exact group.
        pass
    finally:
        try:
            os.close(status_fd)
        except OSError:
            pass
    # A successful direct child is not proof that its descendants are gone.
    # Remain as the exact group anchor until the supervisor receives the result
    # and kills the whole group.  The same invariant covers cancellation and
    # crash recovery, including a descendant that ignored SIGTERM.
    anchor_forever()


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    # server_close() must not return until every in-flight handler has exited;
    # the service writes its durable stopped receipt only after that barrier.
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._connections_lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        admission = self.admission  # type: ignore[attr-defined]
        management = self.management  # type: ignore[attr-defined]
        if not admission.try_admit(management=management):
            try:
                request.sendall(
                    b'{"ok":false,"error":"Fleet Control is stopping or busy"}\n'
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            admission.release(management=management)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.admission.release(  # type: ignore[attr-defined]
                management=self.management  # type: ignore[attr-defined]
            )

    def register_connection(self, connection: socket.socket) -> None:
        with self._connections_lock:
            self._connections.add(connection)

    def unregister_connection(self, connection: socket.socket) -> None:
        with self._connections_lock:
            self._connections.discard(connection)

    def cancel_connections(self) -> None:
        """Wake every accepted handler without owning its final close."""

        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _ControlSocketHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _ThreadingUnixServer)
        endpoint_instance = server.endpoint_instance  # type: ignore[attr-defined]
        server.register_connection(self.request)
        try:
            # A peer that connects but never terminates a frame cannot pin
            # service quiescence forever.  Authorized shutdown also wakes all
            # tracked connections immediately via cancel_connections().
            self.request.settimeout(1.0)
            if _peer_uid(self.request) != os.geteuid():
                raise FleetControlError("control socket peer uid mismatch")
            raw = self.rfile.readline(MAX_SOCKET_REQUEST_BYTES + 1)
            if (
                not raw
                or len(raw) > MAX_SOCKET_REQUEST_BYTES
                or not raw.endswith(b"\n")
            ):
                raise FleetControlError("invalid control socket frame")
            envelope = _loads_strict(raw)
            if endpoint_instance is None:
                health_result = server.health_result  # type: ignore[attr-defined]
                if isinstance(envelope, dict) and envelope == {"operation": "health"}:
                    value = health_result
                elif isinstance(envelope, dict) and envelope == {
                    "operation": "shutdown",
                    "process_id": health_result["process_id"],
                    "launch_id": health_result["launch_id"],
                }:
                    # Shutdown travels over the already authenticated,
                    # controller-only base endpoint.  This avoids a PID reuse
                    # race between a health response and an unauthenticated
                    # process signal.
                    # Keep this exact request alive long enough to return its
                    # acknowledgement; every other accepted connection is
                    # cancelled by the service shutdown barrier.
                    server.unregister_connection(self.request)
                    server.admission.begin_shutdown()  # type: ignore[attr-defined]
                    value = {
                        "status": "stopping",
                        "mission_id": health_result["mission_id"],
                        "process_id": health_result["process_id"],
                        "launch_id": health_result["launch_id"],
                    }
                else:
                    raise FleetControlError(
                        "base control socket is health-only except exact CONTROL shutdown"
                    )
            else:
                value = handle_socket_envelope(
                    server.control,  # type: ignore[attr-defined]
                    envelope,
                    endpoint_instance=endpoint_instance,
                )
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
            reply = {
                "ok": False,
                "error": (
                    SPECIALIST_DENIAL_MESSAGE
                    if endpoint_instance is not None
                    else str(exc)
                ),
            }
        try:
            try:
                payload = _dumps_strict(reply).encode("utf-8") + b"\n"
            except (TypeError, ValueError):
                payload = (
                    b'{"ok":false,"error":"specialist control request failed closed"}\n'
                )
            self.wfile.write(payload)
        except OSError:
            # Shutdown may have cancelled this accepted connection.  The
            # handler still exits through the barrier below.
            pass
        finally:
            server.unregister_connection(self.request)


def _create_socket_server(
    control: FleetControl,
    path: Path,
    *,
    root_fd: int,
    endpoint_instance: str | None,
    health_result: dict[str, Any],
    inherited_fd: int | None = None,
) -> _ThreadingUnixServer:
    if inherited_fd is None:
        server = _ThreadingUnixServer(
            str(path), _ControlSocketHandler, bind_and_activate=False
        )
        bound_identity: dict[str, int] | None = None
        try:
            server.server_bind()
            bound_identity = control_runtime.socket_binding_at(root_fd, path.name)
            bound_identity = control_runtime.chmod_socket_at(
                root_fd, path.name, bound_identity
            )
            server.server_activate()
        except BaseException:
            server.server_close()
            if bound_identity is not None:
                try:
                    control_runtime.unlink_socket_binding_at(
                        root_fd, path.name, bound_identity
                    )
                except control_runtime.RuntimeIdentityError:
                    pass
            raise
    else:
        if isinstance(inherited_fd, bool) or inherited_fd < 0:
            raise FleetControlError("invalid inherited control socket descriptor")
        server = _ThreadingUnixServer(
            str(path), _ControlSocketHandler, bind_and_activate=False
        )
        server.socket.close()
        try:
            inherited = socket.socket(
                family=socket.AF_UNIX,
                type=socket.SOCK_STREAM,
                proto=0,
                fileno=inherited_fd,
            )
            server.socket = inherited
            try:
                accepting = inherited.getsockopt(
                    socket.SOL_SOCKET, socket.SO_ACCEPTCONN
                )
            except OSError as exc:
                if exc.errno not in {errno.ENOPROTOOPT, errno.EOPNOTSUPP}:
                    raise
                # macOS AF_UNIX does not expose SO_ACCEPTCONN. The inherited
                # descriptor came through the gated controller pipe; the first
                # accept loop plus controller health probe proves listen state.
                accepting = 1
            if (
                inherited.family != socket.AF_UNIX
                or inherited.type & socket.SOCK_STREAM != socket.SOCK_STREAM
                or accepting != 1
            ):
                raise FleetControlError(
                    "inherited control descriptor is not a listening Unix stream socket"
                )
            # Linux pins SO_PEERCRED at listen(2), so the staging parent's
            # credentials would answer every client peer-PID check forever.
            # Re-listen from this exact server process so the kernel attests
            # the authenticated PID; macOS LOCAL_PEERPID reports the live
            # peer already and an extra listen only restates the backlog.
            inherited.listen()
        except BaseException:
            server.server_close()
            raise
    server.control = control  # type: ignore[attr-defined]
    server.endpoint_instance = endpoint_instance  # type: ignore[attr-defined]
    server.management = endpoint_instance is None  # type: ignore[attr-defined]
    server.health_result = health_result  # type: ignore[attr-defined]
    return server


def _write_stopped_receipt(
    control: FleetControl,
    *,
    health_result: dict[str, Any],
) -> None:
    relative = (
        Path("missions") / control.mission_id / "control" / "service-stopped.json"
    )
    receipt = {
        "schema_version": 1,
        "mission_id": control.mission_id,
        "preset": control.preset,
        "pid": health_result["process_id"],
        "process_identity": health_result["process_identity"],
        "launch_id": health_result["launch_id"],
        "socket_root_identity": health_result["socket_root_identity"],
        "endpoint_binding_sha256": health_result["endpoint_binding_sha256"],
        "endpoint_identities": health_result["endpoint_identities"],
        "stopped_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with fleet_safe_paths.RootedFS(control.runs_dir) as rooted:
            rooted.atomic_write(
                relative,
                mission_state.canonical_bytes(receipt) + b"\n",
                directory_modes=(0o700, 0o700, 0o700),
                file_mode=0o600,
            )
    except fleet_safe_paths.SafePathError as exc:
        raise FleetControlError("cannot publish exact control stopped receipt") from exc


def serve_sockets(
    control: FleetControl,
    base_path: Path,
    instance_paths: dict[str, Path],
    *,
    launch_id: str,
    inherited_fds: dict[str, int] | None = None,
) -> None:
    expected_instances = set(control.members()) - {"lead"}
    if set(instance_paths) != expected_instances:
        raise FleetControlError(
            "specialist control endpoints do not match compiled roster"
        )
    try:
        launch_id = str(uuid.UUID(launch_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise FleetControlError("invalid Fleet Control launch identity") from exc
    raw_paths = [base_path, *instance_paths.values()]
    if any(
        not path.is_absolute() or path.name in {"", ".", ".."} for path in raw_paths
    ):
        raise FleetControlError("control socket endpoints must be absolute leaves")
    try:
        socket_root = fleet_safe_paths.canonical_root(
            base_path.parent, required_mode=0o700
        )
        for path in raw_paths:
            if (
                fleet_safe_paths.canonical_root(path.parent, required_mode=0o700)
                != socket_root
            ):
                raise FleetControlError("control socket endpoints must share one root")
    except fleet_safe_paths.SafePathError as exc:
        raise FleetControlError("unsafe control socket root") from exc
    base_path = socket_root / base_path.name
    instance_paths = {
        instance: socket_root / path.name
        for instance, path in sorted(instance_paths.items())
    }
    all_paths = [base_path, *instance_paths.values()]
    if len(set(all_paths)) != len(all_paths):
        raise FleetControlError("control socket endpoints must be unique")
    binding = mission_state.sha256(
        {instance: str(path) for instance, path in instance_paths.items()}
    )
    try:
        process_identity, zombie = control_runtime.process_observation(os.getpid())
    except control_runtime.RuntimeIdentityError as exc:
        raise FleetControlError("cannot bind exact Fleet Control process") from exc
    if process_identity is None or zombie:
        raise FleetControlError("Fleet Control process is not live")
    health_result: dict[str, Any] = {}
    specifications: list[tuple[Path, str | None]] = [(base_path, None)]
    specifications.extend((path, instance) for instance, path in instance_paths.items())
    servers: list[_ThreadingUnixServer] = []
    endpoint_identities: dict[str, dict[str, int]] = {}
    admission = _AdmissionGate()
    stopping = admission.stopping
    operation_supervisor = _OperationSupervisor(
        control, launch_id=launch_id, stopping=stopping
    )
    expected_endpoint_keys = {"base", *expected_instances}
    if inherited_fds is not None and set(inherited_fds) != expected_endpoint_keys:
        raise FleetControlError(
            "inherited control descriptors do not match compiled endpoints"
        )
    try:
        with control_runtime.open_socket_root(socket_root) as (
            root_fd,
            socket_root_identity,
        ):
            for path, instance in specifications:
                key = "base" if instance is None else instance
                inherited_fd = None if inherited_fds is None else inherited_fds[key]
                if inherited_fd is None:
                    try:
                        control_runtime.assert_absent_at(root_fd, path.name)
                    except control_runtime.RuntimeIdentityError as exc:
                        raise FleetControlError(
                            "refusing pre-existing control socket path"
                        ) from exc
                servers.append(
                    _create_socket_server(
                        control,
                        path,
                        root_fd=root_fd,
                        endpoint_instance=instance,
                        health_result=health_result,
                        inherited_fd=inherited_fd,
                    )
                )
                endpoint_identities[key] = control_runtime.socket_identity_at(
                    root_fd, path.name
                )
                servers[-1].stopping = stopping  # type: ignore[attr-defined]
                servers[-1].admission = admission  # type: ignore[attr-defined]
            health_result.update(
                {
                    "status": "ok",
                    "mission_id": control.mission_id,
                    "preset": control.preset,
                    "protocol": "fleet-control-unix-v2",
                    "process_id": os.getpid(),
                    "process_identity": process_identity,
                    "launch_id": launch_id,
                    "socket_root_identity": socket_root_identity,
                    "endpoint_identities": endpoint_identities,
                    "instance_endpoint_count": len(instance_paths),
                    "endpoint_binding_sha256": binding,
                }
            )
    except BaseException:
        for server in servers:
            server.server_close()
        try:
            with control_runtime.open_socket_root(socket_root) as (root_fd, _):
                for key, identity in endpoint_identities.items():
                    path = base_path if key == "base" else instance_paths[key]
                    control_runtime.unlink_socket_at(root_fd, path.name, identity)
        except control_runtime.RuntimeIdentityError:
            pass
        raise
    threads = [
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1})
        for server in servers
    ]
    started = 0
    original_run_process = fleet_control_module.run_process
    fleet_control_module.run_process = operation_supervisor.run
    try:
        for thread in threads:
            thread.start()
            started += 1
        while not stopping.wait(0.1):
            if any(not thread.is_alive() for thread in threads):
                raise FleetControlError("control socket endpoint stopped unexpectedly")
    finally:
        requested_shutdown = stopping.is_set()
        admission.begin_shutdown()
        operation_supervisor.cancel_all()
        for server in servers:
            server.cancel_connections()
        for server in servers[:started]:
            server.shutdown()
        for thread in threads[:started]:
            thread.join(timeout=2)
        # Close the accept race between the first cancellation snapshot and
        # serve_forever() observing shutdown.
        for server in servers:
            server.cancel_connections()
        for server in servers:
            server.server_close()
        operation_supervisor.assert_quiescent()
        fleet_control_module.run_process = original_run_process
        try:
            with control_runtime.open_socket_root(
                socket_root, health_result["socket_root_identity"]
            ) as (root_fd, _):
                for key, identity in sorted(endpoint_identities.items()):
                    path = base_path if key == "base" else instance_paths[key]
                    control_runtime.unlink_socket_at(root_fd, path.name, identity)
        except control_runtime.RuntimeIdentityError as exc:
            raise FleetControlError(
                "cannot remove exact control socket endpoints"
            ) from exc
        if requested_shutdown:
            _write_stopped_receipt(control, health_result=health_result)


def _parse_instance_sockets(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise FleetControlError("--instance-socket requires INSTANCE=PATH")
        instance, raw_path = value.split("=", 1)
        if not instance or not raw_path or instance in result:
            raise FleetControlError("invalid or duplicate --instance-socket binding")
        result[instance] = Path(raw_path)
    return result


def _parse_inherited_sockets(values: list[str]) -> dict[str, int] | None:
    if not values:
        return None
    result: dict[str, int] = {}
    for value in values:
        if "=" not in value:
            raise FleetControlError("--inherited-socket requires ENDPOINT=FD")
        endpoint, raw_fd = value.split("=", 1)
        try:
            descriptor = int(raw_fd)
        except ValueError as exc:
            raise FleetControlError("invalid --inherited-socket descriptor") from exc
        if (
            not endpoint
            or endpoint in result
            or descriptor < 0
            or descriptor in result.values()
        ):
            raise FleetControlError("invalid or duplicate --inherited-socket binding")
        result[endpoint] = descriptor
    return result


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "_operation-worker":
        return _operation_worker(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--preset")
    parser.add_argument("--socket")
    parser.add_argument("--instance-socket", action="append", default=[])
    parser.add_argument("--inherited-socket", action="append", default=[])
    parser.add_argument("--launch-id")
    args = parser.parse_args(argv)
    try:
        control = FleetControl(
            Path(args.runs_dir),
            args.mission_id,
            preset=args.preset,
            decision_notifier=fleet_control_module.cmux_decision_notifier,
        )
        if args.socket:
            if not args.launch_id:
                raise FleetControlError("--socket requires --launch-id")
            serve_sockets(
                control,
                Path(args.socket),
                _parse_instance_sockets(args.instance_socket),
                launch_id=args.launch_id,
                inherited_fds=_parse_inherited_sockets(args.inherited_socket),
            )
            return 0
        if args.instance_socket or args.inherited_socket or args.launch_id:
            raise FleetControlError("--instance-socket/--launch-id require --socket")
        for raw in sys.stdin:
            try:
                request = _loads_strict(raw)
                value = handle(control, request)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
                value = response(None, error={"code": -32700, "message": "Parse error"})
            if value is not None:
                print(_dumps_strict(value), flush=True)
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
