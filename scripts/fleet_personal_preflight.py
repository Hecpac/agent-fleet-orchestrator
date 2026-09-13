#!/usr/bin/env python3
"""Read-only personal CLI readiness and capability inventory; never starts agents."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

from fleet_herdr_versions import PERSONAL_CONTRACT


def command(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)


def inspect(target: Path, *, run=command, which=shutil.which) -> dict:
    target = target.resolve(strict=True)
    checks, binaries = {}, {}
    for name, expected in (("herdr", PERSONAL_CONTRACT["herdr_version"]),
                           ("codex", PERSONAL_CONTRACT["codex_version"])):
        binary = which(name)
        binaries[name] = binary
        check = {"path": binary, "expected": expected, "observed": None, "status": "missing"}
        if binary:
            result = run([binary, "--version"])
            prefix = "codex-cli " if name == "codex" else "herdr "
            observed = result.stdout.strip()
            check.update(observed=observed.removeprefix(prefix),
                         status="compatible" if result.returncode == 0 and observed == prefix + expected
                         else "incompatible")
        checks[name] = check

    auth = "unavailable"
    advertised = []
    if binaries["codex"]:
        result = run([binaries["codex"], "login", "status"])
        # Do not retain raw diagnostics, account identifiers, config, or credentials.
        if result.returncode == 0:
            auth = "chatgpt" if "Logged in using ChatGPT" in result.stdout + result.stderr else "other"
        else:
            auth = "not_logged_in"
        help_result = run([binaries["codex"], "--help"])
        if help_result.returncode == 0:
            for name, marker in (("shell", "--sandbox"), ("web_search", "--search"),
                                 ("image_input", "--image"), ("mcp", "mcp"), ("plugins", "plugin")):
                if marker in help_result.stdout:
                    advertised.append({"name": name, "status": "advertised_by_cli",
                                       "session_availability": "NOT_VERIFIED"})

    git = run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(target),
               "status", "--porcelain=v1", "--untracked-files=normal", "--ignored"])
    source = {"path": str(target), "status": "unavailable", "changed_entries": None}
    if git.returncode == 0:
        entries = git.stdout.splitlines()
        source.update(status="dirty" if entries else "clean", changed_entries=len(entries))
    blockers = [f"{name}_{check['status']}" for name, check in checks.items()
                if check["status"] != "compatible"]
    if auth != "chatgpt":
        blockers.append("personal_chatgpt_login_unavailable")
    if source["status"] != "clean":
        blockers.append("target_requires_exact_snapshot" if source["status"] == "dirty"
                        else "target_git_unavailable")
    return {"schema_version": 1, "lane": "official-cli-personal-v1",
            "runtime_contract": dict(PERSONAL_CONTRACT), "binaries": checks,
            "authentication": auth, "target": source, "capabilities": advertised,
            "blockers": blockers, "runtime_preflight": "PASS" if not blockers else "BLOCKED",
            "mission_requirements": ["explicit_session", "objective", "acceptance_contract"],
            "model_access": "NOT_VERIFIED", "tools_execution": "NOT_VERIFIED",
            "generation_requests": 0, "agents_started": 0}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        result = inspect(args.target_repo)
    except (OSError, subprocess.SubprocessError):
        print("personal-preflight: a local diagnostic could not complete", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["runtime_preflight"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
