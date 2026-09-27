"""Supervisor authority/recovery, without Docker or model providers."""
import copy
import io
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import fleet_harness_campaign as campaign
import fleet_harness_contract as contract
import fleet_json


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="fleet-pilot-prep-");self.root=Path(self.temp.name)/"pilot"
        self.prepared=campaign.prepare(self.root);self.plan=campaign.read(self.root,"plan.json");self.pin=self.prepared["plan_sha256"]
    def tearDown(self):self.temp.cleanup()

    def test_expanded_budget_is_consistent_and_original_default_unchanged(self):
        target=self.root.parent/"expanded"
        prepared=campaign.prepare(target,requests_per_task=12)
        plan=campaign.validate(target,prepared["plan_sha256"])
        self.assertEqual([t["requests"] for t in plan["tasks"]],[12,12])
        self.assertEqual(plan["total_requests"],24)
        self.assertEqual(plan["estimated_cap_nano_usd"],2_400_000_000)
        self.assertEqual(self.plan["total_requests"],12)
        start=time.time();admitted={"plan_sha256":prepared["plan_sha256"],"started_at":start,"deadline_at":start+1200}
        for task in plan["tasks"]:
            spec=campaign.creation_for(target,plan,task,admitted,start)
            limits=spec["request_budget"]
            self.assertEqual(limits["max_requests"],12)
            self.assertEqual(limits["estimated_cap_nano_usd"],1_200_000_000)
            self.assertEqual(limits["token_cap"],2_000_000)
            self.assertEqual(campaign.verify_creation(target,plan,task,admitted,spec),spec)
            changed=copy.deepcopy(task);changed["requests"]=6
            with self.assertRaisesRegex(ValueError,"task differs"):
                campaign.creation_for(target,plan,changed,admitted,start)

    def test_thinking_profile_preserves_token_cap_and_requires_control(self):
        target=self.root.parent/"thinking"
        for count in (16,30,True,12.0):
            with self.assertRaises(ValueError):campaign.prepare(target,requests_per_task=count,profile="thinking-32k-v1")
            self.assertFalse(target.exists())
        prepared=campaign.prepare(target,requests_per_task=12,profile="thinking-32k-v1")
        plan=campaign.validate(target,prepared["plan_sha256"])
        self.assertEqual(plan["total_requests"],24);self.assertEqual(plan["estimated_cap_nano_usd"],4_800_000_000)
        limits=campaign.financial_limits(cycle_id=str(uuid.uuid4()),deadline_at=time.time()+60,requests=12,profile="thinking-32k-v1")
        self.assertEqual(limits["reserve_tokens_per_request"],131072)
        self.assertEqual(limits["reserve_nano_usd_per_request"],200_000_000)
        self.assertEqual(limits["token_cap"],2_000_000)
        for profile in ("legacy-8k",None,"unknown"):
            forged=copy.deepcopy(plan);forged["output_profile"]=profile
            with self.assertRaises(ValueError):campaign.validate_budget_plan(forged)
        with self.assertRaisesRegex(ValueError,"requires CONTROL"):
            campaign.creation_for(target,plan,plan["tasks"][0],{},time.time())

    def test_output_policy_rejects_inexact_types_without_normalization(self):
        for profile,cap in (("legacy-8k",8192),("thinking-32k-v1",32768)):
            policy=campaign.budget.contract(cycle_id=str(uuid.uuid4()),deadline_at=time.time()+60)["request_policy"]
            for value in (float(cap),True,str(cap),None):
                policy["max_output_tokens"]=value
                with self.subTest(profile=profile,value=value),self.assertRaises(ValueError):
                    campaign.financial_limits(cycle_id=str(uuid.uuid4()),deadline_at=time.time()+60,requests=3,request_policy=policy,profile=profile)
                self.assertEqual(policy["max_output_tokens"],value)

    def test_invalid_request_counts_and_partial_budget_changes_are_rejected(self):
        target=self.root.parent/"must-not-exist"
        for count in (True,False,12.0,"12",None,0,-1,31):
            with self.subTest(count=count),self.assertRaises(ValueError):campaign.prepare(target,requests_per_task=count)
            self.assertFalse(target.exists())
        for key,value in (("total_requests",12.0),("estimated_cap_nano_usd",1_200_000_000.0),("total_seconds",1200.0)):
            changed=copy.deepcopy(self.plan);changed[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):campaign.validate_budget_plan(changed)
        changed=copy.deepcopy(self.plan);changed["tasks"][0]["requests"]=12
        changed.update(total_requests=18,estimated_cap_nano_usd=1_800_000_000)
        with self.assertRaisesRegex(ValueError,"equally"):campaign.validate_budget_plan(changed)

    def test_creation_bound_to_plan_and_replay_without_candidate(self):
        start=time.time();admitted={"plan_sha256":self.pin,"started_at":start,"deadline_at":start+self.plan["total_seconds"]}
        task=self.plan["tasks"][0];spec=campaign.creation_for(self.root,self.plan,task,admitted,start)
        shutil.rmtree(task["candidate"])
        self.assertEqual(campaign.verify_creation(self.root,self.plan,task,admitted,spec),spec)
        changed=copy.deepcopy(spec);changed["request_budget"]["max_requests"]+=1
        contract.validate(changed)
        with self.assertRaises(ValueError):campaign.verify_creation(self.root,self.plan,task,admitted,changed)
        with self.assertRaises(ValueError):campaign.verify_creation(self.root,self.plan,self.plan["tasks"][1],admitted,spec)
        changed=copy.deepcopy(spec);sources=changed["prepared"]["sources"]
        sources["public_read"]["read_only"]={}
        sources["compiled_digest"]=contract._compiled(sources,campaign.checker.validate_suite(fleet_json.loads(sources["functional_tests"])))
        changed["prepared"]=contract._prepared(sources);contract.validate(changed)
        with self.assertRaisesRegex(ValueError,"public read authority"):
            campaign.verify_creation(self.root,self.plan,task,admitted,changed)

    def test_historical_contract_verifies_but_cannot_dispatch_or_mutate_backend(self):
        start=time.time();admitted={"plan_sha256":self.pin,"started_at":start,"deadline_at":start+self.plan["total_seconds"]}
        task=self.plan["tasks"][0];spec=campaign.creation_for(self.root,self.plan,task,admitted,start)
        sources=copy.deepcopy(spec["prepared"]["sources"]);del sources["public_read"]
        sources["permissions"]["mechanism"]="owned-executor-v1"
        sources["compiled_digest"]=contract._compiled(sources,fleet_json.loads(sources["functional_tests"]))
        legacy=contract.create(contract._prepared(sources),cycle_id=spec["cycle_id"],started_at=start,budget_limits=spec["request_budget"])
        contract.validate(legacy)
        owner=mock.MagicMock();owner.json.return_value=legacy
        with mock.patch.object(campaign.backend_module.budget,"Ledger") as ledger:
            with self.assertRaisesRegex(ValueError,"legacy harness is read-only"):
                campaign.backend_module.LocalHarnessBackend(owner,endpoint="http://127.0.0.1:1/chat/completions",dependencies=self.root/"dependencies")
            ledger.assert_not_called();self.assertNotIn("runs",{c[0] for c in owner.mock_calls})
        # Merely deleting the manifest from a new pinned packet fails validation.
        del spec["prepared"]["sources"]["public_read"]
        with self.assertRaises(ValueError):contract.validate(spec)

    def test_completed_inventory_and_live_gates_reject_before_resources(self):
        campaign.sandbox.publish(self.root,"completed.json",{"status":"synthetic_complete","results":[]})
        with self.assertRaises(ValueError):campaign.supervise(self.root,self.pin)
        with self.assertRaisesRegex(ValueError,"live lane closed"):campaign.supervise(self.root,self.pin,provider="live")

    def test_two_processes_lease_and_owner_loss(self):
        script="import sys;sys.path.insert(0,sys.argv[1]);import fleet_harness_campaign as c\nwith c.lease(sys.argv[2]) as locked:print(locked)"
        with campaign.lease(self.root) as locked:
            self.assertTrue(locked)
            child=subprocess.run([sys.executable,"-I","-B","-c",script,str(Path(campaign.__file__).parent),str(self.root)],capture_output=True,timeout=10)
            self.assertEqual(child.stdout.strip(),b"False")
        with campaign.lease(self.root) as locked:self.assertTrue(locked)

    def test_real_process_loss_recovers_cancel_without_new_identity(self):
        script='''import os,sys
sys.path.insert(0,sys.argv[1])
import fleet_harness_campaign as c,fleet_safe_paths as s
original=c.sandbox.publish
def publish(root,name,value):
    if name=='cancel.json':s._atomic_write_checkpoint=lambda p:os._exit(91) if p=='after_atomic_pending_fsync' else None
    return original(root,name,value)
c.sandbox.publish=publish
c.cancel(sys.argv[2],sys.argv[3],'original reason')
'''
        child=subprocess.run([sys.executable,"-I","-B","-c",script,str(Path(campaign.__file__).parent),str(self.root),self.pin],capture_output=True,timeout=10)
        self.assertEqual(child.returncode,91,child.stderr)
        campaign.cancel(self.root,self.pin,"different reason")
        self.assertEqual(campaign.read(self.root,"cancel.json")["reason"],"original reason")
        raw=(self.root/"cancel.json").read_bytes();campaign.cancel(self.root,self.pin,"again")
        self.assertEqual((self.root/"cancel.json").read_bytes(),raw);self.assertFalse(list(self.root.glob(".fleet-atomic-*.tmp")))

    def test_signal_to_duplicate_start_cannot_cancel_the_lease_owner(self):
        script='''import os,sys,time,threading,signal
sys.path.insert(0,sys.argv[1])
import fleet_harness_campaign as c
original=c.validate
def validate(*args):
    time.sleep(.25);return original(*args)
c.validate=validate
threading.Timer(.075,lambda:os.kill(os.getpid(),signal.SIGTERM)).start()
sys.argv=['supervisor','supervise',sys.argv[2],'--plan-sha256',sys.argv[3]]
c.main()
'''
        with campaign.lease(self.root):
            child=subprocess.run([sys.executable,"-I","-B","-c",script,str(Path(campaign.__file__).parent),str(self.root),self.pin],capture_output=True,timeout=10)
        self.assertEqual(child.returncode,0,child.stderr)
        self.assertEqual(fleet_json.loads(child.stdout)["status"],"supervisor_busy")
        self.assertFalse((self.root/"cancel.json").exists());self.assertFalse((self.root/"admitted.json").exists())

    def test_signal_cancel_error_forbids_completed_publication(self):
        requested=threading.Event()
        provider=mock.MagicMock();provider.__enter__.return_value="http://127.0.0.1:1/chat/completions"
        provider.__exit__.side_effect=lambda *_:requested.set()
        owner=mock.MagicMock();owner.tick.return_value={"status":"accepted_contract"};owner.pin="f"*64
        adapter=mock.MagicMock();adapter.ledger.summary.return_value={}
        with mock.patch.object(campaign,"SyntheticProvider",return_value=provider),mock.patch.object(campaign.cycle.Cycle,"create",return_value=owner),mock.patch.object(campaign.backend_module,"LocalHarnessBackend",return_value=adapter),mock.patch.object(campaign,"cancel",side_effect=OSError("cancel publication failed")),mock.patch.object(campaign,"verify_completed") as verify:
            with self.assertRaisesRegex(RuntimeError,"completion forbidden"):
                campaign.supervise(self.root,self.pin,signal_requested=requested)
        verify.assert_not_called();self.assertFalse((self.root/"completed.json").exists())
        errors=[fleet_json.loads(p.read_bytes()) for p in (self.root/"supervisors").glob("*.json")]
        self.assertTrue(any(e.get("kind")=="cancellation_failed" and e["error_type"]=="OSError" for e in errors))
        with campaign.lease(self.root) as locked:self.assertTrue(locked)

    def test_cancel_drain_retains_process_lease_even_if_error_record_fails(self):
        script='''import sys,time,threading
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import fleet_harness_campaign as c
root=Path(sys.argv[2]);requested=threading.Event()
def cancel(*args,**kwargs):
    (root/'watcher-started').touch()
    while not (root/'release-watcher').exists():time.sleep(.01)
def record(*args):
    (root/'drain-pending').touch()
    raise OSError('evidence unavailable')
c.cancel=cancel;c.SignalCancellation.DRAIN_SECONDS=.05;c.SignalCancellation.record_error=record
try:
    with c.lease(root) as locked:
        assert locked
        with c.signal_cancellation(root,sys.argv[3],requested) as monitor:
            requested.set()
            while not (root/'watcher-started').exists():time.sleep(.005)
            monitor.drain()
except RuntimeError:print('error retained after actual drain')
else:raise AssertionError('unsafe close')
'''
        child=subprocess.Popen([sys.executable,"-I","-B","-c",script,str(Path(campaign.__file__).parent),str(self.root),self.pin],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            until=time.monotonic()+5
            while not (self.root/"drain-pending").exists() and time.monotonic()<until:time.sleep(.01)
            self.assertTrue((self.root/"drain-pending").exists())
            self.assertIsNone(child.poll())
            with campaign.lease(self.root) as locked:self.assertFalse(locked)
            self.assertFalse((self.root/"completed.json").exists())
            (self.root/"release-watcher").touch()
            stdout,stderr=child.communicate(timeout=5)
            self.assertEqual(child.returncode,0,stderr);self.assertIn(b"after actual drain",stdout)
        finally:
            if child.poll() is None:child.kill();child.wait()
        with campaign.lease(self.root) as locked:self.assertTrue(locked)

    def test_cancel_failure_receipt_is_recovery_gate_before_admission(self):
        monitor=campaign.SignalCancellation(self.root,self.pin,None)
        monitor.record_error("cancellation_failed",OSError("intent could not be published"))
        with mock.patch.object(campaign,"cancel",side_effect=OSError("still unavailable")) as cancel:
            with self.assertRaises(OSError):campaign.supervise(self.root,self.pin)
        self.assertEqual(cancel.call_args.kwargs["request_id"],monitor.request["id"])
        self.assertFalse((self.root/"admitted.json").exists())
        self.assertFalse((self.root/"completed.json").exists())
        result=campaign.supervise(self.root,self.pin)
        self.assertEqual(result["status"],"cancelled")
        self.assertEqual(campaign.read(self.root,"cancel.json")["id"],monitor.request["id"])
        self.assertFalse(any((self.root/t["id"]/"creation.json").exists() for t in self.plan["tasks"]))

    def test_signal_between_drain_and_terminal_decision_is_retained(self):
        requested=threading.Event();original=campaign.SignalCancellation.drain
        def drain(monitor):
            result=original(monitor);requested.set();return result
        provider=mock.MagicMock();provider.__enter__.return_value="http://127.0.0.1:1/chat/completions"
        owner=mock.MagicMock();owner.tick.return_value={"status":"accepted_contract"};owner.pin="f"*64
        adapter=mock.MagicMock();adapter.ledger.summary.return_value={}
        with mock.patch.object(campaign,"SyntheticProvider",return_value=provider),mock.patch.object(campaign.cycle.Cycle,"create",return_value=owner),mock.patch.object(campaign.backend_module,"LocalHarnessBackend",return_value=adapter),mock.patch.object(campaign.SignalCancellation,"drain",drain),mock.patch.object(campaign,"verify_completed") as verify:
            result=campaign.supervise(self.root,self.pin,signal_requested=requested)
        self.assertEqual(result["status"],"cancelled");verify.assert_not_called()
        self.assertFalse((self.root/"completed.json").exists())
        self.assertEqual(campaign.read(self.root,"signal-cancellation.json")["id"],campaign.read(self.root,"cancel.json")["id"])

    def test_synthetic_scope_cannot_read_control_files_and_failed_enter_closes_socket(self):
        servers=[]
        class Server:
            def __init__(self,address,handler):self.handler=handler;self.server_port=49123;self.closed=False;servers.append(self)
            def server_close(self):self.closed=True
            def serve_forever(self):pass
            def shutdown(self):pass
        with mock.patch.object(campaign,"ThreadingHTTPServer",Server),mock.patch.object(campaign,"publish_run",side_effect=OSError("publication failed")):
            with self.assertRaises(OSError):campaign.SyntheticProvider(self.root,self.plan).__enter__()
        self.assertTrue(servers[-1].closed)
        with mock.patch.object(campaign,"ThreadingHTTPServer",Server):
            with campaign.SyntheticProvider(self.root,self.plan):
                handler=servers[-1].handler.__new__(servers[-1].handler)
                task={"requirements":{"functional":{"task":"D2"}},"scope":{"editable_paths":[str(self.root/"plan.json")]}}
                body=fleet_json.canonical_bytes({"messages":[{}, {"content":fleet_json.canonical_bytes(task).decode()}]})
                handler.headers={"Content-Length":str(len(body))};handler.rfile=io.BytesIO(body);handler.wfile=io.BytesIO();errors=[]
                handler.send_error=lambda code,message:errors.append(code)
                handler.do_POST();self.assertEqual(errors,[400]);self.assertEqual(handler.wfile.getvalue(),b"")
        self.assertFalse((self.root/"provider-requests").exists())


if __name__=="__main__":unittest.main()
