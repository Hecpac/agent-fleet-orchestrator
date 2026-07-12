#!/usr/bin/env bash
set -euo pipefail

# Boot a capability-ordered cmux fleet from orchestration/router.yaml.
#
# Usage:
#   ./scripts/fleet-up.sh <feature> [--preset <name>]
#   ./scripts/fleet-up.sh <feature> [--lead-provider <role>] [instance=role ...]
#   ./scripts/fleet-up.sh --list-presets
#
# A bare role is shorthand for role=role. Named instances allow duplicate role
# types, for example: triage_scope=triage triage_sources=triage.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
router="$repo_root/scripts/router_config.py"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
export CMUX_QUIET=1

usage() {
  cat >&2 <<'EOF'
Usage:
  fleet-up.sh <feature> [--preset <name>] [--target-repo <path>]
  fleet-up.sh <feature> [--lead-provider <role>] [--allow-fallback] [instance=role ...]
  fleet-up.sh --list-presets

--target-repo <path>: git repository the fleet works on. Every instance with
write authority gets its own detached git worktree there and its agent starts
inside it, so writers never share a working tree with the daemon or each other.
EOF
}

if [[ "${1:-}" == "--list-presets" ]]; then
  exec python3 "$router" presets
fi

feature="${1:-}"
if [[ -z "$feature" ]]; then
  usage
  exit 2
fi
if [[ ! "$feature" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  echo "Invalid feature name '$feature'. Use letters, numbers, dot, underscore, or dash." >&2
  exit 2
fi
shift

preset=""
lead_provider="${FLEET_LEAD_PROVIDER:-}"
allow_fallback=0
target_repo="${FLEET_TARGET_REPO:-}"
instance_specs=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --preset)
      [[ $# -ge 2 ]] || { echo "--preset requires a name" >&2; exit 2; }
      preset="$2"
      shift 2
      ;;
    --target-repo)
      [[ $# -ge 2 ]] || { echo "--target-repo requires a path" >&2; exit 2; }
      target_repo="$2"
      shift 2
      ;;
    --lead-provider|--lead)
      [[ $# -ge 2 ]] || { echo "$1 requires a role type" >&2; exit 2; }
      lead_provider="$2"
      shift 2
      ;;
    --allow-fallback)
      allow_fallback=1
      shift
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do instance_specs+=("$1"); shift; done
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
    *)
      instance_specs+=("$1")
      shift
      ;;
  esac
done

if [[ -n "$target_repo" ]]; then
  if ! git -C "$target_repo" rev-parse --git-dir >/dev/null 2>&1; then
    echo "--target-repo is not a git repository: $target_repo" >&2
    exit 2
  fi
  target_repo="$(cd "$target_repo" && pwd)"
fi

plan_args=(plan --format records)
[[ -n "$preset" ]] && plan_args+=(--preset "$preset")
[[ -n "$lead_provider" ]] && plan_args+=(--lead-provider "$lead_provider")
(( allow_fallback )) && plan_args+=(--allow-fallback)
[[ "${FLEET_NO_LEAD:-0}" == "1" ]] && plan_args+=(--no-lead)
if (( ${#instance_specs[@]} > 0 )); then
  plan_args+=("${instance_specs[@]}")
fi

# Resolve and validate everything before the first cmux mutation.
plan_output="$(python3 "$router" "${plan_args[@]}")" || exit 2

schema_version=""
resolved_preset=""
lead_role_type=""
lead_command=""
lead_executable=""
lead_requires_env=""
lead_ready_pattern=""
lead_phase="CONTROL"
lead_tool_access=""
lead_provider=""
instance_ids=()
role_types=()
display_ranks=()
runners=()
commands=()
executables=()
required_envs=()
resource_classes=()
models=()
authorities=()
ready_patterns=()
phases=()
tool_accesses=()
providers=()
warnings=()

while IFS=$'\x1f' read -r record a b c d e f g h i j k l m n; do
  case "$record" in
    META)
      schema_version="$a"
      resolved_preset="$b"
      ;;
    LEAD)
      lead_role_type="$b"
      lead_command="$e"
      lead_executable="$f"
      lead_requires_env="$g"
      lead_ready_pattern="$j"
      lead_phase="$k"
      lead_tool_access="$l"
      lead_provider="$m"
      ;;
    INSTANCE)
      instance_ids+=("$a")
      role_types+=("$b")
      display_ranks+=("$c")
      runners+=("$d")
      commands+=("$e")
      executables+=("$f")
      required_envs+=("$g")
      resource_classes+=("$h")
      models+=("$i")
      authorities+=("$j")
      ready_patterns+=("$k")
      phases+=("$l")
      tool_accesses+=("$m")
      providers+=("$n")
      ;;
    WARNING)
      warnings+=("$a")
      ;;
  esac
done <<< "$plan_output"

[[ -n "$schema_version" ]] || { echo "Router returned no plan metadata." >&2; exit 2; }
if (( ${#warnings[@]} > 0 )); then
  for warning in "${warnings[@]}"; do
    echo "WARNING: $warning" >&2
  done
fi

check_required_env() {
  local csv="$1" env_name
  [[ -z "$csv" ]] && return 0
  local old_ifs="$IFS"
  IFS=','
  for env_name in $csv; do
    if [[ -z "${!env_name:-}" ]]; then
      echo "Missing required environment variable: $env_name" >&2
      IFS="$old_ifs"
      return 1
    fi
  done
  IFS="$old_ifs"
}

interactive_launch_command() {
  local role_type="$1" authority="$2" required_csv="$3" command_shell="$4"
  [[ -n "$required_csv" ]] || required_csv="-"
  printf '%q %q %q %q %s' \
    "$repo_root/scripts/run-interactive-agent.sh" "$role_type" "$authority" "$required_csv" "$command_shell"
}

wait_for_agent_prompt() {
  local surface="$1" label="$2" ready_pattern="$3" executable="$4"
  local attempts="${FLEET_BOOT_WAIT_ATTEMPTS:-15}"
  local delay="${FLEET_BOOT_WAIT_DELAY:-2}"
  local attempt screen=""
  for ((attempt=1; attempt<=attempts; attempt++)); do
    screen="$(cmux read-screen --surface "$surface" --workspace "$ws_ref" --lines 8 2>/dev/null || true)"
    tty_name="$(cmux tree --workspace "$ws_ref" | awk -v target="$surface" '
      index($0, target) { if (match($0, /tty=[^ ]+/)) print substr($0, RSTART + 4, RLENGTH - 4) }
    ')"
    process_alive=0
    if [[ -n "$tty_name" ]] && ps -t "$tty_name" -o command= 2>/dev/null | grep -Fq "$executable"; then
      process_alive=1
    fi
    if (( process_alive == 1 )) && grep -Eq "$ready_pattern" <<< "$screen"; then
      return 0
    fi
    [[ "$delay" == "0" ]] || sleep "$delay"
  done
  echo "Interactive agent '$label' did not reach a recognized input prompt." >&2
  echo "Last screen snapshot:" >&2
  printf '%s\n' "$screen" >&2
  return 1
}

if [[ "${FLEET_NO_LEAD:-0}" != "1" ]]; then
  [[ -n "$lead_executable" ]] || { echo "Router did not resolve a lead provider." >&2; exit 2; }
  command -v "$lead_executable" >/dev/null 2>&1 || {
    echo "Lead executable not found: $lead_executable" >&2
    exit 2
  }
  check_required_env "$lead_requires_env" || exit 2
fi

for ((i=0; i<${#instance_ids[@]}; i++)); do
  if [[ "${runners[$i]}" == "interactive" ]]; then
    command -v "${executables[$i]}" >/dev/null 2>&1 || {
      echo "${instance_ids[$i]} needs missing executable: ${executables[$i]}" >&2
      exit 2
    }
    check_required_env "${required_envs[$i]}" || {
      echo "Role instance ${instance_ids[$i]} is not ready." >&2
      exit 2
    }
  else
    command -v ollama >/dev/null 2>&1 || { echo "ollama is required for ${instance_ids[$i]}" >&2; exit 2; }
    ollama show "${models[$i]}" >/dev/null 2>&1 || {
      echo "Local model not installed for ${instance_ids[$i]}: ${models[$i]}" >&2
      exit 2
    }
  fi
done

manifest="$runs_dir/fleet-$feature.manifest"
if [[ -e "$manifest" ]]; then
  echo "Manifest already exists: $manifest" >&2
  echo "Close or reconcile the existing fleet before reusing '$feature'." >&2
  exit 2
fi
if ! cmux ping >/dev/null 2>&1; then
  echo "cmux app is not running (cmux ping failed). Open cmux.app first." >&2
  exit 1
fi

mkdir -p "$runs_dir"
created_workspace=0
completed=0
manifest_published=0
ws_ref=""
manifest_tmp=""
created_worktrees=()
cleanup_on_exit() {
  local code=$?
  if (( code != 0 && created_workspace == 1 && completed == 0 )); then
    cmux close-workspace --workspace "$ws_ref" >/dev/null 2>&1 || true
  fi
  if (( code != 0 && manifest_published == 1 && completed == 0 )); then
    rm -f "$manifest"
    rm -f "${manifest%.manifest}.state.json"
  fi
  if (( code != 0 && completed == 0 )); then
    local wt
    for wt in ${created_worktrees[@]+"${created_worktrees[@]}"}; do
      git -C "$target_repo" worktree remove --force "$wt" >/dev/null 2>&1 || true
    done
  fi
  [[ -n "$manifest_tmp" && -e "$manifest_tmp" ]] && rm -f "$manifest_tmp"
  return "$code"
}
trap cleanup_on_exit EXIT

# Writers never share a working tree: each write-authority instance gets a
# detached git worktree of the target repo and its agent starts inside it.
worktrees=()
if [[ -n "$target_repo" ]]; then
  for ((i=0; i<${#instance_ids[@]}; i++)); do
    worktrees[$i]=""
    if [[ "${authorities[$i]}" == "write" ]]; then
      wt="$runs_dir/worktrees/$feature-${instance_ids[$i]}"
      if [[ -e "$wt" ]]; then
        echo "Worktree path already exists: $wt" >&2
        exit 2
      fi
      mkdir -p "$runs_dir/worktrees"
      chmod 700 "$runs_dir/worktrees"
      if ! git -C "$target_repo" worktree add --detach "$wt" >/dev/null 2>&1; then
        echo "Could not create worktree for ${instance_ids[$i]} at $wt" >&2
        exit 2
      fi
      worktrees[$i]="$wt"
      created_worktrees+=("$wt")
    fi
  done
fi

ws_output="$(cmux new-workspace --name "fleet-$feature" --cwd "$repo_root" --focus false)"
ws_ref="$(grep -o 'workspace:[0-9]*' <<< "$ws_output" | head -1)"
[[ -n "$ws_ref" ]] || { echo "Could not parse workspace ref from: $ws_output" >&2; exit 1; }
created_workspace=1
cmux workspace-action --action set-color --workspace "$ws_ref" --color "#7C3AED" >/dev/null

lead_surface="$(cmux tree --workspace "$ws_ref" | grep -o 'surface:[0-9]*' | head -1)"
[[ -n "$lead_surface" ]] || { echo "Could not find initial lead surface." >&2; exit 1; }
lead_title="lead"
[[ "${FLEET_NO_LEAD:-0}" == "1" ]] && lead_title="monitor"
cmux rename-tab --surface "$lead_surface" --workspace "$ws_ref" "$lead_title" >/dev/null

# cmux inserts every right split next to the still-focused lead. Creating the
# sorted plan in reverse therefore renders the requested rank order naturally.
surfaces=()
for ((i=${#instance_ids[@]}-1; i>=0; i--)); do
  pane_output="$(cmux new-pane --direction right --workspace "$ws_ref" --focus false)"
  surface="$(grep -o 'surface:[0-9]*' <<< "$pane_output" | head -1)"
  [[ -n "$surface" ]] || { echo "Could not create pane for ${instance_ids[$i]}" >&2; exit 1; }
  surfaces[$i]="$surface"
  cmux rename-tab --surface "$surface" --workspace "$ws_ref" "${instance_ids[$i]}" >/dev/null
  if [[ "${runners[$i]}" == "interactive" ]]; then
    launch_command="$(interactive_launch_command "${role_types[$i]}" "${authorities[$i]}" "${required_envs[$i]}" "${commands[$i]}")"
    if [[ -n "${worktrees[$i]:-}" ]]; then
      launch_command="$(printf 'cd %q && ' "${worktrees[$i]}")$launch_command"
    fi
    cmux send --surface "$surface" --workspace "$ws_ref" "$launch_command" >/dev/null
  else
    cmux send --surface "$surface" --workspace "$ws_ref" \
      "clear; echo '== worker: ${instance_ids[$i]} (${role_types[$i]}) — idle =='; echo 'dispatch with: ./scripts/fleet-dispatch.sh $feature ${instance_ids[$i]} \"<task>\"'" >/dev/null
  fi
  cmux send-key --surface "$surface" --workspace "$ws_ref" enter >/dev/null
  if [[ "${runners[$i]}" == "interactive" ]]; then
    wait_for_agent_prompt "$surface" "${instance_ids[$i]}/${role_types[$i]}" "${ready_patterns[$i]}" "${executables[$i]}" || exit 1
  else
    cmux read-screen --surface "$surface" --workspace "$ws_ref" --lines 3 >/dev/null
  fi
done

expected_titles=("$lead_title")
if (( ${#instance_ids[@]} > 0 )); then
  expected_titles+=("${instance_ids[@]}")
fi
expected_csv="$(IFS=,; echo "${expected_titles[*]}")"
cmux --json tree --workspace "$ws_ref" \
  | python3 "$router" verify-layout --expected "$expected_csv" >/dev/null

tree_both="$(cmux tree --workspace "$ws_ref" --id-format both)"
uuid_for_ref() {
  local target="$1"
  awk -v target="$target" '
    {
      for (i = 1; i < NF; i++) {
        if ($i == target && length($(i + 1)) == 36) {
          print $(i + 1)
          exit
        }
      }
    }
  ' <<< "$tree_both"
}

workspace_uuid="$(uuid_for_ref "$ws_ref")"
lead_uuid="$(uuid_for_ref "$lead_surface")"
[[ -n "$workspace_uuid" && -n "$lead_uuid" ]] || {
  echo "Could not capture durable cmux UUIDs for the new fleet." >&2
  exit 1
}

manifest_tmp="$(mktemp "$runs_dir/.fleet-$feature.manifest.XXXXXX")"
{
  echo "schema_version=$schema_version"
  echo "feature=$feature"
  echo "preset=$resolved_preset"
  echo "created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "workspace_cwd=$repo_root"
  if [[ -n "$target_repo" ]]; then
    echo "target_repo=$target_repo"
  fi
  echo "workspace=$ws_ref"
  echo "workspace_uuid=$workspace_uuid"
  echo "lead=$lead_surface"
  echo "lead.uuid=$lead_uuid"
  echo "lead.phase=$lead_phase"
  echo "lead.authority=control"
  echo "lead.tool_access=$lead_tool_access"
  echo "lead.provider=$lead_provider"
  if [[ "${FLEET_NO_LEAD:-0}" == "1" ]]; then
    echo "lead.role_type=monitor"
    echo "lead.runner=monitor"
  else
    echo "lead.role_type=$lead_role_type"
    echo "lead.runner=interactive"
  fi
  for ((i=0; i<${#instance_ids[@]}; i++)); do
    surface_uuid="$(uuid_for_ref "${surfaces[$i]}")"
    [[ -n "$surface_uuid" ]] || { echo "Missing UUID for ${instance_ids[$i]}" >&2; exit 1; }
    echo "${instance_ids[$i]}=${surfaces[$i]}"
    echo "${instance_ids[$i]}.uuid=$surface_uuid"
    echo "${instance_ids[$i]}.role_type=${role_types[$i]}"
    echo "${instance_ids[$i]}.runner=${runners[$i]}"
    echo "${instance_ids[$i]}.display_rank=${display_ranks[$i]}"
    echo "${instance_ids[$i]}.resource_class=${resource_classes[$i]}"
    echo "${instance_ids[$i]}.authority=${authorities[$i]}"
    echo "${instance_ids[$i]}.phase=${phases[$i]}"
    echo "${instance_ids[$i]}.tool_access=${tool_accesses[$i]}"
    echo "${instance_ids[$i]}.provider=${providers[$i]}"
    if [[ -n "${worktrees[$i]:-}" ]]; then
      echo "${instance_ids[$i]}.worktree=${worktrees[$i]}"
    fi
  done
} > "$manifest_tmp"
mv "$manifest_tmp" "$manifest"
manifest_tmp=""
manifest_published=1
python3 "$repo_root/scripts/fleet_state.py" init "$manifest" >/dev/null

if [[ "${FLEET_NO_LEAD:-0}" == "1" ]]; then
  cmux send --surface "$lead_surface" --workspace "$ws_ref" \
    "clear; echo '== fleet-$feature monitor — no lead agent =='" >/dev/null
  cmux send-key --surface "$lead_surface" --workspace "$ws_ref" enter >/dev/null
  cmux read-screen --surface "$lead_surface" --workspace "$ws_ref" --lines 3 >/dev/null
else
  lead_launch="$(interactive_launch_command "$lead_role_type" control "$lead_requires_env" "$lead_command")"
  cmux send --surface "$lead_surface" --workspace "$ws_ref" "$lead_launch" >/dev/null
  cmux send-key --surface "$lead_surface" --workspace "$ws_ref" enter >/dev/null

  wait_for_agent_prompt "$lead_surface" "lead/$lead_role_type" "$lead_ready_pattern" "$lead_executable" || exit 1

  cmux send --surface "$lead_surface" --workspace "$ws_ref" \
    "Eres el lead ($lead_role_type) de fleet-$feature en $ws_ref. El preset resuelto es '$resolved_preset'. Lee $manifest y verifica UUIDs/surfaces contra 'cmux tree --workspace $ws_ref --id-format both'. Los panes están ordenados por capacidad. No despaches todavía: reporta el roster en una línea y espera instrucciones." >/dev/null
  [[ "${FLEET_SEND_KEY_DELAY:-0.2}" == "0" ]] || sleep "${FLEET_SEND_KEY_DELAY:-0.2}"
  cmux send-key --surface "$lead_surface" --workspace "$ws_ref" enter >/dev/null
  cmux read-screen --surface "$lead_surface" --workspace "$ws_ref" --lines 8 >/dev/null
fi

completed=1
echo "fleet-$feature is up (preset: $resolved_preset, lead: ${lead_role_type:-monitor})."
echo "manifest: $manifest"
cat "$manifest"
