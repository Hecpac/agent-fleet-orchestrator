"""Executable serial Herdr CONTROL pilot. Paid admission is explicit operator input.

prepare never registers a plugin or starts a provider. authorize is an operator
action requiring prior human permission for the exact printed scope; no task,
model response or receipt calls it. Candidate resources cannot access ingress.
"""
import argparse
from contextlib import contextmanager
import copy
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import uuid

import fleet_harness_campaign as fixtures
import fleet_harness_control as control
import fleet_harness_live_contract as contract
import fleet_harness_live_budget as budget
import fleet_harness_live_backend as backend_module
import fleet_harness_mini as mini
import fleet_harness_sandbox as sandbox
import fleet_herdr_owner_cycle as cycle
import fleet_herdr_work_packet as work
import fleet_json
import fleet_safe_paths as safe

VERSION="harness-control-pilot-v1"
PLUGIN="fleet.harness-control-e4"


def read(root,name,optional=False):return control.read(root,name,optional=optional)


def publish(root,authority,name,value):control.publish(Path(root),authority,name,value)


def recover(root,authority,*,allow_partial=False):
    with safe.RootedFS(root,root_mode=0o700) as fs:
        with fs.exclusive_lock(".control-send.lock",directory_modes=()):
            partial=control.recover(Path(root),authority)
    if partial and not allow_partial:raise ValueError("dependency: incomplete CONTROL publication preserved; effects remain denied")
    return partial



def _darwin_executable():
    """Absolute path of this process's executing image (libproc proc_pidpath)."""
    import ctypes
    library=ctypes.CDLL("/usr/lib/libproc.dylib",use_errno=True)
    library.proc_pidpath.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32]
    path=ctypes.create_string_buffer(4096)  # PROC_PIDPATHINFO_MAXSIZE
    if library.proc_pidpath(os.getpid(),path,len(path))<=0:
        raise ValueError("exact CONTROL interpreter identity unavailable")
    return os.fsdecode(path.value)

def prepare(root,*,mode,pricing_evidence=None,credential_source="env",requests_per_task=6,output_profile="legacy-8k"):
    root=Path(root).resolve()
    if mode not in {"synthetic_tls","live"}:raise ValueError("unknown pilot mode")
    if mode=="live" and pricing_evidence is None:raise ValueError("live preparation needs retained current pricing observation")
    if credential_source not in {"env","file"} or (mode!="live" and credential_source!="env"):
        raise ValueError("credential file ingress is for explicitly prepared live mode only")
    original=fixtures.prepare(root,requests_per_task=requests_per_task,profile=output_profile);base=read(root,"plan.json")
    totals=fixtures.validate_budget_plan(base)
    pricing=None
    if pricing_evidence is not None:
        pricing=fleet_json.loads(Path(pricing_evidence).read_bytes())
        sandbox.publish(root,"pricing-observation.json",pricing)
    # macOS framework python's bin launcher execs a different Mach-O binary.
    # Freeze the executable actually observed for this trusted preparation
    # process, so the Herdr argv check does not confuse launcher with runtime.
    # proc_pidpath reports that image's absolute path; `ps -o comm=` reported
    # the invoked name instead (e.g. a bare python3.12 found through PATH).
    if sys.platform=="darwin":interpreter=str(Path(_darwin_executable()).resolve(strict=True))
    elif sys.platform.startswith("linux"):interpreter=str(Path("/proc/self/exe").resolve(strict=True))
    else:raise ValueError("exact CONTROL interpreter identity unsupported")
    script=str(Path(__file__).resolve())
    plan={"version":VERSION,"base_plan_sha256":original["plan_sha256"],"mode":mode,"root":str(root),
        "plugin_id":PLUGIN,"herdr":str(Path(shutil.which("herdr") or "/unavailable/herdr").resolve()),
        "python":interpreter,"script":script,"cwd":str(fixtures.REPO),**totals,
        "pricing_sha256":control.pin(pricing) if pricing is not None else None,
        "cancel_scope":"entire serial campaign; no admission of later tasks",
        "credential_reference":"file:"+str(root/"provider-key") if credential_source=="file" else "env:DEEPSEEK_API_KEY","promotion":"disabled",
        "identity":"requested Mini2.4.6/deepseek-flash/max; observed model and billed cost require live evidence"}
    if output_profile!="legacy-8k":plan["output_profile"]=output_profile
    pin=control.pin(plan);sandbox.publish(root,"control-campaign.json",plan)
    command=[interpreter,"-B",script,"supervise",str(root),"--plan-sha256",pin]
    manifest=root/"owned-control-plugin.toml"
    manifest.write_text('id = "'+PLUGIN+'"\nname = "Fleet owned CONTROL"\nversion = "0.2.0"\nmin_herdr_version = "0.9.0"\nplatforms = ["macos"]\n\n[[panes]]\nid = "supervisor"\ntitle = "Fleet owned CONTROL"\nplacement = "tab"\ncommand = '+fleet_json.canonical_bytes(command).decode()+'\n')
    authority=control.prepare(root,campaign_sha256=pin,seconds=plan["total_seconds"],launch={"herdr":plan["herdr"],"plugin_id":PLUGIN,
        "entrypoint":"supervisor","manifest":str(manifest),"manifest_sha256":sandbox.digest(manifest.read_bytes()),"command":command,"cwd":plan["cwd"]})
    if mode=="synthetic_tls":
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-keyout",str(root/"synthetic-key.pem"),"-out",str(root/"synthetic-cert.pem"),
            "-subj","/CN=api.deepseek.com","-addext","subjectAltName=DNS:api.deepseek.com"],capture_output=True,check=True,timeout=10)
        (root/"synthetic-key.pem").chmod(0o600)
    template={"version":"harness-paid-approval-v1","decision":"PENDING","campaign_sha256":pin,"control_plan_sha256":control.pin(authority),
        "credential_reference":plan["credential_reference"],"human_authorization_reference":None,"approved_at":None,"start_before":None,
        "financial_limits":[financial_template(base,task,plan) for task in base["tasks"]],
        "total_requests":plan["total_requests"],"estimated_cap_nano_usd":plan["estimated_cap_nano_usd"]}
    sandbox.publish(root,"approval-template.json",template)
    return {"plan_sha256":pin,"control_plan_sha256":control.pin(authority),"root":str(root),"mode":mode,"paid_authorization":None,
        "manifest":str(manifest),"requests":plan["total_requests"],"total_seconds":plan["total_seconds"],
        "reserved_estimated_usd":plan["estimated_cap_nano_usd"]/1_000_000_000}


def financial_template(base,task,plan):
    fixtures.validate_task(base,task)
    profile=base.get("output_profile","legacy-8k")
    if plan.get("output_profile","legacy-8k")!=profile:raise ValueError("CONTROL output profile differs from preparation")
    identity=str(uuid.uuid5(uuid.UUID(base["id"]),task["id"]))
    policy=fixtures.budget.contract(cycle_id=identity,deadline_at=1)["request_policy"]
    policy["max_output_tokens"]=fixtures.output_profile(profile)["max_output_tokens"]
    policy["pricing_evidence"]="sha256:"+plan["pricing_sha256"] if plan["pricing_sha256"] else "synthetic-assumption"
    limits=fixtures.financial_limits(cycle_id=identity,deadline_at=1,requests=task["requests"],request_policy=policy,profile=profile)
    del limits["deadline_at"]
    return limits


def validate(root,pin,*,sources=True):
    root=Path(root).resolve(strict=True);plan=read(root,"control-campaign.json")
    if control.pin(plan)!=pin or plan["version"]!=VERSION or plan["root"]!=str(root):raise ValueError("foreign CONTROL pilot")
    base=fixtures.validate(root,plan["base_plan_sha256"]) if sources else read(root,"plan.json")
    if control.pin(base)!=plan["base_plan_sha256"]:raise ValueError("pilot baseline changed")
    totals=fixtures.validate_budget_plan(base)
    if plan.get("output_profile","legacy-8k")!=base.get("output_profile","legacy-8k"):
        raise ValueError("CONTROL output profile differs from preparation")
    if any(type(plan.get(k)) is not int or plan[k]!=v for k,v in totals.items()):
        raise ValueError("CONTROL aggregate limits differ from prepared task budgets")
    authority=read(root,"control-plan.json")
    expected=[plan["python"],"-B",plan["script"],"supervise",str(root),"--plan-sha256",pin]
    if (authority["campaign_sha256"]!=pin or authority["seconds"]!=plan["total_seconds"] or authority["launch"]["command"]!=expected
            or authority["launch"]["plugin_id"]!=plan["plugin_id"] or authority["launch"]["cwd"]!=plan["cwd"]
            or authority["launch"]["herdr"]!=plan["herdr"]):raise ValueError("CONTROL launch differs from prepared pilot")
    if sources and sandbox.digest(Path(authority["launch"]["manifest"]).read_bytes())!=authority["launch"]["manifest_sha256"]:
        raise ValueError("prepared plugin manifest changed")
    if plan["mode"]=="live" and control.pin(read(root,"pricing-observation.json"))!=plan["pricing_sha256"]:
        raise ValueError("pricing observation differs")
    return plan,base,authority


def authorize(root,pin,*,human_reference):
    plan,base,authority=validate(root,pin)
    if plan["mode"]!="live" or not isinstance(human_reference,str) or not human_reference.strip():
        raise ValueError("explicit live operator authorization reference required")
    # This CLI operation is the trusted operator ingress. It is never invoked
    # by supervise or by any model/tool subprocess. It is not a signature proving
    # human consent against an adversary controlling the maintainer host.
    with safe.RootedFS(root,root_mode=0o700) as fs:
        with fs.exclusive_lock(".control-send.lock",directory_modes=()):
            if control.recover(Path(root),authority):raise ValueError("incomplete CONTROL publication; no paid admission")
            return _authorize_under_barrier(root,pin,plan,base,authority,human_reference)


def _authorize_under_barrier(root,pin,plan,base,authority,human_reference):
    template={"version":"harness-paid-approval-v1","decision":"PENDING","campaign_sha256":pin,
        "control_plan_sha256":control.pin(authority),"credential_reference":plan["credential_reference"],
        "human_authorization_reference":None,"approved_at":None,"start_before":None,
        "financial_limits":[financial_template(base,t,plan) for t in base["tasks"]],
        "total_requests":plan["total_requests"],"estimated_cap_nano_usd":plan["estimated_cap_nano_usd"]}
    previous=read(root,"operator-approval.json",optional=True)
    if previous is not None:
        import math
        times=[previous.get("approved_at"),previous.get("start_before")]
        if (any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in times)
                or times[1]<=times[0]):raise ValueError("operator approval timing invalid")
        expected={**template,"decision":"approved","human_authorization_reference":human_reference,
                  "approved_at":times[0],"start_before":times[1]}
        if control.pin(previous)!=control.pin(expected):
            raise ValueError("operator authorization cannot be replaced or reused for another scope")
        anchor={"campaign_sha256":pin,"approval_sha256":control.pin(previous)}
        publish(root,authority,"operator-approval-anchor.json",anchor)
        return anchor
    approval=read(root,"approval-template.json")
    if control.pin(approval)!=control.pin(template):raise ValueError("approval scope changed")
    now=time.time();approval.update(decision="approved",human_authorization_reference=human_reference,approved_at=now,start_before=now+86400)
    publish(root,authority,"operator-approval.json",approval)
    anchor={"campaign_sha256":pin,"approval_sha256":control.pin(approval)}
    publish(root,authority,"operator-approval-anchor.json",anchor)
    return anchor


class SyntheticTLSProvider:
    """A scoped known control, not a measure of model ability."""
    def __init__(self,root,base,authority,guard):
        self.root,self.base,self.authority,self.guard=Path(root),base,authority,guard
        self.cleanup=None
    def __enter__(self):
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                if self.headers.get("Authorization") is not None:self.send_error(400);return
                size=int(self.headers["Content-Length"])
                if not 0<size<=1024*1024:self.send_error(400);return
                payload=fleet_json.loads(self.rfile.read(size))
                if any(m["role"]=="assistant" and "reasoning_content" not in m for m in payload["messages"]):self.send_error(400);return
                task=fleet_json.loads(payload["messages"][1]["content"]);task_id=task["requirements"]["functional"]["task"]
                fixed=next((t for t in outer.base["tasks"] if t["id"]==task_id),None)
                if fixed is None or task["scope"]["editable_paths"]!=fixed["editable_paths"]:self.send_error(400);return
                final=any(m["role"]=="tool" for m in payload["messages"])
                if final:
                    command=mini.SENTINEL;content=fleet_json.canonical_bytes({"type":"submit_candidate","summary":"synthetic control restored","paths":fixed["editable_paths"],"checks":[]}).decode()
                else:
                    command="python3 -B - <<'PY'\nfrom pathlib import Path\n"+"\n".join("Path("+repr(n)+").write_text("+repr((outer.root/"private-controls"/task_id/n).read_text())+")" for n in fixed["editable_paths"])+"\nPY\n";content="Restore reviewed synthetic control."
                result={"choices":[{"message":{"role":"assistant","content":content,"reasoning_content":"synthetic thinking fixture","tool_calls":[{"id":("final-" if final else "write-")+str(len(payload["messages"])),"type":"function","function":{"name":"bash","arguments":fleet_json.canonical_bytes({"command":command}).decode()}}]}}]}
                raw=fleet_json.canonical_bytes(result);identity=str(uuid.uuid4())
                fixtures.publish_run(outer.root,outer.base,"provider-requests/"+identity+".json",{"id":identity,"payload_sha256":control.pin(payload),"response_sha256":sandbox.digest(raw),"mode":"synthetic_tls","at":time.time()})
                self.send_response(200);self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
        prior=read(self.root,"synthetic-tls-endpoint.json",optional=True)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(self.root/"synthetic-cert.pem",self.root/"synthetic-key.pem")
        class Server(ThreadingHTTPServer):
            # Non-daemon handlers are joined by server_close. A stalled TLS
            # handshake or request cannot make that cleanup unbounded.
            daemon_threads=False
            def get_request(self):
                sock,address=self.socket.accept();sock.settimeout(2)
                try:return context.wrap_socket(sock,server_side=True),address
                except BaseException:sock.close();raise
        self.server=Server(("127.0.0.1",prior["port"] if prior else 0),Handler)
        try:
            self.endpoint={"version":"synthetic-loopback-tls-v1","host":"127.0.0.1","port":self.server.server_port,"certificate_pem":(self.root/"synthetic-cert.pem").read_text()}
            publish(self.root,self.authority,"synthetic-tls-endpoint.json",self.endpoint)
            self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        except BaseException:self.server.server_close();raise
        return self.endpoint
    def __exit__(self,*args):
        try:
            try:self.server.shutdown()
            finally:
                self.server.server_close();self.thread.join(5)
            if self.thread.is_alive():raise RuntimeError("synthetic provider thread still alive")
            self.cleanup={"endpoint_sha256":control.pin(self.endpoint),"socket_closed":True,"threads_joined":True}
        except BaseException:
            self.guard._pending.add("synthetic-provider-cleanup-unconfirmed")
            raise


@contextmanager
def signal_monitor(guard,requested):
    done=threading.Event();errors=[]
    def fail(exc):
        if not errors:errors.append(type(exc).__name__)
        record_signal_failure(guard,errors[0])
    def monitor():
        try:
            while not done.wait(.025):
                if requested.is_set():cancel_campaign(guard.root,guard.plan["campaign_sha256"],"CONTROL process signal requested");return
        except BaseException as exc:
            try:fail(exc)
            except BaseException:pass  # main check still forbids successful release/admission
    thread=threading.Thread(target=monitor,daemon=True);thread.start()
    def check():
        if errors:raise RuntimeError("signal cancellation observation failed: "+str(errors))
        if requested.is_set():
            try:cancel_campaign(guard.root,guard.plan["campaign_sha256"],"CONTROL process signal requested")
            except BaseException as exc:
                fail(exc)
                raise RuntimeError("signal cancellation failed; completion forbidden") from exc
    try:yield check
    finally:
        done.set();thread.join(5)
        if thread.is_alive():
            guard._pending.add("signal-monitor-undrained")
            raise RuntimeError("signal watcher cleanup unconfirmed; CONTROL lease retained")
        check()


def record_signal_failure(guard,error):
    publish(guard.root,guard.plan,"signal-monitor-failure.json",{"generation_sha256":control.pin(guard.generation),"error":error})


def creation(root,plan,base,task,guard,approval,endpoint):
    fixtures.validate_task(base,task)
    now=time.time();remaining=int(min(guard.clock["deadline_at"]-now,guard.clock["monotonic_deadline"]-time.monotonic()))
    if remaining<60:raise ValueError("original campaign deadline exhausted")
    seconds=min(600,remaining);runs=Path(root)/task["id"]/"runs";runs.mkdir(mode=0o700,exist_ok=True)
    prepared=contract.prepare(candidate_repo=task["candidate"],base_sha=task["base_sha"],objective="Complete "+task["id"]+" contract",
        scope_contract=scope_for(task),acceptance_contract=acceptance_for(task),
        suite=read(root,task["id"]+"/suite.json"),timeout_seconds=seconds,public_readonly_paths=list(task["public_read"]["read_only"]))
    financial=financial_template(base,task,plan);financial["deadline_at"]=now+seconds
    limits=budget.contract(financial,control_plan=guard.plan,control_clock=guard.clock,mode=plan["mode"],approval_sha256=control.pin(approval) if plan["mode"]=="live" else None,
        synthetic_endpoint=endpoint,owner_store={"runs":str(runs),"identity":control.runtime.directory_identity_from_stat(runs.lstat())},
        wire_version=fixtures.output_profile(base.get("output_profile","legacy-8k"))["wire_version"])
    return contract.create(prepared,cycle_id=financial["cycle_id"],started_at=now,budget_limits=limits)


def scope_for(task):
    return {"schema_version":1,"editable_paths":task["editable_paths"],"temporary_directories":[".tmp"],"max_entries":100,"max_bytes":4*1024*1024}


def acceptance_for(task):
    return {"schema_version":1,"requirements":[{"id":"api","description":"retain public API","checks":[{"kind":"text_contains","path":task["editable_paths"][0],"expected":"def "}]}]}


def verify_child(root,plan,base,task,spec,authority):
    fixtures.validate_task(base,task)
    contract.validate(spec);source=spec["prepared"]["sources"];limits=spec["request_budget"]
    profile=base.get("output_profile","legacy-8k")
    if profile=="thinking-32k-v1":
        if limits["wire_version"]!=fixtures.wire.THINKING_32K_VERSION:raise ValueError("cycle wire differs from output profile")
    elif limits["wire_version"] not in (fixtures.wire.LEGACY_VERSION,fixtures.wire.INDEXED_VERSION):
        raise ValueError("historical output profile cannot gain a new wire contract")
    expected=financial_template(base,task,plan)
    clock=read(root,"control-clock.json")
    endpoint=read(root,"synthetic-tls-endpoint.json") if plan["mode"]=="synthetic_tls" else None
    approval=read(root,"operator-approval-anchor.json") if plan["mode"]=="live" else None
    if ({k:v for k,v in limits["financial"].items() if k!="deadline_at"}!=expected or control.pin(limits["control_plan"])!=control.pin(authority)
            or limits["mode"]!=plan["mode"] or source["permissions"]["cwd"]!=task["candidate"]
            or source["instructions"]["base_sha"]!=task["base_sha"] or control.pin(source["instructions"])!=task["instructions_sha256"]
            or source["public_read"]!=task["public_read"] or control.pin(fleet_json.loads(source["functional_tests"]))!=task["suite_sha256"]
            or control.pin(source["scope"])!=control.pin(scope_for(task)) or control.pin(source["acceptance"])!=control.pin(acceptance_for(task))
            or limits["control_clock"]!=clock or limits["synthetic_endpoint"]!=endpoint
            or limits["owner_store"] is None or limits["owner_store"]["runs"]!=str(Path(root)/task["id"]/"runs")
            or limits["approval_sha256"]!=(approval["approval_sha256"] if approval else None)
            or source["objective"]!="Complete "+task["id"]+" contract" or spec["limits"]["deadline_seconds"]>600
            or spec["started_at"]<limits["control_clock"]["started_at"]):raise ValueError("cycle differs from prepared campaign task")
    return spec


def verify_completed(root,pin,*,_require_release=True):
    plan,base,authority=validate(root,pin,sources=False);result=read(root,"control-completed.json")
    expected_keys={"version","plan_sha256","mode","status","results","quality","billed_cost_usd","promotion","generation","services_sha256"}
    if (set(result)!=expected_keys or result["version"]!=VERSION or result["plan_sha256"]!=pin
            or result["mode"]!=plan["mode"] or result["status"]!=("synthetic_complete" if plan["mode"]=="synthetic_tls" else "live_pilot_complete")
            or result["quality"]!="NOT_VERIFIED" or result["billed_cost_usd"] is not None or result["promotion"]!="disabled"
            or len(result["results"])!=len(base["tasks"])):raise ValueError("foreign completed CONTROL pilot")
    if str(uuid.UUID(result["generation"]))!=result["generation"]:raise ValueError("invalid CONTROL generation")
    generation=read(root,"control-generations/"+result["generation"]+".json")
    if (generation["id"]!=result["generation"] or generation["plan_sha256"]!=control.pin(authority)
            or generation["clock_sha256"]!=control.pin(read(root,"control-clock.json"))):raise ValueError("foreign completed generation")
    control.verify_launch(read(root,"control-launches/"+result["generation"]+".json"),generation,authority)
    services=read(root,"control-services/"+result["generation"]+".json")
    expected={"generation_sha256":control.pin(generation),"signal_monitor_joined":True,"provider":None}
    if plan["mode"]=="synthetic_tls":expected["provider"]={"endpoint_sha256":control.pin(read(root,"synthetic-tls-endpoint.json")),"socket_closed":True,"threads_joined":True}
    release=read(root,"control-releases/"+result["generation"]+".json",optional=True)
    if (services!=expected or control.pin(services)!=result["services_sha256"]
            or (_require_release and release is None)
            or (release is not None and (release["generation_sha256"]!=control.pin(generation) or release["transports_drained"] is not True))):
        raise ValueError("CONTROL completion awaits exact resource release")
    requests=cost=0
    for task,row in zip(base["tasks"],result["results"]):
        spec=verify_child(root,plan,base,task,read(root,task["id"]+"/control-creation.json"),authority)
        owner=cycle.Cycle(Path(root)/task["id"]/"runs",spec["cycle_id"],contract_sha256=control.pin(spec))
        if (set(row)!={"task","contract_sha256","result"} or row["task"]!=task["id"] or row["contract_sha256"]!=owner.pin
                or row["result"]!=owner.verify() or row["result"]["status"]!="accepted_contract"):raise ValueError("CONTROL pilot lacks exact accepted archives")
        measured=owner.load()["attempts"][-1]["classified"]["runtime"]["usage"]
        requests+=measured["admitted_requests"];cost+=measured["reserved_estimated_nano_usd"]
    if requests>plan["total_requests"] or cost>plan["estimated_cap_nano_usd"]:raise ValueError("CONTROL aggregate limits exceeded")
    return result


def reconcile_completed(root,pin,authority):
    result=verify_completed(root,pin,_require_release=False)
    name="control-releases/"+result["generation"]+".json"
    if read(root,name,optional=True) is not None:return verify_completed(root,pin)
    # All workload and service cleanup was already archived before terminal.
    # Only release publication can be absent here. Never take over a living
    # unreleased process merely because its flock happened to disappear.
    previous=read(root,"control-generations/"+result["generation"]+".json")
    with control.OwnedLease(root,control.pin(authority)) as guard:
        observed,zombie=control.runtime.process_observation(previous["pid"])
        if observed==previous["birth"] and not zombie:raise control.AuthorityError("terminal CONTROL process still alive without release")
        publish(root,authority,name,{"generation_sha256":control.pin(previous),"transports_drained":True,"at":time.time(),
            "recovered_after_process_loss":{"observer_generation_sha256":control.pin(guard.generation),"observed_birth":observed,"zombie":zombie}})
    return verify_completed(root,pin)


def cancel_campaign(root,pin,reason):
    plan,base,authority=validate(root,pin,sources=False)
    with safe.RootedFS(root,root_mode=0o700) as fs:
        with fs.exclusive_lock(".control-send.lock",directory_modes=()):
            control.recover(Path(root),authority)
            if read(root,"control-completed.json",optional=True) is not None:return verify_completed(root,pin)
            return control.cancel_under_barrier(fs,control.pin(authority),reason)


def owned_resource_record(root,owner,spec,backend_root,path,current):
    """Recover cleanup authority from the journal, never from a copied record."""
    parts=path.relative_to(backend_root).parts;store=path.parent.parent
    def metadata(path):
        relative=str(path.relative_to(root))
        with safe.RootedFS(root,root_mode=0o700) as fs:
            # Backend grouping directories created by parents=True may be 0755;
            # the private 0700 campaign root and no-follow traversal remain the
            # access boundary. Metadata files still require owner/0600/one link.
            return fleet_json.loads(fs.read_regular(relative,directory_modes=(None,)*(len(Path(relative).parts)-1),max_bytes=64*1024*1024))
    def retained(name):return metadata(store/name)
    record=metadata(path)
    expected_owner=None;candidate=None
    if len(parts) in {6,8} and parts[0]=="attempts":
        attempt=next((a for a in current["attempts"] if control.pin(a["admission"])==parts[1]),None)
        if attempt is None or not attempt["intent"]:raise ValueError("resource lacks registered dispatch")
        if len(parts)==6 and parts[2]=="controls" and len(parts[3])==4 and parts[3].isdigit():
            if control.pin(retained("task.json"))!=attempt["admission"]["prompt_sha256"]:raise ValueError("Mini resource has another task")
            expected_owner=attempt["admission"]["generation"];candidate=store/"empty"
        elif len(parts)==8 and parts[2]=="steps" and parts[4]=="commands":
            from fleet_harness_executor import verify_preparation
            intent=retained("intent.json");preparation=retained("preparation.json")
            verify_preparation(preparation,intent,str(store))
            if (intent["binding"]!={"admission_sha256":parts[1],"tool_call_id":intent["action"]["tool_call_id"]}
                    or parts[5]!=sandbox.digest(intent["action"]["tool_call_id"].encode())
                    or intent["candidate"]!=spec["prepared"]["sources"]["permissions"]["cwd"]
                    or intent["scope"]!=spec["prepared"]["sources"]["scope"]
                    or intent["deadline_at"]!=spec["deadline_at"]):raise ValueError("executor belongs to another admission")
            expected_owner=intent["owner"];candidate=store/"workspace"
    elif len(parts)==5 and parts[0]=="checks":
        attempt=next((a for a in current["attempts"] if a["functional"] and a["functional"]["binding"]==parts[1]),None)
        if attempt is None:raise ValueError("checker lacks registered functional intent")
        bound=owner.json(attempt["functional"]["binding"]);original=retained("contract.json")
        if (original["binding"]!=bound or original["evidence_root"]!=str(store)
                or original["suite_sha256"]!=bound["suite_sha256"]):raise ValueError("checker belongs to another frozen revision")
        expected_owner=record["owner"];candidate=store/"sources"
    if (candidate is None or record["owner"]!=expected_owner or record["candidate"]!=str(candidate)
            or record["guest"]!=str(store/"bridge") or record["scratch"]!=str(path.parent/"work")
            or record["image"]!=sandbox.IMAGE):raise ValueError("resource is not the registered local instance")
    return record


def reconcile_resources(root,pin):
    """Cancel and observe only registered owned resources; no model credential.

    This path is available after deadline, partial CONTROL publication, or a
    failed signal watcher. Missing/corrupt evidence remains a dependency; one
    failed cleanup never suppresses cleanup of other independently bound CIDs.
    """
    root=Path(root).resolve(strict=True);plan,base,authority=validate(root,pin,sources=False)
    recover(root,authority,allow_partial=True)
    if read(root,"control-completed.json",optional=True):return reconcile_completed(root,pin,authority)
    request=cancel_campaign(root,pin,"explicit cleanup/reconciliation; admissions stopped")
    results=[];errors=[];resources=[]
    with control.OwnedLease(root,control.pin(authority)) as guard:
        for task in base["tasks"]:
            try:
                spec=read(root,task["id"]+"/control-creation.json",optional=True)
                if spec is None:
                    if any((root/task["id"]/"runs").glob("**/resource.json")):raise ValueError("resource inventory exists without registered creation")
                    continue
                verify_child(root,plan,base,task,spec,authority)
                runs=root/task["id"]/"runs"
                if control.runtime.directory_identity_from_stat(runs.lstat())!=spec["request_budget"]["owner_store"]["identity"]:
                    raise ValueError("registered owner store replaced")
                owner=cycle.Cycle(runs,spec["cycle_id"],contract_sha256=control.pin(spec))
                backend_root=runs/owner.prefix/"local-backend"
                current=owner.recover();resource_errors=len(errors)
                for index,path in enumerate(sorted(backend_root.rglob("resource.json"))):
                    if index>=1024:raise ValueError("resource inventory exceeds bounded task capacity")
                    if path.parent.name!="resource":continue
                    try:
                        # read() enforces physical no-symlink traversal from the
                        # private campaign root. Sandbox verifies CID/name/labels.
                        relative=str(path.relative_to(root));record=owned_resource_record(root,owner,spec,backend_root,path,current)
                        receipt=sandbox.Sandbox(path.parent,owner=record["owner"]).cleanup()
                        resources.append({"record":relative,"cleanup":receipt})
                    except Exception as exc:errors.append({"task":task["id"],"resource":str(path.relative_to(root)),"error":str(exc)[:500]})
                if len(errors)>resource_errors:continue
                if current["terminal"]:outcome=owner.verify()
                else:
                    approval=read(root,"operator-approval.json") if plan["mode"]=="live" else {"mode":"synthetic_tls","paid_authority":False}
                    backend=backend_module.ControlHarnessBackend(owner,guard=guard,approval=approval,dependencies=root/"dependencies",credential=None)
                    for _ in range(4):
                        outcome=owner.tick(backend)
                        if outcome["status"] in {"cancelled","accepted_contract","exhausted"}:break
                results.append({"task":task["id"],"result":outcome})
                if outcome["status"] not in {"cancelled","accepted_contract","exhausted"}:errors.append({"task":task["id"],"error":"owner cancellation remains pending"})
            except Exception as exc:errors.append({"task":task["id"],"error":str(exc)[:500]})
        result={"status":"dependency_pending" if errors else "cancelled","cancel_id":request["id"],"resources":resources,
            "results":results,"errors":errors,"new_admissions":0,"observation_complete":not errors}
        publish(root,authority,"control-reconciliations/"+guard.generation["id"]+".json",result)
    return result


def source_review(root,pin,*,task_id,binding_sha256,review):
    """Trusted maintainer ingress for an independently reviewed frozen source.

    The Worker cannot access this CLI/store. A reviewer name in a model message
    is not authority. This operation supplements originals; it grants no time,
    budget, model send, new check execution, or acceptance by itself.
    """
    root=Path(root).resolve(strict=True);plan,base,authority=validate(root,pin,sources=False)
    recover(root,authority)
    task=next((t for t in base["tasks"] if t["id"]==task_id),None)
    if task is None:raise ValueError("source review task is not in the prepared campaign")
    with control.OwnedLease(root,control.pin(authority)) as guard:
        if read(root,"control-cancel.json",optional=True):raise ValueError("cancelled campaign cannot admit a new source review")
        spec=verify_child(root,plan,base,task,read(root,task_id+"/control-creation.json"),authority)
        owner=cycle.Cycle(root/task_id/"runs",spec["cycle_id"],contract_sha256=control.pin(spec))
        current=owner.recover();attempt=current["attempts"][-1] if current["attempts"] else None
        if (current["terminal"] or attempt is None or not attempt["functional"]
                or attempt["functional"]["binding"]!=binding_sha256):raise ValueError("review does not target the current frozen check")
        approval=read(root,"operator-approval.json") if plan["mode"]=="live" else {"mode":"synthetic_tls","paid_authority":False}
        backend=backend_module.ControlHarnessBackend(owner,guard=guard,approval=approval,dependencies=root/"dependencies",credential=None)
        binding=owner.json(binding_sha256);backend.register_source_review(binding,review)
    return {"status":"source_review_retained","task":task_id,"binding_sha256":binding_sha256,"review_sha256":control.pin(review),"acceptance":"NOT_VERIFIED; resume original supervisor"}


def load_credential(root,reference):
    """CONTROL-only reference resolution. Never put a secret in argv/artifacts."""
    if reference=="env:DEEPSEEK_API_KEY":value=os.environ.get("DEEPSEEK_API_KEY")
    elif reference=="file:"+str(Path(root)/"provider-key"):
        with safe.RootedFS(root,root_mode=0o700) as fs:
            raw=fs.read_regular_optional("provider-key",directory_modes=(),max_bytes=16384)
        value=raw.decode().rstrip("\r\n") if raw is not None else None
    else:raise ValueError("credential reference is not the prepared CONTROL ingress")
    if value is None:return None
    if not value or len(value)>16384 or any(ord(c)<33 or ord(c)>126 for c in value):
        raise ValueError("invalid credential format at prepared reference")
    return value


def supervise(root,pin,*,signal_requested=None):
    root=Path(root).resolve(strict=True);plan,base,authority=validate(root,pin)
    partial=recover(root,authority,allow_partial=True)
    if read(root,"control-completed.json",optional=True) is not None:return reconcile_completed(root,pin,authority)
    if partial or read(root,"signal-monitor-failure.json",optional=True) or read(root,"control-cancel.json",optional=True):
        return reconcile_resources(root,pin)
    approval={"mode":"synthetic_tls","paid_authority":False};credential=None
    if plan["mode"]=="live":
        anchor=read(root,"operator-approval-anchor.json",optional=True)
        if anchor is None:raise ValueError("dependency: exact paid scope/budget authorization has not been admitted")
        approval=read(root,"operator-approval.json")
        if anchor!={"campaign_sha256":pin,"approval_sha256":control.pin(approval)}:raise ValueError("operator ingress pin changed")
        credential=load_credential(root,plan["credential_reference"])
        if not credential:raise ValueError("dependency: credential unavailable at prepared CONTROL reference")
    requested=signal_requested if signal_requested is not None else threading.Event()
    from contextlib import ExitStack
    with control.OwnedLease(root,control.pin(authority)) as guard:
        guard.bind_herdr()
        provider=None
        with ExitStack() as stack:
            monitor=stack.enter_context(signal_monitor(guard,requested))
            provider=SyntheticTLSProvider(root,base,authority,guard) if plan["mode"]=="synthetic_tls" else None
            endpoint=stack.enter_context(provider) if provider else None
            results=[]
            for task in base["tasks"]:
                monitor();name=task["id"]+"/control-creation.json"
                spec=read(root,name,optional=True)
                cancelled=read(root,"control-cancel.json",optional=True)
                if spec is None:
                    if cancelled:return {"status":"cancelled","results":results,"new_admissions":0}
                    guard.assert_effect();spec=creation(root,plan,base,task,guard,approval,endpoint)
                    publish(root,authority,name,spec)
                spec=verify_child(root,plan,base,task,spec,authority)
                owner=cycle.Cycle.create(root/task["id"]/"runs",spec)
                if owner.load()["terminal"]:
                    outcome=owner.verify()
                else:
                    backend=backend_module.ControlHarnessBackend(owner,guard=guard,approval=approval,dependencies=root/"dependencies",credential=credential,
                        source_admissions=[base["source_admissions"][task["id"]]])
                    for _ in range(20):
                        monitor();validate(root,pin);outcome=owner.tick(backend)
                        if outcome["status"] in {"accepted_contract","cancelled","exhausted","blocked"}:break
                results.append({"task":task["id"],"contract_sha256":owner.pin,"result":outcome})
                if outcome["status"]!="accepted_contract":return {"status":"dependency_pending" if outcome["status"] not in {"cancelled","exhausted"} else outcome["status"],"results":results}
            monitor()
        # Both service context managers have drained before the success record.
        # A failed drain retains this process' lease; it cannot be mistaken for
        # absence of resources or allow a replacement controller.
        if guard._pending:raise RuntimeError("CONTROL resources remain unconfirmed")
        services={"generation_sha256":control.pin(guard.generation),"signal_monitor_joined":True,"provider":provider.cleanup if provider else None}
        publish(root,authority,"control-services/"+guard.generation["id"]+".json",services)
        with safe.RootedFS(root,root_mode=0o700) as fs:
            with fs.exclusive_lock(".control-send.lock",directory_modes=()):
                if requested.is_set():
                    try:control.cancel_under_barrier(fs,control.pin(authority),"CONTROL signal before completion")
                    except BaseException as exc:
                        record_signal_failure(guard,type(exc).__name__);raise
                if read(root,"control-cancel.json",optional=True):return {"status":"cancelled","results":results}
                guard.assert_effect()
                result={"version":VERSION,"plan_sha256":pin,"mode":plan["mode"],"status":"synthetic_complete" if plan["mode"]=="synthetic_tls" else "live_pilot_complete",
                    "results":results,"quality":"NOT_VERIFIED","billed_cost_usd":None,"promotion":"disabled",
                    "generation":guard.generation["id"],"services_sha256":control.pin(services)}
                publish(root,authority,"control-completed.json",result)
    return verify_completed(root,pin)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("action",choices=("prepare","validate","authorize","supervise","cancel","reconcile","source-review","verify"));parser.add_argument("root",type=Path)
    parser.add_argument("--plan-sha256");parser.add_argument("--mode",choices=("synthetic_tls","live"),default="synthetic_tls")
    parser.add_argument("--pricing-evidence",type=Path);parser.add_argument("--human-authorization-reference")
    parser.add_argument("--credential-source",choices=("env","file"),default="env")
    parser.add_argument("--requests-per-task",type=int,default=6,help="Preparation only: 1..30; existing plan limits never change")
    parser.add_argument("--output-profile",choices=("legacy-8k","thinking-32k-v1"),default="legacy-8k",
        help="Preparation only: thinking-32k-v1 reserves USD0.20/request and permits up to15 requests/task")
    parser.add_argument("--task",choices=("D1","D2"));parser.add_argument("--binding-sha256");parser.add_argument("--review",type=Path)
    args=parser.parse_args()
    if args.action=="prepare":result=prepare(args.root,mode=args.mode,pricing_evidence=args.pricing_evidence,credential_source=args.credential_source,requests_per_task=args.requests_per_task,output_profile=args.output_profile)
    elif args.action=="validate":result={"status":"prepared","plan_sha256":control.pin(validate(args.root,args.plan_sha256)[0])}
    elif args.action=="authorize":result=authorize(args.root,args.plan_sha256,human_reference=args.human_authorization_reference)
    elif args.action=="verify":result=verify_completed(args.root,args.plan_sha256)
    elif args.action=="reconcile":result=reconcile_resources(args.root,args.plan_sha256)
    elif args.action=="source-review":result=source_review(args.root,args.plan_sha256,task_id=args.task,binding_sha256=args.binding_sha256,review=fleet_json.loads(args.review.read_bytes()))
    elif args.action=="cancel":
        result=cancel_campaign(args.root,args.plan_sha256,"explicit serial campaign cancellation")
    else:
        requested=threading.Event();prior={s:signal.signal(s,lambda *_:requested.set()) for s in (signal.SIGINT,signal.SIGTERM)}
        try:result=supervise(args.root,args.plan_sha256,signal_requested=requested)
        finally:
            for sig,handler in prior.items():signal.signal(sig,handler)
    print(fleet_json.canonical_bytes(result).decode())


if __name__=="__main__":main()
