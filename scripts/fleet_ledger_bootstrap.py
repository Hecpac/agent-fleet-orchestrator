#!/usr/bin/env python3
"""Crash-safe ownership intent for one fleet lifecycle ledger bootstrap.

The lifecycle ledger must exist before any model/CMUX effect, but an arbitrary
pre-existing empty pathname must never be adopted.  This helper publishes a
durable intent *while the ledger is absent*, creates the empty ledger, and then
binds the intent to its inode.  A retry may therefore adopt only the exact
empty ledger descended from that intent.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import signal
import sys
import uuid
from typing import Any

import fleet_json
import fleet_safe_paths


FEATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SCHEMA_VERSION = 1
STATES = frozenset({"prepared", "bound", "handoff", "cleanup"})
FIELDS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "feature",
        "ledger",
        "state",
        "ledger_dev",
        "ledger_ino",
    }
)


class BootstrapError(RuntimeError):
    """A ledger bootstrap intent or its bound inode is not trustworthy."""


def _checkpoint(name: str) -> None:
    if os.environ.get("FLEET_TEST_LEDGER_BOOT_CRASH_AT") == name:
        os.kill(os.getpid(), signal.SIGKILL)


def _names(feature: str) -> tuple[str, str]:
    if type(feature) is not str or not FEATURE.fullmatch(feature):
        raise BootstrapError("invalid fleet feature for ledger bootstrap")
    return (
        f"fleet-{feature}.ledger.jsonl",
        f".fleet-{feature}.ledger-bootstrap.json",
    )


def _intent_bytes(value: dict[str, Any]) -> bytes:
    try:
        return fleet_json.canonical_bytes(value) + b"\n"
    except fleet_json.FleetJSONError as exc:
        raise BootstrapError(f"ledger bootstrap intent is not canonical: {exc}") from exc


def _decode_intent(payload: bytes, *, feature: str, ledger: str) -> dict[str, Any]:
    try:
        value = fleet_json.loads(payload)
    except fleet_json.FleetJSONError as exc:
        raise BootstrapError(f"ledger bootstrap intent is invalid: {exc}") from exc
    if (
        type(value) is not dict
        or any(type(key) is not str for key in value)
        or set(value) != FIELDS
    ):
        raise BootstrapError("ledger bootstrap intent fields are invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise BootstrapError("ledger bootstrap intent schema is unsupported")
    if value["feature"] != feature or value["ledger"] != ledger:
        raise BootstrapError("ledger bootstrap intent binding mismatch")
    if type(value["state"]) is not str or value["state"] not in STATES:
        raise BootstrapError("ledger bootstrap intent state is invalid")
    try:
        normalized_attempt = str(uuid.UUID(value["attempt_id"]))
    except (AttributeError, TypeError, ValueError) as exc:
        raise BootstrapError("ledger bootstrap attempt_id is invalid") from exc
    if type(value["attempt_id"]) is not str or value["attempt_id"] != normalized_attempt:
        raise BootstrapError("ledger bootstrap attempt_id is not canonical")
    dev = value["ledger_dev"]
    ino = value["ledger_ino"]
    if value["state"] == "prepared":
        if dev is not None or ino is not None:
            raise BootstrapError("prepared ledger bootstrap cannot claim an inode")
    elif (
        type(dev) is not int
        or type(ino) is not int
        or dev < 0
        or ino <= 0
    ):
        raise BootstrapError("bound ledger bootstrap inode is invalid")
    return value


def _read_intent(
    rooted: fleet_safe_paths.RootedFS,
    intent_leaf: str,
    *,
    feature: str,
    ledger_leaf: str,
) -> dict[str, Any] | None:
    payload = rooted.read_regular_optional(
        intent_leaf,
        directory_modes=(),
        file_mode=0o600,
        max_bytes=4096,
    )
    if payload is None:
        return None
    return _decode_intent(payload, feature=feature, ledger=ledger_leaf)


def _new_intent(feature: str, ledger_leaf: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": str(uuid.uuid4()),
        "feature": feature,
        "ledger": ledger_leaf,
        "state": "prepared",
        "ledger_dev": None,
        "ledger_ino": None,
    }


def _pending_prefix(intent_leaf: str) -> str:
    leaf_digest = hashlib.sha256(intent_leaf.encode("utf-8")).hexdigest()
    return f".fleet-atomic-{leaf_digest}-"


def _recover_prepared_pending(
    rooted: fleet_safe_paths.RootedFS,
    intent_leaf: str,
    *,
    feature: str,
    ledger_leaf: str,
) -> dict[str, Any] | None:
    """Publish one exact fsync-complete prepared intent left by atomic_write.

    The private runs root, content-addressed pending name, canonical payload,
    closed schema, feature/ledger binding, prepared state, and absent ledger are
    all required before the normal atomic writer is allowed to adopt it.  More
    than one candidate is an ambiguous provenance claim and fails closed.
    """

    prefix = _pending_prefix(intent_leaf)
    try:
        names = rooted.list_directory(Path(), directory_modes=())
    except fleet_safe_paths.SafePathError as exc:
        raise BootstrapError("cannot enumerate pending ledger bootstrap intents") from exc
    candidates = sorted(name for name in names if name.startswith(prefix))
    if not candidates:
        return None
    if len(candidates) != 1:
        raise BootstrapError("ambiguous pending ledger bootstrap intents")
    pending_leaf = candidates[0]
    if not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{64}\.tmp", pending_leaf):
        raise BootstrapError("pending ledger bootstrap name is invalid")
    try:
        payload = rooted.read_regular(
            pending_leaf,
            directory_modes=(),
            file_mode=0o600,
            max_bytes=4096,
            require_single_link=True,
        )
    except fleet_safe_paths.SafePathError as exc:
        raise BootstrapError("pending ledger bootstrap file is unsafe") from exc
    expected_leaf = prefix + hashlib.sha256(payload).hexdigest() + ".tmp"
    if pending_leaf != expected_leaf:
        raise BootstrapError("pending ledger bootstrap name/content hash mismatch")
    intent = _decode_intent(payload, feature=feature, ledger=ledger_leaf)
    if payload != _intent_bytes(intent):
        raise BootstrapError("pending ledger bootstrap intent is not canonical")
    if intent["state"] != "prepared":
        raise BootstrapError("pending ledger bootstrap intent is not prepared")
    try:
        rooted.assert_absent(ledger_leaf, directory_modes=())
        rooted.atomic_write(
            intent_leaf,
            payload,
            directory_modes=(),
            file_mode=0o600,
            require_absent=True,
        )
    except fleet_safe_paths.SafePathError as exc:
        raise BootstrapError(
            "pending ledger bootstrap cannot be safely published"
        ) from exc
    recovered = _read_intent(
        rooted,
        intent_leaf,
        feature=feature,
        ledger_leaf=ledger_leaf,
    )
    if recovered != intent:
        raise BootstrapError("recovered ledger bootstrap intent changed")
    return intent


def _replace_intent(
    rooted: fleet_safe_paths.RootedFS,
    intent_leaf: str,
    value: dict[str, Any],
) -> None:
    rooted.replace_regular(
        intent_leaf,
        _intent_bytes(value),
        directory_modes=(),
        file_mode=0o600,
    )


def _empty_ledger_info(
    rooted: fleet_safe_paths.RootedFS,
    ledger_leaf: str,
) -> os.stat_result | None:
    payload = rooted.read_regular_optional(
        ledger_leaf,
        directory_modes=(),
        file_mode=0o600,
        max_bytes=0,
    )
    if payload is None:
        return None
    if payload != b"":
        raise BootstrapError("bootstrap ledger is not byte-empty")
    return rooted.stat_regular(
        ledger_leaf,
        directory_modes=(),
        file_mode=0o600,
        require_single_link=True,
    )


def _bound_info(
    rooted: fleet_safe_paths.RootedFS,
    ledger_leaf: str,
    intent: dict[str, Any],
    *,
    require_empty: bool,
) -> os.stat_result:
    if require_empty:
        info = _empty_ledger_info(rooted, ledger_leaf)
        if info is None:
            raise BootstrapError("bound lifecycle ledger is missing")
    else:
        info = rooted.stat_regular(
            ledger_leaf,
            directory_modes=(),
            file_mode=0o600,
            require_single_link=True,
        )
    if intent["state"] != "prepared" and (
        info.st_dev != intent["ledger_dev"] or info.st_ino != intent["ledger_ino"]
    ):
        raise BootstrapError("lifecycle ledger inode does not match its durable intent")
    return info


def _bind(intent: dict[str, Any], info: os.stat_result, state: str) -> dict[str, Any]:
    return {
        **intent,
        "state": state,
        "ledger_dev": info.st_dev,
        "ledger_ino": info.st_ino,
    }


def ensure(runs_dir: Path, feature: str) -> str:
    ledger_leaf, intent_leaf = _names(feature)
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            intent = _read_intent(
                rooted,
                intent_leaf,
                feature=feature,
                ledger_leaf=ledger_leaf,
            )
            if intent is None:
                # The absence check precedes the durable intent.  Consequently,
                # a later empty ledger can be adopted only while this exact
                # controller-authored intent remains present.
                rooted.assert_absent(ledger_leaf, directory_modes=())
                intent = _recover_prepared_pending(
                    rooted,
                    intent_leaf,
                    feature=feature,
                    ledger_leaf=ledger_leaf,
                )
                if intent is None:
                    intent = _new_intent(feature, ledger_leaf)
                    rooted.atomic_write(
                        intent_leaf,
                        _intent_bytes(intent),
                        directory_modes=(),
                        file_mode=0o600,
                        require_absent=True,
                    )
                    _checkpoint("after_intent")
            elif intent["state"] == "cleanup":
                info = _empty_ledger_info(rooted, ledger_leaf)
                if info is not None:
                    if (
                        info.st_dev != intent["ledger_dev"]
                        or info.st_ino != intent["ledger_ino"]
                    ):
                        raise BootstrapError(
                            "cleanup ledger inode does not match its durable intent"
                        )
                    rooted.unlink_regular_if_content(
                        ledger_leaf,
                        b"",
                        directory_modes=(),
                        file_mode=0o600,
                    )
                rooted.unlink_regular(
                    intent_leaf,
                    directory_modes=(),
                    file_mode=0o600,
                )
                return ensure(runs_dir, feature)
            elif intent["state"] == "handoff":
                info = _empty_ledger_info(rooted, ledger_leaf)
                if info is None:
                    # The prior manifest owner completed teardown after a
                    # crash between publication and intent retirement.
                    rooted.unlink_regular(
                        intent_leaf,
                        directory_modes=(),
                        file_mode=0o600,
                    )
                    return ensure(runs_dir, feature)
                if (
                    info.st_dev != intent["ledger_dev"]
                    or info.st_ino != intent["ledger_ino"]
                ):
                    raise BootstrapError(
                        "handoff ledger inode does not match its durable intent"
                    )

            info = _empty_ledger_info(rooted, ledger_leaf)
            if info is None:
                if intent["state"] != "prepared":
                    raise BootstrapError("bound lifecycle ledger is missing")
                rooted.atomic_write(
                    ledger_leaf,
                    b"",
                    directory_modes=(),
                    file_mode=0o600,
                    require_absent=True,
                )
                _checkpoint("after_ledger_create")
                info = _empty_ledger_info(rooted, ledger_leaf)
                if info is None:  # pragma: no cover - atomic publication contract
                    raise BootstrapError("lifecycle ledger publication disappeared")
            elif intent["state"] != "prepared" and (
                info.st_dev != intent["ledger_dev"]
                or info.st_ino != intent["ledger_ino"]
            ):
                raise BootstrapError(
                    "lifecycle ledger inode does not match its durable intent"
                )
            intent = _bind(intent, info, "bound")
            _replace_intent(rooted, intent_leaf, intent)
            rooted.assert_root_binding()
            return intent["attempt_id"]
    except fleet_safe_paths.SafePathError as exc:
        raise BootstrapError(f"unsafe ledger bootstrap state: {exc}") from exc


def handoff(runs_dir: Path, feature: str) -> None:
    ledger_leaf, intent_leaf = _names(feature)
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            intent = _read_intent(
                rooted,
                intent_leaf,
                feature=feature,
                ledger_leaf=ledger_leaf,
            )
            if intent is None or intent["state"] not in {"bound", "handoff"}:
                raise BootstrapError("ledger bootstrap is not bound for handoff")
            info = _bound_info(rooted, ledger_leaf, intent, require_empty=True)
            _replace_intent(rooted, intent_leaf, _bind(intent, info, "handoff"))
    except fleet_safe_paths.SafePathError as exc:
        raise BootstrapError(f"unsafe ledger bootstrap handoff: {exc}") from exc


def cleanup(runs_dir: Path, feature: str) -> None:
    ledger_leaf, intent_leaf = _names(feature)
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            intent = _read_intent(
                rooted,
                intent_leaf,
                feature=feature,
                ledger_leaf=ledger_leaf,
            )
            if intent is None:
                raise BootstrapError("ledger bootstrap intent is missing during cleanup")
            if intent["state"] == "prepared":
                info = _empty_ledger_info(rooted, ledger_leaf)
                if info is None:
                    rooted.unlink_regular(
                        intent_leaf,
                        directory_modes=(),
                        file_mode=0o600,
                    )
                    return
            else:
                info = _bound_info(rooted, ledger_leaf, intent, require_empty=True)
            assert info is not None
            cleanup_intent = _bind(intent, info, "cleanup")
            _replace_intent(rooted, intent_leaf, cleanup_intent)
            rooted.unlink_regular_if_content(
                ledger_leaf,
                b"",
                directory_modes=(),
                file_mode=0o600,
            )
            rooted.unlink_regular(
                intent_leaf,
                directory_modes=(),
                file_mode=0o600,
            )
    except fleet_safe_paths.SafePathError as exc:
        raise BootstrapError(f"unsafe ledger bootstrap cleanup: {exc}") from exc


def complete(runs_dir: Path, feature: str) -> None:
    ledger_leaf, intent_leaf = _names(feature)
    try:
        with fleet_safe_paths.RootedFS(runs_dir) as rooted:
            intent = _read_intent(
                rooted,
                intent_leaf,
                feature=feature,
                ledger_leaf=ledger_leaf,
            )
            if intent is None or intent["state"] != "handoff":
                raise BootstrapError("ledger bootstrap handoff is not complete")
            _bound_info(rooted, ledger_leaf, intent, require_empty=False)
            rooted.unlink_regular(
                intent_leaf,
                directory_modes=(),
                file_mode=0o600,
            )
    except fleet_safe_paths.SafePathError as exc:
        raise BootstrapError(f"unsafe ledger bootstrap completion: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("ensure", "handoff", "cleanup", "complete"))
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--feature", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ensure":
            print(ensure(Path(args.runs_dir), args.feature))
        elif args.command == "handoff":
            handoff(Path(args.runs_dir), args.feature)
        elif args.command == "cleanup":
            cleanup(Path(args.runs_dir), args.feature)
        else:
            complete(Path(args.runs_dir), args.feature)
    except BootstrapError as exc:
        print(f"ledger bootstrap: {exc}", file=sys.stderr)
        return 74
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
