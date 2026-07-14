# FDP-3 resumed smoke with agent-pinned checker — 2026-07-13

## Verdict

`STATUS: FAIL` for FDP-3 progression — with one decisive `PASS` inside it.

The agent-pin fix held completely: the MiniMax checker ran with
`variant=none`, `reasoning_tokens=0`, and **zero** `<think>` leakage — the
exact failure mode that killed the previous FDP-3 smoke is dead. But the
conversation terminalized `indeterminate` on a **new, distinct** failure mode:
MiniMax-M3, with its reasoning channel disabled, narrated its analysis as
visible prose *before* the (otherwise valid) `ACCEPT` JSON, violating the
strict first-character-`{` contract. Fail-closed behavior was correct end to
end: exact terminal reason, single-attempt policy honored, no parser
relaxation, clean teardown with durable archive.

## Recon closures folded into this attempt

Both UNKNOWNs from the pre-resume recon closed as **already covered** (no new
code was written for them):

- `fleet_state.py` CHALLENGE→VERIFY wiring: exercised through the real
  subprocess by `tests/test_fleet_assurance_controller.py::enter_verify`
  (success path, rc 0) and `test_phase_gate_requires_exact_control_head`
  (failure path, rc 3, active_phase retained).
- FDP-2 verifiers after FDP-3 publications (the deviation #1 regression):
  `test_verified_path_binds_context_gate_messages_receipt_and_offline_archive`
  calls `fdp2_controller.create_live_receipt` (line 245) and
  `fdp2_controller.verify_archive` (line 270) over a 4-message store
  (proposal, verdict, challenge, verification) and requires both to pass.

## Scope and baseline

- Feature: `fdp3-agentpin-live-20260713`, preset `fleet_dialogue`,
  `FLEET_NO_LEAD=1`, target repo: this repository.
- Working tree: committed agent-pin (`8971e86`, `b03609e`) + uncommitted FDP-3
  remainder (the incarnation under test).
- Conversation: `cef7d7b3-c7ea-4194-b594-343fed362f92`; frozen task spec
  (sha256 `07da8dc7…`) creating `docs/fdp3-agentpin-live.md` (3 exact lines).
- All four provider auths verified before boot (codex ChatGPT, claude.ai
  OAuth, MINIMAX_API_KEY, ZHIPU_API_KEY).

## Live trace

1. Boot: `fleet-up.sh` exited 0; workspace:42; `checker.variant=none` in the
   manifest; `advance CONTROL -> BUILD`.
2. `fdp2-start` → `awaiting_proposal_run`, maker prompt sha `10f7c4b8…`.
3. Maker run `f7a336fe-e154-49e2-8952-7ac44b93fa76` → `succeeded` (~3 min 20 s).
   One commit `32736c6` on `fleet/fdp3-agentpin-live-20260713/maker`, only
   `docs/fdp3-agentpin-live.md` changed.
4. Proposal published (`0456cc1f…`) and bound → `awaiting_checker_run`,
   checker prompt sha `2260842e…`.
5. Checker run `7183dac8-1268-4da8-8978-758a42ff3e2f` → `succeeded` at the
   frontier layer in ~40 s (vs 141 s in the pre-pin run — consistent with the
   disabled reasoning channel).
6. `fdp2-step-run` → **`indeterminate`**, terminal reason
   `invalid_checker_contract:FDP-2 result body is not one JSON document`.

## The decisive evidence pair

- **Old failure mode (dead):** result body contains zero `<think>` tags;
  OpenCode DB records `variant="none"`, `tokens.reasoning=0` for the final
  assistant message. `minimax_checker_requires_durable_none_variant` held live.
- **New failure mode (open):** result body =
  one prose paragraph ("I have enough evidence. The Maker head commit…")
  followed by a schema-valid `ACCEPT` JSON and the exact sentinel. The
  checker prompt contract explicitly forbids any visible analysis before the
  object; the strict parser rejected it without repair. The JSON itself was
  correct — the violation is purely the visible preamble.

## Teardown and integrity

- `just fleet-down` exited 0, closed workspace:42; no active manifests remain.
- Maker work survives teardown on its durable branch (`32736c6`).
- Archive: `orchestration/runs/archive/fdp3-agentpin-live-20260713-20260713T205802Z/`
  - ledger: `dcb64d274f21767054480e01b828eebb6f72fc56afc12080612a9279e22f6164`
  - manifest: `eebe86ebf0364bd334cc86fad5b36181fdde1492724549d447f742bb916d45dc`
  - state: `3cab9b46a1fe00dc667dced32cdfb196e7577f676b85c599491b95128514d3a4`

## Open decision (returned to the human — not taken here)

A third FDP-2 attempt with unchanged runtime would re-test an already
falsified strategy (MiniMax narrates ~unpredictably without a reasoning
channel). The fork, in rough order of blast radius:

- **A.** Harden the output contract in the checker prompt template
  (`fleet_dialogue_controller.py` prompt composition) — runtime change,
  needs a frozen decision.
- **B.** Harden the `minimax-checker` agent instructions (config change to
  the committed agent file).
- **C.** Keep runtime frozen; treat contract violations as expected
  occasional `indeterminate` terminals and retry as a fresh conversation.
- **D.** Change the checker model/provider — widest change.

## Open lanes

- FDP-3 CHALLENGE/VERIFY stages (GLM + Claude) remain unexercised live; the
  ownership split still has only synthetic coverage.
- The prior run's leftover branch/worktree
  (`fleet/fdp3-smoke-verified-20260713/maker`) and this run's maker branch
  await a cleanup decision.
