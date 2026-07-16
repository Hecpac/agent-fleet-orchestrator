# Plantilla de validación de flota

Usa esta plantilla desde el orquestador para probar un preset por el camino real
de la flota. Sustituye todos los valores `<...>` antes de ejecutar. Los nombres
de comandos, fases, estados y campos contractuales se conservan en inglés.

## Parámetros del intento

```text
FEATURE=<identificador único y efímero>
PRESET=<preset de orchestration/router.yaml>
TARGET_REPO=<ruta absoluta del repositorio objetivo>
OBJECTIVE=<resultado concreto que debe producir la flota>
NEGATIVE_SCOPE=<acciones y archivos expresamente prohibidos>
EXPECTED_INSTANCES=<instance_id separados por espacios, sin incluir CONTROL>
EXPECTED_IDENTITY_GROUPS=<grupos separados por ;, miembros separados por coma>
TIMEOUT_SECONDS=<límite por fase>
EVIDENCE_DIR=<directorio durable para resultados y reporte final>
```

Antes del arranque, confirma en `orchestration/router.yaml` que cada instancia
esperada pertenece al preset y anota su `runner`, `phase`, `authority`,
`tool_access`, provider, model y variant. Copia también los `identity_groups`
declarados: cada grupo debe tener tuplas `provider/model/variant` distintas.
CONTROL coordina el lifecycle; solo se incluye
en `EXPECTED_INSTANCES` si recibe una tarea cuyo resultado deba evaluarse.

## Contratos no negociables

- Usa `fleet-up`, `fleet-send`, `fleet-dispatch`, `fleet-wait`, `fleet-abandon`
  y `fleet-down`; no controles panes directamente.
- Trata `orchestration/router.yaml` como source of truth. No sustituyas roles,
  modelos o autoridad para conseguir un PASS.
- Avanza las fases en su orden configurado y siempre con evidencia durable.
- Conserva el `run_id` devuelto por cada envío. Toda espera debe nombrar el par
  exacto `instance=run_id`; una notificación o un Stop solo despierta la
  reconciliación y nunca demuestra éxito.
- Consume la salida canónica `--json`; no extraigas identidad o estado del texto
  dirigido a humanos ni del terminal chrome.
- Una instancia read-only no modifica el repositorio. Una instancia con write
  authority solo trabaja mediante `--target-repo` y su worktree dedicado.
- Falla cerrado ante timeout, `blocked`, `failed`, `abandoned`, `indeterminate`,
  evidencia ausente, identidad ambigua o un `STATUS` inválido.
- No uses polling ni sleeps para esperar agentes. Usa `fleet-wait`.
- No llames “independientes” a dos runs por cantidad. Solo un grupo declarado y
  validado puede reportarse como identity-diverse, y aun así no prueba verdad.
- No hagas teardown mientras exista un run activo o indeterminado. Confirma que
  el agente está quiescent y usa `fleet-abandon` explícitamente cuando corresponda.

## 1. Preflight y baseline

1. Confirma que no existe una flota previa con el mismo `FEATURE` y ejecuta
   `cmux ping` únicamente como prueba de disponibilidad.
2. Captura branch, HEAD y `git status --short` de `TARGET_REPO` como baseline.
3. Resuelve el plan sin mutaciones y verifica que coincide con
   `EXPECTED_INSTANCES` y `EXPECTED_IDENTITY_GROUPS`.
4. Arranca el preset por el wrapper soportado:

   ```bash
   just fleet-preset <feature> <preset>
   ```

   Para un preset con write authority, usa en cambio:

   ```bash
   ./scripts/fleet-up.sh <feature> --preset <preset> --target-repo <ruta-absoluta>
   ```

5. Conserva el manifest y el state file como evidencia. Si el preflight falla,
   el intento es FAIL: no elimines ni reemplaces miembros del preset.

## 2. Ejecución por fase

Para cada fase configurada, avanza el gate con una referencia durable:

```bash
just advance <feature> <PHASE> <evidence-path-or-gate-id>
```

Envía la misma tarea acotada a las instancias identity-diverse de esa fase. Para
un agente frontier:

```bash
./scripts/fleet-send.sh <feature> <instance> "<task>" --json
```

Para un worker local:

```bash
./scripts/fleet-dispatch.sh <feature> <instance> "<evidence-pack-and-task>" --json
```

Guarda cada JSON de envío sin transformarlo. Después espera los runs exactos de
la fase; puedes nombrar varias instancias en una sola espera:

```bash
./scripts/fleet-wait.sh <feature> <instance> \
  --run <instance>=<run_id> --timeout <seconds> --json
```

No avances a la fase siguiente hasta validar para cada instancia:

- resultado terminal `succeeded` y `exit_code=0`;
- para un worker local, `result_file` real, no vacío y atribuible al mismo
  `run_id`; para un agente frontier, evento terminal y transcript estructurado
  ligados al mismo run mediante el sentinel verificado;
- bloque final con `STATUS: DONE`;
- `EVIDENCE` suficiente para revisar las afirmaciones;
- ausencia de acciones fuera de `OBJECTIVE` y `NEGATIVE_SCOPE`.

Cuando una fase posterior no tiene acceso al repositorio, entrégale un evidence
pack autosuficiente: objetivo, alcance negativo, baseline, extractos fuente
necesarios y resultados completos de las fases anteriores. Nunca le pidas que
invente la evidencia que no puede leer.

## 3. Consolidación fail-closed

El intento solo es PASS si se cumplen todas estas condiciones:

- todas las instancias de `EXPECTED_INSTANCES` produjeron exactamente un run
  terminal `succeeded` con `STATUS: DONE`;
- las conclusiones incluyen evidencia verificable y las discrepancias están
  resueltas o declaradas como riesgo;
- branch, HEAD, diff y archivos sin seguimiento respetan la política de
  escritura definida para el preset;
- no quedan leases, runs activos ni estados indeterminados;
- el teardown termina limpio y preserva toda evidencia durable.

Cualquier condición ausente produce FAIL. No conviertas éxito parcial en PASS.

## 4. Teardown y no-regresión

1. Lee una vez los resultados y surfaces después del evento terminal; no uses la
   pantalla como prueba de completion.
2. Si un frontier run quedó indeterminado, confirma primero que su agente está
   quiescent y libera la lease de forma explícita:

   ```bash
   ./scripts/fleet-abandon.sh <feature> <instance> <run_id> "<reason>"
   ```

3. Verifica la identidad del workspace desde el manifest y ejecuta:

   ```bash
   just fleet-down <feature>
   ```

4. Compara el estado Git final con el baseline. En una validación read-only,
   `git diff --exit-code`, HEAD y `git status --short` deben demostrar cero
   modificaciones atribuibles a la flota.
5. Confirma que el manifest fue archivado y que no quedan workspace ni leases
   activos para `FEATURE`.

## 5. Reporte obligatorio

```text
STATUS: PASS | FAIL
FEATURE:
PRESET:
TARGET_REPO:
BASE_SHA:
FINAL_SHA:
EXPECTED_INSTANCES:
IDENTITY_GROUPS: <miembros y tupla provider/model/variant de cada uno>
RUNS: <instance=run_id, terminal status, exit_code, result_file cuando aplique>
WORKER_RESULTS:
DISAGREEMENTS:
GIT_INTEGRITY:
TEARDOWN:
EVIDENCE:
ROUND_TRIP:
OPEN_LANES:
NEXT_ACTION:
```

`OPEN_LANES` nunca se omite: enumera presets, providers, fases o caminos que no
se ejercieron, por qué quedaron fuera y cómo podrían probarse en otro intento.

## Caso de smoke aprobado: preset `audit`

```text
PRESET=audit
TARGET_REPO=<raíz de agent-fleet-orchestrator>
OBJECTIVE=Auditar la coherencia entre orchestration/router.yaml, README.md y
  .agents/skills/cmux/SKILL.md.
NEGATIVE_SCOPE=No modificar archivos, no ejecutar acciones destructivas y no
  ampliar la revisión fuera de esos tres documentos.
EXPECTED_INSTANCES=analysis challenge verify
EXPECTED_IDENTITY_GROUPS=analysis,challenge,verify
```

Distribución obligatoria:

1. `analysis` (RECON, frontier read-only) inspecciona las tres fuentes y entrega
   hallazgos con citas `path:line`.
2. `challenge` (CHALLENGE, frontier read-only) repite la revisión con una
   identidad distinta y busca contradicciones u omisiones del primer análisis.
3. `verify` (VERIFY, local) recibe un evidence pack con los tres documentos y
   ambos resultados; decide si las conclusiones están respaldadas.

El smoke `audit` solo pasa si los tres runs cumplen el contrato, Git permanece
sin cambios y `fleet-down` cierra la flota sin perder la evidencia.
