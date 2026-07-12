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

# Local dispatches come first so the waiter can bind each instance to its exact
# run_id. A local run that finishes before subscription remains observable in
# the durable ledger. Frontier turns are sent only after the event ACK waiter
# has been armed.
run_args=()
frontier_instances=()
echo "== dispatching task to local racers"
for instance in "${instances[@]}"; do
  if is_frontier "$instance"; then
    frontier_instances+=("$instance")
  else
    dispatch_output="$("$repo_root/scripts/fleet-dispatch.sh" "$feature" "$instance" "$task")"
    run_id="$(sed -n 's/.*dispatched run_id=\([^ ]*\).*/\1/p' <<< "$dispatch_output" | tail -1)"
    if [[ -z "$run_id" ]]; then
      echo "Could not recover run_id for local racer '$instance'." >&2
      exit 2
    fi
    run_args+=(--run "$instance=$run_id")
  fi
done

result_file="$(mktemp)"
ready_file="$result_file.ready"
wait_pid=""
cleanup_waiter() {
  if [[ -n "$wait_pid" ]] && kill -0 "$wait_pid" 2>/dev/null; then
    kill "$wait_pid" 2>/dev/null || true
    wait "$wait_pid" 2>/dev/null || true
  fi
  rm -f "$result_file" "$ready_file"
}
trap cleanup_waiter EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

FLEET_WAIT_READY_FILE="$ready_file" \
"$repo_root/scripts/fleet-wait.sh" "$feature" "${instances[@]}" \
  ${run_args[@]+"${run_args[@]}"} --any --json --timeout "$timeout_sec" \
  > "$result_file" &
wait_pid=$!

if [[ ${#frontier_instances[@]} -gt 0 ]]; then
  ready=0
  for _ in {1..200}; do
    if [[ -f "$ready_file" ]]; then
      ready=1
      break
    fi
    if ! kill -0 "$wait_pid" 2>/dev/null; then
      wait "$wait_pid" 2>/dev/null || true
      wait_pid=""
      echo "Race waiter exited before subscription ACK." >&2
      exit 5
    fi
    sleep 0.05
  done
  if (( ready == 0 )); then
    echo "Race waiter did not confirm subscription ACK." >&2
    exit 5
  fi
  echo "== dispatching task to frontier racers"
  for instance in "${frontier_instances[@]}"; do
    "$repo_root/scripts/fleet-send.sh" "$feature" "$instance" "$task" >/dev/null
  done
fi

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
  for instance in "${instances[@]}"; do
    [[ "$instance" == "$candidate" ]] && continue
    surface="$(manifest_value "$instance")"
    if is_frontier "$instance"; then
      cmux send-key --surface "$surface" --workspace "$ws_ref" escape >/dev/null || true
    else
      cmux send-key --surface "$surface" --workspace "$ws_ref" ctrl+c >/dev/null || true
    fi
    cmux read-screen --surface "$surface" --workspace "$ws_ref" --lines 5 >/dev/null || true
    echo "   interrupted: $instance"
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
