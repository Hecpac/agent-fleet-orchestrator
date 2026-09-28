"""Executable local pilot preparation and exclusive CONTROL supervisor.

The paid/Herdr lane remains closed until its own registered-plugin preflight
and explicit campaign authorization exist. Synthetic runs use the real owner
cycle, Mini, isolated executor and checker; they measure no model quality.
"""
import argparse
from contextlib import contextmanager, ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

import fleet_harness_acceptance as checker
import fleet_harness_backend as backend_module
import fleet_harness_budget as budget
import fleet_harness_contract as contract
import fleet_harness_mini as mini
import fleet_harness_sandbox as sandbox
import fleet_herdr_owner_cycle as cycle
import fleet_control_runtime as process_runtime
import fleet_safe_paths as safe
import fleet_json
import fleet_artifacts
import fleet_harness_read_scope as read_scope
import fleet_harness_provider_protocol as wire

REPO=Path(__file__).resolve().parents[1]
FIXTURES=REPO/"tests/fixtures/harness_v1"
VERSION="harness-pilot-preparation-v2"
REQUEST_RESERVE_NANO_USD=100_000_000


def output_profile(name="legacy-8k"):
    if name=="legacy-8k":
        return {"max_output_tokens":8192,"reserve_tokens":65536,"reserve_nano_usd":REQUEST_RESERVE_NANO_USD,
                "wire_version":wire.INDEXED_VERSION}
    if name=="thinking-32k-v1":
        return {"max_output_tokens":32768,"reserve_tokens":131072,"reserve_nano_usd":200_000_000,
                "wire_version":wire.THINKING_32K_VERSION}
    raise ValueError("unknown output profile")


def request_count(value,*,profile="legacy-8k"):
    # No profile increases the original two-million-token task authority.
    maximum=2_000_000//output_profile(profile)["reserve_tokens"]
    if type(value) is not int or not 1<=value<=maximum:
        raise ValueError("requests_per_task must be an exact integer from 1 to "+str(maximum))
    return value


def financial_limits(*,cycle_id,deadline_at,requests,request_policy=None,profile="legacy-8k"):
    selected=output_profile(profile);count=request_count(requests,profile=profile)
    policy=dict(request_policy) if request_policy is not None else budget.contract(cycle_id=cycle_id,deadline_at=deadline_at)["request_policy"]
    if request_policy is not None and (type(policy["max_output_tokens"]) is not int
            or policy["max_output_tokens"]!=selected["max_output_tokens"]):
        raise ValueError("request policy differs from output profile")
    policy["max_output_tokens"]=selected["max_output_tokens"]
    return budget.contract(cycle_id=cycle_id,deadline_at=deadline_at,max_requests=count,
        estimated_cap_nano_usd=count*selected["reserve_nano_usd"],reserve_tokens_per_request=selected["reserve_tokens"],
        reserve_nano_usd_per_request=selected["reserve_nano_usd"],request_policy=policy)


def validate_budget_plan(plan):
    tasks=plan.get("tasks")
    if (not isinstance(tasks,list) or len(tasks)!=2 or any(not isinstance(t,dict) for t in tasks)
            or [t.get("id") for t in tasks]!=["D1","D2"]):
        raise ValueError("pilot needs exact D1/D2 task inventory")
    profile=plan.get("output_profile","legacy-8k");selected=output_profile(profile)
    counts=[request_count(t["requests"],profile=profile) for t in tasks]
    if len(set(counts))!=1:raise ValueError("requests_per_task must apply equally to D1 and D2")
    requests=sum(counts)
    expected={"total_requests":requests,"estimated_cap_nano_usd":requests*selected["reserve_nano_usd"],
              "total_seconds":1200}
    if any(type(plan.get(k)) is not int or plan[k]!=v for k,v in expected.items()):
        raise ValueError("pilot aggregate limits differ from its task reservations")
    for task in tasks:
        if (type(task.get("timeout_seconds")) is not int or task["timeout_seconds"]!=600
                or type(task.get("max_attempts")) is not int or task["max_attempts"]!=3):
            raise ValueError("request count cannot change task deadline or repair limits")
    return expected


def validate_task(plan,task):
    validate_budget_plan(plan)
    if not any(checker.pin(task)==checker.pin(registered) for registered in plan["tasks"]):
        raise ValueError("task differs from its exact prepared budget and identity")


def read(root,name):
    with safe.RootedFS(root) as fs:
        return fleet_json.loads(fs.read_regular(name,directory_modes=(0o700,)*(len(Path(name).parts)-1),max_bytes=16*1024*1024))


def publish_run(root,plan,name,value):
    raw=fleet_json.canonical_bytes(value)
    fleet_artifacts.put_bytes(root,plan["id"],raw)
    sandbox.publish(root,name,value)


def recover_run(root,plan,*,only=None):
    """Complete exact CAS-backed publications; never regenerate an authority."""
    roots=[Path(root),Path(root)/"supervisors",Path(root)/"provider-requests",*[Path(root)/t["id"] for t in plan["tasks"]]]
    for directory in roots:
        for pending in directory.glob(".fleet-atomic-*.tmp"):
            raw=fleet_artifacts.get_bytes(root,plan["id"],pending.name[-68:-4]);value=fleet_json.loads(raw)
            candidates=["admitted.json","creation.json","synthetic-endpoint.json","completed.json","cancel.json","signal-cancellation.json"]
            if value.get("id"):candidates.append(value["id"]+".json")
            names=[n for n in candidates if safe._atomic_pending_name(n,raw)==pending.name]
            if len(names)!=1:raise ValueError("unknown pilot pending publication")
            if only is not None and str((directory/names[0]).relative_to(root)) not in only:continue
            with safe.RootedFS(directory) as fs:fs.atomic_write(names[0],raw,directory_modes=())


def prepare(root,*,dependencies=mini.DIST,requests_per_task=6,profile="legacy-8k"):
    selected=output_profile(profile);requests_per_task=request_count(requests_per_task,profile=profile)
    root=Path(root).resolve();root.mkdir(mode=0o700,parents=True,exist_ok=False)
    frozen=mini.freeze_dependencies(dependencies,root/"dependencies")
    tasks=[]
    for task in ("D1","D2"):
        candidate=root/task/"candidate";candidate.parent.mkdir(mode=0o700)
        # Bytecode caches (e.g. from compileall in CI) are never fixture content.
        shutil.copytree(FIXTURES/task.lower(),candidate,ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
        names=[p.name for p in sorted(candidate.iterdir())]
        for name in names:
            target=root/"private-controls"/task/name;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            target.write_bytes((candidate/name).read_bytes());target.chmod(0o600)
        (candidate/"SPEC.md").write_text("Implement the "+task+" contract. Public executable RPC expectations are supplied in /bridge/public-checks.json. Preserve the API and all required behavior.\n")
        # Deterministic known defect; the synthetic provider restores the
        # independently reviewed control. This is a plumbing pilot fixture.
        path=candidate/("ledger.py" if task=="D1" else "report.py")
        text=path.read_text()
        old,new=("type(generation) is not int","not isinstance(generation,int)") if task=="D1" else ("for record in latest:","for record in latest[:1]:")
        if old not in text:raise ValueError("pilot baseline mutation no longer applies")
        path.write_text(text.replace(old,new))
        def git(*args):return subprocess.check_output(["git",*args],cwd=candidate,stderr=subprocess.PIPE).decode().strip()
        git("init","-q");git("add",".")
        git("-c","user.name=Harness Fixture","-c","user.email=fixture@invalid","commit","-qm","owned isolated pilot baseline")
        private=[]
        if task=="D2":
            private=[{"id":"reserved-usage-1","family":"aggregation","visibility":"reserved",
                "request":{"op":"usage","records":[checker.row(sequence=23,prompt_tokens=43,completion_tokens=7)],"mission":"m","admitted":["r"]},
                "expected":{"value":checker.summary(prompt=43,completion=7),"errors":None}}]
        else:
            private=[{"id":"reserved-reset","family":"setup","visibility":"reserved","request":{"op":"reset"},"expected":{"value":None,"errors":None}},
                {"id":"reserved-ledger-1","family":"atomicity","visibility":"reserved",
                    "request":{"op":"record","args":["reserved",11,"fresh/δ","bytes"]},
                    "expected":{"value":checker.expected_receipt("fresh/δ","bytes",11),"errors":None}}]
        sandbox.publish(root,f"{task}/suite.json",checker.suite(task,reserved=private))
        tasks.append({"id":task,"base_sha":git("rev-parse","HEAD"),"editable_paths":names,
            "public_read":read_scope.prepare(candidate,{"schema_version":1,"editable_paths":names,
                "temporary_directories":[".tmp"],"max_entries":100,"max_bytes":4*1024*1024},["SPEC.md"]),
            "instructions_sha256":checker.pin(contract.instructions.snapshot(candidate,git("rev-parse","HEAD"))),
            "suite_sha256":checker.pin(checker.suite(task,reserved=private)),"candidate":str(candidate),
            "requests":requests_per_task,"timeout_seconds":600,"max_attempts":3,"source_review_required":True})
    sources={str(p):sandbox.digest(p.read_bytes()) for p in (REPO/"scripts").glob("fleet_*.py")}
    plan={"version":VERSION,"id":str(uuid.uuid4()),"root":str(root),"tasks":tasks,"runtime":contract.RUNTIME,
        "image":sandbox.IMAGE,"source_pins":sources,"dependencies":frozen,
        "control_sources":{str(p.relative_to(root)):sandbox.digest(p.read_bytes()) for p in (root/"private-controls").rglob("*.py")},
        "source_admissions":fleet_json.loads((FIXTURES/"source-admissions.json").read_bytes()),
        "total_seconds":1200,"total_requests":2*requests_per_task,
        "estimated_cap_nano_usd":2*requests_per_task*selected["reserve_nano_usd"],
        "credential_reference":"env:DEEPSEEK_API_KEY (CONTROL only; never serialized)",
        "paid_authorization":None,"billing_cost_usd":None,
        "required_live_gates":["explicit global registration of local Herdr plugin", "synthetic Herdr launch/identity/cancel preflight",
            "exact paid scope/budget approval", "current provider pricing and bounded DNS/HTTP process"],
        "stop_conditions":["false acceptance","out-of-scope effect","state loss","unconfirmed cleanup"],
        "promotion":"disabled; this two-task pilot cannot replace the representative 12x3 evaluation"}
    if profile!="legacy-8k":plan["output_profile"]=profile
    sandbox.publish(root,"plan.json",plan)
    (root/"herdr-plugin.toml").write_text('id = "fleet.harness-control-e4"\nname = "Fleet Harness CONTROL"\nversion = "0.1.0"\nmin_herdr_version = "0.9.0"\nplatforms = ["macos"]\n\n[[panes]]\nid = "supervisor"\ntitle = "Fleet CONTROL"\nplacement = "tab"\ncommand = '+fleet_json.canonical_bytes([sys.executable,"-B",str(Path(__file__).resolve()),"supervise",str(root),"--plan-sha256",checker.pin(plan),"--provider","synthetic"]).decode()+'\n')
    return {"plan_sha256":checker.pin(plan),"root":str(root),"paid_authorization":None}


def validate(root,pin):
    plan=read(root,"plan.json")
    if plan["version"]!=VERSION or checker.pin(plan)!=pin or plan["root"]!=str(Path(root).resolve()):raise ValueError("foreign pilot plan")
    validate_budget_plan(plan)
    for name,sha in plan["source_pins"].items():
        if sandbox.digest(Path(name).read_bytes())!=sha:raise ValueError("prepared implementation changed: "+name)
    for name,sha in plan["control_sources"].items():
        if sandbox.digest((Path(root)/name).read_bytes())!=sha:raise ValueError("prepared synthetic control changed")
    if mini.dependency_manifest(Path(root)/"dependencies")!=plan["dependencies"]:raise ValueError("prepared Mini dependencies changed")
    for task in plan["tasks"]:
        suite=read(root,task["id"]+"/suite.json")
        if checker.pin(suite)!=task["suite_sha256"]:raise ValueError("prepared acceptance changed")
    return plan


@contextmanager
def lease(root):
    """A live flock holds exclusivity; a historical PID/O_EXCL file does not."""
    with safe.RootedFS(root) as fs:
        with fs.exclusive_lock(".supervisor.lock",directory_modes=(),blocking=False) as locked:
            yield locked


def creation_for(root,plan,task,admitted,start):
    validate_task(plan,task)
    if plan.get("output_profile","legacy-8k")!="legacy-8k":raise ValueError("output profile requires CONTROL supervisor")
    identity=str(uuid.uuid5(uuid.UUID(plan["id"]),task["id"]))
    if (admitted["plan_sha256"]!=checker.pin(plan) or admitted["deadline_at"]!=admitted["started_at"]+plan["total_seconds"]
            or type(start) not in (int,float) or start<admitted["started_at"]):raise ValueError("invalid original campaign authority")
    remaining=int(admitted["deadline_at"]-start)
    if remaining<60:raise ValueError("original campaign deadline exhausted")
    seconds=min(task["timeout_seconds"],remaining)
    prepared=contract.prepare(candidate_repo=task["candidate"],base_sha=task["base_sha"],objective="Complete "+task["id"]+" contract",
        scope_contract={"schema_version":1,"editable_paths":task["editable_paths"],"temporary_directories":[".tmp"],"max_entries":100,"max_bytes":4*1024*1024},
        acceptance_contract={"schema_version":1,"requirements":[{"id":"api","description":"retain public API","checks":[{"kind":"text_contains","path":task["editable_paths"][0],"expected":"def "}]}]},
        suite=read(root,task["id"]+"/suite.json"),timeout_seconds=seconds,public_readonly_paths=list(task["public_read"]["read_only"]))
    if prepared["sources"]["public_read"]!=task["public_read"]:raise ValueError("prepared public readonly bytes changed")
    return contract.create(prepared,cycle_id=identity,started_at=start,budget_limits=financial_limits(cycle_id=identity,deadline_at=start+seconds,requests=task["requests"]))


def verify_creation(root,plan,task,admitted,spec):
    validate_task(plan,task)
    if plan.get("output_profile","legacy-8k")!="legacy-8k":raise ValueError("output profile requires CONTROL supervisor")
    contract.validate(spec)
    start=spec["started_at"];remaining=int(admitted["deadline_at"]-start);seconds=min(task["timeout_seconds"],remaining)
    identity=str(uuid.uuid5(uuid.UUID(plan["id"]),task["id"]))
    expected_scope={"schema_version":1,"editable_paths":task["editable_paths"],"temporary_directories":[".tmp"],"max_entries":100,"max_bytes":4*1024*1024}
    expected_acceptance={"schema_version":1,"requirements":[{"id":"api","description":"retain public API","checks":[{"kind":"text_contains","path":task["editable_paths"][0],"expected":"def "}]}]}
    source=spec["prepared"]["sources"]
    if plan["version"] not in {"harness-pilot-preparation-v1",VERSION}:raise ValueError("unknown pilot preparation version")
    if plan["version"]==VERSION and source.get("public_read")!=task["public_read"]:
        raise ValueError("cycle creation differs from prepared public read authority")
    if plan["version"]=="harness-pilot-preparation-v1" and "public_read" in source:
        raise ValueError("historical pilot cannot be silently upgraded")
    expected_budget=financial_limits(cycle_id=identity,deadline_at=start+seconds,requests=task["requests"])
    if (admitted["plan_sha256"]!=checker.pin(plan) or admitted["deadline_at"]!=admitted["started_at"]+plan["total_seconds"]
            or start<admitted["started_at"] or remaining<60 or spec["cycle_id"]!=identity
            or spec["deadline_at"]!=start+seconds or source["timeout_seconds"]!=seconds
            or checker.pin(spec["request_budget"])!=checker.pin(expected_budget)
            or source["permissions"]["cwd"]!=task["candidate"] or source["instructions"]["base_sha"]!=task["base_sha"]
            or checker.pin(source["instructions"])!=task["instructions_sha256"] or source["objective"]!="Complete "+task["id"]+" contract"
            or checker.pin(source["scope"])!=checker.pin(expected_scope) or checker.pin(source["acceptance"])!=checker.pin(expected_acceptance)
            or checker.pin(fleet_json.loads(source["functional_tests"]))!=task["suite_sha256"]):
        raise ValueError("cycle creation differs from exact prepared task and campaign")
    return spec


def cancel(root,pin,reason="pilot cancellation requested",*,request_id=None):
    root=Path(root);plan=read(root,"plan.json")
    if checker.pin(plan)!=pin:raise ValueError("cancel belongs to another prepared pilot")
    with safe.RootedFS(root) as fs:
        with fs.exclusive_lock(".campaign-control.lock",directory_modes=()):
            recover_run(root,plan,only={"cancel.json","completed.json"})
            if (root/"completed.json").exists():
                verify_completed(root,plan,pin)
                return {"status":"already_terminal","resources_clean":"verified in archived acceptance"}
            if not (root/"cancel.json").exists():
                publish_run(root,plan,"cancel.json",{"plan_sha256":pin,"id":request_id or str(uuid.uuid4()),"reason":reason})
    for task in plan["tasks"]:
        if not (root/task["id"]/"creation.json").exists():continue
        spec=verify_creation(root,plan,task,read(root,"admitted.json"),read(root,task["id"]+"/creation.json"));runs=root/task["id"]/"runs"
        owner=cycle.Cycle.create(runs,spec);current=owner.recover()
        if current["terminal"]:continue
        active=current["attempts"][-1] if current["attempts"] else None
        request=read(root,"cancel.json")
        owner.control("cancel",request_id=request["id"],target=backend_module.runtime.resource(active["admission"]) if active else None,reason=request["reason"])
    return {"status":"cancel_requested","resources_clean":"NOT_VERIFIED until supervisor reconciles"}


def verify_completed(root,plan,pin):
    result=read(root,"completed.json")
    if (result.get("version")!="pilot-result-v1" or result.get("plan_sha256")!=pin or result.get("status")!="synthetic_complete"
            or [r["task"] for r in result["results"]]!=[t["id"] for t in plan["tasks"]]):raise ValueError("pilot completion lacks exact task inventory")
    total=0;cost=0
    for item,task in zip(result["results"],plan["tasks"]):
        spec=verify_creation(root,plan,task,read(root,"admitted.json"),read(root,task["id"]+"/creation.json"));identity=str(uuid.uuid5(uuid.UUID(plan["id"]),task["id"]))
        if item["cycle_id"]!=identity or item["contract_sha256"]!=checker.pin(spec):raise ValueError("foreign completed cycle")
        owner=cycle.Cycle(Path(root)/task["id"]/"runs",identity,contract_sha256=checker.pin(spec))
        verified=owner.verify();state=owner.load()
        measured=state["attempts"][-1]["classified"]["runtime"]["usage"]
        if verified!=item["result"] or verified["status"]!="accepted_contract" or measured!=item["budget"]:raise ValueError("pilot result differs from archived acceptance")
        total+=measured["admitted_requests"];cost+=measured["reserved_estimated_nano_usd"]
    if total>plan["total_requests"] or cost>plan["estimated_cap_nano_usd"]:raise ValueError("pilot aggregate budget exceeded")
    return result


class SignalCancellation:
    """Signal delivery is subordinate to the supervisor's still-held lease.

    A timed-out thread cannot safely be killed in Python. Record the dependency
    and retain the lease until it stops (or this owning process terminates).
    Neither a timeout nor a failed durable cancel authorizes a successful close.
    """
    DRAIN_SECONDS=5

    def __init__(self,root,pin,requested):
        self.root,self.pin,self.requested=Path(root),pin,requested
        self.request={"plan_sha256":pin,"id":str(uuid.uuid4()),"reason":"supervisor signal requested cancellation"}
        self.done=threading.Event();self.error=None;self.drained=False;self.watcher=None
        if requested is not None:
            self.watcher=threading.Thread(target=self.control,daemon=True);self.watcher.start()

    def control(self):
        try:
            while True:
                # Check again after stop was requested, so an already delivered
                # terminal signal cannot be discarded at the drain boundary.
                if self.requested.is_set():
                    plan=read(self.root,"plan.json")
                    with safe.RootedFS(self.root) as fs:
                        with fs.exclusive_lock(".campaign-control.lock",directory_modes=()):
                            recover_run(self.root,plan,only={"signal-cancellation.json","completed.json"})
                            if (self.root/"completed.json").exists():return
                            if (self.root/"signal-cancellation.json").exists():self.request=read(self.root,"signal-cancellation.json")
                            else:publish_run(self.root,plan,"signal-cancellation.json",self.request)
                    cancel(self.root,self.pin,self.request["reason"],request_id=self.request["id"])
                    return
                if self.done.is_set():return
                self.done.wait(.05)
        except BaseException as exc:
            self.error=exc

    def record_error(self,kind,error):
        plan=read(self.root,"plan.json");identifier=str(uuid.uuid4())
        publish_run(self.root,plan,"supervisors/"+identifier+".json",{"id":identifier,
            "plan_sha256":self.pin,"kind":kind,"at":time.time(),"error_type":type(error).__name__,
            "message":str(error),"request":self.request,"resources_clean":"NOT_VERIFIED"})

    def check(self):
        if self.error is not None:self.drain()

    def drain(self):
        if not self.drained:
            self.done.set()
            if self.watcher is not None:
                self.watcher.join(self.DRAIN_SECONDS)
                if self.watcher.is_alive():
                    self.error=RuntimeError("cancellation drain pending; supervisor lease retained")
                    try:self.record_error("cancellation_drain_pending",self.error)
                    except BaseException as exc:self.error=exc
                    finally:
                        # Always wait under the lease, even if writing the error
                        # fails. Returning with this authority thread alive is unsafe.
                        self.watcher.join()
            self.drained=True
            if self.error is not None:
                try:self.record_error("cancellation_failed",self.error)
                except BaseException as exc:self.error=exc
        if self.error is not None:raise RuntimeError("supervisor cancellation failed; completion forbidden") from self.error
        return self.requested is not None and self.requested.is_set()


@contextmanager
def signal_cancellation(root,pin,requested):
    """Arm only after acquiring the lease; disarm before releasing it."""
    monitor=SignalCancellation(root,pin,requested)
    try:yield monitor
    finally:monitor.drain()


def reconcile_signal_request(root,plan,pin):
    """An earlier watcher failure is an admission gate, including after restart."""
    requests=[]
    if (root/"signal-cancellation.json").exists():requests.append(read(root,"signal-cancellation.json"))
    for path in sorted((root/"supervisors").glob("*.json")):
        value=read(root,str(path.relative_to(root)))
        if value.get("kind") in {"cancellation_failed","cancellation_drain_pending"}:requests.append(value["request"])
    for request in requests:
        if request["plan_sha256"]!=pin or str(uuid.UUID(request["id"]))!=request["id"]:raise ValueError("foreign signal request")
    if requests:
        request=requests[0]
        cancel(root,pin,request["reason"],request_id=request["id"])


class SyntheticProvider:
    def __init__(self,root,plan):self.root,self.plan=Path(root),plan;self.server=None
    def __enter__(self):
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                size=int(self.headers["Content-Length"])
                if not 0<size<=1024*1024:raise ValueError("oversized synthetic request")
                payload=fleet_json.loads(self.rfile.read(size));task=fleet_json.loads(payload["messages"][1]["content"])
                task_id=task["requirements"]["functional"]["task"]
                fixed=next((t for t in outer.plan["tasks"] if t["id"]==task_id),None)
                if fixed is None or task["scope"]["editable_paths"]!=fixed["editable_paths"]:
                    self.send_error(400,"task outside prepared synthetic scope");return
                final=any(m["role"]=="tool" for m in payload["messages"])
                if final:
                    command=mini.SENTINEL;content=fleet_json.canonical_bytes({"type":"submit_candidate","summary":"synthetic control restored","paths":task["scope"]["editable_paths"],"checks":[]}).decode()
                else:
                    command="python3 -B - <<'PY'\nfrom pathlib import Path\n"+"\n".join("Path("+repr(name)+").write_text("+repr((outer.root/"private-controls"/fixed["id"]/name).read_text())+")" for name in fixed["editable_paths"])+"\nPY\n";content="Restore the independently reviewed synthetic control."
                identifier=("final-" if final else "write-")+str(len(payload["messages"]))
                value={"choices":[{"message":{"role":"assistant","content":content,"tool_calls":[{"id":identifier,"type":"function","function":{"name":"bash","arguments":fleet_json.canonical_bytes({"command":command}).decode()}}]},"finish_reason":"tool_calls"}]}
                raw=fleet_json.canonical_bytes(value)
                identifier=str(uuid.uuid4())
                publish_run(outer.root,outer.plan,"provider-requests/"+identifier+".json",{"id":identifier,"payload_sha256":checker.pin(payload),"response_sha256":sandbox.digest(raw),"at":time.time()})
                self.send_response(200);self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
        port=read(self.root,"synthetic-endpoint.json")["port"] if (self.root/"synthetic-endpoint.json").exists() else 0
        self.server=ThreadingHTTPServer(("127.0.0.1",port),Handler)
        try:
            publish_run(self.root,self.plan,"synthetic-endpoint.json",{"port":self.server.server_port})
            self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        except BaseException:
            self.server.server_close();raise
        return f"http://127.0.0.1:{self.server.server_port}/chat/completions"
    def __exit__(self,*args):
        self.server.shutdown();self.server.server_close();self.thread.join(2)


def supervise(root,pin,*,provider="synthetic",signal_requested=None):
    root=Path(root);plan=validate(root,pin)
    if provider!="synthetic":raise ValueError("live lane closed: "+"; ".join(plan["required_live_gates"]))
    with ExitStack() as stack:
        locked=stack.enter_context(lease(root))
        if not locked:return {"status":"supervisor_busy","new_admissions":0}
        if signal_requested is not None and signal_requested.is_set():return {"status":"interrupted_before_admission","new_admissions":0}
        monitor=stack.enter_context(signal_cancellation(root,pin,signal_requested))
        recover_run(root,plan)
        if (root/"completed.json").exists():return verify_completed(root,plan,pin)
        reconcile_signal_request(root,plan,pin)
        if not (root/"admitted.json").exists():
            start=time.time();publish_run(root,plan,"admitted.json",{"plan_sha256":pin,"started_at":start,"deadline_at":start+plan["total_seconds"]})
        admitted=read(root,"admitted.json")
        if admitted["plan_sha256"]!=pin:raise ValueError("pilot admission changed")
        birth,_=process_runtime.process_observation(os.getpid())
        identifier=str(uuid.uuid4())
        publish_run(root,plan,"supervisors/"+identifier+".json",{"id":identifier,"plan_sha256":pin,"pid":os.getpid(),"birth":birth,
            "herdr":{k:os.environ.get(k) for k in ("HERDR_ENV","HERDR_PANE_ID","HERDR_PLUGIN_ENTRYPOINT_ID")},"at":time.time()})
        results=[]
        with SyntheticProvider(root,plan) as endpoint:
            for task in plan["tasks"]:
                monitor.check()
                if signal_requested is not None and signal_requested.is_set():monitor.drain()
                if (root/"cancel.json").exists():
                    cancel(root,pin)
                    if not (root/task["id"]/"creation.json").exists():return {"status":"cancelled","results":results}
                identity=str(uuid.uuid5(uuid.UUID(plan["id"]),task["id"]));runs=root/task["id"]/"runs";runs.mkdir(mode=0o700,exist_ok=True)
                creation=root/task["id"]/"creation.json"
                if not creation.exists():
                    start=time.time();remaining=int(admitted["deadline_at"]-start)
                    if remaining<60:return {"status":"exhausted","results":results}
                    spec=creation_for(root,plan,task,admitted,start)
                    publish_run(root,plan,task["id"]+"/creation.json",spec)
                spec=verify_creation(root,plan,task,admitted,read(root,task["id"]+"/creation.json"))
                if spec["deadline_at"]>admitted["deadline_at"]:raise ValueError("child deadline exceeds campaign")
                owner=cycle.Cycle.create(runs,spec)
                backend=backend_module.LocalHarnessBackend(owner,endpoint=endpoint,dependencies=root/"dependencies",source_admissions=[plan["source_admissions"][task["id"]]])
                for _ in range(20):
                    monitor.check()
                    if signal_requested is not None and signal_requested.is_set():monitor.drain()
                    validate(root,pin)
                    if (root/"cancel.json").exists():cancel(root,pin)
                    result=owner.tick(backend)
                    if result["status"] in {"accepted_contract","cancelled","exhausted","blocked"}:break
                results.append({"task":task["id"],"cycle_id":identity,"contract_sha256":owner.pin,"result":result,"budget":backend.ledger.summary()})
                if result["status"]!="accepted_contract":return {"status":"cancelled" if result["status"]=="cancelled" else "dependency_pending","results":results}
        result={"version":"pilot-result-v1","plan_sha256":pin,"status":"synthetic_complete","results":results,"live":"NOT_VERIFIED","quality":"NOT_VERIFIED","billed_cost_usd":None}
        interrupted=monitor.drain()
        with safe.RootedFS(root) as fs:
            with fs.exclusive_lock(".campaign-control.lock",directory_modes=()):
                # This observation, serialized with explicit durable cancel,
                # is the campaign terminal decision. Signals arriving after it
                # are late notifications on already accepted terminal tasks.
                interrupted=interrupted or signal_requested is not None and signal_requested.is_set()
                if interrupted and not (root/"signal-cancellation.json").exists():
                    publish_run(root,plan,"signal-cancellation.json",monitor.request)
                cancelled=interrupted or (root/"cancel.json").exists()
                if not cancelled:publish_run(root,plan,"completed.json",result)
        if cancelled:
            reconcile_signal_request(root,plan,pin)
            return {"status":"cancelled","results":results}
        return verify_completed(root,plan,pin)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("action",choices=("prepare","validate","supervise","cancel"));parser.add_argument("root",type=Path)
    parser.add_argument("--plan-sha256");parser.add_argument("--provider",choices=("synthetic","live"),default="synthetic")
    parser.add_argument("--requests-per-task",type=int,default=6,help="Preparation only: 1..30; existing plan limits never change")
    args=parser.parse_args()
    if args.action=="prepare":result=prepare(args.root,requests_per_task=args.requests_per_task)
    elif args.action=="validate":result={"status":"prepared","plan_sha256":checker.pin(validate(args.root,args.plan_sha256))}
    elif args.action=="cancel":result=cancel(args.root,args.plan_sha256)
    else:
        requested=threading.Event()
        prior={s:signal.signal(s,lambda *_:requested.set()) for s in (signal.SIGINT,signal.SIGTERM)}
        try:result=supervise(args.root,args.plan_sha256,provider=args.provider,signal_requested=requested)
        finally:
            for sig,handler in prior.items():signal.signal(sig,handler)
    print(fleet_json.canonical_bytes(result).decode())


if __name__=="__main__":main()
