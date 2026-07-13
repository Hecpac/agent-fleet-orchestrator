# FDP-2→FDP-3 end-to-end campaign — 2026-07-13

## Verdict

`STATUS: PARTIAL PASS` — every mitigation Hector authorized is implemented,
test-locked (163/163), and all but one are live-proven. The single remaining
unproven lane is the hardened Claude VERIFY leg. Fail-closed behavior held at
every layer in every failure. Fleets: b, b2, e2e, e2e2, e2e4 torn down clean;
e2e3 deliberately left up (poisoned store, finding 3).

## Live-proven (with runs)

1. **Maker sandbox fix (#7)** — `fleet-up` appends
   `sandbox_workspace_write.writable_roots=[<target .git>]` for openai
   instances with worktrees (commit `7cb8b48`). Probed with `codex sandbox`
   (index.lock EPERM → objects EPERM → full .git OK), then four live maker
   commits: `f7a336fe`(e2e), `8bb4639b`/`d43653f4`(e2e3), `250c6934`(e2e4).
   Root cause: the hardening guardrail boots openai instances
   `--sandbox workspace-write --ask-for-approval never`
   (`router_config.py:636`), and a linked worktree's admin dir/objects live
   outside it. (The earlier attempt-A success remains unexplained — UNKNOWN.)
2. **Teardown receipt fix (#8)** — commit `b059555`; live-verified by tearing
   down the two previously stranded fleets (b, b2), which then correctly
   stopped at the writer-reconciliation gate until the reproducible untracked
   files were discarded.
3. **Hardened MiniMax checker (Decision B)** — three real controller-composed
   checker turns parsed clean and closed `accepted / checker_accept`
   (conversations `5e99b73c` e2e, `3c56e162` e2e2, `a9893d0c` e2e3).
4. **Hardened GLM challenger** — commit `b53be2d`. e2e2 run `e3e8a15a`: pure
   JSON challenge, parser accepted, CHALLENGE→VERIFY phase gate crossed with
   the exact control head. Pre-hardening baseline (e2e run `df6887bb`):
   checklist prose → `invalid_challenge_contract`.
5. **FDP-3 chain to VERIFY dispatch** — e2e2 reached the Claude verify run
   (`0597c8a8`, succeeded at frontier layer) — the deepest live FDP-3
   penetration so far.

## Not yet proven

- **Hardened Claude verifier** (`--append-system-prompt` contract, commit
  `b53be2d`): added AFTER e2e2's Claude prefixed "El advisor no está
  disponible; procedo…" to a valid `VERIFIED` JSON →
  `invalid_claude_contract`-class indeterminate. No live turn has run with
  the hardened command yet.
- Full `verified` terminal + `fdp3-verify` receipt + teardown of a fully
  verified fleet.

## New findings (returned to the human)

1. **Codex double-submit race** — e2e3 run `64cc6743`: two
   `UserPromptSubmit` events in one run window →
   `frontier_session_binding_ambiguous` (fail-closed). Same class as the
   known double-enter quirk; transport-level robustness is an open design
   lane.
2. **OpenCode degraded-pane loop** — e2e4 checker pane: after one
   sentinel-glue failure (`frontier_sentinel_missing`: valid JSON but
   sentinel concatenated to `}` without newline, a 3.6-second lazy ACCEPT),
   every subsequent dispatch to the same pane double-submitted →
   `frontier_session_binding_ambiguous` ×2, deterministic. Recovery likely
   requires relaunching the pane process; not attempted (budget).
3. **Orphan-publish poisons a feature irreversibly** —
   `fleet_dialogue.publish` accepts a message no controller expected (my
   zombie script published proposal for orphan run `8bb4639b`). The
   append-only store then fails `_verify_message_bindings` forever: the
   BUILD→CHALLENGE gate AND `fleet-down`'s receipt both refuse. Fleet
   `fdp3-e2e3-20260713` (workspace:50) is left up as the live exhibit.
   Decision needed: gate `publish` on the controller's `expected`, and/or a
   quarantine path for poisoned stores.
4. **Operational lesson** — both e2e3 losses were caused by chained
   automation scripts silencing intermediate failures (a silenced `step` of
   an indeterminate run terminalized the conversation; a cascading zombie
   chunk published the orphan). Single-step verbs with explicit checks made
   e2e4's recovery lanes work.

## State left behind

- Zombie: `fdp3-e2e3-20260713` (workspace:50) — finding 3's exhibit; its
  maker branch `fleet/fdp3-e2e3-20260713/maker` retains commits (worktree
  mounted).
- Cleaned per standing policy (shas recorded): `fleet/fdp3-e2e-20260713/maker`
  (`777f4b4`), `…e2e2…` (`550336a`), `…e2e4…` (`99d11e0`).
- Archives for b, b2, e2e, e2e2, e2e4 under `orchestration/runs/archive/`.

## Next named recon (before another e2e attempt)

Confirm in `scripts/fleet-send.sh` / `fleet_frontier.py` how the opencode and
codex transports submit (single physical submission rule) and what produces a
second `UserPromptSubmit` on a reused pane — then decide the transport
hardening slice. Only after that, rerun the full flow to prove the hardened
Claude leg and the `verified` terminal.

## Addendum — transport root cause nailed (post-campaign recon)

`~/.cmuxterm/events.jsonl` + OpenCode DB for the degraded e2e4 checker pane:

- Turn 1: ONE UserPromptSubmit, but the stored user message is **1341 bytes
  of a ~3.9 KB prompt** — the pane submitted after the first paste chunk;
  the model judged with a third of the contract (hence the 3.6 s lazy
  ACCEPT and glued sentinel).
- Turns 2–3: THREE UserPromptSubmit each (~0.7 s apart), and the DB shows
  the prompt split mid-word into 3 user messages (2058 + 984 + 851 bytes:
  `…m`/`aker``, `…AC`/`CEPT``) — each chunk auto-submitted →
  `frontier_session_binding_ambiguous`.

Root cause: `fleet-send.sh` delivers multi-KB multi-line prompts via
`cmux send` paste; under terminal latency the chunked write escapes the
bracketed paste and newlines submit each chunk. This also retro-explains the
codex `64cc6743` double-submit. Consequence class is worse than flake: a
silently TRUNCATED prompt can still produce a well-formed, sentinel-correct,
fail-open-looking verdict.

Fix fork (human decision):
- **A. Pointer-send**: dispatch a tiny constant-size instruction referencing
  the durable, already-hashed `prompt_file`; eliminates chunking
  deterministically; the pane reads the file (read tools are allowlisted).
- **B. Post-send verification**: after send, require exactly one
  UserPromptSubmit (and/or verify stored prompt length) before accepting the
  dispatch; detect-and-retry instead of prevent.
- **C. Both** (prevention + detection).

## Frozen Decision C — transport slice spec (authorized 2026-07-13, build pending)

Hector froze option C (pointer-send + post-send verification). Spec for the
next build session:

1. `fleet_frontier.py prepare`: persist the composed prompt (the exact
   output of `prompt_with_contract`) to `runs_dir/prompts/<feature>/<run_id>.txt`
   and return `prompt_path` alongside `prompt`.
2. `fleet-send.sh`: dispatch ONE single-line pointer with no newlines and no
   backslash sequences (`cmux send` interprets literal \n/\r as Enter — a
   second candidate mechanism for the multi-submit, alongside paste
   chunking; the pointer is immune to both):
   `FLEET_RUN <run_id>: open <prompt_path> and execute its entire content as
   your exact task for this turn, including its completion protocol.`
3. Post-send verification (new frontier verb `confirm-submit`): poll
   `audit_events()` until >=1 `agent.hook.UserPromptSubmit` (phase received,
   matching hook_source + workspace_uuid, occurred_at >= dispatch time);
   on timeout the existing `frontier_send_transfer_unconfirmed` cleanup path
   marks the run indeterminate. Multi-submit remains guarded by the existing
   `frontier_session_binding_ambiguous` completion check.
4. Tests: `test_fleet_up` send-path fixtures must seed a synthetic
   UserPromptSubmit into `CMUX_EVENTS_LOG`; add a prepare test asserting
   `prompt_path` exists, is durable, and matches the returned `prompt`
   byte-for-byte; spec-lock the pointer shape (single line, no backslashes).
5. Then rerun the e2e flow to prove the hardened Claude verifier and the
   `verified` terminal + `fdp3-verify` receipt + clean teardown.
