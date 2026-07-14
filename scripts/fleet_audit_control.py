#!/usr/bin/env python3
"""CONTROL-owned audit writer with signed chains and S3 Object Lock anchoring."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import ctypes
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import re
import socket
import socketserver
import stat
import sys
from typing import Any, BinaryIO, Protocol
import urllib.error
import urllib.parse
import urllib.request
import uuid


GENESIS_SHA256 = "0" * 64
MAX_REQUEST_BYTES = 2_000_000
SAFE_AUDIT_EVENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SAFE_METADATA_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
FORBIDDEN_METADATA = {"prompt", "payload", "content", "secret", "credential", "environment", "raw"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def hmac_hex(key: bytes, purpose: str, value: bytes) -> str:
    return hmac.new(key, purpose.encode("utf-8") + b"\0" + value, hashlib.sha256).hexdigest()


def secure_directory(path: Path, *, owner_uid: int, mode: int) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"not a directory: {path}")
    if info.st_uid != owner_uid:
        raise RuntimeError(f"unexpected owner for {path}: {info.st_uid}")
    if stat.S_IMODE(info.st_mode) != mode:
        raise RuntimeError(f"unexpected mode for {path}: {oct(stat.S_IMODE(info.st_mode))}")


def load_control_key(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeError("CONTROL key must be a regular non-symlink file")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError("CONTROL key must be owner-only mode 0600")
    value = path.read_bytes()
    if len(value) < 32:
        raise RuntimeError("CONTROL key must contain at least 256 bits")
    return value


def peer_credentials(connection: socket.socket) -> tuple[int, int]:
    """Return BSD peer euid/egid without trusting request-supplied identity."""
    libc = ctypes.CDLL(None, use_errno=True)
    uid = ctypes.c_uint()
    gid = ctypes.c_uint()
    result = libc.getpeereid(
        ctypes.c_int(connection.fileno()), ctypes.byref(uid), ctypes.byref(gid)
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(uid.value), int(gid.value)


class WormSink(Protocol):
    compliance_mode: bool
    backend_name: str

    def object_key(self, run_id: str, sequence: int, event_id: str) -> str: ...

    def anchor(self, object_key: str, payload: bytes, event_sha256: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SignedLocalSink:
    """Signed local chain; deliberately makes no WORM durability claim."""

    compliance_mode: bool = False
    backend_name: str = "signed-local"

    def object_key(self, run_id: str, sequence: int, event_id: str) -> str:
        del run_id, sequence, event_id
        return ""

    def anchor(self, object_key: str, payload: bytes, event_sha256: str) -> dict[str, Any]:
        del object_key, payload
        return {
            "schema_version": 1,
            "worm": False,
            "backend": self.backend_name,
            "event_sha256": event_sha256,
        }


@dataclass(frozen=True)
class DirectoryTestSink:
    """Explicitly non-compliant sink used only by tests and local smoke."""

    root: Path
    compliance_mode: bool = False
    backend_name: str = "directory-test-only"

    def object_key(self, run_id: str, sequence: int, event_id: str) -> str:
        return f"{run_id}/{sequence:012d}-{event_id}.json"

    def anchor(self, object_key: str, payload: bytes, event_sha256: str) -> dict[str, Any]:
        target = self.root / object_key
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            if target.read_bytes() != payload:
                raise RuntimeError(f"test anchor collision: {object_key}")
            return {
                "schema_version": 1,
                "worm": False,
                "backend": self.backend_name,
                "object_key": object_key,
                "event_sha256": event_sha256,
            }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(target, flags, 0o400)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise RuntimeError("short write to test anchor")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        return {
            "schema_version": 1,
            "worm": False,
            "backend": self.backend_name,
            "object_key": object_key,
            "event_sha256": event_sha256,
        }


@dataclass(frozen=True)
class S3Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str = ""


@dataclass(frozen=True)
class S3ObjectLockSink:
    bucket: str
    region: str
    credentials: S3Credentials
    endpoint: str
    retention_days: int
    prefix: str = "fleet-audits"
    compliance_mode: bool = True
    backend_name: str = "s3-object-lock-compliance"

    @classmethod
    def from_environment(cls) -> "S3ObjectLockSink":
        required = {
            "FLEET_WORM_BUCKET": os.environ.get("FLEET_WORM_BUCKET", ""),
            "FLEET_WORM_REGION": os.environ.get("FLEET_WORM_REGION", "")
            or os.environ.get("AWS_REGION", "")
            or os.environ.get("AWS_DEFAULT_REGION", ""),
            "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", ""),
            "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("missing S3 Object Lock configuration: " + ", ".join(missing))
        retention_days = int(os.environ.get("FLEET_WORM_RETENTION_DAYS", "2557"))
        if retention_days < 1:
            raise RuntimeError("FLEET_WORM_RETENTION_DAYS must be positive")
        region = required["FLEET_WORM_REGION"]
        endpoint = os.environ.get(
            "FLEET_WORM_ENDPOINT", f"https://s3.{region}.amazonaws.com"
        ).rstrip("/")
        return cls(
            bucket=required["FLEET_WORM_BUCKET"],
            region=region,
            credentials=S3Credentials(
                required["AWS_ACCESS_KEY_ID"],
                required["AWS_SECRET_ACCESS_KEY"],
                os.environ.get("AWS_SESSION_TOKEN", ""),
            ),
            endpoint=endpoint,
            retention_days=retention_days,
            prefix=os.environ.get("FLEET_WORM_PREFIX", "fleet-audits").strip("/"),
        )

    def object_key(self, run_id: str, sequence: int, event_id: str) -> str:
        return f"{self.prefix}/{run_id}/{sequence:012d}-{event_id}.json"

    def _signed_request(
        self,
        method: str,
        object_key: str,
        payload: bytes,
        extra_headers: dict[str, str],
    ) -> urllib.request.Request:
        parsed = urllib.parse.urlsplit(self.endpoint)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RuntimeError("FLEET_WORM_ENDPOINT must be an HTTPS endpoint")
        encoded_key = "/".join(
            urllib.parse.quote(part, safe="-_.~") for part in object_key.split("/")
        )
        base_path = parsed.path.rstrip("/")
        canonical_uri = f"{base_path}/{urllib.parse.quote(self.bucket, safe='-_.~')}/{encoded_key}"
        request_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, canonical_uri, "", "")
        )
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()
        headers = {
            "host": parsed.netloc,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            **{name.lower(): value.strip() for name, value in extra_headers.items()},
        }
        if self.credentials.session_token:
            headers["x-amz-security-token"] = self.credentials.session_token
        signed_names = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        canonical_request = "\n".join(
            [method, canonical_uri, "", canonical_headers, signed_names, payload_hash]
        )
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )

        def sign(key: bytes, value: str) -> bytes:
            return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

        date_key = sign(("AWS4" + self.credentials.secret_access_key).encode(), date_stamp)
        region_key = sign(date_key, self.region)
        service_key = sign(region_key, "s3")
        signing_key = sign(service_key, "aws4_request")
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.credentials.access_key_id}/{scope}, "
            f"SignedHeaders={signed_names}, Signature={signature}"
        )
        request_headers = {name: value for name, value in headers.items()}
        request_headers["authorization"] = authorization
        return urllib.request.Request(
            request_url,
            data=payload if method != "HEAD" else None,
            headers=request_headers,
            method=method,
        )

    def anchor(self, object_key: str, payload: bytes, event_sha256: str) -> dict[str, Any]:
        retain_until = (
            datetime.now(timezone.utc) + timedelta(days=self.retention_days)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        headers = {
            "content-type": "application/json",
            "x-amz-object-lock-mode": "COMPLIANCE",
            "x-amz-object-lock-retain-until-date": retain_until,
            "x-amz-meta-event-sha256": event_sha256,
        }
        request = self._signed_request("PUT", object_key, payload, headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                version_id = response.headers.get("x-amz-version-id", "")
                if not version_id:
                    raise RuntimeError("S3 Object Lock response lacks version id")
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", "replace")
            raise RuntimeError(f"S3 Object Lock PUT failed: {exc.code} {detail}") from exc

        head = self._signed_request("HEAD", object_key, b"", {})
        try:
            with urllib.request.urlopen(head, timeout=30) as response:
                mode = response.headers.get("x-amz-object-lock-mode", "")
                retained = response.headers.get("x-amz-object-lock-retain-until-date", "")
                stored_digest = response.headers.get("x-amz-meta-event-sha256", "")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"S3 Object Lock HEAD failed: {exc.code}") from exc
        if mode != "COMPLIANCE" or not retained or stored_digest != event_sha256:
            raise RuntimeError("S3 object did not retain required COMPLIANCE metadata")
        return {
            "schema_version": 1,
            "worm": True,
            "backend": self.backend_name,
            "object_key": object_key,
            "event_sha256": event_sha256,
            "version_id": version_id,
            "retained_until": retained,
            "retention_mode": mode,
        }


class AuditLedger:
    def __init__(
        self, root: Path, key: bytes, sink: WormSink, *, receipt_root: Path | None = None
    ) -> None:
        self.root = root
        self.key = key
        self.sink = sink
        self.receipt_root = receipt_root
        secure_directory(root, owner_uid=os.geteuid(), mode=0o700)
        if receipt_root is not None:
            receipt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            secure_directory(receipt_root, owner_uid=os.geteuid(), mode=0o700)

    def _write_receipt(self, event_id: str, receipt: dict[str, Any]) -> None:
        if self.receipt_root is None:
            return
        path = self.receipt_root / f"{event_id}.json"
        encoded = canonical(receipt) + b"\n"
        if path.exists():
            if path.is_symlink() or path.read_bytes() != encoded:
                raise RuntimeError("audit anchor receipt conflicts")
            return
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _run_directory(self, run_id: str) -> Path:
        try:
            parsed = str(uuid.UUID(run_id))
        except ValueError as exc:
            raise RuntimeError("run_id must be a UUID") from exc
        if parsed != run_id.lower():
            raise RuntimeError("run_id must use canonical UUID form")
        run_directory = self.root / run_id
        try:
            run_directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        secure_directory(run_directory, owner_uid=os.geteuid(), mode=0o700)
        return run_directory

    def _load_and_verify(self, handle: BinaryIO) -> list[dict[str, Any]]:
        handle.seek(0)
        events: list[dict[str, Any]] = []
        previous = GENESIS_SHA256
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                raise RuntimeError(f"blank audit record at line {line_number}")
            event = json.loads(raw)
            signature = str(event.pop("control_signature", ""))
            if not hmac.compare_digest(
                signature, hmac_hex(self.key, "event-signature", canonical(event))
            ):
                raise RuntimeError(f"invalid CONTROL signature at line {line_number}")
            stored_digest = str(event.get("event_sha256", ""))
            unsigned = {k: v for k, v in event.items() if k != "event_sha256"}
            if unsigned.get("previous_event_sha256") != previous:
                raise RuntimeError(f"audit chain break at line {line_number}")
            if stored_digest != digest(unsigned):
                raise RuntimeError(f"audit digest mismatch at line {line_number}")
            event["control_signature"] = signature
            previous = stored_digest
            events.append(event)
        return events

    def read_verified(self, run_id: str) -> list[dict[str, Any]]:
        ledger_path = self._run_directory(run_id) / "a2a_ledger.jsonl"
        if not ledger_path.exists():
            return []
        with ledger_path.open("rb") as handle:
            return self._load_and_verify(handle)

    def append(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        run_directory = self._run_directory(run_id)
        ledger_path = run_directory / "a2a_ledger.jsonl"
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(ledger_path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise RuntimeError("ledger must be a CONTROL-owned regular file")
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise RuntimeError("ledger must use mode 0600")
            with os.fdopen(fd, "r+b", closefd=False) as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                events = self._load_and_verify(handle)
                requested_event_id = str(payload.get("event_id") or "")
                if requested_event_id:
                    existing = next(
                        (item for item in events if item.get("event_id") == requested_event_id),
                        None,
                    )
                    if existing is not None:
                        if any(existing.get(key) != value for key, value in payload.items()):
                            raise RuntimeError("audit event_id conflicts with existing payload")
                        return existing
                sequence = len(events) + 1
                event = dict(payload)
                event["run_id"] = run_id
                event["sequence"] = sequence
                event["previous_event_sha256"] = (
                    events[-1]["event_sha256"] if events else GENESIS_SHA256
                )
                event_id = str(event.get("event_id") or uuid.uuid4())
                event["event_id"] = event_id
                object_key = self.sink.object_key(run_id, sequence, event_id)
                event["worm_backend"] = self.sink.backend_name
                event["worm_compliance_mode"] = self.sink.compliance_mode
                event["worm_object_key"] = object_key
                event["event_sha256"] = digest(event)
                event["control_signature"] = hmac_hex(
                    self.key, "event-signature", canonical(event)
                )
                encoded = canonical(event) + b"\n"
                receipt = self.sink.anchor(object_key, encoded, event["event_sha256"])
                if not isinstance(receipt, dict):
                    raise RuntimeError("audit sink returned no anchor receipt")
                self._write_receipt(event_id, receipt)
                handle.seek(0, os.SEEK_END)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                fcntl.flock(handle, fcntl.LOCK_UN)
                return event
        finally:
            os.close(fd)


class AuditService:
    def __init__(
        self,
        ledger: AuditLedger,
        *,
        control_uid: int,
        maker_uid: int,
        human_subject: str,
        mode: str | None = None,
    ) -> None:
        self.ledger = ledger
        self.control_uid = control_uid
        self.maker_uid = maker_uid
        self.mode = mode or ("worm" if ledger.sink.compliance_mode else "signed")
        self.human_uid = "hmac-sha256:" + hmac_hex(
            ledger.key, "human-identity", human_subject.encode("utf-8")
        )

    def _run_started(self, request: dict[str, Any], peer_uid: int) -> dict[str, Any]:
        if peer_uid != self.control_uid:
            raise RuntimeError("RunStarted requires CONTROL peer UID")
        run_id = str(request.get("run_id", ""))
        artifacts = request.get("artifacts")
        if not isinstance(artifacts, dict) or not artifacts:
            raise RuntimeError("RunStarted requires artifact paths")
        attestations: dict[str, str] = {}
        for name, raw_path in sorted(artifacts.items()):
            if not isinstance(name, str) or not isinstance(raw_path, str):
                raise RuntimeError("invalid artifact attestation")
            path = Path(raw_path)
            if not path.is_absolute() or not path.is_file():
                raise RuntimeError(f"artifact is not an absolute file: {name}")
            attestations[name] = file_sha256(path)
        existing = self.ledger.read_verified(run_id)
        if existing:
            first = existing[0]
            if (
                first.get("event_type") != "RunStarted"
                or first.get("artifact_sha256") != attestations
                or first.get("model_version") != str(request.get("model_version", ""))
            ):
                raise RuntimeError("run already exists with different identity")
            return {"event_id": first["event_id"], "human_uid": self.human_uid}
        event = self.ledger.append(
            run_id,
            {
                "timestamp": utc_now(),
                "event_type": "RunStarted",
                "human_uid": self.human_uid,
                "control_uid": self.control_uid,
                "maker_uid": self.maker_uid,
                "model_version": str(request.get("model_version", "")),
                "data_classification": str(
                    request.get("data_classification", "restricted")
                ),
                "artifact_sha256": attestations,
                "event_id": str(request.get("event_id") or uuid.uuid4()),
            },
        )
        return {"event_id": event["event_id"], "human_uid": self.human_uid}

    @staticmethod
    def _safe_metadata(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or len(value) > 32:
            raise RuntimeError("control audit metadata must be a bounded object")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not SAFE_METADATA_KEY.fullmatch(key)
                or any(term in key for term in FORBIDDEN_METADATA)
            ):
                raise RuntimeError("control audit metadata key is unsafe")
            if not isinstance(item, (str, int, bool)) or isinstance(item, str) and len(item) > 256:
                raise RuntimeError("control audit metadata values must be bounded scalars")
            result[key] = item
        return result

    def _control_event(self, request: dict[str, Any], peer_uid: int) -> dict[str, Any]:
        if peer_uid != self.control_uid:
            raise RuntimeError("control event requires CONTROL peer UID")
        run_id = str(request.get("run_id", ""))
        events = self.ledger.read_verified(run_id)
        if not events or events[0].get("event_type") != "RunStarted":
            raise RuntimeError("control event has no RunStarted")
        event_type = str(request.get("event_type", ""))
        subject_id = str(request.get("subject_id", ""))
        subject_sha256 = str(request.get("subject_sha256", ""))
        event_id = str(request.get("event_id", ""))
        if not SAFE_AUDIT_EVENT.fullmatch(event_type) or not SAFE_AUDIT_EVENT.fullmatch(subject_id):
            raise RuntimeError("control audit event identity is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", subject_sha256):
            raise RuntimeError("control audit subject hash is invalid")
        try:
            event_id = str(uuid.UUID(event_id))
        except ValueError as exc:
            raise RuntimeError("control audit event_id must be a UUID") from exc
        prior = next((item for item in events if item.get("event_id") == event_id), None)
        if prior is not None:
            if (
                prior.get("event_type") != event_type
                or prior.get("subject_id") != subject_id
                or prior.get("subject_sha256") != subject_sha256
                or prior.get("metadata") != self._safe_metadata(request.get("metadata") or {})
            ):
                raise RuntimeError("control audit event_id conflicts with existing event")
            return {"event_id": prior["event_id"], "event_sha256": prior["event_sha256"]}
        event = self.ledger.append(
            run_id,
            {
                "event_id": event_id,
                "timestamp": utc_now(),
                "event_type": event_type,
                "subject_id": subject_id,
                "subject_sha256": subject_sha256,
                "metadata": self._safe_metadata(request.get("metadata") or {}),
                "human_uid": self.human_uid,
                "control_uid": self.control_uid,
                "data_classification": str(request.get("data_classification") or "restricted"),
            },
        )
        return {"event_id": event["event_id"], "event_sha256": event["event_sha256"]}

    def _hook_event(self, request: dict[str, Any], peer_uid: int) -> dict[str, Any]:
        if peer_uid != self.maker_uid:
            raise RuntimeError("hook event requires Maker peer UID")
        run_id = str(request.get("run_id", ""))
        events = self.ledger.read_verified(run_id)
        if not events or events[0].get("event_type") != "RunStarted":
            raise RuntimeError("hook event has no RunStarted")
        hook = request.get("hook")
        if not isinstance(hook, dict):
            raise RuntimeError("hook payload must be an object")
        event_type = str(hook.get("hook_event_name") or "unknown")
        tool_input = hook.get("tool_input") or {}
        args_sha256 = digest(tool_input)
        correlation_sha256 = digest(
            {
                "session_id": str(hook.get("session_id") or ""),
                "tool_name": str(hook.get("tool_name") or "unknown"),
                "args_sha256": args_sha256,
            }
        )
        payload: dict[str, Any] = {
            "timestamp": utc_now(),
            "event_type": event_type,
            "tool_name": str(hook.get("tool_name") or "unknown"),
            "tool_use_id": str(hook.get("tool_use_id") or ""),
            "session_id": str(hook.get("session_id") or ""),
            "human_uid": self.human_uid,
            "agent_uid": "_fleet_maker",
            "os_uid": peer_uid,
            "model_version": str(request.get("model_version") or "unknown"),
            "data_classification": str(
                request.get("data_classification") or "restricted"
            ),
            "args_sha256": args_sha256,
            "correlation_sha256": correlation_sha256,
        }
        if event_type == "PermissionRequest":
            payload["approval_status"] = "requested"
        if event_type in {"PostToolUse", "PostToolUseFailure"}:
            response = hook.get("tool_response") or hook.get("error") or {}
            payload["response_sha256"] = digest(response)
            payload["exit_code"] = 1 if event_type == "PostToolUseFailure" else 0
            request_event = next(
                (
                    item
                    for item in reversed(events)
                    if item.get("event_type") == "PermissionRequest"
                    and item.get("correlation_sha256") == correlation_sha256
                ),
                None,
            )
            approval_event = next(
                (
                    item
                    for item in reversed(events)
                    if item.get("event_type") == "HumanApproval"
                    and item.get("correlation_sha256") == correlation_sha256
                    and item.get("approval_status") == "approved"
                ),
                None,
            )
            payload["approval_request_reference"] = (
                request_event.get("event_id") if request_event else None
            )
            payload["human_approval_reference"] = (
                approval_event.get("event_id") if approval_event else None
            )
        event = self.ledger.append(run_id, payload)
        return {"event_id": event["event_id"]}

    def handle(self, request: dict[str, Any], peer_uid: int) -> dict[str, Any]:
        operation = str(request.get("operation", ""))
        if operation == "run_started":
            return self._run_started(request, peer_uid)
        if operation == "hook_event":
            return self._hook_event(request, peer_uid)
        if operation == "control_event":
            return self._control_event(request, peer_uid)
        if operation == "health":
            if peer_uid != self.control_uid:
                raise RuntimeError("health requires CONTROL peer UID")
            return {
                "status": "ok",
                "control_uid": self.control_uid,
                "maker_uid": self.maker_uid,
                "mode": self.mode,
                "backend": self.ledger.sink.backend_name,
            }
        if operation == "verify":
            if peer_uid != self.control_uid:
                raise RuntimeError("verification requires CONTROL peer UID")
            events = self.ledger.read_verified(str(request.get("run_id", "")))
            return {
                "records": len(events),
                "head": events[-1]["event_sha256"] if events else GENESIS_SHA256,
                "worm_compliance_mode": bool(events)
                and all(item.get("worm_compliance_mode") is True for item in events),
            }
        raise RuntimeError(f"unsupported operation: {operation}")


class AuditRequestHandler(socketserver.StreamRequestHandler):
    server: "AuditUnixServer"

    def handle(self) -> None:
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise RuntimeError("invalid or oversized request")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise RuntimeError("request must be a JSON object")
            peer_uid, _ = peer_credentials(self.request)
            result = self.server.audit_service.handle(request, peer_uid)
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write(canonical(response) + b"\n")


class AuditUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: str, audit_service: AuditService) -> None:
        self.audit_service = audit_service
        super().__init__(socket_path, AuditRequestHandler)


def send_request(socket_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(canonical(request) + b"\n")
        response_file = client.makefile("rb")
        raw = response_file.readline(MAX_REQUEST_BYTES + 1)
    response = json.loads(raw)
    if not response.get("ok"):
        raise RuntimeError(str(response.get("error") or "audit request failed"))
    return dict(response.get("result") or {})


def serve(args: argparse.Namespace) -> int:
    key = load_control_key(Path(args.key))
    root = Path(args.root)
    maker_uid = pwd.getpwnam(args.maker_user).pw_uid
    if args.mode == "test" or args.allow_local_test:
        if os.environ.get("FLEET_AUDIT_TEST_MODE") != "1":
            raise RuntimeError("local test sink requires FLEET_AUDIT_TEST_MODE=1")
        sink: WormSink = DirectoryTestSink(Path(args.test_anchor_root))
        Path(args.test_anchor_root).mkdir(parents=True, exist_ok=True, mode=0o700)
    elif args.mode == "signed":
        sink = SignedLocalSink()
    else:
        sink = S3ObjectLockSink.from_environment()
    ledger = AuditLedger(
        root,
        key,
        sink,
        receipt_root=Path(args.receipt_root) if args.receipt_root else None,
    )
    service = AuditService(
        ledger,
        control_uid=os.geteuid(),
        maker_uid=maker_uid,
        human_subject=os.environ.get("FLEET_HUMAN_SUBJECT")
        or pwd.getpwuid(os.geteuid()).pw_name,
        mode="test" if args.allow_local_test else args.mode,
    )
    socket_path = Path(args.socket)
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o711)
    secure_directory(socket_path.parent, owner_uid=os.geteuid(), mode=0o711)
    if socket_path.exists() or socket_path.is_symlink():
        socket_path.unlink()
    server = AuditUnixServer(str(socket_path), service)
    os.chmod(socket_path, 0o666)
    try:
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
    finally:
        server.server_close()
        if socket_path.exists():
            socket_path.unlink()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--socket", required=True)
    serve_parser.add_argument("--root", required=True)
    serve_parser.add_argument("--key", required=True)
    serve_parser.add_argument("--maker-user", default="_fleet_maker")
    serve_parser.add_argument("--mode", choices=("signed", "worm", "test"), default="worm")
    serve_parser.add_argument("--receipt-root", default="")
    serve_parser.add_argument("--allow-local-test", action="store_true")
    serve_parser.add_argument("--test-anchor-root", default="")
    request_parser = subparsers.add_parser("request")
    request_parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    if args.command == "serve":
        if (args.allow_local_test or args.mode == "test") and not args.test_anchor_root:
            parser.error("--test-anchor-root is required with --allow-local-test")
        return serve(args)
    try:
        request = json.load(sys.stdin)
        result = send_request(Path(args.socket), request)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"audit request failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
