#!/usr/bin/env python3
"""Deterministic monotonic risk classification for Mission Control."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "unknown": 3}
CATEGORY_RISK = {
    "repository_local": "low",
    "external_side_effect": "high",
    "production": "high",
    "money": "high",
    "credentials": "high",
    "private_data": "high",
    "destructive": "high",
    "regulated": "high",
    "unknown": "unknown",
}
PATTERNS = {
    "production": re.compile(r"\b(prod(?:uction)?|live environment|release to customers?)\b", re.I),
    "money": re.compile(r"\b(payment|charge|refund|invoice|purchase|bank|money|billing)\b", re.I),
    "credentials": re.compile(r"\b(secret|credential|api[-_ ]?key|password|token|private key)\b", re.I),
    "private_data": re.compile(r"\b(private data|personal data|customer data|pii|phi|ssn)\b", re.I),
    "destructive": re.compile(r"\b(delete|destroy|drop table|wipe|purge|force push|reset --hard)\b", re.I),
    "regulated": re.compile(r"\b(regulated|hipaa|pci(?:-dss)?|sox|gdpr|compliance retention)\b", re.I),
    "external_side_effect": re.compile(
        r"\b(deploy|publish|send (?:an )?(?:email|message)|open (?:a )?pr|push to|call external|modify cloud)\b",
        re.I,
    ),
}


class RiskError(ValueError):
    """Risk input or monotonic transition is invalid."""


def max_risk(*levels: str) -> str:
    if not levels or any(level not in RISK_ORDER for level in levels):
        raise RiskError("invalid risk level")
    return max(levels, key=RISK_ORDER.__getitem__)


def classify_categories(
    objective: str, target: str, repository_root: str | None = None
) -> list[str]:
    if not isinstance(objective, str) or not objective.strip():
        raise RiskError("objective must be non-empty")
    categories = {
        category
        for category, pattern in PATTERNS.items()
        if pattern.search(objective) or pattern.search(target)
    }
    target_path = Path(target).expanduser()
    root = Path(repository_root).expanduser().resolve() if repository_root else None
    if target_path.is_absolute() or target.startswith("."):
        try:
            resolved = target_path.resolve()
            contained = root is not None and (resolved == root or root in resolved.parents)
        except OSError:
            contained = False
        categories.add("repository_local" if contained else "external_side_effect")
    elif re.match(r"^(?:https?|ssh|s3)://|^[^/]+@[^:]+:", target):
        categories.add("external_side_effect")
    if not categories:
        categories.add("repository_local")
    return sorted(categories)


def assess(
    *,
    workflow_minimum: str,
    objective: str,
    target: str,
    repository_root: str | None = None,
    override: str = "auto",
) -> dict[str, Any]:
    if workflow_minimum not in RISK_ORDER:
        raise RiskError("invalid workflow minimum risk")
    if override != "auto" and override not in RISK_ORDER:
        raise RiskError("invalid risk override")
    categories = classify_categories(objective, target, repository_root)
    category_level = max_risk(*(CATEGORY_RISK[item] for item in categories))
    levels = [workflow_minimum, category_level]
    if override != "auto":
        levels.append(override)
    level = max_risk(*levels)
    return {
        "level": level,
        "categories": categories,
        "workflow_minimum": workflow_minimum,
        "override": override,
        "requires_confirmation": level in {"high", "unknown"},
    }


def escalate(current: str, requested: str) -> str:
    if current not in RISK_ORDER or requested not in RISK_ORDER:
        raise RiskError("invalid risk level")
    if RISK_ORDER[requested] < RISK_ORDER[current]:
        raise RiskError("risk cannot decrease")
    return requested


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow-minimum", required=True, choices=sorted(RISK_ORDER))
    parser.add_argument("--objective", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--override", default="auto", choices=("auto", *RISK_ORDER))
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                assess(
                    workflow_minimum=args.workflow_minimum,
                    objective=args.objective,
                    target=args.target,
                    repository_root=args.repository_root,
                    override=args.override,
                ),
                sort_keys=True,
            )
        )
        return 0
    except RiskError as exc:
        print(f"risk error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
