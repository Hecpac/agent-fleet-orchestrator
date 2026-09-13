from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleet_acceptance
import fleet_admission
import fleet_artifacts
import fleet_herdr_mission as driver
import fleet_mission
import fleet_mission_state as state
import fleet_safe_paths
import workflow_config


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE, text=True).strip()


class FakeBackend:
    calls = []
    tasks = {}
    observations = {}
    results = {}
    crash_stage = None
    missing_stage = None
    bad_result = None
    bad_context = None
    role_status = "PASS"
    mutate_review = False
    closed = False
    teardown_error = False

    def __init__(self, runs_dir, mission_id, *, feature, target_repo, compiled, session):
        self.runs, self.mid, self.repo, self.compiled = runs_dir, mission_id, target_repo, compiled
        self.session = session

    def boot(self):
        self.calls.append(("boot", self.repo, self.session))
        assert self.repo.name == "candidate"
        driver._Driver(self.runs, self.mid).write("herdr-backend.json", self.state())
        return self.state()

    def artifact_path(self, stage):
        return "answer.txt" if (self.repo / "answer.txt").exists() else "README.md"

    def state(self):
        return {"schema_version": 2, "backend_version": "0.8.2",
                "mission_id": self.mid, "compiled_digest": self.compiled["compiled_digest"],
                "generation": str(uuid.uuid5(uuid.UUID(self.mid), "fixture-generation")),
                "session": self.session, "workspace": {"closed": self.closed}}

    def submit(self, run_id, prompt, *, instance_id):
        task = json.loads(prompt)
        current = fleet_mission.load_state(self.runs, self.mid)
        admission = next(a for a in current["admissions"].values() if a["run_id"] == run_id)
        assert admission["phase"] == "authorized", "submit preceded admission authorization"
        assert admission["task_sha256"] == state.artifact_id(prompt)
        assert self.repo != Path(current["target_repo"])
        assert (current["active_writer"] == admission["admission_id"]) == (instance_id == "worker")
        assert all(not a["active"] for a in current["admissions"].values() if a["run_id"] != run_id)
        assert run_id not in self.tasks, "duplicate submit"
        self.calls.append(("submit", task["stage"], run_id))
        self.tasks[run_id] = task
        self.observations[run_id] = {"status": "working"}
        if task["stage"] == "build":
            (self.repo / "answer.txt").write_text("implemented\n")
        if task["stage"] == "review":
            assert (self.runs / "missions" / self.mid / "herdr-freeze.json").exists()
            if self.mutate_review:
                (self.repo / "answer.txt").write_text("tampered\n")
        path = self.artifact_path(task["stage"])
        self.results[run_id] = {"schema_version": 1, "mission_id": self.mid, "run_id": run_id,
            "instance_id": instance_id, "status": self.role_status, "summary": f"Evidence for {task['stage']}",
            "candidate_tree_sha": task["frozen_candidate"]["tree_sha"] if task["frozen_candidate"] else None,
            "artifacts": [{"path": path, "sha256": state.artifact_id((self.repo / path).read_bytes())}]}
        if self.bad_result:
            self.bad_result(self.results[run_id])
        final_bytes = state.canonical_bytes(self.results[run_id])
        final = fleet_artifacts.put_bytes(self.runs, self.mid, final_bytes)
        turn_id = f"turn-{run_id}"
        agent_session = str(uuid.uuid5(uuid.UUID(self.mid), f"session:{instance_id}"))
        member = next(m for m in [self.compiled["resolved"].get("lead"), *self.compiled["resolved"]["instances"]]
                      if m is not None and m["instance_id"] == instance_id)
        rows = [
            {"type": "session_meta", "payload": {"id": agent_session, "model_provider": "openai"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": prompt}]}},
            {"type": "turn_context", "payload": {"turn_id": turn_id, "model": member["model"], "effort": "high",
                "cwd": str(self.repo), "approval_policy": "never", "sandbox_policy": (
                    {"type": "workspace-write", "network_access": False, "exclude_tmpdir_env_var": False,
                     "exclude_slash_tmp": False} if instance_id == "worker" else {"type": "read-only"})}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": final_bytes.decode()}]}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id,
                "last_agent_message": final_bytes.decode()}},
        ]
        if self.bad_context:
            self.bad_context(task["stage"], rows[3]["payload"])
        transcript = b"".join(state.canonical_bytes({**row, "timestamp": f"2026-09-06T00:00:{i:02d}Z"}) + b"\n"
                              for i, row in enumerate(rows))
        transcript_id = fleet_artifacts.put_bytes(self.runs, self.mid, transcript)["artifact_id"]
        self.results[run_id].update(artifact_id=final["artifact_id"], turn_id=turn_id,
            evidence={"herdr_session": self.session, "prompt_sha256": state.artifact_id(prompt),
                "agent_session": {"kind": "id", "value": agent_session, "agent": "codex", "source": "codex"},
                "transcript_sha256": transcript_id, "transcript_artifact_id": transcript_id})
        envelope = fleet_artifacts.put_bytes(self.runs, self.mid, state.canonical_bytes(self.results[run_id]))
        self.results[run_id]["result_artifact_id"] = envelope["artifact_id"]
        if self.crash_stage == task["stage"]:
            raise RuntimeError("process lost after submit")
        return self.observations[run_id]

    def recover(self, run_id):
        self.calls.append(("recover", run_id))
        return self.observations.get(run_id, {"status": "indeterminate"})

    def wait(self, run_id, *, timeout_ms):
        self.calls.append(("wait", run_id, timeout_ms))
        return {"status": "settled"}

    def collect_result(self, run_id):
        self.calls.append(("collect", run_id))
        if self.tasks.get(run_id, {}).get("stage") == self.missing_stage:
            return None
        return self.results.get(run_id)

    def cancel(self, run_id):
        self.calls.append(("cancel", run_id))
        return {"status": "indeterminate"}

    def teardown(self):
        self.calls.append(("teardown",))
        assert fleet_mission.load_state(self.runs, self.mid)["status"] in state.TERMINAL_STATUSES
        if self.teardown_error:
            raise RuntimeError("teardown transport unavailable")
        type(self).closed = True
        return True


class FakeArchive:
    """Independent candidate fingerprint and final-artifact predicate, no providers."""
    def __init__(self):
        self.frozen = None
        self.result = None
        self.valid = True
        self.acceptance = "accepted"

    def freeze(self, runs, mid, candidate):
        files = {str(p.relative_to(candidate)): p.read_text() for p in candidate.rglob("*")
                 if p.is_file() and ".git" not in p.relative_to(candidate).parts}
        content = state.canonical_bytes(files)
        digest = fleet_artifacts.put_bytes(runs, mid, content)["artifact_id"]
        value = {"tree_sha": digest[:40], "tree_artifact_id": digest,
                 "patch_artifact_id": fleet_artifacts.put_bytes(runs, mid, b"fixture patch")["artifact_id"]}
        if self.frozen is not None and self.frozen != value:
            raise RuntimeError("frozen candidate drift")
        self.frozen = value
        return value

    def create(self, runs, mid, candidate, role_results, backend_state):
        current = fleet_mission.load_state(runs, mid)
        assert current["status"] in {"completing", "archived"}
        assert not any(a["active"] for a in current["admissions"].values())
        assert set(role_results) == {"lead", "worker", "reviewer", "verifier"}
        assert all(r["status"] == "PASS" for r in role_results.values())
        assert (candidate / "answer.txt").read_text() == "implemented\n"
        for role, result in role_results.items():
            assert state.loads_strict(fleet_artifacts.get_bytes(runs, mid, result["artifact_id"]))["summary"].startswith("Evidence")
        self.freeze(runs, mid, candidate)
        anchored = current["status"] == "archived"
        self.result = {"valid": anchored, "staged_valid": True, "anchored": anchored,
                       "index_sha256": state.sha256(role_results),
                       "acceptance": {"status": self.acceptance}, "path": str(candidate.parent / "herdr-archive" / "archive-index.json")}
        with fleet_safe_paths.RootedFS(runs) as fs:
            fs.atomic_write(Path("missions") / mid / "herdr-archive" / "archive-index.json",
                state.canonical_bytes(self.result), directory_modes=(0o700, 0o700, 0o700), file_mode=0o600)
        return self.result

    def verify(self, runs, mid, *, require_anchor=True, for_completion=False):
        events = state.read_events(state.ledger_path(runs, mid))
        anchors = [event for event in events if event["kind"] == "archive_created"]
        assert len(anchors) == 1 if require_anchor else len(anchors) <= 1, "public verification requires exactly one durable anchor"
        if anchors:
            assert anchors[0]["payload"] == {"path": self.result["path"], "sha256": self.result["index_sha256"], "mode": "herdr"}
        assert not any(event["kind"] == "mission_terminal" for event in events), "terminal preceded public verification"
        policy = fleet_mission.load_state(runs, mid).get("herdr_finalization_policy")
        return {**self.result, "valid": self.valid if require_anchor else bool(anchors), "anchored": bool(anchors),
                "archive_schema_version": 3, "permissions": {"status": "attested", "policy_version": 1, "runs": 5},
                **({"finalization_policy_event_sha256": policy["event_sha256"]} if for_completion else {})}


class HerdrMissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="herdr-driver-test-")
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name).resolve()
        self.target = self.tmp / "target"
        self.target.mkdir()
        git(self.target, "init")
        (self.target / "README.md").write_text("baseline\n")
        git(self.target, "add", "README.md")
        # Fixture history only; never commits the working repository under test.
        git(self.target, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "--no-gpg-sign", "-m", "fixture")
        self.head = git(self.target, "rev-parse", "HEAD")
        self.runs = self.tmp / "runs"
        self.compiled = workflow_config.compile_path(ROOT / "workflows/herdr-implementation.yaml")
        FakeBackend.calls, FakeBackend.tasks, FakeBackend.observations, FakeBackend.results = [], {}, {}, {}
        FakeBackend.crash_stage = FakeBackend.missing_stage = FakeBackend.bad_result = None
        FakeBackend.bad_context = None
        FakeBackend.role_status, FakeBackend.mutate_review = "PASS", False
        FakeBackend.closed = FakeBackend.teardown_error = False
        self.archive = FakeArchive()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(driver.fleet_herdr, "HerdrBackend", FakeBackend).start()
        mock.patch.object(driver, "_archive", return_value=self.archive).start()
        self.mid = self.create()

    def create(self, *, objective="Implement a local answer artifact", options=None, key="fixture", teardown=False, timeout=7200):
        contract = {"schema_version": 1, "requirements": [{"id": "answer", "description": "answer exists",
            "checks": [{"kind": "text_contains", "path": "answer.txt", "expected": "implemented"}]}]}
        opts = {"herdr_session": "mission-fixture", "acceptance_contract": contract,
                "teardown": teardown, "timeout_seconds": timeout}
        if options is not None:
            opts = options
        return fleet_mission.create_mission(self.runs, compiled=self.compiled, feature="driver-test",
            objective=objective, target_repo=self.target, base_sha=self.head,
            idempotency_key=fleet_acceptance.bound_key(key, contract) if opts.get("acceptance_contract") else key,
            runtime_options=opts)[0]

    def run_driver(self):
        return driver.drive(self.runs, self.mid)

    def test_archive_retry_after_control_growth_reuses_selected_snapshot(self):
        import fleet_herdr_archive as archive
        import fleet_herdr_control as control
        original = archive._write

        def interrupt(store, relative, content):
            original(store, relative, content)
            if "herdr-archive" in relative.parts and relative.name == "ledger.jsonl":
                raise OSError("interrupted after archive ledger publication")

        with mock.patch.object(driver, "_archive", return_value=archive), \
                mock.patch.object(driver.fleet_functional.runner, "execute",
                                  side_effect=AssertionError("functional work replayed")) as functional:
            with mock.patch.object(archive, "_write", side_effect=interrupt):
                with self.assertRaisesRegex(OSError, "interrupted after archive"):
                    self.run_driver()
            root = self.runs / "missions" / self.mid / "herdr-archive"
            selected_ledger = (root / "ledger.jsonl").read_bytes()
            self.assertEqual(len(self.submitted()), 5)
            control.request(self.runs, self.mid, action="pause", reason="archive recovery",
                            idempotency_key="archive-pause")
            self.assertEqual(self.run_driver()["control"]["applied"], "paused")
            control.request(self.runs, self.mid, action="resume", reason="archive recovery",
                            idempotency_key="archive-resume")
            try:
                with mock.patch.object(driver._Driver, "prepare_candidate", side_effect=AssertionError("candidate reopened")), \
                        mock.patch.object(driver._Driver, "backend", side_effect=AssertionError("backend reopened")), \
                        mock.patch.object(driver.fleet_functional, "run", side_effect=AssertionError("functional recovery revisited")):
                    result = self.run_driver()
            except archive.HerdrArchiveError as exc:
                self.fail(f"archive retry must reuse its selected snapshot: {exc}")
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual((root / "ledger.jsonl").read_bytes(), selected_ledger)
            self.assertTrue(archive.verify(self.runs, self.mid)["valid"])
            self.assertEqual(self.run_driver()["status"], "succeeded")
            self.assertEqual(len(self.submitted()), 5)
            functional.assert_not_called()
            events = state.read_events(state.ledger_path(self.runs, self.mid))
            self.assertEqual(sum(e["kind"] == "archive_created" for e in events), 1)
            self.assertEqual(sum(e["kind"] == "herdr_archive_selected" for e in events), 1)

    def test_archive_completion_boundary_matrix(self):
        import fleet_herdr_archive as archive
        import fleet_herdr_control as control
        boundaries = ("before_completion_verify", "after_completion_verify", "before_anchor",
                      "after_anchor", "before_terminal", "after_terminal")
        for boundary in boundaries:
            for growth in (False, True):
                if boundary == "after_terminal" and growth:
                    continue  # The existing reducer forbids every post-terminal append.
                with self.subTest(boundary=boundary, live_growth=growth):
                    helper = HerdrMissionTests()
                    helper.setUp()
                    try:
                        original_event, original_verify = state.append_event, archive.verify
                        def interrupt():
                            raise OSError("completion boundary interruption")
                        def event(*args, **kwargs):
                            kind = kwargs["kind"]
                            relevant = (kind == "archive_created" and boundary.endswith("anchor")
                                        or kind == "mission_terminal" and boundary.endswith("terminal"))
                            if relevant and boundary.startswith("before_"):
                                interrupt()
                            result = original_event(*args, **kwargs)
                            if relevant and boundary.startswith("after_"):
                                interrupt()
                            return result
                        def verify(*args, **kwargs):
                            relevant = kwargs.get("for_completion") and kwargs.get("require_anchor") is False
                            if relevant and boundary == "before_completion_verify":
                                interrupt()
                            result = original_verify(*args, **kwargs)
                            if relevant and boundary == "after_completion_verify":
                                interrupt()
                            return result
                        with mock.patch.object(driver, "_archive", return_value=archive), \
                                mock.patch.object(driver.fleet_functional.runner, "execute", side_effect=AssertionError("functional replay")):
                            with mock.patch.object(state, "append_event", side_effect=event), \
                                    mock.patch.object(archive, "verify", side_effect=verify):
                                with self.assertRaisesRegex(OSError, "completion boundary"):
                                    helper.run_driver()
                            selected = fleet_mission.load_state(helper.runs, helper.mid)["herdr_archive_selection"]
                            root = helper.runs / "missions" / helper.mid / "herdr-archive"
                            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                            if growth:
                                control.request(helper.runs, helper.mid, action="resume", reason="completion restart",
                                                idempotency_key="completion-restart")
                            self.assertEqual(helper.run_driver()["status"], "succeeded")
                            self.assertEqual(helper.run_driver()["status"], "succeeded")
                            self.assertEqual(len(helper.submitted()), 5)
                            self.assertEqual(fleet_mission.load_state(helper.runs, helper.mid)["herdr_archive_selection"], selected)
                            self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()})
                            events = state.read_events(state.ledger_path(helper.runs, helper.mid))
                            self.assertEqual(sum(e["kind"] == "archive_created" for e in events), 1)
                            self.assertEqual(sum(e["kind"] == "mission_terminal" for e in events), 1)
                    finally:
                        helper.doCleanups()

    def test_concurrent_archive_completion_uses_existing_driver_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        import fleet_herdr_archive as archive
        entered, release = threading.Event(), threading.Event()
        original = archive.create
        def held(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError("fixture did not release archive completion")
            return original(*args, **kwargs)
        with mock.patch.object(driver, "_archive", return_value=archive), \
                mock.patch.object(archive, "create", side_effect=held), ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.run_driver)
            try:
                self.assertTrue(entered.wait(10))
                other = self.run_driver()
                self.assertEqual(other["next_action"], "another Herdr driver owns this mission")
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10)["status"], "succeeded")
        self.assertEqual(self.run_driver()["status"], "succeeded")
        self.assertEqual(len(self.submitted()), 5)
        events = state.read_events(state.ledger_path(self.runs, self.mid))
        self.assertEqual(sum(e["kind"] == "herdr_archive_selected" for e in events), 1)
        self.assertEqual(sum(e["kind"] == "archive_created" for e in events), 1)

    def test_selected_partial_still_obeys_pause_and_cancel(self):
        import fleet_herdr_archive as archive
        import fleet_herdr_control as control
        for action in ("pause", "cancel"):
            with self.subTest(action=action):
                helper = HerdrMissionTests()
                helper.setUp()
                try:
                    original = archive._write
                    def interrupt(store, relative, content):
                        original(store, relative, content)
                        if "herdr-archive" in relative.parts and relative.name == "ledger.jsonl":
                            raise OSError("control archive interruption")
                    with mock.patch.object(driver, "_archive", return_value=archive):
                        with mock.patch.object(archive, "_write", side_effect=interrupt):
                            with self.assertRaisesRegex(OSError, "control archive"):
                                helper.run_driver()
                        selected = fleet_mission.load_state(helper.runs, helper.mid)["herdr_archive_selection"]
                        control.request(helper.runs, helper.mid, action=action, reason="selected archive control",
                                        idempotency_key="selected-control")
                        result = helper.run_driver()
                        self.assertEqual(result["control"]["applied"], "paused" if action == "pause" else "cancelled")
                        events = state.read_events(state.ledger_path(helper.runs, helper.mid))
                        self.assertFalse(any(e["kind"] == "archive_created" for e in events))
                        if action == "pause":
                            control.request(helper.runs, helper.mid, action="resume", reason="resume selected archive",
                                            idempotency_key="selected-resume")
                            self.assertEqual(helper.run_driver()["status"], "succeeded")
                        else:
                            self.assertEqual(result["status"], "abandoned")
                        self.assertEqual(len(helper.submitted()), 5)
                        self.assertEqual(fleet_mission.load_state(helper.runs, helper.mid)["herdr_archive_selection"], selected)
                finally:
                    helper.doCleanups()

    def submitted(self):
        return [c[1] for c in FakeBackend.calls if c[0] == "submit"]

    def test_complete_sequence_admission_writer_cas_archive_and_source_preserved(self):
        result = self.run_driver()
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["archive"]["valid"])
        self.assertTrue(result["archive"]["anchored"])
        self.assertEqual(self.submitted(), ["plan", "build", "review", "verify", "synthesis"])
        self.assertEqual(set(result["role_results"]), {"lead", "worker", "reviewer", "verifier"})
        current = fleet_mission.load_state(self.runs, self.mid)
        self.assertEqual(current["delegation_credits_spent"], 5)
        self.assertTrue(all(a["phase"] == "finalized" for a in current["admissions"].values()))
        self.assertEqual(sum(a["writer"] for a in current["admissions"].values()), 1)
        self.assertIsNotNone(current["synthesis_result"])
        self.assertEqual(git(self.target, "status", "--porcelain"), "")
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.head)
        self.assertFalse((self.target / "answer.txt").exists())
        state.verify_events(state.read_events(state.ledger_path(self.runs, self.mid)))
        fleet_artifacts.verify_store(self.runs, self.mid)

    def test_terminal_ledger_returns_without_backend_or_compiled_reads(self):
        self.run_driver()
        FakeBackend.calls.clear()
        with mock.patch.object(driver._Driver, "load", side_effect=AssertionError("terminal must be ledger first")):
            self.assertEqual(self.run_driver()["status"], "succeeded")
        self.assertEqual(FakeBackend.calls, [])

    def test_risk_high_blocks_before_candidate_or_boot(self):
        self.mid = self.create(objective="deploy to production", key="high")
        result = self.run_driver()
        self.assertEqual(result["status"], "awaiting_assurance_confirmation")
        self.assertIn("no CMUX fallback", result["next_action"])
        self.assertFalse((self.runs / "missions" / self.mid / "candidate").exists())
        self.assertEqual(FakeBackend.calls, [])

    def test_dirty_baseline_fails_before_clone_and_boot(self):
        (self.target / "untracked.txt").write_text("preserve")
        with self.assertRaisesRegex(driver.HerdrMissionError, "dirty baseline"):
            self.run_driver()
        self.assertEqual((self.target / "untracked.txt").read_text(), "preserve")
        self.assertEqual(FakeBackend.calls, [])

    def test_hidden_assume_unchanged_baseline_is_rejected(self):
        git(self.target, "update-index", "--assume-unchanged", "README.md")
        (self.target / "README.md").write_text("hidden dirty baseline")
        self.assertEqual(git(self.target, "status", "--porcelain"), "")
        with self.assertRaisesRegex(driver.HerdrMissionError, "index flags"):
            self.run_driver()
        self.assertEqual(FakeBackend.calls, [])
        self.assertFalse((self.runs / "missions" / self.mid / "candidate").exists())

    def test_candidate_git_rebinding_cannot_target_source_checkout(self):
        FakeBackend.missing_stage = "plan"
        self.run_driver()
        candidate = self.runs / "missions" / self.mid / "candidate"
        (candidate / ".git").rename(candidate / ".git.original")
        (candidate / ".git").symlink_to(self.target / ".git", target_is_directory=True)
        FakeBackend.calls.clear()
        with self.assertRaisesRegex(driver.HerdrMissionError, "independent physical directory"):
            self.run_driver()
        self.assertEqual(FakeBackend.calls, [])

    def test_objective_tamper_rejected_before_effect(self):
        (self.runs / "missions" / self.mid / "objective.txt").write_text("different")
        with self.assertRaisesRegex(driver.HerdrMissionError, "objective digest"):
            self.run_driver()
        self.assertEqual(FakeBackend.calls, [])

    def test_options_tamper_rejected_before_effect(self):
        (self.runs / "missions" / self.mid / "runtime-options.json").write_text('{"herdr_session":"foreign"}')
        with self.assertRaisesRegex(driver.HerdrMissionError, "runtime options"):
            self.run_driver()

    def test_session_required_without_default(self):
        self.mid = self.create(options={}, key="no-session")
        with self.assertRaisesRegex(driver.HerdrMissionError, "herdr_session"):
            self.run_driver()

    def test_settled_is_not_a_result_and_resume_does_not_resubmit(self):
        FakeBackend.missing_stage = "build"
        result = self.run_driver()
        self.assertEqual(result["status"], "running")
        self.assertIn("durable role result", result["next_action"])
        self.assertEqual(self.submitted(), ["plan", "build"])
        self.run_driver()
        self.assertEqual(self.submitted(), ["plan", "build"])
        FakeBackend.missing_stage = None
        self.assertEqual(self.run_driver()["status"], "succeeded")
        self.assertEqual(self.submitted().count("build"), 1)

    def test_crash_after_send_recovers_without_duplicate(self):
        FakeBackend.crash_stage = "build"
        with self.assertRaisesRegex(RuntimeError, "process lost"):
            self.run_driver()
        FakeBackend.crash_stage = None
        self.assertEqual(self.run_driver()["status"], "succeeded")
        self.assertEqual(self.submitted().count("build"), 1)

    def test_crash_after_authorization_before_send_never_retries(self):
        with mock.patch.object(FakeBackend, "submit", side_effect=RuntimeError("before send")):
            with self.assertRaisesRegex(RuntimeError, "before send"):
                self.run_driver()
        result = self.run_driver()
        self.assertEqual(result["status"], "running")
        self.assertEqual(self.submitted(), [])
        self.assertTrue(any(c[0] == "recover" for c in FakeBackend.calls))

    def test_cas_result_recovers_after_crash_before_finalize(self):
        with mock.patch.object(fleet_admission, "finalize", side_effect=RuntimeError("before finalize")):
            with self.assertRaisesRegex(RuntimeError, "before finalize"):
                self.run_driver()
        self.assertEqual(self.run_driver()["status"], "succeeded")
        self.assertEqual(self.submitted().count("plan"), 1)

    def test_wrong_artifact_digest_rejected(self):
        FakeBackend.bad_result = staticmethod(lambda r: r["artifacts"][0].update(sha256="0" * 64))
        with self.assertRaisesRegex(driver.HerdrMissionError, "artifact digest"):
            self.run_driver()
        self.assertEqual(self.submitted(), ["plan"])

    def test_incompatible_planning_permissions_do_not_finalize_or_start_worker(self):
        FakeBackend.bad_context = staticmethod(lambda stage, c: c.pop("approval_policy"))
        result = self.run_driver()
        self.assertEqual(result["status"], "running")
        self.assertEqual(self.submitted(), ["plan"])
        admission = next(iter(fleet_mission.load_state(self.runs, self.mid)["admissions"].values()))
        self.assertTrue(admission["active"])
        self.assertNotEqual(admission["phase"], "finalized")
        # The ledger-CAS recovery path must enforce the same policy without replay.
        result = self.run_driver()
        self.assertEqual(result["status"], "running")
        self.assertEqual(self.submitted(), ["plan"])

    def test_worker_network_permission_drift_does_not_start_review(self):
        def mutate(stage, context):
            if stage == "build":
                context["sandbox_policy"]["network_access"] = True
        FakeBackend.bad_context = staticmethod(mutate)
        self.assertEqual(self.run_driver()["status"], "running")
        self.assertEqual(self.submitted(), ["plan", "build"])
        self.assertIsNone(fleet_mission.load_state(self.runs, self.mid)["synthesis_result"])

    def test_wrong_run_binding_rejected(self):
        FakeBackend.bad_result = staticmethod(lambda r: r.update(run_id="foreign"))
        with self.assertRaisesRegex(driver.HerdrMissionError, "run_id binding"):
            self.run_driver()

    def test_path_escape_rejected(self):
        FakeBackend.bad_result = staticmethod(lambda r: r["artifacts"][0].update(path="../objective.txt"))
        with self.assertRaisesRegex(driver.HerdrMissionError, "safe candidate-relative"):
            self.run_driver()

    def test_role_fail_stops_later_submits(self):
        FakeBackend.role_status = "FAIL"
        self.assertEqual(self.run_driver()["status"], "failed")
        self.assertEqual(self.submitted(), ["plan"])

    def test_read_only_candidate_drift_prevents_verify_and_acceptance(self):
        FakeBackend.mutate_review = True
        with self.assertRaisesRegex(RuntimeError, "frozen candidate drift"):
            self.run_driver()
        self.assertEqual(self.submitted(), ["plan", "build", "review"])

    def test_archive_independent_verification_must_pass(self):
        self.archive.valid = False
        with self.assertRaisesRegex(driver.HerdrMissionError, "independent verification"):
            self.run_driver()
        self.assertEqual(fleet_mission.load_state(self.runs, self.mid)["status"], "archived")
        self.archive.valid = True
        self.assertEqual(self.run_driver()["status"], "succeeded")
        self.assertEqual(len(self.submitted()), 5)

    def test_acceptance_rejection_is_failure_despite_all_role_passes(self):
        self.archive.acceptance = "rejected"
        result = self.run_driver()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(all(r["status"] == "PASS" for r in result["role_results"].values()))

    def test_not_evaluated_archive_cannot_succeed(self):
        self.archive.acceptance = "not_evaluated"
        result = self.run_driver()
        self.assertEqual(result["status"], "archived")
        self.assertIn("next_action", result)

    def test_second_driver_is_excluded_by_mission_lock(self):
        relative = Path("missions") / self.mid / "herdr-driver.lock"
        with fleet_safe_paths.RootedFS(self.runs) as fs:
            with fs.exclusive_lock(relative, directory_modes=(0o700, 0o700)):
                self.assertIn("another Herdr driver", self.run_driver()["next_action"])
        self.assertEqual(FakeBackend.calls, [])

    def test_real_archive_end_to_end_with_real_candidate_git_and_cas(self):
        import fleet_herdr_archive
        with mock.patch.object(driver, "_archive", return_value=fleet_herdr_archive):
            result = self.run_driver()
            verified = fleet_herdr_archive.verify(self.runs, self.mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["acceptance"]["status"], "accepted")
        self.assertEqual(result["archive"]["index_sha256"], verified["index_sha256"])
        candidate = self.runs / "missions" / self.mid / "candidate"
        self.assertEqual(git(candidate, "rev-parse", "HEAD"), self.head)
        self.assertEqual(git(candidate, "diff", "--cached", "--name-only"), "")

    def test_teardown_after_terminal_is_once_and_ledger_first_on_resume(self):
        self.mid = self.create(key="teardown", teardown=True)
        result = self.run_driver()
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["cleanup_pending"])
        FakeBackend.calls.clear()
        with mock.patch.object(driver._Driver, "load", side_effect=AssertionError("already closed")):
            again = self.run_driver()
        self.assertEqual(again["status"], "succeeded")
        self.assertFalse(again["cleanup_pending"])
        self.assertEqual(FakeBackend.calls, [])

    def test_teardown_failure_preserves_verdict_and_reconciles_on_resume(self):
        self.mid = self.create(key="teardown-failure", teardown=True)
        FakeBackend.teardown_error = True
        result = self.run_driver()
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["cleanup_pending"])
        self.assertIn("transport unavailable", result["cleanup_error"])
        original_head = result["head_sha256"]
        FakeBackend.teardown_error = False
        again = self.run_driver()
        self.assertEqual(again["head_sha256"], original_head)
        self.assertFalse(again["cleanup_pending"])
        self.assertEqual(len(self.submitted()), 5)

    def test_worker_prompt_contains_durable_acceptance_contract(self):
        self.run_driver()
        task = next(task for task in FakeBackend.tasks.values() if task["stage"] == "build")
        self.assertEqual(task["acceptance_contract"]["requirements"][0]["checks"][0]["expected"], "implemented")
        self.assertIn("exactly one raw JSON object", task["instructions"])
        self.assertIn("no memory citations", task["instructions"])

    def test_prompts_scope_status_to_assigned_stage_without_future_or_identity_blockers(self):
        self.run_driver()
        tasks = {task["stage"]: task for task in FakeBackend.tasks.values()}
        self.assertEqual(set(tasks), {"plan", "build", "review", "verify", "synthesis"})
        for stage, task in tasks.items():
            with self.subTest(stage=stage):
                self.assertIn("Status judges ONLY your assigned stage", task["instructions"])
                self.assertIn("Do not wait for future stages", task["instructions"])
                self.assertIn("BLOCKED means a real inability to complete your own assigned stage", task["instructions"])
                self.assertIn("controller verifies model and effort from the transcript", task["instructions"])
                self.assertTrue(task["stage_success_criteria"].startswith("PASS when"))
        self.assertIn("viable bounded implementation plan", tasks["plan"]["stage_success_criteria"])
        self.assertIn("actual baseline evidence", tasks["plan"]["stage_success_criteria"])
        self.assertIn("not blockers", tasks["plan"]["stage_success_criteria"])
        self.assertIn("absence does not prevent plan PASS", tasks["plan"]["stage_success_criteria"])
        self.assertIn("run relevant focused tests", tasks["build"]["stage_success_criteria"])
        self.assertIn("read-only review of the frozen candidate", tasks["review"]["stage_success_criteria"])
        self.assertIn("independently reproduce", tasks["verify"]["stage_success_criteria"])
        self.assertIn("reconcile the available Worker, Reviewer and Verifier results", tasks["synthesis"]["stage_success_criteria"])

    def test_review_wrong_frozen_tree_binding_rejected(self):
        def wrong(result):
            if result["instance_id"] == "reviewer":
                result["candidate_tree_sha"] = "0" * 40
        FakeBackend.bad_result = staticmethod(wrong)
        with self.assertRaisesRegex(driver.HerdrMissionError, "different frozen candidate"):
            self.run_driver()
        self.assertNotIn("verify", self.submitted())

    def test_ledger_cancel_reconciles_durable_pass_without_accepting_mission(self):
        FakeBackend.missing_stage = "build"
        self.run_driver()
        current = fleet_mission.load_state(self.runs, self.mid)
        admission = next(a for a in current["admissions"].values() if a["recipient_instance"] == "worker")
        state.append_event(self.runs, self.mid, kind="run_cancel_requested", actor="CONTROL",
            idempotency_key="test:cancel", payload={"run_id": admission["run_id"], "reason": "cancel fixture"})
        FakeBackend.missing_stage = None
        result = self.run_driver()
        self.assertEqual(result["status"], "abandoned")
        self.assertEqual(self.submitted(), ["plan", "build"])
        self.assertNotIn(("cancel", admission["run_id"]), FakeBackend.calls)

    def _explicit_cancel_after_result_crash(self, *, ledger_recorded, expired=False):
        real_event, real_finalize = driver._Driver.event, fleet_admission.finalize
        def crash_event(controller, kind, key, payload):
            if not ledger_recorded and kind == "result_recorded" and key == "build:result":
                raise RuntimeError("crash with durable backend result")
            return real_event(controller, kind, key, payload)
        def crash_finalize(*args, **kwargs):
            if ledger_recorded and kwargs["recipient_instance"] == "worker":
                raise RuntimeError("crash with durable ledger result")
            return real_finalize(*args, **kwargs)
        with mock.patch.object(driver._Driver, "event", autospec=True, side_effect=crash_event), \
                mock.patch.object(fleet_admission, "finalize", side_effect=crash_finalize):
            with self.assertRaisesRegex(RuntimeError, "crash with durable"):
                self.run_driver()
        before = fleet_mission.load_state(self.runs, self.mid)
        writer_id = before["active_writer"]
        admission = before["admissions"][writer_id]
        self.assertEqual(admission["result"] is not None, ledger_recorded)
        state.append_event(self.runs, self.mid, kind="run_cancel_requested", actor="CONTROL",
            idempotency_key="explicit:cancel-after-crash",
            payload={"run_id": admission["run_id"], "reason": "user cancellation after crash"})
        if expired:
            mock.patch.object(driver._Driver, "remaining_seconds", return_value=0).start()
        with mock.patch.object(FakeBackend, "cancel", side_effect=AssertionError("must not signal completed run")), \
                mock.patch.object(FakeBackend, "submit", side_effect=AssertionError("must not open another stage")), \
                mock.patch.object(FakeBackend, "recover", side_effect=AssertionError("durable result first")):
            result = self.run_driver()
        after = fleet_mission.load_state(self.runs, self.mid)
        final = after["admissions"][writer_id]
        self.assertEqual(result["status"], "abandoned")
        self.assertEqual(final["terminal"]["status"], "succeeded")
        self.assertFalse(any(a["active"] for a in after["admissions"].values()))
        self.assertIsNone(after["active_writer"])
        self.assertFalse(after["run_claims"])
        self.assertEqual(set(after["admissions"]), set(before["admissions"]))
        self.assertEqual(self.submitted(), ["plan", "build"])
        envelope = state.loads_strict(fleet_artifacts.get_bytes(self.runs, self.mid, final["result"]["artifact_id"]))
        self.assertEqual(envelope["backend_result_artifact_id"], FakeBackend.results[admission["run_id"]]["result_artifact_id"])
        events = state.read_events(state.ledger_path(self.runs, self.mid))
        state.verify_events(events)
        self.assertFalse(any(e["kind"] == "archive_created" for e in events))
        with mock.patch.object(driver._Driver, "backend", side_effect=AssertionError("terminal ledger first")):
            self.assertEqual(self.run_driver()["status"], "abandoned")
        self.assertEqual(fleet_mission.load_state(self.runs, self.mid)["head_sha256"], after["head_sha256"])

    def test_explicit_cancel_consumes_backend_result_after_crash_before_ledger_record(self):
        self._explicit_cancel_after_result_crash(ledger_recorded=False)

    def test_explicit_cancel_consumes_ledger_result_after_crash_before_finalize(self):
        self._explicit_cancel_after_result_crash(ledger_recorded=True)

    def test_explicit_cancel_with_durable_result_still_abandons_after_deadline(self):
        self._explicit_cancel_after_result_crash(ledger_recorded=False, expired=True)

    def test_confirmed_cancel_finalizes_exact_admission_with_cas_proof(self):
        FakeBackend.missing_stage = "build"
        self.run_driver()
        current = fleet_mission.load_state(self.runs, self.mid)
        admission = next(a for a in current["admissions"].values() if a["recipient_instance"] == "worker")
        state.append_event(self.runs, self.mid, kind="run_cancel_requested", actor="CONTROL",
            idempotency_key="test:cancel-confirmed", payload={"run_id": admission["run_id"], "reason": "cancel fixture"})
        receipt = {"run_id": admission["run_id"], "instance_id": "worker", "prompt_sha256": admission["task_sha256"],
                   "cancel_attempted": True, "status": "abandoned"}
        with mock.patch.object(FakeBackend, "cancel", return_value=receipt):
            result = self.run_driver()
        self.assertEqual(result["status"], "abandoned")
        current = fleet_mission.load_state(self.runs, self.mid)
        final = current["admissions"][admission["admission_id"]]
        self.assertFalse(final["active"])
        proof = state.loads_strict(fleet_artifacts.get_bytes(self.runs, self.mid,
            final["terminal"]["terminal_evidence"]["source_event_sha256"]))
        self.assertEqual(proof["backend_receipt"], receipt)
        self.assertEqual(self.submitted(), ["plan", "build"])

    def test_backend_envelope_id_is_preserved_separately_from_driver_cas(self):
        result = self.run_driver()
        for value in result["role_results"].values():
            self.assertNotEqual(value["backend_result_artifact_id"], value["result_artifact_id"])
            envelope = state.loads_strict(fleet_artifacts.get_bytes(self.runs, self.mid, value["result_artifact_id"]))
            self.assertEqual(envelope, {k: v for k, v in value.items() if k != "result_artifact_id"})

    def test_cached_backend_result_precedes_failing_runtime_recovery(self):
        FakeBackend.crash_stage = "build"
        with self.assertRaisesRegex(RuntimeError, "process lost"):
            self.run_driver()
        FakeBackend.crash_stage = None
        with mock.patch.object(FakeBackend, "recover", side_effect=driver.fleet_herdr.HerdrBackendError("unavailable")):
            result = self.run_driver()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.submitted().count("build"), 1)

    def test_unknown_backend_submission_returns_pending_without_replay(self):
        with mock.patch.object(FakeBackend, "submit", side_effect=driver.fleet_herdr.HerdrBackendError("unknown submit")):
            self.assertIn("without resubmitting", self.run_driver()["next_action"])
        with mock.patch.object(FakeBackend, "recover", side_effect=driver.fleet_herdr.HerdrBackendError("missing submission")):
            result = self.run_driver()
        self.assertIn("missing submission", result["next_action"])
        self.assertEqual(self.submitted(), [])

    def test_expired_durable_deadline_blocks_before_candidate_or_provider(self):
        with mock.patch.object(driver._Driver, "remaining_seconds", return_value=0):
            result = self.run_driver()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(FakeBackend.calls, [])
        self.assertFalse((self.runs / "missions" / self.mid / "candidate").exists())

    def test_timeout_requests_cancel_of_existing_writer_without_new_submit(self):
        FakeBackend.missing_stage = "build"
        self.run_driver()
        with mock.patch.object(driver._Driver, "remaining_seconds", return_value=0):
            result = self.run_driver()
        self.assertIn("cancellation remains pending", result["next_action"])
        current = fleet_mission.load_state(self.runs, self.mid)
        self.assertEqual(len(current["cancelled_runs"]), 1)
        self.assertIsNotNone(current["active_writer"])
        self.assertEqual(self.submitted(), ["plan", "build"])

    def test_missing_backend_ownership_cannot_claim_cleanup_complete(self):
        self.mid = self.create(key="missing-ownership", teardown=True)
        FakeBackend.teardown_error = True
        self.assertTrue(self.run_driver()["cleanup_pending"])
        backend_receipt = self.runs / "missions" / self.mid / "herdr-backend.json"
        backend_receipt.rename(backend_receipt.with_suffix(".lost"))
        FakeBackend.calls.clear()
        result = self.run_driver()
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["cleanup_pending"])
        self.assertIn("ownership receipt missing", result["cleanup_error"])
        self.assertEqual(FakeBackend.calls, [])

    def test_completed_roles_can_finish_archive_after_execution_deadline(self):
        self.archive.valid = False
        with self.assertRaisesRegex(driver.HerdrMissionError, "independent verification"):
            self.run_driver()
        self.archive.valid = True
        with mock.patch.object(driver._Driver, "remaining_seconds", return_value=0):
            result = self.run_driver()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(self.submitted()), 5)

    def expired_runtime_fixture(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=120)
        class CreationClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return past if tz else past.replace(tzinfo=None)
        # Only fixture creation is backdated. Driver clock and admission/ledger
        # implementation remain real throughout the verification below.
        with mock.patch.object(state, "datetime", CreationClock):
            self.mid = self.create(key="runtime-timeout", timeout=60)
        controller = driver._Driver(self.runs, self.mid)
        controller.load()
        current = controller.current()
        self.assertEqual(controller.options["timeout_seconds"], 60)
        self.assertEqual(controller.compiled["workflow"]["limits"]["deadline_seconds"], 7200)
        self.assertLess(controller.remaining_seconds(), 0)
        self.assertGreater(state.parse_timestamp(current["admission_policy"]["deadline_at"], "policy"),
                           datetime.now(timezone.utc))
        return controller

    def test_runtime_timeout_60_overrides_live_compiled_deadline_7200(self):
        self.expired_runtime_fixture()
        result = self.run_driver()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(FakeBackend.calls, [])
        self.assertFalse(fleet_mission.load_state(self.runs, self.mid)["admissions"])

    def test_expired_runtime_retains_real_started_writer_and_ownership(self):
        controller = self.expired_runtime_fixture()
        controller.prepare_candidate()
        controller.event("fleet_boot_started", "boot", {"feature": "driver-test", "preset": "astra_sol"})
        controller.backend().boot()
        controller.event("mission_running", "running", {"manifest": str(controller.root / "herdr-backend.json")})
        request = {"request_key": "herdr:build", "run_kind": "specialist", "recipient_instance": "worker",
                   "capability": "build", "effect_sha256": "a" * 64, "task_sha256": "b" * 64,
                   "delegated_budget": 0, "writer": True}
        admission = fleet_admission.reserve_many(self.runs, self.mid, requests=[request],
            idempotency_key="fixture:active:reserve")["admissions"][0]
        binding = {k: admission[k] for k in ("admission_id", "request_digest", "effect_sha256",
                                             "recipient_instance", "writer", "run_id")}
        committed = fleet_admission.commit(self.runs, self.mid, **binding, idempotency_key="fixture:active:commit")
        authorized = fleet_admission.authorize_launch(self.runs, self.mid, **binding,
            commit_event_sha256=committed["commit_event_sha256"], idempotency_key="fixture:active:authorize")
        fleet_admission.mark_started(self.runs, self.mid, **binding,
            authorization_event_sha256=authorized["authorization_event_sha256"], idempotency_key="fixture:active:started")
        before = controller.current()
        result = self.run_driver()
        after = controller.current()
        self.assertEqual(result["status"], "running")
        self.assertIn("cancellation remains pending", result["next_action"])
        self.assertEqual(after["active_writer"], admission["admission_id"])
        self.assertEqual(after["active_recipients"], before["active_recipients"])
        self.assertEqual(after["run_owners"], before["run_owners"])
        self.assertEqual(after["admissions"][admission["admission_id"]]["phase"], "started")
        self.assertTrue(after["admissions"][admission["admission_id"]]["active"])
        events = state.read_events(state.ledger_path(self.runs, self.mid))
        self.assertEqual([e["kind"] for e in events if e["sequence"] > before["last_sequence"]],
                         ["herdr_finalization_policy_frozen", "run_cancel_requested"])
        self.assertEqual(self.submitted(), [])

    def test_expiry_after_fresh_authorization_releases_unsent_writer(self):
        real_authorize = fleet_admission.authorize_launch
        expired = False
        def authorize(*args, **kwargs):
            nonlocal expired
            result = real_authorize(*args, **kwargs)
            if kwargs["recipient_instance"] == "worker":
                expired = True
            return result
        with mock.patch.object(fleet_admission, "authorize_launch", side_effect=authorize), \
                mock.patch.object(driver._Driver, "remaining_seconds", side_effect=lambda: 0 if expired else 60):
            result = self.run_driver()
        current = fleet_mission.load_state(self.runs, self.mid)
        writer = next(a for a in current["admissions"].values() if a["writer"])
        self.assertEqual(writer["phase"], "finalized")
        self.assertFalse(writer["active"])
        self.assertIsNone(current["active_writer"])
        self.assertIn(writer["run_id"], current["run_owners"])  # immutable ownership history
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.submitted(), ["plan"])

    def test_public_verifier_crash_retains_anchor_and_resume_does_not_resubmit(self):
        real_verify = self.archive.verify
        def crash(*args, **kwargs):
            if kwargs.get("require_anchor", True):
                raise RuntimeError("public verifier unavailable")
            return real_verify(*args, **kwargs)
        with mock.patch.object(self.archive, "verify", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "public verifier unavailable"):
                self.run_driver()
        events = state.read_events(state.ledger_path(self.runs, self.mid))
        self.assertEqual(events[-1]["kind"], "archive_created")
        self.assertEqual(sum(e["kind"] == "archive_created" for e in events), 1)
        self.assertFalse(any(e["kind"] == "mission_terminal" for e in events))
        self.assertEqual(self.run_driver()["status"], "succeeded")
        self.assertEqual(len(self.submitted()), 5)
        events = state.read_events(state.ledger_path(self.runs, self.mid))
        self.assertEqual(sum(e["kind"] == "archive_created" for e in events), 1)

    def test_invalid_staging_receipt_is_never_anchored(self):
        with mock.patch.object(self.archive, "create", return_value={"valid": True, "index_sha256": "bad", "path": "/tmp/index"}):
            with self.assertRaisesRegex(driver.HerdrMissionError, "staging receipt"):
                self.run_driver()
        events = state.read_events(state.ledger_path(self.runs, self.mid))
        self.assertFalse(any(e["kind"] in {"archive_created", "mission_terminal"} for e in events))

    def test_public_verifier_path_must_match_published_anchor(self):
        real_verify = self.archive.verify
        def changed_path(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            return {**result, "path": "/tmp/foreign-index"} if kwargs.get("require_anchor", True) else result
        with mock.patch.object(self.archive, "verify", side_effect=changed_path):
            with self.assertRaisesRegex(driver.HerdrMissionError, "independent verification"):
                self.run_driver()
        self.assertEqual(fleet_mission.load_state(self.runs, self.mid)["status"], "archived")

    def test_timeout_consumes_backend_result_after_crash_before_ledger_record(self):
        real_event = driver._Driver.event
        def crash(controller, kind, key, payload):
            if kind == "result_recorded" and key == "build:result":
                raise RuntimeError("backend terminal persisted before mission result")
            return real_event(controller, kind, key, payload)
        with mock.patch.object(driver._Driver, "event", autospec=True, side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "backend terminal persisted"):
                self.run_driver()
        before = fleet_mission.load_state(self.runs, self.mid)
        writer_id = before["active_writer"]
        self.assertIsNone(before["admissions"][writer_id]["result"])
        with mock.patch.object(driver._Driver, "remaining_seconds", return_value=0), \
                mock.patch.object(FakeBackend, "cancel", side_effect=AssertionError("durable result must precede cancel")):
            result = self.run_driver()
        after = fleet_mission.load_state(self.runs, self.mid)
        self.assertEqual(result["status"], "failed")  # deadline prevented required later roles
        self.assertEqual(after["admissions"][writer_id]["terminal"]["status"], "succeeded")
        self.assertIsNone(after["active_writer"])
        self.assertFalse(after["cancelled_runs"])
        self.assertEqual(self.submitted(), ["plan", "build"])

    def test_timeout_consumes_ledger_result_after_crash_before_finalize(self):
        real_finalize = fleet_admission.finalize
        def crash(*args, **kwargs):
            if kwargs["recipient_instance"] == "worker":
                raise RuntimeError("before writer finalize")
            return real_finalize(*args, **kwargs)
        with mock.patch.object(fleet_admission, "finalize", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "before writer finalize"):
                self.run_driver()
        before = fleet_mission.load_state(self.runs, self.mid)
        writer_id = before["active_writer"]
        self.assertIsNotNone(before["admissions"][writer_id]["result"])
        with mock.patch.object(driver._Driver, "remaining_seconds", return_value=0), \
                mock.patch.object(FakeBackend, "collect_result", side_effect=AssertionError("ledger result first")), \
                mock.patch.object(FakeBackend, "cancel", side_effect=AssertionError("no cancel after durable result")):
            result = self.run_driver()
        after = fleet_mission.load_state(self.runs, self.mid)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(after["admissions"][writer_id]["active"])
        self.assertEqual(after["admissions"][writer_id]["terminal"]["status"], "succeeded")
        self.assertFalse(after["cancelled_runs"])
        self.assertEqual(self.submitted(), ["plan", "build"])

    def test_timeout_with_last_durable_result_can_archive_without_new_turns(self):
        real_finalize = fleet_admission.finalize
        def crash(*args, **kwargs):
            if kwargs["idempotency_key"] == "herdr:synthesis:finalized":
                raise RuntimeError("before synthesis finalize")
            return real_finalize(*args, **kwargs)
        with mock.patch.object(fleet_admission, "finalize", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "before synthesis finalize"):
                self.run_driver()
        with mock.patch.object(driver._Driver, "remaining_seconds", return_value=0), \
                mock.patch.object(FakeBackend, "submit", side_effect=AssertionError("no new turns after deadline")), \
                mock.patch.object(FakeBackend, "cancel", side_effect=AssertionError("result already durable")):
            result = self.run_driver()
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["archive"]["anchored"])
        self.assertEqual(len(self.submitted()), 5)

    def test_real_staged_archive_recovers_without_candidate_or_backend_before_anchor(self):
        import fleet_herdr_archive
        real_event = driver._Driver.event
        def crash(controller, kind, key, payload):
            if kind == "archive_created":
                raise RuntimeError("crash before archive anchor")
            return real_event(controller, kind, key, payload)
        with mock.patch.object(driver, "_archive", return_value=fleet_herdr_archive):
            with mock.patch.object(driver._Driver, "event", autospec=True, side_effect=crash):
                with self.assertRaisesRegex(RuntimeError, "crash before archive anchor"):
                    self.run_driver()
            staged = fleet_herdr_archive.verify(self.runs, self.mid, require_anchor=False)
            self.assertTrue(staged["staged_valid"])
            self.assertFalse(staged["valid"])
            candidate = self.runs / "missions" / self.mid / "candidate"
            candidate.rename(candidate.with_name("candidate-unavailable"))
            with mock.patch.object(driver._Driver, "backend", side_effect=AssertionError("no backend")), \
                    mock.patch.object(driver._Driver, "prepare_candidate", side_effect=AssertionError("no candidate")):
                result = self.run_driver()
            verified = fleet_herdr_archive.verify(self.runs, self.mid)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(verified["anchored"])
        self.assertEqual(staged["index_sha256"], verified["index_sha256"])
        self.assertEqual(len(self.submitted()), 5)

    def test_anchored_archive_recovers_without_candidate_or_backend_after_verify_failure(self):
        self.archive.valid = False
        with self.assertRaisesRegex(driver.HerdrMissionError, "independent verification"):
            self.run_driver()
        candidate = self.runs / "missions" / self.mid / "candidate"
        candidate.rename(candidate.with_name("candidate-unavailable"))
        self.archive.valid = True
        with mock.patch.object(driver._Driver, "backend", side_effect=AssertionError("no backend")), \
                mock.patch.object(driver._Driver, "check_candidate", side_effect=AssertionError("no candidate")):
            result = self.run_driver()
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["archive"]["valid"])
        self.assertEqual(len(self.submitted()), 5)


class HerdrProtocolRejectionTests(unittest.TestCase):
    """Real backend, driver and CAS; only Herdr transport/output is synthetic."""

    def setUp(self):
        from tests import test_fleet_herdr as transport
        self.transport = transport
        self.Backend = driver.fleet_herdr.HerdrBackend
        self.helper = HerdrMissionTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.runs = self.helper.runs
        self.mid = self.helper.create(key='protocol-rejection', options={
            'herdr_session': 'mission-control-test', 'timeout_seconds': 7200, 'teardown': False,
        })
        self.fake = transport.FakeHerdr()
        self.transcripts = {}
        self.backends = []
        self.stage = 'build'
        self.verdict = 'PASS'
        self.runtime_state = 'working'
        self.mutate = lambda result: result.update(artifacts=[])
        self.transcript_edit = lambda rows: None

        def factory(controller):
            backend = self.Backend(
                self.runs, self.mid, feature='driver-test', target_repo=controller.candidate,
                compiled=controller.compiled, session='mission-control-test',
                environment={'PATH': '/usr/bin:/bin'}, run_command=self.fake,
                transcript_resolver=self.transcripts.get,
            )
            submit = backend.submit

            def observed_submit(run_id, prompt, *, instance_id):
                observed = submit(run_id, prompt, instance_id=instance_id)
                task = json.loads(prompt)
                selected = task['stage'] == self.stage
                member = next(m for m in backend.state()['members'] if m['instance_id'] == instance_id)
                if task['stage'] == 'build':
                    (controller.candidate / 'answer.txt').write_text('implemented\n')
                final = {
                    **{k: task['result_contract'][k] for k in ('schema_version', 'mission_id', 'run_id', 'instance_id', 'candidate_tree_sha')},
                    'status': self.verdict if selected else 'PASS',
                    'summary': 'Evidence for ' + task['stage'],
                    'artifacts': [{'path': 'README.md', 'sha256': state.artifact_id((controller.candidate / 'README.md').read_bytes())}],
                }
                if selected:
                    self.mutate(final)
                fixture = SimpleNamespace(target=controller.candidate, fake=self.fake,
                                          tmp=self.helper.tmp, transcripts=self.transcripts)
                transport.HerdrBackendTests.write_transcript(
                    fixture, agent_session=member['agent_session']['value'], member=member,
                    prompt=prompt, final=final, turn_id='turn-protocol-' + task['stage'],
                )
                if selected:
                    path = self.transcripts[member['agent_session']['value']]
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                    self.transcript_edit(rows)
                    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                self.fake.agent_states[member['agent_name']] = self.runtime_state if selected else 'done'
                return observed

            backend.submit = observed_submit
            self.backends.append(backend)
            return backend

        patcher = mock.patch.object(driver._Driver, 'backend', factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def current(self):
        return fleet_mission.load_state(self.runs, self.mid)

    def admission(self):
        return next(a for a in self.current()['admissions'].values() if a['request_key'] == 'herdr:' + self.stage)

    def drive(self):
        return driver.drive(self.runs, self.mid)

    def operations(self, operation):
        return [c for c in map(self.fake.operation, self.fake.calls) if c[1:3] == ['agent', operation]]

    def assert_one_prompt(self):
        prompts = [c for c in self.operations('prompt') if json.loads(c[-1])['stage'] == self.stage]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(self.operations('send-keys'), [])

    def assert_owned_without_verdict(self):
        self.assertTrue(self.admission()['active'])
        self.assertEqual(bool(self.current()['active_writer']), self.stage == 'build')
        self.assertIsNone(self.admission()['result'])
        self.assertIsNone(self.admission()['terminal'])

    def reject(self, reason='requires 1..100 artifact checks'):
        with self.assertRaisesRegex(driver.RoleProtocolError, reason):
            self.drive()
        self.assert_owned_without_verdict()
        proof = self.drive()['protocol_rejection']
        self.assertEqual(proof['kind'], 'herdr_role_protocol_rejection')
        self.assertNotIn('status', proof)
        run_id = self.admission()['run_id']
        raw = self.backends[-1].collect_result(run_id)
        self.assertEqual(proof['run_id'], run_id)
        self.assertEqual(proof['prompt_sha256'], self.admission()['task_sha256'])
        self.assertEqual(fleet_artifacts.get_bytes(self.runs, self.mid, proof['observed_result_artifact_id']), state.canonical_bytes(raw))
        with mock.patch.object(driver._Driver, 'validate_result', side_effect=AssertionError('reparsed rejection')):
            self.assertEqual(self.drive()['protocol_rejection'], proof)
        self.assert_owned_without_verdict()
        self.assert_one_prompt()
        return proof

    def request_cancel(self, key='reject-cancel'):
        driver.control.request(self.runs, self.mid, action='cancel', reason='fixture rejection cancellation',
                               idempotency_key=key, run_id=self.admission()['run_id'])

    def set_runtime(self, runtime_state):
        member = next(m for m in self.backends[-1].state()['members'] if m['instance_id'] == self.admission()['recipient_instance'])
        self.fake.agent_states[member['agent_name']] = runtime_state

    def test_cached_invalid_role_result_is_durably_rejected_and_cancel_reconciles(self):
        with self.assertRaisesRegex(driver.HerdrMissionError, 'requires 1..100 artifact checks'):
            self.drive()
        admission = self.admission()
        run_id = admission['run_id']
        cached = self.backends[-1].collect_result(run_id)
        before = state.canonical_bytes(cached)
        self.assertEqual(cached['artifacts'], [])
        self.assertEqual(cached['turn_id'], 'turn-protocol-build')
        self.assertTrue(admission['active'])
        self.assertTrue(self.current()['active_writer'])
        self.assertIsNone(admission['result'])
        self.assertIsNone(admission['terminal'])
        try:
            resumed = self.drive()
        except driver.HerdrMissionError as exc:
            self.fail(f'same invalid cached result was parsed again without durable adjudication: {exc}; active_writer={bool(self.current()["active_writer"])}')
        rejection = resumed['protocol_rejection']
        self.assertEqual(rejection['run_id'], run_id)
        proof = state.loads_strict(fleet_artifacts.get_bytes(self.runs, self.mid, rejection['artifact_id']))
        self.assertEqual(fleet_artifacts.get_bytes(self.runs, self.mid, proof['observed_result_artifact_id']), before)
        with mock.patch.object(driver._Driver, 'validate_result', side_effect=AssertionError('reparsed adjudicated result')):
            self.assertEqual(self.drive()['protocol_rejection'], rejection)
            driver.control.request(self.runs, self.mid, action='cancel', reason='fixture cancel',
                                   idempotency_key='invalid-cancel', run_id=run_id)
            self.assertEqual(self.drive()['status'], 'running')
            self.assertTrue(self.admission()['active'])
            self.assertTrue(self.current()['active_writer'])
            self.assertIsNone(self.admission()['result'])
            self.assertEqual(self.operations('send-keys'), [])
            member = next(m for m in self.backends[-1].state()['members'] if m['instance_id'] == 'worker')
            self.fake.agent_states[member['agent_name']] = 'idle'
            self.assertEqual(self.drive()['status'], 'abandoned')
        self.assertFalse(self.admission()['active'])
        self.assertFalse(self.current()['active_writer'])
        self.assertEqual(self.admission()['terminal']['status'], 'abandoned')
        self.assertIsNone(self.admission()['result'])
        self.assertEqual(state.canonical_bytes(self.backends[-1].collect_result(run_id)), before)
        prompts = [c for c in self.operations('prompt') if json.loads(c[-1])['stage'] == self.stage]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(self.operations('send-keys'), [])

    def test_pass_empty_artifacts_in_plan_has_no_role_verdict(self):
        self.stage = 'plan'
        self.reject()

    def test_blocked_empty_artifacts_has_no_role_verdict(self):
        self.verdict = 'BLOCKED'
        self.reject()

    def test_fail_empty_artifacts_has_no_role_verdict(self):
        self.verdict = 'FAIL'
        self.reject()

    def test_malformed_artifact_item_is_rejected(self):
        self.mutate = lambda result: result.update(artifacts=['README.md'])
        self.reject('invalid role artifact contract')

    def test_bad_artifact_hash_is_rejected(self):
        self.mutate = lambda result: result['artifacts'][0].update(sha256='0' * 64)
        self.reject('role artifact digest mismatch')

    def test_missing_artifact_reference_is_rejected(self):
        self.mutate = lambda result: result['artifacts'][0].update(path='missing.txt')
        self.reject('role artifact is unavailable or unsafe')

    def test_unsafe_artifact_reference_is_rejected(self):
        self.mutate = lambda result: result['artifacts'][0].update(path='../README.md')
        self.reject('safe candidate-relative path')

    def test_nul_artifact_path_is_rejected_before_filesystem_lookup(self):
        self.mutate = lambda result: result['artifacts'][0].update(path='README.md\x00')
        self.reject('safe candidate-relative path')

    def test_nonlist_artifacts_are_observed_then_rejected(self):
        self.mutate = lambda result: result.update(artifacts=None)
        self.reject()

    def test_malformed_status_with_bound_execution_is_rejected(self):
        self.mutate = lambda result: result.update(status=['PASS'])
        self.reject('requires PASS/BLOCKED/FAIL')

    def test_empty_summary_with_bound_execution_is_rejected(self):
        self.mutate = lambda result: result.update(summary='')
        self.reject('requires PASS/BLOCKED/FAIL')

    def assert_unresolved(self):
        for _ in range(2):
            self.assertEqual(self.drive()['status'], 'running')
            self.assert_owned_without_verdict()
        run_id = self.admission()['run_id']
        self.assertIsNone(driver.fleet_herdr.load_result_rejection(self.runs, self.mid, run_id))
        self.assertFalse((self.runs / 'missions' / self.mid / 'herdr-results' / f'{run_id}.json').exists())
        self.assert_one_prompt()

    def test_wrong_run_identity_remains_unresolved(self):
        self.mutate = lambda result: result.update(run_id=str(uuid.uuid4()), artifacts=[])
        self.assert_unresolved()

    def test_incomplete_transcript_remains_unresolved_then_same_result_can_be_adjudicated(self):
        self.saved_complete = None
        def incomplete(rows):
            self.saved_complete = rows.pop()
        self.transcript_edit = incomplete
        self.assert_unresolved()
        member = next(m for m in self.backends[-1].state()['members'] if m['instance_id'] == 'worker')
        with self.transcripts[member['agent_session']['value']].open('a') as stream:
            stream.write(json.dumps(self.saved_complete) + '\n')
        self.reject()

    def test_unverifiable_permissions_do_not_adjudicate_invalid_result(self):
        def unverifiable(rows):
            for row in rows:
                if row['type'] == 'turn_context':
                    row['payload'].pop('sandbox_policy', None)
        self.transcript_edit = unverifiable
        self.assert_unresolved()

    def test_unbound_non_json_output_remains_unresolved(self):
        def malformed(rows):
            rows[-2]['payload']['content'][0]['text'] = 'not a bound role result'
            rows[-1]['payload']['last_agent_message'] = 'not a bound role result'
        self.transcript_edit = malformed
        self.assert_unresolved()

    def assert_valid_verdict(self, verdict, expected):
        self.verdict = verdict
        self.mutate = lambda result: None
        result = self.drive()
        self.assertEqual(result['status'], expected)
        self.assertNotIn('protocol_rejection', result)
        self.assertFalse(self.admission()['active'])
        self.assertFalse(self.current()['active_writer'])
        self.assertEqual(self.admission()['terminal']['status'], expected)
        self.assertIsNotNone(self.admission()['result'])
        run_id = self.admission()['run_id']
        self.assertIsNone(driver.fleet_herdr.load_result_rejection(self.runs, self.mid, run_id))
        calls = list(self.fake.calls)
        self.assertEqual(self.drive()['status'], expected)
        self.assertEqual(self.fake.calls, calls)
        self.assert_one_prompt()

    def test_valid_pass_with_artifacts_keeps_success_path(self):
        self.assert_valid_verdict('PASS', 'succeeded')

    def test_valid_blocked_with_artifacts_keeps_blocked_path(self):
        self.assert_valid_verdict('BLOCKED', 'blocked')

    def test_valid_fail_with_artifacts_keeps_failed_path(self):
        self.assert_valid_verdict('FAIL', 'failed')

    def test_restart_after_observation_before_adjudication(self):
        collect = self.Backend.collect_result
        def crash(backend, run_id):
            raw = collect(backend, run_id)
            if raw and raw['instance_id'] == 'worker':
                raise RuntimeError('crash after cached observation')
            return raw
        with mock.patch.object(self.Backend, 'collect_result', crash):
            with self.assertRaisesRegex(RuntimeError, 'crash after cached observation'):
                self.drive()
        run_id = self.admission()['run_id']
        before = state.canonical_bytes(self.backends[-1].collect_result(run_id))
        self.assertIsNone(driver.fleet_herdr.load_result_rejection(self.runs, self.mid, run_id))
        self.request_cancel()
        self.reject()
        self.assertEqual(state.canonical_bytes(self.backends[-1].collect_result(run_id)), before)

    def interrupted_rejection_write(self, *, after):
        write = driver._Driver.write
        def crash(controller, name, value):
            if name.startswith('herdr-result-rejection-'):
                self.attempted_proof = value['artifact_id']
                if after:
                    write(controller, name, value)
                raise RuntimeError('interrupted rejection publication')
            write(controller, name, value)
        with mock.patch.object(driver._Driver, 'write', crash):
            with self.assertRaisesRegex(RuntimeError, 'interrupted rejection publication'):
                self.drive()
        self.assert_owned_without_verdict()
        return self.attempted_proof

    def test_restart_before_rejection_pointer_recovers_same_cas_identity(self):
        attempted = self.interrupted_rejection_write(after=False)
        self.assertIsNone(driver.fleet_herdr.load_result_rejection(self.runs, self.mid, self.admission()['run_id']))
        self.assertEqual(self.reject()['artifact_id'], attempted)

    def test_restart_after_rejection_pointer_skips_protocol_parser(self):
        attempted = self.interrupted_rejection_write(after=True)
        with mock.patch.object(driver._Driver, 'validate_result', side_effect=AssertionError('reparsed after restart')):
            self.assertEqual(self.drive()['protocol_rejection']['artifact_id'], attempted)
        self.assert_owned_without_verdict()
        self.assert_one_prompt()

    def test_fresh_process_recovers_rejection_from_cas_without_runtime_or_parser(self):
        proof = self.reject()
        code = '''
import json, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
import fleet_herdr, fleet_herdr_mission
with mock.patch.object(fleet_herdr.HerdrBackend, '_default_run', side_effect=AssertionError('runtime invoked')), \\
     mock.patch.object(fleet_herdr.HerdrBackend, '_default_transcript', side_effect=AssertionError('live transcript read')), \\
     mock.patch.object(fleet_herdr_mission._Driver, 'validate_result', side_effect=AssertionError('reparsed rejection')):
    print(json.dumps(fleet_herdr_mission.drive(Path(sys.argv[2]), sys.argv[3])))
'''
        child = subprocess.run([sys.executable, '-B', '-c', code, str(ROOT / 'scripts'), str(self.runs), self.mid],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout)['protocol_rejection'], proof)
        self.assert_owned_without_verdict()
        self.assert_one_prompt()

    def test_repeated_pause_resume_and_deadline_use_durable_rejection(self):
        proof = self.reject()
        with mock.patch.object(driver._Driver, 'validate_result', side_effect=AssertionError('reparsed control result')):
            for action in ('pause', 'resume', 'pause', 'resume'):
                driver.control.request(self.runs, self.mid, action=action, reason='fixture ' + action,
                    idempotency_key=action + str(len(self.current()['herdr_control']['requests'])))
                self.assertEqual(self.drive()['protocol_rejection'], proof)
                self.assert_owned_without_verdict()
            with mock.patch.object(driver._Driver, 'remaining_seconds', return_value=-1):
                for _ in range(2):
                    self.assertEqual(self.drive()['status'], 'running')
                    self.assert_owned_without_verdict()
                self.set_runtime('idle')
                self.assertEqual(self.drive()['status'], 'abandoned')
        self.assertFalse(self.admission()['active'])
        self.assertIsNone(self.admission()['result'])
        self.assert_one_prompt()

    def test_restart_before_cancel_and_after_closure_before_admission_finalization(self):
        proof = self.reject()
        self.request_cancel()
        with mock.patch.object(self.Backend, 'cancel', side_effect=RuntimeError('crash before reconciliation')):
            with self.assertRaisesRegex(RuntimeError, 'crash before reconciliation'):
                self.drive()
        self.assertEqual(self.drive()['protocol_rejection'], proof)
        self.assert_owned_without_verdict()
        self.set_runtime('idle')
        with mock.patch.object(fleet_admission, 'finalize', side_effect=RuntimeError('crash before admission finalization')):
            with self.assertRaisesRegex(RuntimeError, 'crash before admission finalization'):
                self.drive()
        self.assert_owned_without_verdict()
        self.request_cancel()
        self.assertEqual(self.drive()['status'], 'abandoned')
        self.assertEqual(self.drive()['status'], 'abandoned')
        self.assertIsNone(self.admission()['result'])
        self.assertFalse(self.admission()['active'])
        self.assert_one_prompt()

    def test_rejection_does_not_expand_cancellation_generation_authority(self):
        self.reject()
        calls = list(self.fake.calls)
        with self.assertRaisesRegex(state.MissionConflict, 'generation does not match owned backend'):
            driver.control.request(self.runs, self.mid, action='cancel', reason='wrong generation',
                idempotency_key='wrong-generation', run_id=self.admission()['run_id'], generation=str(uuid.uuid4()))
        self.assertEqual(self.fake.calls, calls)
        self.assert_owned_without_verdict()
        self.assert_one_prompt()

    def test_rejection_cannot_authorize_different_cached_bytes(self):
        proof = self.reject()
        self.request_cancel()
        raw = self.backends[-1].collect_result(proof['run_id'])
        changed = {**raw, 'turn_id': 'another-turn'}
        calls = list(self.fake.calls)
        with mock.patch.object(self.Backend, 'collect_result', return_value=changed):
            with self.assertRaisesRegex(driver.fleet_herdr.HerdrBackendError, 'rejection differs from observed result'):
                self.drive()
        self.assertEqual(self.fake.calls, calls)
        self.assert_owned_without_verdict()
        self.assertEqual(self.drive()['protocol_rejection'], proof)
        self.assert_one_prompt()

    def test_rejection_is_optional_for_historical_readers_and_does_not_add_ledger_events(self):
        from tests.test_mission_run import mission_run
        import fleet_report
        proof = self.reject()
        root = self.runs / 'missions' / self.mid
        before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        with mock.patch('subprocess.run', side_effect=AssertionError('historical reader used runtime')):
            self.assertEqual(mission_run.mission_status(self.runs, self.mid)['status'], 'running')
            fleet_report.build_report(self.runs, self.mid)
            driver.control.backend_generation(self.runs, self.mid)
        self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()})
        self.assertEqual(list(root.glob('herdr-result-rejection-*.json')), [root / f'herdr-result-rejection-{proof["run_id"]}.json'])

    def test_capsule_rejected_result_still_requires_exact_quiescence_and_cleanup(self):
        import fleet_mission_capsule as capsule
        self.reject()
        run_id = self.admission()['run_id']
        raw = self.backends[-1].collect_result(run_id)
        attempt = {'role': 'worker', 'generation': self.backends[-1].state()['generation']}
        launch = {'attempt': attempt, 'prompt_sha256': self.admission()['task_sha256']}
        launch_id = fleet_artifacts.put_bytes(self.runs, self.mid, state.canonical_bytes(launch))['artifact_id']
        adapter = SimpleNamespace(runs=self.runs, mid=self.mid, collect_result=lambda run: raw,
                                  _name=lambda run, kind: kind)
        plan_run = next(a['run_id'] for a in self.current()['admissions'].values() if a['request_key'] == 'herdr:plan')
        plan_result = self.backends[-1].collect_result(plan_run)
        with mock.patch.object(adapter, 'collect_result', return_value=plan_result), \
                mock.patch.object(capsule, 'read_json', side_effect=AssertionError('valid result entered cancel reconciliation')):
            self.assertEqual(capsule.CapsuleBackend.cancel(adapter, plan_run), {'status': 'settled', 'run_id': plan_run})
        for quiescent, cleanup, exact in ((False, True, True), (True, False, True), (True, True, False), (True, True, True)):
            with self.subTest(quiescent=quiescent, cleanup=cleanup, exact=exact):
                report = {'run_id': run_id if exact else str(uuid.uuid4()), 'attempt': attempt,
                          'quiescence_confirmed': quiescent, 'cleanup_confirmed': cleanup}
                report_id = fleet_artifacts.put_bytes(self.runs, self.mid, state.canonical_bytes(report))['artifact_id']
                with mock.patch.object(capsule, 'read_json', return_value={'launch_artifact_id': launch_id, 'report_artifact_id': report_id}):
                    if not all((quiescent, cleanup, exact)):
                        with self.assertRaisesRegex(RuntimeError, 'capsule cancellation lacks quiescence'):
                            capsule.CapsuleBackend.cancel(adapter, run_id)
                    else:
                        observed = capsule.CapsuleBackend.cancel(adapter, run_id)
                        self.assertEqual(observed['status'], 'abandoned')
                        self.assertEqual(observed['run_id'], run_id)
                        self.assertEqual(observed['prompt_sha256'], self.admission()['task_sha256'])
        self.assert_owned_without_verdict()  # the adapter receipt alone never writes the admission
        self.assert_one_prompt()


if __name__ == "__main__":
    unittest.main()
