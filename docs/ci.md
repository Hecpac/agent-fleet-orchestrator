# CI remoto

El workflow `.github/workflows/ci.yml` es el gate determinista de la flota. Se
ejecuta en un runner hospedado de Ubuntu para cada pull request, cada push a
`main` y mediante `workflow_dispatch`.

## Qué valida

`scripts/check-ci.sh` valida los workflows tipados, compila `scripts/` y
`tests/`, comprueba la sintaxis de todos los launchers Bash, ejecuta la suite
completa y revisa whitespace con `git diff --check`. Los tests que dependen de
una instalación local de hooks de Claude se marcan explícitamente como
omitidos en runners hospedados y permanecen cubiertos por el lane live de
macOS; no se sustituyen por credenciales ni servicios falsos.

El contrato portátil requiere Bash, Git, Python 3.12 o posterior y `uv`. El lane
hospedado fija Python 3.12 e instala de forma reproducible `uv==0.9.26` antes
de ejecutar el gate, y
`scripts/check-ci.sh` falla al inicio con un diagnóstico explícito si falta
alguna de esas dependencias. Los tests de coherencia del router desactivan las
comprobaciones de binarios de proveedores porque validan configuración
estática; el lane portátil no necesita instalar Codex, Claude, OpenCode o Kimi.

Los tests del harness Mini necesitan las fuentes reales de Mini 2.4.6. Si
`FLEET_MINI_DIST` no está definido, `scripts/check-ci.sh` las instala desde
`requirements/mini-2.4.6.txt` con `--require-hashes` en una caché indexada por
el hash del lock (`$FLEET_CI_CACHE`, o `~/.cache/agent-fleet-ci`) y las reutiliza
en ejecuciones posteriores. Esa instalación requiere acceso a PyPI. Sin fuentes,
los tests fallan con un diagnóstico explícito; no se omiten.

La validación de `workflows/regulated.yaml` en CI es estática. No implica que
pueda admitir efectos: el compilador de ejecución exige `hard_total` en todos
los proveedores resolubles y el run regulado exige WORM externo, dependencias
que el S0 local no declara disponibles.

`just workflow-catalog` muestra por separado la validez del esquema y la
disponibilidad de compilación con el router actual. Es diagnóstico estático:
`runtime_checked=false` no acredita binarios, credenciales, WORM ni permisos de
ejecución. Las políticas bloqueadas permanecen visibles con el error del
compilador. `just workflow-validate` conserva su significado de validación de
esquemas, incluido `regulated.yaml`.

Las recetas Python de `just` y el gate portátil usan `FLEET_PYTHON`, con
`python3.12` como valor por defecto. Para seleccionar otro Python compatible:

```bash
FLEET_PYTHON=/ruta/a/python3.12 just workflow-catalog
FLEET_PYTHON=/ruta/a/python3.12 just ci
```

Las invocaciones directas de scripts deben utilizar ese mismo intérprete; el
proyecto no modifica el Python global ni el `PATH` del usuario.

El mismo contrato se puede ejecutar localmente con:

```bash
just ci
```

El workflow tiene permisos `contents: read`, cancela ejecuciones obsoletas,
tiene un timeout de 45 minutos y conserva los diagnósticos por 14 días. No
instala CMUX, no arranca proveedores y no recibe credenciales de Codex, Claude,
GLM, MiniMax, S3 o WORM.

## Activación del gate

En GitHub, después de la primera ejecución exitosa:

1. proteger `main`;
2. exigir el check `portable` antes del merge;
3. exigir ramas actualizadas antes del merge;
4. mantener al menos una revisión humana;
5. no permitir que workflows de PRs no confiables accedan a secretos.

## Lane live separado

El smoke Mission, CMUX y los canarios directos de Codex/Claude requieren un
runner macOS controlado y credenciales instaladas. Ese lane debe ser manual o
programado, con un `--runs-dir` temporal y sin ejecutarse automáticamente sobre
PRs de forks. No es parte del gate hospedado porque su resultado depende del
host, del estado de CMUX y de servicios externos.
