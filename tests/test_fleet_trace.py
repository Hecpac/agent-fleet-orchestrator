from __future__ import annotations

import importlib
from pathlib import Path
import sys
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
trace = importlib.import_module("fleet_trace")


class FleetTraceTests(unittest.TestCase):
    def test_spans_are_derived_from_durable_events_and_redact_content(self) -> None:
        mission_id = str(uuid.uuid4())
        root = {
            "mission_id": mission_id,
            "sequence": 1,
            "timestamp": "2026-07-14T00:00:00Z",
            "kind": "mission_created",
            "payload": {"feature": "demo", "workflow_digest": "a" * 64},
        }
        run_id = str(uuid.uuid4())
        child = {
            "event_id": str(uuid.uuid4()),
            "mission_id": mission_id,
            "sequence": 2,
            "timestamp": "2026-07-14T00:00:01Z",
            "kind": "delegation_registered",
            "payload": {
                "run_id": run_id,
                "parent_run_id": None,
                "objective": "private prompt",
                "objective_sha256": "b" * 64,
            },
        }
        spans = trace.events_to_spans([root, child])
        self.assertEqual(
            [span["name"] for span in spans], ["mission", "delegation", "agent_run"]
        )
        self.assertEqual(spans[1]["parent_span_id"], f"mission:{mission_id}")
        self.assertNotIn("objective", spans[1]["attributes"])
        self.assertEqual(spans[1]["attributes"]["objective_sha256"], "b" * 64)
        self.assertEqual(spans[2]["parent_span_id"], spans[1]["span_id"])
        self.assertEqual(spans[2]["attributes"]["run_id"], run_id)


if __name__ == "__main__":
    unittest.main()
