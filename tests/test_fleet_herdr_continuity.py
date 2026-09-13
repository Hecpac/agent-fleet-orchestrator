import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fleet_herdr_continuity as c

NOW = 1788818400.0


def transcript(member, sid, prompt='ordinary work', final='Completed milestone', used=850, timestamp=NOW):
    stamp = c.datetime.fromtimestamp(timestamp,c.timezone.utc).isoformat()
    tid = str(uuid.uuid4())
    def row(kind,payload): return dict(type=kind,timestamp=stamp,payload=payload)
    return c.encoded(row('session_meta',dict(id=sid,cwd=member['cwd'],model_provider='openai',cli_version='0.153.0',timestamp=stamp)))+b'\n'+b'\n'.join(c.encoded(r) for r in [
        row('event_msg',dict(type='task_started',turn_id=tid)),
        row('turn_context',dict(member['expected_context'],turn_id=tid)),
        row('response_item',dict(type='message',role='user',content=[dict(type='input_text',text=prompt)])),
        row('response_item',dict(type='message',role='assistant',phase='final_answer',content=[dict(type='output_text',text=final)])),
        row('event_msg',dict(type='token_count',info=dict(model_context_window=20000,last_token_usage=dict(input_tokens=used-100,output_tokens=100,total_tokens=used)))),
        row('event_msg',dict(type='task_complete',turn_id=tid,last_agent_message=final))])+b'\n'


class FakeLive:
    def __init__(self, members):
        self.members={m['name']:m for m in members}; self.agents={}; self.raw={}; self.calls=[]
        self.fail_after_send=False; self.drop_send=False; self.corrupt_ack=False
        self.delayed_metadata=False; self.pending_session=None
        for m in members:
            sid=str(uuid.uuid4()); self.agents[m['name']]=dict(agent_status='idle',binding=dict(name=m['name'],pane='p-'+m['name'],terminal='t-'+m['name'],session=sid,cwd=m['cwd']))
            self.raw[sid]=transcript(m,sid,used=18000)

    def get(self,member): return copy.deepcopy(self.agents[member['name']])
    def transcript(self,member,sid): return c.native(self.raw[sid],member,sid)
    def status_session(self,member): return self.pending_session

    def send(self,member,expected,prompt):
        self.calls.append((member['name'],prompt))
        agent=self.agents[member['name']]
        if agent['binding']!=expected: raise c.ContinuityError('fake_binding_changed')
        if not self.drop_send:
            if prompt=='/new':
                if self.delayed_metadata:self.pending_session=str(uuid.uuid4())
                else:agent['binding']['session']=str(uuid.uuid4())
            elif prompt=='/status':
                pass
            else:
                data=json.loads(prompt.rsplit('\n',1)[1])
                if 'reference_files' in data:
                    value=dict(schema_version=1,role=member['label'],goal=member['goal'],required_facts=member['required_facts'],decisions=['Use measured evidence'],completed=['Camera was implemented'],pending=['Test camera exit'],risks=['No measured body fat'],evidence_paths=[])
                else:
                    value=data['acknowledgement']
                    if self.corrupt_ack: value['status']='FAKE'
                    if self.pending_session:
                        agent['binding']['session']=self.pending_session;self.pending_session=None
                sid=agent['binding']['session']
                # Append ordinary turns, retain the fresh session metadata.
                raw=transcript(member,sid,prompt,c.encoded(value).decode(),used=2000)
                if sid in self.raw: raw=self.raw[sid]+raw.split(b'\n',1)[1]
                self.raw[sid]=raw
        if self.fail_after_send: raise TimeoutError('ambiguous')
        return {'returncode':0}


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve(); self.product=self.root/'product';self.product.mkdir()
        self.file=self.product/'report.md';self.file.write_text('verified output')
        def member(name):
            return dict(name=name,label=name.title(),cwd=str(self.product),codex_home=str(self.root),cli_version='0.153.0',
                        goal='Continue FitScan',authority='Read only',required_facts=['Weight, height and scan'],reference_files=[str(self.file)],read_roots=[str(self.product)],
                        expected_context=dict(cwd=str(self.product),model='observed-model',effort=None,approval_policy='never',sandbox_policy={'type':'read-only'}))
        self.members=[member('researcher'),member('lead')]
        self.plan=dict(schema_version=1,scope='test',lead_name='lead',prepare_percent=70,renew_percent=80,stale_after=300,phase_timeout_seconds=120,max_generations=3,members=self.members)
        self.config={'command':['/observed/herdr','--session','test'],'environment':{}}
        self.live=FakeLive(self.members);self.store=c.Store(self.root/'control');self.now=NOW
        self.ctrl=c.Controller(self.plan,self.config,self.store,self.live,lambda:self.now)

    def lower(self,name='lead',used=1000,age=0):
        m=self.live.members[name];sid=self.live.agents[name]['binding']['session']
        self.live.raw[sid]=transcript(m,sid,used=used,timestamp=NOW-age)

    def run_steps(self,n=9,manual='researcher'):
        state=self.ctrl.step(manual)
        for _ in range(n-1): state=self.ctrl.step()
        return state

    def issued_phase(self, phase):
        self.lower('researcher', used=11000); self.lower()
        state = self.ctrl.step('researcher')
        for _ in range(10):
            if state['active']['phase'] == phase:
                return copy.deepcopy(state['active'])
            state = self.ctrl.step()
        self.fail('fixture did not reach ' + phase)

    def restart_controller(self):
        self.ctrl = c.Controller(self.plan, self.config, c.Store(self.store.root),
                                 self.live, lambda: self.now)

    def phase_fixture(self, phase, *, delayed_metadata=False, ambiguous=False):
        fixture = ContinuityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.live.delayed_metadata = delayed_metadata
        fixture.live.fail_after_send = ambiguous
        return fixture, fixture.issued_phase(phase)

    def assert_original_intent(self, issued):
        entry = self.ctrl.status()
        found = []
        while entry:
            op = entry.get('active') or {}
            if op.get('id') == issued['id'] and op.get('phase') == issued['phase']:
                found.append(op)
            entry = c.read_json(self.store.get(entry['previous'])) if entry.get('previous') else None
        self.assertTrue(found)
        for op in found:
            for key in ('since', 'prompt_sha256', 'send_binding'):
                self.assertEqual(op[key], issued[key])
            self.assertEqual(op['since'] + self.plan['phase_timeout_seconds'], issued['since'] + 120)

    def response_location(self, issued):
        key = {'checkpoint_sent': 'source_binding', 'restore_sent': 'new_binding',
               'lead_sent': 'lead_binding'}[issued['phase']]
        name = 'lead' if issued['phase'] == 'lead_sent' else issued['member']
        return name, issued[key]['session']

    def test_late_checkpoint_recovers_exact_evidence_without_timing_reset(self):
        issued = self.issued_phase('checkpoint_sent')
        calls = list(self.live.calls)
        self.now = issued['since'] + 121
        self.restart_controller()
        with mock.patch.object(self.ctrl, 'response', wraps=self.ctrl.response) as response:
            result = self.ctrl.step()
            self.assertEqual(result['active']['phase'], 'prepared')
            self.assertEqual(response.call_count, 1)
        active = result['active']
        self.assertEqual(active['since'], issued['since'])
        self.assertEqual(active['id'], issued['id'])
        checkpoint = c.read_json(self.store.get(active['checkpoint_sha256']))
        self.assertEqual(checkpoint['renewal_id'], issued['id'])
        self.assertEqual(checkpoint['source_binding'], issued['source_binding'])
        raw = self.store.get(checkpoint['summary_transcript'])
        self.assertEqual(raw, self.live.raw[issued['source_binding']['session']])
        for _ in range(2):
            self.now += 121
            self.restart_controller()
            self.assertEqual(self.ctrl.reconcile()['active'], active)
        self.assertEqual(self.live.calls, calls)

    def test_late_restore_repeated_reconcile_preserves_evidence_and_deadline(self):
        issued = self.issued_phase('restore_sent')
        calls = list(self.live.calls)
        self.now = issued['since'] + 121
        observations = []
        with mock.patch.object(self.live, 'transcript', wraps=self.live.transcript) as reads:
            for _ in range(3):
                self.restart_controller()
                self.ctrl.step()
                observations.append(self.ctrl.reconcile())
                self.now += 121
            self.assertEqual(reads.call_count, 1)
        for result in observations:
            self.assertEqual(result['active']['phase'], 'lead_pending')
            self.assertEqual(result['active']['since'], issued['since'])
            self.assertEqual(result['active']['new_since'], issued['new_since'])
            self.assertEqual(result['active']['id'], issued['id'])
            raw = self.store.get(result['active']['restoration_transcript'])
            self.assertEqual(raw, self.live.raw[issued['new_binding']['session']])
        self.assertEqual(self.live.calls, calls)
        self.assertEqual(result['completed'], [])

    def test_late_lead_receipt_completes_exact_attempt_without_replay(self):
        issued = self.issued_phase('lead_sent')
        calls = list(self.live.calls)
        self.now = issued['since'] + 121
        self.restart_controller()
        result = self.ctrl.step()
        self.assertIsNone(result['active'])
        self.assertEqual(len(result['completed']), 1)
        record = c.read_json(self.store.get(result['completed'][0]))
        self.assertEqual(record['id'], issued['id'])
        self.assertEqual(record['since'], issued['since'])
        self.assertEqual(self.store.get(record['lead_receipt']),
                         self.live.raw[issued['lead_binding']['session']])
        for _ in range(2):
            self.now += 121
            self.restart_controller()
            self.ctrl.reconcile()
            self.assertEqual(self.ctrl.step()['completed'], result['completed'])
        self.assertEqual(self.live.calls, calls)
        self.assertEqual(self.ctrl.verify()['completed'][0]['status'], 'PASS')

    def test_late_restoration_blocks_next_transition_even_for_self_lead(self):
        for name in ('researcher', 'lead'):
            with self.subTest(member=name):
                f = ContinuityTests()
                f.setUp()
                self.addCleanup(f.doCleanups)
                f.lower('researcher', used=11000); f.lower('lead', used=11000)
                state = f.ctrl.step(name)
                for _ in range(10):
                    if state['active']['phase'] == 'restore_sent':
                        break
                    state = f.ctrl.step()
                self.assertEqual(state['active']['phase'], 'restore_sent')
                issued = copy.deepcopy(state['active'])
                calls = list(f.live.calls)
                f.now = issued['since'] + 121
                f.restart_controller()
                retained = f.ctrl.step()['active']
                self.assertEqual(retained['phase'], 'lead_pending')
                self.assertEqual(retained['since'], issued['since'])
                raw = f.live.raw[issued['new_binding']['session']]
                self.assertEqual(f.store.get(retained['restoration_transcript']), raw)
                for _ in range(2):
                    f.restart_controller()
                    with mock.patch.object(f.live, 'get', wraps=f.live.get) as read:
                        result = f.ctrl.step()
                        self.assertEqual(read.call_count, 0)
                    self.assertEqual(result['active']['phase'], 'blocked')
                    self.assertEqual(result['active']['reason'], 'handoff_timeout_reconcile_without_resending')
                    self.assertEqual(result['completed'], [])
                    restored = f.ctrl.reconcile()['active']
                    self.assertEqual(restored['phase'], 'lead_pending')
                    self.assertEqual(restored['since'], issued['since'])
                    self.assertEqual(restored['restoration_transcript'], retained['restoration_transcript'])
                    f.now += 121
                f.assert_original_intent(issued)
                self.assertEqual(f.live.calls, calls)
                self.assertEqual(f.ctrl.verify()['completed'], [])

    def test_result_deadline_boundary_preserves_existing_phase_clocks(self):
        for phase in ('checkpoint_sent', 'restore_sent', 'lead_sent'):
            for elapsed in (119, 120, 121):
                with self.subTest(phase=phase, elapsed=elapsed):
                    f, issued = self.phase_fixture(phase)
                    calls = list(f.live.calls)
                    f.now = issued['since'] + elapsed
                    with mock.patch.object(f.ctrl, 'response', wraps=f.ctrl.response) as read:
                        result = f.ctrl.step()
                        self.assertEqual(read.call_count, 1)
                    if phase == 'lead_sent':
                        op = c.read_json(f.store.get(result['completed'][0]))
                        self.assertEqual(op['since'], issued['since'])
                        self.assertEqual(op['completed_at'], f.now)
                    else:
                        self.assertEqual(result['active']['phase'],
                                         'prepared' if phase == 'checkpoint_sent' else 'lead_pending')
                        self.assertEqual(result['active']['since'], issued['since'] if elapsed > 120 else f.now)
                    f.assert_original_intent(issued)
                    self.assertEqual(f.live.calls, calls)

    def test_missing_late_results_still_timeout_without_new_wait_window(self):
        for phase in ('checkpoint_sent', 'restore_sent', 'lead_sent'):
            with self.subTest(phase=phase):
                f, issued = self.phase_fixture(phase)
                _, sid = f.response_location(issued)
                rows = f.live.raw[sid].splitlines()
                start = max(i for i, raw in enumerate(rows)
                            if c.read_json(raw)['payload'].get('type') == 'task_started')
                f.live.raw[sid] = b'\n'.join(rows[:start + 1]) + b'\n'
                calls = list(f.live.calls)
                for elapsed in (119, 121, 500):
                    f.now = issued['since'] + elapsed
                    f.restart_controller()
                    result = f.ctrl.step()
                    if elapsed <= 120:
                        self.assertEqual(result['active']['phase'], phase)
                    else:
                        self.assertEqual(result['active']['phase'], 'blocked')
                        self.assertEqual(result['active']['reason'], 'handoff_timeout_reconcile_without_resending')
                    reconciled = f.ctrl.reconcile()
                    self.assertEqual(reconciled['active']['since'], issued['since'])
                    self.assertEqual(reconciled['active']['phase'], phase)
                    self.assertEqual(reconciled['completed'], [])
                f.assert_original_intent(issued)
                self.assertEqual(f.live.calls, calls)

    def test_result_available_after_timeout_block_recovers_same_attempt(self):
        for phase in ('checkpoint_sent', 'restore_sent', 'lead_sent'):
            with self.subTest(phase=phase):
                f, issued = self.phase_fixture(phase)
                calls = list(f.live.calls)
                f.now = issued['since'] + 121
                with mock.patch.object(f.live, 'transcript', side_effect=c.ContinuityError('native_transcript_unavailable')):
                    self.assertEqual(f.ctrl.step()['active']['phase'], 'blocked')
                f.now += 121
                f.restart_controller()
                self.assertEqual(f.ctrl.reconcile()['active']['since'], issued['since'])
                result = f.ctrl.step()
                if phase == 'lead_sent':
                    self.assertEqual(len(result['completed']), 1)
                else:
                    self.assertEqual(result['active']['phase'], 'prepared' if phase == 'checkpoint_sent' else 'lead_pending')
                    self.assertEqual(result['active']['since'], issued['since'])
                f.assert_original_intent(issued)
                self.assertEqual(f.live.calls, calls)

    def test_invalid_late_results_cannot_adjudicate_or_authorize_effects(self):
        for phase in ('checkpoint_sent', 'restore_sent', 'lead_sent'):
            for corruption in ('protocol', 'malformed_final', 'wrong_prompt', 'wrong_session',
                               'wrong_native_session', 'wrong_context', 'wrong_completion', 'incomplete'):
                with self.subTest(phase=phase, corruption=corruption):
                    f, issued = self.phase_fixture(phase)
                    name, sid = f.response_location(issued)
                    rows = [c.read_json(line) for line in f.live.raw[sid].splitlines()]
                    final = next(r['payload'] for r in reversed(rows) if r['type'] == 'response_item'
                                 and r['payload'].get('role') == 'assistant')
                    complete = next(r['payload'] for r in reversed(rows) if r['payload'].get('type') == 'task_complete')
                    if corruption in {'protocol', 'malformed_final'}:
                        value = c.read_json(final['content'][0]['text'])
                        value['goal' if phase == 'checkpoint_sent' else 'status'] = 'FOREIGN'
                        text = '{' if corruption == 'malformed_final' else c.encoded(value).decode()
                        final['content'][0]['text'] = text; complete['last_agent_message'] = text
                    elif corruption == 'wrong_prompt':
                        user = next(r['payload'] for r in reversed(rows) if r['payload'].get('role') == 'user')
                        user['content'][0]['text'] += ' foreign attempt'
                    elif corruption == 'wrong_session':
                        f.live.agents[name]['binding']['session'] = str(uuid.uuid4())
                    elif corruption == 'wrong_native_session':
                        rows[0]['payload']['id'] = str(uuid.uuid4())
                    elif corruption == 'wrong_context':
                        next(r['payload'] for r in reversed(rows) if r['type'] == 'turn_context')['model'] = 'foreign'
                    elif corruption == 'wrong_completion':
                        complete['turn_id'] = str(uuid.uuid4())
                    raw = b'\n'.join(c.encoded(row) for row in rows) + b'\n'
                    f.live.raw[sid] = raw[:-1] if corruption == 'incomplete' else raw
                    calls = list(f.live.calls)
                    result_key = {'checkpoint_sent': 'checkpoint_sha256', 'restore_sent': 'restoration_transcript',
                                  'lead_sent': 'lead_receipt'}[phase]
                    for elapsed in (121, 500):
                        f.now = issued['since'] + elapsed
                        f.restart_controller()
                        f.ctrl.reconcile()
                        result = f.ctrl.step()
                        self.assertEqual(result['active']['phase'], 'blocked')
                        self.assertNotIn(result_key, result['active'])
                        self.assertEqual(result['completed'], [])
                        self.assertEqual(f.ctrl.reconcile()['active']['since'], issued['since'])
                    f.assert_original_intent(issued)
                    self.assertEqual(f.live.calls, calls)

    def test_late_prepared_checkpoint_keeps_existing_next_effect_gates(self):
        for change in ('none', 'file', 'session', 'work', 'busy'):
            with self.subTest(change=change):
                f, issued = self.phase_fixture('checkpoint_sent')
                f.now = issued['since'] + 121
                prepared = f.ctrl.step()['active']
                self.assertEqual(prepared['phase'], 'prepared')
                self.assertEqual(prepared['since'], issued['since'])
                calls = list(f.live.calls)
                if change == 'file': f.file.write_text('changed evidence')
                elif change == 'session': f.live.agents['researcher']['binding']['session'] = str(uuid.uuid4())
                elif change == 'work': f.lower('researcher', used=2000)
                elif change == 'busy': f.live.agents['researcher']['agent_status'] = 'working'
                f.now += 121
                f.restart_controller()
                result = f.ctrl.step()
                if change == 'none':
                    # prepared has an existing, explicit timeout exemption.
                    self.assertEqual(f.live.calls[len(calls):], [('researcher', '/new')])
                    self.assertEqual(result['active']['phase'], 'new_sent')
                    self.assertEqual(result['active']['since'], f.now)
                else:
                    self.assertEqual(f.live.calls, calls)
                f.assert_original_intent(issued)

    def test_late_warning_checkpoint_does_not_authorize_renewal(self):
        self.lower('researcher', used=11000); self.lower()
        sid = self.live.agents['researcher']['binding']['session']
        self.live.raw[sid] = self.live.raw[sid].replace(b'"model_context_window":20000', b'"model_context_window":100000').replace(
            b'"input_tokens":10900', b'"input_tokens":74900').replace(b'"total_tokens":11000', b'"total_tokens":75000')
        issued = copy.deepcopy(self.ctrl.step()['active'])
        self.assertFalse(issued['renew'])
        self.now = issued['since'] + 121
        result = self.ctrl.step()
        self.assertIsNone(result['active'])
        prepared = c.read_json(self.store.get(result['roles']['researcher']['prepared_sha256']))
        self.assertEqual(prepared['since'], issued['since'])
        self.assertFalse(prepared['renew'])
        self.restart_controller(); self.ctrl.step()
        self.assertEqual(len(self.live.calls), 1)
        self.assert_original_intent(issued)

    def test_late_lead_receipt_still_requires_unchanged_restoration(self):
        for change in ('work', 'session', 'file'):
            with self.subTest(change=change):
                f, issued = self.phase_fixture('lead_sent')
                calls = list(f.live.calls)
                if change == 'work': f.lower('researcher', used=2000)
                elif change == 'session': f.live.agents['researcher']['binding']['session'] = str(uuid.uuid4())
                else: f.file.write_text('changed after restore')
                f.now = issued['since'] + 121
                result = f.ctrl.step()
                self.assertEqual(result['active']['phase'], 'blocked')
                self.assertEqual(result['completed'], [])
                self.assertNotIn('lead_receipt', result['active'])
                self.assertEqual(f.live.calls, calls)
                self.assertEqual(f.ctrl.reconcile()['active']['since'], issued['since'])
                f.assert_original_intent(issued)

    def test_expired_next_effect_phases_remain_blocked(self):
        for phase in ('new_sent', 'status_sent', 'lead_pending'):
            with self.subTest(phase=phase):
                f, issued = self.phase_fixture(phase, delayed_metadata=phase == 'status_sent')
                calls = list(f.live.calls)
                f.now = issued['since'] + 121
                with mock.patch.object(f.live, 'get', wraps=f.live.get) as read:
                    result = f.ctrl.step()
                    self.assertEqual(read.call_count, 0)
                self.assertEqual(result['active']['phase'], 'blocked')
                self.assertEqual(result['active']['reason'], 'handoff_timeout_reconcile_without_resending')
                self.assertEqual(f.ctrl.reconcile()['active']['since'], issued['since'])
                self.assertEqual(f.live.calls, calls)

    def test_adjudication_crossing_deadline_does_not_refresh_since(self):
        for phase in ('checkpoint_sent', 'restore_sent'):
            with self.subTest(phase=phase):
                f, issued = self.phase_fixture(phase)
                _, sid = f.response_location(issued)
                raw = f.live.raw[sid]
                original = f.store.put
                def put(content):
                    if content == raw:
                        f.now = issued['since'] + 121
                    return original(content)
                f.now = issued['since'] + 119
                with mock.patch.object(f.store, 'put', side_effect=put):
                    result = f.ctrl.step()
                self.assertEqual(result['active']['since'], issued['since'])
                self.assertEqual(result['active']['phase'], 'prepared' if phase == 'checkpoint_sent' else 'lead_pending')

    def test_late_ambiguous_send_observes_completed_exact_attempt(self):
        for phase in ('checkpoint_sent', 'restore_sent', 'lead_sent'):
            with self.subTest(phase=phase):
                f, issued = self.phase_fixture(phase, ambiguous=True)
                self.assertTrue(issued['send_receipt']['ambiguous'])
                calls = list(f.live.calls)
                f.now = issued['since'] + 121
                f.restart_controller()
                result = f.ctrl.step()
                if phase == 'lead_sent': self.assertEqual(len(result['completed']), 1)
                else: self.assertEqual(result['active']['phase'], 'prepared' if phase == 'checkpoint_sent' else 'lead_pending')
                f.assert_original_intent(issued)
                self.assertEqual(f.live.calls, calls)

    def test_late_observation_persistence_interruptions_recover_exact_intent(self):
        class Interrupted(BaseException): pass
        original = c.Store.save
        for phase in ('checkpoint_sent', 'restore_sent', 'lead_sent'):
            for boundary in ('before_save', 'after_save', 'before_next_transition'):
                with self.subTest(phase=phase, boundary=boundary):
                    f, issued = self.phase_fixture(phase)
                    calls = list(f.live.calls)
                    f.now = issued['since'] + 121
                    destination = 'prepared' if phase == 'checkpoint_sent' else 'lead_pending'
                    def save(store, value):
                        adjudicated = ((value.get('active') or {}).get('phase') == destination
                                       if phase != 'lead_sent' else bool(value['completed']))
                        if adjudicated and boundary == 'before_save': raise Interrupted()
                        original(store, value)
                        if adjudicated and boundary == 'after_save': raise Interrupted()
                    if boundary == 'before_next_transition':
                        result = f.ctrl.step()
                        # A completed handoff has no next active transition.
                        if phase != 'lead_sent':
                            with mock.patch.object(f.ctrl, 'advance', side_effect=Interrupted):
                                with self.assertRaises(Interrupted): f.ctrl.step()
                    else:
                        with mock.patch.object(c.Store, 'save', new=save):
                            with self.assertRaises(Interrupted): f.ctrl.step()
                    f.restart_controller()
                    durable = f.ctrl.status()
                    if boundary == 'before_save':
                        self.assertEqual(durable['active']['phase'], phase)
                        self.assertEqual(durable['active']['since'], issued['since'])
                        result = f.ctrl.step()
                    else:
                        result = f.ctrl.reconcile()
                        with mock.patch.object(f.ctrl, 'response', side_effect=AssertionError('adjudicated result read again')):
                            if phase == 'lead_sent': f.ctrl.step()
                            else: f.ctrl.reconcile()
                    if phase == 'lead_sent':
                        self.assertEqual(len(result['completed']), 1)
                        retained = c.read_json(f.store.get(result['completed'][0]))
                        evidence = retained['lead_receipt']
                    else:
                        retained = result['active']
                        self.assertEqual(retained['phase'], destination)
                        evidence = (c.read_json(f.store.get(retained['checkpoint_sha256']))['summary_transcript']
                                    if phase == 'checkpoint_sent' else retained['restoration_transcript'])
                    _, sid = f.response_location(issued)
                    self.assertEqual(f.store.get(evidence), f.live.raw[sid])
                    self.assertEqual(retained['since'], issued['since'])
                    self.assertEqual(retained['id'], issued['id'])
                    f.assert_original_intent(issued)
                    self.assertEqual(f.live.calls, calls)

    def test_complete_recall_and_lead_receipt_are_durable(self):
        self.lower('researcher',used=11000);self.lower()
        state=self.run_steps()
        self.assertEqual(len(state['completed']),1)
        self.assertEqual(state['roles']['researcher']['generation'],1)
        record=c.read_json(self.store.get(state['completed'][0]))
        checkpoint=c.read_json(self.store.get(record['checkpoint_sha256']))
        self.assertEqual(checkpoint['summary']['completed'],['Camera was implemented'])
        self.assertEqual(checkpoint['summary']['pending'],['Test camera exit'])
        self.assertNotEqual(record['source_binding']['session'],record['new_binding']['session'])
        self.assertTrue(record['lead_receipt']);self.assertTrue(record['restoration_transcript'])
        self.assertEqual(sum(prompt=='/new' for _,prompt in self.live.calls),1)

    def test_each_completed_milestone_saved_below_threshold(self):
        self.lower('researcher');self.lower()
        state=self.ctrl.step()
        self.assertEqual(len(self.live.calls),0)
        first=state['roles']['researcher']['milestone_sha256']
        self.lower('researcher',used=2000)
        state=self.ctrl.step()
        second=c.read_json(self.store.get(state['roles']['researcher']['milestone_sha256']))
        self.assertEqual(second['previous'],first)

    def test_auto_never_sends_to_working_or_blocked_agent(self):
        self.lower()
        for status in ['working','blocked','unknown']:
            self.live.agents['researcher']['agent_status']=status
            self.ctrl.step()
        self.assertEqual(self.live.calls,[])

    def test_stale_does_not_auto_reset_but_explicit_request_can_prepare(self):
        self.lower('researcher',used=11000,age=301);self.lower()
        self.ctrl.step();self.assertFalse(self.live.calls)
        state=self.ctrl.step('researcher');self.assertEqual(state['active']['phase'],'checkpoint_sent')

    def test_auto_at_eighty_and_warning_only_at_seventy(self):
        self.lower('researcher',used=15000);self.lower()
        # More than 8k reserve is required: use a 100k effective window.
        sid=self.live.agents['researcher']['binding']['session']
        self.live.raw[sid]=self.live.raw[sid].replace(b'"model_context_window":20000',b'"model_context_window":100000').replace(b'"input_tokens":14900',b'"input_tokens":74900').replace(b'"total_tokens":15000',b'"total_tokens":75000')
        self.ctrl.step();state=self.ctrl.step()
        self.assertIsNone(state['active'])
        self.assertIn('prepared_sha256',state['roles']['researcher'])
        self.ctrl.step();self.assertEqual(len(self.live.calls),1)
        self.ctrl.step('researcher');self.ctrl.step()
        self.assertIn(('researcher','/new'),self.live.calls)

    def test_insufficient_margin_refuses_summary_call(self):
        state=self.ctrl.step()
        self.assertEqual(state['observation_error'],'insufficient_margin_for_checkpoint')
        self.assertFalse(self.live.calls)

    def test_auto_renewal_uses_eighty_percent_with_enough_reserve(self):
        self.lower('researcher',used=11000);self.lower()
        sid=self.live.agents['researcher']['binding']['session']
        self.live.raw[sid]=self.live.raw[sid].replace(b'"model_context_window":20000',b'"model_context_window":100000').replace(b'"input_tokens":10900',b'"input_tokens":84900').replace(b'"total_tokens":11000',b'"total_tokens":85000')
        state=self.run_steps(manual=None)
        self.assertEqual(len(state['completed']),1)
        record=c.read_json(self.store.get(state['completed'][0]))
        self.assertFalse(record['manual'])

    def test_timeout_after_effect_is_reconciled_without_duplicate_send(self):
        self.lower('researcher',used=11000);self.lower()
        self.live.fail_after_send=True
        state=self.run_steps()
        self.assertEqual(len(state['completed']),1)
        self.assertEqual(len(self.live.calls),4)

    def test_unsent_ambiguous_intent_is_never_blindly_retried(self):
        self.lower('researcher',used=11000);self.lower()
        self.live.drop_send=True
        self.ctrl.step('researcher')
        self.ctrl.step();self.ctrl.step()
        self.assertEqual(len(self.live.calls),1)
        self.now+=121
        state=self.ctrl.step()
        self.assertEqual(state['active']['phase'],'blocked')

    def test_restarted_controller_resumes_same_intent(self):
        self.lower('researcher',used=11000);self.lower()
        first=self.ctrl.step('researcher')['active']['id']
        self.ctrl=c.Controller(self.plan,self.config,self.store,self.live,lambda:self.now)
        state=self.run_steps(8,manual=None)
        record=c.read_json(self.store.get(state['completed'][0]))
        self.assertEqual(record['id'],first);self.assertEqual(len(self.live.calls),4)

    def test_file_changed_after_checkpoint_prevents_new_session(self):
        self.lower('researcher',used=11000)
        self.ctrl.step('researcher');self.ctrl.step()
        self.file.write_text('altered')
        state=self.ctrl.step()
        self.assertEqual(state['active']['reason'],'checkpoint_file_changed')
        self.assertNotIn(('researcher','/new'),self.live.calls)

    def test_unknown_or_changed_session_does_not_reuse_prepared(self):
        self.lower('researcher',used=11000)
        self.ctrl.step('researcher');self.ctrl.step()
        self.live.agents['researcher']['binding']['session']=str(uuid.uuid4())
        state=self.ctrl.step()
        self.assertEqual(state['active']['phase'],'blocked')

    def test_modified_objects_rejected(self):
        sha=self.store.put(b'original');path=self.store.root/'objects'/sha
        path.chmod(0o600);path.write_bytes(b'forged')
        with self.assertRaises(c.ContinuityError):self.store.get(sha)

    def test_plan_drift_rejected_after_restart(self):
        self.lower('researcher',used=11000);self.ctrl.step('researcher')
        different=copy.deepcopy(self.plan);different['renew_percent']=90
        other=c.Controller(different,self.config,self.store,self.live,lambda:self.now)
        with self.assertRaises(c.ContinuityError):other.status()

    def test_wrong_model_policy_effort_or_version_rejected(self):
        member=self.members[0];sid=self.live.agents['researcher']['binding']['session']
        raw=transcript(member,sid)
        for before,after in [(b'"observed-model"',b'"different"'),(b'"never"',b'"always"'),(b'"effort":null',b'"effort":"high"'),(b'"0.153.0"',b'"0.152.0"'),(b'"read-only"',b'"workspace-write"')]:
            with self.subTest(before=before):
                with self.assertRaises(c.ContinuityError):c.native(raw.replace(before,after),member,sid)

    def test_partial_json_duplicate_keys_and_wrong_completion_rejected(self):
        member=self.members[0];sid=self.live.agents['researcher']['binding']['session'];raw=transcript(member,sid)
        for bad in [raw[:-1],raw.replace(b'"last_agent_message":"Completed milestone"',b'"last_agent_message":"forged"'),raw.replace(b'"model":"observed-model"',b'"model":"observed-model","model":"other"')]:
            with self.assertRaises((c.ContinuityError,ValueError)):c.native(bad,member,sid)

    def test_unicode_separators_inside_json_are_preserved(self):
        m=self.members[0];sid=self.live.agents['researcher']['binding']['session']
        result=c.native(transcript(m,sid,final='before\u2028after'),m,sid)
        self.assertEqual(result['done']['final'],'before\u2028after')

    def test_native_initialization_prelude_is_separate_from_task_input(self):
        m=self.members[0];sid=self.live.agents['researcher']['binding']['session']
        rows=[json.loads(line) for line in transcript(m,sid).splitlines()]
        rows.insert(2,dict(type='response_item',payload=dict(type='message',role='user',content=[dict(type='input_text',text='runtime environment context')])) )
        result=c.native(b'\n'.join(c.encoded(r) for r in rows)+b'\n',m,sid)
        self.assertEqual(result['done']['user_messages'],1)
        self.assertEqual(len(result['done']['initialization_prelude_sha256']),1)

    def test_forged_ack_never_reports_continuity_complete(self):
        self.lower('researcher',used=11000);self.lower()
        self.live.corrupt_ack=True
        state=self.run_steps(7)
        self.assertEqual(state['completed'],[]);self.assertEqual(state['active']['phase'],'blocked')

    def test_symlink_and_outside_evidence_rejected(self):
        link=self.product/'alias.md';link.symlink_to(self.file)
        for path in [link,self.root/'outside.md']:
            with self.assertRaises(c.ContinuityError):c.evidence(self.store,self.members[0],[str(path)])

    def test_reference_notes_and_urls_are_not_opened_as_files(self):
        files=c.evidence(self.store,self.members[0],['LEÍDO: '+str(self.file),'https://example.invalid/paper'])
        self.assertEqual([f['path'] for f in files],[str(self.file)])

    def test_reconcile_preserves_intent_and_deadline_without_resend(self):
        self.lower('researcher',used=11000);self.lower()
        state=self.ctrl.step('researcher');since=state['active']['since']
        original=self.ctrl.response
        self.ctrl.response=lambda *args: (_ for _ in ()).throw(c.ContinuityError('local_reader_error'))
        state=self.ctrl.step();self.assertEqual(state['active']['phase'],'blocked')
        self.ctrl.response=original;self.now+=5
        state=self.ctrl.reconcile()
        self.assertEqual(state['active']['since'],since)
        state=self.ctrl.step();self.assertEqual(state['active']['phase'],'prepared')
        self.assertEqual(len(self.live.calls),1)

    def test_lead_busy_delays_reporting_without_interrupting(self):
        self.lower('researcher',used=11000);self.lower()
        self.live.agents['lead']['agent_status']='working'
        state=self.run_steps(8)
        self.assertEqual(state['active']['phase'],'lead_pending')
        self.assertFalse(any(n=='lead' for n,_ in self.live.calls))
        self.live.agents['lead']['agent_status']='idle'
        self.ctrl.step();state=self.ctrl.step()
        self.assertEqual(len(state['completed']),1)

    def test_previous_session_cannot_be_reused_after_completed_generation(self):
        self.lower('researcher',used=11000);self.lower()
        old=copy.deepcopy(self.live.agents['researcher'])
        state=self.run_steps()
        self.live.agents['researcher']=old
        state=self.ctrl.step('researcher')
        self.assertEqual(state['observation_error'],'registered_generation_session_drift')

    def test_delayed_metadata_uses_status_then_checks_native_restoration(self):
        self.lower('researcher',used=11000);self.lower();self.live.delayed_metadata=True
        state=self.run_steps(10)
        self.assertEqual(len(state['completed']),1)
        self.assertEqual(sum(p=='/status' for _,p in self.live.calls),1)
        self.assertEqual(sum(p=='/new' for _,p in self.live.calls),1)

    def test_new_work_before_lead_receipt_cannot_close_old_handoff(self):
        self.lower('researcher',used=11000);self.lower()
        state=self.run_steps(5)
        self.assertEqual(state['active']['phase'],'lead_pending')
        self.lower('researcher',used=2000)
        state=self.ctrl.step()
        self.assertEqual(state['active']['reason'],'new_work_superseded_restoration')
        self.assertEqual(state['completed'],[])

    def test_second_writer_cannot_enter_controller_lock(self):
        with self.store.lock():
            with self.assertRaises(BlockingIOError):
                with self.store.lock():pass

    def test_exact_ack_envelope_only(self):
        ack={'status':'RECEIVED','renewal_id':'exact'}
        self.assertEqual(c.acknowledgement(c.encoded({'acknowledgement':ack})),ack)
        self.assertNotEqual(c.acknowledgement(c.encoded({'acknowledgement':ack,'extra':'ignored?'})),ack)

    def test_counter_does_not_transfer_memory_label_to_another_session(self):
        self.lower('researcher',used=11000);self.lower();state=self.run_steps()
        item={'name':'researcher','binding':['researcher','p-researcher','different-session',str(self.product)]}
        c.context.attach_continuity([item],str(self.store.root))
        self.assertEqual(item['continuity'],'sin memoria verificada para esta sesión')

    def test_counter_rejects_modified_continuity_object(self):
        self.lower('researcher',used=11000);self.lower();self.run_steps()
        pointer=json.loads((self.store.root/'head.json').read_text());path=self.store.root/'objects'/pointer['sha256']
        path.chmod(0o600);path.write_bytes(b'{}')
        item={'name':'researcher'};c.context.attach_continuity([item],str(self.store.root))
        self.assertEqual(item['continuity'],'SIN DATO VERIFICABLE')

    def test_offline_replay_checks_complete_journal_and_native_receipts(self):
        self.lower('researcher',used=11000);self.lower();self.run_steps()
        report=self.ctrl.verify()
        self.assertEqual(report['completed'][0]['status'],'PASS')
        self.assertFalse(report['quality_guarantee'])
        record=c.read_json(self.store.get(self.ctrl.status()['completed'][0]))
        path=self.store.root/'objects'/record['lead_receipt'];path.chmod(0o600);path.write_bytes(b'forged')
        with self.assertRaises(c.ContinuityError):self.ctrl.verify()


if __name__=='__main__':unittest.main()
