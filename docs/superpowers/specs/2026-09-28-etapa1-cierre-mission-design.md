# Etapa 1 — Cerrar el ciclo de una Mission: diseño

> **Fecha:** 2026-09-28 · **Estado:** aprobada por el operador el 2026-09-28; D1,
> D1b, D4 y D5 decididas (§6) · **Deriva de:** [constitución v1.0](../../fleet-constitution.md),
> §5 Etapa 1 · **Base examinada:** `main` en `75baa80`.

Los identificadores `C1`–`C9` remiten a los principios de la constitución. Este
documento es un plan revisable: no concede permisos ni describe capacidades
implementadas.

## 1. Objetivo

En una Mission del perfil MINIMAL con el contrato funcional
`python-stats-rpc-v1`, el Worker produce una revisión de `sample_stats.py`.
CONTROL la congela y ejecuta el check funcional independiente. Si el check falla
y la política de reparación deja intentos, el Worker recibe feedback derivado de
la evidencia de CONTROL y produce una nueva revisión en la misma Mission, con el
mismo contrato, plazo y límites. La Mission se cierra con éxito, sin
confirmación adicional, solo cuando una revisión pasa el check, esa misma
revisión se entrega en el destino local y la lectura posterior del destino
coincide con ella.

El plan SDD de la tarea que ejecuta el Worker está en
[`examples/sdd/stats-repair-delivery.json`](../../../examples/sdd/stats-repair-delivery.json).
Cubre la implementación de `stats`; la entrega corresponde a CONTROL y se
especifica aquí.

## 2. Punto de partida verificado

| Hecho en `75baa80` | Referencia |
| --- | --- |
| MINIMAL tiene una sola etapa `build` del Worker; su gate funcional se ejecuta tras congelar el árbol | [fleet_herdr_profile.py:119](../../../scripts/fleet_herdr_profile.py), [fleet_herdr_mission.py:1214](../../../scripts/fleet_herdr_mission.py) |
| Un check `failed` termina la Mission como `failed`; `blocked` e `indeterminate` quedan pendientes de reconciliación sin repetición automática | [fleet_herdr_mission.py:1242](../../../scripts/fleet_herdr_mission.py) |
| Cualquier resultado de rol distinto de `PASS` termina la Mission | [fleet_herdr_mission.py:1205](../../../scripts/fleet_herdr_mission.py) |
| Cada Mission admite un único `functional_attempt`; otro árbol se rechaza con «cannot switch tree/environment or silently retry» | [fleet_functional.py:179](../../../scripts/fleet_functional.py) |
| `attempt_id` es un `uuid5` de la Mission y del digest del contrato, que incluye `tree_sha`; cambia con cada revisión | [fleet_functional.py:82](../../../scripts/fleet_functional.py) |
| Owner Cycle ya repara: cuenta intentos contra `max_attempts`, genera feedback `checks_rejected` y termina `exhausted` | [fleet_herdr_owner_cycle.py:945](../../../scripts/fleet_herdr_owner_cycle.py) |
| Owner Cycle usa su propio espacio y rechaza adjuntarse a una Mission | [fleet_herdr_owner_cycle.py:103](../../../scripts/fleet_herdr_owner_cycle.py) |
| Confirmar pausa o cancelación exige admisiones quiescentes y un intento funcional con resultado | [fleet_herdr_control.py:78](../../../scripts/fleet_herdr_control.py) |
| El plazo de admisión se fija al crear la Mission y su vencimiento aborta los turnos pendientes | [fleet_herdr_mission.py:444](../../../scripts/fleet_herdr_mission.py) |
| No existe entrega a un destino ni lectura posterior; el resultado queda en el candidato congelado y el archivo | [fleet_herdr_archive.py](../../../scripts/fleet_herdr_archive.py) |
| Generalizar el bucle de intentos y revisiones es el prerrequisito de S8 | [architecture-refactor-2026-09-23.md:139](../../architecture-refactor-2026-09-23.md) |
| Los estados terminales de Mission son `succeeded`, `failed`, `blocked`, `abandoned` e `indeterminate` | [fleet_mission_state.py:59](../../../scripts/fleet_mission_state.py) |
| El workflow MINIMAL fija `deadline_seconds: 3600` y `token_budget: 0` | [herdr-minimal-implementation.yaml](../../../workflows/herdr-minimal-implementation.yaml) |
| `outputs/` está ignorado por Git; `outputs/sdd-deliveries/` todavía no existe | [.gitignore](../../../.gitignore) |
| El archivo con scope físico opt-in es v8 (`scope/baseline.json`, `scope/result.json`); la verificación exige v8 si y solo si la Mission tiene scope, lo que rechaza la degradación | [fleet_herdr_scope.py:24](../../../scripts/fleet_herdr_scope.py), [fleet_herdr_archive.py:561](../../../scripts/fleet_herdr_archive.py) |
| Un intento funcional con el mismo contrato y receipt existente se verifica y devuelve sin segunda ejecución física | [fleet_functional.py:182](../../../scripts/fleet_functional.py) |
| La cancelación confirmada termina la Mission como `abandoned` | [fleet_herdr_mission.py:296](../../../scripts/fleet_herdr_mission.py) |

## 3. Decisiones de diseño

| # | Decisión | Valor | Estado |
| --- | --- | --- | --- |
| E1 | Vehículo | Perfil MINIMAL con contrato funcional `python-stats-rpc-v1`; objetivo «Implementar `sample_stats.stats`» | Decidida (D1) |
| E2 | Activación | La reparación exige una `repair_policy` explícita congelada al crear la Mission. Sin ella, y en todas las Missions existentes, se conserva el comportamiento actual (C9) | Propuesta |
| E3 | Modelo de intentos | Lista de intentos por ordinal en el ledger de la Mission. Cada intento agrupa admisión del Worker, freeze, intento funcional y feedback. Se conserva la derivación actual de `attempt_id` | Propuesta |
| E4 | Mecánica de reparación | Reutilizar la de Owner Cycle —límite `max_attempts`, feedback `checks_rejected`, agotamiento— extrayendo lo común a un módulo compartido en lugar de duplicarlo (C9). Owner Cycle conserva su espacio, sus lectores y la prohibición de adjuntarse a Missions | Propuesta |
| E5 | Qué se repara | Solo un check `failed` con intentos restantes abre un nuevo intento. `blocked` e `indeterminate` siguen exigiendo reconciliación (C6). Un resultado del Worker distinto de `PASS` sigue siendo terminal en esta etapa | Propuesta |
| E6 | Feedback | Solo desde el receipt de CONTROL: llamadas fallidas, tipo de excepción, valor esperado y observado, y violaciones de la política de fuente, con un límite de bytes. Se entrega al Worker como datos no confiables, nunca como instrucciones | Propuesta |
| E7 | Agotamiento | Estado terminal `failed` con causa `repair_attempts_exhausted` o `mission_deadline_expired`, sin añadir estados nuevos. Todas las revisiones, receipts y feedback se conservan en CAS | Decidida (D5) en su efecto; representación propuesta |
| E8 | Raíz de entrega | `outputs/sdd-deliveries/owner-loop-v0/` en este repositorio, declarada y congelada al crear la Mission. CONTROL rechaza la raíz si está dentro del directorio de runs de la Mission o lo contiene, después de resolver enlaces simbólicos | Decidida (D1b) |
| E9 | Ruta por revisión | Solo se entrega la revisión aceptada, en `<raíz>/<mission_id>/<ordinal>-<tree_sha>/`. Revisiones y Missions distintas nunca comparten ruta | Decidida (D1b); formato propuesto |
| E10 | Escritura | CONTROL registra `delivery_started`, escribe el árbol aceptado en un directorio de preparación propio del mismo nivel y lo publica sin reemplazo (§3.3). Después relee la ruta final y compara hashes por archivo y el conjunto exacto de archivos con el árbol congelado. Solo escribe archivos: no crea commits ni hace push | Propuesta |
| E11 | Colisión | Si la ruta final ya existe con el mismo contenido que la revisión aceptada, se trata como entrega ya realizada (E12). Si existe con otro contenido, se bloquea solo esa entrega: no se escribe nada, la Mission termina `blocked` con causa `delivery_collision` y la revisión aceptada y su receipt se conservan. Los cambios sin commit en otros archivos del repositorio no bloquean la entrega | Decidida (D1b) |
| E12 | Recuperación de la entrega | Tras reinicio: si la ruta final coincide con la revisión, `delivered` sin segunda escritura; si no existe y solo queda la preparación propia de esa entrega, se limpia esa preparación y puede reintentarse; en cualquier otro caso, resultado indeterminado (C6) | Propuesta |
| E13 | Cierre | `closure_policy: automatic`. Éxito solo si se cumplen todos los requisitos obligatorios, la revisión entregada es exactamente la aceptada y la lectura en destino es satisfactoria. Nunca se declara éxito con efectos indeterminados ni con comprobaciones obligatorias pendientes | Decidida (D4) |
| E14 | Límites | `max_attempts: 3`, que incluye el intento inicial y hasta dos reparaciones; plazo total de 3600 s, el `deadline_seconds` del workflow MINIMAL, que se congela como `deadline_at` en la política de admisión al crear la Mission y que reiniciar o reanudar no renueva. La política de reparación no lo duplica, para que haya una sola fuente del plazo. Los créditos de delegación se fijan al crear la Mission como créditos del workflow × `max_attempts`, un turno del Worker por intento; `token_budget: 0`, porque Herdr rechaza cualquier valor positivo ([mission-run.py:186](../../../scripts/mission-run.py)) | Decidida (D5) |
| E15 | Archivo | Esquema v9 = capa de scope de v8 más intentos, receipts, feedback y recibo de entrega (§3.1). Los archivos v2–v8 se leen y verifican sin cambios y sin inventar intentos ni entrega (C9) | Propuesta |
| E16 | Transporte | Fase offline con `FakeBackend`/`MinimalFixture` y un runner funcional simulado que devuelve `failed` y después `passed`, sin llamadas a proveedores ni gasto. El carril Docker (`FLEET_FUNCTIONAL_DOCKER_TESTS=1`) y la validación con modelos requieren autorizaciones separadas | Decidida (D5) en la fase offline |
| E17 | Scope obligatorio | Una `repair_policy` exige contrato de scope físico: sin él no se crea la Mission. Así todo archivo v9 lleva la capa de scope y un incumplimiento de alcance impide el éxito (C7) | Propuesta |
| E18 | Revisión repetida | Un intento cuyo árbol coincide con el de un intento anterior reutiliza su receipt, consume su ordinal y se registra con la marca `unchanged_revision` (§3.2) | Propuesta |
| E19 | Publicación sin reemplazo | `renameat2(RENAME_NOREPLACE)` en Linux y `renamex_np(RENAME_EXCL)` en macOS; si el primitivo no está disponible, la entrega se bloquea. Nunca `os.rename` ni `os.replace` (§3.3) | Propuesta |
| E20 | Cancelación y plazo en la entrega | El efecto decisivo es la publicación. Antes de ella, se limpia la preparación propia y no se publica; después, se reconcilia y se registra el hecho sin revertirlo (§3.4) | Propuesta |

### 3.1 Compatibilidad histórica con v8

- Una Mission sin `repair_policy` produce exactamente el archivo que produce hoy:
  v7 en MINIMAL o v8 con scope físico. Su índice, entradas y verificación no
  cambian.
- Una Mission con `repair_policy` produce v9. v9 contiene todo lo que v8 exige
  (`scope/baseline.json`, `scope/result.json` y su vinculación con la creación,
  el evento de baseline y el árbol congelado) y añade `attempts/`, con receipts y
  feedback por ordinal, y `delivery/receipt.json`.
- La regla de degradación se generaliza sin debilitarla: con scope, el esquema
  debe ser v8 o v9; con `repair_policy`, debe ser v9. Un v9 presentado como v8,
  un v8 con entradas de intentos o de entrega, o un v9 sin capa de scope se
  rechazan.
- Los verificadores y lectores existentes de v2–v8 obtienen el mismo resultado
  sobre los archivos y ledgers existentes antes y después del cambio.

### 3.2 Intentos sucesivos con el mismo árbol

- La identidad de una revisión es su `tree_sha`; la de un intento, su ordinal.
- Cuando el árbol congelado del intento N coincide con el de un intento anterior
  k, el contrato funcional es idéntico y también su `attempt_id`. CONTROL
  reutiliza el receipt verificado de k, sin segunda ejecución física, y registra
  el intento N como `failed` con la marca `unchanged_revision` y la referencia a
  k. El intento consume su ordinal: repetir una revisión no esquiva
  `max_attempts` (C5).
- El feedback del intento N indica que la revisión no cambió respecto a k, además
  de los fallos del receipt reutilizado.
- Si el intento k no tiene receipt (`indeterminate`), no se ejecuta de nuevo: la
  Mission sigue la reconciliación de E5.
- El guard actual que impide cambiar de árbol o reintentar en silencio se
  conserva dentro de cada ordinal; solo un ordinal nuevo admite un árbol
  distinto o la reutilización descrita.

### 3.3 Publicación atómica sin reemplazo

- La preparación es un directorio hermano de la ruta final, en el mismo sistema
  de archivos: `<raíz>/<mission_id>/.staging-<ordinal>-<tree_sha>`. Su nombre
  determinista permite limpiar exactamente la preparación propia y nada más.
- Antes de publicar, CONTROL sincroniza en disco los archivos y la preparación.
  Publica renombrando la preparación a la ruta final con un primitivo que falla
  si el destino existe: `renameat2(RENAME_NOREPLACE)` en Linux y
  `renamex_np(RENAME_EXCL)` en macOS. Después sincroniza el directorio padre.
- Si el primitivo no está disponible o el sistema de archivos lo rechaza, la
  entrega queda bloqueada con causa `delivery_primitive_unavailable`. No hay
  alternativa con `os.rename` u `os.replace`, que sustituyen un directorio vacío
  existente.
- Colisión concurrente: si otro proceso crea la ruta final entre la comprobación
  previa y la publicación, el rename falla con `EEXIST`. CONTROL relee entonces
  la ruta final: si su contenido es idéntico al árbol aceptado, la entrega queda
  `delivered` sin escritura propia; si difiere, se aplica `delivery_collision`
  (E11). En ambos casos elimina solo su preparación.
- El lock del driver impide dos entregas concurrentes de la misma Mission;
  Missions distintas usan rutas disjuntas.

### 3.4 Cancelación y vencimiento durante la entrega

| Fase | Cancelación solicitada | Plazo vencido |
| --- | --- | --- |
| P0: revisión aceptada, sin `delivery_started` | No se entrega. Tras la quiescencia, terminal `abandoned` | No se entrega. Terminal `failed` con `mission_deadline_expired` |
| P1–P2: preparación en curso o completa, sin publicar | Se detiene la escritura, se elimina la preparación propia y no se publica. Terminal `abandoned` | Igual, con terminal `failed` y `mission_deadline_expired` |
| P3: publicada, sin recibo | Se relee la ruta final y se registra el recibo como hecho, sin revertir la publicación (C9). Terminal `abandoned`, indicando que la entrega ocurrió | Igual: se registra el recibo y el terminal es `failed` con `mission_deadline_expired` |
| P4: recibo registrado, sin terminal | Terminal `abandoned` con el recibo | Terminal `failed` con `mission_deadline_expired` si el recibo se registró después de `deadline_at` |

- `succeeded` exige que el recibo de entrega se registre antes de `deadline_at` y
  que no exista una solicitud de cancelación anterior al evento terminal.
- No se inicia una publicación después de `deadline_at` ni con una cancelación
  solicitada; se comprueba inmediatamente antes del rename.
- La confirmación de una cancelación exige, además de lo que ya exige hoy, que no
  quede preparación propia ni publicación sin reconciliar.
- Si tras un reinicio no puede determinarse si la publicación ocurrió (E12), el
  terminal es `indeterminate` y no se declara éxito.

## 4. Requisitos, escenarios y comprobaciones

Todas las comprobaciones son `NOT_VERIFIED` hasta que exista su implementación y
evidencia.

| REQ | Requisito | Principios |
| --- | --- | --- |
| REQ-001 | Una revisión que falla el check funcional nunca cierra la Mission con éxito, aunque el Worker declare `PASS` | C1, C7 |
| REQ-002 | Con `repair_policy` e intentos restantes, un check `failed` produce feedback y un nuevo turno del Worker en la misma Mission, contrato, plazo y límites | C2, C5 |
| REQ-003 | El feedback procede solo de evidencia de CONTROL y respeta su límite de bytes | C7, C4 |
| REQ-004 | Agotar `max_attempts` o el plazo termina la Mission con la causa correspondiente y conserva todo el trabajo parcial | C5, C1 |
| REQ-005 | Reiniciar conserva intentos, feedback, decisiones, plazo y límites, y no repite un turno ni un intento funcional inciertos | C6 |
| REQ-006 | Una solicitud de cancelación impide nuevos intentos; la confirmación exige reconciliar todos los intentos y la entrega | C6 |
| REQ-007 | La entrega escribe exactamente el árbol de la revisión aceptada en su ruta propia y la lectura posterior coincide | C1, C7 |
| REQ-008 | Una entrega interrumpida se reconcilia antes de repetirse; si no puede establecerse su efecto, el resultado es indeterminado | C6 |
| REQ-009 | La entrega no sobrescribe trabajo existente, solo escribe en `<raíz>/<mission_id>/` (su ruta final y su preparación propia), rechaza una raíz dentro de los runs y no se bloquea por cambios ajenos | C3, C9 |
| REQ-010 | Las Missions sin `repair_policy`, los archivos anteriores y los lectores históricos conservan su comportamiento y significado | C9 |
| REQ-011 | El cierre automático exige requisitos cumplidos, revisión entregada idéntica a la aceptada y lectura satisfactoria, sin efectos indeterminados ni comprobaciones pendientes | C3, C7 |
| REQ-012 | La fase offline no invoca proveedores ni binarios reales de Herdr o Codex | C5 |
| REQ-013 | Los archivos v2–v8 y las Missions sin `repair_policy` verifican igual que hoy; v9 exige la capa de scope y rechaza cualquier degradación | C9, C7 |
| REQ-014 | Un intento con el árbol de un intento anterior reutiliza su receipt, consume su ordinal y queda marcado `unchanged_revision` | C5, C6 |
| REQ-015 | La publicación nunca reemplaza un destino existente, incluso ante una colisión concurrente, y falla cerrada sin el primitivo adecuado | C3, C9 |
| REQ-016 | Cancelar o vencer el plazo en cualquier fase de la entrega produce el terminal de §3.4, sin revertir una publicación ni declarar éxito | C6, C5 |

| SCN | REQ | Dado | Cuando | Entonces | CHK |
| --- | --- | --- | --- | --- | --- |
| SCN-001 | REQ-001 | Mission sin `repair_policy` | El Worker declara `PASS` y el check devuelve `failed` | Terminal `failed`, como hoy | CHK-001 |
| SCN-002 | REQ-002 | `max_attempts: 3` | El intento 1 falla y el 2 pasa | Un solo `mission_id`, dos intentos en el ledger y plazo original; éxito tras la entrega | CHK-002 |
| SCN-003 | REQ-003 | El candidato imprime texto que simula instrucciones | Falla el check | El feedback contiene solo campos del receipt, truncados al límite | CHK-003 |
| SCN-004 | REQ-004 | `max_attempts: 3` | Los tres intentos fallan | Terminal `failed` con causa `repair_attempts_exhausted`, sin cuarto turno; los tres árboles siguen en CAS | CHK-004 |
| SCN-005 | REQ-004 | El plazo vence tras el intento 1 fallido | El driver avanza | No se abre el intento 2; terminal con causa `mission_deadline_expired` y el intento 1 conservado | CHK-004 |
| SCN-006 | REQ-005 | Intento funcional 2 iniciado sin resultado | El driver se reinicia | Se registra `indeterminate` para ese intento y no se vuelve a ejecutar | CHK-005 |
| SCN-007 | REQ-005 | Turno del Worker del intento 2 con envío ambiguo | El driver se reinicia | Se reconcilia el mismo turno; no se reenvía a ciegas | CHK-005 |
| SCN-008 | REQ-005 | Mission reanudada tras una pausa | Se reanuda | Conserva el `deadline_at` original; el plazo no se renueva | CHK-005 |
| SCN-009 | REQ-006 | Intento 1 fallido y cancelación solicitada | El driver avanza | No se abre el intento 2; la confirmación llega solo con admisiones quiescentes | CHK-006 |
| SCN-010 | REQ-007 | Intento aceptado y raíz declarada | Se entrega | La ruta `<mission_id>/<ordinal>-<tree_sha>/` contiene exactamente los archivos del árbol aceptado, con los mismos hashes | CHK-007 |
| SCN-011 | REQ-008 | `delivery_started` registrado sin recibo y ruta final ya escrita con el contenido aceptado | El driver se reinicia | Queda `delivered` sin segunda escritura | CHK-008 |
| SCN-012 | REQ-008 | `delivery_started` registrado sin recibo y solo la preparación propia presente | El driver se reinicia | Se limpia esa preparación y se entrega una vez | CHK-008 |
| SCN-013 | REQ-009 | La ruta final existe con otro contenido | Se intenta entregar | Terminal `blocked` con causa `delivery_collision`, sin escrituras; revisión aceptada y receipt conservados | CHK-009 |
| SCN-014 | REQ-009 | El repositorio tiene cambios sin commit fuera de la ruta de entrega | Se entrega | La entrega procede y esos cambios quedan intactos | CHK-009 |
| SCN-015 | REQ-009 | La raíz declarada resuelve dentro del directorio de runs | Se crea la Mission | Se rechaza antes de lanzar agentes | CHK-009 |
| SCN-016 | REQ-010 | Archivo v7 y Mission histórica sin `repair_policy` | Se leen y verifican | Mismo resultado que en `75baa80` | CHK-010 |
| SCN-017 | REQ-011 | La entrega queda indeterminada | El driver evalúa el cierre | No se declara éxito | CHK-011 |
| SCN-018 | REQ-011 | Check y entrega satisfactorios | El driver evalúa el cierre | `succeeded` sin confirmación adicional | CHK-011 |
| SCN-019 | REQ-012 | Cualquier escenario offline | Se ejecuta la suite | Cero invocaciones de proveedor y ningún binario real de Herdr o Codex | CHK-012 |
| SCN-020 | REQ-013 | Archivos v7 y v8 existentes, con y sin scope | Se verifican con el código nuevo | Mismo resultado que en `75baa80` | CHK-013 |
| SCN-021 | REQ-013 | Archivo v9 | Se reetiqueta como v8, se le quita la capa de scope o se añaden intentos a un v8 | Cada variante se rechaza | CHK-013 |
| SCN-022 | REQ-013 | `repair_policy` sin contrato de scope | Se crea la Mission | Rechazo antes de lanzar agentes | CHK-013 |
| SCN-023 | REQ-014 | El intento 2 produce el mismo árbol que el intento 1 fallido | Se evalúa el intento 2 | Sin ejecución física; `failed` con `unchanged_revision`; queda el intento 3 | CHK-014 |
| SCN-024 | REQ-014 | Los tres intentos producen el mismo árbol | Se evalúan | Terminal `repair_attempts_exhausted` con una sola ejecución física | CHK-014 |
| SCN-025 | REQ-015 | Otro proceso crea la ruta final justo antes del rename, con el mismo contenido | Se publica | `EEXIST`; relectura idéntica; `delivered` sin escritura propia | CHK-015 |
| SCN-026 | REQ-015 | Otro proceso crea la ruta final justo antes del rename, con otro contenido | Se publica | `EEXIST`; `delivery_collision`; el contenido ajeno queda intacto | CHK-015 |
| SCN-027 | REQ-015 | La ruta final existe como directorio vacío | Se publica | No se reemplaza; `delivery_collision` | CHK-015 |
| SCN-028 | REQ-015 | Primitivo sin reemplazo no disponible | Se intenta publicar | `delivery_primitive_unavailable`, sin escrituras en la ruta final | CHK-015 |
| SCN-029 | REQ-016 | Cancelación solicitada en P0, P1–P2, P3 y P4 | El driver avanza | Terminales y efectos de §3.4 en cada fase | CHK-016 |
| SCN-030 | REQ-016 | Plazo vencido en P0, P1–P2, P3 y P4 | El driver avanza | Terminales y efectos de §3.4 en cada fase | CHK-016 |

| CHK | Procedimiento offline |
| --- | --- |
| CHK-001 | Driver MINIMAL con backend simulado y runner que devuelve `failed`; comprobar el terminal y que no hay segundo turno |
| CHK-002 | Runner simulado `failed` → `passed`; comprobar ordinales, identidad de la Mission, plazo original y recibo de entrega |
| CHK-003 | Receipt con salida hostil; comprobar los campos y el tamaño del feedback |
| CHK-004 | Runner siempre `failed` y, por separado, reloj que vence tras el intento 1; comprobar causas terminales, ausencia de turnos extra y árboles en CAS |
| CHK-005 | Interrumpir tras `functional_check_started`, tras un envío ambiguo y durante una pausa; reiniciar y comprobar que no hay ejecución ni envío duplicados y que `deadline_at` no cambia |
| CHK-006 | Solicitar cancelación entre intentos; comprobar que no hay admisión nueva y que la confirmación exige quiescencia |
| CHK-007 | Entregar en una raíz temporal; comprobar ruta, hashes, conjunto exacto de archivos y ausencia de escrituras fuera de `<raíz>/<mission_id>/` |
| CHK-008 | Interrumpir tras `delivery_started` con ruta final escrita y con solo la preparación; comprobar `delivered` y una única escritura |
| CHK-009 | Ruta con contenido ajeno, repositorio con cambios sin commit fuera de la ruta y raíz dentro de los runs; comprobar bloqueo acotado, entrega normal y rechazo en la creación |
| CHK-010 | Replay de archivos y ledgers existentes con los tests actuales de lectura histórica |
| CHK-011 | Cierre con entrega indeterminada y con entrega satisfactoria; comprobar que solo el segundo termina `succeeded` |
| CHK-012 | Backend y runner simulados que cuentan invocaciones y fallan ante cualquier binario real; comprobar cero llamadas de proveedor |
| CHK-013 | Ejecutar los tests de archivo y scope existentes sin cambios (`test_fleet_herdr_archive*`, `test_fleet_herdr_scope`) y añadir fixtures v9 manipulados |
| CHK-014 | Runner simulado que cuenta ejecuciones físicas; Worker simulado que repite el mismo árbol |
| CHK-015 | Inyectar la creación concurrente entre la comprobación y el rename; directorio vacío preexistente; primitivo ausente simulado |
| CHK-016 | Matriz de fases P0–P4 por cancelación y por reloj vencido; comprobar terminal, recibo, preparación eliminada y ruta final intacta |

## 5. Fuera de alcance

- Ejecución real en Docker y con proveedores: requieren autorización propia.
- Otros perfiles (cinco o seis etapas), Fusion y delegación.
- Reparar resultados del Worker distintos de `PASS` y los checks `blocked` o
  `indeterminate`.
- Entregar revisiones no aceptadas, entregas externas, commits o push.
- Migrar Missions históricas o retirar Owner Cycle.
- Nuevos perfiles funcionales o ampliar la política de fuente
  `stats-python-subset-v1`.

## 6. Decisiones

| ID | Decisión | Valor |
| --- | --- | --- |
| D1 | Primera tarea | Perfil de estadísticas `python-stats-rpc-v1` |
| D1b | Destino | `/Users/hector/Projects/agent-fleet-orchestrator/outputs/sdd-deliveries/owner-loop-v0/`, con un subdirectorio por Mission y revisión, fuera de los runs y comprobado por lectura posterior. Una colisión bloquea solo esa entrega y conserva el resultado; los cambios sin commit en otros archivos no la bloquean |
| D4 | Cierre | `automatic`, con las condiciones de E13 |
| D5 | Límites | `max_attempts: 3` (inicial más dos reparaciones) y `deadline_seconds: 3600` totales, no renovables al reiniciar o reanudar. Agotar un límite conserva el trabajo parcial y registra la causa. Fase offline sin llamadas a proveedores y con gasto cero |

Con la aprobación, las filas D1, D4 y D5 de la constitución (§7) constan como
decididas. El plan de implementación está en
[`docs/superpowers/plans/2026-09-28-etapa1-cierre-mission.md`](../plans/2026-09-28-etapa1-cierre-mission.md).
