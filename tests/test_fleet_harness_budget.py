import copy
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_harness_budget as budget
import fleet_safe_paths as safe


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fleet-budget-")
        self.root = Path(self.temp.name)
        self.spec = budget.contract(cycle_id=str(uuid.uuid4()), deadline_at=time.time()+30)
        self.ledger = budget.Ledger(self.root / "ledger", self.spec)
        self.payload = {"model": "deepseek-flash", "max_tokens": 20, "messages": [{"role": "user", "content": "synthetic"}]}
        self.requests = []
        self.http_status = 200
        self.truncate = False
        self.drip = False
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append(json.loads(raw))
                data = json.dumps({"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}).encode()
                self.send_response(outer.http_status)
                self.send_header("Content-Length", str(len(data) + (99 if outer.truncate else 0)))
                self.end_headers()
                try:
                    if outer.drip:
                        for char in data:
                            self.wfile.write(bytes([char])); self.wfile.flush(); time.sleep(0.05)
                    else:
                        self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError): pass
                self.close_connection = True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/chat/completions"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        self.temp.cleanup()

    def call(self, logical="call-1", **kw):
        return self.ledger.request(logical, "a"*64, self.payload, endpoint=self.endpoint, synthetic=True, **kw)

    def test_single_wire_request_and_no_implicit_retry_on_provider_errors(self):
        self.http_status = 402
        self.assertEqual(self.call()["http_status"], 402)
        with self.assertRaises(budget.BudgetError): self.call("call-2")
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(self.ledger.summary()["billed_cost_usd"])

    def test_partial_response_stays_ambiguous_and_blocks_next_call(self):
        self.truncate = True
        with self.assertRaises(budget.ReconcileRequired): self.call()
        self.assertEqual(self.ledger.reconcile("call-1")["status"], "indeterminate")
        with self.assertRaises(budget.ReconcileRequired): self.call("call-2")
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(self.ledger.summary()["observed_tokens"])

    def test_after_reservation_crash_and_recovery_never_resend_or_renew(self):
        def crash(point):
            if point == "after_reserve": raise SystemExit("controlled interruption")
        with self.assertRaises(SystemExit): self.call(fault=crash)
        self.ledger = budget.Ledger(self.root / "ledger", self.spec)
        with self.assertRaises(budget.ReconcileRequired): self.call()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.ledger.summary()["admitted_requests"], 1)
        self.spec["deadline_at"] += 100
        with self.assertRaises(safe.SafePathError): budget.Ledger(self.root / "ledger", self.spec)

    def test_real_process_loss_at_fsynced_pending_reservation_and_revocation(self):
        script = '''import json,os,sys
sys.path.insert(0,sys.argv[1])
import fleet_harness_budget as b, fleet_safe_paths as safe
root,limits,mode=sys.argv[2],json.loads(sys.argv[3]),sys.argv[4]
ledger=b.Ledger(root,limits)
original=b.publish
def publish(root,name,value):
    if (mode=='reserve' and name.startswith('calls/')) or (mode=='revoke' and name=='revoked.json'):
        safe._atomic_write_checkpoint=lambda point: os._exit(91) if point=='after_atomic_pending_fsync' else None
    return original(root,name,value)
b.publish=publish
if mode=='reserve': ledger.reserve('crash','a'*64,{'model':'deepseek-flash','max_tokens':1,'messages':[]})
else: ledger.revoke('cancel')
os._exit(92)
'''
        for mode in ("reserve", "revoke"):
            with self.subTest(mode=mode):
                root=self.root/mode
                original=budget.Ledger(root,self.spec)
                child=subprocess.run([sys.executable,"-I","-B","-c",script,str(Path(budget.__file__).parent),str(root),json.dumps(self.spec),mode],capture_output=True,timeout=10)
                self.assertEqual(child.returncode,91,child.stderr)
                restored=budget.Ledger(root,self.spec)
                payload={"model":"deepseek-flash","max_tokens":1,"messages":[]}
                if mode=="reserve":
                    self.assertEqual(restored.summary()["admitted_requests"],1)
                    with self.assertRaises(budget.ReconcileRequired):restored.reserve("crash","a"*64,payload)
                else:
                    for ledger in (original,restored):
                        with self.assertRaises(budget.BudgetError):ledger.reserve("after-cancel","a"*64,payload)
                    self.assertTrue((root/"revoked.json").exists())
        self.assertEqual(self.requests,[])

    def test_absolute_deadline_interrupts_slow_response_headers(self):
        client,peer=socket.socketpair()
        original=budget.http.client.HTTPConnection
        class Connection(original):
            def connect(self):self.sock=client;self.sock.settimeout(self.timeout)
        def send():
            try:
                peer.sendall(b"HTTP/1.1 200 OK\r\n")
                for i in range(20):
                    time.sleep(.035);peer.sendall((f"X-{i}: x\r\n").encode())
                peer.sendall(b"Content-Length: 2\r\n\r\n{}")
            except OSError:pass
            finally:peer.close()
        thread=threading.Thread(target=send);thread.start()
        start=time.monotonic()
        ledger=budget.Ledger(self.root/"headers",budget.contract(cycle_id=str(uuid.uuid4()),deadline_at=time.time()+.15))
        try:
            with mock.patch.object(budget.http.client,"HTTPConnection",Connection):
                with self.assertRaises(budget.ReconcileRequired):
                    ledger.request("headers","a"*64,self.payload,endpoint=self.endpoint,synthetic=True)
            self.assertLess(time.monotonic()-start,.45)
            self.assertEqual(ledger.reconcile("headers")["status"],"indeterminate")
        finally:client.close();thread.join(2)

    def test_mutable_arguments_cannot_change_frozen_payload_or_limits(self):
        original = copy.deepcopy(self.payload)
        def mutate(point):
            if point == "after_reserve": self.payload["model"] = "foreign"
        self.call(fault=mutate)
        self.assertEqual(self.requests, [original])
        self.spec["deadline_at"] += 1000
        self.assertNotEqual(self.ledger.limits["deadline_at"], self.spec["deadline_at"])

    def test_oversized_output_cap_rejected_before_wire(self):
        self.payload["max_tokens"] = 500000
        with self.assertRaises(budget.BudgetError): self.call()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.ledger.summary()["admitted_requests"], 0)

    def test_revoke_between_reserve_and_send_prevents_send(self):
        def revoke(point):
            if point == "after_reserve": self.ledger.revoke("cancel")
        with self.assertRaises(budget.ReconcileRequired): self.call(fault=revoke)
        self.assertEqual(self.requests, [])

    def test_dripping_response_cannot_renew_total_deadline(self):
        limits = budget.contract(cycle_id=str(uuid.uuid4()), deadline_at=time.time()+0.3)
        self.ledger = budget.Ledger(self.root / "short", limits)
        self.drip = True
        started = time.monotonic()
        with self.assertRaises(budget.ReconcileRequired): self.call()
        self.assertLess(time.monotonic()-started, 1.5)
        self.assertIsNone(self.ledger.summary()["observed_tokens"])

    def test_live_lane_requires_new_authorization_not_key_presence(self):
        with self.assertRaises(budget.BudgetError):
            self.ledger.request("live", "a"*64, self.payload, endpoint="https://api.deepseek.com/chat/completions", token="synthetic-key")
        self.assertEqual(self.ledger.summary()["admitted_requests"], 0)

    def test_live_dictionary_cannot_substitute_for_controller_admission(self):
        authorization={"budget_sha256":budget.digest(budget.fleet_json.canonical_bytes(self.spec)),
            "scope":"new-paid-harness-pilot","user_approval_reference":True}
        for endpoint in ("https://api.deepseek.com/chat/completions","https://api.deepseek.com/unapproved/path","https://api.deepseek.com:8443/chat/completions"):
            with self.subTest(endpoint=endpoint),mock.patch.object(budget.http.client,"HTTPSConnection") as connection:
                with self.assertRaises(budget.BudgetError):
                    self.ledger.request("live", "a"*64,self.payload,endpoint=endpoint,token="synthetic-key",authorization=authorization)
                connection.assert_not_called()
        self.assertEqual(self.ledger.summary()["admitted_requests"],0)

    def test_originals_reject_revocation_underfunding_and_observed_overruns(self):
        with self.assertRaises(budget.BudgetError):
            budget.contract(cycle_id=str(uuid.uuid4()),deadline_at=time.time()+30,reserve_nano_usd_per_request=20_000_000)
        self.call()
        originals=self.ledger.originals()
        self.assertEqual(budget.verify_originals(originals,self.spec)[0],self.ledger.summary())
        for mode in ("http","usage","revoked"):
            damaged=copy.deepcopy(originals)
            key=next(k for k in damaged if k.startswith("responses/"));value=json.loads(base64.b64decode(damaged[key]))
            if mode=="http":value["http_status"]=429
            if mode=="usage":
                value["usage"]={"prompt_tokens":100000,"completion_tokens":1,"total_tokens":100001}
                value["body_b64"]=base64.b64encode(json.dumps({"usage":value["usage"]}).encode()).decode()
            damaged[key]=base64.b64encode(json.dumps(value).encode()).decode()
            if mode=="revoked":damaged["revoked.json"]=base64.b64encode(b'{}').decode()
            with self.subTest(mode=mode),self.assertRaises(budget.BudgetError):budget.verify_originals(damaged,self.spec)


if __name__ == "__main__": unittest.main()
