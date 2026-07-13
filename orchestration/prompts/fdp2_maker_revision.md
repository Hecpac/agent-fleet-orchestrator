# FDP-2 Maker — rebuttal y revisión conjunta

Feature: `{{FEATURE}}`
Conversation: `{{CONVERSATION_ID}}`
Revision round: `{{ROUND}}`
Checker message to inspect: `{{MESSAGE_ID}}`
Required base SHA: `{{BASE_SHA}}`
Target repository: `{{TARGET_REPO}}`
Dedicated worktree: `{{WORKTREE}}`
Durable branch: `{{BRANCH}}`
Durable task spec: `{{TASK_SPEC_FILE}}`
Task spec SHA-256: `{{TASK_SPEC_SHA256}}`

Exact authorized task spec:

```json
{{TASK_SPEC_JSON}}
```

Authorization state: this revision remains inside the already authorized BUILD
conversation. Do not request another recon, slice gate, or implementation
authorization; address the exact findings now.

Lee el mensaje exacto con:

```bash
python3 scripts/fleet_dialogue.py read orchestration/runs --feature {{FEATURE}} --message-id {{MESSAGE_ID}}
```

Responde a todos los finding IDs y realiza únicamente la revisión necesaria
dentro de la spec exacta y su alcance negativo.
Antes de `DONE`, deja el worktree limpio y añade exactamente un commit sobre el
SHA base; no reescribas ningún commit ya publicado.

`fleet-send` añadirá a este prompt el `run_id` y la línea sentinel exacta que
debes usar. Devuelve solamente un objeto JSON con estos campos exactos seguido
por ese sentinel `FLEET_RESULT` como última línea; no escribas nada más:

```json
{
  "schema_version": 1,
  "rebuttal": [
    {
      "finding_id": "finding exacto",
      "disposition": "accepted",
      "reason": "respuesta concreta",
      "evidence": [{"kind": "file", "ref": "ruta/relativa:linea"}]
    }
  ],
  "revision_summary": "qué cambió",
  "base_sha": "{{BASE_SHA}}",
  "head_sha": "SHA del nuevo commit",
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

Cada finding debe aparecer exactamente una vez como `accepted` o `disputed`.
`verification` nunca puede estar vacío; `not_applicable` exige una razón.
El `ref` de `file` es `ruta/relativa:línea`; el de `test` es un `id` exacto de
esta revisión; el de `run` es un `run_id` exacto; y el de `message` es un
`message_id` exacto. Nunca uses una ruta bajo `test`, `run` o `message`.
Cada `command` marcado `passed` debe incluir una aserción que termine con exit
no cero cuando la condición falle; imprimir valores no basta como prueba.
