"""FLEET-003 regression: real verifier/backend, synthetic CLI transport only."""
import copy
import json
import sys
from pathlib import Path
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fleet_artifacts as cas
import fleet_herdr as backend_module
import fleet_herdr_skill_context as context
import fleet_herdr_role_guidance as guidance
import fleet_herdr_versions as versions
import fleet_json
from tests import test_fleet_herdr as fixtures
from tests import test_fleet_herdr_mission as missions

# The exact FLEET-003 nine names and runtime metadata kind. Original bytes,
# session and rejection hashes remain in the incident's immutable CAS.
NINE = ['commit', 'deploy', 'entrevista-pre-slice', 'fase-0-recon',
        'human-web-art-direction', 'impl-notes', 'slice-gate', 'smoke-verify', 'product-design:index']


def injected(name, turn='turn-herdr-test'):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
        'internal_chat_message_metadata_passthrough': {
            'turn_id': turn, 'content_item_kinds': ['skills.selected_skill_instructions']},
        'content': [{'type': 'input_text', 'text': f'<skill>\n<name>{name}</name>\n<path>/fixture/global/{name}/SKILL.md</path>\nUnbound global content\n</skill>'}]}}


class StartupContextTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.HerdrBackendTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.fake.codex_version = '0.159.3'
        self.f.fake.screen = 'OpenAI Codex (v0.159.3)\n› Ask Codex to do anything\n'
        self.catalog_names = ['commit', 'slice-gate']
        self.previews = []
        original_run = backend_module.subprocess.run
        def runner(command, **kwargs):
            if command[0] not in {'codex', 'herdr'}:
                return original_run(command, **kwargs)
            if command[0] == 'codex' and command[-3:] == ['debug', 'prompt-input', context.PROBE]:
                self.previews.append(command)
                flags = ' '.join(command)
                names = [n for n in self.catalog_names if '{name="'+n+'",enabled=false}' not in flags]
                text = '<skills_instructions>\n### Available skills\n' + '\n'.join(
                    f'- {n}: Fixture skill (file: /fixture/{n}/SKILL.md)' for n in names)
                return fixtures.completed(command, value=[{'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': text}]}, {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': context.PROBE}]}])
            return self.f.fake(command, **kwargs)
        self.b = self.f.backend(runner=runner)
        self.b.initial_runtime_contract = dict(versions.CURRENT_TASK_CONTEXT_CONTRACT)
        self.b.boot()
        self.run = str(uuid.uuid4())
        task = json.loads(self.f.prompt(self.run))
        bundle = guidance.build_bundle()
        pin = cas.put_bytes(self.f.runs, self.f.mission_id, fleet_json.canonical_bytes(bundle))['artifact_id']
        task['role_guidance'] = guidance.project(bundle, pin, 'lead')
        self.task = task
        self.prompt = fleet_json.canonical_bytes(task).decode()
        cas.put_bytes(self.f.runs, self.f.mission_id, self.prompt.encode())

    def complete(self, edit=lambda rows: None):
        self.b.submit(self.run, self.prompt, instance_id='lead')
        member = self.b.state()['members'][0]
        final = {**self.task['result_contract'], 'status': 'PASS', 'summary': 'fixture', 'artifacts': []}
        self.f.write_transcript(agent_session=member['agent_session']['value'], member=member,
                                prompt=self.prompt, final=final)
        path = self.f.transcripts[member['agent_session']['value']]
        rows = [json.loads(r) for r in path.read_text().splitlines()]; edit(rows)
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        return self.b.collect_result(self.run)

    def test_authorized_inline_skills_pass_and_flags_are_bound(self):
        result = self.complete()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['evidence']['runtime_contract'], versions.CURRENT_TASK_CONTEXT_CONTRACT)
        current = self.b.state()
        contract = fleet_json.loads(cas.get_bytes(self.f.runs, self.f.mission_id, current['context_artifact_id']))
        self.assertEqual(contract['disabled_skills'], self.catalog_names)
        for command in self.f.fake.calls:
            if 'start' in command:
                self.assertIn('features.skill_search=false', command)
                self.assertIn(context.flags(contract)[1], command)

    def test_additional_catalog_skill_blocks_before_any_prompt(self):
        self.catalog_names.append('deploy')
        with self.assertRaisesRegex(backend_module.HerdrBackendError, 'host skills remain'):
            self.b.submit(self.run, self.prompt, instance_id='lead')
        self.assertFalse(self.b.state()['submissions'])
        self.assertFalse(self.f.fake.prompted_agents)

    def test_modified_additional_or_out_of_role_inline_skills_rejected(self):
        for mutation in ('changed', 'additional', 'wrong_role'):
            task = copy.deepcopy(self.task)
            if mutation == 'changed': task['role_guidance']['skills'][0]['content'] += '\nchanged'
            if mutation == 'additional': task['role_guidance']['skills'].append({'name': 'deploy'})
            if mutation == 'wrong_role': task['role_guidance']['contract']['role'] = 'worker'
            with self.subTest(mutation=mutation), self.assertRaisesRegex(backend_module.HerdrBackendError, 'role scope'):
                self.b.submit(self.run, fleet_json.canonical_bytes(task).decode(), instance_id='lead')
        self.assertFalse(self.f.fake.prompted_agents)

    def test_nine_global_messages_rejected_and_retained_offline(self):
        with self.assertRaises(backend_module.ExecutionEvidenceRejected) as caught:
            self.complete(lambda rows: rows.__setitem__(slice(-2, -2), [injected(n) for n in NINE]))
        proof = caught.exception.proof
        for p in self.f.transcripts.values(): p.unlink()
        with self.assertRaises(backend_module.ExecutionEvidenceRejected) as recovered:
            self.b.collect_result(self.run)
        self.assertEqual(proof, recovered.exception.proof)
        self.assertIn('additional user input', proof['reason'])
        self.assertFalse((self.f.runs / self.b._result_relative(self.run)).exists())

    def test_even_selected_skill_cannot_arrive_as_extra_message(self):
        with self.assertRaisesRegex(backend_module.ExecutionEvidenceRejected, 'additional user input'):
            self.complete(lambda rows: rows.insert(-2, injected('slice-gate')))

    def test_completion_rendering_mismatch_is_retained_and_rejected_offline(self):
        def mismatch(rows):
            final = next(r['payload'] for r in rows if r.get('type') == 'response_item'
                         and r['payload'].get('phase') == 'final_answer')
            value = json.loads(final['content'][0]['text'])
            value['summary'] += '<oai-mem-citation>fixture</oai-mem-citation>'
            final['content'][0]['text'] = json.dumps(value)
            # Completion retains the original text, as observed in FLEET-004.
        with self.assertRaisesRegex(backend_module.ExecutionEvidenceRejected, 'task_complete binding mismatch') as caught:
            self.complete(mismatch)
        proof = caught.exception.proof
        observed = fleet_json.loads(cas.get_bytes(self.f.runs, self.f.mission_id, proof['observed_result_artifact_id']))
        self.assertIn(b'<oai-mem-citation>', cas.get_bytes(self.f.runs, self.f.mission_id, observed['artifact_id']))
        prompts = len(self.f.fake.prompted_agents)
        for path in self.f.transcripts.values(): path.unlink()
        with self.assertRaises(backend_module.ExecutionEvidenceRejected) as recovered:
            self.b.collect_result(self.run)
        self.assertEqual(recovered.exception.proof, proof)
        self.assertEqual(len(self.f.fake.prompted_agents), prompts)
        self.assertFalse((self.f.runs / self.b._result_relative(self.run)).exists())

    def test_arbitrary_completion_mismatch_is_not_normalized(self):
        def mismatch(rows):
            rows[-1]['payload']['last_agent_message'] = '{"status":"FAIL"}'
        with self.assertRaisesRegex(backend_module.ExecutionEvidenceRejected, 'task_complete binding mismatch'):
            self.complete(mismatch)

    def test_duplicate_completion_is_still_ambiguous(self):
        with self.assertRaisesRegex(backend_module.HerdrBackendError, 'task_complete binding is ambiguous'):
            self.complete(lambda rows: rows.append(copy.deepcopy(rows[-1])))

    def test_skill_before_prompt_cannot_evade_context_rule(self):
        with self.assertRaisesRegex(backend_module.ExecutionEvidenceRejected, 'outside frozen task'):
            self.complete(lambda rows: rows.insert(2, injected('deploy')))

    def test_frozen_context_anchor_rejects_drift(self):
        value = self.b.state()
        value['context_artifact_id'] = '0' * 64
        (self.f.runs / self.b.relative).write_bytes(fleet_json.canonical_bytes(value) + b'\n')
        with self.assertRaisesRegex(backend_module.HerdrBackendError, 'runtime contract changed'):
            self.b.state()

    def test_rejected_cas_tamper_does_not_become_recovery_authority(self):
        with self.assertRaises(backend_module.ExecutionEvidenceRejected) as caught:
            self.complete(lambda rows: rows.insert(-2, injected('deploy')))
        pin = caught.exception.proof['observed_result_artifact_id']
        # The CAS accessor must reject altered bytes even though the pointer and
        # original context-rejection reason remain unchanged.
        cas.artifact_path(self.f.runs, self.f.mission_id, pin).write_bytes(b'corrupted fixture')
        with self.assertRaisesRegex(backend_module.HerdrBackendError, 'artifact_id'):
            self.b.collect_result(self.run)

    def test_interrupted_rejection_publication_recovers_without_resend(self):
        import fleet_safe_paths
        original = fleet_safe_paths.RootedFS.atomic_write
        attempted = []
        def interrupted(fs, path, data, **kwargs):
            if str(path).endswith('herdr-evidence-rejection-' + self.run + '.json'):
                attempted.append(fleet_json.loads(data)['artifact_id'])
                raise RuntimeError('fixture interruption before pointer')
            return original(fs, path, data, **kwargs)
        with mock.patch.object(fleet_safe_paths.RootedFS, 'atomic_write', interrupted), self.assertRaisesRegex(RuntimeError, 'fixture interruption'):
            self.complete(lambda rows: rows.insert(-2, injected('deploy')))
        prompts_before = len([c for c in self.f.fake.calls if 'prompt' in c])
        with self.assertRaises(backend_module.ExecutionEvidenceRejected) as caught:
            self.b.collect_result(self.run)
        self.assertEqual(attempted, [caught.exception.proof['artifact_id']])
        self.assertEqual(prompts_before, len([c for c in self.f.fake.calls if 'prompt' in c]))

    def test_unknown_catalog_syntax_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            context.catalog(json.dumps([{'content': [{'text': '<skills_instructions>\n- malformed (file: /x/SKILL.md)'}]}]))


class RejectedEvidenceRecoveryTests(unittest.TestCase):
    setUp = missions.HerdrProtocolRejectionTests.setUp
    current = missions.HerdrProtocolRejectionTests.current
    admission = missions.HerdrProtocolRejectionTests.admission
    drive = missions.HerdrProtocolRejectionTests.drive

    def reject_plan(self):
        self.stage = 'plan'; self.mutate = lambda result: None
        self.transcript_edit = lambda rows: rows.__setitem__(slice(-2, -2), [injected(n, 'turn-protocol-plan') for n in NINE])
        return self.drive()

    def test_rejected_execution_telemetry_survives_offline_without_admission(self):
        import fleet_report
        self.stage = 'plan'; self.mutate = lambda result: None
        def edit(rows):
            counts = {"input_tokens":150, "output_tokens":20, "cached_input_tokens":30}
            rows.insert(-2, {"type":"event_msg", "payload":{"type":"token_count",
                "info":{"total_token_usage":counts,"last_token_usage":counts}}})
            rows.insert(-2, injected('deploy', 'turn-protocol-plan'))
        self.transcript_edit = edit
        capture = self.Backend._capture_usage_baseline
        def seed_empty_session(backend, **kwargs):
            session = kwargs['member']['agent_session']['value']
            path = self.helper.tmp / ('empty-' + session + '.jsonl')
            metadata = {"type":"session_meta", "timestamp":"2026-09-06T00:00:00Z",
                "payload":{"id":session,"model_provider":"openai","cli_version":self.fake.codex_version}}
            path.write_text(json.dumps(metadata) + '\n')
            self.transcripts[session] = path
            return capture(backend, **kwargs)
        with mock.patch.object(self.Backend, '_capture_usage_baseline', seed_empty_session):
            rejected = self.drive()
        before = self.current(); calls = len(self.fake.calls)
        report = fleet_report.build_report(self.runs, self.mid)
        run = report['runs'][0]
        self.assertEqual(run['result_disposition'], 'rejected_execution_evidence')
        self.assertIsNotNone(run['observed'])
        self.assertEqual((run['prompt_tokens'],run['completion_tokens'],run['cached_input_tokens']), (150,20,30))
        for path in self.transcripts.values(): path.unlink()
        self.assertEqual(fleet_report.build_report(self.runs,self.mid), report)
        self.assertEqual(self.current(), before)
        self.assertEqual(len(self.fake.calls), calls)
        self.assertIsNone(self.admission()['result'])
        self.assertEqual(len(before['admissions']),1)
        self.assertIn('evidence_rejection', rejected)

    def test_rejection_prevents_downstream_and_repeated_pause_resume_resends(self):
        from fleet_herdr_mission import control, _Driver
        result = self.reject_plan(); proof = result['evidence_rejection']
        initial = self.current(); admission = self.admission(); calls = len(self.fake.calls)
        self.assertEqual([a['request_key'] for a in initial['admissions'].values()], ['herdr:plan'])
        for p in self.transcripts.values(): p.unlink()
        for action in ('pause', 'resume', 'pause'):
            control.request(self.runs, self.mid, action=action, reason='fixture', idempotency_key='fixture-'+action+str(len(self.current()['herdr_control']['requests'])))
            with mock.patch.object(_Driver, 'remaining_seconds', return_value=-1):
                result = self.drive()
            self.assertEqual(result['evidence_rejection'], proof)
            self.assertEqual(result['recovery'], 'blocked')
            self.assertEqual(self.admission(), admission)
            self.assertEqual(len(self.fake.calls), calls)
            self.assertEqual(len(self.current()['admissions']), 1)
            self.assertEqual(self.current()['mission_id'], self.mid)
        self.assertEqual(control.view(self.current())['desired'], 'pause_requested')
        self.assertEqual(control.view(self.current())['applied'], 'running')
        self.assertIsNone(control.view(self.current())['requests'][control.view(self.current())['latest']]['applied_at'])

    def test_supervision_returns_explicit_block_without_spinning(self):
        self.reject_plan()
        result = missions.driver.supervise(self.runs, self.mid, seconds=1)
        self.assertEqual(result['supervision'], 'blocked')
        self.assertEqual(result['iterations'], 1)
        self.assertEqual(len(self.current()['admissions']), 1)

    def test_explicit_cancel_closes_owned_run_without_accepting_plan(self):
        self.runtime_state = 'done'
        result = self.reject_plan()
        missions.driver.control.request(self.runs, self.mid, action='cancel', reason='fixture cancellation', idempotency_key='fixture-cancel')
        closed = self.drive()
        self.assertEqual(closed['status'], 'abandoned')
        self.assertIsNone(self.admission()['result'])
        self.assertFalse(self.admission()['active'])
        self.assertEqual(closed['evidence_rejection'], result['evidence_rejection'])
        self.assertEqual(len(self.current()['admissions']), 1)

    def test_expired_rejection_cannot_expand_cancel_generation_scope(self):
        self.runtime_state = 'done'
        self.reject_plan()
        missions.driver.control.request(self.runs, self.mid, action='cancel', reason='wrong fixture generation',
            idempotency_key='wrong-generation')
        calls = len(self.fake.calls)
        backend = self.backends[-1]
        observed = {**backend.state(), 'generation': str(uuid.uuid4())}
        with mock.patch.object(missions.driver._Driver, 'remaining_seconds', return_value=-1), \
             mock.patch.object(missions.driver._Driver, 'backend', return_value=backend), \
             mock.patch.object(backend, 'state', return_value=observed):
            result = self.drive()
        self.assertIn('owned generation changed', result['next_action'])
        self.assertEqual(len(self.fake.calls), calls)
        self.assertTrue(self.admission()['active'])
        self.assertFalse(self.current()['cancelled_runs'])


class RejectedCompletionRecoveryTests(RejectedEvidenceRecoveryTests):
    """The same pause/cancel/no-resend invariants cover completion mismatch."""
    def reject_plan(self):
        self.stage = 'plan'; self.mutate = lambda result: None
        def mismatch(rows):
            rows[-1]['payload']['last_agent_message'] = '{"status":"FAIL"}'
        self.transcript_edit = mismatch
        return self.drive()


class ResearchEndToEndTests(unittest.TestCase):
    """Six real controller stages and offline archive; CLI/model output is fixture data."""
    def setUp(self):
        from tests import test_fleet_herdr_research as research
        from types import SimpleNamespace
        self.h = research.ResearchProfileTests(); self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.mid = self.h.create(options=self.h.options(herdr_session='mission-control-test'))
        self.fake = fixtures.FakeHerdr(); self.fake.codex_version = '0.159.3'
        self.fake.screen = 'OpenAI Codex (v0.159.3)\n› Ask Codex to do anything\n'
        self.tasks = []; self.transcripts = {}; self.extra = False
        original_run = backend_module.subprocess.run
        def runner(command, **kwargs):
            if command[0] not in {'codex', 'herdr'}:
                return original_run(command, **kwargs)
            if command[0] == 'codex' and command[-3:] == ['debug', 'prompt-input', context.PROBE]:
                return fixtures.completed(command, value=[
                    {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'fixture runtime'}]},
                    {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': context.PROBE}]}])
            return self.fake(command, **kwargs)
        def factory(controller):
            backend = research.REAL_BACKEND(self.h.runs, self.mid, feature='research-test',
                target_repo=controller.candidate, compiled=self.h.compiled, session='mission-control-test',
                personal_cli=True, environment={'PATH': '/usr/bin:/bin'},
                transcript_resolver=self.transcripts.get)
            submit = backend.submit
            def observed(run_id, prompt, *, instance_id):
                observation = submit(run_id, prompt, instance_id=instance_id)
                task = fleet_json.loads(prompt); self.tasks.append(task)
                member = next(m for m in backend.state()['members'] if m['instance_id'] == instance_id)
                if task['stage'] == 'build': (controller.candidate / 'answer.txt').write_text('implemented\n')
                final = {**task['result_contract'], 'status': 'PASS', 'summary': 'synthetic stage evidence',
                    'artifacts': [{'path': 'README.md', 'sha256': fleet_json.sha256((controller.candidate / 'README.md').read_bytes())}]}
                helper = SimpleNamespace(target=controller.candidate, fake=self.fake, tmp=self.h.tmp, transcripts=self.transcripts)
                fixtures.HerdrBackendTests.write_transcript(helper, agent_session=member['agent_session']['value'],
                    member=member, prompt=prompt, final=final, turn_id='fixture-'+task['stage'])
                if self.extra and task['stage'] == 'plan':
                    path = self.transcripts[member['agent_session']['value']]
                    rows = [json.loads(r) for r in path.read_text().splitlines()]
                    rows[-2:-2] = [injected(n, 'fixture-plan') for n in NINE]
                    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
                self.fake.agent_states[member['agent_name']] = 'done'
                return observation
            backend.submit = observed
            return backend
        patcher = mock.patch.object(missions.driver._Driver, 'backend', factory); patcher.start(); self.addCleanup(patcher.stop)
        transport_patch = mock.patch.object(backend_module.subprocess, 'run', side_effect=runner)
        transport_patch.start(); self.addCleanup(transport_patch.stop)

    def test_all_six_stages_accept_authorized_skills_and_archive_verifies(self):
        import fleet_herdr_archive as archive
        result = missions.driver.drive(self.h.runs, self.mid)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual([t['stage'] for t in self.tasks], ['plan', 'research', 'build', 'review', 'verify', 'synthesis'])
        verified = archive.verify(self.h.runs, self.mid, attest_permissions=True)
        self.assertEqual(verified['permissions']['runs'], 6)
        self.assertEqual(verified['archive_schema_version'], 6)
        self.assertEqual((self.h.runs / 'missions' / self.mid / 'candidate' / 'answer.txt').read_text(), 'implemented\n')

    def test_nine_messages_block_research_build_and_all_replays(self):
        self.extra = True
        result = missions.driver.drive(self.h.runs, self.mid)
        self.assertEqual(result['recovery'], 'blocked')
        current = missions.fleet_mission.load_state(self.h.runs, self.mid)
        self.assertEqual([a['request_key'] for a in current['admissions'].values()], ['herdr:plan'])
        for _ in range(2):
            repeated = missions.driver.drive(self.h.runs, self.mid)
            self.assertEqual(repeated['evidence_rejection'], result['evidence_rejection'])
        self.assertEqual([t['stage'] for t in self.tasks], ['plan'])
        self.assertFalse((self.h.runs / 'missions' / self.mid / 'candidate' / 'answer.txt').exists())
