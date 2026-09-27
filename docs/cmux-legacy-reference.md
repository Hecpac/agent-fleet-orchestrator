# Legacy CMUX operational reference

This reference applies only to explicitly selected legacy CMUX work. Herdr is
the project's orchestration lane. Nothing here authorizes a fleet, subdelegation,
provider use, notifications or cleanup beyond the current task. It is not a
Herdr fallback. Preserve existing CMUX readers and historical records.

Consult only the relevant sections: Mental model / Core verbs / Hard rules for
pane control; Authorized legacy fleets for launch and dispatch; Event-driven
waiting for completion and recovery; Memory budget / Agent race / Fleet
etiquette for those specific operations.

Commands, model names and profile details below describe the legacy lane.
Check the current router and supported wrapper before an authorized operation;
do not infer active model identity or Herdr configuration from these examples.
Run the command examples from the repository root.

You control the cmux terminal app through its CLI (`cmux <command>`). Everything
reduces to one loop: **send → read → decide → repeat**.

## Mental model

`Window → Workspace → Pane → Surface (tab)`. A workspace is one agent team.
Address anything with short refs: `workspace:5`, `pane:7`, `surface:7`.
Commands print the refs they create: `OK surface:7 pane:7 workspace:5`.

Set `CMUX_QUIET=1` to silence alias notices.

Durable legacy Missions run through `scripts/fleet_legacy_mission.py`.
`mission-run.py` dispatches Herdr presets first and hands only the remaining
legacy workflows to that driver; it is never a fallback for a Herdr Mission.

## Core verbs

```bash
cmux ping                                              # is the app up? (PONG)
cmux tree --all                                        # map of everything
cmux new-workspace --name "<team>" --cwd "<dir>" --focus false
cmux new-pane --direction right --workspace <ws> --focus false
cmux send --surface <s> --workspace <ws> "<text>"      # type into a terminal
cmux send-key --surface <s> --workspace <ws> enter     # press enter
cmux read-screen --surface <s> --workspace <ws> --lines 40   [--scrollback]
cmux close-surface --surface <s>  /  cmux close-workspace --workspace <ws>
```

Visibility helpers: `rename-tab --surface <s> "<title>"`, `rename-workspace`,
`workspace-action --action set-color --workspace <ws> --color "#7C3AED"`,
`set-status <key> <value>`, `set-progress 0.7 --label "<text>"`,
`notify --title "<t>" --body "<b>"`, `trigger-flash`.

Event stream (prefer listening over polling when waiting on many agents):
`cmux events --limit 20` or `cmux events --reconnect --cursor-file <path>`.

## Hard rules

1. **`send` does not press enter.** Always follow with `send-key ... enter`.
2. **Always `read-screen` after acting.** Never assume a command worked.
3. **Wait on events, not on sleeps.** For fleet members use
   `./scripts/fleet-wait.sh` (see below). Raw stream if you need it:
   `cmux events --name agent.hook.Stop --no-ack --no-heartbeat`. Only fall
   back to short sleep + read-screen for plain shell commands.
4. **Quote carefully.** Text sent via `send` passes through your own shell
   first; single-quote the payload and escape inner quotes.
5. **Never send to a surface you have not identified in `tree` or a manifest.**
   Same for closing: confirm the ref exists in `tree` FIRST — short refs are
   positional, and `close-surface` on a stale ref can resolve to and close a
   DIFFERENT surface (verified incident: a stale ref closed a live lead pane;
   it had to be restored with `claude --resume <session-id>`).
6. Interactive agents (Claude, Codex, OpenCode) need `send` followed by
   `enter`; interrupt interactive TUIs with `escape` and one-shot shells with ctrl-c.
7. **Never launch a fleet OpenCode role directly from the target repository.**
   A raw `opencode ... --agent glm-challenger` can silently fall back to the
   generic `Build` agent when the target does not contain this repo's agent
   definition; that fallback asks for Bash or external-directory permissions
   and can leave the pane waiting. Launch through `fleet-up.sh` or
   `scripts/run-interactive-agent.sh`, then verify the resolved role/policy.
   The supported wrapper installs the repo-owned agent into fresh isolated
   config without importing the controller's OpenCode config, plugins,
   database, history, or state, and fails closed unless `external_directory`
   is denied.
8. **Launch authoritative Kimi review panes only through a Mission-bound
   supported wrapper.** The router pins `kimi-code/k3` with the `plan` thinking
   mode, injects the exact read-only contract through `AGENTS.md`, and runs
   against a sealed read-only clone. Standalone guided Kimi does not yet provide
   that boundary. Current kimi-code no longer supports the legacy `--thinking`,
   `--work-dir`, or repo agent-file flags. Never use `--yolo`, `--afk`, or
   `--print` for a review: those modes can auto-approve shell commands and file
   mutations.

## Authorized legacy fleets

Create or reuse a fleet and dispatch agents only when the user has explicitly
authorized that legacy CMUX work. Audits, reviews, task size and independent
subtasks are not automatic delegation triggers. Consult
`orchestration/router.yaml` for the selected roles and supported wrappers.
Verify results against source evidence; agreement between agents does not
establish correctness or independent assurance.

For an explicitly authorized end-to-end legacy CMUX mission, Dan+ is available:

```bash
just dan <feature> "<complete objective>" --target-repo <repo>
```

This boots the `dan` preset in `autonomous` mode and gives the full mission to
the lead. The lead chooses specialists, dispatches separable work before
waiting, cross-feeds durable results, verifies proportionally, and returns one
exact tracked result. Routine work needs no phase advance or human approval.
Use `fleet_dialogue` when production, money, secrets, destructive actions, or a
known high-risk boundary requires the assured FDP-2/FDP-3 path.

For lower-level/manual operation, boot a team from the capability router. The
default `small` preset creates a Codex lead. Claude is a declared candidate,
but fallback is disabled unless the operator explicitly passes
`--allow-fallback`; `first_available` then walks `lead.candidates` in order.
Panes render by rank: CONTROL → RECON → BUILD → CHALLENGE → VERIFY.

```bash
just fleet <feature>                              # default small: lead only
just fleet-preset <feature> audit                 # canonical preset
./scripts/fleet-up.sh <feature> build=codex verify=reviewer
just fleet-down <feature>
```

`orchestration/router.yaml` is the source of truth for launch commands, models,
capabilities, access declarations, resource classes, display ranks, and presets.
An `instance_id` is addressable in the manifest and points to a reusable
`role_type`; duplicates use names such as `triage_scope=triage` and
`triage_sources=triage`.

Review presets declare `identity_groups`. Router validation requires a distinct
`provider/model/variant` tuple for every member before CMUX effects, and the
plan plus manifest retain the group. This proves configured identity diversity,
not semantic independence. Default race roles must also be identity-diverse;
custom same-model races remain allowed but are never assurance.

`authority`, `tool_access`, and `resource_class` are validated declarations,
and the supported wrappers now enforce important parts of them: interactive
agents inherit an environment allowlist; advisory Codex runs read-only;
OpenCode challengers and Claude verification use plan/read-only modes; local
dispatch uses instance/heavy leases. Direct manual `cmux send` can change the
visible pane but cannot bind a contract-v3 tracked run; keep one writer and use
`fleet-send.sh` for every authoritative turn.
The CONTROL lead alone uses `danger-full-access` so it can reach the cmux Unix
socket; its environment is still allowlisted.
OpenCode fleet reviewers default every tool to deny and resolve with only the
built-in `read`, `glob`, and `grep` tools enabled; Bash, external roots, and
unlisted/future tools remain denied. The only exception is the instance's fresh
isolated `tool-output` directory. The launcher validates the merged
provider policy before TUI boot. FDP-2 supplies Git and dialogue data through a
CONTROL-generated evidence pack embedded in the hash-bound Checker prompt.
Every Fleet Codex process uses an ephemeral Codex home with a
single controller-owned CMUX hook bridge so legacy and current controller hook
trees cannot emit duplicate physical submissions. The automation-only hook
trust bypass applies exclusively to those hard-coded repo-owned overrides;
controller hook command text is never copied. Mission-bound read-only
specialists additionally use a
named profile that extends `:read-only` and permits only the mission's exact
Fleet Control socket. Existing authentication is copied and CMUX hooks are
installed through a fixed controller-owned bridge; unrelated hook commands and
broad controller sandbox settings are not copied.
Pass `--target-repo <path>` to `fleet-up` and each write-authority instance
gets a dedicated `fleet/<feature>/<instance>` branch in an isolated clone at
the target repo's current `HEAD`. The clone has its own Git metadata and no
remote or alternates, so it cannot advance target refs directly. Existing
branches fail closed; CONTROL publishes verified output during teardown by a
durable compare-and-swap intent. Local-worker token spend
accrues in the ledger and `fleet-dispatch` refuses once observed spend reaches
`limits.local_token_budget_per_feature`; this is a soft dispatch-time cap, not
a reservation. `fleet-wait` fires a `cmux notify` ESCALATION on timeout.
Local leases are owner-checked and reclaimed automatically only from a terminal
ledger event or confirmed workspace/surface UUID absence. If a hard-killed
runner leaves leases on a live pane, close that workspace deliberately and use
`./scripts/fleet-down.sh <feature> --recover-absent`; PID/TTL reclaim is not
trusted. Teardown uses a closing marker to reject concurrent dispatch.

Roles come in two kinds:

- **Local workers** (one-shot, prompt-only Ollama workers, dispatched with
  `./scripts/fleet-dispatch.sh <feature> <instance-id> "<task>"`):
  `triage`, `code_worker`, `light_code`, `reviewer`, `general_worker`.
- **Frontier agents** (interactive CLIs, tracked in cmux's agent panel via
  hooks; prompt them by `send`ing text + enter like a human typing):
`codex` (codex CLI),
  `minimax` (OpenCode agent pinned to MiniMax-M3/none, needs `MINIMAX_API_KEY`),
  `glm` (OpenCode `-m` pinned to GLM-5.2 with no variant, needs `ZHIPU_API_KEY`).

Fleet Codex roles copy authentication from the controller home but never load
its general hook/config tree. The router pins `gpt-5.6-sol`; personal model
defaults are not inherited.

Frontier panes are interactive agents: after sending a prompt, wait for their
hook event and then `read-screen`. `fleet-up` preflights executables, keys,
models, and lead health before creating the workspace; an unavailable member
fails closed instead of leaving a shell that can mistake a prompt for a command.

`fleet-up.sh` writes `orchestration/runs/fleet-<feature>.manifest`:

- `manifest_contract_version=3`, `execution_profile`, and
  `tracking_protocol=control-v1` describe enforcement without changing the
  router's declared capabilities. Contracts v1/v2 remain readable for
  standalone fleets; mission-bound v1/v2 runs must be restarted because an
  in-place migration cannot invent their compiled launch binding.

```
schema_version=3
feature=example
preset=implementation_review
workspace=workspace:5
workspace_uuid=<uuid>
lead=surface:6
lead.role_type=codex
build=surface:7
build.role_type=codex
verify=surface:8
verify.role_type=reviewer
```

Read the manifest to address instances. Verify its UUIDs against `tree` before
any destructive action; short refs remain positional. The supported send,
dispatch, wait, race, and teardown scripts perform that comparison and fail
closed.

Every fleet starts with a durable `CONTROL` gate. Advance only with an evidence
reference, then prompt through the gated wrapper:

```bash
just advance <feature> BUILD <scope-or-gate-id>
just send <feature> build "bounded task"
```

Only CONTROL and the currently active phase may receive work. Advancing freezes
earlier phases, so BUILD cannot mutate an artifact during CHALLENGE/VERIFY.
Standalone guided fleets use `--approved-by <operator-attestation>` when
leaving BUILD; this is a label, not proof of human presence. Mission-bound
assured fleets reject that flag and require the exact scoped
`--approval-event-sha256`; the assured runner supplies it from Mission state and
FDP-3 revalidates it before starting.

### Event-driven waiting (preferred — do not poll)

Never busy-wait with sleep + read-screen loops. Dispatch, then block on the
event stream, then read the result once:

```bash
# Local worker: dispatch, retain its run_id, then wait for that exact run
./scripts/fleet-dispatch.sh <feature> code_worker "explain the bug in scripts/foo.sh"
./scripts/fleet-wait.sh <feature> code_worker --run code_worker=<run_id> --timeout 600
cmux read-screen --surface <its surface> --workspace <ws> --lines 50

# Frontier agent: durable send returns a run_id, then wait for that exact turn
./scripts/fleet-send.sh <feature> codex "your prompt"
./scripts/fleet-wait.sh <feature> codex --run codex=<run_id> --timeout 1800
```

`fleet-wait.sh` prints `instance`, `run_id`, terminal `status`, `exit_code`, and
`result_file`; pass `--json` for canonical JSONL. Exit codes are 0 succeeded,
1 failed, 2 usage/identity/protocol, 3 blocked, 4 abandoned, 5 indeterminate,
and 124 deadline. Every local or frontier instance requires `--run`. Frontier
CONTROL authorizes one exact `UserPromptSubmit` event ID/boot/sequence before
it can bind session/surface; completed Stop only wakes exact
`FLEET_RESULT:<run_id>:<STATUS>` verification. Missing or ambiguous evidence is
indeterminate, never success. The sentinel must be the final non-empty line and
the Stop must follow the single bound submit. A replay gap attempts catch-up
from the bounded cmux audit and retains the frontier lease if still
indeterminate; after confirming the agent is quiescent, release it with
`./scripts/fleet-abandon.sh <feature> <instance> <run_id> [reason]`. Partial
sends and unconfirmed race interrupts also retain their surface-UUID lease.
OpenCode emits multiple Stops: only its structured final Stop is eligible, and
the run-tagged user message, complete assistant text, completion time,
provider, and model are read through `opencode db`. Terminal chrome, truncated
workstream preambles, and lossy exports never prove OpenCode completion. Each
OpenCode pane writes its copied database to a surface-scoped temporary XDG data
home so the verifier can query the exact isolated session without consulting
the controller's global database; the wrapper removes it when the pane exits.
Provider, model, and provider-specific variant identity must match the final
database message exactly: MiniMax pins explicit `none`; GLM pins absence.
The dispatch wrappers also accept `--json`; orchestration callers must use that
canonical stdout rather than parse human-facing text; diagnostics stay on
stderr. Audit recovery requires
the recorded baseline plus a continuous boot-scoped sequence.
Local notifications are also wake-ups only. Local workers cannot write files
themselves; feed them a bounded evidence pack and capture their stdout durably
when the result is too large for a pane.

Workers answer with a fixed contract: `STATUS: DONE|BLOCKED|FAILED`, then
`SUMMARY / EVIDENCE / RISKS / NEXT_ACTION`. Parse STATUS before trusting the
rest; treat a missing STATUS block as still-running or failed.

### Memory budget (M5, 16 GB — from orchestration/router.yaml)

- Max **one** 7B+ local model in flight: `code_worker` (qwen2.5-coder:7b),
  `reviewer` (code-auditor), `general_worker` (gemma4).
- Small roles can run in parallel (max 3 local workers total): `triage`
  (gemma3:4b), `light_code` (granite-code:3b).
- Never use gemma4:26b, qwen3-coder:30b, devstral:24b locally.

### Agent race (first success is a candidate)

Only when a legacy CMUX race is explicitly authorized, dispatch the selected
agents on the bounded task. Defaults are identity-diverse; explicit custom
roles may repeat an identity:

```bash
just race <name> "<task>" [instance=role ...]  # defaults come from router
./scripts/fleet-race.sh <name> "<task>" candidate_a=codex candidate_b=minimax
```

Prints the first successful candidate and its screen, then leaves the other
agents running by default. Failed, blocked, and abandoned candidates do not
stop `--any` while another candidate remains viable. Only use
`--cancel-losers` after a separate verification gate. The workspace remains
available for inspection and teardown. A race result is never independent
assurance, even when its configured identities differ.

### Decision queue for an authorized legacy fleet

- Check `just status` at session start and before long waits: it lists every
  tracked agent session, surfacing ⚠️ needsInput ones with age. Agents blocked
  on a human decision have silently waited hours before — don't let them.
- When YOU stop to ask the human a business decision, first fire
  `cmux notify --title "DECISION: <tema en 5 palabras>" --body "<opciones>"`
  so the wait is visible outside your pane.

### Fleet etiquette

- One workspace per team/feature; never mix teams in one workspace.
- In `autonomous` mode, collaboration is lateral but tracked: route a worker's
  exact durable result to any peer in a later `fleet-send` turn. Never inject a
  raw second prompt into a pane with an active run. In `assured` mode, CONTROL
  mediates the fixed dialogue protocol.
- When notifications are authorized for the legacy fleet, emit `cmux notify --title "fleet-<feature>" --body
  "<result>"` so the human sees it without watching.
- When teardown is authorized, use `just fleet-down <feature>` for the exact
  owned fleet after confirming quiescence.
