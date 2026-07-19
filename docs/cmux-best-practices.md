# cmux + fleet: beneficios y buenas prácticas

Destilado de la adopción real (2026-07-07/08): fleet heterogéneo verificado
(Claude lead, codex/gpt-5.5, MiniMax-M3, workers Ollama), auditoría de
unknown-unknowns sobre QTS-ARCHITECT, orquestación por eventos y carrera de
agentes. Cada práctica de abajo tiene un incidente o evidencia detrás.

Los nombres/modelos/costos de ese párrafo son evidencia histórica, no defaults
actuales. Hoy Codex es el Lead predeterminado, Claude/fallback es opt-in y el
alcance endurecido es una Mac/un UID; consulta `guia-uso-flota.md` para operar.

## Beneficios comprobados

1. **Acceso programático = velocidad agéntica.** Todo el ciclo (bootear
   equipos, despachar, leer, cancelar, notificar) se opera por CLI, así que un
   agente lo opera igual que un humano. Sin esto, tú eres el bottleneck.
2. **Visibilidad con identidad.** Panes nombrados por rol, colores por
   workspace, estados en sidebar. Un agente que ves es un agente que puedes
   interrumpir, corregir y mejorar.
3. **Heterogeneidad de modelos.** Distintos proveedores en una pantalla. La
   auditoría uu demostró el valor: Codex y MiniMax corroboraron 2 P0 y cada uno
   encontró otros 2 que el otro no vio. El router ahora registra y valida esa
   diversidad de identidad; la coincidencia sigue sin ser prueba de verdad ni
   de errores estadísticamente independientes.
4. **Recuperación.** Los hooks de agentes guardan sesiones; un pane cerrado por
   accidente se restauró con `claude --resume <id>` sin perder contexto.
5. **Orquestación por eventos.** `fleet-wait` duerme hasta `agent.hook.Stop` /
   notificación del worker. Cero polling, cero tokens quemados en re-leer
   pantallas.
6. **Costo.** Una auditoría histórica costó < $1 (MiniMax $0.83), pero eso no
   constituye un límite. Un canary Claude posterior reportó USD 0.56816 pese a
   un cap CLI de USD 0.05. Ollama local no incurre costo de API; toda inferencia
   frontier se habilita deliberadamente y conserva su evidencia de uso cuando
   el proveedor la ofrece.
7. **Es solo un terminal.** Cualquier agente CLI funciona; no hay lock-in;
   open source (GPL). tmux cubre lo mismo en Linux/Windows.

## Buenas prácticas (cada una con su cicatriz)

1. **Problema primero, herramienta después.** No adoptes cmux por hype;
   adóptalo cuando el arranque manual de equipos y la falta de acceso
   programático te duelan de verdad.
2. **Sube por la escalera.** Manual (`send`/`read-screen` a mano) → un
   orquestador operando cmux → fleet con manifest → patrones (eventos, race).
   Cada nivel valida el anterior.
3. **Panes para VER, archivos para DATOS, eventos para SEÑALES.** Las
   pantallas truncan (panes angostos, TUIs que redibujan); los títulos de
   notificaciones van REDACTADOS en el event stream; el polling quema tokens.
   Findings grandes → archivo; completado → evento; supervisión → pane.
4. **Direccionamiento explícito.** El manifest (rol → surface) es la fuente de
   verdad. Nunca prompteés un surface que no identificaste en `tree` o el
   manifest.
5. **Verifica refs antes de actuar sobre ellas.** Incidente real: un
   `close-surface` con una ref obsoleta resolvió a OTRO pane y cerró el lead
   vivo. Las refs cortas son posicionales. `tree` primero, siempre.
6. **Contratos de salida + verificación frontier.** Los workers responden
   STATUS/SUMMARY/EVIDENCE/RISKS/NEXT_ACTION. Los modelos chicos marcan DONE
   con optimismo (gemma3 marcó DONE sin implementar nada) — el lead parsea y
   verifica, no confía.
7. **Presupuesto de recursos codificado.** En 16 GB: 1 modelo local 7B+ máximo,
   3 workers locales en paralelo. Mission añade admisión global durable; el
   presupuesto local del router sigue siendo el gate standalone heredado.
8. **Un workspace = una misión; teardown al cerrar.** `just fleet-down` al
   mergear/abandonar. Workspaces zombis acumulan confusión y sesiones idle.
9. **Higiene de secretos.** Las notificaciones al teléfono pasan por servidores
   de cmux (activa Hide content). Los shells interactivos pueden imprimir env
   con llaves (incidente real: el check de llaves dumpeó el environment; quedó
   silenciado). Nunca leas ~/.zshrc crudo en un transcript.
10. **Ganador ≠ correcto.** La carrera (`just race`) da velocidad, no verdad.
    Verifica la respuesta ganadora antes de actuar — especialmente en hotfixes.
11. **Codifica cada lección en la skill.** `.agents/skills/cmux/SKILL.md` y
    `.claude/skills/cmux/SKILL.md` son copias sincronizadas de la memoria
    operativa: cada incidente se vuelve regla dura que el próximo orquestador
    hereda gratis.
12. **Revisa tus colas de decisión.** Los agentes se detienen (correctamente)
    en decisiones de negocio y pueden esperar horas sin que lo notes (dos
    sesiones esperaron ~1h una elección). Los eventos/notificaciones al humano
    son parte del sistema, no un extra.
13. **Delegación proporcional.** Fleet para "¿qué se me escapa?" (auditorías,
    diagnóstico, revisión pre-merge); un solo agente para "haz esto" acotado.
    Un lead que decide trabajar solo en una tarea chica está decidiendo bien.
14. **UUID antes de cada efecto.** `fleet-send`, `fleet-dispatch`, `fleet-wait`
    y `fleet-down` comparan el UUID durable del manifest contra el tree actual.
    Una discrepancia falla cerrada.
15. **Fase antes de prompt.** La flota nace en CONTROL y avanza con evidencia;
    los wrappers soportados aceptan solo CONTROL o la fase activa. Al avanzar,
    las fases anteriores quedan congeladas.
16. **Entorno por allowlist.** Los agentes no heredan todo el shell. Solo se
    conservan variables base, contexto CMUX y secretos declarados por el rol.
17. **Finalización tipada.** Workers locales validan `STATUS`; `BLOCKED`,
    `FAILED` y contrato ausente retornan no-cero. Una carrera entrega un
    candidato, nunca una verdad verificada.
18. **Teardown reconciliado.** Rechaza leases activos, confirma que el UUID ya
    desapareció y archiva manifest/state/ledger en vez de borrarlos.
19. **Autonomía proporcional.** `just dan` entrega la misión completa al lead y
    abre todas las fases del roster; no exige `advance` ni aprobación rutinaria.
    El lead escala únicamente efectos externos, riesgo alto o decisiones de
    negocio ausentes. `fleet_dialogue` conserva los gates para esos casos.
20. **Roster disponible ≠ roster usado.** El lead decide qué panes aportan valor.
    Arrancar capacidad visible no obliga a gastar tokens en cada agente.
21. **Colaboración por resultados rastreados.** Comparte `result_file` y `run_id`
    en un turno posterior; no inyectes un segundo prompt crudo en un agente con
    run activo, porque destruye la atribución de completion.

## Antipatrones

- Token-maxing: 20 agentes en loop sin mirar qué hacen. Si no supervisas ni
  cosechas patrones, el fleet es teatro caro.
- Scraping de pantallas como canal de datos entre agentes.
- Sleeps calibrados a mano en lugar de eventos.
- Un workspace eterno con misiones mezcladas.
- Confiar el merge/deploy al fleet sin gate humano.
- Convertir al humano o a CONTROL en un stepper de acciones rutinarias.
- Ejecutar el pipeline completo cuando el lead ya tiene evidencia suficiente.
