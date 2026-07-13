from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_dialogue  # noqa: E402
import fleet_dialogue_controller as controller  # noqa: E402
from fleet_ledger import append_event  # noqa: E402


class FleetDialogueControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.feature = "fdp2-test"
        self.target = self.tmp / "target"
        self.target.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "fleet/fdp2-test/maker"], cwd=self.target, check=True)
        subprocess.run(["git", "config", "user.email", "fleet@example.test"], cwd=self.target, check=True)
        subprocess.run(["git", "config", "user.name", "Fleet Test"], cwd=self.target, check=True)
        (self.target / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "base.txt"], cwd=self.target, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=self.target, check=True)
        self.base_sha = self.git("rev-parse", "HEAD")
        self.manifest = self.runs / f"fleet-{self.feature}.manifest"
        self.manifest.write_text(
            "\n".join(
                (
                    "schema_version=3",
                    f"feature={self.feature}",
                    f"preset={controller.FDP2_PRESET}",
                    f"target_repo={self.target}",
                    "workspace=workspace:1",
                    "workspace_uuid=00000000-0000-0000-0000-000000000001",
                    "lead=surface:1",
                    "lead.uuid=00000000-0000-0000-0000-000000000101",
                    "lead.phase=CONTROL",
                    "maker=surface:2",
                    "maker.uuid=00000000-0000-0000-0000-000000000102",
                    "maker.role_type=codex",
                    "maker.phase=BUILD",
                    "maker.authority=write",
                    "maker.provider=openai",
                    "maker.model=gpt-5.6-sol",
                    f"maker.worktree={self.target}",
                    "maker.branch=fleet/fdp2-test/maker",
                    f"maker.base_sha={self.base_sha}",
                    f"maker.final_sha={self.base_sha}",
                    "checker=surface:3",
                    "checker.uuid=00000000-0000-0000-0000-000000000103",
                    "checker.role_type=minimax_checker",
                    "checker.phase=BUILD",
                    "checker.authority=advisory",
                    "checker.provider=minimax",
                    "checker.model=MiniMax-M3",
                    "checker.variant=none",
                    "challenge=surface:4",
                    "challenge.uuid=00000000-0000-0000-0000-000000000104",
                    "challenge.role_type=glm",
                    "challenge.phase=CHALLENGE",
                    "challenge.authority=advisory",
                    "challenge.provider=zai",
                    "challenge.model=glm-5.2",
                    "verify=surface:5",
                    "verify.uuid=00000000-0000-0000-0000-000000000105",
                    "verify.role_type=claude_reviewer",
                    "verify.phase=VERIFY",
                    "verify.authority=verification",
                    "verify.provider=anthropic",
                    "verify.model=claude-fable-5",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (self.runs / f"fleet-{self.feature}.state.json").write_text(
            json.dumps({"schema_version": 1, "feature": self.feature, "active_phase": "BUILD", "history": []}),
            encoding="utf-8",
        )
        self.identity = mock.patch.object(controller, "validate_identity", return_value=[])
        self.dialogue_identity = mock.patch.object(fleet_dialogue, "validate_identity", return_value=[])
        self.identity.start()
        self.dialogue_identity.start()
        self.addCleanup(self.identity.stop)
        self.addCleanup(self.dialogue_identity.stop)
        self.now = datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc)
        self.spec_file = self.tmp / "task-spec.json"
        self.spec_file.write_text(
            json.dumps(
                {
                    "objective": "implement the bounded FDP-2 test change",
                    "negative_scope": ["do not touch unrelated files"],
                    "acceptance_criteria": ["the declared verification passes"],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.target, text=True, capture_output=True, check=True
        ).stdout.strip()

    def commit(self, name: str, content: str) -> str:
        (self.target / name).write_text(content, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-q", "-m", name)
        return self.git("rev-parse", "HEAD")

    def start(self, key: str = "start-1") -> dict:
        return controller.start(
            self.runs,
            feature=self.feature,
            idempotency_key=key,
            spec_file=self.spec_file,
            now=self.now,
        )

    def result_payload(self, value: dict, run_id: str) -> bytes:
        return (json.dumps(value, sort_keys=True) + f"\nFLEET_RESULT:{run_id}:DONE").encode()

    def seed_run(
        self,
        event: dict,
        run_id: str,
        value: dict | None,
        *,
        status: str = "succeeded",
        elapsed: int = 10,
        raw: bytes | None = None,
    ) -> bytes:
        expected = event["snapshot"]["expected"]
        started = self.now + timedelta(minutes=1)
        common = {
            "run_id": run_id,
            "feature": self.feature,
            "instance": expected["instance"],
            "role": self.manifest_value(f"{expected['instance']}.role_type"),
            "phase": "BUILD",
            "task_sha256": expected["prompt_sha256"],
        }
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

    def manifest_value(self, key: str) -> str:
        values = dict(
            line.split("=", 1)
            for line in self.manifest.read_text().splitlines()
            if "=" in line
        )
        return values[key]

    def verification(self, path: str) -> list[dict]:
        return [
            {
                "id": "test-1",
                "command": "python3 -m unittest",
                "status": "passed",
                "evidence": [{"kind": "file", "ref": path}],
                "reason": "",
            }
        ]

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

    def proposal_to_checker(self) -> tuple[dict, str]:
        started = self.start()
        head = self.commit("proposal.txt", "proposal\n")
        run_id = "proposal-run"
        proposal = {
            "schema_version": 1,
            "summary": "implemented proposal",
            "base_sha": self.base_sha,
            "head_sha": head,
            "changes": ["proposal.txt"],
            "verification": self.verification("proposal.txt:1"),
        }
        self.seed_run(started, run_id, proposal)
        run_event = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="step-proposal-run",
            run_id=run_id,
            now=self.now + timedelta(minutes=2),
        )
        message = self.publish(run_event["snapshot"]["expected"], "publish-proposal")
        checker_event = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="step-proposal-message",
            message_id=message["message_id"],
            now=self.now + timedelta(minutes=3),
        )
        checker_prompt = Path(
            checker_event["snapshot"]["expected"]["prompt_file"]
        ).read_text(encoding="utf-8")
        self.assertIn(started["snapshot"]["task_spec_sha256"], checker_prompt)
        self.assertIn('"objective": "implement the bounded FDP-2 test change"', checker_prompt)
        self.assertIn(str(self.target), checker_prompt)
        return checker_event, head

    def accept_conversation(self) -> tuple[dict, str]:
        checker_event, head = self.proposal_to_checker()
        run_id = "checker-accept-run"
        checker = {
            "schema_version": 1,
            "verdict": "ACCEPT",
            "summary": "no findings",
            "findings": [],
        }
        self.seed_run(checker_event, run_id, checker)
        run_event = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="step-checker-run",
            run_id=run_id,
            now=self.now + timedelta(minutes=4),
        )
        message = self.publish(run_event["snapshot"]["expected"], "publish-checker")
        accepted = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="step-checker-message",
            message_id=message["message_id"],
            now=self.now + timedelta(minutes=5),
        )
        return accepted, head

    def test_happy_path_accepts_and_build_gate_revalidates_exact_head(self) -> None:
        accepted, head = self.accept_conversation()

        self.assertEqual(accepted["snapshot"]["status"], "accepted")
        self.assertEqual(accepted["snapshot"]["accepted_head_sha"], head)
        events = controller.load_events(self.runs, self.feature)
        self.assertEqual([event["sequence"] for event in events], list(range(1, 6)))
        self.assertEqual(
            controller.accepted_build_gate(
                self.runs,
                self.feature,
                controller._manifest_values(self.manifest),
            ),
            head,
        )
        task_spec = Path(accepted["snapshot"]["task_spec_file"])
        original_spec = task_spec.read_bytes()
        task_spec.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(controller.ControllerError, "artifact hash mismatch"):
            controller.accepted_build_gate(
                self.runs,
                self.feature,
                controller._manifest_values(self.manifest),
            )
        task_spec.write_bytes(original_spec)
        (self.target / "dirty.txt").write_text("drift\n")
        with self.assertRaisesRegex(controller.ContractError, "not clean"):
            controller.accepted_build_gate(
                self.runs,
                self.feature,
                controller._manifest_values(self.manifest),
            )

    def test_live_receipt_and_offline_archive_verify_same_hashes(self) -> None:
        accepted, _ = self.accept_conversation()
        receipt_path = self.runs / f"fleet-{self.feature}.verification-receipt.json"
        receipt = controller.create_live_receipt(
            self.runs,
            feature=self.feature,
            receipt_path=receipt_path,
            now=self.now + timedelta(minutes=6),
        )
        self.assertEqual(receipt["summary"]["latest_status"], "accepted")
        self.assertEqual(
            receipt["summary"]["control_head_sha256"], accepted["event_sha256"]
        )

        archive = self.tmp / "archive"
        archive.mkdir()
        shutil.copy2(self.manifest, archive / "manifest")
        shutil.copy2(
            controller.ledger_path(self.runs, self.feature),
            archive / "dialogue-control.jsonl",
        )
        shutil.copy2(
            fleet_dialogue.ledger_path(self.runs, self.feature),
            archive / "dialogue.jsonl",
        )
        shutil.copy2(
            self.runs / f"fleet-{self.feature}.ledger.jsonl",
            archive / "ledger.jsonl",
        )
        shutil.copytree(self.runs / "dialogue" / self.feature, archive / "dialogue")
        shutil.copy2(receipt_path, archive / "verification-receipt.json")
        summary = controller.verify_archive(archive)
        self.assertEqual(summary, receipt["summary"])

        payload = next((archive / "dialogue" / "payloads").iterdir())
        payload.write_bytes(b"corrupt")
        with self.assertRaises(fleet_dialogue.DialogueError):
            controller.verify_archive(archive)

    def test_invalid_checker_json_terminalizes_indeterminate(self) -> None:
        checker_event, _ = self.proposal_to_checker()
        run_id = "checker-invalid-run"
        self.seed_run(
            checker_event,
            run_id,
            None,
            raw=f"not-json\nFLEET_RESULT:{run_id}:DONE".encode(),
        )
        terminal = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="invalid-checker",
            run_id=run_id,
            now=self.now + timedelta(minutes=4),
        )
        self.assertEqual(terminal["snapshot"]["status"], "indeterminate")
        self.assertIn("invalid_checker_contract", terminal["snapshot"]["terminal_reason"])

    def test_checker_verdict_and_severity_matrix_is_strict(self) -> None:
        finding = {
            "id": "finding-1",
            "severity": "medium",
            "description": "bounded defect",
            "evidence": [{"kind": "file", "ref": "proposal.txt:1"}],
        }
        with self.assertRaises(controller.ContractError):
            controller._checker_contract(
                {
                    "schema_version": 1,
                    "verdict": "ACCEPT",
                    "summary": "contradictory acceptance",
                    "findings": [finding],
                }
            )
        with self.assertRaises(controller.ContractError):
            controller._checker_contract(
                {
                    "schema_version": 1,
                    "verdict": "REJECT",
                    "summary": "no critical issue",
                    "findings": [finding],
                }
            )
        critical = {**finding, "severity": "critical"}
        with self.assertRaises(controller.ContractError):
            controller._checker_contract(
                {
                    "schema_version": 1,
                    "verdict": "REVISE",
                    "summary": "critical cannot revise",
                    "findings": [critical],
                }
            )
        accepted_reject = controller._checker_contract(
            {
                "schema_version": 1,
                "verdict": "REJECT",
                "summary": "critical issue",
                "findings": [critical],
            }
        )
        self.assertEqual(accepted_reject["verdict"], "REJECT")

    def test_run_over_30_minutes_is_indeterminate_even_when_succeeded(self) -> None:
        started = self.start()
        head = self.commit("late.txt", "late\n")
        run_id = "late-run"
        proposal = {
            "schema_version": 1,
            "summary": "late",
            "base_sha": self.base_sha,
            "head_sha": head,
            "changes": ["late.txt"],
            "verification": self.verification("late.txt"),
        }
        self.seed_run(started, run_id, proposal, elapsed=controller.RUN_TIMEOUT_SECONDS + 1)
        terminal = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="late-step",
            run_id=run_id,
            now=self.now + timedelta(minutes=32),
        )
        self.assertEqual(terminal["snapshot"]["status"], "indeterminate")
        self.assertEqual(terminal["snapshot"]["terminal_reason"], f"run_timeout_exceeded:{run_id}")

    def test_failed_run_preserves_cause_and_late_step_cannot_change_terminal(self) -> None:
        started = self.start()
        self.seed_run(started, "maker-failed", None, status="failed")
        terminal = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="consume-failed-maker",
            run_id="maker-failed",
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(terminal["snapshot"]["status"], "failed")
        self.assertEqual(terminal["snapshot"]["terminal_reason"], "run_failed:maker-failed")
        event_count = len(controller.load_events(self.runs, self.feature))

        with self.assertRaises(controller.ControllerConflict):
            controller.step(
                self.runs,
                feature=self.feature,
                idempotency_key="late-step-after-failure",
                run_id="maker-failed",
                now=self.now + timedelta(minutes=3),
            )
        self.assertEqual(len(controller.load_events(self.runs, self.feature)), event_count)

    def test_show_records_deadline_once_with_system_idempotency(self) -> None:
        started = self.start()
        expired = controller.show(
            self.runs,
            feature=self.feature,
            now=self.now + timedelta(hours=4, seconds=1),
        )
        repeated = controller.show(
            self.runs,
            feature=self.feature,
            now=self.now + timedelta(hours=5),
        )
        self.assertEqual(expired["snapshot"]["status"], "indeterminate")
        self.assertEqual(expired["event_type"], "deadline_expired")
        self.assertEqual(repeated["event_id"], expired["event_id"])
        self.assertEqual(len(controller.load_events(self.runs, self.feature)), 2)
        self.assertEqual(started["conversation_id"], expired["conversation_id"])

    def test_mutation_idempotency_and_hash_chain_corruption_fail_closed(self) -> None:
        first = self.start("same-key")
        retry = self.start("same-key")
        self.assertEqual(retry["event_id"], first["event_id"])
        with self.assertRaises(controller.ControllerConflict):
            controller.abandon(
                self.runs,
                feature=self.feature,
                idempotency_key="same-key",
                reason="different request",
                now=self.now + timedelta(minutes=1),
            )

        path = controller.ledger_path(self.runs, self.feature)
        row = json.loads(path.read_text())
        row["snapshot"]["revision_round"] = 1
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(controller.ControllerError, "hash"):
            controller.load_events(self.runs, self.feature)

    def test_start_copies_and_hashes_strict_task_spec(self) -> None:
        started = self.start("spec-start")
        snapshot = started["snapshot"]
        durable_spec = Path(snapshot["task_spec_file"])

        self.assertNotEqual(durable_spec, self.spec_file)
        self.assertEqual(durable_spec.read_bytes(), self.spec_file.read_bytes())
        self.assertEqual(
            snapshot["task_spec_sha256"],
            hashlib.sha256(self.spec_file.read_bytes()).hexdigest(),
        )
        prompt = Path(snapshot["expected"]["prompt_file"]).read_text(encoding="utf-8")
        self.assertFalse(prompt.endswith("\n"))
        self.assertEqual(
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            snapshot["expected"]["prompt_sha256"],
        )
        self.assertIn(snapshot["task_spec_sha256"], prompt)
        self.assertIn('"objective": "implement the bounded FDP-2 test change"', prompt)

        self.spec_file.write_text(
            json.dumps(
                {
                    "objective": "a changed objective",
                    "negative_scope": ["do not touch unrelated files"],
                    "acceptance_criteria": ["the declared verification passes"],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(controller.ControllerConflict):
            self.start("spec-start")

        durable_spec.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(controller.ControllerError, "artifact hash mismatch"):
            controller._verify_live_control_artifacts(
                self.runs,
                self.feature,
                controller.load_events(self.runs, self.feature),
            )

    def test_start_rejects_task_spec_with_extra_or_empty_fields(self) -> None:
        self.spec_file.write_text(
            json.dumps(
                {
                    "objective": "bounded change",
                    "negative_scope": [],
                    "acceptance_criteria": ["tests pass"],
                    "unexpected": True,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(controller.ControllerError):
            self.start("bad-spec")

    def test_revision_requires_every_finding_and_opens_round_before_run(self) -> None:
        checker_event, head = self.proposal_to_checker()
        checker_run = "checker-revise-run"
        checker = {
            "schema_version": 1,
            "verdict": "REVISE",
            "summary": "one issue",
            "findings": [
                {
                    "id": "finding-1",
                    "severity": "high",
                    "description": "needs correction",
                    "evidence": [{"kind": "file", "ref": "proposal.txt:1"}],
                }
            ],
        }
        self.seed_run(checker_event, checker_run, checker)
        checker_result = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="checker-revise-result",
            run_id=checker_run,
            now=self.now + timedelta(minutes=4),
        )
        message = self.publish(checker_result["snapshot"]["expected"], "publish-revise")
        revision_expected = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="open-round",
            message_id=message["message_id"],
            now=self.now + timedelta(minutes=5),
        )
        self.assertEqual(revision_expected["snapshot"]["revision_round"], 1)
        self.assertEqual(revision_expected["snapshot"]["open_finding_ids"], ["finding-1"])
        revision_prompt = Path(
            revision_expected["snapshot"]["expected"]["prompt_file"]
        ).read_text(encoding="utf-8")
        self.assertIn(revision_expected["snapshot"]["task_spec_sha256"], revision_prompt)
        self.assertIn('"negative_scope": [', revision_prompt)

        revision_head = self.commit("revision.txt", "revision\n")
        invalid_revision = {
            "schema_version": 1,
            "rebuttal": [],
            "revision_summary": "missed finding",
            "base_sha": head,
            "head_sha": revision_head,
            "changes": ["revision.txt"],
            "verification": self.verification("revision.txt"),
        }
        revision_run = "maker-revision-run"
        self.seed_run(revision_expected, revision_run, invalid_revision)
        terminal = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="bad-revision",
            run_id=revision_run,
            now=self.now + timedelta(minutes=6),
        )
        self.assertEqual(terminal["snapshot"]["status"], "indeterminate")
        self.assertIn("cover every Checker finding", terminal["snapshot"]["terminal_reason"])

    def test_valid_revision_uses_one_run_for_rebuttal_and_revision_messages(self) -> None:
        checker_event, proposal_head = self.proposal_to_checker()
        checker_run = "checker-one-finding"
        checker = {
            "schema_version": 1,
            "verdict": "REVISE",
            "summary": "fix one issue",
            "findings": [
                {
                    "id": "finding-1",
                    "severity": "medium",
                    "description": "add correction",
                    "evidence": [{"kind": "file", "ref": "proposal.txt:1"}],
                }
            ],
        }
        self.seed_run(checker_event, checker_run, checker)
        checker_result = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="one-finding-run",
            run_id=checker_run,
            now=self.now + timedelta(minutes=4),
        )
        challenge = self.publish(checker_result["snapshot"]["expected"], "one-finding-message")
        revision_expected = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="one-finding-open",
            message_id=challenge["message_id"],
            now=self.now + timedelta(minutes=5),
        )

        revision_head = self.commit("fixed.txt", "fixed\n")
        revision_run = "combined-maker-run"
        revision = {
            "schema_version": 1,
            "rebuttal": [
                {
                    "finding_id": "finding-1",
                    "disposition": "accepted",
                    "reason": "corrected",
                    "evidence": [{"kind": "file", "ref": "fixed.txt:1"}],
                }
            ],
            "revision_summary": "added correction",
            "base_sha": proposal_head,
            "head_sha": revision_head,
            "changes": ["fixed.txt"],
            "verification": self.verification("fixed.txt:1"),
        }
        self.seed_run(revision_expected, revision_run, revision)
        rebuttal_expected = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="combined-run",
            run_id=revision_run,
            now=self.now + timedelta(minutes=6),
        )
        rebuttal = self.publish(rebuttal_expected["snapshot"]["expected"], "publish-rebuttal")
        revision_message_expected = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="bind-rebuttal",
            message_id=rebuttal["message_id"],
            now=self.now + timedelta(minutes=7),
        )
        revision_message = self.publish(
            revision_message_expected["snapshot"]["expected"], "publish-revision"
        )
        next_checker = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="bind-revision",
            message_id=revision_message["message_id"],
            now=self.now + timedelta(minutes=8),
        )

        self.assertEqual(rebuttal["source_run_id"], revision_run)
        self.assertEqual(revision_message["source_run_id"], revision_run)
        self.assertEqual(rebuttal["payload_sha256"], revision_message["payload_sha256"])
        self.assertEqual(revision_message["reply_to"], rebuttal["message_id"])
        self.assertEqual(next_checker["snapshot"]["status"], "awaiting_checker_run")
        self.assertEqual(next_checker["snapshot"]["current_head_sha"], revision_head)
        self.assertEqual(next_checker["snapshot"]["revision_round"], 1)

    def test_fourth_revision_is_never_opened(self) -> None:
        checker_expected, current_head = self.proposal_to_checker()
        minute = 4
        for round_number in range(1, controller.MAX_REVISION_ROUNDS + 1):
            checker_run = f"checker-revise-{round_number}"
            finding_id = f"finding-{round_number}"
            checker = {
                "schema_version": 1,
                "verdict": "REVISE",
                "summary": f"revision {round_number} required",
                "findings": [
                    {
                        "id": finding_id,
                        "severity": "medium",
                        "description": "bounded correction required",
                        "evidence": [{"kind": "file", "ref": "proposal.txt:1"}],
                    }
                ],
            }
            self.seed_run(checker_expected, checker_run, checker)
            checker_result = controller.step(
                self.runs,
                feature=self.feature,
                idempotency_key=f"round-{round_number}-checker-run",
                run_id=checker_run,
                now=self.now + timedelta(minutes=minute),
            )
            minute += 1
            challenge = self.publish(
                checker_result["snapshot"]["expected"],
                f"round-{round_number}-checker-message",
            )
            revision_expected = controller.step(
                self.runs,
                feature=self.feature,
                idempotency_key=f"round-{round_number}-open",
                message_id=challenge["message_id"],
                now=self.now + timedelta(minutes=minute),
            )
            minute += 1
            self.assertEqual(revision_expected["snapshot"]["revision_round"], round_number)

            filename = f"revision-{round_number}.txt"
            revision_head = self.commit(filename, f"revision {round_number}\n")
            revision_run = f"maker-revision-{round_number}"
            revision = {
                "schema_version": 1,
                "rebuttal": [
                    {
                        "finding_id": finding_id,
                        "disposition": "accepted",
                        "reason": "corrected",
                        "evidence": [{"kind": "file", "ref": f"{filename}:1"}],
                    }
                ],
                "revision_summary": f"completed revision {round_number}",
                "base_sha": current_head,
                "head_sha": revision_head,
                "changes": [filename],
                "verification": self.verification(f"{filename}:1"),
            }
            self.seed_run(revision_expected, revision_run, revision)
            rebuttal_expected = controller.step(
                self.runs,
                feature=self.feature,
                idempotency_key=f"round-{round_number}-maker-run",
                run_id=revision_run,
                now=self.now + timedelta(minutes=minute),
            )
            minute += 1
            rebuttal = self.publish(
                rebuttal_expected["snapshot"]["expected"],
                f"round-{round_number}-rebuttal",
            )
            revision_message_expected = controller.step(
                self.runs,
                feature=self.feature,
                idempotency_key=f"round-{round_number}-bind-rebuttal",
                message_id=rebuttal["message_id"],
                now=self.now + timedelta(minutes=minute),
            )
            minute += 1
            revision_message = self.publish(
                revision_message_expected["snapshot"]["expected"],
                f"round-{round_number}-revision",
            )
            checker_expected = controller.step(
                self.runs,
                feature=self.feature,
                idempotency_key=f"round-{round_number}-bind-revision",
                message_id=revision_message["message_id"],
                now=self.now + timedelta(minutes=minute),
            )
            minute += 1
            current_head = revision_head

        final_checker_run = "checker-fourth-revise"
        self.seed_run(
            checker_expected,
            final_checker_run,
            {
                "schema_version": 1,
                "verdict": "REVISE",
                "summary": "would require a forbidden fourth round",
                "findings": [
                    {
                        "id": "finding-4",
                        "severity": "high",
                        "description": "another correction",
                        "evidence": [{"kind": "file", "ref": "proposal.txt:1"}],
                    }
                ],
            },
        )
        final_checker_result = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="fourth-checker-run",
            run_id=final_checker_run,
            now=self.now + timedelta(minutes=minute),
        )
        final_message = self.publish(
            final_checker_result["snapshot"]["expected"], "fourth-checker-message"
        )
        terminal = controller.step(
            self.runs,
            feature=self.feature,
            idempotency_key="refuse-fourth-round",
            message_id=final_message["message_id"],
            now=self.now + timedelta(minutes=minute + 1),
        )
        self.assertEqual(terminal["snapshot"]["status"], "indeterminate")
        self.assertEqual(terminal["snapshot"]["revision_round"], 3)
        self.assertEqual(terminal["snapshot"]["terminal_reason"], "max_revision_rounds_exhausted")

    def test_abandon_does_not_modify_active_run_or_lease(self) -> None:
        started = self.start()
        lease = self.runs / "locks" / f"{self.feature}.maker.lock"
        lease.mkdir(parents=True)
        (lease / "lease.json").write_text('{"run_id":"active"}\n')
        terminal = controller.abandon(
            self.runs,
            feature=self.feature,
            idempotency_key="abandon-dialogue",
            reason="operator stop",
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(terminal["snapshot"]["status"], "abandoned")
        self.assertTrue(lease.exists())
        self.assertEqual(started["conversation_id"], terminal["conversation_id"])


if __name__ == "__main__":
    unittest.main()
