"""Adversarial contract fixtures only; no runtime or sandbox equivalence."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_artifacts
import fleet_herdr_binding as legacy
import fleet_herdr_binding_v2 as binding
import fleet_json
import fleet_mission_state as state
from tests import test_fleet_herdr_binding as fixtures


class StagedBindingTests(unittest.TestCase):
    def setUp(self):
        fixtures.BindingReceiverTests.setUp(self)
        self.attempt = {k: v for k, v in self.expected["binding"].items() if k != "run_id"}
        self.run_id = self.expected["binding"]["run_id"]
        self.frozen = binding.freeze_attempt(attempt=self.attempt, challenge="d" * 64,
            candidate=self.candidate, codex=self.expected["codex"],
            temporary_roots=self.expected["temporary_roots"], protected_roots=self.expected["protected_roots"],
            router_sha256="a" * 64, workflow_sha256="b" * 64)
        policy = fleet_json.loads(self.frozen)
        self.pins = {"frozen": binding.sha(self.frozen)}
        process = {"pid": 900001, "birth_identity": "synthetic-codex-start"}
        operation = str(uuid.uuid4())
        self.records, self.observed = {}, {}
        common = {"schema_version": 2, "attempt": self.attempt, "challenge": "d" * 64,
                  "frozen_policy_sha256": self.pins["frozen"]}

        def add(phase, fields):
            self.observed[phase] = copy.deepcopy(fields)
            self.records[phase] = fleet_json.canonical_bytes({**common, "kind": phase, **fields})
            self.pins[phase] = binding.sha(self.records[phase])

        add("launch", {"candidate": policy["candidate"], "codex": policy["codex"], "process": process,
                       "invocation": self.expected["invocation"], "environment": self.expected["environment"]})
        add("pre_exec", {"run_id": self.run_id, "launch_sha256": self.pins["launch"], "sequence": 1,
            "operation_id": operation, "process": process, "candidate": policy["candidate"],
            "effective_policy": policy["effective_constraint"], "invocation": self.expected["invocation"],
            "environment": self.expected["environment"], "input_hmac_sha256": "c" * 64,
            "fd_policy_sha256": "d" * 64, "os_sandbox": {"kind": "seatbelt",
                "profile_sha256": "e" * 64, "parameters_sha256": "f" * 64}})
        add("ack", {"pre_exec_sha256": self.pins["pre_exec"], "operation_id": operation, "sequence": 1})
        add("spawn", {"ack_sha256": self.pins["ack"], "pre_exec_sha256": self.pins["pre_exec"],
            "operation_id": operation, "process": {"pid": 900002, "birth_identity": "synthetic-child-start"}})
        snapshots = {name: fleet_artifacts.put_bytes(self.runs, self.mid,
            fleet_json.canonical_bytes({"synthetic": name}))["artifact_id"]
            for name in ("before", "after", "quiescence")}
        add("effects", {"spawn_sha256": self.pins["spawn"], "pre_exec_sha256": self.pins["pre_exec"],
            "operation_id": operation, "before_sha256": snapshots["before"], "after_sha256": snapshots["after"],
            "quiescence_sha256": snapshots["quiescence"], "protected_before": {k: "a" * 64
                for k in policy["protected_roots"]}, "protected_after": {k: "a" * 64 for k in policy["protected_roots"]},
            "checks": {k: True for k in binding.CHECKS}})

    def check(self, **overrides):
        args = dict(pins=self.pins, active_attempt=self.attempt, active_run_id=self.run_id,
                    observations=self.observed)
        args.update(overrides)
        return binding.validate_chain(self.frozen, self.records, **args)

    def mutate(self, phase, path, value, *, repin=True, observe=False):
        record = fleet_json.loads(self.records[phase])
        node = record
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        self.records[phase] = fleet_json.canonical_bytes(record)
        if repin:
            self.pins[phase] = binding.sha(self.records[phase])
        if observe:
            self.observed[phase] = {k: record[k] for k in binding.FIELDS[phase]}

    def test_complete_self_consistent_forgery_is_never_authority(self):
        result = self.check()
        self.assertEqual(result, {"schema_version": 2, "record_consistent": True,
            "authority": "none", "INTEGRATION_BINDING": "NOT_VERIFIED",
            "reason": "authenticated_runtime_channel_unavailable", "artifacts": self.pins})

    def test_freeze_survives_permissions_code_change_without_reinterpretation(self):
        with patch.object(legacy, "requested_policy", side_effect=AssertionError("must not rederive")):
            self.assertEqual(self.check()["authority"], "none")

    def test_requested_policy_cannot_contradict_frozen_constraint(self):
        policy = fleet_json.loads(self.frozen)
        policy["requested_policy"]["policy"]["sandbox_policy"]["network_access"] = True
        self.frozen = fleet_json.canonical_bytes(policy)
        self.pins["frozen"] = binding.sha(self.frozen)
        with self.assertRaisesRegex(binding.BindingError, "requested sandbox"):
            self.check()

    def test_tampered_bytes_and_noncanonical_json_fail(self):
        self.records["ack"] += b" "
        with self.assertRaisesRegex(binding.BindingError, "digest mismatch"):
            self.check()
        self.pins["ack"] = binding.sha(self.records["ack"])
        with self.assertRaisesRegex(binding.BindingError, "canonical JSON"):
            self.check()

    def test_missing_phase_cannot_release_operation(self):
        for phase in binding.PHASES:
            with self.subTest(phase=phase):
                saved = self.records.pop(phase)
                with self.assertRaisesRegex(binding.BindingError, "incomplete chain"):
                    self.check()
                self.records[phase] = saved

    def test_preexec_cannot_claim_future_pid_or_effects(self):
        for field in ("exec_pid", "observer_artifact_id", "ledger_event_sha256", "status"):
            with self.subTest(field=field):
                saved = dict(self.records), dict(self.pins)
                self.mutate("pre_exec", [field], "invented")
                with self.assertRaisesRegex(binding.BindingError, "fields mismatch"):
                    self.check()
                self.records, self.pins = saved

    def test_wrong_active_mission_generation_attempt_role_and_run_fail_after_recovery(self):
        for key in self.attempt:
            active = {**self.attempt, key: "lead" if key == "role" else str(uuid.uuid4())}
            with self.subTest(key=key), self.assertRaises(binding.BindingError):
                self.check(active_attempt=active)
        with self.assertRaisesRegex(binding.BindingError, "active run"):
            self.check(active_run_id=str(uuid.uuid4()))

    def test_policy_challenge_and_candidate_divergence_fail(self):
        for path, value in ((["frozen_policy_sha256"], "0" * 64), (["challenge"], "0" * 64),
                            (["candidate", "realpath"], str(self.root))):
            with self.subTest(path=path):
                saved = dict(self.records), dict(self.pins)
                self.mutate("pre_exec", path, value)
                with self.assertRaises(binding.BindingError):
                    self.check()
                self.records, self.pins = saved

    def test_alias_and_replaced_inode_fail_even_with_frozen_bytes(self):
        candidate = Path(self.candidate)
        moved = candidate.with_name("moved-candidate")
        candidate.rename(moved)
        candidate.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(binding.BindingError, "symlink or path alias"):
            self.check()
        candidate.unlink()
        candidate.mkdir()
        with self.assertRaisesRegex(binding.BindingError, "physical identity"):
            self.check()

    def test_unexpected_roots_rejected_even_if_observer_agrees(self):
        self.mutate("pre_exec", ["effective_policy", "sandbox_policy", "writable_roots"],
                    [str(self.root)], observe=True)
        with self.assertRaisesRegex(binding.BindingError, "effective policy"):
            self.check()

    def test_different_codex_version_rejected_even_if_observer_agrees(self):
        self.mutate("launch", ["codex", "version"], "codex-cli 0.154.0", observe=True)
        with self.assertRaisesRegex(binding.BindingError, "Codex image/version"):
            self.check()

    def test_binary_bytes_are_rechecked(self):
        self.binary.write_bytes(b"different binary")
        with self.assertRaisesRegex(binding.BindingError, "physical identity"):
            self.check()

    def test_environment_value_drift_cannot_hide_behind_redaction(self):
        changed = legacy.environment_commitment({**self.env, "HOME": "different"}, self.key)
        self.assertEqual(changed["sanitized_sha256"], self.expected["environment"]["sanitized_sha256"])
        self.mutate("pre_exec", ["environment"], changed)
        with self.assertRaisesRegex(binding.BindingError, "environment mismatch"):
            self.check()

    def test_raw_environment_or_argv_cannot_be_persisted(self):
        self.mutate("pre_exec", ["invocation", "redacted", "argv"], ["secret-value"], observe=True)
        with self.assertRaisesRegex(binding.BindingError, "unredacted argv"):
            self.check()

    def test_ack_from_previous_operation_rejected(self):
        self.mutate("ack", ["operation_id"], str(uuid.uuid4()), observe=True)
        with self.assertRaisesRegex(binding.BindingError, "operation id"):
            self.check()

    def test_profile_parameters_and_input_are_independently_pinned(self):
        for path in (["os_sandbox", "parameters_sha256"], ["input_hmac_sha256"], ["fd_policy_sha256"]):
            with self.subTest(path=path):
                saved = dict(self.records), dict(self.pins)
                self.mutate("pre_exec", path, "0" * 64)
                with self.assertRaises(binding.BindingError):
                    self.check()
                self.records, self.pins = saved

    def test_changed_protected_snapshot_or_missing_check_fails(self):
        self.mutate("effects", ["protected_after", "control"], "b" * 64, observe=True)
        with self.assertRaisesRegex(binding.BindingError, "protected roots changed"):
            self.check()

    def test_quiescence_cannot_be_omitted_or_numeric_true(self):
        for value in (False, 1, None):
            with self.subTest(value=value):
                self.mutate("effects", ["checks", "descendants_quiescent"], value, observe=True)
                with self.assertRaisesRegex(binding.BindingError, "canary checks"):
                    self.check()

    def test_observer_snapshot_must_exist_before_quarantine(self):
        effects = fleet_json.loads(self.records["effects"])
        fleet_artifacts.artifact_path(self.runs, self.mid, effects["quiescence_sha256"]).unlink()
        with self.assertRaises(fleet_artifacts.ArtifactError):
            binding.quarantine_chain(self.runs, self.frozen, self.records,
                ledger_event_sha256=self.expected["ledger_event_sha256"], pins=self.pins,
                active_attempt=self.attempt, active_run_id=self.run_id, observations=self.observed)

    def test_stdout_accepted_is_not_chain_evidence(self):
        self.records["effects"] = b"status=accepted\n"
        self.pins["effects"] = binding.sha(self.records["effects"])
        with self.assertRaises(binding.BindingError):
            self.check()

    def test_durable_quarantine_does_not_append_ledger_or_acceptance(self):
        ledger = state.ledger_path(self.runs, self.mid)
        before = ledger.read_bytes()
        result = binding.quarantine_chain(self.runs, self.frozen, self.records,
            ledger_event_sha256=self.expected["ledger_event_sha256"], pins=self.pins,
            active_attempt=self.attempt, active_run_id=self.run_id, observations=self.observed)
        envelope = fleet_json.loads(fleet_artifacts.get_bytes(self.runs, self.mid, result["artifact_id"]))
        self.assertEqual(envelope["authority"], "none")
        self.assertEqual(ledger.read_bytes(), before)
        for name, digest in self.pins.items():
            self.assertEqual(fleet_artifacts.get_bytes(self.runs, self.mid, digest),
                             self.frozen if name == "frozen" else self.records[name])


if __name__ == "__main__":
    unittest.main()
