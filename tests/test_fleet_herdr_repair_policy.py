"""Stage 1 S1: repair policy contract, admission and creation binding."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.test_fleet_herdr_orchestration_closure import MinimalBackend, MinimalFixture, git
from tests.test_fleet_herdr_scope import contract as scope_contract
from tests.test_fleet_functional import synthetic_spec

import fleet_acceptance
import fleet_artifacts
import fleet_herdr_mission
import fleet_herdr_repair_policy as repair
import fleet_json
import fleet_mission
import fleet_mission_state as state
import workflow_config


ROOT = Path(__file__).resolve().parents[1]


def policy(root="/srv/deliveries/owner-loop-v0", **changes):
    return {"schema": repair.SCHEMA, "max_attempts": 3, "closure_policy": "automatic",
            "delivery_root": root, **changes}


class ValidateTests(unittest.TestCase):
    def test_valid_policy_round_trips_and_digest_is_canonical(self):
        value = policy()
        self.assertIs(repair.validate(value), value)
        self.assertEqual(repair.digest(value), fleet_json.sha256(fleet_json.canonical_bytes(value)))
        self.assertEqual(repair.preview(value)["execution"], "not_available")

    def test_fields_schema_and_closure_are_exact(self):
        for bad in ({**policy(), "extra": 1}, {k: v for k, v in policy().items() if k != "delivery_root"},
                    policy(schema="fleet.repair-policy.v2"), [], None):
            with self.subTest(bad=bad), self.assertRaisesRegex(repair.RepairPolicyError, "unsupported repair policy"):
                repair.validate(bad)
        for closure in ("manual", "", 1, None):
            with self.subTest(closure=closure), self.assertRaisesRegex(repair.RepairPolicyError, "closure"):
                repair.validate(policy(closure_policy=closure))

    def test_max_attempts_is_a_bounded_integer(self):
        for attempts in (0, 21, -1, True, False, 3.0, "3", None):
            with self.subTest(attempts=attempts), self.assertRaisesRegex(repair.RepairPolicyError, "max_attempts"):
                repair.validate(policy(max_attempts=attempts))
        for attempts in (1, 3, 20):
            self.assertEqual(repair.validate(policy(max_attempts=attempts))["max_attempts"], attempts)

    def test_delivery_root_is_a_canonical_absolute_path(self):
        for root in ("relative/path", "/", "", "/a/../b", "/a/./b", "/a//b", "/a/", "/a\\b",
                     "/a\nb", "/" + "a" * 4096, 7, None):
            with self.subTest(root=root), self.assertRaisesRegex(repair.RepairPolicyError, "delivery root"):
                repair.validate(policy(root=root))

    def test_load_rejects_oversized_files_and_invalid_json_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            path.write_bytes(fleet_json.canonical_bytes(policy()))
            self.assertEqual(repair.load(path), policy())
            path.write_bytes(b" " * (64 * 1024 + 1))
            with self.assertRaisesRegex(repair.RepairPolicyError, "size limit"):
                repair.load(path)
            path.write_bytes(fleet_json.canonical_bytes(policy(max_attempts=0)))
            with self.assertRaises(repair.RepairPolicyError):
                repair.load(path)


class AdmitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fleet-repair-admit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.compiled = workflow_config.compile_path(ROOT / "workflows" / "herdr-minimal-implementation.yaml")

    def admit(self, value, *, compiled=None, functional=True, scope=True):
        return repair.admit(value, compiled=compiled or self.compiled, runs_dir=self.runs,
                            functional_contract=synthetic_spec() if functional else None,
                            scope_contract=scope_contract() if scope else None)

    def test_valid_policy_outside_runs_is_admitted(self):
        value = policy(str(self.root / "deliveries"))
        self.assertIs(self.admit(value), value)

    def test_delivery_root_overlapping_runs_is_rejected_after_resolution(self):
        link = self.root / "alias"
        os.symlink(self.runs, link)
        for root in (self.runs, self.runs / "deliveries", self.root, link / "deliveries"):
            with self.subTest(root=root), self.assertRaisesRegex(repair.RepairPolicyError, "outside the runs"):
                self.admit(policy(str(root)))

    def test_profile_contracts_and_budget_are_required(self):
        value = policy(str(self.root / "deliveries"))
        other = workflow_config.compile_path(ROOT / "workflows" / "herdr-implementation.yaml")
        with self.assertRaisesRegex(repair.RepairPolicyError, "requires"):
            self.admit(value, compiled=other)
        with self.assertRaisesRegex(repair.RepairPolicyError, "functional contract"):
            self.admit(value, functional=False)
        with self.assertRaisesRegex(repair.RepairPolicyError, "physical scope contract"):
            self.admit(value, scope=False)
        budgeted = copy.deepcopy(self.compiled)
        budgeted["workflow"]["limits"]["token_budget"] = 1000
        with self.assertRaisesRegex(repair.RepairPolicyError, "token budget"):
            self.admit(value, compiled=budgeted)


class CreationTests(MinimalFixture):
    def setUp(self):
        super().setUp()
        self.delivery_root = self.root / "deliveries"
        self.policy = policy(str(self.delivery_root))

    def repair_options(self, **overrides):
        options = {**self.options(), "functional_contract": synthetic_spec(),
                   "scope_contract": scope_contract(), "repair_policy": self.policy}
        options.update(overrides)
        return {key: value for key, value in options.items() if value is not None}

    def create_repair(self, key="repair", **overrides):
        return fleet_mission.create_mission(self.runs, compiled=self.compiled,
            feature="repair-test", objective="Implement sample_stats.stats",
            target_repo=self.target, base_sha=self.head,
            idempotency_key=fleet_acceptance.bound_key(key, self.contract),
            runtime_options=self.repair_options(**overrides))[0]

    def test_creation_pins_and_freezes_the_policy_in_cas(self):
        mid = self.create_repair()
        current = fleet_mission.load_state(self.runs, mid)
        pin = repair.digest(self.policy)
        self.assertEqual(current[repair.FIELD], pin)
        self.assertEqual(current["repair_policy"]["policy_artifact_id"], pin)
        created = next(event for event in state.read_events(state.ledger_path(self.runs, mid))
                       if event["kind"] == "mission_created")
        self.assertEqual(created["payload"][repair.FIELD], pin)
        self.assertEqual(fleet_artifacts.get_bytes(self.runs, mid, pin), fleet_json.canonical_bytes(self.policy))

    def test_changed_or_removed_runtime_policy_fails_binding_before_effects(self):
        mid = self.create_repair()
        root = self.runs / "missions" / mid
        original = fleet_json.loads((root / "runtime-options.json").read_bytes())
        for label, mutate in (("changed", lambda options: options.update(
                                  repair_policy=policy(str(self.delivery_root), max_attempts=2))),
                              ("removed", lambda options: options.pop("repair_policy"))):
            with self.subTest(label=label):
                options = copy.deepcopy(original)
                mutate(options)
                creation = fleet_json.loads((root / "creation-request.json").read_bytes())
                creation["runtime_options"] = options
                for name, value in (("runtime-options.json", options), ("creation-request.json", creation)):
                    (root / name).write_bytes(fleet_json.canonical_bytes(value) + b"\n")
                result = fleet_herdr_mission.drive(self.runs, mid)
                self.assertIn("repair policy binding failed", result["next_action"])
                self.assertEqual(MinimalBackend.calls, [])
                self.assertFalse((root / "candidate").exists())

    def test_mission_run_cli_dry_reads_the_policy_file_without_effects(self):
        import subprocess
        import sys
        paths = {}
        for name, value in (("acceptance", self.contract), ("functional", synthetic_spec()),
                            ("scope", scope_contract()), ("repair", self.policy)):
            paths[name] = self.root / f"{name}.json"
            paths[name].write_bytes(fleet_json.canonical_bytes(value))
        run = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/mission-run.py"),
            "--runs-dir", str(self.runs), "dry", "repair-dry", "Implement sample_stats.stats",
            "--workflow", "herdr-minimal-implementation", "--target-repo", str(self.target),
            "--acceptance-contract", str(paths["acceptance"]), "--functional-contract", str(paths["functional"]),
            "--scope-contract", str(paths["scope"]), "--repair-policy", str(paths["repair"]), "--json"],
            text=True, capture_output=True, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        dry = fleet_json.loads(run.stdout)
        self.assertEqual(dry["repair"]["policy_sha256"], repair.digest(self.policy))
        self.assertEqual(dry["effects"], [])
        self.assertFalse(self.runs.exists())

    def test_creation_refuses_incomplete_or_unsafe_policies_without_a_mission(self):
        cases = (({"scope_contract": None}, "physical scope contract"),
                 ({"functional_contract": None}, "functional contract"),
                 ({"repair_policy": policy(str(self.runs / "deliveries"))}, "outside the runs"),
                 ({"repair_policy": policy(str(self.delivery_root), max_attempts=0)}, "max_attempts"))
        for index, (overrides, message) in enumerate(cases):
            with self.subTest(message=message), self.assertRaisesRegex(repair.RepairPolicyError, message):
                self.create_repair(key=f"repair-refused-{index}", **overrides)
        self.assertFalse((self.runs / "missions").exists() and any((self.runs / "missions").iterdir()))

    def test_missions_without_policy_keep_their_creation_record(self):
        mid = self.create()
        current = fleet_mission.load_state(self.runs, mid)
        self.assertNotIn(repair.FIELD, current)
        self.assertNotIn("repair_policy", current)
        created = next(event for event in state.read_events(state.ledger_path(self.runs, mid))
                       if event["kind"] == "mission_created")
        self.assertNotIn(repair.FIELD, created["payload"])

    def test_policy_cannot_be_attached_after_creation_or_changed(self):
        mid = self.create()
        with self.assertRaises(state.MissionConflict):
            repair.freeze_policy(self.runs, mid, self.compiled["compiled_digest"], self.policy)
        repaired = self.create_repair(key="repair-pinned")
        other = policy(str(self.delivery_root), max_attempts=2)
        with self.assertRaises(state.MissionStateError):
            state.append_event(self.runs, repaired, kind=repair.EVENT, actor="CONTROL",
                idempotency_key="repair:policy:again", payload={"compiled_digest": self.compiled["compiled_digest"],
                    "policy_artifact_id": repair.digest(other)})

    def test_ledger_requires_scope_with_a_repair_pin(self):
        payload = {"feature": "repair-test", "objective_sha256": "a" * 64, "target_repo": str(self.target),
                   "base_sha": self.head, "workflow_digest": "b" * 64, "initial_risk": "low",
                   "herdr_profile": state.PHYSICAL_SCOPE_PROFILE_ID, "herdr_profile_sha256": "c" * 64,
                   repair.FIELD: "d" * 64}
        with self.assertRaisesRegex(state.MissionStateError, "physical scope contract"):
            state._validate_payload("mission_created", payload)

    def test_mission_run_dry_previews_the_policy_and_run_binds_it(self):
        from tests.test_mission_run import mission_run
        preview = mission_run.dry_run(feature="repair-dry", objective="Implement sample_stats.stats",
            workflow_name="herdr-minimal-implementation", target_repo=self.target, risk_override="auto",
            timeout_seconds=None, acceptance_contract=self.contract, functional_contract=synthetic_spec(),
            scope_contract=scope_contract(), repair_policy=self.policy, runs_dir=self.runs)
        self.assertEqual(preview["repair"]["policy_sha256"], repair.digest(self.policy))
        self.assertEqual(preview["effects"], [])
        self.assertFalse(self.runs.exists())
        with self.assertRaisesRegex(mission_run.MissionRunError, "physical scope contract"):
            mission_run.dry_run(feature="repair-dry", objective="Implement sample_stats.stats",
                workflow_name="herdr-minimal-implementation", target_repo=self.target, risk_override="auto",
                timeout_seconds=None, acceptance_contract=self.contract, functional_contract=synthetic_spec(),
                repair_policy=self.policy, runs_dir=self.runs)
        with mock.patch.object(mission_run, "drive_mission", side_effect=lambda runs, mid: {"mission_id": mid}):
            created = mission_run.create_and_drive(self.runs, feature="repair-cli",
                objective="Implement sample_stats.stats", workflow_name="herdr-minimal-implementation",
                target_repo=self.target, risk_override="auto", timeout_seconds=3600, allow_dirty_baseline=False,
                teardown=False, herdr_session="fixture", acceptance_contract=self.contract,
                functional_contract=synthetic_spec(), scope_contract=scope_contract(), repair_policy=self.policy)
        current = fleet_mission.load_state(self.runs, created["mission_id"])
        self.assertEqual(current[repair.FIELD], repair.digest(self.policy))
        self.assertEqual(MinimalBackend.calls, [])


if __name__ == "__main__":
    unittest.main()
