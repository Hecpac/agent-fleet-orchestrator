# Operating Model

## Principle

Use frontier models for coordination, judgment, and high-risk implementation.
Use local models for parallel, bounded work.

## Local Concurrency Policy

On a 16 GB Apple Silicon Mac:

- Prefer 2-3 small local workers.
- Load only one 8B-ish model at a time.
- Avoid 14B+ models in parallel sessions.
- Keep `gemma4:26b`, `qwen3-coder:30b`, and `devstral:24b` outside the default workflow.

## Escalation Rules

Escalate to the frontier orchestrator when:

- local workers disagree,
- a task crosses architecture boundaries,
- a change touches auth, data loss, money, security, or deployment,
- verification fails,
- a worker returns assumptions instead of evidence.

## Worker Contract

Every worker must end with:

```text
STATUS: <DONE | BLOCKED | FAILED>
SUMMARY:
EVIDENCE:
RISKS:
NEXT_ACTION:
```

## Enforced phase gate

Every fleet starts in `CONTROL`. Supported dispatch paths reject work for a
future phase until the lead advances the durable gate with an evidence
reference:

```bash
just advance <feature> BUILD <scope-or-gate-id>
just send <feature> build "<task>"
```

Configured phases may be skipped only when the preset has no instance in that
phase. `fleet-send.sh` and `fleet-dispatch.sh` validate both UUID identity and
the phase gate before acting. Except for CONTROL, only the currently active
phase may receive work; advancing to CHALLENGE or VERIFY freezes BUILD.

Interactive agents run through an environment allowlist. Codex non-writers use
the read-only sandbox, OpenCode challengers use the `plan` agent, and the
frontier Claude reviewer uses `permission-mode plan`.

The CONTROL lead is the deliberate exception: it uses Codex
`danger-full-access` because CMUX control requires its Unix socket outside the
workspace sandbox. Its environment remains allowlisted, and writer/reviewer
permissions are unchanged.

Local dispatch acquires a per-instance lease and, for heavy models, a global
heavy-worker lease, plus global-local and per-role semaphore slots. Every lease
contains its owning `run_id`. Task text is stored mode `0600`; the ledger stores
only its SHA-256 plus lifecycle/result metadata.

Teardown refuses fleets with active dispatch leases and archives manifest,
phase history, and ledger only after CMUX confirms the workspace disappeared.
