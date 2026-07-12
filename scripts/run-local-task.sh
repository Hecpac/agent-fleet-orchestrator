#!/usr/bin/env bash
set -euo pipefail

feature="${1:-}"
instance_id="${2:-}"
role_type="${3:-}"
phase="${4:-}"
resource_class="${5:-}"
run_id="${6:-}"
task_file="${7:-}"
task_sha256="${8:-}"
local_slot="${9:-}"
role_slot="${10:-}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
ledger="$runs_dir/fleet-$feature.ledger.jsonl"
result_dir="$runs_dir/results/$feature"
instance_lock="$runs_dir/locks/$feature.$instance_id.lock"
heavy_lock="$runs_dir/locks/local-heavy.lock"
result_file="$result_dir/$run_id.txt"

cleanup() {
  rm -rf "$instance_lock"
  if [[ "$resource_class" == "local_heavy" ]]; then
    rm -rf "$heavy_lock"
  fi
  rm -rf "$local_slot" "$role_slot"
}
trap cleanup EXIT INT TERM

lease_owned() {
  local lock="$1"
  [[ -d "$lock" && -f "$lock/owner" && "$(<"$lock/owner")" == "$run_id" ]]
}

if [[ ! -f "$task_file" ]] || ! lease_owned "$instance_lock" || \
  ! lease_owned "$local_slot" || ! lease_owned "$role_slot"; then
  echo "Task or dispatch lease missing for $run_id" >&2
  exit 2
fi
if [[ "$resource_class" == "local_heavy" ]] && ! lease_owned "$heavy_lock"; then
  echo "Heavy-worker lease missing for $run_id" >&2
  exit 2
fi

mkdir -p "$result_dir"
chmod 700 "$result_dir"
python3 "$repo_root/scripts/fleet_ledger.py" "$ledger" \
  --run-id "$run_id" --feature "$feature" --instance "$instance_id" \
  --role "$role_type" --phase "$phase" --status running --task-sha256 "$task_sha256"

set +e
"$repo_root/scripts/run-local-worker.sh" "$role_type" "$(<"$task_file")" | tee "$result_file"
pipeline_status=("${PIPESTATUS[@]}")
set -e
rc=${pipeline_status[0]}
if [[ ${pipeline_status[1]} -ne 0 && $rc -eq 0 ]]; then
  rc=1
fi
chmod 600 "$result_file"

status="failed"
[[ $rc -eq 0 ]] && status="succeeded"
[[ $rc -eq 3 ]] && status="blocked"
python3 "$repo_root/scripts/fleet_ledger.py" "$ledger" \
  --run-id "$run_id" --feature "$feature" --instance "$instance_id" \
  --role "$role_type" --phase "$phase" --status "$status" --task-sha256 "$task_sha256" \
  --exit-code "$rc" --result-file "$result_file"
exit "$rc"
