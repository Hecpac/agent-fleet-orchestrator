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

manifest_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); exit }' "$manifest"
}

set_manifest_value() {
  local key="$1" value="$2" temporary
  temporary="$(mktemp "$runs_dir/.fleet-$feature.manifest.XXXXXX")"
  awk -F= -v key="$key" -v value="$value" '
    BEGIN { found = 0 }
    $1 == key { print key "=" value; found = 1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$manifest" > "$temporary"
  mv "$temporary" "$manifest"
}

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

# Writer worktrees must be reconciled before the fleet disappears: uncommitted
# work would be orphaned, so fail closed and keep the fleet alive.
target_repo="$(grep '^target_repo=' "$manifest" | cut -d= -f2- || true)"
worktree_entries=()
worktree_instances=()
worktree_paths=()
worktree_branches=()
worktree_bases=()
while IFS= read -r entry; do
  [[ -n "$entry" ]] && worktree_entries+=("$entry")
done < <(grep '\.worktree=' "$manifest" || true)
for entry in ${worktree_entries[@]+"${worktree_entries[@]}"}; do
  wt="${entry#*=}"
  instance="${entry%%.*}"
  branch="$(manifest_value "$instance.branch")"
  base_sha="$(manifest_value "$instance.base_sha")"
  if [[ -z "$target_repo" || -z "$branch" || -z "$base_sha" ]]; then
    echo "Refusing teardown: incomplete writer git metadata for '$instance'." >&2
    exit 75
  fi
  if ! git -C "$target_repo" show-ref --verify --quiet "refs/heads/$branch"; then
    echo "Refusing teardown: writer branch is missing for '$instance': $branch" >&2
    exit 75
  fi
  checked_out_branch="$(git -C "$wt" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  if [[ ! -d "$wt" || "$checked_out_branch" != "$branch" ]]; then
    echo "Refusing teardown: writer worktree is not attached to '$branch': $wt" >&2
    exit 75
  fi
  worktree_head="$(git -C "$wt" rev-parse --verify HEAD 2>/dev/null || true)"
  branch_head="$(git -C "$target_repo" rev-parse --verify "refs/heads/$branch" 2>/dev/null || true)"
  if [[ -z "$worktree_head" || "$worktree_head" != "$branch_head" ]]; then
    echo "Refusing teardown: writer HEAD does not match durable branch '$branch'." >&2
    exit 75
  fi
  if [[ -d "$wt" && -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]]; then
    echo "Refusing teardown: worktree for '$instance' has uncommitted changes: $wt" >&2
    echo "Commit/branch the work in the target repo or discard it, then retry." >&2
    exit 75
  fi
  worktree_instances+=("$instance")
  worktree_paths+=("$wt")
  worktree_branches+=("$branch")
  worktree_bases+=("$base_sha")
done

cmux close-workspace --workspace "$ws_ref" >/dev/null
for _ in 1 2 3 4 5 6 7 8 9 10; do
  set +e
  python3 "$identity" exists "$manifest" >/dev/null 2>&1
  identity_rc=$?
  set -e
  if (( identity_rc == 1 )); then
    for ((i=0; i<${#worktree_paths[@]}; i++)); do
      instance="${worktree_instances[$i]}"
      wt="${worktree_paths[$i]}"
      branch="${worktree_branches[$i]}"
      base_sha="${worktree_bases[$i]}"
      checked_out_branch="$(git -C "$wt" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
      worktree_head="$(git -C "$wt" rev-parse --verify HEAD 2>/dev/null || true)"
      final_sha="$(git -C "$target_repo" rev-parse --verify "refs/heads/$branch" 2>/dev/null || true)"
      if [[ ! -d "$wt" || "$checked_out_branch" != "$branch" || \
            -z "$worktree_head" || "$worktree_head" != "$final_sha" ]]; then
        echo "Refusing cleanup after workspace shutdown: writer worktree left durable branch '$branch'." >&2
        echo "Preserved manifest and worktree for manual recovery: $wt" >&2
        exit 75
      fi
      if [[ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]]; then
        echo "Refusing cleanup after workspace shutdown: writer worktree became dirty: $wt" >&2
        echo "Preserved manifest and worktree for manual recovery." >&2
        exit 75
      fi
      set_manifest_value "$instance.final_sha" "$final_sha"
      if [[ -d "$wt" ]] && ! git -C "$target_repo" worktree remove "$wt" >/dev/null 2>&1; then
        echo "Refusing successful teardown: could not remove worktree $wt." >&2
        echo "Preserved manifest and writer branch for manual recovery." >&2
        exit 75
      fi
      if [[ "$final_sha" == "$base_sha" ]] && \
          ! git -C "$target_repo" update-ref -d "refs/heads/$branch" "$base_sha" >/dev/null 2>&1; then
        echo "Refusing successful teardown: unchanged writer branch moved; preserved $branch" >&2
        exit 75
      fi
    done
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
  if (( identity_rc > 1 )); then
    probe_failed=1
  fi
  sleep 0.1
done

if [[ "${probe_failed:-0}" == "1" ]]; then
  echo "Could not confirm cmux shutdown because identity probes failed; manifest preserved." >&2
else
  echo "cmux reported success but $ws_ref is still present; manifest preserved." >&2
fi
exit 1
