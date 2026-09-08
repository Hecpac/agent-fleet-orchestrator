# B: aceptación de artefactos con CMUX

Primera entrega de B. El transporte sigue siendo CMUX. Un contrato seleccionado
por el operador define requisitos obligatorios y predicados deterministas sobre
los artefactos del commit final archivado. No se ejecuta código del candidato.

## Uso

El contrato JSON tiene esta forma:

```json
{
  "schema_version": 1,
  "requirements": [
    {
      "id": "R1",
      "description": "El documento especifica los límites de aceptación",
      "checks": [
        {
          "kind": "text_contains",
          "path": "docs/acceptance.md",
          "expected": "La aceptación cubre solamente el contrato declarado."
        }
      ]
    }
  ]
}
```

Validar el contrato sin arrancar agentes:

```sh
python3 scripts/fleet_acceptance.py --contract /ruta/contrato.json
python3 scripts/mission-run.py dry mi-tarea "Objetivo concreto" \
  --target-repo /ruta/proyecto --acceptance-contract /ruta/contrato.json --json
```

La ruta de ejecución B exige el contrato:

```sh
just mission-verified mi-tarea "Objetivo concreto" /ruta/contrato.json \
  --target-repo /ruta/proyecto
```

Ese último comando sí arranca proveedores y conserva las autorizaciones y
límites habituales de Mission Control. No ejecutarlo como una validación estática.

## Contrato y evidencia

- Todos los requisitos y todas sus comprobaciones son obligatorios.
- `text_contains`: texto UTF-8 que contiene un valor esperado no vacío.
- `json_equals`: valor JSON exacto, seleccionado por una lista `keys` de claves
  de objeto. No confunde `true` con `1`; rechaza claves JSON duplicadas.
- `sha256`: contenido exacto de un archivo, útil para artefactos congelados.
- Rechaza rutas no canónicas, artefactos ausentes y enlaces en lugar de archivos.
- La identidad de la misión incorpora el hash del contrato; reanudar no permite
  quitarlo, cambiarlo ni añadirlo retroactivamente a una misión legacy.
- Verifica el archivo de evidencia y evalúa su árbol final en la misma lectura;
  los archivos posteriores del checkout no alteran el resultado.
- Guarda `acceptance-result.json` junto al archivo de evidencia. El evento terminal
  incluye su hash. El recibo identifica misión, commit, contrato y árbol, además
  del resultado de cada requisito. No modifica el archivo de evidencia sellado.
- Un resultado rechazado produce una misión `failed` y salida CLI distinta de cero.
  Los errores de integridad bloquean el cierre; no se convierten en aceptación.
- La ruta legacy permanece compatible y se identifica como `not_evaluated`.

## Límites

`accepted` significa **cumple los predicados declarados sobre los artefactos**.
No demuestra comportamiento funcional, revisión independiente ni veracidad de
un informe de pruebas escrito por el modelo. No usar `status: passed` en un JSON
como sustituto de ejecutar pruebas. Los predicados deben representar requisitos
reales; la calidad de un contrato superficial también será superficial.

Esta entrega no cambia el supervisor Kimi, los permisos, los modelos, el
transporte ni la política de publicación. La aceptación ocurre después del
archivado/publicación local existente: un rechazo no revierte el commit publicado.
Las misiones fallidas conservan su evidencia; corregir requiere una nueva misión.

Pendientes para B completo: pruebas funcionales ejecutadas por CONTROL en un
entorno autorizado y aislado, revisión independiente del resultado, supervisión
conjunta proveedor/bridge y evaluación comparativa con tareas reales.

## Verificación de esta entrega

Las suites `test_fleet_acceptance`, `test_mission_run` y `test_fleet_archive`
pasaron juntas: 61 tests con Python 3.13 y OpenSSL de Homebrew. El OpenSSL del
sistema no ofrece ED25519 para el caso de auditoría firmado.

```sh
PATH=/opt/homebrew/opt/openssl@3/bin:/opt/homebrew/bin:/usr/bin:/bin \
  /opt/homebrew/bin/python3.13 -m unittest \
  tests.test_fleet_acceptance tests.test_mission_run tests.test_fleet_archive -q
```

El smoke separado ejerció la CLI real con un `git archive` temporal: aceptación,
rechazo con exit 1 y `mission-run.py dry` con contrato sin efectos. La integración
de Mission Control usa transporte simulado y archivo Git real. No equivale a
una misión viva en CMUX; tampoco se ejecutó la suite completa del repositorio.
