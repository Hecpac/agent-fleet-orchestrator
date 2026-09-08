"""Real ledger/archive recovery regressions; no mocked validation or providers."""
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_acceptance
import fleet_herdr_archive as archive
import fleet_herdr_mission as driver
import fleet_json
import fleet_mission
import fleet_mission_state as state
from tests import test_fleet_herdr_archive


class HerdrFinalizationTests(unittest.TestCase):
    def fixture(self, *, unsafe_plan=False):
        helper = test_fleet_herdr_archive.HerdrSnapshotTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        def mutate(stage, context):
            if stage == "plan" and unsafe_plan:
                context["sandbox_policy"] = {"type": "danger-full-access"}
        mid, roles, backend = helper.archive_fixture(mutate_context=mutate,
            runtime_options={"herdr_session": "offline-fixture"})
        return helper, mid, roles, backend

    def historical_archive(self, helper, mid, roles, backend, *, schema=2):
        """Serialize the historical v2 contract without bypassing any validator.

        The initial Plan lives in CAS/ledger but not role-results, exactly as in
        real v2 archives. The production reader must accept this fixture before
        a recovery test can exercise the stricter completion gate.
        """
        root = helper.runs / "missions" / mid
        current = fleet_mission.load_state(helper.runs, mid)
        contents = {name: (root / source).read_bytes() for name, source in [
            ("compiled-workflow.json", "compiled-workflow.json"), ("runtime-options.json", "runtime-options.json"),
            ("creation-request.json", "creation-request.json"), ("objective.txt", "objective.txt"),
            ("ledger.jsonl", "mission.jsonl"), ("candidate-freeze.json", "candidate-freeze.json")]}
        contents.update({"artifacts/" + p.name: p.read_bytes() for p in (root / "artifacts").iterdir()})
        contents["backend.json"] = state.canonical_bytes(backend) + b"\n"
        contents["role-results.json"] = state.canonical_bytes(roles) + b"\n"
        for role, result in roles.items():
            contents[f"results/{role}.txt"] = contents["artifacts/" + result["artifact_id"]]
        frozen = fleet_json.loads(contents["candidate-freeze.json"])
        contents["writer/final-tree.tar"] = contents["artifacts/" + frozen["tree_artifact_id"]]
        contents["writer/change.patch"] = contents["artifacts/" + frozen["patch_artifact_id"]]
        contract = fleet_json.loads(contents["runtime-options.json"])["acceptance_contract"]
        acceptance = fleet_acceptance.evaluate(contract, contents["writer/final-tree.tar"],
            mission_id=mid, final_sha=frozen["tree_sha"])
        contents["acceptance-result.json"] = state.canonical_bytes(acceptance) + b"\n"
        index = {"schema_version": schema, "backend": "herdr", "mission_id": mid,
            "compiled_digest": current["compiled_digest"], "ledger_head": current["head_sha256"],
            "base_sha": current["base_sha"], "final_tree_sha": frozen["tree_sha"],
            "entries": {name: {"sha256": state.artifact_id(raw), "bytes": len(raw)} for name, raw in contents.items()}}
        if schema == 3:
            index["permissions_policy_version"] = 1
        destination = root / "herdr-archive"
        destination.mkdir(mode=0o700)
        for name, raw in {**contents, "archive-index.json": state.canonical_bytes(index) + b"\n"}.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(raw)
            path.chmod(0o600)
        return destination / "archive-index.json"

    def guarded_drive(self, helper, mid):
        with mock.patch.object(driver._Driver, "backend", side_effect=AssertionError("no runtime")), \
                mock.patch.object(driver._Driver, "prepare_candidate", side_effect=AssertionError("no candidate effect")):
            return driver.drive(helper.runs, mid)

    def anchor(self, helper, mid, proof):
        state.append_event(helper.runs, mid, kind="archive_created", actor="CONTROL",
            idempotency_key="herdr:archive", payload={"path": proof["path"],
            "sha256": proof["index_sha256"], "mode": "herdr"})

    def test_staged_v2_invalid_plan_cannot_finalize_directly_or_on_resume(self):
        helper, mid, roles, backend = self.fixture(unsafe_plan=True)
        path = self.historical_archive(helper, mid, roles, backend)
        raw = path.read_bytes()
        proof = archive.verify(helper.runs, mid, require_anchor=False)
        self.assertEqual(proof["permissions"]["status"], "not_attested")
        with self.assertRaisesRegex(archive.HerdrArchiveError, "herdr:plan permission"):
            archive.verify(helper.runs, mid, require_anchor=False, attest_permissions=True)
        with self.assertRaisesRegex(archive.HerdrArchiveError, "cannot authorize new completion"):
            driver._Driver(helper.runs, mid).complete_archive(archive, proof)
        before = state.ledger_path(helper.runs, mid).read_bytes()
        with self.assertRaisesRegex(archive.HerdrArchiveError, "cannot authorize new completion"):
            self.guarded_drive(helper, mid)
        self.assertEqual(state.ledger_path(helper.runs, mid).read_bytes(), before)
        self.assertEqual(path.read_bytes(), raw)
        current = fleet_mission.load_state(helper.runs, mid)
        self.assertEqual(current["status"], "completing")
        self.assertEqual(current["herdr_finalization_policy"]["minimum_archive_schema_version"], 3)

    def test_downgrade_preanchor_cannot_erase_frozen_policy(self):
        helper, mid, roles, backend = self.fixture(unsafe_plan=True)
        path = self.historical_archive(helper, mid, roles, backend, schema=3)
        with self.assertRaisesRegex(archive.HerdrArchiveError, "herdr:plan permission"):
            self.guarded_drive(helper, mid)
        policy = fleet_mission.load_state(helper.runs, mid)["herdr_finalization_policy"]
        index = fleet_json.loads(path.read_bytes())
        index["schema_version"] = 2
        index.pop("permissions_policy_version")
        path.write_bytes(state.canonical_bytes(index) + b"\n")
        self.assertTrue(archive.verify(helper.runs, mid, require_anchor=False)["staged_valid"])
        with self.assertRaisesRegex(archive.HerdrArchiveError, "cannot authorize new completion"):
            self.guarded_drive(helper, mid)
        current = fleet_mission.load_state(helper.runs, mid)
        self.assertEqual(current["herdr_finalization_policy"], policy)
        self.assertEqual(current["status"], "completing")

    def test_anchored_nonterminal_v2_also_cannot_append_terminal(self):
        helper, mid, roles, backend = self.fixture(unsafe_plan=True)
        self.historical_archive(helper, mid, roles, backend)
        self.anchor(helper, mid, archive.verify(helper.runs, mid, require_anchor=False))
        with self.assertRaisesRegex(archive.HerdrArchiveError, "cannot authorize new completion"):
            self.guarded_drive(helper, mid)
        current = fleet_mission.load_state(helper.runs, mid)
        self.assertEqual(current["status"], "archived")
        self.assertIsNone(current["terminal"])

    def test_valid_v3_recovers_and_terminal_replay_does_not_mutate_evidence(self):
        helper, mid, roles, backend = self.fixture()
        staged = archive.create(helper.runs, mid, helper.repo, roles, backend)
        with self.assertRaisesRegex(archive.HerdrArchiveError, "durable finalization policy"):
            archive.verify(helper.runs, mid, require_anchor=False, for_completion=True)
        result = self.guarded_drive(helper, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["archive"]["permissions"]["runs"], 5)
        self.assertEqual(result["archive"]["index_sha256"], staged["index_sha256"])
        current = fleet_mission.load_state(helper.runs, mid)
        self.assertEqual(result["archive"]["finalization_policy_event_sha256"],
                         current["herdr_finalization_policy"]["event_sha256"])
        root = helper.runs / "missions" / mid
        before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        self.assertEqual(self.guarded_drive(helper, mid)["status"], "succeeded")
        self.assertEqual(driver._Driver(helper.runs, mid).complete_archive(None, {})["status"], "succeeded")
        self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_terminal_historical_v2_stays_readable_without_policy_or_rewrite(self):
        helper, mid, roles, backend = self.fixture(unsafe_plan=True)
        self.historical_archive(helper, mid, roles, backend)
        self.anchor(helper, mid, archive.verify(helper.runs, mid, require_anchor=False))
        # Represent a pre-fix terminal ledger; this is not a new driver success.
        state.append_terminal(helper.runs, mid, status="succeeded", reason="historical fixture",
                              idempotency_key="historical-terminal")
        before = state.ledger_path(helper.runs, mid).read_bytes()
        self.assertTrue(archive.verify(helper.runs, mid)["valid"])
        self.assertEqual(archive.verify(helper.runs, mid)["permissions"]["status"], "not_attested")
        self.assertEqual(self.guarded_drive(helper, mid)["status"], "succeeded")
        self.assertEqual(driver._Driver(helper.runs, mid).complete_archive(None, {})["status"], "succeeded")
        self.assertEqual(state.ledger_path(helper.runs, mid).read_bytes(), before)
        self.assertNotIn("herdr_finalization_policy", fleet_mission.load_state(helper.runs, mid))

    def test_valid_anchored_v3_recovers_with_one_anchor_and_one_policy(self):
        helper, mid, roles, backend = self.fixture()
        proof = archive.create(helper.runs, mid, helper.repo, roles, backend)
        self.anchor(helper, mid, proof)
        result = self.guarded_drive(helper, mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["archive"]["index_sha256"], proof["index_sha256"])
        events = state.read_events(state.ledger_path(helper.runs, mid))
        for kind in ("archive_created", "herdr_finalization_policy_frozen", "mission_terminal"):
            self.assertEqual(sum(e["kind"] == kind for e in events), 1)

    def test_finalization_policy_is_immutable_and_controller_bound(self):
        helper, mid, _, _ = self.fixture()
        controller = driver._Driver(helper.runs, mid)
        compiled_digest = fleet_mission.load_state(helper.runs, mid)["compiled_digest"]
        initial = {"compiled_digest": compiled_digest, "minimum_archive_schema_version": 3,
                   "permissions_policy_version": 1, "required_turns": 5}
        for actor, digest in (("worker", compiled_digest), ("CONTROL", "0" * 64)):
            with self.subTest(actor=actor, digest=digest), self.assertRaisesRegex(state.MissionStateError, "authority mismatch"):
                state.append_event(helper.runs, mid, kind="herdr_finalization_policy_frozen", actor=actor,
                    idempotency_key="foreign-policy", payload={**initial, "compiled_digest": digest})
        expected = controller.freeze_finalization_policy()
        self.assertEqual(controller.freeze_finalization_policy(), expected)
        payload = {k: v for k, v in expected.items() if k != "event_sha256"}
        before = state.ledger_path(helper.runs, mid).read_bytes()
        for changes in ({}, {"minimum_archive_schema_version": 2}, {"permissions_policy_version": 0},
                        {"required_turns": 4}, {"compiled_digest": "0" * 64}):
            with self.subTest(changes=changes), self.assertRaises(state.MissionStateError):
                state.append_event(helper.runs, mid, kind="herdr_finalization_policy_frozen", actor="CONTROL",
                    idempotency_key="changed-policy", payload={**payload, **changes})
        self.assertEqual(state.ledger_path(helper.runs, mid).read_bytes(), before)
