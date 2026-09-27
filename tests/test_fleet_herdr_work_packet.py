"""Unit B: provider-free work projection and exact owner interaction evidence."""
from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path
import subprocess
import sys
import uuid
from unittest import mock

from tests.test_fleet_herdr_orchestration_closure import MinimalFixture, MinimalBackend, git, ROOT
from tests.test_fleet_functional import synthetic_spec
from tests.test_mission_run import mission_run

import fleet_artifacts
import fleet_functional_runner as runner
import fleet_herdr
import fleet_herdr_evidence as evidence
import fleet_herdr_owner_protocol as owner
import fleet_herdr_scope as scope
import fleet_herdr_work_packet as work
import fleet_json
import fleet_mission
import fleet_mission_state as state
import workflow_config

REAL_BACKEND = fleet_herdr.HerdrBackend


class PacketFixture(MinimalFixture):
    def setUp(self):
        super().setUp()
        (self.target / "AGENTS.md").write_text("Preserve existing work.\n")
        (self.target / "src").mkdir()
        (self.target / "src/AGENTS.md").write_text("Nested instruction.\n")
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "fixture instructions")
        self.head = git(self.target, "rev-parse", "HEAD")
        self.scope = {"schema_version": 1, "editable_paths": ["answer.txt", "sample_stats.py"],
                      "temporary_directories": [".scratch"], "max_entries": 1000, "max_bytes": 1048576}
        self.brief = {"summary": "A small bounded correction", "entry_points": ["sample_stats.py"],
            "evidence": [{"name": "baseline failure", "content": "Empty input returned a value."}],
            "additional_requirements": ["Explain the behavior in the answer."],
            "procedure_preferences": ["Prefer focused tests."],
            "constraints": ["Use existing dependencies."],
            "decisions": [{"decision": "Preserve the public signature", "reason": "Existing callers depend on it."}]}

    def spec(self):
        spec = synthetic_spec()
        spec["runtime"]["controller_sha256"] = runner.controller_sha()
        spec["runtime"]["guest_sha256"] = state.artifact_id(runner.GUEST.read_bytes())
        return spec  # Runtime/image identity is synthetic; no Docker is used.

    def prepare(self, **changes):
        return work.prepare(**{"objective": "Correct the statistics and provide an answer.",
            "candidate_repo": self.target, "base_sha": self.head, "compiled": self.compiled,
            "acceptance_contract": self.contract, "scope_contract": self.scope,
            "functional_contract": self.spec(), "work_context": self.brief, **changes})


class WorkPacketTests(PacketFixture):
    def test_complete_functional_scope_context_and_limits_without_bookkeeping(self):
        with mock.patch.object(runner, "Docker", side_effect=AssertionError("runtime called")):
            prepared = self.prepare()
        packet = prepared["work_packet"]
        self.assertEqual(packet["objective"], "Correct the statistics and provide an answer.")
        self.assertEqual(packet["context"]["evidence"], self.brief["evidence"])
        for field in ("decisions", "procedure_preferences", "constraints"):
            self.assertEqual(packet[field], self.brief[field])
        self.assertEqual(packet["requirements"]["additional"], self.brief["additional_requirements"])
        self.assertEqual(packet["requirements"]["artifact"], self.contract["requirements"])
        self.assertEqual(packet["scope"]["editable_paths"], self.scope["editable_paths"])
        self.assertEqual(packet["scope"]["temporary_directories"], [".scratch"])
        functional = packet["requirements"]["functional"]
        self.assertEqual(functional["oracle"]["content"], Path(self.spec()["tests"]["path"]).read_text())
        names = {name for row in functional["requirements"] for name in row["tests"]}
        self.assertEqual(names, {"StatsTests.test_regular", "StatsTests.test_negative_and_fractional",
                                "StatsTests.test_empty", "StatsTests.test_invalid", "StatsTests.test_input_not_mutated"})
        self.assertEqual(functional["source_policy"]["validator_python"], inspect.getsource(runner.check_bridge_source))
        self.assertEqual(functional["execution"]["limits"], self.spec()["limits"])
        self.assertIn("not documentation", functional["oracle"]["coverage"])
        self.assertEqual(packet["capabilities"]["native_tools"]["availability"], "not_observed")
        self.assertEqual(functional["execution"]["availability"], "not_observed")
        self.assertFalse(packet["limits"]["token_budget_enforced"])
        self.assertIsNone(packet["limits"]["tokens_available"])
        self.assertFalse(packet["limits"]["owner_cycle_enabled"])
        self.assertEqual(packet["capabilities"]["permissions"]["effect_mediation"], "not_provided")
        def keys(value):
            if isinstance(value, dict):
                return set(value).union(*(keys(v) for v in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(v) for v in value))
            return set()
        reserved = {"mission_id", "run_id", "admission_id", "instance_id", "turn_id", "sha256",
                    "artifact_id", "compiled_digest", "candidate_tree_sha", "base_sha", "task_sha256"}
        self.assertFalse(keys(packet) & reserved)
        self.assertEqual(prepared["execution_envelope"]["base_sha"], self.head)
        self.assertEqual(prepared["execution_envelope"]["work_packet_sha256"], work.digest(packet))
        self.assertEqual(work.verify(prepared), prepared)
        self.assertFalse(MinimalBackend.calls)

    def test_instructions_and_selected_skills_transfer_content_not_only_references(self):
        prepared = self.prepare()
        projected = prepared["work_packet"]["instructions"]
        self.assertEqual(projected["project"]["entries"], [
            {"path": "AGENTS.md", "scope": ".", "content": "Preserve existing work.\n"},
            {"path": "src/AGENTS.md", "scope": "src", "content": "Nested instruction.\n"}])
        self.assertEqual({s["name"] for s in projected["skills"]}, {"fase-0-recon", "smoke-verify", "impl-notes"})
        self.assertTrue(all(s["content"] and s["when"] for s in projected["skills"]))
        (self.target / "AGENTS.md").write_text("Edited after preparation")
        self.assertEqual(work.verify(prepared), prepared)
        self.assertEqual(self.prepare()["work_packet"]["instructions"], projected)

    def test_input_mutation_cannot_rewrite_the_retained_packet(self):
        prepared = self.prepare()
        saved = copy.deepcopy(prepared)
        self.scope["editable_paths"].append("README.md")
        self.contract["requirements"][0]["description"] = "Changed requirement"
        self.brief["decisions"].clear()
        self.assertEqual(prepared, saved)
        work.verify(prepared)

    def test_absent_functional_contract_does_not_imply_verification(self):
        packet = self.prepare(functional_contract=None)["work_packet"]
        self.assertFalse(packet["requirements"]["functional"]["configured"])
        self.assertFalse(packet["capabilities"]["controller_functional_check"])
        self.assertIn("no functional or semantic coverage is inferred", packet["requirements"]["coverage"])

    def test_content_digest_predicate_is_a_requirement_not_response_bookkeeping(self):
        contract = copy.deepcopy(self.contract)
        contract["requirements"][0]["checks"] = [{"kind": "sha256", "path": "answer.txt", "expected": "a" * 64}]
        packet = self.prepare(acceptance_contract=contract)["work_packet"]
        self.assertEqual(packet["requirements"]["artifact"], contract["requirements"])

    def test_incomplete_preparation_and_oracle_inside_candidate_are_rejected(self):
        prepared = self.prepare()
        for field in ("instructions", "functional_tests", "scope", "permissions"):
            damaged = copy.deepcopy(prepared)
            damaged["sources"].pop(field)
            with self.subTest(field=field), self.assertRaises(work.WorkPacketError):
                work.verify(damaged)
        spec = self.spec()
        local = self.target / "tests.py"
        local.write_bytes(Path(spec["tests"]["path"]).read_bytes())
        spec["tests"]["path"] = str(local)
        with self.assertRaisesRegex(work.WorkPacketError, "external to candidate"):
            self.prepare(functional_contract=spec)

    def test_deadline_is_capped_without_promising_turn_or_token_budgets(self):
        short = self.prepare(timeout_seconds=60)["work_packet"]["limits"]
        capped = self.prepare(timeout_seconds=7200)["work_packet"]["limits"]
        self.assertEqual(short["deadline_seconds"], 60)
        self.assertEqual(capped["deadline_seconds"], 3600)
        for value in (False, 0, 59):
            with self.assertRaises(work.WorkPacketError):
                self.prepare(timeout_seconds=value)
        self.assertFalse(capped["repair_enabled"])
        self.assertFalse(capped["amendments_enabled"])
        self.assertNotIn("max_owner_turns", capped)

    def test_projection_rejects_changed_oracle_runtime_and_unsupported_check(self):
        for change in ("tests", "controller", "guest", "check"):
            spec = self.spec()
            if change == "tests":
                bad = self.root / "changed-tests.py"
                bad.write_text("different oracle")
                spec["tests"]["path"] = str(bad)
            elif change == "check":
                spec["check_id"] = "arbitrary-shell"
            else:
                spec["runtime"][change + "_sha256"] = "0" * 64
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.prepare(functional_contract=spec)

    def test_unknown_profile_missing_scope_or_oversized_context_never_emit_partial_packet(self):
        with self.assertRaises(work.WorkPacketError):
            self.prepare(compiled=workflow_config.compile_path(ROOT / "workflows/herdr-implementation.yaml"))
        with self.assertRaises(scope.ScopeError):
            self.prepare(scope_contract=None)
        for context in ({"summary": "x" * (work.MAX_CONTEXT_BYTES + 1)}, {"permissions": {"network": True}},
                        {"entry_points": ["../outside"]}, {"decisions": [{"decision": "yes"}]}):
            with self.subTest(context=list(context)), self.assertRaises((ValueError, state.MissionStateError)):
                self.prepare(work_context=context)
        with self.assertRaises(work.WorkPacketError):
            self.prepare(objective="x" * (64 * 1024 + 1))

    def test_tampering_with_packet_sources_and_envelope_is_detected(self):
        prepared = self.prepare()
        mutations = [lambda p: p["work_packet"]["scope"]["editable_paths"].append("README.md"),
            lambda p: p["execution_envelope"].update(dispatch_enabled=True),
            lambda p: p["sources"]["instructions"]["entries"][0].update(content="replaced"),
            lambda p: p["sources"]["functional"].update(check_id="other"),
            lambda p: p["execution_envelope"].update(base_sha="a" * 40)]
        for mutate in mutations:
            changed = copy.deepcopy(prepared)
            mutate(changed)
            with self.assertRaises(ValueError):
                work.verify(changed)


class OwnerProtocolTests(PacketFixture):
    def setUp(self):
        super().setUp()
        self.prepared = self.prepare()
        self.execution = {"mission_id": str(uuid.uuid4()), "run_id": str(uuid.uuid4()),
            "admission_id": str(uuid.uuid4()), "instance_id": "worker", "herdr_session": "fixture-herdr",
            "agent_session": "fixture-codex", "turn_id": "fixture-turn",
            "task_sha256": self.prepared["execution_envelope"]["work_packet_sha256"]}
        self.pin = work.digest(self.prepared["execution_envelope"])
        self.candidate = {"type": "submit_candidate", "summary": "Corrected behavior; controller checks are pending.",
            "paths": ["answer.txt", "sample_stats.py"],
            "checks": [{"name": "local unit check", "outcome": "passed", "detail": "A claim to be retained, not promoted to controller evidence."}]}
        self.decision = {"type": "request_decision", "question": "Should the public output change?",
            "why_needed": "The compatibility requirement is ambiguous.", "options": [],
            "recommendation": None, "work_completed": "Baseline investigated."}

    def transcript(self, response):
        final = json.dumps(response, ensure_ascii=False, indent=2).encode()
        policy = self.prepared["sources"]["permissions"]
        rows = [
            {"type": "session_meta", "payload": {"id": self.execution["agent_session"], "model_provider": "openai", "cli_version": "0.154.0"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": self.execution["turn_id"]}},
            {"type": "turn_context", "payload": {"turn_id": self.execution["turn_id"],
                **{k: policy[k] for k in ("model", "effort", "cwd", "sandbox_policy", "approval_policy")}}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": fleet_json.canonical_bytes(self.prepared["work_packet"]).decode()}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": final.decode()}]}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": self.execution["turn_id"], "last_agent_message": final.decode()}},
        ]
        return final, rows

    def raw(self, rows):
        return b"".join(fleet_json.canonical_bytes(row) + b"\n" for row in rows)

    def bind(self, response, **overrides):
        final, rows = self.transcript(response)
        return owner.bind_response(self.prepared, self.execution,
            **{"expected_envelope_sha256": self.pin, "transcript": self.raw(rows), "final_bytes": final, **overrides})

    def verify(self, bound, **changes):
        return owner.verify_receipt(bound["receipt"], **{"read_artifact": bound["artifacts"].__getitem__,
            "expected_execution": self.execution, "expected_envelope_sha256": self.pin, **changes})

    def test_candidate_receipt_preserves_raw_bytes_and_binds_internally_without_accepting(self):
        final, rows = self.transcript(self.candidate)
        backend = object.__new__(REAL_BACKEND)
        row_bytes = [fleet_json.canonical_bytes(row) + b"\n" for row in rows]
        with mock.patch.object(backend, "_transcript_rows", return_value=(rows, row_bytes, "/fixture/transcript", None)):
            observed = backend._transcript_result({"prompt_sha256": self.execution["task_sha256"]},
                                                 {"agent_session": {"value": "fixture-codex"}})
        self.assertEqual(observed[0], self.candidate)
        bound = self.bind(self.candidate, final_bytes=observed[1], transcript=observed[4])
        receipt = self.verify(bound)
        self.assertEqual(receipt["execution"], self.execution)
        self.assertEqual(receipt["message"], self.candidate)
        self.assertEqual(bound["artifacts"][receipt["authored_response_sha256"]], final)
        self.assertNotEqual(final, fleet_json.canonical_bytes(self.candidate))
        self.assertEqual(receipt["disposition"], "candidate_proposed")
        self.assertEqual(receipt["authority"], "none")
        self.assertFalse(receipt["candidate_accepted"])
        self.assertFalse(receipt["owner_cycle_enabled"])
        self.assertNotIn("artifacts", receipt["message"])
        self.assertFalse((self.target / "answer.txt").exists())  # Paths do not prove content.

    def test_decision_without_artifact_or_invented_choices_uses_existing_cas_without_ledger_transition(self):
        mid = self.create(key="owner-cas-fixture")
        self.execution["mission_id"] = mid
        before = fleet_mission.load_state(self.runs, mid)
        with mock.patch.object(scope, "capture", side_effect=AssertionError("decision inventoried candidate")):
            bound = self.bind(self.decision)
        for pin, raw in bound["artifacts"].items():
            self.assertEqual(fleet_artifacts.put_bytes(self.runs, mid, raw)["artifact_id"], pin)
        receipt = self.verify(bound, read_artifact=lambda pin: fleet_artifacts.get_bytes(self.runs, mid, pin))
        self.assertEqual(receipt["disposition"], "decision_requested")
        self.assertEqual(receipt["message"], self.decision)
        self.assertFalse(receipt["decision_applied"])
        self.assertEqual(fleet_mission.load_state(self.runs, mid), before)
        self.assertFalse(MinimalBackend.calls)

    def test_candidate_can_report_deletion_or_no_paths_without_fake_artifact(self):
        for paths in ([], ["answer.txt"]):
            self.verify(self.bind({**self.candidate, "paths": paths, "checks": []}))

    def test_material_options_and_recommendation_remain_content_not_permission(self):
        decision = {**self.decision, "options": [
            {"label": "Preserve", "consequence": "Keep compatibility."},
            {"label": "Change", "consequence": "Require caller migration."}], "recommendation": "Preserve"}
        receipt = self.verify(self.bind(decision))
        self.assertEqual(receipt["message"]["recommendation"], "Preserve")
        self.assertFalse(receipt["decision_applied"])

    def test_ids_hashes_terminal_status_and_unknown_fields_are_not_model_output(self):
        for field, value in (("mission_id", self.execution["mission_id"]), ("sha256", "a" * 64),
                             ("candidate_tree_sha", self.head), ("status", "PASS"), ("artifact_id", "b" * 64)):
            for message in (self.candidate, self.decision):
                with self.subTest(field=field, type=message["type"]), self.assertRaises(owner.OwnerProtocolError):
                    self.bind({**message, field: value})

    def test_invalid_paths_choices_and_unbounded_or_ambiguous_json_are_rejected(self):
        for paths in (["README.md"], [".scratch/output"], ["../outside"], ["answer.txt", "answer.txt"]):
            with self.subTest(paths=paths), self.assertRaises(owner.OwnerProtocolError):
                self.bind({**self.candidate, "paths": paths})
        for change in ({"recommendation": "not an option"}, {"options": [{"label": "A"}]},
                       {"question": ""}, {"options": [1, 2, 3, 4]}):
            with self.assertRaises(owner.OwnerProtocolError):
                self.bind({**self.decision, **change})
        for raw in (b"{}", b"[]", b'{"type":"submit_candidate","type":"request_decision"}',
                    b"```json\n{}\n```", b"x" * (work.MAX_RESPONSE_BYTES + 1)):
            with self.assertRaises(owner.OwnerProtocolError):
                owner.parse_response(raw, self.prepared["work_packet"])

    def test_wrong_prompt_session_turn_model_permissions_and_completion_cannot_bind(self):
        final, rows = self.transcript(self.candidate)
        variants = []
        for index, field, value in ((0, "id", "foreign"), (2, "turn_id", "foreign"), (2, "model", "other"),
                                   (2, "approval_policy", "on-request"), (5, "last_agent_message", "different")):
            changed = copy.deepcopy(rows)
            changed[index]["payload"][field] = value
            variants.append(changed)
        changed = copy.deepcopy(rows)
        changed[3]["payload"]["content"][0]["text"] = "different objective"
        variants += [changed, rows[:-1], rows[:4] + [rows[3]] + rows[4:]]
        extra = copy.deepcopy(rows[3]); extra["payload"]["content"][0]["text"] = "new user amendment"
        variants.append(rows[:4] + [extra] + rows[4:])
        for changed in variants:
            with self.assertRaises(evidence.EvidenceError):
                self.bind(self.candidate, final_bytes=final, transcript=self.raw(changed))

    def test_changed_pin_or_controller_identity_cannot_relabel_existing_receipt(self):
        bound = self.bind(self.candidate)
        with self.assertRaises(owner.OwnerProtocolError):
            self.verify(bound, expected_envelope_sha256="0" * 64)
        for field in ("mission_id", "run_id", "admission_id", "agent_session", "turn_id", "task_sha256"):
            with self.subTest(field=field), self.assertRaises((owner.OwnerProtocolError, evidence.EvidenceError)):
                self.verify(bound, expected_execution={**self.execution, field: str(uuid.uuid4())})
        changed = copy.deepcopy(bound)
        changed["receipt"]["candidate_accepted"] = True
        with self.assertRaises(owner.OwnerProtocolError):
            self.verify(changed)

    def test_cas_bytes_and_resealed_public_projection_cannot_override_controller_pin(self):
        bound = self.bind(self.candidate)
        bound["artifacts"][bound["receipt"]["authored_response_sha256"]] = b"{}"
        with self.assertRaisesRegex(owner.OwnerProtocolError, "CAS mismatch"):
            self.verify(bound)
        original = self.prepared
        self.prepared = self.prepare(objective="A different authorized task")
        self.execution["task_sha256"] = self.prepared["execution_envelope"]["work_packet_sha256"]
        with self.assertRaisesRegex(owner.OwnerProtocolError, "controller pin"):
            self.bind(self.candidate)
        self.prepared = original

    def test_live_backend_rejects_new_protocol_before_any_runtime_effect(self):
        backend = object.__new__(REAL_BACKEND)
        with mock.patch.object(backend, "_require_execution_mediation", side_effect=AssertionError("effect gate reached")):
            with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "owner-work-v1 dispatch is disabled"):
                backend.submit(self.execution["run_id"], fleet_json.canonical_bytes(self.prepared["work_packet"]).decode(), instance_id="worker")


class WorkPacketCliTests(PacketFixture):
    def arguments(self):
        files = {}
        for name, value in (("acceptance", self.contract), ("scope", self.scope), ("functional", self.spec()), ("context", self.brief)):
            files[name] = self.root / (name + ".json")
            files[name].write_bytes(fleet_json.canonical_bytes(value))
        return ["--runs-dir", str(self.runs), "dry", "owner-preview", "Correct statistics locally",
            "--workflow", "herdr-minimal-implementation", "--target-repo", str(self.target),
            "--acceptance-contract", str(files["acceptance"]), "--scope-contract", str(files["scope"]),
            "--functional-contract", str(files["functional"]), "--work-context", str(files["context"]),
            "--work-packet", "--json"]

    def test_real_cli_preview_is_read_only_and_contains_the_full_packet(self):
        before = git(self.target, "status", "--porcelain"), git(self.target, "rev-parse", "HEAD")
        completed = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/mission-run.py"), *self.arguments()],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["effects"], [])
        self.assertFalse(result["owner_protocol"]["dispatch_enabled"])
        self.assertEqual(result["owner_work_packet"]["decisions"], self.brief["decisions"])
        self.assertTrue(result["owner_work_packet"]["requirements"]["functional"]["configured"])
        self.assertFalse(self.runs.exists())
        self.assertEqual((git(self.target, "status", "--porcelain"), git(self.target, "rev-parse", "HEAD")), before)
        self.assertFalse(MinimalBackend.calls)

    def test_flag_cannot_launch_a_mission_and_context_cannot_be_silently_ignored(self):
        args = self.arguments()
        args[2] = "run"
        completed = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/mission-run.py"), *args],
                                   capture_output=True, text=True, timeout=30)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unrecognized arguments", completed.stderr)
        self.assertFalse(self.runs.exists())
        with self.assertRaisesRegex(mission_run.MissionRunError, "requires --work-packet"):
            mission_run.dry_run(feature="preview", objective="Local change", workflow_name="herdr-minimal-implementation",
                target_repo=self.target, risk_override="auto", timeout_seconds=None,
                acceptance_contract=self.contract, work_context=self.brief)

    def test_legacy_minimal_prompt_and_archive_are_unchanged(self):
        mid = self.create(key="old-protocol")
        result = mission_run.drive_mission(self.runs, mid)
        self.assertEqual(result["status"], "succeeded")
        tasks = list(MinimalBackend.tasks.values())
        self.assertEqual(len(tasks), 1)
        self.assertIn("result_contract", tasks[0])
        self.assertNotIn("contract_version", tasks[0])
        self.assertEqual(tasks[0]["result_contract"]["mission_id"], mid)
