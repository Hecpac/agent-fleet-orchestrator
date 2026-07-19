from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import multiprocessing
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
admission = importlib.import_module("fleet_admission")
state = importlib.import_module("fleet_mission_state")


WORKFLOW_DIGEST = "a" * 64
COMPILED_DIGEST = "b" * 64


def _reserve_worker(
    runs_dir: str,
    mission_id: str,
    request: dict[str, object],
    idempotency_key: str,
    start: object,
    results: object,
) -> None:
    """Race one reservation in a fresh interpreter process."""

    start.wait()
    try:
        reserved = admission.reserve_many(
            Path(runs_dir),
            mission_id,
            requests=[request],
            idempotency_key=idempotency_key,
        )
    except Exception as exc:  # noqa: BLE001 - the parent asserts the durable result.
        results.put(("error", type(exc).__name__, str(exc)))
    else:
        results.put(("ok", reserved["admissions"][0]["admission_id"], ""))


def _commit_authorize_worker(
    runs_dir: str,
    mission_id: str,
    item: dict[str, object],
    committed: object,
    results: object,
) -> None:
    """Commit two admissions before either requests launch authorization."""

    try:
        commit = admission.commit(
            Path(runs_dir),
            mission_id,
            admission_id=item["admission_id"],
            request_digest=item["request_digest"],
            effect_sha256=item["effect_sha256"],
            recipient_instance=item["recipient_instance"],
            writer=item["writer"],
            run_id=item["run_id"],
            idempotency_key=f"process:commit:{item['request_key']}",
        )
        committed.wait(timeout=20)
        authorized = admission.authorize_launch(
            Path(runs_dir),
            mission_id,
            admission_id=item["admission_id"],
            commit_event_sha256=commit["commit_event_sha256"],
            request_digest=item["request_digest"],
            effect_sha256=item["effect_sha256"],
            recipient_instance=item["recipient_instance"],
            writer=item["writer"],
            run_id=item["run_id"],
            idempotency_key=f"process:authorize:{item['request_key']}",
        )
    except Exception as exc:  # noqa: BLE001 - parent asserts exact outcomes.
        results.put(("error", type(exc).__name__, str(exc)))
    else:
        results.put(("ok", authorized["authorization_event_sha256"], ""))


class FleetAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs = self.tmp / "runs"
        self.runs.mkdir(mode=0o700)
        self.target = self.tmp / "target"
        self.target.mkdir()
        self.mission_id = self._new_mission()

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    def _new_mission(
        self,
        *,
        credits: int = 20,
        max_active: int = 10,
        deadline_at: str = "2099-01-01T00:00:00.000000Z",
        bootstrap_lead: bool = True,
        start_running: bool = True,
    ) -> str:
        mission_id = str(uuid.uuid4())
        state.append_events(
            self.runs,
            mission_id,
            [
                {
                    "kind": "mission_created",
                    "actor": "CONTROL",
                    "idempotency_key": "mission:create",
                    "payload": {
                        "feature": f"admission-{mission_id[:8]}",
                        "objective_sha256": "c" * 64,
                        "target_repo": str(self.target.resolve()),
                        "base_sha": "d" * 40,
                        "workflow_digest": WORKFLOW_DIGEST,
                        "initial_risk": "low",
                    },
                },
                {
                    "kind": "workflow_compiled",
                    "actor": "CONTROL",
                    "idempotency_key": "workflow:compiled",
                    "payload": {"compiled_digest": COMPILED_DIGEST},
                },
            ],
        )
        admission.freeze_policy(
            self.runs,
            mission_id,
            workflow_digest=WORKFLOW_DIGEST,
            compiled_digest=COMPILED_DIGEST,
            deadline_at=deadline_at,
            delegation_credits=credits,
            max_active_delegations=max_active,
        )
        if start_running:
            state.append_events(
                self.runs,
                mission_id,
                [
                    {
                        "kind": "fleet_boot_started",
                        "actor": "CONTROL",
                        "idempotency_key": "fleet:boot",
                        "payload": {"feature": f"admission-{mission_id[:8]}"},
                    },
                    {
                        "kind": "mission_running",
                        "actor": "CONTROL",
                        "idempotency_key": "fleet:running",
                        "payload": {"manifest": str(self.runs / "fleet.manifest")},
                    },
                ],
            )
        if bootstrap_lead:
            reserved = admission.reserve_many(
                self.runs,
                mission_id,
                requests=[self._request("lead", run_kind="lead", recipient="lead")],
                idempotency_key="reserve:lead",
            )["admissions"][0]
            committed = admission.commit(
                self.runs,
                mission_id,
                admission_id=reserved["admission_id"],
                request_digest=reserved["request_digest"],
                effect_sha256=reserved["effect_sha256"],
                recipient_instance=reserved["recipient_instance"],
                writer=reserved["writer"],
                run_id=reserved["run_id"],
                idempotency_key="commit:lead",
            )
            authorized = admission.authorize_launch(
                self.runs,
                mission_id,
                admission_id=reserved["admission_id"],
                commit_event_sha256=committed["commit_event_sha256"],
                request_digest=reserved["request_digest"],
                effect_sha256=reserved["effect_sha256"],
                recipient_instance=reserved["recipient_instance"],
                writer=reserved["writer"],
                run_id=reserved["run_id"],
                idempotency_key="authorize:lead",
            )
            admission.mark_started(
                self.runs,
                mission_id,
                admission_id=reserved["admission_id"],
                authorization_event_sha256=authorized["authorization_event_sha256"],
                request_digest=reserved["request_digest"],
                effect_sha256=reserved["effect_sha256"],
                recipient_instance=reserved["recipient_instance"],
                writer=reserved["writer"],
                run_id=reserved["run_id"],
                idempotency_key="start:lead",
            )
        return mission_id

    @staticmethod
    def _request(
        request_key: str,
        *,
        recipient: str | None = None,
        run_kind: str = "specialist",
        delegated_budget: int = 0,
        writer: bool = False,
        parent_admission_id: str | None = None,
        parent_run_id: str | None = None,
        effect_sha256: str | None = None,
        task_sha256: str | None = None,
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "request_key": request_key,
            "run_kind": run_kind,
            "recipient_instance": recipient or f"worker-{request_key}",
            "capability": "implementation" if run_kind == "specialist" else "lead",
            "effect_sha256": effect_sha256
            or state.sha256(
                {
                    "request_key": request_key,
                    "recipient": recipient or f"worker-{request_key}",
                    "run_kind": run_kind,
                }
            ),
            "task_sha256": task_sha256
            or state.sha256(
                {
                    "task": request_key,
                    "recipient": recipient or f"worker-{request_key}",
                    "capability": "implementation"
                    if run_kind == "specialist"
                    else "lead",
                }
            ),
            "delegated_budget": delegated_budget,
            "writer": writer,
        }
        if parent_admission_id is not None:
            request["parent_admission_id"] = parent_admission_id
        if parent_run_id is not None:
            request["parent_run_id"] = parent_run_id
        return request

    def _current(self, mission_id: str | None = None) -> dict[str, object]:
        selected = mission_id or self.mission_id
        events = state.read_events(
            state.ledger_path(self.runs, selected),
            expected_mission_id=selected,
        )
        return state.derive_state(events)

    def _reserve_one(
        self,
        request_key: str,
        *,
        mission_id: str | None = None,
        idempotency_key: str | None = None,
        **request_overrides: object,
    ) -> dict[str, object]:
        selected = mission_id or self.mission_id
        reserved = admission.reserve_many(
            self.runs,
            selected,
            requests=[self._request(request_key, **request_overrides)],
            idempotency_key=idempotency_key or f"reserve:{request_key}",
        )
        return reserved["admissions"][0]

    def _commit_one(
        self,
        item: dict[str, object],
        *,
        mission_id: str | None = None,
        idempotency_key: str | None = None,
        **overrides: object,
    ) -> dict[str, object]:
        selected = mission_id or self.mission_id
        bindings: dict[str, object] = {
            "admission_id": item["admission_id"],
            "request_digest": item["request_digest"],
            "effect_sha256": item["effect_sha256"],
            "recipient_instance": item["recipient_instance"],
            "writer": item["writer"],
            "run_id": item["run_id"],
            "idempotency_key": idempotency_key or f"commit:{item['request_key']}",
        }
        bindings.update(overrides)
        return admission.commit(self.runs, selected, **bindings)

    def _authorize_one(
        self,
        item: dict[str, object],
        committed: dict[str, object],
        *,
        mission_id: str | None = None,
        idempotency_key: str | None = None,
        **overrides: object,
    ) -> dict[str, object]:
        selected = mission_id or self.mission_id
        bindings: dict[str, object] = {
            "admission_id": item["admission_id"],
            "commit_event_sha256": committed["commit_event_sha256"],
            "request_digest": item["request_digest"],
            "effect_sha256": item["effect_sha256"],
            "recipient_instance": item["recipient_instance"],
            "writer": item["writer"],
            "run_id": item["run_id"],
            "idempotency_key": idempotency_key or f"authorize:{item['request_key']}",
        }
        bindings.update(overrides)
        return admission.authorize_launch(self.runs, selected, **bindings)

    def _start_one(
        self,
        item: dict[str, object],
        authorized: dict[str, object],
        *,
        mission_id: str | None = None,
        idempotency_key: str | None = None,
        **overrides: object,
    ) -> dict[str, object]:
        selected = mission_id or self.mission_id
        bindings: dict[str, object] = {
            "admission_id": item["admission_id"],
            "authorization_event_sha256": authorized["authorization_event_sha256"],
            "request_digest": item["request_digest"],
            "effect_sha256": item["effect_sha256"],
            "recipient_instance": item["recipient_instance"],
            "writer": item["writer"],
            "run_id": item["run_id"],
            "idempotency_key": idempotency_key or f"start:{item['request_key']}",
        }
        bindings.update(overrides)
        return admission.mark_started(self.runs, selected, **bindings)

    def _finalize_one(
        self,
        item: dict[str, object],
        *,
        status: str,
        reason: str,
        idempotency_key: str,
        mission_id: str | None = None,
        source_event_sha256: str = "f" * 64,
        terminal_evidence: dict[str, object] | None = None,
        **overrides: object,
    ) -> dict[str, object]:
        selected = mission_id or self.mission_id
        evidence = terminal_evidence or {
            "schema_version": 1,
            "source_event_sha256": source_event_sha256,
            "run_id": item["run_id"],
            "task_sha256": item["task_sha256"],
            "status": status,
        }
        bindings: dict[str, object] = {
            "admission_id": item["admission_id"],
            "recipient_instance": item["recipient_instance"],
            "writer": item["writer"],
            "terminal_evidence": evidence,
            "reason": reason,
            "idempotency_key": idempotency_key,
        }
        bindings.update(overrides)
        return admission.finalize(self.runs, selected, **bindings)

    def _abort_one(
        self,
        item: dict[str, object],
        *,
        reason: str,
        idempotency_key: str,
        mission_id: str | None = None,
        **overrides: object,
    ) -> dict[str, object]:
        selected = mission_id or self.mission_id
        bindings: dict[str, object] = {
            "admission_id": item["admission_id"],
            "run_id": item["run_id"],
            "request_digest": item["request_digest"],
            "effect_sha256": item["effect_sha256"],
            "task_sha256": item["task_sha256"],
            "recipient_instance": item["recipient_instance"],
            "writer": item["writer"],
            "reason": reason,
            "idempotency_key": idempotency_key,
        }
        bindings.update(overrides)
        return admission.abort_prelaunch(self.runs, selected, **bindings)

    def _assert_one_race_winner(
        self,
        mission_id: str,
        requests: tuple[dict[str, object], dict[str, object]],
    ) -> list[tuple[str, str, str]]:
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_reserve_worker,
                args=(
                    str(self.runs),
                    mission_id,
                    request,
                    f"race:{index}",
                    start,
                    results,
                ),
            )
            for index, request in enumerate(requests)
        ]
        try:
            for process in processes:
                process.start()
            start.set()
            rows = [results.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(timeout=20)
                self.assertFalse(process.is_alive(), "reservation race process hung")
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            results.close()
            results.join_thread()
        self.assertEqual([row[0] for row in rows].count("ok"), 1, rows)
        self.assertEqual([row[0] for row in rows].count("error"), 1, rows)
        self.assertEqual(
            next(row[1] for row in rows if row[0] == "error"), "MissionConflict"
        )
        return rows

    def test_tree_batch_debits_global_pool_once_and_children_only_debit_parent(
        self,
    ) -> None:
        mission_id = self._new_mission(bootstrap_lead=False)
        lead_ids = admission.deterministic_ids(
            mission_id, request_key="lead", run_kind="lead"
        )
        root_ids = admission.deterministic_ids(
            mission_id, request_key="root", run_kind="specialist"
        )
        requests = [
            self._request("lead", run_kind="lead", recipient="lead"),
            self._request(
                "root",
                recipient="root",
                delegated_budget=4,
                parent_admission_id=str(lead_ids["admission_id"]),
            ),
            self._request(
                "child-a",
                recipient="child-a",
                delegated_budget=1,
                parent_admission_id=str(root_ids["admission_id"]),
            ),
            self._request(
                "child-b",
                recipient="child-b",
                parent_admission_id=str(root_ids["admission_id"]),
            ),
        ]

        reserved = admission.reserve_many(
            self.runs,
            mission_id,
            requests=requests,
            idempotency_key="reserve:tree",
        )

        by_key = {item["request_key"]: item for item in reserved["admissions"]}
        self.assertEqual(by_key["lead"]["credit_cost"], 0)
        self.assertEqual(by_key["lead"]["global_credit_debit"], 0)
        self.assertEqual(by_key["root"]["credit_cost"], 5)
        self.assertEqual(by_key["root"]["global_credit_debit"], 5)
        self.assertEqual(by_key["root"]["parent_credit_debit"], 0)
        self.assertEqual(by_key["child-a"]["credit_cost"], 2)
        self.assertEqual(by_key["child-a"]["global_credit_debit"], 0)
        self.assertEqual(by_key["child-a"]["parent_credit_debit"], 2)
        self.assertEqual(by_key["child-b"]["credit_cost"], 1)
        self.assertEqual(by_key["child-b"]["global_credit_debit"], 0)
        self.assertEqual(by_key["child-b"]["parent_credit_debit"], 1)
        current = self._current(mission_id)
        self.assertEqual(current["delegation_credits_spent"], 5)
        self.assertEqual(current["delegation_credits_remaining"], 15)
        self.assertEqual(current["active_delegations"], 3)
        self.assertEqual(
            current["admissions"][root_ids["admission_id"]]["child_credits_spent"],
            3,
        )

    def test_rejected_batch_is_atomic(self) -> None:
        mission_id = self._new_mission(credits=1)
        ledger = state.ledger_path(self.runs, mission_id)
        before = ledger.read_bytes()

        with self.assertRaisesRegex(state.MissionConflict, "credits"):
            admission.reserve_many(
                self.runs,
                mission_id,
                requests=[
                    self._request("atomic-a", recipient="atomic-a"),
                    self._request("atomic-b", recipient="atomic-b"),
                ],
                idempotency_key="reserve:atomic",
            )

        self.assertEqual(ledger.read_bytes(), before)
        current = self._current(mission_id)
        self.assertFalse(
            any(
                item["run_kind"] == "specialist"
                for item in current["admissions"].values()
            )
        )
        self.assertEqual(current["delegation_credits_spent"], 0)

    def test_uuid5_identity_and_reservation_idempotency(self) -> None:
        namespace = uuid.UUID(self.mission_id)
        expected = {
            "admission_id": str(uuid.uuid5(namespace, "admission:specialist:stable")),
            "run_id": str(uuid.uuid5(namespace, "run:specialist:stable")),
            "delegation_id": str(uuid.uuid5(namespace, "delegation:stable")),
        }
        self.assertEqual(
            admission.deterministic_ids(
                self.mission_id, request_key="stable", run_kind="specialist"
            ),
            expected,
        )
        request = self._request("stable", recipient="stable-worker", delegated_budget=2)
        first = admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=[request],
            idempotency_key="reserve:stable",
        )
        before = state.ledger_path(self.runs, self.mission_id).read_bytes()

        repeated = admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=[request],
            idempotency_key="reserve:stable",
        )

        self.assertTrue(first["appended"])
        self.assertFalse(repeated["appended"])
        self.assertEqual(repeated["event"], first["event"])
        self.assertEqual(repeated["admissions"], first["admissions"])
        self.assertEqual(
            state.ledger_path(self.runs, self.mission_id).read_bytes(), before
        )
        with self.assertRaises(state.MissionConflict):
            admission.reserve_many(
                self.runs,
                self.mission_id,
                requests=[self._request("changed", recipient="changed-worker")],
                idempotency_key="reserve:stable",
            )
        self.assertEqual(
            state.ledger_path(self.runs, self.mission_id).read_bytes(), before
        )

    def test_reservation_replay_does_not_recalculate_parent_after_lead_appears(
        self,
    ) -> None:
        mission_id = self._new_mission(bootstrap_lead=False)
        request = self._request("root-before-lead", recipient="root-before-lead")
        first = admission.reserve_many(
            self.runs,
            mission_id,
            requests=[request],
            idempotency_key="reserve:root-before-lead",
        )
        self.assertIsNone(first["admissions"][0]["parent_admission_id"])

        lead = self._reserve_one(
            "late-lead",
            mission_id=mission_id,
            recipient="lead",
            run_kind="lead",
        )
        lead_commit = self._commit_one(lead, mission_id=mission_id)
        lead_authorized = self._authorize_one(lead, lead_commit, mission_id=mission_id)
        self._start_one(lead, lead_authorized, mission_id=mission_id)
        before = state.ledger_path(self.runs, mission_id).read_bytes()

        replayed = admission.reserve_many(
            self.runs,
            mission_id,
            requests=[request],
            idempotency_key="reserve:root-before-lead",
        )
        self.assertFalse(replayed["appended"])
        self.assertEqual(replayed["event"], first["event"])
        self.assertIsNone(replayed["admissions"][0]["parent_admission_id"])
        self.assertEqual(state.ledger_path(self.runs, mission_id).read_bytes(), before)

        with self.assertRaisesRegex(state.MissionConflict, "another admission request"):
            admission.reserve_many(
                self.runs,
                mission_id,
                requests=[
                    self._request(
                        "root-before-lead",
                        recipient="root-before-lead",
                        effect_sha256="0" * 64,
                    )
                ],
                idempotency_key="reserve:root-before-lead",
            )
        self.assertEqual(state.ledger_path(self.runs, mission_id).read_bytes(), before)

    def test_commit_and_verify_require_every_exact_binding_and_current_head(
        self,
    ) -> None:
        item = self._reserve_one("binding", recipient="binding-worker", writer=True)
        ledger = state.ledger_path(self.runs, self.mission_id)
        reserved_bytes = ledger.read_bytes()
        wrong_bindings = (
            {"request_digest": "0" * 64},
            {"effect_sha256": "0" * 64},
            {"recipient_instance": "other-worker"},
            {"writer": False},
            {"run_id": str(uuid.uuid4())},
        )
        for index, override in enumerate(wrong_bindings):
            with self.subTest(commit_override=override):
                with self.assertRaisesRegex(state.MissionConflict, "binding"):
                    self._commit_one(
                        item,
                        idempotency_key=f"commit:bad:{index}",
                        **override,
                    )
                self.assertEqual(ledger.read_bytes(), reserved_bytes)

        committed = self._commit_one(item)
        proof_kwargs = {
            "admission_id": item["admission_id"],
            "commit_event_sha256": committed["commit_event_sha256"],
            "expected_head_sha256": committed["head_sha256"],
            "request_digest": item["request_digest"],
            "effect_sha256": item["effect_sha256"],
            "recipient_instance": item["recipient_instance"],
            "writer": item["writer"],
            "run_id": item["run_id"],
        }
        proof = admission.verify_commit(self.runs, self.mission_id, **proof_kwargs)
        self.assertEqual(
            proof["commit_event_sha256"], committed["event"]["event_sha256"]
        )
        for override in wrong_bindings:
            with self.subTest(verify_override=override):
                with self.assertRaisesRegex(state.MissionConflict, "bindings"):
                    admission.verify_commit(
                        self.runs,
                        self.mission_id,
                        **{**proof_kwargs, **override},
                    )

        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_assessed",
            actor="CONTROL",
            idempotency_key="risk:after-commit",
            payload={
                "level": "low",
                "categories": [],
                "workflow_minimum": "low",
                "override": "auto",
                "requires_confirmation": False,
            },
        )
        current_head = state.read_events(
            state.ledger_path(self.runs, self.mission_id),
            expected_mission_id=self.mission_id,
        )[-1]["event_sha256"]
        refreshed = admission.verify_commit(
            self.runs,
            self.mission_id,
            **{**proof_kwargs, "expected_head_sha256": current_head},
        )
        self.assertEqual(refreshed["head_sha256"], current_head)

    def test_admission_transitions_are_monotonic_and_release_active_claims(
        self,
    ) -> None:
        item = self._reserve_one("lifecycle", recipient="lifecycle", writer=True)
        self.assertEqual(
            self._current()["admissions"][item["admission_id"]]["phase"], "reserved"
        )
        with self.assertRaisesRegex(state.MissionConflict, "authorized"):
            self._start_one(
                item,
                {"authorization_event_sha256": "0" * 64},
                idempotency_key="start:too-early",
            )

        committed = self._commit_one(item)
        current = self._current()
        self.assertEqual(
            current["admissions"][item["admission_id"]]["phase"], "committed"
        )
        self.assertIn(item["run_id"], current["run_owners"])
        authorized = self._authorize_one(item, committed)
        self.assertEqual(
            self._current()["admissions"][item["admission_id"]]["phase"],
            "authorized",
        )
        started = self._start_one(item, authorized, idempotency_key="start:lifecycle")
        repeated_start = self._start_one(
            item, authorized, idempotency_key="start:lifecycle"
        )
        self.assertFalse(repeated_start["appended"])
        self.assertEqual(repeated_start["event"], started["event"])
        self.assertEqual(
            self._current()["admissions"][item["admission_id"]]["phase"], "started"
        )

        self._finalize_one(
            item,
            status="succeeded",
            reason="complete",
            idempotency_key="finalize:lifecycle",
        )
        current = self._current()
        self.assertEqual(
            current["admissions"][item["admission_id"]]["phase"], "finalized"
        )
        self.assertNotIn("lifecycle", current["active_recipients"])
        self.assertIsNone(current["active_writer"])
        self.assertIn(item["run_id"], current["run_owners"])

        abortable = self._reserve_one("abortable", recipient="abortable", writer=True)
        self._abort_one(
            abortable,
            reason="dispatch was not attempted",
            idempotency_key="abort:abortable",
        )
        current = self._current()
        self.assertEqual(
            current["admissions"][abortable["admission_id"]]["phase"], "aborted"
        )
        with self.assertRaisesRegex(state.MissionConflict, "reserved"):
            self._commit_one(abortable, idempotency_key="commit:after-abort")

        committed_abortable = self._reserve_one(
            "committed-abortable", recipient="committed-abortable"
        )
        self._commit_one(committed_abortable)
        before_abort = state.ledger_path(self.runs, self.mission_id).read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "abort binding"):
            self._abort_one(
                committed_abortable,
                task_sha256="0" * 64,
                reason="crossed task",
                idempotency_key="abort:committed-abortable:wrong-task",
            )
        self.assertEqual(
            state.ledger_path(self.runs, self.mission_id).read_bytes(), before_abort
        )
        self._abort_one(
            committed_abortable,
            reason="approval expired before launch",
            idempotency_key="abort:committed-abortable",
        )
        current = self._current()
        self.assertEqual(
            current["admissions"][committed_abortable["admission_id"]]["phase"],
            "aborted",
        )
        self.assertNotIn(committed_abortable["run_id"], current["run_owners"])

        timestamps = [
            state.parse_timestamp(event["timestamp"], "test event timestamp")
            for event in state.read_events(
                state.ledger_path(self.runs, self.mission_id),
                expected_mission_id=self.mission_id,
            )
        ]
        self.assertTrue(
            all(left < right for left, right in zip(timestamps, timestamps[1:]))
        )

    def test_frozen_policy_blocks_legacy_bypass_and_terminal_with_active_admission(
        self,
    ) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "committed admission"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="lead_dispatched",
                actor="CONTROL",
                idempotency_key="legacy:lead:bypass",
                payload={"run_id": str(uuid.uuid4()), "prompt_sha256": "1" * 64},
            )
        with self.assertRaisesRegex(state.MissionConflict, "committed admission"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="delegation_registered",
                actor="CONTROL",
                idempotency_key="legacy:specialist:bypass",
                payload={
                    "delegation_id": str(uuid.uuid4()),
                    "mission_id": self.mission_id,
                    "run_id": str(uuid.uuid4()),
                    "parent_run_id": None,
                    "delegated_by": "lead",
                    "recipient_instance": "legacy-worker",
                    "capability": "implementation",
                    "objective_sha256": "2" * 64,
                    "input_artifact_ids": [],
                    "expected_output_contract": {"type": "text"},
                    "deadline": "2099-01-01T00:00:00Z",
                    "provider": "openai",
                    "model": "test",
                    "variant": None,
                    "depth": 0,
                    "token_id": None,
                },
            )
        with self.assertRaisesRegex(state.MissionConflict, "inactive"):
            state.append_terminal(
                self.runs,
                self.mission_id,
                status="failed",
                reason="cannot seal active admissions",
                idempotency_key="terminal:active",
            )
        self.assertEqual(ledger.read_bytes(), before)

        current = self._current()
        lead_id = current["lead_admission_id"]
        self._finalize_one(
            current["admissions"][lead_id],
            status="failed",
            reason="Lead shutdown complete",
            idempotency_key="finalize:lead",
        )
        state.append_terminal(
            self.runs,
            self.mission_id,
            status="failed",
            reason="all admissions are inactive",
            idempotency_key="terminal:inactive",
        )
        self.assertEqual(self._current()["status"], "failed")

    def test_completing_and_archive_seal_all_admission_activity(self) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "all admissions.*inactive"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="mission_completing",
                actor="CONTROL",
                idempotency_key="closure:too-early",
                payload={"lead_artifact_id": "a" * 64},
            )
        self.assertEqual(ledger.read_bytes(), before)

        current = self._current()
        lead = current["admissions"][current["lead_admission_id"]]
        self._finalize_one(
            lead,
            status="failed",
            reason="Lead is quiescent",
            idempotency_key="closure:finalize-lead",
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="mission_completing",
            actor="CONTROL",
            idempotency_key="closure:completing",
            payload={"lead_artifact_id": "a" * 64},
        )
        completing_bytes = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "no longer accepts"):
            self._reserve_one("after-completing", recipient="after-completing")
        with self.assertRaisesRegex(state.MissionConflict, "no longer accepts"):
            self._commit_one(lead, idempotency_key="closure:commit-after-completing")
        self.assertEqual(ledger.read_bytes(), completing_bytes)

        state.append_event(
            self.runs,
            self.mission_id,
            kind="archive_created",
            actor="CONTROL",
            idempotency_key="closure:archive",
            payload={
                "path": "/tmp/archive.tar",
                "sha256": "b" * 64,
                "mode": "local",
            },
        )
        archived_bytes = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "no longer accepts"):
            self._reserve_one("after-archive", recipient="after-archive")
        self.assertEqual(ledger.read_bytes(), archived_bytes)

    def test_child_cannot_commit_before_parent(self) -> None:
        root_ids = admission.deterministic_ids(
            self.mission_id, request_key="parent-root", run_kind="specialist"
        )
        reserved = admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=[
                self._request(
                    "parent-root",
                    recipient="parent-root",
                    delegated_budget=2,
                ),
                self._request(
                    "child-before-parent",
                    recipient="child-before-parent",
                    parent_admission_id=str(root_ids["admission_id"]),
                ),
            ],
            idempotency_key="reserve:parent-child",
        )
        by_key = {item["request_key"]: item for item in reserved["admissions"]}
        before = state.ledger_path(self.runs, self.mission_id).read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "parent must be started"):
            self._commit_one(
                by_key["child-before-parent"],
                idempotency_key="commit:child:too-early",
            )
        self.assertEqual(
            state.ledger_path(self.runs, self.mission_id).read_bytes(), before
        )

        parent_commit = self._commit_one(
            by_key["parent-root"], idempotency_key="commit:parent-root"
        )
        before = state.ledger_path(self.runs, self.mission_id).read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "parent must be started"):
            self._commit_one(
                by_key["child-before-parent"],
                idempotency_key="commit:child:parent-only-committed",
            )
        self.assertEqual(
            state.ledger_path(self.runs, self.mission_id).read_bytes(), before
        )
        parent_authorized = self._authorize_one(by_key["parent-root"], parent_commit)
        self._start_one(by_key["parent-root"], parent_authorized)
        child_commit = self._commit_one(
            by_key["child-before-parent"],
            idempotency_key="commit:child:after-parent",
        )
        self.assertTrue(child_commit["appended"])
        self.assertIn(
            by_key["child-before-parent"]["run_id"], self._current()["run_owners"]
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="run_cancel_requested",
            actor="CONTROL",
            idempotency_key="cancel:parent-root",
            payload={
                "run_id": by_key["parent-root"]["run_id"],
                "reason": "test launch revalidation",
            },
        )
        before = state.ledger_path(self.runs, self.mission_id).read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "parent must be started"):
            self._authorize_one(
                by_key["child-before-parent"],
                child_commit,
                idempotency_key="authorize:cancelled-parent-child",
            )
        self.assertEqual(
            state.ledger_path(self.runs, self.mission_id).read_bytes(), before
        )

    def test_launch_authorization_reanchors_after_commit_head_drift(self) -> None:
        item = self._reserve_one("start-head", recipient="start-head")
        committed = self._commit_one(item)
        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_assessed",
            actor="CONTROL",
            idempotency_key="risk:between-commit-and-start",
            payload={
                "level": "low",
                "categories": [],
                "workflow_minimum": "low",
                "override": "auto",
                "requires_confirmation": False,
            },
        )
        prior_head = state.read_events(
            state.ledger_path(self.runs, self.mission_id),
            expected_mission_id=self.mission_id,
        )[-1]["event_sha256"]
        authorized = self._authorize_one(item, committed)
        self.assertEqual(authorized["event"]["previous_event_sha256"], prior_head)
        state.append_event(
            self.runs,
            self.mission_id,
            kind="human_approval_requested",
            actor="CONTROL",
            idempotency_key="human:between-authorize-and-start",
            payload={"reason": "head drift", "scope": "test"},
        )
        self._start_one(item, authorized)
        self.assertEqual(
            self._current()["admissions"][item["admission_id"]]["phase"],
            "started",
        )

    def test_cancellation_blocks_authorization_but_not_post_effect_start_record(
        self,
    ) -> None:
        cancelled = self._reserve_one(
            "cancel-before-auth", recipient="cancel-before-auth"
        )
        cancelled_commit = self._commit_one(cancelled)
        state.append_event(
            self.runs,
            self.mission_id,
            kind="run_cancel_requested",
            actor="CONTROL",
            idempotency_key="cancel:before-auth",
            payload={"run_id": cancelled["run_id"], "reason": "stop before effect"},
        )
        ledger = state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "cancelled admission"):
            self._authorize_one(cancelled, cancelled_commit)
        self.assertEqual(ledger.read_bytes(), before)

        linearized = self._reserve_one(
            "cancel-after-auth", recipient="cancel-after-auth"
        )
        linearized_commit = self._commit_one(linearized)
        authorized = self._authorize_one(linearized, linearized_commit)
        state.append_event(
            self.runs,
            self.mission_id,
            kind="run_cancel_requested",
            actor="CONTROL",
            idempotency_key="cancel:after-auth",
            payload={
                "run_id": linearized["run_id"],
                "reason": "effect may already exist",
            },
        )
        started = self._start_one(linearized, authorized)
        self.assertTrue(started["appended"])
        self.assertEqual(
            self._current()["admissions"][linearized["admission_id"]]["phase"],
            "started",
        )

    def test_multiple_commits_can_start_from_fresh_exact_heads(self) -> None:
        reserved = admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=[
                self._request("reanchor-a", recipient="reanchor-a"),
                self._request("reanchor-b", recipient="reanchor-b"),
            ],
            idempotency_key="reserve:reanchor",
        )
        by_key = {item["request_key"]: item for item in reserved["admissions"]}
        first = by_key["reanchor-a"]
        second = by_key["reanchor-b"]
        first_commit = self._commit_one(first)
        second_commit = self._commit_one(second)

        first_authorized = self._authorize_one(first, first_commit)
        second_authorized = self._authorize_one(second, second_commit)
        self._start_one(
            first,
            first_authorized,
            idempotency_key="start:reanchor-a",
        )
        self._start_one(
            second,
            second_authorized,
            idempotency_key="start:reanchor-b",
        )

        current = self._current()
        self.assertEqual(
            current["admissions"][first["admission_id"]]["phase"], "started"
        )
        self.assertEqual(
            current["admissions"][second["admission_id"]]["phase"], "started"
        )

    def test_concurrent_commits_recover_with_durable_launch_authorizations(
        self,
    ) -> None:
        reserved = admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=[
                self._request("process-a", recipient="process-a"),
                self._request("process-b", recipient="process-b"),
            ],
            idempotency_key="reserve:process-head-drift",
        )["admissions"]
        context = multiprocessing.get_context("spawn")
        committed = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_commit_authorize_worker,
                args=(str(self.runs), self.mission_id, item, committed, results),
            )
            for item in reserved
        ]
        try:
            for process in processes:
                process.start()
            rows = [results.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(timeout=30)
                self.assertFalse(process.is_alive(), "commit/authorize process hung")
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            results.close()
            results.join_thread()
        self.assertEqual([row[0] for row in rows], ["ok", "ok"], rows)
        current = self._current()
        self.assertTrue(
            all(
                current["admissions"][item["admission_id"]]["phase"] == "authorized"
                for item in reserved
            )
        )

    def test_result_keeps_writer_claim_until_finalization(self) -> None:
        item = self._reserve_one(
            "result-writer", recipient="result-writer", writer=True
        )
        committed = self._commit_one(item)
        registration_payload = {
            "delegation_id": item["delegation_id"],
            "mission_id": self.mission_id,
            "run_id": item["run_id"],
            "parent_run_id": item["parent_run_id"],
            "delegated_by": "lead",
            "recipient_instance": item["recipient_instance"],
            "capability": item["capability"],
            "objective_sha256": "e" * 64,
            "input_artifact_ids": [],
            "expected_output_contract": {"type": "text"},
            "deadline": "2099-01-01T00:00:00Z",
            "provider": "openai",
            "model": "test",
            "variant": None,
            "depth": 0,
            "token_id": None,
        }
        before = state.ledger_path(self.runs, self.mission_id).read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "globally owned"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="delegation_registered",
                actor="lead",
                idempotency_key="register:result-writer:too-early",
                payload=registration_payload,
            )
        self.assertEqual(
            state.ledger_path(self.runs, self.mission_id).read_bytes(), before
        )

        authorized = self._authorize_one(item, committed)
        self._start_one(
            item,
            authorized,
            idempotency_key="start:result-writer",
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="delegation_registered",
            actor="lead",
            idempotency_key="register:result-writer",
            payload=registration_payload,
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="result_recorded",
            actor="CONTROL",
            idempotency_key="result:result-writer",
            payload={
                "run_id": item["run_id"],
                "delegation_id": item["delegation_id"],
                "artifact_id": "f" * 64,
                "provider": "openai",
                "model": "test",
                "variant": None,
            },
        )
        current = self._current()
        self.assertEqual(current["active_writer"], item["admission_id"])
        self.assertTrue(current["admissions"][item["admission_id"]]["active"])
        before = state.ledger_path(self.runs, self.mission_id).read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "active writer"):
            self._reserve_one("next-writer", recipient="next-writer", writer=True)
        self.assertEqual(
            state.ledger_path(self.runs, self.mission_id).read_bytes(), before
        )

        self._finalize_one(
            item,
            status="succeeded",
            reason="result accepted",
            idempotency_key="finalize:result-writer",
            source_event_sha256="f" * 64,
        )
        next_writer = self._reserve_one(
            "next-writer", recipient="next-writer", writer=True
        )
        self.assertEqual(self._current()["active_writer"], next_writer["admission_id"])

    def test_parent_cannot_finalize_or_abort_with_active_children(self) -> None:
        parent_ids = admission.deterministic_ids(
            self.mission_id, request_key="terminal-parent", run_kind="specialist"
        )
        reserved = admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=[
                self._request(
                    "terminal-parent",
                    recipient="terminal-parent",
                    delegated_budget=1,
                ),
                self._request(
                    "terminal-child",
                    recipient="terminal-child",
                    parent_admission_id=str(parent_ids["admission_id"]),
                ),
            ],
            idempotency_key="reserve:terminal-tree",
        )
        by_key = {item["request_key"]: item for item in reserved["admissions"]}
        parent = by_key["terminal-parent"]
        child = by_key["terminal-child"]

        before = state.ledger_path(self.runs, self.mission_id).read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "active children"):
            self._abort_one(
                parent,
                reason="cannot abandon child",
                idempotency_key="abort:terminal-parent:too-early",
            )
        self.assertEqual(
            state.ledger_path(self.runs, self.mission_id).read_bytes(), before
        )

        parent_commit = self._commit_one(parent)
        parent_authorized = self._authorize_one(parent, parent_commit)
        self._start_one(
            parent,
            parent_authorized,
            idempotency_key="start:terminal-parent",
        )
        child_commit = self._commit_one(child)
        child_authorized = self._authorize_one(child, child_commit)
        self._start_one(
            child,
            child_authorized,
            idempotency_key="start:terminal-child",
        )
        before = state.ledger_path(self.runs, self.mission_id).read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "active children"):
            self._finalize_one(
                parent,
                status="succeeded",
                reason="cannot finish before child",
                idempotency_key="finalize:terminal-parent:too-early",
            )
        self.assertEqual(
            state.ledger_path(self.runs, self.mission_id).read_bytes(), before
        )

        self._finalize_one(
            child,
            status="succeeded",
            reason="child complete",
            idempotency_key="finalize:terminal-child",
        )
        self._finalize_one(
            parent,
            status="succeeded",
            reason="parent complete",
            idempotency_key="finalize:terminal-parent",
        )
        self.assertEqual(
            self._current()["admissions"][parent["admission_id"]]["phase"],
            "finalized",
        )

    def test_abort_never_owns_run_and_never_refunds_global_credits(self) -> None:
        mission_id = self._new_mission(credits=3)
        item = self._reserve_one(
            "expensive",
            mission_id=mission_id,
            recipient="expensive",
            delegated_budget=2,
        )
        reserved = self._current(mission_id)
        self.assertEqual(reserved["delegation_credits_spent"], 3)
        self.assertIn(item["run_id"], reserved["run_claims"])
        self.assertNotIn(item["run_id"], reserved["run_owners"])

        self._abort_one(
            item,
            mission_id=mission_id,
            reason="pre-dispatch validation failed",
            idempotency_key="abort:expensive",
        )

        aborted = self._current(mission_id)
        self.assertNotIn(item["run_id"], aborted["run_claims"])
        self.assertNotIn(item["run_id"], aborted["run_owners"])
        self.assertEqual(aborted["delegation_credits_spent"], 3)
        self.assertEqual(aborted["delegation_credits_remaining"], 0)
        self.assertEqual(aborted["active_delegations"], 0)
        before = state.ledger_path(self.runs, mission_id).read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "credits"):
            self._reserve_one(
                "after-abort", mission_id=mission_id, recipient="after-abort"
            )
        self.assertEqual(state.ledger_path(self.runs, mission_id).read_bytes(), before)

    def test_recipient_writer_and_run_claims_are_exclusive_across_processes(
        self,
    ) -> None:
        recipient_mission = self._new_mission()
        self._assert_one_race_winner(
            recipient_mission,
            (
                self._request("recipient-a", recipient="shared-recipient"),
                self._request("recipient-b", recipient="shared-recipient"),
            ),
        )
        self.assertEqual(
            sum(
                item["run_kind"] == "specialist"
                for item in self._current(recipient_mission)["admissions"].values()
            ),
            1,
        )

        writer_mission = self._new_mission()
        self._assert_one_race_winner(
            writer_mission,
            (
                self._request("writer-a", recipient="writer-a", writer=True),
                self._request("writer-b", recipient="writer-b", writer=True),
            ),
        )
        writer_state = self._current(writer_mission)
        self.assertEqual(
            sum(
                item["run_kind"] == "specialist"
                for item in writer_state["admissions"].values()
            ),
            1,
        )
        self.assertIsNotNone(writer_state["active_writer"])

        run_mission = self._new_mission()
        expected_run = admission.deterministic_ids(
            run_mission, request_key="same-run", run_kind="specialist"
        )["run_id"]
        self._assert_one_race_winner(
            run_mission,
            (
                self._request("same-run", recipient="run-a"),
                self._request("same-run", recipient="run-b"),
            ),
        )
        run_state = self._current(run_mission)
        self.assertEqual(list(run_state["run_claims"]), [expected_run])
        self.assertEqual(
            sum(
                item["run_kind"] == "specialist"
                for item in run_state["admissions"].values()
            ),
            1,
        )

    def test_deadline_blocks_new_reservations_and_stale_commit_proofs(self) -> None:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=1.25)
        deadline_at = self._timestamp(deadline)
        mission_id = self._new_mission(deadline_at=deadline_at)
        item = self._reserve_one(
            "deadline", mission_id=mission_id, recipient="deadline"
        )
        committed = self._commit_one(item, mission_id=mission_id)
        proof = {
            "admission_id": item["admission_id"],
            "commit_event_sha256": committed["commit_event_sha256"],
            "expected_head_sha256": committed["head_sha256"],
            "request_digest": item["request_digest"],
            "effect_sha256": item["effect_sha256"],
            "recipient_instance": item["recipient_instance"],
            "writer": item["writer"],
            "run_id": item["run_id"],
        }
        admission.verify_commit(self.runs, mission_id, **proof)
        while datetime.now(timezone.utc) <= deadline:
            time.sleep(0.01)
        before = state.ledger_path(self.runs, mission_id).read_bytes()

        with self.assertRaisesRegex(state.MissionConflict, "deadline"):
            admission.verify_commit(self.runs, mission_id, **proof)
        with self.assertRaisesRegex(state.MissionConflict, "deadline"):
            self._reserve_one("expired", mission_id=mission_id, recipient="expired")

        self.assertEqual(state.ledger_path(self.runs, mission_id).read_bytes(), before)

    def test_predeadline_authorization_can_start_after_deadline_without_orphan(
        self,
    ) -> None:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=1.0)
        mission_id = self._new_mission(deadline_at=self._timestamp(deadline))
        reserved = admission.reserve_many(
            self.runs,
            mission_id,
            requests=[
                self._request(
                    "authorized-before-deadline", recipient="before-deadline"
                ),
                self._request("unauthorized-at-deadline", recipient="at-deadline"),
            ],
            idempotency_key="reserve:deadline-linearization",
        )["admissions"]
        by_key = {item["request_key"]: item for item in reserved}
        before_item = by_key["authorized-before-deadline"]
        late_item = by_key["unauthorized-at-deadline"]
        before_commit = self._commit_one(before_item, mission_id=mission_id)
        late_commit = self._commit_one(late_item, mission_id=mission_id)
        authorized = self._authorize_one(
            before_item,
            before_commit,
            mission_id=mission_id,
        )
        while datetime.now(timezone.utc) <= deadline:
            time.sleep(0.01)

        self._start_one(before_item, authorized, mission_id=mission_id)
        self.assertEqual(
            self._current(mission_id)["admissions"][before_item["admission_id"]][
                "phase"
            ],
            "started",
        )
        before = state.ledger_path(self.runs, mission_id).read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "deadline"):
            self._authorize_one(late_item, late_commit, mission_id=mission_id)
        self.assertEqual(state.ledger_path(self.runs, mission_id).read_bytes(), before)

    def test_launch_authorization_is_denied_until_control_lane_is_running(self) -> None:
        mission_id = self._new_mission(
            bootstrap_lead=False,
            start_running=False,
        )
        item = self._reserve_one(
            "pre-running",
            mission_id=mission_id,
            recipient="pre-running",
        )
        committed = self._commit_one(item, mission_id=mission_id)
        ledger = state.ledger_path(self.runs, mission_id)

        compiled = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "running admission lane"):
            self._authorize_one(item, committed, mission_id=mission_id)
        self.assertEqual(ledger.read_bytes(), compiled)

        state.append_event(
            self.runs,
            mission_id,
            kind="fleet_boot_started",
            actor="CONTROL",
            idempotency_key="pre-running:boot",
            payload={"feature": "pre-running"},
        )
        booting = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "running admission lane"):
            self._authorize_one(
                item,
                committed,
                mission_id=mission_id,
                idempotency_key="authorize:pre-running:booting",
            )
        self.assertEqual(ledger.read_bytes(), booting)

        state.append_event(
            self.runs,
            mission_id,
            kind="mission_running",
            actor="CONTROL",
            idempotency_key="pre-running:running",
            payload={"manifest": str(self.runs / "pre-running.manifest")},
        )
        authorized = self._authorize_one(
            item,
            committed,
            mission_id=mission_id,
            idempotency_key="authorize:pre-running:running",
        )
        state.append_event(
            self.runs,
            mission_id,
            kind="mission_running",
            actor="CONTROL",
            idempotency_key="pre-running:head-drift",
            payload={"manifest": str(self.runs / "replacement.manifest")},
        )
        self._start_one(
            item,
            authorized,
            mission_id=mission_id,
            idempotency_key="start:pre-running:after-head-drift",
        )

    def test_assurance_handoff_closes_control_and_opens_only_assured_lane(self) -> None:
        current = self._current()
        lead = current["admissions"][current["lead_admission_id"]]
        state.append_event(
            self.runs,
            self.mission_id,
            kind="lead_dispatched",
            actor="CONTROL",
            idempotency_key="lane:lead-dispatched",
            payload={"run_id": lead["run_id"], "prompt_sha256": "1" * 64},
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_escalated",
            actor="CONTROL",
            idempotency_key="lane:risk",
            payload={
                "from": "low",
                "to": "high",
                "categories": ["production"],
                "reason": "exercise authority handoff",
            },
        )
        request_payload = {
            "risk": "high",
            "categories": ["production"],
            "scope": str(self.target.resolve()),
            "workflow_digest": WORKFLOW_DIGEST,
        }
        ledger = state.ledger_path(self.runs, self.mission_id)
        active = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "all admissions"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_requested",
                actor="CONTROL",
                idempotency_key="lane:request:too-early",
                payload=request_payload,
            )
        self.assertEqual(ledger.read_bytes(), active)

        self._finalize_one(
            lead,
            status="succeeded",
            reason="CONTROL handoff complete",
            idempotency_key="lane:lead-finalized",
        )
        request, _ = state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_requested",
            actor="CONTROL",
            idempotency_key="lane:request",
            payload=request_payload,
        )
        waiting = ledger.read_bytes()
        for actor in ("CONTROL", "ASSURED"):
            with (
                self.subTest(status="waiting", actor=actor),
                self.assertRaisesRegex(state.MissionConflict, "no longer accepts"),
            ):
                admission.reserve_many(
                    self.runs,
                    self.mission_id,
                    requests=[self._request(f"waiting-{actor.lower()}")],
                    idempotency_key=f"lane:waiting:{actor.lower()}",
                    actor=actor,
                )
            self.assertEqual(ledger.read_bytes(), waiting)

        approval, _ = state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_approved",
            actor="HUMAN",
            idempotency_key="lane:approval",
            payload={
                "approval_id": str(uuid.uuid4()),
                "request_event_sha256": request["event_sha256"],
                "workflow_digest": WORKFLOW_DIGEST,
                "scope": str(self.target.resolve()),
                "risk": "high",
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(seconds=600)
                ).isoformat(),
                "expires_in_seconds": 600,
                "approved_by_sha256": "2" * 64,
                "decision": "approved",
            },
        )
        approved = ledger.read_bytes()
        for actor in ("CONTROL", "ASSURED"):
            with (
                self.subTest(status="approved", actor=actor),
                self.assertRaisesRegex(state.MissionConflict, "no longer accepts"),
            ):
                admission.reserve_many(
                    self.runs,
                    self.mission_id,
                    requests=[self._request(f"approved-{actor.lower()}")],
                    idempotency_key=f"lane:approved:{actor.lower()}",
                    actor=actor,
                )
            self.assertEqual(ledger.read_bytes(), approved)

        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_boot_started",
            actor="CONTROL",
            idempotency_key="lane:assurance-boot",
            payload={
                "preset": "fleet_dialogue",
                "approval_event_sha256": approval["event_sha256"],
            },
        )
        assured_booting = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "ASSURED actor"):
            admission.reserve_many(
                self.runs,
                self.mission_id,
                requests=[self._request("assured-control-bypass")],
                idempotency_key="lane:assured:control-bypass",
                actor="CONTROL",
            )
        self.assertEqual(ledger.read_bytes(), assured_booting)

        assured = admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=[self._request("assured-root", recipient="assured-root")],
            idempotency_key="lane:assured:reserve",
            actor="ASSURED",
        )["admissions"][0]
        self.assertIsNone(assured["parent_admission_id"])
        self.assertIsNone(assured["parent_run_id"])
        self.assertEqual(assured["lane_actor"], "ASSURED")

        before_wrong_commit = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "ASSURED actor"):
            self._commit_one(
                assured,
                actor="CONTROL",
                idempotency_key="lane:assured:wrong-commit",
            )
        self.assertEqual(ledger.read_bytes(), before_wrong_commit)
        committed = self._commit_one(
            assured,
            actor="ASSURED",
            idempotency_key="lane:assured:commit",
        )
        before_running = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "running admission lane"):
            self._authorize_one(
                assured,
                committed,
                actor="ASSURED",
                approval_event_sha256=approval["event_sha256"],
                idempotency_key="lane:assured:authorize-booting",
            )
        self.assertEqual(ledger.read_bytes(), before_running)

        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_started",
            actor="CONTROL",
            idempotency_key="lane:assurance-started",
            payload={
                "manifest": str(self.runs / "assured.manifest"),
                "approval_event_sha256": approval["event_sha256"],
            },
        )
        with self.assertRaisesRegex(state.MissionConflict, "ASSURED actor"):
            self._authorize_one(
                assured,
                committed,
                actor="CONTROL",
                idempotency_key="lane:assured:wrong-authorize",
            )
        authorized = self._authorize_one(
            assured,
            committed,
            actor="ASSURED",
            approval_event_sha256=approval["event_sha256"],
            idempotency_key="lane:assured:authorize",
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_assessed",
            actor="CONTROL",
            idempotency_key="lane:assured:post-auth-head",
            payload={
                "level": "high",
                "categories": ["production"],
                "workflow_minimum": "high",
                "override": "auto",
                "requires_confirmation": True,
            },
        )
        self._start_one(
            assured,
            authorized,
            actor="ASSURED",
            idempotency_key="lane:assured:start",
        )
        self._finalize_one(
            assured,
            actor="ASSURED",
            status="succeeded",
            reason="assured root complete",
            idempotency_key="lane:assured:finalize",
        )

    def test_assured_admissions_require_live_exact_approval_and_can_renew_quiescent(
        self,
    ) -> None:
        current = self._current()
        lead = current["admissions"][current["lead_admission_id"]]
        self._finalize_one(
            lead,
            status="succeeded",
            reason="CONTROL handoff complete",
            idempotency_key="expiry-lane:lead-finalized",
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_escalated",
            actor="CONTROL",
            idempotency_key="expiry-lane:risk",
            payload={
                "from": "low",
                "to": "high",
                "categories": ["production"],
                "reason": "exercise assured approval lifetime",
            },
        )
        request, _ = state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_requested",
            actor="CONTROL",
            idempotency_key="expiry-lane:request",
            payload={
                "risk": "high",
                "categories": ["production"],
                "scope": str(self.target.resolve()),
                "workflow_digest": WORKFLOW_DIGEST,
            },
        )
        approved_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        expires_at = approved_at + timedelta(seconds=60)
        with mock.patch.object(
            state, "_next_timestamp", return_value=approved_at.isoformat()
        ):
            approval, _ = state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_approved",
                actor="HUMAN",
                idempotency_key="expiry-lane:approval",
                payload={
                    "approval_id": str(uuid.uuid4()),
                    "request_event_sha256": request["event_sha256"],
                    "workflow_digest": WORKFLOW_DIGEST,
                    "scope": str(self.target.resolve()),
                    "risk": "high",
                    "expires_at": expires_at.isoformat(),
                    "expires_in_seconds": 60,
                    "approved_by_sha256": "6" * 64,
                    "decision": "approved",
                },
            )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_boot_started",
            actor="CONTROL",
            idempotency_key="expiry-lane:boot",
            payload={
                "preset": "fleet_dialogue",
                "approval_event_sha256": approval["event_sha256"],
            },
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_started",
            actor="CONTROL",
            idempotency_key="expiry-lane:started",
            payload={
                "manifest": str(self.runs / "expiry-assured.manifest"),
                "approval_event_sha256": approval["event_sha256"],
            },
        )

        reserved = admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=[
                self._request("expiry-authorized", recipient="expiry-authorized"),
                self._request("expiry-committed", recipient="expiry-committed"),
                self._request("expiry-reserved", recipient="expiry-reserved"),
            ],
            idempotency_key="expiry-lane:reserve-live",
            actor="ASSURED",
        )["admissions"]
        by_key = {item["request_key"]: item for item in reserved}
        authorized_item = by_key["expiry-authorized"]
        committed_item = by_key["expiry-committed"]
        reserved_item = by_key["expiry-reserved"]
        authorized_commit = self._commit_one(authorized_item, actor="ASSURED")
        committed_commit = self._commit_one(committed_item, actor="ASSURED")
        authorized = self._authorize_one(
            authorized_item,
            authorized_commit,
            actor="ASSURED",
            approval_event_sha256=approval["event_sha256"],
        )

        after_expiry = expires_at + timedelta(seconds=1)
        with mock.patch.object(
            state, "_next_timestamp", return_value=after_expiry.isoformat()
        ):
            self._start_one(
                authorized_item,
                authorized,
                actor="ASSURED",
                idempotency_key="expiry-lane:start-after-expiry",
            )
        ledger = state.ledger_path(self.runs, self.mission_id)
        expired_bytes = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "approval has expired"):
            admission.reserve_many(
                self.runs,
                self.mission_id,
                requests=[self._request("expiry-new", recipient="expiry-new")],
                idempotency_key="expiry-lane:reserve-expired",
                actor="ASSURED",
            )
        with self.assertRaisesRegex(state.MissionConflict, "approval has expired"):
            self._commit_one(
                reserved_item,
                actor="ASSURED",
                idempotency_key="expiry-lane:commit-expired",
            )
        with self.assertRaisesRegex(state.MissionConflict, "approval has expired"):
            self._authorize_one(
                committed_item,
                committed_commit,
                actor="ASSURED",
                approval_event_sha256=approval["event_sha256"],
                idempotency_key="expiry-lane:authorize-expired",
            )
        self.assertEqual(ledger.read_bytes(), expired_bytes)

        renewal_key = "expiry-lane:renewal"
        renewal_payload = {
            "approval_id": str(
                uuid.uuid5(
                    uuid.UUID(self.mission_id),
                    f"approval-renewal:{approval['event_sha256']}:{renewal_key}",
                )
            ),
            "prior_approval_event_sha256": approval["event_sha256"],
            "request_event_sha256": approval["payload"]["request_event_sha256"],
            "workflow_digest": WORKFLOW_DIGEST,
            "scope": str(self.target.resolve()),
            "risk": "high",
            "expires_at": (after_expiry + timedelta(seconds=600)).isoformat(),
            "expires_in_seconds": 600,
            "approved_by_sha256": "7" * 64,
            "decision": "approved",
        }
        with self.assertRaisesRegex(state.MissionConflict, "inactive"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_approval_renewed",
                actor="HUMAN",
                idempotency_key=renewal_key,
                payload=renewal_payload,
            )

        self._finalize_one(
            authorized_item,
            actor="ASSURED",
            status="abandoned",
            reason="approval expired after launch authorization",
            idempotency_key="expiry-lane:finalize-authorized",
        )
        self._abort_one(
            committed_item,
            actor="ASSURED",
            reason="approval expired before launch authorization",
            idempotency_key="expiry-lane:abort-committed",
        )
        self._abort_one(
            reserved_item,
            actor="ASSURED",
            reason="approval expired before commit",
            idempotency_key="expiry-lane:abort-reserved",
        )
        renewed, appended = state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_approval_renewed",
            actor="HUMAN",
            idempotency_key=renewal_key,
            payload=renewal_payload,
        )
        self.assertTrue(appended)
        self.assertEqual(self._current()["status"], "assured_running")

        renewed_item = admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=[
                self._request(
                    "expiry-renewed",
                    recipient="expiry-renewed",
                    effect_sha256=state.sha256(
                        {
                            "task": "expiry-renewed",
                            "approval_event_sha256": renewed["event_sha256"],
                        }
                    ),
                )
            ],
            idempotency_key="expiry-lane:reserve-renewed",
            actor="ASSURED",
        )["admissions"][0]
        renewed_commit = self._commit_one(renewed_item, actor="ASSURED")
        before_old_reference = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "approval reference"):
            self._authorize_one(
                renewed_item,
                renewed_commit,
                actor="ASSURED",
                approval_event_sha256=approval["event_sha256"],
                idempotency_key="expiry-lane:authorize-old-approval",
            )
        self.assertEqual(ledger.read_bytes(), before_old_reference)
        renewed_authorized = self._authorize_one(
            renewed_item,
            renewed_commit,
            actor="ASSURED",
            approval_event_sha256=renewed["event_sha256"],
            idempotency_key="expiry-lane:authorize-renewed",
        )
        with self.assertRaisesRegex(state.MissionConflict, "proof does not match"):
            self._start_one(
                renewed_item,
                authorized,
                actor="ASSURED",
                idempotency_key="expiry-lane:start-old-authorization",
            )
        self._start_one(
            renewed_item,
            renewed_authorized,
            actor="ASSURED",
            idempotency_key="expiry-lane:start-renewed",
        )

    def test_finalization_rejects_crossed_ownership_bindings_without_release(
        self,
    ) -> None:
        reserved = admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=[
                self._request("owned-writer", recipient="owned-writer", writer=True),
                self._request("other-run", recipient="other-run"),
            ],
            idempotency_key="reserve:finalize-bindings",
        )["admissions"]
        by_key = {item["request_key"]: item for item in reserved}
        owned = by_key["owned-writer"]
        other = by_key["other-run"]
        self._commit_one(owned)
        before = state.ledger_path(self.runs, self.mission_id).read_bytes()

        base_evidence = {
            "schema_version": 1,
            "source_event_sha256": "e" * 64,
            "run_id": owned["run_id"],
            "task_sha256": owned["task_sha256"],
            "status": "failed",
        }
        crossed = (
            (
                {"terminal_evidence": {**base_evidence, "run_id": other["run_id"]}},
                "ownership binding",
            ),
            (
                {
                    "terminal_evidence": {
                        **base_evidence,
                        "task_sha256": other["task_sha256"],
                    }
                },
                "structured terminal evidence binding",
            ),
            ({"recipient_instance": other["recipient_instance"]}, "ownership binding"),
            ({"writer": other["writer"]}, "ownership binding"),
        )
        for index, (override, error) in enumerate(crossed):
            with (
                self.subTest(override=override),
                self.assertRaisesRegex(state.MissionConflict, error),
            ):
                self._finalize_one(
                    owned,
                    status="failed",
                    reason="crossed terminal evidence",
                    idempotency_key=f"finalize:crossed:{index}",
                    **override,
                )
            self.assertEqual(
                state.ledger_path(self.runs, self.mission_id).read_bytes(), before
            )
        with self.assertRaisesRegex(admission.AdmissionError, "source_event_sha256"):
            self._finalize_one(
                owned,
                status="failed",
                reason="malformed evidence",
                idempotency_key="finalize:malformed-evidence",
                source_event_sha256="not-a-sha",
            )
        with self.assertRaisesRegex(state.MissionConflict, "evidence digest"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="delegation_finalized",
                actor="CONTROL",
                idempotency_key="finalize:forged-digest",
                payload={
                    "admission_id": owned["admission_id"],
                    "run_id": owned["run_id"],
                    "recipient_instance": owned["recipient_instance"],
                    "writer": owned["writer"],
                    "terminal_evidence": base_evidence,
                    "terminal_evidence_sha256": "0" * 64,
                    "status": "failed",
                    "reason": "digest is not bound",
                },
            )
        current = self._current()
        self.assertEqual(current["active_writer"], owned["admission_id"])
        self.assertTrue(current["admissions"][owned["admission_id"]]["active"])

    def test_first_finalization_is_immutable_but_exact_retry_is_idempotent(
        self,
    ) -> None:
        item = self._reserve_one("terminal", recipient="terminal")
        self._commit_one(item)
        first = self._finalize_one(
            item,
            status="failed",
            reason="worker failed",
            idempotency_key="finalize:terminal",
        )
        terminal_payload = first["event"]["payload"]
        self.assertEqual(
            terminal_payload["terminal_evidence"]["run_id"], item["run_id"]
        )
        self.assertEqual(
            terminal_payload["terminal_evidence"]["task_sha256"],
            item["task_sha256"],
        )
        self.assertEqual(
            terminal_payload["terminal_evidence_sha256"],
            state.sha256(
                {
                    "mission_id": self.mission_id,
                    "admission_id": item["admission_id"],
                    "recipient_instance": item["recipient_instance"],
                    "writer": item["writer"],
                    "terminal_evidence": terminal_payload["terminal_evidence"],
                }
            ),
        )
        before = state.ledger_path(self.runs, self.mission_id).read_bytes()

        repeated = self._finalize_one(
            item,
            status="failed",
            reason="worker failed",
            idempotency_key="finalize:terminal",
        )

        self.assertTrue(first["appended"])
        self.assertFalse(repeated["appended"])
        self.assertEqual(repeated["event"], first["event"])
        with self.assertRaises(state.MissionConflict):
            self._finalize_one(
                item,
                status="succeeded",
                reason="late contradictory result",
                idempotency_key="finalize:terminal:second",
            )
        with self.assertRaises(state.MissionConflict):
            self._finalize_one(
                item,
                status="failed",
                reason="payload drift",
                idempotency_key="finalize:terminal",
            )
        self.assertEqual(
            state.ledger_path(self.runs, self.mission_id).read_bytes(), before
        )

    def test_historical_admission_schemas_are_read_only_for_raw_appends(self) -> None:
        item = self._reserve_one("legacy-schema", recipient="legacy-schema")
        reservation = next(
            event
            for event in state.read_events(
                state.ledger_path(self.runs, self.mission_id),
                expected_mission_id=self.mission_id,
            )
            if event["idempotency_key"] == "reserve:legacy-schema"
        )
        historical_admission = {
            field: value
            for field, value in reservation["payload"]["admissions"][0].items()
            if field not in {"effect_sha256", "task_sha256"}
        }
        historical_requests = (
            (
                "delegations_reserved",
                {
                    "batch_id": str(uuid.uuid4()),
                    "batch_sha256": state.sha256([historical_admission]),
                    "admissions": [historical_admission],
                },
                "reservation schema",
            ),
            (
                "delegation_committed",
                {
                    "admission_id": item["admission_id"],
                    "commit_id": str(
                        uuid.uuid5(
                            uuid.UUID(self.mission_id),
                            f"commit:{item['admission_id']}",
                        )
                    ),
                    "reservation_event_sha256": reservation["event_sha256"],
                    "request_digest": item["request_digest"],
                    "recipient_instance": item["recipient_instance"],
                    "writer": item["writer"],
                    "run_id": item["run_id"],
                },
                "commit schema",
            ),
            (
                "delegation_started",
                {
                    "admission_id": item["admission_id"],
                    "commit_event_sha256": "3" * 64,
                    "expected_head_sha256": "4" * 64,
                    "request_digest": item["request_digest"],
                    "recipient_instance": item["recipient_instance"],
                    "writer": item["writer"],
                    "run_id": item["run_id"],
                },
                "start schema",
            ),
            (
                "delegation_finalized",
                {
                    "admission_id": item["admission_id"],
                    "run_id": item["run_id"],
                    "recipient_instance": item["recipient_instance"],
                    "writer": item["writer"],
                    "terminal_evidence_sha256": "5" * 64,
                    "status": "failed",
                    "reason": "historical envelope",
                },
                "finalization schema",
            ),
        )
        ledger = state.ledger_path(self.runs, self.mission_id)
        stable = ledger.read_bytes()
        for index, (kind, payload, error) in enumerate(historical_requests):
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(state.MissionConflict, error),
            ):
                state.append_event(
                    self.runs,
                    self.mission_id,
                    kind=kind,
                    actor="CONTROL",
                    idempotency_key=f"legacy-schema:raw:{index}",
                    payload=payload,
                )
            self.assertEqual(ledger.read_bytes(), stable)

    def test_invalid_or_unknown_payloads_are_rejected_without_mutation(self) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()
        before_stat = ledger.stat()
        invalid_calls = (
            lambda: state.append_event(
                self.runs,
                self.mission_id,
                kind="future_admission_event",
                actor="CONTROL",
                idempotency_key="invalid:unknown-kind",
                payload={},
            ),
            lambda: state.append_event(
                self.runs,
                self.mission_id,
                kind="delegation_started",
                actor="CONTROL",
                idempotency_key="invalid:extra-field",
                payload={
                    "admission_id": str(uuid.uuid4()),
                    "commit_event_sha256": "0" * 64,
                    "extra": True,
                },
            ),
            lambda: state.append_event(
                self.runs,
                self.mission_id,
                kind="delegation_reservation_aborted",
                actor="CONTROL",
                idempotency_key="invalid:missing-field",
                payload={"admission_id": str(uuid.uuid4())},
            ),
            lambda: admission.reserve_many(
                self.runs,
                self.mission_id,
                requests=[{**self._request("extra"), "unexpected": "field"}],
                idempotency_key="invalid:request-extra",
            ),
            lambda: admission.reserve_many(
                self.runs,
                self.mission_id,
                requests=[{**self._request("bad-writer"), "writer": "yes"}],
                idempotency_key="invalid:request-type",
            ),
        )
        for invalid_call in invalid_calls:
            with self.subTest(call=invalid_call):
                with self.assertRaises(state.MissionStateError):
                    invalid_call()

        after_stat = ledger.stat()
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(after_stat.st_ino, before_stat.st_ino)
        self.assertEqual(after_stat.st_size, before_stat.st_size)
        self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
