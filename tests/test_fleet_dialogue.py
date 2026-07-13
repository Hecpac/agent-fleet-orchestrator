from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_dialogue  # noqa: E402
from fleet_ledger import append_event  # noqa: E402


class FleetDialogueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.runs = Path(self.tempdir.name) / "runs"
        self.runs.mkdir()
        self.feature = "dialogue-test"
        self.identity_patch = mock.patch.object(
            fleet_dialogue, "validate_identity", return_value=[]
        )
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)
        (self.runs / f"fleet-{self.feature}.manifest").write_text(
            "\n".join(
                (
                    "schema_version=3",
                    f"feature={self.feature}",
                    "workspace=workspace:1",
                    "workspace_uuid=00000000-0000-0000-0000-000000000001",
                    "lead=surface:1",
                    "lead.uuid=00000000-0000-0000-0000-000000000101",
                    "lead.role_type=codex",
                    "lead.phase=CONTROL",
                    "maker=surface:2",
                    "maker.uuid=00000000-0000-0000-0000-000000000102",
                    "maker.role_type=codex_candidate",
                    "maker.phase=BUILD",
                    "checker=surface:3",
                    "checker.uuid=00000000-0000-0000-0000-000000000103",
                    "checker.role_type=reviewer",
                    "checker.phase=VERIFY",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def seed_result(
        self,
        run_id: str,
        payload: bytes,
        *,
        instance: str = "maker",
        status: str = "succeeded",
        result_file: bool = True,
    ) -> Path:
        path = self.runs / "results" / self.feature / f"{run_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        event = {
            "timestamp": "2026-07-13T00:00:00+00:00",
            "run_id": run_id,
            "feature": self.feature,
            "instance": instance,
            "role": "codex_candidate",
            "phase": "BUILD",
            "status": status,
            "task_sha256": "a" * 64,
        }
        if result_file:
            event["result_file"] = str(path)
        append_event(self.runs / f"fleet-{self.feature}.ledger.jsonl", event)
        return path

    def publish(self, run_id: str, **overrides):
        options = {
            "feature": self.feature,
            "kind": "proposal",
            "recipient": "checker",
            "source_instance": "maker",
            "source_run_id": run_id,
            "idempotency_key": f"proposal:{run_id}",
            "reply_to": None,
        }
        options.update(overrides)
        return fleet_dialogue.publish(self.runs, **options)

    def test_publish_copies_exact_payload_and_queries_by_recipient(self) -> None:
        run_id = "run-proposal"
        payload = b"answer\nFLEET_RESULT:run-proposal:DONE"
        source = self.seed_result(run_id, payload)

        message = self.publish(run_id)

        self.assertEqual(message["schema_version"], 1)
        self.assertEqual(message["sender"], "CONTROL")
        self.assertEqual(message["recipient"], "checker")
        self.assertEqual(message["source_run_id"], run_id)
        self.assertEqual(message["payload_sha256"], hashlib.sha256(payload).hexdigest())
        stored = fleet_dialogue.payload_path(
            self.runs, self.feature, message["payload_sha256"]
        )
        self.assertEqual(stored.read_bytes(), source.read_bytes())
        self.assertEqual(
            fleet_dialogue.query(self.runs, feature=self.feature, recipient="checker"),
            [message],
        )
        self.assertEqual(
            fleet_dialogue.verify(self.runs, feature=self.feature),
            {"messages": 1, "payloads": 1, "payload_bytes": len(payload)},
        )

    def test_idempotent_retry_returns_existing_and_changed_request_fails(self) -> None:
        run_id = "run-idempotent"
        self.seed_result(run_id, b"stable result")
        first = self.publish(run_id)
        second = self.publish(run_id)
        self.assertEqual(second, first)
        self.assertEqual(
            len(fleet_dialogue.load_messages(self.runs, self.feature)), 1
        )

        with self.assertRaises(fleet_dialogue.DialogueConflict):
            self.publish(run_id, kind="challenge")

    def test_reply_must_name_prior_message_in_same_ledger(self) -> None:
        first_run = "run-first"
        second_run = "run-second"
        self.seed_result(first_run, b"proposal")
        self.seed_result(second_run, b"challenge", instance="checker")
        first = self.publish(first_run)
        second = self.publish(
            second_run,
            kind="challenge",
            recipient="maker",
            source_instance="checker",
            idempotency_key="challenge:run-second",
            reply_to=first["message_id"],
        )
        self.assertEqual(second["reply_to"], first["message_id"])

        with self.assertRaisesRegex(
            fleet_dialogue.DialogueError, "prior message"
        ):
            self.publish(
                second_run,
                idempotency_key="unknown-parent",
                reply_to="00000000-0000-0000-0000-000000000999",
            )

    def test_only_exact_succeeded_result_file_is_publishable(self) -> None:
        failed_run = "run-failed"
        self.seed_result(failed_run, b"failure", status="failed")
        with self.assertRaisesRegex(fleet_dialogue.DialogueError, "not terminal succeeded"):
            self.publish(failed_run)

        missing_run = "run-no-file"
        self.seed_result(missing_run, b"orphan", result_file=False)
        with self.assertRaisesRegex(fleet_dialogue.DialogueError, "no durable result_file"):
            self.publish(missing_run)

        outside_run = "run-outside"
        expected = self.seed_result(outside_run, b"inside")
        outside = self.runs / "outside.txt"
        outside.write_bytes(b"outside")
        ledger = self.runs / f"fleet-{self.feature}.ledger.jsonl"
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        rows[-1]["result_file"] = str(outside)
        ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with self.assertRaisesRegex(fleet_dialogue.DialogueError, "outside"):
            self.publish(outside_run)

    def test_payload_limit_and_close_marker_fail_closed(self) -> None:
        oversized = "run-oversized"
        self.seed_result(oversized, b"x" * (fleet_dialogue.MAX_PAYLOAD_BYTES + 1))
        with self.assertRaisesRegex(fleet_dialogue.DialogueError, "exceeds"):
            self.publish(oversized)

        run_id = "run-closing"
        self.seed_result(run_id, b"ready")
        marker = self.runs / "locks" / f"{self.feature}.closing"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(fleet_dialogue.DialogueClosing):
            self.publish(run_id)

    def test_live_identity_mismatch_and_result_symlink_are_rejected(self) -> None:
        run_id = "run-identity"
        result = self.seed_result(run_id, b"identity-bound")
        fleet_dialogue.validate_identity.return_value = [
            "surface identity mismatch: checker (surface:3)"
        ]
        with self.assertRaisesRegex(fleet_dialogue.DialogueError, "identity mismatch"):
            self.publish(run_id)
        self.assertFalse(
            (self.runs / f"fleet-{self.feature}.dialogue.jsonl").exists()
        )

        fleet_dialogue.validate_identity.return_value = []
        outside = self.runs / "symlink-target.txt"
        outside.write_bytes(b"substituted")
        result.unlink()
        result.symlink_to(outside)
        with self.assertRaisesRegex(fleet_dialogue.DialogueError, "symlink"):
            self.publish(run_id)

    def test_corrupt_payload_is_rejected_by_retry_read_and_verify(self) -> None:
        run_id = "run-corrupt"
        self.seed_result(run_id, b"trusted")
        message = self.publish(run_id)
        stored = fleet_dialogue.payload_path(
            self.runs, self.feature, message["payload_sha256"]
        )
        stored.write_bytes(b"corrupt")
        with self.assertRaises(fleet_dialogue.DialogueError):
            self.publish(run_id)
        with self.assertRaises(fleet_dialogue.DialogueError):
            fleet_dialogue.verify(self.runs, feature=self.feature)

    def test_parallel_cli_retry_appends_one_message(self) -> None:
        run_id = "run-parallel"
        self.seed_result(run_id, b"parallel result")
        command = [
            sys.executable,
            str(ROOT / "scripts" / "fleet_dialogue.py"),
            "publish",
            str(self.runs),
            "--feature",
            self.feature,
            "--kind",
            "proposal",
            "--recipient",
            "checker",
            "--source-instance",
            "maker",
            "--source-run-id",
            run_id,
            "--idempotency-key",
            "parallel:one",
        ]
        processes = [
            subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._cli_env(),
            )
            for _ in range(4)
        ]
        results = [process.communicate(timeout=20) + (process.returncode,) for process in processes]
        self.assertTrue(all(returncode == 0 for _, _, returncode in results), results)
        message_ids = {json.loads(stdout)["message_id"] for stdout, _, _ in results}
        self.assertEqual(len(message_ids), 1)
        self.assertEqual(len(fleet_dialogue.load_messages(self.runs, self.feature)), 1)

    def _cli_env(self) -> dict[str, str]:
        binary_dir = self.runs.parent / "bin"
        binary_dir.mkdir(exist_ok=True)
        cmux = binary_dir / "cmux"
        cmux.write_text(
            "#!/usr/bin/env bash\n"
            "cat <<'EOF'\n"
            "workspace workspace:1 00000000-0000-0000-0000-000000000001 fleet\n"
            "surface surface:1 00000000-0000-0000-0000-000000000101\n"
            "surface surface:2 00000000-0000-0000-0000-000000000102\n"
            "surface surface:3 00000000-0000-0000-0000-000000000103\n"
            "EOF\n",
            encoding="utf-8",
        )
        cmux.chmod(0o755)
        return {**os.environ, "PATH": f"{binary_dir}:{os.environ.get('PATH', '')}"}


if __name__ == "__main__":
    unittest.main()
