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
    matches = SESSION_ID_RE.findall(text)
    return matches[-1] if matches else ""


def validator_leg(
    tier: str, task: str, av_dir: Path, timeout_seconds: float
) -> dict[str, Any]:
    spec = TIERS[tier]["architect"]
    prompt = render_av_template("validator.md", {"{{TASK}}": task})
    before = {entry.name for entry in av_dir.iterdir()}
    argv = _fill(VALIDATOR_ARGV, model=spec["model"], prompt=prompt)
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
    argv = _fill(TRIAGE_ARGV, model=spec["model"], prompt=prompt)
    outcome = run_agent("triage", argv, timeout_seconds)
    match = TRIAGE_VERDICT_RE.search(outcome["output"])
    if not match:
        return outcome, None, ""
    return outcome, match.group(1), match.group(2).strip()
