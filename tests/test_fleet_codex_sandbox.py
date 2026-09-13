"""Real installed CLI + code-mode host; all provider responses are synthetic."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests import test_fleet_codex_responses as fixtures
import fleet_codex_sandbox as capsule
import fleet_herdr_effects as effects
import fleet_native_sandbox as sandbox
import fleet_json

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get('FLEET_CODEX_IMAGE')


class ContractTests(unittest.TestCase):
    def test_transcript_export_never_follows_a_guest_owned_ancestor_link(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); home=root/'home'; home.mkdir()
            identity=(home.stat().st_dev,home.stat().st_ino)
            outside=root/'outside'; outside.mkdir()
            (outside/'synthetic.jsonl').write_text('SYNTHETIC_ONLY')
            (home/'sessions').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(sandbox.SandboxError):
                capsule._transcripts(home,identity)
            (home/'sessions').unlink(); (home/'sessions/2026').mkdir(parents=True)
            (home/'sessions/2026/09').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(sandbox.SandboxError):
                capsule._transcripts(home,identity)

    def test_unknown_bridge_or_profile_never_spawns(self):
        with mock.patch.object(capsule.subprocess, 'Popen') as spawn:
            with self.assertRaises(sandbox.SandboxError):
                capsule.execute(object(), b'fixture', image=Path('/not-used'), files={}, parent=ROOT)
            for role, port in [('owner',1234),('build',True),('build',0),('build',65536)]:
                with self.assertRaises(sandbox.SandboxError):
                    capsule.profile(Path('/tmp/capsule'),role,port)
            spawn.assert_not_called()

    def test_path_injection_rejected(self):
        with self.assertRaises(sandbox.SandboxError):
            capsule.profile(Path('/tmp/"(allow default)'), 'build', 1234)

    def test_capsule_report_never_opens_unconfined_herdr(self):
        with mock.patch.dict(os.environ, {'FLEET_NATIVE_MEDIATION':'fleet.codex.capsule.v1'}):
            with self.assertRaises(effects.EffectMediationDenied):
                effects.require_native_mediation()


@unittest.skipUnless(IMAGE and sys.platform=='darwin', 'explicit installed CLI path and macOS required')
class NativeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ResponsesTests()
        self.fixture.max_input_bytes=256*1024
        self.fixture.call_timeout_ms=3000
        # Runtime copying/signature validation precedes the first request.
        self.fixture.peer_timeout=10
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.join_peers)
        self.root=self.fixture.tmp
        self.image=Path(IMAGE)
        self.results=[]

    def execute(self, *, source=None, role='build', **options):
        f=self.fixture
        if source is None:
            f.peer_once()
        else:
            call={'id':'ct_native','type':'custom_tool_call','call_id':'call_native',
                  'namespace':'functions','name':'exec','input':source}
            # Each response has its own peer thread and durable broker receipt.
            def callback(envelope):
                if len(f.calls)==1:
                    f.peer_once()
                    return f.response(envelope,[call])
                return f.response(envelope)
            f.peer_once(callback)
        result=f.bridge.execute_confined(b'Synthetic native capsule fixture.',image=self.image,
            files={'input.txt':b'original\n'},parent=self.root,role=role,**options)
        self.results.append(result)
        self.assertTrue(result.report['cleanup_confirmed'])
        self.assertTrue(result.report['quiescence_confirmed'])
        self.assertEqual(f.bridge.listener.fileno(),-1)
        self.assertNotIn(f.bridge.token, json.dumps(result.report))
        self.assertFalse(list(self.root.glob('codex-capsule-*')))
        return result

    def test_real_cli_completes_through_bound_bridge(self):
        r=self.execute()
        self.assertEqual((r.report['execution_status'],r.report['returncode']),('exited',0),r.stderr)
        self.assertIn(b'synthetic final',r.stdout)
        self.assertEqual(r.files,{'input.txt':b'original\n'})
        self.assertEqual(len(r.transcripts),1)
        rows=fleet_json.load_jsonl(next(iter(r.transcripts.values())))
        meta=next(x['payload'] for x in rows if x['type']=='session_meta')
        self.assertEqual(meta['cli_version'],capsule.CODEX_VERSION)
        self.assertEqual(meta['model_provider'],'fleet-local')
        self.assertEqual(len(self.fixture.calls),1)
        self.assertEqual(r.report['authority'],'none')
        self.assertEqual({Path(p['image']).name for p in r.report['processes_observed']},{'codex'})

    def test_native_tool_writes_only_candidate_and_real_host_is_reaped(self):
        patch='*** Begin Patch\n*** Add File: answer.txt\n+CONFINED_WRITE_OK\n*** End Patch'
        r=self.execute(source='text(await tools.apply_patch('+json.dumps(patch)+'));')
        self.assertEqual(r.report['returncode'],0,(r.report,r.stderr))
        self.assertEqual(r.files,{'input.txt':b'original\n','answer.txt':b'CONFINED_WRITE_OK\n'})
        self.assertEqual(len(self.fixture.calls),2)
        self.assertIn('codex-code-mode-host',{Path(p['image']).name for p in r.report['processes_observed']})

    def test_reader_cannot_write_candidate_even_with_inner_full_access(self):
        patch='*** Begin Patch\n*** Add File: forbidden.txt\n+NO\n*** End Patch'
        r=self.execute(source='text(await tools.apply_patch('+json.dumps(patch)+'));',role='verify')
        self.assertEqual(r.files,{'input.txt':b'original\n'})
        self.assertIn(b'failed',r.stdout)
        self.assertEqual(len(self.fixture.calls),2)

    def test_native_tool_cannot_write_outside_capsule(self):
        outside=self.root/'outside-marker'
        patch='*** Begin Patch\n*** Add File: '+str(outside)+'\n+NO\n*** End Patch'
        r=self.execute(source='text(await tools.apply_patch('+json.dumps(patch)+'));')
        self.assertFalse(outside.exists())
        self.assertEqual(r.files,{'input.txt':b'original\n'}, (r.report,r.stdout,r.stderr))
        self.assertIn(b'failed',r.stdout)

    def test_shell_is_not_an_allowed_executable(self):
        marker=self.root/'shell-marker'
        r=self.execute(source='text(await tools.exec_command({cmd:'+json.dumps('touch '+str(marker))+'}));')
        self.assertFalse(marker.exists())
        self.assertEqual(r.files,{'input.txt':b'original\n'})
        self.assertEqual(len(self.fixture.calls),2)
        self.assertIn(b'Operation not permitted',r.stderr)

    def test_timeout_interrupts_code_mode_host_and_exports_nothing(self):
        f=self.fixture
        call={'id':'ct_loop','type':'custom_tool_call','call_id':'call_loop',
              'namespace':'functions','name':'exec','input':'while (true) {}'}
        f.peer_once(lambda e:f.response(e,[call]))
        started=time.monotonic()
        r=f.bridge.execute_confined(b'Synthetic loop fixture.',image=self.image,files={},parent=self.root,timeout=3)
        self.assertEqual(r.report['execution_status'],'timed_out')
        self.assertLess(time.monotonic()-started,7)
        self.assertIn('codex-code-mode-host',{Path(p['image']).name for p in r.report['processes_observed']})
        self.assertEqual(r.files,{})
        self.assertTrue(r.report['cleanup_confirmed'])
        self.assertTrue(r.report['quiescence_confirmed'])

    def test_revoked_admission_stops_native_process(self):
        f=self.fixture
        def revoke(e):
            f.pause('cancel')
            return f.response(e)
        f.peer_once(revoke)
        r=f.bridge.execute_confined(b'Synthetic cancellation fixture.',image=self.image,files={},parent=self.root)
        self.assertIn(r.report['execution_status'],{'interrupted','protocol_rejected'})
        self.assertEqual(r.files,{})
        self.assertTrue(r.report['cleanup_confirmed'])

    def test_unconfirmed_quiescence_retains_resources_and_revokes_endpoint(self):
        # No workload is spawned: simulate an unavailable process inventory.
        with mock.patch.object(capsule, '_capture', return_value=(-9,'interrupted',None,b'',b'',[],False)), \
             mock.patch.object(capsule.Processes, 'stop', side_effect=OSError('fixture inventory unavailable')):
            with self.assertRaisesRegex(sandbox.SandboxError, 'quiescence unknown; capsule retained:'):
                self.fixture.bridge.execute_confined(b'Synthetic failure fixture.',
                    image=self.image,files={},parent=self.root)
        self.assertEqual(self.fixture.bridge.listener.fileno(),-1)
        self.assertEqual(len(list(self.root.glob('codex-capsule-*'))),1)


if __name__=='__main__':unittest.main()
