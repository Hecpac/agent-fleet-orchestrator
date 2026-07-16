# P1-HITL1A approval provenance — live smoke (2026-07-16)

## Objective

Verify through the real phase-gate CLI that a Mission-bound assured fleet
cannot leave BUILD with free-form `--approved-by` text or a foreign approval
hash, while the exact active `assurance_approved.event_sha256` advances the
phase and remains acceptable to the FDP-3 start preflight.

## Runtime adaptation

No daemon restart applies to this slice. `fleet_state.py` and
`fleet_assurance_controller.py` are one-shot CLIs/modules that load the current
working tree on every invocation, and the changed gate executes before agent
dispatch or CMUX mutation.

The smoke used an isolated durable Mission under
`/private/tmp/p1-hitl1a-smoke-20260716.A5iZDB`. The Mission and its events were
created through the production `fleet_mission` / `fleet_mission_state` APIs;
the minimal assured manifest existed only to exercise the phase-gate surface
without spending a full FDP-2/FDP-3 campaign.

Mission identity and approval binding:

- Mission ID: `d282477f-4caf-5444-9031-5459d8f8cf08`
- Mission status: `assured_running`
- Request SHA-256:
  `ef48f2f5de093895fa5a312e08f534d28acebceb82ff775ea8c12b06c9ba9718`
- Approval event SHA-256:
  `8325934c5842d9b8c052d94c7e94b5335d8f7493a91aaa19637b90a755b8608e`
- Risk/scope: `high`, category `production`, exact isolated target path
- Approval expiry: one hour after setup

## No-regression probes

After the real CLI initialized the phase state and advanced CONTROL to BUILD:

1. `fleet_state.py advance ... CHALLENGE --approved-by CONTROL` exited `2` in
   `0.03s` with `Mission-bound BUILD exit rejects --approved-by text`.
2. The same command with a syntactically valid but foreign all-`c` SHA-256
   exited `3` in `0.03s` with `approval event is not the active Mission
   approval`.
3. The phase-state SHA-256 was unchanged after each rejection.
4. `cmux tree --all` was byte-identical before and after both probes; no
   workspace or surface effect occurred.

The expired-event and wrong-mode cases are deterministic test lanes because
the live approval intentionally remained valid for the happy path.

## Exact-event happy path

The real CLI received the active event hash:

```text
advanced BUILD -> CHALLENGE
real 0.03
EXACT_EVENT_RC=0
```

The resulting durable CHALLENGE history entry contains
`approval_event_sha256=8325934c...b8608e` and contains no `approved_by` field.
The exact `_require_challenge_start_state` preflight used by FDP-3 then
re-derived the Mission ledger and returned:

```text
FDP3_PREFLIGHT_PHASE=CHALLENGE
FDP3_APPROVAL_EVENT=8325934c5842d9b8c052d94c7e94b5335d8f7493a91aaa19637b90a755b8608e
```

Final isolated evidence hashes:

- phase state:
  `f52cdb6529361611bc115779a41907f855fca06dff65770c27ebfb989b413690`
- Mission ledger:
  `15bfd4f253e0d2aadb59c90a0132aa352d9d85d6f9653ac11f4c55031d8a1878`

## Identity-distinct review fleet

A live `audit` fleet used workspace UUID
`F2030B01-3707-4C86-9EF2-D55CEE3CB959` and the declared
`analysis,challenge,verify` identity group.

- Codex run `328d09f7-75a1-4d3c-9e5e-be8ad7f2c6f8` terminalized
  `succeeded`, exit `0`, reason `frontier_sentinel_verified`, with verdict
  `ACCEPT`. Result SHA-256:
  `b7e85964c240762f628472f2b8e92b7afb17b6efd3e4cb2fdcbd2398380bf314`.
- MiniMax run `56037b08-f04f-4fda-a8f8-f4347bfae899` terminalized
  `succeeded`, exit `0`, reason `frontier_sentinel_verified`, with verdict
  `ACCEPT` and no bypass findings. Result SHA-256:
  `7d2a2029f6e0411ff1907ff3193135635ec06d7a29d1fab3e78df14c425e2e56`.

`fleet-down.sh` verified the fresh manifest/CMUX identity, closed only
`workspace:26`, and returned CMUX to the original `idle` workspace. Archived
review ledger:
`orchestration/runs/archive/p1-hitl1a-review-20260716-20260716T200439Z/ledger.jsonl`
(SHA-256
`6e107cb858aa94c590028b1b22730fcd9d4714354b43e52b37dbea7f1c24b4ff`).

## Regression

- Full repository suite: **350/350 passed in 80.736s** with
  `python3 -m unittest discover -s tests -v`.
- Focused phase, Mission, assured-runner, FDP-3, approval, and spec suites:
  **58/58 passed in 8.786s**.
- Python compilation, `just --list`, synchronized CMUX skill copies, and
  `git diff --check` passed.

## Verdict

**PASS for P1-HITL1A.**

The changed CLI path rejects both the original free-form bypass and a foreign
event without state or CMUX effects. The exact active Mission approval advances
BUILD and is independently revalidated by the FDP-3 preflight.

## Open lanes

- This smoke did not run a full Maker/Checker FDP-2 conversation or launch the
  complete FDP-3 reviewer chain; those controllers remain covered by their
  deterministic and prior live campaigns.
- The isolated setup used the production Mission APIs rather than the public
  `fleet-approve.py` CLI; that CLI was not changed by this slice.
- Expiry, wrong mode, and runner command propagation are deterministic tests,
  not wall-clock live waits.
- Standalone `--approved-by` compatibility is test-locked but was not launched
  as a separate live fleet.
- This slice does not prove cryptographic or out-of-band human presence. A
  same-UID process or compromised CONTROL remains inside the documented trust
  boundary; closing that lane requires an external broker or separate OS
  principal.
