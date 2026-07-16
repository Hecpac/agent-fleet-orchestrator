# Dan+ autonomous live smoke — 2026-07-14

## Verdict

`STATUS: PASS`

The public one-command path launched a fresh visible CMUX fleet, gave the full
mission to the lead, let the lead delegate exactly one bounded read-only task,
waited on the exact scout run, and returned the lead's durable result with exit
code `0`. No human phase advance or BUILD approval was used.

## Incarnation and scenario

- Command: `just dan danplus-smoke-0714e '<read-only README verification>'
  --target-repo "$PWD" --allow-dirty-baseline --timeout 600`
- Preset/mode: `dan` / `autonomous`
- Workspace: `workspace:20`, UUID
  `396BF00A-F956-4D5F-B4DF-A0512E23367E`
- Target: the current repository working tree.
- Acceptance: the lead had to use exactly one scout, identify the primary Dan+
  command and all three modes from `README.md`, adopt the scout's durable
  result, avoid builder/challenger/verifier, and make no product edit.

The fleet was the real runtime under test; no daemon restart applies to this
product. CMUX surfaces, official Codex hooks, the exact-run ledger, durable
result files, status/progress/log calls, and teardown were exercised live.

## Exact run trace

### Lead

- Run: `9682425e-6530-46fa-84aa-51b1b02f7fe9`
- Dispatch: `2026-07-14T05:38:51.128223+00:00`
- Completion: `2026-07-14T05:40:48.850Z`
- Terminal state: `succeeded`, exit `0`
- Reason: `frontier_sentinel_verified`
- Result:
  `orchestration/runs/results/danplus-smoke-0714e/9682425e-6530-46fa-84aa-51b1b02f7fe9.txt`

### Scout

- Run: `6df53246-77b5-462c-85e3-c0b4b81e9184`
- Dispatch: `2026-07-14T05:39:27.963271+00:00`
- Completion: `2026-07-14T05:39:48.073Z`
- Terminal state: `succeeded`, exit `0`
- Reason: `frontier_sentinel_verified`
- Result:
  `orchestration/runs/results/danplus-smoke-0714e/6df53246-77b5-462c-85e3-c0b4b81e9184.txt`

The ledger contains only the `lead` and `scout` instance names. There are no
builder, challenger, or verifier run records. The lead independently checked
the scout result against `README.md`, reported the exact run IDs, and observed
that the existing README modification predated the fleet. The writer branch
remained at its base SHA and was never adopted.

## Preceding attempts and fixes

Earlier attempts were not counted as passes:

- `0714a`: Codex auto-updated during boot and exited before submission. The
  autonomous boot no longer spends the first turn on an orientation prompt.
- `0714b`: the live panes executed, but stale hooks made transfer evidence
  indeterminate. Official hooks were reinstalled in the active `CODEX_HOME`.
- `0714c`: a successfully dispatched lead exposed that the new launcher only
  parsed single-line JSON. The parser now accepts compact or pretty JSON and
  has a regression test.
- `0714d`: the isolated agent `HOME` hid the controller's CMUX audit paths from
  tracked peer dispatch. Agents now retain their ephemeral HOME while receiving
  explicit `CMUX_HOOK_DIR` and `CMUX_EVENTS_LOG` paths; the isolation contract
  is covered by tests.

All affected runs were interrupted or terminalized explicitly before their
workspaces were removed. No failed or indeterminate attempt was promoted to
success.

## Teardown and integrity

- `fleet-down.sh danplus-smoke-0714e` exited `0` and closed `workspace:20`.
- The live manifest, writer worktree, writer branch, and CMUX workspace are
  absent after teardown.
- Archive:
  `orchestration/runs/archive/danplus-smoke-0714e-20260714T054121Z/`
- SHA-256:
  - ledger: `0746feb74b523c4e97e429f505478eaf270677d7ca2b748a20cfe0648349af0d`
  - manifest: `0fcbd1576dd88ef119bca4cfad48a7f5dbeb49edaf91c168aaa2f4fce730d384`
  - state: `c80beaa4afb70fbd54b1da39bd1f74f98ed27dac77fac6269b51bf6fdc31f971`
  - scout result:
    `09988a753a10dca99f73661881e7e6590c5eaec2fa8e8b554e02921af49a16e4`
  - lead result:
    `dd34c456effc63ed6e035eb9b8b611131b49130adfbafd15205f2f3e532e7f8f`
