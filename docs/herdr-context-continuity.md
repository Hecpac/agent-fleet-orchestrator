# Continuidad entre sesiones de agentes

El contador es una observación. `fleet_herdr_continuity.py` añade un controlador
explícito de relevos con memoria durable para un roster Herdr identificado.
Mantiene un registro propio; no reabre ni altera la aceptación de misiones
históricas. Esta primera integración cubre la sesión de asesoría de FitScan.
No sustituye la autoridad, bloqueos ni generaciones de `HerdrBackend` para una
misión activa; ese backend no debe compartir agentes con este controlador.

## Qué se conserva

Cada hito observado y terminado guarda el resultado final, la transcripción
nativa completa, las referencias fijadas por el operador y el vínculo al hito
anterior. Al preparar un relevo, el agente redacta un resumen de decisiones,
acciones completadas, pendientes, riesgos y archivos de evidencia. El paquete
incluye objetivo, hechos que debe conservar, restricciones, identidad de origen
y hashes de archivos. Cada paquete apunta al anterior; la información completa
permanece en disco aunque no se copie toda al nuevo contexto.

La nueva sesión recibe el paquete, las referencias y la instrucción de continuar
desde los pendientes sin repetir tareas terminadas. Debe devolver exactamente
objetivo, hechos requeridos, decisiones, completados y pendientes del contrato.
El controlador verifica ese resultado en la transcripción de la nueva sesión y
lo comunica al Lead. La continuidad se cierra cuando el Lead confirma recepción.
Si se renueva al propio Lead, su respuesta de restauración es esa recepción.

Este ACK comprueba recuperación del contrato suministrado, no comprensión
profunda, conservación de todos los matices ni calidad del razonamiento futuro.
Los resúmenes siguen siendo resultados de modelos que requieren revisión para
decisiones importantes. Los originales y transcripciones permiten auditarlos.

## Reglas operativas

- Observación fresca y >=70%: preparar un resumen al cerrar el turno.
- >=80%: renovar después de guardar y verificar el resumen y los archivos.
- Un pedido manual puede iniciar el mismo circuito aunque la última medida sea
  antigua; exige identidad actual, cierre nativo y margen suficiente.
- Reservar al menos 8192 tokens estimados antes de pedir el resumen. Si no hay
  margen, detenerse para intervención; no borrar contexto para resolverlo.
- Nunca interrumpir agentes trabajando, bloqueados o sin estado verificable.
- Verificar sesión, cwd, modelo, esfuerzo, permisos y versión observados.
- Cada envío tiene una intención persistida antes del efecto. Si el resultado
  es ambiguo, observar el mismo intento; no reenviar a ciegas después de un crash.
- Un resumen preparado al 70% se conserva sin bloquear a los otros roles. Si
  aparece trabajo posterior, preparar uno actualizado antes de renovar.
- Una sesión antigua no puede reutilizar la generación ya completada.

70% y 80% son heurísticas iniciales configuradas, no óptimos demostrados. El
contador considera la última llamada; no puede predecir exactamente la siguiente.

## Uso explícito

```sh
just herdr-continuity /ruta/session.json /ruta/plan.json /ruta/control --action status
just herdr-continuity /ruta/session.json /ruta/plan.json /ruta/control --action tick
just herdr-continuity /ruta/session.json /ruta/plan.json /ruta/control --action renew --agent nombre
just herdr-continuity /ruta/session.json /ruta/plan.json /ruta/control --action watch --duration 3600
just herdr-continuity /ruta/session.json /ruta/plan.json /ruta/control --action reconcile
just herdr-continuity /ruta/session.json /ruta/plan.json /ruta/control --action verify
```

`renew` registra/inicia el pedido y devuelve; `tick` o `watch` continúa sus fases.
`watch` es un observador en primer plano acotado a una hora, sin cron, daemon ni
autoarranque. Ctrl+C deja el registro recuperable. Un error de contrato se muestra
como `blocked` y detiene el avance; no se elimina ni convierte en éxito. La misma
configuración y plan deben conservarse para recuperar el registro. Cambios de
política requieren una migración explícita, no editar el archivo bajo el proceso.
`reconcile` permite volver a comprobar la evidencia retenida tras corregir un
lector local. Conserva la intención y el plazo de la fase; no reenvía el prompt.
`verify` recorre offline el journal, los objetos y los turnos nativos de cada
relevo cerrado, sin llamar al proveedor. El PASS se limita al contrato retenido.

El plan fija sesión, roles, Lead, cwd, hogares de Codex, objetivos, restricciones,
hechos requeridos, archivos y raíces de lectura, contexto nativo esperado,
presupuesto de generaciones y timeout por fase. El directorio de control queda
fuera de los candidates y usa archivos de objetos direccionados por SHA-256.
No debe provenir de un Worker ni ubicarse bajo una raíz de producto escribible.

Las escrituras del registro son atómicas y sincronizadas, con lock local. Los
objetos no se sobrescriben. Hashes y permisos de archivos no constituyen una
frontera de seguridad contra otro proceso del mismo usuario. La integración de
aislamiento del Worker conserva su veredicto separado.

## Panel y límites de integración

`fleet_herdr_context.py --continuity-dir /ruta/control` añade el estado de memoria
al panel. Esa opción solo lee: no ejecuta relevos. El nuevo contador proviene del
uso nativo de la sesión nueva; no se pone artificialmente en cero.

Un solo controlador debe ser dueño del despacho del roster durante un relevo.
No existe un bloqueo global contra prompts manuales o clientes Herdr externos.
Un cambio detectado detiene la transición; no se afirma entrega exactamente una
vez frente a clientes concurrentes. No se deben añadir automáticamente estos
agentes a un driver de misión activa que conserve una identidad anterior.

La memoria por hito registra los cierres que el observador alcanza a ver. Si hubo
varios mientras estaba apagado, conserva la transcripción completa del último,
que contiene los anteriores; no fabrica un evento individual por hito perdido.
Al terminar la hora no se inicia otra automáticamente. Relanzar el mismo comando
recupera el journal; la automatización es activa mientras ese controlador corre.

## Verificación

```sh
python3 -B -m unittest discover -s tests -p test_fleet_herdr_continuity.py -v
python3 -B -m unittest discover -s tests -p test_fleet_herdr_context.py -v
git diff --check
```

Los tests de estado usan un transporte simulado y no prueban Codex real. La
evidencia de integración debe conservar por separado los turnos de resumen,
restauración y recepción del Lead, identidades antigua/nueva, política observada,
archivos sin cambios y contador posterior. Que una sesión abra o responda no
demuestra por sí solo esa continuidad.

## Compatibilidades observadas

Codex puede emitir un mensaje de entorno de inicialización antes del primer
`turn_context`. Se conserva su hash por separado; el prompt operativo tiene que
aparecer después del contexto nativo. Entradas adicionales posteriores no se
aceptan como una restauración unívoca.

Herdr puede mantener la identidad anterior después de `/new` hasta el próximo
turno. En ese caso se consulta `/status` una vez, sin guardar datos de cuenta.
Su identificador es solo un candidato: el relevo exige después la identidad
Herdr actualizada y el turno nativo de restauración, con creación posterior,
modelo, esfuerzo, permisos, prompt y respuesta coincidentes. La captura por sí
sola no valida el relevo. `/status` puede consultar los límites de la cuenta.

Las referencias que contienen anotaciones o URLs permanecen como notas de la
memoria. No se convierten en rutas ni se descargan. Se fijan los archivos del
operador y las rutas absolutas literales permitidas, rechazando aliases, enlaces
simbólicos, archivos ocultos o ubicaciones fuera del ámbito autorizado.
