"""Closed, versioned Herdr runtime profiles shared by every lifecycle consumer."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import fleet_json


LEGACY_PRESET = "astra_sol"
RESEARCH_PRESET = "astra_sol_research_v1"
HERDR_PRESETS = frozenset({LEGACY_PRESET, RESEARCH_PRESET})


@dataclass(frozen=True)
class HerdrProfile:
    profile_id: str
    preset: str
    stages: tuple[tuple[str, str, str], ...]
    members: tuple[tuple[str, str, str, str, str], ...]
    input_policy: str
    permissions_policy_version: int
    minimum_archive_schema_version: int
    archive_schema_version: int
    result_roles: frozenset[str]
    writer_instance: str
    allow_capsule: bool
    allow_experimental_launch: bool

    def contract(self) -> dict[str, Any]:
        return {
            "schema_version": "fleet.herdr.profile.v1",
            "profile_id": self.profile_id,
            "preset": self.preset,
            "stages": [
                {"stage": stage, "instance_id": instance, "capability": capability}
                for stage, instance, capability in self.stages
            ],
            "members": [
                {
                    "instance_id": instance,
                    "role_type": role_type,
                    "model": model,
                    "phase": phase,
                    "authority": authority,
                }
                for instance, role_type, model, phase, authority in self.members
            ],
            "input_policy": self.input_policy,
            "permissions_policy_version": self.permissions_policy_version,
            "minimum_archive_schema_version": self.minimum_archive_schema_version,
            "archive_schema_version": self.archive_schema_version,
            "result_roles": sorted(self.result_roles),
            "writer_instance": self.writer_instance,
            "allow_capsule": self.allow_capsule,
            "allow_experimental_launch": self.allow_experimental_launch,
        }

    @property
    def digest(self) -> str:
        return fleet_json.sha256(self.contract())


LEGACY = HerdrProfile(
    profile_id="astra_sol_v1",
    preset=LEGACY_PRESET,
    stages=(
        ("plan", "lead", "recon"),
        ("build", "worker", "build"),
        ("review", "reviewer", "challenge"),
        ("verify", "verifier", "verify"),
        ("synthesis", "lead", "synthesis"),
    ),
    members=(
        ("lead", "astra_lead", "gpt-6-astra", "CONTROL", "control"),
        ("worker", "sol_worker", "gpt-5.6-sol", "BUILD", "write"),
        ("reviewer", "sol_reviewer", "gpt-5.6-sol", "CHALLENGE", "advisory"),
        ("verifier", "sol_verifier", "gpt-5.6-sol", "VERIFY", "verification"),
    ),
    input_policy="independent-v1",
    permissions_policy_version=1,
    minimum_archive_schema_version=3,
    archive_schema_version=3,
    result_roles=frozenset({"lead", "worker", "reviewer", "verifier"}),
    writer_instance="worker",
    allow_capsule=True,
    allow_experimental_launch=True,
)

RESEARCH = HerdrProfile(
    profile_id=RESEARCH_PRESET,
    preset=RESEARCH_PRESET,
    stages=(
        ("plan", "lead", "recon"),
        ("research", "research", "recon"),
        ("build", "worker", "build"),
        ("review", "reviewer", "challenge"),
        ("verify", "verifier", "verify"),
        ("synthesis", "lead", "synthesis"),
    ),
    members=(
        ("lead", "astra_lead", "gpt-6-astra", "CONTROL", "control"),
        ("research", "astra_research", "gpt-6-astra", "RECON", "advisory"),
        ("worker", "sol_worker", "gpt-5.6-sol", "BUILD", "write"),
        ("reviewer", "sol_reviewer", "gpt-5.6-sol", "CHALLENGE", "advisory"),
        ("verifier", "sol_verifier", "gpt-5.6-sol", "VERIFY", "verification"),
    ),
    input_policy="independent-research-v1",
    permissions_policy_version=3,
    minimum_archive_schema_version=6,
    archive_schema_version=6,
    result_roles=frozenset({"lead", "research", "worker", "reviewer", "verifier"}),
    writer_instance="worker",
    allow_capsule=False,
    allow_experimental_launch=False,
)

BY_PRESET = {profile.preset: profile for profile in (LEGACY, RESEARCH)}


class ProfileError(ValueError):
    """A compiled workflow or durable profile binding is unsupported."""


def is_herdr_preset(preset: Any) -> bool:
    return type(preset) is str and preset in HERDR_PRESETS


def resolve_profile(compiled: dict[str, Any]) -> HerdrProfile:
    try:
        resolved = compiled["resolved"]
        workflow = compiled["workflow"]
        profile = BY_PRESET[resolved["preset"]]
        members = [resolved["lead"], *resolved["instances"]]
    except (KeyError, TypeError) as exc:
        raise ProfileError("compiled workflow has no supported Herdr profile") from exc
    actual = [
        tuple(member.get(key) for key in ("instance_id", "role_type", "model", "phase", "authority"))
        for member in members
        if isinstance(member, dict)
    ]
    if (
        resolved.get("mode") != "autonomous"
        or actual != list(profile.members)
        or resolved.get("writer_instance") != profile.writer_instance
        or any(
            member.get("provider") != "openai"
            or member.get("runner") != "interactive"
            or member.get("hook_source") != "codex"
            for member in members
        )
    ):
        raise ProfileError("compiled Herdr profile identity drift")
    if profile is RESEARCH:
        if (
            workflow.get("name") != "herdr-research-implementation"
            or workflow.get("assurance", {}).get("profile") != "none"
            or set(workflow.get("capabilities", {}).get("required_outcomes", []))
            != {"lead_result", "research_result", "worker_result", "reviewer_result", "verifier_result"}
        ):
            raise ProfileError("Research workflow contract is incomplete")
    return profile


def creation_binding(compiled: dict[str, Any]) -> dict[str, Any]:
    profile = resolve_profile(compiled)
    if profile is LEGACY:
        return {}
    return {"herdr_profile": profile.profile_id, "herdr_profile_sha256": profile.digest}


def runtime_binding(profile: HerdrProfile) -> dict[str, Any]:
    return {
        "herdr_profile": profile.profile_id,
        "herdr_profile_sha256": profile.digest,
        "herdr_input_policy": profile.input_policy,
    }


def validate_creation_binding(compiled: dict[str, Any], request: Any) -> HerdrProfile:
    profile = resolve_profile(compiled)
    if not isinstance(request, dict):
        raise ProfileError("Herdr creation request must be an object")
    expected = creation_binding(compiled)
    observed = {
        key: request.get(key)
        for key in ("herdr_profile", "herdr_profile_sha256")
        if key in request
    }
    if observed != expected:
        raise ProfileError("creation Herdr profile binding differs from compiled profile")
    return profile


def validate_profile_binding(
    compiled: dict[str, Any], runtime_options: dict[str, Any], current: dict[str, Any] | None = None
) -> HerdrProfile:
    profile = resolve_profile(compiled)
    if not isinstance(runtime_options, dict):
        raise ProfileError("Herdr runtime options must be an object")
    supplied = {
        key: runtime_options.get(key)
        for key in ("herdr_profile", "herdr_profile_sha256", "herdr_input_policy")
    }
    if profile is LEGACY:
        if supplied["herdr_profile"] is None and supplied["herdr_profile_sha256"] is None:
            if supplied["herdr_input_policy"] not in {None, profile.input_policy}:
                raise ProfileError("unsupported historical Herdr input policy")
        elif supplied != runtime_binding(profile):
            raise ProfileError("historical Herdr profile binding drift")
    elif supplied != runtime_binding(profile):
        raise ProfileError("Research Herdr profile binding is missing or changed")
    if profile is RESEARCH:
        from fleet_herdr_personal import PROFILE as PERSONAL_PROFILE
        if runtime_options.get("herdr_personal_cli") != PERSONAL_PROFILE:
            raise ProfileError("Research profile requires its exact personal Codex CLI binding")
        if (runtime_options.get("herdr_capsule_manifest") is not None
                or runtime_options.get("herdr_launch_manifest") is not None
                or "executor" in runtime_options):
            raise ProfileError("Research profile does not accept an alternate runtime executor")
    if current is not None:
        expected = creation_binding(compiled)
        observed = {
            key: current.get(key) for key in ("herdr_profile", "herdr_profile_sha256")
            if key in current
        }
        if observed != expected:
            raise ProfileError("ledger Herdr profile binding differs from compiled profile")
    return profile
