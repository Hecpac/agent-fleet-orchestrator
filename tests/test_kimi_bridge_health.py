from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_kimi_state  # noqa: E402


SURFACE = "00000000-0000-4000-8000-000000000111"
WORKSPACE = "00000000-0000-4000-8000-000000000112"
MISSION = "00000000-0000-4000-8000-000000000113"
GENERATION = "00000000-0000-4000-8000-000000000114"


class KimiBridgeHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "state"
        self.config = Path(self.temporary.name) / "config.toml"
        self.config.write_text("[test]\n", encoding="utf-8")
        self.proxy = Path(self.temporary.name) / "proxy"
        self.proxy.write_text("", encoding="utf-8")
        fleet_kimi_state.provision(
            self.root,
            surface_id=SURFACE,
            workspace_id=WORKSPACE,
            mission_id=MISSION,
            generation_id=GENERATION,
            config_source=self.config,
            credential_source=None,
            mcp_command=sys.executable,
            mcp_proxy=str(self.proxy),
        )

    def document(
        self,
        *,
        status: str = "ready",
        pid: int | None = None,
        generation: str = GENERATION,
    ) -> dict:
        arguments = {
            "surface_id": SURFACE,
            "workspace_id": WORKSPACE,
            "mission_id": MISSION,
            "generation_id": generation,
            "launch_id": GENERATION,
            "pid": os.getpid() if pid is None else pid,
            "started_at": 1,
            "status": status,
        }
        if status == "exited":
            arguments.update(
                {
                    "ended_at": 2,
                    "exit_code": 2,
                    "stderr_sha256": "a" * 64,
                    "stderr_tail": "bridge failed",
                }
            )
        return fleet_kimi_state.bridge_document(**arguments)

    def require_health(self) -> dict:
        return fleet_kimi_state.require_bridge_health(
            self.root,
            surface_id=SURFACE,
            workspace_id=WORKSPACE,
            mission_id=MISSION,
            generation_id=GENERATION,
        )

    def test_missing_health_rejects_dispatch(self) -> None:
        with self.assertRaisesRegex(
            fleet_kimi_state.KimiStateError, "bridge health"
        ):
            self.require_health()

    def test_live_binding_ready_health_is_exact(self) -> None:
        self.hold_bridge_lock()
        fleet_kimi_state.publish_bridge_status(self.root, self.document())
        health = self.require_health()
        self.assertEqual(health["status"], "ready")
        self.assertEqual(health["launchId"], GENERATION)

    def test_exit_receipt_invalidates_previously_ready_bridge(self) -> None:
        fleet_kimi_state.publish_bridge_status(self.root, self.document())
        fleet_kimi_state.publish_bridge_status(
            self.root, self.document(status="exited")
        )
        with self.assertRaisesRegex(
            fleet_kimi_state.KimiStateError, "already exited"
        ):
            self.require_health()

    def test_dead_bridge_receipt_does_not_count_as_health(self) -> None:
        fleet_kimi_state.publish_bridge_status(
            self.root, self.document(status="exited", pid=999999)
        )
        with self.assertRaisesRegex(
            fleet_kimi_state.KimiStateError, "already exited"
        ):
            self.require_health()

    def test_duplicate_writer_and_identity_drift_fail_closed(self) -> None:
        fleet_kimi_state.publish_bridge_status(self.root, self.document())
        with self.assertRaisesRegex(
            fleet_kimi_state.KimiStateError, "unsafe Kimi bridge status"
        ):
            fleet_kimi_state.publish_bridge_status(self.root, self.document())
        with self.assertRaisesRegex(
            fleet_kimi_state.KimiStateError, "lifecycle identity drift"
        ):
            fleet_kimi_state.publish_bridge_status(
                self.root,
                self.document(
                    status="exited",
                    generation="00000000-0000-4000-8000-000000000115",
                ),
            )

    def hold_bridge_lock(self) -> int:
        """Hold the surface bridge lock the way a live hook bridge does."""
        lock_path = self.root / SURFACE.upper() / fleet_kimi_state.SURFACE_LOCK
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor

    def test_recycled_pid_of_foreign_live_process_is_rejected(self) -> None:
        """A ready receipt outlives its bridge when the supervisor is killed.

        The operating system later hands that pid to an unrelated process, so
        `os.kill(pid, 0)` succeeds and a dead bridge would pass as live. Only
        the bridge lock proves the bridge itself is still running.
        """
        foreign = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        self.addCleanup(foreign.wait)
        self.addCleanup(foreign.kill)
        fleet_kimi_state.publish_bridge_status(
            self.root, self.document(pid=foreign.pid)
        )
        with self.assertRaisesRegex(
            fleet_kimi_state.KimiStateError, "not alive"
        ):
            self.require_health()

    def test_symlinked_metadata_is_rejected(self) -> None:
        surface = self.root / SURFACE.upper()
        target = surface / "target"
        target.write_text("{}", encoding="utf-8")
        (surface / fleet_kimi_state.BRIDGE_HEALTH).symlink_to(target)
        with self.assertRaisesRegex(
            fleet_kimi_state.KimiStateError, "unsafe Kimi bridge status"
        ):
            fleet_kimi_state.publish_bridge_status(self.root, self.document())


class KimiBridgeSupervisorTests(unittest.TestCase):
    def test_supervisor_records_exit_receipt_and_stderr_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            config = root / "config.toml"
            config.write_text("[test]\n", encoding="utf-8")
            proxy = root / "proxy"
            proxy.write_text("", encoding="utf-8")
            fleet_kimi_state.provision(
                state,
                surface_id=SURFACE,
                workspace_id=WORKSPACE,
                mission_id=MISSION,
                generation_id=GENERATION,
                config_source=config,
                credential_source=None,
                mcp_command=sys.executable,
                mcp_proxy=str(proxy),
            )
            hook_dir = root / "hooks"
            hook_dir.mkdir(mode=0o700)
            bridge = root / "bridge.py"
            bridge.write_text(
                "import sys\n"
                "sys.stderr.write('bridge exploded\\n')\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(ROOT / "scripts" / "kimi_bridge_supervisor.py"),
                "--state-root",
                str(state),
                "--bridge",
                sys.executable,
                "--surface-id",
                SURFACE,
                "--workspace-id",
                WORKSPACE,
                "--mission-id",
                MISSION,
                "--generation-id",
                GENERATION,
                "--launch-id",
                GENERATION,
                "--",
                str(bridge),
                "--hook-dir",
                str(hook_dir),
            ]
            result = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 7, result.stderr)
            receipt_path = state / SURFACE.upper() / fleet_kimi_state.BRIDGE_RECEIPT
            receipt = fleet_kimi_state._parse_object(
                receipt_path.read_bytes(), "receipt"
            )

        self.assertEqual(receipt["exitCode"], 7)
        self.assertEqual(
            receipt["stderrSha256"],
            hashlib.sha256(b"bridge exploded\n").hexdigest(),
        )
        self.assertEqual(receipt["stderrTail"], "bridge exploded\n")


if __name__ == "__main__":
    unittest.main()
