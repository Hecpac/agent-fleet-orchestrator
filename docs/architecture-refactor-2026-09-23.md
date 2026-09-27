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
| S2 | Lectores, contratos y operaciones puras | Extracción inicial verificada; decoders históricos y resto de readers pendientes |
| S3 | Composición moderna y compatibilidad | Pendiente |
| S4 | Catálogo de perfiles e identidad | Pendiente |
| S5 | Telemetría separada de autoridad | Pendiente |
| S6 | Scope y aceptación independientes del perfil | Pendiente |
| S7 | Mecanismos comunes de Owner Cycle | Pendiente |
| S8 | Política opt-in de reparación | Pendiente; calidad live requiere autorización propia |
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

S2 continúa con decoders históricos explícitos y la separación de los readers
restantes. Su criterio es conservar o explicar los resultados según la versión
original; no convertir los 13 rechazos en aceptación relajando validaciones.
Después sigue S3. El pipeline predeterminado, las políticas de reparación y
la telemetría todavía no se han refactorizado.
