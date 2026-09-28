# Constitución de Agent Fleet

> **Estado:** ratificada por el operador · **Versión:** 1.0 ·
> **Fecha de ratificación:** 2026-09-28 · **Base examinada:** `main` en `9762f92`.

Este documento fija los compromisos estables del proyecto. Las especificaciones,
diseños y tareas SDD se derivan de él. Son normativas las secciones 1–3, 6 y 8.
Las secciones 4 (decisiones y método experimental), 5 (etapas) y 7 (decisiones
pendientes) forman un plan revisable: cambian mediante un cambio revisado, sin
enmendar los principios, siempre que respeten C1–C9. El Anexo A describe el
estado del código en la revisión base, sin valor normativo. El documento no
concede permisos ni describe capacidades implementadas.

Los principios usan identificadores estables `C1`–`C9`. Las especificaciones
pueden citarlos en su prosa; el formato `fleet.sdd.plan.v1` todavía no los valida.

## 1. Misión

Agent Fleet es un orquestador que coordina modelos frontier y open-weight para
completar tareas de principio a fin, con autonomía, continuidad y resultados
verificables. Debe poder cambiar de modelos y de forma de coordinación según la
tarea sin reconstruir sus garantías ni imponer funciones fijas a una familia de
modelos.

El recorrido fundamental es:

```text
Definir el resultado → trabajar → verificar → reparar si es necesario → entregar → comprobar la entrega
```

Completar una tarea significa satisfacer sus requisitos obligatorios y dejar el
resultado en el destino acordado, comprobado allí.

## 2. Reparto de decisiones

| Decisión | Responsable |
| --- | --- |
| Objetivos, compromisos materiales, autorizaciones que falten y ratificación de esta constitución | Usuario (operador) |
| Método: investigación, plan, orden de trabajo, elección entre herramientas disponibles y reparación reversible | Responsable de la tarea |
| Proponer una delegación, un cambio de modelo o una alternativa técnica | Responsable de la tarea |
| Admitir esas propuestas según permisos, datos, recursos y capacidades | CONTROL |
| Identidad de ejecución, propiedad de escritura, persistencia, límites exigibles, cancelación y conciliación de efectos | CONTROL |
| Obtener evidencia mediante pruebas, consultas o evaluadores | Verificadores admitidos |
| Determinar si las condiciones obligatorias permiten cerrar | CONTROL, según la política de aceptación |

CONTROL es el controlador durable común: conserva objetivo, autorizaciones,
recursos, límites, estado y condiciones de aceptación. El responsable de la
tarea es el agente que responde de completarla; puede planificar y ejecutar
dentro de una misma responsabilidad sin un Lead separado. Su responsabilidad es
sobre el método: la escritura de cada recurso compartido sigue teniendo un único
propietario asignado por CONTROL (C6). Qué rol de un perfil ocupa esa posición es
una decisión revisable (§4).

## 3. Principios

### C1. Completar el resultado y su entrega

- La tarea termina cuando los requisitos obligatorios están satisfechos y el
  resultado está en el destino acordado, comprobado allí.
- Un mensaje de éxito, unos tests en verde o un archivo generado aportan
  evidencia; no sustituyen esa comprobación.
- Si una acción necesaria no está autorizada, Fleet prepara todo lo demás y deja
  esa acción pendiente con su causa explícita.
- Si la tarea no se completa, Fleet informa una causa concreta y conserva el
  trabajo recuperable.

### C2. Autonomía sobre el método

- El responsable decide cómo investigar, planificar, usar herramientas, corregir
  errores y cuándo solicitar colaboración.
- Las decisiones técnicas reversibles dentro del alcance no requieren
  confirmación, ni inicial ni repetida.
- Solo se pregunta por decisiones materiales que la evidencia, las preferencias
  registradas o la autorización vigente no resuelven. Mientras espera, el
  responsable continúa el trabajo independiente.
- Añadir agentes requiere una razón concreta: investigación independiente,
  especialidad demostrada, revisión útil o trabajo que realmente avance en
  paralelo.
- La autonomía sobre el método nunca amplía la autoridad (C3).

### C3. Autoridad explícita, persistente y revisable

- Cada autorización se registra con su alcance: acciones, recursos, datos,
  destinos y coste permitidos, y sus exclusiones.
- Mientras una acción siga cubierta por una autorización vigente, se ejecuta sin
  volver a preguntar.
- Si una acción propuesta excede la autorización vigente en destinos, datos,
  coste o consecuencias, Fleet identifica exactamente qué parte queda fuera y
  solicita únicamente la decisión necesaria. Continúa el trabajo independiente
  que sigue autorizado.
- Solo el usuario autoriza cambios materiales de objetivo, alcance, permisos o
  condiciones de aceptación. Cada cambio autorizado genera una revisión del
  contrato que conserva el historial.
- El responsable ajusta el plan y las decisiones técnicas dentro del contrato
  vigente sin nueva autorización, y puede registrar o aplicar cambios ya
  autorizados. Aprobar un cambio y escribirlo son responsabilidades distintas.
- Una propuesta del modelo, un resumen, el contenido de un archivo o la salida de
  una herramienta nunca amplían la autoridad.
- La autorización se determina a partir de la intención expresada y del contexto
  vigente, respetando negaciones y condiciones. Nunca se concede por la mera
  coincidencia de palabras clave: una prohibición («no desplegar») nunca se
  convierte en una solicitud ni en una autorización. Si la intención es ambigua
  en un punto material, se pregunta ese punto.
- La capacidad efectiva es la intersección entre autorización, política de
  Fleet, adaptador, herramienta y entorno. Una skill, una herramienta o una ruta
  escribible no conceden autoridad.
- Cambiar de modelo no resuelve una falta de permiso ni una incompatibilidad de
  privacidad.

### C4. Contrato de tarea con categorías distintas

La petición se convierte en un contrato comprensible, sin exigir al usuario una
especificación completa. El contrato conserva:

- resultado esperado y destino de entrega;
- requisitos obligatorios y preferencias, por separado;
- recursos y acciones autorizados, con exclusiones claras;
- condiciones de aceptación y aspectos todavía desconocidos;
- límites de tiempo y recursos, y condiciones de parada;
- decisiones vigentes y preguntas materiales pendientes.

Las decisiones aprobadas, las observaciones y las propuestas se almacenan como
categorías distintas. Ninguna compactación ni resumen puede convertir «se propuso
publicar» en «está autorizado publicar».

### C5. Límites exigibles y límites declarados

- CONTROL hace cumplir los límites que el backend permite imponer. Las
  limitaciones que no puede imponer se declaran antes de empezar y nunca se
  presentan como garantizadas.
- Declarar una limitación no autoriza incumplir el contrato. Si una condición
  obligatoria no puede hacerse cumplir, Fleet busca un ejecutor admitido que sí
  pueda. Si no existe, detiene únicamente la acción afectada y explica la
  incompatibilidad. Tratar esa condición como no obligatoria es un cambio del
  contrato que decide el usuario (C3). Por ejemplo, que un backend no garantice
  un presupuesto máximo no permite superar el máximo autorizado.
- Un límite económico estricto solo se ofrece cuando puede imponerse antes de
  cada llamada.
- Los recursos incluyen concurrencia, memoria de modelos locales, procesos,
  espacio en disco y recursos externos.
- Un fallback no renueva el presupuesto. El agotamiento conserva el resultado
  parcial y su causa.
- Los límites de reparación e intentos pertenecen al contrato de cada tarea; no
  hay reparación infinita. Cuando varias iteraciones repiten el mismo fallo, el
  responsable diagnostica, cambia de enfoque o explica el agotamiento.

### C6. Estado durable, continuidad y recuperación

- El estado durable (ledger y CAS) es la autoridad; la conversación es una vista
  del trabajo que puede compactarse. La memoria reutilizable lleva procedencia,
  alcance y fecha, y no concede permisos ni reemplaza evidencia actual.
- Un relevo entrega objetivo, contrato vigente, decisiones, resultados
  comprobados, referencias, trabajo pendiente y operaciones todavía inciertas. El
  ACK del receptor comprueba el paquete, no su comprensión completa.
- Antes de repetir una operación que pudo tener efecto, se reconcilia el intento
  original. Si no puede establecerse, el resultado es indeterminado: no se asume
  fracaso ni se duplica el efecto.
- Reanudar reutiliza evidencia, revisiones, decisiones, permisos, plazos y
  presupuesto originales.
- Cada unidad de trabajo tiene un único controlador capaz de hacerla avanzar.
  Cada recurso compartido tiene un único propietario de escritura; los
  especialistas contribuyen en espacios aislados y la integración comprueba que
  la base no cambió.
- Cuando el contrato exige esa separación, las credenciales y la escritura del
  ledger quedan fuera del entorno donde se ejecuta código generado.

CONTROL distingue estos estados:

| Estado | Significado |
| --- | --- |
| Progreso | Nueva evidencia útil, requisitos satisfechos o incertidumbres resueltas |
| Bloqueo | Falta concreta de capacidad, decisión, permiso o dependencia |
| Agotamiento | Se alcanzó un límite acordado sin completar |
| Indeterminado | No puede establecerse si un efecto ocurrió |
| Éxito | Requisitos obligatorios y entrega comprobados, sin pendientes incompatibles con el cierre |
| Cancelación confirmada | Trabajo detenido y recursos temporales reconciliados; una solicitud de cancelación no basta |

### C7. Aceptación basada en evidencia independiente

| Nivel | Pregunta | Evidencia apropiada |
| --- | --- | --- |
| Integridad | ¿Es exactamente el resultado que se examinó? | Hashes, versiones, snapshots y procedencia |
| Alcance | ¿Se respetaron recursos, permisos y límites? | Controles de ejecución y observación de efectos |
| Funcionamiento | ¿Hace lo requerido en los casos definidos? | Ejecución funcional, consultas y pruebas independientes |
| Objetivo | ¿Resuelve la petición completa? | Cobertura de requisitos y comprobación del recorrido real |
| Entrega | ¿Está disponible en el destino solicitado? | Lectura posterior del destino, identidad y accesibilidad del resultado |

- El ejecutor puede comprobar y corregir su trabajo: ejecutar tests, detectar
  errores y repararlos antes de presentar un candidato. Sus comprobaciones
  aportan evidencia, pero no sustituyen la aceptación independiente ni le
  conceden autoridad para cerrar la tarea.
- El cierre lo decide CONTROL según la política de aceptación. La declaración del
  ejecutor nunca cierra una tarea.
- La independencia se exige respecto de la autoridad y la evidencia del
  ejecutor. No requiere siempre otro LLM; dos modelos distintos también pueden
  compartir errores. Un perfil o el [modelo de amenaza](fleet-threat-model.md)
  pueden exigir además diversidad de identidad, que hace auditable la
  corroboración sin demostrar errores independientes.
- Un incumplimiento de alcance observado impide declarar éxito, aunque los
  artefactos hayan sido aceptados. Stage PASS, transporte completado, aceptación
  de artefactos y éxito de la tarea son hechos distintos.
- Los jueces LLM sirven para aspectos abiertos, con rúbricas calibradas contra
  revisión humana. No anulan un fallo determinista, no inventan evidencia, no
  aceptan su propia declaración de éxito y pueden responder «no determinable».
- La evidencia es proporcional al resultado y a sus consecuencias. Los informes
  distinguen hechos verificados, inferencias y `NOT_VERIFIED`.
- Los evaluadores también se prueban: con una solución válida conocida, con un
  resultado incorrecto que parezca convincente y con alternativas válidas que no
  sigan el recorrido previsto.

### C8. Independencia entre modelo, organización, herramientas y control

- Las garantías de C3–C7 no dependen de un modelo, familia, proveedor, CLI ni
  forma de organización concretos.
- No hay jerarquía fija entre frontier y open-weight. Open-weight no implica
  ejecución local ni coste cero. Cualquier modelo puede ser responsable,
  planificador o especialista según su capacidad demostrada.
- La selección se hace en dos pasos. Primero se aplican filtros obligatorios:
  política de datos y privacidad, permisos, modalidades, contexto utilizable,
  herramientas, disponibilidad y garantías requeridas. Después, entre los
  candidatos elegibles, se decide por éxito observado, latencia, coste total e
  intervención humana.
- La unidad evaluada es la combinación completa:
  `modelo/revisión + cuantización + servidor + plantilla/parser + adaptador + herramientas + entorno`.
- Para cada ejecutor se distinguen capacidades declaradas, capacidades
  comprobadas por pruebas del adaptador, permisos efectivos observados,
  garantías que el entorno realmente impone y aspectos desconocidos.
- La identidad de modelo y esfuerzo se registra mediante evidencia del runtime
  vinculada a la ejecución, indicando su procedencia y grado de comprobación. Se
  distinguen configuración solicitada, identidad reportada e identidad
  verificable; lo desconocido permanece explícito. Ninguna fuente, sea un
  transcript o una respuesta de API, prueba por sí sola qué modelo ejecutó el
  proveedor.

La sustitución de un ejecutor depende de la causa:

| Situación | Comportamiento |
| --- | --- |
| Indisponibilidad antes de efectos | Reintento acotado o alternativa ya admitida |
| Protocolo o llamadas mal formadas | Diagnóstico del adaptador, reparación limitada o sustitución compatible |
| Resultado incorrecto | Feedback del verificador, reparación y posible escalado por capacidad demostrada |
| Falta de permiso o incompatibilidad de privacidad | Bloquear esa acción (C3) |
| Timeout después de un posible efecto | Reconciliar el intento original antes de repetirlo (C6) |
| Agotamiento de recursos | Conservar resultado parcial y causa (C5) |

### C9. Preservación del trabajo y del significado histórico

- Fleet preserva el trabajo existente del usuario, incluidos cambios sin commit
  y archivos sin seguimiento, y no sobrescribe ni revierte cambios ajenos.
- Los registros terminales son de solo lectura. Los lectores históricos
  conservan su significado; ninguna migración ni refactor reinterpreta lo que un
  registro afirmaba.
- Las duplicaciones se retiran solo tras demostrar equivalencia y ausencia de
  consumidores.
- Se prioriza reutilizar los mecanismos existentes: ledger, CAS, identidades,
  admisiones, freeze, verificación independiente y recuperación conservadora.
  Una sustitución requiere justificar su beneficio y demostrar que conserva las
  garantías de C3–C7, el trabajo existente y la interpretación del historial.
  Qué componente implementa cada garantía es una decisión de diseño revisable
  (§4).

## 4. Decisiones revisables

Esta sección, incluido el método de evaluación, no forma parte de los
principios. Sus decisiones cambian con evidencia registrada en el repositorio,
sin enmienda, siempre que respeten C1–C9:

- componentes que implementan cada garantía, según la regla de sustitución de C9;
- selección y asignación de modelos, y reglas del router;
- transporte, CLI, runtime y versiones, que pertenecen al contrato de runtime;
- organización: agente único, equipo fijo, delegación opcional o coordinación
  dinámica; qué rol de perfil ocupa la responsabilidad y cuándo delegar;
- uso de Fusion y modelos que participan;
- frecuencia y forma de la revisión LLM;
- política de compactación y relevo, sin porcentajes universales de contexto;
- límites concretos de reparación e intentos por familia de tareas.

Método de evaluación:

- Se elige la organización más sencilla salvo que otra aporte una mejora
  reproducible y relevante.
- La base de comparación es un agente competente con herramientas, verificación
  y reparación, no una respuesta de un solo turno.
- Las alternativas comparten entradas, entorno, permisos, herramientas,
  criterios de entrega y evaluadores. Primero se mantiene el modelo constante
  para medir la organización; después se evalúan asignaciones heterogéneas.
  Selección de modelos, delegación y cómputo total se separan mediante
  comparaciones adicionales.
- Se mide: tareas completadas y requisitos omitidos; calidad; falsos éxitos y
  rechazos de soluciones válidas; violaciones de alcance; intervención humana
  necesaria y redundante; recuperación (trabajo conservado, tiempo y efectos
  duplicados); latencia y variabilidad; coste por resultado aceptado, separando
  facturado, estimado y desconocido; y operación (recursos abandonados y
  bloqueos explicables).
- Tokens de tokenizadores distintos no son una unidad económica equivalente. La
  calidad real de los modelos y el coste facturado permanecen `NOT_VERIFIED` sin
  la evidencia correspondiente.
- Las pruebas avanzan en tres fases: offline, con transportes simulados y
  fixtures que incluyen una prohibición que no debe convertirse en solicitud;
  con proveedores, bajo autorización propia y versiones fijadas; y tareas reales
  elegidas por el usuario.
- Los resultados publicados o comunicados por fabricantes orientan los
  experimentos, pero no son premisas ni umbrales del router.

## 5. Etapas de desarrollo (plan revisable)

Las etapas ordenan el trabajo. Pueden reordenarse, dividirse o redefinirse
mediante un cambio revisado, sin enmienda, siempre que respeten C1–C9.

### Etapa 1 — Cerrar el ciclo de una Mission

Objetivo: el responsable recibe un fallo verificable, produce una nueva revisión
dentro del mismo contrato y la Mission termina solo cuando el resultado acordado
está comprobado y entregado.

Alcance:

- Ejecutar, verificar, reparar, recuperarse y entregar dentro de una misma
  Mission y bajo su autoridad. La reparación de Owner Cycle se integra bajo esa
  autoridad; no queda un segundo controlador capaz de avanzar el mismo trabajo.
- Una familia de tareas local, un ejecutor admitido y un verificador
  independiente.
- Entrega en un destino local propiedad del operador y comprobación posterior en
  ese destino.
- Demostración inicial con transporte simulado. La validación con modelos
  requiere su propia autorización.

Criterios de aceptación:

- Un resultado que declara éxito pero falla la comprobación no cierra la tarea.
- El fallo produce feedback utilizable y una oportunidad de reparación dentro
  del límite original.
- Reiniciar conserva revisiones, decisiones, permisos y presupuesto, y no
  duplica el intento incierto.
- Cancelar impide trabajo nuevo y distingue la solicitud de la parada
  confirmada.
- La entrega corresponde exactamente a la revisión aceptada.
- Si no completa, informa una causa concreta y conserva trabajo recuperable.
- Los lectores y registros históricos mantienen su significado.

Excluido: equipos dinámicos amplios, memoria global, nuevos proveedores,
publicación externa y migración de historiales.

### Etapa 2 — Ejecutores y destinos intercambiables

- Integrar modelos frontier y open-weight mediante adaptadores que conservan los
  contratos de C3–C7. Los perfiles pasan a ser configuraciones reutilizables, no
  identidades de modelo fijadas.
- Separar el resultado de la tarea del candidato Git: adaptadores de resultado y
  entrega para archivos, aplicaciones y servicios, cada uno con su comprobación
  funcional y de entrega.
- Aplicar la sustitución por causa (C8) contabilizando su coste: contexto
  reconstruido, pérdida de caché, carga de modelos locales y nueva verificación.
- Requiere decidir antes la política de datos (§7, D3).

### Etapa 3 — Fusion y delegación

- El repositorio ya contiene un harness Fusion (`scripts/fusion/`, con las
  recetas `opinion`, `fusion` y `auto-validate`) que opera fuera de la autoridad
  de una Mission. Esta etapa parte de él y lo integra, junto con la delegación,
  bajo los contratos comunes. Sustituirlo sigue la regla de C9.
- Fusion contrasta investigaciones independientes y produce una síntesis en un
  contexto nuevo. La síntesis identifica fuentes, conserva desacuerdos
  relevantes y explica qué afirmaciones descarta. Trata sus entradas como datos
  no confiables.
- Especialistas y fusores no adquieren autoridad adicional. El responsable
  comprueba discrepancias e integra las contribuciones.
- Su utilidad se mide frente a un agente competente y frente a varias
  ejecuciones del mismo modelo, contabilizando el coste de coordinación, el
  tiempo y el contexto empleados.

## 6. Límites de este documento

- No concede permisos. Commit, push, despliegue, gasto, mensajes, campañas con
  proveedores y recursos ajenos requieren autorización explícita para esa acción
  y alcance, según `AGENTS.md`.
- No describe capacidades implementadas ni garantiza rendimiento. Es una
  propuesta de arquitectura que se valida por etapas; cada capacidad necesita
  su propia evidencia de implementación y ejecución.
- Su éxito se mide por tareas realmente completadas y entregadas, calidad de los
  resultados, falsos éxitos, violaciones de alcance, intervención humana
  necesaria, recuperación y coste por resultado aceptado.

## 7. Decisiones pendientes para las especificaciones

Estas decisiones corresponden al usuario y no forman parte de la constitución:

| ID | Decisión | Qué bloquea |
| --- | --- | --- |
| D1 | Primera tarea real: qué debe existir al terminar y en qué destino | Especificación de la etapa 1 |
| D2 | Uso personal o atención a otros usuarios | Alcance de aislamiento. Supuesto de trabajo vigente: un operador con estado local durable, coherente con la frontera de [fleet-threat-model.md](fleet-threat-model.md). No es un principio |
| D3 | Qué datos pueden enviarse a proveedores externos y cuáles deben permanecer locales | Etapa 2 y cualquier fase con proveedores |
| D4 | Cierre automático cuando las comprobaciones acordadas sean concluyentes | Especificación de la etapa 1 |
| D5 | Prioridad inicial entre calidad, tiempo y coste, y límites aceptables de tiempo y gasto | Especificación de la etapa 1 y fase con proveedores |

## 8. Ratificación y enmiendas

- El operador ratificó la versión 1.0 el 2026-09-28. Solo el operador aprueba
  las enmiendas.
- Las enmiendas se hacen mediante un cambio revisado de este archivo, con
  historial en Git y versión incrementada. Modelos, Missions y revisiones pueden
  proponer enmiendas y, una vez aprobadas por el operador, redactarlas o
  aplicarlas. Aprobar y escribir el cambio son responsabilidades distintas.
- Los cambios de §4, §5 y §7 son cambios revisados del plan y no requieren
  enmienda. Si un resultado experimental contradice un principio, se propone una
  enmienda en lugar de ignorar el principio.
- La constitución prevalece sobre los documentos de
  arquitectura y diseño que la contradigan hasta que estos se reconcilien. No
  prevalece sobre `AGENTS.md`, los contratos de rol ni el modelo de amenaza, que
  siguen rigiendo permisos y límites de seguridad; un conflicto con ellos se
  resuelve mediante enmienda o cambio revisado de esos documentos.

## Anexo A — Brechas frente al código (no normativo)

Observaciones sobre `9762f92` (2026-09-28). Motivan las etapas; se actualizan en
sus especificaciones y no modifican los principios.

| Principio | Estado observado | Referencia |
| --- | --- | --- |
| C3 | Las decisiones de continuación solo se admiten `within_existing_contract`; no existe vía para ampliar la autoridad | [fleet_herdr_owner_contract.py:150](../scripts/fleet_herdr_owner_contract.py) |
| C3 | El clasificador de riesgo busca patrones sin tratar negaciones: `despleg(?:ar\|ado)` coincide con «no desplegar» | [fleet-risk.py:68](../scripts/fleet-risk.py) |
| C5 | Herdr rechaza `token_budget > 0` porque no puede imponerlo | [mission-run.py:186](../scripts/mission-run.py) |
| C6 | Owner Cycle usa un espacio separado y rechaza adjuntarse a una Mission existente; la reparación opt-in (S8) está pendiente | [fleet_herdr_owner_cycle.py:103](../scripts/fleet_herdr_owner_cycle.py), [architecture-refactor-2026-09-23.md](architecture-refactor-2026-09-23.md) |
| C7 | Una Mission real terminó `succeeded` con artefactos `accepted` y alcance físico FAIL por un archivo ignored | `outputs/livebgijyamp/RESULTADO.md` (local, ignorado por Git) |
| C7 | Los predicados de artefactos se limitan a `text_contains`, `sha256` y `json_equals`; el contrato funcional está fijado a `python-stats-rpc-v1` | [fleet_acceptance.py:71](../scripts/fleet_acceptance.py), [fleet_functional.py:34](../scripts/fleet_functional.py) |
| C8 | Los perfiles Herdr exigen modelos concretos, `provider=openai`, runner interactivo y hooks de Codex | [fleet_herdr_profile.py:162](../scripts/fleet_herdr_profile.py) |
| C8 | El contrato personal exige Codex 0.154.0; el paquete instalado examinado es 0.156.0 | [fleet_herdr_versions.py:25](../scripts/fleet_herdr_versions.py) |
| Etapa 2 | El driver prepara un clon Git y congela su árbol como resultado | [fleet_herdr_mission.py:493](../scripts/fleet_herdr_mission.py) |
| Etapa 3 | Fusion ejecuta `claude`/`codex` sin interfaz y mantiene un ledger propio en `outputs/fusion/` | [fusion_harness.py](../scripts/fusion/fusion_harness.py) |

Documentos pendientes de reconciliar:

- [operating-model.md](operating-model.md) fija en su principio una jerarquía
  frontier/local que C8 convierte en decisión revisable.
- [herdr-sdd-roadmap.md](herdr-sdd-roadmap.md) mantiene Research como pendiente
  aunque el perfil existe. Su regla «solo Worker puede ser owner de
  implementación» es compatible con C6 como asignación de perfil revisable.
- [fusion-harness-propuesta.md](fusion-harness-propuesta.md) conserva el estado
  «propuesta (sin código)» aunque el harness ya está implementado.
