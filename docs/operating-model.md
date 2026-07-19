# Operating Model

## Principle

Use frontier models for coordination, judgment, and high-risk implementation.
Use local models for parallel, bounded work.

This model is scoped to one trusted Mac and one trusted Unix UID. Same-UID
hostile processes, compromised CONTROL/provider/CMUX binaries, multi-host
coordination, external WORM custody, and hard total-token enforcement are
outside the S0 boundary.

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

Every fleet starts in `CONTROL`. In `guided` and `assured`, supported dispatch
paths reject work for a future phase until CONTROL advances the durable gate
with an evidence reference:

```bash
just advance <feature> BUILD <scope-or-gate-id>
just send <feature> build "<task>"
```

Configured phases may be skipped only when the preset has no instance in that
phase. `fleet-send.sh` and `fleet-dispatch.sh` validate both UUID identity and
the phase gate before acting. Except for CONTROL, only the currently active
phase may receive work; advancing to CHALLENGE or VERIFY freezes BUILD. In
`autonomous`, all roster phases are dispatchable and no routine `advance` is
required; identity, exact run ownership, one writer, admission, and completion
checks remain enforced. The current `dan` and `research` presets are
`autonomous`; presets without an explicit mode are `guided`, and
`fleet_dialogue` is `assured`.

Interactive agents run through an environment allowlist. Codex non-writers use
the read-only sandbox; OpenCode reviewers default every tool to deny, enable
only built-in `read`/`glob`/`grep`, and deny external roots except their fresh
isolated tool-output directory; the frontier Claude reviewer uses
`permission-mode plan`. OpenCode's resolved policy is
validated before its TUI boots. A mission-bound Codex specialist gets an
ephemeral Codex home whose only auth binding is a descriptor-verified symlink to
the canonical controller store; the OAuth refresh token is never copied. Its
named permission profile starts at `:minimal`, reopens only the isolated
workspace, denies exact and `/**` lexical/canonical credential and run paths,
and permits only the canonical Fleet Control Unix socket path. Codex, Claude,
and OpenCode all pass the same real MCP proxy/socket preflight before TUI start.

The CONTROL lead is the deliberate exception: it uses Codex
`danger-full-access` because CMUX control requires its Unix socket outside the
workspace sandbox. Its environment remains allowlisted, and writer/reviewer
permissions are unchanged.

The process profile is separate from phase/authority. `native` preserves the
current tools; `sandboxed` confines temporary/cache/runtime state to the
ephemeral worker home; `regulated` additionally requires Mission identity and
CONTROL-only effects. Router `tool_access` is identical across profiles.
`regulated.yaml` is statically inspectable but cannot currently admit effects:
its hard token ceiling requires provider capability `hard_total`, unavailable
in every current adapter, and its trust claim also requires external WORM.

For `control-v1` interactive runs under manifest contract v2 or v3, a hook event
is not ownership by itself.
`fleet-send` must persist an authorization for the exact submit event ID, boot,
sequence, session, workspace, and surface before the waiter can bind it. Raw
CMUX input is therefore visible but untracked. Mission-bound control calls use
a private Unix socket with peer-UID plus Lead-run/capability-token validation.
CONTROL assigns the exact specialist run ID and binds its capability token
before prompt transfer, closing the fast-caller registration race.

Frontier specialists communicate only through CONTROL-mediated MCP, CAS grants,
and durable dialogue. Local Ollama workers are prompt-only, have no MCP client,
and cannot follow an artifact ID to controller storage.

Local dispatch acquires a per-instance lease and, for heavy models, a global
heavy-worker lease, plus global-local and per-role semaphore slots. Every lease
contains its owning `run_id`. Task text is stored mode `0600`; the ledger stores
only its SHA-256 plus lifecycle/result metadata.

Mission launches additionally pass a global admission transaction. The compiled
workflow freezes deadline, delegation credits, maximum active delegations,
writer ownership, and token policy. Every current Lead, specialist, batch
member, and assured run advances through the same state machine:

```text
reserved → committed → authorized → started → finalized
```

This is the path for an external effect. A `reserved` or `committed` request may
be aborted prelaunch only with its exact run/request/effect/task/recipient/writer
bindings; spent credits are not refunded. An `authorized` failure before
`started` may finalize with exact terminal evidence and does not fabricate a
launch event.

Reservation claims the recipient, run identity, credits, and optional writer.
Commit binds that claim to the exact process request. `effect_sha256` freezes
the full effect contract and `task_sha256` freezes the submitted logical task.
Authorization is the last durable, deadline-checked step before the wrapper
effect; `started` can safely be recorded after the deadline when it references
that pre-deadline authorization. Finalization releases ownership only after a
structured terminal envelope binds the exact `run_id`, `task_sha256`, terminal
status, `source_event_sha256`, recipient, and writer claim. The envelope is
closed schema version 1. A positive soft token limit refuses unknown usage,
while soft zero disables token admission. The
router's `local_token_budget_per_feature` remains a standalone legacy Ollama
dispatch check, not a substitute for Mission admission.
Usage receipts are closed schema-v1 envelopes with
`state=observed|not_incurred|unknown`; only `not_incurred` is exact zero, and
any missing or unknown source preserves aggregate `total_tokens=null`.

Admission authority is split into non-overlapping lanes. The main lifecycle
uses actor `CONTROL`; the assurance lifecycle uses actor `ASSURED`. Reservation
and commit are allowed for CONTROL in `compiled`/`booting`/`running` and for
ASSURED in `assured_booting`/`assured_running`; launch authorization requires
`running` or `assured_running`, respectively. Assurance cannot inherit a CONTROL
parent, CONTROL cannot mutate ASSURED admissions, and the Mission cannot
request, complete, or archive while any admission remains active.

The Fleet Control `request_assurance` call records/escalates risk only. It does
not transition the Mission from the caller's still-active CONTROL lane. The
driver waits for exact terminal evidence, finalizes the Lead and every child,
then appends `assurance_requested`. Once approved, `mission-run` performs an
internal idempotent `fleet-down --handoff-assurance`: it stops only the main
service generation, moves the main runtime to
`missions/<mission_id>/assurance-handoff/main-runtime/`, and leaves the final
Mission archive untouched. Main and assured service state are kept separately
under `control/` and `control-assured/`. A durable quiescing marker cuts off
late mutations before the barrier drains already-admitted writers. Rejected
mutations are not applied or queued; READY, drain, and COMMIT waits are bounded
and preserve the existing recovery intent/receipt protocol. Boot-lock readiness
is bounded too. The Mission driver gives the enclosing teardown more time than
the sum of its internal waits and runs it in a separately reaped process group,
so an outer timeout cannot strand a handoff child holding the barrier.

Presets may declare `identity_groups` for review paths whose configured model
diversity matters. Router validation requires at least two members and a unique
`provider/model/variant` tuple for every member before CMUX starts. Plans,
compiled workflows, and manifests preserve the group membership. Duplicate
roles remain valid outside such a group, and tuple diversity is observational
evidence—not proof that conclusions or model errors are independent.

BUILD-exit approval has two explicit provenance levels. Standalone guided
fleets retain `--approved-by` as a legacy operator attestation only.
Mission-bound assured fleets reject that string and require the exact active
approval event hash (`assurance_approved` or `assurance_approval_renewed`);
phase advance and FDP-3 both revalidate the Mission, request, workflow, scope,
risk, and expiry. This binds durable provenance but does not prove out-of-band
human presence because CONTROL and the approval CLI still share the local Unix
trust boundary.

An approval may be renewed explicitly with `fleet-approve.py ... --renew` only
after the prior approval expires. Renewal is allowed before
`assurance_boot_started`, or in `assured_running` after every run claim,
admission, and writer claim is inactive; the latter replaces the current
approval without replaying boot/start. It keeps the same request, workflow,
scope, and risk. A still-live approval, active assured work, or attempted
widening fails closed. Approval is rechecked at assurance boot/start and for
every ASSURED reservation, commit, and launch authorization; authorization
seals the exact current approval event hash. A previously authorized effect may
record start/final evidence after expiry, but no new effect crosses it.

Cancellation is exact-run or no-op. Frontier cancellation retains uncertainty
until its tracked run is reconciled. Local workers do not yet expose an exact
run-scoped process handle, so Fleet Control refuses their cancellation rather
than sending `ctrl-c` to a reusable CMUX surface.

Teardown refuses fleets with active dispatch leases and nonterminal Missions,
stops Fleet Control, freezes the portable Mission archive while evidence is
available, and removes the manifest only after CMUX confirms disappearance.
Sensitive `full` archives additionally require a distinct scoped, expiring
archive approval; the ordinary assurance approval cannot authorize disclosure.

Known S0 debts are explicit: trusted CONTROL supplies the structured terminal
`source_event_sha256` (there is no independent source-event attestor), and an
active legacy admission ledger has no authority-preserving in-place migration.
Historical formats remain readable; operators must not mint new effects from a
legacy active Mission or claim the old record acquired current provenance.
