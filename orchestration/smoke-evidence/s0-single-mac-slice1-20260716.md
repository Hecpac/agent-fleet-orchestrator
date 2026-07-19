# S0 single-Mac stabilization — Slice 1 live smoke (2026-07-16)

> PASS for the Slice 1 code at that date. It predates compiled-v2 snapshot,
> global admission, strict-JSON, phase, and dialogue closure changes and is not
> evidence that the final 2026-07-17 working tree passes. See
> `s0-single-mac-operational-truth-20260717.md`.

## Verdict

`STATUS: PASS`

Canonical Mission boot, compiled-plan binding, autonomous Fleet Control
delegation, a real Ollama local worker, durable result adoption, unified archive,
and exact teardown completed on an isolated temporary Git target. Two earlier
attempts failed closed and exposed defects that were fixed before this PASS; they
are retained below instead of being hidden.

## Final autonomous mission

- Feature: `s0slice1v3final0716`
- Mission: `c13e8e4d-fc8e-5172-b071-47480c617db2`
- Workspace UUID: `D0705150-B258-4704-952C-C35FDEE830B4`
- Workflow/preset: `research` / `research`, `mode=autonomous`
- Manifest contract: `v3`, `tracking_protocol=control-v1`
- Compiled digest: `4ce9f0b3dbf812ca1305e9b54630152f6e2a8babdb84162797821237a49b399b`
- Launch digest: `bfb1bd7a06f5e8fd6897ce779ee3c06a86a56afc1ff894cc2d989160a04e5985`
- Lead run: `b08df330-e4fd-4a19-8d6f-6786671672f1`, OpenAI
  `gpt-5.6-sol`
- Exact local run: `5aff3cf2-b336-4e7c-a83c-242852694ec3`, Ollama
  `gemma3:4b`, `variant=null`
- Local lifecycle: `dispatched -> running -> succeeded`
- Local usage: 443 prompt tokens and 103 completion tokens
- Local artifact:
  `ce27acff65cf9dcc8685c31627464248880577613fe55f09a0a2adcd2873b776`
- Lead artifact:
  `c6631cb1ddce0ab96af717c0f058311c4e5e270465d6ccb77d7a4823e019545c`
- Terminal: `succeeded`; reason `lead result accepted and unified archive verified`

The lead dispatched exactly one `recon` delegation to `triage_scope`, waited on
that exact run ID, retrieved the durable artifact, incorporated its limited
evidence without overstating it, verified the target source directly, called
Fleet Control completion, and emitted the required terminal sentinel.

## Integrity and teardown

- Target branch remained `main` at
  `bd9043401988458e98894e023d15fe33efee71fa` before and after.
- The target working tree remained clean; no writer existed in the workflow.
- The live manifest was removed.
- CMUX returned to the single pre-existing `idle` workspace; no agent pane was
  left behind.
- Archive hashes:
  - manifest: `0ca3729156401d6c4ad484105359ff367a52f1b881536a56928d0d5e9b6b3e07`
  - ledger: `b5adf74b9e62279f5a5587f2188763d158f9015f495beb94d9b022e3e4b4ce8a`
  - state: `2dc1ec27e85ca95fc9f79b578cab24a9b3c1ab15009a6c0122d12467456f46c8`

## Regulated lifecycle canary

The external-compliance workflow cannot honestly pass on this laptop without a
real external S3 Object Lock WORM service. The boot-order lane was therefore
tested below that unavailable boundary with the production Fleet Control daemon
and production interactive wrapper:

- Mission: `0437cef5-a25a-55bf-8b19-755824ff1b63`
- First start: `started=true`, PID `73046`, health identity and
  `fleet-control-unix-v1` protocol verified.
- Second start: `started=false`, same PID `73046`; no duplicate daemon.
- Wrapper-observed perimeter: `regulated`, exact mission/socket identity,
  `FLEET_EFFECT_POLICY=control-only`, and
  `FLEET_FILESYSTEM_PERIMETER=isolated-runtime`.
- Stop: `stopped=true`, durable `stopped_at`, socket absent afterward.
- The canary mission was explicitly terminalized as `abandoned` because it
  intentionally performed no fleet boot or unified archive; it is not presented
  as a completed regulated business mission.

## Fail-closed attempts retained

1. A missing local `hook_source` normalized as `null` instead of the manifest's
   empty string. Binding rejected pre-effect. Central normalization and a
   research regression closed it.
2. The `research` preset inherited `guided`, while canonical Mission execution
   requires autonomous mode. Mission rejected the already-booted fleet; the
   run was terminalized, archived, and torn down. The preset now declares
   `autonomous` and has a contract test.
3. The first v3 mission reached an exact local dispatch, but the command sent to
   the independent worker pane did not carry the isolated `FLEET_RUNS_DIR`.
   The worker rejected leases from the default root. The mission and both runs
   were terminalized, exact leases released, and the workspace archived and
   closed. Dispatch now transports the run root and router path explicitly; a
   regression asserts the exact command envelope.
4. A deliberately overlong regulated socket root was rejected before daemon
   creation by the AF_UNIX path-length guard. The canary then used a short,
   private root and passed.

## Static verification at this gate

- 45 router/workflow/manifest tests: PASS.
- 122 expanded Mission/Fleet Control/local worker tests: PASS.
- Real Unix socket reconciliation test outside the sandbox: PASS.
- Local dispatch run-root regression: PASS.
- Shell syntax and `git diff --check`: PASS.

## Open lane

- Full `regulated.yaml` execution remains deliberately unclaimed until a real
  external-compliance WORM endpoint is configured. Local lifecycle ordering and
  perimeter are verified; external retention compliance is not.
- Multi-host federation remains outside the approved single-Mac S0 scope.
