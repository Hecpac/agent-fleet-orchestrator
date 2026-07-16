#!/usr/bin/env bash
set -euo pipefail

# Boot a capability-ordered cmux fleet from orchestration/router.yaml.
#
# Usage:
#   ./scripts/fleet-up.sh <feature> [--preset <name>] [--execution-profile <name>]
#   ./scripts/fleet-up.sh <feature> [--lead-provider <role>] [instance=role ...]
#   ./scripts/fleet-up.sh --list-presets
#
# A bare role is shorthand for role=role. Named instances allow duplicate role
# types, for example: triage_scope=triage triage_sources=triage.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
router="$repo_root/scripts/router_config.py"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
worktrees_root="${FLEET_WORKTREES_DIR:-/tmp/fleet_workspaces-$(id -u)}"
mission_id="${FLEET_MISSION_ID:-}"
execution_profile="${FLEET_EXECUTION_PROFILE:-native}"
control_socket=""
if [[ -n "$mission_id" ]]; then
  control_socket="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' \
    "${FLEET_CONTROL_SOCKET_DIR:-/tmp/fleet-control-$(id -u)}/$mission_id.sock")"
fi
export CMUX_QUIET=1

if [[ -n "$mission_id" && ! "$mission_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; then
  echo "Invalid FLEET_MISSION_ID: expected canonical lowercase UUID." >&2
  exit 2
fi

usage() {
  cat >&2 <<'EOF'
Usage:
  fleet-up.sh <feature> [--preset <name>] [--target-repo <path>] [--execution-profile <name>]
  fleet-up.sh <feature> [--lead-provider <role>] [--allow-fallback] [instance=role ...]
  fleet-up.sh --list-presets

--target-repo <path>: git repository the fleet works on. Every instance with
write authority gets a dedicated fleet/<feature>/<instance> branch and worktree
there, so committed output remains reachable after teardown.
--execution-profile <name>: native (default), sandboxed, or regulated. Profiles
change the process perimeter; router-declared tool capabilities remain intact.
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
    --execution-profile)
      [[ $# -ge 2 ]] || { echo "--execution-profile requires a name" >&2; exit 2; }
      execution_profile="$2"
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

python3 "$repo_root/scripts/fleet_manifest.py" validate-profile "$execution_profile" >/dev/null || exit 2
if [[ "$execution_profile" == "regulated" && -z "$mission_id" ]]; then
  echo "regulated execution requires a canonical FLEET_MISSION_ID" >&2
  exit 2
fi

if [[ -n "$target_repo" ]]; then
  if ! git -C "$target_repo" rev-parse --git-dir >/dev/null 2>&1; then
    echo "--target-repo is not a git repository: $target_repo" >&2
    exit 2
  fi
  # Preserve the operator-visible repository path in the manifest. Only the
  # kernel-addressed control socket needs physical-path canonicalization.
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
execution_mode="guided"
lead_role_type=""
lead_command=""
lead_executable=""
lead_requires_env=""
lead_ready_pattern=""
lead_phase="CONTROL"
lead_tool_access=""
lead_provider=""
lead_hook_source=""
lead_model=""
lead_variant=""
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
hook_sources=()
variants=()
warnings=()

while IFS=$'\x1f' read -r record a b c d e f g h i j k l m n o p; do
  case "$record" in
    META)
      schema_version="$a"
      resolved_preset="$b"
      execution_mode="${c:-guided}"
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
      lead_hook_source="$n"
      lead_model="$o"
      lead_variant="$p"
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
      hook_sources+=("$o")
      variants+=("$p")
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

preflight_interactive_role() {
  local role_type="$1" authority="$2" required_csv="$3" label="$4"
  local healthcheck_json="" argument=""
  local -a healthcheck=()

  if ! healthcheck_json="$(python3 "$router" role-field "$role_type" healthcheck)"; then
    echo "Interactive role '$label' has no valid healthcheck." >&2
    return 1
  fi
  while IFS= read -r argument; do
    healthcheck+=("$argument")
  done < <(python3 -c '
import json
import sys
for value in json.loads(sys.argv[1]):
    print(value)
' "$healthcheck_json")
  if (( ${#healthcheck[@]} == 0 )); then
    echo "Interactive role '$label' resolved an empty healthcheck." >&2
    return 1
  fi

  if ! FLEET_HEALTHCHECK=1 \
    FLEET_EXECUTION_PROFILE="$execution_profile" \
    FLEET_MISSION_ID="$mission_id" \
    FLEET_CONTROL_SOCKET="$control_socket" \
    "$repo_root/scripts/run-interactive-agent.sh" \
      "$role_type" "$authority" "${required_csv:--}" "${healthcheck[@]}" \
      >/dev/null; then
    echo "Interactive role '$label' failed its isolated runtime healthcheck." >&2
    return 1
  fi
}

interactive_launch_command() {
  local role_type="$1" authority="$2" required_csv="$3" command_shell="$4"
  [[ -n "$required_csv" ]] || required_csv="-"
  printf 'FLEET_EXECUTION_PROFILE=%q FLEET_MISSION_ID=%q FLEET_CONTROL_SOCKET=%q %q %q %q %q %s' \
    "$execution_profile" "$mission_id" "$control_socket" "$repo_root/scripts/run-interactive-agent.sh" \
    "$role_type" "$authority" "$required_csv" "$command_shell"
}

codex_control_permission_profile() {
  local socket_path="$1"
  python3 -c '
import json
import sys

socket = json.dumps(sys.argv[1])
print(
    "permissions={fleet_control={description=\"Mission-scoped read-only Fleet Control client.\","
    "extends=\":read-only\",network={enabled=true,mode=\"limited\","
    "unix_sockets={" + socket + "=\"allow\"}}}}"
)
' "$socket_path"
}

wait_for_agent_prompt() {
  local surface="$1" label="$2" ready_pattern="$3" executable="$4"
  local attempts="${FLEET_BOOT_WAIT_ATTEMPTS:-15}"
  local delay="${FLEET_BOOT_WAIT_DELAY:-2}"
  local attempt screen=""
  for ((attempt=1; attempt<=attempts; attempt++)); do
    screen="$(cmux read-screen --surface "$surface" --workspace "$ws_ref" --lines 8 2>/dev/null || true)"
    if grep -Eqi 'Not logged in|Please run /login|Invalid MCP configuration|provider state.*symlink' <<< "$screen"; then
      echo "Interactive agent '$label' reached a fatal provider/authentication screen." >&2
      printf '%s\n' "$screen" >&2
      return 1
    fi
    tty_name="$(cmux tree --workspace "$ws_ref" | awk -v target="$surface" '
      index($0, target) { if (match($0, /tty=[^ ]+/)) print substr($0, RSTART + 4, RLENGTH - 4) }
    ')"
    process_alive=0
    if [[ -n "$tty_name" ]] && ps -t "$tty_name" -o command= 2>/dev/null | grep -Fq "$executable"; then
      process_alive=1
    fi
    if (( process_alive == 1 )) && grep -Eq "$ready_pattern" <<< "$screen"; then
      if grep -Fq "Do you trust the contents of this directory?" <<< "$screen"; then
        echo "Interactive agent '$label' is blocked on an unresolved project trust gate." >&2
        return 1
      fi
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
  preflight_interactive_role "$lead_role_type" control "$lead_requires_env" \
    "lead/$lead_role_type" || exit 2
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
    preflight_interactive_role \
      "${role_types[$i]}" "${authorities[$i]}" "${required_envs[$i]}" \
      "${instance_ids[$i]}/${role_types[$i]}" || exit 2
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

# Resolve writer branches before any cmux mutation. A fleet never reuses an
# existing branch: that would mix output from two runs under one durable ref.
worktree_branches=()
worktree_base_shas=()
if [[ -n "$target_repo" ]]; then
  target_head="$(git -C "$target_repo" rev-parse --verify HEAD)"
  for ((i=0; i<${#instance_ids[@]}; i++)); do
    worktree_branches[$i]=""
    worktree_base_shas[$i]=""
    if [[ "${authorities[$i]}" == "write" ]]; then
      branch="fleet/$feature/${instance_ids[$i]}"
      if ! git -C "$target_repo" check-ref-format --branch "$branch" >/dev/null 2>&1; then
        echo "Writer branch name is invalid: $branch" >&2
        exit 2
      fi
      if git -C "$target_repo" show-ref --verify --quiet "refs/heads/$branch"; then
        echo "Writer branch already exists: $branch" >&2
        echo "Reconcile or remove it before reusing fleet '$feature'." >&2
        exit 2
      fi
      worktree_branches[$i]="$branch"
      worktree_base_shas[$i]="$target_head"
    fi
  done
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
created_worktree_branches=()
created_worktree_bases=()
cleanup_on_exit() {
  local code=$?
  local workspace_gone=0
  local rollback_complete=1
  local tree_output=""
  if (( code != 0 && created_workspace == 1 && completed == 0 )); then
    cmux close-workspace --workspace "$ws_ref" >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      if tree_output="$(cmux tree --all 2>/dev/null)"; then
        if ! awk -v target="$ws_ref" '
          { for (i = 1; i <= NF; i++) if ($i == target) found = 1 }
          END { exit(found ? 0 : 1) }
        ' <<< "$tree_output"; then
          workspace_gone=1
          break
        fi
      fi
      sleep 0.1
    done
  elif (( created_workspace == 0 )); then
    workspace_gone=1
  fi
  if (( code != 0 && completed == 0 )); then
    if (( workspace_gone == 0 )); then
      echo "WARNING: preserving writer worktrees because cmux did not confirm workspace shutdown." >&2
      rollback_complete=0
    else
      local index wt branch base branch_head checked_out_branch worktree_head
      for ((index=0; index<${#created_worktrees[@]}; index++)); do
        wt="${created_worktrees[$index]}"
        branch="${created_worktree_branches[$index]}"
        base="${created_worktree_bases[$index]}"
        checked_out_branch="$(git -C "$wt" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
        worktree_head="$(git -C "$wt" rev-parse --verify HEAD 2>/dev/null || true)"
        branch_head="$(git -C "$target_repo" rev-parse --verify "refs/heads/$branch" 2>/dev/null || true)"
        if [[ -d "$wt" && ( "$checked_out_branch" != "$branch" || \
              -z "$worktree_head" || "$worktree_head" != "$branch_head" ) ]]; then
          echo "WARNING: preserving writer worktree on unexpected HEAD after failed boot: $wt" >&2
          rollback_complete=0
          continue
        fi
        if [[ -d "$wt" && -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]]; then
          echo "WARNING: preserving dirty writer worktree after failed boot: $wt" >&2
          rollback_complete=0
          continue
        fi
        if [[ -d "$wt" ]] && ! git -C "$target_repo" worktree remove "$wt" >/dev/null 2>&1; then
          echo "WARNING: preserving writer branch because worktree cleanup failed: $branch" >&2
          rollback_complete=0
          continue
        fi
        if [[ -n "$branch_head" && "$branch_head" == "$base" ]]; then
          if ! git -C "$target_repo" update-ref -d "refs/heads/$branch" "$base" >/dev/null 2>&1; then
            echo "WARNING: could not remove unchanged writer branch $branch" >&2
            rollback_complete=0
          fi
        elif [[ -n "$branch_head" ]]; then
          echo "WARNING: preserving advanced writer branch after failed boot: $branch" >&2
          rollback_complete=0
        fi
      done
    fi
  fi
  if (( code != 0 && manifest_published == 1 && completed == 0 )); then
    if (( rollback_complete == 1 )); then
      rm -f "$manifest"
      rm -f "${manifest%.manifest}.state.json"
    else
      echo "WARNING: preserving manifest for incomplete boot recovery: $manifest" >&2
    fi
  fi
  [[ -n "$manifest_tmp" && -e "$manifest_tmp" ]] && rm -f "$manifest_tmp"
  return "$code"
}
trap cleanup_on_exit EXIT

# Writers never share a working tree: each write-authority instance gets a
# named branch and dedicated worktree of the target repo, then starts inside it.
worktrees=()
if [[ -n "$target_repo" ]]; then
  python3 -c '
import os, stat, sys
from pathlib import Path
path = Path(sys.argv[1]).expanduser()
if path.exists() or path.is_symlink():
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
        raise SystemExit("unsafe fleet worktree root")
else:
    path.mkdir(parents=True, mode=0o700)
os.chmod(path, 0o700)
' "$worktrees_root" || { echo "Refusing unsafe worktree root: $worktrees_root" >&2; exit 2; }
  for ((i=0; i<${#instance_ids[@]}; i++)); do
    worktrees[$i]=""
    if [[ "${authorities[$i]}" == "write" ]]; then
      wt="$worktrees_root/$feature-${instance_ids[$i]}"
      branch="${worktree_branches[$i]}"
      base_sha="${worktree_base_shas[$i]}"
      if [[ -e "$wt" ]]; then
        echo "Worktree path already exists: $wt" >&2
        exit 2
      fi
      if ! git -C "$target_repo" branch "$branch" "$base_sha" >/dev/null 2>&1; then
        echo "Could not create writer branch $branch; it may have been created concurrently." >&2
        exit 2
      fi
      if ! git -C "$target_repo" worktree add "$wt" "$branch" >/dev/null 2>&1; then
        git -C "$target_repo" update-ref -d "refs/heads/$branch" "$base_sha" >/dev/null 2>&1 || true
        echo "Could not create worktree for ${instance_ids[$i]} at $wt" >&2
        exit 2
      fi
      worktrees[$i]="$wt"
      created_worktrees+=("$wt")
      created_worktree_branches+=("$branch")
      created_worktree_bases+=("$base_sha")
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
    agent_command="${commands[$i]}"
    if [[ -n "$mission_id" && "${providers[$i]}" == "openai" \
      && "${authorities[$i]}" != "write" && "${authorities[$i]}" != "control" ]]; then
      if [[ "$agent_command" != *" --sandbox read-only"* ]]; then
        echo "Mission-bound OpenAI specialist ${instance_ids[$i]} lacks the expected read-only sandbox contract." >&2
        exit 2
      fi
      # Permission profiles do not compose with --sandbox. Replace the legacy
      # read-only flag with an equivalent read-only profile that additionally
      # permits exactly this mission's authenticated AF_UNIX control socket.
      agent_command="${agent_command/ --sandbox read-only/}"
      codex_control_policy="$(codex_control_permission_profile "$control_socket")"
      codex_project_root="${target_repo:-$repo_root}"
      codex_project_policy="$(python3 -c 'import json, sys; print(f"projects={{{json.dumps(sys.argv[1])}={{trust_level=\"untrusted\"}}}}")' "$codex_project_root")"
      agent_command="$agent_command$(printf ' -c %q -c %q -c %q' \
        "$codex_control_policy" 'default_permissions="fleet_control"' "$codex_project_policy")"
      # run-interactive-agent provisions only controller-vetted CMUX hooks in
      # an ephemeral Codex home. Skip the per-run trust UI so that automation
      # cannot have its first tracked prompt consumed by the hook browser.
      agent_command="$agent_command --dangerously-bypass-hook-trust"
    fi
    launch_command="$(interactive_launch_command "${role_types[$i]}" "${authorities[$i]}" "${required_envs[$i]}" "$agent_command")"
    if [[ -n "${worktrees[$i]:-}" ]]; then
      # A linked worktree keeps its Git admin dir and objects in the target
      # repo's common .git, outside the Codex workspace-write sandbox. Writer
      # commits are the designed durable output, so grant exactly that dir.
      if [[ "${providers[$i]}" == "openai" ]]; then
        writer_git_dir="$(git -C "$target_repo" rev-parse --path-format=absolute --git-common-dir)"
        writer_git_dir_json="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$writer_git_dir")"
        codex_project_policy="$(python3 -c 'import json, sys; print(f"projects={{{json.dumps(sys.argv[1])}={{trust_level=\"untrusted\"}}}}")' "$target_repo")"
        launch_command="$launch_command$(printf ' -c %q' "sandbox_workspace_write.writable_roots=[$writer_git_dir_json]")"
        launch_command="$launch_command$(printf ' -c %q' "$codex_project_policy")"
      fi
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
  echo "manifest_contract_version=2"
  echo "execution_profile=$execution_profile"
  echo "tracking_protocol=control-v1"
  echo "feature=$feature"
  echo "preset=$resolved_preset"
  echo "mode=$execution_mode"
  [[ -z "$mission_id" ]] || echo "mission_id=$mission_id"
  [[ -z "$control_socket" ]] || echo "control_socket=$control_socket"
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
  echo "lead.model=$lead_model"
  echo "lead.hook_source=$lead_hook_source"
  [[ -z "$lead_variant" ]] || echo "lead.variant=$lead_variant"
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
    echo "${instance_ids[$i]}.model=${models[$i]}"
    echo "${instance_ids[$i]}.hook_source=${hook_sources[$i]}"
    [[ -z "${variants[$i]}" ]] || echo "${instance_ids[$i]}.variant=${variants[$i]}"
    if [[ -n "${worktrees[$i]:-}" ]]; then
      echo "${instance_ids[$i]}.worktree=${worktrees[$i]}"
      echo "${instance_ids[$i]}.branch=${worktree_branches[$i]}"
      echo "${instance_ids[$i]}.base_sha=${worktree_base_shas[$i]}"
      echo "${instance_ids[$i]}.final_sha=${worktree_base_shas[$i]}"
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

  if [[ "$execution_mode" == "autonomous" ]]; then
    # The first submitted lead turn must be the tracked mission from fleet-run.
    # A boot-time orientation turn can still be active (or trigger a CLI update)
    # when the mission arrives, making UserPromptSubmit ownership ambiguous.
    cmux set-status mission ready --workspace "$ws_ref" --icon sparkles >/dev/null || true
    cmux read-screen --surface "$lead_surface" --workspace "$ws_ref" --lines 8 >/dev/null
  else
    cmux send --surface "$lead_surface" --workspace "$ws_ref" \
      "Eres el lead ($lead_role_type) de fleet-$feature en $ws_ref. El preset resuelto es '$resolved_preset' y su modo es '$execution_mode'. Lee $manifest y verifica UUIDs/surfaces contra 'cmux tree --workspace $ws_ref --id-format both'. Los panes están ordenados por capacidad. No despaches todavía: reporta el roster y el modo en una línea, y espera la misión." >/dev/null
    [[ "${FLEET_SEND_KEY_DELAY:-0.2}" == "0" ]] || sleep "${FLEET_SEND_KEY_DELAY:-0.2}"
    cmux send-key --surface "$lead_surface" --workspace "$ws_ref" enter >/dev/null
    cmux read-screen --surface "$lead_surface" --workspace "$ws_ref" --lines 8 >/dev/null
  fi
fi

completed=1
echo "fleet-$feature is up (preset: $resolved_preset, mode: $execution_mode, lead: ${lead_role_type:-monitor})."
echo "manifest: $manifest"
cat "$manifest"
