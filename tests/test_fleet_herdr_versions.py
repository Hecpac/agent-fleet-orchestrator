"""Current CLI contract and offline legacy behavior; no external providers."""
import copy
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_artifacts
import fleet_herdr
import fleet_herdr_versions as versions
import fleet_json
from tests import test_fleet_herdr as backend_tests


class VersionContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = backend_tests.HerdrBackendTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def write_state(self, backend, value):
        (self.fixture.runs / backend.relative).write_bytes(fleet_json.canonical_bytes(value) + b"\n")

    def legacy(self):
        backend = self.fixture.booted()
        value = backend.state()
        value.update(schema_version=2, backend_version="0.8.2")
        value.pop("runtime_contract")
        # Construct an actual pre-upgrade fixture, where this file never existed.
        (self.fixture.runs / backend.runtime_relative).unlink()
        self.write_state(backend, value)
        return backend, value

    def test_new_boot_pins_exact_official_pair(self):
        backend = self.fixture.booted()
        value = backend.state()
        self.assertEqual(value["schema_version"], 3)
        self.assertEqual(value["runtime_contract"], versions.CURRENT_CONTRACT)
        self.assertEqual(value["backend_version"], "0.9.0")
        self.assertTrue((self.fixture.runs / backend.runtime_relative).is_file())

    def test_wrong_cli_pair_fails_before_workspace_or_state_creation(self):
        fake = self.fixture.fake
        for herdr, codex in (("0.8.2", "0.159.3"), ("0.9.0", "0.154.0"), ("0.9.1", "0.159.3")):
            with self.subTest(herdr=herdr, codex=codex):
                fake.calls.clear();fake.version=herdr;fake.codex_version=codex
                backend=self.fixture.backend()
                with self.assertRaises(fleet_herdr.HerdrBackendError):backend.boot()
                self.assertFalse((self.fixture.runs / backend.relative).exists())
                self.assertFalse(any("workspace" in c for c in fake.calls))

    def test_legacy_read_is_offline_and_effects_require_legacy_version(self):
        backend, value = self.legacy()
        raw=(self.fixture.runs / backend.relative).read_bytes()
        self.fixture.fake.calls.clear()
        self.assertEqual(self.fixture.backend().state(), value)
        self.assertEqual(self.fixture.fake.calls, [])
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, "0.8.2"):
            self.fixture.backend().boot()
        self.assertEqual((self.fixture.runs / backend.relative).read_bytes(), raw)
        self.fixture.fake.version="0.8.2"
        self.assertEqual(self.fixture.backend().boot(), value)
        self.assertEqual((self.fixture.runs / backend.relative).read_bytes(), raw)

    def test_new_record_cannot_be_downgraded_to_readable_legacy(self):
        backend=self.fixture.booted();original=backend.state()
        variants=[]
        downgraded=copy.deepcopy(original)
        downgraded.update(schema_version=2,backend_version="0.8.2")
        downgraded.pop("runtime_contract");variants.append(downgraded)
        for change in ({"codex_version":"0.153.0"}, {"herdr_version":"0.8.2"},
                       {"version":True}, {"lane":"instrumented"}, {"startup_guard":"none"}):
            changed=copy.deepcopy(original);changed["runtime_contract"].update(change);variants.append(changed)
        missing=copy.deepcopy(original);missing.pop("runtime_contract");variants.append(missing)
        for value in variants:
            with self.subTest(value=value.get("runtime_contract")):
                self.write_state(backend,value)
                with self.assertRaises(fleet_herdr.HerdrBackendError):self.fixture.backend().state()

    def test_missing_frozen_contract_or_missing_state_fails_closed(self):
        backend=self.fixture.booted();anchor=self.fixture.runs/backend.runtime_relative
        saved=anchor.read_bytes();anchor.unlink()
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError,"frozen runtime"):
            backend.state()
        anchor.write_bytes(saved);anchor.chmod(0o600)
        (self.fixture.runs/backend.relative).unlink()
        self.fixture.fake.calls.clear()
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError,"state missing"):
            backend.boot()
        self.assertEqual(self.fixture.fake.calls,[])

    def test_observed_codex_version_change_blocks_new_submission_without_intent(self):
        backend=self.fixture.booted();self.fixture.fake.codex_version="0.153.0"
        run=str(uuid.uuid4());self.fixture.fake.calls.clear()
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError,"Codex CLI version"):
            backend.submit(run,self.fixture.prompt(run))
        self.assertEqual(backend.state()["submissions"],{})
        self.assertFalse(any("prompt" in c for c in self.fixture.fake.calls))

    def test_ready_receipt_with_trust_or_resume_dialog_never_sends_or_reserves(self):
        backend=self.fixture.booted()
        for dialog in ("Do you trust the contents of this directory?", "Resuming session…", "Hooks need review"):
            with self.subTest(dialog=dialog):
                self.fixture.fake.screen="› Ask Codex to do anything\n"+dialog
                self.fixture.fake.calls.clear();run=str(uuid.uuid4())
                with self.assertRaisesRegex(fleet_herdr.HerdrBackendError,"delivery blocked"):
                    backend.submit(run,self.fixture.prompt(run))
                self.assertEqual(backend.state()["submissions"],{})
                self.assertFalse(any("prompt" in c or "send-keys" in c for c in self.fixture.fake.calls))

    def test_restored_terminal_identity_is_not_silently_rebound(self):
        backend=self.fixture.booted();member=backend.state()["members"][0]
        self.fixture.fake.agent_bindings[member["agent_name"]]["terminal_id"]="replacement-terminal"
        self.fixture.fake.calls.clear();run=str(uuid.uuid4())
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError,"identity mismatch"):
            backend.submit(run,self.fixture.prompt(run))
        self.assertEqual(backend.state()["submissions"],{})
        self.assertFalse(any("prompt" in c for c in self.fixture.fake.calls))

    def test_preblocked_agent_records_one_terminal_refusal_without_sending(self):
        backend = self.fixture.booted()
        member = backend.state()["members"][0]
        self.fixture.fake.agent_states[member["agent_name"]] = "blocked"
        self.fixture.fake.calls.clear()
        run = str(uuid.uuid4())
        result = backend.submit(run, self.fixture.prompt(run))
        self.assertEqual((result["phase"], result["status"]), ("terminal", "blocked"))
        self.assertEqual(backend.state()["submissions"], {run: result})
        self.assertFalse(any("prompt" in c or "send-keys" in c for c in self.fixture.fake.calls))
        self.assertEqual(backend.submit(run, self.fixture.prompt(run)), result)
        self.assertEqual(len(backend.state()["submissions"]), 1)

    def test_native_transcript_version_must_match_contract(self):
        backend,run,path=self.fixture.permission_result_fixture()
        rows=fleet_json.load_jsonl(path.read_bytes());rows[0]["payload"]["cli_version"]="0.153.0"
        path.write_bytes(b"".join(fleet_json.canonical_bytes(r)+b"\n" for r in rows))
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError,"CLI version differs"):
            backend.collect_result(run)
        self.assertFalse((self.fixture.runs/backend._result_relative(run)).exists())

    def test_cached_result_rejects_runtime_contract_removed_even_if_resealed(self):
        backend,run,_=self.fixture.permission_result_fixture()
        result=backend.collect_result(run)
        self.assertEqual(result["evidence"]["runtime_contract"],versions.CURRENT_CONTRACT)
        result["evidence"].pop("runtime_contract");result.pop("result_artifact_id")
        result["result_artifact_id"]=fleet_artifacts.put_bytes(self.fixture.runs,self.fixture.mission_id,
            fleet_json.canonical_bytes(result))["artifact_id"]
        (self.fixture.runs/backend._result_relative(run)).write_bytes(fleet_json.canonical_bytes(result)+b"\n")
        self.fixture.fake.calls.clear()
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError,"runtime contract changed"):
            backend.collect_result(run)
        self.assertEqual(self.fixture.fake.calls,[])

    def test_historical_completed_result_stays_readable_without_runtime(self):
        backend,run,_=self.fixture.permission_result_fixture()
        result=backend.collect_result(run)
        state=backend.state();state.update(schema_version=2,backend_version="0.8.2")
        state.pop("runtime_contract");(self.fixture.runs/backend.runtime_relative).unlink()
        result["evidence"].pop("runtime_contract")
        rows=fleet_json.load_jsonl(fleet_artifacts.get_bytes(self.fixture.runs,self.fixture.mission_id,
            result["evidence"]["transcript_artifact_id"]))
        rows[0]["payload"]["cli_version"]="0.153.0"
        pin=fleet_artifacts.put_bytes(self.fixture.runs,self.fixture.mission_id,
            b"".join(fleet_json.canonical_bytes(r)+b"\n" for r in rows))["artifact_id"]
        result["evidence"].update(transcript_artifact_id=pin,transcript_sha256=pin)
        result.pop("result_artifact_id")
        result["result_artifact_id"]=fleet_artifacts.put_bytes(self.fixture.runs,self.fixture.mission_id,
            fleet_json.canonical_bytes(result))["artifact_id"]
        state["submissions"][run]["result_artifact_id"]=result["result_artifact_id"]
        self.write_state(backend,state)
        path=self.fixture.runs/backend._result_relative(run)
        path.write_bytes(fleet_json.canonical_bytes(result)+b"\n");before=path.read_bytes()
        self.fixture.fake.calls.clear();self.fixture.fake.version="unavailable"
        self.assertEqual(self.fixture.backend().collect_result(run),result)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.fixture.fake.calls,[])
