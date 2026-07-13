# impl-notes: FDP-2 añade un diálogo Maker–Checker acotado y fail-closed

## Spec anclado
Tabla de 50 decisiones congeladas en `entrevista-pre-slice` para FDP-2, autorizada por el usuario antes de construir.

## Log de desviaciones
| # | Dónde (qué parte del spec) | La desviación | Clasificación | Decisión conservadora / A quién regresa | Reversible? |
|---|----------------------------|---------------|---------------|------------------------------------------|-------------|
| 1 | Deadline e idempotencia de CONTROL | `show`/`verify` pueden descubrir el deadline sin clave humana | REGISTRA-Y-SIGUE | Aditivo: usar la clave determinista `system:deadline:<conversation_id>` | sí |
| 2 | Fuente de la task spec inicial | El formato de entrada no estaba congelado al comenzar la entrevista | DETÉN | → `entrevista-pre-slice`; resuelto por el usuario con `start --spec-file` y JSON de campos exactos | — |
| 3 | Identidad read-only del Checker | El agente `plan` de OpenCode pedía otra aprobación y no podía finalizar el contrato en un solo turno | REGISTRA-Y-SIGUE | Aditivo: agente local `fleet-reviewer`, primary y read-only, sin fallback ni ampliación de autoridad | sí |
| 4 | Transporte del prompt Checker | OpenCode convirtió un prompt multilínea largo en varios `UserPromptSubmit` | REGISTRA-Y-SIGUE | Plumbing conservador: codificar el prompt lógico exacto como una cadena JSON de una sola línea; mantener fail-closed ante submits múltiples | sí |
| 5 | Evidencia de Maker y contexto de Checker | El primer smoke mostró refs ambiguas y revisión del checkout raíz en vez del worktree Maker | REGISTRA-Y-SIGUE | Endurecer prompts: gramática exacta de refs, comandos que afirman condiciones y worktree/SHA obligatorios | sí |
| 6 | Nombre del preset | El spec fijó el roster pero no el identificador de configuración | REGISTRA-Y-SIGUE | Aditivo: `fleet_dialogue`, sin modificar presets existentes | sí |

## Detenido, esperando resolución
ninguno. El único DETÉN volvió cerrado desde `entrevista-pre-slice` antes de la implementación autorizada.

## Tres para el intento #2
1. Probar en recon un prompt OpenCode mayor de 1 KiB y contar `UserPromptSubmit` antes de diseñar el smoke final.
2. Incluir desde el primer prompt Checker la obligación de inspeccionar el worktree y SHA del Maker, nunca el checkout raíz.
3. Separar desde el inicio los dos smokes obligatorios —JSON inválido y aceptación— en flotas limpias independientes.

## Estado
Construcción completa y sin DETÉN abierto → listo para slice-gate.
