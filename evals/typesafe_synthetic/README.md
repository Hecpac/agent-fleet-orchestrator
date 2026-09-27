# Prueba sintética de TypeSafe

Ensayo independiente preparado con la skill `typesafe-ai`: 18 solicitudes
ficticias de trabajo de software, escritas en español e inglés. El resultado es
una **recomendación** entre `research`, `plan`, `build`, `review`, `verify` y
`human_review`. No se conecta al router de Fleet ni tiene funciones para crear
Missions, agentes o ejecutar las tareas descritas en las solicitudes.

Objetivo: preparar entradas revisables para Jev, comprobar el consumidor local y
dejar disponible una medición posterior sobre estas etiquetas. Éxito local:
18 decisiones simuladas coinciden con sus etiquetas; las respuestas inválidas
y los fallos de transporte producen abstención y un resultado fallido; las
etiquetas nunca forman parte del `state` enviado.

## Diseño y datos

- `cases.json`: 18 textos inventados, etiquetas manuales y categorías de cobertura.
  Incluye negaciones, contexto ausente, tareas múltiples, una instrucción citada,
  texto vacío y operaciones fuera del catálogo. Ningún texto procede de usuarios,
  correos, repositorios o Missions reales. Las etiquetas no tienen revisión independiente.
- `questions.json`: una `Choice` para la intención y una `Noul` para identificar
  si existe una primera tarea clara. Se envían juntas sobre el mismo `state`;
  ninguna depende de la respuesta de la otra.
- `mock_answers.json`: respuestas **inventadas**, almacenadas por separado de las
  etiquetas. Ejercitan el consumidor, no imitan resultados medidos de Jev.
  `syn-12` contiene intencionadamente una Choice firme y un Noul bajo para
  comprobar que la segunda señal puede vetar la recomendación.
- `trial.py`: Python estándar, sin instalación ni SDK adicional. Por defecto
  funciona offline. Conserva solicitudes, respuestas válidas, modelo declarado
  por el proveedor, uso observado y resultados por caso en un informe JSON.
- `test_trial.py`: pruebas locales del consumidor, los fallos y el transporte
  simulado. No envían peticiones al proveedor.

La política propone una categoría solo si `Choice.confidence >= 0.80`,
`Noul.noul >= 0.80` y la opción difiere de `no_match`. En los demás casos devuelve
`human_review`. Estos umbrales son **provisionales y no están calibrados**.
`Noul` representa probabilidad de sí; no tiene un campo de confianza separado.
Una recomendación nunca concede permisos: todos los informes tienen
`authority: none` y `executed_actions: []`.

## Reproducir localmente

Desde la raíz del repositorio, con Python 3.12 o posterior:

```bash
python3 -B evals/typesafe_synthetic/trial.py
python3 -B evals/typesafe_synthetic/trial.py prepare --limit 2
python3 -B -m unittest discover -s evals/typesafe_synthetic -p 'test_trial.py' -v
```

El primer comando produce el informe offline; el segundo muestra dos cuerpos
HTTP exactos sin enviarlos. Ninguno lee claves ni realiza llamadas al proveedor.
El modo offline usa el mismo constructor, validador, política y evaluador que el
modo real. Las respuestas inventadas **no clasifican los textos**.

`offline-report.json` conserva la ejecución local entregada. Puede regenerarse:

```bash
python3 -B evals/typesafe_synthetic/trial.py > evals/typesafe_synthetic/offline-report.json
```

Los hashes del informe identifican los datos, las preguntas, las respuestas
simuladas y el programa que lo generó. Exit 0 significa que las decisiones
coinciden con las etiquetas en la ejecución seleccionada; no prueba la calidad
del modelo ni el éxito de una Mission. Exit 1 indica desacuerdos o errores;
exit 2, argumentos o clave ausentes.

## Ejecución real preparada, pendiente

El contrato fue consultado el **2026-09-17** en las páginas oficiales de
[HTTP API](https://docs.typesafe.ai/api.md),
[Choice](https://docs.typesafe.ai/primitives/choice.md),
[Noul](https://docs.typesafe.ai/primitives/noul.md),
[State](https://docs.typesafe.ai/concepts/state.md),
[Confidence](https://docs.typesafe.ai/confidence.md),
[intent routing](https://docs.typesafe.ai/patterns/intent-routing.md),
[function calling](https://docs.typesafe.ai/cookbooks/function_calling.md) y
[Models](https://docs.typesafe.ai/models.md).

Está preparado `POST https://api.typesafe.ai/v1/systemone`, con autenticación
Bearer y el modelo versionado `jev-1.13.0`. Se puede cambiar mediante `--model`;
el informe distingue el modelo solicitado del declarado por la respuesta.
La compatibilidad del servicio real sigue `NOT_VERIFIED`.

Para una ejecución posterior, el [AGENTS.md](../../AGENTS.md) exige autorización
explícita para gasto y campañas con proveedores. Una vez autorizada esa ejecución,
la clave debe estar disponible como `TYPESAFE_API_KEY` en el entorno del proceso,
sin incluirla en archivos, argumentos ni informes. Entonces:

```bash
# Una solicitud real, dos preguntas; puede generar coste.
python3 -B evals/typesafe_synthetic/trial.py live --limit 1

# Muestra completa: máximo 18 solicitudes, dos preguntas por solicitud.
python3 -B evals/typesafe_synthetic/trial.py live --limit 18
```

`--limit` es obligatorio para `live`. Se procesa secuencialmente, con un intento
por caso, timeout de 30 segundos y sin seguir redirecciones. Un error HTTP,
de conexión o de contrato detiene el ensayo y conserva el informe parcial.
Una respuesta válida pero incorrecta se registra y permite evaluar el siguiente
caso. Tras un timeout, puede haber consumo aunque falte la respuesta; no hay
reintentos automáticos ni garantía de ausencia de cargo.

## Interpretar el informe

| Campo | Significado |
| --- | --- |
| `decision_agreement` | Recomendaciones que coinciden con la etiqueta / casos intentados; los errores no cuentan como acierto. |
| `intent_accuracy` | Aciertos de Choice entre respuestas válidas con intención etiquetada; excluye los tres casos ambiguos con `intent: null`. |
| `proposal_coverage` | Recomendaciones distintas de `human_review` / casos intentados. |
| `proposal_precision` | Propuestas que coinciden con la etiqueta / propuestas emitidas; `null` si no hay propuestas. |
| `single_intent_brier` | Error cuadrático medio de Noul frente a la etiqueta binaria entre respuestas válidas; menor es mejor. |
| `abstentions` | Respuestas válidas que terminan en `human_review`; los errores se contabilizan aparte. |
| `usage` | Suma solo cuando todos los intentos tienen ese contador; en otro caso `null`, con cobertura explícita. |
| `latency_ms` | Tiempo observado por intento real, incluidas lectura y validación; `null` en offline. |

En offline, las métricas describen exclusivamente las respuestas inventadas y
el comportamiento del consumidor (`metrics_scope: simulated_consumer_only`).
La comprobación de una instrucción citada no demuestra resistencia real a
prompt injection. El código comprueba el formato y las abstenciones; los datos
no constituyen un benchmark representativo ni un conjunto independiente para
calibrar umbrales. Antes de ajustar y evaluar, se necesitan muestras nuevas y
conjuntos separados de calibración y evaluación.

Incluso con respuestas reales, las métricas solo describirían esta muestra
sintética. Calidad general, calibración, aislamiento y coste facturado siguen
**NOT_VERIFIED**. `cost_usd` permanece `null`; no se deduce una factura a partir
de tokens o precios publicados.
