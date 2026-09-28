# Etapa 1 — Cerrar el ciclo de una Mission: plan de implementación

> **Fecha:** 2026-09-28 · **Estado:** aprobado con la especificación; S1 a S4 implementados ·
> **Especificación (leer primero):**
> [`docs/superpowers/specs/2026-09-28-etapa1-cierre-mission-design.md`](../specs/2026-09-28-etapa1-cierre-mission-design.md) ·
> **Base:** `main` en `75baa80`.

**Objetivo:** reparación acotada, entrega local verificada y cierre automático
dentro de una misma Mission MINIMAL con el contrato funcional
`python-stats-rpc-v1`, según E1–E20 y REQ-001–REQ-016 de la especificación.

**Límites de este plan:**

- Todo se demuestra offline, con `FakeBackend`/`MinimalFixture` y un runner
  funcional simulado. Sin proveedores, sin binarios reales de Herdr o Codex y con
  gasto cero (REQ-012).
- El carril Docker (`FLEET_FUNCTIONAL_DOCKER_TESTS=1`) y la validación con modelos
  requieren autorizaciones separadas y quedan fuera.
- No incluye la reparación del CI (`ci/portable-fixes`), que es un cambio
  independiente.
- Cada slice es un commit propio cuando se autorice; ninguno cambia el
  comportamiento de las Missions sin `repair_policy` (REQ-010).

## Módulos

| Módulo | Estado | Responsabilidad |
| --- | --- | --- |
| `scripts/fleet_herdr_repair_policy.py` | Nuevo | Esquema `fleet.repair-policy.v1` (`max_attempts`, `closure_policy`, `delivery_root`), validación y congelación en la creación |
| `scripts/fleet_attempt_loop.py` | Nuevo | Mecánica común de intentos: límite, feedback `checks_rejected`, agotamiento. Extraída de Owner Cycle |
| `scripts/fleet_herdr_repair.py` | Nuevo | Apertura, evaluación y cierre de intentos por ordinal, reutilización de receipts de árboles repetidos, feedback acotado y agotamiento |
| `scripts/fleet_herdr_delivery.py` | Nuevo | Preparación, publicación sin reemplazo, relectura, colisión y reconciliación |
| `scripts/fleet_herdr_owner_cycle.py` | Cambio | Consume `fleet_attempt_loop` sin cambiar su comportamiento |
| `scripts/fleet_mission_state.py` | Cambio | Eventos de intento y de entrega; `repair_policy` en el estado |
| `scripts/fleet_functional.py` | Cambio | Guard por ordinal y reutilización de receipt con `unchanged_revision` |
| `scripts/fleet_herdr_mission.py` | Cambio | Bucle de reparación de MINIMAL, entrega, cierre y fases de cancelación y plazo |
| `scripts/fleet_herdr_control.py` | Cambio | La confirmación de pausa o cancelación recorre todos los intentos y la entrega |
| `scripts/fleet_herdr_archive.py` | Cambio | Esquema v9 y regla de degradación generalizada |
| `scripts/mission-run.py` | Cambio | Opción `--repair-policy` y admisión previa al lanzamiento |

## Slices

### S1 — Contrato de reparación y admisión

**Cambios:** `fleet_herdr_repair_policy.py`, `mission-run.py`,
`fleet_mission_state.py` (campo congelado).

- [x] Validar el esquema: campos exactos, `max_attempts` entre 1 y el máximo
      del contrato, `closure_policy` en `{automatic}`, `delivery_root` absoluto.
- [x] Rechazar al crear la Mission, antes de lanzar agentes: ausencia de
      contrato de scope (E17), perfil distinto de MINIMAL, ausencia de contrato
      funcional, `token_budget > 0` y una raíz de entrega que, tras resolver
      enlaces, contenga el directorio de runs o esté dentro de él (E8).
- [x] Congelar la política y su digest en el evento de creación; reanudar nunca
      la relee del disco.
- [x] El plazo total no forma parte de la política: es el `deadline_at` que la
      admisión ya congela al crear la Mission (E14), así que hay una sola fuente.
- [x] El driver compara la política de las opciones con el pin del ledger y
      bloquea antes de cualquier efecto si difiere o falta.
- [x] Mientras no exista el bucle de reparación, el driver bloquea toda Mission
      con `repair_policy` antes de preparar el candidato o lanzar agentes, en
      lugar de aplicarle en silencio el camino sin reparación.

**Comprobaciones:** `tests/test_fleet_herdr_repair_policy.py` (nuevo): SCN-015 y
SCN-022, con casos negativos por campo. Los tests existentes de `mission-run` y
`test_fleet_herdr_policy_admission` sin cambios (CHK-010).

### S2 — Mecánica común de intentos

**Cambios:** `fleet_attempt_loop.py`, `fleet_herdr_owner_cycle.py`.

- [x] Extraer el cálculo de ordinal, la comprobación de `max_attempts`, el
      feedback `checks_rejected` y la causa de agotamiento
      (`fleet_herdr_owner_cycle.py:944-946` y `_repair_feedback`).
- [x] Owner Cycle usa el módulo común; su journal, su sello y su espacio no
      cambian (E4).

**Comprobaciones:** `test_fleet_herdr_owner_cycle`,
`test_fleet_herdr_owner_transport` y la conformidad y el replay de S7
(`scripts/fleet_herdr_owner_conformance.py`) con resultados idénticos a
`75baa80` (CHK-010).

### S3 — Intentos en el ledger y check funcional por ordinal

**Cambios:** `fleet_mission_state.py`, `fleet_functional.py`,
`fleet_herdr_repair.py` (nuevo), `fleet_attempt_loop.py`.

- [x] Eventos `repair_attempt_opened` y `repair_attempt_settled` con ordinal,
      `tree_sha`, `attempt_id` funcional, resultado y feedback en CAS (E3). El
      ledger exige orden, un intento anterior fallido, Mission en ejecución, sin
      cancelación solicitada y antes de `deadline_at`.
- [x] El guard de `fleet_functional.py:179-181` se aplica dentro de cada ordinal;
      un ordinal nuevo admite un árbol distinto o reutiliza el receipt de un
      árbol ya evaluado, sin ejecución física (E18, §3.2). Las claves de
      idempotencia del check llevan el ordinal a partir del segundo intento;
      las Missions sin política conservan las históricas.
- [x] Feedback solo desde el receipt, con límite de bytes y campos fijos (E6).
- [x] Agotamiento: terminal `failed` con `repair_attempts_exhausted` (E7).

**Comprobaciones:** `tests/test_fleet_functional.py` (clase pura
`FunctionalContractTests`) y `tests/test_fleet_herdr_repair_attempts.py`
(nuevo): SCN-003, SCN-004, SCN-023 y SCN-024 (CHK-003, CHK-004, CHK-014).

### S4 — Bucle de reparación en el driver MINIMAL

**Cambios:** `fleet_herdr_mission.py`, `fleet_herdr_control.py`.

- [x] Sustituir el terminal de `fleet_herdr_mission.py:1242-1247` por un nuevo
      intento del Worker solo cuando hay `repair_policy`, el check es `failed` y
      quedan intentos y plazo (E2, E5). Sin política, el comportamiento actual
      se conserva.
- [x] Cada intento es una admisión nueva con clave de idempotencia por ordinal;
      reanudar reconcilia el mismo turno y el mismo intento funcional (E3). El
      turno de reparación separa su clave (`build-repair-<n>`) de su contrato de
      rol (`build`), y cada ordinal congela su revisión en un archivo propio de
      una sola escritura.
- [x] No abrir un intento con cancelación solicitada o plazo vencido (SCN-005,
      SCN-009).
- [x] La confirmación de control exige que todos los intentos tengan resultado:
      solo el intento abierto puede carecer de él, y ese caso ya bloquea.
- [x] Sustituir el bloqueo de S1 por la ejecución del bucle.
- [x] Una política de reparación fija al crear la Mission un crédito de
      delegación por intento: créditos del workflow × `max_attempts`.
- [x] Hasta S6 y S7, un intento aceptado deja la Mission en ejecución con la
      causa «delivery and closure are not available yet»: el archivo actual
      exige un único turno y todavía no hay entrega.

**Comprobaciones:** `tests/test_fleet_herdr_repair_loop.py` (nuevo), sobre
`MinimalFixture` de `test_fleet_herdr_orchestration_closure.py`: SCN-001,
SCN-002 (sin entrega todavía), SCN-005 a SCN-009 y SCN-019 (CHK-001, CHK-002,
CHK-004 a CHK-006, CHK-012). `test_fleet_herdr_orchestration_closure` y
`test_fleet_herdr_mission` sin cambios.

### S5 — Módulo de entrega

**Cambios:** `fleet_herdr_delivery.py`.

- [ ] Ruta `<raíz>/<mission_id>/<ordinal>-<tree_sha>/` y preparación
      `.staging-<ordinal>-<tree_sha>` hermana (E9, §3.3).
- [ ] Escribir el árbol desde CAS, sincronizar archivos y directorio, publicar con
      `renameat2(RENAME_NOREPLACE)` o `renamex_np(RENAME_EXCL)` mediante
      `ctypes` y sincronizar el padre. Sin primitivo:
      `delivery_primitive_unavailable` (E19).
- [ ] Relectura con hashes por archivo y conjunto exacto de archivos (E10).
- [ ] Colisión, colisión concurrente y directorio vacío preexistente (E11, §3.3).
- [ ] Reconciliación tras reinicio (E12).
- [ ] Volver a resolver la raíz de entrega en el momento de entregar: S1 la
      admite con `resolve(strict=False)` al crear la Mission, así que un enlace
      simbólico creado después solo se detecta en la entrega.

**Comprobaciones:** `tests/test_fleet_herdr_delivery.py` (nuevo), puro y válido en
macOS y Linux: SCN-010 a SCN-014 y SCN-025 a SCN-028 (CHK-007 a CHK-009,
CHK-015). La colisión concurrente se inyecta con un hook entre la comprobación y
el rename.

### S6 — Entrega, cierre y fases en el driver

**Cambios:** `fleet_herdr_mission.py`, `fleet_mission_state.py`.

- [ ] Eventos `delivery_started` y `delivery_finished`; recibo en CAS.
- [ ] Cierre automático solo con requisitos cumplidos, recibo idéntico al árbol
      aceptado y registrado antes de `deadline_at`, y sin cancelación solicitada
      (E13, §3.4).
- [ ] Matriz de fases P0–P4 para cancelación y plazo; `indeterminate` si no puede
      establecerse la publicación (E20).

**Comprobaciones:** ampliar `tests/test_fleet_herdr_repair_loop.py`: SCN-002
completo, SCN-017, SCN-018, SCN-029 y SCN-030 (CHK-002, CHK-011, CHK-016).

### S7 — Archivo v9 y verificación

**Cambios:** `fleet_herdr_archive.py`, `fleet_herdr_profile.py` si hace falta
declarar la versión.

- [ ] v9 = capa de scope de v8 más `attempts/` y `delivery/receipt.json` (§3.1).
- [ ] Generalizar `fleet_herdr_archive.py:561`: con scope, v8 o v9; con
      `repair_policy`, v9; rechazar cualquier degradación o mezcla.
- [ ] La verificación offline reevalúa intentos, receipts y recibo de entrega
      desde el archivo y el ledger, sin releer la raíz de entrega.

**Comprobaciones:** `test_fleet_herdr_archive*`, `test_fleet_herdr_scope` y los
lectores históricos sin cambios; fixtures v9 manipulados nuevos: SCN-016,
SCN-020 y SCN-021 (CHK-010, CHK-013).

### S8 — Recorrido completo offline y documentación

- [ ] Test de extremo a extremo con el plan SDD
      [`examples/sdd/stats-repair-delivery.json`](../../../examples/sdd/stats-repair-delivery.json)
      vinculado: intento 1 `failed`, intento 2 `passed`, entrega en una raíz
      temporal, relectura y `succeeded`.
- [ ] Actualizar `docs/herdr-functional-checks.md` («A corrected check needs a new
      explicitly created Mission» deja de ser cierto con `repair_policy`),
      `docs/herdr-mission-control.md` y el estado de S8 en
      `docs/architecture-refactor-2026-09-23.md`.
- [ ] Marcar D1, D4 y D5 como decididas en §7 de la constitución, en el mismo
      commit que apruebe la especificación.

**Comprobaciones:** suite completa con `./scripts/check-ci.sh` en un checkout
limpio de Linux, además de macOS. Validar el plan SDD con
`python3 -B scripts/fleet_sdd_contract.py examples/sdd/stats-repair-delivery.json`.

## Comprobación en cada slice

```sh
python3 -B -m unittest <módulos del slice y los existentes afectados>
git diff --check
```

Al cerrar S4, S7 y S8, además, la suite completa (`./scripts/check-ci.sh`). Un
test existente que cambie de resultado bloquea el slice hasta explicarlo; no se
convierten errores en skips ni se debilitan aserciones.

## Trazabilidad

| CHK | Slice |
| --- | --- |
| CHK-001, CHK-002 (sin entrega), CHK-004–CHK-006, CHK-012 | S4 |
| CHK-003, CHK-014 | S3 |
| CHK-007–CHK-009, CHK-015 | S5 (y S1 para la raíz dentro de los runs) |
| CHK-002 (completo), CHK-011, CHK-016 | S6 |
| CHK-010 | S1, S2, S4, S7 |
| CHK-013 | S1 (scope obligatorio), S7 |

## Después de este plan

- Ejecutar el check real en Docker con un Worker simulado: requiere autorización.
- Validar con modelos: requiere autorización, versiones fijadas y límite de gasto.
