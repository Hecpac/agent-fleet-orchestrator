from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_archive
import fleet_audit_client
import fleet_manifest
import fleet_mission
import fleet_mission_state as state
import fleet_state
import workflow_config
from tests.mission_control_test_support import legacy_v1_compiled, write_compiled

APPROVE_SPEC = __import__("importlib.util").util.spec_from_file_location(
    "fleet_approve_for_archive", ROOT / "scripts" / "fleet-approve.py"
)
assert APPROVE_SPEC and APPROVE_SPEC.loader
fleet_approve = __import__("importlib.util").util.module_from_spec(APPROVE_SPEC)
APPROVE_SPEC.loader.exec_module(fleet_approve)


class FleetArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs = self.tmp / "runs"
        self.repo = self.tmp / "target"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "fleet@example.test")
        self.git("config", "user.name", "Fleet Archive Test")
        (self.repo / "README.md").write_text("baseline\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "baseline")
        self.base_sha = self.git("rev-parse", "HEAD")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.strip()

    def compiled(self, policy: str = "full") -> dict:
        raw = copy.deepcopy(
            workflow_config.compile_path(ROOT / "workflows" / "implementation.yaml")["workflow"]
        )
        raw["archive"]["content_policy"] = policy
        return workflow_config.compile_workflow(raw)

    def create_mission(self, feature: str, policy: str = "full") -> tuple[str, Path]:
        mission_id, _ = fleet_mission.create_mission(
            self.runs,
            compiled=self.compiled(policy),
            feature=feature,
            objective="inspect sensitive implementation evidence",
            target_repo=self.repo.resolve(),
            base_sha=self.base_sha,
            idempotency_key=f"archive:{feature}",
            runtime_options={"timeout_seconds": 300},
        )
        manifest = self.runs / f"fleet-{feature}.manifest"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"feature={feature}\nmission_id={mission_id}\npreset=dan\nmode=autonomous\n"
            f"target_repo={self.repo.resolve()}\nbase_sha={self.base_sha}\n"
            "workspace=workspace:1\n",
            encoding="utf-8",
        )
        manifest.chmod(0o600)
        return mission_id, manifest

    def add_writer_commit(self, manifest: Path) -> str:
        branch = "fleet/archive/build"
        self.git("switch", "-q", "-c", branch)
        (self.repo / "README.md").write_text("final\n", encoding="utf-8")
        (self.repo / "payload.bin").write_bytes(bytes(range(256)) * 8)
        self.git("add", "README.md", "payload.bin")
        self.git("commit", "-q", "-m", "feat: archive binary result")
        final_sha = self.git("rev-parse", "HEAD")
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                "build.authority=write\n"
                f"build.branch={branch}\n"
                f"build.worktree={(self.tmp / 'retired-writer-clone').resolve()}\n"
                f"build.base_sha={self.base_sha}\n"
                f"build.final_sha={final_sha}\n"
                f"build.published_sha={final_sha}\n"
                "build.git_isolation=isolated-clone\n"
                "build.publication_state=published\n"
                "workspace.quiesced=1\n"
            )
        return final_sha

    def add_evidence(self, feature: str, mission_id: str) -> None:
        for kind in ("prompts", "tasks", "results"):
            directory = self.runs / kind / feature
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{kind}.txt").write_text(f"{kind} evidence\n", encoding="utf-8")
        artifact = self.runs / "missions" / mission_id / "artifacts"
        artifact.mkdir()
        (artifact / ("a" * 64)).write_text("artifact evidence\n", encoding="utf-8")

    def add_phase_state(self, manifest: Path, mission_id: str) -> Path:
        compiled, _ = fleet_mission.load_mission_compiled(
            self.runs, mission_id, mode="effect"
        )
        binding = fleet_manifest.binding_for_preset(
            compiled, compiled["resolved"]["preset"]
        )
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                "manifest_contract_version=3\n"
                f"compiled_digest={binding['compiled_digest']}\n"
                f"router_digest={binding['router_digest']}\n"
                f"roster_digest={binding['roster_digest']}\n"
                f"launch_digest={binding['launch_digest']}\n"
            )
        values = fleet_archive._parse_manifest(manifest.read_bytes())
        phase_state = fleet_state._new_state(values)
        path = manifest.with_suffix(".state.json")
        path.write_bytes(state.canonical_bytes(phase_state) + b"\n")
        path.chmod(0o600)
        return path

    def test_archive_phase_state_is_descriptor_bound_and_manifest_bound(self) -> None:
        attacks = ("symlink", "hardlink", "corrupt", "manifest-drift")
        for attack in attacks:
            with self.subTest(attack=attack):
                feature = f"phase-state-{attack}"
                mission_id, manifest = self.create_mission(feature)
                state_path = self.add_phase_state(manifest, mission_id)
                outside = self.tmp / f"{feature}-outside.json"
                if attack == "symlink":
                    outside.write_bytes(state_path.read_bytes())
                    outside.chmod(0o600)
                    state_path.unlink()
                    state_path.symlink_to(outside)
                    pattern = "unsafe phase state source"
                elif attack == "hardlink":
                    os.link(state_path, outside)
                    pattern = "unsafe phase state source"
                elif attack == "corrupt":
                    state_path.write_text("{not-json}\n", encoding="utf-8")
                    pattern = "phase state is invalid"
                else:
                    manifest.write_text(
                        manifest.read_text(encoding="utf-8").replace(
                            "preset=dan\n", "preset=research\n", 1
                        ),
                        encoding="utf-8",
                    )
                    pattern = "immutable manifest binding changed"

                archive = self.runs / "missions" / mission_id / "archive"
                with self.assertRaisesRegex(fleet_archive.ArchiveError, pattern):
                    fleet_archive.ArchiveBuilder(
                        self.runs, mission_id, archive
                    ).create(manifest)
                self.assertFalse(archive.exists())

    def test_archive_builder_rejects_historical_compiled_before_output_effects(
        self,
    ) -> None:
        mission_id, _ = self.create_mission("historical-effect")
        root = self.runs / "missions" / mission_id
        compiled = fleet_archive.fleet_compiled.load(
            root / "compiled-workflow.json", mode="read"
        )
        write_compiled(
            root / "compiled-workflow.json", legacy_v1_compiled(compiled)
        )
        with (
            mock.patch.object(fleet_archive, "_run") as effect,
            self.assertRaisesRegex(
                fleet_archive.ArchiveError, "historical read-only.*require v2"
            ),
        ):
            fleet_archive.ArchiveBuilder(self.runs, mission_id)
        effect.assert_not_called()
        self.assertFalse((root / "archive").exists())

    def test_historical_archive_policy_accepts_bound_v1_for_read_only_verify(
        self,
    ) -> None:
        compiled = legacy_v1_compiled(self.compiled())
        content = state.canonical_bytes(compiled) + b"\n"
        loaded = fleet_archive._historical_compiled(
            content,
            {
                "workflow_digest": compiled["workflow_digest"],
                "compiled_digest": compiled["compiled_digest"],
            },
            compiled["workflow"]["archive"]["content_policy"],
        )
        self.assertEqual(loaded, compiled)

        foreign = legacy_v1_compiled(
            workflow_config.compile_path(ROOT / "workflows" / "research.yaml")
        )
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "mission ledger"):
            fleet_archive._historical_compiled(
                state.canonical_bytes(foreign) + b"\n",
                {
                    "workflow_digest": compiled["workflow_digest"],
                    "compiled_digest": compiled["compiled_digest"],
                },
                compiled["workflow"]["archive"]["content_policy"],
            )

    def test_full_archive_is_portable_and_reproduces_binary_writer_commit(self) -> None:
        mission_id, manifest = self.create_mission("full")
        final_sha = self.add_writer_commit(manifest)
        self.add_evidence("full", mission_id)

        archive = self.runs / "missions" / mission_id / "archive"
        created = fleet_archive.ArchiveBuilder(self.runs, mission_id, archive).create(manifest)
        self.assertTrue(created["valid"])
        self.assertEqual(created["final_sha"], final_sha)
        self.assertTrue(fleet_archive.verify_archive(archive, repo=self.repo)["valid"])
        self.assertIn(b"GIT binary patch", (archive / "writer" / "change.patch").read_bytes())
        self.assertEqual(self.git("rev-parse", "fleet/archive/build"), final_sha)

        portable = self.tmp / "clean-directory" / "archive"
        shutil.copytree(archive, portable)
        shutil.rmtree(self.runs)
        self.assertTrue(fleet_archive.verify_archive(portable)["valid"])

    def test_manifest_loader_is_descriptor_anchored_and_rejects_ambiguous_rows(self) -> None:
        for suffix, expected in (
            ("mode=guided\n", "duplicate fleet manifest key"),
            ("malformed-row\n", "malformed fleet manifest row"),
        ):
            with self.subTest(expected=expected):
                feature = "strict-" + expected.split()[0]
                mission_id, manifest = self.create_mission(feature)
                with manifest.open("a", encoding="utf-8") as handle:
                    handle.write(suffix)
                with self.assertRaisesRegex(fleet_archive.ArchiveError, expected):
                    fleet_archive.ArchiveBuilder(
                        self.runs, mission_id
                    ).create(manifest)

        mission_id, manifest = self.create_mission("manifest-symlink")
        backing = manifest.with_name(".manifest-symlink.backing")
        manifest.rename(backing)
        manifest.symlink_to(backing.name)
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "unsafe fleet manifest"):
            fleet_archive.ArchiveBuilder(self.runs, mission_id).create(manifest)

    def test_writer_metadata_rejects_intermediate_length_git_hashes(self) -> None:
        for length in range(41, 64):
            with self.subTest(length=length):
                feature = f"invalid-git-sha-{length}"
                mission_id, manifest = self.create_mission(feature)
                invalid = "a" * length
                with manifest.open("a", encoding="utf-8") as handle:
                    handle.write(
                        "build.authority=write\n"
                        f"build.branch=fleet/{feature}/build\n"
                        f"build.worktree={(self.tmp / f'retired-{length}').resolve()}\n"
                        f"build.base_sha={self.base_sha}\n"
                        f"build.final_sha={invalid}\n"
                        f"build.published_sha={invalid}\n"
                        "build.git_isolation=isolated-clone\n"
                        "build.publication_state=published\n"
                        "workspace.quiesced=1\n"
                    )
                with self.assertRaisesRegex(
                    fleet_archive.ArchiveError, "published SHA metadata is invalid"
                ):
                    fleet_archive.ArchiveBuilder(
                        self.runs, mission_id
                    ).create(manifest)

    def test_archive_rejects_mutable_or_drifted_writer_branch(self) -> None:
        mission_id, manifest = self.create_mission("writer-drift")
        final_sha = self.add_writer_commit(manifest)
        mutable = manifest.read_text(encoding="utf-8").replace(
            "workspace.quiesced=1", "workspace.quiesced=0"
        )
        manifest.write_text(mutable, encoding="utf-8")
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "not quiescent"):
            fleet_archive.ArchiveBuilder(self.runs, mission_id).create(manifest)

        manifest.write_text(
            mutable.replace("workspace.quiesced=0", "workspace.quiesced=1"),
            encoding="utf-8",
        )
        self.git("commit", "--allow-empty", "-q", "-m", "late writer mutation")
        self.assertNotEqual(self.git("rev-parse", "HEAD"), final_sha)
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "drifted after quiescence"):
            fleet_archive.ArchiveBuilder(self.runs, mission_id).create(manifest)

    def test_archive_create_rejects_manifest_and_writer_base_drift(self) -> None:
        cases = (("manifest", "base_sha"), ("writer", "build.base_sha"))
        for index, (suffix, field) in enumerate(cases):
            with self.subTest(field=field):
                if index:
                    self.git("switch", "-q", "main")
                    self.git("branch", "-D", "fleet/archive/build")
                mission_id, manifest = self.create_mission(f"base-drift-{suffix}")
                final_sha = self.add_writer_commit(manifest)
                original = (
                    f"{field}={self.base_sha}\n"
                    if field != "base_sha"
                    else f"base_sha={self.base_sha}\n"
                )
                manifest.write_text(
                    manifest.read_text(encoding="utf-8").replace(
                        original, f"{field}={final_sha}\n", 1
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    fleet_archive.ArchiveError, "base_sha does not match mission state"
                ):
                    fleet_archive.ArchiveBuilder(self.runs, mission_id).create(manifest)

    def test_archive_verify_rejects_semantically_rehashed_base_and_final_drift(
        self,
    ) -> None:
        mission_id, manifest = self.create_mission("verify-base-drift")
        final_sha = self.add_writer_commit(manifest)
        archive = self.runs / "missions" / mission_id / "archive"
        fleet_archive.ArchiveBuilder(self.runs, mission_id).create(manifest)

        archived_manifest = archive / "manifest"
        index_path = archive / "archive-index.json"
        receipt_path = archive / "archive-receipt.json"

        def rehash_manifest_entry() -> None:
            manifest_payload = archived_manifest.read_bytes()
            index = json.loads(index_path.read_text(encoding="utf-8"))
            manifest_entry = next(
                entry for entry in index["entries"] if entry["path"] == "manifest"
            )
            manifest_entry["size"] = len(manifest_payload)
            manifest_entry["sha256"] = hashlib.sha256(manifest_payload).hexdigest()
            index_bytes = state.canonical_bytes(index) + b"\n"
            index_path.write_bytes(index_bytes)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["index_sha256"] = hashlib.sha256(index_bytes).hexdigest()
            receipt["content_root_sha256"] = state.sha256(
                sorted(
                    [
                        entry
                        for entry in index["entries"]
                        if not entry["path"].startswith("audit/")
                    ],
                    key=lambda entry: entry["path"],
                )
            )
            receipt_path.write_bytes(state.canonical_bytes(receipt) + b"\n")

        archived_manifest.write_text(
            archived_manifest.read_text(encoding="utf-8").replace(
                f"base_sha={self.base_sha}\n", f"base_sha={final_sha}\n", 1
            ),
            encoding="utf-8",
        )
        rehash_manifest_entry()

        with self.assertRaisesRegex(
            fleet_archive.ArchiveError, "base_sha does not match mission state"
        ):
            fleet_archive.verify_archive(archive)

        drifted = archived_manifest.read_text(encoding="utf-8").replace(
            f"base_sha={final_sha}\n", f"base_sha={self.base_sha}\n", 1
        )
        drifted = drifted.replace(
            f"build.final_sha={final_sha}\n", f"build.final_sha={self.base_sha}\n"
        ).replace(
            f"build.published_sha={final_sha}\n",
            f"build.published_sha={self.base_sha}\n",
        )
        archived_manifest.write_text(drifted, encoding="utf-8")
        rehash_manifest_entry()
        with self.assertRaisesRegex(
            fleet_archive.ArchiveError,
            "archive index does not match manifest and mission binding",
        ):
            fleet_archive.verify_archive(archive)

    def test_empty_git_tree_archive_is_valid_and_reproducible(self) -> None:
        empty = self.tmp / "empty-target"
        empty.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=empty, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Fleet Archive Test", "-c",
             "user.email=fleet@example.test", "commit", "--allow-empty", "-q", "-m", "baseline"],
            cwd=empty,
            check=True,
        )
        compiled = self.compiled()
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=empty, text=True,
            stdout=subprocess.PIPE, check=True,
        ).stdout.strip()
        mission_id, _ = fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature="empty-tree",
            objective="archive an empty Git tree",
            target_repo=empty.resolve(),
            base_sha=base,
            idempotency_key="archive:empty-tree",
        )
        manifest = self.runs / "fleet-empty-tree.manifest"
        manifest.write_text(
            f"feature=empty-tree\nmission_id={mission_id}\n"
            f"target_repo={empty.resolve()}\nbase_sha={base}\n",
            encoding="utf-8",
        )
        manifest.chmod(0o600)

        result = fleet_archive.ArchiveBuilder(self.runs, mission_id).create(manifest)

        self.assertTrue(result["valid"])
        self.assertEqual(result["base_sha"], result["final_sha"])
        self.assertTrue(
            fleet_archive.verify_archive(
                self.runs / "missions" / mission_id / "archive", repo=empty
            )["valid"]
        )

    def test_sha256_archive_reproduces_native_raw_tree(self) -> None:
        repo = self.tmp / "target-sha256"
        repo.mkdir()
        initialized = subprocess.run(
            ["git", "init", "-q", "--object-format=sha256", "-b", "main"],
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if initialized.returncode != 0:
            self.skipTest("installed Git does not support SHA-256 repositories")
        self.repo = repo
        self.git("config", "user.email", "fleet@example.test")
        self.git("config", "user.name", "Fleet Archive Test")
        (repo / "README.md").write_text("sha256 baseline\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "sha256 baseline")
        self.base_sha = self.git("rev-parse", "HEAD")
        mission_id, manifest = self.create_mission("sha256-archive")
        final_sha = self.add_writer_commit(manifest)

        archive = self.runs / "missions" / mission_id / "archive"
        result = fleet_archive.ArchiveBuilder(
            self.runs, mission_id, archive
        ).create(manifest)

        self.assertEqual(result["object_format"], "sha256")
        self.assertEqual(len(final_sha), 64)
        self.assertTrue(fleet_archive.verify_archive(archive, repo=repo)["valid"])
        commits = json.loads(
            (archive / "writer" / "commits.json").read_text(encoding="utf-8")
        )
        self.assertEqual(commits["object_format"], "sha256")
        self.assertEqual(len(commits["final_tree_sha"]), 64)

    def test_repository_verification_accepts_legacy_git_archive_format(self) -> None:
        mission_id, manifest = self.create_mission("legacy-tree")
        final_sha = self.add_writer_commit(manifest)
        archive = self.runs / "missions" / mission_id / "archive"
        fleet_archive.ArchiveBuilder(self.runs, mission_id, archive).create(manifest)

        commits_path = archive / "writer" / "commits.json"
        commits = json.loads(commits_path.read_text(encoding="utf-8"))
        del commits["object_format"]
        commits_path.write_bytes(state.canonical_bytes(commits) + b"\n")
        tree_path = archive / "writer" / "final-tree.tar"
        tree_path.write_bytes(
            subprocess.run(
                ["git", "archive", "--format=tar", final_sha],
                cwd=self.repo,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout
        )

        index_path = archive / "archive-index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for entry in index["entries"]:
            source = archive / entry["path"]
            if entry["path"] in {"writer/commits.json", "writer/final-tree.tar"}:
                content = source.read_bytes()
                entry["size"] = len(content)
                entry["sha256"] = hashlib.sha256(content).hexdigest()
        index_bytes = state.canonical_bytes(index) + b"\n"
        index_path.write_bytes(index_bytes)
        receipt_path = archive / "archive-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["index_sha256"] = hashlib.sha256(index_bytes).hexdigest()
        receipt["content_root_sha256"] = state.sha256(
            sorted(
                [
                    entry
                    for entry in index["entries"]
                    if not entry["path"].startswith("audit/")
                ],
                key=lambda entry: entry["path"],
            )
        )
        receipt_path.write_bytes(state.canonical_bytes(receipt) + b"\n")

        verified = fleet_archive.verify_archive(archive, repo=self.repo)

        self.assertTrue(verified["valid"])
        self.assertEqual(verified["object_format"], "sha1")

    def test_raw_tree_ignores_replace_refs_and_batches_blob_reads(self) -> None:
        mission_id, manifest = self.create_mission("replace-ref")
        final_sha = self.add_writer_commit(manifest)
        original_tree = self.git("rev-parse", f"{final_sha}^{{tree}}")
        (self.repo / "README.md").write_text("replacement tree\n", encoding="utf-8")
        self.git("add", "README.md")
        replacement_tree = self.git("write-tree")
        replacement_sha = self.git("commit-tree", replacement_tree, "-m", "replacement")
        self.git("reset", "--hard", "-q", final_sha)
        self.git("replace", final_sha, replacement_sha)
        self.assertEqual(
            self.git("rev-parse", f"{final_sha}^{{tree}}"), replacement_tree
        )

        archive = self.runs / "missions" / mission_id / "archive"
        original_run = subprocess.run
        with mock.patch.object(
            fleet_archive.subprocess, "run", wraps=original_run
        ) as run:
            fleet_archive.ArchiveBuilder(
                self.runs, mission_id, archive
            ).create(manifest)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            sum("cat-file" in command and "--batch" in command for command in commands),
            1,
        )
        self.assertFalse(
            any(
                "cat-file" in command and "blob" in command
                for command in commands
            )
        )
        commits = json.loads(
            (archive / "writer" / "commits.json").read_text(encoding="utf-8")
        )
        self.assertEqual(commits["final_tree_sha"], original_tree)
        with tarfile.open(archive / "writer" / "final-tree.tar", mode="r:") as tree:
            readme = tree.extractfile("README.md")
            assert readme is not None
            self.assertEqual(readme.read(), b"final\n")
        self.assertTrue(fleet_archive.verify_archive(archive, repo=self.repo)["valid"])

    def test_archive_ignores_grafts_when_checking_writer_ancestry(self) -> None:
        mission_id, manifest = self.create_mission("grafted-writer")
        (self.repo / "README.md").write_text("unrelated tree\n", encoding="utf-8")
        self.git("add", "README.md")
        tree_sha = self.git("write-tree")
        final_sha = self.git("commit-tree", tree_sha, "-m", "unrelated root")
        branch = "fleet/archive/grafted-writer"
        self.git("branch", branch, final_sha)
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                "build.authority=write\n"
                f"build.branch={branch}\n"
                f"build.worktree={(self.tmp / 'retired-grafted-clone').resolve()}\n"
                f"build.base_sha={self.base_sha}\n"
                f"build.final_sha={final_sha}\n"
                f"build.published_sha={final_sha}\n"
                "build.git_isolation=isolated-clone\n"
                "build.publication_state=published\n"
                "workspace.quiesced=1\n"
            )
        git_dir = Path(self.git("rev-parse", "--git-dir"))
        if not git_dir.is_absolute():
            git_dir = self.repo / git_dir
        grafts = git_dir / "info" / "grafts"
        grafts.parent.mkdir(parents=True, exist_ok=True)
        grafts.write_text(f"{final_sha} {self.base_sha}\n", encoding="ascii")
        grafted_check = subprocess.run(
            ["git", "merge-base", "--is-ancestor", self.base_sha, final_sha],
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(grafted_check.returncode, 0)

        with self.assertRaisesRegex(fleet_archive.ArchiveError, "git failed"):
            fleet_archive.ArchiveBuilder(self.runs, mission_id).create(manifest)

    def test_archive_sanitizes_git_environment_and_disables_external_helpers(self) -> None:
        mission_id, manifest = self.create_mission("hostile-git-environment")
        self.add_writer_commit(manifest)
        marker = self.tmp / "external-helper-ran"
        helper = self.tmp / "hostile-diff-helper"
        helper.write_text(
            "#!/bin/sh\nprintf ran > \"$FLEET_ARCHIVE_CANARY\"\nexit 97\n",
            encoding="utf-8",
        )
        helper.chmod(0o700)
        hook_dir = self.tmp / "hostile-hooks"
        hook_dir.mkdir()
        hook = hook_dir / "post-commit"
        hook.write_text(
            "#!/bin/sh\nprintf ran > \"$FLEET_ARCHIVE_CANARY\"\n",
            encoding="utf-8",
        )
        hook.chmod(0o700)
        poisoned = {
            "FLEET_ARCHIVE_CANARY": str(marker),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(hook_dir),
            "GIT_CONFIG_PARAMETERS": f"'diff.external'='{helper}'",
            "GIT_DIR": str(self.tmp / "foreign.git"),
            "GIT_EXTERNAL_DIFF": str(helper),
            "GIT_GRAFT_FILE": str(self.tmp / "foreign-grafts"),
            "GIT_NO_REPLACE_OBJECTS": "0",
            "GIT_OBJECT_DIRECTORY": str(self.tmp / "foreign-objects"),
            "GIT_WORK_TREE": str(self.tmp / "foreign-worktree"),
        }

        with mock.patch.dict(os.environ, poisoned, clear=False):
            result = fleet_archive.ArchiveBuilder(
                self.runs, mission_id
            ).create(manifest)

        self.assertTrue(result["valid"])
        self.assertFalse(marker.exists(), "hostile Git helper or hook executed")

    def test_archive_records_non_utf8_commit_subject_with_replacement(self) -> None:
        mission_id, manifest = self.create_mission("legacy-subject")
        tree_sha = self.git("rev-parse", f"{self.base_sha}^{{tree}}")
        identity = b"Fleet Archive Test <fleet@example.test> 0 +0000"
        commit = (
            f"tree {tree_sha}\nparent {self.base_sha}\n".encode("ascii")
            + b"author " + identity + b"\n"
            + b"committer " + identity + b"\n\n"
            + b"bad-\xff-subject\n"
        )
        final_sha = subprocess.run(
            ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
            cwd=self.repo,
            input=commit,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.decode("ascii").strip()
        branch = "fleet/archive/legacy-subject"
        self.git("branch", branch, final_sha)
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                "build.authority=write\n"
                f"build.branch={branch}\n"
                f"build.worktree={(self.tmp / 'retired-legacy-clone').resolve()}\n"
                f"build.base_sha={self.base_sha}\n"
                f"build.final_sha={final_sha}\n"
                f"build.published_sha={final_sha}\n"
                "build.git_isolation=isolated-clone\n"
                "build.publication_state=published\n"
                "workspace.quiesced=1\n"
            )

        result = fleet_archive.ArchiveBuilder(
            self.runs, mission_id
        ).create(manifest)

        commits = json.loads(
            (
                self.runs
                / "missions"
                / mission_id
                / "archive"
                / "writer"
                / "commits.json"
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(result["valid"])
        self.assertEqual(commits["commits"][0]["subject"], "bad-�-subject")

    def test_raw_tree_preserves_surrogateescaped_git_names(self) -> None:
        mission_id, manifest = self.create_mission("surrogate-name")
        blob_sha = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=self.repo,
            input=b"surrogate path payload\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.decode("ascii").strip()
        raw_name = b"legacy-\xff-name.txt"
        tree_sha = subprocess.run(
            ["git", "mktree", "-z"],
            cwd=self.repo,
            input=b"100644 blob " + blob_sha.encode("ascii") + b"\t" + raw_name + b"\0",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.decode("ascii").strip()
        final_sha = self.git(
            "commit-tree", tree_sha, "-p", self.base_sha, "-m", "surrogate path"
        )
        branch = "fleet/archive/surrogate-name"
        self.git("branch", branch, final_sha)
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                "build.authority=write\n"
                f"build.branch={branch}\n"
                f"build.worktree={(self.tmp / 'retired-surrogate-clone').resolve()}\n"
                f"build.base_sha={self.base_sha}\n"
                f"build.final_sha={final_sha}\n"
                f"build.published_sha={final_sha}\n"
                "build.git_isolation=isolated-clone\n"
                "build.publication_state=published\n"
                "workspace.quiesced=1\n"
            )

        archive = self.runs / "missions" / mission_id / "archive"
        fleet_archive.ArchiveBuilder(self.runs, mission_id).create(manifest)

        with tarfile.open(archive / "writer" / "final-tree.tar", mode="r:") as tree:
            names = [fleet_archive._git_name(member.name) for member in tree]
        self.assertIn(raw_name, names)
        self.assertTrue(fleet_archive.verify_archive(archive, repo=self.repo)["valid"])

    def test_raw_tree_preflights_blob_and_pax_size_limits(self) -> None:
        oid = "a" * 40
        oversized = f"{oid} blob {fleet_archive.MAX_FILE_BYTES + 1}\n".encode("ascii")
        with mock.patch.object(fleet_archive, "_git_run", return_value=oversized):
            with self.assertRaisesRegex(fleet_archive.ArchiveError, "blob exceeds"):
                fleet_archive._raw_blob_data(
                    self.repo,
                    [("huge.bin", "100644", "blob", oid)],
                    "sha1",
                )

        records = [
            (f"empty-{index}", "100644", "blob", oid)
            for index in range(fleet_archive.MAX_FILE_BYTES // 1536 + 1)
        ]
        empty = f"{oid} blob 0\n".encode("ascii")
        with mock.patch.object(fleet_archive, "_git_run", return_value=empty):
            with self.assertRaisesRegex(
                fleet_archive.ArchiveError, "generated final tree exceeds"
            ):
                fleet_archive._raw_blob_data(self.repo, records, "sha1")

        long_target_size = fleet_archive.MAX_FILE_BYTES // 2
        long_target = f"{oid} blob {long_target_size}\n".encode("ascii")
        symlinks = [
            (f"link-{index}", "120000", "blob", oid) for index in range(3)
        ]
        with (
            mock.patch.object(fleet_archive, "_git_run", return_value=long_target),
            mock.patch.object(fleet_archive.subprocess, "run") as batch_run,
        ):
            with self.assertRaisesRegex(
                fleet_archive.ArchiveError, "generated final tree exceeds"
            ):
                fleet_archive._raw_blob_data(self.repo, symlinks, "sha1")
        batch_run.assert_not_called()

        deep_gitlink = (
            "/".join(["deep"] * 50 + ["child"]),
            "160000",
            "commit",
            oid,
        )
        with (
            mock.patch.object(fleet_archive, "MAX_FILE_BYTES", 8192),
            mock.patch.object(fleet_archive.subprocess, "run") as batch_run,
        ):
            with self.assertRaisesRegex(
                fleet_archive.ArchiveError, "generated final tree exceeds"
            ):
                fleet_archive._raw_blob_data(self.repo, [deep_gitlink], "sha1")
        batch_run.assert_not_called()

    def test_bounded_command_capture_kills_oversized_output(self) -> None:
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "output exceeds limit"):
            fleet_archive._run_bounded(
                [
                    sys.executable,
                    "-c",
                    "import os; os.write(1, b'x' * 131072)",
                ],
                max_stdout_bytes=1024,
                timeout_seconds=10,
            )

    def test_raw_tree_ignores_export_filters_and_preserves_gitlinks(self) -> None:
        child = self.tmp / "submodule-source"
        child.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=child, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Fleet Archive Test", "-c",
             "user.email=fleet@example.test", "commit", "--allow-empty", "-q",
             "-m", "submodule baseline"],
            cwd=child,
            check=True,
        )
        child_oid = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=child,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.strip()
        (self.repo / ".gitattributes").write_text(
            "secret.txt export-ignore\nversion.txt export-subst\n",
            encoding="utf-8",
        )
        (self.repo / "secret.txt").write_text("raw secret\n", encoding="utf-8")
        (self.repo / "version.txt").write_text(
            "commit=$Format:%H$\n", encoding="utf-8"
        )
        self.git("add", ".gitattributes", "secret.txt", "version.txt")
        self.git(
            "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            str(child), "vendor/child",
        )
        self.git("commit", "-q", "-m", "raw tree edge cases")
        self.base_sha = self.git("rev-parse", "HEAD")
        mission_id, manifest = self.create_mission("raw-tree")

        archive = self.runs / "missions" / mission_id / "archive"
        fleet_archive.ArchiveBuilder(self.runs, mission_id, archive).create(manifest)

        tree_tar = archive / "writer" / "final-tree.tar"
        with tarfile.open(tree_tar, mode="r:") as tree:
            secret = tree.extractfile("secret.txt")
            version = tree.extractfile("version.txt")
            gitlink = tree.getmember("vendor/child")
            assert secret is not None and version is not None
            self.assertEqual(secret.read(), b"raw secret\n")
            self.assertEqual(version.read(), b"commit=$Format:%H$\n")
            self.assertTrue(gitlink.isdir())
            self.assertEqual(gitlink.pax_headers[fleet_archive.GIT_MODE_PAX], "160000")
            self.assertEqual(gitlink.pax_headers[fleet_archive.GIT_OID_PAX], child_oid)
        self.assertTrue(fleet_archive.verify_archive(archive, repo=self.repo)["valid"])

    def test_existing_archive_is_exact_mission_bound_and_root_is_not_symlinked(self) -> None:
        first_id, first_manifest = self.create_mission("archive-owner-a")
        first_archive = self.runs / "missions" / first_id / "archive"
        fleet_archive.ArchiveBuilder(
            self.runs, first_id, first_archive
        ).create(first_manifest)
        second_id, second_manifest = self.create_mission("archive-owner-b")
        second_archive = self.runs / "missions" / second_id / "archive"

        with self.assertRaisesRegex(fleet_archive.ArchiveError, "exact mission root"):
            fleet_archive.ArchiveBuilder(self.runs, second_id, first_archive)

        shutil.copytree(first_archive, second_archive)
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "mission_id"):
            fleet_archive.ArchiveBuilder(
                self.runs, second_id, second_archive
            ).create(second_manifest)

        shutil.rmtree(second_archive)
        second_archive.symlink_to(first_archive, target_is_directory=True)
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "must not be a symlink"):
            fleet_archive.verify_archive(second_archive)
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "must not be a symlink"):
            fleet_archive.ArchiveBuilder(
                self.runs, second_id, second_archive
            ).create(second_manifest)

    def test_tamper_and_unsafe_symlink_fail_closed(self) -> None:
        mission_id, manifest = self.create_mission("tamper")
        self.add_evidence("tamper", mission_id)
        archive = self.runs / "missions" / mission_id / "archive"
        fleet_archive.ArchiveBuilder(self.runs, mission_id, archive).create(manifest)
        with (archive / "manifest").open("ab") as handle:
            handle.write(b"x")
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "hash mismatch"):
            fleet_archive.verify_archive(archive)

        unsafe_id, unsafe_manifest = self.create_mission("unsafe")
        prompts = self.runs / "prompts" / "unsafe"
        prompts.mkdir(parents=True)
        (prompts / "escape").symlink_to(self.repo / "README.md")
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "unsafe archive tree source"):
            fleet_archive.ArchiveBuilder(self.runs, unsafe_id).create(unsafe_manifest)

    def test_archive_rejects_symlinked_lifecycle_ledger_without_copying_target(self) -> None:
        mission_id, manifest = self.create_mission("ledger-symlink")
        external = self.tmp / "external-ledger.jsonl"
        external.write_text('{"secret":"must-not-copy"}\n', encoding="utf-8")
        external.chmod(0o600)
        ledger = self.runs / "fleet-ledger-symlink.ledger.jsonl"
        ledger.symlink_to(external)

        with self.assertRaisesRegex(fleet_archive.ArchiveError, "unsafe lifecycle ledger"):
            fleet_archive.ArchiveBuilder(self.runs, mission_id).create(manifest)

        archive = self.runs / "missions" / mission_id / "archive"
        self.assertFalse((archive / "ledger.jsonl").exists())
        self.assertEqual(external.read_text(encoding="utf-8"), '{"secret":"must-not-copy"}\n')

    def test_redacted_and_hash_only_policies_preserve_hash_evidence(self) -> None:
        for policy in ("redacted", "hash-only"):
            with self.subTest(policy=policy):
                mission_id, manifest = self.create_mission(policy, policy)
                self.add_evidence(policy, mission_id)
                archive = self.runs / "missions" / mission_id / "archive"
                fleet_archive.ArchiveBuilder(self.runs, mission_id, archive).create(manifest)
                index = json.loads((archive / "archive-index.json").read_text(encoding="utf-8"))
                original = next(
                    item for item in index["omissions"] if item["path"] == "objective.txt"
                )
                self.assertIn(policy, original["reason"])
                self.assertFalse((archive / "objective.txt").exists())
                if policy == "redacted":
                    self.assertTrue((archive / "redacted" / "objective.txt.json").is_file())
                else:
                    objective = next(
                        item for item in index["entries"] if item["path"] == "objective.txt"
                    )
                    self.assertEqual(objective["policy"], "hash-only")
                self.assertTrue(fleet_archive.verify_archive(archive)["valid"])

    def test_sensitive_full_archive_requires_specific_unexpired_approval(self) -> None:
        mission_id, manifest = self.create_mission("sensitive-full")
        state.append_event(
            self.runs, mission_id, kind="risk_escalated", actor="CONTROL",
            idempotency_key="sensitive-risk", payload={
                "from": "low", "to": "high", "categories": ["credentials"],
                "reason": "test",
            },
        )
        archive = self.runs / "missions" / mission_id / "archive"
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "without archive approval"):
            fleet_archive.ArchiveBuilder(self.runs, mission_id, archive).create(manifest)
        approval = fleet_approve.approve_archive(
            self.runs, mission_id, scope=str(self.repo), expires_in=600,
            idempotency_key="human:archive:sensitive-full",
        )
        result = fleet_archive.ArchiveBuilder(self.runs, mission_id, archive).create(manifest)
        self.assertTrue(result["valid"])
        archived = json.loads((archive / "archive-approval.json").read_text())
        self.assertEqual(archived["approval_id"], approval["approval"]["approval_id"])
        replay = fleet_archive.ArchiveBuilder(self.runs, mission_id, archive).create(manifest)
        self.assertFalse(replay["created"])
        Path(approval["path"]).unlink()
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "without archive approval"):
            fleet_archive.ArchiveBuilder(self.runs, mission_id, archive).create(manifest)

    def test_assured_archive_binds_signed_audit_without_private_keys(self) -> None:
        mission_id, manifest = self.create_mission("assured")
        state.append_event(
            self.runs, mission_id, kind="risk_escalated", actor="CONTROL",
            idempotency_key="risk", payload={
                "from": "low", "to": "high", "categories": ["production"], "reason": "test",
            },
        )
        request, _ = state.append_event(
            self.runs, mission_id, kind="assurance_requested", actor="CONTROL",
            idempotency_key="request", payload={
                "risk": "high", "categories": ["production"],
                "scope": str(self.repo.resolve()),
                "workflow_digest": self.compiled()["workflow_digest"],
            },
        )
        state.append_event(
            self.runs, mission_id, kind="assurance_approved", actor="HUMAN",
            idempotency_key="approval", payload={
                "approval_id": str(uuid.uuid4()),
                "request_event_sha256": request["event_sha256"],
                "workflow_digest": self.compiled()["workflow_digest"],
                "scope": str(self.repo.resolve()), "risk": "high",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "expires_in_seconds": 600,
                "approved_by_sha256": "b" * 64, "decision": "approved",
            },
        )
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "mode=autonomous\n", "mode=assured\n"
            ),
            encoding="utf-8",
        )
        lifecycle = fleet_audit_client.AuditLifecycle(self.runs, mission_id)
        lifecycle.start(manifest)
        self.addCleanup(lambda: lifecycle.stop() if lifecycle.socket_path.exists() else None)

        archive = self.runs / "missions" / mission_id / "archive"
        result = fleet_archive.ArchiveBuilder(self.runs, mission_id, archive).create(manifest)
        self.assertTrue(result["audit"]["valid"])
        self.assertFalse(result["audit"]["worm"])
        self.assertFalse((archive / "audit" / "control-hmac.key").exists())
        self.assertFalse((archive / "audit" / "audit-signing-private.pem").exists())
        self.assertTrue((archive / "audit" / "audit-signing-public.pem").is_file())
        self.assertTrue(fleet_archive.verify_archive(archive)["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
