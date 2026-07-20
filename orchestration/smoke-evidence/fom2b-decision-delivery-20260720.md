# Smoke en vivo: FOM-2B entrega durable de Decision Briefs

## Veredicto

**PASS.** Una misión efímera ejecutó la CLI productiva, el Fleet Control Unix
persistente y el `cmux notify` real. Un rechazo explícito se reintentó con el
backoff durable, el worker releyó el Lead vigente de la misma misión y CMUX
aceptó el aviso dirigido. Un resultado ambiguo quedó `indeterminate` y no se
reintentó después de restaurar un Lead válido.

La aceptación aquí significa exactamente que el proceso `cmux notify` terminó
con código 0. No afirma lectura humana ni convierte CMUX en autoridad de la
decisión.

## Re-smoke posterior a revisión independiente

**PASS.** Después de incorporar el lock de serialización hallado por review, una
segunda Mission fresca (`9746e6a2-4fac-5940-b20c-45ba0e2ef155`) ejecutó dos
`request-decision` productivos en procesos concurrentes mientras el worker
persistente también estaba activo.

- La decisión `3b65c3f7-e331-55cb-9df0-75aba09c161c` adquirió el lock, reclamó
  el intento 1 y obtuvo aceptación del CMUX real.
- La decisión `57ba83dd-be9a-5c87-90a6-76c935e0d469` publicó request + enqueue,
  pero su CLI observó el lock ocupado y regresó `pending`, `attempts=0`, sin
  tocar CMUX. Después el worker la reclamó una sola vez y CMUX la aceptó.
- El shim diagnóstico añadió una espera de 1 s y un guard independiente
  alrededor del binario real. Su log exacto fue `START 20478`, `END 20478 0`,
  `START 20502`, `END 20502 0`; no apareció `OVERLAP`. El segundo claim ocurrió
  118.018 ms después del primer receipt durable.
- Ambos attempts se dirigieron al mismo Lead real
  `597d4970-ec3e-4aa0-b7a3-2f96125aace2`, terminaron `accepted/1` y fueron
  resueltos después por la CLI HUMAN.
- Fleet Control PID `20342`, launch
  `4bea2468-e800-47ed-a41c-ab80855fe78e`, permaneció healthy. El stop
  autenticado publicó `service-stopped.json` modo 0600 solo después de que el
  worker terminó; luego no quedaron PID ni sockets.
- `service.stderr.log` quedó en 0 líneas/0 bytes. Mission verify: `valid=true`,
  39 eventos, head
  `2de9b70655f5124e840240661d942f6d6d9480cc3ad5f14b1e66c62a2c349b4f`.

El primer intento de iniciar esta segunda Mission usó un socket root demasiado
largo y el lifecycle lo rechazó antes de lanzar proceso o CMUX. El harness se
repitió con `/tmp/f2bs.JKOr6j`; no se relajó el límite AF_UNIX del producto.
Este re-smoke cargó el working tree final posterior a review.

## Revisión independiente

La flota read-only usó dos identidades: Codex
`openai/gpt-5.6-sol` y Claude Reviewer `anthropic/claude-fable-5`. Sus runs
durables fueron `ac786a39-ea40-45d7-a90f-bbd3b49dcd82`,
`fef05224-f5b8-4602-bac7-a253f719fe78` y el follow-up
`9eda4e7f-7766-4f76-a081-3d9759da718a`.

- Codex encontró la carrera alta entre dos briefs distintos. Se corrigió con
  el lock por Mission y quedó cubierta por test más el re-smoke concurrente.
- Su observación sobre `target_unavailable`/`command_unavailable` no cambió el
  diseño: la entrevista congelada los clasificó como rechazo explícito
  pre-envío y retryable; la ambigüedad post-intento sigue sin retry.
- Claude encontró tres detalles bajos: receipt de stop antes del check de
  worker, falta de telemetría y `/tmp` rígido en una prueba. Los tres se
  corrigieron.
- El follow-up confirmó el span del lock y encontró que stderr roto podía matar
  el worker. La telemetría quedó best-effort y un test fuerza `OSError` sin
  afectar liveness.

La flota fue archivada tras el review en
`orchestration/runs/archive/fom2b-review-20260720-C5EFD904-2393-458E-929A-C3D977771469-cd24572889ffc72ff210a130776eaaa0c786c483bcce7c73927a478430cca713`.

## Verificación automatizada

- Suite completa con Python/Homebrew, OpenSSL moderno y `HOME` aislado después
  del fix de serialización: 935 tests, 2 skips, 0 fallos, 804.272 s.
- Tras el último guard best-effort de telemetría: 139 tests relevantes, 0
  fallos, y los 11 nodos exactos del invariante, 0 fallos.
- `git diff --check` y `py_compile` de las superficies tocadas terminaron 0.

## Encarnación probada y adaptación del daemon

Este repositorio no usa Claw, TCP 8765 ni `observe_stream`. La encarnación
productiva es un Fleet Control por misión, con sockets Unix autenticados y un
worker de outbox dentro de `scripts/fleet_mcp.py`. Por eso `smoke-verify` se
adaptó a un servicio fresco cargado directamente desde este working tree:

```text
/opt/homebrew/bin/python3 scripts/fleet_control_service.py \
  --runs-dir /tmp/fom2b-live.hyylqc/runs \
  --mission-id 168f55df-90ef-5d3a-9718-4ebcadd71ff1 start
```

- Proceso: PID `61399`, launch
  `559f76df-ebd1-4688-bb83-f2bccdc1af8c`.
- Health antes y después: `status=ok`, protocolo
  `fleet-control-unix-v2`, cuatro endpoints especialistas y binding
  `8fef52a661aa89ab250fe685dcf97e519b1a8075f1c0e96d94ef69e20279469b`.
- Socket base y socket builder: Unix `srw-------`, owner `hector`.
- `service.stderr.log`: 0 líneas y 0 bytes antes y después; delta cero.
- Stop autenticado: `stopped=true`; PID ausente y cero sockets remanentes.

El árbol probado contenía cambios sin commit; no se afirmó que `HEAD` los
describiera. La misión y el servicio cargaron el working tree exacto.

## Camino real ejercido

1. El fixture preparó una Mission `dan` running y dos resultados attested por
   admission/ledger/CAS: scout `openai/gpt-5.6-sol`, artefacto
   `a5a27e32174a10c26d2570c486913b145ae7ae708920d019d77438ae70dbbf25`,
   y challenger `zai/glm-5.2`, artefacto
   `614408b6e7ee56ea8b07bf3f563b527f749df277b1a57bd079359f0d1be6f02c`.
2. La CLI real `scripts/fleet_control.py request-decision` publicó atómicamente
   el request y el enqueue para la decisión
   `c8c1da00-d3b8-5620-87ed-b3d4b850f4fa`. El primer target, deliberadamente
   ausente, fue rechazado por el CMUX real con código 1; el recibo durable quedó
   `rejected`, intento 1.
3. El worker reintentó tras 5.017 s y todavía observó el target anterior. Ese
   segundo rechazo programó el escalón siguiente. Tras re-vincular el manifest
   al Lead vigente de la misma misión, superficie
   `597d4970-ec3e-4aa0-b7a3-2f96125aace2` en workspace
   `68703155-5c86-4014-a803-ffbc8f323c0c`, el tercer intento ocurrió 32.012 s
   después y `cmux notify` terminó 0. El recibo quedó `accepted` en 93.102 ms.
4. La segunda decisión,
   `0ff79f7e-dfe2-54c1-ac53-647495c97ac1`, ejerció la ambigüedad: el proceso
   excedió el timeout productivo de 2 s. El ledger registró un intento y outcome
   `indeterminate/timeout`, sin `next_attempt_at`.
5. Se restauró inmediatamente el Lead CMUX válido y el servicio continuó
   healthy. El radar se consultó 37.520 s después del request: la decisión
   ambigua conservó exactamente un intento y ninguna nueva llamada.
6. `scripts/fleet_status.py --json` reconstruyó desde Mission las dos filas:
   `accepted/3` e `indeterminate/1`, ambas con
   `delivery_next_attempt_at=null`; `read_only=true` y
   `authority=mission_ledger`.
7. La CLI HUMAN real resolvió ambas decisiones. `list-decisions --pending`
   devolvió una lista vacía.
8. `fleet_mission.py verify` cerró con `valid=true`, 43 eventos y head
   `08a2bfa9196ea0a2eb988e560400fc34f87540b4ce699ddf43a5c1b959583b46`.

## Traza durable relevante

- Secuencias 30–31: request y enqueue primarios publicados en la misma
  transacción Mission.
- Secuencias 32–37: tres pares claim/outcome; targets exactos
  `1111…`, `1111…`, `597d…`; outcomes `rejected`, `rejected`, `accepted`.
- Secuencias 38–39: segundo request y enqueue atómicos.
- Secuencias 40–41: único claim/outcome al target `2222…`, outcome
  `indeterminate/timeout`.
- Secuencias 42–43: resoluciones HUMAN.

Cada attempt se publicó antes del efecto CMUX y cada outcome enlaza el SHA-256
del enqueue y del attempt correspondiente.

## No-regresión específica

- El worker no hizo scan global ni fallback: cada attempt quedó ligado al
  `lead.uuid` y `workspace_uuid` del manifest de esa Mission.
- Un rechazo explícito permaneció retryable solo mientras la decisión estaba
  pending; los tiempos observados corresponden a los escalones 5 s y 30 s con
  jitter.
- La ambigüedad no se convirtió en entrega ni en retry. Restaurar el target no
  cambió `indeterminate/1`.
- El radar y el brief mostraron estado, intentos y próximo intento sin usar
  eventos CMUX como recibo.
- El cuerpo del aviso enviado solo contenía feature, mission ID, decision ID,
  riesgo, impacto y deadline; no incluyó pregunta, opciones, disenso, paths ni
  artefactos.

## Harness y carriles no probados en vivo

- No se ejecutó inferencia remota para fabricar recomendación/challenge; el
  fixture sembró dos resultados distintos por el kernel durable. Para cerrarlo,
  ejecutar dos agentes reales, esperar ambos run IDs y usar sus artefactos.
- El timeout ambiguo se produjo con un shim efímero de `cmux` que durmió 3 s
  únicamente para el UUID centinela `2222…`; para los targets ausente y válido
  el shim hizo `exec` al binario real de CMUX. Esto prueba el boundary de proceso
  productivo, pero no una caída orgánica del socket CMUX. Para cerrar ese
  carril, inducir una falla real post-envío cuya recepción no pueda conocerse.
- No se mató el proceso entre claim y receipt; el estado `in_flight` y la
  prohibición de retry están test-locked. Para cerrarlo en vivo, matar una
  misión desechable exactamente después del evento attempted y reiniciar el
  host sin tocar el ledger.
- No se esperó el cap real de 10 minutos. La secuencia completa y el jitter
  determinista están cubiertos por pruebas de reloj; para cerrar capacidad,
  mantener un target explícitamente rechazado hasta observar el cap.
