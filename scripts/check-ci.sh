#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

fleet_python="${FLEET_PYTHON:-python3.12}"
export FLEET_PYTHON="$fleet_python"
export PYTHONDONTWRITEBYTECODE=1

for dependency in bash git "$fleet_python" uv; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    echo "portable CI dependency is unavailable: $dependency" >&2
    exit 2
  fi
done

if ! "$fleet_python" -B -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "portable CI requires Python 3.12 or newer; select it with FLEET_PYTHON" >&2
  exit 2
fi

"$fleet_python" -B scripts/workflow_config.py validate workflows/*.yaml
"$fleet_python" -B -m compileall -q scripts tests

for script in scripts/*.sh; do
  [[ -f "$script" ]] || continue
  bash -n "$script"
done

"$fleet_python" -B -m unittest discover -s tests -p 'test_*.py' -q
git diff --check
