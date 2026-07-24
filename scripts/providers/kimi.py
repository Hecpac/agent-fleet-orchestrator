"""Kimi interactive provider adapter backed by Kimi Wire evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fleet_providers import BaseAdapter, ProviderError, ProviderIdentity


NEWLINE_TOKEN = "<<<FDP_NEWLINE_7F4C2A91>>>"
BACKSLASH_TOKEN = "<<<FDP_BACKSLASH_7F4C2A91>>>"
# kimi-code 0.28 flag surface: --auto is fully autonomous, -p/--prompt and
# --output-format are non-interactive, -S/--session and -c/--continue resume
# a prior thread into what must be a fresh tracked Fleet identity.
UNSAFE_FLAGS = {
    "--yolo",
    "--yes",
    "-y",
    "--auto",
    "--auto-approve",
    "--print",
    "--prompt",
    "-p",
    "--output-format",
    "--continue",
    "-c",
    "--session",
    "-S",
}


def _option_values(command: list[str], option: str) -> list[str]:
    values: list[str] = []
    for index, argument in enumerate(command):
        if argument == option:
            if index + 1 >= len(command):
                raise ProviderError(f"Kimi launch command {option} lacks a value")
            values.append(command[index + 1])
        elif argument.startswith(f"{option}="):
            values.append(argument.partition("=")[2])
    return values


class KimiAdapter(BaseAdapter):
    name = "kimi"
    hook_source = "kimi"
    session_file = "kimi-hook-sessions.json"
    fixed_provider = "moonshot-ai"

    def validate_configuration(
        self,
        identity: ProviderIdentity,
        *,
        command: list[str] | None = None,
        runner: str = "interactive",
    ) -> dict[str, Any]:
        value = super().validate_configuration(identity, command=command, runner=runner)
        if runner != "interactive":
            raise ProviderError("Kimi adapter supports only the interactive runner")
        if identity.variant is not None:
            raise ProviderError("Kimi does not accept OpenCode variant identity")
        if command is None:
            return value
        if command[0] != "kimi":
            raise ProviderError("Kimi adapter requires a kimi launch command")
        if len(command) > 1 and not command[1].startswith("-"):
            raise ProviderError(
                "Kimi fleet command must launch the interactive TUI, not a subcommand"
            )
        if any(argument in UNSAFE_FLAGS for argument in command):
            raise ProviderError("Kimi fleet command enables an unsafe/non-interactive mode")
        models = _option_values(command, "--model") + _option_values(command, "-m")
        if models != [identity.model]:
            raise ProviderError("Kimi launch command does not bind the configured model")
        if command.count("--plan") != 1:
            raise ProviderError(
                "Kimi fleet command must start in plan mode exactly once"
            )
        return value

    def prepare_submission(
        self, identity: ProviderIdentity, task: str, run_id: str, prompt_path: Path
    ) -> dict[str, str]:
        del prompt_path
        self._validate_source(identity)
        logical = self.logical_prompt(task, run_id)
        if NEWLINE_TOKEN in logical or BACKSLASH_TOKEN in logical:
            raise ProviderError("Kimi prompt collides with the inline transport tokens")
        encoded = logical.replace("\\", BACKSLASH_TOKEN).replace("\n", NEWLINE_TOKEN)
        payload = (
            f"Reconstruct your exact prompt by replacing every literal {NEWLINE_TOKEN} "
            f"with a newline and every literal {BACKSLASH_TOKEN} with one backslash. "
            f"Then follow it. FDP_PROMPT={encoded}"
        )
        return {"prompt": logical, "payload": payload, "transport": "inline-tokenized"}
