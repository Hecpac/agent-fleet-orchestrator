"""Ollama local/API provider adapter."""

from __future__ import annotations

from typing import Any

from fleet_providers import BaseAdapter, ProviderError, ProviderIdentity


class OllamaAdapter(BaseAdapter):
    name = "ollama"
    hook_source = ""
    session_file = None
    fixed_provider = "ollama"

    def validate_configuration(
        self, identity: ProviderIdentity, *, command: list[str] | None = None,
        runner: str = "local",
    ) -> dict[str, Any]:
        value = super().validate_configuration(identity, command=command, runner=runner)
        if runner not in {"local", "api"}:
            raise ProviderError("Ollama adapter supports only local or API runners")
        if identity.variant is not None:
            raise ProviderError("Ollama does not accept OpenCode variant identity")
        if command is not None and command[0] != "ollama":
            raise ProviderError("Ollama direct launch command must run ollama")
        return value

    def prepare_submission(
        self, identity: ProviderIdentity, task: str, run_id: str, prompt_path
    ) -> dict[str, str]:
        del prompt_path, run_id
        self._validate_source(identity)
        if not task:
            raise ProviderError("Ollama task is empty")
        return {"prompt": task, "payload": task, "transport": "api"}
