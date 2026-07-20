# Agent Fleet Orchestrator

Hybrid orchestration scaffold for frontier and local open-weight models.

The project is designed around one rule: frontier models coordinate and decide;
local models explore, summarize, review, and verify in parallel.

## Operational scope

The hardened target is **S0: one trusted Mac, one trusted Unix UID, one local
`FLEET_RUNS_DIR`**. It is the recommended path for personal experimentation on
that machine, not a multi-tenant or distributed security boundary. It does not
claim containment against arbitrary hostile code already running as the same
UID, a compromised CONTROL/provider/CMUX process, multi-host coordination, or
external regulatory retention. External WORM remains a separate dependency,
and the installed providers do not expose a trustworthy hard total-token
ceiling.

| Before | Now | Practical benefit |
|---|---|---|
| Live router could be consulted again after compilation | The complete canonical router snapshot is embedded and digest-bound in compiled v2 policy | A later router edit cannot silently change an admitted Mission |
| Provider fallback and launches could happen without one Mission-wide reservation record | Fallback is explicit opt-in; each Mission launch advances `reserved → committed → authorized → started → finalized` with exact effect, task, owner, and terminal bindings | Fewer surprise providers, duplicate effects, writer conflicts, and crossed-run finalization |
| Writer work shared target Git internals | Writer uses an isolated clone and CONTROL publishes only after quiescence by durable intent + compare-and-swap | Model-visible work cannot advance target refs before controlled teardown |
| Local token accounting was a standalone dispatch check | Mission policy gates every admitted launch with provider-aware usage semantics; standalone fleets keep the legacy local cap | Unknown usage fails closed when a positive soft limit matters, without pretending unsupported hard ceilings exist |
| Main and assurance work could share one runtime generation | The CONTROL lane is closed before the ASSURED lane opens; the main runtime is preserved by an idempotent handoff | Assurance cannot silently inherit a still-live main Lead or its service state |
| Ledger readers and handoff progress depended on `flock` fairness | Readers consume complete descriptor-pinned immutable snapshots; handoff publishes a durable mutation marker, cuts off late writers, drains admitted writers with a deadline, and then owns the barrier | Status polling cannot starve Mission mutations, and no late mutation can cross handoff silently |

Two S0 debts remain explicit. The structured terminal
`source_event_sha256` is asserted and recorded by trusted CONTROL; it is not a
receipt from an independently controlled verifier. Historical ledgers remain
readable, but there is no authority-preserving in-place migration for a legacy
Mission with active admissions; preserve it as evidence and start a current
Mission instead of upgrading it live. Hard total-token enforcement, multi-host
coordination, and external WORM custody are also outside the implemented scope.

## Target Machine

- MacBook Pro Apple M5
- 16 GB unified memory
- Ollama available locally

## Recommended Local Roles

| Role type | Model | Actual harness capability |
|---|---|---|
| triage | `gemma3:4b` | classify, summarize, and extract supplied evidence |
| code_worker | `qwen2.5-coder:7b` | analyze supplied code and suggest focused patches |
| light_code | `granite-code:3b` | fast analysis of supplied snippets or command output |
| reviewer | `code-auditor:latest` | review a supplied diff/evidence pack |
| general_worker | `gemma4:latest` | heavier synthesis over supplied evidence |

Avoid 14B+ models in local parallel sessions on this 16 GB Mac.
Local workers are prompt-only: they do not receive filesystem, shell, or git
tools. Their input must include the evidence they are expected to analyze.

## Layout

```text
orchestration/
  router.yaml
  agents/
  prompts/
  runs/
local_models/
  ollama/
workflows/
scripts/
evals/
docs/
```

## Quick Start

Check local prerequisites:

```bash
./scripts/check-env.sh
```

Install recommended local workers:

```bash
./scripts/pull-local-models.sh
```

Run a single local worker:

```bash
./scripts/run-local-worker.sh triage "Summarize the orchestration strategy in 5 bullets."
```

Run directly with Python:

```bash
python3 orchestration/agents/local_worker.py \
  --role triage \
  --model gemma3:4b \
  --instruction "Classify and summarize supplied evidence." \
  --prompt "Classify this task and return STATUS/SUMMARY/NEXT_ACTION."
```

The cheapest end-to-end learning path uses only the installed local Ollama
worker and deliberately omits a frontier Lead:

```bash
FLEET_NO_LEAD=1 ./scripts/fleet-up.sh local-safe triage=triage
just advance local-safe RECON local-safe-scope
dispatch="$(./scripts/fleet-dispatch.sh local-safe triage \
  "Summarize the evidence supplied in this prompt." --json)"
run_id="$(jq -er '.run_id' <<<"$dispatch")"
./scripts/fleet-wait.sh local-safe triage \
  --run "triage=$run_id" --timeout 600 --json
./scripts/fleet-down.sh local-safe
```

This path incurs no frontier API inference. Local workers are prompt-only and
have no authenticated MCP client, filesystem, shell, Git, or direct peer
channel; put all required evidence in the prompt.

## cmux Fleet Layer

Terminal orchestration is wired through the cmux CLI. The canonical roster,
capabilities, launch commands, display ranks, limits, and presets live in
`orchestration/router.yaml` (JSON-compatible YAML, parsed with Python stdlib).
The canonical operator guide is
[`docs/guia-uso-flota.md`](docs/guia-uso-flota.md); the shorter
[`docs/guia-operacion-cmux.md`](docs/guia-operacion-cmux.md) is a complementary
CMUX visibility runbook.

### Mission Control (canonical entry point)

Mission Control compiles a typed workflow, creates a durable `mission_id`,
boots the visible fleet, lets the Lead choose its delegation graph, pauses
before high/unknown-risk effects, archives the evidence, and can resume without
replaying an acknowledged effect:

```bash
just mission <feature> "<complete objective>" \
  --workflow implementation --target-repo <repo> --teardown
just mission-dry <feature> "<objective>" --workflow implementation --target-repo <repo>
just mission-show <mission_id>
just mission-resume <mission_id>
just mission-approve <mission_id> <target-repo> --idempotency-key <key>
```

`--execution-profile native` is the default and preserves the current provider
tools. `sandboxed` moves temporary/cache/runtime state inside the worker's
private ephemeral home and declares provider-managed network perimeter without
changing router `tool_access`. `regulated` adds CONTROL-only effect policy and
requires a mission identity. Select either strict profile explicitly:

```bash
just mission <feature> "<objective>" --execution-profile sandboxed --target-repo <repo>
```

Fresh manifests use `manifest_contract_version=3` and
`tracking_protocol=control-v1`. Contracts v1 and v2 remain readable only for
standalone fleets. A mission-bound v1/v2 manifest must be cut over by restarting
the fleet; it cannot be migrated in place because doing so would invent the
compiled launch binding after processes already exist. Standalone legacy
manifests are read as `native` plus `legacy-cmux`, and migration never invents
stronger provenance for an already-live fleet:

```bash
just manifest-inspect orchestration/runs/fleet-<feature>.manifest
just manifest-migrate orchestration/runs/fleet-<feature>.manifest --in-place
```

Canonical Mission boot binds the live router plan to the compiled workflow with
separate main and assurance `launch_digest` values. Each digest covers the
complete private member plans, resolved provider adapters, capability/tool
boundaries, readiness requirements, and plan limits. Compiled contract v2 also
embeds the complete canonical `router_snapshot`, binds it with `router_digest`,
and resolves every later main/assurance launch from that snapshot. It is an
integrity record, not a secret container: do not put credentials in the router.
Effect admission recomputes the selected plan and rejects a changed or resealed
snapshot before any CMUX effect. Contract v1 remains read-only historical data.

### Dan+ autonomous mode (recommended product path)

Dan+ keeps Dan's visible, programmable fleet but removes routine phase driving.
One command boots a heterogeneous team, gives the complete mission to the lead,
lets that lead choose the task graph, and waits for its exact durable result:

```bash
just dan <feature> "<complete objective>" --target-repo <repo>
```

The `dan` preset creates a visible CONTROL lead plus `scout`, `builder`,
`challenger`, and `verifier` panes. It is `mode=autonomous`: every roster phase
is dispatchable without `advance` or routine human approval. The lead decides
which specialists are useful, dispatches separable runs before waiting,
cross-feeds durable results when another perspective is valuable, verifies in
proportion to risk, and reports the writer branch/HEAD. A successful mission is
still bound to an exact lead `run_id`, provider/model evidence, and durable
`result_file`; autonomy does not fall back to screen scraping.

`fleet-run.py` leaves the workspace visible by default. Add `--teardown` only
when you want automatic cleanup. It refuses a dirty target checkout unless
`--allow-dirty-baseline` explicitly acknowledges that writer clones start
from committed `HEAD`. Preview the exact lead mission without CMUX effects:

```bash
just dan-dry <feature> "<objective>" --target-repo <repo>
```

Execution modes are intentionally separate:

| Mode | Entry point | Control owner | Use for |
|---|---|---|---|
| `autonomous` | `just dan` or preset `research` | Lead LLM dynamically decides/delegates | open-ended coding, research, diagnosis |
| `guided` | custom fleet or a preset without explicit mode | Operator/lead advances explicit phases | manual experiments and narrow workflows |
| `assured` | preset `fleet_dialogue` | FDP-2/FDP-3 + scoped Mission approval provenance | stronger local evidence for high-risk work; it does not create an external production boundary |

The design rationale and source research live in `docs/dan-plus.md`.

Boot a team:

```bash
just fleet <feature>                         # default `small`: lead only
just fleet-preset <feature> audit            # canonical audit roster
just fleet-preset <feature> research         # duplicate triage instances have unique IDs
just fleet <feature> build=codex verify=reviewer  # explicit instances
just fleet-down <feature>            # tear down
```

The default lead provider is Codex. Although the router records Claude as a
candidate, automatic fallback is disabled unless the operator explicitly adds
`--allow-fallback`; a failed Codex preflight otherwise fails closed. Select
Claude deliberately with `--lead-provider claude`. Claude, GLM, and MiniMax
dispatches may incur provider charges; the `dan` roster makes them available
but does not make them free. A 2026-07-16 Claude MCP canary reported USD
0.56816 despite `--max-budget-usd 0.05`, so paid inference must remain an
explicitly authorized lane rather than an automatic smoke.
Frontier roles run interactive agent CLIs tracked by cmux's agent panel:
`codex`, `minimax` (opencode + MiniMax-M3, `MINIMAX_API_KEY`), and `glm`
(opencode + GLM-5.2, `ZHIPU_API_KEY`). The `kimi` role pins
`moonshot-ai/kimi-k3` and is available through the guided `kimi_review` preset.
It requires an authenticated Kimi CLI configuration and may incur provider
charges; boot/preflight does not submit an inference request.

Panes are ordered by capability rank rather than argument accident:
`CONTROL → RECON → BUILD → CHALLENGE → VERIFY`. `instance_id` is separate from
`role_type`, so `triage_scope=triage triage_sources=triage` is valid and each
worker remains independently addressable.

Presets that rely on heterogeneous review declare `identity_groups`. The router
fails before CMUX effects unless every member in a group has a distinct
`provider/model/variant` tuple, then persists the resolved groups as
`identity_group.*` in the manifest. This is an auditable identity property, not
a claim that model errors are statistically independent. The default race is
also identity-diverse; custom same-model races remain allowed but return only
an unverified candidate.

Current enforcement boundary: `authority`, `tool_access`, resource classes,
execution profile, and tracked-input protocol are validated contracts. Every
consumer (`fleet-send`, `fleet-dispatch`, `fleet-wait`, `fleet-race`,
`fleet-down`) validates durable UUIDs before acting; dispatch acquires
instance/heavy/slot leases so local concurrency limits hold at runtime;
interactive agents launch through an environment allowlist
(`run-interactive-agent.sh`), with per-authority Codex sandbox levels
(writer → `workspace-write`, CONTROL → `danger-full-access`). Mission-bound
read-only interactive specialists use isolated provider homes and a
controller-owned `fleet_control` MCP proxy connected only to their exact
per-instance Unix endpoint. Prompts receive capability IDs, never token/socket/
CAS paths, and the server requires the exact dispatched lifecycle plus live
frontier lease. Codex reuses the one canonical provider auth file through a
descriptor-verified symlink in an ephemeral home; it never copies a single-use
refresh token, and exact plus `/**` sandbox denies cover lexical and canonical
auth/run paths. Hook commands come from fixed controller-owned CMUX bridges, never
arbitrary user hook text. Optional strict profiles narrow process-owned runtime paths; a
general OS/network sandbox remains provider- and
platform-specific. Direct manual `cmux send` may change a visible pane, but it
cannot create or bind a `control-v1` tracked result: CONTROL must durably
authorize the exact `UserPromptSubmit` event first. `fleet-race` returns the
first successful completion as a candidate; it is not an acceptance gate.

Write isolation: pass `--target-repo <path>` (or set `FLEET_TARGET_REPO`) to
`fleet-up` and every write-authority instance gets a dedicated
`fleet/<feature>/<instance>` branch inside an isolated clone based on the target
repo's current `HEAD`. Writer clones live under `/tmp/fleet_workspaces-<uid>` by default
(override with `FLEET_WORKTREES_DIR`) so agent-visible paths do not disclose the
controller's home directory. The clone has its own Git directory/object store,
no remotes or alternates, and cannot mutate target refs. Existing target branch
names fail closed. `fleet-down` first closes CMUX and retires the clone from the
model-visible path, then records a durable publication intent, imports the
clean descendant commit, creates the target ref by compare-and-swap, and
archives only after the published SHA and quiescence marker are durable. A
crash resumes that exact intent; drift preserves the evidence and fails closed.

Execution economics have two deliberately different paths. A Mission freezes
its deadline, delegation credits, maximum active delegations, writer ownership,
and workflow token policy. Every current Lead, specialist, batch member, and
assured launch then follows one durable sequence:

```text
reserved → committed → authorized → started → finalized
```

That is the external-effect path. A `reserved` or `committed` request abandoned
before launch is explicitly aborted with all bindings intact (spent credits are
not refunded); an `authorized` failure before `started` may finalize with exact
terminal evidence instead of inventing a launch.

`effect_sha256` binds the complete effect contract (prompt/objective, inputs,
output contract, grants, provider identity, runner and, when applicable,
wrapper/transport), while
`task_sha256` binds the exact logical task submitted to the tracked runtime.
Authorization is the deadline-linearization point immediately before the
external effect; a launch authorized before the deadline can record `started`
after it without becoming an orphan. Finalization requires structured terminal
evidence with `schema_version=1`, the exact `source_event_sha256`, `run_id`,
`task_sha256`, and terminal `status`, then binds that evidence again to the
admission, recipient, and writer claim.
Reservations are exact, idempotent, parent-bound, and do not refund spent
delegation credits. Positive soft token limits refuse a new launch when
trustworthy usage is unknown; soft zero disables token admission. Current
Codex, Claude, and OpenCode adapters do not provide trustworthy total-token
enforcement, while Ollama can cap output only; none supports `hard_total`.
Therefore no current workflow may honestly claim a hard total-token ceiling.
Each usage receipt is a closed schema-v1 envelope with
`state=observed|not_incurred|unknown`: only `not_incurred` means exact zero,
`unknown` carries no invented counts, and any missing/unknown source keeps the
aggregate `total_tokens=null`.

Standalone `just dan`, `just fleet`, and `just race` remain compatibility paths:
their local Ollama dispatches feed terminal receipts into
`limits.local_token_budget_per_feature`, which warns at 70% and refuses at
100%. That legacy per-feature check is not the Mission-wide admission ledger
and is not a reservation, so concurrent local runs can overshoot it.
`fleet-wait` fires a `cmux notify` escalation (title `ESCALATION: ...`) naming
stuck instances when its deadline expires.

Approval gate: a standalone `guided`/`assured` fleet records the legacy
`--approved-by <operator-attestation>` label when leaving BUILD. That label is
not proof of human presence. A Mission-bound `assured` fleet rejects the label
and requires `--approval-event-sha256` for the exact current approval event
(`assurance_approved` or `assurance_approval_renewed`); the gate revalidates its
Mission, request, workflow, target scope, risk, and expiry before recording the
reference. The Fleet Control `request_assurance` tool only raises/records the
risk request; it cannot
switch authority lanes while its caller still owns an active admission. The
Mission driver first waits for and finalizes every exact CONTROL admission,
then records `assurance_requested`. An expired approval may be renewed after
expiry either before assurance boot or from a quiescent `assured_running` lane
with no run claims, active admission, or active writer:

```bash
python3 scripts/fleet-approve.py --runs-dir <runs-dir> \
  --mission-id <mission-id> --scope <target-repo> --renew \
  --idempotency-key <stable-key-for-this-renewal>
```

Renewal cannot widen the request, workflow, scope, or risk, and it cannot be
used while the prior approval is still live. In `assured_running` it replaces
the current approval without replaying assurance boot/start. `autonomous`
fleets do not need phase advances: all manifest instances are dispatchable, and
the lead escalates only risk-significant decisions defined by the Dan+ mission
contract.

Mission admissions are authority-laned. `CONTROL` may reserve and commit only
while the main lane is open and may authorize launches only while the Mission
is `running`. `ASSURED` receives a fresh root lane only after approval and the
main lane has no active admissions; CONTROL cannot mint assured admissions and
ASSURED cannot reuse historical CONTROL parents. `mission-run` performs the
internal, crash-recoverable transition with
`fleet-down.sh <feature> --handoff-assurance`. The command is idempotent, stops
only the main Fleet Control generation, preserves the exact main manifest,
state, ledgers, and receipts under
`missions/<mission_id>/assurance-handoff/main-runtime/`, and never creates the
ordinary final Mission archive. Service generations remain separate at
`missions/<mission_id>/control/` and
`missions/<mission_id>/control-assured/`. Operators normally run
`mission-resume`; the handoff flag is an internal recovery primitive, not normal
teardown. Handoff publishes `.mutation.quiescing` while holding its gate,
rejects late mutations immediately as not-applied/not-queued, drains only the
already-admitted writers within a bounded deadline, and retains the barrier
through its exact receipt. Its READY and COMMIT channels are also bounded and
terminate/reap only their exact child on timeout; boot-lock readiness is bounded
as well, and an exited helper fails immediately rather than consuming the rest
of its deadline. `mission-run` supervises the whole handoff with the sum of those
deadlines plus teardown grace, in a dedicated process group that is terminated
as a unit only if that outer bound is exceeded. The active approval is rechecked
for exact workflow/scope/risk and
expiry at assurance boot/start and at every ASSURED reservation, commit, and
launch authorization. Authorization seals its exact approval event hash. Work
already authorized may still record its exact start/terminal evidence after
expiry, but no new ASSURED effect can cross the expired authority.

`scripts/fleet-up.sh` resolves and validates the complete plan before touching
cmux, creates workspace `fleet-<feature>`, verifies the rendered pane order,
and writes `orchestration/runs/fleet-<feature>.manifest`. The manifest records
instance-to-surface mappings plus role types, runners, ranks, authority, and
durable workspace/surface UUIDs.

`just status` is the durable operator radar. It scans pending Decision Briefs
across every canonical Mission in one `FLEET_RUNS_DIR`, groups them by a
redacted stable project identity, and renders them before auxiliary cmux hook
state. `just status --json` emits the machine contract. Status is read-only: an
expired safe default is labelled `DEFAULT_ELIGIBLE`, never applied. An unsafe or
invalid Mission ledger remains visible as `INVALID/UNREADABLE`; valid missions
are still printed and the command exits non-zero.

Mission decisions use a stronger path than a free-form wait. The Lead must bind
one recommendation and one dissenting `CHALLENGE`/`VERIFY` artifact from
different instance and provider/model identities, then publish a closed
Decision Brief with two or three options:

```bash
just mission-decision-request <mission-id> brief.json decision:<stable-key>
just mission-decisions <mission-id> --pending
just mission-decision-show <mission-id> <decision-id>
just mission-decision-resolve <mission-id> <decision-id> <option-id> \
  human:<stable-key> "<reason>"
```

A blocking brief stops only new admissions for its named instances; unrelated
specialists continue. A checkpoint brief lets work continue. Both prevent
Mission completion until resolution. Only low-risk reversible briefs may carry
a default, and CONTROL can durably apply only that exact default after two
hours. Specialists cannot discover or call `request_decision`; the management
socket and Lead CLI share the same Fleet Control core.

The request and its notification enqueue are one crash-atomic Mission-ledger
publication. Before touching CMUX, CONTROL durably claims one attempt against the
current mission-bound Lead surface. A descriptor-anchored per-Mission lock spans
claim, send, and receipt, so separate briefs and separate Fleet Control callers
cannot overlap physical notifications. Exit zero is recorded as CMUX acceptance;
it does not prove that the human read the brief. Explicit rejection retries only
while the decision remains pending, using `5s -> 30s -> 2m -> 10m` capped
backoff with deterministic jitter and a fresh read of the same Mission manifest.
A replacement Lead surface therefore receives the next attempt, but there is no
cross-project/global fallback.

Timeouts, crashes after the durable claim, and receipt-write failures remain
ambiguous and are not retried, preventing duplicate wake-ups at the accepted
cost of a possible missed delivery. Pre-outbox pending briefs are likewise
labelled `legacy_indeterminate`, never silently resent. Consequently this is not
strict `at-least-once` delivery under ambiguity. The ledger/radar remain the
recovery path and the only authority; CMUX events and human-read receipts are
not part of this contract.

Coordination is event-driven, not polled: both `fleet-dispatch` and `fleet-send`
return a durable `run_id`; pass every one back as `--run instance=run_id` to
`fleet-wait`. Local notifications are wake-ups only. Frontier
CONTROL first authorizes one exact `UserPromptSubmit` event ID/boot/sequence;
only that event may bind the exact session/surface, and a completed `Stop` only
wakes verification of the run-specific `FLEET_RESULT` sentinel; Stop by itself
is never success. The sentinel must be the final non-empty line and the Stop
must follow the single bound submit. Replay is boot-scoped, and gaps attempt
bounded catch-up from cmux's audit before failing closed as `indeterminate`.
OpenCode can emit several Stops for one turn: only its final structured Stop is
eligible. Completion then binds the unique run-tagged user message to the last
assistant completed before that Stop and reads full text plus provider/model
through the read-only `opencode db` interface. Codex and Claude likewise bind
the exact turn through their source-specific hook session and transcript files;
their terminal chrome is never completion evidence. Kimi has no native CMUX
hook integration, so a controller-owned bridge tails its Wire 1.2/1.3 log,
publishes metadata-only submit/stop events, and verifies the bounded
`TurnBegin`/`TurnEnd` response before accepting the final sentinel. Kimi's Wire
record does not embed model identity; the isolated launcher therefore attests
the exact provider/model command in the per-surface session record. Every
interactive role pins a model before dispatch. Missing or mismatched evidence
retains the lease as `indeterminate`.
OpenCode reviewers are additionally deny-by-default: only the built-in
`read`, `glob`, and `grep` tools resolve enabled; Bash, external roots, and
every unlisted/future tool remain denied. The only external exception is
OpenCode's fresh isolated `tool-output` directory; controller history is not
copied into it. The launcher validates that exact
merged provider policy inside the isolated XDG environment before TUI boot.
Catch-up requires the recorded baseline and continuous boot-scoped audit
sequence; truncated or corrupt audit evidence is rejected. Event ACKs are
schema-validated before readiness is published.
Local and frontier dispatch leases are owner-checked; frontier exclusivity is
enforced by surface UUID across manifest aliases. Every mission-bound Fleet
Codex specialist uses one descriptor-verified symlink to the canonical
controller authentication file inside its ephemeral home; the OAuth refresh
token is never copied or forked. One controller-owned CMUX hook bridge prevents
legacy and current hook trees from double-submitting a turn. Its
automation-only hook trust bypass applies only to
the three hard-coded repo-owned overrides; user hook command text is never
copied into the process. Mission-bound read-only specialists additionally use the
filtered permission profile described above. The router pins `gpt-5.6-sol`
independently of the user's configured model.
Teardown publishes an atomic closing owner so new dispatches cannot enter
after its lease check.
If a hard-killed runner leaves leases while its pane is still alive, automatic
PID/TTL reclaim is intentionally disabled: close the workspace deliberately,
then run `scripts/fleet-down.sh <feature> --recover-absent`; the UUID-absence
probe records `abandoned`, quarantines the leases, and archives the fleet.
An unrecoverable frontier gap retains its surface lease; release it explicitly
with `scripts/fleet-abandon.sh <feature> <instance> <run_id> [reason]` only
after confirming the agent is quiescent. Unconfirmed partial sends and race
interrupts retain the lease under the same rule.

Cancellation also fails closed. Interactive frontier cancellation uses the
exact tracked run and preserves uncertainty when quiescence is not proven.
Local workers currently expose no exact run-scoped process handle, so Fleet
Control refuses local cancellation and never sends `ctrl-c` to a reusable CMUX
surface that may already belong to a newer run. Wait for/reconcile the exact
`run_id`; do not interpret a rejected cancel as a stopped worker.

`fleet-send` and `fleet-dispatch` accept `--json` for callers such as
`fleet-race`; machine ownership never depends on parsing human-readable output.

Mission-bound fleets also expose a private Unix Fleet Control socket. The
kernel-authenticated peer UID is checked for every connection. Operational MCP
requests are accepted only from a specialist run plus its ledger-bound
capability token; Lead-shaped socket callers fail closed because the Lead uses
the direct CONTROL CLI. Specialists cannot call `complete`, cancel arbitrary
work, escape capability scope, or subdelegate to the writer.
CONTROL preassigns and token-binds a specialist `run_id` before transferring its
prompt, so a fast tool call cannot race delegation registration. OpenCode result
extraction tolerates only a bounded database-visibility delay and otherwise
fails closed.
Frontier specialists never message one another directly: CONTROL mediates
their authenticated MCP calls, CAS artifact grants, and durable dialogue
envelopes. Local Ollama workers have no MCP client; they receive only bounded
prompt evidence and cannot retrieve CAS artifacts by themselves.
The service is reconciled across Mission runner restarts and stopped before
teardown. Health and lifecycle commands are:

```bash
just mission-control-health <mission_id>
just mission-control-start <mission_id>   # idempotent recovery
just mission-control-stop <mission_id>
```

After completion, `just mission-report <mission_id>`,
`just mission-trace <mission_id>`, and
`just mission-archive-verify <archive>` read durable evidence only. Reports and
exporters never decide completion. Assured workflows require signed audit; a
workflow declaring WORM fails closed unless a real S3 Object Lock COMPLIANCE
sink supplies versioned, verifiable anchor receipts. `regulated.yaml` can be
schema-validated for inspection, but effect compilation/execution is
intentionally unavailable today: its hard token policy requires `hard_total`,
which none of the current provider adapters implements, and an honest regulated
run also requires independently controlled external WORM. Supplying
`--workflow regulated --execution-profile regulated` therefore fails before
CMUX effects until both dependencies exist.
The additive `local-worm` workflow proves the same Object Lock mechanics on an
HTTPS loopback backend but records `trust_scope=local-development`; it can never
satisfy `regulated` or any `external-compliance` requirement. See
[`docs/local-worm.md`](docs/local-worm.md) for the pinned RustFS setup, live
smoke, deletion proof, and teardown recipes.
If a workflow declares `credentials` or `private_data` and requests a `full`
archive, CONTROL must first record a separate, scoped, expiring approval:

```bash
just mission-approve-archive <mission_id> <target-repo> \
  --idempotency-key <unique-archive-approval-key>
```

In the `assured` `fleet_dialogue` preset, dialogue is durable and mediated by CONTROL. It does not let workers send
direct peer traffic and publishing a message never dispatches the recipient.
Only the byte-exact `result_file` of an exact terminal `succeeded` run can be
published; the command copies at most 1 MiB into a SHA-256-addressed payload
store and appends a versioned envelope to a separate dialogue ledger. Every
operation first validates the manifest's live workspace and surface UUIDs:

```bash
python3 scripts/fleet_dialogue.py publish orchestration/runs \
  --feature <feature> --kind proposal --recipient <instance> \
  --source-instance <instance> --source-run-id <run_id> \
  --idempotency-key <stable-key> [--reply-to <message_id>]

python3 scripts/fleet_dialogue.py list orchestration/runs \
  --feature <feature> [--recipient <instance>] [--kind challenge]
python3 scripts/fleet_dialogue.py show orchestration/runs \
  --feature <feature> --message-id <message_id>
python3 scripts/fleet_dialogue.py read orchestration/runs \
  --feature <feature> --message-id <message_id>
python3 scripts/fleet_dialogue.py verify orchestration/runs --feature <feature>
```

Message kinds are `proposal`, `challenge`, `rebuttal`, `revision`, and
`verification`. The sender is always `CONTROL`, recipients are exact manifest
instances, retries require the same idempotency key and content, and replies
name at most one prior message. `fleet-down` blocks new publication through its
closing marker and archives both the dialogue ledger and payload store.

### FDP-2 bounded Maker–Checker loop

The `fleet_dialogue` preset adds a fail-closed BUILD loop with fixed identities:
Codex/OpenAI is the only Maker, the dedicated read-only `minimax_checker` pins
`variant: none` through the `minimax-checker` OpenCode agent (the OpenCode
TUI rejects `--variant`, so the boot command carries no `-m` or `--variant`),
GLM remains the CHALLENGE role, and Claude remains the
VERIFY role. The checker variant is persisted in the manifest and lifecycle
ledger and must match OpenCode's final `message.variant`; absence or drift closes
the run as `indeterminate`. CONTROL is a stepper: it
returns one `next_action` but never dispatches a model or publishes an FDP-1
message automatically.

The MiniMax Checker does not execute Bash, Git, or `fleet_dialogue.py` itself.
Before every Checker turn, CONTROL embeds a bounded evidence pack in the
already hash-bound durable prompt: exact message envelope and source result,
base/head/branch identity, clean worktree state, commit metadata, name-status,
and a patch produced with external diff/text conversion disabled. This keeps
the dialogue auditable without exposing a process tool to untrusted model input.

OpenCode 1.18.0 records `variant: none` for MiniMax but omits the field for the
current GLM session. MiniMax fleet roles therefore pin that value in dedicated
project agents and carry it through the manifest and lifecycle ledger; GLM
retains an absent variant. The verifier requires the provider-specific value
exactly and does not reinterpret or discard either representation.

Start the fleet, enter BUILD, and freeze a strict task spec:

```bash
./scripts/fleet-up.sh <feature> --preset fleet_dialogue --target-repo <repo>
python3 scripts/fleet_state.py advance \
  orchestration/runs/fleet-<feature>.manifest BUILD --evidence <scope-evidence>

cat > /tmp/fdp2-task.json <<'JSON'
{
  "objective": "one bounded objective",
  "negative_scope": ["one explicit exclusion"],
  "acceptance_criteria": ["one observable acceptance condition"]
}
JSON

python3 scripts/fleet_dialogue_controller.py start orchestration/runs \
  --feature <feature> --spec-file /tmp/fdp2-task.json \
  --idempotency-key <stable-start-key>
```

The controller copies the exact spec into the conversation store and freezes
its SHA-256. For every returned `next_action`:

- `action=dispatch`: read `prompt_file` with `prompt="$(<"$prompt_file")"`,
  dispatch it to the exact `instance` using `fleet-send.sh`, wait for that exact
  `run_id`, then call `step --run-id <run_id>` with a new stable idempotency key.
- `action=publish`: publish the exact source result through
  `fleet_dialogue.py publish` using every returned binding (`kind`, `recipient`,
  `source_instance`, `source_run_id`, `reply_to`), then call
  `step --message-id <message_id>` with a new stable idempotency key.
- `action=terminal`: stop the loop. Late runs and messages cannot change it.

The prompt files intentionally have no trailing newline, so Bash command
substitution preserves the byte sequence whose hash is recorded by CONTROL.
Each Maker result must add exactly one commit to the current accepted base and
leave its dedicated branch clean. Checker output is strict JSON; malformed
output, a 30-minute run timeout, the four-hour conversation deadline, or an
attempted fourth revision round closes the conversation without acceptance.

Inspect or verify without causing model work:

```bash
python3 scripts/fleet_dialogue_controller.py show orchestration/runs --feature <feature>
python3 scripts/fleet_dialogue_controller.py verify orchestration/runs --feature <feature>
python3 scripts/fleet_dialogue_controller.py abandon orchestration/runs \
  --feature <feature> --reason <reason> --idempotency-key <stable-abandon-key>
```

An `accepted` dialogue still does not advance phases. In a standalone fleet the
operator supplies the legacy attestation below; the gate also rechecks that the
Maker branch is clean and still at the exact accepted HEAD:

```bash
python3 scripts/fleet_state.py advance \
  orchestration/runs/fleet-<feature>.manifest CHALLENGE \
  --evidence <accepted-dialogue-evidence> \
  --approved-by <operator-attestation>
```

For a Mission-bound assured fleet, first record the scoped Mission approval
with `just mission-approve`; `mission-run` and the assured runner then propagate
its exact event hash automatically. Supplying `--approved-by` on that path is a
hard error.

`fleet-down` refuses an active conversation. For a terminal one it creates a
live verification receipt and archives the control hash chain, task spec,
prompts, FDP-1 ledger, and payloads. The archive remains independently
verifiable with `fleet_dialogue_controller.py verify --archive <archive>`.

### FDP-3 sequential assurance

After FDP-2 reaches `accepted` and the approval-bound gate advances BUILD to
CHALLENGE, FDP-3 runs one identity-diverse GLM challenge followed by one Claude verification. It has
its own hash-chained controller ledger and never reopens Maker automatically.
Start it only from the exact clean FDP-2 accepted HEAD:

```bash
python3 scripts/fleet_assurance_controller.py start orchestration/runs \
  --feature <feature> --idempotency-key <stable-start-key>
```

Start copies and hashes the FDP-2 task spec, bound messages, and lifecycle
context, then creates separate detached worktrees for GLM and Claude at the
accepted commit. Both roles remain read-only. As in FDP-2, CONTROL executes
each returned action explicitly:

- `action=dispatch`: send the exact `prompt_file` to the named instance, wait
  for the exact `run_id`, then use `step --run-id`.
- `action=publish`: relay the exact result with `fleet_dialogue.py publish`,
  then use `step --message-id`.
- `action=advance_phase`: advance CHALLENGE to VERIFY with the returned exact
  `evidence` hash, then acknowledge it with `step --phase-advanced`.
- `action=terminal`: stop. Terminal assurance is immutable.

```bash
python3 scripts/fleet_state.py advance \
  orchestration/runs/fleet-<feature>.manifest VERIFY \
  --evidence <fdp3-control-head>
python3 scripts/fleet_assurance_controller.py step orchestration/runs \
  --feature <feature> --phase-advanced --idempotency-key <stable-key>
```

GLM returns findings only. Claude must separately adjudicate every GLM
finding and returns exactly `VERIFIED` or `REJECTED`; malformed JSON, identity
drift, a 30-minute run timeout, or the two-hour absolute deadline closes
fail-closed. There are no retries or provider fallbacks.

`fleet-down` requires terminal FDP-3 whenever the fleet reached CHALLENGE or
VERIFY. It writes a separate assurance receipt, refuses dirty or drifted
snapshots, removes only clean exact detached worktrees, and archives the copied
context, prompts, lifecycle, messages, and both control ledgers. Verify that
archive offline with:

```bash
python3 scripts/fleet_assurance_controller.py verify --archive <archive>
```

The orchestration playbook (mental model, verbs, wait rules, memory budget)
lives in both `.agents/skills/cmux/SKILL.md` and `.claude/skills/cmux/SKILL.md`;
the two copies must stay synchronized.
