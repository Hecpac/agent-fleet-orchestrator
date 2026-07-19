# impl-notes: FOM-2 radar multi-proyecto y avisos de Decision Briefs

## Spec anclado

Tabla congelada de la entrevista FOM-2: una `FLEET_RUNS_DIR`, aviso inmediato
best-effort, invalid visible con salida no-cero, radar read-only y done con
salida humana/JSON más métricas en `fleet_report`, sin scheduler.

## Log de desviaciones

| # | Dónde (qué parte del spec) | La desviación | Clasificación | Decisión conservadora / A quién regresa | Reversible? |
|---|----------------------------|---------------|---------------|------------------------------------------|-------------|
| 1 | Agrupación por `target_repo` y salida secret-free | El spec no fijó cómo identificar un proyecto sin exponer la ruta completa | REGISTRA-Y-SIGUE | Reusar el target durable internamente; exponer basename sanitizado más SHA-256 estable | sí |
| 2 | Ejecución de pytest | `pytest` y el módulo no estaban en el Python del sistema | REGISTRA-Y-SIGUE | Usar `uvx pytest` efímero con `PYTHONPATH=.`; no instalar ni escribir dependencias en el repo | sí |
| 3 | Superficie exacta `just status` | El `python3` 3.9 de la receta evaluaba `Path \| None` y no soportaba `zip(strict=True)` | REGISTRA-Y-SIGUE | Posponer anotaciones en `fleet_status.py` y usar `zip` normal sólo tras la validación previa de cardinalidad exacta en `fleet_safe_paths` | sí |
| 4 | Suite global | `pytest` sin ruta recogió clones históricos bajo `orchestration/runs/worktrees` y colisionó módulos | REGISTRA-Y-SIGUE | Ejecutar la suite canónica explícita `pytest tests`; preservar todos los worktrees y caches del usuario | sí |
| 5 | Suite de audit/archive | `/usr/bin/openssl` no ofrece ED25519 y causó nueve fallos ambientales | REGISTRA-Y-SIGUE | Reejecutar los nueve nodos con el OpenSSL 3.6.3 ya instalado en `/opt/homebrew/bin` | sí |

## Detenido, esperando resolución

Ninguno.

## Tres para el intento #2

1. Probar `just status --json` con el intérprete exacto del usuario antes del primer diff.
2. Invocar desde el inicio `uvx pytest tests`, excluyendo stores/worktrees runtime.
3. Anteponer `/opt/homebrew/bin` para las suites que requieren ED25519.

## Estado

Construcción completa y sin DETÉN abierto → listo para slice-gate.
