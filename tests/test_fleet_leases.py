from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_leases  # noqa: E402
import fleet_json  # noqa: E402
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

    def write_raw_lease(self, name: str, raw: bytes | str) -> Path:
        locks = self.runs / "locks"
        locks.mkdir(mode=0o700, exist_ok=True)
        locks.chmod(0o700)
        lease = locks / name
        lease.mkdir(mode=0o700, exist_ok=True)
        lease.chmod(0o700)
        metadata = lease / "lease.json"
        metadata.write_bytes(raw.encode("utf-8") if isinstance(raw, str) else raw)
        metadata.chmod(0o600)
        return lease

    def durable_files(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.runs)): path.read_bytes()
            for path in self.runs.rglob("*")
            if path.is_file()
        }

    @staticmethod
    def remote_metadata(
        *, feature: str = "feature", instance: str = "triage"
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": "run-strict",
            "feature": feature,
            "instance": instance,
            "role": "triage",
            "phase": "RECON",
            "resource_class": "remote",
            "runner": "interactive",
            "task_sha256": "a" * 64,
            "workspace_uuid": WORKSPACE_UUID,
            "surface_uuid": SURFACE_UUID,
            "acquired_at": "2026-07-16T00:00:00+00:00",
            "pid": None,
            "pgid": None,
            "kind": f"{feature}.{instance}.lock",
        }

    def test_terminal_run_is_quarantined_idempotently(self) -> None:
        acquired = self.acquire("run-1")
        self.ledger_event("run-1", "succeeded")
        result = fleet_leases.reconcile(
            self.runs,
            tree_reader=lambda: (_ for _ in ()).throw(
                AssertionError("tree not needed")
            ),
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
            {
                fleet_leases.read_metadata(path)["run_id"]
                for path in self.lease_paths(first)
            },
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
                "ollama",
                "gemma3:4b",
                "",
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
        self.assertEqual(event["provider"], "ollama")
        self.assertEqual(event["model"], "gemma3:4b")
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
                f"workspace:1 {WORKSPACE_UUID}\nsurface:1 {SURFACE_UUID}\n"
            ),
        )
        with self.assertRaises(fleet_leases.LeaseBusy):
            self.acquire("run-too-late")
        with self.assertRaises(fleet_leases.LeaseError):
            fleet_leases.end_close(self.runs, feature="feature", close_id="wrong-owner")
        fleet_leases.end_close(self.runs, feature="feature", close_id="close-1")

    def test_absent_workspace_quarantines_stale_close_owner_for_retry(self) -> None:
        def live_tree() -> str:
            return f"workspace:1 {WORKSPACE_UUID}\nsurface:1 {SURFACE_UUID}\n"

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
        fleet_leases.end_close(self.runs, feature="feature", close_id="close-retry")

    def test_locks_ancestor_symlink_is_rejected_without_external_mutation(self) -> None:
        outside = Path(self.tempdir.name) / "outside-locks"
        outside.mkdir(mode=0o700)
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("untouched", encoding="utf-8")
        (self.runs / "locks").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(fleet_leases.LeaseError, "unsafe lease storage"):
            self.acquire("run-symlink")

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")
        self.assertEqual(
            sorted(path.name for path in outside.iterdir()), ["sentinel.txt"]
        )

    def test_coordinator_symlink_is_rejected_without_target_mutation(self) -> None:
        locks = self.runs / "locks"
        locks.mkdir(mode=0o700)
        target = Path(self.tempdir.name) / "external-coordinator"
        target.write_text("do-not-lock-or-write", encoding="utf-8")
        target.chmod(0o600)
        (locks / ".coordinator").symlink_to(target)

        with self.assertRaisesRegex(
            fleet_leases.LeaseError, "physical coordinator lock"
        ):
            self.acquire("run-coordinator-symlink")

        self.assertEqual(target.read_text(encoding="utf-8"), "do-not-lock-or-write")
        self.assertEqual(
            sorted(path.name for path in locks.iterdir()), [".coordinator"]
        )

    def test_lease_directory_symlink_fails_closed_without_external_read_or_write(
        self,
    ) -> None:
        fleet_leases.reconcile(self.runs, tree_reader=lambda: "")
        outside = Path(self.tempdir.name) / "external-lease"
        outside.mkdir(mode=0o700)
        metadata = outside / "lease.json"
        metadata.write_text('{"run_id":"foreign"}\n', encoding="utf-8")
        metadata.chmod(0o600)
        (self.runs / "locks" / "foreign.lock").symlink_to(
            outside, target_is_directory=True
        )

        with self.assertRaisesRegex(
            fleet_leases.LeaseError, "physical lease directory"
        ):
            fleet_leases.reconcile(self.runs, tree_reader=lambda: "")

        self.assertEqual(metadata.read_text(encoding="utf-8"), '{"run_id":"foreign"}\n')
        self.assertFalse((self.runs / "archive").exists())

    def test_lease_metadata_symlink_fails_closed_without_target_mutation(self) -> None:
        fleet_leases.reconcile(self.runs, tree_reader=lambda: "")
        lease = self.runs / "locks" / "foreign.lock"
        lease.mkdir(mode=0o700)
        target = Path(self.tempdir.name) / "external-lease.json"
        target.write_text('{"run_id":"foreign"}\n', encoding="utf-8")
        target.chmod(0o600)
        (lease / "lease.json").symlink_to(target)

        with self.assertRaisesRegex(fleet_leases.LeaseError, "physical lease file"):
            fleet_leases.reconcile(self.runs, tree_reader=lambda: "")

        self.assertEqual(target.read_text(encoding="utf-8"), '{"run_id":"foreign"}\n')
        self.assertTrue((lease / "lease.json").is_symlink())

    def test_lease_directory_mode_drift_blocks_release_before_any_mutation(
        self,
    ) -> None:
        leases = self.lease_paths(self.acquire("run-directory-mode"))
        leases[-1].chmod(0o755)

        with self.assertRaisesRegex(fleet_leases.LeaseError, "directory mode mismatch"):
            fleet_leases.release(self.runs, "run-directory-mode", leases)

        self.assertTrue(all(path.exists() for path in leases))

    def test_lease_file_mode_drift_blocks_release_before_any_mutation(self) -> None:
        leases = self.lease_paths(self.acquire("run-file-mode"))
        metadata = leases[-1] / "lease.json"
        metadata.chmod(0o644)

        with self.assertRaisesRegex(fleet_leases.LeaseError, "file mode mismatch"):
            fleet_leases.release(self.runs, "run-file-mode", leases)

        self.assertTrue(all(path.exists() for path in leases))
        self.assertEqual(metadata.stat().st_mode & 0o777, 0o644)

    def test_owner_validators_reject_foreign_uid(self) -> None:
        foreign_uid = os.geteuid() + 1
        with self.assertRaisesRegex(
            fleet_leases.LeaseError, "directory owner mismatch"
        ):
            fleet_leases._validate_directory(
                SimpleNamespace(st_mode=0o040700, st_uid=foreign_uid), "lease"
            )
        with self.assertRaisesRegex(fleet_leases.LeaseError, "file owner mismatch"):
            fleet_leases._validate_regular(
                SimpleNamespace(st_mode=0o100600, st_uid=foreign_uid), "lease.json"
            )

    def test_archive_symlink_blocks_quarantine_without_external_mutation(self) -> None:
        acquired = self.acquire("run-archive-symlink")
        leases = self.lease_paths(acquired)
        self.ledger_event("run-archive-symlink", "succeeded")
        outside = Path(self.tempdir.name) / "external-archive"
        outside.mkdir(mode=0o700)
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("untouched", encoding="utf-8")
        (self.runs / "archive").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(fleet_leases.LeaseError, "archive storage"):
            fleet_leases.reconcile(self.runs, tree_reader=lambda: "")

        self.assertTrue(all(path.exists() for path in leases))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")
        self.assertEqual(
            sorted(path.name for path in outside.iterdir()), ["sentinel.txt"]
        )

    def test_closing_marker_mode_drift_blocks_owner_release(self) -> None:
        marker = fleet_leases.begin_close(
            self.runs,
            feature="mode-drift",
            close_id="close-mode",
            workspace_uuid=WORKSPACE_UUID,
            tree_reader=lambda: "",
        )
        marker.chmod(0o644)

        with self.assertRaisesRegex(fleet_leases.LeaseError, "file mode mismatch"):
            fleet_leases.end_close(
                self.runs, feature="mode-drift", close_id="close-mode"
            )

        self.assertTrue(marker.exists())
        self.assertEqual(marker.stat().st_mode & 0o777, 0o644)

    def test_malformed_lease_schemas_block_close_without_mutation(self) -> None:
        fleet_leases.reconcile(self.runs, tree_reader=lambda: "")
        metadata = self.remote_metadata()
        wrong_feature = {**metadata, "feature": "foreign"}
        wrong_kind = {**metadata, "kind": "foreign.lock"}
        canonical = fleet_json.canonical_bytes(metadata) + b"\n"
        cases = {
            "minimal": b'{"run_id":"run-strict"}\n',
            "duplicate-key": canonical.replace(b"{", b'{"schema_version":1,', 1),
            "nan": b'{"value":NaN}\n',
            "infinity": b'{"value":Infinity}\n',
            "overflow": b'{"value":1e999}\n',
            "bom": b"\xef\xbb\xbf" + canonical,
            "invalid-utf8": b'{"value":"\xff"}\n',
            "surrogate": b'{"value":"\\ud800"}\n',
            "trailing": canonical.rstrip(b"\n") + b" trailing\n",
            "missing-lf": canonical.rstrip(b"\n"),
            "noncanonical-whitespace": b" " + canonical,
            "wrong-feature": fleet_json.canonical_bytes(wrong_feature) + b"\n",
            "wrong-kind": fleet_json.canonical_bytes(wrong_kind) + b"\n",
        }
        lease = self.write_raw_lease("feature.triage.lock", cases["minimal"])
        lease_json = lease / "lease.json"
        for label, raw in cases.items():
            with self.subTest(label=label):
                lease_json.write_bytes(raw)
                lease_json.chmod(0o600)
                before = self.durable_files()
                with self.assertRaisesRegex(
                    fleet_leases.LeaseBusy, "active or malformed leases"
                ):
                    fleet_leases.begin_close(
                        self.runs,
                        feature="feature",
                        close_id=f"close-{label}",
                        workspace_uuid=WORKSPACE_UUID,
                        tree_reader=lambda: (_ for _ in ()).throw(
                            AssertionError("malformed metadata must not be probed")
                        ),
                    )
                self.assertEqual(lease_json.read_bytes(), raw)
                self.assertEqual(self.durable_files(), before)
                self.assertFalse((self.runs / "locks" / "feature.closing").exists())
                self.assertFalse((self.runs / "archive").exists())

    def test_noncanonical_closing_marker_is_rejected_without_mutation(self) -> None:
        marker = fleet_leases.begin_close(
            self.runs,
            feature="strict-marker",
            close_id="close-strict",
            workspace_uuid=WORKSPACE_UUID,
            tree_reader=lambda: "",
        )
        value = json.loads(marker.read_text(encoding="utf-8"))
        canonical = fleet_json.canonical_bytes(value) + b"\n"
        cases = {
            "duplicate-key": canonical.replace(b"{", b'{"schema_version":1,', 1),
            "nan": b'{"value":NaN}\n',
            "infinity": b'{"value":Infinity}\n',
            "overflow": b'{"value":1e999}\n',
            "bom": b"\xef\xbb\xbf" + canonical,
            "invalid-utf8": b'{"value":"\xff"}\n',
            "surrogate": b'{"value":"\\ud800"}\n',
            "trailing": canonical.rstrip(b"\n") + b" trailing\n",
            "missing-lf": canonical.rstrip(b"\n"),
            "noncanonical-whitespace": b" " + canonical,
        }
        for label, raw in cases.items():
            with self.subTest(label=label):
                marker.write_bytes(raw)
                marker.chmod(0o600)
                before = self.durable_files()
                with self.assertRaisesRegex(
                    fleet_leases.LeaseError, "missing or malformed"
                ):
                    fleet_leases.end_close(
                        self.runs,
                        feature="strict-marker",
                        close_id="close-strict",
                    )
                self.assertEqual(marker.read_bytes(), raw)
                self.assertEqual(self.durable_files(), before)
                self.assertFalse((self.runs / "archive").exists())


if __name__ == "__main__":
    unittest.main()
