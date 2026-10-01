#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

fleet_python="${FLEET_PYTHON:-python3.12}"
export FLEET_PYTHON="$fleet_python"
export PYTHONDONTWRITEBYTECODE=1
# Tests never read this machine's certified Codex registry (empty disables it).
export FLEET_CODEX_ROOT=

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
# Compile for syntax only: bytecode goes to a private prefix, never into the
# tree, so fixtures that copy directories (and the tests) see source files only.
bytecode_prefix="$(mktemp -d "${TMPDIR:-/tmp}/fleet-ci-bytecode.XXXXXX")"
trap 'rm -rf "$bytecode_prefix"' EXIT
PYTHONPYCACHEPREFIX="$bytecode_prefix" "$fleet_python" -B -m compileall -q scripts tests

for script in scripts/*.sh; do
  [[ -f "$script" ]] || continue
  bash -n "$script"
done

# Mini harness tests freeze the real Mini runtime sources. Provide them from the
# hash lock unless the caller selected an installation with FLEET_MINI_DIST.
if [[ -z "${FLEET_MINI_DIST:-}" ]]; then
  lock_digest="$("$fleet_python" -B -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest()[:16])' requirements/mini-2.4.6.txt)"
  export FLEET_MINI_DIST="${FLEET_CI_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/agent-fleet-ci}/mini-2.4.6-$lock_digest/dist"
fi
./scripts/install-mini-deps.sh "$FLEET_MINI_DIST"

"$fleet_python" -B -m unittest discover -s tests -p 'test_*.py' -q
git diff --check
