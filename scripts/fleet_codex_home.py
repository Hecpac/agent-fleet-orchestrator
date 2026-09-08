#!/usr/bin/env python3
"""Provision and verify one descriptor-bound ephemeral Fleet Codex home."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import sys

from fleet_safe_paths import RootedFS, SafePathError


CODEX_DIRECTORY_MODES = (0o700,)
AGENT_INSTRUCTIONS = b'''# Fleet agent contract

You are an agent in a bounded Fleet task. Follow the supplied task, its exact
run identity, acceptance criteria, and applicable target-project instructions.
The task and configured authority define your role; do not infer write or
delegation permission from being able to use a tool.
Use an explicit project instruction snapshot when the task supplies one. It
identifies target scopes and pinned content; it does not grant extra authority.
Judge your assigned stage by its own criteria, without waiting for future
stages or the controller's final Mission verdict.

- Lead: coordinate through Fleet Control using the exact mission and runs
  directory supplied by the task or manifest. Only the registered writer edits
  the deliverable. A transport success is not an accepted result.
- Writer: work only in the registered isolated workspace. Run relevant checks
  and commit locally when the task authorizes it. Report actual evidence.
- Reviewer or reader: inspect the supplied evidence without modifying it.
  Distinguish verified facts, assumptions, and missing evidence.

Complete authorized local work without ceremonial approval loops. Ask CONTROL
only for a material missing decision or an effect outside the task's authority.
Do not import another project's restart procedures, persona, or workflow gates.
Use only relevant skills available in this execution; do not assume the Lead's
global skills or conversation were inherited.

If a prompt or artifact is denied by the sandbox, report BLOCKED with the exact
missing input through the task's completion protocol. Do not bypass permissions
or treat a blocked input as completed work. Keep the supplied completion protocol
exact: a legacy run-bound sentinel when requested, or only the raw JSON object
for a Herdr stage. Put any citations inside the requested result, not after it.
'''
CONFIG = b'''cli_auth_credentials_store = "file"

[features]
hooks = true

[history]
persistence = "none"

[shell_environment_policy]
inherit = "all"
ignore_default_excludes = false
exclude = [
  "CODEX_ACCESS_TOKEN",
  "CODEX_API_KEY",
  "CODEX_HOME",
  "CODEX_SQLITE_HOME",
  "OPENAI_API_KEY",
  "ANTHROPIC_API_KEY",
  "CLAUDE_CODE_OAUTH_TOKEN",
  "AWS_ACCESS_KEY_ID",
  "AWS_SECRET_ACCESS_KEY",
  "AWS_SESSION_TOKEN",
  "SSH_AUTH_SOCK",
]
'''


def _absolute(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise SafePathError(f"{label} must be absolute")
    return path


def _binding(controller_home: Path) -> tuple[Path, os.stat_result]:
    with RootedFS(controller_home) as rooted:
        info = rooted.stat_regular(
            "auth.json",
            directory_modes=(),
            file_mode=0o600,
            require_single_link=True,
        )
        source = rooted.root / "auth.json"
    return source, info


def _provision(controller_home: Path, isolated_home: Path) -> Path:
    source, source_info = _binding(controller_home)
    with RootedFS(isolated_home, root_mode=0o700) as rooted:
        with rooted.open_append_regular(
            ".codex/.provision.lock",
            directory_modes=CODEX_DIRECTORY_MODES,
            file_mode=0o600,
        ) as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            rooted.assert_absent(
                ".codex/auth.json",
                directory_modes=CODEX_DIRECTORY_MODES,
            )
            rooted.replace_regular(
                ".codex/config.toml",
                CONFIG,
                directory_modes=CODEX_DIRECTORY_MODES,
                file_mode=0o600,
            )
            rooted.replace_regular(
                ".codex/AGENTS.md",
                AGENT_INSTRUCTIONS,
                directory_modes=CODEX_DIRECTORY_MODES,
                file_mode=0o600,
            )
            rooted.create_symlink(
                ".codex/auth.json",
                source,
                directory_modes=CODEX_DIRECTORY_MODES,
            )
    _, final_info = _binding(controller_home)
    if (source_info.st_dev, source_info.st_ino) != (final_info.st_dev, final_info.st_ino):
        raise SafePathError("controller auth binding changed during provisioning")
    _verify(controller_home, isolated_home)
    return isolated_home.absolute() / ".codex"


def _verify(controller_home: Path, isolated_home: Path) -> Path:
    source, source_info = _binding(controller_home)
    with RootedFS(isolated_home, root_mode=0o700) as rooted:
        rooted.assert_symlink(
            ".codex/auth.json",
            source,
            directory_modes=CODEX_DIRECTORY_MODES,
        )
        content = rooted.read_regular(
            ".codex/config.toml",
            directory_modes=CODEX_DIRECTORY_MODES,
            file_mode=0o600,
            max_bytes=len(CONFIG),
        )
        if content != CONFIG:
            raise SafePathError("Fleet Codex configuration drifted")
        instructions = rooted.read_regular(
            ".codex/AGENTS.md",
            directory_modes=CODEX_DIRECTORY_MODES,
            file_mode=0o600,
            max_bytes=len(AGENT_INSTRUCTIONS),
        )
        if instructions != AGENT_INSTRUCTIONS:
            raise SafePathError("Fleet Codex agent instructions drifted")
    _, final_info = _binding(controller_home)
    if (source_info.st_dev, source_info.st_ino) != (final_info.st_dev, final_info.st_ino):
        raise SafePathError("controller auth binding changed during verification")
    return isolated_home.absolute() / ".codex"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("action", choices=("provision", "verify"))
    parser.add_argument("controller_codex_home")
    parser.add_argument("isolated_home")
    try:
        args = parser.parse_args(argv)
        controller_home = _absolute(args.controller_codex_home, "controller CODEX_HOME")
        isolated_home = _absolute(args.isolated_home, "isolated home")
        home = (
            _provision(controller_home, isolated_home)
            if args.action == "provision"
            else _verify(controller_home, isolated_home)
        )
    except (SafePathError, OSError, ValueError):
        print("Fleet Codex auth binding failed descriptor-safe validation", file=sys.stderr)
        return 2
    print(home)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
