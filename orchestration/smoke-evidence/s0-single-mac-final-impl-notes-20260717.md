# S0 single-Mac — final implementation notes

Date: 2026-07-17 (America/Chicago)

`STATUS: CLOSED FOR S0; REGISTERED SCALE/BOUNDARY DEBT REMAINS`

This is the final deviation log for the hardening and smoke campaign. Historical
slice notes remain historical and are not rewritten as evidence for this tree.

## Resolved deviations

| Observation during implementation | Classification | Resolution and evidence |
|---|---|---|
| Shared reader `flock` could starve a writer on macOS | Correctness/liveness defect | Readers now consume a validated descriptor-pinned immutable inode snapshot without taking the shared ledger lock; publication-race stress passed 100/100 |
| A handoff barrier could starve behind a stream of shared mutation locks | Correctness/liveness defect | Added `.mutation.gate.lock` plus durable `.mutation.quiescing`; late writers fail fast, admitted writers drain under deadline, and stale markers recover only under the gate |
| First ledger creation used a link/unlink window | Crash-consistency defect | Replaced with no-replace rename, deterministic temp cleanup, and exact recovery of the historical single hardlink shape |
| Delegation validation could reload Mission state while already owning `MissionTransaction` exclusively | Nested-lock defect | Compiled workflow/state snapshots are loaded once and propagated through validation; no Mission read/lock is reacquired inside the transaction |
| Handoff FIFO open/read could block if the helper failed before attaching or replying | Availability defect | FIFO descriptors open RDWR; boot-lock, READY, and COMMIT have validated deadlines and observe the Bash direct-job table so exited/zombie children fail fast |
| Interrupting READY before `handoff_lock_held=1` could leave the child | Cleanup defect | Cleanup now owns any recorded child PID, not only a READY holder; interruption regression proves exact reaping |
| `mission-run` had a fixed 180-second outer timeout below READY+COMMIT defaults | Composition/liveness defect | Outer timeout is `boot + READY + COMMIT + 180s`; values are frozen from the same environment and invalid overrides fail before effects |
| Killing only the direct parent could leave descendants retaining pipes/barriers | Process-lifecycle defect | Every supervised command owns a fresh session/process group; timeout sends TERM, probes the PGID through grace, then KILLs/reaps the PGID even if the leader exited or pipes closed; a stubborn same-PGID grandchild regression passes |
| Docs claimed Mission archive preceded CMUX close | Documentation inconsistency | Guide now matches runtime: close/retire, publish, create/verify Mission archive, then archive fleet records |
| First final local prompt requested JSON instead of the worker's five-field contract | Smoke-harness deviation | Preserved as a superseded rehearsal; final post-fix run returned the exact five-field contract and `S0-POSTFIX-OK` |
| First control evidence collector used the wrong `read_events` call signature after the service had already stopped | Smoke-harness deviation | Re-ran a clean post-fix control lane; start/health/finalize/stop and final evidence all succeeded |

## Registered, not hidden

| Item | Current decision | Exit criterion |
|---|---|---|
| Mission derive/rewrite cost is O(n²) over long histories | Accepted only for this personal S0 project; no timing claim without a reproducible benchmark artifact | Add checkpoints/segments or SQLite WAL and benchmark before large fleets/shared service |
| Ruff executable is unavailable | Recorded as tooling unavailable; final acceptance uses the complete test suite, built-in Python compilation, Bash syntax checks, strict schemas, and diff-check | Install/pin Ruff and add it to the reproducible gate if style lint becomes required |
| Independent source-event attestation | `source_event_sha256` is asserted and recorded by trusted CONTROL inside S0; it is not an independently controlled receipt | Add a separately controlled effect broker/verifier and bind its signed receipt |
| Active legacy Mission migration | Historical ledgers stay readable, but an active legacy admission ledger cannot acquire current authority/provenance in place | Preserve the old Mission as evidence, abandon it explicitly, and start a current Mission; design a proven authority-preserving migration before changing this rule |
| Same-UID hostile code, compromised CONTROL/CMUX/provider | Outside the S0 trust boundary | Add OS/user/container isolation, signed binaries/policy, and a smaller trusted computing base |
| Multi-host coordination | Not implemented | Add distributed ownership, leases/consensus, authenticated transport, clock/deadline policy, and failure-domain tests |
| External WORM/regulatory custody | Not available; local WORM is development-only | Configure an external compliant backend and run independent retention/deletion evidence |
| Hard total-token ceiling | Unsupported by current mixed provider capabilities | Use providers/adapters with enforceable `hard_total` or retain fail-closed rejection |
| Semantic truth/human presence | Not proven by orchestration records | Use domain evaluation and external human/identity controls where required |

## Evidence discipline

- The real Ollama/CMUX lane is identified separately from deterministic
  production-process fixtures.
- The writer fixture proves shell/Git publication, not frontier-model quality.
- The handoff fixture proves READY/COMMIT/receipt, not real CMUX closure.
- Lane A proves real CMUX closure and local inference, not Mission admission.
- No paid inference was used in the final post-fix smoke.
- Temporary roots are removed only after their IDs, hashes, receipts, and
  absence checks are recorded in the final operational-truth document.

## Final gate

The definitive suite passed 895 tests in 764.783 seconds with one expected skip.
Python 3.12 affected integration passed 45/45. Independent review reported no
remaining P0/P1/P2 in the reviewed lock/timeout/handoff scope.
