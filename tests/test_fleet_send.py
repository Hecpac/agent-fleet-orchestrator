from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
FLEET_SEND = ROOT / "scripts" / "fleet-send.sh"


class FleetSendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.cmux_log = self.tmp / "cmux.log"
        self.frontier_log = self.tmp / "frontier.log"
        self.confirm_count = self.tmp / "confirm.count"
        self._executable(
            "cmux",
            """
            #!/bin/sh
            printf '%s\n' "$*" >> "$FLEET_TEST_CMUX_LOG"
            exit 0
            """,
        )
        self._executable(
            "python3",
            """
            #!/bin/sh
            script="$1"
            shift
            case "$script" in
              */fleet_identity.py|*/fleet_state.py)
                exit 0
                ;;
              */fleet_frontier.py)
                command="$1"
                printf '%s\n' "$*" >> "$FLEET_TEST_FRONTIER_LOG"
                case "$command" in
                  prepare)
                    printf '%s\n' '{"run_id":"00000000-0000-4000-8000-000000000001","prompt":"logical prompt","submission_payload":"wire payload","prompt_path":"/tmp/prompt","dispatched_at":"2026-07-20T18:30:16.333420+00:00"}'
                    exit 0
                    ;;
                  confirm-submit)
                    printf x >> "$FLEET_TEST_CONFIRM_COUNT"
                    count=$(wc -c < "$FLEET_TEST_CONFIRM_COUNT" | tr -d ' ')
                    if [ "${FLEET_TEST_CONFIRM_FIRST_FAIL:-0}" = 1 ] && [ "$count" = 1 ]; then
                      exit 1
                    fi
                    printf '%s\n' '{}'
                    exit 0
                    ;;
                  mark-indeterminate|abandon)
                    printf '%s\n' '{}'
                    exit 0
                    ;;
                esac
                ;;
            esac
            exit 2
            """,
        )

    def _executable(self, name: str, source: str) -> None:
        path = self.bin / name
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _manifest(self, hook_source: str) -> None:
        (self.runs / "fleet-test.manifest").write_text(
            "\n".join(
                (
                    "workspace=workspace:1",
                    "workspace_uuid=00000000-0000-0000-0000-000000000001",
                    "agent=surface:2",
                    "agent.uuid=00000000-0000-0000-0000-000000000102",
                    "agent.runner=interactive",
                    "agent.role_type=reviewer",
                    "agent.phase=CHALLENGE",
                    "agent.provider=moonshot-ai",
                    "agent.model=kimi-k3",
                    f"agent.hook_source={hook_source}",
                    "agent.variant=",
                )
            )
            + "\n",
            encoding="utf-8",
        )

    def test_kimi_never_represses_enter_while_bridge_confirmation_lags(self) -> None:
        self._manifest("kimi")
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FLEET_RUNS_DIR": str(self.runs),
            "FLEET_SEND_KEY_DELAY": "0",
            "FLEET_KIMI_CONFIRM_SUBMIT_TIMEOUT": "0",
            "FLEET_TEST_CONFIRM_FIRST_FAIL": "1",
            "FLEET_TEST_CMUX_LOG": str(self.cmux_log),
            "FLEET_TEST_FRONTIER_LOG": str(self.frontier_log),
            "FLEET_TEST_CONFIRM_COUNT": str(self.confirm_count),
        }
        result = subprocess.run(
            ["bash", str(FLEET_SEND), "test", "agent", "audit the target", "--json"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        cmux_calls = self.cmux_log.read_text(encoding="utf-8").splitlines()
        enter_calls = [call for call in cmux_calls if call.startswith("send-key ")]
        self.assertEqual(len(enter_calls), 1, cmux_calls)
        frontier_calls = self.frontier_log.read_text(encoding="utf-8")
        self.assertEqual(frontier_calls.count("confirm-submit "), 2)
        self.assertIn(
            "--since 2026-07-20T18:30:16.333420+00:00", frontier_calls
        )


if __name__ == "__main__":
    unittest.main()
