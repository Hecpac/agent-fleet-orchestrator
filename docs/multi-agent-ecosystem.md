# Ecosistema multi-agente: casos de uso más allá del fleet propio

Investigación 2026-07-08. Fuentes: addyosmani.com/blog/code-agent-orchestra,
shipyard.build/blog/claude-code-multi-agent, github.com/andyrewlee/
awesome-agent-orchestrators (60+ orquestadores), betterstack.com (guía cmux).

## El marco de 3 niveles (Addy Osmani)

Por qué multi-agente multiplica: paralelismo (3× throughput), especialización
(contexto enfocado por agente), aislamiento (clones Git separados), aprendizaje compuesto
(reglas acumuladas entre sesiones — nuestra skill de cmux).

- **Tier 1** — subagentes / Agent Teams dentro de una sesión.
- **Tier 2** — orquestadores locales (cmux, este fleet). Sprints paralelos.
- **Tier 3** — agentes cloud asíncronos (Claude Code Web, Jules, Copilot
  Agent): asignar → cerrar laptop → volver a un PR. Para drenar backlog.

Uso maduro en 2026 = los tres a la vez.

## Arquetipos del ecosistema y su caso de uso

| Arquetipo | Ejemplo | Caso de uso |
|---|---|---|
| Ratchet CI-gated | Multiclaude (D. Lorenc) | Drenar issues chicos: CI verde → automerge; tolera duplicados |
| Ciudad jerárquica | Gas Town (S. Yegge) | Máximo paralelismo personal ("mayor" descompone y spawnea); caro (3 cuentas Max) |
| Empresa de agentes | 5dive | Agentes de larga vida con rol/memoria/organigrama + escalación a humano por Telegram |
| Teams nativo | Claude Code Agent Teams | Teammates que se mensajean entre sí + task list compartida; cmux los renderiza como panes (`cmux claude-teams`) |
| Sandbox aislado | agentbox, agenttier | Cada agente en Docker/K8s con red default-deny; para agentes con permisos peligrosos |
| Kanban lead-worker | agent-kanban | Backlog visual como contrato entre lead y workers |

## Advertencias validadas (Shipyard + experiencia propia)

1. Multi-agente NO aplica al ~95% de tareas — es para "¿qué se me escapa?" y
   backlogs paralelos, no para el día a día.
2. Los límites de uso llegan rápido; el prompt inicial imperfecto desperdicia
   horas de cómputo × N agentes (por eso: decisiones congeladas antes de
   construir).
3. Los orquestadores mismos son vibe-coded: asumir bugs y fallas de seguridad;
   permisos acotados.
4. Sin feedback loop (tests, smoke, ambientes efímeros) un fleet solo produce
   más código sin verificar, más rápido.

## Aplicación a este setup

- **Probar Agent Teams + cmux** (`cmux claude-teams`; env ya habilitado) — el
  único patrón grande aún no ejercitado aquí.
- **Tier 3 para el backlog documentado** (pendientes QTS: plist, labels) —
  especificables y verificables por CI, perfectos para agentes nocturnos.
- **Escalación a humano como infraestructura** (patrón 5dive): implementado en
  este repo — ver `just status` (colas de decisión) y la regla DECISION en la
  skill de cmux.
- **Sandboxing tiene un límite explícito**: los perfiles y clones reducen la
  superficie visible al modelo dentro de una Mac/un UID, pero dinero real exige
  además un broker de efectos o principal OS separado; S0 no contiene código
  hostil arbitrario del mismo UID.
