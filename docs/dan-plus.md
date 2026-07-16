# Dan+: visible autonomy with proportional assurance

## Product thesis

Dan's core idea is right: agents need programmatic access, visible execution,
fast repeatable launch, model diversity, and an orchestrator that can scale
compute when the problem benefits. Dan+ adds one constraint: control should be
proportional to consequence. Routine repository-local work runs autonomously;
high-risk work selects the existing assured workflow instead of forcing that
ceremony onto every mission.

The primary product command is:

```bash
just dan <feature> "<complete objective>" --target-repo <repo>
```

It implements a three-level operating model:

```text
caller / top orchestrator
  └─ visible fleet lead (owns decomposition and synthesis)
       ├─ scout
       ├─ builder (only writer, isolated worktree)
       ├─ challenger
       └─ verifier
```

The roster is capability, not a mandatory pipeline. The lead may use one worker,
several in parallel, or none. Pre-booting visible panes makes capacity available;
it does not justify spending tokens on every pane.

## Research translated into design

- [Dan's CMUX demonstration](https://youtu.be/WAFUMBLOjHo) emphasizes agentic
  access, monitoring, instant sessions, heterogeneous races, and an
  orchestrator → leads → workers organization with lateral communication.
- [CMUX's official product documentation](https://cmux.com/) defines the useful
  substrate: notification rings, visible splits, a CLI/socket API, and native
  panes for agent teammates. Dan+ uses CMUX as the observable execution plane,
  not as the source of truth for completion.
- [Anthropic's “Building effective agents”](https://www.anthropic.com/engineering/building-effective-agents)
  distinguishes fixed workflows from agents that dynamically direct their own
  tools. It recommends simple composable patterns and adding complexity only
  when it improves measured outcomes. Therefore the Dan+ lead owns the task
  graph; FDP state machines are not its default path.
- [Anthropic Agent Teams](https://code.claude.com/docs/en/agent-teams) validates
  a lead plus separately addressable, directly inspectable teammates for research, review,
  competing hypotheses, and cross-layer work—and warns against multi-agent
  overhead for sequential tasks. Dan+ tells the lead to delegate selectively.
- [OpenAI Agents SDK orchestration guidance](https://openai.github.io/openai-agents-python/multi_agent/)
  supports mixing LLM-led manager/handoff patterns with code-led flows. Dan+
  uses an LLM-led manager for open-ended work and retains the code-led assured
  profile for predetermined high-risk verification.
- [Anthropic's Claude Code auto-mode report](https://www.anthropic.com/engineering/claude-code-auto-mode)
  describes approval fatigue and motivates risk-sensitive automation between
  manual approval for everything and unrestricted execution. Dan+ keeps one
  isolated writer and exact completion evidence, but removes routine phase
  approval.
- [Anthropic Managed Agents](https://www.anthropic.com/engineering/managed-agents)
  argues for stable session, harness, and sandbox interfaces while model
  assumptions evolve. This repo's manifests, run IDs, result files, and
  worktrees remain stable below both autonomous and assured modes.

## What was deliberately removed from the default path

- Manual `advance` before each specialist.
- Human approval for ordinary BUILD → review movement.
- A predetermined Maker → Checker → Challenger → Verifier sequence for every
  task.
- Screen scraping as completion evidence.
- Dispatching every available model merely because its pane exists.

The last two are reliability rules, not ceremony, so they remain: exact run IDs,
event-driven waiting, provider/model-bound results, one writer, and worktree
isolation.

## Collaboration model

Raw peer prompts are unsafe in this harness because a second terminal submission
can make the active run ambiguous. In a contract-v2 fleet, raw CMUX input also
cannot become a tracked result: CONTROL must authorize the exact submit event
before session binding. Dan+ uses result-driven collaboration:

1. separately tracked agents return a durable result;
2. the lead can route that exact result file to any peer in a later tracked run;
3. the recipient challenges, revises, or verifies it;
4. every adopted contribution retains its run identity.

This preserves lateral intellectual collaboration while improving Dan's raw
“any pane prompts any pane” mechanism with deterministic turn ownership. Native
provider teams can still be used inside a specialist when their own harness
provides safe teammate messaging.

## Human interruption policy

The lead proceeds without asking for routine implementation or verification
approval. It interrupts only for:

- external side effects or production changes;
- money, credentials, private data, or secret handling;
- destructive or hard-to-reverse actions;
- a genuinely missing business/product decision;
- exhaustion of safe in-scope recovery paths.

When those conditions are known in advance, use `fleet_dialogue` (`assured`)
instead of Dan+.

## Execution and control perimeter

`native` remains the default execution profile and keeps the established tool
surface. `sandboxed` and `regulated` narrow ephemeral runtime state without
rewriting intellectual capabilities from the compiled roster; `regulated`
also requires a durable Mission identity and routes effects through CONTROL.
These profiles are orthogonal to `autonomous`, `guided`, and `assured`, which
describe who owns decisions and gates rather than the process perimeter.

Mission-bound operations can cross the private Fleet Control Unix socket only
after kernel peer-UID validation plus Lead run identity or a ledger-bound
specialist capability token. This makes CMUX the visible execution plane while
Mission/Fleet ledgers remain the authority for dispatch, result, and lineage.

## Improvement loop

CMUX panes show behavior; manifests and ledgers prove identity; status/progress
and logs show the live mission. The repository's report/eval layer aggregates:

- time to first useful result and total wall time;
- worker selection and skipped workers;
- human interruptions by reason;
- provider/model success and indeterminate rates;
- cost/tokens per accepted result;
- verification failures caught before handoff.

These metrics improve prompts and routing. They are observational and do not
become gates in the mission path.
