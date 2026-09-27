#!/usr/bin/env python3
"""Version-bound Herdr backend with unchanged historical state readers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Any, Callable, Mapping, Optional
import uuid

import fleet_compiled
import fleet_artifacts
import fleet_json
import fleet_herdr_evidence
import fleet_herdr_skill_context
import fleet_herdr_rejection
import fleet_herdr_permissions
import fleet_herdr_launch
import fleet_herdr_startup
import fleet_herdr_profile
import fleet_herdr_versions as versions
import fleet_mission_state as mission_state
import fleet_safe_paths


HERDR_VERSION = versions.HERDR_VERSION
AGENT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SESSION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TERMINAL = {"succeeded", "failed", "blocked", "abandoned", "indeterminate", "not_sent"}
# The whole task travels as one argv element; the controller refuses larger
# prompts before admission instead of discovering the limit at exec time.
MAX_PROMPT_BYTES = 512 * 1024
QUIESCENT = {"idle", "done", "blocked"}
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
TranscriptResolver = Callable[[str], Optional[Path]]


class HerdrBackendError(RuntimeError):
    """The Herdr backend cannot safely reconcile its owned lifecycle."""


class HerdrCommandNotStarted(HerdrBackendError):
    """The Herdr process was never started, so the command had no runtime effect."""


class ExecutionEvidenceRejected(HerdrBackendError):
    def __init__(self, proof):
        self.proof = proof
        super().__init__("completed result permission evidence: " + proof["reason"])


class DeliveryRejected(ExecutionEvidenceRejected):
    """A completed prompt-bound final that cannot be a role result; never a verdict."""

    def __init__(self, proof):
        self.proof = proof
        HerdrBackendError.__init__(self, "completed result delivery: " + proof["reason"])


def load_result_rejection(runs_dir: Path, mission_id: str, run_id: str,
                          *, result: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Read a controller adjudication receipt, never a role or termination verdict."""
    mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
    run_id = mission_state.normalize_uuid(run_id, "run_id")
    with fleet_safe_paths.RootedFS(runs_dir) as fs:
        raw = fs.read_regular_optional(
            Path("missions") / mission_id / f"herdr-result-rejection-{run_id}.json",
            directory_modes=(0o700, 0o700), file_mode=0o600,
            max_bytes=fleet_artifacts.MAX_ARTIFACT_BYTES)
    if raw is None:
        return None
    pointer = fleet_json.loads(raw)
    proof_bytes = fleet_artifacts.get_bytes(runs_dir, mission_id, pointer["artifact_id"])
    proof = fleet_json.loads(proof_bytes)
    if (proof_bytes != fleet_json.canonical_bytes(proof)
            or proof.get("schema_version") != 1 or proof.get("kind") != "herdr_role_protocol_rejection"
            or proof.get("mission_id") != mission_id or proof.get("run_id") != run_id
            or not isinstance(proof.get("reason"), str) or not proof["reason"]):
        raise HerdrBackendError("role protocol rejection receipt binding mismatch")
    observed = fleet_artifacts.get_bytes(runs_dir, mission_id, proof["observed_result_artifact_id"])
    cached = fleet_json.loads(observed)
    if (any(cached.get(k) != proof.get(k) for k in ("mission_id", "run_id", "instance_id"))
            or cached.get("evidence", {}).get("prompt_sha256") != proof.get("prompt_sha256")
            or result is not None and observed != fleet_json.canonical_bytes(result)):
        raise HerdrBackendError("role protocol rejection differs from observed result")
    return {**proof, "artifact_id": pointer["artifact_id"]}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _copy(value: Any) -> Any:
    return fleet_json.loads(fleet_json.canonical_bytes(value))


def _nonempty(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or any(
        character in value for character in ("\x00", "\r", "\n")
    ):
        raise HerdrBackendError(f"{where} must be a non-empty safe string")
    return value


def _object_output(result: subprocess.CompletedProcess[str]) -> dict[str, Any] | None:
    for raw in (result.stdout, result.stderr):
        if not raw or not raw.strip().startswith("{"):
            continue
        try:
            value = fleet_json.loads(raw)
        except fleet_json.FleetJSONError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _result_payload(result: subprocess.CompletedProcess[str], action: str) -> dict[str, Any]:
    value = _object_output(result)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise HerdrBackendError(f"Herdr {action} failed: {detail}")
    if value is None:
        raise HerdrBackendError(f"Herdr {action} returned invalid JSON")
    payload = value.get("result", value)
    if not isinstance(payload, dict):
        raise HerdrBackendError(f"Herdr {action} returned an invalid result envelope")
    return payload


def _error_code(result: subprocess.CompletedProcess[str]) -> str:
    value = _object_output(result)
    candidates: list[Any] = [value]
    while candidates:
        candidate = candidates.pop()
        if not isinstance(candidate, dict):
            continue
        code = candidate.get("code")
        if isinstance(code, str):
            return code
        candidates.extend(candidate.values())
    return ""


def _agent_receipt(
    payload: Mapping[str, Any], *, allow_missing_session: bool = False
) -> dict[str, Any]:
    agent = payload.get("agent")
    if not isinstance(agent, dict):
        raise HerdrBackendError("Herdr agent receipt is missing")
    status = agent.get("agent_status")
    if status not in {"idle", "working", "blocked", "done", "unknown"}:
        raise HerdrBackendError("Herdr agent receipt has invalid agent_status")
    session = agent.get("agent_session")
    if session is None and allow_missing_session:
        normalized_session = None
    elif not isinstance(session, dict) or not {
        "agent",
        "source",
        "kind",
        "value",
    }.issubset(session):
        raise HerdrBackendError("Herdr agent receipt has no explicit agent_session")
    else:
        if session.get("kind") != "id":
            raise HerdrBackendError("Herdr Codex agent_session must be an id")
        normalized_session = {
            "agent": _nonempty(session.get("agent"), "Herdr agent_session.agent"),
            "source": _nonempty(session.get("source"), "Herdr agent_session.source"),
            "kind": "id",
            "value": _nonempty(session.get("value"), "Herdr agent_session.value"),
        }
    binding = {
        "agent_name": _nonempty(agent.get("name"), "Herdr agent name"),
        "workspace_id": _nonempty(agent.get("workspace_id"), "Herdr agent workspace_id"),
        "tab_id": _nonempty(agent.get("tab_id"), "Herdr agent tab_id"),
        "pane_id": _nonempty(agent.get("pane_id"), "Herdr agent pane_id"),
        "terminal_id": _nonempty(agent.get("terminal_id"), "Herdr agent terminal_id"),
        "agent_session": normalized_session,
        "agent_status": status,
    }
    revision = agent.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise HerdrBackendError("Herdr agent receipt has invalid revision")
    binding["revision"] = revision
    return binding


def _message_text(content: Any, *, kind: str) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part["text"]
        for part in content
        if isinstance(part, dict)
        and part.get("type") == kind
        and isinstance(part.get("text"), str)
    )


class HerdrBackend:
    """Own one Herdr workspace and exactly-once prompt intents for one Mission."""

    max_prompt_bytes = MAX_PROMPT_BYTES

    def __init__(
        self,
        runs_dir: Path,
        mission_id: str,
        *,
        session: str,
        feature: str,
        target_repo: Path,
        compiled: dict[str, Any],
        environment: Mapping[str, str] | None = None,
        launch_manifest: dict[str, Any] | None = None,
        personal_cli: bool = False,
        run_command: RunCommand | None = None,
        transcript_resolver: TranscriptResolver | None = None,
    ) -> None:
        try:
            self.runs_dir = fleet_safe_paths.canonical_root(runs_dir)
            self.mission_id = mission_state.normalize_uuid(mission_id, "mission_id")
            self.compiled = fleet_compiled.validate(compiled, mode="effect")
        except (
            fleet_safe_paths.SafePathError,
            mission_state.MissionStateError,
            fleet_compiled.CompiledError,
        ) as exc:
            raise HerdrBackendError(f"invalid Herdr Mission binding: {exc}") from exc
        self.feature = _nonempty(feature, "feature")
        if type(personal_cli) is not bool or personal_cli and launch_manifest is not None:
            raise HerdrBackendError("personal CLI cannot use the experimental launcher")
        self.personal_cli = personal_cli
        self.initial_runtime_contract = dict(versions.PERSONAL_CONTRACT if personal_cli else versions.OFFICIAL_CONTRACT)
        self.launch_manifest = fleet_herdr_launch.validate_manifest(launch_manifest) if launch_manifest is not None else None
        if not isinstance(session, str) or not SESSION_NAME.fullmatch(session):
            raise HerdrBackendError("Herdr session must be an explicit safe name")
        self.session = session
        try:
            self.target_repo = target_repo.expanduser().resolve(strict=True)
        except OSError as exc:
            raise HerdrBackendError("Herdr target repository is unavailable") from exc
        if not self.target_repo.is_dir():
            raise HerdrBackendError("Herdr target repository must be a directory")
        self.environment = dict(os.environ if environment is None else environment)
        for inherited in (
            "HERDR_SOCKET_PATH",
            "HERDR_CLIENT_SOCKET_PATH",
            "HERDR_WORKSPACE_ID",
            "HERDR_TAB_ID",
            "HERDR_PANE_ID",
            "HERDR_ACTIVE_WORKSPACE_ID",
            "HERDR_ACTIVE_TAB_ID",
            "HERDR_ACTIVE_PANE_ID",
        ):
            self.environment.pop(inherited, None)
        self.environment["HERDR_SESSION"] = self.session
        self.environment["PATH"] = _nonempty(
            self.environment.get("PATH"), "Herdr PATH"
        )
        self.run_command = run_command or self._default_run
        self.transcript_resolver = transcript_resolver or self._default_transcript
        self.relative = Path("missions") / self.mission_id / "herdr-backend.json"
        self.runtime_relative = Path("missions") / self.mission_id / "herdr-runtime-contract.json"
        self.lock_relative = Path("missions") / self.mission_id / "herdr-backend.lock"
        self.directory_modes = (0o700, 0o700)
        self.result_directory_modes = (0o700, 0o700, 0o700)
        try:
            self.profile = fleet_herdr_profile.resolve_profile(self.compiled)
        except fleet_herdr_profile.ProfileError as exc:
            raise HerdrBackendError(str(exc)) from exc
        if self.profile is not fleet_herdr_profile.LEGACY and (launch_manifest is not None or not personal_cli):
            raise HerdrBackendError("versioned profile requires the personal Codex lane without experimental launch")
        if self.profile is not fleet_herdr_profile.LEGACY:
            self.initial_runtime_contract = dict(versions.TASK_CONTEXT_CONTRACT)
        self.context = None
        self.member_contract = self.profile.members
        self.members = self._compiled_members()

    def _default_transcript(self, agent_session: str) -> Path | None:
        if self.launch_manifest is not None:
            roles = self.target_repo.parent / "roles"
            matches = list(roles.glob(f"*/*/codex-home/sessions/*/*/*/*{agent_session}.jsonl"))
            if len(matches) > 1:
                raise HerdrBackendError("Codex agent_session has ambiguous role transcripts")
            return matches[0] if matches else None
        codex_home = self.environment.get("CODEX_HOME")
        if not codex_home and self.personal_cli:
            codex_home = str(Path(self.environment.get("HOME", str(Path.home()))) / ".codex")
        if not codex_home:
            return None
        root = Path(codex_home).expanduser() / "sessions"
        matches = list(root.glob(f"*/*/*/*{agent_session}.jsonl"))
        if len(matches) > 1:
            raise HerdrBackendError("Codex agent_session has ambiguous transcripts")
        return matches[0] if matches else None

    def _default_run(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        from fleet_herdr_effects import EffectMediationDenied, require_control_operation
        executable = self.launch_manifest["herdr"]["image"]["realpath"] if self.launch_manifest else "herdr"
        native_executable = self.launch_manifest["codex"]["image"]["realpath"] if self.launch_manifest else "codex"
        try:
            require_control_operation(command, executable=executable, native_executable=native_executable, session=self.session)
        except EffectMediationDenied as exc:
            if not self.personal_cli:
                raise HerdrBackendError(str(exc)) from exc
            from fleet_herdr_personal import require_command
            try:
                require_command(self, command, executable)
            except (ValueError, OSError, RuntimeError) as refusal:
                raise HerdrBackendError(str(refusal)) from refusal
        return subprocess.run(
            command,
            cwd=kwargs["cwd"],
            env=kwargs["env"],
            timeout=kwargs["timeout"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _require_execution_mediation(self) -> None:
        # Refuse known-denied live work before recording a send intent. Trusted
        # injected transports remain usable for provider-free lifecycle tests.
        if self.personal_cli:
            if self.run_command != self._default_run:
                return
            from fleet_herdr_personal import require
            try:
                require(self)
            except (ValueError, OSError, RuntimeError) as exc:
                raise HerdrBackendError(str(exc)) from exc
            return
        if self.run_command == self._default_run:
            from fleet_herdr_effects import EffectMediationDenied, require_native_mediation
            try:
                require_native_mediation()
            except EffectMediationDenied as exc:
                raise HerdrBackendError(str(exc)) from exc

    def _command(
        self, command: list[str], *, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        codex_version_check = command == ["codex", "--version"] or (
            bool(command) and command[0] == "codex" and command[-3:] == ["debug", "prompt-input", fleet_herdr_skill_context.PROBE])
        if not command or (command[0] != "herdr" and not codex_version_check) or "--session" in command:
            raise HerdrBackendError("Herdr command must use backend session routing")
        kind = "codex" if codex_version_check else "herdr"
        executable = self.launch_manifest[kind]["image"]["realpath"] if self.launch_manifest else kind
        routed = [executable, *command[1:]] if codex_version_check else [executable, "--session", self.session, *command[1:]]
        observation_deadline = getattr(self, "observation_deadline", None)
        if observation_deadline is not None:
            remaining = observation_deadline - time.monotonic()
            if remaining <= 0:
                raise HerdrCommandNotStarted("supervisor observation budget exhausted before command")
            timeout = min(timeout, remaining)
        try:
            return self.run_command(
                routed,
                cwd=self.target_repo,
                env=self.environment,
                timeout=timeout,
            )
        except OSError as exc:
            # Spawn failed (for example E2BIG or ENOENT): no Herdr process ran.
            raise HerdrCommandNotStarted(f"Herdr command failed to run: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise HerdrBackendError(f"Herdr command failed to run: {exc}") from exc

    def _compiled_members(self) -> list[dict[str, Any]]:
        resolved = self.compiled["resolved"]
        members = [member for member in [resolved.get("lead"), *resolved.get("instances", [])]
                   if member is not None]
        if len(members) != len(self.member_contract) or any(
            not isinstance(member, dict) for member in members
        ):
            raise HerdrBackendError("Herdr compiled roster differs from the selected profile")
        expected = [tuple(item) for item in self.member_contract]
        actual = [
            (
                member.get("instance_id"),
                member.get("role_type"),
                member.get("model"),
                member.get("phase"),
                member.get("authority"),
            )
            for member in members
        ]
        if actual != expected or any(
            member.get("provider") != "openai"
            or member.get("hook_source") != "codex"
            or member.get("runner") != "interactive"
            for member in members
        ) or "cmux" in members[0].get("tool_access", []):
            raise HerdrBackendError("Herdr compiled roster identity drift")
        for member in members:
            self._compiled_launch(member)
        return [_copy(member) for member in members]

    def _compiled_launch(self, member: Mapping[str, Any]) -> list[str]:
        snapshot = self.compiled.get("router_snapshot")
        roles = snapshot.get("roles") if isinstance(snapshot, dict) else None
        role = roles.get(member.get("role_type")) if isinstance(roles, dict) else None
        command = role.get("command") if isinstance(role, dict) else None
        expected = [
            "codex",
            "--model",
            member.get("model"),
            "-c",
            'model_reasoning_effort="high"',
        ]
        if command != expected:
            raise HerdrBackendError("Herdr compiled router launch identity drift")
        return list(command)

    def _initial_state(self) -> dict[str, Any]:
        context_pin = None
        if self.initial_runtime_contract == versions.TASK_CONTEXT_CONTRACT:
            names = fleet_herdr_skill_context.catalog(self._context_preview([]))
            self.context = {"policy": fleet_herdr_skill_context.POLICY, "disabled_skills": names}
            self._check_context()
            context_pin = fleet_artifacts.put_bytes(self.runs_dir, self.mission_id,
                fleet_json.canonical_bytes(self.context))["artifact_id"]
        short = self.mission_id.split("-", 1)[0]
        generation = str(uuid.uuid4())
        generation_short = generation.split("-", 1)[0]
        roster = []
        for member in self.members:
            name = f"fleet_{short}_{generation_short}_{member['instance_id']}"
            if not AGENT_NAME.fullmatch(name):
                raise HerdrBackendError(f"derived Herdr agent name is invalid: {name}")
            roster.append(
                {
                    "instance_id": member["instance_id"],
                    "role_type": member["role_type"],
                    "provider": member["provider"],
                    "model": member["model"],
                    "phase": member["phase"],
                    "authority": member["authority"],
                    "agent_name": name,
                    "generation": generation,
                    "workspace_id": None,
                    "tab_id": None,
                    "pane_id": None,
                    "terminal_id": None,
                    "agent_session": None,
                    "pane_phase": "unallocated",
                    "start_phase": "not_started",
                    "start_attempts": [],
                    "pending_retry": None,
                }
            )
        return {
            "schema_version": 2 if self.launch_manifest else 3,
            **({"context_artifact_id": context_pin} if context_pin else {}),
            **({"runtime_contract": dict(self.initial_runtime_contract)} if not self.launch_manifest else {}),
            **({"launch_manifest_sha256": fleet_herdr_launch.digest(self.launch_manifest)} if self.launch_manifest else {}),
            "backend": "herdr",
            "backend_version": versions.LEGACY_HERDR_VERSION if self.launch_manifest else HERDR_VERSION,
            "session": self.session,
            "generation": generation,
            "mission_id": self.mission_id,
            "feature": self.feature,
            "target_repo": str(self.target_repo),
            "compiled_digest": self.compiled["compiled_digest"],
            "router_digest": self.compiled["router_digest"],
            "preset": self.profile.preset,
            **({"herdr_profile": self.profile.profile_id,
                "herdr_profile_sha256": self.profile.digest}
               if self.profile is not fleet_herdr_profile.LEGACY else {}),
            "phase": "new",
            "workspace": {
                "label": f"fleet-{self.feature}-{short}-{generation_short}",
                "workspace_id": None,
                "root_tab_id": None,
                "root_pane_id": None,
                "root_terminal_id": None,
                "create_phase": "not_started",
                "owned": False,
                "close_attempted": False,
                "closed": False,
            },
            "members": roster,
            "submissions": {},
            "updated_at": _now(),
        }

    def _validate_state(self, state: Any) -> dict[str, Any]:
        if not isinstance(state, dict):
            raise HerdrBackendError("Herdr backend state must be an object")
        try:
            versions.state_contract(state)
        except ValueError as exc:
            raise HerdrBackendError(str(exc)) from exc
        bindings = {
            "backend": "herdr",
            "session": self.session,
            "mission_id": self.mission_id,
            "feature": self.feature,
            "target_repo": str(self.target_repo),
            "compiled_digest": self.compiled["compiled_digest"],
            "router_digest": self.compiled["router_digest"],
            "preset": self.profile.preset,
        }
        if self.profile is not fleet_herdr_profile.LEGACY:
            bindings.update(herdr_profile=self.profile.profile_id,
                            herdr_profile_sha256=self.profile.digest)
        if any(state.get(key) != value for key, value in bindings.items()):
            raise HerdrBackendError("Herdr backend durable binding drift")
        if state.get("launch_manifest_sha256") != (fleet_herdr_launch.digest(self.launch_manifest) if self.launch_manifest else None):
            raise HerdrBackendError("Herdr launch manifest changed or downgraded")
        workspace = state.get("workspace")
        if not isinstance(workspace, dict):
            raise HerdrBackendError("Herdr backend workspace state is invalid")
        if not isinstance(state.get("members"), list) or not isinstance(
            state.get("submissions"), dict
        ):
            raise HerdrBackendError("Herdr backend roster or submissions are invalid")
        try:
            generation = mission_state.normalize_uuid(
                state.get("generation"), "Herdr generation"
            )
        except mission_state.MissionStateError as exc:
            raise HerdrBackendError("Herdr backend generation is invalid") from exc
        short = self.mission_id.split("-", 1)[0]
        generation_short = generation.split("-", 1)[0]
        if workspace.get("label") != (
            f"fleet-{self.feature}-{short}-{generation_short}"
        ):
            raise HerdrBackendError("Herdr backend workspace ownership drift")
        for key in ("owned", "close_attempted", "closed"):
            if not isinstance(workspace.get(key), bool):
                raise HerdrBackendError("Herdr backend workspace state is invalid")
        if workspace["owned"] and not all(
            isinstance(workspace.get(key), str) and workspace[key]
            for key in (
                "workspace_id",
                "root_tab_id",
                "root_pane_id",
                "root_terminal_id",
            )
        ):
            raise HerdrBackendError("Herdr backend owned workspace receipt is invalid")
        expected = [tuple(item) for item in self.member_contract]
        actual = [
            (
                item.get("instance_id"),
                item.get("role_type"),
                item.get("model"),
                item.get("phase"),
                item.get("authority"),
            )
            for item in state["members"]
            if isinstance(item, dict)
        ]
        if actual != expected:
            raise HerdrBackendError("Herdr backend durable roster drift")
        expected_names = {
            item[0]: f"fleet_{short}_{generation_short}_{item[0]}"
            for item in self.member_contract
        }
        if any(
            item.get("agent_name") != expected_names.get(item.get("instance_id"))
            or item.get("generation") != generation
            for item in state["members"]
        ):
            raise HerdrBackendError("Herdr backend durable agent identity drift")
        member_by_instance = {item["instance_id"]: item for item in state["members"]}
        for member in state["members"]:
            member.setdefault("start_attempts", [])
            member.setdefault("pending_retry", None)
            if not isinstance(member["start_attempts"], list) or any(
                not isinstance(attempt, dict) for attempt in member["start_attempts"]
            ):
                raise HerdrBackendError("Herdr durable start attempt history is invalid")
            if member["pending_retry"] is not None and not isinstance(
                member["pending_retry"], dict
            ):
                raise HerdrBackendError("Herdr durable pending retry is invalid")
            if member.get("start_phase") == "started":
                if not all(
                    isinstance(member.get(key), str) and member[key]
                    for key in ("workspace_id", "tab_id", "pane_id", "terminal_id")
                ):
                    raise HerdrBackendError("Herdr durable surface binding is invalid")
                agent_session = member.get("agent_session")
                if agent_session is not None and (
                    not isinstance(agent_session, dict)
                    or agent_session.get("kind") != "id"
                    or not all(
                        isinstance(agent_session.get(key), str) and agent_session[key]
                        for key in ("agent", "source", "value")
                    )
                ):
                    raise HerdrBackendError("Herdr durable agent_session is invalid")
                if workspace["owned"] and member["workspace_id"] != workspace["workspace_id"]:
                    raise HerdrBackendError("Herdr durable agent workspace drift")
        started = [
            member for member in state["members"] if member.get("start_phase") == "started"
        ]
        for key in ("pane_id", "terminal_id"):
            values = [member[key] for member in started]
            if len(values) != len(set(values)):
                raise HerdrBackendError(f"Herdr durable {key} binding is not unique")
        for run_id, submission in state["submissions"].items():
            try:
                normalized_run = mission_state.normalize_uuid(run_id, "Herdr run_id")
            except mission_state.MissionStateError as exc:
                raise HerdrBackendError(
                    "Herdr durable submission run_id is invalid"
                ) from exc
            if not isinstance(submission, dict) or submission.get("run_id") != normalized_run:
                raise HerdrBackendError("Herdr durable submission binding is invalid")
            member = member_by_instance.get(submission.get("instance_id"))
            if (
                member is None
                or submission.get("agent_name") != member["agent_name"]
                or submission.get("generation") != generation
                or submission.get("workspace_id") != member.get("workspace_id")
                or submission.get("tab_id") != member.get("tab_id")
                or submission.get("pane_id") != member.get("pane_id")
                or submission.get("terminal_id") != member.get("terminal_id")
                or submission.get("agent_session") != member.get("agent_session")
                or not SHA256.fullmatch(str(submission.get("prompt_sha256", "")))
                or not isinstance(submission.get("cancel_attempted"), bool)
                or submission.get("usage_baseline_artifact_id") is not None
                and not SHA256.fullmatch(str(submission.get("usage_baseline_artifact_id")))
                or submission.get("candidate_tree_sha") is not None
                and not mission_state.GIT_OID.fullmatch(
                    str(submission.get("candidate_tree_sha"))
                )
            ):
                raise HerdrBackendError("Herdr durable submission identity drift")
            if any(
                key in submission
                for key in ("artifact_id", "result_artifact_id", "turn_id")
            ) and submission.get("agent_session") is None:
                raise HerdrBackendError(
                    "Herdr durable result requires a concrete agent_session"
                )
        return state

    def _load(self, rooted: fleet_safe_paths.RootedFS) -> dict[str, Any] | None:
        try:
            value = versions.read_state(rooted, self.relative, mission_id=self.mission_id,
                compiled_digest=self.compiled["compiled_digest"], directory_modes=self.directory_modes)
        except ValueError as exc:
            raise HerdrBackendError(str(exc)) from exc
        if value is not None:
            self.context = None
            if value.get("runtime_contract") == versions.TASK_CONTEXT_CONTRACT:
                self.context = fleet_herdr_skill_context.validate(fleet_json.loads(fleet_artifacts.get_bytes(
                    self.runs_dir, self.mission_id, value["context_artifact_id"])))
        return self._validate_state(value) if value is not None else None

    def _runtime_anchor(self, state: Mapping[str, Any]) -> bytes | None:
        return versions.anchor_bytes(state)

    def _save(
        self,
        rooted: fleet_safe_paths.RootedFS,
        state: dict[str, Any],
        *,
        exists: bool,
    ) -> None:
        state["updated_at"] = _now()
        content = fleet_json.canonical_bytes(state) + b"\n"
        if exists:
            rooted.replace_regular(
                self.relative,
                content,
                directory_modes=self.directory_modes,
                file_mode=0o600,
            )
        else:
            anchor = self._runtime_anchor(state)
            if anchor is not None:
                rooted.atomic_write(self.runtime_relative, anchor,
                    directory_modes=self.directory_modes, file_mode=0o600, require_absent=True)
            rooted.atomic_write(
                self.relative,
                content,
                directory_modes=self.directory_modes,
                file_mode=0o600,
                require_absent=True,
            )

    def _preflight(self, state: dict[str, Any] | None = None) -> None:
        contract = versions.state_contract(state) if state is not None else (
            None if self.launch_manifest else dict(self.initial_runtime_contract))
        expected_herdr = contract["herdr_version"] if contract else versions.LEGACY_HERDR_VERSION
        if self.environment.get("HERDR_SESSION") != self.session:
            raise HerdrBackendError("Herdr backend session binding drift")
        version = self._command(["herdr", "--version"])
        if version.returncode != 0 or version.stdout.strip() != f"herdr {expected_herdr}":
            raise HerdrBackendError(f"Herdr backend requires exact CLI version {expected_herdr}; no implicit runtime migration")
        if contract:
            codex = self._command(["codex", "--version"])
            if codex.returncode != 0 or codex.stdout.strip() != f"codex-cli {contract['codex_version']}":
                raise HerdrBackendError(f"Herdr backend requires exact Codex CLI version {contract['codex_version']}")
        if self.launch_manifest is not None:
            fleet_herdr_launch.validate_manifest(self.launch_manifest)
            fleet_herdr_launch.install_wrapper(self.runs_dir, self.mission_id)
            with fleet_safe_paths.RootedFS(self.runs_dir) as fs:
                for name in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "CODEX_HOME", "TMPDIR"):
                    relative = Path(self.environment[name]).relative_to(self.runs_dir)
                    fs.atomic_write(relative / ".fleet-owned", b"controller runtime\n",
                                    directory_modes=(0o700,) * len(relative.parts), file_mode=0o600)

    @staticmethod
    def _workspace_binding(payload: Mapping[str, Any]) -> dict[str, str]:
        workspace = payload.get("workspace")
        tab = payload.get("tab")
        pane = payload.get("root_pane")
        if (
            not isinstance(workspace, dict)
            or not isinstance(tab, dict)
            or not isinstance(pane, dict)
        ):
            raise HerdrBackendError("Herdr workspace create receipt is incomplete")
        binding = {
            "workspace_id": _nonempty(
                workspace.get("workspace_id"), "Herdr workspace_id"
            ),
            "root_tab_id": _nonempty(tab.get("tab_id"), "Herdr root tab_id"),
            "root_pane_id": _nonempty(pane.get("pane_id"), "Herdr root pane_id"),
            "root_terminal_id": _nonempty(
                pane.get("terminal_id"), "Herdr root terminal_id"
            ),
        }
        if (
            tab.get("workspace_id") != binding["workspace_id"]
            or pane.get("workspace_id") != binding["workspace_id"]
            or pane.get("tab_id") != binding["root_tab_id"]
        ):
            raise HerdrBackendError("Herdr workspace create receipt identity mismatch")
        return binding

    @staticmethod
    def _pane_binding(payload: Mapping[str, Any], workspace_id: str) -> dict[str, str]:
        pane = payload.get("pane")
        if not isinstance(pane, dict):
            raise HerdrBackendError("Herdr pane split receipt is incomplete")
        if pane.get("workspace_id") != workspace_id:
            raise HerdrBackendError("Herdr pane split receipt workspace mismatch")
        return {
            "workspace_id": workspace_id,
            "tab_id": _nonempty(pane.get("tab_id"), "Herdr pane tab_id"),
            "pane_id": _nonempty(pane.get("pane_id"), "Herdr pane_id"),
            "terminal_id": _nonempty(
                pane.get("terminal_id"), "Herdr pane terminal_id"
            ),
        }

    def _personal_environment_arguments(self) -> list[str]:
        if not self.personal_cli:
            return []
        return [arg for name in ("PATH", "CODEX_HOME", "HOME", "ZDOTDIR")
                if self.environment.get(name)
                for arg in ("--env", f"{name}={self.environment[name]}")]

    def _start_arguments(self, member: Mapping[str, Any]) -> list[str]:
        self._check_context()
        launch = self._compiled_launch(member)
        intent = member["start_attempts"][-1].get("launch_intent") if self.launch_manifest and member.get("start_attempts") else None
        return [
            "herdr",
            "agent",
            "start",
            str(member["agent_name"]),
            "--kind",
            "codex",
            "--pane",
            str(member["pane_id"]),
            "--timeout",
            "30000",
            *(["--executable", str(fleet_herdr_launch.launcher_bin(self.runs_dir, self.mission_id) / "codex")] if self.launch_manifest else []),
            "--",
            *(["--fleet-launch-intent", intent["path"], intent["sha256"]] if intent else []),
            *launch[1:],
            *(fleet_herdr_skill_context.flags(self.context) if self.context is not None else []),
            "-c",
            self._project_trust_override(),
            *fleet_herdr_permissions.launch_flags(member["instance_id"], str(self.target_repo),
                                                  version=self.profile.permissions_policy_version),
        ]

    def _context_preview(self, flags):
        result = self._command(["codex", *flags, "debug", "prompt-input", fleet_herdr_skill_context.PROBE])
        if result.returncode != 0:
            raise HerdrBackendError("local Codex context preview failed")
        return result.stdout

    def _check_context(self):
        if self.context is not None and fleet_herdr_skill_context.catalog(
                self._context_preview(fleet_herdr_skill_context.flags(self.context))):
            raise HerdrBackendError("host skills remain enabled outside frozen context")

    def _project_trust_override(self) -> str:
        project = json.dumps(str(self.target_repo))
        return f'projects={{{project}={{trust_level="untrusted"}}}}'

    def _require_native_bootstrap(self, member, launch_pin):
        if not self.launch_manifest or self.launch_manifest["version"] != 2 or member["instance_id"] != "worker":
            return None
        from fleet_herdr_native import require_bootstrap
        try:
            return require_bootstrap(self.runs_dir, self.mission_id, member, launch_pin)
        except (RuntimeError, OSError, KeyError, ValueError) as exc:
            raise HerdrBackendError("native Worker bootstrap could not be verified") from exc

    def _verify_agent_receipt(
        self,
        payload: Mapping[str, Any],
        member: Mapping[str, Any],
        *,
        allow_missing_session: bool = False,
    ) -> dict[str, Any]:
        receipt = _agent_receipt(
            payload, allow_missing_session=allow_missing_session
        )
        expected = {
            "agent_name": member["agent_name"],
            "workspace_id": member["workspace_id"],
            "tab_id": member["tab_id"],
            "pane_id": member["pane_id"],
            "terminal_id": member["terminal_id"],
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise HerdrBackendError("Herdr agent receipt identity mismatch")
        bound_session = member.get("agent_session")
        if bound_session is not None and receipt["agent_session"] != bound_session:
            raise HerdrBackendError("Herdr agent_session identity mismatch")
        return receipt

    def _verify_start_argv(
        self, payload: Mapping[str, Any], member: Mapping[str, Any]
    ) -> None:
        argv = payload.get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise HerdrBackendError("Herdr agent start receipt has invalid argv")
        requested = self._start_arguments(member)
        expected = requested[requested.index("--") + 1:]
        if argv[1:] != expected or Path(argv[0]).name != "codex":
            raise HerdrBackendError("Herdr agent start receipt launch identity drift")
        if self.launch_manifest and argv[0] != str(fleet_herdr_launch.launcher_bin(self.runs_dir, self.mission_id) / "codex"):
            raise HerdrBackendError("Herdr did not use the exact trusted launcher")

    def _get_agent(
        self,
        member: Mapping[str, Any],
        *,
        allow_missing_session: bool = False,
    ) -> dict[str, Any]:
        payload = _result_payload(
            self._command(["herdr", "agent", "get", str(member["agent_name"])]),
            "agent get",
        )
        return self._verify_agent_receipt(
            payload, member, allow_missing_session=allow_missing_session
        )

    def _resolve_agent_session(self, member: Mapping[str, Any]) -> dict[str, Any]:
        receipt = self._get_agent(member, allow_missing_session=True)
        if receipt["agent_status"] not in {"idle", "done"}:
            raise HerdrBackendError("Herdr agent surface was not observed ready after start")
        return receipt

    def _require_deliverable(self, member: Mapping[str, Any]) -> None:
        """A ready receipt can describe a modal or a restored metadata-only pane.

        This is a conservative delivery guard, never an acceptance or sandbox
        proof. No key is sent to dismiss a dialog, and no prompt is retried.
        """
        screen = self._command(["herdr", "agent", "read", str(member["agent_name"]),
                                "--source", "visible"])
        if screen.returncode != 0 or len(screen.stdout) > 128 * 1024:
            raise HerdrBackendError("Codex delivery surface is unavailable")
        reason = fleet_herdr_startup.codex_startup_blocker(screen.stdout)
        if reason:
            raise HerdrBackendError(f"Codex delivery blocked before prompt: {reason}")
        receipt = self._get_agent(member, allow_missing_session=member.get("agent_session") is None)
        if receipt["agent_status"] not in {"idle", "done"}:
            raise HerdrBackendError("Codex delivery state changed before prompt")

    @staticmethod
    def _freeze_agent_session(
        member: dict[str, Any],
        submission: dict[str, Any],
        receipt: Mapping[str, Any],
    ) -> bool:
        observed = receipt.get("agent_session")
        if observed is None:
            return False
        bound_member = member.get("agent_session")
        bound_submission = submission.get("agent_session")
        if bound_member is not None and bound_member != observed:
            raise HerdrBackendError("Herdr member agent_session identity mismatch")
        if bound_submission is not None and bound_submission != observed:
            raise HerdrBackendError("Herdr submission agent_session identity mismatch")
        member["agent_session"] = _copy(observed)
        submission["agent_session"] = _copy(observed)
        return True

    def boot(self) -> dict[str, Any]:
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                with rooted.exclusive_lock(
                    self.lock_relative,
                    directory_modes=self.directory_modes,
                    file_mode=0o600,
                ):
                    state = self._load(rooted)
                    if state is None or state["phase"] != "ready":
                        self._require_execution_mediation()
                    self._preflight(state)
                    exists = state is not None
                    if state is None:
                        state = self._initial_state()
                        self._save(rooted, state, exists=False)
                        exists = True
                    self._check_context()
                    if state["phase"] == "ready":
                        self._revalidate_workspace(state)
                        for member in state["members"]:
                            self._get_agent(
                                member,
                                allow_missing_session=member["agent_session"] is None,
                            )
                            if state.get("runtime_contract"):
                                self._require_deliverable(member)
                        return _copy(state)
                    workspace = state["workspace"]
                    if workspace["workspace_id"] is None:
                        if workspace["create_phase"] == "creating":
                            raise HerdrBackendError(
                                "Herdr workspace creation is indeterminate; refusing a duplicate"
                            )
                        workspace["create_phase"] = "creating"
                        self._save(rooted, state, exists=exists)
                        payload = _result_payload(
                            self._command(
                                [
                                    "herdr",
                                    "workspace",
                                    "create",
                                    "--cwd",
                                    str(self.target_repo),
                                    "--label",
                                    workspace["label"],
                                    "--env",
                                    f"FLEET_MISSION_ID={self.mission_id}",
                                    "--env",
                                    f"FLEET_HERDR_GENERATION={state['generation']}",
                                    "--env",
                                    f"FLEET_HERDR_SESSION={self.session}",
                                    *(self._personal_environment_arguments() if self.personal_cli
                                      else ["--env", f"PATH={self.environment['PATH']}"]),
                                    "--no-focus",
                                ]
                            ),
                            "workspace create",
                        )
                        workspace.update(self._workspace_binding(payload))
                        workspace.update({"create_phase": "created", "owned": True})
                        self._save(rooted, state, exists=True)
                    state["members"][0].update(
                        {
                            "workspace_id": workspace["workspace_id"],
                            "tab_id": workspace["root_tab_id"],
                            "pane_id": workspace["root_pane_id"],
                            "terminal_id": workspace["root_terminal_id"],
                            "pane_phase": "allocated",
                        }
                    )
                    members_by_id = {
                        member["instance_id"]: member for member in state["members"]
                    }
                    grid = (("worker", "lead", "right"),
                            ("reviewer", "lead", "down"),
                            ("verifier", "worker", "down"))
                    if self.profile is fleet_herdr_profile.RESEARCH:
                        grid = (("research", "lead", "down"),
                                ("worker", "lead", "right"),
                                ("reviewer", "worker", "down"),
                                ("verifier", "research", "down"))
                    elif self.profile is fleet_herdr_profile.MINIMAL:
                        grid = ()
                    for instance_id, source_id, direction in grid:
                        member = members_by_id[instance_id]
                        if member["pane_id"] is None:
                            if member["pane_phase"] == "splitting":
                                raise HerdrBackendError(
                                    f"Herdr pane creation is indeterminate for {member['instance_id']}"
                                )
                            member["pane_phase"] = "splitting"
                            self._save(rooted, state, exists=True)
                            payload = _result_payload(
                                self._command(
                                    [
                                        "herdr",
                                        "pane",
                                        "split",
                                        str(members_by_id[source_id]["pane_id"]),
                                        "--direction",
                                        direction,
                                        "--cwd",
                                        str(self.target_repo),
                                        *self._personal_environment_arguments(),
                                        "--no-focus",
                                    ]
                                ),
                                "pane split",
                            )
                            member.update(
                                self._pane_binding(payload, workspace["workspace_id"])
                            )
                            member["pane_phase"] = "allocated"
                            self._save(rooted, state, exists=True)
                    for member in state["members"]:
                        if member["start_phase"] == "started":
                            continue
                        if member["start_phase"] == "starting":
                            ready_receipt = self._resolve_agent_session(member)
                        else:
                            pending_retry = member.get("pending_retry")
                            attempt = {
                                "attempt_id": str(uuid.uuid4()),
                                "started_at": _now(),
                                "reason": (
                                    pending_retry["reason"]
                                    if isinstance(pending_retry, dict)
                                    else "initial_start"
                                ),
                                "workspace_id": member["workspace_id"],
                                "tab_id": member["tab_id"],
                                "pane_id": member["pane_id"],
                                "terminal_id": member["terminal_id"],
                            }
                            if isinstance(pending_retry, dict):
                                attempt["retry_id"] = pending_retry["retry_id"]
                            member["start_attempts"].append(attempt)
                            member["pending_retry"] = None
                            member["start_phase"] = "starting"
                            self._save(rooted, state, exists=True)
                            if self.launch_manifest is not None:
                                args = self._start_arguments(member)
                                argv = ["codex", *args[args.index("--") + 1:]]
                                attempt["launch_intent"] = fleet_herdr_launch.prepare(
                                    self.runs_dir, self.mission_id, self.target_repo, self.compiled,
                                    member, self.launch_manifest, argv)
                                self._save(rooted, state, exists=True)
                            start_payload = _result_payload(
                                self._command(self._start_arguments(member), timeout=45),
                                "agent start",
                            )
                            self._verify_start_argv(start_payload, member)
                            ready_receipt = self._verify_agent_receipt(
                                start_payload,
                                member,
                                allow_missing_session=True,
                            )
                            if ready_receipt["agent_status"] not in {"idle", "done"}:
                                raise HerdrBackendError(
                                    "Herdr agent surface was not observed ready after start"
                                )
                        if state.get("runtime_contract"):
                            self._require_deliverable(member)
                        if ready_receipt["agent_session"] is not None:
                            if (
                                member["agent_session"] is not None
                                and member["agent_session"]
                                != ready_receipt["agent_session"]
                            ):
                                raise HerdrBackendError(
                                    "Herdr agent_session identity mismatch"
                                )
                            member["agent_session"] = _copy(
                                ready_receipt["agent_session"]
                            )
                        if member["start_attempts"]:
                            if self.launch_manifest:
                                launch_intent = member["start_attempts"][-1].get("launch_intent")
                                if not launch_intent:
                                    raise HerdrBackendError("launch observation missing after recovery")
                                observed = Path(launch_intent["path"]).with_name("consumed.json").relative_to(self.runs_dir)
                                raw = rooted.read_regular(observed, directory_modes=(0o700,) * 4,
                                                          file_mode=0o600, max_bytes=4096)
                                pin = fleet_json.loads(raw)["artifact_id"]
                                record = fleet_json.loads(fleet_artifacts.get_bytes(self.runs_dir, self.mission_id, pin))
                                if record.get("intent_sha256") != launch_intent["sha256"]:
                                    raise HerdrBackendError("launch observation intent mismatch")
                                member["start_attempts"][-1]["launch_observation_artifact_id"] = pin
                                if self.launch_manifest["version"] == 2 and member["instance_id"] == "worker":
                                    member["start_attempts"][-1]["native_bootstrap_artifact_id"] = self._require_native_bootstrap(member, pin)
                            member["start_attempts"][-1].update(
                                {"ready_at": _now(), "outcome": "ready"}
                            )
                        member["start_phase"] = "started"
                        self._save(rooted, state, exists=True)
                    state["phase"] = "ready"
                    self._save(rooted, state, exists=True)
                    return _copy(state)
        except fleet_safe_paths.SafePathError as exc:
            raise HerdrBackendError(f"unsafe Herdr backend state: {exc}") from exc

    def retry_unsubmitted_start(self, instance_id: str) -> dict[str, Any]:
        self._require_execution_mediation()
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                with rooted.exclusive_lock(
                    self.lock_relative,
                    directory_modes=self.directory_modes,
                    file_mode=0o600,
                ):
                    state = self._load(rooted)
                    if state is None:
                        raise HerdrBackendError("Herdr backend state is missing")
                    self._preflight(state)
                    workspace = state["workspace"]
                    if (
                        not workspace["owned"]
                        or workspace["closed"]
                        or self._get_workspace(workspace) is None
                    ):
                        raise HerdrBackendError("Herdr retry workspace is not owned")
                    if state["submissions"]:
                        raise HerdrBackendError(
                            "Herdr start retry requires zero submissions"
                        )
                    member = self._member(state, instance_id)
                    if member["start_phase"] != "starting":
                        raise HerdrBackendError(
                            "Herdr start retry requires start_phase=starting"
                        )
                    agent = self._command(
                        ["herdr", "agent", "get", member["agent_name"]]
                    )
                    if agent.returncode == 0:
                        raise HerdrBackendError(
                            "Herdr start retry refused because the agent still exists"
                        )
                    if _error_code(agent) not in {
                        "agent_not_found",
                        "agent_name_not_found",
                    }:
                        raise HerdrBackendError(
                            "Herdr start retry could not prove agent absence"
                        )
                    pane_payload = _result_payload(
                        self._command(["herdr", "pane", "get", member["pane_id"]]),
                        "pane get",
                    )
                    pane = pane_payload.get("pane")
                    expected_pane = {
                        "workspace_id": member["workspace_id"],
                        "tab_id": member["tab_id"],
                        "pane_id": member["pane_id"],
                        "terminal_id": member["terminal_id"],
                        "cwd": str(self.target_repo),
                    }
                    if not isinstance(pane, dict) or any(
                        pane.get(key) != value for key, value in expected_pane.items()
                    ):
                        raise HerdrBackendError(
                            "Herdr start retry pane identity mismatch"
                        )
                    process_payload = _result_payload(
                        self._command(
                            [
                                "herdr",
                                "pane",
                                "process-info",
                                "--pane",
                                member["pane_id"],
                            ]
                        ),
                        "pane process-info",
                    )
                    process_info = process_payload.get("process_info")
                    if not isinstance(process_info, dict):
                        raise HerdrBackendError(
                            "Herdr start retry process receipt is missing"
                        )
                    shell_pid = process_info.get("shell_pid")
                    foreground = process_info.get("foreground_processes")
                    if (
                        process_info.get("pane_id") != member["pane_id"]
                        or isinstance(shell_pid, bool)
                        or not isinstance(shell_pid, int)
                        or shell_pid <= 0
                        or process_info.get("foreground_process_group_id") != shell_pid
                        or not isinstance(foreground, list)
                        or len(foreground) != 1
                        or not isinstance(foreground[0], dict)
                        or foreground[0].get("pid") != shell_pid
                        or foreground[0].get("cwd") != str(self.target_repo)
                    ):
                        raise HerdrBackendError(
                            "Herdr start retry pane is not at its exact shell foreground"
                        )
                    if not member["start_attempts"]:
                        member["start_attempts"].append(
                            {
                                "attempt_id": str(uuid.uuid4()),
                                "started_at": state["updated_at"],
                                "reason": "pre_history_start",
                                "workspace_id": member["workspace_id"],
                                "tab_id": member["tab_id"],
                                "pane_id": member["pane_id"],
                                "terminal_id": member["terminal_id"],
                            }
                        )
                    retry_id = str(uuid.uuid4())
                    authorized_at = _now()
                    member["start_attempts"][-1].update(
                        {
                            "outcome": "agent_absent",
                            "resolved_at": authorized_at,
                            "retry_id": retry_id,
                        }
                    )
                    member["pending_retry"] = {
                        "retry_id": retry_id,
                        "authorized_at": authorized_at,
                        "reason": "agent_absent_shell_foreground",
                        "previous_terminal_id": member["terminal_id"],
                    }
                    member["agent_session"] = None
                    member["start_phase"] = "not_started"
                    self._save(rooted, state, exists=True)
            return self.boot()
        except fleet_safe_paths.SafePathError as exc:
            raise HerdrBackendError(f"unsafe Herdr start retry state: {exc}") from exc

    def _revalidate_workspace(self, state: Mapping[str, Any]) -> None:
        workspace = state["workspace"]
        receipt = self._get_workspace(workspace)
        if receipt is None:
            raise HerdrBackendError("Herdr workspace is absent")

    def _get_workspace(
        self, workspace: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        result = self._command(
            ["herdr", "workspace", "get", str(workspace["workspace_id"])]
        )
        if result.returncode != 0:
            if _error_code(result) == "workspace_not_found":
                return None
            _result_payload(result, "workspace get")
            raise HerdrBackendError("Herdr workspace get did not fail as expected")
        payload = _result_payload(result, "workspace get")
        receipt = payload.get("workspace")
        if (
            not isinstance(receipt, dict)
            or receipt.get("workspace_id") != workspace["workspace_id"]
            or receipt.get("label") != workspace["label"]
        ):
            raise HerdrBackendError("Herdr workspace receipt identity mismatch")
        return receipt

    def _member(self, state: Mapping[str, Any], instance_id: str) -> dict[str, Any]:
        for member in state["members"]:
            if member["instance_id"] == instance_id:
                return member
        raise HerdrBackendError(f"unknown Herdr instance: {instance_id}")

    @staticmethod
    def _classify_error(result: subprocess.CompletedProcess[str]) -> str:
        code = _error_code(result)
        if code == "agent_blocked":
            return "blocked"
        if code in {"agent_prompt_stalled", "agent_not_ready"}:
            return "indeterminate"
        return "failed"

    def _prompt_contract(
        self, prompt: str, run_id: str, instance_id: str
    ) -> dict[str, Any]:
        try:
            value = fleet_json.loads(prompt)
        except fleet_json.FleetJSONError as exc:
            raise HerdrBackendError("Herdr prompt must be canonical Driver JSON") from exc
        if not isinstance(value, dict):
            raise HerdrBackendError("Herdr prompt must be a Driver object")
        if value.get("contract_version") == "owner-work-v1":
            raise HerdrBackendError(
                "owner-work-v1 dispatch is disabled; owner revisions and decision lifecycle are not implemented")
        expected = {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "run_id": run_id,
            "instance_id": instance_id,
        }
        if any(value.get(key) != item for key, item in expected.items()):
            raise HerdrBackendError("Herdr prompt Driver binding mismatch")
        if self.context is not None:
            self._check_context()
            try:
                fleet_herdr_skill_context.verify_task(prompt, instance_id,
                    lambda pin: fleet_artifacts.get_bytes(self.runs_dir, self.mission_id, pin))
            except ValueError as exc:
                raise HerdrBackendError(str(exc)) from exc
        contract = value.get("result_contract")
        if not isinstance(contract, dict) or any(
            contract.get(key) != item for key, item in expected.items()
        ):
            raise HerdrBackendError("Herdr result_contract binding mismatch")
        candidate_tree = contract.get("candidate_tree_sha")
        if candidate_tree is not None and not mission_state.GIT_OID.fullmatch(
            str(candidate_tree)
        ):
            raise HerdrBackendError("Herdr candidate_tree_sha is invalid")
        return {
            "candidate_tree_sha": candidate_tree,
            "schema_version": contract["schema_version"],
        }

    def submit(
        self, run_id: str, prompt: str, *, instance_id: str = "lead"
    ) -> dict[str, Any]:
        # The supervisor budget bounds observation, not a send already under a
        # durable dispatch intent: stopping midway would strand that intent.
        # Each command keeps its own timeout, so the send stays bounded.
        budget = getattr(self, "observation_deadline", None)
        self.observation_deadline = None
        try:
            return self._submit(run_id, prompt, instance_id=instance_id)
        finally:
            self.observation_deadline = budget

    def _submit(
        self, run_id: str, prompt: str, *, instance_id: str
    ) -> dict[str, Any]:
        normalized_run = mission_state.normalize_uuid(run_id, "run_id")
        if not isinstance(prompt, str) or not prompt:
            raise HerdrBackendError("Herdr prompt must be non-empty")
        prompt_contract = self._prompt_contract(prompt, normalized_run, instance_id)
        prompt_sha256 = mission_state.artifact_id(prompt)
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                with rooted.exclusive_lock(
                    self.lock_relative,
                    directory_modes=self.directory_modes,
                    file_mode=0o600,
                ):
                    state = self._load(rooted)
                    if state is None or state["phase"] != "ready":
                        raise HerdrBackendError("Herdr backend is not ready")
                    member = self._member(state, instance_id)
                    current = state["submissions"].get(normalized_run)
                    if current is not None:
                        if (
                            current.get("instance_id") != instance_id
                            or current.get("prompt_sha256") != prompt_sha256
                        ):
                            raise HerdrBackendError("Herdr run_id submission binding drift")
                        if current.get("phase") == "prepared":
                            current.update(
                                {
                                    "phase": "terminal",
                                    "status": "indeterminate",
                                    "reason": "submit outcome was not durably confirmed",
                                }
                            )
                            self._save(rooted, state, exists=True)
                        return _copy(current)
                    self._require_execution_mediation()
                    if self.personal_cli:
                        from fleet_herdr_personal import require
                        try:
                            require(self, run_id=normalized_run, prompt_sha256=prompt_sha256, instance_id=instance_id)
                        except (ValueError, OSError, RuntimeError) as exc:
                            raise HerdrBackendError(str(exc)) from exc
                    self._preflight(state)
                    receipt = self._get_agent(
                        member,
                        allow_missing_session=member["agent_session"] is None,
                    )
                    if receipt["agent_status"] == "blocked":
                        return self._record_pre_submit_blocked(
                            rooted,
                            state,
                            normalized_run,
                            instance_id,
                            member,
                            prompt_sha256,
                            prompt_contract["candidate_tree_sha"],
                        )
                    if receipt["agent_status"] not in {"idle", "done"}:
                        raise HerdrBackendError(
                            "Herdr agent is not quiescent for an unambiguous prompt"
                        )
                    self._require_deliverable(member)
                    usage_baseline_artifact_id = self._capture_usage_baseline(
                        run_id=normalized_run, prompt_sha256=prompt_sha256,
                        generation=state["generation"], member=member)
                    submission = {
                        "run_id": normalized_run,
                        "instance_id": instance_id,
                        "agent_name": member["agent_name"],
                        "prompt_sha256": prompt_sha256,
                        "generation": state["generation"],
                        "workspace_id": member["workspace_id"],
                        "tab_id": member["tab_id"],
                        "pane_id": member["pane_id"],
                        "terminal_id": member["terminal_id"],
                        "agent_session": _copy(member["agent_session"]),
                        "candidate_tree_sha": prompt_contract["candidate_tree_sha"],
                        "phase": "prepared",
                        "status": "queued",
                        "prepared_at": _now(),
                        "usage_baseline_artifact_id": usage_baseline_artifact_id,
                        "cancel_attempted": False,
                    }
                    state["submissions"][normalized_run] = submission
                    if self.launch_manifest:
                        if self.launch_manifest["version"] == 2 and instance_id == "worker":
                            self._require_native_bootstrap(member, member["start_attempts"][-1]["launch_observation_artifact_id"])
                        submission["launch_run_link_artifact_id"] = fleet_herdr_launch.link_run(
                            self.runs_dir, self.mission_id, member, normalized_run, prompt_sha256)
                    self._save(rooted, state, exists=True)
                    try:
                        result = self._command(
                            [
                                "herdr",
                                "agent",
                                "prompt",
                                member["agent_name"],
                                prompt,
                            ]
                        )
                    except HerdrCommandNotStarted as exc:
                        # Known not delivered, unlike a timeout after the process ran.
                        submission.update(
                            {
                                "phase": "terminal",
                                "status": "not_sent",
                                "reason": f"prompt command did not start: {exc}",
                            }
                        )
                        self._save(rooted, state, exists=True)
                        return _copy(submission)
                    if result.returncode == 0:
                        receipt = self._verify_agent_receipt(
                            _result_payload(result, "agent prompt"),
                            member,
                            allow_missing_session=member["agent_session"] is None,
                        )
                        self._freeze_agent_session(member, submission, receipt)
                        submission.update(
                            {
                                "phase": "submitted",
                                "status": "working",
                                "submitted_at": _now(),
                                "submitted_revision": receipt["revision"],
                            }
                        )
                    else:
                        status = self._classify_error(result)
                        submission.update(
                            {
                                "phase": "terminal",
                                "status": status,
                                "reason": _error_code(result) or "Herdr prompt failed",
                            }
                        )
                    self._save(rooted, state, exists=True)
                    return _copy(submission)
        except (fleet_safe_paths.SafePathError, mission_state.MissionStateError) as exc:
            raise HerdrBackendError(f"unsafe Herdr submit state: {exc}") from exc

    def _record_pre_submit_blocked(
        self,
        rooted: fleet_safe_paths.RootedFS,
        state: dict[str, Any],
        run_id: str,
        instance_id: str,
        member: Mapping[str, Any],
        prompt_sha256: str,
        candidate_tree_sha: str | None,
    ) -> dict[str, Any]:
        submission = {
            "run_id": run_id,
            "instance_id": instance_id,
            "agent_name": member["agent_name"],
            "prompt_sha256": prompt_sha256,
            "generation": state["generation"],
            "workspace_id": member["workspace_id"],
            "tab_id": member["tab_id"],
            "pane_id": member["pane_id"],
            "terminal_id": member["terminal_id"],
            "agent_session": _copy(member["agent_session"]),
            "candidate_tree_sha": candidate_tree_sha,
            "phase": "terminal",
            "status": "blocked",
            "reason": "Herdr agent was already blocked before prompt",
            "cancel_attempted": False,
        }
        state["submissions"][run_id] = submission
        self._save(rooted, state, exists=True)
        return _copy(submission)

    @staticmethod
    def _status_from_agent_state(state: str) -> str:
        if state in {"idle", "done"}:
            return "settled"
        if state == "blocked":
            return "blocked"
        if state == "working":
            return "working"
        return "indeterminate"

    def recover(self, run_id: str) -> dict[str, Any]:
        normalized_run = mission_state.normalize_uuid(run_id, "run_id")
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                with rooted.exclusive_lock(
                    self.lock_relative,
                    directory_modes=self.directory_modes,
                    file_mode=0o600,
                ):
                    state = self._load(rooted)
                    if state is None:
                        raise HerdrBackendError("Herdr backend state is missing")
                    submission = state["submissions"].get(normalized_run)
                    if not isinstance(submission, dict):
                        raise HerdrBackendError("Herdr submission is missing")
                    if submission["status"] in TERMINAL:
                        return _copy(submission)
                    if submission["phase"] == "prepared":
                        submission.update(
                            {
                                "phase": "terminal",
                                "status": "indeterminate",
                                "reason": "submit outcome was not durably confirmed",
                            }
                        )
                    else:
                        self._preflight(state)
                        member = self._member(state, submission["instance_id"])
                        result = self._command(
                            ["herdr", "agent", "get", submission["agent_name"]]
                        )
                        if result.returncode != 0:
                            submission.update(
                                {
                                    "phase": "terminal",
                                    "status": self._classify_error(result),
                                    "reason": _error_code(result) or "Herdr agent recovery failed",
                                }
                            )
                        else:
                            receipt = self._verify_agent_receipt(
                                _result_payload(result, "agent get"),
                                member,
                                allow_missing_session=member["agent_session"] is None,
                            )
                            self._freeze_agent_session(member, submission, receipt)
                            status = self._status_from_agent_state(
                                receipt["agent_status"]
                            )
                            submission["status"] = status
                            if status in TERMINAL:
                                submission["phase"] = "terminal"
                    self._save(rooted, state, exists=True)
                    return _copy(submission)
        except (fleet_safe_paths.SafePathError, mission_state.MissionStateError) as exc:
            raise HerdrBackendError(f"unsafe Herdr recovery state: {exc}") from exc

    def wait(self, run_id: str, *, timeout_ms: int) -> dict[str, Any]:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
            raise HerdrBackendError("Herdr wait timeout must be a positive integer")
        normalized_run = mission_state.normalize_uuid(run_id, "run_id")
        state = self.state()
        submission = state["submissions"].get(normalized_run)
        if not isinstance(submission, dict):
            raise HerdrBackendError("Herdr submission is missing")
        if submission["status"] in TERMINAL:
            return _copy(submission)
        self._preflight(state)
        member = self._member(state, submission["instance_id"])
        self._get_agent(
            member, allow_missing_session=member["agent_session"] is None
        )
        result = self._command(
            [
                "herdr",
                "agent",
                "wait",
                submission["agent_name"],
                "--timeout",
                str(timeout_ms),
            ],
            timeout=max(1, timeout_ms // 1000 + 5),
        )
        if result.returncode != 0:
            if _error_code(result) == "timeout":
                return self.recover(normalized_run)
            return self._record_terminal_error(normalized_run, result, "Herdr agent wait failed")
        return self.recover(normalized_run)

    def _record_terminal_error(
        self,
        run_id: str,
        result: subprocess.CompletedProcess[str],
        default_reason: str,
    ) -> dict[str, Any]:
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                with rooted.exclusive_lock(
                    self.lock_relative,
                    directory_modes=self.directory_modes,
                    file_mode=0o600,
                ):
                    state = self._load(rooted)
                    if state is None or run_id not in state["submissions"]:
                        raise HerdrBackendError("Herdr submission is missing")
                    submission = state["submissions"][run_id]
                    submission.update(
                        {
                            "phase": "terminal",
                            "status": self._classify_error(result),
                            "reason": _error_code(result) or default_reason,
                        }
                    )
                    self._save(rooted, state, exists=True)
                    return _copy(submission)
        except fleet_safe_paths.SafePathError as exc:
            raise HerdrBackendError(f"unsafe Herdr terminal state: {exc}") from exc

    def _result_relative(self, run_id: str) -> Path:
        return (
            Path("missions")
            / self.mission_id
            / "herdr-results"
            / f"{run_id}.json"
        )

    def _load_result(
        self, rooted: fleet_safe_paths.RootedFS, run_id: str
    ) -> dict[str, Any] | None:
        try:
            raw = rooted.read_regular_optional(
                self._result_relative(run_id),
                directory_modes=self.result_directory_modes,
                file_mode=0o600,
                max_bytes=fleet_artifacts.MAX_ARTIFACT_BYTES,
            )
        except fleet_safe_paths.SafePathError as exc:
            if not str(exc).startswith("rooted directory is missing:"):
                raise
            raw = None
        if raw is None:
            return None
        try:
            result = fleet_json.loads(raw)
        except fleet_json.FleetJSONError as exc:
            raise HerdrBackendError("Herdr result receipt is invalid") from exc
        if not isinstance(result, dict) or raw != fleet_json.canonical_bytes(result) + b"\n":
            raise HerdrBackendError("Herdr result receipt is not canonical")
        expected = {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "run_id": run_id,
        }
        if any(result.get(key) != value for key, value in expected.items()):
            raise HerdrBackendError("Herdr result receipt binding drift")
        artifact_id = result.get("artifact_id")
        envelope_id = result.get("result_artifact_id")
        if not all(
            isinstance(value, str) and SHA256.fullmatch(value)
            for value in (artifact_id, envelope_id)
        ):
            raise HerdrBackendError("Herdr result receipt CAS binding is invalid")
        try:
            fleet_artifacts.get_bytes(self.runs_dir, self.mission_id, artifact_id)
            envelope = dict(result)
            envelope.pop("result_artifact_id")
            if fleet_artifacts.get_bytes(
                self.runs_dir, self.mission_id, envelope_id
            ) != fleet_json.canonical_bytes(envelope):
                raise HerdrBackendError("Herdr result envelope CAS mismatch")
            evidence = result.get("evidence")
            if not isinstance(evidence, dict):
                raise HerdrBackendError("Herdr result transcript evidence is missing")
            evidence_session = evidence.get("agent_session")
            if (
                not isinstance(evidence_session, dict)
                or evidence_session.get("kind") != "id"
                or not all(
                    isinstance(evidence_session.get(key), str)
                    and evidence_session[key]
                    for key in ("agent", "source", "value")
                )
            ):
                raise HerdrBackendError(
                    "Herdr result requires a concrete agent_session"
                )
            transcript_id = evidence.get("transcript_artifact_id")
            transcript_sha256 = evidence.get("transcript_sha256")
            if (
                not isinstance(transcript_id, str)
                or not SHA256.fullmatch(transcript_id)
                or transcript_sha256 != transcript_id
            ):
                raise HerdrBackendError("Herdr result transcript CAS binding is invalid")
            transcript = fleet_artifacts.get_bytes(
                self.runs_dir, self.mission_id, transcript_id
            )
            if hashlib.sha256(transcript).hexdigest() != transcript_sha256:
                raise HerdrBackendError("Herdr result transcript digest mismatch")
        except fleet_artifacts.ArtifactError as exc:
            raise HerdrBackendError("Herdr result CAS is unavailable") from exc
        state = self._load(rooted)
        submission = state["submissions"].get(run_id) if state else None
        if not isinstance(submission, dict):
            raise HerdrBackendError("cached result lacks its bound submission")
        member = self._member(state, submission["instance_id"])
        if (evidence.get("runtime_contract") != state.get("runtime_contract")
                or evidence.get("context_artifact_id") != state.get("context_artifact_id")):
            raise HerdrBackendError("cached result runtime contract changed or downgraded")
        try:
            fleet_herdr_evidence.verify_result(result,
                read_artifact=lambda digest: fleet_artifacts.get_bytes(self.runs_dir, self.mission_id, digest),
                role=member["instance_id"], cwd=str(self.target_repo),
                prompt_sha256=submission["prompt_sha256"],
                agent_session=member["agent_session"]["value"],
                permission_version=self.profile.permissions_policy_version)
        except fleet_herdr_evidence.EvidenceError as exc:
            raise HerdrBackendError(f"cached result permission evidence: {exc}") from exc
        return result

    def _transcript_rows(
        self, agent_session: str
    ) -> tuple[list[dict[str, Any]], list[bytes], str, str]:
        transcript = self.transcript_resolver(agent_session)
        if transcript is None:
            return [], [], "", ""
        try:
            path = Path(transcript).expanduser().resolve(strict=True)
            raw = fleet_artifacts.read_regular(path)
        except (OSError, fleet_artifacts.ArtifactError) as exc:
            raise HerdrBackendError("Codex transcript is not a safe regular file") from exc
        rows: list[dict[str, Any]] = []
        row_bytes: list[bytes] = []
        lines = raw.splitlines(keepends=True)
        if raw and not raw.endswith(b"\n"):
            lines = lines[:-1]
        for line in lines:
            if not line.strip():
                continue
            try:
                row = fleet_json.loads(line)
            except fleet_json.FleetJSONError as exc:
                raise HerdrBackendError("Codex transcript contains invalid JSON") from exc
            if not isinstance(row, dict):
                raise HerdrBackendError("Codex transcript row is not an object")
            rows.append(row)
            row_bytes.append(line)
        return rows, row_bytes, str(path), hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _usage_counts(value: Any) -> dict[str, int] | None:
        keys = ("input_tokens", "output_tokens", "cached_input_tokens")
        if (not isinstance(value, dict)
                or any(type(value.get(key)) is not int or value[key] < 0 for key in keys)
                or value["cached_input_tokens"] > value["input_tokens"]):
            return None
        return {key: value[key] for key in keys}

    def _capture_usage_baseline(self, *, run_id: str, prompt_sha256: str,
                                generation: str, member: Mapping[str, Any]) -> str:
        """Pin the cumulative counter frontier before a prompt can be sent."""
        session = member.get("agent_session")
        evidence: dict[str, Any] = {"schema_version": 1,
            "kind": "herdr_usage_baseline", "mission_id": self.mission_id,
            "run_id": run_id, "prompt_sha256": prompt_sha256,
            "generation": generation, "agent_session": _copy(session),
            "captured_at": _now(), "status": "unknown", "counts": None,
            "source": None, "reason": "session_identity_not_available_before_dispatch",
            "transcript_sha256": None, "captured_rows": 0}
        if session is not None:
            rows, row_bytes, _, _ = self._transcript_rows(session["value"])
            prefix_sha = hashlib.sha256(b"".join(row_bytes)).hexdigest() if row_bytes else None
            evidence.update(transcript_sha256=prefix_sha,
                            captured_rows=len(rows))
            metadata = [row for row in rows if row.get("type") == "session_meta"
                        and isinstance(row.get("payload"), dict)
                        and row["payload"].get("id") == session["value"]]
            # A rotated/reset live transcript cannot become a new zero baseline
            # after this same owned session already completed a durable run.
            prior_pins = []
            try:
                with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                    prior_state = self._load(rooted)
                    for prior_run, prior in prior_state["submissions"].items():
                        if (prior_run == run_id or prior.get("instance_id") != member["instance_id"]
                                or prior.get("generation") != generation or not prior.get("submitted_at")):
                            continue
                        previous = self._load_result(rooted, prior_run)
                        if previous is None or previous["evidence"]["agent_session"] != session:
                            raise ValueError("prior session result unavailable")
                        pin = previous["evidence"]["transcript_artifact_id"]
                        retained = fleet_artifacts.get_bytes(self.runs_dir, self.mission_id, pin)
                        retained_rows = retained.splitlines(keepends=True)
                        # Collection retains metadata then the exact bound segment.
                        segment = b"".join(retained_rows[1:])
                        if not segment or segment not in b"".join(row_bytes):
                            raise ValueError("prior session transcript changed")
                        prior_pins.append(previous["result_artifact_id"])
            except (ValueError, RuntimeError, OSError, KeyError, TypeError):
                evidence["reason"] = "prior_session_history_unavailable_or_changed"
                return fleet_artifacts.put_bytes(self.runs_dir, self.mission_id,
                    fleet_json.canonical_bytes(evidence))["artifact_id"]
            if prior_pins:
                evidence["prior_result_artifact_ids"] = sorted(prior_pins)
            active: set[str] = set()
            snapshots = []
            invalid_counter = False
            counter_regression = False
            for row in rows:
                payload = row.get("payload", {})
                if row.get("type") != "event_msg" or not isinstance(payload, dict):
                    continue
                if payload.get("type") == "task_started" and isinstance(payload.get("turn_id"), str):
                    active.add(payload["turn_id"])
                elif payload.get("type") == "task_complete":
                    active.discard(payload.get("turn_id"))
                elif payload.get("type") == "token_count":
                    counts = self._usage_counts((payload.get("info") or {}).get("total_token_usage"))
                    if counts is not None:
                        if snapshots and any(counts[k] < snapshots[-1][k] for k in counts):
                            counter_regression = True
                        snapshots.append(counts)
                    else:
                        invalid_counter = True
            if len(metadata) != 1 or metadata[0]["payload"].get("model_provider") != "openai":
                evidence["reason"] = "session_metadata_unavailable_or_ambiguous"
            elif active:
                evidence["reason"] = "overlapping_turn_at_dispatch"
            elif invalid_counter:
                evidence["reason"] = "invalid_usage_counter_snapshot"
            elif counter_regression:
                evidence["reason"] = "usage_counter_reset_or_regression"
            elif snapshots:
                evidence.update(status="known", counts=snapshots[-1],
                    source="pre_dispatch_session_counter", reason=None)
            elif not any(row.get("type") == "event_msg"
                         and row.get("payload", {}).get("type") == "task_started" for row in rows):
                evidence.update(status="known",
                    counts={"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
                    source="observed_empty_session_before_first_turn", reason=None)
            else:
                evidence["reason"] = "prior_turn_has_no_counter_frontier"
        return fleet_artifacts.put_bytes(self.runs_dir, self.mission_id,
                                         fleet_json.canonical_bytes(evidence))["artifact_id"]

    def _transcript_result(
        self,
        submission: Mapping[str, Any],
        member: Mapping[str, Any],
    ) -> tuple[Any, bytes, str, str, bytes, str, dict[str, Any]] | None:
        agent_session = member["agent_session"]["value"]
        rows, row_bytes, transcript_path, _ = self._transcript_rows(agent_session)
        if not rows:
            return None
        metadata = [
            (index, row["payload"])
            for index, row in enumerate(rows)
            if row.get("type") == "session_meta"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("id") == agent_session
        ]
        if len(metadata) != 1 or metadata[0][1].get("model_provider") != "openai":
            raise HerdrBackendError("Codex transcript session identity mismatch")
        starts: list[tuple[str, int]] = []
        for index, row in enumerate(rows):
            payload = row.get("payload")
            if (
                row.get("type") == "event_msg"
                and isinstance(payload, dict)
                and payload.get("type") == "task_started"
                and isinstance(payload.get("turn_id"), str)
                and payload["turn_id"]
                and (not starts or starts[-1][0] != payload["turn_id"])
            ):
                starts.append((payload["turn_id"], index))
        matching: list[tuple[str, int, int]] = []
        for position, (turn_id, start) in enumerate(starts):
            end = starts[position + 1][1] if position + 1 < len(starts) else len(rows)
            prompt_matches = 0
            for row in rows[start:end]:
                payload = row.get("payload")
                if (
                    row.get("type") == "response_item"
                    and isinstance(payload, dict)
                    and payload.get("type") == "message"
                    and payload.get("role") == "user"
                ):
                    prompt = _message_text(payload.get("content"), kind="input_text")
                    if mission_state.artifact_id(prompt) == submission["prompt_sha256"]:
                        prompt_matches += 1
            if prompt_matches > 1:
                raise HerdrBackendError("Codex transcript prompt binding is ambiguous")
            if prompt_matches == 1:
                matching.append((turn_id, start, end))
        if not matching:
            return None
        if len(matching) != 1:
            raise HerdrBackendError("Codex transcript prompt binding is ambiguous")
        turn_id, start, end = matching[0]
        finals: list[tuple[int, str]] = []
        for index in range(start, end):
            row = rows[index]
            payload = row.get("payload")
            if (
                row.get("type") == "response_item"
                and isinstance(payload, dict)
                and payload.get("type") == "message"
                and payload.get("role") == "assistant"
                and payload.get("phase") == "final_answer"
            ):
                text = _message_text(payload.get("content"), kind="output_text")
                if text:
                    finals.append((index, text))
        if not finals:
            return None
        if len(finals) != 1:
            raise HerdrBackendError("Codex transcript final result is ambiguous")
        final_index, final_text = finals[0]
        completed = []
        for index in range(final_index + 1, end):
            row = rows[index]
            if (
                row.get("type") == "event_msg"
                and isinstance(row.get("payload"), dict)
                and row["payload"].get("type") == "task_complete"
                and row["payload"].get("turn_id") == turn_id
            ):
                completed.append((index, row["payload"]))
        if not completed:
            return None
        if len(completed) != 1:
            raise HerdrBackendError("Codex task_complete binding is ambiguous")
        # Preserve a uniquely identified completion even when its rendered text
        # differs. The strict evidence verifier below rejects that mismatch
        # after retaining both versions, so recovery cannot spin or admit it.
        complete_index = completed[0][0]
        segment_start = start
        usage_frontier = {"status": "not_applicable", "reason": None}
        baseline_id = submission.get("usage_baseline_artifact_id")
        if baseline_id is not None:
            baseline = fleet_json.loads(fleet_artifacts.get_bytes(
                self.runs_dir, self.mission_id, baseline_id))
            captured_rows = baseline.get("captured_rows")
            prefix_valid = (type(captured_rows) is int and 0 <= captured_rows <= start)
            if prefix_valid and baseline.get("transcript_sha256") is not None:
                prefix_valid = (hashlib.sha256(b"".join(row_bytes[:captured_rows])).hexdigest()
                                == baseline["transcript_sha256"])
            if prefix_valid:
                segment_start = captured_rows
                usage_frontier = {"status": "verified", "reason": None}
            else:
                # A session transcript is mutable external evidence. Its
                # replacement/reset makes the counter delta unknown, but does
                # not invalidate an otherwise bound completed role result.
                usage_frontier = {"status": "unknown",
                    "reason": "usage_baseline_transcript_frontier_changed"}
        selected = list(range(segment_start, complete_index + 1))
        if metadata[0][0] not in selected:
            selected.insert(0, metadata[0][0])
        transcript_segment = b"".join(row_bytes[index] for index in selected)
        transcript_sha256 = hashlib.sha256(transcript_segment).hexdigest()
        # Content is classified by collect_result, after the turn is bound, so an
        # unbound final is retained as a delivery rejection instead of spinning.
        final_bytes = final_text.encode("utf-8")
        try:
            raw_result = fleet_json.loads(final_bytes)
        except fleet_json.FleetJSONError:
            raw_result = None
        return (
            raw_result,
            final_bytes,
            turn_id,
            transcript_path,
            transcript_segment,
            transcript_sha256,
            usage_frontier,
        )

    def collect_result(self, run_id: str) -> dict[str, Any] | None:
        normalized_run = mission_state.normalize_uuid(run_id, "run_id")
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                with rooted.exclusive_lock(
                    self.lock_relative,
                    directory_modes=self.directory_modes,
                    file_mode=0o600,
                ):
                    existing = self._load_result(rooted, normalized_run)
                    if existing is not None:
                        return _copy(existing)
                    state = self._load(rooted)
                    if state is None:
                        raise HerdrBackendError("Herdr backend state is missing")
                    submission = state["submissions"].get(normalized_run)
                    if not isinstance(submission, dict):
                        raise HerdrBackendError("Herdr submission is missing")
                    member = self._member(state, submission["instance_id"])
                    if member["agent_session"] is None:
                        self._preflight(state)
                        receipt = self._get_agent(
                            member, allow_missing_session=True
                        )
                        if not self._freeze_agent_session(
                            member, submission, receipt
                        ):
                            return None
                        self._save(rooted, state, exists=True)
                    rejected = fleet_herdr_rejection.load(self.runs_dir, self.mission_id, normalized_run, state)
                    if rejected is not None:
                        raise ExecutionEvidenceRejected(rejected)
                    delivered = fleet_herdr_rejection.load_delivery(
                        self.runs_dir, self.mission_id, normalized_run, state)
                    if delivered is not None:
                        raise DeliveryRejected(delivered)
                    observed = self._transcript_result(submission, member)
                    if observed is None:
                        return None
                    (
                        _,
                        final_bytes,
                        turn_id,
                        transcript_path,
                        transcript_segment,
                        transcript_sha256,
                        usage_frontier,
                    ) = observed
                    expected = {
                        "schema_version": 1,
                        "mission_id": self.mission_id,
                        "run_id": normalized_run,
                        "instance_id": submission["instance_id"],
                    }
                    # Cache attributed execution bytes even when their role protocol
                    # is invalid. The driver durably adjudicates that separate fact.
                    problem = fleet_herdr_rejection.delivery_problem(
                        final_bytes, expected=expected,
                        candidate_tree_sha=submission.get("candidate_tree_sha"))
                    final_artifact = fleet_artifacts.put_bytes(
                        self.runs_dir, self.mission_id, final_bytes
                    )
                    transcript_artifact = fleet_artifacts.put_bytes(
                        self.runs_dir, self.mission_id, transcript_segment
                    )
                    baseline_id = submission.get("usage_baseline_artifact_id")
                    if baseline_id is not None:
                        baseline = fleet_json.loads(fleet_artifacts.get_bytes(
                            self.runs_dir, self.mission_id, baseline_id))
                        if (not isinstance(baseline, dict)
                                or baseline.get("mission_id") != self.mission_id
                                or baseline.get("run_id") != normalized_run
                                or baseline.get("prompt_sha256") != submission["prompt_sha256"]
                                or baseline.get("generation") != state["generation"]
                                or baseline.get("agent_session") not in (None, member["agent_session"])):
                            raise HerdrBackendError("usage baseline CAS binding mismatch")
                    evidence = {
                        **({"runtime_contract": _copy(state["runtime_contract"])} if state.get("runtime_contract") else {}),
                        **({"context_artifact_id": state["context_artifact_id"]} if state.get("context_artifact_id") else {}),
                        "herdr_session": self.session,
                        "generation": state["generation"],
                        "workspace_id": member["workspace_id"],
                        "tab_id": member["tab_id"],
                        "pane_id": member["pane_id"],
                        "terminal_id": member["terminal_id"],
                        "agent_session": _copy(member["agent_session"]),
                        "prompt_sha256": submission["prompt_sha256"],
                        "transcript_path": transcript_path,
                        "transcript_sha256": transcript_sha256,
                        "transcript_artifact_id": transcript_artifact["artifact_id"],
                        **({"usage_baseline_artifact_id": baseline_id}
                           if baseline_id is not None else {}),
                        "usage_baseline_frontier": usage_frontier,
                    }
                    if problem is not None:
                        # Controller-derived identity only; no model-authored field.
                        envelope = {
                            "mission_id": self.mission_id,
                            "run_id": normalized_run,
                            "instance_id": submission["instance_id"],
                            "candidate_tree_sha": submission.get("candidate_tree_sha"),
                            "delivery": "unbound_final",
                            "artifact_id": final_artifact["artifact_id"],
                            "turn_id": turn_id,
                            "evidence": evidence,
                        }
                        execution = fleet_herdr_rejection.execution_status(envelope,
                            read=lambda digest: fleet_artifacts.get_bytes(self.runs_dir, self.mission_id, digest),
                            role=member["instance_id"], cwd=str(self.target_repo),
                            prompt_sha256=submission["prompt_sha256"],
                            agent_session=member["agent_session"]["value"],
                            permission_version=self.profile.permissions_policy_version)
                        proof = fleet_herdr_rejection.retain_delivery(self.runs_dir, self.mission_id,
                            normalized_run, envelope, problem, execution, rooted)
                        raise DeliveryRejected(proof)
                    result = {
                        **fleet_json.loads(final_bytes),
                        "artifact_id": final_artifact["artifact_id"],
                        "turn_id": turn_id,
                        "evidence": evidence,
                    }
                    try:
                        result["evidence"]["permissions"] = fleet_herdr_evidence.verify_result(result,
                            read_artifact=lambda digest: fleet_artifacts.get_bytes(self.runs_dir, self.mission_id, digest),
                            role=member["instance_id"], cwd=str(self.target_repo),
                            prompt_sha256=submission["prompt_sha256"],
                            agent_session=member["agent_session"]["value"],
                            permission_version=self.profile.permissions_policy_version)
                    except fleet_herdr_evidence.EvidenceError as exc:
                        proof = fleet_herdr_rejection.retain(self.runs_dir, self.mission_id,
                            normalized_run, result, str(exc), rooted)
                        raise ExecutionEvidenceRejected(proof) from exc
                    envelope_artifact = fleet_artifacts.put_bytes(
                        self.runs_dir,
                        self.mission_id,
                        fleet_json.canonical_bytes(result),
                    )
                    result["result_artifact_id"] = envelope_artifact["artifact_id"]
                    rooted.atomic_write(
                        self._result_relative(normalized_run),
                        fleet_json.canonical_bytes(result) + b"\n",
                        directory_modes=self.result_directory_modes,
                        file_mode=0o600,
                        require_absent=True,
                    )
                    submission.update(
                        {
                            "phase": "terminal",
                            "status": {
                                "PASS": "succeeded",
                                "BLOCKED": "blocked",
                                "FAIL": "failed",
                            }.get(str(result.get("status")), "indeterminate"),
                            "artifact_id": result["artifact_id"],
                            "result_artifact_id": result["result_artifact_id"],
                            "turn_id": turn_id,
                        }
                    )
                    self._save(rooted, state, exists=True)
                    return _copy(result)
        except (
            fleet_safe_paths.SafePathError,
            mission_state.MissionStateError,
            fleet_artifacts.ArtifactError,
        ) as exc:
            raise HerdrBackendError(f"unsafe Herdr result state: {exc}") from exc

    def _active_turn_id(
        self, submission: Mapping[str, Any], member: Mapping[str, Any]
    ) -> str | None:
        if member.get("agent_session") is None:
            return None
        agent_session = member["agent_session"]["value"]
        rows, _, _, _ = self._transcript_rows(agent_session)
        if not rows:
            return None
        metadata = [
            row["payload"]
            for row in rows
            if row.get("type") == "session_meta"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("id") == agent_session
        ]
        if len(metadata) != 1 or metadata[0].get("model_provider") != "openai":
            raise HerdrBackendError("Codex transcript session identity mismatch")
        starts: list[tuple[str, int]] = []
        for index, row in enumerate(rows):
            payload = row.get("payload")
            if (
                row.get("type") == "event_msg"
                and isinstance(payload, dict)
                and payload.get("type") == "task_started"
                and isinstance(payload.get("turn_id"), str)
                and payload["turn_id"]
                and (not starts or starts[-1][0] != payload["turn_id"])
            ):
                starts.append((payload["turn_id"], index))
        if not starts:
            return None
        turn_id, start = starts[-1]
        segment = rows[start:]
        if any(
            row.get("type") == "event_msg"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("type") == "task_complete"
            and row["payload"].get("turn_id") == turn_id
            for row in segment
        ):
            return None
        prompt_hashes = []
        for row in segment:
            payload = row.get("payload")
            if (
                row.get("type") == "response_item"
                and isinstance(payload, dict)
                and payload.get("type") == "message"
                and payload.get("role") == "user"
            ):
                prompt_hashes.append(
                    mission_state.artifact_id(
                        _message_text(payload.get("content"), kind="input_text")
                    )
                )
        if prompt_hashes != [submission["prompt_sha256"]]:
            return None
        contexts = [
            row["payload"]
            for row in segment
            if row.get("type") == "turn_context"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("turn_id") == turn_id
        ]
        if not contexts or any(
            context.get("model") != member["model"]
            or context.get("effort") != "high"
            for context in contexts
        ):
            return None
        return turn_id

    def cancel(self, run_id: str) -> dict[str, Any]:
        normalized_run = mission_state.normalize_uuid(run_id, "run_id")
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                with rooted.exclusive_lock(
                    self.lock_relative,
                    directory_modes=self.directory_modes,
                    file_mode=0o600,
                ):
                    state = self._load(rooted)
                    if state is None:
                        raise HerdrBackendError("Herdr backend state is missing")
                    submission = state["submissions"].get(normalized_run)
                    if not isinstance(submission, dict):
                        raise HerdrBackendError("Herdr submission is missing")
                    if submission["status"] == "not_sent":
                        # No prompt reached Herdr, so this run has no runtime to signal.
                        submission.update(
                            {
                                "status": "abandoned",
                                "cancel_attempted": True,
                                "reason": "cancelled before delivery; prompt command never started",
                            }
                        )
                        self._save(rooted, state, exists=True)
                        return _copy(submission)
                    # An uncertain transport outcome is not proof of quiescence;
                    # an explicit cancellation must reconcile the exact resource.
                    if submission["status"] in TERMINAL and submission["status"] != "indeterminate":
                        rejection = load_result_rejection(self.runs_dir, self.mission_id, normalized_run)
                        if submission["status"] == "abandoned" or rejection is None:
                            return _copy(submission)
                        cached = self._load_result(rooted, normalized_run)
                        if cached is None:
                            raise HerdrBackendError("rejected result is no longer available")
                        load_result_rejection(self.runs_dir, self.mission_id, normalized_run, result=cached)
                    self._preflight(state)
                    member = self._member(state, submission["instance_id"])
                    receipt = self._get_agent(
                        member,
                        allow_missing_session=member["agent_session"] is None,
                    )
                    if self._freeze_agent_session(member, submission, receipt):
                        self._save(rooted, state, exists=True)
                    if submission["cancel_attempted"]:
                        if receipt["agent_status"] in QUIESCENT:
                            submission.update(
                                {
                                    "phase": "terminal",
                                    "status": "abandoned",
                                    "reason": "cancel quiescence recovered",
                                }
                            )
                        self._save(rooted, state, exists=True)
                        return _copy(submission)
                    if receipt["agent_status"] in QUIESCENT:
                        submission.update(
                            {
                                "phase": "terminal",
                                "status": "abandoned",
                                "cancel_attempted": True,
                                "reason": "cancellation found the exact agent quiescent",
                            }
                        )
                        self._save(rooted, state, exists=True)
                        return _copy(submission)
                    active_turn = self._active_turn_id(submission, member)
                    if active_turn is None:
                        submission["cancel_blocked_reason"] = (
                            "exact admitted turn is not the active incomplete Codex turn"
                        )
                        self._save(rooted, state, exists=True)
                        observation = _copy(submission)
                        observation.update(
                            {
                                "status": "indeterminate",
                                "reason": submission["cancel_blocked_reason"],
                            }
                        )
                        return observation
                    submission["cancel_turn_id"] = active_turn
                    submission["cancel_attempted"] = True
                    self._save(rooted, state, exists=True)
                    result = self._command(
                        [
                            "herdr",
                            "agent",
                            "send-keys",
                            submission["agent_name"],
                            "ctrl+c",
                        ]
                    )
                    if result.returncode != 0:
                        submission.update(
                            {
                                "phase": "terminal",
                                "status": "indeterminate",
                                "reason": _error_code(result) or "Herdr cancel failed",
                            }
                        )
                    else:
                        waited = self._command(
                            [
                                "herdr",
                                "agent",
                                "wait",
                                submission["agent_name"],
                                "--until",
                                "idle",
                                "--until",
                                "done",
                                "--until",
                                "blocked",
                                "--timeout",
                                "5000",
                            ],
                            timeout=10,
                        )
                        quiescent = False
                        if waited.returncode == 0:
                            quiescent = self._get_agent(
                                member,
                                allow_missing_session=member["agent_session"] is None,
                            )["agent_status"] in QUIESCENT
                        submission.update(
                            {
                                "phase": "terminal",
                                "status": "abandoned" if quiescent else "indeterminate",
                                "reason": (
                                    "cancelled and quiescence verified"
                                    if quiescent
                                    else _error_code(waited)
                                    or "Herdr cancel quiescence was not verified"
                                ),
                            }
                        )
                    self._save(rooted, state, exists=True)
                    return _copy(submission)
        except (fleet_safe_paths.SafePathError, mission_state.MissionStateError) as exc:
            raise HerdrBackendError(f"unsafe Herdr cancel state: {exc}") from exc

    def teardown(self) -> bool:
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                with rooted.exclusive_lock(
                    self.lock_relative,
                    directory_modes=self.directory_modes,
                    file_mode=0o600,
                ):
                    state = self._load(rooted)
                    if state is None:
                        return False
                    workspace = state["workspace"]
                    if not workspace["owned"] or workspace["closed"]:
                        return False
                    self._preflight(state)
                    if workspace["close_attempted"]:
                        if self._get_workspace(workspace) is None:
                            workspace["closed"] = True
                            state["phase"] = "closed"
                            self._save(rooted, state, exists=True)
                            return True
                        raise HerdrBackendError(
                            "Herdr workspace close is indeterminate; refusing a duplicate"
                        )
                    workspace_id = _nonempty(
                        workspace.get("workspace_id"), "owned Herdr workspace_id"
                    )
                    if any(
                        submission.get("phase") != "terminal"
                        or submission.get("status") not in TERMINAL
                        for submission in state["submissions"].values()
                    ):
                        raise HerdrBackendError(
                            "Herdr workspace has non-terminal submissions"
                        )
                    self._revalidate_workspace(state)
                    statuses = []
                    for member in state["members"]:
                        statuses.append(
                            self._get_agent(
                                member,
                                allow_missing_session=member["agent_session"] is None,
                            )["agent_status"]
                        )
                    if any(status not in {"idle", "done"} for status in statuses):
                        raise HerdrBackendError(
                            "Herdr workspace agents are not safely quiescent"
                        )
                    workspace["close_attempted"] = True
                    self._save(rooted, state, exists=True)
                    result = self._command(
                        ["herdr", "workspace", "close", workspace_id]
                    )
                    if result.returncode != 0:
                        raise HerdrBackendError(
                            "Herdr workspace close failed: "
                            + (result.stderr.strip() or result.stdout.strip())
                        )
                    workspace["closed"] = True
                    state["phase"] = "closed"
                    self._save(rooted, state, exists=True)
                    return True
        except fleet_safe_paths.SafePathError as exc:
            raise HerdrBackendError(f"unsafe Herdr teardown state: {exc}") from exc

    def state(self) -> dict[str, Any]:
        try:
            with fleet_safe_paths.RootedFS(self.runs_dir) as rooted:
                state = self._load(rooted)
        except fleet_safe_paths.SafePathError as exc:
            raise HerdrBackendError(f"unsafe Herdr backend state: {exc}") from exc
        if state is None:
            raise HerdrBackendError("Herdr backend state is missing")
        return _copy(state)
