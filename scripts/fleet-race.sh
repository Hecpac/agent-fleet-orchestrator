#!/usr/bin/env bash
set -euo pipefail

# Agent race: give the same task to N agents in parallel; the first completion
# becomes a candidate. Other agents keep running unless --cancel-losers is
# explicitly requested. A candidate is never treated as verified truth.
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
    --timeout) timeout_sec="$2"; shift 2 ;;
    --keep-losers) keep_losers=1; shift ;;
    --cancel-losers) keep_losers=0; shift ;;
    *) role_specs+=("$1"); shift ;;
  esac
done
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

# Arm the event listener BEFORE dispatching so a fast finisher can't slip
# past the subscription window.
result_file="$(mktemp)"
"$repo_root/scripts/fleet-wait.sh" "$feature" "${instances[@]}" --any --timeout "$timeout_sec" \
  > "$result_file" 2>/dev/null &
wait_pid=$!
sleep 1

echo "== dispatching task to all racers"
for instance in "${instances[@]}"; do
  surface="$(manifest_value "$instance")"
  if is_frontier "$instance"; then
    "$repo_root/scripts/fleet-send.sh" "$feature" "$instance" "$task" >/dev/null
  else
    "$repo_root/scripts/fleet-dispatch.sh" "$feature" "$instance" "$task" >/dev/null
  fi
done

echo "== racing (timeout ${timeout_sec}s)..."
wait "$wait_pid" || true
result="$(grep '=done' "$result_file" | head -1 || true)"
rm -f "$result_file"
candidate="${result%%=*}"

if [[ -z "$candidate" ]]; then
  echo "RACE TIMED OUT — no agent finished within ${timeout_sec}s" >&2
  cmux notify --title "race-$name: timeout" --body "no candidate in ${timeout_sec}s" >/dev/null
  exit 124
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
