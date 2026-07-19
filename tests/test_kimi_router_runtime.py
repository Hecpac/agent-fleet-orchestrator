from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
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

    def test_runner_provisions_isolated_kimi_identity_mcp_and_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            controller_home = temp / "home"
            controller_kimi = controller_home / ".kimi"
            controller_kimi.mkdir(parents=True)
            (controller_kimi / "config.toml").write_text(
                'default_model = "moonshot-ai/kimi-k3"\n'
                '[models."moonshot-ai/kimi-k3"]\n'
                'provider = "managed:moonshot-ai"\n'
                'model = "kimi-k3"\n'
                'max_context_size = 1048576\n'
                '[providers."managed:moonshot-ai"]\n'
                'type = "kimi"\n'
                'base_url = "https://api.moonshot.ai/v1"\n'
                'api_key = "controller-secret-placeholder"\n',
                encoding="utf-8",
            )
            binary_dir = temp / "bin"
            binary_dir.mkdir()
            fake_kimi = binary_dir / "kimi"
            fake_kimi.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, stat, sys\n"
                "args = sys.argv[1:]\n"
                "config = pathlib.Path(args[args.index('--config-file') + 1])\n"
                "print(json.dumps({\n"
                "  'args': args,\n"
                "  'home': os.environ['HOME'],\n"
                "  'fleet_home': os.environ['FLEET_HOME'],\n"
                "  'share': os.environ['KIMI_SHARE_DIR'],\n"
                "  'pythonpath': os.environ['PYTHONPATH'],\n"
                "  'config_private': stat.S_IMODE(config.stat().st_mode) == 0o600,\n"
                "  'config_has_secret': 'controller-secret-placeholder' in config.read_text(),\n"
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
            result = subprocess.run(
                [
                    "bash",
                    str(RUNNER),
                    "kimi",
                    "verification",
                    "-",
                    str(fake_kimi),
                    "--model",
                    "moonshot-ai/kimi-k3",
                    "--thinking",
                    "--agent-file",
                    ".kimi/agents/fleet-reviewer/agent.yaml",
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
                timeout=20,
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
            self.assertEqual(args[args.index("--model") + 1], "moonshot-ai/kimi-k3")
            self.assertEqual(args.count("--thinking"), 1)
            self.assertEqual(
                Path(args[args.index("--agent-file") + 1]).resolve(),
                (ROOT / ".kimi/agents/fleet-reviewer/agent.yaml").resolve(),
            )
            self.assertEqual(Path(args[args.index("--work-dir") + 1]), ROOT.resolve())
            self.assertEqual(args[args.index("--session") + 1], SURFACE_UUID.lower())
            mcp = json.loads(args[args.index("--mcp-config") + 1])
            self.assertEqual(set(mcp["mcpServers"]), {"fleet_control"})
            self.assertEqual(
                mcp["mcpServers"]["fleet_control"]["args"],
                [str(ROOT / "scripts/fleet_agent_mcp.py")],
            )
            self.assertTrue(value["config_private"])
            self.assertTrue(value["config_has_secret"])
            self.assertTrue(value["home"].startswith("/tmp/fleet_home."))
            self.assertEqual(value["home"], value["fleet_home"])
            self.assertEqual(value["pythonpath"], str(ROOT / "scripts"))
            self.assertTrue(value["share"].startswith(str(state_root)))
            self.assertFalse(Path(value["home"]).exists())
            self.assertFalse((Path(value["share"]) / "config.toml").exists())
            session_file = json.loads(
                (hook_dir / "kimi-hook-sessions.json").read_text(encoding="utf-8")
            )
            session = session_file["sessions"][SURFACE_UUID.lower()]
            self.assertEqual(session["workspaceId"], WORKSPACE_UUID)
            self.assertEqual(session["surfaceId"], SURFACE_UUID)


if __name__ == "__main__":
    unittest.main()
