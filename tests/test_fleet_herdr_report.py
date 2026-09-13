import copy
from pathlib import Path
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_artifacts
import fleet_herdr_archive as archive
import fleet_json
import fleet_mission
import fleet_mission_state as state
import fleet_report
import fleet_herdr_report
import fleet_herdr_versions as versions
from tests import test_fleet_herdr_archive


class HerdrReportTests(unittest.TestCase):
    def official_report_fixture(self):
        helper, mid, _, _ = self.fixture()
        compiled, current = fleet_mission.load_mission_compiled(helper.runs, mid, mode="read")
        root = helper.runs / "missions" / mid
        backend = {"schema_version": 3, "backend_version": versions.HERDR_VERSION,
            "runtime_contract": dict(versions.OFFICIAL_CONTRACT), "mission_id": mid,
            "compiled_digest": compiled["compiled_digest"], "submissions": {}}
        for name, raw in (("herdr-backend.json", fleet_json.canonical_bytes(backend) + b"\n"),
                          ("herdr-runtime-contract.json", versions.anchor_bytes(backend))):
            path = root / name
            path.write_bytes(raw)
            path.chmod(0o600)
        current = copy.deepcopy(current)
        for admission in current["admissions"].values():
            result = fleet_json.loads(fleet_artifacts.get_bytes(helper.runs, mid, admission["result"]["artifact_id"]))
            rows = fleet_json.load_jsonl(fleet_artifacts.get_bytes(helper.runs, mid,
                result["evidence"]["transcript_artifact_id"]))
            rows[0]["payload"]["cli_version"] = versions.CODEX_VERSION
            result["evidence"]["runtime_contract"] = dict(versions.OFFICIAL_CONTRACT)
            self.repin_report_result(helper, mid, admission, result, rows)
        return helper, mid, compiled, current

    @staticmethod
    def repin_report_result(helper, mid, admission, result, rows):
        transcript = b"".join(fleet_json.canonical_bytes(r) + b"\n" for r in rows)
        pin = fleet_artifacts.put_bytes(helper.runs, mid, transcript)["artifact_id"]
        result["evidence"].update(transcript_artifact_id=pin, transcript_sha256=pin)
        admission["result"]["artifact_id"] = fleet_artifacts.put_bytes(helper.runs, mid,
            fleet_json.canonical_bytes(result))["artifact_id"]

    def test_official_report_observes_only_matching_version_and_contract(self):
        helper, mid, compiled, current = self.official_report_fixture()
        events = state.read_events(state.ledger_path(helper.runs, mid))
        self.assertEqual(fleet_herdr_report.build_report(helper.runs, current, compiled, events)
            ["provider_observation"]["observed_runs"], 5)
        for mutation in ("version", "missing_version", "missing_contract", "changed_contract", "typed_contract"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(current)
                admission = next(iter(changed["admissions"].values()))
                result = fleet_json.loads(fleet_artifacts.get_bytes(helper.runs, mid,
                    admission["result"]["artifact_id"]))
                rows = fleet_json.load_jsonl(fleet_artifacts.get_bytes(helper.runs, mid,
                    result["evidence"]["transcript_artifact_id"]))
                if mutation == "version":
                    rows[0]["payload"]["cli_version"] = "0.153.0"
                elif mutation == "missing_version":
                    rows[0]["payload"].pop("cli_version")
                elif mutation == "missing_contract":
                    result["evidence"].pop("runtime_contract")
                elif mutation == "typed_contract":
                    result["evidence"]["runtime_contract"]["version"] = True
                else:
                    result["evidence"]["runtime_contract"]["codex_version"] = "0.153.0"
                self.repin_report_result(helper, mid, admission, result, rows)
                report = fleet_herdr_report.build_report(helper.runs, changed, compiled, events)
                bad = next(r for r in report["runs"] if r["run_id"] == admission["run_id"])
                self.assertIsNone(bad["observed"])
                self.assertIn("runtime contract", bad["observation_reason"])
                self.assertEqual(report["provider_observation"]["observed_runs"], 4)

    def test_new_result_without_validated_backend_cannot_assert_observed_version(self):
        helper, mid, compiled, current = self.official_report_fixture()
        root = helper.runs / "missions" / mid
        (root / "herdr-backend.json").unlink()
        (root / "herdr-runtime-contract.json").unlink()
        report = fleet_herdr_report.build_report(helper.runs, current, compiled,
            state.read_events(state.ledger_path(helper.runs, mid)))
        self.assertEqual(report["provider_observation"]["observed_runs"], 0)
        self.assertTrue(all("runtime contract" in r["observation_reason"] for r in report["runs"]))

    def fixture(self, **kwargs):
        helper = test_fleet_herdr_archive.HerdrSnapshotTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        mid, roles, backend = helper.archive_fixture(**kwargs)
        return helper, mid, roles, backend

    def staged(self):
        helper, mid, roles, backend = self.fixture()
        proof = archive.create(helper.runs, mid, helper.repo, roles, backend)
        return helper, mid, proof

    def anchor(self, helper, mid, proof):
        state.append_event(helper.runs, mid, kind="archive_created", actor="CONTROL", idempotency_key="anchor",
            payload={"path": proof["path"], "sha256": proof["index_sha256"], "mode": "herdr"})

    def test_five_turns_four_sessions_and_unknown_metrics_are_read_only(self):
        helper, mid, proof = self.staged()
        self.anchor(helper, mid, proof)
        root = helper.runs / "missions" / mid
        before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        # Reading reports may verify Git trees offline; it must never call a runtime.
        with mock.patch("fleet_herdr.HerdrBackend", side_effect=AssertionError("live runtime")):
            report = fleet_report.build_report(helper.runs, mid)
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["outcomes"]["runs"], 5)
        self.assertEqual(report["agents"]["observed_sessions"], 4)
        self.assertEqual(report["delegation"], {"count": 4, "synthesis_turns": 1})
        self.assertEqual({r["model"]: r["runs"] for r in report["providers"]}, {"gpt-6-astra": 2, "gpt-5.6-sol": 3})
        self.assertEqual(report["archive"]["state"], "verified")
        self.assertEqual(report["archive"]["permissions"]["runs"], 5)
        for run in report["runs"]:
            for key in ("prompt_tokens", "completion_tokens", "cost_usd", "execution_seconds"):
                self.assertIsNone(run[key])
            self.assertTrue(run["usage_reason"])
            self.assertTrue(run["timing_reason"])
        self.assertIsNone(report["timing"]["controller_wait_seconds"])
        self.assertIsNone(report["timing"]["requested_pause_seconds"])
        self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_staged_absent_missing_and_corrupt_archives_are_distinct(self):
        helper, mid, roles, backend = self.fixture()
        report = fleet_report.build_report(helper.runs, mid)
        self.assertEqual(report["archive"]["state"], "absent")
        proof = archive.create(helper.runs, mid, helper.repo, roles, backend)
        self.assertEqual(fleet_report.build_report(helper.runs, mid)["archive"]["state"], "unverifiable")
        self.anchor(helper, mid, proof)
        item = Path(proof["path"]).parent / "writer/change.patch"
        raw = item.read_bytes()
        item.unlink()  # Only this test's disposable fixture.
        missing = fleet_report.build_report(helper.runs, mid)
        self.assertEqual(missing["archive"]["state"], "unverifiable")
        self.assertIsNone(missing["acceptance"])
        item.write_bytes(raw + b"forged")
        item.chmod(0o600)
        corrupt = fleet_report.build_report(helper.runs, mid)
        self.assertEqual(corrupt["archive"]["state"], "corrupt")
        self.assertEqual(corrupt["status_source"], "mission_ledger")
        self.assertIsNone(corrupt["acceptance"])
        Path(proof["path"]).write_bytes(b"[]\n")
        self.assertEqual(fleet_report.build_report(helper.runs, mid)["archive"]["state"], "corrupt")

    def test_missing_turn_evidence_does_not_infer_observed_models_from_roster(self):
        helper, mid, _, _ = self.fixture()
        with mock.patch.object(fleet_artifacts, "get_bytes", side_effect=FileNotFoundError("fixture missing")):
            report = fleet_report.build_report(helper.runs, mid)
        self.assertEqual(report["outcomes"]["runs"], 5)
        self.assertEqual(report["providers"], [])
        self.assertIsNone(report["agents"]["observed_sessions"])
        self.assertEqual(report["provider_observation"]["unobserved_runs"], 5)
        self.assertTrue(all(r["observed"] is None and r["requested"]["model"] for r in report["runs"]))

    def test_transport_settled_does_not_finalize_pending_admission(self):
        helper, mid, _, _ = self.fixture()
        import fleet_herdr_report
        compiled, current = fleet_mission.load_mission_compiled(helper.runs, mid, mode="read")
        current = copy.deepcopy(current)
        admission = next(iter(current["admissions"].values()))
        admission.update(phase="started", terminal=None, result=None, active=True)
        backend = {"schema_version": 2, "backend_version": "0.8.2",
                   "mission_id": mid, "compiled_digest": compiled["compiled_digest"],
                   "submissions": {admission["run_id"]: {"status": "settled"}}}
        path = helper.runs / "missions" / mid / "herdr-backend.json"
        path.write_bytes(state.canonical_bytes(backend) + b"\n")
        path.chmod(0o600)
        report = fleet_herdr_report.build_report(helper.runs, current, compiled,
            state.read_events(state.ledger_path(helper.runs, mid)))
        run = next(r for r in report["runs"] if r["run_id"] == admission["run_id"])
        self.assertEqual((run["transport_status"], run["status"]), ("settled", "pending"))
        self.assertIsNone(run["observed"])

    def test_herdr_report_ignores_foreign_mission_and_shared_feature_ledger(self):
        helper, mid, _, _ = self.fixture()
        compiled, current = fleet_mission.load_mission_compiled(helper.runs, mid, mode="read")
        before = fleet_report.build_report(helper.runs, mid)
        self.assertEqual(before["backend"], "herdr")
        foreign_mid, _ = fleet_mission.create_mission(helper.runs, compiled=compiled,
            feature=current["feature"], objective="Foreign Mission sharing the feature",
            target_repo=helper.repo, base_sha=current["base_sha"], idempotency_key="foreign-report")
        self.assertNotEqual(foreign_mid, mid)
        foreign_run = str(uuid.uuid4())
        legacy = helper.runs / f"fleet-{current['feature']}.ledger.jsonl"
        legacy.write_bytes(state.canonical_bytes({"mission_id": foreign_mid, "run_id": foreign_run,
            "feature": current["feature"], "instance": "worker", "provider": "openai",
            "model": "gpt-5.6-sol", "status": "succeeded", "prompt_tokens": 999999,
            "completion_tokens": 999999, "timestamp": "2026-09-12T00:00:00Z"}) + b"\n")
        root = helper.runs / "missions" / mid
        snapshot = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}

        after = fleet_report.build_report(helper.runs, mid)

        self.assertEqual(after, before)
        self.assertNotIn(foreign_run, {r["run_id"] for r in after["runs"]})
        self.assertEqual(snapshot, {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_new_archive_rejects_incompatible_context_in_each_of_five_stages(self):
        for stage in ("plan", "build", "review", "verify", "synthesis"):
            with self.subTest(stage=stage):
                def mutate(actual, context):
                    if actual == stage:
                        context.pop("sandbox_policy")
                helper, mid, roles, backend = self.fixture(mutate_context=mutate)
                with self.assertRaisesRegex(archive.HerdrArchiveError, f"herdr:{stage} permission"):
                    archive.create(helper.runs, mid, helper.repo, roles, backend)
                self.assertFalse((helper.runs / "missions" / mid / "herdr-archive").exists())

    def test_historical_archive_is_readable_and_reassessment_never_rewrites_it(self):
        helper, mid, proof = self.staged()
        path = Path(proof["path"])
        index = fleet_json.loads(path.read_bytes())
        index["schema_version"] = 2
        index.pop("permissions_policy_version")
        raw = state.canonical_bytes(index) + b"\n"
        path.write_bytes(raw)
        self.anchor(helper, mid, {"path": str(path), "index_sha256": state.artifact_id(raw)})
        self.assertEqual(archive.verify(helper.runs, mid)["permissions"]["status"], "not_attested")
        reassessed = archive.verify(helper.runs, mid, attest_permissions=True)
        self.assertEqual(reassessed["permissions"]["status"], "not_attested")
        self.assertEqual(reassessed["permissions_reassessment"]["runs"], 5)
        self.assertEqual(path.read_bytes(), raw)

    def test_historical_missing_permissions_remain_readable_but_cannot_be_reassessed(self):
        # Stage an intentionally historical fixture. No production verifier is bypassed.
        helper, mid, roles, backend = self.fixture(mutate_context=lambda stage, c: c.pop("approval_policy"))
        with mock.patch.object(archive, "attest_admissions", return_value={"policy_version": 1}):
            proof = archive.create(helper.runs, mid, helper.repo, roles, backend)
        path = Path(proof["path"])
        index = fleet_json.loads(path.read_bytes())
        index["schema_version"] = 2
        index.pop("permissions_policy_version")
        raw = state.canonical_bytes(index) + b"\n"
        path.write_bytes(raw)
        self.anchor(helper, mid, {"path": str(path), "index_sha256": state.artifact_id(raw)})
        self.assertTrue(archive.verify(helper.runs, mid)["valid"])
        with self.assertRaisesRegex(archive.HerdrArchiveError, "permission"):
            archive.verify(helper.runs, mid, attest_permissions=True)
        self.assertEqual(path.read_bytes(), raw)

    def test_offline_v3_rejects_resealed_evidence_with_invalid_plan_permissions(self):
        def mutate(stage, context):
            if stage == "plan":
                context["sandbox_policy"] = {"type": "danger-full-access"}
        helper, mid, roles, backend = self.fixture(mutate_context=mutate)
        # Create a hash-consistent negative fixture, then use the unpatched verifier.
        with mock.patch.object(archive, "attest_admissions", return_value={"policy_version": 1}):
            proof = archive.create(helper.runs, mid, helper.repo, roles, backend)
        self.anchor(helper, mid, proof)
        with self.assertRaisesRegex(archive.HerdrArchiveError, "herdr:plan permission"):
            archive.verify(helper.runs, mid)
