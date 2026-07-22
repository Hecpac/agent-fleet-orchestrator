"""Conformance contract for provider transports and submit semantics.

Every provider adapter and every interactive router role must satisfy one
shared contract, so a new provider quirk surfaces as a red test here instead
of as a surprise inside a live fleet:

- ``prepare_submission`` returns exactly ``{prompt, payload, transport}``.
- ``prompt`` is always the logical prompt (the durable evidence written to
  ``runs/prompts/``), never a wire wrapper.
- Inline payloads survive a TUI composer: single line, collision-checked.
- The transport a role declares in the router matches what its adapter
  actually produces, and every interactive hook_source has declared submit
  semantics in ``router.providers``.
- Every enum in the MCP tool schemas declares an explicit type (strict
  consumers such as Kimi CLI reject enums with inferred types).
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_providers  # noqa: E402
import router_config  # noqa: E402


TASK = "Review the supplied evidence and report findings."
RUN_ID = "00000000-0000-4000-8000-000000000001"
PROMPT_PATH = Path("/tmp/fleet-conformance/prompt.txt")

INTERACTIVE_IDENTITIES = {
    "codex": ("openai", "gpt-test", None),
    "claude": ("anthropic", "claude-test", None),
    "kimi": ("moonshot-ai", "kimi-test", None),
    "opencode": ("minimax", "MiniMax-M3", None),
}


def submission_for(hook_source: str) -> dict[str, str]:
    provider, model, variant = INTERACTIVE_IDENTITIES[hook_source]
    identity = fleet_providers.identity(
        provider, model, variant, hook_source=hook_source
    )
    adapter = fleet_providers.DEFAULT_REGISTRY.resolve(
        hook_source=hook_source, provider=provider
    )
    return adapter.prepare_submission(identity, TASK, RUN_ID, PROMPT_PATH)


class TransportConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = router_config.load_router()

    def test_every_adapter_returns_the_exact_submission_shape(self) -> None:
        for hook_source in sorted(INTERACTIVE_IDENTITIES):
            with self.subTest(hook_source=hook_source):
                submission = submission_for(hook_source)
                self.assertEqual(
                    set(submission), {"prompt", "payload", "transport"}
                )
                for field, value in submission.items():
                    self.assertIsInstance(value, str, field)
                    self.assertTrue(value, field)

    def test_prompt_is_always_the_logical_prompt_never_a_wrapper(self) -> None:
        logical = fleet_providers.BaseAdapter.logical_prompt(TASK, RUN_ID)
        for hook_source in sorted(INTERACTIVE_IDENTITIES):
            with self.subTest(hook_source=hook_source):
                submission = submission_for(hook_source)
                self.assertEqual(submission["prompt"], logical)
                self.assertIn(f"FLEET_RESULT:{RUN_ID}:<STATUS>", submission["prompt"])

    def test_inline_payloads_are_single_line_and_pointer_references_disk(self) -> None:
        for hook_source in sorted(INTERACTIVE_IDENTITIES):
            with self.subTest(hook_source=hook_source):
                submission = submission_for(hook_source)
                if submission["transport"] == "pointer":
                    self.assertIn(str(PROMPT_PATH), submission["payload"])
                else:
                    self.assertNotIn("\n", submission["payload"])
                    self.assertNotIn("\r", submission["payload"])

    def test_declared_role_transport_matches_adapter_output(self) -> None:
        for role_type, role in self.router["roles"].items():
            if role["runner"] != "interactive":
                continue
            with self.subTest(role=role_type):
                submission = submission_for(role["hook_source"])
                self.assertEqual(role["transport"], submission["transport"])

    def test_kimi_transport_rejects_token_collisions(self) -> None:
        from providers.kimi import KimiAdapter, NEWLINE_TOKEN

        identity = fleet_providers.identity(
            "moonshot-ai", "kimi-test", hook_source="kimi"
        )
        with self.assertRaisesRegex(
            fleet_providers.ProviderError, "collides with the inline transport tokens"
        ):
            KimiAdapter().prepare_submission(
                identity, f"{TASK} {NEWLINE_TOKEN}", RUN_ID, PROMPT_PATH
            )


class SubmitSemanticsConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = router_config.load_router()

    def test_providers_section_covers_exactly_the_interactive_hook_sources(
        self,
    ) -> None:
        used = {
            role["hook_source"]
            for role in self.router["roles"].values()
            if role["runner"] == "interactive"
        }
        self.assertEqual(set(self.router["providers"]), used)

    def test_submit_semantics_are_complete_and_typed(self) -> None:
        for hook_source, entry in self.router["providers"].items():
            with self.subTest(hook_source=hook_source):
                submit = entry["submit"]
                self.assertEqual(
                    set(submit),
                    {"repress_safe", "confirm_timeout_seconds", "event_source"},
                )
                self.assertIsInstance(submit["repress_safe"], bool)
                self.assertIsInstance(submit["confirm_timeout_seconds"], int)
                self.assertGreater(submit["confirm_timeout_seconds"], 0)
                self.assertIn(submit["event_source"], {"cmux", "wire-bridge"})

    def test_kimi_submit_is_single_enter_via_wire_bridge(self) -> None:
        submit = self.router["providers"]["kimi"]["submit"]
        self.assertFalse(submit["repress_safe"])
        self.assertEqual(submit["event_source"], "wire-bridge")

    def test_plan_records_publish_provider_submit_semantics(self) -> None:
        plan = router_config.build_plan(
            self.router, preset_name="kimi_review", run_healthcheck=False
        )
        records = [
            line.split("\x1f")
            for line in router_config._records(plan).splitlines()
            if line.startswith("PROVIDER\x1f")
        ]
        published = {record[1]: record[2:] for record in records}
        self.assertEqual(set(published), set(self.router["providers"]))
        self.assertEqual(published["kimi"], ["false", "30", "wire-bridge"])
        for values in published.values():
            self.assertEqual(len(values), 3)


class RouterConformanceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = router_config.load_router()

    def test_interactive_role_without_transport_fails_closed(self) -> None:
        del self.router["roles"]["kimi"]["transport"]
        with self.assertRaisesRegex(router_config.RouterError, "kimi.transport"):
            router_config.validate_router(self.router)

    def test_transport_on_local_role_fails_closed(self) -> None:
        self.router["roles"]["reviewer"]["transport"] = "pointer"
        with self.assertRaisesRegex(
            router_config.RouterError, "only for interactive roles"
        ):
            router_config.validate_router(self.router)

    def test_hook_source_without_providers_entry_fails_closed(self) -> None:
        del self.router["providers"]["kimi"]
        with self.assertRaisesRegex(
            router_config.RouterError, "no router.providers entry: kimi"
        ):
            router_config.validate_router(self.router)

    def test_unused_providers_entry_fails_closed(self) -> None:
        for role_type in [
            role_type
            for role_type, role in self.router["roles"].items()
            if role.get("hook_source") == "kimi"
        ]:
            del self.router["roles"][role_type]
        for preset in self.router["presets"].values():
            preset["instances"] = [
                item
                for item in preset["instances"]
                if item["role_type"] != "kimi"
            ]
        with self.assertRaisesRegex(
            router_config.RouterError, "entries with no interactive role: kimi"
        ):
            router_config.validate_router(self.router)

    def test_invalid_submit_semantics_fail_closed(self) -> None:
        for mutation, error in (
            ({"repress_safe": "yes"}, "repress_safe must be boolean"),
            ({"confirm_timeout_seconds": 0}, "must be a positive integer"),
            ({"confirm_timeout_seconds": True}, "must be a positive integer"),
            ({"event_source": "screen"}, "event_source must be one of"),
        ):
            router = router_config.load_router()
            router["providers"]["codex"]["submit"].update(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(router_config.RouterError, error):
                    router_config.validate_router(router)

    def test_num_predict_is_local_only_and_positive(self) -> None:
        self.assertEqual(self.router["roles"]["reviewer"]["num_predict"], 2048)
        router = router_config.load_router()
        router["roles"]["kimi"]["num_predict"] = 2048
        with self.assertRaisesRegex(
            router_config.RouterError, "only for local workers"
        ):
            router_config.validate_router(router)
        router = router_config.load_router()
        router["roles"]["reviewer"]["num_predict"] = 0
        with self.assertRaisesRegex(
            router_config.RouterError, "num_predict must be a positive integer"
        ):
            router_config.validate_router(router)


class StrictSchemaConformanceTests(unittest.TestCase):
    def _walk(self, node: object, where: str, failures: list[str]) -> None:
        if isinstance(node, dict):
            if "enum" in node and node.get("type") is None:
                failures.append(where)
            for key, value in node.items():
                self._walk(value, f"{where}.{key}", failures)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                self._walk(value, f"{where}[{index}]", failures)

    def test_every_mcp_enum_declares_an_explicit_type(self) -> None:
        import fleet_agent_mcp
        import fleet_mcp

        failures: list[str] = []
        self._walk(fleet_mcp.TOOLS, "fleet_mcp.TOOLS", failures)
        self._walk(
            fleet_mcp.DECISION_BRIEF_SCHEMA,
            "fleet_mcp.DECISION_BRIEF_SCHEMA",
            failures,
        )
        self._walk(fleet_agent_mcp.TOOLS, "fleet_agent_mcp.TOOLS", failures)
        self.assertEqual(failures, [], "enums without an explicit type")


if __name__ == "__main__":
    unittest.main()
