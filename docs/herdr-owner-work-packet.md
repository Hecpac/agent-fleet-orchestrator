# B: WorkPacket y protocolo del responsable

**Estado: implementado para previsualización local y fixtures sin proveedores.
El nuevo ciclo y su dispatch siguen deshabilitados.** B mejora la información
y la interacción; A conserva la aceptación de alcance físico. Ninguno completa
por sí solo el ciclo autónomo.

## Alcance cerrado

- Proyectar objetivo, contexto, requisitos del resultado, alcance de A,
  instrucciones aplicables, preferencias, decisiones iniciales y límites.
- Aceptar dos mensajes del responsable: `submit_candidate` y
  `request_decision`, sin identidades ni hashes calculados por el modelo.
- Verificar el mensaje contra el prompt exacto, la sesión, el turno completado
  y la configuración de permisos registrada; conservar el JSON original.
- Ofrecer la proyección por `mission-run.py dry --work-packet` sin crear
  Mission, candidate, admisión, sesión ni archivos de ejecución.

No se añaden turnos, reparación, enmiendas, selección de modelos, nuevos estados
del ledger, aceptación automática, aplicación al origen, MCPs ni otro controlador.
No se adapta una solicitud del Worker al antiguo DecisionBrief de Lead y
challenger. La persistencia de revisiones/intentos corresponde a C; el tratamiento
de decisiones, reparación y enmiendas corresponde a las unidades siguientes.

## Uso local

Se reutilizan los contratos de aceptación y alcance existentes. El contrato
funcional es opcional; si se proporciona, debe referirse al oráculo canónico y a
las fuentes actuales del runner y guest. No se consulta Docker para proyectarlo.

```sh
python3 -B scripts/mission-run.py dry ejemplo "Corregir el comportamiento acordado" \
  --workflow herdr-minimal-implementation \
  --target-repo /ruta/al/repositorio \
  --acceptance-contract /ruta/acceptance.json \
  --scope-contract /ruta/scope.json \
  --work-context /ruta/context.json \
  --work-packet --json
```

`owner_work_packet` es el documento visible. El resto de la salida de `dry` es
información del controlador, no parte del prompt. Las instrucciones se leen del
HEAD tracked indicado por la previsualización, incluso si el checkout está
sucio; no se presenta como snapshot de esos cambios locales. El workspace del
paquete significa el candidate aislado que un futuro controlador vinculará,
nunca permiso para trabajar en el checkout usado para previsualizar.

`--work-packet` y `--work-context` sólo existen en `dry`. Pasarlos a `run` es un
error de argumentos. El backend también rechaza explícitamente `owner-work-v1`
antes de acceder al runtime. El perfil `sol_minimal_v1`, los prompts actuales y
los archivos históricos mantienen su protocolo anterior.

Contexto inicial opcional; campos omitidos se proyectan vacíos, nunca inferidos:

```json
{
  "summary": "Corregir la función existente preservando sus llamadas públicas.",
  "entry_points": ["sample_stats.py"],
  "evidence": [{"name": "fallo inicial", "content": "La entrada vacía devuelve un valor."}],
  "additional_requirements": ["Explicar el comportamiento en answer.txt."],
  "procedure_preferences": ["Preferir comprobaciones focalizadas."],
  "constraints": ["Usar sólo dependencias existentes."],
  "decisions": [{"decision": "Conservar la firma pública", "reason": "Hay consumidores existentes."}]
}
```

Este contenido debe proceder del usuario/controlador autorizado. Es contexto
inicial, no un canal de enmiendas ni una ampliación de permisos. La evidencia
inline conserva su contenido; no implica que sus afirmaciones se hayan probado.
No se trunca silenciosamente: contexto hasta 64 KiB, paquete hasta 256 KiB.
Un exceso requiere reducir el contenido explícitamente antes de prepararlo.

## Proyección y vinculaciones

`fleet_herdr_work_packet.prepare` produce tres elementos:

| Elemento | Contenido y responsabilidad |
| --- | --- |
| `work_packet`, versión `owner-work-v1` | Objetivo, contexto y evidencia legible; requisitos de artefactos y funcionales; alcance editable y temporales; instrucciones por directorio y skills seleccionadas; decisiones, preferencias, autoridad y protocolo de respuesta. |
| `execution_envelope` | Digest del paquete, baseline, candidate, workflow, perfil, runtime y pins de las fuentes. Exclusivo del controlador. `dispatch_enabled=false`. |
| `sources` | Contratos e instrucciones originales, tests y proyección de la política de fuentes. Permiten comprobar la proyección offline sin releer archivos que podrían haber cambiado. |

La preparación no inventa Mission, run, admisión, sesión ni turno. Al observar
una interacción, el controlador suministra esas identidades, el digest esperado
del envelope y el digest de tarea de su admisión. El agente nunca los copia.
`verify` comprueba las fuentes retenidas y vuelve a producir el paquete, en lugar
de confiar sólo en que un JSON tenga forma válida.

El contrato funcional soportado sigue siendo `python-stats-rpc-v1`. El paquete
incluye los cinco tests originales completos, su mapeo a requisitos, el código
exacto del validador `check_bridge_source`, entorno, runner y límites. Las fuentes
del runner/guest y los bytes del oráculo deben coincidir con el contrato. Un
contrato desconocido o contenido incompleto falla; no se inventa una cobertura
para un hash desconocido. Sin contrato funcional se declara su ausencia.

Los requisitos adicionales se distinguen de las preferencias de procedimiento.
Los predicados de artefactos siguen visibles con sus valores exactos: un SHA
exigido como requisito de contenido puede aparecer ahí, aunque el agente no
tenga que calcularlo ni copiarlo en su respuesta. El test estadístico no verifica
la calidad del texto ni otros requisitos adicionales.

La proyección usa la selección actual de skills del Worker y sus contenidos;
omite la contabilidad y las instrucciones de resultados/etapas del protocolo
antiguo. La jerarquía de instrucciones del proyecto y la prohibición de ampliar
autoridad siguen aplicándose. Planificar o investigar no crea otra etapa.

Permisos configurados, disponibilidad y verificación se distinguen explícitamente:

- Se conserva `workspace-write`, network deshabilitado, `approval_policy=never`
  y la configuración existente de temporales. La autoridad de tarea se limita
  a los archivos editables y scratch declarado, aunque el sandbox permita más.
- Herramientas efectivamente expuestas y disponibilidad del runner son
  `not_observed` durante la preparación. No se provisionan herramientas.
- La atestación posterior verifica configuración registrada, no mediación
  universal de efectos. El inventario de A comprueba snapshots, no escrituras
  transitorias ni externas al candidate.
- El plazo proyectado respeta el menor de timeout y plazo del workflow. No hay
  presupuesto efectivo de tokens; `tokens_available=null`. No se añaden límites
  ficticios de coste, turnos o reparación.

## Mensajes y receipt

Ejemplo de candidato:

```json
{
  "type": "submit_candidate",
  "summary": "Corregidos los casos acordados; faltan las comprobaciones del controlador.",
  "paths": ["sample_stats.py", "answer.txt"],
  "checks": [{"name": "prueba local", "outcome": "passed", "detail": "Casos ejecutados y resultado observado."}]
}
```

Las rutas son sugerencias dentro del alcance editable, no un inventario ni prueba
de que existen. Se admiten eliminaciones y listas vacías. Los checks son
afirmaciones del agente. Fleet sigue calculando hashes, selección de archivos y
aceptación mediante A al congelar el candidate en el recorrido correspondiente;
B no fabrica un árbol congelado ni un resultado `PASS` antiguo.

Ejemplo de decisión sin artefacto:

```json
{
  "type": "request_decision",
  "question": "¿Debe cambiar el formato público de salida?",
  "why_needed": "Los requisitos recibidos no resuelven la compatibilidad requerida.",
  "options": [],
  "recommendation": null,
  "work_completed": "Conservada la investigación del baseline."
}
```

No se exige inventar alternativas o una recomendación. Si las hay, se admiten
hasta tres opciones con `label` y `consequence`; `recommendation` nombra una de
ellas. El JSON es estricto, con claves cerradas, textos acotados y sin duplicados.
No acepta identidades, hashes, artefactos ficticios, `PASS` ni estados terminales.
El máximo es 32 KiB, 100 rutas sugeridas y 50 comprobaciones declaradas.

`fleet_herdr_owner_protocol.bind_response` reutiliza `verify_transcript`, con el
prompt exacto, identidad y permisos esperados del controlador. Conserva los
bytes originales del mensaje, transcript y preparación como objetos preparados
para el CAS existente; no los escribe automáticamente ni modifica el ledger.
El receipt mantiene el mensaje separado de las vinculaciones internas:

- `disposition=candidate_proposed` o `decision_requested`;
- `authority=none`, `candidate_accepted=false`, `decision_applied=false`;
- identidades de ejecución y hashes calculados por Fleet;
- atestación de la configuración del turno y referencias al contenido exacto.

`verify_receipt` relee CAS y comprueba la interacción contra el envelope y la
ejecución esperados **externamente al receipt**. Cambiar identidades, contenido,
prompt, permisos o renderizado del cierre no permite reutilizarlo. Una entrada
adicional del usuario durante el turno sigue rechazándose: B no habilita enmiendas.

El límite de esta prueba es preciso: valida interacción y procedencia. El caller
debe obtener las identidades de la autoridad del controlador; la función no
admite un run, comprueba un deadline activo ni demuestra ownership del scheduler.
C tendrá que persistir y verificar esa relación en ledger/archivo antes de activar
dispatch. No se permite usar un receipt aislado como autorización ni reetiquetar
una respuesta como nueva ejecución. Las reanudaciones o prompts repetidos
necesitarán sus vínculos de intento; un transcript ambiguo se rechaza.

## Ciclo experimental offline `owner_cycle_v1`

El controlador de `fleet_herdr_owner_cycle.py` añade una implementación separada
y versionada para conformidad offline. **No está registrado como perfil live ni
habilita `mission-run.py run --work-packet`.** No reinterpreta `sol_minimal_v1`,
su ledger, sus archivos ni las preparaciones B existentes. Solo acepta un
`OfflineBackend`; no hay adaptador de lanzamiento real.

La creación fija contrato, único Worker, baseline físico, runtime solicitado,
número máximo de intentos y plazo original. El journal propio reside bajo
`missions/<cycle_id>/owner-cycle`; reutiliza el CAS y las comprobaciones de
alcance/artefactos existentes. Rechaza adjuntarse a un `mission.jsonl` existente.
Cada intento tiene admisión, generación, sesión, turno, prompt y revisión padre.
Un nonce calculado por el controlador distingue tareas iguales entre ciclos.

La intención se persiste antes del envío. Recuperar una intención consulta el
mismo intento; una caída entre intención y envío puede requerir reconciliación
o cancelación, nunca reenvío ciego. Cada evento se guarda primero en CAS, de modo
que `recover()` puede terminar su publicación interrumpida incluso si el temporal
está incompleto. Las revisiones conservan inventario, tar, patch y comprobaciones
inmutables. `verify()` solo lee y reproduce los vínculos y predicados desde CAS;
no necesita el candidate mutable. El seal se publica al terminar o por recovery
explícito, no durante la verificación.

Las respuestas originales se conservan antes de parsear. Una entrega inválida
consume un intento; una entrega contradictoria bloquea aceptación. Si no se
pueden retener los bytes, queda un veto explícito, no un resultado reparado o
truncado. Las decisiones se vinculan a la admisión y al mensaje, y no amplían
alcance ni aceptación. Repetir una decisión/control con contenido distinto bajo
el mismo ID se rechaza. No hay mecanismo de enmiendas en esta versión.

Pausa/cancelación usan el lock corto del journal, independiente del driver.
Confirmarlas exige observación del recurso/generación exactos y limpieza; un ACK
no basta. Los errores de observación no impiden solicitar la cancelación del
recurso admitido. La entrega tardía se reconcilia sin reenviar; después del cierre
solo puede conservarse como evidencia en cuarentena sin reabrir el terminal.

El adaptador Codex nuevo valida configuración registrada contra el contrato
explícito, incluyendo versión/modelo/esfuerzo. Los lectores históricos conservan
sus defaults. Exige un turno aislado, un único prompt, herramientas completadas
y un final inequívoco; rechaza tipos de herramienta desconocidos. Claude dispone
solo de inspección de eventos, sin admisión de Worker ni identidad de proveedor
inferida. OpenCode conserva su lector observacional existente. DSH y mini-SWE
no están admitidos. `CAPABILITIES` documenta estas fronteras.

El resultado `accepted_contract` significa que pasan el alcance físico y los
predicados declarados del artefacto y, si se configura, el contrato funcional.
**No equivale a éxito semántico general ni a Mission live aceptada.** El bridge
funcional reutiliza `contract_for` y `verify_receipt`, añade un namespace por
revisión (sin crear otra Mission) y reproduce los tests canónicos sobre las
respuestas RPC retenidas. Conserva el oráculo exacto del paquete; no ejecuta el
candidate durante la verificación. En este carril el runtime y sus observaciones
son simulados, sin llamar a Docker. Su envío, resultado, cancelación y limpieza
tienen vínculos independientes del Worker. Un backend sin esa capacidad o
requisitos adicionales sin evidencia independiente bloquean aceptación.
El consumo carece de baseline nativo admitido y permanece `null`; se rechazan
presupuestos de tokens/coste que el carril no puede aplicar. Permisos efectivos,
calidad de modelo, facturación y recuperación live siguen `NOT_VERIFIED`.

### Identidad observada: contrato v2 con transporte inyectado

`owner_cycle_observed_v2` conserva la lectura del contrato v1 y fija una superficie
Herdr distinta por intento. La admisión CONTROL y su target son inmutables;
`agent_session` y `turn_id` comienzan en `null`. Un receipt y el transcript completo
de una sesión Codex nueva vinculan después la identidad, el prompt, runtime y
permisos. Las identidades conocidas incompatibles se rechazan antes del envío.

El journal conserva el frontier anterior al dispatch, los snapshots originales y
la procedencia del primer binding. Una observación rechazada veta aceptación,
pero no impide demostrar posteriormente la limpieza del recurso original.
Reanudar vuelve a observar la superficie. La señal de cancelación tiene su propio
intento durable: un ACK perdido no ocasiona otra señal. Un turno manual posterior,
herramientas pendientes o un receipt `idle` aislado no acreditan quiescencia.

`InjectedHerdrBackend` exige operaciones condicionales `send_if_unchanged` y
`stop_if_unchanged` al transporte de fixture. Herdr no ofrece actualmente esa
precondición atómica para `send-keys ctrl+c`; tampoco hay mediación nativa completa.
Por ello la construcción sin runner inyectado rechaza antes de cualquier efecto.
Este perfil sigue fuera del router y del driver live. La sesión fresca evita
recortar retrospectivamente un transcript persistente; a cambio requiere una
superficie independiente por intento, cuya provisión live queda pendiente.

El baseline previo permite atribuir contadores nativos cuando existen. Contadores
ausentes o regresivos siguen desconocidos y `cost_usd` sigue `null`. Esta medición
no habilita presupuestos que el controlador no pueda aplicar ni demuestra calidad.

Comprobación reproducible sin proveedores, Docker ni instalaciones:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest \
  tests.test_fleet_herdr_owner_cycle tests.test_fleet_herdr_owner_functional
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest tests.test_fleet_herdr_owner_transport
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/fleet_herdr_owner_conformance.py \
  --output /ruta/absoluta/nueva
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/fleet_herdr_owner_conformance.py \
  --verify /ruta/absoluta/nueva/summary.json
```

El comando de conformidad crea exclusivamente repositorios sintéticos nuevos
(incluidos commits de fixture), conserva once casos con journal/CAS y rechaza
sobrescribir un destino existente. Transcripts y reloj son sintéticos; no mide
modelos ni latencia real. La suite añade interrupciones reales de procesos en
publicación parcial/fsync/rename, además de casos de mensajes tardíos, resultados
contradictorios, revisiones cruzadas, decisiones repetidas y cancelación.

## Verificación y continuación

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest tests.test_fleet_herdr_work_packet
```

Los fixtures ejercen la CLI real de previsualización, el extractor de transcript,
el verificador de evidencia y el CAS existente, con contenido sintético y sin
proveedores. Cubren requisitos/casos completos, instrucciones y contexto,
candidate y pregunta sin hashes, alteración de evidencia, rechazo del dispatch,
ausencia de transiciones de Mission y compatibilidad con el mínimo anterior.
La suite de A mantiene las comprobaciones de ignorados, temporales, baseline
alterado y captura incompleta.

La comprensión efectiva por un modelo, calidad, latencia, tokens y coste facturado
siguen `NOT_VERIFIED`. Estas comprobaciones no lanzan Missions reales ni proveedores.
La parte offline de C dispone del ciclo experimental descrito arriba, con revisiones
e intentos persistentes. Sigue pendiente su integración live: lanzamiento, permisos
efectivos, recuperación y limpieza observados en cada runtime antes de promoverlo.
Reparación y enmiendas siguen separadas; activar sólo un flag de dispatch no
completaría esa integración.
