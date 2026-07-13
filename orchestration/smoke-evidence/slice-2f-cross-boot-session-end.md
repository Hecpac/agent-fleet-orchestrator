# Slice 2F — exact cross-boot SessionEnd boundary

Date: 2026-07-12 CDT / 2026-07-13 UTC

Verdict: PASS

## Scope

Slice 2F is test-only. It changes no runtime file. Its purpose is to lock the
exact boundary that Slice 2E's broader recovery test did not exercise:
UserPromptSubmit binds the Claude run in one cmux boot and SessionEnd completes
in the next boot.

## Deterministic boundary evidence

`tests/test_fleet_frontier.py::FleetFrontierTests::test_cross_boot_audit_recovers_claude_session_end_without_stop`
now constructs this contiguous audit:

- dispatch baseline: `boot-old`, sequence 900;
- bound UserPromptSubmit: `boot-old`, sequence 901 at `00:00:01Z`;
- completed SessionEnd: `boot-new`, sequence 1 at `00:00:02Z`.

The recovered terminal event is `indeterminate` with reason
`frontier_session_ended_without_stop`, `completion_boot_id=boot-new`,
`completion_seq=1`, and `lease_retained=true`; the owned lease remains present.
The test passed without any runtime modification, confirming the existing
timestamp fallback in `_event_after_binding` handles the boot boundary.

## Verification

- Exact directed test: 1 test, PASS.
- `python3 -m unittest discover -s tests`: 118 tests, PASS in 34.698 seconds.
- `git diff --check`: PASS.

## Live-smoke adaptation

No new live smoke was run because Slice 2F changes no runtime behavior. The real
Claude event shape, waiter exit 5, indeterminate reason, and retained lease were
already verified in Slice 2E and remain recorded in
`orchestration/smoke-evidence/slice-2e-claude-session-end.md`. Repeating that
same-boot paid turn would not exercise the newly locked boot boundary.

## Open lane

A full cmux application restart remains an operational resilience drill, not a
missing runtime assertion. It requires an isolated cmux instance and an external
supervisor that survives the restart; it must not be forced on the primary
workspace. The deterministic frontier boundary is now exact and test-locked.

# impl-notes: Slice 2F exact cross-boot SessionEnd boundary

## Spec anclado

Tabla congelada de la entrevista pre-Slice 2F: test-only para binding en boot
viejo y SessionEnd en boot nuevo; runtime prohibido salvo nuevo recon.

## Log de desviaciones

| # | Dónde (qué parte del spec) | La desviación | Clasificación | Decisión conservadora / A quién regresa | Reversible? |
|---|---|---|---|---|---|
| — | — | ninguna | — | — | — |

## Detenido, esperando resolución

ninguno.

## Tres para el intento #2

1. Fijar explícitamente qué evento queda a cada lado del cambio de boot.
2. Afirmar `completion_seq=1`, no solo el identificador del boot nuevo.
3. Ejecutar primero el test dirigido para detectar cualquier necesidad de volver a recon.

## Estado

Construcción completa y sin DETÉN abierto → listo para slice-gate.
