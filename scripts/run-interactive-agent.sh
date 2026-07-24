#!/usr/bin/env bash
set -euo pipefail

# Launch an interactive agent with an explicit environment allowlist.
# Secrets are inherited only when the role declares them in requires_env.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
controller_home="${HOME:?HOME must be set by CONTROL}"
controller_user="${USER:-fleet_controller}"
controller_user_sha256="$(printf '%s' "$controller_user" | shasum -a 256 | awk '{print $1}')"
execution_profile="${FLEET_EXECUTION_PROFILE:-native}"
fleet_agent_mcp_proxy="$repo_root/scripts/fleet_agent_mcp.py"
fleet_agent_mcp_python=""
fleet_agent_mcp_enabled=0
fleet_codex_home=""
fleet_codex_home_helper="$repo_root/scripts/fleet_codex_home.py"
codex_runtime_permission=""
codex_runtime_permission_name=""
kimi_bridge_pid=""
kimi_bridge_python=""
kimi_events_file=""
kimi_config_file=""
kimi_session_id=""
kimi_share_dir=""
kimi_state_root="${FLEET_KIMI_STATE_ROOT:-/tmp/agent-fleet-orchestrator-kimi}"
kimi_work_dir=""

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

# A provider receives the specialist proxy only when CONTROL supplied one
# already-bound Unix endpoint. Lead/control gets the health-only base endpoint
# and must never receive this specialist surface.
if [[ "$authority" != "control" \
  && -f "$fleet_agent_mcp_proxy" \
  && ! -L "$fleet_agent_mcp_proxy" ]] \
  && python3 - "${FLEET_CONTROL_SOCKET:-}" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
if not path or not os.path.isabs(path):
    raise SystemExit(1)
try:
    info = os.lstat(path)
except OSError:
    raise SystemExit(1)
if (
    not stat.S_ISSOCK(info.st_mode)
    or info.st_uid != os.geteuid()
    or stat.S_IMODE(info.st_mode) != 0o600
):
    raise SystemExit(1)
PY
then
  fleet_agent_mcp_python="$(command -v python3)"
  fleet_agent_mcp_enabled=1
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
  if [[ -n "$kimi_bridge_pid" ]]; then
    # Kimi flushes TurnEnd before returning to its prompt. Give the bridge one
    # final polling interval before stopping it on pane shutdown.
    sleep 0.2
    kill "$kimi_bridge_pid" >/dev/null 2>&1 || true
    wait "$kimi_bridge_pid" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$isolated_home"
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

codex_mission_permission_profile() {
  local fleet_codex_home="$1" controller_codex_home="$2" fleet_runs_dir="$3"
  python3 -c '
import json
import os
import sys

socket, authority, controller_home, controller_codex_home, fleet_codex_home, fleet_runs_dir = sys.argv[1:]
profile = "fleet_writer" if authority == "write" else "fleet_reader"
workspace_access = "write" if authority == "write" else "read"
denied = []
for path in (controller_home, controller_codex_home, fleet_codex_home, fleet_runs_dir):
    lexical = os.path.abspath(path)
    canonical = os.path.realpath(lexical)
    for candidate in (lexical, canonical):
        if candidate not in denied:
            denied.append(candidate)
rules = []
for path in denied:
    rules.extend((path, path.rstrip("/") + "/**"))
for auth_home in (controller_codex_home, fleet_codex_home):
    rules.append(os.path.join(os.path.abspath(auth_home), "auth.json"))
    rules.append(os.path.join(os.path.realpath(auth_home), "auth.json"))
rules = list(dict.fromkeys(rules))
filesystem = (
    "filesystem={glob_scan_max_depth=64,\":minimal\"=\"read\","
    + "\":workspace_roots\"={\".\"=\"" + workspace_access + "\"},"
    + ",".join(json.dumps(path) + "=\"deny\"" for path in rules)
    + "},"
)
print(
    "permissions={" + profile
    + "={description=\"Mission-scoped Fleet Control client with credential roots denied.\","
    + filesystem
    + "network={enabled=true,mode=\"limited\",unix_sockets={"
    + json.dumps(socket) + "=\"allow\"}}}}"
)
' "${FLEET_CONTROL_SOCKET:-}" "$authority" "$controller_home" \
    "$controller_codex_home" "$fleet_codex_home" "$fleet_runs_dir"
}

provision_fleet_codex_home() {
  local controller_codex_home codex_sqlite_home
  controller_codex_home="${CODEX_HOME:-$controller_home/.codex}"
  if [[ ! -f "$fleet_codex_home_helper" || -L "$fleet_codex_home_helper" ]]; then
    echo "Fleet Codex auth-home helper is unavailable" >&2
    exit 2
  fi
  if ! fleet_codex_home="$(python3 "$fleet_codex_home_helper" provision \
    "$controller_codex_home" "$isolated_home")"; then
    exit 2
  fi
  codex_sqlite_home="$isolated_home/codex-sqlite"
  mkdir -m 700 "$codex_sqlite_home"
  if (( fleet_agent_mcp_enabled == 1 )) && [[ -n "${FLEET_MISSION_ID:-}" ]]; then
    if [[ "${FLEET_RUNS_DIR:-}" != /* ]]; then
      echo "Mission Codex role requires an absolute FLEET_RUNS_DIR" >&2
      exit 2
    fi
    codex_runtime_permission_name="fleet_reader"
    [[ "$authority" != "write" ]] || codex_runtime_permission_name="fleet_writer"
    codex_runtime_permission="$(codex_mission_permission_profile \
      "$fleet_codex_home" "$controller_codex_home" "$FLEET_RUNS_DIR")"
  fi
  keep+=(
    "CODEX_HOME=$fleet_codex_home"
    "CODEX_SQLITE_HOME=$codex_sqlite_home"
    # cmux injects its own Codex hooks through CLI `-c` flags. Disable those
    # in the shim, then install the repo-owned overrides below with the
    # surface identity embedded in each command.
    "CMUX_CODEX_HOOKS_DISABLED=1"
  )
}

codex_session_override() {
  local section="$1"
  python3 -c '
import json
import os
import sys

section, workspace, command, proxy = sys.argv[1:]
if section == "projects":
    value = {os.path.realpath(workspace): {"trust_level": "untrusted"}}
elif section == "mcp_servers":
    value = {"fleet_control": {
        "command": command,
        "args": [proxy],
        "required": True,
        "startup_timeout_sec": 10,
        "tool_timeout_sec": 1815,
    }}
else:
    raise SystemExit(2)

def toml(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ",".join(toml(item) for item in value) + "]"
    return "{" + ",".join(json.dumps(key) + "=" + toml(item) for key, item in value.items()) + "}"

print(section + "=" + toml(value))
' "$section" "$PWD" "$fleet_agent_mcp_python" "$fleet_agent_mcp_proxy"
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
  if [[ -L "$opencode_state_dir" || ( -e "$opencode_state_dir" && ! -d "$opencode_state_dir" ) ]]; then
    echo "OpenCode evidence state is not a directory for surface $surface_id" >&2
    exit 2
  fi
  if [[ ! -e "$opencode_state_dir" ]]; then
    mkdir -m 700 "$opencode_state_dir"
  fi
  xdg_data="$opencode_state_dir/data"
}

compiled_workflow="${FLEET_COMPILED_WORKFLOW:-}"
compiled_digest="${FLEET_COMPILED_DIGEST:-}"
if [[ -n "$compiled_workflow" || -n "$compiled_digest" ]]; then
  if [[ -z "$compiled_workflow" || -z "$compiled_digest" ]]; then
    echo "Compiled workflow path and digest must be inherited together." >&2
    exit 2
  fi
  if [[ "$compiled_workflow" != /* ]]; then
    echo "FLEET_COMPILED_WORKFLOW must be an absolute path." >&2
    exit 2
  fi
  if [[ ! "$compiled_digest" =~ ^[0-9a-f]{64}$ ]]; then
    echo "FLEET_COMPILED_DIGEST must be a lowercase SHA-256 digest." >&2
    exit 2
  fi
fi

keep=()
for name in PATH SHELL TERM COLORTERM LANG LC_ALL LC_CTYPE TMPDIR \
  CLAUDE_CODE_NO_FLICKER HOMEBREW_PREFIX HOMEBREW_CELLAR HOMEBREW_REPOSITORY \
  FLEET_RUNS_DIR FLEET_MISSION_ID FLEET_CONTROL_SOCKET \
  FLEET_COMPILED_WORKFLOW FLEET_COMPILED_DIGEST; do
  if [[ -n "${!name:-}" ]]; then
    keep+=("$name=${!name}")
  fi
done

keep+=(
  "FLEET_HOME=$isolated_home"
  "FLEET_EXECUTION_PROFILE=$execution_profile"
  "FLEET_HUMAN_UID=sha256:$controller_user_sha256"
  # The cmux CLI shim may intentionally consume CMUX_* variables before the
  # provider starts. Preserve controller evidence locations under the Fleet
  # namespace so nested CONTROL wrappers never fall back to ephemeral HOME.
  "FLEET_CONTROLLER_HOOK_DIR=${CMUX_HOOK_DIR:-$controller_home/.cmuxterm}"
  "FLEET_CONTROLLER_EVENTS_LOG=${CMUX_EVENTS_LOG:-$controller_home/.cmuxterm/events.jsonl}"
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
    if (( fleet_agent_mcp_enabled == 1 )); then
      jq -n \
        --arg command "$fleet_agent_mcp_python" \
        --arg proxy "$fleet_agent_mcp_proxy" \
        '{mcpServers: {fleet_control: {
          type: "stdio", command: $command, args: [$proxy]
        }}}' > "$claude_config/mcp.json"
    else
      jq -n '{mcpServers: {}}' > "$claude_config/mcp.json"
    fi
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
    # one Enter. Bind the canonical auth file once (never copy its single-use
    # refresh token) and install one controller-owned CMUX bridge.
    provision_fleet_codex_home
    if [[ "$(basename "$1")" == "codex" && "${FLEET_HEALTHCHECK:-0}" != "1" ]]; then
      set -- "$@" --strict-config -c "$(codex_session_override projects)"
      if (( fleet_agent_mcp_enabled == 1 )); then
        set -- "$@" -c "$(codex_session_override mcp_servers)"
      fi
    elif [[ "$(basename "$1")" == "codex" \
      && "${FLEET_HEALTHCHECK:-0}" == "1" \
      && "${2:-}" == "mcp" ]]; then
      # `login status` rejects --strict-config in Codex 0.144.5. MCP discovery
      # accepts a config layer when it is placed before the subcommand.
      codex_health_command=("$1" -c "$(codex_session_override projects)")
      if (( fleet_agent_mcp_enabled == 1 )); then
        codex_health_command+=(-c "$(codex_session_override mcp_servers)")
      fi
      codex_health_command+=("${@:2}")
      set -- "${codex_health_command[@]}"
    fi
    if [[ "$(basename "$1")" == "codex" && "${FLEET_HEALTHCHECK:-0}" != "1" ]]; then
      hook_trust_bypass=0
      for argument in "$@"; do
        if [[ "$argument" == "--dangerously-bypass-hook-trust" ]]; then
          hook_trust_bypass=1
          break
        fi
      done
      set -- "$@" --enable hooks
      if (( hook_trust_bypass == 0 )); then
        set -- "$@" --dangerously-bypass-hook-trust
      fi
      for event in SessionStart UserPromptSubmit Stop; do
        set -- "$@" -c "$(codex_hook_override "$event")"
      done
    fi
    # The final profile is generated only after the ephemeral CODEX_HOME
    # exists. Start from Codex's minimal runtime roots, reopen only the active
    # workspace, and deny CONTROL/auth/run-state roots exactly.
    if [[ "$(basename "$1")" == "codex" \
      && "${FLEET_HEALTHCHECK:-0}" != "1" \
      && -n "$codex_runtime_permission" ]]; then
      set -- "$@" -c "$codex_runtime_permission" \
        -c "default_permissions=\"$codex_runtime_permission_name\""
    fi
    ;;
  kimi)
    controller_kimi_config="$controller_home/.kimi/config.toml"
    if [[ ! -f "$controller_kimi_config" || -L "$controller_kimi_config" ]]; then
      echo "Kimi controller configuration is unavailable" >&2
      exit 2
    fi
    if [[ "${FLEET_HEALTHCHECK:-0}" == "1" ]]; then
      kimi_share_dir="$isolated_home/.kimi"
    else
      if [[ ! "${CMUX_WORKSPACE_ID:-}" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ \
        || ! "${CMUX_SURFACE_ID:-}" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]; then
        echo "Kimi Fleet launch requires canonical cmux workspace/surface identities" >&2
        exit 2
      fi
      if [[ "$kimi_state_root" != /* || -L "$kimi_state_root" ]]; then
        echo "Kimi evidence state root must not be a symlink" >&2
        exit 2
      fi
      mkdir -p "$kimi_state_root"
      chmod 700 "$kimi_state_root"
      canonical_surface_id="$(printf '%s' "$CMUX_SURFACE_ID" | tr '[:lower:]' '[:upper:]')"
      kimi_state_dir="$kimi_state_root/$canonical_surface_id"
      if [[ -L "$kimi_state_dir" || ( -e "$kimi_state_dir" && ! -d "$kimi_state_dir" ) ]]; then
        echo "Kimi evidence state is unsafe for surface $canonical_surface_id" >&2
        exit 2
      fi
      mkdir -p "$kimi_state_dir"
      chmod 700 "$kimi_state_dir"
      kimi_share_dir="$kimi_state_dir/share"
      kimi_events_file="$kimi_state_dir/events.jsonl"
      kimi_session_id="$(printf '%s' "$CMUX_SURFACE_ID" | tr '[:upper:]' '[:lower:]')"
      kimi_work_dir="$(pwd -P)"
    fi
    if [[ -L "$kimi_share_dir" || ( -e "$kimi_share_dir" && ! -d "$kimi_share_dir" ) ]]; then
      echo "Kimi share state is unsafe" >&2
      exit 2
    fi
    # kimi-code (>= 0.28) resolves config, credentials, mcp.json, and the
    # session store from one KIMI_CODE_HOME root. The fleet provisions an
    # isolated home per surface: the legacy --config-file/--work-dir/
    # --session flags and KIMI_SHARE_DIR are gone from the CLI.
    mkdir -p "$kimi_share_dir"
    chmod 700 "$kimi_share_dir"
    cp "$controller_kimi_config" "$kimi_share_dir/config.toml"
    chmod 600 "$kimi_share_dir/config.toml"
    controller_kimi_credentials="$controller_home/.kimi/credentials/kimi-code.json"
    if [[ -f "$controller_kimi_credentials" && ! -L "$controller_kimi_credentials" ]]; then
      mkdir -p "$kimi_share_dir/credentials"
      chmod 700 "$kimi_share_dir/credentials"
      cp "$controller_kimi_credentials" "$kimi_share_dir/credentials/kimi-code.json"
      chmod 600 "$kimi_share_dir/credentials/kimi-code.json"
    fi
    keep+=(
      "KIMI_CODE_HOME=$kimi_share_dir"
      "KIMI_CODE_NO_AUTO_UPDATE=1"
      "KIMI_CLI_NO_AUTO_UPDATE=1"
    )
    if [[ "$(basename "$1")" == "kimi" && "${FLEET_HEALTHCHECK:-0}" != "1" ]]; then
      if (( fleet_agent_mcp_enabled != 1 )); then
        echo "Kimi Fleet role requires an authenticated Fleet Control endpoint" >&2
        exit 2
      fi
      python3 -c '
import json
import sys
path, command, proxy = sys.argv[1:4]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({"mcpServers": {"fleet_control": {
        "command": command, "args": [proxy]
    }}}, handle, separators=(",", ":"))
' "$kimi_share_dir/mcp.json" "$fleet_agent_mcp_python" "$fleet_agent_mcp_proxy"
      chmod 600 "$kimi_share_dir/mcp.json"
      kimi_bridge_python="$(command -v python3)"
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
        if [[ "$destination_root" == "$xdg_data" \
          && -d "$destination_root/$(basename "$source_dir")" ]]; then
          # The controller removes this durable evidence home only after it has
          # persisted a terminal frontier receipt. Preserve it for provider
          # restart/reconciliation until then.
          validate_isolated_provider_tree "$destination_root/$(basename "$source_dir")"
          continue
        fi
        cp -R "$source_dir" "$destination_root/"
        validate_isolated_provider_tree "$destination_root/$(basename "$source_dir")"
      fi
    done
    # The canonical fleet agents live in the orchestrator repository, not in
    # an arbitrary --target-repo checkout. Install the fixed role set into the
    # isolated global OpenCode config so `--agent` resolves identically from
    # every launch cwd. Without this overlay OpenCode can silently fall back to
    # its generic Build agent and prompt for Bash or external-directory access.
    fleet_opencode_agents="$repo_root/.opencode/agents"
    isolated_opencode_agents="$xdg_config/opencode/agents"
    if [[ ! -d "$fleet_opencode_agents" || -L "$fleet_opencode_agents" ]]; then
      echo "Canonical OpenCode fleet agent directory is unavailable" >&2
      exit 2
    fi
    mkdir -p "$isolated_opencode_agents"
    for agent_name in fleet-reviewer glm-challenger minimax-checker; do
      source_agent="$fleet_opencode_agents/$agent_name.md"
      destination_agent="$isolated_opencode_agents/$agent_name.md"
      if [[ ! -f "$source_agent" || -L "$source_agent" ]]; then
        echo "Canonical OpenCode fleet agent is unavailable: $agent_name" >&2
        exit 2
      fi
      if [[ -L "$destination_agent" \
        || ( -e "$destination_agent" && ! -f "$destination_agent" ) ]]; then
        echo "Isolated OpenCode fleet agent path is unsafe: $agent_name" >&2
        exit 2
      fi
      cp "$source_agent" "$destination_agent"
      chmod 600 "$destination_agent"
    done
    validate_isolated_provider_tree "$xdg_config/opencode"
    if (( fleet_agent_mcp_enabled == 1 )); then
      command -v jq >/dev/null 2>&1 || {
        echo "jq is required to provision isolated OpenCode MCP settings" >&2
        exit 2
      }
      opencode_config_dir="$xdg_config/opencode"
      opencode_config="$opencode_config_dir/opencode.json"
      mkdir -p "$opencode_config_dir"
      if [[ -L "$opencode_config" \
        || ( -e "$opencode_config" && ! -f "$opencode_config" ) ]]; then
        echo "OpenCode MCP config must be a regular isolated file" >&2
        exit 2
      fi
      opencode_config_tmp="$(mktemp "$opencode_config_dir/.opencode.json.XXXXXX")"
      if [[ -f "$opencode_config" ]]; then
        jq \
          --arg command "$fleet_agent_mcp_python" \
          --arg proxy "$fleet_agent_mcp_proxy" \
          '.mcp = {fleet_control: {
            type: "local", command: [$command, $proxy], enabled: true,
            timeout: 10000
          }}' "$opencode_config" > "$opencode_config_tmp"
      else
        jq -n \
          --arg command "$fleet_agent_mcp_python" \
          --arg proxy "$fleet_agent_mcp_proxy" \
          '{mcp: {fleet_control: {
            type: "local", command: [$command, $proxy], enabled: true,
            timeout: 10000
          }}}' > "$opencode_config_tmp"
      fi
      chmod 600 "$opencode_config_tmp"
      mv "$opencode_config_tmp" "$opencode_config"
      opencode_inline_config="$(jq -nc \
        --arg command "$fleet_agent_mcp_python" \
        --arg proxy "$fleet_agent_mcp_proxy" \
        '{
          mcp: {fleet_control: {
            type: "local", command: [$command, $proxy], enabled: true,
            timeout: 10000
          }},
          agent: {
            "fleet-reviewer": {permission: {"fleet_control_*": "allow"}},
            "glm-challenger": {permission: {"fleet_control_*": "allow"}},
            "minimax-checker": {permission: {"fleet_control_*": "allow"}}
          }
        }')"
      keep+=("OPENCODE_CONFIG_CONTENT=$opencode_inline_config")
    fi
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

# A Fleet Codex role follows one descriptor-verified symlink to the controller
# auth file. It never copies or injects OAuth material; all refreshes therefore
# update the one canonical store instead of forking a single-use refresh token.
if [[ "$role_type" =~ ^(codex|codex_candidate)$ ]] \
  && [[ "$(basename "$1")" == "codex" ]]; then
  if ! /usr/bin/env -i "${keep[@]}" "$1" login status \
    >/dev/null 2>&1; then
    echo "Fleet Codex canonical authentication is unavailable" >&2
    exit 2
  fi
  if ! python3 "$fleet_codex_home_helper" verify \
    "${CODEX_HOME:-$controller_home/.codex}" "$isolated_home" >/dev/null; then
    exit 2
  fi
fi

# OpenCode's debug policy view can omit dynamically discovered MCP tools, so
# it is not readiness evidence by itself. Before the policy/TUI gate, exercise
# the exact repo-owned proxy: MCP initialize, ping, exact nine-tool discovery,
# and one real round-trip to this instance's AF_UNIX socket. At fleet boot no
# delegated identity exists yet, so the expected socket proof is a well-formed
# authentication denial; a complete caller identity can opt into an
# authenticated ping without ever being printed.
provider_basename="$(basename "$1")"
provider_mcp_preflight=0
if [[ "$role_type" =~ ^(glm|minimax|minimax_checker)$ && "$provider_basename" == "opencode" ]] \
  || [[ "$role_type" =~ ^(codex|codex_candidate)$ && "$provider_basename" == "codex" ]] \
  || [[ "$role_type" =~ ^(claude|claude_reviewer|claude_checker)$ && "$provider_basename" == "claude" ]] \
  || [[ "$role_type" == "kimi" && "$provider_basename" == "kimi" ]]; then
  provider_mcp_preflight=1
fi
if (( fleet_agent_mcp_enabled == 1 && provider_mcp_preflight == 1 )); then
  preflight_env=("${keep[@]}")
  [[ -z "${FLEET_PREFLIGHT_RUN_ID:-}" ]] \
    || preflight_env+=("FLEET_PREFLIGHT_RUN_ID=$FLEET_PREFLIGHT_RUN_ID")
  [[ -z "${FLEET_PREFLIGHT_TOKEN_ID:-}" ]] \
    || preflight_env+=("FLEET_PREFLIGHT_TOKEN_ID=$FLEET_PREFLIGHT_TOKEN_ID")
  if ! preflight_json="$(/usr/bin/env -i "${preflight_env[@]}" \
    "$fleet_agent_mcp_python" "$fleet_agent_mcp_proxy" --preflight)"; then
    echo "Specialist MCP preflight failed closed" >&2
    exit 2
  fi
  if ! python3 -c '
import json
import sys

expected = {
    "dispatch", "dispatch_many", "wait", "get_result", "relay_result",
    "request_assurance", "request_human", "inspect_roster", "inspect_mission",
}
try:
    value = json.loads(sys.argv[1])
except (json.JSONDecodeError, UnicodeDecodeError):
    raise SystemExit(1)
if (
    not isinstance(value, dict)
    or set(value) != {
        "schema_version", "status", "protocol_version", "tool_names", "socket_probe"
    }
    or value["schema_version"] != 1
    or value["status"] != "ready"
    or value["protocol_version"] != "2024-11-05"
    or not isinstance(value["tool_names"], list)
    or len(value["tool_names"]) != len(expected)
    or set(value["tool_names"]) != expected
    or value["socket_probe"] not in {"denial_ping", "authenticated_ping"}
):
    raise SystemExit(1)
' "$preflight_json"; then
    echo "Specialist MCP preflight evidence is malformed" >&2
    exit 2
  fi
  unset preflight_json preflight_env
fi

if [[ "$role_type" == "kimi" && "$provider_basename" == "kimi" \
  && "${FLEET_HEALTHCHECK:-0}" != "1" ]]; then
  /usr/bin/env -i "${keep[@]}" "PYTHONPATH=$repo_root/scripts" "$kimi_bridge_python" \
    "$repo_root/scripts/kimi_hook_bridge.py" \
    --share-dir "$kimi_share_dir" \
    --work-dir "$kimi_work_dir" \
    --session-id "$kimi_session_id" \
    --workspace-id "$CMUX_WORKSPACE_ID" \
    --surface-id "$CMUX_SURFACE_ID" \
    --hook-dir "${CMUX_HOOK_DIR:-$controller_home/.cmuxterm}" \
    --events-file "$kimi_events_file" \
    --provider moonshot-ai \
    --model kimi-code/k3 \
    >/dev/null 2>&1 &
  kimi_bridge_pid=$!
  sleep 0.1
  if ! kill -0 "$kimi_bridge_pid" >/dev/null 2>&1; then
    wait "$kimi_bridge_pid" || true
    kimi_bridge_pid=""
    echo "Kimi hook bridge failed to start" >&2
    exit 2
  fi
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
