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
opencode_state_root="/tmp/agent-fleet-orchestrator-opencode"
opencode_state_dir=""
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
  if [[ -n "$opencode_state_dir" ]]; then
    rm -rf -- "$opencode_state_dir"
    rmdir "$opencode_state_root" 2>/dev/null || true
  fi
}
trap cleanup EXIT HUP INT TERM

validate_isolated_provider_tree() {
  local root="$1"
  python3 - "$root" <<'PY'
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
if root.is_symlink():
    print(f"OpenCode provider state root must not be a symlink: {root}", file=sys.stderr)
    raise SystemExit(2)

try:
    resolved_root = root.resolve(strict=True)
except OSError as exc:
    print(f"OpenCode provider state is unreadable: {root}: {exc}", file=sys.stderr)
    raise SystemExit(2)

for current, directories, files in os.walk(root, followlinks=False):
    current_path = Path(current)
    for name in [*directories, *files]:
        candidate = current_path / name
        if not candidate.is_symlink():
            continue
        raw_target = os.readlink(candidate)
        if os.path.isabs(raw_target):
            print(
                f"OpenCode provider state contains an absolute symlink: {candidate}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        try:
            resolved_target = candidate.resolve(strict=True)
            resolved_target.relative_to(resolved_root)
        except (OSError, ValueError):
            print(
                f"OpenCode provider state symlink escapes its isolated root: {candidate}",
                file=sys.stderr,
            )
            raise SystemExit(2)
PY
}

codex_hook_override() {
  local event="$1" hook_bridge surface socket command_json
  hook_bridge="$(printf '%q' "$repo_root/scripts/cmux-codex-hook.sh")"
  surface="$(printf '%q' "${CMUX_SURFACE_ID:-}")"
  socket="$(printf '%q' "${CMUX_SOCKET_PATH:-}")"
  command_json="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' \
    "/usr/bin/env CMUX_SURFACE_ID=$surface CMUX_SOCKET_PATH=$socket CMUX_CODEX_HOOKS_DISABLED=0 /bin/bash $hook_bridge $event")"
  printf 'hooks.%s=[{hooks=[{type="command",command=%s}]}]' "$event" "$command_json"
}

provision_fleet_codex_home() {
  local controller_codex_home fleet_codex_home project_key
  controller_codex_home="${CODEX_HOME:-$controller_home/.codex}"
  if [[ ! -f "$controller_codex_home/auth.json" || -L "$controller_codex_home/auth.json" ]]; then
    echo "Fleet Codex role requires a regular $controller_codex_home/auth.json" >&2
    exit 2
  fi

  fleet_codex_home="$isolated_home/.codex"
  mkdir -p "$fleet_codex_home"
  chmod 700 "$fleet_codex_home"
  cp "$controller_codex_home/auth.json" "$fleet_codex_home/auth.json"
  chmod 600 "$fleet_codex_home/auth.json"
  project_key="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$repo_root")"
  printf '[features]\nhooks = true\n\n[projects.%s]\ntrust_level = "untrusted"\n' \
    "$project_key" > "$fleet_codex_home/config.toml"
  chmod 600 "$fleet_codex_home/config.toml"
  keep+=(
    "CODEX_HOME=$fleet_codex_home"
    # cmux injects its own Codex hooks through CLI `-c` flags. Disable those
    # in the shim, then install the repo-owned overrides below with the
    # surface identity embedded in each command.
    "CMUX_CODEX_HOOKS_DISABLED=1"
  )
}

prepare_opencode_data_home() {
  local surface_id="${CMUX_SURFACE_ID:-}"
  if [[ -z "$surface_id" ]]; then
    xdg_data="$isolated_home/xdg/data"
    return
  fi
  if [[ ! "$surface_id" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]; then
    echo "OpenCode surface identity is not a canonical UUID: $surface_id" >&2
    exit 2
  fi
  surface_id="$(printf '%s' "$surface_id" | tr '[:lower:]' '[:upper:]')"
  if [[ -L "$opencode_state_root" ]]; then
    echo "OpenCode evidence state root must not be a symlink: $opencode_state_root" >&2
    exit 2
  fi
  mkdir -p "$opencode_state_root"
  chmod 700 "$opencode_state_root"
  opencode_state_dir="$opencode_state_root/$surface_id"
  if [[ -e "$opencode_state_dir" || -L "$opencode_state_dir" ]]; then
    echo "OpenCode evidence state already exists for surface $surface_id" >&2
    exit 2
  fi
  mkdir -m 700 "$opencode_state_dir"
  xdg_data="$opencode_state_dir/data"
}

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
    # Claude's interactive TUI consults ~/.claude.json after applying the
    # session HOME override. A fresh isolated HOME otherwise re-enters
    # onboarding and reports "Not logged in" even when OAuth is available in
    # the controller user's macOS Keychain. Seed only the onboarding marker and
    # the opaque account identifiers needed to locate that Keychain entry;
    # never copy email, profile, history, settings, or credential material.
    controller_claude_state="$controller_home/.claude.json"
    if [[ -f "$controller_claude_state" && ! -L "$controller_claude_state" ]]; then
      jq '
        .oauthAccount as $account |
        {hasCompletedOnboarding: true} +
        (if (($account | type) == "object"
             and ($account.accountUuid | type) == "string"
             and ($account.organizationUuid | type) == "string")
         then {oauthAccount: {
           accountUuid: $account.accountUuid,
           organizationUuid: $account.organizationUuid
         }}
         else {}
         end)
      ' "$controller_claude_state" > "$isolated_home/.claude.json"
    else
      jq -n '{hasCompletedOnboarding: true}' > "$isolated_home/.claude.json"
    fi
    chmod 600 "$isolated_home/.claude.json"
    # Claude OAuth is stored in the macOS Keychain under the controller USER
    # and HOME. Keep those values only for CLI bootstrap; the additional
    # settings above replace them for the session and all tool subprocesses.
    process_home="$controller_home"
    process_user="$controller_user"
    process_logname="${LOGNAME:-$controller_user}"
    if [[ "$(basename "$1")" == "claude" ]]; then
      if [[ "${FLEET_HEALTHCHECK:-0}" == "1" ]]; then
        # `--mcp-config` accepts a variadic list and would consume `auth status`
        # as file names. Authentication healthchecks do not need MCP state, but
        # must still load the same isolated settings used by the real session.
        set -- "$1" --setting-sources "" --settings "$claude_config/settings.json" \
          "${@:2}"
      else
        set -- "$1" --setting-sources "" --settings "$claude_config/settings.json" \
          --strict-mcp-config --mcp-config "$claude_config/mcp.json" "${@:2}"
      fi
    fi
    ;;
  codex|codex_candidate)
    # Fleet Codex processes must never merge controller and legacy hook trees:
    # current Codex releases can otherwise emit two physical submit events for
    # one Enter. Copy authentication only and install one controller-owned
    # CMUX bridge in an ephemeral home for every authority/profile.
    provision_fleet_codex_home
    if [[ "$(basename "$1")" == "codex" && "${FLEET_HEALTHCHECK:-0}" != "1" ]]; then
      set -- "$@" --enable hooks --dangerously-bypass-hook-trust
      for event in SessionStart UserPromptSubmit Stop; do
        set -- "$@" -c "$(codex_hook_override "$event")"
      done
    fi
    ;;
  glm|minimax|minimax_checker)
    xdg_config="$isolated_home/xdg/config"
    prepare_opencode_data_home
    xdg_state="$isolated_home/xdg/state"
    mkdir -p "$xdg_config" "$xdg_data" "$xdg_state"
    for mapping in \
      "$controller_home/.config/opencode:$xdg_config" \
      "$controller_home/.local/share/opencode:$xdg_data" \
      "$controller_home/.local/state/opencode:$xdg_state"; do
      source_dir="${mapping%%:*}"
      destination_root="${mapping#*:}"
      if [[ -d "$source_dir" ]]; then
        if [[ -L "$source_dir" ]]; then
          echo "OpenCode provider state root must not be a symlink: $source_dir" >&2
          exit 2
        fi
        cp -R "$source_dir" "$destination_root/"
        validate_isolated_provider_tree "$destination_root/$(basename "$source_dir")"
      fi
    done
    # OpenCode may allow its own truncated-output directory after the agent's
    # catch-all external deny. Never seed that exception with controller
    # history; the isolated process may populate only its fresh copy.
    rm -rf -- "$xdg_data/opencode/tool-output"
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

# OpenCode permissions are resolved after global and project configuration are
# merged. Validate the exact effective tool map inside the same isolated XDG
# environment before the TUI starts; a prose role label or router declaration
# is never accepted as enforcement evidence.
if [[ "$role_type" =~ ^(glm|minimax|minimax_checker)$ ]] && \
   [[ "$(basename "$1")" == "opencode" ]]; then
  # Fleet-up's preflight is the exact two-argument `opencode --version`
  # command. Do not let an inherited FLEET_HEALTHCHECK value bypass policy
  # validation for a real agent launch.
  if [[ ! ( "${FLEET_HEALTHCHECK:-0}" == "1" && $# -eq 2 && "$2" == "--version" ) ]]; then
    opencode_agent=""
    for ((index=1; index<=$#; index++)); do
      if [[ "${!index}" == "--agent" && $((index + 1)) -le $# ]]; then
        next_index=$((index + 1))
        opencode_agent="${!next_index}"
        break
      fi
    done
    if [[ -z "$opencode_agent" ]]; then
      echo "OpenCode fleet command lacks a dedicated --agent identity" >&2
      exit 2
    fi
    /usr/bin/env -i "${keep[@]}" python3 "$repo_root/scripts/opencode_policy.py" \
      "$opencode_agent" --executable "$1" --quiet || exit 2
  fi
fi

set +e
/usr/bin/env -i "${keep[@]}" "$@"
rc=$?
set -e
exit "$rc"
