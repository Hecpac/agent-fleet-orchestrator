#!/usr/bin/env bash
set -euo pipefail

# Launch an interactive agent with an explicit environment allowlist.
# Secrets are inherited only when the role declares them in requires_env.

role_type="${1:-}"
authority="${2:-}"
required_csv="${3:-}"
shift 3 || true

if [[ -z "$role_type" || -z "$authority" || $# -eq 0 ]]; then
  echo "Usage: $0 <role-type> <authority> <required-env-csv|-> <command> [args...]" >&2
  exit 2
fi

keep=()
for name in HOME PATH USER LOGNAME SHELL TERM COLORTERM LANG LC_ALL LC_CTYPE TMPDIR \
  CLAUDE_CODE_NO_FLICKER HOMEBREW_PREFIX HOMEBREW_CELLAR HOMEBREW_REPOSITORY \
  FLEET_RUNS_DIR; do
  if [[ -n "${!name:-}" ]]; then
    keep+=("$name=${!name}")
  fi
done

# Fleet Codex workers must use the default ~/.codex configuration where cmux
# installs its official hooks. Other interactive providers keep the caller's
# CODEX_HOME unchanged for backward compatibility.
case "$role_type" in
  codex|codex_candidate) ;;
  *) [[ -n "${CODEX_HOME:-}" ]] && keep+=("CODEX_HOME=$CODEX_HOME") ;;
esac

# Preserve only the observed cmux identity/hook variables. Future CMUX-prefixed
# values are not inherited automatically.
for name in CMUX_BUNDLE_ID CMUX_BUNDLED_CLI_PATH CMUX_CLAUDE_WRAPPER_SHIM \
  CMUX_CLAUDE_WRAPPER_SHIM_ROOT CMUX_KIRO_NOTIFICATION_LEVEL \
  CMUX_LOAD_GHOSTTY_ZSH_INTEGRATION CMUX_NO_GIT_WATCH CMUX_NO_PR_WATCH \
  CMUX_PANEL_ID CMUX_PORT CMUX_PORT_END CMUX_PORT_RANGE CMUX_SHELL_INTEGRATION \
  CMUX_SHELL_INTEGRATION_DIR CMUX_SOCKET CMUX_SOCKET_PATH \
  CMUX_SUPPRESS_SUBAGENT_NOTIFICATIONS CMUX_SURFACE_ID CMUX_TAB_ID CMUX_WORKSPACE_ID; do
  if [[ -n "${!name:-}" ]]; then
    keep+=("$name=${!name}")
  fi
done

if [[ "$authority" == "control" || "$authority" == "write" ]]; then
  [[ -n "${SSH_AUTH_SOCK:-}" ]] && keep+=("SSH_AUTH_SOCK=$SSH_AUTH_SOCK")
fi

if [[ -n "$required_csv" && "$required_csv" != "-" ]]; then
  old_ifs="$IFS"
  IFS=','
  for name in $required_csv; do
    if [[ -z "${!name:-}" ]]; then
      echo "Missing required environment variable for $role_type: $name" >&2
      exit 2
    fi
    keep+=("$name=${!name}")
  done
  IFS="$old_ifs"
fi

exec /usr/bin/env -i "${keep[@]}" "$@"
