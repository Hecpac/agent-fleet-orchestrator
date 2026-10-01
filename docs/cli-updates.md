# Actualización automática de las CLI

[fleet_cli_update.py](../scripts/fleet_cli_update.py) mantiene al día las CLI
que usa la flota sin romper los contratos fijados. Corre a diario con launchd
(`com.agent-fleet.cli-update`, 09:30) y deja un informe JSON por ejecución en
`~/.local/share/fleet-cli-updates/runs/`. No hace commits, no edita contratos de
runtime, no envía prompts ni llama a proveedores de modelos.

## Políticas

| CLI | Política | Qué hace | Comprobación posterior |
|---|---|---|---|
| `claude` | `auto` | `npm install -g @anthropic-ai/claude-code@<última>` | Versión exacta; si no coincide, reinstala la anterior |
| `opencode` | `auto` | `brew upgrade opencode` | Versión; la fórmula puede ir por detrás de upstream |
| `ollama` | `auto` | `brew upgrade ollama` | Versión |
| `kimi` | `auto` | Instalación nativa: manifiesto de `code.kimi.com`, binario y SHA-256; nunca ejecuta el script remoto | Antes de reemplazar el binario, un turno real del binario descargado contra un proveedor loopback; `kimi_hook_bridge` y `fleet_frontier` deben aceptar el Wire producido. Si no, no se instala |
| `codex` | `certify` | Actualiza el `codex` global como `auto` y, además, instala la versión nueva en paralelo en `~/.local/share/fleet-codex/versions/<v>/` | Certificación automática real ([fleet_codex_certify.py](../scripts/fleet_codex_certify.py)); si pasa, entra en el registro y la usan las Missions nuevas |
| `herdr` | `report` | Sólo informa la versión | — |

## Codex sin restricciones para el operador, fijado para las Missions

Herdr 0.9.0 arranca `codex` desde el `PATH` del pane: `agent start` no admite
`--executable`. Por eso el `codex` global y el que usa una Mission son
instalaciones distintas:

- **Global** (`npm install -g`): se actualiza a diario a la última versión, sin
  condiciones. Ninguna Mission depende de él.
- **Missions**: cada versión certificada vive en
  `~/.local/share/fleet-codex/versions/<v>/` y queda en el registro
  (`registry.json`, sólo añade). Una Mission nueva congela un contrato
  certificado (`version: 2`, con el id de su certificación, que también guarda
  en su CAS) y su `PATH` apunta a esa instalación. Una Mission en curso conserva
  la suya aunque llegue otra versión. El preflight comprueba que el `PATH` del
  pane resuelve exactamente ese binario y su SHA-256.

La certificación es el ensayo de mantenimiento completo, automático y sin
modelo: Herdr y la TUI reales en un perfil privado, red sólo loopback, hook de
Herdr con hash revisado, arranque de los cuatro roles con la comprobación de
arranque **vigente**, dos turnos del Worker, recuperación por un controlador
nuevo, rechazo del proveedor de fixture y del revinculado tras reiniciar el
servidor. Registra también las peticiones de título de tarea.

Si una versión nueva no pasa (por ejemplo, un diálogo o una migración de modelo
nuevos), el `codex` global igualmente se actualiza, las Missions siguen con la
última versión certificada y el fallo queda en `attempts` del registro y en el
informe diario. Ese fallo no se reintenta cada día. Adaptar la comprobación de
arranque a un diálogo nuevo sigue siendo un cambio revisado.

El registro se activa con `FLEET_CODEX_ROOT`: lo exportan `just` y la tarea
diaria. `check-ci.sh` lo deja vacío, así que los tests nunca leen el registro
de la máquina. Sin registro, el backend usa el contrato estático 0.159.3.

Certificar a mano una instalación:

```sh
FLEET_CODEX_ROOT=~/.local/share/fleet-codex python3 -B scripts/fleet_codex_certify.py \
  --codex ~/.local/share/fleet-codex/versions/<v>/bin/codex --register
```

## Carril cápsula

Su suite real de Seatbelt pasa con Codex 0.154.0 y falla con 0.159.3 (timeout
que deja de interrumpirse y peers que no se detienen). La cápsula sigue fijada a
0.154.0 y, por defecto, usa la instalación en paralelo
`~/.local/share/fleet-codex/versions/0.154.0/` (`fleet_mission_capsule.py
--image` es opcional). Migrarla a otra versión requiere repetir esa suite.

## Kimi y Claude Code

- El manifiesto y el binario de Kimi vienen del mismo origen: el SHA-256 prueba
  la integridad de la descarga, no la autenticidad del publicador. Es la misma
  confianza que el instalador oficial, sin ejecutar su script.
- La instalación en segundo plano de Kimi está desactivada en
  `~/.kimi-code/tui.toml` (`[upgrade] auto_install = false`; respaldo en
  `~/.local/share/fleet-cli-backups/20261001/kimi-tui.toml`). La tarea también
  exporta `KIMI_CODE_NO_AUTO_UPDATE=1`. Así, toda actualización de Kimi pasa por
  la comprobación del bridge.
- Claude Code conserva su actualizador propio. Es compatible: ambos instalan
  versiones publicadas y Fusion lanza `claude` sin un contrato de versión.

## Dentro de los panes de una Mission

Una actualización no debe aparecer como diálogo en un pane. En el perfil privado
de Codex: `check_for_update_on_startup=false` y las migraciones de modelo vistas
en `[notice.model_migrations]`. Los homes aislados de Kimi heredan
`KIMI_CODE_NO_AUTO_UPDATE=1` si el lanzador lo exporta.

## Operación

```sh
python3 -B scripts/fleet_cli_update.py check
python3 -B scripts/fleet_cli_update.py run
python3 -B scripts/fleet_cli_update.py install-agent --hour 9 --minute 30
python3 -B scripts/fleet_cli_update.py uninstall-agent
```

Los respaldos de cada ejecución quedan en
`~/.local/share/fleet-cli-updates/backups/`. Homebrew no conserva versiones
anteriores tras su limpieza, así que una actualización de `opencode` u
`ollama` se informa pero no se revierte automáticamente.
