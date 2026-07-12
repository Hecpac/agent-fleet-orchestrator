#!/usr/bin/env bash
set -euo pipefail

feature="${1:-}"
instance_id="${2:-}"
task="${3:-}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
manifest="$runs_dir/fleet-$feature.manifest"

if [[ -z "$feature" || -z "$instance_id" || -z "$task" || ! -f "$manifest" ]]; then
  echo "Usage: $0 <feature> <instance-id> \"<task>\"" >&2
  exit 2
fi

python3 "$repo_root/scripts/fleet_identity.py" validate "$manifest" "$instance_id" >/dev/null
python3 "$repo_root/scripts/fleet_state.py" check "$manifest" "$instance_id" >/dev/null

manifest_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); exit }' "$manifest"
}

runner="$(manifest_value "$instance_id.runner")"
if [[ "$runner" != "interactive" ]]; then
  echo "Instance '$instance_id' is not interactive; use fleet-dispatch.sh." >&2
  exit 2
fi
surface="$(manifest_value "$instance_id")"
workspace="$(manifest_value workspace)"
cmux send --surface "$surface" --workspace "$workspace" "$task" >/dev/null
[[ "${FLEET_SEND_KEY_DELAY:-0.2}" == "0" ]] || sleep "${FLEET_SEND_KEY_DELAY:-0.2}"
cmux send-key --surface "$surface" --workspace "$workspace" enter >/dev/null
cmux read-screen --surface "$surface" --workspace "$workspace" --lines 8
