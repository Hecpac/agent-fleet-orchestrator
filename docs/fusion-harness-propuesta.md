# Propuesta: Fusion Harness para `agent-fleet-orchestrator`

> **Fecha:** 2026-07-20 · **Estado:** propuesta (sin código)
> **Fuentes:** video + repo `disler/fusion-harness` (análisis línea a línea), Aider architect/editor (2024), Devin Fusion (Cognition, jun 2026), OpenRouter Fusion (jun 2026), literatura 2023–2026 (MAGIS, SWE-Debate, FrugalGPT, "Wisdom and Delusion of LLM Ensembles", "The Collaboration Gap"), y el estado actual de este repo (Mission Control, router v3, FDP-2/3, workers Ollama).

---

## 1. Qué vamos a construir (una frase)

Un **arnés de fusión de dos modelos con gate-first validation** — tres comandos (`opinion`, `fusion`, `auto-validate`) — implementado **sobre nuestra propia infraestructura** (Python + cmux + router v3 + Ollama), que graba evidencia ligera en el mission ledger y escala a Mission Control completo solo cuando el riesgo lo exige.

**No** es: un port de la extensión TS de pi, ni un modo nuevo de Mission Control con admission de 5 fases para cada run.

---

## 2. Qué dice la evidencia (por qué este diseño y no otro)

El deep research separa lo verificado del marketing. Estas son las reglas que gobiernan la propuesta:

| # | Hallazgo | Fuente | Consecuencia en el diseño |
|---|---|---|---|
| 1 | El split de roles (razonador ≠ ejecutor) mejora incluso al mismo modelo (+3 a +10 pts) | Aider benchmark [verificado, 2024] | ARCHITECT y BUILDER son **roles con prompts distintos**, no solo dos modelos |
| 2 | Main perezoso + sidekick barato: −35/41% coste manteniendo inteligencia frontier | Devin Fusion [auto-reportado] | Los workers **Ollama locales son sidekicks de coste ~0** para trabajo acotado; el frontier solo planea, decide y revisa |
| 3 | Panel + judge supera al mejor modelo solo (69.0 vs 65.3); **self-fusion** (mismo modelo ×2 + síntesis) da +6.7 pts | OpenRouter DRACO [metodología pública] | `/opinion` + síntesis vale incluso con un solo provider; el paso de **síntesis** es donde vive gran parte del lift |
| 4 | **"Popularity trap"**: el consenso amplifica errores compartidos | Vallecillos et al. 2025 [verificado] | NUNCA majority voting ciego. La fusión es un **juez con atribución** (Consensus/Divergence/Discarded), no un voto |
| 5 | Modelos buenos en solo **degradan** al forzarlos a colaborar | "The Collaboration Gap" 2025 | Los dos agentes trabajan **independientes y en paralelo**; solo se fusionan los outputs, nunca conversan mid-task |
| 6 | Consultar otro modelo "como tool" paga contexto completo no cacheado en cada llamada | Cognition | La fusión se invoca **selectivamente** (puntos de decisión), nunca por tool-call |
| 7 | El fallo de un loop agéntico local cuesta wall-clock, no tokens; benchmarks de tool-use sobreestiman fiabilidad 20–40% | análisis safeclaw 2026 | Ollama solo para tareas **acotadas y verificables** (formato, búsqueda, resumen); nunca como builder principal |
| 8 | Gate-first (spec ejecutable antes del build) con líneas FAIL como prompt de corrección | fusion-harness [verificado en vivo] | Núcleo de `/auto-validate`: gate `uv` PEP 723, baseline RED, máx 5 rondas, triage con repair único |
| 9 | Los benchmarks de los vendors son propios | los tres vendors | Medimos nosotros: cada run emite artefactos + métricas comparables (nuestro `evals/`) |

**Conclusión del research:** la arquitectura ganadora 2026 es *frontier perezoso para juicio + barato/local para volumen + síntesis con atribución + gate ejecutable*. Eso es exactamente lo que propone esta propuesta, y encaja con lo que este repo ya tiene (router multi-proveedor, Ollama prompt-only, ledger de evidencia).

---

## 3. El mejor escenario (decisión de arquitectura)

### Opción descartada: instalar pi + la extensión de Dan tal cual
Añade una segunda harness (pi) junto a la nuestra, otro runtime (npm/bun), y pierde el router, los leases y el ledger. Dan mismo dice: *"no me importa si usas pi — construye tu propia harness"*.

### Opción elegida: `fusion/` como capa ligera nativa del repo

```
┌─────────────────────────────────────────────────────────────────┐
│  just opinion "…"  │  just fusion "…"  │  just auto-validate "…"│  ← 3 comandos
├─────────────────────────────────────────────────────────────────┤
│  scripts/fusion/fusion_harness.py  (orquestador, ~600 líneas)    │
│   · spawn de workers (providers/ollama.py + CLIs frontier)       │
│   · gate loop red→green con baseline, triage y repair único      │
│   · artefactos en outputs/fusion/<run_id>/  (nunca en el repo)   │
├─────────────────────────────────────────────────────────────────┤
│  orchestration/router.yaml  → preset nuevo: `fusion`             │
│   ARCHITECT ◆ (fable/sonnet) · BUILDER ▲ (sol/terra)             │
│   FUSION ⧉ (= model ARCHITECT, sesión fresca)                    │
│   VALIDATOR ✓ (= model ARCHITECT, tools: write solo al gate)     │
│   PANEL local Ollama (opinión barata, coste ~0)                  │
├─────────────────────────────────────────────────────────────────┤
│  cmux: workspace "fusion-<run>" · 2 panes etiquetados por rol    │  ← visibilidad (video 1)
├─────────────────────────────────────────────────────────────────┤
│  Ledger LIGERO: append de eventos fusion.* al mission ledger     │
│  (evidencia, NO admission). Escalada: si el gate toca auth/      │
│  pagos/datos → handoff a `just mission` (regulated)              │
└─────────────────────────────────────────────────────────────────┘
```

**Principios del mejor escenario:**

1. **Independencia, no colaboración** (hallazgo #5): los dos agentes nunca se leen mid-task. La comunicación es: prompt → outputs → fuser. Cero conversación entre workers.
2. **Síntesis con atribución, no consenso** (hallazgo #4): el FUSION mergea con tags `[ARCHITECT]`/`[BUILDER]` y cierra con Consensus & Divergence. Las divergencias son el producto.
3. **Gate antes que código** (hallazgo #8): el VALIDATOR escribe el gate ejecutable *antes* de que exista el trabajo; baseline debe fallar RED; las líneas `FAIL: expected X, found Y, at <path> — <fix>` son literalmente el siguiente prompt del builder.
4. **Selectivo, no always-on** (hallazgos #3, #6): `opinion`/`fusion` se usan en puntos de decisión (arquitectura, approach A vs B); `auto-validate` para construir. Nada de fusionar cada tool call.
5. **Local para volumen verificable** (hallazgos #2, #7): Ollama entra como (a) tercera opinión barata en `/opinion`, y (b) ejecutor de checks del gate que no requieran frontier. Nunca como builder principal ni como juez.
6. **El filesystem es el transporte, no el chat**: gate, respuestas completas y reportes viven en `outputs/fusion/<run_id>/`; los prompts llevan paths absolutos ("lee estos paths, nunca escanees el FS"), con inline truncado a 60k chars como en el original.
7. **El poder se enforcea por herramientas, no por prompts**: el VALIDATOR solo tiene `write` al path del gate; el BUILDER nunca puede editar el gate; el TRIAGE pierde `write` tras su única reparación. Esto ya es natural en nuestro modelo de roles del router (`tool_access`).

---

## 4. Los tres comandos (mecánica exacta)

### 4.1 `just opinion "<pregunta>"` — scout (2+1 agentes, read-only)
- ARCHITECT y BUILDER responden en paralelo, tools read-only + bash. Opcional `--panel`: un worker Ollama (gemma3/qwen) da la tercera opinión de coste ~0.
- Panel comparativo: modelo, latencia, tokens in/out, coste, respuesta completa. *"Relativity is the best benchmark."*
- Sin merge: el humano lee y decide. Artefactos: `opinion/{architect,builder,panel}.md` + `summary.json`.

### 4.2 `just fusion "<prompt>" ["<instrucción de fusión>"]` — planning (3 agentes)
- Los dos workers ejecutan con full tools en paralelo. **Anti-colisión por naming**: todo archivo creado lleva `-ARCHITECT-<modelo>` / `-BUILDER-<modelo>` (comparten cwd; un path "bare" sería una carrera).
- Tercer agente FUSION (modelo ARCHITECT, **sesión fresca** — un architect que hereda las asunciones del builder es un eco) mergea según la instrucción, o la default del repo original:
  > *"Critically merge the two answers into one definitive answer. Discard anything incorrect or hallucinated, keep the strongest elements of each, and combine complementary insights — do not simply concatenate."*
- Output contract: `1. Fused answer` (atribución inline) · `2. Consensus & divergence` (qué descartó y por qué).

### 4.3 `just auto-validate "<prompt>"` — build+test en uno (2 agentes + gate)

```
VALIDATOR (read-only + write→gate.py solamente)
   │  escribe gate.py (uv PEP 723, exit 0 iff el trabajo está hecho,
   │  una línea PASS/FAIL por check, determinista, <60s, sin side-effects)
   ▼
BASELINE: uv run gate.py  → debe fallar RED
   │  (si pasa: ⚠ gate débil o trabajo ya hecho — warning ruidoso)
   ▼
┌─ ronda 1..5 ────────────────────────────────────────────┐
│ BUILDER (full tools, gate visible pero INMUTABLE)        │
│   ronda 1: prompt + gate completo                        │
│   ronda N: output verbatim del gate + triage (si hay)    │
│ gate corre → PASS: verde, summary.json, fin              │
│           → FAIL: líneas FAIL = siguiente prompt          │
│ ronda ≥3: TRIAGE (architect, read-only) diagnostica      │
│   · root cause real (verifica contra el FS, no claims)   │
│   · si GATE DEFECT y repair no usado → reescribe gate    │
│     (re-run GRATIS, no consume ronda; gate viejo se      │
│      preserva como gate.py.rN)                           │
└──────────────────────────────────────────────────────────┘
   ▼
HALT ruidoso tras 5 rondas (nunca loop infinito silencioso)
```

Reglas portadas del original que son no-negociables:
- Los errores del harness (timeout del gate, `uv` ausente) **nunca se cobran como ronda del builder**.
- El re-run tras gate repair es gratis: *"un defecto del gate nunca fue culpa del builder"*.
- Kill: SIGTERM → SIGKILL a los 5s; abort antes del spawn = no spawn.
- Memoria por sesión: las rondas 2+ continúan el hilo del builder (en nuestro caso: transcript persistente del worker frontier por `run_id`), no re-prompt desde cero.

---

## 5. Matriz de roles → modelos (tiers)

| Tier | ARCHITECT ◆ | BUILDER ▲ | Uso |
|---|---|---|---|
| `workhorse` | claude-sonnet-5 | gpt-5.6-terra | testing del propio harness, iteración diaria barata |
| `sota` | claude-fable-5 | gpt-5.6-sol | decisiones y builds que importan |
| `local-panel` | gemma3:4b / qwen2.5-coder:7b (Ollama) | — | tercera opinión en `/opinion`, coste ~0 |

- Thinking: `medium` en workhorse, `xhigh` en SOTA (igual que Dan).
- El preset vive en `orchestration/router.yaml` como `fusion`, declarando `identity_groups` (ARCHITECT y BUILDER deben ser tuplas provider/model distintas — diversidad auditable, principio ya existente en el repo).
- Cambiar de modelo el día que salga uno nuevo = una línea en el router. *"Role ≠ model. Models change quarterly; the harness compounds."*

---

## 6. Integración con lo que ya existe (sin romper nada)

| Componente actual | Relación con el fusion harness |
|---|---|
| `orchestration/router.yaml` | Añade preset `fusion` (roles, authority, tool_access). Nada más se toca. |
| `scripts/providers/ollama.py` | Se reutiliza tal cual para el panel local (prompt-only, `think:false`). |
| `fleet_mission_state.py` (ledger) | Modo **ligero**: el harness appendea eventos `fusion.run.*` (run_id, roles, modelos, gate verdict, rondas, coste) como evidencia. **No** usa admission de 5 fases — eso es para `regulated`. |
| cmux (skill del repo) | Workspace `fusion-<run_id>` con 2 panes: ARCHITECT izquierda (color), BUILDER derecha. Los eventos son doorbells; el harness reconcilia por artefactos en disco (misma doctrina que `fleet-wait.py`). |
| `workflows/` | NO es un workflow de Mission Control. Es una capa previa: *micro-SDLC* para el 90% del trabajo diario. Si el resultado toca auth/pagos/datos → el humano lanza `just mission` con el plan fusionado como input. |
| `evals/` | Nuevo fixture: replay de runs fusion (artefactos + summary.json) para medir nuestro propio lift de fusion vs single-model (hallazgo #9: no creernos los benchmarks ajenos). |

---

## 7. Plan de implementación (slices incrementales)

| Slice | Contenido | Criterio de done |
|---|---|---|
| **F0 — opinion** | `fusion_harness.py` con un solo comando `opinion`: spawn paralelo de 2 frontier + 1 local, panel comparativo, artefactos + evento ledger | `just opinion "top 3 sqlite strategies"` produce 3 respuestas + summary.json con costes |
| **F1 — fusion** | Segundo comando + agente FUSION con sesión fresca, naming anti-colisión, output contract con atribución | Run end-to-end con fused.md que cita `[ARCHITECT]`/`[BUILDER]` y lista divergencias |
| **F2 — auto-validate** | Gate loop completo: validator→baseline RED→builder rounds→triage→repair único→halt | Un build real pasa el gate en ≤5 rondas; un gate defectuoso se repara sin consumir ronda |
| **F3 — DX cmux** | Workspace etiquetado, colores por rol, panel final markdown; eventos como wake-ups | Se ve como la demo de Dan; la completitud nunca depende de la pantalla |
| **F4 — eval propio** | Fixture en `evals/` que corre la misma tarea: single-model vs fusion, N=5, y reporta lift real | Informe con nuestros números, no los de OpenRouter |

Estimación honesta: F0–F2 son el corazón (~1.200 líneas Python total incluyendo prompts externalizados como `USER_PROMPT_*.md` con `{{VAR}}`, igual que el original). F3–F4 son polish medible.

---

## 8. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| **Popularity trap** — el fuser adopta un error compartido | Output contract exige sección "Discarded + por qué"; el humano lee divergencias en decisiones críticas |
| **Gate débil** (pasa sin probar nada) | Baseline RED obligatorio + warning ruidoso; triage puede declarar GATE DEFECT |
| **Doble coste frontier** en cada build | `auto-validate` solo para trabajo que importa; `opinion` con panel local es casi gratis; workhorse tier por defecto |
| **Latencia 2–3×** | Paralelo real (los dos workers a la vez); el humano trabaja en otra cosa (notificación cmux al terminar) |
| **Collaboration gap** — degradar al forzar interacción | Los workers nunca conversan; la fusión es post-hoc sobre outputs |
| **Workers paralelos se pisan archivos** | Naming `{{ROLE}}-{{MODEL}}` obligatorio en todo path creado (portado del original) |
| **El harness crece hasta ser otro monstruo de 54k líneas** | Regla dura: `fusion_harness.py` < 800 líneas; prompts en archivos; si una feature no cabe, va a Mission Control, no aquí |

---

## 9. Resumen ejecutivo

- **Qué:** arnés de fusión nativo (3 comandos, 2 roles + fuser + gate), inspirado en `disler/fusion-harness`, construido sobre nuestro router/cmux/Ollama/ledger.
- **Por qué este escenario y no otro:** la evidencia (Aider, Devin, OpenRouter, literatura) converge en: roles separados + síntesis con atribución + gate ejecutable + uso selectivo + local para volumen. El diseño incorpora los 9 hallazgos y evita las 4 trampas documentadas (popularity trap, collaboration gap, cache misses, loops locales lentos).
- **Qué NO es:** ni un port de pi, ni un modo de Mission Control, ni fusion siempre-activa.
- **Coste de entrada:** F0 (`opinion`) es útil el primer día; F2 (`auto-validate`) es la joya — *"quien construye no se califica a sí mismo"* con 1/100 de la maquinaria de FDP-2.
