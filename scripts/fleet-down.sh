#!/usr/bin/env bash
set -euo pipefail

# Close a fleet workspace created by fleet-up.sh and remove its manifest.
#
# Usage:
#   ./scripts/fleet-down.sh <feature>

feature="${1:-}"
if [[ -z "$feature" ]]; then
  echo "Usage: $0 <feature>" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
manifest="$runs_dir/fleet-$feature.manifest"
identity="$repo_root/scripts/fleet_identity.py"
export CMUX_QUIET=1

if [[ ! -f "$manifest" ]]; then
  echo "No manifest at $manifest — nothing to close." >&2
  exit 1
fi

python3 "$identity" validate "$manifest" >/dev/null || {
  echo "Refusing teardown: manifest identity does not match cmux tree." >&2
  exit 2
}

ws_ref="$(grep '^workspace=' "$manifest" | cut -d= -f2)"
shopt -s nullglob
active_locks=("$runs_dir/locks/$feature."*.lock)
shopt -u nullglob
if (( ${#active_locks[@]} > 0 )); then
  echo "Refusing teardown: fleet '$feature' has active dispatch leases." >&2
  exit 75
fi
cmux close-workspace --workspace "$ws_ref" >/dev/null
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if ! python3 "$identity" exists "$manifest" >/dev/null 2>&1; then
    archive="$runs_dir/archive/$feature-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$archive"
    chmod 700 "$runs_dir/archive" "$archive"
    mv "$manifest" "$archive/manifest"
    state_file="${manifest%.manifest}.state.json"
    [[ -f "$state_file" ]] && mv "$state_file" "$archive/state.json"
    ledger_file="${manifest%.manifest}.ledger.jsonl"
    [[ -f "$ledger_file" ]] && mv "$ledger_file" "$archive/ledger.jsonl"
    echo "closed $ws_ref (fleet-$feature)"
    exit 0
  fi
  sleep 0.1
done

echo "cmux reported success but $ws_ref is still present; manifest preserved." >&2
exit 1
