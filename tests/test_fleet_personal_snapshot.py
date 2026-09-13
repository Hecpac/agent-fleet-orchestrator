from pathlib import Path
import tempfile
import unittest
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_personal_snapshot as snapshots


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        snapshots.git(self.source, "init")
        (self.source / "answer.py").write_text("old\n")
        (self.source / "deleted.txt").write_text("remove me\n")
        (self.source / ".gitignore").write_text("cache/\n")
        snapshots.git(self.source, "add", ".")
        snapshots.git(self.source, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost.invalid",
                      "commit", "--no-gpg-sign", "-m", "fixture")

    def test_preserves_dirty_bytes_deletions_and_source_git_without_ignored_inputs(self):
        (self.source / "answer.py").write_text("modified\n")
        (self.source / "deleted.txt").unlink()
        (self.source / "untracked.txt").write_text("new\n")
        (self.source / "cache").mkdir()
        (self.source / "cache" / "token.txt").write_text("synthetic excluded value")
        before = snapshots.inventory(self.source)
        source_index = (self.source / ".git" / "index").read_bytes()
        result = snapshots.create(self.source, self.root / "snapshot", exclude_ignored=True)
        target = Path(result["target"])
        self.assertEqual((target / "answer.py").read_text(), "modified\n")
        self.assertEqual((target / "untracked.txt").read_text(), "new\n")
        self.assertFalse((target / "deleted.txt").exists())
        self.assertFalse((target / "cache").exists())
        self.assertEqual(snapshots.inventory(self.source), before)
        self.assertEqual((self.source / ".git" / "index").read_bytes(), source_index)
        self.assertEqual(snapshots.git(target, "status", "--porcelain"), b"")
        self.assertEqual(snapshots.git(target, "remote"), b"")

    def test_ignored_policy_is_required_before_creating_output(self):
        (self.source / "cache").mkdir()
        (self.source / "cache" / "input").write_text("excluded")
        output = self.root / "snapshot"
        with self.assertRaisesRegex(snapshots.SnapshotError, "exclude-ignored"):
            snapshots.create(self.source, output)
        self.assertFalse(output.exists())

    def test_source_drift_never_produces_a_verified_receipt(self):
        original = snapshots.inventory
        calls = 0
        def inspect(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.source / "answer.py").write_text("concurrent change")
            return original(path)
        output = self.root / "snapshot"
        with mock.patch.object(snapshots, "inventory", side_effect=inspect):
            with self.assertRaisesRegex(snapshots.SnapshotError, "source changed"):
                snapshots.create(self.source, output)
        self.assertFalse((output / "snapshot.json").exists())
        self.assertEqual((self.source / "answer.py").read_text(), "concurrent change")

    def test_existing_output_and_source_alias_are_rejected(self):
        output = self.root / "existing"
        output.mkdir()
        (output / "user.txt").write_text("preserve")
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.create(self.source, output)
        self.assertEqual((output / "user.txt").read_text(), "preserve")
        (self.source / "alias").symlink_to(self.root)
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.create(self.source, self.root / "snapshot")


if __name__ == "__main__":
    unittest.main()
