from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "scripts" / "fleet_state.py"
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_dialogue_controller as controller  # noqa: E402
import fleet_mission  # noqa: E402
import fleet_mission_state as mission_state  # noqa: E402
import fleet_state  # noqa: E402
import workflow_config  # noqa: E402


class FleetStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.manifest = Path(self.tempdir.name) / "fleet-test.manifest"
        self.manifest.write_text(
            "feature=test\n"
            "lead=surface:1\nlead.uuid=00000000-0000-0000-0000-000000000101\nlead.phase=CONTROL\n"
            "build=surface:2\nbuild.phase=BUILD\n"
            "verify=surface:3\nverify.phase=VERIFY\n",
            encoding="utf-8",
        )

    def run_state(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(STATE), *args, str(self.manifest)] if args[0] == "init" else
            ["python3", str(STATE), args[0], str(self.manifest), *args[1:]],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def mission_approval(self) -> str:
        runs = self.manifest.parent
        target = runs / "target"
        target.mkdir()
        compiled = workflow_config.compile_path(ROOT / "workflows" / "implementation.yaml")
        mission_id, _ = fleet_mission.create_mission(
            runs,
            compiled=compiled,
            feature="test",
            objective="exercise exact approval provenance",
            target_repo=target.resolve(),
            base_sha="a" * 40,
            idempotency_key="create:test-approval",
        )
        current = fleet_mission.load_state(runs, mission_id)
        if current["risk"] != "high":
            mission_state.append_event(
                runs,
                mission_id,
                kind="risk_escalated",
                actor="CONTROL",
                idempotency_key="approval:risk",
                payload={
                    "from": current["risk"],
                    "to": "high",
                    "categories": ["production"],
                    "reason": "test exact approval provenance",
                },
            )
        request, _ = mission_state.append_event(
            runs,
            mission_id,
            kind="assurance_requested",
            actor="CONTROL",
            idempotency_key="approval:request",
            payload={
                "risk": "high",
                "categories": ["production"],
                "scope": str(target.resolve()),
                "workflow_digest": compiled["workflow_digest"],
            },
        )
        approval, _ = mission_state.append_event(
            runs,
            mission_id,
            kind="assurance_approved",
            actor="HUMAN",
            idempotency_key="approval:decision",
            payload={
                "approval_id": str(uuid.uuid4()),
                "request_event_sha256": request["event_sha256"],
                "workflow_digest": compiled["workflow_digest"],
                "scope": str(target.resolve()),
                "risk": "high",
                "expires_at": "2099-07-16T00:00:00Z",
                "approved_by_sha256": "b" * 64,
                "decision": "approved",
            },
        )
        mission_state.append_event(
            runs,
            mission_id,
            kind="assurance_boot_started",
            actor="CONTROL",
            idempotency_key="approval:boot",
            payload={
                "preset": "fleet_dialogue",
                "approval_event_sha256": approval["event_sha256"],
            },
        )
        mission_state.append_event(
            runs,
            mission_id,
            kind="assurance_started",
            actor="CONTROL",
            idempotency_key="approval:started",
            payload={
                "manifest": str(self.manifest),
                "approval_event_sha256": approval["event_sha256"],
            },
        )
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                f"mission_id={mission_id}\n"
                "mode=assured\n"
                f"target_repo={target.resolve()}\n"
            )
        return approval["event_sha256"]

    def test_future_phase_is_closed_until_evidence_backed_advance(self) -> None:
        self.assertEqual(self.run_state("init").returncode, 0)
        closed = self.run_state("check", "build")
        self.assertEqual(closed.returncode, 3)
        advanced = self.run_state("advance", "BUILD", "--evidence", "scope-approved")
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        self.assertEqual(self.run_state("check", "build").returncode, 0)
        self.assertEqual(self.run_state("check", "verify").returncode, 3)
        self.assertEqual(
            self.run_state(
                "advance", "VERIFY", "--evidence", "build-frozen", "--approved-by", "hector"
            ).returncode,
            0,
        )
        self.assertEqual(self.run_state("check", "build").returncode, 3)
        self.assertEqual(self.run_state("check", "verify").returncode, 0)

    def test_standalone_build_exit_requires_operator_attestation(self) -> None:
        self.assertEqual(self.run_state("init").returncode, 0)
        self.assertEqual(
            self.run_state("advance", "BUILD", "--evidence", "scope-approved").returncode, 0
        )
        unapproved = self.run_state("advance", "VERIFY", "--evidence", "diff-ready")
        self.assertEqual(unapproved.returncode, 2)
        self.assertIn("--approved-by", unapproved.stderr)
        state = json.loads(self.manifest.with_suffix(".state.json").read_text())
        self.assertEqual(state["active_phase"], "BUILD")

        approved = self.run_state(
            "advance", "VERIFY", "--evidence", "diff-ready", "--approved-by", "hector"
        )
        self.assertEqual(approved.returncode, 0, approved.stderr)
        state = json.loads(self.manifest.with_suffix(".state.json").read_text())
        self.assertEqual(state["active_phase"], "VERIFY")
        self.assertEqual(state["history"][-1]["approved_by"], "hector")

    def test_mission_bound_build_exit_requires_exact_active_approval_event(self) -> None:
        approval_sha = self.mission_approval()
        self.assertEqual(self.run_state("init").returncode, 0)
        self.assertEqual(
            self.run_state("advance", "BUILD", "--evidence", "scope-approved").returncode,
            0,
        )

        raw_label = self.run_state(
            "advance", "VERIFY", "--evidence", "diff-ready", "--approved-by", "CONTROL"
        )
        self.assertEqual(raw_label.returncode, 2)
        self.assertIn("rejects --approved-by text", raw_label.stderr)

        wrong_event = self.run_state(
            "advance",
            "VERIFY",
            "--evidence",
            "diff-ready",
            "--approval-event-sha256",
            "c" * 64,
        )
        self.assertEqual(wrong_event.returncode, 3)
        self.assertIn("not the active Mission approval", wrong_event.stderr)

        manifest_values = fleet_state.manifest_values(self.manifest)
        wrong_mode = dict(manifest_values, mode="guided")
        with self.assertRaisesRegex(fleet_state.PhaseApprovalError, "mode=assured"):
            fleet_state.validate_mission_approval(
                self.manifest,
                wrong_mode,
                approval_sha,
            )

        with self.assertRaisesRegex(fleet_state.PhaseApprovalError, "expired"):
            fleet_state.validate_mission_approval(
                self.manifest,
                manifest_values,
                approval_sha,
                now=datetime(2100, 1, 1, tzinfo=timezone.utc),
            )

        advanced = self.run_state(
            "advance",
            "VERIFY",
            "--evidence",
            "diff-ready",
            "--approval-event-sha256",
            approval_sha,
        )
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        state = json.loads(self.manifest.with_suffix(".state.json").read_text())
        self.assertEqual(state["active_phase"], "VERIFY")
        self.assertEqual(state["history"][-1]["approval_event_sha256"], approval_sha)
        self.assertNotIn("approved_by", state["history"][-1])

    def test_autonomous_mode_opens_all_roster_phases_without_approval(self) -> None:
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write("mode=autonomous\n")
        self.assertEqual(self.run_state("init").returncode, 0)
        self.assertEqual(self.run_state("check", "build").returncode, 0)
        self.assertEqual(self.run_state("check", "verify").returncode, 0)
        self.assertEqual(
            self.run_state("advance", "BUILD", "--evidence", "lead-started").returncode,
            0,
        )
        advanced = self.run_state("advance", "VERIFY", "--evidence", "lead-verified")
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        state = json.loads(self.manifest.with_suffix(".state.json").read_text())
        self.assertEqual(state["active_phase"], "VERIFY")
        self.assertNotIn("approved_by", state["history"][-1])

    def test_skipping_configured_phase_is_rejected(self) -> None:
        self.assertEqual(self.run_state("init").returncode, 0)
        skipped = self.run_state("advance", "VERIFY", "--evidence", "bad")
        self.assertEqual(skipped.returncode, 2)
        state = json.loads(self.manifest.with_suffix(".state.json").read_text())
        self.assertEqual(state["active_phase"], "CONTROL")

    def test_fdp2_requires_latest_accepted_clean_head_before_leaving_build(self) -> None:
        repo = Path(self.tempdir.name) / "target"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "fleet/test/maker"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "fleet@example.test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Fleet Test"], cwd=repo, check=True)
        (repo / "accepted.txt").write_text("accepted\n")
        subprocess.run(["git", "add", "accepted.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "accepted"], cwd=repo, check=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True
        ).stdout.strip()
        self.manifest.write_text(
            "feature=test\n"
            "preset=fleet_dialogue\n"
            f"target_repo={repo}\n"
            "lead=surface:1\nlead.uuid=00000000-0000-0000-0000-000000000101\nlead.phase=CONTROL\n"
            "maker=surface:2\nmaker.uuid=00000000-0000-0000-0000-000000000102\n"
            "maker.role_type=codex\nmaker.phase=BUILD\nmaker.authority=write\n"
            "maker.provider=openai\nmaker.model=gpt-5.6-sol\n"
            f"maker.worktree={repo}\nmaker.branch=fleet/test/maker\nmaker.base_sha={head}\n"
            "checker=surface:3\nchecker.uuid=00000000-0000-0000-0000-000000000103\n"
            "checker.role_type=minimax_checker\nchecker.phase=BUILD\nchecker.authority=advisory\n"
            "checker.provider=minimax\nchecker.model=MiniMax-M3\nchecker.variant=none\n"
            "challenge=surface:4\nchallenge.uuid=00000000-0000-0000-0000-000000000104\n"
            "challenge.role_type=glm\nchallenge.phase=CHALLENGE\nchallenge.authority=advisory\n"
            "challenge.provider=zai\nchallenge.model=glm-5.2\n"
            "verify=surface:5\nverify.uuid=00000000-0000-0000-0000-000000000105\n"
            "verify.role_type=claude_reviewer\nverify.phase=VERIFY\nverify.authority=verification\n"
            "verify.provider=anthropic\nverify.model=claude-fable-5\n",
            encoding="utf-8",
        )
        self.assertEqual(self.run_state("init").returncode, 0)
        self.assertEqual(
            self.run_state("advance", "BUILD", "--evidence", "scope-approved").returncode,
            0,
        )
        no_dialogue = self.run_state(
            "advance", "CHALLENGE", "--evidence", "diff", "--approved-by", "hector"
        )
        self.assertEqual(no_dialogue.returncode, 3)
        self.assertIn("requires a conversation", no_dialogue.stderr)

        created = datetime.now(timezone.utc).isoformat()
        conversation_id = "00000000-0000-0000-0000-000000000777"
        task_spec = b'{"objective":"test","negative_scope":["none"],"acceptance_criteria":["gate"]}'
        task_spec_file = (
            self.manifest.parent
            / "dialogue"
            / "test"
            / "control"
            / conversation_id
            / "task-spec.json"
        )
        task_spec_file.parent.mkdir(parents=True)
        task_spec_file.write_bytes(task_spec)
        (self.manifest.parent / "dialogue" / "test" / "payloads").mkdir(parents=True)
        (self.manifest.parent / "fleet-test.dialogue.jsonl").touch()
        snapshot = {
            "status": "accepted",
            "created_at": created,
            "deadline_at": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
            "max_revision_rounds": controller.MAX_REVISION_ROUNDS,
            "run_timeout_seconds": controller.RUN_TIMEOUT_SECONDS,
            "revision_round": 0,
            "maker_instance": "maker",
            "checker_instance": "checker",
            "task_spec_file": str(task_spec_file),
            "task_spec_sha256": hashlib.sha256(task_spec).hexdigest(),
            "start_head_sha": head,
            "current_head_sha": head,
            "accepted_head_sha": head,
            "last_message_id": None,
            "maker_verification_ids": [],
            "open_finding_ids": [],
            "expected": None,
            "terminal_reason": "checker_accept",
        }
        controller._append_event_locked(
            self.manifest.parent,
            "test",
            [],
            conversation_id=conversation_id,
            event_type="conversation_accepted",
            idempotency_key="accepted-test",
            request={"test": "accepted"},
            snapshot=snapshot,
        )
        advanced = self.run_state(
            "advance", "CHALLENGE", "--evidence", "diff", "--approved-by", "hector"
        )
        self.assertEqual(advanced.returncode, 0, advanced.stderr)


if __name__ == "__main__":
    unittest.main()
