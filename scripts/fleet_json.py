#!/usr/bin/env python3
"""One strict, deterministic JSON contract for Fleet durable data."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable


class FleetJSONError(ValueError):
    """Input is not unambiguous UTF-8 JSON under the Fleet contract."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise FleetJSONError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_nonfinite_number(value: str) -> None:
    raise FleetJSONError(f"non-finite JSON number: {value}")


def _parse_finite_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise FleetJSONError(f"non-finite JSON number: {raw}")
    return value


def _valid_utf8(value: str, where: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise FleetJSONError(f"{where} is not valid Unicode for UTF-8") from exc


def _validate_value(value: Any, *, active: set[int] | None = None) -> None:
    """Require exact JSON-domain types and UTF-8 scalar strings recursively."""

    if active is None:
        active = set()
    value_type = type(value)
    if value is None or value_type in {bool, int}:
        return
    if value_type is float:
        if not math.isfinite(value):
            raise FleetJSONError("non-finite JSON number")
        return
    if value_type is str:
        _valid_utf8(value, "JSON string")
        return
    if value_type not in {list, dict}:
        raise FleetJSONError("value contains a non-JSON type")

    identity = id(value)
    if identity in active:
        raise FleetJSONError("value contains a circular JSON container")
    active.add(identity)
    try:
        if value_type is list:
            for item in value:
                _validate_value(item, active=active)
            return
        for key, item in value.items():
            if type(key) is not str:
                raise FleetJSONError("JSON object keys must be strings")
            _valid_utf8(key, "JSON object key")
            _validate_value(item, active=active)
    finally:
        active.remove(identity)


def _text(raw: bytes | bytearray | memoryview | str) -> str:
    if isinstance(raw, str):
        value = raw
    elif isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            value = bytes(raw).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise FleetJSONError(f"JSON is not valid UTF-8: {exc}") from exc
    else:
        raise FleetJSONError("JSON input must be bytes or text")
    if value.startswith("\ufeff"):
        raise FleetJSONError("JSON UTF-8 BOM is not allowed")
    return value


def loads(raw: bytes | bytearray | memoryview | str) -> Any:
    """Parse exactly one JSON value and reject every ambiguous extension."""

    try:
        value = json.loads(
            _text(raw),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_number,
            parse_float=_parse_finite_float,
        )
        _validate_value(value)
        return value
    except FleetJSONError:
        raise
    except (
        json.JSONDecodeError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
    ) as exc:
        raise FleetJSONError(f"invalid JSON: {exc}") from exc


def load(path: str | Path) -> Any:
    """Read a regular caller-authorized path and parse one strict JSON value.

    Security-sensitive stores must obtain bytes through ``RootedFS`` first;
    this convenience helper intentionally owns parsing, not path authority.
    """

    try:
        source = Path(path)
        raw = source.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        raise FleetJSONError(f"cannot read JSON: {exc}") from exc
    return loads(raw)


def load_jsonl(
    raw: bytes | bytearray | memoryview | str,
    *,
    require_nonempty: bool = False,
    require_final_newline: bool = True,
) -> list[Any]:
    """Parse canonical JSON Lines without silently skipping blank records."""

    if not isinstance(require_nonempty, bool) or not isinstance(
        require_final_newline, bool
    ):
        raise FleetJSONError("JSONL options must be boolean")
    value = _text(raw)
    if value and require_final_newline and not value.endswith("\n"):
        raise FleetJSONError("JSONL input lacks its final newline")
    if "\r" in value:
        raise FleetJSONError("JSONL record framing must use LF, not CR or CRLF")
    if value:
        body = value[:-1] if value.endswith("\n") else value
        lines = body.split("\n")
    else:
        lines = []
    if any(not line for line in lines):
        raise FleetJSONError("JSONL contains an empty record")
    if require_nonempty and not lines:
        raise FleetJSONError("JSONL must contain at least one record")
    result: list[Any] = []
    for number, line in enumerate(lines, start=1):
        try:
            result.append(loads(line))
        except FleetJSONError as exc:
            raise FleetJSONError(f"invalid JSONL record {number}: {exc}") from exc
    return result


def canonical_bytes(value: Any) -> bytes:
    """Return the one hashable UTF-8 representation accepted by Fleet."""

    try:
        _validate_value(value)
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except FleetJSONError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise FleetJSONError(f"value is not canonical JSON: {exc}") from exc


def canonical_jsonl(values: Iterable[Any]) -> bytes:
    if isinstance(values, (str, bytes, bytearray, memoryview, dict)):
        raise FleetJSONError("JSONL values must be an iterable of JSON records")
    try:
        return b"".join(canonical_bytes(value) + b"\n" for value in values)
    except FleetJSONError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise FleetJSONError(f"cannot encode canonical JSONL: {exc}") from exc


def sha256(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()
