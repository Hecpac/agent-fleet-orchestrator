"""Codex interactive provider adapter."""

from __future__ import annotations

from typing import Any

from fleet_providers import BaseAdapter, ProviderError, ProviderIdentity


class CodexAdapter(BaseAdapter):
    name = "codex"
    hook_source = "codex"
    session_file = "codex-hook-sessions.json"
    fixed_provider = "openai"

    def validate_configuration(
        self, identity: ProviderIdentity, *, command: list[str] | None = None,
        runner: str = "interactive",
    ) -> dict[str, Any]:
        value = super().validate_configuration(identity, command=command, runner=runner)
        if identity.variant is not None:
            raise ProviderError("Codex does not accept OpenCode variant identity")
        if command is not None and (
            command[0] != "codex"
            or "--model" not in command
            or command.index("--model") + 1 >= len(command)
            or command[command.index("--model") + 1] != identity.model
        ):
            raise ProviderError("Codex launch command does not bind the configured model")
        return value
