#!/usr/bin/env python3
"""Translate Kimi Wire records into durable Fleet hook evidence.

cmux does not currently ship a native Kimi hook integration.  This bridge
tails one surface-bound Kimi ``wire.jsonl`` and records only TurnBegin/TurnEnd
metadata in a repo-owned event file.  Prompts and model output remain in Kimi's
private transcript and are read later by the provider evidence adapter.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
from typing import Any

import fleet_json


SUPPORTED_WIRE_PROTOCOLS = {"1.2", "1.3", "1.10"}


class KimiBridgeError(RuntimeError):
    pass


def _canonical_uuid(value: str, field: str) -> str:
    try:
        canonical = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise KimiBridgeError(f"{field} must be a canonical UUID") from exc
    if canonical != value.lower():
        raise KimiBridgeError(f"{field} must be a canonical UUID")
    return canonical


def _safe_directory(path: Path, field: str, *, create: bool = False) -> Path:
    if path.is_symlink():
        raise KimiBridgeError(f"{field} must not be a symlink")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise KimiBridgeError(f"{field} is unavailable") from exc
    if not resolved.is_dir():
        raise KimiBridgeError(f"{field} must be a directory")
    return resolved


def wire_path(share_dir: Path, work_dir: Path, session_id: str) -> Path:
    """Resolve the surface's wire.jsonl under an ISOLATED kimi-code home.

    kimi-code (>= 0.28) mints its own session id and lays sessions out as
    ``sessions/wd_cwd_<sha256(work_dir)[:12]>/session_<id>/agents/main/``.
    The fleet cannot choose the id (the legacy ``--session`` flag now only
    resumes), so the bridge discovers it — safely, because ``share_dir`` is
    a per-surface ``KIMI_CODE_HOME``: more than one session directory means
    the isolation broke, and the bridge fails closed instead of guessing.
    The caller-supplied ``session_id`` (surface-derived) remains the stable
    identity used in emitted event ids; it never names the path.
    """
    del session_id
    canonical_work_dir = work_dir.resolve(strict=True)
    legacy_hash = hashlib.md5(str(canonical_work_dir).encode("utf-8")).hexdigest()
    legacy_root = share_dir / "sessions" / legacy_hash
    work_hash = hashlib.sha256(str(canonical_work_dir).encode("utf-8")).hexdigest()[:12]
    session_root = share_dir / "sessions" / f"wd_cwd_{work_hash}"
    if not session_root.is_dir():
        if legacy_root.is_dir():
            raise KimiBridgeError(
                "legacy kimi-cli session layout found; the fleet requires kimi-code"
            )
        raise KimiBridgeError("Kimi session root has not been created yet")
    candidates = sorted(
        entry
        for entry in session_root.iterdir()
        if entry.is_dir() and entry.name.startswith("session_")
    )
    if not candidates:
        raise KimiBridgeError("Kimi session root has not been created yet")
    if len(candidates) > 1:
        raise KimiBridgeError(
            "multiple Kimi sessions in one isolated home make evidence ambiguous"
        )
    return candidates[0] / "agents" / "main" / "wire.jsonl"


def resolve_wire_path(
    share_dir: Path,
    work_dir: Path,
    session_id: str,
    *,
    timeout_seconds: float = 120.0,
    poll_seconds: float = 0.25,
) -> Path:
    """Wait for the TUI to mint its session before binding the transcript."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return wire_path(share_dir, work_dir, session_id)
        except KimiBridgeError as error:
            if "not been created yet" not in str(error):
                raise
            if time.monotonic() >= deadline:
                raise
        time.sleep(poll_seconds)


def _read_object(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return default
    except OSError as exc:
        raise KimiBridgeError(f"cannot read {path.name}") from exc
    try:
        value = fleet_json.loads(raw)
    except fleet_json.FleetJSONError as exc:
        raise KimiBridgeError(f"{path.name} contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise KimiBridgeError(f"{path.name} must contain one JSON object")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = fleet_json.canonical_bytes(value) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def record_session(
    hook_dir: Path,
    *,
    session_id: str,
    workspace_id: str,
    surface_id: str,
    transcript_path: Path,
    provider: str,
    model: str,
) -> None:
    session_file = hook_dir / "kimi-hook-sessions.json"
    lock_path = hook_dir / ".kimi-hook-sessions.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with os.fdopen(lock_fd, "r+b", closefd=True) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            document = _read_object(session_file, {"sessions": {}})
            sessions = document.get("sessions")
            if not isinstance(sessions, dict):
                raise KimiBridgeError("kimi-hook-sessions.json has invalid sessions")
            sessions[session_id] = {
                "sessionId": session_id,
                "workspaceId": workspace_id.upper(),
                "surfaceId": surface_id.upper(),
                "transcriptPath": str(transcript_path),
                "provider": provider,
                "model": model,
                "updatedAt": int(time.time() * 1000),
            }
            _atomic_json(session_file, document)
    except OSError as exc:
        raise KimiBridgeError("cannot update Kimi session binding") from exc


def _occurred_at(timestamp: Any) -> str:
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise KimiBridgeError("Kimi Wire record has an invalid timestamp")
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError) as exc:
        raise KimiBridgeError("Kimi Wire record timestamp is out of range") from exc


def hook_event(
    *,
    record_index: int,
    record: dict[str, Any],
    session_id: str,
    workspace_id: str,
    surface_id: str,
) -> dict[str, Any] | None:
    message = record.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("payload"), dict):
        raise KimiBridgeError("Kimi Wire record has an invalid message envelope")
    message_type = message.get("type")
    if message_type == "TurnBegin":
        name, phase = "agent.hook.UserPromptSubmit", "received"
    elif message_type == "TurnEnd":
        name, phase = "agent.hook.Stop", "completed"
    else:
        return None
    kind = "submit" if message_type == "TurnBegin" else "stop"
    return {
        "type": "event",
        "id": f"kimi-{session_id}-{record_index}-{kind}",
        "boot_id": f"kimi-{session_id}",
        "seq": record_index,
        "category": "agent.hook",
        "name": name,
        "source": "kimi",
        "workspace_id": workspace_id.upper(),
        "surface_id": surface_id.upper(),
        "occurred_at": _occurred_at(record.get("timestamp")),
        "payload": {
            "_source": "kimi",
            "phase": phase,
            "session_id": f"kimi-{session_id}",
        },
    }


def _existing_event_ids(events_file: Path) -> set[str]:
    try:
        lines = events_file.read_bytes().splitlines()
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise KimiBridgeError("cannot read Kimi event evidence") from exc
    result: set[str] = set()
    for raw in lines:
        if not raw.strip():
            continue
        try:
            event = fleet_json.loads(raw)
        except fleet_json.FleetJSONError as exc:
            raise KimiBridgeError("Kimi event evidence contains invalid JSON") from exc
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            raise KimiBridgeError("Kimi event evidence contains an invalid event")
        result.add(event["id"])
    return result


def append_event(events_file: Path, event: dict[str, Any]) -> None:
    payload = fleet_json.canonical_bytes(event) + b"\n"
    fd = os.open(events_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def acquire_bridge_lock(state_dir: Path) -> int:
    lock_path = state_dir / ".bridge.lock"
    if lock_path.is_symlink():
        raise KimiBridgeError("bridge lock must not be a symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = -1
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        if lock_fd >= 0:
            os.close(lock_fd)
        raise KimiBridgeError("another Kimi bridge owns this surface") from exc
    except OSError as exc:
        if lock_fd >= 0:
            os.close(lock_fd)
        raise KimiBridgeError("cannot acquire Kimi bridge lock") from exc
    return lock_fd


def _notify(surface_id: str, session_id: str) -> None:
    try:
        subprocess.run(
            [
                "cmux",
                "notify",
                "--surface",
                surface_id,
                "--title",
                "Kimi turn completed",
                "--body",
                f"session=kimi-{session_id}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            env={**os.environ, "CMUX_QUIET": "1"},
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def watch(args: argparse.Namespace) -> int:
    session_id = _canonical_uuid(args.session_id, "session_id")
    workspace_id = _canonical_uuid(args.workspace_id, "workspace_id")
    surface_id = _canonical_uuid(args.surface_id, "surface_id")
    share_dir = _safe_directory(Path(args.share_dir), "share_dir", create=True)
    hook_dir = _safe_directory(Path(args.hook_dir), "hook_dir", create=True)
    state_dir = _safe_directory(Path(args.events_file).parent, "state_dir", create=True)
    _bridge_lock_fd = acquire_bridge_lock(state_dir)
    events_file = state_dir / Path(args.events_file).name
    if events_file.is_symlink() or (events_file.exists() and not events_file.is_file()):
        raise KimiBridgeError("events_file must be a regular non-symlink file")
    transcript = resolve_wire_path(share_dir, Path(args.work_dir), session_id)
    record_session(
        hook_dir,
        session_id=session_id,
        workspace_id=workspace_id,
        surface_id=surface_id,
        transcript_path=transcript,
        provider=args.provider,
        model=args.model,
    )
    seen = _existing_event_ids(events_file)
    processed_lines = 0
    # Keep the descriptor alive for the entire watcher lifetime. The OS releases
    # the advisory lock even when the process exits after an unhandled error.
    while True:
        try:
            raw_content = transcript.read_bytes()
        except FileNotFoundError:
            time.sleep(args.poll_interval)
            continue
        except OSError as exc:
            raise KimiBridgeError("cannot read Kimi Wire transcript") from exc
        raw_lines = raw_content.splitlines()
        if raw_content and not raw_content.endswith(b"\n"):
            raw_lines = raw_lines[:-1]
        for raw in raw_lines[processed_lines:]:
            processed_lines += 1
            if not raw.strip():
                continue
            try:
                value = fleet_json.loads(raw)
            except fleet_json.FleetJSONError as exc:
                raise KimiBridgeError("Kimi Wire transcript contains invalid JSON") from exc
            if not isinstance(value, dict):
                raise KimiBridgeError("Kimi Wire transcript row is not an object")
            if value.get("type") == "metadata":
                if value.get("protocol_version") not in SUPPORTED_WIRE_PROTOCOLS:
                    raise KimiBridgeError("unsupported Kimi Wire protocol")
                continue
            event = hook_event(
                record_index=processed_lines,
                record=value,
                session_id=session_id,
                workspace_id=workspace_id,
                surface_id=surface_id,
            )
            if event is None or event["id"] in seen:
                continue
            append_event(events_file, event)
            seen.add(event["id"])
            record_session(
                hook_dir,
                session_id=session_id,
                workspace_id=workspace_id,
                surface_id=surface_id,
                transcript_path=transcript,
                provider=args.provider,
                model=args.model,
            )
            if event["name"] == "agent.hook.Stop":
                _notify(surface_id, session_id)
        time.sleep(args.poll_interval)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--share-dir", required=True)
    value.add_argument("--work-dir", required=True)
    value.add_argument("--session-id", required=True)
    value.add_argument("--workspace-id", required=True)
    value.add_argument("--surface-id", required=True)
    value.add_argument("--hook-dir", required=True)
    value.add_argument("--events-file", required=True)
    value.add_argument("--provider", required=True)
    value.add_argument("--model", required=True)
    value.add_argument("--poll-interval", type=float, default=0.1)
    return value


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if not 0.02 <= args.poll_interval <= 5:
            raise KimiBridgeError("poll interval is out of range")
        return watch(args)
    except (KimiBridgeError, OSError) as exc:
        print(f"kimi-hook-bridge: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
