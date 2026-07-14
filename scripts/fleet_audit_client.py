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
) -> dict[str, Any]:
    events = _chain_without_secret(ledger_path)
    receipt = _load_json(receipt_path)
    required = {
        "schema_version", "mission_id", "records", "head_sha256", "ledger_sha256",
        "worm", "backend", "public_key_sha256", "verified_at", "ed25519_signature",
    }
    if set(receipt) != required or receipt["schema_version"] != 1:
        raise AuditClientError("audit verification receipt fields are invalid")
    if receipt["records"] != len(events) or receipt["head_sha256"] != events[-1]["event_sha256"]:
        raise AuditClientError("audit verification receipt chain summary mismatch")
    if receipt["ledger_sha256"] != audit.file_sha256(ledger_path):
        raise AuditClientError("audit verification receipt ledger hash mismatch")
    if receipt["public_key_sha256"] != audit.file_sha256(public_key):
        raise AuditClientError("audit verification public key hash mismatch")
    _verify_signature(public_key, receipt)
    compliance = all(event.get("worm_compliance_mode") is True for event in events)
    if bool(receipt["worm"]) != compliance:
        raise AuditClientError("audit receipt WORM claim differs from ledger")
    if require_worm and not compliance:
        raise AuditClientError("workflow requires WORM compliance")
    for event in events:
        receipt_file = anchor_receipts / f"{event['event_id']}.json"
        anchor = _load_json(receipt_file)
        if anchor.get("event_sha256") != event["event_sha256"]:
            raise AuditClientError("audit anchor receipt event hash mismatch")
        if compliance and (
            anchor.get("worm") is not True
            or anchor.get("retention_mode") != "COMPLIANCE"
            or not anchor.get("version_id")
            or not anchor.get("retained_until")
        ):
            raise AuditClientError("WORM anchor receipt is incomplete")
        if not compliance and anchor.get("worm") is not False:
            raise AuditClientError("signed audit receipt makes a WORM claim")
    return {
        "mission_id": receipt["mission_id"],
        "records": len(events),
        "head_sha256": events[-1]["event_sha256"],
        "worm": compliance,
        "valid": True,
    }


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

    def _lifecycle(self) -> dict[str, Any]:
        return _load_json(self.lifecycle_path)

    def health(self) -> dict[str, Any]:
        try:
            result = audit.send_request(self.socket_path, {"operation": "health"})
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            raise AuditClientError(f"AuditService health failed: {exc}") from exc
        lifecycle = self._lifecycle()
        if result.get("status") != "ok" or result.get("mode") != lifecycle.get("mode"):
            raise AuditClientError("AuditService health identity mismatch")
        return result

    def preflight(self) -> dict[str, Any]:
        """Validate an audit backend before any fleet or key side effect."""
        mode = self._mode()
        if mode == "worm":
            try:
                sink = audit.S3ObjectLockSink.from_environment()
            except (RuntimeError, ValueError) as exc:
                raise AuditClientError(f"WORM audit preflight failed: {exc}") from exc
            return {
                "mode": mode,
                "backend": sink.backend_name,
                "compliance_mode": sink.compliance_mode,
                "configured": True,
            }
        return {
            "mode": mode,
            "backend": "signed-local",
            "compliance_mode": False,
            "configured": True,
        }

    def start(self, manifest_path: Path) -> dict[str, Any]:
        if self.lifecycle_path.exists():
            existing = self._lifecycle()
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
        log_out = self.root / "service.stdout.log"
        log_err = self.root / "service.stderr.log"
        command = [
            "python3", str(ROOT / "scripts" / "fleet_audit_control.py"), "serve",
            "--socket", str(self.socket_path), "--root", str(self.ledger_root),
            "--key", str(hmac_key), "--maker-user", pwd.getpwuid(os.geteuid()).pw_name,
            "--mode", mode, "--receipt-root", str(self.anchor_receipts),
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
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            raise AuditClientError(f"cannot start signed audit run: {exc}") from exc
        self.record_control_event(
            event_type="MissionAuditStarted",
            subject_id=self.mission_id,
            subject_sha256=state["head_sha256"],
            metadata={"audit_mode": mode, "mission_status": state["status"]},
            idempotency_key="audit:mission-started",
        )
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
        require_worm = lifecycle["mode"] == "worm"
        if require_worm and not compliance:
            raise AuditClientError("workflow requires WORM but audit chain is non-compliant")
        if not require_worm and compliance:
            raise AuditClientError("signed profile unexpectedly claims WORM")
        ledger_path = self.ledger_root / self.mission_id / "a2a_ledger.jsonl"
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "records": len(events),
            "head_sha256": events[-1]["event_sha256"],
            "ledger_sha256": audit.file_sha256(ledger_path),
            "worm": compliance,
            "backend": events[-1]["worm_backend"],
            "public_key_sha256": audit.file_sha256(self.public_key),
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        receipt["ed25519_signature"] = _sign_receipt(self.private_key, receipt)
        content = mission_state.canonical_bytes(receipt) + b"\n"
        if self.receipt_path.exists():
            prior = _load_json(self.receipt_path)
            stable = {key: value for key, value in receipt.items() if key in {
                "schema_version", "mission_id", "records", "head_sha256", "ledger_sha256",
                "worm", "backend", "public_key_sha256",
            }}
            if any(prior.get(key) != value for key, value in stable.items()):
                raise AuditClientError("audit verification receipt conflicts with current chain")
            receipt = prior
        else:
            mission_state.atomic_write(self.receipt_path, content)
        return verify_offline(
            ledger_path, self.receipt_path, self.public_key, self.anchor_receipts,
            require_worm=require_worm,
        )

    def stop(self) -> dict[str, Any]:
        verified = self.verify()
        lifecycle = self._lifecycle()
        if lifecycle.get("stopped_at") is not None:
            return {"stopped": False, "verified": verified}
        self.health()
        pid = int(lifecycle["pid"])
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        if self._process is not None:
            try:
                self._process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify-offline":
            value = verify_offline(
                Path(args.ledger), Path(args.receipt), Path(args.public_key),
                Path(args.anchor_receipts), require_worm=args.require_worm,
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
