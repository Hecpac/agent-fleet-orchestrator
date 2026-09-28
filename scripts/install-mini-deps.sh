#!/usr/bin/env bash
# Install the hash-locked Mini 2.4.6 runtime sources.
#
# Mini executes inside the Linux sandbox, so the wheels are selected for the
# Linux platform of the host architecture even when the host is macOS. The
# pure-Python sources are identical on every platform; only the compiled
# extensions of pydantic_core and markupsafe depend on the selected platform.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lock="$repo_root/requirements/mini-2.4.6.txt"
fleet_python="${FLEET_PYTHON:-python3.12}"
target="${1:-${FLEET_MINI_DIST:-${XDG_DATA_HOME:-$HOME/.local/share}/fleet-harness/mini-2.4.6/dist}}"

if [[ -z "${FLEET_MINI_PLATFORM:-}" ]]; then
  case "$(uname -m)" in
    arm64 | aarch64) FLEET_MINI_PLATFORM=aarch64-unknown-linux-gnu ;;
    x86_64 | amd64) FLEET_MINI_PLATFORM=x86_64-unknown-linux-gnu ;;
    *)
      echo "unsupported Mini sandbox architecture: $(uname -m); set FLEET_MINI_PLATFORM" >&2
      exit 2
      ;;
  esac
fi

for dependency in uv "$fleet_python"; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    echo "Mini dependency installation requires: $dependency" >&2
    exit 2
  fi
done

if [[ -e "$target" ]]; then
  echo "Mini runtime sources already present: $target" >&2
  exit 0
fi

# Install beside the target and publish with one rename, so an interrupted
# installation never looks complete.
parent="$(dirname "$target")"
mkdir -p "$parent"
staging="$(mktemp -d "$parent/.mini-install.XXXXXX")"
trap 'rm -rf "$staging"' EXIT
uv pip install --quiet --python "$fleet_python" --python-platform "$FLEET_MINI_PLATFORM" \
  --target "$staging/dist" --require-hashes --no-deps -r "$lock"
mv "$staging/dist" "$target"
echo "Mini runtime sources installed: $target ($FLEET_MINI_PLATFORM)"
