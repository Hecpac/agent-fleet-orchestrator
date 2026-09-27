import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
import copy
import subprocess
import shutil
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_harness_executor as executor
import fleet_harness_sandbox as sandbox
import fleet_json
import fleet_harness_read_scope as read_scope


class PublicReadTests(unittest.TestCase):
    def test_reserved_checks_cannot_replace_admitted_public_context_on_resume(self):
        for retained in (None,{"public_cases":[],"reserved":[{"expected":"SYNTHETIC_PRIVATE_CANARY"}]}):
            with self.subTest(retained=retained),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);candidate=root/"candidate";candidate.mkdir();(candidate/"report.py").write_bytes(b"A")
                store=root/"command";deadline=time.time()+60
                spec={"schema_version":1,"editable_paths":["report.py"],"temporary_directories":[],"max_entries":100,"max_bytes":1024*1024}
                action={"command":"true","tool_call_id":"t"};public={"public_cases":[]}
                with mock.patch.object(executor,"_run"):
                    executor.execute(candidate,spec,action,store,binding={},deadline_at=deadline,public_checks=retained)
                with mock.patch.object(executor.sand,"Sandbox") as resource,mock.patch.object(executor,"_replace_owned") as writer:
                    with self.assertRaisesRegex(read_scope.InputBindingError,"admitted public projection"):
                        executor.execute(candidate,spec,action,store,binding={},deadline_at=deadline,public_checks=public)
                    with self.assertRaisesRegex(read_scope.InputBindingError,"admitted public projection"):
                        executor.reconcile(candidate,store,binding={},expected_before={"report.py":{"sha256":sandbox.digest(b"A"),"bytes":1}},public_checks=public)
                    resource.assert_not_called();writer.assert_not_called()

    def test_orphan_preparation_cas_then_pending_intent_reuses_one_owner(self):
        script='''import os,sys
sys.path.insert(0,sys.argv[1])
import fleet_harness_executor as e,fleet_safe_paths as s
if sys.argv[5]=='orphan':
    original=e.sand.publish
    def publish(root,name,value):
        if name=='preparation.json':os._exit(91)
        return original(root,name,value)
    e.sand.publish=publish
else:
    original=s.RootedFS.atomic_write
    def write(self,name,*args,**kwargs):
        if str(name)=='intent.json':s._atomic_write_checkpoint=lambda point:os._exit(92) if point=='after_atomic_pending_fsync' else None
        return original(self,name,*args,**kwargs)
    s.RootedFS.atomic_write=write
e._run=lambda *args:None
spec={'schema_version':1,'editable_paths':['report.py'],'temporary_directories':[],'max_entries':100,'max_bytes':1048576}
e.execute(sys.argv[2],spec,{'command':'true','tool_call_id':'t'},sys.argv[3],binding={},deadline_at=float(sys.argv[4]))
'''
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);candidate=root/"candidate";candidate.mkdir();(candidate/"report.py").write_bytes(b"original bytes")
            store=root/"command";deadline=time.time()+60
            for phase,code in (("orphan",91),("pending",92)):
                child=subprocess.run([sys.executable,"-I","-B","-c",script,str(Path(executor.__file__).parent),str(candidate),str(store),str(deadline),phase],capture_output=True,timeout=10)
                self.assertEqual(child.returncode,code,child.stderr)
                owners=[p.name for p in (store/"missions").iterdir()];self.assertEqual(len(owners),1)
                if phase=="orphan":first=owners;self.assertFalse((store/"preparation.json").exists())
                else:self.assertEqual(owners,first)
            spec={"schema_version":1,"editable_paths":["report.py"],"temporary_directories":[],"max_entries":100,"max_bytes":1024*1024}
            with mock.patch.object(executor,"_run") as run:
                executor.execute(candidate,spec,{"command":"true","tool_call_id":"t"},store,binding={},deadline_at=deadline)
                self.assertEqual(run.call_count,1)
            self.assertEqual(fleet_json.loads((store/"intent.json").read_bytes())["owner"],first[0])
            self.assertFalse(list(store.glob(".fleet-atomic-*.tmp")))

    def test_bridge_extra_or_changed_guest_rejected_before_any_resource(self):
        for attack in ("extra","git","guest"):
            with self.subTest(attack=attack),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);candidate=root/"candidate";candidate.mkdir();(candidate/"report.py").write_bytes(b"original")
                store=root/"command";deadline=time.time()+60
                spec={"schema_version":1,"editable_paths":["report.py"],"temporary_directories":[],"max_entries":100,"max_bytes":1024*1024}
                action={"command":"true","tool_call_id":"t"}
                with mock.patch.object(executor,"_run"):
                    executor.execute(candidate,spec,action,store,binding={},deadline_at=deadline)
                if attack=="extra":(store/"bridge/undeclared-canary.txt").write_text("synthetic")
                elif attack=="git":(store/"bridge/.git").symlink_to("/nonexistent-synthetic-path")
                else:(store/"bridge"/executor.GUEST.name).write_bytes(b"changed guest")
                with mock.patch.object(executor.sand,"Sandbox") as resource:
                    with self.assertRaises(read_scope.InputBindingError):
                        executor.execute(candidate,spec,action,store,binding={},deadline_at=deadline)
                    resource.assert_not_called()

    def test_predecessor_mismatch_has_no_staging_and_all_recovery_checks_exact_types(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);candidate=root/"candidate";candidate.mkdir();path=candidate/"report.py";path.write_bytes(b"B")
            spec={"schema_version":1,"editable_paths":["report.py"],"temporary_directories":[],"max_entries":100,"max_bytes":1024*1024}
            expected={"report.py":{"sha256":sandbox.digest(b"A"),"bytes":1}}
            store=root/"command";action={"command":"true","tool_call_id":"t"};deadline=time.time()+60
            with mock.patch.object(executor,"_run") as run:
                with self.assertRaises(read_scope.InputBindingError):
                    executor.execute(candidate,spec,action,store,binding={},deadline_at=deadline,expected_before=expected)
                run.assert_not_called();self.assertFalse((store/"workspace").exists());self.assertFalse((store/"bridge").exists())
                path.write_bytes(b"A")
                executor.execute(candidate,spec,action,store,binding={},deadline_at=deadline,expected_before=expected)
                self.assertEqual(run.call_count,1)
            bad=copy.deepcopy(expected);bad["report.py"]["bytes"]=True
            with mock.patch.object(executor,"_run") as run,mock.patch.object(executor,"_replace_owned") as writer:
                with self.assertRaises(read_scope.InputBindingError):
                    executor.execute(candidate,spec,action,store,binding={},deadline_at=deadline,expected_before=bad)
                with self.assertRaises(read_scope.InputBindingError):executor.reconcile(candidate,store,binding={},expected_before=bad)
                foreign=copy.deepcopy(expected);foreign["report.py"]["sha256"]="f"*64
                with self.assertRaises(read_scope.InputBindingError):executor.reconcile(candidate,store,binding={},expected_before=foreign)
                run.assert_not_called();writer.assert_not_called()

    def test_real_process_loss_during_preintent_staging_recovers_same_preparation(self):
        script='''import os,sys
sys.path.insert(0,sys.argv[1])
import fleet_harness_executor as e,fleet_safe_paths as s
original=s.RootedFS.atomic_write
def write(self,name,*args,**kwargs):
    if str(name)=='workspace/report.py':s._atomic_write_checkpoint=lambda point:os._exit(91) if point==sys.argv[5] else None
    return original(self,name,*args,**kwargs)
s.RootedFS.atomic_write=write
e._run=lambda *args:None
spec={'schema_version':1,'editable_paths':['report.py'],'temporary_directories':['.tmp'],'max_entries':100,'max_bytes':1048576}
e.execute(sys.argv[2],spec,{'command':'true','tool_call_id':'t'},sys.argv[3],binding={},deadline_at=float(sys.argv[4]))
'''
        for phase in ("after_atomic_partial_write","after_atomic_pending_fsync","after_atomic_rename"):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);candidate=root/"candidate";candidate.mkdir();(candidate/"report.py").write_bytes(b"original bytes")
                store=root/"command";deadline=time.time()+60
                child=subprocess.run([sys.executable,"-I","-B","-c",script,str(Path(executor.__file__).parent),str(candidate),str(store),str(deadline),phase],capture_output=True,timeout=10)
                self.assertEqual(child.returncode,91,child.stderr)
                retained=(store/"preparation.json").read_bytes();self.assertFalse((store/"intent.json").exists())
                spec={"schema_version":1,"editable_paths":["report.py"],"temporary_directories":[".tmp"],"max_entries":100,"max_bytes":1024*1024}
                with mock.patch.object(executor,"_run") as run:
                    executor.execute(candidate,spec,{"command":"true","tool_call_id":"t"},store,binding={},deadline_at=deadline)
                    self.assertEqual(run.call_count,1)
                self.assertEqual((store/"preparation.json").read_bytes(),retained)
                self.assertEqual((store/"workspace/report.py").read_bytes(),b"original bytes")
                self.assertFalse(list((store/"workspace").rglob(".fleet-atomic-*.tmp")))

    def test_ignored_secrets_are_not_projected_and_recovery_rechecks_exact_contents(self):
        for attack in ("extra", "git", "readonly", "editable", "symlink", "foreign_manifest"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);candidate=root/"candidate";candidate.mkdir()
                for name,raw in (("report.py","value=1"),("SPEC.md","public spec"),(".env","SYNTHETIC_SECRET")):
                    (candidate/name).write_text(raw)
                (candidate/".env").chmod(0o600)
                (candidate/".aws").mkdir();(candidate/".aws/credentials").write_text("SYNTHETIC_CREDENTIAL")
                (candidate/".aws/credentials").chmod(0o600)
                spec={"schema_version":1,"editable_paths":["report.py"],"temporary_directories":[".tmp"],"max_entries":100,"max_bytes":1024*1024}
                manifest=read_scope.prepare(candidate,spec,["SPEC.md"])
                deadline=time.time()+60;action={"command":"true","tool_call_id":"t"};store=root/"command"
                with mock.patch.object(executor,"_run",return_value=None):
                    executor.execute(candidate,spec,action,store,binding={"a":1},deadline_at=deadline,public_read=manifest)
                workspace=store/"workspace"
                self.assertEqual({p.name for p in workspace.iterdir()},{"report.py","SPEC.md",".tmp"})
                if attack=="extra":(workspace/".env").write_text("injected")
                elif attack=="git":(workspace/".git").symlink_to("/nonexistent-synthetic-path")
                elif attack=="readonly":(workspace/"SPEC.md").write_text("changed")
                elif attack=="editable":(workspace/"report.py").write_text("changed")
                elif attack=="symlink":(workspace/"extra").symlink_to(candidate/".env")
                else:manifest["read_only"]={}
                with mock.patch.object(executor.sand,"Sandbox") as resource:
                    with self.assertRaises(ValueError):
                        executor.execute(candidate,spec,action,store,binding={"a":1},deadline_at=deadline,public_read=manifest)
                    resource.assert_not_called()

    def test_readonly_pin_is_not_renewed_and_manifest_cannot_expand_write_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);candidate=root/"candidate";candidate.mkdir()
            (candidate/"report.py").write_text("v=1");(candidate/"SPEC.md").write_text("original")
            spec={"schema_version":1,"editable_paths":["report.py"],"temporary_directories":[],"max_entries":100,"max_bytes":1024*1024}
            manifest=read_scope.prepare(candidate,spec,["SPEC.md"])
            (candidate/"SPEC.md").write_text("changed")
            with mock.patch.object(executor.sand,"Sandbox") as resource:
                with self.assertRaisesRegex(ValueError,"changed since creation"):
                    executor.execute(candidate,spec,{"command":"true","tool_call_id":"t"},root/"command",
                        binding={},deadline_at=time.time()+60,public_read=manifest)
                resource.assert_not_called()
            changed=copy.deepcopy(manifest);changed["editable_paths"].append("SPEC.md")
            with self.assertRaises(ValueError):read_scope.validate(changed,spec)
            changed=copy.deepcopy(manifest);changed["read_only"]["report.py/child"]="a"*64
            with self.assertRaises(ValueError):read_scope.validate(changed,spec)


@unittest.skipUnless(os.environ.get("FLEET_HARNESS_LOCAL_TESTS") == "1", "explicit owned local test lane")
class ExecutorTests(unittest.TestCase):
    def guest_probe(self,command,seconds):
        with tempfile.TemporaryDirectory(prefix="fleet-executor-exit-") as temporary:
            root=Path(temporary);candidate=root/"candidate";candidate.mkdir();bridge=root/"bridge";bridge.mkdir()
            shutil.copyfile(executor.GUEST,bridge/executor.GUEST.name)
            resource=sandbox.Sandbox(root/"resource",owner=str(uuid.uuid4()))
            try:
                resource.create(candidate=candidate,guest=bridge,
                    argv=["/usr/local/bin/python3","-I","-S","-B","/bridge/"+executor.GUEST.name],processes=32)
                resource.start()
                result=resource.rpc({"id":"exit-probe","command":command,"seconds":seconds},timeout=seconds+2)
                self.assertIsNone(result["error"])
                return result["value"]
            finally:self.assertTrue(resource.cleanup()["resources_clean"])

    def test_eof_before_child_exit_waits_inside_original_deadline(self):
        result=self.guest_probe("exec 1>&- 2>&-; sleep .15; exit 0",.8)
        self.assertEqual(result,{"output":"","returncode":0,"timed_out":False,"truncated":False})

    def test_deadline_requires_child_exit_and_stream_eof(self):
        for command in ("sleep 10","exec 1>&- 2>&-; sleep 10","sleep 10 & exit 0"):
            with self.subTest(command=command):
                result=self.guest_probe(command,.15)
                self.assertTrue(result["timed_out"]);self.assertFalse(result["truncated"])
                if command.endswith("exit 0"):self.assertEqual(result["returncode"],0)

    def test_output_limit_is_not_a_premature_timeout(self):
        result=self.guest_probe("python3 -B -c 'import sys,time;sys.stdout.write(\"x\"*32769);sys.stdout.flush();time.sleep(10)'",1)
        self.assertTrue(result["truncated"]);self.assertFalse(result["timed_out"])
        self.assertEqual(len(result["output"]),32768)

    def test_timeout_report_fits_inside_same_task_deadline(self):
        with tempfile.TemporaryDirectory(prefix="fleet-executor-budget-") as temporary:
            root=Path(temporary);candidate=root/"candidate";candidate.mkdir();(candidate/"report.py").write_text("value = 1\n")
            spec={"schema_version":1,"editable_paths":["report.py"],"temporary_directories":[".tmp"],"max_entries":100,"max_bytes":1024*1024}
            deadline=time.time()+6
            result=executor.execute(candidate,spec,{"tool_call_id":"bounded-timeout","command":"sleep 10"},root/"command",binding={"admission":"a"},deadline_at=deadline)
            self.assertTrue(result["timed_out"]);self.assertFalse(result["truncated"])
            self.assertLess(time.time(),deadline)
            self.assertEqual((candidate/"report.py").read_text(),"value = 1\n")

    def test_scope_credentials_network_cache_and_idempotent_publication(self):
        with tempfile.TemporaryDirectory(prefix="fleet-executor-") as temporary:
            root = Path(temporary); candidate = root / "candidate"; candidate.mkdir()
            (candidate / "report.py").write_text("value = 1\n")
            (candidate / ".gitignore").write_text("*.pyc\nignored.cache\n")
            (candidate / "preserve.txt").write_text("preserved")
            (candidate/".env").write_text("SYNTHETIC_FILE_SECRET");(candidate/".env").chmod(0o600)
            (candidate/".aws").mkdir();(candidate/".aws/credentials").write_text("SYNTHETIC_FILE_CREDENTIAL")
            (candidate/".aws/credentials").chmod(0o600)
            spec = {"schema_version": 1, "editable_paths": ["report.py"], "temporary_directories": [".tmp"],
                    "max_entries": 100, "max_bytes": 1024 * 1024}
            manifest=read_scope.prepare(candidate,spec,["preserve.txt"])
            program = '''import json, os, pathlib, py_compile, socket
checks = {}
for path in ["/candidate/ignored.cache", "/candidate/report.pyc", "/candidate/preserve.txt", "/bridge/probe", "/etc/probe"]:
    try:
        pathlib.Path(path).write_text("OUT-OF-SCOPE")
        checks[path] = "WRITTEN"
    except OSError: checks[path] = "denied"
for path in ["/var/run/docker.sock", "/run/host-services/ssh-auth.sock"]:
    checks[path] = pathlib.Path(path).exists()
checks["secret"] = os.environ.get("DEEPSEEK_API_KEY")
checks["public_readonly"] = pathlib.Path("/candidate/preserve.txt").read_text()
checks["file_secrets_visible"] = any(pathlib.Path(p).exists() for p in ["/candidate/.env", "/candidate/.aws/credentials", "/candidate/.gitignore"])
try:
    socket.create_connection(("198.51.100.1", 443), timeout=.2)
    checks["network"] = "connected"
except OSError: checks["network"] = "denied"
try:
    py_compile.compile("report.py", cfile="report.pyc", doraise=True)
    checks["explicit_compile"] = "WRITTEN"
except Exception: checks["explicit_compile"] = "denied"
py_compile.compile("report.py", doraise=True)
checks["cache"] = bool(list(pathlib.Path("/tmp/pycache").rglob("*.pyc")))
pathlib.Path("/candidate/.tmp/cache").write_text("allowed temporary")
pathlib.Path("report.py").write_text("value = 2\\n")
print(json.dumps(checks))
'''
            action = {"tool_call_id": "canaries", "command": "python3 -B - <<'PY'\n" + program + "PY\n"}
            before = dict(os.environ)
            os.environ["DEEPSEEK_API_KEY"] = "SYNTHETIC-CANARY-NOT-A-CREDENTIAL"
            try:
                result = executor.execute(candidate, spec, action, root / "command", binding={"admission": "a"}, deadline_at=time.time()+40,public_read=manifest)
            finally:
                if "DEEPSEEK_API_KEY" in before: os.environ["DEEPSEEK_API_KEY"] = before["DEEPSEEK_API_KEY"]
                else: os.environ.pop("DEEPSEEK_API_KEY", None)
            self.assertEqual(result["returncode"], 0, result)
            checks = fleet_json.loads(result["output"])
            self.assertEqual(checks["network"], "denied")
            self.assertIsNone(checks["secret"])
            self.assertFalse(checks["file_secrets_visible"])
            self.assertEqual(checks["public_readonly"],"preserved")
            self.assertEqual(checks["explicit_compile"], "denied")
            self.assertTrue(checks["cache"])
            for path, value in checks.items():
                if path.startswith("/candidate") or path in {"/bridge/probe", "/etc/probe"}: self.assertEqual(value, "denied")
                if path.endswith(".sock"): self.assertFalse(value)
            self.assertEqual((candidate / "report.py").read_text(), "value = 2\n")
            self.assertEqual((candidate / "preserve.txt").read_text(), "preserved")
            self.assertFalse((candidate / "report.pyc").exists())
            expected={"report.py":{"sha256":sandbox.digest(b"value = 1\n"),"bytes":len(b"value = 1\n")}}
            self.assertEqual(executor.reconcile(candidate, root / "command", binding={"admission": "a"},public_read=manifest,expected_before=expected), result)
            with self.assertRaises(ValueError): executor.reconcile(candidate, root / "command", binding={"admission": "other"},expected_before=expected)
            record = fleet_json.loads((root / "command/resource/resource.json").read_bytes())
            resource = sandbox.Sandbox(root / "command/resource", owner=record["owner"])
            self.assertTrue(resource.cleanup()["resources_clean"])
            self.assertTrue(resource.cleanup()["resources_clean"])
            # Retained publication/result cannot bypass the projection check.
            (root/"command/workspace/.env").write_text("SYNTHETIC_INJECTED_SECRET")
            with self.assertRaisesRegex(ValueError,"undeclared"):
                executor.reconcile(candidate,root/"command",binding={"admission":"a"},public_read=manifest,expected_before=expected)


if __name__ == "__main__": unittest.main()
