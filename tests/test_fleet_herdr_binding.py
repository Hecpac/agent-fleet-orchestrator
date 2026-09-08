"""Synthetic receiver tests; no Herdr mock, Codex process, or sandbox claim."""
import copy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_artifacts
import fleet_herdr_binding as binding
import fleet_json
import fleet_mission_state as state


class BindingReceiverTests(unittest.TestCase):
    def setUp(self):
        # CONTROL must not sit under /tmp, even for this synthetic contract.
        parent = Path(os.environ.get("FLEET_TEST_CONTROL_PARENT", str(Path.home()))).resolve()
        self.temp = tempfile.TemporaryDirectory(prefix=".fleet-binding-test-", dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("candidate", "control", "runs", "role-tmp"):
            (self.root / name).mkdir(mode=0o700)
        self.runs = self.root / "runs"
        self.mid = str(uuid.uuid4())
        self.candidate = str(self.root / "candidate")
        event, _ = state.append_event(self.runs, self.mid, kind="mission_created", actor="CONTROL",
            idempotency_key="fixture-created", payload={"feature": "binding-fixture",
                "objective_sha256": "a" * 64, "target_repo": self.candidate, "base_sha": "b" * 40,
                "workflow_digest": "c" * 64, "initial_risk": "low"})
        observer = fleet_artifacts.put_bytes(self.runs, self.mid, b'{"synthetic_observer":true}')
        self.binary = self.root / "synthetic-codex-image"
        self.binary.write_bytes(b"fixture bytes, never executed\n")
        self.key = b"synthetic-controller-key-32-bytes!"
        self.env = {"TMPDIR": str(self.root / "role-tmp"), "HOME": str(self.root),
                    "SYNTHETIC_SECRET": "do-not-persist-fixture-secret"}
        self.argv = [str(self.binary), "--sandbox", "workspace-write", "--token=fixture-secret"]
        self.config = {"sandbox_mode": "workspace-write", "synthetic_secret": "fixture-secret"}
        self.expected = {
            "binding": {"mission_id": self.mid, "run_id": str(uuid.uuid4()),
                        "generation": str(uuid.uuid4()), "role": "worker", "attempt_id": str(uuid.uuid4())},
            "challenge": "d" * 64,
            "candidate": binding.path_identity(self.candidate, directory=True),
            "codex": {"image": binding.path_identity(str(self.binary), directory=False),
                      "version": "codex-cli 0.153.0"},
            "invocation": binding.invocation_commitment(self.argv, self.config, self.key),
            "environment": binding.environment_commitment(self.env, self.key),
            "process": {"codex_pid": 900001, "codex_start_identity": "synthetic-start-1",
                        "exec_pid": 900002, "exec_start_identity": "synthetic-start-2"},
            "operation_sha256": "e" * 64, "observer_artifact_id": observer["artifact_id"],
            "ledger_event_sha256": event["event_sha256"],
            "os_sandbox": {"kind": "seatbelt", "profile_sha256": "f" * 64},
            "requested_policy": binding.requested_policy(self.candidate),
            "temporary_roots": {"slash_tmp": str(Path("/tmp").resolve()), "tmpdir": self.env["TMPDIR"]},
            "protected_roots": {"control": str(self.root / "control"), "runs": str(self.runs),
                "cas": str(fleet_artifacts.store_path(self.runs, self.mid)),
                "ledger": str(state.mission_root(self.runs, self.mid))},
        }
        self.record = {k: copy.deepcopy(self.expected[k]) for k in binding.OBSERVED_FIELDS}
        self.record.update(schema_version=1, kind="effective-policy-attestation",
            requested_policy_sha256=hashlib.sha256(fleet_json.canonical_bytes(self.expected["requested_policy"])).hexdigest(),
            effective_policy={"sandbox_policy": {"type": "workspace-write", "network_access": False,
                "exclude_tmpdir_env_var": False, "exclude_slash_tmp": False, "writable_roots": []},
                "approval_policy": "never", "product_writable_roots": [self.candidate],
                "temporary_roots": copy.deepcopy(self.expected["temporary_roots"])})

    def check(self, record=None, expected=None):
        raw = fleet_json.canonical_bytes(self.record if record is None else record)
        return binding.validate_record(raw, expected=self.expected if expected is None else expected,
                                       pinned_sha256=hashlib.sha256(raw).hexdigest())

    def test_consistent_forgery_and_recomputed_hash_never_authenticate_emitter(self):
        proof = self.check()
        self.assertTrue(proof["record_consistent"])
        self.assertEqual(proof["INTEGRATION_BINDING"], "NOT_VERIFIED")
        self.assertEqual(proof["reason"], "trusted_exec_hook_unavailable")
        for field in ("status", "origin", "trusted", "signature"):
            record = copy.deepcopy(self.record)
            record[field] = "VERIFIED"
            with self.subTest(field=field), self.assertRaises(binding.BindingError):
                self.check(record)

    def test_modified_bytes_do_not_match_external_pin(self):
        raw = fleet_json.canonical_bytes(self.record)
        with self.assertRaisesRegex(binding.BindingError, "record digest"):
            binding.validate_record(raw + b" ", expected=self.expected,
                                    pinned_sha256=hashlib.sha256(raw).hexdigest())

    def test_stdout_accepted_is_not_an_attestation(self):
        raw = b"status=accepted\n"
        before = state.read_events(state.ledger_path(self.runs, self.mid), expected_mission_id=self.mid)
        with self.assertRaises(binding.BindingError):
            binding.quarantine_record(self.runs, raw, expected=self.expected,
                                      pinned_sha256=hashlib.sha256(raw).hexdigest())
        self.assertEqual(state.read_events(state.ledger_path(self.runs, self.mid), expected_mission_id=self.mid), before)

    def test_mission_run_generation_role_and_attempt_are_independent_bindings(self):
        for field in binding.BINDING_FIELDS:
            record = copy.deepcopy(self.record)
            record["binding"][field] = "lead" if field == "role" else str(uuid.uuid4())
            with self.subTest(field=field), self.assertRaisesRegex(binding.BindingError, "binding mismatch"):
                self.check(record)

    def test_old_attempt_or_process_cannot_be_reused_after_recovery(self):
        for field in ("attempt_id", "generation", "run_id"):
            current = copy.deepcopy(self.expected)
            current["binding"][field] = str(uuid.uuid4())
            with self.subTest(field=field), self.assertRaises(binding.BindingError):
                self.check(expected=current)
        for field in ("codex_start_identity", "exec_start_identity"):
            current = copy.deepcopy(self.expected)
            current["process"][field] = "same-pid-different-birth"
            with self.subTest(field=field), self.assertRaises(binding.BindingError):
                self.check(expected=current)
        current = copy.deepcopy(self.expected)
        current["challenge"] = "0" * 64
        with self.assertRaises(binding.BindingError):
            self.check(expected=current)

    def test_noncanonical_identity_aliases_are_rejected_even_in_both_inputs(self):
        for field in ("mission_id", "run_id", "generation", "attempt_id"):
            current = copy.deepcopy(self.expected)
            current["binding"][field] = "urn:uuid:" + current["binding"][field]
            record = copy.deepcopy(self.record)
            record["binding"] = copy.deepcopy(current["binding"])
            with self.subTest(field=field), self.assertRaisesRegex(binding.BindingError, "noncanonical"):
                self.check(record, expected=current)

    def test_policy_digest_and_grants_reject_drift_even_when_resealed(self):
        mutations = [lambda r: r.update(requested_policy_sha256="0" * 64),
            lambda r: r["effective_policy"].update(approval_policy="on-request"),
            lambda r: r["effective_policy"]["sandbox_policy"].update(network_access=True),
            lambda r: r["effective_policy"]["sandbox_policy"].update(network_access=0),
            lambda r: r["effective_policy"]["sandbox_policy"].update(writable_roots=[str(self.root)]),
            lambda r: r["effective_policy"].update(product_writable_roots=[self.candidate, str(self.root)]),
            lambda r: r["effective_policy"]["sandbox_policy"].update(exclude_slash_tmp=True),
            lambda r: r["effective_policy"]["temporary_roots"].update(tmpdir=str(self.root))]
        for i, mutate in enumerate(mutations):
            record = copy.deepcopy(self.record)
            mutate(record)
            with self.subTest(mutation=i), self.assertRaises(binding.BindingError):
                self.check(record)

    def test_binary_version_path_hash_or_inode_drift_is_rejected(self):
        for field, value in (("version", "codex-cli 0.154.0"), ("image", {})):
            record = copy.deepcopy(self.record)
            record["codex"][field] = value
            with self.subTest(field=field), self.assertRaises(binding.BindingError):
                self.check(record)
        self.binary.write_bytes(b"changed binary")
        with self.assertRaisesRegex(binding.BindingError, "Codex image"):
            self.check()

    def test_candidate_replacement_at_same_path_is_rejected(self):
        Path(self.candidate).rename(self.root / "old-candidate")
        Path(self.candidate).mkdir()
        with self.assertRaisesRegex(binding.BindingError, "candidate inode"):
            self.check()

    def test_symlink_absolute_alias_and_traversal_are_not_normalized_away(self):
        link = self.root / "candidate-link"
        link.symlink_to(self.candidate, target_is_directory=True)
        paths = [str(link), self.candidate + "/../candidate", self.candidate + "/.",
                 "/" + self.candidate, self.candidate + "//"]
        for value in paths:
            with self.subTest(value=value), self.assertRaises(binding.BindingError):
                binding.path_identity(value, directory=True)
        self.binary.rename(self.root / "original-image")
        self.binary.symlink_to(self.root / "original-image")
        with self.assertRaises(binding.BindingError):
            self.check()

    def test_environment_value_drift_survives_redaction_but_changes_commitment(self):
        changed = {**self.env, "SYNTHETIC_SECRET": "different-secret"}
        observed = binding.environment_commitment(changed, self.key)
        self.assertEqual(observed["sanitized_sha256"], self.expected["environment"]["sanitized_sha256"])
        self.assertNotEqual(observed["values_hmac_sha256"], self.expected["environment"]["values_hmac_sha256"])
        current = copy.deepcopy(self.expected)
        current["environment"] = observed
        with self.assertRaisesRegex(binding.BindingError, "environment mismatch"):
            self.check(expected=current)
        for env in ({**self.env, "ADDED": "1"}, {k: v for k, v in self.env.items() if k != "HOME"}):
            current["environment"] = binding.environment_commitment(env, self.key)
            with self.assertRaises(binding.BindingError):
                self.check(expected=current)

    def test_argv_config_drift_and_redaction(self):
        raw = fleet_json.canonical_bytes(self.record)
        self.assertNotIn(b"fixture-secret", raw)
        self.assertNotIn(b"do-not-persist", raw)
        self.assertNotIn(self.key, raw)
        for argv, config in ((self.argv + ["--add-dir", str(self.root)], self.config),
                             (self.argv, {**self.config, "sandbox_mode": "danger-full-access"})):
            current = copy.deepcopy(self.expected)
            current["invocation"] = binding.invocation_commitment(argv, config, self.key)
            with self.assertRaisesRegex(binding.BindingError, "invocation mismatch"):
                self.check(expected=current)

    def test_reanchoring_unredacted_invocation_does_not_permit_secret_persistence(self):
        for section in ("argv", "config"):
            record = copy.deepcopy(self.record)
            redacted = record["invocation"]["redacted"]
            if section == "argv":
                redacted["argv"].append("plaintext-secret")
            else:
                redacted["config"]["sandbox_mode"] = "plaintext-secret"
            record["invocation"]["sanitized_sha256"] = hashlib.sha256(fleet_json.canonical_bytes(redacted)).hexdigest()
            current = copy.deepcopy(self.expected)
            current["invocation"] = copy.deepcopy(record["invocation"])
            with self.subTest(section=section), self.assertRaisesRegex(binding.BindingError, "unredacted"):
                self.check(record, expected=current)

    def test_inconsistent_controller_policy_is_not_used_as_a_new_permission_grant(self):
        current = copy.deepcopy(self.expected)
        current["requested_policy"]["policy"]["sandbox_policy"]["network_access"] = True
        record = copy.deepcopy(self.record)
        record["requested_policy_sha256"] = hashlib.sha256(fleet_json.canonical_bytes(current["requested_policy"])).hexdigest()
        record["effective_policy"]["sandbox_policy"]["network_access"] = True
        with self.assertRaisesRegex(binding.BindingError, "requested Fleet policy"):
            self.check(record, expected=current)

    def test_control_under_candidate_or_declared_temporaries_is_rejected(self):
        for value in (self.candidate, self.env["TMPDIR"], str(Path("/tmp").resolve())):
            current = copy.deepcopy(self.expected)
            current["protected_roots"]["control"] = value
            with self.subTest(root=value), self.assertRaisesRegex(binding.BindingError, "overlap"):
                self.check(expected=current)

    def test_os_profile_operation_observer_and_ledger_cannot_be_substituted(self):
        for field in ("os_sandbox", "process", "operation_sha256", "observer_artifact_id", "ledger_event_sha256"):
            record = copy.deepcopy(self.record)
            record[field] = "0" * 64
            with self.subTest(field=field), self.assertRaises(binding.BindingError):
                self.check(record)

    def test_missing_unknown_schema_and_duplicate_keys_are_rejected(self):
        for field in binding.RECORD_FIELDS:
            record = copy.deepcopy(self.record)
            record.pop(field)
            with self.subTest(missing=field), self.assertRaises(binding.BindingError):
                self.check(record)
        for version in (True, 0, 2, "1"):
            record = copy.deepcopy(self.record)
            record["schema_version"] = version
            with self.subTest(version=version), self.assertRaises(binding.BindingError):
                self.check(record)
        raw = b'{"kind":"one","kind":"two"}'
        with self.assertRaises(binding.BindingError):
            binding.validate_record(raw, expected=self.expected, pinned_sha256=hashlib.sha256(raw).hexdigest())

    def quarantine(self, expected=None):
        raw = fleet_json.canonical_bytes(self.record)
        return binding.quarantine_record(self.runs, raw, expected=self.expected if expected is None else expected,
                                         pinned_sha256=hashlib.sha256(raw).hexdigest())

    def test_durable_quarantine_preserves_ledger_and_never_grants_authority(self):
        before = state.read_events(state.ledger_path(self.runs, self.mid), expected_mission_id=self.mid)
        receipt = self.quarantine()
        self.assertEqual(receipt["INTEGRATION_BINDING"], "NOT_VERIFIED")
        self.assertEqual(receipt["authority"], "none")
        self.assertEqual(state.read_events(state.ledger_path(self.runs, self.mid), expected_mission_id=self.mid), before)
        raw = fleet_artifacts.get_bytes(self.runs, self.mid, receipt["artifact_id"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), receipt["record_sha256"])
        self.assertNotIn(b"fixture-secret", raw)
        self.assertEqual(self.quarantine(), receipt)

    def test_modified_or_symlinked_cas_cannot_be_read_as_receipt(self):
        receipt = self.quarantine()
        path = fleet_artifacts.artifact_path(self.runs, self.mid, receipt["artifact_id"])
        path.write_bytes(b"forged")
        with self.assertRaises(fleet_artifacts.ArtifactError):
            fleet_artifacts.get_bytes(self.runs, self.mid, receipt["artifact_id"])
        path.unlink()
        path.symlink_to(self.binary)
        with self.assertRaises(fleet_artifacts.ArtifactError):
            fleet_artifacts.get_bytes(self.runs, self.mid, receipt["artifact_id"])

    def test_quarantine_requires_real_ledger_and_observer_links(self):
        for field in ("ledger_event_sha256", "observer_artifact_id"):
            old = self.record[field]
            self.record[field] = "0" * 64
            current = copy.deepcopy(self.expected)
            current[field] = "0" * 64
            try:
                with self.subTest(field=field), self.assertRaises((binding.BindingError, fleet_artifacts.ArtifactError)):
                    self.quarantine(current)
            finally:
                self.record[field] = old


if __name__ == "__main__":
    unittest.main()
