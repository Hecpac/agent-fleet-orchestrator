"""Conservative UI preflight for the explicitly selected Codex TUI lanes.

This is a delivery guard, not a session identity, sandbox, or acceptance proof.
Call before reserving a first stage; a hook identity may appear only when Codex
creates its first thread. Runtime results still need the normal transcript binding.

Each runtime contract names its guard. ``codex-0.153-v1`` and ``codex-0.154-v1``
share the observed 0.153 markers. ``codex-0.159-v1`` adds the 0.159.3 surfaces:
the Folder access notice shown for every explicitly untrusted folder and the
model migration prompt (outputs/cex91_aoh shows both consuming a prompt).
"""
from __future__ import annotations

import re

GUARD_0153 = "codex-0.153-v1"
GUARD_0154 = "codex-0.154-v1"
GUARD_0159 = "codex-0.159-v1"
GUARDS = frozenset({GUARD_0153, GUARD_0154, GUARD_0159})
READY_MARKER = "Ask Codex to do anything"

_COMMON_MARKERS = (
    ("Do you trust the contents of this directory?", "Codex project trust dialog"),
    ("Trusting the directory allows project-local config", "Codex project trust dialog"),
    ("Resuming session", "Codex session is still resuming"),
    ("Sign in with ChatGPT", "Codex authentication dialog"),
    ("Update available!", "Codex update dialog"),
    ("Hooks need review", "Codex hook trust dialog"),
    ("hook needs review", "Codex hook trust dialog"),
    ("hooks need review", "Codex hook trust dialog"),
    ("Press t to trust all", "Codex hook review panel"),
    ("Press enter to view hooks", "Codex hook review panel"),
    ("Press enter to confirm or esc to go back", "Codex startup choice"),
)
_CODEX_0159_MARKERS = (
    ("Folder access", "Codex folder access notice"),
    ("Trust this folder?", "Codex project trust dialog"),
    ("Trust and continue", "Codex project trust dialog"),
    ("Open existing task", "Codex existing task dialog"),
    ("Meet GPT-", "Codex model migration prompt"),
    ("Try new model", "Codex model migration prompt"),
    ("Use existing model", "Codex model migration prompt"),
    ("enter continue · esc quit", "Codex startup choice"),
    ("enter/esc confirm", "Codex startup choice"),
)

# The only notice the 0.159 guard may acknowledge: Codex's own text says the
# choice keeps restrictions and changes no saved trust. Exact wording, wrapped
# at any width; any other dialog, option or highlight is rejected.
FOLDER_ACCESS_BODY = (
    "Config, hooks, and exec policies from untrusted folders stay disabled. Trusted project "
    "folders can still contribute settings. Skills still load, and tools follow your "
    "permission settings. Opening will not change saved trust."
)
FOLDER_ACCESS_OPTIONS = ("› 1. Open restricted", "2. Quit", "enter continue · esc quit")


def codex_startup_blocker(screen: str, guard: str = GUARD_0153) -> str | None:
    """Reject observed startup dialogs even if Herdr calls the process ready."""
    if guard not in GUARDS:
        return "unknown Codex startup guard"
    if not isinstance(screen, str) or not screen.strip():
        return "empty Codex surface"
    markers = _COMMON_MARKERS + (_CODEX_0159_MARKERS if guard == GUARD_0159 else ())
    for marker, reason in markers:
        if marker in screen:
            return reason
    if READY_MARKER not in screen:
        return "Codex input prompt was not observed"
    return None


def folder_access_notice(screen: str, *, guard: str, cwd: str, home: str | None = None) -> bool:
    """True only for the exact 0.159 restricted Folder access notice for ``cwd``."""
    if guard != GUARD_0159 or not isinstance(screen, str) or not isinstance(cwd, str) or not cwd.startswith("/"):
        return False
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    if len(lines) < 5 or lines[0] != "Folder access" or tuple(lines[-3:]) != FOLDER_ACCESS_OPTIONS:
        return False
    paths = {cwd}
    if home and home.startswith("/") and cwd.startswith(home.rstrip("/") + "/"):
        paths.add("~" + cwd[len(home.rstrip("/")):])
    # Wrapping may split the path or the body anywhere; compare without
    # whitespace so width never changes the decision, only the exact content.
    middle = re.sub(r"\s+", "", "".join(lines[1:-3]))
    body = re.sub(r"\s+", "", FOLDER_ACCESS_BODY)
    return any(middle == re.sub(r"\s+", "", path) + body for path in paths)


def validate_local_socket_path(path: str, *, max_bytes: int = 103) -> None:
    """Check the complete Unix socket filename, including the client suffix."""
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise ValueError("socket path must be absolute and contain no NUL")
    if len(path.encode("utf-8")) > max_bytes:
        raise ValueError("private Herdr socket path exceeds the local byte limit")
