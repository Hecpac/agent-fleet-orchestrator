# Contador de contexto por agente

`scripts/fleet_herdr_context.py` observa sesiones Codex identificadas por Herdr.
Separa esta señal del consumo acumulado de `fleet_herdr_metrics.usage()`.
No envía prompts, compacta, reinicia, cambia límites ni decide aceptación.

## Dato mostrado

Usado aproximado = `last_token_usage.input_tokens + output_tokens`; límite =
`model_context_window` observado en el mismo evento nativo. Disponible aproximado
= máximo entre cero y límite menos usado. Nunca se suman snapshots, contadores
acumulados ni tokens cacheados. El JSON conserva valores exactos del contador.

La barra muestra `ctx~93% ALTO ANT` y `~240/258k L18k`: última llamada de unos
240 mil tokens frente a 258 mil declarados, con unos 18 mil de margen. `L` es
libre aproximado; no descuenta herramientas o mensajes posteriores a la última
llamada. `ANT` indica una observación de más de cinco minutos; no significa que
el agente esté trabajando. `? SIN DATO` nunca equivale a cero consumo.

Las alertas operativas son AVISO desde 70%, ALTO desde 85% y EXCESO sobre 100%.
No son umbrales de calidad de razonamiento. El porcentaje no demuestra capacidad
efectiva para completar el próximo encargo y puede diferir del indicador nativo.
Una compacción o un turno nuevo invalida la medida anterior hasta otro contador.
Identidad, cwd o ventana ausentes/divergentes, JSON incompleto, timestamp futuro
y transcripciones ambiguas producen UNKNOWN. El código no cambia los límites de
compacción ni la política de admisión.

## Entrada explícita

El archivo de sesión, de confianza y provisto por el operador, usa el contrato
existente `{"command": ["/ruta/herdr", "--session", "nombre"], "environment": {...}}`.
El roster usa `schema_version: 1` y `members`, cada uno con `name`, `label`, `cwd`
y `codex_home` explícitos. No se descubren ni leen hogares personales por defecto.
El archivo de sesión determina el ejecutable y entorno; no aceptar uno generado
por un Worker como autoridad para ejecutarlo.

```sh
just herdr-context /ruta/session.json /ruta/roster.json --json
just herdr-context /ruta/session.json /ruta/roster.json --publish --watch-seconds 3600
```

Sin `--publish` es observación. Con ese flag modifica exclusivamente tres tokens
de presentación del pane: `fleet_role`, `fleet_context` y `fleet_context_detail`.
Antes de publicar revalida ocupante/sesión y después comprueba los valores con
`pane get`. El pequeño intervalo entre llamadas no es una transacción de
identidad atómica; estas etiquetas no sirven como evidencia de autoridad.
Cada etiqueta caduca tras 15 segundos sin actualización. El observador es un
proceso de primer plano acotado a 0–3600 segundos, sin daemon ni autoarranque.
Ctrl+C lo detiene; un cierre inesperado deja caducar las etiquetas.

Actualiza cada cinco segundos. El roster fija nombres y cwd; un nuevo ID de
sesión del mismo agente se resuelve de nuevo. Agentes nuevos o movidos de cwd
requieren actualizar explícitamente ese roster. Una caída de Herdr queda como
UNKNOWN y los valores publicados previamente caducan. Un archivo mayor de 64 MiB
queda UNKNOWN; no se presenta un recorte como una transcripción completa.

Para mostrar los tokens, en la configuración local de la sesión Herdr:

```toml
[ui.sidebar.agents]
rows = [["state_icon", "workspace", "tab"], ["$fleet_role", "agent"], ["$fleet_context"], ["$fleet_context_detail"]]
```

Usar `herdr --session nombre config check` y `server reload-config` en el mismo
entorno aislado. No cambiar la configuración global. Herdr mantiene la identidad,
estado, títulos y sesiones nativas; solo se amplía la presentación de sidebar.

## Verificación

```sh
python3 -B -m unittest discover -s tests -p test_fleet_herdr_context.py -v
PYTHONPATH=scripts python3 -B -m unittest discover -s tests -p test_fleet_herdr_metrics.py -v
git diff --check
```

Los fixtures cubren identidades divergentes, acumulados/cache, duplicados,
compacción, ventana agotada, contadores inválidos, antigüedad, JSON parcial,
reescritura y publicación con lectura posterior. Las pruebas de fiabilidad de
razonamiento y recuperación semántica son distintas y permanecen pendientes.

En FitScan se habilitó únicamente la configuración privada de la sesión existente
y un observador de una hora. Los datos proceden de turnos ya ejecutados: esta
verificación no necesita iniciar llamadas nuevas a modelos.

La pestaña `Contexto - Agentes` muestra además un panel de lectura con barras,
porcentaje, valores completos y antigüedad para los cinco roles. Su proceso no
publica metadata: el observador del sidebar sigue siendo el único publicador.
Ambos tienen un presupuesto de una hora; no son servicios persistentes. Al
terminar el proceso, el texto que permanezca en Terminal es una última captura,
no una vista en actualización. La señal puede consultarse de nuevo ejecutando
el mismo comando con el roster y la sesión explícitos.
