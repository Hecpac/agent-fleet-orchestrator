from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_clone_guard  # noqa: E402


class FleetCloneGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "clone"
        self.root.mkdir(mode=0o700)
        (self.root / "file.txt").write_text("same device\n", encoding="utf-8")

    @staticmethod
    def ambiguous_json_rows() -> dict[str, bytes]:
        return {
            "duplicate": b'{"value":1,"value":2}\n',
            "nan": b'{"value":NaN}\n',
            "infinity": b'{"value":Infinity}\n',
            "negative-infinity": b'{"value":-Infinity}\n',
            "overflow": b'{"value":1e999}\n',
            "bom": b'\xef\xbb\xbf{"value":1}\n',
            "invalid-utf8": b'{"value":"\xff"}\n',
            "lone-high-surrogate": b'{"value":"\\ud800"}\n',
            "lone-low-surrogate": b'{"value":"\\udc00"}\n',
            "escaped-surrogate-pair": b'{"value":"\\ud83d\\ude00"}\n',
            "trailing-value": b'{"value":1}\n{}',
            "trailing-whitespace": b'{"value":1}\n ',
            "missing-lf": b'{"value":1}',
            "crlf": b'{"value":1}\r\n',
            "noncanonical-order": b'{"z":1,"a":2}\n',
        }

    @staticmethod
    def tree_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
        snapshot: dict[str, tuple[object, ...]] = {}
        for path in (root, *sorted(root.rglob("*"))):
            info = path.lstat()
            relative = "." if path == root else str(path.relative_to(root))
            common = (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_nlink,
                info.st_size,
                info.st_mtime_ns,
            )
            if stat.S_ISREG(info.st_mode):
                snapshot[relative] = ("file", *common, path.read_bytes())
            elif stat.S_ISLNK(info.st_mode):
                snapshot[relative] = ("symlink", *common, os.readlink(path))
            else:
                snapshot[relative] = ("other", *common)
        return snapshot

    def assert_canonical_intent(self, path: Path) -> None:
        content = path.read_bytes()
        value = fleet_clone_guard.fleet_json.loads(content)
        self.assertEqual(
            content,
            fleet_clone_guard.fleet_json.canonical_bytes(value) + b"\n",
        )

    def test_same_device_tree_accepts_local_descendants(self) -> None:
        self.assertEqual(
            fleet_clone_guard.assert_same_device_tree(self.root),
            self.root.stat().st_dev,
        )

    def test_same_device_tree_portably_rejects_cross_device_descendant(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        root_device = os.fstat(descriptor).st_dev
        foreign = SimpleNamespace(
            st_dev=root_device + 1,
            st_ino=999,
            st_mode=stat.S_IFREG | 0o600,
            st_uid=os.geteuid(),
            st_nlink=1,
        )
        with (
            mock.patch.object(
                fleet_clone_guard.os, "listdir", return_value=["file.txt"]
            ),
            mock.patch.object(
                fleet_clone_guard,
                "_stat_optional",
                return_value=foreign,
            ),
            self.assertRaisesRegex(
                fleet_clone_guard.CloneGuardError,
                "cross-device descendant",
            ),
        ):
            fleet_clone_guard._assert_same_device_contents(
                descriptor,
                root_device,
            )

    def test_partial_reader_clone_is_durably_staged_without_deletion(self) -> None:
        runs = Path(self.tempdir.name) / "runs"
        runs.mkdir(mode=0o700)
        worktrees = Path(self.tempdir.name) / "worktrees"
        worktrees.mkdir(mode=0o700)
        source = worktrees / "partial-reader-scout"
        source.mkdir(mode=0o700)
        (source / "incomplete.pack").write_bytes(b"partial clone bytes")
        arguments = SimpleNamespace(
            runs_dir=runs,
            worktrees_root=worktrees,
            feature="partial-reader",
            instance="scout",
            workspace_uuid="00000000-0000-5000-8000-000000000001",
            kind="partial",
            expected_sha="a" * 40,
            branch="-",
        )

        staged = fleet_clone_guard.stage_clone(arguments)
        retried = fleet_clone_guard.stage_clone(arguments)

        self.assertEqual(retried, staged)
        self.assertFalse(source.exists())
        self.assertTrue(staged.is_dir())
        self.assertEqual(
            (staged / "incomplete.pack").read_bytes(),
            b"partial clone bytes",
        )
        intents = list(runs.glob("*.clone-stage-intent.json"))
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].stat().st_nlink, 1)
        self.assert_canonical_intent(intents[0])

    def creation_arguments(self) -> SimpleNamespace:
        runs = Path(self.tempdir.name) / "creation-runs"
        runs.mkdir(mode=0o700, exist_ok=True)
        worktrees = Path(self.tempdir.name) / "creation-worktrees"
        worktrees.mkdir(mode=0o700, exist_ok=True)
        target = Path(self.tempdir.name) / "creation-target"
        target.mkdir(mode=0o700, exist_ok=True)
        return SimpleNamespace(
            mode="plan",
            runs_dir=runs,
            worktrees_root=worktrees,
            target_repo=target,
            feature="planned",
            instance="build",
            workspace_uuid="00000000-0000-5000-8000-000000000002",
            kind="writer",
            expected_sha="b" * 40,
            branch="fleet/planned/build",
        )

    def test_creation_plan_precedes_source_and_binding(self) -> None:
        arguments = self.creation_arguments()
        source = arguments.worktrees_root / "planned-build"

        fleet_clone_guard.creation_intent(arguments)

        self.assertFalse(source.exists())
        plans = list(arguments.runs_dir.glob("*.clone-creation-intent.json"))
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].stat().st_nlink, 1)
        self.assertEqual(stat.S_IMODE(plans[0].stat().st_mode), 0o600)
        self.assert_canonical_intent(plans[0])

        arguments.mode = "source"
        fleet_clone_guard.creation_intent(arguments)
        self.assertTrue(source.is_dir())
        self.assertEqual(list(source.iterdir()), [])
        self.assertFalse(list(arguments.runs_dir.glob("*.clone-creation-binding.json")))

        arguments.mode = "bind"
        fleet_clone_guard.creation_intent(arguments)
        bindings = list(arguments.runs_dir.glob("*.clone-creation-binding.json"))
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0].stat().st_nlink, 1)
        self.assert_canonical_intent(bindings[0])
        arguments.mode = "require"
        self.assertEqual(fleet_clone_guard.creation_intent(arguments), "source")

    def test_exact_json_contract_uses_utf8_canonical_bytes_and_one_lf(self) -> None:
        value = {"z": "café", "a": 1.5}
        encoded = fleet_clone_guard._encode_exact(value)

        self.assertEqual(encoded, '{"a":1.5,"z":"café"}\n'.encode())
        self.assertEqual(fleet_clone_guard._decode_exact(encoded), value)
        for invalid in ({"value": float("nan")}, {"value": "bad-\ud800"}):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(fleet_clone_guard.CloneGuardError),
            ):
                fleet_clone_guard._encode_exact(invalid)

    def test_creation_plan_rejects_ambiguous_json_without_effects(self) -> None:
        arguments = self.creation_arguments()
        plan = Path(fleet_clone_guard.creation_intent(arguments))
        arguments.mode = "source"

        for attack, content in self.ambiguous_json_rows().items():
            with self.subTest(attack=attack):
                plan.write_bytes(content)
                plan.chmod(0o600)
                before = self.tree_snapshot(Path(self.tempdir.name))
                with self.assertRaises(fleet_clone_guard.CloneGuardError):
                    fleet_clone_guard.creation_intent(arguments)
                self.assertEqual(
                    self.tree_snapshot(Path(self.tempdir.name)),
                    before,
                )

    def test_creation_plan_path_substitution_is_rejected_before_parse_or_effect(
        self,
    ) -> None:
        arguments = self.creation_arguments()
        plan = Path(fleet_clone_guard.creation_intent(arguments))
        valid = plan.read_bytes()
        arguments.mode = "source"

        for attack in ("symlink", "hardlink"):
            with self.subTest(attack=attack):
                plan.unlink()
                outside = Path(self.tempdir.name) / f"outside-{attack}.json"
                outside.write_bytes(valid)
                outside.chmod(0o600)
                if attack == "symlink":
                    plan.symlink_to(outside)
                else:
                    os.link(outside, plan)
                before = self.tree_snapshot(Path(self.tempdir.name))
                with (
                    mock.patch.object(fleet_clone_guard, "_decode_exact") as decode,
                    self.assertRaises(
                        (
                            fleet_clone_guard.CloneGuardError,
                            fleet_clone_guard.SafePathError,
                        )
                    ),
                ):
                    fleet_clone_guard.creation_intent(arguments)
                decode.assert_not_called()
                self.assertEqual(
                    self.tree_snapshot(Path(self.tempdir.name)),
                    before,
                )

    def test_stage_intent_rejects_ambiguous_json_before_clone_mutation(self) -> None:
        runs = Path(self.tempdir.name) / "stage-runs"
        runs.mkdir(mode=0o700)
        worktrees = Path(self.tempdir.name) / "stage-worktrees"
        worktrees.mkdir(mode=0o700)
        source = worktrees / "strict-stage-worker"
        source.mkdir(mode=0o700)
        (source / "payload").write_bytes(b"must remain reachable")
        arguments = SimpleNamespace(
            runs_dir=runs,
            worktrees_root=worktrees,
            feature="strict-stage",
            instance="worker",
            workspace_uuid="00000000-0000-5000-8000-000000000003",
            kind="partial",
            expected_sha="c" * 40,
            branch="-",
        )
        stage_name, _, _ = fleet_clone_guard._intent_names(
            arguments.feature,
            arguments.instance,
            arguments.workspace_uuid,
        )
        intent = runs / stage_name

        for attack, content in self.ambiguous_json_rows().items():
            with self.subTest(attack=attack):
                intent.write_bytes(content)
                intent.chmod(0o600)
                before = self.tree_snapshot(Path(self.tempdir.name))
                with self.assertRaises(fleet_clone_guard.CloneGuardError):
                    fleet_clone_guard.stage_clone(arguments)
                self.assertEqual(
                    self.tree_snapshot(Path(self.tempdir.name)),
                    before,
                )

    def test_tombstone_rejects_bad_json_before_staging_root_creation(self) -> None:
        runs = Path(self.tempdir.name) / "early-tombstone-runs"
        runs.mkdir(mode=0o700)
        worktrees = Path(self.tempdir.name) / "early-tombstone-worktrees"
        worktrees.mkdir(mode=0o700)
        workspace_uuid = "00000000-0000-5000-8000-000000000006"
        arguments = SimpleNamespace(
            mode="ensure",
            runs_dir=runs,
            worktrees_root=worktrees,
            feature="early-retire",
            instance="worker",
            workspace_uuid=workspace_uuid,
            kind="partial",
            expected_sha="a" * 40,
            branch="-",
        )
        _, tombstone_name, _ = fleet_clone_guard._intent_names(
            arguments.feature,
            arguments.instance,
            arguments.workspace_uuid,
        )
        tombstone = runs / tombstone_name

        for attack, content in self.ambiguous_json_rows().items():
            with self.subTest(attack=attack):
                tombstone.write_bytes(content)
                tombstone.chmod(0o600)
                before = self.tree_snapshot(Path(self.tempdir.name))
                with self.assertRaises(fleet_clone_guard.CloneGuardError):
                    fleet_clone_guard.tombstone(arguments)
                self.assertEqual(
                    self.tree_snapshot(Path(self.tempdir.name)),
                    before,
                )

    def test_tombstone_rejects_ambiguous_json_before_retirement(self) -> None:
        runs = Path(self.tempdir.name) / "tombstone-runs"
        runs.mkdir(mode=0o700)
        worktrees = Path(self.tempdir.name) / "tombstone-worktrees"
        worktrees.mkdir(mode=0o700)
        staging_root = worktrees / ".fleet-control-staging"
        staging_root.mkdir(mode=0o700)
        workspace_uuid = "00000000-0000-5000-8000-000000000004"
        staged = staging_root / f"strict-retire--worker--{workspace_uuid}"
        staged.mkdir(mode=0o700)
        (staged / "payload").write_bytes(b"must not be retired")
        arguments = SimpleNamespace(
            mode="remove",
            runs_dir=runs,
            worktrees_root=worktrees,
            feature="strict-retire",
            instance="worker",
            workspace_uuid=workspace_uuid,
            kind="partial",
            expected_sha="d" * 40,
            branch="-",
        )
        _, tombstone_name, _ = fleet_clone_guard._intent_names(
            arguments.feature,
            arguments.instance,
            arguments.workspace_uuid,
        )
        tombstone = runs / tombstone_name

        for attack, content in self.ambiguous_json_rows().items():
            with self.subTest(attack=attack):
                tombstone.write_bytes(content)
                tombstone.chmod(0o600)
                before = self.tree_snapshot(Path(self.tempdir.name))
                with self.assertRaises(fleet_clone_guard.CloneGuardError):
                    fleet_clone_guard.tombstone(arguments)
                self.assertEqual(
                    self.tree_snapshot(Path(self.tempdir.name)),
                    before,
                )

    def test_publication_rejects_ambiguous_json_before_clear(self) -> None:
        runs = Path(self.tempdir.name) / "publication-runs"
        runs.mkdir(mode=0o700)
        worktrees = Path(self.tempdir.name) / "publication-worktrees"
        worktrees.mkdir(mode=0o700)
        target = Path(self.tempdir.name) / "publication-target"
        target.mkdir(mode=0o700)
        workspace_uuid = "00000000-0000-5000-8000-000000000005"
        staging = (
            worktrees
            / ".fleet-control-staging"
            / f"strict-publish--worker--{workspace_uuid}"
        )
        arguments = SimpleNamespace(
            mode="clear",
            runs_dir=runs,
            worktrees_root=worktrees,
            feature="strict-publish",
            instance="worker",
            workspace_uuid=workspace_uuid,
            target_repo=target,
            branch="fleet/strict-publish/worker",
            base_sha="e" * 40,
            candidate_sha="f" * 40,
            staging_path=staging,
        )
        _, _, publication_name = fleet_clone_guard._intent_names(
            arguments.feature,
            arguments.instance,
            arguments.workspace_uuid,
        )
        publication = runs / publication_name

        for attack, content in self.ambiguous_json_rows().items():
            with self.subTest(attack=attack):
                publication.write_bytes(content)
                publication.chmod(0o600)
                before = self.tree_snapshot(Path(self.tempdir.name))
                with self.assertRaises(fleet_clone_guard.CloneGuardError):
                    fleet_clone_guard.publication_intent(arguments)
                self.assertEqual(
                    self.tree_snapshot(Path(self.tempdir.name)),
                    before,
                )

    def test_unbound_nonempty_planned_source_is_preserved_and_refused(self) -> None:
        arguments = self.creation_arguments()
        fleet_clone_guard.creation_intent(arguments)
        arguments.mode = "source"
        fleet_clone_guard.creation_intent(arguments)
        source = arguments.worktrees_root / "planned-build"
        marker = source / "unexpected"
        marker.write_text("preserve\n", encoding="utf-8")

        arguments.mode = "bind"
        with self.assertRaisesRegex(
            fleet_clone_guard.CloneGuardError,
            "not empty",
        ):
            fleet_clone_guard.creation_intent(arguments)

        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve\n")
        self.assertTrue(list(arguments.runs_dir.glob("*.clone-creation-intent.json")))
        self.assertFalse(list(arguments.runs_dir.glob("*.clone-creation-binding.json")))

    def test_source_without_creation_plan_is_never_adopted(self) -> None:
        arguments = self.creation_arguments()
        source = arguments.worktrees_root / "planned-build"
        source.mkdir(mode=0o700)
        marker = source / "foreign"
        marker.write_text("untouched\n", encoding="utf-8")

        with self.assertRaisesRegex(
            fleet_clone_guard.CloneGuardError,
            "without exact creation plan",
        ):
            fleet_clone_guard.creation_intent(arguments)

        self.assertEqual(marker.read_text(encoding="utf-8"), "untouched\n")
        self.assertFalse(list(arguments.runs_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
