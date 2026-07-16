# impl-notes: FDP-3 encadena CHALLENGE y VERIFY con assurance independiente

## Spec anclado
Tabla de 35 decisiones congeladas en `entrevista-pre-slice` para FDP-3, autorizada por el usuario después del recon y del Slice-Gate de FDP-2.

## Log de desviaciones
| # | Dónde (qué parte del spec) | La desviación | Clasificación | Decisión conservadora / A quién regresa | Reversible? |
|---|----------------------------|---------------|---------------|------------------------------------------|-------------|
| 1 | Reuso del ledger FDP-1 para `challenge` y `verification` | El verificador FDP-2 comparaba sus bindings contra todos los mensajes del ledger, por lo que rechazaría los dos mensajes legítimos de FDP-3 | REGISTRA-Y-SIGUE | Aditivo: limitar el ownership FDP-2 a mensajes con `source_instance` Maker/Checker y dar a FDP-3 ownership exclusivo de Challenge/Verify | sí |
| 2 | Handoff explícito CHALLENGE → VERIFY | El spec congeló dos pasos explícitos pero no nombró el acuse del controller después de que `fleet_state` escribiera la transición | REGISTRA-Y-SIGUE | Reversible: `step --phase-advanced`; `fleet_state` sigue siendo el único escritor de fase y FDP-3 revalida el control head en history antes de crear el prompt Claude | sí |
| 3 | Teardown de snapshots manteniendo solo cinco verbos públicos | El teardown necesita retirar worktrees después de confirmar ausencia del workspace, pero el spec congeló solo `start|step|show|verify|abandon` | REGISTRA-Y-SIGUE | Reversible: opción interna `verify --cleanup-snapshots`, idempotente y clean/exact-only, sin introducir un sexto verbo de controller | sí |
| 4 | Identidad durable de `minimax_checker` | La decisión congeló el fail-closed por ausencia/cambio de variant, pero no nombró el motivo terminal de auditoría | REGISTRA-Y-SIGUE | Reversible: usar `frontier_opencode_variant_mismatch` tanto para ausencia como para valor distinto, separado del mismatch provider/model | sí |
| 5 | Pin de `variant=none` en la TUI reutilizable | El smoke demostró que OpenCode 1.17.15 solo acepta `--variant` bajo `opencode run`; la TUI `opencode` rechaza el flag y aborta el boot. `opencode run --interactive` exige un mensaje inicial y sale al terminar, por lo que no sustituye al pane reutilizable. Una prueba aislada de TUI sin `-m`, con `model` y `variant` declarados por agente, produjo `message.variant=none` y `reasoning_tokens=0` | DETÉN | Regresa al usuario: autorizar o rechazar el cambio del mecanismo congelado, de identidad por flag CLI a identidad por agente dedicado, manteniendo manifest/ledger/evidence fail-closed | sí |

## Detenido, esperando resolución
Autorizar si `minimax_checker` puede usar un agente OpenCode dedicado que declare
`model: minimax/MiniMax-M3` y `variant: none`, arrancado como TUI sin `-m` ni
`--variant`. La evidencia final seguirá exigiendo provider/model/variant exactos.

**Resuelto 2026-07-13:** Hector autorizó la opción A vía entrevista-pre-slice.
Mecanismo cambiado a agente dedicado `.opencode/agents/minimax-checker.md`;
validación estática reformada agent-pinned en `router_config.py`; suite
162/162 OK; smoke en vivo PASS con evidencia en
`orchestration/smoke-evidence/minimax-checker-agent-pin-20260713.md`.
DETÉN #5 cerrado — el build FDP-3 puede reanudar.

## Tres para el intento #2
1. Modelar explícitamente el ownership por slice cuando varios controllers comparten el ledger FDP-1.
2. Congelar en entrevista la sintaxis exacta del handoff de dos comandos entre el controller y `fleet_state`.
3. Definir antes del build los nombres del receipt/archive y el retry idempotente de cleanup posterior al cierre.

## Log de desviaciones (continuación — build reanudado)
| # | Dónde (qué parte del spec) | La desviación | Clasificación | Decisión conservadora / A quién regresa | Reversible? |
|---|----------------------------|---------------|---------------|------------------------------------------|-------------|
| 6 | Contrato estricto del Checker (primer carácter `{`) | Con el reasoning deshabilitado (`variant: none`), MiniMax-M3 narró su análisis como prosa visible ANTES del JSON ACCEPT válido; el parser estricto terminalizó `indeterminate` (`invalid_checker_contract`). El modo de falla original (`<think>` en el body) está muerto — evidencia en `fdp3-agentpin-resume-20260713.md` — pero el flujo FDP-2→FDP-3 sigue sin completarse en vivo | DETÉN | Regresa al usuario: A) endurecer contrato en el prompt template (runtime), B) endurecer instrucciones del agente `minimax-checker`, C) runtime congelado + retry como conversación nueva, D) cambiar modelo del checker. No se relajó el parser ni se reintentó | — |

**DETÉN #6 resuelto 2026-07-13 (Opción B, autorizada por Hector):** contrato
endurecido en `.opencode/agents/minimax-checker.md` (runtime intacto),
lockeado en spec-coherence, verificado en vivo con el mismo gatillo de
narración: primer carácter `{`, un solo JSON, cero prosa, `variant=none`,
`reasoning=0`. Evidencia: `minimax-checker-hardened-contract-20260713.md`.

| # | Dónde | La desviación | Clasificación | A quién regresa | Reversible? |
|---|-------|---------------|---------------|-----------------|-------------|
| 7 | Maker (codex sandbox) | `blocked` determinista ×3 en dos boots frescos: seatbelt niega `index.lock` en `.git/worktrees/<feature>-maker`; el intento A había commiteado con el comando idéntico (diferencial UNKNOWN). Todo fix cruza el runtime congelado (flags codex en router, config codex, o layout de worktrees) | DETÉN | → usuario (decisión de runtime/entorno) | — |
| 8 | `fleet-down.sh` (código FDP-3 sin commitear) | Conversación abandonada sin publicaciones deja `dialogue-control.jsonl` sin `dialogue.jsonl`; el receipt del teardown falla (exit 2) y el workspace sobrevive; reproducido ×2 (fleets b y b2, vivos-idle) | DETÉN (fix pendiente del build FDP-3, hecho verificado) | → build FDP-3 al reanudar (fix de receipt sobre archivos existentes, fail-closed) | sí |

**DETÉN #7 resuelto 2026-07-13 ("Go" de Hector):** causa raíz real era el
guardrail `--sandbox workspace-write` de `router_config.py:636` + admin dir
del worktree fuera del sandbox; fix en `fleet-up.sh` (writable_roots del
`.git` común, commit `7cb8b48`), probado con `codex sandbox` y 4 commits de
maker en vivo. **DETÉN #8 resuelto:** fix de receipt (commit `b059555`),
verificado en vivo con los teardowns de b/b2.

| # | Dónde | La desviación | Clasificación | A quién regresa | Reversible? |
|---|-------|---------------|---------------|-----------------|-------------|
| 9 | Contratos GLM/Claude | GLM (agente stock `plan`) y Claude (sin wrapper) violaron el contrato primer-carácter-`{` igual que MiniMax; patrón B extendido: agente `glm-challenger` + `--append-system-prompt` en `claude_reviewer` (commit `b53be2d`). GLM endurecido PROBADO en vivo (e2e2 cruzó el phase gate); Claude endurecido SIN probar | REGISTRA-Y-SIGUE (patrón B ya congelado) | — | sí |
| 10 | Transporte frontier | Carreras de doble `UserPromptSubmit` (codex 1×, pane opencode degradado 3× determinista) → `frontier_session_binding_ambiguous`; y sentinel pegado a `}` sin newline (MiniMax, 1×) → `frontier_sentinel_missing`. Fail-closed correcto siempre | DETÉN (diseño de robustez de transporte) | → usuario / próximo slice FDP-3 | — |
| 11 | `fleet_dialogue.publish` | Acepta publicaciones que ningún controller espera; un mensaje huérfano envenena el store append-only para siempre (gate BUILD→CHALLENGE y teardown rechazan). Exhibit vivo: fleet `fdp3-e2e3-20260713` (workspace:50) | DETÉN (gap de diseño: gatear publish en `expected` y/o quarantine de stores) | → usuario | — |

## Estado
DETÉN #5-#8 resueltos y verificados en vivo. **DETÉN #10 y #11 abiertos.**
Carril restante del flujo completo: la pata Claude endurecida y el terminal
`verified` (recon nombrado: transporte de `fleet-send.sh`/`fleet_frontier.py`
antes del próximo intento e2e). Evidencia consolidada:
`fdp3-e2e-campaign-20260713.md`. No pasa a slice-gate.
