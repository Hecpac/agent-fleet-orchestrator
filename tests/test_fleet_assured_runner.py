from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleet_assured_runner as assured


def event(sequence: int, action: dict, *, status: str = "running") -> dict:
    return {
        "sequence": sequence,
        "event_sha256": f"{sequence:064x}",
        "snapshot": {
            "status": status,
            "accepted_head_sha": "a" * 40,
        },
        "next_action": action,
    }


class FleetAssuredRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runner = object.__new__(assured.AssuredRunner)
        self.runner.runs_dir = self.tmp
        self.runner.mission_id = "00000000-0000-4000-8000-000000000001"
        self.runner.feature = "assured"
        self.runner.root = self.tmp / "missions" / self.runner.mission_id
        self.runner.manifest_path = self.tmp / "fleet-assured.manifest"
        self.runner.manifest = {
            "maker": "surface:1",
            "maker.runner": "interactive",
            "maker.authority": "write",
            "maker.phase": "BUILD",
            "challenge": "surface:2",
            "challenge.runner": "interactive",
            "challenge.authority": "advisory",
            "challenge.phase": "CHALLENGE",
            "verify": "surface:3",
            "verify.runner": "interactive",
            "verify.authority": "verification",
            "verify.phase": "VERIFY",
            "verify.role_type": "verifier",
            "verify.provider": "anthropic",
            "verify.model": "claude-fable-5",
        }

    def test_dispatch_retry_reconciles_the_same_run_without_resend(self) -> None:
        prompt = self.tmp / "prompt.txt"
        prompt.write_text("exact prompt", encoding="utf-8")
        action = {
            "instance": "maker",
            "prompt_file": str(prompt),
            "prompt_sha256": hashlib.sha256(b"exact prompt").hexdigest(),
        }
        sent = subprocess.CompletedProcess(
            ["fleet-send.sh"], 0, '{"run_id":"run-one"}\n', ""
        )
        with mock.patch.object(self.runner, "_record"), mock.patch.object(
            self.runner, "_reconcile_run", side_effect=[None, "run-one"]
        ), mock.patch.object(assured, "run_process", return_value=sent) as run:
            self.assertEqual(self.runner._dispatch(action, "action:1"), "run-one")
            self.assertEqual(self.runner._dispatch(action, "action:1"), "run-one")
        run.assert_called_once()

    def test_fdp2_executes_dispatch_wait_publish_then_accepts(self) -> None:
        dispatch = event(1, {
            "action": "dispatch", "instance": "maker", "prompt_file": "p",
            "prompt_sha256": "a" * 64, "timeout_seconds": 30,
        })
        publish = event(2, {
            "action": "publish", "kind": "proposal", "recipient": "checker",
            "source_instance": "maker", "source_run_id": "run-one", "reply_to": None,
            "payload_sha256": "b" * 64,
        })
        terminal = event(
            3, {"action": "terminal", "status": "accepted", "reason": "checker_accept"},
            status="accepted",
        )
        with mock.patch.object(self.runner, "_dispatch", return_value="run-one") as dispatch_run, \
             mock.patch.object(self.runner, "_wait") as wait, \
             mock.patch.object(self.runner, "_publish", return_value="message-one") as publish_run, \
             mock.patch.object(assured.fdp2, "step", side_effect=[publish, terminal]) as step:
            result = self.runner._drive_fdp2(dispatch)
        self.assertEqual(result, terminal)
        dispatch_run.assert_called_once()
        wait.assert_called_once()
        publish_run.assert_called_once()
        self.assertEqual(step.call_count, 2)

    def test_controller_internal_event_is_normalized_to_public_action(self) -> None:
        internal = {
            "sequence": 9,
            "event_sha256": "9" * 64,
            "snapshot": {"status": "accepted", "accepted_head_sha": "a" * 40},
        }
        public = {
            **internal,
            "next_action": {
                "action": "terminal", "status": "accepted", "reason": "checker_accept",
            },
        }
        with mock.patch.object(assured.fdp2, "public_event", return_value=public) as normalize:
            result = self.runner._drive_fdp2(internal)
        self.assertEqual(result, public)
        normalize.assert_called_once_with(internal)

    def test_fdp3_reconciles_phase_advance_and_verifies(self) -> None:
        advance = event(4, {"action": "advance_phase", "phase": "VERIFY"})
        terminal = event(
            5, {"action": "terminal", "status": "verified", "reason": "claude_verified"},
            status="verified",
        )
        with mock.patch.object(self.runner, "_advance") as phase, mock.patch.object(
            assured.fdp3, "step", return_value=terminal
        ) as step:
            result = self.runner._drive_fdp3(advance)
        self.assertEqual(result, terminal)
        phase.assert_called_once_with("VERIFY", advance["event_sha256"])
        self.assertTrue(step.call_args.kwargs["phase_advanced"])

    def test_build_exit_passes_exact_mission_approval_event(self) -> None:
        self.runner.manifest_path.write_text("feature=assured\n", encoding="utf-8")
        self.runner.manifest_path.with_suffix(".state.json").write_text(
            json.dumps(
                {
                    "active_phase": "BUILD",
                    "history": [{"phase": "BUILD", "evidence": "ready"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        completed = subprocess.CompletedProcess(["fleet_state.py"], 0, "advanced\n", "")
        approval_sha = "d" * 64
        with mock.patch.object(assured, "run_process", return_value=completed) as run:
            self.runner._advance(
                "CHALLENGE",
                "accepted-head",
                approval_event_sha256=approval_sha,
            )
        command = run.call_args.args[0]
        self.assertIn("--approval-event-sha256", command)
        self.assertEqual(command[command.index("--approval-event-sha256") + 1], approval_sha)
        self.assertNotIn("--approved-by", command)

    def test_malformed_or_timed_out_controller_terminal_fails_closed(self) -> None:
        for status, reason in (
            ("indeterminate", "invalid_proposal_contract"),
            ("indeterminate", "run_timeout_exceeded:run-one"),
            ("rejected", "checker_reject"),
        ):
            terminal = event(
                8, {"action": "terminal", "status": status, "reason": reason}, status=status
            )
            with self.subTest(reason=reason), self.assertRaisesRegex(
                assured.AssuredRunnerError, status
            ):
                self.runner._drive_fdp2(terminal)

    def test_wait_requires_exact_run_evidence(self) -> None:
        action = {"instance": "maker", "timeout_seconds": 30}
        response = subprocess.CompletedProcess(
            ["fleet-wait.sh"], 0, json.dumps({"run_id": "different", "status": "succeeded"}), ""
        )
        with mock.patch.object(self.runner, "_record"), mock.patch.object(
            assured, "run_process", return_value=response
        ), self.assertRaisesRegex(assured.AssuredRunnerError, "exact run"):
            self.runner._wait(action, "expected", "action:wait")

    def test_advance_is_noop_after_kill_between_advance_and_ack(self) -> None:
        state_path = self.runner.manifest_path.with_suffix(".state.json")
        state_path.write_text(json.dumps({
            "active_phase": "VERIFY",
            "history": [{"phase": "VERIFY", "evidence": "evidence"}],
        }), encoding="utf-8")
        with mock.patch.object(assured, "run_process") as run:
            self.runner._advance("VERIFY", "evidence")
        run.assert_not_called()

        state_path.write_text(json.dumps({
            "active_phase": "VERIFY",
            "history": [{"phase": "VERIFY", "evidence": "other"}],
        }), encoding="utf-8")
        with self.assertRaisesRegex(assured.AssuredRunnerError, "binding differs"):
            self.runner._advance("VERIFY", "evidence")

    def test_additional_advisory_is_phase_scoped_and_never_writer(self) -> None:
        with mock.patch.object(self.runner, "_phase", return_value="BUILD"), self.assertRaisesRegex(
            assured.AssuredRunnerError, "writer"
        ):
            self.runner.advisory(
                instance="maker", objective="review", idempotency_key="extra:writer"
            )

        run_id = "advisory-run"
        result = self.tmp / "results" / "assured" / f"{run_id}.txt"
        result.parent.mkdir(parents=True)
        result.write_text("read-only evidence", encoding="utf-8")
        lifecycle = [{
            "instance": "verify", "run_id": run_id, "status": "succeeded",
            "result_file": str(result), "phase": "VERIFY", "role": "verifier",
            "task_sha256": hashlib.sha256(
                (
                    f"MISSION_ID={self.runner.mission_id}\nADVISORY_ONLY=true\n"
                    "ACTIVE_PHASE=VERIFY\n\nreview exact evidence\n\n"
                    "Return read-only analysis with exact evidence. Do not modify the repository."
                ).encode("utf-8")
            ).hexdigest(),
            "provider": "anthropic", "model": "claude-fable-5", "variant": None,
        }]
        with mock.patch.object(self.runner, "_phase", return_value="VERIFY"), \
             mock.patch.object(self.runner, "_dispatch", return_value=run_id), \
             mock.patch.object(self.runner, "_wait"), \
             mock.patch.object(self.runner, "_legacy_events", return_value=lifecycle):
            value = self.runner.advisory(
                instance="verify", objective="review exact evidence",
                idempotency_key="extra:verify",
            )
        self.assertEqual(value["status"], "succeeded")
        self.assertEqual(value["result_file"], str(result))

        lifecycle[0]["model"] = "unbound-model"
        with mock.patch.object(self.runner, "_phase", return_value="VERIFY"), \
             mock.patch.object(self.runner, "_dispatch", return_value=run_id), \
             mock.patch.object(self.runner, "_wait"), \
             mock.patch.object(self.runner, "_legacy_events", return_value=lifecycle), \
             self.assertRaisesRegex(assured.AssuredRunnerError, "provenance"):
            self.runner.advisory(
                instance="verify", objective="review exact evidence",
                idempotency_key="extra:verify-drift",
            )

        lifecycle[0]["model"] = "claude-fable-5"
        target = self.tmp / "outside-advisory.txt"
        target.write_text("untrusted target", encoding="utf-8")
        result.unlink()
        result.symlink_to(target)
        with mock.patch.object(self.runner, "_phase", return_value="VERIFY"), \
             mock.patch.object(self.runner, "_dispatch", return_value=run_id), \
             mock.patch.object(self.runner, "_wait"), \
             mock.patch.object(self.runner, "_legacy_events", return_value=lifecycle), \
             self.assertRaisesRegex(assured.AssuredRunnerError, "regular file"):
            self.runner.advisory(
                instance="verify", objective="review exact evidence",
                idempotency_key="extra:verify-symlink",
            )


if __name__ == "__main__":
    unittest.main()
