# Smoke en vivo: F1 `fusion` — adjudicación con evidencia

Encarnación: `main@ee58d96` (worktree limpio). Corrida real, tier workhorse,
`ANTHROPIC_API_KEY` fuera del entorno (el arnés además la sanea por diseño).

## Veredicto: **PASS**

Run `d8281769`, `status=complete`, exit 0, `input_mode=inline`:

| leg | modelo | status | latencia | chars |
|---|---|---|---|---|
| architect | claude-sonnet-5 | ok | 116.6 s | 5457 |
| builder | gpt-5.6-terra | ok | 54.2 s | 3667 |
| fusion | claude-sonnet-5 | ok | 219.9 s | 12521 |

Wall total ≈ 5.6 min (workers en paralelo; adjudicador secuencial).
`summary.json` con `fusion_prompt_hash` y `rendered_prompt_hash` poblados;
línea canónica en `outputs/fusion/ledger.jsonl`.

## Contrato de salida verificado (fused.md)

- Las tres secciones exactas: `# Fused Answer`, `# Consensus & Divergence`
  (con `## Supported consensus`, `## Genuine divergence`, `## Uncertainty`),
  `# Discarded` — más el `## Final self-audit` A1–A9 respondido por guardrail.
- Atribución inline en claims materiales, incluidas variantes calificadas:
  `[ARCHITECT+BUILDER, verified]`, `[FUSION, derived from direct grep…]`.
- **La conducta diseñada ocurrió en vivo**: el adjudicador refutó la premisa
  de la pregunta (fleet-wait no pollea el ledger — bloquea sobre el event
  stream de cmux, `fleet_wait.py:296-310,357`) y **corrigió con evidencia**
  la afirmación cuantitativa confiada del BUILDER ("2 × pending roles scans"):
  fila en Discarded con `contradicted-by-evidence` citando
  `fleet_wait.py:236-259` cruzado con `fleet_ledger.py:73-102,209-240`.
- `Lean:` únicamente con evidencia citada (file:line leídos con tools +
  comandos ejecutados); lo no resoluble quedó `genuinely open` (el mecanismo
  de fallback), no promediado.
- A1 (trust boundary) respondido: no siguió instrucciones embebidas en los
  inputs.

## Desviaciones del plan (registradas durante la ejecución con subagentes)

1. Tasks 4+5 se combinaron en un implementer (mecánicas, mismo archivo);
   commits separados y revisión conjunta.
2. El fixture del plan para contar invocaciones del shim claude (`wc -l`) era
   defectuoso con prompts multilínea; el implementer lo reemplazó por archivos
   por-llamada preservando la intención exacta de cada aserción (auditado por
   el spec reviewer).
3. Quality review encontró 1 Important real: una FusionError post-gate
   (colisión de markers) escapaba a `finish()` — run sin summary/ledger.
   Corregido (`42e6a6c`): exit 5 con registro durable; test nuevo lo lockea.
4. Task 7 (justfile) lo ejecutó el controller inline por proporcionalidad;
   sus gates (23/23, compileall, 545<800 líneas) son la verificación.
5. No hubo pase final de revisión adicional: cada commit pasó doble revisión
   (spec + calidad) y este smoke es el gate de cierre del spec (§5.7).
6. Dos implementers reportaron en su contexto inicial archivos sucios
   inexistentes (confabulación); ground truth verificado limpio por el
   controller en ambos casos con `git status` real.

## Carriles NO probados

1. **Modo path en vivo** — solo test-level (shim de 61k chars). Vía: una
   pregunta que produzca outputs >60k reales, o bajar
   `FLEET_FUSION_PROMPT_BUDGET`.
2. **Instrucción del operador** (`just fusion "<q>" "<instrucción>"`) — no
   ejercida en vivo; cubierta por render tests.
3. **Tier sota y panel local** — no ejercidos en F1 (panel no es parte de
   `fusion` por spec).
4. **Timeout del adjudicador en vivo** — solo test-level (process-group kill
   verificado con shims).
5. FYI heredado del review de Task 2: el saneo de entorno cubre solo
   `ANTHROPIC_API_KEY`; variables hermanas (`ANTHROPIC_AUTH_TOKEN`,
   `CLAUDE_CODE_OAUTH_TOKEN`) quedan fuera del scope congelado.
