#!/usr/bin/env bash
set -euo pipefail

# Dispatch a task to a local worker pane and return its durable run_id.
# The worker runs the one-shot task in its own pane (visible output), then
# fires `cmux notify` with title "fleet-<feature>:<instance>" as a waiter
# wake-up. The terminal ledger event for the returned run_id is authoritative.
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
leases="$repo_root/scripts/fleet_leases.py"
export CMUX_QUIET=1

if [[ -z "$feature" || -z "$instance_id" || -z "$task" ]]; then
  echo "Usage: $0 <feature> <instance-id> \"<task>\"" >&2
  exit 2
fi
if [[ ! -f "$manifest" ]]; then
  echo "No manifest at $manifest" >&2
  exit 2
fi
python3 "$leases" reconcile "$runs_dir" >/dev/null || exit $?
python3 "$identity" validate "$manifest" "$instance_id" >/dev/null || exit 2
python3 "$repo_root/scripts/fleet_state.py" check "$manifest" "$instance_id" >/dev/null || exit $?

token_budget="$(python3 "$repo_root/scripts/router_config.py" limits-field local_token_budget_per_feature 2>/dev/null || true)"
if [[ -n "$token_budget" ]]; then
  python3 "$repo_root/scripts/fleet_budget.py" check \
    "$runs_dir/fleet-$feature.ledger.jsonl" "$token_budget" || exit $?
fi

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
workspace_uuid="$(manifest_value workspace_uuid)"
surface_uuid="$(manifest_value "$instance_id.uuid")"
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

max_parallel="$(python3 "$repo_root/scripts/router_config.py" limits-field max_parallel_local_workers)"
role_limit="$(python3 "$repo_root/scripts/router_config.py" role-field "$role_type" concurrency)"
set +e
lease_json="$(python3 "$leases" acquire "$runs_dir" \
  --run-id "$run_id" --feature "$feature" --instance "$instance_id" \
  --role "$role_type" --phase "$phase" --resource-class "$resource_class" \
  --task-sha256 "$task_sha256" --workspace-uuid "$workspace_uuid" \
  --surface-uuid "$surface_uuid" --max-local "$max_parallel" --role-limit "$role_limit")"
lease_rc=$?
set -e
if (( lease_rc != 0 )); then
  exit "$lease_rc"
fi
instance_lock="$(jq -r '.instance_lock' <<< "$lease_json")"
heavy_lock="$(jq -r '.heavy_lock' <<< "$lease_json")"
local_slot="$(jq -r '.local_slot' <<< "$lease_json")"
role_slot="$(jq -r '.role_slot' <<< "$lease_json")"
lease_args=(--lease "$instance_lock" --lease "$local_slot" --lease "$role_slot")
[[ -n "$heavy_lock" ]] && lease_args+=(--lease "$heavy_lock")

lease_transferred=0
ledger_dispatched=0
cleanup_untransferred_lease() {
  if (( lease_transferred == 0 )); then
    terminal_durable=1
    if (( ledger_dispatched == 1 )); then
      if ! python3 "$repo_root/scripts/fleet_ledger.py" "$runs_dir/fleet-$feature.ledger.jsonl" \
        --run-id "$run_id" --feature "$feature" --instance "$instance_id" \
        --role "$role_type" --phase "$phase" --status abandoned \
        --task-sha256 "$task_sha256" --exit-code 4 --reason dispatch_not_transferred \
        >/dev/null 2>&1; then
        terminal_durable=0
        echo "ERROR: terminal ledger write failed; preserving leases for $run_id" >&2
      fi
    fi
    if (( terminal_durable == 1 )); then
      python3 "$leases" release "$runs_dir" --run-id "$run_id" \
        "${lease_args[@]}" >/dev/null 2>&1 || true
    fi
  fi
}
trap cleanup_untransferred_lease EXIT

python3 "$repo_root/scripts/fleet_ledger.py" "$runs_dir/fleet-$feature.ledger.jsonl" \
  --run-id "$run_id" --feature "$feature" --instance "$instance_id" \
  --role "$role_type" --phase "$phase" --status dispatched --task-sha256 "$task_sha256"
ledger_dispatched=1

runner_command="./scripts/run-local-task.sh $feature $instance_id $role_type $phase $resource_class $run_id $task_file $task_sha256 $local_slot $role_slot"

if ! cmux send --surface "$surface" --workspace "$ws_ref" \
  "$runner_command; rc=\$?; cmux notify --title 'fleet-$feature:$instance_id' --body \"run_id=$run_id exit=\$rc\"; printf 'run_id=$run_id exit=%s\\n' \"\$rc\"" >/dev/null; then
  exit 1
fi
lease_transferred=1
if ! cmux send-key --surface "$surface" --workspace "$ws_ref" enter >/dev/null; then
  lease_transferred=0
  exit 1
fi
cmux read-screen --surface "$surface" --workspace "$ws_ref" --lines 5 >/dev/null || true

echo "dispatched run_id=$run_id to $instance_id/$role_type ($surface). Wait with: ./scripts/fleet-wait.sh $feature $instance_id --run $instance_id=$run_id"
