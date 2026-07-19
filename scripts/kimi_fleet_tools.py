"""Workspace-scoped read tools for the Fleet Kimi reviewer.

Kimi's stock ReadFile, ReadMediaFile, and Grep tools accept absolute paths
outside the configured work directory.  A read-only Fleet role must not be
able to inspect CONTROL credentials or another instance's state, so the agent
spec loads these wrappers instead.
"""

from pathlib import Path
from typing import Any

from kaos.path import KaosPath
from kosong.tooling import ToolError, ToolReturnValue

from kimi_cli.soul.agent import BuiltinSystemPromptArgs, Runtime
from kimi_cli.tools.file.grep_local import Grep
from kimi_cli.tools.file.read import ReadFile
from kimi_cli.tools.file.read_media import ReadMediaFile
from kimi_cli.utils.path import is_within_directory


def _outside_workspace(path: KaosPath, work_dir: KaosPath) -> ToolError | None:
    try:
        resolved = path.canonical()
    except Exception:
        return ToolError(message=f"`{path}` cannot be resolved safely.", brief="Invalid path")
    if is_within_directory(resolved, work_dir):
        return None
    return ToolError(
        message=f"`{path}` is outside the configured Fleet working directory.",
        brief="Path outside working directory",
    )


class FleetReadFile(ReadFile):
    """ReadFile with an enforced workspace boundary for absolute paths."""

    def __init__(self, runtime: Runtime) -> None:
        super().__init__(runtime)

    async def _validate_path(self, path: KaosPath) -> ToolError | None:
        return _outside_workspace(path, self._work_dir)


class FleetReadMediaFile(ReadMediaFile):
    """ReadMediaFile with the same enforced workspace boundary."""

    def __init__(self, runtime: Runtime) -> None:
        super().__init__(runtime)

    async def _validate_path(self, path: KaosPath) -> ToolError | None:
        return _outside_workspace(path, self._work_dir)


class FleetGrep(Grep):
    """Grep that canonicalizes and confines its search root to the workspace."""

    def __init__(self, builtin_args: BuiltinSystemPromptArgs) -> None:
        super().__init__()
        self._work_dir = Path(str(builtin_args.KIMI_WORK_DIR)).resolve(strict=True)

    async def __call__(self, params: Any) -> ToolReturnValue:
        supplied = Path(str(params.path))
        candidate = supplied if supplied.is_absolute() else self._work_dir / supplied
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._work_dir)
        except (OSError, ValueError):
            return ToolError(
                message=f"`{params.path}` is outside the configured Fleet working directory.",
                brief="Path outside working directory",
            )
        return await super().__call__(params.model_copy(update={"path": str(resolved)}))
