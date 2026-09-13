"""Subscription credentials, real local HTTP egress, ledger and native capsule.

Every server and credential in this suite is synthetic. No external request.
"""
import base64
import copy
import http.server
import io
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import test_fleet_codex_responses as fixtures
import fleet_artifacts as artifacts
import fleet_chatgpt_provider as subscription
import fleet_codex_responses as responses
import fleet_herdr_inference as inference
import fleet_json


def token(expiry):
    claims = base64.urlsafe_b64encode(json.dumps({'exp':expiry}).encode()).decode().rstrip('=')
    return 'synthetic.'+claims+'.signature'


def auth(home, *, account='account-a', expiry=None, mode='chatgpt'):
    raw = {'auth_mode':mode,'OPENAI_API_KEY':None,
        'tokens':{'access_token':token(time.time()+3600 if expiry is None else expiry),
                  'account_id':account,'refresh_token':'SYNTHETIC_REFRESH_NEVER_SENT'}}
    path = home/'auth.json'; path.write_text(json.dumps(raw)); path.chmod(0o600)
    return raw


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.home=Path(tmp.name); self.record=auth(self.home)

    def test_preflight_uses_subscription_and_never_exposes_or_writes_tokens(self):
        before=(self.home/'auth.json').read_bytes()
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY':'SYNTHETIC_API_KEY'}), \
             mock.patch.object(subscription.http.client, 'HTTPSConnection') as connect:
            result=subscription.preflight(self.home)
            connect.assert_not_called()
        self.assertEqual(result['authentication'],'chatgpt')
        self.assertEqual(result['server_authentication'],'NOT_VERIFIED')
        self.assertEqual(result['network_requests'],0)
        for secret in (*self.record['tokens'].values(), 'SYNTHETIC_API_KEY'):
            self.assertNotIn(secret,json.dumps(result))
        self.assertEqual((self.home/'auth.json').read_bytes(),before)

    def test_expired_api_login_and_unsafe_file_are_rejected(self):
        for changes in [{'expiry':0},{'mode':'apikey'}]:
            auth(self.home,**changes)
            with self.assertRaises(inference.InferenceError): subscription.preflight(self.home)
        auth(self.home); (self.home/'auth.json').chmod(0o644)
        with self.assertRaises(inference.InferenceError): subscription.preflight(self.home)
        (self.home/'auth.json').rename(self.home/'real.json')
        (self.home/'auth.json').symlink_to(self.home/'real.json')
        with self.assertRaises(inference.InferenceError): subscription.preflight(self.home)

    def test_auth_route_and_workspace_changes_are_not_silently_overridden(self):
        for config in ['chatgpt_base_url="https://untrusted.invalid"',
                       'cli_auth_credentials_store="keyring"', 'forced_chatgpt_workspace_id="another-account"']:
            (self.home/'config.toml').write_text(config)
            with self.assertRaises(inference.InferenceError): subscription.preflight(self.home)

    def test_changed_account_requires_a_new_binding(self):
        provider=subscription.ChatGPTProvider(self.home)
        original=provider.binding
        auth(self.home,account='account-b')
        with self.assertRaisesRegex(inference.InferenceError,'account changed'): provider.ready()
        self.assertEqual(provider.binding,original)

    def test_production_transport_has_fixed_tls_endpoint_and_headers(self):
        provider=subscription.ChatGPTProvider(self.home)
        body={'model':'fixture-model','instructions':'Synthetic test.',
            'input':[{'role':'user','content':[{'type':'input_text','text':'fixture'}]}],
            'tools':[], 'tool_choice':'none','parallel_tool_calls':False,'reasoning':{'effort':'low'},
            'store':False,'stream':True,'include':[]}
        envelope={'input':json.dumps(body),'model':'fixture-model','effort':'low','max_output_tokens':32,
            'policy_id':'a'*64,'request_id':'fixture','request_sha256':'b'*64}
        stream=io.BytesIO(responses.sse(fixtures.bundle(body,[fixtures.message()])))
        stream.status=200; stream.getheader=lambda *args: 'text/event-stream'
        with mock.patch.object(subscription.http.client,'HTTPSConnection') as factory:
            conn=factory.return_value; conn.getresponse.return_value=stream
            result=subscription._worker({'home':str(self.home),'provider':provider.binding,'timeout':3},envelope)
        self.assertEqual(factory.call_args.args,('chatgpt.com',443))
        self.assertTrue(factory.call_args.kwargs['context'].check_hostname)
        self.assertEqual(conn.request.call_args.args,('POST','/backend-api/codex/responses'))
        headers=conn.request.call_args.kwargs['headers']
        self.assertEqual(headers['Authorization'],'Bearer '+self.record['tokens']['access_token'])
        self.assertEqual(headers['ChatGPT-Account-ID'],'account-a')
        self.assertNotIn('SYNTHETIC_REFRESH_NEVER_SENT',repr(headers))
        self.assertEqual(json.loads(conn.request.call_args.kwargs['body']),body)
        self.assertEqual(json.loads(result)['output_tokens'],3)
        conn.close.assert_called_once()


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.requests=[]; self.mode='complete'; self.reached=threading.Event(); self.release=threading.Event()
        self.on_request=None
        owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append({'path':self.path,'headers':dict(self.headers),'body':body})
                owner.reached.set()
                if owner.on_request: owner.on_request()
                if owner.mode=='stall':
                    owner.release.wait(5)
                    return
                if owner.mode=='redirect':
                    self.send_response(307); self.send_header('Location','https://untrusted.invalid/'); self.end_headers(); return
                if owner.mode=='unauthorized':
                    self.send_response(401); self.end_headers(); return
                output=[fixtures.message()]
                if owner.mode=='tool' and len(owner.requests)==1:
                    patch='*** Begin Patch\n*** Add File: subscription.txt\n+SUBSCRIPTION_ADAPTER_OK\n*** End Patch'
                    output=[{'id':'ct_fixture','type':'custom_tool_call','call_id':'call_fixture',
                        'namespace':'functions','name':'exec','input':'text(await tools.apply_patch('+json.dumps(patch)+'));'}]
                if owner.mode=='echo': output=[fixtures.message(subscription.SYNTHETIC_TOKEN)]
                values=fixtures.bundle(body,output)
                values.insert(1,{'type':'response.in_progress','response':{'id':'resp_fixture','status':'in_progress'}})
                for n,event in enumerate(values): event['sequence_number']=n
                raw=responses.sse(values)
                self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Content-Length',str(len(raw))); self.end_headers()
                try: self.wfile.write(raw)
                except BrokenPipeError: pass
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02}); self.thread.start()
        self.addCleanup(self.stop_server)
        self.provider=subscription.ChatGPTProvider.fixture(self.server.server_port)
        self.addCleanup(self.provider.close)
        self.f=fixtures.ResponsesTests()
        self.f.provider_binding=self.provider.binding
        self.f.provider_factory=lambda:self.provider
        self.f.max_input_bytes=256*1024
        self.f.call_timeout_ms=3000
        self.f.setUp(); self.addCleanup(self.f.doCleanups); self.addCleanup(self.f.join_peers)

    def stop_server(self):
        self.release.set(); self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def assert_no_credentials(self):
        for p in (self.f.runs/'missions'/self.f.mid/'artifacts').iterdir():
            if p.is_file():
                self.assertNotIn(subscription.SYNTHETIC_TOKEN.encode(),p.read_bytes())
                self.assertNotIn(self.f.bridge.token.encode(),p.read_bytes())
        self.assertIsNone(self.provider.child)

    def test_real_http_egress_bound_to_ledger_and_replay_does_not_send_again(self):
        result=self.f.http(); self.f.decode(result)
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.requests[0]['path'],'/backend-api/codex/responses')
        self.assertEqual(self.requests[0]['headers']['Authorization'],'Bearer '+subscription.SYNTHETIC_TOKEN)
        self.assertEqual(self.requests[0]['body'],self.f.body)
        self.assertEqual(self.f.http()['raw'],result['raw'])
        self.assertEqual(len(self.requests),1)
        verified=inference.verify_evidence(self.f.current(),lambda pin:artifacts.get_bytes(self.f.runs,self.f.mid,pin))
        self.assertEqual(verified['requests']['completed'],1)
        self.assertEqual(verified['provider_execution'],'SIMULATED')
        self.assertEqual(self.f.bridge.policy['schema_version'],2)
        self.assert_no_credentials()

    def test_unknown_account_binding_rejects_before_http_or_reservation(self):
        self.provider._binding['account_sha256']='f'*64
        before=self.f.current()['head_sha256']
        self.assertEqual(self.f.http()['status'],400)
        self.assertEqual(self.requests,[])
        self.assertEqual(self.f.current()['head_sha256'],before)

    def test_redirect_is_not_followed_and_ambiguous_request_is_never_retried(self):
        self.mode='redirect'
        self.assertEqual(self.f.http()['status'],400)
        self.assertEqual(self.f.http()['status'],400)
        self.assertEqual(len(self.requests),1)
        with self.assertRaises(inference.InferenceError): inference.require_settled(self.f.current())
        self.assert_no_credentials()

    def test_expired_server_session_does_not_refresh_or_fall_back_to_api(self):
        self.mode='unauthorized'
        with mock.patch.dict(os.environ,{'OPENAI_API_KEY':'SYNTHETIC_MUST_NOT_BE_USED'}):
            self.assertEqual(self.f.http()['status'],400)
        self.assertEqual(len(self.requests),1)
        self.assertNotIn('SYNTHETIC_MUST_NOT_BE_USED',repr(self.requests))
        self.assert_no_credentials()

    def test_safe_failure_diagnostics_distinguish_http_from_sse_without_secrets(self):
        for mode, phase, code, status in [('unauthorized','http','http_status',401),
                ('redirect','http','http_status',307), ('echo','sse','credential_echo',None)]:
            with self.subTest(mode=mode):
                self.mode=mode
                provider=subscription.ChatGPTProvider.fixture(self.server.server_port)
                self.addCleanup(provider.close)
                envelope={'input':json.dumps(self.f.body),'model':self.f.body['model'],
                    'effort':self.f.body['reasoning']['effort'],'max_output_tokens':32,
                    'policy_id':'a'*64,'request_id':'fixture','request_sha256':'b'*64}
                with self.assertRaises(subscription.SubscriptionFailure):
                    provider.exchange(fleet_json.canonical_bytes(envelope),time.monotonic()+2,lambda:None)
                self.assertEqual(provider.last_diagnostic,subscription.diagnostic(phase,code,status))
                self.assertNotIn(subscription.SYNTHETIC_TOKEN,json.dumps(provider.last_diagnostic))
                self.assertIsNone(provider.child)

    def test_credential_echo_is_discarded_before_cas_or_client(self):
        self.mode='echo'
        result=self.f.http()
        self.assertEqual(result['status'],400)
        self.assertNotIn(subscription.SYNTHETIC_TOKEN.encode(),result['raw'])
        self.assert_no_credentials()

    def test_cancel_stops_egress_child_without_claiming_remote_quiescence(self):
        self.mode='stall'; self.on_request=lambda:self.f.pause('cancel')
        start=time.monotonic()
        self.assertEqual(self.f.http()['status'],400)
        self.assertLess(time.monotonic()-start,2)
        self.assertIsNone(self.provider.child)
        self.assertEqual(next(iter(self.f.requests().values()))['result']['status'],'indeterminate')
        with self.assertRaises(inference.InferenceError): inference.require_settled(self.f.current())

    def test_deadline_stops_pending_http_child_without_resend(self):
        self.mode='stall'
        self.f.broker.local_deadline=time.monotonic()+.5
        start=time.monotonic()
        self.assertEqual(self.f.http()['status'],400)
        self.assertLess(time.monotonic()-start,1.5)
        self.assertEqual(len(self.requests),1)
        self.assertIsNone(self.provider.child)
        self.assertEqual(next(iter(self.f.requests().values()))['result']['status'],'indeterminate')

    def test_worker_deadline_survives_loss_of_supervisor_channel(self):
        self.mode='stall'
        parent,peer=socket.socketpair()
        self.addCleanup(parent.close); self.addCleanup(peer.close)
        child=subprocess.Popen([sys.executable,'-I','-S','-B',str(Path(subscription.__file__).resolve()),
            '--worker-fd',str(peer.fileno())],pass_fds=(peer.fileno(),),close_fds=True,
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            env={'PATH':'/usr/bin:/bin','LANG':'en_US.UTF-8'})
        def reap():
            if child.poll() is None: child.kill()
            child.wait(timeout=2)
        self.addCleanup(reap); peer.close()
        envelope={'policy_id':self.f.policy_id,'request_id':'owned-fixture','request_sha256':'a'*64,
            'profile':inference.RESPONSES_PROFILE,'input':json.dumps(self.f.body),
            'model':self.f.body['model'],'effort':self.f.body['reasoning']['effort'],'max_output_tokens':1024}
        inference.write_frame(parent,fleet_json.canonical_bytes({'spec':{'provider':self.provider.binding,
            'port':self.server.server_port,'home':None,'timeout':.3},'envelope':envelope}),time.monotonic()+2)
        self.assertTrue(self.reached.wait(1))
        parent.close()
        self.assertEqual(child.wait(timeout=2),-signal.SIGALRM)
        self.assertEqual(len(self.requests),1)

    def test_progress_event_cannot_change_response_identity(self):
        values=fixtures.bundle(self.f.body,[fixtures.message()])
        values.insert(1,{'type':'response.in_progress','response':{'id':'other','status':'in_progress'}})
        with self.assertRaises(inference.InferenceError):
            responses.events(fleet_json.canonical_bytes(values),self.f.body,self.f.body['model'],32)

    def test_offline_evidence_rejects_transport_relabeling(self):
        self.assertEqual(self.f.http()['status'],200)
        current=self.f.current(); current['inference_policies'][self.f.policy_id]['policy']['provider']['transport']=subscription.TRANSPORT
        with self.assertRaises(inference.InferenceError):
            inference.verify_evidence(current,lambda pin:artifacts.get_bytes(self.f.runs,self.f.mid,pin))

    @unittest.skipUnless(os.environ.get('FLEET_CODEX_IMAGE') and sys.platform=='darwin','explicit installed Codex required')
    def test_installed_cli_tool_roundtrip_through_subscription_adapter_fixture(self):
        self.mode='tool'
        result=self.f.bridge.execute_confined(b'Synthetic subscription adapter fixture.',
            image=Path(os.environ['FLEET_CODEX_IMAGE']),files={},parent=self.f.tmp)
        self.assertEqual(result.report['returncode'],0,(result.report,result.stderr))
        self.assertEqual(result.files,{'subscription.txt':b'SUBSCRIPTION_ADAPTER_OK\n'})
        self.assertEqual(len(self.requests),2)
        self.assertTrue(result.report['cleanup_confirmed'])
        self.assertEqual(result.report['provider_execution'],'SIMULATED')
        self.assert_no_credentials()


if __name__=='__main__': unittest.main()
