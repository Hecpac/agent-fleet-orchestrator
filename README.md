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

Current enforcement boundary: `authority`, `tool_access`, and resource classes
are validated declarations that the supported wrappers now enforce. Every
consumer (`fleet-send`, `fleet-dispatch`, `fleet-wait`, `fleet-race`,
`fleet-down`) validates durable UUIDs before acting; dispatch acquires
instance/heavy/slot leases so local concurrency limits hold at runtime;
interactive agents launch through an environment allowlist
(`run-interactive-agent.sh`), with per-authority Codex sandbox levels
(writer → `workspace-write`, non-writer → `read-only`, CONTROL alone →
`danger-full-access` to reach the cmux socket). What remains out of scope:
OS-level process sandboxing. Direct manual `cmux send` bypasses the wrappers,
so keep one writer and use the scripts. `fleet-race` returns the first
successful completion as a candidate; it is not an acceptance gate.

Write isolation: pass `--target-repo <path>` (or set `FLEET_TARGET_REPO`) to
`fleet-up` and every write-authority instance gets a dedicated
`fleet/<feature>/<instance>` branch and worktree, based on the target repo's
current `HEAD`. Existing branch names fail closed. `fleet-down` refuses
uncommitted changes, records the final SHA, preserves branches with commits,
and removes branches that never advanced beyond their base SHA.

Execution economics: local worker runs record prompt/completion token counts
in the per-feature ledger; `limits.local_token_budget_per_feature` in the
router makes `fleet-dispatch` warn at 70% observed spend and refuse new
dispatches at 100%. This is a soft dispatch-time cap, not a reservation, so
already-concurrent runs may overshoot it. `fleet-wait` fires a `cmux notify`
escalation (title `ESCALATION: ...`) naming the stuck instances when its
deadline expires.

Human-in-the-loop: advancing the durable phase gate out of BUILD requires
`--approved-by <human>` in addition to `--evidence`; the approver is recorded
in the state history.

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
`UserPromptSubmit` binds the exact session/surface and a completed `Stop` only
wakes verification of the run-specific `FLEET_RESULT` sentinel; Stop by itself
is never success. The sentinel must be the final non-empty line and the Stop
must follow the single bound submit. Replay is boot-scoped, and gaps attempt
bounded catch-up from cmux's audit before failing closed as `indeterminate`.
Catch-up requires the recorded baseline and continuous boot-scoped audit
sequence; truncated or corrupt audit evidence is rejected. Event ACKs are
schema-validated before readiness is published.
Local and frontier dispatch leases are owner-checked; frontier exclusivity is
enforced by surface UUID across manifest aliases. Fleet Codex workers use the
default `~/.codex` configuration so cmux's official hooks remain active, while
the router pins `gpt-5.6-sol` independently of the user's configured model.
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

The orchestration playbook (mental model, verbs, wait rules, memory budget)
lives in both `.agents/skills/cmux/SKILL.md` and `.claude/skills/cmux/SKILL.md`;
the two copies must stay synchronized.
