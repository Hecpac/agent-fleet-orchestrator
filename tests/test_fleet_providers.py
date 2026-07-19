from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_frontier  # noqa: E402
import fleet_providers  # noqa: E402
import router_config  # noqa: E402


class FakeAdapter(fleet_providers.BaseAdapter):
    name = "fake"
    hook_source = "fake"
    session_file = "fake-sessions.json"
    fixed_provider = "fake-provider"


class FleetProviderTests(unittest.TestCase):
    def test_builtin_adapters_validate_every_repository_role(self) -> None:
        router = router_config.load_router(ROOT / "orchestration" / "router.yaml")
        seen: set[str] = set()
        for role in router["roles"].values():
            configured = fleet_providers.identity(
                role["provider"], role["model"], role.get("variant"),
                role.get("hook_source", ""),
            )
            adapter = fleet_providers.DEFAULT_REGISTRY.resolve(
                hook_source=configured.hook_source, provider=configured.provider
            )
            spec = adapter.validate_configuration(
                configured,
                command=role.get("command"),
                runner=role["runner"],
            )
            self.assertEqual(spec["adapter"], adapter.name)
            seen.add(adapter.name)
        self.assertEqual(seen, {"codex", "claude", "kimi", "opencode", "ollama"})

    def test_submission_transport_preserves_existing_prompt_contracts(self) -> None:
        run_id = "run-provider-contract"
        task = "first line\nsecond line with ñ"
        cases = (
            ("openai", "gpt-test", "codex", "pointer"),
            ("anthropic", "claude-test", "claude", "pointer"),
            ("moonshot-ai", "kimi-test", "kimi", "inline-json"),
            ("minimax", "MiniMax-M3", "opencode", "inline-json"),
        )
        for provider, model, source, transport in cases:
            with self.subTest(source=source):
                configured = fleet_providers.identity(provider, model, None, source)
                submission = fleet_providers.adapter_for(configured).prepare_submission(
                    configured, task, run_id, Path("/tmp/prompt.txt")
                )
                self.assertEqual(submission["transport"], transport)
                self.assertEqual(
                    submission["prompt"],
                    fleet_frontier.prompt_with_contract(task, run_id, hook_source=source),
                )
                self.assertIn(f"FLEET_RESULT:{run_id}:<STATUS>", submission["prompt"])
                if source in {"kimi", "opencode"}:
                    self.assertNotIn("\n", submission["payload"])
                    logical = json.loads(submission["payload"].rsplit("FDP_PROMPT=", 1)[1])
                    self.assertTrue(logical.startswith(task))
                else:
                    self.assertTrue(submission["payload"].startswith(f"FLEET_RUN {run_id}:"))

    def test_same_structured_sentinels_produce_same_terminals(self) -> None:
        run_id = "run-terminal-equivalence"
        for status, terminal in (
            ("DONE", "succeeded"), ("BLOCKED", "blocked"), ("FAILED", "failed")
        ):
            response = f"evidence\nFLEET_RESULT:{run_id}:{status}"
            cases = (
                (fleet_providers.identity("openai", "gpt", None, "codex"), (response, "openai", "gpt")),
                (fleet_providers.identity("anthropic", "claude", None, "claude"), (response, "anthropic", "claude")),
                (fleet_providers.identity("moonshot-ai", "kimi", None, "kimi"), (response, "moonshot-ai", "kimi")),
                (fleet_providers.identity("zai", "glm", None, "opencode"), (response, "zai", "glm", None)),
            )
            for configured, raw in cases:
                with self.subTest(status=status, source=configured.hook_source):
                    adapter = fleet_providers.adapter_for(configured)
                    evidence = adapter.extract_final_response(
                        configured, f"{configured.hook_source}-session", run_id,
                        "2026-07-14T00:00:00Z",
                        {adapter.name: lambda *_args, value=raw: value},
                    )
                    adapter.verify_identity(configured, evidence)
                    self.assertEqual(
                        fleet_frontier.structured_sentinel_status(evidence.response, run_id)[0],
                        terminal,
                    )

    def test_fake_adapter_exercises_the_full_contract_deterministically(self) -> None:
        adapter = FakeAdapter()
        registry = fleet_providers.ProviderRegistry([adapter])
        configured = fleet_providers.identity(
            "fake-provider", "fake-model", None, "fake"
        )
        self.assertIs(registry.resolve(hook_source="fake", provider="fake-provider"), adapter)
        self.assertEqual(
            adapter.launch_spec(configured, ["fake", "--model", "fake-model"], runner="api")[
                "adapter"
            ],
            "fake",
        )
        submission = adapter.prepare_submission(
            configured, "do work", "fake-run", Path("/tmp/fake-prompt")
        )
        sent: list[str] = []
        adapter.submit(submission, lambda payload: sent.append(payload) or {"sent": True})
        self.assertEqual(sent, [submission["payload"]])
        self.assertEqual(adapter.confirm_submission(lambda: {"confirmed": True}), {"confirmed": True})
        self.assertEqual(
            adapter.observe(
                {
                    "source": "fake", "name": "agent.hook.Stop",
                    "payload": {"_source": "fake", "phase": "completed"},
                },
                {},
            ),
            "stop",
        )
        evidence = adapter.extract_final_response(
            configured, "fake-session", "fake-run", "now",
            {"fake": lambda *_: ("answer", "fake-provider", "fake-model")},
        )
        adapter.verify_identity(configured, evidence)
        self.assertEqual(adapter.cancel(lambda: {"cancelled": True}), {"cancelled": True})

    def test_wrong_adapter_cannot_claim_another_provider_or_variant(self) -> None:
        with self.assertRaisesRegex(fleet_providers.ProviderIdentityError, "cannot claim provider"):
            fleet_providers.adapter_for(
                fleet_providers.identity("anthropic", "gpt", None, "codex")
            )
        configured = fleet_providers.identity("minimax", "MiniMax-M3", "none", "opencode")
        adapter = fleet_providers.adapter_for(configured)
        with self.assertRaisesRegex(fleet_providers.ProviderIdentityError, "variant"):
            adapter.verify_identity(
                configured,
                fleet_providers.ProviderEvidence(
                    "answer", "minimax", "MiniMax-M3", "thinking"
                ),
            )

    def test_provider_commands_bind_exactly_one_configured_model(self) -> None:
        cases = (
            ("openai", "gpt-test", "codex", ["codex", "--model", "gpt-test", "--model", "other"]),
            ("anthropic", "claude-test", "claude", ["claude", "--model", "claude-test", "--model", "other"]),
            ("moonshot-ai", "kimi-test", "kimi", ["kimi", "--model", "kimi-test", "--model", "other", "--thinking", "--agent-file", ".kimi/agents/fleet-reviewer/agent.yaml"]),
            ("ollama", "local-test", "", ["ollama", "run", "other"]),
        )
        for provider, model, source, command in cases:
            with self.subTest(provider=provider), self.assertRaises(fleet_providers.ProviderError):
                identity = fleet_providers.identity(provider, model, None, source)
                fleet_providers.adapter_for(identity).validate_configuration(
                    identity, command=command, runner="local" if provider == "ollama" else "interactive"
                )
        for provider, executable, source in (
            ("openai", "codex", "codex"),
            ("anthropic", "claude", "claude"),
            ("moonshot-ai", "kimi", "kimi"),
        ):
            identity = fleet_providers.identity(provider, "model", None, source)
            for command in (
                [executable, "--model", "model", "--model"],
                [executable, "--model", "model", "--model=other"],
            ):
                with self.subTest(provider=provider, command=command), self.assertRaises(
                    fleet_providers.ProviderError
                ):
                    if source == "kimi":
                        command += ["--thinking", "--agent-file", ".kimi/agents/fleet-reviewer/agent.yaml"]
                    fleet_providers.adapter_for(identity).validate_configuration(
                        identity, command=command, runner="interactive"
                    )

    def test_provider_cli_is_machine_readable(self) -> None:
        result = subprocess.run(
            ["python3", str(ROOT / "scripts" / "fleet_providers.py"), "list"],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["adapters"],
            ["claude", "codex", "kimi", "ollama", "opencode"],
        )

    def test_provider_cli_rejects_ambiguous_command_json(self) -> None:
        base = [
            "python3",
            str(ROOT / "scripts" / "fleet_providers.py"),
            "validate",
            "--provider",
            "ollama",
            "--model",
            "gemma3:4b",
            "--runner",
            "local",
            "--command-json",
        ]
        for payload in (
            '["ollama","run","gemma3:4b",1e999]',
            '\ufeff["ollama","run","gemma3:4b"]',
            r'["ollama","run","\ud800"]',
            '["ollama","run","gemma3:4b"][]',
        ):
            result = subprocess.run(
                [*base, payload],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            with self.subTest(payload=payload):
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("fleet-providers:", result.stderr)


if __name__ == "__main__":
    unittest.main()
