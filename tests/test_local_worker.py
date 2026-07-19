from __future__ import annotations

import importlib.util
import json
import os
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
            "local_worker.py",
            "--role",
            "triage",
            "--model",
            "fake",
            "--instruction",
            "test",
            "--prompt",
            "test",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(
                local_worker,
                "call_ollama",
                return_value={"response": response, "model": "fake"},
            ),
        ):
            return local_worker.main()

    def test_done_returns_zero(self) -> None:
        self.assertEqual(
            self.run_main("STATUS: DONE\nSUMMARY:\nEVIDENCE:\nRISKS:\nNEXT_ACTION:"), 0
        )

    def test_blocked_returns_three(self) -> None:
        self.assertEqual(
            self.run_main("STATUS: BLOCKED\nSUMMARY:\nEVIDENCE:\nRISKS:\nNEXT_ACTION:"),
            3,
        )

    def test_failed_or_missing_contract_returns_nonzero(self) -> None:
        self.assertEqual(
            self.run_main("STATUS: FAILED\nSUMMARY:\nEVIDENCE:\nRISKS:\nNEXT_ACTION:"),
            1,
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
                "local_worker.py",
                "--role",
                "triage",
                "--model",
                "fake",
                "--instruction",
                "test",
                "--prompt",
                "test",
                "--usage-file",
                str(usage_path),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    local_worker,
                    "call_ollama",
                    return_value={
                        "response": response,
                        "model": "fake",
                        "done": True,
                        "prompt_eval_count": 42,
                        "eval_count": 7,
                    },
                ),
            ):
                self.assertEqual(local_worker.main(), 0)
            usage = json.loads(usage_path.read_text())
            self.assertEqual(usage, {"prompt_eval_count": 42, "eval_count": 7})

    def test_missing_or_partial_usage_is_unknown_never_zero(self) -> None:
        response = "STATUS: DONE\nSUMMARY:\nEVIDENCE:\nRISKS:\nNEXT_ACTION:"
        bodies = (
            {"response": response, "model": "fake", "done": True},
            {
                "response": response,
                "model": "fake",
                "done": True,
                "prompt_eval_count": 42,
            },
            {
                "response": response,
                "model": "fake",
                "done": True,
                "prompt_eval_count": True,
                "eval_count": 7,
            },
            {
                "response": response,
                "model": "fake",
                "done": True,
                "prompt_eval_count": -1,
                "eval_count": 7,
            },
            {
                "response": response,
                "model": "fake",
                "done": False,
                "prompt_eval_count": 42,
                "eval_count": 7,
            },
            {
                "response": response,
                "model": "fake",
                "done": 1,
                "prompt_eval_count": 42,
                "eval_count": 7,
            },
            {
                "response": response,
                "model": "fake",
                "prompt_eval_count": 42,
                "eval_count": 7,
            },
        )
        for index, body in enumerate(bodies):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp:
                usage_path = Path(tmp) / "usage.json"
                argv = [
                    "local_worker.py",
                    "--role",
                    "triage",
                    "--model",
                    "fake",
                    "--instruction",
                    "test",
                    "--prompt",
                    "test",
                    "--usage-file",
                    str(usage_path),
                ]
                with (
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(local_worker, "call_ollama", return_value=body),
                ):
                    self.assertEqual(local_worker.main(), 0)
                self.assertEqual(
                    json.loads(usage_path.read_text()),
                    {"prompt_eval_count": None, "eval_count": None},
                )

    def test_usage_is_unknown_before_inference_and_survives_call_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "usage.json"
            argv = [
                "local_worker.py",
                "--role",
                "triage",
                "--model",
                "fake",
                "--instruction",
                "test",
                "--prompt",
                "test",
                "--usage-file",
                str(usage_path),
            ]

            def fail_after_preflight(*_args: object) -> dict:
                self.assertEqual(
                    json.loads(usage_path.read_text()),
                    {"prompt_eval_count": None, "eval_count": None},
                )
                raise SystemExit("inference failed")

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    local_worker, "call_ollama", side_effect=fail_after_preflight
                ),
                self.assertRaisesRegex(SystemExit, "inference failed"),
            ):
                local_worker.main()
            self.assertEqual(
                json.loads(usage_path.read_text()),
                {"prompt_eval_count": None, "eval_count": None},
            )
            self.assertEqual(usage_path.stat().st_mode & 0o777, 0o600)

    def test_usage_leaf_conflicts_fail_before_inference_without_clobbering(self) -> None:
        for kind in ("regular", "symlink", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                usage_path = root / "usage.json"
                victim = root / "victim.txt"
                victim.write_text("preserve me")
                victim.chmod(0o600)
                if kind == "regular":
                    usage_path = victim
                elif kind == "symlink":
                    usage_path.symlink_to(victim)
                else:
                    os.link(victim, usage_path)
                argv = [
                    "local_worker.py",
                    "--role",
                    "triage",
                    "--model",
                    "fake",
                    "--instruction",
                    "test",
                    "--prompt",
                    "test",
                    "--usage-file",
                    str(usage_path),
                ]
                ollama = mock.Mock(return_value={})
                with (
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(local_worker, "call_ollama", ollama),
                    self.assertRaises(local_worker.fleet_safe_paths.SafePathError),
                ):
                    local_worker.main()
                ollama.assert_not_called()
                self.assertEqual(victim.read_text(), "preserve me")

    def test_observed_usage_survives_later_identity_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "usage.json"
            argv = [
                "local_worker.py",
                "--role",
                "triage",
                "--model",
                "frozen-model",
                "--instruction",
                "test",
                "--prompt",
                "test",
                "--usage-file",
                str(usage_path),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    local_worker,
                    "call_ollama",
                    return_value={
                        "response": "STATUS: DONE\nSUMMARY:\nEVIDENCE:\nRISKS:\nNEXT_ACTION:",
                        "model": "different-model",
                        "done": True,
                        "prompt_eval_count": 42,
                        "eval_count": 7,
                    },
                ),
                self.assertRaises(local_worker.fleet_providers.ProviderIdentityError),
            ):
                local_worker.main()
            self.assertEqual(
                json.loads(usage_path.read_text()),
                {"prompt_eval_count": 42, "eval_count": 7},
            )

    def test_response_model_must_match_frozen_dispatch_identity(self) -> None:
        argv = [
            "local_worker.py",
            "--role",
            "triage",
            "--model",
            "frozen-model",
            "--instruction",
            "test",
            "--prompt",
            "test",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(
                local_worker,
                "call_ollama",
                return_value={
                    "response": "STATUS: DONE\nSUMMARY:\nEVIDENCE:\nRISKS:\nNEXT_ACTION:",
                    "model": "different-model",
                },
            ),
            self.assertRaises(local_worker.fleet_providers.ProviderIdentityError),
        ):
            local_worker.main()

        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(
                local_worker,
                "call_ollama",
                return_value={
                    "response": "STATUS: DONE\nSUMMARY:\nEVIDENCE:\nRISKS:\nNEXT_ACTION:"
                },
            ),
            self.assertRaises(local_worker.fleet_providers.ProviderIdentityError),
        ):
            local_worker.main()

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

    def test_ollama_response_uses_strict_fleet_json(self) -> None:
        invalid_bodies = (
            b'{"response":"first","response":"second"}',
            b'{"response":"ok","count":NaN}',
            b'{"response":"ok","count":1e999}',
            b'{"response":"\\ud800"}',
            b'[1,2,3]',
        )
        for raw in invalid_bodies:
            with self.subTest(raw=raw):
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = raw
                with (
                    mock.patch.object(
                        local_worker.urllib.request,
                        "urlopen",
                        return_value=response,
                    ),
                    self.assertRaisesRegex(
                        SystemExit, "Invalid Ollama JSON response"
                    ),
                ):
                    local_worker.call_ollama(
                        "http://localhost:11434", "fake", "prompt", 0.2, 768
                    )


if __name__ == "__main__":
    unittest.main()
