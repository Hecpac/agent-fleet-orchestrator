#!/usr/bin/env python3
"""Inspect, resolve, or reconcile durable Mission Control decisions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import fleet_decisions
import fleet_json
import fleet_mission
import fleet_mission_state as mission_state


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = ROOT / "orchestration" / "runs"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--pending", action="store_true")
    show = commands.add_parser("show")
    show.add_argument("--decision-id", required=True)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("--decision-id", required=True)
    resolve.add_argument("--option-id", required=True)
    resolve.add_argument("--reason", required=True)
    resolve.add_argument("--idempotency-key", required=True)
    commands.add_parser("reconcile")
    return parser


def _emit_json(value: object) -> None:
    sys.stdout.buffer.write(fleet_json.canonical_bytes(value) + b"\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runs_dir = Path(args.runs_dir).expanduser().resolve()
    try:
        if args.command == "resolve":
            value = fleet_decisions.resolve_human(
                runs_dir,
                args.mission_id,
                decision_id=args.decision_id,
                option_id=args.option_id,
                reason=args.reason,
                idempotency_key=args.idempotency_key,
            )
            _emit_json(value)
            return 0
        if args.command == "reconcile":
            _emit_json(fleet_decisions.reconcile_expired(runs_dir, args.mission_id))
            return 0
        current, decisions = fleet_decisions.list_decisions(
            runs_dir,
            args.mission_id,
            pending_only=bool(getattr(args, "pending", False)),
        )
        if args.command == "show":
            decision = next(
                (
                    item
                    for item in decisions
                    if item["decision_id"] == args.decision_id
                ),
                None,
            )
            if decision is None:
                raise fleet_decisions.DecisionError(
                    f"unknown human decision: {args.decision_id}"
                )
            if args.json:
                _emit_json(decision)
            else:
                print(fleet_decisions.format_brief(current, decision))
            return 0
        if args.json:
            _emit_json(decisions)
        else:
            for decision in decisions:
                request = decision["request"]
                print(
                    f"{decision['decision_id']} {decision['status']} "
                    f"{request['impact']}/{request['risk']} "
                    f"{request['title']}"
                )
        return 0
    except (
        fleet_decisions.DecisionError,
        fleet_mission.MissionError,
        mission_state.MissionStateError,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ) as exc:
        print(f"fleet-decision: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
