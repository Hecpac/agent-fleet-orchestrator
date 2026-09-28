"""Owner Cycle mechanisms keep one home and the exact messages its journal retains."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

from tests import test_fleet_herdr_owner_cycle as owner_cycle_tests
from tests import test_fleet_herdr_owner_transport as transport_tests
import fleet_harness_contract
import fleet_harness_live_contract
import fleet_herdr_evidence as evidence
import fleet_herdr_owner_contract as contracts
import fleet_herdr_permissions as permissions
import fleet_herdr_profile as profiles
import fleet_herdr_owner_runtime as runtime
import fleet_herdr_work_packet as work
import fleet_json
import workflow_config

ROOT = Path(__file__).resolve().parents[1]


def rewrite(raw: bytes, mutate) -> bytes:
    rows = fleet_json.load_jsonl(raw)
    mutate(rows[2]["payload"])  # the bound turn_context
    return b"".join(fleet_json.canonical_bytes(row) + b"\n" for row in rows)


DRIFTS = {
    "cwd": lambda context: context.update(cwd="/elsewhere"),
    "approval_policy": lambda context: context.update(approval_policy="on-request"),
    "sandbox_policy": lambda context: context["sandbox_policy"].update(network_access=True),
}


class TerminalPermissionTests(owner_cycle_tests.CycleFixture):
    setup_delivery = owner_cycle_tests.RuntimeTests.setup_delivery

    def verify(self, mutate):
        _, backend, admission, response = self.setup_delivery()
        return runtime.verify_terminal(rewrite(response["transcript"], mutate), response["final"],
                                       contract=backend.contract, admission=admission)

    def assert_drift(self, mutate, key: str) -> None:
        with self.assertRaisesRegex(evidence.EvidenceError, rf"^recorded permissions differ from admission: {key}$"):
            self.verify(mutate)

    def test_each_drifting_key_is_named(self) -> None:
        for key, mutate in DRIFTS.items():
            with self.subTest(key=key):
                self.assert_drift(mutate, key)

    def test_the_first_drifting_key_is_reported(self) -> None:
        def both(first, second):
            return lambda context: (DRIFTS[first](context), DRIFTS[second](context))
        self.assert_drift(both("cwd", "sandbox_policy"), "cwd")
        self.assert_drift(both("approval_policy", "sandbox_policy"), "approval_policy")

    def test_empty_writable_roots_only_is_tolerated(self) -> None:
        self.assertEqual(self.verify(lambda c: c["sandbox_policy"].update(writable_roots=[]))["permissions"],
                         "recorded_configuration_attested")
        self.assert_drift(lambda c: c["sandbox_policy"].update(writable_roots=["/tmp"]), "sandbox_policy")

    def test_booleans_are_not_integers(self) -> None:
        def as_integer(context):
            flag = next(k for k, v in context["sandbox_policy"].items() if type(v) is bool)
            context["sandbox_policy"][flag] = int(context["sandbox_policy"][flag])
        self.assert_drift(as_integer, "sandbox_policy")


class ObservedPermissionTests(owner_cycle_tests.CycleFixture):
    setup_cycle = transport_tests.ObservedTransportTests.setup_cycle
    snapshot = transport_tests.ObservedTransportTests.snapshot

    def observe(self, mutate) -> list[str]:
        owner, runner, backend = self.setup_cycle()
        owner.tick(backend, now=1001)
        _, snapshot = self.snapshot(owner, runner)
        snapshot["transcript"] = rewrite(snapshot["transcript"], mutate)
        owner.tick(backend, now=1002)
        attempt = owner.load()["attempts"][0]
        self.assertIsNone(attempt["native_binding"])
        return [error["detail"] for error in attempt["observation_errors"]]

    def test_native_drift_is_retained_with_the_first_key(self) -> None:
        for key in DRIFTS:
            with self.subTest(key=key):
                self.assertIn("ContractError: observed native permission drift: " + key,
                              self.observe(DRIFTS[key]))
        details = self.observe(lambda c: (DRIFTS["cwd"](c), DRIFTS["sandbox_policy"](c)))
        self.assertIn("ContractError: observed native permission drift: cwd", details)


class OwnerWorkProjectionTests(owner_cycle_tests.CycleFixture):
    def test_other_profiles_cannot_project_owner_work(self) -> None:
        for workflow in ("herdr-implementation", "herdr-research-implementation"):
            with self.subTest(workflow=workflow):
                with self.assertRaisesRegex(work.WorkPacketError, r"^owner work projection requires sol_minimal_v1$"):
                    self.prepare(compiled=workflow_config.compile_path(ROOT / "workflows" / f"{workflow}.yaml"))

    def test_projection_binds_the_minimal_profile(self) -> None:
        prepared = self.prepare(functional_contract=None)
        self.assertEqual(prepared["execution_envelope"]["profile"], "sol_minimal_v1")
        self.assertEqual(prepared["sources"]["permissions"]["version"], 4)
        self.assertIs(work.verify(prepared), prepared)


class OwnerCommonHomeTests(unittest.TestCase):
    OWNER_MODULES = sorted((ROOT / "scripts").glob("fleet_herdr_owner_*.py"))

    def test_version_families_follow_their_lanes(self) -> None:
        self.assertEqual(contracts.HARNESS_CONTRACT_VERSIONS,
                         {fleet_harness_contract.VERSION, fleet_harness_live_contract.VERSION})
        self.assertEqual(contracts.DELIVERED_ADMISSION_VERSIONS,
                         {"owner-cycle-admission-v1", "owner-cycle-admission-v3", "owner-cycle-admission-v4"})

    def test_each_family_and_drift_rule_has_one_home(self) -> None:
        homes = {"harness contract set": [], "delivered admission set": [], "writable_roots rule": []}
        for path in self.OWNER_MODULES:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                values = ({e.value for e in node.elts if isinstance(e, ast.Constant)}
                          if isinstance(node, ast.Set) else set())
                if contracts.HARNESS_CONTRACT_VERSIONS <= values:
                    homes["harness contract set"].append(path.name)
                if contracts.DELIVERED_ADMISSION_VERSIONS <= values:
                    homes["delivered admission set"].append(path.name)
                if isinstance(node, ast.Constant) and node.value == "writable_roots":
                    homes["writable_roots rule"].append(path.name)
        self.assertEqual(homes, {"harness contract set": ["fleet_herdr_owner_contract.py"],
                                 "delivered admission set": ["fleet_herdr_owner_contract.py"],
                                 "writable_roots rule": ["fleet_herdr_owner_contract.py"] * 2})

    def test_owner_work_profile_is_declared_once(self) -> None:
        self.assertIs(profiles.OWNER_WORK_PROFILE, profiles.MINIMAL)
        self.assertEqual(profiles.OWNER_WORK_PROFILE.writer_instance, contracts.PROFILE["writer"])
        self.assertEqual(permissions.MINIMAL_VERSION, profiles.OWNER_WORK_PROFILE.permissions_policy_version)
        tree = ast.parse((ROOT / "scripts" / "fleet_herdr_work_packet.py").read_text())
        named = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                 and isinstance(node.value, ast.Name) and node.value.id in {"profiles", "permissions"}}
        self.assertFalse(named & {"MINIMAL", "MINIMAL_VERSION"})
        self.assertIn("OWNER_WORK_PROFILE", named)


if __name__ == "__main__":
    unittest.main()
