"""CONTROL supervisor authority and cleanup, using owned provider-free fixtures.

Herdr originals are labelled fixture observations. Fake accepted owners only
exercise supervisor closure ordering; Docker integration proves acceptance.
"""
import copy
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import fleet_harness_live_campaign as campaign
import fleet_harness_control as control
import fleet_json


def fixture_bind(guard):
    launch=guard.plan["launch"]
    bound={"generation_sha256":control.pin(guard.generation),"pane_id":"fixture:p1","workspace_id":"fixture","terminal_id":"fixture-terminal",
        "launch":launch,"originals":{
            "pane":{"result":{"pane":{"pane_id":"fixture:p1","workspace_id":"fixture","terminal_id":"fixture-terminal","cwd":launch["cwd"]}}},
            "process":{"result":{"process_info":{"pane_id":"fixture:p1","foreground_processes":[{"pid":guard.pid,"argv":launch["command_resolved"],"cwd":launch["cwd"]}]}}},
            "plugin":{"result":{"plugins":[{"plugin_id":launch["plugin_id"],"enabled":True,"manifest_path":launch["manifest"],"panes":[{"id":launch["entrypoint"],"command":launch["command"]}]}]}}}}
    control.verify_launch(bound,guard.generation,guard.plan)
    control.publish(guard.root,guard.plan,"control-launches/"+guard.generation["id"]+".json",bound)
    guard._bound=bound
    return bound


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="fleet-control-pilot-tests-")
        self.root=Path(self.temp.name).resolve()/"pilot"
        self.prepared=campaign.prepare(self.root,mode="synthetic_tls")
        self.pin=self.prepared["plan_sha256"]
        self.plan,self.base,self.authority=campaign.validate(self.root,self.pin)
        self.endpoint={"version":"synthetic-loopback-tls-v1","host":"127.0.0.1","port":49123,"certificate_pem":(self.root/"synthetic-cert.pem").read_text()}
    def tearDown(self):self.temp.cleanup()

    def child(self):
        campaign.publish(self.root,self.authority,"synthetic-tls-endpoint.json",self.endpoint)
        with control.OwnedLease(self.root,control.pin(self.authority)) as guard:
            spec=campaign.creation(self.root,self.plan,self.base,self.base["tasks"][0],guard,{"mode":"synthetic_tls","paid_authority":False},self.endpoint)
        return spec

    def test_preparation_does_not_admit_provider_or_authority(self):
        self.assertFalse((self.root/"control-clock.json").exists())
        self.assertFalse((self.root/"operator-approval.json").exists())
        self.assertFalse((self.root/"provider-requests").exists())
        self.assertEqual(campaign.read(self.root,"approval-template.json")["decision"],"PENDING")
        with self.assertRaises(ValueError):campaign.authorize(self.root,self.pin,human_reference="cannot approve synthetic fixtures")

    def test_thinking_profile_binds_budget_wire_and_approval(self):
        root=self.root.parent/"thinking"
        prepared=campaign.prepare(root,mode="synthetic_tls",requests_per_task=12,output_profile="thinking-32k-v1")
        plan,base,authority=campaign.validate(root,prepared["plan_sha256"])
        self.assertEqual(plan["total_requests"],24);self.assertEqual(plan["estimated_cap_nano_usd"],4_800_000_000)
        self.assertEqual(prepared["reserved_estimated_usd"],4.8)
        template=campaign.read(root,"approval-template.json")
        for limits in template["financial_limits"]:
            self.assertEqual(limits["request_policy"]["max_output_tokens"],32768)
            self.assertEqual(limits["reserve_tokens_per_request"],131072)
            self.assertEqual(limits["estimated_cap_nano_usd"],2_400_000_000)
            self.assertEqual(limits["token_cap"],2_000_000)
        endpoint={**self.endpoint,"certificate_pem":(root/"synthetic-cert.pem").read_text()}
        campaign.publish(root,authority,"synthetic-tls-endpoint.json",endpoint)
        with control.OwnedLease(root,control.pin(authority)) as guard:
            spec=campaign.creation(root,plan,base,base["tasks"][0],guard,{"mode":"synthetic_tls","paid_authority":False},endpoint)
            self.assertEqual(spec["request_budget"]["wire_version"],campaign.fixtures.wire.THINKING_32K_VERSION)
            self.assertEqual(campaign.verify_child(root,plan,base,base["tasks"][0],spec,authority),spec)
        forged=copy.deepcopy(spec);forged["request_budget"]["wire_version"]=campaign.fixtures.wire.INDEXED_VERSION
        with self.assertRaisesRegex(ValueError,"wire differs"):
            campaign.verify_child(root,plan,base,base["tasks"][0],forged,authority)
        old=campaign.read(self.root,"approval-template.json")
        old.update(decision="approved",human_authorization_reference="fixture",approved_at=time.time(),start_before=time.time()+60)
        with mock.patch.object(campaign,"read",return_value=old),mock.patch.object(campaign,"publish") as publish:
            with self.assertRaisesRegex(ValueError,"another scope"):
                campaign._authorize_under_barrier(root,prepared["plan_sha256"],plan,base,authority,"fixture")
            publish.assert_not_called()

    def test_expanded_campaign_binds_both_children_and_rejects_old_approval(self):
        target=self.root.parent/"expanded-live"
        pricing=self.root.parent/"pricing.json";pricing.write_text('{"source":"synthetic pricing fixture"}')
        prepared=campaign.prepare(target,mode="live",pricing_evidence=pricing,requests_per_task=12)
        plan,base,authority=campaign.validate(target,prepared["plan_sha256"])
        template=campaign.read(target,"approval-template.json")
        self.assertEqual(prepared["requests"],24);self.assertEqual(prepared["reserved_estimated_usd"],2.4)
        self.assertEqual([row["max_requests"] for row in template["financial_limits"]],[12,12])
        self.assertEqual([row["estimated_cap_nano_usd"] for row in template["financial_limits"]],[1_200_000_000]*2)
        with self.assertRaisesRegex(ValueError,"authorization has not been admitted"):
            campaign.supervise(target,prepared["plan_sha256"])
        self.assertFalse((target/"control-clock.json").exists())
        old=campaign.read(self.root,"approval-template.json")
        old.update(decision="approved",human_authorization_reference="fixture",approved_at=time.time(),start_before=time.time()+60)
        # Exercise trusted ingress using fixture data only. No live approval is
        # ever written, and rejected reuse must not publish even an anchor.
        with mock.patch.object(campaign,"read",return_value=old),mock.patch.object(campaign,"publish") as publish:
            with self.assertRaisesRegex(ValueError,"another scope"):
                campaign._authorize_under_barrier(target,prepared["plan_sha256"],plan,base,authority,"fixture")
            publish.assert_not_called()
        changed=copy.deepcopy(template);changed["total_requests"]=24.0
        with mock.patch.object(campaign,"read",side_effect=[None,changed]),mock.patch.object(campaign,"publish") as publish:
            with self.assertRaisesRegex(ValueError,"scope changed"):
                campaign._authorize_under_barrier(target,prepared["plan_sha256"],plan,base,authority,"fixture")
            publish.assert_not_called()
        approved={**template,"decision":"approved","human_authorization_reference":"fixture",
                  "approved_at":time.time(),"start_before":time.time()+60}
        with mock.patch.object(campaign,"read",return_value=approved),mock.patch.object(campaign,"publish") as publish:
            anchor=campaign._authorize_under_barrier(target,prepared["plan_sha256"],plan,base,authority,"fixture")
            self.assertEqual(anchor["approval_sha256"],control.pin(approved));publish.assert_called_once()

    def test_creation_binds_all_task_and_clock_contracts(self):
        spec=self.child();task=self.base["tasks"][0]
        self.assertEqual(campaign.verify_child(self.root,self.plan,self.base,task,spec,self.authority),spec)
        # Build internally valid contracts for different acceptance/scope; the
        # campaign must still reject those contracts against its frozen task.
        for field in ("acceptance","scope"):
            changed=copy.deepcopy(spec);sources=changed["prepared"]["sources"]
            if field=="acceptance":sources[field]["requirements"][0]["checks"][0]["expected"]="pass"
            else:sources[field]["max_bytes"]+=1
            local_digest=campaign.contract.local._compiled(sources,fleet_json.loads(sources["functional_tests"]))
            sources["compiled_digest"]=control.pin({"profile":campaign.contract.VERSION,"local_contract":local_digest})
            changed["prepared"]=campaign.contract._prepared(sources)
            campaign.contract.validate(changed)
            with self.assertRaisesRegex(ValueError,"differs from prepared"):campaign.verify_child(self.root,self.plan,self.base,task,changed,self.authority)
        changed=copy.deepcopy(spec);changed["request_budget"]["owner_store"]["runs"]=str(self.root/"another-runs")
        campaign.contract.validate(changed)
        with self.assertRaises(ValueError):campaign.verify_child(self.root,self.plan,self.base,task,changed,self.authority)
        changed=copy.deepcopy(spec);clock=changed["request_budget"]["control_clock"]
        for field in ("started_at","deadline_at","monotonic_started","monotonic_deadline"):clock[field]-=1
        campaign.contract.validate(changed)
        with self.assertRaises(ValueError):campaign.verify_child(self.root,self.plan,self.base,task,changed,self.authority)

    def test_foreign_task_rejected_before_candidate_preparation(self):
        changed=copy.deepcopy(self.base["tasks"][0]);changed["requests"]=12
        with mock.patch.object(campaign.contract,"prepare") as prepare:
            with self.assertRaisesRegex(ValueError,"task differs"):
                campaign.creation(self.root,self.plan,self.base,changed,None,{},self.endpoint)
            prepare.assert_not_called()
        self.assertFalse((self.root/"D1/runs").exists())

    def test_tls_enter_failure_after_bind_closes_exact_socket(self):
        servers=[]
        class Server:
            def __init__(self,*args):self.server_port=49124;self.closed=False;servers.append(self)
            def server_close(self):self.closed=True
        with mock.patch.object(campaign,"ThreadingHTTPServer",Server),mock.patch.object(Path,"read_text",side_effect=OSError("certificate disappeared")):
            with self.assertRaises(OSError):campaign.SyntheticTLSProvider(self.root,self.base,self.authority,mock.Mock()).__enter__()
        self.assertEqual(len(servers),1);self.assertTrue(servers[0].closed)

    def fake_owners(self,*args):
        owner=mock.Mock();owner.pin=control.pin(args[1]);owner.load.return_value={"terminal":False}
        owner.tick.return_value={"status":"accepted_contract"}
        return owner

    def test_success_publication_follows_all_service_cleanup_and_release(self):
        real_publish=campaign.publish;stages=[]
        def publish(root,authority,name,value):
            if name=="control-completed.json":
                services=campaign.read(root,"control-services/"+value["generation"]+".json")
                self.assertTrue(services["provider"]["socket_closed"])
                self.assertTrue(services["signal_monitor_joined"])
                stages.append("complete")
            return real_publish(root,authority,name,value)
        def verify(root,pin):
            result=campaign.read(root,"control-completed.json")
            release=campaign.read(root,"control-releases/"+result["generation"]+".json")
            self.assertTrue(release["transports_drained"]);stages.append("release_verified");return result
        with mock.patch.object(control.OwnedLease,"bind_herdr",fixture_bind),mock.patch.object(campaign.cycle.Cycle,"create",side_effect=self.fake_owners),mock.patch.object(campaign.backend_module,"ControlHarnessBackend"),mock.patch.object(campaign,"publish",side_effect=publish),mock.patch.object(campaign,"verify_completed",side_effect=verify):
            result=campaign.supervise(self.root,self.pin)
        self.assertEqual(result["status"],"synthetic_complete")
        self.assertEqual(stages,["complete","release_verified"])

    def test_provider_cleanup_failure_forbids_completed(self):
        original=campaign.SyntheticTLSProvider.__exit__
        def cleanup(provider,*args):
            original(provider,*args)
            raise OSError("cleanup receipt unavailable")
        with mock.patch.object(control.OwnedLease,"bind_herdr",fixture_bind),mock.patch.object(campaign.cycle.Cycle,"create",side_effect=self.fake_owners),mock.patch.object(campaign.backend_module,"ControlHarnessBackend"),mock.patch.object(campaign.SyntheticTLSProvider,"__exit__",cleanup):
            with self.assertRaises(OSError):campaign.supervise(self.root,self.pin)
        self.assertFalse((self.root/"control-completed.json").exists())

    def test_cancel_after_tls_drain_vetoes_completed(self):
        requested=threading.Event();original=campaign.SyntheticTLSProvider.__exit__
        def cleanup(provider,*args):original(provider,*args);requested.set()
        with mock.patch.object(control.OwnedLease,"bind_herdr",fixture_bind),mock.patch.object(campaign.cycle.Cycle,"create",side_effect=self.fake_owners),mock.patch.object(campaign.backend_module,"ControlHarnessBackend"),mock.patch.object(campaign.SyntheticTLSProvider,"__exit__",cleanup):
            result=campaign.supervise(self.root,self.pin,signal_requested=requested)
        self.assertEqual(result["status"],"cancelled")
        self.assertFalse((self.root/"control-completed.json").exists())
        self.assertIsNotNone(campaign.read(self.root,"control-cancel.json"))

    def test_monitor_error_forbids_completion(self):
        requested=threading.Event();original=campaign.SyntheticTLSProvider.__exit__
        def cleanup(provider,*args):original(provider,*args);requested.set()
        with mock.patch.object(control.OwnedLease,"bind_herdr",fixture_bind),mock.patch.object(campaign.cycle.Cycle,"create",side_effect=self.fake_owners),mock.patch.object(campaign.backend_module,"ControlHarnessBackend"),mock.patch.object(campaign.SyntheticTLSProvider,"__exit__",cleanup),mock.patch.object(campaign,"cancel_campaign",side_effect=OSError("cancel unavailable")):
            with self.assertRaises(RuntimeError):campaign.supervise(self.root,self.pin,signal_requested=requested)
        self.assertFalse((self.root/"control-completed.json").exists())
        self.assertIsNotNone(campaign.read(self.root,"signal-monitor-failure.json"))

    def test_pure_completion_rejects_wrong_status_and_inventory(self):
        campaign.publish(self.root,self.authority,"control-completed.json",{"status":"live_pilot_complete","results":[]})
        with self.assertRaises(ValueError):campaign.verify_completed(self.root,self.pin)

    def test_cancel_recovers_same_cas_identity_after_real_process_loss(self):
        code='''import os,sys
sys.path.insert(0,sys.argv[1])
import fleet_harness_live_campaign as c
original=c.control.sandbox.publish
def crash(root,name,value):
    if name=="control-cancel.json":os._exit(91)
    return original(root,name,value)
c.control.sandbox.publish=crash
c.cancel_campaign(sys.argv[2],sys.argv[3],"original-cancel")
'''
        child=subprocess.run([sys.executable,"-I","-B","-c",code,str(Path(campaign.__file__).parent),str(self.root),self.pin],capture_output=True,timeout=10)
        self.assertEqual(child.returncode,91,child.stderr)
        result=campaign.cancel_campaign(self.root,self.pin,"later reason")
        self.assertEqual(result["reason"],"original-cancel")
        self.assertEqual(campaign.cancel_campaign(self.root,self.pin,"duplicate"),result)
        self.assertFalse((self.root/"control-clock.json").exists())

    def test_cancelled_recovery_needs_neither_provider_nor_credentials(self):
        campaign.cancel_campaign(self.root,self.pin,"cancel before admission")
        with mock.patch.dict(os.environ,{"DEEPSEEK_API_KEY":""}),mock.patch.object(campaign,"SyntheticTLSProvider") as provider,mock.patch.object(control.OwnedLease,"bind_herdr") as bind:
            result=campaign.supervise(self.root,self.pin)
        self.assertEqual(result["status"],"cancelled")
        self.assertTrue(result["observation_complete"])
        provider.assert_not_called();bind.assert_not_called()
        self.assertFalse(any((self.root/t["id"]/"control-creation.json").exists() for t in self.base["tasks"]))

    def test_one_unreadable_creation_does_not_suppress_independent_cleanup(self):
        campaign.cancel_campaign(self.root,self.pin,"fixture")
        (self.root/"D1/control-creation.json").write_bytes(b'{"truncated":')
        resource=self.root/"D2/runs/unregistered/resource.json";resource.parent.mkdir(parents=True)
        resource.write_text('{}')
        with mock.patch.object(campaign.sandbox,"Sandbox") as effect:
            result=campaign.reconcile_resources(self.root,self.pin)
        self.assertEqual(result["status"],"dependency_pending")
        self.assertEqual({e["task"] for e in result["errors"]},{"D1","D2"})
        self.assertFalse(result["observation_complete"]);effect.assert_not_called()

    def test_unknown_resource_cannot_derive_cleanup_authority_from_its_own_owner(self):
        spec=self.child();owner=mock.Mock();backend=self.root/"D1/runs/fixture/backend"
        resource=backend/"attempts/unregistered/controls/0001/resource/resource.json"
        resource.parent.mkdir(parents=True,mode=0o700)
        for parent in resource.parents:
            if parent==self.root:break
            parent.chmod(0o700)
        campaign.sandbox.publish(resource.parent,"resource.json",{"owner":"foreign-identity"})
        with self.assertRaisesRegex(ValueError,"registered dispatch"):
            campaign.owned_resource_record(self.root,owner,spec,backend,resource,{"attempts":[]})

    def test_private_credential_reference_rejects_links_modes_and_header_text(self):
        reference="file:"+str(self.root/"provider-key")
        self.assertIsNone(campaign.load_credential(self.root,reference))
        path=self.root/"provider-key";path.write_text("synthetic-test-token\n");path.chmod(0o600)
        self.assertEqual(campaign.load_credential(self.root,reference),"synthetic-test-token")
        path.chmod(0o644)
        with self.assertRaises(campaign.safe.SafePathError):campaign.load_credential(self.root,reference)
        path.chmod(0o600);link=self.root/"hardlink";os.link(path,link)
        with self.assertRaises(campaign.safe.SafePathError):campaign.load_credential(self.root,reference)
        link.unlink();path.rename(link);path.symlink_to(link)
        with self.assertRaises(campaign.safe.SafePathError):campaign.load_credential(self.root,reference)
        path.unlink();link.rename(path);path.write_text("synthetic\nInjected: header")
        with self.assertRaises(ValueError):campaign.load_credential(self.root,reference)
        with self.assertRaises(ValueError):campaign.load_credential(self.root,"file:/tmp/foreign-provider-key")

    def test_registered_mini_cleanup_reads_actual_private_backend_directory_modes(self):
        spec=self.child();backend=self.root/"D1/runs/backend";task={"version":"trusted fixture"}
        admission={"generation":"5685f03f-6777-4d68-b3bf-08c7a7138fcb","prompt_sha256":control.pin(task)}
        store=backend/"attempts"/control.pin(admission)/"controls/0001";store.mkdir(parents=True,mode=0o700)
        campaign.sandbox.publish(store,"task.json",task)
        resource=store/"resource";resource.mkdir(mode=0o700)
        record={"owner":admission["generation"],"candidate":str(store/"empty"),"guest":str(store/"bridge"),"scratch":str(resource/"work"),"image":campaign.sandbox.IMAGE}
        campaign.sandbox.publish(resource,"resource.json",record)
        self.assertEqual((backend/"attempts").stat().st_mode & 0o777,0o755)
        current={"attempts":[{"admission":admission,"intent":True}]}
        self.assertEqual(campaign.owned_resource_record(self.root,mock.Mock(),spec,backend,resource/"resource.json",current),record)
        (store/"task.json").rename(store/"real-task.json");(store/"task.json").symlink_to(store/"real-task.json")
        with self.assertRaises(campaign.safe.SafePathError):campaign.owned_resource_record(self.root,mock.Mock(),spec,backend,resource/"resource.json",current)

    def test_synthetic_supervisor_never_resolves_a_credential_reference(self):
        campaign.cancel_campaign(self.root,self.pin,"no workload fixture")
        with mock.patch.object(campaign,"load_credential",side_effect=AssertionError("credential read")):
            self.assertEqual(campaign.supervise(self.root,self.pin)["status"],"cancelled")

    def test_live_file_preparation_records_only_a_reference_and_no_approval(self):
        observation=self.root/"fixture-pricing.json";observation.write_text('{"source":"unit fixture only"}')
        target=self.root.parent/"live-reference-fixture"
        result=campaign.prepare(target,mode="live",pricing_evidence=observation,credential_source="file")
        plan,_,_=campaign.validate(target,result["plan_sha256"])
        self.assertEqual(plan["credential_reference"],"file:"+str(target/"provider-key"))
        self.assertFalse((target/"provider-key").exists())
        self.assertFalse((target/"operator-approval.json").exists())
        with self.assertRaisesRegex(ValueError,"authorization has not been admitted"):
            campaign.supervise(target,result["plan_sha256"])
        self.assertFalse((target/"control-clock.json").exists())


@unittest.skipUnless(os.environ.get("FLEET_HARNESS_LOCAL_TESTS")=="1","explicit owned local Docker lane")
class CampaignIntegrationTests(unittest.TestCase):
    def test_thinking_profile_completes_both_synthetic_tasks(self):
        with tempfile.TemporaryDirectory(prefix="fleet-thinking-profile-") as tmp:
            root=Path(tmp).resolve()/"campaign"
            prepared=campaign.prepare(root,mode="synthetic_tls",requests_per_task=12,output_profile="thinking-32k-v1")
            with mock.patch.object(control.OwnedLease,"bind_herdr",fixture_bind):
                result=campaign.supervise(root,prepared["plan_sha256"])
            self.assertEqual(result["status"],"synthetic_complete",result)
            self.assertEqual([t["result"]["status"] for t in result["results"]],["accepted_contract"]*2)
            self.assertEqual(campaign.verify_completed(root,prepared["plan_sha256"]),result)

    def test_source_review_resumes_exact_frozen_check_and_original_campaign_clock(self):
        with tempfile.TemporaryDirectory(prefix="fleet-control-review-resume-") as tmp:
            root=Path(tmp).resolve()/"campaign";prepared=campaign.prepare(root,mode="synthetic_tls");pin=prepared["plan_sha256"]
            base=campaign.read(root,"plan.json");original_init=campaign.backend_module.ControlHarnessBackend.__init__
            def no_review(backend,*args,**kwargs):
                original_init(backend,*args,**kwargs);backend.source_admissions=[]
            with mock.patch.object(control.OwnedLease,"bind_herdr",fixture_bind),mock.patch.object(campaign.backend_module.ControlHarnessBackend,"__init__",no_review):
                result=campaign.supervise(root,pin)
            self.assertEqual(result["status"],"dependency_pending",result)
            self.assertFalse((root/"D2/control-creation.json").exists())
            original_clock=(root/"control-clock.json").read_bytes()
            spec=campaign.read(root,"D1/control-creation.json")
            owner=campaign.cycle.Cycle(root/"D1/runs",spec["cycle_id"],contract_sha256=control.pin(spec))
            current=owner.load();binding=current["attempts"][-1]["functional"]["binding"]
            check=root/"D1/runs"/owner.prefix/"local-backend/checks"/binding/"check"
            raw=(check/"result.json").read_bytes();self.assertEqual(fleet_json.loads(raw)["pending"],["independent_source_admission_required"])
            with self.assertRaises(ValueError):campaign.source_review(root,pin,task_id="D1",binding_sha256="0"*64,review=base["source_admissions"]["D1"])
            review=campaign.source_review(root,pin,task_id="D1",binding_sha256=binding,review=base["source_admissions"]["D1"])
            self.assertEqual(review["status"],"source_review_retained")
            self.assertEqual((check/"result.json").read_bytes(),raw)
            with mock.patch.object(control.OwnedLease,"bind_herdr",fixture_bind):result=campaign.supervise(root,pin)
            self.assertEqual(result["status"],"synthetic_complete",result)
            self.assertEqual((root/"control-clock.json").read_bytes(),original_clock)
            self.assertEqual(len(list((root/"provider-requests").glob("*.json"))),4)
            self.assertEqual(owner.load()["attempts"][-1]["functional"]["binding"],binding)
            self.assertEqual(campaign.verify_completed(root,pin),result)


if __name__=="__main__":unittest.main()
