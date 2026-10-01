"""Machine-local registry of side-by-side Codex installs certified for Herdr.

Herdr 0.9.0 starts ``codex`` from the pane shell's PATH (``agent start`` has no
``--executable``). The fleet therefore installs every Codex version it certifies
in its own directory and pins each Mission to one of them through PATH. The
operator's global ``codex`` can update freely; a Mission keeps the exact binary
its contract froze. New Missions use the latest certified version.

The registry is opt-in through ``FLEET_CODEX_ROOT``: unset or empty disables it,
so tests and CI never read a machine registry. It is append-only: a version a
Mission froze is never removed. A certification record is stored once, by the
SHA-256 of its canonical bytes, which is also its CAS id when a Mission pins it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping

import fleet_json

ROOT_ENV = "FLEET_CODEX_ROOT"
REGISTRY_SCHEMA = "fleet.codex.registry.v1"
RECORD_SCHEMA = "fleet.codex.certification.v1"
PACKAGE = "@openai/codex"
SEMVER = re.compile(r"\d+\.\d+\.\d+")
SHA256 = re.compile(r"[0-9a-f]{64}")
ENTRY_FIELDS = frozenset({"codex_version", "binary_sha256", "bin_dir", "startup_guard",
                          "herdr_version", "certification_sha256", "certified_at"})


class RegistryError(RuntimeError):
    pass


def from_environment(environment: Mapping[str, str]) -> "Registry | None":
    value = environment.get(ROOT_ENV)
    if not value:
        return None
    root = Path(value)
    if not root.is_absolute():
        raise RegistryError(f"{ROOT_ENV} must be absolute")
    return Registry(root)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


class Registry:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "registry.json"

    # Layout --------------------------------------------------------------
    def install_dir(self, version: str) -> Path:
        if not SEMVER.fullmatch(version):
            raise RegistryError("Codex version must be an exact semantic version")
        return self.root / "versions" / version

    def bin_dir(self, version: str) -> Path:
        return self.install_dir(version) / "bin"

    def records_dir(self) -> Path:
        return self.root / "certifications"

    # Reading -------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": REGISTRY_SCHEMA, "entries": [], "attempts": []}
        value = fleet_json.loads(self.path.read_bytes())
        if (not isinstance(value, dict) or value.get("schema_version") != REGISTRY_SCHEMA
                or not isinstance(value.get("entries"), list) or not isinstance(value.get("attempts"), list)):
            raise RegistryError("Codex registry is invalid")
        for entry in value["entries"]:
            self._validate_entry(entry)
        return value

    @staticmethod
    def _validate_entry(entry: Any) -> dict[str, Any]:
        if (not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS
                or not SEMVER.fullmatch(str(entry["codex_version"])) or not SEMVER.fullmatch(str(entry["herdr_version"]))
                or not SHA256.fullmatch(str(entry["binary_sha256"]))
                or not SHA256.fullmatch(str(entry["certification_sha256"]))
                or not isinstance(entry["startup_guard"], str) or not Path(str(entry["bin_dir"])).is_absolute()):
            raise RegistryError("Codex registry entry is invalid")
        return entry

    def latest_certified(self, *, guard: str | None = None, herdr_version: str | None = None) -> dict[str, Any] | None:
        entries = [e for e in self.load()["entries"]
                   if (guard is None or e["startup_guard"] == guard)
                   and (herdr_version is None or e["herdr_version"] == herdr_version)]
        return max(entries, key=lambda e: _version_key(e["codex_version"]), default=None)

    def by_certification(self, certification: str) -> dict[str, Any] | None:
        matches = [e for e in self.load()["entries"] if e["certification_sha256"] == certification]
        if len(matches) > 1:
            raise RegistryError("Codex certification is registered twice")
        return matches[0] if matches else None

    def record_bytes(self, certification: str) -> bytes:
        if not SHA256.fullmatch(certification):
            raise RegistryError("invalid certification id")
        raw = (self.records_dir() / f"{certification}.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != certification:
            raise RegistryError("certification record bytes changed")
        return raw

    def verify_install(self, entry: Mapping[str, Any]) -> Path:
        """The pinned binary must still be the certified bytes."""
        binary = Path(entry["bin_dir"]) / "codex"
        try:
            resolved = binary.resolve(strict=True)
        except OSError as exc:
            raise RegistryError("certified Codex install missing; reconcile without relaunch") from exc
        if file_sha256(resolved) != entry["binary_sha256"]:
            raise RegistryError("certified Codex binary changed since certification")
        return resolved

    # Writing -------------------------------------------------------------
    def _write(self, value: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f".registry.{os.getpid()}.tmp")
        temporary.write_bytes(fleet_json.canonical_bytes(value) + b"\n")
        os.replace(temporary, self.path)

    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = (self.root / ".registry.lock").open("a")
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    def store_record(self, record: Mapping[str, Any]) -> str:
        raw = fleet_json.canonical_bytes(dict(record))
        certification = hashlib.sha256(raw).hexdigest()
        directory = self.records_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{certification}.json"
        if not path.exists():
            path.write_bytes(raw)
        return certification

    def register(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Store a PASS certification record and append its entry once."""
        if record.get("schema_version") != RECORD_SCHEMA or record.get("status") != "PASS":
            raise RegistryError("only a PASS certification record can be registered")
        handle = self._locked()
        try:
            certification = self.store_record(record)
            value = self.load()
            existing = [e for e in value["entries"] if e["codex_version"] == record["codex_version"]
                        and e["startup_guard"] == record["startup_guard"]]
            if existing:
                return existing[0]
            entry = {"codex_version": record["codex_version"], "binary_sha256": record["binary_sha256"],
                     "bin_dir": record["bin_dir"], "startup_guard": record["startup_guard"],
                     "herdr_version": record["herdr_version"], "certification_sha256": certification,
                     "certified_at": record["finished_at"]}
            self._validate_entry(entry)
            value["entries"].append(entry)
            self._write(value)
            return entry
        finally:
            handle.close()

    def note_attempt(self, record: Mapping[str, Any]) -> str:
        """Keep failed certification attempts for diagnosis; never as entries."""
        handle = self._locked()
        try:
            certification = self.store_record(record)
            value = self.load()
            value["attempts"].append({"codex_version": record.get("codex_version"), "status": record.get("status"),
                                      "certification_sha256": certification,
                                      "at": record.get("finished_at")})
            self._write(value)
            return certification
        finally:
            handle.close()

    def install(self, version: str, run: Callable[..., subprocess.CompletedProcess[str]]) -> Path:
        """npm-install one exact version side by side; link bin/codex to its native binary."""
        prefix = self.install_dir(version)
        binaries = sorted(prefix.glob("node_modules/**/@openai/codex-darwin-*/vendor/*/bin/codex"))
        if not binaries:
            result = run(["npm", "install", "--prefix", str(prefix), "--no-audit", "--no-fund",
                          f"{PACKAGE}@{version}"], timeout=900)
            if result.returncode:
                raise RegistryError(f"npm install of Codex {version} failed: {(result.stderr or '')[-300:]}")
            binaries = sorted(prefix.glob("node_modules/**/@openai/codex-darwin-*/vendor/*/bin/codex"))
        if len(binaries) != 1:
            raise RegistryError("versioned Codex install has no single native binary")
        link = self.bin_dir(version) / "codex"
        link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if link.is_symlink() or link.exists():
            if link.resolve() != binaries[0].resolve():
                raise RegistryError("versioned Codex link points elsewhere")
        else:
            link.symlink_to(binaries[0])
        observed = run([str(link), "--version"], timeout=60)
        if observed.returncode or (observed.stdout or "").strip() != f"codex-cli {version}":
            raise RegistryError(f"versioned Codex reports {(observed.stdout or '').strip()!r}, expected {version}")
        return link


def now() -> str:
    return datetime.now(timezone.utc).isoformat()
