"""Telemetry observes Mission authority and never defines it."""
from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tests import test_fleet_herdr_mission as fixtures  # noqa: E402
import fleet_mission  # noqa: E402
import fleet_mission_state as state  # noqa: E402

# Read-only renderers of Mission evidence.
REPORTING_MODULES = ("fleet_trace", "fleet_export_trace", "fleet_report", "fleet_herdr_report")
MEASUREMENT_MODULES = ("fleet_herdr_metrics", *REPORTING_MODULES)
LEDGER_WRITERS = {"append_event", "append_events", "append_terminal", "atomic_write",
                  "exclusive_lock", "ensure_private_directory", "observe"}
INTERVAL_KINDS = ("herdr_interval_started", "herdr_interval_finished")


def module_tree(name: str) -> ast.Module:
    return ast.parse((ROOT / "scripts" / f"{name}.py").read_text())


def imported_modules(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def called_names(tree: ast.AST) -> set[str]:
    """Called attribute and bare names, so `from x import writer` is also caught."""
    return {node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))}


class IntervalLedgerSchemaTests(unittest.TestCase):
    """The ledger's interval events keep their exact schema and messages."""

    def assert_rejected(self, kind: str, payload: dict, message: str) -> None:
        with self.assertRaisesRegex(state.MissionStateError, message):
            state._validate_payload(kind, payload)

    def test_valid_interval_payloads(self) -> None:
        identifier = str(uuid.uuid4())
        for kind in ("controller_operation", "controller_wait", "functional_execution", "supervisor_idle"):
            state._validate_payload("herdr_interval_started",
                                    {"interval_id": identifier, "kind": kind, "run_id": None})
        state._validate_payload("herdr_interval_started",
                                {"interval_id": identifier, "kind": "controller_wait", "run_id": str(uuid.uuid4())})
        for outcome in ("returned", "raised"):
            state._validate_payload("herdr_interval_finished",
                                    {"interval_id": identifier, "elapsed_ns": 0, "outcome": outcome})

    def test_interval_payload_rejections(self) -> None:
        identifier = str(uuid.uuid4())
        started = {"interval_id": identifier, "kind": "controller_wait", "run_id": None}
        finished = {"interval_id": identifier, "elapsed_ns": 1, "outcome": "returned"}
        self.assert_rejected("herdr_interval_started", {**started, "extra": 1},
                             "herdr_interval_started payload fields do not match schema")
        self.assert_rejected("herdr_interval_finished", {"interval_id": identifier},
                             "herdr_interval_finished payload fields do not match schema")
        self.assert_rejected("herdr_interval_started", {**started, "kind": "agent_execution"},
                             "unsupported controller interval")
        self.assert_rejected("herdr_interval_started", {**started, "run_id": identifier.upper()},
                             "observed run is not canonical")
        self.assert_rejected("herdr_interval_finished", {**finished, "elapsed_ns": -1},
                             "monotonic duration must be a non-negative integer")
        self.assert_rejected("herdr_interval_finished", {**finished, "elapsed_ns": True},
                             "monotonic duration must be a non-negative integer")
        self.assert_rejected("herdr_interval_finished", {**finished, "outcome": "timeout"},
                             "invalid interval outcome")
        self.assert_rejected("herdr_interval_started", {**started, "interval_id": 7},
                             "interval identity must be a canonical UUID string")
        self.assert_rejected("herdr_interval_finished", {**finished, "interval_id": "not-a-uuid"},
                             "interval identity")


class IntervalLedgerReducerTests(unittest.TestCase):
    def setUp(self) -> None:
        helper = fixtures.HerdrMissionTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.runs, self.mid = helper.runs, helper.mid

    def append(self, kind: str, payload: dict, *, actor: str = "CONTROL", key: str | None = None) -> None:
        state.append_event(self.runs, self.mid, kind=kind, actor=actor,
                           idempotency_key=key or str(uuid.uuid4()), payload=payload)

    def intervals(self) -> dict:
        return fleet_mission.load_state(self.runs, self.mid)["herdr_intervals"]

    def test_interval_lifecycle_projection(self) -> None:
        identifier, run_id = str(uuid.uuid4()), str(uuid.uuid4())
        self.append("herdr_interval_started", {"interval_id": identifier, "kind": "controller_wait", "run_id": run_id})
        opened = self.intervals()[identifier]
        self.assertEqual(set(opened), {"interval_id", "kind", "run_id", "started_at",
                                       "ended_at", "elapsed_ns", "outcome"})
        self.assertEqual((opened["kind"], opened["run_id"], opened["ended_at"], opened["elapsed_ns"],
                          opened["outcome"]), ("controller_wait", run_id, None, None, None))
        self.append("herdr_interval_finished", {"interval_id": identifier, "elapsed_ns": 5, "outcome": "raised"})
        closed = self.intervals()[identifier]
        self.assertEqual((closed["elapsed_ns"], closed["outcome"], closed["started_at"]),
                         (5, "raised", opened["started_at"]))
        self.assertIsInstance(closed["ended_at"], str)

    def test_interval_conflicts(self) -> None:
        identifier = str(uuid.uuid4())
        started = {"interval_id": identifier, "kind": "controller_operation", "run_id": None}
        with self.assertRaisesRegex(state.MissionConflict, "controller interval requires CONTROL"):
            self.append("herdr_interval_started", started, actor="LEAD")
        with self.assertRaisesRegex(state.MissionConflict, "interval completion lacks its unique start"):
            self.append("herdr_interval_finished", {"interval_id": identifier, "elapsed_ns": 1, "outcome": "returned"})
        self.append("herdr_interval_started", started)
        with self.assertRaisesRegex(state.MissionConflict, "interval identity cannot be reused"):
            self.append("herdr_interval_started", started)
        self.append("herdr_interval_finished", {"interval_id": identifier, "elapsed_ns": 1, "outcome": "returned"})
        with self.assertRaisesRegex(state.MissionConflict, "interval completion lacks its unique start"):
            self.append("herdr_interval_finished", {"interval_id": identifier, "elapsed_ns": 2, "outcome": "returned"})


class UsageTotalityTests(unittest.TestCase):
    """Archive, Owner Cycle and report verdicts call usage() on validated rows.

    Those validators check rows and payloads but not token_count contents, so a
    counter snapshot must degrade to unknown usage instead of raising into them.
    """

    JSON_VALUES = (None, True, False, 0, 5, -1, 1.5, "", "x", [], [1], {}, {"input_tokens": "1"},
                   {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 2},
                   {"input_tokens": 3, "output_tokens": 1, "cached_input_tokens": 2})

    @staticmethod
    def event(kind: str, **payload) -> dict:
        return {"type": "event_msg", "payload": {"type": kind, **payload}}

    def turn(self, *counters: dict) -> list[dict]:
        return [self.event("task_started", turn_id="t"), *counters, self.event("task_complete", turn_id="t")]

    def test_non_object_counter_snapshot_is_invalid_usage(self) -> None:
        import fleet_herdr_metrics
        for info in (None, {}, 0, "", [], [1], "x", 5, True, [{}]):
            with self.subTest(info=info):
                usage = fleet_herdr_metrics.usage(self.turn(self.event("token_count", info=info)), "t")
                self.assertEqual(usage["usage_reason"], "invalid_usage_counter_snapshot")
                self.assertIsNone(usage["prompt_tokens"])

    def test_usage_is_total_over_validated_rows(self) -> None:
        import fleet_herdr_metrics
        valid = {"input_tokens": 3, "output_tokens": 1, "cached_input_tokens": 2}
        for value in self.JSON_VALUES:
            infos = (value, {"total_token_usage": value, "last_token_usage": value},
                     {"total_token_usage": valid, "last_token_usage": value})
            for info in infos:
                for prefix in ([], [self.event("token_count", info={"total_token_usage": valid})]):
                    with self.subTest(info=info, prefix=bool(prefix)):
                        rows = [*prefix, *self.turn(self.event("token_count", info=info))]
                        usage = fleet_herdr_metrics.usage(rows, "t")
                        self.assertIn("usage_reason", usage)


class TelemetryDirectionTests(unittest.TestCase):
    def test_ledger_never_imports_measurement(self) -> None:
        self.assertFalse(imported_modules(module_tree("fleet_mission_state")) & set(MEASUREMENT_MODULES))

    def test_ledger_owns_the_interval_kinds(self) -> None:
        import fleet_herdr_metrics
        self.assertEqual(state.HERDR_INTERVAL_KINDS, frozenset(
            {"controller_operation", "controller_wait", "functional_execution", "supervisor_idle"}))
        self.assertIs(fleet_herdr_metrics.KINDS, state.HERDR_INTERVAL_KINDS)

    def test_reporting_modules_never_write_mission_state(self) -> None:
        for name in REPORTING_MODULES:
            with self.subTest(module=name):
                self.assertFalse(called_names(module_tree(name)) & LEDGER_WRITERS)

    def test_interval_instrumentation_is_the_only_measurement_writer(self) -> None:
        tree = module_tree("fleet_herdr_metrics")
        writers = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)
                   and called_names(node) & LEDGER_WRITERS}
        self.assertEqual(writers, {"observe"})
        kinds = {keyword.value.value for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "append_event"
                 for keyword in node.keywords if keyword.arg == "kind"}
        self.assertEqual(kinds, set(INTERVAL_KINDS))


if __name__ == "__main__":
    unittest.main()
