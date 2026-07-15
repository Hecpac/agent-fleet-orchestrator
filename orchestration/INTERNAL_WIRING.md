# Orchestration Internal Wiring

This file records invariants that the fleet runtime must preserve across
refactors. Each rule names the tests that lock the behavior and the regression
that would reopen if the rule disappeared.

## Mission Control contracts are policy-only

`rule: mission_workflows_cannot_smuggle_runtime_or_authority_changes`

`enforced_by:`

- `tests/test_mission_schemas.py::MissionSchemaContractTests::test_contract_schemas_are_strict_and_loadable`
- `tests/test_mission_schemas.py::MissionSchemaContractTests::test_workflow_schema_has_no_runtime_or_authority_escape_hatches`
- `tests/test_mission_schemas.py::MissionSchemaContractTests::test_duplicate_fixture_fails_closed`
- `tests/test_mission_schemas.py::MissionSchemaContractTests::test_invalid_fixtures_expose_forbidden_unknown_keys`

`why:` Mission workflows declare policy and available capabilities, never an
agent order, shell command, CMUX surface, preassigned run identity, authority,
or tool access. Every contract object rejects unknown fields, and JSON-compatible
YAML is parsed with duplicate-key rejection. This keeps the Lead authoritative
over the emergent work graph while the kernel retains effect and identity
control.

## Workflow compilation is deterministic and effect-free

`rule: workflow_compile_resolves_policy_without_launching_agents`

`enforced_by:`

- `tests/test_workflow_config.py::WorkflowConfigTests::test_repository_workflows_compile_against_router`
- `tests/test_workflow_config.py::WorkflowConfigTests::test_compile_is_canonical_deterministic_and_has_no_cmux_effects`
- `tests/test_workflow_config.py::WorkflowConfigTests::test_unprovided_capability_and_writer_drift_fail_before_boot`

`why:` A workflow is compiled into canonical JSON and stable digests while
router healthchecks are disabled. Compilation resolves the preset, public
provider/model identity, capabilities, assurance preset, and unique writer, but
never exposes commands/tool access or invokes CMUX. Therefore invalid policy
fails before boot and identical inputs always produce identical bytes.

## Mission state is append-only, idempotent, and restart-safe

`rule: mission_truth_is_verified_hash_chain_with_immutable_first_terminal`

`enforced_by:`

- `tests/test_fleet_mission_state.py::MissionStateTests::test_create_is_durable_and_idempotent_across_restart`
- `tests/test_fleet_mission_state.py::MissionStateTests::test_create_recovers_kill_between_files_and_first_append`
- `tests/test_fleet_mission_state.py::MissionStateTests::test_retry_same_key_does_not_duplicate_and_payload_drift_fails`
- `tests/test_fleet_mission_state.py::MissionStateTests::test_first_terminal_is_immutable_and_success_requires_archive`
- `tests/test_fleet_mission_state.py::MissionStateTests::test_tampering_and_partial_append_break_verification`
- `tests/test_fleet_mission_state.py::MissionStateTests::test_lineage_and_artifact_identity_are_derived`

`why:` Mission truth lives only in `missions/<mission_id>/mission.jsonl`. Every
append verifies the complete prior chain while holding one exclusive mission
lock, gives the event a monotonic sequence and previous hash, and fsyncs before
returning. A retry with the same key and request returns the original event; a
different request conflicts. State, lineage, risk, resume action, and terminal
are re-derived after process death, tampering fails verification, risk cannot
decrease, and no event may follow the first terminal.

## Canonical missions reconcile effects and pause high risk

`rule: mission_runner_binds_one_lead_run_and_pauses_before_high_risk_effects`

`enforced_by:`

- `tests/test_mission_run.py::MissionRunTests::test_dry_run_compiles_and_assesses_without_creating_state`
- `tests/test_mission_run.py::MissionRunTests::test_high_risk_pauses_before_fleet_boot`
- `tests/test_mission_run.py::MissionRunTests::test_autonomous_mission_completes_with_durable_result_and_checkpoint`
- `tests/test_mission_run.py::MissionRunTests::test_resume_adopts_exact_legacy_lead_run_without_redispatch`
- `tests/test_fleet_up.py::FleetUpTests::test_mission_boot_binds_canonical_mission_id_in_manifest`

`why:` `just mission` compiles before effects, binds a canonical mission UUID
into the fleet manifest, records an intent hash before dispatch, and accepts
only the exact durable Lead terminal. Resume reconciles the existing legacy
ledger by mission-specific prompt hash and refuses ambiguity instead of
creating a second Lead run. Deterministic high/unknown risk transitions to a
durable assurance wait before fleet boot; repository-local low/medium work can
continue autonomously. CMUX status includes the mission ID but never decides
completion.

## Fleet Control collaboration is artifact-driven and capability-scoped

`rule: fleet_control_never_uses_terminal_chrome_or_delegates_writer_authority`

`enforced_by:`

- `tests/test_fleet_control.py::FleetControlTests::test_dispatch_many_creates_all_runs_without_waiting`
- `tests/test_fleet_control.py::FleetControlTests::test_authorized_specialist_can_subdelegate_but_never_to_writer`
- `tests/test_fleet_control.py::FleetControlTests::test_wait_persists_exact_artifact_and_relay_references_its_id`
- `tests/test_fleet_control.py::FleetControlTests::test_artifact_tampering_and_identity_drift_fail_closed`
- `tests/test_fleet_control.py::FleetControlTests::test_cli_and_mcp_share_core_and_do_not_screen_scrape`
- `tests/test_fleet_delegation.py::FleetDelegationTests::test_token_tampering_is_rejected_even_if_file_remains_valid_json`

`why:` The CLI and local MCP server share one core that dispatches only through
the existing tracked wrappers and waits on exact run IDs. Results enter a
mission content-addressed store only after durable terminal/provider/model
verification; relay passes an artifact ID/path, never raw terminal traffic.
Subdelegation requires a ledger-bound capability token with depth, scope, and
budget, and the core rejects every child attempt to reach the unique writer.
CMUX screen content is absent from the acceptance path.

## Risk-proportional execution modes

`rule: autonomous_dispatch_skips_routine_phase_gates_without_weakening_identity`

`enforced_by:`

- `tests/test_router_config.py::RouterConfigTests::test_execution_modes_separate_autonomy_from_assurance`
- `tests/test_fleet_state.py::FleetStateTests::test_autonomous_mode_opens_all_roster_phases_without_approval`
- `tests/test_fleet_up.py::FleetUpTests::test_dan_preset_publishes_autonomous_visible_roster`
- `tests/test_fleet_run.py::FleetRunTests::test_existing_autonomous_fleet_runs_lead_to_durable_result`

`why:` Presets declare `autonomous`, `guided`, or `assured`. Only autonomous
manifests bypass phase checks and routine BUILD-exit approval; UUID validation,
surface leases, provider/model binding, exact `run_id` completion, durable result
files, one-writer validation, and worktree isolation remain unchanged. The
one-command runner gives the complete mission to CONTROL and waits for that
lead's exact terminal result, so the LLM owns open-ended task decomposition
without making terminal chrome authoritative. The `fleet_dialogue` preset is
explicitly `assured` and retains FDP-2/FDP-3 gates. Autonomous boot does not
submit a separate orientation turn: the first lead submission is always the
tracked mission, preventing boot guidance or CLI-update flows from racing its
`UserPromptSubmit` binding.

## Execution profiles and manifests migrate without authority drift

`rule: execution_profile_changes_perimeter_not_compiled_tool_capabilities`

`enforced_by:`

- `tests/test_fleet_manifest.py::FleetManifestTests::test_legacy_defaults_are_explicit_and_migration_does_not_invent_provenance`
- `tests/test_fleet_manifest.py::FleetManifestTests::test_new_contract_validates_profiles_and_control_tracking`
- `tests/test_fleet_manifest.py::FleetManifestTests::test_duplicates_unknown_profiles_and_false_legacy_claim_fail_closed`
- `tests/test_run_interactive_agent.py::InteractiveAgentEnvironmentTests::test_sandboxed_profile_narrows_runtime_perimeter_without_changing_role`
- `tests/test_run_interactive_agent.py::InteractiveAgentEnvironmentTests::test_regulated_profile_requires_mission_identity`
- `tests/test_run_interactive_agent.py::InteractiveAgentEnvironmentTests::test_claude_receives_minimal_hardened_configuration`
- `tests/test_fleet_up.py::FleetUpTests::test_execution_profiles_are_manifested_without_changing_declared_tools`
- `tests/test_fleet_up.py::FleetUpTests::test_regulated_profile_rejects_unbound_legacy_boot`

`why:` Fresh fleets publish manifest contract v2, an explicit `native`,
`sandboxed`, or `regulated` process profile, and their tracked-input protocol.
Native retains the established provider commands and tool declarations.
Sandboxed moves process-owned temporary/cache/runtime state inside the private
ephemeral home; regulated additionally requires Mission identity and declares
CONTROL-only effects. Neither rewrites router `tool_access`. Strict parsing
rejects duplicate keys, unsafe controls, unknown profiles, and false legacy
claims. Claude's isolated settings expand the actual controller home and repo
root at runtime, so secret-path denies and audit hooks do not depend on one
developer's absolute paths. A legacy manifest normalizes to native plus
`legacy-cmux`; in-place migration records those honest defaults and never claims
`control-v1` for events created before that enforcement existed.

## Fleet Control socket authenticates durable caller identity

`rule: mission_control_socket_requires_peer_uid_and_bound_run_capability`

`enforced_by:`

- `tests/test_fleet_control_socket.py::FleetControlSocketTests::test_specialist_socket_rejects_lead_impersonation`
- `tests/test_fleet_control_socket.py::FleetControlSocketTests::test_bound_specialist_token_limits_tools_and_delegated_scope`
- `tests/test_fleet_control_socket.py::FleetControlSocketTests::test_socket_permissions_and_health_are_kernel_scoped`
- `tests/test_fleet_control.py::FleetControlTests::test_cli_and_mcp_share_core_and_do_not_screen_scrape`

`why:` The private Unix socket is mode 0600 inside an owner-only directory and
rejects a peer UID not reported by the kernel. Every operational request also
names the exact durable caller. The socket accepts only a specialist that
matches one delegation plus the uniquely bound capability token; Lead-shaped
callers fail closed and the Lead retains the direct CONTROL CLI. Specialist
calls are limited to inspection/result operations or subdelegation whose
parent, token, capability, depth, and budget match that identity; they cannot
complete the Mission, cancel arbitrary work, or reach the writer. The stdio
MCP/CLI contracts remain available for legacy callers, while canonical Mission
lifecycle reconciles one socket service across runner death and stops it before
teardown.

## Raw CMUX input is never tracked completion evidence

`rule: interactive_result_requires_control_authorized_submit_provenance`

`enforced_by:`

- `tests/test_fleet_frontier.py::FleetFrontierTests::test_control_authorization_prevents_raw_cmux_submit_from_binding_tracked_run`
- `tests/test_fleet_tracking.py::FleetTrackingTests::test_control_authorized_success_verifies_and_binding_tamper_fails`
- `tests/test_fleet_tracking.py::FleetTrackingTests::test_new_manifest_rejects_legacy_interactive_success_but_allows_local`
- `tests/test_fleet_up.py::FleetUpTests::test_frontier_send_returns_exact_run_and_rejects_second_active_turn`
- `tests/test_fleet_wait.py::FleetWaitLedgerAuthorityTests::test_spurious_notification_is_ignored_until_exact_run_is_terminal`

`why:` A contract-v2 interactive dispatch persists `control-v1` before CMUX
input. After the physical send, CONTROL must authorize exactly one matching
`UserPromptSubmit` and persist its event ID, boot ID, sequence, session,
workspace, and surface. Only that exact event can bind the run. A manual or
later submit may alter visible pane state but remains untracked. Before a
successful result is consumed, the waiter, canonical Mission runner, and Fleet
Control verify the unique dispatch → authorization → binding → terminal chain;
binding drift becomes indeterminate or a hard provenance error. Local workers
retain their lease/ledger path, and honest legacy manifests remain compatible
without being relabeled as control-authorized.

## CONTROL-owned compliance audit writer

`rule: maker_never_opens_or_owns_compliance_ledger`

`enforced_by:`

- `tests/test_fleet_audit_control.py::FleetAuditControlTests::test_control_writer_hashes_payloads_and_signs_complete_chain`
- `tests/test_fleet_audit_control.py::FleetAuditControlTests::test_peer_uid_contract_fails_closed`
- `tests/test_fleet_audit_control.py::FleetAuditControlTests::test_peer_credentials_support_linux_so_peercred`
- `tests/test_fleet_audit_control.py::FleetAuditControlTests::test_tampering_breaks_signature_verification`
- `tests/test_fleet_audit_control.py::FleetAuditControlTests::test_s3_anchor_requires_compliance_headers_and_versioned_receipt`

`why:` Compliance events cross a Unix socket into a CONTROL-owned writer that
authenticates the kernel-reported peer UID. Maker supplies hook input but never
opens, owns, reads, truncates, or replaces the ledger. CONTROL hashes raw tool
arguments and responses, HMAC-signs the complete event chain, and anchors each
event before local append. Production startup fails closed unless an HTTPS S3
Object Lock sink returns a version id and HEAD confirms COMPLIANCE retention.
The directory sink is test-only, marks every record non-compliant, and can
never support a GO verdict. Without this boundary, environment labels, mode
0600, or a locally recalculated hash chain could be mistaken for immutability.

## Fail-closed fleet validation prompt

`rule: fleet_validation_prompt_preserves_exact_fail_closed_lifecycle`

`enforced_by:`

- `tests/test_spec_coherence.py::SpecCoherenceTests::test_fleet_validation_prompt_locks_exact_fail_closed_lifecycle`

`why:` The reusable orchestrator prompt must route work through the supported
fleet wrappers, retain every exact `instance=run_id`, consume canonical JSON,
respect configured phase and authority boundaries, and reject partial or
indeterminate completion. Its approved `audit` smoke names the router's exact
`analysis`, `challenge`, and `verify` instances, requires read-only Git
integrity, preserves evidence, and tears down through `fleet-down`. Without
this lock, prose could drift into direct pane control, stale completion,
best-effort success, or an audit roster that no longer matches the router.

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

## Sequential independent fleet assurance

`rule: sequential_assurance_is_context_bound_phase_gated_and_fail_closed`

`enforced_by:`

- `tests/test_fleet_assurance_controller.py::FleetAssuranceControllerTests::test_verified_path_binds_context_gate_messages_receipt_and_offline_archive`
- `tests/test_fleet_assurance_controller.py::FleetAssuranceControllerTests::test_rejected_requires_sustained_or_new_finding`
- `tests/test_fleet_assurance_controller.py::FleetAssuranceControllerTests::test_invalid_glm_and_timeout_fail_closed`
- `tests/test_fleet_assurance_controller.py::FleetAssuranceControllerTests::test_invalid_claude_json_is_indeterminate`
- `tests/test_fleet_assurance_controller.py::FleetAssuranceControllerTests::test_phase_gate_requires_exact_control_head`
- `tests/test_fleet_assurance_controller.py::FleetAssuranceControllerTests::test_dirty_snapshot_cleanup_fails_and_retains_both`
- `tests/test_spec_coherence.py::SpecCoherenceTests::test_fdp3_contract_and_documentation_remain_aligned`

`why:` FDP-3 starts only after the human-approved BUILD exit and rebinds the
exact clean FDP-2 accepted HEAD. CONTROL copies and hashes the FDP-2 task spec,
messages, and lifecycle evidence into an assurance-owned context, creates one
clean detached snapshot per reviewer, and exposes exactly one explicit action
at a time. GLM may publish findings but no verdict; only after that exact
message is bound may the phase gate accept its control-head hash and enable
Claude. Claude independently adjudicates every GLM finding and can close only
`verified` or `rejected`; malformed contracts, identity drift, timeouts, or
missing evidence terminalize uncertainty. The separate assurance hash chain,
single-attempt policy, immutable terminals, live receipt, offline archive, and
clean-only snapshot teardown prevent stale reviews, silent retries, Maker
self-validation, cross-phase dispatch, or destructive cleanup from being
mistaken for technical verification.

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

## Assured mission actions reconcile instead of replaying effects

`rule: assured_runner_binds_scoped_approval_and_reconciles_every_action`

`enforced_by:`

- `tests/test_fleet_approve.py::FleetApproveTests::test_mission_approval_is_scoped_expiring_and_idempotent`
- `tests/test_fleet_approve.py::FleetApproveTests::test_mission_approval_rejects_scope_drift`
- `tests/test_fleet_mission_state.py::MissionStateTests::test_scoped_approval_drives_assured_boot_transitions`
- `tests/test_fleet_assured_runner.py::FleetAssuredRunnerTests::test_dispatch_retry_reconciles_the_same_run_without_resend`
- `tests/test_fleet_assured_runner.py::FleetAssuredRunnerTests::test_fdp2_executes_dispatch_wait_publish_then_accepts`
- `tests/test_fleet_assured_runner.py::FleetAssuredRunnerTests::test_fdp3_reconciles_phase_advance_and_verifies`
- `tests/test_fleet_assured_runner.py::FleetAssuredRunnerTests::test_malformed_or_timed_out_controller_terminal_fails_closed`
- `tests/test_fleet_assured_runner.py::FleetAssuredRunnerTests::test_additional_advisory_is_phase_scoped_and_never_writer`
- `tests/test_mission_run.py::MissionRunTests::test_approved_high_risk_mission_bridges_to_assured_runner_and_lead`

`why:` A high/unknown mission cannot boot the assured fleet until the exact
request, workflow digest, target scope, risk, human identity hash, and expiry
are bound into the Mission hash chain. The action executor consumes the
existing FDP-2/FDP-3 `next_action` contracts. Dispatch reconciles by exact
prompt hash, wait requires the exact run, publication is content-addressed and
idempotent, and phase advance checks the current durable phase before acting.
A process death between any effect and acknowledgement therefore resumes the
same action instead of creating a second run or message. Malformed, timed-out,
or non-verified controller terminals fail closed. Extra advisory turns are
read-only, phase-scoped, and cannot target the writer.

## Mission-bound assured fleets require signed audit closure

`rule: mission_assured_close_requires_signed_audit_and_honest_worm_receipts`

`enforced_by:`

- `tests/test_fleet_audit_control.py::FleetAuditControlTests::test_control_writer_hashes_payloads_and_signs_complete_chain`
- `tests/test_fleet_audit_control.py::FleetAuditControlTests::test_peer_uid_contract_fails_closed`
- `tests/test_fleet_audit_control.py::FleetAuditControlTests::test_tampering_breaks_signature_verification`
- `tests/test_fleet_audit_control.py::FleetAuditControlTests::test_s3_anchor_requires_compliance_headers_and_versioned_receipt`
- `tests/test_fleet_audit_integration.py::FleetAuditIntegrationTests::test_signed_lifecycle_is_idempotent_and_verifies_offline`
- `tests/test_fleet_audit_integration.py::FleetAuditIntegrationTests::test_raw_metadata_is_rejected_and_tampering_breaks_offline_verify`
- `tests/test_fleet_audit_integration.py::FleetAuditIntegrationTests::test_worm_profile_fails_closed_without_compliance_configuration`
- `tests/test_fleet_audit_integration.py::FleetAuditIntegrationTests::test_offline_worm_verification_rejects_partial_anchor_receipt`
- `tests/test_fleet_audit_integration.py::FleetAuditIntegrationTests::test_offline_receipts_must_match_ledger_backend_scope_and_object`
- `tests/test_fleet_audit_integration.py::FleetAuditIntegrationTests::test_external_compliance_preflight_rejects_loopback_without_state`
- `tests/test_mission_run.py::MissionRunTests::test_local_worm_cannot_satisfy_regulated_profile_or_risk`
- `tests/test_mission_run.py::MissionRunTests::test_declared_worm_category_rejects_signed_workflow`

`why:` Every Mission-bound assured fleet starts or reconciles a CONTROL-owned
AuditService before FDP actions. The live ledger is HMAC-authenticated, chained,
peer-UID checked, and accepts only hashes plus bounded metadata. Its final root
receipt is signed with Ed25519 so offline verification needs only the public
key, never the CONTROL HMAC or private signing key. `signed` explicitly records
`worm=false`; `worm` requires S3 Object Lock COMPLIANCE metadata, exact version
IDs, and one complete receipt per event. Every ledger event and receipt binds
the backend, object key, digest, and `trust_scope`; observational WORM trace
spans preserve those verified trust attributes. `local-development` accepts
only an HTTPS endpoint resolving entirely to loopback through a validated CA;
`external-compliance` accepts only public-global DNS answers and connects only
to those prevalidated numeric addresses, so a later DNS answer cannot redirect
the request to a weaker network boundary. It can never fall back to local or
signed evidence. Missing service, corrupt chain, unsafe raw
metadata, wrong UID, absent compliance configuration, or a partial receipt
blocks Mission success and mission-bound assured teardown.

## Mission closure requires a portable content-addressed archive

`rule: mission_archive_reproduces_writer_and_binds_assured_audit_root`

`enforced_by:`

- `tests/test_fleet_archive.py::FleetArchiveTests::test_full_archive_is_portable_and_reproduces_binary_writer_commit`
- `tests/test_fleet_archive.py::FleetArchiveTests::test_tamper_and_unsafe_symlink_fail_closed`
- `tests/test_fleet_archive.py::FleetArchiveTests::test_redacted_and_hash_only_policies_preserve_hash_evidence`
- `tests/test_fleet_archive.py::FleetArchiveTests::test_assured_archive_binds_signed_audit_without_private_keys`
- `tests/test_fleet_archive.py::FleetArchiveTests::test_sensitive_full_archive_requires_specific_unexpired_approval`
- `tests/test_fleet_approve.py::FleetApproveTests::test_sensitive_full_archive_approval_is_scoped_and_idempotent`
- `tests/test_mission_run.py::MissionRunTests::test_autonomous_mission_completes_with_durable_result_and_archive`

`why:` Mission success is impossible until `fleet_archive.py` has created and
re-read an index containing every durable evidence hash, size, and applied
content policy. The full-index binary patch is derived from the immutable
base/final commit pair; the final-tree tar must reproduce the exact Git tree
object; and the writer worktree must be clean, attached to the only declared
writer branch, and equal to its durable ref. Verification is workspace-free,
rejects unknown physical files, unsafe paths, symlinks, malformed metadata,
hash drift, patch drift, tree drift, or unreachable commits. Redacted and
hash-only omissions remain explicit. Assured archives exclude HMAC/private
keys and bind their non-audit content root to the signed audit ledger before
teardown, so neither a mutable CMUX surface nor an unsigned directory listing
can be mistaken for portable closure evidence. A workflow that combines a
`full` archive with `credentials` or `private_data` must also carry a separate,
unexpired archive approval bound to mission, workflow digest, scope, and risk
categories; normal assurance approval is deliberately insufficient.

## Mission specialists reach only their exact authenticated control socket

`rule: mission_specialist_socket_access_is_read_only_prebound_and_race_free`

`enforced_by:`

- `tests/test_run_interactive_agent.py::InteractiveAgentEnvironmentTests::test_mission_specialist_uses_ephemeral_codex_home_with_auth_and_cmux_hooks`
- `tests/test_run_interactive_agent.py::InteractiveAgentEnvironmentTests::test_mission_specialist_fails_closed_without_codex_auth`
- `tests/test_fleet_up.py::FleetUpTests::test_mission_boot_binds_canonical_mission_id_in_manifest`
- `tests/test_fleet_control.py::FleetControlTests::test_delegating_run_is_socket_bound_before_interactive_transfer`
- `tests/test_fleet_control_socket.py::FleetControlSocketTests::test_bound_specialist_token_limits_tools_and_delegated_scope`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_opencode_retries_bounded_database_visibility_race`

`why:` The CONTROL lead needs broad CMUX access, but read-only specialists do
not. Mission-bound Codex specialists therefore run with a named permission
profile extending `:read-only` and allow only the canonical mission socket.
Their ephemeral Codex home contains existing authentication plus a fixed
controller-owned CMUX hook bridge, never user-provided hook command text or a
broad controller sandbox override.
CONTROL deterministically assigns the specialist `run_id`, persists its exact
delegation/token binding, and only then transfers the prompt. The server may
therefore authenticate an immediate first tool call without weakening lineage.
Provider transcript readers tolerate only bounded storage visibility races.

## Provider adapters cannot weaken durable result identity

`rule: provider_adapter_transport_and_evidence_are_identity_bound`

`enforced_by:`

- `tests/test_fleet_providers.py::FleetProviderTests::test_builtin_adapters_validate_every_repository_role`
- `tests/test_fleet_providers.py::FleetProviderTests::test_submission_transport_preserves_existing_prompt_contracts`
- `tests/test_fleet_providers.py::FleetProviderTests::test_same_structured_sentinels_produce_same_terminals`
- `tests/test_fleet_providers.py::FleetProviderTests::test_fake_adapter_exercises_the_full_contract_deterministically`
- `tests/test_fleet_providers.py::FleetProviderTests::test_wrong_adapter_cannot_claim_another_provider_or_variant`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_codex_and_claude_stops_use_structured_evidence_not_screen`
- `tests/test_fleet_frontier.py::FleetFrontierTests::test_opencode_ignores_intermediate_stops_and_uses_structured_final_response`
- `tests/test_fleet_control.py::FleetControlTests::test_artifact_tampering_and_identity_drift_fail_closed`

`why:` Codex, Claude, OpenCode, and Ollama implement the same configuration,
launch, submission, confirmation, observation, extraction, identity, and
cancellation interface. The adapter chooses only provider-specific transport
and evidence decoding; CONTROL retains leases, side effects, terminal policy,
and durable storage. The compiled workflow freezes both hook source and adapter
name, while every result still binds provider, model, and variant. An adapter
whose hook source or fixed provider differs from the dispatch cannot validate
configuration or evidence. The public `fleet-send` and `fleet-dispatch`
arguments remain unchanged, and a fake adapter exercises the full contract
without CMUX or a provider process.

## Mission metrics and traces are durable, redacted, and non-authoritative

`rule: mission_observability_never_reads_content_or_controls_completion`

`enforced_by:`

- `tests/test_fleet_report.py::FleetReportTests::test_report_derives_routing_parallelism_relay_and_escalation`
- `tests/test_fleet_report.py::FleetReportTests::test_trace_parent_child_and_exporter_failure_are_non_authoritative`
- `tests/test_fleet_report.py::FleetReportTests::test_build_report_verifies_durable_ledgers_and_cli_redacts_content`
- `tests/test_fleet_report.py::FleetReportTests::test_orchestration_eval_cases_match_expected_metrics`
- `tests/test_fleet_trace.py::FleetTraceTests::test_spans_are_derived_from_durable_events_and_redact_content`

`why:` Reports validate the Mission hash chain, compiled digest, strict run
JSONL, and optional archive before calculating timestamps, selection,
parallelism, lineage, outcomes, provider/model tokens, adoption, and evidence
set counts. They never load objective, prompt, task, result, artifact, or
environment content. Missing cost receipts and invisible retry attempts remain
explicitly `null`; no estimate is invented. Trace attributes use a scalar
allowlist and parent runs come only from durable lineage. WORM spans require a
complete offline-verified public audit envelope. File or HTTP exporters are
optional, label themselves `observational_only`, and are not invoked by the
mission completion path, so exporter failure cannot change Lead authority or a
terminal.
