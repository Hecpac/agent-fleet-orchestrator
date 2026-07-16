#!/usr/bin/env python3
"""Mission-scoped lifecycle, client, and offline verifier for AuditService."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

import fleet_audit_control as audit
import fleet_mission
import fleet_mission_state as mission_state


ROOT = Path(__file__).resolve().parents[1]
_LIVE_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
AUDIT_RECEIPT_FIELDS = {
    "schema_version", "mission_id", "records", "head_sha256", "ledger_sha256",
    "worm", "backend", "trust_scope", "public_key_sha256", "verified_at",
    "anchor_receipts_sha256", "ed25519_signature",
}


class AuditClientError(RuntimeError):
    """Audit lifecycle or evidence failed closed."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditClientError(f"cannot load audit lifecycle file: {path.name}") from exc
    if not isinstance(value, dict):
        raise AuditClientError("audit lifecycle file must contain an object")
    return value


def _anchor_envelope(
    events: list[dict[str, Any]], anchor_receipts: Path, *, exact: bool = True
) -> tuple[list[dict[str, Any]], str]:
    try:
        root_info = anchor_receipts.lstat()
    except OSError as exc:
        raise AuditClientError("audit anchor receipt directory is missing") from exc
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise AuditClientError("audit anchor receipt directory is unsafe")
    expected_names = {f"{event['event_id']}.json" for event in events}
    if exact:
        actual_names = {path.name for path in anchor_receipts.iterdir()}
        if actual_names != expected_names:
            raise AuditClientError("audit anchor receipt set differs from ledger")
    envelope: list[dict[str, Any]] = []
    for event in events:
        path = anchor_receipts / f"{event['event_id']}.json"
        try:
            info = path.lstat()
        except OSError as exc:
            raise AuditClientError("audit anchor receipt is missing") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise AuditClientError("audit anchor receipt is not a regular file")
        envelope.append({"event_id": event["event_id"], "receipt": _load_json(path)})
    return envelope, mission_state.sha256(envelope)


def _run(command: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditClientError(f"cannot run {Path(command[0]).name}: {exc}") from exc
    if result.returncode != 0:
        raise AuditClientError(
            f"{Path(command[0]).name} failed: {result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result


def _write_once(path: Path, content: bytes, mode: int) -> None:
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or path.read_bytes() != content:
            raise AuditClientError(f"audit file conflicts: {path.name}")
        os.chmod(path, mode)
        return
    mission_state.atomic_write(path, content, mode=mode)


def _generate_keys(root: Path) -> tuple[Path, Path, Path]:
    hmac_key = root / "control-hmac.key"
    private_key = root / "audit-signing-private.pem"
    public_key = root / "audit-signing-public.pem"
    if not hmac_key.exists():
        mission_state.atomic_write(hmac_key, secrets.token_bytes(32), mode=0o600)
    if not private_key.exists():
        temporary = root / ".audit-signing-private.pem.tmp"
        _run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(temporary)])
        os.chmod(temporary, 0o600)
        os.replace(temporary, private_key)
    os.chmod(private_key, 0o600)
    if not public_key.exists():
        result = _run(["openssl", "pkey", "-in", str(private_key), "-pubout"])
        mission_state.atomic_write(public_key, result.stdout, mode=0o644)
    os.chmod(public_key, 0o644)
    return hmac_key, private_key, public_key


def _chain_without_secret(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous = audit.GENESIS_SHA256
    try:
        rows = path.read_bytes().splitlines(keepends=True)
    except OSError as exc:
        raise AuditClientError("signed audit ledger is missing") from exc
    for number, raw in enumerate(rows, 1):
        if not raw.endswith(b"\n"):
            raise AuditClientError(f"partial signed audit record at line {number}")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AuditClientError(f"invalid signed audit JSON at line {number}") from exc
        if not isinstance(event, dict) or not event.get("control_signature"):
            raise AuditClientError(f"signed audit record lacks signature at line {number}")
        unsigned_signature = {key: value for key, value in event.items() if key != "control_signature"}
        stored = str(unsigned_signature.get("event_sha256", ""))
        unsigned_hash = {
            key: value for key, value in unsigned_signature.items() if key != "event_sha256"
        }
        if unsigned_hash.get("previous_event_sha256") != previous:
            raise AuditClientError(f"signed audit chain break at line {number}")
        if stored != audit.digest(unsigned_hash):
            raise AuditClientError(f"signed audit digest mismatch at line {number}")
        previous = stored
        events.append(event)
    if not events:
        raise AuditClientError("signed audit ledger is empty")
    return events


def read_verified_public_chain(path: Path) -> list[dict[str, Any]]:
    """Read the digest/signature-bearing public chain without CONTROL secrets."""
    return _chain_without_secret(path)


def _receipt_unsigned(receipt: dict[str, Any]) -> bytes:
    return audit.canonical({key: value for key, value in receipt.items() if key != "ed25519_signature"})


def _sign_receipt(private_key: Path, receipt: dict[str, Any]) -> str:
    input_path = private_key.parent / f".audit-sign-input.{uuid.uuid4().hex}.tmp"
    try:
        mission_state.atomic_write(input_path, _receipt_unsigned(receipt), mode=0o600)
        result = _run(
            [
                "openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(private_key),
                "-in", str(input_path),
            ]
        )
        return base64.b64encode(result.stdout).decode("ascii")
    finally:
        input_path.unlink(missing_ok=True)


def _verify_signature(public_key: Path, receipt: dict[str, Any]) -> None:
    try:
        signature = base64.b64decode(receipt["ed25519_signature"], validate=True)
    except (KeyError, ValueError) as exc:
        raise AuditClientError("audit receipt signature is invalid") from exc
    signature_path = public_key.parent / ".audit-signature.verify.tmp"
    input_path = public_key.parent / ".audit-receipt.verify.tmp"
    try:
        mission_state.atomic_write(signature_path, signature, mode=0o600)
        mission_state.atomic_write(input_path, _receipt_unsigned(receipt), mode=0o600)
        _run(
            [
                "openssl", "pkeyutl", "-verify", "-pubin", "-rawin",
                "-inkey", str(public_key), "-sigfile", str(signature_path),
                "-in", str(input_path),
            ]
        )
    finally:
        signature_path.unlink(missing_ok=True)
        input_path.unlink(missing_ok=True)


def verify_offline(
    ledger_path: Path,
    receipt_path: Path,
    public_key: Path,
    anchor_receipts: Path,
    *,
    require_worm: bool,
    required_trust_scope: str | None = None,
) -> dict[str, Any]:
    events = _chain_without_secret(ledger_path)
    receipt = _load_json(receipt_path)
    if set(receipt) != AUDIT_RECEIPT_FIELDS or receipt["schema_version"] != 1:
        raise AuditClientError("audit verification receipt fields are invalid")
    if receipt["records"] != len(events) or receipt["head_sha256"] != events[-1]["event_sha256"]:
        raise AuditClientError("audit verification receipt chain summary mismatch")
    run_ids = {event.get("run_id") for event in events}
    if len(run_ids) != 1 or receipt["mission_id"] not in run_ids:
        raise AuditClientError("audit verification receipt mission differs from ledger")
    if receipt["ledger_sha256"] != audit.file_sha256(ledger_path):
        raise AuditClientError("audit verification receipt ledger hash mismatch")
    if receipt["public_key_sha256"] != audit.file_sha256(public_key):
        raise AuditClientError("audit verification public key hash mismatch")
    _verify_signature(public_key, receipt)
    envelope, envelope_sha256 = _anchor_envelope(events, anchor_receipts)
    if receipt["anchor_receipts_sha256"] != envelope_sha256:
        raise AuditClientError("audit verification anchor envelope hash mismatch")
    compliance = all(event.get("worm_compliance_mode") is True for event in events)
    if any(event.get("worm_compliance_mode") is not compliance for event in events):
        raise AuditClientError("audit ledger mixes WORM compliance modes")
    backends = {event.get("worm_backend") for event in events}
    trust_scopes = {event.get("worm_trust_scope") for event in events}
    if len(backends) != 1 or receipt["backend"] not in backends:
        raise AuditClientError("audit receipt backend differs from ledger")
    if not isinstance(receipt["backend"], str) or not receipt["backend"]:
        raise AuditClientError("audit receipt backend is invalid")
    if len(trust_scopes) != 1 or receipt["trust_scope"] not in trust_scopes:
        raise AuditClientError("audit receipt trust scope differs from ledger")
    trust_scope = str(receipt["trust_scope"])
    if trust_scope not in audit.TRUST_SCOPES:
        raise AuditClientError("audit receipt trust scope is invalid")
    if not isinstance(receipt["worm"], bool) or receipt["worm"] != compliance:
        raise AuditClientError("audit receipt WORM claim differs from ledger")
    if require_worm and not compliance:
        raise AuditClientError("workflow requires WORM compliance")
    if required_trust_scope is not None and trust_scope != required_trust_scope:
        raise AuditClientError(
            f"workflow requires {required_trust_scope} trust, receipt has {trust_scope}"
        )
    if trust_scope == "external-compliance" and not compliance:
        raise AuditClientError("external-compliance trust requires WORM compliance")
    for event, envelope_item in zip(events, envelope, strict=True):
        anchor = envelope_item["receipt"]
        anchor_required = {
            "schema_version", "worm", "backend", "trust_scope", "object_key",
            "event_sha256",
        }
        if compliance:
            worm_fields = {"retention_mode", "retained_until", "version_id"}
            if not worm_fields.issubset(anchor):
                raise AuditClientError("WORM anchor receipt is incomplete")
            anchor_required |= worm_fields
        if set(anchor) != anchor_required or anchor.get("schema_version") != 1:
            raise AuditClientError("audit anchor receipt fields are invalid")
        exact_fields = {
            "event_sha256": "event_sha256",
            "backend": "worm_backend",
            "trust_scope": "worm_trust_scope",
            "object_key": "worm_object_key",
            "worm": "worm_compliance_mode",
        }
        if compliance:
            exact_fields["retention_mode"] = "worm_retention_mode"
        if any(anchor.get(receipt_key) != event.get(event_key) for receipt_key, event_key in exact_fields.items()):
            raise AuditClientError("audit anchor receipt differs from ledger")
        if compliance and (
            anchor.get("worm") is not True
            or anchor.get("retention_mode") != "COMPLIANCE"
            or not anchor.get("version_id")
            or not anchor.get("retained_until")
        ):
            raise AuditClientError("WORM anchor receipt is incomplete")
        if not compliance and anchor.get("worm") is not False:
            raise AuditClientError("signed audit receipt makes a WORM claim")
        if not isinstance(anchor["object_key"], str) or not isinstance(
            anchor["event_sha256"], str
        ):
            raise AuditClientError("audit anchor receipt identity is invalid")
        if compliance and (
            not anchor["object_key"]
            or not isinstance(anchor["version_id"], str)
            or anchor["version_id"] in {"", "null"}
            or not isinstance(anchor["retained_until"], str)
            or not anchor["retained_until"]
        ):
            raise AuditClientError("WORM anchor receipt identity is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", anchor["event_sha256"]):
            raise AuditClientError("audit anchor receipt digest is invalid")
        if compliance:
            try:
                retained_at = datetime.fromisoformat(
                    anchor["retained_until"].replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise AuditClientError("WORM retain-until is invalid") from exc
            if retained_at.tzinfo is None:
                raise AuditClientError("WORM retain-until must include a timezone")
    return {
        "mission_id": receipt["mission_id"],
        "records": len(events),
        "head_sha256": events[-1]["event_sha256"],
        "worm": compliance,
        "backend": receipt["backend"],
        "trust_scope": trust_scope,
        "valid": True,
    }


def _validate_prior_receipt(
    prior: dict[str, Any],
    events: list[dict[str, Any]],
    ledger_path: Path,
    public_key: Path,
    anchor_receipts: Path,
    *,
    mission_id: str,
    compliance: bool,
    backend: str,
    trust_scope: str,
) -> int:
    if set(prior) != AUDIT_RECEIPT_FIELDS or prior.get("schema_version") != 1:
        raise AuditClientError("prior audit verification receipt fields are invalid")
    _verify_signature(public_key, prior)
    records = prior.get("records")
    if isinstance(records, bool) or not isinstance(records, int) or not 1 <= records <= len(events):
        raise AuditClientError("prior audit verification receipt length is invalid")
    prefix = events[:records]
    if any(
        (
            prior.get("mission_id") != mission_id,
            prior.get("head_sha256") != prefix[-1]["event_sha256"],
            prior.get("worm") is not compliance,
            prior.get("backend") != backend,
            prior.get("trust_scope") != trust_scope,
            prior.get("public_key_sha256") != audit.file_sha256(public_key),
        )
    ):
        raise AuditClientError("prior audit verification receipt identity conflicts")
    try:
        ledger_rows = ledger_path.read_bytes().splitlines(keepends=True)
    except OSError as exc:
        raise AuditClientError("signed audit ledger is missing") from exc
    prefix_sha256 = hashlib.sha256(b"".join(ledger_rows[:records])).hexdigest()
    if prior.get("ledger_sha256") != prefix_sha256:
        raise AuditClientError("prior audit verification receipt is not a ledger prefix")
    _, anchors_sha256 = _anchor_envelope(prefix, anchor_receipts, exact=False)
    if prior.get("anchor_receipts_sha256") != anchors_sha256:
        raise AuditClientError("prior audit receipt anchor prefix conflicts")
    return records


class AuditLifecycle:
    def __init__(self, runs_dir: Path, mission_id: str) -> None:
        self.runs_dir = runs_dir.resolve()
        self.mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
        self.mission_root = mission_state.mission_root(self.runs_dir, self.mission_id)
        self.root = self.mission_root / "audit"
        self.lifecycle_path = self.root / "lifecycle.json"
        socket_name = hashlib.sha256(
            f"{self.runs_dir}:{self.mission_id}".encode("utf-8")
        ).hexdigest()[:24]
        self.socket_dir = Path(tempfile.gettempdir()) / f"fleet-audit-{os.geteuid()}"
        self.socket_path = self.socket_dir / f"{socket_name}.sock"
        self.ledger_root = self.root / "ledgers"
        self.anchor_receipts = self.root / "anchor-receipts"
        self.receipt_path = self.root / "audit-verification.json"
        self.hmac_key = self.root / "control-hmac.key"
        self.private_key = self.root / "audit-signing-private.pem"
        self.public_key = self.root / "audit-signing-public.pem"
        self._process: subprocess.Popen[bytes] | None = None

    def _compiled(self) -> dict[str, Any]:
        try:
            return fleet_mission.validate_compiled(
                json.loads((self.mission_root / "compiled-workflow.json").read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise AuditClientError("cannot load compiled audit policy") from exc

    def _mode(self) -> str:
        mode = str(self._compiled()["workflow"]["audit"]["mode"])
        if mode not in {"signed", "worm"}:
            raise AuditClientError(f"unsupported Mission audit mode: {mode}")
        return mode

    def _trust_scope(self) -> str:
        trust_scope = str(self._compiled()["workflow"]["audit"]["trust_scope"])
        if trust_scope not in audit.TRUST_SCOPES:
            raise AuditClientError(f"unsupported Mission audit trust scope: {trust_scope}")
        return trust_scope

    def _lifecycle(self) -> dict[str, Any]:
        return _load_json(self.lifecycle_path)

    def health(self) -> dict[str, Any]:
        try:
            result = audit.send_request(self.socket_path, {"operation": "health"})
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            raise AuditClientError(f"AuditService health failed: {exc}") from exc
        lifecycle = self._lifecycle()
        if (
            result.get("status") != "ok"
            or result.get("mode") != lifecycle.get("mode")
            or result.get("trust_scope") != lifecycle.get("trust_scope")
        ):
            raise AuditClientError("AuditService health identity mismatch")
        return result

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def preflight(self) -> dict[str, Any]:
        """Validate an audit backend before any fleet or key side effect."""
        mode = self._mode()
        trust_scope = self._trust_scope()
        if mode == "worm":
            try:
                sink = audit.S3ObjectLockSink.from_environment(trust_scope)
            except (RuntimeError, ValueError) as exc:
                raise AuditClientError(f"WORM audit preflight failed: {exc}") from exc
            return {
                "mode": mode,
                "backend": sink.backend_name,
                "trust_scope": sink.trust_scope,
                "compliance_mode": sink.compliance_mode,
                "configured": True,
            }
        return {
            "mode": mode,
            "backend": "signed-local",
            "trust_scope": trust_scope,
            "compliance_mode": False,
            "configured": True,
        }

    def start(self, manifest_path: Path) -> dict[str, Any]:
        if self.lifecycle_path.exists():
            existing = self._lifecycle()
            if (
                existing.get("mode") != self._mode()
                or existing.get("trust_scope") != self._trust_scope()
            ):
                raise AuditClientError("AuditService lifecycle differs from compiled policy")
            if existing.get("stopped_at") is not None:
                raise AuditClientError("AuditService was already stopped for this mission")
            try:
                health = self.health()
                return {"started": False, "health": health, "lifecycle": existing}
            except AuditClientError:
                try:
                    os.kill(int(existing["pid"]), 0)
                except (ProcessLookupError, ValueError, KeyError):
                    self.lifecycle_path.unlink()
                    self.socket_path.unlink(missing_ok=True)
                else:
                    raise
        self.preflight()
        mission_state.ensure_private_directory(self.root)
        if not self.socket_dir.exists():
            self.socket_dir.mkdir(mode=0o711)
        audit.secure_directory(self.socket_dir, owner_uid=os.geteuid(), mode=0o711)
        mission_state.ensure_private_directory(self.ledger_root)
        mission_state.ensure_private_directory(self.anchor_receipts)
        hmac_key, private_key, public_key = _generate_keys(self.root)
        mode = self._mode()
        trust_scope = self._trust_scope()
        log_out = self.root / "service.stdout.log"
        log_err = self.root / "service.stderr.log"
        command = [
            "python3", str(ROOT / "scripts" / "fleet_audit_control.py"), "serve",
            "--socket", str(self.socket_path), "--root", str(self.ledger_root),
            "--key", str(hmac_key), "--maker-user", pwd.getpwuid(os.geteuid()).pw_name,
            "--mode", mode, "--trust-scope", trust_scope,
            "--receipt-root", str(self.anchor_receipts),
        ]
        with log_out.open("ab") as stdout, log_err.open("ab") as stderr:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=os.environ.copy(),
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        self._process = process
        _LIVE_PROCESSES[process.pid] = process
        lifecycle = {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "mode": mode,
            "trust_scope": trust_scope,
            "pid": process.pid,
            "socket": str(self.socket_path),
            "ledger_root": str(self.ledger_root),
            "anchor_receipts": str(self.anchor_receipts),
            "public_key": str(public_key),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "stopped_at": None,
        }
        mission_state.atomic_write(
            self.lifecycle_path, mission_state.canonical_bytes(lifecycle) + b"\n"
        )
        deadline = time.monotonic() + 8
        last_error = "socket not ready"
        ready = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                last_error = log_err.read_text(encoding="utf-8", errors="replace").strip()
                break
            if self.socket_path.exists():
                try:
                    self.health()
                    ready = True
                    break
                except AuditClientError as exc:
                    last_error = str(exc)
            time.sleep(0.05)
        if not ready:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            _LIVE_PROCESSES.pop(process.pid, None)
            self.lifecycle_path.unlink(missing_ok=True)
            self.socket_path.unlink(missing_ok=True)
            raise AuditClientError(f"AuditService failed to start: {last_error}")
        state = fleet_mission.load_state(self.runs_dir, self.mission_id)
        artifacts = {
            "mission_ledger": str(mission_state.ledger_path(self.runs_dir, self.mission_id)),
            "compiled_workflow": str(self.mission_root / "compiled-workflow.json"),
            "manifest": str(manifest_path.resolve()),
        }
        try:
            started = audit.send_request(
                self.socket_path,
                {
                    "operation": "run_started",
                    "run_id": self.mission_id,
                    "event_id": str(uuid.uuid5(uuid.UUID(self.mission_id), "audit:run-started")),
                    "model_version": "mission-control-v1",
                    "data_classification": "restricted",
                    "artifacts": artifacts,
                },
            )
            self.record_control_event(
                event_type="MissionAuditStarted",
                subject_id=self.mission_id,
                subject_sha256=state["head_sha256"],
                metadata={
                    "audit_mode": mode,
                    "trust_scope": trust_scope,
                    "mission_status": state["status"],
                },
                idempotency_key="audit:mission-started",
            )
        except (AuditClientError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            _LIVE_PROCESSES.pop(process.pid, None)
            self.lifecycle_path.unlink(missing_ok=True)
            self.socket_path.unlink(missing_ok=True)
            raise AuditClientError(f"cannot start signed audit run: {exc}") from exc
        return {"started": True, "run_started": started, "health": self.health(), "lifecycle": lifecycle}

    def record_control_event(
        self,
        *,
        event_type: str,
        subject_id: str,
        subject_sha256: str,
        metadata: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        event_id = str(uuid.uuid5(uuid.UUID(self.mission_id), f"audit-event:{idempotency_key}"))
        try:
            return audit.send_request(
                self.socket_path,
                {
                    "operation": "control_event",
                    "run_id": self.mission_id,
                    "event_id": event_id,
                    "event_type": event_type,
                    "subject_id": subject_id,
                    "subject_sha256": subject_sha256,
                    "metadata": metadata,
                    "data_classification": "restricted",
                },
            )
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            raise AuditClientError(f"cannot append signed control event: {exc}") from exc

    def verify(self) -> dict[str, Any]:
        lifecycle = self._lifecycle()
        try:
            key = audit.load_control_key(self.hmac_key)
            ledger = audit.AuditLedger(
                self.ledger_root, key, audit.SignedLocalSink(), receipt_root=self.anchor_receipts
            )
            events = ledger.read_verified(self.mission_id)
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            raise AuditClientError(f"signed audit live verification failed: {exc}") from exc
        if not events:
            raise AuditClientError("assured audit chain is empty")
        compliance = all(event.get("worm_compliance_mode") is True for event in events)
        require_worm = self._mode() == "worm"
        required_trust_scope = self._trust_scope()
        if (
            lifecycle.get("mode") != self._mode()
            or lifecycle.get("trust_scope") != required_trust_scope
        ):
            raise AuditClientError("audit lifecycle differs from compiled policy")
        if require_worm and not compliance:
            raise AuditClientError("workflow requires WORM but audit chain is non-compliant")
        if not require_worm and compliance:
            raise AuditClientError("signed profile unexpectedly claims WORM")
        ledger_path = self.ledger_root / self.mission_id / "a2a_ledger.jsonl"
        _, anchor_receipts_sha256 = _anchor_envelope(events, self.anchor_receipts)
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "records": len(events),
            "head_sha256": events[-1]["event_sha256"],
            "ledger_sha256": audit.file_sha256(ledger_path),
            "worm": compliance,
            "backend": events[-1]["worm_backend"],
            "trust_scope": events[-1]["worm_trust_scope"],
            "public_key_sha256": audit.file_sha256(self.public_key),
            "anchor_receipts_sha256": anchor_receipts_sha256,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        receipt["ed25519_signature"] = _sign_receipt(self.private_key, receipt)
        content = mission_state.canonical_bytes(receipt) + b"\n"
        if self.receipt_path.exists():
            prior = _load_json(self.receipt_path)
            prior_records = _validate_prior_receipt(
                prior, events, ledger_path, self.public_key, self.anchor_receipts,
                mission_id=self.mission_id, compliance=compliance,
                backend=str(events[-1]["worm_backend"]),
                trust_scope=str(events[-1]["worm_trust_scope"]),
            )
            stable = {key: value for key, value in receipt.items() if key in {
                "schema_version", "mission_id", "records", "head_sha256", "ledger_sha256",
                "worm", "backend", "trust_scope", "public_key_sha256",
                "anchor_receipts_sha256",
            }}
            if prior_records == len(events) and all(
                prior.get(key) == value for key, value in stable.items()
            ):
                receipt = prior
            else:
                mission_state.atomic_write(self.receipt_path, content)
        else:
            mission_state.atomic_write(self.receipt_path, content)
        return verify_offline(
            ledger_path, self.receipt_path, self.public_key, self.anchor_receipts,
            require_worm=require_worm, required_trust_scope=required_trust_scope,
        )

    def stop(self) -> dict[str, Any]:
        verified = self.verify()
        lifecycle = self._lifecycle()
        if lifecycle.get("stopped_at") is not None:
            return {"stopped": False, "verified": verified}
        self.health()
        pid = int(lifecycle["pid"])
        if not self._pid_alive(pid):
            raise AuditClientError("AuditService process disappeared before stop")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        process = self._process or _LIVE_PROCESSES.get(pid)
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
            raise AuditClientError(
                "AuditService did not stop before the shutdown deadline"
            )
        _LIVE_PROCESSES.pop(pid, None)
        self.socket_path.unlink(missing_ok=True)
        lifecycle["stopped_at"] = datetime.now(timezone.utc).isoformat()
        mission_state.atomic_write(
            self.lifecycle_path, mission_state.canonical_bytes(lifecycle) + b"\n"
        )
        return {"stopped": True, "verified": verified}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir")
    parser.add_argument("--mission-id")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--manifest", required=True)
    record = commands.add_parser("record")
    record.add_argument("--event-type", required=True)
    record.add_argument("--subject-id", required=True)
    record.add_argument("--subject-sha256", required=True)
    record.add_argument("--metadata-json", default="{}")
    record.add_argument("--idempotency-key", required=True)
    commands.add_parser("health")
    commands.add_parser("verify")
    commands.add_parser("stop")
    offline = commands.add_parser("verify-offline")
    offline.add_argument("--ledger", required=True)
    offline.add_argument("--receipt", required=True)
    offline.add_argument("--public-key", required=True)
    offline.add_argument("--anchor-receipts", required=True)
    offline.add_argument("--require-worm", action="store_true")
    offline.add_argument(
        "--require-trust-scope", choices=tuple(sorted(audit.TRUST_SCOPES))
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify-offline":
            value = verify_offline(
                Path(args.ledger), Path(args.receipt), Path(args.public_key),
                Path(args.anchor_receipts), require_worm=args.require_worm,
                required_trust_scope=args.require_trust_scope,
            )
            print(json.dumps(value, sort_keys=True))
            return 0
        if not args.runs_dir or not args.mission_id:
            raise AuditClientError("live audit commands require --runs-dir and --mission-id")
        lifecycle = AuditLifecycle(Path(args.runs_dir), args.mission_id)
        if args.command == "start":
            value = lifecycle.start(Path(args.manifest))
        elif args.command == "record":
            metadata = json.loads(args.metadata_json)
            if not isinstance(metadata, dict):
                raise AuditClientError("--metadata-json must be an object")
            value = lifecycle.record_control_event(
                event_type=args.event_type,
                subject_id=args.subject_id,
                subject_sha256=args.subject_sha256,
                metadata=metadata,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "health":
            value = lifecycle.health()
        elif args.command == "verify":
            value = lifecycle.verify()
        else:
            value = lifecycle.stop()
        print(json.dumps(value, sort_keys=True))
        return 0
    except (AuditClientError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"audit-client: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
