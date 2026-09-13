from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
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

    def archive_fixture(self, expected="after", mutate_result=None, mutate_context=None, runtime_options=None, contract_override=None, sdd_plan_path=None):
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
            runtime_options={"acceptance_contract": contract, **(runtime_options or {})},
            sdd_plan_path=sdd_plan_path)
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

    def test_archive_snapshot_read_race_uses_one_history(self):
        import fleet_herdr_control as control
        mid, roles, backend = self.archive_fixture()
        control.enable(self.runs, mid)
        original = archive.freeze

        def interleave(*args, **kwargs):
            frozen = original(*args, **kwargs)
            control.request(self.runs, mid, action="resume", reason="snapshot interleave",
                            idempotency_key="snapshot-interleave")
            return frozen

        try:
            with mock.patch.object(archive, "freeze", side_effect=interleave):
                result = archive.create(self.runs, mid, self.repo, roles, backend)
        except archive.HerdrArchiveError as exc:
            self.fail(f"snapshot state and ledger must share one history: {exc}")
        self.assertTrue(result["staged_valid"])
        root = self.runs / "missions" / mid / "herdr-archive"
        index = fleet_json.loads((root / "archive-index.json").read_bytes())
        events = fleet_json.load_jsonl((root / "ledger.jsonl").read_bytes())
        self.assertEqual(state.derive_state(events)["head_sha256"], index["ledger_head"])
        self.assertEqual(state.read_events(state.ledger_path(self.runs, mid))[:len(events)], events)

    def test_archive_pending_rename_reuses_selected_bytes(self):
        import fleet_herdr_control as control
        import fleet_safe_paths
        mid, roles, backend = self.archive_fixture()
        control.enable(self.runs, mid)
        with tempfile.TemporaryDirectory() as temporary:
            child_input = Path(temporary) / "input.json"
            child_input.write_bytes(state.canonical_bytes({"runs": str(self.runs), "mid": mid,
                "candidate": str(self.repo), "roles": roles, "backend": backend}))
            code = """
import json, os, sys
from pathlib import Path
from unittest import mock
import fleet_herdr_archive as archive
inputs = json.loads(Path(sys.argv[1]).read_text())
original = archive._write
def interrupt(store, relative, content):
    if 'herdr-archive' in relative.parts and relative.name == 'ledger.jsonl':
        os.environ['FLEET_TEST_SAFE_PATH_CRASH_AT'] = 'after_atomic_pending_fsync'
    return original(store, relative, content)
with mock.patch.object(archive, '_write', side_effect=interrupt):
    archive.create(Path(inputs['runs']), inputs['mid'], Path(inputs['candidate']),
                   inputs['roles'], inputs['backend'])
"""
            child = subprocess.run([sys.executable, "-B", "-c", code, str(child_input)],
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "scripts"),
                     "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True)
        self.assertEqual(child.returncode, -9, child.stderr)
        root = self.runs / "missions" / mid / "herdr-archive"
        pending = list(root.glob(fleet_safe_paths._atomic_pending_prefix("ledger.jsonl") + "*.tmp"))
        self.assertEqual(len(pending), 1)
        selected_ledger = pending[0].read_bytes()
        self.assertFalse((root / "ledger.jsonl").exists())
        control.request(self.runs, mid, action="resume", reason="pending rename recovery",
                        idempotency_key="pending-recovery")
        try:
            result = archive.create(self.runs, mid, self.repo, roles, backend)
        except archive.HerdrArchiveError as exc:
            self.fail(f"pending publication must reuse its selected bytes: {exc}")
        self.assertTrue(result["staged_valid"])
        self.assertEqual((root / "ledger.jsonl").read_bytes(), selected_ledger)

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

    def test_archive_publication_failure_matrix(self):
        import fleet_herdr_control as control
        mid, roles, backend = self.archive_fixture()
        control.enable(self.runs, mid)
        original_write, original_put = archive._write, fleet_artifacts.put_bytes
        original_append, original_verify = state.MissionTransaction.append_event, archive.verify
        names = []

        def discover(store, relative, content):
            if "herdr-archive" in relative.parts:
                self.assertIn("herdr_archive_selection", fleet_mission.load_state(store.root, mid))
                names.append("/".join(relative.parts[3:]))
            original_write(store, relative, content)

        with tempfile.TemporaryDirectory() as temporary:
            runs = Path(temporary) / "runs"
            shutil.copytree(self.runs, runs)
            with mock.patch.object(archive, "_write", side_effect=discover):
                archive.create(runs, mid, self.repo, roles, backend)
        boundaries = ["before_capture", "during_input_cas", "before_selection", "after_selection",
                      "before_first_file", "before_index", "before_verify", "after_verify"]
        boundaries += ["after_file:" + name for name in names]
        for growth in (False, True):
            for boundary in boundaries:
                with self.subTest(boundary=boundary, live_growth=growth), tempfile.TemporaryDirectory() as temporary:
                    runs = Path(temporary) / "runs"
                    shutil.copytree(self.runs, runs)
                    root = runs / "missions" / mid / "herdr-archive"

                    def fail():
                        raise OSError("injected archive interruption")

                    def put(store, mission, content):
                        result = original_put(store, mission, content)
                        if boundary == "during_input_cas" and content == state.ledger_path(runs, mid).read_bytes():
                            fail()
                        return result

                    def append(transaction, **kwargs):
                        if kwargs["kind"] == "herdr_archive_selected" and boundary == "before_selection":
                            fail()
                        result = original_append(transaction, **kwargs)
                        if kwargs["kind"] == "herdr_archive_selected" and boundary == "after_selection":
                            fail()
                        return result

                    def write(store, relative, content):
                        name = "/".join(relative.parts[3:]) if "herdr-archive" in relative.parts else None
                        if (boundary == "before_first_file" and name == names[0]
                                or boundary == "before_index" and name == "archive-index.json"):
                            fail()
                        original_write(store, relative, content)
                        if name is not None and boundary == "after_file:" + name:
                            fail()

                    def verify(*args, **kwargs):
                        if boundary == "before_verify":
                            fail()
                        result = original_verify(*args, **kwargs)
                        if boundary == "after_verify":
                            fail()
                        return result

                    original_freeze = archive.freeze
                    def freeze(*args, **kwargs):
                        if boundary == "before_capture":
                            fail()
                        return original_freeze(*args, **kwargs)

                    with mock.patch.object(archive, "freeze", side_effect=freeze), \
                            mock.patch.object(archive, "_write", side_effect=write), \
                            mock.patch.object(fleet_artifacts, "put_bytes", side_effect=put), \
                            mock.patch.object(state.MissionTransaction, "append_event", new=append), \
                            mock.patch.object(archive, "verify", side_effect=verify):
                        with self.assertRaisesRegex(OSError, "injected archive"):
                            archive.create(runs, mid, self.repo, roles, backend)
                    selected = fleet_mission.load_state(runs, mid).get("herdr_archive_selection")
                    visible = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                    if growth:
                        control.request(runs, mid, action="resume", reason="publication retry",
                                        idempotency_key="publication-retry")
                    result = archive.create(runs, mid, self.repo, roles, {**backend, "updated_at": "later observation"})
                    self.assertTrue(result["staged_valid"])
                    after = fleet_mission.load_state(runs, mid)["herdr_archive_selection"]
                    if selected:
                        self.assertEqual(after, selected)
                        self.assertEqual(result["index_sha256"], selected["index_artifact_id"])
                    for relative, raw in visible.items():
                        self.assertEqual((root / relative).read_bytes(), raw)
                    index = fleet_json.loads((root / "archive-index.json").read_bytes())
                    events = fleet_json.load_jsonl((root / "ledger.jsonl").read_bytes())
                    self.assertEqual(state.derive_state(events)["head_sha256"], index["ledger_head"])
                    if growth and selected:
                        self.assertNotIn("publication-retry", [e["idempotency_key"] for e in events])
                    history = state.read_events(state.ledger_path(runs, mid))
                    self.assertEqual(history[:len(events)], events)
                    self.assertEqual(sum(e["kind"] == "herdr_archive_selected" for e in history), 1)
                    self.assertEqual(archive.recover(runs, mid), result)

    def test_archive_historical_complete_and_unselected_partial(self):
        import fleet_herdr_control as control
        mid, roles, backend = self.archive_fixture()
        control.enable(self.runs, mid)
        compiled, current = fleet_mission.load_mission_compiled(self.runs, mid, mode="effect")
        frozen = archive.freeze(self.runs, mid, self.repo)
        # Reproduce the pre-selection writer: existing format, no new event.
        contents, index = archive._capture_contents(self.runs, mid, roles, backend,
            compiled, current, frozen, state.ledger_path(self.runs, mid).read_bytes())
        for complete in (False, True):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as temporary:
                runs = Path(temporary) / "runs"
                shutil.copytree(self.runs, runs)
                if complete:
                    archive._publish_selected(runs, mid, archive._bytes(index), contents)
                else:
                    with archive.fleet_safe_paths.RootedFS(runs) as store:
                        archive._write(store, Path("missions") / mid / "herdr-archive/ledger.jsonl", contents["ledger.jsonl"])
                control.request(runs, mid, action="resume", reason="historical recovery",
                                idempotency_key="historical-retry")
                before = {p.relative_to(runs): p.read_bytes() for p in runs.rglob("*") if p.is_file()}
                if complete:
                    self.assertTrue(archive.create(runs, mid, self.repo, roles, backend)["staged_valid"])
                else:
                    with self.assertRaisesRegex(archive.HerdrArchiveError, "legacy partial.*pending"):
                        archive.create(runs, mid, self.repo, roles, backend)
                self.assertEqual(before, {p.relative_to(runs): p.read_bytes() for p in runs.rglob("*") if p.is_file()})
                self.assertNotIn("herdr_archive_selection", fleet_mission.load_state(runs, mid))

    def test_selected_archive_rejects_missing_cas_and_foreign_runtime_binding(self):
        mid, roles, backend = self.archive_fixture()
        archive.create(self.runs, mid, self.repo, roles, backend)
        for key in ("mission_id", "compiled_digest", "session", "generation"):
            with self.subTest(key=key), self.assertRaisesRegex(archive.HerdrArchiveError, "inputs differ"):
                archive.create(self.runs, mid, self.repo, roles, {**backend, key: "foreign"})
        selected = fleet_mission.load_state(self.runs, mid)["herdr_archive_selection"]
        (self.runs / "missions" / mid / "artifacts" / selected["index_artifact_id"]).unlink()
        with self.assertRaises(fleet_artifacts.ArtifactError):
            archive.recover(self.runs, mid)

    def test_selected_archive_does_not_overwrite_conflicting_partial(self):
        mid, roles, backend = self.archive_fixture()
        original = archive._write
        def interrupt(store, relative, content):
            original(store, relative, content)
            if "herdr-archive" in relative.parts and relative.name == "ledger.jsonl":
                raise OSError("partial archive interruption")
        with mock.patch.object(archive, "_write", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "partial archive"):
                archive.create(self.runs, mid, self.repo, roles, backend)
        ledger = self.runs / "missions" / mid / "herdr-archive/ledger.jsonl"
        ledger.write_bytes(b"conflicting fixture bytes\n")
        with self.assertRaisesRegex(archive.HerdrArchiveError, "immutable artifact differs"):
            archive.recover(self.runs, mid)
        self.assertEqual(ledger.read_bytes(), b"conflicting fixture bytes\n")
        self.assertFalse((ledger.parent / "archive-index.json").exists())

    def test_archive_cas_selection_and_ledger_process_crashes(self):
        import fleet_herdr_control as control
        mid, roles, backend = self.archive_fixture()
        control.enable(self.runs, mid)
        code = """
import json, os, sys
from pathlib import Path
from unittest import mock
import fleet_herdr_archive as archive
import fleet_artifacts as artifacts
import fleet_mission_state as state
inputs = json.loads(Path(sys.argv[1]).read_text())
boundary = inputs['boundary']
real_put, real_append, real_write = artifacts.put_bytes, state.MissionTransaction.append_event, archive._write
def put(runs, mid, content):
    if boundary == 'cas_pending' and content == state.ledger_path(runs, mid).read_bytes():
        os.environ['FLEET_TEST_SAFE_PATH_CRASH_AT'] = 'after_atomic_pending_fsync'
    return real_put(runs, mid, content)
def append(transaction, **kwargs):
    if kwargs['kind'] == 'herdr_archive_selected' and boundary.startswith('selection_'):
        os.environ['FLEET_TEST_MISSION_TRANSACTION_CRASH_AT'] = (
            'after_pending_fsync' if boundary == 'selection_pending' else 'after_publish')
    return real_append(transaction, **kwargs)
def write(store, relative, content):
    if boundary == 'ledger_pending' and 'herdr-archive' in relative.parts and relative.name == 'ledger.jsonl':
        os.environ['FLEET_TEST_SAFE_PATH_CRASH_AT'] = 'after_atomic_pending_fsync'
    return real_write(store, relative, content)
with mock.patch.object(artifacts, 'put_bytes', side_effect=put), mock.patch.object(
        state.MissionTransaction, 'append_event', new=append), mock.patch.object(archive, '_write', side_effect=write):
    archive.create(Path(inputs['runs']), inputs['mid'], Path(inputs['candidate']), inputs['roles'], inputs['backend'])
"""
        for boundary in ("cas_pending", "selection_pending", "selection_published", "ledger_pending"):
            for growth in (False, True):
                with self.subTest(boundary=boundary, live_growth=growth), tempfile.TemporaryDirectory() as temporary:
                    runs = Path(temporary) / "runs"
                    shutil.copytree(self.runs, runs)
                    input_path = Path(temporary) / "input.json"
                    input_path.write_bytes(state.canonical_bytes({"boundary": boundary, "runs": str(runs),
                        "mid": mid, "candidate": str(self.repo), "roles": roles, "backend": backend}))
                    child = subprocess.run([sys.executable, "-B", "-c", code, str(input_path)],
                        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "scripts"),
                             "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True)
                    self.assertEqual(child.returncode, 137 if boundary.startswith("selection_") else -9, child.stderr)
                    selected = fleet_mission.load_state(runs, mid).get("herdr_archive_selection")
                    self.assertEqual(selected is not None, boundary in {"selection_published", "ledger_pending"})
                    if growth:
                        control.request(runs, mid, action="resume", reason="process restart",
                                        idempotency_key="process-restart")
                    result = archive.create(runs, mid, self.repo, roles, backend)
                    self.assertTrue(result["staged_valid"])
                    if selected:
                        self.assertEqual(fleet_mission.load_state(runs, mid)["herdr_archive_selection"], selected)
                        self.assertEqual(result["index_sha256"], selected["index_artifact_id"])
                    events = state.read_events(state.ledger_path(runs, mid))
                    self.assertEqual(sum(e["kind"] == "herdr_archive_selected" for e in events), 1)

    def test_selected_functional_receipt_survives_retry_without_execution(self):
        from tests.test_fleet_functional import GOOD, synthetic_spec
        import fleet_functional as functional
        import fleet_herdr_control as control
        runner = functional.runner
        spec = synthetic_spec()
        (self.repo / "sample_stats.py").write_text(GOOD)
        mid, roles, backend = self.archive_fixture(runtime_options={"functional_contract": spec, "herdr_session": "fixture"})
        frozen = archive.freeze(self.runs, mid, self.repo)
        contract = functional.contract_for(mid, frozen, spec)
        limits = spec["limits"]
        # Synthetic retained runtime evidence; no Docker or provider is invoked.
        config = {"Image": spec["runtime"]["image_id"], "Name": "/fleet-functional-" + contract["attempt_id"],
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
                "Memory": limits["memory_bytes"], "MemorySwap": limits["memory_bytes"], "PidsLimit": limits["processes"],
                "NanoCpus": 1000000000, "ShmSize": limits["shm_bytes"], "CapDrop": ["ALL"],
                "IpcMode": "private", "SecurityOpt": ["no-new-privileges=true"], "LogConfig": {"Type": "none"}},
            "Config": {"User": "65534:65534", "WorkingDir": "/candidate", "Entrypoint": [runner.ARGV[0]],
                "Cmd": runner.ARGV[1:], "Labels": {"fleet.functional.attempt": contract["attempt_id"]}},
            "Mounts": [{"Type": "bind", "Destination": dest, "Source": source, "RW": False}
                       for dest, source in (("/candidate", str(self.repo)), ("/harness", "/synthetic-harness"))]}
        hello = {"kind": "runtime", "python": spec["runtime"]["python_version"], "uid": 65534,
            "cwd": "/candidate", "env": runner.ENV, "cap_eff": "0000000000000000", "no_new_privs": "1", "seccomp": "2",
            "cgroup": {"memory.max": str(limits["memory_bytes"]), "memory.swap.max": "0",
                "pids.max": str(limits["processes"]), "cpu.max": "100000 100000"},
            "interfaces": {"lo": 1}, "seccomp_filters": 2, "ipv4_routes": [],
            "readonly": {"/": True, "/candidate": True, "/harness": True},
            "tmp_bytes": limits["tmp_bytes"], "shm_bytes": limits["shm_bytes"],
            "rlimits": {"RLIMIT_CPU": [limits["cpu_seconds"]] * 2, "RLIMIT_FSIZE": [limits["file_bytes"]] * 2,
                "RLIMIT_NOFILE": [limits["open_files"]] * 2, "RLIMIT_CORE": [0, 0]}}
        outcome = {"status": "passed", "reason": "synthetic R05 fixture", "evidence": {
            "container-config.json": state.canonical_bytes(config),
            "container-state.json": state.canonical_bytes({"ExitCode": 0, "Running": False, "OOMKilled": False}),
            "test-result.json": state.canonical_bytes({"tests_run": 5, "failures": 0, "errors": 0, "passed": True}),
            "test-output.txt": b"synthetic test output", "stdout.txt": state.canonical_bytes(hello) + b"\n{}\n", "stderr.txt": b""}}
        with mock.patch.object(runner, "execute", return_value=outcome) as execute:
            receipt = functional.run(self.runs, mid, frozen)
            execute.assert_called_once()
        control.enable(self.runs, mid)
        original = archive._write
        def interrupt(store, relative, content):
            original(store, relative, content)
            if "herdr-archive" in relative.parts and relative.name == "ledger.jsonl":
                raise OSError("functional archive interruption")
        with mock.patch.object(runner, "execute", side_effect=AssertionError("functional execution replayed")), \
                mock.patch.object(functional, "run", side_effect=AssertionError("functional run revisited")):
            with mock.patch.object(archive, "_write", side_effect=interrupt):
                with self.assertRaisesRegex(OSError, "functional archive interruption"):
                    archive.create(self.runs, mid, self.repo, roles, backend)
            control.request(self.runs, mid, action="resume", reason="functional archive restart",
                            idempotency_key="functional-archive-restart")
            result = archive.recover(self.runs, mid)
            self.assertEqual(result["archive_schema_version"], 4)
            self.assertEqual(result["functional"], receipt)
            self.assertEqual(archive.recover(self.runs, mid), result)
