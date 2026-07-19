# Smoke en vivo: FOM-2 radar multi-proyecto de Decision Briefs

## Veredicto

**PASS.** La CLI productiva publicó dos Decision Briefs en dos proyectos bajo una
sola `FLEET_RUNS_DIR`, intentó sus avisos CMUX y el radar real devolvió ambas
decisiones antes de un ledger inválido sin mutar la verdad Mission. El round-trip
del radar JSON fue **53.858 ms** y agregó cero líneas a stderr.

## Encarnación probada y adaptación del daemon

Este cambio vive en CLIs de proceso corto (`fleet_control.py`,
`fleet_status.py`, `fleet_report.py`) y en la construcción del proceso MCP; no
existe un daemon global de radar que reiniciar. Reiniciar Fleet Control no carga
el status ni el reporte y habría agregado un efecto fuera del camino cambiado.
Por eso `smoke-verify` se adaptó a procesos frescos cargados directamente desde
este working tree sin commit. Cada invocación arrancó un intérprete Python 3.13
nuevo y terminó con stderr vacío.

El primer intento de preparar el fixture con `/usr/bin/python3` no llegó a la
superficie: macOS entregó Python 3.9, incompatible con `zip(..., strict=True)`
ya usado por el repo. El smoke se repitió completo mediante `uv run --python
3.13`; esto fue una corrección del runtime del harness, no del producto.
Después se cerró la compatibilidad de la superficie exacta: `just status
--json` ejecutó el `python3` 3.9 del sistema contra `orchestration/runs`, produjo
JSON canónico y terminó 0 con stderr vacío.

## Camino real ejercido

1. Se crearon dos Missions válidas y activas dentro del mismo store físico:
   `fom2-alpha` apuntó a `alpha-repo` y `fom2-beta` a `beta-repo`.
2. Para cada Mission se sembraron dos resultados attested por el kernel real de
   admission/ledger/CAS: una recomendación y un challenge con identidad distinta.
3. La CLI productiva `scripts/fleet_control.py ... request-decision` publicó:
   - Mission `f989f368-d496-5122-ad48-2b74c0e7b63f`, decisión
     `83f434e5-ae97-560b-a377-85f68c66764e`, 203.214 ms.
   - Mission `011d7814-8ea8-5832-8276-c17af58f74f8`, decisión
     `abb24c35-33e4-5afe-a878-492c0bbefc1c`, 95.773 ms.
4. Ambas respuestas tuvieron `appended=true` y notification
   `attempted=true`, `accepted=true`, `guarantee=best_effort`,
   `authority=wake_up_only`. Los dos avisos atravesaron el `cmux notify` real.
5. Se agregó una tercera Mission canónica con ledger intencionalmente inválido:
   `3ec06083-e0db-4175-8dd1-540a3862f1e7`.
6. `scripts/fleet_status.py --runs-dir <store> --json` devolvió 2 —la salida
   esperada por evidencia inválida— y un JSON con:
   - `authority=mission_ledger`, `read_only=true`;
   - dos decisiones pendientes;
   - proyectos `alpha-repo/aad41f9f5256` y `beta-repo/c1d44a472b1c`;
   - un `INVALID/UNREADABLE` con razón redactada;
   - cero líneas en stderr.
7. La superficie humana devolvió también 2, mostró las Decision Briefs antes del
   bloque de ledger inválido y agrupó ambos proyectos. No apareció ninguna
   pregunta, título ni disenso privado sembrado.
8. `scripts/fleet_report.py ... --json` sobre la Mission alpha devolvió 0 y
   `decisions={total:1,pending:1,resolved:0,human_resolved:0,
   automatic_resolved:0}` con cero stderr.

## No-regresión específica

- Los SHA-256 de ambos `mission.jsonl` fueron idénticos antes y después de las
  consultas:
  - alpha: `1e70268023bc0b611682f89adb53a00c09ae48f48b990e752b7d61c93e65f688`;
  - beta: `b7b96ee124aaab8245d90381641b58d8378c1fdfc81f673ed4448a752e548f6b`.
- El ledger inválido no ocultó las dos decisiones sanas y forzó salida no-cero.
- El radar no incluyó marcadores privados de question/dissent/title.
- La notificación no sustituyó al ledger: las filas fueron reconstruidas por un
  proceso posterior exclusivamente desde Mission.

## Carriles no probados en vivo

- No se esperaron dos horas reales para observar `DEFAULT_ELIGIBLE`. El lector
  read-only y la no-mutación están test-locked con reloj posterior al deadline;
  para cerrarlo en vivo, mantener una Mission efímera dos horas y consultar el
  radar sin ejecutar reconcile.
- No se forzó una falla real de CMUX después del append. El test lock inyecta
  `OSError`, prueba que la decisión permanece durable y reporta
  `accepted=false`; para cerrarlo en vivo, retirar temporalmente `cmux` del PATH
  de una CLI efímera y verificar el ledger.
- La publicación se ejerció por la CLI Lead, no por una sesión MCP stdio o el
  servicio Unix completo. La misma construcción productiva inyecta el notifier
  en MCP y está test-locked; para cerrarlo en vivo, repetir la petición por
  `scripts/fleet_mcp.py` y correlacionar el evento exacto.
- El ledger inválido vivo fue JSON corrupto, no symlink/hardlink/mode hostil.
  Esos casos están cubiertos por el lector descriptor-anclado y pytest; para
  cerrarlos en vivo, repetir el store efímero con cada binding inseguro.
- No se midió un store con cientos o miles de Missions. Para cerrar capacidad,
  generar un corpus efímero grande y medir latencia/RSS sin alterar el contrato.
- Multi-root, outbox, reintentos y recordatorios periódicos no se probaron porque
  fueron descartados explícitamente del slice S0.
