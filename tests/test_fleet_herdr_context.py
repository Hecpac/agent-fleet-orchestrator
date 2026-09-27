import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
spec = importlib.util.spec_from_file_location("context", Path(__file__).resolve().parents[1]/"scripts/fleet_herdr_context.py")
ctx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctx)
SID = "12345678-1234-4234-8234-123456789abc"
CWD = "/candidate"
NOW = ctx.stamp("2026-09-07T22:00:00Z")


def event(kind, **payload):
    return dict(type="event_msg", timestamp="2026-09-07T22:00:00Z", payload=dict(type=kind, **payload))


def rows():
    return [dict(type="session_meta", payload=dict(id=SID, cwd=CWD)),
            event("task_started", turn_id="turn"),
            dict(type="turn_context", payload=dict(turn_id="turn", model="observed-model", effort=None)),
            event("token_count", info=dict(model_context_window=1000,
                total_token_usage=dict(total_tokens=9000000),
                last_token_usage=dict(input_tokens=800, output_tokens=100, total_tokens=900, cached_input_tokens=700)))]


class MeasureTests(unittest.TestCase):
    def check(self, data=None, **kw):
        return ctx.measure(rows() if data is None else data, SID, CWD, now=NOW, **kw)

    def test_last_call_not_cumulative_or_uncached_usage(self):
        r = self.check()
        self.assertEqual((r['used_tokens'], r['available_tokens'], r['used_percent']), (900,100,90))
        self.assertEqual(r['model'], 'observed-model')
        self.assertEqual(r['pressure'], 'HIGH')

    def test_duplicate_snapshots_are_not_summed(self):
        data = rows(); data.append(copy.deepcopy(data[-1]))
        self.assertEqual(self.check(data)['used_tokens'],900)

    def test_stale_is_explicit_and_keeps_historical_value(self):
        r = ctx.measure(rows(),SID,CWD,now=NOW+301)
        self.assertEqual(r['status'],'STALE')
        self.assertIn('ANT',ctx.badge(r)[0])

    def test_missing_zero_boolean_negative_and_inconsistent_counters(self):
        for field, value in [('model_context_window',None),('model_context_window',0),('model_context_window',True),('total_tokens',-1),('total_tokens',850),('input_tokens',True)]:
            with self.subTest(field=field,value=value):
                data=rows(); info=data[-1]['payload']['info']
                (info if field=='model_context_window' else info['last_token_usage'])[field]=value
                self.assertIsNone(self.check(data)['used_tokens'])

    def test_identity_cannot_transfer_between_session_or_cwd(self):
        for key in ['id','cwd']:
            data=rows(); data[0]['payload'][key]='other'
            self.assertEqual(self.check(data)['status'],'UNKNOWN')

    def test_new_turn_and_compaction_invalidate_previous_counter(self):
        for extra in [event('task_started',turn_id='next'),event('context_compacted'),dict(type='compacted',payload={})]:
            self.assertIsNone(self.check(rows()+[extra])['used_tokens'])

    def test_compaction_can_be_followed_by_smaller_fresh_usage(self):
        data=rows()+[dict(type='compacted',payload={})]
        new=rows()[-1]; new['payload']['info']['last_token_usage']=dict(input_tokens=100,output_tokens=20,total_tokens=120)
        data.append(new)
        self.assertEqual(self.check(data)['used_tokens'],120)

    def test_exceeded_window_is_not_hidden_or_negative_available(self):
        data=rows(); data[-1]['payload']['info']['model_context_window']=500
        r=self.check(data)
        self.assertEqual((r['pressure'],r['used_percent'],r['available_tokens']),('EXCEEDED',180,0))

    def test_future_timestamp_and_unbound_turn(self):
        data=rows(); data[-1]['timestamp']='2027-01-01T00:00:00Z'
        self.assertEqual(self.check(data)['status'],'UNKNOWN')
        data=rows(); data[2]['payload']['turn_id']='different'
        self.assertEqual(self.check(data)['reason'],'usage_turn_unbound')


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name); self.dir=self.home/'sessions/2026/09/07'; self.dir.mkdir(parents=True)
        self.path=self.dir/f'rollout-{SID}.jsonl'; self.reader=ctx.Reader()

    def read(self):
        return self.reader.read(str(self.home),SID,CWD,now=NOW,stale_after=300)

    def write(self,data):
        self.path.write_text(''.join(json.dumps(r)+'\n' for r in data))

    def test_missing_partial_and_malformed_transcripts(self):
        self.assertEqual(self.read()['status'],'UNKNOWN')
        self.write(rows()); self.path.write_text(self.path.read_text()+'{"partial":')
        self.assertEqual(self.read()['status'],'UNKNOWN')
        self.write(rows()); self.path.write_text('invalid\n'+self.path.read_text())
        self.assertEqual(self.read()['status'],'UNKNOWN')

    def test_ambiguous_and_symlink_paths_rejected(self):
        self.write(rows()); other=self.dir/f'other-{SID}.jsonl'; other.write_bytes(self.path.read_bytes())
        self.assertEqual(self.read()['status'],'UNKNOWN')
        self.path.unlink(); self.path.symlink_to(other)
        other.rename(self.dir/'source.jsonl')
        self.assertEqual(self.read()['status'],'UNKNOWN')

    def test_rewritten_file_does_not_reuse_cached_counter(self):
        self.write(rows()); self.assertEqual(self.read()['used_tokens'],900)
        self.write(rows()[:3]); self.assertIsNone(self.read()['used_tokens'])


class LiveTests(unittest.TestCase):
    def agent(self,sid=SID):
        return dict(agent='codex',name='lead',pane_id='w1:p1',cwd=CWD,agent_status='idle',
                    agent_session=dict(agent='codex',source='herdr:codex',kind='id',value=sid))

    def test_changed_live_session_prevents_publication(self):
        item=dict(name='lead',label='Lead',binding=list(ctx.live_binding(self.agent())),**ctx.measure(rows(),SID,CWD,now=NOW))
        calls=[]
        def run(config,args):
            calls.append(args); return {'agent':self.agent('different')}
        self.assertFalse(ctx.publish({},item,run=run))
        self.assertEqual(len(calls),1)

    def test_publisher_only_mutates_expiring_metadata(self):
        item=dict(name='lead',label='Lead',binding=list(ctx.live_binding(self.agent())),**ctx.measure(rows(),SID,CWD,now=NOW))
        calls=[]
        def run(config,args):
            calls.append(args)
            if args[0]=='agent': return {'agent':self.agent()}
            if args[:2]==['pane','get']:
                title, detail=ctx.badge(item)
                return {'pane':dict(self.agent(),tokens=dict(zip(ctx.TOKEN_KEYS,('Lead',title,detail))))}
            return {}
        self.assertTrue(ctx.publish({},item,run=run))
        self.assertEqual(calls[1][:3],['pane','report-metadata','w1:p1'])
        self.assertIn('--ttl-ms',calls[1]); self.assertNotIn('--title',calls[1])

    def test_silent_metadata_success_requires_readback(self):
        item=dict(name='lead',label='Lead',binding=list(ctx.live_binding(self.agent())),**ctx.measure(rows(),SID,CWD,now=NOW))
        def run(config,args):
            if args[0]=='agent': return {'agent':self.agent()}
            return {'pane':dict(self.agent(),tokens={})}
        self.assertFalse(ctx.publish({},item,run=run))

    def test_unavailable_backend_is_unknown_not_zero(self):
        def run(config,args): raise ValueError('offline')
        result=ctx.snapshot({},[dict(name='lead',label='Lead')],None,now=NOW,run=run)
        self.assertIsNone(result[0]['used_percent'])


if __name__ == '__main__':
    unittest.main()
