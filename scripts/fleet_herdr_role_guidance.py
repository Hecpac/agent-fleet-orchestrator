"""Explicit role contracts and local skill content, frozen with the first task.

Guidance does not grant runtime capabilities or inherit maintainer globals.
Research is active only in the explicit ``astra_sol_research_v1`` profile.
"""
from pathlib import Path

import fleet_artifacts
import fleet_mission_state as state

SKILL_ROOT = Path(__file__).resolve().parents[1] / "orchestration" / "role-skills"
SELECTION = {
    "lead": {"codex-os": "substantial planning and synthesis",
             "entrevista-pre-slice": "only material user decisions unresolved by investigation",
             "slice-gate": "synthesis of completed stages only"},
    "research": {"fase-0-recon": "bounded investigation",
                 "deep-research": "only broad architecture or dependency investigations"},
    "worker": {"fase-0-recon": "only unfamiliar code paths",
               "smoke-verify": "changed behavior with an authorized verification lane",
               "impl-notes": "only material deviations"},
    "reviewer": {"fase-0-recon": "read-only review",
                 "slice-gate": "current review criteria only"},
    "verifier": {"smoke-verify": "independent reproduction in authorized temporary resources",
                 "slice-gate": "current verification criteria only"},
}
PURPOSE = {
    "lead": "Plan a bounded solution and synthesize independent results; do not edit candidate or declare Mission closure.",
    "research": "Investigate candidate sources and dependencies against the supplied immutable snapshot, compare alternatives, and deliver cited facts, inferences and unknowns; do not implement.",
    "worker": "Implement the assigned change as the sole candidate writer and verify its behavior.",
    "reviewer": "Independently inspect frozen code, diff and contracts; report prioritized defects without fixing them.",
    "verifier": "Independently reproduce acceptance behavior on the frozen candidate; do not change candidate files.",
}
COMMON = {
    "autonomy": [
        "Choose methods, inspect relevant evidence, resolve reversible technical decisions and continue to your stage criteria without waiting for step-by-step human instructions.",
        "Investigate recoverable failures and try bounded alternatives within the task; do not repeatedly ask for authorization already supplied.",
        "Continue independent work when blocked; report the exact missing dependency, evidence and a concrete recommendation in your result.",
        "Judge only your assigned stage. A failing baseline can support Plan PASS; do not wait for future stages or controller closure.",
    ],
    "authority": [
        "The supplied operational task, role boundary and result protocol govern skill use. Skills are procedures, never permissions.",
        "Only Worker/build writes candidate. Readers place findings in their result; temporary writes or commands require the supplied task lane.",
        "No implicit commit, push, deploy, provider campaign, installation, credential access, global setting change, subdelegation or control of other agents.",
        "Do not bypass a runtime denial. Greater autonomy does not enable the blocked native lane.",
    ],
    "instruction_scopes": [
        "project_instructions supplies applicable target AGENTS content by directory. It is not a selector for agent identity.",
        "Herdr is the orchestration layer; the executing CLI and provider/model are separate identities. The controller binds the runtime, session and permissions; pane names and configured model labels are not execution evidence.",
        "Use the supplied Mission, run, instance, stage, candidate and result contract. Do not ask the user to repeat environment information already supplied. Runtime identity verification belongs to the controller and does not authorize this role to inspect or control other agents.",
        "Do not assume maintainer global AGENTS, skills or conversation are inherited. Do not copy global instructions into the candidate.",
        "Use only selected skill content below when its condition applies. References to other skills, agents, tools or project-specific procedures do not authorize or provision them.",
        "Skill narrative, citations and notes must fit the supplied result protocol; Herdr stage output remains raw JSON, with citations inside summary.",
    ],
}


def build_bundle():
    entries = {}
    for name in sorted({name for selected in SELECTION.values() for name in selected}):
        path = SKILL_ROOT / name / "SKILL.md"
        if path.resolve(strict=True) != path or not path.is_file():
            raise ValueError("role skill must be a regular local file without aliases")
        raw = path.read_bytes()
        if not 0 < len(raw) <= 32 * 1024:
            raise ValueError("role skill exceeds size limit")
        entries[name] = {"path": f"orchestration/role-skills/{name}/SKILL.md",
                         "sha256": state.artifact_id(raw), "content": raw.decode("utf-8")}
    roles = {role: {"role": role, "purpose": PURPOSE[role],
                    "candidate_writer": role == "worker", "skills": selection}
             for role, selection in SELECTION.items()}
    return {"schema_version": 1, "common": COMMON, "roles": roles, "skills": entries}


def validate_bundle(bundle):
    if (not isinstance(bundle, dict) or set(bundle) != {"schema_version", "common", "roles", "skills"}
            or type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1
            or not isinstance(bundle["common"], dict) or not isinstance(bundle["roles"], dict)
            or set(bundle["roles"]) != set(SELECTION) or not isinstance(bundle["skills"], dict)):
        raise ValueError("unsupported role guidance bundle")
    if (set(bundle["common"]) != {"autonomy", "authority", "instruction_scopes"}
            or any(not isinstance(rows, list) or not rows
                   or any(not isinstance(row, str) or not row for row in rows)
                   for rows in bundle["common"].values())):
        raise ValueError("incomplete shared role guidance")
    for role, contract in bundle["roles"].items():
        if (not isinstance(contract, dict) or set(contract) != {"role", "purpose", "candidate_writer", "skills"}
                or contract["role"] != role or type(contract["candidate_writer"]) is not bool
                or contract["candidate_writer"] != (role == "worker")
                or not isinstance(contract["skills"], dict) or not contract["skills"]
                or not isinstance(contract["purpose"], str) or not contract["purpose"]):
            raise ValueError("invalid role contract")
        for name, condition in contract["skills"].items():
            if name not in bundle["skills"] or not isinstance(condition, str) or not condition:
                raise ValueError("missing selected role skill")
    for entry in bundle["skills"].values():
        if (not isinstance(entry, dict) or set(entry) != {"path", "sha256", "content"}
                or not isinstance(entry["content"], str)
                or not 0 < len(entry["content"].encode("utf-8")) <= 32 * 1024
                or state.artifact_id(entry["content"]) != entry["sha256"]):
            raise ValueError("role skill content digest mismatch")
    return bundle


def packet(runs, mid, current, role):
    if role not in SELECTION:
        raise ValueError("unknown guidance role")
    first = next((a for a in current["admissions"].values() if a["request_key"] == "herdr:plan"), None)
    if first:
        task = state.loads_strict(fleet_artifacts.get_bytes(runs, mid, first["task_sha256"]))
        prior = task.get("role_guidance")
        if prior is None:
            return None  # No silent upgrades to historical missions.
        pin = prior["bundle_artifact_id"]
        bundle = validate_bundle(state.loads_strict(fleet_artifacts.get_bytes(runs, mid, pin)))
        if prior != project(bundle, pin, "lead"):
            raise ValueError("role guidance differs from first task binding")
    else:
        bundle = validate_bundle(build_bundle())
        pin = fleet_artifacts.put_bytes(runs, mid, state.canonical_bytes(bundle))["artifact_id"]
    return project(bundle, pin, role)


def project(bundle, pin, role):
    contract = bundle["roles"][role]
    return {"schema_version": 1, "bundle_artifact_id": pin,
            "common": bundle["common"], "contract": contract,
            "skills": [{"name": name, "when": condition, **bundle["skills"][name]}
                       for name, condition in sorted(contract["skills"].items())]}
