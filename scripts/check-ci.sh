#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

for dependency in bash git python3 uv; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    echo "portable CI dependency is unavailable: $dependency" >&2
    exit 2
  fi
done

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "portable CI requires Python 3.12 or newer" >&2
  exit 2
fi

python3 scripts/workflow_config.py validate workflows/*.yaml
python3 -m compileall -q scripts tests

for script in scripts/*.sh; do
  [[ -f "$script" ]] || continue
  bash -n "$script"
done

python3 -m unittest discover -s tests -p 'test_*.py' -q
git diff --check
