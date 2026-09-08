"""Conservative UI preflight for the explicitly selected Codex 0.153 TUI lane.

This is a delivery guard, not a session identity, sandbox, or acceptance proof.
Call before reserving a first stage; a hook identity may appear only when Codex
creates its first thread. Runtime results still need the normal transcript binding.
"""
from __future__ import annotations


def codex_startup_blocker(screen: str) -> str | None:
    """Reject observed startup dialogs even if Herdr calls the process ready."""
    if not isinstance(screen, str) or not screen.strip():
        return "empty Codex surface"
    for marker, reason in (
        ("Update available!", "Codex update dialog"),
        ("Hooks need review", "Codex hook trust dialog"),
        ("hook needs review", "Codex hook trust dialog"),
        ("hooks need review", "Codex hook trust dialog"),
        ("Press t to trust all", "Codex hook review panel"),
        ("Press enter to view hooks", "Codex hook review panel"),
        ("Press enter to confirm or esc to go back", "Codex startup choice"),
    ):
        if marker in screen:
            return reason
    if "Ask Codex to do anything" not in screen:
        return "Codex input prompt was not observed"
    return None


def validate_local_socket_path(path: str, *, max_bytes: int = 103) -> None:
    """Check the complete Unix socket filename, including the client suffix."""
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise ValueError("socket path must be absolute and contain no NUL")
    if len(path.encode("utf-8")) > max_bytes:
        raise ValueError("private Herdr socket path exceeds the local byte limit")
