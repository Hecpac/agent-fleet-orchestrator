#!/usr/bin/env python3
"""Read-only personal CLI readiness and capability inventory; never starts agents."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys

import fleet_codex_registry as codex_registry
import fleet_herdr_versions as versions
from fleet_herdr_versions import CURRENT_CONTRACT


def command(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)


def inspect(target: Path, *, run=command, which=shutil.which, environment=None) -> dict:
    target = target.resolve(strict=True)
    # With FLEET_CODEX_ROOT the Missions use the latest certified side-by-side
    # Codex, not the operator's global binary, which may be newer.
    store = codex_registry.from_environment(os.environ if environment is None else environment)
    entry = store.latest_certified(herdr_version=CURRENT_CONTRACT["herdr_version"]) if store else None
    contract = versions.certified_contract(entry) if entry else dict(CURRENT_CONTRACT)
    pin = None
    if entry:
        pin = {"codex_version": entry["codex_version"], "bin_dir": entry["bin_dir"],
               "certification": entry["certification_sha256"]}
        try:
            store.verify_install(entry)
        except codex_registry.RegistryError:
            pin["status"] = "changed_or_missing"
    checks, binaries = {}, {}
    for name, expected in (("herdr", contract["herdr_version"]),
                           ("codex", contract["codex_version"])):
        binary = str(Path(entry["bin_dir"]) / "codex") if name == "codex" and entry else which(name)
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
    if pin and pin.get("status"):
        blockers.append("certified_codex_install_" + pin["status"])
    if auth != "chatgpt":
        blockers.append("personal_chatgpt_login_unavailable")
    if source["status"] != "clean":
        blockers.append("target_requires_exact_snapshot" if source["status"] == "dirty"
                        else "target_git_unavailable")
    return {"schema_version": 1, "lane": "official-cli-personal-v1",
            "runtime_contract": contract, "codex_pin": pin, "binaries": checks,
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
