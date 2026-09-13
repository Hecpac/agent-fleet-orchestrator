# Denegación de ejecución nativa antes de efectos

Se ha integrado un [ejecutor confinado por etapa](herdr-confined-mediation.md)
con Seatbelt, broker y evidencia de Mission. No concede grants a este transporte
nativo ni completa su atestación por operación; el bloqueo descrito aquí sigue
vigente. La vía personal no se selecciona como parte de ese desarrollo.

Estado: **admisión fail-closed verificada localmente**. No hay todavía un
adaptador que imponga mediación completa mientras el agente ejecuta código.
Por tanto, el transporte real de Herdr rechaza nuevos arranques y prompts,
cuando se selecciona el backend nativo sin perfil personal, incluidos los
manifiestos v1/v2. Las nuevas Missions personales usan una admisión distinta,
[documentada aquí](personal-autonomy.md), sin atribuirse mediación completa. El wrapper
también rechaza su invocación directa. No existe una opción de entorno, flag,
prompt o recibo que habilite ejecución.

## Frontera real y cambio mínimo

El router, la política solicitada y el transcript describen permisos; no
interceptan los efectos. En el carril oficial, `HerdrBackend._default_run`
entrega capacidad al enviar `agent start`/`agent prompt` al runtime. Crear un
workspace o dividir un pane también puede iniciar un shell. En el carril del
wrapper, `fleet_herdr_launch.main` transfiere capacidad en `os.execve`.

La copia experimental de Codex bajo `outputs/integration-binding-rxmp8__u`
contiene `codex-rs/utils/pty/src/native_spawn_gate.rs`: `authorize` y
`spawn_with_observer` consultan al observador sólo cuando está instalado y
después llegan a `Command::spawn`. Esa copia ignorada por Git no se modifica.
Su canal no cubre todos los caminos de efectos: una denegación de spawn no
demuestra mediación de filesystem directo, MCP, transporte remoto o credenciales.

`fleet_herdr_effects.py` niega la entrega de capacidad sin un mediador completo.
No interpreta el código generado ni finge ofrecer aislamiento con una lista de
palabras prohibidas. Actualmente no concede ninguna ejecución del agente:

| Dominio | Denegación comprobada |
| --- | --- |
| Filesystem | El ejecutable que escribiría un marcador no se ejecuta. |
| Red | El ejecutable que conectaría a loopback no se ejecuta; no llega conexión. |
| Procesos | No se ejecuta el agente ni su proceso hijo; no aparece el marcador del hijo. |
| Credenciales | No se ejecuta el lector de una credencial sintética ni aparece su copia. |

El transporte real usa una allowlist de formas exactas para observación,
`ctrl+c` y cierre de recursos ya propios. Conserva las comprobaciones existentes
de ownership, identidad y quiescencia. Las consultas exactas `--version` de las
CLI confiables siguen siendo diagnósticos de CONTROL, sin entrada del agente;
sus binarios, el proceso Python, el servidor Herdr y su resolución por PATH/pins
pertenecen a la base de confianza. Esta allowlist no los aísla.

Boot, retry y un prompt nuevo se rechazan antes de persistir un intento de envío
cuando usan el transporte real. Un run histórico puede leerse o cancelarse sin
darle permiso para otro prompt. El guard del subprocess protege además llamadas
directas y operaciones futuras desconocidas. Los transportes Python inyectados
son código confiable de embedding/pruebas; no son extensiones para el agente.

El wrapper primero valida y consume el intent existente, luego deniega antes
del fork del broker y del exec. El intento queda consumido sin ejecución, no
reutilizable. Su observación CAS continúa con `authority=none`; `consumed.json`
no demuestra un proceso lanzado ni ejecución aceptada.

## Evidencia reproducible

```sh
python3 -B -m unittest tests.test_fleet_herdr_effects tests.test_fleet_herdr_launch tests.test_fleet_herdr_native tests.test_fleet_native_spawn_channel tests.test_fleet_herdr tests.test_fleet_herdr_permissions
git diff --check
```

`WrapperCanaryTests` ejecuta primero cada canario local como control positivo:
escritura temporal, conexión loopback, hijo local o lectura de un secreto
sintético. Después invoca el wrapper real con el mismo ejecutable fijado para
ambos manifiestos y comprueba el rechazo y la ausencia del efecto. Los recursos
son temporales y se cierran al terminar. No se lanzan Herdr, Codex ni proveedores.

Las pruebas del transporte verifican rechazo antes de `subprocess.run`, ausencia
de intent de envío ambiguo, rechazo de argumentos adicionales, grants inventados
y operaciones desconocidas. Recovery y cancelación atraviesan la admisión real
con recibos CLI sintéticos. Las pruebas anteriores del canal siguen cubriendo
autenticación, durabilidad antes de ACK y rechazo por bindings incorrectos.

Resultado local: 79 pruebas pasaron, incluidas cinco de la suite existente
`test_fleet_herdr_readers`. Su setup creó historia Git en fixtures temporales,
eliminadas al terminar; no modificó la historia del repositorio de trabajo.
Eso excedió la restricción de no crear commits también en fixtures. El comando
recomendado arriba excluye esa suite y contiene las otras 74 pruebas, que ya
pasaron en la misma ejecución. Los tests nuevos no crean commits.

## Límites y compatibilidad

**NOT_VERIFIED:** contención de código nativo ya ejecutándose; mediación por
operación con grants permitidos; aislamiento OS de filesystem/red/procesos/
credenciales; nacimiento e imagen mapeada; procesos externos del mismo usuario;
quiescencia de árboles arbitrarios; herramientas y proveedores reales.

Este cambio sacrifica la ejecución nativa funcional hasta disponer del adaptador.
La denegación previa al arranque no prueba ninguna de esas propiedades de runtime.
No detiene ni modifica agentes previamente arrancados. No cambia los lectores
históricos, los contratos de aceptación ni los workflows CMUX explícitos; estos
últimos quedan fuera de esta protección Herdr y no reciben garantías nuevas.

Para habilitar un grant será necesario implementar y verificar un adaptador que
controle todos los efectos, falle cerrado sin cobertura y vincule la autorización
al intento/operación efectivos. El saludo del observador y los flags de sandbox
existentes no satisfacen ese requisito.
