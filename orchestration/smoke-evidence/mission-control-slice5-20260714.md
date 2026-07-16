# Mission Control slice 5 evidence — 2026-07-14

## Scope

- Scoped, expiring Mission approval while retaining the legacy Claude
  permission approval interface.
- Mission transitions from assurance request through approved, assured boot,
  and assured running.
- Idempotent FDP-2/FDP-3 executor over the existing controllers' actions:
  dispatch, exact wait, content-addressed publish, phase advance, and terminal.
- Phase-scoped non-writer advisory turns and final Lead synthesis.

## Deterministic gates

- `python3 -m unittest tests.test_fleet_assured_runner tests.test_mission_run tests.test_fleet_approve tests.test_fleet_mission_state tests.test_fleet_dialogue_controller tests.test_fleet_assurance_controller`
  passed: 48 tests.
- The tests include dispatch reconciliation after death, publish/step sequencing,
  phase-advance reconciliation, controller internal/public event normalization,
  exact wait identity, malformed result and timeout fail-closed terminals,
  scoped approval/idempotency, no-writer advisory turns, the Mission bridge,
  and the existing FDP-2/FDP-3 verification suites.
- `python3 -m unittest discover -s tests -p 'test_*.py'` passed with the complete
  repository suite.
- `python3 -m compileall -q scripts tests`, `just --list`,
  `just workflow-validate`, and `git diff --check` passed.

## Live pause/resume exercise

- Clean isolated target: `/tmp/mission-control-slice5-target.Vt6zni`.
- Isolated runs: `/tmp/mission-control-slice5-runs.WCz6Ru`.
- Mission: `4e337ac5-b396-507e-94f3-81e67616d088`.
- The high-risk `production` objective returned
  `awaiting_assurance_confirmation` before a fleet manifest existed.
- Scoped approval event:
  `303308f55884ce24b44b0daaa99d0f737b79fcb83d96afee589e48ec5bd37058`.
- Resume booted `preset=fleet_dialogue`, `mode=assured`, bound to the same
  mission and target, then created FDP-2 conversation
  `a922506a-8582-4479-872e-4e5133280c0a`.
- The first run exposed an implementation defect before dispatch; after the
  internal/public controller event normalization fix, the same mission resumed
  without a second conversation. This is direct kill/resume evidence for the
  controller boundary.

## Live deviation retained for final gate

The resumed Maker dispatch `7966e8e3-86da-4589-a3df-b32da93c97fe` received no
durable `UserPromptSubmit`. Frontier terminalized it `indeterminate` with
`frontier_send_transfer_unconfirmed`; Mission Control propagated an immutable
`indeterminate` terminal instead of inferring success. No writer commit or
target modification occurred. The retained lease and FDP-2 conversation were
explicitly abandoned, the verification-aware teardown succeeded, the manifest
was removed, and the target remained clean.

This exercise proves live pre-effect pause, scoped approval, resume, assured
boot, conversation creation, and fail-closed dispatch. It does **not** claim a
live accepted FDP-2 or verified FDP-3. The full deterministic controller path
passes and the live FDP-2/FDP-3 gate remains scheduled in the final smoke matrix.

## Invariant review

- Approval binds the active request hash, workflow digest, canonical target
  scope, risk, actor hash, and expiry.
- Existing FDP-2/FDP-3 controllers remain the sole validators of their ledgers,
  contracts, Git heads, dialogue messages, snapshots, and phase gates.
- Every runner effect has a stable action identity and reconciles durable state
  before repeating.
- Invalid/timeout/identity-uncertain runs cannot advance a controller.
- Extra advisory turns cannot target CONTROL or write authority.
- Final success requires verified FDP-3 followed by an exact Lead result.
