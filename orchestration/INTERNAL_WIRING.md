# Orchestration Internal Wiring

This file records invariants that the fleet runtime must preserve across
refactors. Each rule names the tests that lock the behavior and the regression
that would reopen if the rule disappeared.

## Durable CONTROL-mediated fleet dialogue

`rule: durable_dialogue_is_control_mediated_and_source_run_scoped`

`enforced_by:`

- `tests/test_fleet_dialogue.py::FleetDialogueTests::test_publish_copies_exact_payload_and_queries_by_recipient`
- `tests/test_fleet_dialogue.py::FleetDialogueTests::test_idempotent_retry_returns_existing_and_changed_request_fails`
- `tests/test_fleet_dialogue.py::FleetDialogueTests::test_only_exact_succeeded_result_file_is_publishable`
- `tests/test_fleet_dialogue.py::FleetDialogueTests::test_payload_limit_and_close_marker_fail_closed`
- `tests/test_fleet_dialogue.py::FleetDialogueTests::test_live_identity_mismatch_and_result_symlink_are_rejected`
- `tests/test_fleet_dialogue.py::FleetDialogueTests::test_corrupt_payload_is_rejected_by_retry_read_and_verify`
- `tests/test_fleet_dialogue.py::FleetDialogueTests::test_parallel_cli_retry_appends_one_message`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_codex_and_claude_stops_use_structured_evidence_not_screen`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_frontier_result_persistence_failure_is_indeterminate`
- `tests/test_fleet_up.py::FleetUpTests::test_teardown_archives_dialogue_ledger_and_payloads`

`why:` Fleet dialogue is an internal, explicit CONTROL relay rather than direct
peer traffic. Every message names one manifest recipient and one exact
`source_instance=source_run_id` whose immutable lifecycle terminal is
`succeeded`. Its payload is the byte-exact durable result, limited to 1 MiB,
copied into a SHA-256-addressed store, and referenced from a separate synced
JSONL ledger. A required idempotency key makes retries stable and conflicts
fail closed; a reply may name only one prior message. Interactive Codex,
Claude, and OpenCode success is not terminal until that result file is durable.
Publication shares the lease coordinator lock and rejects the fleet closing
marker, while teardown archives both the dialogue ledger and every payload.
Without this lock, agents could consume transformed or stale output, retry into
duplicate work, publish during teardown, lose frontier evidence, or leave a
conversation whose hashes can no longer be verified. Publication never starts
the recipient automatically; CONTROL must use the existing dispatch wrappers.

## Bounded fail-closed Maker–Checker dialogue

`rule: bounded_fleet_dialogue_is_control_stepped_and_fail_closed`

`enforced_by:`

- `tests/test_fleet_dialogue_controller.py::FleetDialogueControllerTests::test_happy_path_accepts_and_build_gate_revalidates_exact_head`
- `tests/test_fleet_dialogue_controller.py::FleetDialogueControllerTests::test_invalid_checker_json_terminalizes_indeterminate`
- `tests/test_fleet_dialogue_controller.py::FleetDialogueControllerTests::test_valid_revision_uses_one_run_for_rebuttal_and_revision_messages`
- `tests/test_fleet_dialogue_controller.py::FleetDialogueControllerTests::test_start_copies_and_hashes_strict_task_spec`
- `tests/test_fleet_dialogue_controller.py::FleetDialogueControllerTests::test_live_receipt_and_offline_archive_verify_same_hashes`
- `tests/test_fleet_state.py::FleetStateTests::test_fdp2_requires_latest_accepted_clean_head_before_leaving_build`
- `tests/test_fleet_up.py::FleetUpTests::test_fdp2_teardown_requires_terminal_and_archives_offline_receipt`
- `tests/test_spec_coherence.py::SpecCoherenceTests::test_fdp2_contract_and_documentation_remain_aligned`

`why:` The `fleet_dialogue` preset fixes Maker, Checker, CHALLENGE, and VERIFY
to independent providers and grants write authority only to Maker. CONTROL
freezes an exact task-spec hash, exposes one explicit dispatch or publication
action at a time, and binds every accepted run and message by ID and payload
hash; it never performs either side effect. Strict JSON contracts, real
evidence references, a per-run timeout, an absolute deadline, and a three-round
cap terminalize uncertainty instead of inferring success. Every Maker result
must be one clean append-only commit. The separate control ledger is a durable
hash chain, one conversation per feature may be active, and terminal state is
immutable. Checker acceptance still requires a human BUILD exit and exact clean
HEAD. Teardown refuses active dialogue and archives a live receipt whose files
can be verified offline. Without the rule, self-validation, stale results,
silent prompt drift, unbounded debate, or teardown races could be mistaken for
accepted work.

## Dedicated MiniMax Checker variant identity

`rule: minimax_checker_requires_durable_none_variant`

`enforced_by:`

- `tests/test_router_config.py::RouterConfigTests::test_opencode_variant_must_be_durable_and_agent_pinned`
- `tests/test_fleet_up.py::FleetUpTests::test_fleet_dialogue_preset_materializes_one_writer_and_three_independent_gates`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_prepare_identity_is_durable_before_lease_acquisition`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_opencode_variant_mismatch_is_indeterminate_and_retains_lease`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_opencode_missing_required_variant_is_indeterminate`
- `tests/test_spec_coherence.py::SpecCoherenceTests::test_fdp2_contract_and_documentation_remain_aligned`

`why:` FDP-2 uses a dedicated `minimax_checker` instead of changing the shared
MiniMax candidate or CHALLENGE roles. The OpenCode 1.17.15 TUI rejects
`--variant` (only `opencode run` accepts it), so the identity is pinned by the
dedicated agent `.opencode/agents/minimax-checker.md`, which declares
`model: minimax/MiniMax-M3` and `variant: none`; the boot command is
`opencode --agent minimax-checker` with no `-m` or `--variant`. Router
validation fails closed unless that agent file declares the exact
provider/model and variant, and rejects any TUI command carrying `--variant`.
The same expected value is carried through the fleet manifest and every
lifecycle event for that run. Completion reads the final assistant
`message.variant` from OpenCode's database and requires an exact match. An
absent or different variant terminalizes `indeterminate` and retains the lease.
The strict Checker JSON contract remains byte-exact: no parser relaxation,
`<think>` stripping, or output repair can convert reasoning leakage into an
accepted result.

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
non-final, or ambiguous evidence cannot complete the run. Provider-specific
structured evidence—not terminal chrome—must bind that final line to the
dispatched turn and configured provider/model identity, plus any explicitly
pinned OpenCode variant. Audit recovery requires the
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
- `tests/test_router_config.py::RouterConfigTests::test_interactive_roles_declare_supported_source_and_model_identity`
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

## Single-submit OpenCode prompt transport

`rule: opencode_prompt_transport_is_one_physical_submission`

`enforced_by:`

- `tests/test_fleet_frontier.py::FleetFrontierTests::test_opencode_prompt_transport_is_one_submission_with_exact_logical_prompt`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_non_opencode_prompt_transport_keeps_multiline_contract`
- `tests/test_spec_coherence.py::SpecCoherenceTests::test_fdp2_contract_and_documentation_remain_aligned`

`why:` OpenCode's TUI treats literal newlines pasted through cmux as separate
submissions. A long multiline task can therefore bind one run to several
`UserPromptSubmit` events and must fail closed as ambiguous. Frontier dispatch
preserves the exact logical task and completion contract inside a JSON string,
then sends that encoded value as one physical line for `hook_source=opencode`.
The durable `task_sha256` continues to bind the unencoded logical task supplied
by CONTROL. Codex and Claude retain their native multiline transport. The
encoding is transport-only: it does not permit a second submit, relax session
identity, or infer success from terminal chrome.

## Structured Codex and Claude completion

`rule: codex_claude_completion_requires_structured_transcript_identity`

`enforced_by:`

- `tests/test_fleet_frontier.py::FleetFrontierTests::test_hook_session_lookup_requires_exact_source_prefix_and_file`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_codex_turn_evidence_binds_transcript_response_and_identity`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_claude_turn_evidence_binds_final_end_turn_and_model`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_codex_and_claude_stops_use_structured_evidence_not_screen`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_claude_process_event_accepts_tool_using_main_turn`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_claude_duplicate_run_marker_is_ambiguous`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_codex_and_claude_identity_mismatch_is_indeterminate`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_claude_unavailable_transcript_is_indeterminate`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_prepare_rejects_unsupported_hook_source_before_ledger_write`
- `tests/test_router_config.py::RouterConfigTests::test_interactive_roles_declare_supported_source_and_model_identity`
- `tests/test_router_config.py::RouterConfigTests::test_codex_and_claude_model_must_match_command`
- `tests/test_router_config.py::RouterConfigTests::test_unsupported_interactive_hook_source_is_rejected`
- `tests/test_fleet_wait.py::FleetWaitLedgerAuthorityTests::test_waiter_recovers_frontier_from_audit_on_boot_change`

`why:` Codex and Claude terminal chrome changes with suggestions, timing labels,
separators, permission modes, and client versions, so it cannot prove where an
answer ends. Their completed Stops are wake-ups only. Completion reads the
source-specific transcript path from the exact `codex-` or `claude-` session
record, binds the unique run-tagged user message, and selects a later final
assistant response completed no later than the Stop. The transcript must confirm
the configured model; Codex also supplies its provider, while Claude's provider
is fixed by the validated `claude`/`anthropic` router contract. Only `codex`,
`claude`, and `opencode` hook sources are accepted, and each resolves exclusively
through its matching prefix and hook-session file. Claude tool-result rows are
continuations of the bound human turn, never new user turns, and sidechain rows
cannot bind or complete the parent run. Missing, malformed, late, or
mismatched evidence terminalizes indeterminate and retains the lease. Claude's
duplicate Stop notifications therefore cannot turn UI timing into success or
produce conflicting terminal outcomes.

## Claude session exit without Stop

`rule: claude_session_end_without_stop_fails_closed`

`enforced_by:`

- `tests/test_fleet_frontier.py::FleetFrontierTests::test_event_snapshot_subscribes_to_session_end`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_claude_session_end_without_stop_is_indeterminate_and_retains_lease`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_claude_stop_then_session_end_keeps_verified_terminal_result`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_cross_boot_audit_recovers_claude_session_end_without_stop`
- `tests/test_fleet_wait.py::FleetWaitEscalationTests::test_timeout_escalates_via_notify_and_uses_ack_stream`

`why:` Claude may emit a completed SessionEnd when its process exits without a
Stop, including an interrupted tool turn. SessionEnd proves process exit, never
successful completion. The live snapshot, waiter stream, and contiguous audit
recovery all observe it, but only a nonterminal Claude run with an exact prior
UserPromptSubmit binding may consume it. The event must match the bound session,
workspace, and source and occur after the binding. It terminalizes indeterminate
with reason `frontier_session_ended_without_stop` while retaining the lease; it
does not consult a possibly cleaned-up session record or infer success from a
transcript. An unbound, received-phase, stale, foreign, or post-Stop SessionEnd
cannot change run ownership or the first terminal result. Cross-boot recovery is
locked at the exact boundary where UserPromptSubmit binds under the old boot and
SessionEnd arrives as sequence 1 of the new boot; ordering then falls back to
their durable timestamps without weakening source, workspace, or session checks.
