"""Private native-spawn observation protocol; not Fleet acceptance authority.

The trusted caller owns an anonymous socketpair, supplies independently observed
PID/launch pins, and must validate policy before allowing a request. This module
does not launch an agent or establish all-effect-path sandbox coverage.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import struct
import time

import fleet_json

LIMIT = 1024 * 1024
REQUEST = b"native-spawn/request/v1\0"
ACK = b"native-spawn/ack/v1\0"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def hex32(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("invalid channel digest")
    return value


def signed(key, domain, payload):
    return {"payload": payload, "hmac_sha256": hmac.new(key, domain + canonical(payload), "sha256").hexdigest()}


def write_frame(sock, value, deadline):
    body = canonical(value)
    if not 0 < len(body) <= LIMIT:
        raise ValueError("channel frame size")
    data = memoryview(struct.pack("!I", len(body)) + body)
    while data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("channel deadline")
        sock.settimeout(remaining)
        count = sock.send(data)
        if count == 0:
            raise EOFError("channel closed")
        data = data[count:]


def read_frame(sock, deadline):
    body = bytearray()
    for size in (4, None):
        expected = size if size else struct.unpack("!I", body)[0]
        if not 0 < expected <= LIMIT:
            raise ValueError("channel frame size")
        body = bytearray()
        while len(body) < expected:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("channel deadline")
            sock.settimeout(remaining)
            block = sock.recv(expected - len(body))
            if not block:
                raise EOFError("channel closed")
            body.extend(block)
    value = fleet_json.loads(body)
    if canonical(value) != body:
        raise ValueError("noncanonical channel frame")
    return value


class ObservationChannel:
    """One owner, one process, one journal; errors permanently close the channel.

    Caller creates a fresh directory outside Worker-writable roots. Journal
    durability is fsync-before-ACK. A trusted optional commit callback supplies
    the independent CAS/ledger anchor inside the same transaction as the ACK.
    Keys exist only in these two trusted processes and are never persisted.
    """

    def __init__(self, sock, directory, *, process_id, launch_sha256, transaction=nullcontext, commit=None):
        self.transaction, self.commit = transaction, commit
        self.sock = sock
        self.directory = Path(directory)
        self.root_fd = None
        self.closed = False
        self.sequence = 0
        self.previous = None
        self.key = os.urandom(32)
        self.channel_id = os.urandom(32).hex()
        self.process_id = process_id
        try:
            self.launch_sha256 = hex32(launch_sha256)
            if type(process_id) is not int or process_id <= 0:
                raise ValueError("invalid process identity")
            if self.directory.resolve(strict=True) != self.directory:
                raise ValueError("journal path alias")
            if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM or sock.getsockname() or sock.getpeername():
                raise ValueError("anonymous connected Unix stream required")
            self.root_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(self.root_fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("private journal required")
            self._persist("owner.json", {"schema_version":1, "kind":"native-spawn-journal-owner",
                "authority":"none", "channel_id":self.channel_id, "launch_sha256":self.launch_sha256,
                "process_id":process_id})
        except BaseException:
            self.close()
            raise

    def _persist(self, name, value):
        data = canonical(value)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.root_fd)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("journal write failed")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self.root_fd)
        return hashlib.sha256(data).hexdigest()

    def bootstrap(self, *, deadline=None):
        try:
            deadline = time.monotonic() + 3 if deadline is None else deadline
            write_frame(self.sock, {"schema_version":1, "kind":"native-spawn-bootstrap",
                "key_hex":self.key.hex(), "channel_id":self.channel_id,
                "launch_sha256":self.launch_sha256}, deadline)
            return self.receive(approve=lambda request: request["kind"] == "native-spawn-hello", deadline=deadline)
        except BaseException:
            self.close()
            raise

    def receive(self, *, approve, deadline=None):
        if self.closed:
            raise RuntimeError("channel consumed")
        try:
            deadline = time.monotonic() + 3 if deadline is None else deadline
            frame = read_frame(self.sock, deadline)
            if not isinstance(frame, dict) or set(frame) != {"payload", "hmac_sha256"}:
                raise ValueError("invalid authenticated frame")
            payload = frame["payload"]
            signature = hex32(frame["hmac_sha256"])
            if not hmac.compare_digest(signature, signed(self.key, REQUEST, payload)["hmac_sha256"]):
                raise ValueError("channel authentication failed")
            expected = {"schema_version":1, "authority":"none", "channel_id":self.channel_id,
                "launch_sha256":self.launch_sha256, "process_id":self.process_id, "sequence":self.sequence,
                "kind":"native-spawn-hello" if self.sequence == 0 else "native-spawn-request"}
            if not isinstance(payload, dict) or any(type(payload.get(k)) is not type(v) or payload.get(k) != v for k,v in expected.items()):
                raise ValueError("channel binding mismatch")
            if self.sequence == 0 and payload != expected:
                raise ValueError("invalid hello")
            # The optional trusted transaction spans decision, durability and ACK.
            with self.transaction():
                # Default caller should deny. A policy predicate is supplied only by
                # trusted code, never a field in the Worker request or stdout.
                decision = "allow" if approve(payload) is True else "deny"
                receipt = {"schema_version":1, "kind":"native-spawn-observation-receipt", "authority":"none",
                    "request":payload, "request_sha256":sha(payload), "decision":decision,
                    "previous_receipt_sha256":self.previous}
                pin = self._persist(f"{self.sequence:016d}.json", receipt)
                if self.commit is not None:
                    pin = hex32(self.commit(receipt))
                    self._persist(f"{self.sequence:016d}-anchor.json", {"artifact_id":pin})
                ack = {"schema_version":1, "kind":"native-spawn-ack", "channel_id":self.channel_id,
                    "launch_sha256":self.launch_sha256, "sequence":self.sequence,
                    "request_sha256":sha(payload), "decision":decision, "durable_receipt_sha256":pin}
                write_frame(self.sock, signed(self.key, ACK, ack), deadline)
            self.previous = pin
            self.sequence += 1
            if decision != "allow":
                self.close()
            return receipt
        except BaseException:
            self.close()
            raise

    def close(self):
        self.closed = True
        self.sock.close()
        if self.root_fd is not None:
            os.close(self.root_fd)
            self.root_fd = None
        self.key = b""
