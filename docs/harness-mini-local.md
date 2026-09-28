# Mini: conformidad local y preparación de piloto

El perfil opt-in `harness_mini_local_v2` usa el owner-cycle v3 local y sintético.
`harness_mini_herdr_v1` añade owner-cycle v4 y un proceso CONTROL exclusivo
lanzado por Herdr. El transporte nativo v2 por pane conserva su gate: Herdr
no ofrece el envío/cancelación condicional que ese contrato requiere.
La configuración solicitada del modelo y su identidad observada son campos
distintos; las pruebas sintéticas no acreditan identidad, calidad ni facturación.

## Ejecutar las comprobaciones

Requiere las dependencias Mini 2.4.6 ya presentes y la imagen local fijada en
`fleet_harness_sandbox.IMAGE`. Los comandos no instalan paquetes ni descargan
imágenes. La infraestructura Docker propia necesita autorización aplicable.

Las fuentes Mini proceden de `requirements/mini-2.4.6.txt`, fijado por hash. Se
buscan en `FLEET_MINI_DIST` o, si no está definido, en
`$XDG_DATA_HOME/fleet-harness/mini-2.4.6/dist` (por defecto bajo
`~/.local/share`). `scripts/install-mini-deps.sh [destino]` las instala para la
plataforma Linux de la arquitectura local, porque Mini se ejecuta en el sandbox
Linux; esa instalación descarga paquetes y debe ejecutarse de forma explícita.
Una instalación anterior se puede reutilizar apuntando `FLEET_MINI_DIST` a ella.

```sh
FLEET_HARNESS_LOCAL_TESTS=1 python3 -B -m unittest \
  tests.test_fleet_harness_acceptance tests.test_fleet_harness_budget \
  tests.test_fleet_harness_recovery tests.test_fleet_harness_mini \
  tests.test_fleet_harness_executor tests.test_fleet_harness_cycle \
  tests.test_fleet_harness_campaign tests.test_fleet_harness_https
FLEET_HARNESS_LOCAL_TESTS=1 python3 -B -m unittest \
  tests.test_fleet_harness_control tests.test_fleet_harness_live_budget \
  tests.test_fleet_harness_live_cycle tests.test_fleet_harness_live_campaign
```

Las pruebas crean repositorios Git desechables como fixtures; no crean commits
en el checkout de Fleet. Sin la variable explícita se omiten las pruebas Docker.
Una suite con omisiones no demuestra conformidad completa.

## Autoridad y aceptación

El Worker recibe el WorkPacket público v3, instrucciones fijadas, alcance y
`/bridge/public-checks.json`. La suite privada permanece en CONTROL. Su hash,
custodia y versión se fijan antes de admitir la tarea. El mantenedor y el revisor
conocen esos casos: no se presenta la evaluación como ciega para ellos.

El cliente HTTP, Mini CONTROL, el ejecutor de comandos y el checker son recursos
separados. El ejecutor carece de red, credenciales y sockets de control; su árbol
es readonly salvo archivos existentes admitidos y temporales declarados.
El manifiesto `public-read-manifest-v1` declara exactamente los editables y los
archivos públicos de solo lectura, con hashes fijados en la creación. Por
defecto sólo se proyectan los editables; el piloto añade explícitamente `SPEC.md`.
El inventario completo, incluidos ignored, sigue perteneciendo al chequeo de
scope; no se copia al ejecutor. No se infiere que un archivo sea público por
estar tracked o por no parecer una credencial. Quien prepara el contrato debe
declarar únicamente contenido público: esto no clasifica secretos por contenido.

El ejecutor v2 conserva inventarios exactos antes y después del comando, enlazados
al recurso montado y a la publicación. Revalida archivos, directorios, tipos,
modos, hashes readonly e identidad al recuperar, aunque exista un resultado.
Una proyección con archivos extra, incluso `.git`, se rechaza antes de enviar
un comando o publicar bytes. Los contratos y archivos v1 conservan su lector
histórico; no pueden iniciar ejecuciones nuevas ni adquirir autoridad v2.
La preparación tiene una identidad estable y publicaciones atómicas recuperables,
incluso si se interrumpe antes de conservar el intent de ejecución. `bridge`
también exige inventario exacto; `public-checks.json` debe coincidir byte a byte
con la proyección pública del contrato, al ejecutar, recuperar y verificar el
archivo. Un hash autodeclarado no basta para acreditar custodia.
`/tmp/pycache` recibe bytecode automático y la compilación explícita sólo puede
escribir donde el sistema operativo lo permite. No admite renombrar o borrar
los mountpoints editables. El estado final del árbol y la prevención de escrituras
transitorias son comprobaciones diferentes.

CONTROL nunca importa el candidato. El adaptador RPC se ejecuta dentro de un
contenedor con recursos limitados y CONTROL compara respuestas y snapshots
externos. Para conceder PASS a Python general también exige una revisión
independiente del código exacto contra interferencia con ese adaptador. Los
controles admitidos están fijados en `tests/fixtures/harness_v1/source-admissions.json`.
Código nuevo sin revisión conserva `independent_source_admission_required`;
`register_source_review()` puede adjuntar una revisión vinculada al check ya
congelado sin alterar el resultado original. Esta política no es una prueba de
seguridad de Python hostil arbitrario ni de corrección universal.

El checker `d1d2-contract-rpc-v1` incluye oráculos públicos D1/D2, controles
positivos revisados y mutaciones defectuosas. La inmutabilidad de campos del
recibo D1 está explícita en esta versión. No debe reinterpretarse como único
fundamento para rechazar candidatos históricos cuyo contrato era ambiguo.

## Reparación y recuperación

Cada tarea dispone de un intento y hasta dos reparaciones. Un protocolo terminal
inválido consume una reparación. El presupuesto y el deadline originales cubren
todo el ciclo. Antes de cada HTTP se conserva la reserva; una respuesta ambigua
impide reenviar o admitir otra solicitud. Uso desconocido y coste facturado
permanecen `null`. La reserva monetaria cubre todos los tokens reservados a la
tarifa estimada fijada; no es un recibo de facturación.

Cada final conserva el original Mini, su extracción y vínculos con admisión,
tarea, herramientas, revisión y solicitudes/respuestas HTTP. El replay exige
conservar las reservas de los intentos anteriores, incluidos los inválidos.
El owner journal mantiene la autoridad de admisión, revisión y cierre.
Los nuevos deliveries fallidos pueden incluir `mini-public-failure-v1`: distingue
una respuesta `length` vacía y sin acciones del límite de solicitudes agotado.
El diagnóstico deriva esperado/observado de los originales verificados y fija
admisión, revisión previa (o `null`) y presupuesto. No copia razonamiento ni texto
del proveedor. `response_sha256` identifica el objeto JSON canónico; el hash del
presupuesto vincula los bytes HTTP originales. La reparación recibe ese
diagnóstico, sin renovar límites ni convertir un fallo en una entrega válida.
Los deliveries históricos sin suplemento conservan su clasificación original.
En el perfil v2, un evento `input_snapshot` conserva el inventario de entrada de
cada admisión antes del envío. Recuperar no vuelve a capturarlo. La primera
herramienta debe coincidir con esos hashes y tamaños; las publicaciones enlazan
cada comando con el siguiente y la última con el árbol congelado. Un cambio
antes de la primera ejecución bloquea el intento sin refrescar el ancla. Una
reparación de protocolo posterior a escrituras válidas recibe su propia entrada
durable, conservando el mismo límite total.

Un checker interrumpido puede ocupar hasta dos generaciones de recuperación
adicionales sobre el mismo árbol. Primero debe probar la quiescencia del recurso
anterior; la cadena y los originales quedan vinculados al resultado siguiente.
Un contraejemplo completo retenido en el RPC obliga a reparar, incluso si falta
su observación derivada. Una observación de filesystem incompleta no demuestra
PASS. Salida parcial y resultado semántico se conservan separados de limpieza.

La recuperación no inventa identidad de un proceso de attach. Un arranque ambiguo
sin identidad recuperable, un recurso no observado o un cleanup fallido dejan una
dependencia concreta; no permiten aceptar ni iniciar otro Worker.

## Artefacto de piloto

```sh
python3 -B scripts/fleet_harness_campaign.py prepare /RUTA/NUEVA/piloto
python3 -B scripts/fleet_harness_campaign.py validate /RUTA/NUEVA/piloto \
  --plan-sha256 HASH_DEVUELTO
python3 -B scripts/fleet_harness_campaign.py supervise /RUTA/NUEVA/piloto \
  --plan-sha256 HASH_DEVUELTO --provider synthetic
python3 -B scripts/fleet_harness_campaign.py cancel /RUTA/NUEVA/piloto \
  --plan-sha256 HASH_DEVUELTO
```

La preparación fija dos tareas conocidas D1/D2, casos privados, dependencias,
fuentes, identidad y límites: 12 solicitudes en total, 20 minutos, 1+2 intentos
por tarea y USD 1,20 de reservas estimadas bajo la política sintética. El
supervisor conserva un `flock` exclusivo, admite por journal/CAS y recupera el
mismo contrato. `completed.json` se contrasta con los archivos de ambos ciclos.
El canario sintético restaura controles conocidos: no mide capacidad del modelo.

La cancelación explícita conserva su solicitud antes de reconciliar recursos.
Las señales del proceso se convierten en una intención durable; hasta entonces
un `Event` en memoria puede perderse con una caída. El cierre comprueba esa
intención bajo el mismo lock que la cancelación. Un error del watcher bloquea
la reanudación; tras cinco segundos sin drenaje registra la dependencia y
conserva el lease hasta que termine el hilo. Cinco segundos no constituyen una
garantía de cleanup. Los terminales verificados no se reabren al pedir cancel.

Se genera un `herdr-plugin.toml` local, sin registrarlo. Herdr 0.9.0 requiere
registrar el plugin antes de abrir su pane; `plugin link` cambia el registro
global del usuario. Esa acción necesita autorización específica. Tampoco se
ha autorizado una nueva campaña pagada. El modo live rechaza por defecto.
El piloto pagado exige transporte y cancelación propios demostrados, precios
actuales fijados y un cliente cuya resolución DNS y HTTP estén acotados.

`fleet_harness_https` prepara esa última capacidad sin otorgar autorización:
resolver separado sin credenciales, identidad del hijo, terminación y originales;
una IP fijada, sin fallback; TLS con hostname/SAN verificados; `exchange()`
conserva el socket también durante la lectura del cuerpo. El reloj monotónico
limita DNS y HTTP además del deadline civil. La vigencia de cinco minutos es una
política local, no un TTL observado. La preparación DNS incompleta no se reintenta
en el mismo directorio. El ledger v1 sigue rechazando solicitudes live. El ledger
v2 usa v1 exclusivamente como subledger financiero; vincula el HTTPS propio a la
admisión, payload completo, generación, lanzamiento Herdr, reloj original y
aprobación externa fijada.

## Supervisor CONTROL v4

```sh
python3 -B scripts/fleet_harness_live_campaign.py prepare /RUTA/NUEVA/piloto \
  --mode synthetic_tls
python3 -B scripts/fleet_harness_live_campaign.py validate /RUTA/NUEVA/piloto \
  --plan-sha256 HASH_DEVUELTO
```

Esta preparación genera `owned-control-plugin.toml`. Con autorización de registro,
se enlaza ese manifiesto al plugin propio `fleet.harness-control-e4` y se abre su
entrypoint `supervisor` en un tab propio, con `--no-focus`. El supervisor comprueba
los originales Herdr del proceso exacto, argv, cwd y terminal antes de enviar.
El `flock` continuo de CONTROL, su identidad de proceso y la raíz física sostienen
la exclusividad; la observación Herdr es procedencia del lanzamiento. Una copia
de un recibo, de la raíz, o un guard heredado por fork no confieren autoridad.

Las reservas, envíos, originales TLS y generaciones son inmutables. Una caída
tras reservar/enviar sin respuesta reconciliable mantiene consumo desconocido y
bloquea el reenvío. Una respuesta completa retenida se verifica antes de usarla.
Los mensajes derivados conservan `reasoning_content`; no se alteran los originales
del proveedor. El deadline monotónico está ligado al boot original: reiniciar
el proceso no lo renueva; un cambio de boot requiere una dependencia explícita.

La proyección `deepseek-mini-wire-v3` admite el campo opcional `index` observado
en llamadas de herramientas de la API: exige enteros exactos consecutivos en el
orden recibido y lo retira únicamente de la copia derivada. No reordena acciones,
no cambia IDs ni argumentos y no elimina campos desconocidos. El contrato de
presupuesto fija la versión; las evidencias v2 conservan su interpretación y
rechazo originales. Cambiar el adaptador requiere una preparación nueva; no
reabre un intento agotado ni renueva autorización, presupuesto o deadline.

El transporte conserva 30 segundos para conexión, handshake y envío. Una vez
enviado el request, esperar headers y cuerpo utiliza el tiempo restante del
deadline original y de la vigencia DNS, con límite monotónico y cancelación del
socket exacto. No impone 30 segundos de silencio a una inferencia en curso ni
renueva tiempo por bloque. Una respuesta incompleta sigue reteniendo su reserva
y bloqueando reenvíos; esta corrección no recupera respuestas ya perdidas.
El guardian interrumpe el socket; el hilo de la operación cierra los buffers HTTP
para evitar cierres concurrentes. El ejecutor de comandos requiere EOF y salida
del hijo antes de informar terminación normal. Reserva dentro del mismo deadline
tiempo para reap y respuesta RPC; truncación y timeout son observaciones distintas.

La preparación live usa `--mode live --pricing-evidence ARCHIVO`. No admite gasto
ni lee la clave. `--credential-source file` declara alternativamente el archivo
privado `provider-key` en la raíz CONTROL, con modo 0600, propietario actual y un
solo link; rechaza symlinks. El contenido no entra en argv, manifiestos, candidatos
ni evidencia. Esta opción permite cargar la clave sin cambiar el entorno global
de Herdr. El operador crea ese archivo mediante entrada oculta, fuera del chat.
Una autorización humana concreta permite posteriormente el
ingreso `authorize --human-authorization-reference REFERENCIA`, ligado al hash
exacto y a los límites preparados. Por defecto son dos tareas, 12 solicitudes,
20 minutos y USD 1,20 de reservas estimadas. `prepare --requests-per-task 12`
prepara 24 solicitudes y USD 2,40, repartidos por igual entre D1/D2. Admite de
1 a 30 solicitudes por tarea, dentro del límite existente de tokens; cada una
reserva USD 0,10. El presupuesto y los límites de tiempo y reparaciones siguen
compartidos por todos los intentos. Esta opción sólo configura una preparación
nueva: no altera ejecuciones previas ni reutiliza su aprobación. El ingreso
revalida cada presupuesto de tarea, los agregados y la identidad de la campaña.
La opción opt-in `--output-profile thinking-32k-v1` fija `max_tokens=32768`
mediante `deepseek-mini-wire-v4`, conservando thinking/max. Reserva 131072 tokens
y USD 0,20 por solicitud: con 12 solicitudes por tarea prepara 24 solicitudes y
USD 4,80 en total. Permite hasta 15 solicitudes por tarea para conservar el tope
original de dos millones de tokens. Un request debe caber completo (bytes JSON,
1024 de margen y salida máxima) dentro de su reserva; el exceso se rechaza antes
del envío. La reserva anterior de 65536 no basta para 32K de salida y algunos
prompts observados. No cambia el deadline, los intentos ni las reglas de aceptación.

El perfil anterior `legacy-8k` y los lectores wire-v2/v3 conservan 8192 de salida;
el nuevo perfil requiere CONTROL, nunca el supervisor local histórico. Su
preparación y aprobación incluyen el perfil, reservas y coste nuevos. Un pago
anterior no lo autoriza. Los tests sintéticos demuestran configuración, rechazo
de excesos, entrega y recuperación; no demuestran que 32K basten para DeepSeek.
La [API oficial](https://api-docs.deepseek.com/api/create-chat-completion/)
permite salidas mayores; este límite opt-in es una decisión experimental acotada,
no el máximo ni el default del proveedor. El presupuesto de salida puede seguir
agotándose y los deadlines originales siguen aplicándose.

La referencia segura (`env:DEEPSEEK_API_KEY` o el archivo exacto) sólo se resuelve
en CONTROL y después de comprobar la autorización.
Los casos privados no se montan ni se entregan a Mini. Los controles sintéticos
restauran código conocido; su éxito no acredita calidad de DeepSeek.

```sh
python3 -B scripts/fleet_harness_live_campaign.py cancel /RUTA/piloto \
  --plan-sha256 HASH
python3 -B scripts/fleet_harness_live_campaign.py reconcile /RUTA/piloto \
  --plan-sha256 HASH
python3 -B scripts/fleet_harness_live_campaign.py source-review /RUTA/piloto \
  --plan-sha256 HASH --task D1 --binding-sha256 CHECK_ACTUAL --review REVISION.json
python3 -B scripts/fleet_harness_live_campaign.py verify /RUTA/piloto \
  --plan-sha256 HASH
```

La cancelación detiene toda la campaña serial. `reconcile` no carga credenciales,
no envía solicitudes y sólo limpia recursos ligados a admisiones/checks del
journal, rutas de creación y CIDs propios. Continúa con la otra tarea si un
registro falla; el resultado conserva la dependencia. No contar un error de
observación como ausencia. `source-review` admite un suplemento independiente
de los bytes congelados actuales; no ejecuta el candidato ni concede aceptación.
La reanudación desde el mismo plugin conserva presupuesto y reloj, incluido el
tiempo usado en revisión. Un timeout sigue siendo agotamiento.

`control-completed.json` requiere archivos aceptados de ambas tareas, servicios
locales drenados y release de la generación exacta. La recuperación puede completar
un release faltante sólo tras verificar los archivos y la muerte del proceso
anterior. Una señal en memoria aún no persistida puede perderse con el proceso;
la solicitud durable es la frontera de cancelación confirmada.

La comparación posterior de 12 tareas × 3 repeticiones por configuración debe
cubrir las familias del playbook y fijar rúbricas para tareas generales. Estas
dos tareas Python no sustituyen esa muestra. No se promueve un perfil con esta
conformidad local.

## Reutilizar resultados históricos

```sh
python3 -B scripts/fleet_harness_reevaluate.py \
  /RUTA/evidencia-historica /RUTA/NUEVA/reevaluacion-derivada
```

La herramienta copia únicamente fuentes fijadas a recursos de evaluación,
conserva procedencia/versiones y compara el inventario histórico antes/después.
No ejecuta proveedores ni modifica los ocho intentos. Su comprobación de scope
final no prueba ausencia de escrituras transitorias históricas.

Los resultados distinguen siempre: listo offline, listo live y calidad demostrada.
