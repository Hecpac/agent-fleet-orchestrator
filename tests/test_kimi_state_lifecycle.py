from __future__ import annotations

import fcntl
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_frontier  # noqa: E402
import fleet_kimi_state  # noqa: E402
from fleet_ledger import append_event  # noqa: E402


WORKSPACE = "00000000-0000-4000-8000-000000000001"
SURFACE = "00000000-0000-4000-8000-000000000101"
MISSION = "00000000-0000-4000-8000-000000000201"
GENERATION = "00000000-0000-4000-8000-000000000301"


class KimiStateLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.base.chmod(0o700)
        self.root = self.base / "state"
        self.hooks = self.base / "hooks"
        self.hooks.mkdir(mode=0o700)
        self.config = self.base / "config.toml"
        self.config.write_text('default_model = "kimi-code/k3"\n', encoding="utf-8")
        self.config.chmod(0o600)
        self.credential = self.base / "credential.json"
        self.credential.write_text('{"token":"secret"}\n', encoding="utf-8")
        self.credential.chmod(0o600)
        self.share = fleet_kimi_state.provision(
            self.root,
            surface_id=SURFACE,
            workspace_id=WORKSPACE,
            mission_id=MISSION,
            generation_id=GENERATION,
            config_source=self.config,
            credential_source=self.credential,
            mcp_command="/usr/bin/python3",
            mcp_proxy=str(ROOT / "scripts" / "fleet_agent_mcp.py"),
        )
        transcript_parent = (
            self.share / "sessions" / "wd_cwd_test" / "session_test" / "agents" / "main"
        )
        transcript_parent.mkdir(parents=True)
        for directory, _, _ in os.walk(self.share):
            Path(directory).chmod(0o700)
        self.transcript = transcript_parent / "wire.jsonl"
        self.transcript.write_text('{}\n', encoding="utf-8")
        self.transcript.chmod(0o600)
        fleet_kimi_state.update_session_binding(
            self.hooks,
            share_dir=self.share,
            session_id=SURFACE,
            workspace_id=WORKSPACE,
            surface_id=SURFACE,
            mission_id=MISSION,
            generation_id=GENERATION,
            transcript_path=self.transcript,
            provider="moonshot-ai",
            model="kimi-code/k3",
        )

    def test_retirement_quarantines_exact_quiescent_generation_and_unbinds(self) -> None:
        retired = fleet_kimi_state.retire_surface(
            self.root,
            hook_dir=self.hooks,
            surface_id=SURFACE,
            workspace_id=WORKSPACE,
            mission_id=MISSION,
            generation_id=GENERATION,
        )
        self.assertFalse((self.root / SURFACE.upper()).exists())
        self.assertTrue(retired.is_dir())
        self.assertFalse((retired / "share" / "config.toml").exists())
        self.assertFalse(
            (retired / "share" / "credentials" / "kimi-code.json").exists()
        )
        self.assertFalse((retired / "share" / "mcp.json").exists())
        self.assertTrue((retired / fleet_kimi_state.SURFACE_BINDING).is_file())
        self.assertTrue(
            (
                retired
                / "share/sessions/wd_cwd_test/session_test/agents/main/wire.jsonl"
            ).is_file()
        )
        sessions = __import__("json").loads(
            (self.hooks / fleet_kimi_state.SESSION_FILE).read_text(encoding="utf-8")
        )["sessions"]
        self.assertNotIn(SURFACE, sessions)
        # Retirement is idempotent: the quarantined generation is the recovery anchor.
        self.assertEqual(
            fleet_kimi_state.retire_surface(
                self.root,
                hook_dir=self.hooks,
                surface_id=SURFACE,
                workspace_id=WORKSPACE,
                mission_id=MISSION,
                generation_id=GENERATION,
            ),
            retired,
        )

    def test_new_binding_coexists_with_strict_legacy_records(self) -> None:
        import json

        binding_file = self.hooks / fleet_kimi_state.SESSION_FILE
        document = json.loads(binding_file.read_text(encoding="utf-8"))
        legacy_id = "00000000-0000-4000-8000-000000000102"
        current = document["sessions"].pop(SURFACE)
        current["sessionId"] = legacy_id
        current["surfaceId"] = legacy_id.upper()
        document["sessions"] = {
            legacy_id: {
                key: value
                for key, value in current.items()
                if key not in {"missionId", "generationId"}
            }
        }
        binding_file.write_bytes(fleet_kimi_state._canonical_json(document))
        binding_file.chmod(0o600)

        fleet_kimi_state.update_session_binding(
            self.hooks,
            share_dir=self.share,
            session_id=SURFACE,
            workspace_id=WORKSPACE,
            surface_id=SURFACE,
            mission_id=MISSION,
            generation_id=GENERATION,
            transcript_path=self.transcript,
            provider="moonshot-ai",
            model="kimi-code/k3",
        )

        sessions = json.loads(binding_file.read_text(encoding="utf-8"))["sessions"]
        self.assertNotIn("missionId", sessions[legacy_id])
        self.assertEqual(sessions[SURFACE]["generationId"], GENERATION)

    def test_retirement_preserves_everything_while_bridge_lock_is_live(self) -> None:
        lock_path = self.root / SURFACE.upper() / fleet_kimi_state.SURFACE_LOCK
        lock_path.touch(mode=0o600)
        lock_path.chmod(0o600)
        descriptor = os.open(lock_path, os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaisesRegex(fleet_kimi_state.KimiStateError, "still active"):
            fleet_kimi_state.retire_surface(
                self.root,
                hook_dir=self.hooks,
                surface_id=SURFACE,
                workspace_id=WORKSPACE,
                mission_id=MISSION,
                generation_id=GENERATION,
            )
        self.assertTrue((self.root / SURFACE.upper()).is_dir())
        self.assertTrue(self.transcript.is_file())
        self.assertIn(
            SURFACE,
            __import__("json").loads(
                (self.hooks / fleet_kimi_state.SESSION_FILE).read_text(encoding="utf-8")
            )["sessions"],
        )

    def test_frontier_retirement_preserves_indeterminate_and_retained_state(self) -> None:
        runs = self.base / "runs"
        runs.mkdir(mode=0o700)
        feature = "kimi-life"
        ledger = runs / f"fleet-{feature}.ledger.jsonl"
        terminal = {
            "timestamp": "2026-07-26T00:00:00+00:00",
            "completed_at": "2026-07-26T00:00:00+00:00",
            "run_id": "00000000-0000-4000-8000-000000000777",
            "feature": feature,
            "instance": "verify",
            "runner": "interactive",
            "status": "indeterminate",
            "hook_source": "kimi",
            "workspace_uuid": WORKSPACE.upper(),
            "surface_uuid": SURFACE.upper(),
            "mission_id": MISSION,
            "generation_id": GENERATION,
            "lease_retained": True,
        }
        append_event(ledger, terminal, runs_dir=runs)
        with (
            mock.patch.object(fleet_frontier, "KIMI_STATE_ROOT", self.root),
            self.assertRaisesRegex(fleet_frontier.FrontierError, "preserved"),
        ):
            fleet_frontier.retire_kimi_surface(
                runs,
                feature=feature,
                instance="verify",
                workspace_uuid=WORKSPACE,
                surface_uuid=SURFACE,
                mission_id=MISSION,
                generation_id=GENERATION,
                hook_dir=self.hooks,
                workspace_quiesced=True,
            )
        self.assertTrue((self.root / SURFACE.upper()).is_dir())
        self.assertTrue(self.transcript.is_file())

    def test_frontier_retirement_requires_durable_releasable_terminal(self) -> None:
        runs = self.base / "runs-success"
        runs.mkdir(mode=0o700)
        feature = "kimi-success"
        append_event(
            runs / f"fleet-{feature}.ledger.jsonl",
            {
                "timestamp": "2026-07-26T00:00:00+00:00",
                "completed_at": "2026-07-26T00:00:00+00:00",
                "run_id": "00000000-0000-4000-8000-000000000778",
                "feature": feature,
                "instance": "verify",
                "runner": "interactive",
                "status": "succeeded",
                "hook_source": "kimi",
                "workspace_uuid": WORKSPACE.upper(),
                "surface_uuid": SURFACE.upper(),
                "mission_id": MISSION,
                "generation_id": GENERATION,
            },
            runs_dir=runs,
        )
        with mock.patch.object(fleet_frontier, "KIMI_STATE_ROOT", self.root):
            result = fleet_frontier.retire_kimi_surface(
                runs,
                feature=feature,
                instance="verify",
                workspace_uuid=WORKSPACE,
                surface_uuid=SURFACE,
                mission_id=MISSION,
                generation_id=GENERATION,
                hook_dir=self.hooks,
                workspace_quiesced=True,
            )
        self.assertEqual(result["status"], "retired")
        self.assertEqual(result["terminal_runs"], 1)
        self.assertFalse((self.root / SURFACE.upper()).exists())

    def test_opencode_cleanup_never_runs_for_indeterminate_retained_terminal(self) -> None:
        runs = self.base / "runs-opencode"
        runs.mkdir(mode=0o700)
        state = {
            "run_id": "00000000-0000-4000-8000-000000000779",
            "feature": "open-preserve",
            "instance": "review",
            "role": "glm",
            "phase": "VERIFY",
            "runner": "interactive",
            "task_sha256": "0" * 64,
            "workspace_uuid": WORKSPACE.upper(),
            "surface_uuid": SURFACE.upper(),
            "provider": "zai",
            "model": "glm-5.2",
            "hook_source": "opencode",
            "tracking_protocol": "control-v1",
        }
        with mock.patch.object(
            fleet_frontier, "cleanup_opencode_data_home"
        ) as cleanup:
            terminal = fleet_frontier.terminalize(
                runs,
                state,
                status="indeterminate",
                reason="evidence_uncertain",
                release_lease=False,
            )
        self.assertTrue(terminal["lease_retained"])
        cleanup.assert_not_called()

    def test_provision_rejects_symlink_destination_without_touching_target(self) -> None:
        other_surface = "00000000-0000-4000-8000-000000000102"
        outside = self.base / "outside"
        outside.mkdir(mode=0o700)
        (self.root / other_surface.upper()).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(fleet_kimi_state.KimiStateError):
            fleet_kimi_state.provision(
                self.root,
                surface_id=other_surface,
                workspace_id=WORKSPACE,
                mission_id=MISSION,
                generation_id="00000000-0000-4000-8000-000000000302",
                config_source=self.config,
                credential_source=self.credential,
                mcp_command="/usr/bin/python3",
                mcp_proxy=str(ROOT / "scripts" / "fleet_agent_mcp.py"),
            )
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
