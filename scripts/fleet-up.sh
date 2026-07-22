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
clone_guard="$repo_root/scripts/fleet_clone_guard.py"
manifest_guard="$repo_root/scripts/fleet_manifest_guard.py"
cmux_launcher="$repo_root/scripts/fleet_cmux_launcher.py"
ledger_bootstrap="$repo_root/scripts/fleet_ledger_bootstrap.py"
runs_dir="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
worktrees_root="${FLEET_WORKTREES_DIR:-/tmp/fleet_workspaces-$(id -u)}"
mission_id="${FLEET_MISSION_ID:-}"
execution_profile="${FLEET_EXECUTION_PROFILE:-native}"
compiled_workflow=""
expected_base_sha=""
control_socket=""
instance_control_sockets=()
endpoint_instances=()
export CMUX_QUIET=1
unset FLEET_COMPILED_WORKFLOW FLEET_COMPILED_DIGEST

sanitized_git() {
  /usr/bin/env \
    -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR -u GIT_OBJECT_DIRECTORY \
    -u GIT_ALTERNATE_OBJECT_DIRECTORIES -u GIT_INDEX_FILE -u GIT_NAMESPACE \
    -u GIT_CONFIG_COUNT -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_SYSTEM \
    -u GIT_CONFIG_GLOBAL \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_OPTIONAL_LOCKS=0 \
    git -c core.fsmonitor=false -c core.hooksPath=/dev/null \
      -c submodule.recurse=false "$@"
}

boot_lock_pid=""
boot_lock_held=0

release_fleet_boot_lock() {
  if (( boot_lock_held == 1 )); then
    exec 9>&- || true
    if [[ -n "$boot_lock_pid" ]]; then
      wait "$boot_lock_pid" 2>/dev/null || true
    fi
    boot_lock_held=0
    boot_lock_pid=""
  fi
}

acquire_fleet_boot_lock() {
  local channel_dir control_fifo ready_fifo readiness
  channel_dir="$(mktemp -d /tmp/fleet-boot-lock.XXXXXX)" || return 1
  control_fifo="$channel_dir/control"
  ready_fifo="$channel_dir/ready"
  if ! mkfifo "$control_fifo" "$ready_fifo"; then
    rmdir "$channel_dir" 2>/dev/null || true
    return 1
  fi
  python3 "$clone_guard" boot-lock --runs-dir "$runs_dir" --feature "$feature" \
    < "$control_fifo" > "$ready_fifo" &
  boot_lock_pid=$!
  exec 9>"$control_fifo"
  if ! IFS= read -r readiness < "$ready_fifo" || [[ "$readiness" != "READY" ]]; then
    exec 9>&- || true
    wait "$boot_lock_pid" 2>/dev/null || true
    rm -f "$control_fifo" "$ready_fifo"
    rmdir "$channel_dir" 2>/dev/null || true
    boot_lock_pid=""
    return 1
  fi
  rm -f "$control_fifo" "$ready_fifo"
  rmdir "$channel_dir" 2>/dev/null || true
  boot_lock_held=1
}

if [[ -n "$mission_id" && ! "$mission_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; then
  echo "Invalid FLEET_MISSION_ID: expected canonical lowercase UUID." >&2
  exit 2
fi

usage() {
  cat >&2 <<'EOF'
Usage:
  fleet-up.sh <feature> [--preset <name>] [--target-repo <path>] [--expected-base-sha <sha>]
  fleet-up.sh <feature> [--lead-provider <role>] [--allow-fallback] [instance=role ...]
  fleet-up.sh --list-presets

--target-repo <path>: git repository the fleet works on. Every instance with
write authority gets a dedicated fleet/<feature>/<instance> branch and worktree
there, so committed output remains reachable after teardown.
--execution-profile <name>: native (default), sandboxed, or regulated. Profiles
change the process perimeter; router-declared tool capabilities remain intact.
--compiled-workflow <path>: bind the pre-effect router plan and published
manifest to one immutable compiled workflow.
--expected-base-sha <sha>: require the target HEAD to match a frozen mission
baseline before any CMUX mutation; mandatory for mission-bound boots.
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
    --compiled-workflow)
      [[ $# -ge 2 ]] || { echo "--compiled-workflow requires a path" >&2; exit 2; }
      compiled_workflow="$2"
      shift 2
      ;;
    --expected-base-sha)
      [[ $# -ge 2 ]] || { echo "--expected-base-sha requires a full Git object id" >&2; exit 2; }
      expected_base_sha="$2"
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
if [[ -n "$mission_id" && -z "$compiled_workflow" ]]; then
  echo "mission-bound manifest v3 requires --compiled-workflow; restart through canonical Mission boot for the v3 cutover" >&2
  exit 2
fi
if [[ -n "$mission_id" && -z "$expected_base_sha" ]]; then
  echo "mission-bound fleet boot requires --expected-base-sha" >&2
  exit 2
fi
if [[ -n "$expected_base_sha" && ! "$expected_base_sha" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]]; then
  echo "--expected-base-sha must be a full lowercase Git object id" >&2
  exit 2
fi
if [[ -n "$compiled_workflow" ]]; then
  if ! compiled_workflow="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve(strict=True))' "$compiled_workflow")"; then
    echo "--compiled-workflow is unavailable" >&2
    exit 2
  fi
  export FLEET_COMPILED_WORKFLOW="$compiled_workflow"
fi

if [[ -n "$target_repo" ]]; then
  if ! target_physical="$(cd "$target_repo" 2>/dev/null && pwd -P)"; then
    echo "--target-repo is not a git repository: $target_repo" >&2
    exit 2
  fi
  if ! worktrees_root="$(python3 "$clone_guard" ensure-root \
      --worktrees-root "$worktrees_root" --protected "$repo_root" \
      --protected "$runs_dir" --protected "$target_physical")"; then
    echo "Refusing unsafe worktree root: $worktrees_root" >&2
    exit 2
  fi
  if ! sanitized_git -C "$target_repo" rev-parse --git-dir >/dev/null 2>&1; then
    echo "--target-repo is not a git repository: $target_repo" >&2
    exit 2
  fi
  target_repo="$(cd "$target_repo" && pwd -P)"
  target_toplevel="$(sanitized_git -C "$target_repo" rev-parse --path-format=absolute --show-toplevel 2>/dev/null || true)"
  if [[ -z "$target_toplevel" ]]; then
    echo "Could not resolve the physical Git toplevel for --target-repo." >&2
    exit 2
  fi
  target_toplevel="$(cd "$target_toplevel" && pwd -P)"
  if [[ "$target_repo" != "$target_toplevel" ]]; then
    echo "--target-repo must be the exact physical Git toplevel: $target_toplevel" >&2
    exit 2
  fi
  target_repo="$target_toplevel"
  if [[ -n "$expected_base_sha" ]]; then
    target_head="$(sanitized_git -C "$target_repo" rev-parse --verify HEAD 2>/dev/null || true)"
    if [[ "$target_head" != "$expected_base_sha" ]]; then
      echo "Target HEAD drifted from --expected-base-sha before fleet boot." >&2
      exit 2
    fi
  fi
elif [[ -n "$expected_base_sha" ]]; then
  echo "--expected-base-sha requires --target-repo" >&2
  exit 2
fi

plan_args=(plan --format records)
[[ -n "$compiled_workflow" ]] && plan_args+=(--compiled-workflow "$compiled_workflow")
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
compiled_digest=""
router_digest=""
roster_digest=""
launch_digest=""
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
identity_groups=()
warnings=()

while IFS=$'\x1f' read -r record a b c d e f g h i j k l m n o p; do
  case "$record" in
    META)
      schema_version="$a"
      resolved_preset="$b"
      execution_mode="${c:-guided}"
      ;;
    BINDING)
      compiled_digest="$a"
      router_digest="$b"
      roster_digest="$c"
      launch_digest="$d"
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
    IDENTITY_GROUP)
      identity_groups+=("$b")
      ;;
    WARNING)
      warnings+=("$a")
      ;;
  esac
done <<< "$plan_output"

[[ -n "$schema_version" ]] || { echo "Router returned no plan metadata." >&2; exit 2; }
if [[ -n "$compiled_workflow" ]]; then
  [[ "$compiled_digest" =~ ^[0-9a-f]{64}$ ]] || {
    echo "Compiled router plan returned no valid compiled digest." >&2
    exit 2
  }
  export FLEET_COMPILED_DIGEST="$compiled_digest"
fi
if (( ${#warnings[@]} > 0 )); then
  for warning in "${warnings[@]}"; do
    echo "WARNING: $warning" >&2
  done
fi

if [[ -n "$mission_id" ]]; then
  control_records="$(PYTHONPATH="$repo_root/scripts" python3 - \
    "$runs_dir" "$mission_id" "$resolved_preset" "${instance_ids[@]}" <<'PY'
from pathlib import Path
import sys

from fleet_control_service import ControlLifecycle
import fleet_mission_state as mission_state

runs_dir = Path(sys.argv[1]).expanduser().resolve()
mission_id = mission_state.normalize_uuid(sys.argv[2], "mission_id")
preset = sys.argv[3]
expected_instances = sys.argv[4:]
lifecycle = ControlLifecycle(runs_dir, mission_id, preset=preset)
health = lifecycle.health()
mapping = lifecycle.instance_socket_paths
if set(mapping) != set(expected_instances):
    raise SystemExit("mission control lifecycle instance endpoint mapping drift")
if health.get("mission_id") != mission_id or health.get("preset") != preset:
    raise SystemExit("mission control health endpoint binding mismatch")
print(f"BASE\x1f\x1f{lifecycle.socket_path}")
for instance in expected_instances:
    print(f"INSTANCE\x1f{instance}\x1f{mapping[instance]}")
PY
  )" || { echo "Mission-bound Fleet Control endpoints are unavailable." >&2; exit 2; }
  while IFS=$'\x1f' read -r record instance endpoint; do
    case "$record" in
      BASE) control_socket="$endpoint" ;;
      INSTANCE)
        endpoint_instances+=("$instance")
        instance_control_sockets+=("$endpoint")
        ;;
      *) echo "Invalid Fleet Control endpoint record." >&2; exit 2 ;;
    esac
  done <<< "$control_records"
  [[ -n "$control_socket" ]] || { echo "Mission-bound Fleet Control base endpoint is missing." >&2; exit 2; }
  for ((i=0; i<${#instance_ids[@]}; i++)); do
    if [[ "${endpoint_instances[$i]:-}" != "${instance_ids[$i]}" \
      || -z "${instance_control_sockets[$i]:-}" ]]; then
      echo "Mission-bound Fleet Control endpoint is missing for ${instance_ids[$i]}." >&2
      exit 2
    fi
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
  local role_type="$1" authority="$2" required_csv="$3" label="$4" endpoint="$5"
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
    FLEET_RUNS_DIR="$runs_dir" \
    FLEET_CONTROL_SOCKET="$endpoint" \
    "$repo_root/scripts/run-interactive-agent.sh" \
      "$role_type" "$authority" "${required_csv:--}" "${healthcheck[@]}" \
      >/dev/null; then
    echo "Interactive role '$label' failed its isolated runtime healthcheck." >&2
    return 1
  fi
}

interactive_agent_command() {
  local role_type="$1" authority="$2" required_csv="$3" command_shell="$4"
  [[ -n "$required_csv" ]] || required_csv="-"
  printf '%q %q %q %q %s' "$repo_root/scripts/run-interactive-agent.sh" \
    "$role_type" "$authority" "$required_csv" "$command_shell"
}

codex_control_permission_profile() {
  local socket_path="$1" authority="$2" controller_home="$3" fleet_runs_dir="$4"
  python3 -c '
import json
import os
import sys

socket = json.dumps(sys.argv[1])
writer = sys.argv[2] == "write"
denied = []
for path in sys.argv[3:5]:
    lexical = os.path.abspath(path)
    canonical = os.path.realpath(lexical)
    for candidate in (lexical, canonical):
        if candidate not in denied:
            denied.append(candidate)
profile = "fleet_writer" if writer else "fleet_reader"
workspace_access = "write" if writer else "read"
filesystem = (
    "filesystem={\":minimal\"=\"read\","
    + "\":workspace_roots\"={\".\"=\"" + workspace_access + "\"},"
    + ",".join(json.dumps(path) + "=\"deny\"" for path in denied)
    + "},"
)
print(
    "permissions={" + profile + "={description=\"Mission-scoped Fleet Control client with CONTROL home denied.\","
    + filesystem + "network={enabled=true,mode=\"limited\","
    + "unix_sockets={" + socket + "=\"allow\"}}}}"
)
' "$socket_path" "$authority" "$controller_home" "$fleet_runs_dir"
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

wait_for_launch_acceptance() {
  local label="$1" launch_id="$2" spec_sha256="$3"
  local attempts="${FLEET_LAUNCH_RECEIPT_ATTEMPTS:-15}"
  local delay="${FLEET_LAUNCH_RECEIPT_DELAY:-0.1}"
  local attempt receipt_rc
  for ((attempt=1; attempt<=attempts; attempt++)); do
    if python3 "$cmux_launcher" verify "$runs_dir" "$launch_id" \
        "$spec_sha256" >/dev/null; then
      return 0
    else
      receipt_rc=$?
    fi
    if (( receipt_rc != 1 )); then
      echo "Interactive agent '$label' produced an invalid launch receipt." >&2
      return 1
    fi
    [[ "$delay" == "0" ]] || sleep "$delay"
  done
  echo "Interactive agent '$label' did not accept its durable launch spec." >&2
  return 1
}

launch_interactive_surface() {
  local surface="$1" label="$2" cwd="$3" role_type="$4" authority="$5"
  local required_csv="$6" endpoint="$7" command_shell="$8"
  local ready_pattern="$9" executable="${10}"
  local agent_command descriptor launch_id spec_sha256 launcher_runs launcher_command
  local -a compiled_environment=()
  agent_command="$(interactive_agent_command \
    "$role_type" "$authority" "$required_csv" "$command_shell")"
  if [[ -n "$compiled_workflow" ]]; then
    compiled_environment=(
      --env "FLEET_COMPILED_WORKFLOW=$compiled_workflow"
      --env "FLEET_COMPILED_DIGEST=$compiled_digest"
    )
  fi
  descriptor="$(python3 "$cmux_launcher" create \
    --runs-dir "$runs_dir" --label "$label" --cwd "$cwd" \
    --env "FLEET_EXECUTION_PROFILE=$execution_profile" \
    --env "FLEET_MISSION_ID=$mission_id" \
    --env "FLEET_RUNS_DIR=$runs_dir" \
    --env "FLEET_CONTROL_SOCKET=$endpoint" \
    ${compiled_environment[@]+"${compiled_environment[@]}"} \
    --command-shell "$agent_command")" || return 1
  IFS=$'\x1f' read -r launch_id spec_sha256 <<< "$descriptor"
  if [[ ! "$launch_id" =~ ^[0-9a-f-]{36}$ \
    || ! "$spec_sha256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Interactive agent '$label' returned an invalid launch descriptor." >&2
    return 1
  fi
  launcher_runs="$(cd "$runs_dir" && pwd -P)" || return 1
  if [[ "$launcher_runs" == "$repo_root/"* ]]; then
    launcher_runs="${launcher_runs#"$repo_root/"}"
  fi
  launcher_command="$(printf 'python3 scripts/fleet_cmux_launcher.py run %q %q %q' \
    "$launcher_runs" "$launch_id" "$spec_sha256")"
  if (( ${#launcher_command} > ${FLEET_CMUX_LAUNCH_MAX_BYTES:-384} )); then
    echo "Interactive agent '$label' resolved an unsafe long CMUX launcher." >&2
    return 1
  fi
  cmux send --surface "$surface" --workspace "$ws_ref" \
    "$launcher_command" >/dev/null
  cmux send-key --surface "$surface" --workspace "$ws_ref" enter >/dev/null
  wait_for_launch_acceptance "$label" "$launch_id" "$spec_sha256" || return 1
  wait_for_agent_prompt "$surface" "$label" "$ready_pattern" "$executable"
}

cmux_send_line() {
  local surface="$1" payload="$2"
  cmux send --surface "$surface" --workspace "$ws_ref" "$payload" >/dev/null
  cmux send-key --surface "$surface" --workspace "$ws_ref" enter >/dev/null
}

if [[ "${FLEET_NO_LEAD:-0}" != "1" ]]; then
  [[ -n "$lead_executable" ]] || { echo "Router did not resolve a lead provider." >&2; exit 2; }
  command -v "$lead_executable" >/dev/null 2>&1 || {
    echo "Lead executable not found: $lead_executable" >&2
    exit 2
  }
  check_required_env "$lead_requires_env" || exit 2
  preflight_interactive_role "$lead_role_type" control "$lead_requires_env" \
    "lead/$lead_role_type" "$control_socket" || exit 2
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
      "${instance_ids[$i]}/${role_types[$i]}" \
      "${instance_control_sockets[$i]:-}" || exit 2
  else
    command -v ollama >/dev/null 2>&1 || { echo "ollama is required for ${instance_ids[$i]}" >&2; exit 2; }
    ollama show "${models[$i]}" >/dev/null 2>&1 || {
      echo "Local model not installed for ${instance_ids[$i]}: ${models[$i]}" >&2
      exit 2
    }
  fi
done

mkdir -p "$runs_dir"
if ! acquire_fleet_boot_lock; then
  echo "Another fleet-up owns boot admission for '$feature'; refusing concurrent boot." >&2
  exit 2
fi
trap release_fleet_boot_lock EXIT

manifest="$runs_dir/fleet-$feature.manifest"
lifecycle_ledger="$runs_dir/fleet-$feature.ledger.jsonl"
if [[ -e "$manifest" || -L "$manifest" ]]; then
  echo "Manifest already exists: $manifest" >&2
  echo "Close or reconcile the existing fleet before reusing '$feature'." >&2
  exit 2
fi
set +e
python3 "$manifest_guard" recover-archived --runs-dir "$runs_dir" \
  --feature "$feature" >/dev/null 2>&1
archive_recovery_rc=$?
set -e
if (( archive_recovery_rc == 0 )); then
  echo "A prior teardown archive is pending recovery for '$feature'; run fleet-down before booting again." >&2
  exit 2
fi
if (( archive_recovery_rc != 1 )); then
  echo "Invalid teardown recovery state blocks fleet boot for '$feature'." >&2
  exit 75
fi

# Resolve writer branches before any cmux mutation. The public target ref must
# remain absent while the writer is live: the writer works in an object-isolated
# clone and CONTROL publishes that ref only after the workspace is quiescent.
worktree_branches=()
worktree_base_shas=()
if [[ -n "$target_repo" ]]; then
  target_head="$(sanitized_git -C "$target_repo" rev-parse --verify HEAD)"
  if [[ -n "$expected_base_sha" && "$target_head" != "$expected_base_sha" ]]; then
    echo "Target HEAD drifted from --expected-base-sha before CMUX mutation." >&2
    exit 2
  fi
  for ((i=0; i<${#instance_ids[@]}; i++)); do
    worktree_branches[$i]=""
    worktree_base_shas[$i]=""
    if [[ "${authorities[$i]}" == "write" ]]; then
      branch="fleet/$feature/${instance_ids[$i]}"
      if ! sanitized_git -C "$target_repo" check-ref-format --branch "$branch" >/dev/null 2>&1; then
        echo "Writer branch name is invalid: $branch" >&2
        exit 2
      fi
      if sanitized_git -C "$target_repo" show-ref --verify --quiet "refs/heads/$branch"; then
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
workspace_uuid=""
boot_cleanup_id=""
manifest_tmp=""
lifecycle_ledger_initialized=0
created_worktrees=()
created_worktree_instances=()
created_worktree_branches=()
created_worktree_bases=()
created_reader_workspaces=()
created_reader_instances=()
created_reader_bases=()
if [[ -n "$target_repo" ]]; then
  boot_cleanup_id="$(python3 -c '
import sys, uuid
print(str(uuid.uuid5(uuid.NAMESPACE_URL, "fleet-failed-boot:" + "\x1f".join(sys.argv[1:]))).upper())
' "$worktrees_root" "$target_repo" "$feature")"
fi

failed_boot_stage_path() {
  local instance="$1" cleanup_id="$2"
  printf '%s/.fleet-control-staging/%s--%s--%s\n' \
    "$worktrees_root" "$feature" "$instance" "$cleanup_id"
}

failed_boot_intent_path() {
  local instance="$1" cleanup_id="$2" suffix="$3"
  printf '%s/.fleet-%s.%s.%s.%s\n' \
    "$runs_dir" "$feature" "$instance" "$cleanup_id" "$suffix"
}

failed_boot_pending_prefix() {
  local instance="$1" cleanup_id="$2" suffix="$3" leaf digest
  leaf=".fleet-$feature.$instance.$cleanup_id.$suffix"
  digest="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$leaf")"
  printf '%s/.fleet-atomic-%s-\n' "$runs_dir" "$digest"
}

failed_boot_creation() {
  local mode="$1" kind="$2" instance="$3" expected_sha="$4" branch="$5" cleanup_id="$6"
  python3 "$clone_guard" creation "$mode" --runs-dir "$runs_dir" \
    --worktrees-root "$worktrees_root" --target-repo "$target_repo" \
    --feature "$feature" --instance "$instance" --workspace-uuid "$cleanup_id" \
    --kind "$kind" --expected-sha "$expected_sha" --branch "$branch"
}

failed_boot_bind_creation() {
  local kind="$1" instance="$2" expected_sha="$3" branch="$4" cleanup_id="$5"
  if [[ -n "${FLEET_TEST_CREATION_BINDING_SAFE_CRASH_AT:-}" ]]; then
    FLEET_TEST_SAFE_PATH_CRASH_AT="$FLEET_TEST_CREATION_BINDING_SAFE_CRASH_AT" \
      failed_boot_creation bind "$kind" "$instance" "$expected_sha" \
        "$branch" "$cleanup_id"
  else
    failed_boot_creation bind "$kind" "$instance" "$expected_sha" \
      "$branch" "$cleanup_id"
  fi
}

failed_boot_creation_empty() {
  local kind="$1" instance="$2" expected_sha="$3" branch="$4" cleanup_id="$5"
  python3 "$clone_guard" creation-empty --runs-dir "$runs_dir" \
    --worktrees-root "$worktrees_root" --target-repo "$target_repo" \
    --feature "$feature" --instance "$instance" --workspace-uuid "$cleanup_id" \
    --kind "$kind" --expected-sha "$expected_sha" --branch "$branch"
}

failed_boot_creation_stage_kind() {
  local kind="$1" instance="$2" expected_sha="$3" branch="$4" cleanup_id="$5"
  python3 "$clone_guard" creation-stage-kind --runs-dir "$runs_dir" \
    --worktrees-root "$worktrees_root" --target-repo "$target_repo" \
    --feature "$feature" --instance "$instance" --workspace-uuid "$cleanup_id" \
    --kind "$kind" --expected-sha "$expected_sha" --branch "$branch"
}

fleet_boot_checkpoint() {
  local checkpoint="$1"
  local configured="${FLEET_TEST_BOOT_CRASH_AT:-${FLEET_TEST_PUBLICATION_CRASH_AT:-}}"
  if [[ "$configured" == "$checkpoint" ]]; then
    kill -KILL "$$"
  fi
  if [[ "${FLEET_TEST_BOOT_PAUSE_AT:-}" == "$checkpoint" ]]; then
    if [[ -z "${FLEET_TEST_BOOT_RESUME_FILE:-}" ]]; then
      echo "FLEET_TEST_BOOT_RESUME_FILE is required for a boot pause." >&2
      return 1
    fi
    while [[ ! -e "$FLEET_TEST_BOOT_RESUME_FILE" ]]; do
      sleep 0.05
    done
  fi
}

failed_boot_stage_clone() {
  local kind="$1" instance="$2" expected_sha="$3" branch="$4" cleanup_id="$5"
  python3 "$clone_guard" stage --runs-dir "$runs_dir" \
    --worktrees-root "$worktrees_root" --feature "$feature" \
    --instance "$instance" --workspace-uuid "$cleanup_id" --kind "$kind" \
    --expected-sha "$expected_sha" --branch "$branch" >/dev/null
}

failed_boot_tombstone() {
  local mode="$1" kind="$2" instance="$3" expected_sha="$4" branch="$5" cleanup_id="$6"
  python3 "$clone_guard" tombstone "$mode" --runs-dir "$runs_dir" \
    --worktrees-root "$worktrees_root" --feature "$feature" \
    --instance "$instance" --workspace-uuid "$cleanup_id" --kind "$kind" \
    --expected-sha "$expected_sha" --branch "$branch" >/dev/null
}

failed_boot_clear_stage() {
  local kind="$1" instance="$2" expected_sha="$3" branch="$4" cleanup_id="$5"
  python3 "$clone_guard" clear-stage --runs-dir "$runs_dir" \
    --worktrees-root "$worktrees_root" --feature "$feature" \
    --instance "$instance" --workspace-uuid "$cleanup_id" --kind "$kind" \
    --expected-sha "$expected_sha" --branch "$branch" >/dev/null
}

retire_failed_boot_clone() {
  local kind="$1" instance="$2" expected_sha="$3" branch="$4" cleanup_id="$5"
  local staged tombstone
  staged="$(failed_boot_stage_path "$instance" "$cleanup_id")"
  tombstone="$(failed_boot_intent_path \
    "$instance" "$cleanup_id" "clone-retirement-tombstone.json")"
  if [[ ! -e "$tombstone" && ! -L "$tombstone" ]]; then
    failed_boot_tombstone ensure "$kind" "$instance" "$expected_sha" "$branch" "$cleanup_id" || return 1
  fi
  failed_boot_tombstone remove "$kind" "$instance" "$expected_sha" "$branch" "$cleanup_id" || return 1
  [[ ! -e "$staged" && ! -L "$staged" ]] || return 1
  failed_boot_tombstone clear "$kind" "$instance" "$expected_sha" "$branch" "$cleanup_id" || return 1
  failed_boot_clear_stage "$kind" "$instance" "$expected_sha" "$branch" "$cleanup_id"
}

reconcile_failed_boot_journal() {
  local kind="$1" instance="$2" expected_sha="$3" branch="$4"
  local cleanup_id="$boot_cleanup_id" source staged creation_intent creation_binding
  local stage_intent tombstone creation_pending_prefix binding_pending_prefix
  local stage_pending_prefix location stage_kind stage_branch
  local head check_rc stage_kind_rc empty_rc
  source="$worktrees_root/$feature-$instance"
  staged="$(failed_boot_stage_path "$instance" "$cleanup_id")"
  creation_intent="$(failed_boot_intent_path \
    "$instance" "$cleanup_id" "clone-creation-intent.json")"
  creation_binding="$(failed_boot_intent_path \
    "$instance" "$cleanup_id" "clone-creation-binding.json")"
  stage_intent="$(failed_boot_intent_path \
    "$instance" "$cleanup_id" "clone-stage-intent.json")"
  tombstone="$(failed_boot_intent_path \
    "$instance" "$cleanup_id" "clone-retirement-tombstone.json")"
  creation_pending_prefix="$(failed_boot_pending_prefix \
    "$instance" "$cleanup_id" "clone-creation-intent.json")"
  binding_pending_prefix="$(failed_boot_pending_prefix \
    "$instance" "$cleanup_id" "clone-creation-binding.json")"
  stage_pending_prefix="$(failed_boot_pending_prefix \
    "$instance" "$cleanup_id" "clone-stage-intent.json")"

  if [[ -e "$manifest" || -L "$manifest" ]]; then
    echo "Active fleet manifest prevents failed-boot clone reconciliation: $manifest" >&2
    return 1
  fi

  if [[ ! -e "$creation_intent" && ! -L "$creation_intent" \
      && -n "$(compgen -G "${creation_pending_prefix}*.tmp" || true)" \
      && ! -e "$source" && ! -L "$source" \
      && ! -e "$staged" && ! -L "$staged" ]]; then
    if ! failed_boot_creation plan "$kind" "$instance" "$expected_sha" \
        "$branch" "$cleanup_id" >/dev/null; then
      echo "An incomplete clone creation plan is preserved for retry." >&2
      return 1
    fi
  fi

  if [[ ( -e "$source" || -L "$source" ) \
      && ! -e "$creation_intent" && ! -L "$creation_intent" ]]; then
    echo "Clone source exists without its exact creation plan; refusing adoption: $source" >&2
    return 1
  fi

  if [[ ( -e "$creation_binding" || -L "$creation_binding" ) \
      && ! -e "$creation_intent" && ! -L "$creation_intent" ]]; then
    if [[ ! -e "$source" && ! -L "$source" \
        && ! -e "$staged" && ! -L "$staged" \
        && ! -e "$stage_intent" && ! -L "$stage_intent" \
        && ! -e "$tombstone" && ! -L "$tombstone" ]]; then
      failed_boot_creation clear "$kind" "$instance" "$expected_sha" \
        "$branch" "$cleanup_id" >/dev/null || return 1
      return 0
    fi
    echo "Clone creation binding exists without its exact plan; refusing recovery." >&2
    return 1
  fi

  if [[ -e "$creation_intent" || -L "$creation_intent" ]]; then
    if [[ ! -e "$creation_binding" && ! -L "$creation_binding" ]]; then
      if [[ ! -e "$source" && ! -L "$source" \
          && ! -e "$staged" && ! -L "$staged" \
          && ! -e "$stage_intent" && ! -L "$stage_intent" \
          && ! -e "$tombstone" && ! -L "$tombstone" \
          && -z "$(compgen -G "${binding_pending_prefix}*.tmp" || true)" ]]; then
        failed_boot_creation clear "$kind" "$instance" "$expected_sha" \
          "$branch" "$cleanup_id" >/dev/null || return 1
        return 0
      fi
      if [[ -e "$staged" || -L "$staged" \
          || -e "$stage_intent" || -L "$stage_intent" \
          || -e "$tombstone" || -L "$tombstone" ]]; then
        echo "Unbound clone creation plan has unexpected staging state; preserving it." >&2
        return 1
      fi
      if ! failed_boot_creation source "$kind" "$instance" "$expected_sha" \
          "$branch" "$cleanup_id" >/dev/null; then
        echo "Planned clone source is not safely bindable; preserving it: $source" >&2
        return 1
      fi
      if ! failed_boot_creation bind "$kind" "$instance" "$expected_sha" \
          "$branch" "$cleanup_id" >/dev/null; then
        echo "Planned clone source could not be durably bound; preserving it: $source" >&2
        return 1
      fi
    fi
    set +e
    location="$(failed_boot_creation require "$kind" "$instance" \
      "$expected_sha" "$branch" "$cleanup_id" 2>/dev/null)"
    check_rc=$?
    set -e
    if (( check_rc != 0 )); then
      echo "A prior clone creation intent drifted; preserving all clone state: $creation_intent" >&2
      return 1
    fi

    set +e
    stage_kind="$(failed_boot_creation_stage_kind "$kind" "$instance" \
      "$expected_sha" "$branch" "$cleanup_id" 2>/dev/null)"
    stage_kind_rc=$?
    set -e
    if (( stage_kind_rc != 0 && stage_kind_rc != 1 )); then
      echo "A creation-bound stage intent drifted; preserving all clone state: $staged" >&2
      return 1
    fi
    if (( stage_kind_rc == 1 )); then
      stage_kind=""
    fi

    if [[ "$location" == "absent" ]]; then
      if [[ -e "$tombstone" || -L "$tombstone" ]]; then
        [[ -n "$stage_kind" ]] || {
          echo "A retirement tombstone has no exact creation-bound stage intent." >&2
          return 1
        }
        stage_branch="$branch"
        [[ "$stage_kind" != "partial" ]] || stage_branch=-
        retire_failed_boot_clone "$stage_kind" "$instance" "$expected_sha" \
          "$stage_branch" "$cleanup_id" || return 1
      elif [[ -n "$stage_kind" ]]; then
        stage_branch="$branch"
        [[ "$stage_kind" != "partial" ]] || stage_branch=-
        failed_boot_clear_stage "$stage_kind" "$instance" "$expected_sha" \
          "$stage_branch" "$cleanup_id" || return 1
      fi
      failed_boot_creation clear "$kind" "$instance" "$expected_sha" \
        "$branch" "$cleanup_id" >/dev/null || return 1
      return 0
    fi

    if [[ "$location" == "staged" && -z "$stage_kind" ]]; then
      echo "A creation-bound staged clone has no exact stage intent: $staged" >&2
      return 1
    fi

    if [[ -z "$stage_kind" ]]; then
      set +e
      failed_boot_creation_empty "$kind" "$instance" "$expected_sha" \
        "$branch" "$cleanup_id" >/dev/null 2>&1
      empty_rc=$?
      set -e
      if (( empty_rc != 0 && empty_rc != 1 )); then
        echo "Could not inspect the exact creation-bound clone: $source" >&2
        return 1
      fi
      if [[ "$kind" == "reader" ]]; then
        set +e
        head="$(failed_reader_head "$source" "$expected_sha")"
        check_rc=$?
        set -e
      else
        set +e
        head="$(failed_writer_head "$source" "$branch" "$expected_sha")"
        check_rc=$?
        set -e
      fi
      if (( check_rc == 0 )); then
        stage_kind="$kind"
        stage_branch="$branch"
      else
        stage_kind=partial
        stage_branch=-
      fi
      if ! failed_boot_stage_clone "$stage_kind" "$instance" "$expected_sha" \
          "$stage_branch" "$cleanup_id"; then
        echo "A creation-bound clone could not be durably staged: $source" >&2
        return 1
      fi
      failed_boot_creation require "$kind" "$instance" "$expected_sha" \
        "$branch" "$cleanup_id" >/dev/null || return 1
      location=staged
      if (( empty_rc == 0 )); then
        retire_failed_boot_clone partial "$instance" "$expected_sha" - \
          "$cleanup_id" || return 1
        failed_boot_creation clear "$kind" "$instance" "$expected_sha" \
          "$branch" "$cleanup_id" >/dev/null || return 1
        return 0
      fi
    fi

    stage_branch="$branch"
    [[ "$stage_kind" != "partial" ]] || stage_branch=-
    if [[ -e "$tombstone" || -L "$tombstone" ]]; then
      retire_failed_boot_clone "$stage_kind" "$instance" "$expected_sha" \
        "$stage_branch" "$cleanup_id" || return 1
      failed_boot_creation clear "$kind" "$instance" "$expected_sha" \
        "$branch" "$cleanup_id" >/dev/null || return 1
      return 0
    fi
    if [[ "$stage_kind" == "partial" ]]; then
      echo "A prior partial clone is preserved with exact creation/stage journals: $staged" >&2
      return 1
    fi
    if [[ "$kind" == "reader" ]]; then
      set +e
      head="$(failed_reader_head "$staged" "$expected_sha")"
      check_rc=$?
      set -e
    else
      set +e
      head="$(failed_writer_head "$staged" "$branch" "$expected_sha")"
      check_rc=$?
      set -e
    fi
    if (( check_rc != 0 )); then
      echo "A creation-bound clone is staged but not safely retirable: $staged" >&2
      return 1
    fi
    if [[ "$kind" == "writer" && "$head" != "$expected_sha" ]]; then
      echo "A prior advanced writer is preserved in CONTROL staging with no public ref: $staged" >&2
      return 1
    fi
    retire_failed_boot_clone "$kind" "$instance" "$expected_sha" \
      "$branch" "$cleanup_id" || return 1
    failed_boot_creation clear "$kind" "$instance" "$expected_sha" \
      "$branch" "$cleanup_id" >/dev/null || return 1
    return 0
  fi

  if [[ ! -e "$staged" && ! -L "$staged" \
      && ! -e "$stage_intent" && ! -L "$stage_intent" \
      && ! -e "$tombstone" && ! -L "$tombstone" \
      && -z "$(compgen -G "${stage_pending_prefix}*.tmp" || true)" \
      && -z "$(compgen -G "${creation_pending_prefix}*.tmp" || true)" \
      && -z "$(compgen -G "${binding_pending_prefix}*.tmp" || true)" ]]; then
    return 0
  fi
  if [[ -n "$(compgen -G "${creation_pending_prefix}*.tmp" || true)" ]]; then
    echo "An incomplete clone creation intent is preserved for manual inspection." >&2
    return 1
  fi
  if [[ -n "$(compgen -G "${binding_pending_prefix}*.tmp" || true)" ]]; then
    echo "An incomplete clone creation binding is preserved for manual inspection." >&2
    return 1
  fi
  if [[ ! -e "$tombstone" && ! -L "$tombstone" ]]; then
    if ! failed_boot_stage_clone "$kind" "$instance" "$expected_sha" \
        "$branch" "$cleanup_id"; then
      echo "A prior failed-boot journal is preserved and needs manual inspection: $staged" >&2
      return 1
    fi
    if [[ "$kind" == "reader" ]]; then
      set +e
      head="$(failed_reader_head "$staged" "$expected_sha")"
      check_rc=$?
      set -e
    else
      set +e
      head="$(failed_writer_head "$staged" "$branch" "$expected_sha")"
      check_rc=$?
      set -e
    fi
    if (( check_rc != 0 )); then
      echo "A prior failed-boot clone is journaled but not safely retirable: $staged" >&2
      return 1
    fi
    if [[ "$kind" == "writer" && "$head" != "$expected_sha" ]]; then
      echo "A prior advanced writer is preserved in CONTROL staging with no public ref: $staged" >&2
      return 1
    fi
  fi
  retire_failed_boot_clone "$kind" "$instance" "$expected_sha" \
    "$branch" "$cleanup_id"
}

make_reader_clone_read_only() {
  local clone_path="$1"
  python3 "$clone_guard" verify-device --clone-path "$clone_path" >/dev/null || return 1
  python3 -c '
import os, stat, sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
path = Path(sys.argv[2])
git_dir = path / ".git"
info = path.lstat()
if (
    not stat.S_ISDIR(info.st_mode)
    or stat.S_ISLNK(info.st_mode)
    or info.st_uid != os.geteuid()
    or path.parent.resolve(strict=True) != root
):
    raise SystemExit("unsafe isolated reader clone")
for current, directories, files in os.walk(path, topdown=False, followlinks=False):
    for name in files:
        candidate = Path(current) / name
        candidate_info = candidate.lstat()
        if stat.S_ISLNK(candidate_info.st_mode):
            if candidate.is_relative_to(git_dir):
                raise SystemExit("reader Git metadata contains a symlink")
            continue
        if (
            not stat.S_ISREG(candidate_info.st_mode)
            or candidate_info.st_uid != os.geteuid()
            or candidate_info.st_nlink != 1
        ):
            raise SystemExit("reader clone contains an unsafe filesystem entry")
        os.chmod(candidate, stat.S_IMODE(candidate_info.st_mode) & ~0o222)
    for name in directories:
        candidate = Path(current) / name
        candidate_info = candidate.lstat()
        if stat.S_ISLNK(candidate_info.st_mode):
            if candidate.is_relative_to(git_dir):
                raise SystemExit("reader Git metadata contains a symlink")
            continue
        if (
            not stat.S_ISDIR(candidate_info.st_mode)
            or candidate_info.st_uid != os.geteuid()
        ):
            raise SystemExit("reader clone contains an unsafe directory")
        os.chmod(candidate, stat.S_IMODE(candidate_info.st_mode) & ~0o222)
os.chmod(path, stat.S_IMODE(path.lstat().st_mode) & ~0o222)
' "$worktrees_root" "$clone_path"
}

reader_clone_root_fingerprint() {
  local clone_path="$1"
  python3 -c '
import hashlib, json, os, stat, sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
path = Path(sys.argv[2])
info = path.lstat()
if (
    not stat.S_ISDIR(info.st_mode)
    or stat.S_ISLNK(info.st_mode)
    or info.st_uid != os.geteuid()
    or path.parent.resolve(strict=True) != root
):
    raise SystemExit("unsafe isolated reader clone")
records = []
for entry in sorted(os.scandir(path), key=lambda item: os.fsencode(item.name)):
    item = entry.stat(follow_symlinks=False)
    records.append([
        entry.name,
        item.st_mode,
        item.st_uid,
        item.st_gid,
        item.st_dev,
        item.st_ino,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
    ])
payload = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()
print(hashlib.sha256(payload).hexdigest())
' "$worktrees_root" "$clone_path"
}

make_kimi_reader_root_cli_writable() {
  local clone_path="$1" fingerprint
  python3 "$clone_guard" verify-device --clone-path "$clone_path" >/dev/null || return 1
  fingerprint="$(reader_clone_root_fingerprint "$clone_path")" || return 1
  python3 -c '
import os, stat, sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
path = Path(sys.argv[2])
info = path.lstat()
mode = stat.S_IMODE(info.st_mode)
if (
    not stat.S_ISDIR(info.st_mode)
    or stat.S_ISLNK(info.st_mode)
    or info.st_uid != os.geteuid()
    or path.parent.resolve(strict=True) != root
    or mode & 0o222
):
    raise SystemExit("unsafe Kimi reader launch transition")
os.chmod(path, mode | stat.S_IWUSR)
' "$worktrees_root" "$clone_path" || return 1
  printf '%s\n' "$fingerprint"
}

isolated_git() {
  sanitized_git "$@"
}

failed_writer_head() {
  local clone_path="$1" branch="$2" base_sha="$3"
  local checked_out_branch config_key config_keys final_sha git_common_dir git_dir object_format remote_names top_level
  if [[ ! -d "$clone_path" || -L "$clone_path" || ! -d "$clone_path/.git" || -L "$clone_path/.git" ]]; then
    return 10
  fi
  clone_path="$(cd "$clone_path" && pwd -P)" || return 10
  python3 "$clone_guard" verify-device --clone-path "$clone_path" >/dev/null 2>&1 || return 14
  top_level="$(isolated_git -C "$clone_path" rev-parse --path-format=absolute --show-toplevel 2>/dev/null || true)"
  git_dir="$(isolated_git -C "$clone_path" rev-parse --path-format=absolute --git-dir 2>/dev/null || true)"
  git_common_dir="$(isolated_git -C "$clone_path" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  [[ "$top_level" == "$clone_path" && "$git_dir" == "$clone_path/.git" \
    && "$git_common_dir" == "$clone_path/.git" ]] || return 10
  if ! python3 -c '
import os, stat, sys
from pathlib import Path
git_dir = Path(sys.argv[1])
for root, directories, files in os.walk(git_dir, followlinks=False):
    for name in directories + files:
        path = Path(root) / name
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise SystemExit(1)
        if name in directories:
            if not stat.S_ISDIR(info.st_mode):
                raise SystemExit(1)
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SystemExit(1)
alternates = git_dir / "objects" / "info" / "alternates"
if alternates.exists() or alternates.is_symlink():
    raise SystemExit(1)
hooks = git_dir / "hooks"
if hooks.exists():
    for hook in hooks.iterdir():
        info = hook.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            stat.S_ISREG(info.st_mode)
            and info.st_mode & 0o111
            and not hook.name.endswith(".sample")
        ):
            raise SystemExit(1)
' "$clone_path/.git"; then
    return 14
  fi
  config_keys="$(isolated_git config --no-includes --file "$clone_path/.git/config" \
    --name-only --list 2>/dev/null)" || return 14
  while IFS= read -r config_key; do
    case "$config_key" in
      core.repositoryformatversion|core.filemode|core.bare|core.logallrefupdates|core.ignorecase|core.precomposeunicode|core.symlinks|extensions.objectformat|user.name|user.email) ;;
      *) return 14 ;;
    esac
  done <<< "$config_keys"
  object_format="$(isolated_git config --no-includes --file "$clone_path/.git/config" \
    --get extensions.objectformat 2>/dev/null || true)"
  [[ -z "$object_format" || "$object_format" == "sha256" ]] || return 14
  remote_names="$(isolated_git -C "$clone_path" remote 2>/dev/null || true)"
  [[ -z "$remote_names" ]] || return 10
  checked_out_branch="$(isolated_git -C "$clone_path" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  [[ "$checked_out_branch" == "$branch" ]] || return 11
  final_sha="$(isolated_git -C "$clone_path" rev-parse --verify HEAD 2>/dev/null || true)"
  [[ -n "$final_sha" ]] || return 12
  isolated_git -C "$clone_path" merge-base --is-ancestor "$base_sha" "$final_sha" >/dev/null 2>&1 || return 12
  [[ -z "$(isolated_git -C "$clone_path" status --porcelain 2>/dev/null)" ]] || return 13
  printf '%s\n' "$final_sha"
}

failed_reader_head() {
  local clone_path="$1" base_sha="$2"
  local checked_out_branch config_key config_keys git_common_dir git_dir head_sha object_format remote_names status_output top_level
  if [[ ! -d "$clone_path" || -L "$clone_path" || ! -d "$clone_path/.git" || -L "$clone_path/.git" ]]; then
    return 10
  fi
  clone_path="$(cd "$clone_path" && pwd -P)" || return 10
  python3 "$clone_guard" verify-device --clone-path "$clone_path" >/dev/null 2>&1 || return 14
  top_level="$(isolated_git -C "$clone_path" rev-parse --path-format=absolute --show-toplevel 2>/dev/null || true)"
  git_dir="$(isolated_git -C "$clone_path" rev-parse --path-format=absolute --git-dir 2>/dev/null || true)"
  git_common_dir="$(isolated_git -C "$clone_path" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  [[ "$top_level" == "$clone_path" && "$git_dir" == "$clone_path/.git" \
    && "$git_common_dir" == "$clone_path/.git" ]] || return 10
  if ! python3 -c '
import os, stat, sys
from pathlib import Path

clone = Path(sys.argv[1])
git_dir = clone / ".git"
clone_info = clone.lstat()
if (
    not stat.S_ISDIR(clone_info.st_mode)
    or stat.S_ISLNK(clone_info.st_mode)
    or clone_info.st_uid != os.geteuid()
    or clone_info.st_mode & 0o222
):
    raise SystemExit(1)
for current, directories, files in os.walk(clone, followlinks=False):
    for name in directories + files:
        path = Path(current) / name
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            if path.is_relative_to(git_dir):
                raise SystemExit(1)
            continue
        if info.st_mode & 0o222:
            raise SystemExit(1)
        if name in directories:
            if not stat.S_ISDIR(info.st_mode):
                raise SystemExit(1)
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SystemExit(1)
alternates = git_dir / "objects" / "info" / "alternates"
if alternates.exists() or alternates.is_symlink():
    raise SystemExit(1)
hooks = git_dir / "hooks"
if hooks.exists():
    for hook in hooks.iterdir():
        info = hook.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            stat.S_ISREG(info.st_mode)
            and info.st_mode & 0o111
            and not hook.name.endswith(".sample")
        ):
            raise SystemExit(1)
' "$clone_path"; then
    return 14
  fi
  config_keys="$(isolated_git config --no-includes --file "$clone_path/.git/config" \
    --name-only --list 2>/dev/null)" || return 14
  while IFS= read -r config_key; do
    case "$config_key" in
      core.repositoryformatversion|core.filemode|core.bare|core.logallrefupdates|core.ignorecase|core.precomposeunicode|core.symlinks|extensions.objectformat) ;;
      *) return 14 ;;
    esac
  done <<< "$config_keys"
  object_format="$(isolated_git config --no-includes --file "$clone_path/.git/config" \
    --get extensions.objectformat 2>/dev/null || true)"
  [[ -z "$object_format" || "$object_format" == "sha256" ]] || return 14
  remote_names="$(isolated_git -C "$clone_path" remote 2>/dev/null || true)"
  [[ -z "$remote_names" ]] || return 10
  checked_out_branch="$(isolated_git -C "$clone_path" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  [[ -z "$checked_out_branch" ]] || return 11
  head_sha="$(isolated_git -C "$clone_path" rev-parse --verify HEAD 2>/dev/null || true)"
  [[ "$head_sha" == "$base_sha" ]] || return 12
  status_output="$(isolated_git -C "$clone_path" status --porcelain 2>/dev/null)" || return 14
  [[ -z "$status_output" ]] || return 13
  printf '%s\n' "$head_sha"
}

cleanup_on_exit() {
  local code=$?
  local workspace_gone=0
  local rollback_complete=1
  local rollback_journaled=1
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
      echo "WARNING: preserving specialist clones because cmux did not confirm workspace shutdown." >&2
      rollback_complete=0
      rollback_journaled=0
    else
      local index instance wt branch base worktree_head writer_rc reader_head reader_rc
      local cleanup_id staged tombstone
      cleanup_id="$boot_cleanup_id"
      for ((index=0; index<${#created_reader_workspaces[@]}; index++)); do
        instance="${created_reader_instances[$index]}"
        wt="${created_reader_workspaces[$index]}"
        base="${created_reader_bases[$index]}"
        staged="$(failed_boot_stage_path "$instance" "$cleanup_id")"
        tombstone="$(failed_boot_intent_path \
          "$instance" "$cleanup_id" "clone-retirement-tombstone.json")"
        if [[ ! -e "$tombstone" && ! -L "$tombstone" ]]; then
          if [[ -e "$wt" || -L "$wt" ]]; then
            set +e
            reader_head="$(failed_reader_head "$wt" "$base")"
            reader_rc=$?
            set -e
            if (( reader_rc != 0 )) || [[ "$reader_head" != "$base" ]]; then
              echo "WARNING: preserving reader snapshot on unexpected state after failed boot: $wt" >&2
              rollback_complete=0
              rollback_journaled=0
              continue
            fi
          fi
          if ! failed_boot_stage_clone reader "$instance" "$base" - "$cleanup_id"; then
            echo "WARNING: preserving journaled reader snapshot after failed cleanup: $wt" >&2
            rollback_complete=0
            rollback_journaled=0
            continue
          fi
          set +e
          reader_head="$(failed_reader_head "$staged" "$base")"
          reader_rc=$?
          set -e
          if (( reader_rc != 0 )) || [[ "$reader_head" != "$base" ]]; then
            echo "WARNING: preserving staged reader snapshot on unexpected state: $staged" >&2
            rollback_complete=0
            continue
          fi
        fi
        if ! retire_failed_boot_clone reader "$instance" "$base" - "$cleanup_id"; then
          echo "WARNING: preserving journaled reader retirement for retry: $staged" >&2
          rollback_complete=0
        elif ! failed_boot_creation clear reader "$instance" "$base" - \
            "$cleanup_id" >/dev/null; then
          echo "WARNING: preserving completed reader creation journal for retry." >&2
          rollback_complete=0
        fi
      done
      for ((index=0; index<${#created_worktrees[@]}; index++)); do
        instance="${created_worktree_instances[$index]}"
        wt="${created_worktrees[$index]}"
        branch="${created_worktree_branches[$index]}"
        base="${created_worktree_bases[$index]}"
        staged="$(failed_boot_stage_path "$instance" "$cleanup_id")"
        tombstone="$(failed_boot_intent_path \
          "$instance" "$cleanup_id" "clone-retirement-tombstone.json")"
        if [[ ! -e "$tombstone" && ! -L "$tombstone" ]]; then
          if [[ -e "$wt" || -L "$wt" ]]; then
            set +e
            worktree_head="$(failed_writer_head "$wt" "$branch" "$base")"
            writer_rc=$?
            set -e
            if (( writer_rc != 0 )); then
              echo "WARNING: preserving writer clone on unexpected state after failed boot: $wt" >&2
              rollback_complete=0
              rollback_journaled=0
              continue
            fi
          fi
          if ! failed_boot_stage_clone writer "$instance" "$base" "$branch" "$cleanup_id"; then
            echo "WARNING: preserving journaled writer clone after failed cleanup: $wt" >&2
            rollback_complete=0
            rollback_journaled=0
            continue
          fi
          set +e
          worktree_head="$(failed_writer_head "$staged" "$branch" "$base")"
          writer_rc=$?
          set -e
          if (( writer_rc != 0 )); then
            echo "WARNING: preserving staged writer clone on unexpected state: $staged" >&2
            rollback_complete=0
            continue
          fi
          if [[ "$worktree_head" != "$base" ]]; then
            echo "WARNING: preserving advanced writer only in CONTROL staging; no public ref created: $staged" >&2
            rollback_complete=0
            continue
          fi
        fi
        if ! retire_failed_boot_clone writer "$instance" "$base" "$branch" "$cleanup_id"; then
          echo "WARNING: preserving journaled writer retirement for retry: $staged" >&2
          rollback_complete=0
        elif ! failed_boot_creation clear writer "$instance" "$base" "$branch" \
            "$cleanup_id" >/dev/null; then
          echo "WARNING: preserving completed writer creation journal for retry." >&2
          rollback_complete=0
        fi
      done
    fi
  fi
  if (( code != 0 && completed == 0 && lifecycle_ledger_initialized == 1 \
      && (rollback_complete == 1 || rollback_journaled == 1) )); then
    if python3 "$ledger_bootstrap" cleanup \
        --runs-dir "$runs_dir" --feature "$feature" >/dev/null; then
      lifecycle_ledger_initialized=0
    else
      echo "WARNING: preserving fleet state because the trusted empty ledger changed during failed boot." >&2
      rollback_complete=0
      rollback_journaled=0
    fi
  fi
  if (( code != 0 && manifest_published == 1 && completed == 0 )); then
    if (( rollback_complete == 1 || rollback_journaled == 1 )); then
      rm -f "$manifest"
      rm -f "${manifest%.manifest}.state.json"
    else
      echo "WARNING: preserving manifest for incomplete boot recovery: $manifest" >&2
    fi
  fi
  [[ -n "$manifest_tmp" && -e "$manifest_tmp" ]] && rm -f "$manifest_tmp"
  release_fleet_boot_lock
  return "$code"
}
trap cleanup_on_exit EXIT

# Publish the budget evidence source before any clone, CMUX, or model effect.
# Recovery requires the durable creation intent that was published while the
# ledger pathname was absent; an arbitrary prior empty pathname is never
# adopted because that could erase an exhausted budget.
if ! python3 "$ledger_bootstrap" ensure \
    --runs-dir "$runs_dir" --feature "$feature" >/dev/null; then
  echo "A fresh trusted lifecycle ledger could not be established for fleet-$feature." >&2
  exit 2
fi
lifecycle_ledger_initialized=1

# Mission specialists never use the mutable target repository as their process
# workspace. Writers receive private branches; every non-writer receives a
# physically read-only detached clone of the exact frozen base. Each clone has
# its own Git dir and object store and no remote before any model process starts.
worktrees=()
if [[ -n "$target_repo" ]]; then
  worktrees_root="$(python3 "$clone_guard" ensure-root \
    --worktrees-root "$worktrees_root" --protected "$repo_root" \
    --protected "$runs_dir" --protected "$target_repo")" || {
    echo "Refusing unsafe worktree root: $worktrees_root" >&2
    exit 2
  }
  for ((i=0; i<${#instance_ids[@]}; i++)); do
    worktrees[$i]=""
    if [[ "${authorities[$i]}" == "write" ]]; then
      if ! reconcile_failed_boot_journal writer "${instance_ids[$i]}" \
          "${worktree_base_shas[$i]}" "${worktree_branches[$i]}"; then
        exit 2
      fi
      wt="$worktrees_root/$feature-${instance_ids[$i]}"
      branch="${worktree_branches[$i]}"
      base_sha="${worktree_base_shas[$i]}"
      if [[ -e "$wt" || -L "$wt" ]]; then
        echo "Worktree path already exists: $wt" >&2
        exit 2
      fi
      if ! failed_boot_creation plan writer "${instance_ids[$i]}" "$base_sha" \
          "$branch" "$boot_cleanup_id" >/dev/null; then
        echo "Could not establish isolated writer clone creation plan for ${instance_ids[$i]}." >&2
        exit 2
      fi
      fleet_boot_checkpoint after_clone_creation_plan
      if ! failed_boot_creation source writer "${instance_ids[$i]}" "$base_sha" \
          "$branch" "$boot_cleanup_id" >/dev/null; then
        echo "Could not prepare planned writer clone source for ${instance_ids[$i]}." >&2
        exit 2
      fi
      fleet_boot_checkpoint after_clone_source_mkdir_before_binding
      fleet_boot_checkpoint after_clone_source_mkdir
      fleet_boot_checkpoint after_writer_clone_source_mkdir
      if ! failed_boot_bind_creation writer "${instance_ids[$i]}" "$base_sha" \
          "$branch" "$boot_cleanup_id" >/dev/null; then
        echo "Could not bind planned writer clone source for ${instance_ids[$i]}." >&2
        exit 2
      fi
      fleet_boot_checkpoint after_clone_creation_binding
      fleet_boot_checkpoint after_clone_creation_intent
      if ! sanitized_git clone -q --no-hardlinks --no-checkout "$target_repo" "$wt" \
          || [[ "${FLEET_TEST_FAIL_CLONE_INSTANCE:-}" == "${instance_ids[$i]}" ]] \
          || ! sanitized_git -C "$wt" remote remove origin \
          || ! sanitized_git -C "$wt" switch -q -c "$branch" "$base_sha" \
          || ! sanitized_git -C "$wt" config user.name FleetMaker \
          || ! sanitized_git -C "$wt" config user.email maker@fleet.local; then
        if [[ -e "$wt" || -L "$wt" ]]; then
          failed_boot_stage_clone partial "${instance_ids[$i]}" "$base_sha" \
            - "$boot_cleanup_id" || true
        fi
        echo "Could not create isolated writer clone for ${instance_ids[$i]} at $wt" >&2
        exit 2
      fi
      set +e
      worktree_head="$(failed_writer_head "$wt" "$branch" "$base_sha")"
      writer_rc=$?
      set -e
      if (( writer_rc != 0 )) || [[ "$worktree_head" != "$base_sha" ]]; then
        failed_boot_stage_clone partial "${instance_ids[$i]}" "$base_sha" \
          - "$boot_cleanup_id" || true
        echo "Isolated writer clone failed verification for ${instance_ids[$i]} (check=$writer_rc)." >&2
        exit 2
      fi
      fleet_boot_checkpoint after_clone_complete
      fleet_boot_checkpoint after_writer_clone_ready_before_registration
      fleet_boot_checkpoint after_writer_clone_complete
      worktrees[$i]="$wt"
      created_worktrees+=("$wt")
      created_worktree_instances+=("${instance_ids[$i]}")
      created_worktree_branches+=("$branch")
      created_worktree_bases+=("$base_sha")
    elif [[ -n "$mission_id" ]]; then
      if ! reconcile_failed_boot_journal reader "${instance_ids[$i]}" \
          "$target_head" -; then
        exit 2
      fi
      wt="$worktrees_root/$feature-${instance_ids[$i]}"
      base_sha="$target_head"
      if [[ -e "$wt" || -L "$wt" ]]; then
        echo "Reader workspace path already exists: $wt" >&2
        exit 2
      fi
      if ! failed_boot_creation plan reader "${instance_ids[$i]}" "$base_sha" \
          - "$boot_cleanup_id" >/dev/null; then
        echo "Could not establish isolated reader clone creation plan for ${instance_ids[$i]}." >&2
        exit 2
      fi
      fleet_boot_checkpoint after_clone_creation_plan
      if ! failed_boot_creation source reader "${instance_ids[$i]}" "$base_sha" \
          - "$boot_cleanup_id" >/dev/null; then
        echo "Could not prepare planned reader clone source for ${instance_ids[$i]}." >&2
        exit 2
      fi
      fleet_boot_checkpoint after_clone_source_mkdir_before_binding
      fleet_boot_checkpoint after_clone_source_mkdir
      fleet_boot_checkpoint after_reader_clone_source_mkdir
      if ! failed_boot_bind_creation reader "${instance_ids[$i]}" "$base_sha" \
          - "$boot_cleanup_id" >/dev/null; then
        echo "Could not bind planned reader clone source for ${instance_ids[$i]}." >&2
        exit 2
      fi
      fleet_boot_checkpoint after_clone_creation_binding
      fleet_boot_checkpoint after_clone_creation_intent
      if ! sanitized_git clone -q --no-hardlinks --no-checkout "$target_repo" "$wt" \
          || [[ "${FLEET_TEST_FAIL_CLONE_INSTANCE:-}" == "${instance_ids[$i]}" ]] \
          || ! sanitized_git -C "$wt" remote remove origin \
          || ! sanitized_git -C "$wt" switch -q --detach "$base_sha" \
          || ! make_reader_clone_read_only "$wt"; then
        if [[ -e "$wt" || -L "$wt" ]]; then
          failed_boot_stage_clone partial "${instance_ids[$i]}" "$base_sha" \
            - "$boot_cleanup_id" || true
        fi
        echo "Could not create isolated reader snapshot for ${instance_ids[$i]} at $wt" >&2
        exit 2
      fi
      set +e
      reader_head="$(failed_reader_head "$wt" "$base_sha")"
      reader_rc=$?
      set -e
      if (( reader_rc != 0 )) || [[ "$reader_head" != "$base_sha" ]]; then
        failed_boot_stage_clone partial "${instance_ids[$i]}" "$base_sha" \
          - "$boot_cleanup_id" || true
        echo "Isolated reader snapshot failed verification for ${instance_ids[$i]} (check=$reader_rc)." >&2
        exit 2
      fi
      fleet_boot_checkpoint after_clone_complete
      fleet_boot_checkpoint after_reader_clone_ready_before_registration
      fleet_boot_checkpoint after_reader_clone_complete
      worktrees[$i]="$wt"
      created_reader_workspaces+=("$wt")
      created_reader_instances+=("${instance_ids[$i]}")
      created_reader_bases+=("$base_sha")
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
    instance_endpoint="${instance_control_sockets[$i]:-}"
    kimi_reader_fingerprint=""
    if [[ -n "$mission_id" && "${providers[$i]}" == "openai" \
      && "${authorities[$i]}" != "control" ]]; then
      expected_sandbox="read-only"
      codex_project_root="${worktrees[$i]:-}"
      if [[ -z "$codex_project_root" ]]; then
        echo "Mission-bound OpenAI instance ${instance_ids[$i]} has no isolated workspace." >&2
        exit 2
      fi
      if [[ "${authorities[$i]}" == "write" ]]; then
        expected_sandbox="workspace-write"
      fi
      if [[ "$agent_command" != *" --sandbox $expected_sandbox"* ]]; then
        echo "Mission-bound OpenAI instance ${instance_ids[$i]} lacks the expected $expected_sandbox contract." >&2
        exit 2
      fi
      # Permission profiles do not compose with --sandbox. Replace the legacy
      # flag with an equivalent authority profile that additionally permits
      # exactly this instance's authenticated AF_UNIX control socket.
      agent_command="${agent_command/ --sandbox $expected_sandbox/}"
      codex_control_policy="$(codex_control_permission_profile \
        "$instance_endpoint" "${authorities[$i]}" "$HOME" "$runs_dir")"
      codex_permission_name="fleet_reader"
      [[ "${authorities[$i]}" != "write" ]] || codex_permission_name="fleet_writer"
      codex_project_policy="$(python3 -c 'import json, sys; print(f"projects={{{json.dumps(sys.argv[1])}={{trust_level=\"untrusted\"}}}}")' "$codex_project_root")"
      agent_command="$agent_command$(printf ' -c %q -c %q -c %q' \
        "$codex_control_policy" "default_permissions=\"$codex_permission_name\"" \
        "$codex_project_policy")"
      # run-interactive-agent provisions only controller-vetted CMUX hooks in
      # an ephemeral Codex home. Skip the per-run trust UI so that automation
      # cannot have its first tracked prompt consumed by the hook browser.
      agent_command="$agent_command --dangerously-bypass-hook-trust"
    fi
    launch_cwd="$repo_root"
    if [[ -n "${worktrees[$i]:-}" ]]; then
      # The writer's cwd contains its complete isolated Git store. Never add
      # the target repository's common Git dir to a model-writable root.
      if [[ "${providers[$i]}" == "openai" && -z "$mission_id" ]]; then
        codex_project_policy="$(python3 -c 'import json, sys; print(f"projects={{{json.dumps(sys.argv[1])}={{trust_level=\"untrusted\"}}}}")' "${worktrees[$i]}")"
        agent_command="$agent_command$(printf ' -c %q' "$codex_project_policy")"
      fi
      launch_cwd="${worktrees[$i]}"
    fi
    # Kimi CLI 1.11 validates an explicit --work-dir as writable before its
    # reduced read-only agent is loaded.  Permit only the clone root during
    # provider startup; descendants remain physically read-only, no task has
    # been submitted, and the exact root entry set is sealed.  Restore and
    # revalidate the complete reader snapshot before publishing the manifest.
    if [[ -n "$mission_id" && "${hook_sources[$i]}" == "kimi" \
      && "${authorities[$i]}" != "write" ]]; then
      kimi_reader_fingerprint="$(make_kimi_reader_root_cli_writable "$launch_cwd")" || {
        echo "Could not prepare the read-only Kimi workspace for CLI startup." >&2
        exit 1
      }
    fi
    launch_rc=0
    launch_interactive_surface \
      "$surface" "${instance_ids[$i]}/${role_types[$i]}" "$launch_cwd" \
      "${role_types[$i]}" "${authorities[$i]}" "${required_envs[$i]}" \
      "$instance_endpoint" "$agent_command" "${ready_patterns[$i]}" \
      "${executables[$i]}" || launch_rc=$?
    if [[ -n "$kimi_reader_fingerprint" ]]; then
      if ! make_reader_clone_read_only "$launch_cwd"; then
        echo "Could not restore the Kimi reader snapshot to read-only mode." >&2
        exit 1
      fi
      restored_fingerprint="$(reader_clone_root_fingerprint "$launch_cwd")" || {
        echo "Could not verify the restored Kimi reader snapshot." >&2
        exit 1
      }
      if [[ "$restored_fingerprint" != "$kimi_reader_fingerprint" ]]; then
        echo "Kimi CLI startup changed the sealed reader root." >&2
        exit 1
      fi
      set +e
      reader_head="$(failed_reader_head "$launch_cwd" "$target_head")"
      reader_rc=$?
      set -e
      if (( reader_rc != 0 )) || [[ "$reader_head" != "$target_head" ]]; then
        echo "Kimi reader snapshot drifted during CLI startup (check=$reader_rc)." >&2
        exit 1
      fi
    fi
    (( launch_rc == 0 )) || exit "$launch_rc"
  else
    idle_command="clear; echo '== worker: ${instance_ids[$i]} (${role_types[$i]}) — idle =='; echo 'dispatch with: ./scripts/fleet-dispatch.sh $feature ${instance_ids[$i]} \"<task>\"'"
    if [[ -n "${worktrees[$i]:-}" ]]; then
      idle_command="$(printf 'cd %q && ' "${worktrees[$i]}")$idle_command"
    fi
    cmux_send_line "$surface" "$idle_command"
  fi
  if [[ "${runners[$i]}" != "interactive" ]]; then
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
  echo "manifest_contract_version=3"
  echo "execution_profile=$execution_profile"
  echo "tracking_protocol=control-v1"
  echo "feature=$feature"
  echo "preset=$resolved_preset"
  echo "mode=$execution_mode"
  [[ -z "$compiled_digest" ]] || echo "compiled_digest=$compiled_digest"
  [[ -z "$router_digest" ]] || echo "router_digest=$router_digest"
  [[ -z "$roster_digest" ]] || echo "roster_digest=$roster_digest"
  [[ -z "$launch_digest" ]] || echo "launch_digest=$launch_digest"
  echo "identity_group.count=${#identity_groups[@]}"
  for ((i=0; i<${#identity_groups[@]}; i++)); do
    echo "identity_group.$((i + 1))=${identity_groups[$i]}"
  done
  [[ -z "$mission_id" ]] || echo "mission_id=$mission_id"
  [[ -z "$control_socket" ]] || echo "control_socket=$control_socket"
  echo "created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "workspace_cwd=$repo_root"
  if [[ -n "$target_repo" ]]; then
    echo "target_repo=$target_repo"
    echo "base_sha=$target_head"
  fi
  echo "workspace=$ws_ref"
  echo "workspace_uuid=$workspace_uuid"
  echo "workspace.handoff_state=live"
  echo "workspace.quiesced=0"
  echo "lead=$lead_surface"
  echo "lead.uuid=$lead_uuid"
  echo "lead.phase=$lead_phase"
  echo "lead.authority=control"
  if [[ "${FLEET_NO_LEAD:-0}" == "1" ]]; then
    # This surface is an operator-visible shell monitor, not a model-backed
    # roster member.  Do not publish empty or fabricated provider metadata.
    echo "lead.role_type=monitor"
    echo "lead.runner=monitor"
  else
    echo "lead.tool_access=$lead_tool_access"
    echo "lead.provider=$lead_provider"
    echo "lead.model=$lead_model"
    echo "lead.hook_source=$lead_hook_source"
    [[ -z "$control_socket" ]] || echo "lead.control_socket=$control_socket"
    [[ -z "$lead_variant" ]] || echo "lead.variant=$lead_variant"
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
    [[ -z "${instance_control_sockets[$i]:-}" ]] || \
      echo "${instance_ids[$i]}.control_socket=${instance_control_sockets[$i]}"
    [[ -z "${variants[$i]}" ]] || echo "${instance_ids[$i]}.variant=${variants[$i]}"
    if [[ -n "${worktrees[$i]:-}" ]]; then
      if [[ "${authorities[$i]}" == "write" ]]; then
        echo "${instance_ids[$i]}.worktree=${worktrees[$i]}"
        echo "${instance_ids[$i]}.branch=${worktree_branches[$i]}"
        echo "${instance_ids[$i]}.base_sha=${worktree_base_shas[$i]}"
        echo "${instance_ids[$i]}.final_sha=${worktree_base_shas[$i]}"
        echo "${instance_ids[$i]}.git_isolation=isolated-clone"
        echo "${instance_ids[$i]}.publication_state=private"
      elif [[ -n "$mission_id" ]]; then
        echo "${instance_ids[$i]}.workspace=${worktrees[$i]}"
        echo "${instance_ids[$i]}.workspace_kind=isolated-read-clone"
        echo "${instance_ids[$i]}.workspace_base_sha=$target_head"
        echo "${instance_ids[$i]}.workspace_publication=none"
      fi
    fi
  done
} > "$manifest_tmp"
if ! python3 "$ledger_bootstrap" handoff \
    --runs-dir "$runs_dir" --feature "$feature" >/dev/null; then
  echo "Could not bind the lifecycle ledger bootstrap to manifest handoff." >&2
  exit 2
fi
mv "$manifest_tmp" "$manifest"
manifest_tmp=""
manifest_published=1
python3 "$repo_root/scripts/fleet_state.py" init "$manifest" >/dev/null
fleet_boot_checkpoint after_manifest_publish_before_creation_clear
if [[ -n "$target_repo" ]]; then
  for ((i=0; i<${#instance_ids[@]}; i++)); do
    if [[ "${authorities[$i]}" == "write" ]]; then
      failed_boot_creation clear writer "${instance_ids[$i]}" \
        "${worktree_base_shas[$i]}" "${worktree_branches[$i]}" \
        "$boot_cleanup_id" >/dev/null || {
        echo "Could not transfer writer clone ownership from creation journal to manifest." >&2
        exit 2
      }
    elif [[ -n "$mission_id" ]]; then
      failed_boot_creation clear reader "${instance_ids[$i]}" "$target_head" - \
        "$boot_cleanup_id" >/dev/null || {
        echo "Could not transfer reader clone ownership from creation journal to manifest." >&2
        exit 2
      }
    fi
  done
fi

if [[ "${FLEET_NO_LEAD:-0}" == "1" ]]; then
  cmux_send_line "$lead_surface" \
    "clear; echo '== fleet-$feature monitor — no lead agent =='"
  cmux read-screen --surface "$lead_surface" --workspace "$ws_ref" --lines 3 >/dev/null
else
  launch_interactive_surface \
    "$lead_surface" "lead/$lead_role_type" "$repo_root" \
    "$lead_role_type" control "$lead_requires_env" "$control_socket" \
    "$lead_command" "$lead_ready_pattern" "$lead_executable" || exit 1

  if [[ "$execution_mode" == "autonomous" ]]; then
    # The first submitted lead turn must be the tracked mission from fleet-run.
    # A boot-time orientation turn can still be active (or trigger a CLI update)
    # when the mission arrives, making UserPromptSubmit ownership ambiguous.
    cmux set-status mission ready --workspace "$ws_ref" --icon sparkles >/dev/null || true
    cmux read-screen --surface "$lead_surface" --workspace "$ws_ref" --lines 8 >/dev/null
  else
    cmux_send_line "$lead_surface" \
      "Eres el lead ($lead_role_type) de fleet-$feature en $ws_ref. El preset resuelto es '$resolved_preset' y su modo es '$execution_mode'. Lee $manifest y verifica UUIDs/surfaces contra 'cmux tree --workspace $ws_ref --id-format both'. Los panes están ordenados por capacidad. No despaches todavía: reporta el roster y el modo en una línea, y espera la misión."
    cmux read-screen --surface "$lead_surface" --workspace "$ws_ref" --lines 8 >/dev/null
  fi
fi

if ! python3 "$ledger_bootstrap" complete \
    --runs-dir "$runs_dir" --feature "$feature" >/dev/null; then
  echo "Could not retire the completed lifecycle ledger bootstrap intent." >&2
  exit 2
fi
completed=1
echo "fleet-$feature is up (preset: $resolved_preset, mode: $execution_mode, lead: ${lead_role_type:-monitor})."
echo "manifest: $manifest"
cat "$manifest"
