from pathlib import Path


def resolve_target(root, relative):
    if not isinstance(relative, str) or not relative or relative.startswith("/") or "\\" in relative:
        raise ValueError("invalid relative path")
    parts = relative.split("/")
    if any(part in {"", ".", "..", ".git"} for part in parts):
        raise ValueError("invalid component")
    target = Path(root)
    for index, part in enumerate(parts):
        target = target / part
        if target.is_symlink():
            raise ValueError("symlink")
        if index < len(parts) - 1 and target.exists() and not target.is_dir():
            raise ValueError("parent is not a directory")
    return target
