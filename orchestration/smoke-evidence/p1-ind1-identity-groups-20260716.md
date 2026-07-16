# P1-IND1 identity groups — live smoke (2026-07-16)

## Objective

Verify that declared identity diversity is rejected before CMUX effects when a
group repeats a configured `(provider, model, variant)` tuple, and that a valid
group survives into a live fleet manifest beside the exact member identities.
The smoke also checks that two identity-distinct frontier reviews remain
ordinary evidence rather than being promoted to semantic truth.

Versions exercised:

- Codex CLI `0.144.5`
- OpenCode `1.18.0`
- CMUX `0.64.19 (99) [1c22c5564]`

No daemon reload was needed: router validation and `fleet-up.sh` load the
current repository files for each invocation, and the valid path booted a new
workspace.

## Negative pre-effect probe

An in-memory copy of the current router was changed so
`defaults.race_roles` contained `codex_candidate` twice and was passed to the
real `router_config.validate_router` entrypoint. It failed in `0.03s` with:

```text
router.defaults.race_roles repeats provider/model/variant identity openai/gpt-5.6-sol/- for codex_candidate and codex_candidate
```

`cmux tree --all` was captured immediately before and after the probe and was
unchanged. The duplicate identity therefore failed before any workspace or
surface effect.

## Valid tracked fleet

Feature `p1-ind1-smoke-20260716` used the `audit` preset in workspace UUID
`7A4F17A7-B5C8-4CBE-B1F1-C1134D2645CE`. The live manifest contained:

```text
identity_group.count=1
identity_group.1=analysis,challenge,verify
```

The declared group resolved to three different configured identities:

| Instance | Provider/model/variant | Surface UUID |
|---|---|---|
| `analysis` | OpenAI / `gpt-5.6-sol` / `-` | `9F5CF6E8-9F0E-41B1-A97C-2E80621DD92E` |
| `challenge` | MiniMax / `MiniMax-M3` / `none` | `C112B255-54D7-4593-AA73-7EA59260B199` |
| `verify` | Ollama / `code-auditor:latest` / `-` | `E557A9E0-86A8-4A63-8403-FFC6691B39CA` |

The manifest workspace and surface UUIDs matched the live CMUX tree before
dispatch and again immediately before teardown.

## Identity-distinct reviews

CONTROL advanced the phase gates explicitly and dispatched both frontier
reviews through `fleet-send.sh`; `fleet-wait.sh` consumed the event-backed
completion evidence.

1. Codex run `313b7538-7c2e-4bc6-92d7-c75f648d5cb9` terminalized
   `succeeded`, exit `0`, reason `frontier_sentinel_verified`, and returned
   `ACCEPT` with no findings. Result SHA-256:
   `105bace7509e89282faff8925d5edcf9f1b15ce879bc50ecc386282a07cc3bbd`.
2. MiniMax run `21d5f875-ecfd-4215-a820-f60e82076eba` terminalized
   `succeeded`, exit `0`, reason `frontier_sentinel_verified`, and also
   returned `ACCEPT`. Result SHA-256:
   `8c4465fcaf31e8bc3d9515662a2c0bb2a542a3fbe92db0ed96f4e1e954e2382f`.

MiniMax included an informational claim that
`.opencode/agents/glm-challenger.md` did not exist. Direct repository
inspection disproved it: the file exists and contains the hardened JSON-only
challenger contract. No implementation change was made for that false
positive. This is useful negative semantic evidence: a tracked, successful,
identity-distinct reviewer can still emit a plausible factual error.

## Regression and teardown

- Full repository suite: **347/347 passed in 79.041s** with
  `python3 -m unittest discover -s tests -v`.
- Focused router, workflow, assurance, launcher, and threat-model suites also
  passed before the full run.
- Router validation, generated record inspection, Bash syntax, Python
  compilation, synchronized CMUX skill copies, and `git diff --check` passed.
- `fleet-down.sh` closed only workspace `workspace:25`. CMUX returned to the
  original `idle` workspace; the active manifest and lock directory were
  absent.
- Archived ledger:
  `orchestration/runs/archive/p1-ind1-smoke-20260716-20260716T192637Z/ledger.jsonl`
  (SHA-256
  `9918465b0f594327ad2b96a4d3953a7f5964b4c7d1b034346ad1b1ecddc3f703`).
- Archived manifest SHA-256:
  `dc040c12d7767b87ec75c155e6dae677acfb05271ce02f5aaedc1d9d93b39c46`.

## Verdict

**PASS for P1-IND1.**

The duplicate default-race identity failed before CMUX mutation, while the
valid group was present in the live manifest beside three exact, distinct
configured identities. Both frontier completions remained bound to their run,
provider session, and surface UUID. The MiniMax false positive demonstrates
that this slice correctly avoids equating identity diversity with correctness.

This smoke does not claim training-lineage independence, statistically
uncorrelated errors, or semantic truth from agreement. It did not run a full
FDP-2/FDP-3 assurance chain live; FDP-3 variant mismatch is covered by a
deterministic controller test. A custom same-model race was not launched live;
its candidate-only label and absence of assurance promotion are covered by the
threat-model test.
