# Slice 2D — Codex/Claude structured transcript evidence

Date: 2026-07-12 CDT / 2026-07-13 UTC

Base: `ee78a4880cb51d56f8dd0e9cb23545f08918b848`

Tested HEAD: `43c4539`

## Live boot

`smoke-verify` restart step was adapted because this repository has no daemon or
port. The production incarnation is a cmux fleet launched by `fleet-up.sh` from
the committed `main` tree. `cmux ping` returned `PONG`; boot published
`fleet-slice2d-final-smoke` as workspace `workspace:25`, UUID
`049E3B48-228A-4ED6-9B57-9D7914F8ED87`. Manifest identities were:

- Codex candidate: `openai` / `gpt-5.6-sol` / `codex`
- Claude: `anthropic` / `claude-fable-5` / `claude`
- Claude reviewer: `anthropic` / `claude-fable-5` / `claude`

All three interactive clients reached their real idle prompts. Codex emitted
pre-existing local MCP/agent-definition warnings; neither Claude process nor the
fleet launcher emitted a launch error.

## Acceptance runs

Every row below came from the durable frontier ledger and ended
`frontier_sentinel_verified` with no `lease_retained` field.

| Role | Run | Session | Round-trip | Result |
|---|---|---|---:|---|
| Codex | `623e12e7-1628-4832-b7fb-7f2e30a1aa52` | `codex-019f58cb-ad68-7962-95a1-229af34ee1ca` | 4.256 s | succeeded |
| Codex | `3c66ae73-d69a-4f93-a2cd-5bc488304043` | `codex-019f58cb-ad68-7962-95a1-229af34ee1ca` | 2.717 s | succeeded |
| Claude | `a7ac3fed-3785-40be-8430-3d086cedbf3d` | `claude-37f14a57-a9cb-4462-83b4-d35dc8589a8f` | 6.909 s | succeeded |
| Claude | `dc1990c6-6940-44da-89da-07add3f9f102` | `claude-37f14a57-a9cb-4462-83b4-d35dc8589a8f` | 5.208 s | succeeded |
| Claude reviewer | `a9a6679e-34ae-4234-b263-29fb42d751bb` | `claude-be58cac7-271f-4b5f-9f3e-dd8b59d5f091` | 10.693 s | succeeded |
| Claude reviewer | `fdac5ef1-9645-4a2c-86ff-857753e41c05` | `claude-be58cac7-271f-4b5f-9f3e-dd8b59d5f091` | 7.895 s | succeeded |

The two runs per required role reused exactly one source-prefixed session. The
ledger persisted the expected provider/model/source on every run. The fleet lock
directory contained no `slice2d-final-smoke.*` lease after the successful runs.

## Explicit no-regression

The pre-change Claude probe `2ee4c91f-6785-4066-9646-4c10fab16415` reproduced
the original failure as `frontier_sentinel_not_final` despite a valid final
sentinel, because real chrome contained timing text, separators, and the
permission bar. After Slice 2D, both Claude roles completed without consulting
that chrome, despite receiving duplicate completed Stops.

The first post-change Claude run exposed a bounded file-visibility race and
failed closed as `frontier_claude_evidence_unavailable`; commit `14de416`
introduced four bounded structured reads at 100 ms intervals. Both required
Claude runs then passed.

Independent review run `cd5868a9-73b4-48be-adda-506f2c5556c4` used tools and
exposed that Claude serializes `tool_result` as top-level `type=user`. Commit
`43c4539` treats those rows as continuations of the bound human turn and excludes
sidechains. Live run `42cdcb1c-d1f0-4431-b7ad-e55c6ad861c9` then executed one
shell command, observed HEAD `43c4539`, and completed `succeeded` in 13.240 s.

The independent follow-up review run
`7de9d812-b03e-4600-946e-3942999a1f2e` completed `succeeded` and reported PASS
for both the tool-result boundary and sidechain fixes. It also ran all 29
frontier tests successfully.

## Automated verification

- `python3 -m unittest discover -s tests`: 114 tests, PASS.
- `python3 -m unittest tests.test_fleet_frontier`: 29 tests, PASS.
- `python3 scripts/router_config.py validate`: schema 3 valid.
- `python3 -m py_compile scripts/fleet_frontier.py scripts/router_config.py`: PASS.
- `bash -n scripts/fleet-up.sh`: PASS.
- `git diff --check`: PASS before each commit.

## Open lanes

- OpenCode structured completion was not re-smoked because Slice 2D did not
  modify its evidence reader; its existing unit/integration contract passed.
- `BLOCKED` and `FAILED` sentinels were not sent to paid Codex/Claude sessions;
  structured status mapping is covered by deterministic tests.
- A live Codex tool-using turn was not induced; the independent reviewer checked
  a real Codex rollout and confirmed tool outputs are not user-message rows.
- A run dispatched before identity fields existed will fail closed and require
  explicit operator abandonment; Slice 2D intentionally preserves the earlier
  legacy rejection decision rather than migrating in-flight legacy state.
- `SessionEnd` without Stop remains the named post-Slice-2D recon and was not
  added to this slice.

# impl-notes: Slice 2D structured Codex/Claude completion

## Spec anclado
Tabla congelada de la entrevista pre-Slice 2D: transcripción para ambos clientes,
modelos fijados, allowlist estricta y dos turnos vivos por rol.

## Log de desviaciones

| # | Dónde (qué parte del spec) | La desviación | Clasificación | Decisión conservadora / A quién regresa | Reversible? |
|---|---|---|---|---|---|
| 1 | Identidad Claude | La transcripción no declara provider | REGISTRA-Y-SIGUE | Validar `claude → anthropic` en router/source y el modelo en transcript | sí |
| 2 | Lectura estructurada | El primer Stop puede adelantarse a la visibilidad de la fila final | REGISTRA-Y-SIGUE | Reintento corto, constante y acotado; nunca fallback a chrome | sí |
| 3 | Binding de turno Claude | Los `tool_result` aparecen como filas `type=user`; sidechains comparten sessionId | REGISTRA-Y-SIGUE | Delimitar por prompts humanos del carril principal y excluir sidechains | sí |

## Detenido, esperando resolución
ninguno.

## Tres para el intento #2
1. Incluir desde el primer test una transcripción Claude con tool calls y sidechain.
2. Probar la visibilidad de la fila final bajo el primer Stop, no solo después del idle.
3. Ejecutar la revisión independiente antes del smoke corto para ampliar antes el corpus.

## Estado
Construcción completa y sin DETÉN abierto → listo para slice-gate.
