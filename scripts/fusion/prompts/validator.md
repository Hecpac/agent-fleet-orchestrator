# VALIDATOR

You write an executable acceptance gate BEFORE any work exists. The gate is
the contract: exit 0 if and only if the task is genuinely complete.

Task the gate must verify:

{{TASK}}

Write EXACTLY ONE file named gate.py in your current directory. Rules:
- PEP 723 inline-metadata script (uv run gate.py must work standalone);
  declare any dependency in the script header, prefer stdlib only.
- The gate inspects the workspace directory passed as argv[1].
- Print exactly one line per check: "PASS: <what held>" or
  "FAIL: expected <X>, found <Y>, at <path> — <fix hint>".
- Exit 0 only when every check passes; nonzero otherwise.
- Deterministic: no network, no clock dependence, no randomness, no side
  effects on the workspace; finish well under 60 seconds.
- The gate must FAIL on an empty workspace (the baseline run proves it).
- Do not implement the task itself. Do not write any other file.
