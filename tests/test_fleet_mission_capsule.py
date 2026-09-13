"""Provider-free recovery regressions and an opt-in installed CLI/Seatbelt lane."""
import copy
import http.server
import json
import os
import shutil
from pathlib import Path
import sys
import threading
import unittest
import uuid
from unittest import mock

from tests import test_fleet_herdr_mission as fixtures
from tests import test_fleet_codex_responses as wire
import fleet_mission_capsule as native
import fleet_herdr_archive as archive
import fleet_herdr_mission as driver
import fleet_chatgpt_provider as subscription
import fleet_codex_responses as responses
import fleet_herdr_inference as inference
import fleet_herdr_control as control
import fleet_artifacts as artifacts
import fleet_mission as mission
import fleet_json


class CapsuleRecoveryTests(unittest.TestCase):
    """Synthetic execution; real admission, CAS, promotion, driver and archive."""

    def setUp(self):
        self.f = fixtures.HerdrMissionTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.calls = []
        self.output_path = 'src/nested/answer.txt'
        self.outputs = None
        self.manifest = {'schema': native.SCHEMA, 'cli_version': native.capsule.CODEX_VERSION,
            'images': {n: {'path': str(self.f.tmp/n), 'sha256': 'a'*64}
                       for n in ('codex', 'codex-code-mode-host')},
            'codex_home': str(self.f.tmp/'synthetic-home'),
            'provider': subscription.descriptor(subscription.SYNTHETIC_ACCOUNT, subscription.FIXTURE)}
        options = {'herdr_session': 'mission-fixture', 'herdr_capsule_manifest': self.manifest,
            'timeout_seconds': 7200, 'acceptance_contract': {'schema_version': 1, 'requirements': [
                {'id': 'answer', 'description': 'nested answer exists', 'checks': [
                    {'kind': 'text_contains', 'path': self.output_path, 'expected': 'implemented'}]}]}}
        self.f.mid = mission.create_mission(self.f.runs, compiled=self.f.compiled, feature='driver-test',
            objective='Implement a nested answer artifact', target_repo=self.f.target, base_sha=self.f.head,
            idempotency_key=fixtures.fleet_acceptance.bound_key('capsule-recovery', options['acceptance_contract']),
            runtime_options=options)[0]
        validate = native.validate_manifest
        mock.patch.object(native, 'validate_manifest', side_effect=lambda value, **kw: validate(value)).start()
        mock.patch.object(native.CapsuleBackend, 'provider',
                          lambda _: subscription.ChatGPTProvider.fixture(1)).start()
        mock.patch.object(subscription.ChatGPTProvider, 'exchange', self.exchange).start()
        mock.patch.object(responses.Bridge, 'execute_confined', autospec=True,
                          side_effect=self.execute).start()
        mock.patch.object(driver, '_archive', return_value=archive).start()

    @staticmethod
    def exchange(provider, raw, deadline, poll):
        poll()
        envelope = fleet_json.loads(raw)
        body = fleet_json.loads(envelope['input'])
        return fleet_json.canonical_bytes({
            **{k: envelope[k] for k in ('policy_id', 'request_id', 'request_sha256', 'model')},
            'output': fleet_json.canonical_bytes(wire.bundle(body, [wire.message()])).decode(),
            'output_tokens': 3})

    def execute(self, bridge, prompt, *, files, role, **kwargs):
        self.addCleanup(bridge.close)
        task = fleet_json.loads(prompt)
        self.calls.append(role)
        body = {'model': bridge.policy['model'], 'input': [{'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': prompt.decode()}]}], 'tools': [],
            'tool_choice': 'none', 'parallel_tool_calls': False, 'reasoning': {'effort': 'high'},
            'store': False, 'stream': True, 'include': []}
        bridge.broker.handle(fleet_json.canonical_bytes({'policy_id': bridge.broker.policy_id,
            'request_id': str(uuid.uuid4()), 'input': fleet_json.canonical_bytes(body).decode(),
            'max_output_tokens': 1024}))
        exported = dict(files)
        if role == 'build':
            exported.update(self.outputs or {self.output_path: b'implemented\n'})
        path = 'README.md' if role == 'plan' else self.output_path
        final = fleet_json.canonical_bytes({**task['result_contract'], 'status': 'PASS',
            'summary': 'Synthetic execution fixture',
            'artifacts': [{'path': path, 'sha256': inference.sha(exported[path])}]}).decode()
        turn, session = 'turn-'+task['run_id'], str(uuid.uuid4())
        root = self.f.tmp/('guest-'+task['run_id'])
        rows = [
            {'type': 'session_meta', 'payload': {'id': session, 'model_provider': 'fleet-local',
                'cli_version': native.capsule.CODEX_VERSION}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': turn}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': prompt.decode()}]}},
            {'type': 'turn_context', 'payload': {'turn_id': turn, 'model': bridge.policy['model'],
                'effort': 'high', 'cwd': str(root/'work'), 'approval_policy': 'never',
                'sandbox_policy': {'type': 'danger-full-access'}}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': final}]}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': turn,
                'last_agent_message': final}}]
        transcript = b''.join(fleet_json.canonical_bytes(row)+b'\n' for row in rows)
        port = bridge.listener.getsockname()[1]
        profile = native.capsule.profile(root, role, port)
        report = {'schema': 'fleet.codex.capsule.v1', 'attempt': bridge.policy['attempt'],
            'run_id': task['run_id'], 'role': role, 'prompt_sha256': inference.sha(prompt),
            'runtime_images': {k: v['sha256'] for k, v in self.manifest['images'].items()},
            'cli_version': self.manifest['cli_version'], 'execution_status': 'exited', 'returncode': 0,
            'quiescence_confirmed': True, 'cleanup_confirmed': True, 'provider_execution': 'SIMULATED',
            'capsule_root': str(root), 'broker_port': port, 'profile': profile,
            'profile_sha256': inference.sha(profile.encode()), 'inner_sandbox': 'danger-full-access',
            'input_files': {n: inference.sha(b) for n, b in files.items()},
            'files': {n: inference.sha(b) for n, b in exported.items()},
            'transcripts': {'session.jsonl': inference.sha(transcript)},
            'stdout_sha256': inference.sha(b''), 'stderr_sha256': inference.sha(b''),
            'broker_policy_id': bridge.broker.policy_id}
        bridge.close()
        return native.capsule.Result(report, b'', b'', exported, {'session.jsonl': transcript})

    def current(self):
        return mission.load_state(self.f.runs, self.f.mid)

    def backend(self):
        controller = driver._Driver(self.f.runs, self.f.mid)
        controller.load()
        return controller.backend()

    def interrupt_build(self, after_promotion=False):
        original = native.CapsuleBackend.promote

        def interrupt(backend, launch, report):
            if launch['stage'] == 'build':
                if after_promotion:
                    original(backend, launch, report)
                raise RuntimeError('synthetic promotion interruption')
            return original(backend, launch, report)

        with mock.patch.object(native.CapsuleBackend, 'promote', interrupt):
            with self.assertRaisesRegex(RuntimeError, 'synthetic promotion interruption'):
                driver.drive(self.f.runs, self.f.mid)
        self.assertEqual(self.calls, ['plan', 'build'])
        return next(a for a in self.current()['admissions'].values() if a['writer'])

    def assert_cancelled(self, build, *, published=False):
        backend = self.backend()
        retained = native.read_json(self.f.runs, self.f.mid, backend._name(build['run_id'], 'completed'))
        control.request(self.f.runs, self.f.mid, action='cancel', reason='fixture', idempotency_key='cancel')
        for _ in range(2):
            self.assertEqual(driver.drive(self.f.runs, self.f.mid)['status'], 'abandoned')
        current = self.current()
        self.assertIsNone(current['active_writer'])
        self.assertEqual(current['herdr_control']['applied'], 'cancelled')
        admission = current['admissions'][build['admission_id']]
        self.assertEqual(admission['terminal']['status'], 'succeeded' if published else 'abandoned')
        self.assertEqual(native.read_json(self.f.runs, self.f.mid,
            backend._name(build['run_id'], 'completed')), retained)
        self.assertIsNone(native.fleet_herdr.load_result_rejection(self.f.runs, self.f.mid, build['run_id']))
        self.assertEqual(self.calls, ['plan', 'build'])

    def test_new_nested_directories_complete_archive_without_resend(self):
        result = driver.drive(self.f.runs, self.f.mid)
        self.assertEqual(result['status'], 'succeeded', result)
        backend = self.backend()
        self.assertEqual((backend.repo/self.output_path).read_bytes(), b'implemented\n')
        self.assertEqual((backend.repo/'src').stat().st_mode & 0o777, 0o700)
        self.assertTrue(archive.verify(self.f.runs, self.f.mid)['valid'])
        self.assertEqual(driver.drive(self.f.runs, self.f.mid)['status'], 'succeeded')
        self.assertEqual(self.calls, ['plan', 'build', 'review', 'verify', 'synthesis'])
        self.assertFalse((self.f.target/'src').exists())

    def test_cancel_before_promotion_releases_writer_without_publishing(self):
        build = self.interrupt_build()
        self.assert_cancelled(build)
        self.assertFalse((self.backend().repo/'src').exists())

    def test_cancel_after_promotion_preserves_completed_verdict(self):
        build = self.interrupt_build(after_promotion=True)
        self.assert_cancelled(build, published=True)
        self.assertEqual((self.backend().repo/self.output_path).read_bytes(), b'implemented\n')

    def test_partial_promotion_can_cancel_without_publishing_remaining_files(self):
        self.outputs = {'answer.txt': b'first\n', self.output_path: b'implemented\n'}
        original = native.paths.RootedFS.replace_regular

        def interrupt(fs, relative, content, **kwargs):
            if str(relative) == self.output_path:
                raise RuntimeError('synthetic partial promotion')
            return original(fs, relative, content, **kwargs)

        with mock.patch.object(native.paths.RootedFS, 'replace_regular', interrupt):
            with self.assertRaisesRegex(RuntimeError, 'synthetic partial promotion'):
                driver.drive(self.f.runs, self.f.mid)
        backend = self.backend()
        self.assertEqual((backend.repo/'answer.txt').read_bytes(), b'first\n')
        build = next(a for a in self.current()['admissions'].values() if a['writer'])
        self.assert_cancelled(build)
        self.assertEqual((backend.repo/'answer.txt').read_bytes(), b'first\n')
        self.assertFalse((backend.repo/'src').exists())

    def test_pause_then_resume_promotes_retained_output_with_original_deadline(self):
        build = self.interrupt_build()
        deadline = self.current()['admission_policy']['deadline_at']
        control.request(self.f.runs, self.f.mid, action='pause', reason='fixture', idempotency_key='pause')
        result = driver.drive(self.f.runs, self.f.mid)
        self.assertEqual(result['status'], 'running')
        self.assertFalse((self.backend().repo/'src').exists())
        self.assertIsNone(native.fleet_herdr.load_result_rejection(self.f.runs, self.f.mid, build['run_id']))
        control.request(self.f.runs, self.f.mid, action='resume', reason='fixture', idempotency_key='resume')
        self.assertEqual(driver.drive(self.f.runs, self.f.mid)['status'], 'succeeded')
        self.assertEqual(self.current()['admission_policy']['deadline_at'], deadline)
        self.assertEqual(self.calls, ['plan', 'build', 'review', 'verify', 'synthesis'])

    def test_existing_parent_modes_and_symlink_rejection_survive_recovery(self):
        self.interrupt_build()
        backend = self.backend()
        outside = self.f.tmp/'outside'
        outside.mkdir()
        (backend.repo/'src').symlink_to(outside, target_is_directory=True)
        with self.assertRaises((native.paths.SafePathError, inference.InferenceError)):
            driver.drive(self.f.runs, self.f.mid)
        self.assertEqual(list(outside.iterdir()), [])
        (backend.repo/'src').unlink()
        (backend.repo/'src').mkdir(mode=0o755)
        self.assertEqual(driver.drive(self.f.runs, self.f.mid)['status'], 'succeeded')
        self.assertEqual((backend.repo/'src').stat().st_mode & 0o777, 0o755)
        self.assertEqual((backend.repo/'src/nested').stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.calls, ['plan', 'build', 'review', 'verify', 'synthesis'])

    def test_deadline_before_promotion_closes_exact_run_without_publishing(self):
        build = self.interrupt_build()
        deadline = native.state.parse_timestamp(self.current()['admission_policy']['deadline_at'], 'deadline')
        with mock.patch.object(native, 'datetime') as clock, \
                mock.patch.object(driver._Driver, 'remaining_seconds', return_value=0):
            clock.now.return_value = deadline
            result = driver.drive(self.f.runs, self.f.mid)
        self.assertEqual(result['status'], 'abandoned')
        self.assertIsNone(self.current()['active_writer'])
        self.assertEqual(self.current()['admissions'][build['admission_id']]['terminal']['status'], 'abandoned')
        self.assertFalse((self.backend().repo/'src').exists())
        self.assertEqual(self.calls, ['plan', 'build'])

    def test_interruption_after_mkdir_recovers_without_new_execution(self):
        original = native.paths.RootedFS._open_directory_chain

        def interrupt(fs, parts, modes, *, create):
            fd = original(fs, parts, modes, create=create)
            if fs.root.name == 'candidate' and parts == ('src', 'nested') and create:
                os.close(fd)
                raise RuntimeError('synthetic interruption after mkdir')
            return fd

        with mock.patch.object(native.paths.RootedFS, '_open_directory_chain', interrupt):
            with self.assertRaisesRegex(RuntimeError, 'synthetic interruption after mkdir'):
                driver.drive(self.f.runs, self.f.mid)
        backend = self.backend()
        self.assertTrue((backend.repo/'src/nested').is_dir())
        self.assertFalse((backend.repo/self.output_path).exists())
        self.assertEqual(driver.drive(self.f.runs, self.f.mid)['status'], 'succeeded')
        self.assertEqual(self.calls, ['plan', 'build', 'review', 'verify', 'synthesis'])

    def test_candidate_drift_is_not_overwritten_on_resume(self):
        self.interrupt_build()
        backend = self.backend()
        (backend.repo/'README.md').write_bytes(b'unrelated edit\n')
        with self.assertRaisesRegex(inference.InferenceError, 'candidate changed before promotion'):
            driver.drive(self.f.runs, self.f.mid)
        self.assertEqual((backend.repo/'README.md').read_bytes(), b'unrelated edit\n')
        self.assertFalse((backend.repo/'src').exists())
        self.assertIsNotNone(self.current()['active_writer'])
        self.assertEqual(self.calls, ['plan', 'build'])


@unittest.skipUnless(sys.platform=='darwin' and os.environ.get('FLEET_CODEX_IMAGE'), 'explicit installed CLI required')
class CapsuleTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.HerdrMissionTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        mock.patch.stopall()
        self.f=self.fixture; self.calls=[]; self.on_request=None; owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                task=None
                for item in body['input']:
                    for part in item.get('content',[]) if type(item.get('content')) is list else []:
                        try:
                            parsed=json.loads(part.get('text',''))
                            if type(parsed) is dict and parsed.get('stage') in native.STAGES: task=parsed
                        except ValueError: pass
                if task is None:
                    self.send_response(400); self.end_headers(); return
                stage=task['stage']; count=owner.calls.count(stage); owner.calls.append(stage)
                if owner.on_request: owner.on_request(stage)
                if stage=='build' and count==0:
                    patch='*** Begin Patch\n*** Add File: answer.txt\n+implemented\n*** End Patch'
                    output=[{'id':'ct_fixture','type':'custom_tool_call','call_id':'call_fixture',
                        'namespace':'functions','name':'exec','input':'text(await tools.apply_patch('+json.dumps(patch)+'));'}]
                else:
                    path='README.md' if stage in {'research','plan'} else 'answer.txt'
                    data=b'baseline\n' if path=='README.md' else b'implemented\n'
                    result={**task['result_contract'],'status':'PASS','summary':'Synthetic stage evidence',
                        'artifacts':[{'path':path,'sha256':inference.sha(data)}]}
                    output=[wire.message(json.dumps(result))]
                raw=responses.sse(wire.bundle(body,output))
                self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Content-Length',str(len(raw))); self.end_headers()
                try: self.wfile.write(raw)
                except BrokenPipeError: pass
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02}); self.thread.start()
        self.addCleanup(self.stop_server)
        images=native.capsule.runtime(Path(os.environ['FLEET_CODEX_IMAGE']))
        self.manifest={'schema':native.SCHEMA,'cli_version':native.capsule.CODEX_VERSION,'images':{n:{'path':p,'sha256':inference.sha(Path(p).read_bytes())} for n,p in images.items()},
            'codex_home':str(self.f.tmp),'provider':subscription.descriptor(subscription.SYNTHETIC_ACCOUNT,subscription.FIXTURE)}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(native.CapsuleBackend,'provider',lambda _:subscription.ChatGPTProvider.fixture(self.server.server_port)).start()
        options={'herdr_session':'mission-fixture','herdr_capsule_manifest':self.manifest,'timeout_seconds':7200,
                 'acceptance_contract':{'schema_version':1,'requirements':[{'id':'answer','description':'answer exists',
                 'checks':[{'kind':'text_contains','path':'answer.txt','expected':'implemented'}]}]}}
        self.options=options
        self.f.mid=self.f.create(options=options,key='capsule-fixture')

    def stop_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def test_driver_all_stages_native_cli_archive_and_no_resend(self):
        result=driver.drive(self.f.runs,self.f.mid)
        self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(self.calls,['plan','build','build','review','verify','synthesis'])
        self.assertEqual(result['archive']['permissions']['policy_version'],2)
        self.assertEqual(result['archive']['permissions']['runs'],5)
        self.assertEqual(result['archive']['inference_broker']['provider_execution'],'SIMULATED')
        self.assertEqual((self.f.target/'README.md').read_bytes(),b'baseline\n')
        self.assertFalse((self.f.target/'answer.txt').exists())
        before=list(self.calls)
        self.assertEqual(driver.drive(self.f.runs,self.f.mid)['status'],'succeeded')
        self.assertEqual(self.calls,before)
        verified=archive.verify(self.f.runs,self.f.mid)
        self.assertTrue(verified['valid'])
        self.assertEqual(verified['permissions']['scope'],'external_seatbelt_capsule')
        current=mission.load_state(self.f.runs,self.f.mid)
        from tests.test_mission_run import mission_run
        import fleet_herdr_report
        status = mission_run.mission_status(self.f.runs, self.f.mid)
        self.assertEqual(status['runtime']['executor'], native.SCHEMA)
        self.assertEqual(status['runtime']['visibility'], 'headless_confined_cli')
        report = fleet_herdr_report.build_report(self.f.runs, current, self.f.compiled,
            native.state.read_events(native.state.ledger_path(self.f.runs, self.f.mid)))
        self.assertEqual(report['agents']['observed_sessions'], 5)
        self.assertTrue(all(r['observed']['provider_execution'] == 'SIMULATED' for r in report['runs']))
        controller = driver._Driver(self.f.runs, self.f.mid); controller.load()
        closed_backend = controller.backend()
        closed_backend.teardown()
        closed_backend.teardown()
        self.assertTrue(closed_backend.state()['workspace']['closed'])
        for entry in current['inference_policies'].values():
            policy=entry['policy']
            self.assertEqual(policy['schema_version'],3)
            launch=fleet_json.loads(artifacts.get_bytes(self.f.runs,self.f.mid,policy['launch_artifact_id']))
            for field,value in [('prompt_sha256','f'*64),('stage','build')]:
                altered=copy.deepcopy(launch); altered[field]=value
                if altered==launch: continue
                with self.assertRaises((ValueError,KeyError,inference.InferenceError)):
                    native.verify_launch(altered,current,lambda p:artifacts.get_bytes(self.f.runs,self.f.mid,p))
            execution = next(r for r in current['admissions'].values() if r['run_id'] == policy['run_id'])
            result = fleet_json.loads(artifacts.get_bytes(self.f.runs,self.f.mid,execution['result']['artifact_id']))
            proof = result['evidence']
            record = fleet_json.loads(artifacts.get_bytes(self.f.runs,self.f.mid,proof['capsule_report_artifact_id']))
            for field,value in [('cli_version','0.0.0'),('quiescence_confirmed',False),('profile',record['profile']+'(allow default)\n')]:
                altered = copy.deepcopy(record); altered[field] = value
                with self.assertRaises(inference.InferenceError):
                    native.verify_report(launch,altered,lambda p:artifacts.get_bytes(self.f.runs,self.f.mid,p))
        evidence_dir = os.environ.get('FLEET_MEDIATION_EVIDENCE_DIR')
        if evidence_dir:
            destination = Path(evidence_dir).resolve(strict=True)
            shutil.copytree(self.f.runs, destination/'runs')
            (destination/'mission.json').write_bytes(fleet_json.canonical_bytes({
                'mission_id':self.f.mid, 'status':current['status'], 'provider_execution':'SIMULATED',
                'cli_version':native.capsule.CODEX_VERSION, 'calls':self.calls, 'archive':verified})+b'\n')

    def test_crash_before_promotion_recovers_retained_build_without_new_send(self):
        original=native.CapsuleBackend.promote
        crashed=[]
        def crash(backend,launch,report):
            if launch['stage']=='build' and not crashed:
                crashed.append(True)
                raise RuntimeError('synthetic crash before promotion')
            return original(backend,launch,report)
        with mock.patch.object(native.CapsuleBackend,'promote',crash):
            with self.assertRaisesRegex(RuntimeError,'synthetic crash'):
                driver.drive(self.f.runs,self.f.mid)
        self.assertEqual(self.calls,['plan','build','build'])
        result=driver.drive(self.f.runs,self.f.mid)
        self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(self.calls,['plan','build','build','review','verify','synthesis'])

    def test_pause_during_request_denies_new_send_and_preserves_unknown_outcome(self):
        def pause(stage):
            if stage=='plan':
                control.request(self.f.runs,self.f.mid,action='pause',reason='synthetic pause',idempotency_key='pause-fixture')
        self.on_request=pause
        with self.assertRaises((inference.InferenceError,native.sandbox.SandboxError)):
            driver.drive(self.f.runs,self.f.mid)
        self.assertEqual(self.calls,['plan'])
        current=mission.load_state(self.f.runs,self.f.mid)
        a=next(iter(current['admissions'].values()))
        candidate = driver._Driver(self.f.runs,self.f.mid); candidate.load()
        backend=native.CapsuleBackend(self.f.runs,self.f.mid,feature='driver-test',target_repo=candidate.candidate,
            compiled=self.f.compiled,session='mission-fixture',manifest=self.manifest)
        self.assertEqual(backend.recover(a['run_id'])['status'],'indeterminate')
        self.assertEqual(self.calls,['plan'])
        self.assertTrue(a['active'])
        self.assertNotIn(current['status'],{'succeeded','failed','abandoned'})


if __name__=='__main__': unittest.main()
