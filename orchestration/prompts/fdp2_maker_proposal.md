# FDP-2 Maker — propuesta inicial

Feature: `{{FEATURE}}`
Conversation: `{{CONVERSATION_ID}}`
Target repository: `{{TARGET_REPO}}`
Dedicated worktree: `{{WORKTREE}}`
Durable branch: `{{BRANCH}}`
Required base SHA: `{{BASE_SHA}}`
Durable task spec: `{{TASK_SPEC_FILE}}`
Task spec SHA-256: `{{TASK_SPEC_SHA256}}`

Exact authorized task spec:

```json
{{TASK_SPEC_JSON}}
```

Authorization state: CONTROL already completed the required recon/decision
gates and explicitly authorized implementation before opening this BUILD
conversation. Do not stop to request another pre-slice or implementation
authorization; execute this exact task spec now.

Implementa únicamente el cambio autorizado por CONTROL. Antes de declarar
`DONE`, deja el worktree limpio y añade exactamente un commit sobre el SHA base.
No hagas amend, squash ni reescribas commits anteriores.

`fleet-send` añadirá a este prompt el `run_id` y la línea sentinel exacta que
debes usar. Devuelve solamente un objeto JSON con estos campos exactos seguido
por ese sentinel `FLEET_RESULT` como última línea; no escribas nada más:

```json
{
  "schema_version": 1,
  "summary": "resumen concreto",
  "base_sha": "{{BASE_SHA}}",
  "head_sha": "SHA del commit nuevo",
  "changes": ["cambio verificable"],
  "verification": [
    {
      "id": "test-1",
      "command": "comando ejecutado",
      "status": "passed",
      "evidence": [{"kind": "file", "ref": "ruta/relativa:linea"}],
      "reason": ""
    }
  ]
}
```

`verification` nunca puede estar vacío. Usa `status: not_applicable` con una
razón concreta cuando no exista una prueba ejecutable. Las evidencias admitidas
son `file`, `test`, `run` y `message`. El `ref` de `file` es una ruta relativa al
repo con línea opcional (`ruta:línea`); el de `test` es exactamente un `id` de
esta misma lista `verification`; el de `run` es un `run_id` real de la
conversación; y el de `message` es un `message_id` real. Nunca pongas una ruta
de archivo bajo `test`, `run` o `message`.
Cada `command` con `status: passed` debe afirmar realmente la condición y salir
distinto de cero si falla. Un comando que solo imprime `git status`, `rev-list`
o cualquier otro dato no demuestra por sí solo la afirmación.
