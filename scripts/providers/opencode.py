"""OpenCode interactive provider adapter."""

from __future__ import annotations

import json
from typing import Any, Callable

from fleet_providers import (
    BaseAdapter,
    ProviderError,
    ProviderEvidence,
    ProviderIdentity,
)


class OpenCodeAdapter(BaseAdapter):
    name = "opencode"
    hook_source = "opencode"
    session_file = "opencode-hook-sessions.json"

    def validate_configuration(
        self, identity: ProviderIdentity, *, command: list[str] | None = None,
        runner: str = "interactive",
    ) -> dict[str, Any]:
        value = super().validate_configuration(identity, command=command, runner=runner)
        if command is not None and command[0] != "opencode":
            raise ProviderError("OpenCode adapter requires an opencode launch command")
        if command is not None and "--variant" in command:
            raise ProviderError("OpenCode variant identity must be pinned by a durable agent")
        return value

    def prepare_submission(
        self, identity: ProviderIdentity, task: str, run_id: str, prompt_path
    ) -> dict[str, str]:
        del prompt_path
        self._validate_source(identity)
        logical = self.logical_prompt(task, run_id)
        payload = (
            "Decode the JSON string after FDP_PROMPT= as your exact prompt and follow it. "
            f"FDP_PROMPT={json.dumps(logical, ensure_ascii=False)}"
        )
        return {"prompt": logical, "payload": payload, "transport": "inline-json"}

    def observe(self, event: dict[str, Any], state: dict[str, Any]) -> str:
        value = super().observe(event, state)
        if value != "stop":
            return value
        payload = event.get("payload") or {}
        if not (
            "_opencode_request_id" in payload
            and payload.get("_opencode_request_id") is None
            and isinstance(payload.get("context_length"), int)
            and not isinstance(payload.get("context_length"), bool)
            and int(payload["context_length"]) > 0
        ):
            return "ignore"
        return value

    def extract_final_response(
        self,
        identity: ProviderIdentity,
        session_id: str,
        run_id: str,
        stopped_at: str,
        readers: dict[str, Callable[[str, str, str], tuple[Any, ...]]],
    ) -> ProviderEvidence:
        self._validate_source(identity)
        reader = readers.get(self.name)
        if reader is None:
            raise ProviderError("no evidence reader for opencode")
        value = reader(session_id, run_id, stopped_at)
        if len(value) != 4:
            raise ProviderError("opencode evidence shape is invalid")
        response, provider, model, variant = value
        if not all(isinstance(item, str) and item for item in (response, provider, model)):
            raise ProviderError("opencode evidence fields are invalid")
        if variant is not None and not isinstance(variant, str):
            raise ProviderError("opencode variant evidence is invalid")
        return ProviderEvidence(response, provider, model, variant)
