"""Certified side-by-side Codex registry and per-Mission PATH pinning; offline."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_codex_registry as registry  # noqa: E402
import fleet_herdr  # noqa: E402
import fleet_herdr_versions as versions  # noqa: E402
from tests import test_fleet_herdr as backend_tests  # noqa: E402


def fake_install(root: Path, version: str, content: bytes) -> Path:
    bin_dir = root / "versions" / version / "bin"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "codex"
    binary.write_bytes(content)
    binary.chmod(0o755)
    return bin_dir


def record(bin_dir: Path, version: str, *, status: str = "PASS") -> dict:
    return {"schema_version": registry.RECORD_SCHEMA, "status": status, "codex_version": version,
            "binary_sha256": hashlib.sha256((bin_dir / "codex").read_bytes()).hexdigest(),
            "bin_dir": str(bin_dir), "startup_guard": "codex-0.159-v1", "herdr_version": "0.9.0",
            "finished_at": "2026-10-01T00:00:00+00:00"}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "fleet-codex"
        self.store = registry.Registry(self.root)

    def test_activation_is_explicit(self):
        self.assertIsNone(registry.from_environment({}))
        self.assertIsNone(registry.from_environment({"FLEET_CODEX_ROOT": ""}))
        with self.assertRaises(registry.RegistryError):
            registry.from_environment({"FLEET_CODEX_ROOT": "relative"})
        self.assertEqual(registry.from_environment({"FLEET_CODEX_ROOT": str(self.root)}).root, self.root)

    def test_register_latest_and_lookup(self):
        old = self.store.register(record(fake_install(self.root, "0.159.3", b"a"), "0.159.3"))
        new = self.store.register(record(fake_install(self.root, "0.160.0", b"b"), "0.160.0"))
        self.assertEqual(self.store.latest_certified()["codex_version"], "0.160.0")
        self.assertEqual(self.store.by_certification(old["certification_sha256"]), old)
        raw = self.store.record_bytes(new["certification_sha256"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), new["certification_sha256"])
        self.assertEqual(self.store.register(record(Path(new["bin_dir"]), "0.160.0")), new)
        self.assertEqual(len(self.store.load()["entries"]), 2)

    def test_failed_attempts_never_become_entries(self):
        bin_dir = fake_install(self.root, "0.161.0", b"c")
        with self.assertRaises(registry.RegistryError):
            self.store.register(record(bin_dir, "0.161.0", status="FAIL"))
        self.store.note_attempt(record(bin_dir, "0.161.0", status="FAIL"))
        value = self.store.load()
        self.assertEqual((value["entries"], value["attempts"][0]["status"]), ([], "FAIL"))

    def test_changed_binary_or_record_is_detected(self):
        entry = self.store.register(record(fake_install(self.root, "0.159.3", b"a"), "0.159.3"))
        self.assertEqual(self.store.verify_install(entry).name, "codex")
        (Path(entry["bin_dir"]) / "codex").write_bytes(b"tampered")
        with self.assertRaisesRegex(registry.RegistryError, "changed since certification"):
            self.store.verify_install(entry)
        path = self.root / "certifications" / f"{entry['certification_sha256']}.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(registry.RegistryError, "record bytes changed"):
            self.store.record_bytes(entry["certification_sha256"])


class BackendPinningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = backend_tests.HerdrBackendTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.tmp / "fleet-codex"
        self.store = registry.Registry(self.root)
        self.old = self.store.register(record(fake_install(self.root, "0.159.3", b"old"), "0.159.3"))
        self.new = self.store.register(record(fake_install(self.root, "0.160.0", b"new"), "0.160.0"))

    def personal(self, **kwargs):
        return fleet_herdr.HerdrBackend(self.fixture.runs, self.fixture.mission_id, session="mission-control-test",
                                        feature="herdr-test", target_repo=self.fixture.target,
                                        compiled=self.fixture.compiled, personal_cli=True,
                                        environment={"PATH": "/usr/bin:/bin"}, run_command=self.fixture.fake,
                                        **kwargs)

    def test_new_mission_pins_the_latest_certified_install(self):
        backend = self.personal(codex_registry=self.store)
        contract = backend.initial_runtime_contract
        self.assertTrue(versions.certified(contract))
        self.assertEqual((contract["codex_version"], contract["certification"]),
                         ("0.160.0", self.new["certification_sha256"]))
        self.assertEqual(backend.environment["PATH"], self.new["bin_dir"] + ":/usr/bin:/bin")
        backend._verify_codex_pin(contract)

    def test_existing_mission_keeps_its_frozen_install(self):
        backend = self.personal(codex_registry=self.store)
        frozen = versions.certified_contract(self.old)
        backend._resolve_pin(frozen)
        self.assertEqual(backend.environment["PATH"], self.old["bin_dir"] + ":/usr/bin:/bin")
        backend._verify_codex_pin(frozen)
        backend._resolve_pin(versions.CURRENT_CONTRACT)
        self.assertEqual(backend.environment["PATH"], "/usr/bin:/bin")

    def test_missing_or_changed_install_refuses(self):
        backend = self.personal(codex_registry=self.store)
        unknown = versions.certified_contract({**self.old, "certification_sha256": "f" * 64})
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "install missing"):
            backend._resolve_pin(unknown)
        (Path(self.new["bin_dir"]) / "codex").write_bytes(b"replaced")
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "changed since certification"):
            backend._verify_codex_pin(backend.initial_runtime_contract)

    def test_boot_freezes_certified_contract_and_its_record_in_cas(self):
        self.fixture.fake.codex_version = "0.160.0"
        backend = fleet_herdr.HerdrBackend(self.fixture.runs, self.fixture.mission_id, session="mission-control-test",
                                           feature="herdr-test", target_repo=self.fixture.target,
                                           compiled=self.fixture.compiled, environment={"PATH": "/usr/bin:/bin"},
                                           run_command=self.fixture.fake,
                                           codex_candidate={k: self.new[k] for k in ("codex_version", "binary_sha256",
                                                            "bin_dir", "startup_guard", "herdr_version")})
        state = backend.boot()
        self.assertEqual(state["runtime_contract"]["certification"], versions.CANDIDATE_CERTIFICATION)
        self.assertEqual(state["runtime_contract"]["codex_version"], "0.160.0")
        # A different controller cannot reuse a candidate contract outside its run.
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "certification run"):
            self.fixture.backend().boot()

    def test_registry_pinning_requires_the_personal_lane(self):
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "personal CLI lane"):
            fleet_herdr.HerdrBackend(self.fixture.runs, self.fixture.mission_id, session="mission-control-test",
                                     feature="herdr-test", target_repo=self.fixture.target,
                                     compiled=self.fixture.compiled, environment={"PATH": "/usr/bin:/bin"},
                                     run_command=self.fixture.fake, codex_registry=self.store)


if __name__ == "__main__":
    unittest.main()
