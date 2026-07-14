#!/usr/bin/env python3
"""Run one visible, autonomous Dan+ fleet mission from a single command."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = ROOT / "orchestration" / "runs"
PROMPT_TEMPLATE = ROOT / "orchestration" / "prompts" / "dan_lead.md"
FEATURE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class MissionError(RuntimeError):
    """An autonomous mission could not be started or completed safely."""


def parse_manifest(path: Path) -> dict[str, str]:
    try:
        return dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
    except OSError as exc:
        raise MissionError(f"cannot read fleet manifest {path}: {exc}") from exc


def run_command(command: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MissionError(f"command failed to run: {command[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise MissionError(f"{Path(command[0]).name} failed: {detail}")
    return result


def last_json_object(text: str, *, source: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise MissionError(f"{source} returned no JSON object")


def render_prompt(
    *,
    feature: str,
    task: str,
    risk: str,
    target_repo: Path,
    manifest: Path,
    timeout_seconds: int,
) -> str:
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "{{FEATURE}}": feature,
        "{{TASK}}": task,
        "{{RISK}}": risk,
        "{{TARGET_REPO}}": str(target_repo),
        "{{MANIFEST}}": str(manifest),
        "{{TIMEOUT_SECONDS}}": str(timeout_seconds),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def cmux_signal(
    manifest: dict[str, str],
    *,
    state: str,
    progress: float,
    log: str,
    notify: bool = False,
) -> None:
    workspace = manifest.get("workspace")
    if not workspace:
        return
    commands = [
        ["cmux", "set-status", "mission", state, "--workspace", workspace, "--icon", "sparkles"],
        [
            "cmux",
            "set-progress",
            f"{progress:.2f}",
            "--label",
            state,
            "--workspace",
            workspace,
        ],
        ["cmux", "log", "--level", "info", "--source", "fleet-run", "--workspace", workspace, log],
    ]
    if notify:
        commands.append(
            ["cmux", "notify", "--title", f"Dan+ {manifest.get('feature', 'fleet')}: {state}", "--workspace", workspace]
        )
    for command in commands:
        subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, "CMUX_QUIET": "1"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def git_is_dirty(path: Path) -> bool:
    result = run_command(["git", "-C", str(path), "status", "--porcelain"])
    return bool(result.stdout.strip())


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("feature", help="stable fleet/branch identifier")
    value.add_argument("task", help="complete mission objective")
    value.add_argument("--preset", default="dan")
    value.add_argument("--target-repo", default=os.getcwd())
    value.add_argument("--timeout", type=int, default=3600)
    value.add_argument("--risk", choices=("auto", "low", "medium"), default="auto")
    value.add_argument(
        "--allow-dirty-baseline",
        action="store_true",
        help="base writer worktrees on HEAD even when the target checkout has uncommitted changes",
    )
    value.add_argument("--teardown", action="store_true", help="close the fleet after success")
    value.add_argument("--dry-run", action="store_true", help="render the mission without CMUX effects")
    value.add_argument("--json", action="store_true", help="emit one machine-readable result")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not FEATURE_RE.fullmatch(args.feature):
        print("invalid feature name", file=sys.stderr)
        return 2
    if args.timeout < 60:
        print("--timeout must be at least 60 seconds", file=sys.stderr)
        return 2
    target_repo = Path(args.target_repo).expanduser().resolve()
    runs_dir = Path(os.environ.get("FLEET_RUNS_DIR", DEFAULT_RUNS_DIR))
    manifest_path = runs_dir / f"fleet-{args.feature}.manifest"

    try:
        run_command(["git", "-C", str(target_repo), "rev-parse", "--git-dir"])
        dirty = git_is_dirty(target_repo)
        if dirty and not args.allow_dirty_baseline and not manifest_path.exists():
            raise MissionError(
                "target checkout is dirty; commit/stash it or pass --allow-dirty-baseline "
                "to acknowledge that writer worktrees start from HEAD only"
            )

        if args.dry_run:
            preview = render_prompt(
                feature=args.feature,
                task=args.task,
                risk=args.risk,
                target_repo=target_repo,
                manifest=manifest_path,
                timeout_seconds=args.timeout,
            )
            if args.json:
                print(json.dumps({"feature": args.feature, "preset": args.preset, "prompt": preview}))
            else:
                print(preview)
            return 0

        created = False
        if not manifest_path.exists():
            boot = run_command(
                [
                    str(ROOT / "scripts" / "fleet-up.sh"),
                    args.feature,
                    "--preset",
                    args.preset,
                    "--target-repo",
                    str(target_repo),
                ],
                timeout=300,
            )
            created = True
            if not args.json and boot.stdout.strip():
                print(boot.stdout.rstrip())

        manifest = parse_manifest(manifest_path)
        if manifest.get("mode") != "autonomous":
            raise MissionError(
                f"preset {manifest.get('preset', '?')} is mode={manifest.get('mode', 'guided')}; "
                "fleet-run only drives autonomous fleets"
            )
        recorded_target = Path(manifest.get("target_repo", "")).resolve()
        if recorded_target != target_repo:
            raise MissionError(
                f"existing fleet targets {recorded_target}, not requested target {target_repo}"
            )
        if manifest.get("lead.runner") != "interactive":
            raise MissionError("autonomous fleet has no interactive lead")

        prompt = render_prompt(
            feature=args.feature,
            task=args.task,
            risk=args.risk,
            target_repo=target_repo,
            manifest=manifest_path,
            timeout_seconds=args.timeout,
        )
        cmux_signal(manifest, state="delegating", progress=0.15, log="mission dispatched to lead")
        sent = run_command(
            [str(ROOT / "scripts" / "fleet-send.sh"), args.feature, "lead", prompt, "--json"],
            timeout=120,
        )
        send_value = last_json_object(sent.stdout, source="fleet-send")
        run_id = str(send_value.get("run_id", ""))
        if not run_id:
            raise MissionError("fleet-send returned no run_id")

        cmux_signal(manifest, state="working", progress=0.35, log=f"lead run started {run_id}")
        waited = run_command(
            [
                str(ROOT / "scripts" / "fleet-wait.sh"),
                args.feature,
                "lead",
                "--run",
                f"lead={run_id}",
                "--timeout",
                str(args.timeout),
                "--json",
            ],
            timeout=args.timeout + 30,
        )
        wait_value = last_json_object(waited.stdout, source="fleet-wait")
        if wait_value.get("status") != "succeeded":
            raise MissionError(f"lead ended with status={wait_value.get('status', 'unknown')}")
        result_file = Path(str(wait_value.get("result_file", "")))
        if not result_file.is_file():
            raise MissionError("lead succeeded without a durable result file")

        result_text = result_file.read_text(encoding="utf-8")
        cmux_signal(
            manifest,
            state="complete",
            progress=1.0,
            log=f"mission completed {run_id}",
            notify=True,
        )
        value = {
            "feature": args.feature,
            "preset": manifest.get("preset", ""),
            "mode": manifest.get("mode", ""),
            "workspace": manifest.get("workspace", ""),
            "lead_run_id": run_id,
            "status": "succeeded",
            "result_file": str(result_file),
            "result": result_text,
            "fleet_created": created,
        }
        if args.teardown:
            run_command([str(ROOT / "scripts" / "fleet-down.sh"), args.feature], timeout=180)
            value["teardown"] = "completed"
        if args.json:
            print(json.dumps(value, sort_keys=True))
        else:
            print(f"\nDan+ mission complete: {args.feature} ({run_id})")
            print(f"result: {result_file}\n")
            print(result_text.rstrip())
        return 0
    except MissionError as exc:
        manifest = parse_manifest(manifest_path) if manifest_path.exists() else {"feature": args.feature}
        cmux_signal(manifest, state="needs-attention", progress=0.0, log=str(exc)[:160], notify=True)
        if args.json:
            print(json.dumps({"feature": args.feature, "status": "failed", "error": str(exc)}))
        else:
            print(f"fleet-run: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
