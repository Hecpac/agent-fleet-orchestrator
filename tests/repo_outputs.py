from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def repo_outputs() -> Path:
    """Return the ignored repository scratch root, creating it in a fresh checkout.

    Fixtures that must live beside the repository use it instead of assuming a
    developer machine already created ``outputs/``.
    """
    path = ROOT / "outputs"
    path.mkdir(mode=0o700, exist_ok=True)
    return path
