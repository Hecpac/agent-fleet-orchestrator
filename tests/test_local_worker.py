from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
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
            local_worker, "call_ollama", return_value={"response": response}
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

    def test_usage_file_records_token_counts(self) -> None:
        response = "STATUS: DONE\nSUMMARY:\nEVIDENCE:\nRISKS:\nNEXT_ACTION:"
        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "usage.json"
            argv = [
                "local_worker.py", "--role", "triage", "--model", "fake",
                "--instruction", "test", "--prompt", "test",
                "--usage-file", str(usage_path),
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                local_worker,
                "call_ollama",
                return_value={
                    "response": response,
                    "prompt_eval_count": 42,
                    "eval_count": 7,
                },
            ):
                self.assertEqual(local_worker.main(), 0)
            usage = json.loads(usage_path.read_text())
            self.assertEqual(usage, {"prompt_eval_count": 42, "eval_count": 7})

    def test_ollama_request_disables_hidden_thinking(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"response": "STATUS: DONE"}
        ).encode("utf-8")
        with mock.patch.object(
            local_worker.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            body = local_worker.call_ollama(
                "http://localhost:11434", "fake", "prompt", 0.2, 768
            )
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertFalse(payload["think"])
        self.assertEqual(body["response"], "STATUS: DONE")


if __name__ == "__main__":
    unittest.main()
