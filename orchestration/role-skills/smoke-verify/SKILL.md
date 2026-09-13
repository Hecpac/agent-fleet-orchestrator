---
name: smoke-verify
description: Verify changed behavior through the real local entry point after implementation. Distinguish live evidence, simulation, and untested paths.
---

# Verificación funcional proporcional

Identifica el comportamiento cambiado, su entrada real y la evidencia observable de éxito. Lee los comandos del proyecto antes de ejecutarlos. Un test unitario, un proceso terminado o una pantalla activa no demuestran por sí solos que se completó la tarea.

Ejecuta las pruebas enfocadas y, cuando el entorno y la autorización lo permitan, ejerce la CLI, API, interfaz o servicio real. Incluye el caso que fallaba y comprueba el resultado material. Distingue explícitamente fixtures, transporte simulado y proveedores reales.

Reinicia un servicio solo si necesita cargar el cambio, mediante su procedimiento documentado. No inventes puertos, nombres de daemon, ramas ni rutas de logs. Limita la prueba a recursos propios; no interrumpas ejecuciones ajenas. No uses una prueba como autorización implícita para gasto, publicación o cambios de permisos.

Conserva evidencia útil, informa PASS/FAIL del comportamiento probado y enumera los carriles pendientes. Si falta autorización o infraestructura para un smoke real, completa la verificación local posible y entrega el límite concreto; no declares verificado el carril omitido.

Solo para el repositorio Dr.-strange/Claw, consulta [el procedimiento específico](references/claw-workflow.md) si sus instrucciones actuales todavía lo requieren. Las rutas de esa referencia no se aplican a otros proyectos.
