import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleet_control_runtime as runtime


class LinuxObservationTests(unittest.TestCase):
    """Exercise the pinned /proc reader against a synthetic /proc tree."""

    pid = 4242

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fake-proc-")
        self.addCleanup(temporary.cleanup)
        self.proc = Path(temporary.name)
        (self.proc / "sys/kernel/random").mkdir(parents=True)
        (self.proc / "sys/kernel/random/boot_id").write_text("boot-a\n", encoding="ascii")
        patcher = mock.patch.object(runtime, "_PROC_ROOT", self.proc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_task(self, start_ticks, state="S"):
        task = self.proc / str(self.pid)
        task.mkdir(exist_ok=True)
        fields = " ".join([state, *(["1"] * 18), str(start_ticks), "0", "0"])
        (task / "stat").write_text(f"{self.pid} (py thon) {fields}\n", encoding="ascii")
        uid = os.geteuid()
        (task / "status").write_text(f"Name:\tpython\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n", encoding="ascii")

    def expected(self, start_ticks):
        return {"kind": "linux-proc-stat-v1", "uid": os.geteuid(), "boot_id": "boot-a", "start_ticks": start_ticks}

    def released_once(self, between):
        real = runtime._read_pinned_proc_entry
        calls = []

        def read(directory, name):
            if not calls:
                calls.append(name)
                between()
                raise runtime._ReleasedLinuxTask
            return real(directory, name)

        return mock.patch.object(runtime, "_read_pinned_proc_entry", side_effect=read)

    def test_missing_entry_is_absent(self):
        self.assertEqual(runtime._linux_process_observation(self.pid), (None, False))

    def test_live_and_zombie_tasks_report_exact_identity(self):
        self.write_task(77)
        self.assertEqual(runtime._linux_process_observation(self.pid), (self.expected(77), False))
        self.write_task(77, state="Z")
        self.assertEqual(runtime._linux_process_observation(self.pid), (self.expected(77), True))

    def test_task_released_during_read_and_reaped_is_absent(self):
        self.write_task(77)
        with self.released_once(lambda: shutil.rmtree(self.proc / str(self.pid))):
            self.assertEqual(runtime._linux_process_observation(self.pid), (None, False))

    def test_task_released_during_read_and_pid_reused_reports_new_identity(self):
        self.write_task(77)
        with self.released_once(lambda: self.write_task(91)):
            self.assertEqual(runtime._linux_process_observation(self.pid), (self.expected(91), False))

    def test_repeated_release_is_an_inspection_failure_never_absence(self):
        self.write_task(77)
        with mock.patch.object(runtime, "_read_pinned_proc_entry", side_effect=runtime._ReleasedLinuxTask):
            with self.assertRaisesRegex(runtime.RuntimeIdentityError, "cannot read exact Linux process identity"):
                runtime._linux_process_observation(self.pid)

    def test_esrch_and_vanished_entries_map_to_a_released_task(self):
        self.write_task(77)
        directory = os.open(self.proc / str(self.pid), os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, directory)
        with mock.patch.object(runtime.os, "read", side_effect=ProcessLookupError):
            with self.assertRaises(runtime._ReleasedLinuxTask):
                runtime._read_pinned_proc_entry(directory, "stat")
        with self.assertRaises(runtime._ReleasedLinuxTask):
            runtime._read_pinned_proc_entry(directory, "missing")

    def test_other_read_failures_are_not_absence(self):
        self.write_task(77)
        with mock.patch.object(runtime.os, "open", side_effect=PermissionError):
            with self.assertRaisesRegex(runtime.RuntimeIdentityError, "cannot read exact Linux process identity"):
                runtime._linux_process_observation(self.pid)
        (self.proc / str(self.pid) / "status").write_bytes(b"Uid:\t\xff\n")
        with self.assertRaisesRegex(runtime.RuntimeIdentityError, "cannot read exact Linux process identity"):
            runtime._linux_process_observation(self.pid)
        (self.proc / str(self.pid) / "status").write_text("Name:\tpython\n", encoding="ascii")
        with self.assertRaisesRegex(runtime.RuntimeIdentityError, "invalid Linux process status record"):
            runtime._linux_process_observation(self.pid)


@unittest.skipUnless(sys.platform.startswith("linux"), "real /proc required")
class RealLinuxProcTests(unittest.TestCase):
    def test_current_process_matches_the_public_observation(self):
        identity, zombie = runtime.process_observation(os.getpid())
        self.assertEqual(identity["kind"], "linux-proc-stat-v1")
        self.assertEqual(identity["uid"], os.geteuid())
        self.assertFalse(zombie)


if __name__ == "__main__":
    unittest.main()
