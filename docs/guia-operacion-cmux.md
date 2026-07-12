# Guía paso a paso: trabajar con cmux + fleet

Tu runbook diario. Para el detalle de cada comando ve `.agents/skills/cmux/SKILL.md`
(sincronizada con `.claude/skills/cmux/SKILL.md`);
para el porqué de cada regla, `docs/cmux-best-practices.md`.

---

## 0 · Al sentarte (30 segundos)

```bash
cmux ping        # ¿la app está viva? (PONG)
just status      # radar: ¿hay agentes ⚠️ bloqueados esperándote desde ayer?
```

Si `just status` muestra ⚠️ needsInput, atiende eso primero — es trabajo tuyo
pendiente, no de los agentes. Cierra workspaces zombis que ya no tengan misión
(`just fleet-down <feature>` o la X en el sidebar).

## 1 · Decide si necesitas un fleet

- **"Haz esto"** (tarea acotada, secuencial, verificable con un test) →
  sesión normal de Claude, sin fleet. Es el ~95% de los casos.
- **"¿Qué se me escapa?"** (auditoría, review pre-merge, diagnóstico raro,
  varias subtareas independientes) → fleet.
- **"Producción rota, necesito la respuesta YA"** → carrera (`just race`).

## 2 · Bootea el equipo (1 comando)

```bash
just fleet <nombre> [instance=role...]
# ejemplos:
just fleet-preset audit audit               # roster canónico por capacidades
just fleet-preset research research         # dos triage con IDs únicos
just fleet sse                              # default small: solo lead
just fleet fix build=codex verify=reviewer  # roster explícito
```

El router canónico es `orchestration/router.yaml`: allí viven commands, modelos,
capacidades, permisos declarativos, ranks y presets. Codex es el lead por defecto;
Claude está configurado pero deshabilitado hasta restaurar su entitlement.

Roles frontier: `codex` · `minimax` (MiniMax-M3) · `glm` (GLM-5.2)
Roles locales prompt-only: `code_worker` · `triage` · `light_code` · `reviewer` · `general_worker`
Límite de RAM: **un solo rol local pesado en ejecución** y máximo tres locales concurrentes.

Nota de enforcement: `authority`, `tool_access` y `resource_class` son hoy
declaraciones validadas, no sandboxes ni leases. Hasta el siguiente slice de
seguridad: un solo writer, pesados secuenciales, `tree` antes de cada acción y
la primera finalización de una race se trata únicamente como candidato.

Qué pasa solo: el plan completo se valida antes de tocar cmux, los panes quedan
ordenados `CONTROL → RECON → BUILD → CHALLENGE → VERIFY`, se verifican contra
`tree`, el manifest conserva refs + UUIDs y el **lead se auto-orienta**.

## 3 · Dale la misión al lead

Tres formas equivalentes — elige la cómoda:

- **Directa**: clic en el pane `lead` y escribe la misión.
- **Vía otra sesión** (esta es la potente): en cualquier sesión agéntica del
  repo di *"dile al lead de fleet-audit que audite el diff del PR #N y espera
  su respuesta"* — ella hace send/wait/read por ti.
- **Desde el teléfono**: Remote Control → esa sesión → ella opera los panes.

El lead decide solo a quién despachar (política de delegación en la skill).
Escribe la misión completa de una vez: objetivo, alcance, qué es "done", y
dónde dejar el resultado. Prompt ambiguo × N agentes = horas de cómputo mal
gastadas.

## 4 · Supervisa sin niñear

- Ojo al **sidebar**: colores, banners y notificaciones te dicen quién terminó
  o pide input. `just status` en cualquier momento.
- Entra a cualquier pane a mirar o interrumpir (Escape en agentes, ctrl+c en
  workers). Ver ≠ teclear: se supervisa mirando, se corrige solo si hace falta.
- Nunca esperes con reloj: los agentes esperan con `fleet-wait`; tú con las
  notificaciones.

## 5 · Cosecha resultados

- Resultados grandes viven en **archivos** (pídelo así en la misión: "escribe
  findings a outputs/<x>.md") — las pantallas truncan.
- Cruza perspectivas: coincidencia entre 2 modelos = confianza; desacuerdo =
  ahí hay que excavar; DONE de un modelo chico = verificar antes de creer.
- La síntesis del lead debe marcar claims verificados contra código.

## 6 · Cierra limpio

```bash
just fleet-down <nombre>    # cierra workspace + borra manifest
```

Checklist: ¿el resultado quedó en un archivo/commit/PR? ¿decisiones pendientes
anotadas? ¿`just status` sin ⚠️? Un workspace = una misión; misión cerrada =
workspace cerrado.

---

## Patrones rápidos

**Carrera (hotfix / needle-in-haystack):**
```bash
just race <nombre> "<tarea de 1 línea>" codex minimax triage
# primero en terminar gana, resto se cancela. GANAR ≠ TENER RAZÓN: verifica.
```

**Despacho manual a un worker (sin lead):**
```bash
./scripts/fleet-dispatch.sh <fleet> <instance-id> "<tarea>"
./scripts/fleet-wait.sh <fleet> <instance-id> --timeout 600
cmux read-screen --surface <su surface> --workspace <ws> --lines 50
```

**Esperar a varios / al primero:**
```bash
./scripts/fleet-wait.sh <fleet> codex minimax          # a TODOS
./scripts/fleet-wait.sh <fleet> codex minimax --any    # al PRIMERO
```

## Si algo sale mal

| Síntoma | Remedio |
|---|---|
| Cerraste un pane vivo por accidente | `claude --resume <session-id>` en un pane nuevo (los hooks guardan la sesión); actualiza el manifest |
| Ref del manifest no coincide | `cmux tree --workspace <ws>` es la verdad; las refs son posicionales — NUNCA cierres sin verificar en tree |
| Worker frontier no lanza | El preflight debe fallar antes de crear el workspace. Revisa el command/env declarado en `router.yaml`. |
| codex se come el prompt al bootear | Diálogo de update pendiente: `npm install -g @openai/codex` |
| fleet-wait nunca dispara | ¿hooks instalados? `cmux hooks setup`. ¿El agente ya había terminado ANTES de armar el wait? Arma el listener antes de despachar |
| No sabes qué agente es quién | `cmux tree --all` + el manifest del fleet |

## Reglas de oro (las 5 que no se negocian)

1. `tree` antes de tocar cualquier ref — y jamás cierres lo que no verificaste.
2. Panes para VER, archivos para DATOS, eventos para ESPERAR.
3. El lead verifica los claims de los workers contra código, siempre.
4. Un rol pesado local a la vez (16 GB); teardown al cerrar la misión.
5. `just status` al llegar y al irte — tú eres el cuello de botella del
   sistema, no los agentes.
