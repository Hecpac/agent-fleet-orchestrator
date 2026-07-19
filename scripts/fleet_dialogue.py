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

import fleet_safe_paths
from fleet_identity import validate as validate_identity
from fleet_leases import closing_path, coordinator
from fleet_ledger import LedgerError, append_record, latest_event, read_records


SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 1024 * 1024
MESSAGE_KINDS = {"proposal", "challenge", "rebuttal", "revision", "verification"}
SAFE_FEATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
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
CONTROLLED_PUBLICATION_FIELDS = (
    "kind",
    "recipient",
    "source_instance",
    "source_run_id",
    "reply_to",
    "payload_sha256",
)


class DialogueError(RuntimeError):
    pass


class DialogueConflict(DialogueError):
    pass


class DialogueClosing(DialogueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _feature(value: str) -> str:
    if not isinstance(value, str) or not SAFE_FEATURE.fullmatch(value):
        raise DialogueError("invalid feature")
    return value


def manifest_path(runs_dir: Path, feature: str) -> Path:
    return runs_dir / f"fleet-{_feature(feature)}.manifest"


def ledger_path(runs_dir: Path, feature: str) -> Path:
    return runs_dir / f"fleet-{_feature(feature)}.dialogue.jsonl"


def store_path(runs_dir: Path, feature: str) -> Path:
    return runs_dir / "dialogue" / _feature(feature) / "payloads"


def payload_path(runs_dir: Path, feature: str, digest: str) -> Path:
    return runs_dir / _payload_relative(feature, digest)


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
    feature = _feature(feature)
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
    feature = _feature(feature)
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


def _validated_messages(
    records: list[dict[str, Any]], feature: str
) -> list[dict[str, Any]]:
    feature = _feature(feature)
    messages: list[dict[str, Any]] = []
    message_ids: set[str] = set()
    idempotency_keys: set[str] = set()
    for message in records:
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


def _load_messages_rooted(
    root: Path, ledger_leaf: str, feature: str
) -> list[dict[str, Any]]:
    feature = _feature(feature)
    allowed = {"dialogue.jsonl", f"fleet-{feature}.dialogue.jsonl"}
    if ledger_leaf not in allowed or Path(ledger_leaf).name != ledger_leaf:
        raise DialogueError("dialogue ledger is outside its selected root")
    try:
        records = read_records(root / ledger_leaf, runs_dir=root)
    except LedgerError as exc:
        raise DialogueError(f"cannot read dialogue ledger: {exc}") from exc
    return _validated_messages(records, feature)


def load_messages_from_path(path: Path, feature: str) -> list[dict[str, Any]]:
    feature = _feature(feature)
    path = Path(path)
    root = path.parent
    if path != root / path.name:
        raise DialogueError("dialogue ledger is outside its selected root")
    return _load_messages_rooted(root, path.name, feature)


def load_messages(runs_dir: Path, feature: str) -> list[dict[str, Any]]:
    feature = _feature(feature)
    return _load_messages_rooted(
        runs_dir, ledger_path(runs_dir, feature).name, feature
    )


def _read_bounded_regular_file(
    path: Path, *, expected_size: int | None = None
) -> bytes:
    """Read an explicitly selected external source without following its leaf."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DialogueError(f"cannot open dialogue payload source: {path}") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise DialogueError(
                f"dialogue payload source is not a regular file: {path}"
            )
        if metadata.st_size <= 0:
            raise DialogueError("dialogue payload is empty")
        if metadata.st_size > MAX_PAYLOAD_BYTES:
            raise DialogueError(
                f"dialogue payload exceeds {MAX_PAYLOAD_BYTES} bytes: "
                f"{metadata.st_size}"
            )
        payload = b""
        while len(payload) <= MAX_PAYLOAD_BYTES:
            chunk = os.read(
                fd, min(65536, MAX_PAYLOAD_BYTES + 1 - len(payload))
            )
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


def _payload_store_relative(feature: str, *, archived: bool = False) -> Path:
    feature = _feature(feature)
    if archived:
        return Path("dialogue") / "payloads"
    return Path("dialogue") / feature / "payloads"


def _payload_relative(feature: str, digest: str, *, archived: bool = False) -> Path:
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise DialogueError("invalid dialogue payload digest")
    return _payload_store_relative(feature, archived=archived) / digest


def _payload_directory_modes(store_relative: Path) -> tuple[int, ...]:
    """Keep public traversal directories read-only and the payload store private."""

    return tuple(0o755 for _ in store_relative.parts[:-1]) + (0o700,)


def _read_rooted_payload(
    root: Path,
    store_relative: Path,
    message: dict[str, Any],
) -> tuple[Path, bytes]:
    digest = str(message["payload_sha256"])
    if not SHA256.fullmatch(digest):
        raise DialogueError("invalid dialogue payload digest")
    relative = store_relative / digest
    try:
        with fleet_safe_paths.RootedFS(root) as rooted:
            payload = rooted.read_regular(
                relative,
                directory_modes=_payload_directory_modes(store_relative),
                file_mode=0o600,
                max_bytes=MAX_PAYLOAD_BYTES,
            )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise DialogueError(f"unsafe dialogue payload path: {exc}") from exc
    if len(payload) != int(message["payload_bytes"]):
        raise DialogueError("dialogue payload size does not match envelope")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise DialogueError("dialogue payload hash does not match envelope")
    return root / relative, payload


def _source_payload(
    runs_dir: Path,
    feature: str,
    source_instance: str,
    source_run_id: str,
) -> bytes:
    feature = _feature(feature)
    if not SAFE_RUN_ID.fullmatch(source_run_id):
        raise DialogueError(f"invalid source run_id: {source_run_id}")
    try:
        event = latest_event(
            runs_dir / f"fleet-{feature}.ledger.jsonl",
            run_id=source_run_id,
            instance=source_instance,
            runs_dir=runs_dir,
        )
    except LedgerError as exc:
        raise DialogueError(f"cannot read source lifecycle ledger: {exc}") from exc
    if not event:
        raise DialogueError("source run is absent from the lifecycle ledger")
    if event.get("feature") != feature or event.get("instance") != source_instance:
        raise DialogueError("source run identity does not match the active fleet")
    if event.get("status") != "succeeded":
        raise DialogueError("source run is not terminal succeeded")
    recorded = event.get("result_file")
    if not isinstance(recorded, str) or not recorded:
        raise DialogueError("source run has no durable result_file")
    relative = Path("results") / feature / f"{source_run_id}.txt"
    recorded_path = Path(recorded)
    try:
        recorded_root = recorded_path.parents[2]
    except IndexError as exc:
        raise DialogueError(
            "source result_file is outside the fleet result store"
        ) from exc
    if recorded_path != recorded_root / relative:
        raise DialogueError("source result_file is outside the fleet result store")
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            if fleet_safe_paths.canonical_root(recorded_root) != rooted.root:
                raise DialogueError(
                    "source result_file is outside the fleet result store"
                )
            payload = rooted.read_regular(
                relative,
                directory_modes=(0o755, 0o700),
                file_mode=0o600,
                max_bytes=MAX_PAYLOAD_BYTES,
            )
            rooted.assert_root_binding()
    except fleet_safe_paths.SafePathError as exc:
        raise DialogueError(
            f"source result_file is unsafe or symlinked: {exc}"
        ) from exc
    if not payload:
        raise DialogueError("dialogue payload is empty")
    return payload


def _write_payload(
    runs_dir: Path, feature: str, digest: str, payload: bytes
) -> Path:
    relative = _payload_relative(feature, digest)
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            path = rooted.atomic_write(
                relative,
                payload,
                directory_modes=_payload_directory_modes(relative.parent),
                file_mode=0o600,
            )
            rooted.assert_root_binding()
            return path
    except fleet_safe_paths.SafePathError as exc:
        raise DialogueError(f"unsafe dialogue payload publication: {exc}") from exc


def _verified_payload(runs_dir: Path, feature: str, message: dict[str, Any]) -> tuple[Path, bytes]:
    feature = _feature(feature)
    return _read_rooted_payload(
        runs_dir, _payload_store_relative(feature), message
    )


def _verified_payload_from_store(
    store: Path, message: dict[str, Any]
) -> tuple[Path, bytes]:
    feature = _feature(str(message.get("feature") or ""))
    store = Path(store)
    live_relative = _payload_store_relative(feature)
    archived_relative = _payload_store_relative(feature, archived=True)
    candidates: list[tuple[Path, Path]] = []
    try:
        candidates.append((store.parents[2], live_relative))
        candidates.append((store.parents[1], archived_relative))
    except IndexError:
        pass
    for root, relative in candidates:
        if store == root / relative:
            return _read_rooted_payload(root, relative, message)
    raise DialogueError("dialogue payload store is outside its selected root")


def verify_storage(
    dialogue_ledger: Path,
    payload_store: Path,
    *,
    feature: str,
    instances: set[str] | None = None,
) -> dict[str, int]:
    feature = _feature(feature)
    dialogue_ledger = Path(dialogue_ledger)
    payload_store = Path(payload_store)
    root = dialogue_ledger.parent
    live_store = _payload_store_relative(feature)
    archived_store = _payload_store_relative(feature, archived=True)
    if (
        dialogue_ledger == root / f"fleet-{feature}.dialogue.jsonl"
        and payload_store == root / live_store
    ):
        ledger_leaf = dialogue_ledger.name
        store_relative = live_store
    elif (
        dialogue_ledger == root / "dialogue.jsonl"
        and payload_store == root / archived_store
    ):
        ledger_leaf = dialogue_ledger.name
        store_relative = archived_store
    else:
        raise DialogueError("dialogue storage is outside its selected root")
    messages = _load_messages_rooted(root, ledger_leaf, feature)
    verified: set[str] = set()
    total_bytes = 0
    for message in messages:
        if instances is not None and (
            message["recipient"] not in instances
            or message["source_instance"] not in instances
        ):
            raise DialogueError("dialogue message references an unknown manifest instance")
        if message["payload_sha256"] not in verified:
            _, payload = _read_rooted_payload(root, store_relative, message)
            verified.add(message["payload_sha256"])
            total_bytes += len(payload)
    return {
        "messages": len(messages),
        "payloads": len(verified),
        "payload_bytes": total_bytes,
    }


def _entry_exists(path: Path, *, where: str) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DialogueError(f"cannot inspect {where}") from exc
    return True


def _controller_publication_state_locked(
    runs_dir: Path, feature: str
) -> dict[str, Any] | None:
    """Resolve the one durable controller allowed to authorize publication.

    Imports are intentionally local: the controller modules depend on this
    transport module, while publication is called only after all imports have
    completed and while the fleet coordinator lock is already held.
    """

    fdp2_path = runs_dir / f"fleet-{feature}.dialogue-control.jsonl"
    fdp3_path = runs_dir / f"fleet-{feature}.assurance-control.jsonl"
    has_fdp2 = _entry_exists(fdp2_path, where="FDP-2 publication state")
    has_fdp3 = _entry_exists(fdp3_path, where="FDP-3 publication state")
    if not has_fdp2 and not has_fdp3:
        return None

    states: list[dict[str, Any]] = []
    if has_fdp2:
        import fleet_dialogue_controller as fdp2

        try:
            state = fdp2.publication_state_locked(runs_dir, feature)
        except fdp2.ControllerError as exc:
            raise DialogueError(f"cannot validate FDP-2 publication state: {exc}") from exc
        if state is None:
            raise DialogueError("FDP-2 publication state disappeared while locked")
        states.append(state)
    if has_fdp3:
        import fleet_assurance_controller as fdp3

        try:
            state = fdp3.publication_state_locked(runs_dir, feature)
        except fdp3.AssuranceError as exc:
            raise DialogueError(f"cannot validate FDP-3 publication state: {exc}") from exc
        if state is None:
            raise DialogueError("FDP-3 publication state disappeared while locked")
        states.append(state)

    active = [state for state in states if state["active"]]
    if len(active) > 1:
        raise DialogueConflict("multiple active controllers make publication authority ambiguous")
    if not active:
        raise DialogueConflict(
            "controller state exists but no active controller authorizes publication"
        )
    return active[0]


def _authorize_publication_locked(
    runs_dir: Path,
    feature: str,
    request_identity: dict[str, Any],
) -> None:
    state = _controller_publication_state_locked(runs_dir, feature)
    if state is None:
        return
    expected = state["expected"]
    if not isinstance(expected, dict) or expected.get("type") != "message":
        raise DialogueConflict(
            f"{state['controller']} controller {state['controller_id']} is not awaiting publication"
        )
    mismatches = [
        field
        for field in CONTROLLED_PUBLICATION_FIELDS
        if request_identity.get(field) != expected.get(field)
    ]
    if mismatches:
        stage = expected.get("stage")
        raise DialogueConflict(
            f"{state['controller']} controller {state['controller_id']} stage {stage} "
            "does not authorize publication fields: "
            + ", ".join(mismatches)
        )


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
    feature = _feature(feature)
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
        _authorize_publication_locked(
            runs_dir,
            feature,
            request_identity,
        )
        _write_payload(runs_dir, feature, digest, payload)
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
            runs_dir=runs_dir,
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
    feature = _feature(feature)
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
    feature = _feature(feature)
    messages = query(runs_dir, feature=feature)
    message = next((item for item in messages if item["message_id"] == message_id), None)
    if message is None:
        raise DialogueError(f"unknown dialogue message: {message_id}")
    return message


def read_message(runs_dir: Path, *, feature: str, message_id: str) -> bytes:
    feature = _feature(feature)
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
    feature = _feature(feature)
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
