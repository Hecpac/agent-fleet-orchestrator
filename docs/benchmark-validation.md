# Benchmark Validation Notes

As of 2026-07-07:

- SWE-bench is the strongest public signal for real coding-agent issue resolution.
- Aider Polyglot is useful for terminal-based code editing and instruction following.
- Artificial Analysis includes Terminal-Bench and broader quality/speed/cost signals.
- Anthropic's multi-agent research report supports orchestrator-worker designs for
  breadth-first tasks, but also warns that multi-agent systems can use many more tokens.

Design consequence:

- frontier model: orchestrator, architecture, final decision;
- local model: bounded worker, reviewer, summarizer, classifier;
- verification: real tests, smoke checks, or command output, not model confidence.
