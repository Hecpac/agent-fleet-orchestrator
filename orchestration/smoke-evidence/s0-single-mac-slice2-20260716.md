# S0 single-Mac — Slice 2 contracts/security evidence (2026-07-16)

> Historical slice evidence. A later disposable Slice 2 integration smoke did
> run successfully, but additional admission/phase/dialogue changes followed.
> Therefore neither this file nor that later run is the final-tree acceptance
> smoke. See `s0-single-mac-operational-truth-20260717.md`.

## Alcance

Evidencia de implementación para tokens/ACL/presupuesto, MCP autenticado por
instancia, paths CONTROL físicos, writer Git aislado y publicación recuperable.
Este archivo no declara smoke vivo PASS: por contrato, el integrador ejecuta esa
lane después del gate estático/unitario final.

## Suites ejecutadas

### Contratos, paths, leases, frontier y AF_UNIX

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_fleet_safe_paths \
  tests.test_fleet_mission_state \
  tests.test_fleet_delegation \
  tests.test_fleet_control \
  tests.test_fleet_artifacts \
  tests.test_fleet_leases \
  tests.test_fleet_frontier \
  tests.test_fleet_control_socket

Ran 131 tests in 8.132s
OK
```

La suite se ejecutó fuera del sandbox porque el sandbox rechaza
`bind(AF_UNIX)` con `EPERM`. Incluye token v2 universal, ACL de input/direct
child, budget concurrente/atómico, batch recipient único con cero efectos,
symlinks/modes/owner, frontier result/prompt/lease/ledger y endpoint por
instancia con lifecycle+lease exactos.

### Cierre de runner, auth Codex y preflight multi-provider

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_run_interactive_agent \
  tests.test_fleet_codex_home

Ran 28 tests in 23.069s
OK (skipped=1)
```

El único skip sigue siendo la inferencia Claude pagada. Codex usa un
`CODEX_HOME` efímero con un enlace simbólico descriptor-verificado al único
`auth.json` canónico (owner, 0600, regular, un solo hardlink); nunca copia el
refresh token OAuth de un solo uso. Una prueba con Codex 0.144.5 y credencial
sintética confirmó que `codex login --with-api-key` conserva el symlink y
actualiza el target canónico. `login status` se ejecuta sin `--strict-config`,
que esa versión rechaza; el gate estricto se aplica a la sesión/MCP compatible.

Codex, Claude y OpenCode ejecutan antes del TUI el mismo preflight repo-owned:
initialize, ping, lista exacta de nueve tools y round-trip al socket AF_UNIX.
Los perfiles Mission incluyen denies exactos y `/**` para aliases léxicos y
canónicos de auth, home efímero y runs. El canary real de `codex sandbox` 0.144.5
devolvió `auth_readable=no` y `runs_readable=no`; una denegación de directorio
sin glob no es evidencia suficiente en esa versión.

### Proxy MCP, providers y política del router

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_fleet_agent_mcp \
  tests.test_opencode_policy \
  tests.test_run_interactive_agent \
  tests.test_router_config \
  tests.test_fleet_threat_model

Ran 77 tests in 12.513s
OK (skipped=1)
```

El skip es únicamente la inferencia pagada Claude y requiere
`FLEET_RUN_PAID_CLAUDE_MCP_CANARY=1`. Antes del skip, el canary instalado de
Codex descubrió `fleet_control` y OpenCode pasó config/policy efectiva con
descubrimiento MCP dinámico diferido. Las integraciones AF_UNIX del proxy sí
llamaron el socket real y comprobaron grant de artefacto, sibling/base/terminal
deny, identidad exacta, framing y redacción.

### Alias físico macOS

```text
tests.test_fleet_frontier.FleetFrontierTests.test_frontier_result_accepts_trusted_var_root_alias ... ok
tests.test_fleet_frontier.FleetFrontierTests.test_frontier_result_rejects_symlinked_result_ancestor ... ok

Ran 2 tests
OK
```

La regresión usa `/var/...` como path lógico y `/private/var/...` como root
físico; solo canonicaliza la raíz confiable y nunca los descendientes.

## Red-team

El primer review del guard de publicación encontró:

1. tupla writer no cerrada contra roster/base global;
2. un `update-ref` final alimentado por `awk` después de mover el manifest;
3. binding parcial/no-canónico de `creation-request.json`;
4. soporte OID incoherente (regex 41–63 y publicación SHA-1 only);
5. ventana `SIGKILL` después de mover el manifest que podía dejar `closing`
   sin ruta de recuperación.

Esos hallazgos se cerraron con las regresiones focales de manifest/publicación,
clones aislados para readers y el gate de `tests.test_fleet_up`. El review también dejó como
fuera de Slice 2 la defensa contra un proceso arbitrario concurrente bajo el
mismo UID, coherente con el threat model local.

## Canarios instalados

| Provider | Resultado | Evidencia |
|---|---|---|
| Codex | PASS local | `codex mcp list --json` bajo el runner aislado incluyó `fleet_control`; save sintético preservó el symlink canónico y denies exact+`/**` bloquearon auth/runs |
| OpenCode | PASS local | config MCP exacta, policy exacta, TUI/CLI arrancó; OpenCode 2.0.x difiere la enumeración MCP hasta iniciar server, por lo que policy acepta 0 o 9 tools, nunca un subconjunto |
| Claude | PARTIAL | Runner de producción saneado (`env -i`, sin `ANTHROPIC_API_KEY`, `apiKeySource:none`) usó OAuth de macOS Keychain; init mostró `fleet_control: connected` y las 9 tools exactas. Claude llamó `ToolSearch`, pero terminó antes del envelope por `--max-budget-usd 0.05` |

El proveedor Claude reportó costo real **USD 0.56816** pese al cap configurado
de USD 0.05. No se repitió inferencia pagada. El canary quedó opt-in y el costo
inesperado se registra como hallazgo, no como PASS.

## Blockers externos / lanes pendientes

- Inferencia Claude live con envelope exacto: requiere autorización explícita
  de costo y presupuesto realista.
- Smoke CMUX/provider end-to-end: pendiente del integrador después de que el
  gate Git/manifest quede verde.
- Regulated/WORM externo continúa fuera de este S0 local.

## Comandos preparados para smoke vivo del integrador

Preparar un target descartable y baseline limpio:

```bash
slice2_smoke_root="$(mktemp -d /tmp/fleet-s2-live.XXXXXX)"
slice2_target="$slice2_smoke_root/target"
slice2_runs="$slice2_smoke_root/runs"
mkdir "$slice2_target"
git -C "$slice2_target" init
git -C "$slice2_target" config user.name "Fleet Smoke"
git -C "$slice2_target" config user.email "fleet-smoke@example.invalid"
git -C "$slice2_target" commit --allow-empty -m "baseline"
```

Compilar sin efectos:

```bash
python3 scripts/mission-run.py --runs-dir "$slice2_runs" dry \
  s2-live \
  "Create and commit slice2-smoke.txt. The Lead must dispatch Scout with can_delegate=true, allowed_capabilities=[challenge], remaining_budget=1; Scout must use fleet_control to dispatch Challenger as its direct child and wait for it. Then Lead must dispatch Builder and Verifier, relay only artifact IDs, and complete only after exact verification." \
  --workflow implementation \
  --target-repo "$slice2_target" \
  --risk low \
  --execution-profile native \
  --json
```

Ejecutar la misión, publicación post-quiescence y teardown:

```bash
python3 scripts/mission-run.py --runs-dir "$slice2_runs" run \
  s2-live \
  "Create and commit slice2-smoke.txt. The Lead must dispatch Scout with can_delegate=true, allowed_capabilities=[challenge], remaining_budget=1; Scout must use fleet_control to dispatch Challenger as its direct child and wait for it. Then Lead must dispatch Builder and Verifier, relay only artifact IDs, and complete only after exact verification." \
  --workflow implementation \
  --target-repo "$slice2_target" \
  --risk low \
  --execution-profile native \
  --timeout 7200 \
  --json \
  --teardown
```

Con el `mission_id` exacto devuelto, verificar estado, lineage, archive y ref:

```bash
python3 scripts/mission-run.py --runs-dir "$slice2_runs" show --mission-id <mission_id>
python3 scripts/fleet_control.py --runs-dir "$slice2_runs" --mission-id <mission_id> inspect-mission
python3 scripts/fleet_archive.py verify "$slice2_runs/missions/<mission_id>/archive" --repo "$slice2_target"
git -C "$slice2_target" rev-parse --verify refs/heads/fleet/s2-live/builder
git -C "$slice2_target" show refs/heads/fleet/s2-live/builder:slice2-smoke.txt
```

La lane Claude pagada, separada del smoke, no debe ejecutarse sin aprobación:

```bash
FLEET_RUN_PAID_CLAUDE_MCP_CANARY=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest -v \
  tests.test_run_interactive_agent.InteractiveAgentEnvironmentTests.test_installed_provider_clis_discover_the_specialist_mcp
```

## Estado

Evidencia de contracts/security y gate Git/red-team integrada. Después de este
documento se completó un smoke CMUX/writer/archive descartable con Mission
`412093af-8b7f-55b8-ab9f-6b35dfe866ea`, pero ese smoke precede los últimos
cambios y no se promueve a PASS final. No se hizo commit desde Slice 2.
