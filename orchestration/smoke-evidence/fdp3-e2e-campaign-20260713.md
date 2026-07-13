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
