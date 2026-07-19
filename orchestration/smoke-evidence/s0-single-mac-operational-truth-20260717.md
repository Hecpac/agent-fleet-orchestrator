# S0 single-Mac — final operational truth

Date: 2026-07-17 (America/Chicago); final UTC events: 2026-07-18

## Status and supported claim

`STATUS: PASS — TESTED S0/LOCAL RECIPE ONLY`

The repository is operational for the tested S0 boundary: one trusted Mac, one
trusted Unix UID, one trusted local runs root, local CMUX, and local Git. The
acceptance run covered a real Ollama worker and real CMUX teardown, plus
deterministic production-process lanes for writer publication, authenticated
AF_UNIX control, and assurance handoff.

This PASS does **not** claim containment against hostile code already running as
the same UID, a compromised CONTROL/CMUX/provider executable, multi-host
coordination, external WORM custody, regulatory compliance, cryptographic proof
of a distinct human, a hard total-token ceiling unsupported by the provider, or
semantic truth merely because several models agree.

## Before → now → benefit

| Before hardening | Final contract | Benefit inside S0 |
|---|---|---|
| Effect consumers could consult a changed live router | Compiled v2 embeds and seals the complete router snapshot and launch digests | The admitted plan cannot silently change after compilation |
| Lead fallback could select a paid provider implicitly | Codex remains the default; fallback requires explicit opt-in or provider selection | No surprise paid Lead |
| Dispatch ownership, credits, and writer exclusion were spread across local checks | Every Mission launch advances `reserved → committed → authorized → started → finalized` with exact owner/effect/task/run bindings | Retries, children, and terminal evidence cannot cross runs |
| Reader polling and handoff depended on `flock` fairness | Readers consume descriptor-pinned immutable inode snapshots; handoff owns a gate, publishes `.mutation.quiescing`, drains admitted writers, and rejects late writers immediately | Polling cannot starve publication and no late mutation silently crosses handoff |
| First ledger publication used a link/unlink crash window | First publication uses no-replace rename with deterministic temporary recovery, including the historical hardlink shape | A first-write crash recovers to one single-link ledger |
| Writer shared target Git internals | Writer uses an isolated clone with no remotes/alternates; CONTROL publishes a clean descendant by durable intent and compare-and-swap after quiescence | Model-visible work cannot advance target refs before controlled close |
| Handoff FIFO opens and outer supervision could outlive one another | Boot-lock, READY, and COMMIT have validated deadlines; early child exit fails fast; the outer deadline includes all waits plus grace and kills/reaps the exact process group | No indefinite FIFO wait or orphaned barrier holder |
| Teardown order was described inconsistently | Runtime and docs now agree: close CMUX/retire clone, publish exact writer commit, create/verify Mission archive, then archive fleet records | Operators can reason from documentation without reversing the real effect order |
| Unknown token usage could look like zero | Usage receipts explicitly distinguish `observed`, `not_incurred`, and `unknown`; unsupported hard totals are rejected | Accounting does not invent certainty |

## Definitive validation

The acceptance command ran outside the app sandbox because AF_UNIX bind tests
require local socket access:

```text
python3 -m unittest discover -s tests
Ran 895 tests in 764.783s
OK (skipped=1)
exit_code=0
```

The single skip is the suite's expected platform/availability skip; no test was
manually excluded. Expected negative-test stderr (malformed worker contracts,
missing CMUX, and historical compiled schemas) appeared without failures.

Additional final gates:

| Gate | Result |
|---|---|
| Python 3.14 affected integration (`mission_run`, assurance handoff, timeout regressions) | 45/45 in 43.333s |
| Python 3.12.12 affected integration | 45/45 in 42.515s |
| Post-fix full-shell handoff + boot-lock race + writer publication | 3/3 in 34.359s |
| Timeout/process-group focused audit suite | 33/33; independent sign-off |
| Mission snapshot publication stress | 100/100 repeated reader/writer races |
| Independent lock/timeout/handoff review | no remaining P0/P1/P2 in that reviewed scope |
| Ruff | unavailable on this Mac; recorded, not silently substituted |
| Bash syntax, Python source compilation, docs/spec/schema tests, `git diff --check` | 14/14 shell scripts; 111/111 canonical Python sources; 20/20 tests; clean diff-check |

Three debts remain registered rather than hidden. Structured terminal evidence
records the `source_event_sha256` asserted by trusted CONTROL; it is not a
receipt from an independently controlled verifier. Historical Missions remain
readable, but an active legacy admission ledger has no authority-preserving
in-place migration: preserve it as evidence and start a current Mission instead.
Finally, deriving and rewriting the full Mission ledger is O(n²) across a long
event history. No reproducible final benchmark artifact was recorded, so this
report makes no timing claim. Add checkpointing/segments or SQLite WAL before
treating this personal S0 system as a large shared service.

## Final live lane A — real CMUX + real local Ollama

This is the final post-fix canary and the primary live acceptance lane.

```text
temporary root: /private/tmp/af-s0-postfix.Br6c3K (removed after verification)
feature: s0-postfix-br6c3k
workspace: workspace:43
workspace_uuid: 7549D674-C552-45B2-A0B0-4FF350A27516
lead: monitor (FLEET_NO_LEAD=1; no provider call)
triage surface: surface:159
triage_uuid: 155C154E-4565-4AC8-856E-85056C501D51
provider/model: ollama / gemma3:4b
phase: CONTROL → RECON
run_id: cc380cc1-57ca-4d1e-8d93-35c5587e617f
task_sha256: dd6e4b08aba879798f2480278b3b64416309be3aae71a944e4457d7c19c877f2
terminal: succeeded, exit_code=0, exactly one terminal event
```

Durable worker result:

```text
STATUS: DONE
SUMMARY: S0 postfix local smoke passed
EVIDENCE: S0-POSTFIX-OK
RISKS: None
NEXT_ACTION: None
```

Observed usage receipt:

```json
{"capability":"hard_output_only","input_tokens":188,"output_tokens":35,"provider":"ollama","schema_version":1,"state":"observed","total_tokens":223}
```

Receipt SHA-256:
`0666bac128238de4efc3b4e4fe801c7b8ad7da9ffa610ec7c20cfa400183277c`.

The strict lifecycle ledger contains exactly `dispatched → running → succeeded`
for that run. Teardown returned `closed workspace:43`, and the archived manifest
was verified by `fleet_manifest_guard get-archived` against its digest:

```text
archive directory:
  s0-postfix-br6c3k-7549D674-C552-45B2-A0B0-4FF350A27516-6e1b5c15c06c66066bd37830f7e0587c361fd0293d2af613d09ef831edb3bfb8
manifest SHA-256: 6e1b5c15c06c66066bd37830f7e0587c361fd0293d2af613d09ef831edb3bfb8
ledger SHA-256:   aafbd78e71376758a60c459e5dcdf80ea564c4c68f750567f76d785b7be51c38
state SHA-256:    e543008a7257fcf0ed65ed62068c88472042f71cdcdf6117a4c12b62bdb7ddaa
```

Post-teardown checks: no active manifest, no active lease directory (only the
idle `locks/.coordinator` sentinel), no worktree root, no socket, and no
temporary CMUX workspace.

### User CMUX preservation

Immediately before and after the final canary the tree was identical:

```text
window window:1 [current]
└── workspace workspace:1 "idle" [selected]
    ├── pane pane:1
    │   └── surface surface:1 [terminal] "~/Projects/agent-fleet-orchestrator" tty=ttys000
    └── pane pane:128 [focused]
        └── surface surface:129 [browser] GitHub pull request #2
```

The existing terminal, browser, selected workspace, focused pane, and URL were
unchanged.

## Final lane B — writer publication (deterministic full-shell fixture)

This lane used the production `fleet-up.sh`/`fleet-down.sh`, real isolated Git
clone, real commit, real bundle/import, and real compare-and-swap publication.
CMUX and provider executables were deterministic test doubles, so this lane
proves the writer/publication transaction but is not presented as a frontier
model result.

```text
feature: s0-writer-postfix
fixture workspace_uuid: 00000000-0000-0000-0000-000000000001
writer instance: build
branch: fleet/s0-writer-postfix/build
baseline / writer HEAD before: 42cbb70bf8542f84e8e0c0e41e34258de954a639
writer final SHA:              097b96940c79b2d14c1ddd2cdcaff0a96912e74b
published target SHA:          097b96940c79b2d14c1ddd2cdcaff0a96912e74b
archive manifest SHA-256:      764a75949e62947a6bd6fceb540ab5a4f0c5694fbaa893cecacd1af9db79de9d
archive state SHA-256:         fd25b07e30d16b31d8d8e73aa9552fb0ff02cc2f8860d27b30970a97f7254ed4
archive ledger SHA-256:        e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

The archived manifest reports `workspace.handoff_state=quiesced` and
`workspace.quiesced=1`; `fleet_manifest_guard` returned the exact final SHA.
The writer clone and active manifest are absent, one fake-CMUX close occurred,
and the published tree contains `S0-WRITER-POSTFIX-OK`.

## Final lane C — authenticated AF_UNIX control process

This deterministic Mission fixture launched the real `fleet_mcp.py` server,
used the real lifecycle and admission APIs, authenticated the exact process,
then finalized and stopped it. It made no provider or CMUX call.

```text
mission_id: a11cbf03-959d-5fd8-af3d-15af5804a624
compiled_digest: 144b97c3bff232d3df57c612cbf292efc897f0cee53d405940ba68aa238eecab
admission_id: 87c7b4d5-ae3b-5493-9471-4957c6543112
run_id: 3f8a9270-0962-5220-9c7b-321fae4d5efd
authorization_event_sha256: 7cc7162dc91489ad2813f6d78b7dcc4478c3c1c0599b48ad24772c07164bde86
finalization_event_sha256: e190d9efd944d6a00f945f8cf98d89bae6741e3b4b73a2657d5624909a8b3d01
ledger head: e8e4f04f63d744d6b10097caddad43d16b88ab991a8f475138b61e2697d2fa46
launch_id: 4a952a42-f7db-4323-8616-2303efbd44e9
pid: 12951
protocol: fleet-control-unix-v2
socket root mode: 0700
all endpoint modes: 0600
```

Health returned `ok` for the exact PID. Admission ended `finalized/inactive`,
the deterministic Mission ended `abandoned` because it intentionally performed
no external work, service stop succeeded, and every endpoint was absent after
stop.

## Final lane D — assurance handoff production process

This deterministic runtime fixture launched the real
`fleet_assurance_handoff.py hold` process and drove the actual READY/COMMIT
protocol. It did not close a real CMUX workspace; lane A covers real CMUX close.

```text
mission_id: ac4564cb-165d-5e9d-8b8e-02bfd4b87f06
feature: s0-handoff-postfix
READY observed: yes
COMMIT sent: yes
process exit: 0
receipt status: complete
receipt item count: 8
receipt SHA-256: 90794d93e64d7901e726eaedb7ded2a59c65bc9380ff8e061996639a9c6a32d3
approval_event_sha256: 1f177ead925ed0208f7331488a8789669b5367b778b66ea58d07ebf98690159f
```

Intent and receipt are canonical JSON; every moved file is 0600 and single-link;
all source runtime files are absent; Mission ledger bytes are unchanged;
`.mutation.quiescing` is absent; replay/preflight returns `complete` with the
same receipt. The final full suite also exercises the complete
`fleet-down --handoff-assurance` shell composition, timeout, interruption, and
crash-recovery paths.

## Cost and superseded rehearsals

The final post-fix smoke made **no paid-provider inference calls**. Its only
inference was the 223-token local Ollama receipt above.

For completeness, a pre-final rehearsal under feature
`s0-final-0717-7jzghv` used two local runs totaling 465 tokens. Its first prompt
asked for the wrong JSON contract and lacked the requested evidence token; the
second contained `S0-LOCAL-OK`. It is not used as final PASS evidence. An older
Claude discovery canary incurred USD 0.56816 before this final campaign; it
remains `PARTIAL` historical evidence and was not repeated.

## Reproducibility snapshot

```text
timestamp local / UTC: 2026-07-17T21:42:14-0500 / 2026-07-18T02:42:14Z
macOS / architecture: macOS 26.5.2 (25F84) / arm64 (Darwin 25.5.0)
Python default: Python 3.14.6 (/opt/homebrew/bin/python3)
Python compatibility: Python 3.12.12
CMUX: 0.64.19 (99) [1c22c5564]
Git: 2.50.1 (Apple Git-155)
branch / HEAD: fix/live-smoke-verification / daf8b8bfdadb1effd1822dc23c91a254d896605f
upstream divergence: origin/fix/live-smoke-verification; 0 behind / 0 ahead
porcelain entries: 135
porcelain path/status SHA-256: d21bc6a3a1244947ec1a701f7aa4a534c65ea087b3e590c0e47a93ad09c30d30
tracked binary diff SHA-256: 4c433441de85d7de3bc13446fb50f6543e1a6ff5e2c66d4a25fc0193fbf992a1
untracked path+content SHA-256 (excluding this self-referential report and final impl-notes): 447826bdaab53cb9cf54351740ed26fc129801a7fbcab4f858d45f428337fa0e (34 files)
```

Configuration SHA-256 values:

```text
orchestration/router.yaml:                 361ebac595a4c17a31252ab4b1a3b0cbc5dde46d4907972cc8fb11424f8593e0
workflows/hotfix.yaml:                     8faef2d2b08e88ab6dcef7e702b898d529e263283b922678f976fd49b15b43c2
workflows/implementation.yaml:             66922efd790d1e250400c7ff9de3b078070f2c2fb9b07cb36c376d3f8a26fdbb
workflows/local-worm.yaml:                 767a3a8217c453b8ab81591a48821a9d65f3f2c1fc7c5f02e713178929255c55
workflows/regulated.yaml:                  774138d79d83dec4b78340c2a4a21b71ae284be3fde54f6803b954315478a6a0
workflows/research.yaml:                   1296bec88f5932d6dc57f0773a01c863d2d43ceb5eb52d291a86e4fb497bf61d
```

The tree remains intentionally dirty and no commit was created by this work.
The three fingerprints above have distinct scopes so untracked implementation
files are not hidden and this report does not attempt an impossible self-hash.

## Final acceptance

`OPERATIVE: YES, for the tested S0/local recipe.`

Unit/integration tests, independent scoped reviews, real local inference, real CMUX
archive/teardown, writer CAS publication, authenticated control lifecycle,
handoff receipt, exact cleanup, and preservation of the pre-existing workspace
all passed. Expansion beyond the stated S0 boundary requires new architecture
and new evidence rather than relabeling this PASS.
