#!/usr/bin/env python3
"""Lifecycle and client helpers for the authenticated Fleet Control socket."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import struct
import sys
import time
from typing import Any
import uuid

from fleet_control import FleetControl
import fleet_control_runtime as control_runtime
import fleet_json
import fleet_mission_state as mission_state
import fleet_safe_paths


ROOT = Path(__file__).resolve().parents[1]
MAX_REPLY_BYTES = 2_000_000
_LIVE_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
_STAGING_LEAF = re.compile(r"^\.fc-[0-9a-f]{32}-[0-9]{3}\.stage$")


class ControlServiceError(RuntimeError):
    """The Fleet Control service cannot be authenticated or reconciled."""


def _gate_exec(argv: list[str]) -> int:
    """Exec the socket server only after its lifecycle PID is durable.

    The read side is inherited by the child and the write side remains owned
    only by the controller.  If the controller dies before publishing the
    lifecycle, EOF makes the child exit instead of becoming an unrecorded
    daemon.
    """

    if len(argv) < 2:
        return 125
    try:
        gate_fd = int(argv[0])
    except ValueError:
        return 125
    if gate_fd < 0:
        return 125
    command = argv[1:]
    try:
        release = os.read(gate_fd, 1)
    except OSError:
        return 125
    finally:
        try:
            os.close(gate_fd)
        except OSError:
            pass
    if release != b"\x01":
        return 125
    os.execvpe(command[0], command, os.environ.copy())
    return 125


def instance_socket_name(mission_id: str, instance: str) -> str:
    """Return a short deterministic AF_UNIX name without trusting instance text."""
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    if not isinstance(instance, str) or not instance or instance == "lead":
        raise ControlServiceError("invalid specialist control endpoint instance")
    digest = hashlib.sha256(instance.encode("utf-8")).hexdigest()[:16]
    return f"{mission_id}.{digest}.sock"


def default_socket_root(runs_dir: Path) -> Path:
    """Namespace fallback endpoints by their exact durable mission store."""

    digest = hashlib.sha256(os.fsencode(runs_dir)).hexdigest()[:12]
    return Path(f"/tmp/fc-{os.geteuid()}-{digest}")


def endpoint_binding_sha256(paths: dict[str, Path]) -> str:
    return mission_state.sha256(
        {instance: str(path) for instance, path in sorted(paths.items())}
    )


def _peer_pid(connection: socket.socket) -> int:
    if hasattr(socket, "SO_PEERCRED"):
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        pid, _, _ = struct.unpack("3i", raw)
        return int(pid)
    if sys.platform == "darwin":
        # sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=2.  Python does not expose
        # these constants on every macOS build.
        raw = connection.getsockopt(0, 2, 4)
        return int(struct.unpack("i", raw)[0])
    raise ControlServiceError("platform cannot authenticate control server pid")


def send_request(
    path: Path,
    value: dict[str, Any],
    *,
    timeout: float = 5.0,
    expected_peer_pid: int | None = None,
) -> dict[str, Any]:
    try:
        payload = fleet_json.canonical_bytes(value) + b"\n"
    except fleet_json.FleetJSONError as exc:
        raise ControlServiceError("control request is not canonical JSON") from exc
    if len(payload) > 2_000_000:
        raise ControlServiceError("control request exceeds maximum size")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        if expected_peer_pid is not None:
            if (
                isinstance(expected_peer_pid, bool)
                or not isinstance(expected_peer_pid, int)
                or expected_peer_pid <= 1
                or _peer_pid(connection) != expected_peer_pid
            ):
                raise ControlServiceError("control server kernel pid mismatch")
        connection.sendall(payload)
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            chunk = connection.recv(min(65536, MAX_REPLY_BYTES + 1 - len(chunks)))
            if not chunk:
                raise ControlServiceError(
                    "control socket closed before a complete reply"
                )
            chunks.extend(chunk)
            if len(chunks) > MAX_REPLY_BYTES:
                raise ControlServiceError("control reply exceeds maximum size")
    try:
        result = fleet_json.loads(chunks)
    except fleet_json.FleetJSONError as exc:
        raise ControlServiceError("control socket returned invalid JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        raise ControlServiceError("control socket returned an invalid envelope")
    return result


class ControlLifecycle:
    def __init__(
        self, runs_dir: Path, mission_id: str, *, preset: str | None = None
    ) -> None:
        try:
            self.runs_dir = fleet_safe_paths.canonical_root(runs_dir)
        except fleet_safe_paths.SafePathError as exc:
            raise ControlServiceError("unsafe Fleet Control runs root") from exc
        self.mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
        control = FleetControl(self.runs_dir, self.mission_id, preset=preset)
        self.preset = control.preset
        main_preset = control.compiled["resolved"]["preset"]
        # Assurance is a second, explicitly approved service generation.  A
        # stopped control lifecycle is immutable, so it must never be deleted
        # or silently reused to launch a different preset.
        control_namespace = (
            "control" if self.preset == main_preset else "control-assured"
        )
        self.control_relative = Path("missions") / self.mission_id / control_namespace
        self.root = self.runs_dir / self.control_relative
        configured_socket_root = os.environ.get("FLEET_CONTROL_SOCKET_DIR")
        self.configured_socket_root = (
            default_socket_root(self.runs_dir)
            if configured_socket_root is None
            else Path(configured_socket_root).expanduser()
        )
        self.socket_root = self.configured_socket_root
        self.socket_path = self.socket_root / f"{self.mission_id}.sock"
        members = control.members()
        self.specialist_instances = tuple(
            instance for instance in sorted(members) if instance != "lead"
        )
        self.instance_socket_paths = {
            instance: self.socket_root / instance_socket_name(self.mission_id, instance)
            for instance in self.specialist_instances
        }
        if len(set(self.instance_socket_paths.values())) != len(
            self.instance_socket_paths
        ):
            raise ControlServiceError("specialist control endpoint name collision")
        self.lifecycle_path = self.root / "lifecycle.json"
        self.stopped_receipt_path = self.root / "service-stopped.json"
        self.startups_relative = self.control_relative / "startups"
        self.operations_relative = self.control_relative / "operations"
        self.lock_path = self.root / "service.lock"
        self.lifecycle_relative = self.control_relative / "lifecycle.json"
        self.stopped_receipt_relative = self.control_relative / "service-stopped.json"
        self.lock_relative = self.control_relative / "service.lock"
        self.stdout_relative = self.control_relative / "service.stdout.log"
        self.stderr_relative = self.control_relative / "service.stderr.log"
        self._control_directory_modes = (0o700, 0o700, 0o700)
        self._journal_directory_modes = (0o700, 0o700, 0o700, 0o700)
        self._operation_directory_modes = (
            0o700,
            0o700,
            0o700,
            0o700,
            0o700,
        )
        self.process: subprocess.Popen[bytes] | None = None

    def _set_socket_binding(self, socket_root: Path) -> None:
        self.socket_root = socket_root
        self.socket_path = self.socket_root / f"{self.mission_id}.sock"
        self.instance_socket_paths = {
            instance: self.socket_root / instance_socket_name(self.mission_id, instance)
            for instance in self.specialist_instances
        }

    def _prepare_configured_socket_root(self) -> tuple[Path, dict[str, int]]:
        try:
            mission_state.ensure_private_directory(self.configured_socket_root)
            socket_root = fleet_safe_paths.canonical_root(
                self.configured_socket_root, required_mode=0o700
            )
            with control_runtime.open_socket_root(socket_root) as (_, identity):
                return socket_root, identity
        except (
            mission_state.MissionStateError,
            fleet_safe_paths.SafePathError,
            control_runtime.RuntimeIdentityError,
        ) as exc:
            raise ControlServiceError("unsafe Fleet Control socket root") from exc

    @contextmanager
    def _rooted_state(self):
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                yield rooted
        except fleet_safe_paths.SafePathError as exc:
            raise ControlServiceError("unsafe Fleet Control state") from exc

    @contextmanager
    def _lock(self):
        with self._rooted_state() as rooted:
            with rooted.exclusive_lock(
                self.lock_relative,
                directory_modes=self._control_directory_modes,
                file_mode=0o600,
            ):
                yield

    def _lifecycle_exists(self) -> bool:
        with self._rooted_state() as rooted:
            return "lifecycle.json" in rooted.list_directory(
                self.control_relative,
                directory_modes=self._control_directory_modes,
            )

    def _replace_lifecycle(self, lifecycle: dict[str, Any]) -> None:
        content = mission_state.canonical_bytes(lifecycle) + b"\n"
        with self._rooted_state() as rooted:
            rooted.replace_regular(
                self.lifecycle_relative,
                content,
                directory_modes=self._control_directory_modes,
                file_mode=0o600,
            )

    def _startup_relative(self, launch_id: str) -> Path:
        return self.startups_relative / f"{launch_id}.json"

    def _startup_checkpoint_relative(self) -> Path:
        return self.startups_relative / "startup.pending.json"

    def _publish_startup_checkpoint(self, journal: dict[str, Any]) -> None:
        """Durably retain the exact bytes needed to recover atomic publication."""

        content = mission_state.canonical_bytes(journal) + b"\n"
        try:
            with self._rooted_state() as rooted:
                rooted.atomic_write(
                    self._startup_checkpoint_relative(),
                    content,
                    directory_modes=self._journal_directory_modes,
                    file_mode=0o600,
                )
        except (KeyError, fleet_safe_paths.SafePathError) as exc:
            raise ControlServiceError(
                "cannot publish Fleet Control startup checkpoint"
            ) from exc

    def _publish_startup_journal(self, journal: dict[str, Any]) -> None:
        content = mission_state.canonical_bytes(journal) + b"\n"
        try:
            with self._rooted_state() as rooted:
                rooted.atomic_write(
                    self._startup_relative(journal["launch_id"]),
                    content,
                    directory_modes=self._journal_directory_modes,
                    file_mode=0o600,
                )
        except (KeyError, fleet_safe_paths.SafePathError) as exc:
            raise ControlServiceError(
                "cannot publish Fleet Control startup journal"
            ) from exc

    def _validate_startup_journal(self, value: Any) -> dict[str, Any]:
        required = {
            "schema_version",
            "mission_id",
            "preset",
            "pid",
            "process_identity",
            "launch_id",
            "socket_root",
            "socket_root_identity",
            "endpoint_binding_sha256",
            "endpoints",
            "started_at",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ControlServiceError("Fleet Control startup journal schema mismatch")
        try:
            launch_id = str(uuid.UUID(str(value.get("launch_id"))))
            process_identity = control_runtime.validate_process_identity(
                value.get("process_identity")
            )
            root_identity = control_runtime.validate_directory_identity(
                value.get("socket_root_identity")
            )
        except (
            ValueError,
            TypeError,
            AttributeError,
            control_runtime.RuntimeIdentityError,
        ) as exc:
            raise ControlServiceError(
                "Fleet Control startup journal identity is invalid"
            ) from exc
        if (
            value.get("schema_version") != 1
            or value.get("mission_id") != self.mission_id
            or value.get("preset") != self.preset
            or value.get("launch_id") != launch_id
            or isinstance(value.get("pid"), bool)
            or not isinstance(value.get("pid"), int)
            or value["pid"] <= 1
            or value.get("process_identity") != process_identity
            or value.get("socket_root_identity") != root_identity
            or value.get("socket_root") != str(self.socket_root)
            or value.get("endpoint_binding_sha256")
            != endpoint_binding_sha256(self.instance_socket_paths)
            or not isinstance(value.get("started_at"), str)
            or not value["started_at"]
        ):
            raise ControlServiceError("Fleet Control startup journal binding mismatch")
        endpoints = value.get("endpoints")
        expected_paths = self._endpoint_paths()
        if not isinstance(endpoints, dict) or set(endpoints) != set(expected_paths):
            raise ControlServiceError("Fleet Control startup endpoints mismatch")
        normalized: dict[str, dict[str, Any]] = {}
        for index, key in enumerate(sorted(expected_paths)):
            endpoint = endpoints[key]
            if not isinstance(endpoint, dict) or set(endpoint) != {
                "staging_leaf",
                "final_leaf",
                "identity",
            }:
                raise ControlServiceError("Fleet Control startup endpoint is malformed")
            expected_staging = f".fc-{uuid.UUID(launch_id).hex}-{index:03d}.stage"
            if (
                endpoint.get("staging_leaf") != expected_staging
                or endpoint.get("final_leaf") != expected_paths[key].name
            ):
                raise ControlServiceError(
                    "Fleet Control startup endpoint leaf mismatch"
                )
            try:
                identity = control_runtime.validate_socket_identity(
                    endpoint.get("identity")
                )
            except control_runtime.RuntimeIdentityError as exc:
                raise ControlServiceError(
                    "Fleet Control startup endpoint identity is invalid"
                ) from exc
            normalized[key] = {**endpoint, "identity": identity}
        return {**value, "endpoints": normalized}

    def _read_startup_journals(self) -> list[dict[str, Any]]:
        self._recover_startup_checkpoints()
        try:
            with self._rooted_state() as rooted:
                try:
                    names = rooted.list_directory(
                        self.startups_relative,
                        directory_modes=self._journal_directory_modes,
                    )
                except fleet_safe_paths.SafePathError:
                    rooted.assert_absent(
                        self.startups_relative,
                        directory_modes=self._control_directory_modes,
                    )
                    return []
                values: list[dict[str, Any]] = []
                for name in names:
                    if name.endswith(".pending.json"):
                        raise ControlServiceError(
                            "unreconciled Fleet Control startup checkpoint"
                        )
                    if name.startswith(".fleet-atomic-") and name.endswith(".tmp"):
                        raise ControlServiceError(
                            "unreconciled Fleet Control startup publication"
                        )
                    try:
                        launch_id = str(uuid.UUID(name.removesuffix(".json")))
                    except (ValueError, AttributeError) as exc:
                        raise ControlServiceError(
                            "unexpected Fleet Control startup journal"
                        ) from exc
                    if name != f"{launch_id}.json":
                        raise ControlServiceError(
                            "unexpected Fleet Control startup journal"
                        )
                    content = rooted.read_regular(
                        self.startups_relative / name,
                        directory_modes=self._journal_directory_modes,
                        file_mode=0o600,
                        max_bytes=2 * 1024 * 1024,
                    )
                    try:
                        value = mission_state.loads_strict(content)
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise ControlServiceError(
                            "cannot read Fleet Control startup journal"
                        ) from exc
                    journal = self._validate_startup_journal(value)
                    if journal["launch_id"] != launch_id:
                        raise ControlServiceError(
                            "Fleet Control startup journal filename mismatch"
                        )
                    values.append(journal)
                return values
        except fleet_safe_paths.SafePathError as exc:
            raise ControlServiceError("unsafe Fleet Control startup journals") from exc

    def _recover_startup_checkpoints(self) -> None:
        """Recover the single serialized startup publication checkpoint."""

        checkpoint = self._startup_checkpoint_relative()
        checkpoint_leaf = checkpoint.name
        checkpoint_prefix = (
            ".fleet-atomic-"
            + hashlib.sha256(checkpoint_leaf.encode("utf-8")).hexdigest()
            + "-"
        )
        try:
            with self._rooted_state() as rooted:
                try:
                    names = rooted.list_directory(
                        self.startups_relative,
                        directory_modes=self._journal_directory_modes,
                    )
                except fleet_safe_paths.SafePathError:
                    rooted.assert_absent(
                        self.startups_relative,
                        directory_modes=self._control_directory_modes,
                    )
                    return
                atomic_pending = {
                    name
                    for name in names
                    if name.startswith(".fleet-atomic-") and name.endswith(".tmp")
                }
                checkpoint_content = rooted.read_regular_optional(
                    checkpoint,
                    directory_modes=self._journal_directory_modes,
                    file_mode=0o600,
                    max_bytes=2 * 1024 * 1024,
                )
                checkpoint_pending = sorted(
                    name
                    for name in atomic_pending
                    if name.startswith(checkpoint_prefix)
                )
                if len(checkpoint_pending) > 1:
                    raise ControlServiceError(
                        "multiple Fleet Control startup checkpoint publications"
                    )
                partial_checkpoint: Path | None = None
                journal: dict[str, Any] | None = None
                if checkpoint_content is None and checkpoint_pending:
                    pending_relative = self.startups_relative / checkpoint_pending[0]
                    candidate = rooted.read_regular(
                        pending_relative,
                        directory_modes=self._journal_directory_modes,
                        file_mode=0o600,
                        max_bytes=2 * 1024 * 1024,
                    )
                    try:
                        journal = self._validate_startup_journal(
                            mission_state.loads_strict(candidate)
                        )
                    except (ValueError, json.JSONDecodeError):
                        # No checkpoint exists, so the gated child was never
                        # released and this incomplete private CAS pending has
                        # no externally visible effect to adopt.
                        partial_checkpoint = pending_relative
                    else:
                        expected_name = (
                            checkpoint_prefix
                            + hashlib.sha256(candidate).hexdigest()
                            + ".tmp"
                        )
                        if checkpoint_pending[0] != expected_name:
                            raise ControlServiceError(
                                "Fleet Control startup checkpoint hash mismatch"
                            )
                        checkpoint_content = candidate
                elif checkpoint_content is not None:
                    try:
                        journal = self._validate_startup_journal(
                            mission_state.loads_strict(checkpoint_content)
                        )
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise ControlServiceError(
                            "cannot read Fleet Control startup checkpoint"
                        ) from exc

                # Validate every already-published final journal before any
                # partial-checkpoint cleanup or recovery mutation.
                for name in names:
                    if name in {checkpoint_leaf} or name.startswith(".fleet-atomic-"):
                        continue
                    try:
                        launch_id = str(uuid.UUID(name.removesuffix(".json")))
                    except (ValueError, AttributeError) as exc:
                        raise ControlServiceError(
                            "unexpected Fleet Control startup journal"
                        ) from exc
                    if name != f"{launch_id}.json":
                        raise ControlServiceError(
                            "unexpected Fleet Control startup journal"
                        )
                    content = rooted.read_regular(
                        self.startups_relative / name,
                        directory_modes=self._journal_directory_modes,
                        file_mode=0o600,
                        max_bytes=2 * 1024 * 1024,
                    )
                    final_journal = self._validate_startup_journal(
                        mission_state.loads_strict(content)
                    )
                    if final_journal["launch_id"] != launch_id:
                        raise ControlServiceError(
                            "Fleet Control startup journal filename mismatch"
                        )

                if partial_checkpoint is not None:
                    if atomic_pending != {partial_checkpoint.name}:
                        raise ControlServiceError(
                            "foreign Fleet Control startup pending publication"
                        )
                    rooted.unlink_regular(
                        partial_checkpoint,
                        directory_modes=self._journal_directory_modes,
                        file_mode=0o600,
                    )
                    return
                if checkpoint_content is None or journal is None:
                    if atomic_pending:
                        raise ControlServiceError(
                            "foreign Fleet Control startup pending publication"
                        )
                    return
                expected_checkpoint_pending = (
                    checkpoint_prefix
                    + hashlib.sha256(checkpoint_content).hexdigest()
                    + ".tmp"
                )
                final = self._startup_relative(journal["launch_id"])
                expected_final_pending = (
                    ".fleet-atomic-"
                    + hashlib.sha256(final.name.encode("utf-8")).hexdigest()
                    + "-"
                    + hashlib.sha256(checkpoint_content).hexdigest()
                    + ".tmp"
                )
                if not atomic_pending <= {
                    expected_checkpoint_pending,
                    expected_final_pending,
                }:
                    raise ControlServiceError(
                        "foreign Fleet Control startup pending publication"
                    )
                rooted.atomic_write(
                    checkpoint,
                    checkpoint_content,
                    directory_modes=self._journal_directory_modes,
                    file_mode=0o600,
                )
                rooted.atomic_write(
                    final,
                    checkpoint_content,
                    directory_modes=self._journal_directory_modes,
                    file_mode=0o600,
                )
                rooted.unlink_regular(
                    checkpoint,
                    directory_modes=self._journal_directory_modes,
                    file_mode=0o600,
                )
        except (
            ValueError,
            json.JSONDecodeError,
            fleet_safe_paths.SafePathError,
        ) as exc:
            raise ControlServiceError(
                "unsafe Fleet Control startup checkpoint recovery"
            ) from exc

    def _startup_locations(
        self,
        journal: dict[str, Any],
        *,
        allow_absent: bool,
    ) -> dict[str, str]:
        try:
            with control_runtime.open_socket_root(
                self.socket_root, journal["socket_root_identity"]
            ) as (root_fd, _):
                return {
                    key: control_runtime.socket_location_at(
                        root_fd,
                        endpoint["staging_leaf"],
                        endpoint["final_leaf"],
                        endpoint["identity"],
                        allow_absent=allow_absent,
                    )
                    for key, endpoint in sorted(journal["endpoints"].items())
                }
        except (KeyError, control_runtime.RuntimeIdentityError) as exc:
            raise ControlServiceError(
                "Fleet Control startup endpoint identity drift"
            ) from exc

    def _unlink_startup_endpoints(
        self,
        journal: dict[str, Any],
        locations: dict[str, str],
    ) -> None:
        try:
            with control_runtime.open_socket_root(
                self.socket_root, journal["socket_root_identity"]
            ) as (root_fd, _):
                for key, location in sorted(locations.items()):
                    if location == "absent":
                        continue
                    endpoint = journal["endpoints"][key]
                    leaf = endpoint[f"{location}_leaf"]
                    control_runtime.unlink_socket_at(
                        root_fd, leaf, endpoint["identity"]
                    )
        except (KeyError, control_runtime.RuntimeIdentityError) as exc:
            raise ControlServiceError(
                "cannot remove journal-bound Fleet Control endpoints"
            ) from exc

    def _cleanup_unjournaled_staging(
        self,
        root_identity: dict[str, int],
        journals: list[dict[str, Any]],
    ) -> None:
        """Remove only launch-unique private staging leaves without a journal.

        The local threat model treats arbitrary processes under CONTROL's UID as
        trusted.  A controller crash before journal publication can therefore
        leave only these non-public, launch-unique leaves; public endpoint names
        are never inferred or removed without durable inode identities.
        """

        recorded = {
            endpoint["staging_leaf"]
            for journal in journals
            for endpoint in journal["endpoints"].values()
        }
        try:
            with control_runtime.open_socket_root(self.socket_root, root_identity) as (
                root_fd,
                _,
            ):
                for name in sorted(os.listdir(root_fd)):
                    if name in recorded or _STAGING_LEAF.fullmatch(name) is None:
                        continue
                    identity = control_runtime.socket_identity_at(root_fd, name)
                    control_runtime.unlink_socket_at(root_fd, name, identity)
        except control_runtime.RuntimeIdentityError as exc:
            raise ControlServiceError(
                "cannot reconcile unjournaled Fleet Control staging"
            ) from exc

    def _validate_operation_journal(
        self,
        value: Any,
        lifecycle: dict[str, Any],
    ) -> dict[str, Any]:
        required = {
            "schema_version",
            "mission_id",
            "preset",
            "service_launch_id",
            "operation_id",
            "pid",
            "process_identity",
            "process_group_id",
            "command_sha256",
            "state",
            "started_at",
            "completed_at",
            "returncode",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ControlServiceError("Fleet Control operation journal schema mismatch")
        try:
            operation_id = str(uuid.UUID(str(value.get("operation_id"))))
            identity = control_runtime.validate_process_identity(
                value.get("process_identity")
            )
        except (
            ValueError,
            TypeError,
            AttributeError,
            control_runtime.RuntimeIdentityError,
        ) as exc:
            raise ControlServiceError(
                "Fleet Control operation journal identity is invalid"
            ) from exc
        state = value.get("state")
        returncode = value.get("returncode")
        completed_at = value.get("completed_at")
        if (
            value.get("schema_version") != 1
            or value.get("mission_id") != self.mission_id
            or value.get("preset") != self.preset
            or value.get("service_launch_id") != lifecycle["launch_id"]
            or value.get("operation_id") != operation_id
            or isinstance(value.get("pid"), bool)
            or not isinstance(value.get("pid"), int)
            or value["pid"] <= 1
            or value.get("process_identity") != identity
            or value.get("process_group_id") != value["pid"]
            or not isinstance(value.get("command_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["command_sha256"]) is None
            or state not in {"running", "completed", "cancelled", "reconciled"}
            or not isinstance(value.get("started_at"), str)
            or not value["started_at"]
        ):
            raise ControlServiceError("Fleet Control operation journal mismatch")
        if state == "running":
            if completed_at is not None or returncode is not None:
                raise ControlServiceError(
                    "running Fleet Control operation journal is terminal"
                )
        elif (
            not isinstance(completed_at, str)
            or not completed_at
            or (state != "reconciled" and type(returncode) is not int)
            or (state == "reconciled" and returncode is not None)
        ):
            raise ControlServiceError(
                "terminal Fleet Control operation journal is invalid"
            )
        return value

    def _operation_launch_relative(self, lifecycle: dict[str, Any]) -> Path:
        return self.operations_relative / lifecycle["launch_id"]

    def _operation_relative(self, lifecycle: dict[str, Any], operation_id: str) -> Path:
        return self._operation_launch_relative(lifecycle) / f"{operation_id}.json"

    def _operation_checkpoint_relative(self, lifecycle: dict[str, Any]) -> Path:
        return self._operation_launch_relative(lifecycle) / "operation.pending.json"

    def _operation_terminal_relative(
        self, lifecycle: dict[str, Any], operation_id: str
    ) -> Path:
        return (
            self._operation_launch_relative(lifecycle) / f"{operation_id}.terminal.json"
        )

    @staticmethod
    def _same_operation_binding(
        running: dict[str, Any], terminal: dict[str, Any]
    ) -> bool:
        mutable = {"state", "completed_at", "returncode"}
        return all(
            running.get(key) == terminal.get(key) for key in set(running) - mutable
        )

    def _recover_operation_checkpoints(self, lifecycle: dict[str, Any]) -> None:
        launch_relative = self._operation_launch_relative(lifecycle)
        checkpoint = self._operation_checkpoint_relative(lifecycle)
        checkpoint_prefix = (
            ".fleet-atomic-"
            + hashlib.sha256(checkpoint.name.encode("utf-8")).hexdigest()
            + "-"
        )
        try:
            with self._rooted_state() as rooted:
                try:
                    names = rooted.list_directory(
                        launch_relative,
                        directory_modes=self._operation_directory_modes,
                    )
                except fleet_safe_paths.SafePathError:
                    try:
                        rooted.list_directory(
                            self.operations_relative,
                            directory_modes=self._journal_directory_modes,
                        )
                    except fleet_safe_paths.SafePathError:
                        rooted.assert_absent(
                            self.operations_relative,
                            directory_modes=self._control_directory_modes,
                        )
                    else:
                        rooted.assert_absent(
                            launch_relative,
                            directory_modes=self._journal_directory_modes,
                        )
                    return
                actual_pending = {
                    name
                    for name in names
                    if name.startswith(".fleet-atomic-") and name.endswith(".tmp")
                }
                running_by_id: dict[str, dict[str, Any]] = {}
                terminal_by_id: dict[str, dict[str, Any]] = {}
                for name in names:
                    if name == checkpoint.name or name.startswith(".fleet-atomic-"):
                        continue
                    terminal_name = name.endswith(".terminal.json")
                    raw_id = name.removesuffix(
                        ".terminal.json" if terminal_name else ".json"
                    )
                    try:
                        operation_id = str(uuid.UUID(raw_id))
                    except (ValueError, AttributeError) as exc:
                        raise ControlServiceError(
                            "unexpected Fleet Control operation journal"
                        ) from exc
                    expected_name = (
                        f"{operation_id}.terminal.json"
                        if terminal_name
                        else f"{operation_id}.json"
                    )
                    if name != expected_name:
                        raise ControlServiceError(
                            "unexpected Fleet Control operation journal"
                        )
                    content = rooted.read_regular(
                        launch_relative / name,
                        directory_modes=self._operation_directory_modes,
                        file_mode=0o600,
                        max_bytes=2 * 1024 * 1024,
                    )
                    journal = self._validate_operation_journal(
                        mission_state.loads_strict(content), lifecycle
                    )
                    if journal["operation_id"] != operation_id:
                        raise ControlServiceError(
                            "Fleet Control operation journal filename mismatch"
                        )
                    target = terminal_by_id if terminal_name else running_by_id
                    expected_state = journal["state"] != "running"
                    if expected_state != terminal_name or operation_id in target:
                        raise ControlServiceError(
                            "Fleet Control operation journal state mismatch"
                        )
                    target[operation_id] = journal
                if set(terminal_by_id) - set(running_by_id):
                    raise ControlServiceError(
                        "Fleet Control terminal operation lacks running journal"
                    )
                for operation_id, terminal in terminal_by_id.items():
                    if not self._same_operation_binding(
                        running_by_id[operation_id], terminal
                    ):
                        raise ControlServiceError(
                            "Fleet Control operation terminal binding mismatch"
                        )

                checkpoint_content = rooted.read_regular_optional(
                    checkpoint,
                    directory_modes=self._operation_directory_modes,
                    file_mode=0o600,
                    max_bytes=2 * 1024 * 1024,
                )
                checkpoint_pending = sorted(
                    name
                    for name in actual_pending
                    if name.startswith(checkpoint_prefix)
                )
                if len(checkpoint_pending) > 1:
                    raise ControlServiceError(
                        "multiple Fleet Control operation checkpoint publications"
                    )
                checkpoint_journal: dict[str, Any] | None = None
                partial_checkpoint: Path | None = None
                if checkpoint_content is None and checkpoint_pending:
                    pending_relative = launch_relative / checkpoint_pending[0]
                    candidate = rooted.read_regular(
                        pending_relative,
                        directory_modes=self._operation_directory_modes,
                        file_mode=0o600,
                        max_bytes=2 * 1024 * 1024,
                    )
                    try:
                        checkpoint_journal = self._validate_operation_journal(
                            mission_state.loads_strict(candidate), lifecycle
                        )
                    except (ValueError, json.JSONDecodeError):
                        partial_checkpoint = pending_relative
                    else:
                        expected_name = (
                            checkpoint_prefix
                            + hashlib.sha256(candidate).hexdigest()
                            + ".tmp"
                        )
                        if checkpoint_pending[0] != expected_name:
                            raise ControlServiceError(
                                "Fleet Control operation checkpoint hash mismatch"
                            )
                        checkpoint_content = candidate
                elif checkpoint_content is not None:
                    checkpoint_journal = self._validate_operation_journal(
                        mission_state.loads_strict(checkpoint_content), lifecycle
                    )
                if checkpoint_journal is not None:
                    if checkpoint_journal["state"] != "running":
                        raise ControlServiceError(
                            "Fleet Control operation checkpoint is terminal"
                        )
                    previous = running_by_id.get(checkpoint_journal["operation_id"])
                    if previous is not None and previous != checkpoint_journal:
                        raise ControlServiceError(
                            "Fleet Control operation checkpoint conflicts with journal"
                        )
                    running_by_id[checkpoint_journal["operation_id"]] = (
                        checkpoint_journal
                    )

                expected_running_pending: str | None = None
                if checkpoint_content is not None and checkpoint_journal is not None:
                    running_final = self._operation_relative(
                        lifecycle, checkpoint_journal["operation_id"]
                    )
                    expected_running_pending = (
                        ".fleet-atomic-"
                        + hashlib.sha256(running_final.name.encode("utf-8")).hexdigest()
                        + "-"
                        + hashlib.sha256(checkpoint_content).hexdigest()
                        + ".tmp"
                    )

                excluded = set(checkpoint_pending)
                if expected_running_pending is not None:
                    excluded.add(expected_running_pending)
                terminal_recoveries: list[tuple[Path, bytes]] = []
                aborted_terminal: list[Path] = []
                for pending_name in sorted(actual_pending - excluded):
                    matches = [
                        operation_id
                        for operation_id in running_by_id
                        if pending_name.startswith(
                            ".fleet-atomic-"
                            + hashlib.sha256(
                                f"{operation_id}.terminal.json".encode("utf-8")
                            ).hexdigest()
                            + "-"
                        )
                    ]
                    if len(matches) != 1:
                        raise ControlServiceError(
                            "foreign Fleet Control operation pending publication"
                        )
                    operation_id = matches[0]
                    pending_relative = launch_relative / pending_name
                    content = rooted.read_regular(
                        pending_relative,
                        directory_modes=self._operation_directory_modes,
                        file_mode=0o600,
                        max_bytes=2 * 1024 * 1024,
                    )
                    try:
                        terminal = self._validate_operation_journal(
                            mission_state.loads_strict(content), lifecycle
                        )
                    except (ValueError, json.JSONDecodeError):
                        aborted_terminal.append(pending_relative)
                        continue
                    final = self._operation_terminal_relative(lifecycle, operation_id)
                    expected_name = (
                        ".fleet-atomic-"
                        + hashlib.sha256(final.name.encode("utf-8")).hexdigest()
                        + "-"
                        + hashlib.sha256(content).hexdigest()
                        + ".tmp"
                    )
                    if (
                        pending_name != expected_name
                        or terminal["operation_id"] != operation_id
                        or terminal["state"] == "running"
                        or not self._same_operation_binding(
                            running_by_id[operation_id], terminal
                        )
                    ):
                        raise ControlServiceError(
                            "Fleet Control terminal operation pending mismatch"
                        )
                    terminal_recoveries.append((final, content))

                if partial_checkpoint is not None:
                    if actual_pending != {partial_checkpoint.name}:
                        raise ControlServiceError(
                            "foreign Fleet Control operation pending publication"
                        )
                    rooted.unlink_regular(
                        partial_checkpoint,
                        directory_modes=self._operation_directory_modes,
                        file_mode=0o600,
                    )
                    return
                if checkpoint_content is not None and checkpoint_journal is not None:
                    final = self._operation_relative(
                        lifecycle, checkpoint_journal["operation_id"]
                    )
                    rooted.atomic_write(
                        checkpoint,
                        checkpoint_content,
                        directory_modes=self._operation_directory_modes,
                        file_mode=0o600,
                    )
                    rooted.atomic_write(
                        final,
                        checkpoint_content,
                        directory_modes=self._operation_directory_modes,
                        file_mode=0o600,
                    )
                    rooted.unlink_regular(
                        checkpoint,
                        directory_modes=self._operation_directory_modes,
                        file_mode=0o600,
                    )
                elif checkpoint_pending:
                    raise ControlServiceError(
                        "unbound Fleet Control operation checkpoint publication"
                    )
                for final, content in terminal_recoveries:
                    rooted.atomic_write(
                        final,
                        content,
                        directory_modes=self._operation_directory_modes,
                        file_mode=0o600,
                    )
                for pending in aborted_terminal:
                    rooted.unlink_regular(
                        pending,
                        directory_modes=self._operation_directory_modes,
                        file_mode=0o600,
                    )
        except (
            ValueError,
            json.JSONDecodeError,
            fleet_safe_paths.SafePathError,
        ) as exc:
            raise ControlServiceError(
                "unsafe Fleet Control operation checkpoint recovery"
            ) from exc

    def _read_operation_journals(
        self, lifecycle: dict[str, Any]
    ) -> list[tuple[Path, dict[str, Any]]]:
        self._recover_operation_checkpoints(lifecycle)
        launch_relative = self._operation_launch_relative(lifecycle)
        try:
            with self._rooted_state() as rooted:
                try:
                    names = rooted.list_directory(
                        launch_relative,
                        directory_modes=self._operation_directory_modes,
                    )
                except fleet_safe_paths.SafePathError:
                    try:
                        rooted.list_directory(
                            self.operations_relative,
                            directory_modes=self._journal_directory_modes,
                        )
                    except fleet_safe_paths.SafePathError:
                        rooted.assert_absent(
                            self.operations_relative,
                            directory_modes=self._control_directory_modes,
                        )
                    else:
                        rooted.assert_absent(
                            launch_relative,
                            directory_modes=self._journal_directory_modes,
                        )
                    return []
                running: dict[str, tuple[Path, dict[str, Any]]] = {}
                terminal: dict[str, tuple[Path, dict[str, Any]]] = {}
                for name in names:
                    if name.endswith(".pending.json"):
                        raise ControlServiceError(
                            "unreconciled Fleet Control operation checkpoint"
                        )
                    if name.startswith(".fleet-atomic-") and name.endswith(".tmp"):
                        raise ControlServiceError(
                            "unreconciled Fleet Control operation publication"
                        )
                    terminal_name = name.endswith(".terminal.json")
                    raw_operation_id = name.removesuffix(
                        ".terminal.json" if terminal_name else ".json"
                    )
                    try:
                        operation_id = str(uuid.UUID(raw_operation_id))
                    except (ValueError, AttributeError) as exc:
                        raise ControlServiceError(
                            "unexpected Fleet Control operation journal"
                        ) from exc
                    expected_name = (
                        f"{operation_id}.terminal.json"
                        if terminal_name
                        else f"{operation_id}.json"
                    )
                    if name != expected_name:
                        raise ControlServiceError(
                            "unexpected Fleet Control operation journal"
                        )
                    relative = launch_relative / name
                    content = rooted.read_regular(
                        relative,
                        directory_modes=self._operation_directory_modes,
                        file_mode=0o600,
                        max_bytes=2 * 1024 * 1024,
                    )
                    try:
                        value = mission_state.loads_strict(content)
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise ControlServiceError(
                            "cannot read Fleet Control operation journal"
                        ) from exc
                    journal = self._validate_operation_journal(value, lifecycle)
                    if journal["operation_id"] != operation_id:
                        raise ControlServiceError(
                            "Fleet Control operation journal filename mismatch"
                        )
                    if terminal_name:
                        if journal["state"] == "running" or operation_id in terminal:
                            raise ControlServiceError(
                                "Fleet Control terminal operation journal mismatch"
                            )
                        terminal[operation_id] = (relative, journal)
                    else:
                        if journal["state"] != "running" or operation_id in running:
                            raise ControlServiceError(
                                "Fleet Control running operation journal mismatch"
                            )
                        running[operation_id] = (relative, journal)
                if set(terminal) - set(running):
                    raise ControlServiceError(
                        "Fleet Control terminal operation lacks running journal"
                    )
                result: list[tuple[Path, dict[str, Any]]] = []
                for operation_id, running_item in sorted(running.items()):
                    terminal_item = terminal.get(operation_id)
                    if terminal_item is None:
                        result.append(running_item)
                        continue
                    if not self._same_operation_binding(
                        running_item[1], terminal_item[1]
                    ):
                        raise ControlServiceError(
                            "Fleet Control operation terminal binding mismatch"
                        )
                    result.append(terminal_item)
                return result
        except fleet_safe_paths.SafePathError as exc:
            raise ControlServiceError(
                "unsafe Fleet Control operation journals"
            ) from exc

    @staticmethod
    def _operation_process_running(journal: dict[str, Any]) -> bool:
        try:
            observed, zombie = control_runtime.process_observation(journal["pid"])
        except control_runtime.RuntimeIdentityError as exc:
            raise ControlServiceError(
                "cannot inspect journal-bound Fleet Control operation"
            ) from exc
        return observed == journal["process_identity"] and not zombie

    def _verified_operation_group(self, journal: dict[str, Any]) -> int | None:
        """Return a group only after an immediate identity+pgid revalidation."""

        if not self._operation_process_running(journal):
            return None
        pid = int(journal["pid"])
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            return None
        except OSError as exc:
            raise ControlServiceError(
                "cannot inspect Fleet Control operation process group"
            ) from exc
        if group != journal["process_group_id"] or group != pid:
            raise ControlServiceError(
                "Fleet Control operation process group identity mismatch"
            )
        if not self._operation_process_running(journal):
            return None
        try:
            if os.getpgid(pid) != group:
                raise ControlServiceError(
                    "Fleet Control operation process group identity drift"
                )
        except ProcessLookupError:
            return None
        return group

    def _terminate_operation_process(self, journal: dict[str, Any]) -> None:
        group = self._verified_operation_group(journal)
        if group is None:
            return
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            return
        # The operation worker is a SIGTERM-resistant, journal-bound group
        # anchor.  Recovery is a zero-late-effect boundary, so revalidate that
        # exact anchor and escalate immediately instead of granting an
        # untrusted child time to act after cancellation.
        if self._operation_process_running(journal):
            group = self._verified_operation_group(journal)
            if group is None:
                return
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not self._operation_process_running(journal):
                    return
                time.sleep(0.02)
        if self._operation_process_running(journal):
            raise ControlServiceError("Fleet Control operation did not terminate")

    def _reconcile_operation_journals(self, lifecycle: dict[str, Any]) -> None:
        journals = self._read_operation_journals(lifecycle)
        # Validation of every journal completes before the first signal or
        # durable mutation, so one malformed/foreign record is zero-effect.
        active = [item for item in journals if item[1]["state"] == "running"]
        for _, journal in active:
            self._terminate_operation_process(journal)
        completed_at = datetime.now(timezone.utc).isoformat()
        for _, journal in active:
            if self._operation_process_running(journal):
                raise ControlServiceError(
                    "Fleet Control operation remains live after reconciliation"
                )
            reconciled = {
                **journal,
                "state": "reconciled",
                "completed_at": completed_at,
                "returncode": None,
            }
            try:
                with self._rooted_state() as rooted:
                    rooted.atomic_write(
                        self._operation_terminal_relative(
                            lifecycle, journal["operation_id"]
                        ),
                        mission_state.canonical_bytes(reconciled) + b"\n",
                        directory_modes=self._operation_directory_modes,
                        file_mode=0o600,
                    )
            except fleet_safe_paths.SafePathError as exc:
                raise ControlServiceError(
                    "cannot reconcile Fleet Control operation journal"
                ) from exc

    def _unlink_lifecycle(self, *, missing_ok: bool = False) -> bool:
        with self._rooted_state() as rooted:
            return rooted.unlink_regular(
                self.lifecycle_relative,
                directory_modes=self._control_directory_modes,
                file_mode=0o600,
                missing_ok=missing_ok,
            )

    def _read_log(self, relative: Path) -> str:
        with self._rooted_state() as rooted:
            content = rooted.read_regular(
                relative,
                directory_modes=self._control_directory_modes,
                file_mode=0o600,
                max_bytes=8 * 1024 * 1024,
            )
        return content.decode("utf-8", errors="replace")

    def _read_stopped_receipt_optional(
        self, lifecycle: dict[str, Any]
    ) -> dict[str, Any] | None:
        try:
            with self._rooted_state() as rooted:
                content = rooted.read_regular_optional(
                    self.stopped_receipt_relative,
                    directory_modes=self._control_directory_modes,
                    file_mode=0o600,
                    max_bytes=2 * 1024 * 1024,
                )
            if content is None:
                return None
            value = mission_state.loads_strict(content)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ControlServiceError(
                "cannot read Fleet Control stopped receipt"
            ) from exc
        required = {
            "schema_version",
            "mission_id",
            "preset",
            "pid",
            "process_identity",
            "launch_id",
            "socket_root_identity",
            "endpoint_binding_sha256",
            "endpoint_identities",
            "stopped_at",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ControlServiceError("Fleet Control stopped receipt schema mismatch")
        endpoint_identities = self._validated_endpoint_identities(
            value.get("endpoint_identities")
        )
        expected = {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "preset": self.preset,
            "pid": lifecycle["pid"],
            "process_identity": lifecycle["process_identity"],
            "launch_id": lifecycle["launch_id"],
            "socket_root_identity": lifecycle["socket_root_identity"],
            "endpoint_binding_sha256": endpoint_binding_sha256(
                self.instance_socket_paths
            ),
            "endpoint_identities": endpoint_identities,
        }
        if any(value.get(key) != item for key, item in expected.items()):
            raise ControlServiceError("Fleet Control stopped receipt identity mismatch")
        if not isinstance(value.get("stopped_at"), str) or not value["stopped_at"]:
            raise ControlServiceError(
                "Fleet Control stopped receipt timestamp is invalid"
            )
        recorded = lifecycle.get("endpoint_identities")
        if recorded is not None and recorded != endpoint_identities:
            raise ControlServiceError(
                "Fleet Control stopped endpoint identity mismatch"
            )
        return value

    def _validated_endpoint_identities(self, value: Any) -> dict[str, dict[str, int]]:
        expected_keys = {"base", *self.instance_socket_paths}
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise ControlServiceError("control lifecycle endpoint identities mismatch")
        try:
            return {
                key: control_runtime.validate_socket_identity(value[key])
                for key in sorted(expected_keys)
            }
        except control_runtime.RuntimeIdentityError as exc:
            raise ControlServiceError("invalid control endpoint identity") from exc

    def _read_lifecycle(self) -> dict[str, Any]:
        try:
            with self._rooted_state() as rooted:
                content = rooted.read_regular(
                    self.lifecycle_relative,
                    directory_modes=self._control_directory_modes,
                    file_mode=0o600,
                    max_bytes=2 * 1024 * 1024,
                )
            value = mission_state.loads_strict(content)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ControlServiceError(f"cannot read control lifecycle: {exc}") from exc
        required = {
            "schema_version",
            "mission_id",
            "preset",
            "pid",
            "process_identity",
            "launch_id",
            "socket_root",
            "socket_root_identity",
            "socket",
            "instance_sockets",
            "endpoint_identities",
            "started_at",
            "stopped_at",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ControlServiceError(
                "control lifecycle fields do not match schema_version=3"
            )
        if (
            value.get("schema_version") != 3
            or value.get("mission_id") != self.mission_id
            or value.get("preset") != self.preset
        ):
            raise ControlServiceError("control lifecycle mission identity mismatch")
        if (
            not isinstance(value.get("socket_root"), str)
            or not Path(value["socket_root"]).is_absolute()
            or str(Path(value["socket_root"])) != value["socket_root"]
        ):
            raise ControlServiceError("control lifecycle socket root is invalid")
        try:
            root_identity = control_runtime.validate_directory_identity(
                value.get("socket_root_identity")
            )
            with control_runtime.open_socket_root(
                Path(value["socket_root"]), root_identity
            ):
                pass
            process_identity = control_runtime.validate_process_identity(
                value.get("process_identity")
            )
        except control_runtime.RuntimeIdentityError as exc:
            raise ControlServiceError(
                "control lifecycle runtime identity mismatch"
            ) from exc
        try:
            launch_id = str(uuid.UUID(str(value.get("launch_id"))))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ControlServiceError(
                "control lifecycle launch identity is invalid"
            ) from exc
        if launch_id != value.get("launch_id"):
            raise ControlServiceError("control lifecycle launch identity is invalid")
        self._set_socket_binding(Path(value["socket_root"]))
        expected = {
            instance: str(path) for instance, path in self.instance_socket_paths.items()
        }
        if (
            value.get("socket") != str(self.socket_path)
            or value.get("instance_sockets") != expected
        ):
            raise ControlServiceError("control lifecycle endpoint binding mismatch")
        if (
            isinstance(value.get("pid"), bool)
            or not isinstance(value.get("pid"), int)
            or value["pid"] <= 1
        ):
            raise ControlServiceError("control lifecycle pid is invalid")
        if value.get("process_identity") != process_identity:
            raise ControlServiceError("control lifecycle process identity is invalid")
        if value.get("socket_root_identity") != root_identity:
            raise ControlServiceError(
                "control lifecycle socket root identity is invalid"
            )
        endpoint_identities = value.get("endpoint_identities")
        if endpoint_identities is None:
            raise ControlServiceError(
                "control lifecycle endpoint identities are not durable"
            )
        validated = self._validated_endpoint_identities(endpoint_identities)
        if endpoint_identities != validated:
            raise ControlServiceError("control lifecycle endpoint identity is invalid")
        if not isinstance(value.get("started_at"), str) or not value["started_at"]:
            raise ControlServiceError("control lifecycle start timestamp is invalid")
        if value.get("stopped_at") is not None and (
            not isinstance(value["stopped_at"], str) or not value["stopped_at"]
        ):
            raise ControlServiceError("control lifecycle stop timestamp is invalid")
        matching = [
            journal
            for journal in self._read_startup_journals()
            if journal["launch_id"] == launch_id
        ]
        if len(matching) != 1:
            raise ControlServiceError(
                "control lifecycle lacks one exact startup journal"
            )
        expected = self._lifecycle_from_startup(matching[0])
        if any(
            value.get(key) != expected[key] for key in expected if key != "stopped_at"
        ):
            raise ControlServiceError(
                "control lifecycle and startup journal do not match"
            )
        return value

    def _endpoint_paths(self) -> dict[str, Path]:
        return {"base": self.socket_path, **self.instance_socket_paths}

    def _bind_staged_endpoints(
        self,
        launch_id: str,
        root_identity: dict[str, int],
    ) -> tuple[dict[str, socket.socket], dict[str, dict[str, Any]]]:
        sockets: dict[str, socket.socket] = {}
        endpoints: dict[str, dict[str, Any]] = {}
        launch_hex = uuid.UUID(launch_id).hex
        try:
            with control_runtime.open_socket_root(self.socket_root, root_identity) as (
                root_fd,
                _,
            ):
                for index, (key, final_path) in enumerate(
                    sorted(self._endpoint_paths().items())
                ):
                    staging_leaf = f".fc-{launch_hex}-{index:03d}.stage"
                    staging_path = self.socket_root / staging_leaf
                    if len(os.fsencode(staging_path)) >= 100:
                        raise ControlServiceError(
                            "FLEET_CONTROL_SOCKET_DIR produces an unsafe staging path"
                        )
                    control_runtime.assert_absent_at(root_fd, final_path.name)
                    control_runtime.assert_absent_at(root_fd, staging_leaf)
                    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    bound_identity: dict[str, int] | None = None
                    try:
                        endpoint.bind(str(staging_path))
                        bound_identity = control_runtime.socket_binding_at(
                            root_fd, staging_leaf
                        )
                        bound_identity = control_runtime.chmod_socket_at(
                            root_fd, staging_leaf, bound_identity
                        )
                        endpoint.listen()
                        identity = control_runtime.socket_identity_at(
                            root_fd, staging_leaf
                        )
                    except BaseException:
                        endpoint.close()
                        if bound_identity is not None:
                            try:
                                control_runtime.unlink_socket_binding_at(
                                    root_fd, staging_leaf, bound_identity
                                )
                            except control_runtime.RuntimeIdentityError:
                                pass
                        raise
                    sockets[key] = endpoint
                    endpoints[key] = {
                        "staging_leaf": staging_leaf,
                        "final_leaf": final_path.name,
                        "identity": identity,
                    }
            return sockets, endpoints
        except BaseException:
            for endpoint in sockets.values():
                endpoint.close()
            try:
                with control_runtime.open_socket_root(
                    self.socket_root, root_identity
                ) as (root_fd, _):
                    for value in endpoints.values():
                        control_runtime.unlink_socket_at(
                            root_fd,
                            value["staging_leaf"],
                            value["identity"],
                        )
            except control_runtime.RuntimeIdentityError:
                pass
            raise

    @staticmethod
    def _close_staged_sockets(sockets: dict[str, socket.socket]) -> None:
        for endpoint in sockets.values():
            try:
                endpoint.close()
            except OSError:
                pass

    def _publish_staged_endpoints(self, journal: dict[str, Any]) -> None:
        # Validate the complete set before the first rename.  A single foreign,
        # replaced, duplicated, or missing leaf therefore produces zero mutation.
        locations = self._startup_locations(journal, allow_absent=False)
        if set(locations.values()) != {"staging"}:
            raise ControlServiceError(
                "Fleet Control startup endpoints are not wholly staged"
            )
        order = [key for key in sorted(journal["endpoints"]) if key != "base"]
        order.append("base")
        try:
            with control_runtime.open_socket_root(
                self.socket_root, journal["socket_root_identity"]
            ) as (root_fd, _):
                for index, key in enumerate(order):
                    endpoint = journal["endpoints"][key]
                    control_runtime.publish_socket_at(
                        root_fd,
                        endpoint["staging_leaf"],
                        endpoint["final_leaf"],
                        endpoint["identity"],
                    )
                    if (
                        index == 0
                        and os.environ.get("FLEET_TEST_CONTROL_CRASH_AT")
                        == "after_partial_publish"
                    ):
                        os._exit(137)
        except control_runtime.RuntimeIdentityError as exc:
            raise ControlServiceError(
                "cannot publish exact Fleet Control endpoints"
            ) from exc

    @staticmethod
    def _journal_process_running(journal: dict[str, Any]) -> bool:
        try:
            observed, zombie = control_runtime.process_observation(journal["pid"])
        except control_runtime.RuntimeIdentityError as exc:
            raise ControlServiceError(
                "cannot inspect journal-bound Fleet Control process"
            ) from exc
        return observed == journal["process_identity"] and not zombie

    def _terminate_journal_process(self, journal: dict[str, Any]) -> None:
        """Terminate only a still-exact startup child and its owned process group."""

        group = self._verified_startup_group(journal)
        if group is None:
            return
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not self._journal_process_running(journal):
                break
            time.sleep(0.02)
        if self._journal_process_running(journal):
            # Recheck exact PID identity and pgid immediately before
            # escalation. Observation drift is always a no-signal outcome.
            group = self._verified_startup_group(journal)
            if group is None:
                return
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not self._journal_process_running(journal):
                    break
                time.sleep(0.02)
        if self._journal_process_running(journal):
            raise ControlServiceError(
                "journal-bound Fleet Control process did not terminate"
            )

    def _verified_startup_group(self, journal: dict[str, Any]) -> int | None:
        if not self._journal_process_running(journal):
            return None
        pid = int(journal["pid"])
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise ControlServiceError(
                "cannot inspect journal-bound Fleet Control process group"
            ) from exc
        if group != pid:
            raise ControlServiceError(
                "journal-bound Fleet Control process group identity mismatch"
            )
        if not self._journal_process_running(journal):
            return None
        try:
            if os.getpgid(pid) != group:
                raise ControlServiceError(
                    "journal-bound Fleet Control process group identity drift"
                )
        except ProcessLookupError:
            return None
        return group

    def _lifecycle_from_startup(self, journal: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": 3,
            "mission_id": self.mission_id,
            "preset": self.preset,
            "pid": journal["pid"],
            "process_identity": journal["process_identity"],
            "launch_id": journal["launch_id"],
            "socket_root": str(self.socket_root),
            "socket_root_identity": journal["socket_root_identity"],
            "socket": str(self.socket_path),
            "instance_sockets": {
                instance: str(path)
                for instance, path in self.instance_socket_paths.items()
            },
            "endpoint_identities": {
                key: endpoint["identity"]
                for key, endpoint in journal["endpoints"].items()
            },
            "started_at": journal["started_at"],
            "stopped_at": None,
        }

    def _reconcile_orphan_startups(
        self,
        root_identity: dict[str, int],
    ) -> dict[str, Any] | None:
        journals = self._read_startup_journals()
        self._cleanup_unjournaled_staging(root_identity, journals)
        observations: list[tuple[dict[str, Any], dict[str, str], bool]] = []
        for journal in journals:
            if journal["socket_root_identity"] != root_identity:
                raise ControlServiceError(
                    "Fleet Control startup journal root identity drift"
                )
            locations = self._startup_locations(journal, allow_absent=True)
            observations.append(
                (journal, locations, self._journal_process_running(journal))
            )
        live_final = [
            item
            for item in observations
            if item[2] and set(item[1].values()) == {"final"}
        ]
        if len(live_final) > 1:
            raise ControlServiceError("multiple live Fleet Control startup journals")
        if live_final:
            journal, _, _ = live_final[0]
            lifecycle = self._lifecycle_from_startup(journal)
            self._replace_lifecycle(lifecycle)
            try:
                self._probe_health(lifecycle)
            except ControlServiceError:
                self._unlink_lifecycle(missing_ok=True)
            else:
                return lifecycle
        # All journal shapes and inode locations were validated before this
        # mutation phase.  Non-final live children are gated or unhealthy exact
        # launches; terminate only their identity-bound process group.
        for journal, _, live in observations:
            if live:
                self._terminate_journal_process(journal)
        for journal, _, _ in observations:
            current = self._startup_locations(journal, allow_absent=True)
            self._unlink_startup_endpoints(journal, current)
        return None

    def _capture_endpoint_identities(
        self, root_identity: dict[str, int]
    ) -> dict[str, dict[str, int]]:
        try:
            with control_runtime.open_socket_root(self.socket_root, root_identity) as (
                root_fd,
                _,
            ):
                return {
                    key: control_runtime.socket_identity_at(root_fd, path.name)
                    for key, path in sorted(self._endpoint_paths().items())
                }
        except control_runtime.RuntimeIdentityError as exc:
            raise ControlServiceError(
                "Fleet Control endpoint identity mismatch"
            ) from exc

    def _assert_endpoint_identities(
        self,
        root_identity: dict[str, int],
        endpoint_identities: dict[str, dict[str, int]],
    ) -> None:
        try:
            with control_runtime.open_socket_root(self.socket_root, root_identity) as (
                root_fd,
                _,
            ):
                for key, path in sorted(self._endpoint_paths().items()):
                    control_runtime.assert_socket_at(
                        root_fd, path.name, endpoint_identities[key]
                    )
        except (KeyError, control_runtime.RuntimeIdentityError) as exc:
            raise ControlServiceError(
                "Fleet Control endpoint identity mismatch"
            ) from exc

    def _assert_endpoints_absent(self, root_identity: dict[str, int]) -> None:
        try:
            with control_runtime.open_socket_root(self.socket_root, root_identity) as (
                root_fd,
                _,
            ):
                for path in self._endpoint_paths().values():
                    control_runtime.assert_absent_at(root_fd, path.name)
        except control_runtime.RuntimeIdentityError as exc:
            raise ControlServiceError("Fleet Control endpoint is not absent") from exc

    def _unlink_bound_endpoints(
        self,
        root_identity: dict[str, int],
        endpoint_identities: dict[str, dict[str, int]],
    ) -> None:
        """Remove only exact durable socket inodes through the pinned root fd."""

        try:
            with control_runtime.open_socket_root(self.socket_root, root_identity) as (
                root_fd,
                _,
            ):
                for key, path in sorted(self._endpoint_paths().items()):
                    try:
                        control_runtime.assert_absent_at(root_fd, path.name)
                        continue
                    except control_runtime.RuntimeIdentityError as exc:
                        if "must be absent" not in str(exc):
                            raise
                    control_runtime.unlink_socket_at(
                        root_fd, path.name, endpoint_identities[key]
                    )
        except (KeyError, control_runtime.RuntimeIdentityError) as exc:
            raise ControlServiceError(
                "cannot remove exact Fleet Control endpoints"
            ) from exc

    def _unlink_crashed_launch_staging(self, lifecycle: dict[str, Any]) -> None:
        """Remove exact staged leaves left by a launch that crashed mid-publish.

        The lifecycle records final endpoint paths only, so a crash between
        the first publish rename and gate release strands the unpublished
        staging inodes; they are located and removed through their exact
        startup-journal identities.
        """

        matching = [
            journal
            for journal in self._read_startup_journals()
            if journal["launch_id"] == lifecycle["launch_id"]
        ]
        if len(matching) != 1:
            raise ControlServiceError(
                "crashed Fleet Control lifecycle lacks one exact startup journal"
            )
        journal = matching[0]
        staged = {
            key: location
            for key, location in self._startup_locations(
                journal, allow_absent=True
            ).items()
            if location == "staging"
        }
        if staged:
            self._unlink_startup_endpoints(journal, staged)

    def _send_bound_request(
        self,
        lifecycle: dict[str, Any],
        value: dict[str, Any],
        *,
        require_after: bool = True,
    ) -> dict[str, Any]:
        endpoint_identities = lifecycle.get("endpoint_identities")
        if endpoint_identities is None:
            raise ControlServiceError(
                "Fleet Control endpoint identities are not durable"
            )
        root_identity = lifecycle["socket_root_identity"]
        self._assert_endpoint_identities(root_identity, endpoint_identities)
        result = send_request(
            self.socket_path,
            value,
            expected_peer_pid=int(lifecycle["pid"]),
        )
        if require_after:
            self._assert_endpoint_identities(root_identity, endpoint_identities)
        return result

    def _discard_unrecorded_process(self, process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise ControlServiceError(
                "gated Fleet Control process did not exit after launch cancellation"
            )
        _LIVE_PROCESSES.pop(process.pid, None)
        if self.process is process:
            self.process = None
        try:
            with control_runtime.open_socket_root(self.socket_root) as (
                root_fd,
                _,
            ):
                for path in self._endpoint_paths().values():
                    try:
                        identity = control_runtime.socket_identity_at(
                            root_fd, path.name
                        )
                    except control_runtime.RuntimeIdentityError:
                        continue
                    control_runtime.unlink_socket_at(root_fd, path.name, identity)
        except control_runtime.RuntimeIdentityError:
            pass

    def _probe_health(self, lifecycle: dict[str, Any]) -> dict[str, Any]:
        expected_pid = int(lifecycle["pid"])
        try:
            endpoint_identities = lifecycle.get("endpoint_identities")
            if endpoint_identities is None:
                endpoint_identities = self._capture_endpoint_identities(
                    lifecycle["socket_root_identity"]
                )
                reply = send_request(
                    self.socket_path,
                    {"operation": "health"},
                    expected_peer_pid=expected_pid,
                )
                self._assert_endpoint_identities(
                    lifecycle["socket_root_identity"], endpoint_identities
                )
            else:
                reply = self._send_bound_request(lifecycle, {"operation": "health"})
        except (OSError, ControlServiceError) as exc:
            raise ControlServiceError(f"Fleet Control health failed: {exc}") from exc
        result = reply.get("result") if reply.get("ok") else None
        if not isinstance(result, dict) or any(
            (
                result.get("status") != "ok",
                result.get("mission_id") != self.mission_id,
                result.get("preset") != self.preset,
                result.get("protocol") != "fleet-control-unix-v2",
                isinstance(result.get("process_id"), bool),
                not isinstance(result.get("process_id"), int),
                result.get("process_id") != expected_pid,
                result.get("process_identity") != lifecycle["process_identity"],
                result.get("launch_id") != lifecycle["launch_id"],
                result.get("socket_root_identity") != lifecycle["socket_root_identity"],
                result.get("endpoint_identities") != endpoint_identities,
                result.get("instance_endpoint_count")
                != len(self.instance_socket_paths),
                result.get("endpoint_binding_sha256")
                != endpoint_binding_sha256(self.instance_socket_paths),
            )
        ):
            raise ControlServiceError("Fleet Control health identity mismatch")
        if lifecycle.get("endpoint_identities") is None:
            lifecycle["endpoint_identities"] = endpoint_identities
            self._replace_lifecycle(lifecycle)
        return result

    def health(self) -> dict[str, Any]:
        lifecycle = self._read_lifecycle()
        if lifecycle.get("stopped_at") is not None:
            raise ControlServiceError("Fleet Control lifecycle is already stopped")
        return self._probe_health(lifecycle)

    @staticmethod
    def _expected_process_running(lifecycle: dict[str, Any]) -> bool:
        try:
            observed, zombie = control_runtime.process_observation(
                int(lifecycle["pid"])
            )
        except control_runtime.RuntimeIdentityError as exc:
            raise ControlServiceError("cannot verify exact control process") from exc
        if observed is None or observed != lifecycle["process_identity"] or zombie:
            return False
        return True

    def _reap_owned_child(self, lifecycle: dict[str, Any]) -> None:
        """Idempotently reap this interpreter's exact quiescent child.

        A separate fleet-down process can stop the service and durably mark the
        lifecycle while the original launcher still owns the ``Popen`` object.
        The child is then a zombie until that launcher calls ``wait()``.  This
        method never signals a process and never waits on an unverified live
        service.
        """

        pid = int(lifecycle["pid"])
        direct = self.process
        registered = _LIVE_PROCESSES.get(pid)
        if direct is not None and direct.pid != pid:
            raise ControlServiceError("Fleet Control child process identity mismatch")
        if direct is not None and registered is not None and direct is not registered:
            raise ControlServiceError("conflicting Fleet Control child handles")
        process = direct or registered
        if process is None:
            return
        if process.pid != pid:
            raise ControlServiceError("Fleet Control child pid mismatch")
        if process.returncode is None:
            if self._expected_process_running(lifecycle):
                raise ControlServiceError("refusing to reap a live Fleet Control child")
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired as exc:
                raise ControlServiceError(
                    "quiescent Fleet Control child was not reapable"
                ) from exc
        if process.returncode is None:
            raise ControlServiceError("Fleet Control child reap did not complete")
        if _LIVE_PROCESSES.get(pid) is process:
            _LIVE_PROCESSES.pop(pid, None)

    def _record_stopped(
        self, lifecycle: dict[str, Any], *, stopped_at: str | None = None
    ) -> dict[str, Any]:
        pid = int(lifecycle["pid"])
        self._reconcile_operation_journals(lifecycle)
        self._reap_owned_child(lifecycle)
        _LIVE_PROCESSES.pop(pid, None)
        lifecycle["stopped_at"] = stopped_at or datetime.now(timezone.utc).isoformat()
        self._replace_lifecycle(lifecycle)
        return {"stopped": True, "lifecycle": lifecycle}

    def start(self) -> dict[str, Any]:
        with self._lock():
            return self._start_locked()

    def _start_locked(self) -> dict[str, Any]:
        if self._lifecycle_exists():
            existing = self._read_lifecycle()
            if existing.get("stopped_at") is not None:
                raise ControlServiceError(
                    "Fleet Control was already stopped for this mission"
                )
            receipt = self._read_stopped_receipt_optional(existing)
            if receipt is not None:
                # The service crossed its handler/socket quiescence barrier,
                # but the controller that requested shutdown may have crashed
                # before replacing lifecycle.json.  Preserve both durable
                # records and make the lifecycle terminal before returning an
                # error; never unlink evidence and then attempt a fresh launch.
                receipt = self._wait_for_quiescence(existing, require_receipt=True)
                assert receipt is not None
                self._record_stopped(existing, stopped_at=receipt["stopped_at"])
                raise ControlServiceError(
                    "Fleet Control was already stopped for this mission"
                )
            try:
                return {
                    "started": False,
                    "health": self.health(),
                    "lifecycle": existing,
                }
            except ControlServiceError:
                if self._expected_process_running(existing):
                    raise
                self._reconcile_operation_journals(existing)
                self._unlink_bound_endpoints(
                    existing["socket_root_identity"],
                    existing["endpoint_identities"],
                )
                self._unlink_lifecycle()
        socket_root, socket_root_identity = self._prepare_configured_socket_root()
        self._set_socket_binding(socket_root)
        adopted = self._reconcile_orphan_startups(socket_root_identity)
        if adopted is not None:
            return {
                "started": False,
                "health": self._probe_health(adopted),
                "lifecycle": self._read_lifecycle(),
            }
        try:
            with self._rooted_state() as rooted:
                rooted.assert_absent(
                    self.stopped_receipt_relative,
                    directory_modes=self._control_directory_modes,
                )
        except fleet_safe_paths.SafePathError as exc:
            raise ControlServiceError(
                "Fleet Control stopped receipt exists before launch"
            ) from exc
        socket_paths = [self.socket_path, *self.instance_socket_paths.values()]
        if any(len(os.fsencode(path)) >= 100 for path in socket_paths):
            raise ControlServiceError(
                "FLEET_CONTROL_SOCKET_DIR produces an unsafe AF_UNIX path"
            )
        launch_id = str(uuid.uuid4())
        staged_sockets, startup_endpoints = self._bind_staged_endpoints(
            launch_id, socket_root_identity
        )
        if (
            os.environ.get("FLEET_TEST_CONTROL_CRASH_AT")
            == "after_staged_bind_before_journal"
        ):
            os._exit(137)
        command = [
            sys.executable,
            str(ROOT / "scripts" / "fleet_mcp.py"),
            "--runs-dir",
            str(self.runs_dir),
            "--mission-id",
            self.mission_id,
            "--preset",
            self.preset,
            "--socket",
            str(self.socket_path),
            "--launch-id",
            launch_id,
        ]
        for instance, path in self.instance_socket_paths.items():
            command.extend(["--instance-socket", f"{instance}={path}"])
        for key, endpoint in sorted(staged_sockets.items()):
            command.extend(["--inherited-socket", f"{key}={endpoint.fileno()}"])
        process: subprocess.Popen[bytes] | None = None
        gate_read: int | None = None
        gate_write: int | None = None
        startup: dict[str, Any] | None = None
        released = False
        try:
            gate_read, gate_write = os.pipe()
            gated_command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "_gate-exec",
                str(gate_read),
                *command,
            ]
            with self._rooted_state() as rooted:
                with (
                    rooted.open_append_regular(
                        self.stdout_relative,
                        directory_modes=self._control_directory_modes,
                        file_mode=0o600,
                    ) as stdout_fd,
                    rooted.open_append_regular(
                        self.stderr_relative,
                        directory_modes=self._control_directory_modes,
                        file_mode=0o600,
                    ) as stderr_fd,
                ):
                    process = subprocess.Popen(
                        gated_command,
                        cwd=ROOT,
                        env=os.environ.copy(),
                        stdout=stdout_fd,
                        stderr=stderr_fd,
                        start_new_session=True,
                        pass_fds=(
                            gate_read,
                            *(
                                endpoint.fileno()
                                for endpoint in staged_sockets.values()
                            ),
                        ),
                    )
            os.close(gate_read)
            gate_read = None
            if (
                os.environ.get("FLEET_TEST_CONTROL_CRASH_AT")
                == "after_popen_before_lifecycle"
            ):
                os._exit(137)
            self.process = process
            _LIVE_PROCESSES[process.pid] = process
            try:
                process_identity, zombie = control_runtime.process_observation(
                    process.pid
                )
            except control_runtime.RuntimeIdentityError as exc:
                raise ControlServiceError(
                    "cannot bind exact control process identity"
                ) from exc
            if process_identity is None or zombie:
                raise ControlServiceError(
                    "Fleet Control gated process exited before binding"
                )
            started_at = datetime.now(timezone.utc).isoformat()
            startup = {
                "schema_version": 1,
                "mission_id": self.mission_id,
                "preset": self.preset,
                "pid": process.pid,
                "process_identity": process_identity,
                "launch_id": launch_id,
                "socket_root": str(self.socket_root),
                "socket_root_identity": socket_root_identity,
                "endpoint_binding_sha256": endpoint_binding_sha256(
                    self.instance_socket_paths
                ),
                "endpoints": startup_endpoints,
                "started_at": started_at,
            }
            self._publish_startup_checkpoint(startup)
            self._publish_startup_journal(startup)
            with self._rooted_state() as rooted:
                rooted.unlink_regular(
                    self._startup_checkpoint_relative(),
                    directory_modes=self._journal_directory_modes,
                    file_mode=0o600,
                )
            if (
                os.environ.get("FLEET_TEST_CONTROL_CRASH_AT")
                == "after_startup_journal_before_lifecycle"
            ):
                os._exit(137)
            lifecycle = self._lifecycle_from_startup(startup)
            self._replace_lifecycle(lifecycle)
            if (
                os.environ.get("FLEET_TEST_CONTROL_CRASH_AT")
                == "after_lifecycle_before_release"
            ):
                os._exit(137)
            self._publish_staged_endpoints(startup)
            if (
                os.environ.get("FLEET_TEST_CONTROL_CRASH_AT")
                == "after_publish_before_release"
            ):
                os._exit(137)
            assert gate_write is not None
            if os.write(gate_write, b"\x01") != 1:
                raise ControlServiceError("Fleet Control launch gate was not released")
            released = True
            os.close(gate_write)
            gate_write = None
            self._close_staged_sockets(staged_sockets)
            staged_sockets = {}
            if (
                os.environ.get("FLEET_TEST_CONTROL_CRASH_AT")
                == "after_release_before_health"
            ):
                os._exit(137)
        except Exception:
            for descriptor in (gate_read, gate_write):
                if descriptor is None:
                    continue
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self._close_staged_sockets(staged_sockets)
            if startup is not None and process is not None:
                if released and self._journal_process_running(startup):
                    self._terminate_journal_process(startup)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    raise ControlServiceError(
                        "Fleet Control launch child did not exit after cancellation"
                    ) from exc
                locations = self._startup_locations(startup, allow_absent=True)
                self._unlink_startup_endpoints(startup, locations)
            self._unlink_lifecycle(missing_ok=True)
            if process is not None:
                _LIVE_PROCESSES.pop(process.pid, None)
                if self.process is process:
                    self.process = None
            raise
        assert process is not None
        deadline = time.monotonic() + 8
        last_error = "socket not ready"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                last_error = self._read_log(self.stderr_relative).strip()
                break
            if self.socket_path.exists():
                try:
                    health = self.health()
                    return {
                        "started": True,
                        "health": health,
                        "lifecycle": self._read_lifecycle(),
                    }
                except ControlServiceError as exc:
                    last_error = str(exc)
            time.sleep(0.05)
        try:
            self._stop_locked()
        except ControlServiceError:
            # Preserve the exact lifecycle and launch identity.  A live service
            # that cannot authenticate must remain a hard, recoverable blocker;
            # it is never terminated through an unauthenticated process signal.
            pass
        raise ControlServiceError(f"Fleet Control failed to start: {last_error}")

    def stop(self) -> dict[str, Any]:
        with self._lock():
            return self._stop_locked()

    def stop_if_present(self) -> dict[str, Any]:
        """Stop one descriptor-verified lifecycle or prove no service exists."""

        with self._lock():
            if not self._lifecycle_exists():
                if (
                    self.configured_socket_root.exists()
                    or self.configured_socket_root.is_symlink()
                ):
                    try:
                        socket_root = fleet_safe_paths.canonical_root(
                            self.configured_socket_root, required_mode=0o700
                        )
                        self._set_socket_binding(socket_root)
                        with control_runtime.open_socket_root(socket_root) as (
                            root_fd,
                            root_identity,
                        ):
                            pass
                        adopted = self._reconcile_orphan_startups(root_identity)
                        if adopted is not None:
                            return self._stop_locked()
                        with control_runtime.open_socket_root(
                            socket_root, root_identity
                        ) as (root_fd, _):
                            for path in self._endpoint_paths().values():
                                control_runtime.assert_absent_at(root_fd, path.name)
                    except (
                        fleet_safe_paths.SafePathError,
                        control_runtime.RuntimeIdentityError,
                    ) as exc:
                        raise ControlServiceError(
                            "Fleet Control endpoints exist without a durable lifecycle"
                        ) from exc
                return {"stopped": False, "absent": True}
            return self._stop_locked()

    def _wait_for_quiescence(
        self,
        lifecycle: dict[str, Any],
        *,
        require_receipt: bool,
        timeout: float = 8.0,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        receipt: dict[str, Any] | None = None
        pid = int(lifecycle["pid"])
        process = self.process or _LIVE_PROCESSES.get(pid)
        while time.monotonic() < deadline:
            receipt = self._read_stopped_receipt_optional(lifecycle)
            if receipt is not None:
                if lifecycle.get("endpoint_identities") is None:
                    lifecycle["endpoint_identities"] = receipt["endpoint_identities"]
                self._assert_endpoints_absent(lifecycle["socket_root_identity"])
            if process is not None and process.poll() is not None:
                running = False
            else:
                running = self._expected_process_running(lifecycle)
            if not running and (receipt is not None or not require_receipt):
                if process is not None:
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired as exc:
                        raise ControlServiceError(
                            "exact Fleet Control child was quiescent but not reapable"
                        ) from exc
                return receipt
            time.sleep(0.02)
        if require_receipt and receipt is None:
            raise ControlServiceError(
                "Fleet Control did not prove handler quiescence before the shutdown deadline"
            )
        raise ControlServiceError(
            "Fleet Control exact process did not quiesce before the shutdown deadline"
        )

    def _stop_locked(self) -> dict[str, Any]:
        lifecycle = self._read_lifecycle()
        if lifecycle.get("stopped_at") is not None:
            self._assert_endpoints_absent(lifecycle["socket_root_identity"])
            if self._expected_process_running(lifecycle):
                raise ControlServiceError(
                    "stopped Fleet Control lifecycle retains its exact process"
                )
            self._reap_owned_child(lifecycle)
            self._reconcile_operation_journals(lifecycle)
            return {"stopped": False, "lifecycle": lifecycle}
        pid = int(lifecycle["pid"])
        try:
            self.health()
        except ControlServiceError:
            # Reconcile a crash after the service exited but before the
            # lifecycle receipt was durably marked stopped.  A live/reused PID
            # remains a hard failure because its identity can no longer be
            # proven through the bound socket.
            # A self-stopping server removes its endpoints before the
            # interpreter finishes exiting; slow hosts need more than one
            # second to cross that window, so the bounded grace matches the
            # quiescence deadline instead of racing interpreter teardown.
            crash_deadline = time.monotonic() + 5.0
            while (
                self._expected_process_running(lifecycle)
                and time.monotonic() < crash_deadline
            ):
                time.sleep(0.02)
            if self._expected_process_running(lifecycle):
                raise
            receipt = self._read_stopped_receipt_optional(lifecycle)
            if receipt is not None:
                if lifecycle.get("endpoint_identities") is None:
                    lifecycle["endpoint_identities"] = receipt["endpoint_identities"]
                self._assert_endpoints_absent(lifecycle["socket_root_identity"])
                return self._record_stopped(lifecycle, stopped_at=receipt["stopped_at"])
            endpoint_identities = lifecycle.get("endpoint_identities")
            if endpoint_identities is None:
                self._assert_endpoints_absent(lifecycle["socket_root_identity"])
            else:
                self._unlink_bound_endpoints(
                    lifecycle["socket_root_identity"], endpoint_identities
                )
            self._unlink_crashed_launch_staging(lifecycle)
            self._wait_for_quiescence(lifecycle, require_receipt=False, timeout=1.0)
            return self._record_stopped(lifecycle)
        lifecycle = self._read_lifecycle()
        shutdown = self._send_bound_request(
            lifecycle,
            {
                "operation": "shutdown",
                "process_id": pid,
                "launch_id": lifecycle["launch_id"],
            },
            require_after=False,
        )
        shutdown_result = shutdown.get("result") if shutdown.get("ok") else None
        if shutdown_result != {
            "status": "stopping",
            "mission_id": self.mission_id,
            "process_id": pid,
            "launch_id": lifecycle["launch_id"],
        }:
            raise ControlServiceError("Fleet Control rejected authenticated shutdown")
        receipt = self._wait_for_quiescence(lifecycle, require_receipt=True)
        assert receipt is not None
        return self._record_stopped(lifecycle, stopped_at=receipt["stopped_at"])


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "_gate-exec":
        return _gate_exec(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--preset")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start")
    sub.add_parser("health")
    sub.add_parser("stop")
    sub.add_parser("stop-if-present")
    request = sub.add_parser("request")
    request.add_argument("--json", required=True)
    args = parser.parse_args(argv)
    try:
        lifecycle = ControlLifecycle(
            Path(args.runs_dir), args.mission_id, preset=args.preset
        )
        if args.command == "start":
            value = lifecycle.start()
        elif args.command == "health":
            value = lifecycle.health()
        elif args.command == "stop":
            value = lifecycle.stop()
        elif args.command == "stop-if-present":
            value = lifecycle.stop_if_present()
        else:
            payload = fleet_json.loads(args.json)
            if not isinstance(payload, dict):
                raise ControlServiceError("request JSON must be an object")
            value = send_request(lifecycle.socket_path, payload)
        print(fleet_json.canonical_bytes(value).decode("utf-8"))
        return 0
    except (ControlServiceError, OSError, ValueError) as exc:
        print(f"fleet-control-service: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
