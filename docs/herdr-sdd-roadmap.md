# Integración SDD: contrato inicial

## Estado y límites

Estado de la etapa 1: formato local `fleet.sdd.plan.v1`, ejemplo sintético y
validador de trazabilidad. Esa descripción original de la etapa 1 indicaba que
no se modificaba el driver de Mission; la etapa 2 (implementada, ver abajo) ya
añade integración opt-in de creación, recuperación y archivo SDD en el driver
Herdr. La ejecución nativa y la mediación de efectos siguen sin habilitarse.
`VALID` significa estructura y referencias válidas, nunca cumplimiento funcional
ni aceptación de Mission.

El documento JSON agrupa spec (`requirements`, `scenarios`), diseño (`design`),
tareas (`tasks`) y matriz prevista (`checks`). Cada escenario referencia un
requisito y expresa `given`, `when`, `then`; debe tener tarea y comprobación.
El diseño cubre todos los requisitos. Los IDs son únicos dentro de su tipo y
deben conservarse al revisar documentos; esta primera versión no compara
revisiones ni implementa deltas. Solo Worker puede ser owner de implementación.

Los checks son procedimientos declarativos: el validador no ejecuta su texto.
Todos permanecen `NOT_VERIFIED`. Se rechazan campos desconocidos, claves JSON
duplicadas, referencias rotas, texto vacío y afirmaciones de éxito funcional.
La validación estructural no demuestra que la prosa sea suficiente o correcta;
eso corresponde a revisión independiente. Este contrato no concede permisos.

Ejemplo: `examples/sdd/deny-before-effect.json`. Describe una futura fixture de
denegación, no una prueba ejecutada de aislamiento o del mediador nativo.

```sh
python3 -B scripts/fleet_sdd_contract.py examples/sdd/deny-before-effect.json
python3 -B -m unittest discover -s tests -p 'test_fleet_sdd_contract.py'
git diff --check
```

## Siguientes etapas y criterios

1. **Contrato local:** referencias completas y negativas focalizadas; esta etapa.
2. **CAS por misión:** congelar spec/diseño/tareas y comprobar recuperación sin
   releer documentos mutables. Implementada como cierre de etapa 2: creación
   Herdr opt-in, binding por ledger, archivo schema v5 con `sdd/plan.json` y
   `sdd/binding.json`, autoridad de selección pre-anchor y verificación
   independiente desde archivo + ledger durable. Limita a integridad/membresía;
   el cumplimiento de escenarios es la etapa 5 y sigue `NOT_VERIFIED`.
3. **Research:** etapa de lectura explícita, resultado durable y recuperación;
   el contrato de rol existente no equivale a una etapa activa. Pendiente.
4. **Autonomía:** avance por criterios y recuperación acotada; detener decisiones
   materiales fuera de alcance. Probar todo el flujo sin proveedores. Pendiente.
5. **Evidencia:** asociar cada escenario a prueba, resultado y digest del candidate;
   validar evidencia desde el controlador antes de aceptar. Pendiente.
6. **Engram:** memoria con procedencia y alcance; probar que un recuerdo obsoleto
   no altera permisos, artefactos congelados ni aceptación. Pendiente.

La mediación de efectos es un requisito independiente para habilitar el carril
nativo. Mantener globales, AGENTS por directorio y contratos con skills por rol.
No instalar componentes externos ni activar campañas como prueba implícita.

Referencias de diseño revisadas, no dependencias instaladas:
- https://github.com/Gentleman-Programming/gentle-ai
- https://github.com/Gentleman-Programming/engram

Adaptación deliberada: un documento JSON estricto permite probar el primer
contrato sin incorporar otro runtime. Formatos de evidencia y migración de
esquema se definirán al integrar CAS; no dar a v1 autoridad de cierre.
