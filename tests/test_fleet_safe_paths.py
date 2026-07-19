from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_safe_paths  # noqa: E402


class FleetSafePathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)

    def private_directory(self, name: str) -> Path:
        path = self.tmp / name
        path.mkdir(mode=0o700)
        path.chmod(0o700)
        return path

    def test_canonical_root_accepts_trusted_alias_then_rejects_descendant_symlink(self) -> None:
        physical = self.private_directory("physical")
        alias = self.tmp / "alias"
        alias.symlink_to(physical, target_is_directory=True)

        self.assertEqual(
            fleet_safe_paths.canonical_root(alias, required_mode=0o700),
            physical.resolve(),
        )
        outside = self.private_directory("outside")
        (physical / "private").symlink_to(outside, target_is_directory=True)
        with fleet_safe_paths.RootedFS(alias, root_mode=0o700) as rooted:
            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "cannot open rooted directory"
            ):
                rooted.atomic_write(
                    "private/result.txt",
                    b"must stay rooted\n",
                    directory_modes=(0o700,),
                )
        self.assertFalse((outside / "result.txt").exists())

    def test_created_directories_file_and_lock_have_exact_modes_and_owner(self) -> None:
        root = self.private_directory("runs")
        with self.assertRaisesRegex(
            fleet_safe_paths.SafePathError, "unexpected owner"
        ):
            fleet_safe_paths.RootedFS(
                root,
                owner_uid=os.geteuid() + 1,
                root_mode=0o700,
            )
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            result = rooted.atomic_write(
                "missions/mission/artifacts/value",
                b"artifact bytes\n",
                directory_modes=(0o700, 0o700, 0o700),
            )
            self.assertEqual(
                rooted.read_regular(
                    "missions/mission/artifacts/value",
                    directory_modes=(0o700, 0o700, 0o700),
                    max_bytes=1024,
                ),
                b"artifact bytes\n",
            )
            with rooted.exclusive_lock(
                "missions/mission/.artifacts.lock",
                directory_modes=(0o700, 0o700),
            ):
                lock = root / "missions" / "mission" / ".artifacts.lock"
                self.assertTrue(lock.is_file())
                self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
                self.assertEqual(lock.stat().st_uid, os.geteuid())
            with rooted.shared_lock(
                "missions/mission/.artifacts.lock",
                directory_modes=(0o700, 0o700),
            ):
                self.assertTrue(lock.is_file())

            missing = root / "missions" / "mission" / ".missing.lock"
            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "cannot open rooted shared lock"
            ):
                with rooted.shared_lock(
                    "missions/mission/.missing.lock",
                    directory_modes=(0o700, 0o700),
                ):
                    pass
            self.assertFalse(missing.exists())

        for directory in (
            root / "missions",
            root / "missions" / "mission",
            root / "missions" / "mission" / "artifacts",
        ):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(directory.stat().st_uid, os.geteuid())
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
        self.assertEqual(result.stat().st_uid, os.geteuid())

        (root / "missions" / "mission" / "artifacts").chmod(0o755)
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "directory mode mismatch"
            ):
                rooted.read_regular(
                    "missions/mission/artifacts/value",
                    directory_modes=(0o700, 0o700, 0o700),
                )

    def test_open_root_descriptor_survives_pathname_substitution_without_escape(self) -> None:
        root = self.private_directory("runs")
        secure = root / "secure"
        secure.mkdir(mode=0o700)
        secure.chmod(0o700)
        outside = self.private_directory("outside")
        moved = self.tmp / "pinned-runs"

        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            root.rename(moved)
            root.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "trusted root binding changed"
            ):
                rooted.atomic_write(
                    "secure/result.txt",
                    b"must not claim durable success\n",
                    directory_modes=(0o700,),
                )
            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "trusted root binding changed"
            ):
                rooted.assert_root_binding()

        self.assertFalse((moved / "secure" / "result.txt").exists())
        self.assertFalse((outside / "secure" / "result.txt").exists())

    def test_conflict_fails_closed_and_cleans_temporary_file(self) -> None:
        root = self.private_directory("runs")
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            rooted.atomic_write(
                "results/feature/run.txt",
                b"first\n",
                directory_modes=(0o755, 0o700),
            )
            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "conflicts with requested bytes"
            ):
                rooted.atomic_write(
                    "results/feature/run.txt",
                    b"second\n",
                    directory_modes=(0o755, 0o700),
                )
            self.assertEqual(
                rooted.list_directory(
                    "results/feature",
                    directory_modes=(0o755, 0o700),
                ),
                ["run.txt"],
            )
        self.assertEqual((root / "results" / "feature" / "run.txt").read_bytes(), b"first\n")

    def test_failed_atomic_publication_removes_temporary_file(self) -> None:
        root = self.private_directory("runs")
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            with mock.patch.object(
                fleet_safe_paths,
                "_rename_noreplace",
                side_effect=OSError("forced publication failure"),
            ), self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "cannot publish rooted file"
            ):
                rooted.atomic_write(
                    "results/feature/run.txt",
                    b"unpublished\n",
                    directory_modes=(0o755, 0o700),
                )
            self.assertEqual(
                rooted.list_directory(
                    "results/feature",
                    directory_modes=(0o755, 0o700),
                ),
                [],
            )

    def test_atomic_publication_recovers_exact_sigkill_windows_without_hardlink(self) -> None:
        for checkpoint in (
            "after_atomic_partial_write",
            "after_atomic_pending_fsync",
            "after_atomic_rename",
        ):
            with self.subTest(checkpoint=checkpoint):
                root = self.private_directory(f"runs-{checkpoint}")
                script = """
from pathlib import Path
import sys
from fleet_safe_paths import RootedFS
root = Path(sys.argv[1])
with RootedFS(root, root_mode=0o700) as rooted:
    rooted.atomic_write(
        "results/feature/run.txt",
        b"durable bytes\\n",
        directory_modes=(0o755, 0o700),
    )
"""
                environment = os.environ.copy()
                environment["PYTHONPATH"] = str(ROOT / "scripts")
                environment["FLEET_TEST_SAFE_PATH_CRASH_AT"] = checkpoint
                crashed = subprocess.run(
                    [sys.executable, "-c", script, str(root)],
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertNotEqual(crashed.returncode, 0)

                with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
                    result = rooted.atomic_write(
                        "results/feature/run.txt",
                        b"durable bytes\n",
                        directory_modes=(0o755, 0o700),
                    )
                    self.assertEqual(
                        rooted.list_directory(
                            "results/feature",
                            directory_modes=(0o755, 0o700),
                        ),
                        ["run.txt"],
                    )
                self.assertEqual(result.read_bytes(), b"durable bytes\n")
                self.assertEqual(result.stat().st_nlink, 1)

    def test_identical_atomic_retry_syncs_parent_directory(self) -> None:
        root = self.private_directory("runs-identical-retry")
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            rooted.atomic_write(
                "results/feature/run.txt",
                b"durable bytes\n",
                directory_modes=(0o755, 0o700),
            )
            real_fsync = os.fsync
            with mock.patch.object(
                fleet_safe_paths.os,
                "fsync",
                wraps=real_fsync,
            ) as sync:
                rooted.atomic_write(
                    "results/feature/run.txt",
                    b"durable bytes\n",
                    directory_modes=(0o755, 0o700),
                )
            self.assertEqual(sync.call_count, 1)

    def test_atomic_publication_replaces_only_valid_aborted_partial_pending(self) -> None:
        root = self.private_directory("runs-partial-pending")
        parent = root / "results" / "feature"
        parent.mkdir(parents=True)
        (root / "results").chmod(0o755)
        parent.chmod(0o700)
        leaf = "run.txt"
        content = b"complete durable bytes\n"
        pending = parent / fleet_safe_paths._atomic_pending_name(
            leaf,
            content,
        )
        pending.write_bytes(b"partial")
        pending.chmod(0o600)

        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            result = rooted.atomic_write(
                "results/feature/run.txt",
                content,
                directory_modes=(0o755, 0o700),
            )

        self.assertEqual(result.read_bytes(), b"complete durable bytes\n")
        self.assertEqual(result.stat().st_nlink, 1)
        self.assertFalse(pending.exists())

    def test_atomic_publication_preserves_complete_different_content_pending(self) -> None:
        root = self.private_directory("runs-conflicting-pending")
        parent = root / "results" / "feature"
        parent.mkdir(parents=True)
        (root / "results").chmod(0o755)
        parent.chmod(0o700)
        leaf = "run.txt"
        prior = b"prior complete bytes\n"
        pending = parent / fleet_safe_paths._atomic_pending_name(leaf, prior)
        pending.write_bytes(prior)
        pending.chmod(0o600)

        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError,
                "pending publication conflicts",
            ):
                rooted.atomic_write(
                    "results/feature/run.txt",
                    b"different retry bytes\n",
                    directory_modes=(0o755, 0o700),
                )

        self.assertEqual(pending.read_bytes(), prior)
        self.assertEqual(pending.stat().st_nlink, 1)
        self.assertFalse((parent / leaf).exists())

    def test_atomic_publication_serializes_same_and_conflicting_processes(self) -> None:
        script = """
from pathlib import Path
import sys
from fleet_safe_paths import RootedFS
with RootedFS(Path(sys.argv[1]), root_mode=0o700) as rooted:
    rooted.atomic_write(
        "results/feature/run.txt",
        sys.argv[2].encode(),
        directory_modes=(0o755, 0o700),
    )
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "scripts")
        for label, payloads, expected_successes in (
            ("same", ("same", "same"), 2),
            ("different", ("first", "second"), 1),
        ):
            with self.subTest(label=label):
                root = self.private_directory(f"runs-concurrent-{label}")
                parent = root / "results" / "feature"
                parent.mkdir(parents=True)
                (root / "results").chmod(0o755)
                parent.chmod(0o700)
                processes = [
                    subprocess.Popen(
                        [sys.executable, "-c", script, str(root), payload],
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    for payload in payloads
                ]
                results = [process.communicate(timeout=10) for process in processes]
                returncodes = [process.returncode for process in processes]
                self.assertEqual(returncodes.count(0), expected_successes, results)
                published = parent / "run.txt"
                self.assertIn(published.read_text(), payloads)
                self.assertEqual(published.stat().st_nlink, 1)

    def test_atomic_publication_still_rejects_external_hardlink(self) -> None:
        root = self.private_directory("runs-hardlink")
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            result = rooted.atomic_write(
                "results/feature/run.txt",
                b"durable bytes\n",
                directory_modes=(0o755, 0o700),
            )
            outside = self.tmp / "outside-hardlink"
            os.link(result, outside)
            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "unexpected link count"
            ):
                rooted.atomic_write(
                    "results/feature/run.txt",
                    b"durable bytes\n",
                    directory_modes=(0o755, 0o700),
                )
        self.assertEqual(outside.read_bytes(), b"durable bytes\n")

    def test_append_is_durable_and_rejects_symlink_leaf(self) -> None:
        root = self.private_directory("runs")
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            rooted.append_regular(
                "missions/mission/mission.jsonl",
                b"first\n",
                directory_modes=(0o700, 0o700),
            )
            rooted.append_regular(
                "missions/mission/mission.jsonl",
                b"second\n",
                directory_modes=(0o700, 0o700),
            )
            self.assertEqual(
                rooted.read_regular(
                    "missions/mission/mission.jsonl",
                    directory_modes=(0o700, 0o700),
                ),
                b"first\nsecond\n",
            )

            ledger = root / "missions" / "mission" / "mission.jsonl"
            ledger.unlink()
            outside = root / "outside"
            outside.write_bytes(b"outside\n")
            outside.chmod(0o600)
            ledger.symlink_to(outside)
            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "cannot open rooted append file"
            ):
                rooted.append_regular(
                    "missions/mission/mission.jsonl",
                    b"escaped\n",
                    directory_modes=(0o700, 0o700),
                )
            self.assertEqual(outside.read_bytes(), b"outside\n")

    def test_replace_optional_read_and_unlink_are_descriptor_anchored(self) -> None:
        root = self.private_directory("runs")
        control = root / "missions" / "mission" / "control"
        control.mkdir(parents=True, mode=0o700)
        for directory in (control.parent.parent, control.parent, control):
            directory.chmod(0o700)
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            self.assertIsNone(
                rooted.read_regular_optional(
                    "missions/mission/control/lifecycle.json",
                    directory_modes=(0o700, 0o700, 0o700),
                )
            )
            lifecycle = rooted.replace_regular(
                "missions/mission/control/lifecycle.json",
                b'{"status":"running"}\n',
                directory_modes=(0o700, 0o700, 0o700),
            )
            rooted.replace_regular(
                "missions/mission/control/lifecycle.json",
                b'{"status":"stopped"}\n',
                directory_modes=(0o700, 0o700, 0o700),
            )
            self.assertEqual(lifecycle.read_bytes(), b'{"status":"stopped"}\n')
            self.assertTrue(
                rooted.unlink_regular(
                    "missions/mission/control/lifecycle.json",
                    directory_modes=(0o700, 0o700, 0o700),
                )
            )
            self.assertFalse(
                rooted.unlink_regular(
                    "missions/mission/control/lifecycle.json",
                    directory_modes=(0o700, 0o700, 0o700),
                    missing_ok=True,
                )
            )

            outside = root / "outside"
            outside.write_bytes(b"outside\n")
            outside.chmod(0o600)
            lifecycle.symlink_to(outside)
            with self.assertRaises(fleet_safe_paths.SafePathError):
                rooted.read_regular_optional(
                    "missions/mission/control/lifecycle.json",
                    directory_modes=(0o700, 0o700, 0o700),
                )
            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "not regular"
            ):
                rooted.replace_regular(
                    "missions/mission/control/lifecycle.json",
                    b"escaped\n",
                    directory_modes=(0o700, 0o700, 0o700),
                )
            self.assertEqual(outside.read_bytes(), b"outside\n")

    def test_guarded_and_open_append_reject_hard_links(self) -> None:
        root = self.private_directory("runs")
        relative = "missions/mission/control/service.stdout.log"
        modes = (0o700, 0o700, 0o700)
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            with rooted.open_append_regular(
                relative,
                directory_modes=modes,
            ) as descriptor:
                os.write(descriptor, b"first\n")
                os.fsync(descriptor)
            path = root / relative
            rejected_path, appended = rooted.guarded_append_regular(
                relative,
                b"rejected\n",
                directory_modes=modes,
                reject_if=lambda snapshot: snapshot == b"first\n",
            )
            self.assertEqual(rejected_path, path.resolve())
            self.assertFalse(appended)
            self.assertEqual(path.read_bytes(), b"first\n")
            _, appended = rooted.guarded_append_regular(
                relative,
                b"second\n",
                directory_modes=modes,
                reject_if=lambda _snapshot: False,
            )
            self.assertTrue(appended)
            self.assertEqual(path.read_bytes(), b"first\nsecond\n")

            hard_link = root / "hard-linked.log"
            os.link(path, hard_link)
            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "unexpected link count"
            ):
                with rooted.open_append_regular(relative, directory_modes=modes):
                    pass
            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "unexpected link count"
            ):
                rooted.guarded_append_regular(
                    relative,
                    b"escaped\n",
                    directory_modes=modes,
                    reject_if=lambda _snapshot: False,
                )
            self.assertEqual(hard_link.read_bytes(), b"first\nsecond\n")

    def test_guarded_append_enforces_projected_size_before_guard_or_write(
        self,
    ) -> None:
        root = self.private_directory("runs-guarded-cap")
        relative = "ledger.jsonl"
        with fleet_safe_paths.RootedFS(root, root_mode=0o700) as rooted:
            _, appended = rooted.guarded_append_regular(
                relative,
                b"one\n",
                directory_modes=(),
                reject_if=lambda _snapshot: False,
                max_existing_bytes=8,
            )
            self.assertTrue(appended)

            guard = mock.Mock(return_value=False)
            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "would exceed maximum size"
            ):
                rooted.guarded_append_regular(
                    relative,
                    b"two!!\n",
                    directory_modes=(),
                    reject_if=guard,
                    max_existing_bytes=8,
                )
            guard.assert_not_called()
            self.assertEqual((root / relative).read_bytes(), b"one\n")

            _, appended = rooted.guarded_append_regular(
                relative,
                b"two\n",
                directory_modes=(),
                reject_if=lambda _snapshot: False,
                max_existing_bytes=8,
            )
            self.assertTrue(appended)
            self.assertEqual((root / relative).read_bytes(), b"one\ntwo\n")

            with self.assertRaisesRegex(
                fleet_safe_paths.SafePathError, "appended content exceeds"
            ):
                rooted.guarded_append_regular(
                    "too-small.jsonl",
                    b"oversized\n",
                    directory_modes=(),
                    reject_if=lambda _snapshot: False,
                    max_existing_bytes=4,
                )
            self.assertFalse((root / "too-small.jsonl").exists())

    def test_concurrent_guarded_appends_cannot_race_past_projected_size(
        self,
    ) -> None:
        root = self.private_directory("runs-concurrent-guarded-cap")
        script = """
from pathlib import Path
import sys
import fleet_safe_paths

try:
    with fleet_safe_paths.RootedFS(Path(sys.argv[1]), root_mode=0o700) as rooted:
        rooted.guarded_append_regular(
            "ledger.jsonl",
            sys.argv[2].encode("ascii"),
            directory_modes=(),
            reject_if=lambda _snapshot: False,
            max_existing_bytes=4,
        )
except fleet_safe_paths.SafePathError:
    raise SystemExit(3)
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "scripts")
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(root), payload],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for payload in ("one\n", "two\n")
        ]
        results = [process.communicate(timeout=10) for process in processes]
        self.assertEqual(
            sorted(process.returncode for process in processes),
            [0, 3],
            results,
        )
        self.assertIn((root / "ledger.jsonl").read_bytes(), (b"one\n", b"two\n"))
        self.assertEqual((root / "ledger.jsonl").stat().st_size, 4)


if __name__ == "__main__":
    unittest.main()
