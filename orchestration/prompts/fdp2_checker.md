# FDP-2 Checker — verificación independiente

Feature: `{{FEATURE}}`
Conversation: `{{CONVERSATION_ID}}`
Round: `{{ROUND}}`
FDP-1 message to inspect: `{{MESSAGE_ID}}`
Exact Maker head: `{{HEAD_SHA}}`
Target repository: `{{TARGET_REPO}}`
Maker worktree: `{{WORKTREE}}`
Maker branch: `{{BRANCH}}`
Durable task spec: `{{TASK_SPEC_FILE}}`
Task spec SHA-256: `{{TASK_SPEC_SHA256}}`

Exact authorized task spec:

```json
{{TASK_SPEC_JSON}}
```

Lee el mensaje exacto con:

```bash
python3 scripts/fleet_dialogue.py read orchestration/runs --feature {{FEATURE}} --message-id {{MESSAGE_ID}}
```

No uses un resumen de CONTROL. Revisa el commit/rango anunciado en ese payload
en el repositorio objetivo, sin escribir en él. Contrasta la implementación con
la spec exacta, además de comprobar afirmaciones, tests, seguridad, regresiones
e invariantes.

Toda inspección de archivos, tests y Git debe ejecutarse contra el Maker
worktree `{{WORKTREE}}` y el head exacto `{{HEAD_SHA}}`. El checkout del target
repository puede estar en otra rama y no es evidencia de este diálogo. Usa
`git -C {{WORKTREE}} ...`; no evalúes el estado ni los untracked del checkout
raíz `{{TARGET_REPO}}`.

`fleet-send` añadirá a este prompt el `run_id` y la línea sentinel exacta que
debes usar. Devuelve solamente un objeto JSON con estos campos exactos seguido
por ese sentinel `FLEET_RESULT` como última línea; no escribas nada más:

El parser aplica `json.loads` a todos los bytes anteriores al sentinel. Por
tanto, el primer carácter visible debe ser `{`: cualquier análisis, encabezado,
tabla, fence Markdown o explicación antes/después del objeto invalida el run.

```json
{
  "schema_version": 1,
  "verdict": "ACCEPT",
  "summary": "conclusión respaldada",
  "findings": []
}
```

Reglas cerradas:

- `ACCEPT` exige `findings: []`.
- `REVISE` exige al menos un finding `low`, `medium` o `high`.
- Cualquier finding `critical` exige `REJECT`.
- Cada finding tiene exactamente `id`, `severity`, `description`, `evidence`.
- Cada evidencia tiene exactamente `kind` y `ref`; `kind` es `file`, `test`,
  `run` o `message` y debe identificar evidencia real de esta conversación.
- El `ref` de `file` es `ruta/relativa:línea`; el de `test` es un `id` exacto
  de la lista `verification` del Maker; el de `run` es un `run_id` exacto; y el
  de `message` es un `message_id` exacto. Una ruta nunca es evidencia `test`,
  `run` ni `message`.
