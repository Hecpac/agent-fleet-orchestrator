#!/usr/bin/env python3
"""Lifecycle and client helpers for the authenticated Fleet Control socket."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from typing import Any

import fleet_mission_state as mission_state


ROOT = Path(__file__).resolve().parents[1]
MAX_REPLY_BYTES = 2_000_000
_LIVE_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}


class ControlServiceError(RuntimeError):
    """The Fleet Control service cannot be authenticated or reconciled."""


def send_request(path: Path, value: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > 2_000_000:
        raise ControlServiceError("control request exceeds maximum size")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(payload)
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            chunk = connection.recv(min(65536, MAX_REPLY_BYTES + 1 - len(chunks)))
            if not chunk:
                raise ControlServiceError("control socket closed before a complete reply")
            chunks.extend(chunk)
            if len(chunks) > MAX_REPLY_BYTES:
                raise ControlServiceError("control reply exceeds maximum size")
    try:
        result = json.loads(chunks)
    except json.JSONDecodeError as exc:
        raise ControlServiceError("control socket returned invalid JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        raise ControlServiceError("control socket returned an invalid envelope")
    return result


class ControlLifecycle:
    def __init__(self, runs_dir: Path, mission_id: str) -> None:
        self.runs_dir = runs_dir.resolve()
        self.mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
        self.root = mission_state.mission_root(self.runs_dir, self.mission_id) / "control"
        default_socket_root = Path(f"/tmp/fleet-control-{os.geteuid()}")
        self.socket_root = Path(
            os.environ.get("FLEET_CONTROL_SOCKET_DIR", str(default_socket_root))
        ).expanduser().resolve()
        self.socket_path = self.socket_root / f"{self.mission_id}.sock"
        self.lifecycle_path = self.root / "lifecycle.json"
        self.lock_path = self.root / "service.lock"
        self.process: subprocess.Popen[bytes] | None = None

    @contextmanager
    def _lock(self):
        mission_state.ensure_private_directory(self.root)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read_lifecycle(self) -> dict[str, Any]:
        try:
            value = json.loads(self.lifecycle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControlServiceError(f"cannot read control lifecycle: {exc}") from exc
        if not isinstance(value, dict) or value.get("mission_id") != self.mission_id:
            raise ControlServiceError("control lifecycle mission identity mismatch")
        return value

    def health(self) -> dict[str, Any]:
        try:
            reply = send_request(self.socket_path, {"operation": "health"})
        except (OSError, ControlServiceError) as exc:
            raise ControlServiceError(f"Fleet Control health failed: {exc}") from exc
        result = reply.get("result") if reply.get("ok") else None
        if not isinstance(result, dict) or any(
            (
                result.get("status") != "ok",
                result.get("mission_id") != self.mission_id,
                result.get("protocol") != "fleet-control-unix-v1",
            )
        ):
            raise ControlServiceError("Fleet Control health identity mismatch")
        return result

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            raise ControlServiceError("cannot verify control service process owner") from exc

    def start(self) -> dict[str, Any]:
        with self._lock():
            return self._start_locked()

    def _start_locked(self) -> dict[str, Any]:
        if self.lifecycle_path.exists():
            existing = self._read_lifecycle()
            if existing.get("stopped_at") is not None:
                raise ControlServiceError("Fleet Control was already stopped for this mission")
            try:
                return {"started": False, "health": self.health(), "lifecycle": existing}
            except ControlServiceError:
                pid = int(existing.get("pid", 0))
                if pid > 0 and self._pid_alive(pid):
                    raise
                self.lifecycle_path.unlink(missing_ok=True)
                self.socket_path.unlink(missing_ok=True)
        mission_state.ensure_private_directory(self.socket_root)
        if len(os.fsencode(self.socket_path)) >= 100:
            raise ControlServiceError("FLEET_CONTROL_SOCKET_DIR produces an unsafe AF_UNIX path")
        stdout_path = self.root / "service.stdout.log"
        stderr_path = self.root / "service.stderr.log"
        command = [
            "python3",
            str(ROOT / "scripts" / "fleet_mcp.py"),
            "--runs-dir",
            str(self.runs_dir),
            "--mission-id",
            self.mission_id,
            "--socket",
            str(self.socket_path),
        ]
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=os.environ.copy(),
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        self.process = process
        _LIVE_PROCESSES[process.pid] = process
        lifecycle = {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "pid": process.pid,
            "socket": str(self.socket_path),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "stopped_at": None,
        }
        mission_state.atomic_write(
            self.lifecycle_path, mission_state.canonical_bytes(lifecycle) + b"\n"
        )
        deadline = time.monotonic() + 8
        last_error = "socket not ready"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                last_error = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
                break
            if self.socket_path.exists():
                try:
                    health = self.health()
                    return {"started": True, "health": health, "lifecycle": lifecycle}
                except ControlServiceError as exc:
                    last_error = str(exc)
            time.sleep(0.05)
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        _LIVE_PROCESSES.pop(process.pid, None)
        self.lifecycle_path.unlink(missing_ok=True)
        self.socket_path.unlink(missing_ok=True)
        raise ControlServiceError(f"Fleet Control failed to start: {last_error}")

    def stop(self) -> dict[str, Any]:
        with self._lock():
            return self._stop_locked()

    def _stop_locked(self) -> dict[str, Any]:
        lifecycle = self._read_lifecycle()
        if lifecycle.get("stopped_at") is not None:
            return {"stopped": False, "lifecycle": lifecycle}
        self.health()
        pid = int(lifecycle["pid"])
        if not self._pid_alive(pid):
            raise ControlServiceError("Fleet Control process disappeared before stop")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        process = self.process or _LIVE_PROCESSES.get(pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                break
            if not self._pid_alive(pid):
                break
            time.sleep(0.05)
        if process is not None:
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        if self._pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process is not None:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if self._pid_alive(pid):
            raise ControlServiceError(
                "Fleet Control did not stop before the shutdown deadline"
            )
        _LIVE_PROCESSES.pop(pid, None)
        self.socket_path.unlink(missing_ok=True)
        lifecycle["stopped_at"] = datetime.now(timezone.utc).isoformat()
        mission_state.atomic_write(
            self.lifecycle_path, mission_state.canonical_bytes(lifecycle) + b"\n"
        )
        return {"stopped": True, "lifecycle": lifecycle}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--mission-id", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start")
    sub.add_parser("health")
    sub.add_parser("stop")
    request = sub.add_parser("request")
    request.add_argument("--json", required=True)
    args = parser.parse_args(argv)
    try:
        lifecycle = ControlLifecycle(Path(args.runs_dir), args.mission_id)
        if args.command == "start":
            value = lifecycle.start()
        elif args.command == "health":
            value = lifecycle.health()
        elif args.command == "stop":
            value = lifecycle.stop()
        else:
            payload = json.loads(args.json)
            if not isinstance(payload, dict):
                raise ControlServiceError("request JSON must be an object")
            value = send_request(lifecycle.socket_path, payload)
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return 0
    except (ControlServiceError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"fleet-control-service: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
