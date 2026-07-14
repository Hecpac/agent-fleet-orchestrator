# Mission Control Slice 8 — provider adapters evidence

Date: 2026-07-14

## Implemented

- `fleet_providers.py` defines the common adapter contract and registry.
- Codex, Claude, OpenCode, and Ollama adapters validate launch identity and implement provider-specific submission/evidence semantics.
- `router_config.py`, `workflow_config.py`, `fleet_frontier.py`, the Ollama worker, and Fleet Control consume the adapter boundary.
- Existing `fleet-send.sh` and `fleet-dispatch.sh` CLI arguments are unchanged.
- The registry is an extension point for direct API adapters and accepts deterministic fake implementations.

## Compatibility evidence

Command:

```text
python3 -m unittest tests.test_fleet_providers tests.test_fleet_control tests.test_fleet_frontier tests.test_router_config -q
```

Result: `Ran 78 tests ... OK`.

All pre-existing Codex, Claude, and OpenCode transcript tests pass through the
adapter path with the same success/blocked/failed/indeterminate terminals and
the same retained-lease behavior. The OpenCode single physical submission and
the Codex/Claude prompt pointer remain byte-equivalent to the prior contract.
Identity tests reject provider/model drift and exact OpenCode variant drift.

## Deviation log

- Provider-specific transcript parsers remain in `fleet_frontier.py` during
  this gradual migration and are injected into adapters as evidence readers.
  Moving the parsers themselves would be a riskier rewrite with no contract
  benefit; the kernel decision point is already provider-neutral.
- CMUX submission remains in `fleet-send.sh`. The adapter produces the exact
  payload, while CONTROL retains the visible side effect and confirmation loop.
  This preserves the public CLI and current recovery semantics.
