#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_repo="${1:-}"
agents_source="$repo_root/orchestration/prompts/kimi_reviewer_agents.md"

if [[ -z "$target_repo" ]]; then
  echo "Usage: $0 <absolute-target-repo>" >&2
  exit 2
fi
if [[ "$target_repo" != /* || ! -d "$target_repo" || -L "$target_repo" ]]; then
  echo "Kimi reviewer target must be an absolute, non-symlink directory" >&2
  exit 2
fi
if [[ ! -f "$agents_source" || -L "$agents_source" ]]; then
  echo "Kimi read-only reviewer contract is unavailable" >&2
  exit 2
fi
kimi_executable="$(command -v kimi || true)"
if [[ -z "$kimi_executable" ]]; then
  echo "Kimi CLI is unavailable" >&2
  exit 2
fi

# kimi-code reads the role contract from AGENTS.md; KIMI_AGENTS_MD points at
# the repo-owned contract without writing into the target checkout.
cd "$target_repo"
KIMI_AGENTS_MD="$agents_source" exec "$kimi_executable" \
  --model kimi-code/k3 \
  --plan
