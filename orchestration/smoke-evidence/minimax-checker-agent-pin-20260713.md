# MiniMax checker agent-pin smoke — 2026-07-13

## Verdict

`STATUS: PASS`

The `minimax_checker` identity mechanism moved from CLI flags (impossible: the
OpenCode 1.17.15 TUI rejects `--variant`, which only `opencode run` accepts) to
the dedicated agent `.opencode/agents/minimax-checker.md` declaring
`model: minimax/MiniMax-M3` and `variant: none`. The reusable fleet TUI booted
without `-m` or `--variant`, a real turn terminalized `succeeded` with
`frontier_sentinel_verified`, and the effective session evidence matched the
durable identity exactly.

## Authorization trail

- Blocker: DETÉN #5 in `orchestration/smoke-evidence/fdp3-impl-notes-20260713.md`
  (FDP-3 build paused; the TUI aborted on `--variant`).
- Decision frozen via entrevista-pre-slice: option A — dedicated agent pin,
  fail-closed provider/model/variant validation preserved. Authorized by Hector
  on 2026-07-13.

## Change under test

- `.opencode/agents/minimax-checker.md` (new): mirrors the `fleet-reviewer`
  read-only contract and adds `model: minimax/MiniMax-M3`, `variant: none`.
- `orchestration/router.yaml`: `minimax_checker.command` is now
  `["opencode", "--agent", "minimax-checker"]` (no `-m`, no `--variant`);
  durable `variant: "none"` unchanged.
- `scripts/router_config.py`: static validation reshaped, not relaxed — any TUI
  command carrying `--variant` is rejected; variant-declaring roles must pin
  identity through a dedicated `--agent` whose frontmatter declares the exact
  provider/model and variant; roles without a durable variant are rejected if
  their agent declares one (bidirectional mirror preserved).
- Tests: 162/162 OK (unittest discover, 46.7 s), including the renamed
  `test_opencode_variant_must_be_durable_and_agent_pinned` negative paths.

## Adaptation of the restart technique

Daemon restart is not applicable in this repo. The real surface under test is a
fresh cmux fleet booted through the supported wrappers, matching the precedent
in `fleet-validation-audit-20260713.md`.

## Scope and baseline

- Feature: `mmx-agentpin-20260713`
- Roster: custom, `FLEET_NO_LEAD=1`, single instance `checker=minimax_checker`
  (monitor lead) — the minimal roster that exercises the changed path.
- Branch: `main`, uncommitted FDP-3 + agent-pin working tree.
- OpenCode version at boot: `1.17.15` (the exact version that rejected the flag).
- `MINIMAX_API_KEY` present; no other active fleet manifests before boot.

## Phase trace

### Boot (the previously failing step)

- `FLEET_NO_LEAD=1 ./scripts/fleet-up.sh mmx-agentpin-20260713 checker=minimax_checker`
  exited 0 at 2026-07-13T20:22:47Z.
- Workspace `workspace:41` (`DB9E053D-7639-4EB5-81DB-33C2B533D412`), checker at
  `surface:134`; manifest recorded `checker.variant=none`,
  `checker.provider=minimax`, `checker.model=MiniMax-M3`.
- TUI screen capture (`cmux read-screen`) showed the loaded identity in the
  status line: `Minimax-Checker · MiniMax-M3 MiniMax (minimax.io) · none`.
- `fleet_state.py advance … BUILD --evidence smoke-agentpin-minimax-checker`
  reported `advanced CONTROL -> BUILD`.

### Live turn

- `./scripts/fleet-send.sh mmx-agentpin-20260713 checker "…" --json` returned
  `run_id e67e32cf-8052-4a29-8e5a-6fb3ecc9f533`.
- `./scripts/fleet-wait.sh … --run checker=e67e32cf… --json` returned
  `status=succeeded`, `exit_code=0`, completed 2026-07-13T20:23:36.122Z.
- Turn round-trip: dispatched 20:23:31.338Z → completed 20:23:36.122Z (~4.8 s);
  boot-to-archive total ~1 min 51 s.
- Result file contains exactly `AGENT-PIN-OK` plus the sentinel
  `FLEET_RESULT:e67e32cf-8052-4a29-8e5a-6fb3ecc9f533:DONE`.

### Fail-closed identity evidence

- All four lifecycle events (`preparing`, `dispatched`, `running`, `succeeded`)
  carry `variant: "none"`; terminal reason `frontier_sentinel_verified`.
- Effective session values read from OpenCode's database
  (session `ses_0a2d9cfceffeTM1NDGBRwzhqlE`, final assistant message):
  `variant="none"`, `providerID="minimax"`, `modelID="MiniMax-M3"`,
  `tokens.reasoning=0` — exactly the values the frozen spec expected.

## No-regression of the specific failure

The original failure was a boot abort: the TUI rejected `--variant` and the
ready pattern never matched. This smoke re-ran the identical wrapper path
(`fleet-up.sh` → cmux boot → ready wait) with the new agent-pinned command and
the boot completed cleanly, the pane stayed alive showing the pinned identity,
and the same fail-closed completion check that would have terminalized
`frontier_opencode_variant_mismatch` on absent/different variant verified the
live turn instead.

## Teardown and integrity

- `just fleet-down mmx-agentpin-20260713` exited 0 and closed `workspace:41`.
- No active manifests remain.
- Durable archive: `orchestration/runs/archive/mmx-agentpin-20260713-20260713T202438Z/`
  - ledger: `e4b92ff3f3641061a2364ac23361b8afe3009bc30104d153bd0b0146ef17b2a6`
  - manifest: `16ad9ac5b36c2693e8dfc407cabbe44129a28b803f80ddfc9654cfbb9e2ca0a2`
  - state: `d23ad689c37f5ea66658eb6a4860fba51aef6d4fb24d1cee0dd8f0b98498c1b1`

## Open lanes (not exercised here)

- **Checker tool lane**: the live task was text-only, deliberately avoiding the
  known MiniMax OpenCode permission re-block quirk, which is orthogonal to the
  pin mechanism. Close by sending a read-only task that requires `cat`/`git`
  and confirming a `succeeded` terminal.
- **Full `fleet_dialogue` preset**: only the minimal custom roster booted; the
  four-instance preset (codex maker, GLM challenge, Claude verify alongside the
  new checker) was not re-booted. Close during the FDP-2/FDP-3 live flows.
- **FDP-3 assurance chain**: this smoke resolves DETÉN #5 only; the containing
  FDP-3 slice still owes its own gate and smoke.
- **Static negative paths**: exercised via the 162-test suite, not via live
  boots of mutated configs (a live boot of a `--variant` config would abort by
  construction — that is the falsified mechanism this change removes).
