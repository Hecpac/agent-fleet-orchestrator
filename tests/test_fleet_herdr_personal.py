"""Personal CLI routing with a fake Herdr transport; no provider calls."""
from pathlib import Path
import json
from types import SimpleNamespace
import uuid
import unittest
from unittest import mock

from tests import test_fleet_herdr_mission as fixtures
from tests import test_fleet_herdr as transport
import fleet_herdr as herdr
import fleet_herdr_mission as driver
import fleet_herdr_personal as personal
import fleet_chatgpt_provider as subscription
import fleet_herdr_control as control


class PersonalTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.HerdrMissionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        mock.patch.stopall()
        self.options={'herdr_session':'mission-control-test','herdr_personal_cli':personal.PROFILE,'timeout_seconds':7200}
        self.mid=self.f.create(options=self.options,key='personal-fixture')
        self.controller=driver._Driver(self.f.runs,self.mid);self.controller.load();self.controller.prepare_candidate()
        self.controller.event('fleet_boot_started','boot',{'feature':'driver-test','preset':'astra_sol'})
        self.fake=transport.FakeHerdr()
        self.fake.codex_version="0.154.0"
        self.fake.screen="OpenAI Codex (v0.154.0)\n› Ask Codex to do anything\n"
        self.backend=herdr.HerdrBackend(self.f.runs,self.mid,feature='driver-test',target_repo=self.controller.candidate,
            compiled=self.f.compiled,session='mission-control-test',personal_cli=True,
            environment={'PATH':'/usr/bin:/bin','CODEX_HOME':'/synthetic/default-codex-home'})

    def test_official_boot_uses_native_cli_and_default_home_without_adapter(self):
        with mock.patch.object(subscription,'ChatGPTProvider',side_effect=AssertionError('adapter must not run')), \
             mock.patch.object(herdr.subprocess,'run',side_effect=self.fake):
            result=self.backend.boot()
        self.assertEqual(result['phase'],'ready')
        calls=self.fake.calls
        starts=[c for c in calls if c[3:5]==['agent','start']]
        self.assertEqual(len(starts),4)
        self.assertTrue(all('--executable' not in c and '--fleet-launch-intent' not in c for c in starts))
        self.assertFalse(any('fleet-local' in str(c) for c in calls))
        creates=[c for c in calls if c[3:5]==['workspace','create']]
        self.assertIn('CODEX_HOME=/synthetic/default-codex-home',creates[0])

    def test_profile_requires_creation_and_exact_candidate(self):
        self.backend.target_repo=self.f.target
        with self.assertRaises(ValueError):personal.require(self.backend)
        self.backend.target_repo=self.controller.candidate
        with self.assertRaises(ValueError):personal.require_command(self.backend,['herdr','--session','other','agent','start','x'],'herdr')
        with self.assertRaises(ValueError):personal.require_command(self.backend,['herdr','--session','mission-control-test','agent','future-operation','x'],'herdr')

    def test_prompt_requires_real_admission_before_transport(self):
        with self.assertRaises(ValueError),mock.patch.object(herdr.subprocess,'run') as execute:
            personal.require(self.backend,run_id='00000000-0000-0000-0000-000000000001',prompt_sha256='a'*64,instance_id='worker')
        execute.assert_not_called()

    def test_new_mission_defaults_to_personal_cli_and_persists_selection(self):
        from tests.test_mission_run import mission_run
        contract={'schema_version':1,'requirements':[{'id':'answer','description':'answer exists',
            'checks':[{'kind':'text_contains','path':'answer.txt','expected':'implemented'}]}]}
        with mock.patch.object(mission_run,'drive_mission',side_effect=lambda runs,mid:{'mission_id':mid}), \
             mock.patch.object(subscription,'ChatGPTProvider',side_effect=AssertionError('adapter must not run')):
            result=mission_run.create_and_drive(self.f.runs,feature='personal-default',objective='Implement local answer',
                workflow_name='herdr-implementation',target_repo=self.f.target,risk_override='low',timeout_seconds=7200,
                allow_dirty_baseline=False,teardown=False,herdr_session='mission-control-test',acceptance_contract=contract)
        selected=driver._Driver(self.f.runs,result['mission_id']).read('runtime-options.json')
        self.assertEqual(selected['herdr_personal_cli'],personal.PROFILE)
        self.assertNotIn('herdr_capsule_manifest',selected)
        self.assertNotIn('herdr_launch_manifest',selected)

    def recovered_backend(self):
        return herdr.HerdrBackend(
            self.f.runs, self.mid, feature='driver-test', target_repo=self.controller.candidate,
            compiled=self.f.compiled, session='mission-control-test', personal_cli=True,
            environment=self.backend.environment, transcript_resolver=self.transcripts.get,
        )

    def uncertain_writer(self):
        actual_run = herdr.subprocess.run

        def fake_runtime(command, **kwargs):
            if command[0] in {'herdr', 'codex'}:
                return self.fake(command, **kwargs)
            return actual_run(command, **kwargs)

        patcher = mock.patch.object(herdr.subprocess, 'run', side_effect=fake_runtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.transcripts = {}
        self.backend.transcript_resolver = self.transcripts.get
        self.backend.boot()
        self.controller.event('mission_running', 'running', {'manifest': str(self.controller.root / 'herdr-backend.json')})
        control.enable(self.f.runs, self.mid)
        self.fake.lose_prompt_ack = True
        with self.assertRaisesRegex(RuntimeError, 'ACK lost after runtime accepted prompt'):
            self.controller.turn(self.backend, 'build', 'worker', 'build', [], None)
        admission = next(a for a in self.controller.current()['admissions'].values() if a['writer'])
        run_id = admission['run_id']
        self.backend = self.recovered_backend()
        recovered = self.backend.recover(run_id)
        self.assertEqual(recovered['status'], 'indeterminate')
        self.assertTrue(admission['active'])
        self.assertTrue(self.controller.current()['active_writer'])
        member = next(m for m in self.backend.state()['members'] if m['instance_id'] == 'worker')
        prompt = driver.fleet_artifacts.get_bytes(self.f.runs, self.mid, admission['task_sha256']).decode()
        self.transcript_fixture = SimpleNamespace(
            tmp=self.f.tmp, target=self.controller.candidate, fake=self.fake, transcripts=self.transcripts,
        )
        transport.HerdrBackendTests.write_active_transcript(self.transcript_fixture, member=member, prompt=prompt)
        return run_id, member, prompt

    def resume_cancellation(self):
        with mock.patch.object(driver._Driver, 'backend', side_effect=self.recovered_backend):
            return driver.drive(self.f.runs, self.mid)

    def test_uncertain_writer_cancel_keeps_admission_until_quiescence(self):
        self.assert_uncertain_writer_cancellation()

    def test_uncertain_writer_lost_cancel_ack_reobserves_without_resending(self):
        self.assert_uncertain_writer_cancellation(lose_cancel_ack=True)

    def assert_uncertain_writer_cancellation(self, *, lose_cancel_ack=False):
        run_id, member, _ = self.uncertain_writer()
        self.fake.cancel_quiescent = False
        self.fake.wait_timeout = True
        self.fake.lose_cancel_ack = lose_cancel_ack
        request = control.request(self.f.runs, self.mid, action='cancel', reason='fixture cancel',
                                  idempotency_key='uncertain-cancel', run_id=run_id)
        repeated = control.request(self.f.runs, self.mid, action='cancel', reason='fixture cancel',
                                   idempotency_key='uncertain-cancel', run_id=run_id)
        self.assertEqual(request['request_id'], repeated['request_id'])
        self.assertFalse(repeated['recorded'])
        if lose_cancel_ack:
            with self.assertRaisesRegex(RuntimeError, 'ACK lost after runtime accepted cancellation'):
                self.resume_cancellation()
        for _ in range(2):
            self.resume_cancellation()
            current = self.controller.current()
            admission = next(a for a in current['admissions'].values() if a['run_id'] == run_id)
            self.assertTrue(admission['active'])
            self.assertTrue(current['active_writer'])
            self.assertIsNone(admission['terminal'])
            self.assertEqual(control.view(current)['desired'], 'cancel_requested')
            self.assertEqual(control.view(current)['applied'], 'running')
        operations = [self.fake.operation(c) for c in self.fake.calls]
        self.assertEqual(
            [c for c in operations if c[1:3] == ['agent', 'send-keys']],
            [['herdr', 'agent', 'send-keys', member['agent_name'], 'ctrl+c']],
        )
        self.fake.agent_states[member['agent_name']] = 'idle'
        result = self.resume_cancellation()
        current = self.controller.current()
        admission = next(a for a in current['admissions'].values() if a['run_id'] == run_id)
        self.assertEqual(result['status'], 'abandoned')
        self.assertFalse(admission['active'])
        self.assertFalse(current['active_writer'])
        self.assertEqual(admission['terminal']['status'], 'abandoned')
        self.assertEqual(control.view(current)['applied'], 'cancelled')
        calls = list(self.fake.calls)
        self.assertEqual(self.resume_cancellation()['status'], 'abandoned')
        self.assertEqual(self.fake.calls, calls)
        operations = [self.fake.operation(c) for c in calls]
        self.assertEqual(sum(c[1:3] == ['agent', 'prompt'] for c in operations), 1)
        self.assertEqual(sum(c[1:3] == ['agent', 'send-keys'] for c in operations), 1)

    def test_uncertain_writer_late_result_precedes_cancel(self):
        self.assert_late_writer_result_wins()

    def test_historical_indeterminate_with_durable_result_stays_read_only(self):
        self.assert_late_writer_result_wins(lose_result_save=True)

    def assert_late_writer_result_wins(self, *, lose_result_save=False):
        run_id, member, prompt = self.uncertain_writer()
        task = json.loads(prompt)
        final = {
            **{k: task['result_contract'][k] for k in ('schema_version', 'mission_id', 'run_id', 'instance_id', 'candidate_tree_sha')},
            'status': 'PASS', 'summary': 'Synthetic late result with existing file evidence',
            'artifacts': [{'path': 'README.md', 'sha256': driver.state.artifact_id((self.controller.candidate / 'README.md').read_bytes())}],
        }
        final_text = transport.HerdrBackendTests.write_transcript(
            self.transcript_fixture, agent_session=member['agent_session']['value'], member=member,
            prompt=prompt, final=final, turn_id='turn-active',
        )
        if lose_result_save:
            with mock.patch.object(self.backend, '_save', side_effect=RuntimeError('result persisted before backend save')):
                with self.assertRaisesRegex(RuntimeError, 'result persisted before backend save'):
                    self.backend.collect_result(run_id)
            self.assertEqual(self.backend.state()['submissions'][run_id]['status'], 'indeterminate')
        control.request(self.f.runs, self.mid, action='cancel', reason='fixture cancel',
                        idempotency_key='late-result-cancel', run_id=run_id)
        with mock.patch.object(herdr.HerdrBackend, 'cancel', side_effect=AssertionError('validated result must win')):
            result = self.resume_cancellation()
        current = self.controller.current()
        admission = next(a for a in current['admissions'].values() if a['run_id'] == run_id)
        self.assertEqual(result['status'], 'abandoned')
        self.assertFalse(admission['active'])
        self.assertFalse(current['active_writer'])
        self.assertEqual(admission['terminal']['status'], 'succeeded')
        self.assertIsNotNone(admission['result'])
        cached = self.recovered_backend().collect_result(run_id)
        self.assertEqual(cached['status'], 'PASS')
        self.assertEqual(cached['turn_id'], 'turn-active')
        self.assertEqual(cached['evidence']['agent_session'], member['agent_session'])
        self.assertEqual(driver.fleet_artifacts.get_bytes(self.f.runs, self.mid, cached['artifact_id']).decode(), final_text)
        self.assertEqual(self.recovered_backend().collect_result(run_id), cached)
        if lose_result_save:
            self.assertEqual(self.backend.state()['submissions'][run_id]['status'], 'indeterminate')
        calls = list(self.fake.calls)
        self.assertEqual(self.resume_cancellation()['status'], 'abandoned')
        self.assertFalse(control.request(self.f.runs, self.mid, action='cancel', reason='historical',
                                        idempotency_key='historical-cancel', run_id=run_id)['recorded'])
        self.assertEqual(self.fake.calls, calls)
        operations = [self.fake.operation(c) for c in calls]
        self.assertEqual(sum(c[1:3] == ['agent', 'prompt'] for c in operations), 1)
        self.assertFalse(any(c[1:3] == ['agent', 'send-keys'] for c in operations))

    def test_uncertain_writer_rejects_cancel_generation_and_run_mismatch(self):
        run_id, _, _ = self.uncertain_writer()
        calls = list(self.fake.calls)
        with self.assertRaisesRegex(driver.state.MissionConflict, 'generation'):
            control.request(self.f.runs, self.mid, action='cancel', reason='wrong generation',
                            idempotency_key='wrong-generation', run_id=run_id, generation=str(uuid.uuid4()))
        with self.assertRaisesRegex(driver.state.MissionConflict, 'unknown owned run'):
            control.request(self.f.runs, self.mid, action='cancel', reason='wrong run',
                            idempotency_key='wrong-run', run_id=str(uuid.uuid4()))
        self.assertEqual(self.fake.calls, calls)
        self.assertTrue(self.controller.current()['active_writer'])
        admission = next(a for a in self.controller.current()['admissions'].values() if a['run_id'] == run_id)
        self.assertTrue(admission['active'])
        self.assertEqual(control.view(self.controller.current())['desired'], 'running')

    def test_uncertain_writer_missing_runtime_keeps_admission(self):
        run_id, _, _ = self.uncertain_writer()
        self.fake.get_error = 'agent_not_found'
        control.request(self.f.runs, self.mid, action='cancel', reason='fixture cancel',
                        idempotency_key='missing-runtime-cancel', run_id=run_id)
        for _ in range(2):
            with self.assertRaisesRegex(herdr.HerdrBackendError, 'agent_not_found'):
                self.resume_cancellation()
            current = self.controller.current()
            admission = next(a for a in current['admissions'].values() if a['run_id'] == run_id)
            self.assertTrue(admission['active'])
            self.assertTrue(current['active_writer'])
            self.assertIsNone(admission['terminal'])
            self.assertEqual(control.view(current)['applied'], 'running')
        operations = [self.fake.operation(c) for c in self.fake.calls]
        self.assertEqual(sum(c[1:3] == ['agent', 'prompt'] for c in operations), 1)
        self.assertFalse(any(c[1:3] == ['agent', 'send-keys'] for c in operations))

    def test_uncertain_writer_already_quiescent_finalizes_without_signal(self):
        run_id, member, _ = self.uncertain_writer()
        self.fake.agent_states[member['agent_name']] = 'idle'
        control.request(self.f.runs, self.mid, action='cancel', reason='fixture cancel',
                        idempotency_key='quiescent-cancel', run_id=run_id)
        self.assertEqual(self.resume_cancellation()['status'], 'abandoned')
        current = self.controller.current()
        admission = next(a for a in current['admissions'].values() if a['run_id'] == run_id)
        self.assertFalse(admission['active'])
        self.assertFalse(current['active_writer'])
        self.assertEqual(admission['terminal']['status'], 'abandoned')
        self.assertEqual(control.view(current)['applied'], 'cancelled')
        operations = [self.fake.operation(c) for c in self.fake.calls]
        self.assertEqual(sum(c[1:3] == ['agent', 'prompt'] for c in operations), 1)
        self.assertFalse(any(c[1:3] == ['agent', 'send-keys'] for c in operations))


    def test_context_preview_allows_only_exact_frozen_flags_and_admitted_mission(self):
        from fleet_herdr_versions import TASK_CONTEXT_CONTRACT
        from fleet_herdr_skill_context import PROBE, POLICY, flags
        self.backend.initial_runtime_contract = dict(TASK_CONTEXT_CONTRACT)
        self.backend.context = {'policy': POLICY, 'disabled_skills': ['commit']}
        valid = ['codex', *flags(self.backend.context), 'debug', 'prompt-input', PROBE]
        kwargs = {'cwd': self.backend.target_repo, 'env': self.backend.environment, 'timeout': 1}
        with mock.patch.object(herdr.subprocess, 'run') as execute:
            self.backend._default_run(valid, **kwargs)
            self.assertEqual(execute.call_count, 1)
            for command in (['codex', 'exec', PROBE], valid + ['extra'],
                            ['codex', 'debug', 'prompt-input', PROBE],
                            valid[:-1] + ['different prompt'],
                            ['codex', '-c', 'approval_policy="on-request"', *valid[1:]]):
                with self.subTest(command=command), self.assertRaises(herdr.HerdrBackendError):
                    self.backend._default_run(command, **kwargs)
            self.assertEqual(execute.call_count, 1)
            control.request(self.f.runs, self.mid, action='pause', reason='fixture pause', idempotency_key='preview-pause')
            with self.assertRaisesRegex(herdr.HerdrBackendError, 'no longer admitted'):
                self.backend._default_run(valid, **kwargs)
            self.assertEqual(execute.call_count, 1)

if __name__=='__main__':unittest.main()
