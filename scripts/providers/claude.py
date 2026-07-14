"""Claude interactive provider adapter."""

from __future__ import annotations

from typing import Any

from fleet_providers import BaseAdapter, ProviderError, ProviderIdentity


class ClaudeAdapter(BaseAdapter):
    name = "claude"
    hook_source = "claude"
    session_file = "claude-hook-sessions.json"
    fixed_provider = "anthropic"

    def validate_configuration(
        self, identity: ProviderIdentity, *, command: list[str] | None = None,
        runner: str = "interactive",
    ) -> dict[str, Any]:
        value = super().validate_configuration(identity, command=command, runner=runner)
        if identity.variant is not None:
            raise ProviderError("Claude does not accept OpenCode variant identity")
        if command is not None and (
            command[0] != "claude"
            or "--model" not in command
            or command.index("--model") + 1 >= len(command)
            or command[command.index("--model") + 1] != identity.model
        ):
            raise ProviderError("Claude launch command does not bind the configured model")
        return value

    def observe(self, event: dict[str, Any], state: dict[str, Any]) -> str:
        value = super().observe(event, state)
        payload = event.get("payload") or {}
        if (
            value == "ignore"
            and event.get("source") == self.hook_source
            and payload.get("_source") == self.hook_source
            and event.get("name") == "agent.hook.SessionEnd"
            and payload.get("phase") == "completed"
        ):
            return "session_end"
        return value
