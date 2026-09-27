"""Real owner journal, Mini, isolated shell/checker; synthetic HTTP responses only."""
import copy
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from unittest import mock
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
import fleet_harness_acceptance as checker
import fleet_harness_backend as backend_module
import fleet_harness_budget as budget
import fleet_harness_contract as contract
import fleet_harness_mini as mini
import fleet_harness_functional as functional
import fleet_herdr_owner_cycle as cycle
import fleet_herdr_work_packet as work
import fleet_json
from tests.test_fleet_harness_mini import response

FIXTURES = Path(__file__).parent/"fixtures/harness_v1"


@unittest.skipUnless(os.environ.get("FLEET_HARNESS_LOCAL_TESTS") == "1", "explicit isolated local conformance lane")
class HarnessCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fleet-harness-cycle-")
        self.root = Path(self.temp.name); self.candidate = self.root/"candidate"
        shutil.copytree(FIXTURES/"d2", self.candidate)
        (self.candidate/"SPEC.md").write_text("Implement summarize_usage according to the supplied D2 contract.\n")
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=self.candidate, stderr=subprocess.PIPE).decode().strip()
        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=Harness Fixture", "-c", "user.email=fixture@invalid", "commit", "-qm", "isolated fixture baseline")
        self.base = git("rev-parse", "HEAD")
        mini.freeze_dependencies(mini.DIST, self.root/"deps")
        self.responses, self.requests = [], []
        self.response_entered=threading.Event();self.response_release=None
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                outer.requests.append(fleet_json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                outer.response_entered.set()
                if outer.response_release is not None:outer.response_release.wait(20)
                value = outer.responses.pop(0) if outer.responses else {"error":"unexpected request"}
                raw = value if isinstance(value,bytes) else fleet_json.canonical_bytes(value)
                self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers()
                try:self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError):pass
        self.server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/chat/completions"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        self.temp.cleanup()

    def make(self, *, requests=12, reserved=()):
        prepared = contract.prepare(candidate_repo=self.candidate, base_sha=self.base, objective="Complete the D2 usage aggregation contract",
            scope_contract={"schema_version":1, "editable_paths":["report.py"], "temporary_directories":[".tmp"], "max_entries":100,"max_bytes":1024*1024},
            acceptance_contract={"schema_version":1, "requirements":[{"id":"api", "description":"Retain the public API", "checks":[{"kind":"text_contains","path":"report.py","expected":"def summarize_usage("}]}]},
            suite=checker.suite("D2", reserved=reserved), timeout_seconds=600,public_readonly_paths=["SPEC.md"])
        identity = str(uuid.uuid4()); start=time.time()
        spec = contract.create(prepared, cycle_id=identity, started_at=start,
            budget_limits=budget.contract(cycle_id=identity, deadline_at=start+600, max_requests=requests))
        (self.root/"runs").mkdir()
        owner=cycle.Cycle.create(self.root/"runs", spec)
        review = fleet_json.loads((FIXTURES/"source-admissions.json").read_bytes())["D2"]
        backend=backend_module.LocalHarnessBackend(owner,endpoint=self.endpoint,dependencies=self.root/"deps",source_admissions=[review])
        return owner,backend

    def submit(self, identifier, final=None):
        final = fleet_json.canonical_bytes({"type":"submit_candidate","summary":"synthetic fixture delivery","paths":["report.py"],"checks":[]}).decode() if final is None else final
        return response(identifier=identifier,content=final)

    def drive(self, owner, backend):
        for _ in range(20):
            result=owner.tick(backend)
            if result["status"] in {"accepted_contract","exhausted","cancelled","blocked"}: return result
        self.fail("bounded cycle did not settle: "+str((result,[a.get("functional") for a in owner.load()["attempts"]])))

    def test_real_work_submit_freeze_repair_and_revision_bound_acceptance(self):
        good=(FIXTURES/"d2/report.py").read_text()
        bad=good.replace('for record in latest:', 'for record in latest[:1]:')
        def write(source): return "python3 -B - <<'PY'\nfrom pathlib import Path\nPath('report.py').write_text("+repr(source)+")\nPY\n"
        self.responses=[response(write(bad),identifier="bad-write"),self.submit("first-final"),
                        response(write(good),identifier="repair-write"),self.submit("second-final")]
        owner,backend=self.make()
        result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",(result,[a["classified"] for a in owner.load()["attempts"]]))
        current=owner.load(); self.assertEqual(len(current["attempts"]),2); self.assertEqual(len(current["revisions"]),2)
        self.assertNotEqual(owner.json(current["revisions"][0])["tree"],owner.json(current["revisions"][1])["tree"])
        self.assertEqual(owner.json(current["revisions"][1])["parent_revision"],current["revisions"][0])
        feedback=owner.json(current["attempts"][1]["task"])["continuation"]["feedback"]["detail"]["functional"]
        self.assertEqual(feedback["revision_sha256"],current["revisions"][0]);self.assertTrue(feedback["public_feedback"])
        self.assertEqual(len(self.requests),4);self.assertEqual(backend.ledger.summary()["admitted_requests"],4)
        self.assertIsNone(backend.ledger.summary()["billed_cost_usd"])
        shutil.rmtree(self.candidate)
        shutil.rmtree(backend.root/"checks")
        self.assertEqual(owner.verify(),result)

    def test_protocol_repair_consumes_same_three_attempts(self):
        self.responses=[self.submit("empty",final=""),self.submit("invalid",final="not JSON"),self.submit("good")]
        owner,backend=self.make()
        result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",result)
        self.assertEqual(result["attempts"],3);self.assertEqual(len(self.requests),3)
        attempts=owner.load()["attempts"]
        self.assertEqual(len({a["admission"]["deadline_at"] for a in attempts}),1)
        self.assertFalse(attempts[0]["classified"]["valid"])
        self.assertFalse(attempts[1]["classified"]["valid"])
        from fleet_harness_delivery import verify_continuity
        originals=[owner.get(a["response"]["transcript"]) for a in attempts]
        value=fleet_json.loads(originals[-1]);early=fleet_json.loads(originals[0])["budget_originals"]
        for name in early:
            if name!="contract.json":del value["budget_originals"][name]
        value["budget"],_=budget.verify_originals(value["budget_originals"],backend.contract["request_budget"])
        with self.assertRaises(ValueError):
            verify_continuity(fleet_json.canonical_bytes(value),list(zip(originals[:-1],[a["admission"] for a in attempts[:-1]])),
                limits=backend.contract["request_budget"],admission=attempts[-1]["admission"])

    def test_input_snapshot_recovery_never_refreshes_drift_or_dispatches_without_anchor(self):
        owner,backend=self.make();append=owner._append
        class Interrupted(BaseException):pass
        def stop(kind,payload,**kwargs):
            result=append(kind,payload,**kwargs)
            if kind=="input_snapshot":raise Interrupted()
            return result
        with mock.patch.object(owner,"_append",side_effect=stop),self.assertRaises(Interrupted):owner.tick(backend)
        state=owner.load();a=state["attempts"][0];pin=a["input_snapshot"]
        original=owner.get(pin);self.assertFalse(a["intent"]);self.assertFalse(self.requests)
        no_anchor=copy.deepcopy(state);no_anchor["attempts"][0]["input_snapshot"]=None
        event={"seq":state["seq"]+1,"previous":state["head"],"at":state["last_at"],"kind":"dispatch_intent","payload":backend_module.runtime.resource(a["admission"])}
        with self.assertRaisesRegex(cycle.CycleError,"immutable input snapshot"):owner._apply(no_anchor,event)
        (self.candidate/"report.py").write_text("changed after durable input\n")
        owner.tick(backend);result=owner.tick(backend)
        self.assertEqual(result["status"],"blocked",result)
        self.assertEqual(len(owner.load()["attempts"]),1);self.assertEqual(owner.get(pin),original)
        self.assertFalse(self.requests);self.assertFalse(list(backend.root.rglob("resource.json")))
        dependency=list(backend.root.rglob("dependency.json"));self.assertEqual(len(dependency),1)
        self.assertIn("no refresh",fleet_json.loads(dependency[0].read_bytes())["reason"])

    def test_protocol_repair_after_writes_uses_new_admission_input_without_new_budget(self):
        good=(self.candidate/"report.py").read_text();bad=good.replace('for record in latest:', 'for record in latest[:1]:')
        def write(text):return "python3 -B - <<'PY'\nfrom pathlib import Path\nPath('report.py').write_text("+repr(text)+")\nPY\n"
        self.responses=[response(write(bad),identifier="initial-write"),self.submit("empty",final=""),
            response(write(good),identifier="repair-write"),self.submit("valid")]
        owner,backend=self.make();result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",result)
        attempts=owner.load()["attempts"];self.assertEqual(len(attempts),2)
        self.assertNotEqual(attempts[0]["input_snapshot"],attempts[1]["input_snapshot"])
        second=owner.json(attempts[1]["input_snapshot"])["entries"]["report.py"]
        self.assertEqual(second["sha256"],mini.digest(bad.encode()))
        self.assertEqual(len(self.requests),4);self.assertEqual(result["revisions"],1)
        self.assertEqual(owner.verify(),result)

    def test_change_between_delivery_and_freeze_cannot_be_accepted(self):
        self.responses=[self.submit("valid")];owner,backend=self.make()
        self.assertEqual(owner.tick(backend)["status"],"dispatched")
        (self.candidate/"report.py").write_text("# different frozen candidate\n"+(self.candidate/"report.py").read_text())
        with self.assertRaisesRegex(cycle.CycleError,"frozen revision differs"):owner.tick(backend)
        state=owner.load();self.assertIsNone(state["terminal"]);self.assertFalse(state["revisions"])
        self.assertEqual(len(state["attempts"]),1);self.assertEqual(len(self.requests),1)

    def test_non_json_http_response_is_a_counted_protocol_repair(self):
        self.responses=[b"invalid JSON from HTTP 200",self.submit("valid")]
        owner,backend=self.make();result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",result)
        self.assertEqual(result["attempts"],2);self.assertEqual(len(self.requests),2)
        self.assertEqual(backend.ledger.summary()["admitted_requests"],2)

    def test_truncated_output_feedback_survives_interruption_and_rejects_foreign_reports(self):
        from fleet_harness_delivery import verify_terminal, FAILURE_VERSION
        self.responses=[{"choices":[{"finish_reason":"length", "message":{
            "role":"assistant", "content":"", "reasoning_content":"provider-private-text-canary"}}]},
            self.submit("valid-after-truncation")]
        owner,backend=self.make()
        class Interrupted(BaseException):pass
        append=owner._append
        def interrupt(kind,payload,**kwargs):
            result=append(kind,payload,**kwargs)
            if kind=="classified":raise Interrupted()
            return result
        with mock.patch.object(owner,"_append",side_effect=interrupt),self.assertRaises(Interrupted):
            for _ in range(4):owner.tick(backend)
        original=owner.load()["attempts"][0]
        self.assertEqual(len(self.requests),1)
        report=fleet_json.loads(original["classified"]["error"])
        self.assertEqual(report["version"],FAILURE_VERSION)
        self.assertEqual(report["category"],"output_truncated_without_action")
        self.assertEqual(report["admission_sha256"],work.digest(original["admission"]))
        self.assertIsNone(report["revision_sha256"])
        self.assertEqual(report["observed"],{"finish_reason":"length","content_empty":True,
            "tool_calls":0,"max_output_tokens":8192})
        self.assertNotIn("provider-private-text-canary",original["classified"]["error"])
        value=owner.json(original["response"]["transcript"])
        legacy=copy.deepcopy(value);del legacy["public_failure"]
        with self.assertRaisesRegex(ValueError,"^invalid or foreign Mini delivery$"):
            verify_terminal(fleet_json.canonical_bytes(legacy),b"",contract=backend.contract,admission=original["admission"])
        for mutation in ("revision","task","admission","observation","error_cleared","final","terminal"):
            forged=copy.deepcopy(value);final=b""
            if mutation=="revision":forged["public_failure"]["revision_sha256"]="a"*64
            elif mutation=="task":forged["task"]["objective"]="foreign task"
            elif mutation=="admission":forged["admission"]["run_id"]=str(uuid.uuid4())
            elif mutation=="observation":forged["public_failure"]["observed"]["tool_calls"]=False
            elif mutation=="error_cleared":forged["error"]=None
            elif mutation=="final":final=b'{"type":"submit_candidate"}'
            else:forged["terminal"]={}
            with self.subTest(mutation=mutation),self.assertRaises(ValueError) as failure:
                verify_terminal(fleet_json.canonical_bytes(forged),final,contract=backend.contract,admission=original["admission"])
            self.assertNotIn('"category"',str(failure.exception))
        owner=cycle.Cycle(owner.runs,owner.cycle_id,contract_sha256=owner.pin)
        backend.cycle=owner
        result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",result)
        self.assertEqual(len(self.requests),2)
        attempts=owner.load()["attempts"]
        self.assertEqual(attempts[0]["response"],original["response"])
        feedback=owner.json(attempts[1]["task"])["continuation"]["feedback"]
        self.assertEqual(fleet_json.loads(feedback["detail"]),report)
        self.assertEqual(result["revisions"],1)
        self.assertEqual(owner.verify(),result)

    def test_real_process_loss_reserve_send_freeze_and_retained_check(self):
        script='''import os,sys
sys.path.insert(0,sys.argv[1])
from pathlib import Path
import fleet_herdr_owner_cycle as cycle, fleet_harness_backend as backend, fleet_json
owner=cycle.Cycle(Path(sys.argv[2]),sys.argv[3],contract_sha256=sys.argv[4])
def fault(point):
    if point==sys.argv[7]:os._exit(91)
review=fleet_json.loads(Path(sys.argv[8]).read_bytes())['D2']
runtime=backend.LocalHarnessBackend(owner,endpoint=sys.argv[5],dependencies=Path(sys.argv[6]),source_admissions=[review],fault=fault)
for _ in range(8):owner.tick(runtime)
os._exit(92)
'''
        # One fixture per interruption preserves exact ownership of each run.
        for point in ("after_reserve","after_send","after_freeze","after_check_rpc","after_check"):
            with self.subTest(point=point):
                if (self.root/"runs").exists():shutil.rmtree(self.root/"runs")
                self.responses=[self.submit("final")];self.requests.clear()
                owner,backend=self.make()
                child=subprocess.run([sys.executable,"-I","-B","-c",script,str(Path(mini.__file__).parent),str(owner.runs),owner.cycle_id,owner.pin,self.endpoint,str(self.root/"deps"),point,str(FIXTURES/"source-admissions.json")],capture_output=True,timeout=45)
                self.assertEqual(child.returncode,91,child.stderr)
                admission=copy.deepcopy(owner.load()["attempts"][0]["admission"])
                result=self.drive(owner,backend)
                self.assertEqual(len(owner.load()["attempts"]),1)
                self.assertEqual(owner.load()["attempts"][0]["admission"],admission)
                if point in ("after_reserve","after_send"):
                    self.assertEqual(result["status"],"blocked",result)
                    self.assertEqual(len(self.requests),0 if point=="after_reserve" else 1)
                    self.assertEqual(backend.ledger.summary()["admitted_requests"],1)
                    self.assertTrue(backend.ledger.summary()["ambiguous"])
                    owner.control("cancel",request_id=str(uuid.uuid4()),target=backend_module.runtime.resource(admission),reason="finish owned interrupted fixture")
                    self.assertEqual(self.drive(owner,backend)["status"],"cancelled")
                else:
                    a=owner.load()["attempts"][-1]
                    if a["functional"] and a["functional"]["receipt"]:
                        functional.verify(owner.json(a["functional"]["binding"]),owner.json(a["functional"]["receipt"]),owner.get,
                            backend.contract["prepared"]["sources"]["functional_tests"].encode())
                    self.assertEqual(result["status"],"accepted_contract",result)
                    self.assertEqual(len(self.requests),1)
                    self.assertEqual(len(owner.load()["revisions"]),1)

    def test_cleanup_failure_prevents_acceptance_and_recovery_reuses_admission(self):
        self.responses=[self.submit("final")]
        owner,backend=self.make()
        with mock.patch.object(backend_module.sandbox.Sandbox,"_remove_exact",side_effect=RuntimeError("owned cleanup failure")):
            with self.assertRaises(RuntimeError):owner.tick(backend)
        current=owner.load();admission=current["attempts"][0]["admission"]
        self.assertFalse(current["attempts"][0]["quiescent"]);self.assertIsNone(current["terminal"])
        result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",result)
        self.assertEqual(owner.load()["attempts"][0]["admission"],admission)
        self.assertEqual(len(self.requests),1)

    def test_actual_cancel_during_http_preserves_late_response_reservation(self):
        self.responses=[self.submit("late")];self.response_release=threading.Event()
        owner,backend=self.make();errors=[]
        def run():
            try:owner.tick(backend)
            except Exception as exc:errors.append(exc)
        worker=threading.Thread(target=run);worker.start()
        try:
            self.assertTrue(self.response_entered.wait(20),"synthetic request never started")
            admission=owner.load()["attempts"][0]["admission"]
            owner.control("cancel",request_id=str(uuid.uuid4()),target=backend_module.runtime.resource(admission),reason="cancel owned in-flight request")
            worker.join(15);self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
            self.assertEqual(self.drive(owner,backend)["status"],"cancelled")
            self.response_release.set();time.sleep(.05)
            self.assertEqual(owner.verify()["status"],"cancelled")
            self.assertEqual(len(self.requests),1);self.assertEqual(len(owner.load()["attempts"]),1)
            self.assertTrue(backend.ledger.summary()["ambiguous"]);self.assertIsNone(backend.ledger.summary()["observed_tokens"])
        finally:self.response_release.set();worker.join(20)

    def test_request_budget_exhaustion_never_renews_on_protocol_repairs(self):
        self.responses=[self.submit("invalid",final="")]
        owner,backend=self.make(requests=1);result=self.drive(owner,backend)
        self.assertEqual(result["status"],"exhausted",result)
        self.assertEqual(len(self.requests),1);self.assertEqual(backend.ledger.summary()["admitted_requests"],1)
        self.assertLessEqual(len(owner.load()["attempts"]),3)
        self.assertEqual(len({a["admission"]["deadline_at"] for a in owner.load()["attempts"]}),1)
        for attempt in owner.load()["attempts"][1:]:
            report=fleet_json.loads(attempt["classified"]["error"])
            self.assertEqual(report["category"],"request_limit_exhausted")
            self.assertEqual(report["observed"],{"admitted_requests":1,"new_send_permitted":False})
            self.assertEqual(report["expected"],{"requests_before_next_send_less_than":1})
            self.assertEqual(report["admission_sha256"],work.digest(attempt["admission"]))
        self.assertEqual(owner.verify(),result)

    def test_counterexample_in_rpc_survives_crash_before_derived_observation(self):
        good=(FIXTURES/"d2/report.py").read_text()
        bad=good.replace('"total_runs": len(admitted)', '"total_runs": 999')
        self.assertNotEqual(bad,good)
        (self.candidate/"report.py").write_text(bad)
        write="python3 -B - <<'PY'\nfrom pathlib import Path\nPath('report.py').write_text("+repr(good)+")\nPY\n"
        self.responses=[self.submit("bad-final"),response(write,identifier="repair"),self.submit("fixed")]
        owner,backend=self.make()
        script='''import os,sys
sys.path.insert(0,sys.argv[1])
from pathlib import Path
import fleet_herdr_owner_cycle as cycle,fleet_harness_backend as backend,fleet_harness_sandbox as sandbox,fleet_json
owner=cycle.Cycle(Path(sys.argv[2]),sys.argv[3],contract_sha256=sys.argv[4])
original=sandbox.Sandbox.rpc
def rpc(self,request,**kwargs):
    result=original(self,request,**kwargs)
    if request['id']=='regular':os._exit(91)
    return result
sandbox.Sandbox.rpc=rpc
review=fleet_json.loads(Path(sys.argv[7]).read_bytes())['D2']
runtime=backend.LocalHarnessBackend(owner,endpoint=sys.argv[5],dependencies=Path(sys.argv[6]),source_admissions=[review])
for _ in range(6):owner.tick(runtime)
os._exit(92)
'''
        child=subprocess.run([sys.executable,"-I","-B","-c",script,str(Path(mini.__file__).parent),str(owner.runs),owner.cycle_id,owner.pin,self.endpoint,str(self.root/"deps"),str(FIXTURES/"source-admissions.json")],capture_output=True,timeout=40)
        self.assertEqual(child.returncode,91,child.stderr)
        first=owner.load()["attempts"][0];root=backend.root/"checks"/first["functional"]["binding"]
        self.assertFalse((root/"check/observations").exists())
        result=self.drive(owner,backend);self.assertEqual(result["status"],"accepted_contract",result)
        self.assertEqual(result["attempts"],2);self.assertEqual(len(self.requests),3)
        self.assertFalse(list(root.glob("recovery-*.json")))
        original=fleet_json.loads((root/"check/result.json").read_bytes())
        self.assertEqual(original["status"],"failed");self.assertEqual(len(original["results"]),1)
        self.assertEqual(original["public_feedback"][0]["observed"]["value"]["total_runs"],999)

    def test_real_process_loss_after_write_restores_same_execution_without_resend(self):
        # Append a harmless comment once. Replaying bash would append it twice.
        self.responses=[response("printf '\\n# once\\n' >> report.py",identifier="write-once"),self.submit("final")]
        owner,backend=self.make()
        # This candidate variant needs a separate exact source admission. The
        # control below changes only a comment; source review still checks bytes.
        expected=(self.candidate/"report.py").read_bytes()+b"\n# once\n"
        # Do not self-admit the variant: it must stop at the source-review gate.
        script='''import json,os,sys
sys.path.insert(0,sys.argv[1])
from pathlib import Path
import fleet_herdr_owner_cycle as cycle, fleet_harness_backend as backend, fleet_json
owner=cycle.Cycle(Path(sys.argv[2]),sys.argv[3],contract_sha256=sys.argv[4])
def fault(point):
    if point=='after_write': os._exit(91)
runtime=backend.LocalHarnessBackend(owner,endpoint=sys.argv[5],dependencies=Path(sys.argv[6]),fault=fault)
owner.tick(runtime)
os._exit(92)
'''
        child=subprocess.run([sys.executable,"-I","-B","-c",script,str(Path(mini.__file__).parent),str(owner.runs),owner.cycle_id,owner.pin,self.endpoint,str(self.root/"deps")],capture_output=True,timeout=40)
        self.assertEqual(child.returncode,91,child.stderr)
        admitted=owner.load()["attempts"][0]["admission"]
        self.assertEqual(len(self.requests),1)
        result=self.drive(owner,backend)
        self.assertEqual(result["status"],"blocked",result)
        self.assertEqual(len(owner.load()["attempts"]),1)
        self.assertEqual(owner.load()["attempts"][0]["admission"],admitted)
        self.assertEqual((self.candidate/"report.py").read_bytes(),expected)
        self.assertEqual(len(self.requests),2)
        a=owner.load()["attempts"][0]
        self.assertIsNone(a["checks"])
        path=backend.root/"checks"/a["functional"]["binding"]/"check/result.json"
        self.assertEqual(fleet_json.loads(path.read_bytes())["pending"],["independent_source_admission_required"])

    def test_independent_source_review_resumes_frozen_check_without_rerunning(self):
        self.responses=[self.submit("final")]
        owner,backend=self.make()
        reviewed=backend.source_admissions.pop()
        self.assertEqual(self.drive(owner,backend)["status"],"blocked")
        a=owner.load()["attempts"][0];bound=owner.json(a["functional"]["binding"])
        path=backend.root/"checks"/work.digest(bound)/"check/result.json";original=path.read_bytes()
        self.assertEqual(fleet_json.loads(original)["status"],"blocked")
        backend.register_source_review(bound,reviewed)
        check_root=path.parent; originals=functional.collect(check_root)
        result_original=fleet_json.loads(original); original_contract=fleet_json.loads(originals["contract.json"])
        for name in ("resource/cleanup.json","resource/stdout.raw","resource/terminal-streams.json","resource/cleanup-observation.json"):
            damaged=dict(originals);del damaged[name]
            with self.subTest(missing=name),self.assertRaises((ValueError,KeyError)):
                functional.reviewed_status(bound,result_original,original_contract,damaged)
        for key in ("resources_clean","inactive"):
            damaged=dict(originals);value=fleet_json.loads(damaged["resource/cleanup.json"]);value[key]=False
            damaged["resource/cleanup.json"]=fleet_json.canonical_bytes(value)
            with self.subTest(cleanup=key),self.assertRaises(ValueError):functional.reviewed_status(bound,result_original,original_contract,damaged)
        result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",result)
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(result["attempts"],1);self.assertEqual(len(self.requests),1)

    def test_reserved_probe_custody_and_worker_filesystem_boundary(self):
        secret="reserved-"+str(uuid.uuid4())
        private={"id":"held-1","family":"selection","visibility":"reserved",
            "request":{"op":"usage","records":[checker.row(mission_id=secret,sequence=19,prompt_tokens=37)],
                "mission":secret,"admitted":["r"]},
            "expected":{"value":checker.summary(prompt=37),"errors":None}}
        private_path=self.root/"private-suite.json";private_path.write_bytes(fleet_json.canonical_bytes(private));private_path.chmod(0o600)
        (self.candidate/".gitignore").write_text(".env\n.aws/\n")
        (self.candidate/".env").write_text("SYNTHETIC_LOCAL_SECRET");(self.candidate/".env").chmod(0o600)
        (self.candidate/".aws").mkdir();(self.candidate/".aws/credentials").write_text("SYNTHETIC_LOCAL_CREDENTIAL")
        (self.candidate/".aws/credentials").chmod(0o600)
        # The worker is told only a path to probe, never the canary contents.
        command="python3 -B - <<'PY'\nfrom pathlib import Path\nassert not Path("+repr(str(private_path))+").exists()\nassert not Path('/deps').exists()\nassert not Path('/proc/1/root"+str(private_path)+"').exists()\nassert not Path('/candidate/.env').exists()\nassert not Path('/candidate/.aws/credentials').exists()\nassert not Path('/candidate/.git').exists()\nassert Path('/candidate/SPEC.md').read_text().startswith('Implement')\nprint('private paths inaccessible')\nPY\n"
        self.responses=[response(command,identifier="custody"),self.submit("final")]
        owner,backend=self.make(reserved=[private]);result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",result)
        for request in self.requests:self.assertNotIn(secret,fleet_json.canonical_bytes(request).decode())
        a=owner.load()["attempts"][0]
        self.assertNotIn(secret,owner.get(a["task"]).decode())
        transcript=fleet_json.loads(owner.get(a["response"]["transcript"]))
        self.assertNotIn(secret,fleet_json.canonical_bytes(transcript).decode())
        self.assertNotIn("SYNTHETIC_LOCAL_SECRET",fleet_json.canonical_bytes(transcript).decode())
        self.assertNotIn("SYNTHETIC_LOCAL_CREDENTIAL",fleet_json.canonical_bytes(transcript).decode())
        self.assertEqual((self.candidate/".env").read_text(),"SYNTHETIC_LOCAL_SECRET")
        inventory=contract.scope.capture(self.candidate,backend.contract["prepared"]["sources"]["scope"])
        self.assertIn(".env",inventory["entries"]);self.assertIn(".aws/credentials",inventory["entries"])
        check=owner.json(a["functional"]["receipt"])
        result_original=fleet_json.loads(owner.get(check["evidence"]["result.json"]))
        self.assertTrue(next(r for r in result_original["results"] if r["id"]=="held-1")["passed"])
        suite=fleet_json.loads(owner.get(check["evidence"]["suite.json"]))
        self.assertFalse(suite["custody"]["blind_to_maintainer"])

    def test_malformed_inventory_delivery_cannot_block_durable_cancellation(self):
        class Interrupted(BaseException):
            pass
        self.responses=[self.submit("final")]
        owner,backend=self.make()
        def interrupt(name):
            if name=="after_delivery":raise Interrupted()
        backend.fault=interrupt
        with self.assertRaises(Interrupted):owner.tick(backend)
        backend.fault=None
        admission=owner.load()["attempts"][0]["admission"]
        path=backend._attempt(admission)/"delivery.json"
        value=fleet_json.loads(path.read_bytes())
        originals=value["tools"][0]["originals"]
        publication=fleet_json.loads(base64.b64decode(originals["publication.json"]))
        publication["projection_after"]["inventory"]["entries"]["report.py"]["bytes"]=True
        originals["publication.json"]=base64.b64encode(fleet_json.canonical_bytes(publication)).decode()
        path.write_bytes(fleet_json.canonical_bytes(value))
        owner.control("cancel",request_id=str(uuid.uuid4()),target=backend_module.runtime.resource(admission),reason="cancel malformed delivery")
        result=self.drive(owner,backend)
        self.assertEqual(result["status"],"cancelled",result)
        state=owner.load()
        self.assertEqual(len(state["attempts"]),1)
        self.assertFalse(state["attempts"][0]["classified"]["valid"])
        self.assertEqual(owner.verify(),result)
        self.assertEqual(owner.tick(backend),result)
        self.assertEqual(len(self.requests),1)

    def test_wrong_revision_late_and_duplicate_finals_do_not_reopen_acceptance(self):
        self.responses=[response("true",identifier="inspection"),self.submit("final")]
        owner,backend=self.make();result=self.drive(owner,backend)
        self.assertEqual(result["status"],"accepted_contract",result)
        a=owner.load()["attempts"][0]
        raw=owner.get(a["response"]["transcript"]);final=owner.get(a["response"]["final"])
        from fleet_harness_delivery import verify_terminal
        value=fleet_json.loads(raw)
        changed=copy.deepcopy(value);changed["admission"]["parent_revision"]="f"*64
        with self.assertRaises(ValueError):verify_terminal(fleet_json.canonical_bytes(changed),final,contract=backend.contract,admission=a["admission"])
        changed=copy.deepcopy(value);changed["terminal"]["trajectory"]["messages"].append(changed["terminal"]["trajectory"]["messages"][-1])
        with self.assertRaises(ValueError):verify_terminal(fleet_json.canonical_bytes(changed),final,contract=backend.contract,admission=a["admission"])
        # Claims in a derived summary cannot replace exact provider originals.
        changed=copy.deepcopy(value);changed["budget"]["admitted_requests"]=0
        with self.assertRaises(ValueError):verify_terminal(fleet_json.canonical_bytes(changed),final,contract=backend.contract,admission=a["admission"])
        changed=copy.deepcopy(value);key=next(k for k in changed["budget_originals"] if k.startswith("payloads/"))
        original=fleet_json.loads(base64.b64decode(changed["budget_originals"][key]));original["payload"]["messages"][1]["content"]="another task"
        changed["budget_originals"][key]=base64.b64encode(fleet_json.canonical_bytes(original)).decode()
        with self.assertRaises(ValueError):verify_terminal(fleet_json.canonical_bytes(changed),final,contract=backend.contract,admission=a["admission"])
        for number in (False,0.0):
            changed=copy.deepcopy(value);tool=changed["tools"][0]
            original=fleet_json.loads(base64.b64decode(tool["originals"]["response.json"]));original["value"]["returncode"]=number
            tool["originals"]["response.json"]=base64.b64encode(fleet_json.canonical_bytes(original)).decode()
            with self.subTest(returncode=number),self.assertRaises(ValueError):verify_terminal(fleet_json.canonical_bytes(changed),final,contract=backend.contract,admission=a["admission"])
        changed=copy.deepcopy(value);changed["terminal"]["trajectory"]["info"]=[]
        with self.assertRaises(ValueError):backend_module.runtime.verify_terminal(fleet_json.canonical_bytes(changed),final,contract=backend.contract,admission=a["admission"])
        for tamper in ("extra", "omitted", "root", "git", "renew", "legacy", "size", "continuity"):
            changed=copy.deepcopy(value);originals=changed["tools"][-1 if tamper=="continuity" else 0]["originals"]
            intent=fleet_json.loads(base64.b64decode(originals["intent.json"]))
            publication=fleet_json.loads(base64.b64decode(originals["publication.json"]))
            if tamper=="extra":
                for snapshot in (intent["projection_before"],publication["projection_after"]):
                    snapshot["inventory"]["entries"][".env"]={"kind":"file","mode":0o644,"sha256":"f"*64,"bytes":5}
            elif tamper=="omitted":
                for snapshot in (intent["projection_before"],publication["projection_after"]):del snapshot["inventory"]["entries"]["SPEC.md"]
            elif tamper=="root":intent["projection_before"]["inventory"]["root"]="/foreign-projection"
            elif tamper=="git":intent["projection_before"]["root_git_absent"]=False
            elif tamper=="renew":
                intent["public_read"]["read_only"]["SPEC.md"]="f"*64
                for snapshot in (intent["projection_before"],publication["projection_after"]):snapshot["inventory"]["entries"]["SPEC.md"]["sha256"]="f"*64
            elif tamper=="size":publication["projection_after"]["inventory"]["entries"]["report.py"]["bytes"]=0
            elif tamper=="continuity":
                intent["before"]["report.py"]="f"*64;publication["before"]["report.py"]="f"*64
                intent["projection_before"]["inventory"]["entries"]["report.py"]["sha256"]="f"*64
            else:intent["version"]="owned-executor-v1";del intent["public_read"]
            originals["intent.json"]=base64.b64encode(fleet_json.canonical_bytes(intent)).decode()
            originals["publication.json"]=base64.b64encode(fleet_json.canonical_bytes(publication)).decode()
            with self.subTest(tamper=tamper),self.assertRaises(ValueError):
                verify_terminal(fleet_json.canonical_bytes(changed),final,contract=backend.contract,admission=a["admission"])
        for leak in (None,{"reserved":[{"expected":"SYNTHETIC_PRIVATE_CHECK"}]}):
            changed=copy.deepcopy(value);originals=changed["tools"][0]["originals"]
            if leak is None:del originals["bridge/public-checks.json"]
            else:
                public=fleet_json.loads(base64.b64decode(originals["bridge/public-checks.json"]))
                originals["bridge/public-checks.json"]=base64.b64encode(fleet_json.canonical_bytes({**public,**leak})).decode()
            with self.subTest(leak=leak),self.assertRaisesRegex(ValueError,"exact public contract projection"):
                verify_terminal(fleet_json.canonical_bytes(changed),final,contract=backend.contract,admission=a["admission"])
        from fleet_harness_delivery import verify_continuity
        changed=copy.deepcopy(value)
        call_name=next(n for n in changed["budget_originals"] if n.startswith("calls/"))
        call=fleet_json.loads(base64.b64decode(changed["budget_originals"][call_name]));call["admission"]="f"*64
        changed["budget_originals"][call_name]=base64.b64encode(fleet_json.canonical_bytes(call)).decode()
        with self.assertRaises(ValueError):verify_continuity(fleet_json.canonical_bytes(changed),[],limits=backend.contract["request_budget"],admission=a["admission"])
        head=owner.load()["head"]
        owner.retain_late(work.digest(a["admission"]),transcript=raw,final=b"late unrelated final")
        self.assertEqual(owner.load()["head"],head);self.assertEqual(owner.verify(),result)


if __name__ == "__main__": unittest.main()
