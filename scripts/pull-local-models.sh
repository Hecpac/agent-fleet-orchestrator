#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
models=()
while IFS= read -r model; do
  [[ -n "$model" ]] && models+=("$model")
done < <(python3 "$repo_root/scripts/router_config.py" local-models)

if [[ ${#models[@]} -eq 0 ]]; then
  echo "No enabled local models in orchestration/router.yaml." >&2
  exit 2
fi

for model in "${models[@]}"; do
  echo "Pulling ${model}"
  ollama pull "$model"
done
