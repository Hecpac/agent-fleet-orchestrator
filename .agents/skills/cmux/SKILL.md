---
name: cmux
description: Orchestrate agents and terminals through the cmux CLI — create workspaces, boot fleets, send prompts to any pane, read results back, and coordinate the local Ollama workers of this repo. Use whenever the task involves cmux, booting an agent team/fleet, sending a prompt to another terminal/agent, reading another pane's output, or multi-agent orchestration across terminals.
---

# cmux orchestration

You control the cmux terminal app through its CLI (`cmux <command>`). Everything
reduces to one loop: **send → read → decide → repeat**.

## Mental model

`Window → Workspace → Pane → Surface (tab)`. A workspace is one agent team.
Address anything with short refs: `workspace:5`, `pane:7`, `surface:7`.
Commands print the refs they create: `OK surface:7 pane:7 workspace:5`.

Set `CMUX_QUIET=1` to silence alias notices.

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

## This repo's fleet pattern

Rule of the repo: **frontier coordinates, local models execute in parallel.**

### Delegation policy (decide WHEN to delegate — don't wait to be told)

Before starting any substantial task, classify it and delegate accordingly.
Consult `orchestration/router.yaml` (`use_for` per role) to pick workers.

DELEGATE (boot/reuse a fleet, dispatch, fleet-wait, then verify + synthesize):

- **Audit / review / "what am I missing?"** → ALWAYS ≥2 workers with distinct
  perspectives (e.g. codex + minimax, or reviewer + code_worker) before giving
  your own verdict. Independent agreement = confidence; disagreement = dig in.
- **Risky change about to close** (merge, deploy, config touching money/prod)
  → one independent frontier review is mandatory, not optional.
- **≥3 independent subtasks** → parallelize across workers instead of serial.
- **Broad search / classification / summarization at scale** → fan out to
  cheap local workers (triage, light_code); synthesize yourself.

DO IT YOURSELF (delegating is waste):

- Single sequential task, small verification against code, anything a test
  answers cheaply, conversational turns. (~95% of tasks — delegation is the
  exception that pays, not the default.)

Always: workers generate, YOU verify claims against source before adopting
them (small models mark DONE optimistically), and you own the synthesis.

Boot a team from the capability router. The default `small` preset creates a
Codex lead; Claude is an enabled fallback candidate (`first_available` walks
`lead.candidates` in order). Panes render by rank: CONTROL → RECON → BUILD → CHALLENGE → VERIFY.

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

`authority`, `tool_access`, and `resource_class` are validated declarations,
and the supported wrappers now enforce important parts of them: interactive
agents inherit an environment allowlist; advisory Codex runs read-only;
OpenCode challengers and Claude verification use plan/read-only modes; local
dispatch uses instance/heavy leases. Direct manual `cmux send` remains an
emergency bypass, so keep one writer and use `fleet-send.sh` normally.
The CONTROL lead alone uses `danger-full-access` so it can reach the cmux Unix
socket; its environment is still allowlisted.
Pass `--target-repo <path>` to `fleet-up` and each write-authority instance
gets a dedicated `fleet/<feature>/<instance>` branch and worktree at the target
repo's current `HEAD`. Existing branches fail closed; teardown removes only an
unchanged branch and preserves committed output. Local-worker token spend
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
  `minimax` (opencode -m minimax/MiniMax-M3, needs `MINIMAX_API_KEY`),
  `glm` (opencode -m zai/glm-5.2, needs `ZHIPU_API_KEY`).

Fleet Codex roles use the official hooks and authentication from `~/.codex`,
but the router pins `gpt-5.6-sol`; personal model defaults are not inherited.

Frontier panes are interactive agents: after sending a prompt, wait for their
hook event and then `read-screen`. `fleet-up` preflights executables, keys,
models, and lead health before creating the workspace; an unavailable member
fails closed instead of leaving a shell that can mistake a prompt for a command.

`fleet-up.sh` writes `orchestration/runs/fleet-<feature>.manifest`:

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
Leaving BUILD additionally requires `--approved-by <human>` — a person signs
off on the writer's diff before CHALLENGE/VERIFY see it.

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
`UserPromptSubmit` binds session/surface; completed Stop only wakes exact
`FLEET_RESULT:<run_id>:<STATUS>` verification. Missing or ambiguous evidence is
indeterminate, never success. The sentinel must be the final non-empty line and
the Stop must follow the single bound submit. A replay gap attempts catch-up
from the bounded cmux audit and retains the frontier lease if still
indeterminate; after confirming the agent is quiescent, release it with
`./scripts/fleet-abandon.sh <feature> <instance> <run_id> [reason]`. Partial
sends and unconfirmed race interrupts also retain their surface-UUID lease.
The dispatch wrappers also accept `--json`; orchestration callers must use that
canonical output rather than parse human-facing text. Audit recovery requires
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

For hotfix/needle-in-a-haystack tasks, race heterogeneous agents on the same
task; losers are interrupted automatically:

```bash
just race <name> "<task>" [instance=role ...]  # defaults come from router
./scripts/fleet-race.sh <name> "<task>" candidate_a=codex candidate_b=minimax
```

Prints the first successful candidate and its screen, then leaves the other
agents running by default. Failed, blocked, and abandoned candidates do not
stop `--any` while another candidate remains viable. Only use
`--cancel-losers` after a separate verification gate. The workspace remains
available for inspection and teardown.

### Decision queue (humans are slow — make blocking visible)

- Check `just status` at session start and before long waits: it lists every
  tracked agent session, surfacing ⚠️ needsInput ones with age. Agents blocked
  on a human decision have silently waited hours before — don't let them.
- When YOU stop to ask the human a business decision, first fire
  `cmux notify --title "DECISION: <tema en 5 palabras>" --body "<opciones>"`
  so the wait is visible outside your pane.

### Fleet etiquette

- One workspace per team/feature; never mix teams in one workspace.
- Communication is flat: any pane may `cmux send` to any other pane, but keep
  synthesis/decisions in the lead.
- On finishing a major task, emit `cmux notify --title "fleet-<feature>" --body
  "<result>"` so the human sees it without watching.
- Tear down with `just fleet-down <feature>` when work is merged/abandoned.
