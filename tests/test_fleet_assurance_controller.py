from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_assurance_controller as assurance  # noqa: E402
import fleet_dialogue  # noqa: E402
import fleet_dialogue_controller as fdp2_controller  # noqa: E402
from fleet_ledger import append_event  # noqa: E402
import tests.test_fleet_dialogue_controller as fdp2_tests  # noqa: E402


STATE = ROOT / "scripts" / "fleet_state.py"


class FleetAssuranceControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fdp2 = fdp2_tests.FleetDialogueControllerTests(
            methodName="test_happy_path_accepts_and_build_gate_revalidates_exact_head"
        )
        self.fdp2.setUp()
        self.addCleanup(self.fdp2.doCleanups)
        self.runs = self.fdp2.runs
        self.feature = self.fdp2.feature
        self.target = self.fdp2.target
        self.manifest = self.fdp2.manifest
        self.now = self.fdp2.now
        self.accepted, self.accepted_head = self.fdp2.accept_conversation()
        # Keep deterministic control deadlines safely ahead of wall-clock time;
        # expiry behavior is exercised explicitly below with a patched clock.
        self.now = datetime(2099, 7, 13, 16, 0, tzinfo=timezone.utc)
        advanced = self.run_state(
            "advance",
            "CHALLENGE",
            "--evidence",
            self.accepted["event_sha256"],
            "--approved-by",
            "hector",
        )
        self.assertEqual(advanced.returncode, 0, advanced.stderr)

    def run_state(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(STATE), args[0], str(self.manifest), *args[1:]],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def manifest_values(self) -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in self.manifest.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )

    def result_payload(self, value: dict, run_id: str) -> bytes:
        return (
            json.dumps(value, sort_keys=True)
            + f"\nFLEET_RESULT:{run_id}:DONE"
        ).encode("utf-8")

    def seed_run(
        self,
        event: dict,
        run_id: str,
        value: dict | None = None,
        *,
        raw: bytes | None = None,
        status: str = "succeeded",
        elapsed: int = 10,
        variant: str | None = None,
    ) -> bytes:
        expected = event["snapshot"]["expected"]
        values = self.manifest_values()
        started = self.now + timedelta(minutes=7)
        common = {
            "run_id": run_id,
            "feature": self.feature,
            "instance": expected["instance"],
            "role": values[f"{expected['instance']}.role_type"],
            "phase": expected["phase"],
            "task_sha256": expected["prompt_sha256"],
            "provider": values[f"{expected['instance']}.provider"],
            "model": values[f"{expected['instance']}.model"],
        }
        if variant is not None:
            common["variant"] = variant
        append_event(
            self.runs / f"fleet-{self.feature}.ledger.jsonl",
            {**common, "timestamp": started.isoformat(), "status": "preparing"},
        )
        payload = raw if raw is not None else self.result_payload(value or {}, run_id)
        result_file = self.runs / "results" / self.feature / f"{run_id}.txt"
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.write_bytes(payload)
        terminal = {
            **common,
            "timestamp": (started + timedelta(seconds=elapsed)).isoformat(),
            "status": status,
            "exit_code": 0 if status == "succeeded" else 1,
        }
        if status == "succeeded":
            terminal["result_file"] = str(result_file)
        append_event(self.runs / f"fleet-{self.feature}.ledger.jsonl", terminal)
        return payload

    def publish(self, expected: dict, key: str) -> dict:
        return fleet_dialogue.publish(
            self.runs,
            feature=self.feature,
            kind=expected["kind"],
            recipient=expected["recipient"],
            source_instance=expected["source_instance"],
            source_run_id=expected["source_run_id"],
            reply_to=expected["reply_to"],
            idempotency_key=key,
        )

    def start_assurance(self) -> dict:
        return assurance.start(
            self.runs,
            feature=self.feature,
            idempotency_key="fdp3-start",
            now=self.now + timedelta(minutes=6),
        )

    def challenge_to_gate(self, findings: list[dict] | None = None) -> dict:
        started = self.start_assurance()
        run_id = "glm-challenge-run"
        contract = {
            "schema_version": 1,
            "summary": "independent GLM challenge completed",
            "findings": findings or [],
        }
        self.seed_run(started, run_id, contract)
        run_event = assurance.step(
            self.runs,
            feature=self.feature,
            idempotency_key="fdp3-step-glm-run",
            run_id=run_id,
            now=self.now + timedelta(minutes=8),
        )
        message = self.publish(run_event["snapshot"]["expected"], "publish-glm")
        return assurance.step(
            self.runs,
            feature=self.feature,
            idempotency_key="fdp3-step-glm-message",
            message_id=message["message_id"],
            now=self.now + timedelta(minutes=9),
        )

    def enter_verify(self, gate_event: dict) -> dict:
        advanced = self.run_state(
            "advance",
            "VERIFY",
            "--evidence",
            gate_event["event_sha256"],
        )
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        return assurance.step(
            self.runs,
            feature=self.feature,
            idempotency_key="fdp3-ack-verify",
            phase_advanced=True,
            now=self.now + timedelta(minutes=10),
        )

    def complete_verification(self, verify_event: dict, contract: dict) -> dict:
        run_id = "claude-verify-run"
        self.seed_run(verify_event, run_id, contract)
        run_event = assurance.step(
            self.runs,
            feature=self.feature,
            idempotency_key="fdp3-step-claude-run",
            run_id=run_id,
            now=self.now + timedelta(minutes=11),
        )
        message = self.publish(run_event["snapshot"]["expected"], "publish-claude")
        return assurance.step(
            self.runs,
            feature=self.feature,
            idempotency_key="fdp3-step-claude-message",
            message_id=message["message_id"],
            now=self.now + timedelta(minutes=12),
        )

    def one_finding(self) -> dict:
        return {
            "finding_id": "glm-1",
            "severity": "medium",
            "description": "challenge the changed file",
            "evidence": [{"kind": "file", "ref": "proposal.txt:1"}],
        }

    def test_verified_path_binds_context_gate_messages_receipt_and_offline_archive(self) -> None:
        gate = self.challenge_to_gate([self.one_finding()])
        self.assertEqual(gate["snapshot"]["status"], "awaiting_phase_advance")
        self.assertEqual(gate["snapshot"]["accepted_head_sha"], self.accepted_head)
        self.assertEqual(
            gate["snapshot"]["fdp2_control_head_sha256"],
            self.accepted["event_sha256"],
        )
        verify_event = self.enter_verify(gate)
        terminal = self.complete_verification(
            verify_event,
            {
                "schema_version": 1,
                "verdict": "VERIFIED",
                "summary": "independent verification passed",
                "adjudications": [
                    {
                        "finding_id": "glm-1",
                        "disposition": "dismissed",
                        "reason": "the accepted implementation satisfies the check",
                        "evidence": [{"kind": "file", "ref": "proposal.txt:1"}],
                    }
                ],
                "new_findings": [],
            },
        )
        self.assertEqual(terminal["snapshot"]["status"], "verified")
        with self.assertRaises(assurance.AssuranceConflict):
            assurance.step(
                self.runs,
                feature=self.feature,
                idempotency_key="late-step",
                run_id="late-run",
            )

        receipt_path = self.runs / f"fleet-{self.feature}.assurance-receipt.json"
        receipt = assurance.create_live_receipt(
            self.runs,
            feature=self.feature,
            receipt_path=receipt_path,
            now=self.now + timedelta(minutes=13),
        )
        self.assertEqual(receipt["summary"]["latest_status"], "verified")
        fdp2_receipt_path = self.runs / f"fleet-{self.feature}.verification-receipt.json"
        fdp2_receipt = fdp2_controller.create_live_receipt(
            self.runs,
            feature=self.feature,
            receipt_path=fdp2_receipt_path,
            now=self.now + timedelta(minutes=13),
        )
        self.assertEqual(fdp2_receipt["summary"]["latest_status"], "accepted")
        self.assertEqual(fdp2_receipt["summary"]["dialogue_messages"], 4)

        archive = self.fdp2.tmp / "fdp3-archive"
        archive.mkdir()
        shutil.copy2(self.manifest, archive / "manifest")
        shutil.copy2(assurance.ledger_path(self.runs, self.feature), archive / "assurance-control.jsonl")
        shutil.copy2(self.runs / f"fleet-{self.feature}.dialogue-control.jsonl", archive / "dialogue-control.jsonl")
        shutil.copy2(self.runs / f"fleet-{self.feature}.dialogue.jsonl", archive / "dialogue.jsonl")
        shutil.copy2(self.runs / f"fleet-{self.feature}.ledger.jsonl", archive / "ledger.jsonl")
        shutil.copy2(receipt_path, archive / "assurance-receipt.json")
        shutil.copy2(fdp2_receipt_path, archive / "verification-receipt.json")
        shutil.copytree(self.runs / "assurance" / self.feature, archive / "assurance")
        shutil.copytree(self.runs / "dialogue" / self.feature, archive / "dialogue")
        self.assertEqual(
            assurance.verify_archive(archive),
            receipt["summary"],
        )
        self.assertEqual(
            fdp2_controller.verify_archive(archive),
            fdp2_receipt["summary"],
        )
        copied = next((archive / "assurance").rglob("fdp2-task-spec.json"))
        copied.write_text("{}", encoding="utf-8")
        with self.assertRaises(assurance.AssuranceError):
            assurance.verify_archive(archive)

    def test_rejected_requires_sustained_or_new_finding(self) -> None:
        verify_event = self.enter_verify(self.challenge_to_gate([self.one_finding()]))
        terminal = self.complete_verification(
            verify_event,
            {
                "schema_version": 1,
                "verdict": "REJECTED",
                "summary": "GLM finding is sustained",
                "adjudications": [
                    {
                        "finding_id": "glm-1",
                        "disposition": "sustained",
                        "reason": "independent reproduction confirms it",
                        "evidence": [{"kind": "file", "ref": "proposal.txt:1"}],
                    }
                ],
                "new_findings": [],
            },
        )
        self.assertEqual(terminal["snapshot"]["status"], "rejected")

    def test_invalid_glm_and_timeout_fail_closed(self) -> None:
        started = self.start_assurance()
        run_id = "glm-invalid-run"
        self.seed_run(
            started,
            run_id,
            raw=f"not-json\nFLEET_RESULT:{run_id}:DONE".encode(),
        )
        invalid = assurance.step(
            self.runs,
            feature=self.feature,
            idempotency_key="invalid-glm",
            run_id=run_id,
            now=self.now + timedelta(minutes=8),
        )
        self.assertEqual(invalid["snapshot"]["status"], "indeterminate")
        self.assertIn("invalid_challenge_contract", invalid["snapshot"]["terminal_reason"])

    def test_run_over_30_minutes_is_indeterminate(self) -> None:
        started = self.start_assurance()
        run_id = "glm-timeout-run"
        self.seed_run(
            started,
            run_id,
            {
                "schema_version": 1,
                "summary": "late",
                "findings": [],
            },
            elapsed=assurance.RUN_TIMEOUT_SECONDS + 1,
        )
        terminal = assurance.step(
            self.runs,
            feature=self.feature,
            idempotency_key="timeout-glm",
            run_id=run_id,
            now=self.now + timedelta(minutes=40),
        )
        self.assertEqual(terminal["snapshot"]["status"], "indeterminate")
        self.assertEqual(
            terminal["snapshot"]["terminal_reason"],
            f"run_timeout_exceeded:{run_id}",
        )

    def test_run_variant_must_match_complete_fdp3_roster_identity(self) -> None:
        started = self.start_assurance()
        run_id = "glm-variant-drift-run"
        self.seed_run(
            started,
            run_id,
            {
                "schema_version": 1,
                "summary": "identity drift must fail closed",
                "findings": [],
            },
            variant="unexpected",
        )
        with self.assertRaisesRegex(assurance.AssuranceError, "run variant"):
            assurance.step(
                self.runs,
                feature=self.feature,
                idempotency_key="variant-drift-glm",
                run_id=run_id,
                now=self.now + timedelta(minutes=8),
            )

    def test_mission_bound_start_revalidates_exact_approval_event(self) -> None:
        state_path = self.runs / f"fleet-{self.feature}.state.json"
        value = json.loads(state_path.read_text(encoding="utf-8"))
        challenge = value["history"][-1]
        challenge.pop("approved_by")
        approval_sha = "e" * 64
        challenge["approval_event_sha256"] = approval_sha
        state_path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        manifest = self.manifest_values()
        manifest["mission_id"] = "00000000-0000-4000-8000-000000000999"

        with mock.patch.object(
            assurance.fleet_state,
            "validate_mission_approval",
            return_value={"event_sha256": approval_sha},
        ) as validate:
            assurance._require_challenge_start_state(
                self.runs,
                self.feature,
                manifest,
                now=self.now,
            )
        validate.assert_called_once_with(
            self.runs / f"fleet-{self.feature}.manifest",
            manifest,
            approval_sha,
            now=self.now,
        )

        with mock.patch.object(
            assurance.fleet_state,
            "validate_mission_approval",
            side_effect=assurance.fleet_state.PhaseApprovalError("foreign event"),
        ), self.assertRaisesRegex(assurance.AssuranceError, "foreign event"):
            assurance._require_challenge_start_state(
                self.runs,
                self.feature,
                manifest,
                now=self.now,
            )

    def test_invalid_claude_json_is_indeterminate(self) -> None:
        verify_event = self.enter_verify(self.challenge_to_gate())
        run_id = "claude-invalid-run"
        self.seed_run(
            verify_event,
            run_id,
            raw=f"prose around json\nFLEET_RESULT:{run_id}:DONE".encode(),
        )
        terminal = assurance.step(
            self.runs,
            feature=self.feature,
            idempotency_key="invalid-claude",
            run_id=run_id,
            now=self.now + timedelta(minutes=11),
        )
        self.assertEqual(terminal["snapshot"]["status"], "indeterminate")
        self.assertIn("invalid_verification_contract", terminal["snapshot"]["terminal_reason"])

    def test_phase_gate_requires_exact_control_head(self) -> None:
        gate = self.challenge_to_gate()
        denied = self.run_state("advance", "VERIFY", "--evidence", "wrong-head")
        self.assertEqual(denied.returncode, 3)
        self.assertIn("exact control head", denied.stderr)
        self.assertEqual(
            json.loads(self.manifest.with_suffix(".state.json").read_text())["active_phase"],
            "CHALLENGE",
        )
        self.assertEqual(
            self.run_state("advance", "VERIFY", "--evidence", gate["event_sha256"]).returncode,
            0,
        )

    def test_phase_gate_materializes_expired_assurance_before_verify(self) -> None:
        gate = self.challenge_to_gate()
        with mock.patch.object(
            assurance, "utc_now", return_value=self.now + timedelta(days=2)
        ), self.assertRaisesRegex(assurance.AssuranceError, "valid published GLM challenge"):
            assurance.challenge_phase_gate(
                self.runs, self.feature, self.manifest_values(), gate["event_sha256"]
            )
        self.assertEqual(
            assurance.load_events(self.runs, self.feature)[-1]["snapshot"]["status"],
            "indeterminate",
        )

    def test_snapshot_validation_rejects_ignored_residue(self) -> None:
        repo = self.fdp2.tmp / "ignored-snapshot"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        subprocess.run(["git", "-C", str(repo), "checkout", "--detach", "-q"], check=True)
        (repo / "ignored.tmp").write_text("residue", encoding="utf-8")
        with self.assertRaisesRegex(assurance.AssuranceContractError, "dirty"):
            assurance._validate_snapshot_worktree(repo, head)

    def test_dirty_snapshot_cleanup_fails_and_retains_both(self) -> None:
        started = self.start_assurance()
        terminal = assurance.abandon(
            self.runs,
            feature=self.feature,
            idempotency_key="abandon-fdp3",
            reason="test cleanup",
            now=self.now + timedelta(minutes=7),
        )
        challenge_path = Path(terminal["snapshot"]["challenge_snapshot_path"])
        verify_path = Path(terminal["snapshot"]["verification_snapshot_path"])
        (challenge_path / "dirty.txt").write_text("retain\n", encoding="utf-8")
        with self.assertRaisesRegex(assurance.AssuranceContractError, "dirty"):
            assurance.cleanup_snapshots(self.runs, feature=self.feature)
        self.assertTrue(challenge_path.exists())
        self.assertTrue(verify_path.exists())

    def test_clean_terminal_snapshots_are_removed_without_touching_accepted_head(self) -> None:
        started = self.start_assurance()
        terminal = assurance.abandon(
            self.runs,
            feature=self.feature,
            idempotency_key="abandon-clean-fdp3",
            reason="test clean cleanup",
            now=self.now + timedelta(minutes=7),
        )
        paths = [
            Path(terminal["snapshot"]["challenge_snapshot_path"]),
            Path(terminal["snapshot"]["verification_snapshot_path"]),
        ]
        result = assurance.cleanup_snapshots(self.runs, feature=self.feature)
        self.assertEqual(result["removed_snapshots"], [str(path) for path in paths])
        self.assertTrue(all(not path.exists() for path in paths))
        retry = assurance.cleanup_snapshots(self.runs, feature=self.feature)
        self.assertEqual(retry["removed_snapshots"], [])
        self.assertEqual(retry["already_absent"], [str(path) for path in paths])
        self.assertEqual(self.fdp2.git("rev-parse", "HEAD"), self.accepted_head)


if __name__ == "__main__":
    unittest.main()
