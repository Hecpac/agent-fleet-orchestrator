from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_herdr_archive as archive
import fleet_artifacts
import fleet_admission
import fleet_acceptance
import fleet_json
import fleet_mission
import fleet_mission_state as state
import workflow_config


class HerdrSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "result.txt").write_text("before\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture baseline")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args])

    def test_snapshot_captures_uncommitted_and_untracked_without_index_or_head_change(self):
        original_index = (self.repo / ".git" / "index").read_bytes()
        original_head = self.git("rev-parse", "HEAD")
        (self.repo / "result.txt").write_text("after\n")
        (self.repo / "new.json").write_text('{"value":42}\n')
        tree_sha, tree, patch = archive.snapshot(self.repo)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), original_index)
        self.assertEqual(self.git("rev-parse", "HEAD"), original_head)
        self.assertIn(b"+after", patch)
        with tarfile.open(fileobj=io.BytesIO(tree)) as tar:
            self.assertEqual(tar.extractfile("result.txt").read(), b"after\n")
            self.assertEqual(tar.extractfile("new.json").read(), b'{"value":42}\n')
        self.assertEqual(archive.snapshot(self.repo), (tree_sha, tree, patch))

    def test_snapshot_preserves_deleted_files_and_executable_modes(self):
        (self.repo / "result.txt").unlink()
        script = self.repo / "check.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        _, tree, patch = archive.snapshot(self.repo)
        with tarfile.open(fileobj=io.BytesIO(tree)) as tar:
            self.assertNotIn("result.txt", tar.getnames())
            self.assertEqual(tar.getmember("check.sh").mode & 0o111, 0o111)
        self.assertIn(b"deleted file", patch)

    def test_snapshot_rejects_subdirectory_as_repository(self):
        nested = self.repo / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(archive.HerdrArchiveError, "exact Git root"):
            archive.snapshot(nested)

    def archive_fixture(self, expected="after", mutate_result=None, mutate_context=None, runtime_options=None, contract_override=None):
        # A durable fixture drives the real ledger. Role admission behavior is
        # separately exercised by the Mission driver tests.
        self.runs = self.repo / "runs"
        (self.repo / ".gitignore").write_text("runs/\n")
        compiled = workflow_config.compile_path(Path(__file__).resolve().parents[1] / "workflows/herdr-implementation.yaml")
        contract = {"schema_version": 1, "requirements": [{"id": "output", "description": "Output matches", "checks": [{"kind": "text_contains", "path": "result.txt", "expected": expected}]}]}
        contract = contract_override or contract
        mission_id, _ = fleet_mission.create_mission(self.runs, compiled=compiled, feature="archive-test",
            objective="Create a result", target_repo=self.repo, base_sha=self.git("rev-parse", "HEAD").decode().strip(),
            idempotency_key=fleet_acceptance.bound_key("archive-test", contract),
            runtime_options={"acceptance_contract": contract, **(runtime_options or {})})
        for kind, payload in (("fleet_boot_started", {"feature": "archive-test"}), ("mission_running", {"manifest": "fixture"})):
            state.append_event(self.runs, mission_id, kind=kind, actor="CONTROL", idempotency_key=kind, payload=payload)
        (self.repo / "result.txt").write_text("after\n")
        frozen = archive.freeze(self.runs, mission_id, self.repo)
        roles = {}
        for stage, role in [("plan", "lead"), ("build", "worker"), ("review", "reviewer"), ("verify", "verifier"), ("synthesis", "lead")]:
            final = fleet_artifacts.put_bytes(self.runs, mission_id, stage.encode())["artifact_id"]
            request = {"request_key": "herdr:" + stage, "run_kind": "specialist", "recipient_instance": role,
                "capability": "synthesis" if role == "lead" else role, "effect_sha256": final,
                "task_sha256": final, "delegated_budget": 0, "writer": role == "worker"}
            admission = fleet_admission.reserve_many(self.runs, mission_id, requests=[request], idempotency_key=stage + ":reserve")["admissions"][0]
            binding = {k: admission[k] for k in ("admission_id", "request_digest", "effect_sha256", "recipient_instance", "writer", "run_id")}
            commit = fleet_admission.commit(self.runs, mission_id, **binding, idempotency_key=stage + ":commit")
            authorization = fleet_admission.authorize_launch(self.runs, mission_id, **binding,
                commit_event_sha256=commit["commit_event_sha256"], idempotency_key=stage + ":authorize")
            fleet_admission.mark_started(self.runs, mission_id, **binding,
                authorization_event_sha256=authorization["authorization_event_sha256"], idempotency_key=stage + ":start")
            evidence_id = fleet_artifacts.put_bytes(self.runs, mission_id, b"after\n")["artifact_id"]
            authored = {"schema_version": 1, "mission_id": mission_id, "instance_id": role,
                "run_id": admission["run_id"], "status": "PASS", "summary": "Fixture evidence",
                "candidate_tree_sha": frozen["tree_sha"],
                "artifacts": [{"path": "result.txt", "sha256": evidence_id}]}
            if mutate_result:
                mutate_result(role, authored)
            raw_id = fleet_artifacts.put_bytes(self.runs, mission_id, state.canonical_bytes(authored))["artifact_id"]
            session_id, turn_id = str(uuid.uuid5(uuid.UUID(mission_id), role)), str(uuid.uuid4())
            final_text = state.canonical_bytes(authored).decode()
            transcript = [
                {"type": "session_meta", "payload": {"id": session_id, "model_provider": "openai"}},
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": stage}]}},
                {"type": "turn_context", "payload": {"turn_id": turn_id, "model": "gpt-6-astra" if role == "lead" else "gpt-5.6-sol", "effort": "high", "cwd": str(self.repo), "approval_policy": "never",
                    "sandbox_policy": {"type": "workspace-write", "network_access": False,
                        "exclude_tmpdir_env_var": False, "exclude_slash_tmp": False} if role == "worker" else {"type": "read-only"}}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": final_text}]}},
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id, "last_agent_message": final_text}},
            ]
            if mutate_context:
                mutate_context(stage, transcript[3]["payload"])
            transcript_id = fleet_artifacts.put_bytes(self.runs, mission_id, b"".join(state.canonical_bytes(row) + b"\n" for row in transcript))["artifact_id"]
            backend_result = {**authored, "artifact_id": raw_id, "turn_id": turn_id, "evidence": {
                "herdr_session": "fixture", "agent_session": {"kind": "id", "value": session_id},
                "prompt_sha256": final, "transcript_sha256": transcript_id, "transcript_artifact_id": transcript_id}}
            backend_id = fleet_artifacts.put_bytes(self.runs, mission_id, state.canonical_bytes(backend_result))["artifact_id"]
            result = {**backend_result, "backend_result_artifact_id": backend_id, "evidence_artifact_ids": [evidence_id]}
            stored = fleet_artifacts.put_bytes(self.runs, mission_id, state.canonical_bytes(result))
            metadata = {"run_id": admission["run_id"], "artifact_id": stored["artifact_id"], "provider": "openai",
                        "model": "gpt-6-astra" if role == "lead" else "gpt-5.6-sol", "variant": None}
            if stage == "synthesis":
                kind = "synthesis_result_recorded"
                payload = {**metadata, "admission_id": admission["admission_id"], "result_file": stored["path"]}
            else:
                state.append_event(self.runs, mission_id, kind="delegation_registered", actor="CONTROL", idempotency_key=stage + ":register", payload={
                    "delegation_id": admission["delegation_id"], "mission_id": mission_id, "run_id": admission["run_id"],
                    "parent_run_id": None, "delegated_by": "CONTROL", "recipient_instance": role, "capability": role,
                    "objective_sha256": final, "input_artifact_ids": [], "expected_output_contract": {},
                    "deadline": fleet_mission.load_state(self.runs, mission_id)["admission_policy"]["deadline_at"],
                    "depth": 0, "token_id": None, "provider": metadata["provider"], "model": metadata["model"], "variant": None})
                kind = "result_recorded"
                payload = {**metadata, "delegation_id": admission["delegation_id"]}
            event, _ = state.append_event(self.runs, mission_id, kind=kind, actor="CONTROL", idempotency_key=stage + ":result", payload=payload)
            fleet_admission.finalize(self.runs, mission_id, admission_id=admission["admission_id"], recipient_instance=role,
                writer=role == "worker", reason="fixture result", idempotency_key=stage + ":final",
                terminal_evidence={"schema_version": 1, "source_event_sha256": event["event_sha256"], "run_id": admission["run_id"], "task_sha256": final, "status": "succeeded"})
            if stage != "plan":
                roles[role] = {**result, "result_artifact_id": stored["artifact_id"]}
        state.append_event(self.runs, mission_id, kind="mission_completing", actor="CONTROL", idempotency_key="completing", payload={"lead_artifact_id": roles["lead"]["artifact_id"]})
        return mission_id, roles, {"mission_id": mission_id, "compiled_digest": compiled["compiled_digest"], "session": "fixture"}

    def test_archive_roundtrip_and_offline_rejection(self):
        mission_id, roles, backend = self.archive_fixture()
        archive.freeze(self.runs, mission_id, self.repo)
        result = archive.create(self.runs, mission_id, self.repo, roles, backend)
        self.assertTrue(result["staged_valid"])
        self.assertFalse(result["valid"])
        self.assertEqual(result["acceptance"]["status"], "accepted")
        self.assertEqual(archive.create(self.runs, mission_id, self.repo, roles, backend), result)
        root = self.runs / "missions" / mission_id / "herdr-archive"
        (root / "results/worker.txt").write_bytes(b"forged")
        with self.assertRaisesRegex(archive.HerdrArchiveError, "archive content changed"):
            archive.verify(self.runs, mission_id)

    def test_archive_rejects_candidate_changed_after_freeze(self):
        mission_id, roles, backend = self.archive_fixture()
        archive.freeze(self.runs, mission_id, self.repo)
        (self.repo / "result.txt").write_text("changed after review\n")
        with self.assertRaisesRegex(archive.HerdrArchiveError, "immutable artifact differs"):
            archive.create(self.runs, mission_id, self.repo, roles, backend)

    def test_archive_replays_rejected_acceptance(self):
        mission_id, roles, backend = self.archive_fixture("required content absent")
        result = archive.create(self.runs, mission_id, self.repo, roles, backend)
        self.assertEqual(result["acceptance"]["status"], "rejected")

    def test_archive_recovery_rejects_changed_role_inputs(self):
        mission_id, roles, backend = self.archive_fixture()
        archive.create(self.runs, mission_id, self.repo, roles, backend)
        roles["worker"]["status"] = "FAIL"
        with self.assertRaisesRegex(archive.HerdrArchiveError, "recovery inputs differ"):
            archive.create(self.runs, mission_id, self.repo, roles, backend)

    def test_archive_rejects_role_borrowed_from_other_admission(self):
        mission_id, roles, backend = self.archive_fixture()
        roles["worker"]["run_id"] = roles["reviewer"]["run_id"]
        with self.assertRaisesRegex(archive.HerdrArchiveError, "exact admission"):
            archive.create(self.runs, mission_id, self.repo, roles, backend)

    def test_archive_verification_works_after_candidate_removal_from_view(self):
        mission_id, roles, backend = self.archive_fixture()
        result = archive.create(self.runs, mission_id, self.repo, roles, backend)
        self.anchor(mission_id, result)
        (self.repo / "result.txt").unlink()
        verified = archive.verify(self.runs, mission_id)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["index_sha256"], result["index_sha256"])
        self.assertEqual(archive.create(self.runs, mission_id, self.repo, roles, backend), verified)

    def anchor(self, mission_id, result):
        state.append_event(self.runs, mission_id, kind="archive_created", actor="CONTROL", idempotency_key="archive",
            payload={"path": result["path"], "sha256": result["index_sha256"], "mode": "herdr"})

    def test_public_verifier_requires_anchor(self):
        mission_id, roles, backend = self.archive_fixture()
        result = archive.create(self.runs, mission_id, self.repo, roles, backend)
        with self.assertRaisesRegex(archive.HerdrArchiveError, "ledger anchor"):
            archive.verify(self.runs, mission_id)
        self.anchor(mission_id, result)
        self.assertTrue(archive.verify(self.runs, mission_id)["anchored"])

    def test_archive_recovery_rejects_changed_backend(self):
        mission_id, roles, backend = self.archive_fixture()
        archive.create(self.runs, mission_id, self.repo, roles, backend)
        with self.assertRaisesRegex(archive.HerdrArchiveError, "recovery inputs differ"):
            archive.create(self.runs, mission_id, self.repo, roles, {**backend, "session": "other"})

    def test_archive_rejects_wrong_role_identity(self):
        mission_id, roles, backend = self.archive_fixture(mutate_result=lambda role, value: value.update(instance_id="wrong"))
        with self.assertRaisesRegex(archive.HerdrArchiveError, "result/admission mismatch"):
            archive.create(self.runs, mission_id, self.repo, roles, backend)

    def test_archive_rejects_wrong_mission_identity(self):
        mission_id, roles, backend = self.archive_fixture(mutate_result=lambda role, value: value.update(mission_id="wrong"))
        with self.assertRaisesRegex(archive.HerdrArchiveError, "result/admission mismatch"):
            archive.create(self.runs, mission_id, self.repo, roles, backend)

    def test_archive_rejects_empty_role_evidence(self):
        mission_id, roles, backend = self.archive_fixture(mutate_result=lambda role, value: value.update(artifacts=[]))
        with self.assertRaisesRegex(archive.HerdrArchiveError, "nonempty bound role evidence"):
            archive.create(self.runs, mission_id, self.repo, roles, backend)

    def test_archive_rechecks_unique_writer(self):
        mission_id, roles, backend = self.archive_fixture()
        result = archive.create(self.runs, mission_id, self.repo, roles, backend)
        self.anchor(mission_id, result)
        original = state.derive_state
        def wrong_writer(events):
            derived = original(events)
            for admission in derived["admissions"].values():
                admission["writer"] = admission["recipient_instance"] == "reviewer"
            return derived
        with mock.patch.object(state, "derive_state", side_effect=wrong_writer):
            with self.assertRaisesRegex(archive.HerdrArchiveError, "result/admission mismatch"):
                archive.verify(self.runs, mission_id)

    def test_archive_rejects_evidence_not_in_final_tree(self):
        def wrong_path(role, value):
            value["artifacts"][0]["path"] = "missing.txt"
        mission_id, roles, backend = self.archive_fixture(mutate_result=wrong_path)
        with self.assertRaisesRegex(archive.HerdrArchiveError, "missing from final tree"):
            archive.create(self.runs, mission_id, self.repo, roles, backend)

    def test_snapshot_rejects_concurrent_head_move(self):
        old = self.git("rev-parse", "HEAD").decode().strip()
        self.git("commit", "--allow-empty", "-qm", "second fixture")
        new = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-ref", "HEAD", old)
        original = archive._git
        moved = False
        def racing(repo, *args, **kwargs):
            nonlocal moved
            if args[0] == "read-tree" and not moved:
                moved = True
                self.git("update-ref", "HEAD", new)
            return original(repo, *args, **kwargs)
        with mock.patch.object(archive, "_git", side_effect=racing):
            with self.assertRaisesRegex(archive.HerdrArchiveError, "HEAD changed"):
                archive.snapshot(self.repo, expected_base=old)

    def test_archive_index_must_match_durable_ledger_anchor(self):
        mission_id, roles, backend = self.archive_fixture()
        result = archive.create(self.runs, mission_id, self.repo, roles, backend)
        state.append_event(self.runs, mission_id, kind="archive_created", actor="CONTROL", idempotency_key="archive",
            payload={"path": result["path"], "sha256": result["index_sha256"], "mode": "herdr"})
        path = Path(result["path"])
        value = fleet_json.loads(path.read_bytes())
        value["extra"] = "rehash attempt"
        path.write_bytes(state.canonical_bytes(value) + b"\n")
        with self.assertRaisesRegex(archive.HerdrArchiveError, "ledger anchor"):
            archive.verify(self.runs, mission_id)
