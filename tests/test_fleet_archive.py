from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_archive
import fleet_audit_client
import fleet_mission
import fleet_mission_state as state
import workflow_config

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
            f"target_repo={self.repo.resolve()}\nworkspace=workspace:1\n",
            encoding="utf-8",
        )
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
                f"build.worktree={self.repo.resolve()}\n"
                f"build.base_sha={self.base_sha}\n"
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
            f"feature=empty-tree\nmission_id={mission_id}\ntarget_repo={empty.resolve()}\n",
            encoding="utf-8",
        )

        result = fleet_archive.ArchiveBuilder(self.runs, mission_id).create(manifest)

        self.assertTrue(result["valid"])
        self.assertEqual(result["base_sha"], result["final_sha"])
        self.assertTrue(
            fleet_archive.verify_archive(
                self.runs / "missions" / mission_id / "archive", repo=empty
            )["valid"]
        )

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
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "non-symlink"):
            fleet_archive.ArchiveBuilder(self.runs, unsafe_id).create(unsafe_manifest)

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
                "approved_by_sha256": "b" * 64, "decision": "approved",
            },
        )
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write("mode=assured\n")
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
