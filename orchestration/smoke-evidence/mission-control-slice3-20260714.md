# Mission Control Slice 3 live smoke — 2026-07-14

## Environment and boot

- Real `cmux` binary: `/opt/homebrew/bin/cmux`
- `cmux ping`: `PONG`
- Router healthcheck: `DAN_READY lead=openai instances=openai,openai,zai,anthropic`
- Target: fresh temporary Git repository, branch `main`, empty baseline commit
  `d7c2769b9834b779049736f1bd80115069f76087`
- Workflow: `implementation` / preset `dan`
- Mission: `089ea5ec-b9d0-5da5-ba4d-653e4fd53d1f`
- Mission objective explicitly prohibited edits, commits, and delegation.

The real fleet boot exposed Lead, Scout, Builder, Challenger, and Verifier panes.
The manifest bound the exact mission ID. The Lead run was
`32663e77-6be7-424b-88c9-88cf61dc1e1f` on `openai/gpt-5.6-sol`.

## End-to-end trace

```text
mission_created -> workflow_compiled -> risk_assessed -> fleet_boot_started ->
mission_running -> lead_dispatch_intent -> lead_dispatched ->
lead_result_recorded -> mission_completing -> archive_created -> mission_terminal
```

Round-trip from first mission event to immutable terminal: `73.653 seconds`.

Offline mission verification after teardown:

```json
{"events": 11, "head_sha256": "c584b11d9f35cc20fe4a2e87b91d1c34753bcfad8683f97add3901ceeacae41a", "mission_id": "089ea5ec-b9d0-5da5-ba4d-653e4fd53d1f", "status": "succeeded", "valid": true}
```

Accepted Lead artifact:

```text
artifact_id=109bd817330385a4a775df62bd336299d547d646c7e22f7fbf5f26b1e39b022b
STATUS: DONE
DELEGATION: None, as required.
VERIFICATION: HEAD=d7c2769b9834b779049736f1bd80115069f76087; branch=main;
              git status --short --branch returned exactly "## main";
              porcelain status returned empty output.
```

Post-run controller checks independently confirmed `## main`, mission chain
valid, manifest removed, and CMUX workspace absent. Legacy teardown archived
manifest/state/ledger.

## High-risk no-effect smoke

A separate real CLI mission with objective `deploy this to production` returned
exit `3`, risk `high`, categories `external_side_effect`, `production`, and
`repository_local`, and state `awaiting_assurance_confirmation`. Resume returned
the same mission ID/state and no fleet manifest existed.

## Verdict

PASS — canonical dry-run, high-risk pause/resume, autonomous Lead execution,
exact provider/model-bound result acceptance, immutable terminal verification,
and teardown all worked through real CLI/CMUX surfaces.

## Open lanes

- Builder commit, two-specialist parallelism, and result relay are Fleet Control
  smokes for Slice 4, not exercised by this read-only Slice 3 mission.
- Human approval into FDP-2/FDP-3 is deferred to the assured runner in Slice 5.
- The Slice 3 archive is an explicit ledger/result checkpoint; portable unified
  archive verification is deferred to Slice 7.
