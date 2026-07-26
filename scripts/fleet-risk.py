#!/usr/bin/env python3
"""Deterministic monotonic risk classification for Mission Control."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any
import unicodedata


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
    "production": re.compile(
        r"\b(prod(?:uction)?|live (?:environment|database|system)|customer[- ]facing|"
        r"release to customers?|produccion|entorno productivo|ambiente productivo|"
        r"sistema en vivo|base de datos en vivo)\b",
        re.I,
    ),
    "money": re.compile(
        r"\b(payment|charge|refund|invoice|purchase|bank|money|billing|"
        r"pag(?:o|ar|a|ue|uen|ado)|cobr(?:o|ar|a|e|en|ado)|"
        r"reembolso|factura|compra|banco|dinero|facturacion)\b",
        re.I,
    ),
    "credentials": re.compile(
        r"\b(secret|credential|api[-_ ]?key|password|token|private key|secreto|"
        r"credencial(?:es)?|clave de api|contrasena|llave privada)\b",
        re.I,
    ),
    "private_data": re.compile(
        r"\b(private data|personal data|customer data|pii|phi|ssn|datos privados|"
        r"datos personales|datos de clientes?|informacion personal)\b",
        re.I,
    ),
    "destructive": re.compile(
        r"\b(delete|destroy|drop (?:the )?table|truncate|wipe|purge|erase|"
        r"remove all (?:rows|records|data)|force push|reset --hard|"
        r"borr(?:a|ar|e|en|ado|ados)|elimin(?:a|ar|e|en|ado|ados)|"
        r"destru(?:ye|ir|ya|yan|ido)|vaci(?:a|ar|e|en|ado)|"
        r"purg(?:a|ar|ue|uen|ado)|trunc(?:a|ar|que|quen|ado)|"
        r"borrar todos? los (?:registros|datos)|"
        r"eliminar todos? los (?:registros|datos))\b",
        re.I,
    ),
    "regulated": re.compile(
        r"\b(regulated|hipaa|pci(?:-dss)?|sox|gdpr|compliance retention|regulado|"
        r"regulada|retencion normativa|cumplimiento normativo)\b",
        re.I,
    ),
    "external_side_effect": re.compile(
        r"\b(deploy|publish|send (?:an )?(?:email|message)|notify (?:the )?customers?|"
        r"open (?:a )?pr|push to|call external|modify cloud|"
        r"despleg(?:ar|ado)|desplieg(?:a|ue|uen)|public(?:ar|a|e|en|ado)|"
        r"envi(?:a|ar|e|en|ado) (?:un |una )?(?:correo|email|mensaje)|"
        r"notific(?:a|ar|o|e|en|ado) (?:a |al |a los )?clientes?|"
        r"abrir (?:un )?pr|subir (?:cambios )?a|llamar (?:a un )?servicio externo|"
        r"modificar (?:la )?nube)\b",
        re.I,
    ),
}


class RiskError(ValueError):
    """Risk input or monotonic transition is invalid."""


def _normalized_text(value: str) -> str:
    """Return a case-folded, accent-insensitive string for policy matching."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(
        char for char in decomposed if not unicodedata.combining(char)
    ).casefold()


def max_risk(*levels: str) -> str:
    if not levels or any(level not in RISK_ORDER for level in levels):
        raise RiskError("invalid risk level")
    return max(levels, key=RISK_ORDER.__getitem__)


def classify_categories(
    objective: str, target: str, repository_root: str | None = None
) -> list[str]:
    if not isinstance(objective, str) or not objective.strip():
        raise RiskError("objective must be non-empty")
    normalized_objective = _normalized_text(objective)
    normalized_target = _normalized_text(target)
    categories = {
        category
        for category, pattern in PATTERNS.items()
        if pattern.search(normalized_objective) or pattern.search(normalized_target)
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
