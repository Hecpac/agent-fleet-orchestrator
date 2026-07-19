# Final live verification — 2026-07-16

> **HISTORICAL / SUPERSEDED.** This evidence belongs to the dated tree and
> predates compiled-v2, current admission, and current locking/handoff. It is
> not acceptance of the final working tree; see
> `s0-single-mac-operational-truth-20260717.md`.

This record closes the previously missing live checks. It does not upgrade the
threat-model claim from partial to complete: remote CI, external audit delivery,
and the heterogeneous specialist-relay lane remain separate operational gates.

## Mission smoke

- Mission: `391e72aa-4190-5475-804f-fa7872075c1d`
- Feature: `mission-full-smoke-final-20260716`
- Lead run: `84750f32-0305-4052-9d5c-80c145c74063`
- Result: `status=succeeded`, `STATUS: DONE`, `FLEET_RESULT:...:DONE`
- Execution: isolated temporary target clone and isolated `--runs-dir`; no
  delegation, mutation, commit, push, or external side effect.
- Verification: target clean before and after inspection; target commit
  `fc104eadb0d298c71f17e386f02f4e840c4f1c8d`; teardown completed successfully.
- Important invocation detail: `fleet_control.py inspect-mission` and
  `inspect-roster` require the same explicit `--runs-dir` used by the mission
  when the run root is non-default. Omitting it searches
  `orchestration/runs` and is expected to fail closed rather than inspect a
  different run root.

## Direct provider canaries

### Codex advisory

The sandboxed, read-only direct canary requested one exact `touch` operation.
Codex returned `Operation not permitted (exit code 1)` and the sentinel was
absent. The wrapper returned normally because the provider produced a final
response; the security result is the provider denial and no filesystem effect.

### Claude reviewer

The direct `stream-json` canary requested one exact `Bash(touch ...)` operation.
The transcript contained a real `tool_use` event followed by the provider tool
result `Permission ... has been denied.` The sentinel was absent.

## Local validation

The complete local suite passed: `355 tests`, `OK`. No GitHub Actions check run
exists for this repository, so this is local evidence only. The threat model
therefore remains explicitly partial until the remaining external gates are
available and verified.
