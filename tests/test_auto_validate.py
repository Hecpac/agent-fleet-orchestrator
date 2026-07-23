from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import textwrap
import unittest

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "fusion"))

import fleet_json  # noqa: E402

HARNESS = ROOT / "scripts" / "fusion" / "fusion_harness.py"
PROMPTS = ROOT / "scripts" / "fusion" / "prompts"

TOY_GATE = '''\
# /// script
# requires-python = ">=3.10"
# ///
import sys
from pathlib import Path

target = Path(sys.argv[1]) / "hello.txt"
if target.is_file() and "hello" in target.read_text(encoding="utf-8"):
    print("PASS: hello.txt present with greeting")
    raise SystemExit(0)
print(
    f"FAIL: expected hello.txt containing 'hello', found missing, "
    f"at {target} — create it"
)
raise SystemExit(1)
'''

ALWAYS_GREEN_GATE = '''\
# /// script
# requires-python = ">=3.10"
# ///
print("PASS: trivially green")
raise SystemExit(0)
'''


class AutoValidateBase(unittest.TestCase):
    """Shared shim scaffolding for every auto-validate test class."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.out = self.tmp / "fusion-out"
        (self.tmp / "gate-fixture.py").write_text(TOY_GATE, encoding="utf-8")
        (self.tmp / "triage-fixture.txt").write_text(
            "diagnosis text\nTRIAGE_VERDICT: BUILDER_DEFECT — write the file\n",
            encoding="utf-8",
        )
        # claude shim: VALIDATOR writes the gate fixture into its cwd;
        # TRIAGE prints the triage fixture. Distinguished by prompt content.
        self._executable(
            "claude",
            """
            #!/bin/sh
            n=$(cat "$FLEET_TEST_DIR/claude.count" 2>/dev/null || echo 0)
            n=$((n+1)); echo $n > "$FLEET_TEST_DIR/claude.count"
            printf '%s' "$*" > "$FLEET_TEST_DIR/claude.call.$n"
            case "$*" in
              *"# VALIDATOR"*)
                cp "$FLEET_TEST_DIR/gate-fixture.py" gate.py
                [ "${FLEET_TEST_VALIDATOR_EXTRA:-0}" = 1 ] && printf 'x' > extra.txt
                printf 'gate written\\n'
                ;;
              *"# TRIAGE"*)
                cat "$FLEET_TEST_DIR/triage-fixture.txt"
                ;;
              *)
                printf 'unexpected claude call\\n' >&2
                exit 3
                ;;
            esac
            """,
        )
        # codex shim: BUILDER. Succeeds (writes hello.txt into the -C
        # workspace) once the round counter reaches FLEET_TEST_SUCCEED_ON.
        # Emits a session id unless FLEET_TEST_NO_SESSION=1.
        self._executable(
            "codex",
            """
            #!/bin/sh
            n=$(cat "$FLEET_TEST_DIR/codex.count" 2>/dev/null || echo 0)
            n=$((n+1)); echo $n > "$FLEET_TEST_DIR/codex.count"
            printf '%s' "$*" > "$FLEET_TEST_DIR/codex.call.$n"
            ws=""
            prev=""
            for arg in "$@"; do
              [ "$prev" = "-C" ] && ws="$arg"
              prev="$arg"
            done
            if [ -n "$ws" ] && [ "$n" -ge "${FLEET_TEST_SUCCEED_ON:-1}" ]; then
              printf 'hello world\\n' > "$ws/hello.txt"
            fi
            [ "${FLEET_TEST_NO_SESSION:-0}" = 1 ] || \\
              printf 'session id: test-sess-%s\\n' "$n"
            printf 'builder round done\\n'
            """,
        )

    def _executable(self, name: str, source: str) -> None:
        path = self.bin / name
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _env(self, **extra: str) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FLEET_FUSION_OUTPUT_DIR": str(self.out),
            "FLEET_TEST_DIR": str(self.tmp),
            **extra,
        }

    def _run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(HARNESS), "auto-validate", *args],
            env=self._env(**env),
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )

    def _summary(self) -> dict:
        runs = [item for item in self.out.iterdir() if item.is_dir()]
        self.assertEqual(len(runs), 1)
        return fleet_json.load(runs[0] / "autovalidate" / "summary.json")


class AutoValidateTemplateTests(unittest.TestCase):
    def test_templates_declare_their_variables(self) -> None:
        expected = {
            "validator.md": {"{{TASK}}"},
            "builder_round.md": {
                "{{ROUND}}",
                "{{MAX_ROUNDS}}",
                "{{TASK_BLOCK}}",
                "{{GATE_SOURCE}}",
                "{{ROUND_FEEDBACK}}",
            },
            "triage.md": {
                "{{ROUND}}",
                "{{TASK}}",
                "{{GATE_SOURCE}}",
                "{{GATE_OUTPUT}}",
                "{{WORKSPACE}}",
            },
        }
        for name, variables in expected.items():
            text = (PROMPTS / name).read_text(encoding="utf-8")
            for variable in variables:
                self.assertIn(variable, text, f"{name} missing {variable}")

    def test_contract_language_is_present(self) -> None:
        validator = (PROMPTS / "validator.md").read_text(encoding="utf-8")
        self.assertIn("EXACTLY ONE file named gate.py", validator)
        self.assertIn("FAIL on an empty workspace", validator)
        self.assertIn("Do not write any other file", validator)
        self.assertIn('"FAIL: expected <X>, found <Y>, at <path>', validator)
        builder = (PROMPTS / "builder_round.md").read_text(encoding="utf-8")
        self.assertIn("IMMUTABLE", builder)
        triage = (PROMPTS / "triage.md").read_text(encoding="utf-8")
        self.assertIn("TRIAGE_VERDICT: BUILDER_DEFECT", triage)
        self.assertIn("TRIAGE_VERDICT: GATE_DEFECT", triage)


if __name__ == "__main__":
    unittest.main()
