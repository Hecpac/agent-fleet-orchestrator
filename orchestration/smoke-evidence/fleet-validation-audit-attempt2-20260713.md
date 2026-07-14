# Fleet validation audit smoke — attempt 2 — 2026-07-13

## Verdict

`STATUS: FAIL`

The second independent `audit` attempt preserved the approved fail-closed
contract. Both frontier auditors succeeded, but the local verifier again
exhausted its generation limit without producing a status body. The fleet was
not retried, the runtime was not changed, and the failed attempt was torn down
cleanly with its evidence preserved.

## Frozen difference from attempt 1

The only authorized change was the VERIFY evidence pack:

- prompt tokens fell from `1728` to `631` (about 63% smaller);
- the pack contained four decisive source facts and two compact worker
  summaries;
- it contained no nested status contracts or full worker reports;
- model remained `code-auditor:latest`;
- `num_predict` remained `768`;
- scripts, router configuration, retry policy, and acceptance criteria remained
  unchanged.

## Scope and baseline

- Feature: `fleet-template-audit-20260713-a2`
- Preset: `audit`
- Branch: `main`
- Base/final HEAD: `9f9df28da2b387feba96f2c69991f15b2f8aa25d`
- Expected result instances: `analysis`, `challenge`, `verify`
- CONTROL lead excluded from worker-result acceptance.
- Audited sources and stable hashes:
  - `orchestration/router.yaml`:
    `bd2797d55532716b10b2703218663fdb464884a1ed84269547014feb799d9df3`
  - `README.md`:
    `0c0ccf0a52cd3b468976adcd447c1d589d8118d0bf16a5e574d7eb06e59650d1`
  - `.agents/skills/cmux/SKILL.md`:
    `1f9808e0732f79bc9da59db85e0627b8a63566eb24e880b914108cec665d646c`

As in attempt 1, daemon restart was not applicable. A fresh cmux fleet was the
real surface under test, and the supported wrappers exercised boot, identity,
phase gates, exact-run waiting, local leases, terminalization, and teardown.

## Phase trace

### RECON — `analysis`

- Run: `analysis=1cc408ae-0af5-4f0d-8e3a-7d9135f49c4d`
- Terminal: `succeeded`, exit `0`
- Reason: `frontier_sentinel_verified`
- Dispatch: `2026-07-13T14:33:47.272164+00:00`
- Completion: `2026-07-13T14:36:24.609Z`
- Round-trip: about 157 seconds

The auditor found two source-backed contradictions: the skill directs routing
through a nonexistent `use_for` field while the router exposes capabilities,
and the skill describes race losers as both interrupted automatically and left
running by default. It classified roster, lifecycle, state, lease, and teardown
gaps separately as omissions or UNKNOWNs.

### CHALLENGE — `challenge`

- Run: `challenge=b317d315-97b2-4df8-ac3b-cbe373e32bdc`
- Terminal: `succeeded`, exit `0`
- Reason: `frontier_sentinel_verified`
- Dispatch: `2026-07-13T14:36:44.136514+00:00`
- Completion: `2026-07-13T14:37:52.221Z`
- Round-trip: about 68 seconds

The independent challenger reported several additional “contradictions,” but
some were differences of detail or vocabulary rather than mutually exclusive
rules. The disagreement was retained for the prompt-only verifier instead of
being resolved by worker count.

### VERIFY — `verify`

- Run: `verify=4222b770-ad00-4fe4-a5f1-472aeb6f84d5`
- Terminal: `failed`, exit `1`
- Dispatch: `2026-07-13T14:38:35.468929+00:00`
- Completion: `2026-07-13T14:39:04.702682+00:00`
- Round-trip: about 29 seconds
- Prompt tokens: `631`
- Completion tokens: `768`
- Result size: one newline; no status body
- Result file:
  `orchestration/runs/results/fleet-template-audit-20260713-a2/4222b770-ad00-4fe4-a5f1-472aeb6f84d5.txt`

The wrapper correctly rejected the empty response:

```text
Worker contract error: malformed or ambiguous status contract
```

Because both attempts ended at exactly `eval_count=768` with an empty final
body, the smaller prompt did not close the failure. A third attempt without a
runtime/model/budget decision would repeat an already falsified strategy.

## Teardown and integrity

- No feature, local-slot, reviewer, or heavy lease remained before teardown.
- Live workspace UUID matched the manifest:
  `48AC5E45-34C6-4092-A274-6BE67B4B8705`.
- `just fleet-down fleet-template-audit-20260713-a2` exited `0` and closed
  `workspace:30`.
- The active manifest disappeared; no feature process or workspace remained.
- Durable archive:
  `orchestration/runs/archive/fleet-template-audit-20260713-a2-20260713T143927Z/`
- Archive hashes:
  - ledger: `4fea7c5149b6cdb79d2e9c95270f8835036a787b968d401f0db96a0d277db8e1`
  - manifest: `b4db2adee139a87ee5e08cb8230a0b26bd98137fdabd8fb7b84f3b9422084ff7`
  - state: `59acac7025b93026ea4b2f7ee59d5cb5c260c39ed85791d156a9dd6d4c13c821`
- Total boot-to-archive time: about 6 minutes 10 seconds.
- Git branch, HEAD, implementation diff, and all audited-source hashes matched
  the preflight baseline.

## No-regression and open lanes

The attempt reproduced the exact failure from attempt 1 under a materially
smaller prompt and confirmed that fail-closed terminalization, lease release,
and teardown remain correct.

Open lanes:

- Choosing a larger `num_predict`, another reviewer model, or a response-mode
  change requires a separately frozen runtime decision.
- No third attempt is justified with the current parameters.
- The other five presets, writer isolation, Claude/GLM paths, races, timeouts,
  explicit abandon, and recovery-after-absence remain untested.
