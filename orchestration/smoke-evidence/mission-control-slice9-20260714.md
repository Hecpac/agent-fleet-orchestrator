# Mission Control Slice 9 — report, trace, and eval evidence

Date: 2026-07-14

## Implemented

- JSON and human Mission reports from verified durable evidence.
- Hierarchical Mission/delegation/agent/relay/assurance/archive/WORM spans.
- Atomic file and optional HTTP trace exporters marked `observational_only`.
- Deterministic routing, parallelism, relay, and escalation eval cases.
- Default privacy boundary excludes objectives, prompts, tasks, raw results, artifacts, credentials, and environment.

## Deterministic evidence

Command:

```text
python3 -m unittest tests.test_fleet_report tests.test_fleet_trace tests.test_fleet_audit_integration -v
```

Result: `Ran 9 tests ... OK` before the final slice gate expansion.

The metrics fixture contains three overlapping durable runs, two sibling
delegations, two challenge/verify results, one relay, and a five-second human
wait. Exact derived values are asserted. A failed HTTP exporter still writes
the requested local trace, returns a warning, remains observational, and never
touches Mission state. The WORM span fixture requires compliance mode; the live
CLI additionally requires the Ed25519-signed public verification envelope.

## Deviation log

- Provider cost is `null` until a durable provider cost receipt exists. Token
  counts are reported when the run ledger contains them; pricing is never
  inferred from mutable external tables.
- Resume and avoided-replay counts are `null` because successful idempotency
  reconciliation deliberately produces no second append. Reporting a guessed
  count would violate the durable-only acceptance criterion.
- Finding contents are private by default. Reports count durable challenge and
  verify result sets, not findings parsed from raw model output.
