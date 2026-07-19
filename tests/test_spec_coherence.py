from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "orchestration" / "router.yaml"
FLEET_VALIDATION_PROMPT = ROOT / "orchestration" / "prompts" / "fleet_validation.md"
FDP2_PROMPTS = (
    ROOT / "orchestration" / "prompts" / "fdp2_maker_proposal.md",
    ROOT / "orchestration" / "prompts" / "fdp2_checker.md",
    ROOT / "orchestration" / "prompts" / "fdp2_maker_revision.md",
)
FDP2_CONTROLLER = ROOT / "scripts" / "fleet_dialogue_controller.py"
FDP3_PROMPTS = (
    ROOT / "orchestration" / "prompts" / "fdp3_glm_challenge.md",
    ROOT / "orchestration" / "prompts" / "fdp3_claude_verify.md",
)
FDP3_CONTROLLER = ROOT / "scripts" / "fleet_assurance_controller.py"
FLEET_STATE = ROOT / "scripts" / "fleet_state.py"
FLEET_DOWN = ROOT / "scripts" / "fleet-down.sh"
INTERNAL_WIRING = ROOT / "orchestration" / "INTERNAL_WIRING.md"
README = ROOT / "README.md"
FLEET_REVIEWER = ROOT / ".opencode" / "agents" / "fleet-reviewer.md"
MINIMAX_CHECKER = ROOT / ".opencode" / "agents" / "minimax-checker.md"
GLM_CHALLENGER = ROOT / ".opencode" / "agents" / "glm-challenger.md"
SKILL_COPIES = (
    ROOT / ".agents" / "skills" / "cmux" / "SKILL.md",
    ROOT / ".claude" / "skills" / "cmux" / "SKILL.md",
)


class SpecCoherenceTests(unittest.TestCase):
    """The router is the single source of truth; prose must not drift from it."""

    def test_internal_wiring_test_references_resolve(self) -> None:
        wiring = INTERNAL_WIRING.read_text(encoding="utf-8")
        references = sorted(
            set(
                re.findall(
                    r"`(tests/[^`\n]+?\.py::[A-Za-z_][A-Za-z0-9_]*::"
                    r"[A-Za-z_][A-Za-z0-9_]*)`",
                    wiring,
                )
            )
        )
        self.assertTrue(references, "INTERNAL_WIRING has no test references")
        parsed: dict[Path, ast.Module] = {}
        for reference in references:
            relative, class_name, method_name = reference.split("::")
            path = ROOT / relative
            with self.subTest(reference=reference):
                self.assertTrue(
                    path.is_file(), f"missing referenced test file: {relative}"
                )
                if path not in parsed:
                    parsed[path] = ast.parse(
                        path.read_text(encoding="utf-8"), filename=str(path)
                    )
                module = parsed[path]
                classes = {
                    node.name: node
                    for node in module.body
                    if isinstance(node, ast.ClassDef)
                }
                self.assertIn(
                    class_name, classes, f"missing referenced class: {reference}"
                )
                methods = {
                    node.name
                    for node in classes[class_name].body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                self.assertIn(
                    method_name,
                    methods,
                    f"missing referenced test method: {reference}",
                )

    def test_skill_copies_are_identical(self) -> None:
        agents_copy, claude_copy = (
            path.read_text(encoding="utf-8") for path in SKILL_COPIES
        )
        self.assertEqual(
            agents_copy,
            claude_copy,
            ".agents and .claude copies of the cmux SKILL have drifted apart",
        )

    def test_skill_schema_version_matches_router(self) -> None:
        router_version = json.loads(ROUTER.read_text(encoding="utf-8"))[
            "schema_version"
        ]
        for path in SKILL_COPIES:
            versions = re.findall(
                r"^schema_version=(\d+)$", path.read_text(encoding="utf-8"), re.M
            )
            self.assertTrue(
                versions, f"{path} shows no manifest schema_version example"
            )
            for version in versions:
                self.assertEqual(
                    int(version),
                    router_version,
                    f"{path} shows schema_version={version}; router.yaml is {router_version}",
                )

    def test_fleet_validation_prompt_locks_exact_fail_closed_lifecycle(self) -> None:
        router = json.loads(ROUTER.read_text(encoding="utf-8"))
        prompt = FLEET_VALIDATION_PROMPT.read_text(encoding="utf-8")
        audit_instances = router["presets"]["audit"]["instances"]

        self.assertEqual(
            [instance["instance_id"] for instance in audit_instances],
            ["analysis", "challenge", "verify"],
        )
        for instance in audit_instances:
            self.assertIn(f"`{instance['instance_id']}`", prompt)

        required_contracts = (
            "EXPECTED_INSTANCES=<instance_id separados por espacios, sin incluir CONTROL>",
            './scripts/fleet-send.sh <feature> <instance> "<task>" --json',
            './scripts/fleet-dispatch.sh <feature> <instance> "<evidence-pack-and-task>" --json',
            "--run <instance>=<run_id>",
            "para un worker local, `result_file` real, no vacío",
            "para un agente frontier, evento terminal y transcript estructurado",
            "STATUS: DONE",
            "blocked`, `failed`, `abandoned`, `indeterminate",
            "git diff --exit-code",
            "just fleet-down <feature>",
            "OPEN_LANES:",
            "EXPECTED_INSTANCES=analysis challenge verify",
        )
        for contract in required_contracts:
            with self.subTest(contract=contract):
                self.assertIn(contract, prompt)

        self.assertIn("Cualquier condición ausente produce FAIL", prompt)
        self.assertIn("No uses polling ni sleeps", prompt)
        self.assertNotIn("STATUS: PASS | PARTIAL", prompt)

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
            self.assertNotEqual(
                router["roles"][instances[instance]]["authority"], "write"
            )
        checker_role = router["roles"][instances["checker"]]
        self.assertEqual(
            checker_role["command"],
            ["opencode", "--agent", "minimax-checker"],
        )
        self.assertEqual(checker_role["variant"], "none")
        self.assertNotIn("-m", checker_role["command"])
        self.assertNotIn("--variant", checker_role["command"])

        challenge_role = router["roles"][instances["challenge"]]
        self.assertEqual(
            challenge_role["command"],
            ["opencode", "-m", "zai/glm-5.2", "--agent", "glm-challenger"],
        )
        self.assertNotIn("variant", challenge_role)

        verify_role = router["roles"][instances["verify"]]
        self.assertIn("--append-system-prompt", verify_role["command"])
        appended = verify_role["command"][
            verify_role["command"].index("--append-system-prompt") + 1
        ]
        for hardened_verify in (
            "HARD OUTPUT CONTRACT",
            "first visible character",
            "One stray visible word outside the JSON",
            "tool-availability notes",
        ):
            with self.subTest(hardened_verify=hardened_verify):
                self.assertIn(hardened_verify, appended)

        reviewer = FLEET_REVIEWER.read_text(encoding="utf-8")
        checker_agent = MINIMAX_CHECKER.read_text(encoding="utf-8")
        challenger_agent = GLM_CHALLENGER.read_text(encoding="utf-8")
        for restriction in (
            "mode: primary",
            '  "*": deny',
            '"*.env": deny',
            "external_directory: deny",
            "Bash, Git commands, external directories, and every unlisted tool are denied.",
            "do not ask the user to approve a plan",
            "the first visible character",
        ):
            with self.subTest(restriction=restriction):
                self.assertIn(restriction, reviewer)
                self.assertIn(restriction, checker_agent)
                self.assertIn(restriction, challenger_agent)
        for agent in (reviewer, checker_agent, challenger_agent):
            self.assertNotIn("\n  bash:", agent)
        self.assertIn("model: minimax/MiniMax-M3", checker_agent)
        self.assertIn("variant: none", checker_agent)
        self.assertIn("model: minimax/MiniMax-M3", reviewer)
        self.assertIn("variant: none", reviewer)
        self.assertNotIn("\nmodel:", challenger_agent)
        self.assertNotIn("\nvariant:", challenger_agent)
        for hardened in (
            "HARD OUTPUT CONTRACT",
            "exactly one JSON object",
            "One stray visible word outside the JSON invalidates",
            "Do not waste your token budget narrating",
        ):
            with self.subTest(hardened=hardened):
                self.assertIn(hardened, checker_agent)
                self.assertIn(hardened, challenger_agent)
        self.assertIn("FORBIDDEN anywhere before, between, or after", checker_agent)
        self.assertIn("FORBIDDEN anywhere before, between, or after", challenger_agent)

        controller = FDP2_CONTROLLER.read_text(encoding="utf-8")
        for contract in (
            "MAX_REVISION_ROUNDS = 3",
            "RUN_TIMEOUT_SECONDS = 30 * 60",
            "DIALOGUE_DEADLINE_SECONDS = 4 * 60 * 60",
            '"objective", "negative_scope", "acceptance_criteria"',
            '"start", "step", "show", "verify", "abandon"',
            "each Maker result must add exactly one commit",
            "FDP-2 BUILD gate requires latest status accepted",
            "fleet-down refuses an active FDP-2 conversation",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, controller)

        maker, checker, revision = (
            path.read_text(encoding="utf-8") for path in FDP2_PROMPTS
        )
        self.assertIn('"base_sha": "{{BASE_SHA}}"', maker)
        self.assertIn("explicitly authorized implementation", maker)
        self.assertIn("Nunca pongas una ruta", maker)
        self.assertIn("debe afirmar realmente la condición", maker)
        self.assertIn('"verdict": "ACCEPT"', checker)
        self.assertIn("Una ruta nunca es evidencia", checker)
        self.assertIn("el primer carácter visible debe ser `{`", checker)
        self.assertIn("EVIDENCE_PACK_JSON_BEGIN", checker)
        self.assertIn("No ejecutes Bash ni Git", checker)
        self.assertIn("Cada finding debe aparecer exactamente una vez", revision)
        self.assertIn("already authorized BUILD", revision)
        self.assertNotIn("fleet_dialogue.py read", checker)
        self.assertIn("fleet_dialogue.py read", revision)

        wiring = INTERNAL_WIRING.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        self.assertIn(
            "rule: bounded_fleet_dialogue_is_control_stepped_and_fail_closed", wiring
        )
        self.assertIn(
            "rule: opencode_prompt_transport_is_one_physical_submission", wiring
        )
        self.assertIn(
            "rule: minimax_opencode_roles_require_durable_none_variant", wiring
        )
        self.assertIn(
            "test_opencode_prompt_transport_is_one_submission_with_exact_logical_prompt",
            wiring,
        )
        for documented in (
            "--preset fleet_dialogue",
            "--spec-file /tmp/fdp2-task.json",
            "CONTROL is a stepper",
            "never dispatches a model or publishes an FDP-1",
            "verify --archive <archive>",
        ):
            with self.subTest(documented=documented):
                self.assertIn(documented, readme)

    def test_fdp3_contract_and_documentation_remain_aligned(self) -> None:
        controller = FDP3_CONTROLLER.read_text(encoding="utf-8")
        glm_prompt, claude_prompt = (
            path.read_text(encoding="utf-8") for path in FDP3_PROMPTS
        )
        state = FLEET_STATE.read_text(encoding="utf-8")
        teardown = FLEET_DOWN.read_text(encoding="utf-8")
        wiring = INTERNAL_WIRING.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")

        for contract in (
            "RUN_TIMEOUT_SECONDS = 30 * 60",
            "ASSURANCE_DEADLINE_SECONDS = 2 * 60 * 60",
            '"awaiting_phase_advance"',
            '"verified"',
            '"rejected"',
            '("start", "step", "show", "verify", "abandon")',
            "step requires exactly one of run_id, message_id, or phase_advanced",
            "FDP-3 CHALLENGE gate evidence is not the exact control head",
            "fleet-down refuses an active FDP-3 assurance",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, controller)

        self.assertIn('"summary": "resumen no vacío', glm_prompt)
        self.assertIn("no emitas `ACCEPT`, `REJECT`, `VERIFIED`", glm_prompt)
        self.assertIn('"verdict": "VERIFIED|REJECTED"', claude_prompt)
        self.assertIn("cada `finding_id` de GLM exactamente una vez", claude_prompt)
        self.assertIn("El primer carácter visible debe ser `{`", glm_prompt)
        self.assertIn("El primer carácter visible debe ser `{`", claude_prompt)
        self.assertIn("challenge_phase_gate", state)
        self.assertIn("--cleanup-snapshots", teardown)
        self.assertIn("assurance-receipt.json", teardown)
        self.assertIn(
            "rule: sequential_assurance_is_context_bound_phase_gated_and_fail_closed",
            wiring,
        )
        for documented in (
            "FDP-3 sequential assurance",
            "step --phase-advanced",
            "fleet_assurance_controller.py verify --archive <archive>",
            "There are no retries or provider fallbacks",
        ):
            with self.subTest(documented=documented):
                self.assertIn(documented, readme)


if __name__ == "__main__":
    unittest.main()
