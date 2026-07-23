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
