# impl-notes: FOM-1 Decision Brief durable entre Lead y humano

## Spec anclado

Decisiones congeladas de la entrevista y slice FOM-1 autorizado tras Fase 0: consenso con evidencia, challenger de identidad distinta, brief Lead→humano, pausa por alcance y timeout de dos horas solo autorresoluble para riesgo bajo y reversible.

## Log de desviaciones

| # | Dónde (qué parte del spec) | La desviación | Clasificación | Decisión conservadora / A quién regresa | Reversible? |
|---|----------------------------|---------------|---------------|------------------------------------------|-------------|
| 1 | Verificación del slice | La shell resolvió `/usr/bin/python3` 3.9, pero el repo ya requiere un runtime con `zip(..., strict=True)`; 80 setups fallaron antes de ejercer FOM-1. | REGISTRA-Y-SIGUE | Usar explícitamente el Python Homebrew compatible para build/test; no ensanchar el slice con compatibilidad 3.9. | sí |
| 2 | Contrato de errores de admisión | Normalizar toda `MissionStateError` del dispatch simple a `FleetControlError` también cambiaba el tipo histórico usado para detectar drift de idempotencia. | REGISTRA-Y-SIGUE | Revertir la normalización amplia y conservar el tipo existente; normalizar únicamente los fallos propios del reconciliador nuevo. | sí |
| 3 | Smoke del sistema arrancado | La receta de la skill asume el daemon Claw, `restart.sh`, TCP 8765 y `observe_stream`; este repo opera un servicio por misión sobre sockets Unix y ledger hash-chained. | REGISTRA-Y-SIGUE | Adaptar el smoke a `fleet_control_service.py start/health/stop`, PID + endpoints privados, MCP stdio/CLI reales, delta de `service.stderr.log` y traza por secuencias del Mission ledger. | sí |

## Detenido, esperando resolución

ninguno

## Tres para el intento #2

1. Fijar el intérprete compatible desde el primer comando y registrar su versión antes de cualquier test.
2. Probar primero el tipo de error público histórico antes de envolver excepciones en un boundary compartido.
3. Diseñar desde el inicio el smoke contra las superficies reales de este repo: servicio Unix para boot/aislamiento, MCP stdio para Lead y CLI para HUMAN.

## Estado

Construcción completa y sin DETÉN abierto → listo para slice-gate.
