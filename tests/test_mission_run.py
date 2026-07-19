from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from tests.mission_control_test_support import legacy_v1_compiled, write_compiled


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
        self.tmp = Path(self.tempdir.name).resolve()
        self.runs = self.tmp / "runs"
        self.target = self.tmp / "target"
        self.target.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.target, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fleet@example.test"],
            cwd=self.target,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Fleet Test"], cwd=self.target, check=True
        )
        (self.target / "README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.target, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "baseline"], cwd=self.target, check=True
        )
        self.base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.target,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        control_root = self.runs / "fake-control"

        class FakeControlLifecycle:
            def __init__(inner_self, runs_dir, mission_id, preset=None):
                inner_self.lifecycle_path = control_root / mission_id / "lifecycle.json"
                inner_self.socket_root = control_root / mission_id
                inner_self.preset = preset

            def start(inner_self):
                return {"started": True}

            def stop(inner_self):
                return {"stopped": True}

            def stop_if_present(inner_self):
                return {"stopped": True}

        control_patch = mock.patch.object(
            mission_run.fleet_control_service, "ControlLifecycle", FakeControlLifecycle
        )
        control_patch.start()
        self.addCleanup(control_patch.stop)

    def test_assurance_handoff_outer_timeout_encloses_child_deadlines(self) -> None:
        self.assertEqual(mission_run.assurance_handoff_command_timeout({}), 600)
        self.assertEqual(
            mission_run.assurance_handoff_command_timeout(
                {
                    "FLEET_HANDOFF_READY_TIMEOUT_SECONDS": "7",
                    "FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS": "11",
                }
            ),
            258,
        )
        self.assertEqual(
            mission_run.assurance_handoff_command_timeout(
                {
                    "FLEET_BOOT_LOCK_READY_TIMEOUT_SECONDS": "600",
                    "FLEET_HANDOFF_READY_TIMEOUT_SECONDS": "600",
                    "FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS": "3600",
                }
            ),
            4980,
        )

    def test_assurance_handoff_outer_timeout_rejects_invalid_overrides(self) -> None:
        for environment in (
            {"FLEET_BOOT_LOCK_READY_TIMEOUT_SECONDS": "0"},
            {"FLEET_BOOT_LOCK_READY_TIMEOUT_SECONDS": "601"},
            {"FLEET_HANDOFF_READY_TIMEOUT_SECONDS": "0"},
            {"FLEET_HANDOFF_READY_TIMEOUT_SECONDS": "601"},
            {"FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS": "not-a-number"},
            {"FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS": "3601"},
        ):
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(
                    mission_run.MissionRunError,
                    "invalid FLEET_(?:BOOT_LOCK|HANDOFF)",
                ):
                    mission_run.assurance_handoff_command_timeout(environment)

    def test_run_process_timeout_reaps_the_exact_descendant_group(self) -> None:
        child_pid = self.tmp / "timeout-child.pid"
        child_ready = self.tmp / "timeout-child.ready"
        program = self.tmp / "timeout-parent.py"
        child_program = "; ".join(
            (
                "import os, pathlib, signal, time",
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                "os.close(1)",
                "os.close(2)",
                f"pathlib.Path({str(child_ready)!r}).write_text('ready', encoding='utf-8')",
                "time.sleep(60)",
            )
        )
        program.write_text(
            "\n".join(
                (
                    "import pathlib, subprocess, sys, time",
                    f"child = subprocess.Popen([sys.executable, '-c', {child_program!r}])",
                    f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid), encoding='utf-8')",
                    f"ready = pathlib.Path({str(child_ready)!r})",
                    "while not ready.exists(): time.sleep(0.01)",
                    "time.sleep(60)",
                )
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(mission_run.MissionRunError, "timed out"):
            mission_run.run_process([sys.executable, str(program)], timeout=1)

        pid = int(child_pid.read_text(encoding="utf-8"))
        alive = True
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        self.assertFalse(alive, "timed-out descendant process was not reaped")

    def test_historical_compiled_workflow_is_rejected_before_runtime_effects(
        self,
    ) -> None:
        compiled = mission_run.workflow_config.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )
        mission_id, _ = mission_run.fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature="historical-runtime",
            objective="must not execute a historical plan",
            target_repo=self.target.resolve(),
            base_sha=self.base_sha,
            idempotency_key="create:historical-runtime",
        )
        root = self.runs / "missions" / mission_id
        write_compiled(root / "compiled-workflow.json", legacy_v1_compiled(compiled))
        with (
            mock.patch.object(mission_run, "require_success") as shell_effect,
            mock.patch.object(mission_run, "run_process") as dispatch_effect,
            self.assertRaisesRegex(
                mission_run.MissionRunError, "historical read-only.*require v2"
            ),
        ):
            mission_run.drive_mission(self.runs, mission_id)
        shell_effect.assert_not_called()
        dispatch_effect.assert_not_called()

    def test_runtime_options_strict_json_and_path_binding_block_all_effects(
        self,
    ) -> None:
        compiled = mission_run.workflow_config.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )
        mission_id, _ = mission_run.fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature="strict-runtime-options",
            objective="reject ambiguous durable runtime configuration",
            target_repo=self.target.resolve(),
            base_sha=self.base_sha,
            idempotency_key="create:strict-runtime-options",
            runtime_options={"timeout_seconds": 300},
        )
        options = self.runs / "missions" / mission_id / "runtime-options.json"
        original = options.read_bytes()
        mutations = {
            "duplicate": b'{"timeout_seconds":300,"timeout_seconds":1}\n',
            "bom": b"\xef\xbb\xbf" + original,
            "overflow": b'{"timeout_seconds":1e999}\n',
            "surrogate": b'{"label":"\\ud800"}\n',
            "invalid-utf8": b'{"label":"\xff"}\n',
            "trailing-space": original[:-1] + b" \n",
        }
        for label, poisoned in mutations.items():
            with self.subTest(label=label):
                options.write_bytes(poisoned)
                options.chmod(0o600)
                with (
                    mock.patch.object(mission_run, "run_process") as effects,
                    self.assertRaisesRegex(
                        mission_run.MissionRunError,
                        "durable JSON|not canonical",
                    ),
                ):
                    mission_run.drive_mission(self.runs, mission_id)
                effects.assert_not_called()
                options.write_bytes(original)
                options.chmod(0o600)

        for scenario in ("symlink", "hardlink"):
            outside = self.tmp / f"outside-options-{scenario}.json"
            outside.write_bytes(original)
            outside.chmod(0o600)
            options.unlink()
            if scenario == "symlink":
                options.symlink_to(outside)
            else:
                os.link(outside, options)
            with (
                self.subTest(scenario=scenario),
                mock.patch.object(mission_run, "run_process") as effects,
                self.assertRaisesRegex(
                    mission_run.MissionRunError,
                    "cannot load durable JSON",
                ),
            ):
                mission_run.drive_mission(self.runs, mission_id)
            effects.assert_not_called()
            options.unlink()
            outside.unlink()
            options.write_bytes(original)
            options.chmod(0o600)

    def write_bound_manifest(
        self, *, feature: str, mission_id: str, preset: str
    ) -> dict[str, object]:
        compiled = mission_run.fleet_mission.load_compiled(
            self.runs / "missions" / mission_id / "compiled-workflow.json"
        )
        resolved = compiled["resolved"]
        if preset == resolved["preset"]:
            mode = resolved["mode"]
            groups = resolved["identity_groups"]
            lead = resolved["lead"]
            instances = resolved["instances"]
        else:
            self.assertEqual(preset, resolved["assurance_preset"])
            mode = resolved["assurance_mode"]
            groups = resolved["assurance_identity_groups"]
            lead = resolved["assurance_lead"]
            instances = resolved["assurance_instances"]
        binding = mission_run.fleet_manifest.binding_for_preset(compiled, preset)
        runtime_options = json.loads(
            (self.runs / "missions" / mission_id / "runtime-options.json").read_text(
                encoding="utf-8"
            )
        )
        lines = [
            "manifest_contract_version=3",
            "tracking_protocol=control-v1",
            f"feature={feature}",
            f"mission_id={mission_id}",
            f"preset={preset}",
            f"mode={mode}",
            f"execution_profile={runtime_options.get('execution_profile', 'native')}",
            f"compiled_digest={binding['compiled_digest']}",
            f"router_digest={binding['router_digest']}",
            f"roster_digest={binding['roster_digest']}",
            f"launch_digest={binding['launch_digest']}",
            f"identity_group.count={len(groups)}",
            f"target_repo={self.target.resolve()}",
            f"base_sha={self.base_sha}",
            "workspace=workspace:1",
        ]
        lines.extend(
            f"identity_group.{index}={','.join(group)}"
            for index, group in enumerate(groups, 1)
        )
        for index, member in enumerate(([lead] if lead else []) + list(instances), 1):
            instance = member["instance_id"]
            lines.extend(
                [
                    f"{instance}=surface:{index}",
                    f"{instance}.role_type={member['role_type']}",
                    f"{instance}.runner={member['runner']}",
                    f"{instance}.phase={member['phase']}",
                    f"{instance}.authority={member['authority']}",
                    f"{instance}.provider={member['provider']}",
                    f"{instance}.hook_source={member['hook_source']}",
                    f"{instance}.model={member['model']}",
                ]
            )
            if member.get("variant") is not None:
                lines.append(f"{instance}.variant={member['variant']}")
            if member.get("authority") == "write":
                lines.extend(
                    [
                        f"{instance}.worktree={self.tmp / ('writer-' + feature + '-' + instance)}",
                        f"{instance}.branch=fleet/{feature}/{instance}",
                        f"{instance}.base_sha={self.base_sha}",
                        f"{instance}.final_sha={self.base_sha}",
                        f"{instance}.git_isolation=isolated-clone",
                        f"{instance}.publication_state=private",
                    ]
                )
        self.runs.mkdir(parents=True, exist_ok=True)
        manifest_path = self.runs / f"fleet-{feature}.manifest"
        manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest_path.chmod(0o600)
        return lead

    def publish_fake_writer(self, feature: str) -> None:
        manifest_path = self.runs / f"fleet-{feature}.manifest"
        manifest = mission_run.fleet_manifest.read(manifest_path)
        if manifest.get("workspace.quiesced") == "1":
            return
        writers = [
            key.removesuffix(".authority")
            for key, value in manifest.items()
            if key.endswith(".authority") and value == "write"
        ]
        self.assertEqual(len(writers), 1)
        writer = writers[0]
        branch = f"fleet/{feature}/{writer}"
        subprocess.run(
            ["git", "-C", str(self.target), "branch", branch, self.base_sha], check=True
        )
        manifest.update(
            {
                f"{writer}.worktree": str(
                    self.tmp / ("retired-" + feature + "-" + writer)
                ),
                f"{writer}.branch": branch,
                f"{writer}.base_sha": self.base_sha,
                f"{writer}.final_sha": self.base_sha,
                f"{writer}.published_sha": self.base_sha,
                f"{writer}.git_isolation": "isolated-clone",
                f"{writer}.publication_state": "published",
                "workspace.quiesced": "1",
            }
        )
        manifest_path.write_text(
            "".join(f"{key}={value}\n" for key, value in manifest.items()),
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)

    def test_dry_run_compiles_and_assesses_without_creating_state(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--runs-dir",
                str(self.runs),
                "dry",
                "preview",
                "inspect the parser",
                "--workflow",
                "implementation",
                "--target-repo",
                str(self.target),
                "--json",
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

    def test_mission_rejects_target_subdirectory_before_creation(self) -> None:
        subdir = self.target / "docs"
        subdir.mkdir()
        with self.assertRaisesRegex(
            mission_run.MissionRunError, "exact physical Git toplevel"
        ):
            mission_run.create_and_drive(
                self.runs,
                feature="subdir",
                objective="reject authority widening",
                workflow_name="implementation",
                target_repo=subdir.resolve(),
                risk_override="auto",
                timeout_seconds=300,
                allow_dirty_baseline=False,
                teardown=False,
            )
        self.assertFalse((self.runs / "missions").exists())

    def test_mission_identity_binds_canonical_target_and_runs_root(self) -> None:
        second_target = self.tmp / "second-target"
        subprocess.run(
            ["git", "clone", "-q", str(self.target), str(second_target)], check=True
        )
        second_runs = self.tmp / "second-runs"

        def identity_only(runs_dir: Path, mission_id: str) -> dict[str, str]:
            return {"mission_id": mission_id, "runs_dir": str(runs_dir)}

        arguments = {
            "feature": "identity-binding",
            "objective": "exercise the same mission identity",
            "workflow_name": "implementation",
            "risk_override": "auto",
            "timeout_seconds": 300,
            "allow_dirty_baseline": False,
            "teardown": False,
        }
        with mock.patch.object(mission_run, "drive_mission", side_effect=identity_only):
            first = mission_run.create_and_drive(
                self.runs, target_repo=self.target, **arguments
            )
            repeated = mission_run.create_and_drive(
                self.runs, target_repo=self.target, **arguments
            )
            different_target = mission_run.create_and_drive(
                self.runs, target_repo=second_target, **arguments
            )
            different_runs = mission_run.create_and_drive(
                second_runs, target_repo=self.target, **arguments
            )

        self.assertEqual(first["mission_id"], repeated["mission_id"])
        self.assertNotEqual(first["mission_id"], different_target["mission_id"])
        self.assertNotEqual(first["mission_id"], different_runs["mission_id"])
        self.assertEqual(first["runs_dir"], str(self.runs.resolve()))
        self.assertEqual(different_runs["runs_dir"], str(second_runs.resolve()))

    def test_high_risk_pauses_before_fleet_boot(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--runs-dir",
                str(self.runs),
                "run",
                "high-risk",
                "deploy this to production",
                "--workflow",
                "implementation",
                "--target-repo",
                str(self.target),
                "--json",
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

    def test_expired_approval_requires_renewal_before_handoff_or_boot(self) -> None:
        calls: list[str] = []

        def git_only(command: list[str], *, timeout=None, env=None):
            del timeout, env
            name = Path(command[0]).name
            calls.append(name)
            if name != "git":
                raise AssertionError(command)
            if "status" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[-2:] == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(command, 0, self.base_sha + "\n", "")
            return subprocess.CompletedProcess(command, 0, ".git\n", "")

        with mock.patch.object(mission_run, "run_process", side_effect=git_only):
            paused = mission_run.create_and_drive(
                self.runs,
                feature="expired-approval",
                objective="deploy this to production",
                workflow_name="implementation",
                target_repo=self.target.resolve(),
                risk_override="auto",
                timeout_seconds=300,
                allow_dirty_baseline=False,
                teardown=False,
            )
        fleet_approve.approve_mission(
            self.runs,
            paused["mission_id"],
            scope=str(self.target),
            expires_in=600,
            idempotency_key="human:expired-approval",
        )
        future = mission_run.datetime.now(mission_run.timezone.utc) + (
            mission_run.timedelta(seconds=601)
        )

        class ExpiredClock:
            @classmethod
            def now(cls, tz=None):
                return future if tz is not None else future.replace(tzinfo=None)

        calls.clear()
        with (
            mock.patch.object(mission_run, "run_process", side_effect=git_only),
            mock.patch.object(mission_run, "datetime", ExpiredClock),
            mock.patch.object(
                mission_run.fleet_audit_client, "AuditLifecycle"
            ) as audit,
        ):
            value = mission_run.drive_mission(self.runs, paused["mission_id"])

        self.assertEqual(value["status"], "assurance_approved")
        self.assertIn("--renew", value["next_action"])
        self.assertEqual(calls, [])
        audit.assert_not_called()
        current = mission_run.fleet_mission.load_state(self.runs, paused["mission_id"])
        self.assertEqual(current["status"], "assurance_approved")
        self.assertFalse(
            any(
                event["kind"] == "assurance_boot_started"
                for event in mission_run.mission_state.read_events(
                    mission_run.mission_state.ledger_path(
                        self.runs, paused["mission_id"]
                    ),
                    expected_mission_id=paused["mission_id"],
                )
            )
        )

    def test_local_worm_cannot_satisfy_regulated_profile_or_risk(self) -> None:
        for objective, profile in (
            ("inspect a local audit", "regulated"),
            ("prepare a HIPAA regulated archive", "native"),
        ):
            with self.subTest(objective=objective, profile=profile):
                result = subprocess.run(
                    [
                        "python3",
                        str(SCRIPT),
                        "--runs-dir",
                        str(self.runs),
                        "dry",
                        "local-worm-negative",
                        objective,
                        "--workflow",
                        "local-worm",
                        "--target-repo",
                        str(self.target),
                        "--execution-profile",
                        profile,
                        "--json",
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
                "python3",
                str(SCRIPT),
                "--runs-dir",
                str(self.runs),
                "dry",
                "research-private",
                "analyze private customer data",
                "--workflow",
                "research",
                "--target-repo",
                str(self.target),
                "--json",
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
        active_run_id: str | None = None

        def result_for(feature: str, run_id: str) -> Path:
            results = self.runs / "results"
            feature_results = results / feature
            results.mkdir(mode=0o755, exist_ok=True)
            results.chmod(0o755)
            for directory in (feature_results,):
                directory.mkdir(mode=0o700, exist_ok=True)
                directory.chmod(0o700)
            result_file = feature_results / f"{run_id}.txt"
            result_file.write_text(
                "STATUS: DONE\nDECISION: accepted\n", encoding="utf-8"
            )
            result_file.chmod(0o600)
            return result_file

        def fake(command: list[str], *, timeout=None, env=None):
            nonlocal active_run_id
            del timeout
            name = Path(command[0]).name
            calls.append(name)
            if name == "git":
                if "status" in command:
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[-2:] == ["rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(
                        command, 0, self.base_sha + "\n", ""
                    )
                return subprocess.CompletedProcess(command, 0, ".git\n", "")
            if name == "fleet-up.sh":
                assert env is not None
                mission_id = env["FLEET_MISSION_ID"]
                feature = command[1]
                preset = command[command.index("--preset") + 1]
                self.assertEqual(
                    command[command.index("--expected-base-sha") + 1], self.base_sha
                )
                self.write_bound_manifest(
                    feature=feature, mission_id=mission_id, preset=preset
                )
                return subprocess.CompletedProcess(command, 0, "booted\n", "")
            if name == "fleet-send.sh":
                prompt = command[3]
                run_id = command[command.index("--run-id") + 1]
                active_run_id = run_id
                result_file = result_for(command[1], run_id)
                manifest = mission_run.fleet_manifest.load(
                    self.runs / f"fleet-{command[1]}.manifest"
                )
                event = {
                    "timestamp": "2026-07-14T00:00:00Z",
                    "run_id": run_id,
                    "feature": command[1],
                    "instance": "lead",
                    "status": "succeeded",
                    "task_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "result_file": str(result_file),
                    "provider": manifest["lead.provider"],
                    "model": manifest["lead.model"],
                    "variant": manifest.get("lead.variant"),
                }
                ledger_path = self.runs / f"fleet-{command[1]}.ledger.jsonl"
                ledger_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
                ledger_path.chmod(0o600)
                return subprocess.CompletedProcess(
                    command, 0, json.dumps({"run_id": run_id}) + "\n", ""
                )
            if name == "fleet-wait.sh":
                requested_run = command[command.index("--run") + 1].split("=", 1)[1]
                self.assertEqual(requested_run, active_run_id)
                result_file = result_for(command[1], requested_run)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "run_id": requested_run,
                            "status": "succeeded",
                            "result_file": str(result_file),
                        }
                    )
                    + "\n",
                    "",
                )
            if name == "fleet-down.sh":
                self.assertEqual(command[-1], "--prepare-archive")
                self.publish_fake_writer(command[1])
                return subprocess.CompletedProcess(command, 0, "closed\n", "")
            raise AssertionError(command)

        return calls, fake

    def complete_autonomous(self, feature: str) -> dict[str, object]:
        _, fake = self.fake_runtime()
        with (
            mock.patch.object(mission_run, "run_process", side_effect=fake),
            mock.patch.object(mission_run, "cmux_signal"),
        ):
            return mission_run.create_and_drive(
                self.runs,
                feature=feature,
                objective="implement and verify the parser",
                workflow_name="implementation",
                target_repo=self.target.resolve(),
                risk_override="auto",
                timeout_seconds=300,
                allow_dirty_baseline=False,
                teardown=False,
            )

    def test_autonomous_mission_completes_with_durable_result_and_archive(self) -> None:
        calls, fake = self.fake_runtime()
        with (
            mock.patch.object(mission_run, "run_process", side_effect=fake),
            mock.patch.object(mission_run, "cmux_signal"),
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
        self.assertEqual(calls.count("fleet-down.sh"), 1)
        mission_id = value["mission_id"]
        root = self.runs / "missions" / mission_id
        self.assertTrue((root / "archive" / "archive-index.json").is_file())
        self.assertTrue(
            mission_run.fleet_archive.verify_archive(root / "archive")["valid"]
        )
        state = mission_run.fleet_mission.load_state(self.runs, mission_id)
        self.assertEqual(state["lead_result"]["provider"], "openai")
        self.assertEqual(state["lead_result"]["model"], "gpt-5.6-sol")
        result_bytes = value["result"].encode("utf-8")
        self.assertEqual(
            hashlib.sha256(result_bytes).hexdigest(),
            state["lead_result"]["artifact_id"],
        )
        self.assertEqual(
            mission_run.fleet_artifacts.get_bytes(
                self.runs, mission_id, state["lead_result"]["artifact_id"]
            ),
            result_bytes,
        )

    def test_live_assurance_request_waits_for_exact_lead_finalization(self) -> None:
        calls, base_runtime = self.fake_runtime()
        request_statuses: list[str] = []

        def live_escalation(command: list[str], *, timeout=None, env=None):
            if Path(command[0]).name == "fleet-wait.sh" and not request_statuses:
                manifest = mission_run.fleet_manifest.load(
                    self.runs / f"fleet-{command[1]}.manifest"
                )
                requested = mission_run.fleet_control.FleetControl(
                    self.runs, manifest["mission_id"]
                ).request_assurance(
                    risk="high",
                    categories=["production"],
                    reason="Lead discovered a production effect during execution",
                    idempotency_key="live:assurance",
                    actor="lead",
                )
                request_statuses.append(requested["status"])
            return base_runtime(command, timeout=timeout, env=env)

        with (
            mock.patch.object(mission_run, "run_process", side_effect=live_escalation),
            mock.patch.object(mission_run, "cmux_signal"),
        ):
            value = mission_run.create_and_drive(
                self.runs,
                feature="live-assurance",
                objective="implement and verify the parser",
                workflow_name="implementation",
                target_repo=self.target.resolve(),
                risk_override="auto",
                timeout_seconds=300,
                allow_dirty_baseline=False,
                teardown=False,
            )

        self.assertEqual(request_statuses, ["running"])
        self.assertEqual(value["status"], "awaiting_assurance_confirmation")
        self.assertEqual(calls.count("fleet-send.sh"), 1)
        current = mission_run.fleet_mission.load_state(self.runs, value["mission_id"])
        self.assertFalse(
            any(admission.get("active") for admission in current["admissions"].values())
        )
        leads = [
            admission
            for admission in current["admissions"].values()
            if admission["run_kind"] == "lead"
        ]
        self.assertEqual(len(leads), 1)
        lead = leads[0]
        self.assertEqual(lead["phase"], "finalized")
        events = mission_run.mission_state.read_events(
            mission_run.mission_state.ledger_path(self.runs, value["mission_id"]),
            expected_mission_id=value["mission_id"],
        )
        finalized_sequence = next(
            event["sequence"]
            for event in events
            if event["kind"] == "delegation_finalized"
            and event["payload"]["admission_id"] == lead["admission_id"]
        )
        requested_sequence = next(
            event["sequence"]
            for event in events
            if event["kind"] == "assurance_requested"
        )
        self.assertLess(finalized_sequence, requested_sequence)

        def lane_probe(label: str) -> dict[str, object]:
            task_sha256 = hashlib.sha256(label.encode("utf-8")).hexdigest()
            return {
                "request_key": f"lane:{label}",
                "run_kind": "specialist",
                "recipient_instance": f"probe-{label}",
                "capability": "recon",
                "parent_admission_id": None,
                "parent_run_id": None,
                "delegated_budget": 0,
                "writer": False,
                "effect_sha256": mission_run.mission_state.sha256(
                    {"lane_probe": label, "task_sha256": task_sha256}
                ),
                "task_sha256": task_sha256,
            }

        with self.assertRaisesRegex(
            mission_run.mission_state.MissionConflict,
            "closed|lane|requires|no longer",
        ):
            mission_run.fleet_admission.reserve_many(
                self.runs,
                value["mission_id"],
                requests=[lane_probe("awaiting")],
                idempotency_key="lane:awaiting:control",
                actor="CONTROL",
            )

        fleet_approve.approve_mission(
            self.runs,
            value["mission_id"],
            scope=str(self.target),
            expires_in=600,
            idempotency_key="human:live-assurance",
        )
        with self.assertRaisesRegex(
            mission_run.mission_state.MissionConflict,
            "closed|lane|requires|no longer",
        ):
            mission_run.fleet_admission.reserve_many(
                self.runs,
                value["mission_id"],
                requests=[lane_probe("approved")],
                idempotency_key="lane:approved:control",
                actor="CONTROL",
            )

        class StopAfterAssured(BaseException):
            pass

        class FakeAssuredRunner:
            def __init__(inner_self, runs_dir, mission_id):
                self.assertEqual(runs_dir, self.runs)
                self.assertEqual(mission_id, value["mission_id"])

            def drive(inner_self, spec_path):
                self.assertTrue(spec_path.is_file())
                raise StopAfterAssured()

        class FakeAuditLifecycle:
            def __init__(inner_self, runs_dir, mission_id):
                inner_self.lifecycle_path = (
                    runs_dir / "missions" / mission_id / "audit" / "lifecycle.json"
                )

            def preflight(inner_self):
                return {"mode": "signed", "configured": True}

            def start(inner_self, manifest_path):
                self.assertTrue(manifest_path.is_file())
                inner_self.lifecycle_path.parent.mkdir(parents=True, exist_ok=True)
                inner_self.lifecycle_path.parent.chmod(0o700)
                inner_self.lifecycle_path.write_text(
                    '{"stopped_at":null}\n', encoding="utf-8"
                )
                inner_self.lifecycle_path.chmod(0o600)
                return {"started": True}

            def record_control_event(inner_self, **kwargs):
                self.assertRegex(kwargs["subject_sha256"], r"^[0-9a-f]{64}$")
                return {"event_id": str(uuid.uuid4())}

        handoff_timeouts: list[int | None] = []

        def assured_runtime(command: list[str], *, timeout=None, env=None):
            if (
                Path(command[0]).name == "fleet-down.sh"
                and command[-1] == "--handoff-assurance"
            ):
                handoff_timeouts.append(timeout)
                mission_id = value["mission_id"]
                handoff_root = self.runs / "missions" / mission_id / "assurance-handoff"
                runtime_root = handoff_root / "main-runtime"
                runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                runtime_root.chmod(0o700)
                for suffix in (
                    "manifest",
                    "state.json",
                    "ledger.jsonl",
                    "dialogue.jsonl",
                    "dialogue-control.jsonl",
                    "assurance-control.jsonl",
                    "verification-receipt.json",
                    "assurance-receipt.json",
                ):
                    source = self.runs / f"fleet-live-assurance.{suffix}"
                    if source.exists():
                        source.replace(runtime_root / source.name)
                receipt_relative = (
                    f"missions/{mission_id}/assurance-handoff/receipt.json"
                )
                receipt = handoff_root / "receipt.json"
                receipt.write_bytes(
                    mission_run.mission_state.canonical_bytes(
                        {
                            "feature": "live-assurance",
                            "mission_id": mission_id,
                            "receipt": receipt_relative,
                            "status": "ready",
                        }
                    )
                    + b"\n"
                )
                receipt.chmod(0o600)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "feature": "live-assurance",
                            "mission_id": mission_id,
                            "receipt": receipt_relative,
                            "status": "ready",
                        }
                    )
                    + "\n",
                    "",
                )
            return base_runtime(command, timeout=timeout, env=env)

        with (
            mock.patch.object(mission_run, "run_process", side_effect=assured_runtime),
            mock.patch.object(mission_run, "cmux_signal"),
            mock.patch.object(
                mission_run.fleet_assured_runner,
                "AssuredRunner",
                FakeAssuredRunner,
            ),
            mock.patch.object(
                mission_run.fleet_audit_client,
                "AuditLifecycle",
                FakeAuditLifecycle,
            ),
            mock.patch.dict(
                os.environ,
                {
                    "FLEET_HANDOFF_READY_TIMEOUT_SECONDS": "7",
                    "FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS": "11",
                },
            ),
            self.assertRaises(StopAfterAssured),
        ):
            mission_run.drive_mission(self.runs, value["mission_id"])

        self.assertEqual(handoff_timeouts, [258])

        assured_state = mission_run.fleet_mission.load_state(
            self.runs, value["mission_id"]
        )
        self.assertEqual(assured_state["status"], "assured_running")
        self.assertFalse(
            any(
                admission.get("active")
                for admission in assured_state["admissions"].values()
            )
        )
        with self.assertRaisesRegex(
            mission_run.mission_state.MissionConflict,
            "lane|requires|no longer",
        ):
            mission_run.fleet_admission.reserve_many(
                self.runs,
                value["mission_id"],
                requests=[lane_probe("assured-control")],
                idempotency_key="lane:assured:control",
                actor="CONTROL",
            )
        assured_reservation = mission_run.fleet_admission.reserve_many(
            self.runs,
            value["mission_id"],
            requests=[lane_probe("assured")],
            idempotency_key="lane:assured:allowed",
            actor="ASSURED",
        )["admissions"][0]
        self.assertEqual(assured_reservation["lane_actor"], "ASSURED")
        self.assertIsNone(assured_reservation["parent_admission_id"])
        self.assertIsNone(assured_reservation["parent_run_id"])

    def test_terminal_lead_result_rejects_symlink_before_teardown(self) -> None:
        value = self.complete_autonomous("terminal-result-symlink")
        mission_id = str(value["mission_id"])
        result_path = self.runs / "missions" / mission_id / "lead-result.txt"
        outside = self.tmp / "outside-result.txt"
        outside.write_bytes(result_path.read_bytes())
        outside.chmod(0o600)
        before = outside.read_bytes()
        result_path.unlink()
        result_path.symlink_to(outside)
        with mock.patch.object(mission_run, "require_success") as effects:
            with self.assertRaisesRegex(
                mission_run.MissionRunError, "unsafe terminal Lead result"
            ):
                mission_run.drive_mission(self.runs, mission_id)
        effects.assert_not_called()
        self.assertEqual(outside.read_bytes(), before)

    def test_terminal_lead_result_rejects_hardlink(self) -> None:
        value = self.complete_autonomous("terminal-result-hardlink")
        mission_id = str(value["mission_id"])
        current = mission_run.fleet_mission.load_state(self.runs, mission_id)
        result_path = self.runs / "missions" / mission_id / "lead-result.txt"
        outside = self.tmp / "outside-hardlink.txt"
        outside.write_bytes(result_path.read_bytes())
        outside.chmod(0o600)
        before = outside.read_bytes()
        result_path.unlink()
        os.link(outside, result_path)
        with self.assertRaisesRegex(
            mission_run.MissionRunError, "unsafe terminal Lead result"
        ):
            mission_run.terminal_lead_result(self.runs, mission_id, current)
        self.assertEqual(outside.read_bytes(), before)

    def test_terminal_lead_result_rejects_digest_drift(self) -> None:
        value = self.complete_autonomous("terminal-result-drift")
        mission_id = str(value["mission_id"])
        current = mission_run.fleet_mission.load_state(self.runs, mission_id)
        result_path = self.runs / "missions" / mission_id / "lead-result.txt"
        result_path.write_bytes(b"STATUS: DONE\nDECISION: substituted\n")
        result_path.chmod(0o600)
        with self.assertRaisesRegex(
            mission_run.MissionRunError, "does not match its artifact_id"
        ):
            mission_run.terminal_lead_result(self.runs, mission_id, current)

    def test_terminal_lead_result_rejects_corrupt_cas(self) -> None:
        value = self.complete_autonomous("terminal-result-cas")
        mission_id = str(value["mission_id"])
        current = mission_run.fleet_mission.load_state(self.runs, mission_id)
        artifact_id = current["lead_result"]["artifact_id"]
        artifact = mission_run.fleet_artifacts.artifact_path(
            self.runs, mission_id, artifact_id
        )
        artifact.write_bytes(b"corrupt")
        artifact.chmod(0o600)
        with self.assertRaisesRegex(
            mission_run.MissionRunError, "cannot verify terminal Lead artifact"
        ):
            mission_run.terminal_lead_result(self.runs, mission_id, current)

    def test_lead_result_rejects_symlinked_result_store_ancestor(self) -> None:
        _, runtime = self.fake_runtime()

        def symlink_result_store(command: list[str], *, timeout=None, env=None):
            result = runtime(command, timeout=timeout, env=env)
            if Path(command[0]).name == "fleet-send.sh":
                feature_dir = self.runs / "results" / command[1]
                outside = self.tmp / "outside-results"
                feature_dir.rename(outside)
                feature_dir.symlink_to(outside, target_is_directory=True)
            return result

        with (
            mock.patch.object(
                mission_run, "run_process", side_effect=symlink_result_store
            ),
            mock.patch.object(mission_run, "cmux_signal"),
        ):
            with self.assertRaisesRegex(
                mission_run.MissionRunError, "unsafe lead result store"
            ):
                mission_run.create_and_drive(
                    self.runs,
                    feature="unsafe-result",
                    objective="reject a symlinked result store",
                    workflow_name="implementation",
                    target_repo=self.target.resolve(),
                    risk_override="auto",
                    timeout_seconds=300,
                    allow_dirty_baseline=False,
                    teardown=False,
                )

    def test_wait_failure_without_json_preserves_active_admission_for_reconciliation(
        self,
    ) -> None:
        _, runtime = self.fake_runtime()

        def fail_wait(command, *, timeout=None, env=None):
            if Path(command[0]).name == "fleet-wait.sh":
                return subprocess.CompletedProcess(
                    command, 5, "", "timeout without JSON"
                )
            return runtime(command, timeout=timeout, env=env)

        with (
            mock.patch.object(mission_run, "run_process", side_effect=fail_wait),
            mock.patch.object(mission_run, "cmux_signal"),
        ):
            with self.assertRaisesRegex(
                mission_run.MissionRunError,
                "without durable Lead terminal evidence",
            ):
                mission_run.create_and_drive(
                    self.runs,
                    feature="wait-no-json",
                    objective="implement and verify the parser",
                    workflow_name="implementation",
                    target_repo=self.target.resolve(),
                    risk_override="auto",
                    timeout_seconds=300,
                    allow_dirty_baseline=False,
                    teardown=False,
                )
        mission_dirs = [
            path for path in (self.runs / "missions").iterdir() if path.is_dir()
        ]
        self.assertEqual(len(mission_dirs), 1)
        mission_id = mission_dirs[0].name
        current = mission_run.fleet_mission.load_state(self.runs, mission_id)
        self.assertEqual(current["status"], "running")
        self.assertIsNone(current["terminal"])
        self.assertEqual(
            current["admissions"][current["lead_admission_id"]]["phase"], "started"
        )
        events = mission_run.mission_state.read_events(
            mission_run.mission_state.ledger_path(self.runs, mission_id),
            expected_mission_id=mission_id,
        )
        self.assertNotEqual(events[-1]["kind"], "mission_terminal")

    def test_terminal_teardown_keeps_audit_live_until_fleet_down(self) -> None:
        compiled = mission_run.workflow_config.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )
        mission_id, _ = mission_run.fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature="terminal-down",
            objective="verify teardown ordering",
            target_repo=self.target.resolve(),
            base_sha=self.base_sha,
            idempotency_key="create:terminal-down",
        )
        root = self.runs / "missions" / mission_id
        (root / "objective.txt").write_text(
            "verify teardown ordering\n", encoding="utf-8"
        )
        (root / "runtime-options.json").write_bytes(
            mission_run.fleet_json.canonical_bytes(
                {"teardown": True, "execution_profile": "native"}
            )
            + b"\n"
        )
        (root / "runtime-options.json").chmod(0o600)
        mission_run.mission_state.append_terminal(
            self.runs,
            mission_id,
            status="failed",
            reason="synthetic terminal",
            idempotency_key="test:terminal",
        )
        self.write_bound_manifest(
            feature="terminal-down",
            mission_id=mission_id,
            preset=compiled["resolved"]["preset"],
        )
        audit_lifecycle = root / "audit" / "lifecycle.json"
        audit_lifecycle.parent.mkdir(parents=True)
        audit_lifecycle.parent.chmod(0o700)
        audit_lifecycle.write_text('{"stopped_at":null}\n', encoding="utf-8")
        audit_lifecycle.chmod(0o600)
        calls: list[str] = []

        class FakeControlLifecycle:
            def __init__(inner_self, runs_dir, actual_mission_id, preset=None):
                inner_self.lifecycle_path = root / "control" / "lifecycle.json"

        class FakeAuditLifecycle:
            def __init__(inner_self, runs_dir, actual_mission_id):
                inner_self.lifecycle_path = audit_lifecycle

            def stop(inner_self):
                calls.append("audit-stop")

        def fake_require(command, *, timeout=None, env=None):
            self.assertEqual(Path(command[0]).name, "fleet-down.sh")
            self.assertNotIn("audit-stop", calls)
            self.assertIsNotNone(env)
            self.assertEqual(env["FLEET_RUNS_DIR"], str(self.runs))
            calls.append("fleet-down")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(
                mission_run.fleet_control_service,
                "ControlLifecycle",
                FakeControlLifecycle,
            ),
            mock.patch.object(
                mission_run.fleet_audit_client, "AuditLifecycle", FakeAuditLifecycle
            ),
            mock.patch.object(mission_run, "require_success", side_effect=fake_require),
        ):
            result = mission_run.drive_mission(self.runs, mission_id)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(calls, ["fleet-down"])

    def test_regulated_boot_starts_control_before_healthcheck_and_reconciles_failure(
        self,
    ) -> None:
        compiled = mission_run.workflow_config.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )
        mission_id, _ = mission_run.fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature="regulated-boot-order",
            objective="verify regulated boot ordering",
            target_repo=self.target.resolve(),
            base_sha=self.base_sha,
            idempotency_key="create:regulated-boot-order",
            runtime_options={"execution_profile": "regulated", "timeout_seconds": 300},
        )
        mission_run.mission_state.append_event(
            self.runs,
            mission_id,
            kind="fleet_boot_started",
            actor="CONTROL",
            idempotency_key="test:boot",
            payload={"feature": "regulated-boot-order", "preset": "dan"},
        )

        class ReconciledControl:
            active = False
            physical_starts = 0
            start_calls = 0
            stop_calls = 0

            def __init__(inner_self, runs_dir, actual_mission_id, preset=None):
                inner_self.lifecycle_path = self.runs / "control-lifecycle.json"
                inner_self.socket_root = self.runs / "control-socket"

            def start(inner_self):
                ReconciledControl.start_calls += 1
                if not ReconciledControl.active:
                    ReconciledControl.active = True
                    ReconciledControl.physical_starts += 1
                return {"started": ReconciledControl.physical_starts == 1}

            def stop(inner_self):
                ReconciledControl.stop_calls += 1
                ReconciledControl.active = False

        def fail_boot(command, *, timeout=None, env=None):
            del timeout
            self.assertEqual(Path(command[0]).name, "fleet-up.sh")
            self.assertTrue(ReconciledControl.active)
            self.assertIn("--compiled-workflow", command)
            self.assertEqual(
                command[command.index("--expected-base-sha") + 1], self.base_sha
            )
            self.assertEqual(
                env["FLEET_CONTROL_SOCKET_DIR"], str(self.runs / "control-socket")
            )
            raise mission_run.MissionRunError("synthetic healthcheck failure")

        with (
            mock.patch.object(
                mission_run.fleet_control_service, "ControlLifecycle", ReconciledControl
            ),
            mock.patch.object(mission_run, "enforce_audit_trust"),
            mock.patch.object(mission_run, "require_success", side_effect=fail_boot),
        ):
            for _ in range(2):
                with self.assertRaisesRegex(
                    mission_run.MissionRunError, "synthetic healthcheck failure"
                ):
                    mission_run.drive_mission(self.runs, mission_id)
        self.assertEqual(ReconciledControl.start_calls, 2)
        self.assertEqual(ReconciledControl.physical_starts, 1)
        self.assertEqual(ReconciledControl.stop_calls, 0)
        self.assertEqual(
            mission_run.fleet_mission.load_state(self.runs, mission_id)["status"],
            "booting",
        )

    def test_resume_rejects_target_head_drift_before_fleet_boot(self) -> None:
        compiled = mission_run.workflow_config.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )
        mission_id, _ = mission_run.fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature="base-drift",
            objective="reject a changed baseline",
            target_repo=self.target.resolve(),
            base_sha=self.base_sha,
            idempotency_key="create:base-drift",
            runtime_options={"execution_profile": "native", "timeout_seconds": 300},
        )
        mission_run.mission_state.append_event(
            self.runs,
            mission_id,
            kind="fleet_boot_started",
            actor="CONTROL",
            idempotency_key="test:base-drift:boot",
            payload={"feature": "base-drift", "preset": "dan"},
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.target),
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "drift",
            ],
            check=True,
        )
        with mock.patch.object(mission_run, "require_success") as effects:
            with self.assertRaisesRegex(mission_run.MissionRunError, "frozen base_sha"):
                mission_run.drive_mission(self.runs, mission_id)
        effects.assert_not_called()

    def test_resume_adopts_exact_legacy_lead_run_without_redispatch(self) -> None:
        calls, fake = self.fake_runtime()
        original_append = mission_run.mission_state.append_event
        crashed = False

        def crash_once(*args, **kwargs):
            nonlocal crashed
            if kwargs.get("kind") == "lead_dispatched" and not crashed:
                crashed = True
                raise mission_run.MissionRunError(
                    "simulated controller death after fleet-send"
                )
            return original_append(*args, **kwargs)

        with (
            mock.patch.object(mission_run, "run_process", side_effect=fake),
            mock.patch.object(mission_run, "cmux_signal"),
            mock.patch.object(
                mission_run.mission_state, "append_event", side_effect=crash_once
            ),
        ):
            with self.assertRaisesRegex(
                mission_run.MissionRunError, "simulated controller death"
            ):
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

        mission_dirs = [
            path for path in (self.runs / "missions").iterdir() if path.is_dir()
        ]
        self.assertEqual(len(mission_dirs), 1)
        with (
            mock.patch.object(mission_run, "run_process", side_effect=fake),
            mock.patch.object(mission_run, "cmux_signal"),
        ):
            value = mission_run.drive_mission(self.runs, mission_dirs[0].name)
        self.assertEqual(value["status"], "succeeded")
        self.assertEqual(calls.count("fleet-send.sh"), 1)

    def test_approved_high_risk_mission_bridges_to_assured_runner_and_lead(
        self,
    ) -> None:
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
        synthesis_runs: list[str] = []
        assured_calls: list[str] = []

        def assured_runtime(command: list[str], *, timeout=None, env=None):
            del timeout
            name = Path(command[0]).name
            assured_calls.append(name)
            if name == "fleet-down.sh":
                self.assertEqual(command[-1], "--prepare-archive")
                self.publish_fake_writer("assured")
                return subprocess.CompletedProcess(command, 0, "prepared\n", "")
            if name != "fleet-up.sh":
                raise AssertionError(command)
            assert env is not None
            preset = command[command.index("--preset") + 1]
            self.write_bound_manifest(
                feature="assured", mission_id=mission_id, preset=preset
            )
            return subprocess.CompletedProcess(command, 0, "booted\n", "")

        class FakeAssuredRunner:
            def __init__(inner_self, runs_dir, actual_mission_id):
                self.assertEqual(runs_dir, self.runs)
                self.assertEqual(actual_mission_id, mission_id)

            def drive(inner_self, spec_path):
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
                self.assertIn("acceptance_criteria", spec)
                approval_event_sha256 = mission_run.fleet_mission.load_state(
                    self.runs, mission_id
                )["approval"]["event_sha256"]
                request_key = (
                    "assured-"
                    + mission_run.mission_state.sha256(
                        {
                            "action": "runner:assured-synthesis",
                            "approval_event_sha256": approval_event_sha256,
                        }
                    )[:40]
                )
                effect_sha256 = mission_run.mission_state.sha256(
                    {
                        "approval_event_sha256": approval_event_sha256,
                        "prompt_sha256": "d" * 64,
                        "recipient_instance": "lead",
                        "capability": "synthesis",
                    }
                )
                admission = mission_run.fleet_admission.reserve_many(
                    self.runs,
                    mission_id,
                    requests=[
                        {
                            "request_key": request_key,
                            "run_kind": "specialist",
                            "recipient_instance": "lead",
                            "capability": "synthesis",
                            "parent_admission_id": None,
                            "parent_run_id": None,
                            "delegated_budget": 0,
                            "writer": False,
                            "effect_sha256": effect_sha256,
                            "task_sha256": "d" * 64,
                        }
                    ],
                    idempotency_key=f"admission:{request_key}",
                    actor="ASSURED",
                )["admissions"][0]
                committed = mission_run.fleet_admission.commit(
                    self.runs,
                    mission_id,
                    admission_id=admission["admission_id"],
                    request_digest=admission["request_digest"],
                    effect_sha256=effect_sha256,
                    recipient_instance="lead",
                    writer=False,
                    run_id=admission["run_id"],
                    idempotency_key=f"admission:commit:{request_key}",
                    actor="ASSURED",
                )
                authorization = mission_run.fleet_admission.authorize_launch(
                    self.runs,
                    mission_id,
                    admission_id=admission["admission_id"],
                    commit_event_sha256=committed["commit_event_sha256"],
                    request_digest=admission["request_digest"],
                    effect_sha256=effect_sha256,
                    recipient_instance="lead",
                    writer=False,
                    run_id=admission["run_id"],
                    approval_event_sha256=approval_event_sha256,
                    idempotency_key=f"admission:authorize:{request_key}",
                    actor="ASSURED",
                )
                mission_run.fleet_admission.mark_started(
                    self.runs,
                    mission_id,
                    admission_id=admission["admission_id"],
                    authorization_event_sha256=authorization[
                        "authorization_event_sha256"
                    ],
                    request_digest=admission["request_digest"],
                    effect_sha256=effect_sha256,
                    recipient_instance="lead",
                    writer=False,
                    run_id=admission["run_id"],
                    idempotency_key=f"admission:start:{request_key}",
                    actor="ASSURED",
                )
                lead_run = admission["run_id"]
                synthesis_runs.append(lead_run)
                result_file = self.runs / "results" / "assured" / f"{lead_run}.txt"
                (self.runs / "results").mkdir(mode=0o755, exist_ok=True)
                (self.runs / "results").chmod(0o755)
                result_file.parent.mkdir(mode=0o700, exist_ok=True)
                result_file.parent.chmod(0o700)
                result_file.write_text(
                    "STATUS: DONE\nDECISION: assured\n", encoding="utf-8"
                )
                result_file.chmod(0o600)
                manifest = mission_run.fleet_manifest.load(
                    self.runs / "fleet-assured.manifest"
                )
                lifecycle = {
                    "timestamp": "2026-07-14T00:00:00Z",
                    "run_id": lead_run,
                    "feature": "assured",
                    "instance": "lead",
                    "status": "succeeded",
                    "task_sha256": "d" * 64,
                    "result_file": str(result_file),
                    "provider": manifest["lead.provider"],
                    "model": manifest["lead.model"],
                    "variant": manifest.get("lead.variant"),
                }
                ledger_path = self.runs / "fleet-assured.ledger.jsonl"
                ledger_path.write_text(json.dumps(lifecycle) + "\n", encoding="utf-8")
                ledger_path.chmod(0o600)
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
                inner_self.lifecycle_path = (
                    self.runs / "missions" / mission_id / "audit" / "lifecycle.json"
                )

            def preflight(inner_self):
                return {"mode": "signed", "configured": True}

            def start(inner_self, manifest_path):
                self.assertEqual(manifest_path, self.runs / "fleet-assured.manifest")
                inner_self.lifecycle_path.parent.mkdir(parents=True, exist_ok=True)
                inner_self.lifecycle_path.parent.chmod(0o700)
                inner_self.lifecycle_path.write_bytes(
                    mission_run.fleet_json.canonical_bytes({"stopped_at": None}) + b"\n"
                )
                inner_self.lifecycle_path.chmod(0o600)
                return {"started": True}

            def record_control_event(inner_self, **kwargs):
                self.assertRegex(kwargs["subject_sha256"], r"^[0-9a-f]{64}$")
                return {"event_id": str(uuid.uuid4())}

            def verify(inner_self):
                return {"valid": True, "worm": False}

            def stop(inner_self):
                inner_self.lifecycle_path.write_bytes(
                    mission_run.fleet_json.canonical_bytes(
                        {"stopped_at": "2099-01-01T00:00:00Z"}
                    )
                    + b"\n"
                )
                inner_self.lifecycle_path.chmod(0o600)
                return {"stopped": True}

        class FakeArchiveBuilder:
            def __init__(inner_self, runs_dir, actual_mission_id):
                self.assertEqual(runs_dir, self.runs)
                self.assertEqual(actual_mission_id, mission_id)

            def create(inner_self, manifest_path):
                self.assertEqual(manifest_path, self.runs / "fleet-assured.manifest")
                self.assertEqual(assured_calls[-1], "fleet-down.sh")
                manifest = mission_run.fleet_manifest.read(manifest_path)
                self.assertEqual(manifest["workspace.quiesced"], "1")
                archive = self.runs / "missions" / mission_id / "archive"
                archive.mkdir(parents=True)
                index = archive / "archive-index.json"
                index.write_text("{}\n", encoding="utf-8")
                return {"valid": True}

        with (
            mock.patch.object(mission_run, "run_process", side_effect=assured_runtime),
            mock.patch.object(mission_run, "cmux_signal"),
            mock.patch.object(
                mission_run.fleet_assured_runner, "AssuredRunner", FakeAssuredRunner
            ),
            mock.patch.object(
                mission_run.fleet_audit_client, "AuditLifecycle", FakeAuditLifecycle
            ),
            mock.patch.object(
                mission_run.fleet_archive, "ArchiveBuilder", FakeArchiveBuilder
            ),
        ):
            value = mission_run.drive_mission(self.runs, mission_id)
        self.assertEqual(value["status"], "succeeded")
        self.assertIn("DECISION: assured", value["result"])
        current = mission_run.fleet_mission.load_state(self.runs, mission_id)
        self.assertIsNone(current["lead_run_id"])
        self.assertEqual(current["synthesis_result"]["run_id"], synthesis_runs[0])
        synthesis_owner = current["run_owners"][synthesis_runs[0]]
        self.assertEqual(synthesis_owner["owner_kind"], "admission")
        self.assertEqual(
            current["admissions"][synthesis_owner["owner_id"]]["capability"],
            "synthesis",
        )
        self.assertEqual(current["approval"]["scope"], str(self.target.resolve()))

    def test_worm_preflight_failure_happens_before_fleet_boot(self) -> None:
        workflow = mission_run.workflow_config.load_workflow(
            ROOT / "workflows" / "regulated.yaml"
        )
        # This test isolates external-WORM ordering.  The checked-in regulated
        # hard-token policy is independently rejected at compilation because
        # current S0 providers cannot enforce total tokens.
        workflow["limits"] = {
            **workflow["limits"],
            "budget_mode": "soft",
            "token_budget": 0,
        }
        compiled = mission_run.workflow_config.compile_workflow(workflow)
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
            payload={
                "level": "high",
                "categories": ["regulated"],
                "workflow_minimum": compiled["workflow"]["risk"]["minimum"],
                "override": "auto",
                "requires_confirmation": True,
            },
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

        with (
            mock.patch.object(
                mission_run.fleet_audit_client, "AuditLifecycle", FailingAuditLifecycle
            ),
            mock.patch.object(
                mission_run,
                "run_process",
                side_effect=AssertionError("fleet must not boot"),
            ),
            self.assertRaisesRegex(
                mission_run.fleet_audit_client.AuditClientError,
                "missing S3 Object Lock",
            ),
        ):
            mission_run.drive_mission(self.runs, mission_id)

        current = mission_run.fleet_mission.load_state(self.runs, mission_id)
        self.assertEqual(current["status"], "assurance_approved")
        self.assertFalse((self.runs / "fleet-worm-preflight.manifest").exists())


if __name__ == "__main__":
    unittest.main()
