# Slice 2E — Claude SessionEnd without Stop

Date: 2026-07-12 CDT / 2026-07-13 UTC

Verdict: PASS

## Runtime adaptation

This repository has no daemon or web port to restart. The production surfaces
are fresh cmux fleets, `fleet-send`, and `fleet-wait`; the disposable fleets
loaded the uncommitted working-tree scripts in new processes. `cmux ping`
returned `PONG`, each Claude client reached its real prompt, and all disposable
workspaces were closed after evidence capture. The original workspace remained
the only workspace after cleanup.

## Primary live path: bound SessionEnd without Stop

- Fleet: `slice2e-sessionend-live`, workspace `workspace:27`, UUID
  `A0D843DB-6AD9-4324-8111-61E23C408EFE`.
- Run: `a86340f1-d3ab-4838-bb56-57f5cb350a76`.
- Session: `claude-136707cc-204b-4b33-b603-bee436e50320`.
- Identity: `anthropic` / `claude-fable-5` / `claude`.
- `UserPromptSubmit received`: sequence 4970 at `00:39:37.709Z`.
- Claude was terminated directly while executing `sleep 120`, without sending
  another UI input.
- No Stop event exists between binding and exit.
- `SessionEnd received`: sequence 4977 at `00:39:49.675Z`.
- `SessionEnd completed`: sequence 4979 at `00:39:49.675Z`.
- The waiter returned exit code 5 with `status=indeterminate`.
- The ledger completion event is exactly sequence 4979 with reason
  `frontier_session_ended_without_stop` and `lease_retained=true`.
- The owned lease still named the exact run after terminalization.
- Submit-to-terminal round trip: 11.966 seconds.
- Explicit `fleet-abandon.sh` released the retained lease only after inspection;
  `fleet-down.sh` then closed the workspace.

Durable ledger evidence is archived at
`orchestration/runs/archive/slice2e-sessionend-live-20260713T004005Z/ledger.jsonl`.

## Explicit no-regression: normal Stop remains authoritative

- Fleet: `slice2e-sessionend-normal`, workspace `workspace:28`, UUID
  `C6C66D27-525C-4ED0-BC8B-E2C6A6CA7C31`.
- Run: `777e9168-8bef-4a68-9ad2-2bffcaa5819a`.
- Session: `claude-cf3487a7-77fe-4999-8455-2fd0bd37ff72`.
- `UserPromptSubmit received`: sequence 5007 at `00:40:12.827Z`.
- `Stop completed`: sequence 5016 at `00:40:19.956Z`.
- The waiter returned exit code 0; the ledger recorded `succeeded` with
  `frontier_sentinel_verified`, and the lease was absent.
- Submit-to-terminal round trip: 7.129 seconds.
- Teardown later emitted `SessionEnd completed` at sequence 5033. The archived
  first terminal remains the earlier successful Stop at sequence 5016.

Durable ledger evidence is archived at
`orchestration/runs/archive/slice2e-sessionend-normal-20260713T004035Z/ledger.jsonl`.

## Failed smoke attempt retained as diagnostic evidence

The first attempt used repeated terminal `ctrl+c` input. Claude emitted a second
`UserPromptSubmit` before SessionEnd, so the pre-existing ambiguity guard
correctly terminalized `frontier_session_binding_ambiguous` first. This did not
exercise the new branch. The attempt was rejected as primary evidence, its lease
was explicitly released, and its workspace was closed. Its ledger remains at
`orchestration/runs/archive/slice2e-sessionend-smoke-20260713T003930Z/ledger.jsonl`.

## Automated verification

- Five directed invariant tests: PASS.
- `python3 -m unittest discover -s tests`: 118 tests, PASS.
- `python3 -m py_compile scripts/fleet_frontier.py scripts/fleet_wait.py`: PASS.
- `python3 scripts/router_config.py validate`: schema 3 valid.
- `git diff --check`: PASS.

## Open lanes

- Cross-boot audit recovery of SessionEnd was test-locked but cmux was not
  restarted during a paid turn. Close with a disposable cmux restart drill.
- Unbound, foreign-session, foreign-source, foreign-workspace, and received-only
  SessionEnd rejection were exercised deterministically, not by injecting fake
  events into the live cmux audit stream.
- Codex and OpenCode were not re-smoked because the new terminal branch is
  explicitly Claude-only; their full unit suite passed and the subscription
  change is additive.

# impl-notes: Slice 2E Claude SessionEnd without Stop

## Spec anclado

Tabla congelada de la entrevista pre-Slice 2E: SessionEnd completed ligado debe
fallar cerrado, retener lease, preservar Stop previo y funcionar en stream/audit.

## Log de desviaciones

| # | Dónde (qué parte del spec) | La desviación | Clasificación | Decisión conservadora / A quién regresa | Reversible? |
|---|---|---|---|---|---|
| 1 | Smoke: dispatch real | La flota custom inicia en CONTROL y Claude está en BUILD | REGISTRA-Y-SIGUE | Avanzar con el phase gate durable y evidencia del Green Light | sí |
| 2 | Smoke: inducir salida | `ctrl+c` produjo un segundo UserPromptSubmit y disparó primero el guard de binding ambiguo | REGISTRA-Y-SIGUE | Rechazar ese intento como evidencia y terminar directamente el proceso Claude en una flota nueva | sí |

## Detenido, esperando resolución

ninguno.

## Tres para el intento #2

1. Inducir SessionEnd con señal directa al proceso para no crear input de UI.
2. Comprobar la ausencia de Stop antes de aceptar el smoke primario.
3. Capturar el ledger y el lease antes del abandono y teardown explícitos.

## Estado

Construcción completa y sin DETÉN abierto → listo para slice-gate.
