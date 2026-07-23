# Smoke en vivo: F2 `auto-validate` — gate-first loop con CLIs reales

Encarnación: `main@f4d3be3` (worktree limpio). Corrida real tier workhorse,
`ANTHROPIC_API_KEY` saneada por el harness, `FLEET_FUSION_TIMEOUT=240`.

## Veredicto: **PASS**

Run `748043a2`, `status=green` en **ronda 1**, exit 0, ~2.5 min wall:

| actor | status | latencia |
|---|---|---|
| validator (claude-sonnet-5, acceptEdits, cwd=av_dir) | ok | 118.3 s |
| builder-r1 (gpt-5.6-terra, workspace-write) | ok | 26.4 s |

## Contrato verificado punto por punto

- **El VALIDATOR escribió un gate real de calidad**: PEP 723 (`requires-python
  >=3.8`, deps vacías), 5 checks granulares (existencia, exit 0, 10 líneas,
  parse a enteros, secuencia exacta), formato `FAIL: expected X, found Y, at
  <path> — <hint>` exacto, y NO implementó la tarea (enforcement recursivo
  exactly-one-file pasó).
- **Baseline genuinamente RED** (`baseline.txt`): el gate falló sobre el
  workspace vacío con el formato del contrato.
- **El BUILDER construyó en el workspace aislado**: `primes.py` imprime
  exactamente los primeros 10 primos; el repo jamás se tocó.
- **Re-run independiente del gate por el operador**: 5×PASS, exit 0 —
  determinista y reproducible fuera del harness.
- **Summary/ledger exactly-once**: schema completo (gate_sha256,
  template_hashes ×3, rounds con memory/tampered, agents con latencias),
  línea canónica en `outputs/fusion/ledger.jsonl`.

## Calibración D-mem observada

`codex exec` NO emite session-id en stdout con los flags actuales → la
captura quedó vacía y un run multi-ronda tomaría `stateless-fallback` en las
rondas 2+ — **el comportamiento diseñado exactamente para esto** (degrada,
registra en summary, nunca aborta). Calibrar el resume nativo (p. ej.
`codex exec --json` o parseo de stderr) queda como refinamiento cuando un
run real multi-ronda lo amerite; el contrato de fallback está test-lockeado.

## Carriles NO probados en vivo (cubiertos por tests con shims)

1. **Rondas 2+ reales** (el smoke pasó en ronda 1): FAIL-feed, triage,
   GATE_DEFECT + repair único, halt a 5 — todos test-lockeados (29 tests).
2. **Resume nativo del builder** — bloqueado por la calibración de arriba.
3. **Tampering del gate por un builder real** — sellado test-lockeado.
4. **run_gate sin process-group hardening** (nota de review): un gate que
   spawnee descendientes desprendidos podría burlar el timeout de 60 s; el
   contrato del validator (determinista, sin side effects) lo acota.
5. Convención `exit 127 → harness-error` añadida durante la implementación:
   pendiente una línea en el spec (registrada aquí como addendum vinculante).

## Ejecución del slice (registro)

5 tareas por subagentes con doble revisión c/u; hallazgos reales de reviews:
enforcement recursivo del validator + ignore de droppings CLI, triage
last-match, bug de identidad dual de módulo (`sys.modules.setdefault`),
regla never-charged en el re-run del repair, helper único de harness-error,
y tres candados de accounting. 53 tests en verde total (29 auto-validate +
23 fusion-harness + compileall); 451/561 líneas (<800).
