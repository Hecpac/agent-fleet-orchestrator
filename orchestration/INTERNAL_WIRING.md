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

## Exact frontier completion

`rule: frontier_run_completion_is_run_id_scoped`

`enforced_by:`

- `tests/test_fleet_frontier.py::FleetFrontierTests::test_exact_binding_ignores_old_stop_then_terminalizes_verified_sentinel`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_missing_or_duplicate_sentinel_is_indeterminate`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_second_submit_in_same_session_is_ambiguous_and_retains_lease`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_stop_must_be_after_binding`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_losing_terminalizer_cannot_release_retained_lease`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_prepare_identity_is_durable_before_lease_acquisition`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_cross_boot_audit_recovers_binding_stop_and_status`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_truncated_audit_cannot_bind_a_later_submit`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_unrecoverable_gap_is_indeterminate_and_retains_lease_until_abandon`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_frontier_instance_lease_rejects_second_active_run`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_frontier_surface_rejects_alias_from_another_feature`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_malformed_lease_blocks_frontier_acquisition`
- `tests/test_fleet_wait.py::FleetWaitLedgerAuthorityTests::test_mixed_any_drains_replay_then_uses_durable_completion_time`
- `tests/test_fleet_wait.py::FleetWaitLedgerAuthorityTests::test_mixed_any_frontier_terminal_during_replay_competes_by_durable_time`
- `tests/test_fleet_wait.py::FleetWaitLedgerAuthorityTests::test_waiter_recovers_frontier_from_audit_on_boot_change`
- `tests/test_fleet_wait.py::FleetWaitEscalationTests::test_malformed_protocol_ack_fails_closed`
- `tests/test_fleet_up.py::FleetUpTests::test_frontier_send_returns_exact_run_and_rejects_second_active_turn`
- `tests/test_fleet_up.py::FleetUpTests::test_frontier_preset_is_reproducible_and_ordered`
- `tests/test_fleet_up.py::FleetUpTests::test_frontier_send_key_failure_retains_indeterminate_lease`
- `tests/test_fleet_up.py::FleetUpTests::test_race_partial_dispatch_reports_exact_runs_and_retains_leases`
- `tests/test_run_interactive_agent.py::InteractiveAgentEnvironmentTests::test_codex_roles_use_default_codex_home`

`why:` Every frontier attempt records a durable `preparing` identity before
lease acquisition. A surface UUID—not merely its manifest alias—can belong to
only one active run. Before Enter, dispatch records `run_id`, cmux `boot_id`,
lower sequence, workspace UUID, and surface UUID. Fleet Codex processes use
the default `~/.codex` config so the official cmux hooks are active, while the
router pins `gpt-5.6-sol` so fleet execution does not inherit a personal model.
`UserPromptSubmit` must bind exactly one later turn to that surface; another
distinct submit is ambiguous. A completed Stop must follow that binding and is
only a wake-up: succeeded/blocked/failed requires exactly one matching final
line `FLEET_RESULT:<run_id>:<STATUS>`. Old, duplicated, cross-surface,
non-final, or ambiguous evidence cannot complete the run. Known idle-prompt
chrome rendered after the answer is excluded from final-line evaluation; text
between the sentinel and that prompt remains a protocol failure, as does any
unknown suffix after the prompt/status chrome. Audit recovery requires the
recorded baseline and a contiguous per-boot sequence; a truncated/corrupt audit
cannot correlate a later submit. Protocol ACKs and lease metadata are validated
fail-closed. The first terminal
ledger event alone may release its lease. Boot gaps use the bounded cmux audit
and otherwise terminalize indeterminate while retaining the lease until
explicit operator abandonment. Partial sends and unconfirmed race interrupts
also retain their leases. Mixed races drain ACK replay, normalize durable
`completed_at` instants to UTC, then use `instance_id` as the deterministic
tie-breaker.

## Structured OpenCode completion

`rule: opencode_completion_requires_structured_final_evidence`

`enforced_by:`

- `tests/test_fleet_frontier.py::FleetFrontierTests::test_opencode_ignores_intermediate_stops_and_uses_structured_final_response`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_opencode_identity_mismatch_is_indeterminate_and_retains_lease`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_opencode_unverifiable_final_evidence_is_indeterminate`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_opencode_rejects_wrong_source_and_non_opencode_session_file`
- `tests/test_router_config.py::RouterConfigTests::test_interactive_roles_declare_hook_source_and_opencode_identity`
- `tests/test_fleet_up.py::FleetUpTests::test_frontier_preset_is_reproducible_and_ordered`

`why:` OpenCode emits multiple completed Stops for one turn. Stops without the
feed plugin's structured final-context marker remain wake-ups and cannot
terminalize a run. After that marker, the exact `run_id` user message, later
assistant messages, full text parts, completion timestamps, provider, and model
come from the read-only `opencode db` interface—not terminal chrome, truncated
workstream preambles, or lossy session exports. The last completed assistant
before the Stop must contain one exact sentinel as its final non-empty line.
The event source, `opencode-` session prefix, and
`opencode-hook-sessions.json` workspace/surface binding must also match the
identity persisted before dispatch. Missing, malformed, temporally late, or
mismatched evidence terminalizes indeterminate while retaining the lease; it
never degrades to success by inference.
