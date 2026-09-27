"""Controller recovery primitives; data fixtures, never imported candidate code."""
import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import fleet_harness_executor as executor
import fleet_harness_sandbox as sandbox
import fleet_json
import fleet_safe_paths as safe
import fleet_harness_backend as backend_module
import fleet_harness_mini as mini
import fleet_herdr_scope as scope
import fleet_herdr_work_packet as work


class RecoveryTests(unittest.TestCase):
    def test_restore_checks_admission_predecessor_before_any_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();candidate=root/"candidate";candidate.mkdir();path=candidate/"report.py";path.write_bytes(b"A")
            spec={"schema_version":1,"editable_paths":["report.py"],"temporary_directories":[".tmp"],"max_entries":100,"max_bytes":1024*1024}
            initial=scope.capture(candidate,spec);path.write_bytes(b"B")
            admission={"generation":str(uuid.uuid4())};task={"fixture":"synthetic recovery"};deadline=9999999999
            action={"tool_call_id":"call1","command":"pwd"};bound={"admission_sha256":work.digest(admission),"tool_call_id":"call1"}
            suite=backend_module.checker.suite("D2");public=backend_module.checker.public_projection(suite)
            attempt=root/"backend";step=attempt/"steps/0001";command=step/"commands"/mini.digest(b"call1")
            with mock.patch.object(executor,"_run"):
                executor.execute(candidate,spec,action,command,binding=bound,deadline_at=deadline,public_checks=public)
            (command/"workspace/report.py").write_bytes(b"C")
            response={"choices":[{"message":{"role":"assistant","content":"inspect","tool_calls":[{"id":"call1","type":"function","function":{"name":"bash","arguments":"{\"command\":\"pwd\"}"}}]}}]}
            output={"tool_call_id":"call1","output":"/candidate\n","returncode":0}
            sandbox.publish(step,"response.json",response);sandbox.publish(step,"outputs.json",[output])
            sandbox.publish(attempt,"intent.json",{"admission":admission,"task":task,"input_snapshot":"input-pin"})
            class FakeCycle:
                def load(self):return {"attempts":[{"admission":admission,"input_snapshot":"input-pin"}]}
                def json(self,pin):return initial
            class Control:
                def __init__(self,*args,**kwargs):self.trajectory={"info":{"exit_status":""},"messages":[]}
                def start(self,task):pass
                def query(self,response):return mini.validate_batch(response,set())
                def observe(self,outputs):raise AssertionError("unbound output observed")
            backend=backend_module.LocalHarnessBackend.__new__(backend_module.LocalHarnessBackend)
            backend.cycle=FakeCycle();backend.controls={};backend.dependencies=root/"unused-dependencies"
            backend.contract={"deadline_at":deadline,"prepared":{"execution_envelope":{"candidate_repo":str(candidate)},"sources":{"scope":spec,
                "functional_tests":fleet_json.canonical_bytes(suite).decode(),
                "public_read":{"version":"public-read-manifest-v1","editable_paths":["report.py"],"read_only":{}}}}}
            backend._attempt=lambda a:attempt;backend._reconcile_cancel=lambda:None;backend._stopped=lambda:False;backend._cleanup=lambda *args:[]
            with mock.patch.object(backend_module.mini,"MiniControl",Control),mock.patch.object(executor,"_replace_owned") as writer:
                backend._drive(admission);writer.assert_not_called()
            self.assertEqual(path.read_bytes(),b"B")
            self.assertFalse((step/"native.json").exists())
            self.assertIn("no refresh",fleet_json.loads((attempt/"dependency.json").read_bytes())["reason"])

    def test_attach_originals_reconcile_complete_partial_extra_and_oversize(self):
        response={"id":"r","value":{"output":"ok","returncode":0},"error":None,"input_after":None}
        line=fleet_json.canonical_bytes(response)+b"\n"
        for mode,raw in (("complete",line),("partial",line[:12]),("extra",line+b"unbound"),
                         ("oversized",b"x"*(sandbox.MAX_OUTPUT+1)+b"\n")):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);owner=str(uuid.uuid4())
                sand=sandbox.Sandbox.__new__(sandbox.Sandbox);sand.store=root;sand.owner=owner;sand.process=None
                sand.record=lambda:{"resource_id":"fixture","owner":owner}
                sandbox.publish(root,"attach.json",{"owner":owner,"resource_id":"fixture"})
                sandbox.publish(root,"rpc-intents/0001.json",{"request_b64":base64.b64encode(b'{"id":"r"}').decode()})
                for name,content in (("stdout.raw",raw),("stderr.raw",b"")):
                    path=root/name;path.write_bytes(content);path.chmod(0o600)
                if mode=="oversized":
                    with self.assertRaises(ValueError):sand._reap()
                    self.assertFalse((root/"terminal-streams.json").exists())
                else:
                    tail,err=sand._reap();self.assertEqual(err,b"")
                    self.assertEqual(tail,b"" if mode=="complete" else b"unbound" if mode=="extra" else raw)
                    self.assertEqual((root/"rpc/0001.json").exists(),mode!="partial")
                    self.assertEqual(sand._reap(),(tail,err))
                    if mode=="complete":
                        (root/"stdout.raw").write_bytes(line.replace(b'"ok"',b'"NO"'))
                        with self.assertRaises(ValueError):sand._reap()

    def test_replacement_process_loss_partial_temp_and_after_rename_under_private_umask(self):
        script='''import os,sys
sys.path.insert(0,sys.argv[1])
import fleet_harness_executor as executor,fleet_safe_paths as safe
os.umask(0o077)
if sys.argv[3]=='partial':
    original=os.write
    def write(fd,raw):
        original(fd,raw[:3]);os.fsync(fd);os._exit(91)
    os.write=write
else:
    original=os.replace
    def replace(*args,**kwargs):
        original(*args,**kwargs);os._exit(91)
    os.replace=replace
with safe.RootedFS(sys.argv[2]) as fs:executor._replace_owned(fs,'data.txt',b'complete contents',token='a'*64)
'''
        for mode in ("partial","rename"):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);path=root/"data.txt";path.write_bytes(b"before");path.chmod(0o644)
                child=subprocess.run([sys.executable,"-I","-B","-c",script,str(Path(executor.__file__).parent),str(root),mode],capture_output=True,timeout=10)
                self.assertEqual(child.returncode,91,child.stderr)
                with safe.RootedFS(root) as fs:
                    executor._replace_owned(fs,"data.txt",b"complete contents",token="a"*64,already=mode=="rename")
                self.assertEqual(path.read_bytes(),b"complete contents");self.assertEqual(path.stat().st_mode&0o777,0o644)
                self.assertEqual([p.name for p in root.iterdir()],["data.txt"])


if __name__=="__main__":unittest.main()
