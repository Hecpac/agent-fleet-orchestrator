#!/usr/bin/env python3
"""Run one local Ollama-backed worker with a fixed status contract."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import re


STATUS_CONTRACT = """Return your final answer in this format.
Replace <STATUS> with exactly one of: DONE, BLOCKED, FAILED.

STATUS: <STATUS>
SUMMARY:
EVIDENCE:
RISKS:
NEXT_ACTION:

Evidence rules:
- Only cite files, commands, outputs, or facts explicitly included in the task.
- If no evidence was provided, write "EVIDENCE: Not provided".
- Do not infer project structure from common conventions.
"""


def call_ollama(host: str, model: str, prompt: str, temperature: float, num_predict: int) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
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
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise SystemExit(f"Unable to reach Ollama at {host}: {exc}") from exc

    return body.get("response", "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one local model worker.")
    parser.add_argument("--role", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--num-predict", type=int, default=768)
    args = parser.parse_args()

    worker_prompt = "\n\n".join(
        [
            f"Role type: {args.role}",
            args.instruction,
            STATUS_CONTRACT,
            "Task:",
            args.prompt,
        ]
    )
    output = call_ollama(args.host, args.model, worker_prompt, args.temperature, args.num_predict)
    cleaned = output.strip()
    sys.stdout.write(cleaned + "\n")
    statuses = re.findall(r"(?m)^STATUS: (DONE|BLOCKED|FAILED)\s*$", cleaned)
    required_headers = ("SUMMARY:", "EVIDENCE:", "RISKS:", "NEXT_ACTION:")
    if len(statuses) != 1 or any(cleaned.count(header) != 1 for header in required_headers):
        print("Worker contract error: malformed or ambiguous status contract", file=sys.stderr)
        return 1
    return {"DONE": 0, "BLOCKED": 3, "FAILED": 1}[statuses[0]]


if __name__ == "__main__":
    raise SystemExit(main())
