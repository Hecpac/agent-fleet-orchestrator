# Refactorización arquitectónica de Agent Fleet

## Alcance

La auditoría de esta conversación fue read-only. La instrucción posterior
«arranca con el plan de la refactorizacion» autoriza mantenimiento local por
este único implementador. Commit, push, proveedores, Missions, flotas, gasto,
configuración global y limpieza de evidencia conservan sus límites anteriores.

Objetivo: extraer un núcleo común de autoridad, lifecycle y evidencia con
políticas versionadas, conservando Herdr, contratos históricos y trabajo previo.
Un resultado local de tests no acredita calidad de modelos ni éxito de Missions.

## Base y evidencia

- HEAD inicial: `a029f0a2d84dbc445a7ad3b8a09d36b904431e32`.
- 392 archivos tracked, 69 untracked, 12 tracked modificados.
- Base local: `outputs/refactor-20260923/baseline.json`.
- `source.tar.gz` y `source-manifest.json`: contenido y modos de los 461 paths
  del source físico previo. No restaurar sobre trabajo posterior sin comparar.
- `physical-metadata.jsonl.gz`: inventario previo que incluye ignored y
  worktrees; no es un hash del contenido de todos los outputs.
- `archive-corpus.json`: resultados iniciales y errores encadenados de 20
  archives. Los 3 Herdr y 4 legacy verifican; 13 legacy son rechazados.
  No se presume corrupción ni aceptación original para los rechazados.
- `compiled-workflows.json`: resultados y contratos compilados previos;
  9 compilables y una política rechazada antes de admitir efectos.

La copia del source preserva también la implementación que las campañas
existentes fijaban por hashes. Cambiar source puede invalidar esas preparaciones;
no se alteran sus planes, pins, deadlines, autorizaciones ni evidencia.

## Orden de trabajo

| Slice | Objetivo | Estado |
| --- | --- | --- |
| S0 | Base física y corpus histórico | Completado; procedencia y aceptación originales desconocidas quedan explícitas |
| S1 | Intérprete y disponibilidad de workflows | Verificado localmente |
| S2 | Lectores, contratos y operaciones puras | Completado: dos carriles históricos explícitos, lectores puros extraídos y un rechazo explicado |
| S3 | Composición moderna y compatibilidad | Completado: raíz moderna, soporte neutral y driver legacy explícito, sin cambio de comportamiento |
| S4 | Catálogo de perfiles e identidad | Completado: catálogo como fuente única, identidades fijadas y copias ancladas por test |
| S5 | Telemetría separada de autoridad | Completado: el ledger es dueño del esquema de intervalos y la dirección medición → autoridad queda fijada por test |
| S6 | Scope y aceptación independientes del perfil | Completado: cada política declarada una vez en el catálogo, mecánicas sin perfil y esquema v8 como capa de scope |
| S7 | Mecanismos comunes de Owner Cycle | Completado: primitivas comunes verificadas, duplicados internos con un solo hogar y proyección del Owner como política declarada |
| S8 | Política opt-in de reparación | Implementada como etapa 1 de la constitución (`repair_policy`, verificada offline); la calidad live y el carril Docker requieren autorización propia |
| S9 | Retirada de duplicación y extracción de experimentos | Pendiente de equivalencia y consumidores |

Cada slice conserva contratos anteriores, exige pruebas proporcionales y una
revisión del diff. Los historiales terminales no se migran in situ. El rollback
consiste en revertir únicamente el delta propio después de compararlo con la
base, nunca en restaurar a ciegas el worktree completo.

## Desviaciones del informe

### D1 — Validación estática de `regulated.yaml`

El informe infería que el rechazo de compilación bloqueaba la fase de validación
de CI. La implementación de `workflow_config.main` y
`test_cli_validate_is_static_but_compile_admits_effect_policy` distinguen ambos
comandos: `validate` acepta el esquema; `compile` rechaza el presupuesto no
imponible. La documentación ya expone esa distinción.

S1 conserva el archivo, la validación y el rechazo de ejecución. Añade un
catálogo diagnóstico derivado del compilador, sin otro catálogo de autoridad ni
una lista manual paralela. La verificación exige conservar los nueve contratos
compilados y el rechazo de `regulated`; el catálogo nunca promete runtime listo.

### D2 — Entorno criptográfico de las pruebas

La primera ronda pasó 92 de 93 tests. El caso de archive firmado falló porque
el OpenSSL seleccionado por el entorno no soportaba ED25519. El mismo caso
pasó con el OpenSSL 3 ya instalado. La ronda conjunta utiliza ese binario
mediante un `PATH` limitado al proceso de tests, sin instalación ni cambio global.

### D3 — Alcance de S3

El informe que definió los slices no se conserva en el repositorio ni en las
transcripciones locales. S3 se interpreta a partir de su nombre, del objetivo de
esta refactorización y de la regla de carriles de `AGENTS.md`: `mission-run.py`
queda como raíz de composición moderna (entrada, despacho, `create_and_drive`,
`dry_run`, `status` y `main`). El driver de Missions CMUX y sus primitivas de
efecto pasan a un módulo de compatibilidad explícito, y los helpers neutrales
compartidos a un módulo de soporte. No se introduce fallback entre carriles.

### D4 — Alcance de S4

Como en D3, S4 se interpreta a partir de su nombre: una sola fuente de hechos por
perfil Herdr y de su identidad. Los consumidores del carril Herdr derivan
versiones, modelos y restricciones de `fleet_herdr_profile`. El validador del
ledger y los contratos puros conservan literales propios, anclados al catálogo
por test, sin ampliar sus dependencias. `HerdrProfile.contract()` no cambia: su
digest está fijado en ledgers existentes. Quedan fuera `fleet_herdr_roster`
(identidades del roster FLEET-01), `fleet_herdr_owner_runtime` (S7) y el
controlador de diálogo legacy.

### D5 — Alcance de S5

Como en D3 y D4, S5 se interpreta a partir de su nombre y de la semántica de
medición documentada («observational rather than Mission authority»): la
telemetría observa la autoridad y nunca la define. El ledger es dueño del esquema
de todo evento que guarda; los módulos de medición e informe leen la autoridad y
no escriben el ledger, salvo `fleet_herdr_metrics.observe`, único instrumentador
declarado de intervalos, y un fallo al medir no puede decidir un veredicto.
Verificadores, formatos archivados y resultados de `verify` no cambian;
`usage()` pasa a cumplir su contrato documentado para una forma de snapshot que
antes lanzaba (ver la entrega). Quedan fuera los otros módulos en los que el
ledger delega esquema (`fleet_herdr_inference`, `fleet_herdr_control`), que son
autoridad y no medición, y la semántica de escritura de `observe`.

### D6 — Alcance de S6

Como en D3–D5, S6 se interpreta a partir de su nombre. Las mecánicas de scope
físico, aceptación de artefactos y contrato funcional ya no leen el perfil:
reciben candidato, contrato y árbol por parámetro. Lo que dependía del perfil
eran dos políticas de admisión (qué perfil admite scope y cuál exige contrato de
aceptación al crear) expresadas como comparaciones sueltas, y el lector de
archives, que trataba el esquema v8 como si fuera un perfil. S6 declara cada
política una vez en `fleet_herdr_profile`, fuera de `contract()`, y hace que cada
comprobación la consulte. No se admite ni se relaja nada: los demás perfiles
siguen rechazando el scope; habilitarlo en otro perfil es una decisión de
producto, no de este slice. Quedan fuera la proyección de trabajo del Owner
(`fleet_herdr_work_packet`, S7), el momento en que el driver ejecuta la
aceptación funcional (tras el único escritor en el perfil mínimo; antes de la
síntesis en los demás), que depende de la topología del perfil, y la duplicación
entre `run` y `dry` de `mission-run` (S9).

### D7 — Alcance de S7

Como en D3–D6, S7 se interpreta a partir de su nombre. El Owner Cycle ya consume
las primitivas comunes del carril de Missions: CAS (`fleet_artifacts`), bloqueos y
escritura atómica (`RootedFS`), scope físico, aceptación de artefactos, snapshot
del candidato (`fleet_herdr_archive.snapshot`), normalización de uso y
verificación de transcripciones. Su journal y su sello son un formato propio,
sellado y fijado por el replay de pilot-07, y no se unifican con el ledger de
Missions. S7 da un solo hogar a los duplicados internos que son predicados o
comparaciones puras y declara la proyección de trabajo del Owner como política
del catálogo (patrón de S6). Esquema de eventos, `_seal()`, formas de
`_classify`/`_checks` y todo mensaje alcanzable desde ellos no cambian.
Generalizar el bucle de intentos y revisiones es el prerrequisito de S8 y una
decisión de diseño; queda para S8.

## Entrega inicial: S0, S1 y extracción inicial de S2

- `fleet_archive_tree.py`: verificación de paths y hashes del árbol sobre bytes,
  sin Mission, proveedores ni procesos. Conserva SHA-1/SHA-256, gitlinks,
  symlinks, nombres Git, límites y rechazos del código previo.
- `fleet_git_snapshot.py`: construcción acotada del snapshot mediante Git;
  separado de la verificación pura. `fleet_archive.py` conserva aliases para
  sus consumidores anteriores y la misma clase de error compartida.
- Herdr, Owner Cycle y checks funcionales consumen las nuevas APIs; no cambian
  formats, receipts ni reglas de aceptación.
- `fleet_workflow_contract.py`: contrato puro del esquema v1. El compilador
  conserva sus aliases públicos. La lectura de compiled workflows ya no importa
  el compilador ni el controlador de Missions.
- `router_config` carga la autoridad Mission solo cuando procesa una solicitud
  de runtime compilado. Se mantienen las comprobaciones actuales de router y
  de identidad local OpenCode; su versionado histórico aún está pendiente.
- `FLEET_PYTHON` selecciona el intérprete de las recetas y CI. Se conserva el
  override anterior `personal_python` y se añade `just workflow-catalog`.

Verificación local en `outputs/refactor-20260923/`:

- `tests-s0-s2.json` y `.log`: 237 tests, 224 ejecutados con éxito y 13 omitidos
  del lane Docker explícito. No se ejecutó la suite completa del repositorio.
- `replay-s2.json`: mismos resultados completos en 20 archives, incluidos los
  13 rechazos previos; mismos nueve contratos compilados y rechazo de
  `regulated`. Owner Cycle conserva su seal y estado `exhausted` con 3 intentos
  y 0 revisiones. Se rechazaban efectos durante este replay y no se intentó ninguno.
- `corpus-classification.json`: 7 archives verificables con el lector actual,
  9 rechazados por binding global `base_sha` y 4 por snapshot sin `providers`.
  Esta clasificación no acredita aceptación semántica original.
- `cli-smoke.json` y salidas asociadas: CLI real de catálogo, estado y archive
  Herdr; el archive conserva hash, anclaje y aceptación declarada.
- `preservation-s2.json`: los cambios en source coinciden con los 14 archivos
  existentes previstos; los demás contenidos y modos de la base se conservan.
  La comparación física no detecta paths previos eliminados. Detecta cambios
  concurrentes en `.claude/settings.local.json` y metadatos de `.claude/` y
  `.claude/.cc-writes/`. Sus timestamps son posteriores al fin de los tests;
  se observó además un proceso de la aplicación Claude, ajeno al runner de
  pruebas, con este repositorio como cwd. No se atribuye causalidad exclusiva
  ni se revierten esos cambios. `concurrent-state.json` registra la observación.
- `own-changes.patch`: delta de esta intervención respecto de la base física,
  separado del trabajo que ya existía antes de la autorización.

## Entrega S2: carriles históricos y lectores puros (2026-09-27)

Criterio: conservar o explicar cada resultado según la versión original, sin
relajar las validaciones actuales. Evidencia en `outputs/refactor-20260927-s2/`.

- Resultado original: `original_readers.py` ejecuta el árbol versionado de cada
  fecha sobre copias temporales (`original-reader-outcomes.json`). Los 9 archives
  del 14-07 verifican con `67a7734` y se rechazan desde `c1e0f2c`/`1bcdec2`. Los 4
  del 20-07 verifican con `b259e18` y `85261cf` y se rechazan desde `3b8d894`. La
  revisión exacta del controlador sigue `NOT_VERIFIED`; los commits son el proxy.
- `manifest-contract-v2`: solo con `manifest_contract_version=2` y sin `base_sha`
  global. Enlaza la base mediante un único escritor, exige su `final_sha` de
  arranque igual a la base y rechaza cualquier campo de publicación v3. El final
  lo acredita la evidencia Git archivada. No acredita aislamiento del escritor,
  publicación tras quiescencia ni enlace del final en el manifest.
- `router-pre-provider-contract`: `3b8d894` añadió `router.providers`, `transport`
  y `num_predict` sin cambiar `schema_version`. El carril exige que el router
  archivado omita los tres y que el manifest no tenga registros `provider.*`; una
  mezcla se rechaza. `validate_router(provider_contract=False)` solo lo usa la
  lectura de archives; compilación, validación y modo `effect` conservan el
  contrato actual.
- El resultado incluye `historical_lanes` y `not_attested`; `verify_acceptance`
  lo rechaza y `fleet_report` lo muestra. Los 7 archives del lector actual
  conservan resultados idénticos.
- `d0d20dda` sigue rechazado: su recibo de auditoría es anterior a `27c728d`
  (sin `trust_scope` ni `anchor_receipts_sha256`). Un carril propio duplicaría la
  verificación de firma, anclas y trust scope para una Mission de prueba.
- `fleet_archive_contract.py` recibe 11 funciones y 9 constantes sin IO, Git ni
  procesos, AST-equivalentes (`extraction-ast.json`); `fleet_archive` conserva
  aliases.

Verificación: `replay_corpus.py` reproduce exactamente S0 antes del cambio
(20 archives, 10 workflows, sello owner-cycle y 0 efectos). Después: 7 idénticos,
12 verificados por carril, 1 rechazo explicado, workflows y sello sin cambios
(`replay-before.json`, `replay-after.json`, `corpus-classification.json`). Pasan
`test_fleet_archive_historical_lanes` y las suites de archive, router, compilados,
informes y Mission; el único error es el bind de socket Unix del sandbox.
Los tests unitarios cubren la selección, el enlace estricto y el rechazo de
mezclas; la verificación completa de los carriles solo la ejercita el replay
local del corpus ignorado, no el CI hospedado.

Quedan fuera de S2 `fleet_herdr_archive.verify` y la dependencia de la
validación del router archivado respecto a `.opencode/agents` del checkout
actual.

## Entrega S3: raíz moderna y carril legacy explícito (2026-09-27)

Interpretación en D3. Evidencia en `outputs/refactor-20260927-s3/`.

- `fleet_mission_run_support.py`: `MissionRunError`, lectores de manifest y de
  archivos durables, lecturas Git exactas, `enforce_audit_trust`,
  `effect_compiled` y la carga de `fleet-risk.py`. No elige carril ni runtime.
- `fleet_legacy_mission.py`: primitivas de proceso y CMUX, admisión y cierre del
  Lead, handoff de assurance y `drive_legacy_mission`, cuyo cuerpo es el texto
  exacto (1.239 líneas) que seguía al despacho Herdr en `drive_mission`.
- `mission-run.py` pasa de 2.716 a 652 líneas: entrada, despacho,
  `create_and_drive`, `dry_run`, `status` y `main`. `drive_mission` resuelve el
  plan y despacha los presets Herdr a `fleet_herdr_mission.drive` y el resto a
  `fleet_legacy_mission.drive_legacy_mission`. El CLI no cambia.
- Cada nombre que los tests parchean tiene un solo hogar: las primitivas de
  efecto y `PROMPT_TEMPLATE` solo existen en el módulo legacy y
  `enforce_audit_trust` en el de soporte, con llamadas cualificadas. `git_value`
  usa `fleet_legacy_mission.require_success` para conservar la interceptación
  anterior; reubicar esa primitiva corresponde a S9.
- 32 definiciones movidas son AST-equivalentes y el cuerpo del driver es
  verbatim (`s3a-ast.json`, `s3b-ast.json`).

Verificación: `LegacyRuntimeGuard`, permanente en los tests, rechaza
`fleet-up.sh`, `fleet-down.sh`, `fleet-send.sh` y `cmux` reales; no se disparó.
Los 23 módulos que referencian `mission-run` ejecutan 561 tests con el mismo
conjunto de fallos antes, tras S3a y tras S3b, todos bloqueos conocidos del
sandbox (`failset-*.txt`). Un parche en forma de tupla sin reubicar falló con
`AttributeError`, como estaba previsto, y se corrigió.
`ModernCompositionRootTests` fija que las primitivas legacy no existen en la
raíz, que un preset Herdr nunca llega al driver legacy y que uno legacy se
delega con el plan resuelto. `surface_check.sh` reproduce byte a byte `dry` de
`herdr-implementation` e `implementation` y `status` de dos Missions legacy; el
replay del corpus coincide con la entrega S2. `mission-run.py` conserva imports
sin uso propio porque los tests acceden a esos módulos a través de él; también
pasan los tests de `fleet_personal_pool`, que lo carga por ruta.

## Entrega S4: catálogo de perfiles e identidad (2026-09-27)

Interpretación en D4. Evidencia en `outputs/refactor-20260927-s4/`.

- `fleet_herdr_profile` añade `BY_PROFILE_ID`, `VERSIONED`,
  `PHYSICAL_SCOPE_PROFILE` y `role_models()` fuera de `HerdrProfile.contract()`;
  los tres digests no cambian.
- Derivan del catálogo: las versiones, modelos, turnos y esquema mínimo de
  `fleet_herdr_permissions`; la versión de permisos de `fleet_herdr_rejection`
  (antes dos mapas); `fleet_herdr_mission.PROFILE`, alias sin consumidores; y la
  restricción de alcance físico en `fleet_herdr_scope`, `fleet_mission` y
  `mission-run`. Un binding desconocido conserva su versión 1 histórica porque el
  estado del backend no valida ese campo.
- `fleet_mission_state` y `fleet_workflow_contract` no importan el catálogo:
  sus literales pasan a constantes con nombre (`VERSIONED_FINALIZATION_CONTRACTS`,
  `LEGACY_FINALIZATION_CONTRACT`, `PHYSICAL_SCOPE_PROFILE_ID`,
  `WORK_OWNER_BY_PRESET`, …).
- `tests/test_fleet_herdr_profile_catalog.py` fija los tres digests completos,
  ancla cada copia a la derivación del catálogo y valida los contratos de
  finalización por comportamiento. Pasó sobre el árbol sin cambios antes de
  derivar nada, sin divergencias.

- Residuo anclado, no derivado: `fleet_herdr_archive.verify` elige el perfil por
  esquema (6 → Research, si no Minimal) y admite versiones de permisos
  `{1, 2, 3, 4}`. El test los fija contra el catálogo; derivarlos corresponde al
  trabajo pendiente del lector de archives Herdr.

Verificación: suite completa antes y después con el mismo conjunto de fallos
(solo bloqueos del sandbox; 2.179 → 2.182 tests por los anclajes nuevos). El
replay del corpus coincide con S3, incluida la ruta de permisos de los tres
archives Herdr, y `surface_check.sh` reproduce la superficie real. La conversión
final del segundo chequeo de alcance de `mission-run`, su texto de ayuda y el
ancla del lector se verificaron con los tests afectados, no con otra suite completa.

## Entrega S5: telemetría separada de autoridad (2026-09-27)

Interpretación en D5. Evidencia en `outputs/refactor-20260927-s5/`.

- `fleet_mission_state` define `HERDR_INTERVAL_KINDS`, `_validate_herdr_interval`
  y `_reduce_herdr_interval`, y su validador y su reductor dejan de importar
  `fleet_herdr_metrics`. Las dos funciones son AST-equivalentes a las anteriores
  salvo el prefijo `state.`, el nombre y las anotaciones añadidas
  (`move-ast.json`); mensajes y proyección `herdr_intervals` no cambian. La rama
  de intervalos del ledger ya no importa el módulo de métricas.
- `fleet_herdr_metrics` conserva `observe`, `timing` y `usage`; su `KINDS` es la
  misma constante del ledger, ahora `frozenset`, con el mismo orden de iteración
  del que `timing()` construye su resultado (comprobado con varias semillas de hash).
- `tests/test_fleet_telemetry_authority.py` fija el esquema, los mensajes, la
  proyección y los conflictos de los eventos de intervalo (pasó sobre el árbol
  sin cambios) y la dirección: el ledger no importa módulos de medición ni de
  informe; `fleet_trace`, `fleet_export_trace`, `fleet_report` y
  `fleet_herdr_report` no llaman escritores del ledger; en `fleet_herdr_metrics`
  solo `observe` escribe, y solo eventos de intervalo. También fija que `usage()`
  es total sobre filas ya validadas (ver la corrección).

Corrección: medir el uso no puede abortar un veredicto.

- `fleet_herdr_archive.attest_admissions` (creación y verificación, también
  `for_completion`), las respuestas `owner-cycle-admission-v2` de
  `fleet_herdr_owner_cycle` y `fleet_herdr_report` llaman a
  `fleet_herdr_metrics.usage()` después de validar la transcripción: JSONL,
  filas y payloads objeto, identidad y turno. Ninguno valida el contenido de
  `token_count`. Con un `info` no vacío que no es objeto, `usage()` lanzaba
  `AttributeError`: el archive no se creaba ni verificaba, la excepción salía del
  driver sin cerrar la Mission (ni el driver ni el CLI la capturan), el Owner
  Cycle invalidaba la respuesta completa y el informe marcaba el archive como
  corrupto.
- `usage()` trata ahora ese `info` como snapshot inválido
  (`invalid_usage_counter_snapshot`), igual que ya hacía con `null` y `{}`, como
  exige la semántica de medición. Toda entrada que antes no lanzaba sigue el
  mismo camino. Validarlo en el lector de transcripciones habría hecho que la
  autoridad rechazara evidencia por un campo de medición.
- Alcance real: los 180 `token_count` de los archives Herdr del corpus traen
  `info` objeto; hace falta una transcripción ligada fabricada o corrupta. Se
  reprodujo solo con datos sintéticos (`usage-pin-before-fix.log`).

Revisado y conservado a propósito:

- `permissions.usage_by_run` sigue en el resultado de `verify`: forma parte del
  corpus, de la salida del CLI y del informe. Extraerlo alteraría además qué
  error informa primero un archive inválido.
- `usage baseline binding mismatch` protege la integridad de la evidencia
  archivada; relajarlo sería un cambio de política, no una separación.
- El presupuesto por tokens de `fleet_ledger`/`fleet_budget` es control de
  admisión deliberado del carril legacy.

Hallazgo pendiente de decisión, fuera de S5: `observe` escribe el inicio y el fin
de cada intervalo alrededor de callbacks del controlador (llamadas al backend,
incluido `submit`, ejecución funcional y espera del supervisor). Si falla la
escritura del inicio, el callback no se ejecuta; si falla la del fin, esa
excepción sustituye el resultado o la excepción del callback. Hacerlo tolerante
cambia la semántica del ledger («un fin ausente es desconocido») y exige una
decisión propia.

Verificación: la suite completa conserva el mismo conjunto de fallos (solo
bloqueos del sandbox) antes (2.183 tests), tras el traslado (2.191) y tras la
corrección (2.193), con los tests nuevos en verde. El replay del corpus coincide
con S4 en ambos puntos: los ledgers de los tres archives Herdr llevan 2.950
eventos de intervalo por el validador y el reductor trasladados, y sus 180
`token_count` por la línea corregida. `surface_check.sh` reproduce `dry` y
`status` byte a byte y los módulos afectados importan en frío sin ciclos. El
ajuste final del anclaje de dirección (también llamadas por nombre importado) y
las precisiones de este texto se verificaron con los tests afectados.

## Entrega S6: scope y aceptación independientes del perfil (2026-09-27)

Interpretación en D6. Evidencia en `outputs/refactor-20260927-s6/`.

- `fleet_herdr_profile` declara `ACCEPTANCE_REQUIRED_PROFILE` junto a
  `PHYSICAL_SCOPE_PROFILE`, fuera de `contract()`: los tres digests no cambian.
  `fleet_mission.create_mission` y las comprobaciones de `run` y `dry` de
  `mission-run` consultan la declaración en lugar de comparar con `MINIMAL`;
  mensajes, tipos de excepción y orden de las comprobaciones no cambian.
- `fleet_herdr_scope.validate_binding` admite el scope según la constante del
  ledger (`PHYSICAL_SCOPE_PROFILE_ID`), que es la vinculación de creación que
  `mission_created` ya impone, y deja de importar el catálogo. Revierte a
  propósito una conversión de S4: el catálogo sigue siendo la fuente del dato a
  través del anclaje de S4 sobre esa constante.
- `fleet_herdr_archive` deriva `VERSIONED_ARCHIVE_PROFILES` del catálogo más la
  capa de scope (`ARCHIVE_VERSION` → perfil de scope), y de él los esquemas
  legibles y los que admiten evidencia funcional. Sustituye `{6, 7, 8}`, el
  ternario «6 → Research, si no Minimal», `{2, …, 8}` y `{4, …, 8}`; los
  resultados son idénticos. Queda derivada la mitad del residuo de S4; las
  versiones de permisos `{1, 2, 3, 4}` siguen siendo literales ancladas.
- `tests/test_fleet_herdr_policy_admission.py` fija, en verde sobre el árbol sin
  cambios, los rechazos que ningún test cubría: scope en Legacy y Research por
  `dry`, `run`, creación, ledger y vinculación; aceptación obligatoria del
  perfil mínimo por `dry`, `run` y creación, y la precedencia del rechazo
  funcional sobre el de scope. Con el cambio añade que scope, aceptación y
  contrato funcional no importan el catálogo ni contienen identificadores de
  perfil, que los sitios de admisión consultan las declaraciones y que los
  esquemas del lector siguen al catálogo.

Verificación: la suite completa conserva el mismo conjunto de fallos (solo
bloqueos del sandbox) antes (2.193 tests) y después (2.207), con los tests nuevos
en verde. El replay del corpus coincide con S5 y `surface_check.sh` reproduce
`dry` y `status` byte a byte; los módulos tocados importan en frío. Los tres
archives Herdr del corpus son de esquema 3, así que el mapa derivado para los
esquemas 6, 7 y 8 solo lo ejercitan los tests (Research, mínimo y
`test_fleet_herdr_scope` con archive v8), no el replay.

## Entrega S7: mecanismos comunes de Owner Cycle (2026-09-27)

Interpretación en D7. Evidencia en `outputs/refactor-20260927-s7/`.

- `fleet_herdr_profile` declara `OWNER_WORK_PROFILE` junto a las políticas de S6.
  `fleet_herdr_work_packet` la consulta para admitir la proyección, para el
  perfil del envelope y para la versión de permisos del Worker; el mensaje
  `owner work projection requires sol_minimal_v1` y los valores hasheados del
  envelope no cambian.
- `fleet_herdr_owner_contract` declara las familias `HARNESS_CONTRACT_VERSIONS`
  (v3/v4, carril harness) y `DELIVERED_ADMISSION_VERSIONS` (v1/v3/v4, cuya
  respuesta llega como evento `response`; v2 se observa de forma nativa), que
  sustituyen seis conjuntos literales en `owner_contract`, `owner_runtime`,
  `owner_functional` y `owner_cycle`.
- `permission_drift` en `fleet_herdr_owner_contract` es el único hogar de la
  comparación de permisos registrados (`writable_roots: []` tolerado, JSON
  canónico que distingue booleanos de enteros, orden `cwd`, `approval_policy`,
  `sandbox_policy`). `owner_runtime.verify_terminal` y `owner_native.observe`
  conservan su clase de excepción y su mensaje; los bucles extraídos son
  AST-equivalentes salvo `raise` → `return key` (`drift-ast.json`). No se
  enruta por `fleet_herdr_permissions.attest`, que comprueba otros campos con
  otros mensajes.
- `tests/test_fleet_herdr_owner_common.py` fija, en verde sobre el árbol sin
  cambios, los mensajes exactos de deriva de permisos (terminal y nativo, primera
  clave, `writable_roots`, booleanos) y el rechazo de proyección para Legacy y
  Research, que ningún test fijaba por texto; con el cambio añade que cada
  familia y la regla de `writable_roots` tienen un solo hogar entre los módulos
  `fleet_herdr_owner_*` y que la proyección consulta la política declarada.

Revisado y conservado a propósito:

- Las dos `verify_quiescence`: parametrizarlas calcularía `resource()` antes de
  `exact()` y cambiaría el error ante una observación malformada.
- `owner_cycle` compara el final con `hashlib.sha256` en línea: `artifact_id`
  codifica `str`, y hoy un `TypeError` se registra como error de retención.
- La comprobación de pins de `owner_protocol.verify_receipt` usa otra clase y
  otro mensaje que `contracts.pin`.
- El emparejamiento de llamadas a herramientas de `owner_runtime` y
  `owner_native` tiene semántica distinta (rechazo inmediato frente a marca
  `unsafe` reevaluada en cada observación).

Verificación: la suite completa conserva el mismo conjunto de fallos (solo
bloqueos del sandbox) antes (2.207 tests, la suite posterior de S6 sobre el mismo
árbol commiteado) y después (2.217), con los tests nuevos en verde. Los 11 Owner
Cycles de conformance generados con el árbol anterior verifican con el código
nuevo, sello incluido, y los regenerados dan los mismos resultados
(`conformance.json`). El replay del corpus coincide con S6, incluido el sello de
pilot-07; pilot-07 es contrato v4 (carril harness), así que el camino nativo
v1/v2 cambiado solo lo ejercitan los tests y conformance. `surface_check.sh`
reproduce `dry` y `status` byte a byte y los módulos tocados importan en frío.

Sigue S8. El pipeline predeterminado y las políticas de reparación todavía no se
han refactorizado.
