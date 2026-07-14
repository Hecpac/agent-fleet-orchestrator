# Mission Control Slice 1 smoke — 2026-07-14

## Surface

The compiler is a standalone CLI and loads no daemon code, so restart/PID/port
checks are not applicable. The live surface exercised was the real
`workflow_config.py` CLI against every repository workflow and the real router.

## Commands and observations

```text
python3 scripts/workflow_config.py validate workflows/*.yaml
python3 scripts/workflow_config.py show workflows/implementation.yaml
python3 scripts/workflow_config.py compile workflows/implementation.yaml
```

All three workflows returned `workflow valid`. Two independent `compile`
processes produced byte-identical output (`cmp` exit 0). The compiled artifact
was parsed in a third process and reported:

```text
SMOKE PASS digest=5122734dd8bf7404159708b0f2240efe72243919a06c72e292e401ea5c7b1600 writer=builder mode=autonomous
```

## Non-regression

The CLI rejected duplicate keys, unknown `command`, unknown `authority`,
unprovided capabilities, writer drift, invalid assurance gates, and invalid
subdelegation depth in the specific test gate. The compiler test replaces
`subprocess.run` with a failing sentinel, proving compile does not invoke CMUX
or provider healthchecks.

## Verdict

PASS — validate/show/compile work through the real CLI, compilation is
deterministic, and the router resolves the expected autonomous writer without
runtime effects.

## Open lanes

- Mission creation and persistence are not part of the compiler; Slice 2 closes
  that lane.
- CMUX boot remains intentionally untested until the canonical mission runner
  exists in Slice 3.
