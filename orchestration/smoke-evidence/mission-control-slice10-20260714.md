# Mission Control Slice 10 — end-to-end smoke evidence

> **HISTORICAL / SUPERSEDED.** This evidence belongs to the dated tree and
> predates compiled-v2, current admission, and current locking/handoff. It is
> not acceptance of the final working tree; see
> `s0-single-mac-operational-truth-20260717.md`.

Date: 2026-07-14

## Scope

This closeout exercised the canonical Mission runner, heterogeneous CMUX
roster, authenticated Fleet Control socket, nested delegation, writer
isolation, portable archive verification, strict Codex permissions, sensitive
archive approval, and regulated WORM preflight. Durable runtime evidence lives
under the ignored `orchestration/runs/` tree; this file records the stable IDs
and observed terminal facts without copying prompts, results, credentials, or
environment values.

## Successful live missions

| Path | Mission | Durable result |
|---|---|---|
| Autonomous read-only | `7a958642-872c-5881-85b9-71c7ee9c4de3` | Lead run `39cbe94b…` succeeded; archive verified with 13 entries; teardown removed the fleet. |
| Isolated writer | `e09c44a6-ae45-58ae-bf7a-b88a2600db73` | Builder run `3e45b79e…` produced artifact `e2f88f…` and commit `8c5da5d18dbf39bcc7cb56f2381ae06657754803`; the exact expected file was committed on the declared writer branch; archive verified with 16 entries; teardown was clean. |
| Socket + nested delegation | `fedb61f5-72fe-5d33-a06b-6c27fb90fb3d` | Root Scout run `958c05cf…` and depth-2 Verifier run `0570c128…` succeeded with correct parent/delegated-by lineage; archive verified with 19 entries and content root `395693…`; final SHA equalled base SHA and no lease, manifest, or worktree survived teardown. |
| Fixed Codex hook bridge | `73555457-032a-5d6d-b8ee-40d77860b90e` | Lead run `e308c84b…` delegated to Scout run `b781670a…`; both reached ledger status `succeeded`, Scout artifact `44c3b1f…` was relayed exactly, and the result verified the first README heading without edits. Offline archive verification returned 16 entries and content root `a84fcfad…`; teardown removed the fleet branch, manifest, and worktree. |

The writer smoke started from `c8ff8a3…`; its single change contained exactly
`mission-control writer smoke` plus the terminating newline. Git showed the
commit reachable from the durable writer branch before teardown.

## Parallel and nested collaboration

Mission `4256f333-a00a-538a-9eba-66f3284e0777` registered both root
delegations before the first wait:

- Scout delegation `6d505f94-6843-5c54-8adc-1aab0130144a`, run
  `95985ba6-3a15-5769-b26c-d97096ec96a6`, succeeded as artifact
  `9a0f385f6b24c0e0ad11045eb18d0f7997cb94f58a1c03017048db530dca8b7f`.
- Challenger delegation `440b31b9-aa52-552c-9174-e6e5a4b32d04`, run
  `23707d0a-a601-5d4d-80a3-70ceedbb12bf`, was cancelled after the provider
  reported insufficient balance/resource package.
- Scout created depth-2 Verifier delegation
  `242520bd-4d43-5f7b-ae42-84ee9423fcbb`, run
  `367d6f12-5fe0-513b-8b91-f081cec5b103`, which succeeded as artifact
  `459cda2cb821ebf1a86b5096f4766b514e4c59758eb7ac9bdd92330367185c7b`.

The Mission was terminalized `blocked` with the exact external-quota reason,
the interrupted Lead lease was explicitly abandoned, Fleet Control stopped,
and `fleet-down.sh` closed the CMUX workspace. Offline archive verification
then returned `valid=true`, `entries=19`, base/final
`4b00c1d3796a92c4cc50d53b4551a68addda6f0b`, and content root
`6ed6d33ba697f8e62221348e52db34cd9057d44234e04569d468c47db13b19cf`.

The Verifier found duplicate Mission Control recipes in `justfile`. They were
removed, the sensitive archive approval recipe was added, and `just --list`
subsequently exited 0. This is a live-smoke defect corrected during closeout.

## Permission boundary

A real mission-bound Codex specialist was launched with the generated named
permission profile. A health request to its exact canonical Fleet Control Unix
socket succeeded. A write probe in the target repository failed with kernel
`EPERM`, and no probe file was created. The specialist used an ephemeral Codex
home containing existing authentication plus only CMUX hook commands; an
unrelated controller hook was excluded. Final hardening replaced copied hook
command text with the fixed `cmux-codex-hook.sh` bridge; its deterministic
regression rejects even an unrelated command containing CMUX-like text. This confirms socket reachability did
not require granting filesystem write access or inheriting the controller's
broad sandbox mode.

The fixed bridge was then exercised end-to-end in the live Mission above. Its
controller-owned `UserPromptSubmit` event authorized each exact tracked run,
and its verified `Stop` event plus structured sentinel terminalized both Lead
and Scout as `succeeded`. This proves the hardening did not merely pass a
configuration-level test: it preserved authenticated live dispatch and result
capture without importing arbitrary user hook commands.

## Sensitive full archive approval

A live mission whose declared risk categories included `credentials` could not
create its `full` archive without a separate archive approval. CONTROL recorded
scoped receipt `a6b3b029…` with mode `0600`; the approved archive then closed
and teardown completed. Deterministic tests cover scope mismatch, expiry,
idempotency, workflow binding, and absence of approval.

## Regulated WORM preflight — external blocker proven fail-closed

Feature `mc-s10-worm-preflight-20260714`, mission
`d804d7ac-f80a-5c93-9440-43e703c09560`, was run with workflow `regulated` and
execution profile `regulated`. It exited 1 before boot and named the missing
configuration: `FLEET_WORM_BUCKET`, `FLEET_WORM_REGION`,
`AWS_ACCESS_KEY_ID`, and `AWS_SECRET_ACCESS_KEY`.

The Mission remained `assurance_approved`; the CMUX workspace inventory was
byte-identical before and after; no fleet manifest, control socket, or runtime
state was created. This is the required fail-closed ordering. A success receipt
cannot be manufactured locally: the acceptance path requires a real versioned
S3 Object Lock bucket returning COMPLIANCE-mode retention metadata.

## Deviations and fixes found during smoke

- The controller's `danger-full-access` Codex setting overrides ordinary CLI
  sandbox flags. Mission-bound read-only Codex specialists now use an ephemeral
  home and named permission profile rather than inheriting that setting.
- Codex hook trust UI could consume the first tracked prompt. Only for the
  ephemeral configuration, the controller passes the explicit hook-trust bypass
  after installing its fixed CMUX-only hook bridge.
- A fast specialist could call Fleet Control before its run was registered.
  CONTROL now deterministically preassigns the run ID and binds the exact token
  before prompt transfer.
- OpenCode can emit a terminal hook before its SQLite transcript is visible.
  Evidence extraction now performs a bounded retry and remains fail-closed.
- An empty Git tree produces a valid empty tar, not an archive with header
  entries. Structural empty-tree verification now accepts that canonical form.
- `justfile` contained duplicate recipes. The duplicates were removed instead
  of weakening the recipe parser.

## External completion lanes

Two provider-owned operations remain impossible in the current environment:

1. configure a real S3 Object Lock COMPLIANCE bucket and AWS credentials, then
   rerun the regulated Mission to obtain versioned WORM anchor receipts;
2. replenish the Z.AI resource package, then rerun the heterogeneous parallel
   relay smoke so the Challenger can produce the artifact that CONTROL relays.

All deterministic relay, WORM, socket, lineage, resume, cancellation, archive,
and failure-path contracts remain covered by the repository test suite.

## Final local verification

After the live closeout fixes, `python3 -m unittest discover -s tests -p
'test_*.py' -v` ran 275 tests in 70.464 seconds with `OK`. The fixed-hook
Mission archive independently passed `fleet_archive.py verify` with
`valid=true`; its base and final SHA were both
`4b00c1d3796a92c4cc50d53b4551a68addda6f0b`, proving the read-only run left no
repository mutation. Compile, shell syntax, schema, workflow, router,
duplicate-key, secret, documentation-coherence, and diff checks were repeated
during final closeout.
