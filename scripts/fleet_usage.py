#!/usr/bin/env python3
"""Provider-aware token usage receipts and admission decisions.

The module is intentionally side-effect free.  Callers own durable storage and
must pass ``None`` when that storage is missing; an absent source is never
treated as an empty, zero-spend ledger.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

import fleet_json


SCHEMA_VERSION = 1
CAPABILITIES = frozenset({"none", "observed_total", "hard_output_only", "hard_total"})
RECEIPT_STATES = frozenset({"observed", "not_incurred", "unknown"})
PROVIDER_CAPABILITIES: Mapping[str, str] = MappingProxyType(
    {
        "claude": "none",
        "codex": "none",
        "kimi": "none",
        "ollama": "hard_output_only",
        "opencode": "none",
    }
)
POLICY_FIELDS = frozenset({"budget_mode", "token_budget"})
RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "provider",
        "capability",
        "state",
        "input_tokens",
        "output_tokens",
        "total_tokens",
    }
)


class UsageError(ValueError):
    """A usage policy, receipt, or admission input is not trustworthy."""


def _provider_capability(provider: Any) -> str:
    if type(provider) is not str or provider not in PROVIDER_CAPABILITIES:
        raise UsageError(f"unsupported usage provider: {provider!r}")
    return PROVIDER_CAPABILITIES[provider]


def _count(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise UsageError(f"{field} must be a non-negative integer")
    return value


def _canonical(value: dict[str, Any]) -> dict[str, Any]:
    try:
        fleet_json.canonical_bytes(value)
    except fleet_json.FleetJSONError as exc:
        raise UsageError(f"usage value is not canonical JSON: {exc}") from exc
    return value


def validate_policy(
    policy: Any,
    *,
    providers: list[str] | None = None,
) -> dict[str, Any]:
    """Validate one closed budget policy against its launch providers.

    A zero soft budget disables usage admission.  A hard budget is meaningful
    only when every possible provider can enforce a total-token ceiling before
    launch; none of the current S0 providers has that capability.
    """

    if (
        type(policy) is not dict
        or any(type(key) is not str for key in policy)
        or set(policy) != POLICY_FIELDS
    ):
        raise UsageError(
            "usage policy fields must be exactly budget_mode, token_budget"
        )
    mode = policy["budget_mode"]
    budget = policy["token_budget"]
    if type(mode) is not str or mode not in {"soft", "hard"}:
        raise UsageError("budget_mode must be soft or hard")
    if type(budget) is not int or budget < 0:
        raise UsageError("token_budget must be a non-negative integer")
    if providers is None:
        provider_names: list[str] = []
    elif type(providers) is list:
        provider_names = providers
    else:
        raise UsageError("providers must be a JSON array")
    capabilities = [
        (provider, _provider_capability(provider)) for provider in provider_names
    ]
    if mode == "hard":
        if budget == 0:
            raise UsageError("hard token_budget must be positive")
        if not capabilities:
            raise UsageError("hard token budget requires the complete provider set")
        unsupported = sorted(
            {
                provider
                for provider, capability in capabilities
                if capability != "hard_total"
            }
        )
        if unsupported:
            joined = ", ".join(unsupported)
            raise UsageError(
                "hard token budget requires hard_total capability; "
                f"unsupported providers: {joined}"
            )
    return _canonical({"budget_mode": mode, "token_budget": budget})


def receipt(
    provider: str,
    state: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
) -> dict[str, Any]:
    """Build one closed usage receipt without inventing missing counts."""

    capability = _provider_capability(provider)
    if type(state) is not str or state not in RECEIPT_STATES:
        raise UsageError(
            "usage receipt state must be observed, not_incurred, or unknown"
        )

    if state == "observed":
        if capability == "none":
            raise UsageError(f"provider {provider} cannot claim observed token totals")
        input_value = _count(input_tokens, "input_tokens")
        output_value = _count(output_tokens, "output_tokens")
        total_value = _count(total_tokens, "total_tokens")
        if input_value + output_value != total_value:
            raise UsageError("total_tokens must equal input_tokens + output_tokens")
    elif state == "not_incurred":
        supplied = (input_tokens, output_tokens, total_tokens)
        if any(
            value is not None and not (type(value) is int and value == 0)
            for value in supplied
        ):
            raise UsageError("not_incurred receipt counts must be zero or omitted")
        input_value = output_value = total_value = 0
    else:
        if any(
            value is not None for value in (input_tokens, output_tokens, total_tokens)
        ):
            raise UsageError("unknown receipt counts must be null or omitted")
        input_value = output_value = total_value = None

    return _canonical(
        {
            "schema_version": SCHEMA_VERSION,
            "provider": provider,
            "capability": capability,
            "state": state,
            "input_tokens": input_value,
            "output_tokens": output_value,
            "total_tokens": total_value,
        }
    )


def _validated_receipt(value: Any) -> dict[str, Any]:
    if (
        type(value) is not dict
        or any(type(key) is not str for key in value)
        or set(value) != RECEIPT_FIELDS
    ):
        raise UsageError("usage receipt fields do not match schema_version=1")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise UsageError("unsupported usage receipt schema")
    normalized = receipt(
        value["provider"],
        value["state"],
        input_tokens=value["input_tokens"],
        output_tokens=value["output_tokens"],
        total_tokens=value["total_tokens"],
    )
    if (
        type(value["capability"]) is not str
        or value["capability"] != normalized["capability"]
    ):
        raise UsageError("usage receipt capability does not match its provider")
    if value != normalized:
        raise UsageError("usage receipt is not canonical")
    return normalized


def summarize(receipts: Any) -> dict[str, Any]:
    """Aggregate closed receipts deterministically.

    ``None`` means the receipt source itself is missing.  In that case, or when
    any receipt is unknown, aggregate token totals are ``None``.  Known
    subtotals remain available under ``observed_*`` fields for diagnostics.
    """

    source_present = receipts is not None
    if receipts is None:
        values: list[Any] = []
    elif type(receipts) is list:
        values = receipts
    else:
        raise UsageError("usage receipts must be a JSON array or null")

    observed_input = 0
    observed_output = 0
    observed_total = 0
    observed_receipts = 0
    not_incurred_receipts = 0
    unknown_receipts = 0
    providers: set[str] = set()
    for raw in values:
        value = _validated_receipt(raw)
        providers.add(value["provider"])
        if value["state"] == "observed":
            observed_receipts += 1
            observed_input += value["input_tokens"]
            observed_output += value["output_tokens"]
            observed_total += value["total_tokens"]
        elif value["state"] == "not_incurred":
            not_incurred_receipts += 1
        else:
            unknown_receipts += 1

    complete = source_present and unknown_receipts == 0
    if not complete:
        state = "unknown"
        input_tokens = output_tokens = total_tokens = None
    elif observed_receipts:
        state = "observed"
        input_tokens = observed_input
        output_tokens = observed_output
        total_tokens = observed_total
    else:
        state = "not_incurred"
        input_tokens = output_tokens = total_tokens = 0

    return _canonical(
        {
            "schema_version": SCHEMA_VERSION,
            "state": state,
            "source_present": source_present,
            "providers": sorted(providers),
            "receipt_count": len(values),
            "observed_receipts": observed_receipts,
            "not_incurred_receipts": not_incurred_receipts,
            "unknown_receipts": unknown_receipts,
            "observed_input_tokens": observed_input,
            "observed_output_tokens": observed_output,
            "observed_total_tokens": observed_total,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
    )


def admit(
    policy: Any,
    provider: str,
    receipts: Any,
) -> dict[str, Any]:
    """Decide whether one new launch is admissible under a usage policy."""

    capability = _provider_capability(provider)
    validated_policy = validate_policy(policy, providers=[provider])
    summary = summarize(receipts)
    mode = validated_policy["budget_mode"]
    budget = validated_policy["token_budget"]

    if mode == "soft" and budget == 0:
        admitted = True
        reason = "budget_disabled"
        remaining_tokens = None
    elif summary["state"] == "unknown":
        admitted = False
        reason = "usage_unknown"
        remaining_tokens = None
    else:
        total = summary["total_tokens"]
        remaining_tokens = max(budget - total, 0)
        admitted = total < budget
        reason = "within_budget" if admitted else "budget_exhausted"

    return _canonical(
        {
            "schema_version": SCHEMA_VERSION,
            "admitted": admitted,
            "reason": reason,
            "provider": provider,
            "provider_capability": capability,
            "budget_mode": mode,
            "token_budget": budget,
            "remaining_tokens": remaining_tokens,
            "summary": summary,
        }
    )


def canonical_bytes(value: Any) -> bytes:
    """Encode API output using Fleet's strict canonical JSON contract."""

    try:
        return fleet_json.canonical_bytes(value)
    except fleet_json.FleetJSONError as exc:
        raise UsageError(f"usage value is not canonical JSON: {exc}") from exc
