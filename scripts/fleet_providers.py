#!/usr/bin/env python3
"""Provider-neutral contracts for launch, submission, evidence, and cancellation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Protocol, runtime_checkable


# Provider modules import this contract by its stable module name. Preserve the
# same class identities when this file is invoked directly as a CLI.
if __name__ == "__main__":
    sys.modules.setdefault("fleet_providers", sys.modules[__name__])


SAFE_IDENTITY = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")


class ProviderError(RuntimeError):
    """A provider adapter request is unsupported or identity-unsafe."""


class ProviderIdentityError(ProviderError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class ProviderIdentity:
    provider: str
    model: str
    variant: str | None
    hook_source: str

    @classmethod
    def create(
        cls, provider: str, model: str, variant: str | None, hook_source: str
    ) -> "ProviderIdentity":
        normalized_variant = variant or None
        for name, value in (("provider", provider), ("model", model)):
            if not isinstance(value, str) or not SAFE_IDENTITY.fullmatch(value):
                raise ProviderIdentityError(name, f"provider {name} is invalid")
        if not isinstance(hook_source, str) or (
            hook_source and not SAFE_IDENTITY.fullmatch(hook_source)
        ):
            raise ProviderIdentityError("hook_source", "provider hook_source is invalid")
        if normalized_variant is not None and not SAFE_IDENTITY.fullmatch(normalized_variant):
            raise ProviderIdentityError("variant", "provider variant is invalid")
        return cls(provider, model, normalized_variant, hook_source)


@dataclass(frozen=True)
class ProviderEvidence:
    response: str
    provider: str
    model: str
    variant: str | None = None


@runtime_checkable
class ProviderAdapter(Protocol):
    name: str
    hook_source: str
    session_file: str | None

    def validate_configuration(
        self,
        identity: ProviderIdentity,
        *,
        command: list[str] | None = None,
        runner: str = "interactive",
    ) -> dict[str, Any]: ...

    def launch_spec(
        self, identity: ProviderIdentity, command: list[str], *, runner: str
    ) -> dict[str, Any]: ...

    def prepare_submission(
        self, identity: ProviderIdentity, task: str, run_id: str, prompt_path: Path
    ) -> dict[str, str]: ...

    def submit(
        self, submission: dict[str, str], sender: Callable[[str], Any]
    ) -> Any: ...

    def confirm_submission(self, confirmer: Callable[[], Any]) -> Any: ...

    def observe(self, event: dict[str, Any], state: dict[str, Any]) -> str: ...

    def extract_final_response(
        self,
        identity: ProviderIdentity,
        session_id: str,
        run_id: str,
        stopped_at: str,
        readers: dict[str, Callable[[str, str, str], tuple[Any, ...]]],
    ) -> ProviderEvidence: ...

    def verify_identity(
        self, expected: ProviderIdentity, evidence: ProviderEvidence
    ) -> None: ...

    def cancel(self, canceller: Callable[[], Any]) -> Any: ...


class BaseAdapter:
    name = "base"
    hook_source = ""
    session_file: str | None = None
    fixed_provider: str | None = None

    def _validate_source(self, identity: ProviderIdentity) -> None:
        if identity.hook_source != self.hook_source:
            raise ProviderIdentityError(
                "hook_source",
                f"{self.name} adapter cannot claim hook source {identity.hook_source}",
            )
        if self.fixed_provider and identity.provider != self.fixed_provider:
            raise ProviderIdentityError(
                "provider",
                f"{self.name} adapter cannot claim provider {identity.provider}",
            )

    def validate_configuration(
        self,
        identity: ProviderIdentity,
        *,
        command: list[str] | None = None,
        runner: str = "interactive",
    ) -> dict[str, Any]:
        self._validate_source(identity)
        if runner not in {"interactive", "local", "api"}:
            raise ProviderError(f"unsupported provider runner: {runner}")
        if command is not None and (
            not isinstance(command, list)
            or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise ProviderError("provider command must be a non-empty string list")
        return {
            "adapter": self.name,
            "identity": asdict(identity),
            "runner": runner,
            "command": list(command or []),
        }

    def launch_spec(
        self, identity: ProviderIdentity, command: list[str], *, runner: str
    ) -> dict[str, Any]:
        return self.validate_configuration(identity, command=command, runner=runner)

    @staticmethod
    def logical_prompt(task: str, run_id: str) -> str:
        if not isinstance(task, str) or not task or not RUN_ID.fullmatch(run_id):
            raise ProviderError("submission task or run_id is invalid")
        return (
            f"{task}\n\n"
            "Fleet completion protocol: in the final answer, include exactly one final line "
            f"using FLEET_RESULT:{run_id}:<STATUS>, where STATUS is DONE, BLOCKED, or FAILED. "
            "Do not emit that line before the final answer."
        )

    def prepare_submission(
        self, identity: ProviderIdentity, task: str, run_id: str, prompt_path: Path
    ) -> dict[str, str]:
        self._validate_source(identity)
        prompt = self.logical_prompt(task, run_id)
        payload = (
            f"FLEET_RUN {run_id}: open the file {prompt_path} and execute its entire content "
            "as your exact task for this turn, following its output schema and field names "
            "exactly as written. Its completion protocol requires one final line using "
            f"FLEET_RESULT:{run_id}:<STATUS> where STATUS is DONE, BLOCKED, or FAILED."
        )
        return {"prompt": prompt, "payload": payload, "transport": "pointer"}

    def submit(self, submission: dict[str, str], sender: Callable[[str], Any]) -> Any:
        if set(submission) != {"prompt", "payload", "transport"}:
            raise ProviderError("provider submission fields are invalid")
        return sender(submission["payload"])

    def confirm_submission(self, confirmer: Callable[[], Any]) -> Any:
        result = confirmer()
        if result is False or result is None:
            raise ProviderError("provider submission was not confirmed")
        return result

    def observe(self, event: dict[str, Any], state: dict[str, Any]) -> str:
        del state
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            return "ignore"
        if (
            event.get("source") != self.hook_source
            or payload.get("_source") != self.hook_source
        ):
            return "ignore"
        if event.get("name") == "agent.hook.UserPromptSubmit" and payload.get("phase") == "received":
            return "bind"
        if event.get("name") == "agent.hook.Stop" and payload.get("phase") == "completed":
            return "stop"
        return "ignore"

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
            raise ProviderError(f"no evidence reader for {self.name}")
        value = reader(session_id, run_id, stopped_at)
        if len(value) != 3:
            raise ProviderError(f"{self.name} evidence shape is invalid")
        response, provider, model = value
        if not all(isinstance(item, str) and item for item in (response, provider, model)):
            raise ProviderError(f"{self.name} evidence fields are invalid")
        return ProviderEvidence(response, provider, model)

    def verify_identity(
        self, expected: ProviderIdentity, evidence: ProviderEvidence
    ) -> None:
        self._validate_source(expected)
        if evidence.provider != expected.provider or evidence.model != expected.model:
            raise ProviderIdentityError(
                "identity", f"{self.name} provider/model evidence does not match dispatch"
            )
        if evidence.variant != expected.variant:
            raise ProviderIdentityError(
                "variant", f"{self.name} variant evidence does not match dispatch"
            )

    def cancel(self, canceller: Callable[[], Any]) -> Any:
        result = canceller()
        if result is False or result is None:
            raise ProviderError("provider cancellation was not acknowledged")
        return result


class ProviderRegistry:
    def __init__(self, adapters: list[ProviderAdapter] | None = None) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}
        for adapter in default_adapters() if adapters is None else adapters:
            self.register(adapter)

    def register(self, adapter: ProviderAdapter) -> None:
        if not isinstance(adapter, ProviderAdapter):
            raise ProviderError("adapter does not implement the provider contract")
        if not adapter.name or adapter.name in self._adapters:
            raise ProviderError(f"duplicate provider adapter: {adapter.name}")
        self._adapters[adapter.name] = adapter

    def resolve(self, *, hook_source: str, provider: str) -> ProviderAdapter:
        name = hook_source or ("ollama" if provider == "ollama" else "")
        adapter = self._adapters.get(name)
        if adapter is None:
            raise ProviderError(f"unsupported provider adapter: {name or provider}")
        return adapter

    def names(self) -> list[str]:
        return sorted(self._adapters)


def default_adapters() -> list[ProviderAdapter]:
    from providers.claude import ClaudeAdapter
    from providers.codex import CodexAdapter
    from providers.ollama import OllamaAdapter
    from providers.opencode import OpenCodeAdapter

    return [CodexAdapter(), ClaudeAdapter(), OpenCodeAdapter(), OllamaAdapter()]


DEFAULT_REGISTRY = ProviderRegistry()


def identity(
    provider: str, model: str, variant: str | None = None, hook_source: str = ""
) -> ProviderIdentity:
    return ProviderIdentity.create(provider, model, variant, hook_source)


def infer_hook_source(provider: str) -> str:
    """Compatibility mapping for pre-adapter manifests that lack hook_source."""
    return {"openai": "codex", "anthropic": "claude", "ollama": ""}.get(
        provider, "opencode"
    )


def adapter_for(identity_value: ProviderIdentity) -> ProviderAdapter:
    adapter = DEFAULT_REGISTRY.resolve(
        hook_source=identity_value.hook_source, provider=identity_value.provider
    )
    adapter.validate_configuration(
        identity_value,
        runner="local" if adapter.name == "ollama" else "interactive",
    )
    return adapter


def _parser():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    validate = command.add_parser("validate")
    validate.add_argument("--provider", required=True)
    validate.add_argument("--model", required=True)
    validate.add_argument("--variant", default="")
    validate.add_argument("--hook-source", default="")
    validate.add_argument("--runner", default="interactive")
    validate.add_argument("--command-json", default="[]")
    command.add_parser("list")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "list":
            value: Any = {"adapters": DEFAULT_REGISTRY.names()}
        else:
            configured = identity(args.provider, args.model, args.variant, args.hook_source)
            adapter = DEFAULT_REGISTRY.resolve(
                hook_source=configured.hook_source, provider=configured.provider
            )
            command = json.loads(args.command_json)
            value = adapter.validate_configuration(
                configured, command=command or None, runner=args.runner
            )
        print(json.dumps(value, sort_keys=True))
        return 0
    except (ProviderError, json.JSONDecodeError) as exc:
        print(f"fleet-providers: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
