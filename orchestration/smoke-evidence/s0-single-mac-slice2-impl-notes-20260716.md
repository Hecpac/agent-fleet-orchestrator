# impl-notes: S0 Slice 2 — contratos, seguridad y publicación aislada

> Registro histórico del slice. El smoke integrador citado como pendiente se
> ejecutó después, pero otros cambios siguieron; el gate final permanece en
> `s0-single-mac-operational-truth-20260717.md`.

## Spec anclado

Cerrar la frontera contractual de la flota local de una sola Mac: token v2
universal y no amplificable, ACL de artefactos, endpoints MCP por instancia con
ciclo de vida verificable, estado CONTROL anclado por descriptores, writer en
clone Git aislado y publicación recuperable únicamente después de quiescencia.
No incluye federación multi-host ni sustituye la lane de smoke vivo ejecutada
por el integrador.

## Log de desviaciones

| # | Dónde | Desviación | Clasificación | Resolución conservadora | ¿Reversible? |
|---|---|---|---|---|---|
| 1 | Profundidad | La primera delegación Lead→especialista se contaba como hop especialista y hacía imposible un leaf hotfix con `max_depth=0` | REGISTRA-Y-SIGUE | Definir profundidad 0 para la raíz especialista y contar solo hops especialista→especialista; schema y regresión actualizados | sí |
| 2 | Presupuesto | El diseño inicial validaba y debitaba hijos individualmente, permitiendo consumo parcial de un batch | MEJORA | Preflight estático completo y reserva durable/atómica del batch bajo lock por token; retries reutilizan la misma asignación | sí |
| 3 | Identidad de batch | Dos requests con distinto idempotency key podían apuntar al mismo pane | MEJORA | Rechazar `recipient_instance` duplicado antes de token, intent, budget o launch; regresión de cero efectos | sí |
| 4 | Primera llamada MCP | El token prebound permitía una ventana anterior a `delegation_registered` sin exigir aceptación frontier | MEJORA | Exigir intent exacto, evento `dispatched` exacto y lease frontier físico vivo; no-events/preparing/missing/foreign/malformed/released/terminal fallan | sí |
| 5 | Superficie MCP | Los proveedores no tenían un cliente autenticado común para llamar Fleet Control | MEJORA | Proxy stdio controller-owned con nueve tools, caller IDs obligatorios, inyección de identidad y transporte AF_UNIX estricto por instancia | sí |
| 6 | OpenCode | OpenCode 2.0.x no enumera tools MCP dinámicas en `debug agent --pure` antes de arrancar el servidor | REGISTRA-Y-SIGUE | Verificar primero config MCP exacta; aceptar enumeración diferida vacía o las nueve tools completas, nunca un subconjunto | sí |
| 7 | Estado físico | Varias rutas confiaban en pathname/mode 0600 sin bloquear symlinks ancestro o reemplazo de raíz | MEJORA | `RootedFS` pinneado y operaciones no-follow para misión, tokens/locks, CAS, frontier, leases y manifest; solo se canonicaliza la raíz confiable | sí |
| 8 | Alias macOS | `/var` y `/private/var` representan la misma raíz física pero resolver el resultado completo seguiría symlinks descendientes | REGISTRA-Y-SIGUE | Derivar y comparar el root léxico del path registrado, canonicalizar solo ese root y leer el descendiente por descriptor | sí |
| 9 | Root de runs | El repo real usa `orchestration/runs` 0755 mientras los fixtures usaban 0700 | REGISTRA-Y-SIGUE | Aceptar raíz owner-bound no group/world-writable; mantener descendientes sensibles 0700/0600 exactos | sí |
| 10 | Writer Git | Un worktree compartía object store/refs con el target y podía publicar antes de teardown | MEJORA | Clone aislado sin remote/alternates/hooks, retiro del path del modelo al cerrar CMUX, intent durable, bundle + CAS y recuperación por checkpoints | sí |
| 11 | Ollama local | Los workers locales no tienen cliente autenticado para recuperar inputs CAS | DEUDA-DOCUMENTADA | Rechazar artifact inputs para roles locales; no exponer paths CAS ni simular una autoridad inexistente | sí |
| 12 | Canary Claude | El canary productivo usó OAuth de macOS Keychain mediante el runner saneado, expuso las nueve tools, pero ToolSearch agotó `--max-budget-usd 0.05`; el proveedor reportó costo real de USD 0.56816 sin llegar al envelope | REGISTRA-Y-SIGUE | No repetir inferencia pagada automáticamente; registrar discovery/connect como evidencia parcial y dejar la llamada live para aprobación explícita | sí |
| 13 | Sandbox | El sandbox impide `bind(AF_UNIX)` aunque la implementación sea local | REGISTRA-Y-SIGUE | Ejecutar suites AF_UNIX focales fuera del sandbox con aprobación; no reinterpretar fallos EPERM como bugs de producto | sí |
| 14 | Auth Codex | Copiar `auth.json` bifurca refresh tokens OAuth de un solo uso; keyring se liga al hash del `CODEX_HOME` y un home aleatorio no reutiliza la cuenta | MEJORA | Mantener un único target canónico y enlazarlo desde cada home efímero sólo tras validación descriptor-safe; Codex 0.144.5 preserva el enlace al guardar. Denegar exacto y `/**` en aliases léxicos/canónicos; excluir auth/CODEX_HOME del entorno de tools | sí |
| 15 | Permission profiles Codex | En 0.144.5 el deny de directorio sin glob no bloqueó lecturas aunque la documentación lo describe como subtree | REGISTRA-Y-SIGUE | Exigir canary real y emitir simultáneamente path exacto, `path/**` y auth file exacto, con `glob_scan_max_depth=64`; el canary bloqueó auth y runs | sí |
| 16 | Readiness MCP | La policy debug de OpenCode y el flag `required` nativo de Codex no probaban callability/socket para todos los proveedores | MEJORA | Ejecutar el mismo initialize/list/ping/socket preflight repo-owned para Codex, Claude y OpenCode antes de sus TUIs | sí |

## Detenido, esperando resolución

- La llamada Claude live que demuestre el envelope exacto requiere autorización
  explícita para otra inferencia pagada y un presupuesto realista. El intento
  actual conectó el MCP y listó las nueve tools, pero terminó por límite de costo.
- El smoke CMUX/provider end-to-end corresponde al integrador de Slice 2; este
  slice entrega comandos y no lo ejecuta por su cuenta.

## Tres para el intento #2

1. Separar canaries de descubrimiento local y de inferencia pagada; la segunda
   lane debe ser opt-in, presupuestada y conservar el recibo de costo.
2. Modelar cada frontera mutable como intent durable + CAS + reconciliación
   antes de añadir el primer efecto externo.
3. Probar siempre el root real del repositorio (0755 y alias `/var`) además de
   fixtures privados, porque exact-mode y canonicalización divergen en macOS.

## Estado

Implementación integrada; runner/auth cerró 28 pruebas (un skip Claude pagado),
con canaries reales de symlink/save, deny exact+glob y preflight AF_UNIX para los
tres proveedores. Sin commit ni smoke vivo desde este slice.
