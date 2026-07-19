from __future__ import annotations

import copy
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_mission  # noqa: E402
import fleet_json  # noqa: E402
import fleet_manifest  # noqa: E402
import fleet_mission_state as mission_state  # noqa: E402
import workflow_config  # noqa: E402
import fleet_admission  # noqa: E402


def legacy_v1_compiled(compiled: dict) -> dict:
    """Return a correctly sealed historical compiled-workflow v1 fixture."""

    legacy = copy.deepcopy(compiled)
    legacy["schema_version"] = 1
    legacy.pop("router_snapshot")
    legacy["workflow"]["audit"].pop("trust_scope")
    for field in (
        "identity_groups",
        "launch_digest",
        "assurance_mode",
        "assurance_launch_digest",
        "assurance_identity_groups",
        "assurance_lead",
        "assurance_instances",
    ):
        legacy["resolved"].pop(field)
    members = [legacy["resolved"]["lead"], *legacy["resolved"]["instances"]]
    for member in members:
        member.pop("runner")
    legacy["workflow_digest"] = fleet_json.sha256(legacy["workflow"])
    unsigned = {key: value for key, value in legacy.items() if key != "compiled_digest"}
    legacy["compiled_digest"] = fleet_json.sha256(unsigned)
    return legacy


def write_compiled(path: Path, compiled: dict) -> None:
    path.write_bytes(fleet_json.canonical_bytes(compiled) + b"\n")
    path.chmod(0o600)


def create_running_mission(
    tmp: Path, *, feature: str = "control", workflow_name: str = "implementation"
) -> tuple[Path, str, str]:
    runs = tmp / "runs"
    target = tmp / "target"
    target.mkdir(parents=True, exist_ok=True)
    compiled = workflow_config.compile_path(
        ROOT / "workflows" / f"{workflow_name}.yaml"
    )
    mission_id, _ = fleet_mission.create_mission(
        runs,
        compiled=compiled,
        feature=feature,
        objective="exercise Fleet Control",
        target_repo=target.resolve(),
        base_sha="a" * 40,
        idempotency_key=f"create:{feature}",
        runtime_options={"timeout_seconds": 1800},
    )
    mission_state.append_event(
        runs,
        mission_id,
        kind="fleet_boot_started",
        actor="CONTROL",
        idempotency_key="boot",
        payload={"feature": feature, "preset": compiled["resolved"]["preset"]},
    )
    mission_state.append_event(
        runs,
        mission_id,
        kind="mission_running",
        actor="CONTROL",
        idempotency_key="running",
        payload={"manifest": str(runs / f"fleet-{feature}.manifest")},
    )
    lead = fleet_admission.reserve_many(
        runs,
        mission_id,
        requests=[
            {
                "request_key": "mission-lead",
                "run_kind": "lead",
                "recipient_instance": "lead",
                "capability": "lead",
                "delegated_budget": 0,
                "writer": False,
                "effect_sha256": mission_state.sha256(
                    {
                        "kind": "fixture-lead",
                        "prompt_sha256": "b" * 64,
                        "provider": compiled["resolved"]["lead"]["provider"],
                        "model": compiled["resolved"]["lead"]["model"],
                    }
                ),
                "task_sha256": "b" * 64,
            }
        ],
        idempotency_key="admission:lead:reserve",
    )["admissions"][0]
    committed = fleet_admission.commit(
        runs,
        mission_id,
        admission_id=lead["admission_id"],
        request_digest=lead["request_digest"],
        effect_sha256=lead["effect_sha256"],
        recipient_instance=lead["recipient_instance"],
        writer=lead["writer"],
        run_id=lead["run_id"],
        idempotency_key="admission:lead:commit",
    )
    authorized = fleet_admission.authorize_launch(
        runs,
        mission_id,
        admission_id=lead["admission_id"],
        commit_event_sha256=committed["commit_event_sha256"],
        request_digest=lead["request_digest"],
        effect_sha256=lead["effect_sha256"],
        recipient_instance=lead["recipient_instance"],
        writer=lead["writer"],
        run_id=lead["run_id"],
        approval_event_sha256=None,
        idempotency_key="admission:lead:authorize",
    )
    fleet_admission.mark_started(
        runs,
        mission_id,
        admission_id=lead["admission_id"],
        authorization_event_sha256=authorized["authorization_event_sha256"],
        request_digest=lead["request_digest"],
        effect_sha256=lead["effect_sha256"],
        recipient_instance=lead["recipient_instance"],
        writer=lead["writer"],
        run_id=lead["run_id"],
        idempotency_key="admission:lead:start",
    )
    lead_run_id = str(lead["run_id"])
    mission_state.append_event(
        runs,
        mission_id,
        kind="lead_dispatched",
        actor="CONTROL",
        idempotency_key="lead",
        payload={"run_id": lead_run_id, "prompt_sha256": "b" * 64},
    )
    members = [compiled["resolved"]["lead"], *compiled["resolved"]["instances"]]
    binding = fleet_manifest.binding_for_preset(
        compiled, compiled["resolved"]["preset"]
    )
    identity_groups = compiled["resolved"]["identity_groups"]
    lines = [
        "manifest_contract_version=3",
        "tracking_protocol=control-v1",
        f"feature={feature}",
        f"mission_id={mission_id}",
        f"preset={compiled['resolved']['preset']}",
        f"mode={compiled['resolved']['mode']}",
        f"compiled_digest={binding['compiled_digest']}",
        f"router_digest={binding['router_digest']}",
        f"roster_digest={binding['roster_digest']}",
        f"launch_digest={binding['launch_digest']}",
        f"identity_group.count={len(identity_groups)}",
        f"target_repo={target.resolve()}",
        "workspace=workspace:1",
        "workspace_uuid=00000000-0000-0000-0000-000000000001",
    ]
    lines.extend(
        f"identity_group.{index}={','.join(group)}"
        for index, group in enumerate(identity_groups, 1)
    )
    for index, member in enumerate(members, 1):
        instance = member["instance_id"]
        lines.extend(
            [
                f"{instance}=surface:{index}",
                f"{instance}.uuid=00000000-0000-0000-0000-{index:012d}",
                f"{instance}.runner={member['runner']}",
                f"{instance}.role_type={member['role_type']}",
                f"{instance}.phase={member['phase']}",
                f"{instance}.authority={member['authority']}",
                f"{instance}.provider={member['provider']}",
                f"{instance}.model={member['model']}",
                f"{instance}.hook_source={member['hook_source']}",
            ]
        )
        if member.get("variant") is not None:
            lines.append(f"{instance}.variant={member['variant']}")
    manifest_path = runs / f"fleet-{feature}.manifest"
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    return runs, mission_id, lead_run_id
