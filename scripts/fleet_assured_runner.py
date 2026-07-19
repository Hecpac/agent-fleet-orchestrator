#!/usr/bin/env python3
"""Idempotently drive existing FDP-2/FDP-3 controllers for one Mission."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
from typing import Any

import fleet_admission
import fleet_assurance_controller as fdp3
import fleet_audit_client
import fleet_control
import fleet_dialogue
import fleet_dialogue_controller as fdp2
import fleet_json
import fleet_ledger
import fleet_manifest
import fleet_mission
import fleet_mission_state as mission_state
import fleet_providers
import fleet_safe_paths
import fleet_state


ROOT = Path(__file__).resolve().parents[1]
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class AssuredRunnerError(RuntimeError):
    """The assured action stream cannot be reconciled safely."""


class AssuredApprovalRenewalRequired(AssuredRunnerError):
    """An assured launch paused safely until a human renews its authority."""

    def __init__(self, message: str, *, next_action: str) -> None:
        super().__init__(message)
        self.next_action = next_action


def run_process(
    command: list[str], *, runs_dir: Path, timeout: int | None = None
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
        raise AssuredRunnerError(
            f"command failed: {Path(command[0]).name}: {exc}"
        ) from exc


def json_objects(text: str) -> list[dict[str, Any]]:
    try:
        values = fleet_json.load_jsonl(
            text, require_nonempty=True, require_final_newline=False
        )
    except fleet_json.FleetJSONError as exc:
        raise AssuredRunnerError(
            f"process returned invalid strict JSON: {exc}"
        ) from exc
    if any(not isinstance(value, dict) for value in values):
        raise AssuredRunnerError("process returned a non-object JSON record")
    return values


def parse_manifest(path: Path) -> dict[str, str]:
    """Read one active manifest through a pinned parent descriptor."""

    try:
        parent = path.parent.resolve(strict=True)
        with fleet_safe_paths.RootedFS(parent) as rooted:
            content = rooted.read_regular(
                path.name,
                directory_modes=(),
                file_mode=0o600,
                max_bytes=MAX_MANIFEST_BYTES,
                require_single_link=True,
            )
            rooted.assert_root_binding()
        return fleet_manifest.normalize(fleet_manifest.parse_bytes(content))
    except (
        OSError,
        RuntimeError,
        fleet_manifest.ManifestError,
        fleet_safe_paths.SafePathError,
    ) as exc:
        raise AssuredRunnerError(f"cannot read assured manifest safely: {exc}") from exc


def validate_manifest_binding(
    manifest: dict[str, str],
    mission: dict[str, Any],
    compiled: dict[str, Any],
) -> None:
    """Bind the assured runtime to one Mission, repository, base, and roster."""

    mission_id = str(mission.get("mission_id", ""))
    if manifest.get("mission_id") != mission_id:
        raise AssuredRunnerError("assured manifest mission_id mismatch")
    if manifest.get("feature") != mission.get("feature"):
        raise AssuredRunnerError("assured manifest feature mismatch")
    if manifest.get("preset") != "fleet_dialogue" or manifest.get("mode") != "assured":
        raise AssuredRunnerError(
            "assured runner requires preset=fleet_dialogue mode=assured"
        )
    if manifest.get("target_repo") != mission.get("target_repo"):
        raise AssuredRunnerError("assured manifest target repository mismatch")
    if manifest.get("base_sha") != mission.get("base_sha"):
        raise AssuredRunnerError("assured manifest base_sha mismatch")
    if compiled.get("workflow_digest") != mission.get("workflow_digest"):
        raise AssuredRunnerError("assured compiled workflow digest mismatch")
    writers = {
        key.rsplit(".", 1)[0]
        for key, value in manifest.items()
        if key.endswith(".authority") and value == "write"
    }
    if writers != {fdp2.MAKER_INSTANCE}:
        raise AssuredRunnerError("assured manifest writer roster mismatch")
    if manifest.get(f"{fdp2.MAKER_INSTANCE}.base_sha") != mission.get("base_sha"):
        raise AssuredRunnerError("assured manifest writer base_sha mismatch")
    try:
        fleet_manifest.verify_compiled_binding(manifest, compiled)
        fdp2._validate_roster(manifest)
    except (fleet_manifest.ManifestError, fdp2.ControllerError) as exc:
        raise AssuredRunnerError(
            f"assured manifest workflow/roster mismatch: {exc}"
        ) from exc


class AssuredRunner:
    def __init__(self, runs_dir: Path, mission_id: str) -> None:
        self.runs_dir = runs_dir.resolve()
        self.mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
        try:
            compiled, self.mission = fleet_mission.load_mission_compiled(
                self.runs_dir, self.mission_id, mode="effect"
            )
        except fleet_mission.MissionError as exc:
            raise AssuredRunnerError(
                f"assured effects require a valid compiled workflow v2: {exc}"
            ) from exc
        self.compiled = compiled
        self.feature = self.mission["feature"]
        self.root = mission_state.mission_root(self.runs_dir, self.mission_id)
        self.manifest_path = self.runs_dir / f"fleet-{self.feature}.manifest"
        try:
            self._phase_manifest, _ = fleet_state.load_live(self.manifest_path)
            self.manifest = fleet_manifest.normalize(self._phase_manifest)
        except (
            OSError,
            fleet_manifest.ManifestError,
            fleet_safe_paths.SafePathError,
            fleet_state.PhaseStateError,
        ) as exc:
            raise AssuredRunnerError(
                f"cannot read assured manifest/phase safely: {exc}"
            ) from exc
        validate_manifest_binding(self.manifest, self.mission, compiled)
        self.audit = fleet_audit_client.AuditLifecycle(self.runs_dir, self.mission_id)
        self.audit.health()

    def _events(self) -> list[dict[str, Any]]:
        return mission_state.read_events(
            mission_state.ledger_path(self.runs_dir, self.mission_id),
            expected_mission_id=self.mission_id,
        )

    def _record(self, kind: str, key: str, payload: dict[str, Any]) -> None:
        event, _ = mission_state.append_event(
            self.runs_dir,
            self.mission_id,
            kind=kind,
            actor="ASSURED",
            idempotency_key=key,
            payload=payload,
        )
        self.audit.record_control_event(
            event_type="MissionEvent",
            subject_id=event["event_id"],
            subject_sha256=event["event_sha256"],
            metadata={"kind": kind, "sequence": event["sequence"]},
            idempotency_key=f"mission-event:{event['event_id']}",
        )

    def _approval(self, *, require_live: bool = True) -> dict[str, Any]:
        current = fleet_mission.load_state(self.runs_dir, self.mission_id)
        approval = current.get("approval")
        if not isinstance(approval, dict):
            raise AssuredRunnerError("assured mission lacks scoped human approval")
        if approval["workflow_digest"] != current["workflow_digest"]:
            raise AssuredRunnerError("approval workflow digest drift")
        if Path(approval["scope"]).resolve() != Path(current["target_repo"]).resolve():
            raise AssuredRunnerError("approval scope drift")
        try:
            expires = datetime.fromisoformat(
                approval["expires_at"].replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise AssuredRunnerError("approval expiry is invalid") from exc
        if require_live and expires <= datetime.now(timezone.utc):
            command = shlex.join(
                [
                    "python3",
                    "scripts/fleet-approve.py",
                    "--runs-dir",
                    str(self.runs_dir),
                    "--mission-id",
                    self.mission_id,
                    "--scope",
                    str(current["target_repo"]),
                    "--renew",
                ]
            )
            raise AssuredApprovalRenewalRequired(
                "scoped human approval expired before assured launch",
                next_action=f"{command}; then resume",
            )
        return approval

    def _legacy_events(self) -> list[dict[str, Any]]:
        path = self.runs_dir / f"fleet-{self.feature}.ledger.jsonl"
        try:
            return fleet_ledger.read_records(path)
        except fleet_ledger.LedgerError as exc:
            raise AssuredRunnerError(
                f"legacy fleet ledger is unsafe or corrupt: {exc}"
            ) from exc

    def _reconcile_run(
        self,
        instance: str,
        expected_run_id: str,
        prompt_sha256: str,
    ) -> str | None:
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
            raise AssuredRunnerError(
                "exact assured run lifecycle differs from its task binding"
            )
        return expected_run_id

    def _completed_dispatch(
        self,
        *,
        instance: str,
        action_key: str,
        capability: str,
        prompt_sha256: str,
    ) -> str | None:
        """Recover an exact completed dispatch before consulting newer authority."""

        idempotency_key = f"{action_key}:dispatched"
        matches = [
            event
            for event in self._events()
            if event["kind"] == "assured_action_completed"
            and event["idempotency_key"] == idempotency_key
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise AssuredRunnerError("assured dispatch completion is not unique")
        payload = matches[0]["payload"]
        run_id = str(payload.get("run_id", ""))
        expected = {
            "action": "dispatch",
            "instance": instance,
            "prompt_sha256": prompt_sha256,
            "controller_sequence": action_key,
            "run_id": run_id,
        }
        if payload != expected:
            raise AssuredRunnerError("assured dispatch completion binding drift")
        current = fleet_mission.load_state(self.runs_dir, self.mission_id)
        owner = current["run_owners"].get(run_id)
        if owner is None or owner.get("owner_kind") != "admission":
            raise AssuredRunnerError("completed assured dispatch lost run ownership")
        admission = current["admissions"].get(owner["owner_id"])
        if (
            not isinstance(admission, dict)
            or admission["run_id"] != run_id
            or admission["recipient_instance"] != instance
            or admission["capability"] != capability
            or admission["task_sha256"] != prompt_sha256
            or admission["phase"] not in {"started", "finalized"}
            or not isinstance(admission.get("launch_authorization"), dict)
        ):
            raise AssuredRunnerError(
                "completed assured dispatch lost its exact admission binding"
            )
        return run_id

    def _reserve_effect(
        self,
        *,
        instance: str,
        action_key: str,
        capability: str,
        prompt_sha256: str,
        approval_event_sha256: str,
    ) -> dict[str, Any]:
        current = fleet_mission.load_state(self.runs_dir, self.mission_id)
        parent_admission_id = None
        parent_run_id = None
        lead_id = current.get("lead_admission_id")
        if isinstance(lead_id, str):
            lead = current["admissions"].get(lead_id)
            if (
                isinstance(lead, dict)
                and lead.get("active")
                and lead.get("phase") == "started"
            ):
                parent_admission_id = lead_id
                parent_run_id = lead["run_id"]
        attempt = 0
        while True:
            identity = {
                "action": action_key,
                "approval_event_sha256": approval_event_sha256,
            }
            if attempt:
                identity["attempt"] = attempt
            request_key = "assured-" + mission_state.sha256(identity)[:40]
            admission_id = fleet_admission.deterministic_ids(
                self.mission_id,
                request_key=request_key,
                run_kind="specialist",
            )["admission_id"]
            prior = current["admissions"].get(admission_id)
            if not isinstance(prior, dict) or prior["phase"] != "aborted":
                break
            attempt += 1
        hook_source = str(
            self.manifest.get(f"{instance}.hook_source")
            or self.manifest.get(f"{instance}.provider_adapter")
            or ""
        )
        try:
            provider_identity = fleet_providers.identity(
                str(self.manifest.get(f"{instance}.provider") or ""),
                str(self.manifest.get(f"{instance}.model") or ""),
                self.manifest.get(f"{instance}.variant") or None,
                hook_source,
            )
        except fleet_providers.ProviderError as exc:
            raise AssuredRunnerError(
                "assured provider identity is invalid before admission"
            ) from exc
        writer = self.manifest.get(f"{instance}.authority") == "write"
        effect_sha256 = mission_state.sha256(
            {
                "schema_version": 1,
                "approval_event_sha256": approval_event_sha256,
                "objective_sha256": prompt_sha256,
                "prompt_sha256": prompt_sha256,
                "input_artifact_ids": [],
                "expected_output_contract": {
                    "type": "assured-controller-result",
                    "controller_sequence": action_key,
                },
                "grants": {
                    "capability": capability,
                    "delegated_budget": 0,
                    "writer": writer,
                },
                "provider_identity": {
                    "provider": provider_identity.provider,
                    "model": provider_identity.model,
                    "variant": provider_identity.variant,
                    "hook_source": provider_identity.hook_source,
                },
                "runner": self.manifest.get(f"{instance}.runner"),
            }
        )
        request = {
            "request_key": request_key,
            "run_kind": "specialist",
            "recipient_instance": instance,
            "capability": capability,
            "parent_admission_id": parent_admission_id,
            "parent_run_id": parent_run_id,
            "delegated_budget": 0,
            "writer": writer,
            "effect_sha256": effect_sha256,
            "task_sha256": prompt_sha256,
        }
        try:
            return fleet_admission.reserve_many(
                self.runs_dir,
                self.mission_id,
                requests=[request],
                idempotency_key=f"admission:{request_key}",
                actor="ASSURED",
            )["admissions"][0]
        except mission_state.MissionStateError as exc:
            try:
                self._approval()
            except AssuredApprovalRenewalRequired as renewal:
                raise renewal from exc
            raise AssuredRunnerError(
                f"assured admission reservation failed: {exc}"
            ) from exc

    def _abort_prelaunch(self, admission_id: str, *, reason: str) -> None:
        """Release one exact reserved/committed admission without minting credit."""

        durable = fleet_mission.load_state(self.runs_dir, self.mission_id)[
            "admissions"
        ].get(admission_id)
        if durable is None or durable["phase"] == "aborted":
            return
        if durable["phase"] not in {"reserved", "committed"}:
            raise AssuredRunnerError(
                "assured prelaunch cleanup reached a launch-authorized admission"
            )
        try:
            fleet_admission.abort_prelaunch(
                self.runs_dir,
                self.mission_id,
                admission_id=durable["admission_id"],
                run_id=durable["run_id"],
                request_digest=durable["request_digest"],
                effect_sha256=durable["effect_sha256"],
                task_sha256=durable["task_sha256"],
                recipient_instance=durable["recipient_instance"],
                writer=bool(durable["writer"]),
                reason=reason,
                idempotency_key=(f"admission:abort-prelaunch:{durable['request_key']}"),
                actor="ASSURED",
            )
        except mission_state.MissionStateError as exc:
            raise AssuredRunnerError(
                f"assured prelaunch admission cleanup failed: {exc}"
            ) from exc

    def _raise_prelaunch_failure(
        self,
        admission_id: str,
        *,
        reason: str,
        cause: BaseException,
    ) -> None:
        """Abort exact prelaunch state and preserve an expiry renewal instruction."""

        self._abort_prelaunch(admission_id, reason=reason)
        try:
            self._approval()
        except AssuredApprovalRenewalRequired as renewal:
            raise renewal from cause
        raise AssuredRunnerError(reason) from cause

    def _start_effect(
        self,
        admission: dict[str, Any],
        *,
        authorization_event_sha256: str,
    ) -> None:
        durable = fleet_mission.load_state(self.runs_dir, self.mission_id)[
            "admissions"
        ].get(admission["admission_id"])
        if durable is None:
            raise AssuredRunnerError("accepted assured effect lost admission ownership")
        if durable["phase"] == "started":
            return
        if durable["phase"] != "authorized":
            raise AssuredRunnerError(
                f"accepted assured effect has terminal admission phase {durable['phase']}"
            )
        try:
            fleet_admission.mark_started(
                self.runs_dir,
                self.mission_id,
                admission_id=durable["admission_id"],
                authorization_event_sha256=authorization_event_sha256,
                request_digest=durable["request_digest"],
                effect_sha256=durable["effect_sha256"],
                recipient_instance=durable["recipient_instance"],
                writer=bool(durable["writer"]),
                run_id=durable["run_id"],
                idempotency_key=f"admission:start:{durable['request_key']}",
                actor="ASSURED",
            )
        except mission_state.MissionStateError as exc:
            raise AssuredRunnerError(
                "assured wrapper accepted but durable start failed; the exact "
                "authorization remains active for reconciliation"
            ) from exc

    def _finalize_effect(
        self,
        run_id: str,
        *,
        terminal_evidence: dict[str, Any],
    ) -> None:
        current = fleet_mission.load_state(self.runs_dir, self.mission_id)
        owner = current["run_owners"].get(run_id)
        if owner is None or owner.get("owner_kind") != "admission":
            raise AssuredRunnerError("assured terminal run lacks admission ownership")
        durable = current["admissions"].get(owner["owner_id"])
        if durable is None:
            raise AssuredRunnerError("assured terminal admission disappeared")
        if durable["phase"] == "finalized":
            if durable["terminal"].get("terminal_evidence") != terminal_evidence:
                raise AssuredRunnerError("assured admission terminal is immutable")
            return
        try:
            fleet_admission.finalize(
                self.runs_dir,
                self.mission_id,
                admission_id=durable["admission_id"],
                recipient_instance=durable["recipient_instance"],
                writer=bool(durable["writer"]),
                terminal_evidence=terminal_evidence,
                reason=(
                    f"durable assured terminal status: {terminal_evidence['status']}"
                ),
                idempotency_key=f"admission:finalize:{durable['request_key']}",
                actor="ASSURED",
            )
        except mission_state.MissionStateError as exc:
            raise AssuredRunnerError(
                f"assured admission finalization failed: {exc}"
            ) from exc

    def _dispatch_prompt(
        self,
        *,
        instance: str,
        prompt: str,
        prompt_sha: str,
        action_key: str,
        capability: str,
    ) -> str:
        lock_name = (
            f"dispatch-locks/{mission_state.sha256({'action': action_key})}.lock"
        )
        try:
            with (
                fleet_safe_paths.RootedFS(self.root, root_mode=0o700) as rooted,
                rooted.exclusive_lock(
                    lock_name,
                    directory_modes=(0o700,),
                    file_mode=0o600,
                    require_single_link=True,
                ),
            ):
                return self._dispatch_prompt_locked(
                    instance=instance,
                    prompt=prompt,
                    prompt_sha=prompt_sha,
                    action_key=action_key,
                    capability=capability,
                )
        except fleet_safe_paths.SafePathError as exc:
            raise AssuredRunnerError(
                f"cannot acquire exact assured dispatch lock: {exc}"
            ) from exc

    def _dispatch_prompt_locked(
        self,
        *,
        instance: str,
        prompt: str,
        prompt_sha: str,
        action_key: str,
        capability: str,
    ) -> str:
        completed_run_id = self._completed_dispatch(
            instance=instance,
            action_key=action_key,
            capability=capability,
            prompt_sha256=prompt_sha,
        )
        if completed_run_id is not None:
            return completed_run_id
        approval = self._approval(require_live=False)
        approval_event_sha256 = str(approval["event_sha256"])
        admission = self._reserve_effect(
            instance=instance,
            action_key=action_key,
            capability=capability,
            prompt_sha256=prompt_sha,
            approval_event_sha256=approval_event_sha256,
        )
        intent = {
            "action": "dispatch",
            "instance": instance,
            "prompt_sha256": prompt_sha,
            "controller_sequence": action_key,
        }
        try:
            self._record("assured_action_intent", f"{action_key}:intent", intent)
        except (
            fleet_audit_client.AuditClientError,
            mission_state.MissionStateError,
        ) as exc:
            self._raise_prelaunch_failure(
                admission["admission_id"],
                reason=f"assured dispatch intent failed before launch: {exc}",
                cause=exc,
            )
        durable = fleet_mission.load_state(self.runs_dir, self.mission_id)[
            "admissions"
        ].get(admission["admission_id"])
        if durable is None:
            raise AssuredRunnerError("assured admission disappeared before commit")
        if durable["phase"] == "reserved":
            try:
                committed = fleet_admission.commit(
                    self.runs_dir,
                    self.mission_id,
                    admission_id=durable["admission_id"],
                    request_digest=durable["request_digest"],
                    effect_sha256=durable["effect_sha256"],
                    recipient_instance=instance,
                    writer=bool(durable["writer"]),
                    run_id=durable["run_id"],
                    idempotency_key=f"admission:commit:{durable['request_key']}",
                    actor="ASSURED",
                )
            except mission_state.MissionStateError as exc:
                self._raise_prelaunch_failure(
                    durable["admission_id"],
                    reason=f"assured admission commit failed: {exc}",
                    cause=exc,
                )
            commit_event_sha256 = committed["commit_event_sha256"]
        elif durable["phase"] in {
            "committed",
            "authorized",
            "started",
            "finalized",
        }:
            proof = durable.get("commit")
            if proof is None:
                raise AssuredRunnerError("assured admission lacks commit proof")
            commit_event_sha256 = proof["event_sha256"]
        else:
            raise AssuredRunnerError(
                f"assured effect cannot dispatch from phase {durable['phase']}"
            )
        durable = fleet_mission.load_state(self.runs_dir, self.mission_id)[
            "admissions"
        ].get(admission["admission_id"])
        if durable is None:
            raise AssuredRunnerError("assured admission disappeared after commit")
        run_id = self._reconcile_run(instance, durable["run_id"], prompt_sha)
        if run_id is None:
            if durable["phase"] in {"started", "finalized"}:
                raise AssuredRunnerError(
                    "recognized assured admission lacks external effect; refusing relaunch"
                )
            runner = self.manifest.get(f"{instance}.runner")
            wrapper = {
                "interactive": "fleet-send.sh",
                "local": "fleet-dispatch.sh",
            }.get(runner)
            if wrapper is None:
                error = AssuredRunnerError(
                    f"unsupported assured runner: {instance}={runner}"
                )
                self._raise_prelaunch_failure(
                    durable["admission_id"],
                    reason=str(error),
                    cause=error,
                )
            command = [
                str(ROOT / "scripts" / wrapper),
                self.feature,
                instance,
                prompt,
                "--run-id",
                durable["run_id"],
                "--json",
            ]
            if durable["phase"] == "authorized":
                authorization_proof = durable.get("launch_authorization")
                if not isinstance(authorization_proof, dict):
                    raise AssuredRunnerError(
                        "authorized assured effect lacks its durable launch proof"
                    )
                authorization_event_sha256 = authorization_proof["event_sha256"]
            else:
                try:
                    self._phase_state()
                    fleet_control.require_usage_launch(
                        self.runs_dir,
                        self.compiled,
                        feature=self.feature,
                        provider=str(self.manifest.get(f"{instance}.provider") or ""),
                        hook_source=str(
                            self.manifest.get(f"{instance}.hook_source")
                            or self.manifest.get(f"{instance}.provider_adapter")
                            or ""
                        ),
                    )
                except (
                    AssuredRunnerError,
                    fleet_control.FleetControlError,
                    mission_state.MissionStateError,
                ) as exc:
                    self._raise_prelaunch_failure(
                        durable["admission_id"],
                        reason=f"assured launch preflight failed: {exc}",
                        cause=exc,
                    )
                try:
                    launch_approval = self._approval()
                except AssuredApprovalRenewalRequired:
                    self._abort_prelaunch(
                        durable["admission_id"],
                        reason="assured approval expired before launch authorization",
                    )
                    raise
                if launch_approval["event_sha256"] != approval_event_sha256:
                    error = AssuredRunnerError(
                        "assured approval changed after effect reservation"
                    )
                    self._raise_prelaunch_failure(
                        durable["admission_id"],
                        reason=str(error),
                        cause=error,
                    )
                try:
                    authorization = fleet_admission.authorize_launch(
                        self.runs_dir,
                        self.mission_id,
                        admission_id=durable["admission_id"],
                        commit_event_sha256=commit_event_sha256,
                        request_digest=durable["request_digest"],
                        effect_sha256=durable["effect_sha256"],
                        recipient_instance=instance,
                        writer=bool(durable["writer"]),
                        run_id=durable["run_id"],
                        approval_event_sha256=approval_event_sha256,
                        idempotency_key=(
                            f"admission:authorize:{durable['request_key']}"
                        ),
                        actor="ASSURED",
                    )
                except mission_state.MissionStateError as exc:
                    self._raise_prelaunch_failure(
                        durable["admission_id"],
                        reason=f"assured launch authorization failed: {exc}",
                        cause=exc,
                    )
                authorization_event_sha256 = authorization["authorization_event_sha256"]
            # The external wrapper is the immediate next effect after the
            # durable launch authorization boundary.
            result = run_process(
                command,
                runs_dir=self.runs_dir,
                timeout=120,
            )
            values = json_objects(result.stdout)
            if result.returncode != 0 or len(values) != 1:
                detail = result.stderr.strip() or "ambiguous dispatch response"
                raise AssuredRunnerError(f"assured dispatch failed: {detail}")
            run_id = str(values[0].get("run_id", ""))
        else:
            authorization_proof = durable.get("launch_authorization")
            if not isinstance(authorization_proof, dict):
                raise AssuredRunnerError(
                    "reconciled assured effect lacks launch authorization"
                )
            authorization_event_sha256 = authorization_proof["event_sha256"]
        if run_id != durable["run_id"]:
            raise AssuredRunnerError("assured effect changed deterministic run_id")
        self._start_effect(
            durable,
            authorization_event_sha256=authorization_event_sha256,
        )
        self._record(
            "assured_action_completed",
            f"{action_key}:dispatched",
            {**intent, "run_id": run_id},
        )
        return run_id

    def _dispatch(self, action: dict[str, Any], action_key: str) -> str:
        instance = str(action["instance"])
        prompt_path = Path(str(action["prompt_file"]))
        try:
            info = prompt_path.lstat()
            prompt = prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AssuredRunnerError("assured prompt file is unavailable") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise AssuredRunnerError("assured prompt must be a regular file")
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if prompt_sha != action["prompt_sha256"]:
            raise AssuredRunnerError("assured prompt hash changed before dispatch")
        phase = str(
            action.get("phase") or self.manifest.get(f"{instance}.phase") or "work"
        ).lower()
        return self._dispatch_prompt(
            instance=instance,
            prompt=prompt,
            prompt_sha=prompt_sha,
            action_key=action_key,
            capability=f"assured_{phase}",
        )

    def _wait(
        self,
        action: dict[str, Any],
        run_id: str,
        action_key: str,
        *,
        finalize: bool = True,
    ) -> None:
        instance = str(action["instance"])
        timeout_seconds = int(action["timeout_seconds"])
        prompt_sha256 = action.get("prompt_sha256")
        if not isinstance(prompt_sha256, str) or not mission_state.SHA256.fullmatch(
            prompt_sha256
        ):
            raise AssuredRunnerError(
                "assured wait requires its exact dispatch prompt_sha256"
            )
        intent = {
            "action": "wait",
            "instance": instance,
            "run_id": run_id,
            "timeout_seconds": timeout_seconds,
            "controller_sequence": action_key,
        }
        self._record("assured_action_intent", f"{action_key}:wait:intent", intent)
        result = run_process(
            [
                str(ROOT / "scripts" / "fleet-wait.sh"),
                self.feature,
                instance,
                "--run",
                f"{instance}={run_id}",
                "--timeout",
                str(timeout_seconds),
                "--json",
            ],
            runs_dir=self.runs_dir,
            timeout=timeout_seconds + 30,
        )
        values = json_objects(result.stdout)
        exact = next((value for value in values if value.get("run_id") == run_id), None)
        if exact is None:
            raise AssuredRunnerError("assured wait returned no exact run evidence")
        status = str(exact.get("status", "indeterminate"))
        lifecycle = [
            event
            for event in self._legacy_events()
            if event.get("instance") == instance and event.get("run_id") == run_id
        ]
        if (
            status not in mission_state.TERMINAL_STATUSES
            or not lifecycle
            or lifecycle[-1].get("status") != status
            or any(event.get("task_sha256") != prompt_sha256 for event in lifecycle)
        ):
            raise AssuredRunnerError(
                "assured wait lacks matching durable terminal lifecycle evidence"
            )
        self._record(
            "assured_action_completed",
            f"{action_key}:wait:completed",
            {**intent, "status": status, "exit_code": result.returncode},
        )
        if finalize:
            self._finalize_effect(
                run_id,
                terminal_evidence={
                    "schema_version": 1,
                    "source_event_sha256": mission_state.sha256(lifecycle[-1]),
                    "run_id": run_id,
                    "task_sha256": prompt_sha256,
                    "status": status,
                },
            )

    def _publish(self, action: dict[str, Any], action_key: str) -> str:
        self._approval()
        request = {
            field: action[field]
            for field in (
                "kind",
                "recipient",
                "source_instance",
                "source_run_id",
                "reply_to",
            )
        }
        self._record(
            "assured_action_intent",
            f"{action_key}:publish:intent",
            {
                "action": "publish",
                **request,
                "payload_sha256": action["payload_sha256"],
            },
        )
        message = fleet_dialogue.publish(
            self.runs_dir,
            feature=self.feature,
            idempotency_key=f"runner:{action_key}:publish",
            **request,
        )
        if message["payload_sha256"] != action["payload_sha256"]:
            raise AssuredRunnerError(
                "published payload hash differs from controller action"
            )
        self._record(
            "assured_action_completed",
            f"{action_key}:publish:completed",
            {
                "action": "publish",
                "message_id": message["message_id"],
                "payload_sha256": message["payload_sha256"],
            },
        )
        return str(message["message_id"])

    def _phase_state(self) -> dict[str, Any]:
        try:
            live_manifest, state = fleet_state.load_live(self.manifest_path)
        except (
            OSError,
            fleet_safe_paths.SafePathError,
            fleet_state.PhaseStateError,
        ) as exc:
            raise AssuredRunnerError("cannot read assured fleet phase") from exc
        if live_manifest != self._phase_manifest:
            raise AssuredRunnerError(
                "assured fleet manifest changed after runner start"
            )
        return state

    def _phase(self) -> str:
        return str(self._phase_state()["active_phase"])

    def _require_phase_receipt(
        self,
        phase: str,
        evidence: str,
        *,
        approval_event_sha256: str | None = None,
    ) -> None:
        state = self._phase_state()
        matches = [
            entry
            for entry in state.get("history", [])
            if isinstance(entry, dict) and entry.get("phase") == phase
        ]
        if len(matches) != 1:
            raise AssuredRunnerError(f"assured {phase} phase receipt is not unique")
        receipt = matches[0]
        if receipt.get("evidence") != evidence:
            raise AssuredRunnerError(
                f"assured {phase} phase receipt binding differs from controller"
            )
        receipt_approval = receipt.get("approval_event_sha256")
        if approval_event_sha256 is not None and (
            receipt_approval != approval_event_sha256
        ):
            raise AssuredRunnerError(
                f"assured {phase} phase receipt approval binding drift"
            )
        if phase == "CHALLENGE":
            approvals = [
                event
                for event in self._events()
                if event["event_sha256"] == receipt_approval
                and event["kind"]
                in {"assurance_approved", "assurance_approval_renewed"}
            ]
            if len(approvals) != 1 or approvals[0]["actor"] != "HUMAN":
                raise AssuredRunnerError(
                    "assured CHALLENGE receipt lacks historical human approval"
                )
            approval = approvals[0]
            payload = approval["payload"]
            current = fleet_mission.load_state(self.runs_dir, self.mission_id)
            if any(
                (
                    payload["workflow_digest"] != current["workflow_digest"],
                    payload["scope"] != current["target_repo"],
                    payload["risk"] != current["risk"],
                )
            ):
                raise AssuredRunnerError(
                    "assured CHALLENGE historical approval authority drift"
                )
            approved_at = mission_state.parse_timestamp(
                approval["timestamp"], "historical approval timestamp"
            )
            advanced_at = mission_state.parse_timestamp(
                receipt.get("timestamp"), "CHALLENGE phase timestamp"
            )
            expires_at = mission_state.parse_timestamp(
                payload["expires_at"], "historical approval expiry"
            )
            if not approved_at <= advanced_at < expires_at:
                raise AssuredRunnerError(
                    "assured CHALLENGE receipt used approval outside its lifetime"
                )

    def _advance(
        self,
        phase: str,
        evidence: str,
        *,
        approval_event_sha256: str | None = None,
    ) -> None:
        state = self._phase_state()
        current = str(state["active_phase"])
        if current == phase:
            history = state.get("history")
            latest = history[-1] if isinstance(history, list) and history else None
            if not isinstance(latest, dict) or any(
                (
                    latest.get("phase") != phase,
                    latest.get("evidence") != evidence,
                    latest.get("approval_event_sha256") != approval_event_sha256,
                )
            ):
                raise AssuredRunnerError(
                    "persisted phase binding differs from requested advance"
                )
            return
        command = [
            "python3",
            str(ROOT / "scripts" / "fleet_state.py"),
            "advance",
            str(self.manifest_path),
            phase,
            "--evidence",
            evidence,
        ]
        if approval_event_sha256:
            command += ["--approval-event-sha256", approval_event_sha256]
        result = run_process(command, runs_dir=self.runs_dir, timeout=60)
        if result.returncode != 0:
            raise AssuredRunnerError(
                result.stderr.strip() or "assured phase advance failed"
            )

    @staticmethod
    def _action_key(prefix: str, event: dict[str, Any]) -> str:
        return f"runner:{prefix}:{event['sequence']}:{event['event_sha256'][:16]}"

    def _drive_fdp2(self, event: dict[str, Any]) -> dict[str, Any]:
        if "next_action" not in event:
            event = fdp2.public_event(event)
        while True:
            action = event["next_action"]
            if action["action"] == "terminal":
                if action["status"] != "accepted":
                    raise AssuredRunnerError(
                        f"FDP-2 terminal {action['status']}: {action.get('reason', '')}"
                    )
                return event
            key = self._action_key("fdp2", event)
            if action["action"] == "dispatch":
                run_id = self._dispatch(action, key)
                self._wait(action, run_id, key)
                event = fdp2.step(
                    self.runs_dir,
                    feature=self.feature,
                    idempotency_key=f"{key}:step",
                    run_id=run_id,
                )
            elif action["action"] == "publish":
                message_id = self._publish(action, key)
                event = fdp2.step(
                    self.runs_dir,
                    feature=self.feature,
                    idempotency_key=f"{key}:step",
                    message_id=message_id,
                )
            else:
                raise AssuredRunnerError(
                    f"unsupported FDP-2 action: {action['action']}"
                )
            if "next_action" not in event:
                event = fdp2.public_event(event)

    def _drive_fdp3(self, event: dict[str, Any]) -> dict[str, Any]:
        if "next_action" not in event:
            event = fdp3.public_event(event)
        while True:
            action = event["next_action"]
            if action["action"] == "terminal":
                if action["status"] != "verified":
                    raise AssuredRunnerError(
                        f"FDP-3 terminal {action['status']}: {action.get('reason', '')}"
                    )
                return event
            key = self._action_key("fdp3", event)
            if action["action"] == "dispatch":
                run_id = self._dispatch(action, key)
                self._wait(action, run_id, key)
                event = fdp3.step(
                    self.runs_dir,
                    feature=self.feature,
                    idempotency_key=f"{key}:step",
                    run_id=run_id,
                )
            elif action["action"] == "publish":
                message_id = self._publish(action, key)
                event = fdp3.step(
                    self.runs_dir,
                    feature=self.feature,
                    idempotency_key=f"{key}:step",
                    message_id=message_id,
                )
            elif action["action"] == "advance_phase":
                if self._phase() != "VERIFY":
                    self._approval()
                self._advance("VERIFY", event["event_sha256"])
                event = fdp3.step(
                    self.runs_dir,
                    feature=self.feature,
                    idempotency_key=f"{key}:step",
                    phase_advanced=True,
                )
            else:
                raise AssuredRunnerError(
                    f"unsupported FDP-3 action: {action['action']}"
                )
            if "next_action" not in event:
                event = fdp3.public_event(event)

    def _synthesize(
        self, accepted: dict[str, Any], verified: dict[str, Any]
    ) -> dict[str, Any]:
        prompt = (
            f"MISSION_ID={self.mission_id}\n"
            "Synthesize the completed assured mission from durable FDP-2/FDP-3 evidence. "
            "Do not dispatch more work or modify the repository. Report STATUS, DECISION, "
            "ARTIFACTS, VERIFICATION, RISKS, and NEXT_ACTION.\n"
            f"FDP2_CONTROL_HEAD={accepted['event_sha256']}\n"
            f"FDP2_ACCEPTED_HEAD={accepted['snapshot']['accepted_head_sha']}\n"
            f"FDP3_CONTROL_HEAD={verified['event_sha256']}\n"
            f"FDP3_STATUS={verified['snapshot']['status']}\n"
        )
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        action_key = "runner:assured-synthesis"
        run_id = self._dispatch_prompt(
            instance="lead",
            prompt=prompt,
            prompt_sha=prompt_sha,
            action_key=action_key,
            capability="synthesis",
        )
        self._wait(
            {
                "instance": "lead",
                "timeout_seconds": 1800,
                "prompt_sha256": prompt_sha,
            },
            run_id,
            action_key,
            finalize=False,
        )
        lifecycle = [
            event
            for event in self._legacy_events()
            if event.get("instance") == "lead" and event.get("run_id") == run_id
        ]
        if not lifecycle or lifecycle[-1].get("status") != "succeeded":
            raise AssuredRunnerError("assured Lead synthesis did not succeed")
        result_file = Path(str(lifecycle[-1].get("result_file", "")))
        expected_result = self.runs_dir / "results" / self.feature / f"{run_id}.txt"
        try:
            info = result_file.lstat()
            if result_file.resolve(strict=True) != expected_result.resolve(strict=True):
                raise AssuredRunnerError(
                    "assured Lead result is outside the exact result store"
                )
        except OSError as exc:
            raise AssuredRunnerError(
                "assured Lead synthesis lacks a result file"
            ) from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise AssuredRunnerError("assured Lead result must be a regular file")
        if lifecycle[-1].get("provider") != self.manifest.get(
            "lead.provider"
        ) or lifecycle[-1].get("model") != self.manifest.get("lead.model"):
            raise AssuredRunnerError("assured Lead provider/model identity drift")
        return {
            "run_id": run_id,
            "result_file": str(result_file),
            "prompt_sha256": prompt_sha,
        }

    def drive(self, spec_file: Path, *, synthesize: bool = True) -> dict[str, Any]:
        approval = self._approval(require_live=False)
        phase = self._phase()
        if phase == "CONTROL":
            approval = self._approval()
            self._advance("BUILD", approval["request_event_sha256"])
            phase = "BUILD"
        dialogue_path = self.runs_dir / f"fleet-{self.feature}.dialogue-control.jsonl"
        if dialogue_path.exists():
            dialogue = fdp2.show(self.runs_dir, feature=self.feature)
        else:
            self._approval()
            dialogue = fdp2.start(
                self.runs_dir,
                feature=self.feature,
                idempotency_key=f"runner:{self.mission_id}:fdp2:start",
                spec_file=spec_file.resolve(),
            )
        accepted = self._drive_fdp2(dialogue)
        self.audit.record_control_event(
            event_type="ControllerReceipt",
            subject_id=accepted["conversation_id"],
            subject_sha256=accepted["event_sha256"],
            metadata={"controller": "fdp2", "status": accepted["snapshot"]["status"]},
            idempotency_key=f"fdp2:{accepted['event_sha256']}",
        )
        if phase == "BUILD":
            approval = self._approval()
            self._advance(
                "CHALLENGE",
                accepted["event_sha256"],
                approval_event_sha256=approval["event_sha256"],
            )
            phase = "CHALLENGE"
        elif phase in {"CHALLENGE", "VERIFY"}:
            self._require_phase_receipt(
                "CHALLENGE",
                accepted["event_sha256"],
            )
        else:
            raise AssuredRunnerError(
                f"assured runner cannot resume FDP-3 from phase {phase}"
            )
        assurance_path = self.runs_dir / f"fleet-{self.feature}.assurance-control.jsonl"
        if assurance_path.exists():
            assurance = fdp3.show(self.runs_dir, feature=self.feature)
        else:
            self._approval()
            assurance = fdp3.start(
                self.runs_dir,
                feature=self.feature,
                idempotency_key=f"runner:{self.mission_id}:fdp3:start",
            )
        verified = self._drive_fdp3(assurance)
        self.audit.record_control_event(
            event_type="ControllerReceipt",
            subject_id=verified["assurance_id"],
            subject_sha256=verified["event_sha256"],
            metadata={"controller": "fdp3", "status": verified["snapshot"]["status"]},
            idempotency_key=f"fdp3:{verified['event_sha256']}",
        )
        synthesis = self._synthesize(accepted, verified) if synthesize else None
        return {
            "mission_id": self.mission_id,
            "feature": self.feature,
            "status": "verified",
            "accepted_head_sha": accepted["snapshot"]["accepted_head_sha"],
            "fdp2_event_sha256": accepted["event_sha256"],
            "fdp3_event_sha256": verified["event_sha256"],
            "synthesis": synthesis,
        }

    def advisory(
        self,
        *,
        instance: str,
        objective: str,
        idempotency_key: str,
        timeout_seconds: int = 1800,
    ) -> dict[str, Any]:
        if not objective.strip():
            raise AssuredRunnerError("advisory objective must be non-empty")
        if not mission_state.SAFE_KEY.fullmatch(idempotency_key):
            raise AssuredRunnerError("invalid advisory idempotency key")
        if timeout_seconds < 1:
            raise AssuredRunnerError("advisory timeout must be positive")
        if instance not in self.manifest or instance == "lead":
            raise AssuredRunnerError("unknown advisory instance")
        if self.manifest.get(f"{instance}.authority") in {"write", "control"}:
            raise AssuredRunnerError(
                "additional advisory turns cannot grant writer authority"
            )
        phase = self.manifest.get(f"{instance}.phase")
        if phase != self._phase():
            raise AssuredRunnerError("advisory instance phase is not active")
        prompt = (
            f"MISSION_ID={self.mission_id}\nADVISORY_ONLY=true\n"
            f"ACTIVE_PHASE={phase}\n\n{objective}\n\n"
            "Return read-only analysis with exact evidence. Do not modify the repository."
        )
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        prompt_path = self.root / "advisory" / f"{prompt_sha}.txt"
        content = prompt.encode("utf-8")
        if prompt_path.exists():
            if prompt_path.is_symlink() or prompt_path.read_bytes() != content:
                raise AssuredRunnerError("durable advisory prompt conflicts")
        else:
            mission_state.atomic_write(prompt_path, content)
        key = f"runner:advisory:{idempotency_key}"
        action = {
            "instance": instance,
            "prompt_file": str(prompt_path),
            "prompt_sha256": prompt_sha,
            "timeout_seconds": timeout_seconds,
        }
        run_id = self._dispatch(action, key)
        self._wait(action, run_id, key)
        lifecycle = [
            event
            for event in self._legacy_events()
            if event.get("instance") == instance and event.get("run_id") == run_id
        ]
        if not lifecycle or lifecycle[-1].get("status") != "succeeded":
            raise AssuredRunnerError("additional advisory turn did not succeed")
        terminal = lifecycle[-1]
        expected_identity = {
            "phase": phase,
            "role": self.manifest.get(f"{instance}.role_type"),
            "task_sha256": prompt_sha,
            "provider": self.manifest.get(f"{instance}.provider"),
            "model": self.manifest.get(f"{instance}.model"),
        }
        if any(
            terminal.get(field) != value for field, value in expected_identity.items()
        ):
            raise AssuredRunnerError(
                "additional advisory result provenance differs from dispatch"
            )
        expected_variant = self.manifest.get(f"{instance}.variant")
        if terminal.get("variant") != expected_variant:
            raise AssuredRunnerError(
                "additional advisory result variant differs from dispatch"
            )
        result_path = Path(str(terminal.get("result_file", "")))
        expected = self.runs_dir / "results" / self.feature / f"{run_id}.txt"
        try:
            info = result_path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise AssuredRunnerError("advisory result is not a regular file")
            if result_path.resolve(strict=True) != expected.resolve(strict=True):
                raise AssuredRunnerError(
                    "advisory result is outside the exact result store"
                )
        except OSError as exc:
            raise AssuredRunnerError("advisory result file is missing") from exc
        return {
            "mission_id": self.mission_id,
            "instance": instance,
            "run_id": run_id,
            "result_file": str(result_path),
            "prompt_sha256": prompt_sha,
            "status": "succeeded",
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--mission-id", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    drive = commands.add_parser("drive")
    drive.add_argument("--spec-file", required=True)
    drive.add_argument("--no-synthesis", action="store_true")
    advisory = commands.add_parser("advisory")
    advisory.add_argument("--instance", required=True)
    advisory.add_argument("--objective", required=True)
    advisory.add_argument("--idempotency-key", required=True)
    advisory.add_argument("--timeout", type=int, default=1800)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        runner = AssuredRunner(Path(args.runs_dir), args.mission_id)
        if args.command == "drive":
            value = runner.drive(Path(args.spec_file), synthesize=not args.no_synthesis)
        else:
            value = runner.advisory(
                instance=args.instance,
                objective=args.objective,
                idempotency_key=args.idempotency_key,
                timeout_seconds=args.timeout,
            )
        print(json.dumps(value, sort_keys=True))
        return 0
    except (
        AssuredRunnerError,
        fleet_audit_client.AuditClientError,
        fdp2.ControllerError,
        fdp3.AssuranceError,
        fleet_dialogue.DialogueError,
        mission_state.MissionStateError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"assured-runner: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
