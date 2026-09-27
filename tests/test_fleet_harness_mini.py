import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_harness_mini as mini
import fleet_json


def response(command=mini.SENTINEL, *, identifier="call-1", content="{}"):
    return {"model": "deepseek-flash", "choices": [{"message": {"role": "assistant", "content": content,
        "tool_calls": [{"id": identifier, "type": "function", "function": {"name": "bash",
            "arguments": fleet_json.canonical_bytes({"command": command}).decode()}}]}}]}


class MiniTests(unittest.TestCase):
    def test_entire_batch_validated_before_effects(self):
        good = response("pwd")
        actions = mini.validate_batch(good, set())
        self.assertEqual(actions[0]["command"], "pwd")
        with self.assertRaises(ValueError): mini.validate_batch(good, {"call-1"})
        mixed = response()
        mixed["choices"][0]["message"]["tool_calls"] += response("touch forbidden", identifier="call-2")["choices"][0]["message"]["tool_calls"]
        with self.assertRaises(ValueError): mini.validate_batch(mixed, set())


@unittest.skipUnless(os.environ.get("FLEET_HARNESS_LOCAL_TESTS") == "1", "explicit local Docker test lane")
class NativeMiniTests(unittest.TestCase):
    def test_installed_native_mini_full_prompt_tools_and_terminal(self):
        with tempfile.TemporaryDirectory(prefix="fleet-mini-control-") as temporary:
            root = Path(temporary)
            manifest = mini.freeze_dependencies(mini.DIST, root / "deps")
            self.assertIn("minisweagent/agents/default.py", manifest)
            control = mini.MiniControl(root / "control", owner=str(uuid.uuid4()), dependencies=root / "deps")
            try:
                task = {"objective": "synthetic native protocol conformance; no provider", "revision": "test"}
                trajectory = control.start(task)
                self.assertEqual(trajectory["info"]["mini_version"], "2.4.6")
                self.assertEqual(trajectory["messages"][1]["content"], fleet_json.canonical_bytes(task).decode())
                control.query(response("pwd"))
                control.observe([{"tool_call_id": "call-1", "output": "/candidate\n", "returncode": 0}])
                final = '{"type":"submit_candidate","summary":"synthetic","paths":[],"checks":[]}'
                control.query(response(identifier="call-2", content=final))
                for invalid in (False, 0.0):
                    with self.assertRaises(ValueError):
                        control.observe([{"tool_call_id": "call-2", "output": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", "returncode": invalid}])
                control.observe([{"tool_call_id": "call-2", "output": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", "returncode": 0}])
                control.trajectory["messages"][-2]["content"] = "FORGED AFTER RETENTION"
                recovered = control.terminal()
                self.assertEqual(recovered["final"], final)
                self.assertEqual(recovered["trajectory"]["info"]["submission"], "")
                with self.assertRaises(ValueError): control.query(response(identifier="call-3"))
            finally:
                cleaned = control.cleanup()
                if cleaned is not None: self.assertTrue(cleaned["resources_clean"])


if __name__ == "__main__": unittest.main()
