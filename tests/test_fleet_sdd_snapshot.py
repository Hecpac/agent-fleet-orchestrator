"""Provider-free tests for the standalone SDD stage-2 plan snapshot primitive.

No Git, Docker, providers, Herdr or driver integration are exercised here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.fleet_sdd_snapshot as snapshot  # noqa: E402
from scripts.fleet_sdd_snapshot import SnapshotError, freeze_plan, load_plan  # noqa: E402

SCRIPT = ROOT / "scripts/fleet_sdd_snapshot.py"
EXAMPLE = ROOT / "examples/sdd/deny-before-effect.json"


def _example_document() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = self.root / "store"
        self.store.mkdir(mode=0o700)
        self.mid = str(uuid.uuid4())

    def plan_bytes(self, *, objective: str | None = None) -> bytes:
        document = _example_document()
        if objective is not None:
            document["objective"] = objective
        return json.dumps(document, sort_keys=True).encode("utf-8")

    def write_source(self, name: str, raw: bytes) -> Path:
        path = self.root / name
        path.write_bytes(raw)
        return path

    def cli(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *arguments],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )

    # -- happy path and recovery -------------------------------------------

    def test_freeze_and_load_round_trip_preserves_exact_bytes(self) -> None:
        raw = self.plan_bytes()
        source = self.write_source("plan.json", raw)
        manifest = freeze_plan(self.store, self.mid, source)
        self.assertEqual(set(manifest), set(snapshot.MANIFEST_FIELDS))
        self.assertEqual(manifest["schema"], "fleet.sdd.snapshot.v1")
        self.assertEqual(manifest["mission_id"], self.mid)
        self.assertEqual(manifest["functional_status"], "NOT_VERIFIED")
        self.assertEqual(manifest["plan_sha256"], hashlib.sha256(raw).hexdigest())
        blob = self.store / "sdd/blobs" / f"{manifest['plan_sha256']}.json"
        self.assertEqual(blob.read_bytes(), raw)
        self.assertEqual(load_plan(self.store, self.mid, manifest["plan_sha256"]), json.loads(raw))

    def test_recovery_in_fresh_process_after_source_deleted(self) -> None:
        raw = self.plan_bytes(objective="Recuperar sin releer la fuente mutable.")
        source = self.write_source("plan.json", raw)
        manifest = freeze_plan(self.store, self.mid, source)
        source.unlink()
        run = self.cli(
            "load", "--store", str(self.store), "--mission-id", self.mid,
            "--sha256", manifest["plan_sha256"],
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), json.loads(raw))

    def test_late_load_needs_no_source_even_for_new_parser_instance(self) -> None:
        raw = self.plan_bytes()
        source = self.write_source("plan.json", raw)
        manifest = freeze_plan(self.store, self.mid, source)
        source.unlink()
        recovered = load_plan(self.store, self.mid, manifest["plan_sha256"])
        self.assertEqual(recovered["objective"], json.loads(raw)["objective"])

    # -- idempotency, conflicts, cross-mission ------------------------------

    def test_repeat_freeze_is_idempotent_and_conflict_fails_closed(self) -> None:
        raw = self.plan_bytes()
        source = self.write_source("plan.json", raw)
        first = freeze_plan(self.store, self.mid, source)
        second = freeze_plan(self.store, self.mid, source)
        self.assertEqual(first, second)
        binding = self.store / "sdd/missions" / f"{self.mid}.json"
        self.assertEqual(json.loads(binding.read_text())["plan_sha256"], first["plan_sha256"])
        other = self.write_source("other.json", self.plan_bytes(objective="Otro plan distinto."))
        with self.assertRaises(SnapshotError):
            freeze_plan(self.store, self.mid, other)
        self.assertEqual(json.loads(binding.read_text())["plan_sha256"], first["plan_sha256"])

    def test_same_plan_two_missions_share_blob_with_independent_bindings(self) -> None:
        raw = self.plan_bytes()
        source = self.write_source("plan.json", raw)
        other_mid = str(uuid.uuid4())
        first = freeze_plan(self.store, self.mid, source)
        second = freeze_plan(self.store, other_mid, source)
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        blobs = list((self.store / "sdd/blobs").glob("*.json"))
        self.assertEqual(len(blobs), 1)
        a = self.store / "sdd/missions" / f"{self.mid}.json"
        b = self.store / "sdd/missions" / f"{other_mid}.json"
        self.assertNotEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(load_plan(self.store, other_mid, second["plan_sha256"]), json.loads(raw))

    # -- identifiers and inputs --------------------------------------------

    def test_malformed_identifiers_rejected_without_mutation(self) -> None:
        raw = self.plan_bytes()
        source = self.write_source("plan.json", raw)
        bad_mission_ids = [
            "",
            "not-a-uuid",
            str(uuid.uuid4()).upper(),
            "{" + str(uuid.uuid4()) + "}",
            "../" + str(uuid.uuid4()),
        ]
        for bad in bad_mission_ids:
            with self.subTest(mission_id=bad), self.assertRaises(SnapshotError):
                freeze_plan(self.store, bad, source)
            with self.subTest(mission_id=bad), self.assertRaises(SnapshotError):
                load_plan(self.store, bad, "0" * 64)
        self.assertFalse((self.store / "sdd").exists())
        for bad_sha in ["", "0" * 63, "A" * 64, "../" + "0" * 62]:
            with self.subTest(sha256=bad_sha), self.assertRaises(SnapshotError):
                load_plan(self.store, self.mid, bad_sha)

    def test_duplicate_keys_constants_and_invalid_plans_rejected(self) -> None:
        base = self.plan_bytes()
        text = base.decode("utf-8")
        duplicate = self.write_source("dup.json", (text[:-1] + ',"objective":"again"}').encode())
        with self.assertRaises(SnapshotError):
            freeze_plan(self.store, self.mid, duplicate)
        non_json = self.write_source("nan.json", (text[:-1] + ',"extra":NaN}').encode())
        with self.assertRaises(SnapshotError):
            freeze_plan(self.store, self.mid, non_json)
        broken = _example_document()
        del broken["checks"]
        invalid = self.write_source("invalid.json", json.dumps(broken, sort_keys=True).encode())
        with self.assertRaises(SnapshotError):
            freeze_plan(self.store, self.mid, invalid)
        self.assertFalse((self.store / "sdd").exists())

    def test_foreign_schema_plan_rejected_by_sdd_validation(self) -> None:
        document = _example_document()
        document["schema"] = "fleet.sdd.plan.v2"
        source = self.write_source("future.json", json.dumps(document, sort_keys=True).encode())
        with self.assertRaises(SnapshotError):
            freeze_plan(self.store, self.mid, source)

    # -- integrity and tampering -------------------------------------------

    def test_tampering_wrong_pin_and_foreign_manifest_fail_closed(self) -> None:
        raw = self.plan_bytes()
        source = self.write_source("plan.json", raw)
        manifest = freeze_plan(self.store, self.mid, source)
        digest = manifest["plan_sha256"]
        blob = self.store / "sdd/blobs" / f"{digest}.json"
        original_blob = blob.read_bytes()
        blob.write_bytes(original_blob + b" ")
        with self.assertRaises(SnapshotError):
            load_plan(self.store, self.mid, digest)
        blob.write_bytes(original_blob)
        with self.assertRaises(SnapshotError):
            load_plan(self.store, self.mid, "0" * 64)
        binding = self.store / "sdd/missions" / f"{self.mid}.json"
        original_binding = binding.read_bytes()
        binding.write_bytes(b'{"schema":')
        with self.assertRaises(SnapshotError):
            load_plan(self.store, self.mid, digest)
        foreign = json.loads(original_binding)
        foreign["mission_id"] = str(uuid.uuid4())
        binding.write_bytes(json.dumps(foreign, sort_keys=True).encode())
        with self.assertRaises(SnapshotError):
            load_plan(self.store, self.mid, digest)
        unsupported = json.loads(original_binding)
        unsupported["schema"] = "fleet.sdd.snapshot.v2"
        binding.write_bytes(json.dumps(unsupported, sort_keys=True).encode())
        with self.assertRaises(SnapshotError):
            load_plan(self.store, self.mid, digest)
        binding.write_bytes(original_binding)
        self.assertEqual(load_plan(self.store, self.mid, digest), json.loads(raw))

    def test_missing_store_and_unreferenced_blob_never_expose_a_plan(self) -> None:
        digest = hashlib.sha256(self.plan_bytes()).hexdigest()
        with self.assertRaises(SnapshotError):
            load_plan(self.store / "absent", self.mid, digest)
        blobs = self.store / "sdd/blobs"
        blobs.mkdir(parents=True, mode=0o700)
        (blobs / f"{digest}.json").write_bytes(self.plan_bytes())
        with self.assertRaises(SnapshotError):
            load_plan(self.store, self.mid, digest)

    # -- source I/O hardening ---------------------------------------------

    def test_writerless_fifo_source_is_rejected_promptly(self) -> None:
        fifo = self.root / "plan.fifo"
        os.mkfifo(fifo)
        absent_store = self.store / "absent"
        run = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "freeze", "--store", str(absent_store),
             "--mission-id", self.mid, "--plan", str(fifo)],
            capture_output=True, text=True, cwd=str(ROOT), timeout=2,
        )
        self.assertEqual(run.returncode, 2, (run.stdout, run.stderr))
        self.assertEqual(run.stdout, "")
        self.assertIn("snapshot error", run.stderr)
        self.assertFalse(absent_store.exists())

    def test_source_read_oserror_becomes_snapshterror_and_closes_fd(self) -> None:
        source = self.write_source("plan.json", self.plan_bytes())
        real_close = os.close
        closed_fds: list[int] = []

        def tracking_close(fd: int) -> None:
            closed_fds.append(fd)
            real_close(fd)

        with mock.patch.object(snapshot.os, "read",
                               side_effect=OSError("injected source read error")), \
                mock.patch.object(snapshot.os, "close", side_effect=tracking_close):
            with self.assertRaises(SnapshotError) as caught:
                freeze_plan(self.store, self.mid, source)
        self.assertIn("injected source read error", str(caught.exception))
        self.assertTrue(closed_fds)
        self.assertFalse((self.store / "sdd").exists())

    def test_non_regular_and_uninspectable_source_rejected(self) -> None:
        directory = self.root / "plan-dir"
        directory.mkdir(mode=0o700)
        with self.assertRaises(SnapshotError):
            freeze_plan(self.store, self.mid, directory)
        source = self.write_source("plan.json", self.plan_bytes())
        with mock.patch.object(snapshot.os, "fstat",
                               side_effect=OSError("injected fstat error")):
            with self.assertRaises(SnapshotError) as caught:
                freeze_plan(self.store, self.mid, source)
        self.assertIn("injected fstat error", str(caught.exception))
        self.assertFalse((self.store / "sdd").exists())

    # -- symlinks ----------------------------------------------------------
    def test_symlinked_store_components_rejected(self) -> None:
        raw = self.plan_bytes()
        source = self.write_source("plan.json", raw)
        link = self.root / "store-link"
        os.symlink(self.store, link)
        with self.assertRaises(SnapshotError):
            freeze_plan(link, self.mid, source)
        external = self.root / "external"
        external.mkdir(mode=0o700)
        sdd_link = self.store / "sdd"
        os.symlink(external, sdd_link)
        with self.assertRaises(SnapshotError):
            freeze_plan(self.store, self.mid, source)
        sdd_link.unlink()

    def test_symlinked_manifest_and_blob_rejected(self) -> None:
        raw = self.plan_bytes()
        source = self.write_source("plan.json", raw)
        manifest = freeze_plan(self.store, self.mid, source)
        digest = manifest["plan_sha256"]
        binding = self.store / "sdd/missions" / f"{self.mid}.json"
        binding.unlink()
        os.symlink(source, binding)
        with self.assertRaises(SnapshotError):
            load_plan(self.store, self.mid, digest)
        binding.unlink()
        blob = self.store / "sdd/blobs" / f"{digest}.json"
        target = self.write_source("blob-copy.json", raw)
        blob.unlink()
        os.symlink(target, blob)
        with self.assertRaises(SnapshotError):
            load_plan(self.store, self.mid, digest)

    # -- CLI and bounded concurrency ---------------------------------------

    def test_cli_freeze_and_load_output_json_and_reject_cleanly(self) -> None:
        raw = self.plan_bytes()
        source = self.write_source("plan.json", raw)
        frozen = self.cli(
            "freeze", "--store", str(self.store), "--mission-id", self.mid, "--plan", str(source)
        )
        self.assertEqual(frozen.returncode, 0, frozen.stderr)
        manifest = json.loads(frozen.stdout)
        loaded = self.cli(
            "load", "--store", str(self.store), "--mission-id", self.mid,
            "--sha256", manifest["plan_sha256"],
        )
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertEqual(json.loads(loaded.stdout), json.loads(raw))
        rejected = self.cli(
            "load", "--store", str(self.store), "--mission-id", self.mid, "--sha256", "0" * 64
        )
        self.assertEqual(rejected.stdout, "")
        self.assertEqual(rejected.returncode, 2)
        self.assertTrue(rejected.stderr.strip())

    def test_concurrent_first_writers_bind_exactly_one_digest(self) -> None:
        plan_a = self.plan_bytes(objective="Escritor A.")
        plan_b = self.plan_bytes(objective="Escritor B.")
        source_a = self.write_source("a.json", plan_a)
        source_b = self.write_source("b.json", plan_b)
        command = [sys.executable, "-B", str(SCRIPT), "freeze", "--store", str(self.store),
                   "--mission-id", self.mid]
        first = subprocess.Popen(command + ["--plan", str(source_a)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(ROOT))
        second = subprocess.Popen(command + ["--plan", str(source_b)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(ROOT))
        first.communicate()
        second.communicate()
        self.assertEqual(sorted([first.returncode, second.returncode]), [0, 2])
        digest_a = hashlib.sha256(plan_a).hexdigest()
        digest_b = hashlib.sha256(plan_b).hexdigest()
        binding = json.loads((self.store / "sdd/missions" / f"{self.mid}.json").read_text())
        bound = binding["plan_sha256"]
        self.assertIn(bound, {digest_a, digest_b})
        expected = json.loads(plan_a if bound == digest_a else plan_b)
        self.assertEqual(load_plan(self.store, self.mid, bound), expected)


if __name__ == "__main__":
    unittest.main()
