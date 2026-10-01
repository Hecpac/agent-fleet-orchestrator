"""Codex 0.159 Folder access acknowledgment in the Herdr backend; no provider."""
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_herdr
import fleet_herdr_startup as startup
import fleet_herdr_versions as versions
from tests import test_fleet_herdr as backend_tests

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex-0.159.3"


class FolderAccessAcknowledgmentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = backend_tests.HerdrBackendTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fake = self.fixture.fake
        cwd = str(self.fixture.target)
        self.notice = (FIXTURES / "folder-access.txt").read_text().replace("/fleet/candidate", cwd)
        self.ready = (FIXTURES / "ready.txt").read_text().replace("/fleet/candidate", cwd)
        self.acknowledged = set()
        self.notice_persists = False

    def runner(self, command, **kwargs):
        operation = self.fake.operation(command)
        if operation[1:3] == ["agent", "read"]:
            name = operation[3]
            if self.notice_persists or name not in self.acknowledged:
                return fleet_herdr.subprocess.CompletedProcess(command, 0, self.notice, "")
            return fleet_herdr.subprocess.CompletedProcess(command, 0, self.ready, "")
        if operation[1:3] == ["agent", "send-keys"] and operation[4:] == ["enter"]:
            self.acknowledged.add(operation[3])
            self.fake.calls.append(command)
            return fleet_herdr.subprocess.CompletedProcess(command, 0, '{"result":{"sent":true}}', "")
        return self.fake(command, **kwargs)

    def operations(self):
        return [self.fake.operation(call) for call in self.fake.calls]

    def test_each_member_acknowledges_once_before_any_prompt(self):
        backend = self.fixture.backend(runner=self.runner)
        state = backend.boot()
        self.assertEqual(state["runtime_contract"], versions.CURRENT_CONTRACT)
        keys = [op for op in self.operations() if op[1:3] == ["agent", "send-keys"]]
        self.assertEqual(sorted(op[3] for op in keys), sorted(m["agent_name"] for m in state["members"]))
        self.assertTrue(all(op[4:] == ["enter"] for op in keys))
        self.assertFalse(any(op[1:3] == ["agent", "prompt"] for op in self.operations()))
        for member in state["members"]:
            (record,) = member["startup_acknowledgments"]
            self.assertEqual((record["guard"], record["notice"], record["key"]),
                             (startup.GUARD_0159, "folder-access-open-restricted", "enter"))
            self.assertNotEqual(record["screen_before_sha256"], record["screen_after_sha256"])
        # A reloaded controller keeps the durable records and presses nothing new.
        self.fake.calls.clear()
        reloaded = self.fixture.backend(runner=self.runner)
        self.assertEqual(reloaded.boot()["members"], state["members"])
        self.assertFalse(any(op[1:3] == ["agent", "send-keys"] for op in self.operations()))

    def test_notice_that_persists_blocks_and_never_presses_twice(self):
        self.notice_persists = True
        backend = self.fixture.backend(runner=self.runner)
        with mock.patch.object(fleet_herdr, "STARTUP_ACK_SETTLE_SECONDS", 0.3):
            with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "blocked before prompt: Codex folder access notice"):
                backend.boot()
        presses = [op for op in self.operations() if op[1:3] == ["agent", "send-keys"]]
        self.assertEqual(len(presses), 1)
        state = backend.state()
        acknowledged = [m for m in state["members"] if m.get("startup_acknowledgments")]
        self.assertEqual(len(acknowledged), 1)
        self.assertIsNotNone(acknowledged[0]["startup_acknowledgments"][0]["screen_after_sha256"])
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "reappeared after its acknowledgment"):
            self.fixture.backend(runner=self.runner).boot()
        self.assertEqual(len([op for op in self.operations() if op[1:3] == ["agent", "send-keys"]]), 1)

    def test_contracts_before_0159_never_answer_a_dialog(self):
        self.fake.codex_version = "0.154.0"
        backend = self.fixture.backend(runner=self.runner)
        backend.initial_runtime_contract = dict(versions.PERSONAL_CONTRACT)
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "Codex input prompt was not observed"):
            backend.boot()
        self.assertFalse(any(op[1:3] == ["agent", "send-keys"] for op in self.operations()))

    def test_durable_acknowledgment_shape_is_validated(self):
        backend = self.fixture.backend(runner=self.runner)
        state = backend.boot()
        state["members"][0]["startup_acknowledgments"][0]["key"] = "y"
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "startup acknowledgment is invalid"):
            backend._validate_state(state)


if __name__ == "__main__":
    unittest.main()
