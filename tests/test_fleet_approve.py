from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "fleet_approve", ROOT / "scripts" / "fleet-approve.py"
)
assert SPEC and SPEC.loader
fleet_approve = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet_approve)

import fleet_json  # noqa: E402
import fleet_mission  # noqa: E402
import fleet_mission_state as state  # noqa: E402
import workflow_config  # noqa: E402
from tests.mission_control_test_support import (  # noqa: E402
    legacy_v1_compiled,
    write_compiled,
)


class FleetApproveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs = self.tmp / "runs"
        self.target = self.tmp / "target"
        self.target.mkdir()
        self.compiled = workflow_config.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )
        self.mission_id, _ = fleet_mission.create_mission(
            self.runs,
            compiled=self.compiled,
            feature="approve",
            objective="operate on the production service",
            target_repo=self.target.resolve(),
            base_sha="a" * 40,
            idempotency_key="create:approve",
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_escalated",
            actor="CONTROL",
            idempotency_key="risk",
            payload={
                "from": "low",
                "to": "high",
                "categories": ["production"],
                "reason": "risk",
            },
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_requested",
            actor="CONTROL",
            idempotency_key="request",
            payload={
                "risk": "high",
                "categories": ["production"],
                "scope": str(self.target.resolve()),
                "workflow_digest": self.compiled["workflow_digest"],
            },
        )

    def add_archive_risk(self) -> None:
        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_escalated",
            actor="CONTROL",
            idempotency_key="archive-risk",
            payload={
                "from": "high",
                "to": "high",
                "categories": ["credentials"],
                "reason": "archive policy test",
            },
        )

    def append_assurance_approval(
        self, *, expires_at: str, idempotency_key: str
    ) -> dict:
        events = state.read_events(
            state.ledger_path(self.runs, self.mission_id),
            expected_mission_id=self.mission_id,
        )
        request = next(
            event
            for event in reversed(events)
            if event["kind"] == "assurance_requested"
        )
        payload = {
            "approval_id": str(uuid.uuid4()),
            "request_event_sha256": request["event_sha256"],
            "workflow_digest": self.compiled["workflow_digest"],
            "scope": str(self.target.resolve()),
            "risk": "high",
            "expires_at": expires_at,
            "approved_by_sha256": "c" * 64,
            "decision": "approved",
        }
        expired = state.parse_timestamp(
            expires_at, "test approval expiry"
        ) <= datetime.now(timezone.utc)
        if not expired:
            payload["expires_in_seconds"] = 600
        context = (
            mock.patch.object(
                state, "_require_current_authority_append_schema", return_value=None
            )
            if expired
            else nullcontext()
        )
        with context:
            event, _ = state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_approved",
                actor="HUMAN",
                idempotency_key=idempotency_key,
                payload=payload,
            )
        return event

    @staticmethod
    def ambiguous_json(valid: bytes) -> dict[str, bytes]:
        return {
            "duplicate": valid.replace(b"{", b'{"schema_version":1,', 1),
            "nan": b'{"value":NaN}\n',
            "infinity": b'{"value":Infinity}\n',
            "overflow": b'{"value":1e400}\n',
            "bom": b"\xef\xbb\xbf" + valid,
            "invalid-utf8": b'{"value":"\xff"}\n',
            "surrogate": b'{"value":"\\ud800"}\n',
            "trailing": valid.rstrip(b"\n") + b" trailing\n",
        }

    def test_historical_compiled_workflow_cannot_authorize_approval_effects(
        self,
    ) -> None:
        root = self.runs / "missions" / self.mission_id
        ledger = root / "mission.jsonl"
        before = ledger.read_bytes()
        write_compiled(
            root / "compiled-workflow.json", legacy_v1_compiled(self.compiled)
        )
        for approve, key in (
            (fleet_approve.approve_mission, "human:v1:mission"),
            (fleet_approve.approve_archive, "human:v1:archive"),
        ):
            with (
                self.subTest(approval=approve.__name__),
                self.assertRaisesRegex(
                    RuntimeError, "historical read-only.*require v2"
                ),
            ):
                approve(
                    self.runs,
                    self.mission_id,
                    scope=str(self.target),
                    expires_in=600,
                    idempotency_key=key,
                )
        self.assertEqual(ledger.read_bytes(), before)
        self.assertFalse((root / fleet_approve.ARCHIVE_APPROVAL_FILE).exists())

    def test_mission_approval_is_scoped_expiring_and_idempotent(self) -> None:
        first = fleet_approve.approve_mission(
            self.runs,
            self.mission_id,
            scope=str(self.target),
            expires_in=600,
            idempotency_key="human:approve",
        )
        second = fleet_approve.approve_mission(
            self.runs,
            self.mission_id,
            scope=str(self.target),
            expires_in=600,
            idempotency_key="human:approve",
        )
        self.assertTrue(first["appended"])
        self.assertFalse(second["appended"])
        self.assertEqual(first["event"], second["event"])
        current = fleet_mission.load_state(self.runs, self.mission_id)
        self.assertEqual(current["status"], "assurance_approved")
        self.assertEqual(current["approval"]["scope"], str(self.target.resolve()))
        with self.assertRaisesRegex(RuntimeError, "replay identity conflicts"):
            fleet_approve.approve_mission(
                self.runs,
                self.mission_id,
                scope=str(self.target),
                expires_in=601,
                idempotency_key="human:approve",
            )

    def test_expired_mission_approval_renews_exactly_and_cli_does_not_boot(
        self,
    ) -> None:
        prior = self.append_assurance_approval(
            expires_at="2000-01-01T00:00:00Z",
            idempotency_key="human:expired",
        )
        command = [
            sys.executable,
            str(ROOT / "scripts" / "fleet-approve.py"),
            "--runs-dir",
            str(self.runs),
            "--mission-id",
            self.mission_id,
            "--scope",
            str(self.target),
            "--expires-in",
            "600",
            "--idempotency-key",
            "human:renew:cli",
            "--renew",
        ]
        first = subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        value = fleet_json.loads(first.stdout)
        self.assertTrue(value["appended"])
        self.assertEqual(value["event"]["kind"], "assurance_approval_renewed")
        self.assertEqual(
            value["event"]["payload"]["prior_approval_event_sha256"],
            prior["event_sha256"],
        )
        current = fleet_mission.load_state(self.runs, self.mission_id)
        self.assertEqual(current["status"], "assurance_approved")
        self.assertEqual(
            current["approval"]["event_sha256"], value["event"]["event_sha256"]
        )

        replay = subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertFalse(fleet_json.loads(replay.stdout)["appended"])
        with self.assertRaisesRegex(RuntimeError, "idempotent replay conflicts"):
            fleet_approve.renew_mission_approval(
                self.runs,
                self.mission_id,
                scope=str(self.target),
                expires_in=601,
                idempotency_key="human:renew:cli",
            )

        renewed = current["approval"]
        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_boot_started",
            actor="CONTROL",
            idempotency_key="renewed:boot",
            payload={
                "preset": "fleet_dialogue",
                "approval_event_sha256": renewed["event_sha256"],
            },
        )
        self.assertEqual(
            fleet_mission.load_state(self.runs, self.mission_id)["status"],
            "assured_booting",
        )

    def test_approval_renewal_rejects_live_or_assured_booting_authority(self) -> None:
        live = self.append_assurance_approval(
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            idempotency_key="human:live",
        )
        ledger = state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "before expiry"):
            fleet_approve.renew_mission_approval(
                self.runs,
                self.mission_id,
                scope=str(self.target),
                expires_in=600,
                idempotency_key="human:renew:too-early",
            )
        self.assertEqual(ledger.read_bytes(), before)

        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_boot_started",
            actor="CONTROL",
            idempotency_key="live:boot",
            payload={
                "preset": "fleet_dialogue",
                "approval_event_sha256": live["event_sha256"],
            },
        )
        after_boot = ledger.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "approved or assured-running"):
            fleet_approve.renew_mission_approval(
                self.runs,
                self.mission_id,
                scope=str(self.target),
                expires_in=600,
                idempotency_key="human:renew:after-boot",
            )
        self.assertEqual(ledger.read_bytes(), after_boot)

    def test_mission_approval_rejects_scope_drift(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "scope"):
            fleet_approve.approve_mission(
                self.runs,
                self.mission_id,
                scope=str(self.tmp / "other"),
                expires_in=600,
                idempotency_key="human:wrong",
            )

    def test_idempotent_replay_rejects_a_new_assurance_request(self) -> None:
        approved = fleet_approve.approve_mission(
            self.runs,
            self.mission_id,
            scope=str(self.target),
            expires_in=600,
            idempotency_key="human:replay",
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_boot_started",
            actor="CONTROL",
            idempotency_key="replay:boot",
            payload={
                "preset": "fleet_dialogue",
                "approval_event_sha256": approved["event"]["event_sha256"],
            },
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_started",
            actor="CONTROL",
            idempotency_key="replay:started",
            payload={
                "manifest": str(self.runs / "fleet-approve.manifest"),
                "approval_event_sha256": approved["event"]["event_sha256"],
            },
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_requested",
            actor="CONTROL",
            idempotency_key="replay:new-request",
            payload={
                "risk": "high",
                "categories": ["production", "credentials"],
                "scope": str(self.target.resolve()),
                "workflow_digest": self.compiled["workflow_digest"],
            },
        )
        with self.assertRaisesRegex(RuntimeError, "replay identity conflicts"):
            fleet_approve.approve_mission(
                self.runs,
                self.mission_id,
                scope=str(self.target),
                expires_in=600,
                idempotency_key="human:replay",
            )

    def test_sensitive_full_archive_approval_is_scoped_and_idempotent(self) -> None:
        # The assurance request made this mission high risk; add the independent
        # data category that specifically gates a full archive.
        self.add_archive_risk()
        first = fleet_approve.approve_archive(
            self.runs,
            self.mission_id,
            scope=str(self.target),
            expires_in=600,
            idempotency_key="human:archive:approve",
        )
        second = fleet_approve.approve_archive(
            self.runs,
            self.mission_id,
            scope=str(self.target),
            expires_in=600,
            idempotency_key="human:archive:approve",
        )
        self.assertTrue(first["appended"])
        self.assertFalse(second["appended"])
        self.assertEqual(first["approval"], second["approval"])
        self.assertEqual(first["approval"]["risk_categories"], ["credentials"])
        approval_path = Path(first["path"])
        self.assertEqual(approval_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            approval_path.read_bytes(),
            fleet_json.canonical_bytes(first["approval"]) + b"\n",
        )
        self.assertEqual(
            approval_path.read_bytes(), state.canonical_bytes(first["approval"]) + b"\n"
        )

    def test_mission_approval_rejects_ambiguous_json_without_mutation(self) -> None:
        root = self.runs / "missions" / self.mission_id
        ledger = root / "mission.jsonl"
        original = ledger.read_bytes()
        archive_approval = root / fleet_approve.ARCHIVE_APPROVAL_FILE
        try:
            for name, invalid in self.ambiguous_json(original).items():
                with self.subTest(name=name):
                    ledger.write_bytes(invalid)
                    ledger.chmod(0o600)
                    with self.assertRaisesRegex(
                        RuntimeError, "mission approval JSON is unsafe or invalid"
                    ):
                        fleet_approve.approve_mission(
                            self.runs,
                            self.mission_id,
                            scope=str(self.target),
                            expires_in=600,
                            idempotency_key=f"human:invalid:{name}",
                        )
                    self.assertEqual(ledger.read_bytes(), invalid)
                    self.assertFalse(archive_approval.exists())
        finally:
            ledger.write_bytes(original)
            ledger.chmod(0o600)

    def test_existing_archive_approval_rejects_ambiguous_json_without_mutation(
        self,
    ) -> None:
        self.add_archive_risk()
        created = fleet_approve.approve_archive(
            self.runs,
            self.mission_id,
            scope=str(self.target),
            expires_in=600,
            idempotency_key="human:archive:strict",
        )
        path = Path(created["path"])
        original = path.read_bytes()
        ledger = self.runs / "missions" / self.mission_id / "mission.jsonl"
        ledger_before = ledger.read_bytes()
        try:
            for name, invalid in self.ambiguous_json(original).items():
                with self.subTest(name=name):
                    path.write_bytes(invalid)
                    path.chmod(0o600)
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "existing archive approval is unsafe or invalid",
                    ):
                        fleet_approve.approve_archive(
                            self.runs,
                            self.mission_id,
                            scope=str(self.target),
                            expires_in=600,
                            idempotency_key="human:archive:strict",
                        )
                    self.assertEqual(path.read_bytes(), invalid)
                    self.assertEqual(ledger.read_bytes(), ledger_before)
        finally:
            path.write_bytes(original)
            path.chmod(0o600)

    def test_approval_rejects_symlinked_or_hardlinked_durable_json(self) -> None:
        root = self.runs / "missions" / self.mission_id
        ledger = root / "mission.jsonl"
        original_ledger = ledger.read_bytes()
        for link_kind in ("symlink", "hardlink"):
            with self.subTest(store="mission-ledger", link_kind=link_kind):
                outside = self.tmp / f"outside-ledger-{link_kind}.jsonl"
                outside.write_bytes(original_ledger)
                outside.chmod(0o600)
                ledger.unlink()
                if link_kind == "symlink":
                    ledger.symlink_to(outside)
                else:
                    os.link(outside, ledger)
                try:
                    with self.assertRaisesRegex(
                        RuntimeError, "mission approval JSON is unsafe or invalid"
                    ):
                        fleet_approve.approve_mission(
                            self.runs,
                            self.mission_id,
                            scope=str(self.target),
                            expires_in=600,
                            idempotency_key=f"human:ledger:{link_kind}",
                        )
                    self.assertEqual(outside.read_bytes(), original_ledger)
                finally:
                    ledger.unlink()
                    outside.unlink()
                    ledger.write_bytes(original_ledger)
                    ledger.chmod(0o600)

        self.add_archive_risk()
        approval_path = root / fleet_approve.ARCHIVE_APPROVAL_FILE
        ledger_before = ledger.read_bytes()
        for link_kind in ("symlink", "hardlink"):
            with self.subTest(store="archive-approval", link_kind=link_kind):
                outside = self.tmp / f"outside-approval-{link_kind}.json"
                outside.write_bytes(b"{}\n")
                outside.chmod(0o600)
                if link_kind == "symlink":
                    approval_path.symlink_to(outside)
                else:
                    os.link(outside, approval_path)
                try:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "existing archive approval is unsafe or invalid",
                    ):
                        fleet_approve.approve_archive(
                            self.runs,
                            self.mission_id,
                            scope=str(self.target),
                            expires_in=600,
                            idempotency_key=f"human:archive:{link_kind}",
                        )
                    self.assertEqual(outside.read_bytes(), b"{}\n")
                    self.assertEqual(ledger.read_bytes(), ledger_before)
                finally:
                    approval_path.unlink()
                    outside.unlink()

    def test_legacy_audit_rejects_ambiguous_json_before_append_or_cmux(self) -> None:
        audit_dir = self.tmp / "legacy-audit"
        audit_dir.mkdir(mode=0o700)
        audit_dir.chmod(0o700)
        audit_path = audit_dir / "audit.jsonl"
        valid = b'{"schema_version":1}\n'
        for name, invalid in self.ambiguous_json(valid).items():
            with self.subTest(name=name):
                audit_path.write_bytes(invalid)
                audit_path.chmod(0o600)
                audit = SimpleNamespace(
                    AUDIT_PATH=audit_path,
                    load_and_verify=mock.Mock(),
                    append_event=mock.Mock(),
                )
                with (
                    mock.patch.object(
                        fleet_approve, "load_audit_common", return_value=audit
                    ),
                    mock.patch.object(fleet_approve.subprocess, "run") as cmux,
                    self.assertRaisesRegex(
                        RuntimeError, "legacy approval JSON is unsafe or invalid"
                    ),
                ):
                    fleet_approve.approve_legacy("request", "surface:1")
                audit.load_and_verify.assert_not_called()
                audit.append_event.assert_not_called()
                cmux.assert_not_called()
                self.assertEqual(audit_path.read_bytes(), invalid)

    def test_legacy_historical_json_and_cli_canonical_output_remain_compatible(
        self,
    ) -> None:
        audit_dir = self.tmp / "legacy-valid"
        audit_dir.mkdir(mode=0o700)
        audit_dir.chmod(0o700)
        audit_path = audit_dir / "audit.jsonl"
        request = {
            "event_id": "request-1",
            "event_type": "PermissionRequest",
            "tool_name": "Bash",
            "tool_use_id": "tool-1",
            "session_id": "session-1",
            "model_version": "model-1",
            "data_classification": "restricted",
            "args_sha256": "a" * 64,
            "correlation_sha256": "b" * 64,
        }
        historical = (
            json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
            + b"\n"
        )
        self.assertNotEqual(historical, fleet_json.canonical_jsonl([request]))
        audit_path.write_bytes(historical)
        audit_path.chmod(0o600)
        audit = SimpleNamespace(
            AUDIT_PATH=audit_path,
            load_and_verify=mock.Mock(
                side_effect=lambda handle: fleet_json.load_jsonl(handle.read())
            ),
            append_event=mock.Mock(side_effect=lambda event: event),
        )
        with (
            mock.patch.object(fleet_approve, "load_audit_common", return_value=audit),
            mock.patch.object(fleet_approve.subprocess, "run") as cmux,
        ):
            event_id = fleet_approve.approve_legacy("request-1", "surface:1")
        self.assertTrue(event_id)
        self.assertEqual(audit.append_event.call_count, 1)
        self.assertEqual(cmux.call_count, 2)
        self.assertEqual(audit_path.read_bytes(), historical)

        self.add_archive_risk()
        archive = fleet_approve.approve_archive(
            self.runs,
            self.mission_id,
            scope=str(self.target),
            expires_in=600,
            idempotency_key="human:archive:cli",
        )
        approval_path = Path(archive["path"])
        valid_approval = approval_path.read_bytes()
        invalid_approval = b"\xef\xbb\xbf" + valid_approval
        approval_path.write_bytes(invalid_approval)
        approval_path.chmod(0o600)
        command = [
            sys.executable,
            str(ROOT / "scripts" / "fleet-approve.py"),
            "--runs-dir",
            str(self.runs),
            "--mission-id",
            self.mission_id,
            "--scope",
            str(self.target),
            "--expires-in",
            "600",
            "--idempotency-key",
            "human:archive:cli",
            "--archive",
        ]
        refused = subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertEqual(refused.stdout, b"")
        self.assertIn(
            b"approval failed: existing archive approval is unsafe or invalid",
            refused.stderr,
        )
        self.assertEqual(approval_path.read_bytes(), invalid_approval)

        approval_path.write_bytes(valid_approval)
        approval_path.chmod(0o600)
        approved = subprocess.run(
            command[:-1],
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(approved.returncode, 0, approved.stderr)
        value = fleet_json.loads(approved.stdout)
        self.assertEqual(approved.stdout, fleet_json.canonical_bytes(value) + b"\n")
        self.assertTrue(value["appended"])


if __name__ == "__main__":
    unittest.main()
