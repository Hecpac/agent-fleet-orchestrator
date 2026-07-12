#!/usr/bin/env bash
set -euo pipefail

role_type="${1:-}"
task="${2:-}"

if [[ -z "$role_type" || -z "$task" ]]; then
  echo "Usage: $0 <local-role-type> <task>" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
router="$repo_root/scripts/router_config.py"

runner="$(python3 "$router" role-field "$role_type" runner)" || exit 2
enabled="$(python3 "$router" role-field "$role_type" enabled)" || exit 2
if [[ "$runner" != "local" ]]; then
  echo "Role type '$role_type' is $runner, not a local worker." >&2
  exit 2
fi
if [[ "$enabled" != "true" ]]; then
  echo "Role type '$role_type' is disabled in orchestration/router.yaml." >&2
  exit 2
fi

model="$(python3 "$router" role-field "$role_type" model)" || exit 2
instruction="$(python3 "$router" role-field "$role_type" instructions)" || exit 2

python3 "$repo_root/orchestration/agents/local_worker.py" \
  --role "$role_type" \
  --model "$model" \
  --instruction "$instruction" \
  --prompt "$task"
