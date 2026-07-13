# MiniMax checker hardened-contract smoke (Decision B) — 2026-07-13

## Verdict

`STATUS: PASS` for the Decision B objective — the hardened `minimax-checker`
agent now honors the strict output contract live. The full FDP-2→FDP-3 lanes
remain open, blocked by two NEW environment/build findings outside the frozen
runtime (below), both returned to the human.

## Authorization

DETÉN #6 was resolved by Hector as **Option B**: harden the dedicated agent's
instructions; keep the main runtime (controller prompt templates) intact.
Rationale recorded in the conversation: blast-radius containment (reject A),
budget/controllability (reject C), cognitive diversity of the fleet (reject D).

## Change under test

`.opencode/agents/minimax-checker.md` body hardened: a NON-NEGOTIABLE output
contract section (entire visible reply = exactly one JSON object + sentinel;
first visible character `{`; explicit FORBIDDEN list for preambles, prose,
fences, narration; analysis only inside schema fields; token discipline).
Frontmatter identity untouched (`model: minimax/MiniMax-M3`, `variant: none`).
Lock added in `tests/test_spec_coherence.py` (five hardened-contract
substrings asserted on the agent file). Suite: 162/162 OK.

## The decisive turn

Fleet `fdp3-agentpin-b2-20260713` (fresh boot, `checker.variant=none` in
manifest). Direct checker turn with a contract identical in shape to CONTROL
checker turns AND an evidence-gathering step (reading README.md + running
`git rev-parse HEAD` — the same narration trigger that produced the prose
preamble pre-hardening):

- Run `0cccca36-5a18-4a02-a26e-fe9c73c179f5` → `succeeded`.
- Result body, verbatim: one minified JSON object
  (`verdict: ACCEPT`, correct heading `# Agent Fleet Orchestrator`, correct
  HEAD `b03609e…`) + exact sentinel. **First visible character `{`. Zero
  prose anywhere.**
- Session evidence from OpenCode DB: `variant="none"`, `tokens.reasoning=0` —
  identity intact under the hardened instructions.

Pre-hardening baseline for contrast (same day, `fdp3-agentpin-resume`
evidence): same class of turn produced a prose paragraph before the JSON and
terminalized `indeterminate`.

## New findings (returned to the human — frozen runtime untouched)

1. **Maker codex sandbox denies worktree commits (deterministic).** Three
   consecutive maker runs terminalized `blocked` across two fresh boots
   (`253ecea7`, `336d19c4` on fleet b; `29c58056` on fleet b2). Verbatim codex
   reason: `Git could not create /Users/hector/Projects/agent-fleet-orchestrator/.git/worktrees/fdp3-agentpin-b2-20260713-maker/index.lock: Operation not permitted.`
   The maker files were created correctly; only the commit is denied (the
   linked worktree's admin dir lives outside the sandbox's writable roots).
   Contrast: attempt A (`f7a336fe`, 20:52Z) committed fine with the identical
   `codex --model gpt-5.6-sol` command — the differential root cause is
   UNKNOWN. Fail-closed behavior was correct every time (self-declared
   BLOCKED sentinel, no metadata rewriting). Candidate fixes all cross the
   frozen boundary: router codex flags, codex config, or worktree layout.
2. **`fleet-down.sh` receipt edge (FDP-3 uncommitted code):** a conversation
   abandoned before any publication leaves `dialogue-control.jsonl` without
   `dialogue.jsonl`; teardown fails
   (`verification receipt source is not a regular file`, exit 2) and the
   workspace survives. Reproduced twice (fleets b and b2). Needs a fix in the
   FDP-3 build (receipt over existing files only, still fail-closed).

## State left behind

- Fleets `fdp3-agentpin-b-20260713` (workspace:43) and
  `fdp3-agentpin-b2-20260713` (workspace:44) are alive-and-idle: conversations
  abandoned, no bound runs, no result deliverables pending; teardown blocked
  only by finding 2. Left up deliberately rather than bypassing the wrapper.
- Maker branches `fleet/fdp3-agentpin-b-20260713/maker` and
  `…-b2…/maker` exist with no commits beyond base (nothing to preserve).
- Prior cleanup executed as authorized: branches
  `fleet/fdp3-smoke-verified-20260713/maker` (was `f7dc38b`) and
  `fleet/fdp3-agentpin-live-20260713/maker` (was `32736c6`) deleted; shas
  remain recorded in their evidence docs.

## Open lanes

- Full FDP-2 accept with the hardened checker (blocked by finding 1 — the
  proposal stage needs a maker commit).
- FDP-3 CHALLENGE/VERIFY live (GLM + Claude) — unchanged.
- Teardown of fleets b/b2 after the finding-2 fix.
