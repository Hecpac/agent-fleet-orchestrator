# Integración local del runtime Herdr

Estado: implementación local opt-in. `INTEGRATION_BINDING=NOT_VERIFIED`.
Este cambio integra paths, handoffs y observaciones de launch; no declara un
sandbox efectivo atestado ni una campaña real de cuatro modelos verificada.

## Contrato y compatibilidad

`mission-run.py run --herdr-runtime-root /ruta/fisica/privada` selecciona
`herdr_layout.version=2` y `herdr_input_policy=independent-v1`. La raíz debe existir,
pertenecer al usuario, tener modo 0700 y estar separada de runs y del checkout
fuente. Se conserva en runtime-options y creation-request; participa en la clave
de idempotencia. No se migran misiones históricas. Sin la opción se conserva la
ubicación anterior y la lista acumulativa de inputs.

Layout v2:

```text
CONTROL/runs/missions/<mission_id>/  ledger, CAS, intents, observaciones
runtime_root/<mission_id>/candidate/  única raíz de producto del Worker
runtime_root/<mission_id>/roles/<role>/<attempt_id>/  HOME, CODEX_HOME, TMPDIR, XDG
```

Cada Mission reclama un directorio nuevo: una preparación interrumpida requiere
reconciliación, no reutilización silenciosa. Recovery comprueba identidades de
candidate, .git y directorios padres. La comprobación funcional utiliza el
candidate congelado para excluir tests alojados dentro de él, también en v2.

Inputs seleccionados: Build recibe Plan; Review y Verify reciben Plan/Build;
Synthesis recibe los cuatro resultados anteriores. Verify no recibe el veredicto
de Review en su lista de inputs. Esto limita el contexto suministrado; no impide
leer otros objetos CAS cuando el sistema operativo permite esa lectura.

## Camino de launch observado

La opción adicional `--herdr-launch-manifest /ruta/manifest.json` exige v2 y fija
dos imágenes locales mediante realpath, inode/dispositivo, SHA-256 y versión.
Formato: `{"version":1,"codex":{"image":<path_identity>,"version":"codex-cli 0.153.0"},
"herdr":{"image":<path_identity>,"version":"herdr 0.8.2"}}`.
`fleet_herdr_launch.validate_manifest()` verifica los pins sin ejecutar imágenes.
Las observaciones de versión deben obtenerse por separado, con HOME/CODEX_HOME
propios. La versión dentro del registro de launch se etiqueta `pinned_manifest`.

La ruta es:

1. Driver → HerdrBackend → intento `starting` durable con generation/role/attempt.
2. CONTROL prepara y almacena el intent y, para Worker, llama al receptor v2 para
   congelar la política solicitada. El pin queda en el intento del backend.
3. CLI Herdr `agent start --executable <wrapper absoluto>` → nuevo método API
   `agent.start_explicit_v1` → shell del pane → wrapper confiable.
4. El wrapper compara argv/cwd reales, candidate/binario, directorios de entorno,
   generación/intento activo, política congelada, antecedente del ledger y deadline.
5. Persiste una observación CAS y consume el intento de forma exclusiva y durable;
   llama a `os.execve` con el Codex fijado y un entorno generado explícitamente.
   El manifiesto v2 agrega un broker privado antes del mismo exec, sin cambiar
   el PID que Herdr observa para Codex.
6. Antes de enviar un turno, Fleet enlaza run_id/prompt con la observación de launch.

El wrapper se encuentra bajo CONTROL, fuera del candidate y temporales declarados.
`-I` evita importar código de PYTHONPATH/user-site. El entorno explícito contiene
rutas generadas y constantes; no copia secretos ni auth desde HOME del usuario.
Se guardan nombres heredados, digest del entorno sanitizado y argv redactado.
No se persisten valores sensibles heredados. Los intents contienen únicamente el
argv generado por Fleet y ese entorno controlado.

Una CLI/servidor antiguos no deben poder ignorar el executable y lanzar otro Codex:
la CLI nueva usa un método API diferente, que el servidor antiguo rechaza. No hay
fallback a la instalación global. El hash fija la imagen del cliente Herdr; aún
no demuestra la imagen o identidad del servidor que atiende la sesión.

La selección absoluta evita una ampliación considerable del lifecycle de panes.
Se conserva el shell intermedio existente y se captura inmediatamente antes del
exec de Codex. Por ello esto no se presenta como el launch nativo A autenticado
del diseño v2. Los registros tienen `authority=none`, `run_id=null` durante boot y
`INTEGRATION_BINDING=NOT_VERIFIED`. Los posteriores `launch-run-link` tampoco
otorgan autoridad. No se cambian modelos, routing ni aceptación/finalización.

## Lo que falta para verificar la operación real

El punto concreto sigue siendo
`codex-rs/utils/pty/src/native_spawn_gate.rs::install_native_spawn_observer`:
los wrappers existen y la copia local de Codex ofrece ahora un bootstrap
explícito `--native-spawn-observer-fd=N` como primer argumento. El launcher Fleet
lo suministra con el manifiesto v2 descrito más abajo. Es una extensión de la
copia local instrumentada; no una API soportada por Codex upstream 0.153.0.
`core/src/spawn.rs` y los backends pipe/PTY aportan el Command final. Las fuentes
locales ya enlazan `resolved-execution-policy` v1 desde el PermissionProfile de
la solicitud transformada: core shell, unified_exec local, exec-server y su
captura previa de shell. El contexto se limita a la ejecución asíncrona y no
se hereda a tareas/hilos nuevos. No se reconstruye desde flags de Fleet.
El perfil Seatbelt y sus parámetros siguen disponibles en el Command final.
El callback también recibe `codex-tool-execution-context` v1 cuando la llamada
atraviesa `ToolOrchestrator::run_attempt`: session/thread/turn/call, identidad de
herramienta, approval policy del `StepContext` capturado y sandbox del intento.
Ese registro se toma después de la aprobación, dentro del mismo futuro del
runtime. No consulta los defaults mutables de la sesión ni el getter legacy del
turno. `resolved-execution-policy` permanece separado: un adaptador debe consumir
ambos registros y rechazar la ausencia de cualquiera cuando requiera binding.

Las rutas directas sin el orquestador no aportan esa identidad. Los contextos
no se transmiten implícitamente por RPC, tareas/hilos nuevos ni por FDs a hijos.
El canal privado nuevo autentica los mensajes y vincula un digest de launch,
PID y secuencia; el receptor recibe el digest del launcher Fleet y persiste cada
decisión en journal/CAS/ledger antes de ACK. Puede adjuntar el enlace del único
run Worker con admisión activa; ese enlace aún no autentica el turn/call de Codex
como el mismo run. Sus recibos tienen `authority=none`: no son registros B
completos ni atestaciones de Worker.
La evidencia del enlace nativo está en
[la etapa de política resuelta](/Users/hector/Projects/agent-fleet-orchestrator/outputs/codex-policy-bridge-vtuky9v3/README.md).
El enlace de identidad/aprobación se documenta en
[la etapa de contexto de operación](/Users/hector/Projects/agent-fleet-orchestrator/outputs/codex-operation-context-2hs0oxpd/README.md).

La implementación del canal se documenta en
[la etapa de canal privado](/Users/hector/Projects/agent-fleet-orchestrator/outputs/codex-private-channel-93tp_nmi/README.md).
Usa un socketpair anónimo heredado sólo al arranque; Codex fija CLOEXEC antes de
leer el bootstrap. La clave efímera nunca se guarda en argv, entorno ni journal.
Los mensajes canónicos están limitados a 1 MiB, autenticados con HMAC-SHA256 y
sujetos a un deadline absoluto de tres segundos dentro de Codex. El broker
permite hasta 30 segundos para que arranque la imagen y limita la transacción
CONTROL a 2,5 segundos; no amplía el plazo de autorización de herramientas.
Un error de protocolo consume el canal.
El ACK exige digest del request, canal, launch, secuencia y receipt durable.
El receptor Python hace writes exclusivos y fsync de archivo/directorio antes de
responder. Un journal consumido no puede abrirse como intento nuevo.

Esto es una primitiva de observación opt-in, no un modo de aislamiento requerido.
El adaptador Fleet permite exclusivamente el saludo inicial. Todas las
solicitudes nativas de herramientas se deniegan, incluso si aportan contexto.
El registro conserva política/contexto, programa/cwd, nombres
de variables y compromisos HMAC separados del entorno y de argv/entorno/FDs,
con todos los argumentos redactados. El límite nativo ahora materializa el entorno
explícito en el mismo `Command` inmediatamente antes de observarlo y lanzarlo:
recoge sus entradas finales, excluye las eliminadas y ejecuta `env_clear().envs(...)`.
No infiere el estado interno desde getters. El receptor rechaza una captura ausente
o divergente. El compromiso de inputs usa versión 2; la preparación explícita se
identifica por separado. Esto prueba el input preparado, no posibles mutaciones
en closures `pre_exec` ni el entorno que observe el sistema operativo después de
exec. Los callers de producción ya limpiaban la herencia; se conservan sus valores.
La clave compartida no proporciona una firma pública verificable tras descartar
los padres confiables.

Contrato requerido del adaptador:

- B: intento/run/operación/secuencia, política efectiva, Command final, perfil
  Seatbelt y sus parámetros, compromiso del payload y de los FDs.
- Canal privado autenticado, no heredable por herramientas; secretos/capacidades
  sólo en los padres confiables. UID y archivos 0600 no bastan contra otro proceso
  hostil del mismo usuario.
- ACK de CONTROL persistido antes del efecto, vinculado al B exacto, de un solo
  uso y con deadline. Recovery invalida canales/intentos viejos; un ACK ambiguo
  exige reconciliación. El marcador consumido del wrapper no sustituye este ACK.
- Evento posterior al spawn con PID/birth/imagen realmente observados; cerrar la
  carrera entre hash del archivo y ejecución. Observar closures pre_exec y FDs.
- Cubrir o rechazar apply_patch/FS payloads, write_stdin, transporte remoto de
  exec-server, escalation, MCP/dynamic tools y las rutas que no atraviesan el
  wrapper de spawn; transportar datos no equivale a imponer un modo requerido.
- C: observador externo de escrituras permitidas/denegadas, bind, stdout sin
  autoridad, integridad de CONTROL/CAS/ledger/runs/Git y quiescencia del árbol de
  procesos. Definir una ventana que distinga writes legítimos de CONTROL de los
  efectos del Worker, sin esconder estos últimos.
- D: anchor de la cadena autenticada al ledger y política de finalización
  versionada. Los registros históricos no adquieren autoridad nueva.

## Manifiesto v2: broker conectado al exec

`{"version":2,"native_observer":"worker-v1","codex":<pin>,"herdr":<pin>}`
activa el canal exclusivamente para Worker. Requiere las imágenes locales
instrumentadas. Es un carril de diagnóstico que deniega herramientas: no debe
usarse como una campaña funcional completada. El manifiesto v1 y los demás
roles mantienen su comportamiento anterior; no se selecciona v2 globalmente.

`fleet_herdr_launch.consume` crea el socketpair antes de registrar el argv
real, fija el digest CAS y consume el intento. `NativeLaunch.start` bifurca un
broker confiable y el wrapper original hace `os.execve`. El descriptor queda
heredable sólo para ese exec; Codex lo marca CLOEXEC antes de la negociación.
El broker elimina su entorno heredado y descriptores ajenos. Su clave nace en
memoria después del fork y nunca se persiste. Codex recibe el entorno generado
por Fleet, cuyo digest y nombres ya estaban fijados por el launcher.

`Publisher.transaction` valida intento/generación, candidate, imagen fijada,
política congelada, deadline y control deseado. `proc_pidpath` observa la ruta
del proceso Codex actual; no demuestra identidad de nacimiento ni bytes
mapeados. El lock del backend se mantiene en Herdr durante boot: el broker usa
una lectura atómica de ese archivo y no toma ese lock mientras Herdr espera
el saludo. El saludo no autoriza efectos. Fleet revalida sus pins, estado y
proceso antes de marcar readiness y antes de enviar un prompt.

`herdr_native_observed` es un evento pasivo, sólo de CONTROL. El orden es recibo
local → CAS → ledger → anchor local → ACK autenticado. Fallos de persistencia o
contención cierran el canal; no hay fallback ni replay tras recovery. La misma
transacción del ledger abarca decisión, persistencia y ACK. Una denegación
consume el canal y bloquea nuevos envíos desde ese intento. El broker conserva
un resultado de salida local y termina al cerrar Codex el socket o perder al
padre; aún no cubre todos los descendientes posibles de un Worker real.

La evidencia reproducida está en
[la etapa de launcher nativo](/Users/hector/Projects/agent-fleet-orchestrator/outputs/fleet-native-launch-pegsfrqh/README.md).
Incluye el binario real vía wrapper con `--version`, journal/CAS/ledger y un
observador externo de los dos PID. No envía a Herdr ni inicia un run/modelo;
por ello sólo verifica el bootstrap y conserva `INTEGRATION_BINDING=NOT_VERIFIED`.

## Conductor determinista y compatibilidad pendiente

Codex dispone de `/shell` sin modelo, pero
[user_shell.rs:210](/Users/hector/Projects/agent-fleet-orchestrator/outputs/integration-binding-rxmp8__u/codex/codex-rs/core/src/tasks/user_shell.rs:210) fija
`PermissionProfile::Disabled`, `SandboxType::None` y elimina el proxy gestionado.
Es una ruta de acceso completo explícita; no puede sustituir un canary del Worker.

El conductor alternativo debe servir Responses/SSE únicamente en loopback, sin
auth, retries ni conexiones externas. Una secuencia fija devuelve una tool call
de la herramienta realmente anunciada por la TUI, espera su resultado y termina.
Las fuentes incluyen
[MockResponsesConfig](/Users/hector/Projects/agent-fleet-orchestrator/outputs/integration-binding-rxmp8__u/codex/codex-rs/app-server/tests/common/config.rs:7)
con `wire_api=responses`,
reintentos cero, WebSockets deshabilitados y auth opcional; no prueban la
compatibilidad de Fleet/Herdr/TUI ni las solicitudes auxiliares del startup.
La fixture tiene por defecto `sandbox_mode=read-only` y `model=mock-model`;
copiar su configuración sin adaptar el protocolo de prueba alteraría la política
y el modelo. El conductor sólo debe usarla como referencia de Responses/SSE y
conservar la política solicitada por Fleet. El ensayo posterior se describe abajo.

Primera operación: el Worker real ejecuta un canary fijo mediante su herramienta
habitual, sin modificar el perfil de sandbox. CONTROL observa candidate/temporal,
symlink/absoluta/traversal, bind, stdout y procesos. Después se prueba la secuencia
de cinco etapas con sólo Worker escribiendo. Se requiere definir y verificar el
carril loopback antes de autorizar efectos; no se cambia el provider/routing de
las misiones actuales para simular éxito.

El ensayo local posterior sí activa un conductor acotado en una Mission aislada:
[observación de inputs efectivos](/Users/hector/Projects/agent-fleet-orchestrator/outputs/native-effective-inputs-m0_o9hdf/README.md).
Usa HerdrBackend, el wrapper, Herdr y Codex reales, sin mock del backend. Conserva
el modelo y los argumentos de permisos de Fleet; sólo el CODEX_HOME de la fixture
apunta a loopback, sin auth ni inferencia. El protocolo observado es Responses
Lite: herramientas dentro de `additional_tools`, llamada custom `functions.exec`
y resultado como lista de texto. Se rechaza una solicitud auxiliar con otra
`prompt_cache_key` sin consumir el intercambio principal. Esta clave permite
correlacionar el transporte local; no autentica el productor ni sustituye run_id.

La llamada real llega al límite de pipe con `/usr/bin/sandbox-exec`, política
resuelta Seatbelt, approval `never`, candidate, temporales, entorno materializado
y contexto de operación. CONTROL registra saludo permitido y herramienta
denegada en journal/CAS/ledger; el conductor recibe ese rechazo y emite el texto
fijo `status=accepted`. Ese texto no cambia el estado de la Mission, que permanece
`booting`. Candidate queda intacto y el observador externo no encuentra procesos
restantes entre los descendientes muestreados. No se enviaron las cinco etapas ni
se obtuvo un run_id: se ejercitó el launch normal seguido de un prompt directo
por Herdr, no `Fleet.submit`. La escritura canary y el binding completo siguen
`NOT_VERIFIED`.

La imagen CLI local necesita `codex-code-mode-host`. Compilarlo offline intentó
descargar un archivo de V8 y el guard de mantenimiento denegó esa red. Se usó una
copia privada, fijada por SHA-256, del helper ya instalado; no se instaló ni se
descargó nada. Su paquete declara la misma versión, pero el helper no ofrece
`--version`. La compatibilidad comprobada se limita a esta llamada rechazada.

## Verificación local y límites

```sh
python3 -B -m unittest tests.test_fleet_herdr_runtime tests.test_fleet_herdr_launch tests.test_fleet_herdr
git diff --check
```

Incluye layout real v2/legacy, recuperación sin resend, alias y reemplazo de
padres, cinco handoffs, archivo/CAS/aceptación reales con transporte sintético,
exec de una fixture sin modelo, replay, alteración de política/argv/entorno,
intentos/generaciones/misiones/roles distintos, binario/version divergentes y
stdout `status=accepted` sin autoridad. No es evidencia de contención del Worker.

En Herdr se verifican API, ruta absoluta y su fixture viva; las herramientas
existentes se ejecutan offline. El informe de esta etapa conserva comandos,
resultados, diff y una incidencia de la fixture upstream que resolvió el Pi
instalado antes de corregir su PATH/HOME. Esa incidencia impide afirmar ausencia
total de accesos a configuración/credenciales o efectos externos del turno.

La lectura externa y los procesos del mismo usuario siguen sin aislamiento
demostrado. HOME/CODEX_HOME propios reducen herencia, pero no prueban inaccesibilidad
de otros homes. `/tmp` sigue compartido y TMPDIR es otra raíz declarada. Ni wait,
ni exit 0 ni killpg prueban por sí solos la quiescencia de descendientes escapados.
