#!/usr/bin/env python3
"""Own one Kimi hook bridge and publish fail-closed lifecycle evidence."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import fleet_kimi_state


def now_ms() -> int:
    return int(time.time() * 1000)


def stderr_evidence(stream) -> tuple[str, str]:
    stream.seek(0)
    digest = hashlib.sha256()
    tail = b""
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        tail = (tail + chunk)[-1024:]
    summary = tail.decode("utf-8", errors="ignore").replace("\x00", "")
    while len(summary.encode("utf-8")) > 1024:
        summary = summary[1:]
    return digest.hexdigest(), summary


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--state-root", required=True)
    value.add_argument("--bridge", required=True)
    for name in (
        "surface-id",
        "workspace-id",
        "mission-id",
        "generation-id",
        "launch-id",
    ):
        value.add_argument(f"--{name}", required=True)
    value.add_argument("bridge_args", nargs=argparse.REMAINDER)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    bridge_args = list(args.bridge_args)
    if bridge_args[:1] == ["--"]:
        bridge_args = bridge_args[1:]
    if not bridge_args:
        print("kimi-bridge-supervisor: bridge command is empty", file=sys.stderr)
        return 2
    command = [args.bridge, *bridge_args]
    started_at = now_ms()
    child: subprocess.Popen[bytes] | None = None
    published_ready = False
    with tempfile.TemporaryFile() as bridge_stderr:
        try:
            child = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=bridge_stderr,
            )
            while child.poll() is None:
                if not published_ready:
                    try:
                        hook_index = bridge_args.index("--hook-dir") + 1
                        binding = fleet_kimi_state.read_session_binding(
                            Path(bridge_args[hook_index]), args.surface_id
                        )
                        if not binding or any(
                            (
                                str(binding.get("surfaceId") or "").upper()
                                != args.surface_id.upper(),
                                str(binding.get("workspaceId") or "").upper()
                                != args.workspace_id.upper(),
                                binding.get("missionId") != args.mission_id,
                                binding.get("generationId") != args.generation_id,
                            )
                        ):
                            raise fleet_kimi_state.KimiStateError(
                                "bridge session binding is absent or drifted"
                            )
                        fleet_kimi_state.publish_bridge_status(
                            Path(args.state_root),
                            fleet_kimi_state.bridge_document(
                                surface_id=args.surface_id,
                                workspace_id=args.workspace_id,
                                mission_id=args.mission_id,
                                generation_id=args.generation_id,
                                launch_id=args.launch_id,
                                pid=child.pid,
                                started_at=started_at,
                                status="ready",
                            ),
                        )
                        published_ready = True
                    except (ValueError, fleet_kimi_state.KimiStateError):
                        pass
                time.sleep(0.05)
            exit_code = child.returncode if child.returncode is not None else 2
            stderr_sha256, stderr_tail = stderr_evidence(bridge_stderr)
            fleet_kimi_state.publish_bridge_status(
                Path(args.state_root),
                fleet_kimi_state.bridge_document(
                    surface_id=args.surface_id,
                    workspace_id=args.workspace_id,
                    mission_id=args.mission_id,
                    generation_id=args.generation_id,
                    launch_id=args.launch_id,
                    pid=child.pid,
                    started_at=started_at,
                    status="exited",
                    ended_at=now_ms(),
                    exit_code=exit_code,
                    stderr_sha256=stderr_sha256,
                    stderr_tail=stderr_tail,
                ),
            )
            return exit_code
        except (OSError, fleet_kimi_state.KimiStateError) as exc:
            print(f"kimi-bridge-supervisor: {exc}", file=sys.stderr)
            return 2
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
