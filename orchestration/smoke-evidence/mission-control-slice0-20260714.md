# Mission Control Slice 0 smoke — 2026-07-14

## Scope

Slice 0 changes only static contracts, fixtures, tests, and
`INTERNAL_WIRING.md`; it loads no code into a daemon. Restart, PID, port,
watchdog, and daemon stderr checks are therefore not applicable. The real
surface for this slice is loading the JSON schemas and JSON-compatible YAML
fixtures in a clean Python process.

## Live command

```text
python3 -m json.tool orchestration/workflow.schema.json
python3 - <schema/fixture loader and duplicate-key/unknown-key probes>
```

Observed output:

```text
SMOKE PASS schemas=4 valid=implementation duplicate=rejected shell_escape=rejected
round-trip: 0.048 seconds
```

## Non-regression

The duplicate `schema_version` fixture raised `duplicate key: schema_version`.
The shell fixture exposed exactly the forbidden root key `command`; the schema
does not declare `authority`, `tool_access`, `run_id`, or CMUX surfaces.

## Verdict

PASS — all four contracts parse, the valid fixture loads, and both duplicate
keys and the representative runtime escape are rejected by the frozen
contract.

## Open lanes

- Runtime compiler enforcement is intentionally not exercised in Slice 0; it
  is the acceptance surface for Slice 1 and will be closed by the compiler CLI
  smoke.
- CMUX and agent execution are intentionally not exercised because Slice 0
  changes no runtime path; Mission Control live smokes close those lanes in
  Slices 3–10.
