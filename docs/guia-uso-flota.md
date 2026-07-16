# Guía canónica de uso de la flota

Esta guía explica cómo elegir, ejecutar, supervisar, recuperar y cerrar una
flota sin debilitar sus límites de autoridad. El runtime y sus invariantes
siguen siendo la fuente de verdad: `orchestration/router.yaml` define roles y
presets; `workflows/*.yaml` define política; y
`orchestration/INTERNAL_WIRING.md` documenta las propiedades fail-closed.

Para una referencia visual breve de CMUX, consulta
[`guia-operacion-cmux.md`](guia-operacion-cmux.md). Para la receta completa de
WORM local, consulta [`local-worm.md`](local-worm.md).

## 1. Elige el flujo correcto

| Necesidad | Entrada | Cuándo usarla |
|---|---|---|
| Misión durable canónica | `just mission` | Trabajo de repositorio que debe compilar política, evaluar riesgo, poder reanudarse y producir archivo verificable. |
| Flota autónoma visible | `just dan` | Implementación, investigación o diagnóstico abiertos con un Lead que decide la delegación. |
| Flota guiada | `just fleet` o `just fleet-preset` | Experimentos manuales donde el operador controla fases y despachos. |
| Flujo asegurado | preset `fleet_dialogue` | Producción, dinero, secretos, destrucción u otros cambios de riesgo alto que requieren FDP-2, aprobación humana y FDP-3. |
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

El checkout objetivo debe ser un repositorio Git. Dan+ y Mission Control
rechazan por defecto un baseline sucio. `--allow-dirty-baseline` solo reconoce
explícitamente que los worktrees partirán del `HEAD` ya confirmado; no incorpora
los cambios sin commit del checkout principal.

## 3. Modelo operativo

### Modos y perfiles

Los modos describen quién conduce el trabajo:

| Modo | Propietario de control | Gates |
|---|---|---|
| `autonomous` | Lead | Abre las fases rutinarias del roster, pero conserva identidad, leases, un solo writer y completion exacta. |
| `guided` | Operador o Lead | Requiere avanzar la fase durable antes del despacho. |
| `assured` | CONTROL + FDP-2/FDP-3 + humano | Conserva todos los gates, incluida la aprobación humana para abandonar BUILD. |

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

### Identidad, escritor y evidencia

- Cada intento tiene un `run_id` durable. Guarda el valor exacto devuelto por
  `fleet-send` o `fleet-dispatch` y úsalo en `fleet-wait`.
- Cuando se proporciona `--target-repo`, cada instancia con autoridad de
  escritura obtiene la branch `fleet/<feature>/<instance>` y un worktree aislado
  bajo `/tmp/fleet_workspaces-$UID`. Para trabajo de código, proporciona siempre
  el repositorio objetivo.
- Solo puede existir un writer. Los challengers y verificadores no adquieren su
  autoridad.
- Los paneles sirven para observar. Los archivos, ledgers, receipts y eventos
  autorizados aportan evidencia.
- `run_id`, UUID de workspace/surface, proveedor, modelo y, cuando aplica,
  variante deben coincidir. La ambigüedad falla cerrada.

El contrato de salida de un worker es:

```text
STATUS: <DONE | BLOCKED | FAILED>
SUMMARY:
EVIDENCE:
RISKS:
NEXT_ACTION:
```

## 4. Ejecución cotidiana

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
identidad humana y expiración:

```bash
just mission-show <mission_id>
just mission-approve <mission_id> <target-repo> \
  --idempotency-key <clave-estable-y-unica>
just mission-resume <mission_id>
```

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

### Flota guiada

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
| `audit` | guiado: analysis, challenge, verify | Perspectivas independientes y revisión supplied-diff. |
| `implementation_review` | guiado: build, verify | Un writer y un reviewer local independiente. |
| `frontier_verification` | guiado: build, challenge, verify | Writer, GLM read-only y Claude verifier. |
| `fleet_dialogue` | asegurado: maker, checker, challenge, verify | FDP-2 y FDP-3 con gates completos. |
| `hotfix_validated` | guiado: candidate_codex, candidate_minimax, verify | Candidatos paralelos que aún requieren verificación. |
| `research` | guiado: triage_scope, triage_sources, research, challenge | Investigación sin writer con síntesis heterogénea. |

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
misiones no terminales y verifica los receipts requeridos. En una flota ligada a
Mission también crea y verifica el archivo Mission antes de cerrar CMUX. El
manifest, estado, ledgers y receipts de la flota se mueven a
`orchestration/runs/archive/`.

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
Verifica el candidato mediante tests deterministas y revisión independiente
antes de actuar, integrar, cancelar evidencia o cerrar la flota.

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

- Un writer dirty bloquea teardown. Revisa su worktree y conserva el trabajo en
  su branch dedicada mediante un commit válido, o descártalo únicamente si el
  propietario lo decide y ha identificado exactamente el worktree.
- Si la branch del writer avanzó respecto al baseline, teardown elimina el
  worktree limpio pero preserva la branch y registra su SHA final. No borres esa
  branch para "limpiar" la flota.
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

4. `accepted` no abandona BUILD. Una persona debe aprobar y el gate vuelve a
   comprobar el HEAD limpio exacto:

   ```bash
   python3 scripts/fleet_state.py advance \
     orchestration/runs/fleet-<feature>.manifest CHALLENGE \
     --evidence <evidencia-del-dialogo-aceptado> --approved-by <humano>
   ```

### FDP-3: GLM CHALLENGE y Claude VERIFY

1. Inicia FDP-3 solo después de la aprobación humana y desde el HEAD exacto
   aceptado por FDP-2:

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

GLM publica findings, no verdict. Claude revisa independientemente cada finding
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

La ruta regulada exige infraestructura externa accesible por HTTPS, bucket con
versionado y Object Lock `COMPLIANCE`, credenciales válidas y DNS que resuelva
solo a direcciones públicas globales prevalidables. Rechaza localhost,
loopback, link-local, redes privadas, respuestas DNS mixtas y cambios DNS después
del preflight. No existe fallback externo→local ni WORM→signed.

```bash
just mission-dry <feature> "<objetivo regulado>" \
  --workflow regulated --execution-profile regulated --target-repo <repo>
just mission <feature> "<objetivo regulado>" \
  --workflow regulated --execution-profile regulated --target-repo <repo>
```

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
6. No borres worktrees, branches, manifests, leases, sockets, snapshots,
   contenedores, volúmenes ni archivos sin demostrar identidad y ownership.
7. No reclames recursos por antigüedad. Un probe fallido o ambiguo conserva el
   estado para investigación.
8. No reduzcas FDP-2/FDP-3 a prompts informales: conserva tarea bounded,
   `run_id`, `message_id`, idempotency keys, aprobación humana, gates de fase,
   receipts y verificación offline.
