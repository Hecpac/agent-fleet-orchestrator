# Arranque de una sesión privada Herdr/Codex

El backend oficial actual fija Herdr 0.9.0/Codex 0.153.4 y aplica el guard de
superficie antes de cada envío. Consulta el [contrato de versiones](herdr-cli-versions.md):
los ensayos históricos descritos abajo no prueban la recuperación después de
reiniciar el servidor con las versiones nuevas.

La reproducción de FitScan usa el backend Herdr real y el perfil existente de
cuatro roles. El controlador operativo está en
`outputs/frh47_east/drive.py`; consulta `mission.json` para la identidad vigente.
El controlador añade las restricciones literales de la tarea al prompt, usa el
lock normal y conserva las admisiones, CAS, validación de resultados y archivo
del driver original. No modifica modelos, routing ni aceptación.

## Preflight necesario

1. Usar un `HOME`, `CODEX_HOME` y espacio XDG privados. La autenticación de archivo
   requiere autorización explícita. No leer ni copiar sus valores al informe.
2. Comprobar la longitud en bytes del socket **del cliente**, además del socket
   API. En el ensayo macOS, la ruta API cabía y el sufijo `herdr-client.sock`
   excedía el límite. `validate_local_socket_path` rechaza la ruta antes del boot.
3. Ejecutar Herdr sin envolverlo en otro `sandbox-exec`. Codex debe aplicar el
   sandbox de cada herramienta una sola vez. La envoltura adicional del ensayo
   inicial produjo `sandbox_apply: Operation not permitted`, incluso para una
   lectura. Esto no autoriza desactivar el sandbox de Codex.
4. Incorporar el hook `SessionStart` incluido en el binario Herdr existente,
   exclusivamente en el perfil privado. El comando soportado es
   `herdr integration install codex`; escribe el script y la configuración local,
   sin instalar un paquete ni descargar un ejecutable. Revisar los bytes antes
   de confiar en el hook: se ejecuta fuera del sandbox de herramientas.
5. Resolver el diálogo de confianza del hook con autorización para ese hook
   exacto. `features.hooks=true` y el archivo `hooks.json` no bastan: el TUI puede
   mostrar un hook instalado y cero activos. El ensayo autorizó únicamente el
   hook suministrado por Herdr, en ese perfil privado.
6. Desactivar el aviso de actualización únicamente en el perfil privado o
   seleccionar `Skip`. Herdr llegó a informar `interactive_ready=true` mientras
   Codex esperaba una elección de actualización o confianza.
7. Antes de admitir Plan, leer las cuatro superficies exactas con Herdr. El
   helper `codex_startup_blocker` rechaza los diálogos observados y exige el
   prompt de entrada de la versión probada. El controlador local lo aplica
   antes de reservar una primera tarea. No pulsa elecciones ni concede permisos.
8. La identidad de sesión puede aparecer al crear el primer turno. No inventarla
   desde el nombre del panel. El hook debe reportarla a Herdr, y el collector
   original debe verificar identidad, prompt, turno, respuesta final y CAS.

El helper de superficie es conservador y específico del TUI Codex 0.153
observado. Una versión, idioma o superficie distintos pueden necesitar otra
comprobación. Un prompt visible no demuestra que el modelo haya trabajado ni
que la política efectiva esté atestada.

## Objetivo y restricciones

El clasificador existente busca palabras en el objetivo, sin interpretar
negaciones. `No ... hacer commit, push o deploy` elevó una revisión local a riesgo
alto por la palabra `deploy`. El intento quedó registrado y fue retirado por el
controlador. El objetivo positivo se volvió a expresar con la misma revisión
local; el párrafo completo de restricciones se conservó literalmente en CAS y
en `scope_constraints` de cada prompt real. No se cambió el clasificador ni se
redujo el riesgo de un registro existente. Esta separación no sirve para ocultar
efectos solicitados: toda acción positiva debe permanecer en el objetivo evaluado.

## Recuperación y evidencia

Una entrada consumida por un diálogo puede tener un dispatch durable sin turno
Codex. Reconciliar el mismo intento; no reenviar a ciegas. El ensayo retiró esos
intentos mediante cancelación normal y comprobó quiescencia antes de crear una
Mission nueva enlazada. La respuesta `BLOCKED` del primer Lead se conserva como
diagnóstico sin inventar el enlace Herdr que faltaba. Un intento retirado no se
presenta como completado correctamente.

Pruebas locales del helper:

```sh
python3 -B -m unittest discover -s tests -p test_fleet_herdr_startup.py
git diff --check
```

Continuación explícita de la Mission registrada, sin duplicar envíos:

```sh
python3 -B outputs/frh47_east/drive.py
```

## Límites

Este ensayo demuestra entrega y recogida por la ruta ordinaria. La atestación
nativa completa del Worker sigue siendo una capa separada:
`INTEGRATION_BINDING=NOT_VERIFIED` hasta unir política efectiva, proceso, efectos
externos y evidencia protegida en una misma operación.

Al quitar la envoltura adicional, la sesión padre ya no tiene aquella denegación
OS específica de Keychain y source. Se conserva autenticación por archivo, el
sandbox de cada rol y la comprobación externa de hashes. No se afirma aislamiento
de lectura frente a HOME, otros procesos del mismo usuario o todos los temporales.
Las advertencias de Git/xcrun sobre un TMPDIR no escribible se deben reportar
según su efecto real; no convierten por sí solas una lectura exitosa en un fallo.
