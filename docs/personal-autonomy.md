# Flota personal: arranque, tareas y limpieza

El carril `official-cli-personal-v1` usa la autenticación y el transporte del CLI
oficial. Nuevas Missions Astra/Sol sin manifiesto experimental seleccionan este
carril. Los registros existentes conservan su selección y contrato de versiones.
La ruta personal mantiene admisiones, candidato separado, CAS, pausa/cancelación,
verificación de resultados y archivo. No afirma aislamiento frente a código hostil.

El contrato personal actual solicita Herdr 0.9.0 y Codex CLI 0.154.0. El contrato
oficial histórico de 0.153.4 sigue reconocido. La versión de una sesión y sus
permisos se contrastan con el transcript; un CLI instalado no prueba que un
modelo esté disponible ni que una herramienta funcione.

## Operación

Las recetas personales usan Python 3.12 o superior. `FLEET_PYTHON` permite elegir
un intérprete ya instalado, sin modificar el PATH o la configuración global.
El PATH del controlador y de los panes importa: el diagnóstico muestra el
ejecutable resuelto, porque pueden coexistir versiones diferentes de Codex.

1. `just personal-check /ruta/al/repo` comprueba versiones, login y limpieza del
   objetivo. Informa capacidades anunciadas por el CLI; no inicia agentes ni
   envía prompts. Un resultado compatible todavía requiere objetivo, sesión y
   contrato para ejecutar una Mission.
2. Si hay cambios locales, `just personal-snapshot /ruta/al/repo /ruta/nueva/fuera/del/repo`
   crea una copia privada limpia. Conserva ediciones, archivos nuevos y borrados,
   sin cambiar el índice ni las referencias del origen. Si hay archivos ignorados,
   hay que seleccionar explícitamente `--exclude-ignored`; el recibo enumera las
   exclusiones. Rechaza submódulos, symlinks, hardlinks e índices que oculten cambios.
   El commit de snapshot se crea solo en la copia privada; los roles no obtienen
   autorización para crear commits. Un fallo conserva el directorio parcial y no
   publica un recibo `VERIFIED`.
3. `just personal-prepare /ruta/privada/pool /ruta/snapshot/target default` levanta
   Lead, Worker, Reviewer y Verifier en una cuadrícula de Herdr. Todos permanecen
   `read-only` mientras esperan. No hay Mission ni prompts de trabajo y el estado
   del pool no tiene autoridad de aceptación. Repetir la orden observa el pool
   existente; un envío ambiguo nunca se repite automáticamente.
4. `just personal-show /ruta/privada/pool` comprueba las identidades y superficies
   actuales. `ready` significa disponible para input, no trabajo completado.
   Los diálogos de autenticación, hooks o actualización quedan visibles y no se
   responden automáticamente.
5. `just personal-assign /ruta/privada/pool feature "Objetivo" /ruta/contrato.json`
   congela la asignación y cierra el pool exacto antes de iniciar las sesiones de
   Mission Control. La primera versión reemplaza las sesiones vacías; no adopta
   conversaciones como evidencia de una Mission. Una repetición usa la asignación
   original y, cuando está registrada, la misma Mission. `FLEET_RUNS_DIR` selecciona
   el almacén. Pausa, recuperación, cancelación y archivo usan las recetas normales.
6. `just personal-close /ruta/privada/pool` cierra solo su workspace comprobado y
   rechaza agentes que estén trabajando. El cierre comprueba desaparición del
   workspace; no demuestra contención de descendientes arbitrarios.

## Limpieza y compatibilidad

Los generadores personales de currículum se retiran de `scripts/` conservando sus
bytes fuera del candidato de producto. No se elimina el trabajo experimental de
otra sesión. El broker de suscripción, cápsula y launcher instrumentado siguen
siendo opt-in y no participan en el transporte de la ruta personal.

`legacy-fleet` y `legacy-fleet-down` nombran explícitamente las recetas CMUX. Los
nombres antiguos se conservan por compatibilidad. Fusion, FDP-2/FDP-3, WORM y el
runner estadístico conservan sus entradas y lectores existentes.

## Siguientes bloques del plan

Este bloque implementa el arranque personal y la preparación sin tarea, la copia
exacta de fuentes seleccionadas y el diagnóstico de capacidades. Los permisos
de las Missions siguen siendo v1: un único escritor, lectores read-only, red de
shell del Worker desactivada. No se presenta esta entrega como autonomía ilimitada.

Quedan por implementar los grants de herramientas/red por tarea con evidencia,
descubrimiento efectivo de herramientas por sesión, flujo adaptativo de etapas,
relevo de conversaciones después de reiniciar Herdr y extracción de validadores
offline del runtime experimental. Esos cambios requieren sus propios contratos
versionados y pruebas; eliminar guards no los sustituye.

Pruebas focalizadas sin proveedores:

```sh
python3.12 -B -m unittest tests.test_fleet_herdr_personal tests.test_fleet_personal_pool tests.test_fleet_personal_snapshot tests.test_fleet_personal_preflight
git diff --check
```
