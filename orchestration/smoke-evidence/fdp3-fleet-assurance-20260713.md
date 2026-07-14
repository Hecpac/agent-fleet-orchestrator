# FDP-3 fleet assurance smoke — 2026-07-13

## Verdict

`STATUS: FAIL`

The fresh live fleet proved the fail-closed boundary, but it could not reach the
FDP-3 CHALLENGE and VERIFY stages. The Codex Maker completed its isolated task
correctly. MiniMax then returned a semantically valid `ACCEPT` contract preceded
by visible `<think>...</think>` reasoning. OpenCode stored both pieces in the
same text part, so the strict FDP-2 parser correctly rejected the result as more
than one JSON document and terminalized the conversation as `indeterminate`.

This is a transport compatibility blocker in the FDP-2 Checker frontier, not a
successful FDP-3 smoke. No reasoning was stripped and no contract was repaired
or inferred.

## Smoke adaptation and scope

The generic daemon restart in `smoke-verify` was adapted to this repository's
real production surface: a fresh cmux workspace created by `fleet-up.sh`. The
incarnation loaded the uncommitted FDP-3 implementation from the current
working tree.

- Preset: `fleet_dialogue`
- Feature: `fdp3-smoke-verified-20260713`
- Workspace: `workspace:36`
- Workspace UUID: `9F29F972-2E88-440E-AB12-E53FA54C3E37`
- Base SHA: `8ca8b009b80566f3841a9cbe1271d9edef032bf6`
- Conversation: `1e8f5970-097b-4f02-b65e-bda2d79296f3`
- Roster: Codex/OpenAI Maker, MiniMax Checker, GLM Challenge, Claude Verify

`fleet-up.sh` exited zero with all five surfaces present. No traceback occurred.
The optional Codex Maker boot reported an existing Figma MCP login/startup
warning; it did not prevent the Maker run from completing.

## Live trace

1. CONTROL advanced to BUILD and started the frozen task-spec conversation.
2. Maker run `66306d1c-61f5-4bdd-b9f0-f021ffc2e21a` completed `succeeded` in
   `172.222 s` with frontier-sentinel evidence.
3. The Maker created exactly one commit,
   `f7dc38b99ebc1c6eee68eba3657763ca97746bfa`, whose only changed file was
   `docs/fdp3-live-verified.md`; its three lines matched the task exactly.
4. CONTROL accepted proposal message
   `72557bf4-c16c-4177-91a4-72a22b710d81` and bound it to the exact Maker run.
5. Checker run `16f436b4-6873-441c-8f63-a6637ee86619` completed `succeeded` at
   the process/frontier layer in `141.859 s`.
6. The strict Checker contract parser rejected its result and closed CONTROL
   as `indeterminate` with reason
   `invalid_checker_contract:FDP-2 result body is not one JSON document`.

Round-trip from conversation creation to terminal rejection: `356.361 s`.

## Explicit negative check — PASS

The malformed Checker envelope failed closed without content repair:

- Terminal status: `indeterminate`
- Control head:
  `be4355cdd12b020b895ceabcdbc127ef3baf7aa9fe2bc1e12da8e654ebf54c6f`
- Rejected result:
  `orchestration/runs/results/fdp3-smoke-verified-20260713/16f436b4-6873-441c-8f63-a6637ee86619.txt`
- Offline-verified archive:
  `orchestration/runs/archive/fdp3-smoke-verified-20260713-20260713T192625Z/`

This negative behavior is correct and preserves the zero-trust contract. It
does not compensate for the missing happy-path FDP-3 evidence.

## Root-cause evidence

The OpenCode database session `ses_0a3113c2dffeR1dSFMdrIymDbo` stores the
Checker's reasoning and final contract as `part.type=text` values. It exposes no
`reasoning` field or distinct final-answer part for this run. The last text part
starts with `<think>` and later contains the JSON contract; therefore selecting
all text parts reproduces the rejected envelope, while stripping tags would add
new semantic parsing at a trust boundary.

## Teardown and offline receipt

- `fleet-down.sh fdp3-smoke-verified-20260713` exited zero.
- `workspace:36` was absent immediately afterward; only the pre-existing idle
  workspace remained.
- The isolated Maker worktree was removed.
- The advanced Maker branch was preserved at
  `f7dc38b99ebc1c6eee68eba3657763ca97746bfa`.
- `verify --archive --require-terminal` returned 1 conversation, 4 control
  events, 1 dialogue message, 1 payload, and status `indeterminate` with the
  expected control head.

## Automated verification

- `python3 -m unittest discover -s tests`: 159 tests, `OK` in `46.114 s`.
- `python3 -m compileall -q scripts tests`: exit 0.
- `bash -n scripts/*.sh`: exit 0.
- `git diff --check`: exit 0.

Expected negative-test diagnostics about missing cmux, timeouts, and malformed
worker contracts appeared inside the unit suite; the suite itself exited zero.

## Open lanes

- Decide and implement a bounded FDP-2 transport policy for providers that
  embed `<think>` reasoning in ordinary text: configure reasoning off at the
  provider, require a structured final channel, or add an explicitly specified
  and tested envelope extractor. This requires a separate trust-boundary
  decision; prompt-only compliance was insufficient in this live run.
- Repeat the happy path after that blocker is closed and require FDP-2
  `accepted`, GLM CHALLENGE `PASS`, Claude VERIFY `VERIFIED`, and terminal FDP-3
  `verified` before changing this smoke verdict.
- Run the planned live negative FDP-3 contract test after reaching CHALLENGE;
  the present negative stopped one stage earlier in FDP-2.
- Resolve or explicitly waive the pre-existing optional Figma MCP boot warning.

## Remediation smoke attempt #2 — CLI variant boot blocker

`STATUS: FAIL`

The remediated fleet `fdp3-variant-smoke-0713` did not publish a manifest or
reach a model turn. Its dedicated Checker launch used the frozen command
`opencode -m minimax/MiniMax-M3 --agent fleet-reviewer --variant none`.
OpenCode 1.17.15 rejected `--variant` at the reusable TUI entrypoint, printed
the root help, and returned to the shell; `fleet-up.sh` correctly failed boot
because the Checker never reached a recognized input prompt. Automatic boot
cleanup removed the temporary workspace and unchanged writer branch.

The installed CLI exposes `--variant` only on `opencode run`. A live isolated
probe confirmed that `opencode run --interactive` cannot be an idle reusable
pane: it requires an initial message and exits after one answer. A second probe
started the reusable TUI without `-m` while an agent supplied
`model=minimax/MiniMax-M3` and `variant=none`. The final OpenCode database rows
for sessions `ses_0a2ef69b7ffeWNhzEm60o0Re61` and
`ses_0a2eef8dbffeUnxn05h7u3EJuS` recorded:

- `providerID=minimax`
- `modelID=MiniMax-M3`
- `variant=none`
- `tokens.reasoning=0`

Probe workspace `workspace:40` was identity-checked before closure and was
absent afterward; only the pre-existing idle workspace remained. This proves a
viable agent-declared mechanism, but adopting it changes the frozen transport
decision and therefore remains blocked pending explicit authorization.
