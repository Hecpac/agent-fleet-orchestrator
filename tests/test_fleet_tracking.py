from __future__ import annotations

import copy
import unittest

import fleet_tracking


class FleetTrackingTests(unittest.TestCase):
    def controlled_success(self) -> list[dict]:
        common = {
            "run_id": "run-1",
            "runner": "interactive",
            "tracking_protocol": "control-v1",
        }
        return [
            {**common, "status": "preparing"},
            {**common, "status": "dispatched"},
            {
                **common,
                "status": "authorized",
                "submission_event_id": "event-9",
                "submission_boot_id": "boot-1",
                "submission_seq": 9,
                "submission_session_id": "codex-session",
            },
            {
                **common,
                "status": "running",
                "binding_event_id": "event-9",
                "binding_boot_id": "boot-1",
                "binding_seq": 9,
                "session_id": "codex-session",
            },
            {**common, "status": "succeeded", "result_file": "/safe/result"},
        ]

    def test_control_authorized_success_verifies_and_binding_tamper_fails(self) -> None:
        events = self.controlled_success()
        self.assertEqual(
            fleet_tracking.verify_run_events(events, required_protocol="control-v1")["status"],
            "succeeded",
        )
        tampered = copy.deepcopy(events)
        tampered[3]["binding_event_id"] = "raw-cmux-event"
        with self.assertRaisesRegex(fleet_tracking.TrackingError, "does not match"):
            fleet_tracking.verify_run_events(tampered, required_protocol="control-v1")

    def test_new_manifest_rejects_legacy_interactive_success_but_allows_local(self) -> None:
        with self.assertRaisesRegex(fleet_tracking.TrackingError, "lacks required"):
            fleet_tracking.verify_run_events(
                [{"runner": "interactive", "status": "succeeded"}],
                required_protocol="control-v1",
            )
        local = fleet_tracking.verify_run_events(
            [{"runner": "local", "status": "succeeded"}],
            required_protocol="control-v1",
        )
        self.assertEqual(local["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
