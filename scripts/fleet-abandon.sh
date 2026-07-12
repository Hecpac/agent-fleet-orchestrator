#!/usr/bin/env bash
set -euo pipefail

# Explicitly terminalize one exact frontier run and release its owned lease.

feature="${1:-}"
instance_id="${2:-}"
run_id="${3:-}"
reason="${4:-operator_abandoned}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
manifest="$runs_dir/fleet-$feature.manifest"

if [[ -z "$feature" || -z "$instance_id" || -z "$run_id" || ! -f "$manifest" ]]; then
  echo "Usage: $0 <feature> <instance-id> <run-id> [reason]" >&2
  exit 2
fi

runner="$(awk -F= -v key="$instance_id.runner" '$1 == key {print $2; exit}' "$manifest")"
if [[ "$runner" != "interactive" ]]; then
  echo "Instance '$instance_id' is not frontier/interactive." >&2
  exit 2
fi

python3 "$repo_root/scripts/fleet_frontier.py" abandon "$runs_dir" \
  --feature "$feature" --instance "$instance_id" --run-id "$run_id" --reason "$reason"
