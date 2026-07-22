# Diagnóstico: por qué la flota erra y los modelos no están en sintonía

> **Fecha:** 2026-07-21 · **Estado:** congelada (entrevista 2026-07-21; tabla de decisiones en §5)
> **Compañeras:** [fusion-harness-propuesta.md](fusion-harness-propuesta.md) (el diseño de la remediación cognitiva) y
> [fusion-cmux-fleet-orchestrator.md](fusion-cmux-fleet-orchestrator.md) (el escenario objetivo).
> Esta propuesta aporta lo que a esas les falta: el **diagnóstico con evidencia local** de por qué
> hoy la flota produce errores, y el orden de remediación que se deriva.

---

## 1. Tesis

Los errores de la flota no son aleatorios: se concentran en **dos capas de desincronía**
que hoy nadie remedia de forma sistémica.

1. **Desincronía mecánica** — cada proveedor CLI es un dialecto (transporte, submit,
   permisos, sesión, schemas), y el harness lo persigue con shims ad-hoc uno por uno.
   Cada release de un CLI rompe algo distinto.
2. **Desincronía cognitiva** — los modelos nunca se fusionan. El kernel coordina
   *ciclo de vida* (ledger, admission, FDP pass/fail) pero jamás *contenido*: no hay
   síntesis, ni panel de opiniones, ni atribución consensus/divergence. La diversidad
   de modelos solo se usa para adjudicar binariamente, no para producir mejor trabajo.

La capa 2 es la que el usuario percibe como "no están en sintonía"; la capa 1 es la que
genera el goteo constante de errores operativos que erosiona la confianza en la flota.

---

## 2. Evidencia (local, verificable)

### 2.1 Capa mecánica: el borde TUI/protocolo es donde muere todo

| # | Error observado | Evidencia |
|---|---|---|
| M1 | Kimi renombró el modelo (`kimi-k3` → `kimi-code/kimi-for-coding`), cambió el banner del TUI (ready_pattern), y CLI 1.11 empezó a validar `--work-dir` como escribible — obligando a la danza chmod+fingerprint sobre el reader clone sellado | commit `6a9af7a` (hoy): router, `fleet-up.sh` |
| M2 | El transporte `inline-json` de Kimi se rompía con prompts multilínea en el composer; hubo que inventar tokens `<<<FDP_NEWLINE_…>>>` | commit `6a9af7a`: `providers/kimi.py` |
| M3 | Re-presionar Enter es seguro en Codex (se traga el primero) pero **inseguro** en Kimi (el Wire bridge reporta con lag y el texto restante se enviaría como segundo prompt) — semánticas de submit opuestas por proveedor | commit `6a9af7a`: `fleet-send.sh`, `tests/test_fleet_send.py` |
| M4 | Kimi valida schemas MCP estricto: enums sin `"type": "string"` fallaban solo con ese consumidor | commit `85261cf` |
| M5 | El shim CLI de cmux consume `CMUX_HOOK_DIR`/`CMUX_EVENTS_LOG` antes de arrancar el proveedor; la evidencia del controller caía a un HOME efímero | commit `e2be25e` |
| M6 | MiniMax/GLM re-bloquean permisos por scope (requieren right+enter+Confirm manual); los surface IDs cambian tras restart de cmux | quirks operativos documentados de la flota frontier |
| M7 | El reviewer local trunca a 768 tokens y rompe el contrato (falta `NEXT_ACTION`); `run-local-worker.sh` no expone `--num-predict` | quirk documentado del carril Ollama |
| M8 | El run `fom2b-kimi-audit-v4` murió en fase CONTROL antes de despachar nada (ledger vacío, state creado 17:59); el v5 idéntico pasó | `orchestration/runs/fleet-fom2b-kimi-audit-v4-20260720.*` |
| M9 | Un socket AF_UNIX demasiado largo abortó el primer intento del re-smoke FOM-2B | `smoke-evidence/fom2b-decision-delivery-20260720.md` |

**Lectura:** M1–M5 son *drift de protocolo por proveedor*: cinco fallos distintos, cinco
shims distintos, un solo día de trabajo (los 3 commits de hoy). No hay un contrato único
que un adaptador deba cumplir — cada quirk se descubre en producción y se parcha a mano.

### 2.2 Capa cognitiva: la diversidad de modelos está desaprovechada

- El harness ya paga el costo de 6 proveedores (anthropic, openai, zai, minimax,
  moonshot-ai, ollama), pero sus outputs **nunca se combinan**. FDP-2/3 adjudica
  VERIFIED/REJECTED; nadie sintetiza "qué dijo cada uno y qué se descarta y por qué".
- El lift documentado que se deja en la mesa (ver propuesta de fusión, hallazgos #1, #3):
  split de roles +3–10 pts, panel+judge 69.0 vs 65.3, self-fusion +6.7 pts.
- Los contratos de prompt divergen por proveedor (pointer / inline-json /
  inline-tokenized): ni siquiera el *input* de los modelos está en sintonía, cada
  dialecto se prueba por separado y muta por separado (M2).
- Cuando la flota sí trabajó bien (audit v5), el valor vino de **un** modelo aislado
  (Kimi encontró el bug de decisiones pendientes en misión terminal, con repro). Nadie
  contrastó ese hallazgo contra una segunda identidad; la confianza descansó en un solo par de ojos.

---

## 3. Remediación (orden derivado del diagnóstico)

### R1 — Contrato de conformance para proveedores (capa mecánica, pequeño)

Un solo documento ejecutable de lo que TODO adaptador debe cumplir, con suite
`tests/test_provider_conformance.py` parametrizada por proveedor:

1. **Transporte único**: payload tokenizado autodescriptivo (generalizar el mecanismo
   `<<<FDP_*>>>` de Kimi a todos los dialectos inline; pointer queda para quien tenga FS).
2. **Submit confirmado**: cada adaptador declara su semántica (`repress_safe: bool`,
   timeout de confirmación) en el router, y `fleet-send.sh` la lee — en vez del `if kimi`
   hardcodeado de hoy.
3. **Schemas estrictos por defecto**: todo enum con `type`, todo objeto cerrado
   (M4 ya lo test-lockeó; extenderlo a contrato general).
4. **Presupuesto local expuesto**: `--num-predict` configurable por rol (cierra M7).

Esto no elimina el drift (los CLIs seguirán cambiando), pero convierte "cinco fallos
sorpresa en producción" en "una suite roja que señala exactamente qué proveedor rompió qué".

### R2 — Capa de fusión F0→F2 (capa cognitiva, el corazón)

Es la propuesta ya escrita en [fusion-harness-propuesta.md](fusion-harness-propuesta.md)
(§7): `opinion` → `fusion` → `auto-validate`. El diagnóstico añade el porqué local:
la flota ya demostró que un solo modelo produce hallazgos valiosos (audit v5); la fusión
convierte eso en el patrón por defecto — dos identidades independientes + síntesis con
atribución — sin la maquinaria completa de FDP-2 para el trabajo diario.

Dependencia real: **F0 consume lo que R1 estabiliza.** Cada run de `opinion` lanza 2–3
proveedores en paralelo; si el borde TUI falla al 10% por proveedor, un run de fusión
de 3 agentes falla ~27% de las veces y la herramienta nace con mala reputación.

### R3 — Doorbells, no verdad (ya es doctrina; mantenerla)

M8 (v4 muerto sin ledger) confirma la regla existente: la completitud se reconcilia por
ledger/artefactos, nunca por pantalla ni eventos. R1 y R2 deben heredarla tal cual
(`fleet-wait` reconcilia por disco; los eventos cmux solo despiertan).

---

## 4. Qué NO propone este documento

- No propone reescribir `fleet-up.sh` ni tocar el kernel de Mission Control.
- No propone majority voting ni conversación entre workers (trampas #4 y #5 de la
  propuesta de fusión).
- No propone perseguir cada quirk de TUI hasta la perfección: R1 los hace *visibles y
  atribuibles*, no imposibles.

---

## 5. Decisiones congeladas (entrevista 2026-07-21)

| # | Bifurcación | Decisión | Consecuencia aceptada |
|---|---|---|---|
| D1 | Orden de slices | **R1 primero, luego F0** | Estabilizar el borde TUI antes de apilar fusión; el valor visible de F0 se retrasa ~1 slice |
| D2 | Alcance de R1 | **Los 4 puntos** (transporte único, submit declarativo, schemas estrictos, `--num-predict`) | M4 y M7 se cierran dentro del contrato, no como parches sueltos |
| D3 | Tier por defecto de F0 | **workhorse** (claude-sonnet-5 + gpt-5.6-terra) | Iteración barata; subir a sota es una línea del router |
| D4 | Smokes en vivo de R1 | **Solo Kimi + Codex** (los de mayor historial de drift) | MiniMax/GLM/Claude quedan cubiertos por la suite de conformance sin smoke manual |

**Precondición del siguiente slice (R1):** un recon nombrado sobre los tres dialectos de
transporte actuales (`pointer`, `inline-json`, `inline-tokenized`) y las semánticas de
submit por proveedor en `fleet-send.sh` / `providers/*.py`, para congelar el contrato
único antes de escribir la suite de conformance.
