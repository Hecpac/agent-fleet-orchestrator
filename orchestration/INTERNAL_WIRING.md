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
