# FDP-2 fleet dialogue smoke — 2026-07-13

## Verdict

`STATUS: PASS`

The bounded Maker–Checker dialogue completed end to end through the real cmux
fleet surface. Codex produced one clean commit, MiniMax independently returned
strict `ACCEPT`, CONTROL published and bound both FDP-1 messages, the human
BUILD gate accepted the exact clean HEAD, teardown removed the workspace, and
the archived receipt verified offline.

## Smoke adaptation and scope

The generic daemon restart in `smoke-verify` was intentionally adapted. This
repository has no web daemon or `restart.sh`; its production surface is a fresh
cmux workspace created by `fleet-up.sh`. The tested incarnation loaded the
uncommitted implementation directly from the current working tree.

- Preset: `fleet_dialogue`
- Feature: `fdp2-live-accepted-final`
- Workspace: `workspace:35`
- Target base: `04d317d3d9676af6ab3edc070d3514b96bcc21eb`
- Conversation: `12ec6ed0-23e1-417d-99df-958079ee2068`
- Frozen task spec: SHA-256
  `698c787e6e9c3cd99ab88744ad49745cf384e6cccd8dbda11087bd844d9feecb`
- Roster: Codex/OpenAI Maker, MiniMax Checker, GLM Challenge, Claude Verify

`fleet-up.sh` exited zero with all five surfaces present. The final lifecycle
ledger contains succeeded terminals for the exact Maker and Checker runs; the
CLI emitted no traceback or boot error. There is no daemon stderr stream in
this architecture, so the comparable evidence is the run-bound lifecycle
ledger plus the post-teardown offline receipt.

## End-to-end trace

1. CONTROL advanced to BUILD with base-SHA evidence and started the strict
   `--spec-file` conversation.
2. Maker run `6abd2f40-687c-4aa0-b58b-b8361c0c1e9f` completed `succeeded` with
   result-file evidence and exactly one new clean commit:
   `63343137ec347ed99369ecd2b9394f6a3661dd24`.
3. Proposal message `a1493dbd-828e-4899-9039-7c2ba18b3229` was copied into the
   content-addressed FDP-1 store and bound to that run.
4. Checker run `eec925bd-6a69-46a4-a174-99ff4ae4a9d3` completed `succeeded`.
   Its strict contract was `ACCEPT` with no findings.
5. Challenge message `0b8a8aaa-8a5a-4744-8830-d8881e3dd103` replied to the
   exact proposal and was bound by CONTROL.
6. CONTROL closed `accepted` with head
   `94268556d51648e928b1af0e4af83a477a617fd90cb16b5666de0c157b6b1997`.
7. The human phase gate recorded `approved_by=hector` and advanced BUILD to
   CHALLENGE only after revalidating the exact accepted clean HEAD.

Round-trip from conversation start to terminal acceptance: `218.039453 s`.

## Explicit no-regression checks

### Invalid Checker JSON fails closed

The independent live feature `fdp2-live-green` deliberately produced Checker
prose around otherwise valid JSON. CONTROL did not repair or infer it:

- Terminal: `indeterminate`
- Reason: `invalid_checker_contract:FDP-2 result body is not one JSON document`
- Control head:
  `c6cb6d67f721de8d34636e2a08b37e47fae1a6e16fd365697d21e36e0c01a6f7`
- Offline-verified archive:
  `orchestration/runs/archive/fdp2-live-green-20260713T174303Z/`

### Multiline OpenCode prompt no longer fragments

The first long Checker prompt exposed three physical `UserPromptSubmit` events
and correctly terminalized `frontier_session_binding_ambiguous`. After encoding
the exact logical prompt as one JSON string, the accepted run emitted exactly
one pair for its session:

- sequence `7155`: `received`, input length `1035`
- sequence `7158`: `completed`, input length `1035`
- no second `received` before terminal completion sequence `7257`

This proves the fixed transport condition directly, not merely through the
happy-path acceptance.

## Teardown and offline receipt

- Maker worktree was clean at the accepted SHA before teardown.
- `fleet-down.sh fdp2-live-accepted-final` exited zero.
- `workspace:35` was absent immediately afterward.
- Archive:
  `orchestration/runs/archive/fdp2-live-accepted-final-20260713T175923Z/`
- `verify --archive` result: 1 conversation, 5 control events, 2 dialogue
  messages, 2 payloads, status `accepted`.
- Receipt control head:
  `94268556d51648e928b1af0e4af83a477a617fd90cb16b5666de0c157b6b1997`.

## Automated verification

- `python3 -m unittest discover -s tests`: 150 tests, `OK` in 41.929 seconds.
- `python3 -m compileall -q scripts tests`: exit 0.
- `bash -n scripts/*.sh`: exit 0.
- `git diff --check`: exit 0.

Expected negative-test diagnostics about missing cmux, timeouts, and malformed
worker contracts appeared inside the unit suite; the suite itself exited zero.

## Open lanes

- A full live `REVISE` → Maker rebuttal/revision → second Checker cycle was not
  completed. Close it with a task whose first proposal intentionally leaves one
  noncritical finding, then verify the two FDP-1 revision messages.
- `REJECT` and the three-round exhaustion path were covered by deterministic
  tests, not by spending live model runs.
- The 30-minute run timeout and four-hour absolute deadline were advanced with
  injected clocks in tests; no wall-clock soak was performed.
- GLM CHALLENGE and Claude VERIFY were booted and identity-checked but not
  dispatched, because FDP-2 changes only the BUILD dialogue and the frozen spec
  leaves those later roles unchanged.
- Crash/restart recovery and archive tampering are unit-tested but were not
  induced against this accepted live workspace.
