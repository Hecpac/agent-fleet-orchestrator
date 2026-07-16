#!/usr/bin/env bash
set -uo pipefail

# Fixed, controller-owned bridge for the only Codex hook events Mission Control
# needs. Never execute hook command text copied from a user's general Codex
# configuration.

event="${1:-}"
case "$event" in
  SessionStart)
    hook_args=(hooks codex session-start)
    ;;
  UserPromptSubmit)
    hook_args=(hooks codex prompt-submit)
    ;;
  Stop)
    hook_args=(hooks codex stop)
    ;;
  *)
    cat >/dev/null 2>&1 || true
    echo '{}'
    exit 0
    ;;
esac

surface_id="${CMUX_SURFACE_ID:-}"
socket_path="${CMUX_SOCKET_PATH:-}"
cmux_cli="${CMUX_BUNDLED_CLI_PATH:-}"
if [[ -z "$cmux_cli" || ! -x "$cmux_cli" ]]; then
  cmux_cli="$(command -v cmux 2>/dev/null || true)"
fi
if [[ -z "$surface_id" || "${CMUX_CODEX_HOOKS_DISABLED:-}" == "1" \
  || -z "$cmux_cli" ]]; then
  cat >/dev/null 2>&1 || true
  echo '{}'
  exit 0
fi

payload="$(mktemp "${TMPDIR:-/tmp}/fleet-codex-hook.XXXXXX")" || {
  cat >/dev/null 2>&1 || true
  echo '{}'
  exit 0
}
chmod 600 "$payload"
trap 'rm -f -- "$payload"' EXIT HUP INT TERM
cat > "$payload" || true

socket_args=()
if [[ -n "$socket_path" ]]; then
  socket_args=(--socket "$socket_path")
fi
if ! CMUX_SURFACE_ID="$surface_id" CMUX_SOCKET_PATH="$socket_path" \
  "$cmux_cli" "${socket_args[@]}" "${hook_args[@]}" < "$payload"; then
  echo '{}'
fi
