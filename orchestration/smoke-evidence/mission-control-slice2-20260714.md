# Mission Control Slice 2 smoke — 2026-07-14

## Surface

Mission state is a standalone durable CLI and does not run inside a daemon, so
daemon restart/PID/port checks are not applicable. Independent processes ran
the real compiler, mission create, create retry, verify, resume-plan, and trace
CLI commands against a fresh temporary runs directory.

## Observations

```text
{"events": 2, "head_sha256": "667b16a3b2b2f03f9ee002e7f0dbc7973273fc138f4cbd0c296f71e7e206840a", "mission_id": "872636da-03d0-52ff-9029-4d58940d2397", "status": "compiled", "valid": true}
{"head_sha256": "667b16a3b2b2f03f9ee002e7f0dbc7973273fc138f4cbd0c296f71e7e206840a", "last_sequence": 2, "mission_id": "872636da-03d0-52ff-9029-4d58940d2397", "next_action": "boot", "status": "compiled"}
SMOKE PASS mission_id=872636da-03d0-52ff-9029-4d58940d2397 retry_created=false
```

The retry returned the same mission UUID and did not append a third event.

## Non-regression

Specific tests corrupt an event byte and append a partial JSON line; both make
verification fail. They also simulate a process death after durable files but
before the first event, then prove that retry repairs the ledger rather than
creating a second mission. Invalid risk decrease and premature success are
rejected before append.

## Verdict

PASS — mission creation, exact retry, offline verification, derived resume plan,
and trace derivation work across clean process boundaries.

## Open lanes

- Actual fleet boot/lead dispatch is intentionally absent from Slice 2 and is
  the changed path for Slice 3.
- Concurrency with real agent processes is deferred to Fleet Control in Slice 4;
  the ledger lock semantics are covered deterministically here.
