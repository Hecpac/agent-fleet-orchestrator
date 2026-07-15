# Plan de implementación: Mission Control multi-agente

Estado: listo para iniciar implementación por slices.

Fecha de congelación: 2026-07-14.

Objetivo: convertir el orquestador actual en una entrada única, durable y
reanudable que potencie la autonomía del Lead y sus especialistas sin debilitar
las garantías existentes de identidad, un solo escritor, evidencia exacta,
aislamiento Git y cierre fail-closed.

## 1. Resultado objetivo

El producto final tendrá una entrada canónica:

    just mission <feature> "<objective>" --workflow <name> --target-repo <repo>

El comando:

1. compila un workflow tipado;
2. crea una mission_id durable;
3. establece el riesgo mínimo y las políticas de auditoría/archivo;
4. entrega al Lead el objetivo completo y un catálogo de capacidades;
5. permite al Lead decidir libremente qué agentes usar, en qué orden, en
   paralelo y cuántas iteraciones ejecutar;
6. registra cada delegación, resultado y dependencia como evidencia durable;
7. puede elevar la misión a assured sin perder la historia previa;
8. reanuda después de la muerte del proceso controlador;
9. produce un archivo incremental verificable;
10. mantiene just dan, just fleet, just race y los comandos FDP como interfaces
    compatibles.

Mission Control será el kernel durable. El Lead seguirá siendo el orquestador
cognitivo. El kernel no elegirá el plan intelectual ni impondrá un pipeline a
las misiones autónomas.

## 2. Decisiones arquitectónicas congeladas

| # | Decisión | Elección |
|---|---|---|
| D1 | Entrada canónica | Modelo híbrido: just mission automatiza trabajo normal y pausa antes de efectos de riesgo alto. |
| D2 | Workflows | DSL tipado y versionado; compone capacidades registradas, nunca shell arbitrario. |
| D3 | Autoridad del grafo | El Lead crea dinámicamente el grafo de trabajo. El workflow declara políticas y capacidades, no una secuencia obligatoria. |
| D4 | Especialistas | Patrón manager/agents-as-tools: el Lead conserva la síntesis; los especialistas pueden recibir subobjetivos acotados y devolver artefactos. |
| D5 | Colaboración | Result-driven y trazable. Un resultado durable puede alimentar un turno posterior; no se usa tráfico crudo entre panes como evidencia. |
| D6 | Subdelegación | Permitida mediante capacidades delegadas y trazables. No se limita al Lead si éste concede can_delegate al run hijo. |
| D7 | Riesgo | El workflow/CLI establece un piso determinista. El Lead puede elevarlo en cualquier momento; el riesgo es monotónico dentro de la misión. |
| D8 | Assured | FDP-2/FDP-3 son mínimos obligatorios, no el máximo de agentes permitido. El Lead puede añadir investigación o revisión antes, entre o después de los gates. |
| D9 | Auditoría | Ledger firmado por CONTROL obligatorio en assured; WORM obligatorio sólo cuando audit.mode=worm. `trust_scope` separa evidencia local de cumplimiento externo. |
| D10 | Archivo | Paquete incremental con evidencia, snapshot final del escritor y delta base_sha a final_sha. |
| D11 | Framework externo | No introducir LangGraph, AutoGen o Temporal en el kernel inicial. Se adoptan sus patrones detrás de interfaces propias. |
| D12 | CMUX | Plano visible y programable; nunca fuente de verdad de completion. |
| D13 | Herramientas del modelo | No se reduce el toolset intelectual. Las restricciones se aplican a identidad, destino de escritura y efectos externos. |
| D14 | Compatibilidad | Todos los contratos actuales de run_id, provider/model, sentinel, leases y worktrees permanecen vigentes. |

## 3. Principios no negociables

### 3.1 Autonomía cognitiva

- El Lead recibe el objetivo completo, no una lista de pasos prefijada.
- Puede usar cero, uno o varios especialistas.
- Puede ejecutar especialistas en paralelo cuando las dependencias lo permitan.
- Puede iterar challenge/revision/verify sin pedir permiso rutinario.
- Puede crear subobjetivos nuevos si permanecen dentro del scope de la misión.
- Los límites de tiempo y coste son recursos, no instrucciones sobre cómo
  razonar.
- No se exige que todos los agentes del roster participen.

### 3.2 Control de efectos

- Sólo una instancia conserva authority=write.
- Toda escritura ocurre en la rama/worktree registrada.
- Acciones sobre producción, dinero, credenciales, datos privados o destrucción
  solicitan aprobación durable antes del efecto, no antes del razonamiento.
- Un prompt enviado manualmente con cmux puede existir visualmente, pero no puede
  producir un terminal aceptado sin run_id, binding, identidad y evidencia.

### 3.3 Evidencia

- Cada misión tiene mission_id.
- Cada invocación tiene run_id.
- Cada delegación tiene delegation_id y parent_run_id.
- Cada resultado tiene artifact_id igual a SHA-256 del contenido.
- El primer terminal de misión y de run es inmutable.
- Notification, Stop y terminal chrome sólo despiertan reconciliación.
- La aceptación depende del ledger y del resultado exacto.

### 3.4 Durabilidad

- Cada efecto del kernel es idempotente.
- El estado se deriva de un event ledger append-only.
- El controlador puede morir entre dos acciones y reanudar sin duplicar
  dispatch, publicación, aprobación o archivo.
- Los comandos de resume no dependen de memoria del proceso anterior.

## 4. Arquitectura objetivo

    Operador / Top orchestrator
        |
        v
    Mission CLI
        |
        +-- Workflow compiler
        +-- Mission state + trace ledger
        +-- Risk/effect policy
        +-- Audit policy
        +-- Archive policy
        |
        v
    Mission kernel ---------------- Human approval
        |
        +-- Autonomous driver ------ CONTROL Lead
        |                               |
        |                               +-- fleet-control / MCP facade
        |                                      |
        |                                      +-- Scout
        |                                      +-- Builder
        |                                      +-- Challenger
        |                                      +-- Verifier
        |                                      +-- delegated child agents
        |
        +-- Assured driver --------- FDP-2 -> human BUILD exit -> FDP-3
        |
        +-- Provider adapters ------ Codex / Claude / OpenCode / Ollama / future API
        |
        +-- CMUX execution plane --- panes / hooks / events / status
        |
        +-- Evidence plane --------- mission ledger / lifecycle ledger / artifacts
        |
        +-- Close ------------------ signed audit / WORM / incremental archive

## 5. Contratos nuevos

### 5.1 Workflow DSL

Los archivos workflows/*.yaml pasan a ser JSON-compatible YAML, igual que
orchestration/router.yaml. Se rechazan claves duplicadas y campos desconocidos.

Ejemplo canónico:

    {
      "schema_version": 1,
      "name": "implementation",
      "description": "Autonomous implementation with proportional assurance",
      "preset": "dan",
      "autonomy": {
        "owner": "lead",
        "allow_parallel": true,
        "allow_subdelegation": true,
        "max_delegation_depth": 3
      },
      "capabilities": {
        "available": ["recon", "build", "challenge", "verify"],
        "required_outcomes": ["lead_result"],
        "writer": "build"
      },
      "risk": {
        "minimum": "low",
        "allow_lead_escalation": true,
        "high_action": "confirm_assured"
      },
      "assurance": {
        "profile": "proportional",
        "preset": "fleet_dialogue",
        "minimum_gates": ["fdp2", "human_build_exit", "fdp3"]
      },
      "audit": {
        "mode": "signed",
        "trust_scope": "local-development",
        "worm_required_for": ["regulated"]
      },
      "archive": {
        "mode": "incremental",
        "content_policy": "full",
        "include_final_tree": true,
        "include_git_delta": true
      },
      "limits": {
        "deadline_seconds": 7200,
        "token_budget": 0,
        "budget_mode": "soft"
      }
    }

El DSL no admite:

- comandos shell;
- nombres de surface CMUX;
- run_id preasignados;
- orden obligatorio de agentes autónomos;
- secretos inline;
- instrucciones capaces de cambiar authority o tool_access;
- desactivar identidad, leases, exact completion o un solo escritor.

### 5.2 Mission ledger

Ruta:

    orchestration/runs/missions/<mission_id>/mission.jsonl

Campos mínimos por evento:

    schema_version
    event_id
    mission_id
    sequence
    timestamp
    kind
    actor
    idempotency_key
    payload
    previous_event_sha256
    event_sha256

Estados derivados:

    created
      -> compiled
      -> booting
      -> running
      -> awaiting_assurance_confirmation
      -> assured_running
      -> completing
      -> archived
      -> succeeded | failed | blocked | abandoned | indeterminate

El riesgo puede subir, nunca bajar. Una aprobación humana permite continuar con
el perfil requerido, pero no reescribe la evaluación anterior.

### 5.3 Grafo de delegación

Cada dispatch registra:

    delegation_id
    mission_id
    run_id
    parent_run_id
    delegated_by
    recipient_instance
    capability
    objective_sha256
    input_artifact_ids
    expected_output_contract
    deadline
    provider
    model

El grafo es emergente. No existe un DAG compilado antes de que el Lead razone.

Un especialista puede subdelegar cuando su capability token durable contiene:

    can_delegate=true
    allowed_capabilities
    max_depth
    remaining_budget
    mission_id
    parent_run_id

La subdelegación no concede write. El writer continúa siendo único.

### 5.4 Fleet Control API

Se implementa una única librería de control con dos fachadas:

- CLI JSON para cualquier agente con shell;
- MCP local para proveedores que soporten herramientas MCP.

Operaciones:

    dispatch
    dispatch-many
    wait
    get-result
    relay-result
    request-assurance
    request-human
    inspect-roster
    inspect-mission
    cancel
    complete

dispatch-many crea todos los runs antes de esperar y devuelve sus run_id. Esto
permite paralelismo real sin convertir la UI en autoridad.

relay-result no copia texto por el terminal. Registra el artifact_id como input
de un nuevo run y genera un prompt tracked que referencia el artefacto exacto.

### 5.5 Riesgo y efectos

El riesgo inicial es el máximo de:

- risk.minimum del workflow;
- override explícito del CLI;
- categorías deterministas declaradas por el objetivo o target;
- elevaciones emitidas por el Lead.

Categorías mínimas:

    repository_local
    external_side_effect
    production
    money
    credentials
    private_data
    destructive
    regulated
    unknown

Reglas:

- repository_local low/medium continúa autónomamente;
- high o unknown exige confirmación antes del primer efecto correspondiente;
- el Lead puede seguir investigando read-only mientras espera;
- la aprobación queda ligada a mission_id, workflow_digest, scope y expiración;
- una aprobación no autoriza acciones fuera de su scope;
- no hay aprobación humana para edición, pruebas o revisión dentro del worktree.

### 5.6 Assured driver

El driver automatiza las acciones que hoy devuelve CONTROL:

1. inicia o reanuda FDP-2;
2. ejecuta dispatch, wait y publish con idempotency keys derivadas de
   mission_id + controller sequence;
3. verifica terminal accepted;
4. solicita y registra la aprobación BUILD;
5. avanza a CHALLENGE;
6. inicia o reanuda FDP-3;
7. ejecuta challenge, publish, phase advance y verify;
8. verifica receipts;
9. permite turnos advisory adicionales solicitados por el Lead;
10. entrega el resultado de assurance al Lead para síntesis final.

FDP-2 y FDP-3 siguen siendo las autoridades de sus invariantes. El driver no
duplica sus state machines.

### 5.7 Auditoría firmada y WORM

Para toda misión assured:

- Mission Control inicia o conecta el AuditService.
- La clave CONTROL se carga desde archivo 0600 o proveedor de claves.
- Se autentica el peer UID del socket.
- Se registran hashes, no secretos crudos.
- Se firman eventos de misión, dispatch, resultado, aprobación, branch,
  receipts y archive root.
- fleet-down no cierra assured si el audit ledger no verifica.

Con audit.mode=worm:

- el arranque comprueba configuración S3 Object Lock;
- cada evento o checkpoint definido se ancla con compliance retention;
- el version ID y headers de retención se guardan en el receipt;
- un fallo de anclaje bloquea el terminal accepted/verified;
- la verificación offline comprueba cadena, firmas y receipts WORM.
- `trust_scope=local-development` exige HTTPS con CA validada y resolución
  exclusivamente loopback; nunca satisface un requisito regulado;
- `trust_scope=external-compliance` rechaza loopback, link-local, redes privadas
  y DNS ambiguo, y es obligatorio para el workflow/perfil/riesgo regulated.

Con audit.mode=signed:

- la cadena local firmada es obligatoria;
- el receipt declara explícitamente worm=false;
- no se presenta como archivo regulado.

### 5.8 Archivo incremental

Nuevo comando:

    python3 scripts/fleet_archive.py create ...
    python3 scripts/fleet_archive.py verify <archive>

Contenido estándar:

    archive-index.json
    archive-receipt.json
    manifest
    state.json
    mission.jsonl
    ledger.jsonl
    prompts/
    tasks/
    results/
    artifacts/
    writer/commits.json
    writer/change.patch
    writer/final-tree.tar

Contenido adicional assured:

    dialogue.jsonl
    dialogue-control.jsonl
    dialogue/
    assurance-control.jsonl
    assurance/
    audit/
    verification-receipt.json
    assurance-receipt.json
    worm-receipts/

Git:

- change.patch usa diff binario y full-index entre base_sha y final_sha;
- final-tree.tar contiene el árbol final sin .git;
- commits.json registra los commits del rango;
- opcionalmente se crea un bundle delta con base_sha como prerequisite;
- el branch original permanece en el repositorio destino.

Privacidad:

- nunca se archivan variables de entorno ni archivos de credenciales;
- content_policy puede ser full, redacted o hash-only;
- toda omisión aparece en archive-index.json;
- un workflow que declare credentials o private_data no puede usar full sin una
  aprobación específica de archivo;
- WORM recibe hashes y receipts, no prompts crudos por defecto.

### 5.9 Provider Adapter

Interfaz común:

    validate_configuration
    launch_spec
    prepare_submission
    submit
    confirm_submission
    observe
    extract_final_response
    verify_identity
    cancel

Adaptadores iniciales:

    CodexAdapter
    ClaudeAdapter
    OpenCodeAdapter
    OllamaAdapter

La primera implementación envuelve el código existente; no lo reescribe en un
big bang. Los adaptadores API directos futuros usan la misma interfaz.

### 5.10 Trazas y métricas

El mission ledger es la fuente primaria. Se pueden añadir exporters, pero nunca
son autoridad de completion.

Spans mínimos:

    mission
    workflow_compile
    risk_escalation
    agent_run
    delegation
    result_relay
    human_wait
    assurance_gate
    archive
    worm_anchor

Métricas:

- tiempo hasta primer resultado útil;
- wall time total;
- tiempo esperando humano;
- agentes seleccionados y omitidos;
- profundidad y fan-out de delegación;
- tokens/coste por provider/model;
- tasa de succeeded/blocked/failed/indeterminate;
- resultados adoptados por el Lead;
- findings capturados por challenge/verify;
- reanudaciones y acciones idempotentes evitadas;
- tamaño y verificación del archivo.

Los datos sensibles quedan fuera de spans por defecto.

## 6. Plan por slices

### Slice 0 — Congelar contratos y fixtures

Objetivo: convertir este plan en invariantes ejecutables sin cambiar runtime.

Cambios:

- añadir reglas nuevas a orchestration/INTERNAL_WIRING.md;
- añadir schemas de workflow, mission event, delegation y archive index;
- crear fixtures válidos e inválidos;
- documentar compatibilidad de comandos.

Pruebas:

- claves desconocidas y duplicadas fallan;
- schemas no permiten shell ni cambios de authority;
- los 180 tests existentes continúan verdes.

Salida:

- contratos congelados;
- ningún comportamiento productivo cambia.

Rollback: eliminar schemas/fixtures nuevos.

### Slice 1 — Workflow compiler

Objetivo: hacer workflows ejecutables como política, no como DAG.

Archivos previstos:

    scripts/workflow_config.py
    orchestration/workflow.schema.json
    workflows/implementation.yaml
    workflows/hotfix.yaml
    workflows/research.yaml
    tests/test_workflow_config.py

Entregables:

- validate, show y compile;
- canonical JSON y workflow_digest;
- resolución contra router presets/capabilities;
- migración de workflows actuales;
- just workflow-validate.

Aceptación:

- misma entrada produce bytes canónicos idénticos;
- no hay efectos CMUX durante compile;
- workflow inválido falla antes del boot.

### Slice 2 — Mission state, lineage y resume

Objetivo: crear el kernel durable sin ejecutar agentes todavía.

Archivos previstos:

    scripts/fleet_mission.py
    scripts/fleet_mission_state.py
    scripts/fleet_trace.py
    tests/test_fleet_mission_state.py
    tests/test_fleet_trace.py

Entregables:

- mission_id y directorio durable;
- ledger hash-chained;
- eventos idempotentes;
- state derivado;
- show, resume-plan y mark-terminal;
- parent_run_id, delegation_id y artifact_id.

Pruebas:

- kill/restart entre eventos;
- retry con misma key no duplica;
- retry con payload distinto falla;
- primer terminal inmutable;
- tampering rompe verify.

### Slice 3 — just mission sobre el camino autónomo

Objetivo: activar la entrada canónica reutilizando Dan+.

Archivos previstos:

    scripts/mission-run.py
    scripts/fleet-risk.py
    orchestration/prompts/mission_lead.md
    justfile
    tests/test_mission_run.py
    tests/test_fleet_risk.py

Entregables:

- mission, mission-dry, mission-show y mission-resume;
- workflow compile + mission creation + fleet boot;
- ejecución autónoma con Lead;
- runtime request-assurance;
- aliases existentes sin cambio;
- status/progress CMUX ligados a mission_id.

Aceptación:

- una misión repo-local completa igual que just dan;
- matar mission-run y reanudar no crea otro lead run;
- high/unknown pausa antes del efecto;
- el Lead conserva selección dinámica.

Smoke:

- una misión read-only;
- una misión con Builder y commit;
- una misión que usa Scout + Challenger en paralelo;
- una misión que eleva riesgo.

### Slice 4 — Fleet Control API y colaboración multi-agente

Objetivo: convertir agentes en capacidades invocables y trazables.

Archivos previstos:

    scripts/fleet_control.py
    scripts/fleet_artifacts.py
    scripts/fleet_delegation.py
    scripts/fleet_mcp.py
    tests/test_fleet_control.py
    tests/test_fleet_artifacts.py
    tests/test_fleet_delegation.py

Entregables:

- CLI JSON y MCP sobre el mismo core;
- dispatch-many;
- relay-result por artifact_id;
- capability tokens;
- subdelegación con profundidad y scope;
- grafo consultable;
- prompts del Lead y especialistas con catálogo de capacidades.

Aceptación:

- el Lead puede lanzar dos runs antes de esperar;
- un resultado exacto alimenta otro run;
- un especialista autorizado puede subdelegar;
- subdelegación nunca crea un segundo writer;
- un artifact alterado es rechazado;
- ningún flujo depende de screen scraping.

### Slice 5 — Assured runner automático

Objetivo: ejecutar FDP-2/FDP-3 de extremo a extremo con resume.

Archivos previstos:

    scripts/fleet_assured_runner.py
    scripts/fleet-approve.py
    scripts/mission-run.py
    tests/test_fleet_assured_runner.py

Entregables:

- action executor idempotente para dispatch/wait/publish/advance;
- puente de misión autónoma a assured;
- aprobación ligada a mission/scope;
- turnos advisory adicionales permitidos;
- síntesis final vuelve al Lead.

Aceptación:

- accepted -> human approval -> challenge -> verify funciona con un comando;
- process kill en cualquier action reanuda sin duplicar;
- malformed result y timeout fallan cerrados;
- el Lead conserva capacidad de pedir análisis adicional;
- los controllers actuales siguen verificando sus propios archivos.

### Slice 6 — Audit service integrado y WORM

Objetivo: conectar la capacidad existente al ciclo de vida real.

Archivos previstos:

    scripts/fleet_audit_control.py
    scripts/fleet_audit_client.py
    scripts/mission-run.py
    scripts/fleet_assured_runner.py
    scripts/fleet-down.sh
    tests/test_fleet_audit_integration.py

Entregables:

- lifecycle start/health/stop del AuditService;
- firma de eventos assured;
- receipts de anchor;
- perfil signed y perfil worm;
- verificación antes de teardown.

Aceptación:

- assured no puede cerrar sin cadena válida;
- signed funciona offline y declara no-WORM;
- worm falla cerrado sin configuración compliance;
- corrupción, UID incorrecto o anchor incompleto bloquean cierre;
- payloads crudos no salen al sink WORM.

### Slice 7 — Archivo incremental unificado

Objetivo: hacer portable la evidencia normal y assured.

Archivos previstos:

    scripts/fleet_archive.py
    scripts/fleet-down.sh
    tests/test_fleet_archive.py

Entregables:

- packer y verifier;
- archive index con hashes/tamaños/políticas;
- resultados, prompts y tareas generales incluidos;
- final tree + patch + commits;
- assured stores y audit incluidos;
- política full/redacted/hash-only.

Aceptación:

- verify funciona sin el workspace CMUX;
- final-tree reproduce exactamente final_sha;
- patch binario corresponde a base/final;
- un byte alterado rompe verificación;
- archivos/symlinks inseguros son rechazados;
- el branch con commits permanece alcanzable.

### Slice 8 — Provider adapters

Objetivo: desacoplar el kernel de TUIs específicos sin perder capacidades.

Archivos previstos:

    scripts/fleet_providers.py
    scripts/providers/codex.py
    scripts/providers/claude.py
    scripts/providers/opencode.py
    scripts/providers/ollama.py
    tests/test_fleet_providers.py

Entregables:

- interfaz común;
- wrappers actuales migrados gradualmente;
- provider/model/variant siguen ligados al resultado;
- punto de extensión para API directa.

Aceptación:

- mismos transcripts/sentinels producen mismos terminales;
- no cambia el CLI de fleet-send/fleet-dispatch;
- una implementación fake permite tests deterministas;
- adapter incorrecto no puede reclamar identidad de otro provider.

### Slice 9 — Métricas, trazas y evals de orquestación

Objetivo: mejorar selección y prompts sin crear gates nuevos.

Archivos previstos:

    scripts/fleet_report.py
    scripts/fleet_export_trace.py
    tests/test_fleet_report.py
    evals/

Entregables:

- report JSON/humano por misión;
- spans con parent/child;
- exporters opcionales;
- casos de evaluación para routing, paralelismo, relay y escalation.

Aceptación:

- métricas se derivan sólo de evidencia durable;
- exporter caído no afecta completion;
- prompts/resultados sensibles no aparecen por defecto;
- comparación de providers no cambia autoridad del Lead.

### Slice 10 — Enforcement, perfiles de ejecución y migración

Objetivo: cerrar bypasses sin reducir el toolset por defecto.

Entregables:

- execution_profile=native como default;
- execution_profile=sandboxed/regulated opcional;
- control socket valida identity/capability token;
- raw CMUX input no puede producir resultado tracked;
- migración y compatibilidad de manifests;
- live smoke completo;
- documentación operativa final.

Aceptación:

- native conserva herramientas actuales;
- sandboxed cambia el perímetro, no las capacidades declaradas;
- todos los comandos legacy continúan funcionando;
- una misión completa autonomous y otra worm-assured pasan live smoke;
- teardown y recovery funcionan después de reinicio CMUX.

## 7. Dependencias entre slices

    Slice 0
      -> Slice 1
      -> Slice 2
      -> Slice 3
          -> Slice 4
          -> Slice 5
              -> Slice 6
              -> Slice 7
          -> Slice 8
          -> Slice 9
    Slices 4-9
      -> Slice 10

No se inicia Slice 5 antes de que mission resume sea confiable. No se integra
WORM antes de que assured runner sea idempotente. No se reemplaza fleet-down
antes de que el archive verifier exista.

## 8. Estrategia de pruebas

### Suite obligatoria por slice

    python3 -m unittest discover -s tests

### Matriz de fallos

- proceso muerto antes/después de cada append;
- dispatch confirmado pero respuesta CLI perdida;
- resultado persistido pero terminal no escrito;
- terminal escrito pero lease no liberado;
- CMUX boot_id cambiado;
- replay con gap;
- result_file alterado;
- workflow alterado después de compile;
- aprobación reusada fuera de scope;
- AuditService ausente;
- WORM anchor parcial;
- archive incompleto;
- branch avanzado durante teardown;
- dos intentos de subdelegación al writer;
- provider/model/variant drift.

### Live smokes requeridos

1. autonomous read-only;
2. autonomous writer con commit;
3. paralelismo de dos especialistas;
4. relay de Challenger a Verifier;
5. subdelegación autorizada;
6. pause/resume humana;
7. FDP-2/FDP-3 completo;
8. signed audit offline;
9. WORM contract con sink compliance;
10. archive verify desde un directorio limpio;
11. kill/resume del mission runner;
12. CMUX restart/recovery.

## 9. Rollout

### Etapa 1 — Shadow

- just mission --dry-run compila y compara contra just dan;
- no cambia el runtime legacy;
- se recopilan diferencias de plan.

### Etapa 2 — Canary

- workflows research e implementation usan Mission Control;
- feature flag FLEET_MISSION_ENGINE=1;
- just dan permanece como fallback.

### Etapa 3 — Default

- just mission es recomendado;
- just dan delega al workflow implementation;
- fleet/fdp commands siguen disponibles para operación manual.

### Etapa 4 — Hardening

- perfiles regulated;
- WORM real;
- adapters API opcionales;
- sandbox OS opcional.

## 10. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Convertir autonomía en pipeline | El DSL no puede declarar orden de agentes autónomos; el grafo nace de delegaciones del Lead. |
| Kernel demasiado grande | Slices pequeños, interfaces explícitas y reutilización de controllers existentes. |
| Doble autoridad Lead/controlador | Lead decide trabajo; kernel decide identidad/efectos; FDP decide sólo sus gates. |
| Repetir acciones después de crash | Idempotency key obligatoria y state derivado del ledger. |
| Conversación infinita | Deadline/coste como límites de recursos, terminal explícito y posibilidad de extensión; no orden fijo. |
| Pérdida de capacidades por sandbox | native sigue default; perfiles estrictos cambian destino/efecto, no herramientas intelectuales. |
| Filtrar secretos en trazas/archivo | políticas de contenido, no env capture, hash-only/redacted y receipts de omisión. |
| Vendor lock-in | provider adapters + CLI/MCP core; sin framework externo en el kernel. |
| Reescritura peligrosa | envolver primero, migrar después; comandos y tests existentes permanecen. |

## 11. Definición global de terminado

Todos deben cumplirse:

- just mission ejecuta una misión autónoma completa.
- El Lead selecciona agentes dinámicamente y puede paralelizar.
- Especialistas autorizados pueden subdelegar.
- Toda contribución adoptada conserva lineage y artifact_id.
- No existe más de un writer.
- Un high/unknown se pausa antes del efecto y puede reanudarse.
- Assured se ejecuta end-to-end y permite agentes advisory adicionales.
- La auditoría signed es obligatoria en assured.
- WORM falla cerrado cuando el workflow lo exige.
- Mission runner sobrevive kill/restart sin duplicar efectos.
- Archivo estándar y assured verifican offline.
- Writer final tree y delta son restaurables/inspeccionables.
- Provider/model/variant permanecen ligados a cada resultado.
- CMUX sigue visible pero no autoritativo.
- Métricas no son gates.
- Comandos legacy siguen pasando sus contratos.
- Toda la suite y los live smokes están verdes.
- INTERNAL_WIRING.md documenta cada nuevo invariante y su test.

## 12. Punto exacto de arranque

El primer trabajo autorizable es Slice 0.

Scope:

- schemas y fixtures;
- reglas nuevas de INTERNAL_WIRING;
- tests de schema;
- ninguna modificación de ejecución.

Comando de cierre:

    python3 -m unittest discover -s tests

Sólo después de cerrar Slice 0 se autoriza Slice 1. Cada slice posterior requiere
su propio gate, implementación, suite y smoke proporcional al riesgo.

## 13. Referencias técnicas adoptadas

- OpenAI Agents SDK: manager con agents-as-tools, handoffs, mezcla de
  orquestación LLM/código y trazas jerárquicas:
  https://github.com/openai/openai-agents-python
- LangGraph: ejecución durable, resume y human-in-the-loop:
  https://github.com/langchain-ai/langgraph
- AutoGen SelectorGroupChat: selección dinámica y necesidad de termination
  conditions explícitas:
  https://github.com/microsoft/autogen
- Claude Agent SDK: herramientas completas, MCP in-process y hooks
  deterministas alrededor del loop del modelo:
  https://github.com/anthropics/claude-agent-sdk-python
- CMUX: plano programable mediante CLI/socket, hooks y sesiones visibles:
  https://github.com/manaflow-ai/cmux
- Temporal AI Agents workshop: durabilidad, HITL y especialistas heterogéneos
  detrás de un orquestador:
  https://github.com/temporal-community/ai-agents-workshop-python
