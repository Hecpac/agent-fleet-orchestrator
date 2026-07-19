from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import stat
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
        manifest = self.manifest_values()
        self.cmux_bin = self.fdp2.tmp / "fake-cmux-bin"
        self.cmux_bin.mkdir()
        cmux = self.cmux_bin / "cmux"
        identities = [
            f"{manifest['workspace']} {manifest['workspace_uuid']}",
            *[
                f"{manifest[instance]} {manifest[f'{instance}.uuid']}"
                for instance in ("lead", "maker", "checker", "challenge", "verify")
            ],
        ]
        cmux.write_text(
            "#!/bin/sh\nprintf '%s\\n' "
            + " ".join(f"'{identity}'" for identity in identities)
            + "\n",
            encoding="utf-8",
        )
        cmux.chmod(0o700)
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
            env={**os.environ, "PATH": f"{self.cmux_bin}:{os.environ['PATH']}"},
            check=False,
        )

    def manifest_values(self) -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in self.manifest.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )

    @staticmethod
    def tree_snapshot(
        root: Path,
    ) -> dict[Path, tuple[str, int, int, bytes | str]]:
        snapshot: dict[Path, tuple[str, int, int, bytes | str]] = {}
        for path in root.rglob("*"):
            info = path.lstat()
            relative = path.relative_to(root)
            if stat.S_ISLNK(info.st_mode):
                kind = "symlink"
                payload: bytes | str = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
                payload = path.read_bytes()
            elif stat.S_ISDIR(info.st_mode):
                kind = "directory"
                payload = b""
            else:
                kind = "special"
                payload = b""
            snapshot[relative] = (
                kind,
                stat.S_IMODE(info.st_mode),
                info.st_nlink,
                payload,
            )
        return snapshot

    def assert_target_remains_private(self) -> None:
        branch = self.manifest_values()["maker.branch"]
        self.assertEqual(
            assurance._git(
                self.target,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
                check=False,
            ).returncode,
            1,
        )
        self.assertNotEqual(
            assurance._git(
                self.target,
                "cat-file",
                "-e",
                f"{self.accepted_head}^{{commit}}",
                check=False,
            ).returncode,
            0,
        )

    def assert_isolated_read_only_snapshot(self, path: Path) -> None:
        self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o500)
        self.assertTrue((path / ".git").is_dir())
        self.assertFalse((path / ".git").is_symlink())
        writer_git = self.fdp2.writer / ".git"
        if writer_git.exists():
            self.assertNotEqual(
                ((path / ".git").stat().st_dev, (path / ".git").stat().st_ino),
                (writer_git.stat().st_dev, writer_git.stat().st_ino),
            )
        for argument in ("--git-common-dir", "--git-dir"):
            self.assertEqual(
                Path(
                    assurance._git(
                        path,
                        "rev-parse",
                        "--path-format=absolute",
                        argument,
                    ).stdout.strip()
                ),
                path / ".git",
            )
        self.assertEqual(assurance._git(path, "remote").stdout.strip(), "")
        self.assertFalse((path / ".git" / "objects" / "info" / "alternates").exists())
        self.assertEqual(
            assurance._git(path, "rev-parse", "--verify", "HEAD").stdout.strip(),
            self.accepted_head,
        )
        for directory, directories, files in os.walk(path, followlinks=False):
            for name in [*directories, *files]:
                entry = Path(directory) / name
                info = entry.lstat()
                self.assertFalse(stat.S_ISLNK(info.st_mode), entry)
                self.assertEqual(info.st_mode & 0o222, 0, entry)
                if stat.S_ISREG(info.st_mode):
                    self.assertEqual(info.st_nlink, 1, entry)

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
        (self.runs / "results").chmod(0o755)
        result_file.parent.chmod(0o700)
        result_file.write_bytes(payload)
        result_file.chmod(0o600)
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

    def test_active_fdp3_rejects_unexpected_publication_without_mutation(self) -> None:
        started = self.start_assurance()
        run_id = "glm-unexpected-publication"
        self.seed_run(
            started,
            run_id,
            {
                "schema_version": 1,
                "summary": "challenge ready for controlled publication",
                "findings": [],
            },
        )
        waiting = assurance.step(
            self.runs,
            feature=self.feature,
            idempotency_key="fdp3-unexpected-run",
            run_id=run_id,
            now=self.now + timedelta(minutes=8),
        )
        expected = waiting["snapshot"]["expected"]
        assurance_path = assurance.ledger_path(self.runs, self.feature)
        dialogue_path = fleet_dialogue.ledger_path(self.runs, self.feature)
        assurance_before = assurance_path.read_bytes()
        dialogue_before = dialogue_path.read_bytes()
        payload_store = fleet_dialogue.store_path(self.runs, self.feature)
        payloads_before = {
            path.name: path.read_bytes() for path in payload_store.iterdir()
        }

        with self.assertRaisesRegex(
            fleet_dialogue.DialogueConflict,
            "FDP-3 controller .* stage challenge .*kind",
        ):
            fleet_dialogue.publish(
                self.runs,
                feature=self.feature,
                kind="verification",
                recipient=expected["recipient"],
                source_instance=expected["source_instance"],
                source_run_id=expected["source_run_id"],
                reply_to=expected["reply_to"],
                idempotency_key="fdp3-unexpected-publication",
            )

        self.assertEqual(assurance_path.read_bytes(), assurance_before)
        self.assertEqual(dialogue_path.read_bytes(), dialogue_before)
        self.assertEqual(
            {path.name: path.read_bytes() for path in payload_store.iterdir()},
            payloads_before,
        )
        self.assertEqual(assurance.load_events(self.runs, self.feature)[-1], waiting)

    def test_assurance_control_ledger_is_strict_jsonl_and_read_only(self) -> None:
        self.start_assurance()
        path = assurance.ledger_path(self.runs, self.feature)
        valid = path.read_bytes()
        cases = {
            "duplicate-key": b'{"sequence":1,' + valid[1:],
            "nan": b'{"ambiguous":NaN,' + valid[1:],
            "infinity": b'{"ambiguous":Infinity,' + valid[1:],
            "overflow": b'{"ambiguous":1e999,' + valid[1:],
            "bom": b"\xef\xbb\xbf" + valid,
            "invalid-utf8": b'{"ambiguous":"\xff",' + valid[1:],
            "surrogate": b'{"ambiguous":"\\ud800",' + valid[1:],
            "trailing": valid.rstrip(b"\n") + b" trailing\n",
            "partial": valid.rstrip(b"\n"),
            "crlf": valid.rstrip(b"\n") + b"\r\n",
            "blank-row": valid + b"\n",
            "non-object": b"[]\n",
        }
        for name, corrupt in cases.items():
            with self.subTest(name=name):
                path.write_bytes(corrupt)
                before = self.tree_snapshot(self.runs)
                errors = []
                for _ in range(2):
                    with self.assertRaises(assurance.AssuranceError) as raised:
                        assurance.load_events(self.runs, self.feature)
                    errors.append(str(raised.exception))
                    self.assertEqual(self.tree_snapshot(self.runs), before)
                self.assertEqual(errors[0], errors[1])
                path.write_bytes(valid)

        self.assertEqual(assurance.load_events(self.runs, self.feature)[0]["sequence"], 1)

    def test_invalid_lifecycle_json_fails_before_assurance_artifacts(self) -> None:
        lifecycle = self.runs / f"fleet-{self.feature}.ledger.jsonl"
        valid = lifecycle.read_bytes()
        lifecycle.write_bytes(b'{"run_id":"duplicate",' + valid[1:])
        before = self.tree_snapshot(self.runs)

        with self.assertRaisesRegex(
            assurance.AssuranceError,
            "FDP-2 lifecycle ledger is invalid",
        ):
            self.start_assurance()

        self.assertEqual(self.tree_snapshot(self.runs), before)
        self.assertFalse(assurance.ledger_path(self.runs, self.feature).exists())
        self.assertFalse((self.runs / "assurance").exists())

    def test_rooted_durable_json_reader_rejects_ambiguous_or_unsafe_inputs(
        self,
    ) -> None:
        valid = b'{"schema_version":1}\n'
        cases = {
            "duplicate-key": b'{"schema_version":1,"schema_version":1}\n',
            "nan": b'{"bad":NaN}\n',
            "infinity": b'{"bad":Infinity}\n',
            "overflow": b'{"bad":1e999}\n',
            "bom": b'\xef\xbb\xbf{"schema_version":1}\n',
            "invalid-utf8": b'{"bad":"\xff"}\n',
            "surrogate": b'{"bad":"\\ud800"}\n',
            "trailing": b'{"schema_version":1} true\n',
            "non-object": b"[]\n",
            "symlink": valid,
            "hardlink": valid,
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                scenario = self.fdp2.tmp / f"strict-rooted-json-{name}"
                root = scenario / "store"
                context = root / "context"
                context.mkdir(parents=True, mode=0o700)
                context.chmod(0o700)
                path = context / "value.json"
                path.write_bytes(payload)
                path.chmod(0o600)
                if name == "symlink":
                    outside = scenario / "outside.json"
                    path.rename(outside)
                    path.symlink_to(outside)
                elif name == "hardlink":
                    outside = scenario / "outside.json"
                    os.link(path, outside)
                before = self.tree_snapshot(scenario)
                errors = []
                for _ in range(2):
                    with self.assertRaises(assurance.AssuranceError) as raised:
                        assurance._load_rooted_json(
                            root,
                            Path("context") / "value.json",
                            directory_modes=(0o700,),
                            max_bytes=1024,
                            error_message="strict rooted JSON is invalid",
                        )
                    errors.append(str(raised.exception))
                    self.assertEqual(self.tree_snapshot(scenario), before)
                self.assertEqual(errors, ["strict rooted JSON is invalid"] * 2)

    def test_phase_reader_rejects_path_corruption_and_binding_drift_read_only(
        self,
    ) -> None:
        for scenario in ("symlink", "hardlink", "corrupt", "manifest-drift"):
            with self.subTest(scenario=scenario):
                scenario_root = self.fdp2.tmp / f"phase-reader-{scenario}"
                runs = scenario_root / "runs"
                runs.mkdir(parents=True)
                feature = f"fdp3-{scenario}"
                manifest_path = runs / f"fleet-{feature}.manifest"
                manifest_path.write_text(
                    "".join(
                        (
                            "schema_version=3\n",
                            f"feature={feature}\n",
                            "preset=fleet_dialogue\n",
                            f"target_repo={self.target}\n",
                            "workspace=workspace:1\n",
                            "workspace_uuid=00000000-0000-0000-0000-000000000001\n",
                            "lead=surface:1\n",
                            "lead.uuid=00000000-0000-0000-0000-000000000101\n",
                            "lead.phase=CONTROL\n",
                            "maker=surface:2\n",
                            "maker.phase=BUILD\n",
                            "challenge=surface:3\n",
                            "challenge.phase=CHALLENGE\n",
                            "verify=surface:4\n",
                            "verify.phase=VERIFY\n",
                        )
                    ),
                    encoding="utf-8",
                )
                manifest_path.chmod(0o600)
                manifest = assurance.fleet_state.manifest_values(manifest_path)
                state_path = manifest_path.with_suffix(".state.json")
                state_path.write_text(
                    json.dumps(
                        assurance.fleet_state._new_state(manifest),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )
                state_path.chmod(0o600)
                self.assertEqual(
                    assurance._state(runs, feature, manifest)["active_phase"],
                    "CONTROL",
                )

                outside = scenario_root / "outside-state.json"
                if scenario == "symlink":
                    state_path.rename(outside)
                    state_path.symlink_to(outside)
                elif scenario == "hardlink":
                    os.link(state_path, outside)
                elif scenario == "corrupt":
                    state_path.write_bytes(
                        b'{"schema_version":2,"schema_version":2}\n'
                    )
                else:
                    manifest_path.write_bytes(
                        manifest_path.read_bytes().replace(
                            b"preset=fleet_dialogue",
                            b"preset=changed",
                            1,
                        )
                    )
                before = self.tree_snapshot(scenario_root)

                with self.assertRaisesRegex(
                    assurance.AssuranceError,
                    "cannot read fleet state",
                ):
                    assurance._state(runs, feature, manifest)

                self.assertEqual(self.tree_snapshot(scenario_root), before)

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
        manifest = self.manifest_values()
        manifest["mission_id"] = "00000000-0000-4000-8000-000000000999"

        with mock.patch.object(
            assurance.fleet_state,
            "load_live",
            return_value=(manifest, value),
        ), mock.patch.object(
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
            "load_live",
            return_value=(manifest, value),
        ), mock.patch.object(
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

    def test_private_start_clones_exact_head_without_publishing_target(self) -> None:
        self.assert_target_remains_private()
        started = self.start_assurance()
        paths = [
            Path(started["snapshot"]["challenge_snapshot_path"]),
            Path(started["snapshot"]["verification_snapshot_path"]),
        ]
        for path in paths:
            self.assert_isolated_read_only_snapshot(path)
        records = json.loads(
            Path(started["snapshot"]["snapshots_file"]).read_text(encoding="utf-8")
        )["snapshots"]
        self.assertEqual(
            {record["source_repo"] for record in records},
            {str(self.fdp2.writer)},
        )
        self.assertEqual(
            {record["publication_state"] for record in records},
            {"private"},
        )
        self.assertEqual(
            {record["git_isolation"] for record in records},
            {"isolated-clone"},
        )
        self.assert_target_remains_private()

    def test_private_start_rejects_history_and_path_drift_before_effects(self) -> None:
        grafts = self.fdp2.writer / ".git" / "info" / "grafts"
        grafts.write_text(f"{self.accepted_head}\n", encoding="utf-8")
        with self.assertRaisesRegex(
            assurance.AssuranceError,
            "history grafts are forbidden",
        ):
            self.start_assurance()
        self.assertFalse((self.runs / "worktrees").exists())
        grafts.unlink()

        moved = self.fdp2.tmp / "writer-moved"
        self.fdp2.writer.rename(moved)
        self.fdp2.writer.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(
            assurance.AssuranceError,
            "exact physical directory|binding drifted",
        ):
            self.start_assurance()
        self.assertFalse((self.runs / "worktrees").exists())

    def test_hostile_git_environment_is_ignored(self) -> None:
        hostile_environment = {
            "GIT_DIR": str(self.target / ".git"),
            "GIT_WORK_TREE": str(self.target),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(self.fdp2.tmp / "missing-hooks"),
            "GIT_OPTIONAL_LOCKS": "1",
        }
        with mock.patch.dict(os.environ, hostile_environment, clear=False):
            started = self.start_assurance()
        for field in (
            "challenge_snapshot_path",
            "verification_snapshot_path",
        ):
            self.assert_isolated_read_only_snapshot(Path(started["snapshot"][field]))
        self.assert_target_remains_private()

    def test_global_git_hook_never_executes(self) -> None:
        hostile_hooks = self.fdp2.tmp / "hostile-hooks"
        hostile_hooks.mkdir()
        sentinel = self.fdp2.tmp / "hook-ran"
        hook = hostile_hooks / "post-checkout"
        hook.write_text(
            f"#!/bin/sh\nprintf compromised > {sentinel}\n",
            encoding="utf-8",
        )
        hook.chmod(0o700)
        hostile_config = self.fdp2.tmp / "hostile-gitconfig"
        subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(hostile_config),
                "core.hooksPath",
                str(hostile_hooks),
            ],
            check=True,
        )
        hostile_environment = {
            "GIT_CONFIG_GLOBAL": str(hostile_config),
            "GIT_CONFIG_SYSTEM": str(hostile_config),
        }
        with mock.patch.dict(os.environ, hostile_environment, clear=False):
            started = self.start_assurance()
        self.assertFalse(sentinel.exists())
        for field in (
            "challenge_snapshot_path",
            "verification_snapshot_path",
        ):
            self.assert_isolated_read_only_snapshot(Path(started["snapshot"][field]))
        self.assert_target_remains_private()

    def test_published_start_uses_exact_target_ref_as_snapshot_source(self) -> None:
        branch = self.manifest_values()["maker.branch"]
        assurance._git(
            self.target,
            "fetch",
            "--no-tags",
            str(self.fdp2.writer),
            self.accepted_head,
        )
        assurance._git(
            self.target,
            "update-ref",
            f"refs/heads/{branch}",
            self.accepted_head,
            "0" * 40,
        )
        shutil.rmtree(self.fdp2.writer)
        manifest = self.manifest_values()
        manifest["maker.final_sha"] = self.accepted_head
        manifest["maker.publication_state"] = "published"
        manifest["maker.published_sha"] = self.accepted_head
        manifest["workspace.quiesced"] = "1"
        self.manifest.write_text(
            "\n".join(f"{key}={value}" for key, value in manifest.items()) + "\n",
            encoding="utf-8",
        )
        started = self.start_assurance()
        for field in (
            "challenge_snapshot_path",
            "verification_snapshot_path",
        ):
            self.assert_isolated_read_only_snapshot(Path(started["snapshot"][field]))
        records = json.loads(
            Path(started["snapshot"]["snapshots_file"]).read_text(encoding="utf-8")
        )["snapshots"]
        self.assertEqual(
            {(record["source_repo"], record["publication_state"]) for record in records},
            {(str(self.target), "published")},
        )

    def test_snapshot_validation_rejects_ignored_residue(self) -> None:
        repo = self.fdp2.tmp / "ignored-snapshot"
        repo.mkdir()
        empty_templates = self.fdp2.tmp / "empty-git-templates"
        empty_templates.mkdir()
        subprocess.run(
            [
                "git",
                "-c",
                f"init.templateDir={empty_templates}",
                "init",
                "-q",
                str(repo),
            ],
            check=True,
        )
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
        assurance._make_snapshot_read_only(repo)
        with self.assertRaisesRegex(assurance.AssuranceContractError, "dirty"):
            assurance._validate_snapshot_worktree(repo, head)

    def test_snapshot_validation_rejects_hidden_index_drift(self) -> None:
        repo = self.fdp2.tmp / "hidden-index-snapshot"
        repo.mkdir()
        empty_templates = self.fdp2.tmp / "hidden-index-templates"
        empty_templates.mkdir()
        subprocess.run(
            [
                "git",
                "-c",
                f"init.templateDir={empty_templates}",
                "init",
                "-q",
                str(repo),
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            check=True,
        )
        tracked = repo / "proposal.txt"
        tracked.write_text("accepted\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "proposal.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        subprocess.run(["git", "-C", str(repo), "checkout", "--detach", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "update-index", "--skip-worktree", "proposal.txt"],
            check=True,
        )
        tracked.write_text("concealed drift\n", encoding="utf-8")
        assurance._make_snapshot_read_only(repo)
        self.assertEqual(
            assurance._git(repo, "status", "--porcelain", "--ignored").stdout,
            "",
        )
        with self.assertRaisesRegex(
            assurance.AssuranceContractError,
            "hidden Git index flags",
        ):
            assurance._validate_snapshot_worktree(repo, head)

    def test_offline_context_verifies_historical_snapshot_schema_v1(self) -> None:
        started = self.start_assurance()
        snapshot = started["snapshot"]
        snapshots_path = Path(snapshot["snapshots_file"])
        snapshots = json.loads(snapshots_path.read_text(encoding="utf-8"))
        snapshots["schema_version"] = 1
        for record in snapshots["snapshots"]:
            for field in (
                "source_repo",
                "publication_state",
                "git_isolation",
                "root_device",
                "root_inode",
            ):
                record.pop(field)
        snapshots_payload = (
            json.dumps(snapshots, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        )
        snapshots_path.write_bytes(snapshots_payload)

        context_path = Path(snapshot["context_file"])
        context = json.loads(context_path.read_text(encoding="utf-8"))
        snapshot_record = next(
            record for record in context["files"] if record["path"] == "snapshots.json"
        )
        snapshot_record["sha256"] = assurance.digest(snapshots_payload)
        snapshot_record["bytes"] = len(snapshots_payload)
        context_path.write_bytes(
            json.dumps(context, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        )

        verified = assurance._verify_context_tree(
            self.runs / "assurance" / self.feature,
            started["assurance_id"],
        )
        self.assertEqual(verified["accepted_head_sha"], self.accepted_head)

    def test_dirty_snapshot_cleanup_fails_and_retains_both(self) -> None:
        self.start_assurance()
        terminal = assurance.abandon(
            self.runs,
            feature=self.feature,
            idempotency_key="abandon-fdp3",
            reason="test cleanup",
            now=self.now + timedelta(minutes=7),
        )
        challenge_path = Path(terminal["snapshot"]["challenge_snapshot_path"])
        verify_path = Path(terminal["snapshot"]["verification_snapshot_path"])
        proposal = challenge_path / "proposal.txt"
        original = proposal.read_bytes()
        proposal.chmod(0o600)
        proposal.write_text("retain\n", encoding="utf-8")
        proposal.chmod(0o400)
        with self.assertRaisesRegex(assurance.AssuranceContractError, "dirty"):
            assurance.cleanup_snapshots(self.runs, feature=self.feature)
        self.assertTrue(challenge_path.exists())
        self.assertTrue(verify_path.exists())
        proposal.chmod(0o600)
        proposal.write_bytes(original)
        proposal.chmod(0o400)
        result = assurance.cleanup_snapshots(self.runs, feature=self.feature)
        self.assertEqual(
            result["removed_snapshots"],
            [str(challenge_path), str(verify_path)],
        )

    def test_hardlink_snapshot_cleanup_fails_closed_then_retries(self) -> None:
        self.start_assurance()
        terminal = assurance.abandon(
            self.runs,
            feature=self.feature,
            idempotency_key="abandon-hardlink-fdp3",
            reason="test hardlink cleanup",
            now=self.now + timedelta(minutes=7),
        )
        challenge_path = Path(terminal["snapshot"]["challenge_snapshot_path"])
        verify_path = Path(terminal["snapshot"]["verification_snapshot_path"])
        outside = self.fdp2.tmp / "snapshot-hardlink"
        os.link(challenge_path / "proposal.txt", outside)
        with self.assertRaisesRegex(assurance.AssuranceContractError, "hardlinked"):
            assurance.cleanup_snapshots(self.runs, feature=self.feature)
        self.assertTrue(challenge_path.exists())
        self.assertTrue(verify_path.exists())
        outside.unlink()
        result = assurance.cleanup_snapshots(self.runs, feature=self.feature)
        self.assertEqual(
            result["removed_snapshots"],
            [str(challenge_path), str(verify_path)],
        )

    def test_symlink_snapshot_cleanup_fails_closed_then_retries(self) -> None:
        self.start_assurance()
        terminal = assurance.abandon(
            self.runs,
            feature=self.feature,
            idempotency_key="abandon-symlink-fdp3",
            reason="test symlink cleanup",
            now=self.now + timedelta(minutes=7),
        )
        challenge_path = Path(terminal["snapshot"]["challenge_snapshot_path"])
        verify_path = Path(terminal["snapshot"]["verification_snapshot_path"])
        injected = challenge_path / "unsafe-link"
        challenge_path.chmod(0o700)
        injected.symlink_to(self.target)
        challenge_path.chmod(0o500)
        with self.assertRaisesRegex(assurance.AssuranceContractError, "symlink"):
            assurance.cleanup_snapshots(self.runs, feature=self.feature)
        self.assertTrue(challenge_path.exists())
        self.assertTrue(verify_path.exists())
        challenge_path.chmod(0o700)
        injected.unlink()
        challenge_path.chmod(0o500)
        result = assurance.cleanup_snapshots(self.runs, feature=self.feature)
        self.assertEqual(
            result["removed_snapshots"],
            [str(challenge_path), str(verify_path)],
        )

    def test_snapshot_replacement_is_never_deleted_as_the_recorded_clone(self) -> None:
        self.start_assurance()
        terminal = assurance.abandon(
            self.runs,
            feature=self.feature,
            idempotency_key="abandon-replaced-fdp3",
            reason="test replacement cleanup",
            now=self.now + timedelta(minutes=7),
        )
        challenge_path = Path(terminal["snapshot"]["challenge_snapshot_path"])
        verify_path = Path(terminal["snapshot"]["verification_snapshot_path"])
        original = challenge_path.with_name(f"{challenge_path.name}-original")
        original_identity = (
            challenge_path.lstat().st_dev,
            challenge_path.lstat().st_ino,
        )
        challenge_path.rename(original)
        assurance._clone_snapshot(original, challenge_path, self.accepted_head)
        replacement_identity = (
            challenge_path.lstat().st_dev,
            challenge_path.lstat().st_ino,
        )
        self.assertNotEqual(replacement_identity, original_identity)

        with self.assertRaisesRegex(
            assurance.AssuranceContractError,
            "root identity changed",
        ):
            assurance.cleanup_snapshots(self.runs, feature=self.feature)
        self.assertTrue(challenge_path.exists())
        self.assertTrue(original.exists())
        self.assertTrue(verify_path.exists())

        for directory, directories, files in os.walk(
            challenge_path,
            topdown=False,
            followlinks=False,
        ):
            for name in files:
                (Path(directory) / name).chmod(0o600)
            for name in directories:
                (Path(directory) / name).chmod(0o700)
        challenge_path.chmod(0o700)
        shutil.rmtree(challenge_path)
        original.rename(challenge_path)
        result = assurance.cleanup_snapshots(self.runs, feature=self.feature)
        self.assertEqual(
            result["removed_snapshots"],
            [str(challenge_path), str(verify_path)],
        )

    def test_cleanup_resumes_from_durable_stage_intent(self) -> None:
        self.start_assurance()
        terminal = assurance.abandon(
            self.runs,
            feature=self.feature,
            idempotency_key="abandon-staged-fdp3",
            reason="test staged cleanup retry",
            now=self.now + timedelta(minutes=7),
        )
        challenge_path = Path(terminal["snapshot"]["challenge_snapshot_path"])
        verify_path = Path(terminal["snapshot"]["verification_snapshot_path"])
        args = assurance._snapshot_guard_args(
            self.runs,
            feature=self.feature,
            assurance_id=terminal["assurance_id"],
            stage="challenge",
            head_sha=self.accepted_head,
        )
        staged = assurance.fleet_clone_guard.stage_clone(args)
        self.assertFalse(challenge_path.exists())
        self.assertTrue(staged.exists())
        staged.chmod(0o700)
        result = assurance.cleanup_snapshots(self.runs, feature=self.feature)
        self.assertEqual(
            result["removed_snapshots"],
            [str(challenge_path), str(verify_path)],
        )
        self.assertFalse(staged.exists())

    def test_cleanup_resumes_after_interrupted_tombstone_removal(self) -> None:
        self.start_assurance()
        terminal = assurance.abandon(
            self.runs,
            feature=self.feature,
            idempotency_key="abandon-interrupted-fdp3",
            reason="test interrupted cleanup retry",
            now=self.now + timedelta(minutes=7),
        )
        challenge_path = Path(terminal["snapshot"]["challenge_snapshot_path"])
        verify_path = Path(terminal["snapshot"]["verification_snapshot_path"])
        args = assurance._snapshot_guard_args(
            self.runs,
            feature=self.feature,
            assurance_id=terminal["assurance_id"],
            stage="challenge",
            head_sha=self.accepted_head,
        )
        staged = assurance.fleet_clone_guard.stage_clone(args)
        args.mode = "ensure"
        assurance.fleet_clone_guard.tombstone(args)
        crashed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "fleet_clone_guard.py"),
                "tombstone",
                "remove",
                "--runs-dir",
                str(args.runs_dir),
                "--worktrees-root",
                str(args.worktrees_root),
                "--feature",
                args.feature,
                "--instance",
                args.instance,
                "--workspace-uuid",
                args.workspace_uuid,
                "--kind",
                args.kind,
                "--expected-sha",
                args.expected_sha,
            ],
            env={
                **os.environ,
                "FLEET_TEST_PUBLICATION_CRASH_AT": "after_retirement_delete_entry",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(crashed.returncode, -signal.SIGKILL, crashed.stderr.decode())
        self.assertTrue(staged.exists())
        result = assurance.cleanup_snapshots(self.runs, feature=self.feature)
        self.assertEqual(
            result["removed_snapshots"],
            [str(challenge_path), str(verify_path)],
        )
        self.assertFalse(staged.exists())

    def test_clean_terminal_snapshots_are_removed_without_touching_accepted_head(self) -> None:
        self.start_assurance()
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
        self.assert_target_remains_private()


if __name__ == "__main__":
    unittest.main()
