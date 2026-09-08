# Herdr migration closure — 2026-09-06

## Verdict

The real-provider synthetic Mission completed with `status=succeeded`, artifact
acceptance `accepted`, a valid anchored archive, and `cleanup_pending=false`.
The broader environment-preservation gate is **FAIL**, solely because the global
Codex configuration hash differs from its recorded baseline. Do not describe this
as an unconditional migration PASS or attribute that change without evidence.

## Recorded and reproduced evidence

- Focused local regression log: **195 tests passed**, router schema 3 valid.
  This log was recovered during closure; that suite was not rerun then.
- Five original functional tests were independently rerun against the candidate:
  **5/5 passed**, including empty/invalid input and input preservation.
- Original test bytes and the clean synthetic source checkout/HEAD are unchanged.
- Five admissions, five submissions, four distinct agent sessions, one writer.
- Archived turn contexts verify Astra Lead and three Sol roles, all `high`;
  Worker has `workspace-write`, Lead/Reviewer/Verifier have `read-only`.
- The exact JSON acceptance predicate passes against the archived frozen tree.
- Poison CMUX executable recorded **zero invocations**.
- Only the Mission-owned temporary workspace was closed. The original four
  agents retain their sessions and panes; the original grid is present and focused.
- Repository `git diff --check` passes. Migration source HEAD remains
  `abd7153ee364bb95f7510c523e13aed536068d8e`; no commit or push was made.
- Comparison with the initial dirty-file backup finds changes only in the six
  intentionally overlapping migration files: `justfile`, `orchestration/router.yaml`,
  `scripts/mission-run.py`, and the Mission/router/workflow tests. Other backed-up
  pre-existing edits are byte-identical.

## Live attempts

1. `02fd8c3f-5ab9-54c1-a5a8-81960c04720c` (`herdr-stats`): **BLOCKED**.
   Lead incorrectly treated future stages as its own missing prerequisites.
   Controller preserved the negative verdict, did not dispatch Build, and closed
   its owned workspace. Stale terminal cancellation sent no signal.
2. `8df9de85-8ee7-5629-ad1f-91de06268184` (`herdr-stats-r2`): **ACCEPTED**.
   One sequential retry after a stage-only success-criteria correction. Existing
   Build was recovered without resubmission, then Review, Verify and Synthesis
   completed through the canonical `resume` path.

Final frozen Git tree: `585a12534be986258d896d94de96daaf99d77fdf`.
Archive index SHA-256:
`9510ab27f9d0b572dd436a3f4eeaf4622db61e8e847392eb0e21e7c66b3b619a`.

## Environmental exception

Global configuration `/Users/hector/.codex-cli/config.toml`:

- Baseline SHA-256: `997c958fe8778dabdef1d2d34e2c5d3887e0db687d44dae92ca84e5f1e84dae6`.
- Closure SHA-256: `08736c2a546224ba7ccc6eb1d89a8970d4e2d969257d8a248866f4c863b577e2`.
- Observed modification time: September 6, 2026, 20:15:33 CDT, before this closure
  resumed. Author and changed fields were not established; no restoration was made.
- The post-Mission helper reports this mismatch as `FAIL` and exits 1 while
  completing the independent remaining assertions. It does not weaken acceptance
  or overwrite the Mission's distinct semantic verdict.

## Evidence locations and limits

Local evidence root: `/private/tmp/agent-fleet-herdr-live.d79gT2`.
Files: `final-local-gate.log`, `blocked-status.json`, `terminal-cancel.log`,
`closure-evidence.json`, `verify_live.py`, and the two Mission directories under
`runs/missions/`. The accepted Mission retains `herdr-archive/archive-index.json`,
role results, transcript segments, original artifacts, final tree and portable patch.
Temporary paths are not durable off-host custody; this document does not replace
those artifacts. No deletion of the evidence was performed.

Scope is one synthetic local real-provider workflow and targeted regression tests,
not exhaustive provider coverage, production certification, hostile same-UID
isolation, external custody, or measured cost/latency improvements. High-risk lanes,
dirty-source snapshot support and enforceable positive token budgets remain outside
this completed test. Engineering work is paused after closure; a separately
requested consultation may assess risks and prioritize the roadmap without edits.
