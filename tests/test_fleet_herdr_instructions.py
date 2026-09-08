import contextlib
import io
from pathlib import Path
import unittest
from unittest import mock

from tests import test_fleet_herdr_mission as fixtures
from tests.test_mission_run import mission_run
import fleet_artifacts
import fleet_herdr_instructions as instructions
import fleet_mission
import fleet_mission_state as state


class HerdrInstructionTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.HerdrMissionTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def seed(self, files):
        for name, content in files.items():
            path = self.helper.target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        fixtures.git(self.helper.target, "add", ".")
        fixtures.git(self.helper.target, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "--no-gpg-sign", "-qm", "synthetic instruction fixture")
        self.helper.head = fixtures.git(self.helper.target, "rev-parse", "HEAD")
        self.helper.mid = self.helper.create(key="instructions")

    def test_cli_transfers_project_file_content_scope_and_cas_to_all_five_stages(self):
        original = (fixtures.ROOT / "AGENTS.md").read_text()
        self.seed({"AGENTS.md": original, "src/AGENTS.md": "Only this source scope.\n"})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = mission_run.main(["--runs-dir", str(self.helper.runs), "resume", "--mission-id", self.helper.mid, "--json"])
        self.assertEqual(code, 0, output.getvalue())
        tasks = list(fixtures.FakeBackend.tasks.values())
        self.assertEqual([task["stage"] for task in tasks], ["plan", "build", "review", "verify", "synthesis"])
        packet = tasks[0]["project_instructions"]
        self.assertTrue(all(task["project_instructions"] == packet for task in tasks))
        entries = packet["snapshot"]["entries"]
        self.assertEqual([(e["path"], e["scope"]) for e in entries], [("AGENTS.md", "."), ("src/AGENTS.md", "src")])
        self.assertEqual(entries[0]["content"], original)
        self.assertEqual(entries[0]["sha256"], state.artifact_id(original))
        self.assertEqual(fleet_artifacts.get_bytes(self.helper.runs, self.helper.mid, packet["snapshot_artifact_id"]),
                         state.canonical_bytes(packet["snapshot"]))
        self.assertEqual((self.helper.target / "AGENTS.md").read_text(), original)

    def test_worker_instruction_edit_and_controller_recovery_keep_initial_snapshot(self):
        self.seed({"AGENTS.md": "Initial target instruction.\n"})
        original_submit = fixtures.FakeBackend.submit
        def submit(backend, run_id, prompt, **kwargs):
            try:
                return original_submit(backend, run_id, prompt, **kwargs)
            finally:
                if kwargs["instance_id"] == "worker":
                    (backend.repo / "AGENTS.md").write_text("Edited deliverable, not new controller authority.\n")
        fixtures.FakeBackend.crash_stage = "build"
        with mock.patch.object(fixtures.FakeBackend, "submit", submit):
            with self.assertRaises(RuntimeError):
                self.helper.run_driver()
        packet = next(iter(fixtures.FakeBackend.tasks.values()))["project_instructions"]
        fixtures.FakeBackend.crash_stage = None
        self.assertEqual(self.helper.run_driver()["status"], "succeeded")
        self.assertEqual(len(self.helper.submitted()), 5)
        self.assertTrue(all(task["project_instructions"] == packet for task in fixtures.FakeBackend.tasks.values()))
        self.assertEqual(packet["snapshot"]["entries"][0]["content"], "Initial target instruction.\n")

    def test_override_scope_and_no_parent_or_untracked_discovery(self):
        (self.helper.tmp / "AGENTS.md").write_text("Foreign parent must not enter packet.\n")
        self.seed({"AGENTS.md": "Shadowed root.\n", "AGENTS.override.md": "Selected root.\n", "x/AGENTS.md": "Nested.\n"})
        (self.helper.target / "untracked").mkdir()
        (self.helper.target / "untracked/AGENTS.md").write_text("Not the baseline.\n")
        value = instructions.snapshot(self.helper.target, self.helper.head)
        self.assertEqual([e["path"] for e in value["entries"]], ["AGENTS.override.md", "x/AGENTS.md"])
        self.assertEqual(value["entries"][0]["content"], "Selected root.\n")

    def test_linked_or_oversized_instructions_fail_before_role_boot(self):
        self.seed({"AGENTS.md": "x" * (instructions.MAX_FILE_BYTES + 1)})
        with self.assertRaisesRegex(ValueError, "byte limit"):
            self.helper.run_driver()
        self.assertFalse(fixtures.FakeBackend.calls)
        path = self.helper.target / "AGENTS.md"
        path.unlink()
        path.symlink_to(self.helper.target / "README.md")
        fixtures.git(self.helper.target, "add", ".")
        fixtures.git(self.helper.target, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "--no-gpg-sign", "-qm", "synthetic link")
        with self.assertRaisesRegex(ValueError, "regular tracked"):
            instructions.snapshot(self.helper.target, fixtures.git(self.helper.target, "rev-parse", "HEAD"))
