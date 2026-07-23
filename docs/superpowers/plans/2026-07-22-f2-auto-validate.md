# F2 Auto-Validate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `just auto-validate "<task>"` — validator-authored sealed gate, RED baseline, ≤5 builder rounds with resume+fallback memory, read-only triage with one free gate repair, isolated workspace, exactly-once durable summary/ledger.

**Architecture:** New module `scripts/fusion/auto_validate.py` (state machine + legs) importing shared machinery from `fusion_harness` (`run_agent` — gains an optional `cwd` param, `output_root`, `_agent_summary`, `TIERS`, `FusionError`). Three new prompt templates. `fusion_harness.py` only gains the delegating subcommand. Tests in a new `tests/test_auto_validate.py` using PATH-shim CLIs and toy PEP 723 gates run by the REAL `uv`.

**Tech Stack:** Python 3 stdlib, `uv` (present on the machine), unittest + shim pattern from `tests/test_fusion_harness.py`.

**Spec (read first):** `docs/superpowers/specs/2026-07-22-f2-auto-validate-design.md`. Template texts in Task 1 are copied VERBATIM from spec §6.

---

### Task 1: Templates + test infrastructure + template integrity tests

**Files:**
- Create: `scripts/fusion/prompts/validator.md`, `scripts/fusion/prompts/builder_round.md`, `scripts/fusion/prompts/triage.md`
- Create: `tests/test_auto_validate.py`

- [ ] **Step 1: Create the three templates** — copy each fenced ```text block from spec §6 verbatim (fences excluded): `validator.md` (starts `# VALIDATOR`), `builder_round.md` (starts `# BUILDER — round {{ROUND}} of {{MAX_ROUNDS}}`), `triage.md` (starts `# TRIAGE — read-only diagnosis, round {{ROUND}}`). Extract with `sed` from the spec and `diff` against the written files to guarantee fidelity.

- [ ] **Step 2: Create `tests/test_auto_validate.py` with shared fixtures + template tests**

```python
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
            printf '%s' "$2" > "$FLEET_TEST_DIR/claude.call.$n"
            case "$2" in
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
        builder = (PROMPTS / "builder_round.md").read_text(encoding="utf-8")
        self.assertIn("IMMUTABLE", builder)
        triage = (PROMPTS / "triage.md").read_text(encoding="utf-8")
        self.assertIn("TRIAGE_VERDICT: BUILDER_DEFECT", triage)
        self.assertIn("TRIAGE_VERDICT: GATE_DEFECT", triage)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run** `python3 -m unittest tests.test_auto_validate -v` → the 2 template tests PASS (they only read the template files; base class has no tests yet). If they fail, fix the template transcription, never the assertions.

- [ ] **Step 4: Commit**

```bash
git add scripts/fusion/prompts/validator.md scripts/fusion/prompts/builder_round.md scripts/fusion/prompts/triage.md tests/test_auto_validate.py
git commit -m "feat(fusion): add F2 auto-validate prompt templates and test scaffolding

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `run_agent` cwd override + gate primitives (render, seal, run, tamper)

**Files:**
- Modify: `scripts/fusion/fusion_harness.py` (`run_agent` signature)
- Create: `scripts/fusion/auto_validate.py`
- Test: `tests/test_auto_validate.py` (append), `tests/test_fusion_harness.py` (no changes — full module must stay green)

- [ ] **Step 1: Append the failing unit tests**

```python
class GatePrimitiveTests(AutoValidateBase):
    def setUp(self) -> None:
        super().setUp()
        import auto_validate

        self.av = auto_validate
        self.av_dir = self.tmp / "avdir"
        self.av_dir.mkdir()
        self.workspace = self.av_dir / "workspace"
        self.workspace.mkdir()

    def test_render_av_template_is_single_pass_and_checks_unknowns(self) -> None:
        rendered = self.av.render_av_template(
            "triage.md",
            {
                "{{ROUND}}": "3",
                "{{TASK}}": "make {{GATE_SOURCE}} appear literally",
                "{{GATE_SOURCE}}": "print('gate')",
                "{{GATE_OUTPUT}}": "FAIL: x",
                "{{WORKSPACE}}": "/tmp/ws",
            },
        )
        self.assertIn("make {{GATE_SOURCE}} appear literally", rendered)
        self.assertEqual(rendered.count("print('gate')"), 1)

    def test_seal_and_run_gate_red_and_green(self) -> None:
        (self.av_dir / "gate.py").write_text(TOY_GATE, encoding="utf-8")
        digest = self.av.seal_gate(self.av_dir)
        self.assertEqual(len(digest), 64)
        result = self.av.run_gate(self.av_dir, self.workspace, timeout_seconds=60)
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(len(result["fail_lines"]), 1)
        self.assertFalse(result["tampered"])
        (self.workspace / "hello.txt").write_text("hello", encoding="utf-8")
        result = self.av.run_gate(self.av_dir, self.workspace, timeout_seconds=60)
        self.assertEqual(result["verdict"], "pass")

    def test_run_gate_restores_sealed_copy_and_records_tampering(self) -> None:
        (self.av_dir / "gate.py").write_text(TOY_GATE, encoding="utf-8")
        self.av.seal_gate(self.av_dir)
        (self.av_dir / "gate.py").write_text(ALWAYS_GREEN_GATE, encoding="utf-8")
        result = self.av.run_gate(self.av_dir, self.workspace, timeout_seconds=60)
        self.assertTrue(result["tampered"])
        self.assertEqual(result["verdict"], "fail")  # sealed toy gate ran, not the green one
        self.assertEqual(
            (self.av_dir / "gate.py").read_text(encoding="utf-8"), TOY_GATE
        )

    def test_run_gate_harness_errors_are_typed(self) -> None:
        (self.av_dir / "gate.py").write_text(TOY_GATE, encoding="utf-8")
        self.av.seal_gate(self.av_dir)
        slow = self.av_dir / "gate.py"
        slow.write_text(
            TOY_GATE.replace(
                "import sys", "import sys, time\ntime.sleep(30)"
            ),
            encoding="utf-8",
        )
        self.av.seal_gate(self.av_dir)  # reseal the slow gate deliberately
        result = self.av.run_gate(self.av_dir, self.workspace, timeout_seconds=1)
        self.assertEqual(result["verdict"], "harness-error")
        self.assertIn("timeout", result["harness_error"])

    def test_run_agent_accepts_cwd_override(self) -> None:
        import fusion_harness

        self._executable(
            "claude",
            """
            #!/bin/sh
            pwd > "$FLEET_TEST_DIR/agent.cwd"
            printf 'ok\\n'
            """,
        )
        env_patch = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FLEET_TEST_DIR": str(self.tmp)}
        old = {k: os.environ.get(k) for k in env_patch}
        os.environ.update(env_patch)
        try:
            fusion_harness.run_agent("probe", ["claude", "-p", "x"], 30, cwd=self.av_dir)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(
            (self.tmp / "agent.cwd").read_text(encoding="utf-8").strip(),
            str(self.av_dir),
        )
```

- [ ] **Step 2: Run** `python3 -m unittest tests.test_auto_validate.GatePrimitiveTests -v` → ERROR (no module auto_validate; run_agent lacks cwd).

- [ ] **Step 3: Implement.** In `fusion_harness.py`, change `run_agent` signature to `def run_agent(role, argv, timeout_seconds, cwd: Path | None = None)` and pass `cwd=cwd or REPO_ROOT` to Popen (one-line change each). Create `scripts/fusion/auto_validate.py`:

```python
#!/usr/bin/env python3
"""F2 auto-validate: gate-first build loop over the fusion harness.

The gate is the contract: a VALIDATOR writes it before any work exists, the
baseline must fail RED, a sandboxed BUILDER iterates against the gate's FAIL
lines, TRIAGE diagnoses from round 3 and may spend the single free gate
repair. Harness errors are never charged as rounds. This file stays under
800 lines.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import re
import subprocess
from typing import Any

from fusion_harness import (  # noqa: F401  (re-exported for the harness)
    FusionError,
    TIERS,
    _agent_summary,
    output_root,
    run_agent,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "prompts"
MAX_ROUNDS = 5
GATE_NAME = "gate.py"
SEALED_NAME = "gate.py.sealed"
SESSION_ID_RE = re.compile(r"session[ _-]?id[:=]\s*([A-Za-z0-9._-]+)", re.IGNORECASE)

VALIDATOR_ARGV = [
    "claude", "-p", "{prompt}", "--model", "{model}",
    "--permission-mode", "acceptEdits",
]
BUILDER_FIRST_ARGV = [
    "codex", "exec", "-s", "workspace-write", "-C", "{workspace}",
    "--model", "{model}", "{prompt}",
]
BUILDER_RESUME_ARGV = [
    "codex", "exec", "resume", "{session_id}", "-s", "workspace-write",
    "-C", "{workspace}", "--model", "{model}", "{prompt}",
]
TRIAGE_ARGV = ["claude", "-p", "{prompt}", "--model", "{model}"]


def render_av_template(name: str, mapping: dict[str, str]) -> str:
    template = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    unknown = set(re.findall(r"\{\{[A-Z_]+\}\}", template)) - set(mapping)
    if unknown:
        raise FusionError(f"{name} has unresolved variables: {sorted(unknown)}")
    pattern = re.compile("|".join(re.escape(key) for key in mapping))
    # Single pass: substituted content is never rescanned (same G1 rationale
    # as render_fusion_prompt).
    return pattern.sub(lambda match: mapping[match.group(0)], template)


def _fill(argv: list[str], **values: str) -> list[str]:
    filled = []
    for part in argv:
        for key, value in values.items():
            part = part.replace("{" + key + "}", value)
        filled.append(part)
    return filled


def seal_gate(av_dir: Path) -> str:
    gate = av_dir / GATE_NAME
    data = gate.read_bytes()
    (av_dir / SEALED_NAME).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def run_gate(
    av_dir: Path, workspace: Path, *, timeout_seconds: float
) -> dict[str, Any]:
    """Restore the sealed gate, record tampering, run it via uv."""
    gate = av_dir / GATE_NAME
    sealed = (av_dir / SEALED_NAME).read_bytes()
    tampered = gate.read_bytes() != sealed
    if tampered:
        gate.write_bytes(sealed)
    result: dict[str, Any] = {
        "verdict": "harness-error",
        "output": "",
        "fail_lines": [],
        "tampered": tampered,
        "harness_error": "",
    }
    try:
        completed = subprocess.run(
            ["uv", "run", str(gate), str(workspace)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            cwd=av_dir,
        )
    except FileNotFoundError:
        result["harness_error"] = "uv is not available on PATH"
        return result
    except subprocess.TimeoutExpired:
        result["harness_error"] = f"gate timeout after {timeout_seconds}s"
        return result
    output = (completed.stdout or "") + (completed.stderr or "")
    result["output"] = output
    result["fail_lines"] = [
        line for line in output.splitlines() if line.startswith("FAIL:")
    ]
    result["verdict"] = "pass" if completed.returncode == 0 else "fail"
    return result


def extract_session_id(text: str) -> str:
    match = SESSION_ID_RE.search(text)
    return match.group(1) if match else ""
```

- [ ] **Step 4: Run** `python3 -m unittest tests.test_auto_validate -v` (7 tests PASS) **and** `python3 -m unittest tests.test_fusion_harness -v` (23/23 — the cwd default must not regress anything).

- [ ] **Step 5: Commit**

```bash
git add scripts/fusion/fusion_harness.py scripts/fusion/auto_validate.py tests/test_auto_validate.py
git commit -m "feat(fusion): gate primitives — seal, tamper-restore, typed harness errors

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Agent legs — validator (exactly-one-file), builder (resume/fallback), triage (verdict parse)

**Files:**
- Modify: `scripts/fusion/auto_validate.py`
- Test: `tests/test_auto_validate.py` (append)

- [ ] **Step 1: Append the failing tests**

```python
class AgentLegTests(AutoValidateBase):
    def setUp(self) -> None:
        super().setUp()
        import auto_validate

        self.av = auto_validate
        self.av_dir = self.tmp / "avdir"
        self.av_dir.mkdir()
        self.workspace = self.av_dir / "workspace"
        self.workspace.mkdir()
        self._patch_env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FLEET_TEST_DIR": str(self.tmp),
        }

    def _with_env(self, fn, *args, **kwargs):
        old = {k: os.environ.get(k) for k in self._patch_env}
        os.environ.update(self._patch_env)
        try:
            return fn(*args, **kwargs)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_validator_leg_accepts_exactly_gate_py(self) -> None:
        outcome = self._with_env(
            self.av.validator_leg, "workhorse", "make hello.txt", self.av_dir, 60
        )
        self.assertEqual(outcome["status"], "ok")
        self.assertTrue((self.av_dir / "gate.py").is_file())

    def test_validator_leg_rejects_extra_files(self) -> None:
        os.environ["FLEET_TEST_VALIDATOR_EXTRA"] = "1"
        self.addCleanup(os.environ.pop, "FLEET_TEST_VALIDATOR_EXTRA", None)
        with self.assertRaisesRegex(self.av.FusionError, "exactly gate.py"):
            self._with_env(
                self.av.validator_leg, "workhorse", "make hello.txt", self.av_dir, 60
            )

    def test_builder_leg_first_round_then_resume_then_fallback(self) -> None:
        outcome, session, memory = self._with_env(
            self.av.builder_leg,
            "workhorse", 1, "make hello.txt", "gate src", "", self.workspace, "", 60,
        )
        self.assertEqual(memory, "first")
        self.assertEqual(session, "test-sess-1")
        outcome, session, memory = self._with_env(
            self.av.builder_leg,
            "workhorse", 2, "make hello.txt", "gate src", "FAIL: x", self.workspace, session, 60,
        )
        self.assertEqual(memory, "resume")
        argv2 = (self.tmp / "codex.call.2").read_text(encoding="utf-8")
        self.assertIn("resume test-sess-1", argv2)
        os.environ["FLEET_TEST_NO_SESSION"] = "1"
        self.addCleanup(os.environ.pop, "FLEET_TEST_NO_SESSION", None)
        outcome, session, memory = self._with_env(
            self.av.builder_leg,
            "workhorse", 3, "make hello.txt", "gate src", "FAIL: x", self.workspace, "", 60,
        )
        self.assertEqual(memory, "stateless-fallback")
        self.assertEqual(session, "")
        argv3 = (self.tmp / "codex.call.3").read_text(encoding="utf-8")
        self.assertIn("make hello.txt", argv3)

    def test_triage_leg_parses_verdicts(self) -> None:
        outcome, verdict, guidance = self._with_env(
            self.av.triage_leg,
            "workhorse", 3, "task", "gate src", "FAIL: x", self.workspace, 60,
        )
        self.assertEqual(verdict, "BUILDER_DEFECT")
        self.assertIn("write the file", guidance)
        (self.tmp / "triage-fixture.txt").write_text(
            "bad gate\nTRIAGE_VERDICT: GATE_DEFECT — checks wrong path\n",
            encoding="utf-8",
        )
        outcome, verdict, guidance = self._with_env(
            self.av.triage_leg,
            "workhorse", 4, "task", "gate src", "FAIL: x", self.workspace, 60,
        )
        self.assertEqual(verdict, "GATE_DEFECT")

    def test_triage_leg_malformed_verdict_is_none(self) -> None:
        (self.tmp / "triage-fixture.txt").write_text(
            "rambling with no verdict line\n", encoding="utf-8"
        )
        outcome, verdict, guidance = self._with_env(
            self.av.triage_leg,
            "workhorse", 3, "task", "gate src", "FAIL: x", self.workspace, 60,
        )
        self.assertIsNone(verdict)
```

- [ ] **Step 2: Run** → ERROR (missing functions).

- [ ] **Step 3: Implement in `auto_validate.py`**

```python
def validator_leg(
    tier: str, task: str, av_dir: Path, timeout_seconds: float
) -> dict[str, Any]:
    spec = TIERS[tier]["architect"]
    prompt = render_av_template("validator.md", {"{{TASK}}": task})
    before = {entry.name for entry in av_dir.iterdir()}
    argv = _fill(VALIDATOR_ARGV, prompt=prompt, model=spec["model"])
    outcome = run_agent("validator", argv, timeout_seconds, cwd=av_dir)
    created = {entry.name for entry in av_dir.iterdir()} - before
    if outcome["status"] != "ok":
        raise FusionError(f"validator failed: {outcome['stderr_tail'][-200:]}")
    if created != {GATE_NAME}:
        raise FusionError(
            f"validator must create exactly gate.py; created: {sorted(created)}"
        )
    return outcome


def builder_leg(
    tier: str,
    round_no: int,
    task: str,
    gate_source: str,
    feedback: str,
    workspace: Path,
    session_id: str,
    timeout_seconds: float,
) -> tuple[dict[str, Any], str, str]:
    spec = TIERS[tier]["builder"]
    if round_no == 1:
        memory = "first"
        task_block = f"Task:\n\n{task}"
    elif session_id:
        memory = "resume"
        task_block = "Continue the same task from your previous rounds."
    else:
        memory = "stateless-fallback"
        task_block = f"Task:\n\n{task}"
    prompt = render_av_template(
        "builder_round.md",
        {
            "{{ROUND}}": str(round_no),
            "{{MAX_ROUNDS}}": str(MAX_ROUNDS),
            "{{TASK_BLOCK}}": task_block,
            "{{GATE_SOURCE}}": gate_source,
            "{{ROUND_FEEDBACK}}": feedback,
        },
    )
    if memory == "resume":
        argv = _fill(
            BUILDER_RESUME_ARGV,
            session_id=session_id,
            workspace=str(workspace),
            model=spec["model"],
            prompt=prompt,
        )
    else:
        argv = _fill(
            BUILDER_FIRST_ARGV,
            workspace=str(workspace),
            model=spec["model"],
            prompt=prompt,
        )
    outcome = run_agent("builder", argv, timeout_seconds)
    new_session = extract_session_id(outcome["output"]) or extract_session_id(
        outcome["stderr_tail"]
    )
    return outcome, new_session, memory


TRIAGE_VERDICT_RE = re.compile(
    r"^TRIAGE_VERDICT:\s*(BUILDER_DEFECT|GATE_DEFECT)\s*[—-]?\s*(.*)$",
    re.MULTILINE,
)


def triage_leg(
    tier: str,
    round_no: int,
    task: str,
    gate_source: str,
    gate_output: str,
    workspace: Path,
    timeout_seconds: float,
) -> tuple[dict[str, Any], str | None, str]:
    spec = TIERS[tier]["architect"]
    prompt = render_av_template(
        "triage.md",
        {
            "{{ROUND}}": str(round_no),
            "{{TASK}}": task,
            "{{GATE_SOURCE}}": gate_source,
            "{{GATE_OUTPUT}}": gate_output,
            "{{WORKSPACE}}": str(workspace),
        },
    )
    argv = _fill(TRIAGE_ARGV, prompt=prompt, model=spec["model"])
    outcome = run_agent("triage", argv, timeout_seconds)
    match = TRIAGE_VERDICT_RE.search(outcome["output"])
    if not match:
        return outcome, None, ""
    return outcome, match.group(1), match.group(2).strip()
```

- [ ] **Step 4: Run** the full new module (12 tests) + `tests.test_fusion_harness` (23) → all green.

- [ ] **Step 5: Commit**

```bash
git add scripts/fusion/auto_validate.py tests/test_auto_validate.py
git commit -m "feat(fusion): validator, builder and triage legs with memory fallback

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: The state machine + subcommand wiring + exactly-once summary/ledger

**Files:**
- Modify: `scripts/fusion/auto_validate.py` (add `auto_validate()`)
- Modify: `scripts/fusion/fusion_harness.py` (subparser + dispatch)
- Test: `tests/test_auto_validate.py` (append)

- [ ] **Step 1: Append the failing end-to-end tests** (spec §7 cases 1-4, 6-10)

```python
class AutoValidateCommandTests(AutoValidateBase):
    def test_green_round_one(self) -> None:
        result = self._run("make hello.txt", FLEET_TEST_SUCCEED_ON="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self._summary()
        self.assertEqual(summary["command"], "auto-validate")
        self.assertEqual(summary["status"], "green")
        self.assertEqual(len(summary["rounds"]), 1)
        self.assertEqual(summary["rounds"][0]["gate_verdict"], "pass")
        self.assertEqual(summary["gate_repairs"], 0)

    def test_green_round_three_feeds_fail_lines_forward(self) -> None:
        result = self._run("make hello.txt", FLEET_TEST_SUCCEED_ON="3")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self._summary()
        self.assertEqual(len(summary["rounds"]), 3)
        self.assertEqual(
            [item["gate_verdict"] for item in summary["rounds"]],
            ["fail", "fail", "pass"],
        )
        argv2 = (self.tmp / "codex.call.2").read_text(encoding="utf-8")
        self.assertIn("FAIL: expected hello.txt", argv2)

    def test_baseline_not_red_exits_6(self) -> None:
        (self.tmp / "gate-fixture.py").write_text(
            ALWAYS_GREEN_GATE, encoding="utf-8"
        )
        result = self._run("make hello.txt")

        self.assertEqual(result.returncode, 6, result.stderr)
        summary = self._summary()
        self.assertEqual(summary["status"], "baseline-not-red")
        self.assertEqual(summary["rounds"], [])
        self.assertFalse((self.tmp / "codex.count").exists(), "no builder round")

    def test_halt_after_five_rounds_exits_7(self) -> None:
        result = self._run("make hello.txt", FLEET_TEST_SUCCEED_ON="99")

        self.assertEqual(result.returncode, 7, result.stderr)
        summary = self._summary()
        self.assertEqual(summary["status"], "halted")
        self.assertEqual(len(summary["rounds"]), 5)
        ledger = (self.out / "ledger.jsonl").read_bytes()
        self.assertEqual(
            fleet_json.loads(ledger.splitlines()[0])["run_id"], summary["run_id"]
        )

    def test_validator_extra_file_exits_5(self) -> None:
        result = self._run(
            "make hello.txt", FLEET_TEST_VALIDATOR_EXTRA="1"
        )

        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertEqual(self._summary()["status"], "invalid-validator")

    def test_gate_tampering_is_recorded_and_sealed_copy_runs(self) -> None:
        self._executable(
            "codex",
            """
            #!/bin/sh
            n=$(cat "$FLEET_TEST_DIR/codex.count" 2>/dev/null || echo 0)
            n=$((n+1)); echo $n > "$FLEET_TEST_DIR/codex.count"
            printf '%s' "$*" > "$FLEET_TEST_DIR/codex.call.$n"
            ws=""; prev=""
            for arg in "$@"; do [ "$prev" = "-C" ] && ws="$arg"; prev="$arg"; done
            printf 'print("PASS: forged")\\nraise SystemExit(0)\\n' > "$ws/../gate.py"
            printf 'session id: s-%s\\n' "$n"
            """,
        )
        result = self._run("make hello.txt")

        self.assertEqual(result.returncode, 7, result.stderr)
        summary = self._summary()
        self.assertTrue(all(item["gate_tampered"] for item in summary["rounds"]))
        self.assertTrue(
            all(item["gate_verdict"] == "fail" for item in summary["rounds"])
        )

    def test_harness_error_round_is_not_charged(self) -> None:
        # Break uv for the FIRST gate run after baseline by shadowing it with
        # a one-shot failing shim, then restore. Simplest deterministic proxy:
        # break uv entirely -> baseline itself is a harness error, retried
        # once, then exit 2 with status harness-error and zero rounds charged.
        self._executable("uv", "#!/bin/sh\nexit 127\n")
        result = self._run("make hello.txt")

        self.assertEqual(result.returncode, 2, result.stderr)
        summary = self._summary()
        self.assertEqual(summary["status"], "harness-error")
        self.assertEqual(summary["rounds"], [])

    def test_memory_fallback_is_recorded(self) -> None:
        result = self._run(
            "make hello.txt",
            FLEET_TEST_SUCCEED_ON="2",
            FLEET_TEST_NO_SESSION="1",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self._summary()
        self.assertEqual(
            [item["memory"] for item in summary["rounds"]],
            ["first", "stateless-fallback"],
        )
```

- [ ] **Step 2: Run** → ERROR (argparse rejects auto-validate).

- [ ] **Step 3: Implement `auto_validate()`** in `auto_validate.py`:

```python
import json
import uuid

import fleet_json


def auto_validate(args: Any) -> int:
    task = args.task.strip()
    if not task:
        raise FusionError("auto-validate requires a non-empty task")
    tier = args.tier or os.environ.get("FLEET_FUSION_TIER", "workhorse")
    if tier not in TIERS:
        raise FusionError(f"unknown tier: {tier}; available: {sorted(TIERS)}")
    agent_timeout = float(os.environ.get("FLEET_FUSION_TIMEOUT", "300"))
    gate_timeout = float(os.environ.get("FLEET_GATE_TIMEOUT", "60"))

    run_id = str(uuid.uuid4())[:8]
    av_dir = output_root() / run_id / "autovalidate"
    workspace = av_dir / "workspace"
    rounds_dir = av_dir / "rounds"
    workspace.mkdir(parents=True, exist_ok=False)
    rounds_dir.mkdir()

    rounds: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    gate_repairs = 0
    gate_sha = ""

    def finish(status: str, exit_code: int) -> int:
        summary = {
            "schema_version": 1,
            "command": "auto-validate",
            "run_id": run_id,
            "tier": tier,
            "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
            "gate_sha256": gate_sha,
            "template_hashes": {
                name.split(".")[0]: hashlib.sha256(
                    (TEMPLATES_DIR / name).read_bytes()
                ).hexdigest()
                for name in ("validator.md", "builder_round.md", "triage.md")
            },
            "status": status,
            "gate_repairs": gate_repairs,
            "rounds": rounds,
            "agents": agents,
        }
        (av_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with (output_root() / "ledger.jsonl").open("ab") as ledger:
            ledger.write(fleet_json.canonical_bytes(summary) + b"\n")
        print(f"auto-validate run {run_id} (tier={tier}) status={status}")
        for item in rounds:
            print(
                f"  round {item['n']}: {item['gate_verdict']:<7} "
                f"memory={item['memory']} triage={item['triage_verdict']}"
            )
        print(f"  summary: {av_dir / 'summary.json'}")
        return exit_code

    # 1. VALIDATOR writes the gate.
    try:
        outcome = validator_leg(tier, task, av_dir, agent_timeout)
    except FusionError as error:
        print(f"auto-validate: {error}", file=sys.stderr)
        return finish("invalid-validator", 5)
    agents.append(
        _agent_summary(
            {"role": "validator", "cli": "claude",
             "model": TIERS[tier]["architect"]["model"]},
            outcome, av_dir / GATE_NAME,
        )
    )
    gate_sha = seal_gate(av_dir)
    gate_source = (av_dir / GATE_NAME).read_text(encoding="utf-8")

    # 2. Baseline must be RED. A harness error is retried once, never charged.
    baseline = run_gate(av_dir, workspace, timeout_seconds=gate_timeout)
    if baseline["verdict"] == "harness-error":
        baseline = run_gate(av_dir, workspace, timeout_seconds=gate_timeout)
        if baseline["verdict"] == "harness-error":
            print(
                f"auto-validate: {baseline['harness_error']}",
                file=sys.stderr,
            )
            return finish("harness-error", 2)
    (av_dir / "baseline.txt").write_text(baseline["output"], encoding="utf-8")
    if baseline["verdict"] == "pass":
        print(
            "auto-validate: baseline is GREEN — weak gate or work already "
            "done; refusing to run the builder.",
            file=sys.stderr,
        )
        return finish("baseline-not-red", 6)

    # 3. Builder rounds.
    session_id = ""
    feedback = ""
    triage_verdict: str | None = None
    triage_guidance = ""
    round_no = 1
    harness_strikes = 0
    while round_no <= MAX_ROUNDS:
        outcome, session_id, memory = builder_leg(
            tier, round_no, task, gate_source, feedback,
            workspace, session_id, agent_timeout,
        )
        (rounds_dir / f"round-{round_no}.md").write_text(
            outcome["output"], encoding="utf-8"
        )
        agents.append(
            _agent_summary(
                {"role": f"builder-r{round_no}", "cli": "codex",
                 "model": TIERS[tier]["builder"]["model"]},
                outcome, rounds_dir / f"round-{round_no}.md",
            )
        )
        gate_result = run_gate(av_dir, workspace, timeout_seconds=gate_timeout)
        if gate_result["verdict"] == "harness-error":
            harness_strikes += 1
            rounds.append(
                {
                    "n": round_no, "memory": memory, "gate_verdict": "harness-error",
                    "fail_lines": [], "triage_verdict": None,
                    "gate_tampered": gate_result["tampered"],
                    "harness_errors": [gate_result["harness_error"]],
                }
            )
            if harness_strikes >= 2:
                return finish("harness-error", 2)
            continue  # repeat the same round number: never charged
        harness_strikes = 0
        record = {
            "n": round_no, "memory": memory,
            "gate_verdict": gate_result["verdict"],
            "fail_lines": gate_result["fail_lines"],
            "triage_verdict": None,
            "gate_tampered": gate_result["tampered"],
            "harness_errors": [],
        }
        if gate_result["verdict"] == "pass":
            rounds.append(record)
            return finish("green", 0)
        # FAIL: triage from round 3.
        triage_verdict = None
        triage_guidance = ""
        if round_no >= 3:
            t_outcome, triage_verdict, triage_guidance = triage_leg(
                tier, round_no, task, gate_source,
                gate_result["output"], workspace, agent_timeout,
            )
            (av_dir / f"triage-{round_no}.md").write_text(
                t_outcome["output"], encoding="utf-8"
            )
            record["triage_verdict"] = triage_verdict
            if triage_verdict == "GATE_DEFECT" and gate_repairs == 0:
                gate_repairs = 1
                (av_dir / GATE_NAME).rename(av_dir / f"gate.py.r{round_no}")
                repair_task = (
                    f"{task}\n\nThe previous gate was defective: "
                    f"{triage_guidance}. Write a corrected gate."
                )
                try:
                    r_outcome = validator_leg(
                        tier, repair_task, av_dir, agent_timeout
                    )
                except FusionError as error:
                    print(f"auto-validate: {error}", file=sys.stderr)
                    rounds.append(record)
                    return finish("invalid-validator", 5)
                agents.append(
                    _agent_summary(
                        {"role": f"validator-repair-r{round_no}",
                         "cli": "claude",
                         "model": TIERS[tier]["architect"]["model"]},
                        r_outcome, av_dir / GATE_NAME,
                    )
                )
                gate_sha = seal_gate(av_dir)
                gate_source = (av_dir / GATE_NAME).read_text(encoding="utf-8")
                # Free re-run: the builder does NOT run again first.
                gate_result = run_gate(
                    av_dir, workspace, timeout_seconds=gate_timeout
                )
                record["gate_verdict"] = gate_result["verdict"]
                record["fail_lines"] = gate_result["fail_lines"]
                if gate_result["verdict"] == "pass":
                    rounds.append(record)
                    return finish("green", 0)
        rounds.append(record)
        feedback = "Gate output:\n\n" + gate_result["output"]
        if triage_guidance:
            feedback += f"\n\nTriage diagnosis: {triage_guidance}"
        round_no += 1
    return finish("halted", 7)
```

Wire in `fusion_harness.py` `_parser()`:

```python
    autovalidate_parser = sub.add_parser(
        "auto-validate", help="gate-first build loop: validator, builder, triage"
    )
    autovalidate_parser.add_argument("task")
    autovalidate_parser.add_argument("--tier", choices=sorted(TIERS))
```

and in `main()`:

```python
        if args.command == "auto-validate":
            import auto_validate as auto_validate_module

            return auto_validate_module.auto_validate(args)
```

(`import` inside the branch keeps `opinion`/`fusion` startup independent of the new module. `sys.stderr` in the module is `sys.stderr` — import `sys` at module top and use `sys.stderr` directly instead; do NOT ship `os.sys`.)

- [ ] **Step 4: Run** the full file `python3 -m unittest tests.test_auto_validate -v` (20 tests) and `tests.test_fusion_harness` (23). All green.

- [ ] **Step 5: Commit**

```bash
git add scripts/fusion/auto_validate.py scripts/fusion/fusion_harness.py tests/test_auto_validate.py
git commit -m "feat(fusion): auto-validate state machine with exactly-once summary

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Gate-repair path test (spec §7.5) + justfile target + line budgets

**Files:**
- Modify: `tests/test_auto_validate.py`, `justfile`

- [ ] **Step 1: Append the repair test** (uses a defective gate fixture that checks the WRONG filename, and a triage fixture declaring GATE_DEFECT; on repair the validator shim writes the CORRECT toy gate because the repair prompt contains "corrected gate" — extend the claude shim in this test only)

```python
class GateRepairTests(AutoValidateBase):
    def test_gate_defect_single_free_repair(self) -> None:
        wrong_gate = TOY_GATE.replace("hello.txt", "wrong.txt")
        (self.tmp / "gate-fixture.py").write_text(wrong_gate, encoding="utf-8")
        (self.tmp / "gate-fixture-fixed.py").write_text(TOY_GATE, encoding="utf-8")
        (self.tmp / "triage-fixture.txt").write_text(
            "gate checks wrong.txt but the task says hello.txt\n"
            "TRIAGE_VERDICT: GATE_DEFECT — gate checks wrong.txt, task asks hello.txt\n",
            encoding="utf-8",
        )
        self._executable(
            "claude",
            """
            #!/bin/sh
            n=$(cat "$FLEET_TEST_DIR/claude.count" 2>/dev/null || echo 0)
            n=$((n+1)); echo $n > "$FLEET_TEST_DIR/claude.count"
            printf '%s' "$2" > "$FLEET_TEST_DIR/claude.call.$n"
            case "$2" in
              *"corrected gate"*)
                cp "$FLEET_TEST_DIR/gate-fixture-fixed.py" gate.py
                printf 'repaired\\n' ;;
              *"# VALIDATOR"*)
                cp "$FLEET_TEST_DIR/gate-fixture.py" gate.py
                printf 'gate written\\n' ;;
              *"# TRIAGE"*)
                cat "$FLEET_TEST_DIR/triage-fixture.txt" ;;
            esac
            """,
        )
        result = self._run("make hello.txt", FLEET_TEST_SUCCEED_ON="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self._summary()
        self.assertEqual(summary["status"], "green")
        self.assertEqual(summary["gate_repairs"], 1)
        self.assertEqual(len(summary["rounds"]), 3)
        self.assertEqual(summary["rounds"][2]["triage_verdict"], "GATE_DEFECT")
        self.assertEqual(summary["rounds"][2]["gate_verdict"], "pass")
        run_dir = self.out / summary["run_id"] / "autovalidate"
        self.assertTrue((run_dir / "gate.py.r3").is_file())
        codex_calls = int(
            (self.tmp / "codex.count").read_text(encoding="utf-8").strip()
        )
        self.assertEqual(codex_calls, 3, "free re-run: no builder between repair and gate")
```

(Builder succeeds on round 1 — writes hello.txt — but the DEFECTIVE gate keeps failing rounds 1-2; round 3 triage declares GATE_DEFECT, the repair swaps in the correct gate, and the free re-run passes because hello.txt already exists. `codex.count == 3` proves the free re-run spawned no extra builder round.)

- [ ] **Step 2: Run** → confirm it fails/errors before any fix, then passes with the Task 4 implementation (this test may already pass — if so, verify by mutating: temporarily set `gate_repairs = 1` initial... skip mutation; the assertions on `gate.py.r3`, `gate_repairs==1` and `codex_calls==3` bind the behavior).

- [ ] **Step 3: Add the justfile target** after the `fusion` target:

```make
# Fusion harness F2: gate-first build loop in an isolated workspace.
# Ex: just auto-validate "create hello.py that prints the first 10 primes"
auto-validate task *flags:
    python3 scripts/fusion/fusion_harness.py auto-validate "{{task}}" {{flags}}
```

- [ ] **Step 4: Gates:** `python3 -m unittest tests.test_auto_validate tests.test_fusion_harness` (44 total green), `python3 -m compileall -q scripts`, `wc -l scripts/fusion/auto_validate.py scripts/fusion/fusion_harness.py` (both < 800), `just --list | grep auto-validate`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_auto_validate.py justfile
git commit -m "feat(fusion): lock single free gate repair; expose just auto-validate

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Live smoke (closes the slice — controller runs it)

- [ ] **Step 1:** `env -u ANTHROPIC_API_KEY just auto-validate "Create a file primes.py in the workspace that, when run with python3, prints exactly the first 10 prime numbers, one per line."`
Expected: exit 0, status green in ≤5 rounds; calibrate `BUILDER_RESUME_ARGV`/`SESSION_ID_RE`/`VALIDATOR_ARGV` against real CLI behavior if the first live run surfaces drift (one-line data edits, then re-run tests).
- [ ] **Step 2:** Verify: gate.py is PEP 723 + deterministic; baseline.txt shows RED; summary rounds/memory recorded; ledger line present.
- [ ] **Step 3:** Write `orchestration/smoke-evidence/f2-auto-validate-smoke-<date>.md` (verdict, run, latencies, calibrations made, open lanes) and commit.
