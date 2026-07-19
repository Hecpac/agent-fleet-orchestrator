"""Kimi interactive provider adapter backed by Kimi Wire evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fleet_providers import BaseAdapter, ProviderError, ProviderIdentity


AGENT_FILE = ".kimi/agents/fleet-reviewer/agent.yaml"
UNSAFE_FLAGS = {
    "--yolo",
    "--yes",
    "-y",
    "--auto-approve",
    "--print",
    "--prompt",
    "-p",
    "--command",
    "-c",
    "--wire",
    "--acp",
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
        if any(argument in UNSAFE_FLAGS for argument in command):
            raise ProviderError("Kimi fleet command enables an unsafe/non-interactive mode")
        models = _option_values(command, "--model")
        if models != [identity.model]:
            raise ProviderError("Kimi launch command does not bind the configured model")
        if command.count("--thinking") != 1 or "--no-thinking" in command:
            raise ProviderError("Kimi fleet command must enable thinking exactly once")
        agent_files = _option_values(command, "--agent-file")
        if agent_files != [AGENT_FILE]:
            raise ProviderError("Kimi fleet command must bind the canonical reviewer agent")
        return value

    def prepare_submission(
        self, identity: ProviderIdentity, task: str, run_id: str, prompt_path: Path
    ) -> dict[str, str]:
        del prompt_path
        self._validate_source(identity)
        logical = self.logical_prompt(task, run_id)
        payload = (
            "Decode the JSON string after FDP_PROMPT= as your exact prompt and follow it. "
            f"FDP_PROMPT={json.dumps(logical, ensure_ascii=False)}"
        )
        return {"prompt": logical, "payload": payload, "transport": "inline-json"}
