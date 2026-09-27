"""Offline, standard-library verifier for one OpenCode session export segment.

This module validates a single, explicitly bound turn of an OpenCode native
export (``info`` + ``messages`` with ``info`` + ``parts``).  The caller supplies
the trusted bindings; nothing is inferred from labels, configuration or pane
state.  The component performs no filesystem, subprocess, network, database or
Herdr access and grants no authority.

The verifier only attests identity and content of the requested segment.  It
never attests sandboxing, runtime permissions, cost, effort or Mission
acceptance, even when the final text contains such claims.
"""

from __future__ import annotations

import hashlib
import math

import fleet_json

__all__ = ["EvidenceError", "verify_export", "MAX_RAW_BYTES", "MAX_JSON_DEPTH"]

SCHEMA_VERSION = "herdr.opencode.evidence.v1"
MAX_RAW_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 200


class EvidenceError(ValueError):
    """Raised when the export does not satisfy the text-only evidence contract."""


def _require_str(value, name):
    if not isinstance(value, str) or value == "":
        raise EvidenceError("%s must be a non-empty string" % name)
    return value


def _require_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceError("%s must be an integer" % name)
    return value


def _require_mapping(value, name):
    if not isinstance(value, dict):
        raise EvidenceError("%s must be a JSON object" % name)
    return value


def _sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _check_depth(document):
    stack = [(document, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise EvidenceError("JSON nesting is too deep")
        if isinstance(node, dict):
            for value in node.values():
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            for value in node:
                stack.append((value, depth + 1))
        elif isinstance(node, float) and not math.isfinite(node):
            raise EvidenceError("non-finite JSON number is not allowed")


def _load_json(raw):
    if not isinstance(raw, (bytes, bytearray)):
        raise EvidenceError("raw export must be bytes")
    if len(raw) > MAX_RAW_BYTES:
        raise EvidenceError("raw export exceeds the %d byte limit" % MAX_RAW_BYTES)
    try:
        document = fleet_json.loads(raw)
    except fleet_json.FleetJSONError as exc:
        if isinstance(exc.__cause__, RecursionError):
            raise EvidenceError("JSON nesting is too deep") from exc
        raise EvidenceError("raw export is not strict JSON: %s" % exc) from exc
    _check_depth(document)
    return document


def _check_session(info, session_id, cwd, cli_version):
    _require_mapping(info, "info")
    if _require_str(info.get("id"), "info.id") != session_id:
        raise EvidenceError("session id does not match the session_id binding")
    if _require_str(info.get("directory"), "info.directory") != cwd:
        raise EvidenceError("session directory does not match the cwd binding")
    if _require_str(info.get("version"), "info.version") != cli_version:
        raise EvidenceError("session version does not match the cli_version binding")


def _index_messages(messages, session_id):
    if not isinstance(messages, list) or not messages:
        raise EvidenceError("messages must be a non-empty list")
    by_id = {}
    part_ids = set()
    for message in messages:
        _require_mapping(message, "message")
        if "info" not in message or "parts" not in message:
            raise EvidenceError("each message requires info and parts")
        info = _require_mapping(message["info"], "message.info")
        parts = message["parts"]
        if not isinstance(parts, list):
            raise EvidenceError("message.parts must be a list")
        message_id = _require_str(info.get("id"), "message.info.id")
        if message_id in by_id:
            raise EvidenceError("duplicate message id: %s" % message_id)
        session_ref = _require_str(info.get("sessionID"), "message.info.sessionID")
        if session_ref != session_id:
            raise EvidenceError("message %s references another session" % message_id)
        _require_str(info.get("role"), "message.info.role")
        by_id[message_id] = len(by_id)
        for part in parts:
            _require_mapping(part, "message.parts[]")
            part_id = _require_str(part.get("id"), "part.id")
            if part_id in part_ids:
                raise EvidenceError("duplicate part id: %s" % part_id)
            part_ids.add(part_id)
            if _require_str(part.get("sessionID"), "part.sessionID") != session_id:
                raise EvidenceError("part %s references another session" % part_id)
            if _require_str(part.get("messageID"), "part.messageID") != message_id:
                raise EvidenceError("part %s references another message" % part_id)
            _require_str(part.get("type"), "part.type")
    return by_id


def _require_false_marker(part, part_id):
    for marker in ("synthetic", "ignored"):
        if marker in part and part[marker] is not False:
            raise EvidenceError(
                "part %s carries a %s marker that is not the strict boolean false"
                % (part_id, marker)
            )


def _require_completed_tool_state(part, part_id):
    state = part.get("state")
    if not isinstance(state, dict):
        raise EvidenceError("tool part %s has no valid state object" % part_id)
    if state.get("status") != "completed":
        raise EvidenceError("tool part %s is not a completed step" % part_id)


def _user_prompt_text(message):
    info = message["info"]
    if info.get("role") != "user":
        raise EvidenceError("the requested user message role is not user")
    texts = []
    for part in message["parts"]:
        if part.get("type") != "text":
            raise EvidenceError("the user message contains a non-text part")
        _require_false_marker(part, part.get("id"))
        text = part.get("text")
        if not isinstance(text, str):
            raise EvidenceError("the user text part has no string text")
        texts.append(text)
    if not texts:
        raise EvidenceError("the user message contains no text")
    return "\n".join(texts)


def _check_assistant_binding(message, user_message_id, provider, model, cwd):
    info = message["info"]
    if info.get("role") != "assistant":
        raise EvidenceError("the segment contains a non-assistant message")
    if info.get("parentID") != user_message_id:
        raise EvidenceError("an assistant message does not belong to the requested turn")
    if info.get("providerID") != provider:
        raise EvidenceError("an assistant provider does not match the provider binding")
    if info.get("modelID") != model:
        raise EvidenceError("an assistant model does not match the model binding")
    path = _require_mapping(info.get("path"), "assistant.path")
    if path.get("cwd") != cwd:
        raise EvidenceError("an assistant cwd does not match the cwd binding")
    time = _require_mapping(info.get("time"), "assistant.time")
    created = _require_int(time.get("created"), "assistant.time.created")
    completed = _require_int(time.get("completed"), "assistant.time.completed")
    if completed < created:
        raise EvidenceError("an assistant completion precedes its creation")
    if info.get("error") is not None:
        raise EvidenceError("an assistant message reports an error")
    _require_str(info.get("finish"), "assistant.finish")
    return info


def _final_text(message):
    texts = []
    for part in message["parts"]:
        if part.get("type") != "text":
            continue
        _require_false_marker(part, part.get("id"))
        text = part.get("text")
        if not isinstance(text, str):
            raise EvidenceError("the final text part has no string text")
        texts.append(text)
    if not texts:
        raise EvidenceError("the final message contains no text")
    return "\n".join(texts)


def verify_export(
    raw,
    *,
    session_id,
    user_message_id,
    assistant_message_id,
    cwd,
    provider,
    model,
    prompt_sha256,
    final_bytes,
    cli_version,
):
    """Verify one explicitly bound user/final-assistant segment of a native export.

    Every binding argument is trusted input supplied by the controller; the
    parser never selects the latest assistant message nor trusts labels or
    configuration.  Returns a stable mapping describing only identity and
    content attestation.
    """
    session_id = _require_str(session_id, "session_id")
    user_message_id = _require_str(user_message_id, "user_message_id")
    assistant_message_id = _require_str(assistant_message_id, "assistant_message_id")
    cwd = _require_str(cwd, "cwd")
    provider = _require_str(provider, "provider")
    model = _require_str(model, "model")
    prompt_sha256 = _require_str(prompt_sha256, "prompt_sha256")
    cli_version = _require_str(cli_version, "cli_version")
    if not isinstance(final_bytes, (bytes, bytearray)):
        raise EvidenceError("final_bytes must be bytes")
    final_bytes = bytes(final_bytes)
    raw_bytes = bytes(raw) if isinstance(raw, (bytes, bytearray)) else raw

    document = _load_json(raw)
    _require_mapping(document, "export")
    if "info" not in document or "messages" not in document:
        raise EvidenceError("the export requires info and messages")
    _check_session(document["info"], session_id, cwd, cli_version)
    by_id = _index_messages(document["messages"], session_id)
    messages = document["messages"]

    if user_message_id not in by_id:
        raise EvidenceError("the requested user message id is missing")
    if assistant_message_id not in by_id:
        raise EvidenceError("the requested assistant message id is missing")
    user_index = by_id[user_message_id]
    assistant_index = by_id[assistant_message_id]
    if user_index >= assistant_index:
        raise EvidenceError("the requested user message must precede the final assistant message")

    segment_end = len(messages)
    for position in range(user_index + 1, len(messages)):
        if messages[position]["info"].get("role") == "user":
            segment_end = position
            break

    if assistant_index != segment_end - 1:
        raise EvidenceError("the final assistant message must be the last message of its segment")

    prompt_text = _user_prompt_text(messages[user_index])
    try:
        prompt_bytes = prompt_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EvidenceError("the user prompt is not valid UTF-8 text") from exc
    if _sha256_hex(prompt_bytes) != prompt_sha256:
        raise EvidenceError("the user prompt hash does not match the prompt_sha256 binding")

    for position in range(user_index + 1, segment_end):
        message = messages[position]
        info = _check_assistant_binding(message, user_message_id, provider, model, cwd)
        tool_parts = [part for part in message["parts"] if part.get("type") == "tool"]
        if info.get("finish") == "tool-calls" and not tool_parts:
            raise EvidenceError("an assistant tool-calls step contains no tool part")
        for part in message["parts"]:
            if part.get("type") == "tool":
                _require_completed_tool_state(part, part.get("id"))

    final_info = _check_assistant_binding(
        messages[assistant_index], user_message_id, provider, model, cwd
    )
    if final_info.get("finish") != "stop":
        raise EvidenceError("the final assistant message did not finish with stop")
    try:
        final_text_bytes = _final_text(messages[assistant_index]).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EvidenceError("the final assistant text is not valid UTF-8 text") from exc
    if final_text_bytes != final_bytes:
        raise EvidenceError("the final assistant text does not match final_bytes")

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "verified",
        "authority": "none",
        "permissions": {
            "status": "not_attested",
            "scope": "identity_and_content_only",
        },
        "session_id": session_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "cwd": cwd,
        "provider": provider,
        "model": model,
        "cli_version": cli_version,
        "prompt_sha256": prompt_sha256,
        "final_sha256": _sha256_hex(final_bytes),
        "export_sha256": _sha256_hex(raw_bytes),
    }
