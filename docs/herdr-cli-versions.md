# Contrato de versiones del CLI

El nuevo carril personal usa **Herdr 0.9.0 y Codex CLI 0.154.0**.
El contrato ordinario anterior de **0.153.4** se conserva sin migración implícita.
La matriz está definida en `scripts/fleet_herdr_versions.py`; no se acepta un
rango abierto de versiones ni se modifica automáticamente un run histórico.
Modelos, routing, política de permisos y contratos de resultado conservan sus
valores anteriores. La comprobación de versión añade una restricción a la
evidencia nueva; no concede autoridad a stdout ni a una respuesta del modelo.

| Carril | Estado durable | Reglas |
|---|---|---|
| CLI personal nuevo | `schema_version=3`, `backend_version=0.9.0`, `runtime_contract` | Exige `cli_version=0.154.0`; el arranque en espera no demuestra inferencia |
| CLI oficial anterior | `schema_version=3`, `backend_version=0.9.0`, `runtime_contract` | Verifica ambos CLI antes de efectos; el transcript de resultado debe declarar `cli_version=0.153.4` |
| Backend histórico | `schema_version=2`, `backend_version=0.8.2`, sin `runtime_contract` | Sigue siendo legible sin consultar binarios. Sus operaciones conservan la comprobación histórica de Herdr 0.8.2; no se convierten al carril nuevo |
| Launcher experimental instrumentado | Manifiestos v1/v2 originales | Mantiene Herdr 0.8.2 y Codex 0.153.0 fijados por identidad de archivo. El CLI oficial nuevo no incorpora sus extensiones |

La ruta observada por `codex --version` es la seleccionada por el PATH del
controlador. Esa observación y el `cli_version` del transcript son comprobaciones
de compatibilidad; no son una atestación criptográfica de la imagen de proceso
ejecutada por un servidor remoto o ya existente.

## Persistencia y lectura histórica

El contrato nuevo se guarda con el estado y, una sola vez, en
`missions/MISSION_UUID/herdr-runtime-contract.json`, vinculado a `mission_id` y
`compiled_digest`. Las lecturas ordinarias de backend, `mission status`, generación
de cancelación e informe usan el mismo lector offline para comparar ambos registros.
Un cambio de versión, un contrato ausente o la conversión del estado a v2 conservando el registro
congelado se rechazan. Si una interrupción deja sólo el contrato inicial y falta
el estado, el backend se detiene: no crea silenciosamente otra generación.

Los resultados nuevos incluyen el mismo contrato dentro de su evidencia CAS.
La recuperación de un resultado comprueba el contrato contra el estado, y el
verificador del transcript contrasta la versión Codex declarada. El informe compara
también el contrato del resultado con el backend validado: si falta o difiere, o
si la versión del transcript no coincide, no presenta esa identidad como observada.
Eliminar el campo y volver a sellar el resultado no permite recuperar un resultado nuevo
como si fuera histórico.

No se añaden campos a los resultados antiguos ni se reescriben archivos
terminales al leerlos. La verificación histórica de identidad, permisos y CAS
sigue activa. El registro separado utiliza las protecciones del directorio de
CONTROL; no es una firma ni protege frente a un atacante que controle todos
esos archivos como el mismo usuario del sistema operativo.

## Envío y recuperación

El backend ordinario comprueba la superficie durante el boot y todos los
envíos pasan por el guard de entrega antes de guardar un nuevo intento de
transporte. El guard usa `agent read --source visible` y después vuelve a
comprobar la identidad exacta y el estado del agente. Rechaza avisos de confianza
de proyecto/hooks, actualización, autenticación, `Resuming session` y superficies
sin el prompt reconocido. No pulsa Enter para resolverlos.

Esta comprobación es específica del TUI Codex 0.153 observado, no una API atómica
de admisión de input: cambios de idioma, diálogos desconocidos, texto del historial
y carreras entre lectura y envío siguen siendo limitaciones. Un rechazo del guard
de superficie no crea un nuevo `submission`. La rama previa `agent_status=blocked`
conserva su comportamiento: registra un `submission` terminal `blocked`, sin enviar
prompt ni teclas. Volver a consultar ese run devuelve el mismo rechazo; no lo reenvía.
La admisión del driver es una capa distinta. Un envío cuyo resultado ya sea ambiguo se reconcilia por su
run existente; no se reenvía a ciegas.

**Reiniciar el controlador y reiniciar el servidor Herdr son casos distintos.**
Una instancia nueva del controlador puede recuperar su estado y continuar sobre
la misma sesión y el mismo terminal. Se probó con dos turnos deterministas reales.
Después de cerrar y reabrir Herdr, un agente puede faltar o tener otro
`terminal_id`; el backend se detiene ante esa diferencia. No relaja la identidad
persistida para hacer pasar la recuperación.

Queda pendiente un protocolo explícito para reanudar un proceso después del
reinicio del servidor: debe recuperar la misma conversación con los argumentos
de modelo, confianza y permisos originales, verificar el proceso/superficie
nuevos y registrar el relevo sin cambiar las identidades de runs anteriores.
El comando nativo `resume` y un panel restaurado no satisfacen por sí solos ese
contrato. No debe editarse `herdr-backend.json` para forzar coincidencias.

## Comprobaciones

```sh
python3 -B -m unittest tests.test_fleet_herdr_versions
python3 -B -m unittest tests.test_fleet_herdr_readers tests.test_fleet_herdr_report
python3 -B -m unittest discover -s tests -p 'test_fleet_herdr*.py'
git diff --check
```

El ensayo real de mantenimiento se conserva en
`outputs/vcxdp5z3c/`. Para repetirlo en **otro** directorio privado:

```sh
python3 -B outputs/vcxdp5z3c/replay.py
```

Se usan el backend real, los dos CLI instalados y un endpoint Responses/SSE
determinista en loopback, sin modelo. Arrancan cuatro roles; sólo Worker recibe
los dos prompts. El segundo request debe contener el primer prompt y su
respuesta y conservar la identidad de sesión. Se comprueba también que el
collector productivo rechaza ese proveedor de fixture y que el reinicio del
servidor no provoca un nuevo envío ni una revinculación silenciosa. El ensayo
mantiene aislada la configuración HOME/CODEX_HOME/XDG/TMPDIR y configura únicamente
el hook oficial revisado de Herdr en ese perfil. Cierra sus servidores y registra
los descendientes muestreados, los sockets y los hashes del checkout.

Un PASS de este ensayo acredita esas operaciones concretas. No acredita una
Mission aceptada, las cinco etapas, inferencia de un modelo ni aislamiento del
Worker. El PASS previo de Seatbelt con Codex 0.153.0 no se extrapola a 0.153.4.
`INTEGRATION_BINDING` permanece `NOT_VERIFIED`.
