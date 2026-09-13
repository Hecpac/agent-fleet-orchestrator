# Herdr six-role fleet v1 (FLEET-01)

## Relación con FLEET-02

FLEET-02 no activa este roster heterogéneo de seis puestos. Entrega un perfil
Herdr distinto y opt-in, `astra_sol_research_v1`, con cinco miembros Codex y seis
turnos: Plan → Research → Build → Review → Verify → Synthesis. DeepSeek/OpenCode
permanece fuera del runtime y no es segundo writer ni Research admitido.

En ese perfil, CONTROL fija un snapshot CAS pre-Build para Research, Build recibe
Plan+Research, Review y Verify reciben el mismo Plan+Research+Build, y Synthesis
recibe todos los resultados. Permission policy v3 y archive v6 son obligatorios;
el `astra_sol` histórico conserva sus cuatro miembros, cinco turnos y contratos
anteriores. Archive v6 valida evidencia Research contra el árbol investigado y
las demás evidencias contra el árbol final, incluso si el candidate ya no existe.

El lector `fleet_herdr_opencode_evidence.py` valida offline la consistencia de
bytes y bindings externos de un segmento exportado. No autentica
criptográficamente el origen, no lanza procesos y siempre informa permisos
`not_attested` y autoridad `none`. No está conectado a Herdr backend, admission
ni cierre; por tanto no habilita el puesto `worker_deepseek` de este documento.

## Resultado y límite

FLEET-01 entrega un roster declarativo cerrado y un validador local para seis
puestos de mantenimiento supervisado:

- `orchestration/fleet/herdr-six-role-v1.json`
- `scripts/fleet_herdr_roster.py`
- `tests/test_fleet_herdr_roster.py`
- `docs/herdr-six-role-fleet.md`

Los cuatro archivos están integrados en el candidate canónico por
`worker_sol`, su único escritor autorizado. DeepSeek produjo los otros tres
archivos en un directorio aislado; Sol los inspeccionó, verificó e integró.

Este fue un piloto live supervisado: sí utilizó sesiones y modelos para producir
las aportaciones de Research, Sol y DeepSeek/OpenCode. No fue una Mission formal
del controller, no activó el roster de seis puestos y no generó por sí mismo
admissions, ledger/CAS de Mission, cierre ni aceptación. Tampoco existe evidencia
de permisos o aislamiento OS efectivos para OpenCode.

El validador integrado es otra superficie: funciona offline, usa sólo Python
standard library y nunca inicia Herdr, CLIs de modelos, providers ni agentes. Su
éxito demuestra el contrato de datos descrito aquí, no el comportamiento live
del piloto.

## Contrato cerrado

El objeto raíz contiene exactamente:

`schema_version`, `mode`, `mission_enabled`, `canonical_writer`,
`input_policy`, `closure_authority`, `max_canonical_writers` y `roles`.

| Campo | Valor v1 |
| --- | --- |
| `schema_version` | `herdr.fleet.roster.v1` |
| `mode` | `supervised-maintenance` |
| `mission_enabled` | `false` |
| `canonical_writer` | `worker_sol` |
| `input_policy` | `independent-v1` |
| `closure_authority` | `controller` |
| `max_canonical_writers` | `1` |

Cada elemento de `roles` contiene exactamente `id`, `role`, `cli`,
`provider`, `model`, `reasoning_requested`, `workspace_access` y
`deliverable`. Las seis identidades son exactas y únicas:

| `id` | `role` | `cli` | `provider` | `model` | `reasoning_requested` | `workspace_access` | `deliverable` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `lead` | `lead` | `codex` | `openai` | `gpt-6-astra` | `high` | `read-only` | `plan-and-synthesis` |
| `research` | `research` | `codex` | `openai` | `gpt-6-astra` | `high` | `read-only` | `evidence-report` |
| `worker_sol` | `worker` | `codex` | `openai` | `gpt-5.6-sol` | `high` | `canonical-candidate` | `canonical-candidate` |
| `worker_deepseek` | `worker` | `opencode` | `deepseek` | `deepseek-flash` | `thinking` | `isolated-contribution` | `isolated-contribution` |
| `reviewer` | `reviewer` | `codex` | `openai` | `gpt-5.6-sol` | `high` | `read-only` | `review-findings` |
| `verifier` | `verifier` | `codex` | `openai` | `gpt-5.6-sol` | `high` | `read-only` | `verification-result` |

`worker_sol` y `worker_deepseek` comparten `role=worker`, pero no autoridad:
DeepSeek sólo produce una contribución aislada; Sol es el único integrador del
candidate. Los valores de modelo y reasoning son solicitudes del roster, no
evidencia observada de una futura Mission.

## Entradas, dependencias y salidas

Todo task futuro debe suministrar explícitamente identidad de Mission/run/puesto
y fase; objetivo y aceptación; baseline y candidate aplicable; instrucciones y
skills versionados; permisos solicitados; artefactos de entrada; y protocolo de
resultado. No se heredan conversaciones, skills, credenciales, herramientas ni
permiso de subdelegación.

| Puesto | Dependencias de entrada | Salida | Autoridad |
| --- | --- | --- | --- |
| `lead` | Objetivo, restricciones y evidencia admitida; en Synthesis, todos los resultados requeridos. | `plan-and-synthesis`, conservando desacuerdos y `NOT_VERIFIED`. | Lee y recomienda; no escribe ni cierra. |
| `research` | Plan y preguntas acotadas. | `evidence-report` trazable que separa hechos, inferencias y desconocidos. | Lee; no modifica el candidate. |
| `worker_deepseek` | Plan, evidencia Research y copia independiente del baseline fijado. | `isolated-contribution` con patch/contenido, inventario y hashes. | Escribe sólo fuera del candidate canónico. |
| `worker_sol` | Plan, Research, baseline y contribución aislada verificada. | `canonical-candidate` integrado con evidencia local proporcional. | Único escritor canónico; no cierra. |
| `reviewer` | Plan, criterios y el mismo candidate congelado que Verifier. | `review-findings`. | Lee; no recibe el dictamen de Verifier. |
| `verifier` | Plan, criterios y el mismo candidate congelado que Reviewer. | `verification-result`. | Lee; no recibe el dictamen de Reviewer. |

La secuencia propuesta para un perfil runtime futuro es Plan → Research →
contribución DeepSeek → integración Sol → Review y Verify independientes →
Synthesis. `closure_authority=controller` significa que ningún rol, exit 0,
reporte o test cierra por sí solo.

## Skills pertinentes

Los skills viajan como contenido explícito de la tarea; una referencia no
instala herramientas, crea agentes ni amplía permisos.

| Función | Selección |
| --- | --- |
| Lead | `codex-os`; `entrevista-pre-slice` sólo ante una decisión material irresuelta; `slice-gate` para Synthesis. |
| Research | `fase-0-recon`; `deep-research` sólo para investigación amplia y acotada. |
| Workers | `fase-0-recon` ante código desconocido; `smoke-verify`; `impl-notes` sólo ante desviaciones materiales. |
| Reviewer | `fase-0-recon`; `slice-gate` contra su contrato. |
| Verifier | `smoke-verify`; `slice-gate` contra su contrato. |

El metadata de un skill no autoriza subdelegación. Las fuentes locales están en
`orchestration/role-skills`; integrarlas en task packets de seis puestos
requiere el perfil lifecycle futuro.

## Validador offline y protocolo CLI

`load_roster(path)` usa parsing JSON estricto y rechaza claves duplicadas,
`NaN`, `Infinity` y `-Infinity`. `validate_roster(value)` devuelve una
copia equivalente del roster válido o lanza `ValueError`; no devuelve el mismo
objeto, no completa valores y no tolera extensiones.

La validación exige tipos JSON estrictos, seis IDs únicos, identidades y
deliverables exactos, un solo `canonical-candidate`, lectores `read-only`,
`mission_enabled=false`, `input_policy=independent-v1` y cero claves extra.
Un boolean no se acepta como entero.

La entrada CLI es:

```sh
python3 -B scripts/fleet_herdr_roster.py PATH
```

Un manifest válido produce exit 0 y un objeto JSON con exactamente estas claves:
`valid`, `mission_enabled`, `schema_version`, `mode`,
`canonical_writer`, `max_canonical_writers`, `input_policy`,
`closure_authority`, `role_count` y `role_ids`. Un input o uso inválido
produce exit 1 y `{"valid":false,"error":"..."}`. Ambos terminan con newline y
no emiten un traceback esperado.

Los tests provider-free cubren el manifest válido, smoke CLI válido/inválido,
escritores adicionales, lectores promovidos, roles ausentes/duplicados,
identidades y tipos incorrectos, Mission habilitada, policy/closure divergentes,
campos desconocidos, claves JSON duplicadas, constantes no finitas y archivo
ausente. No prueban contención OS, calidad del modelo ni Mission acceptance.

## Estado de FLEET-01 y trabajo futuro

La implementación local de los cuatro archivos está completa y lista para
congelar. Permanecen pendientes la revisión y verificación independientes del
mismo candidate congelado; hasta entonces no hay aceptación final.

Activar realmente este roster es un cambio posterior y versionado:

1. Añadir, si se autoriza, un perfil de seis miembros/siete turnos sin reescribir perfiles
   históricos. El contrato congelado debe ser obligatorio para ejecutar y
   cerrar.
2. Diseñar evidencia de permisos OpenCode antes de habilitar DeepSeek. El lector
   offline FLEET-02 sólo prueba consistencia de identidad/contenido; evidencia
   ausente, mezclada o `not_attested` debe seguir bloqueando una admisión.
3. Hacer durable la contribución aislada con baseline, inventario, hashes y CAS;
   rechazar paths escapados y cualquier escritura al candidate o ledger.
4. Versionar driver, guidance y dependencias. Reviewer y Verifier deben usar
   sesiones distintas, el mismo árbol congelado y ningún veredicto mutuo.
5. Versionar archive/replay/recovery, preservar lectura histórica v2–v5 y vetar
   cierre si falta o cambia evidencia requerida. Resume reconcilia el mismo run,
   sin reenvío ciego.
6. Integrar al pool sólo después de fixtures provider-free. La preparación debe
   ser idempotente: `ready` no implica asignación, admission ni éxito.

Runtime heterogéneo, contención efectiva, recuperación, archive ampliado,
coste/calidad y una Mission formal permanecen `NOT_VERIFIED` en FLEET-01.

## Ocho evaluaciones futuras Sol/DeepSeek

Son diseños pendientes, no resultados ni un benchmark ejecutado. Cada par debe
usar el mismo baseline, work order, fixtures, límites y scoring; separar
corrección, protocolo, latencia y coste. Como cambian CLI, provider y modelo, una
diferencia compara el sistema completo y no demuestra causalidad ni superioridad
general.

| Caso | Trabajo pareado | Evidencia |
| --- | --- | --- |
| 1. JSON estricto | Producir el roster desde la especificación cerrada. | Schema exacto, cero extras y output protocol. |
| 2. Escalada | Revisar fixtures con segundo writer, lector promovido y Mission habilitada. | Rechazos correctos y cero writes fuera de alcance. |
| 3. Parsing adversarial | Reparar duplicate keys, no finitos y tipos confundidos. | Tests rojos antes y verdes después por causa correcta. |
| 4. Implementación | Implementar el validador en clones equivalentes. | Positivos/negativos ocultos, diff y compatibilidad CLI. |
| 5. Diagnóstico read-only | Encontrar divergencias sembradas entre manifest, doc y tests. | Precisión, referencias y abstención ante desconocidos. |
| 6. Reparación iterativa | Corregir una suite focal inicialmente roja. | Iteraciones, regresiones y preservación de trabajo ajeno. |
| 7. Handoff aislado | Entregar tres archivos a otro integrador. | Paths, inventario, hashes, reproducibilidad y aislamiento. |
| 8. Review congelado | Revisar un candidate con defectos conocidos y distractores. | Recall/precision, independencia y respeto del freeze. |

Antes de concluir, los casos requieren repeticiones y evaluación ciega cuando el
artefacto lo permita. Este conjunto limitado no prueba equivalencia de CLIs,
rendimiento en producción ni superioridad de Sol o DeepSeek.
