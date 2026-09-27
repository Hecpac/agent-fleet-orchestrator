"""v4 integration: real Mini/Docker/owner cycle and local TLS, fixture Herdr CLI."""
import os
from pathlib import Path
import ssl
import subprocess
import sys
import threading
import time
import unittest
import uuid
import copy

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import fleet_harness_control as control
import fleet_harness_live_budget as budget
import fleet_harness_live_contract as contract
import fleet_harness_live_backend as backend_module
import fleet_harness_provider_protocol as wire
import tests.test_fleet_harness_cycle as local_tests


@unittest.skipUnless(os.environ.get("FLEET_HARNESS_LOCAL_TESTS")=="1","explicit owned local Docker lane")
class ControlCycleTests(unittest.TestCase):
    def setUp(self):
        local_tests.HarnessCycleTests.setUp(self)
        self.root=self.root.resolve();self.root.chmod(0o700);self.candidate=self.candidate.resolve()
        self.server.shutdown();self.thread.join(2)
        cert=self.root/"cert.pem";key=self.root/"key.pem"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-keyout",str(key),"-out",str(cert),"-subj","/CN=api.deepseek.com","-addext","subjectAltName=DNS:api.deepseek.com"],capture_output=True,check=True,timeout=10)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
        self.server.socket=context.wrap_socket(self.server.socket,server_side=True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.synthetic_endpoint={"version":"synthetic-loopback-tls-v1","host":"127.0.0.1","port":self.server.server_port,"certificate_pem":cert.read_text()}
        launch={"herdr":"/bin/false","manifest":str(self.root/"fixture.toml"),"manifest_sha256":"a"*64,
            "plugin_id":"fixture.control","entrypoint":"supervise","cwd":str(self.root),"command":[sys.executable,"-B","fixture.py"]}
        self.control_plan=control.prepare(self.root,campaign_sha256="a"*64,seconds=900,launch=launch)
        self.guard=control.OwnedLease(self.root,control.pin(self.control_plan)).__enter__()
        launch=self.control_plan["launch"]
        self.guard._bound={"generation_sha256":control.pin(self.guard.generation),"pane_id":"fixture:p1","workspace_id":"fixture","terminal_id":"fixture-terminal",
            "launch":launch,"originals":{
                "pane":{"result":{"pane":{"pane_id":"fixture:p1","workspace_id":"fixture","terminal_id":"fixture-terminal","cwd":str(self.root)}}},
                "process":{"result":{"process_info":{"pane_id":"fixture:p1","foreground_processes":[{"pid":os.getpid(),"argv":launch["command_resolved"],"cwd":str(self.root)}]}}},
                "plugin":{"result":{"plugins":[{"plugin_id":"fixture.control","enabled":True,"manifest_path":launch["manifest"],"panes":[{"id":"supervise","command":launch["command"]}]}]}}}}

    def tearDown(self):
        self.guard.__exit__(None,None,None)
        local_tests.HarnessCycleTests.tearDown(self)

    def make(self,*,requests=12,reserved=(),wire_version=wire.VERSION,estimated_cap_nano_usd=2_000_000_000,output_profile=None):
        prepared=contract.prepare(candidate_repo=self.candidate,base_sha=self.base,objective="Complete the D2 usage aggregation contract",
            scope_contract={"schema_version":1,"editable_paths":["report.py"],"temporary_directories":[".tmp"],"max_entries":100,"max_bytes":1024*1024},
            acceptance_contract={"schema_version":1,"requirements":[{"id":"api","description":"Retain the public API","checks":[{"kind":"text_contains","path":"report.py","expected":"def summarize_usage("}]}]},
            suite=local_tests.checker.suite("D2",reserved=reserved),timeout_seconds=600,public_readonly_paths=["SPEC.md"])
        identity=str(uuid.uuid4());start=time.time();runs=self.root/"runs";runs.mkdir(mode=0o700)
        financial=local_tests.budget.contract(cycle_id=identity,deadline_at=start+600,max_requests=requests,estimated_cap_nano_usd=estimated_cap_nano_usd)
        if output_profile is not None:
            from fleet_harness_campaign import financial_limits
            financial=financial_limits(cycle_id=identity,deadline_at=start+600,requests=requests,profile=output_profile)
        limits=budget.contract(financial,
            control_plan=self.control_plan,control_clock=self.guard.clock,mode="synthetic_tls",approval_sha256=None,synthetic_endpoint=self.synthetic_endpoint,
            owner_store={"runs":str(runs),"identity":control.runtime.directory_identity_from_stat(runs.lstat())},wire_version=wire_version)
        spec=contract.create(prepared,cycle_id=identity,started_at=start,budget_limits=limits)
        owner=local_tests.cycle.Cycle.create(runs,spec)
        review=local_tests.fleet_json.loads((local_tests.FIXTURES/"source-admissions.json").read_bytes())["D2"]
        backend=backend_module.ControlHarnessBackend(owner,guard=self.guard,approval={"mode":"synthetic_tls","paid_authority":False},
            dependencies=self.root/"deps",source_admissions=[review])
        return owner,backend

    submit=local_tests.HarnessCycleTests.submit

    def drive(self,owner,backend):
        for response in self.responses:
            if isinstance(response,dict):
                for choice in response.get("choices",[]):
                    choice["message"]["reasoning_content"]="synthetic thinking fixture"
                    if getattr(self,"indexed_tools",True):
                        for index,call in enumerate(choice["message"].get("tool_calls",[])):call["index"]=index
        result=local_tests.HarnessCycleTests.drive(self,owner,backend)
        for request in self.requests:
            for message in request["messages"]:
                if message["role"]=="assistant":self.assertEqual(message.get("reasoning_content"),"synthetic thinking fixture")
        return result

    test_repair_and_revision_bound_acceptance=local_tests.HarnessCycleTests.test_real_work_submit_freeze_repair_and_revision_bound_acceptance
    test_cancel_inflight_preserves_original_reservation=local_tests.HarnessCycleTests.test_actual_cancel_during_http_preserves_late_response_reservation
    test_truncated_output_feedback_survives_interruption_and_rejects_foreign_reports=local_tests.HarnessCycleTests.test_truncated_output_feedback_survives_interruption_and_rejects_foreign_reports
    test_request_budget_exhaustion_never_renews_on_protocol_repairs=local_tests.HarnessCycleTests.test_request_budget_exhaustion_never_renews_on_protocol_repairs

    def test_expanded_limit_allows_eighth_request_across_repair_with_same_budget(self):
        self.responses=[{"choices":[{"finish_reason":"length","message":{"role":"assistant","content":""}}]}]
        self.responses += [local_tests.response("pwd",identifier="read-"+str(i),content="") for i in range(6)]
        self.responses += [self.submit("eighth-final")]
        owner,backend=self.make(requests=12,estimated_cap_nano_usd=1_200_000_000)
        result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",result)
        self.assertEqual(len(self.requests),8);self.assertEqual(result["attempts"],2)
        limits=owner.load()["contract"]["request_budget"]["financial"]
        self.assertEqual(limits["max_requests"],12);self.assertEqual(limits["estimated_cap_nano_usd"],1_200_000_000)
        self.assertEqual(backend.ledger.summary()["reserved_estimated_nano_usd"],800_000_000)
        self.assertEqual(len({a["admission"]["deadline_at"] for a in owner.load()["attempts"]}),1)
        self.assertEqual(owner.verify(),result)

    def test_thinking_profile_repair_uses_same_budget_and_versioned_delivery(self):
        self.responses=[{"choices":[{"finish_reason":"length","message":{"role":"assistant","content":""}}]},self.submit("new-profile-final")]
        owner,backend=self.make(wire_version=wire.THINKING_32K_VERSION,output_profile="thinking-32k-v1")
        result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",result)
        self.assertEqual([p["max_tokens"] for p in self.requests],[32768,32768])
        self.assertEqual(result["attempts"],2)
        self.assertEqual(backend.ledger.summary()["reserved_estimated_nano_usd"],400_000_000)
        self.assertEqual(len({a["admission"]["deadline_at"] for a in owner.load()["attempts"]}),1)
        self.assertEqual(owner.verify(),result)

    def test_legacy_delivery_remains_verifiable_after_wire_upgrade(self):
        self.indexed_tools=False
        self.responses=[self.submit("legacy-final")]
        owner,backend=self.make(wire_version=wire.LEGACY_VERSION)
        result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",result)
        self.assertEqual(owner.load()["contract"]["request_budget"]["wire_version"],wire.LEGACY_VERSION)
        self.assertEqual(owner.verify(),result)

    def test_indexed_delivery_replay_rejects_changed_projection_or_version(self):
        self.responses=[self.submit("indexed-final")]
        owner,backend=self.make();result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",result)
        attempt=owner.load()["attempts"][-1]
        value=owner.json(attempt["response"]["transcript"]);final=owner.get(attempt["response"]["final"])
        _,rows=budget.verify_originals(value["budget_originals"],backend.contract["request_budget"],contract_sha256=owner.pin)
        self.assertEqual(rows[0]["response"]["choices"][0]["message"]["tool_calls"][0]["index"],0)
        from fleet_harness_delivery import verify_terminal
        for change in ("extra_index","id","arguments"):
            forged=copy.deepcopy(value)
            message=next(m for m in forged["terminal"]["trajectory"]["messages"] if m["role"]=="assistant")
            call=message["extra"]["response"]["choices"][0]["message"]["tool_calls"][0]
            if change=="extra_index":call["index"]=0
            elif change=="id":call["id"]="foreign"
            else:call["function"]["arguments"]='{"command":"pwd"}'
            with self.subTest(change=change),self.assertRaises(ValueError):
                verify_terminal(local_tests.fleet_json.canonical_bytes(forged),final,contract=backend.contract,admission=attempt["admission"])
        old=copy.deepcopy(backend.contract);old["request_budget"]["wire_version"]=wire.LEGACY_VERSION
        with self.assertRaises(ValueError):
            verify_terminal(local_tests.fleet_json.canonical_bytes(value),final,contract=old,admission=attempt["admission"])

    def test_protocol_repairs_share_limits(self):
        self.responses=[self.submit("empty",final=""),self.submit("invalid",final="not JSON"),self.submit("good")]
        owner,backend=self.make();result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",result)
        self.assertEqual(result["attempts"],3);self.assertEqual(len(self.requests),3)
        attempts=owner.load()["attempts"]
        self.assertEqual(len({a["admission"]["deadline_at"] for a in attempts}),1)
        self.assertFalse(attempts[0]["classified"]["valid"]);self.assertFalse(attempts[1]["classified"]["valid"])
        from fleet_harness_delivery import verify_continuity
        originals=[owner.get(a["response"]["transcript"]) for a in attempts]
        value=local_tests.fleet_json.loads(originals[-1]);early=local_tests.fleet_json.loads(originals[0])["budget_originals"]
        for directory in ("financial","control"):
            for name in early[directory]:
                if "/" in name:del value["budget_originals"][directory][name]
        value["budget"],_=budget.verify_originals(value["budget_originals"],backend.contract["request_budget"],contract_sha256=owner.pin)
        with self.assertRaises(ValueError):
            verify_continuity(local_tests.fleet_json.canonical_bytes(value),list(zip(originals[:-1],[a["admission"] for a in attempts[:-1]])),
                limits=backend.contract["request_budget"],admission=attempts[-1]["admission"])

    def crash_and_recover(self,point):
        self.responses=([local_tests.response("printf '\\n# once\\n' >> report.py",identifier="write-once"),self.submit("final")]
            if point=="after_write" else [self.submit("final")])
        for response in self.responses:
            response["choices"][0]["message"]["reasoning_content"]="synthetic thinking fixture"
            for index,call in enumerate(response["choices"][0]["message"].get("tool_calls",[])):call["index"]=index
        original=(self.candidate/"report.py").read_bytes()
        owner,_=self.make();clock=copy.deepcopy(self.guard.clock)
        self.guard.__exit__(None,None,None)
        script='''import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,str(Path(sys.argv[1]).parent))
import fleet_herdr_owner_cycle as cycle,fleet_harness_control as control,fleet_harness_live_backend as backend,fleet_json
from tests.test_fleet_harness_live_campaign import fixture_bind
owner=cycle.Cycle(Path(sys.argv[2]),sys.argv[3],contract_sha256=sys.argv[4])
review=fleet_json.loads(Path(sys.argv[8]).read_bytes())['D2']
def fault(point):
    if point==sys.argv[7]:os._exit(91)
with control.OwnedLease(Path(sys.argv[5]),sys.argv[6]) as guard:
    fixture_bind(guard)
    runtime=backend.ControlHarnessBackend(owner,guard=guard,approval={"mode":"synthetic_tls","paid_authority":False},dependencies=Path(sys.argv[9]),source_admissions=[review],fault=fault)
    for _ in range(8):owner.tick(runtime)
os._exit(92)
'''
        try:
            child=subprocess.run([sys.executable,"-I","-B","-c",script,str(Path(control.__file__).parent),str(owner.runs),owner.cycle_id,owner.pin,str(self.root),control.pin(self.control_plan),point,str(local_tests.FIXTURES/"source-admissions.json"),str(self.root/"deps")],capture_output=True,timeout=55)
            self.assertEqual(child.returncode,91,child.stderr)
        finally:
            self.guard=control.OwnedLease(self.root,control.pin(self.control_plan)).__enter__()
            from tests.test_fleet_harness_live_campaign import fixture_bind
            fixture_bind(self.guard)
        self.assertEqual(self.guard.clock,clock)
        admission=copy.deepcopy(owner.load()["attempts"][0]["admission"])
        review=local_tests.fleet_json.loads((local_tests.FIXTURES/"source-admissions.json").read_bytes())["D2"]
        backend=backend_module.ControlHarnessBackend(owner,guard=self.guard,approval={"mode":"synthetic_tls","paid_authority":False},dependencies=self.root/"deps",source_admissions=[review])
        result=self.drive(owner,backend)
        self.assertEqual(len(owner.load()["attempts"]),1)
        self.assertEqual(owner.load()["attempts"][0]["admission"],admission)
        if point in {"after_reserve","after_send_intent"}:
            self.assertEqual(result["status"],"blocked",result)
            self.assertEqual(len(self.requests),0)
            self.assertEqual(backend.ledger.summary()["admitted_requests"],1)
            self.assertTrue(backend.ledger.summary()["ambiguous"])
            owner.control("cancel",request_id=str(uuid.uuid4()),target=backend_module.runtime.resource(admission),reason="finish owned interrupted v4 fixture")
            self.assertEqual(self.drive(owner,backend)["status"],"cancelled")
        elif point=="after_write":
            self.assertEqual(result["status"],"blocked",result)
            self.assertEqual(len(self.requests),2)
            self.assertEqual((self.candidate/"report.py").read_bytes(),original+b"\n# once\n")
            self.assertIsNone(owner.load()["terminal"])
        else:
            self.assertEqual(result["status"],"accepted_contract",result)
            self.assertEqual(len(self.requests),1)
            self.assertEqual(len(owner.load()["revisions"]),1)

    def test_process_loss_after_reserve(self):self.crash_and_recover("after_reserve")
    def test_process_loss_after_send_intent(self):self.crash_and_recover("after_send_intent")
    def test_process_loss_after_outcome_before_accounting(self):self.crash_and_recover("after_outcome")
    def test_process_loss_after_write(self):self.crash_and_recover("after_write")
    def test_process_loss_after_freeze(self):self.crash_and_recover("after_freeze")
    def test_process_loss_after_check_rpc(self):self.crash_and_recover("after_check_rpc")
    def test_process_loss_after_check(self):self.crash_and_recover("after_check")


if __name__=="__main__":unittest.main()
