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
leases="$repo_root/scripts/fleet_leases.py"
lease_args=(--lease "$instance_lock" --lease "$local_slot" --lease "$role_slot")
[[ "$resource_class" == "local_heavy" ]] && lease_args+=(--lease "$heavy_lock")

terminal_written=0
cleanup() {
  prior_rc=$?
  if (( terminal_written == 0 )); then
    if python3 "$repo_root/scripts/fleet_ledger.py" "$ledger" \
      --run-id "$run_id" --feature "$feature" --instance "$instance_id" \
      --role "$role_type" --phase "$phase" --status abandoned \
      --task-sha256 "$task_sha256" --exit-code 4 \
      --reason runner_exited_before_terminal >/dev/null 2>&1; then
      terminal_written=1
    else
      echo "ERROR: terminal ledger write failed; preserving leases for $run_id" >&2
    fi
  fi
  if (( terminal_written == 1 )); then
    python3 "$leases" release "$runs_dir" --run-id "$run_id" \
      "${lease_args[@]}" >/dev/null 2>&1 || \
      echo "WARNING: lease release skipped for non-owner run $run_id" >&2
  fi
  return "$prior_rc"
}
trap cleanup EXIT

if [[ ! -f "$task_file" ]] || ! python3 "$leases" validate "$runs_dir" \
  --run-id "$run_id" "${lease_args[@]}"; then
  echo "Task or dispatch lease missing for $run_id" >&2
  exit 2
fi
pgid="$(ps -o pgid= -p $$ | tr -d ' ')"
python3 "$leases" activate "$runs_dir" --run-id "$run_id" \
  --pid "$$" --pgid "$pgid" "${lease_args[@]}"

mkdir -p "$result_dir"
chmod 700 "$result_dir"
python3 "$repo_root/scripts/fleet_ledger.py" "$ledger" \
  --run-id "$run_id" --feature "$feature" --instance "$instance_id" \
  --role "$role_type" --phase "$phase" --status running --task-sha256 "$task_sha256"

usage_file="$result_dir/$run_id.usage.json"
export FLEET_USAGE_FILE="$usage_file"

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
ledger_rc="$rc"
if [[ $rc -eq 130 || $rc -eq 143 ]]; then
  status="abandoned"
  ledger_rc=4
fi
token_args=()
if [[ -f "$usage_file" ]]; then
  tokens="$(python3 -c 'import json, sys
data = json.load(open(sys.argv[1]))
print(int(data.get("prompt_eval_count", 0)), int(data.get("eval_count", 0)))' "$usage_file" 2>/dev/null || true)"
  if [[ -n "$tokens" ]]; then
    read -r prompt_tokens completion_tokens <<< "$tokens"
    token_args=(--prompt-tokens "$prompt_tokens" --completion-tokens "$completion_tokens")
  fi
fi
python3 "$repo_root/scripts/fleet_ledger.py" "$ledger" \
  --run-id "$run_id" --feature "$feature" --instance "$instance_id" \
  --role "$role_type" --phase "$phase" --status "$status" --task-sha256 "$task_sha256" \
  --exit-code "$ledger_rc" --result-file "$result_file" \
  ${token_args[@]+"${token_args[@]}"}
terminal_written=1
exit "$rc"
