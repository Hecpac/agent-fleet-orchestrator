#!/usr/bin/env python3
"""Fusion harness: multi-model micro-SDLC commands over the fleet's CLIs.

F0 implements ``opinion``: the same question goes to independent model
perspectives in parallel (ARCHITECT + BUILDER, optional local PANEL), and the
harness renders a comparative panel without merging — the human reads and
decides. Later slices add ``fusion`` (attributed synthesis) and
``auto-validate`` (gate-first build loop).

Doctrine (docs/fusion-harness-propuesta.md):
- Workers never talk to each other; only prompt -> outputs.
- Roles are prompts, not just models: the same tier gains lift from the
  role split alone.
- Artifacts land under outputs/fusion/<run_id>/ (gitignored), never in the
  repo; a light append-only ledger records each run.
- This file stays under 800 lines; features that do not fit belong to
  Mission Control, not here.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Any
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fleet_json  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEMPLATE = Path(__file__).resolve().parent / "prompts" / "opinion.md"

# Role != model. Models change quarterly; the harness compounds. Changing a
# tier is a one-line edit here (frozen decision D3: workhorse is the default).
TIERS: dict[str, dict[str, dict[str, Any]]] = {
    "workhorse": {
        "architect": {
            "cli": "claude",
            "model": "claude-sonnet-5",
            "argv": ["claude", "-p", "{prompt}", "--model", "{model}"],
        },
        "builder": {
            "cli": "codex",
            "model": "gpt-5.6-terra",
            "argv": ["codex", "exec", "--model", "{model}", "{prompt}"],
        },
    },
    "sota": {
        "architect": {
            "cli": "claude",
            "model": "claude-fable-5",
            "argv": ["claude", "-p", "{prompt}", "--model", "{model}"],
        },
        "builder": {
            "cli": "codex",
            "model": "gpt-5.6-sol",
            "argv": ["codex", "exec", "--model", "{model}", "{prompt}"],
        },
    },
}

ROLE_HINTS = {
    "architect": (
        "Reason from architecture: constraints, tradeoffs, failure modes, "
        "and long-term maintenance cost."
    ),
    "builder": (
        "Reason from implementation: concrete steps, code-level costs, and "
        "what breaks in practice."
    ),
    "panel": (
        "Give a fast independent third opinion, and flag anything the "
        "question itself gets wrong."
    ),
}

TERM_GRACE_SECONDS = 5.0


class FusionError(RuntimeError):
    """The harness cannot run the requested command."""


def output_root() -> Path:
    configured = os.environ.get("FLEET_FUSION_OUTPUT_DIR")
    return Path(configured) if configured else REPO_ROOT / "outputs" / "fusion"


def render_prompt(question: str, role: str) -> str:
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    rendered = (
        template.replace("{{ROLE}}", role.upper())
        .replace("{{ROLE_HINT}}", ROLE_HINTS[role])
        .replace("{{QUESTION}}", question)
    )
    if "{{" in rendered:
        raise FusionError("opinion prompt template has unresolved variables")
    return rendered


FUSION_TEMPLATE = Path(__file__).resolve().parent / "prompts" / "fusion.md"
BOUNDARY_JOIN = "\x1f"
DEFAULT_INSTRUCTION = "(none — follow the output contract as written)"
MARKER_PREFIXES = (
    "<<<INPUT_ARCHITECT_",
    "<<<END_INPUT_ARCHITECT_",
    "<<<INPUT_BUILDER_",
    "<<<END_INPUT_BUILDER_",
)


def fusion_boundary(architect_content: str, builder_content: str) -> str:
    digest = hashlib.sha256(
        (architect_content + BOUNDARY_JOIN + builder_content).encode()
    ).hexdigest()
    return digest[:12]


def render_fusion_prompt(
    *,
    question: str,
    instruction: str,
    architect_model: str,
    builder_model: str,
    architect_content: str,
    builder_content: str,
) -> str:
    template = FUSION_TEMPLATE.read_text(encoding="utf-8")
    # Any marker-shaped sequence inside input data is a collision: fail
    # closed before FUSION could mistake data for a boundary (G1).
    for content in (architect_content, builder_content):
        if any(prefix in content for prefix in MARKER_PREFIXES):
            raise FusionError("fusion input collides with the boundary markers")
    boundary = fusion_boundary(architect_content, builder_content)
    mapping = {
        "{{QUESTION}}": question,
        "{{FUSION_INSTRUCTION}}": instruction or DEFAULT_INSTRUCTION,
        "{{ARCHITECT_MODEL}}": architect_model,
        "{{BUILDER_MODEL}}": builder_model,
        "{{BOUNDARY}}": boundary,
        "{{ARCHITECT_CONTENT}}": architect_content,
        "{{BUILDER_CONTENT}}": builder_content,
    }
    unknown = set(re.findall(r"\{\{[A-Z_]+\}\}", template)) - set(mapping)
    if unknown:
        raise FusionError(
            f"fusion template has unresolved variables: {sorted(unknown)}"
        )
    # Single pass: substituted content is never rescanned, so template
    # placeholders inside the untrusted inputs stay literal data (G1).
    pattern = re.compile("|".join(re.escape(key) for key in mapping))
    return pattern.sub(lambda match: mapping[match.group(0)], template)


def _signal_group(process: subprocess.Popen[str], signum: int) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signum)
    except (ProcessLookupError, PermissionError):
        pass


def run_agent(
    role: str, argv: list[str], timeout_seconds: float
) -> dict[str, Any]:
    """Run one worker CLI; SIGTERM then SIGKILL on timeout, never a hang.

    Each agent owns a fresh process group and is terminated as a unit —
    a surviving CLI descendant would otherwise hold the output pipes open
    and turn every timeout into a hang for the descendant's lifetime.
    """
    started = time.monotonic()
    outcome: dict[str, Any] = {"role": role, "status": "ok", "exit_code": 0}
    # A leaked ANTHROPIC_API_KEY diverts headless claude from subscription
    # OAuth to whatever account the key names (smoke F0, corrida 20a44f2c).
    agent_env = {
        key: value
        for key, value in os.environ.items()
        if key != "ANTHROPIC_API_KEY"
    }
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
        start_new_session=True,
        env=agent_env,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _signal_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
        outcome["status"] = "timeout"
    outcome["exit_code"] = process.returncode
    if outcome["status"] == "ok" and process.returncode != 0:
        outcome["status"] = "failed"
    outcome["latency_seconds"] = round(time.monotonic() - started, 3)
    outcome["output"] = stdout or ""
    outcome["stderr_tail"] = (stderr or "")[-2000:]
    return outcome


def opinion(args: argparse.Namespace) -> int:
    question = args.question.strip()
    if not question:
        raise FusionError("opinion requires a non-empty question")
    tier = args.tier or os.environ.get("FLEET_FUSION_TIER", "workhorse")
    if tier not in TIERS:
        raise FusionError(f"unknown tier: {tier}; available: {sorted(TIERS)}")
    timeout_seconds = float(os.environ.get("FLEET_FUSION_TIMEOUT", "300"))

    agents: list[dict[str, Any]] = []
    for role, spec in TIERS[tier].items():
        prompt = render_prompt(question, role)
        argv = [
            part.replace("{prompt}", prompt).replace("{model}", spec["model"])
            for part in spec["argv"]
        ]
        agents.append(
            {"role": role, "cli": spec["cli"], "model": spec["model"], "argv": argv}
        )
    if args.panel:
        panel_command = os.environ.get(
            "FLEET_FUSION_PANEL_CMD",
            str(REPO_ROOT / "scripts" / "run-local-worker.sh"),
        )
        agents.append(
            {
                "role": "panel",
                "cli": panel_command,
                "model": args.panel_role,
                "argv": [
                    panel_command,
                    args.panel_role,
                    render_prompt(question, "panel"),
                ],
            }
        )

    run_id = str(uuid.uuid4())[:8]
    run_dir = output_root() / run_id / "opinion"
    run_dir.mkdir(parents=True, exist_ok=False)

    # Independence is the contract: every perspective runs blind and in
    # parallel; nothing is shared until the human (or a later fusion agent)
    # reads the artifacts from disk.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(agents)) as pool:
        futures = {
            pool.submit(
                run_agent, agent["role"], agent["argv"], timeout_seconds
            ): agent
            for agent in agents
        }
        outcomes = {
            futures[future]["role"]: future.result()
            for future in concurrent.futures.as_completed(futures)
        }

    summary_agents = []
    for agent in agents:
        outcome = outcomes[agent["role"]]
        artifact = run_dir / f"{agent['role']}.md"
        artifact.write_text(outcome["output"], encoding="utf-8")
        summary_agents.append(
            {
                "role": agent["role"],
                "cli": agent["cli"],
                "model": agent["model"],
                "status": outcome["status"],
                "exit_code": outcome["exit_code"],
                "latency_seconds": outcome["latency_seconds"],
                "output_path": str(artifact),
                "output_chars": len(outcome["output"]),
                "stderr_tail": outcome["stderr_tail"],
            }
        )

    summary = {
        "schema_version": 1,
        "command": "opinion",
        "run_id": run_id,
        "tier": tier,
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "agents": summary_agents,
    }
    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    ledger_path = output_root() / "ledger.jsonl"
    with ledger_path.open("ab") as ledger:
        ledger.write(fleet_json.canonical_bytes(summary) + b"\n")

    ok = [item for item in summary_agents if item["status"] == "ok"]
    print(f"opinion run {run_id} (tier={tier})")
    for item in summary_agents:
        print(
            f"  {item['role']:<10} {item['model']:<18} "
            f"{item['status']:<8} {item['latency_seconds']:>7.1f}s "
            f"{item['output_chars']:>7} chars  {item['output_path']}"
        )
    print(f"  summary: {summary_path}")
    if not ok:
        return 2
    if len(ok) != len(summary_agents):
        return 3
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    opinion_parser = sub.add_parser(
        "opinion", help="ask independent model perspectives the same question"
    )
    opinion_parser.add_argument("question")
    opinion_parser.add_argument("--tier", choices=sorted(TIERS))
    opinion_parser.add_argument(
        "--panel", action="store_true", help="add a local Ollama third opinion"
    )
    opinion_parser.add_argument(
        "--panel-role", default="triage", help="local router role for the panel"
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "opinion":
            return opinion(args)
        raise FusionError(f"unknown command: {args.command}")
    except FusionError as error:
        print(f"fusion: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
