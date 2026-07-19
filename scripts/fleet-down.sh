#!/usr/bin/env bash
set -euo pipefail

# Close a fleet workspace created by fleet-up.sh and remove its manifest.
#
# Usage:
#   ./scripts/fleet-down.sh <feature>

feature="${1:-}"
recover_absent=0
prepare_archive=0
handoff_assurance=0
case "${2:-}" in
  "") ;;
  --recover-absent) recover_absent=1 ;;
  --prepare-archive) prepare_archive=1 ;;
  --handoff-assurance) handoff_assurance=1 ;;
  *) echo "Unknown option: $2" >&2; exit 2 ;;
esac
if [[ -z "$feature" ]]; then
  echo "Usage: $0 <feature> [--recover-absent|--prepare-archive|--handoff-assurance]" >&2
  exit 2
fi
if [[ ! "$feature" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  echo "Invalid feature name '$feature'." >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs_dir_raw="${FLEET_RUNS_DIR:-$repo_root/orchestration/runs}"
worktrees_root="${FLEET_WORKTREES_DIR:-/tmp/fleet_workspaces-$(id -u)}"
identity="$repo_root/scripts/fleet_identity.py"
leases="$repo_root/scripts/fleet_leases.py"
dialogue_controller="$repo_root/scripts/fleet_dialogue_controller.py"
assurance_controller="$repo_root/scripts/fleet_assurance_controller.py"
audit_client="$repo_root/scripts/fleet_audit_client.py"
archive_client="$repo_root/scripts/fleet_archive.py"
control_client="$repo_root/scripts/fleet_control_service.py"
manifest_guard="$repo_root/scripts/fleet_manifest_guard.py"
clone_guard="$repo_root/scripts/fleet_clone_guard.py"
assurance_handoff="$repo_root/scripts/fleet_assurance_handoff.py"
export CMUX_QUIET=1

boot_lock_pid=""
boot_lock_held=0
boot_lock_channel_dir=""
handoff_lock_pid=""
handoff_lock_held=0
handoff_channel_dir=""
handoff_result=""
handoff_child_rc=0
boot_lock_ready_timeout="${FLEET_BOOT_LOCK_READY_TIMEOUT_SECONDS:-60}"
handoff_ready_timeout="${FLEET_HANDOFF_READY_TIMEOUT_SECONDS:-60}"
handoff_commit_timeout="${FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS:-300}"

validate_handoff_timeout() {
  local value="$1" maximum="$2" name="$3"
  if [[ ! "$value" =~ ^[1-9][0-9]{0,3}$ ]] || (( 10#$value > maximum )); then
    echo "Invalid $name '$value'." >&2
    return 2
  fi
}

background_job_is_running() {
  local wanted="$1" candidate
  # Bash owns and reaps these direct children. `jobs -pr` excludes completed
  # jobs, unlike `kill -0`, which may still succeed for a zombie.
  for candidate in $(jobs -pr); do
    [[ "$candidate" == "$wanted" ]] && return 0
  done
  return 1
}

# Validate every operator-controlled lifecycle deadline before canonicalization,
# preflight, locks, CMUX, publication, or any other durable/external effect.
if ! validate_handoff_timeout "$boot_lock_ready_timeout" 600 \
    "FLEET_BOOT_LOCK_READY_TIMEOUT_SECONDS"; then
  exit 2
fi
if (( handoff_assurance == 1 )); then
  if ! validate_handoff_timeout "$handoff_ready_timeout" 600 \
      "FLEET_HANDOFF_READY_TIMEOUT_SECONDS" \
    || ! validate_handoff_timeout "$handoff_commit_timeout" 3600 \
      "FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS"; then
    exit 2
  fi
fi

cleanup_assurance_handoff_channel() {
  if [[ -n "$handoff_channel_dir" ]]; then
    rm -f "$handoff_channel_dir/control" "$handoff_channel_dir/ready"
    rmdir "$handoff_channel_dir" 2>/dev/null || true
    handoff_channel_dir=""
  fi
}

reap_assurance_handoff_child() {
  local grace="${1:-5}" pid="$handoff_lock_pid" watchdog_pid
  handoff_child_rc=0
  [[ -n "$pid" ]] || return 0
  (
    sleep "$grace"
    kill -TERM "$pid" 2>/dev/null || true
    sleep 1
    kill -KILL "$pid" 2>/dev/null || true
  ) </dev/null >/dev/null 2>&1 &
  watchdog_pid=$!
  if wait "$pid" 2>/dev/null; then
    handoff_child_rc=0
  else
    handoff_child_rc=$?
  fi
  kill "$watchdog_pid" 2>/dev/null || true
  wait "$watchdog_pid" 2>/dev/null || true
}

release_assurance_handoff_lock() {
  local reap_grace=0
  if [[ -n "$handoff_lock_pid" ]]; then
    if (( handoff_lock_held == 1 )); then
      printf 'ABORT\n' >&8 2>/dev/null || true
      reap_grace=5
    fi
    exec 8>&- || true
    exec 7<&- || true
    reap_assurance_handoff_child "$reap_grace"
  fi
  cleanup_assurance_handoff_channel
  handoff_lock_held=0
  handoff_lock_pid=""
}

start_assurance_handoff_lock() {
  local mission_id="$1" channel_dir control_fifo ready_fifo readiness deadline
  if ! validate_handoff_timeout "$handoff_ready_timeout" 600 \
    "FLEET_HANDOFF_READY_TIMEOUT_SECONDS"; then
    return 2
  fi
  channel_dir="$(mktemp -d /tmp/fleet-assurance-handoff.XXXXXX)" || return 75
  handoff_channel_dir="$channel_dir"
  control_fifo="$channel_dir/control"
  ready_fifo="$channel_dir/ready"
  if ! mkfifo "$control_fifo" "$ready_fifo"; then
    cleanup_assurance_handoff_channel
    return 75
  fi
  python3 "$assurance_handoff" hold --runs-dir "$runs_dir" \
    --feature "$feature" --mission-id "$mission_id" \
    < "$control_fifo" > "$ready_fifo" &
  handoff_lock_pid=$!
  # RDWR opens cannot block forever if the child fails before attaching its
  # FIFO endpoints; the bounded read/pid loop remains the readiness authority.
  exec 8<>"$control_fifo"
  exec 7<>"$ready_fifo"
  readiness=""
  deadline=$((SECONDS + 10#$handoff_ready_timeout))
  while (( SECONDS < deadline )); do
    if IFS= read -r -t 1 readiness <&7; then
      break
    fi
    if ! background_job_is_running "$handoff_lock_pid"; then
      break
    fi
  done
  if [[ "$readiness" != "READY" ]]; then
    exec 8>&- || true
    exec 7<&- || true
    reap_assurance_handoff_child 0
    local hold_rc=$handoff_child_rc
    cleanup_assurance_handoff_channel
    handoff_lock_pid=""
    (( hold_rc == 2 )) && return 2
    return 75
  fi
  cleanup_assurance_handoff_channel
  handoff_lock_held=1
}

commit_assurance_handoff_lock() {
  local hold_rc deadline
  if (( handoff_lock_held != 1 )); then
    return 75
  fi
  if ! validate_handoff_timeout "$handoff_commit_timeout" 3600 \
    "FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS"; then
    return 2
  fi
  printf 'COMMIT\n' >&8 || return 75
  exec 8>&-
  handoff_result=""
  deadline=$((SECONDS + 10#$handoff_commit_timeout))
  while (( SECONDS < deadline )); do
    if IFS= read -r -t 1 handoff_result <&7; then
      break
    fi
    if ! background_job_is_running "$handoff_lock_pid"; then
      break
    fi
  done
  exec 7<&-
  if [[ -n "$handoff_result" ]]; then
    reap_assurance_handoff_child 5
  else
    reap_assurance_handoff_child 0
  fi
  hold_rc=$handoff_child_rc
  handoff_lock_held=0
  handoff_lock_pid=""
  if (( hold_rc != 0 )) || [[ -z "$handoff_result" ]]; then
    (( hold_rc == 2 )) && return 2
    return 75
  fi
}

release_fleet_boot_lock() {
  local grace="${1:-5}" watchdog_pid
  exec 9>&- || true
  exec 6<&- || true
  if [[ -n "$boot_lock_pid" ]]; then
    (
      sleep "$grace"
      kill -TERM "$boot_lock_pid" 2>/dev/null || true
      sleep 1
      kill -KILL "$boot_lock_pid" 2>/dev/null || true
    ) </dev/null >/dev/null 2>&1 &
    watchdog_pid=$!
    wait "$boot_lock_pid" 2>/dev/null || true
    kill "$watchdog_pid" 2>/dev/null || true
    wait "$watchdog_pid" 2>/dev/null || true
  fi
  if [[ -n "$boot_lock_channel_dir" ]]; then
    rm -f "$boot_lock_channel_dir/control" "$boot_lock_channel_dir/ready"
    rmdir "$boot_lock_channel_dir" 2>/dev/null || true
    boot_lock_channel_dir=""
  fi
  boot_lock_held=0
  boot_lock_pid=""
}

acquire_fleet_boot_lock() {
  local channel_dir control_fifo ready_fifo readiness deadline
  channel_dir="$(mktemp -d /tmp/fleet-boot-lock.XXXXXX)" || return 1
  boot_lock_channel_dir="$channel_dir"
  control_fifo="$channel_dir/control"
  ready_fifo="$channel_dir/ready"
  if ! mkfifo "$control_fifo" "$ready_fifo"; then
    release_fleet_boot_lock 0
    return 1
  fi
  python3 "$clone_guard" boot-lock --runs-dir "$runs_dir" --feature "$feature" \
    < "$control_fifo" > "$ready_fifo" &
  boot_lock_pid=$!
  # RDWR opens cannot block forever if the child fails before attaching.
  exec 9<>"$control_fifo"
  exec 6<>"$ready_fifo"
  readiness=""
  deadline=$((SECONDS + 10#$boot_lock_ready_timeout))
  while (( SECONDS < deadline )); do
    if IFS= read -r -t 1 readiness <&6; then
      break
    fi
    if ! background_job_is_running "$boot_lock_pid"; then
      break
    fi
  done
  if [[ "$readiness" != "READY" ]]; then
    exec 9>&- || true
    exec 6<&- || true
    release_fleet_boot_lock 0
    return 1
  fi
  exec 6<&- || true
  rm -f "$control_fifo" "$ready_fifo"
  rmdir "$channel_dir" 2>/dev/null || true
  boot_lock_channel_dir=""
  boot_lock_held=1
}

# The boot-lock acquisition itself can be interrupted before it reports READY;
# install cleanup before the first possible acquisition.  Later teardown traps
# replace this one and call the same exact-PID cleanup.
trap release_fleet_boot_lock EXIT

reconcile_assurance_handoff_close() {
  local workspace_uuid="$1" recovery_close_id
  recovery_close_id="$(python3 -c 'import uuid; print(uuid.uuid4())')" || return 75
  if ! python3 "$leases" begin-close "$runs_dir" --feature "$feature" \
      --close-id "$recovery_close_id" --workspace-uuid "$workspace_uuid" \
      >/dev/null; then
    return 75
  fi
  if ! python3 "$leases" end-close "$runs_dir" --feature "$feature" \
      --close-id "$recovery_close_id" >/dev/null; then
    return 75
  fi
}

if ! runs_dir="$(python3 "$manifest_guard" canonical-root --runs-dir "$runs_dir_raw")"; then
  exit 75
fi
manifest="$runs_dir/fleet-$feature.manifest"

# Assurance handoff has a stricter, entirely read-only preflight than terminal
# teardown.  No boot lock, snapshot, close lease, Mission lock, or journal is
# created until this probe proves the exact approved Mission transition.
handoff_preflight_mode=""
handoff_preflight_mission_id=""
if (( handoff_assurance == 1 )); then
  handoff_preflight_args=(
    preflight --runs-dir "$runs_dir" --feature "$feature"
  )
  if [[ -n "${FLEET_MISSION_ID:-}" ]]; then
    handoff_preflight_args+=(--mission-id "$FLEET_MISSION_ID")
  fi
  set +e
  handoff_preflight_record="$(
    python3 "$assurance_handoff" "${handoff_preflight_args[@]}"
  )"
  handoff_preflight_rc=$?
  set -e
  if (( handoff_preflight_rc != 0 )); then
    exit "$handoff_preflight_rc"
  fi
  handoff_preflight_fields="$(python3 -c '
import json, sys
value = json.loads(sys.argv[1])
print("\t".join((value["mode"], value["mission_id"], value["workspace_uuid"])))
' "$handoff_preflight_record"
  )"
  IFS=$'\t' read -r handoff_preflight_mode handoff_preflight_mission_id \
    handoff_preflight_workspace_uuid \
    <<< "$handoff_preflight_fields"
  if [[ "$handoff_preflight_mode" == "complete" ]]; then
    if ! acquire_fleet_boot_lock; then
      echo "Another fleet lifecycle operation owns '$feature'; refusing concurrent handoff replay." >&2
      exit 75
    fi
    trap release_fleet_boot_lock EXIT
    if ! reconcile_assurance_handoff_close "$handoff_preflight_workspace_uuid"; then
      echo "Could not reconcile assurance handoff close ownership." >&2
      exit 75
    fi
    python3 "$assurance_handoff" recover --runs-dir "$runs_dir" \
      --feature "$feature" --mission-id "$handoff_preflight_mission_id"
    exit $?
  fi
  if [[ "$handoff_preflight_mode" == "recover" ]]; then
    if ! acquire_fleet_boot_lock; then
      echo "Another fleet lifecycle operation owns '$feature'; refusing concurrent handoff recovery." >&2
      exit 75
    fi
    trap release_fleet_boot_lock EXIT
    if ! reconcile_assurance_handoff_close "$handoff_preflight_workspace_uuid"; then
      echo "Could not reconcile assurance handoff close ownership." >&2
      exit 75
    fi
    python3 "$assurance_handoff" recover --runs-dir "$runs_dir" \
      --feature "$feature" --mission-id "$handoff_preflight_mission_id"
    exit $?
  fi
  if [[ "$handoff_preflight_mode" != "active" ]]; then
    echo "Refusing assurance handoff: invalid preflight result." >&2
    exit 2
  fi
fi

# This probe is deliberately read-only and precedes boot-lock admission.  An
# unsafe manifest/state pair therefore cannot create a lock, a teardown
# snapshot, a close lease, or any evidence.  Exit 1 means only that there is no
# active manifest, in which case archived teardown recovery remains available.
# Lifecycle exit contract: 2 is permanent invalid/unsafe durable input; 75 is
# reserved for transient ownership contention or a concurrent lifecycle change.
set +e
preflight_active_phase="$(
  python3 "$repo_root/scripts/fleet_state.py" probe-active "$manifest"
)"
preflight_phase_rc=$?
set -e
if (( preflight_phase_rc != 0 && preflight_phase_rc != 1 )); then
  echo "Refusing teardown: fleet phase state is missing, corrupt, or path-unsafe." >&2
  exit 2
fi
if ! acquire_fleet_boot_lock; then
  echo "Another fleet lifecycle operation owns '$feature'; refusing concurrent teardown." >&2
  exit 75
fi
trap release_fleet_boot_lock EXIT

# Recheck after lifecycle admission and immediately before the first manifest
# guard effect.  This closes the normal fleet-up/fleet-down race without
# changing the no-manifest recovery contract.
set +e
locked_active_phase="$(
  python3 "$repo_root/scripts/fleet_state.py" probe-active "$manifest"
)"
locked_phase_rc=$?
set -e
if (( locked_phase_rc == 0 )); then
  active_phase="$locked_active_phase"
elif (( locked_phase_rc == 1 && preflight_phase_rc == 1 )); then
  active_phase=""
elif (( locked_phase_rc == 1 )); then
  echo "Refusing teardown: active fleet manifest changed during lifecycle admission." >&2
  exit 75
else
  echo "Refusing teardown: fleet phase state is missing, corrupt, or path-unsafe." >&2
  exit 2
fi
manifest_snapshot_name=""
manifest_snapshot=""
manifest_sha256=""
archive_dir_name=""

manifest_value() {
  local key="$1"
  python3 "$manifest_guard" get --runs-dir "$runs_dir" --feature "$feature" \
    --snapshot "$manifest_snapshot_name" --digest "$manifest_snapshot_sha256" \
    --key "$key"
}

manifest_entries_with_suffix() {
  local suffix="$1"
  python3 "$manifest_guard" list-suffix --runs-dir "$runs_dir" \
    --feature "$feature" --snapshot "$manifest_snapshot_name" \
    --digest "$manifest_snapshot_sha256" --suffix "$suffix"
}

assert_manifest_snapshot() {
  python3 "$manifest_guard" get --runs-dir "$runs_dir" --feature "$feature" \
    --snapshot "$manifest_snapshot_name" --digest "$manifest_snapshot_sha256" \
    --key feature >/dev/null
}

assert_manifest_unchanged() {
  python3 "$manifest_guard" assert-active --runs-dir "$runs_dir" \
    --feature "$feature" --digest "$manifest_sha256" >/dev/null
}

archived_manifest_value() {
  local key="$1"
  python3 "$manifest_guard" get-archived --runs-dir "$runs_dir" \
    --feature "$feature" --archive-dir "$archive_dir_name" \
    --digest "$manifest_sha256" --key "$key"
}

archived_manifest_entries_with_suffix() {
  local suffix="$1"
  python3 "$manifest_guard" list-archived-suffix --runs-dir "$runs_dir" \
    --feature "$feature" --archive-dir "$archive_dir_name" \
    --digest "$manifest_sha256" --suffix "$suffix"
}

set_manifest_value() {
  local key="$1" value="$2" new_digest
  new_digest="$(python3 "$manifest_guard" set-active --runs-dir "$runs_dir" \
    --feature "$feature" --digest "$manifest_sha256" \
    --key "$key" --value "$value")" || return $?
  manifest_sha256="$new_digest"
}

staging_path_for() {
  local instance="$1"
  printf '%s/.fleet-control-staging/%s--%s--%s\n' \
    "$worktrees_root" "$feature" "$instance" "$workspace_uuid"
}

stage_isolated_clone() {
  local source_path="$1" destination_path="$2" clone_kind="$3"
  local instance="$4" expected_sha="$5" branch="${6:--}"
  local expected_source="$worktrees_root/$feature-$instance"
  local expected_destination
  expected_destination="$(staging_path_for "$instance")"
  [[ "$source_path" == "$expected_source" && "$destination_path" == "$expected_destination" ]] || return 75
  python3 "$clone_guard" stage --runs-dir "$runs_dir" \
    --worktrees-root "$worktrees_root" --feature "$feature" \
    --instance "$instance" --workspace-uuid "$workspace_uuid" \
    --kind "$clone_kind" --expected-sha "$expected_sha" --branch "$branch"
}

publication_checkpoint() {
  local name="$1"
  if [[ "${FLEET_TEST_PUBLICATION_CRASH_AT:-}" == "$name" ]]; then
    kill -KILL "$$"
  fi
  if [[ "${FLEET_TEST_PUBLICATION_PAUSE_AT:-}" == "$name" ]]; then
    if [[ -z "${FLEET_TEST_PUBLICATION_RESUME_FILE:-}" ]]; then
      echo "FLEET_TEST_PUBLICATION_RESUME_FILE is required for a teardown pause." >&2
      return 1
    fi
    while [[ ! -e "$FLEET_TEST_PUBLICATION_RESUME_FILE" ]]; do
      sleep 0.05
    done
  fi
}

publication_intent_path_for() {
  local instance="$1"
  printf '%s/.fleet-%s.%s.%s.publication-intent.json\n' \
    "$runs_dir" "$feature" "$instance" "$workspace_uuid"
}

clone_stage_intent_path_for() {
  local instance="$1"
  printf '%s/.fleet-%s.%s.%s.clone-stage-intent.json\n' \
    "$runs_dir" "$feature" "$instance" "$workspace_uuid"
}

clone_retirement_path_for() {
  local instance="$1"
  printf '%s/.fleet-%s.%s.%s.clone-retirement-tombstone.json\n' \
    "$runs_dir" "$feature" "$instance" "$workspace_uuid"
}

publication_intent() {
  local mode="$1" path="$2" instance="$3" branch="$4" base_sha="$5"
  local candidate_sha="$6" staging_path="$7"
  [[ "$path" == "$(publication_intent_path_for "$instance")" ]] || return 75
  python3 "$clone_guard" publication "$mode" --runs-dir "$runs_dir" \
    --worktrees-root "$worktrees_root" --feature "$feature" \
    --instance "$instance" --workspace-uuid "$workspace_uuid" \
    --target-repo "$target_repo" --branch "$branch" --base-sha "$base_sha" \
    --candidate-sha "$candidate_sha" --staging-path "$staging_path" >/dev/null
}

clone_retirement() {
  local mode="$1" clone_kind="$2" instance="$3" expected_sha="$4"
  local branch="${5:--}"
  python3 "$clone_guard" tombstone "$mode" --runs-dir "$runs_dir" \
    --worktrees-root "$worktrees_root" --feature "$feature" \
    --instance "$instance" --workspace-uuid "$workspace_uuid" \
    --kind "$clone_kind" --expected-sha "$expected_sha" --branch "$branch" \
    >/dev/null
}

clear_clone_stage_intent() {
  local clone_kind="$1" instance="$2" expected_sha="$3" branch="${4:--}"
  python3 "$clone_guard" clear-stage --runs-dir "$runs_dir" \
    --worktrees-root "$worktrees_root" --feature "$feature" \
    --instance "$instance" --workspace-uuid "$workspace_uuid" \
    --kind "$clone_kind" --expected-sha "$expected_sha" --branch "$branch" \
    >/dev/null
}

clear_clone_creation_intent() {
  local clone_kind="$1" instance="$2" expected_sha="$3" branch="${4:--}"
  python3 "$clone_guard" creation clear --runs-dir "$runs_dir" \
    --worktrees-root "$worktrees_root" --target-repo "$target_repo" \
    --feature "$feature" --instance "$instance" \
    --workspace-uuid "$boot_cleanup_id" --kind "$clone_kind" \
    --expected-sha "$expected_sha" --branch "$branch" >/dev/null
}

isolated_git() {
  /usr/bin/env \
    -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR -u GIT_OBJECT_DIRECTORY \
    -u GIT_ALTERNATE_OBJECT_DIRECTORIES -u GIT_INDEX_FILE -u GIT_NAMESPACE \
    -u GIT_CONFIG_COUNT -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_SYSTEM \
    -u GIT_CONFIG_GLOBAL \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_OPTIONAL_LOCKS=0 \
    git -c core.fsmonitor=false -c core.hooksPath=/dev/null \
      -c submodule.recurse=false "$@"
}

private_writer_head() {
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
clone = git_dir.parent
objects = git_dir / "objects"
hooks = git_dir / "hooks"
for root, directories, files in os.walk(clone, followlinks=False):
    for name in directories + files:
        path = Path(root) / name
        info = path.lstat()
        inside_git = path == git_dir or path.is_relative_to(git_dir)
        if stat.S_ISLNK(info.st_mode):
            if inside_git:
                raise SystemExit(1)
            continue
        if info.st_uid != os.geteuid():
            raise SystemExit(1)
        if name in directories:
            if not stat.S_ISDIR(info.st_mode):
                raise SystemExit(1)
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SystemExit(1)
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
alternates = objects / "info" / "alternates"
if alternates.exists() or alternates.is_symlink():
    raise SystemExit(1)
if hooks.exists():
    for entry in hooks.iterdir():
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            stat.S_ISREG(info.st_mode)
            and info.st_mode & 0o111
            and not entry.name.endswith(".sample")
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

private_reader_head() {
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

publish_isolated_branch() {
  local clone_path="$1" branch="$2" final_sha="$3" base_sha="$4" instance="$5"
  local bundle rechecked_sha
  local bound_target bound_branch bound_base
  local zero_sha
  printf -v zero_sha '%*s' "${#final_sha}" ''
  zero_sha="${zero_sha// /0}"
  bound_target="$(manifest_value target_repo)" || return 1
  bound_branch="$(manifest_value "$instance.branch")" || return 1
  bound_base="$(manifest_value "$instance.base_sha")" || return 1
  if [[ "$bound_target" != "$target_repo" || "$bound_branch" != "$branch" \
      || "$bound_base" != "$base_sha" ]]; then
    return 1
  fi
  assert_manifest_snapshot || return 1
  assert_manifest_unchanged || return 1
  if isolated_git -C "$target_repo" show-ref --verify --quiet "refs/heads/$branch"; then
    return 1
  fi
  bundle="$(mktemp "$runs_dir/.fleet-publication.XXXXXX.bundle")" || return 1
  chmod 600 "$bundle" || { rm -f "$bundle"; return 1; }
  if ! isolated_git -C "$clone_path" bundle create "$bundle" "refs/heads/$branch"; then
    rm -f "$bundle"
    return 1
  fi
  if ! rechecked_sha="$(private_writer_head "$clone_path" "$branch" "$base_sha")"; then
    rm -f "$bundle"
    return 1
  fi
  if [[ "$rechecked_sha" != "$final_sha" ]]; then
    rm -f "$bundle"
    return 1
  fi
  if ! isolated_git -C "$target_repo" fetch -q --no-write-fetch-head \
      "$bundle" "refs/heads/$branch"; then
    rm -f "$bundle"
    return 1
  fi
  rm -f "$bundle"
  isolated_git -C "$target_repo" cat-file -e "$final_sha^{commit}" || return 1
  # Re-derive the exact repository binding from the pinned, strictly parsed
  # snapshot, then revalidate the active CAS immediately before publication.
  bound_target="$(manifest_value target_repo)" || return 1
  bound_branch="$(manifest_value "$instance.branch")" || return 1
  bound_base="$(manifest_value "$instance.base_sha")" || return 1
  if [[ "$bound_target" != "$target_repo" || "$bound_branch" != "$branch" \
      || "$bound_base" != "$base_sha" ]]; then
    return 1
  fi
  assert_manifest_snapshot || return 1
  assert_manifest_unchanged || return 1
  isolated_git -C "$target_repo" update-ref "refs/heads/$branch" "$final_sha" "$zero_sha"
}

recover_archived_teardown() (
  local recovery_record recovery_rc archive_intent_name archive_manifest
  local workspace_uuid close_id close_started identity_rc target_repo records
  local entry instance branch base_sha final_sha branch_head
  set +e
  recovery_record="$(python3 "$manifest_guard" recover-archived \
    --runs-dir "$runs_dir" --feature "$feature")"
  recovery_rc=$?
  set -e
  (( recovery_rc == 0 )) || return "$recovery_rc"
  IFS=$'\t' read -r archive_intent_name archive_dir_name manifest_sha256 \
    workspace_uuid <<< "$recovery_record"
  archive_manifest="$runs_dir/archive/$archive_dir_name/manifest"
  archived_manifest_value feature >/dev/null || return 75
  set +e
  python3 "$identity" exists "$archive_manifest" >/dev/null 2>&1
  identity_rc=$?
  set -e
  if (( identity_rc != 1 )); then
    echo "Refusing archived teardown recovery: workspace absence was not confirmed." >&2
    return 75
  fi
  close_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  close_started=0
  recovery_cleanup() {
    local recovery_exit=$?
    trap - EXIT
    if (( close_started == 1 )); then
      python3 "$leases" end-close "$runs_dir" --feature "$feature" \
        --close-id "$close_id" >/dev/null || recovery_exit=75
    fi
    exit "$recovery_exit"
  }
  trap recovery_cleanup EXIT
  python3 "$leases" begin-close "$runs_dir" --feature "$feature" \
    --close-id "$close_id" --workspace-uuid "$workspace_uuid" >/dev/null || return $?
  close_started=1
  target_repo="$(archived_manifest_value target_repo)" || return 75
  records="$(archived_manifest_entries_with_suffix ".worktree")" || return 75
  while IFS= read -r entry; do
    [[ -n "$entry" ]] || continue
    instance="${entry%%.*}"
    branch="$(archived_manifest_value "$instance.branch")" || return 75
    base_sha="$(archived_manifest_value "$instance.base_sha")" || return 75
    final_sha="$(archived_manifest_value "$instance.final_sha")" || return 75
    branch_head="$(isolated_git -C "$target_repo" rev-parse --verify \
      "refs/heads/$branch" 2>/dev/null || true)"
    if [[ "$final_sha" == "$base_sha" ]]; then
      if [[ -n "$branch_head" && "$branch_head" != "$base_sha" ]]; then
        echo "Refusing archived recovery: unchanged writer ref moved: $branch" >&2
        return 75
      fi
      if [[ "$branch_head" == "$base_sha" ]]; then
        archived_manifest_value feature >/dev/null || return 75
        isolated_git -C "$target_repo" update-ref -d \
          "refs/heads/$branch" "$base_sha" >/dev/null 2>&1 || return 75
      fi
    elif [[ "$branch_head" != "$final_sha" ]]; then
      echo "Refusing archived recovery: published writer ref drifted: $branch" >&2
      return 75
    fi
  done <<< "$records"
  python3 "$leases" end-close "$runs_dir" --feature "$feature" \
    --close-id "$close_id" >/dev/null || return 75
  close_started=0
  python3 "$manifest_guard" clear-archive --runs-dir "$runs_dir" \
    --feature "$feature" --intent "$archive_intent_name" \
    --archive-dir "$archive_dir_name" --digest "$manifest_sha256" \
    --clear-snapshots >/dev/null || return 75
  echo "recovered archived teardown for fleet-$feature"
  return 0
)

set +e
manifest_snapshot_record="$(python3 "$manifest_guard" snapshot \
  --runs-dir "$runs_dir" --feature "$feature")"
snapshot_rc=$?
set -e
if (( snapshot_rc == 1 )); then
  if (( handoff_assurance == 1 )); then
    echo "Refusing assurance handoff: active manifest changed after preflight." >&2
    exit 75
  fi
  set +e
  recover_archived_teardown
  recovery_rc=$?
  set -e
  if (( recovery_rc == 0 )); then
    exit 0
  fi
  if (( recovery_rc != 1 )); then
    exit "$recovery_rc"
  fi
  echo "No manifest at $manifest — nothing to close." >&2
  exit 1
fi
if (( snapshot_rc != 0 )); then
  echo "Refusing teardown: active manifest failed strict descriptor validation." >&2
  exit "$snapshot_rc"
fi
IFS=$'\t' read -r manifest_snapshot_name manifest_snapshot_sha256 \
  <<< "$manifest_snapshot_record"
manifest_snapshot="$runs_dir/$manifest_snapshot_name"
manifest_sha256="$manifest_snapshot_sha256"

close_id=""
close_started=0
cleanup_close() {
  local prior_rc=$?
  trap - EXIT
  release_assurance_handoff_lock
  if (( close_started == 1 )); then
    if ! python3 "$leases" end-close "$runs_dir" --feature "$feature" \
      --close-id "$close_id" >/dev/null; then
      echo "Could not release teardown ownership for fleet '$feature'." >&2
      (( prior_rc == 0 )) && prior_rc=75
    fi
  fi
  if [[ -n "$manifest_snapshot_name" ]]; then
    if ! python3 "$manifest_guard" clear-snapshot --runs-dir "$runs_dir" \
        --feature "$feature" --snapshot "$manifest_snapshot_name" \
        --digest "$manifest_snapshot_sha256" >/dev/null; then
      echo "Could not remove the pinned teardown manifest snapshot." >&2
      (( prior_rc == 0 )) && prior_rc=75
    fi
  fi
  release_fleet_boot_lock
  exit "$prior_rc"
}
trap cleanup_close EXIT
preset="$(manifest_value preset)"
verification_receipt="${manifest%.manifest}.verification-receipt.json"
assurance_receipt="${manifest%.manifest}.assurance-receipt.json"
state_file="${manifest%.manifest}.state.json"
# Validate once more before close ownership.  The snapshot itself is temporary,
# but no close lease, evidence, CMUX, or repository operation may precede this
# exact live-state check.
if ! active_phase="$(python3 "$repo_root/scripts/fleet_state.py" active "$manifest")"; then
  echo "Refusing teardown: fleet phase state is missing, corrupt, or path-unsafe." >&2
  exit 2
fi
mission_id="$(manifest_value mission_id)"
if (( handoff_assurance == 1 )); then
  if [[ -z "$mission_id" || "$mission_id" != "$handoff_preflight_mission_id" ]]; then
    echo "Refusing assurance handoff: pinned manifest Mission binding drifted." >&2
    exit 2
  fi
  set +e
  start_assurance_handoff_lock "$mission_id"
  handoff_hold_rc=$?
  set -e
  if (( handoff_hold_rc != 0 )); then
    echo "Refusing assurance handoff: Mission eligibility could not be held." >&2
    exit "$handoff_hold_rc"
  fi
fi
assurance_required=0
if [[ "$preset" == "fleet_dialogue" && ( "$active_phase" == "CHALLENGE" || "$active_phase" == "VERIFY" ) ]]; then
  assurance_required=1
fi
workspace_uuid="$(manifest_value workspace_uuid)"
close_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
assert_manifest_snapshot
assert_manifest_unchanged
python3 "$leases" begin-close "$runs_dir" --feature "$feature" \
  --close-id "$close_id" --workspace-uuid "$workspace_uuid" >/dev/null || exit $?
close_started=1

handoff_state="$(manifest_value workspace.handoff_state)"
workspace_quiesced="$(manifest_value workspace.quiesced)"
if [[ -n "$handoff_state" && "$handoff_state" != "live" \
    && "$handoff_state" != "closing" && "$handoff_state" != "quiesced" ]]; then
  echo "Refusing teardown: invalid workspace handoff state." >&2
  exit 75
fi
if [[ -n "$workspace_quiesced" && "$workspace_quiesced" != "0" \
    && "$workspace_quiesced" != "1" ]]; then
  echo "Refusing teardown: invalid workspace quiescence marker." >&2
  exit 75
fi
if [[ "$handoff_state" == "live" && "$workspace_quiesced" == "1" ]]; then
  echo "Refusing teardown: live workspace cannot be marked quiesced." >&2
  exit 75
fi

workspace_already_absent=0
assert_manifest_snapshot
assert_manifest_unchanged
if [[ "$handoff_state" == "quiesced" || "$workspace_quiesced" == "1" ]]; then
  set +e
  python3 "$identity" exists "$manifest_snapshot" >/dev/null 2>&1
  identity_rc=$?
  set -e
  if (( identity_rc == 1 )); then
    workspace_already_absent=1
  elif (( identity_rc == 0 )); then
    echo "Refusing teardown: a quiesced workspace is unexpectedly live." >&2
    exit 2
  else
    echo "Refusing teardown: quiesced workspace absence could not be confirmed." >&2
    exit 2
  fi
else
  set +e
  python3 "$identity" validate "$manifest_snapshot" >/dev/null 2>&1
  identity_rc=$?
  set -e
  if (( identity_rc != 0 )); then
    if (( recover_absent == 1 )) || [[ "$handoff_state" == "closing" ]]; then
      set +e
      assert_manifest_snapshot >/dev/null 2>&1
      snapshot_identity_rc=$?
      if (( snapshot_identity_rc == 0 )); then
        python3 "$identity" exists "$manifest_snapshot" >/dev/null 2>&1
        identity_rc=$?
      else
        identity_rc="$snapshot_identity_rc"
      fi
      set -e
      if (( identity_rc == 1 )); then
        workspace_already_absent=1
      else
        echo "Refusing recovery: workspace absence was not confirmed." >&2
        exit 2
      fi
    else
      echo "Refusing teardown: manifest identity does not match cmux tree." >&2
      echo "If the workspace was intentionally removed, retry with --recover-absent." >&2
      exit 2
    fi
  fi
fi

ws_ref="$(manifest_value workspace)"
if [[ "$preset" == "fleet_dialogue" ]]; then
  if (( workspace_already_absent == 0 )); then
    python3 "$dialogue_controller" verify "$runs_dir" --feature "$feature" \
      --require-terminal --write-receipt "$verification_receipt" >/dev/null
  fi
fi
if (( assurance_required == 1 )); then
  if (( workspace_already_absent == 0 )); then
    python3 "$assurance_controller" verify "$runs_dir" --feature "$feature" \
      --require-terminal --write-receipt "$assurance_receipt" >/dev/null
  fi
fi
audit_required=0
if [[ "$(manifest_value mode)" == "assured" && -n "$mission_id" ]]; then
  audit_required=1
fi

# Every mission reader must still be the physically read-only, detached clone
# of the frozen base recorded in the pinned manifest. Reader clones are never
# publication inputs; after CMUX is absent CONTROL stages, revalidates, and
# removes them before considering any writer publication.
target_repo="$(manifest_value target_repo)"
reader_entries=()
reader_instances=()
reader_paths=()
reader_bases=()
reader_staging_paths=()
if ! reader_records="$(manifest_entries_with_suffix ".workspace")"; then
  echo "Refusing teardown: pinned reader workspace roster could not be read." >&2
  exit 75
fi
while IFS= read -r entry; do
  [[ -n "$entry" ]] && reader_entries+=("$entry")
done <<< "$reader_records"

# Private writer clones must be clean and isolated before CONTROL can quiesce
# the workspace. The target branch is intentionally absent until the second,
# post-shutdown verification succeeds.
worktree_entries=()
worktree_instances=()
worktree_paths=()
worktree_branches=()
worktree_bases=()
worktree_states=()
worktree_staging_paths=()
if ! worktree_records="$(manifest_entries_with_suffix ".worktree")"; then
  echo "Refusing teardown: pinned writer roster could not be read." >&2
  exit 75
fi
while IFS= read -r entry; do
  [[ -n "$entry" ]] && worktree_entries+=("$entry")
done <<< "$worktree_records"
if (( ${#reader_entries[@]} > 0 || ${#worktree_entries[@]} > 0 )); then
  if ! worktrees_root="$(python3 -c '
import os, stat, sys
from pathlib import Path

root_arg = Path(sys.argv[1]).expanduser()
info = root_arg.lstat()
if (
    not stat.S_ISDIR(info.st_mode)
    or stat.S_ISLNK(info.st_mode)
    or info.st_uid != os.geteuid()
    or stat.S_IMODE(info.st_mode) != 0o700
):
    raise SystemExit(1)
root = root_arg.resolve(strict=True)
for raw in sys.argv[2:]:
    if not raw:
        continue
    protected = Path(raw).expanduser().resolve(strict=True)
    if root == protected or protected in root.parents or root in protected.parents:
        raise SystemExit(1)
print(root)
' "$worktrees_root" "$repo_root" "$runs_dir" "$target_repo")"; then
    echo "Refusing teardown: isolated specialist root is unavailable." >&2
    exit 75
  fi
fi
boot_cleanup_id=""
if [[ -n "$target_repo" ]]; then
  boot_cleanup_id="$(python3 -c '
import sys, uuid
print(str(uuid.uuid5(uuid.NAMESPACE_URL, "fleet-failed-boot:" + "\x1f".join(sys.argv[1:]))).upper())
' "$worktrees_root" "$target_repo" "$feature")"
fi
for entry in ${reader_entries[@]+"${reader_entries[@]}"}; do
  reader_path="${entry#*=}"
  instance="${entry%%.*}"
  if [[ ! "$instance" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
    echo "Refusing teardown: invalid reader instance '$instance'." >&2
    exit 75
  fi
  base_sha="$(manifest_value "$instance.workspace_base_sha")"
  workspace_kind="$(manifest_value "$instance.workspace_kind")"
  workspace_publication="$(manifest_value "$instance.workspace_publication")"
  staging_path="$(staging_path_for "$instance")"
  expected_reader_path="$worktrees_root/$feature-$instance"
  if [[ "$reader_path" != "$expected_reader_path" ]]; then
    echo "Refusing teardown: reader workspace escaped the isolated specialist root." >&2
    exit 75
  fi
  if [[ -z "$target_repo" || "$base_sha" != "$(manifest_value base_sha)" \
      || "$workspace_kind" != "isolated-read-clone" \
      || "$workspace_publication" != "none" ]]; then
    echo "Refusing teardown: incomplete reader workspace metadata for '$instance'." >&2
    exit 75
  fi
  validation_clone="$reader_path"
  if (( workspace_already_absent == 1 )); then
    if [[ ( -e "$reader_path" || -L "$reader_path" ) \
        && ( -e "$staging_path" || -L "$staging_path" ) ]]; then
      echo "Refusing teardown: reader exists in both its authorized and CONTROL staging paths." >&2
      exit 75
    fi
    if [[ ! -e "$reader_path" && ! -L "$reader_path" ]]; then
      validation_clone="$staging_path"
    fi
    if [[ ! -e "$validation_clone" && ! -L "$validation_clone" ]]; then
      reader_instances+=("$instance")
      reader_paths+=("$reader_path")
      reader_bases+=("$base_sha")
      reader_staging_paths+=("$staging_path")
      continue
    fi
  else
    if [[ -e "$staging_path" || -L "$staging_path" ]]; then
      echo "Refusing teardown: CONTROL reader staging exists while the workspace is live." >&2
      exit 75
    fi
    if [[ ! -e "$reader_path" && ! -L "$reader_path" ]]; then
      echo "Refusing teardown: reader workspace disappeared while CMUX is live." >&2
      exit 75
    fi
  fi
  retirement_path="$(clone_retirement_path_for "$instance")"
  if (( workspace_already_absent == 1 )) \
      && [[ -e "$retirement_path" || -L "$retirement_path" ]]; then
    if ! clone_retirement require reader "$instance" "$base_sha"; then
      echo "Refusing teardown: reader retirement tombstone drifted for '$instance'." >&2
      exit 75
    fi
    reader_instances+=("$instance")
    reader_paths+=("$reader_path")
    reader_bases+=("$base_sha")
    reader_staging_paths+=("$staging_path")
    continue
  fi
  stage_intent_path="$(clone_stage_intent_path_for "$instance")"
  if (( workspace_already_absent == 1 )) \
      && [[ -e "$stage_intent_path" || -L "$stage_intent_path" ]]; then
    if ! stage_isolated_clone "$reader_path" "$staging_path" reader \
        "$instance" "$base_sha" >/dev/null; then
      echo "Refusing teardown: reader stage intent could not be reconciled." >&2
      exit 75
    fi
    validation_clone="$staging_path"
  fi
  set +e
  reader_head="$(private_reader_head "$validation_clone" "$base_sha")"
  reader_rc=$?
  set -e
  case "$reader_rc" in
    0) ;;
    10)
      echo "Refusing teardown: reader clone is not physically isolated: $validation_clone" >&2
      exit 75
      ;;
    11)
      echo "Refusing teardown: reader clone is not detached: $validation_clone" >&2
      exit 75
      ;;
    12)
      echo "Refusing teardown: reader HEAD drifted from frozen base '$base_sha'." >&2
      exit 75
      ;;
    13)
      echo "Refusing teardown: reader snapshot contains filesystem changes: $validation_clone" >&2
      exit 75
      ;;
    14)
      echo "Refusing teardown: reader snapshot is writable or contains unsafe Git metadata." >&2
      exit 75
      ;;
    *)
      echo "Refusing teardown: reader verification failed for '$instance'." >&2
      exit 75
      ;;
  esac
  if [[ "$reader_head" != "$base_sha" ]]; then
    echo "Refusing teardown: reader snapshot does not match its frozen base." >&2
    exit 75
  fi
  reader_instances+=("$instance")
  reader_paths+=("$reader_path")
  reader_bases+=("$base_sha")
  reader_staging_paths+=("$staging_path")
done
for entry in ${worktree_entries[@]+"${worktree_entries[@]}"}; do
  wt="${entry#*=}"
  instance="${entry%%.*}"
  if [[ ! "$instance" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
    echo "Refusing teardown: invalid writer instance '$instance'." >&2
    exit 75
  fi
  branch="$(manifest_value "$instance.branch")"
  base_sha="$(manifest_value "$instance.base_sha")"
  publication_state="$(manifest_value "$instance.publication_state")"
  staging_path="$(staging_path_for "$instance")"
  intent_path="$(publication_intent_path_for "$instance")"
  if [[ "$wt" != "$worktrees_root/$feature-$instance" ]]; then
    echo "Refusing teardown: writer worktree escaped the isolated specialist root." >&2
    exit 75
  fi
  if [[ -z "$target_repo" || -z "$branch" || -z "$base_sha" ]]; then
    echo "Refusing teardown: incomplete writer git metadata for '$instance'." >&2
    exit 75
  fi
  if [[ "$publication_state" == "published" ]]; then
    final_sha="$(manifest_value "$instance.final_sha")"
    published_sha="$(manifest_value "$instance.published_sha")"
    branch_head="$(isolated_git -C "$target_repo" rev-parse --verify "refs/heads/$branch" 2>/dev/null || true)"
    if [[ -z "$final_sha" || "$published_sha" != "$final_sha" || "$branch_head" != "$final_sha" ]]; then
      echo "Refusing teardown: published writer branch drifted for '$instance'." >&2
      exit 75
    fi
    if (( workspace_already_absent == 0 )); then
      echo "Refusing teardown: writer is marked published while its workspace is live." >&2
      exit 75
    fi
    if [[ -e "$wt" || -L "$wt" ]]; then
      echo "Refusing teardown: published writer remained at its model-authorized path: $wt" >&2
      exit 75
    fi
    if [[ -e "$staging_path" || -L "$staging_path" ]]; then
      retirement_path="$(clone_retirement_path_for "$instance")"
      if [[ -e "$retirement_path" || -L "$retirement_path" ]]; then
        if ! clone_retirement require writer "$instance" "$final_sha" "$branch"; then
          echo "Refusing teardown: writer retirement tombstone drifted for '$instance'." >&2
          exit 75
        fi
      else
        set +e
        staged_head="$(private_writer_head "$staging_path" "$branch" "$base_sha")"
        writer_rc=$?
        set -e
        if (( writer_rc != 0 )) || [[ "$staged_head" != "$final_sha" ]]; then
          echo "Refusing teardown: residual CONTROL staging clone does not match published SHA." >&2
          exit 75
        fi
      fi
    fi
    if [[ -e "$intent_path" || -L "$intent_path" ]]; then
      if ! publication_intent require "$intent_path" "$instance" "$branch" \
          "$base_sha" "$final_sha" "$staging_path"; then
        echo "Refusing teardown: published writer intent drifted for '$instance'." >&2
        exit 75
      fi
    fi
  else
    if [[ "$publication_state" != "private" ]]; then
      echo "Refusing teardown: invalid writer publication state for '$instance'." >&2
      exit 75
    fi
    validation_clone="$wt"
    if (( workspace_already_absent == 1 )); then
      if [[ ( -e "$wt" || -L "$wt" ) && ( -e "$staging_path" || -L "$staging_path" ) ]]; then
        echo "Refusing teardown: writer exists in both its authorized and CONTROL staging paths." >&2
        exit 75
      fi
      if [[ ! -e "$wt" && ! -L "$wt" ]]; then
        validation_clone="$staging_path"
      fi
    elif [[ -e "$staging_path" || -L "$staging_path" ]]; then
      echo "Refusing teardown: CONTROL staging exists while the writer workspace is live." >&2
      exit 75
    fi
    if (( workspace_already_absent == 0 )) && [[ -e "$intent_path" || -L "$intent_path" ]]; then
      echo "Refusing teardown: publication intent exists while the writer workspace is live." >&2
      exit 75
    fi
    set +e
    worktree_head="$(private_writer_head "$validation_clone" "$branch" "$base_sha")"
    writer_rc=$?
    set -e
    case "$writer_rc" in
      0) ;;
      10)
        echo "Refusing teardown: writer clone is not physically isolated: $validation_clone" >&2
        exit 75
        ;;
      11)
        echo "Refusing teardown: writer worktree is not attached to '$branch': $validation_clone" >&2
        exit 75
        ;;
      12)
        echo "Refusing teardown: writer HEAD is not descended from base '$base_sha'." >&2
        exit 75
        ;;
      13)
        echo "Refusing teardown: worktree for '$instance' has uncommitted changes: $validation_clone" >&2
        echo "Commit the work in the isolated writer clone or discard it, then retry." >&2
        exit 75
        ;;
    14)
      echo "Refusing teardown: writer clone contains hardlinks or unsafe Git metadata." >&2
      exit 75
        ;;
      *)
        echo "Refusing teardown: writer verification failed for '$instance'." >&2
        exit 75
        ;;
    esac
    branch_head="$(isolated_git -C "$target_repo" rev-parse --verify "refs/heads/$branch" 2>/dev/null || true)"
    if [[ -e "$intent_path" || -L "$intent_path" ]]; then
      if ! publication_intent require "$intent_path" "$instance" "$branch" \
          "$base_sha" "$worktree_head" "$staging_path"; then
        echo "Refusing teardown: writer publication intent drifted for '$instance'." >&2
        exit 75
      fi
    fi
    if [[ -n "$branch_head" ]]; then
      if (( workspace_already_absent == 0 )) \
          || [[ "$branch_head" != "$worktree_head" ]] \
          || [[ ! -f "$intent_path" || -L "$intent_path" ]]; then
        echo "Refusing teardown: private writer publication cannot be reconciled: $branch" >&2
        exit 75
      fi
    fi
    manifest_final="$(manifest_value "$instance.final_sha")"
    manifest_published="$(manifest_value "$instance.published_sha")"
    if [[ -n "$manifest_final" && "$manifest_final" != "$base_sha" && "$manifest_final" != "$worktree_head" ]]; then
      echo "Refusing teardown: partial writer final_sha metadata drifted for '$instance'." >&2
      exit 75
    fi
    if [[ -n "$manifest_published" && "$manifest_published" != "$worktree_head" ]]; then
      echo "Refusing teardown: partial writer published_sha metadata drifted for '$instance'." >&2
      exit 75
    fi
  fi
  worktree_instances+=("$instance")
  worktree_paths+=("$wt")
  worktree_branches+=("$branch")
  worktree_bases+=("$base_sha")
  worktree_states+=("$publication_state")
  worktree_staging_paths+=("$staging_path")
done

# A mission archive is never created while a writer pane remains mutable.
if [[ -n "$mission_id" && ! -d "$runs_dir/missions/$mission_id" ]]; then
  echo "Refusing teardown: mission directory is absent for $mission_id." >&2
  exit 75
fi
if [[ -n "$mission_id" ]]; then
  mission_status="$(PYTHONPATH="$repo_root/scripts" python3 -c '
import pathlib, sys
import fleet_mission
print(fleet_mission.load_state(pathlib.Path(sys.argv[1]), sys.argv[2])["status"])
' "$runs_dir" "$mission_id")"
  if (( handoff_assurance == 1 )); then
    if [[ "$mission_status" != "assurance_approved" ]]; then
      echo "Refusing assurance handoff: mission $mission_id is not assurance_approved (status=$mission_status)." >&2
      exit 2
    fi
  elif (( prepare_archive == 1 )); then
    case "$mission_status" in
      completing|archived) ;;
      *)
        echo "Refusing archive preparation: mission $mission_id is not completing (status=$mission_status)." >&2
        exit 75
        ;;
    esac
  else
    case "$mission_status" in
      succeeded|failed|blocked|abandoned|indeterminate) ;;
      *)
        echo "Refusing teardown: mission $mission_id is not terminal (status=$mission_status)." >&2
        exit 75
        ;;
    esac
  fi
  if ! python3 "$control_client" --runs-dir "$runs_dir" \
      --mission-id "$mission_id" --preset "$preset" \
      stop-if-present >/dev/null; then
    echo "Refusing teardown: Fleet Control lifecycle could not be safely reconciled." >&2
    exit 75
  fi
fi

set_manifest_value "workspace.handoff_state" "closing"
assert_manifest_snapshot
assert_manifest_unchanged
if (( workspace_already_absent == 0 )); then
  cmux close-workspace --workspace "$ws_ref" >/dev/null
fi
for _ in 1 2 3 4 5 6 7 8 9 10; do
  assert_manifest_snapshot
  assert_manifest_unchanged
  set +e
  python3 "$identity" exists "$manifest_snapshot" >/dev/null 2>&1
  identity_rc=$?
  set -e
  if (( identity_rc == 1 )); then
    if (( assurance_required == 1 && workspace_already_absent == 0 )); then
      python3 "$assurance_controller" verify "$runs_dir" --feature "$feature" \
        --cleanup-snapshots >/dev/null
    fi
    for ((i=0; i<${#reader_paths[@]}; i++)); do
      instance="${reader_instances[$i]}"
      reader_path="${reader_paths[$i]}"
      base_sha="${reader_bases[$i]}"
      staging_path="${reader_staging_paths[$i]}"
      bound_reader="$(manifest_value "$instance.workspace")"
      bound_kind="$(manifest_value "$instance.workspace_kind")"
      bound_base="$(manifest_value "$instance.workspace_base_sha")"
      bound_publication="$(manifest_value "$instance.workspace_publication")"
      if [[ "$bound_reader" != "$reader_path" \
          || "$bound_kind" != "isolated-read-clone" \
          || "$bound_base" != "$base_sha" \
          || "$bound_publication" != "none" ]]; then
        echo "Refusing reader retirement: pinned workspace binding changed." >&2
        exit 75
      fi
      assert_manifest_snapshot
      assert_manifest_unchanged
      if [[ ( -e "$reader_path" || -L "$reader_path" ) \
          && ( -e "$staging_path" || -L "$staging_path" ) ]]; then
        echo "Refusing reader retirement: authorized and CONTROL staging paths both exist." >&2
        exit 75
      fi
      if [[ -e "$reader_path" || -L "$reader_path" ]]; then
        if ! stage_isolated_clone "$reader_path" "$staging_path" reader \
            "$instance" "$base_sha" >/dev/null; then
          echo "Refusing reader retirement: staged snapshot changed or CONTROL could not atomically stage it." >&2
          exit 75
        fi
        publication_checkpoint "after_reader_stage"
      fi
      retirement_path="$(clone_retirement_path_for "$instance")"
      if [[ -e "$retirement_path" || -L "$retirement_path" ]]; then
        if ! clone_retirement require reader "$instance" "$base_sha"; then
          echo "Refusing reader retirement: durable tombstone drifted." >&2
          exit 75
        fi
      elif [[ -e "$staging_path" || -L "$staging_path" ]]; then
        set +e
        reader_head="$(private_reader_head "$staging_path" "$base_sha")"
        reader_rc=$?
        set -e
        if (( reader_rc != 0 )) || [[ "$reader_head" != "$base_sha" ]]; then
          echo "Refusing reader retirement: staged snapshot changed after workspace shutdown (check=$reader_rc)." >&2
          echo "Preserved manifest and CONTROL staging clone for recovery." >&2
          exit 75
        fi
        assert_manifest_snapshot
        assert_manifest_unchanged
        if ! clone_retirement ensure reader "$instance" "$base_sha"; then
          echo "Refusing reader retirement: durable deletion tombstone could not be established." >&2
          exit 75
        fi
      fi
      if [[ -e "$retirement_path" || -L "$retirement_path" ]]; then
        if ! clone_retirement remove reader "$instance" "$base_sha"; then
          echo "Refusing reader retirement: could not remove CONTROL staging snapshot $staging_path." >&2
          exit 75
        fi
        publication_checkpoint "after_reader_remove"
        if ! clone_retirement clear reader "$instance" "$base_sha"; then
          echo "Refusing reader retirement: exact deletion tombstone could not be cleared." >&2
          exit 75
        fi
      fi
      if [[ -e "$reader_path" || -L "$reader_path" \
          || -e "$staging_path" || -L "$staging_path" ]]; then
        echo "Refusing successful teardown: reader snapshot remains reachable for '$instance'." >&2
        exit 75
      fi
      if ! clear_clone_stage_intent reader "$instance" "$base_sha"; then
        echo "Refusing reader retirement: exact stage intent could not be cleared." >&2
        exit 75
      fi
      if ! clear_clone_creation_intent reader "$instance" "$base_sha"; then
        echo "Refusing reader retirement: exact creation intent could not be cleared." >&2
        exit 75
      fi
    done
    for ((i=0; i<${#worktree_paths[@]}; i++)); do
      instance="${worktree_instances[$i]}"
      wt="${worktree_paths[$i]}"
      branch="${worktree_branches[$i]}"
      base_sha="${worktree_bases[$i]}"
      publication_state="${worktree_states[$i]}"
      staging_path="${worktree_staging_paths[$i]}"
      intent_path="$(publication_intent_path_for "$instance")"
      bound_target="$(manifest_value target_repo)"
      bound_branch="$(manifest_value "$instance.branch")"
      bound_base="$(manifest_value "$instance.base_sha")"
      if [[ "$bound_target" != "$target_repo" || "$bound_branch" != "$branch" \
          || "$bound_base" != "$base_sha" ]]; then
        echo "Refusing publication: pinned repository binding changed." >&2
        exit 75
      fi
      assert_manifest_snapshot
      assert_manifest_unchanged
      if [[ "$publication_state" == "private" ]]; then
        if ! stage_isolated_clone "$wt" "$staging_path" writer \
            "$instance" "$base_sha" "$branch" >/dev/null; then
          echo "Refusing publication: CONTROL could not atomically retire the writer clone." >&2
          echo "Preserved manifest and clone for manual recovery." >&2
          exit 75
        fi
        set +e
        final_sha="$(private_writer_head "$staging_path" "$branch" "$base_sha")"
        writer_rc=$?
        set -e
        if (( writer_rc != 0 )); then
          echo "Refusing publication after workspace shutdown: staged writer changed unexpectedly (check=$writer_rc): $staging_path" >&2
          echo "Preserved manifest and CONTROL staging clone for manual recovery." >&2
          exit 75
        fi
        if ! publication_intent ensure "$intent_path" "$instance" "$branch" \
            "$base_sha" "$final_sha" "$staging_path"; then
          echo "Refusing publication: durable CONTROL intent could not be established." >&2
          exit 75
        fi
        published_head="$(isolated_git -C "$target_repo" rev-parse --verify "refs/heads/$branch" 2>/dev/null || true)"
        if [[ -z "$published_head" ]]; then
          if ! publish_isolated_branch "$staging_path" "$branch" "$final_sha" \
              "$base_sha" "$instance"; then
            echo "Refusing publication after workspace shutdown: target branch was created or import failed: $branch" >&2
            echo "Preserved manifest and CONTROL staging clone for manual recovery." >&2
            exit 75
          fi
          publication_checkpoint "after_update_ref"
        elif [[ "$published_head" != "$final_sha" ]]; then
          echo "Refusing publication recovery: target branch drifted from durable intent." >&2
          exit 75
        fi
        published_head="$(isolated_git -C "$target_repo" rev-parse --verify "refs/heads/$branch" 2>/dev/null || true)"
        if [[ "$published_head" != "$final_sha" ]]; then
          echo "Refusing cleanup: published writer branch does not match the quiescent clone." >&2
          exit 75
        fi
        set_manifest_value "$instance.final_sha" "$final_sha"
        publication_checkpoint "after_final_sha"
        set_manifest_value "$instance.published_sha" "$final_sha"
        publication_checkpoint "after_published_sha"
        set_manifest_value "$instance.publication_state" "published"
        publication_checkpoint "after_publication_state"
      else
        final_sha="$(manifest_value "$instance.final_sha")"
        published_head="$(isolated_git -C "$target_repo" rev-parse --verify "refs/heads/$branch" 2>/dev/null || true)"
        if [[ -z "$final_sha" || "$published_head" != "$final_sha" ]]; then
          echo "Refusing cleanup: published writer branch drifted after quiescence: $branch" >&2
          exit 75
        fi
        if [[ -e "$staging_path" || -L "$staging_path" ]]; then
          retirement_path="$(clone_retirement_path_for "$instance")"
          if [[ -e "$retirement_path" || -L "$retirement_path" ]]; then
            if ! clone_retirement require writer "$instance" "$final_sha" "$branch"; then
              echo "Refusing cleanup: writer retirement tombstone drifted." >&2
              exit 75
            fi
          else
            set +e
            clone_head="$(private_writer_head "$staging_path" "$branch" "$base_sha")"
            writer_rc=$?
            set -e
            if (( writer_rc != 0 )) || [[ "$clone_head" != "$final_sha" ]]; then
              echo "Refusing cleanup: residual CONTROL staging clone does not match published SHA: $staging_path" >&2
              exit 75
            fi
          fi
        fi
      fi
      if [[ -e "$wt" || -L "$wt" ]]; then
        echo "Refusing successful teardown: writer clone returned to its model-authorized path $wt." >&2
        exit 75
      fi
      retirement_path="$(clone_retirement_path_for "$instance")"
      if [[ -e "$staging_path" || -L "$staging_path" ]]; then
        if ! clone_retirement ensure writer "$instance" "$final_sha" "$branch"; then
          echo "Refusing successful teardown: writer deletion tombstone could not be established." >&2
          exit 75
        fi
      fi
      if [[ -e "$retirement_path" || -L "$retirement_path" ]]; then
        if ! clone_retirement remove writer "$instance" "$final_sha" "$branch"; then
          echo "Refusing successful teardown: could not remove CONTROL staging clone $staging_path." >&2
          echo "Published branch and manifest were preserved for idempotent recovery." >&2
          exit 75
        fi
      fi
      if [[ -e "$intent_path" || -L "$intent_path" ]] \
          && ! publication_intent clear "$intent_path" "$instance" "$branch" \
            "$base_sha" "$final_sha" "$staging_path"; then
        echo "Refusing successful teardown: could not clear exact publication intent." >&2
        exit 75
      fi
      if [[ -e "$retirement_path" || -L "$retirement_path" ]] \
          && ! clone_retirement clear writer "$instance" "$final_sha" "$branch"; then
        echo "Refusing successful teardown: could not clear exact writer deletion tombstone." >&2
        exit 75
      fi
      if ! clear_clone_stage_intent writer "$instance" "$base_sha" "$branch"; then
        echo "Refusing successful teardown: could not clear exact writer stage intent." >&2
        exit 75
      fi
      if ! clear_clone_creation_intent writer "$instance" "$base_sha" "$branch"; then
        echo "Refusing successful teardown: could not clear exact writer creation intent." >&2
        exit 75
      fi
    done
    set_manifest_value "workspace.quiesced" "1"
    set_manifest_value "workspace.handoff_state" "quiesced"
    if (( workspace_already_absent == 1 )) && [[ "$preset" == "fleet_dialogue" ]]; then
      if ! python3 "$dialogue_controller" verify "$runs_dir" --feature "$feature" \
          --require-terminal --write-receipt "$verification_receipt" >/dev/null; then
        echo "Refusing FDP-2 recovery: published verification receipt is absent or invalid." >&2
        exit 75
      fi
    fi
    if (( workspace_already_absent == 1 && assurance_required == 1 )); then
      if ! python3 "$assurance_controller" verify "$runs_dir" --feature "$feature" \
          --require-terminal --cleanup-snapshots \
          --write-receipt "$assurance_receipt" >/dev/null; then
        echo "Refusing FDP-3 recovery: published assurance receipt is absent or invalid." >&2
        exit 75
      fi
    fi
    if (( prepare_archive == 1 )); then
      echo "prepared $ws_ref (fleet-$feature) for archive"
      exit 0
    fi
    if (( handoff_assurance == 1 )); then
      set +e
      commit_assurance_handoff_lock
      handoff_commit_rc=$?
      set -e
      if (( handoff_commit_rc != 0 )); then
        echo "Assurance handoff commit is incomplete; retry the same command." >&2
        exit "$handoff_commit_rc"
      fi
      python3 "$leases" end-close "$runs_dir" --feature "$feature" \
        --close-id "$close_id" >/dev/null
      close_started=0
      python3 "$manifest_guard" clear-snapshot --runs-dir "$runs_dir" \
        --feature "$feature" --snapshot "$manifest_snapshot_name" \
        --digest "$manifest_snapshot_sha256" >/dev/null
      manifest_snapshot_name=""
      printf '%s\n' "$handoff_result"
      exit 0
    fi
    if (( audit_required == 1 )); then
      python3 "$audit_client" --runs-dir "$runs_dir" --mission-id "$mission_id" verify >/dev/null
    fi
    if [[ -n "$mission_id" ]]; then
      mission_archive="$runs_dir/missions/$mission_id/archive"
      python3 "$archive_client" create --runs-dir "$runs_dir" --mission-id "$mission_id" \
        --manifest "$manifest" --output "$mission_archive" >/dev/null
      python3 "$archive_client" verify "$mission_archive" --repo "$target_repo" >/dev/null
    fi
    if (( audit_required == 1 )); then
      python3 "$audit_client" --runs-dir "$runs_dir" --mission-id "$mission_id" stop >/dev/null
    fi
    archive_record="$(python3 "$manifest_guard" prepare-archive \
      --runs-dir "$runs_dir" --feature "$feature" --digest "$manifest_sha256")"
    IFS=$'\t' read -r archive_dir_name archive_intent_name <<< "$archive_record"
    archive="$runs_dir/archive/$archive_dir_name"
    [[ -f "$state_file" ]] && mv "$state_file" "$archive/state.json"
    ledger_file="${manifest%.manifest}.ledger.jsonl"
    [[ -f "$ledger_file" ]] && mv "$ledger_file" "$archive/ledger.jsonl"
    dialogue_ledger="${manifest%.manifest}.dialogue.jsonl"
    [[ -f "$dialogue_ledger" ]] && mv "$dialogue_ledger" "$archive/dialogue.jsonl"
    dialogue_control="${manifest%.manifest}.dialogue-control.jsonl"
    [[ -f "$dialogue_control" ]] && mv "$dialogue_control" "$archive/dialogue-control.jsonl"
    assurance_control="${manifest%.manifest}.assurance-control.jsonl"
    [[ -f "$assurance_control" ]] && mv "$assurance_control" "$archive/assurance-control.jsonl"
    [[ -f "$verification_receipt" ]] && mv "$verification_receipt" "$archive/verification-receipt.json"
    [[ -f "$assurance_receipt" ]] && mv "$assurance_receipt" "$archive/assurance-receipt.json"
    dialogue_store="$runs_dir/dialogue/$feature"
    [[ -d "$dialogue_store" ]] && mv "$dialogue_store" "$archive/dialogue"
    assurance_store="$runs_dir/assurance/$feature"
    [[ -d "$assurance_store" ]] && mv "$assurance_store" "$archive/assurance"
    # The active manifest is the archive commit marker. Move it only after all
    # sidecars, then use the durable intent for post-move crash recovery.
    assert_manifest_unchanged
    mv "$manifest" "$archive/manifest"
    publication_checkpoint "after_manifest_archive"
    # Unchanged public refs are only a cleanup convenience. Delete them last,
    # after the authoritative manifest has reached its archive, so a crash
    # cannot leave an active published manifest whose required ref vanished.
    for ((i=0; i<${#worktree_paths[@]}; i++)); do
      instance="${worktree_instances[$i]}"
      branch="${worktree_branches[$i]}"
      base_sha="${worktree_bases[$i]}"
      archived_target="$(archived_manifest_value target_repo)"
      archived_branch="$(archived_manifest_value "$instance.branch")"
      archived_base="$(archived_manifest_value "$instance.base_sha")"
      final_sha="$(archived_manifest_value "$instance.final_sha")"
      if [[ "$archived_target" != "$target_repo" || "$archived_branch" != "$branch" \
          || "$archived_base" != "$base_sha" ]]; then
        echo "Refusing final cleanup: archived writer binding drifted for $instance." >&2
        exit 75
      fi
      if [[ "$final_sha" == "$base_sha" ]] && \
          ! archived_manifest_value feature >/dev/null; then
        echo "Refusing final cleanup: archived manifest changed before ref deletion." >&2
        exit 75
      fi
      if [[ "$final_sha" == "$base_sha" ]]; then
        if ! isolated_git -C "$target_repo" update-ref -d \
            "refs/heads/$branch" "$base_sha" >/dev/null 2>&1; then
          echo "Refusing final cleanup: unchanged published writer branch moved; preserved $branch" >&2
          exit 75
        fi
      fi
    done
    python3 "$leases" end-close "$runs_dir" --feature "$feature" \
      --close-id "$close_id" >/dev/null
    close_started=0
    python3 "$manifest_guard" clear-archive --runs-dir "$runs_dir" \
      --feature "$feature" --intent "$archive_intent_name" \
      --archive-dir "$archive_dir_name" --digest "$manifest_sha256" >/dev/null
    echo "closed $ws_ref (fleet-$feature)"
    exit 0
  fi
  if (( identity_rc > 1 )); then
    probe_failed=1
  fi
  sleep 0.1
done

if [[ "${probe_failed:-0}" == "1" ]]; then
  echo "Could not confirm cmux shutdown because identity probes failed; manifest preserved." >&2
else
  echo "cmux reported success but $ws_ref is still present; manifest preserved." >&2
fi
exit 1
