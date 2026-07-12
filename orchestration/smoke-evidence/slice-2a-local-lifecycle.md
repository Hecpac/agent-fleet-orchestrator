# Slice 2A live smoke — local lifecycle and leases

Date: 2026-07-12 (America/Chicago)

Verdict: **PASS**

The smoke used the real cmux workspace, the real `fleet-*` CLI wrappers, and
the installed Ollama `gemma3:4b` worker. No daemon restart applies to this
repository; the production surface under test is cmux + Ollama + the shell and
Python orchestration entrypoints.

## Exact local wait

Fleet `smoke-s2a` was booted with a local `triage` pane and advanced to RECON.
Two tasks were dispatched sequentially to the same surface (`surface:61`):

- `e2f21131-6f4d-4ca9-a8df-34c05be8ea26`: `succeeded`, exit 0, about 4.9 s
  from `running` to terminal.
- `8a2a081d-5c94-40ea-b2de-119df4a05d9c`: `succeeded`, exit 0, about 3.5 s
  from `running` to terminal.

Both waits used `fleet-wait.sh ... --run triage=<exact-run-id> --json`. The
second wait returned only the second run even though the first terminal already
existed in the same ledger. The second run had also completed before its waiter
subscribed, exercising ACK-then-ledger reconciliation of a fast finisher.

The real teardown completed through `fleet-down.sh`:

```text
closed workspace:17 (fleet-smoke-s2a)
```

## Positive-evidence reclaim

A real CLI lease set was acquired for run `smoke-reclaim-1783887163` with
deliberately absent workspace/surface UUIDs, then reconciled against the live
`cmux tree --all --id-format both` surface. Reconciliation appended:

```json
{"exit_code":4,"reason":"workspace_uuid_absent:00000000-0000-0000-0000-000000000901","run_id":"smoke-reclaim-1783887163","status":"abandoned"}
```

It moved the instance, local-slot, and role-slot leases under
`orchestration/runs/archive/leases/smoke-reclaim-1783887163/`; no active lease
remained. Round-trip was under 0.2 s.

## No-regression evidence

- An older terminal on the same local instance did not satisfy the newer wait.
- A completion that preceded subscription was recovered after the stream ACK.
- Reclaim required a successful live tree probe and explicit UUID absence.
- Normal teardown completed with the new closing-owner protocol.

## Open lanes

- Frontier and mixed local/frontier replay across `boot_id`, replay gaps, and a
  durable frontier success/blocked/failed status were not exercised. They are
  intentionally deferred to Slice 2B.
- A real concurrent teardown-versus-dispatch race was not forced in cmux; the
  closing marker is covered by the invariant test.
- A real `SIGKILL` with a still-live pane was not injected. The supported
  operator recovery path is close the workspace, then use
  `fleet-down.sh <feature> --recover-absent`; PID/TTL inference remains deferred.
