from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "scripts" / "fleet_state.py"
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_dialogue_controller as controller  # noqa: E402
import fleet_json  # noqa: E402
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
        self.manifest.chmod(0o600)
        self.state_env = os.environ.copy()

    def run_state(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(STATE), *args, str(self.manifest)]
            if args[0] == "init"
            else ["python3", str(STATE), args[0], str(self.manifest), *args[1:]],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=self.state_env,
        )

    def mission_approval(self) -> str:
        runs = self.manifest.parent
        target = runs / "target"
        target.mkdir()
        compiled = workflow_config.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )
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
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
                "expires_in_seconds": 600,
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
                "manifest_contract_version=3\n"
                "tracking_protocol=control-v1\n"
                "execution_profile=native\n"
                f"compiled_digest={compiled['compiled_digest']}\n"
                f"router_digest={compiled['router_digest']}\n"
                f"roster_digest={'c' * 64}\n"
                f"launch_digest={'d' * 64}\n"
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
                "advance",
                "VERIFY",
                "--evidence",
                "build-frozen",
                "--approved-by",
                "hector",
            ).returncode,
            0,
        )
        self.assertEqual(self.run_state("check", "build").returncode, 3)
        self.assertEqual(self.run_state("check", "verify").returncode, 0)

    def test_standalone_build_exit_requires_operator_attestation(self) -> None:
        self.assertEqual(self.run_state("init").returncode, 0)
        self.assertEqual(
            self.run_state(
                "advance", "BUILD", "--evidence", "scope-approved"
            ).returncode,
            0,
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

    def test_mission_bound_build_exit_requires_exact_active_approval_event(
        self,
    ) -> None:
        approval_sha = self.mission_approval()
        self.assertEqual(self.run_state("init").returncode, 0)
        self.assertEqual(
            self.run_state(
                "advance", "BUILD", "--evidence", "scope-approved"
            ).returncode,
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

    def test_fdp2_requires_latest_accepted_clean_head_before_leaving_build(
        self,
    ) -> None:
        repo = Path(self.tempdir.name).resolve() / "target"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fleet@example.test"], cwd=repo, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Fleet Test"], cwd=repo, check=True
        )
        (repo / "accepted.txt").write_text("accepted\n")
        subprocess.run(["git", "add", "accepted.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "accepted"], cwd=repo, check=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        writer = Path(self.tempdir.name).resolve() / "writer"
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--no-hardlinks",
                "--no-checkout",
                str(repo),
                str(writer),
            ],
            check=True,
        )
        subprocess.run(["git", "remote", "remove", "origin"], cwd=writer, check=True)
        subprocess.run(
            ["git", "switch", "-q", "-c", "fleet/test/maker", head],
            cwd=writer,
            check=True,
        )
        cmux_dir = Path(self.tempdir.name) / "cmux-bin"
        cmux_dir.mkdir()
        cmux = cmux_dir / "cmux"
        cmux.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' "
            "'workspace:1 00000000-0000-0000-0000-000000000001' "
            "'surface:1 00000000-0000-0000-0000-000000000101' "
            "'surface:2 00000000-0000-0000-0000-000000000102' "
            "'surface:3 00000000-0000-0000-0000-000000000103' "
            "'surface:4 00000000-0000-0000-0000-000000000104' "
            "'surface:5 00000000-0000-0000-0000-000000000105'\n",
            encoding="utf-8",
        )
        cmux.chmod(0o700)
        self.state_env["PATH"] = f"{cmux_dir}:{self.state_env['PATH']}"
        self.manifest.write_text(
            "feature=test\n"
            "preset=fleet_dialogue\n"
            f"target_repo={repo}\n"
            "workspace=workspace:1\n"
            "workspace_uuid=00000000-0000-0000-0000-000000000001\n"
            "lead=surface:1\nlead.uuid=00000000-0000-0000-0000-000000000101\nlead.phase=CONTROL\n"
            "maker=surface:2\nmaker.uuid=00000000-0000-0000-0000-000000000102\n"
            "maker.role_type=codex\nmaker.phase=BUILD\nmaker.authority=write\n"
            "maker.provider=openai\nmaker.model=gpt-5.6-sol\n"
            f"maker.worktree={writer}\nmaker.branch=fleet/test/maker\nmaker.base_sha={head}\n"
            f"maker.final_sha={head}\nmaker.git_isolation=isolated-clone\n"
            "maker.publication_state=private\n"
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
        self.manifest.chmod(0o600)
        self.assertEqual(self.run_state("init").returncode, 0)
        self.assertEqual(
            self.run_state(
                "advance", "BUILD", "--evidence", "scope-approved"
            ).returncode,
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
        task_spec_file.parent.chmod(0o700)
        task_spec_file.write_bytes(task_spec)
        task_spec_file.chmod(0o600)
        (self.manifest.parent / "dialogue" / "test" / "payloads").mkdir(parents=True)
        dialogue_ledger = self.manifest.parent / "fleet-test.dialogue.jsonl"
        dialogue_ledger.touch()
        dialogue_ledger.chmod(0o600)
        snapshot = {
            "status": "accepted",
            "created_at": created,
            "deadline_at": (
                datetime.now(timezone.utc) + timedelta(hours=4)
            ).isoformat(),
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

    def test_init_writes_closed_v2_state_with_immutable_manifest_binding(self) -> None:
        initialized = self.run_state("init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        state_path = self.manifest.with_suffix(".state.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(state),
            {"schema_version", "feature", "binding", "active_phase", "history"},
        )
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(
            state["binding"],
            {
                "manifest_contract_version": 1,
                "manifest_digest": fleet_json.sha256(
                    fleet_state._phase_manifest_projection(
                        fleet_state.manifest_values(self.manifest)
                    )
                ),
                "mode": "guided",
                "preset": "",
                "phase_order": ["CONTROL", "BUILD", "VERIFY"],
                "mission": None,
            },
        )
        self.assertEqual(state["history"][0]["evidence"], "fleet-created")
        self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
        active = self.run_state("active")
        self.assertEqual(active.returncode, 0, active.stderr)
        self.assertEqual(active.stdout.strip(), "CONTROL")
        probe = self.run_state("probe-active")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "CONTROL")

        original_state = state_path.read_bytes()
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write("preset=mutated-after-init\n")
        rejected = self.run_state("show")
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("immutable manifest binding changed", rejected.stderr)
        self.assertEqual(state_path.read_bytes(), original_state)

    def test_probe_active_is_read_only_and_distinguishes_absence_from_invalidity(
        self,
    ) -> None:
        absent = self.run_state("probe-active")
        self.assertEqual(absent.returncode, 2)
        self.assertIn("cannot open rooted file", absent.stderr)

        # A manifest with no state is invalid, while an absent manifest is the
        # sole return-code-1 case used by archived teardown recovery.
        self.manifest.unlink()
        absent = self.run_state("probe-active")
        self.assertEqual(absent.returncode, 1, absent.stderr)
        self.assertFalse((self.manifest.parent / ".fleet-test.manifest.lock").exists())

    def test_teardown_only_manifest_fields_do_not_break_phase_state_binding(
        self,
    ) -> None:
        self.assertEqual(self.run_state("init").returncode, 0)
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                "workspace.handoff_state=quiesced\n"
                "workspace.quiesced=1\n"
                "build.final_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                "build.published_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                "build.publication_state=published\n"
            )
        active = self.run_state("active")
        self.assertEqual(active.returncode, 0, active.stderr)
        self.assertEqual(active.stdout.strip(), "CONTROL")

    def test_duplicate_manifest_key_is_rejected_before_lock_or_state_effects(
        self,
    ) -> None:
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write("feature=test\n")
        rejected = self.run_state("init")
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("duplicate manifest key", rejected.stderr)
        self.assertFalse(self.manifest.with_suffix(".state.json").exists())
        self.assertFalse((self.manifest.parent / ".fleet-test.manifest.lock").exists())

    def test_state_rejects_unknown_fields_and_nonmonotonic_history_without_mutation(
        self,
    ) -> None:
        self.assertEqual(self.run_state("init").returncode, 0)
        state_path = self.manifest.with_suffix(".state.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["unknown"] = True
        state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
        unknown = state_path.read_bytes()
        rejected = self.run_state("show")
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("fields do not match schema", rejected.stderr)
        self.assertEqual(state_path.read_bytes(), unknown)

        state.pop("unknown")
        state["active_phase"] = "BUILD"
        state["history"].append(
            {
                "phase": "BUILD",
                "timestamp": state["history"][0]["timestamp"],
                "evidence": "scope",
            }
        )
        state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
        nonmonotonic = state_path.read_bytes()
        rejected = self.run_state("show")
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("strictly monotonic", rejected.stderr)
        self.assertEqual(state_path.read_bytes(), nonmonotonic)

    def test_clock_rollback_still_produces_strictly_monotonic_timestamp(self) -> None:
        self.assertEqual(self.run_state("init").returncode, 0)
        state_path = self.manifest.with_suffix(".state.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["history"][0]["timestamp"] = "2099-01-01T00:00:00.000000+00:00"
        state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
        advanced = self.run_state("advance", "BUILD", "--evidence", "clock-rollback")
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        timestamps = [
            datetime.fromisoformat(entry["timestamp"]) for entry in state["history"]
        ]
        self.assertGreater(timestamps[1], timestamps[0])
        self.assertEqual(
            timestamps[1] - timestamps[0],
            timedelta(microseconds=1),
        )

    def test_concurrent_double_advance_serializes_to_one_transition(self) -> None:
        self.assertEqual(self.run_state("init").returncode, 0)
        command = [
            "python3",
            str(STATE),
            "advance",
            str(self.manifest),
            "BUILD",
            "--evidence",
            "concurrent",
        ]
        processes = [
            subprocess.Popen(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.state_env,
            )
            for _ in range(2)
        ]
        completed = [process.communicate(timeout=10) for process in processes]
        self.assertEqual(sorted(process.returncode for process in processes), [0, 2])
        self.assertTrue(
            any("invalid transition" in stderr for _, stderr in completed),
            completed,
        )
        state = json.loads(
            self.manifest.with_suffix(".state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["active_phase"], "BUILD")
        self.assertEqual(
            [entry["phase"] for entry in state["history"]],
            ["CONTROL", "BUILD"],
        )

    def test_symlinked_or_hardlinked_state_is_rejected_without_external_mutation(
        self,
    ) -> None:
        for link_kind in ("symlink", "hardlink"):
            with self.subTest(link_kind=link_kind):
                self.assertEqual(self.run_state("init").returncode, 0)
                state_path = self.manifest.with_suffix(".state.json")
                original = state_path.read_bytes()
                outside = self.manifest.parent / f"outside-{link_kind}.json"
                outside.write_bytes(original)
                outside.chmod(0o600)
                state_path.unlink()
                if link_kind == "symlink":
                    state_path.symlink_to(outside)
                else:
                    os.link(outside, state_path)
                rejected = self.run_state("advance", "BUILD", "--evidence", "unsafe")
                self.assertEqual(rejected.returncode, 2)
                self.assertEqual(outside.read_bytes(), original)
                state_path.unlink()
                outside.unlink()
                # Restore a fresh state for the next subtest without reusing bytes.
                self.assertEqual(self.run_state("init").returncode, 0)
                state_path.unlink()

    def test_symlinked_or_hardlinked_phase_lock_is_rejected_before_state_creation(
        self,
    ) -> None:
        lock = self.manifest.parent / ".fleet-test.manifest.lock"
        for link_kind in ("symlink", "hardlink"):
            with self.subTest(link_kind=link_kind):
                outside = self.manifest.parent / f"outside-{link_kind}.lock"
                outside.write_bytes(b"")
                outside.chmod(0o600)
                if link_kind == "symlink":
                    lock.symlink_to(outside)
                else:
                    os.link(outside, lock)
                rejected = self.run_state("init")
                self.assertEqual(rejected.returncode, 2)
                self.assertFalse(self.manifest.with_suffix(".state.json").exists())
                self.assertEqual(outside.read_bytes(), b"")
                lock.unlink()
                outside.unlink()

    def test_manifest_substitution_while_transition_is_locked_fails_closed(
        self,
    ) -> None:
        self.assertEqual(self.run_state("init").returncode, 0)
        with tempfile.TemporaryDirectory() as coordination:
            ready = Path(coordination) / "ready"
            release = Path(coordination) / "release"
            environment = {
                **self.state_env,
                "FLEET_TEST_STATE_PAUSE_AT": "before-state-write",
                "FLEET_TEST_STATE_READY": str(ready),
                "FLEET_TEST_STATE_RELEASE": str(release),
            }
            process = subprocess.Popen(
                [
                    "python3",
                    str(STATE),
                    "advance",
                    str(self.manifest),
                    "BUILD",
                    "--evidence",
                    "substitution",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None:
                self.assertLess(
                    time.monotonic(), deadline, "phase process did not pause"
                )
                time.sleep(0.01)
            outside = self.manifest.parent / "outside-manifest"
            original_manifest = self.manifest.read_bytes()
            outside.write_bytes(original_manifest)
            outside.chmod(0o600)
            self.manifest.unlink()
            self.manifest.symlink_to(outside)
            release.touch()
            stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 2, (stdout, stderr))
        self.assertIn("unsafe or invalid fleet phase state", stderr)
        self.assertEqual(outside.read_bytes(), original_manifest)
        state = json.loads(
            self.manifest.with_suffix(".state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["active_phase"], "CONTROL")

    def test_runs_root_substitution_is_detected_before_transition_publication(
        self,
    ) -> None:
        self.assertEqual(self.run_state("init").returncode, 0)
        root = Path(self.tempdir.name)
        moved = root.with_name(f"{root.name}-moved-{uuid.uuid4().hex}")
        with tempfile.TemporaryDirectory() as coordination:
            ready = Path(coordination) / "ready"
            release = Path(coordination) / "release"
            process = subprocess.Popen(
                [
                    "python3",
                    str(STATE),
                    "advance",
                    str(self.manifest),
                    "BUILD",
                    "--evidence",
                    "root-substitution",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **self.state_env,
                    "FLEET_TEST_STATE_PAUSE_AT": "before-state-write",
                    "FLEET_TEST_STATE_READY": str(ready),
                    "FLEET_TEST_STATE_RELEASE": str(release),
                },
            )
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None:
                self.assertLess(
                    time.monotonic(), deadline, "phase process did not pause"
                )
                time.sleep(0.01)
            root.rename(moved)
            root.mkdir(mode=0o700)
            release.touch()
            stdout, stderr = process.communicate(timeout=10)
        try:
            self.assertEqual(process.returncode, 2, (stdout, stderr))
            self.assertIn("trusted root binding changed", stderr)
            state = json.loads(
                (moved / "fleet-test.state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["active_phase"], "CONTROL")
            self.assertFalse((root / "fleet-test.state.json").exists())
        finally:
            if root.exists():
                shutil.rmtree(root)
            moved.rename(root)


if __name__ == "__main__":
    unittest.main()
