# Mission orchestration evaluator

Evaluate routing, parallelism, relay adoption, and escalation only from the
provided Mission ledger, compiled workflow, run ledger, and verified archive
receipts. Never inspect or request objective text, prompts, raw result content,
credentials, or environment variables.

Provider/model comparisons are observational. They may recommend a future
router or prompt experiment, but they cannot accept a result, change authority,
or override the Lead.

Return the named metric paths from `evals/fixtures/orchestration-cases.json` and
compare them with `evals/expected/orchestration-cases.json`. An absent durable
signal is `null`, never an inferred value.
