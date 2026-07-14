from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
import uuid


PRE_HOOK = Path("/Users/hector/.claude/hooks/pre_tool.sh")
POST_HOOK = Path("/Users/hector/.claude/hooks/post_tool.sh")
GENESIS = "0" * 64


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class ClaudeAuditHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit_temp = tempfile.TemporaryDirectory(prefix="fleet-audit-test-")
        self.audit_dir = Path(self.audit_temp.name)
        self.audit_dir.chmod(0o700)
        self.ledger = self.audit_dir / "a2a_ledger.jsonl"

    def tearDown(self) -> None:
        self.audit_temp.cleanup()

    def run_hook(self, path: Path, payload: dict[str, object]) -> None:
        env = os.environ.copy()
        env["FLEET_HUMAN_UID"] = "controller-test"
        env["USER"] = "fleet_worker"
        env["LLM_MODEL"] = "claude-audit-test"
        env["FLEET_AUDIT_TEST_MODE"] = "1"
        env["FLEET_AUDIT_TEST_DIR"] = str(self.audit_dir)
        env["PATH"] = "/usr/bin:/bin"
        result = subprocess.run(
            ["bash", str(path)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def load_events(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.ledger.read_text().splitlines() if line]

    def assert_chain_valid(self, events: list[dict[str, object]]) -> None:
        previous = GENESIS
        for event in events:
            self.assertEqual(event["previous_event_sha256"], previous)
            stored = str(event["event_sha256"])
            unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
            self.assertEqual(hashlib.sha256(canonical(unsigned)).hexdigest(), stored)
            previous = stored

    def test_external_hash_chained_ledger_correlates_approval(self) -> None:
        tool_use_id = f"toolu_{uuid.uuid4().hex}"
        secret_marker = f"raw-sensitive-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as workspace:
            common = {
                "tool_name": "Bash",
                "tool_use_id": tool_use_id,
                "session_id": "audit-test-session",
                "cwd": workspace,
                "tool_input": {"command": f"printf {secret_marker}"},
            }
            self.run_hook(PRE_HOOK, {**common, "hook_event_name": "PreToolUse"})
            permission = {**common, "hook_event_name": "PermissionRequest"}
            permission.pop("tool_use_id")
            self.run_hook(PRE_HOOK, permission)
            self.run_hook(
                POST_HOOK,
                {
                    **common,
                    "hook_event_name": "PostToolUse",
                    "tool_response": {"exit_code": 0, "output": secret_marker},
                },
            )
            self.assertFalse(Path(workspace, ".run_audit.jsonl").exists())

        events = self.load_events()
        self.assert_chain_valid(events)
        pre_event = next(
            event
            for event in reversed(events)
            if event.get("tool_use_id") == tool_use_id
            and event.get("event_type") == "PreToolUse"
        )
        correlated = [
            event
            for event in events
            if event.get("correlation_sha256") == pre_event["correlation_sha256"]
        ]
        self.assertEqual(
            [event["event_type"] for event in correlated],
            ["PreToolUse", "PermissionRequest", "PostToolUse"],
        )
        approval = correlated[1]
        post = correlated[2]
        self.assertEqual(approval["approval_status"], "requested")
        self.assertEqual(approval["tool_use_id"], "")
        self.assertEqual(post["correlation_sha256"], approval["correlation_sha256"])
        self.assertEqual(post["approval_request_reference"], approval["event_id"])
        self.assertIsNone(post["human_approval_reference"])
        self.assertEqual(correlated[0]["args_sha256"], post["args_sha256"])
        self.assertEqual(post["exit_code"], 0)
        self.assertEqual(post["model_version"], "claude-audit-test")
        self.assertEqual(post["human_uid"], "controller-test")
        self.assertEqual(post["agent_uid"], "fleet_worker")
        self.assertEqual(post["data_classification"], "restricted")
        self.assertNotIn(secret_marker, self.ledger.read_text())
        self.assertEqual(stat.S_IMODE(self.audit_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.ledger.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
