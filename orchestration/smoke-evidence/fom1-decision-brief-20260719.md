# Smoke en vivo: FOM-1 Decision Brief v1

## Veredicto

**PASS.** Una misión efímera ejecutó el camino real Lead/MCP → ledger → pausa
selectiva → HUMAN/CLI → reapertura de admisión. Publicar el brief tardó
73.193 ms, el rechazo pre-efecto 71.977 ms y la resolución humana 49.578 ms.

## Encarnación probada y adaptación del daemon

Este repositorio no tiene el daemon Claw, `restart.sh`, TCP 8765 ni
`observe_stream`. Su encarnación de producción es un Fleet Control por misión,
con lifecycle descriptor-verificado y sockets Unix. Por eso la técnica A/B se
adaptó explícitamente a un arranque fresco mediante:

```text
python3 scripts/fleet_control_service.py --runs-dir <ephemeral-runs> \
  --mission-id f65c8170-b491-512a-b3e7-25fbd0495af8 start
```

- PID nuevo: `70273`; `ps` mostró estado `Ss`, PPID 1 y uptime 11 s.
- Health antes y después del turno: `status=ok`, protocolo
  `fleet-control-unix-v2`, launch
  `8f1e0c28-0ab8-488c-856e-1500ecf4cacc`, cuatro endpoints especialistas.
- Socket base y socket builder: Unix `srw-------`, owner `hector`; binding
  `15997788d71adba0122378c39f932491aadc9fe2db0bedeee5db041d4243b783`.
- Liveness equivalente: health respondió después del turno y el ledger avanzó
  de la petición en secuencia 30 a la resolución en 31 y al cierre de la prueba
  de admisión en 33.
- `service.stderr.log`: 0 líneas antes y 0 después. Delta: cero tracebacks,
  errores de integridad o reinicios.
- Stop autenticado: `stopped=true`; PID 70273 dejó de existir y los cinco
  endpoints quedaron ausentes.

El árbol probado contenía cambios sin commit; no se afirmó que `HEAD` los
describiera. La misión y el servicio cargaron directamente ese working tree.

## Camino real ejercido

1. Se prepararon dos resultados attested en la misión efímera: scout
   `openai/gpt-5.6-sol` con artefacto
   `d41fd4f5ad7b198c7b516ab9a3703d69e10a8c7f68e607800c3bb1babc4d07d6`
   (secuencia 26) y challenger `zai/glm-5.2` con artefacto
   `769b3834566a33250ef87712b2a910382d5095f7c99a34ce43fab8bfa665964b`
   (secuencia 28).
2. La superficie MCP real de management se ejecutó como proceso stdio de
   `scripts/fleet_mcp.py`. `request_decision` devolvió `isError=false`,
   `appended=true` y decisión
   `9f9be89d-f11a-543f-b650-ab270287395b` pending. El evento de secuencia 30 es
   actor `lead`, hash
   `10c9cd33e65189f197408eaf9be2e062cb38badb1a240c668eba7c6e17ce27a9`.
3. `scripts/fleet-decision.py ... show` renderizó en una pantalla las dos
   opciones, tradeoffs, recomendación, challenge, disenso, identidades, plazo
   exacto y default permitido.
4. La CLI real `scripts/fleet_control.py ... dispatch --recipient builder`
   reprodujo la condición bloqueada. Salió 2 con
   `recipient is blocked by pending human decisions`; mission ledger quedó en
   30 eventos y ledger de wrapper en 4 antes y después. Por tanto hubo cero
   mutación y cero intento de CMUX/proveedor.
5. Un intento de especialista por su socket Unix real de invocar
   `request_decision` devolvió `ok=false`,
   `specialist control request failed closed`, sin filtrar la ruta temporal.
6. La CLI HUMAN real resolvió `additive`: `appended=true`, actor `HUMAN`,
   `resolution_kind=human`, evento de secuencia 31 y hash
   `7d01ccf4b39cfc139cff35a63ac6a342eb4e1dedea165e3e92a8389d19f1a6e5`.
7. Sin lanzar proveedor, el mismo preflight de Control reservó después una
   admisión real para `builder` (secuencia 32) y la abortó prelaunch con sus
   bindings exactos (secuencia 33). El admission terminó `active=false`, fase
   `aborted`, y `pending_decisions=[]`.
8. `fleet_mission.py verify` cerró con `valid=true`, 33 eventos y head
   `a1f3855f9ded5dc3b3d0157278be686bdf31a2c497ffd7a884e7fb3854370533`.

## No-regresión específica

- El fallo objetivo “Lead pregunta sin consenso verificable” no ocurrió: el
  request durable contiene dos lineages distintos y el challenger proviene de
  CHALLENGE con identidad de modelo diferente.
- El fallo “pausa toda la flota o deja pasar al builder” no ocurrió: el builder
  fue rechazado antes de efecto y las pruebas de invariante ejercen que scout
  continúa; `checkpoint` permite trabajo pero impide completion.
- El fallo “cualquier CLI/especialista publica la decisión” no ocurrió: el MCP
  especialista no descubre la herramienta y el socket real falló cerrado.
- El fallo “el timeout pisa una decisión humana concurrente” quedó cubierto por
  el test lock que fuerza esa carrera; HUMAN gana y el reconciliador relee.

## Carriles no probados en vivo

- No se ejecutó inferencia remota real para producir recomendación/challenge;
  el fixture sembró resultados por el mismo admission/ledger/CAS durable. Para
  cerrarlo: ejecutar una misión Dan real, esperar ambos `run_id` y publicar sus
  artefactos por `wait` antes del brief.
- No se esperaron dos horas de reloj real. Para cerrarlo: dejar una misión
  efímera viva hasta el deadline y ejecutar `fleet-decision.py reconcile`; el
  contrato, el rechazo pre-deadline y la carrera HUMAN/CONTROL sí están
  test-locked.
- No se lanzó un builder real tras resolver, para no crear un efecto de
  proveedor/CMUX ajeno al smoke. La reapertura se probó en el admission kernel
  y se abortó antes de launch. Para cerrarlo: despachar una tarea real y esperar
  su terminal exacto.
- No se ejerció en vivo una decisión medium/high/unknown o irreversible. Para
  cerrarlo: publicar un brief sin default y confirmar que sigue pending tras el
  deadline; los rechazos de defaults inseguros sí están test-locked.
- La publicación Lead usa MCP stdio/CLI porque el socket base del daemon está
  deliberadamente reservado a health/shutdown; los sockets por instancia son
  solo para especialistas autenticados. No se abrió un management socket nuevo.
