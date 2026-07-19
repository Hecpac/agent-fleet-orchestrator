#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python3 scripts/workflow_config.py validate workflows/*.yaml
python3 -m compileall -q scripts tests

for script in scripts/*.sh; do
  [[ -f "$script" ]] || continue
  bash -n "$script"
done

python3 -m unittest discover -s tests -p 'test_*.py' -q
git diff --check
