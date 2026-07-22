from __future__ import annotations

import os
from pathlib import Path
import json
import socket
import subprocess
import shutil
import sys
import tempfile
import threading
import tomllib
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run-interactive-agent.sh"
HOOK_BRIDGE = ROOT / "scripts" / "cmux-codex-hook.sh"
sys.path.insert(0, str(ROOT / "scripts"))
import fleet_frontier  # noqa: E402


class InteractiveAgentEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller_codex = tempfile.TemporaryDirectory()
        self.addCleanup(self.controller_codex.cleanup)
        self.controller_codex_home = Path(self.controller_codex.name) / "codex"
        self.controller_codex_home.mkdir()
        (self.controller_codex_home / "auth.json").write_text(
            '{"test":"authentication-placeholder"}\n', encoding="utf-8"
        )
        (self.controller_codex_home / "auth.json").chmod(0o600)

    def test_cmux_hook_bridge_exposes_only_fixed_codex_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "cmux.log"
            fake = root / "cmux"
            fake.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" > \"$FLEET_TEST_LOG\"\n"
                "cat >> \"$FLEET_TEST_LOG\"\n"
                "printf '{}\\n'\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            env = {
                **os.environ,
                "CMUX_BUNDLED_CLI_PATH": str(fake),
                "CMUX_SURFACE_ID": "surface:test",
                "CMUX_SOCKET_PATH": "/tmp/cmux-test.sock",
                "FLEET_TEST_LOG": str(log),
            }
            expected = {
                "SessionStart": "--socket /tmp/cmux-test.sock hooks codex session-start",
                "UserPromptSubmit": "--socket /tmp/cmux-test.sock hooks codex prompt-submit",
                "Stop": "--socket /tmp/cmux-test.sock hooks codex stop",
            }
            for event, command in expected.items():
                with self.subTest(event=event):
                    result = subprocess.run(
                        ["bash", str(HOOK_BRIDGE), event], input='{"test":true}\n',
                        env=env, text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(log.read_text(encoding="utf-8").splitlines()[0], command)
            log.unlink()
            ignored = subprocess.run(
                ["bash", str(HOOK_BRIDGE), "PermissionRequest"], input="{}\n",
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(ignored.returncode, 0, ignored.stderr)
            self.assertFalse(log.exists())
            disabled = subprocess.run(
                ["bash", str(HOOK_BRIDGE), "UserPromptSubmit"], input="{}\n",
                env={**env, "CMUX_CODEX_HOOKS_DISABLED": "1"},
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(disabled.returncode, 0, disabled.stderr)
            self.assertFalse(log.exists())

    def test_cmux_hook_drops_partial_payload_when_stdin_copy_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_cat = root / "cat"
            fake_cat.write_text(
                "#!/bin/sh\n"
                "printf '{\\\"partial\\\":true}'\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_cat.chmod(0o700)
            cmux = root / "cmux"
            cmux.write_text("#!/bin/sh\nprintf '{}\\n'\n", encoding="utf-8")
            cmux.chmod(0o700)
            result = subprocess.run(
                ["bash", str(HOOK_BRIDGE), "UserPromptSubmit"],
                input='{"complete":true}\n',
                env={
                    **os.environ,
                    "PATH": f"{root}:{os.environ['PATH']}",
                    "CMUX_BUNDLED_CLI_PATH": str(cmux),
                    "CMUX_SURFACE_ID": "surface:test",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "{}")

    def run_role(
        self,
        role: str,
        *,
        authority: str = "advisory",
        required: str = "-",
        extra_env: dict[str, str] | None = None,
        expression: str = "{}",
        profile: str = "native",
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.controller_codex_home)
        env["SSH_AUTH_SOCK"] = "/tmp/controller-ssh-agent.sock"
        env["AWS_ACCESS_KEY_ID"] = "controller-aws-key"
        env["AWS_SECRET_ACCESS_KEY"] = "controller-aws-secret"
        env["FLEET_EXECUTION_PROFILE"] = profile
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [
                "bash",
                str(RUNNER),
                role,
                authority,
                required,
                "python3",
                "-c",
                f"import json, os; print(json.dumps({expression}, sort_keys=True))",
            ],
            cwd=cwd or ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def parse(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def serve_one_preflight_denial(
        server: socket.socket,
    ) -> tuple[threading.Thread, list[dict[str, object]], list[BaseException]]:
        received: list[dict[str, object]] = []
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
                    connection.sendall(
                        json.dumps({"ok": False, "error": "generic denial"}).encode()
                        + b"\n"
                    )
            except BaseException as exc:  # reported by the owning test thread
                errors.append(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return thread, received, errors

    def resolved_opencode_policy(
        self,
        *,
        bash_enabled: bool = False,
        fleet_control_enabled: bool = False,
    ) -> dict[str, object]:
        document = {
            "name": "fleet-reviewer",
            "permission": [
                {"permission": "*", "pattern": "*", "action": "deny"},
                {"permission": "read", "pattern": "*", "action": "allow"},
                {"permission": "read", "pattern": "*.env", "action": "deny"},
                {"permission": "read", "pattern": "*.env.*", "action": "deny"},
                {"permission": "read", "pattern": "*.env.example", "action": "allow"},
                {"permission": "glob", "pattern": "*", "action": "allow"},
                {"permission": "grep", "pattern": "*", "action": "allow"},
                {"permission": "external_directory", "pattern": "*", "action": "deny"},
            ],
            "tools": {
                "invalid": False,
                "bash": bash_enabled,
                "read": True,
                "glob": True,
                "grep": True,
                "edit": False,
                "write": False,
                "task": False,
            },
        }
        if fleet_control_enabled:
            document["permission"].append(
                {
                    "permission": "fleet_control_*",
                    "pattern": "*",
                    "action": "allow",
                }
            )
            for name in (
                "dispatch",
                "dispatch_many",
                "wait",
                "get_result",
                "relay_result",
                "request_assurance",
                "request_human",
                "inspect_roster",
                "inspect_mission",
            ):
                document["tools"][f"fleet_control_{name}"] = True
        return document

    def test_isolates_home_credentials_and_git_identity(self) -> None:
        result = self.run_role(
            "codex",
            authority="write",
            expression="{key: os.environ.get(key) for key in ("
            "'HOME','USER','LOGNAME','FLEET_HUMAN_UID','SSH_AUTH_SOCK',"
            "'AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','GIT_AUTHOR_NAME',"
            "'GIT_AUTHOR_EMAIL','GIT_COMMITTER_NAME','GIT_COMMITTER_EMAIL',"
            "'CMUX_HOOK_DIR','CMUX_EVENTS_LOG','FLEET_CONTROLLER_HOOK_DIR',"
            "'FLEET_CONTROLLER_EVENTS_LOG')}",
        )
        values = self.parse(result)
        isolated_home = Path(str(values["HOME"]))
        self.assertEqual(values["USER"], "fleet_worker")
        self.assertEqual(values["LOGNAME"], "fleet_worker")
        self.assertRegex(str(values["FLEET_HUMAN_UID"]), r"^sha256:[0-9a-f]{64}$")
        self.assertIsNone(values["SSH_AUTH_SOCK"])
        self.assertIsNone(values["AWS_ACCESS_KEY_ID"])
        self.assertIsNone(values["AWS_SECRET_ACCESS_KEY"])
        self.assertEqual(values["GIT_AUTHOR_NAME"], "FleetMaker")
        self.assertEqual(values["GIT_AUTHOR_EMAIL"], "maker@fleet.local")
        self.assertEqual(values["GIT_COMMITTER_NAME"], "FleetMaker")
        self.assertEqual(values["GIT_COMMITTER_EMAIL"], "maker@fleet.local")
        self.assertTrue(str(isolated_home).startswith("/tmp/fleet_home."))
        self.assertEqual(
            values["CMUX_HOOK_DIR"],
            str(Path.home() / ".cmuxterm"),
        )
        self.assertEqual(
            values["CMUX_EVENTS_LOG"],
            str(Path.home() / ".cmuxterm" / "events.jsonl"),
        )
        self.assertEqual(values["FLEET_CONTROLLER_HOOK_DIR"], values["CMUX_HOOK_DIR"])
        self.assertEqual(
            values["FLEET_CONTROLLER_EVENTS_LOG"], values["CMUX_EVENTS_LOG"]
        )
        self.assertFalse(isolated_home.exists(), "ephemeral HOME must be removed")

    def test_compiled_authority_pair_reaches_provider_environment_exactly(self) -> None:
        compiled_path = "/tmp/fleet-compiled-authority.json"
        compiled_digest = "a" * 64
        values = self.parse(
            self.run_role(
                "codex",
                extra_env={
                    "FLEET_COMPILED_WORKFLOW": compiled_path,
                    "FLEET_COMPILED_DIGEST": compiled_digest,
                },
                expression="{key: os.environ.get(key) for key in ("
                "'FLEET_COMPILED_WORKFLOW','FLEET_COMPILED_DIGEST')} ",
            )
        )
        self.assertEqual(values["FLEET_COMPILED_WORKFLOW"], compiled_path)
        self.assertEqual(values["FLEET_COMPILED_DIGEST"], compiled_digest)

        for name, environment in (
            ("missing-digest", {"FLEET_COMPILED_WORKFLOW": compiled_path}),
            ("relative-path", {
                "FLEET_COMPILED_WORKFLOW": "relative.json",
                "FLEET_COMPILED_DIGEST": compiled_digest,
            }),
            ("invalid-digest", {
                "FLEET_COMPILED_WORKFLOW": compiled_path,
                "FLEET_COMPILED_DIGEST": "A" * 64,
            }),
        ):
            with self.subTest(name=name):
                rejected = self.run_role("codex", extra_env=environment)
                self.assertEqual(rejected.returncode, 2)

    def test_claude_receives_minimal_hardened_configuration(self) -> None:
        result = self.run_role(
            "claude",
            expression="(lambda fleet_home, p, s: {"
            "'bootstrap_home_is_controller': os.environ['HOME'] != fleet_home,"
            "'config_under_fleet_home': p.startswith(fleet_home + '/'),"
            "'bootstrap_state': json.load(open(fleet_home + '/.claude.json', encoding='utf-8')),"
            "'session_home': s['env']['HOME'],"
            "'session_user': s['env']['USER'],"
            "'llm_model': os.environ.get('LLM_MODEL'),"
            "'config_edit_denied': ('Edit(/' + fleet_home + '/.claude/**)') in s['permissions']['deny'],"
            "'config_write_denied': (fleet_home + '/.claude') in s['sandbox']['filesystem']['denyWrite'],"
            "'default_mode': s['permissions']['defaultMode'],"
            "'allow': s['permissions']['allow'],"
            "'disable_bypass': s['permissions']['disableBypassPermissionsMode'],"
            "'sandbox_enabled': s['sandbox']['enabled'],"
            "'sandbox_fail_closed': s['sandbox']['failIfUnavailable'],"
            "'unsandboxed_disabled': not s['sandbox']['allowUnsandboxedCommands'],"
            "'permission_hook': 'PermissionRequest' in s['hooks']"
            "})(os.environ['FLEET_HOME'], os.environ['FLEET_HOME'] + '/.claude', "
            "json.load(open(os.environ['FLEET_HOME'] + '/.claude/settings.json', "
            "encoding='utf-8')))",
        )
        values = self.parse(result)
        self.assertTrue(values["bootstrap_home_is_controller"])
        self.assertTrue(values["config_under_fleet_home"])
        bootstrap_state = values["bootstrap_state"]
        self.assertTrue(bootstrap_state["hasCompletedOnboarding"])
        self.assertLessEqual(set(bootstrap_state), {"hasCompletedOnboarding", "oauthAccount"})
        if "oauthAccount" in bootstrap_state:
            self.assertEqual(
                set(bootstrap_state["oauthAccount"]),
                {"accountUuid", "organizationUuid"},
            )
        self.assertTrue(str(values["session_home"]).startswith("/tmp/fleet_home."))
        self.assertEqual(values["session_user"], "fleet_worker")
        self.assertIsNone(values["llm_model"])
        self.assertTrue(values["config_edit_denied"])
        self.assertTrue(values["config_write_denied"])
        self.assertEqual(values["default_mode"], "default")
        self.assertEqual(
            values["allow"],
            [
                f"Read(/{ROOT}/orchestration/runs/prompts/**)",
                "mcp__fleet_control__*",
            ],
        )
        template = (ROOT / "orchestration" / "claude-fleet-settings.json").read_text()
        self.assertNotIn("/Users/hector", template)
        self.assertEqual(values["disable_bypass"], "disable")
        self.assertTrue(values["sandbox_enabled"])
        self.assertTrue(values["sandbox_fail_closed"])
        self.assertTrue(values["unsandboxed_disabled"])
        self.assertTrue(values["permission_hook"])

    def test_codex_roles_use_one_curated_ephemeral_hook_home(self) -> None:
        for role in ("codex", "codex_candidate"):
            with self.subTest(role=role):
                with tempfile.TemporaryDirectory() as directory:
                    controller_codex_home = Path(directory) / "controller-codex"
                    controller_codex_home.mkdir()
                    (controller_codex_home / "auth.json").write_text(
                        '{"test":"authentication-placeholder"}\n', encoding="utf-8"
                    )
                    (controller_codex_home / "auth.json").chmod(0o600)
                    (controller_codex_home / "hooks.json").write_text(
                        json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{
                            "type": "command", "command": "python3 unrelated.py"
                        }]}]}}) + "\n",
                        encoding="utf-8",
                    )
                    values = self.parse(self.run_role(
                        role,
                        extra_env={"CODEX_HOME": str(controller_codex_home)},
                        expression="(lambda p: {"
                        "'codex_home': p,"
                        "'fleet_home': os.environ['FLEET_HOME'],"
                        "'auth_is_link': os.path.islink(p + '/auth.json'),"
                        "'auth_target': os.readlink(p + '/auth.json'),"
                        "'hooks_present': os.path.exists(p + '/hooks.json'),"
                        "'config': open(p + '/config.toml', encoding='utf-8').read(),"
                        "'cmux_hooks_disabled': os.environ.get('CMUX_CODEX_HOOKS_DISABLED')"
                        "})(os.environ['CODEX_HOME'])",
                    ))
                self.assertEqual(
                    Path(values["codex_home"]).resolve(),
                    (Path(values["fleet_home"]) / ".codex").resolve(),
                )
                self.assertTrue(values["auth_is_link"])
                self.assertEqual(
                    values["auth_target"],
                    str((controller_codex_home / "auth.json").resolve()),
                )
                self.assertFalse(values["hooks_present"])
                self.assertIn("[features]\nhooks = true\n", values["config"])
                self.assertIn('cli_auth_credentials_store = "file"', values["config"])
                self.assertIn('[history]\npersistence = "none"', values["config"])
                self.assertIn('"CODEX_HOME"', values["config"])
                self.assertEqual(values["cmux_hooks_disabled"], "1")

    def test_codex_cli_gets_exact_repo_owned_hook_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_codex = Path(directory) / "codex"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "python3 - \"$@\" <<'PY'\n"
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "  'argv': sys.argv[1:],\n"
                "  'hooks_file': os.path.exists(os.environ['CODEX_HOME'] + '/hooks.json'),\n"
                "  'cmux_disabled': os.environ.get('CMUX_CODEX_HOOKS_DISABLED'),\n"
                "}))\n"
                "PY\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o700)
            result = subprocess.run(
                [
                    "bash", str(RUNNER), "codex", "advisory", "-",
                    str(fake_codex), "--model", "gpt-5.6-sol",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "CODEX_HOME": str(self.controller_codex_home),
                    "CMUX_SURFACE_ID": "12345678-1234-4234-9234-123456789ABC",
                },
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        overrides = [
            values["argv"][index + 1]
            for index, value in enumerate(values["argv"])
            if value == "-c"
        ]
        self.assertEqual(values["argv"].count("--enable"), 1)
        self.assertIn("hooks", values["argv"])
        self.assertEqual(
            values["argv"].count("--dangerously-bypass-hook-trust"), 1
        )
        self.assertEqual(len(overrides), 4)
        session = next(value for value in overrides if value.startswith("projects="))
        parsed_session = tomllib.loads(session)
        self.assertEqual(
            parsed_session["projects"],
            {str(ROOT.resolve()): {"trust_level": "untrusted"}},
        )
        self.assertNotIn("mcp_servers", parsed_session)
        self.assertIn("--strict-config", values["argv"])
        for event in ("SessionStart", "UserPromptSubmit", "Stop"):
            matching = [value for value in overrides if value.startswith(f"hooks.{event}=")]
            self.assertEqual(len(matching), 1)
            self.assertIn("scripts/cmux-codex-hook.sh", matching[0])
            self.assertIn(
                "CMUX_SURFACE_ID=12345678-1234-4234-9234-123456789ABC",
                matching[0],
            )
            self.assertIn("CMUX_CODEX_HOOKS_DISABLED=0", matching[0])
        self.assertFalse(values["hooks_file"])
        self.assertEqual(values["cmux_disabled"], "1")

    def test_codex_runner_does_not_duplicate_existing_hook_trust_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_codex = Path(directory) / "codex"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "python3 -c 'import sys; print(sys.argv[1:])' \"$@\"\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o700)
            result = subprocess.run(
                [
                    "bash", str(RUNNER), "codex", "advisory", "-",
                    str(fake_codex), "--dangerously-bypass-hook-trust",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "CODEX_HOME": str(self.controller_codex_home),
                    "CMUX_SURFACE_ID": "12345678-1234-4234-9234-123456789ABC",
                },
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.count("--dangerously-bypass-hook-trust"),
            1,
        )

    def test_codex_healthcheck_keeps_login_status_clean_and_preflights_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "argv.jsonl"
            fake_codex = root / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"with open({str(log)!r}, 'a', encoding='utf-8') as out:\n"
                "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "raise SystemExit(0)\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o700)
            socket_path = root / "specialist.sock"
            control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(control_socket.close)
            control_socket.bind(str(socket_path))
            socket_path.chmod(0o600)
            control_socket.listen(1)
            server_thread, received, errors = self.serve_one_preflight_denial(
                control_socket
            )
            result = subprocess.run(
                [
                    "bash", str(RUNNER), "codex_candidate", "advisory", "-",
                    str(fake_codex), "login", "status",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "CODEX_HOME": str(self.controller_codex_home),
                    "FLEET_CONTROL_SOCKET": str(socket_path),
                    "FLEET_HEALTHCHECK": "1",
                    "FLEET_TEST_LOG": str(log),
                },
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            server_thread.join(timeout=5)
            calls = [
                json.loads(line)
                for line in log.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [["login", "status"], ["login", "status"]])
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["request"]["method"], "ping")

    def test_claude_healthcheck_preflights_the_same_specialist_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_claude = root / "claude"
            fake_claude.write_text("#!/bin/sh\nprintf 'CLAUDE_OK\\n'\n", encoding="utf-8")
            fake_claude.chmod(0o700)
            socket_path = root / "specialist.sock"
            control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(control_socket.close)
            control_socket.bind(str(socket_path))
            socket_path.chmod(0o600)
            control_socket.listen(1)
            server_thread, received, errors = self.serve_one_preflight_denial(
                control_socket
            )
            result = subprocess.run(
                [
                    "bash", str(RUNNER), "claude_reviewer", "verification", "-",
                    str(fake_claude), "--version",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "FLEET_CONTROL_SOCKET": str(socket_path),
                    "FLEET_HEALTHCHECK": "1",
                },
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            server_thread.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "CLAUDE_OK")
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["request"]["method"], "ping")

    def test_mission_specialist_uses_ephemeral_codex_home_with_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller_codex_home = Path(directory) / "controller-codex"
            controller_codex_home.mkdir()
            (controller_codex_home / "auth.json").write_text(
                '{"test":"authentication-placeholder"}\n', encoding="utf-8"
            )
            (controller_codex_home / "auth.json").chmod(0o600)
            (controller_codex_home / "hooks.json").write_text(
                json.dumps({
                    "hooks": {
                        "UserPromptSubmit": [{"hooks": [{
                            "type": "command", "command": "cmux hooks feed --source codex"
                        }, {
                            "type": "command",
                            "command": "python3 unrelated.py # cmux hooks codex prompt-submit",
                        }]}],
                        "Stop": [{"hooks": [{
                            "type": "command", "command": "cmux hooks feed --source codex"
                        }]}],
                    }
                }) + "\n",
                encoding="utf-8",
            )
            (controller_codex_home / "config.toml").write_text(
                'sandbox_mode = "danger-full-access"\n\n'
                '[hooks.state]\n'
                f'[hooks.state."{controller_codex_home}/hooks.json:user_prompt_submit:0:0"]\n'
                'trusted_hash = "sha256:test"\n',
                encoding="utf-8",
            )
            values = self.parse(
                self.run_role(
                    "codex_candidate",
                    authority="advisory",
                    extra_env={
                        "CODEX_HOME": str(controller_codex_home),
                        "FLEET_MISSION_ID": "12345678-1234-4234-9234-123456789abc",
                        "FLEET_CONTROL_SOCKET": "/private/tmp/fleet-control/test.sock",
                    },
                    expression="(lambda p: {"
                    "'under_fleet_home': os.path.realpath(p).startswith("
                    "os.path.realpath(os.environ['FLEET_HOME']) + '/'),"
                    "'auth_is_link': os.path.islink(p + '/auth.json'),"
                    "'auth_target': os.readlink(p + '/auth.json'),"
                    "'hooks_present': os.path.exists(p + '/hooks.json'),"
                    "'config': open(p + '/config.toml', encoding='utf-8').read(),"
                    "'config_parses': __import__('tomllib').load(open(p + '/config.toml', 'rb'))['features']['hooks']"
                    "})(os.environ['CODEX_HOME'])",
                )
            )
        self.assertTrue(values["under_fleet_home"])
        self.assertTrue(values["auth_is_link"])
        self.assertEqual(
            values["auth_target"], str((controller_codex_home / "auth.json").resolve())
        )
        self.assertFalse(values["hooks_present"])
        self.assertTrue(values["config_parses"])
        self.assertIn("hooks = true", values["config"])
        self.assertNotIn("sandbox_mode", values["config"])

    def test_mission_codex_profile_denies_auth_controller_and_run_state(self) -> None:
        # Put controller/workspace fixtures on the real project volume. Codex's
        # `:minimal` profile intentionally grants parts of $TMPDIR, so a fixture
        # entirely below the system temp root cannot prove home/run denials.
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            controller_home = root / "controller-home"
            controller_codex_home = controller_home / ".codex"
            runs = controller_home / "project" / "orchestration" / "runs"
            controller_codex_home.mkdir(parents=True)
            runs.mkdir(parents=True)
            (controller_codex_home / "auth.json").write_text(
                '{"test":"credential-must-not-reach-tools"}\n', encoding="utf-8"
            )
            (controller_codex_home / "auth.json").chmod(0o600)
            socket_path = root / "specialist.sock"
            control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(control_socket.close)
            control_socket.bind(str(socket_path))
            socket_path.chmod(0o600)
            control_socket.listen(1)
            fake_codex = root / "codex"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "python3 - \"$@\" <<'PY'\n"
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "  'argv': sys.argv[1:],\n"
                "  'codex_home': os.environ['CODEX_HOME'],\n"
                "  'fleet_home': os.environ['FLEET_HOME'],\n"
                "}))\n"
                "PY\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o700)

            for authority, profile, workspace_access in (
                ("advisory", "fleet_reader", "read"),
                ("write", "fleet_writer", "write"),
            ):
                with self.subTest(authority=authority):
                    server_thread, received, server_errors = (
                        self.serve_one_preflight_denial(control_socket)
                    )
                    result = subprocess.run(
                        [
                            "bash",
                            str(RUNNER),
                            "codex_candidate",
                            authority,
                            "-",
                            str(fake_codex),
                            "--version",
                        ],
                        cwd=ROOT,
                        env={
                            **os.environ,
                            "HOME": str(controller_home),
                            "CODEX_HOME": str(controller_codex_home),
                            "FLEET_MISSION_ID": "12345678-1234-4234-9234-123456789abc",
                            "FLEET_RUNS_DIR": str(runs),
                            "FLEET_CONTROL_SOCKET": str(socket_path),
                        },
                        text=True,
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                    server_thread.join(timeout=5)
                    self.assertFalse(server_thread.is_alive())
                    self.assertEqual(server_errors, [])
                    self.assertEqual(len(received), 1)
                    self.assertEqual(received[0]["request"]["method"], "ping")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    values = json.loads(result.stdout)
                    overrides = [
                        values["argv"][index + 1]
                        for index, argument in enumerate(values["argv"])
                        if argument == "-c"
                    ]
                    permission = next(
                        value for value in overrides if value.startswith("permissions=")
                    )
                    default = next(
                        value
                        for value in overrides
                        if value.startswith("default_permissions=")
                    )
                    parsed = tomllib.loads(permission)["permissions"][profile]
                    self.assertEqual(
                        tomllib.loads(default)["default_permissions"], profile
                    )
                    filesystem = parsed["filesystem"]
                    self.assertNotIn(":root", filesystem)
                    self.assertEqual(filesystem["glob_scan_max_depth"], 64)
                    self.assertEqual(filesystem[":minimal"], "read")
                    self.assertEqual(
                        filesystem[":workspace_roots"], {".": workspace_access}
                    )
                    for denied in (
                        controller_home.resolve(),
                        controller_codex_home.resolve(),
                        Path(values["codex_home"]).resolve(),
                        runs.resolve(),
                    ):
                        self.assertEqual(filesystem[str(denied)], "deny")
                        self.assertEqual(
                            filesystem[str(denied).rstrip("/") + "/**"], "deny"
                        )
                    for auth_path in (
                        controller_codex_home.resolve() / "auth.json",
                        Path(values["codex_home"]).resolve() / "auth.json",
                    ):
                        self.assertEqual(filesystem[str(auth_path)], "deny")
                    lexical_fleet_home = Path(values["codex_home"]).absolute()
                    canonical_fleet_home = Path(values["codex_home"]).resolve()
                    self.assertEqual(filesystem[str(lexical_fleet_home)], "deny")
                    self.assertEqual(
                        filesystem[str(lexical_fleet_home).rstrip("/") + "/**"],
                        "deny",
                    )
                    self.assertEqual(filesystem[str(canonical_fleet_home)], "deny")
                    self.assertEqual(
                        filesystem[str(canonical_fleet_home).rstrip("/") + "/**"],
                        "deny",
                    )
                    for denied in (
                        controller_home.absolute(),
                        controller_codex_home.absolute(),
                        Path(values["codex_home"]).absolute(),
                        runs.absolute(),
                    ):
                        self.assertEqual(filesystem[str(denied)], "deny")
                    self.assertEqual(
                        parsed["network"]["unix_sockets"],
                        {str(socket_path): "allow"},
                    )
                    self.assertNotIn("credential-must-not-reach-tools", result.stdout)

                    if os.environ.get("FLEET_RUN_CODEX_PERMISSION_CANARY") == "1":
                        codex = shutil.which("codex")
                        self.assertIsNotNone(codex)
                        workspace = controller_home / "project"
                        visible = workspace / "visible.txt"
                        visible.write_text("workspace-visible\n", encoding="utf-8")
                        run_secret = runs / "capability-token.json"
                        run_secret.write_text("run-secret\n", encoding="utf-8")
                        fleet_codex_home = Path(values["codex_home"])
                        fleet_codex_home.mkdir(mode=0o700, parents=True)
                        fleet_auth = fleet_codex_home / "auth.json"
                        fleet_auth.write_text("fleet-auth-secret\n", encoding="utf-8")
                        clean_codex_home = root / "canary-codex-home"
                        clean_codex_home.mkdir(mode=0o700, exist_ok=True)
                        deny_lines = "\n".join(
                            f"{json.dumps(path)} = \"deny\""
                            for path, access in filesystem.items()
                            if access == "deny"
                        )
                        deny_lines += "\n" + "\n".join(
                            f"{json.dumps(str(path))} = \"deny\""
                            for path in (
                                controller_codex_home / "auth.json",
                                fleet_auth,
                                run_secret,
                            )
                        )
                        (clean_codex_home / "config.toml").write_text(
                            f'default_permissions = "{profile}"\n\n'
                            f'[permissions.{profile}]\n'
                            'description = "Fleet permission canary"\n\n'
                            f'[permissions.{profile}.filesystem]\n'
                            '":minimal" = "read"\n'
                            f'{deny_lines}\n\n'
                            f'[permissions.{profile}.filesystem.":workspace_roots"]\n'
                            f'"." = "{workspace_access}"\n',
                            encoding="utf-8",
                        )
                        write_probe = workspace / f"{profile}-write.txt"
                        command = (
                            '/bin/cat "$1" >/dev/null || exit 30; '
                            'if [ "$5" = read ] && echo denied > "$6" 2>/dev/null; '
                            'then exit 33; fi; '
                            'for secret in "$2" "$3" "$4"; do '
                            'if /bin/cat "$secret" >/dev/null 2>&1; then exit 31; fi; '
                            'done; '
                            'if [ "$5" = write ]; then '
                            'echo allowed > "$6" || exit 32; fi'
                        )
                        canary_env = {
                            key: value
                            for key, value in os.environ.items()
                            if not key.startswith("CODEX_")
                        }
                        canary_env["CODEX_HOME"] = str(clean_codex_home)
                        canary = subprocess.run(
                            [
                                str(codex),
                                "sandbox",
                                "-P",
                                profile,
                                "-C",
                                str(workspace),
                                "--",
                                "/bin/sh",
                                "-c",
                                command,
                                "fleet-codex-permission-canary",
                                str(visible),
                                str(controller_codex_home / "auth.json"),
                                str(fleet_auth),
                                str(run_secret),
                                workspace_access,
                                str(write_probe),
                            ],
                            env=canary_env,
                            text=True,
                            capture_output=True,
                            timeout=20,
                            check=False,
                        )
                        self.assertEqual(canary.returncode, 0, canary.stderr)
                        self.assertEqual(write_probe.exists(), workspace_access == "write")

    def test_mission_specialist_fails_closed_without_codex_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_role(
                "codex_candidate",
                extra_env={
                    "CODEX_HOME": directory,
                    "FLEET_MISSION_ID": "12345678-1234-4234-9234-123456789abc",
                    "FLEET_CONTROL_SOCKET": "/private/tmp/fleet-control/test.sock",
                },
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Fleet Codex auth binding failed", result.stderr)

    def test_exact_specialist_socket_provisions_controller_owned_mcp_for_each_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller_home = root / "home"
            controller_opencode = controller_home / ".config" / "opencode"
            controller_opencode.mkdir(parents=True)
            (controller_opencode / "opencode.json").write_text(
                json.dumps(
                    {
                        "mcp": {
                            "controller_wide": {
                                "type": "remote",
                                "url": "https://example.invalid/mcp",
                            }
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            socket_path = root / "specialist.sock"
            control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(control_socket.close)
            control_socket.bind(str(socket_path))
            socket_path.chmod(0o600)
            control_socket.listen(1)
            shared_env = {
                "HOME": str(controller_home),
                "FLEET_CONTROL_SOCKET": str(socket_path),
            }

            claude = self.parse(
                self.run_role(
                    "claude",
                    extra_env=shared_env,
                    expression="json.load(open(os.environ['FLEET_HOME'] + "
                    "'/.claude/mcp.json', encoding='utf-8'))",
                )
            )["mcpServers"]["fleet_control"]
            fake_codex = root / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "print(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o700)
            server_thread, received, server_errors = self.serve_one_preflight_denial(
                control_socket
            )
            codex_result = subprocess.run(
                [
                    "bash", str(RUNNER), "codex_candidate", "advisory", "-",
                    str(fake_codex), "--version",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    **shared_env,
                    "CODEX_HOME": str(self.controller_codex_home),
                },
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            server_thread.join(timeout=5)
            self.assertEqual(codex_result.returncode, 0, codex_result.stderr)
            self.assertFalse(server_thread.is_alive())
            self.assertEqual(server_errors, [])
            self.assertEqual(len(received), 1)
            codex_argv = json.loads(codex_result.stdout)
            codex_overrides = [
                codex_argv[index + 1]
                for index, value in enumerate(codex_argv)
                if value == "-c"
            ]
            session_override = next(
                value for value in codex_overrides if value.startswith("mcp_servers=")
            )
            codex = tomllib.loads(session_override)["mcp_servers"]["fleet_control"]
            opencode = self.parse(
                self.run_role(
                    "glm",
                    extra_env=shared_env,
                    expression="{'file': json.load(open("
                    "os.environ['XDG_CONFIG_HOME'] + '/opencode/opencode.json', "
                    "encoding='utf-8')), 'inline': json.loads("
                    "os.environ['OPENCODE_CONFIG_CONTENT'])}",
                )
            )

            self.assertEqual(claude["type"], "stdio")
            self.assertTrue(os.path.isabs(claude["command"]))
            self.assertEqual(
                claude["args"], [str(ROOT / "scripts" / "fleet_agent_mcp.py")]
            )
            self.assertTrue(codex["required"])
            self.assertEqual(codex["startup_timeout_sec"], 10)
            self.assertEqual(codex["tool_timeout_sec"], 1815)
            self.assertTrue(os.path.isabs(codex["command"]))
            self.assertEqual(
                codex["args"], [str(ROOT / "scripts" / "fleet_agent_mcp.py")]
            )
            for source in (opencode["file"], opencode["inline"]):
                self.assertEqual(set(source["mcp"]), {"fleet_control"})
                server = source["mcp"]["fleet_control"]
                self.assertEqual(server["type"], "local")
                self.assertTrue(server["enabled"])
                self.assertEqual(server["timeout"], 10_000)
                self.assertEqual(
                    server["command"][1],
                    str(ROOT / "scripts" / "fleet_agent_mcp.py"),
                )
                self.assertNotIn("controller_wide", source["mcp"])
            for agent in ("fleet-reviewer", "glm-challenger", "minimax-checker"):
                self.assertEqual(
                    opencode["inline"]["agent"][agent]["permission"],
                    {"fleet_control_*": "allow"},
                )
            serialized = json.dumps(
                {"claude": claude, "codex": codex, "opencode": opencode},
                sort_keys=True,
            )
            self.assertNotIn(str(socket_path), serialized)
            self.assertNotIn("token_path", serialized)

            lead = self.parse(
                self.run_role(
                    "codex",
                    authority="control",
                    extra_env=shared_env,
                    expression="__import__('tomllib').load(open("
                    "os.environ['CODEX_HOME'] + '/config.toml', 'rb'))"
                    ".get('mcp_servers')",
                )
            )
            self.assertIsNone(lead)

    def test_sandboxed_profile_narrows_runtime_perimeter_without_changing_role(self) -> None:
        values = self.parse(
            self.run_role(
                "codex",
                authority="write",
                profile="sandboxed",
                expression="{key: os.environ.get(key) for key in ("
                "'FLEET_EXECUTION_PROFILE','FLEET_FILESYSTEM_PERIMETER',"
                "'FLEET_NETWORK_PERIMETER','TMPDIR','XDG_CACHE_HOME','XDG_RUNTIME_DIR')}",
            )
        )
        self.assertEqual(values["FLEET_EXECUTION_PROFILE"], "sandboxed")
        self.assertEqual(values["FLEET_FILESYSTEM_PERIMETER"], "isolated-runtime")
        self.assertEqual(values["FLEET_NETWORK_PERIMETER"], "provider-managed")
        for key in ("TMPDIR", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
            self.assertIn("/tmp/fleet_home.", str(values[key]))

    def test_regulated_profile_requires_mission_identity(self) -> None:
        missing = self.run_role("codex", profile="regulated")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("regulated execution requires a canonical FLEET_MISSION_ID", missing.stderr)
        mission_id = "12345678-1234-4234-9234-123456789abc"
        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "control.sock")
            codex_home = Path(directory) / "codex"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text(
                '{"test":"authentication-placeholder"}\n', encoding="utf-8"
            )
            (codex_home / "auth.json").chmod(0o600)
            control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(control_socket.close)
            control_socket.bind(socket_path)
            control_socket.listen(1)
            values = self.parse(
                self.run_role(
                    "codex",
                    profile="regulated",
                    extra_env={
                        "FLEET_MISSION_ID": mission_id,
                        "FLEET_CONTROL_SOCKET": socket_path,
                        "CODEX_HOME": str(codex_home),
                    },
                    expression="{'mission': os.environ.get('FLEET_MISSION_ID'), "
                    "'policy': os.environ.get('FLEET_EFFECT_POLICY')}",
                )
            )
        self.assertEqual(values, {"mission": mission_id, "policy": "control-only"})

    def test_opencode_receives_only_isolated_provider_xdg_subtrees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for relative in (
                ".config/opencode", ".local/share/opencode", ".local/state/opencode"
            ):
                path = home / relative
                path.mkdir(parents=True)
                (path / "provider.json").write_text("{}\n", encoding="utf-8")
            tool_output = home / ".local" / "share" / "opencode" / "tool-output"
            tool_output.mkdir()
            (tool_output / "controller-history").write_text(
                "must-not-copy\n", encoding="utf-8"
            )
            unrelated = home / ".config" / "other-app"
            unrelated.mkdir()
            (unrelated / "secret").write_text("must-not-copy", encoding="utf-8")
            values = self.parse(self.run_role(
                "glm",
                extra_env={"HOME": str(home)},
                expression="{key: {'root': os.environ.get(key), "
                "'opencode': os.path.isfile(os.environ[key] + '/opencode/provider.json'), "
                "'tool_output': os.path.exists(os.environ[key] + '/opencode/tool-output'), "
                "'other': os.path.exists(os.environ[key] + '/other-app')} "
                "for key in ('XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_STATE_HOME')}",
            ))
        for value in values.values():
            self.assertIn("/tmp/fleet_home.", value["root"])
            self.assertTrue(value["opencode"])
            self.assertFalse(value["other"])
        self.assertFalse(values["XDG_DATA_HOME"]["tool_output"])

    def test_opencode_installs_canonical_agents_outside_orchestrator_cwd(self) -> None:
        agent_names = ("fleet-reviewer", "glm-challenger", "minimax-checker")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target-repo"
            target.mkdir()
            values = self.parse(
                self.run_role(
                    "glm",
                    cwd=target,
                    expression="{name: open(os.environ['XDG_CONFIG_HOME'] + "
                    "'/opencode/agents/' + name + '.md', encoding='utf-8').read() "
                    "for name in ('fleet-reviewer','glm-challenger','minimax-checker')}",
                )
            )
        for name in agent_names:
            expected = (ROOT / ".opencode" / "agents" / f"{name}.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(values[name], expected)

    def test_opencode_live_surface_uses_deterministic_ephemeral_data_home(self) -> None:
        surface_uuid = str(uuid.uuid4()).upper()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            provider = home / ".local" / "share" / "opencode"
            provider.mkdir(parents=True)
            (provider / "provider.json").write_text("{}\n", encoding="utf-8")
            values = self.parse(self.run_role(
                "glm",
                extra_env={"HOME": str(home), "CMUX_SURFACE_ID": surface_uuid},
                expression="{"
                "'data': os.environ['XDG_DATA_HOME'],"
                "'config': os.environ['XDG_CONFIG_HOME'],"
                "'state': os.environ['XDG_STATE_HOME'],"
                "'fleet_home': os.environ['FLEET_HOME'],"
                "'provider': os.path.isfile(os.environ['XDG_DATA_HOME'] + '/opencode/provider.json')"
                "}",
            ))
            resumed = self.parse(self.run_role(
                "glm",
                extra_env={"HOME": str(home), "CMUX_SURFACE_ID": surface_uuid},
                expression="{"
                "'data': os.environ['XDG_DATA_HOME'],"
                "'provider': os.path.isfile(os.environ['XDG_DATA_HOME'] + '/opencode/provider.json')"
                "}",
            ))
        self.assertEqual(
            values["data"],
            f"/tmp/agent-fleet-orchestrator-opencode/{surface_uuid}/data",
        )
        self.assertEqual(resumed["data"], values["data"])
        self.assertTrue(values["config"].startswith(values["fleet_home"] + "/"))
        self.assertTrue(values["state"].startswith(values["fleet_home"] + "/"))
        self.assertTrue(values["provider"])
        self.assertTrue(resumed["provider"])
        self.assertTrue(Path(values["data"]).exists())
        self.assertTrue(fleet_frontier.cleanup_opencode_data_home(surface_uuid))
        self.assertFalse(Path(values["data"]).exists())

    def test_opencode_preserves_relative_symlinks_that_stay_inside_provider_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            package = home / ".config" / "opencode" / "node_modules" / "package"
            package.mkdir(parents=True)
            (package / "tool.py").write_text("internal\n", encoding="utf-8")
            bin_dir = home / ".config" / "opencode" / "node_modules" / ".bin"
            bin_dir.mkdir()
            (bin_dir / "tool").symlink_to("../package/tool.py")
            values = self.parse(self.run_role(
                "glm",
                extra_env={"HOME": str(home)},
                expression="(lambda p: {'is_link': os.path.islink(p), "
                "'content': open(p, encoding='utf-8').read().strip()})("
                "os.environ['XDG_CONFIG_HOME'] + '/opencode/node_modules/.bin/tool')",
            ))
        self.assertTrue(values["is_link"])
        self.assertEqual(values["content"], "internal")

    def test_opencode_rejects_symlinks_that_escape_provider_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            provider = home / ".config" / "opencode"
            provider.mkdir(parents=True)
            (home / ".config" / "outside.json").write_text("{}\n", encoding="utf-8")
            (provider / "escape").symlink_to("../outside.json")
            result = self.run_role("glm", extra_env={"HOME": str(home)})
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink escapes its isolated root", result.stderr)

    def test_opencode_tui_starts_only_after_resolved_policy_passes(self) -> None:
        for bash_enabled in (False, True):
            with self.subTest(bash_enabled=bash_enabled), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                marker = root / "tui-started"
                fake = root / "opencode"
                document = self.resolved_opencode_policy(bash_enabled=bash_enabled)
                fake.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, pathlib, sys\n"
                    f"document = {document!r}\n"
                    "if sys.argv[1:3] == ['debug', 'agent']:\n"
                    "    print(json.dumps(document))\n"
                    "    raise SystemExit(0)\n"
                    f"pathlib.Path({str(marker)!r}).write_text('started\\n')\n"
                    "print('TUI_OK')\n",
                    encoding="utf-8",
                )
                fake.chmod(0o700)
                home = root / "home"
                home.mkdir()
                result = subprocess.run(
                    [
                        "bash",
                        str(RUNNER),
                        "minimax",
                        "advisory",
                        "-",
                        str(fake),
                        "--agent",
                        "fleet-reviewer",
                        "--version",
                    ],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "HOME": str(home),
                        # An inherited marker must not bypass a real --agent launch.
                        "FLEET_HEALTHCHECK": "1",
                    },
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                if bash_enabled:
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("exact specialist set", result.stderr)
                    self.assertFalse(marker.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "TUI_OK")
                    self.assertTrue(marker.exists())

    def test_opencode_specialist_tui_requires_effective_mcp_and_callable_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "tui-started"
            fake = root / "opencode"
            document = self.resolved_opencode_policy(fleet_control_enabled=True)
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                f"document = {document!r}\n"
                "if sys.argv[1:3] == ['debug', 'config']:\n"
                "    print(os.environ['OPENCODE_CONFIG_CONTENT'])\n"
                "    raise SystemExit(0)\n"
                "if sys.argv[1:3] == ['debug', 'agent']:\n"
                "    print(json.dumps(document))\n"
                "    raise SystemExit(0)\n"
                f"pathlib.Path({str(marker)!r}).write_text('started\\n')\n"
                "print('TUI_OK')\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            home = root / "home"
            home.mkdir()
            socket_path = root / "specialist.sock"
            control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(control_socket.close)
            control_socket.bind(str(socket_path))
            socket_path.chmod(0o600)
            control_socket.listen(1)
            server_thread, received, server_errors = self.serve_one_preflight_denial(
                control_socket
            )
            result = subprocess.run(
                [
                    "bash",
                    str(RUNNER),
                    "minimax",
                    "advisory",
                    "-",
                    str(fake),
                    "--agent",
                    "fleet-reviewer",
                    "--version",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "FLEET_CONTROL_SOCKET": str(socket_path),
                },
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            server_thread.join(timeout=5)
            marker_exists = marker.exists()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "TUI_OK")
        self.assertTrue(marker_exists)
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(server_errors, [])
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["request"]["method"], "ping")

    @unittest.skipUnless(
        all(shutil.which(name) for name in ("claude", "codex", "opencode")),
        "installed provider CLIs are required for the MCP discovery canary",
    )
    def test_installed_provider_clis_discover_the_specialist_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            socket_path = root / "specialist.sock"
            control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(control_socket.close)
            control_socket.bind(str(socket_path))
            socket_path.chmod(0o600)
            control_socket.listen(4)
            common_env = {
                **os.environ,
                "HOME": str(home),
                "CODEX_HOME": str(self.controller_codex_home),
                "FLEET_CONTROL_SOCKET": str(socket_path),
            }

            codex_server, codex_received, codex_errors = self.serve_one_preflight_denial(
                control_socket
            )
            codex = subprocess.run(
                [
                    "bash",
                    str(RUNNER),
                    "codex_candidate",
                    "advisory",
                    "-",
                    str(shutil.which("codex")),
                    "mcp",
                    "list",
                    "--json",
                ],
                cwd=ROOT,
                env={**common_env, "FLEET_HEALTHCHECK": "1"},
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            codex_server.join(timeout=5)
            self.assertEqual(codex.returncode, 0, codex.stderr)
            self.assertIn("fleet_control", codex.stdout)
            self.assertFalse(codex_server.is_alive())
            self.assertEqual(codex_errors, [])
            self.assertEqual(len(codex_received), 1)

            server_thread, received, server_errors = self.serve_one_preflight_denial(
                control_socket
            )
            opencode = subprocess.run(
                [
                    "bash",
                    str(RUNNER),
                    "minimax",
                    "advisory",
                    "-",
                    str(shutil.which("opencode")),
                    "--agent",
                    "fleet-reviewer",
                    "--version",
                ],
                cwd=ROOT,
                env=common_env,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            server_thread.join(timeout=5)
            self.assertEqual(opencode.returncode, 0, opencode.stderr)
            self.assertRegex(opencode.stdout, r"\d+\.\d+\.\d+")
            self.assertFalse(server_thread.is_alive())
            self.assertEqual(server_errors, [])
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["request"]["method"], "ping")

            claude_env = {
                **common_env,
                "HOME": os.environ.get("HOME", str(Path.home())),
            }
            claude_server, claude_received, claude_errors = (
                self.serve_one_preflight_denial(control_socket)
            )
            auth = subprocess.run(
                [
                    "bash",
                    str(RUNNER),
                    "claude_reviewer",
                    "verification",
                    "-",
                    str(shutil.which("claude")),
                    "auth",
                    "status",
                    "--json",
                ],
                cwd=ROOT,
                env={**claude_env, "FLEET_HEALTHCHECK": "1"},
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            claude_server.join(timeout=5)
            self.assertFalse(claude_server.is_alive())
            self.assertEqual(claude_errors, [])
            self.assertEqual(len(claude_received), 1)
            try:
                auth_status = json.loads(auth.stdout)
            except json.JSONDecodeError as exc:
                self.fail(
                    "Claude auth preflight did not return JSON: "
                    f"{exc}; stderr={auth.stderr!r}; stdout={auth.stdout!r}"
                )
            if (
                not isinstance(auth_status, dict)
                or type(auth_status.get("loggedIn")) is not bool
            ):
                self.fail(f"Claude auth preflight is malformed: {auth_status!r}")
            if auth_status["loggedIn"] is False:
                self.skipTest(
                    "Claude canary requires OAuth/Keychain or a secure apiKeyHelper; "
                    "environment API keys are intentionally absent from isolated roles"
                )
            self.assertEqual(
                auth.returncode,
                0,
                f"{auth.stderr}\nSTDOUT:\n{auth.stdout}",
            )
            if os.environ.get("FLEET_RUN_PAID_CLAUDE_MCP_CANARY") != "1":
                self.skipTest(
                    "paid Claude MCP inference is opt-in; set "
                    "FLEET_RUN_PAID_CLAUDE_MCP_CANARY=1 after approving cost"
                )

            run_id = str(uuid.uuid4())
            token_id = str(uuid.uuid4())
            received: list[dict[str, object]] = []
            server_errors: list[BaseException] = []
            stop_server = threading.Event()

            def serve_claude_canary() -> None:
                control_socket.settimeout(0.2)
                while not stop_server.is_set():
                    try:
                        connection, _ = control_socket.accept()
                    except socket.timeout:
                        continue
                    except OSError as exc:
                        if not stop_server.is_set():
                            server_errors.append(exc)
                        return
                    try:
                        with connection:
                            raw = bytearray()
                            while not raw.endswith(b"\n"):
                                chunk = connection.recv(65_536)
                                if not chunk:
                                    break
                                raw.extend(chunk)
                            envelope = json.loads(raw)
                            received.append(envelope)
                            request = envelope["request"]
                            reply = {
                                "ok": True,
                                "result": {
                                    "identity": {
                                        "kind": "specialist",
                                        "instance": "scout",
                                        "run_id": run_id,
                                    },
                                    "response": {
                                        "jsonrpc": "2.0",
                                        "id": request["id"],
                                        "result": {
                                            "content": [
                                                {
                                                    "type": "text",
                                                    "text": '{"status":"canary"}',
                                                }
                                            ],
                                            "isError": False,
                                        },
                                    },
                                },
                            }
                            connection.sendall(
                                json.dumps(reply, separators=(",", ":")).encode("utf-8")
                                + b"\n"
                            )
                    except BaseException as exc:  # pragma: no cover - surfaced below
                        server_errors.append(exc)
                    return

            server = threading.Thread(target=serve_claude_canary, daemon=True)
            server.start()
            production_command = json.loads(
                (ROOT / "orchestration" / "router.yaml").read_text(encoding="utf-8")
            )["roles"]["claude_reviewer"]["command"]
            self.assertEqual(production_command[0], "claude")
            self.assertIn("--permission-mode", production_command)
            permission_index = production_command.index("--permission-mode")
            self.assertEqual(production_command[permission_index + 1], "plan")
            self.assertNotIn("--allowedTools", production_command)
            self.assertNotIn("--allowed-tools", production_command)
            prompt = (
                "Call mcp__fleet_control__inspect_mission exactly once. "
                f"Use _caller_run_id={run_id} and _caller_token_id={token_id}. "
                "Do not use any other tool."
            )
            try:
                claude = subprocess.run(
                    [
                        "bash",
                        str(RUNNER),
                        "claude_reviewer",
                        "verification",
                        "-",
                        str(shutil.which("claude")),
                        *production_command[1:],
                        "--no-session-persistence",
                        "--max-budget-usd",
                        "0.05",
                        "--output-format",
                        "stream-json",
                        "--verbose",
                        "-p",
                        prompt,
                    ],
                    cwd=ROOT,
                    env=claude_env,
                    text=True,
                    capture_output=True,
                    timeout=90,
                    check=False,
                )
            finally:
                stop_server.set()
                server.join(timeout=2)
            self.assertFalse(server.is_alive(), "Claude MCP canary server did not stop")
            self.assertFalse(server_errors, server_errors)
            self.assertEqual(
                claude.returncode,
                0,
                f"{claude.stderr}\nSTDOUT:\n{claude.stdout}",
            )
            self.assertIn('"type":"tool_use"', claude.stdout)
            self.assertIn("mcp__fleet_control__inspect_mission", claude.stdout)
            self.assertEqual(len(received), 1, claude.stdout)
            envelope = received[0]
            self.assertEqual(envelope["schema_version"], 2)
            self.assertEqual(
                envelope["caller"], {"run_id": run_id, "token_id": token_id}
            )
            request = envelope["request"]
            self.assertEqual(request["method"], "tools/call")
            self.assertEqual(request["params"]["name"], "inspect_mission")
            self.assertEqual(request["params"]["arguments"], {})

    def test_forbidden_required_credentials_fail_closed(self) -> None:
        for name in (
            "SSH_AUTH_SOCK",
            "AWS_ACCESS_KEY_ID",
            "AWS_SESSION_TOKEN",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "OPENAI_API_KEY",
            "GITHUB_TOKEN",
            "GH_TOKEN",
        ):
            with self.subTest(name=name):
                result = self.run_role(
                    "claude",
                    required=name,
                    extra_env={name: "present"},
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("Forbidden credential variable", result.stderr)


if __name__ == "__main__":
    unittest.main()
