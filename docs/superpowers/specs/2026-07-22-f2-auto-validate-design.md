# F2 `auto-validate` — gate-first build loop sobre el fusion harness

> **Fecha:** 2026-07-22 · **Estado:** aprobado (diseño presentado y autorizado)
> **Fuentes congeladas:** `docs/fusion-harness-propuesta.md` §4.3 (reglas
> no-negociables portadas del original) + entrevista Q1/Q2 de hoy.

## 1. Objetivo

`just auto-validate "<tarea>"`: un VALIDATOR escribe un gate ejecutable ANTES
de que exista el trabajo; el baseline debe fallar RED; un BUILDER con
escritura real itera hasta 5 rondas contra las líneas FAIL del gate; un
TRIAGE read-only diagnostica desde la ronda 3 y puede gastar el único repair
del gate. Verde del gate = hecho. Halt ruidoso, nunca loop silencioso.

## 2. Decisiones congeladas

| # | Decisión | Valor |
|---|---|---|
| Q1 | Workspace del builder | **Aislado fresco**: `outputs/fusion/<run_id>/autovalidate/workspace/`; el repo jamás se toca; `--target-repo` diferido a F2.5 |
| Q2 | Memoria rondas 2+ | **Resume nativo + fallback stateless**: ronda N resume la sesión del builder; si la captura del session-id falla, esa ronda re-promptea con historial FAIL acumulado y se registra `memory: "stateless-fallback"` — nunca un abort |
| D-arch | Organización | Nuevo módulo `scripts/fusion/auto_validate.py` (~300 líneas) importando de `fusion_harness` (`run_agent`, `output_root`, `_agent_summary`, `TIERS`, `FusionError`); el harness solo gana el subcomando que delega. Regla <800 líneas por archivo se mantiene en ambos |
| D-gate | Sellado del gate | Hash sha256 del gate tras VALIDATOR (copia sellada `gate.py.sealed`); antes de CADA `uv run`, el harness restaura desde la copia sellada y registra `gate_tampered: true` en la ronda si el builder lo tocó |
| D-mem | Captura de session-id | El flag/formato exacto del CLI se calibra en la implementación con una invocación real (los shims lockean el contrato); el fallback de Q2 cubre cualquier fallo de captura |

Reglas no-negociables portadas (propuesta §4.3): errores del harness (uv
ausente, timeout del runner, session-id no capturado) **jamás se cobran como
ronda**; el re-run tras gate repair es **gratis**; repair del gate **una sola
vez** (`gate.py.rN` preservado); kill SIGTERM→SIGKILL(5s) (ya en
`run_agent`); máximo 5 rondas.

## 3. Actores (tier workhorse por defecto)

| Rol | CLI/modelo | Permisos | Invocación |
|---|---|---|---|
| VALIDATOR | architect (`claude -p`, claude-sonnet-5) | escribe SOLO `gate.py` | cwd = dir del gate; tras la corrida el harness verifica que exactamente `gate.py` fue creado (cualquier otro archivo nuevo → harness_error, run inválido, exit 5) |
| BUILDER | builder (`codex exec -s workspace-write -C <workspace>`, gpt-5.6-terra) | escritura al workspace aislado | ronda 1: tarea + gate completo; ronda N: resume + output verbatim del gate (+ triage si existe) |
| TRIAGE | architect (`claude -p`, read-only) | ninguno | ronda ≥3, tras cada FAIL; puede emitir `TRIAGE_VERDICT: GATE_DEFECT` |
| Gate runner | `uv run gate.py` (PEP 723) | — | timeout 60 s (`FLEET_GATE_TIMEOUT`); exit 0 ⟺ hecho; salida = líneas `PASS:` / `FAIL:` |

## 4. Máquina de estados

```
1. VALIDATOR → gate.py            (falla → exit 5; archivo extra → exit 5)
2. Sella: gate.py.sealed + sha256
3. BASELINE: uv run gate.py       (exit 0 → warning "gate débil o trabajo
                                   ya hecho" → exit 6; harness_error → no
                                   se cobra, reintento único → exit 2)
4. rondas n = 1..5:
   a. BUILDER (resume si n>1 y hay session-id; si no, fallback stateless)
   b. restaurar gate desde sealed; comparar hash; registrar tampering
   c. uv run gate.py
      - exit 0 → status "green", exit 0
      - FAIL → guardar fail_lines; si n ≥ 3 → TRIAGE
   d. TRIAGE == GATE_DEFECT y repairs == 0:
      - VALIDATOR reescribe gate (preserva gate.py.rN, resella)
      - re-run del gate GRATIS (no consume ronda; el builder no corre)
   e. harness_error en a-c → registrar, repetir la ronda (no se cobra);
      2 harness_errors consecutivos → exit 2
5. tras 5 rondas sin verde → halt ruidoso, exit 7
```

**Exit codes**: 0 verde · 2 uso/harness · 5 VALIDATOR inválido · 6 baseline
no-RED · 7 halt tras 5 rondas.

## 5. Artefactos

`outputs/fusion/<run_id>/autovalidate/`: `gate.py`, `gate.py.sealed`,
`gate.py.rN` (si hubo repair), `workspace/`, `rounds/round-N.md`,
`triage-N.md`, `baseline.txt`, `summary.json` + línea canónica en
`outputs/fusion/ledger.jsonl`.

`summary.json`: `{schema_version: 1, command: "auto-validate", run_id, tier,
task_sha256, gate_sha256, template_hashes: {validator, builder_round,
triage}, status: "green"|"halted"|"baseline-not-red"|"invalid-validator"|
"harness-error", gate_repairs: 0|1, rounds: [{n, memory:
"resume"|"stateless-fallback"|"first", gate_verdict: "pass"|"fail",
fail_lines: [...], triage_verdict: null|"BUILDER_DEFECT"|"GATE_DEFECT",
gate_tampered: bool, harness_errors: [...]}], agents: [...]}`.

## 6. Templates (verbatim; en `scripts/fusion/prompts/`)

### validator.md

```text
# VALIDATOR

You write an executable acceptance gate BEFORE any work exists. The gate is
the contract: exit 0 if and only if the task is genuinely complete.

Task the gate must verify:

{{TASK}}

Write EXACTLY ONE file named gate.py in your current directory. Rules:
- PEP 723 inline-metadata script (uv run gate.py must work standalone);
  declare any dependency in the script header, prefer stdlib only.
- The gate inspects the workspace directory passed as argv[1].
- Print exactly one line per check: "PASS: <what held>" or
  "FAIL: expected <X>, found <Y>, at <path> — <fix hint>".
- Exit 0 only when every check passes; nonzero otherwise.
- Deterministic: no network, no clock dependence, no randomness, no side
  effects on the workspace; finish well under 60 seconds.
- The gate must FAIL on an empty workspace (the baseline run proves it).
- Do not implement the task itself. Do not write any other file.
```

### builder_round.md

```text
# BUILDER — round {{ROUND}} of {{MAX_ROUNDS}}

{{TASK_BLOCK}}

Your working directory is the workspace; build the task there. The
acceptance gate below is VISIBLE but IMMUTABLE — the harness restores a
sealed copy before every run, so editing it cannot help you and is
recorded. Your work is done only when "uv run gate.py <workspace>" exits 0.

<gate>
{{GATE_SOURCE}}
</gate>

{{ROUND_FEEDBACK}}
```

(`{{TASK_BLOCK}}` = la tarea completa en ronda 1 o modo stateless; en modo
resume, una línea de continuación. `{{ROUND_FEEDBACK}}` = vacío en ronda 1;
después: output verbatim del gate y, si existe, el diagnóstico de TRIAGE.)

### triage.md

```text
# TRIAGE — read-only diagnosis, round {{ROUND}}

The builder has failed the gate {{ROUND}} times. Diagnose the ROOT CAUSE by
inspecting the actual workspace state — verify against files, do not trust
either the builder's claims or the gate's phrasing.

Task: {{TASK}}

<gate>
{{GATE_SOURCE}}
</gate>

Latest gate output:

{{GATE_OUTPUT}}

Workspace to inspect (read-only): {{WORKSPACE}}

Answer with your diagnosis, then EXACTLY ONE final line:
TRIAGE_VERDICT: BUILDER_DEFECT — <one-line direction for the builder>
or
TRIAGE_VERDICT: GATE_DEFECT — <what the gate checks wrongly and why>
Declare GATE_DEFECT only when the gate itself checks something the task
never asked for or checks it incorrectly — not merely because passing it
is hard.
```

## 7. Tests (criterio de done; shims + gates de juguete con uv real)

1. Verde en ronda 1 (builder shim escribe el archivo pedido; gate real
   simple) → exit 0, summary green, 1 ronda.
2. Verde en ronda 3: FAIL lines de las rondas 1-2 aparecen en el prompt de
   la ronda siguiente (fixture por-llamada, patrón F1).
3. Baseline no-RED (gate que pasa vacío) → exit 6, sin rondas.
4. Halt tras 5 rondas → exit 7, 5 entradas en rounds, ledger escrito.
5. GATE_DEFECT en ronda 3 → repair único: `gate.py.r3` preservado, re-run
   gratis (el builder NO corre entre repair y re-run), `gate_repairs: 1`;
   un segundo GATE_DEFECT no repara (repair agotado → sigue como
   BUILDER_DEFECT).
6. Harness error (uv simulado ausente / gate timeout) → la ronda no se
   cobra; 2 consecutivos → exit 2 con summary "harness-error".
7. Gate tampering: builder shim reescribe gate.py → el harness restaura la
   copia sellada (el gate corre el original) y `gate_tampered: true`.
8. VALIDATOR escribe un archivo extra → exit 5, run inválido.
9. Fallback de memoria: sin session-id capturable → ronda corre stateless y
   `memory: "stateless-fallback"` queda en summary.
10. Ledger/summary escritos exactamente una vez en TODOS los caminos
    terminales (0/5/6/7 y el harness-error doble).
11. Smoke en vivo (cierre): una tarea real pequeña pasa el gate en ≤5
    rondas con evidencia del contrato completo.

## 8. Fuera de scope

`--target-repo` (F2.5), panel local, tier sota por defecto, integración
Mission Control, generalización del saneo de env a variables hermanas.
