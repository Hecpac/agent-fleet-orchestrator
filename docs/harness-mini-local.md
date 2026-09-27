# Mini: conformidad local y preparación de piloto

El perfil opt-in `harness_mini_local_v2` usa el owner-cycle v3. Su transporte
admitido es local y sintético. El gate de transporte real Herdr permanece cerrado.
La configuración solicitada del modelo y su identidad observada son campos
distintos; las pruebas sintéticas no acreditan identidad, calidad ni facturación.

## Ejecutar las comprobaciones

Requiere las dependencias Mini 2.4.6 ya presentes y la imagen local fijada en
`fleet_harness_sandbox.IMAGE`. Los comandos no instalan paquetes ni descargan
imágenes. La infraestructura Docker propia necesita autorización aplicable.

```sh
FLEET_HARNESS_LOCAL_TESTS=1 python3 -B -m unittest \
  tests.test_fleet_harness_acceptance tests.test_fleet_harness_budget \
  tests.test_fleet_harness_recovery tests.test_fleet_harness_mini \
  tests.test_fleet_harness_executor tests.test_fleet_harness_cycle \
  tests.test_fleet_harness_campaign tests.test_fleet_harness_https
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
política local, no un TTL observado. La preparación incompleta no se reintenta
en el mismo directorio. Todavía falta vincular este transporte al contrato live
admitido por CONTROL y a su archivo de autorización: el ledger v1 rechaza toda
solicitud live, incluso si recibe una credencial o un diccionario de aprobación.

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
