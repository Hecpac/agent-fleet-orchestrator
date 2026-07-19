from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

from tests.mission_control_test_support import create_running_mission

import fleet_admission
import fleet_delegation
import fleet_mission_state


ARTIFACT_A = "a" * 64
ARTIFACT_B = "b" * 64
ARTIFACT_C = "c" * 64
ROOT = Path(__file__).resolve().parents[1]


def _authority_publication_worker(
    operation: str,
    runs_dir: str,
    mission_id: str,
    arguments: dict,
    admission_id: str,
    validated: object,
    release: object,
    results: object,
) -> None:
    """Pause one issuer/binder after authority validation while it owns the mission."""

    original = fleet_delegation._validate_v3_authority
    paused = False

    def guarded(*args: object, **kwargs: object) -> None:
        nonlocal paused
        original(*args, **kwargs)
        token = kwargs.get("token")
        if (
            not paused
            and isinstance(token, dict)
            and token.get("admission_id") == admission_id
            and kwargs.get("effect_stage") is True
        ):
            paused = True
            validated.wait(timeout=20)
            if not release.wait(timeout=20):
                raise RuntimeError("authority publication barrier timed out")

    fleet_delegation._validate_v3_authority = guarded
    try:
        if operation == "issue":
            value = fleet_delegation.issue_token(
                Path(runs_dir), mission_id, **arguments
            )["token_sha256"]
        elif operation == "bind":
            fleet_delegation.bind_token(Path(runs_dir), mission_id, **arguments)
            value = "bound"
        else:  # pragma: no cover - parent controls this fixture.
            raise RuntimeError("unsupported authority publication operation")
    except Exception as exc:  # noqa: BLE001 - parent asserts exact process outcome.
        results.put((operation, "error", type(exc).__name__, str(exc)))
    else:
        results.put((operation, "ok", value, ""))


def _authority_terminal_worker(
    operation: str,
    runs_dir: str,
    mission_id: str,
    admission: dict,
    started: object,
    finished: object,
    results: object,
) -> None:
    """Attempt a terminal transition while an issuer/binder owns the mission lock."""

    started.set()
    try:
        if operation == "finalize":
            fleet_admission.finalize(
                Path(runs_dir),
                mission_id,
                admission_id=admission["admission_id"],
                recipient_instance=admission["recipient_instance"],
                writer=admission["writer"],
                terminal_evidence={
                    "schema_version": 1,
                    "source_event_sha256": "f" * 64,
                    "run_id": admission["run_id"],
                    "task_sha256": admission["task_sha256"],
                    "status": "failed",
                },
                reason="deterministic token publication race",
                idempotency_key=f"race:finalize:{admission['request_key']}",
            )
        elif operation == "cancel":
            fleet_mission_state.append_event(
                Path(runs_dir),
                mission_id,
                kind="run_cancel_requested",
                actor="CONTROL",
                idempotency_key=f"race:cancel:{admission['request_key']}",
                payload={
                    "run_id": admission["run_id"],
                    "reason": "deterministic token binding race",
                },
            )
        else:  # pragma: no cover - parent controls this fixture.
            raise RuntimeError("unsupported authority terminal operation")
    except Exception as exc:  # noqa: BLE001 - parent asserts exact process outcome.
        results.put((operation, "error", type(exc).__name__, str(exc)))
    else:
        results.put((operation, "ok", "published", ""))
    finally:
        finished.set()


class FleetDelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs, self.mission_id, self.lead_run_id = create_running_mission(
            self.tmp, feature="delegation"
        )

    def issue(
        self,
        *,
        delegation_id: str | None = None,
        can_delegate: bool = True,
        allowed_capabilities: list[str] | None = None,
        allowed_artifact_ids: list[str] | None = None,
        remaining_budget: int = 4,
        idempotency_key: str = "token:one",
        recipient_instance: str = "token_worker",
    ) -> tuple[dict, str, str]:
        request_key = (
            f"token-{fleet_mission_state.sha256({'key': idempotency_key})[:24]}"
        )
        effective_capabilities = allowed_capabilities or (
            ["challenge"] if can_delegate else []
        )
        effective_artifacts = allowed_artifact_ids or []
        task_sha256 = fleet_mission_state.sha256(
            {"kind": "delegation-token-task", "request_key": request_key}
        )
        effect_sha256 = fleet_mission_state.sha256(
            {
                "kind": "delegation-token-fixture",
                "request_key": request_key,
                "can_delegate": can_delegate,
                "allowed_capabilities": effective_capabilities,
                "allowed_artifact_ids": effective_artifacts,
                "remaining_budget": remaining_budget,
                "recipient_instance": recipient_instance,
            }
        )
        if delegation_id is not None:
            expected = fleet_admission.deterministic_ids(
                self.mission_id,
                request_key=request_key,
                run_kind="specialist",
            )["delegation_id"]
            self.assertEqual(delegation_id, expected)
        reserved = fleet_admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=[
                {
                    "request_key": request_key,
                    "run_kind": "specialist",
                    "recipient_instance": recipient_instance,
                    "capability": "challenge",
                    "effect_sha256": effect_sha256,
                    "task_sha256": task_sha256,
                    "parent_admission_id": fleet_mission_state.derive_state(
                        self.events()
                    )["lead_admission_id"],
                    "parent_run_id": self.lead_run_id,
                    "delegated_budget": remaining_budget,
                    "writer": False,
                }
            ],
            idempotency_key=f"admission:{request_key}",
        )["admissions"][0]
        committed = fleet_admission.commit(
            self.runs,
            self.mission_id,
            admission_id=reserved["admission_id"],
            request_digest=reserved["request_digest"],
            effect_sha256=reserved["effect_sha256"],
            recipient_instance=reserved["recipient_instance"],
            writer=False,
            run_id=reserved["run_id"],
            idempotency_key=f"admission:commit:{request_key}",
        )
        authorized = fleet_admission.authorize_launch(
            self.runs,
            self.mission_id,
            admission_id=reserved["admission_id"],
            commit_event_sha256=committed["commit_event_sha256"],
            request_digest=reserved["request_digest"],
            effect_sha256=reserved["effect_sha256"],
            recipient_instance=reserved["recipient_instance"],
            writer=False,
            run_id=reserved["run_id"],
            idempotency_key=f"admission:authorize:{request_key}",
        )
        fleet_admission.mark_started(
            self.runs,
            self.mission_id,
            admission_id=reserved["admission_id"],
            authorization_event_sha256=authorized["authorization_event_sha256"],
            request_digest=reserved["request_digest"],
            effect_sha256=reserved["effect_sha256"],
            recipient_instance=reserved["recipient_instance"],
            writer=False,
            run_id=reserved["run_id"],
            idempotency_key=f"admission:start:{request_key}",
        )
        delegation_id = str(reserved["delegation_id"])
        child_run = str(reserved["run_id"])
        token = fleet_delegation.issue_token(
            self.runs,
            self.mission_id,
            delegation_id=delegation_id,
            parent_run_id=self.lead_run_id,
            can_delegate=can_delegate,
            allowed_capabilities=effective_capabilities,
            allowed_artifact_ids=effective_artifacts,
            current_depth=0,
            max_depth=3,
            remaining_budget=remaining_budget,
            writer_instance="builder",
            idempotency_key=idempotency_key,
            run_id=child_run,
            admission_id=reserved["admission_id"],
            reservation_event_sha256=reserved["reservation_event_sha256"],
            commit_id=committed["commit_id"],
            request_digest=reserved["request_digest"],
        )
        fleet_delegation.bind_token(
            self.runs,
            self.mission_id,
            token_id=token["token_id"],
            delegation_id=delegation_id,
            run_id=child_run,
            idempotency_key=f"{idempotency_key}:bind",
        )
        return token, delegation_id, child_run

    def reserve_children(
        self,
        token: dict,
        requests: list[dict],
        *,
        idempotency_key: str,
    ) -> dict:
        admissions = []
        for request in requests:
            child_suffix = request["requested_delegation_id"].replace("-", "")[:12]
            admissions.append(
                {
                    "request_key": f"child-{request['requested_delegation_id'].replace('-', '')[:24]}",
                    "run_kind": "specialist",
                    "recipient_instance": f"child_{child_suffix}",
                    "capability": request["capability"],
                    "effect_sha256": fleet_mission_state.sha256(
                        {"kind": "child-token-fixture", "request": request}
                    ),
                    "task_sha256": fleet_mission_state.sha256(
                        {"kind": "child-token-task", "request": request}
                    ),
                    "parent_admission_id": token["admission_id"],
                    "parent_run_id": token["run_id"],
                    "delegated_budget": (
                        request["requested_budget"]
                        if request["child_can_delegate"]
                        else 0
                    ),
                    "writer": False,
                }
            )
        return fleet_admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=admissions,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def request(
        *,
        requested_delegation_id: str | None = None,
        capability: str = "challenge",
        requested_budget: int = 0,
        child_can_delegate: bool = False,
        child_allowed_capabilities: list[str] | None = None,
        requested_artifact_ids: list[str] | None = None,
    ) -> dict:
        return {
            "requested_delegation_id": requested_delegation_id or str(uuid.uuid4()),
            "capability": capability,
            "requested_budget": requested_budget,
            "child_can_delegate": child_can_delegate,
            "child_allowed_capabilities": child_allowed_capabilities or [],
            "requested_artifact_ids": requested_artifact_ids or [],
        }

    def events(self) -> list[dict]:
        return fleet_mission_state.read_events(
            fleet_mission_state.ledger_path(self.runs, self.mission_id),
            expected_mission_id=self.mission_id,
        )

    def register_token(
        self,
        token: dict,
        *,
        delegation_id: str,
        run_id: str,
        idempotency_key: str,
        overrides: dict | None = None,
    ) -> None:
        current = fleet_mission_state.derive_state(self.events())
        admission = current["admissions"][token["admission_id"]]
        parent = current["admissions"][admission["parent_admission_id"]]
        payload = {
            "delegation_id": delegation_id,
            "mission_id": self.mission_id,
            "run_id": run_id,
            "parent_run_id": token["parent_run_id"],
            "delegated_by": (
                "lead" if parent["run_kind"] == "lead" else parent["run_id"]
            ),
            "recipient_instance": admission["recipient_instance"],
            "capability": admission["capability"],
            "objective_sha256": "d" * 64,
            "input_artifact_ids": token["allowed_artifact_ids"],
            "expected_output_contract": {},
            "deadline": "2099-01-01T00:00:00Z",
            "provider": "openai",
            "model": "test",
            "variant": None,
            "depth": token["current_depth"],
            "token_id": token["token_id"],
        }
        payload.update(overrides or {})
        fleet_mission_state.append_event(
            self.runs,
            self.mission_id,
            kind="delegation_registered",
            actor="CONTROL",
            idempotency_key=idempotency_key,
            payload=payload,
        )

    def test_authorized_token_is_ledger_bound_and_scoped(self) -> None:
        token, delegation_id, child_run = self.issue(remaining_budget=2)
        leaf_id = str(uuid.uuid4())
        verified = fleet_delegation.validate_for_subdelegation(
            self.runs,
            self.mission_id,
            token_id=token["token_id"],
            delegation_id=delegation_id,
            parent_run_id=child_run,
            capability="challenge",
            requested_budget=0,
            requested_delegation_id=leaf_id,
        )
        self.assertEqual(verified["writer_instance"], "builder")
        current = fleet_mission_state.derive_state(self.events())
        self.assertEqual(
            verified["effect_sha256"],
            current["admissions"][verified["admission_id"]]["effect_sha256"],
        )
        self.reserve_children(
            token,
            [self.request(requested_delegation_id=leaf_id)],
            idempotency_key="admission:validated-leaf",
        )
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError, "outside delegated scope"
        ):
            fleet_delegation.validate_for_subdelegation(
                self.runs,
                self.mission_id,
                token_id=token["token_id"],
                delegation_id=delegation_id,
                parent_run_id=child_run,
                capability="build",
                requested_budget=0,
                requested_delegation_id=str(uuid.uuid4()),
            )
        branch = self.request(
            requested_budget=1,
            child_can_delegate=True,
            child_allowed_capabilities=["challenge"],
        )
        fleet_delegation.validate_for_subdelegations(
            self.runs,
            self.mission_id,
            token_id=token["token_id"],
            delegation_id=delegation_id,
            parent_run_id=child_run,
            requests=[branch],
        )
        with self.assertRaisesRegex(
            fleet_mission_state.MissionConflict,
            "parent.*budget|delegated budget|credits",
        ):
            self.reserve_children(
                token,
                [branch],
                idempotency_key="admission:validated-overflow",
            )

    def test_token_tampering_is_rejected_even_if_file_remains_valid_json(self) -> None:
        token, delegation_id, child_run = self.issue()
        path = Path(token["path"])
        value = json.loads(path.read_text())
        value["allowed_capabilities"].append("build")
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError, "canonical|hash mismatch"
        ):
            fleet_delegation.validate_for_subdelegation(
                self.runs,
                self.mission_id,
                token_id=token["token_id"],
                delegation_id=delegation_id,
                parent_run_id=child_run,
                capability="build",
                requested_budget=0,
                requested_delegation_id=str(uuid.uuid4()),
            )

    def test_resealed_token_tamper_cannot_replace_ledger_authority(self) -> None:
        token, _, _ = self.issue(
            allowed_artifact_ids=[ARTIFACT_A],
            idempotency_key="token:resealed-tamper",
        )
        path = Path(token["path"])
        value = json.loads(path.read_text())
        value["allowed_artifact_ids"] = [ARTIFACT_B]
        value["effect_sha256"] = ARTIFACT_C
        value["token_sha256"] = fleet_mission_state.sha256(
            {key: item for key, item in value.items() if key != "token_sha256"}
        )
        path.write_bytes(fleet_mission_state.canonical_bytes(value) + b"\n")
        ledger = fleet_mission_state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()

        with self.assertRaisesRegex(
            fleet_delegation.DelegationError,
            "not bound to the mission ledger",
        ):
            fleet_delegation.load_token(
                self.runs,
                self.mission_id,
                token["token_id"],
            )
        self.assertEqual(ledger.read_bytes(), before)

    def test_bound_v3_rederives_capability_depth_and_delegated_by(self) -> None:
        cases = (
            ("capability", "verify"),
            ("depth", 1),
            ("delegated_by", self.lead_run_id),
        )
        for field, value in cases:
            with self.subTest(field=field):
                token, delegation_id, run_id = self.issue(
                    can_delegate=False,
                    remaining_budget=0,
                    idempotency_key=f"token:registered-{field}",
                    recipient_instance=f"worker_{field}",
                )
                self.register_token(
                    token,
                    delegation_id=delegation_id,
                    run_id=run_id,
                    idempotency_key=f"dispatch:registered-{field}",
                    overrides={field: value},
                )
                with self.assertRaisesRegex(
                    fleet_delegation.DelegationError,
                    "registered delegation authority",
                ):
                    fleet_delegation.validate_bound_token(
                        self.runs,
                        self.mission_id,
                        token_id=token["token_id"],
                        delegation_id=delegation_id,
                        run_id=run_id,
                        require_v3=True,
                    )

    def test_token_store_rejects_ambiguous_and_noncanonical_json_bytes(self) -> None:
        token, _, _ = self.issue()
        path = Path(token["path"])
        original = path.read_bytes()
        ledger = fleet_mission_state.ledger_path(self.runs, self.mission_id)
        events_before = ledger.read_bytes()
        mutations = {
            "duplicate-key": original.replace(b"{", b'{"schema_version":2,', 1),
            "shadowed-nonfinite": original.replace(
                b"{", b'{"remaining_budget":NaN,', 1
            ),
            "trailing-whitespace": original + b" \t\n",
        }

        for label, content in mutations.items():
            with self.subTest(label=label):
                path.write_bytes(content)
                with self.assertRaisesRegex(
                    fleet_delegation.DelegationError,
                    "cannot load capability token|bytes are not canonical",
                ):
                    fleet_delegation.load_token(
                        self.runs, self.mission_id, token["token_id"]
                    )
                self.assertEqual(ledger.read_bytes(), events_before)
                path.write_bytes(original)

    def test_existing_token_reconciliation_requires_canonical_bytes(self) -> None:
        token, delegation_id, _ = self.issue(
            remaining_budget=2,
            idempotency_key="token:canonical-existing",
        )
        path = Path(token["path"])
        path.write_bytes(path.read_bytes() + b" ")
        ledger = fleet_mission_state.ledger_path(self.runs, self.mission_id)
        events_before = ledger.read_bytes()

        with self.assertRaisesRegex(
            fleet_delegation.DelegationError, "bytes are not canonical"
        ):
            fleet_delegation.issue_token(
                self.runs,
                self.mission_id,
                delegation_id=delegation_id,
                parent_run_id=self.lead_run_id,
                can_delegate=True,
                allowed_capabilities=["challenge"],
                allowed_artifact_ids=[],
                current_depth=0,
                max_depth=3,
                remaining_budget=2,
                writer_instance="builder",
                idempotency_key="token:canonical-existing:retry",
                run_id=token["run_id"],
                admission_id=token["admission_id"],
                reservation_event_sha256=token["reservation_event_sha256"],
                commit_id=token["commit_id"],
                request_digest=token["request_digest"],
            )
        self.assertEqual(ledger.read_bytes(), events_before)

    def test_schema_downgrade_with_v3_proof_fails_closed(self) -> None:
        token, _, _ = self.issue()
        path = Path(token["path"])
        value = json.loads(path.read_text())
        value["schema_version"] = 1
        value.pop("allowed_artifact_ids")
        value["token_sha256"] = fleet_mission_state.sha256(
            {key: item for key, item in value.items() if key != "token_sha256"}
        )
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError,
            "fields do not match schema_version=1",
        ):
            fleet_delegation.load_token(self.runs, self.mission_id, token["token_id"])

    def test_binding_requires_the_token_exact_delegation_and_run(self) -> None:
        token, delegation_id, child_run = self.issue()
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError, "delegation_id mismatch"
        ):
            fleet_delegation.bind_token(
                self.runs,
                self.mission_id,
                token_id=token["token_id"],
                delegation_id=str(uuid.uuid4()),
                run_id=child_run,
                idempotency_key="token:wrong-bind",
            )
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError,
            "run_id mismatch|exact delegation and run",
        ):
            fleet_delegation.validate_bound_token(
                self.runs,
                self.mission_id,
                token_id=token["token_id"],
                delegation_id=delegation_id,
                run_id=str(uuid.uuid4()),
            )

    def test_v3_token_cannot_launder_parent_budget_depth_or_workflow_scope(
        self,
    ) -> None:
        token, delegation_id, _ = self.issue(
            remaining_budget=2,
            idempotency_key="token:proof-binding",
        )
        ledger = fleet_mission_state.ledger_path(self.runs, self.mission_id)
        token_path = Path(token["path"])
        before_ledger = ledger.read_bytes()
        before_token = token_path.read_bytes()
        cases = (
            ({"parent_run_id": str(uuid.uuid4())}, "admission proof"),
            ({"remaining_budget": 3}, "admission proof"),
            ({"current_depth": 1}, "depth differs"),
            ({"max_depth": 4}, "depth differs"),
            ({"allowed_capabilities": ["outside_workflow"]}, "workflow"),
        )
        base = {
            "delegation_id": delegation_id,
            "parent_run_id": self.lead_run_id,
            "can_delegate": True,
            "allowed_capabilities": ["challenge"],
            "allowed_artifact_ids": token["allowed_artifact_ids"],
            "current_depth": token["current_depth"],
            "max_depth": token["max_depth"],
            "remaining_budget": token["remaining_budget"],
            "writer_instance": token["writer_instance"],
            "run_id": token["run_id"],
            "admission_id": token["admission_id"],
            "reservation_event_sha256": token["reservation_event_sha256"],
            "commit_id": token["commit_id"],
            "request_digest": token["request_digest"],
        }
        for index, (override, message) in enumerate(cases):
            with (
                self.subTest(override=override),
                self.assertRaisesRegex(
                    fleet_delegation.DelegationError,
                    message,
                ),
            ):
                fleet_delegation.issue_token(
                    self.runs,
                    self.mission_id,
                    **{**base, **override},
                    idempotency_key=f"token:proof-launder:{index}",
                )
            self.assertEqual(ledger.read_bytes(), before_ledger)
            self.assertEqual(token_path.read_bytes(), before_token)

    def test_bound_token_rejects_registered_parent_mismatch(self) -> None:
        token, delegation_id, child_run = self.issue()
        with self.assertRaisesRegex(
            fleet_mission_state.MissionConflict,
            "recipient differs from admission",
        ):
            fleet_mission_state.append_event(
                self.runs,
                self.mission_id,
                kind="delegation_registered",
                actor="CONTROL",
                idempotency_key="dispatch:wrong-parent",
                payload={
                    "delegation_id": delegation_id,
                    "mission_id": self.mission_id,
                    "run_id": child_run,
                    "parent_run_id": str(uuid.uuid4()),
                    "delegated_by": "lead",
                    "recipient_instance": "challenger",
                    "capability": "challenge",
                    "objective_sha256": "d" * 64,
                    "input_artifact_ids": [],
                    "expected_output_contract": {"type": "text"},
                    "deadline": "2099-01-01T00:00:00+00:00",
                    "provider": "test",
                    "model": "test",
                    "variant": None,
                    "depth": 1,
                    "token_id": token["token_id"],
                },
            )

    def test_issue_token_rejects_noncanonical_grants_before_writing(self) -> None:
        delegation_id = str(uuid.uuid4())
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError, "allowed_capabilities must be unique"
        ):
            fleet_delegation.issue_token(
                self.runs,
                self.mission_id,
                delegation_id=delegation_id,
                parent_run_id=self.lead_run_id,
                can_delegate=True,
                allowed_capabilities=["challenge", "challenge"],
                allowed_artifact_ids=[],
                current_depth=1,
                max_depth=3,
                remaining_budget=1,
                writer_instance="builder",
                idempotency_key="token:duplicate-grant",
            )
        token_id = str(
            uuid.uuid5(uuid.UUID(self.mission_id), f"capability-token:{delegation_id}")
        )
        self.assertFalse(
            fleet_delegation.token_path(self.runs, self.mission_id, token_id).exists()
        )
        self.assertFalse(
            any(
                event["kind"] == "capability_token_issued"
                and event["payload"].get("token_id") == token_id
                for event in self.events()
            )
        )

    def test_artifact_and_child_capability_scopes_are_exact(self) -> None:
        token, delegation_id, child_run = self.issue(
            allowed_capabilities=["challenge", "verify"],
            allowed_artifact_ids=[ARTIFACT_A, ARTIFACT_B],
            remaining_budget=4,
        )
        self.assertEqual(token["allowed_artifact_ids"], [ARTIFACT_A, ARTIFACT_B])
        fleet_delegation.validate_for_subdelegation(
            self.runs,
            self.mission_id,
            token_id=token["token_id"],
            delegation_id=delegation_id,
            parent_run_id=child_run,
            capability="verify",
            requested_budget=0,
            requested_delegation_id=str(uuid.uuid4()),
            requested_artifact_ids=[ARTIFACT_B],
        )
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError, "artifacts.*outside"
        ):
            fleet_delegation.validate_for_subdelegation(
                self.runs,
                self.mission_id,
                token_id=token["token_id"],
                delegation_id=delegation_id,
                parent_run_id=child_run,
                capability="verify",
                requested_budget=0,
                requested_delegation_id=str(uuid.uuid4()),
                requested_artifact_ids=[ARTIFACT_C],
            )
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError, "capability scope exceeds"
        ):
            fleet_delegation.validate_for_subdelegation(
                self.runs,
                self.mission_id,
                token_id=token["token_id"],
                delegation_id=delegation_id,
                parent_run_id=child_run,
                capability="verify",
                requested_budget=1,
                requested_delegation_id=str(uuid.uuid4()),
                child_can_delegate=True,
                child_allowed_capabilities=["build"],
            )
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError,
            "child_allowed_capabilities are not canonical",
        ):
            fleet_delegation.validate_for_subdelegation(
                self.runs,
                self.mission_id,
                token_id=token["token_id"],
                delegation_id=delegation_id,
                parent_run_id=child_run,
                capability="verify",
                requested_budget=1,
                requested_delegation_id=str(uuid.uuid4()),
                child_can_delegate=True,
                child_allowed_capabilities=["verify", "challenge"],
            )
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError,
            "requested_artifact_ids are not canonical",
        ):
            fleet_delegation.validate_for_subdelegation(
                self.runs,
                self.mission_id,
                token_id=token["token_id"],
                delegation_id=delegation_id,
                parent_run_id=child_run,
                capability="verify",
                requested_budget=0,
                requested_delegation_id=str(uuid.uuid4()),
                requested_artifact_ids=[ARTIFACT_B, ARTIFACT_A],
            )

    def test_child_issue_rejects_artifact_amplification_with_zero_effects(self) -> None:
        parent, _, parent_run = self.issue(
            allowed_artifact_ids=[ARTIFACT_A],
            idempotency_key="token:artifact-parent",
        )
        request = self.request(requested_artifact_ids=[ARTIFACT_B])
        reserved = self.reserve_children(
            parent,
            [request],
            idempotency_key="admission:artifact-amplification",
        )["admissions"][0]
        child_token_id = str(
            uuid.uuid5(
                uuid.UUID(self.mission_id),
                f"capability-token:{reserved['delegation_id']}",
            )
        )
        ledger = fleet_mission_state.ledger_path(self.runs, self.mission_id)
        before_ledger = ledger.read_bytes()
        token_store = fleet_delegation.token_root(self.runs, self.mission_id)
        before_files = {
            path.name: path.read_bytes()
            for path in token_store.iterdir()
            if path.is_file()
        }

        with self.assertRaisesRegex(
            fleet_delegation.DelegationError,
            "artifacts exceed parent grants and attested children",
        ):
            fleet_delegation.issue_token(
                self.runs,
                self.mission_id,
                delegation_id=reserved["delegation_id"],
                parent_run_id=parent_run,
                can_delegate=False,
                allowed_capabilities=[],
                allowed_artifact_ids=[ARTIFACT_B],
                current_depth=1,
                max_depth=3,
                remaining_budget=0,
                writer_instance="builder",
                idempotency_key="token:artifact-amplification",
                run_id=reserved["run_id"],
                admission_id=reserved["admission_id"],
                reservation_event_sha256=reserved["reservation_event_sha256"],
                commit_id=str(
                    uuid.uuid5(
                        uuid.UUID(self.mission_id),
                        f"commit:{reserved['admission_id']}",
                    )
                ),
                request_digest=reserved["request_digest"],
            )

        self.assertEqual(ledger.read_bytes(), before_ledger)
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in token_store.iterdir()
                if path.is_file()
            },
            before_files,
        )
        self.assertFalse(
            fleet_delegation.token_path(
                self.runs,
                self.mission_id,
                child_token_id,
            ).exists()
        )

    def test_child_issue_rejects_ungrounded_capability_with_zero_effects(self) -> None:
        parent, _, parent_run = self.issue(
            allowed_capabilities=["challenge"],
            idempotency_key="token:capability-parent",
        )
        request = self.request(capability="build")
        reserved = self.reserve_children(
            parent,
            [request],
            idempotency_key="admission:capability-amplification",
        )["admissions"][0]
        child_token_id = str(
            uuid.uuid5(
                uuid.UUID(self.mission_id),
                f"capability-token:{reserved['delegation_id']}",
            )
        )
        ledger = fleet_mission_state.ledger_path(self.runs, self.mission_id)
        before_ledger = ledger.read_bytes()
        token_store = fleet_delegation.token_root(self.runs, self.mission_id)
        before_files = {
            path.name: path.read_bytes()
            for path in token_store.iterdir()
            if path.is_file()
        }

        with self.assertRaisesRegex(
            fleet_delegation.DelegationError,
            "admission capability exceeds parent token",
        ):
            fleet_delegation.issue_token(
                self.runs,
                self.mission_id,
                delegation_id=reserved["delegation_id"],
                parent_run_id=parent_run,
                can_delegate=False,
                allowed_capabilities=[],
                allowed_artifact_ids=[],
                current_depth=1,
                max_depth=3,
                remaining_budget=0,
                writer_instance="builder",
                idempotency_key="token:capability-amplification",
                run_id=reserved["run_id"],
                admission_id=reserved["admission_id"],
                reservation_event_sha256=reserved["reservation_event_sha256"],
                commit_id=str(
                    uuid.uuid5(
                        uuid.UUID(self.mission_id),
                        f"commit:{reserved['admission_id']}",
                    )
                ),
                request_digest=reserved["request_digest"],
            )

        self.assertEqual(ledger.read_bytes(), before_ledger)
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in token_store.iterdir()
                if path.is_file()
            },
            before_files,
        )
        self.assertFalse(
            fleet_delegation.token_path(
                self.runs,
                self.mission_id,
                child_token_id,
            ).exists()
        )

    def test_authorized_artifacts_include_only_attested_direct_child_results(
        self,
    ) -> None:
        token, delegation_id, child_run = self.issue(allowed_artifact_ids=[ARTIFACT_A])
        child_request = self.request(requested_artifact_ids=[ARTIFACT_A])
        reserved = self.reserve_children(
            token,
            [child_request],
            idempotency_key="admission:direct-child",
        )["admissions"][0]
        direct_delegation_id = reserved["delegation_id"]
        direct_run_id = reserved["run_id"]
        child_token = fleet_delegation.issue_token(
            self.runs,
            self.mission_id,
            delegation_id=direct_delegation_id,
            parent_run_id=child_run,
            can_delegate=False,
            allowed_capabilities=[],
            allowed_artifact_ids=[ARTIFACT_A],
            current_depth=1,
            max_depth=3,
            remaining_budget=0,
            writer_instance="builder",
            idempotency_key="token:direct-child",
            run_id=direct_run_id,
            admission_id=reserved["admission_id"],
            reservation_event_sha256=reserved["reservation_event_sha256"],
            commit_id=str(
                uuid.uuid5(
                    uuid.UUID(self.mission_id),
                    f"commit:{reserved['admission_id']}",
                )
            ),
            request_digest=reserved["request_digest"],
        )
        fleet_delegation.bind_token(
            self.runs,
            self.mission_id,
            token_id=child_token["token_id"],
            delegation_id=direct_delegation_id,
            run_id=direct_run_id,
            idempotency_key="token:direct-child:bind",
        )
        committed = fleet_admission.commit(
            self.runs,
            self.mission_id,
            admission_id=reserved["admission_id"],
            request_digest=reserved["request_digest"],
            effect_sha256=reserved["effect_sha256"],
            recipient_instance=reserved["recipient_instance"],
            writer=False,
            run_id=direct_run_id,
            idempotency_key="admission:direct-child:commit",
        )
        authorized = fleet_admission.authorize_launch(
            self.runs,
            self.mission_id,
            admission_id=reserved["admission_id"],
            commit_event_sha256=committed["commit_event_sha256"],
            request_digest=reserved["request_digest"],
            effect_sha256=reserved["effect_sha256"],
            recipient_instance=reserved["recipient_instance"],
            writer=False,
            run_id=direct_run_id,
            idempotency_key="admission:direct-child:authorize",
        )
        fleet_admission.mark_started(
            self.runs,
            self.mission_id,
            admission_id=reserved["admission_id"],
            authorization_event_sha256=authorized["authorization_event_sha256"],
            request_digest=reserved["request_digest"],
            effect_sha256=reserved["effect_sha256"],
            recipient_instance=reserved["recipient_instance"],
            writer=False,
            run_id=direct_run_id,
            idempotency_key="admission:direct-child:start",
        )
        fleet_mission_state.append_event(
            self.runs,
            self.mission_id,
            kind="delegation_registered",
            actor="CONTROL",
            idempotency_key="dispatch:direct-child",
            payload={
                "delegation_id": direct_delegation_id,
                "mission_id": self.mission_id,
                "run_id": direct_run_id,
                "parent_run_id": child_run,
                "delegated_by": child_run,
                "recipient_instance": reserved["recipient_instance"],
                "capability": "challenge",
                "objective_sha256": "d" * 64,
                "input_artifact_ids": [ARTIFACT_A],
                "expected_output_contract": {},
                "deadline": "2030-01-01T00:00:00Z",
                "provider": "openai",
                "model": "test",
                "variant": None,
                "depth": 1,
                "token_id": child_token["token_id"],
            },
        )
        fleet_mission_state.append_event(
            self.runs,
            self.mission_id,
            kind="result_recorded",
            actor="CONTROL",
            idempotency_key="result:direct-child",
            payload={
                "run_id": direct_run_id,
                "delegation_id": direct_delegation_id,
                "artifact_id": ARTIFACT_C,
                "provider": "openai",
                "model": "test",
                "variant": None,
            },
        )
        self.assertEqual(
            fleet_delegation.authorized_artifact_ids(
                self.runs,
                self.mission_id,
                token_id=token["token_id"],
                delegation_id=delegation_id,
                run_id=child_run,
            ),
            [ARTIFACT_A, ARTIFACT_C],
        )
        granted_request = self.request(requested_artifact_ids=[ARTIFACT_C])
        granted = self.reserve_children(
            token,
            [granted_request],
            idempotency_key="admission:attested-child-grant",
        )["admissions"][0]
        granted_token = fleet_delegation.issue_token(
            self.runs,
            self.mission_id,
            delegation_id=granted["delegation_id"],
            parent_run_id=child_run,
            can_delegate=False,
            allowed_capabilities=[],
            allowed_artifact_ids=[ARTIFACT_C],
            current_depth=1,
            max_depth=3,
            remaining_budget=0,
            writer_instance="builder",
            idempotency_key="token:attested-child-grant",
            run_id=granted["run_id"],
            admission_id=granted["admission_id"],
            reservation_event_sha256=granted["reservation_event_sha256"],
            commit_id=str(
                uuid.uuid5(
                    uuid.UUID(self.mission_id),
                    f"commit:{granted['admission_id']}",
                )
            ),
            request_digest=granted["request_digest"],
        )
        self.assertEqual(granted_token["allowed_artifact_ids"], [ARTIFACT_C])

    def test_batch_budget_is_atomic_non_amplifiable_and_single_retries_reuse_it(
        self,
    ) -> None:
        token, delegation_id, child_run = self.issue(remaining_budget=4)
        leaf = self.request()
        branch = self.request(
            requested_budget=2,
            child_can_delegate=True,
            child_allowed_capabilities=["challenge"],
        )
        fleet_delegation.validate_for_subdelegations(
            self.runs,
            self.mission_id,
            token_id=token["token_id"],
            delegation_id=delegation_id,
            parent_run_id=child_run,
            requests=[branch, leaf],
        )
        reserved = self.reserve_children(
            token,
            [branch, leaf],
            idempotency_key="admission:child-batch",
        )
        retried = self.reserve_children(
            token,
            [branch, leaf],
            idempotency_key="admission:child-batch",
        )
        self.assertFalse(retried["appended"])
        allocations = [
            event
            for event in self.events()
            if event["kind"] == fleet_delegation.BUDGET_EVENT_KIND
        ]
        self.assertEqual(allocations, [])
        self.assertEqual(
            sorted(item["credit_cost"] for item in reserved["admissions"]),
            [1, 3],
        )
        with self.assertRaisesRegex(
            fleet_mission_state.MissionConflict,
            "parent.*budget|delegated budget|credits",
        ):
            self.reserve_children(
                token,
                [self.request()],
                idempotency_key="admission:child-overflow",
            )

    def test_insufficient_batch_writes_no_partial_budget_allocation(self) -> None:
        token, delegation_id, child_run = self.issue(remaining_budget=3)
        requests = [
            self.request(),
            self.request(
                requested_budget=2,
                child_can_delegate=True,
                child_allowed_capabilities=["challenge"],
            ),
        ]
        fleet_delegation.validate_for_subdelegations(
            self.runs,
            self.mission_id,
            token_id=token["token_id"],
            delegation_id=delegation_id,
            parent_run_id=child_run,
            requests=requests,
        )
        ledger = fleet_mission_state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()
        with self.assertRaisesRegex(
            fleet_mission_state.MissionConflict,
            "parent.*budget|delegated budget|credits",
        ):
            self.reserve_children(
                token,
                requests,
                idempotency_key="admission:insufficient-batch",
            )
        self.assertEqual(ledger.read_bytes(), before)
        self.assertFalse(
            any(
                event["kind"] == fleet_delegation.BUDGET_EVENT_KIND
                for event in self.events()
            )
        )

    def test_concurrent_budget_reservations_cannot_overspend(self) -> None:
        token, _, _ = self.issue(remaining_budget=1)
        barrier = threading.Barrier(2)

        def reserve(child_id: str) -> str:
            barrier.wait()
            try:
                self.reserve_children(
                    token,
                    [self.request(requested_delegation_id=child_id)],
                    idempotency_key=f"admission:concurrent:{child_id}",
                )
            except fleet_mission_state.MissionConflict:
                return "rejected"
            return "reserved"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(reserve, [str(uuid.uuid4()), str(uuid.uuid4())])
            )
        self.assertEqual(sorted(outcomes), ["rejected", "reserved"])

    def test_frozen_policy_rejects_direct_legacy_token_mint(self) -> None:
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError,
            "schema_version=3 token proof",
        ):
            fleet_delegation.issue_token(
                self.runs,
                self.mission_id,
                delegation_id=str(uuid.uuid4()),
                parent_run_id=self.lead_run_id,
                can_delegate=True,
                allowed_capabilities=["challenge"],
                allowed_artifact_ids=[],
                current_depth=0,
                max_depth=3,
                remaining_budget=1,
                writer_instance="builder",
                idempotency_key="token:legacy-after-freeze",
            )

    def test_historical_v2_is_readable_and_finishable_but_cannot_amplify(self) -> None:
        legacy_runs = self.tmp / "legacy-runs"
        legacy_runs.mkdir(mode=0o700)
        legacy_mission = str(uuid.uuid4())
        legacy_target = self.tmp / "legacy-target"
        legacy_target.mkdir()
        workflow_digest = "d" * 64
        fleet_mission_state.append_event(
            legacy_runs,
            legacy_mission,
            kind="mission_created",
            actor="CONTROL",
            idempotency_key="legacy:create",
            payload={
                "feature": "legacy",
                "objective_sha256": "a" * 64,
                "target_repo": str(legacy_target.resolve()),
                "base_sha": "b" * 40,
                "workflow_digest": workflow_digest,
                "initial_risk": "low",
            },
        )
        fleet_mission_state.append_event(
            legacy_runs,
            legacy_mission,
            kind="workflow_compiled",
            actor="CONTROL",
            idempotency_key="legacy:compiled",
            payload={"compiled_digest": "e" * 64},
        )
        delegation_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        parent_run_id = str(uuid.uuid4())
        token = fleet_delegation.issue_token(
            legacy_runs,
            legacy_mission,
            delegation_id=delegation_id,
            parent_run_id=parent_run_id,
            can_delegate=True,
            allowed_capabilities=["challenge"],
            allowed_artifact_ids=[ARTIFACT_A],
            current_depth=0,
            max_depth=3,
            remaining_budget=2,
            writer_instance="builder",
            idempotency_key="legacy:token",
        )
        self.assertEqual(token["schema_version"], 2)
        fleet_delegation.bind_token(
            legacy_runs,
            legacy_mission,
            token_id=token["token_id"],
            delegation_id=delegation_id,
            run_id=run_id,
            idempotency_key="legacy:bind",
        )
        fleet_mission_state.append_event(
            legacy_runs,
            legacy_mission,
            kind="delegation_registered",
            actor="CONTROL",
            idempotency_key="legacy:dispatch",
            payload={
                "delegation_id": delegation_id,
                "mission_id": legacy_mission,
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "delegated_by": "lead",
                "recipient_instance": "legacy_worker",
                "capability": "challenge",
                "objective_sha256": "f" * 64,
                "input_artifact_ids": [ARTIFACT_A],
                "expected_output_contract": {},
                "deadline": "2099-01-01T00:00:00Z",
                "provider": "legacy",
                "model": "legacy",
                "variant": None,
                "depth": 0,
                "token_id": token["token_id"],
            },
        )
        child_delegation = str(uuid.uuid4())
        child_run = str(uuid.uuid4())
        fleet_mission_state.append_event(
            legacy_runs,
            legacy_mission,
            kind="delegation_registered",
            actor="CONTROL",
            idempotency_key="legacy:child",
            payload={
                "delegation_id": child_delegation,
                "mission_id": legacy_mission,
                "run_id": child_run,
                "parent_run_id": run_id,
                "delegated_by": run_id,
                "recipient_instance": "legacy_child",
                "capability": "challenge",
                "objective_sha256": "1" * 64,
                "input_artifact_ids": [],
                "expected_output_contract": {},
                "deadline": "2099-01-01T00:00:00Z",
                "provider": "legacy",
                "model": "legacy",
                "variant": None,
                "depth": 1,
                "token_id": str(uuid.uuid4()),
            },
        )
        fleet_mission_state.append_event(
            legacy_runs,
            legacy_mission,
            kind="result_recorded",
            actor="CONTROL",
            idempotency_key="legacy:child-result",
            payload={
                "run_id": child_run,
                "delegation_id": child_delegation,
                "artifact_id": ARTIFACT_C,
                "provider": "legacy",
                "model": "legacy",
                "variant": None,
            },
        )
        fleet_mission_state.append_event(
            legacy_runs,
            legacy_mission,
            kind="result_recorded",
            actor="CONTROL",
            idempotency_key="legacy:result",
            payload={
                "run_id": run_id,
                "delegation_id": delegation_id,
                "artifact_id": ARTIFACT_B,
                "provider": "legacy",
                "model": "legacy",
                "variant": None,
            },
        )
        self.assertEqual(
            fleet_delegation.authorized_artifact_ids(
                legacy_runs,
                legacy_mission,
                token_id=token["token_id"],
                delegation_id=delegation_id,
                run_id=run_id,
            ),
            [ARTIFACT_A],
        )
        unbound_delegation_id = str(uuid.uuid4())
        unbound = fleet_delegation.issue_token(
            legacy_runs,
            legacy_mission,
            delegation_id=unbound_delegation_id,
            parent_run_id=parent_run_id,
            can_delegate=False,
            allowed_capabilities=[],
            allowed_artifact_ids=[],
            current_depth=0,
            max_depth=3,
            remaining_budget=0,
            writer_instance="builder",
            idempotency_key="legacy:unbound-token",
        )
        fleet_admission.freeze_policy(
            legacy_runs,
            legacy_mission,
            workflow_digest=workflow_digest,
            compiled_digest="e" * 64,
            deadline_at="2099-01-01T00:00:00Z",
            delegation_credits=8,
            max_active_delegations=4,
            idempotency_key="legacy:policy-freeze",
        )
        self.assertEqual(
            fleet_delegation.validate_bound_token(
                legacy_runs,
                legacy_mission,
                token_id=token["token_id"],
                delegation_id=delegation_id,
                run_id=run_id,
            )["schema_version"],
            2,
        )
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError,
            "read-only after policy freeze",
        ):
            fleet_delegation.authorized_artifact_ids(
                legacy_runs,
                legacy_mission,
                token_id=token["token_id"],
                delegation_id=delegation_id,
                run_id=run_id,
            )
        ledger = fleet_mission_state.ledger_path(legacy_runs, legacy_mission)
        before_bind = ledger.read_bytes()
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError,
            "legacy token binding read-only",
        ):
            fleet_delegation.bind_token(
                legacy_runs,
                legacy_mission,
                token_id=unbound["token_id"],
                delegation_id=unbound_delegation_id,
                run_id=str(uuid.uuid4()),
                idempotency_key="legacy:bind-after-freeze",
            )
        self.assertEqual(ledger.read_bytes(), before_bind)
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError,
            "schema_version=3",
        ):
            fleet_delegation.validate_for_subdelegation(
                legacy_runs,
                legacy_mission,
                token_id=token["token_id"],
                delegation_id=delegation_id,
                parent_run_id=run_id,
                capability="challenge",
                requested_budget=0,
                requested_delegation_id=str(uuid.uuid4()),
            )

    def test_token_publication_is_linearizable_and_crash_recoverable(
        self,
    ) -> None:
        request_key = "concurrent-token"
        reserved = fleet_admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=[
                {
                    "request_key": request_key,
                    "run_kind": "specialist",
                    "recipient_instance": "token_worker",
                    "capability": "challenge",
                    "effect_sha256": fleet_mission_state.sha256(
                        {"kind": "concurrent-token-fixture"}
                    ),
                    "task_sha256": fleet_mission_state.sha256(
                        {"kind": "concurrent-token-task"}
                    ),
                    "parent_admission_id": fleet_mission_state.derive_state(
                        self.events()
                    )["lead_admission_id"],
                    "parent_run_id": self.lead_run_id,
                    "delegated_budget": 2,
                    "writer": False,
                }
            ],
            idempotency_key="admission:concurrent-token",
        )["admissions"][0]
        delegation_id = reserved["delegation_id"]
        barrier = threading.Barrier(2)

        def issue_one(index: int) -> dict:
            barrier.wait()
            return fleet_delegation.issue_token(
                self.runs,
                self.mission_id,
                delegation_id=delegation_id,
                parent_run_id=self.lead_run_id,
                can_delegate=True,
                allowed_capabilities=["challenge"],
                allowed_artifact_ids=[ARTIFACT_A],
                current_depth=0,
                max_depth=3,
                remaining_budget=2,
                writer_instance="builder",
                idempotency_key=f"token:concurrent:{index}",
                run_id=reserved["run_id"],
                admission_id=reserved["admission_id"],
                reservation_event_sha256=reserved["reservation_event_sha256"],
                commit_id=str(
                    uuid.uuid5(
                        uuid.UUID(self.mission_id),
                        f"commit:{reserved['admission_id']}",
                    )
                ),
                request_digest=reserved["request_digest"],
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            tokens = list(executor.map(issue_one, [1, 2]))
        self.assertEqual(tokens[0]["token_sha256"], tokens[1]["token_sha256"])
        stored = json.loads(Path(tokens[0]["path"]).read_text())
        self.assertEqual(stored["token_sha256"], tokens[0]["token_sha256"])
        issued = [
            event
            for event in self.events()
            if event["kind"] == "capability_token_issued"
            and event["payload"]["token_id"] == tokens[0]["token_id"]
        ]
        self.assertEqual(len(issued), 1)
        self.assertEqual(issued[0]["payload"]["token_sha256"], stored["token_sha256"])

        def start_specialist(label: str) -> tuple[dict, dict]:
            task_sha256 = fleet_mission_state.sha256(
                {"kind": "authority-race-task", "label": label}
            )
            reserved = fleet_admission.reserve_many(
                self.runs,
                self.mission_id,
                requests=[
                    {
                        "request_key": f"authority-{label}",
                        "run_kind": "specialist",
                        "recipient_instance": f"authority_{label}",
                        "capability": "challenge",
                        "effect_sha256": fleet_mission_state.sha256(
                            {"kind": "authority-race-effect", "label": label}
                        ),
                        "task_sha256": task_sha256,
                        "parent_admission_id": fleet_mission_state.derive_state(
                            self.events()
                        )["lead_admission_id"],
                        "parent_run_id": self.lead_run_id,
                        "delegated_budget": 0,
                        "writer": False,
                    }
                ],
                idempotency_key=f"race:reserve:{label}",
            )["admissions"][0]
            committed = fleet_admission.commit(
                self.runs,
                self.mission_id,
                admission_id=reserved["admission_id"],
                request_digest=reserved["request_digest"],
                effect_sha256=reserved["effect_sha256"],
                recipient_instance=reserved["recipient_instance"],
                writer=False,
                run_id=reserved["run_id"],
                idempotency_key=f"race:commit:{label}",
            )
            authorized = fleet_admission.authorize_launch(
                self.runs,
                self.mission_id,
                admission_id=reserved["admission_id"],
                commit_event_sha256=committed["commit_event_sha256"],
                request_digest=reserved["request_digest"],
                effect_sha256=reserved["effect_sha256"],
                recipient_instance=reserved["recipient_instance"],
                writer=False,
                run_id=reserved["run_id"],
                idempotency_key=f"race:authorize:{label}",
            )
            fleet_admission.mark_started(
                self.runs,
                self.mission_id,
                admission_id=reserved["admission_id"],
                authorization_event_sha256=authorized["authorization_event_sha256"],
                request_digest=reserved["request_digest"],
                effect_sha256=reserved["effect_sha256"],
                recipient_instance=reserved["recipient_instance"],
                writer=False,
                run_id=reserved["run_id"],
                idempotency_key=f"race:start:{label}",
            )
            issue_arguments = {
                "delegation_id": reserved["delegation_id"],
                "parent_run_id": self.lead_run_id,
                "can_delegate": False,
                "allowed_capabilities": [],
                "allowed_artifact_ids": [],
                "current_depth": 0,
                "max_depth": 3,
                "remaining_budget": 0,
                "writer_instance": "builder",
                "idempotency_key": f"race:token:{label}",
                "run_id": reserved["run_id"],
                "admission_id": reserved["admission_id"],
                "reservation_event_sha256": reserved["reservation_event_sha256"],
                "commit_id": committed["commit_id"],
                "request_digest": reserved["request_digest"],
            }
            return reserved, issue_arguments

        def assert_terminal_race(
            *,
            authority_operation: str,
            authority_arguments: dict,
            terminal_operation: str,
            admission: dict,
        ) -> None:
            context = multiprocessing.get_context("spawn")
            validated = context.Barrier(2)
            release = context.Event()
            terminal_started = context.Event()
            terminal_finished = context.Event()
            results = context.Queue()
            authority = context.Process(
                target=_authority_publication_worker,
                args=(
                    authority_operation,
                    str(self.runs),
                    self.mission_id,
                    authority_arguments,
                    admission["admission_id"],
                    validated,
                    release,
                    results,
                ),
            )
            terminal = context.Process(
                target=_authority_terminal_worker,
                args=(
                    terminal_operation,
                    str(self.runs),
                    self.mission_id,
                    admission,
                    terminal_started,
                    terminal_finished,
                    results,
                ),
            )
            processes = (authority, terminal)
            try:
                authority.start()
                validated.wait(timeout=20)
                terminal.start()
                self.assertTrue(terminal_started.wait(timeout=20))
                self.assertFalse(
                    terminal_finished.wait(timeout=0.5),
                    "terminal transition bypassed authority publication lock",
                )
                release.set()
                rows = [results.get(timeout=30) for _ in processes]
                for process in processes:
                    process.join(timeout=30)
                    self.assertFalse(
                        process.is_alive(), "authority publication race process hung"
                    )
                    self.assertEqual(process.exitcode, 0)
            finally:
                release.set()
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=5)
                results.close()
                results.join_thread()
            self.assertEqual(
                sorted((row[0], row[1]) for row in rows),
                sorted(
                    (
                        (authority_operation, "ok"),
                        (terminal_operation, "ok"),
                    )
                ),
                rows,
            )

        issue_admission, issue_arguments = start_specialist("issue-finalize")
        assert_terminal_race(
            authority_operation="issue",
            authority_arguments=issue_arguments,
            terminal_operation="finalize",
            admission=issue_admission,
        )
        issue_token_id = str(
            uuid.uuid5(
                uuid.UUID(self.mission_id),
                f"capability-token:{issue_admission['delegation_id']}",
            )
        )
        issue_events = self.events()
        issue_index = next(
            index
            for index, event in enumerate(issue_events)
            if event["kind"] == "capability_token_issued"
            and event["payload"].get("token_id") == issue_token_id
        )
        finalize_index = next(
            index
            for index, event in enumerate(issue_events)
            if event["kind"] == "delegation_finalized"
            and event["payload"].get("admission_id") == issue_admission["admission_id"]
        )
        self.assertLess(issue_index, finalize_index)

        bind_admission, bind_issue_arguments = start_specialist("bind-cancel")
        bind_token = fleet_delegation.issue_token(
            self.runs, self.mission_id, **bind_issue_arguments
        )
        assert_terminal_race(
            authority_operation="bind",
            authority_arguments={
                "token_id": bind_token["token_id"],
                "delegation_id": bind_admission["delegation_id"],
                "run_id": bind_admission["run_id"],
                "idempotency_key": "race:bind:bind-cancel",
            },
            terminal_operation="cancel",
            admission=bind_admission,
        )
        bind_events = self.events()
        bind_index = next(
            index
            for index, event in enumerate(bind_events)
            if event["kind"] == "capability_token_bound"
            and event["payload"].get("token_id") == bind_token["token_id"]
        )
        cancel_index = next(
            index
            for index, event in enumerate(bind_events)
            if event["kind"] == "run_cancel_requested"
            and event["payload"].get("run_id") == bind_admission["run_id"]
        )
        self.assertLess(bind_index, cancel_index)

        crash_admission, crash_arguments = start_specialist("file-crash")
        crash_token_id = str(
            uuid.uuid5(
                uuid.UUID(self.mission_id),
                f"capability-token:{crash_admission['delegation_id']}",
            )
        )
        crash_code = """
from pathlib import Path
import json
import sys
import fleet_delegation
fleet_delegation.issue_token(
    Path(sys.argv[1]), sys.argv[2], **json.loads(sys.argv[3])
)
"""
        crashed = subprocess.run(
            [
                sys.executable,
                "-c",
                crash_code,
                str(self.runs),
                self.mission_id,
                json.dumps(crash_arguments),
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "scripts"),
                "FLEET_TEST_DELEGATION_CRASH_AT": "after_token_publish",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(crashed.returncode, 137, crashed.stderr)
        crash_path = fleet_delegation.token_path(
            self.runs, self.mission_id, crash_token_id
        )
        self.assertTrue(crash_path.is_file())
        self.assertFalse(
            any(
                event["kind"] == "capability_token_issued"
                and event["payload"].get("token_id") == crash_token_id
                for event in self.events()
            )
        )
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError, "not bound to the mission ledger"
        ):
            fleet_delegation.load_token(self.runs, self.mission_id, crash_token_id)
        recovered = fleet_delegation.issue_token(
            self.runs, self.mission_id, **crash_arguments
        )
        recovered_events = [
            event
            for event in self.events()
            if event["kind"] == "capability_token_issued"
            and event["payload"].get("token_id") == crash_token_id
        ]
        self.assertEqual(len(recovered_events), 1)
        self.assertEqual(
            recovered_events[0]["payload"]["token_sha256"],
            recovered["token_sha256"],
        )
        self.assertEqual(
            json.loads(crash_path.read_text())["token_sha256"],
            recovered["token_sha256"],
        )

    def test_token_store_and_locks_reject_symlinked_ancestor(self) -> None:
        token, _, _ = self.issue()
        token_root = fleet_delegation.token_root(self.runs, self.mission_id)
        original = token_root.with_name("capability-tokens-original")
        token_root.rename(original)
        outside = self.tmp / "outside-token-store"
        outside.mkdir(mode=0o700)
        token_root.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(
            fleet_delegation.DelegationError, "unsafe capability token store"
        ):
            fleet_delegation.load_token(self.runs, self.mission_id, token["token_id"])
        with self.assertRaisesRegex(
            fleet_delegation.DelegationError, "unsafe capability token store"
        ):
            fleet_delegation.issue_token(
                self.runs,
                self.mission_id,
                delegation_id=token["delegation_id"],
                parent_run_id=self.lead_run_id,
                can_delegate=True,
                allowed_capabilities=["challenge"],
                allowed_artifact_ids=token["allowed_artifact_ids"],
                current_depth=token["current_depth"],
                max_depth=3,
                remaining_budget=token["remaining_budget"],
                writer_instance="builder",
                idempotency_key="token:symlink-ancestor",
                run_id=token["run_id"],
                admission_id=token["admission_id"],
                reservation_event_sha256=token["reservation_event_sha256"],
                commit_id=token["commit_id"],
                request_digest=token["request_digest"],
            )
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
