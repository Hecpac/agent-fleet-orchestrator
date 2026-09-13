from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.test_fleet_herdr import FakeHerdr
import fleet_personal_pool as pools


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / "target"
        self.target.mkdir()
        self.fake = FakeHerdr()
        self.fake.assert_environment = lambda kwargs: None
        self.probe = lambda target: {"runtime_preflight": "PASS", "blockers": [],
                                    "binaries": {"herdr": {"path": "herdr"}}}
        self.pool = pools.Pool(self.root / "pool", run=self.fake, probe=self.probe)

    def test_prepare_is_idempotent_and_never_sends_a_prompt(self):
        result = self.pool.prepare(self.target, "mission-control-test")
        self.assertEqual(result["phase"], "ready")
        self.assertIsNone(result["mission_id"])
        self.assertEqual(len(result["observations"]), 4)
        calls = [self.fake.operation(c) for c in self.fake.calls]
        starts = [c for c in calls if c[1:3] == ["agent", "start"]]
        self.assertEqual(len(starts), 4)
        self.assertTrue(all(c[c.index("--sandbox") + 1] == "read-only" for c in starts))
        self.fake.calls.clear()
        self.pool.prepare(self.target, "mission-control-test")
        self.assertFalse(any(self.fake.operation(c)[1:3] in (["agent", "start"], ["agent", "prompt"],
                                                         ["workspace", "create"], ["pane", "split"])
                             for c in self.fake.calls))

    def test_workspace_identity_drift_prevents_close(self):
        self.pool.prepare(self.target, "mission-control-test")
        self.fake.workspace_label = "someone else's workspace"
        self.fake.calls.clear()
        with self.assertRaisesRegex(pools.PoolError, "ownership"):
            self.pool.close()
        self.assertFalse(any(self.fake.operation(c)[1:3] == ["workspace", "close"] for c in self.fake.calls))

    def test_ambiguous_workspace_creation_is_not_repeated(self):
        def ambiguous(argv):
            value = self.fake(argv)
            if self.fake.operation(argv)[1:3] == ["workspace", "create"]:
                raise OSError("transport lost after creation")
            return value
        self.pool.run = ambiguous
        with self.assertRaises(OSError):
            self.pool.prepare(self.target, "mission-control-test")
        self.pool.run = self.fake
        self.fake.calls.clear()
        with self.assertRaisesRegex(pools.PoolError, "indeterminate"):
            self.pool.prepare(self.target, "mission-control-test")
        self.assertEqual(self.fake.calls, [])

    def test_dialog_is_reported_without_answering_it(self):
        self.fake.screen = "Hooks need review\n› Ask Codex to do anything"
        result = self.pool.prepare(self.target, "mission-control-test")
        self.assertEqual(result["phase"], "blocked")
        self.assertTrue(all(not o["ready"] for o in result["observations"]))
        self.assertFalse(any(self.fake.operation(c)[1:3] == ["agent", "send-keys"] for c in self.fake.calls))

    def test_close_is_idempotent_and_refuses_active_agents(self):
        result = self.pool.prepare(self.target, "mission-control-test")
        name = result["members"][0]["name"]
        self.fake.agent_states[name] = "working"
        with self.assertRaisesRegex(pools.PoolError, "active"):
            self.pool.close()
        self.fake.agent_states[name] = "idle"
        self.assertEqual(self.pool.close()["phase"], "closed")
        self.fake.calls.clear()
        self.assertEqual(self.pool.close()["phase"], "closed")
        self.assertEqual(self.fake.calls, [])

    def test_assignment_recovery_preserves_request_and_mission_identity(self):
        self.pool.prepare(self.target, "mission-control-test")
        request = {"feature": "fixture", "objective": "bounded task"}
        calls = []
        def execute(saved, mid):
            calls.append(mid)
            self.assertFalse(self.fake.workspace_exists)
            return {"mission_id": "fixed-mission", "status": "running"}
        self.pool.assign(request, execute)
        self.pool.assign(request, execute)
        self.assertEqual(calls, [None, "fixed-mission"])
        with self.assertRaisesRegex(pools.PoolError, "different immutable"):
            self.pool.assign({"feature": "another"}, execute)

    def test_assignment_interruption_can_reconcile_after_pool_close(self):
        self.pool.prepare(self.target, "mission-control-test")
        request = {"feature": "fixture"}
        with self.assertRaises(OSError):
            self.pool.assign(request, mock.Mock(side_effect=OSError("lost response")))
        self.assertFalse(self.fake.workspace_exists)
        result = self.pool.assign(request, lambda saved, mid: {"mission_id": "same-idempotent-mission"})
        self.assertEqual(result["mission_id"], "same-idempotent-mission")


if __name__ == "__main__":
    unittest.main()
