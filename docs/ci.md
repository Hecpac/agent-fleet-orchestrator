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

La validación de `workflows/regulated.yaml` en CI es estática. No implica que
pueda admitir efectos: el compilador de ejecución exige `hard_total` en todos
los proveedores resolubles y el run regulado exige WORM externo, dependencias
que el S0 local no declara disponibles.

El mismo contrato se puede ejecutar localmente con:

```bash
just ci
```

El workflow tiene permisos `contents: read`, cancela ejecuciones obsoletas,
tiene un timeout de 15 minutos y conserva los diagnósticos por 14 días. No
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
