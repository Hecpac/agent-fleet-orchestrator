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

# Decision C transport: the composed prompt is durable on disk; dispatch a
# tiny single-line pointer (no newlines, no backslash sequences) so neither
# paste chunking nor cmux escape handling can split or truncate it, then
# require at least one observed UserPromptSubmit before trusting transfer.
prompt_path="$(jq -r '.prompt_path' <<< "$prepared")"
# The pointer carries the run's user-binding marker: provider evidence
# extraction locates the dispatched turn by finding FLEET_RESULT:<run>:<STATUS>
# in the user message.
pointer="FLEET_RUN $run_id: open the file $prompt_path and execute its entire content as your exact task for this turn, following its output schema and field names exactly as written. Its completion protocol requires one final line using FLEET_RESULT:$run_id:<STATUS> where STATUS is DONE, BLOCKED, or FAILED."
# OpenCode prompts are already one single-line FDP_PROMPT-encoded string (no
# raw newlines, so neither chunk boundaries nor escape handling can submit
# early), and MiniMax demonstrably loses schema fidelity through file
# indirection; keep the encoded inline payload there and use the pointer for
# codex/claude, where it is proven.
if [[ "$hook_source" == "opencode" ]]; then
  payload="$prompt"
else
  payload="$pointer"
fi
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
    --workspace-uuid "$workspace_uuid" --hook-source "$hook_source" \
    --since "$since" --timeout "${FLEET_CONFIRM_SUBMIT_TIMEOUT:-6}" >/dev/null 2>&1; then
    confirmed=1
    break
  fi
  cmux send-key --surface "$surface" --workspace "$workspace" enter >/dev/null
done
if (( confirmed == 0 )); then
  python3 "$frontier" confirm-submit "$runs_dir" \
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
