# Catálogo de capacidades de modelos y CLI

> **Instantánea:** 2026-10-01 · **Estado:** hipótesis para evaluación propia.
> Este documento no configura el router ni los perfiles Herdr. Según C8, los
> resultados publicados orientan los experimentos, pero no son premisas ni
> umbrales del router. La calidad real y el coste facturado siguen
> `NOT_VERIFIED` hasta tener evidencia propia.

## 1. Cómo leerlo

- **Unidad evaluada:** `modelo + CLI + adaptador + entorno` (C8). Por eso las
  tablas separan qué CLI alcanza cada modelo y qué evidencia produce ese CLI.
- **Selección en dos pasos:** primero filtros obligatorios (sección 2 y 3);
  después evidencia de capacidad (sección 4).
- **Etiquetas:** `[I]` tercero independiente; `[I*]` leaderboard independiente
  con ejecuciones enviadas por el fabricante; `[V]` cifra del fabricante;
  `[P]` evidencia propia de este proyecto.
- **Versiones de benchmark:** Terminal-Bench 4.0 no es comparable con 2.x;
  SWE-Bench Pro V2 no es comparable con V1 ni con Verified; el índice de
  Artificial Analysis (AA) es la v4.3.2 y cambia de escala entre versiones.
- Las cifras de AA, ARC Prize, Epoch y tbench son capturas del 2026-10-01.
  AA etiqueta las filas Claude con «Default Fallback». Según Anthropic, en sus
  propias mediciones las tareas que activan un safeguard las completa otro
  modelo. No está verificado que AA use el mismo mecanismo.

## 2. Filtro 1: CLI disponibles y evidencia que producen

| CLI | Instalada (origen) | Última estable | Contrato del repo | Modelos que alcanza | Adaptador de evidencia del owner lane | Writer Herdr |
|---|---|---|---|---|---|---|
| `codex` | 0.159.3 (npm global, actualizada 2026-10-01); 0.144.1 standalone sombreada en `~/.local/bin` | 0.159.3 (2026-09-30) | 0.159.3 (`codex-0.159-v1`, certificado 2026-10-01); 0.154.0 y 0.153.4 históricos ([fleet_herdr_versions.py](../scripts/fleet_herdr_versions.py)) | OpenAI | Sí: el transcript atestigua la configuración; solo `gpt-6-astra`/`gpt-5.6-sol`, CLI 0.154.0/0.155.1 ([fleet_herdr_owner_runtime.py:34](../scripts/fleet_herdr_owner_runtime.py)) | Sí (único) |
| `claude` | 2.1.286 (npm global, actualizada 2026-10-01) | 2.1.286 (2026-09-30) | ninguno | Anthropic | `--json-schema` observado; configuración, aislamiento y cancelación `NOT_VERIFIED` | No |
| `opencode` | 1.18.30_2 (Homebrew; la fórmula aún no publica 1.18.34) | 1.18.34 (2026-09-30) | ninguno | DeepSeek, Z.ai, MiniMax, Moonshot | Inspección de eventos; permisos `not_attested` | No |
| `kimi` | 2.1.1 (instalador oficial, actualizada 2026-10-01; misma línea que 0.39.1/0.42.0) | 2.1.1 (2026-09-24) | ninguno | Moonshot | Ninguno; escribe wire 1.5, aceptado por `kimi_hook_bridge` desde el 2026-10-01 | No |
| mini-swe-agent + cliente CONTROL | 2.4.6 fijada por hash | — | `harness_mini_*` | DeepSeek (`api.deepseek.com`, `deepseek-flash`) | Harness propio aparte; live `NOT_VERIFIED` | No (carril separado) |
| `ollama` | 0.35.0 (Homebrew, actualizada 2026-10-01); servidor parado | 0.35.0 (2026-09-28) | ninguno | Modelos locales | Solo prompt, rol consultivo | No |
| `herdr` | 0.9.0 (`~/.local/bin`) | no investigada | 0.9.0 | — | — | — |

Consecuencia: los modelos Anthropic tienen capacidad de primer nivel (sección
4), pero hoy solo entran en Fusion y en el router legacy. Para entrar en Herdr,
incluso como lectores, hace falta trabajo de backend: el roster exige
`provider=openai` y `hook_source=codex`
([fleet_herdr.py:382](../scripts/fleet_herdr.py)). Para ser writer hace falta
además un adaptador que atestigüe modelo, esfuerzo, permisos y cancelación
desde el transcript de Claude Code. **El bloqueo es de evidencia, no de
capacidad.**

## 3. Filtro 2: modelos vigentes

### OpenAI (Codex)

| Modelo | Estado | Contexto / salida | $ entrada/salida por 1M | Esfuerzo | Repo |
|---|---|---|---|---|---|
| `gpt-6-astra` | vigente, el más capaz | 1.05M / 128K | 10 / 50 | low–max | lead, research |
| `gpt-6.1-sol` | vigente; default de Codex desde 0.159.1 | 1.05M / 128K | 2 / 10 | low–max | **ausente** |
| `gpt-6-luna` | vigente, alto volumen | 1.05M / 128K | 0.10 / 0.50 | none–max | ausente (blueprint cita `gpt-5.6-luna`) |
| `gpt-5.6-sol/terra/luna` | generación anterior; activos en la API, ya no listados en Codex | 1.05M / 128K | 4/20 · 2/12 · 0.2/1.2 | none–max | `gpt-5.6-sol` en los tres perfiles Herdr |

### Anthropic (Claude Code)

| Modelo | Estado | Contexto / salida | $ entrada/salida por 1M | Esfuerzo | Repo |
|---|---|---|---|---|---|
| `claude-fable-5-1` | vigente, nivel premium | 1M / 128K | 10 / 50 | low–max (adaptive siempre) | repo usa `claude-fable-5` |
| `claude-opus-5-5` | vigente; default de Claude Code | 1M / 128K | 4 / 20 | low–max (adaptive siempre) | repo usa `claude-opus-5` |
| `claude-sonnet-5-5` | vigente | 1M / 128K | 2 / 10 | low–max | Fusion usa `claude-sonnet-5` |
| `claude-haiku-4-5-20251001` | vigente pero antiguo; Haiku 5.5 anunciado | 200K / 64K | 1 / 5 | sin effort | ausente |

Los IDs antiguos `claude-fable-5`, `claude-opus-5` y `claude-sonnet-5` siguen
activos; su retirada no será antes de junio–julio de 2027. En Opus 5.5, Sonnet
5.5 y Fable 5.1, `tool_choice` `any`/`tool` devuelve 400: hay que usar `auto`
con `strict: true` o structured outputs. Para roles reproducibles, fijar IDs
completos: los alias de Claude Code apuntan a modelos distintos según el
proveedor.

### Open-weight y otros

| Modelo | Estado | Licencia | Contexto | $ entrada/salida por 1M | CLI | Repo |
|---|---|---|---|---|---|---|
| DeepSeek-V4.1-Flash (`deepseek-flash`) | vigente | MIT | 1M | 0.15–0.30 / 0.60–1.20 | OpenCode, harness Mini | `deepseek-flash` |
| `deepseek-v4-pro` | **mapeo en conflicto**: precios dice V4-Pro-0813; noticia del 10-09 dice que redirige a V4.1-Flash | MIT | 1M | 0.66–1.32 / 1.98–3.96 | OpenCode | `opencode.json` |
| GLM-5.3 | vigente | tipo MIT con cláusula para operadores MaaS grandes | 1M | 1.40 / 4.40 | OpenCode `zai/glm-5.3` | repo usa `glm-5.2` (superado) |
| GLM-5.3-Flash | vigente, barato | MIT | 1M | 0.15 / 0.50 | OpenCode | ausente |
| Kimi K3 | vigente; lento (~34 t/s) | custom | 1M | 3 / 15 | Kimi Code, OpenCode | `kimi-code/k3` no aparece documentado |
| MiniMax-M3 | vigente | **solo uso no comercial sin autorización** | 1M | 0.30 / 1.20 | OpenCode | `MiniMax-M3` |
| Qwen3.8-27B | candidato local (18 GB q4) | Apache-2.0 | 262K | local | Ollama `qwen3.8:27b` | ausente |

Locales actuales: `gemma3:4b`, `granite-code:3b` y `qwen2.5-coder:7b` (32K de
contexto) tienen uno o dos años. Candidatos ≤32 GB: `qwen3.8:27b`,
`gemma4:12b` y `gemma4:26b` (ya descargado). Ningún benchmark independiente
mide resumen, clasificación o extracción con estos modelos en un Mac.

## 4. Evidencia por capacidad

### 4.1 Ingeniería agéntica (Build): CLI + modelo

AA Coding Agent Index v1.5 `[I]`: media de DeepSWE v1.1, Terminal-Bench 4.0 y
SWE-Atlas-QnA, con coste de API y tiempo medio por tarea. Es la comparación
más cercana a la unidad de C8 porque mide el par CLI + modelo. Filas que este
repo puede ejecutar o considerar:

| CLI – modelo (esfuerzo) | Índice | DeepSWE | TB 4.0 | SWE-Atlas-QnA | $/tarea | min/tarea |
|---|---|---|---|---|---|---|
| Claude Code – Sonnet 5.5 (max) | 68.4 | 72.0 | 66.2 | 66.9 | 14.19 | 87 |
| Claude Code – Opus 5.5 (max) | 66.0 | 68.4 | 63.1 | 66.4 | 13.04 | 64 |
| Codex – GPT-6.1 Sol (xhigh) | 62.9 | 73.2 | 54.5 | 61.0 | 1.04 | 16 |
| Claude Code – Sonnet 5.5 (xhigh) | 62.9 | 68.4 | 58.1 | 62.1 | 3.33 | 27 |
| Claude Code – Fable 5.1 (max) | 62.2 | 64.3 | 57.6 | 64.8 | 12.39 | 35 |
| Codex – GPT-6 Astra (max) | 61.6 | 67.6 | 55.6 | 61.8 | 7.47 | 29 |
| Codex – GPT-6.1 Sol (medium) | 61.4 | 72.0 | 51.5 | 60.8 | 0.70 | 11 |
| Claude Code – Sonnet 5.5 (high) | 55.0 | 66.7 | 41.9 | 56.5 | 1.24 | 12 |
| Codex – GPT-5.6 Sol (max) | 54.6 | 72.3 | 37.4 | 54.0 | 6.35 | 21 |
| OpenCode – GLM-5.3 (max) | 53.6 | 61.4 | 39.9 | 59.4 | 4.24 | 48 |
| Kimi Code – Kimi K3 | 51.9 | 68.4 | 21.2 | 66.1 | 5.05 | 61 |
| Codex – GPT-6 Luna (max) | 41.1 | 63.7 | 15.2 | 44.4 | 0.18 | 21 |
| Codex – DeepSeek V4 Flash 0731 (max) | 38.7 | 54.3 | 10.6 | 51.3 | 0.09 | 18 |

Lectura:
- **Techo:** Claude Code con Sonnet 5.5 u Opus 5.5 en max, unas 13× más caro
  por tarea que Sol.
- **Eficiencia:** GPT-6.1 Sol en xhigh o medium está a 5–7 puntos del techo
  por ~1 $/tarea y 11–16 min.
- **El modelo actual del Worker Herdr** (`gpt-5.6-sol`) solo figura en esfuerzo
  max: 8 puntos por debajo de 6.1 Sol xhigh y 6× más caro por tarea. Herdr lo
  ejecuta en `high`, que no está medido, así que la comparación no es directa
  con el perfil actual.
- La fila DeepSeek es V4 Flash 0731 dentro de Codex; V4.1-Flash no figura.

Terminal-Bench 4.0 en tbench.ai `[I*]` (330 trials): Codex – Astra max 58.2;
Claude Code – Fable 5.1 max 57.9; Claude Code – Opus 5 xhigh 53.9; Claude
Code – GLM-5.3 max 41.8; Codex – GPT-5.6 Sol max 37.3.

### 4.2 Razonamiento difícil (Plan, Synthesis, arbitraje)

| Modelo (esfuerzo) | AA Index `[I]` | ARC-AGI-2 % · $/tarea `[I]` | GPQA Diamond |
|---|---|---|---|
| Claude Opus 5.5 (max / high) | 57.6 / 53.6 | 93.3 · 0.41 (high) | 90.6 `[I]` Epoch |
| Claude Sonnet 5.5 (max / xhigh) | 56.0 / 51.9 | no encontrado | 95.6 `[I]` Epoch |
| Claude Fable 5.1 (max) | 53.4 | 90.0 · 4.49 | no encontrado |
| GPT-6 Astra (max) | 52.7 | 95.0 · 1.12 | 96.1 `[I]` AA |
| GPT-6.1 Sol (max / xhigh) | 51.8 / 51.0 | 94.2 · 0.25 | no encontrado |
| GLM-5.3 (max) | 44.8 | no encontrado (Flash: 65.8 · 0.09) | 88.1 `[V]` rival |
| Kimi K3 (max) | 43.6 | 60.4 · 1.59 | 93.5 `[V]` |
| DeepSeek V4.1 Flash (max) | 39.5 | V4 Flash 0731: 61.4 · 0.04 | 90.9 `[V]` |
| GPT-6 Luna (max) | 38.1 | 59.3 · 0.06 | no encontrado |
| Claude Haiku 4.5 | 17 | 4.0 | 71.2 `[I]` Epoch |

### 4.3 Comprensión de código, investigación y contexto largo

- **SWE-Atlas-QnA** (preguntas sobre repositorios, componente de AA `[I]`):
  Sonnet 5.5 max 66.9, Opus 5.5 max 66.4, Kimi K3 66.1, Fable 5.1 64.8,
  Astra 61.8, 6.1 Sol 61.0, GLM-5.3 59.4.
- **AA-LCR** (contexto largo `[I]`): Kimi K3 88.7, Fable 5.1 85.3, Opus 5.5
  84.7, V4.1 Flash 84.0, 6.1 Sol 83.0, Sonnet 5.5 82.7, Astra 80.7, GLM-5.3
  79.7.
- **BrowseComp:** Kimi K3 91.2 `[V]`. MRCR v2 8-needle 512K–1M: Astra 96.3
  `[V]`.

### 4.4 Velocidad y volumen

Salida en tokens/s según AA `[I]`, medida en APIs de proveedor, no en local:

| Modelo | tok/s |
|---|---|
| DeepSeek V4.1 Flash | 209 |
| Sonnet 5.5 (max) | 139 |
| GPT-6 Luna | 125 |
| Opus 5.5 (max) | 90 |
| Fable 5.1 | 68 |
| GLM-5.3 | 68 |
| 6.1 Sol | 64 |
| Astra | 51 |
| Kimi K3 | 34 |

En modelos que razonan, el TTFT de AA incluye el tiempo de razonamiento.

### 4.5 Evidencia propia `[P]`

- **DeepSeek:** 8 intentos autorizados con DSH y mini-swe-agent 2.4.6. **0/8
  aceptados.** El oráculo original pasa 7/8, pero el contrato semántico 0/8.
  Fuente: `~/.local/share/fleet-harness-evolution/20260920-01`.
- **Piloto live 07** (DeepSeek, harness Mini): 3 truncamientos rechazados y 0
  aceptaciones. El perfil `thinking-32k-v1` está preparado pero sin autorizar.
- **GLM, baseline textual:** 0/8.
- **Sol/Astra:** no hay comparación representativa propia. Ningún modelo tiene
  todavía evidencia propia que permita ordenarlo frente a otro.

## 5. Mapa de capacidades (hipótesis a probar)

| Capacidad | Primera hipótesis | Alternativas | Notas |
|---|---|---|---|
| Lead: planificación, síntesis, arbitraje | Opus 5.5 (high) | GPT-6 Astra (max); Fable 5.1 | Opus 5.5 lidera el AA Index con ARC-AGI-2 a 0.41 $/tarea; Astra lidera ARC-AGI-2 y TB 4.0 de tbench |
| Writer de máximo nivel | Claude Code – Sonnet 5.5 / Opus 5.5 (max) | Codex – Astra (max) | Techo del índice agéntico; caro y lento por tarea |
| Writer cotidiano | Codex – GPT-6.1 Sol (xhigh o medium) | Claude Code – Sonnet 5.5 (xhigh) | Mejor relación resultado/coste/tiempo; único con adaptador Herdr una vez actualizado |
| Reviewer / challenger (solo lectura) | Otra familia distinta del writer: Claude si escribe Codex, y viceversa | GLM-5.3 (OpenCode) como opción open-weight | La diversidad de identidad hace auditable la corroboración, sin probar errores independientes (C7) |
| Verifier | Primero comprobaciones deterministas (C7); LLM de otra identidad | Opus 5.5 / Astra | El verificador no recibe el dictamen del reviewer |
| Research y contexto largo | Kimi K3 | Fable 5.1, Opus 5.5 | K3 lidera AA-LCR y SWE-Atlas-QnA, pero es lento y flojo en terminal (TB 4.0 21.2) |
| Volumen / triage remoto | GPT-6 Luna | DeepSeek V4.1 Flash, GLM-5.3-Flash | Baratos y rápidos; débiles en tareas agénticas largas |
| Panel local | `qwen3.8:27b` | `gemma4:12b` | Sustituye `gemma3:4b`/`granite-code:3b`; requiere prueba propia |

No hay jerarquía fija entre frontier y open-weight (C8). Los open-weight
quedan hoy por debajo en el índice agéntico, y la evidencia propia de
DeepSeek es negativa. Esto no los excluye: GLM-5.3 y V4.1 Flash siguen siendo
candidatos para challenger y volumen.

### 5.1 Colocación de los open-weight

**GLM-5.3: challenger o revisor de solo lectura.**

- **Evidencia propia `[P]`:** en el ensayo del 2026-09-19 acertó el contenido
  de 8/8 revisiones pequeñas, a 0.05–0.11 USD estimados y 2–4 min cada una.
  En cambio, 0/8 respuestas fueron JSON puro: siempre añadía prosa o
  Markdown, y una vez un `None` de Python. Con cuatro intentos por CLI,
  Claude Code y OpenCode dieron la misma calidad: Claude Code gastó menos
  tokens y OpenCode fue más rápido.
- **Harness:**
  - Para revisar: Claude Code (vía el endpoint compatible con Anthropic de
    Z.ai) u OpenCode.
  - Para código: mini-swe-agent saca DeepSWE 69±3 `[I]` frente a 61.4 de
    OpenCode en AA `[I]`. Son evaluadores distintos, así que es solo un
    indicio.
- **Entrega:** siempre con structured output (`--json-schema` o
  `StructuredOutput`), nunca JSON pedido dentro del texto.
- **Configuración del fabricante:** effort `max`, `temperature=1.0`, salida de
  64K–128K.
- **No usarlo como writer canónico:** cuesta 4.24 USD y 48 min por tarea,
  frente a 1.04 USD y 16 min de Codex – 6.1 Sol.

**DeepSeek V4.1 Flash: volumen con verificación externa.**

- **Usos:**
  - generar candidatos en paralelo, que solo se aceptan si pasan la suite
    privada de CONTROL;
  - lectura y extracción sobre contexto largo: AA-LCR 84.0 `[I]`, a 209 t/s.
- **Harness:** mini-swe-agent. Resultados del fabricante `[V]` por harness:

  | Benchmark | mini-SWE | DSH Minimal | Claude Code | Pi | Codex | OpenCode |
  |---|---|---|---|---|---|---|
  | DeepSWE v1.1 | **74.2** | 72.6 | 69.8 | 66.2 | 65.6 | 65.5 |
  | Terminal-Bench 2.1 | 90.3 | **90.6** | 88.0 | 86.1 | 84.1 | 85.0 |

- **Configuración del fabricante:** `temperature=1.0`, `top_p=0.95`, contexto
  1M, `max_tokens` ≥ 256K, hasta 500 pasos. El harness propio usa 8K
  (`legacy-8k`) o 32K (`thinking-32k-v1`); en el piloto 07 eso produjo
  truncamientos.
- **No usarlo como writer que se valida a sí mismo:** el 0/8 propio falló por
  semántica en casos límite (escritura parcial, `1 == True == 1.0`, empates),
  y sus tests omitían esos casos.

### 5.2 Estructura declarativa

[model-orchestration-v1.json](../orchestration/fleet/model-orchestration-v1.json)
convierte este catálogo en una estructura cerrada y versionada:

- **`bindings`:** cada fila es un par `modelo + CLI + esfuerzo`. Para cada
  carril (`herdr`, `fusion`, `harness_mini`, `legacy_router`) indica
  `admitted` o `blocked`, y cada bloqueo lleva su motivo.
- **`slots`:** las capacidades de la sección 5, con sus candidatos en orden de
  hipótesis. No guardan puntuaciones, solo referencias a este catálogo.
- **`diversity`:** indica si el slot debe preferir una familia distinta de la
  del writer.
- **`teams`:** composiciones con exactamente un writer.

[fleet_model_orchestration.py](../scripts/fleet_model_orchestration.py) valida
el manifiesto y resuelve un equipo para un carril sin lanzar nada:

```sh
python3 -B scripts/fleet_model_orchestration.py validate orchestration/fleet/model-orchestration-v1.json
python3 -B scripts/fleet_model_orchestration.py resolve orchestration/fleet/model-orchestration-v1.json --lane herdr --team standard
```

Las reglas de admisión de cada carril reflejan el código que las aplica: el
adaptador owner, el lanzamiento compilado de Herdr, la política de presupuesto
Mini, el router y los tiers de Fusion. Los tests comprueban esa
correspondencia. Dos límites:
- **En Fusion, `admitted` significa documentado, no probado.** La
  documentación del proveedor da el modelo como disponible en la versión
  instalada del CLI, pero nadie lo ha seleccionado todavía con `codex exec` o
  `claude -p`. Es el mismo criterio con el que se admitieron Opus 5.5 y Astra.
- **El esfuerzo es una petición.** Herdr lo fija en `high` en su lanzamiento.
  Fusion no pasa hoy ningún flag de esfuerzo, así que un binding admitido en
  Fusion corre con el esfuerzo por defecto del CLI.
- **Algunos bloqueos caducan.** Los que citan versiones instaladas son de la
  instantánea y dejan de valer al actualizar. Tras la actualización del
  2026-10-01, GPT-6.1 Sol, GPT-6 Luna y Sonnet 5.5 pasaron a admitidos en
  Fusion, y Kimi K3 quedó bloqueado en el router legacy por el protocolo wire
  1.5. Los que vienen del código (adaptador, lanzamiento, presupuesto)
  no cambian solos.

La estructura no activa nada: ningún perfil, router ni Fusion la consume
todavía. Un perfil Herdr nuevo basado en ella depende del contrato
de Codex y del adaptador de evidencia pendientes.

## 6. Desfases del repositorio

1. `gpt-5.6-sol` aparece en los tres perfiles Herdr, en el router y en el
   adaptador owner. Su sucesor es `gpt-6.1-sol`, default de Codex desde
   0.159.1.
2. `claude-fable-5`, `claude-opus-5` y `claude-sonnet-5` (router, Fusion)
   tienen sucesor: `-5-1`, `-5-5` y `-5-5`.
3. `glm-5.2` está superado por GLM-5.3. El ID `kimi-code/k3` no aparece en la
   documentación. MiniMax-M3 requiere autorización para uso comercial.
4. Hay que resolver el mapeo de `deepseek-v4-pro` con el campo `model` de una
   respuesta real antes de usarlo.
5. CLI actualizadas el 2026-10-01 (Codex 0.159.3, Claude Code 2.1.286, Kimi
   2.1.1, Ollama 0.35.0; OpenCode sigue en 1.18.30 porque la fórmula de
   Homebrew no publica la 1.18.34). Herdr certificó Codex 0.159.3 (contrato
   `codex-0.159-v1`) y el bridge de Kimi acepta el wire 1.5 de Kimi 2.1.1.
   Respaldos de Kimi 0.39.1 y 0.42.0 en `~/.local/share/fleet-cli-backups/20261001/`.

## 7. Implicaciones para la reorganización de roles (paso 2)

- **Perfil nuevo, no edición:** cambiar modelos en `astra_sol`,
  `astra_sol_research_v1` o `sol_minimal_v1` movería digests fijados en los
  ledgers y rompería la lectura histórica. Una asignación nueva debe ser un
  perfil versionado nuevo.
- **Pasar a GPT-6.1 Sol** exige:
  - comprobar empíricamente su selección con Codex 0.159.3 (contrato
    `codex-0.159-v1`): la certificación del 2026-10-01 sólo ejecutó
    `gpt-5.6-sol` y `gpt-6-astra`;
  - ampliar `validate_binding` del adaptador owner;
  - el perfil nuevo.
- **Incluir Anthropic en Herdr** exige el adaptador de evidencia de Claude Code
  y soporte multi-proveedor en el roster. Fusion y el router legacy pueden
  usarlo ya.
- **Método (sección 4 de la constitución):** primero se mantiene el modelo
  constante para medir la organización; después se comparan asignaciones
  heterogéneas, con las mismas entradas, permisos y evaluadores.

## 8. Actualización automática de las CLI (paso 3): conflicto y propuesta

> **Implementado el 2026-10-01:** ver [cli-updates.md](cli-updates.md). Codex se
> prepara en paralelo y se promociona tras certificación; Kimi se comprueba con
> un turno real y se revierte si el bridge no acepta su Wire.

**Conflicto.** El contrato de runtime fija versiones exactas y no acepta
rangos abiertos; cada Mission congela su contrato. El startup guard depende
de la versión del TUI. Además:
- Codex muestra al arrancar un modal «Update now» que bloquea sesiones
  desatendidas (observado en 0.153.4).
- Desde 0.157.0, Codex añade avisos de migración de modelo.

Actualizar un binario en sitio mientras corre una Mission cambiaría la
identidad del proceso.

**Propuesta compatible:**

1. **Canal de instalación automático:**
   - Codex: `codex update` o npm.
   - Claude Code: autoupdater con `autoUpdatesChannel: "stable"`.
   - OpenCode: `opencode upgrade`.
   - Kimi Code: `kimi upgrade`.
   - Ollama: `brew upgrade ollama`.
   - Herdr: `herdr update`, canal `stable`.

   Conviene instalar las versiones lado a lado, en rutas versionadas, en lugar
   de reemplazarlas en sitio. La instalación standalone de Codex ya lo hace
   (`~/.codex/packages/standalone/current` es un symlink).

   Riesgo concreto con Kimi: el wire bridge del repo aceptaba sólo los
   protocolos wire 1.2, 1.3, 1.4 y 1.10 (ahora también 1.5) y la estructura de
   sesiones de kimi-code 0.28/0.29 en adelante
   ([kimi_hook_bridge.py:29](../scripts/kimi_hook_bridge.py)).
   Comprobado el 2026-10-01 con un turno real contra un proveedor local: tanto
   la 0.39.1 como la 2.1.1 escriben wire 1.5 en sesiones nuevas, así que el
   bridge ya era incompatible antes de actualizar. Se adaptó (la migración
   1.4→1.5 de Kimi sólo toca registros `goal.*`).
   Kimi aplica además actualizaciones en segundo plano por defecto: la 0.42.0
   estaba preparada desde el 10-09 y se instaló sola al ejecutar
   `kimi --version`.
2. **Certificación automática** de cada versión nueva:
   - sonda del startup guard en un perfil privado;
   - suite de conformidad sin proveedor;
   - replay determinista de envío y recuperación.
3. **Promoción:** si la certificación pasa, un commit versionado añade el
   contrato nuevo a `fleet_herdr_versions.py` sin borrar los anteriores. Las
   Missions nuevas usan el contrato nuevo y las que están en curso conservan
   el suyo.
4. **Dentro de los panes de una Mission**, comprobación de actualizaciones
   desactivada:
   - Codex: `check_for_update_on_startup=false`.
   - Claude Code: `DISABLE_AUTOUPDATER=1`.
   - OpenCode: `OPENCODE_DISABLE_AUTOUPDATE=1`.

Esto orienta la decisión pendiente sobre Codex hacia certificar la versión
nueva en lugar de instalar 0.154.0. No autoriza instalar software ni relajar
el pin sin esa evidencia de certificación.

## Fuentes principales

Consultadas el 2026-10-01:

- **OpenAI:**
  - https://developers.openai.com/api/docs/models
  - https://learn.chatgpt.com/docs/models
  - https://learn.chatgpt.com/docs/changelog
  - https://github.com/openai/codex/releases
- **Anthropic:**
  - https://platform.claude.com/docs/en/about-claude/models/overview
  - https://platform.claude.com/docs/en/about-claude/pricing
  - https://platform.claude.com/docs/en/about-claude/model-deprecations
  - https://code.claude.com/docs/en/setup
  - https://code.claude.com/docs/en/model-config
- **Open-weight:**
  - https://api-docs.deepseek.com/quick_start/pricing
  - https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash
  - https://huggingface.co/zai-org/GLM-5.3
  - https://docs.z.ai/guides/overview/pricing
  - https://huggingface.co/moonshotai/Kimi-K3
  - https://huggingface.co/MiniMaxAI/MiniMax-M3
  - https://opencode.ai/docs/config/
  - https://www.kimi.com/code/docs/en/kimi-code/whats-new.html
- **Independientes:**
  - https://artificialanalysis.ai/agents/coding-agents
  - https://artificialanalysis.ai/leaderboards/models
  - https://www.tbench.ai/leaderboard/terminal-bench/4.0
  - https://arcprize.org/leaderboard
  - https://epoch.ai/benchmarks
  - https://labs.scale.com/leaderboard/swe_bench_pro_public_v2
  - https://www.swebench.com/

Advertencias sobre las fuentes:
- El leaderboard bash-only de SWE-bench, que usa mini-swe-agent, no recibe
  resultados desde febrero de 2026 y no incluye modelos actuales.
- METR no ha medido modelos posteriores a mayo de 2026.
- openai.com devolvió 403, así que las cifras `[V]` de OpenAI proceden de
  terceros.
