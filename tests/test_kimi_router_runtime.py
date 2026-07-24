from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run-interactive-agent.sh"
WORKSPACE_UUID = "00000000-0000-0000-0000-000000000001"
SURFACE_UUID = "00000000-0000-0000-0000-000000000101"


class KimiRouterRuntimeTests(unittest.TestCase):
    @staticmethod
    def serve_preflight(
        server: socket.socket,
    ) -> tuple[threading.Thread, list[dict], list[BaseException]]:
        received: list[dict] = []
        errors: list[BaseException] = []

        def serve() -> None:
            try:
                connection, _ = server.accept()
                with connection:
                    raw = bytearray()
                    while not raw.endswith(b"\n"):
                        chunk = connection.recv(65_536)
                        if not chunk:
                            break
                        raw.extend(chunk)
                    received.append(json.loads(raw))
                    connection.sendall(b'{"ok":false,"error":"generic denial"}\n')
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return thread, received, errors

    def _wait_for(self, predicate, *, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for Kimi runtime evidence")

    def test_runner_provisions_isolated_kimi_code_home_mcp_and_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            controller_home = temp / "home"
            controller_kimi = controller_home / ".kimi"
            controller_kimi.mkdir(parents=True)
            (controller_kimi / "config.toml").write_text(
                'default_model = "kimi-code/k3"\n'
                '[models."kimi-code/k3"]\n'
                'provider = "managed:moonshot-ai"\n'
                'model = "k3"\n'
                'max_context_size = 1048576\n'
                '[providers."managed:moonshot-ai"]\n'
                'type = "kimi"\n'
                'base_url = "https://api.moonshot.ai/v1"\n'
                'api_key = "controller-secret-placeholder"\n',
                encoding="utf-8",
            )
            credentials_dir = controller_kimi / "credentials"
            credentials_dir.mkdir()
            (credentials_dir / "kimi-code.json").write_text(
                '{"token": "controller-oauth-placeholder"}\n', encoding="utf-8"
            )
            binary_dir = temp / "bin"
            binary_dir.mkdir()
            fake_kimi = binary_dir / "kimi"
            fake_kimi.write_text(
                "#!/usr/bin/env python3\n"
                "import hashlib, json, os, pathlib, stat, sys, time\n"
                "home = pathlib.Path(os.environ['KIMI_CODE_HOME'])\n"
                "config = home / 'config.toml'\n"
                "creds = home / 'credentials' / 'kimi-code.json'\n"
                "mcp = home / 'mcp.json'\n"
                "cwd = os.getcwd()\n"
                "session = (home / 'sessions'\n"
                "    / ('wd_cwd_' + hashlib.sha256(cwd.encode()).hexdigest()[:12])\n"
                "    / 'session_99999999-9999-4999-8999-999999999999'\n"
                "    / 'agents' / 'main')\n"
                "session.mkdir(parents=True)\n"
                "(session / 'wire.jsonl').write_text(\n"
                "    '{\"type\": \"metadata\", \"protocol_version\": \"1.10\"}\\n')\n"
                "# Mirror a real long-lived TUI: stay alive until the orphaned\n"
                "# bridge has discovered the minted session and recorded it, so\n"
                "# cleanup's 0.2s kill grace never races the bridge's 0.25s poll.\n"
                "# Bounded, so a bridge that never records still fails the test.\n"
                "recorded = pathlib.Path(os.environ['CMUX_HOOK_DIR'])"
                " / 'kimi-hook-sessions.json'\n"
                "deadline = time.monotonic() + 5.0\n"
                "while time.monotonic() < deadline and not recorded.exists():\n"
                "    time.sleep(0.05)\n"
                "print(json.dumps({\n"
                "  'args': sys.argv[1:],\n"
                "  'home': os.environ['HOME'],\n"
                "  'fleet_home': os.environ['FLEET_HOME'],\n"
                "  'kimi_code_home': os.environ['KIMI_CODE_HOME'],\n"
                "  'config_private': stat.S_IMODE(config.stat().st_mode) == 0o600,\n"
                "  'config_has_secret': 'controller-secret-placeholder' in config.read_text(),\n"
                "  'credentials_copied': creds.is_file()\n"
                "      and 'controller-oauth-placeholder' in creds.read_text(),\n"
                "  'mcp': json.loads(mcp.read_text()),\n"
                "}))\n",
                encoding="utf-8",
            )
            fake_kimi.chmod(0o700)
            socket_path = temp / "control.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(server.close)
            server.bind(str(socket_path))
            socket_path.chmod(0o600)
            server.listen(1)
            thread, received, errors = self.serve_preflight(server)
            hook_dir = temp / "hooks"
            state_root = temp / "kimi-state"
            self.addCleanup(
                subprocess.run,
                ["pkill", "-f", str(state_root)],
                check=False,
            )
            result = subprocess.run(
                [
                    "bash",
                    str(RUNNER),
                    "kimi",
                    "verification",
                    "-",
                    str(fake_kimi),
                    "--model",
                    "kimi-code/k3",
                    "--plan",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(controller_home),
                    "PATH": f"{binary_dir}:{os.environ['PATH']}",
                    "FLEET_CONTROL_SOCKET": str(socket_path),
                    "FLEET_KIMI_STATE_ROOT": str(state_root),
                    "CMUX_HOOK_DIR": str(hook_dir),
                    "CMUX_WORKSPACE_ID": WORKSPACE_UUID,
                    "CMUX_SURFACE_ID": SURFACE_UUID,
                },
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            thread.join(timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(received), 1)
            value = json.loads(result.stdout)
            args = value["args"]
            self.assertEqual(args.count("--model"), 1)
            self.assertEqual(args[args.index("--model") + 1], "kimi-code/k3")
            self.assertEqual(args.count("--plan"), 1)
            for dead_flag in (
                "--thinking",
                "--agent-file",
                "--config-file",
                "--work-dir",
                "--session",
                "--mcp-config",
            ):
                self.assertNotIn(dead_flag, args)
            self.assertTrue(value["config_private"])
            self.assertTrue(value["config_has_secret"])
            self.assertTrue(value["credentials_copied"])
            self.assertEqual(set(value["mcp"]["mcpServers"]), {"fleet_control"})
            self.assertEqual(
                value["mcp"]["mcpServers"]["fleet_control"]["args"],
                [str(ROOT / "scripts/fleet_agent_mcp.py")],
            )
            self.assertTrue(value["home"].startswith("/tmp/fleet_home."))
            self.assertEqual(value["home"], value["fleet_home"])
            self.assertTrue(value["kimi_code_home"].startswith(str(state_root)))
            self.assertFalse(Path(value["home"]).exists())
            # The bridge discovers the minted session and records the binding
            # after the provider exits; give the orphaned watcher a moment.
            session_path = hook_dir / "kimi-hook-sessions.json"
            self._wait_for(session_path.exists)
            session_file = json.loads(session_path.read_text(encoding="utf-8"))
            session = session_file["sessions"][SURFACE_UUID.lower()]
            self.assertEqual(session["workspaceId"], WORKSPACE_UUID)
            self.assertEqual(session["surfaceId"], SURFACE_UUID)
            self.assertIn(
                "session_99999999-9999-4999-8999-999999999999",
                session["transcriptPath"],
            )
            self.assertEqual(session["model"], "kimi-code/k3")


if __name__ == "__main__":
    unittest.main()
