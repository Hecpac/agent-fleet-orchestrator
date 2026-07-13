#!/usr/bin/env python3
"""Durable, CONTROL-mediated dialogue messages for one active fleet."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any
import uuid

from fleet_identity import validate as validate_identity
from fleet_leases import closing_path, coordinator
from fleet_ledger import append_record, latest_event


SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 1024 * 1024
MESSAGE_KINDS = {"proposal", "challenge", "rebuttal", "revision", "verification"}
SAFE_FEATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
SAFE_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ENVELOPE_FIELDS = {
    "schema_version",
    "timestamp",
    "message_id",
    "feature",
    "kind",
    "sender",
    "recipient",
    "source_instance",
    "source_run_id",
    "reply_to",
    "idempotency_key",
    "payload_sha256",
    "payload_bytes",
}


class DialogueError(RuntimeError):
    pass


class DialogueConflict(DialogueError):
    pass


class DialogueClosing(DialogueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def manifest_path(runs_dir: Path, feature: str) -> Path:
    return runs_dir / f"fleet-{feature}.manifest"


def ledger_path(runs_dir: Path, feature: str) -> Path:
    return runs_dir / f"fleet-{feature}.dialogue.jsonl"


def store_path(runs_dir: Path, feature: str) -> Path:
    return runs_dir / "dialogue" / feature / "payloads"


def payload_path(runs_dir: Path, feature: str, digest: str) -> Path:
    return store_path(runs_dir, feature) / digest


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _manifest_values(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DialogueError(f"missing fleet manifest: {path}") from exc
    values = dict(line.split("=", 1) for line in lines if "=" in line)
    if not values:
        raise DialogueError(f"empty fleet manifest: {path}")
    return values


def _instances(manifest: dict[str, str]) -> set[str]:
    return {
        key
        for key, value in manifest.items()
        if value.startswith("surface:") and manifest.get(f"{key}.uuid")
    }


def _load_manifest(runs_dir: Path, feature: str) -> tuple[dict[str, str], set[str]]:
    if not SAFE_FEATURE.fullmatch(feature):
        raise DialogueError(f"invalid feature: {feature}")
    manifest = _manifest_values(manifest_path(runs_dir, feature))
    if manifest.get("feature") != feature:
        raise DialogueError("manifest feature does not match requested feature")
    instances = _instances(manifest)
    if not instances:
        raise DialogueError("manifest has no durable instance identities")
    try:
        identity_errors = validate_identity(manifest, sorted(instances))
    except RuntimeError as exc:
        raise DialogueError(f"cannot validate live fleet identity: {exc}") from exc
    if identity_errors:
        raise DialogueError("; ".join(identity_errors))
    return manifest, instances


def _validate_envelope(message: dict[str, Any], feature: str) -> None:
    if set(message) != ENVELOPE_FIELDS:
        raise DialogueError("dialogue envelope fields do not match schema_version=1")
    if message.get("schema_version") != SCHEMA_VERSION:
        raise DialogueError("unsupported dialogue schema version")
    try:
        uuid.UUID(str(message.get("message_id") or ""))
    except ValueError as exc:
        raise DialogueError("invalid dialogue message_id") from exc
    if message.get("feature") != feature:
        raise DialogueError("dialogue message belongs to another feature")
    if message.get("sender") != "CONTROL":
        raise DialogueError("dialogue sender must be CONTROL")
    if message.get("kind") not in MESSAGE_KINDS:
        raise DialogueError("invalid dialogue message kind")
    if not SAFE_RUN_ID.fullmatch(str(message.get("source_run_id") or "")):
        raise DialogueError("invalid dialogue source_run_id")
    if not SAFE_IDEMPOTENCY_KEY.fullmatch(str(message.get("idempotency_key") or "")):
        raise DialogueError("invalid dialogue idempotency_key")
    if not SHA256.fullmatch(str(message.get("payload_sha256") or "")):
        raise DialogueError("invalid dialogue payload_sha256")
    size = message.get("payload_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_PAYLOAD_BYTES:
        raise DialogueError("invalid dialogue payload size")
    reply_to = message.get("reply_to")
    if reply_to is not None:
        try:
            uuid.UUID(str(reply_to))
        except ValueError as exc:
            raise DialogueError("invalid dialogue reply_to") from exc
    if not isinstance(message.get("timestamp"), str) or not message["timestamp"]:
        raise DialogueError("invalid dialogue timestamp")
    for field in ("recipient", "source_instance"):
        if not isinstance(message.get(field), str) or not message[field]:
            raise DialogueError(f"invalid dialogue {field}")


def load_messages_from_path(path: Path, feature: str) -> list[dict[str, Any]]:
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise DialogueError(f"cannot read dialogue ledger: {path}") from exc
    messages: list[dict[str, Any]] = []
    message_ids: set[str] = set()
    idempotency_keys: set[str] = set()
    for line_number, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            raise DialogueError(f"blank dialogue ledger row at line {line_number}")
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DialogueError(f"invalid dialogue JSON at line {line_number}") from exc
        if not isinstance(message, dict):
            raise DialogueError(f"dialogue row is not an object at line {line_number}")
        _validate_envelope(message, feature)
        message_id = str(message["message_id"])
        idempotency_key = str(message["idempotency_key"])
        if message_id in message_ids or idempotency_key in idempotency_keys:
            raise DialogueError("dialogue ledger contains duplicate identity")
        reply_to = message.get("reply_to")
        if reply_to is not None and reply_to not in message_ids:
            raise DialogueError("dialogue reply_to does not name a prior message")
        message_ids.add(message_id)
        idempotency_keys.add(idempotency_key)
        messages.append(message)
    return messages


def load_messages(runs_dir: Path, feature: str) -> list[dict[str, Any]]:
    return load_messages_from_path(ledger_path(runs_dir, feature), feature)


def _read_bounded_regular_file(path: Path, *, expected_size: int | None = None) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DialogueError(f"cannot open dialogue payload source: {path}") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise DialogueError(f"dialogue payload source is not a regular file: {path}")
        if metadata.st_size <= 0:
            raise DialogueError("dialogue payload is empty")
        if metadata.st_size > MAX_PAYLOAD_BYTES:
            raise DialogueError(
                f"dialogue payload exceeds {MAX_PAYLOAD_BYTES} bytes: {metadata.st_size}"
            )
        payload = b""
        while len(payload) <= MAX_PAYLOAD_BYTES:
            chunk = os.read(fd, min(65536, MAX_PAYLOAD_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        if len(payload) != metadata.st_size:
            raise DialogueError("dialogue payload changed while being read")
        if expected_size is not None and len(payload) != expected_size:
            raise DialogueError("dialogue payload size does not match envelope")
        return payload
    finally:
        os.close(fd)


def _source_payload(
    runs_dir: Path,
    feature: str,
    source_instance: str,
    source_run_id: str,
) -> bytes:
    if not SAFE_RUN_ID.fullmatch(source_run_id):
        raise DialogueError(f"invalid source run_id: {source_run_id}")
    event = latest_event(
        runs_dir / f"fleet-{feature}.ledger.jsonl",
        run_id=source_run_id,
        instance=source_instance,
    )
    if not event:
        raise DialogueError("source run is absent from the lifecycle ledger")
    if event.get("feature") != feature or event.get("instance") != source_instance:
        raise DialogueError("source run identity does not match the active fleet")
    if event.get("status") != "succeeded":
        raise DialogueError("source run is not terminal succeeded")
    recorded = event.get("result_file")
    if not isinstance(recorded, str) or not recorded:
        raise DialogueError("source run has no durable result_file")
    expected = runs_dir / "results" / feature / f"{source_run_id}.txt"
    recorded_path = Path(recorded)
    if recorded_path.is_symlink() or expected.is_symlink():
        raise DialogueError("source result_file must not be a symlink")
    try:
        actual = expected.resolve(strict=True)
        recorded_actual = recorded_path.resolve(strict=True)
    except OSError as exc:
        raise DialogueError("source result_file is missing") from exc
    if actual != recorded_actual:
        raise DialogueError("source result_file is outside the fleet result store")
    return _read_bounded_regular_file(actual)


def _write_payload(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.exists():
        existing = _read_bounded_regular_file(path, expected_size=len(payload))
        if existing != payload:
            raise DialogueError("content-addressed payload does not match its digest")
        return
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _verified_payload(runs_dir: Path, feature: str, message: dict[str, Any]) -> tuple[Path, bytes]:
    return _verified_payload_from_store(store_path(runs_dir, feature), message)


def _verified_payload_from_store(
    store: Path, message: dict[str, Any]
) -> tuple[Path, bytes]:
    path = store / str(message["payload_sha256"])
    payload = _read_bounded_regular_file(path, expected_size=int(message["payload_bytes"]))
    if hashlib.sha256(payload).hexdigest() != message["payload_sha256"]:
        raise DialogueError("dialogue payload hash does not match envelope")
    return path, payload


def verify_storage(
    dialogue_ledger: Path,
    payload_store: Path,
    *,
    feature: str,
    instances: set[str] | None = None,
) -> dict[str, int]:
    messages = load_messages_from_path(dialogue_ledger, feature)
    verified: set[str] = set()
    total_bytes = 0
    for message in messages:
        if instances is not None and (
            message["recipient"] not in instances
            or message["source_instance"] not in instances
        ):
            raise DialogueError("dialogue message references an unknown manifest instance")
        if message["payload_sha256"] not in verified:
            _, payload = _verified_payload_from_store(payload_store, message)
            verified.add(message["payload_sha256"])
            total_bytes += len(payload)
    return {
        "messages": len(messages),
        "payloads": len(verified),
        "payload_bytes": total_bytes,
    }


def publish(
    runs_dir: Path,
    *,
    feature: str,
    kind: str,
    recipient: str,
    source_instance: str,
    source_run_id: str,
    idempotency_key: str,
    reply_to: str | None = None,
) -> dict[str, Any]:
    if kind not in MESSAGE_KINDS:
        raise DialogueError(f"invalid message kind: {kind}")
    if not SAFE_IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise DialogueError("invalid idempotency key")
    with coordinator(runs_dir):
        _, instances = _load_manifest(runs_dir, feature)
        if closing_path(runs_dir, feature).exists():
            raise DialogueClosing(f"fleet '{feature}' is closing")
        if recipient not in instances:
            raise DialogueError(f"unknown recipient instance: {recipient}")
        if source_instance not in instances:
            raise DialogueError(f"unknown source instance: {source_instance}")
        messages = load_messages(runs_dir, feature)
        by_id = {message["message_id"]: message for message in messages}
        if reply_to is not None and reply_to not in by_id:
            raise DialogueError("reply_to must name a prior message in this fleet")
        payload = _source_payload(runs_dir, feature, source_instance, source_run_id)
        digest = hashlib.sha256(payload).hexdigest()
        request_identity = {
            "kind": kind,
            "recipient": recipient,
            "source_instance": source_instance,
            "source_run_id": source_run_id,
            "reply_to": reply_to,
            "payload_sha256": digest,
            "payload_bytes": len(payload),
        }
        existing = next(
            (
                message
                for message in messages
                if message["idempotency_key"] == idempotency_key
            ),
            None,
        )
        if existing is not None:
            if any(existing[field] != value for field, value in request_identity.items()):
                raise DialogueConflict("idempotency key was already used for another message")
            _verified_payload(runs_dir, feature, existing)
            return existing
        path = payload_path(runs_dir, feature, digest)
        _write_payload(path, payload)
        message = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": utc_now(),
            "message_id": str(uuid.uuid4()),
            "feature": feature,
            "kind": kind,
            "sender": "CONTROL",
            "recipient": recipient,
            "source_instance": source_instance,
            "source_run_id": source_run_id,
            "reply_to": reply_to,
            "idempotency_key": idempotency_key,
            "payload_sha256": digest,
            "payload_bytes": len(payload),
        }
        appended = append_record(
            ledger_path(runs_dir, feature),
            message,
            reject_if=lambda previous: (
                previous.get("message_id") == message["message_id"]
                or previous.get("idempotency_key") == idempotency_key
            ),
        )
        if not appended:
            raise DialogueConflict("dialogue identity changed during publication")
        return message


def query(
    runs_dir: Path,
    *,
    feature: str,
    recipient: str | None = None,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    with coordinator(runs_dir):
        _, instances = _load_manifest(runs_dir, feature)
        if recipient is not None and recipient not in instances:
            raise DialogueError(f"unknown recipient instance: {recipient}")
        if kind is not None and kind not in MESSAGE_KINDS:
            raise DialogueError(f"invalid message kind: {kind}")
        messages = load_messages(runs_dir, feature)
        for message in messages:
            if message["recipient"] not in instances or message["source_instance"] not in instances:
                raise DialogueError("dialogue message references an unknown manifest instance")
        return [
            message
            for message in messages
            if (recipient is None or message["recipient"] == recipient)
            and (kind is None or message["kind"] == kind)
        ]


def get_message(runs_dir: Path, *, feature: str, message_id: str) -> dict[str, Any]:
    messages = query(runs_dir, feature=feature)
    message = next((item for item in messages if item["message_id"] == message_id), None)
    if message is None:
        raise DialogueError(f"unknown dialogue message: {message_id}")
    return message


def read_message(runs_dir: Path, *, feature: str, message_id: str) -> bytes:
    with coordinator(runs_dir):
        _, instances = _load_manifest(runs_dir, feature)
        messages = load_messages(runs_dir, feature)
        message = next((item for item in messages if item["message_id"] == message_id), None)
        if message is None:
            raise DialogueError(f"unknown dialogue message: {message_id}")
        if message["recipient"] not in instances or message["source_instance"] not in instances:
            raise DialogueError("dialogue message references an unknown manifest instance")
        _, payload = _verified_payload(runs_dir, feature, message)
        return payload


def verify(runs_dir: Path, *, feature: str) -> dict[str, int]:
    with coordinator(runs_dir):
        _, instances = _load_manifest(runs_dir, feature)
        return verify_storage(
            ledger_path(runs_dir, feature),
            store_path(runs_dir, feature),
            feature=feature,
            instances=instances,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("publish", "list", "show", "read", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("runs_dir")
        command.add_argument("--feature", required=True)
        if name == "publish":
            command.add_argument("--kind", required=True, choices=sorted(MESSAGE_KINDS))
            command.add_argument("--recipient", required=True)
            command.add_argument("--source-instance", required=True)
            command.add_argument("--source-run-id", required=True)
            command.add_argument("--idempotency-key", required=True)
            command.add_argument("--reply-to")
        elif name == "list":
            command.add_argument("--recipient")
            command.add_argument("--kind", choices=sorted(MESSAGE_KINDS))
        elif name in {"show", "read"}:
            command.add_argument("--message-id", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    runs_dir = Path(args.runs_dir).expanduser().resolve()
    try:
        if args.command == "publish":
            value: Any = publish(
                runs_dir,
                feature=args.feature,
                kind=args.kind,
                recipient=args.recipient,
                source_instance=args.source_instance,
                source_run_id=args.source_run_id,
                idempotency_key=args.idempotency_key,
                reply_to=args.reply_to,
            )
        elif args.command == "list":
            value = query(
                runs_dir,
                feature=args.feature,
                recipient=args.recipient,
                kind=args.kind,
            )
        elif args.command == "show":
            value = get_message(
                runs_dir,
                feature=args.feature,
                message_id=args.message_id,
            )
            value = {
                **value,
                "payload_file": str(payload_path(runs_dir, args.feature, value["payload_sha256"])),
            }
        elif args.command == "read":
            payload = read_message(
                runs_dir,
                feature=args.feature,
                message_id=args.message_id,
            )
            sys.stdout.buffer.write(payload)
            return 0
        else:
            value = verify(runs_dir, feature=args.feature)
    except (DialogueConflict, DialogueClosing) as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except DialogueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
