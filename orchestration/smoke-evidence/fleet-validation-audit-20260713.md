# Fleet validation audit smoke — 2026-07-13

## Verdict

`STATUS: FAIL`

The approved fail-closed criterion was exercised successfully: the overall
smoke failed because the local `verify` instance did not produce a valid status
contract. The two frontier auditors succeeded, source integrity held, leases
were released, and teardown completed cleanly. No retry was used to turn a
partial result into PASS.

## Scope

- Feature: `fleet-template-audit-20260713`
- Preset: `audit`
- Target repository: `/Users/hector/Projects/agent-fleet-orchestrator`
- Base/final branch: `main`
- Base/final HEAD: `9f9df28da2b387feba96f2c69991f15b2f8aa25d`
- Read-only sources:
  - `orchestration/router.yaml`
  - `README.md`
  - `.agents/skills/cmux/SKILL.md`
- Expected result instances: `analysis`, `challenge`, `verify`
- CONTROL lead: lifecycle coordinator, excluded from worker-result acceptance.

The generic daemon restart in `smoke-verify` was intentionally adapted: this
change adds an orchestration prompt and a static contract test, not code loaded
by a service daemon. The real surface under test was a fresh cmux fleet started
through `fleet-up`, with identity, phase gates, exact waits, leases, and teardown
exercised end to end.

## Preflight and source baseline

- `cmux ping`: `PONG`
- The audit plan resolved before mutation to:
  - `analysis`: Codex, interactive, RECON, advisory/read-only,
    OpenAI `gpt-5.6-sol`
  - `challenge`: OpenCode, interactive, CHALLENGE, advisory/read-only,
    MiniMax `MiniMax-M3`
  - `verify`: local, VERIFY, prompt-only, Ollama `code-auditor:latest`
- No active manifest existed for the feature.
- Source hashes before dispatch and after teardown were identical:
  - router: `bd2797d55532716b10b2703218663fdb464884a1ed84269547014feb799d9df3`
  - README: `0c0ccf0a52cd3b468976adcd447c1d589d8118d0bf16a5e574d7eb06e59650d1`
  - cmux skill: `1f9808e0732f79bc9da59db85e0627b8a63566eb24e880b914108cec665d646c`

## Phase trace and exact runs

### RECON — `analysis`

- Run: `analysis=1ca1b1ba-978b-4a2a-828a-eaf8b42005ef`
- Terminal: `succeeded`, exit `0`
- Reason: `frontier_sentinel_verified`
- Dispatch: `2026-07-13T14:15:23.031660+00:00`
- Completion: `2026-07-13T14:18:45.171Z`
- Round-trip: about 202 seconds

The auditor reported one literal contradiction in the cmux skill: race losers
are described as “interrupted automatically” and, seven lines later, as left
running by default unless `--cancel-losers` is used. It also classified the
missing audit-without-BUILD explanation as UNKNOWN within the authorized three
files rather than inventing runtime behavior.

### CHALLENGE — `challenge`

- Run: `challenge=a5443d37-cbce-4932-a478-7d0a421e1d05`
- Terminal: `succeeded`, exit `0`
- Reason: `frontier_sentinel_verified`
- Dispatch: `2026-07-13T14:19:29.309291+00:00`
- Completion: `2026-07-13T14:21:19.059Z`
- Round-trip: about 110 seconds

The independent challenger reported zero contradictions and several prose
omissions. In particular, it missed the mutually exclusive race-loser
statements. This disagreement was preserved for VERIFY instead of being
collapsed by majority or by the orchestrator.

### VERIFY — `verify`

- Run: `verify=1fb4621e-7bf7-4d45-8f8b-7f682abff1ac`
- Terminal: `failed`, exit `1`
- Dispatch: `2026-07-13T14:22:26.948559+00:00`
- Completion: `2026-07-13T14:23:02.932097+00:00`
- Round-trip: about 36 seconds
- Prompt tokens: `1728`
- Completion tokens: `768`
- Result file:
  `orchestration/runs/results/fleet-template-audit-20260713/1fb4621e-7bf7-4d45-8f8b-7f682abff1ac.txt`

The local model consumed exactly the configured `num_predict=768` generation
budget but returned no final body. The result file contains only a newline, so
`local_worker.py` rejected it with:

```text
Worker contract error: malformed or ambiguous status contract
```

This is a valid fail-closed terminal result. The orchestrator did not retry the
instance or substitute a different reviewer.

## Teardown and durable evidence

- All run leases were absent before teardown.
- Workspace UUID `FEF811B3-D6B9-4403-A792-3E16925FE111` matched the live tree.
- `just fleet-down fleet-template-audit-20260713` exited `0` and closed
  `workspace:29`.
- The active manifest disappeared and no feature lease remained.
- The workspace was absent from the post-teardown cmux tree.
- Durable archive:
  `orchestration/runs/archive/fleet-template-audit-20260713-20260713T142332Z/`
- Archive hashes:
  - ledger: `eb00709727ce816fb49d773aac2f002c7e37f322d6e93d8a4670529e11aa9cbe`
  - manifest: `a89e1b3c75e26f6c44701d306f4b799465736beaca6f640dd3bbaa8c9fbf6f8b`
  - state: `ab5c55172c09f2989d98c8067a91d70aab6cc66a0d56fb81129765ea4828a606`
- Total boot-to-archive time: about 8 minutes 38 seconds.

Git remained at the same branch and HEAD. The only working-tree changes after
teardown were the orchestrator-authored prompt, its wiring rule, its static
test, and this smoke report; none of the three audited sources changed.

## Static verification

- `python3 -m unittest -v tests.test_spec_coherence`: 3 passed.
- `python3 -m unittest discover -s tests`: exit `0`.
- `git diff --check`: passed.

The live run exposed and corrected one prompt-only assumption before dispatching
later phases: frontier terminals do not require a non-empty `result_file`;
their evidence is the run-bound structured transcript and sentinel. Local
workers still require a real non-empty result file.

## Open lanes

- The local reviewer could be retested with a smaller evidence pack or a larger
  generation budget, but either action is a new attempt and was not authorized
  as part of this fail-closed smoke.
- The other five presets were not exercised.
- No writer preset, target worktree, branch preservation, or human BUILD
  approval path was exercised.
- Claude fallback, GLM, race execution, timeout escalation, explicit abandon,
  and recovery-after-absence were not exercised.
- The CONTROL lead was not assigned a worker result; only its boot and lifecycle
  presence were observed.
