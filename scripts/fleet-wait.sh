#!/usr/bin/env bash
set -euo pipefail

# Event-driven wait for fleet members. Blocks until every given role has
# finished its current turn, then prints "instance=done" lines and exits 0.
# No polling: subscribes to the cmux event stream.
#
# Usage:
#   ./scripts/fleet-wait.sh <feature> <instance-id> [instance-id ...] [--timeout <sec>]
#
# Signals used:
#   - frontier roles (lead/codex/gemini/minimax/glm): agent.hook.Stop events,
#     matched per-surface via the session_id -> surfaceId hook-session files.
#   - local worker roles: notification.requested with title
#     "fleet-<feature>:<instance>" (emitted by fleet-dispatch.sh).
#
# Exit codes: 0 all roles done, 124 timeout, 2 usage/manifest error.

feature="${1:-}"
shift || true

timeout_sec=1800
any_flag=()
roles=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout) timeout_sec="$2"; shift 2 ;;
    --any) any_flag=(--any); shift ;;
    *) roles+=("$1"); shift ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
manifest="$runs_dir/fleet-$feature.manifest"
export CMUX_QUIET=1

if [[ -z "$feature" || ${#roles[@]} -eq 0 ]]; then
  echo "Usage: $0 <feature> <instance-id> [instance-id ...] [--timeout <sec>]" >&2
  exit 2
fi
if [[ ! -f "$manifest" ]]; then
  echo "No manifest at $manifest" >&2
  exit 2
fi
python3 "$repo_root/scripts/fleet_identity.py" validate "$manifest" "${roles[@]}" >/dev/null || exit 2

ws_ref="$(grep '^workspace=' "$manifest" | cut -d= -f2)"
TREE_BOTH="$(cmux tree --workspace "$ws_ref" --id-format both)"
export TREE_BOTH

# No GNU `timeout` on macOS; fleet_wait.py enforces the deadline itself.
exec python3 -u "$repo_root/scripts/fleet_wait.py" \
  "$feature" "$manifest" "$timeout_sec" ${any_flag[@]+"${any_flag[@]}"} "${roles[@]}"
