#!/usr/bin/env bash
set -euo pipefail

# Dispatch a task to a local worker pane and emit a completion event.
# The worker runs the one-shot task in its own pane (visible output), then
# fires `cmux notify` with title "fleet-<feature>:<instance>" — the signal
# fleet-wait.sh listens for. Pair with fleet-wait.sh to avoid polling.
#
# Usage:
#   ./scripts/fleet-dispatch.sh <feature> <instance-id> "<task>"

feature="${1:-}"
instance_id="${2:-}"
task="${3:-}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
manifest="$runs_dir/fleet-$feature.manifest"
identity="$repo_root/scripts/fleet_identity.py"
export CMUX_QUIET=1

if [[ -z "$feature" || -z "$instance_id" || -z "$task" ]]; then
  echo "Usage: $0 <feature> <instance-id> \"<task>\"" >&2
  exit 2
fi
if [[ ! -f "$manifest" ]]; then
  echo "No manifest at $manifest" >&2
  exit 2
fi
python3 "$identity" validate "$manifest" "$instance_id" >/dev/null || exit 2
python3 "$repo_root/scripts/fleet_state.py" check "$manifest" "$instance_id" >/dev/null || exit $?

ws_ref="$(grep '^workspace=' "$manifest" | cut -d= -f2)"
manifest_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); exit }' "$manifest"
}

surface="$(manifest_value "$instance_id")"
if [[ -z "$surface" ]]; then
  echo "Instance '$instance_id' not in manifest $manifest" >&2
  exit 2
fi
role_type="$(manifest_value "$instance_id.role_type")"
runner="$(manifest_value "$instance_id.runner")"
phase="$(manifest_value "$instance_id.phase")"
resource_class="$(manifest_value "$instance_id.resource_class")"
if [[ -z "$role_type" || "$runner" != "local" ]]; then
  echo "Instance '$instance_id' is not a configured local worker." >&2
  exit 2
fi

run_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
task_dir="$runs_dir/tasks/$feature"
lock_dir="$runs_dir/locks"
mkdir -p "$task_dir" "$lock_dir"
chmod 700 "$task_dir" "$lock_dir"
task_file="$task_dir/$run_id.txt"
printf '%s' "$task" > "$task_file"
chmod 600 "$task_file"
task_sha256="$(shasum -a 256 "$task_file" | awk '{print $1}')"

instance_lock="$lock_dir/$feature.$instance_id.lock"
if ! mkdir "$instance_lock" 2>/dev/null; then
  echo "Instance '$instance_id' is busy; dispatch rejected." >&2
  exit 75
fi
printf '%s\n' "$run_id" > "$instance_lock/owner"
heavy_lock="$lock_dir/local-heavy.lock"
if [[ "$resource_class" == "local_heavy" ]] && ! mkdir "$heavy_lock" 2>/dev/null; then
  rm -rf "$instance_lock"
  echo "A heavy local worker already holds the global lease." >&2
  exit 75
fi
if [[ "$resource_class" == "local_heavy" ]]; then
  printf '%s\n' "$run_id" > "$heavy_lock/owner"
fi

max_parallel="$(python3 "$repo_root/scripts/router_config.py" limits-field max_parallel_local_workers)"
local_slot=""
for ((slot=1; slot<=max_parallel; slot++)); do
  candidate="$lock_dir/local-slot-$slot.lock"
  if mkdir "$candidate" 2>/dev/null; then
    local_slot="$candidate"
    printf '%s\n' "$run_id" > "$local_slot/owner"
    break
  fi
done
if [[ -z "$local_slot" ]]; then
  rm -rf "$instance_lock"
  [[ "$resource_class" == "local_heavy" ]] && rm -rf "$heavy_lock"
  echo "All local-worker slots are busy." >&2
  exit 75
fi

role_limit="$(python3 "$repo_root/scripts/router_config.py" role-field "$role_type" concurrency)"
role_slot=""
for ((slot=1; slot<=role_limit; slot++)); do
  candidate="$lock_dir/role-$role_type-$slot.lock"
  if mkdir "$candidate" 2>/dev/null; then
    role_slot="$candidate"
    printf '%s\n' "$run_id" > "$role_slot/owner"
    break
  fi
done
if [[ -z "$role_slot" ]]; then
  rm -rf "$instance_lock" "$local_slot"
  [[ "$resource_class" == "local_heavy" ]] && rm -rf "$heavy_lock"
  echo "Role '$role_type' reached concurrency limit $role_limit." >&2
  exit 75
fi
lease_transferred=0
cleanup_untransferred_lease() {
  if (( lease_transferred == 0 )); then
    rm -rf "$instance_lock"
    [[ "$resource_class" == "local_heavy" ]] && rm -rf "$heavy_lock"
    rm -rf "$local_slot" "$role_slot"
  fi
}
trap cleanup_untransferred_lease EXIT

python3 "$repo_root/scripts/fleet_ledger.py" "$runs_dir/fleet-$feature.ledger.jsonl" \
  --run-id "$run_id" --feature "$feature" --instance "$instance_id" \
  --role "$role_type" --phase "$phase" --status dispatched --task-sha256 "$task_sha256"

runner_command="./scripts/run-local-task.sh $feature $instance_id $role_type $phase $resource_class $run_id $task_file $task_sha256 $local_slot $role_slot"

if ! cmux send --surface "$surface" --workspace "$ws_ref" \
  "$runner_command; rc=\$?; cmux notify --title 'fleet-$feature:$instance_id' --body \"run_id=$run_id exit=\$rc\"; printf 'run_id=$run_id exit=%s\\n' \"\$rc\"" >/dev/null; then
  rm -rf "$instance_lock"
  [[ "$resource_class" == "local_heavy" ]] && rm -rf "$heavy_lock"
  rm -rf "$local_slot" "$role_slot"
  exit 1
fi
cmux send-key --surface "$surface" --workspace "$ws_ref" enter >/dev/null
lease_transferred=1
cmux read-screen --surface "$surface" --workspace "$ws_ref" --lines 5 >/dev/null

echo "dispatched run_id=$run_id to $instance_id/$role_type ($surface). Wait with: ./scripts/fleet-wait.sh $feature $instance_id"
