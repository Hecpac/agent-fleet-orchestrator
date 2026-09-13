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

    def test_absent_startup_intervals_remain_unknown_and_overlap_is_not_added(self):
        helper, mid, compiled, current = self.official_report_fixture()
        report=fleet_herdr_report.build_report(helper.runs,current,compiled,
            state.read_events(state.ledger_path(helper.runs,mid)))
        self.assertIsNone(report["timing"]["startup_observed_seconds"])
        self.assertEqual(report["timing"]["startup_reason"],"startup_intervals_not_observed")
        self.assertEqual(fleet_herdr_report._covered_seconds([]),(None,0))
        self.assertEqual(fleet_herdr_report._covered_seconds([(None,None)]),(None,0))
        self.assertEqual(fleet_herdr_report._covered_seconds([
            ("2026-09-06T00:00:00Z","2026-09-06T00:00:02Z"),
            ("2026-09-06T00:00:01Z","2026-09-06T00:00:03Z")]),(3,2))

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

    def test_rejected_completed_turn_usage_is_observed_but_not_admitted(self):
        helper, mid, compiled, current = self.official_report_fixture()
        current = copy.deepcopy(current)
        admission = next(iter(current["admissions"].values()))
        recorded = dict(admission["result"])
        result = fleet_json.loads(fleet_artifacts.get_bytes(
            helper.runs, mid, recorded["artifact_id"]))
        rows = fleet_json.load_jsonl(fleet_artifacts.get_bytes(
            helper.runs, mid, result["evidence"]["transcript_artifact_id"]))
        complete = next(index for index, row in enumerate(rows)
                        if row.get("payload", {}).get("type") == "task_complete")
        rows.insert(complete, {"type": "event_msg", "payload": {"type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 150,
                "output_tokens": 20, "cached_input_tokens": 30}}}})
        baseline = {"schema_version": 1, "kind": "herdr_usage_baseline",
            "mission_id": mid, "run_id": admission["run_id"],
            "prompt_sha256": admission["task_sha256"],
            "generation": "fixture-generation", "agent_session": result["evidence"]["agent_session"],
            "captured_at": "2026-09-06T00:00:00Z", "status": "known",
            "counts": {"input_tokens": 100, "output_tokens": 10,
                       "cached_input_tokens": 20},
            "source": "pre_dispatch_session_counter", "reason": None,
            "transcript_sha256": "a" * 64, "captured_rows": 5}
        baseline_id = fleet_artifacts.put_bytes(helper.runs, mid,
            fleet_json.canonical_bytes(baseline))["artifact_id"]
        result["evidence"].update(generation="fixture-generation",
                                  usage_baseline_artifact_id=baseline_id)
        self.repin_report_result(helper, mid, admission, result, rows)
        observed_id = admission["result"]["artifact_id"]
        proof = {"schema_version": 1, "kind": "herdr_role_protocol_rejection",
            "mission_id": mid, "run_id": admission["run_id"],
            "instance_id": admission["recipient_instance"],
            "prompt_sha256": admission["task_sha256"],
            "observed_result_artifact_id": observed_id, "reason": "fixture rejection"}
        proof_id = fleet_artifacts.put_bytes(helper.runs, mid,
            fleet_json.canonical_bytes(proof))["artifact_id"]
        root = helper.runs / "missions" / mid
        pointer = root / f"herdr-result-rejection-{admission['run_id']}.json"
        pointer.write_bytes(fleet_json.canonical_bytes({"artifact_id": proof_id}) + b"\n")
        pointer.chmod(0o600)
        backend_path = root / "herdr-backend.json"
        backend = fleet_json.loads(backend_path.read_bytes())
        backend["submissions"][admission["run_id"]] = {
            "status": "settled", "usage_baseline_artifact_id": baseline_id,
            "prepared_at": "2026-09-06T00:00:00Z",
            "submitted_at": "2026-09-06T00:00:01Z"}
        backend_path.write_bytes(fleet_json.canonical_bytes(backend) + b"\n")
        admission.update(phase="started", active=True, terminal=None, result=None)

        report = fleet_herdr_report.build_report(helper.runs, current, compiled,
            state.read_events(state.ledger_path(helper.runs, mid)))
        run = next(item for item in report["runs"] if item["run_id"] == admission["run_id"])
        self.assertEqual(run["result_disposition"], "rejected_role_protocol")
        self.assertEqual((run["prompt_tokens"], run["completion_tokens"],
                          run["cached_input_tokens"]), (50, 10, 10))
        self.assertEqual(run["status"], "pending")

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
