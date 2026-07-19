# Referencia complementaria: observar la flota en CMUX

La guía canónica de selección, ejecución, completion, recuperación, FDP-2/FDP-3,
WORM y límites de confianza es [`guia-uso-flota.md`](guia-uso-flota.md). Este
archivo conserva únicamente la referencia visual y los patrones cotidianos de
CMUX. Para el detalle agéntico consulta `.agents/skills/cmux/SKILL.md`
(sincronizada con `.claude/skills/cmux/SKILL.md`); para el porqué de cada regla,
consulta [`cmux-best-practices.md`](cmux-best-practices.md).

---

## 0 · Al sentarte (30 segundos)

```bash
cmux ping        # ¿la app está viva? (PONG)
just status      # radar: ¿hay agentes ⚠️ bloqueados esperándote desde ayer?
```

Si `just status` muestra ⚠️ needsInput, atiende eso primero — es trabajo tuyo
pendiente, no de los agentes. Cierra workspaces zombis que ya no tengan misión
con `just fleet-down <feature>`; la X del sidebar omite la reconciliación.

## 1 · Decide si necesitas un fleet

- **"Haz esto"** (tarea acotada, secuencial, verificable con un test) →
  un solo agente apropiado, sin fleet. Es la mayoría de los casos y evita
  activar proveedores pagados que no aportan paralelismo útil.
- **"Resuelve esto de punta a punta"** (cambio abierto, varias perspectivas,
  implementación + verificación) → `just dan`: lead autónomo y fleet visible.
- **"¿Qué se me escapa?"** (auditoría, review pre-merge, diagnóstico raro,
  varias subtareas independientes) → fleet.
- **"Necesito diagnóstico paralelo urgente, sin ejecutar efectos"** → carrera
  (`just race`); un cambio de producción conserva el flujo asegurado y sus gates.

## 2 · Bootea el equipo (1 comando)

Camino recomendado para una misión autónoma completa:

```bash
just dan <nombre> "<objetivo completo>" --target-repo <repo>
```

El preset `dan` abre lead, scout, builder, challenger y verifier. El lead decide
cuáles usar; no hay que avanzar fases ni aprobar transiciones rutinarias. La
flota queda visible al terminar salvo que pases `--teardown`.

Para operar las primitivas manualmente:

```bash
just fleet <nombre> [instance=role...]
# ejemplos:
just fleet-preset audit audit               # roster canónico por capacidades
just fleet-preset research research         # dos triage con IDs únicos
just fleet sse                              # default small: solo lead
./scripts/fleet-up.sh fix --target-repo <repo> \
  build=codex verify=reviewer                # writer aislado
```

El router canónico es `orchestration/router.yaml`: allí viven commands, modelos,
capacidades, permisos declarativos, ranks y presets. Codex es el lead por defecto;
Claude solo entra como Lead si lo seleccionas con `--lead-provider claude` o si
habilitas deliberadamente `--allow-fallback`. Sin ese flag, un Codex no
disponible falla cerrado. Despachar Claude/GLM/MiniMax puede tener costo; no los
uses como smoke automático.

Roles frontier: `codex` · `claude` · `minimax` (MiniMax-M3) · `glm` (GLM-5.2) · `kimi` (Kimi K3)
Roles locales prompt-only: `code_worker` · `triage` · `light_code` · `reviewer` · `general_worker`
Límite de RAM: **un solo rol local pesado en ejecución** y máximo tres locales concurrentes.

Nota de enforcement: los wrappers validan UUID, identidad, autoridad, sandbox,
leases y finalización por `run_id`. En `autonomous`, el gate de fase está abierto
para todo el roster; en `guided`/`assured` conserva las transiciones explícitas.
En todos los modos hay un solo writer y una race entrega un candidato, no verdad.
Los presets de revisión declaran `identity_groups`: antes de crear el workspace,
el router exige una tupla `provider/model/variant` distinta por miembro y el
manifest conserva `identity_group.*`. Esto prueba diversidad de identidad, no
independencia semántica; una race custom del mismo modelo sigue permitida pero
nunca deja de ser candidato no verificado.
Los revisores OpenCode no reciben Bash ni raíces externas del controlador: el launcher
valida que la configuración resuelta exponga únicamente `read`, `glob` y `grep`.
Kimi tampoco recibe shell ni escritura; sus herramientas de lectura están
confinadas al checkout objetivo. Su completion se deriva de Wire mediante un
bridge del controlador porque CMUX aún no publica hooks Kimi nativos.
Para FDP-2, CONTROL incorpora Git y el mensaje exacto en un evidence pack
hash-bound antes de despachar al Checker.
Los frontier colaboran mediante CONTROL (MCP autenticado, CAS y diálogo
durable), no enviándose mensajes directos. Los roles Ollama son prompt-only y
no reciben MCP ni acceso a artifacts del controlador.

Qué pasa solo: el plan completo se valida antes de tocar cmux, los panes quedan
ordenados `CONTROL → RECON → BUILD → CHALLENGE → VERIFY`, se verifican contra
`tree`, el manifest conserva refs + UUIDs y el **lead se auto-orienta**.

## 3 · Despacha por el camino rastreado

`just dan` ya entrega la misión completa al Lead como su primer submit rastreado.
No vuelvas a escribirla en el panel. En una flota guiada usa los wrappers:

```bash
just send <feature> <instance> "<tarea>"
./scripts/fleet-wait.sh <feature> <instance> \
  --run <instance>=<run_id-devuelto> --timeout 600
```

Los wrappers validan identidad, fase y lease y persisten el `run_id`. Escribir
directamente en un panel o usar `cmux send` puede cambiar la pantalla, pero es
**visible y untracked**: no equivale a dispatch `control-v1`, no puede enlazarse
a completion y no debe usarse como evidencia.

## 4 · Supervisa sin niñear

- Ojo al **sidebar**: colores, banners y notificaciones orientan sobre actividad
  o input humano, pero no prueban completion. Usa `just status` en cualquier
  momento y `fleet-wait` para el run exacto.
- Entra a cualquier pane a observar. Interrumpir con Escape o ctrl+c no prueba
  un terminal; un frontier interrumpido puede quedar `indeterminate` con lease
  retenido.
- Nunca infieras por reloj o notificación: `fleet-wait` reconcilia el ledger y
  las notificaciones solo llaman tu atención.

## 5 · Cosecha resultados

- Resultados grandes viven en **archivos** (pídelo así en la misión: "escribe
  findings a outputs/<x>.md") — las pantallas truncan.
- Cruza perspectivas: la coincidencia entre modelos orienta, pero no sustituye
  tests ni evidencia durable; un desacuerdo indica dónde investigar.
- La síntesis del lead debe marcar claims verificados contra código.

## 6 · Cierra limpio

```bash
just fleet-down <nombre>    # reconcilia, cierra y archiva manifest/evidencia
```

Checklist: ¿el resultado quedó en un archivo/commit/PR? ¿decisiones pendientes
anotadas? ¿`just status` sin ⚠️? Un workspace = una misión; misión cerrada =
workspace cerrado.

---

## Patrones rápidos

**Carrera (hotfix / needle-in-haystack):**
```bash
just race <nombre> "<tarea de 1 línea>" codex_candidate minimax_candidate
# el primero es un candidato no verificado; los demás se conservan por defecto.
```

**Despacho manual a un worker (sin lead):**
```bash
dispatch="$(./scripts/fleet-dispatch.sh <fleet> <instance-id> "<tarea>" --json)"
run_id="$(jq -er '.run_id' <<<"$dispatch")"
./scripts/fleet-wait.sh <fleet> <instance-id> \
  --run <instance-id>="$run_id" --timeout 600
cmux read-screen --surface <su surface> --workspace <ws> --lines 50
```

`read-screen` es observacional; consume el `result_file` durable para decisiones.

Para un smoke sin inferencia frontier usa la receta
`FLEET_NO_LEAD=1 ... triage=triage` de
[`guia-uso-flota.md`](guia-uso-flota.md#receta-local-sin-inferencia-frontier).

**Esperar a varios / al primero:**
```bash
./scripts/fleet-wait.sh <fleet> <instance-a> <instance-b> \
  --run <instance-a>=<run-id-a> --run <instance-b>=<run-id-b>
./scripts/fleet-wait.sh <fleet> <instance-a> <instance-b> --any \
  --run <instance-a>=<run-id-a> --run <instance-b>=<run-id-b>
```

## Si algo sale mal

| Síntoma | Remedio |
|---|---|
| Cerraste un pane vivo por accidente | No edites el manifest. En una Mission usa `just mission-show` y `just mission-resume`; en una flota manual conserva evidencia y recupera solo mediante identidad durable. |
| Ref del manifest no coincide | `cmux tree --workspace <ws>` es la verdad; las refs son posicionales — NUNCA cierres sin verificar en tree |
| Worker frontier no lanza | El preflight debe fallar antes de crear el workspace. Revisa el command/env declarado en `router.yaml`. |
| codex se come el prompt al bootear | Diálogo de update pendiente: `npm install -g @openai/codex` |
| `fleet-wait` no termina | Confirma el `instance=run_id` exacto y los hooks. El ledger decide completion; el listener solo despierta reconciliación y también recupera terminales anteriores a la suscripción. |
| No sabes qué agente es quién | `cmux tree --all` + el manifest del fleet |

## Reglas de oro (las 5 que no se negocian)

1. `tree` antes de tocar cualquier ref — y jamás cierres lo que no verificaste.
2. Panes para VER, archivos para DATOS, eventos para ESPERAR.
3. El lead verifica los claims de los workers contra código, siempre.
4. Un rol pesado local a la vez (16 GB); teardown al cerrar la misión.
5. `just status` al llegar y al irte — tú eres el cuello de botella del
   sistema, no los agentes.
