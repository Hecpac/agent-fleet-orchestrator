# Mission Control slice 6 evidence — 2026-07-14

## Scope

- Mission-scoped AuditService lifecycle: start, health, idempotent reconnect,
  signed append, live verify, offline verify, and verified stop.
- CONTROL HMAC chain for live integrity plus Ed25519 root receipt for offline
  verification without archiving secret keys.
- Explicit `signed-local` and S3 Object Lock `worm` profiles.
- Anchor receipt per event and assured Mission/runner/teardown integration.

## Deterministic gates

- `python3 -m unittest tests.test_fleet_audit_control tests.test_fleet_audit_integration tests.test_claude_audit_hooks tests.test_mission_run tests.test_fleet_assured_runner`
  passed: 24 tests.
- `python3 -m unittest tests.test_fleet_up` passed, preserving existing boot,
  FDP-2/FDP-3, worktree, archive, and teardown behavior.
- `python3 -m unittest discover -s tests -p 'test_*.py'` passed with the complete
  repository suite.
- `python3 -m compileall -q scripts tests`, `bash -n scripts/fleet-down.sh`,
  `just workflow-validate`, and `git diff --check` passed.

## Signed live/offline smoke

`tests.test_fleet_audit_integration.FleetAuditIntegrationTests.test_signed_lifecycle_is_idempotent_and_verifies_offline`
starts a real subprocess AuditService over a real Unix socket. It performs:

1. health and peer-UID authentication;
2. idempotent `RunStarted`;
3. two identical control-event requests resolving to one event;
4. HMAC signature and chain verification live;
5. an Ed25519-signed root receipt declaring `worm=false`;
6. verified service stop;
7. offline verification using only ledger, public key, anchor receipts, and root receipt.

The resulting chain contains three records and no HMAC/private key material in
the public receipt. The service socket uses a short UID-owned temporary path to
respect macOS AF_UNIX limits; all durable evidence and keys remain inside the
Mission directory.

## WORM contract evidence

- Missing S3 Object Lock bucket/region/credentials prevents service startup and
  leaves neither a live socket nor a false lifecycle record.
- The S3 adapter test verifies PUT uses COMPLIANCE retention and records a
  version ID, then verifies retention headers and the event digest with HEAD.
- Offline WORM verification rejects a correctly signed/compliant-looking chain
  when even one anchor receipt lacks its version ID.
- Raw prompt/tool payload fields are rejected; sinks receive only signed event
  hashes and bounded metadata. A unique secret marker is absent from ledger and
  anchor content.

No real S3 Object Lock credentials or bucket are available in this environment,
so no production WORM write was attempted. The local compliance contract is
fully tested; the external live WORM smoke remains in the final matrix and must
not be reported as executed without a configured compliance sink.

## Invariant review

- Mission-bound assured boot starts/reconciles audit before controller actions.
- Runner effects and FDP-2/FDP-3 terminal receipts are signed by reference.
- Mission success verifies the audit after the archive checkpoint.
- Mission-bound assured teardown verifies before CMUX close and stops the
  service only after workspace/worktree reconciliation.
- `signed` can never report WORM; `worm` cannot degrade to local/test storage.
- Wrong UID, chain/signature corruption, unsafe metadata, missing service, or
  incomplete anchor receipts fail closed.
