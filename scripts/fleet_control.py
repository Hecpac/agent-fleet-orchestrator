#!/usr/bin/env python3
"""Shared Mission Control core and JSON CLI for tracked agent capabilities."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import uuid

import fleet_artifacts
import fleet_delegation
import fleet_manifest
import fleet_mission
import fleet_mission_state as mission_state
import fleet_providers
import fleet_tracking


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = ROOT / "orchestration" / "runs"


class FleetControlError(RuntimeError):
    """A Fleet Control request violates mission or runtime evidence."""


def run_process(
    command: list[str],
    *,
    runs_dir: Path,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, "FLEET_RUNS_DIR": str(runs_dir)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FleetControlError(f"command failed to run: {Path(command[0]).name}: {exc}") from exc


def json_objects(text: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return [parsed]
    for raw in text.splitlines():
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def parse_manifest(path: Path) -> dict[str, str]:
    try:
        return fleet_manifest.load(path)
    except (OSError, fleet_manifest.ManifestError) as exc:
        raise FleetControlError(f"cannot read manifest: {exc}") from exc


def _deadline(created_at: str, seconds: int) -> str:
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FleetControlError("mission creation timestamp is invalid") from exc
    return (created + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat()


class FleetControl:
    def __init__(self, runs_dir: Path, mission_id: str) -> None:
        self.runs_dir = runs_dir.resolve()
        self.mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
        self.root = mission_state.mission_root(self.runs_dir, self.mission_id)
        self.compiled = fleet_mission.validate_compiled(
            json.loads((self.root / "compiled-workflow.json").read_text(encoding="utf-8"))
        )

    def state(self) -> dict[str, Any]:
        return fleet_mission.load_state(self.runs_dir, self.mission_id)

    def events(self) -> list[dict[str, Any]]:
        return mission_state.read_events(
            mission_state.ledger_path(self.runs_dir, self.mission_id),
            expected_mission_id=self.mission_id,
        )

    def manifest(self) -> dict[str, str]:
        current = self.state()
        path = self.runs_dir / f"fleet-{current['feature']}.manifest"
        value = parse_manifest(path)
        if value.get("mission_id") != self.mission_id:
            raise FleetControlError("fleet manifest mission_id mismatch")
        return value

    def members(self) -> dict[str, dict[str, Any]]:
        resolved = self.compiled["resolved"]
        members = {item["instance_id"]: item for item in resolved["instances"]}
        if resolved.get("lead"):
            members["lead"] = resolved["lead"]
        return members

    def inspect_roster(self) -> dict[str, Any]:
        manifest = self.manifest()
        result = []
        for instance, member in sorted(self.members().items()):
            if instance not in manifest:
                raise FleetControlError(f"manifest lacks compiled instance: {instance}")
            result.append(
                {
                    **member,
                    "runner": manifest.get(f"{instance}.runner", ""),
                    "surface_uuid": manifest.get(f"{instance}.uuid", ""),
                }
            )
        return {
            "mission_id": self.mission_id,
            "feature": self.state()["feature"],
            "writer_instance": self.compiled["resolved"]["writer_instance"],
            "members": result,
        }

    @staticmethod
    def _member_capabilities(member: dict[str, Any]) -> set[str]:
        values = set(member.get("capabilities", []))
        if member.get("phase") == "RECON":
            values.add("recon")
        if member.get("phase") == "CHALLENGE":
            values.add("challenge")
        if member.get("phase") == "VERIFY":
            values.add("verify")
        if member.get("authority") == "write":
            values.add("build")
        return values

    def _legacy_events(self) -> list[dict[str, Any]]:
        feature = self.state()["feature"]
        path = self.runs_dir / f"fleet-{feature}.ledger.jsonl"
        try:
            rows = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        result: list[dict[str, Any]] = []
        for line_number, raw in enumerate(rows, 1):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise FleetControlError(f"legacy ledger corrupt at line {line_number}") from exc
            if not isinstance(value, dict):
                raise FleetControlError("legacy ledger row is not an object")
            result.append(value)
        return result

    def _reconcile_run(self, instance: str, prompt_sha256: str) -> dict[str, Any] | None:
        latest: dict[str, dict[str, Any]] = {}
        for event in self._legacy_events():
            if event.get("instance") == instance and event.get("task_sha256") == prompt_sha256:
                run_id = mission_state.normalize_uuid(str(event.get("run_id", "")), "run_id")
                latest[run_id] = event
        if len(latest) > 1:
            raise FleetControlError("multiple runs match one delegation intent")
        return next(iter(latest.values()), None)

    def _exact_legacy_run(self, instance: str, run_id: str) -> dict[str, Any]:
        matches = [
            event for event in self._legacy_events()
            if event.get("instance") == instance and event.get("run_id") == run_id
        ]
        if not matches:
            raise FleetControlError(f"missing exact run evidence: {instance}={run_id}")
        try:
            return fleet_tracking.verify_run_events(
                matches,
                required_protocol=self.manifest().get("tracking_protocol", "legacy-cmux"),
            )
        except fleet_tracking.TrackingError as exc:
            raise FleetControlError(f"tracked result provenance invalid: {exc}") from exc

    def _validate_dispatch_state(self, member: dict[str, Any]) -> None:
        status = self.state()["status"]
        if status in {"running", "assured_running"}:
            return
        if status == "awaiting_assurance_confirmation" and member.get("authority") != "write":
            return
        raise FleetControlError(f"mission status does not allow dispatch: {status}")

    def dispatch(
        self,
        *,
        recipient_instance: str,
        capability: str,
        objective: str,
        idempotency_key: str,
        parent_run_id: str | None = None,
        token_id: str | None = None,
        input_artifact_ids: list[str] | None = None,
        expected_output_contract: dict[str, Any] | None = None,
        can_delegate: bool = False,
        allowed_capabilities: list[str] | None = None,
        remaining_budget: int = 1,
    ) -> dict[str, Any]:
        if not objective.strip():
            raise FleetControlError("delegation objective must be non-empty")
        if not mission_state.SAFE_KEY.fullmatch(idempotency_key):
            raise FleetControlError("invalid delegation idempotency key")
        members = self.members()
        if recipient_instance not in members or recipient_instance == "lead":
            raise FleetControlError(f"unknown specialist instance: {recipient_instance}")
        member = members[recipient_instance]
        self._validate_dispatch_state(member)
        workflow_capabilities = set(self.compiled["workflow"]["capabilities"]["available"])
        if capability not in workflow_capabilities or capability not in self._member_capabilities(member):
            raise FleetControlError("recipient does not provide requested workflow capability")
        delegation_id = str(
            uuid.uuid5(uuid.UUID(self.mission_id), f"delegation:{idempotency_key}")
        )
        current = self.state()
        root_parent = current.get("lead_run_id")
        if parent_run_id is None:
            if root_parent is None:
                raise FleetControlError("mission has no bound Lead run")
            parent_run_id = root_parent
            depth = 1
            delegated_by = "lead"
        else:
            parent_run_id = mission_state.normalize_uuid(parent_run_id, "parent_run_id")
            if token_id is None:
                raise FleetControlError("specialist subdelegation requires a capability token")
            parent_token = fleet_delegation.validate_for_subdelegation(
                self.runs_dir,
                self.mission_id,
                token_id=token_id,
                parent_run_id=parent_run_id,
                capability=capability,
                requested_budget=remaining_budget if can_delegate else 1,
                requested_delegation_id=delegation_id,
            )
            depth = int(parent_token["current_depth"]) + 1
            delegated_by = parent_run_id
            if member.get("authority") == "write":
                raise FleetControlError("subdelegation can never grant write authority")
        writer = self.compiled["resolved"]["writer_instance"]
        if member.get("authority") == "write":
            if recipient_instance != writer or depth != 1:
                raise FleetControlError("writer authority does not match compiled unique writer")
            completed_delegations = {
                result["delegation_id"] for result in current["results"].values()
            }
            if any(
                value["recipient_instance"] == writer and key not in completed_delegations
                for key, value in current["delegations"].items()
            ):
                raise FleetControlError("a writer run is already active")
        inputs = list(input_artifact_ids or [])
        for artifact_id in inputs:
            fleet_artifacts.get_bytes(self.runs_dir, self.mission_id, artifact_id)
        output_contract = expected_output_contract or {"type": "text", "required": ["status", "evidence"]}
        if not isinstance(output_contract, dict):
            raise FleetControlError("expected_output_contract must be an object")
        try:
            mission_state.canonical_bytes(output_contract)
        except (TypeError, ValueError) as exc:
            raise FleetControlError("expected_output_contract must be canonical JSON") from exc
        existing = current["delegations"].get(delegation_id)
        if existing is not None:
            expected = {
                "recipient_instance": recipient_instance,
                "capability": capability,
                "objective_sha256": hashlib.sha256(objective.encode("utf-8")).hexdigest(),
                "parent_run_id": parent_run_id,
                "input_artifact_ids": inputs,
                "expected_output_contract": output_contract,
            }
            if any(existing[field] != value for field, value in expected.items()):
                raise FleetControlError("delegation idempotency key conflicts with another request")
            if bool(existing["token_id"]) != can_delegate:
                raise FleetControlError("delegation idempotency key changes can_delegate")
            if existing["token_id"]:
                child = fleet_delegation.load_token(
                    self.runs_dir, self.mission_id, existing["token_id"]
                )
                if child["allowed_capabilities"] != sorted(set(allowed_capabilities or workflow_capabilities)):
                    raise FleetControlError("delegation idempotency key changes child capability scope")
                if child["remaining_budget"] != remaining_budget:
                    raise FleetControlError("delegation idempotency key changes delegated budget")
            return {
                "mission_id": self.mission_id,
                "delegation_id": delegation_id,
                "run_id": existing["run_id"],
                "recipient_instance": existing["recipient_instance"],
                "token_id": existing["token_id"],
                "reused": True,
            }
        manifest = self.manifest()
        runner = manifest.get(f"{recipient_instance}.runner")
        wrapper = {
            "interactive": "fleet-send.sh",
            "local": "fleet-dispatch.sh",
        }.get(runner)
        if wrapper is None:
            raise FleetControlError(f"unsupported specialist runner: {runner}")
        assigned_run_id = (
            str(uuid.uuid5(uuid.UUID(self.mission_id), f"run:{delegation_id}"))
            if wrapper == "fleet-send.sh"
            else ""
        )
        allowed = sorted(set(allowed_capabilities or workflow_capabilities))
        if not set(allowed) <= workflow_capabilities:
            raise FleetControlError("child token capabilities exceed workflow scope")
        max_depth = int(self.compiled["workflow"]["autonomy"]["max_delegation_depth"])
        child_token: dict[str, Any] | None = None
        if can_delegate:
            if not self.compiled["workflow"]["autonomy"]["allow_subdelegation"]:
                raise FleetControlError("workflow disables subdelegation")
            if depth >= max_depth:
                raise FleetControlError("delegation depth leaves no room for subdelegation")
            child_token = fleet_delegation.issue_token(
                self.runs_dir,
                self.mission_id,
                delegation_id=delegation_id,
                parent_run_id=parent_run_id,
                can_delegate=True,
                allowed_capabilities=allowed,
                current_depth=depth,
                max_depth=max_depth,
                remaining_budget=remaining_budget,
                writer_instance=writer or "none",
                idempotency_key=f"token:{idempotency_key}",
            )
        events = self.events()
        deadline = _deadline(
            events[0]["timestamp"], int(self.compiled["workflow"]["limits"]["deadline_seconds"])
        )
        artifact_lines = [
            f"- {artifact_id}: {fleet_artifacts.artifact_path(self.runs_dir, self.mission_id, artifact_id)}"
            for artifact_id in inputs
        ] or ["- none"]
        token_line = child_token["path"] if child_token else "none"
        delegated_scope = child_token["allowed_capabilities"] if child_token else []
        prompt = (
            f"MISSION_ID={self.mission_id}\nDELEGATION_ID={delegation_id}\n"
            f"RUN_ID={assigned_run_id or 'assigned-by-runner'}\n"
            f"PARENT_RUN_ID={parent_run_id}\nCAPABILITY={capability}\nDEPTH={depth}/{max_depth}\n"
            f"CAPABILITY_TOKEN={token_line}\n"
            f"DELEGATED_CAPABILITY_CATALOG={json.dumps(delegated_scope, sort_keys=True)}\n"
            f"DEADLINE={deadline}\n\n"
            f"Subobjective:\n{objective}\n\nExact input artifacts:\n"
            + "\n".join(artifact_lines)
            + "\n\nExpected output contract:\n"
            + json.dumps(output_contract, ensure_ascii=False, sort_keys=True)
            + "\nReturn a durable result to CONTROL. Do not contact peer panes directly."
        )
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        mission_state.append_event(
            self.runs_dir,
            self.mission_id,
            kind="delegation_dispatch_intent",
            actor="CONTROL",
            idempotency_key=f"intent:{idempotency_key}",
            payload={
                "delegation_id": delegation_id,
                "recipient_instance": recipient_instance,
                "prompt_sha256": prompt_sha,
            },
        )
        legacy = self._reconcile_run(recipient_instance, prompt_sha)
        if legacy is None:
            predetermined_run_id = assigned_run_id
            if child_token and predetermined_run_id:
                # Bind the authenticated socket identity before the prompt can
                # reach a fast specialist. A failed transfer remains
                # reconcilable from the frontier ledger and this idempotent
                # binding; delegation_registered is still written only after
                # CONTROL observes the exact run.
                fleet_delegation.bind_token(
                    self.runs_dir,
                    self.mission_id,
                    token_id=child_token["token_id"],
                    delegation_id=delegation_id,
                    run_id=predetermined_run_id,
                    idempotency_key=f"token-bind:{idempotency_key}",
                )
            command = [
                str(ROOT / "scripts" / wrapper),
                self.state()["feature"],
                recipient_instance,
                prompt,
            ]
            if predetermined_run_id:
                command += ["--run-id", predetermined_run_id]
            command += ["--json"]
            result = run_process(
                command,
                runs_dir=self.runs_dir,
                timeout=120,
            )
            if result.returncode != 0:
                raise FleetControlError(result.stderr.strip() or f"{wrapper} exit={result.returncode}")
            values = json_objects(result.stdout)
            if len(values) != 1:
                raise FleetControlError(f"{wrapper} returned ambiguous JSON")
            run_id = str(values[0].get("run_id", ""))
            if predetermined_run_id and run_id != predetermined_run_id:
                raise FleetControlError("interactive runner changed CONTROL-assigned run_id")
        else:
            run_id = str(legacy["run_id"])
        run_id = mission_state.normalize_uuid(run_id, "run_id")
        if child_token:
            fleet_delegation.bind_token(
                self.runs_dir,
                self.mission_id,
                token_id=child_token["token_id"],
                delegation_id=delegation_id,
                run_id=run_id,
                idempotency_key=f"token-bind:{idempotency_key}",
            )
        payload = {
            "delegation_id": delegation_id,
            "mission_id": self.mission_id,
            "run_id": run_id,
            "parent_run_id": parent_run_id,
            "delegated_by": delegated_by,
            "recipient_instance": recipient_instance,
            "capability": capability,
            "objective_sha256": hashlib.sha256(objective.encode("utf-8")).hexdigest(),
            "input_artifact_ids": inputs,
            "expected_output_contract": output_contract,
            "deadline": deadline,
            "provider": member["provider"],
            "model": member["model"],
            "variant": member.get("variant"),
            "depth": depth,
            "token_id": child_token["token_id"] if child_token else None,
        }
        mission_state.append_event(
            self.runs_dir,
            self.mission_id,
            kind="delegation_registered",
            actor="CONTROL",
            idempotency_key=f"dispatch:{idempotency_key}",
            payload=payload,
        )
        return {
            "mission_id": self.mission_id,
            "delegation_id": delegation_id,
            "run_id": run_id,
            "recipient_instance": recipient_instance,
            "token_id": child_token["token_id"] if child_token else None,
            "token_path": child_token["path"] if child_token else None,
            "reused": legacy is not None,
        }

    def dispatch_many(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(requests, list) or not requests:
            raise FleetControlError("dispatch-many requires a non-empty request list")
        if any(not isinstance(request, dict) for request in requests):
            raise FleetControlError("each dispatch-many request must be an object")
        if len(requests) > 1 and not self.compiled["workflow"]["autonomy"]["allow_parallel"]:
            raise FleetControlError("workflow disables parallel delegation")
        results = [self.dispatch(**request) for request in requests]
        return {
            "mission_id": self.mission_id,
            "runs": results,
            "all_dispatched_before_wait": True,
        }

    def wait(self, run_ids: list[str], *, timeout_seconds: int = 1800) -> dict[str, Any]:
        if not run_ids:
            raise FleetControlError("wait requires at least one run_id")
        current = self.state()
        by_run = {value["run_id"]: value for value in current["delegations"].values()}
        requested = [mission_state.normalize_uuid(item, "run_id") for item in run_ids]
        unknown = [item for item in requested if item not in by_run]
        if unknown:
            raise FleetControlError(f"wait references unknown run_id: {unknown[0]}")
        delegations = [by_run[item] for item in requested]
        instances = [item["recipient_instance"] for item in delegations]
        if len(instances) != len(set(instances)):
            raise FleetControlError("one wait cannot contain two runs for the same instance")
        feature = current["feature"]
        manifest = self.manifest()
        command = [str(ROOT / "scripts" / "fleet-wait.sh"), feature, *instances]
        for item in delegations:
            command += ["--run", f"{item['recipient_instance']}={item['run_id']}"]
        command += ["--timeout", str(timeout_seconds), "--json"]
        result = run_process(command, runs_dir=self.runs_dir, timeout=timeout_seconds + 30)
        values = json_objects(result.stdout)
        outputs: list[dict[str, Any]] = []
        for delegation in delegations:
            run_id = delegation["run_id"]
            instance = delegation["recipient_instance"]
            value = next((item for item in values if item.get("run_id") == run_id), None)
            status_value = str((value or {}).get("status", "indeterminate"))
            output: dict[str, Any] = {
                "run_id": run_id,
                "delegation_id": delegation["delegation_id"],
                "instance": instance,
                "status": status_value,
            }
            if status_value == "succeeded":
                legacy = self._exact_legacy_run(instance, run_id)
                if legacy.get("status") != "succeeded":
                    raise FleetControlError("wait success disagrees with durable legacy ledger")
                expected = self.runs_dir / "results" / feature / f"{run_id}.txt"
                recorded = Path(str(legacy.get("result_file", "")))
                if expected.is_symlink() or recorded.is_symlink():
                    raise FleetControlError("result_file must not be a symlink")
                try:
                    if expected.resolve(strict=True) != recorded.resolve(strict=True):
                        raise FleetControlError("result_file is outside the exact fleet result store")
                except OSError as exc:
                    raise FleetControlError("result_file is missing") from exc
                artifact = fleet_artifacts.put_file(self.runs_dir, self.mission_id, expected)
                provider = str(legacy.get("provider") or delegation["provider"])
                model = str(legacy.get("model") or delegation["model"])
                variant = legacy.get("variant", delegation.get("variant"))
                try:
                    expected_provider = fleet_providers.identity(
                        delegation["provider"], delegation["model"],
                        delegation.get("variant"),
                        self.members()[instance].get("hook_source")
                        or manifest.get(f"{instance}.hook_source")
                        or fleet_providers.infer_hook_source(delegation["provider"]),
                    )
                    adapter = fleet_providers.adapter_for(expected_provider)
                    adapter.verify_identity(
                        expected_provider,
                        fleet_providers.ProviderEvidence(
                            "durable-result", provider, model, variant
                        ),
                    )
                except fleet_providers.ProviderError as exc:
                    raise FleetControlError(
                        "provider/model/variant identity drift in specialist result"
                    ) from exc
                mission_state.append_event(
                    self.runs_dir,
                    self.mission_id,
                    kind="result_recorded",
                    actor="CONTROL",
                    idempotency_key=f"result:{delegation['delegation_id']}",
                    payload={
                        "run_id": run_id,
                        "delegation_id": delegation["delegation_id"],
                        "artifact_id": artifact["artifact_id"],
                        "provider": provider,
                        "model": model,
                        "variant": variant,
                    },
                )
                output.update(artifact)
            outputs.append(output)
        return {
            "mission_id": self.mission_id,
            "status": "succeeded" if outputs and all(item["status"] == "succeeded" for item in outputs) else "incomplete",
            "results": outputs,
            "wait_exit_code": result.returncode,
        }

    def get_result(self, artifact_id: str) -> dict[str, Any]:
        content = fleet_artifacts.get_bytes(self.runs_dir, self.mission_id, artifact_id)
        return {
            "mission_id": self.mission_id,
            "artifact_id": artifact_id,
            "bytes": len(content),
            "content_base64": base64.b64encode(content).decode("ascii"),
        }

    def relay_result(
        self,
        *,
        artifact_id: str,
        recipient_instance: str,
        capability: str,
        objective: str,
        idempotency_key: str,
        parent_run_id: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any]:
        fleet_artifacts.get_bytes(self.runs_dir, self.mission_id, artifact_id)
        result = self.dispatch(
            recipient_instance=recipient_instance,
            capability=capability,
            objective=objective,
            idempotency_key=idempotency_key,
            parent_run_id=parent_run_id,
            token_id=token_id,
            input_artifact_ids=[artifact_id],
        )
        mission_state.append_event(
            self.runs_dir,
            self.mission_id,
            kind="result_relayed",
            actor="CONTROL",
            idempotency_key=f"relay:{idempotency_key}",
            payload={
                "artifact_id": artifact_id,
                "recipient_run_id": result["run_id"],
                "recipient_instance": recipient_instance,
            },
        )
        return result

    def request_human(self, *, reason: str, scope: str, idempotency_key: str) -> dict[str, Any]:
        event, appended = mission_state.append_event(
            self.runs_dir,
            self.mission_id,
            kind="human_approval_requested",
            actor="CONTROL",
            idempotency_key=idempotency_key,
            payload={"reason": reason, "scope": scope},
        )
        return {"event": event, "appended": appended}

    def request_assurance(
        self, *, risk: str, categories: list[str], reason: str, idempotency_key: str
    ) -> dict[str, Any]:
        current = self.state()
        if risk not in {"high", "unknown"}:
            raise FleetControlError("assurance risk must be high or unknown")
        if mission_state.RISK_ORDER[risk] < mission_state.RISK_ORDER[current["risk"]]:
            raise FleetControlError("assurance request cannot decrease risk")
        if risk != current["risk"]:
            mission_state.append_event(
                self.runs_dir,
                self.mission_id,
                kind="risk_escalated",
                actor="lead",
                idempotency_key=f"{idempotency_key}:risk",
                payload={"from": current["risk"], "to": risk, "categories": categories, "reason": reason},
            )
            current = self.state()
        mission_state.append_event(
            self.runs_dir,
            self.mission_id,
            kind="assurance_requested",
            actor="lead",
            idempotency_key=f"{idempotency_key}:request",
            payload={
                "risk": risk,
                "categories": sorted(set(categories)),
                "scope": current["target_repo"],
                "workflow_digest": current["workflow_digest"],
            },
        )
        return self.state()

    def cancel(self, *, run_id: str, reason: str, idempotency_key: str) -> dict[str, Any]:
        current = self.state()
        run_id = mission_state.normalize_uuid(run_id, "run_id")
        delegation = next(
            (item for item in current["delegations"].values() if item["run_id"] == run_id), None
        )
        if delegation is None:
            raise FleetControlError("cancel references unknown run")
        manifest = self.manifest()
        instance = delegation["recipient_instance"]
        runner = manifest.get(f"{instance}.runner")
        try:
            configured_provider = fleet_providers.identity(
                delegation["provider"], delegation["model"], delegation.get("variant"),
                self.members()[instance].get("hook_source")
                or manifest.get(f"{instance}.hook_source")
                or fleet_providers.infer_hook_source(delegation["provider"]),
            )
            adapter = fleet_providers.adapter_for(configured_provider)
        except fleet_providers.ProviderError as exc:
            raise FleetControlError("run provider adapter identity is invalid") from exc
        if runner == "interactive":
            result = adapter.cancel(
                lambda: run_process(
                    [str(ROOT / "scripts" / "fleet-abandon.sh"), current["feature"], instance, run_id, reason],
                    runs_dir=self.runs_dir,
                    timeout=60,
                )
            )
        elif runner == "local":
            result = adapter.cancel(
                lambda: run_process(
                    ["cmux", "send-key", "--surface", manifest[instance], "--workspace", manifest["workspace"], "ctrl-c"],
                    runs_dir=self.runs_dir,
                    timeout=10,
                )
            )
        else:
            raise FleetControlError("unsupported runner for cancel")
        if result.returncode != 0:
            raise FleetControlError(result.stderr.strip() or "cancel command failed")
        event, _ = mission_state.append_event(
            self.runs_dir,
            self.mission_id,
            kind="run_cancel_requested",
            actor="CONTROL",
            idempotency_key=idempotency_key,
            payload={"run_id": run_id, "reason": reason},
        )
        return {"run_id": run_id, "status": "cancel_requested", "event_id": event["event_id"]}

    def complete(self, *, artifact_id: str, summary: str, idempotency_key: str) -> dict[str, Any]:
        fleet_artifacts.get_bytes(self.runs_dir, self.mission_id, artifact_id)
        event, appended = mission_state.append_event(
            self.runs_dir,
            self.mission_id,
            kind="lead_completion_requested",
            actor="lead",
            idempotency_key=idempotency_key,
            payload={"artifact_id": artifact_id, "summary": summary},
        )
        return {"event": event, "appended": appended, "terminal": False}


def _load_requests(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FleetControlError(f"cannot load dispatch-many spec: {exc}") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise FleetControlError("dispatch-many spec must be a JSON array of objects")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    parser.add_argument("--mission-id", required=True)
    commands = parser.add_subparsers(dest="command", required=True)

    dispatch = commands.add_parser("dispatch")
    dispatch.add_argument("--recipient", required=True)
    dispatch.add_argument("--capability", required=True)
    dispatch.add_argument("--objective", required=True)
    dispatch.add_argument("--idempotency-key", required=True)
    dispatch.add_argument("--parent-run-id")
    dispatch.add_argument("--token-id")
    dispatch.add_argument("--input-artifact-id", action="append", default=[])
    dispatch.add_argument("--can-delegate", action="store_true")
    dispatch.add_argument("--allowed-capability", action="append", default=[])
    dispatch.add_argument("--remaining-budget", type=int, default=1)

    many = commands.add_parser("dispatch-many")
    many.add_argument("--spec-file", required=True)
    wait = commands.add_parser("wait")
    wait.add_argument("--run-id", action="append", required=True)
    wait.add_argument("--timeout", type=int, default=1800)
    get = commands.add_parser("get-result")
    get.add_argument("--artifact-id", required=True)
    relay = commands.add_parser("relay-result")
    relay.add_argument("--artifact-id", required=True)
    relay.add_argument("--recipient", required=True)
    relay.add_argument("--capability", required=True)
    relay.add_argument("--objective", required=True)
    relay.add_argument("--idempotency-key", required=True)
    relay.add_argument("--parent-run-id")
    relay.add_argument("--token-id")
    assurance = commands.add_parser("request-assurance")
    assurance.add_argument("--risk", choices=("high", "unknown"), required=True)
    assurance.add_argument("--categories", required=True)
    assurance.add_argument("--reason", required=True)
    assurance.add_argument("--idempotency-key", required=True)
    human = commands.add_parser("request-human")
    human.add_argument("--reason", required=True)
    human.add_argument("--scope", required=True)
    human.add_argument("--idempotency-key", required=True)
    commands.add_parser("inspect-roster")
    commands.add_parser("inspect-mission")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("--run-id", required=True)
    cancel.add_argument("--reason", required=True)
    cancel.add_argument("--idempotency-key", required=True)
    complete = commands.add_parser("complete")
    complete.add_argument("--artifact-id", required=True)
    complete.add_argument("--summary", required=True)
    complete.add_argument("--idempotency-key", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        control = FleetControl(Path(args.runs_dir), args.mission_id)
        if args.command == "dispatch":
            value = control.dispatch(
                recipient_instance=args.recipient,
                capability=args.capability,
                objective=args.objective,
                idempotency_key=args.idempotency_key,
                parent_run_id=args.parent_run_id,
                token_id=args.token_id,
                input_artifact_ids=args.input_artifact_id,
                can_delegate=args.can_delegate,
                allowed_capabilities=args.allowed_capability or None,
                remaining_budget=args.remaining_budget,
            )
        elif args.command == "dispatch-many":
            value = control.dispatch_many(_load_requests(Path(args.spec_file)))
        elif args.command == "wait":
            value = control.wait(args.run_id, timeout_seconds=args.timeout)
        elif args.command == "get-result":
            value = control.get_result(args.artifact_id)
        elif args.command == "relay-result":
            value = control.relay_result(
                artifact_id=args.artifact_id,
                recipient_instance=args.recipient,
                capability=args.capability,
                objective=args.objective,
                idempotency_key=args.idempotency_key,
                parent_run_id=args.parent_run_id,
                token_id=args.token_id,
            )
        elif args.command == "request-assurance":
            value = control.request_assurance(
                risk=args.risk,
                categories=[item for item in args.categories.split(",") if item],
                reason=args.reason,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "request-human":
            value = control.request_human(
                reason=args.reason, scope=args.scope, idempotency_key=args.idempotency_key
            )
        elif args.command == "inspect-roster":
            value = control.inspect_roster()
        elif args.command == "inspect-mission":
            value = control.state()
        elif args.command == "cancel":
            value = control.cancel(
                run_id=args.run_id, reason=args.reason, idempotency_key=args.idempotency_key
            )
        elif args.command == "complete":
            value = control.complete(
                artifact_id=args.artifact_id,
                summary=args.summary,
                idempotency_key=args.idempotency_key,
            )
        else:
            raise FleetControlError("unknown Fleet Control command")
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return 0
    except (
        FleetControlError,
        fleet_artifacts.ArtifactError,
        fleet_delegation.DelegationError,
        mission_state.MissionStateError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"fleet-control: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
