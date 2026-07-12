# Slice 2B live smoke — exact frontier protocol

Date: 2026-07-12 (America/Chicago)

Verdict: **PASS**

The smoke used a freshly booted real cmux workspace, a real interactive Codex
process, the installed official cmux hooks, and the `fleet-send`/`fleet-wait`
entrypoints from this uncommitted working tree. A daemon restart does not apply
to this repository; `fleet-up.sh` created the clean runtime incarnation under
test.

## Clean frontier boot

Fleet `smoke-s2b-pass` booted as workspace `workspace:20`, UUID
`8FA3B2AB-0688-47A0-B55B-663B671EE9A9`. Instance `agent` used
`surface:67`, UUID `8A1FD011-B9D0-40BD-A569-F2783A859DF7`.

The live process command was:

```text
codex --model gpt-5.6-sol --sandbox read-only --ask-for-approval never
```

Its environment contained no `CODEX_HOME`, so Codex loaded the default
`~/.codex` authentication and hooks. The screen confirmed `gpt-5.6-sol high`.

## Exact sequential turns

Two turns ran sequentially in the same Codex session
`codex-019f5873-01ad-7911-81a3-497139ff6ccd`:

- `d81a8052-8128-43a3-b992-8eb6f2c0f980` bound submit seq 3869 and completed
  Stop seq 3878. Its initial verifier result was `indeterminate` because the
  first parser treated the following Codex idle prompt as response text. This
  live failure directly drove the final-answer/chrome regression fix.
- `75ab09f9-5339-4bb9-810b-bef390d446ef` used a new baseline at seq 3883,
  bound only submit seq 3886, and completed from Stop seq 3896 as
  `succeeded`, exit 0, reason `frontier_sentinel_verified`.

The successful answer ended with:

```text
8 por 7 es 56
FLEET_RESULT:75ab09f9-5339-4bb9-810b-bef390d446ef:DONE
```

`fleet-wait.sh` returned only the second exact run:

```text
instance=agent run_id=75ab09f9-5339-4bb9-810b-bef390d446ef status=succeeded exit_code=0 result_file=- completed_at=2026-07-12T22:30:56.473Z
```

Dispatch-to-terminal round-trip was approximately 3.76 seconds
(`22:30:52.713633Z` → `22:30:56.473Z`).

## No-regression evidence

- Hooks emitted real `UserPromptSubmit` and completed `Stop` events.
- The old turn's Stop and sentinel did not satisfy the newer run in the same
  session and surface.
- The newer run required its own post-baseline submit and Stop.
- UI chrome after the assistant answer was excluded, while unit regressions
  still reject any response text between the sentinel and the idle prompt.
- The model came from the versioned router rather than the user's incompatible
  `~/.codex` model setting.
- The full unit/integration suite passed: 91 tests.

## Final post-review incarnation

After incorporating both independent re-reviews, a new clean fleet
`smoke-s2b-gate` booted as workspace `workspace:21`. The machine-readable
dispatch returned run `73ff53b3-2b91-4116-89d4-1c1daac8c91a` for
`surface:69`. Its own submit seq 3921 followed baseline 3918; completed Stop
seq 3930 produced `succeeded`, exit 0, reason
`frontier_sentinel_verified`.

```text
9 por 9 es 81
FLEET_RESULT:73ff53b3-2b91-4116-89d4-1c1daac8c91a:DONE
```

The exact JSON wait result was:

```json
{"completed_at":"2026-07-12T22:38:58.733Z","exit_code":0,"instance":"agent","result_file":"","run_id":"73ff53b3-2b91-4116-89d4-1c1daac8c91a","status":"succeeded"}
```

Dispatch-to-terminal round-trip was approximately 2.59 seconds
(`22:38:56.147330Z` → `22:38:58.733Z`). This is the runtime incarnation used
for the final verdict.

Two independent read-only re-reviews then confirmed PASS: protocol continuity,
sentinel suffix, ACK validation, malformed-lease handling, partial-race
recovery, and canonical JSON ownership had no remaining actionable findings.

## Open lanes

- A real cmux reboot/rotated-audit recovery was not forced. Cross-boot recovery
  and unrecoverable-gap lease retention are covered by focused integration
  tests; close this lane by restarting cmux between dispatch and wait in a
  disposable fleet.
- A live mixed local/frontier race was not run. Replay draining, UTC completion
  ordering, and deterministic `instance_id` ties are test-locked; close this
  lane with one real local worker and one frontier candidate.
- MiniMax/GLM UI chrome was not exercised live. Their strict sentinel and hook
  paths remain covered with synthetic screens/events; close this lane when the
  corresponding provider credentials are available.
- A real partial `send`/failed Enter and automatic loser interruption were not
  injected. Both retain leases in integration tests; close this lane with a
  disposable fake/fault-injected cmux surface.
