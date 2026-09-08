import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
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
