#!/usr/bin/env bash
set -euo pipefail

# Agent race: give the same task to N agents in parallel; the first successful
# completion becomes a candidate. Failed/blocked/abandoned local runs are
# reported but do not stop the race while another candidate remains viable.
#
# Usage:
#   ./scripts/fleet-race.sh <name> "<task>" [instance=role ...] [--timeout <sec>] [--cancel-losers]
#
# Default racers: codex minimax. Any fleet-up role works; mixing frontier
# and local roles is fine. The first result must pass a separate verification
# gate before losers may be cancelled safely.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
router="$repo_root/scripts/router_config.py"

name="${1:-}"
task="${2:-}"
shift 2 || true

timeout_sec=900
keep_losers=1
role_specs=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout)
      [[ $# -ge 2 ]] || { echo "--timeout requires seconds" >&2; exit 2; }
      timeout_sec="$2"
      shift 2
      ;;
    --keep-losers) keep_losers=1; shift ;;
    --cancel-losers) keep_losers=0; shift ;;
    *) role_specs+=("$1"); shift ;;
  esac
done
if [[ ! "$timeout_sec" =~ ^[1-9][0-9]*$ ]]; then
  echo "--timeout must be a positive integer" >&2
  exit 2
fi
if [[ ${#role_specs[@]} -eq 0 ]]; then
  while IFS= read -r role_type; do
    [[ -n "$role_type" ]] && role_specs+=("$role_type")
  done < <(python3 "$router" defaults-field race_roles)
fi

if [[ -z "$name" || -z "$task" ]]; then
  echo "Usage: $0 <name> \"<task>\" [instance=role ...] [--timeout <sec>] [--cancel-losers]" >&2
  exit 2
fi

feature="race-$name"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
manifest="$runs_dir/fleet-$feature.manifest"
export CMUX_QUIET=1

instances=()
for spec in "${role_specs[@]}"; do
  if [[ "$spec" == *=* ]]; then
    instances+=("${spec%%=*}")
  else
    instances+=("$spec")
  fi
done

manifest_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); exit }' "$manifest"
}

is_frontier() {
  [[ "$(manifest_value "$1.runner")" == "interactive" ]]
}

echo "== booting race '$feature' with: ${role_specs[*]}"
FLEET_NO_LEAD=1 "$repo_root/scripts/fleet-up.sh" "$feature" "${role_specs[@]}"

ws_ref="$(grep '^workspace=' "$manifest" | cut -d= -f2)"
python3 "$repo_root/scripts/fleet_identity.py" validate "$manifest" "${instances[@]}" >/dev/null || exit 2
race_phases="$(awk -F= '$1 ~ /\.phase$/ && $2 != "" && $2 != "CONTROL" {print $2}' "$manifest" | sort -u)"
race_phase_count="$(printf '%s\n' "$race_phases" | grep -c . || true)"
if [[ "$race_phase_count" != "1" || -z "$race_phases" ]]; then
  echo "Race requires every candidate to share one non-empty phase." >&2
  exit 2
fi
first_phase="$race_phases"
python3 "$repo_root/scripts/fleet_state.py" advance "$manifest" "$first_phase" --evidence "race-candidate-search:$name" >/dev/null

# Every candidate is dispatched with a durable run_id and event baseline before
# the shared waiter starts. Fast frontier finishes are recovered by replay.
run_args=()
run_ids=()
result_file=""
wait_pid=""
dispatch_complete=0
cleanup_race() {
  if [[ -n "$wait_pid" ]] && kill -0 "$wait_pid" 2>/dev/null; then
    kill "$wait_pid" 2>/dev/null || true
    wait "$wait_pid" 2>/dev/null || true
  fi
  [[ -z "$result_file" ]] || rm -f "$result_file"
  if (( dispatch_complete == 0 && ${#run_ids[@]} > 0 )); then
    echo "Race dispatch stopped after starting these exact runs; leases remain fail-closed:" >&2
    for ((cleanup_index=0; cleanup_index<${#run_ids[@]}; cleanup_index++)); do
      [[ -n "${run_ids[$cleanup_index]:-}" ]] || continue
      cleanup_instance="${instances[$cleanup_index]}"
      if is_frontier "$cleanup_instance"; then
        echo "  after confirming quiescence: ./scripts/fleet-abandon.sh $feature $cleanup_instance ${run_ids[$cleanup_index]} dispatch_failed_after_partial_start" >&2
      else
        echo "  wait exact local run: ./scripts/fleet-wait.sh $feature $cleanup_instance --run $cleanup_instance=${run_ids[$cleanup_index]} --timeout $timeout_sec" >&2
      fi
    done
  fi
}
trap cleanup_race EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "== dispatching task to all racers"
for ((index=0; index<${#instances[@]}; index++)); do
  instance="${instances[$index]}"
  if is_frontier "$instance"; then
    dispatch_output="$("$repo_root/scripts/fleet-send.sh" "$feature" "$instance" "$task" --json)"
  else
    dispatch_output="$("$repo_root/scripts/fleet-dispatch.sh" "$feature" "$instance" "$task" --json)"
  fi
  run_id="$(jq -r '.run_id // empty' <<< "$dispatch_output")"
  if [[ -z "$run_id" ]]; then
    echo "Could not recover run_id for racer '$instance'." >&2
    exit 2
  fi
  run_ids[$index]="$run_id"
  run_args+=(--run "$instance=$run_id")
done
dispatch_complete=1

result_file="$(mktemp)"
"$repo_root/scripts/fleet-wait.sh" "$feature" "${instances[@]}" \
  ${run_args[@]+"${run_args[@]}"} --any --json --timeout "$timeout_sec" \
  > "$result_file" &
wait_pid=$!

echo "== racing (timeout ${timeout_sec}s)..."
set +e
wait "$wait_pid"
wait_rc=$?
set -e
wait_pid=""
candidate="$(jq -r 'select(.status == "succeeded") | .instance' "$result_file" | head -1)"

if [[ -z "$candidate" ]]; then
  if (( wait_rc == 124 )); then
    echo "RACE TIMED OUT — no successful agent within ${timeout_sec}s" >&2
    cmux notify --title "race-$name: timeout" --body "no successful candidate in ${timeout_sec}s" >/dev/null || true
    exit 124
  fi
  echo "RACE ENDED — every candidate terminated without success (exit $wait_rc)" >&2
  exit "$wait_rc"
fi

echo "== FIRST CANDIDATE (NOT VERIFIED): $candidate"

if (( keep_losers == 0 )); then
  for ((index=0; index<${#instances[@]}; index++)); do
    instance="${instances[$index]}"
    [[ "$instance" == "$candidate" ]] && continue
    surface="$(manifest_value "$instance")"
    if is_frontier "$instance"; then
      cmux send-key --surface "$surface" --workspace "$ws_ref" escape >/dev/null || true
      python3 "$repo_root/scripts/fleet_frontier.py" mark-indeterminate "$runs_dir" \
        --feature "$feature" --instance "$instance" --run-id "${run_ids[$index]}" \
        --reason race_loser_interruption_unconfirmed >/dev/null || true
      echo "   interruption requested: $instance (lease retained until explicit abandon)"
    else
      cmux send-key --surface "$surface" --workspace "$ws_ref" ctrl+c >/dev/null || true
      echo "   interrupted: $instance"
    fi
    cmux read-screen --surface "$surface" --workspace "$ws_ref" --lines 5 >/dev/null || true
  done
fi

candidate_surface="$(manifest_value "$candidate")"
cmux notify --title "race-$name: candidate $candidate" \
  --body "verify surface $candidate_surface before acceptance" >/dev/null

echo "== candidate screen ($candidate, $candidate_surface):"
cmux read-screen --surface "$candidate_surface" --workspace "$ws_ref" --scrollback --lines 60 \
  | grep -v "^\s*$" | tail -30

echo
echo "workspace: $ws_ref (teardown: just fleet-down $feature)"
