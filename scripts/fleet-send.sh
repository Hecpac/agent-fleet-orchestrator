#!/usr/bin/env bash
set -euo pipefail

feature="${1:-}"
instance_id="${2:-}"
task="${3:-}"
output_mode="${4:-}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
manifest="$runs_dir/fleet-$feature.manifest"
frontier="$repo_root/scripts/fleet_frontier.py"
export CMUX_QUIET=1

if [[ -z "$feature" || -z "$instance_id" || -z "$task" || ! -f "$manifest" \
  || ( -n "$output_mode" && "$output_mode" != "--json" ) ]]; then
  echo "Usage: $0 <feature> <instance-id> \"<task>\" [--json]" >&2
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
role="$(manifest_value "$instance_id.role_type")"
phase="$(manifest_value "$instance_id.phase")"
workspace_uuid="$(manifest_value workspace_uuid)"
surface_uuid="$(manifest_value "$instance_id.uuid")"
provider="$(manifest_value "$instance_id.provider")"
model="$(manifest_value "$instance_id.model")"
hook_source="$(manifest_value "$instance_id.hook_source")"
variant="$(manifest_value "$instance_id.variant")"

prepared="$(python3 "$frontier" prepare "$runs_dir" \
  --feature "$feature" --instance "$instance_id" --role "$role" --phase "$phase" \
  --task "$task" --workspace-uuid "$workspace_uuid" --surface-uuid "$surface_uuid" \
  --provider "$provider" --model "$model" --hook-source "$hook_source" \
  --variant "$variant")"
run_id="$(jq -r '.run_id' <<< "$prepared")"
prompt="$(jq -r '.prompt' <<< "$prepared")"
send_attempted=0
entered=0
cleanup_untransferred() {
  if (( entered == 0 )); then
    if (( send_attempted == 1 )); then
      python3 "$frontier" mark-indeterminate "$runs_dir" --feature "$feature" \
        --instance "$instance_id" --run-id "$run_id" \
        --reason frontier_send_transfer_unconfirmed >/dev/null 2>&1 || true
      echo "frontier transfer indeterminate run_id=$run_id; lease retained for explicit abandon" >&2
    else
      python3 "$frontier" abandon "$runs_dir" --feature "$feature" \
        --instance "$instance_id" --run-id "$run_id" \
        --reason frontier_send_not_attempted >/dev/null 2>&1 || true
    fi
  fi
}
trap cleanup_untransferred EXIT

send_attempted=1
cmux send --surface "$surface" --workspace "$workspace" "$prompt" >/dev/null
[[ "${FLEET_SEND_KEY_DELAY:-0.2}" == "0" ]] || sleep "${FLEET_SEND_KEY_DELAY:-0.2}"
cmux send-key --surface "$surface" --workspace "$workspace" enter >/dev/null
entered=1
if [[ "$output_mode" == "--json" ]]; then
  cmux read-screen --surface "$surface" --workspace "$workspace" --lines 8 >/dev/null || true
  jq -n --arg run_id "$run_id" --arg feature "$feature" \
    --arg instance "$instance_id" --arg role "$role" --arg surface "$surface" \
    '{run_id:$run_id,feature:$feature,instance:$instance,role:$role,surface:$surface,runner:"interactive"}'
else
  cmux read-screen --surface "$surface" --workspace "$workspace" --lines 8 || true
  echo "sent run_id=$run_id to $instance_id/$role ($surface). Wait with: ./scripts/fleet-wait.sh $feature $instance_id --run $instance_id=$run_id"
fi
