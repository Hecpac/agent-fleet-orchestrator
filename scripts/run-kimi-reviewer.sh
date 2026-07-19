#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_repo="${1:-}"
agent_file="$repo_root/.kimi/agents/fleet-reviewer/agent.yaml"

if [[ -z "$target_repo" ]]; then
  echo "Usage: $0 <absolute-target-repo>" >&2
  exit 2
fi
if [[ "$target_repo" != /* || ! -d "$target_repo" || -L "$target_repo" ]]; then
  echo "Kimi reviewer target must be an absolute, non-symlink directory" >&2
  exit 2
fi
if [[ ! -f "$agent_file" || -L "$agent_file" ]]; then
  echo "Kimi read-only fleet agent is unavailable" >&2
  exit 2
fi
kimi_executable="$(command -v kimi || true)"
if [[ -z "$kimi_executable" ]]; then
  echo "Kimi CLI is unavailable" >&2
  exit 2
fi

PYTHONPATH="$repo_root/scripts${PYTHONPATH:+:$PYTHONPATH}" exec "$kimi_executable" \
  --work-dir "$target_repo" \
  --model moonshot-ai/kimi-k3 \
  --thinking \
  --agent-file "$agent_file"
