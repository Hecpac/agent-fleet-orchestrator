#!/usr/bin/env python3
"""Export non-authoritative Mission spans without affecting completion state."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any

import fleet_mission_state as mission_state
import fleet_audit_client
import fleet_trace


ROOT = Path(__file__).resolve().parents[1]


class TraceExportError(RuntimeError):
    """A trace cannot be derived or serialized safely."""


def verified_audit_events(root: Path, mission_id: str) -> list[dict[str, Any]]:
    audit_root = root / "audit"
    ledger = audit_root / "ledgers" / mission_id / "a2a_ledger.jsonl"
    receipt = audit_root / "audit-verification.json"
    public_key = audit_root / "audit-signing-public.pem"
    anchors = audit_root / "anchor-receipts"
    if not ledger.exists():
        return []
    if not receipt.is_file() or not public_key.is_file() or not anchors.is_dir():
        raise TraceExportError("audit trace lacks a complete public verification envelope")
    try:
        compiled = json.loads((root / "compiled-workflow.json").read_text(encoding="utf-8"))
        require_worm = compiled["workflow"]["audit"]["mode"] == "worm"
        required_trust_scope = compiled["workflow"]["audit"]["trust_scope"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise TraceExportError("audit trace lacks a valid compiled trust policy") from exc
    fleet_audit_client.verify_offline(
        ledger,
        receipt,
        public_key,
        anchors,
        require_worm=require_worm,
        required_trust_scope=required_trust_scope,
    )
    return fleet_audit_client.read_verified_public_chain(ledger)


def trace_envelope(
    events: list[dict[str, Any]], audit_events: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    spans = fleet_trace.events_to_spans(events, audit_events)
    if not spans:
        raise TraceExportError("mission trace has no spans")
    return {
        "schema_version": 1,
        "authority": "observational_only",
        "mission_id": spans[0]["mission_id"],
        "spans": spans,
    }


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def export_trace(
    events: list[dict[str, Any]],
    *,
    audit_events: list[dict[str, Any]] | None = None,
    output: Path | None = None,
    endpoint: str | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    envelope = trace_envelope(events, audit_events)
    content = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    result: dict[str, Any] = {
        "mission_id": envelope["mission_id"],
        "spans": len(envelope["spans"]),
        "authority": "observational_only",
        "file_exported": False,
        "endpoint_exported": False,
        "warnings": [],
    }
    if output is not None:
        try:
            _write(output.resolve(), content)
            result["file_exported"] = True
        except OSError as exc:
            result["warnings"].append(f"file_export_failed:{type(exc).__name__}")
    if endpoint is not None:
        try:
            request = urllib.request.Request(
                endpoint,
                data=content,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                if not 200 <= int(response.status) < 300:
                    raise urllib.error.URLError(f"HTTP {response.status}")
            result["endpoint_exported"] = True
        except (OSError, urllib.error.URLError, ValueError) as exc:
            result["warnings"].append(f"endpoint_export_failed:{type(exc).__name__}")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", default=str(ROOT / "orchestration" / "runs"))
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--output")
    parser.add_argument("--endpoint")
    parser.add_argument("--strict-export", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        mission_id = mission_state.normalize_uuid(args.mission_id, "mission_id")
        events = mission_state.read_events(
            mission_state.ledger_path(Path(args.runs_dir).resolve(), mission_id),
            expected_mission_id=mission_id,
        )
        root = mission_state.mission_root(Path(args.runs_dir).resolve(), mission_id)
        audit_events = verified_audit_events(
            root, mission_id
        )
        result = export_trace(
            events,
            audit_events=audit_events,
            output=Path(args.output) if args.output else None,
            endpoint=args.endpoint,
        )
        print(json.dumps(result, sort_keys=True))
        return 2 if args.strict_export and result["warnings"] else 0
    except (
        TraceExportError,
        mission_state.MissionStateError,
        fleet_audit_client.AuditClientError,
        OSError,
    ) as exc:
        print(f"fleet-export-trace: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
