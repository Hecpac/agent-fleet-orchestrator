#!/usr/bin/env python3
"""Verification of CONTROL-authorized interactive run provenance."""

from __future__ import annotations

from typing import Any


class TrackingError(RuntimeError):
    """Tracked result provenance is incomplete, ambiguous, or inconsistent."""


def verify_run_events(
    events: list[dict[str, Any]],
    *,
    required_protocol: str = "legacy-cmux",
) -> dict[str, Any]:
    if not events:
        raise TrackingError("run has no durable events")
    latest = events[-1]
    interactive = any(event.get("runner") == "interactive" for event in events)
    controlled = any(event.get("tracking_protocol") == "control-v1" for event in events)
    must_control = interactive and required_protocol == "control-v1"
    if must_control and not controlled:
        raise TrackingError("interactive run lacks required control-v1 provenance")
    if not controlled:
        return latest
    if any(
        event.get("tracking_protocol") not in (None, "control-v1") for event in events
    ):
        raise TrackingError("run mixes incompatible tracking protocols")
    if latest.get("status") != "succeeded":
        return latest
    dispatched = [event for event in events if event.get("status") == "dispatched"]
    authorized = [event for event in events if event.get("status") == "authorized"]
    running = [event for event in events if event.get("status") == "running"]
    if len(dispatched) != 1 or len(authorized) != 1 or len(running) != 1:
        raise TrackingError("successful interactive run lacks unique dispatch/authorization/binding")
    approval = authorized[0]
    binding = running[0]
    for key in ("submission_event_id", "submission_boot_id", "submission_session_id"):
        if not isinstance(approval.get(key), str) or not approval[key]:
            raise TrackingError(f"submission authorization lacks {key}")
    if not isinstance(approval.get("submission_seq"), int):
        raise TrackingError("submission authorization lacks sequence")
    if any(
        (
            binding.get("binding_event_id") != approval["submission_event_id"],
            binding.get("binding_boot_id") != approval["submission_boot_id"],
            binding.get("binding_seq") != approval["submission_seq"],
            binding.get("session_id") != approval["submission_session_id"],
        )
    ):
        raise TrackingError("session binding does not match CONTROL authorization")
    if latest.get("tracking_protocol") != "control-v1":
        raise TrackingError("terminal event dropped control-v1 provenance")
    return latest
