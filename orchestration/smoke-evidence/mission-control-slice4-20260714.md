# Mission Control slice 4 evidence — 2026-07-14

## Scope

- Shared `FleetControl` core with JSON CLI and dependency-free MCP stdio facade.
- Parallel dispatch, exact-run wait, content-addressed artifacts, artifact relay,
  scoped capability records, bounded subdelegation, cancellation, and graph inspection.
- No terminal screen scraping is used by the implementation or its tests.

## Deterministic gates

- `python3 -m unittest tests.test_fleet_artifacts tests.test_fleet_delegation tests.test_fleet_control tests.test_fleet_mission_state`
  passed: 21 tests.
- `python3 -m unittest discover -s tests -p 'test_*.py'` passed with the complete
  repository suite.
- `python3 -m compileall -q scripts tests` passed.
- `git diff --check` passed.
- Tests cover two dispatches before wait, exact `artifact_id` relay, authorized
  child delegation, budget exhaustion/idempotency, writer rejection, artifact
  tamper rejection, CLI JSON, and MCP initialize/tool discovery.

## Live CMUX exercise

- Isolated runs directory: `/tmp/mission-control-slice4-runs.74yPUp`.
- Clean isolated target: `/tmp/mission-control-slice4-target.vaMVOF`.
- Mission: `49ce0631-7443-5813-ae26-cf913e5eb28d`.
- Lead run: `1c714076-57ff-4fa3-8b48-2946e2135605`.
- Scout run: `452aaaf0-e6a7-4b5b-951a-8e91d296c072`.
- Challenger run: `058e62de-7f2d-42e2-b1a0-5879cc84b944`.
- Events 10 and 13 are the Scout and Challenger `delegation_registered`
  records; no `result_recorded` occurs until event 14. Both legacy runs were
  simultaneously `running`, proving dispatch-before-wait on real CMUX.
- Scout capability record: `08124a45-1c5c-5838-a34c-1a8f9410f74f`, issued at
  event 8 and bound to the exact Scout run at event 11.
- Scout completed successfully. Its verified artifact is
  `e0a467012c232f9b7f298f266996f4cf7b01fc9c6385cec06015360d1b9e89a4`.
- Mission ledger verification passed with 13 events while both runs were live;
  final mission head is
  `1faa3d16af25e0b815a6b01962a7073257830f754cac05a72cd4ce8fe98b53c3`.
- Content-addressed store verification passed after teardown. Target remained
  clean and `fleet-mc-s4-live.manifest` was removed by successful teardown.

## Live deviation retained for final gate

The configured Z.AI/OpenCode Challenger returned
`frontier_opencode_evidence_unavailable` and terminal `indeterminate` on three
tracked attempts. Fleet Control rejected all three as success, retained no
fabricated artifact, recorded cancel requests, and the Lead terminated the
mission `blocked`. Therefore this run does **not** claim a successful live
Challenger-to-Verifier relay or child subdelegation. Those paths pass the
deterministic integration tests and remain scheduled for a live retry in the
final smoke matrix.

## Invariant review

- A workflow capability and the recipient's compiled capability must both match.
- The compiled writer is unique and specialist subdelegation cannot target it.
- Capability scope, depth, remaining budget, mission, delegation, and exact
  parent run are durable and verified against the hash-chained ledger.
- Result provider/model must match the registered delegation.
- Relay passes only a verified content-addressed artifact reference.
- CMUX remains execution plane; ledger, result file, and artifact hashes remain
  completion evidence.
