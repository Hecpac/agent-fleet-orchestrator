# Mediación por cápsula: integración local y trabajo pendiente

Este cambio **no completa la mediación del transporte nativo Herdr**. Integra
un ejecutor por etapa para Codex bajo una frontera OS externa, separado del
arranque nativo que permanece cerrado. La vía personal no se habilita.

## Decisión de implementación

Se reutilizan los componentes de cápsula y broker del worktree local `b1db`,
adaptados a este checkout y al CLI instalado **0.154.0**. No se importan SDD,
Engram, la vía personal, ni cambios de configuración global. El nuevo manifiesto
`fleet.mission.capsule.v2` fija versión e imágenes; no reinterpreta los registros
v1 de otro worktree ni cambia la matriz histórica de Herdr.

El proceso Codex y su auxiliar nacen bajo Seatbelt. El sandbox interno se
registra como `danger-full-access`, pero la política OS externa se hereda y
no puede ampliarse desde sus herramientas. Los permisos de archivo v2 exigen
esa evidencia externa; no la hacen pasar por permisos históricos v1.

| Superficie | Frontera implementada y comprobable |
| --- | --- |
| Filesystem | Copias de bytes explícitos; solo Build escribe `work`. Lectores usan candidato de solo lectura. Temporales y HOME privados. |
| Procesos | Ejecución limitada a las imágenes privadas de Codex y code-mode host. Shell y programas del candidato denegados. |
| Red | Solo puerto del broker local; ruta, Host, capacidad, modelo, esfuerzo y protocolo validados antes de reservar/enviar. |
| Credenciales | Auth del proveedor queda en CONTROL. El agente recibe una capacidad efímera; su eco se rechaza antes de CAS/inferencia. Las pruebas usan secretos sintéticos. |
| Lifecycle | Admisión, generación, prompt, candidato, deadline y control enlazados. Intento consumido antes del arranque; sin reenvío de un resultado ambiguo. |
| Exportación | Después de recoger los procesos propios; sin symlinks, hardlinks, archivos especiales ni `.git`. Solo Build promueve bytes de CAS. |
| Cierre | Cinco etapas, candidato congelado, contrato de artefactos, ledger, CAS y verificación independiente del archivo. Inferencia pendiente impide declarar quiescencia. |

Las reglas OS también restringen accesos directos que no atraviesan el wrapper
de spawn. Esto no equivale a un ACK de CONTROL por cada operación ni a la
atestación A/B/C/D completa del diseño nativo. MCP externo, herramientas remotas,
shell y ejecutables arbitrarios siguen fuera del perfil permitido.

## Selección explícita

`mission-run.py run` acepta `--herdr-capsule-manifest` junto con
`--herdr-runtime-root`, sesión lógica y contrato de aceptación. El runtime root
debe ser privado y separado de source/runs. No se combina con el launcher
experimental y no se selecciona automáticamente para Missions existentes.

El ejecutor es **headless**: son cuatro roles y cinco procesos/sesiones por
las cinco etapas, no cuatro panes interactivos de Herdr. `status` y `report`
reconocen su identidad propia y no atribuyen al proveedor real los turnos
de una fixture que declara `SIMULATED`.

El adaptador de proveedor importado solo se usa tras selección explícita del
manifiesto. Puede preparar un descriptor de suscripción mediante
`fleet_mission_capsule.py`, lo que lee la autenticación del HOME indicado.
Ese acceso y una campaña real necesitan autorización de su alcance. Durante
esta implementación no se leyó auth del usuario ni se generó texto remoto.
No se presenta esta ruta como uso del transporte/auth normal del CLI personal.

## Evidencia reproducible

Los módulos relevantes son `test_fleet_native_sandbox`,
`test_fleet_codex_sandbox`, `test_fleet_codex_capsule_os`,
`test_fleet_herdr_inference`, `test_fleet_codex_responses`,
`test_fleet_chatgpt_provider`, `test_fleet_mission_capsule` y
`test_fleet_mediation_contract`. Son pruebas con proveedor sintético.
El carril nativo requiere fijar `FLEET_CODEX_IMAGE` al binario ya instalado;
sin él esas pruebas se omiten y no acreditan ejecución del CLI. Su versión sigue
siendo 0.154.0: con 0.159.3 la suite real falla. Desde el 2026-10-01 se usa la
instalación en paralelo `~/.local/share/fleet-codex/versions/0.154.0/`, que es
también la imagen por defecto de `fleet_mission_capsule.py` cuando
`FLEET_CODEX_ROOT` está definido ([actualización de CLI](cli-updates.md)).

La evidencia de esta integración queda en
`outputs/complete-mediation-_ka4jd2a/`: baseline/diff previo, logs de pruebas,
Mission retenida y verificación offline. El caso de cinco etapas produce
`answer.txt` con los bytes `implemented\n`, verifica el archivo y confirma
que volver a conducir la Mission no envía una segunda solicitud.

Regresión conjunta final: **312 pruebas ejecutadas por unittest, 311 aprobadas
y 1 omitida** en 199,796 segundos. La omitida requiere un probe SDK precompilado
separado; las pruebas del CLI 0.154.0 y del kernel Seatbelt sí se ejecutaron.
`git diff --check` pasó. La comparación de hashes de la baseline no encontró
modificaciones fuera de los once archivos existentes previstos.

```sh
python3 -B scripts/fleet_herdr_archive.py \
  --runs-dir outputs/complete-mediation-_ka4jd2a/runs \
  --mission-id 1387dd85-a4c7-525e-a340-a69d4ea44b84
git diff --check
```

El resultado del broker acredita integridad offline, no calidad de un modelo
ni facturación. El origen temporal de los paths del archivo se preserva como
evidencia; la verificación se realiza con CAS retenido sin consultar ese runtime.

## Dependencias para completar el objetivo nativo

1. Llevar la frontera al arranque interactivo Herdr → Codex, con identidad de
   proceso/imagen y canal autenticado por operación. No sustituir `agent start`
   por un shell no mediado ni otorgar grants por una observación exitosa.
2. Compilar y verificar los hooks nativos fijados. Las fuentes locales de Herdr
   requieren Rust 1.96.1 y las de Codex 1.95.0; el host tiene Rust 1.93.1. Instalar
   toolchains y obtener dependencias fijadas requiere autorización separada.
3. Cubrir o denegar cada ruta de herramientas en el runtime interactivo y
   verificar recuperación, ACK ambiguo, identidad de nacimiento y efectos A/B/C/D.
4. Probar el transporte real con autorización para la sesión, autenticación y
   objetivo concreto. Los ensayos locales no verifican inferencia remota.

No se afirma protección de CONTROL frente a otro proceso host malicioso del
mismo UID, cuotas agregadas de RAM/disco/procesos, aislamiento de todos los
metadatos, ni contención de ejecutables arbitrarios. Esas propiedades no se
deducen del perfil, de los hashes o de una suite verde. La denegación nativa
permanece activa mientras falte su adaptador completo.
