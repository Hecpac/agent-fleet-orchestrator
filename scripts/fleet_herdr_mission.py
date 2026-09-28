#!/usr/bin/env python3
"""Ledger-first Mission driver for the explicit Astra/Sol Herdr profile.

Backend integration contract: HerdrBackend(..., session=<durable option>),
boot/state, submit(run_id, prompt, instance_id=...), recover/wait(run_id),
collect_result(run_id) -> None or the JSON result requested in the prompt.
No transport state is a role result. An uncertain authorized submit is recovered,
never retried by this driver. All five turns use specialist admissions: a single
principal Lead admission cannot be finalized and then reused for synthesis.

Archive integration: freeze(runs_dir, mission_id, candidate_repo),
create(runs_dir, mission_id, candidate_repo, role_results, backend_state), verify.
The archive owns immutable tree/patch capture and independent acceptance checks.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
import stat
import subprocess
import time
from typing import Any
import uuid

import fleet_acceptance
import fleet_admission
import fleet_artifacts
import fleet_herdr
import fleet_herdr_rejection
import fleet_herdr_evidence
import fleet_herdr_permissions
import fleet_herdr_profile
import fleet_herdr_launch
import fleet_herdr_runtime
import fleet_herdr_sdd
import fleet_herdr_scope
import fleet_herdr_repair_policy
import fleet_herdr_control as control
import fleet_herdr_metrics as metrics
import fleet_herdr_instructions
import fleet_herdr_role_guidance
import fleet_functional
import fleet_mission
import fleet_mission_state as state
import fleet_safe_paths

ROOT = Path(__file__).resolve().parents[1]
STAGES = (("plan", "lead", "recon"), ("build", "worker", "build"),
          ("review", "reviewer", "challenge"), ("verify", "verifier", "verify"),
          ("synthesis", "lead", "synthesis"))
PROFILE = fleet_herdr_profile.LEGACY.members  # historical alias of the catalog roster
RESULT_STATUS = {"PASS": "succeeded", "BLOCKED": "blocked", "FAIL": "failed"}


class HerdrMissionError(RuntimeError):
    """A durable contract cannot authorize further Mission effects."""


class RoleProtocolError(HerdrMissionError):
    """Attributed role content fails its protocol, independently of execution closure."""


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "init.templateDir=",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "-C", str(repo), *args], env=env, capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        raise HerdrMissionError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _archive() -> Any:
    try:
        module = importlib.import_module("fleet_herdr_archive")
    except ImportError as exc:
        raise HerdrMissionError("fleet_herdr_archive integration is unavailable") from exc
    if any(not callable(getattr(module, name, None)) for name in ("freeze", "create", "verify")):
        raise HerdrMissionError("archive requires freeze/create/verify")
    return module


class _Driver:
    def __init__(self, runs_dir: Path, mission_id: str) -> None:
        self.runs = runs_dir
        self.mid = state.normalize_uuid(mission_id, "mission_id")
        self.rel = Path("missions") / self.mid
        self.root = self.runs / self.rel
        self.protocol_rejection = None
        self.evidence_rejection = None
        self.transport_rejection = None
        self.sdd_packet: dict[str, Any] | None = None
        self.sdd_error: str | None = None
        self.profile = fleet_herdr_profile.LEGACY
        self.stages = self.profile.stages

    def current(self) -> dict[str, Any]:
        return fleet_mission.load_state(self.runs, self.mid)

    def event(self, kind: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        return state.append_event(self.runs, self.mid, kind=kind, actor="CONTROL",
                                  idempotency_key=f"herdr:{key}", payload=payload)[0]

    def read(self, name: str, *, optional: bool = False) -> Any:
        with fleet_safe_paths.RootedFS(self.runs) as fs:
            reader = fs.read_regular_optional if optional else fs.read_regular
            raw = reader(self.rel / name, directory_modes=(0o700, 0o700),
                         file_mode=0o600, max_bytes=16 * 1024 * 1024)
        if raw is None:
            return None
        return state.loads_strict(raw)

    def write(self, name: str, value: Any) -> None:
        with fleet_safe_paths.RootedFS(self.runs) as fs:
            fs.atomic_write(self.rel / name, state.canonical_bytes(value) + b"\n",
                            directory_modes=(0o700, 0o700), file_mode=0o600)

    def response(self, **extra: Any) -> dict[str, Any]:
        current = self.current()
        return {"mission_id": self.mid, "feature": current["feature"],
                "status": current["status"], "head_sha256": current["head_sha256"],
                **({"control": control.view(current)} if "herdr_control" in current else {}),
                **({"protocol_rejection": self.protocol_rejection} if self.protocol_rejection else {}),
                **({"evidence_rejection": self.evidence_rejection} if self.evidence_rejection else {}),
                **({"transport_rejection": self.transport_rejection} if self.transport_rejection else {}),
                "backend": "herdr", **extra}

    def finish(self, response: dict[str, Any]) -> dict[str, Any]:
        """Cleanup cannot rewrite a durable semantic verdict, including on errors."""
        try:
            options = self.read("runtime-options.json")
            creation = self.read("creation-request.json")
            if creation.get("runtime_options") != options:
                raise HerdrMissionError("teardown options differ from creation receipt")
            if options.get("teardown", False) is not True:
                return response
            binding = {"mission_id": self.mid, "head_sha256": response["head_sha256"],
                       "session": options.get("herdr_session"), "complete": True}
            prior = self.read("herdr-teardown.json", optional=True)
            if prior is not None:
                if prior != binding:
                    raise HerdrMissionError("teardown completion receipt binding mismatch")
                return {**response, "cleanup_pending": False}
            if self.read("herdr-backend.json", optional=True) is not None:
                self.load()
                backend = self.backend()
                backend.teardown()  # backend reconciles its owned workspace/generations
                observed = backend.state()
                if not observed.get("workspace", {}).get("closed"):
                    raise HerdrMissionError("teardown did not confirm owned workspace closed")
            else:
                events = state.read_events(state.ledger_path(self.runs, self.mid), expected_mission_id=self.mid)
                if any(event["kind"] == "fleet_boot_started" for event in events):
                    raise HerdrMissionError("backend ownership receipt missing after boot; selective teardown is unproven")
            self.write("herdr-teardown.json", binding)
            return {**response, "cleanup_pending": False}
        except Exception as exc:
            return {**response, "cleanup_pending": True, "cleanup_error": str(exc)}

    def load(self) -> None:
        self.compiled, current = fleet_mission.load_mission_compiled(self.runs, self.mid, mode="effect")
        self.options = self.read("runtime-options.json")
        creation = self.read("creation-request.json")
        if (not isinstance(self.options, dict) or creation.get("runtime_options") != self.options
                or creation.get("mission_id") != self.mid
                or str(uuid.uuid5(uuid.NAMESPACE_URL, "fleet-mission:" + creation["idempotency_key"])) != self.mid):
            raise HerdrMissionError("durable runtime options/creation binding mismatch")
        if type(self.options.get("teardown", False)) is not bool:
            raise HerdrMissionError("teardown option must be boolean")
        timeout = self.options.get("timeout_seconds", self.compiled["workflow"]["limits"]["deadline_seconds"])
        if type(timeout) is not int or timeout < 60:
            raise HerdrMissionError("timeout_seconds must be an integer of at least 60 seconds")
        events = state.read_events(state.ledger_path(self.runs, self.mid), expected_mission_id=self.mid)
        self.deadline = min(state.parse_timestamp(events[0]["timestamp"], "mission created") + timedelta(seconds=timeout),
                            state.parse_timestamp(current["admission_policy"]["deadline_at"], "admission deadline"))
        with fleet_safe_paths.RootedFS(self.runs) as fs:
            objective = fs.read_regular(self.rel / "objective.txt", directory_modes=(0o700, 0o700),
                                        max_bytes=16 * 1024 * 1024)
        if state.artifact_id(objective) != current["objective_sha256"]:
            raise HerdrMissionError("durable objective digest mismatch")
        self.objective = objective.decode("utf-8")
        # Integrity is recorded, not raised here: an already-authorized exact
        # cancellation must still reconcile an owned run whose SDD blob was
        # later corrupted. New admissions/dispatch fail closed in execute/turn.
        self.sdd_packet = None
        self.sdd_error = None
        try:
            self.sdd_packet = self._sdd_integrity(current)
        except state.MissionStateError as exc:
            self.sdd_error = str(exc)
        self.scope_error = None
        try:
            fleet_herdr_scope.validate_binding(current, self.options)
        except fleet_herdr_scope.ScopeError as exc:
            self.scope_error = str(exc)
        self.repair_error = None
        try:
            fleet_herdr_repair_policy.validate_binding(current, self.options)
        except fleet_herdr_repair_policy.RepairPolicyError as exc:
            self.repair_error = str(exc)
        contract = self.options.get("acceptance_contract")
        fleet_acceptance.check_binding(creation["idempotency_key"], contract)
        functional_spec = self.options.get("functional_contract")
        functional_policy = current.get("functional_policy")
        if (bool(functional_spec) != bool(functional_policy) or
                (functional_spec is not None and fleet_functional.digest(fleet_functional.validate(functional_spec)) != functional_policy["spec_artifact_id"])):
            raise HerdrMissionError("functional policy/runtime options binding mismatch")
        if contract is not None:
            fleet_acceptance.validate(contract)
        try:
            self.profile = fleet_herdr_profile.validate_profile_binding(
                self.compiled, self.options, current)
            fleet_herdr_profile.validate_creation_binding(
                self.compiled, creation.get("request"))
        except fleet_herdr_profile.ProfileError as exc:
            raise HerdrMissionError(str(exc)) from exc
        self.stages = self.profile.stages
        if self.compiled["workflow"].get("assurance", {}).get("profile") != "none":
            raise HerdrMissionError("Herdr driver requires assurance none")
        resolved = self.compiled["resolved"]
        self.members = [member for member in [resolved.get("lead"), *resolved["instances"]]
                        if member is not None]
        self.session = self.options.get("herdr_session")
        if not isinstance(self.session, str) or not self.session.strip():
            raise HerdrMissionError("durable herdr_session is required; no implicit session fallback")
        self.candidate = fleet_herdr_runtime.candidate_path(
            self.runs, self.mid, self.options, Path(current["target_repo"]))
        if self.options.get("herdr_capsule_manifest") is not None:
            import fleet_mission_capsule
            fleet_mission_capsule.validate_manifest(self.options["herdr_capsule_manifest"])
            if self.options.get("herdr_launch_manifest") is not None:
                raise HerdrMissionError("capsule and legacy launch cannot be combined")
        if self.options.get("herdr_launch_manifest") is not None:
            if self.options.get("herdr_layout") is None:
                raise HerdrMissionError("observed launch requires Herdr layout v2")
            fleet_herdr_launch.validate_manifest(self.options["herdr_launch_manifest"])

    def _sdd_integrity(self, current: dict[str, Any]) -> dict[str, Any] | None:
        """Bind the optional creation receipt pin to the ledger and snapshot.

        Presence and absence must agree: a removed creation pin is a mismatch
        even when the ledger and snapshot still hold a valid digest.
        """
        creation = self.read("creation-request.json")
        request = creation.get("request") if isinstance(creation, dict) else None
        request_digest = request.get("sdd_plan_sha256") if isinstance(request, dict) else None
        ledger_digest = current.get("sdd_plan_sha256")
        if request_digest != ledger_digest:
            raise state.MissionStateError(
                "creation-request SDD pin differs from the immutable ledger binding"
            )
        return fleet_herdr_sdd.verify(self.runs, current)

    def revalidate_sdd(self) -> bool:
        """Re-check frozen evidence before any new admission or dispatch.

        Returns ``True`` for a valid or legacy Mission. On failure it stores
        the error and clears the packet so no stale plan can be used. It never
        runs during cancellation, pause or result-only observation paths.
        """
        try:
            self.sdd_packet = self._sdd_integrity(self.current())
            self.sdd_error = None
            return True
        except state.MissionStateError as exc:
            self.sdd_packet = None
            self.sdd_error = str(exc)
            return False

    def remaining_seconds(self) -> float:
        return (self.deadline - datetime.now(timezone.utc)).total_seconds()

    def observe_backend(self, backend, method, *args, **kwargs):
        callback = lambda: getattr(backend, method)(*args, **kwargs)
        if "herdr_control" not in self.current():
            return callback()
        kind = "controller_wait" if method == "wait" else "controller_operation"
        run_id = args[0] if args and method in {"wait", "submit", "recover", "collect_result", "cancel"} else None
        return metrics.observe(self.runs, self.mid, kind, callback, run_id)

    def control_stop(self, backend=None):
        """Reconcile owned work before acknowledging pause/cancellation."""
        current = self.current()
        view = control.view(current)
        request = view["requests"].get(view["latest"])
        if not request:
            return None
        if request["action"] == "resume":
            if request["applied_at"] is None:
                control.acknowledge(self.runs, self.mid, request)
            return None
        if request["applied_at"] is not None:
            if request["action"] == "cancel":
                state.append_terminal(self.runs, self.mid, status="abandoned", reason="Herdr Mission cancellation confirmed",
                    idempotency_key="herdr:control-terminal")
                return self.finish(self.response(control=control.view(self.current())))
            return self.response(control=control.view(self.current()), next_action=view["applied"])
        active = [a for a in current["admissions"].values() if a["active"]]
        if active and backend is None:
            backend = self.backend()
        if request["action"] == "cancel" and request["generation"] is not None:
            observed_generation = (backend.state().get("generation") if backend is not None
                else control.backend_generation(self.runs, self.mid))
            if observed_generation != request["generation"]:
                return self.response(control=view, next_action="cancel requested; owned generation changed, no signal sent")
        for admission in active:
            if request["run_id"] and admission["run_id"] != request["run_id"]:
                # Reconcile the selected run regardless of admission order,
                # without extending cancellation to other owned runs.
                continue
            unsent = admission["phase"] in {"reserved", "committed"} or (
                admission["phase"] == "authorized" and control.fresh_authorization(current, admission)
                and admission["run_id"] not in view["dispatches"])
            if unsent:
                if request["action"] == "pause":
                    continue
                if admission["phase"] in {"reserved", "committed"}:
                    binding = {k: admission[k] for k in ("admission_id", "run_id", "request_digest", "effect_sha256", "task_sha256", "recipient_instance", "writer")}
                    fleet_admission.abort_prelaunch(self.runs, self.mid, **binding, reason=request["reason"],
                        idempotency_key="herdr:control-abort:" + admission["run_id"])
                else:
                    proof = fleet_artifacts.put_bytes(self.runs, self.mid, state.canonical_bytes({
                        "mission_id": self.mid, "run_id": admission["run_id"], "request": request["event_sha256"],
                        "status": "abandoned", "reason": "supervised authorization has no dispatch intent"}))
                    fleet_admission.finalize(self.runs, self.mid, admission_id=admission["admission_id"],
                        recipient_instance=admission["recipient_instance"], writer=admission["writer"], reason=request["reason"],
                        terminal_evidence={"schema_version": 1, "source_event_sha256": proof["artifact_id"],
                            "run_id": admission["run_id"], "task_sha256": admission["task_sha256"], "status": "abandoned"},
                        idempotency_key="herdr:control-unsent:" + admission["run_id"])
                continue
            stage = admission["request_key"].removeprefix("herdr:")
            row = next((s for s in self.stages if s[0] == stage), None)
            if row is None:
                raise HerdrMissionError("control found an unsupported owned admission")
            task = state.loads_strict(fleet_artifacts.get_bytes(self.runs, self.mid, admission["task_sha256"]))
            if request["action"] == "cancel" and admission["run_id"] not in self.current()["cancelled_runs"]:
                self.event("run_cancel_requested", "control-cancel:" + admission["run_id"],
                    {"run_id": admission["run_id"], "reason": request["reason"]})
            self.turn(backend, *row, task["input_artifact_ids"], task["frozen_candidate"], reconcile_only=True)
            if self.current()["status"] in state.TERMINAL_STATUSES:
                return self.finish(self.response(control=control.view(self.current())))
        current = self.current()
        if request["run_id"] and any(a["active"] and a["run_id"] != request["run_id"]
                                     for a in current["admissions"].values()):
            return self.response(control=control.view(current),
                next_action="cancel requested; other active runs remain outside cancellation scope")
        attempt = current.get("functional_attempt")
        if attempt and not attempt.get("result"):
            # A prior physical run was lost. run() cleans only its exact attempt
            # and records indeterminate; it never starts that candidate again.
            fleet_functional.run(self.runs, self.mid, self.read("candidate-freeze.json"))
        cleanup_proof = None
        attempt = self.current().get("functional_attempt")
        if attempt and attempt.get("result"):
            receipt = state.loads_strict(fleet_artifacts.get_bytes(self.runs, self.mid, attempt["result"]["receipt_artifact_id"]))
            if "cleanup_unconfirmed" in receipt["reason"]:
                # The old execution receipt stays immutable. Exact cleanup is
                # reconciled separately and is required before acknowledging.
                try:
                    runner = fleet_functional.runner
                    docker = runner.Docker()
                    contract = state.loads_strict(fleet_artifacts.get_bytes(self.runs, self.mid, attempt["contract_artifact_id"]))
                    if (runner.sha(docker.endpoint.encode()) != contract["environment"]["runtime"]["docker_endpoint_sha256"]
                            or not docker.cleanup("fleet-functional-" + attempt["attempt_id"], attempt["attempt_id"])):
                        raise HerdrMissionError("functional container cleanup remains unconfirmed")
                    cleanup_proof = fleet_artifacts.put_bytes(self.runs, self.mid, state.canonical_bytes({
                        "scope": "controller_observed_exact_container_absence", "mission_id": self.mid,
                        "attempt_id": attempt["attempt_id"], "docker_endpoint_sha256": runner.sha(docker.endpoint.encode()),
                        "cleanup_confirmed": True}))["artifact_id"]
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    return self.response(control=control.view(self.current()), next_action="control requested; " + str(exc))
        try:
            control.acknowledge(self.runs, self.mid, request, cleanup_proof=cleanup_proof)
        except state.MissionConflict:
            return self.response(control=control.view(self.current()), next_action="control requested; reconciliation pending")
        if request["action"] == "cancel":
            state.append_terminal(self.runs, self.mid, status="abandoned", reason="Herdr Mission cancellation confirmed",
                                  idempotency_key="herdr:control-terminal")
            return self.finish(self.response(control=control.view(self.current())))
        return self.response(control=control.view(self.current()), next_action="paused; deadline remains unchanged")

    def functional_interrupt(self):
        if control.view(self.current())["desired"] == "cancel_requested":
            return "mission_cancel_requested"
        if self.remaining_seconds() <= 0:
            return "mission_deadline_expired"
        return None

    def completed_turns(self) -> bool:
        admissions = self.current()["admissions"].values()
        completed = {a["request_key"] for a in admissions if a["phase"] == "finalized"
                     and a.get("result") is not None and a["terminal"]["status"] == "succeeded"}
        return all(f"herdr:{stage}" in completed for stage, _, _ in self.stages)

    def timed_out(self, backend: Any = None) -> dict[str, Any]:
        """Enforce the durable deadline when driven; no background watchdog is implied."""
        for stage, instance, capability in self.stages:
            current = self.current()
            admission = next((a for a in current["admissions"].values()
                              if a["request_key"] == f"herdr:{stage}" and a["active"]), None)
            if admission is None:
                continue
            view = control.view(current)
            if (admission["phase"] == "authorized" and view["enabled_sequence"] is not None
                    and control.fresh_authorization(current, admission) and admission["run_id"] not in view["dispatches"]):
                proof = fleet_artifacts.put_bytes(self.runs, self.mid, state.canonical_bytes({
                    "mission_id": self.mid, "run_id": admission["run_id"], "status": "abandoned",
                    "reason": "deadline expired before supervised dispatch intent"}))
                fleet_admission.finalize(self.runs, self.mid, admission_id=admission["admission_id"],
                    recipient_instance=instance, writer=admission["writer"], reason="deadline expired before dispatch",
                    terminal_evidence={"schema_version": 1, "source_event_sha256": proof["artifact_id"],
                        "run_id": admission["run_id"], "task_sha256": admission["task_sha256"], "status": "abandoned"},
                    idempotency_key="herdr:deadline-unsent:" + admission["run_id"])
                continue
            # A result may be durable in the backend (or Mission ledger) even
            # when its admission has not been finalized. Consume it before any
            # cancellation intent; never infer failure from the expired clock.
            if admission["phase"] in {"authorized", "started"} and self.result_rejection(backend, admission["run_id"]) is None:
                try:
                    raw = None if admission.get("result") is not None else self.observe_backend(backend, "collect_result", admission["run_id"])
                    if admission.get("result") is not None or raw is not None:
                        task = state.loads_strict(fleet_artifacts.get_bytes(self.runs, self.mid, admission["task_sha256"]))
                        completed = self.turn(backend, stage, instance, capability,
                            task["input_artifact_ids"], task["frozen_candidate"],
                            result_only=admission["run_id"] not in current["cancelled_runs"], durable_result=raw)
                        if completed is None:
                            if self.current()["status"] in state.TERMINAL_STATUSES:
                                return self.finish(self.response(terminal=self.current()["terminal"]))
                            return self.response(next_action="deadline expired; durable result reconciliation remains pending")
                        if completed["status"] != "PASS" and not any(a["active"] for a in self.current()["admissions"].values()):
                            state.append_terminal(self.runs, self.mid, status=RESULT_STATUS[completed["status"]],
                                reason=f"Herdr {stage}: {completed['summary']}", idempotency_key=f"herdr:{stage}:terminal")
                            return self.finish(self.response())
                        continue
                except fleet_herdr.ExecutionEvidenceRejected as exc:
                    return self.evidence_block(exc.proof)
                except fleet_herdr.HerdrBackendError as exc:
                    return self.response(next_action=f"deadline expired; reconcile durable result before cancel: {exc}")
            if admission["phase"] in {"reserved", "committed"}:
                binding = {k: admission[k] for k in ("admission_id", "run_id", "request_digest", "effect_sha256",
                                                      "task_sha256", "recipient_instance", "writer")}
                fleet_admission.abort_prelaunch(self.runs, self.mid, **binding, reason="Herdr mission deadline expired",
                                               idempotency_key=f"herdr:{stage}:timeout-abort")
            elif backend is not None:
                if admission["run_id"] not in current["cancelled_runs"]:
                    self.event("run_cancel_requested", f"{stage}:timeout-cancel",
                        {"run_id": admission["run_id"], "reason": "Herdr mission deadline expired"})
                try:
                    self.turn(backend, stage, instance, capability, [], None)
                except fleet_herdr.HerdrBackendError as exc:
                    return self.response(next_action=f"deadline expired; reconcile cancellation: {exc}")
        current = self.current()
        if current["status"] in state.TERMINAL_STATUSES:
            return self.finish(self.response(terminal=current["terminal"]))
        if self.completed_turns():
            return self.execute()  # CAS/archive only: every turn is already finalized
        if not any(a["active"] for a in current["admissions"].values()):
            state.append_terminal(self.runs, self.mid, status="failed", reason="Herdr mission deadline expired",
                                  idempotency_key="herdr:timeout-terminal")
            return self.finish(self.response())
        return self.response(next_action="deadline expired; cancellation remains pending with active admission")

    def risk_gate(self) -> dict[str, Any] | None:
        current = self.current()
        if current["status"] == "compiled":
            spec = importlib.util.spec_from_file_location("herdr_mission_risk", ROOT / "scripts/fleet-risk.py")
            assert spec and spec.loader
            risk = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(risk)
            assessment = risk.assess(workflow_minimum=self.compiled["workflow"]["risk"]["minimum"],
                objective=self.objective, target=current["target_repo"], repository_root=current["target_repo"],
                override=self.options.get("risk_override", "auto"))
            self.event("risk_assessed", "risk", assessment)
            if state.RISK_ORDER[assessment["level"]] > state.RISK_ORDER[current["risk"]]:
                self.event("risk_escalated", "risk-escalated", {"from": current["risk"],
                    "to": assessment["level"], "categories": assessment["categories"],
                    "reason": "deterministic Herdr mission risk assessment"})
            current = self.current()
            if current["risk"] in {"high", "unknown"}:
                self.event("assurance_requested", "assurance", {"risk": current["risk"],
                    "categories": assessment["categories"], "scope": current["target_repo"],
                    "workflow_digest": current["workflow_digest"]})
        current = self.current()
        if current["risk"] in {"high", "unknown"} or current["status"] in {
                "awaiting_assurance_confirmation", "assurance_approved", "assured_booting", "assured_running"}:
            return self.response(next_action="blocked: astra_sol assurance none cannot execute high/unknown risk; no CMUX fallback")
        if current.get("pending_decisions"):
            return self.response(next_action="resolve durable pending decisions before continuing")
        return None

    def prepare_candidate(self) -> None:
        current = self.current()
        binding = {"schema_version": 1, "mission_id": self.mid, "target_repo": current["target_repo"],
                   "base_sha": current["base_sha"], "compiled_digest": current["compiled_digest"],
                   "candidate_repo": str(self.candidate)}
        prior = self.read("herdr-candidate.json", optional=True)
        if prior is not None:
            if {k: v for k, v in prior.items() if k != "identity"} != binding:
                raise HerdrMissionError("candidate receipt binding mismatch")
            self.check_candidate(prior)
            return
        target = Path(current["target_repo"])
        if str(target.resolve(strict=True)) != str(target) or _git(target, "rev-parse", "--show-toplevel") != str(target):
            raise HerdrMissionError("target must be the exact physical Git toplevel")
        if _git(target, "rev-parse", "HEAD") != current["base_sha"]:
            raise HerdrMissionError("target HEAD differs from durable baseline")
        if _git(target, "status", "--porcelain=v1", "--untracked-files=all", "--ignored"):
            raise HerdrMissionError("dirty baseline requires exact snapshot support; refusing before effects")
        if any(entry and not entry.startswith("H ") for entry in _git(target, "ls-files", "-v", "-z").split("\0")):
            raise HerdrMissionError("baseline index flags can hide changes; exact snapshot required")
        if any(line.startswith("160000 ") for line in _git(target, "ls-tree", "-r", "HEAD").splitlines()):
            raise HerdrMissionError("submodule baseline requires snapshot support")
        if self.candidate.exists() or self.candidate.is_symlink() or self.read("herdr-candidate-intent.json", optional=True):
            raise HerdrMissionError("candidate preparation indeterminate; existing resources will not be overwritten")
        self.write("herdr-candidate-intent.json", binding)
        fleet_herdr_runtime.prepare_parent(self.candidate, self.options)
        _git(target, "clone", "--no-local", "--no-hardlinks", "--", str(target), str(self.candidate))
        self.candidate.chmod(0o700)
        if (_git(target, "rev-parse", "HEAD") != current["base_sha"]
                or _git(target, "status", "--porcelain=v1", "--untracked-files=all", "--ignored")
                or _git(self.candidate, "rev-parse", "HEAD") != current["base_sha"]
                or _git(self.candidate, "status", "--porcelain=v1", "--untracked-files=all", "--ignored")):
            raise HerdrMissionError("baseline changed during candidate preparation")
        self.write("herdr-candidate.json", {**binding, "identity": self.candidate_identity()})
        self.check_candidate()

    def candidate_identity(self) -> dict[str, int]:
        if self.candidate.is_symlink() or not self.candidate.is_dir():
            raise HerdrMissionError("candidate must remain an owned physical directory")
        gitdir = self.candidate / ".git"
        if gitdir.is_symlink() or not gitdir.is_dir():
            raise HerdrMissionError("candidate .git must remain an independent physical directory")
        repo_info, git_info = self.candidate.stat(), gitdir.stat()
        return {"dev": repo_info.st_dev, "ino": repo_info.st_ino,
                "git_dev": git_info.st_dev, "git_ino": git_info.st_ino,
                **fleet_herdr_runtime.parent_identity(self.candidate, self.options)}

    def check_candidate(self, receipt: Any = None) -> None:
        receipt = receipt if receipt is not None else self.read("herdr-candidate.json")
        if receipt.get("identity") != self.candidate_identity():
            raise HerdrMissionError("candidate filesystem identity changed")
        if (_git(self.candidate, "rev-parse", "--show-toplevel") != str(self.candidate)
                or _git(self.candidate, "rev-parse", "--absolute-git-dir") != str(self.candidate / ".git")):
            raise HerdrMissionError("candidate Git repository escaped its owned directory")
        if _git(self.candidate, "rev-parse", "HEAD") != receipt["base_sha"]:
            raise HerdrMissionError("candidate HEAD drift; commits are not authorized")

    def backend(self) -> Any:
        if self.options.get("herdr_capsule_manifest") is not None:
            from fleet_mission_capsule import CapsuleBackend
            return CapsuleBackend(self.runs, self.mid, feature=self.current()["feature"],
                target_repo=self.candidate, compiled=self.compiled, session=self.session,
                manifest=self.options["herdr_capsule_manifest"])
        signature = inspect.signature(fleet_herdr.HerdrBackend)
        if "session" not in signature.parameters or not callable(getattr(fleet_herdr.HerdrBackend, "collect_result", None)):
            raise HerdrMissionError("backend integration requires explicit session and collect_result(run_id)")
        launch_options = {}
        if self.options.get("herdr_personal_cli") is not None:
            from fleet_herdr_personal import PROFILE
            if self.options["herdr_personal_cli"] != PROFILE:
                raise HerdrMissionError("unknown personal CLI profile")
            launch_options["personal_cli"] = True
        if self.options.get("herdr_launch_manifest") is not None:
            home = self.root / "herdr-launch" / "controller"
            launch_options = {"launch_manifest": self.options["herdr_launch_manifest"],
                "environment": {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                    "XDG_STATE_HOME": str(home / "state"), "XDG_CACHE_HOME": str(home / "cache"),
                    "CODEX_HOME": str(home / "codex-home"), "TMPDIR": str(home / "tmp"),
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TERM": "xterm-256color", "LANG": "en_US.UTF-8"}}
        backend = fleet_herdr.HerdrBackend(self.runs, self.mid, feature=self.current()["feature"],
            target_repo=self.candidate, compiled=self.compiled, session=self.session, **launch_options)
        backend.observation_deadline = getattr(self, "observation_deadline", None)
        return backend

    def freeze(self) -> dict[str, Any]:
        self.check_candidate()
        value = _archive().freeze(self.runs, self.mid, self.candidate)
        if (not isinstance(value, dict) or not state.GIT_OID.fullmatch(str(value.get("tree_sha", "")))
                or any(not state.SHA256.fullmatch(str(value.get(k, "")))
                       for k in ("tree_artifact_id", "patch_artifact_id"))):
            raise HerdrMissionError("invalid frozen candidate receipt")
        for key in ("tree_artifact_id", "patch_artifact_id"):
            fleet_artifacts.get_bytes(self.runs, self.mid, value[key])
        self.write("herdr-freeze.json", value)
        return value

    def research_snapshot(self) -> dict[str, Any]:
        if self.profile is not fleet_herdr_profile.RESEARCH:
            raise HerdrMissionError("investigated snapshot is available only to the Research profile")
        self.check_candidate()
        archive = _archive()
        value = archive.freeze_research(self.runs, self.mid, self.candidate,
                                        profile_digest=self.profile.digest)
        archive.verify_research_snapshot(self.runs, self.mid, value,
                                         expected_profile_digest=self.profile.digest)
        return value

    def verify_research_authority(self, snapshot: dict[str, Any], *, before_first_build: bool) -> None:
        """Bind the mutable receipt to Research task CAS and, once, to the physical tree."""
        current = self.current()
        admissions = [a for a in current["admissions"].values()
                      if a["request_key"] == "herdr:research"]
        if len(admissions) != 1:
            raise HerdrMissionError("Build requires one durable Research admission")
        admission = admissions[0]
        if (admission["phase"] != "finalized" or admission["terminal"]["status"] != "succeeded"
                or not admission.get("result")):
            raise HerdrMissionError("Build requires successful durable Research evidence")
        task = state.loads_strict(fleet_artifacts.get_bytes(
            self.runs, self.mid, admission["task_sha256"]))
        if (task.get("stage") != "research" or task.get("investigated_snapshot") != snapshot
                or task.get("frozen_candidate") != snapshot
                or task.get("result_contract", {}).get("candidate_tree_sha") != snapshot["tree_sha"]):
            raise HerdrMissionError("Research task CAS does not authorize the investigated snapshot")
        result = state.loads_strict(fleet_artifacts.get_bytes(
            self.runs, self.mid, admission["result"]["artifact_id"]))
        if result.get("candidate_tree_sha") != snapshot["tree_sha"]:
            raise HerdrMissionError("Research result does not bind the investigated snapshot")
        if before_first_build:
            observed_sha, _, _ = _archive().snapshot(
                self.candidate, expected_base=current["base_sha"])
            if observed_sha != snapshot["tree_sha"]:
                raise HerdrMissionError("candidate changed after Research; refusing first Build admission")

    def prompt(self, stage: str, instance: str, run_id: str, inputs: list[str], frozen: Any) -> str:
        research_profile = self.profile is fleet_herdr_profile.RESEARCH
        synthesis_criteria = (
            "PASS when you reconcile the Plan, Research, Build, Review and Verify results against "
            "the frozen candidate and acceptance criteria, including the Research source evidence, "
            "with supported conclusions and residual risks. Do not wait for the controller's "
            "subsequent archive or terminal verdict."
            if research_profile else
            "PASS when you reconcile the available Worker, Reviewer and Verifier results against "
            "the frozen candidate and acceptance criteria, with supported conclusions and residual risks. "
            "Do not wait for the controller's subsequent archive or terminal verdict."
        )
        stage_instructions = (
            "Plan: provide a bounded implementation plan. Research: answer material uncertainties with "
            "pinned source evidence and concrete negative tests. Build: implement and run focused tests "
            "using the bounded Plan/Research handoff, and identify which findings were used or discarded "
            "in the summary. Review: inspect the complete change for actionable defects, binding errors, "
            "risks and omitted cases; explicitly report none when none are found. Verify: independently "
            "reproduce acceptance and evidence against the same frozen tree, using temporary outputs and "
            "without modifying candidate. Synthesis: reconcile discrepancies, evidence limits and the "
            "Plan, Research, Build, Review and Verify results, including Research source evidence. "
            if research_profile else
            "Plan: provide a bounded implementation plan. Build: implement and run focused tests using Plan. "
            "Review: inspect contracts and diff. Verify: independently validate with temporary outputs, "
            "without modifying candidate. Synthesis: reconcile all three role results, report evidence "
            "and residual risks. "
        )
        stage_success_criteria = {
            "plan": ("PASS when you provide a viable bounded implementation plan and actual baseline evidence "
                "from existing candidate files. Expected failing baseline tests or an unimplemented acceptance "
                "criterion are inputs to the plan, not blockers. Build, Review and Verify are future stages; "
                "their absence does not prevent plan PASS."),
            "build": ("PASS when you implement the assigned change as the sole writer and run relevant focused "
                "tests with evidence that the implementation meets the acceptance criteria. Do not wait for "
                "Review, Verify or Synthesis."),
            "research": ("PASS when your read-only investigation supplies concrete source evidence relevant to "
                "material questions in the objective against the controller-pinned investigated snapshot. Include "
                "source locations/hashes, bounded recommendations and negative tests. Do not modify candidate files, "
                "make implementation decisions on behalf of Build, or wait for later stages."),
            "review": ("PASS when your read-only review of the frozen candidate contracts and complete diff finds no "
                "blocking defect in bindings, compatibility, acceptance gates, sole-writer enforcement, duplicate-send "
                "recovery or scope. Report actionable defects as FAIL, or explicitly report none; do not repeat Research "
                "or wait for Verify/Synthesis."),
            "verify": ("PASS when you independently reproduce the required behavior and validate acceptance and its "
                "evidence on the exact frozen candidate tree, using temporary outputs without changing candidate files. "
                "Report reproduced failures as FAIL; do not wait for Synthesis."),
            "synthesis": synthesis_criteria,
        }[stage]
        task = {"schema_version": 1, "mission_id": self.mid, "run_id": run_id, "stage": stage,
            "instance_id": instance, "objective": self.objective, "candidate_repo": str(self.candidate),
            "input_artifact_ids": inputs, "artifact_store": str(self.root / "artifacts"),
            "acceptance_contract": self.options.get("acceptance_contract"),
            "stage_success_criteria": stage_success_criteria,
            "project_instructions": fleet_herdr_instructions.packet(self.runs, self.mid, self.candidate, self.current()),
            "role_guidance": fleet_herdr_role_guidance.packet(self.runs, self.mid, self.current(), instance),
            "frozen_candidate": frozen, "writer": stage == "build",
            "instructions": ("When role_guidance is present, use its explicit role contract and selected skill content "
                "under their stated conditions; do not assume global instructions or skills were inherited. "
                "Only Worker/build may change candidate files. Never commit, push, deploy, "
                "change global configuration, control other agents, or edit the source checkout. "
                + stage_instructions +
                "Status judges ONLY your assigned stage using stage_success_criteria. "
                "PASS requires actual evidence for that stage, not completion of the entire mission. "
                "Do not wait for future stages or require their results to report PASS. "
                "BLOCKED means a real inability to complete your own assigned stage; FAIL means evidence "
                "shows its success criteria are not met. Inability to self-verify your model or reasoning effort "
                "is not a blocker: the controller verifies model and effort from the transcript. Do not invent "
                "that identity evidence or make future-stage completion a prerequisite. "
                "Final answer MUST be exactly one raw JSON object: no Markdown fences, no surrounding "
                "prose, and no memory citations or other text outside that JSON object. Copy schema_version, "
                "mission_id, run_id, instance_id and candidate_tree_sha literally from result_contract; "
                "candidate_tree_sha must be the exact frozen value, or null before freeze. "
                "Choose one literal status PASS, BLOCKED or FAIL. Supply actual artifact paths and hashes. "
                "Do not emit artifact_id, result_artifact_id, turn_id or evidence: those fields are reserved "
                "for the backend. Include any necessary citations within the JSON summary string."),
            "result_contract": {"schema_version": 1, "mission_id": self.mid, "run_id": run_id,
                "instance_id": instance, "status": "PASS|BLOCKED|FAIL", "summary": "nonempty evidence report",
                "artifacts": [{"path": "canonical relative candidate file", "sha256": "exact file SHA-256"}],
                "candidate_tree_sha": frozen["tree_sha"] if frozen else None}}
        if stage == "research":
            task["investigated_snapshot"] = frozen
            task["material_questions"] = [
                "Which source assumptions or lifecycle bindings could invalidate the requested change?",
                "Which existing real entry points and negative fixtures must the implementation reuse?",
                "What evidence remains unknown or cannot be inferred from model-authored summaries?",
            ]
        if (stage == "build" and self.options.get("herdr_handoff_policy") is not None):
            if self.options["herdr_handoff_policy"] != fleet_herdr_runtime.HANDOFF_POLICY:
                raise HerdrMissionError("unsupported Herdr handoff policy")
            try:
                task["input_evidence"] = fleet_herdr_runtime.bounded_handoff(
                    mission_id=self.mid, stage=stage, input_artifact_ids=inputs,
                    current=self.current(),
                    read_artifact=lambda digest: fleet_artifacts.get_bytes(self.runs, self.mid, digest))
            except fleet_herdr_runtime.RuntimeContractError as exc:
                raise HerdrMissionError(str(exc)) from exc
        if getattr(self, "sdd_packet", None) is not None:
            task["sdd_plan"] = self.sdd_packet
        if self.options.get("scope_contract") is not None:
            task["physical_scope"] = self.options["scope_contract"]
        if self.options.get("herdr_capsule_manifest") is not None:
            from fleet_mission_capsule import candidate_files
            task["capsule_context"] = {
                "candidate_files": {n: {"text": b.decode("utf-8"), "sha256": state.artifact_id(b)}
                    for n, b in candidate_files(self.candidate).items()},
                "prior_results": [state.loads_strict(fleet_artifacts.get_bytes(self.runs, self.mid, p)) for p in inputs],
                "execution_limits": "Work in your current directory. Host candidate and artifact_store paths are identity only and inaccessible. Only the installed Codex/code-mode host may execute; shell commands and candidate programs are denied. Use supplied file text for inspection and apply_patch for changes. Report BLOCKED if your stage requires unavailable execution; never claim tests ran."}
        return state.canonical_bytes(task).decode("utf-8")

    def validate_result(self, raw: Any, run_id: str, instance: str, frozen: Any, prompt_sha: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise HerdrMissionError("role result must be a JSON object")
        for key, expected in {"schema_version": 1, "mission_id": self.mid, "run_id": run_id,
                              "instance_id": instance}.items():
            if raw.get(key) != expected or (key == "schema_version" and type(raw[key]) is not int):
                raise HerdrMissionError(f"role result {key} binding mismatch")
        final_id = raw.get("artifact_id")
        if not isinstance(final_id, str) or not state.SHA256.fullmatch(final_id):
            raise HerdrMissionError("role result requires final text CAS artifact_id")
        final_text = fleet_artifacts.get_bytes(self.runs, self.mid, final_id).decode("utf-8")
        if not final_text.strip():
            raise HerdrMissionError("role final text is empty")
        authored = {k: v for k, v in raw.items() if k not in {"artifact_id", "result_artifact_id", "turn_id", "evidence"}}
        if state.loads_strict(final_text) != authored:
            raise HerdrMissionError("final text CAS differs from role result fields")
        if not isinstance(raw.get("turn_id"), str) or not raw["turn_id"].strip():
            raise HerdrMissionError("role result requires a bound Codex turn_id")
        evidence = raw.get("evidence")
        if evidence is not None and (not isinstance(evidence, dict)
                or evidence.get("herdr_session") != self.session or evidence.get("prompt_sha256") != prompt_sha):
            raise HerdrMissionError("backend evidence session/prompt binding mismatch")
        if raw.get("result_artifact_id") is not None:
            envelope = state.loads_strict(fleet_artifacts.get_bytes(self.runs, self.mid, raw["result_artifact_id"]))
            if envelope != {k: v for k, v in raw.items() if k != "result_artifact_id"}:
                raise HerdrMissionError("backend result envelope CAS binding mismatch")
        if frozen and raw.get("candidate_tree_sha") != frozen["tree_sha"]:
            raise HerdrMissionError("role result refers to a different frozen candidate")
        if not isinstance(raw.get("status"), str) or raw["status"] not in RESULT_STATUS or not isinstance(raw.get("summary"), str) or not raw["summary"].strip():
            raise RoleProtocolError("role result requires PASS/BLOCKED/FAIL and evidence summary")
        artifacts = raw.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts or len(artifacts) > 100:
            raise RoleProtocolError("role result requires 1..100 artifact checks")
        copied = []
        with fleet_safe_paths.RootedFS(self.candidate) as fs:
            for item in artifacts:
                if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                    raise RoleProtocolError("invalid role artifact contract")
                path = item["path"]
                if (not isinstance(path, str) or not path or "\x00" in path or PurePosixPath(path).is_absolute()
                        or any(p in {"", ".", "..", ".git"} for p in path.split("/"))):
                    raise RoleProtocolError("role artifact must be a safe candidate-relative path")
                try:
                    content = fs.read_regular(path, directory_modes=(None,) * (len(path.split("/")) - 1),
                        file_mode=stat.S_IMODE((self.candidate / path).lstat().st_mode),
                        max_bytes=fleet_artifacts.MAX_ARTIFACT_BYTES)
                except (OSError, fleet_safe_paths.SafePathError) as exc:
                    raise RoleProtocolError(f"role artifact is unavailable or unsafe: {path}") from exc
                if state.artifact_id(content) != item["sha256"]:
                    raise RoleProtocolError("role artifact digest mismatch")
                copied.append(fleet_artifacts.put_bytes(self.runs, self.mid, content)["artifact_id"])
        normalized = {k: v for k, v in raw.items() if k != "result_artifact_id"}
        return {**normalized, "backend_result_artifact_id": raw.get("result_artifact_id"),
                "evidence_artifact_ids": copied}

    def verify_result_evidence(self, raw, instance, prompt_sha):
        try:
            fleet_herdr_evidence.verify_result(raw,
                read_artifact=lambda digest: fleet_artifacts.get_bytes(self.runs, self.mid, digest),
                role=instance, cwd=str(self.candidate), prompt_sha256=prompt_sha,
                capsule_manifest=self.options.get("herdr_capsule_manifest"), current=self.current(),
                permission_version=self.profile.permissions_policy_version)
        except fleet_herdr_evidence.EvidenceError as exc:
            raise fleet_herdr.HerdrBackendError(f"role permission evidence: {exc}") from exc

    def evidence_block(self, proof):
        self.evidence_rejection = proof
        rejected = ("unbound final delivery rejected" if proof.get("kind") == fleet_herdr_rejection.DELIVERY_KIND
                    else "execution evidence rejected")
        return self.response(next_action=(
            rejected + "; pause cannot be confirmed while the admission remains active; "
            "exact execution closure requires explicit cancellation; frozen task cannot be replayed or upgraded"),
            recovery="blocked", deadline_expired=self.remaining_seconds() <= 0)

    def execution_rejection(self, run_id):
        with fleet_safe_paths.RootedFS(self.runs) as fs:
            stored = fleet_herdr.versions.read_state(fs, self.rel / "herdr-backend.json",
                mission_id=self.mid, compiled_digest=self.compiled["compiled_digest"])
        if stored is None:
            return None
        proof = fleet_herdr_rejection.load(self.runs, self.mid, run_id, stored)
        if proof is None:
            # An unbound final follows the same no-verdict, exact-cancel path.
            proof = fleet_herdr_rejection.load_delivery(self.runs, self.mid, run_id, stored)
        if proof is not None:
            admission = next((a for a in self.current()["admissions"].values() if a["run_id"] == run_id), None)
            submission = stored["submissions"][run_id]
            if (admission is None or admission["task_sha256"] != submission["prompt_sha256"]
                    or admission["recipient_instance"] != submission["instance_id"] or admission.get("result") is not None):
                raise HerdrMissionError("evidence rejection admission binding mismatch")
            self.evidence_rejection = proof
        return proof

    def result_rejection(self, backend, run_id):
        execution = self.execution_rejection(run_id)
        if execution is not None:
            return execution
        proof = fleet_herdr.load_result_rejection(self.runs, self.mid, run_id)
        if proof is None:
            return None
        admission = next(a for a in self.current()["admissions"].values() if a["run_id"] == run_id)
        if (proof["instance_id"] != admission["recipient_instance"]
                or proof["prompt_sha256"] != admission["task_sha256"] or admission.get("result") is not None):
            raise HerdrMissionError("role protocol rejection admission binding mismatch")
        raw = self.observe_backend(backend, "collect_result", run_id)
        if raw is None:
            raise HerdrMissionError("rejected result is no longer available")
        fleet_herdr.load_result_rejection(self.runs, self.mid, run_id, result=raw)
        self.verify_result_evidence(raw, admission["recipient_instance"], admission["task_sha256"])
        self.protocol_rejection = proof
        return proof

    def turn(self, backend: Any, stage: str, instance: str, capability: str,
             inputs: list[str], frozen: Any, *, result_only: bool = False,
             durable_result: dict[str, Any] | None = None, reconcile_only: bool = False) -> dict[str, Any] | None:
        ids = fleet_admission.deterministic_ids(self.mid, request_key=f"herdr:{stage}", run_kind="specialist")
        run_id = str(ids["run_id"])
        rejection = self.result_rejection(backend, run_id)
        if not result_only and run_id in self.current()["cancelled_runs"]:
            admission = self.current()["admissions"][str(ids["admission_id"])]
            if admission["phase"] != "finalized" and rejection is None:
                raw = durable_result
                if admission.get("result") is None and raw is None:
                    raw = self.observe_backend(backend, "collect_result", run_id)
                if admission.get("result") is not None or raw is not None:
                    # A crash may leave a completed backend turn with active ledger
                    # ownership. Preserve its actual verdict before honoring the
                    # mission cancellation; never signal an already completed turn.
                    self.turn(backend, stage, instance, capability, inputs, frozen,
                              result_only=True, durable_result=raw)
                    admission = self.current()["admissions"][str(ids["admission_id"])]
            if admission["phase"] != "finalized":
                observed = self.observe_backend(backend, "cancel", run_id)
                view = control.view(self.current())
                cancellation = view["requests"].get(view["latest"])
                if (not isinstance(observed, dict) or observed.get("status") != "abandoned"
                        or observed.get("run_id") != run_id or observed.get("instance_id") != instance
                        or observed.get("prompt_sha256") != admission["task_sha256"]
                        or observed.get("cancel_attempted") is not True
                        or cancellation and cancellation["generation"] is not None and observed.get("generation") != cancellation["generation"]):
                    self.pending_reason = "cancellation has no exact terminal proof; keep admission active"
                    return None
                # Like fleet_legacy_mission.terminal_evidence, this binds an external terminal
                # receipt, not a role PASS. Keep its exact bytes in CAS for replay.
                external = {"mission_id": self.mid, "run_id": run_id,
                    "task_sha256": admission["task_sha256"], "status": "abandoned", "backend_receipt": observed}
                proof = fleet_artifacts.put_bytes(self.runs, self.mid, state.canonical_bytes(external))
                fleet_admission.finalize(self.runs, self.mid, admission_id=admission["admission_id"],
                    recipient_instance=instance, writer=admission["writer"], reason="Herdr cancellation confirmed",
                    terminal_evidence={"schema_version": 1, "source_event_sha256": proof["artifact_id"],
                        "run_id": run_id, "task_sha256": admission["task_sha256"], "status": "abandoned"},
                    idempotency_key=f"herdr:{stage}:cancel-finalized")
            if not any(a["active"] for a in self.current()["admissions"].values()):
                view = control.view(self.current())
                request = view["requests"].get(view["latest"])
                if request and request["action"] == "cancel" and request["applied_at"] is None:
                    control.acknowledge(self.runs, self.mid, request)
                state.append_terminal(self.runs, self.mid, status="abandoned", reason="Herdr run cancellation confirmed",
                                      idempotency_key="herdr:cancel-terminal")
            return None  # a cancellation receipt is not a successful role result
        if rejection is not None:
            self.pending_reason = "role protocol rejected; cancellation requires separate exact execution closure"
            return None
        existing = self.current()["admissions"].get(str(ids["admission_id"]))
        if existing is None and (reconcile_only or control.view(self.current())["desired"] != "running"):
            self.pending_reason = "control request blocks new stage admission"
            return None
        if existing is None and not self.revalidate_sdd():
            raise HerdrMissionError(
                "SDD plan binding failed closed before new stage admission: " + str(self.sdd_error)
            )
        prompt = fleet_artifacts.get_bytes(self.runs, self.mid, existing["task_sha256"]).decode() if existing else self.prompt(stage, instance, run_id, inputs, frozen)
        limit = getattr(backend, "max_prompt_bytes", None)
        if existing is None and limit is not None and len(prompt.encode()) > limit:
            # Refuse before any admission or dispatch intent can make the send durable.
            raise HerdrMissionError(f"{stage} prompt is {len(prompt.encode())} bytes and exceeds {limit} bytes; "
                                    "reduce the objective or inputs before admission")
        task_sha = fleet_artifacts.put_bytes(self.runs, self.mid, prompt.encode())["artifact_id"]
        member = next(m for m in self.members if m["instance_id"] == instance)
        effect = state.sha256({"backend": "herdr", "session": self.session, "candidate_repo": str(self.candidate),
            "compiled_digest": self.compiled["compiled_digest"], "run_id": run_id,
            "instance_id": instance, "model": member["model"], "prompt_sha256": task_sha})
        request = {"request_key": f"herdr:{stage}", "run_kind": "specialist", "recipient_instance": instance,
                   "capability": capability, "effect_sha256": effect, "task_sha256": task_sha,
                   "delegated_budget": 0, "writer": stage == "build"}
        admission = fleet_admission.reserve_many(self.runs, self.mid, requests=[request],
                        idempotency_key=f"herdr:{stage}:reserve")["admissions"][0]
        binding = {k: admission[k] for k in ("admission_id", "request_digest", "effect_sha256",
                                             "recipient_instance", "writer", "run_id")}
        raw = durable_result
        if result_only and (admission["phase"] not in {"authorized", "started", "finalized"}
                            or (admission.get("result") is None and raw is None)):
            raise HerdrMissionError("result-only reconciliation requires an existing authorized result")
        if admission["phase"] in {"reserved", "committed"}:
            if self.remaining_seconds() <= 0 or reconcile_only or control.view(self.current())["desired"] != "running":
                return None
            commit = fleet_admission.commit(self.runs, self.mid, **binding, idempotency_key=f"herdr:{stage}:commit")
            if self.remaining_seconds() <= 0:
                return None
            auth = fleet_admission.authorize_launch(self.runs, self.mid, **binding,
                commit_event_sha256=commit["commit_event_sha256"], idempotency_key=f"herdr:{stage}:authorize")
            # Only the process which appends a new dispatch intent may submit.
            if self.remaining_seconds() <= 0:
                return None  # authorized ownership stays active until exact reconciliation
            observation = self.dispatch_or_recover(backend, run_id, prompt, instance, reconcile_only=reconcile_only)
        elif admission["phase"] in {"authorized", "started"} and admission.get("result") is None:
            if raw is None:
                raw = self.observe_backend(backend, "collect_result", run_id)  # durable result before runtime observation
            observation = None if raw is not None else self.dispatch_or_recover(backend, run_id, prompt, instance, reconcile_only=reconcile_only)
        else:
            observation = None
        admission = self.current()["admissions"][str(ids["admission_id"])]
        if admission.get("result") is not None:
            result = state.loads_strict(fleet_artifacts.get_bytes(self.runs, self.mid, admission["result"]["artifact_id"]))
            fleet_artifacts.get_bytes(self.runs, self.mid, result["artifact_id"])
            for digest in result["evidence_artifact_ids"]:
                fleet_artifacts.get_bytes(self.runs, self.mid, digest)
        else:
            if raw is None:
                raw = self.observe_backend(backend, "collect_result", run_id)
            if raw is None and (not isinstance(observation, dict) or observation.get("status") in {"indeterminate", "failed", "abandoned", "blocked", "not_sent"}):
                if isinstance(observation, dict) and observation.get("status") == "not_sent":
                    self.transport_rejection = {"run_id": run_id, "status": "not_sent", "reason": observation.get("reason")}
                    self.pending_reason = ("prompt command never started (not_sent); no prompt reached Herdr; "
                                           "exact cancellation closes this run; a corrected attempt needs a new Mission")
                return None
            if admission["phase"] == "authorized":
                fleet_admission.mark_started(self.runs, self.mid, **binding,
                    authorization_event_sha256=admission["launch_authorization"]["event_sha256"],
                    idempotency_key=f"herdr:{stage}:started")
            if raw is None:
                remaining_ms = int(max(0, min(self.wait_deadline - time.monotonic(), self.remaining_seconds())) * 1000)
                if remaining_ms < 1:
                    return None
                self.observe_backend(backend, "wait", run_id, timeout_ms=min(1000, remaining_ms))
                raw = self.observe_backend(backend, "collect_result", run_id)
            if raw is None:
                return None
            try:
                result = self.validate_result(raw, run_id, instance, frozen, task_sha)
            except RoleProtocolError as exc:
                # Content rejection is durable only after exact completed-result
                # evidence passes. It cannot stand in for a runtime terminal proof.
                self.verify_result_evidence(raw, instance, task_sha)
                observed = fleet_artifacts.put_bytes(self.runs, self.mid, state.canonical_bytes(raw))
                proof = fleet_artifacts.put_bytes(self.runs, self.mid, state.canonical_bytes({
                    "schema_version": 1, "kind": "herdr_role_protocol_rejection",
                    "mission_id": self.mid, "run_id": run_id, "instance_id": instance,
                    "prompt_sha256": task_sha, "observed_result_artifact_id": observed["artifact_id"],
                    "reason": str(exc)}))
                self.write(f"herdr-result-rejection-{run_id}.json", {"artifact_id": proof["artifact_id"]})
                raise
            stored = fleet_artifacts.put_bytes(self.runs, self.mid, state.canonical_bytes(result))
            metadata = {"run_id": run_id, "artifact_id": stored["artifact_id"], "provider": member["provider"],
                        "model": member["model"], "variant": member.get("variant")}
            if stage == "synthesis":
                self.event("synthesis_result_recorded", f"{stage}:result", {**metadata,
                    "admission_id": admission["admission_id"], "result_file": stored["path"]})
            else:
                self.event("delegation_registered", f"{stage}:registered", {
                    "delegation_id": admission["delegation_id"], "mission_id": self.mid, "run_id": run_id,
                    "parent_run_id": None, "delegated_by": "CONTROL", "recipient_instance": instance,
                    "capability": capability, "objective_sha256": task_sha, "input_artifact_ids": inputs,
                    "expected_output_contract": {"statuses": list(RESULT_STATUS), "artifacts_required": True},
                    "deadline": self.current()["admission_policy"]["deadline_at"], "depth": 0, "token_id": None,
                    "provider": member["provider"], "model": member["model"], "variant": member.get("variant")})
                self.event("result_recorded", f"{stage}:result", {**metadata, "delegation_id": admission["delegation_id"]})
            admission = self.current()["admissions"][str(ids["admission_id"])]
        self.verify_result_evidence(result, instance, task_sha)
        if admission["phase"] != "finalized":
            fleet_admission.finalize(self.runs, self.mid, admission_id=admission["admission_id"],
                recipient_instance=instance, writer=stage == "build", reason=result["summary"],
                terminal_evidence={"schema_version": 1, "source_event_sha256": admission["result"]["event_sha256"],
                    "run_id": run_id, "task_sha256": task_sha, "status": RESULT_STATUS[result["status"]]},
                idempotency_key=f"herdr:{stage}:finalized")
        return {**result, "result_artifact_id": admission["result"]["artifact_id"]}

    def dispatch_or_recover(self, backend, run_id, prompt, instance, *, reconcile_only=False):
        current = self.current()
        view = control.view(current)
        admission = next(a for a in current["admissions"].values() if a["run_id"] == run_id)
        if (admission["phase"] == "authorized" and view["enabled_sequence"] is not None
                and control.fresh_authorization(current, admission) and run_id not in view["dispatches"]):
            if reconcile_only or view["desired"] != "running":
                return {"status": "blocked", "reason": "control request precedes dispatch"}
            if not self.revalidate_sdd():
                # Includes resumed authorized-but-not-dispatched tasks: never
                # append a dispatch intent or submit under stale evidence.
                return {"status": "blocked", "reason": "SDD plan binding failed closed before dispatch"}
            try:
                _, appended = state.append_event(self.runs, self.mid, kind="herdr_dispatch_intent", actor="CONTROL",
                    idempotency_key="herdr:dispatch:" + run_id, payload={"run_id": run_id,
                        "task_sha256": admission["task_sha256"], "generation": backend.state()["generation"]})
            except state.MissionConflict:
                if control.view(self.current())["desired"] != "running":
                    return {"status": "blocked", "reason": "control request precedes dispatch"}
                raise
            if appended:
                return self.observe_backend(backend, "submit", run_id, prompt, instance_id=instance)
        return self.observe_backend(backend, "recover", run_id)  # intent can be ambiguous; never promise exactly-once

    def execute(self, *, observation_deadline=None) -> dict[str, Any]:
        # Terminal ledger state is returned before touching backend or candidate.
        current = self.current()
        if current["status"] in state.TERMINAL_STATUSES:
            return self.finish(self.response(terminal=current["terminal"]))
        self.wait_deadline = min(time.monotonic() + 30, observation_deadline) if observation_deadline is not None else time.monotonic() + 30
        self.observation_deadline = observation_deadline
        self.load()
        self.freeze_finalization_policy()
        # Rejected evidence is not a role result or proof of quiescence. Preserve
        # a pending pause even after expiry; only explicit cancellation may
        # reconcile this rejected execution. Never replay or upgrade its task.
        for admission in self.current()["admissions"].values():
            proof = self.execution_rejection(admission["run_id"])
            if proof is not None:
                if control.view(self.current())["desired"] == "cancel_requested":
                    # Validate the explicit request's generation/run scope before
                    # any deadline cancellation can touch the rejected execution.
                    return self.control_stop(self.backend())
                return self.evidence_block(proof)
        # A durable cancellation acknowledgement already proves quiescence.
        # Recover its terminal append even if the controller died before closure
        # and the deadline has since elapsed.
        requested = control.view(self.current())
        if requested["desired"] == "cancel_requested" and requested["applied"] == "cancelled":
            return self.control_stop()
        if self.remaining_seconds() <= 0 and self.current().get("functional_attempt", {}).get("result") is None and self.current().get("functional_attempt"):
            fleet_functional.run(self.runs, self.mid, self.read("candidate-freeze.json"))
        if self.remaining_seconds() <= 0 and any(a["active"] for a in self.current()["admissions"].values()):
            return self.timed_out(self.backend())
        if self.remaining_seconds() <= 0 and not any(a["active"] for a in self.current()["admissions"].values()) and not self.completed_turns():
            return self.timed_out()
        stopped = self.control_stop()
        if stopped is not None:
            return stopped
        # The existing high/unknown risk gate still governs recovery ordering.
        stopped = self.risk_gate()
        if stopped is not None:
            return stopped
        if (self.current()["status"] in {"completing", "archived"}
                and self.current().get("herdr_archive_selection")):
            # Selected durable evidence recovery must not depend on the live SDD
            # store; the archive verifier enforces SDD from archived bytes.
            archive = _archive()
            return self.complete_archive(archive, archive.recover(self.runs, self.mid))
        if self.current()["status"] in {"completing", "archived"} and self.archive_index_exists():
            archive = _archive()
            return self.complete_archive(archive, archive.verify(self.runs, self.mid,
                require_anchor=False, for_completion=True))
        if self.sdd_error is not None:
            # Exact cancellation/pause reconciliation above is already handled.
            # Any further candidate preparation, admission or dispatch stops here.
            return self.response(
                next_action="SDD plan binding failed closed before new effects: " + self.sdd_error
            )
        if self.scope_error is not None:
            return self.response(next_action="physical scope binding failed before new effects: " + self.scope_error,
                                 scope_rejection={"status": "rejected", "reason": self.scope_error})
        if self.repair_error is not None:
            return self.response(next_action="repair policy binding failed before new effects: " + self.repair_error)
        if self.current().get("repair_policy_sha256"):
            # The policy is admitted and frozen, but this driver has no repair
            # loop yet; running it would silently apply the non-repairing path.
            return self.response(next_action="blocked: repair policy is frozen but repair execution is "
                                             "not available; no candidate or agent is launched")
        if (self.current()["status"] in {"compiled", "booting", "running"} and self.remaining_seconds() <= 0
                and not self.completed_turns() and not any(a["active"] for a in self.current()["admissions"].values())):
            return self.timed_out()
        _archive()  # Fail before clone/boot when integration is unavailable.
        control.enable(self.runs, self.mid)
        self.prepare_candidate()
        scope_contract = self.options.get("scope_contract")
        if scope_contract is not None:
            if not self.current().get("herdr_scope_baseline") and _git(
                    self.candidate, "status", "--porcelain=v1", "--untracked-files=all", "--ignored"):
                raise HerdrMissionError("scope baseline changed before initial capture")
            fleet_herdr_scope.establish(self.runs, self.current(), self.candidate, scope_contract,
                [p for p in _git(self.candidate, "ls-files", "-z").split("\0") if p])
        # Validate bounded target instructions before booting any role process.
        fleet_herdr_instructions.packet(self.runs, self.mid, self.candidate, self.current())
        backend = self.backend()
        current = self.current()
        if current["status"] == "compiled":
            self.event("fleet_boot_started", "boot", {"feature": current["feature"], "preset": self.profile.preset})
        if self.current()["status"] == "booting":
            self.observe_backend(backend, "boot")
            self.event("mission_running", "running", {"manifest": str(self.root / "herdr-backend.json")})
        results: dict[str, Any] = {}
        inputs: list[str] = []
        completed_inputs: dict[str, str] = {}
        frozen = None
        for stage, instance, capability in self.stages:
            if self.observation_deadline is not None and time.monotonic() >= self.observation_deadline:
                return self.response(next_action="observation budget exhausted before next stage")
            stopped = self.control_stop(backend)
            if stopped is not None:
                return stopped
            current = self.current()
            if current["status"] not in {"running", "completing", "archived"}:
                return self.response(next_action="mission authority changed; reconcile before further effects")
            if current["status"] == "running" and self.remaining_seconds() <= 0 and not self.completed_turns():
                return self.timed_out(backend)
            if not any(a["request_key"] == f"herdr:{stage}"
                       for a in current["admissions"].values()):
                # New stage work (candidate check, freeze, functional check,
                # admission) must re-verify frozen evidence even if an earlier
                # stage corrupted it during this same drive. Stages that already
                # have an admission keep result-only/observation recovery paths.
                if not self.revalidate_sdd():
                    return self.response(
                        next_action="SDD plan binding failed closed before new stage work: "
                                    + str(self.sdd_error))
            self.check_candidate()
            inputs = fleet_herdr_runtime.role_inputs(
                stage, completed_inputs, self.options.get("herdr_input_policy"))
            if stage == "research":
                frozen = self.research_snapshot()
            elif stage == "build" and self.profile is fleet_herdr_profile.RESEARCH:
                snapshot = self.read("research-snapshot.json")
                _archive().verify_research_snapshot(self.runs, self.mid, snapshot,
                    expected_profile_digest=self.profile.digest)
                self.verify_research_authority(snapshot,
                    before_first_build=not any(a["request_key"] == "herdr:build"
                                               for a in current["admissions"].values()))
                frozen = None
            elif stage in {"review", "verify", "synthesis"}:
                frozen = self.freeze()
            functional_attempt = current.get("functional_attempt")
            new_functional = (stage == "synthesis" and current.get("functional_policy")
                              and not (isinstance(functional_attempt, dict)
                                       and functional_attempt.get("result")))
            if new_functional and not self.revalidate_sdd():
                # Gate a NEW functional check; durable receipt recovery (result
                # already present) and cancellation are not blocked here.
                return self.response(
                    next_action="SDD plan binding failed closed before new functional check: "
                                + str(self.sdd_error))
            if stage == "synthesis" and current.get("functional_policy"):
                try:
                    functional = metrics.observe(self.runs, self.mid, "controller_operation" if self.current().get("functional_attempt") else "functional_execution",
                        lambda: fleet_functional.run(self.runs, self.mid, frozen, interrupt=self.functional_interrupt))
                except state.MissionConflict:
                    stopped = self.control_stop(backend)
                    if stopped is not None:
                        return stopped
                    raise
                stopped = self.control_stop(backend)
                if stopped is not None:
                    return stopped
                if functional["status"] != "passed":
                    if functional["status"] == "failed":
                        state.append_terminal(self.runs, self.mid, status="failed",
                            reason="required functional tests failed", idempotency_key="functional:terminal")
                        return self.finish(self.response(functional=functional))
                    return self.response(functional=functional,
                        next_action="required functional check " + functional["status"] + "; reconcile without automatic replay")
                inputs.append(self.current()["functional_attempt"]["result"]["receipt_artifact_id"])
            self.pending_reason = None
            try:
                result = self.turn(backend, stage, instance, capability, inputs, frozen)
            except fleet_herdr.ExecutionEvidenceRejected as exc:
                return self.evidence_block(exc.proof)
            except fleet_herdr.HerdrBackendError as exc:
                # The authorization remains durable. Resuming can only recover it.
                return self.response(next_action=f"reconcile {stage} without resubmitting: {exc}")
            if result is None:
                if self.current()["status"] in state.TERMINAL_STATUSES:
                    return self.finish(self.response(terminal=self.current()["terminal"]))
                if self.remaining_seconds() <= 0:
                    return self.timed_out(backend)
                return self.response(next_action=self.pending_reason or
                    f"resume to recover {stage}; a durable role result is still required")
            completed_inputs[stage] = result["result_artifact_id"]
            if stage != "plan":
                results[instance] = result
            if result["status"] != "PASS":
                state.append_terminal(self.runs, self.mid, status=RESULT_STATUS[result["status"]],
                    reason=f"Herdr {stage}: {result['summary']}", idempotency_key=f"herdr:{stage}:terminal")
                return self.finish(self.response(role_results=results))
            if stage in {"plan", "research"} and not any(a["request_key"] == "herdr:build" for a in self.current()["admissions"].values()):
                if _git(self.candidate, "status", "--porcelain=v1", "--untracked-files=all", "--ignored"):
                    raise HerdrMissionError(f"{stage.title()} changed the candidate before writer admission")
            if stage in {"review", "verify", "synthesis"}:
                self.freeze()  # archive rejects any drift, including read-only role writes
        if self.profile is fleet_herdr_profile.MINIMAL:
            stopped = self.control_stop(backend)
            if stopped is not None:
                return stopped
            # The minimal profile has no Synthesis turn on which to hang closure.
            # Freeze and run the same external functional gate after its sole
            # writer has finalized; a Worker PASS is never the acceptance gate.
            frozen = self.freeze()
            current = self.current()
            if current.get("functional_policy"):
                new_functional = not (isinstance(current.get("functional_attempt"), dict)
                                      and current["functional_attempt"].get("result"))
                if new_functional and not self.revalidate_sdd():
                    return self.response(next_action="SDD plan binding failed closed before new functional check: "
                                         + str(self.sdd_error))
                try:
                    functional = metrics.observe(self.runs, self.mid,
                        "controller_operation" if current.get("functional_attempt") else "functional_execution",
                        lambda: fleet_functional.run(self.runs, self.mid, frozen,
                                                     interrupt=self.functional_interrupt))
                except state.MissionConflict:
                    stopped = self.control_stop(backend)
                    if stopped is not None:
                        return stopped
                    raise
                stopped = self.control_stop(backend)
                if stopped is not None:
                    return stopped
                if functional["status"] != "passed":
                    if functional["status"] == "failed":
                        state.append_terminal(self.runs, self.mid, status="failed",
                            reason="required functional tests failed",
                            idempotency_key="functional:terminal")
                        return self.finish(self.response(functional=functional))
                    return self.response(functional=functional,
                        next_action="required functional check " + functional["status"]
                                    + "; reconcile without automatic replay")
        if set(results) != self.profile.result_roles:
            raise HerdrMissionError("all selected Herdr profile role results are mandatory")
        if self.current()["status"] == "running":
            completion = ({"completion_artifact_id": results["worker"]["artifact_id"]}
                          if self.profile is fleet_herdr_profile.MINIMAL else
                          {"lead_artifact_id": results["lead"]["artifact_id"]})
            self.event("mission_completing", "completing", completion)
        archive = _archive()
        stopped = self.control_stop(backend)
        if stopped is not None:
            return stopped
        created = archive.create(self.runs, self.mid, self.candidate, results, backend.state())
        return self.complete_archive(archive, created, results)

    def archive_index_exists(self) -> bool:
        with fleet_safe_paths.RootedFS(self.runs) as fs:
            if "herdr-archive" not in fs.list_directory(self.rel, directory_modes=(0o700, 0o700)):
                return False
            return fs.read_regular_optional(self.rel / "herdr-archive" / "archive-index.json",
                directory_modes=(0o700, 0o700, 0o700), file_mode=0o600,
                max_bytes=16 * 1024 * 1024) is not None

    def freeze_finalization_policy(self) -> dict[str, Any]:
        current = self.current()
        options = self.read("runtime-options.json")
        if self.read("creation-request.json").get("runtime_options") != options:
            raise HerdrMissionError("finalization options differ from creation")
        self.event("herdr_finalization_policy_frozen", "finalization-policy",
                   fleet_herdr_permissions.finalization_policy(current["compiled_digest"],
                       capsule=options.get("herdr_capsule_manifest") is not None,
                       profile=self.profile))
        return self.current()["herdr_finalization_policy"]

    def completion_receipt_matches_policy(self, receipt: dict[str, Any], policy: dict[str, Any]) -> bool:
        proof = receipt.get("permissions", {})
        return (type(receipt.get("archive_schema_version")) is int
                and receipt["archive_schema_version"] >= policy["minimum_archive_schema_version"]
                and isinstance(proof, dict) and proof.get("status") == "attested"
                and type(proof.get("policy_version")) is int and type(proof.get("runs")) is int
                and proof.get("policy_version") == policy["permissions_policy_version"]
                and proof.get("runs") == policy["required_turns"]
                and receipt.get("finalization_policy_event_sha256") == policy["event_sha256"])

    def complete_archive(self, archive: Any, created: dict[str, Any], results: Any = None) -> dict[str, Any]:
        """Finalize staged durable evidence without requiring candidate or runtime."""
        current = self.current()
        if current["status"] in state.TERMINAL_STATUSES:
            return self.finish(self.response(terminal=current["terminal"]))
        stopped = self.control_stop()
        if stopped is not None:
            return stopped
        if (not isinstance(created, dict) or created.get("staged_valid") is not True
                or not state.SHA256.fullmatch(str(created.get("index_sha256", "")))
                or not isinstance(created.get("path"), str) or not Path(created["path"]).is_absolute()):
            raise HerdrMissionError("archive staging receipt is invalid")
        policy = self.freeze_finalization_policy()
        staged = archive.verify(self.runs, self.mid, require_anchor=False, for_completion=True)
        if (not isinstance(staged, dict) or staged.get("staged_valid") is not True
                or not self.completion_receipt_matches_policy(staged, policy)
                or any(staged.get(k) != created.get(k) for k in ("index_sha256", "path", "acceptance"))):
            raise HerdrMissionError("archive failed independent completion verification before anchor")
        # create verifies staging only. Publish its immutable ledger anchor before
        # calling the public verifier, which must require that anchor by default.
        # Replaying this exact event also rejects a changed index/path on resume.
        self.event("archive_created", "archive", {"path": created["path"],
            "sha256": created["index_sha256"], "mode": "herdr"})
        verified = archive.verify(self.runs, self.mid, for_completion=True)
        if (not isinstance(verified, dict) or verified.get("valid") is not True
                or verified.get("anchored") is not True
                or not self.completion_receipt_matches_policy(verified, policy)
                or verified.get("index_sha256") != created["index_sha256"]
                or verified.get("path") != created["path"]
                or created.get("acceptance") != verified.get("acceptance")):
            raise HerdrMissionError("archive failed independent verification")
        acceptance = verified["acceptance"]
        if not isinstance(acceptance, dict) or acceptance.get("status") not in {"accepted", "rejected"}:
            return self.response(next_action="archive acceptance contract is not satisfied", acceptance=acceptance)
        state.append_terminal(self.runs, self.mid,
            status="succeeded" if acceptance["status"] == "accepted" else "failed",
            reason=f"Herdr artifact acceptance {acceptance['status']}; index sha256={created['index_sha256']}",
            idempotency_key="herdr:terminal")
        extra = {"role_results": results} if results is not None else {}
        return self.finish(self.response(**extra, acceptance=acceptance, archive=verified))


def drive(runs_dir: Path, mission_id: str, *, observation_deadline=None) -> dict[str, Any]:
    """Advance a durable mission, or return a pending state without duplicate sends."""
    runs_dir = fleet_safe_paths.canonical_root(Path(runs_dir))
    driver = _Driver(runs_dir, mission_id)
    with fleet_safe_paths.RootedFS(runs_dir) as fs:
        with fs.exclusive_lock(driver.rel / "herdr-driver.lock", directory_modes=(0o700, 0o700),
                               blocking=False) as acquired:
            if not acquired:
                return driver.response(next_action="another Herdr driver owns this mission")
            try:
                return driver.execute(observation_deadline=observation_deadline)
            except fleet_herdr.ExecutionEvidenceRejected as exc:
                # collect_result published an immutable rejection; this pass
                # stops before any downstream admission or resend.
                return driver.evidence_block(exc.proof)
            except fleet_herdr_scope.ScopeRejected as exc:
                return driver.response(scope_rejection=exc.receipt,
                    next_action="physical scope not accepted; inspect receipt before explicit recovery")


def supervise(runs_dir, mission_id, *, seconds=60, poll_seconds=0.25):
    """Foreground, one Mission, one supervisor; transport attempts remain durable."""
    if type(seconds) not in {int, float} or not 0 < seconds <= 3600 or type(poll_seconds) not in {int, float} or not 0.05 <= poll_seconds <= 1:
        raise HerdrMissionError("supervision requires 0 < seconds <= 3600 and 0.05 <= poll_seconds <= 1")
    runs_dir = fleet_safe_paths.canonical_root(Path(runs_dir))
    mission_id = state.normalize_uuid(mission_id, "mission_id")
    with fleet_safe_paths.RootedFS(runs_dir) as fs:
        with fs.exclusive_lock(Path("missions") / mission_id / "herdr-supervisor.lock", directory_modes=(0o700, 0o700), blocking=False) as acquired:
            if not acquired:
                return {"mission_id": mission_id, "supervision": "already_owned", "next_action": "another supervisor owns this Mission"}
            deadline = time.monotonic() + seconds
            iterations, result = 0, {}
            while time.monotonic() < deadline:
                try:
                    result = drive(runs_dir, mission_id, observation_deadline=deadline)
                except fleet_herdr.HerdrBackendError as exc:
                    result = {"mission_id": mission_id, "next_action": "reconcile existing transport: " + str(exc)}
                iterations += 1
                if result.get("evidence_rejection") or result.get("scope_rejection") or result.get("transport_rejection"):
                    return {**result, "supervision": "blocked", "iterations": iterations}
                current = fleet_mission.load_state(runs_dir, mission_id)
                if current["status"] in state.TERMINAL_STATUSES or control.view(current)["applied"] == "paused" and control.view(current)["desired"] == "pause_requested":
                    return {**result, "supervision": "settled", "iterations": iterations}
                metrics.observe(runs_dir, mission_id, "supervisor_idle",
                    lambda: time.sleep(min(poll_seconds, max(0, deadline - time.monotonic()))))
            return {**result, "supervision": "observation_budget_exhausted", "iterations": iterations,
                    "next_action": "resume supervision of this same Mission; no automatic prompt replay"}
