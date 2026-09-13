"""Creation and reader contracts without a CLI or external provider."""
import copy
from pathlib import Path
import unittest
from unittest import mock
import uuid

from tests import test_fleet_herdr_mission as fixtures
from tests.test_mission_run import mission_run
import fleet_herdr_control as control
import fleet_herdr_mission as driver
import fleet_herdr_report as report
import fleet_mission_capsule as capsule
import fleet_chatgpt_provider as provider
import fleet_json
import fleet_mission


class MediationContractTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.HerdrMissionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.manifest = {'schema': capsule.SCHEMA, 'cli_version': capsule.capsule.CODEX_VERSION,
            'images': {name: {'path': str(self.f.tmp/name), 'sha256': 'a'*64}
                       for name in ('codex', 'codex-code-mode-host')},
            'codex_home': str(self.f.tmp/'synthetic-home'),
            'provider': provider.descriptor(provider.SYNTHETIC_ACCOUNT, provider.FIXTURE)}
        self.options = {'herdr_session': 'contract-fixture', 'herdr_capsule_manifest': self.manifest,
                        'timeout_seconds': 7200}
        self.mid = self.f.create(options=self.options, key='mediation-contract')
        self.controller = driver._Driver(self.f.runs, self.mid)
        self.controller.load()
        self.controller.prepare_candidate()
        self.backend = capsule.CapsuleBackend(self.f.runs, self.mid, feature='driver-test',
            target_repo=self.controller.candidate, compiled=self.f.compiled,
            session='contract-fixture', manifest=self.manifest)
        self.saved = self.backend.boot()

    def test_candidate_cannot_be_replaced_by_source_checkout(self):
        with self.assertRaisesRegex(capsule.inference.InferenceError, 'candidate'):
            capsule.CapsuleBackend(self.f.runs, self.mid, feature='driver-test',
                target_repo=self.f.target, compiled=self.f.compiled,
                session='contract-fixture', manifest=self.manifest)

    def test_version_and_legacy_manifests_require_new_explicit_binding(self):
        for field, value in [('cli_version', '0.153.4'), ('schema', 'fleet.mission.capsule.v1')]:
            changed = copy.deepcopy(self.manifest); changed[field] = value
            with self.assertRaises(capsule.inference.InferenceError):
                capsule.validate_manifest(changed)

    def test_readers_validate_generation_and_never_start_a_process(self):
        readers = [lambda: control.backend_generation(self.f.runs, self.mid),
                   lambda: mission_run.mission_status(self.f.runs, self.mid)]
        with mock.patch('subprocess.run', side_effect=AssertionError('reader started process')):
            for read in readers:
                read()
            changed = copy.deepcopy(self.saved); changed['generation'] = str(uuid.uuid4())
            (self.f.runs/'missions'/self.mid/'herdr-backend.json').write_bytes(fleet_json.canonical_bytes(changed)+b'\n')
            for read in readers:
                with self.assertRaises(ValueError):
                    read()

    def test_creation_options_cannot_select_a_different_executor_after_boot(self):
        changed = copy.deepcopy(self.options); changed.pop('herdr_capsule_manifest')
        (self.f.runs/'missions'/self.mid/'runtime-options.json').write_bytes(fleet_json.canonical_bytes(changed)+b'\n')
        with self.assertRaises((ValueError, capsule.inference.InferenceError)):
            control.backend_generation(self.f.runs, self.mid)

    def test_cli_creation_freezes_capsule_without_selecting_personal_lane(self):
        runtime = self.f.tmp/'private-runtime'; runtime.mkdir(mode=0o700)
        contract = {'schema_version': 1, 'requirements': [{'id':'answer', 'description':'answer exists',
            'checks':[{'kind':'text_contains','path':'answer.txt','expected':'implemented'}]}]}
        with mock.patch.object(capsule, 'validate_manifest', return_value=self.manifest), \
             mock.patch.object(mission_run, 'drive_mission', side_effect=lambda runs, mid: {'mission_id':mid}):
            result = mission_run.create_and_drive(self.f.runs, feature='confined-cli',
                objective='Implement local answer', workflow_name='herdr-implementation',
                target_repo=self.f.target, risk_override='low', timeout_seconds=7200,
                allow_dirty_baseline=False, teardown=False, herdr_session='contract-fixture',
                herdr_runtime_root=str(runtime), herdr_capsule_manifest=self.manifest,
                acceptance_contract=contract)
        options = capsule.read_json(self.f.runs, result['mission_id'], 'runtime-options.json')
        creation = capsule.read_json(self.f.runs, result['mission_id'], 'creation-request.json')
        self.assertEqual(options, creation['runtime_options'])
        self.assertEqual(options['herdr_capsule_manifest'], self.manifest)
        self.assertNotIn('herdr_personal_cli', options)
        self.assertNotIn('herdr_launch_manifest', options)


if __name__ == '__main__':
    unittest.main()
