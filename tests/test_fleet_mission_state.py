from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
admission = importlib.import_module("fleet_admission")
mission = importlib.import_module("fleet_mission")
state = importlib.import_module("fleet_mission_state")
workflow = importlib.import_module("workflow_config")


def _append_concurrent_mission_event(arguments: tuple[str, str, int]) -> int:
    runs_dir, mission_id, index = arguments
    event, appended = state.append_event(
        Path(runs_dir),
        mission_id,
        kind="human_approval_requested",
        actor="CONTROL",
        idempotency_key=f"race:event:{index}",
        payload={"reason": f"race {index}", "scope": "local"},
    )
    if not appended:
        raise AssertionError("unique race event was not appended")
    return int(event["sequence"])


def _append_while_quiesced(arguments: tuple[str, str, str]) -> str:
    runs_dir, mission_id, key = arguments
    try:
        state.append_event(
            Path(runs_dir),
            mission_id,
            kind="human_approval_requested",
            actor="CONTROL",
            idempotency_key=key,
            payload={"reason": "cross-process mutation gate", "scope": "local"},
        )
    except state.MissionConflict as exc:
        return f"conflict:{exc}"
    return "appended"


class MissionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs = self.tmp / "runs"
        self.target = self.tmp / "target"
        self.target.mkdir()
        self.compiled = workflow.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )
        self.mission_id, created = mission.create_mission(
            self.runs,
            compiled=self.compiled,
            feature="demo",
            objective="implement durable state",
            target_repo=self.target.resolve(),
            base_sha="a" * 40,
            idempotency_key="create:demo",
        )
        self.assertTrue(created)

    def create_again(
        self, *, objective: str = "implement durable state"
    ) -> tuple[str, bool]:
        return mission.create_mission(
            self.runs,
            compiled=copy.deepcopy(self.compiled),
            feature="demo",
            objective=objective,
            target_repo=self.target.resolve(),
            base_sha="a" * 40,
            idempotency_key="create:demo",
        )

    def start_specialist(
        self, request_key: str, recipient: str, capability: str
    ) -> dict[str, object]:
        reserved = admission.reserve_many(
            self.runs,
            self.mission_id,
            requests=[
                {
                    "request_key": request_key,
                    "run_kind": "specialist",
                    "recipient_instance": recipient,
                    "capability": capability,
                    "effect_sha256": state.sha256(
                        {
                            "request_key": request_key,
                            "recipient": recipient,
                            "capability": capability,
                        }
                    ),
                    "task_sha256": state.sha256(
                        {
                            "task": request_key,
                            "recipient": recipient,
                            "capability": capability,
                        }
                    ),
                    "parent_admission_id": None,
                    "parent_run_id": None,
                    "delegated_budget": 0,
                    "writer": False,
                }
            ],
            idempotency_key=f"admission:{request_key}",
        )["admissions"][0]
        committed = admission.commit(
            self.runs,
            self.mission_id,
            admission_id=reserved["admission_id"],
            request_digest=reserved["request_digest"],
            effect_sha256=reserved["effect_sha256"],
            recipient_instance=recipient,
            writer=False,
            run_id=reserved["run_id"],
            idempotency_key=f"admission:commit:{request_key}",
        )
        current = mission.load_state(self.runs, self.mission_id)
        if current["status"] == "compiled":
            state.append_events(
                self.runs,
                self.mission_id,
                [
                    {
                        "kind": "fleet_boot_started",
                        "actor": "CONTROL",
                        "idempotency_key": "test:fleet:boot",
                        "payload": {"feature": current["feature"]},
                    },
                    {
                        "kind": "mission_running",
                        "actor": "CONTROL",
                        "idempotency_key": "test:fleet:running",
                        "payload": {"manifest": str(self.runs / "test-fleet.manifest")},
                    },
                ],
            )
        authorized = admission.authorize_launch(
            self.runs,
            self.mission_id,
            admission_id=reserved["admission_id"],
            commit_event_sha256=committed["commit_event_sha256"],
            request_digest=reserved["request_digest"],
            effect_sha256=reserved["effect_sha256"],
            recipient_instance=recipient,
            writer=False,
            run_id=reserved["run_id"],
            idempotency_key=f"admission:authorize:{request_key}",
        )
        admission.mark_started(
            self.runs,
            self.mission_id,
            admission_id=reserved["admission_id"],
            authorization_event_sha256=authorized["authorization_event_sha256"],
            request_digest=reserved["request_digest"],
            effect_sha256=reserved["effect_sha256"],
            recipient_instance=recipient,
            writer=False,
            run_id=reserved["run_id"],
            idempotency_key=f"admission:start:{request_key}",
        )
        return reserved

    def approve_assurance(
        self, *, expires_at: str, key_prefix: str
    ) -> dict[str, object]:
        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_escalated",
            actor="CONTROL",
            idempotency_key=f"{key_prefix}:risk",
            payload={
                "from": "low",
                "to": "high",
                "categories": ["production"],
                "reason": "exercise approval renewal",
            },
        )
        request, _ = state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_requested",
            actor="CONTROL",
            idempotency_key=f"{key_prefix}:request",
            payload={
                "risk": "high",
                "categories": ["production"],
                "scope": str(self.target.resolve()),
                "workflow_digest": self.compiled["workflow_digest"],
            },
        )
        payload = {
            "approval_id": str(uuid.uuid4()),
            "request_event_sha256": request["event_sha256"],
            "workflow_digest": self.compiled["workflow_digest"],
            "scope": str(self.target.resolve()),
            "risk": "high",
            "expires_at": expires_at,
            "approved_by_sha256": "d" * 64,
            "decision": "approved",
        }
        expired = state.parse_timestamp(
            expires_at, "test approval expiry"
        ) <= datetime.now(timezone.utc)
        if not expired:
            payload["expires_in_seconds"] = 600
        context = (
            mock.patch.object(
                state, "_require_current_authority_append_schema", return_value=None
            )
            if expired
            else nullcontext()
        )
        with context:
            approval, _ = state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_approved",
                actor="HUMAN",
                idempotency_key=f"{key_prefix}:approval",
                payload=payload,
            )
        return approval

    def renewal_payload(
        self,
        prior: dict[str, object],
        *,
        idempotency_key: str,
        expires_in_seconds: int = 600,
    ) -> dict[str, object]:
        return {
            "approval_id": str(
                uuid.uuid5(
                    uuid.UUID(self.mission_id),
                    f"approval-renewal:{prior['event_sha256']}:{idempotency_key}",
                )
            ),
            "prior_approval_event_sha256": prior["event_sha256"],
            "request_event_sha256": prior["payload"]["request_event_sha256"],
            "workflow_digest": self.compiled["workflow_digest"],
            "scope": str(self.target.resolve()),
            "risk": "high",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds // 2)
            ).isoformat(),
            "expires_in_seconds": expires_in_seconds,
            "approved_by_sha256": "e" * 64,
            "decision": "approved",
        }

    def test_create_is_durable_and_idempotent_across_restart(self) -> None:
        second, created = self.create_again()
        self.assertEqual(second, self.mission_id)
        self.assertFalse(created)
        reloaded = mission.load_state(self.runs, self.mission_id)
        self.assertEqual(reloaded["status"], "compiled")
        self.assertEqual(reloaded["last_sequence"], 3)

    def test_durable_compiled_loader_binds_bytes_path_and_ledger(self) -> None:
        compiled, current = mission.load_mission_compiled(
            self.runs, self.mission_id, mode="effect"
        )
        self.assertEqual(compiled, self.compiled)
        self.assertEqual(current["compiled_digest"], self.compiled["compiled_digest"])

        path = self.runs / "missions" / self.mission_id / "compiled-workflow.json"
        foreign = workflow.compile_path(ROOT / "workflows" / "hotfix.yaml")
        path.write_bytes(state.canonical_bytes(foreign) + b"\n")
        path.chmod(0o600)
        with self.assertRaisesRegex(
            mission.MissionError, "differs from mission ledger"
        ):
            mission.load_mission_compiled(self.runs, self.mission_id, mode="effect")

    def test_durable_compiled_loader_rejects_noncanonical_bytes_and_hardlinks(
        self,
    ) -> None:
        path = self.runs / "missions" / self.mission_id / "compiled-workflow.json"
        path.write_text(json.dumps(self.compiled, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaisesRegex(mission.MissionError, "bytes are not canonical"):
            mission.load_mission_compiled(self.runs, self.mission_id, mode="effect")

        path.write_bytes(state.canonical_bytes(self.compiled) + b"\n")
        outside = self.tmp / "compiled-hardlink.json"
        os.link(path, outside)
        with (
            mock.patch.object(mission.fleet_compiled, "loads") as parse,
            self.assertRaisesRegex(mission.MissionError, "unexpected link count"),
        ):
            mission.load_mission_compiled(self.runs, self.mission_id, mode="effect")
        parse.assert_not_called()

    def test_durable_compiled_loader_rejects_ancestor_substitution_before_parse(
        self,
    ) -> None:
        external = self.tmp / "external-missions"
        shutil.copytree(self.runs / "missions", external)
        external_bytes = (
            external / self.mission_id / "compiled-workflow.json"
        ).read_bytes()
        held = self.tmp / "held-missions"
        (self.runs / "missions").rename(held)
        (self.runs / "missions").symlink_to(external, target_is_directory=True)

        with (
            mock.patch.object(mission.fleet_compiled, "loads") as parse,
            self.assertRaisesRegex(
                mission.MissionError, "cannot open rooted directory"
            ),
        ):
            mission.load_mission_compiled(self.runs, self.mission_id, mode="effect")
        parse.assert_not_called()
        self.assertEqual(
            (external / self.mission_id / "compiled-workflow.json").read_bytes(),
            external_bytes,
        )

    def test_durable_compiled_loader_rejects_root_replacement_before_parse(
        self,
    ) -> None:
        external = self.tmp / "replacement-runs"
        shutil.copytree(self.runs, external)
        held = self.tmp / "held-runs"
        original_read = mission.fleet_safe_paths.RootedFS.read_regular

        def replace_root(rooted: object, relative: object, **kwargs: object) -> bytes:
            self.runs.rename(held)
            self.runs.symlink_to(external, target_is_directory=True)
            return original_read(rooted, relative, **kwargs)

        with (
            mock.patch.object(
                mission.fleet_safe_paths.RootedFS,
                "read_regular",
                autospec=True,
                side_effect=replace_root,
            ),
            mock.patch.object(mission.fleet_compiled, "loads") as parse,
            self.assertRaisesRegex(
                mission.MissionError, "trusted root binding changed"
            ),
        ):
            mission.load_mission_compiled(self.runs, self.mission_id, mode="effect")
        parse.assert_not_called()

    def test_durable_compiled_loader_rejects_invalid_mode_before_filesystem(
        self,
    ) -> None:
        with (
            mock.patch.object(mission.fleet_safe_paths, "RootedFS") as rooted,
            self.assertRaisesRegex(mission.MissionError, "load mode"),
        ):
            mission.load_mission_compiled(
                self.runs,
                self.mission_id,
                mode="execute",  # type: ignore[arg-type]
            )
        rooted.assert_not_called()

    def test_mission_feature_traversal_is_rejected_before_state_or_files(self) -> None:
        attacked_runs = self.tmp / "feature-attack-runs"
        attacked_runs.mkdir(mode=0o700)
        attacked_mission_id = str(uuid.uuid4())
        payload = {
            "feature": "x/../../escaped",
            "objective_sha256": "a" * 64,
            "target_repo": str(self.target.resolve()),
            "base_sha": "b" * 40,
            "workflow_digest": "c" * 64,
            "initial_risk": "low",
        }
        before = tuple(attacked_runs.rglob("*"))

        with self.assertRaisesRegex(state.MissionStateError, "canonical component"):
            state.append_event(
                attacked_runs,
                attacked_mission_id,
                kind="mission_created",
                actor="CONTROL",
                idempotency_key="create:traversal",
                payload=payload,
            )

        self.assertEqual(tuple(attacked_runs.rglob("*")), before)
        self.assertFalse((self.tmp / "escaped").exists())

        valid_events = state.read_events(
            state.ledger_path(self.runs, self.mission_id),
            expected_mission_id=self.mission_id,
        )
        forged = copy.deepcopy(valid_events[0])
        forged["payload"]["feature"] = payload["feature"]
        with self.assertRaisesRegex(state.MissionStateError, "canonical component"):
            state.derive_state([forged])

    def test_create_rejects_intermediate_length_git_hashes(self) -> None:
        for length in range(41, 64):
            with self.subTest(length=length):
                with self.assertRaisesRegex(
                    mission.MissionError, "base_sha must be a full Git object id"
                ):
                    mission.create_mission(
                        self.runs,
                        compiled=self.compiled,
                        feature=f"invalid-sha-{length}",
                        objective="reject an ambiguous Git object identifier",
                        target_repo=self.target.resolve(),
                        base_sha="a" * length,
                        idempotency_key=f"create:invalid-sha-{length}",
                    )

    def test_mission_created_rejects_noncanonical_target_and_nonfull_oid(self) -> None:
        valid_payload = {
            "feature": "invalid-binding",
            "objective_sha256": "a" * 64,
            "target_repo": str(self.target.resolve()),
            "base_sha": "b" * 40,
            "workflow_digest": "c" * 64,
            "initial_risk": "low",
        }
        invalid_bindings = (
            ("target_repo", "relative/target", "canonical absolute path"),
            (
                "target_repo",
                f"{self.target.resolve()}/../target",
                "canonical absolute path",
            ),
            ("base_sha", "d" * 39, "full Git object id"),
            ("base_sha", "d" * 41, "full Git object id"),
            ("base_sha", "d" * 63, "full Git object id"),
            ("base_sha", "d" * 65, "full Git object id"),
        )
        for index, (field, value, expected) in enumerate(invalid_bindings):
            with self.subTest(field=field, value=value):
                attacked_runs = self.tmp / f"invalid-binding-{index}"
                attacked_runs.mkdir(mode=0o700)
                payload = {**valid_payload, field: value}
                with self.assertRaisesRegex(state.MissionStateError, expected):
                    state.append_event(
                        attacked_runs,
                        str(uuid.uuid4()),
                        kind="mission_created",
                        actor="CONTROL",
                        idempotency_key=f"create:invalid-binding:{index}",
                        payload=payload,
                    )
                self.assertEqual(list(attacked_runs.iterdir()), [])

        noncanonical_runs = self.tmp / "noncanonical-create"
        with self.assertRaisesRegex(mission.MissionError, "canonical absolute path"):
            mission.create_mission(
                noncanonical_runs,
                compiled=self.compiled,
                feature="noncanonical-create",
                objective="reject before durable mission state",
                target_repo=self.target / ".." / "target",
                base_sha="d" * 40,
                idempotency_key="create:noncanonical-target",
            )
        self.assertFalse(noncanonical_runs.exists())

    def test_create_recovers_kill_between_files_and_first_append(self) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        ledger.unlink()
        recovered, created = self.create_again()
        self.assertEqual(recovered, self.mission_id)
        self.assertFalse(created)
        self.assertEqual(mission.load_state(self.runs, recovered)["status"], "compiled")

    def test_atomic_write_rejects_broken_symlink_target(self) -> None:
        path = self.tmp / "private" / "state.json"
        path.parent.mkdir(mode=0o700)
        missing = self.tmp / "missing.json"
        path.symlink_to(missing)
        with self.assertRaisesRegex(state.MissionStateError, "refusing symlink"):
            state.atomic_write(path, b"{}\n")
        self.assertTrue(path.is_symlink())
        self.assertFalse(missing.exists())

    def test_mission_creation_and_ledger_reject_symlinked_ancestors(self) -> None:
        attacked_runs = self.tmp / "attacked-runs"
        attacked_runs.mkdir(mode=0o700)
        outside = self.tmp / "outside-missions"
        outside.mkdir(mode=0o700)
        (attacked_runs / "missions").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(mission.MissionError, "unsafe mission store"):
            mission.create_mission(
                attacked_runs,
                compiled=self.compiled,
                feature="attacked",
                objective="must not escape",
                target_repo=self.target.resolve(),
                base_sha="a" * 40,
                idempotency_key="create:attacked",
            )
        self.assertEqual(list(outside.iterdir()), [])

        mission_root = state.mission_root(self.runs, self.mission_id)
        moved = self.tmp / "moved-mission"
        mission_root.rename(moved)
        mission_root.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(
            state.MissionStateError, "unsafe mission ledger path"
        ):
            mission.load_state(self.runs, self.mission_id)
        with self.assertRaisesRegex(
            state.MissionStateError, "unsafe mission ledger path"
        ):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="risk_escalated",
                actor="CONTROL",
                idempotency_key="risk:symlink",
                payload={
                    "from": "medium",
                    "to": "high",
                    "categories": ["security"],
                    "reason": "must not follow",
                },
            )

    def test_create_key_with_different_payload_conflicts(self) -> None:
        with self.assertRaisesRegex(state.MissionConflict, "conflicts"):
            self.create_again(objective="different objective")

    def test_retry_same_key_does_not_duplicate_and_payload_drift_fails(self) -> None:
        event, appended = state.append_event(
            self.runs,
            self.mission_id,
            kind="fleet_boot_started",
            actor="CONTROL",
            idempotency_key="boot:1",
            payload={"feature": "demo"},
        )
        repeated, appended_again = state.append_event(
            self.runs,
            self.mission_id,
            kind="fleet_boot_started",
            actor="CONTROL",
            idempotency_key="boot:1",
            payload={"feature": "demo"},
        )
        self.assertTrue(appended)
        self.assertFalse(appended_again)
        self.assertEqual(repeated, event)
        with self.assertRaisesRegex(state.MissionConflict, "another mission request"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="fleet_boot_started",
                actor="CONTROL",
                idempotency_key="boot:1",
                payload={"feature": "other"},
            )

    def test_canonical_json_rejects_nonfinite_numbers_without_mutation(self) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "non-finite JSON number"):
                    state.canonical_bytes({"unsafe": value})
                with self.assertRaisesRegex(
                    ValueError,
                    "Out of range float values|non-finite JSON number",
                ):
                    state.append_event(
                        self.runs,
                        self.mission_id,
                        kind="fleet_boot_started",
                        actor="CONTROL",
                        idempotency_key=f"nonfinite:{repr(value)}",
                        payload={"feature": "demo", "unsafe": value},
                    )
                self.assertEqual(ledger.read_bytes(), before)

    def test_ledger_parser_rejects_duplicate_keys_and_nonfinite_numbers(self) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        original = ledger.read_text(encoding="utf-8")
        first, rest = original.split("\n", 1)
        duplicate = first.replace('{"actor":', '{"actor":"CONTROL","actor":', 1)
        for poisoned in (
            duplicate + "\n" + rest,
            first.replace('"schema_version":1', '"schema_version":NaN', 1)
            + "\n"
            + rest,
        ):
            with self.subTest(poisoned=poisoned[:80]):
                ledger.write_text(poisoned, encoding="utf-8")
                with self.assertRaisesRegex(
                    state.MissionStateError, "invalid mission JSON"
                ):
                    state.read_events(ledger, expected_mission_id=self.mission_id)
        ledger.write_text(original, encoding="utf-8")

    def test_ledger_strict_bytes_reject_ambiguous_encodings_without_mutation(
        self,
    ) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        original = ledger.read_bytes()
        first, rest = original.split(b"\n", 1)
        mutations = {
            "bom": b"\xef\xbb\xbf" + original,
            "invalid-utf8": original.replace(
                b'"actor":"CONTROL"', b'"actor":"\xff"', 1
            ),
            "surrogate": original.replace(
                b'"actor":"CONTROL"', b'"actor":"\\ud800"', 1
            ),
            "overflow-number": original.replace(
                b'"sequence":1', b'"sequence":1e999', 1
            ),
            "trailing-space": first + b" \n" + rest,
            "crlf": original.replace(b"\n", b"\r\n", 1),
            "blank-record": first + b"\n\n" + rest,
        }
        for label, poisoned in mutations.items():
            with self.subTest(label=label):
                ledger.write_bytes(poisoned)
                before = ledger.read_bytes()
                with self.assertRaisesRegex(
                    state.MissionStateError,
                    "invalid mission JSON|not canonical|partial",
                ):
                    state.read_events(ledger, expected_mission_id=self.mission_id)
                self.assertEqual(ledger.read_bytes(), before)
        ledger.write_bytes(original)

    def test_cli_payload_uses_the_same_strict_json_contract(self) -> None:
        for raw in (
            '{"feature":"demo","feature":"shadow"}',
            '{"feature":NaN}',
            '{"feature":1e999}',
            '\ufeff{"feature":"demo"}',
            '{"feature":"\\ud800"}',
        ):
            with (
                self.subTest(raw=raw),
                self.assertRaisesRegex(
                    mission.MissionError,
                    "invalid --payload-json",
                ),
            ):
                mission._payload(raw)

    def test_frozen_policy_rejects_legacy_budget_events_without_mutation(self) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()
        with self.assertRaisesRegex(
            state.MissionConflict,
            "legacy delegation budget allocation is disabled",
        ):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="delegation_budget_allocated",
                actor="CONTROL",
                idempotency_key="legacy:budget:after-freeze",
                payload={
                    "token_id": str(uuid.uuid4()),
                    "delegation_id": str(uuid.uuid4()),
                    "parent_run_id": str(uuid.uuid4()),
                    "allocations": [
                        {
                            "requested_delegation_id": str(uuid.uuid4()),
                            "capability": "recon",
                            "child_can_delegate": False,
                            "child_allowed_capabilities": [],
                            "requested_artifact_ids": [],
                            "edge_cost": 1,
                            "delegated_budget": 0,
                            "total_cost": 1,
                        }
                    ],
                },
            )
        self.assertEqual(ledger.read_bytes(), before)

    def test_risk_is_monotonic_and_invalid_event_is_not_persisted(self) -> None:
        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_escalated",
            actor="lead",
            idempotency_key="risk:1",
            payload={
                "from": "low",
                "to": "high",
                "categories": ["production"],
                "reason": "deploy",
            },
        )
        current = mission.load_state(self.runs, self.mission_id)
        self.assertEqual(current["risk"], "high")
        before = current["last_sequence"]
        with self.assertRaisesRegex(state.MissionStateError, "cannot decrease"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="risk_escalated",
                actor="lead",
                idempotency_key="risk:2",
                payload={
                    "from": "high",
                    "to": "low",
                    "categories": [],
                    "reason": "retry",
                },
            )
        self.assertEqual(
            mission.load_state(self.runs, self.mission_id)["last_sequence"], before
        )

    def test_lineage_and_artifact_identity_are_derived(self) -> None:
        first = self.start_specialist("lineage-first", "scout", "recon")
        delegation_id = str(first["delegation_id"])
        run_id = str(first["run_id"])
        artifact = state.artifact_id("exact result")
        state.append_event(
            self.runs,
            self.mission_id,
            kind="delegation_registered",
            actor="lead",
            idempotency_key="delegation:1",
            payload={
                "delegation_id": delegation_id,
                "mission_id": self.mission_id,
                "run_id": run_id,
                "parent_run_id": None,
                "delegated_by": "lead",
                "recipient_instance": "scout",
                "capability": "recon",
                "objective_sha256": state.artifact_id("inspect"),
                "input_artifact_ids": [],
                "expected_output_contract": {"type": "text"},
                "deadline": "2026-07-14T01:00:00Z",
                "provider": "openai",
                "model": "gpt-test",
                "variant": None,
                "depth": 0,
                "token_id": None,
            },
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="result_recorded",
            actor="CONTROL",
            idempotency_key="result:1",
            payload={
                "run_id": run_id,
                "delegation_id": delegation_id,
                "artifact_id": artifact,
                "provider": "openai",
                "model": "gpt-test",
                "variant": None,
            },
        )
        second = self.start_specialist("lineage-second", "challenger", "challenge")
        second_delegation = str(second["delegation_id"])
        second_run = str(second["run_id"])
        state.append_event(
            self.runs,
            self.mission_id,
            kind="delegation_registered",
            actor="lead",
            idempotency_key="delegation:2",
            payload={
                "delegation_id": second_delegation,
                "mission_id": self.mission_id,
                "run_id": second_run,
                "parent_run_id": None,
                "delegated_by": "lead",
                "recipient_instance": "challenger",
                "capability": "challenge",
                "objective_sha256": state.artifact_id("challenge"),
                "input_artifact_ids": [],
                "expected_output_contract": {"type": "text"},
                "deadline": "2026-07-14T01:00:00Z",
                "provider": "anthropic",
                "model": "claude-test",
                "variant": None,
                "depth": 0,
                "token_id": None,
            },
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="result_recorded",
            actor="CONTROL",
            idempotency_key="result:2",
            payload={
                "run_id": second_run,
                "delegation_id": second_delegation,
                "artifact_id": artifact,
                "provider": "anthropic",
                "model": "claude-test",
                "variant": None,
            },
        )
        current = mission.load_state(self.runs, self.mission_id)
        self.assertEqual(current["results"][delegation_id]["run_id"], run_id)
        self.assertEqual(current["results"][second_delegation]["run_id"], second_run)

    def test_first_terminal_is_immutable_and_success_requires_archive(self) -> None:
        before = mission.load_state(self.runs, self.mission_id)["last_sequence"]
        with self.assertRaisesRegex(state.MissionStateError, "requires archived"):
            state.append_terminal(
                self.runs,
                self.mission_id,
                status="succeeded",
                reason="too early",
                idempotency_key="terminal:early",
            )
        self.assertEqual(
            mission.load_state(self.runs, self.mission_id)["last_sequence"], before
        )
        state.append_terminal(
            self.runs,
            self.mission_id,
            status="failed",
            reason="expected fixture",
            idempotency_key="terminal:1",
        )
        with self.assertRaisesRegex(state.MissionConflict, "immutable"):
            state.append_terminal(
                self.runs,
                self.mission_id,
                status="blocked",
                reason="second",
                idempotency_key="terminal:2",
            )

    def test_tampering_and_partial_append_break_verification(self) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        original = ledger.read_text(encoding="utf-8")
        ledger.write_text(
            original.replace('"initial_risk":"low"', '"initial_risk":"high"'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(state.MissionStateError, "hash mismatch"):
            state.read_events(ledger, expected_mission_id=self.mission_id)
        ledger.write_text(original + '{"partial":', encoding="utf-8")
        with self.assertRaisesRegex(state.MissionStateError, "partial"):
            state.read_events(ledger, expected_mission_id=self.mission_id)

    def test_resume_plan_survives_new_process(self) -> None:
        script = ROOT / "scripts" / "fleet_mission.py"
        result = subprocess.run(
            [
                "python3",
                str(script),
                "--runs-dir",
                str(self.runs),
                "resume-plan",
                "--mission-id",
                self.mission_id,
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["next_action"], "boot")

    def test_scoped_approval_drives_assured_boot_transitions(self) -> None:
        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_escalated",
            actor="CONTROL",
            idempotency_key="approval:risk",
            payload={
                "from": "low",
                "to": "high",
                "categories": ["production"],
                "reason": "test assurance",
            },
        )
        request, _ = state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_requested",
            actor="CONTROL",
            idempotency_key="approval:request",
            payload={
                "risk": "high",
                "categories": ["production"],
                "scope": str(self.target.resolve()),
                "workflow_digest": self.compiled["workflow_digest"],
            },
        )
        approval_payload = {
            "approval_id": str(uuid.uuid4()),
            "request_event_sha256": request["event_sha256"],
            "workflow_digest": self.compiled["workflow_digest"],
            "scope": str(self.target.resolve()),
            "risk": "high",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=600)
            ).isoformat(),
            "expires_in_seconds": 600,
            "approved_by_sha256": "c" * 64,
            "decision": "approved",
        }
        ledger = state.ledger_path(self.runs, self.mission_id)
        before_control_approval = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionStateError, "declared duration"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_approved",
                actor="HUMAN",
                idempotency_key="approval:overlong",
                payload={
                    **approval_payload,
                    "approval_id": str(uuid.uuid4()),
                    "expires_at": (
                        datetime.now(timezone.utc) + timedelta(seconds=601)
                    ).isoformat(),
                },
            )
        with self.assertRaisesRegex(state.MissionStateError, "outside policy"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_approved",
                actor="HUMAN",
                idempotency_key="approval:short-policy",
                payload={
                    **approval_payload,
                    "approval_id": str(uuid.uuid4()),
                    "expires_in_seconds": 59,
                },
            )
        self.assertEqual(ledger.read_bytes(), before_control_approval)
        with self.assertRaisesRegex(state.MissionConflict, "HUMAN actor"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_approved",
                actor="CONTROL",
                idempotency_key="approval:control-bypass",
                payload=approval_payload,
            )
        self.assertEqual(ledger.read_bytes(), before_control_approval)
        approval, _ = state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_approved",
            actor="HUMAN",
            idempotency_key="approval:decision",
            payload=approval_payload,
        )
        self.assertEqual(
            state.resume_plan(mission.load_state(self.runs, self.mission_id))[
                "next_action"
            ],
            "boot_assured",
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_boot_started",
            actor="CONTROL",
            idempotency_key="approval:boot",
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
            idempotency_key="approval:started",
            payload={
                "manifest": str(self.runs / "fleet-demo.manifest"),
                "approval_event_sha256": approval["event_sha256"],
            },
        )
        self.assertEqual(
            mission.load_state(self.runs, self.mission_id)["status"], "assured_running"
        )

    def test_assurance_request_types_and_expired_boot_fail_closed(self) -> None:
        before = mission.load_state(self.runs, self.mission_id)["last_sequence"]
        with self.assertRaisesRegex(state.MissionStateError, "categories"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_requested",
                actor="CONTROL",
                idempotency_key="bad:categories",
                payload={
                    "risk": "high",
                    "categories": [{}],
                    "scope": str(self.target.resolve()),
                    "workflow_digest": self.compiled["workflow_digest"],
                },
            )
        self.assertEqual(
            mission.load_state(self.runs, self.mission_id)["last_sequence"], before
        )

        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_escalated",
            actor="CONTROL",
            idempotency_key="expired:risk",
            payload={
                "from": "low",
                "to": "high",
                "categories": ["production"],
                "reason": "exercise expiry",
            },
        )
        request, _ = state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_requested",
            actor="CONTROL",
            idempotency_key="expired:request",
            payload={
                "risk": "high",
                "categories": ["production"],
                "scope": str(self.target.resolve()),
                "workflow_digest": self.compiled["workflow_digest"],
            },
        )
        with mock.patch.object(
            state, "_require_current_authority_append_schema", return_value=None
        ):
            approval, _ = state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_approved",
                actor="HUMAN",
                idempotency_key="expired:approval",
                payload={
                    "approval_id": str(uuid.uuid4()),
                    "request_event_sha256": request["event_sha256"],
                    "workflow_digest": self.compiled["workflow_digest"],
                    "scope": str(self.target.resolve()),
                    "risk": "high",
                    "expires_at": "2000-01-01T00:00:00Z",
                    "approved_by_sha256": "d" * 64,
                    "decision": "approved",
                },
            )
        with self.assertRaisesRegex(state.MissionStateError, "expired before boot"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_boot_started",
                actor="CONTROL",
                idempotency_key="expired:boot",
                payload={
                    "preset": "fleet_dialogue",
                    "approval_event_sha256": approval["event_sha256"],
                },
            )

    def test_assurance_start_revalidates_approval_expiry_after_timely_boot(
        self,
    ) -> None:
        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_escalated",
            actor="CONTROL",
            idempotency_key="start-expiry:risk",
            payload={
                "from": "low",
                "to": "high",
                "categories": ["production"],
                "reason": "exercise start expiry",
            },
        )
        request, _ = state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_requested",
            actor="CONTROL",
            idempotency_key="start-expiry:request",
            payload={
                "risk": "high",
                "categories": ["production"],
                "scope": str(self.target.resolve()),
                "workflow_digest": self.compiled["workflow_digest"],
            },
        )
        approved_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        expires = approved_at + timedelta(seconds=60)
        with mock.patch.object(
            state, "_next_timestamp", return_value=approved_at.isoformat()
        ):
            approval, _ = state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_approved",
                actor="HUMAN",
                idempotency_key="start-expiry:approval",
                payload={
                    "approval_id": str(uuid.uuid4()),
                    "request_event_sha256": request["event_sha256"],
                    "workflow_digest": self.compiled["workflow_digest"],
                    "scope": str(self.target.resolve()),
                    "risk": "high",
                    "expires_at": expires.isoformat(),
                    "expires_in_seconds": 60,
                    "approved_by_sha256": "d" * 64,
                    "decision": "approved",
                },
            )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_boot_started",
            actor="CONTROL",
            idempotency_key="start-expiry:boot",
            payload={
                "preset": "fleet_dialogue",
                "approval_event_sha256": approval["event_sha256"],
            },
        )
        ledger = state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()
        with (
            mock.patch.object(
                state,
                "_next_timestamp",
                return_value=(expires + timedelta(seconds=1)).isoformat(),
            ),
            self.assertRaisesRegex(state.MissionStateError, "expired before start"),
        ):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_started",
                actor="CONTROL",
                idempotency_key="start-expiry:started",
                payload={
                    "manifest": str(self.runs / "fleet-demo.manifest"),
                    "approval_event_sha256": approval["event_sha256"],
                },
            )
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(
            mission.load_state(self.runs, self.mission_id)["status"],
            "assured_booting",
        )

    def test_expired_approval_renews_idempotently_then_allows_boot(self) -> None:
        approval = self.approve_assurance(
            expires_at="2000-01-01T00:00:00Z",
            key_prefix="renew-expired",
        )
        before = mission.load_state(self.runs, self.mission_id)
        ledger = state.ledger_path(self.runs, self.mission_id)
        before_spoof = ledger.read_bytes()
        spoof_key = "renew-expired:duration-spoof"
        with self.assertRaisesRegex(state.MissionStateError, "declared duration"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_approval_renewed",
                actor="HUMAN",
                idempotency_key=spoof_key,
                payload={
                    **self.renewal_payload(
                        approval,
                        idempotency_key=spoof_key,
                        expires_in_seconds=60,
                    ),
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )
        self.assertEqual(ledger.read_bytes(), before_spoof)
        key = "renew-expired:renewal"
        payload = self.renewal_payload(approval, idempotency_key=key)
        renewed, appended = state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_approval_renewed",
            actor="HUMAN",
            idempotency_key=key,
            payload=payload,
        )
        repeated, appended_again = state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_approval_renewed",
            actor="HUMAN",
            idempotency_key=key,
            payload=payload,
        )
        current = mission.load_state(self.runs, self.mission_id)
        self.assertTrue(appended)
        self.assertFalse(appended_again)
        self.assertEqual(repeated, renewed)
        self.assertEqual(current["status"], "assurance_approved")
        self.assertEqual(current["approval"]["event_sha256"], renewed["event_sha256"])
        self.assertEqual(current["admissions"], before["admissions"])
        self.assertEqual(current["admission_policy"], before["admission_policy"])

        for actor in ("CONTROL", "ASSURED"):
            with (
                self.subTest(actor=actor),
                self.assertRaisesRegex(state.MissionConflict, "no longer accepts"),
            ):
                admission.reserve_many(
                    self.runs,
                    self.mission_id,
                    requests=[
                        {
                            "request_key": f"renewal-bypass-{actor.lower()}",
                            "run_kind": "specialist",
                            "recipient_instance": f"renewal-{actor.lower()}",
                            "capability": "verification",
                            "effect_sha256": "8" * 64,
                            "task_sha256": "9" * 64,
                            "parent_admission_id": None,
                            "parent_run_id": None,
                            "delegated_budget": 0,
                            "writer": False,
                        }
                    ],
                    idempotency_key=f"renewal:bypass:{actor.lower()}",
                    actor=actor,
                )

        stable = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "another mission request"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_approval_renewed",
                actor="HUMAN",
                idempotency_key=key,
                payload={**payload, "expires_in_seconds": 601},
            )
        self.assertEqual(ledger.read_bytes(), stable)

        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_boot_started",
            actor="CONTROL",
            idempotency_key="renew-expired:boot",
            payload={
                "preset": "fleet_dialogue",
                "approval_event_sha256": renewed["event_sha256"],
            },
        )
        self.assertEqual(
            mission.load_state(self.runs, self.mission_id)["status"],
            "assured_booting",
        )

    def test_approval_renewal_rejects_live_prior_and_post_boot(self) -> None:
        approval = self.approve_assurance(
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            key_prefix="renew-live",
        )
        ledger = state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionConflict, "before expiry"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_approval_renewed",
                actor="HUMAN",
                idempotency_key="renew-live:too-early",
                payload=self.renewal_payload(
                    approval, idempotency_key="renew-live:too-early"
                ),
            )
        self.assertEqual(ledger.read_bytes(), before)

        state.append_event(
            self.runs,
            self.mission_id,
            kind="assurance_boot_started",
            actor="CONTROL",
            idempotency_key="renew-live:boot",
            payload={
                "preset": "fleet_dialogue",
                "approval_event_sha256": approval["event_sha256"],
            },
        )
        after_boot = ledger.read_bytes()
        with self.assertRaisesRegex(
            state.MissionStateError, "approved or assured-running"
        ):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_approval_renewed",
                actor="HUMAN",
                idempotency_key="renew-live:after-boot",
                payload=self.renewal_payload(
                    approval, idempotency_key="renew-live:after-boot"
                ),
            )
        self.assertEqual(ledger.read_bytes(), after_boot)

    def test_boot_rejects_risk_drift_after_approval(self) -> None:
        approval = self.approve_assurance(
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            key_prefix="approval-risk-drift",
        )
        state.append_event(
            self.runs,
            self.mission_id,
            kind="risk_escalated",
            actor="CONTROL",
            idempotency_key="approval-risk-drift:unknown",
            payload={
                "from": "high",
                "to": "unknown",
                "categories": ["unknown"],
                "reason": "new uncertainty",
            },
        )
        ledger = state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()
        with self.assertRaisesRegex(state.MissionStateError, "mission authority"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="assurance_boot_started",
                actor="CONTROL",
                idempotency_key="approval-risk-drift:boot",
                payload={
                    "preset": "fleet_dialogue",
                    "approval_event_sha256": approval["event_sha256"],
                },
            )
        self.assertEqual(ledger.read_bytes(), before)

    def test_closed_event_registry_rejects_unknown_extra_and_missing_without_mutation(
        self,
    ) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()
        invalid = (
            ("future_unregistered_kind", {}),
            ("workflow_compiled", {}),
            ("workflow_compiled", {"compiled_digest": "a" * 64, "extra": True}),
        )
        for index, (kind, payload) in enumerate(invalid):
            with self.subTest(kind=kind, payload=payload):
                with self.assertRaisesRegex(
                    state.MissionStateError, "unsupported|fields do not match"
                ):
                    state.append_event(
                        self.runs,
                        self.mission_id,
                        kind=kind,
                        actor="CONTROL",
                        idempotency_key=f"closed-registry:{index}",
                        payload=payload,
                    )
                self.assertEqual(ledger.read_bytes(), before)

        pristine = self.tmp / "pristine-runs"
        with self.assertRaisesRegex(state.MissionStateError, "unsupported"):
            state.append_event(
                pristine,
                str(uuid.uuid4()),
                kind="future_unregistered_kind",
                actor="CONTROL",
                idempotency_key="closed-registry:pristine",
                payload={},
            )
        self.assertFalse(pristine.exists())

    def test_event_envelope_and_uuid_payloads_require_exact_canonical_types(
        self,
    ) -> None:
        events = state.read_events(
            state.ledger_path(self.runs, self.mission_id),
            expected_mission_id=self.mission_id,
        )
        original = events[0]
        poisoned_values = (
            ("schema_version", 1.0, "schema_version"),
            ("sequence", 1.0, "sequence"),
            ("actor", 7, "actor"),
            ("idempotency_key", True, "idempotency"),
        )
        for field, value, expected in poisoned_values:
            with self.subTest(field=field, value=value):
                poisoned = copy.deepcopy(original)
                poisoned[field] = value
                unsigned = {
                    key: item for key, item in poisoned.items() if key != "event_sha256"
                }
                poisoned["event_sha256"] = state.sha256(unsigned)
                with self.assertRaisesRegex(state.MissionStateError, expected):
                    state._events_from_bytes(
                        state.canonical_bytes(poisoned) + b"\n",
                        expected_mission_id=self.mission_id,
                        require_nonempty=True,
                    )

        durable = self.start_specialist("uuid-canonical", "uuid-worker", "recon")
        delegation_id = str(durable["delegation_id"])
        run_id = str(durable["run_id"])
        state.append_event(
            self.runs,
            self.mission_id,
            kind="delegation_registered",
            actor="lead",
            idempotency_key="uuid:canonical:first",
            payload={
                "delegation_id": delegation_id,
                "mission_id": self.mission_id,
                "run_id": run_id,
                "parent_run_id": None,
                "delegated_by": "lead",
                "recipient_instance": "uuid-worker",
                "capability": "recon",
                "objective_sha256": "1" * 64,
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
        before = state.ledger_path(self.runs, self.mission_id).read_bytes()
        with self.assertRaisesRegex(state.MissionStateError, "not canonical"):
            state.append_event(
                self.runs,
                self.mission_id,
                kind="delegation_registered",
                actor="lead",
                idempotency_key="uuid:uppercase:bypass",
                payload={
                    "delegation_id": delegation_id.upper(),
                    "mission_id": self.mission_id,
                    "run_id": run_id.upper(),
                    "parent_run_id": None,
                    "delegated_by": "lead",
                    "recipient_instance": "uuid-worker-two",
                    "capability": "recon",
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
        self.assertEqual(
            state.ledger_path(self.runs, self.mission_id).read_bytes(), before
        )

    def test_transaction_batch_is_all_or_nothing(self) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        before = ledger.read_bytes()
        requests = [
            {
                "kind": "human_approval_requested",
                "actor": "CONTROL",
                "idempotency_key": "transaction:valid-first",
                "payload": {"reason": "must not leak", "scope": "local"},
            },
            {
                "kind": "risk_escalated",
                "actor": "CONTROL",
                "idempotency_key": "transaction:invalid-second",
                "payload": {
                    "from": "high",
                    "to": "low",
                    "categories": [],
                    "reason": "invalid transition",
                },
            },
        ]
        with state.MissionTransaction(self.runs, self.mission_id) as transaction:
            with self.assertRaisesRegex(state.MissionStateError, "does not start"):
                transaction.append_events(requests)
        self.assertEqual(ledger.read_bytes(), before)

        requests[1] = {
            "kind": "human_approval_requested",
            "actor": "CONTROL",
            "idempotency_key": "transaction:valid-second",
            "payload": {"reason": "atomic pair", "scope": "local"},
        }
        appended = state.append_events(self.runs, self.mission_id, requests)
        self.assertEqual([item[1] for item in appended], [True, True])
        events = state.read_events(ledger, expected_mission_id=self.mission_id)
        self.assertEqual(
            [event["idempotency_key"] for event in events[-2:]],
            ["transaction:valid-first", "transaction:valid-second"],
        )

    def test_transaction_returns_copies_that_cannot_poison_its_snapshot(self) -> None:
        with state.MissionTransaction(self.runs, self.mission_id) as transaction:
            first, _ = transaction.append_event(
                kind="risk_escalated",
                actor="CONTROL",
                idempotency_key="transaction:copy:first",
                payload={
                    "from": "low",
                    "to": "medium",
                    "categories": ["local"],
                    "reason": "first transition",
                },
            )
            first["payload"]["to"] = "high"
            second, appended = transaction.append_event(
                kind="risk_escalated",
                actor="CONTROL",
                idempotency_key="transaction:copy:second",
                payload={
                    "from": "medium",
                    "to": "high",
                    "categories": ["production"],
                    "reason": "second transition",
                },
            )
            self.assertTrue(appended)
            second["payload"]["from"] = "low"

        reloaded = mission.load_state(self.runs, self.mission_id)
        self.assertEqual(reloaded["risk"], "high")
        events = state.read_events(
            state.ledger_path(self.runs, self.mission_id),
            expected_mission_id=self.mission_id,
        )
        self.assertEqual(events[-2]["payload"]["to"], "medium")
        self.assertEqual(events[-1]["payload"]["from"], "medium")

    def test_transaction_rejects_mission_directory_substitution_without_mutation(
        self,
    ) -> None:
        mission_root = state.mission_root(self.runs, self.mission_id)
        moved_root = self.tmp / "pinned-mission"
        original = (mission_root / "mission.jsonl").read_bytes()

        with state.MissionTransaction(self.runs, self.mission_id) as transaction:
            mission_root.rename(moved_root)
            mission_root.mkdir(mode=0o700)
            replacement_ledger = mission_root / "mission.jsonl"
            replacement_ledger.write_bytes(original)
            replacement_ledger.chmod(0o600)
            moved_before = (moved_root / "mission.jsonl").read_bytes()
            replacement_before = replacement_ledger.read_bytes()

            with self.assertRaisesRegex(
                state.MissionStateError, "pinned mission directory path changed"
            ):
                transaction.append_event(
                    kind="human_approval_requested",
                    actor="CONTROL",
                    idempotency_key="transaction:directory-substitution",
                    payload={"reason": "must stay pinned", "scope": "local"},
                )

            self.assertEqual((moved_root / "mission.jsonl").read_bytes(), moved_before)
            self.assertEqual(replacement_ledger.read_bytes(), replacement_before)

    def test_timestamps_remain_strict_when_wall_clock_moves_backwards(self) -> None:
        class ReversedClock(datetime):
            @classmethod
            def now(cls, tz: timezone | None = None) -> ReversedClock:
                value = cls(2000, 1, 1, tzinfo=timezone.utc)
                return value if tz is not None else value.replace(tzinfo=None)

        with mock.patch.object(state, "datetime", ReversedClock):
            first, _ = state.append_event(
                self.runs,
                self.mission_id,
                kind="human_approval_requested",
                actor="CONTROL",
                idempotency_key="clock:one",
                payload={"reason": "clock one", "scope": "local"},
            )
            second, _ = state.append_event(
                self.runs,
                self.mission_id,
                kind="human_approval_requested",
                actor="CONTROL",
                idempotency_key="clock:two",
                payload={"reason": "clock two", "scope": "local"},
            )
        first_time = datetime.fromisoformat(first["timestamp"].replace("Z", "+00:00"))
        second_time = datetime.fromisoformat(second["timestamp"].replace("Z", "+00:00"))
        self.assertGreater(second_time, first_time)
        self.assertEqual((second_time - first_time).total_seconds(), 0.000001)

    def test_multiprocess_appends_have_one_chain_and_no_lost_events(self) -> None:
        context = multiprocessing.get_context("spawn")
        arguments = [(str(self.runs), self.mission_id, index) for index in range(12)]
        with context.Pool(processes=6) as pool:
            sequences = pool.map(_append_concurrent_mission_event, arguments)
        self.assertEqual(len(set(sequences)), len(arguments))
        events = state.read_events(
            state.ledger_path(self.runs, self.mission_id),
            expected_mission_id=self.mission_id,
        )
        state.verify_events(events)
        race_events = [
            event
            for event in events
            if event["idempotency_key"].startswith("race:event:")
        ]
        self.assertEqual(len(race_events), len(arguments))
        timestamps = [
            datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
            for event in events
        ]
        self.assertTrue(
            all(left < right for left, right in zip(timestamps, timestamps[1:]))
        )

    def test_reader_snapshot_never_blocks_atomic_ledger_publication(self) -> None:
        reader_opened = threading.Event()
        reader_allowed = threading.Event()
        checkpoint_seen = False

        def paused_snapshot(name: str) -> None:
            nonlocal checkpoint_seen
            if name != "after_fd_open" or checkpoint_seen:
                return
            checkpoint_seen = True
            reader_opened.set()
            if not reader_allowed.wait(timeout=10):
                raise AssertionError("snapshot reader was not released")

        def append() -> None:
            state.append_event(
                self.runs,
                self.mission_id,
                kind="human_approval_requested",
                actor="CONTROL",
                idempotency_key="reader-lock:append",
                payload={"reason": "publish one exact snapshot", "scope": "local"},
            )

        def read() -> list[dict]:
            return state.read_events(
                state.ledger_path(self.runs, self.mission_id),
                expected_mission_id=self.mission_id,
            )

        with (
            mock.patch.object(
                state,
                "_mission_snapshot_checkpoint",
                side_effect=paused_snapshot,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            reader = executor.submit(read)
            self.assertTrue(reader_opened.wait(timeout=10))
            try:
                writer = executor.submit(append)
                writer.result(timeout=10)
            finally:
                reader_allowed.set()
            events = reader.result(timeout=10)

        self.assertEqual(events[-1]["idempotency_key"], "reader-lock:append")

    def test_mutation_barrier_allows_reads_and_blocks_writers(self) -> None:
        writer_started = threading.Event()

        def append() -> None:
            writer_started.set()
            state.append_event(
                self.runs,
                self.mission_id,
                kind="human_approval_requested",
                actor="CONTROL",
                idempotency_key="mutation-barrier:append",
                payload={"reason": "wait for handoff", "scope": "local"},
            )

        before = state.ledger_path(self.runs, self.mission_id).read_bytes()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with state.MissionMutationBarrier(
                self.runs, self.mission_id
            ) as barrier:
                with state.MissionTransaction(
                    self.runs,
                    self.mission_id,
                    mutation_barrier=barrier,
                ) as transaction:
                    snapshot = transaction.current_state
                    with self.assertRaisesRegex(
                        state.MissionConflict, "snapshot is read-only"
                    ):
                        transaction.append_event(
                            kind="human_approval_requested",
                            actor="CONTROL",
                            idempotency_key="mutation-barrier:bypass",
                            payload={"reason": "must not bypass", "scope": "local"},
                        )
                self.assertEqual(snapshot["mission_id"], self.mission_id)
                events = state.read_events(
                    state.ledger_path(self.runs, self.mission_id),
                    expected_mission_id=self.mission_id,
                )
                self.assertEqual(events[-1]["event_sha256"], snapshot["head_sha256"])
                writer = executor.submit(append)
                self.assertTrue(writer_started.wait(timeout=10))
                with self.assertRaisesRegex(
                    state.MissionConflict, "mutations are quiesced"
                ):
                    writer.result(timeout=10)
                self.assertEqual(
                    state.ledger_path(self.runs, self.mission_id).read_bytes(), before
                )

        append()

        current = mission.load_state(self.runs, self.mission_id)
        self.assertEqual(
            current["head_sha256"],
            state.read_events(
                state.ledger_path(self.runs, self.mission_id),
                expected_mission_id=self.mission_id,
            )[-1]["event_sha256"],
        )

    def test_mutation_barrier_rejects_mission_directory_substitution(self) -> None:
        mission_root = state.mission_root(self.runs, self.mission_id)
        moved = self.tmp / "barrier-pinned-mission"
        with state.MissionMutationBarrier(self.runs, self.mission_id) as barrier:
            mission_root.rename(moved)
            mission_root.mkdir(mode=0o700)
            with self.assertRaisesRegex(
                state.MissionStateError, "mutation barrier mission path changed"
            ):
                barrier.assert_binding(self.runs.resolve(), self.mission_id)

    def test_mutation_barrier_fails_fast_across_processes(self) -> None:
        context = multiprocessing.get_context("spawn")
        before = state.ledger_path(self.runs, self.mission_id).read_bytes()
        with context.Pool(processes=1) as pool:
            with state.MissionMutationBarrier(self.runs, self.mission_id):
                blocked = pool.apply(
                    _append_while_quiesced,
                    ((str(self.runs), self.mission_id, "barrier:blocked"),),
                )
                self.assertEqual(blocked, "conflict:mission mutations are quiesced")
                self.assertEqual(
                    state.ledger_path(self.runs, self.mission_id).read_bytes(), before
                )
            retried = pool.apply(
                _append_while_quiesced,
                ((str(self.runs), self.mission_id, "barrier:explicit-retry"),),
            )

        self.assertEqual(retried, "appended")
        events = state.read_events(
            state.ledger_path(self.runs, self.mission_id),
            expected_mission_id=self.mission_id,
        )
        self.assertFalse(
            any(event["idempotency_key"] == "barrier:blocked" for event in events)
        )
        self.assertEqual(events[-1]["idempotency_key"], "barrier:explicit-retry")

    def test_mutation_barrier_turnstile_cuts_off_late_writers(self) -> None:
        transaction_entered = threading.Event()
        release_transaction = threading.Event()
        barrier_entered = threading.Event()
        marker = state.mission_root(
            self.runs, self.mission_id
        ) / ".mutation.quiescing"

        def hold_transaction() -> None:
            with state.MissionTransaction(self.runs, self.mission_id):
                transaction_entered.set()
                if not release_transaction.wait(timeout=10):
                    raise AssertionError("transaction was not released")

        def hold_barrier() -> None:
            with state.MissionMutationBarrier(
                self.runs,
                self.mission_id,
                drain_timeout_seconds=2,
                drain_poll_seconds=0.005,
            ):
                barrier_entered.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            transaction = executor.submit(hold_transaction)
            self.assertTrue(transaction_entered.wait(timeout=10))
            barrier = executor.submit(hold_barrier)
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(marker.is_file())
            started = time.monotonic()
            with self.assertRaisesRegex(
                state.MissionConflict, "mutations are quiesced"
            ):
                state.append_event(
                    self.runs,
                    self.mission_id,
                    kind="human_approval_requested",
                    actor="CONTROL",
                    idempotency_key="barrier:late-writer",
                    payload={"reason": "must fail fast", "scope": "local"},
                )
            self.assertLess(time.monotonic() - started, 0.5)
            release_transaction.set()
            transaction.result(timeout=10)
            barrier.result(timeout=10)

        self.assertTrue(barrier_entered.is_set())
        self.assertFalse(marker.exists())

    def test_mutation_barrier_drain_timeout_cleans_marker_for_retry(self) -> None:
        transaction_entered = threading.Event()
        release_transaction = threading.Event()
        marker = state.mission_root(
            self.runs, self.mission_id
        ) / ".mutation.quiescing"

        def hold_transaction() -> None:
            with state.MissionTransaction(self.runs, self.mission_id):
                transaction_entered.set()
                if not release_transaction.wait(timeout=10):
                    raise AssertionError("transaction was not released")

        with ThreadPoolExecutor(max_workers=1) as executor:
            transaction = executor.submit(hold_transaction)
            self.assertTrue(transaction_entered.wait(timeout=10))
            try:
                with self.assertRaisesRegex(
                    state.MissionConflict, "timed out draining mission mutations"
                ):
                    with state.MissionMutationBarrier(
                        self.runs,
                        self.mission_id,
                        drain_timeout_seconds=0.05,
                        drain_poll_seconds=0.005,
                    ):
                        self.fail("barrier entered while a transaction held the gate")
                self.assertFalse(marker.exists())
            finally:
                release_transaction.set()
            transaction.result(timeout=10)

        event, appended = state.append_event(
            self.runs,
            self.mission_id,
            kind="human_approval_requested",
            actor="CONTROL",
            idempotency_key="barrier:after-timeout",
            payload={"reason": "explicit retry", "scope": "local"},
        )
        self.assertTrue(appended)
        self.assertEqual(event["idempotency_key"], "barrier:after-timeout")

    def test_stale_quiescing_marker_is_recovered_under_gate(self) -> None:
        marker = state.mission_root(
            self.runs, self.mission_id
        ) / ".mutation.quiescing"
        marker.write_bytes(b"")
        marker.chmod(0o600)

        event, appended = state.append_event(
            self.runs,
            self.mission_id,
            kind="human_approval_requested",
            actor="CONTROL",
            idempotency_key="barrier:stale-marker-recovery",
            payload={"reason": "recover crash marker", "scope": "local"},
        )

        self.assertTrue(appended)
        self.assertEqual(event["idempotency_key"], "barrier:stale-marker-recovery")
        self.assertFalse(marker.exists())

    def test_after_publish_crash_recovers_as_exact_idempotent_retry(self) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        code = """
from pathlib import Path
import fleet_mission_state as state
state.append_event(
    Path(__import__('sys').argv[1]), __import__('sys').argv[2],
    kind='human_approval_requested', actor='CONTROL',
    idempotency_key='crash:after-publish',
    payload={'reason': 'durable crash retry', 'scope': 'local'},
)
"""
        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "scripts"),
            "FLEET_TEST_MISSION_TRANSACTION_CRASH_AT": "after_publish",
        }
        crashed = subprocess.run(
            [sys.executable, "-c", code, str(self.runs), self.mission_id],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(crashed.returncode, 137, crashed.stderr)
        event, appended = state.append_event(
            self.runs,
            self.mission_id,
            kind="human_approval_requested",
            actor="CONTROL",
            idempotency_key="crash:after-publish",
            payload={"reason": "durable crash retry", "scope": "local"},
        )
        self.assertFalse(appended)
        events = state.read_events(ledger, expected_mission_id=self.mission_id)
        self.assertEqual(
            sum(item["idempotency_key"] == "crash:after-publish" for item in events),
            1,
        )
        self.assertEqual(event, events[-1])

    def test_initial_publish_rename_crash_recovers_one_single_link_ledger(
        self,
    ) -> None:
        runs = self.tmp / "initial-publish-runs"
        key = "create:initial-publish-crash"
        mission_id = mission._mission_id_for_key(key)
        code = """
from pathlib import Path
import fleet_mission as mission
import workflow_config as workflow
root = Path(__import__('sys').argv[1])
mission.create_mission(
    Path(__import__('sys').argv[2]),
    compiled=workflow.compile_path(root / 'workflows' / 'implementation.yaml'),
    feature='initial-publish-crash',
    objective='recover one exclusive first ledger publication',
    target_repo=Path(__import__('sys').argv[3]),
    base_sha='a' * 40,
    idempotency_key='create:initial-publish-crash',
)
"""
        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "scripts"),
            "FLEET_TEST_MISSION_TRANSACTION_CRASH_AT": (
                "after_initial_publish_rename"
            ),
        }
        crashed = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(ROOT),
                str(runs),
                str(self.target.resolve()),
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(crashed.returncode, 137, crashed.stderr)

        ledger = state.ledger_path(runs, mission_id)
        self.assertEqual(ledger.stat().st_nlink, 1)
        recovered_id, created = mission.create_mission(
            runs,
            compiled=copy.deepcopy(self.compiled),
            feature="initial-publish-crash",
            objective="recover one exclusive first ledger publication",
            target_repo=self.target.resolve(),
            base_sha="a" * 40,
            idempotency_key=key,
        )
        self.assertEqual(recovered_id, mission_id)
        self.assertFalse(created)
        events = state.read_events(ledger, expected_mission_id=mission_id)
        self.assertEqual(
            sum(event["kind"] == "mission_created" for event in events),
            1,
        )
        self.assertEqual(ledger.stat().st_nlink, 1)
        self.assertEqual(
            list(ledger.parent.glob(".mission.jsonl.*.tmp")),
            [],
        )

    def test_legacy_initial_publish_hardlink_is_recovered_exactly(self) -> None:
        ledger = state.ledger_path(self.runs, self.mission_id)
        legacy = ledger.parent / f".mission.jsonl.{'a' * 32}.tmp"
        os.link(ledger, legacy)
        self.assertEqual(ledger.stat().st_nlink, 2)

        event, appended = state.append_event(
            self.runs,
            self.mission_id,
            kind="human_approval_requested",
            actor="CONTROL",
            idempotency_key="crash:legacy-link-recovery",
            payload={"reason": "repair old link window", "scope": "local"},
        )

        self.assertTrue(appended)
        self.assertEqual(event["idempotency_key"], "crash:legacy-link-recovery")
        self.assertFalse(legacy.exists())
        self.assertEqual(ledger.stat().st_nlink, 1)

    def test_pending_ledger_crash_retry_removes_exact_temporary(self) -> None:
        code = """
from pathlib import Path
import fleet_mission_state as state
state.append_event(
    Path(__import__('sys').argv[1]), __import__('sys').argv[2],
    kind='human_approval_requested', actor='CONTROL',
    idempotency_key='crash:pending-ledger',
    payload={'reason': 'recover pending bytes', 'scope': 'local'},
)
"""
        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "scripts"),
            "FLEET_TEST_MISSION_TRANSACTION_CRASH_AT": "after_pending_fsync",
        }
        crashed = subprocess.run(
            [sys.executable, "-c", code, str(self.runs), self.mission_id],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(crashed.returncode, 137, crashed.stderr)
        mission_root = state.mission_root(self.runs, self.mission_id)
        self.assertEqual(len(list(mission_root.glob(".mission.jsonl.*.tmp"))), 1)

        event, appended = state.append_event(
            self.runs,
            self.mission_id,
            kind="human_approval_requested",
            actor="CONTROL",
            idempotency_key="crash:pending-ledger",
            payload={"reason": "recover pending bytes", "scope": "local"},
        )
        self.assertTrue(appended)
        self.assertEqual(event["idempotency_key"], "crash:pending-ledger")
        self.assertEqual(list(mission_root.glob(".mission.jsonl.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
