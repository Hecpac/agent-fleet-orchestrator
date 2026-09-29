"""Stage 1 S5: local delivery with rename-without-replacement and read-back."""
from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_herdr_archive
import fleet_herdr_delivery as delivery

MID = "c1529b8b-8496-5bbd-b6fb-33d848b0fa0a"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fleet-delivery-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repo = self.base / "candidate"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "sample_stats.py").write_text("def stats(values):\n    return {}\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        self.head = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "tools").mkdir()
        (self.repo / "tools" / "run.sh").write_text("#!/bin/sh\necho ok\n")
        (self.repo / "tools" / "run.sh").chmod(0o755)
        (self.repo / "sample_stats.py").write_text("def stats(values):\n    return {'count': len(values)}\n")
        self.tree_sha, self.tree, _ = fleet_herdr_archive.snapshot(self.repo, expected_base=self.head)
        self.root = self.base / "outputs" / "sdd-deliveries" / "owner-loop-v0"
        self.runs = self.base / "runs"
        self.runs.mkdir()

    def deliver(self, **kwargs):
        return delivery.deliver(self.root, MID, 2, self.tree_sha, self.tree, runs_dir=self.runs, **kwargs)

    def final(self):
        return delivery.final_path(self.root, MID, 2, self.tree_sha)

    def listing(self):
        return sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*") if "candidate" not in p.parts)

    def test_publishes_exactly_the_accepted_tree_under_its_revision_path(self):
        receipt = self.deliver()
        self.assertEqual((receipt["status"], receipt["written"], receipt["files"]), ("delivered", True, 2))
        final = self.final()
        self.assertEqual(final.name, f"2-{self.tree_sha}")
        self.assertEqual((final / "sample_stats.py").read_text(), "def stats(values):\n    return {'count': len(values)}\n")
        self.assertEqual((final / "tools" / "run.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual((final / "sample_stats.py").stat().st_mode & 0o777, 0o644)
        self.assertEqual(delivery.read_back(final), delivery.manifest(delivery.tree_files(self.tree)))
        self.assertFalse(delivery.staging_path(self.root, MID, 2, self.tree_sha).exists())
        outside = [p for p in self.listing() if not p.startswith(f"outputs/sdd-deliveries/owner-loop-v0/{MID}")]
        self.assertEqual(outside, ["outputs", "outputs/sdd-deliveries", "outputs/sdd-deliveries/owner-loop-v0", "runs"])

    def test_repeated_delivery_is_recognized_without_a_second_write(self):
        first = self.deliver()
        before = (self.final() / "sample_stats.py").stat().st_mtime_ns
        second = self.deliver()
        self.assertEqual((second["status"], second["written"]), ("delivered", False))
        self.assertEqual(second["manifest_sha256"], first["manifest_sha256"])
        self.assertEqual((self.final() / "sample_stats.py").stat().st_mtime_ns, before)

    def test_existing_different_or_empty_destination_is_a_collision_left_intact(self):
        for label, prepare in (("different", lambda f: (f.mkdir(parents=True), (f / "other.txt").write_text("keep\n"))),
                               ("empty", lambda f: f.mkdir(parents=True))):
            with self.subTest(label=label):
                final = self.final()
                if final.exists():
                    for child in final.iterdir():
                        child.unlink()
                    final.rmdir()
                prepare(final)
                snapshot = sorted(p.name for p in final.iterdir())
                receipt = self.deliver()
                self.assertEqual((receipt["status"], receipt["reason"], receipt["written"]),
                                 ("collision", "delivery_collision", False))
                self.assertEqual(sorted(p.name for p in final.iterdir()), snapshot)

    def test_concurrent_creation_before_rename_never_replaces(self):
        def identical(final):
            # Another process publishes the same revision directly at the final path.
            for path, (mode, raw) in delivery.tree_files(self.tree).items():
                target = final / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                target.chmod(mode)

        receipt = self.deliver(before_publish=identical)
        self.assertEqual((receipt["status"], receipt["written"]), ("delivered", False))

        self.root = self.base / "other-root"

        def foreign(final):
            final.mkdir(parents=True)
            (final / "foreign.txt").write_text("someone else\n")

        receipt = self.deliver(before_publish=foreign)
        self.assertEqual(receipt["status"], "collision")
        self.assertEqual([p.name for p in self.final().iterdir()], ["foreign.txt"])
        self.assertFalse(delivery.staging_path(self.root, MID, 2, self.tree_sha).exists())

    def test_missing_primitive_blocks_without_publishing(self):
        with mock.patch.object(delivery, "_rename_noreplace",
                               side_effect=delivery.PrimitiveUnavailable("absent")):
            receipt = self.deliver()
        self.assertEqual((receipt["status"], receipt["reason"]), ("blocked", "delivery_primitive_unavailable"))
        self.assertFalse(self.final().exists())
        self.assertFalse(delivery.staging_path(self.root, MID, 2, self.tree_sha).exists())

    def test_the_platform_primitive_refuses_an_existing_destination(self):
        source, target = self.base / "src", self.base / "dst"
        source.mkdir()
        target.mkdir()
        with self.assertRaises(OSError) as raised:
            delivery._rename_noreplace(source, target)
        self.assertIn(raised.exception.errno, {17, 66})  # EEXIST, ENOTEMPTY
        self.assertTrue(source.exists() and target.exists())

    def test_reconcile_after_restart(self):
        self.assertEqual(delivery.reconcile(self.root, MID, 2, self.tree_sha, self.tree, runs_dir=self.runs)["status"], "retry")
        staging = delivery.staging_path(self.root, MID, 2, self.tree_sha)
        staging.mkdir(parents=True)
        (staging / "partial").write_text("x")
        self.assertEqual(delivery.reconcile(self.root, MID, 2, self.tree_sha, self.tree, runs_dir=self.runs)["status"], "retry")
        self.assertFalse(staging.exists())
        self.deliver()
        self.assertEqual(delivery.reconcile(self.root, MID, 2, self.tree_sha, self.tree, runs_dir=self.runs)["status"], "delivered")
        (self.final() / "sample_stats.py").write_text("changed\n")
        receipt = delivery.reconcile(self.root, MID, 2, self.tree_sha, self.tree, runs_dir=self.runs)
        self.assertEqual((receipt["status"], receipt["reason"]), ("indeterminate", "destination_changed_after_start"))

    def test_root_is_rechecked_against_runs_and_symbolic_links(self):
        with self.assertRaisesRegex(delivery.DeliveryError, "outside the runs"):
            delivery.deliver(self.runs / "deliveries", MID, 2, self.tree_sha, self.tree, runs_dir=self.runs)
        real = self.base / "real"
        real.mkdir()
        os.symlink(real, self.base / "alias")
        with self.assertRaisesRegex(delivery.DeliveryError, "no longer resolves"):
            delivery.deliver(self.base / "alias" / "root", MID, 2, self.tree_sha, self.tree, runs_dir=self.runs)
        self.assertEqual(list(real.iterdir()), [])

    def test_uncommitted_work_elsewhere_does_not_block_delivery(self):
        repo = self.base / "outputs"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "unrelated.txt").write_text("uncommitted\n")
        receipt = self.deliver()
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual((repo / "unrelated.txt").read_text(), "uncommitted\n")

    def test_tree_with_links_or_escapes_is_rejected(self):
        for kind, name in ((tarfile.SYMTYPE, "link"), (tarfile.LNKTYPE, "hard"), (tarfile.REGTYPE, "../escape")):
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
                info = tarfile.TarInfo(name)
                info.type = kind
                info.linkname = "sample_stats.py" if kind != tarfile.REGTYPE else ""
                info.pax_headers = {"fleet.git_mode": "100644"}
                tar.addfile(info, io.BytesIO())
            with self.subTest(name=name), self.assertRaises(delivery.DeliveryError):
                delivery.tree_files(raw.getvalue())


if __name__ == "__main__":
    unittest.main()
