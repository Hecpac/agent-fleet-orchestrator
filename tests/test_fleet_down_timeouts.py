from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
FLEET_DOWN = ROOT / "scripts" / "fleet-down.sh"
MISSION_ID = "00000000-0000-4000-8000-000000000001"


class FleetDownHandoffTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        source = FLEET_DOWN.read_text(encoding="utf-8")
        boundary = source.index("\nrelease_fleet_boot_lock() {")
        self.prefix = source[:boundary]
        boot_boundary = source.index("\nreconcile_assurance_handoff_close() {")
        self.boot_prefix = source[:boot_boundary]
        self.fake_bin = self.tmp / "bin"
        self.fake_bin.mkdir()
        self.pid_file = self.tmp / "child.pid"

    def run_harness(
        self,
        fake_python: str,
        body: str,
        *,
        ready_timeout: str = "1",
        commit_timeout: str = "1",
    ) -> subprocess.CompletedProcess[str]:
        python = self.fake_bin / "python3"
        python.write_text(fake_python, encoding="utf-8")
        python.chmod(0o700)
        harness = self.tmp / "harness.sh"
        harness.write_text(
            self.prefix
            + "\nruns_dir=\"$runs_dir_raw\"\n"
            + "assurance_handoff=/test-only/fake-handoff.py\n"
            + body,
            encoding="utf-8",
        )
        harness.chmod(0o700)
        environment = {
            **os.environ,
            "PATH": f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FLEET_RUNS_DIR": str(self.tmp / "runs"),
            "FLEET_BOOT_LOCK_READY_TIMEOUT_SECONDS": "1",
            "FLEET_HANDOFF_READY_TIMEOUT_SECONDS": ready_timeout,
            "FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS": commit_timeout,
            "HANDOFF_CHILD_PID_FILE": str(self.pid_file),
        }
        started = time.monotonic()
        try:
            result = subprocess.run(
                ["bash", str(harness), "timeout-test"],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                f"handoff timeout harness hung; stdout={exc.stdout!r}; "
                f"stderr={exc.stderr!r}"
            )
        self.assertLess(time.monotonic() - started, 8)
        return result

    def assert_child_reaped(self) -> None:
        pid = int(self.pid_file.read_text(encoding="utf-8").strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def run_boot_harness(
        self, fake_python: str, body: str
    ) -> subprocess.CompletedProcess[str]:
        python = self.fake_bin / "python3"
        python.write_text(fake_python, encoding="utf-8")
        python.chmod(0o700)
        harness = self.tmp / "boot-harness.sh"
        harness.write_text(
            self.boot_prefix
            + "\nruns_dir=\"$runs_dir_raw\"\n"
            + "clone_guard=/test-only/fake-clone-guard.py\n"
            + body,
            encoding="utf-8",
        )
        harness.chmod(0o700)
        environment = {
            **os.environ,
            "PATH": f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FLEET_RUNS_DIR": str(self.tmp / "boot-runs"),
            "FLEET_WORKTREES_DIR": str(self.tmp / "worktrees"),
            "FLEET_BOOT_LOCK_READY_TIMEOUT_SECONDS": "1",
            "FLEET_HANDOFF_READY_TIMEOUT_SECONDS": "1",
            "FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS": "1",
            "HANDOFF_CHILD_PID_FILE": str(self.pid_file),
        }
        try:
            return subprocess.run(
                ["bash", str(harness), "timeout-test"],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=8,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                f"boot-lock timeout harness hung; stdout={exc.stdout!r}; "
                f"stderr={exc.stderr!r}"
            )

    def test_ready_timeout_terminates_exact_child_and_returns_transient(self) -> None:
        result = self.run_harness(
            """#!/usr/bin/python3
import os
import signal
with open(os.environ["HANDOFF_CHILD_PID_FILE"], "w", encoding="utf-8") as stream:
    stream.write(str(os.getpid()))
signal.pause()
""",
            f"""
set +e
start_assurance_handoff_lock "{MISSION_ID}"
result=$?
set -e
printf '%s\n' "$result"
""",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "75")
        self.assert_child_reaped()

    def test_commit_timeout_terminates_exact_child_and_returns_transient(self) -> None:
        result = self.run_harness(
            """#!/usr/bin/python3
import os
import signal
import sys
with open(os.environ["HANDOFF_CHILD_PID_FILE"], "w", encoding="utf-8") as stream:
    stream.write(str(os.getpid()))
print("READY", flush=True)
sys.stdin.readline()
signal.pause()
""",
            f"""
set +e
start_assurance_handoff_lock "{MISSION_ID}"
start_result=$?
commit_result=99
if (( start_result == 0 )); then
  commit_assurance_handoff_lock
  commit_result=$?
fi
set -e
printf '%s %s\n' "$start_result" "$commit_result"
""",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0 75")
        self.assert_child_reaped()

    def test_ready_child_exit_fails_fast_without_waiting_for_deadline(self) -> None:
        started = time.monotonic()
        result = self.run_harness(
            """#!/usr/bin/python3
import os
with open(os.environ["HANDOFF_CHILD_PID_FILE"], "w", encoding="utf-8") as stream:
    stream.write(str(os.getpid()))
raise SystemExit(75)
""",
            f"""
set +e
start_assurance_handoff_lock "{MISSION_ID}"
result=$?
set -e
printf '%s\n' "$result"
""",
            ready_timeout="5",
        )

        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "75")
        self.assert_child_reaped()

    def test_commit_child_exit_fails_fast_without_waiting_for_deadline(self) -> None:
        started = time.monotonic()
        result = self.run_harness(
            """#!/usr/bin/python3
import os
import sys
with open(os.environ["HANDOFF_CHILD_PID_FILE"], "w", encoding="utf-8") as stream:
    stream.write(str(os.getpid()))
print("READY", flush=True)
sys.stdin.readline()
raise SystemExit(75)
""",
            f"""
set +e
start_assurance_handoff_lock "{MISSION_ID}"
start_result=$?
commit_result=99
if (( start_result == 0 )); then
  commit_assurance_handoff_lock
  commit_result=$?
fi
set -e
printf '%s %s\n' "$start_result" "$commit_result"
""",
            commit_timeout="5",
        )

        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0 75")
        self.assert_child_reaped()

    def test_ready_interruption_reaps_child_before_lock_is_held(self) -> None:
        python = self.fake_bin / "python3"
        python.write_text(
            """#!/usr/bin/python3
import os
import signal
with open(os.environ["HANDOFF_CHILD_PID_FILE"], "w", encoding="utf-8") as stream:
    stream.write(str(os.getpid()))
signal.pause()
""",
            encoding="utf-8",
        )
        python.chmod(0o700)
        harness = self.tmp / "interrupt-harness.sh"
        harness.write_text(
            self.prefix
            + "\nruns_dir=\"$runs_dir_raw\"\n"
            + "assurance_handoff=/test-only/fake-handoff.py\n"
            + "trap release_assurance_handoff_lock EXIT\n"
            + "trap 'exit 143' TERM INT\n"
            + f'start_assurance_handoff_lock "{MISSION_ID}"\n',
            encoding="utf-8",
        )
        harness.chmod(0o700)
        environment = {
            **os.environ,
            "PATH": f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FLEET_RUNS_DIR": str(self.tmp / "runs"),
            "FLEET_WORKTREES_DIR": str(self.tmp / "worktrees"),
            "FLEET_BOOT_LOCK_READY_TIMEOUT_SECONDS": "1",
            "FLEET_HANDOFF_READY_TIMEOUT_SECONDS": "5",
            "FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS": "1",
            "HANDOFF_CHILD_PID_FILE": str(self.pid_file),
        }
        process = subprocess.Popen(
            ["bash", str(harness), "timeout-test"],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        for _ in range(100):
            if self.pid_file.is_file() and self.pid_file.stat().st_size:
                break
            time.sleep(0.02)
        self.assertTrue(self.pid_file.is_file())
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            self.fail("interrupted READY owner did not clean up its process group")

        self.assertEqual(process.returncode, 143, stderr + stdout)
        self.assert_child_reaped()

    def test_boot_lock_ready_timeout_is_bounded_and_reaps_exact_child(self) -> None:
        result = self.run_boot_harness(
            """#!/usr/bin/python3
import os
import signal
with open(os.environ["HANDOFF_CHILD_PID_FILE"], "w", encoding="utf-8") as stream:
    stream.write(str(os.getpid()))
signal.pause()
""",
            """
set +e
acquire_fleet_boot_lock
result=$?
set -e
printf '%s\n' "$result"
""",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        self.assert_child_reaped()

    def test_invalid_timeout_overrides_fail_before_any_runtime_effect(self) -> None:
        for name, value in (
            ("FLEET_BOOT_LOCK_READY_TIMEOUT_SECONDS", "0"),
            ("FLEET_HANDOFF_READY_TIMEOUT_SECONDS", "0"),
            ("FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS", "3601"),
        ):
            with self.subTest(name=name):
                runs = self.tmp / f"never-created-{name}"
                environment = {
                    **os.environ,
                    "FLEET_RUNS_DIR": str(runs),
                    "FLEET_WORKTREES_DIR": str(self.tmp / "worktrees"),
                    "FLEET_BOOT_LOCK_READY_TIMEOUT_SECONDS": "1",
                    "FLEET_HANDOFF_READY_TIMEOUT_SECONDS": "1",
                    "FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS": "1",
                    name: value,
                }

                result = subprocess.run(
                    ["bash", str(FLEET_DOWN), "timeout-test", "--handoff-assurance"],
                    cwd=ROOT,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=5,
                    check=False,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn(f"Invalid {name}", result.stderr)
                self.assertFalse(runs.exists())


if __name__ == "__main__":
    unittest.main()
