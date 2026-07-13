# FDP-1 fleet dialogue live smoke — 2026-07-13

## Verdict

PASS. A fresh cmux fleet running the uncommitted FDP-1 tree relayed one local
result to a frontier checker and then relayed the frontier challenge back to
the local instance through two durable CONTROL-authored messages. Both exact
result payloads remained hash-verifiable after teardown.

## Incarnation and boot

This repository has no long-running application daemon to restart. The runtime
surfaces changed by FDP-1 are the per-invocation Python/shell CLIs and the cmux
agents created by `fleet-up`; restarting an unrelated daemon would not load or
verify them. The smoke therefore adapted `smoke-verify` by booting a completely
new disposable workspace from the current uncommitted tree:

- Feature: `fdp1-dialogue-smoke-20260713`
- Workspace: `workspace:31`
- Workspace UUID: `7990B969-624A-4BAB-B71C-42E26825A90F`
- Instances: `lead` (CONTROL), `local=triage` (RECON), and
  `frontier=codex_candidate` (BUILD)
- `fleet-up` exit: `0`
- Boot time: 8 seconds
- No FDP-1 workspace or surface remained after teardown.

## Live local source

CONTROL advanced `CONTROL → RECON`, dispatched the local `triage` worker, and
waited on the exact mapping:

`local=17ffb8dd-e363-4c65-87e0-20842debb3e5`

The lifecycle terminal was `succeeded`, exit `0`, with a real result file.
Dispatch-to-terminal time was approximately 4.324 seconds
(`15:42:39.766371Z → 15:42:44.090288Z`). The model used 171 prompt tokens and
33 completion tokens.

`fleet_dialogue.py publish` created proposal
`9e211dbc-7e7f-4c1a-b540-92d097a7b102` for the exact `frontier` recipient:

- Source: `local=17ffb8dd-e363-4c65-87e0-20842debb3e5`
- Payload: 136 bytes
- SHA-256: `15e28c29e1f5f23040a3ffdeeb5101d1c6e9db72a33259f807ca3004892cdd13`
- Sender: `CONTROL`
- Kind: `proposal`

The real `read` command reproduced the exact local result before it was passed
explicitly to the frontier worker through `fleet-send`.

## Live frontier source

CONTROL advanced `RECON → BUILD`, sent the relayed proposal to the exact
frontier instance, and waited on:

`frontier=9970977a-eb8b-4de7-a2c1-a12edaeb9b62`

Codex completed with a verified structured sentinel. The lifecycle terminal
was `succeeded`, exit `0`, and now contained the durable `result_file` written
by FDP-1. Dispatch-to-terminal time was approximately 4.708 seconds
(`15:43:05.556473Z → 15:43:10.264Z`). Its exact 135-byte content was:

```text
The relay is structurally valid, and the smoke source was supplied as evidence.

FLEET_RESULT:9970977a-eb8b-4de7-a2c1-a12edaeb9b62:DONE
```

CONTROL published challenge `75374b1b-1340-4a42-b1ab-3878ea5ea91a` back to
the exact `local` recipient with the proposal as its single `reply_to`:

- Source: `frontier=9970977a-eb8b-4de7-a2c1-a12edaeb9b62`
- Payload: 135 bytes
- SHA-256: `d240aad4d50fcdfae891634a40d44e3809879a1d2c7855609adb300d011bdd88`
- Sender: `CONTROL`
- Kind: `challenge`

## Integrity and no-regression checks

- `list` returned exactly two ordered envelopes forming
  `proposal → challenge`.
- `verify` returned `messages=2`, `payloads=2`, and `payload_bytes=271`.
- Retrying the identical challenge with the same idempotency key returned the
  original message ID and did not append a third row.
- Reusing that key with kind `verification` failed closed with exit `75` and
  `idempotency key was already used for another message`.
- An owner-checked live closing marker was acquired after both runs quiesced.
  Publication while it existed failed closed with exit `75` and
  `fleet 'fdp1-dialogue-smoke-20260713' is closing`.
- `fleet-down` exited `0`, closed `workspace:31`, and archived the conversation
  without leaving active workspace/surface identity.

## Durable archive

Archive:

`orchestration/runs/archive/fdp1-dialogue-smoke-20260713-20260713T154340Z/`

It contains manifest, state, lifecycle ledger, `dialogue.jsonl`, and both
content-addressed payloads. An independent post-teardown read recomputed both
SHA-256 hashes and returned `HASH_OK=True` for both messages. Total fresh
boot-to-archive time was approximately 73 seconds.

## Automated checks

- `python3 -m unittest discover -s tests`: 129 passed against the final diff.
- `python3 tests/test_fleet_dialogue.py`: 8 dialogue-specific tests are included
  in that total.
- `bash -n scripts/fleet-down.sh`: passed.
- `git diff --check`: passed.
- The active Python 3.14 environment has no `pytest` package, so the repository's
  native `unittest` runner was used without installing or mutating dependencies.

## Open lanes

- Claude and OpenCode result-file persistence passed unit coverage but were not
  exercised live in this smoke. Close by running the same exact source/publish
  flow once with `claude_reviewer` and once with `minimax` or `glm`.
- The 1 MiB rejection, result symlink rejection, corrupted-payload detection,
  and multiprocess idempotency race passed automated tests but were not
  reproduced against cmux. Close with a disposable synthetic lifecycle ledger
  and a live manifest if an operator-level exercise is required.
- Automatic dispatch, multi-parent synthesis, broadcasts, direct agent writes,
  watchdogs, and bounded dialogue rounds were intentionally excluded from
  FDP-1. They require their own recon and slices.
- Power-loss durability cannot be demonstrated without fault injection. The
  implementation uses file locks, flush, file `fsync`, and directory `fsync`;
  close this lane with a crash-injection test in a disposable filesystem.
