#!/usr/bin/env bash
set -euo pipefail

feature="${1:-}"
instance_id="${2:-}"
task="${3:-}"
shift 3 || true
output_mode=""
run_id_override=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json)
      output_mode="--json"
      shift
      ;;
    --run-id)
      [[ $# -ge 2 ]] || { echo "--run-id requires a canonical UUID" >&2; exit 2; }
      run_id_override="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
manifest="$runs_dir/fleet-$feature.manifest"
frontier="$repo_root/scripts/fleet_frontier.py"
export CMUX_QUIET=1

if [[ -z "$feature" || -z "$instance_id" || -z "$task" || ! -f "$manifest" ]]; then
  echo "Usage: $0 <feature> <instance-id> \"<task>\" [--run-id <uuid>] [--json]" >&2
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

prepare_args=(prepare "$runs_dir" \
  --feature "$feature" --instance "$instance_id" --role "$role" --phase "$phase" \
  --task "$task" --workspace-uuid "$workspace_uuid" --surface-uuid "$surface_uuid" \
  --provider "$provider" --model "$model" --hook-source "$hook_source" \
  --variant "$variant")
[[ -z "$run_id_override" ]] || prepare_args+=(--run-id "$run_id_override")
prepared="$(python3 "$frontier" "${prepare_args[@]}")"
run_id="$(jq -r '.run_id' <<< "$prepared")"
prompt="$(jq -r '.prompt' <<< "$prepared")"
payload="$(jq -r '.submission_payload' <<< "$prepared")"
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

# The provider adapter chooses an identity-bound transport while the composed
# prompt remains durable on disk. CONTROL still performs the existing cmux
# side effect and requires a UserPromptSubmit before trusting transfer.
prompt_path="$(jq -r '.prompt_path' <<< "$prepared")"
[[ -n "$prompt_path" && -n "$prompt" && -n "$payload" ]] || exit 75
send_attempted=1
since="$(date -u +%Y-%m-%dT%H:%M:%S)"
cmux send --surface "$surface" --workspace "$workspace" "$payload" >/dev/null
[[ "${FLEET_SEND_KEY_DELAY:-0.2}" == "0" ]] || sleep "${FLEET_SEND_KEY_DELAY:-0.2}"
cmux send-key --surface "$surface" --workspace "$workspace" enter >/dev/null
# Some TUIs (codex) intermittently swallow the first Enter, leaving the
# payload queued in the composer. Re-press Enter only while zero submissions
# have been observed, so a slow-but-submitted turn is never double-entered.
confirmed=0
for _attempt in 1 2 3; do
  if python3 "$frontier" confirm-submit "$runs_dir" \
    --feature "$feature" --instance "$instance_id" --run-id "$run_id" \
    --workspace-uuid "$workspace_uuid" --hook-source "$hook_source" \
    --since "$since" --timeout "${FLEET_CONFIRM_SUBMIT_TIMEOUT:-6}" >/dev/null 2>&1; then
    confirmed=1
    break
  fi
  cmux send-key --surface "$surface" --workspace "$workspace" enter >/dev/null
done
if (( confirmed == 0 )); then
  python3 "$frontier" confirm-submit "$runs_dir" \
    --feature "$feature" --instance "$instance_id" --run-id "$run_id" \
    --workspace-uuid "$workspace_uuid" --hook-source "$hook_source" \
    --since "$since" --timeout 4 >/dev/null
fi
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
