"""Attempt accounting and repair feedback shared by every repairing owner.

Owner Cycle and Mission repair (stage 1) both bound work by a frozen
``max_attempts`` that counts the initial attempt, and both hand the next
attempt a ``checks_rejected`` feedback derived only from controller evidence.
This module is the single home of that accounting and feedback shape; each
owner keeps its own journal, events and terminal representation.
"""
from __future__ import annotations

from typing import Any, Iterable

FEEDBACK_REASONS = frozenset({"invalid_delivery", "decision_resolved", "checks_rejected"})
SCOPE_ISSUE_LIMIT = 10
FUNCTIONAL_FEEDBACK_FIELDS = ("status", "reason", "public_feedback", "revision_sha256")


def next_ordinal(started: int, max_attempts: int) -> int | None:
    """Return the ordinal of the next attempt, or ``None`` once the limit is spent."""
    ordinal = started + 1
    return ordinal if ordinal <= max_attempts else None


def attempts_exhausted(started: int, max_attempts: int) -> bool:
    return started >= max_attempts


def functional_feedback(check: dict[str, Any]) -> dict[str, Any]:
    return {field: check[field] for field in FUNCTIONAL_FEEDBACK_FIELDS if field in check}


def checks_rejected(checks_sha256: str, *, failed_requirements: Iterable[str],
                    scope_issues: list[Any], functional: dict[str, Any] | None = None,
                    unchanged_from: int | None = None) -> dict[str, Any]:
    """Feedback for a revision the controller's checks rejected.

    ``unchanged_from`` names an earlier attempt whose revision was identical;
    it is present only when given, so existing feedback bytes do not change.
    """
    return {"reason": "checks_rejected", "detail": {"checks_sha256": checks_sha256,
        "failed_requirements": list(failed_requirements),
        "scope_issues": scope_issues[:SCOPE_ISSUE_LIMIT],
        "scope_issue_count": len(scope_issues),
        **({"functional": functional} if functional is not None else {}),
        **({"unchanged_from": unchanged_from} if unchanged_from is not None else {})}}
