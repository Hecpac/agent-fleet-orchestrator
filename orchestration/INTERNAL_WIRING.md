# Orchestration Internal Wiring

This file records invariants that the fleet runtime must preserve across
refactors. Each rule names the tests that lock the behavior and the regression
that would reopen if the rule disappeared.

## Durable writer branches

`rule: writer_work_survives_worktree_teardown`

`enforced_by:`

- `tests/test_fleet_up.py::FleetUpTests::test_writer_gets_named_branch_from_target_head`
- `tests/test_fleet_up.py::FleetUpTests::test_existing_writer_branch_fails_before_cmux`
- `tests/test_fleet_up.py::FleetUpTests::test_teardown_preserves_advanced_writer_branch_and_archives_final_sha`
- `tests/test_fleet_up.py::FleetUpTests::test_teardown_refuses_writer_detached_from_durable_branch`
- `tests/test_fleet_up.py::FleetUpTests::test_teardown_rechecks_writer_after_workspace_shutdown`
- `tests/test_fleet_up.py::FleetUpTests::test_teardown_preserves_state_when_shutdown_probe_fails`
- `tests/test_fleet_identity.py::FleetIdentityProbeTests::test_exists_reserves_exit_one_for_confirmed_absence`
- `tests/test_fleet_up.py::FleetUpTests::test_failed_boot_removes_unchanged_writer_branch`
- `tests/test_fleet_up.py::FleetUpTests::test_failed_boot_preserves_writer_branch_that_advanced_during_shutdown`

`why:` Every write-authority instance must start on the dedicated branch
`fleet/<feature>/<instance>` at the target repository's current `HEAD`. Fleet
boot fails closed if that branch already exists. The manifest records branch,
base SHA, and final SHA. Teardown may delete the branch only when its final SHA
still equals its base SHA, and only while the worktree remains attached to that
branch. An advanced branch is preserved after its worktree is removed. This
prevents clean committed agent output from becoming unreachable when a fleet
closes.

## Exact local completion

`rule: local_run_completion_is_run_id_scoped`

`enforced_by:`

- `tests/test_fleet_wait.py::FleetWaitLedgerAuthorityTests::test_spurious_notification_is_ignored_until_exact_run_is_terminal`
- `tests/test_fleet_wait.py::FleetWaitLedgerAuthorityTests::test_terminal_before_subscription_is_reconciled_after_ack`
- `tests/test_fleet_wait.py::FleetWaitLedgerAuthorityTests::test_wait_does_not_accept_an_older_terminal_run`
- `tests/test_fleet_wait.py::FleetWaitLedgerAuthorityTests::test_any_continues_after_failure_until_first_success_and_emits_json`
- `tests/test_fleet_wait.py::FleetWaitLedgerAuthorityTests::test_any_uses_ledger_time_when_successes_precede_subscription`

`why:` A local wait must name `instance=run_id`. The cmux stream is armed with
an ACK, but notifications and heartbeats only wake reconciliation; they never
prove completion. The terminal status comes from the latest ledger event for
that exact instance and run. This closes both the fast-finisher subscription
race and the stale-completion race from an older run on the same pane.

## Ownership-safe local leases

`rule: local_lease_reclaim_requires_positive_evidence`

`enforced_by:`

- `tests/test_fleet_leases.py::FleetLeaseTests::test_terminal_run_is_quarantined_idempotently`
- `tests/test_fleet_leases.py::FleetLeaseTests::test_confirmed_absent_run_is_abandoned_before_quarantine`
- `tests/test_fleet_leases.py::FleetLeaseTests::test_probe_error_preserves_nonterminal_leases`
- `tests/test_fleet_leases.py::FleetLeaseTests::test_unparseable_tree_output_fails_closed_without_reclaim`
- `tests/test_fleet_leases.py::FleetLeaseTests::test_old_owner_cannot_release_reassigned_lease`
- `tests/test_fleet_leases.py::FleetLeaseTests::test_release_validates_every_lease_before_removing_any`
- `tests/test_fleet_leases.py::FleetLeaseTests::test_runner_setup_failure_is_abandoned_before_leases_release`
- `tests/test_fleet_leases.py::FleetLeaseTests::test_first_terminal_ledger_event_is_immutable`
- `tests/test_fleet_leases.py::FleetLeaseTests::test_close_marker_atomically_blocks_new_dispatch`
- `tests/test_fleet_leases.py::FleetLeaseTests::test_absent_workspace_quarantines_stale_close_owner_for_retry`
- `tests/test_fleet_up.py::FleetUpTests::test_teardown_recovers_only_after_confirmed_workspace_absence`

`why:` Every instance, heavy-resource, local-slot, and role-slot lease carries
the owning `run_id` and durable workspace/surface UUIDs. Release validates all
owners before deleting any lease. Reconciliation runs before dispatch and
teardown, but reclaims only after a terminal ledger event or a successful cmux
probe confirms the owning workspace or surface UUID is absent. Probe errors
preserve leases. Ledger appends are serialized, synced to disk, and the first
terminal event is immutable. Teardown holds an owner-checked closing marker
that atomically blocks later acquisitions. Recovered leases move to
`orchestration/runs/archive/leases/<run_id>/` for auditability.
