# impl-notes: FOM-2B entrega durable de Decision Briefs por CMUX

## Spec anclado

Tabla congelada de la entrevista pre-slice FOM-2B: aceptación CMUX, retry solo de rechazo explícito mientras siga pendiente, ambigüedad sin retry, Lead de la misma misión y backoff acotado.

## Log de desviaciones

| # | Dónde (qué parte del spec) | La desviación | Clasificación | Decisión conservadora / A quién regresa | Reversible? |
|---|----------------------------|---------------|---------------|------------------------------------------|-------------|
| 1 | Ejecución física de `cmux notify` | El timeout previo de 10 s podía exceder el grace de apagado del servicio persistente | REGISTRA-Y-SIGUE | Acotar el comando local a 2 s; timeout queda `indeterminate` y nunca autoriza retry | sí |
| 2 | Migración de briefs pendientes | Los requests anteriores a FOM-2B no tienen recibo durable que pruebe si ya notificaron | REGISTRA-Y-SIGUE | Derivarlos como `legacy_indeterminate`, visibles pero sin reenvío automático, conforme a la elección de evitar duplicados ambiguos | sí |
| 3 | Jitter del backoff | El spec congeló la secuencia y exigió jitter, pero no su distribución | REGISTRA-Y-SIGUE | Jitter determinista de ±20% derivado de `decision_id` e intento; queda reproducible y testable | sí |
| 4 | Activación automática | El servicio persistente no tenía una señal interna de wake-up para un outbox nuevo | REGISTRA-Y-SIGUE | Reutilizar el loop existente con un worker único y espera máxima de 0.5 s; ningún daemon paralelo ni segundo journal | sí |
| 5 | Revisión independiente heterogénea | El preset `audit` falló antes de efectos porque `~/.config/opencode/skills/hyperframes` es un symlink que escapa el home aislado | REGISTRA-Y-SIGUE | Mantener el fail-closed y ejecutar la revisión con la flota custom read-only Codex + Claude; no relajar el aislamiento de OpenCode | sí |
| 6 | Un solo envío físico concurrente | La primera implementación impedía duplicar un mismo brief, pero dos briefs distintos podían solapar sus llamadas tras liberar el lock del ledger | REGISTRA-Y-SIGUE | Añadir un lock descriptor-anclado por Mission que cubre claim → CMUX → receipt y falla ocupado sin claim; prueba explícita con dos decisiones | sí |
| 7 | Cierre y diagnóstico del worker | La revisión observó que el receipt de stop podía preceder la detección de worker atascado y que los errores persistentes eran silenciosos | REGISTRA-Y-SIGUE | Publicar el receipt solo tras quiescencia y emitir telemetría stderr rate-limited/best-effort con señal de recuperación; stderr roto nunca gobierna liveness | sí |
| 8 | Qué cuenta como rechazo explícito | Un revisor propuso no reintentar `target_unavailable`/`command_unavailable`; la entrevista congeló ambas fallas pre-envío como rechazo explícito retryable, además del nonzero de CMUX | REGISTRA-Y-SIGUE | Conservar la decisión congelada; timeout, excepción del notifier y receipt incierto siguen terminales sin retry | sí |

## Detenido, esperando resolución

ninguno.

## Tres para el intento #2

1. Congelar explícitamente en entrevista la política de migración de requests legacy.
2. Identificar desde fase-0 si el servicio ofrece una señal interna reutilizable además de su loop persistente.
3. Ejecutar CI desde el inicio con Python/Homebrew y OpenSSL modernos, más un `HOME` aislado para OpenCode.

## Estado

Construcción completa y sin DETÉN abierto; hallazgos de revisión remediados y
lista para re-verificación posterior al review.
