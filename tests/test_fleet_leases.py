from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_leases  # noqa: E402
from fleet_ledger import append_event, latest_event  # noqa: E402


WORKSPACE_UUID = "00000000-0000-0000-0000-000000000001"
SURFACE_UUID = "00000000-0000-0000-0000-000000000101"


class FleetLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.runs = Path(self.tempdir.name) / "runs"
        self.runs.mkdir()

    def acquire(self, run_id: str, feature: str = "feature") -> dict[str, str]:
        return fleet_leases.acquire(
            self.runs,
            run_id=run_id,
            feature=feature,
            instance="triage",
            role="triage",
            phase="RECON",
            resource_class="local_light",
            task_sha256="a" * 64,
            workspace_uuid=WORKSPACE_UUID,
            surface_uuid=SURFACE_UUID,
            max_local=2,
            role_limit=2,
        )

    def ledger_event(self, run_id: str, status: str, feature: str = "feature") -> None:
        append_event(
            self.runs / f"fleet-{feature}.ledger.jsonl",
            {
                "timestamp": "2026-07-12T00:00:00+00:00",
                "run_id": run_id,
                "feature": feature,
                "instance": "triage",
                "role": "triage",
                "phase": "RECON",
                "status": status,
                "task_sha256": "a" * 64,
            },
        )

    @staticmethod
    def lease_paths(value: dict[str, str]) -> list[Path]:
        return [Path(path) for path in value.values() if path]

    def test_terminal_run_is_quarantined_idempotently(self) -> None:
        acquired = self.acquire("run-1")
        self.ledger_event("run-1", "succeeded")
        result = fleet_leases.reconcile(
            self.runs,
            tree_reader=lambda: (_ for _ in ()).throw(AssertionError("tree not needed")),
        )
        self.assertEqual(len(result["quarantined"]), 3)
        self.assertFalse(any(path.exists() for path in self.lease_paths(acquired)))
        second = fleet_leases.reconcile(self.runs, tree_reader=lambda: "")
        self.assertEqual(second["quarantined"], [])

    def test_confirmed_absent_run_is_abandoned_before_quarantine(self) -> None:
        self.acquire("run-absent")
        self.ledger_event("run-absent", "running")
        result = fleet_leases.reconcile(
            self.runs,
            tree_reader=lambda: (
                "workspace workspace:9 99999999-9999-9999-9999-999999999999\n"
                "surface surface:9 99999999-9999-9999-9999-999999999998\n"
            ),
        )
        self.assertEqual(len(result["quarantined"]), 3)
        event = latest_event(
            self.runs / "fleet-feature.ledger.jsonl", run_id="run-absent"
        )
        self.assertEqual(event["status"], "abandoned")
        self.assertIn("uuid_absent", event["reason"])

    def test_probe_error_preserves_nonterminal_leases(self) -> None:
        acquired = self.acquire("run-live")
        self.ledger_event("run-live", "running")
        result = fleet_leases.reconcile(
            self.runs,
            tree_reader=lambda: (_ for _ in ()).throw(RuntimeError("cmux unavailable")),
        )
        self.assertIn("cmux unavailable", result["probe_error"])
        self.assertTrue(all(path.exists() for path in self.lease_paths(acquired)))

    def test_unparseable_tree_output_fails_closed_without_reclaim(self) -> None:
        acquired = self.acquire("run-protocol")
        self.ledger_event("run-protocol", "running")
        with self.assertRaises(fleet_leases.LeaseError):
            fleet_leases.reconcile(
                self.runs,
                tree_reader=lambda: "cmux tree protocol changed",
            )
        self.assertTrue(all(path.exists() for path in self.lease_paths(acquired)))
        self.assertEqual(
            latest_event(
                self.runs / "fleet-feature.ledger.jsonl", run_id="run-protocol"
            )["status"],
            "running",
        )

    def test_old_owner_cannot_release_reassigned_lease(self) -> None:
        first = self.acquire("run-old")
        self.ledger_event("run-old", "succeeded")
        fleet_leases.reconcile(self.runs, tree_reader=lambda: "")
        second = self.acquire("run-new")
        with self.assertRaises(fleet_leases.LeaseError):
            fleet_leases.release(self.runs, "run-old", self.lease_paths(second))
        self.assertTrue(all(path.exists() for path in self.lease_paths(second)))
        self.assertEqual(
            {fleet_leases.read_metadata(path)["run_id"] for path in self.lease_paths(first)},
            {"run-new"},
        )

    def test_release_validates_every_lease_before_removing_any(self) -> None:
        leases = self.lease_paths(self.acquire("run-old"))
        metadata = fleet_leases.read_metadata(leases[-1])
        self.assertIsNotNone(metadata)
        metadata["run_id"] = "run-new"
        fleet_leases._update_metadata(leases[-1], metadata)

        with self.assertRaises(fleet_leases.LeaseError):
            fleet_leases.release(self.runs, "run-old", leases)

        self.assertTrue(all(path.exists() for path in leases))

    def test_runner_setup_failure_is_abandoned_before_leases_release(self) -> None:
        acquired = self.acquire("run-setup")
        env = os.environ.copy()
        env["FLEET_RUNS_DIR"] = str(self.runs)
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts" / "run-local-task.sh"),
                "feature",
                "triage",
                "triage",
                "RECON",
                "local_light",
                "run-setup",
                str(self.runs / "missing-task.txt"),
                "a" * 64,
                acquired["local_slot"],
                acquired["role_slot"],
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        event = latest_event(
            self.runs / "fleet-feature.ledger.jsonl", run_id="run-setup"
        )
        self.assertEqual(event["status"], "abandoned")
        self.assertEqual(event["exit_code"], 4)
        self.assertTrue(all(not path.exists() for path in self.lease_paths(acquired)))

    def test_first_terminal_ledger_event_is_immutable(self) -> None:
        ledger = self.runs / "fleet-feature.ledger.jsonl"
        self.ledger_event("run-terminal", "succeeded")
        appended = append_event(
            ledger,
            {
                "run_id": "run-terminal",
                "feature": "feature",
                "instance": "triage",
                "role": "triage",
                "phase": "RECON",
                "status": "abandoned",
                "task_sha256": "a" * 64,
            },
        )
        self.assertFalse(appended)
        self.assertEqual(
            latest_event(ledger, run_id="run-terminal")["status"],
            "succeeded",
        )

    def test_close_marker_atomically_blocks_new_dispatch(self) -> None:
        fleet_leases.begin_close(
            self.runs,
            feature="feature",
            close_id="close-1",
            workspace_uuid=WORKSPACE_UUID,
            tree_reader=lambda: (
                f"workspace:1 {WORKSPACE_UUID}\n"
                f"surface:1 {SURFACE_UUID}\n"
            ),
        )
        with self.assertRaises(fleet_leases.LeaseBusy):
            self.acquire("run-too-late")
        with self.assertRaises(fleet_leases.LeaseError):
            fleet_leases.end_close(
                self.runs, feature="feature", close_id="wrong-owner"
            )
        fleet_leases.end_close(
            self.runs, feature="feature", close_id="close-1"
        )

    def test_absent_workspace_quarantines_stale_close_owner_for_retry(self) -> None:
        live_tree = lambda: (
            f"workspace:1 {WORKSPACE_UUID}\n"
            f"surface:1 {SURFACE_UUID}\n"
        )
        fleet_leases.begin_close(
            self.runs,
            feature="feature",
            close_id="close-stale",
            workspace_uuid=WORKSPACE_UUID,
            tree_reader=live_tree,
        )
        fleet_leases.begin_close(
            self.runs,
            feature="feature",
            close_id="close-retry",
            workspace_uuid=WORKSPACE_UUID,
            tree_reader=lambda: "",
        )
        archived = list(
            (self.runs / "archive" / "closing" / "feature").glob("*-closing.json")
        )
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text())["close_id"], "close-stale")
        fleet_leases.end_close(
            self.runs, feature="feature", close_id="close-retry"
        )


if __name__ == "__main__":
    unittest.main()
