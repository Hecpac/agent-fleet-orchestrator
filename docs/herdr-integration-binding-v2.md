# INTEGRATION_BINDING: primera etapa local, sin activación

Fecha: 2026-09-07. Este documento describe código local autorizado después de la
auditoría de viabilidad. **La implementación coordinada todavía está incompleta.**
No hay una operación real Fleet → Herdr → Codex atestada.

| Capa | Estado | Alcance |
| --- | --- | --- |
| POLICY_CONSTRUCTION | VERIFIED | Evidencia de entrada, no repetida |
| SANDBOX_CONFORMANCE | PASS | Evidencia previa de Codex 0.153.0 / Seatbelt, no repetida |
| INTEGRATION_BINDING | NOT_VERIFIED | Faltan transporte autenticado, cobertura completa y operación E2E |

## Código entregado

`scripts/fleet_herdr_binding_v2.py` conserva por separado el receptor v1. Congela
los bytes de la política solicitada, su versión, router/workflow, candidate físico,
binario/version Codex esperados, temporales, raíces protegidas, challenge e intento.
La lectura posterior verifica esos bytes contra un pin independiente; no llama al
constructor de permisos vigente. `run_id` se incorpora en pre-exec, porque todavía
no existe cuando se arranca un miembro del roster.

Las fases tienen campos y enlaces distintos:

| Registro | Productor previsto | Datos que le corresponden |
| --- | --- | --- |
| frozen | CONTROL | Política solicitada e identidad del intento antes del launch |
| A / launch | Herdr padre | Proceso Codex, candidate, imagen/version y argumentos/entorno de launch |
| B / pre_exec | Codex padre | run, secuencia, operación, política efectiva, perfil Seatbelt y parámetros, argumentos/entorno finales, compromiso del input y política de FDs |
| ACK | CONTROL | Hash exacto de B, operación y secuencia autorizadas |
| spawn | Codex padre | Hashes de B/ACK e identidad PID/birth posterior al spawn |
| C / effects | Observador externo | Snapshots anterior/posterior, efectos y evidencia de quiescencia |
| D / envelope | CONTROL | Enlaces CAS de todas las fases y referencia al ledger |

El SHA-256 de B compromete el registro completo de la operación, incluidos sus
enlaces. B no contiene un PID futuro ni afirma efectos que aún no ocurrieron.
`before_sha256` identifica exclusivamente el snapshot anterior de C. La identidad
`pid + birth_identity` no se deduce de un PID reutilizable ni de stdout.

`quarantine_chain()` guarda objetos CAS y un envelope **sin autoridad**. Comprueba
que existan el antecedente del ledger y los tres artefactos del observador. No
añade eventos al ledger, no emite ACK y no cambia aceptación/finalización.
Los pins y `observations` son inputs independientes del controlador, no datos
extraídos del registro presentado. Una cadena autoconsistente fabricada devuelve
siempre `NOT_VERIFIED`. Este receptor offline tampoco impide volver a presentar
la misma cadena dentro de un intento activo: el journal de consumo sigue pendiente.

## Puntos de extensión en las fuentes fijadas

Copias locales, fuera de la instalación y dentro del directorio ignorado
`outputs/integration-binding-rxmp8__u/`:

- Herdr v0.8.2: `9eb521456ac0d19d3ab3d9d7cea3cca10baa8a4c`.
- Codex rust-v0.153.0: `41e22fee981a63b3698df7ed36bad393cda24715`.

Herdr usa su copia vendorizada de `portable-pty`. El nuevo callback en
`vendor/portable-pty/src/unix.rs` recibe el `std::process::Command` después de
`CommandBuilder::as_command()`, de la resolución de programa/cwd, de añadir SHELL
y de configurar stdio/pre-exec, inmediatamente antes de `spawn`. Recibir el
Command permite observar el entorno final; observar el builder anterior no basta.

Codex tiene wrappers que consumen el Command nativo final en `utils/pty` para
pipes, PTY Unix con FDs preservados y `core/src/spawn.rs` para shell. El callback
recibe los argumentos, cwd y entorno finales por los getters del mismo objeto.
En Seatbelt incluye el argv ya materializado con `-p` y cada definición `-D`;
el futuro emisor debe comprometer ambos sin regenerarlos desde flags de Fleet.
Después del callback el wrapper llama a spawn sin cambiar argv, cwd o entorno.
Cuando hay un observador instalado, el backend PTY opaco se rechaza; no se toma
su CommandBuilder como sustituto del Command que portable-pty materializaría.

**Estos son puntos de extensión internos, no un modo de atestación operativo.**
Ningún startup instala el callback. No hay nueva opción CLI/config/env que lo
active. La instalación interna sólo se admite una vez y no expone capacidad al
modelo. Su callback aún debe implementar autenticación, redacción, deadline,
ACK de un solo uso y correlación con el proceso. Los getters no prueban efectos
de closures `pre_exec`, stdio, FDs implícitos ni qué inode acabó ejecutándose.

La ruta actual `agent.start` sigue enviando el comando al shell del pane. El hook
vendorizado no convierte ese shell en un launch directo de Codex. Faltan el API
de launch directo, bootstrap privado y correlación PID/birth/image. No debe
activarse el observador sobre esa ruta y llamar al resultado `HERDR_LAUNCH_BINDING`.

## Bloqueos de verificación

El host tiene Rust **1.93.1**. Las fuentes fijan **1.95.0** (Codex) y **1.96.1**
(Herdr). No está `cargo-nextest`. Los comandos se ejecutaron con
`RUSTUP_TOOLCHAIN=stable CARGO_NET_OFFLINE=true` para impedir descargas de toolchain
y dependencias:

```sh
# Dentro de codex/codex-rs:
RUSTUP_TOOLCHAIN=stable CARGO_NET_OFFLINE=true just test -p codex-utils-pty --locked
RUSTUP_TOOLCHAIN=stable CARGO_NET_OFFLINE=true cargo check --locked --offline -p codex-utils-pty
# Dentro de herdr:
RUSTUP_TOOLCHAIN=stable CARGO_NET_OFFLINE=true just test-one native_spawn
RUSTUP_TOOLCHAIN=stable CARGO_NET_OFFLINE=true cargo check --locked --offline -p portable-pty
```

Resultados: ambos `just test` terminan con `no such command: nextest`; Codex check
no puede cargar el commit fijado de `tungstenite-rs` en modo offline; Herdr check
necesita `clap 4.6.1` y sólo encuentra `4.5.60`. Ninguno compiló el runtime.
No se alteraron toolchains ni dependencias fijadas para eludir estos bloqueos.

Se aplicó `rustfmt` local a los archivos Rust modificados. El formatter completo
`just fmt` de Codex ejecuta también DotSlash/uv y puede descargar herramientas;
no se ejecutó bajo la prohibición de descargas. El rustfmt stable advierte que
`imports_granularity=Item` requiere nightly. Esto no es una verificación completa
del formato oficial ni del build.

## Pruebas disponibles

Desde Agent Fleet, usando una raíz temporal propia fuera de `/tmp`:

```sh
FLEET_TEST_CONTROL_PARENT="$PWD/outputs/integration-binding-rxmp8__u" \
  python3 -B -m unittest tests.test_fleet_herdr_binding tests.test_fleet_herdr_binding_v2
git diff --check
```

41 pruebas del receptor pasan. Incluyen bytes manipulados, intentos/generaciones/
misiones/runs diferentes, recovery, candidate con symlink o inode reemplazado,
binario/version diferentes, raíces extra, entorno diferente bajo igual redacción,
ACK de otra operación, perfil/input/FD distintos, fases ausentes, datos futuros
en B, falsificación autoconsistente, quiescencia falsa, snapshots ausentes y
stdout `status=accepted` sin autoridad. Son fixtures sintéticas con CAS/ledger
temporales; no constituyen un backend alternativo.

`native_gate_probe.rs`, en el directorio de trabajo, importa el módulo Rust real
`native_spawn_gate.rs` y se compila con el rustc local, sin dependencias. Sus
cuatro procesos aislados comprueban autorización/denegación de un shell mínimo,
rechazo del backend opaco y rechazo de reinstalar el callback. En autorización
escribe un marcador sintético y captura stdout; en denegación no aparece el
marcador. **No es Codex, Herdr ni un canary del Worker.** No comprueba Tokio, el
crate completo, Seatbelt, proveedor, canal, PID/birth ni quiescencia del Worker.

## Trabajo restante indispensable

1. Proveer localmente los toolchains y dependencias fijados, además de nextest,
   para compilar y ejecutar los tests oficiales de ambos repositorios. Su
   instalación o descarga requiere autorización distinta a la vigente.
2. Herdr: un `agent.start` opt-in que lance el argv nativo directamente en un pane
   nuevo, resuelva el binario fijado y entregue un canal privado sin pasar por un
   shell. Capturar A con el proceso real y su birth/image. Recovery invalida el
   intento/canal previo; restore/reuse no recuperan su capacidad de autorización.
3. Codex: bootstrap trusted y emisor B que comprometa bytes finales, argumentos
   y entorno mediante redacción + HMAC con claves sólo en el padre, perfil
   Seatbelt/definiciones y payload de la operación. El channel debe ser CLOEXEC
   y quedar fuera de los FDs preservados por herramientas. Timeout, mismatch,
   canal ausente o cobertura desconocida deben impedir el efecto cuando se
   requiera binding.
4. Cubrir o rechazar explícitamente `apply_patch`/helpers FS (incluido su payload
   stdin), `write_stdin`, exec-server remoto, zsh escalation/fallback, herramientas
   dinámicas/MCP y toda ruta con efectos que no llega a los wrappers actuales.
   No anunciar una propiedad global de fail-closed a partir de estos wrappers.
5. CONTROL: journal durable de ACK/consumo antes del efecto, secuencias y
   challenges frescos, deadline/recovery, A/B/spawn/C autenticados, anchor D al
   ledger y política de rechazo ante crash/ACK ambiguo. El CAS por sí solo no
   autentica al productor ni impide replay de una operación activa.
6. Conductor Responses-compatible en loopback, sin auth y con reintentos
   limitados, que ingrese por el Worker interactivo real y emita una tool call
   determinista. Debe negar/observar el tráfico ajeno al loopback y comprobar
   también solicitudes auxiliares del startup. No reemplazarlo por exec-server
   standalone o `codex sandbox`. Compatibilidad TUI/protocolo/startup aún no probada.
7. Observador externo: snapshots antes/después y probes de candidate/temporal,
   rutas absoluta/traversal/symlink, network bind, autoridad de stdout, Git,
   CONTROL/CAS/ledger/runs y procesos descendientes. Ninguno de esos efectos del
   Worker real fue ejecutado o probado en esta etapa.

## Threat model y límites

CONTROL, Herdr/Codex fijados y kernel son confiables. Modelo, prompt, candidate,
stdout y tool children no lo son. No hay claves/capacidades ni valores reales de
entorno persistidos por los hooks de esta etapa: no se activaron los emisores.
HMAC y redacción sólo tienen valor de compromiso; no sustituyen autenticación.

El ataque por otro proceso malicioso del mismo usuario sigue **BLOCKED** sin una
frontera OS adicional validada. Peer UID, permisos 0600 o una clave local no bastan.
No se configura aquí un servicio de otro UID, entitlement, Endpoint Security,
VM u otra primitiva de aislamiento.

Tampoco se cierran lectura externa ni acceso a HOME/CODEX_HOME. La política
histórica permite los temporales declarados: `/tmp` suele materializarse como
`/private/tmp` y `TMPDIR` puede ampliar las escrituras. Un temporal propio no
implica exclusividad sobre todo `/tmp`. Setsid/killpg no prueban por sí solos la
terminación de descendientes escapados ni de procesos distintos del mismo UID.

Rollback: los binarios instalados y la ruta activa no cambiaron. El receptor v1
y los 305 archivos de entrada se mantienen; esta etapa añade tres archivos Fleet
y conserva los patches upstream en el directorio ignorado. No hay commits,
pushes, deploys, proveedores, credenciales, red externa ni cambios globales de
configuración o permisos en esta implementación. Las fuentes se copiaron desde
los clones locales ya obtenidos durante la auditoría anterior.
