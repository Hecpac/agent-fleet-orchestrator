#!/usr/bin/env bash
set -euo pipefail

# Close a fleet workspace created by fleet-up.sh and remove its manifest.
#
# Usage:
#   ./scripts/fleet-down.sh <feature>

feature="${1:-}"
recover_absent=0
if [[ "${2:-}" == "--recover-absent" ]]; then
  recover_absent=1
elif [[ -n "${2:-}" ]]; then
  echo "Unknown option: $2" >&2
  exit 2
fi
if [[ -z "$feature" ]]; then
  echo "Usage: $0 <feature> [--recover-absent]" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
manifest="$runs_dir/fleet-$feature.manifest"
identity="$repo_root/scripts/fleet_identity.py"
leases="$repo_root/scripts/fleet_leases.py"
dialogue_controller="$repo_root/scripts/fleet_dialogue_controller.py"
assurance_controller="$repo_root/scripts/fleet_assurance_controller.py"
audit_client="$repo_root/scripts/fleet_audit_client.py"
archive_client="$repo_root/scripts/fleet_archive.py"
control_client="$repo_root/scripts/fleet_control_service.py"
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

workspace_uuid="$(manifest_value workspace_uuid)"
close_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
close_started=0
cleanup_close() {
  prior_rc=$?
  trap - EXIT
  if (( close_started == 1 )); then
    if ! python3 "$leases" end-close "$runs_dir" --feature "$feature" \
      --close-id "$close_id" >/dev/null; then
      echo "Could not release teardown ownership for fleet '$feature'." >&2
      (( prior_rc == 0 )) && prior_rc=75
    fi
  fi
  exit "$prior_rc"
}
trap cleanup_close EXIT
python3 "$leases" begin-close "$runs_dir" --feature "$feature" \
  --close-id "$close_id" --workspace-uuid "$workspace_uuid" >/dev/null || exit $?
close_started=1

workspace_already_absent=0
if ! python3 "$identity" validate "$manifest" >/dev/null; then
  if (( recover_absent == 1 )); then
    set +e
    python3 "$identity" exists "$manifest" >/dev/null 2>&1
    identity_rc=$?
    set -e
    if (( identity_rc == 1 )); then
      workspace_already_absent=1
    else
      echo "Refusing recovery: workspace absence was not confirmed." >&2
      exit 2
    fi
  else
    echo "Refusing teardown: manifest identity does not match cmux tree." >&2
    echo "If the workspace was intentionally removed, retry with --recover-absent." >&2
    exit 2
  fi
fi

ws_ref="$(grep '^workspace=' "$manifest" | cut -d= -f2)"
preset="$(manifest_value preset)"
verification_receipt="${manifest%.manifest}.verification-receipt.json"
assurance_receipt="${manifest%.manifest}.assurance-receipt.json"
state_file="${manifest%.manifest}.state.json"
active_phase=""
if [[ -f "$state_file" ]]; then
  active_phase="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("active_phase", ""))' "$state_file")"
fi
assurance_required=0
if [[ "$preset" == "fleet_dialogue" && ( "$active_phase" == "CHALLENGE" || "$active_phase" == "VERIFY" ) ]]; then
  assurance_required=1
fi
if [[ "$preset" == "fleet_dialogue" ]]; then
  if (( workspace_already_absent == 1 )); then
    if [[ ! -s "$verification_receipt" ]]; then
      echo "Refusing FDP-2 recovery: live verification receipt is absent." >&2
      exit 75
    fi
  else
    python3 "$dialogue_controller" verify "$runs_dir" --feature "$feature" \
      --require-terminal --write-receipt "$verification_receipt" >/dev/null
  fi
fi
if (( assurance_required == 1 )); then
  if (( workspace_already_absent == 1 )); then
    if [[ ! -s "$assurance_receipt" ]]; then
      echo "Refusing FDP-3 recovery: live assurance receipt is absent." >&2
      exit 75
    fi
  else
    python3 "$assurance_controller" verify "$runs_dir" --feature "$feature" \
      --require-terminal --write-receipt "$assurance_receipt" >/dev/null
  fi
fi
mission_id="$(manifest_value mission_id)"
audit_required=0
if [[ "$(manifest_value mode)" == "assured" && -n "$mission_id" ]]; then
  audit_required=1
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

# Mission-bound fleets must freeze and verify their portable archive while the
# writer worktree and (for assured missions) AuditService are still available.
if [[ -n "$mission_id" && -d "$runs_dir/missions/$mission_id" ]]; then
  mission_status="$(PYTHONPATH="$repo_root/scripts" python3 -c '
import pathlib, sys
import fleet_mission
print(fleet_mission.load_state(pathlib.Path(sys.argv[1]), sys.argv[2])["status"])
' "$runs_dir" "$mission_id")"
  case "$mission_status" in
    succeeded|failed|blocked|abandoned|indeterminate) ;;
    *)
      echo "Refusing teardown: mission $mission_id is not terminal (status=$mission_status)." >&2
      exit 75
      ;;
  esac
  control_lifecycle="$runs_dir/missions/$mission_id/control/lifecycle.json"
  if [[ -f "$control_lifecycle" ]] && \
      [[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("stopped_at") or "")' "$control_lifecycle")" == "" ]]; then
    python3 "$control_client" --runs-dir "$runs_dir" --mission-id "$mission_id" stop >/dev/null
  fi
  mission_archive="$runs_dir/missions/$mission_id/archive"
  python3 "$archive_client" create --runs-dir "$runs_dir" --mission-id "$mission_id" \
    --manifest "$manifest" --output "$mission_archive" >/dev/null
  python3 "$archive_client" verify "$mission_archive" --repo "$target_repo" >/dev/null
fi
if (( audit_required == 1 )); then
  python3 "$audit_client" --runs-dir "$runs_dir" --mission-id "$mission_id" verify >/dev/null
fi

if (( workspace_already_absent == 0 )); then
  cmux close-workspace --workspace "$ws_ref" >/dev/null
fi
for _ in 1 2 3 4 5 6 7 8 9 10; do
  set +e
  python3 "$identity" exists "$manifest" >/dev/null 2>&1
  identity_rc=$?
  set -e
  if (( identity_rc == 1 )); then
    if (( assurance_required == 1 )); then
      python3 "$assurance_controller" verify "$runs_dir" --feature "$feature" \
        --cleanup-snapshots >/dev/null
    fi
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
    if (( audit_required == 1 )); then
      python3 "$audit_client" --runs-dir "$runs_dir" --mission-id "$mission_id" stop >/dev/null
    fi
    archive="$runs_dir/archive/$feature-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$archive"
    chmod 700 "$runs_dir/archive" "$archive"
    mv "$manifest" "$archive/manifest"
    [[ -f "$state_file" ]] && mv "$state_file" "$archive/state.json"
    ledger_file="${manifest%.manifest}.ledger.jsonl"
    [[ -f "$ledger_file" ]] && mv "$ledger_file" "$archive/ledger.jsonl"
    dialogue_ledger="${manifest%.manifest}.dialogue.jsonl"
    [[ -f "$dialogue_ledger" ]] && mv "$dialogue_ledger" "$archive/dialogue.jsonl"
    dialogue_control="${manifest%.manifest}.dialogue-control.jsonl"
    [[ -f "$dialogue_control" ]] && mv "$dialogue_control" "$archive/dialogue-control.jsonl"
    assurance_control="${manifest%.manifest}.assurance-control.jsonl"
    [[ -f "$assurance_control" ]] && mv "$assurance_control" "$archive/assurance-control.jsonl"
    [[ -f "$verification_receipt" ]] && mv "$verification_receipt" "$archive/verification-receipt.json"
    [[ -f "$assurance_receipt" ]] && mv "$assurance_receipt" "$archive/assurance-receipt.json"
    dialogue_store="$runs_dir/dialogue/$feature"
    [[ -d "$dialogue_store" ]] && mv "$dialogue_store" "$archive/dialogue"
    assurance_store="$runs_dir/assurance/$feature"
    [[ -d "$assurance_store" ]] && mv "$assurance_store" "$archive/assurance"
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
