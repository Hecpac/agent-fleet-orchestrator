"""Permission tests use independently written contexts, not policy-built evidence."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_herdr_evidence as evidence
import fleet_herdr_permissions as permissions
from tests import test_fleet_herdr_evidence


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_fleet_herdr_evidence.EvidenceTests()
        self.fixture.setUp()

    def check(self, role, mutate=None, *, second_context=False):
        rows = copy.deepcopy(self.fixture.rows)
        context = rows[2]["payload"]
        context.update(model="gpt-6-astra" if role == "lead" else "gpt-5.6-sol",
            cwd="/private/tmp/candidate", approval_policy="never", sandbox_policy=(
                {"type": "workspace-write", "network_access": False,
                 "exclude_tmpdir_env_var": False, "exclude_slash_tmp": False}
                if role == "worker" else {"type": "read-only"}))
        expected = {**self.fixture.expected, "model": context["model"],
                    "permission_policy": permissions.policy(role, "/private/tmp/candidate")}
        if second_context:
            rows.insert(4, copy.deepcopy(rows[2]))
            context = rows[4]["payload"]
        if mutate:
            mutate(context)
        return evidence.verify_transcript(self.fixture.raw(rows), **expected)

    def test_each_role_and_compacted_context_is_attested(self):
        for role in ("lead", "worker", "reviewer", "verifier"):
            with self.subTest(role=role):
                proof = self.check(role, second_context=True)
                self.assertEqual((proof["status"], proof["contexts"]), ("attested", 2))
                self.assertEqual(proof["scope"], "recorded_codex_turn_configuration")

    def test_missing_or_changed_fields_in_any_context_fail(self):
        for field, value in (("cwd", "/private/tmp/foreign"), ("approval_policy", "on-request"),
                             ("sandbox_policy", {"type": "danger-full-access"}),
                             ("model", "gpt-5.5"), ("effort", "low")):
            for compacted in (False, True):
                for missing in (False, True):
                    with self.subTest(field=field, compacted=compacted, missing=missing):
                        def mutate(c):
                            c.pop(field) if missing else c.update({field: value})
                        with self.assertRaises(evidence.EvidenceError):
                            self.check("worker", mutate, second_context=compacted)

    def test_worker_network_roots_temp_flags_and_unknown_permissions_fail_closed(self):
        mutations = [lambda c: c["sandbox_policy"].update(network_access=True),
            lambda c: c["sandbox_policy"].update(network_access=0),
            lambda c: c["sandbox_policy"].update(writable_roots=["/private/tmp/foreign"]),
            lambda c: c["sandbox_policy"].update(exclude_tmpdir_env_var=True),
            lambda c: c["sandbox_policy"].pop("exclude_slash_tmp"),
            lambda c: c["sandbox_policy"].update(future_permission=True)]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(evidence.EvidenceError):
                self.check("worker", mutate)
        self.assertEqual(self.check("worker", lambda c: c["sandbox_policy"].update(writable_roots=[]))["status"], "attested")

    def test_readonly_roles_cannot_borrow_worker_sandbox(self):
        for role in ("lead", "reviewer", "verifier"):
            with self.subTest(role=role), self.assertRaises(evidence.EvidenceError):
                self.check(role, lambda c: c.update(sandbox_policy={"type": "workspace-write",
                    "network_access": False, "exclude_tmpdir_env_var": False, "exclude_slash_tmp": False}))

    def test_unversioned_historical_evidence_never_claims_attestation(self):
        self.assertEqual(evidence.verify_transcript(self.fixture.raw(), **self.fixture.expected)["status"], "not_attested")
        for expected in (None, {}, {"role": [], "cwd": "/tmp", "version": 1},
                         {**permissions.policy("lead", "/tmp"), "version": 2}):
            with self.subTest(expected=expected), self.assertRaises(permissions.PermissionError):
                permissions.attest([{}], expected)
