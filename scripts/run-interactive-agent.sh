#!/usr/bin/env bash
set -euo pipefail

# Launch an interactive agent with an explicit environment allowlist.
# Secrets are inherited only when the role declares them in requires_env.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
controller_home="${HOME:?HOME must be set by CONTROL}"
controller_user="${USER:-fleet_controller}"
controller_user_sha256="$(printf '%s' "$controller_user" | shasum -a 256 | awk '{print $1}')"
execution_profile="${FLEET_EXECUTION_PROFILE:-native}"

case "$execution_profile" in
  native|sandboxed|regulated) ;;
  *)
    echo "Unsupported FLEET_EXECUTION_PROFILE: $execution_profile" >&2
    exit 2
    ;;
esac

role_type="${1:-}"
authority="${2:-}"
required_csv="${3:-}"
shift 3 || true

if [[ -z "$role_type" || -z "$authority" || $# -eq 0 ]]; then
  echo "Usage: $0 <role-type> <authority> <required-env-csv|-> <command> [args...]" >&2
  exit 2
fi

isolated_home="$(mktemp -d /tmp/fleet_home.XXXXXX)"
chmod 700 "$isolated_home"
umask 077
process_home="$isolated_home"
process_user="fleet_worker"
process_logname="fleet_worker"
effective_model=""
command_args=("$@")
for ((index=0; index<${#command_args[@]}; index++)); do
  if [[ "${command_args[$index]}" == "--model" && $((index + 1)) -lt ${#command_args[@]} ]]; then
    effective_model="${command_args[$((index + 1))]}"
    break
  fi
done
cleanup() {
  rm -rf -- "$isolated_home"
}
trap cleanup EXIT HUP INT TERM

keep=()
for name in PATH SHELL TERM COLORTERM LANG LC_ALL LC_CTYPE TMPDIR \
  CLAUDE_CODE_NO_FLICKER HOMEBREW_PREFIX HOMEBREW_CELLAR HOMEBREW_REPOSITORY \
  FLEET_RUNS_DIR FLEET_MISSION_ID FLEET_CONTROL_SOCKET; do
  if [[ -n "${!name:-}" ]]; then
    keep+=("$name=${!name}")
  fi
done

keep+=(
  "FLEET_HOME=$isolated_home"
  "FLEET_EXECUTION_PROFILE=$execution_profile"
  "FLEET_HUMAN_UID=sha256:$controller_user_sha256"
  "CMUX_HOOK_DIR=${CMUX_HOOK_DIR:-$controller_home/.cmuxterm}"
  "CMUX_EVENTS_LOG=${CMUX_EVENTS_LOG:-$controller_home/.cmuxterm/events.jsonl}"
  "GIT_AUTHOR_NAME=FleetMaker"
  "GIT_AUTHOR_EMAIL=maker@fleet.local"
  "GIT_COMMITTER_NAME=FleetMaker"
  "GIT_COMMITTER_EMAIL=maker@fleet.local"
)
[[ -z "$effective_model" ]] || keep+=("LLM_MODEL=$effective_model")

# Strict profiles narrow process-owned temporary/cache state without editing
# the router's declared tool_access. Provider sandboxes continue to enforce the
# role-specific read/write authority selected by the compiled roster.
if [[ "$execution_profile" != "native" ]]; then
  mkdir -p "$isolated_home/tmp" "$isolated_home/cache" "$isolated_home/runtime"
  chmod 700 "$isolated_home/tmp" "$isolated_home/cache" "$isolated_home/runtime"
  keep+=(
    "TMPDIR=$isolated_home/tmp"
    "XDG_CACHE_HOME=$isolated_home/cache"
    "XDG_RUNTIME_DIR=$isolated_home/runtime"
    "FLEET_FILESYSTEM_PERIMETER=isolated-runtime"
    "FLEET_NETWORK_PERIMETER=provider-managed"
  )
fi
if [[ "$execution_profile" == "regulated" ]]; then
  [[ "${FLEET_MISSION_ID:-}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] || {
    echo "regulated execution requires a canonical FLEET_MISSION_ID" >&2
    exit 2
  }
  [[ "${FLEET_CONTROL_SOCKET:-}" == /* && -S "${FLEET_CONTROL_SOCKET:-}" ]] || {
    echo "regulated execution requires an active absolute FLEET_CONTROL_SOCKET" >&2
    exit 2
  }
  keep+=(
    "FLEET_MISSION_ID=$FLEET_MISSION_ID"
    "FLEET_EFFECT_POLICY=control-only"
  )
fi

# Provider configuration is explicit: HOME never grants implicit access to
# CONTROL's dotfiles. Claude receives a minimal hardened configuration and an
# tool environment; Claude's CLI bootstrap is handled explicitly below. Codex
# and OpenCode retain only the roots their existing hooks/databases require.
case "$role_type" in
  claude|claude_reviewer)
    claude_config="$isolated_home/.claude"
    mkdir -p "$claude_config"
    chmod 700 "$claude_config"
    command -v jq >/dev/null 2>&1 || {
      echo "jq is required to provision isolated Claude settings" >&2
      exit 2
    }
    jq --arg home "$isolated_home" \
      --arg controller_home "$controller_home" \
      --arg repo_root "$repo_root" '
      walk(
        if type == "string" then
          (split("__FLEET_CONTROLLER_HOME__") | join($controller_home)
          | split("__FLEET_REPO_ROOT__") | join($repo_root))
        else . end
      ) |
      .env = ((.env // {}) + {
        HOME: $home,
        USER: "fleet_worker",
        LOGNAME: "fleet_worker",
        GIT_AUTHOR_NAME: "FleetMaker",
        GIT_AUTHOR_EMAIL: "maker@fleet.local",
        GIT_COMMITTER_NAME: "FleetMaker",
        GIT_COMMITTER_EMAIL: "maker@fleet.local"
      }) |
      .permissions.deny += [
        ("Edit(/" + $home + "/.claude/**)"),
        ("Write(/" + $home + "/.claude/**)")
      ] |
      .sandbox.filesystem.denyWrite += [($home + "/.claude")]
    ' "$repo_root/orchestration/claude-fleet-settings.json" > "$claude_config/settings.json"
    chmod 600 "$claude_config/settings.json"
    jq -n '{mcpServers: {}}' > "$claude_config/mcp.json"
    chmod 600 "$claude_config/mcp.json"
    # Claude OAuth is stored in the macOS Keychain under the controller USER
    # and HOME. Keep those values only for CLI bootstrap; the additional
    # settings above replace them for the session and all tool subprocesses.
    process_home="$controller_home"
    process_user="$controller_user"
    process_logname="${LOGNAME:-$controller_user}"
    if [[ "$(basename "$1")" == "claude" ]]; then
      set -- "$1" --setting-sources "" --settings "$claude_config/settings.json" \
        --strict-mcp-config --mcp-config "$claude_config/mcp.json" "${@:2}"
    fi
    ;;
  codex|codex_candidate)
    controller_codex_home="${CODEX_HOME:-$controller_home/.codex}"
    if [[ -n "${FLEET_MISSION_ID:-}" && -n "${FLEET_CONTROL_SOCKET:-}" \
      && "$authority" != "write" && "$authority" != "control" ]]; then
      # A controller config may still declare legacy sandbox_mode, which takes
      # precedence over permission profiles. Use an ephemeral Codex home for
      # mission specialists and copy authentication plus the existing trusted
      # hook contract, but not the controller's general config, so the
      # fleet_control least-privilege profile selected by fleet-up is actually
      # enforceable and CMUX can still bind UserPromptSubmit/Stop evidence.
      [[ -f "$controller_codex_home/auth.json" ]] || {
        echo "Mission-bound Codex specialist requires $controller_codex_home/auth.json" >&2
        exit 2
      }
      specialist_codex_home="$isolated_home/.codex"
      mkdir -p "$specialist_codex_home"
      chmod 700 "$specialist_codex_home"
      cp "$controller_codex_home/auth.json" "$specialist_codex_home/auth.json"
      chmod 600 "$specialist_codex_home/auth.json"
      hook_bridge="$(printf '%q' "$repo_root/scripts/cmux-codex-hook.sh")"
      jq -n --arg bridge "$hook_bridge" '
        def binding($event): [{
          hooks: [{
            type: "command",
            command: ("/bin/bash " + $bridge + " " + $event)
          }]
        }];
        {hooks: {
          SessionStart: binding("SessionStart"),
          UserPromptSubmit: binding("UserPromptSubmit"),
          Stop: binding("Stop")
        }}
      ' > "$specialist_codex_home/hooks.json"
      chmod 600 "$specialist_codex_home/hooks.json"
      printf '[features]\nhooks = true\n' > "$specialist_codex_home/config.toml"
      chmod 600 "$specialist_codex_home/config.toml"
      keep+=("CODEX_HOME=$specialist_codex_home")
    else
      keep+=("CODEX_HOME=$controller_codex_home")
    fi
    ;;
  glm|minimax|minimax_checker)
    xdg_config="$isolated_home/xdg/config"
    xdg_data="$isolated_home/xdg/data"
    xdg_state="$isolated_home/xdg/state"
    mkdir -p "$xdg_config" "$xdg_data" "$xdg_state"
    for mapping in \
      "$controller_home/.config/opencode:$xdg_config" \
      "$controller_home/.local/share/opencode:$xdg_data" \
      "$controller_home/.local/state/opencode:$xdg_state"; do
      source_dir="${mapping%%:*}"
      destination_root="${mapping#*:}"
      if [[ -d "$source_dir" ]]; then
        if [[ -n "$(find "$source_dir" -type l -print -quit)" ]]; then
          echo "OpenCode provider state must not contain symlinks: $source_dir" >&2
          exit 2
        fi
        cp -R "$source_dir" "$destination_root/"
      fi
    done
    keep+=(
      "XDG_CONFIG_HOME=$xdg_config"
      "XDG_DATA_HOME=$xdg_data"
      "XDG_STATE_HOME=$xdg_state"
    )
    ;;
  *)
    [[ -n "${CODEX_HOME:-}" ]] && keep+=("CODEX_HOME=$CODEX_HOME")
    ;;
esac

keep+=(
  "HOME=$process_home"
  "USER=$process_user"
  "LOGNAME=$process_logname"
)

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

if [[ -n "$required_csv" && "$required_csv" != "-" ]]; then
  old_ifs="$IFS"
  IFS=','
  for name in $required_csv; do
    case "$name" in
      SSH_AUTH_SOCK|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|AWS_PROFILE|AWS_SHARED_CREDENTIALS_FILE|AWS_CONFIG_FILE|ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|OPENAI_API_KEY|GITHUB_TOKEN|GH_TOKEN)
        echo "Forbidden credential variable for isolated role $role_type: $name" >&2
        exit 2
        ;;
    esac
    if [[ -z "${!name:-}" ]]; then
      echo "Missing required environment variable for $role_type: $name" >&2
      exit 2
    fi
    keep+=("$name=${!name}")
  done
  IFS="$old_ifs"
fi

set +e
/usr/bin/env -i "${keep[@]}" "$@"
rc=$?
set -e
exit "$rc"
