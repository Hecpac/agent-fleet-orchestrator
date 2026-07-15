from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "mission-run.py"
SPEC = importlib.util.spec_from_file_location("mission_run", SCRIPT)
assert SPEC and SPEC.loader
mission_run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mission_run)
APPROVE_SPEC = importlib.util.spec_from_file_location(
    "fleet_approve_for_mission_test", ROOT / "scripts" / "fleet-approve.py"
)
assert APPROVE_SPEC and APPROVE_SPEC.loader
fleet_approve = importlib.util.module_from_spec(APPROVE_SPEC)
APPROVE_SPEC.loader.exec_module(fleet_approve)


class MissionRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs = self.tmp / "runs"
        self.target = self.tmp / "target"
        self.target.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.target, check=True)
        subprocess.run(["git", "config", "user.email", "fleet@example.test"], cwd=self.target, check=True)
        subprocess.run(["git", "config", "user.name", "Fleet Test"], cwd=self.target, check=True)
        (self.target / "README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.target, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=self.target, check=True)
        self.base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.target, text=True, capture_output=True, check=True
        ).stdout.strip()

    def test_dry_run_compiles_and_assesses_without_creating_state(self) -> None:
        result = subprocess.run(
            [
                "python3", str(SCRIPT), "--runs-dir", str(self.runs), "dry",
                "preview", "inspect the parser", "--workflow", "implementation",
                "--target-repo", str(self.target), "--json",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["effects"], [])
        self.assertEqual(value["risk"]["level"], "low")
        self.assertFalse(self.runs.exists())

    def test_high_risk_pauses_before_fleet_boot(self) -> None:
        result = subprocess.run(
            [
                "python3", str(SCRIPT), "--runs-dir", str(self.runs), "run",
                "high-risk", "deploy this to production", "--workflow", "implementation",
                "--target-repo", str(self.target), "--json",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "awaiting_assurance_confirmation")
        self.assertEqual(value["risk"], "high")
        self.assertIn("production", value["categories"])
        self.assertFalse((self.runs / "fleet-high-risk.manifest").exists())

    def test_local_worm_cannot_satisfy_regulated_profile_or_risk(self) -> None:
        for objective, profile in (
            ("inspect a local audit", "regulated"),
            ("prepare a HIPAA regulated archive", "native"),
        ):
            with self.subTest(objective=objective, profile=profile):
                result = subprocess.run(
                    [
                        "python3", str(SCRIPT), "--runs-dir", str(self.runs), "dry",
                        "local-worm-negative", objective, "--workflow", "local-worm",
                        "--target-repo", str(self.target),
                        "--execution-profile", profile, "--json",
                    ],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("external-compliance trust", result.stderr)

    def test_declared_worm_category_rejects_signed_workflow(self) -> None:
        result = subprocess.run(
            [
                "python3", str(SCRIPT), "--runs-dir", str(self.runs), "dry",
                "research-private", "analyze private customer data",
                "--workflow", "research", "--target-repo", str(self.target), "--json",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires WORM audit", result.stderr)

    def fake_runtime(self):
        calls: list[str] = []
        run_id = str(uuid.uuid4())
        result_file = self.tmp / "lead-result.md"
        result_file.write_text("STATUS: DONE\nDECISION: accepted\n", encoding="utf-8")

        def fake(command: list[str], *, timeout=None, env=None):
            del timeout
            name = Path(command[0]).name
            calls.append(name)
            if name == "git":
                if "status" in command:
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[-2:] == ["rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(command, 0, self.base_sha + "\n", "")
                return subprocess.CompletedProcess(command, 0, ".git\n", "")
            if name == "fleet-up.sh":
                assert env is not None
                mission_id = env["FLEET_MISSION_ID"]
                feature = command[1]
                self.runs.mkdir(parents=True, exist_ok=True)
                (self.runs / f"fleet-{feature}.manifest").write_text(
                    f"feature={feature}\nmission_id={mission_id}\npreset=dan\nmode=autonomous\n"
                    f"target_repo={self.target.resolve()}\nworkspace=workspace:1\n"
                    "lead.runner=interactive\nlead.provider=openai\nlead.model=gpt-test\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "booted\n", "")
            if name == "fleet-send.sh":
                prompt = command[3]
                event = {
                    "timestamp": "2026-07-14T00:00:00Z",
                    "run_id": run_id,
                    "feature": command[1],
                    "instance": "lead",
                    "status": "succeeded",
                    "task_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "result_file": str(result_file),
                    "provider": "openai",
                    "model": "gpt-test",
                    "variant": None,
                }
                (self.runs / f"fleet-{command[1]}.ledger.jsonl").write_text(
                    json.dumps(event) + "\n", encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, json.dumps({"run_id": run_id}) + "\n", "")
            if name == "fleet-wait.sh":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"run_id": run_id, "status": "succeeded", "result_file": str(result_file)}) + "\n",
                    "",
                )
            if name == "fleet-down.sh":
                return subprocess.CompletedProcess(command, 0, "closed\n", "")
            raise AssertionError(command)

        return calls, fake

    def test_autonomous_mission_completes_with_durable_result_and_archive(self) -> None:
        calls, fake = self.fake_runtime()
        with mock.patch.object(mission_run, "run_process", side_effect=fake), mock.patch.object(
            mission_run, "cmux_signal"
        ):
            value = mission_run.create_and_drive(
                self.runs,
                feature="autonomous",
                objective="implement and verify the parser",
                workflow_name="implementation",
                target_repo=self.target.resolve(),
                risk_override="auto",
                timeout_seconds=300,
                allow_dirty_baseline=False,
                teardown=False,
            )
        self.assertEqual(value["status"], "succeeded")
        self.assertIn("DECISION: accepted", value["result"])
        self.assertEqual(calls.count("fleet-send.sh"), 1)
        mission_id = value["mission_id"]
        root = self.runs / "missions" / mission_id
        self.assertTrue((root / "archive" / "archive-index.json").is_file())
        self.assertTrue(mission_run.fleet_archive.verify_archive(root / "archive")["valid"])
        state = mission_run.fleet_mission.load_state(self.runs, mission_id)
        self.assertEqual(state["lead_result"]["provider"], "openai")
        self.assertEqual(state["lead_result"]["model"], "gpt-test")

    def test_resume_adopts_exact_legacy_lead_run_without_redispatch(self) -> None:
        calls, fake = self.fake_runtime()
        original_append = mission_run.mission_state.append_event
        crashed = False

        def crash_once(*args, **kwargs):
            nonlocal crashed
            if kwargs.get("kind") == "lead_dispatched" and not crashed:
                crashed = True
                raise mission_run.MissionRunError("simulated controller death after fleet-send")
            return original_append(*args, **kwargs)

        with mock.patch.object(mission_run, "run_process", side_effect=fake), mock.patch.object(
            mission_run, "cmux_signal"
        ), mock.patch.object(mission_run.mission_state, "append_event", side_effect=crash_once):
            with self.assertRaisesRegex(mission_run.MissionRunError, "simulated controller death"):
                mission_run.create_and_drive(
                    self.runs,
                    feature="resume",
                    objective="implement and verify the parser",
                    workflow_name="implementation",
                    target_repo=self.target.resolve(),
                    risk_override="auto",
                    timeout_seconds=300,
                    allow_dirty_baseline=False,
                    teardown=False,
                )

        mission_dirs = [path for path in (self.runs / "missions").iterdir() if path.is_dir()]
        self.assertEqual(len(mission_dirs), 1)
        with mock.patch.object(mission_run, "run_process", side_effect=fake), mock.patch.object(
            mission_run, "cmux_signal"
        ):
            value = mission_run.drive_mission(self.runs, mission_dirs[0].name)
        self.assertEqual(value["status"], "succeeded")
        self.assertEqual(calls.count("fleet-send.sh"), 1)

    def test_approved_high_risk_mission_bridges_to_assured_runner_and_lead(self) -> None:
        def git_only(command: list[str], *, timeout=None, env=None):
            del timeout, env
            if Path(command[0]).name != "git":
                raise AssertionError(command)
            if "status" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[-2:] == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(command, 0, self.base_sha + "\n", "")
            return subprocess.CompletedProcess(command, 0, ".git\n", "")

        with mock.patch.object(mission_run, "run_process", side_effect=git_only):
            paused = mission_run.create_and_drive(
                self.runs,
                feature="assured",
                objective="deploy this to production",
                workflow_name="implementation",
                target_repo=self.target.resolve(),
                risk_override="auto",
                timeout_seconds=300,
                allow_dirty_baseline=False,
                teardown=False,
            )
        self.assertEqual(paused["status"], "awaiting_assurance_confirmation")
        mission_id = paused["mission_id"]
        fleet_approve.approve_mission(
            self.runs,
            mission_id,
            scope=str(self.target),
            expires_in=600,
            idempotency_key="human:assured-test",
        )
        lead_run = str(uuid.uuid4())
        result_file = self.runs / "results" / "assured" / f"{lead_run}.txt"

        def assured_runtime(command: list[str], *, timeout=None, env=None):
            del timeout
            if Path(command[0]).name != "fleet-up.sh":
                raise AssertionError(command)
            assert env is not None
            self.runs.mkdir(parents=True, exist_ok=True)
            (self.runs / "fleet-assured.manifest").write_text(
                f"feature=assured\nmission_id={mission_id}\npreset=fleet_dialogue\nmode=assured\n"
                f"target_repo={self.target.resolve()}\nworkspace=workspace:1\n"
                "lead.provider=openai\nlead.model=gpt-test\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "booted\n", "")

        class FakeAssuredRunner:
            def __init__(inner_self, runs_dir, actual_mission_id):
                self.assertEqual(runs_dir, self.runs)
                self.assertEqual(actual_mission_id, mission_id)

            def drive(inner_self, spec_path):
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
                self.assertIn("acceptance_criteria", spec)
                result_file.parent.mkdir(parents=True, exist_ok=True)
                result_file.write_text("STATUS: DONE\nDECISION: assured\n", encoding="utf-8")
                lifecycle = {
                    "timestamp": "2026-07-14T00:00:00Z",
                    "run_id": lead_run,
                    "feature": "assured",
                    "instance": "lead",
                    "status": "succeeded",
                    "task_sha256": "d" * 64,
                    "result_file": str(result_file),
                    "provider": "openai",
                    "model": "gpt-test",
                    "variant": None,
                }
                (self.runs / "fleet-assured.ledger.jsonl").write_text(
                    json.dumps(lifecycle) + "\n", encoding="utf-8"
                )
                return {
                    "status": "verified",
                    "synthesis": {
                        "run_id": lead_run,
                        "result_file": str(result_file),
                        "prompt_sha256": "d" * 64,
                    },
                }

        class FakeAuditLifecycle:
            def __init__(inner_self, runs_dir, actual_mission_id):
                self.assertEqual(runs_dir, self.runs)
                self.assertEqual(actual_mission_id, mission_id)

            def preflight(inner_self):
                return {"mode": "signed", "configured": True}

            def start(inner_self, manifest_path):
                self.assertEqual(manifest_path, self.runs / "fleet-assured.manifest")
                return {"started": True}

            def record_control_event(inner_self, **kwargs):
                self.assertRegex(kwargs["subject_sha256"], r"^[0-9a-f]{64}$")
                return {"event_id": str(uuid.uuid4())}

            def verify(inner_self):
                return {"valid": True, "worm": False}

        class FakeArchiveBuilder:
            def __init__(inner_self, runs_dir, actual_mission_id):
                self.assertEqual(runs_dir, self.runs)
                self.assertEqual(actual_mission_id, mission_id)

            def create(inner_self, manifest_path):
                self.assertEqual(manifest_path, self.runs / "fleet-assured.manifest")
                archive = self.runs / "missions" / mission_id / "archive"
                archive.mkdir(parents=True)
                index = archive / "archive-index.json"
                index.write_text("{}\n", encoding="utf-8")
                return {"valid": True}

        with mock.patch.object(mission_run, "run_process", side_effect=assured_runtime), \
             mock.patch.object(mission_run, "cmux_signal"), \
             mock.patch.object(mission_run.fleet_assured_runner, "AssuredRunner", FakeAssuredRunner), \
             mock.patch.object(mission_run.fleet_audit_client, "AuditLifecycle", FakeAuditLifecycle), \
             mock.patch.object(mission_run.fleet_archive, "ArchiveBuilder", FakeArchiveBuilder):
            value = mission_run.drive_mission(self.runs, mission_id)
        self.assertEqual(value["status"], "succeeded")
        self.assertIn("DECISION: assured", value["result"])
        current = mission_run.fleet_mission.load_state(self.runs, mission_id)
        self.assertEqual(current["lead_run_id"], lead_run)
        self.assertEqual(current["approval"]["scope"], str(self.target.resolve()))

    def test_worm_preflight_failure_happens_before_fleet_boot(self) -> None:
        compiled = mission_run.workflow_config.compile_path(ROOT / "workflows" / "regulated.yaml")
        mission_id, _ = mission_run.fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature="worm-preflight",
            objective="perform regulated production work",
            target_repo=self.target.resolve(),
            base_sha=self.base_sha,
            idempotency_key="create:worm-preflight",
        )
        mission_run.mission_state.append_event(
            self.runs,
            mission_id,
            kind="risk_assessed",
            actor="CONTROL",
            idempotency_key="test:risk",
            payload={"level": "high", "categories": ["regulated"], "reason": "test"},
        )
        mission_run.mission_state.append_event(
            self.runs,
            mission_id,
            kind="assurance_requested",
            actor="CONTROL",
            idempotency_key="test:request",
            payload={
                "risk": "high",
                "categories": ["regulated"],
                "scope": str(self.target.resolve()),
                "workflow_digest": compiled["workflow_digest"],
            },
        )
        fleet_approve.approve_mission(
            self.runs,
            mission_id,
            scope=str(self.target),
            expires_in=600,
            idempotency_key="human:worm-preflight",
        )

        class FailingAuditLifecycle:
            def __init__(inner_self, runs_dir, actual_mission_id):
                self.assertEqual(runs_dir, self.runs)
                self.assertEqual(actual_mission_id, mission_id)

            def preflight(inner_self):
                raise mission_run.fleet_audit_client.AuditClientError(
                    "WORM audit preflight failed: missing S3 Object Lock bucket"
                )

        with mock.patch.object(
            mission_run.fleet_audit_client, "AuditLifecycle", FailingAuditLifecycle
        ), mock.patch.object(
            mission_run, "run_process", side_effect=AssertionError("fleet must not boot")
        ), self.assertRaisesRegex(
            mission_run.fleet_audit_client.AuditClientError, "missing S3 Object Lock"
        ):
            mission_run.drive_mission(self.runs, mission_id)

        current = mission_run.fleet_mission.load_state(self.runs, mission_id)
        self.assertEqual(current["status"], "assurance_approved")
        self.assertFalse((self.runs / "fleet-worm-preflight.manifest").exists())


if __name__ == "__main__":
    unittest.main()
