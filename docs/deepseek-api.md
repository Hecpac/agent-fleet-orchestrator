# DeepSeek por API con OpenCode

El archivo `opencode.json` de la raíz registra dos modelos para uso directo en
este proyecto: `deepseek/deepseek-flash` y `deepseek/deepseek-v4-pro`.
OpenCode utiliza Chat Completions en `https://api.deepseek.com` y obtiene la
clave de `DEEPSEEK_API_KEY`. La configuración no contiene credenciales ni cambia
el modelo predeterminado.

## Uso

Desde la raíz del proyecto, carga tu clave en la terminal. En zsh puedes hacerlo
sin mostrarla ni escribirla en el historial:

```zsh
read -rs 'DEEPSEEK_API_KEY?DeepSeek API key: '
export DEEPSEEK_API_KEY
printf '\n'
```

La variable dura durante esa sesión de terminal. OpenCode no carga un `.env`
por esta configuración. Puedes obtener la clave en
[DeepSeek Platform](https://platform.deepseek.com/api_keys).

Lista los modelos sin enviar un prompt:

```sh
opencode models deepseek --pure
```

Abre el modelo elegido:

```sh
opencode -m deepseek/deepseek-flash
# O bien:
opencode -m deepseek/deepseek-v4-pro
```

El envío de prompts utiliza tu cuenta API de DeepSeek y su saldo. Estos comandos
son sesiones directas de OpenCode: no asignan Worker/Reviewer a Missions Herdr,
no generan aceptación de Mission y no cambian el router ni el carril personal
Codex/ChatGPT. La clave debe estar en el entorno del proceso que abre OpenCode.

## Modelos y verificación

Los identificadores, modalidades y límites se contrastaron con la
[documentación oficial de DeepSeek](https://api-docs.deepseek.com/quick_start/pricing/).
Se declaran explícitamente para que un catálogo local antiguo pueda reconocer
`deepseek-flash`. Las tarifas mostradas por OpenCode pueden depender de su
catálogo; la facturación real corresponde al proveedor.

La [guía de integración de DeepSeek](https://api-docs.deepseek.com/quick_start/agent_integrations/opencode/)
recomienda OpenCode >= 1.18.30. La instalación local observada es 1.18.10;
en ella se comprobó el 2026-09-12 que `debug config --pure` resuelve el endpoint
y la variable de la clave, y que `models deepseek --pure` lista exactamente los
dos modelos. Se usaron directorios temporales, una clave sintética y bloqueo de
red del sistema operativo. El router existente y `git diff --check` también
pasaron su validación.

Esta comprobación local no certifica inferencia, tool calls, visión ni
compatibilidad completa. No se actualizó software ni se hizo una llamada de
generación.

La autenticación y la generación real quedan `NOT_VERIFIED` mientras no haya
una clave válida y una prueba explícita con el proveedor.
