# impl-notes: S0 single-Mac stabilization

> Registro histórico de Slice 1; no sustituye el smoke posterior al último
> cambio. La consolidación vigente está en
> `s0-single-mac-operational-truth-20260717.md`.

## Spec anchored

Apply the audit remediation for one personal Mac: exact compiled boot, durable
local identity/evidence, artifact and caller isolation, ownership/budget/phase
integrity, and coherent operator contracts. Multi-host federation and external
WORM infrastructure are explicitly outside this local S0.

## Deviation log

| # | Area | Deviation | Classification | Conservative resolution | Reversible? |
|---|------|-----------|----------------|-------------------------|-------------|
| 1 | Regulated boot retry | `ControlLifecycle.stop()` is terminal and cannot be restarted after a transient boot failure | REGISTRA-Y-SIGUE | Keep the mission-bound service alive across retry; reconcile the same process and stop only on terminalization | yes |
| 2 | Test fixture | A temporary target parent did not exist in one synthetic case | REGISTRA-Y-SIGUE | Create fixture parents; no runtime behavior changed | yes |
| 3 | Compiled local identity | Missing local `hook_source` appeared as `null` in one projection and empty in the manifest | REGISTRA-Y-SIGUE | Normalize centrally to the manifest representation and test the research roster | yes |
| 4 | Research mission mode | The research preset inherited guided mode although its workflow is a canonical autonomous Mission | REGISTRA-Y-SIGUE | Declare `mode=autonomous` on that preset and lock it in router tests | yes |
| 5 | Launch binding review | Initial roster hashes omitted private launch fields and the manifest contract remained v2 | REGISTRA-Y-SIGUE | Add private full-plan main/assurance digests, publish only hashes, cut fresh manifests to v3, and require restart for mission-bound v1/v2 | yes |
| 6 | Independent local pane | `FLEET_RUNS_DIR` and a custom router path were not transported into the pane command | REGISTRA-Y-SIGUE | Prefix the exact worker command with an explicit environment envelope and add a command-level regression | yes |
| 7 | Regulated canary socket | The first temporary socket root exceeded the AF_UNIX safe length | REGISTRA-Y-SIGUE | Preserve fail-closed behavior and retry the canary with a short private root | yes |
| 8 | Canary terminal status | `succeeded` was rejected because a lifecycle-only canary has no unified archive | REGISTRA-Y-SIGUE | Preserve the archive invariant; terminalize the canary as intentionally abandoned and report only the lifecycle lane as PASS | yes |

## Stops awaiting resolution

None for Slice 1. Full regulated workflow execution requires external WORM
infrastructure and remains an explicitly untested external lane, not a local
implementation blocker.

## Three for the next attempt

1. Make every command crossing into an independently created pane carry its
   complete non-secret runtime address envelope explicitly.
2. Treat a real smoke failure as design input and retain the failed attempt in
   evidence; never convert a partial path into PASS.
3. Hash the complete effective private plan and expose only the digest, so new
   launch fields cannot silently escape binding.

## Status

Slice 1 implementation, expanded tests, autonomous live smoke, and regulated
lifecycle canary complete. Ready for the next S0 slice.
