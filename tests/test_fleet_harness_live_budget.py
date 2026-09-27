"""Synthetic TLS, exact request counts and durable CONTROL evidence; no provider."""
import base64
import copy
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import fleet_harness_control as control
import fleet_harness_budget as budget
import fleet_harness_live_budget as live
import fleet_harness_provider_protocol as wire
from fleet_harness_backend import TOOLS
import tests.test_fleet_harness_https as https_tests


class LiveBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="fleet-live-budget-")
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.launch={"herdr":"/bin/false","manifest":str(self.root/"plugin.toml"),"manifest_sha256":"a"*64,
            "plugin_id":"fixture.control","entrypoint":"control","cwd":str(self.root),"command":[sys.executable,"-B","fixture.py"]}
        self.plan=control.prepare(self.root,campaign_sha256="a"*64,seconds=60,launch=self.launch)
        self.guard=control.OwnedLease(self.root,control.pin(self.plan)).__enter__()
        launch=self.plan["launch"]
        # The mocked CLI provenance is explicitly a fixture; flock/process/root
        # and NumericHTTPS TLS transport below are real local resources.
        pane={"pane_id":"w1:p1","terminal_id":"fixture-terminal","workspace_id":"w1","cwd":str(self.root)}
        process={"pane_id":"w1:p1","foreground_processes":[{"pid":os.getpid(),"argv":launch["command_resolved"],"cwd":str(self.root)}]}
        plugin={"plugin_id":launch["plugin_id"],"enabled":True,"manifest_path":launch["manifest"],"panes":[{"id":"control","command":launch["command"]}]}
        self.guard._bound={"generation_sha256":control.pin(self.guard.generation),"pane_id":"w1:p1","terminal_id":"fixture-terminal","workspace_id":"w1",
            "launch":launch,"originals":{"pane":{"result":{"pane":pane}},"process":{"result":{"process_info":process}},"plugin":{"result":{"plugins":[plugin]}}}}
        self.cert=self.root/"cert.pem";self.key=self.root/"key.pem"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-keyout",str(self.key),"-out",str(self.cert),"-subj","/CN=api.deepseek.com","-addext","subjectAltName=DNS:api.deepseek.com"],capture_output=True,check=True,timeout=10)
        self.listener=socket.socket();self.listener.bind(("127.0.0.1",0));self.listener.listen(5);self.listener.settimeout(2)
        self.endpoint={"version":"synthetic-loopback-tls-v1","host":"127.0.0.1","port":self.listener.getsockname()[1],"certificate_pem":self.cert.read_text()}
        financial=budget.contract(cycle_id=str(uuid.uuid4()),deadline_at=self.guard.clock["deadline_at"],max_requests=3)
        self.limits=live.contract(financial,control_plan=self.plan,control_clock=self.guard.clock,mode="synthetic_tls",approval_sha256=None,synthetic_endpoint=self.endpoint)
        self.ledger=live.Ledger(self.guard,self.limits,approval={"mode":"synthetic_tls","paid_authority":False})
        self.payload=wire.payload([{"role":"user","content":"local fixture"}],TOOLS)

    def tearDown(self):
        self.guard.__exit__(None,None,None)
        self.listener.close()
        self.temp.cleanup()

    def test_thinking_profile_requires_reservation_and_sends_pinned_32768(self):
        from fleet_harness_campaign import financial_limits
        financial=financial_limits(cycle_id=str(uuid.uuid4()),deadline_at=self.guard.clock["deadline_at"],requests=3,profile="thinking-32k-v1")
        kwargs={"control_plan":self.plan,"control_clock":self.guard.clock,"mode":"synthetic_tls","approval_sha256":None,
                "synthetic_endpoint":self.endpoint,"wire_version":wire.THINKING_32K_VERSION}
        for key,value in (("reserve_tokens_per_request",65536),("reserve_nano_usd_per_request",100_000_000)):
            partial=copy.deepcopy(financial);partial[key]=value
            with self.assertRaises(budget.BudgetError):live.contract(partial,**kwargs)
        limits=live.contract(financial,**kwargs)
        ledger=live.Ledger(self.guard,limits,approval={"mode":"synthetic_tls","paid_authority":False})
        payload=wire.payload([{"role":"user","content":"x"*33000}],TOOLS,version=wire.THINKING_32K_VERSION)
        # A retained07 prompt already exceeded the old headroom for32K output.
        self.assertGreater(len(live.fleet_json.canonical_bytes(payload))+32768+1024,65536)
        oversized=wire.payload([{"role":"user","content":"x"*98000}],TOOLS,version=wire.THINKING_32K_VERSION)
        with mock.patch.object(live.https.NumericHTTPS,"connect",side_effect=AssertionError("oversized send")):
            with self.assertRaises(budget.BudgetError):ledger.request("too-large","a"*64,oversized,policy=https_tests.HTTPSTests().policy(),token=None)
            changed=copy.deepcopy(payload);changed["max_tokens"]=8192
            with self.assertRaises(budget.BudgetError):ledger.request("wrong-cap","a"*64,changed,policy=https_tests.HTTPSTests().policy(),token=None)
        self.assertEqual(ledger.summary()["admitted_requests"],0)
        body=b'{"choices":[],"usage":{"prompt_tokens":9000,"completion_tokens":9000,"total_tokens":18000}}'
        def handler(tls,_):tls.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "+str(len(body)).encode()+b"\r\n\r\n"+body)
        stack,thread,listener,received,errors=self.transport(handler)
        try:
            with stack:ledger.request("new-profile","a"*64,payload,policy=https_tests.HTTPSTests().policy(),token=None)
        finally:thread.join(3);listener.close()
        self.assertEqual(errors,[]);self.assertEqual(len(received),1)
        summary,rows=live.verify_originals(ledger.originals(),limits)
        self.assertEqual(rows[0]["payload"]["max_tokens"],32768)
        self.assertEqual(summary["reserved_estimated_nano_usd"],200_000_000)
        self.assertEqual(summary["observed_tokens"],18000)
        downgraded=copy.deepcopy(limits);downgraded["wire_version"]=wire.INDEXED_VERSION
        with self.assertRaises(budget.BudgetError):live.verify_originals(ledger.originals(),downgraded)

    def test_approval_reconstructs_all_children_and_exact_aggregates(self):
        current={k:v for k,v in self.limits["financial"].items() if k!="deadline_at"}
        second=copy.deepcopy(current);second["cycle_id"]=str(uuid.uuid4())
        approval={"version":"harness-paid-approval-v1","decision":"approved",
            "campaign_sha256":self.plan["campaign_sha256"],"control_plan_sha256":control.pin(self.plan),
            "credential_reference":"env:DEEPSEEK_API_KEY","human_authorization_reference":"synthetic authority fixture only",
            "approved_at":self.guard.clock["started_at"]-1,"start_before":self.guard.clock["deadline_at"],
            "financial_limits":[current,second],"total_requests":6,"estimated_cap_nano_usd":4_000_000_000}
        limits={**self.limits,"mode":"live","approval_sha256":control.pin(approval)}
        live.approval_check(approval,limits)
        for mutation in ("float_requests","float_cost","malformed_other","duplicate_cycle","float_child","bool_time","extra_child_field"):
            changed=copy.deepcopy(approval)
            if mutation=="float_requests":changed["total_requests"]=6.0
            elif mutation=="float_cost":changed["estimated_cap_nano_usd"]=4_000_000_000.0
            elif mutation=="malformed_other":changed["financial_limits"][1]={"max_requests":3,"estimated_cap_nano_usd":2_000_000_000}
            elif mutation=="duplicate_cycle":
                changed["financial_limits"][1]["cycle_id"]=current["cycle_id"]
                changed["financial_limits"][1]["request_policy"]["pricing_evidence"]="different child but same cycle"
            elif mutation=="float_child":changed["financial_limits"][1]["max_requests"]=3.0
            elif mutation=="bool_time":changed["approved_at"]=True
            else:changed["financial_limits"][1]["unapproved_extra"]=1
            with self.subTest(mutation=mutation),self.assertRaises(budget.BudgetError):
                live.approval_check(changed,{**limits,"approval_sha256":control.pin(changed)})

    def transport(self,handler):
        cert=self.cert;key=self.key;listener=self.listener
        server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_context.load_cert_chain(cert,key)
        received=[];errors=[];address=listener.getsockname()
        def serve():
            try:
                peer,_=listener.accept();peer.settimeout(2)
                with server_context.wrap_socket(peer,server_side=True) as tls:
                    raw=b""
                    while b"\r\n\r\n" not in raw:raw+=tls.recv(65536)
                    headers,_,body=raw.partition(b"\r\n\r\n")
                    length=int(next(s for s in headers.split(b"\r\n") if s.lower().startswith(b"content-length:")).split(b":")[1])
                    while len(body)<length:body+=tls.recv(65536)
                    received.append(body);handler(tls,body)
            except BaseException as exc:errors.append(type(exc).__name__)
        thread=threading.Thread(target=serve);thread.start()
        stack=__import__('contextlib').ExitStack()
        return stack,thread,listener,received,errors

    def test_one_request_positive_replay_and_tampered_transport_rejected(self):
        body=b'{"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}'
        def handler(tls,_):tls.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "+str(len(body)).encode()+b"\r\n\r\n"+body)
        stack,thread,listener,received,errors=self.transport(handler)
        try:
            with stack:self.ledger.request("a/query/1","a"*64,self.payload,policy=https_tests.HTTPSTests().policy(),token=None)
        finally:thread.join(3);listener.close()
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[]);self.assertEqual(len(received),1)
        observed=self.ledger.originals();summary,rows=live.verify_originals(observed,self.limits)
        self.assertEqual(summary["observed_tokens"],3);self.assertEqual(len(rows),1)
        with mock.patch.object(live.https.NumericHTTPS,"exchange",side_effect=AssertionError("resend")):
            self.assertEqual(self.ledger.reconcile("a/query/1")["status"],"response_retained")
            with self.assertRaises(budget.ReconcileRequired):self.ledger.request("a/query/1","a"*64,self.payload,policy=https_tests.HTTPSTests().policy(),token=None)
        outcome_name=next(n for n in observed["control"] if n.startswith("outcomes/"))
        for key,value in (("transport_closed",False),("response_complete",False),("cleanup_errors",["OSError"]),("observed_body_bytes",True),("body_truncated",True)):
            forged=copy.deepcopy(observed);item=json.loads(base64.b64decode(forged["control"][outcome_name]));item["outcome"][key]=value
            forged["control"][outcome_name]=base64.b64encode(json.dumps(item).encode()).decode()
            with self.subTest(key=key),self.assertRaises(budget.BudgetError):live.verify_originals(forged,self.limits)

    def test_partial_response_keeps_reservation_and_never_resends(self):
        def handler(tls,_):tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\nprefix")
        stack,thread,listener,received,errors=self.transport(handler)
        try:
            with stack,self.assertRaises(budget.ReconcileRequired):self.ledger.request("partial","a"*64,self.payload,policy=https_tests.HTTPSTests().policy(),token=None)
        finally:thread.join(3);listener.close()
        self.assertEqual(len(received),1);self.assertEqual(self.ledger.summary()["observed_tokens"],None)
        self.assertEqual(self.ledger.reconcile("partial")["status"],"indeterminate")
        with self.assertRaises(budget.ReconcileRequired):self.ledger.request("new","a"*64,self.payload,policy=https_tests.HTTPSTests().policy(),token=None)
        self.assertEqual(self.ledger.summary()["admitted_requests"],1)

    def test_cancel_after_reserve_never_opens_connection(self):
        def fault(name):
            if name=="after_reserve":control.cancel(self.root,control.pin(self.plan),"fixture")
        with mock.patch.object(live.https.NumericHTTPS,"connect",side_effect=AssertionError("send after cancel")):
            with self.assertRaises(control.AuthorityError):self.ledger.request("cancel","a"*64,self.payload,policy=https_tests.HTTPSTests().policy(),token=None,fault=fault)
        self.assertEqual(self.ledger.summary()["admitted_requests"],1)
        self.assertEqual(len(self.ledger.summary()["ambiguous"]),1)

    def test_original_outcome_recovers_accounting_without_resend(self):
        body=b'{"choices":[]}'
        def handler(tls,_):tls.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "+str(len(body)).encode()+b"\r\n\r\n"+body)
        def fault(name):
            if name=="after_outcome":raise KeyboardInterrupt()
        stack,thread,listener,received,errors=self.transport(handler)
        try:
            with stack,self.assertRaises(KeyboardInterrupt):self.ledger.request("recover","a"*64,self.payload,policy=https_tests.HTTPSTests().policy(),token=None,fault=fault)
        finally:thread.join(3);listener.close()
        self.assertEqual(self.ledger.summary()["observed_tokens"],None)
        self.assertEqual(len(self.ledger.summary()["ambiguous"]),1)
        with mock.patch.object(live.https.NumericHTTPS,"exchange",side_effect=AssertionError("recovery resend")):
            self.assertEqual(self.ledger.reconcile("recover")["status"],"response_retained")
        self.assertEqual(len(received),1);live.verify_originals(self.ledger.originals(),self.limits)

    def test_wire_preserves_thinking_without_rewriting_original(self):
        original={"choices":[{"message":{"role":"assistant","content":None,"reasoning_content":"SYNTHETIC_PRIVATE_REASONING","tool_calls":[{"id":"t"}]}}]}
        normalized=wire.normalize(original)
        self.assertIsNone(original["choices"][0]["message"]["content"])
        self.assertEqual(normalized["choices"][0]["message"]["content"],"")
        self.assertEqual(wire.messages([normalized["choices"][0]["message"]])[0]["reasoning_content"],"SYNTHETIC_PRIVATE_REASONING")
        del normalized["choices"][0]["message"]["reasoning_content"]
        with self.assertRaises(ValueError):wire.messages([normalized["choices"][0]["message"]])

    def test_budget_reconstruction_uses_pinned_historical_wire_version(self):
        legacy=copy.deepcopy(self.limits);legacy["wire_version"]=wire.LEGACY_VERSION
        self.assertEqual(live.validate(legacy),legacy)
        self.assertEqual(live.validate(self.limits)["wire_version"],wire.VERSION)
        unknown=copy.deepcopy(legacy);unknown["wire_version"]="unreviewed-wire"
        with self.assertRaises(ValueError):live.validate(unknown)

    def test_synthetic_mode_rejects_credentials_before_reservation(self):
        with mock.patch.object(live.https.NumericHTTPS,"connect",side_effect=AssertionError("network")):
            with self.assertRaisesRegex(budget.BudgetError,"credentials"):
                self.ledger.request("no-paid-authority","a"*64,self.payload,policy=https_tests.HTTPSTests().policy(),token="CANARY")
        self.assertEqual(self.ledger.summary()["admitted_requests"],0)
        with self.assertRaises(ValueError):live.https.synthetic_endpoint({**self.endpoint,"host":"api.deepseek.com"})

    def test_orphan_response_is_not_authority_for_mini(self):
        call=control.sandbox.digest(b"orphan")
        out={"http_status":200,"body_b64":base64.b64encode(b"{}").decode(),"response_complete":True,"transport_closed":True,
            "error":None,"transport_error":None,"cleanup_errors":[],"guardian_errors":[],"observed_body_bytes":2,"body_truncated":False}
        self.ledger._publish("outcomes/"+call+".json",{"call":call,"send_sha256":"f"*64,"outcome":out})
        with self.assertRaises(budget.BudgetError):self.ledger.reconcile("orphan")
        self.assertIsNone(self.ledger.financial.read("responses/"+call+".json",optional=True) if (self.ledger.financial.root/"responses").exists() else None)
        self.assertEqual(self.ledger.summary()["admitted_requests"],0)


if __name__=="__main__":unittest.main()
