from __future__ import annotations

import json
from pathlib import Path
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_mission
import fleet_mission_state as mission_state
import workflow_config


def create_running_mission(tmp: Path, *, feature: str = "control") -> tuple[Path, str, str]:
    runs = tmp / "runs"
    target = tmp / "target"
    target.mkdir(exist_ok=True)
    compiled = workflow_config.compile_path(ROOT / "workflows" / "implementation.yaml")
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
        payload={"feature": feature, "preset": "dan"},
    )
    mission_state.append_event(
        runs,
        mission_id,
        kind="mission_running",
        actor="CONTROL",
        idempotency_key="running",
        payload={"manifest": str(runs / f"fleet-{feature}.manifest")},
    )
    lead_run_id = str(uuid.uuid4())
    mission_state.append_event(
        runs,
        mission_id,
        kind="lead_dispatched",
        actor="CONTROL",
        idempotency_key="lead",
        payload={"run_id": lead_run_id, "prompt_sha256": "b" * 64},
    )
    members = [compiled["resolved"]["lead"], *compiled["resolved"]["instances"]]
    lines = [
        f"feature={feature}",
        f"mission_id={mission_id}",
        "preset=dan",
        "mode=autonomous",
        f"target_repo={target.resolve()}",
        "workspace=workspace:1",
        "workspace_uuid=00000000-0000-0000-0000-000000000001",
    ]
    for index, member in enumerate(members, 1):
        instance = member["instance_id"]
        lines.extend(
            [
                f"{instance}=surface:{index}",
                f"{instance}.uuid=00000000-0000-0000-0000-{index:012d}",
                f"{instance}.runner=interactive",
                f"{instance}.role_type={member['role_type']}",
                f"{instance}.provider={member['provider']}",
                f"{instance}.model={member['model']}",
                f"{instance}.hook_source={member['hook_source']}",
            ]
        )
    (runs / f"fleet-{feature}.manifest").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return runs, mission_id, lead_run_id
