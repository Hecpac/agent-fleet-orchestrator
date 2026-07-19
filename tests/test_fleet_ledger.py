from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_ledger  # noqa: E402


class FleetLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs = self.tmp / "runs"
        self.runs.mkdir(mode=0o700)
        self.runs.chmod(0o700)
        self.ledger = self.runs / "fleet-frontier.ledger.jsonl"

    @staticmethod
    def event(run_id: str, status: str) -> dict[str, str]:
        return {
            "run_id": run_id,
            "feature": "frontier",
            "instance": "challenger",
            "role": "challenger",
            "phase": "CHALLENGE",
            "status": status,
            "task_sha256": "a" * 64,
        }

    def test_append_and_read_enforce_exact_file_provenance(self) -> None:
        self.assertTrue(
            fleet_ledger.append_event(
                self.ledger,
                self.event("run-1", "running"),
                runs_dir=self.runs,
            )
        )
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.ledger.stat().st_uid, os.geteuid())
        self.assertEqual(
            fleet_ledger.latest_event(
                self.ledger,
                run_id="run-1",
                runs_dir=self.runs,
            )["status"],
            "running",
        )

    def test_trusted_empty_ledger_is_exclusive_and_cleanup_is_content_guarded(
        self,
    ) -> None:
        fleet_ledger.initialize_empty(self.ledger, runs_dir=self.runs)
        self.assertEqual(self.ledger.read_bytes(), b"")
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(fleet_ledger.LedgerError, "must be absent"):
            fleet_ledger.initialize_empty(self.ledger, runs_dir=self.runs)

        self.assertTrue(
            fleet_ledger.append_event(
                self.ledger,
                self.event("run-after-init", "running"),
                runs_dir=self.runs,
            )
        )
        before = self.ledger.read_bytes()
        with self.assertRaisesRegex(fleet_ledger.LedgerError, "content changed"):
            fleet_ledger.remove_empty(self.ledger, runs_dir=self.runs)
        self.assertEqual(self.ledger.read_bytes(), before)

        empty = self.runs / "fleet-empty.ledger.jsonl"
        fleet_ledger.initialize_empty(empty, runs_dir=self.runs)
        self.assertTrue(fleet_ledger.remove_empty(empty, runs_dir=self.runs))
        self.assertFalse(empty.exists())

    def test_trusted_empty_ledger_never_adopts_symlink_or_hardlink(self) -> None:
        outside = self.tmp / "outside-empty.jsonl"
        outside.write_bytes(b"")
        outside.chmod(0o600)
        self.ledger.symlink_to(outside)
        with self.assertRaises(fleet_ledger.LedgerError):
            fleet_ledger.initialize_empty(self.ledger, runs_dir=self.runs)
        with self.assertRaises(fleet_ledger.LedgerError):
            fleet_ledger.remove_empty(self.ledger, runs_dir=self.runs)
        self.assertTrue(self.ledger.is_symlink())
        self.assertEqual(outside.read_bytes(), b"")

        self.ledger.unlink()
        os.link(outside, self.ledger)
        with self.assertRaisesRegex(fleet_ledger.LedgerError, "link count"):
            fleet_ledger.remove_empty(self.ledger, runs_dir=self.runs)
        self.assertTrue(self.ledger.exists())
        self.assertEqual(outside.read_bytes(), b"")

    def test_explicit_runs_root_rejects_traversal_with_zero_effects(self) -> None:
        traversal_anchor = self.runs / "fleet-x"
        traversal_anchor.mkdir(mode=0o700)
        escaped = self.tmp / "escaped.ledger.jsonl"
        unsafe = traversal_anchor / ".." / ".." / escaped.name
        before = tuple(
            sorted(str(path.relative_to(self.runs)) for path in self.runs.rglob("*"))
        )

        with self.assertRaisesRegex(fleet_ledger.LedgerError, "selected runs root"):
            fleet_ledger.append_event(
                unsafe,
                self.event("run-escape", "running"),
                runs_dir=self.runs,
            )
        with self.assertRaisesRegex(fleet_ledger.LedgerError, "selected runs root"):
            fleet_ledger.read_records(unsafe, runs_dir=self.runs)
        with self.assertRaisesRegex(fleet_ledger.LedgerError, "directly beneath"):
            fleet_ledger.append_event(
                unsafe,
                self.event("run-legacy-escape", "running"),
            )

        after = tuple(
            sorted(str(path.relative_to(self.runs)) for path in self.runs.rglob("*"))
        )
        self.assertEqual(after, before)
        self.assertFalse(escaped.exists())
        self.assertEqual(list(self.runs.rglob("*.jsonl")), [])

    def test_first_terminal_event_is_immutable_under_guarded_append(self) -> None:
        self.assertTrue(
            fleet_ledger.append_event(self.ledger, self.event("run-1", "succeeded"))
        )
        before = self.ledger.read_bytes()
        self.assertFalse(
            fleet_ledger.append_event(self.ledger, self.event("run-1", "abandoned"))
        )
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(
            fleet_ledger.events_for_run(self.ledger, run_id="run-1"),
            [self.event("run-1", "succeeded")],
        )

    def test_symlink_leaf_is_rejected_for_append_and_read_without_target_mutation(
        self,
    ) -> None:
        outside = self.tmp / "outside-ledger.jsonl"
        outside.write_text('{"run_id":"external"}\n', encoding="utf-8")
        outside.chmod(0o600)
        self.ledger.symlink_to(outside)
        before = outside.read_bytes()

        with self.assertRaisesRegex(
            fleet_ledger.LedgerError, "unsafe lifecycle ledger"
        ):
            fleet_ledger.append_event(self.ledger, self.event("run-escape", "running"))
        with self.assertRaisesRegex(
            fleet_ledger.LedgerError, "unsafe lifecycle ledger"
        ):
            fleet_ledger.latest_event(self.ledger, run_id="external")

        self.assertEqual(outside.read_bytes(), before)
        self.assertTrue(self.ledger.is_symlink())

    def test_hard_link_leaf_cannot_mutate_external_inode(self) -> None:
        outside = self.tmp / "outside-hardlink.jsonl"
        outside.write_text('{"run_id":"external"}\n', encoding="utf-8")
        outside.chmod(0o600)
        os.link(outside, self.ledger)
        before = outside.read_bytes()

        with self.assertRaisesRegex(fleet_ledger.LedgerError, "link count"):
            fleet_ledger.append_event(self.ledger, self.event("run-escape", "running"))

        self.assertEqual(outside.read_bytes(), before)

    def test_mode_drift_fails_closed_for_append_and_read(self) -> None:
        self.ledger.write_text('{"run_id":"existing"}\n', encoding="utf-8")
        self.ledger.chmod(0o644)
        before = self.ledger.read_bytes()

        with self.assertRaisesRegex(fleet_ledger.LedgerError, "mode mismatch"):
            fleet_ledger.append_event(self.ledger, self.event("run-2", "running"))
        with self.assertRaisesRegex(fleet_ledger.LedgerError, "mode mismatch"):
            fleet_ledger.events_for_run(self.ledger, run_id="existing")

        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o644)

    def test_unsafe_runs_root_mode_blocks_all_ledger_access(self) -> None:
        self.runs.chmod(0o777)
        with self.assertRaisesRegex(fleet_ledger.LedgerError, "group/world writable"):
            fleet_ledger.append_event(self.ledger, self.event("run-3", "running"))
        with self.assertRaisesRegex(fleet_ledger.LedgerError, "group/world writable"):
            fleet_ledger.latest_event(self.ledger, run_id="run-3")
        self.assertFalse(self.ledger.exists())

    def test_only_selected_root_alias_is_resolved(self) -> None:
        alias = self.tmp / "runs-alias"
        alias.symlink_to(self.runs, target_is_directory=True)
        alias_ledger = alias / self.ledger.name

        self.assertTrue(
            fleet_ledger.append_event(
                alias_ledger, self.event("run-root-alias", "running")
            )
        )
        self.assertEqual(
            fleet_ledger.latest_event(alias_ledger, run_id="run-root-alias")["status"],
            "running",
        )
        self.assertTrue(self.ledger.is_file())

    def test_unsafe_ledger_name_is_rejected_before_filesystem_mutation(self) -> None:
        unsafe = self.runs / "fleet frontier.ledger.jsonl"
        with self.assertRaisesRegex(fleet_ledger.LedgerError, "safe JSONL"):
            fleet_ledger.append_event(unsafe, self.event("run-4", "running"))
        self.assertEqual(list(self.runs.iterdir()), [])

    def test_strict_reader_rejects_duplicate_keys_nonfinite_and_partial_json(
        self,
    ) -> None:
        invalid_rows = (
            b'{"run_id":"first","run_id":"second"}\n',
            b'{"run_id":"nan","tokens":NaN}\n',
            b'{"run_id":"overflow","tokens":1e999}\n',
            b'{"run_id":"surrogate-\\ud800"}\n',
            b'\xef\xbb\xbf{"run_id":"bom"}\n',
            b'{"run_id":"crlf"}\r\n',
            b'{"run_id":"first"}\n\n',
            b'{"run_id":"first"}\xe2\x80\xa8{"run_id":"second"}\n',
            b'{"run_id":"invalid-utf8-\xff"}\n',
            b'{"run_id":"partial"',
            b'{"run_id":"valid-but-not-durable"}',
        )
        for raw in invalid_rows:
            with self.subTest(raw=raw):
                self.ledger.write_bytes(raw)
                self.ledger.chmod(0o600)
                before = self.ledger.read_bytes()
                with self.assertRaises(fleet_ledger.LedgerError):
                    fleet_ledger.read_records(self.ledger)
                with self.assertRaises(fleet_ledger.LedgerError):
                    fleet_ledger.append_event(
                        self.ledger, self.event("run-after-corruption", "running")
                    )
                self.assertEqual(self.ledger.read_bytes(), before)

    def test_strict_reader_accepts_finite_floats_and_unicode_within_a_row(
        self,
    ) -> None:
        self.ledger.write_bytes(
            '{"line":"first\u2028second","ratio":1e2,"run_id":"finite"}\n'.encode()
        )
        self.ledger.chmod(0o600)
        self.assertEqual(
            fleet_ledger.read_records(self.ledger),
            [{"line": "first\u2028second", "ratio": 100.0, "run_id": "finite"}],
        )

    def test_writer_uses_canonical_json_and_rejects_values_outside_strict_domain(
        self,
    ) -> None:
        self.assertTrue(
            fleet_ledger.append_record(
                self.ledger,
                {"z": "café", "a": 1.5},
            )
        )
        self.assertEqual(
            self.ledger.read_bytes(),
            '{"a":1.5,"z":"café"}\n'.encode(),
        )

        self.ledger.unlink()
        invalid_records = (
            {"tokens": float("nan")},
            {"tokens": float("inf")},
            {"text": "invalid-\ud800"},
            {1: "coerced-key"},
        )
        for record in invalid_records:
            with self.subTest(record=record):
                with self.assertRaisesRegex(fleet_ledger.LedgerError, "strict JSON"):
                    fleet_ledger.append_record(
                        self.ledger,
                        record,  # type: ignore[arg-type]
                    )
                self.assertFalse(self.ledger.exists())

    def test_append_never_publishes_a_ledger_larger_than_its_cap(self) -> None:
        record = {"a": 1}
        encoded = b'{"a":1}\n'
        self.assertEqual(len(encoded), 8)

        with mock.patch.object(fleet_ledger, "MAX_LEDGER_BYTES", len(encoded)):
            self.assertTrue(fleet_ledger.append_record(self.ledger, record))
            before = self.ledger.read_bytes()
            self.assertEqual(before, encoded)
            with self.assertRaisesRegex(
                fleet_ledger.LedgerError, "would exceed maximum size"
            ):
                fleet_ledger.append_record(self.ledger, record)
            self.assertEqual(self.ledger.read_bytes(), before)
            self.assertEqual(fleet_ledger.read_records(self.ledger), [record])

        too_small = self.runs / "fleet-too-small.ledger.jsonl"
        with mock.patch.object(fleet_ledger, "MAX_LEDGER_BYTES", len(encoded) - 1):
            with self.assertRaisesRegex(
                fleet_ledger.LedgerError, "exceeds maximum size"
            ):
                fleet_ledger.append_record(too_small, record)
        self.assertFalse(too_small.exists())

    def test_personal_scale_terminal_guard_remains_practical(self) -> None:
        """Lock the current safe upper bound while a durable index is deferred."""

        started = time.monotonic()
        for index in range(1_000):
            self.assertTrue(
                fleet_ledger.append_event(
                    self.ledger,
                    {"run_id": f"perf-{index}", "status": "running"},
                    runs_dir=self.runs,
                )
            )
        elapsed = time.monotonic() - started

        self.assertEqual(
            len(fleet_ledger.read_records(self.ledger, runs_dir=self.runs)),
            1_000,
        )
        self.assertLess(
            elapsed,
            8.0,
            f"1,000 guarded lifecycle events took {elapsed:.3f}s",
        )


if __name__ == "__main__":
    unittest.main()
