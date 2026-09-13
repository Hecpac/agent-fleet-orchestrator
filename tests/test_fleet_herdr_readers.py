"""Offline readers enforce the same frozen CLI contract before reporting identity."""
import copy
import unittest
import uuid
from unittest import mock

from tests import test_fleet_herdr_mission as mission_fixtures
from tests.test_mission_run import mission_run
import fleet_herdr_control as control
import fleet_herdr_versions as versions
import fleet_json
import fleet_mission
import fleet_mission_state
import fleet_report
import fleet_safe_paths


class HerdrReaderContractTests(unittest.TestCase):
    def setUp(self):
        helper = mission_fixtures.HerdrMissionTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.runs, self.mid = helper.runs, helper.mid
        self.compiled, _ = fleet_mission.load_mission_compiled(self.runs, self.mid, mode="read")
        self.root = self.runs / "missions" / self.mid
        self.path = self.root / "herdr-backend.json"
        self.anchor = self.root / "herdr-runtime-contract.json"
        self.backend = {"schema_version": 3, "backend_version": versions.HERDR_VERSION,
            "runtime_contract": dict(versions.OFFICIAL_CONTRACT), "mission_id": self.mid,
            "compiled_digest": self.compiled["compiled_digest"], "generation": str(uuid.uuid4()),
            "session": "offline-fixture", "phase": "new", "workspace": {}, "members": [], "submissions": {}}
        self.write(self.path, fleet_json.canonical_bytes(self.backend) + b"\n")
        self.write(self.anchor, versions.anchor_bytes(self.backend))
        self.readers = {"status": lambda: mission_run.mission_status(self.runs, self.mid),
            "generation": lambda: control.backend_generation(self.runs, self.mid),
            "report": lambda: fleet_report.build_report(self.runs, self.mid)}

    @staticmethod
    def write(path, raw):
        path.write_bytes(raw)
        path.chmod(0o600)

    def test_current_and_historical_readers_are_offline_and_do_not_mutate_records(self):
        for legacy in (False, True):
            if legacy:
                self.backend.update(schema_version=2, backend_version="0.8.2")
                self.backend.pop("runtime_contract")
                self.anchor.unlink()
                self.write(self.path, fleet_json.canonical_bytes(self.backend) + b"\n")
            before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
            with mock.patch("subprocess.run", side_effect=AssertionError("reader called a runtime")):
                for name, read in self.readers.items():
                    with self.subTest(legacy=legacy, reader=name):
                        result = read()
                        if name == "generation":
                            self.assertEqual(result, self.backend["generation"])
            self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_all_readers_reject_changed_missing_or_downgraded_runtime(self):
        mutations = [lambda b: b.update(mission_id=str(uuid.uuid4())),
            lambda b: b.update(compiled_digest="0" * 64),
            lambda b: b["runtime_contract"].update(codex_version="0.153.0"),
            lambda b: b["runtime_contract"].update(herdr_version="0.8.2"),
            lambda b: b.update(schema_version=True),
            lambda b: b.pop("runtime_contract"),
            lambda b: (b.update(schema_version=2, backend_version="0.8.2"), b.pop("runtime_contract"))]
        for index, mutate in enumerate(mutations):
            value = copy.deepcopy(self.backend)
            mutate(value)
            self.write(self.path, fleet_json.canonical_bytes(value) + b"\n")
            for name, read in self.readers.items():
                with self.subTest(mutation=index, reader=name), self.assertRaises(ValueError):
                    read()

    def test_all_readers_reject_missing_or_foreign_anchor_and_orphaned_anchor(self):
        good_state = self.path.read_bytes()
        good_anchor = self.anchor.read_bytes()
        for variation in ("missing", "foreign", "orphaned"):
            self.write(self.path, good_state)
            self.write(self.anchor, good_anchor)
            if variation == "missing":
                self.anchor.unlink()
            elif variation == "foreign":
                value = fleet_json.loads(good_anchor)
                value["mission_id"] = str(uuid.uuid4())
                self.write(self.anchor, fleet_json.canonical_bytes(value) + b"\n")
            else:
                self.path.unlink()
            for name, read in self.readers.items():
                with self.subTest(variation=variation, reader=name), self.assertRaises(ValueError):
                    read()

    def test_aliases_and_noncanonical_state_do_not_bypass_readers(self):
        for target in (self.path, self.anchor):
            raw = target.read_bytes()
            copy_path = self.root / "alias-target.json"
            self.write(copy_path, raw)
            target.unlink()
            target.symlink_to(copy_path)
            for name, read in self.readers.items():
                with self.subTest(alias=target.name, reader=name), self.assertRaises(fleet_safe_paths.SafePathError):
                    read()
            target.unlink()
            self.write(target, raw)
            copy_path.unlink()
        self.write(self.path, b" " + self.path.read_bytes())
        for name, read in self.readers.items():
            with self.subTest(reader=name), self.assertRaisesRegex(ValueError, "canonical"):
                read()

    def test_cancel_rejects_bad_anchor_before_writing_a_control_request(self):
        ledger = fleet_mission_state.ledger_path(self.runs, self.mid)
        before = ledger.read_bytes()
        self.anchor.unlink()
        with self.assertRaisesRegex(ValueError, "frozen runtime"):
            control.request(self.runs, self.mid, action="cancel", reason="fixture",
                            idempotency_key="must-not-be-written")
        self.assertEqual(ledger.read_bytes(), before)
