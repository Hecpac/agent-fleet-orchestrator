"""Pure schema-v1 workflow policy contract, independent of compilation and runtimes.

The public aliases in workflow_config retain existing caller and error identity.
Historical schema changes require an explicit versioned reader, not mutation of
retained artifacts or a weaker effect-admission policy.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

import fleet_json


IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
RISK_LEVELS = {"low", "medium", "high", "unknown"}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "unknown": 3}
GATES = {"fdp2", "human_build_exit", "fdp3"}
WORM_CATEGORIES = {
    "regulated",
    "production",
    "money",
    "credentials",
    "private_data",
    "destructive",
}
ROOT_FIELDS = {
    "schema_version",
    "name",
    "description",
    "preset",
    "autonomy",
    "capabilities",
    "risk",
    "assurance",
    "audit",
    "archive",
    "limits",
}


class WorkflowError(ValueError):
    """A workflow violates the frozen policy-only contract."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return fleet_json.canonical_bytes(value)
    except fleet_json.FleetJSONError as exc:
        raise WorkflowError(f"value is not canonical JSON: {exc}") from exc


def sha256(value: Any) -> str:
    try:
        return fleet_json.sha256(value)
    except fleet_json.FleetJSONError as exc:
        raise WorkflowError(f"value is not canonical JSON: {exc}") from exc


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowError(f"{where} must be an object")
    return value


def _keys(
    value: dict[str, Any],
    required: Iterable[str],
    where: str,
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - value.keys())
    unknown = sorted(value.keys() - allowed)
    if missing:
        raise WorkflowError(f"{where} missing fields: {', '.join(missing)}")
    if unknown:
        raise WorkflowError(f"{where} unknown fields: {', '.join(unknown)}")


def _identifier(value: Any, where: str, *, allow_none: bool = False) -> str:
    if allow_none and value == "none":
        return value
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise WorkflowError(f"{where} must match {IDENTIFIER.pattern}")
    return value


def _string(value: Any, where: str, *, max_length: int = 1000) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or any(char in value for char in ("\x00", "\r"))
    ):
        raise WorkflowError(f"{where} must be a non-empty safe string")
    return value


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise WorkflowError(f"{where} must be boolean")
    return value


def _integer(
    value: Any, where: str, *, minimum: int, maximum: int | None = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WorkflowError(f"{where} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise WorkflowError(f"{where} must be <= {maximum}")
    return value


def _names(value: Any, where: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not IDENTIFIER.fullmatch(item) for item in value
    ):
        raise WorkflowError(f"{where} must be a list of identifiers")
    if not allow_empty and not value:
        raise WorkflowError(f"{where} must not be empty")
    if len(value) != len(set(value)):
        raise WorkflowError(f"{where} contains duplicates")
    return value


def _enum(value: Any, choices: set[str], where: str) -> str:
    if value not in choices:
        raise WorkflowError(f"{where} must be one of {sorted(choices)}")
    return value


def validate_workflow(value: Any) -> None:
    workflow = _object(value, "workflow")
    _keys(workflow, ROOT_FIELDS, "workflow")
    if type(workflow["schema_version"]) is not int or workflow["schema_version"] != 1:
        raise WorkflowError("workflow.schema_version must be 1")
    _identifier(workflow["name"], "workflow.name")
    _string(workflow["description"], "workflow.description")
    _identifier(workflow["preset"], "workflow.preset")

    autonomy = _object(workflow["autonomy"], "workflow.autonomy")
    _keys(
        autonomy,
        {"owner", "allow_parallel", "allow_subdelegation", "max_delegation_depth"},
        "workflow.autonomy",
    )
    expected_owner = "worker" if workflow["preset"] == "sol_minimal_v1" else "lead"
    if autonomy["owner"] != expected_owner:
        raise WorkflowError(f"workflow.autonomy.owner must be {expected_owner}")
    _boolean(autonomy["allow_parallel"], "workflow.autonomy.allow_parallel")
    _boolean(autonomy["allow_subdelegation"], "workflow.autonomy.allow_subdelegation")
    depth = _integer(
        autonomy["max_delegation_depth"],
        "workflow.autonomy.max_delegation_depth",
        minimum=0,
        maximum=8,
    )
    if not autonomy["allow_subdelegation"] and depth != 0:
        raise WorkflowError(
            "max_delegation_depth must be 0 when subdelegation is disabled"
        )

    capabilities = _object(workflow["capabilities"], "workflow.capabilities")
    _keys(
        capabilities,
        {"available", "required_outcomes", "writer"},
        "workflow.capabilities",
    )
    _names(capabilities["available"], "workflow.capabilities.available")
    _names(capabilities["required_outcomes"], "workflow.capabilities.required_outcomes")
    _identifier(capabilities["writer"], "workflow.capabilities.writer", allow_none=True)

    risk = _object(workflow["risk"], "workflow.risk")
    _keys(risk, {"minimum", "allow_lead_escalation", "high_action"}, "workflow.risk")
    _enum(risk["minimum"], RISK_LEVELS, "workflow.risk.minimum")
    _boolean(risk["allow_lead_escalation"], "workflow.risk.allow_lead_escalation")
    _enum(risk["high_action"], {"confirm_assured", "fail"}, "workflow.risk.high_action")

    assurance = _object(workflow["assurance"], "workflow.assurance")
    _keys(assurance, {"profile", "preset", "minimum_gates"}, "workflow.assurance")
    profile = _enum(
        assurance["profile"],
        {"none", "proportional", "assured"},
        "workflow.assurance.profile",
    )
    _identifier(assurance["preset"], "workflow.assurance.preset")
    gates = _names(
        assurance["minimum_gates"], "workflow.assurance.minimum_gates", allow_empty=True
    )
    unknown_gates = sorted(set(gates) - GATES)
    if unknown_gates:
        raise WorkflowError(
            f"workflow.assurance.minimum_gates unknown values: {', '.join(unknown_gates)}"
        )
    if profile == "assured" and set(gates) != GATES:
        raise WorkflowError(
            "assured workflows require fdp2, human_build_exit, and fdp3"
        )
    if profile == "none" and gates:
        raise WorkflowError("assurance profile none cannot declare gates")

    audit = _object(workflow["audit"], "workflow.audit")
    _keys(audit, {"mode", "trust_scope", "worm_required_for"}, "workflow.audit")
    audit_mode = _enum(audit["mode"], {"signed", "worm"}, "workflow.audit.mode")
    trust_scope = _enum(
        audit["trust_scope"],
        {"local-development", "external-compliance"},
        "workflow.audit.trust_scope",
    )
    worm_for = _names(
        audit["worm_required_for"], "workflow.audit.worm_required_for", allow_empty=True
    )
    unknown_categories = sorted(set(worm_for) - WORM_CATEGORIES)
    if unknown_categories:
        raise WorkflowError(
            f"workflow.audit.worm_required_for unknown values: {', '.join(unknown_categories)}"
        )
    if audit_mode == "signed" and trust_scope != "local-development":
        raise WorkflowError("signed audit cannot claim external-compliance trust")
    if workflow["name"] == "regulated" and (
        audit_mode != "worm" or trust_scope != "external-compliance"
    ):
        raise WorkflowError(
            "regulated workflow requires worm mode and external-compliance trust"
        )
    if (
        "regulated" in worm_for
        and audit_mode == "worm"
        and trust_scope != "external-compliance"
    ):
        raise WorkflowError(
            "regulated WORM requirements need external-compliance trust"
        )

    archive = _object(workflow["archive"], "workflow.archive")
    _keys(
        archive,
        {"mode", "content_policy", "include_final_tree", "include_git_delta"},
        "workflow.archive",
    )
    if archive["mode"] != "incremental":
        raise WorkflowError("workflow.archive.mode must be incremental")
    _enum(
        archive["content_policy"],
        {"full", "redacted", "hash-only"},
        "workflow.archive.content_policy",
    )
    _boolean(archive["include_final_tree"], "workflow.archive.include_final_tree")
    _boolean(archive["include_git_delta"], "workflow.archive.include_git_delta")

    limits = _object(workflow["limits"], "workflow.limits")
    _keys(
        limits,
        {
            "deadline_seconds",
            "token_budget",
            "budget_mode",
            "delegation_credits",
            "max_active_delegations",
        },
        "workflow.limits",
    )
    _integer(
        limits["deadline_seconds"],
        "workflow.limits.deadline_seconds",
        minimum=60,
        maximum=604800,
    )
    _integer(limits["token_budget"], "workflow.limits.token_budget", minimum=0)
    _enum(limits["budget_mode"], {"soft", "hard"}, "workflow.limits.budget_mode")
    _integer(
        limits["delegation_credits"],
        "workflow.limits.delegation_credits",
        minimum=1,
        maximum=10000,
    )
    _integer(
        limits["max_active_delegations"],
        "workflow.limits.max_active_delegations",
        minimum=1,
        maximum=256,
    )
