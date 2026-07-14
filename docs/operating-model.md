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
frontier Claude reviewer uses `permission-mode plan`. A mission-bound Codex
specialist gets an ephemeral Codex home with existing authentication and only
the fixed controller-owned CMUX hook bridge. Its named permission profile extends
`:read-only` and permits only the canonical Fleet Control Unix socket path.

The CONTROL lead is the deliberate exception: it uses Codex
`danger-full-access` because CMUX control requires its Unix socket outside the
workspace sandbox. Its environment remains allowlisted, and writer/reviewer
permissions are unchanged.

The process profile is separate from phase/authority. `native` preserves the
current tools; `sandboxed` confines temporary/cache/runtime state to the
ephemeral worker home; `regulated` additionally requires Mission identity and
CONTROL-only effects. Router `tool_access` is identical across profiles.

For contract-v2 interactive runs, a hook event is not ownership by itself.
`fleet-send` must persist an authorization for the exact submit event ID, boot,
sequence, session, workspace, and surface before the waiter can bind it. Raw
CMUX input is therefore visible but untracked. Mission-bound control calls use
a private Unix socket with peer-UID plus Lead-run/capability-token validation.
CONTROL assigns the exact specialist run ID and binds its capability token
before prompt transfer, closing the fast-caller registration race.

Local dispatch acquires a per-instance lease and, for heavy models, a global
heavy-worker lease, plus global-local and per-role semaphore slots. Every lease
contains its owning `run_id`. Task text is stored mode `0600`; the ledger stores
only its SHA-256 plus lifecycle/result metadata.

Teardown refuses fleets with active dispatch leases and nonterminal Missions,
stops Fleet Control, freezes the portable Mission archive while evidence is
available, and removes the manifest only after CMUX confirms disappearance.
Sensitive `full` archives additionally require a distinct scoped, expiring
archive approval; the ordinary assurance approval cannot authorize disclosure.
