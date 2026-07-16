# Orchestration evals

These deterministic cases exercise the Mission report's routing, parallelism,
relay, and escalation metrics. Fixtures name only durable signal types; the
test harness supplies synthetic hash-addressed events and compares the derived
report with `expected/`.

The evaluator is non-authoritative. Its output can inform later routing and
prompt changes but cannot change Mission completion or Lead authority.
