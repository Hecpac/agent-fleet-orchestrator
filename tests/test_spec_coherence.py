from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "orchestration" / "router.yaml"
FDP2_PROMPTS = (
    ROOT / "orchestration" / "prompts" / "fdp2_maker_proposal.md",
    ROOT / "orchestration" / "prompts" / "fdp2_checker.md",
    ROOT / "orchestration" / "prompts" / "fdp2_maker_revision.md",
)
FDP2_CONTROLLER = ROOT / "scripts" / "fleet_dialogue_controller.py"
INTERNAL_WIRING = ROOT / "orchestration" / "INTERNAL_WIRING.md"
README = ROOT / "README.md"
FLEET_REVIEWER = ROOT / ".opencode" / "agents" / "fleet-reviewer.md"
MINIMAX_CHECKER = ROOT / ".opencode" / "agents" / "minimax-checker.md"
SKILL_COPIES = (
    ROOT / ".agents" / "skills" / "cmux" / "SKILL.md",
    ROOT / ".claude" / "skills" / "cmux" / "SKILL.md",
)


class SpecCoherenceTests(unittest.TestCase):
    """The router is the single source of truth; prose must not drift from it."""

    def test_skill_copies_are_identical(self) -> None:
        agents_copy, claude_copy = (path.read_text(encoding="utf-8") for path in SKILL_COPIES)
        self.assertEqual(
            agents_copy,
            claude_copy,
            ".agents and .claude copies of the cmux SKILL have drifted apart",
        )

    def test_skill_schema_version_matches_router(self) -> None:
        router_version = json.loads(ROUTER.read_text(encoding="utf-8"))["schema_version"]
        for path in SKILL_COPIES:
            versions = re.findall(r"^schema_version=(\d+)$", path.read_text(encoding="utf-8"), re.M)
            self.assertTrue(versions, f"{path} shows no manifest schema_version example")
            for version in versions:
                self.assertEqual(
                    int(version),
                    router_version,
                    f"{path} shows schema_version={version}; router.yaml is {router_version}",
                )

    def test_fdp2_contract_and_documentation_remain_aligned(self) -> None:
        router = json.loads(ROUTER.read_text(encoding="utf-8"))
        preset = router["presets"]["fleet_dialogue"]
        instances = {
            item["instance_id"]: item["role_type"] for item in preset["instances"]
        }
        self.assertTrue(preset["include_lead"])
        self.assertEqual(
            instances,
            {
                "maker": "codex",
                "checker": "minimax_checker",
                "challenge": "glm",
                "verify": "claude_reviewer",
            },
        )
        self.assertEqual(router["roles"][instances["maker"]]["authority"], "write")
        for instance in ("checker", "challenge", "verify"):
            self.assertNotEqual(router["roles"][instances[instance]]["authority"], "write")
        checker_role = router["roles"][instances["checker"]]
        self.assertEqual(
            checker_role["command"],
            ["opencode", "--agent", "minimax-checker"],
        )
        self.assertEqual(checker_role["variant"], "none")
        self.assertNotIn("-m", checker_role["command"])
        self.assertNotIn("--variant", checker_role["command"])

        reviewer = FLEET_REVIEWER.read_text(encoding="utf-8")
        checker_agent = MINIMAX_CHECKER.read_text(encoding="utf-8")
        for restriction in (
            'mode: primary',
            'edit: deny',
            '"*": deny',
            'task: deny',
            'skill: deny',
            'question: deny',
            'do not ask the user to approve a plan',
            'the first visible character',
        ):
            with self.subTest(restriction=restriction):
                self.assertIn(restriction, reviewer)
                self.assertIn(restriction, checker_agent)
        self.assertIn("model: minimax/MiniMax-M3", checker_agent)
        self.assertIn("variant: none", checker_agent)
        self.assertNotIn("\nmodel:", reviewer)
        self.assertNotIn("\nvariant:", reviewer)
        for hardened in (
            "HARD OUTPUT CONTRACT",
            "exactly one JSON object",
            "FORBIDDEN anywhere before, between, or after",
            "One stray visible word outside the JSON invalidates",
            "Do not waste your token budget narrating",
        ):
            with self.subTest(hardened=hardened):
                self.assertIn(hardened, checker_agent)

        controller = FDP2_CONTROLLER.read_text(encoding="utf-8")
        for contract in (
            'MAX_REVISION_ROUNDS = 3',
            'RUN_TIMEOUT_SECONDS = 30 * 60',
            'DIALOGUE_DEADLINE_SECONDS = 4 * 60 * 60',
            '"objective", "negative_scope", "acceptance_criteria"',
            '"start", "step", "show", "verify", "abandon"',
            'each Maker result must add exactly one commit',
            'FDP-2 BUILD gate requires latest status accepted',
            'fleet-down refuses an active FDP-2 conversation',
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, controller)

        maker, checker, revision = (
            path.read_text(encoding="utf-8") for path in FDP2_PROMPTS
        )
        self.assertIn('"base_sha": "{{BASE_SHA}}"', maker)
        self.assertIn('explicitly authorized implementation', maker)
        self.assertIn('Nunca pongas una ruta', maker)
        self.assertIn('debe afirmar realmente la condición', maker)
        self.assertIn('"verdict": "ACCEPT"', checker)
        self.assertIn('Una ruta nunca es evidencia', checker)
        self.assertIn('el primer carácter visible debe ser `{`', checker)
        self.assertIn('Toda inspección de archivos, tests y Git', checker)
        self.assertIn('Cada finding debe aparecer exactamente una vez', revision)
        self.assertIn('already authorized BUILD', revision)
        self.assertIn('fleet_dialogue.py read', checker)
        self.assertIn('fleet_dialogue.py read', revision)

        wiring = INTERNAL_WIRING.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        self.assertIn(
            'rule: bounded_fleet_dialogue_is_control_stepped_and_fail_closed', wiring
        )
        self.assertIn(
            'rule: opencode_prompt_transport_is_one_physical_submission', wiring
        )
        self.assertIn(
            'rule: minimax_checker_requires_durable_none_variant', wiring
        )
        self.assertIn(
            'test_opencode_prompt_transport_is_one_submission_with_exact_logical_prompt',
            wiring,
        )
        for documented in (
            '--preset fleet_dialogue',
            '--spec-file /tmp/fdp2-task.json',
            'CONTROL is a stepper',
            'never dispatches a model or publishes an FDP-1',
            'verify --archive <archive>',
        ):
            with self.subTest(documented=documented):
                self.assertIn(documented, readme)

if __name__ == "__main__":
    unittest.main()
