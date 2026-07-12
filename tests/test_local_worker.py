from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "orchestration" / "agents" / "local_worker.py"
SPEC = importlib.util.spec_from_file_location("local_worker", MODULE_PATH)
assert SPEC and SPEC.loader
local_worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local_worker)


class LocalWorkerContractTests(unittest.TestCase):
    def run_main(self, response: str) -> int:
        argv = [
            "local_worker.py", "--role", "triage", "--model", "fake",
            "--instruction", "test", "--prompt", "test",
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            local_worker, "call_ollama", return_value=response
        ):
            return local_worker.main()

    def test_done_returns_zero(self) -> None:
        self.assertEqual(self.run_main("STATUS: DONE\nSUMMARY:\nEVIDENCE:\nRISKS:\nNEXT_ACTION:"), 0)

    def test_blocked_returns_three(self) -> None:
        self.assertEqual(self.run_main("STATUS: BLOCKED\nSUMMARY:\nEVIDENCE:\nRISKS:\nNEXT_ACTION:"), 3)

    def test_failed_or_missing_contract_returns_nonzero(self) -> None:
        self.assertEqual(
            self.run_main("STATUS: FAILED\nSUMMARY:\nEVIDENCE:\nRISKS:\nNEXT_ACTION:"), 1
        )
        self.assertEqual(self.run_main("looks good"), 1)
        self.assertEqual(
            self.run_main(
                "STATUS: DONE\nSTATUS: FAILED\nSUMMARY:\nEVIDENCE:\nRISKS:\nNEXT_ACTION:"
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
