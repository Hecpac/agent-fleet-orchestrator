#!/usr/bin/env python3
"""Run one local Ollama-backed worker with a fixed status contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request
import re


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import fleet_json  # noqa: E402
import fleet_providers  # noqa: E402
import fleet_safe_paths  # noqa: E402


STATUS_CONTRACT = """Return your final answer in this format.
Replace <STATUS> with exactly one of: DONE, BLOCKED, FAILED.

STATUS: <STATUS>
SUMMARY:
EVIDENCE:
RISKS:
NEXT_ACTION:

Evidence rules:
- Only cite files, commands, outputs, or facts explicitly included in the task.
- Under the single EVIDENCE: heading, write "Not provided" if no evidence was supplied.
- Do not infer project structure from common conventions.
"""


def call_ollama(
    host: str, model: str, prompt: str, temperature: float, num_predict: int
) -> dict:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "keep_alive": 0,
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as response:
            try:
                body = fleet_json.loads(response.read())
            except fleet_json.FleetJSONError as exc:
                raise SystemExit(f"Invalid Ollama JSON response: {exc}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Unable to reach Ollama at {host}: {exc}") from exc

    if type(body) is not dict:
        raise SystemExit("Invalid Ollama JSON response: root must be an object")

    return body


def _usage_bytes(body: dict | None = None) -> bytes:
    if body is None or body.get("done") is not True:
        return fleet_json.canonical_bytes(
            {"prompt_eval_count": None, "eval_count": None}
        ) + b"\n"
    prompt_count = body.get("prompt_eval_count")
    completion_count = body.get("eval_count")
    complete = all(
        type(value) is int and value >= 0 for value in (prompt_count, completion_count)
    )
    usage = {
        # Missing, partial, boolean, or negative counters are unknown.  Never
        # manufacture zero spend from an Ollama response that did not report
        # both sides of the total.
        "prompt_eval_count": prompt_count if complete else None,
        "eval_count": completion_count if complete else None,
    }
    return fleet_json.canonical_bytes(usage) + b"\n"


def initialize_usage_file(path: str) -> None:
    """Publish fail-closed usage before inference without clobbering a leaf."""

    usage_path = Path(path)
    with fleet_safe_paths.RootedFS(usage_path.parent) as rooted:
        rooted.atomic_write(
            usage_path.name,
            _usage_bytes(),
            directory_modes=(),
            file_mode=0o600,
            require_absent=True,
        )
        rooted.assert_root_binding()


def write_usage_file(path: str, body: dict) -> None:
    """Replace only the private preflight receipt with Ollama's final usage."""

    usage_path = Path(path)
    with fleet_safe_paths.RootedFS(usage_path.parent) as rooted:
        rooted.replace_regular(
            usage_path.name,
            _usage_bytes(body),
            directory_modes=(),
            file_mode=0o600,
        )
        rooted.assert_root_binding()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one local model worker.")
    parser.add_argument("--role", required=True)
    parser.add_argument("--provider", default="ollama")
    parser.add_argument("--model", required=True)
    parser.add_argument("--variant", default="")
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--num-predict", type=int, default=768)
    parser.add_argument("--usage-file", help="write prompt/eval token counts as JSON")
    args = parser.parse_args()

    identity = fleet_providers.identity(args.provider, args.model, args.variant or None)
    adapter = fleet_providers.adapter_for(identity)
    adapter.validate_configuration(identity, runner="local")
    submission = adapter.prepare_submission(
        identity, args.prompt, "local-worker", Path("/ollama-api")
    )

    worker_prompt = "\n\n".join(
        [
            f"Role type: {args.role}",
            args.instruction,
            STATUS_CONTRACT,
            "Task:",
            submission["payload"],
        ]
    )
    if args.usage_file:
        # A process death or malformed HTTP body must remain unknown, never zero.
        # Exclusive publication also refuses stale, linked, or attacker-selected
        # receipt leaves before any inference can incur spend.
        initialize_usage_file(args.usage_file)
    body = call_ollama(
        args.host, args.model, worker_prompt, args.temperature, args.num_predict
    )
    if args.usage_file:
        # Record spend even when the status contract check below fails.
        write_usage_file(args.usage_file, body)
    cleaned = body.get("response", "").strip()
    adapter.verify_identity(
        identity,
        fleet_providers.ProviderEvidence(
            cleaned or "empty-response",
            args.provider,
            str(body.get("model") or ""),
            args.variant or None,
        ),
    )
    sys.stdout.write(cleaned + "\n")
    statuses = re.findall(r"(?m)^STATUS: (DONE|BLOCKED|FAILED)\s*$", cleaned)
    required_headers = ("SUMMARY:", "EVIDENCE:", "RISKS:", "NEXT_ACTION:")
    if len(statuses) != 1 or any(
        cleaned.count(header) != 1 for header in required_headers
    ):
        print(
            "Worker contract error: malformed or ambiguous status contract",
            file=sys.stderr,
        )
        return 1
    return {"DONE": 0, "BLOCKED": 3, "FAILED": 1}[statuses[0]]


if __name__ == "__main__":
    raise SystemExit(main())
