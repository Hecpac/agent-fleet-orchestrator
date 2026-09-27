from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile

from paths import resolve_target


@dataclass(frozen=True)
class Receipt:
    path: str
    sha256: str
    generation: int


class ReceiptLedger:
    def __init__(self, root):
        self.root = Path(root)
        self._latest = {}
        self._owners = {}

    def latest(self, run_id):
        return self._latest.get(run_id)

    def record(self, run_id, generation, path, content):
        if (not isinstance(run_id, str) or not run_id or type(generation) is not int
                or generation < 0 or not isinstance(content, str)):
            raise ValueError("invalid identity/content")
        target = resolve_target(self.root, path)
        data = content.encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        old = self._latest.get(run_id)
        if old is not None:
            if generation < old.generation:
                raise ValueError("late generation")
            if generation == old.generation:
                if path != old.path or sha != old.sha256:
                    raise ValueError("conflicting replay")
                if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                    raise ValueError("changed backing file")
                return old
        if path in self._owners and self._owners[path] != run_id:
            raise ValueError("path already owned")
        missing = []
        parent = target.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        temporary = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, delete=False) as stream:
                temporary = stream.name
                stream.write(data)
            os.replace(temporary, target)
        except BaseException:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
            for directory in missing:
                directory.rmdir()
            raise
        result = Receipt(path, sha, generation)
        self._latest[run_id] = result
        self._owners[path] = run_id
        return result
