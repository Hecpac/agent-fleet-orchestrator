#!/usr/bin/env bash
set -euo pipefail

role_type="${1:-}"
task="${2:-}"
shift 2 || true
provider=""
model=""
variant=""
num_predict=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --provider)
      [[ $# -ge 2 ]] || { echo "--provider requires a value" >&2; exit 2; }
      provider="$2"
      shift 2
      ;;
    --model)
      [[ $# -ge 2 ]] || { echo "--model requires a value" >&2; exit 2; }
      model="$2"
      shift 2
      ;;
    --variant)
      [[ $# -ge 2 ]] || { echo "--variant requires a value" >&2; exit 2; }
      variant="$2"
      shift 2
      ;;
    --num-predict)
      [[ $# -ge 2 ]] || { echo "--num-predict requires a value" >&2; exit 2; }
      num_predict="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$role_type" || -z "$task" ]]; then
  echo "Usage: $0 <local-role-type> <task>" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
router="$repo_root/scripts/router_config.py"

runner="$(python3 "$router" role-field "$role_type" runner)" || exit 2
enabled="$(python3 "$router" role-field "$role_type" enabled)" || exit 2
if [[ "$runner" != "local" ]]; then
  echo "Role type '$role_type' is $runner, not a local worker." >&2
  exit 2
fi
if [[ "$enabled" != "true" ]]; then
  echo "Role type '$role_type' is disabled in orchestration/router.yaml." >&2
  exit 2
fi

[[ -n "$provider" ]] || provider="$(python3 "$router" role-field "$role_type" provider)" || exit 2
[[ -n "$model" ]] || model="$(python3 "$router" role-field "$role_type" model)" || exit 2
instruction="$(python3 "$router" role-field "$role_type" instructions)" || exit 2
# The router may declare a per-role output budget; the role was already
# validated above, so a missing optional field is the only expected failure.
[[ -n "$num_predict" ]] || \
  num_predict="$(python3 "$router" role-field "$role_type" num_predict 2>/dev/null || true)"

usage_args=()
if [[ -n "${FLEET_USAGE_FILE:-}" ]]; then
  usage_args=(--usage-file "$FLEET_USAGE_FILE")
fi
predict_args=()
if [[ -n "$num_predict" ]]; then
  predict_args=(--num-predict "$num_predict")
fi

python3 "$repo_root/orchestration/agents/local_worker.py" \
  --role "$role_type" \
  --provider "$provider" \
  --model "$model" \
  --variant "$variant" \
  --instruction "$instruction" \
  --prompt "$task" \
  ${predict_args[@]+"${predict_args[@]}"} \
  ${usage_args[@]+"${usage_args[@]}"}
