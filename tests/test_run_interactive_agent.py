from __future__ import annotations

import os
from pathlib import Path
import json
import socket
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run-interactive-agent.sh"
HOOK_BRIDGE = ROOT / "scripts" / "cmux-codex-hook.sh"


class InteractiveAgentEnvironmentTests(unittest.TestCase):
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
        env["CODEX_HOME"] = "/tmp/controller-codex-home"
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
            "'auth_state_copied': os.path.isfile(fleet_home + '/.claude.json'),"
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
        self.assertFalse(values["auth_state_copied"])
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

    def test_codex_roles_keep_explicit_codex_home(self) -> None:
        for role in ("codex", "codex_candidate"):
            with self.subTest(role=role):
                result = self.run_role(
                    role,
                    expression="{'CODEX_HOME': os.environ.get('CODEX_HOME')}",
                )
                self.assertEqual(
                    self.parse(result)["CODEX_HOME"],
                    "/tmp/controller-codex-home",
                )

    def test_mission_specialist_uses_ephemeral_codex_home_with_auth_and_cmux_hooks(self) -> None:
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
                    "'hooks': json.load(open(p + '/hooks.json', encoding='utf-8')),"
                    "'config': open(p + '/config.toml', encoding='utf-8').read(),"
                    "'config_parses': __import__('tomllib').load(open(p + '/config.toml', 'rb'))['features']['hooks']"
                    "})(os.environ['CODEX_HOME'])",
                )
            )
        self.assertTrue(values["under_fleet_home"])
        self.assertEqual(values["auth"], "authentication-placeholder")
        self.assertTrue(values["hooks_present"])
        self.assertTrue(values["config_parses"])
        hook_commands = [
            hook["command"]
            for groups in values["hooks"]["hooks"].values()
            for group in groups
            for hook in group["hooks"]
        ]
        self.assertTrue(hook_commands)
        self.assertEqual(len(hook_commands), 3)
        self.assertTrue(
            all("scripts/cmux-codex-hook.sh" in command for command in hook_commands)
        )
        self.assertFalse(any("unrelated.py" in command for command in hook_commands))
        self.assertEqual(set(values["hooks"]["hooks"]), {"SessionStart", "UserPromptSubmit", "Stop"})
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
        self.assertIn("Mission-bound Codex specialist requires", result.stderr)

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
            unrelated = home / ".config" / "other-app"
            unrelated.mkdir()
            (unrelated / "secret").write_text("must-not-copy", encoding="utf-8")
            values = self.parse(self.run_role(
                "glm",
                extra_env={"HOME": str(home)},
                expression="{key: {'root': os.environ.get(key), "
                "'opencode': os.path.isfile(os.environ[key] + '/opencode/provider.json'), "
                "'other': os.path.exists(os.environ[key] + '/other-app')} "
                "for key in ('XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_STATE_HOME')}",
            ))
        for value in values.values():
            self.assertIn("/tmp/fleet_home.", value["root"])
            self.assertTrue(value["opencode"])
            self.assertFalse(value["other"])

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
