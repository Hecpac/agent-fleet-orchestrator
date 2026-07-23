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
import json
import os
from pathlib import Path
import sys
import re
import subprocess
import uuid
from typing import Any

import fleet_json
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
    if completed.returncode == 127:
        # POSIX shell convention for "command not found": uv resolved on
        # PATH but could not run at all. Distinct from a legitimate FAIL
        # exit, which the toy/real gates never produce via 127.
        result["harness_error"] = "uv exited 127 (not found / could not run)"
        return result
    output = (completed.stdout or "") + (completed.stderr or "")
    result["output"] = output
    result["fail_lines"] = [
        line for line in output.splitlines() if line.startswith("FAIL:")
    ]
    result["verdict"] = "pass" if completed.returncode == 0 else "fail"
    return result


def extract_session_id(text: str) -> str:
    matches = SESSION_ID_RE.findall(text)
    return matches[-1] if matches else ""


VALIDATOR_IGNORED_DROPPINGS = {".claude.json", ".claude"}


def _tree(root: Path) -> set[str]:
    paths: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        for name in dirnames + filenames:
            paths.add(str(rel / name))
    return paths


def validator_leg(
    tier: str, task: str, av_dir: Path, timeout_seconds: float
) -> dict[str, Any]:
    spec = TIERS[tier]["architect"]
    prompt = render_av_template("validator.md", {"{{TASK}}": task})
    before = _tree(av_dir)
    argv = _fill(VALIDATOR_ARGV, model=spec["model"], prompt=prompt)
    outcome = run_agent("validator", argv, timeout_seconds, cwd=av_dir)
    created = _tree(av_dir) - before
    created -= {p for p in created if Path(p).parts[0] in VALIDATOR_IGNORED_DROPPINGS}
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
    r"^\s*TRIAGE_VERDICT:\s*(BUILDER_DEFECT|GATE_DEFECT)\s*[—-]?\s*(.*)$",
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
    argv = _fill(TRIAGE_ARGV, model=spec["model"], prompt=prompt)
    outcome = run_agent("triage", argv, timeout_seconds)
    matches = list(TRIAGE_VERDICT_RE.finditer(outcome["output"]))
    if not matches:
        return outcome, None, ""
    match = matches[-1]
    return outcome, match.group(1), match.group(2).strip()


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
