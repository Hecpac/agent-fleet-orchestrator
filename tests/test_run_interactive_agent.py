from __future__ import annotations

import os
from pathlib import Path
import json
import socket
import subprocess
import sys
import tempfile
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
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def parse(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def resolved_opencode_policy(self, *, bash_enabled: bool = False) -> dict[str, object]:
        return {
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

    def test_isolates_home_credentials_and_git_identity(self) -> None:
        result = self.run_role(
            "codex",
            authority="write",
            expression="{key: os.environ.get(key) for key in ("
            "'HOME','USER','LOGNAME','FLEET_HUMAN_UID','SSH_AUTH_SOCK',"
            "'AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','GIT_AUTHOR_NAME',"
            "'GIT_AUTHOR_EMAIL','GIT_COMMITTER_NAME','GIT_COMMITTER_EMAIL',"
            "'CMUX_HOOK_DIR','CMUX_EVENTS_LOG')}",
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
        self.assertFalse(isolated_home.exists(), "ephemeral HOME must be removed")

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
                f"Read(/{ROOT}/orchestration/runs/prompts/**)"
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
                        "'auth': json.load(open(p + '/auth.json', encoding='utf-8'))['test'],"
                        "'hooks_present': os.path.exists(p + '/hooks.json'),"
                        "'config': open(p + '/config.toml', encoding='utf-8').read(),"
                        "'cmux_hooks_disabled': os.environ.get('CMUX_CODEX_HOOKS_DISABLED')"
                        "})(os.environ['CODEX_HOME'])",
                    ))
                self.assertEqual(
                    values["codex_home"], values["fleet_home"] + "/.codex"
                )
                self.assertEqual(values["auth"], "authentication-placeholder")
                self.assertFalse(values["hooks_present"])
                self.assertIn("[features]\nhooks = true\n", values["config"])
                self.assertIn(f'[projects."{ROOT}"]', values["config"])
                self.assertIn('trust_level = "untrusted"', values["config"])
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
        self.assertEqual(len(overrides), 3)
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

    def test_mission_specialist_uses_ephemeral_codex_home_with_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller_codex_home = Path(directory) / "controller-codex"
            controller_codex_home.mkdir()
            (controller_codex_home / "auth.json").write_text(
                '{"test":"authentication-placeholder"}\n', encoding="utf-8"
            )
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
                    "'under_fleet_home': p.startswith(os.environ['FLEET_HOME'] + '/'),"
                    "'auth': json.load(open(p + '/auth.json', encoding='utf-8'))['test'],"
                    "'hooks_present': os.path.exists(p + '/hooks.json'),"
                    "'config': open(p + '/config.toml', encoding='utf-8').read(),"
                    "'config_parses': __import__('tomllib').load(open(p + '/config.toml', 'rb'))['features']['hooks']"
                    "})(os.environ['CODEX_HOME'])",
                )
            )
        self.assertTrue(values["under_fleet_home"])
        self.assertEqual(values["auth"], "authentication-placeholder")
        self.assertFalse(values["hooks_present"])
        self.assertTrue(values["config_parses"])
        self.assertIn("hooks = true", values["config"])
        self.assertNotIn("sandbox_mode", values["config"])

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
        self.assertIn("Fleet Codex role requires", result.stderr)

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
                    self.assertIn("exact read-only set", result.stderr)
                    self.assertFalse(marker.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "TUI_OK")
                    self.assertTrue(marker.exists())

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
