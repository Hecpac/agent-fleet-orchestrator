# Verificación del entorno nativo — 2026-09-10

La dependencia de compilación está resuelta. **La mediación nativa completa
continúa incompleta y no se habilita el arranque de una flota.**

Se instalaron Rust 1.95.0 y 1.96.1 en el rustup habitual, con `rustfmt` y
`clippy`; 1.95.0 incluye además `rust-src`. El toolchain predeterminado sigue
siendo `stable`. Rustup informó una actualización propia durante la instalación.
`cargo fetch --locked` terminó correctamente en ambos checkouts sin modificar
sus lockfiles.

También se localizaron las herramientas ya preparadas en
`outputs/integration-binding-rxmp8__u/tooling-stage`: nextest 0.9.143, Zig 0.15.2
y toolchains aislados. Se reutilizaron para las pruebas oficiales. Su ausencia
en el PATH habitual no significaba que faltaran del equipo; no requieren otra
instalación. El wrapper de Zig y SDK de aquella etapa permanece intacto.

## Evidencia actual

| Comprobación | Resultado | Alcance |
| --- | --- | --- |
| Codex `cargo +1.95.0 check --locked --offline -p codex-utils-pty -p codex-sandboxing` | PASS | Compilación de los dos crates y sus dependencias |
| Herdr `cargo +1.96.1 check --locked --offline -p portable-pty` | PASS | Backend vendorizado |
| Codex `just test -p codex-utils-pty -p codex-sandboxing --locked --offline --retries 0` | 147/147 PASS, cero omitidos | Canal autenticado, contexto, PTY y pruebas Seatbelt de los crates |
| Herdr `just test-one native_spawn_gate_observes_and_denies_through_real_herdr_backend` | 1 PASS, 3329 filtrados | Denegación sin marcador y control positivo por el backend Unix real |
| Fleet `tests.test_fleet_herdr_effects`, `tests.test_fleet_herdr_native`, `tests.test_fleet_native_spawn_channel` | 30/30 PASS | Admisión cerrada, canal y vínculo de observación |
| Codex `just fmt` | PASS | Formatter oficial con red denegada y herramientas existentes |
| `git diff --check` | PASS | Fleet y ambos checkouts nativos |

La primera ejecución de Codex tuvo 145 PASS y dos fallas en expectativas de
índices de exclusión. El código de permisos produce `.git`, `.agents`, `.codex`;
dos fixtures esperaban otro orden. El único cambio nuevo en las fuentes nativas
es de cinco líneas en `codex-rs/sandboxing/src/seatbelt_tests.rs`. Conserva las
comparaciones exactas de parámetros y la prueba que intenta escribir
`.codex/config.toml` bajo Seatbelt. No se modificaron reglas de permisos para
obtener PASS. El log fallido permanece disponible junto al corregido.

Los snapshots de contenido confirman que los demás archivos previos de ambos
checkouts permanecen intactos. Los lockfiles conservan estos SHA-256:

- Codex: `0fa17f9f72dbbb3c9581e69ddc111a396f10f57eaa7214f8ec592c716469525e`.
- Herdr: `a827ec0ed9dd4593ad9328fe9447edbbcd7eb5e002274d3b7dab14644da7b3fe`.

Los logs de descargas, compilación, regresiones y el snapshot están en
`outputs/complete-mediation-_ka4jd2a/`. Los logs oficiales de Rust están en
`outputs/integration-binding-rxmp8__u/tooling-stage/logs/`, con prefijos
`codex-mediation-20260910-` y `herdr-mediation-20260910-`. El formato se registra
en `outputs/native-effective-inputs-m0_o9hdf/mediation-20260910-fmt.json`.

## Trabajo de implementación que sigue pendiente

1. Herdr aún inicia el agente enviando texto al shell. Falta el lanzamiento
   directo y su vínculo con la identidad de nacimiento, imagen y canal privado.
2. El canal de observación de Codex conserva `authority=none`; no concede
   autorización completa por operación. Faltan el enlace autenticado al run de
   Fleet y la cobertura o denegación de todas las rutas de efectos.
3. Falta la ejecución interactiva Fleet → Herdr → Codex con observación externa,
   evidencia protegida, quiescencia y cierre A/B/ACK/spawn/C/D.

Las pruebas anteriores usan fixtures locales sin inferencia. No se ejecutó una
campaña, no se accedió a credenciales del proveedor y no se reemplazaron los
binarios instalados de Herdr o Codex. El ejecutor confinado por etapas sigue
siendo un carril distinto; sus resultados no completan esta integración nativa.
