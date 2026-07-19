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

import fleet_admission
import fleet_artifacts
import fleet_budget
import fleet_delegation
import fleet_decisions
import fleet_frontier
import fleet_json
import fleet_ledger
import fleet_manifest
import fleet_mission
import fleet_mission_state as mission_state
import fleet_providers
import fleet_safe_paths
import fleet_tracking
import fleet_usage


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
        raise FleetControlError(
            f"command failed to run: {Path(command[0]).name}: {exc}"
        ) from exc


def json_objects(text: str) -> list[dict[str, Any]]:
    try:
        values = fleet_json.load_jsonl(
            text, require_nonempty=True, require_final_newline=False
        )
    except fleet_json.FleetJSONError as exc:
        raise FleetControlError(f"process returned invalid strict JSON: {exc}") from exc
    if any(not isinstance(value, dict) for value in values):
        raise FleetControlError("process returned a non-object JSON record")
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


def require_usage_launch(
    runs_dir: Path,
    compiled: dict[str, Any],
    *,
    feature: str,
    provider: str,
    hook_source: str,
) -> dict[str, Any]:
    """Fail closed before one Mission-bound external model launch."""

    limits = compiled["workflow"]["limits"]
    policy = {
        "budget_mode": limits["budget_mode"],
        "token_budget": limits["token_budget"],
    }
    try:
        usage_provider = fleet_providers.DEFAULT_REGISTRY.resolve(
            hook_source=hook_source,
            provider=provider,
        ).name
        receipts = (
            fleet_budget.usage_receipts(runs_dir / f"fleet-{feature}.ledger.jsonl")
            if usage_provider == "ollama"
            else None
        )
        decision = fleet_usage.admit(policy, usage_provider, receipts)
    except (fleet_ledger.LedgerError, fleet_usage.UsageError) as exc:
        raise FleetControlError(f"mission token usage cannot be proven: {exc}") from exc
    if not decision["admitted"]:
        raise FleetControlError(
            "mission token budget blocks external launch: " + decision["reason"]
        )
    return decision


class FleetControl:
    def __init__(
        self, runs_dir: Path, mission_id: str, *, preset: str | None = None
    ) -> None:
        self.runs_dir = runs_dir.resolve()
        self.mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
        self.root = mission_state.mission_root(self.runs_dir, self.mission_id)
        try:
            self.compiled, current = fleet_mission.load_mission_compiled(
                self.runs_dir, self.mission_id, mode="effect"
            )
        except fleet_mission.MissionError as exc:
            raise FleetControlError(
                f"compiled workflow is not effect-authorized: {exc}"
            ) from exc
        resolved = self.compiled["resolved"]
        selected = preset or resolved["preset"]
        if selected not in {resolved["preset"], resolved["assurance_preset"]}:
            raise FleetControlError("Fleet Control preset is outside compiled workflow")
        self.preset = selected

    def _selected_roster(self) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        resolved = self.compiled["resolved"]
        if self.preset == resolved["preset"]:
            return resolved.get("lead"), list(resolved["instances"])
        return resolved.get("assurance_lead"), list(resolved["assurance_instances"])

    def writer_instance(self) -> str | None:
        _, instances = self._selected_roster()
        writers = [
            member["instance_id"]
            for member in instances
            if member.get("authority") == "write"
        ]
        if len(writers) > 1:
            raise FleetControlError("compiled preset contains multiple writers")
        return writers[0] if writers else None

    def state(self) -> dict[str, Any]:
        return fleet_mission.load_state(self.runs_dir, self.mission_id)

    def events(self) -> list[dict[str, Any]]:
        return mission_state.read_events(
            mission_state.ledger_path(self.runs_dir, self.mission_id),
            expected_mission_id=self.mission_id,
        )

    def manifest(self) -> dict[str, str]:
        current = self.state()
        name = f"fleet-{current['feature']}.manifest"
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                raw = rooted.read_regular(
                    Path(name),
                    directory_modes=(),
                    file_mode=0o600,
                    max_bytes=16 * 1024 * 1024,
                    require_single_link=True,
                )
                rooted.assert_root_binding()
            value = fleet_manifest.normalize(fleet_manifest.parse_bytes(raw))
        except (
            OSError,
            RuntimeError,
            fleet_manifest.ManifestError,
            fleet_safe_paths.SafePathError,
        ) as exc:
            raise FleetControlError(
                f"cannot read fleet manifest safely: {exc}"
            ) from exc
        if value.get("mission_id") != self.mission_id:
            raise FleetControlError("fleet manifest mission_id mismatch")
        if value.get("preset") != self.preset:
            raise FleetControlError(
                "fleet manifest preset does not match control roster"
            )
        try:
            fleet_manifest.verify_compiled_binding(value, self.compiled)
        except fleet_manifest.ManifestError as exc:
            raise FleetControlError(f"compiled manifest binding drift: {exc}") from exc
        return value

    def members(self) -> dict[str, dict[str, Any]]:
        lead, instances = self._selected_roster()
        members = {item["instance_id"]: item for item in instances}
        if lead:
            members[lead["instance_id"]] = lead
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
            "writer_instance": self.writer_instance(),
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
            return fleet_ledger.read_records(path)
        except fleet_ledger.LedgerError as exc:
            raise FleetControlError(
                f"legacy fleet ledger is unsafe or corrupt: {exc}"
            ) from exc

    def _reconcile_run(
        self,
        instance: str,
        expected_run_id: str,
        prompt_sha256: str,
    ) -> dict[str, Any] | None:
        lifecycle = [
            event
            for event in self._legacy_events()
            if event.get("run_id") == expected_run_id
        ]
        if not lifecycle:
            return None
        if any(
            event.get("instance") != instance
            or event.get("task_sha256") != prompt_sha256
            for event in lifecycle
        ):
            raise FleetControlError(
                "exact delegation run lifecycle differs from its task binding"
            )
        return lifecycle[-1]

    def _dispatch_prompt_sha256(self, delegation_id: str, instance: str) -> str:
        intents = [
            event
            for event in self.events()
            if event["kind"] == "delegation_dispatch_intent"
            and event["payload"].get("delegation_id") == delegation_id
            and event["payload"].get("recipient_instance") == instance
        ]
        if len(intents) != 1:
            raise FleetControlError("delegation lacks one exact dispatch intent")
        prompt_sha256 = intents[0]["payload"].get("prompt_sha256")
        if not isinstance(prompt_sha256, str) or not mission_state.SHA256.fullmatch(
            prompt_sha256
        ):
            raise FleetControlError(
                "delegation dispatch intent has invalid prompt binding"
            )
        return prompt_sha256

    def _exact_legacy_run(
        self,
        instance: str,
        run_id: str,
        *,
        prompt_sha256: str | None = None,
    ) -> dict[str, Any]:
        matches = [
            event
            for event in self._legacy_events()
            if event.get("instance") == instance and event.get("run_id") == run_id
        ]
        if not matches:
            raise FleetControlError(f"missing exact run evidence: {instance}={run_id}")
        if prompt_sha256 is not None and any(
            event.get("task_sha256") != prompt_sha256 for event in matches
        ):
            raise FleetControlError(
                "tracked run task_sha256 differs from its dispatch prompt_sha256"
            )
        try:
            return fleet_tracking.verify_run_events(
                matches,
                required_protocol=self.manifest().get(
                    "tracking_protocol", "legacy-cmux"
                ),
            )
        except fleet_tracking.TrackingError as exc:
            raise FleetControlError(
                f"tracked result provenance invalid: {exc}"
            ) from exc

    @staticmethod
    def _require_active_caller(
        current: dict[str, Any] | None,
        identity: dict[str, Any] | None,
    ) -> None:
        """Revalidate one specialist caller while holding the Mission lock."""

        if identity is None:
            return
        if current is None or identity.get("kind") != "specialist":
            raise FleetControlError("invalid specialist caller mutation guard")
        token = identity.get("token")
        if not isinstance(token, dict):
            raise FleetControlError("specialist caller mutation guard lacks token")
        admission = current.get("admissions", {}).get(token.get("admission_id"))
        delegation = current.get("delegations", {}).get(token.get("delegation_id"))
        run_id = identity.get("run_id")
        if (
            not isinstance(admission, dict)
            or admission.get("phase") != "started"
            or not admission.get("active")
            or admission.get("run_id") != run_id
            or run_id in current.get("cancelled_runs", {})
            or not isinstance(delegation, dict)
            or delegation.get("run_id") != run_id
            or delegation.get("token_id") != token.get("token_id")
            or current.get("status") in mission_state.TERMINAL_STATUSES
        ):
            raise FleetControlError(
                "specialist caller was finalized or revoked before mutation"
            )

    def _validate_dispatch_state(self, member: dict[str, Any]) -> None:
        status = self.state()["status"]
        if status in {"running", "assured_running"}:
            return
        if (
            status == "awaiting_assurance_confirmation"
            and member.get("authority") != "write"
        ):
            return
        raise FleetControlError(f"mission status does not allow dispatch: {status}")

    @staticmethod
    def _attested_artifact_ids(current: dict[str, Any]) -> set[str]:
        """Return only artifacts whose result lineage is durable in the Mission ledger."""
        artifacts = {
            str(result["artifact_id"])
            for result in current.get("results", {}).values()
            if isinstance(result, dict)
            and mission_state.SHA256.fullmatch(str(result.get("artifact_id", "")))
        }
        lead_result = current.get("lead_result")
        if isinstance(lead_result, dict) and mission_state.SHA256.fullmatch(
            str(lead_result.get("artifact_id", ""))
        ):
            artifacts.add(str(lead_result["artifact_id"]))
        return artifacts

    def _require_attested_artifacts(
        self, artifact_ids: list[str], *, current: dict[str, Any] | None = None
    ) -> None:
        attested = self._attested_artifact_ids(current or self.state())
        for artifact_id in artifact_ids:
            if artifact_id not in attested:
                raise FleetControlError("artifact is not an attested mission result")
            fleet_artifacts.get_bytes(self.runs_dir, self.mission_id, artifact_id)

    def _reconcile_expired_decisions(self) -> dict[str, Any]:
        """Normalize lazy decision-reconciliation failures at the Control boundary."""

        try:
            return fleet_decisions.reconcile_expired(self.runs_dir, self.mission_id)
        except (fleet_decisions.DecisionError, mission_state.MissionStateError) as exc:
            raise FleetControlError(f"cannot reconcile pending decisions: {exc}") from exc

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
        _caller_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._reconcile_expired_decisions()
        return self._dispatch(
            recipient_instance=recipient_instance,
            capability=capability,
            objective=objective,
            idempotency_key=idempotency_key,
            parent_run_id=parent_run_id,
            token_id=token_id,
            input_artifact_ids=input_artifact_ids,
            expected_output_contract=expected_output_contract,
            can_delegate=can_delegate,
            allowed_capabilities=allowed_capabilities,
            remaining_budget=remaining_budget,
            _preflight_only=False,
            _reserved_admission=None,
            _caller_identity=_caller_identity,
        )

    def _dispatch(
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
        _preflight_only: bool = False,
        _reserved_admission: dict[str, Any] | None = None,
        _caller_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(objective, str) or not objective.strip():
            raise FleetControlError("delegation objective must be non-empty")
        if not isinstance(idempotency_key, str) or not mission_state.SAFE_KEY.fullmatch(
            idempotency_key
        ):
            raise FleetControlError("invalid delegation idempotency key")
        if not isinstance(recipient_instance, str) or not isinstance(capability, str):
            raise FleetControlError("recipient and capability must be strings")
        if not isinstance(can_delegate, bool) or not isinstance(_preflight_only, bool):
            raise FleetControlError("delegation flags must be boolean")
        if _caller_identity is not None and (
            _caller_identity.get("kind") != "specialist"
            or parent_run_id != _caller_identity.get("run_id")
            or token_id
            != (
                _caller_identity.get("token", {}).get("token_id")
                if isinstance(_caller_identity.get("token"), dict)
                else None
            )
        ):
            raise FleetControlError(
                "specialist dispatch does not match its guarded caller identity"
            )
        if (
            isinstance(remaining_budget, bool)
            or not isinstance(remaining_budget, int)
            or remaining_budget < 0
        ):
            raise FleetControlError("remaining_budget must be a non-negative integer")
        raw_inputs = [] if input_artifact_ids is None else input_artifact_ids
        if not isinstance(raw_inputs, list) or any(
            not isinstance(item, str) or not mission_state.SHA256.fullmatch(item)
            for item in raw_inputs
        ):
            raise FleetControlError("input_artifact_ids must be a SHA-256 list")
        if len(raw_inputs) != len(set(raw_inputs)):
            raise FleetControlError("input_artifact_ids must be unique")
        inputs = sorted(raw_inputs)
        raw_allowed = [] if allowed_capabilities is None else allowed_capabilities
        if not isinstance(raw_allowed, list) or any(
            not isinstance(item, str) or not item for item in raw_allowed
        ):
            raise FleetControlError("allowed_capabilities must be a string list")
        if len(raw_allowed) != len(set(raw_allowed)):
            raise FleetControlError("allowed_capabilities must be unique")
        if not can_delegate and raw_allowed:
            raise FleetControlError(
                "non-delegating child cannot receive child capability scope"
            )
        if can_delegate and remaining_budget < 1:
            raise FleetControlError(
                "delegating child requires positive remaining_budget"
            )
        members = self.members()
        if recipient_instance not in members or recipient_instance == "lead":
            raise FleetControlError(
                f"unknown specialist instance: {recipient_instance}"
            )
        member = members[recipient_instance]
        self._validate_dispatch_state(member)
        workflow_capabilities = set(
            self.compiled["workflow"]["capabilities"]["available"]
        )
        if (
            capability not in workflow_capabilities
            or capability not in self._member_capabilities(member)
        ):
            raise FleetControlError(
                "recipient does not provide requested workflow capability"
            )
        allowed = sorted(raw_allowed) if can_delegate else []
        delegated_budget = remaining_budget if can_delegate else 0
        identities = fleet_admission.deterministic_ids(
            self.mission_id,
            request_key=idempotency_key,
            run_kind="specialist",
        )
        delegation_id = str(identities["delegation_id"])
        assigned_run_id = str(identities["run_id"])
        current = self.state()
        root_parent = current.get("lead_run_id")
        parent_admission_id: str | None = None
        parent_token: dict[str, Any] | None = None
        specialist_parent = parent_run_id is not None
        if parent_run_id is None:
            if root_parent is None:
                raise FleetControlError("mission has no bound Lead run")
            parent_run_id = root_parent
            parent_admission_id = current.get("lead_admission_id")
            if parent_admission_id is None:
                raise FleetControlError("mission Lead has no active admission")
            # The Lead-to-specialist edge establishes the root specialist
            # token. Delegation depth counts only specialist-to-specialist
            # hops, so workflows with max_depth=0 can still dispatch leaves.
            depth = 0
            delegated_by = "lead"
            if can_delegate and allowed_capabilities is None:
                allowed = sorted(workflow_capabilities)
            self._require_attested_artifacts(inputs, current=current)
        else:
            parent_run_id = mission_state.normalize_uuid(parent_run_id, "parent_run_id")
            if token_id is None:
                raise FleetControlError(
                    "specialist subdelegation requires a capability token"
                )
            presented_token = fleet_delegation.load_token(
                self.runs_dir, self.mission_id, token_id
            )
            if can_delegate and allowed_capabilities is None:
                allowed = list(presented_token["allowed_capabilities"])
            parent_token = presented_token
            try:
                parent_token = fleet_delegation.validate_admission_token(
                    self.runs_dir,
                    self.mission_id,
                    token_id=token_id,
                    delegation_id=presented_token["delegation_id"],
                    run_id=parent_run_id,
                    require_started=True,
                )
            except fleet_delegation.DelegationError as exc:
                raise FleetControlError(
                    "specialist parent requires a live v3 admission token"
                ) from exc
            parent_admission_id = parent_token["admission_id"]
            depth = int(presented_token["current_depth"]) + 1
            delegated_by = parent_run_id
            if member.get("authority") == "write":
                raise FleetControlError("subdelegation can never grant write authority")
        if not set(allowed) <= workflow_capabilities:
            raise FleetControlError("child token capabilities exceed workflow scope")
        writer = self.writer_instance()
        if member.get("authority") == "write":
            if recipient_instance != writer or depth != 0:
                raise FleetControlError(
                    "writer authority does not match compiled unique writer"
                )
            completed_delegations = {
                result["delegation_id"] for result in current["results"].values()
            }
            if any(
                value["recipient_instance"] == writer
                and key not in completed_delegations
                for key, value in current["delegations"].items()
            ):
                raise FleetControlError("a writer run is already active")
        output_contract = (
            {"type": "text", "required": ["status", "evidence"]}
            if expected_output_contract is None
            else expected_output_contract
        )
        if not isinstance(output_contract, dict):
            raise FleetControlError("expected_output_contract must be an object")
        try:
            mission_state.canonical_bytes(output_contract)
        except (TypeError, ValueError) as exc:
            raise FleetControlError(
                "expected_output_contract must be canonical JSON"
            ) from exc
        manifest = self.manifest()
        runner = manifest.get(f"{recipient_instance}.runner")
        wrapper = {
            "interactive": "fleet-send.sh",
            "local": "fleet-dispatch.sh",
        }.get(runner)
        if wrapper is None:
            raise FleetControlError(f"unsupported specialist runner: {runner}")
        if wrapper == "fleet-dispatch.sh" and inputs:
            raise FleetControlError(
                "local specialist inputs require an authenticated artifact client"
            )
        max_depth = int(self.compiled["workflow"]["autonomy"]["max_delegation_depth"])
        if can_delegate:
            if not self.compiled["workflow"]["autonomy"]["allow_subdelegation"]:
                raise FleetControlError("workflow disables subdelegation")
            if depth >= max_depth:
                raise FleetControlError(
                    "delegation depth leaves no room for subdelegation"
                )
        if specialist_parent:
            assert parent_token is not None and token_id is not None
            if parent_token["writer_instance"] != (writer or "none"):
                raise FleetControlError(
                    "parent token writer scope does not match control roster"
                )
            parent_token = fleet_delegation.validate_for_subdelegation(
                self.runs_dir,
                self.mission_id,
                token_id=token_id,
                delegation_id=parent_token["delegation_id"],
                parent_run_id=parent_run_id,
                capability=capability,
                requested_budget=delegated_budget,
                requested_delegation_id=delegation_id,
                child_can_delegate=can_delegate,
                child_allowed_capabilities=allowed,
                requested_artifact_ids=inputs,
                reserve_budget=False,
            )
            for artifact_id in inputs:
                fleet_artifacts.get_bytes(self.runs_dir, self.mission_id, artifact_id)
        events = self.events()
        deadline = _deadline(
            events[0]["timestamp"],
            int(self.compiled["workflow"]["limits"]["deadline_seconds"]),
        )
        token_id_line = str(
            uuid.uuid5(
                uuid.UUID(self.mission_id),
                f"capability-token:{delegation_id}",
            )
        )
        caller_run_id = assigned_run_id
        delegated_scope = allowed if can_delegate else []
        artifact_lines = [f"- {artifact_id}" for artifact_id in inputs] or ["- none"]
        prompt = (
            f"MISSION_ID={self.mission_id}\nDELEGATION_ID={delegation_id}\n"
            f"RUN_ID={caller_run_id}\n"
            f"PARENT_RUN_ID={parent_run_id}\nCAPABILITY={capability}\nDEPTH={depth}/{max_depth}\n"
            f"CAPABILITY_TOKEN_ID={token_id_line}\n"
            f"DELEGATED_CAPABILITY_CATALOG={json.dumps(delegated_scope, sort_keys=True)}\n"
            f"DEADLINE={deadline}\n\n"
            f"Subobjective:\n{objective}\n\nExact input artifacts:\n"
            + "\n".join(artifact_lines)
            + "\nUse only the authenticated fleet_control MCP server for CONTROL tools."
            + "\nFor every fleet_control tool call include "
            + f"_caller_run_id={caller_run_id} and _caller_token_id={token_id_line}."
            + "\nRetrieve artifact bytes only with fleet_control.get_result; never read the socket, token, or CAS paths directly."
            + "\n\nExpected output contract:\n"
            + json.dumps(output_contract, ensure_ascii=False, sort_keys=True)
            + "\nReturn a durable result to CONTROL. Do not contact peer panes directly."
        )
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        hook_source = (
            member.get("hook_source")
            or manifest.get(f"{recipient_instance}.hook_source")
            or fleet_providers.infer_hook_source(member["provider"])
        )
        try:
            provider_identity = fleet_providers.identity(
                member["provider"],
                member["model"],
                member.get("variant"),
                hook_source,
            )
        except fleet_providers.ProviderError as exc:
            raise FleetControlError(
                "specialist provider identity is invalid before admission"
            ) from exc
        effect_sha256 = mission_state.sha256(
            {
                "schema_version": 1,
                "objective_sha256": hashlib.sha256(
                    objective.encode("utf-8")
                ).hexdigest(),
                "prompt_sha256": prompt_sha,
                "input_artifact_ids": inputs,
                "expected_output_contract": output_contract,
                "grants": {
                    "capability": capability,
                    "token_id": token_id_line,
                    "parent_run_id": parent_run_id,
                    "can_delegate": can_delegate,
                    "allowed_capabilities": delegated_scope,
                    "allowed_artifact_ids": inputs,
                    "remaining_budget": delegated_budget,
                    "depth": depth,
                    "max_depth": max_depth,
                    "writer_instance": writer or "none",
                },
                "provider_identity": {
                    "provider": provider_identity.provider,
                    "model": provider_identity.model,
                    "variant": provider_identity.variant,
                    "hook_source": provider_identity.hook_source,
                },
                "runner": runner,
                "wrapper": wrapper,
            }
        )
        admission_request = {
            "request_key": idempotency_key,
            "run_kind": "specialist",
            "recipient_instance": recipient_instance,
            "capability": capability,
            "parent_admission_id": parent_admission_id,
            "parent_run_id": parent_run_id,
            "delegated_budget": delegated_budget,
            "writer": member.get("authority") == "write",
            "effect_sha256": effect_sha256,
            "task_sha256": prompt_sha,
        }
        existing = current["delegations"].get(delegation_id)
        if existing is not None:
            expected = {
                "recipient_instance": recipient_instance,
                "capability": capability,
                "objective_sha256": hashlib.sha256(
                    objective.encode("utf-8")
                ).hexdigest(),
                "parent_run_id": parent_run_id,
                "input_artifact_ids": inputs,
                "expected_output_contract": output_contract,
            }
            if any(existing[field] != value for field, value in expected.items()):
                raise FleetControlError(
                    "delegation idempotency key conflicts with another request"
                )
            if not existing["token_id"]:
                raise FleetControlError(
                    "registered delegation lacks its universal capability token"
                )
            child = fleet_delegation.validate_bound_token(
                self.runs_dir,
                self.mission_id,
                token_id=existing["token_id"],
                delegation_id=delegation_id,
                run_id=existing["run_id"],
                require_v3=True,
            )
            durable = current["admissions"].get(child["admission_id"])
            if (
                durable is None
                or durable["delegation_id"] != delegation_id
                or durable["run_id"] != existing["run_id"]
                or durable["effect_sha256"] != effect_sha256
                or durable["phase"] not in {"started", "finalized"}
            ):
                raise FleetControlError(
                    "registered delegation lacks exact admission ownership"
                )
            if child["can_delegate"] != can_delegate:
                raise FleetControlError(
                    "delegation idempotency key changes can_delegate"
                )
            if child["allowed_capabilities"] != allowed:
                raise FleetControlError(
                    "delegation idempotency key changes child capability scope"
                )
            if child["remaining_budget"] != delegated_budget:
                raise FleetControlError(
                    "delegation idempotency key changes delegated budget"
                )
            if child["allowed_artifact_ids"] != inputs:
                raise FleetControlError(
                    "delegation idempotency key changes child artifact scope"
                )
            reused = {
                "mission_id": self.mission_id,
                "delegation_id": delegation_id,
                "run_id": existing["run_id"],
                "recipient_instance": existing["recipient_instance"],
                "token_id": existing["token_id"],
                "reused": True,
            }
            if _preflight_only:
                return {
                    **reused,
                    "admission_request": admission_request,
                    "preflight": True,
                }
            return reused
        if _preflight_only:
            return {
                "mission_id": self.mission_id,
                "delegation_id": delegation_id,
                "recipient_instance": recipient_instance,
                "admission_request": admission_request,
                "preflight": True,
            }
        if _reserved_admission is None:
            reserved = fleet_admission.reserve_many(
                self.runs_dir,
                self.mission_id,
                requests=[admission_request],
                idempotency_key=f"admission:reserve:{idempotency_key}",
            )
            durable_admission = reserved["admissions"][0]
        else:
            durable_admission = _reserved_admission
            if any(
                durable_admission.get(field) != value
                for field, value in admission_request.items()
            ):
                raise FleetControlError(
                    "reserved admission differs from dispatch request"
                )
        if (
            durable_admission["admission_id"] != identities["admission_id"]
            or durable_admission["delegation_id"] != delegation_id
            or durable_admission["run_id"] != assigned_run_id
        ):
            raise FleetControlError("deterministic admission identity drift")
        commit_id = str(
            uuid.uuid5(
                uuid.UUID(self.mission_id),
                f"commit:{durable_admission['admission_id']}",
            )
        )
        child_token = fleet_delegation.issue_token(
            self.runs_dir,
            self.mission_id,
            delegation_id=delegation_id,
            parent_run_id=parent_run_id,
            can_delegate=can_delegate,
            allowed_capabilities=allowed,
            allowed_artifact_ids=inputs,
            current_depth=depth,
            max_depth=max_depth,
            remaining_budget=delegated_budget,
            writer_instance=writer or "none",
            idempotency_key=f"token:{idempotency_key}",
            run_id=assigned_run_id,
            admission_id=durable_admission["admission_id"],
            reservation_event_sha256=durable_admission["reservation_event_sha256"],
            commit_id=commit_id,
            request_digest=durable_admission["request_digest"],
        )
        if child_token["token_id"] != token_id_line:
            raise FleetControlError("capability token deterministic identity drift")
        # Publish every durable identity and authorization binding before the
        # commit.  The commit itself must then be the exact ledger head at the
        # moment CONTROL verifies and performs the wrapper side effect.
        fleet_delegation.bind_token(
            self.runs_dir,
            self.mission_id,
            token_id=child_token["token_id"],
            delegation_id=delegation_id,
            run_id=assigned_run_id,
            idempotency_key=f"token-bind:{idempotency_key}",
        )
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

        current = self.state()
        durable = current["admissions"].get(durable_admission["admission_id"])
        if durable is None:
            raise FleetControlError("reserved admission disappeared before commit")
        if durable["phase"] == "reserved":
            commit = fleet_admission.commit(
                self.runs_dir,
                self.mission_id,
                admission_id=durable["admission_id"],
                request_digest=durable["request_digest"],
                effect_sha256=durable["effect_sha256"],
                recipient_instance=recipient_instance,
                writer=bool(durable["writer"]),
                run_id=assigned_run_id,
                idempotency_key=f"admission:commit:{idempotency_key}",
            )
            commit_event_sha256 = commit["commit_event_sha256"]
        elif durable["phase"] in {"committed", "authorized", "started"}:
            durable_commit = durable.get("commit")
            if durable_commit is None:
                raise FleetControlError("committed admission lacks its durable proof")
            commit_event_sha256 = durable_commit["event_sha256"]
        else:
            raise FleetControlError(
                f"admission cannot launch from terminal phase {durable['phase']}"
            )

        legacy = self._reconcile_run(
            recipient_instance,
            assigned_run_id,
            prompt_sha,
        )
        if legacy is None:
            if durable["phase"] == "started":
                raise FleetControlError(
                    "started admission has no reconcilable external effect; refusing relaunch"
                )
            if mission_state.sha256(self.manifest()) != mission_state.sha256(manifest):
                raise FleetControlError(
                    "fleet manifest changed between dispatch preflight and launch"
                )
            require_usage_launch(
                self.runs_dir,
                self.compiled,
                feature=current["feature"],
                provider=member["provider"],
                hook_source=hook_source,
            )
            command = [
                str(ROOT / "scripts" / wrapper),
                current["feature"],
                recipient_instance,
                prompt,
                "--run-id",
                assigned_run_id,
                "--json",
            ]
            try:
                authorization = fleet_admission.authorize_launch(
                    self.runs_dir,
                    self.mission_id,
                    admission_id=durable["admission_id"],
                    commit_event_sha256=commit_event_sha256,
                    request_digest=durable["request_digest"],
                    effect_sha256=durable["effect_sha256"],
                    recipient_instance=recipient_instance,
                    writer=bool(durable["writer"]),
                    run_id=assigned_run_id,
                    approval_event_sha256=None,
                    idempotency_key=f"admission:authorize:{idempotency_key}",
                )
            except mission_state.MissionStateError as exc:
                raise FleetControlError(
                    "delegation launch authorization was denied before effect"
                ) from exc
            authorization_event_sha256 = authorization["authorization_event_sha256"]
            # No fallible mission operation belongs between authorization and
            # the wrapper boundary.  This call is the externally visible effect.
            result = run_process(
                command,
                runs_dir=self.runs_dir,
                timeout=120,
            )
            if result.returncode != 0:
                raise FleetControlError(
                    result.stderr.strip() or f"{wrapper} exit={result.returncode}"
                )
            values = json_objects(result.stdout)
            if len(values) != 1:
                raise FleetControlError(f"{wrapper} returned ambiguous JSON")
            run_id = str(values[0].get("run_id", ""))
            if run_id != assigned_run_id:
                raise FleetControlError("runner changed CONTROL-assigned run_id")
        else:
            run_id = str(legacy["run_id"])
            authorization_proof = durable.get("launch_authorization")
            if not isinstance(authorization_proof, dict):
                raise FleetControlError(
                    "reconciled effect lacks durable launch authorization"
                )
            authorization_event_sha256 = authorization_proof["event_sha256"]
        run_id = mission_state.normalize_uuid(run_id, "run_id")
        if run_id != assigned_run_id:
            raise FleetControlError(
                "reconciled effect has a different deterministic run_id"
            )
        current_after_effect = self.state()
        started = current_after_effect["admissions"].get(durable["admission_id"])
        if started is None:
            raise FleetControlError("accepted effect lost its admission ownership")
        if started["phase"] == "authorized":
            try:
                fleet_admission.mark_started(
                    self.runs_dir,
                    self.mission_id,
                    admission_id=started["admission_id"],
                    authorization_event_sha256=authorization_event_sha256,
                    request_digest=started["request_digest"],
                    effect_sha256=started["effect_sha256"],
                    recipient_instance=recipient_instance,
                    writer=bool(started["writer"]),
                    run_id=assigned_run_id,
                    idempotency_key=f"admission:start:{idempotency_key}",
                )
            except mission_state.MissionStateError as exc:
                raise FleetControlError(
                    "wrapper accepted the exact run but durable start failed; "
                    "the authorization remains active for exact reconciliation"
                ) from exc
        elif started["phase"] != "started":
            raise FleetControlError(
                f"accepted effect has terminal admission phase {started['phase']}"
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
            "token_id": child_token["token_id"],
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
            "token_id": child_token["token_id"],
            "token_path": child_token["path"],
            "reused": legacy is not None,
        }

    def dispatch_many(
        self,
        requests: list[dict[str, Any]],
        *,
        _caller_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._reconcile_expired_decisions()
        if not isinstance(requests, list) or not requests:
            raise FleetControlError("dispatch-many requires a non-empty request list")
        if any(not isinstance(request, dict) for request in requests):
            raise FleetControlError("each dispatch-many request must be an object")
        allowed_fields = {
            "recipient_instance",
            "capability",
            "objective",
            "idempotency_key",
            "parent_run_id",
            "token_id",
            "input_artifact_ids",
            "expected_output_contract",
            "can_delegate",
            "allowed_capabilities",
            "remaining_budget",
        }
        if any(set(request) - allowed_fields for request in requests):
            raise FleetControlError("dispatch-many request contains unknown fields")
        if (
            len(requests) > 1
            and not self.compiled["workflow"]["autonomy"]["allow_parallel"]
        ):
            raise FleetControlError("workflow disables parallel delegation")
        idempotency_keys = [request.get("idempotency_key") for request in requests]
        if any(not isinstance(value, str) for value in idempotency_keys):
            raise FleetControlError("dispatch-many idempotency keys must be strings")
        if len(idempotency_keys) != len(set(idempotency_keys)):
            raise FleetControlError("dispatch-many idempotency keys must be unique")
        recipients = [request.get("recipient_instance") for request in requests]
        if any(not isinstance(value, str) for value in recipients):
            raise FleetControlError(
                "dispatch-many recipient_instance values must be strings"
            )
        if len(recipients) != len(set(recipients)):
            raise FleetControlError(
                "dispatch-many recipient_instance values must be unique"
            )
        # Every static check completes before one atomic admission reservation.
        # Individual dispatches then repeat their live checks, but cannot debit
        # or claim resources independently from the original batch.
        plans = [
            self._dispatch(
                **request,
                _preflight_only=True,
                _caller_identity=_caller_identity,
            )
            for request in requests
        ]
        admission_requests = [plan["admission_request"] for plan in plans]
        batch_digest = mission_state.sha256(
            sorted(admission_requests, key=lambda item: item["request_key"])
        )
        try:
            reserved = fleet_admission.reserve_many(
                self.runs_dir,
                self.mission_id,
                requests=admission_requests,
                idempotency_key=f"admission:reserve-many:{batch_digest}",
            )
        except mission_state.MissionStateError as exc:
            raise FleetControlError(
                f"atomic dispatch-many admission failed: {exc}"
            ) from exc
        by_request = {item["request_key"]: item for item in reserved["admissions"]}
        results = [
            self._dispatch(
                **request,
                _preflight_only=False,
                _reserved_admission=by_request[str(request["idempotency_key"])],
                _caller_identity=_caller_identity,
            )
            for request in requests
        ]
        return {
            "mission_id": self.mission_id,
            "runs": results,
            "all_dispatched_before_wait": True,
        }

    def wait(
        self,
        run_ids: list[str],
        *,
        timeout_seconds: int = 1800,
        _caller_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not run_ids:
            raise FleetControlError("wait requires at least one run_id")
        current = self.state()
        by_run = {value["run_id"]: value for value in current["delegations"].values()}
        requested = [mission_state.normalize_uuid(item, "run_id") for item in run_ids]
        unknown = [item for item in requested if item not in by_run]
        if unknown:
            raise FleetControlError(f"wait references unknown run_id: {unknown[0]}")
        delegations = [by_run[item] for item in requested]
        admission_by_run: dict[str, dict[str, Any]] = {}
        for item in delegations:
            owner = current["run_owners"].get(item["run_id"])
            if owner is None or owner.get("owner_kind") != "admission":
                raise FleetControlError("delegation wait lacks admission ownership")
            admission = current["admissions"].get(owner.get("owner_id"))
            if admission is None or admission["delegation_id"] != item["delegation_id"]:
                raise FleetControlError("delegation wait admission binding is invalid")
            admission_by_run[item["run_id"]] = admission
        instances = [item["recipient_instance"] for item in delegations]
        if len(instances) != len(set(instances)):
            raise FleetControlError(
                "one wait cannot contain two runs for the same instance"
            )
        feature = current["feature"]
        manifest = self.manifest()
        command = [str(ROOT / "scripts" / "fleet-wait.sh"), feature, *instances]
        for item in delegations:
            command += ["--run", f"{item['recipient_instance']}={item['run_id']}"]
        command += ["--timeout", str(timeout_seconds), "--json"]
        result = run_process(
            command, runs_dir=self.runs_dir, timeout=timeout_seconds + 30
        )
        values = json_objects(result.stdout)
        outputs: list[dict[str, Any]] = []
        for delegation in delegations:
            run_id = delegation["run_id"]
            instance = delegation["recipient_instance"]
            prompt_sha256 = self._dispatch_prompt_sha256(
                delegation["delegation_id"], instance
            )
            value = next(
                (item for item in values if item.get("run_id") == run_id), None
            )
            status_value = str((value or {}).get("status", "indeterminate"))
            output: dict[str, Any] = {
                "run_id": run_id,
                "delegation_id": delegation["delegation_id"],
                "instance": instance,
                "status": status_value,
            }
            if status_value == "succeeded":
                legacy = self._exact_legacy_run(
                    instance, run_id, prompt_sha256=prompt_sha256
                )
                if legacy.get("status") != "succeeded":
                    raise FleetControlError(
                        "wait success disagrees with durable legacy ledger"
                    )
                recorded = Path(str(legacy.get("result_file", "")))
                try:
                    content = fleet_frontier.read_frontier_result(
                        self.runs_dir,
                        feature=feature,
                        run_id=run_id,
                        recorded_path=recorded,
                    )
                except fleet_frontier.FrontierError as exc:
                    raise FleetControlError(str(exc)) from exc
                provider = legacy.get("provider")
                model = legacy.get("model")
                variant = legacy.get("variant")
                if not isinstance(provider, str) or not provider:
                    raise FleetControlError("specialist result lacks provider evidence")
                if not isinstance(model, str) or not model:
                    raise FleetControlError("specialist result lacks model evidence")
                try:
                    expected_provider = fleet_providers.identity(
                        delegation["provider"],
                        delegation["model"],
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
                # Caller revocation and result publication linearize on the
                # Mission lock.  CAS publication happens only after the caller
                # is proven active under that same lock; while the child remains
                # active, parent finalization is forbidden by admission state.
                with mission_state.MissionTransaction(
                    self.runs_dir, self.mission_id
                ) as transaction:
                    self._require_active_caller(
                        transaction.current_state, _caller_identity
                    )
                    artifact = fleet_artifacts.put_bytes(
                        self.runs_dir, self.mission_id, content
                    )
                    transaction.append_event(
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
            if status_value in mission_state.TERMINAL_STATUSES:
                terminal = self._exact_legacy_run(
                    instance, run_id, prompt_sha256=prompt_sha256
                )
                terminal_status = str(terminal.get("status", ""))
                if terminal_status != status_value:
                    raise FleetControlError(
                        "wait terminal status disagrees with durable run evidence"
                    )
                with mission_state.MissionTransaction(
                    self.runs_dir, self.mission_id
                ) as transaction:
                    self._require_active_caller(
                        transaction.current_state, _caller_identity
                    )
                fleet_admission.finalize(
                    self.runs_dir,
                    self.mission_id,
                    admission_id=admission_by_run[run_id]["admission_id"],
                    recipient_instance=instance,
                    writer=bool(admission_by_run[run_id]["writer"]),
                    terminal_evidence={
                        "schema_version": 1,
                        "source_event_sha256": mission_state.sha256(terminal),
                        "run_id": run_id,
                        "task_sha256": prompt_sha256,
                        "status": status_value,
                    },
                    reason=(
                        "specialist result attested"
                        if status_value == "succeeded"
                        else f"durable specialist terminal status: {status_value}"
                    ),
                    idempotency_key=f"admission:finalize:{delegation['delegation_id']}",
                )
            outputs.append(output)
        return {
            "mission_id": self.mission_id,
            "status": "succeeded"
            if outputs and all(item["status"] == "succeeded" for item in outputs)
            else "incomplete",
            "results": outputs,
            "wait_exit_code": result.returncode,
        }

    def get_result(self, artifact_id: str) -> dict[str, Any]:
        self._require_attested_artifacts([artifact_id])
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
        _caller_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_attested_artifacts([artifact_id])
        result = self.dispatch(
            recipient_instance=recipient_instance,
            capability=capability,
            objective=objective,
            idempotency_key=idempotency_key,
            parent_run_id=parent_run_id,
            token_id=token_id,
            input_artifact_ids=[artifact_id],
            _caller_identity=_caller_identity,
        )
        with mission_state.MissionTransaction(
            self.runs_dir, self.mission_id
        ) as transaction:
            self._require_active_caller(transaction.current_state, _caller_identity)
            transaction.append_event(
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

    def request_human(
        self,
        *,
        reason: str,
        scope: str,
        idempotency_key: str,
        _caller_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with mission_state.MissionTransaction(
            self.runs_dir, self.mission_id
        ) as transaction:
            self._require_active_caller(transaction.current_state, _caller_identity)
            event, appended = transaction.append_event(
                kind="human_approval_requested",
                actor="CONTROL",
                idempotency_key=idempotency_key,
                payload={"reason": reason, "scope": scope},
            )
        return {"event": event, "appended": appended}

    @staticmethod
    def _decision_evidence_binding(
        current: dict[str, Any], artifact_id: str
    ) -> dict[str, Any]:
        matches = [
            (delegation_id, result)
            for delegation_id, result in current.get("results", {}).items()
            if isinstance(result, dict) and result.get("artifact_id") == artifact_id
        ]
        if len(matches) != 1:
            raise FleetControlError(
                "decision evidence must identify exactly one specialist result"
            )
        delegation_id, result = matches[0]
        delegation = current.get("delegations", {}).get(delegation_id)
        if not isinstance(delegation, dict):
            raise FleetControlError("decision evidence lacks delegation lineage")
        return {
            "artifact_id": artifact_id,
            "delegation_id": delegation_id,
            "instance": delegation["recipient_instance"],
            "provider": result["provider"],
            "model": result["model"],
            "variant": result.get("variant"),
        }

    def request_decision(
        self,
        *,
        brief: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Publish one Lead-only, evidence-bound Decision Brief v1."""

        if not isinstance(brief, dict) or set(brief) != {
            "title",
            "question",
            "affected_instances",
            "impact",
            "risk",
            "reversible",
            "options",
            "recommendation",
            "challenge",
            "dissent",
            "default_option_id",
        }:
            raise FleetControlError("decision brief fields do not match schema")
        if not isinstance(idempotency_key, str) or not mission_state.SAFE_KEY.fullmatch(
            idempotency_key
        ):
            raise FleetControlError("invalid decision idempotency key")
        recommendation = brief.get("recommendation")
        challenge = brief.get("challenge")
        if not isinstance(recommendation, dict) or set(recommendation) != {
            "option_id",
            "rationale",
            "artifact_id",
        }:
            raise FleetControlError("decision recommendation fields do not match schema")
        if not isinstance(challenge, dict) or set(challenge) != {
            "summary",
            "artifact_id",
        }:
            raise FleetControlError("decision challenge fields do not match schema")
        evidence_ids = [recommendation.get("artifact_id"), challenge.get("artifact_id")]
        if any(
            not isinstance(value, str) or not mission_state.SHA256.fullmatch(value)
            for value in evidence_ids
        ) or len(set(evidence_ids)) != 2:
            raise FleetControlError(
                "decision recommendation and challenge require distinct artifact IDs"
            )
        self._require_attested_artifacts(evidence_ids)
        current = self.state()
        recommendation_binding = self._decision_evidence_binding(
            current, str(recommendation["artifact_id"])
        )
        challenge_binding = self._decision_evidence_binding(
            current, str(challenge["artifact_id"])
        )
        members = self.members()
        challenge_member = members.get(challenge_binding["instance"])
        if not isinstance(challenge_member, dict) or challenge_member.get("phase") not in {
            "CHALLENGE",
            "VERIFY",
        }:
            raise FleetControlError(
                "decision challenge evidence must come from a CHALLENGE or VERIFY member"
            )
        if challenge_member.get("authority") == "write":
            raise FleetControlError("decision challenger cannot have write authority")
        recommendation_identity = tuple(
            recommendation_binding[field] for field in ("provider", "model", "variant")
        )
        challenge_identity = tuple(
            challenge_binding[field] for field in ("provider", "model", "variant")
        )
        if (
            recommendation_binding["instance"] == challenge_binding["instance"]
            or recommendation_identity == challenge_identity
        ):
            raise FleetControlError(
                "decision challenger must use a distinct instance and provider/model identity"
            )
        affected = brief.get("affected_instances")
        if not isinstance(affected, list) or not affected:
            raise FleetControlError("decision affected_instances must be non-empty")
        if any(
            not isinstance(instance, str)
            or instance == "lead"
            or instance not in members
            for instance in affected
        ):
            raise FleetControlError(
                "decision affected_instances must name compiled specialists"
            )
        if len(affected) != len(set(affected)):
            raise FleetControlError("decision affected_instances must be unique")
        decision_id = str(
            uuid.uuid5(uuid.UUID(self.mission_id), f"decision:{idempotency_key}")
        )
        payload = {
            "decision_id": decision_id,
            "title": brief["title"],
            "question": brief["question"],
            "affected_instances": sorted(affected),
            "impact": brief["impact"],
            "risk": brief["risk"],
            "reversible": brief["reversible"],
            "options": brief["options"],
            "recommendation": {
                "option_id": recommendation["option_id"],
                "rationale": recommendation["rationale"],
                **{
                    field: recommendation_binding[field]
                    for field in ("artifact_id", "delegation_id", "instance")
                },
            },
            "challenge": {
                "summary": challenge["summary"],
                **{
                    field: challenge_binding[field]
                    for field in ("artifact_id", "delegation_id", "instance")
                },
            },
            "dissent": brief["dissent"],
            "default_option_id": brief["default_option_id"],
        }
        try:
            mission_state.validate_decision_request_payload(payload)
            event, appended = mission_state.append_event(
                self.runs_dir,
                self.mission_id,
                kind="human_decision_requested",
                actor="lead",
                idempotency_key=idempotency_key,
                payload=payload,
            )
        except mission_state.MissionStateError as exc:
            raise FleetControlError(str(exc)) from exc
        refreshed = self.state()
        decision = refreshed["decisions"][decision_id]
        try:
            formatted = fleet_decisions.format_brief(refreshed, decision)
        except fleet_decisions.DecisionError as exc:
            raise FleetControlError(str(exc)) from exc
        return {
            "event": event,
            "appended": appended,
            "decision": decision,
            "brief": formatted,
        }

    def list_decisions(self, *, pending_only: bool = False) -> dict[str, Any]:
        try:
            current, decisions = fleet_decisions.list_decisions(
                self.runs_dir, self.mission_id, pending_only=pending_only
            )
        except (fleet_decisions.DecisionError, mission_state.MissionStateError) as exc:
            raise FleetControlError(str(exc)) from exc
        return {
            "mission_id": current["mission_id"],
            "pending_only": pending_only,
            "decisions": decisions,
        }

    def request_assurance(
        self,
        *,
        risk: str,
        categories: list[str],
        reason: str,
        idempotency_key: str,
        actor: str = "lead",
        _caller_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if risk not in {"high", "unknown"}:
            raise FleetControlError("assurance risk must be high or unknown")
        if not isinstance(categories, list) or not all(
            isinstance(item, str) and item for item in categories
        ):
            raise FleetControlError("assurance categories must be a string list")
        if not isinstance(reason, str) or not reason:
            raise FleetControlError("assurance reason must be non-empty")
        if not mission_state.SAFE_ACTOR.fullmatch(actor):
            raise FleetControlError("assurance actor identity is invalid")
        with mission_state.MissionTransaction(
            self.runs_dir, self.mission_id
        ) as transaction:
            current = transaction.current_state
            self._require_active_caller(current, _caller_identity)
            if current is None:
                raise FleetControlError("mission does not exist")
            if (
                mission_state.RISK_ORDER[risk]
                < mission_state.RISK_ORDER[current["risk"]]
            ):
                raise FleetControlError("assurance request cannot decrease risk")
            if risk != current["risk"]:
                transaction.append_event(
                    kind="risk_escalated",
                    actor=actor,
                    idempotency_key=f"{idempotency_key}:risk",
                    payload={
                        "from": current["risk"],
                        "to": risk,
                        "categories": categories,
                        "reason": reason,
                    },
                )
                current = transaction.current_state
                assert current is not None
            # A live caller still owns an active admission.  Crossing directly
            # into the assurance lane here would carry that authority into the
            # handoff.  The risk escalation is the durable request signal; the
            # mission driver waits for exact terminal evidence, finalizes every
            # admission, and only then emits `assurance_requested`.
        return self.state()

    def cancel(
        self, *, run_id: str, reason: str, idempotency_key: str
    ) -> dict[str, Any]:
        current = self.state()
        run_id = mission_state.normalize_uuid(run_id, "run_id")
        delegation = next(
            (
                item
                for item in current["delegations"].values()
                if item["run_id"] == run_id
            ),
            None,
        )
        if delegation is None:
            raise FleetControlError("cancel references unknown run")
        manifest = self.manifest()
        instance = delegation["recipient_instance"]
        runner = manifest.get(f"{instance}.runner")
        try:
            configured_provider = fleet_providers.identity(
                delegation["provider"],
                delegation["model"],
                delegation.get("variant"),
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
                    [
                        str(ROOT / "scripts" / "fleet-abandon.sh"),
                        current["feature"],
                        instance,
                        run_id,
                        reason,
                    ],
                    runs_dir=self.runs_dir,
                    timeout=60,
                )
            )
        elif runner == "local":
            # A cmux surface is a reusable presentation resource, not a
            # process/run identity.  Sending ctrl-c to it can kill a newer run
            # after the requested run has already finished (A -> B race).
            # Until the local runner exposes an exact run-scoped process handle,
            # fail closed without any external cancellation effect.
            raise FleetControlError(
                "local cancellation is unavailable without an exact run-scoped handle"
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
        return {
            "run_id": run_id,
            "status": "cancel_requested",
            "event_id": event["event_id"],
        }

    def complete(
        self, *, artifact_id: str, summary: str, idempotency_key: str
    ) -> dict[str, Any]:
        self._reconcile_expired_decisions()
        self._require_attested_artifacts([artifact_id])
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
        value = fleet_json.load(path)
    except fleet_json.FleetJSONError as exc:
        raise FleetControlError(f"cannot load dispatch-many spec: {exc}") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise FleetControlError("dispatch-many spec must be a JSON array of objects")
    return value


def _load_decision_brief(path: Path) -> dict[str, Any]:
    try:
        value = fleet_json.load(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise FleetControlError(f"cannot read decision brief: {exc}") from exc
    if not isinstance(value, dict):
        raise FleetControlError("decision brief must be a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--preset")
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
    decision = commands.add_parser("request-decision")
    decision.add_argument("--brief-file", required=True)
    decision.add_argument("--idempotency-key", required=True)
    decisions = commands.add_parser("list-decisions")
    decisions.add_argument("--pending", action="store_true")
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
        control = FleetControl(Path(args.runs_dir), args.mission_id, preset=args.preset)
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
                reason=args.reason,
                scope=args.scope,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "request-decision":
            value = control.request_decision(
                brief=_load_decision_brief(Path(args.brief_file).expanduser().resolve()),
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "list-decisions":
            value = control.list_decisions(pending_only=args.pending)
        elif args.command == "inspect-roster":
            value = control.inspect_roster()
        elif args.command == "inspect-mission":
            value = control.state()
        elif args.command == "cancel":
            value = control.cancel(
                run_id=args.run_id,
                reason=args.reason,
                idempotency_key=args.idempotency_key,
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
