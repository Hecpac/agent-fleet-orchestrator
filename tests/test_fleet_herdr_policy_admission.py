"""Scope and acceptance are profile-free mechanics; a profile only admits or requires them."""
from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tests.test_fleet_herdr_orchestration_closure import synthetic_spec  # noqa: E402
from tests.test_mission_run import mission_run  # noqa: E402
from fleet_herdr_personal import PROFILE as PERSONAL_CLI  # noqa: E402
import fleet_acceptance  # noqa: E402
import fleet_herdr_archive  # noqa: E402
import fleet_herdr_profile as profiles  # noqa: E402
import fleet_herdr_scope as scope  # noqa: E402
import fleet_mission  # noqa: E402
import fleet_mission_state as state  # noqa: E402
import workflow_config  # noqa: E402

SCOPE = {"schema_version": 1, "editable_paths": ["answer.txt"], "temporary_directories": [".fleet-scratch"],
         "max_entries": 1000, "max_bytes": 1024 * 1024}
ACCEPTANCE = {"schema_version": 1, "requirements": [{
    "id": "answer", "description": "Build output exists", "checks": [{
        "kind": "text_contains", "path": "answer.txt", "expected": "implemented"}]}]}
WORKFLOWS = {profiles.LEGACY: "herdr-implementation", profiles.RESEARCH: "herdr-research-implementation",
             profiles.MINIMAL: "herdr-minimal-implementation"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def compiled_for(profile: profiles.HerdrProfile) -> dict:
    return workflow_config.compile_path(ROOT / "workflows" / f"{WORKFLOWS[profile]}.yaml")


def runtime_options(profile: profiles.HerdrProfile, **extra) -> dict:
    options = {"timeout_seconds": 1800, **extra}
    if profile is not profiles.LEGACY:
        options.update(profiles.runtime_binding(profile), herdr_personal_cli=PERSONAL_CLI)
    return options


class TargetFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="fleet-policy-admission-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.target = self.root / "target"
        self.target.mkdir()
        git(self.target, "init", "-q")
        git(self.target, "config", "user.name", "Fixture")
        git(self.target, "config", "user.email", "fixture@example.invalid")
        (self.target / "README.md").write_text("baseline\n")
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "fixture")
        self.head = git(self.target, "rev-parse", "HEAD")
        self.runs = self.root / "runs"

    def assert_no_mission(self) -> None:
        self.assertFalse((self.runs / "missions").exists() and any((self.runs / "missions").iterdir()))


class CommandPolicyTests(TargetFixture):
    def dry(self, workflow: str, **kwargs):
        return mission_run.dry_run(feature="policy", objective="Implement the answer", workflow_name=workflow,
                                   target_repo=self.target, risk_override="auto", timeout_seconds=None, **kwargs)

    def create(self, workflow: str, **kwargs):
        with mock.patch.object(mission_run, "drive_mission", side_effect=AssertionError("must not drive")):
            return mission_run.create_and_drive(
                self.runs, feature="policy", objective="Implement the answer", workflow_name=workflow,
                target_repo=self.target, risk_override="auto", timeout_seconds=3600,
                allow_dirty_baseline=False, teardown=False, herdr_session="fixture", **kwargs)

    def test_scope_is_rejected_for_every_other_profile(self) -> None:
        for workflow in (WORKFLOWS[profiles.LEGACY], WORKFLOWS[profiles.RESEARCH]):
            for command in (self.dry, self.create):
                with self.subTest(workflow=workflow, command=command.__name__):
                    with self.assertRaisesRegex(mission_run.MissionRunError,
                                                r"^--scope-contract requires sol_minimal_v1$"):
                        command(workflow, acceptance_contract=ACCEPTANCE, scope_contract=SCOPE)
        self.assert_no_mission()

    def test_minimal_profile_requires_an_acceptance_contract(self) -> None:
        for command in (self.dry, self.create):
            with self.subTest(command=command.__name__):
                with self.assertRaisesRegex(mission_run.MissionRunError,
                                            r"^minimal Herdr profile requires --acceptance-contract$"):
                    command(WORKFLOWS[profiles.MINIMAL], scope_contract=SCOPE)
        self.assert_no_mission()

    def test_other_profiles_do_not_require_an_acceptance_contract(self) -> None:
        for workflow in (WORKFLOWS[profiles.LEGACY], WORKFLOWS[profiles.RESEARCH]):
            with self.subTest(workflow=workflow):
                self.assertNotIn("physical_scope", self.dry(workflow))

    def test_minimal_profile_admits_scope(self) -> None:
        dry = self.dry(WORKFLOWS[profiles.MINIMAL], acceptance_contract=ACCEPTANCE, scope_contract=SCOPE)
        self.assertEqual(dry["physical_scope"]["contract_sha256"], scope.digest(SCOPE))

    def test_functional_rejection_precedes_scope_rejection(self) -> None:
        with self.assertRaisesRegex(mission_run.MissionRunError,
                                    r"^functional contracts require a supported Herdr workflow$"):
            self.dry("implementation", functional_contract=synthetic_spec(), scope_contract=SCOPE)


class CreationPolicyTests(TargetFixture):
    def create(self, profile: profiles.HerdrProfile, key: str, **options):
        return fleet_mission.create_mission(
            self.runs, compiled=compiled_for(profile), feature="policy", objective="Implement the answer",
            target_repo=self.target, base_sha=self.head, idempotency_key=key,
            runtime_options=runtime_options(profile, **options))

    def test_scope_is_rejected_for_every_other_profile(self) -> None:
        for profile in (profiles.LEGACY, profiles.RESEARCH):
            with self.subTest(profile=profile.profile_id):
                with self.assertRaisesRegex(fleet_mission.MissionError,
                                            r"^physical scope v1 requires sol_minimal_v1$"):
                    self.create(profile, "policy:" + profile.profile_id, scope_contract=SCOPE)
        self.assert_no_mission()

    def test_minimal_profile_requires_an_acceptance_contract_at_creation(self) -> None:
        with self.assertRaisesRegex(fleet_mission.MissionError,
                                    r"^minimal Herdr profile requires an acceptance contract at creation$"):
            self.create(profiles.MINIMAL, "policy:minimal", scope_contract=SCOPE)
        self.assert_no_mission()

    def test_ledger_admits_a_scope_pin_only_for_the_scope_profile(self) -> None:
        pin = scope.digest(SCOPE)
        created = {}
        for profile in (profiles.LEGACY, profiles.RESEARCH, profiles.MINIMAL):
            key, options = "ledger:" + profile.profile_id, {}
            if profile is profiles.MINIMAL:
                key = "mission-b:ledger:acceptance:" + fleet_acceptance.digest(ACCEPTANCE)
                options = {"acceptance_contract": ACCEPTANCE}
            mission_id, _ = self.create(profile, key, **options)
            events = state.read_events(state.ledger_path(self.runs, mission_id), expected_mission_id=mission_id)
            self.assertEqual(events[0]["kind"], "mission_created")
            created[profile] = events[0]["payload"]
        state._validate_payload("mission_created", {**created[profiles.MINIMAL], scope.FIELD: pin})
        for profile in (profiles.LEGACY, profiles.RESEARCH):
            with self.subTest(profile=profile.profile_id):
                state._validate_payload("mission_created", created[profile])
                with self.assertRaisesRegex(state.MissionStateError,
                                            r"^physical scope v1 requires sol_minimal_v1$"):
                    state._validate_payload("mission_created", {**created[profile], scope.FIELD: pin})


class ScopeBindingPolicyTests(unittest.TestCase):
    def test_binding_admits_only_the_scope_profile(self) -> None:
        pin, options = scope.digest(SCOPE), {"scope_contract": SCOPE}
        self.assertEqual(scope.validate_binding({scope.FIELD: pin, "herdr_profile": "sol_minimal_v1"}, options),
                         SCOPE)
        for profile_id in ("astra_sol_v1", "astra_sol_research_v1", None):
            with self.subTest(profile=profile_id):
                with self.assertRaisesRegex(scope.ScopeError, r"^physical scope v1 requires sol_minimal_v1$"):
                    scope.validate_binding({scope.FIELD: pin, "herdr_profile": profile_id}, options)
                self.assertIsNone(scope.validate_binding({"herdr_profile": profile_id}, {}))

    def test_ledger_binding_mismatch_is_reported_first(self) -> None:
        with self.assertRaisesRegex(scope.ScopeError, r"^scope contract differs from creation ledger$"):
            scope.validate_binding({scope.FIELD: "0" * 64, "herdr_profile": "astra_sol_v1"},
                                   {"scope_contract": SCOPE})


class PolicyDeclarationTests(unittest.TestCase):
    PROFILE_LITERALS = {value for profile in profiles.BY_PROFILE_ID.values()
                        for value in (profile.profile_id, profile.preset)}

    @staticmethod
    def tree(name: str) -> ast.Module:
        return ast.parse((ROOT / "scripts" / name).read_text())

    def test_catalog_declares_each_policy_once(self) -> None:
        self.assertIs(profiles.PHYSICAL_SCOPE_PROFILE, profiles.MINIMAL)
        self.assertIs(profiles.ACCEPTANCE_REQUIRED_PROFILE, profiles.MINIMAL)
        self.assertEqual(state.PHYSICAL_SCOPE_PROFILE_ID, profiles.PHYSICAL_SCOPE_PROFILE.profile_id)

    def test_scope_and_acceptance_mechanics_never_read_the_profile(self) -> None:
        for name in ("fleet_herdr_scope.py", "fleet_acceptance.py", "fleet_functional.py"):
            with self.subTest(module=name):
                tree = self.tree(name)
                imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                            for alias in node.names}
                imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
                self.assertNotIn("fleet_herdr_profile", imported)
                literals = {node.value for node in ast.walk(tree)
                            if isinstance(node, ast.Constant) and isinstance(node.value, str)}
                self.assertFalse(literals & self.PROFILE_LITERALS)

    def test_admission_sites_consult_the_declarations(self) -> None:
        # Both policies belong to the minimal profile; mission-run's Research
        # handoff branch is a runtime policy outside scope and acceptance.
        for name in ("fleet_mission.py", "mission-run.py"):
            with self.subTest(module=name):
                named = {node.attr for node in ast.walk(self.tree(name))
                         if isinstance(node, ast.Attribute)
                         and isinstance(node.value, ast.Name) and node.value.id == "fleet_herdr_profile"}
                self.assertNotIn("MINIMAL", named)
                self.assertTrue(named & {"PHYSICAL_SCOPE_PROFILE", "ACCEPTANCE_REQUIRED_PROFILE"})

    def test_archive_schemas_follow_the_catalog(self) -> None:
        self.assertEqual(fleet_herdr_archive.VERSIONED_ARCHIVE_PROFILES, {
            **{p.archive_schema_version: p for p in profiles.VERSIONED},
            scope.ARCHIVE_VERSION: profiles.PHYSICAL_SCOPE_PROFILE,
            fleet_herdr_archive.REPAIR_ARCHIVE_VERSION: profiles.PHYSICAL_SCOPE_PROFILE})
        self.assertEqual(fleet_herdr_archive.REPAIR_ARCHIVE_VERSION, 9)
        self.assertEqual(fleet_herdr_archive.READABLE_ARCHIVE_SCHEMAS, {2, 3, 4, 5, 6, 7, 8, 9})
        self.assertEqual(fleet_herdr_archive.FUNCTIONAL_ARCHIVE_SCHEMAS, {4, 5, 6, 7, 8, 9})
        self.assertEqual(fleet_herdr_archive.SCOPE_ARCHIVE_SCHEMAS, {8, 9})


if __name__ == "__main__":
    unittest.main()
