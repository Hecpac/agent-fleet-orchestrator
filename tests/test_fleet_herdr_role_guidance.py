"""Provider-free guidance transfer, with reused local Git history and no commits."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests import test_fleet_herdr_runtime as runtime_fixtures
from tests import test_fleet_herdr_mission as fixtures
import fleet_artifacts
import fleet_herdr_role_guidance as guidance
import fleet_herdr_mission as driver
import fleet_mission_state as state


class BundleTests(unittest.TestCase):
    def test_explicit_roles_skills_and_unique_writer(self):
        bundle = guidance.validate_bundle(guidance.build_bundle())
        self.assertEqual(set(bundle["roles"]), {"lead", "research", "worker", "reviewer", "verifier"})
        self.assertEqual([r for r,c in bundle["roles"].items() if c["candidate_writer"]], ["worker"])
        for role in bundle["roles"]:
            packet = guidance.project(bundle, "a"*64, role)
            self.assertEqual({s["name"] for s in packet["skills"]}, set(bundle["roles"][role]["skills"]))
            for skill in packet["skills"]:
                self.assertEqual(state.artifact_id(skill["content"]), skill["sha256"])
        self.assertIn("without waiting", bundle["common"]["autonomy"][0])
        self.assertIn("raw JSON", bundle["common"]["instruction_scopes"][-1])

    def test_missing_aliased_and_oversized_skill_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(guidance, "SKILL_ROOT", root):
                with self.assertRaises(FileNotFoundError):
                    guidance.build_bundle()
                first = root / "codex-os"
                first.mkdir()
                target = root / "foreign"
                target.write_text("not a skill")
                (first / "SKILL.md").symlink_to(target)
                with self.assertRaisesRegex(ValueError, "aliases"):
                    guidance.build_bundle()
                (first / "SKILL.md").unlink()
                (first / "SKILL.md").write_text("x" * (32*1024+1))
                with self.assertRaisesRegex(ValueError, "size limit"):
                    guidance.build_bundle()

    def test_modified_content_writer_and_unknown_role_fail_closed(self):
        bundle = guidance.build_bundle()
        for mutation in ("content", "writer", "missing", "common", "empty_selection"):
            value = copy.deepcopy(bundle)
            if mutation == "content": value["skills"]["codex-os"]["content"] += "forged"
            if mutation == "writer": value["roles"]["research"]["candidate_writer"] = True
            if mutation == "missing": del value["skills"]["codex-os"]
            if mutation == "common": value["common"] = {}
            if mutation == "empty_selection": value["roles"]["worker"]["skills"] = {}
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                guidance.validate_bundle(value)
        with self.assertRaisesRegex(ValueError, "unknown"):
            guidance.packet(None, None, {}, "unregistered")


class GuidanceDeliveryTests(unittest.TestCase):
    setUp = runtime_fixtures.RuntimeMissionTests.setUp
    create = fixtures.HerdrMissionTests.create

    def test_five_tasks_receive_exact_role_content_without_global_inheritance(self):
        self.assertEqual(driver.drive(self.runs, self.mid)["status"], "succeeded")
        tasks = list(fixtures.FakeBackend.tasks.values())
        self.assertEqual([t["stage"] for t in tasks], ["plan", "build", "review", "verify", "synthesis"])
        pins = {t["role_guidance"]["bundle_artifact_id"] for t in tasks}
        self.assertEqual(len(pins), 1)
        bundle = state.loads_strict(fleet_artifacts.get_bytes(self.runs, self.mid, pins.pop()))
        for task in tasks:
            packet = task["role_guidance"]
            self.assertEqual(packet, guidance.project(bundle, packet["bundle_artifact_id"], task["instance_id"]))
            self.assertEqual(packet["contract"]["candidate_writer"], task["writer"])
            self.assertNotIn("/Users/hector", state.canonical_bytes(packet).decode())
        self.assertIn("research", bundle["roles"])
        self.assertNotIn("research", {t["instance_id"] for t in tasks})

    def test_recovery_reuses_pinned_bundle_when_live_sources_unavailable(self):
        fixtures.FakeBackend.missing_stage = "build"
        driver.drive(self.runs, self.mid)
        first = next(iter(fixtures.FakeBackend.tasks.values()))["role_guidance"]
        fixtures.FakeBackend.missing_stage = None
        with mock.patch.object(guidance, "build_bundle", side_effect=AssertionError("must use frozen bundle")):
            self.assertEqual(driver.drive(self.runs, self.mid)["status"], "succeeded")
        self.assertTrue(all(t["role_guidance"]["bundle_artifact_id"] == first["bundle_artifact_id"]
                            for t in fixtures.FakeBackend.tasks.values()))

    def test_historical_task_is_not_silently_upgraded_and_projection_tamper_fails(self):
        pin = fleet_artifacts.put_bytes(self.runs, self.mid, state.canonical_bytes({"stage": "plan"}))["artifact_id"]
        current = {"admissions": {"first": {"request_key": "herdr:plan", "task_sha256": pin}}}
        self.assertIsNone(guidance.packet(self.runs, self.mid, current, "worker"))
        packet = guidance.packet(self.runs, self.mid, {"admissions": {}}, "lead")
        packet["contract"]["purpose"] = "forged"
        pin = fleet_artifacts.put_bytes(self.runs, self.mid, state.canonical_bytes({"role_guidance": packet}))["artifact_id"]
        current["admissions"]["first"]["task_sha256"] = pin
        with self.assertRaisesRegex(ValueError, "first task binding"):
            guidance.packet(self.runs, self.mid, current, "worker")
