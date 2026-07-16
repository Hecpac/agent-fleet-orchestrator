# Agent Fleet Orchestrator

Hybrid orchestration scaffold for frontier and local open-weight models.

The project is designed around one rule: frontier models coordinate and decide;
local models explore, summarize, review, and verify in parallel.

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

Fresh manifests use `manifest_contract_version=2` and
`tracking_protocol=control-v1`. Legacy manifests are read as `native` plus
`legacy-cmux`; migration never invents stronger provenance for an already-live
fleet:

```bash
just manifest-inspect orchestration/runs/fleet-<feature>.manifest
just manifest-migrate orchestration/runs/fleet-<feature>.manifest --in-place
```

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
which specialists are useful, dispatches independent runs before waiting,
cross-feeds durable results when another perspective is valuable, verifies in
proportion to risk, and reports the writer branch/HEAD. A successful mission is
still bound to an exact lead `run_id`, provider/model evidence, and durable
`result_file`; autonomy does not fall back to screen scraping.

`fleet-run.py` leaves the workspace visible by default. Add `--teardown` only
when you want automatic cleanup. It refuses a dirty target checkout unless
`--allow-dirty-baseline` explicitly acknowledges that writer worktrees start
from committed `HEAD`. Preview the exact lead mission without CMUX effects:

```bash
just dan-dry <feature> "<objective>" --target-repo <repo>
```

Execution modes are intentionally separate:

| Mode | Entry point | Control owner | Use for |
|---|---|---|---|
| `autonomous` | `just dan` | Lead LLM dynamically decides/delegates | open-ended coding, research, diagnosis |
| `guided` | `just fleet[-preset]` | Operator/lead advances explicit phases | manual experiments and narrow workflows |
| `assured` | preset `fleet_dialogue` | FDP-2/FDP-3 state machines + human BUILD exit | production, money, secrets, destructive or high-risk work |

The design rationale and source research live in `docs/dan-plus.md`.

Boot a team:

```bash
just fleet <feature>                         # default `small`: lead only
just fleet-preset <feature> audit            # canonical audit roster
just fleet-preset <feature> research         # duplicate triage instances have unique IDs
just fleet <feature> build=codex verify=reviewer  # explicit instances
just fleet-down <feature>            # tear down
```

The default lead provider is Codex. Claude is an enabled fallback candidate:
`fallback_policy: first_available` walks `lead.candidates` in order, so Claude
leads only when Codex is unavailable or when you pass `--lead-provider claude`.
Frontier roles run interactive agent CLIs tracked by cmux's agent panel:
`codex`, `minimax` (opencode + MiniMax-M3, `MINIMAX_API_KEY`), and `glm`
(opencode + GLM-5.2, `ZHIPU_API_KEY`).

Panes are ordered by capability rank rather than argument accident:
`CONTROL → RECON → BUILD → CHALLENGE → VERIFY`. `instance_id` is separate from
`role_type`, so `triage_scope=triage triage_sources=triage` is valid and each
worker remains independently addressable.

Current enforcement boundary: `authority`, `tool_access`, resource classes,
execution profile, and tracked-input protocol are validated contracts. Every
consumer (`fleet-send`, `fleet-dispatch`, `fleet-wait`, `fleet-race`,
`fleet-down`) validates durable UUIDs before acting; dispatch acquires
instance/heavy/slot leases so local concurrency limits hold at runtime;
interactive agents launch through an environment allowlist
(`run-interactive-agent.sh`), with per-authority Codex sandbox levels
(writer → `workspace-write`, CONTROL → `danger-full-access`). Mission-bound
read-only Codex specialists use an ephemeral Codex home and a named permission
profile that extends `:read-only` while allowing only the mission's exact Fleet
Control Unix socket. Existing Codex authentication is copied into it; hook
commands come from a fixed controller-owned CMUX bridge, never arbitrary user
hook text. Optional strict profiles narrow process-owned runtime paths; a
general OS/network sandbox remains provider- and
platform-specific. Direct manual `cmux send` may change a visible pane, but it
cannot create or bind a `control-v1` tracked result: CONTROL must durably
authorize the exact `UserPromptSubmit` event first. `fleet-race` returns the
first successful completion as a candidate; it is not an acceptance gate.

Write isolation: pass `--target-repo <path>` (or set `FLEET_TARGET_REPO`) to
`fleet-up` and every write-authority instance gets a dedicated
`fleet/<feature>/<instance>` branch and worktree, based on the target repo's
current `HEAD`. Writer worktrees live under `/tmp/fleet_workspaces-<uid>` by default
(override with `FLEET_WORKTREES_DIR`) so agent-visible paths do not disclose the
controller's home directory. Existing branch names fail closed. `fleet-down` refuses
uncommitted changes, records the final SHA, preserves branches with commits,
and removes branches that never advanced beyond their base SHA.

Execution economics: local worker runs record prompt/completion token counts
in the per-feature ledger; `limits.local_token_budget_per_feature` in the
router makes `fleet-dispatch` warn at 70% observed spend and refuse new
dispatches at 100%. This is a soft dispatch-time cap, not a reservation, so
already-concurrent runs may overshoot it. `fleet-wait` fires a `cmux notify`
escalation (title `ESCALATION: ...`) naming the stuck instances when its
deadline expires.

Human-in-the-loop: in `guided` and `assured` modes, advancing the durable phase
gate out of BUILD requires `--approved-by <human>` in addition to `--evidence`;
the approver is recorded in the state history. `autonomous` fleets do not need
phase advances: all manifest instances are dispatchable, and the lead escalates
only risk-significant decisions defined by the Dan+ mission contract.

`scripts/fleet-up.sh` resolves and validates the complete plan before touching
cmux, creates workspace `fleet-<feature>`, verifies the rendered pane order,
and writes `orchestration/runs/fleet-<feature>.manifest`. The manifest records
instance-to-surface mappings plus role types, runners, ranks, authority, and
durable workspace/surface UUIDs.

`just status` is the decision-queue radar: it lists every tracked agent
session (via cmux hook data) and surfaces the ones blocked waiting on a human,
with age — check it whenever you return to the machine.

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
their terminal chrome is never completion evidence. Every interactive role pins
a model before dispatch, and the structured result must confirm it. Missing or
mismatched evidence retains the lease as `indeterminate`.
Catch-up requires the recorded baseline and continuous boot-scoped audit
sequence; truncated or corrupt audit evidence is rejected. Event ACKs are
schema-validated before readiness is published.
Local and frontier dispatch leases are owner-checked; frontier exclusivity is
enforced by surface UUID across manifest aliases. Every Fleet Codex process
copies controller authentication into an ephemeral home with one
controller-owned cmux hook bridge, so legacy and current hook trees cannot
double-submit a turn. Its automation-only hook trust bypass applies only to
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
exporters never decide completion. Assured workflows require
signed audit; a workflow declaring WORM fails closed unless a real S3 Object
Lock COMPLIANCE sink supplies versioned, verifiable anchor receipts.
Use `--workflow regulated --execution-profile regulated` for that path; its
backend configuration is preflighted before any CMUX fleet is created.
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

An `accepted` dialogue still does not advance phases. A human must approve the
BUILD exit, and the gate rechecks that the Maker branch is clean and still at
the exact accepted HEAD:

```bash
python3 scripts/fleet_state.py advance \
  orchestration/runs/fleet-<feature>.manifest CHALLENGE \
  --evidence <accepted-dialogue-evidence> --approved-by <human>
```

`fleet-down` refuses an active conversation. For a terminal one it creates a
live verification receipt and archives the control hash chain, task spec,
prompts, FDP-1 ledger, and payloads. The archive remains independently
verifiable with `fleet_dialogue_controller.py verify --archive <archive>`.

### FDP-3 sequential assurance

After FDP-2 reaches `accepted` and a human advances BUILD to CHALLENGE, FDP-3
runs one independent GLM challenge followed by one Claude verification. It has
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

GLM returns findings only. Claude must independently adjudicate every GLM
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
