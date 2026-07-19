# Guía canónica de uso de la flota

Esta guía explica cómo elegir, ejecutar, supervisar, recuperar y cerrar una
flota sin debilitar sus límites de autoridad. El runtime y sus invariantes
siguen siendo la fuente de verdad: `orchestration/router.yaml` define roles y
presets; `workflows/*.yaml` define política; y
`orchestration/INTERNAL_WIRING.md` documenta las propiedades fail-closed.

Para una referencia visual breve de CMUX, consulta
[`guia-operacion-cmux.md`](guia-operacion-cmux.md). Para la receta completa de
WORM local, consulta [`local-worm.md`](local-worm.md).

## Alcance soportado

La ruta endurecida S0 supone una sola Mac confiable, un solo UID Unix confiable
y un único árbol local de runs. No protege contra código hostil arbitrario que
ya corra con ese UID, CONTROL/CMUX/CLI de proveedor comprometidos, coordinación
multi-host ni custodia WORM externa. Sirve para exploración personal local; no
es una frontera multiusuario ni una certificación regulatoria. Tampoco ofrece
un límite duro de tokens totales: los adapters instalados no exponen esa
capacidad.

En pocas palabras, la mejora es comparativa: antes el router vivo, los
lanzamientos y el presupuesto tenían puntos de decisión separados; ahora una
Mission congela el snapshot completo del router y la política, admite cada run
con ownership durable y publica al target solo después de quiescencia. El
beneficio es poder reanudar y auditar sin que una edición tardía o un retry
silencioso cambie lo que se autorizó.

Dos deudas S0 deben conservarse visibles. El `source_event_sha256` de la
evidencia terminal estructurada lo afirma CONTROL, que es confiable dentro de
este alcance; no lo firma un verificador independiente. Además, una Mission
legacy con admissions activas no se puede migrar en vivo sin inventar
procedencia: conserva su evidencia y arranca una Mission actual.

## 1. Elige el flujo correcto

| Necesidad | Entrada | Cuándo usarla |
|---|---|---|
| Misión durable canónica | `just mission` | Trabajo de repositorio que debe compilar política, evaluar riesgo, poder reanudarse y producir archivo verificable. |
| Flota autónoma visible | `just dan` | Implementación, investigación o diagnóstico abiertos con un Lead que decide la delegación. |
| Flota/preset manual | `just fleet` o `just fleet-preset` | Experimentos cuyo modo efectivo proviene del preset; la mayoría son guiados, pero `research` es autónomo. |
| Flujo asegurado | preset `fleet_dialogue` | Evidencia local reforzada para dinero, secretos, destrucción u otros cambios de riesgo alto; producción sigue necesitando fronteras externas apropiadas. |
| Carrera | `just race` | Obtener candidatos en paralelo cuando importa la latencia. El ganador todavía debe verificarse. |
| Worker local aislado | `just triage`, `just review`, `just code` | Análisis prompt-only sobre evidencia suministrada; no recibe filesystem, shell ni Git. |

Mission Control es la entrada canónica. Dan+ es el camino de producto
recomendado cuando no se necesita el ciclo durable completo de Mission Control.
Una tarea pequeña y secuencial puede seguir resolviéndose con un solo agente.

## 2. Preparación

Ejecuta los preflights desde la raíz de este repositorio:

```bash
just check
just workflow-validate
just provider-validate
cmux ping
just status
```

`just check` informa la disponibilidad de Ollama, CMUX, `just`, Python y `jq`;
revisa su salida porque las herramientas opcionales ausentes se reportan sin
convertir el comando en una prueba de salud de cada proveedor. El boot valida el
roster completo antes del primer efecto CMUX. Los roles GLM y MiniMax requieren,
respectivamente, `ZHIPU_API_KEY` y `MINIMAX_API_KEY`; Codex y Claude requieren
sus sesiones autenticadas.

Codex es el Lead predeterminado. Claude solo se selecciona con
`--lead-provider claude` o mediante fallback explícitamente habilitado con
`--allow-fallback`; una falla de Codex no cambia de proveedor por sí sola. Los
despachos Claude, GLM y MiniMax pueden tener costo. El canary Claude del
2026-07-16 reportó USD 0.56816 aunque recibió `--max-budget-usd 0.05`; no uses
inferencia pagada como smoke automático.

El checkout objetivo debe ser un repositorio Git. Dan+ y Mission Control
rechazan por defecto un baseline sucio. `--allow-dirty-baseline` solo reconoce
explícitamente que los clones aislados partirán del `HEAD` ya confirmado; no incorpora
los cambios sin commit del checkout principal.

## 3. Modelo operativo

### Modos y perfiles

Los modos describen quién conduce el trabajo:

| Modo | Propietario de control | Gates |
|---|---|---|
| `autonomous` | Lead | Abre las fases rutinarias del roster, pero conserva identidad, leases, un solo writer y completion exacta. |
| `guided` | Operador o Lead | Requiere avanzar la fase durable antes del despacho. |
| `assured` | CONTROL + FDP-2/FDP-3 + aprobación Mission | Conserva todos los gates y liga la salida de BUILD al evento scoped activo. |

Los perfiles describen el perímetro del proceso:

| Perfil | Límite |
|---|---|
| `native` | Herramientas de proveedor existentes; es el valor predeterminado. |
| `sandboxed` | Estado temporal, caché y runtime dentro del home efímero privado. |
| `regulated` | Requiere identidad Mission y efectos gestionados por CONTROL. |

Un perfil no cambia la autoridad ni `tool_access` declarados por el router. Un
workflow declara política y capacidades disponibles; no puede introducir
comandos, superficies CMUX, identidades de ejecución, autoridad o acceso a
herramientas.

El modo efectivo lo resuelve el preset compilado, no una descripción informal:

| Workflow | Preset principal | Modo efectivo actual |
|---|---|---|
| `implementation` | `dan` | `autonomous` |
| `research` | `research` | `autonomous` |
| `hotfix` | `hotfix_validated` | `guided` |
| `local-worm` | `dan` | `autonomous` |
| `regulated` | `dan` | `autonomous`, solo validación estática por ahora |

Todos los workflows con assurance resuelven `fleet_dialogue` como
`assured`. Los presets sin `mode` explícito (`small`, `audit`,
`implementation_review`, `frontier_verification`, `hotfix_validated`) son
`guided` por contrato.

### Identidad, escritor y evidencia

- Cada intento tiene un `run_id` durable. Guarda el valor exacto devuelto por
  `fleet-send` o `fleet-dispatch` y úsalo en `fleet-wait`.
- Cuando se proporciona `--target-repo`, cada instancia con autoridad de
  escritura obtiene la branch privada `fleet/<feature>/<instance>` dentro de un
  clone aislado bajo `/tmp/fleet_workspaces-$UID`. El target no recibe esa ref
  hasta que CONTROL haya cerrado CMUX, confirmado quiescencia y publicado por
  compare-and-swap. Para trabajo de código, proporciona siempre el repositorio
  objetivo.
- Solo puede existir un writer. Los challengers y verificadores no adquieren su
  autoridad.
- Los paneles sirven para observar. Los archivos, ledgers, receipts y eventos
  autorizados aportan evidencia.
- Los roles OpenCode son filesystem-read sin shell: solo `read`, `glob` y `grep`
  quedan habilitados, `external_directory` se deniega y el launcher valida la
  política resuelta antes de boot; solo se exceptúa el `tool-output` efímero y
  vacío de esa instancia. FDP-2 entrega Git mediante un evidence pack
  durable generado por CONTROL.
- `run_id`, UUID de workspace/surface, proveedor, modelo y, cuando aplica,
  variante deben coincidir. La ambigüedad falla cerrada.
- Un preset que declara `identity_groups` exige tuplas
  `provider/model/variant` distintas dentro de cada grupo antes de tocar CMUX.
  El manifest conserva `identity_group.count` y cada grupo; esto es diversidad
  de identidad auditable, no independencia semántica.
- Los especialistas frontier no se envían prompts entre sí. CONTROL media MCP,
  grants de artefactos CAS y diálogo durable. Los workers Ollama son
  prompt-only, no tienen cliente MCP autenticado y no pueden recuperar un
  artifact ID por sí mismos; su prompt debe contener la evidencia necesaria.

### Plan compilado y admisión

El contrato compilado v2 contiene el `router_snapshot` canónico completo,
`router_digest`, workflow y digests separados de lanzamiento principal y
assurance. Todos los efectos posteriores se vuelven a resolver desde ese
snapshot, no desde el `router.yaml` vivo. El snapshot es evidencia de integridad
y no debe contener secretos. Un contrato v1 se puede leer históricamente pero
no admite efectos nuevos.

La Mission congela deadline, créditos de delegación, máximo de delegaciones
activas, ownership de writer y política de tokens. Antes de cada lanzamiento
Lead/especialista/batch/assured, la admisión avanza siempre así:

```text
reserved → committed → authorized → started → finalized
```

Esa es la ruta de un efecto externo. Un request `reserved` o `committed` que se
abandona antes del launch se aborta con todos sus bindings exactos y no
reembolsa créditos. Una falla `authorized` antes de `started` puede finalizar
con evidencia terminal exacta, sin inventar un lanzamiento.

`reserved` reclama run, destinatario, créditos y writer; `committed` enlaza la
petición exacta. `effect_sha256` congela prompt/objetivo, inputs, contrato de
salida, grants, identidad de proveedor, runner y, cuando aplica,
wrapper/transporte; `task_sha256` congela la tarea enviada. `authorized` es el
último gate durable y con deadline antes del efecto externo. `started`
referencia esa autorización, incluso si el reloj vence justo después.
`finalized` libera ownership solo con evidencia estructurada que
usa schema version 1 y coincide en `source_event_sha256`, `run_id`,
`task_sha256`, estado terminal, destinatario y writer. Los retries idempotentes
reutilizan esa identidad; los créditos consumidos no se reembolsan y solo puede
haber un writer activo.

Hay dos lanes sin solapamiento: `CONTROL` para la flota principal y `ASSURED`
para assurance. CONTROL no puede crear admissions ASSURED; ASSURED no puede
heredar el Lead histórico ni mutar admissions CONTROL. No se solicita el cambio
de lane, no se completa y no se archiva mientras quede una admission activa.

La política de tokens distingue capacidad real del proveedor. Un soft budget
positivo bloquea si el uso es desconocido; soft cero desactiva este gate. Hoy
Codex, Claude y OpenCode no exponen totales confiables y Ollama solo puede
limitar output, de modo que ningún adapter ofrece `hard_total`. El presupuesto
local del router sigue existiendo para `fleet-dispatch` standalone, pero es el
gate heredado por feature de `just dan`/`just fleet`/`just race`, no la admisión
global de una Mission.

Cada usage receipt es un sobre cerrado schema v1 con
`state=observed|not_incurred|unknown`. Solo `not_incurred` representa cero
exacto; `unknown` no lleva conteos. Si falta una fuente o aparece cualquier
receipt `unknown`, el agregado conserva `total_tokens=null` y nunca lo convierte
en cero.

El contrato de salida de un worker es:

```text
STATUS: <DONE | BLOCKED | FAILED>
SUMMARY:
EVIDENCE:
RISKS:
NEXT_ACTION:
```

## 4. Ejecución cotidiana

### Receta local sin inferencia frontier

Para aprender y comprobar el ciclo CMUX → dispatch → wait → teardown sin
activar Codex, Claude, GLM ni MiniMax:

```bash
FLEET_NO_LEAD=1 ./scripts/fleet-up.sh local-safe triage=triage
just advance local-safe RECON local-safe-scope
dispatch="$(./scripts/fleet-dispatch.sh local-safe triage \
  "Resume únicamente la evidencia incluida aquí." --json)"
run_id="$(jq -er '.run_id' <<<"$dispatch")"
./scripts/fleet-wait.sh local-safe triage \
  --run "triage=$run_id" --timeout 600 --json
./scripts/fleet-down.sh local-safe
```

`FLEET_NO_LEAD=1` crea un monitor CONTROL sin proveedor. El único turno de
modelo es Ollama local; el worker no recibe MCP ni acceso al repositorio.

### Mission Control

Primero puedes compilar, evaluar riesgo y ver la resolución sin crear Mission ni
tocar CMUX:

```bash
just mission-dry <feature> "<objetivo completo>" \
  --workflow implementation --target-repo <repo>
```

Ejecuta la misión durable:

```bash
just mission <feature> "<objetivo completo>" \
  --workflow implementation --target-repo <repo> --teardown
```

Mission Control crea un `mission_id`, enlaza un único resultado Lead, registra
intentos y efectos antes de ejecutarlos y archiva la evidencia antes de aceptar
éxito. Una misión de riesgo alto o desconocido se detiene antes de bootear hasta
recibir una aprobación limitada a la misión, workflow, alcance, riesgo,
identidad del operador local y expiración:

```bash
just mission-show <mission_id>
just mission-approve <mission_id> <target-repo> \
  --idempotency-key <clave-estable-y-unica>
just mission-resume <mission_id>
```

Si una aprobación expiró, renuévala explícitamente y después reanuda:

```bash
python3 scripts/fleet-approve.py --runs-dir <runs-dir> \
  --mission-id <mission-id> --scope <target-repo> --renew \
  --idempotency-key <clave-estable-de-esta-renovacion>
just mission-resume <mission-id>
```

`--renew` funciona solo después de la expiración. Se admite antes del boot de
assurance o en `assured_running` cuando no existen run claims, admissions
activas ni writer activo; en el segundo caso sustituye la aprobación actual sin
repetir boot/start. No amplía request, workflow, scope ni riesgo. No la uses para
una aprobación todavía vigente ni mientras haya trabajo ASSURED activo: ambos
casos fallan cerrados.

Reutiliza la misma clave solo para reintentar esa renovación exacta. Si la
aprobación renovada vuelve a expirar en otro ciclo, usa una clave nueva.

La aprobación actual se vuelve a comprobar por workflow/scope/risk y expiración
al boot/start y en cada `reserved`, `committed` y `authorized` ASSURED. La
autorización sella el hash de ese evento de aprobación. Un run ya autorizado
puede registrar su start y terminal exactos aunque la aprobación expire después;
no cruza otro efecto con esa autoridad vencida.

Cuando un Lead llama `request_assurance`, Fleet Control solo registra/eleva el
riesgo. La Mission no cambia de lane mientras ese caller siga activo. El driver
espera los terminales exactos, finaliza Lead e hijos y recién entonces registra
`assurance_requested` para pedir la aprobación humana.

Usa `sandboxed` explícitamente cuando necesites el perímetro efímero estricto:

```bash
just mission <feature> "<objetivo>" \
  --execution-profile sandboxed --target-repo <repo>
```

### Dan+ autónomo

```bash
just dan-dry <feature> "<objetivo completo>" --target-repo <repo>
just dan <feature> "<objetivo completo>" --target-repo <repo>
```

El preset `dan` muestra `lead`, `scout`, `builder`, `challenger` y `verifier`.
El Lead decide cuáles necesita, delega y sintetiza. La flota queda visible al
terminar; añade `--teardown` si quieres cierre automático tras éxito. No envíes
manualmente otra misión al panel Lead: el primer envío ya es la misión rastreada.

### Flotas manuales y presets

Lista los presets reales:

```bash
./scripts/fleet-up.sh --list-presets
```

Los presets disponibles son `dan`, `small`, `audit`, `implementation_review`,
`frontier_verification`, `fleet_dialogue`, `hotfix_validated` y `research`.

| Preset | Modo y roster | Uso |
|---|---|---|
| `dan` | autónomo: lead, scout, builder, challenger, verifier | Misión abierta con delegación dinámica. |
| `small` | guiado: solo lead | Tarea acotada y secuencial. |
| `audit` | guiado: analysis, challenge, verify | Perspectivas con identidad diversa y revisión supplied-diff. |
| `implementation_review` | guiado: build, verify | Un writer y un reviewer local con identidad distinta. |
| `frontier_verification` | guiado: build, challenge, verify | Writer, GLM read-only y Claude verifier. |
| `fleet_dialogue` | asegurado: maker, checker, challenge, verify | FDP-2 y FDP-3 con gates completos. |
| `hotfix_validated` | guiado: candidate_codex, candidate_minimax, verify | Candidatos paralelos que aún requieren verificación. |
| `research` | autónomo: triage_scope, triage_sources, research, challenge | Investigación sin writer con síntesis heterogénea. |

Ejemplos:

```bash
just fleet <feature>
just fleet-preset <feature> audit
./scripts/fleet-up.sh <feature> --preset implementation_review \
  --target-repo <repo>
./scripts/fleet-up.sh <feature> --target-repo <repo> \
  build=codex verify=reviewer
```

Una flota guiada nace en `CONTROL`. Avanza a la fase de la instancia antes del
despacho; por ejemplo:

```bash
just advance <feature> RECON <referencia-de-evidencia>
# o, para un writer:
just advance <feature> BUILD <referencia-de-evidencia>
just send <feature> build "<tarea acotada>"
```

`just send` sirve para instancias interactivas y devuelve el `run_id`. Para un
worker local usa el wrapper local:

```bash
./scripts/fleet-dispatch.sh <feature> <instance> "<tarea>" --json
```

Espera siempre la ejecución exacta:

```bash
./scripts/fleet-wait.sh <feature> <instance> \
  --run <instance>=<run_id> --timeout 600 --json
```

Para varias instancias añade un `--run <instance>=<run_id>` por cada una. `--any`
termina con el primer `succeeded`; sin `--any`, espera todos los runs indicados.

## 5. Evidencia y cierre

Durante una misión usa:

```bash
just status
just mission-show <mission_id>
just mission-control-health <mission_id>
```

Después del terminal verifica evidencia durable. Ejecuta la verificación de
auditoría cuando el workflow haya requerido el AuditService asegurado:

```bash
just mission-audit-verify <mission_id>  # si la misión requirió auditoría
just mission-report <mission_id>
just mission-trace <mission_id>
just mission-archive-verify <archive>
```

Los reports, traces y exporters son observacionales: leen evidencia ya durable,
no deciden completion ni pueden cambiar el terminal. Un archivo `full` que
incluya `credentials` o `private_data` necesita una aprobación separada:

```bash
just mission-approve-archive <mission_id> <target-repo> \
  --idempotency-key <clave-unica-de-aprobacion-de-archivo>
```

Cierra una flota manual solo después de reconciliar trabajo, runs y gates:

```bash
just fleet-down <feature>
```

El teardown valida identidad, impide nuevos despachos, rechaza leases activos y
misiones no terminales y verifica los receipts requeridos. Primero cierra CMUX
y retira cualquier clone writer del path visible al modelo; después publica el
commit exacto mediante intent durable y compare-and-swap. En una flota ligada a
Mission crea y verifica entonces el archivo Mission, y por último mueve el
manifest, estado, ledgers y receipts de la flota a
`orchestration/runs/archive/`.

La transición interna a assurance es distinta del teardown final. `mission-run`
usa `fleet-down.sh <feature> --handoff-assurance` solo después de que todas las
admissions CONTROL estén finalizadas. Es idempotente y recuperable: detiene la
generación principal `missions/<mission_id>/control/`, conserva manifest,
estado, ledgers y receipts en
`missions/<mission_id>/assurance-handoff/main-runtime/`, no crea el archivo
final y no toca `missions/<mission_id>/control-assured/`. Como operador usa
`just mission-resume`; invoca el flag directamente solo al seguir un diagnóstico
de recuperación que identifique esa misma Mission. El handoff publica un
marcador durable de quiescencia antes de drenar writers ya admitidos: una
mutación que recibe `mission mutations are quiesced` no fue aplicada ni quedó
encolada y debe reintentarse explícitamente con la misma idempotency key después
del handoff o su recovery. Las esperas de READY y COMMIT tienen deadline y el
timeout termina y reapea únicamente el proceso hijo exacto. La adquisición del
boot-lock también tiene deadline; si el helper termina antes de responder, la
espera falla inmediatamente. `mission-run` usa un límite exterior mayor que
la suma de esas esperas y la gracia de teardown; si aun así vence, termina el
grupo temporal completo para no dejar descendientes reteniendo la barrera. Los
overrides `FLEET_BOOT_LOCK_READY_TIMEOUT_SECONDS`,
`FLEET_HANDOFF_READY_TIMEOUT_SECONDS` y
`FLEET_HANDOFF_COMMIT_TIMEOUT_SECONDS` se validan antes de CMUX, locks o
publicación.

## 6. Completion y carreras

Un `Stop`, una notificación, un `SessionEnd` o texto visible no prueban éxito.
Solo el ledger terminal del `run_id` exacto, con la cadena de procedencia e
identidad de proveedor verificadas, puede cerrar un run. Los eventos despiertan
la reconciliación; el ledger decide.

`fleet-wait` usa estos códigos:

| Código | Estado | Acción operativa |
|---|---|---|
| `0` | `succeeded` | Consumir el resultado durable y continuar con el siguiente gate. |
| `1` | `failed` | Corregir la causa y crear un nuevo despacho rastreado si procede. |
| `2` | uso, identidad o protocolo | Corregir argumentos o la discrepancia de manifest/UUID/protocolo; no reintentar a ciegas. |
| `3` | `blocked` | Leer `EVIDENCE` y `NEXT_ACTION`, resolver la decisión o precondición externa y reanudar o despachar explícitamente. Nunca convertirlo en éxito. |
| `4` | `abandoned` | Confirmar que el abandono fue deliberado; crear otro run solo si todavía se necesita el trabajo. |
| `5` | `indeterminate` | Preservar evidencia y, en frontier, el lease retenido. Investigar identidad, transcript, gaps y ledger; no aceptar el resultado ni iniciar otro run sobre la misma superficie. |
| `124` | deadline del waiter | El waiter escaló y terminó, no el run. Inspeccionar estado; si sigue vivo, volver a esperar el mismo `run_id` con un timeout apropiado. |

Para `blocked`, registra o satisface la acción humana/precondición y usa el flujo
de reanudación que corresponda; un nuevo prompt implica un nuevo run rastreado.
Para `indeterminate`, conserva la superficie y el lease hasta recuperar evidencia
o confirmar que el agente está quiescente. Para timeout, no asumas fallo ni
éxito: revisa el mismo run y nunca abandones solo porque transcurrió tiempo.

Una carrera se inicia con:

```bash
just race <nombre> "<tarea>" codex_candidate minimax_candidate
```

Los roles predeterminados son `codex_candidate` y `minimax_candidate`. La primera
ejecución `succeeded` es un **candidato no verificado**, no aceptación. Los demás
runs se conservan por defecto. `--cancel-losers` solo solicita interrupción; en
frontier, la interrupción no confirmada queda `indeterminate` y retiene su lease.
Verifica el candidato mediante tests deterministas y revisión con identidad distinta
antes de actuar, integrar, cancelar evidencia o cerrar la flota.

La cancelación local falla cerrada: hoy el worker Ollama no publica un handle de
proceso ligado al `run_id`. Fleet Control rechaza el cancel y no envía `ctrl-c`
a una superficie CMUX reutilizable, porque podría detener un run posterior.
Espera o reconcilia el `run_id` exacto; un cancel rechazado no significa que el
worker se haya detenido.

## 7. Recuperación

### Mission y Mission Control

```bash
just mission-show <mission_id>
just mission-resume <mission_id>
just mission-control-health <mission_id>
just mission-control-start <mission_id>
```

`mission-resume` reconcilia efectos ya registrados y adopta el run exacto cuando
existe evidencia inequívoca; no debe repetir un efecto reconocido. El start de
Mission Control es idempotente y sirve para recuperar el socket privado después
de una caída del runner.

### Teardown normal y workspace ausente

```bash
just fleet-down <feature>
```

Si el workspace fue eliminado deliberadamente fuera del wrapper, confirma
primero su ausencia por UUID. Solo entonces:

```bash
./scripts/fleet-down.sh <feature> --recover-absent
```

`--recover-absent` falla si la ausencia no está positivamente confirmada. No
edites el manifest para forzarla.

### Gap frontier o envío parcial

Un gap de auditoría irrecuperable, un envío parcial o una interrupción no
confirmada terminaliza `indeterminate` y conserva el lease. Después de confirmar
que el agente está quiescente:

```bash
./scripts/fleet-abandon.sh <feature> <instance> <run_id> <razon>
# equivalente:
just abandon <feature> <instance> <run_id> <razon>
```

No hay reclaim por PID, TTL o antigüedad. Los leases solo se liberan por terminal
durable o por evidencia positiva de que el workspace/surface propietario ya no
existe. Un error del probe conserva el lease.

### Writer, branches y flujos activos

- Un writer dirty bloquea teardown. Revisa su clone aislado y conserva el
  trabajo en su branch privada mediante un commit válido, o descártalo
  únicamente si el propietario lo decide y ha identificado exactamente el
  clone.
- Si la branch privada avanzó respecto al baseline, teardown retira primero el
  clone del path visible al modelo, persiste un intent de publicación, importa
  el commit y crea la branch del target por compare-and-swap. No borres esa
  branch ni el intent para "limpiar" una recuperación pendiente.
- Una Mission no terminal bloquea teardown. Usa `mission-show` y
  `mission-resume` o terminalízala por el mecanismo explícito correspondiente.
- Un FDP-2 activo bloquea teardown. Si la flota alcanzó CHALLENGE o VERIFY,
  FDP-3 también debe ser terminal. Usa `fdp2-show`/`fdp3-show`; abandona solo con
  motivo e idempotency key explícitos cuando no sea posible continuar.

## 8. FDP-2 y FDP-3

Este procedimiento es intencionalmente explícito. No elimines pasos ni gates.

### FDP-2: Maker–Checker bounded

1. Inicia el roster asegurado, entra a BUILD con evidencia y congela una tarea
   acotada con objetivo, alcance negativo y criterios observables:

   ```bash
   ./scripts/fleet-up.sh <feature> --preset fleet_dialogue --target-repo <repo>
   just advance <feature> BUILD <evidencia-de-scope>
   just fdp2-start <feature> <spec-json> <idempotency-key-estable>
   ```

2. Consulta `just fdp2-show <feature>` y ejecuta exactamente el `next_action`:

   - `dispatch`: lee el `prompt_file` byte por byte, usa `fleet-send.sh` contra
     la `instance` indicada, espera el `run_id` exacto y reconoce el resultado:

     ```bash
     feature=<feature>
     instance=<instance-devuelta>
     prompt_file=<prompt_file-devuelto>
     prompt="$(<"$prompt_file")"
     dispatch="$(./scripts/fleet-send.sh "$feature" "$instance" "$prompt" --json)"
     run_id="$(jq -er '.run_id' <<<"$dispatch")"
     ./scripts/fleet-wait.sh "$feature" "$instance" \
       --run "$instance=$run_id" --timeout 1800 --json
     just fdp2-step-run "$feature" "$run_id" <nueva-key>
     ```
   - `publish`: publica el resultado exacto con todos los bindings devueltos y
     luego reconoce el `message_id` exacto:

     ```bash
     python3 scripts/fleet_dialogue.py publish orchestration/runs \
       --feature <feature> --kind <kind> --recipient <recipient> \
       --source-instance <source_instance> --source-run-id <source_run_id> \
       --idempotency-key <key> [--reply-to <message_id>]
     just fdp2-step-message <feature> <message_id-devuelto> <nueva-key>
     ```

   - `terminal`: detén el loop. Un run o mensaje tardío no cambia el terminal.

3. Cada resultado Maker debe ser exactamente un commit nuevo, limpio y
   append-only sobre la base aceptada. El Checker es MiniMax read-only y su JSON
   es estricto. Contrato malformado, identidad/variante incorrecta, timeout,
   deadline o exceso de rondas cierran fail-closed.

4. `accepted` no abandona BUILD. El gate exige procedencia de aprobación y
   vuelve a comprobar el HEAD limpio exacto:

   ```bash
   python3 scripts/fleet_state.py advance \
     orchestration/runs/fleet-<feature>.manifest CHALLENGE \
     --evidence <evidencia-del-dialogo-aceptado> \
     --approved-by <attestation-del-operador>
   ```

   Ese comando es el camino standalone heredado y el valor es una etiqueta, no
   prueba criptográfica de presencia humana. Si el manifest contiene
   `mission_id`, `--approved-by` se rechaza: `mission-run` consume el
   evento de aprobación scoped actual (`assurance_approved` o
   `assurance_approval_renewed`) y el assured runner pasa su
   `--approval-event-sha256` exacto.

### FDP-3: GLM CHALLENGE y Claude VERIFY

1. Inicia FDP-3 solo después del gate ligado a la aprobación y desde el HEAD
   exacto aceptado por FDP-2:

   ```bash
   just fdp3-start <feature> <idempotency-key-estable>
   ```

2. Ejecuta cada `next_action` de `just fdp3-show <feature>`:

   - `dispatch`: envía el prompt exacto, espera el `run_id` exacto y usa
     `just fdp3-step-run`.
   - `publish`: publica el resultado exacto y usa `just fdp3-step-message` con el
     `message_id` devuelto.
   - `advance_phase`: avanza CHALLENGE a VERIFY usando el hash `evidence`
     devuelto y después reconoce la transición:

     ```bash
     python3 scripts/fleet_state.py advance \
       orchestration/runs/fleet-<feature>.manifest VERIFY \
       --evidence <fdp3-control-head>
     just fdp3-step-phase <feature> <nueva-key>
     ```

   - `terminal`: detén el flujo; el terminal es inmutable.

GLM publica findings, no verdict. Claude revisa por separado cada finding
y solo puede cerrar `verified` o `rejected`. No hay retries ni fallback de
proveedor. JSON malformado, timeout, identidad desviada, evidencia ausente o
snapshots dirty cierran fail-closed.

Después de un teardown válido, verifica los archivos sin CMUX ni proveedores:

```bash
python3 scripts/fleet_dialogue_controller.py verify --archive <archive>
python3 scripts/fleet_assurance_controller.py verify --archive <archive>
just mission-archive-verify <archive-mission>
```

## 9. WORM

`signed` y `worm` no son sinónimos:

- `signed` conserva una cadena de auditoría autenticada y un receipt agregado
  Ed25519 verificable offline, pero declara `worm=false`.
- `worm` además exige un sink S3-compatible con versionado y Object Lock
  `COMPLIANCE`, version ID exacto, retención futura y un receipt completo por
  evento. `worm=true` no implica por sí solo cumplimiento externo.

### WORM local

La receta local usa RustFS sobre HTTPS loopback con CA local validada:

```bash
just worm-local-setup
STATE_DIR="/tmp/agent-fleet-worm-local-$UID"
set -a
. "$STATE_DIR/worm.env"
set +a
just worm-local-smoke
just worm-local-delete-test
just worm-local-teardown
```

El smoke usa el runtime real para comprobar versionado, Object Lock
`COMPLIANCE`, version ID, retain-until, digest, rechazo del borrado de la versión
exacta y persistencia del objeto. Los receipts llevan
`trust_scope=local-development`. HTTP, CA inválida, archivo CA ausente,
`verify=False` y `--insecure` no están soportados.

Esta evidencia **nunca** satisface `regulated` ni `external-compliance`. El dueño
del computador puede eliminar el volumen Docker, por lo que no hay custodia
independiente ni retención off-machine. El teardown comprueba nombres exactos,
state marker y ownership label antes de eliminar el contenedor o volumen; una
discrepancia falla sin borrar recursos.

### WORM externo y regulated

La ruta regulada no está disponible para efectos con los adapters actuales. Su
workflow puede pasar validación estática, pero la compilación de efectos exige
un presupuesto `hard_total`; Codex, Claude y OpenCode no aportan totales
confiables y Ollama solo puede limitar output. Además exige infraestructura WORM
externa e independiente accesible por HTTPS, bucket con versionado y Object Lock
`COMPLIANCE`, credenciales válidas y DNS que resuelva solo a direcciones
públicas globales prevalidables. Rechaza localhost, loopback, link-local, redes
privadas, respuestas DNS mixtas y cambios DNS después del preflight. No existe
fallback externo→local ni WORM→signed.

```bash
just mission-dry <feature> "<objetivo regulado>" \
  --workflow regulated --execution-profile regulated --target-repo <repo>
just mission <feature> "<objetivo regulado>" \
  --workflow regulated --execution-profile regulated --target-repo <repo>
```

Hoy ambos comandos sirven para comprobar que la ruta falla cerrada antes de
CMUX; no son una receta para completar una misión regulada. Solo serán
operativos cuando todos los proveedores resolubles ofrezcan `hard_total` y se
configure WORM externo real. RustFS local nunca cubre esa segunda condición.

El backend se configura mediante `FLEET_WORM_BUCKET`, `FLEET_WORM_REGION` (o
`AWS_REGION`/`AWS_DEFAULT_REGION`), `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, opcionalmente `AWS_SESSION_TOKEN` y, cuando corresponda,
`FLEET_WORM_ENDPOINT`, `FLEET_WORM_CA_FILE`, `FLEET_WORM_RETENTION_DAYS`,
`FLEET_WORM_PREFIX` y `FLEET_WORM_BACKEND_NAME`. No copies credenciales ni CA
privadas al repositorio. Si se omite `FLEET_WORM_CA_FILE`, TLS usa el trust store
normal del sistema; si se configura, debe ser un archivo regular, no symlink,
legible, no vacío y válido. Nunca se desactiva la verificación TLS. El preflight
ocurre antes de crear la flota CMUX.

## 10. Límites de confianza

Estas reglas no tienen atajos:

1. Escribir o usar `cmux send` directamente puede cambiar lo visible, pero queda
   **untracked**. No puede satisfacer `control-v1` ni producir completion
   aceptable.
2. Un workflow no puede cambiar autoridad, `tool_access`, comandos, superficies
   ni identidades de ejecución. El router y CONTROL conservan esos límites.
3. Reports, traces y exporters son observacionales. Un fallo de exportación no
   altera el terminal y una exportación exitosa no crea éxito.
4. Evidencia `local-development` nunca se presenta como regulatoria o
   `external-compliance`.
5. El primer resultado de una race es un candidato, no un resultado verificado.
6. No borres clones/staging de writer, branches, intents, manifests, leases, sockets, snapshots,
   contenedores, volúmenes ni archivos sin demostrar identidad y ownership.
7. No reclames recursos por antigüedad. Un probe fallido o ambiguo conserva el
   estado para investigación.
8. No reduzcas FDP-2/FDP-3 a prompts informales: conserva tarea bounded,
   `run_id`, `message_id`, idempotency keys, aprobación humana, gates de fase,
   receipts y verificación offline.
