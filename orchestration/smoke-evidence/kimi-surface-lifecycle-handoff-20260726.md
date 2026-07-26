# Kimi surface lifecycle — session handoff (2026-07-26)

## Exact repository state

- Baseline before this session: `ae8b2b1` (`feat(kimi): migrate fleet reviewer from kimi-cli to kimi-code 0.28.1`).
- Stabilization commit: `2469b3d` (`fix(fleet): harden portable orchestration lifecycle`).
- No push, pull request, deploy, forced deletion, `chmod`, or `chown` was performed.
- The commit contains only repository code, tests, skills, and documentation. Runtime manifests, ledgers, transcripts, provider homes, credentials, leases, and workspaces remain outside the commit.

The committed slice establishes the portable CI baseline, accent-insensitive risk classification, hardened OpenCode isolation, launch-generation identity, descriptor-safe Kimi state/bindings, Wire 1.4 support, fail-closed provider cleanup, exact retirement quarantine, and Mission child-admission reconciliation. It does **not** complete the bridge supervisor/reaper described below.

## Verification completed

| Gate | Result |
| --- | --- |
| Portable repository gate (`scripts/check-ci.sh`) | 1035 tests passed in 976.622 s; 1 skipped |
| Mission/Kimi focal suite | 176 tests passed in 18.649 s |
| Final coherence/router/Kimi suite after documentation reconciliation | 70 tests passed in 1.115 s |
| Python compilation | passed for the changed lifecycle modules |
| Diff validation | `git diff --check` passed |
| Staged secret signature scan | no recognized credential or private-key material found |

The full portable gate ran after the implementation changes. The final 70-test gate covered the later documentation-only Wire 1.4 reconciliation, so the full gate was not repeated.

## Preserved Mission-bound runs and services

Four smoke workspaces remain deliberately live and identifiable in cmux. Their Kimi verify runs are durable `indeterminate` with retained leases:

| Feature suffix | Durable reason | Preserved condition |
| --- | --- | --- |
| `h` | transfer unconfirmed | workspace, lease, provider state, transcript, and evidence preserved |
| `i` | transfer unconfirmed | later bridge events exist, but the earlier terminal is immutable and remains indeterminate |
| `j` | provider evidence unavailable | preserved transcript later parses the exact `DONE` sentinel under the Wire 1.4 reader; the ledger was not rewritten |
| `k` | transfer unconfirmed | wrapper bridge disappeared before binding; later manual diagnosis did not alter the durable terminal |

The diagnostic bridges started manually for `i` and `k` were stopped. Test and Mission runner processes started by this session exited. One bridge still runs as a child of the preserved `j` provider wrapper, and the four Mission workspaces retain their interactive TUIs. They were not interrupted manually because the only accepted teardown path rejects their retained leases; bypassing it would violate the fail-closed preservation requirement.

## Preserved Kimi surface inventory

There are nine active, generation-bound Kimi roots and no retired roots. Eight map exactly to smoke features `c`, `d`, `f`, `g`, `h`, `i`, `j`, and `k`; one older active root has no current manifest mapping and is therefore classified `unknown-preserved`.

Each active root currently contains the three expected sensitive runtime categories: provider configuration, credential material, and MCP configuration. Total sensitive runtime files observed: 27. None was staged, committed, copied into this document, scrubbed, or unbound. The next session must not treat the roots as garbage merely because their panes are old.

## Reader snapshot protected by EACCES

The preserved reader snapshot is `/private/tmp/fleet_workspaces-501/kimi-life-smoke-20260726b-verify`, owner `hector:wheel`, mode `0500` (`dr-x------`). The official clone guard now uses descriptor-relative rename-no-replace, and its focal tests pass, but the exact stage operation still returns EACCES. No permission mutation or forced removal was attempted.

Recovery must remain inside `fleet_clone_guard.py`: exact journal/identity validation, descriptor-pinned source and destination, auditable intent/receipt, and no broad pathname deletion. If the operating system cannot rename the sealed directory, the guard needs a documented tombstone or equivalent official transition that does not weaken reader immutability.

## P0 work for the next session

1. **Bridge supervisor:** persist owner-bound stderr and an exact exit receipt tied to surface, workspace, Mission, and launch generation. Couple provider health to the bridge so a missing binding or dead bridge stops the provider before any authoritative turn can be accepted.
2. **Provider/bridge shutdown:** define an official idempotent stop operation that can quiesce a preserved surface without guessing identity or silently releasing a retained lease.
3. **Retirement authority:** add an explicit durable transition for an operator-abandoned Mission. A releasable terminal may quarantine, scrub, and unbind only after confirmed quiescence; `indeterminate` or `lease_retained` must remain preserved unless a separate, audited authority resolves it.
4. **Reaper:** reconcile the nine roots individually. Require exact manifest/ledger/binding identity for mapped roots; keep `unknown-preserved` intact until its owner is proven.
5. **EACCES path:** implement and test the official descriptor-safe reader transition without `chmod`, `chown`, forced deletion, or path-wide commands.

## Required tests before another live smoke

- Bridge exits before binding, after binding, during prompt submission, and after terminal evidence.
- Provider cannot accept a Mission turn while its bridge is absent or unhealthy.
- Stderr and exit receipts reject symlinks, wrong owner/mode, identity drift, replacement, and concurrent writers.
- Reaper distinguishes releasable, abandoned, indeterminate, and lease-retained generations and is idempotent after interruption.
- EACCES recovery proves exact source/destination identity and leaves the snapshot unchanged on every failure.
- Inventory tests prove that unrelated and unknown roots are never scrubbed or unbound.

Only after those tests and a proportional portable gate are green may one reversible Mission-bound smoke run. Its exit criteria are: exact binding before submit, one `UserPromptSubmit`, one durable releasable terminal, provider/bridge quiescence, workspace teardown, exact quarantine, sensitive scrub, matching unbind, no new retained lease, and no increase in active-root count. Stop immediately on any `indeterminate` result.

## Session closure

This session is closed after the handoff commit. No work continues automatically. The preserved runtime state is intentional evidence for the next authorized supervisor/reaper session.
