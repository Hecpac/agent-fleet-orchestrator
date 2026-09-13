"""Task-inline skills: suppress host selection without weakening transcript checks.

The CLI preview is local (no model request). Only catalog names are retained;
Mission skill bytes and role scope come from the existing CAS guidance bundle.
"""
from __future__ import annotations

import re

import fleet_json
import fleet_herdr_role_guidance as guidance

POLICY = "task-inline-skills-v1"
PROBE = "FLEET_LOCAL_CONTEXT_PROBE"


def catalog(raw):
    messages = fleet_json.loads(raw)
    if not isinstance(messages, list) or not messages:
        raise ValueError("invalid Codex context preview")
    if (any(not isinstance(m, dict) or m.get("type") != "message"
            or m.get("role") not in {"developer", "user"} or not isinstance(m.get("content"), list)
            or not m["content"] for m in messages)
            or not any(m["role"] == "developer" for m in messages)
            or messages[-1]["role"] != "user"
            or messages[-1]["content"] != [{"type": "input_text", "text": PROBE}]):
        raise ValueError("unsupported Codex context preview shape")
    names = set()
    for message in messages:
        for block in message["content"]:
            if (not isinstance(block, dict) or block.get("type") != "input_text"
                    or not isinstance(block.get("text"), str)):
                raise ValueError("unsupported Codex context preview content")
            text = block.get("text", "")
            if "<skills_instructions>" not in text:
                continue
            rows = [line for line in text.splitlines() if line.startswith("- ") and "(file: " in line]
            for row in rows:
                match = re.fullmatch(r"- ([A-Za-z0-9_.:-]+): .*\(file: .*/SKILL\.md\)", row)
                if not match:
                    raise ValueError("unsupported Codex skill catalog entry")
                names.add(match[1])
            if not rows and "### Available skills" not in text:
                raise ValueError("unsupported Codex skill catalog")
    if len(names) > 512:
        raise ValueError("Codex skill catalog exceeds bound")
    return sorted(names)


def validate(value):
    if (not isinstance(value, dict) or set(value) != {"policy", "disabled_skills"}
            or value["policy"] != POLICY or not isinstance(value["disabled_skills"], list)
            or any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", n)
                   for n in value["disabled_skills"])
            or value["disabled_skills"] != sorted(set(value["disabled_skills"]))
            or len(value["disabled_skills"]) > 512):
        raise ValueError("invalid frozen skill selection contract")
    return value


def flags(value):
    validate(value)
    disabled = ",".join('{name=' + fleet_json.canonical_bytes(n).decode() + ',enabled=false}'
                        for n in value["disabled_skills"])
    return ["-c", "skills.config=[" + disabled + "]", "-c", "features.skill_search=false"]


def verify_task(raw, role, read):
    task = fleet_json.loads(raw)
    packet = task.get("role_guidance")
    if not isinstance(packet, dict):
        raise ValueError("task-inline skills require frozen role guidance")
    pin = packet.get("bundle_artifact_id")
    bundle = guidance.validate_bundle(fleet_json.loads(read(pin)))
    if (task.get("instance_id") != role or packet != guidance.project(bundle, pin, role)):
        raise ValueError("task skill content or role scope differs from frozen bundle")
    return task
