---
name: slice-gate
description: Review completion of a meaningful implementation stage against its accepted scope and evidence. Use for multi-stage work, not every small edit.
---

# Cierre de una etapa

Compara el cambio con el objetivo autorizado y revisa el diff completo pertinente. Exige evidencia proporcional para los invariantes introducidos: pruebas que detecten su regresión y, cuando sea útil, documentación en el lugar que utiliza el proyecto.

Antes de avanzar, identifica dependencias pendientes y verifica que los resultados de esta etapa permiten el siguiente paso. Si varias etapas ya estaban autorizadas y no existe un bloqueo material, continúa sin una nueva aprobación ceremonial.

Distingue implementación, pruebas locales, smoke real y aceptación del usuario. Una etapa con un carril necesario sin verificar permanece parcialmente validada; documenta el límite sin atribuirle éxito.

No exige un archivo llamado INTERNAL_WIRING.md, un commit ni un push. Esas convenciones dependen del proyecto. Los efectos externos mantienen sus autorizaciones propias.

Solo en Dr.-strange/Claw y cuando sus reglas actuales lo requieran, consulta [su gate específico](references/claw-workflow.md).
