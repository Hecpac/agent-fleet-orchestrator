"""Opt-in launcher/native bootstrap bridge. Tool requests are denied.

This is an observation channel, not a complete effective-policy attestation.
The broker is trusted CONTROL code, forked before the wrapper execs Codex. No
model transport, ambient endpoint, key file, or stdout parser participates.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import os
from pathlib import Path
import select
import signal
import socket
import sys
import time

import fleet_artifacts as artifacts
import fleet_herdr_binding as identity
import fleet_herdr_binding_v2 as binding
import fleet_json
import fleet_mission_state as state
import fleet_native_spawn_channel as protocol
import fleet_safe_paths as paths


class NativeError(RuntimeError):
    pass


def process_image(pid):
    """OS-observed current path; does not attest birth or mapped image bytes."""
    if sys.platform != "darwin":
        raise NativeError("native launcher observation currently requires macOS")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    function = library.proc_pidpath
    function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    if function(pid, buffer, len(buffer)) <= 0:
        raise NativeError("process image unavailable")
    return os.fsdecode(buffer.value)


@contextmanager
def bounded_transaction():
    # Runs only in the dedicated single-threaded broker. Bound even blocking
    # ledger/CAS locks; never change the default MissionTransaction semantics.
    def expired(_signum, _frame):
        raise TimeoutError("native durability deadline")
    old = signal.signal(signal.SIGALRM, expired)
    previous = signal.setitimer(signal.ITIMER_REAL, 2.5)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous)
        signal.signal(signal.SIGALRM, old)


class Publisher:
    def __init__(self, capsule, intent_path, intent_pin, launch_pin, pid):
        self.capsule, self.intent_path, self.intent_pin = capsule, Path(intent_path), intent_pin
        self.launch_pin, self.pid = launch_pin, pid
        self.runs, self.attempt = Path(capsule["runs"]), capsule["attempt"]
        self.mid = self.attempt["mission_id"]
        self.tx = None

    def active(self, current):
        relative = Path("missions") / self.mid / "herdr-backend.json"
        with paths.RootedFS(self.runs) as fs:
            backend = fleet_json.loads(fs.read_regular(relative,
                directory_modes=(0o700, 0o700), file_mode=0o600, max_bytes=4*1024*1024))
        members = [m for m in backend["members"] if m["instance_id"] == self.attempt["role"]]
        member = members[0] if len(members) == 1 else None
        if (backend["mission_id"] != self.mid or backend["generation"] != self.attempt["generation"]
                or not member or member["generation"] != self.attempt["generation"]
                or member["start_phase"] not in {"starting", "started"}
                or member["start_attempts"][-1]["attempt_id"] != self.attempt["attempt_id"]
                or member["start_attempts"][-1].get("launch_intent") != {
                    "path": str(self.intent_path), "sha256": self.intent_pin}):
            raise NativeError("inactive native attempt")
        if (current["status"] not in {"booting", "running"}
                or current.get("herdr_control", {}).get("desired", "running") != "running"
                or datetime.now(timezone.utc) >= state.parse_timestamp(
                    current["admission_policy"]["deadline_at"], "native deadline")):
            raise NativeError("native observation no longer admitted")
        frozen = artifacts.get_bytes(self.runs, self.mid, self.capsule["frozen_policy_sha256"])
        checked = binding.validate_frozen(frozen, pinned_sha256=self.capsule["frozen_policy_sha256"],
                                         active_attempt=self.attempt)
        if (checked["candidate"] != self.capsule["candidate"] or checked["codex"] != self.capsule["codex"]
                or checked["requested_policy"]["policy"] != self.capsule["requested_policy"]
                or identity.path_identity(self.capsule["candidate"]["realpath"], directory=True) != checked["candidate"]
                or identity.path_identity(checked["codex"]["image"]["realpath"], directory=False) != checked["codex"]["image"]):
            raise NativeError("native frozen inputs changed")
        return backend

    @contextmanager
    def transaction(self):
        with bounded_transaction(), state.MissionTransaction(self.runs, self.mid) as tx:
            self.tx = tx
            try:
                self.backend = self.active(tx.current_state)
                self.observed_image = process_image(self.pid)
                if self.observed_image != self.capsule["codex"]["image"]["realpath"]:
                    raise NativeError("native process image differs")
                # Do not acquire herdr-backend.lock here: boot owns it while it
                # waits for this handshake. HELLO grants no execution authority;
                # backend readiness independently rechecks the active attempt.
                yield
            finally:
                self.tx = None

    def run_link(self):
        candidates = [s for s in self.backend.get("submissions", {}).values()
            if s.get("instance_id") == "worker" and s.get("generation") == self.attempt["generation"]
            and s.get("phase") in {"prepared", "submitted"}]
        if len(candidates) != 1:
            return None, None
        submission = candidates[0]
        pin = submission.get("launch_run_link_artifact_id")
        if pin is None:
            return None, None
        link = fleet_json.loads(artifacts.get_bytes(self.runs, self.mid, pin))
        if link != {"schema_version": 1, "kind": "launch-run-link", "attempt": self.attempt,
                    "run_id": submission["run_id"], "prompt_sha256": submission["prompt_sha256"],
                    "launch_observation_sha256": self.launch_pin, "authority": "none",
                    "INTEGRATION_BINDING": "NOT_VERIFIED"}:
            raise NativeError("native run link differs")
        admissions = [a for a in self.tx.current_state["admissions"].values()
            if a["run_id"] == submission["run_id"] and a["phase"] in {"authorized", "started"}
            and a["active"] and a["writer"]]
        if len(admissions) != 1 or self.tx.current_state["active_writer"] != admissions[0]["admission_id"]:
            raise NativeError("native run lacks its writer admission")
        return submission["run_id"], pin

    def commit(self, receipt):
        if self.tx is None:
            raise NativeError("native receipt outside transaction")
        request = receipt["request"]
        if receipt["decision"] == "allow" and request["kind"] != "native-spawn-hello":
            raise NativeError("effective native policy authorization is unavailable")
        run_id, link = self.run_link() if request["kind"] != "native-spawn-hello" else (None, None)
        record = {"schema_version": 1, "kind": "fleet-native-observation", "authority": "none",
            "INTEGRATION_BINDING": "NOT_VERIFIED", "attempt": self.attempt, "run_id": run_id,
            "run_link_artifact_id": link, "intent_sha256": self.intent_pin,
            "launch_observation_artifact_id": self.launch_pin,
            "frozen_policy_sha256": self.capsule["frozen_policy_sha256"],
            "candidate": self.capsule["candidate"], "codex": self.capsule["codex"],
            "observed_process_image_path": self.observed_image,
            "ledger_predecessor_sha256": self.tx.head_sha256, "receipt": receipt,
            "missing": ["native_effective_environment", "public_os_sandbox_projection",
                "process_birth_and_mapped_image", "independent_external_effects", "all_effect_paths",
                "authenticated_turn_to_fleet_run"]}
        pin = artifacts.put_bytes(self.runs, self.mid, fleet_json.canonical_bytes(record))["artifact_id"]
        self.tx.append_event(kind="herdr_native_observed", actor="CONTROL",
            idempotency_key=f"native:{request['channel_id']}:{request['sequence']}",
            payload={"attempt": self.attempt, "run_id": run_id, "artifact_id": pin,
                "channel_id": request["channel_id"], "sequence": request["sequence"],
                "decision": receipt["decision"], "authority": "none"})
        return pin


def require_bootstrap(runs, mid, member, launch_pin):
    """Called by real backend before readiness and again before prompt dispatch."""
    attempt = member["start_attempts"][-1]
    expected = {"mission_id": mid, "generation": member["generation"], "role": member["instance_id"],
                "attempt_id": attempt["attempt_id"]}
    current = state.derive_state(state.read_events(state.ledger_path(runs, mid), expected_mission_id=mid))
    if (current["status"] not in {"booting", "running"}
            or current.get("herdr_control", {}).get("desired", "running") != "running"
            or datetime.now(timezone.utc) >= state.parse_timestamp(current["admission_policy"]["deadline_at"], "native deadline")):
        raise NativeError("native bootstrap no longer admitted")
    matches = [p for p in current.get("herdr_native_observations", [])
               if p["attempt"] == expected and p["sequence"] == 0 and p["decision"] == "allow"]
    if len(matches) != 1 or any(p["attempt"] == expected and p["decision"] == "deny"
                               for p in current.get("herdr_native_observations", [])):
        raise NativeError("unique native bootstrap missing")
    payload = matches[0]
    record = fleet_json.loads(artifacts.get_bytes(runs, mid, payload["artifact_id"]))
    launch = fleet_json.loads(artifacts.get_bytes(runs, mid, launch_pin))
    receipt = record["receipt"]
    request = receipt["request"]
    if (record["schema_version"] != 1 or record["kind"] != "fleet-native-observation" or record["authority"] != "none"
            or record["attempt"] != expected or record["launch_observation_artifact_id"] != launch_pin
            or record["intent_sha256"] != attempt["launch_intent"]["sha256"]
            or record["run_id"] is not None or record["run_link_artifact_id"] is not None
            or record["candidate"] != launch["candidate"] or record["codex"] != launch["codex"]
            or record["frozen_policy_sha256"] != launch["frozen_policy_sha256"]
            or launch.get("native_observer") != "worker-v1" or launch["attempt"] != expected
            or request != {"schema_version": 1, "authority": "none", "kind": "native-spawn-hello",
                "channel_id": payload["channel_id"], "sequence": 0, "process_id": launch["launcher_pid"],
                "launch_sha256": launch_pin}
            or receipt["decision"] != "allow" or receipt["request_sha256"] != protocol.sha(request)
            or record["observed_process_image_path"] != launch["codex"]["image"]["realpath"]
            or process_image(launch["launcher_pid"]) != launch["codex"]["image"]["realpath"]):
        raise NativeError("native bootstrap binding differs")
    return payload["artifact_id"]


class NativeLaunch:
    def __init__(self):
        self.server = self.client = None
        self.launch_pin = None

    def open(self, capsule, path, pin):
        if self.server is not None:
            raise NativeError("native channel already opened")
        self.capsule, self.path, self.pin = capsule, Path(path), pin
        self.server, self.client = socket.socketpair()
        return f"--native-spawn-observer-fd={self.client.fileno()}"

    def bind(self, pin):
        self.launch_pin = protocol.hex32(pin)

    def start(self, env):
        if self.server is None:
            return
        if self.launch_pin is None:
            raise NativeError("native launch observation missing")
        parent = os.getpid()
        child = os.fork()
        if child == 0:
            code = 1
            owner = None
            ended_fd = None
            failure = None
            try:
                self.client.close()
                os.setsid()
                keep = self.server.fileno()
                os.closerange(0, keep)
                os.closerange(keep + 1, os.sysconf("SC_OPEN_MAX"))
                for _ in range(3):
                    os.open("/dev/null", os.O_RDWR)
                os.environ.clear()
                os.environ.update(env)
                os.chdir(self.path.parent)
                runs, mid = Path(self.capsule["runs"]), self.capsule["attempt"]["mission_id"]
                relative = self.path.parent.relative_to(runs) / "native"
                with paths.RootedFS(runs) as fs:
                    fs.atomic_write(relative / ".fleet-owned", b"native broker\n",
                        directory_modes=(0o700,)*5, file_mode=0o600, require_absent=True)
                ended_fd = os.open(runs / relative, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                publisher = Publisher(self.capsule, self.path, self.pin, self.launch_pin, parent)
                owner = protocol.ObservationChannel(self.server, runs / relative,
                    process_id=parent, launch_sha256=self.launch_pin,
                    transaction=publisher.transaction, commit=publisher.commit)
                owner._persist("broker.json", {"process_id": os.getpid(), "codex_process_id": parent,
                                                "authority": "none"})
                # Cold image startup gets a separate bound. Native exchanges
                # still have their three-second deadline once Codex enters them.
                owner.bootstrap(deadline=time.monotonic() + 30)
                while not owner.closed:
                    ready, _, _ = select.select([self.server], [], [], 0.25)
                    if ready:
                        if not self.server.recv(1, socket.MSG_PEEK):
                            break
                        owner.receive(approve=lambda _: False)
                    if os.getppid() != parent:
                        break
                code = 0
            except BaseException as exc:
                failure = type(exc).__name__
            finally:
                try:
                    if ended_fd is not None:
                        fd = os.open("broker-ended.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=ended_fd)
                        try:
                            data = protocol.canonical({"exit_code": code, "error_type": failure, "authority": "none"})
                            if os.write(fd, data) != len(data):
                                raise OSError("incomplete broker outcome")
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                        os.fsync(ended_fd)
                    if owner is not None:
                        owner.close()
                finally:
                    os._exit(code)
        self.server.close()
        self.server = None
        self.client.set_inheritable(True)

    def close(self):
        for sock in (self.server, self.client):
            if sock is not None:
                sock.close()
