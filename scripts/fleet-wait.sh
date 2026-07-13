#!/usr/bin/env bash
set -euo pipefail

# Event-driven wait for fleet members. Every local or frontier instance requires
# an exact run_id; the event stream is a wake-up and the ledger is authoritative.
#
# Usage:
#   ./scripts/fleet-wait.sh <feature> <instance-id> [instance-id ...]
#     [--run instance-id=run-id ...] [--timeout <sec>] [--any] [--json]
#
# Signals used:
#   - frontier roles: UserPromptSubmit binds session to surface; completed Stop
#     wakes strict run_id sentinel verification, while a bound Claude SessionEnd
#     without Stop fails closed and retains its lease.
#   - local worker roles: exact terminal ledger event for the required run_id;
#     notifications and heartbeats only trigger reconciliation.
#
# Exit codes: 0 succeeded, 1 failed, 2 use/identity/protocol, 3 blocked,
# 4 abandoned, 5 indeterminate, 124 deadline.

feature="${1:-}"
shift || true

timeout_sec=1800
any_flag=()
json_flag=()
run_flags=()
roles=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout)
      [[ $# -ge 2 ]] || { echo "--timeout requires seconds" >&2; exit 2; }
      timeout_sec="$2"
      shift 2
      ;;
    --any) any_flag=(--any); shift ;;
    --json) json_flag=(--json); shift ;;
    --run)
      [[ $# -ge 2 ]] || { echo "--run requires instance=run_id" >&2; exit 2; }
      run_flags+=("--run=$2")
      shift 2
      ;;
    *) roles+=("$1"); shift ;;
  esac
done
if [[ ! "$timeout_sec" =~ ^[1-9][0-9]*$ ]]; then
  echo "--timeout must be a positive integer" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
manifest="$runs_dir/fleet-$feature.manifest"
export CMUX_QUIET=1

if [[ -z "$feature" || ${#roles[@]} -eq 0 ]]; then
  echo "Usage: $0 <feature> <instance-id> [instance-id ...] [--run instance=run_id] [--timeout <sec>] [--any] [--json]" >&2
  exit 2
fi
if [[ ! -f "$manifest" ]]; then
  echo "No manifest at $manifest" >&2
  exit 2
fi
python3 "$repo_root/scripts/fleet_identity.py" validate "$manifest" "${roles[@]}" >/dev/null || exit 2

ws_ref="$(grep '^workspace=' "$manifest" | cut -d= -f2)"
if ! TREE_BOTH="$(cmux tree --workspace "$ws_ref" --id-format both)"; then
  echo "Could not read cmux tree for fleet '$feature'." >&2
  exit 5
fi
export TREE_BOTH

# No GNU `timeout` on macOS; fleet_wait.py enforces the deadline itself.
exec python3 -u "$repo_root/scripts/fleet_wait.py" \
  "$feature" "$manifest" "$timeout_sec" \
  ${any_flag[@]+"${any_flag[@]}"} ${json_flag[@]+"${json_flag[@]}"} \
  ${run_flags[@]+"${run_flags[@]}"} "${roles[@]}"
