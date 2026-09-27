"""Versioned CONTROL ledger; v1 is used only as a financial/CAS subledger.

No delegated model or candidate receives the credential, lease or this store.
An incomplete call retains its reservation and never grants resend authority.
"""
import base64
import copy
import math
import os
from pathlib import Path
import time

import fleet_harness_budget as budget
import fleet_harness_control as control
import fleet_harness_https as https
import fleet_harness_provider_protocol as wire
import fleet_harness_sandbox as sandbox
import fleet_json
import fleet_safe_paths as safe

VERSION="harness-budget-v2"


def contract(financial, *, control_plan, control_clock, mode, approval_sha256, synthetic_endpoint=None, owner_store=None, wire_version=wire.VERSION):
    wire.validate_version(wire_version)
    expected=budget.contract(**{k:v for k,v in financial.items() if k!="version"})
    if control.pin(financial)!=control.pin(expected):raise budget.BudgetError("invalid financial limits")
    if wire_version==wire.THINKING_32K_VERSION and (
            financial["request_policy"]["max_output_tokens"]!=32768
            or financial["reserve_tokens_per_request"]!=131072
            or financial["reserve_nano_usd_per_request"]!=200_000_000):
        raise budget.BudgetError("thinking-32k wire requires its complete output and reservation policy")
    if mode not in {"live","synthetic_tls"}:raise budget.BudgetError("invalid CONTROL provider mode")
    if mode=="live":
        if owner_store is None:raise budget.BudgetError("live authority requires exact owner journal root")
        if synthetic_endpoint is not None:raise budget.BudgetError("live authority cannot select synthetic routing")
        if not isinstance(approval_sha256,str) or len(approval_sha256)!=64 or any(c not in "0123456789abcdef" for c in approval_sha256):
            raise budget.BudgetError("live requires exact external approval pin")
    else:
        if approval_sha256 is not None:raise budget.BudgetError("synthetic evidence cannot carry paid approval")
        https.synthetic_endpoint(synthetic_endpoint)
    if control_plan.get("version")!=control.VERSION:raise budget.BudgetError("unknown CONTROL authority")
    if owner_store is not None:
        if set(owner_store)!={"runs","identity"} or not Path(owner_store["runs"]).is_absolute() or not Path(owner_store["runs"]).is_relative_to(Path(control_plan["root"])):
            raise budget.BudgetError("owner journal escaped CONTROL root")
        control.runtime.validate_directory_identity(owner_store["identity"])
    if (control_clock["plan_sha256"]!=control.pin(control_plan)
            or control_clock["deadline_at"]!=control_clock["started_at"]+control_plan["seconds"]
            or control_clock["monotonic_deadline"]!=control_clock["monotonic_started"]+control_plan["seconds"]
            or not control_clock["started_at"]<financial["deadline_at"]<=control_clock["deadline_at"]):
        raise budget.BudgetError("financial deadline exceeds original CONTROL clock")
    return {"version":VERSION,"financial":copy.deepcopy(financial),"control_plan":copy.deepcopy(control_plan),
        "control_clock":copy.deepcopy(control_clock),"mode":mode,"approval_sha256":approval_sha256,"wire_version":wire_version,
        "synthetic_endpoint":copy.deepcopy(synthetic_endpoint),"owner_store":copy.deepcopy(owner_store)}


def validate(limits):
    expected=contract(limits["financial"],control_plan=limits["control_plan"],control_clock=limits["control_clock"],mode=limits["mode"],approval_sha256=limits["approval_sha256"],synthetic_endpoint=limits["synthetic_endpoint"],owner_store=limits["owner_store"],wire_version=limits["wire_version"])
    if control.pin(expected)!=control.pin(limits):raise budget.BudgetError("CONTROL budget contract changed")
    return limits


def approval_check(value, limits):
    """The pin comes from trusted human ingress, not an 'approved' file alone."""
    if limits["mode"]=="synthetic_tls":
        if value!={"mode":"synthetic_tls","paid_authority":False}:raise budget.BudgetError("invalid synthetic authorization marker")
        return
    if (control.pin(value)!=limits["approval_sha256"] or value.get("decision")!="approved"
            or value.get("campaign_sha256")!=limits["control_plan"]["campaign_sha256"]
            or value.get("control_plan_sha256")!=control.pin(limits["control_plan"])
            or value.get("credential_reference") not in {"env:DEEPSEEK_API_KEY","file:"+str(Path(limits["control_plan"]["root"])/"provider-key")}
            or not isinstance(value.get("human_authorization_reference"),str) or not value["human_authorization_reference"]):
        raise budget.BudgetError("paid authorization not bound to this exact campaign")
    # Campaign preparation assigns disjoint deterministic cycle budgets; the
    # approval includes their full contracts, so per-cycle copies cannot grow.
    children=value.get("financial_limits")
    expected={k:v for k,v in limits["financial"].items() if k!="deadline_at"}
    if not isinstance(children,list) or not 1<=len(children)<=1000:
        raise budget.BudgetError("invalid approved financial inventory")
    identities=set()
    for child in children:
        try:
            reconstructed=budget.contract(deadline_at=1,**{k:v for k,v in child.items() if k!="version"})
            del reconstructed["deadline_at"]
            if (control.pin(child)!=control.pin(reconstructed) or child["cycle_id"] in identities):
                raise ValueError("duplicate or malformed child")
            identities.add(child["cycle_id"])
        except (ValueError,TypeError,KeyError,AttributeError) as exc:
            raise budget.BudgetError("invalid approved child financial contract") from exc
    if sum(control.pin(v)==control.pin(expected) for v in children)!=1:
        raise budget.BudgetError("cycle financial limits not approved")
    if (value.get("version")!="harness-paid-approval-v1"
            or any(type(value.get(k)) is not int for k in ("total_requests","estimated_cap_nano_usd"))
            or any(type(value.get(k)) not in (int,float) or not math.isfinite(value[k]) or value[k]<=0
                   for k in ("approved_at","start_before"))
            or not value["approved_at"]<=limits["control_clock"]["started_at"]<value["start_before"]
            or sum(v["max_requests"] for v in children)!=value["total_requests"]
            or sum(v["estimated_cap_nano_usd"] for v in children)!=value["estimated_cap_nano_usd"]):
        raise budget.BudgetError("paid authority timing/aggregate limits differ")


class Ledger:
    def __init__(self, guard, limits, *, approval, cycle=None):
        if type(guard) is not control.OwnedLease:raise budget.BudgetError("a serialized record is not CONTROL authority")
        guard.assert_owned();validate(limits);approval_check(approval,limits)
        if (control.pin(guard.plan)!=control.pin(limits["control_plan"]) or control.pin(guard.clock)!=control.pin(limits["control_clock"])):
            raise budget.BudgetError("foreign live CONTROL or renewed clock")
        if cycle is None and limits["mode"]=="live":raise budget.BudgetError("live ledger requires the registered owner Cycle")
        if cycle is not None:
            from fleet_herdr_owner_cycle import Cycle
            if (type(cycle) is not Cycle or cycle.cycle_id!=limits["financial"]["cycle_id"]
                    or control.pin(cycle.json(cycle.pin)["request_budget"])!=control.pin(limits)
                    or limits["owner_store"] is None or str(cycle.runs.resolve())!=limits["owner_store"]["runs"]
                    or control.runtime.directory_identity_from_stat(cycle.runs.lstat())!=limits["owner_store"]["identity"]
                    or cycle.prefix!=f"missions/{cycle.cycle_id}/owner-cycle"):
                raise budget.BudgetError("ledger belongs to another owner Cycle")
        self.cycle=cycle
        self._owner_seen=(0,None)
        self.guard=guard;self.limits=copy.deepcopy(limits);self.approval=copy.deepcopy(approval)
        self.root=guard.root/"live-ledgers"/limits["financial"]["cycle_id"]
        self._registry_name="control-ledgers/"+limits["financial"]["cycle_id"]+".json"
        registered=control.read(guard.root,self._registry_name,optional=True)
        if registered is not None:
            for name,path in (("ledger",self.root),("financial",self.root/"financial")):
                if not path.exists() or control.runtime.directory_identity_from_stat(path.lstat())!=registered[name]:
                    raise budget.BudgetError("registered financial store disappeared or was replaced")
        self.root.parent.mkdir(mode=0o700,exist_ok=True)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.financial=budget.Ledger(self.root/"financial",limits["financial"])
        self._storage={"contract_sha256":control.pin(limits),"ledger":control.runtime.directory_identity_from_stat(self.root.lstat()),
            "financial":control.runtime.directory_identity_from_stat(self.financial.root.lstat())}
        control.publish(guard.root,guard.plan,self._registry_name,self._storage)
        sandbox.publish(self.root,"contract.json",limits)
        sandbox.publish(self.root,"approval.json",approval)
        self.recover()

    def _publish(self,path,value):
        # Dedicated recoverable publication envelope, independent of v1 format.
        envelope={"path":path,"value":value}
        import fleet_artifacts
        fleet_artifacts.put_bytes(self.root,self.limits["financial"]["cycle_id"],fleet_json.canonical_bytes(envelope))
        sandbox.publish(self.root,path,value)

    def recover(self):
        import fleet_artifacts
        ns=self.limits["financial"]["cycle_id"];path=fleet_artifacts.store_path(self.root,ns)
        if not path.exists():return
        with safe.RootedFS(path) as fs:names=fs.list_directory("",directory_modes=())
        for name in names:
            if name.startswith(".fleet-atomic-"):
                with safe.RootedFS(path) as fs:raw=fs.read_regular(name,directory_modes=(),max_bytes=16*1024*1024)
                if name!=safe._atomic_pending_name(sandbox.digest(raw),raw):raise budget.BudgetError("foreign pending transport CAS")
                fleet_artifacts.put_bytes(self.root,ns,raw)
        with safe.RootedFS(path) as fs:names=fs.list_directory("",directory_modes=())
        for name in names:
            envelope=fleet_json.loads(fleet_artifacts.get_bytes(self.root,ns,name));destination=envelope["path"]
            parts=Path(destination).parts
            allowed=destination=="revoked.json" or (len(parts)==2 and parts[0] in {"sends","outcomes","authority"}
                and parts[1].endswith(".json") and len(parts[1])==69 and not any(c not in "0123456789abcdef" for c in parts[1][:-5]))
            if set(envelope)!={"path","value"} or not allowed:
                raise budget.BudgetError("foreign transport publication")
            sandbox.publish(self.root,destination,envelope["value"])

    def read(self,name,optional=False):return control.read(self.root,name,optional=optional)
    def summary(self):return self.financial.summary()

    def _stopped(self):
        self.guard.assert_effect()
        if self.read("revoked.json",optional=True) is not None:return True
        # A v2 revoke commits before its pathname, independently of the v1
        # financial layout. The guardian never waits on the send barrier.
        import fleet_artifacts
        path=fleet_artifacts.store_path(self.root,self.limits["financial"]["cycle_id"])
        if path.exists():
            with safe.RootedFS(path) as fs:
                for name in fs.list_directory("",directory_modes=()):
                    raw=fs.read_regular(name,directory_modes=(),max_bytes=16*1024*1024)
                    if name.startswith(".fleet-atomic-"):
                        if name!=safe._atomic_pending_name(sandbox.digest(raw),raw):return True
                    elif sandbox.digest(raw)!=name:raise budget.BudgetError("transport CAS bytes changed")
                    if fleet_json.loads(raw)["path"]=="revoked.json":return True
        if control.pin(control.read(self.guard.root,self._registry_name))!=control.pin(self._storage):
            raise budget.BudgetError("financial store binding changed")
        for name,path in (("ledger",self.root),("financial",self.financial.root)):
            if control.runtime.directory_identity_from_stat(path.lstat())!=self._storage[name]:
                raise budget.BudgetError("financial store replaced; new admissions denied")
        clock=self.limits["control_clock"]
        if time.monotonic()>=clock["monotonic_started"]+self.limits["financial"]["deadline_at"]-clock["started_at"]:
            return True
        if self.cycle is not None and self._owner_veto():return True
        return self.financial.read("revoked.json",optional=True) is not None

    def _owner_veto(self):
        """Bounded journal observation; never semantic replay or acceptance.

        Full Cycle.load occurs before reservation outside the HTTP watchdog.
        This observer only withdraws authority on committed cancel, terminal,
        pending publication, rewind, identity change or incomplete observation.
        """
        expected=self.limits["owner_store"]
        with safe.RootedFS(expected["runs"],root_mode=0o700) as fs:
            if control.runtime.directory_identity_from_stat(os.fstat(fs._root_fd))!=expected["identity"]:
                raise budget.BudgetError("owner journal physical root changed")
            directory=self.cycle.prefix+"/events"
            names=fs.list_directory(directory,directory_modes=(0o700,)*4)
            if len(names)>1024 or any(name!=f"{i:06d}.json" for i,name in enumerate(names,1)):return True
            previous=None;total=0;seen=self._owner_seen
            if len(names)<seen[0]:return True
            veto=False
            for seq,name in enumerate(names,1):
                raw=fs.read_regular(directory+"/"+name,directory_modes=(0o700,)*4,max_bytes=65536)
                total+=len(raw)
                if total>4*1024*1024:return True
                event=fleet_json.loads(raw)
                if event["seq"]!=seq or event["previous"]!=previous:return True
                previous=control.pin(event)
                if seq==seen[0] and previous!=seen[1]:return True
                if event["kind"]=="terminal" or event["kind"]=="control_requested" and event["payload"]["action"]=="cancel":veto=True
            if len(names)<self._owner_seen[0]:return True
            self._owner_seen=(len(names),previous)
            return veto

    def revoke(self,reason):
        with safe.RootedFS(self.guard.root,root_mode=0o700) as fs:
            with fs.exclusive_lock(".control-send.lock",directory_modes=()):
                self.recover()
                existing=self.read("revoked.json",optional=True)
                if existing is None:self._publish("revoked.json",{"at":time.time(),"reason":str(reason)[:500]})
                return self.financial.revoke(reason)

    def request(self,logical_id,admission,payload,*,policy,token,fault=None):
        fault=(lambda _:None) if fault is None else fault
        self.guard.assert_effect()
        from fleet_harness_backend import TOOLS
        if control.pin(payload)!=control.pin(wire.payload(payload["messages"],TOOLS,version=self.limits["wire_version"])):
            raise budget.BudgetError("provider payload escaped fixed thinking/tools protocol")
        if self.limits["mode"]=="synthetic_tls" and token is not None:raise budget.BudgetError("synthetic requests cannot receive credentials")
        if self.limits["mode"]=="live" and (not isinstance(token,str) or not token):raise budget.BudgetError("CONTROL credential unavailable")
        policy=https.validate(policy)
        raw=fleet_json.canonical_bytes(payload)
        with safe.RootedFS(self.guard.root,root_mode=0o700) as fs:
            with fs.exclusive_lock(".control-send.lock",directory_modes=()):
                control.recover(self.guard.root,self.guard.plan)
                self.guard.assert_effect()
                if self._stopped():raise budget.BudgetError("owner cancellation/terminal state forbids reservation")
                if self.cycle is not None:
                    attempts=self.cycle.load()["attempts"]
                    if not attempts or control.pin(attempts[-1]["admission"])!=admission:
                        raise budget.BudgetError("provider query is not the current owner admission")
                if self._stopped():raise budget.BudgetError("CONTROL expired during owner validation")
                record=self.financial.reserve(logical_id,admission,payload)
        call=record["id"];fault("after_reserve")
        authority={"generation":copy.deepcopy(self.guard.generation),"launch":copy.deepcopy(self.guard._bound),
            "clock":copy.deepcopy(self.guard.clock),"approval_sha256":control.pin(self.approval),
            "owner_contract_sha256":self.cycle.pin if self.cycle is not None else None}
        self._publish("authority/"+call+".json",authority)
        kwargs={"deadline_at":min(self.limits["financial"]["deadline_at"],self.guard.clock["deadline_at"]),"stopped":self._stopped}
        connection=(https.SyntheticTLS(policy,endpoint=self.limits["synthetic_endpoint"],**kwargs)
            if self.limits["mode"]=="synthetic_tls" else https.NumericHTTPS(policy,**kwargs))
        with safe.RootedFS(self.guard.root,root_mode=0o700) as fs:
            with fs.exclusive_lock(".control-send.lock",directory_modes=()):
                control.recover(self.guard.root,self.guard.plan)
                if self._stopped():raise budget.ReconcileRequired("request revoked before send")
                at=time.time()
                sent={"call":call,"at":at,"monotonic_at":time.monotonic(),"payload_sha256":sandbox.digest(raw),
                    "authority_sha256":control.pin(authority),"policy":policy,"mode":self.limits["mode"]}
                self._publish("sends/"+call+".json",sent)
                self.financial.publish("sends/"+call+".json",{"call":call,"at":at})
                self.guard.begin_transport(call)
        error=None
        try:
            fault("after_send_intent")
            connection.exchange(raw,{"Content-Type":"application/json",**({"Authorization":"Bearer "+token} if token else {})})
        except BaseException as exc:error=exc
        finally:
            outcome=connection.retained_outcome()
            if outcome is not None:
                observed={**outcome,"body_b64":base64.b64encode(outcome["body"]).decode()};del observed["body"]
                try:self._publish("outcomes/"+call+".json",{"call":call,"send_sha256":control.pin(sent),"outcome":observed})
                finally:
                    if outcome["transport_closed"]:self.guard.end_transport(call,closed=True)
            elif not connection.connect_started:
                # A test/process interruption before connect owns no socket.
                self.guard.end_transport(call,closed=True)
        fault("after_outcome")
        if outcome is None or not outcome["response_complete"] or not outcome["transport_closed"]:
            if error is not None and not isinstance(error,Exception):raise error
            raise budget.ReconcileRequired("incomplete/ambiguous owned HTTPS; reservation retained") from error
        retained=self._account(call,outcome)
        if error is not None:raise budget.ReconcileRequired("complete outcome has unresolved transport failure") from error
        if self._stopped():raise budget.ReconcileRequired("late response retained after revocation")
        fault("after_response")
        return retained

    def _receipt(self,call,outcome):
        usage=None
        try:
            incoming=fleet_json.loads(outcome["body"]).get("usage")
            if incoming is not None:
                usage={key:budget.integer(incoming[key],10**8) for key in ("prompt_tokens","completion_tokens","total_tokens")}
                if usage["total_tokens"]!=usage["prompt_tokens"]+usage["completion_tokens"]:usage=None
        except (ValueError,KeyError,TypeError,AttributeError):pass
        retained={"id":call,"http_status":outcome["http_status"],"body_b64":base64.b64encode(outcome["body"]).decode(),
            "usage":usage,"billed_cost_usd":None,"response_complete":True}
        return retained

    def _account(self,call,outcome):
        retained=self._receipt(call,outcome);usage=retained["usage"]
        self.financial.publish("responses/"+call+".json",retained)
        if outcome["http_status"]!=200:self.revoke("provider_http_"+str(outcome["http_status"]))
        if usage and usage["total_tokens"]>self.limits["financial"]["reserve_tokens_per_request"]:
            self.revoke("provider_usage_exceeded_reservation")
        return retained

    def reconcile(self,logical_id):
        self.guard.assert_effect();self.recover();self.financial.recover()
        if self._stopped():raise budget.ReconcileRequired("revoked CONTROL cannot reuse a response")
        call=sandbox.digest(logical_id.encode());observed=self.read("outcomes/"+call+".json",optional=True)
        if observed is not None:
            out=observed["outcome"]
            if out["response_complete"] and out["transport_closed"]:
                outcome={**out,"body":base64.b64decode(out["body_b64"],validate=True)}
                retained=self._receipt(call,outcome)
                prospective=self.originals()
                prospective["financial"]["responses/"+call+".json"]=base64.b64encode(fleet_json.canonical_bytes(retained)).decode()
                # Validate the entire chain BEFORE publishing derived accounting
                # or returning bytes to native Mini. Orphan outcomes are evidence,
                # never input authority, even if their self-reported status is 200.
                verify_originals(prospective,self.limits,contract_sha256=self.cycle.pin if self.cycle is not None else None)
                self.financial.publish("responses/"+call+".json",retained)
        result=self.financial.reconcile(logical_id)
        if result["status"]=="response_retained":
            verify_originals(self.originals(),self.limits,contract_sha256=self.cycle.pin if self.cycle is not None else None)
            if self._stopped():raise budget.ReconcileRequired("response lost active CONTROL authority")
        return result

    def originals(self):
        self.recover()  # never omit a CAS-committed veto or outcome from an archive
        names=["contract.json","approval.json"]
        if (self.root/"revoked.json").exists():names.append("revoked.json")
        for directory in ("sends","outcomes","authority"):
            names.extend(str(p.relative_to(self.root)) for p in sorted((self.root/directory).glob("*.json")))
        return {"version":VERSION,"financial":self.financial.originals(),
            "control":{name:base64.b64encode((self.root/name).read_bytes()).decode() for name in names}}


def verify_originals(originals,limits,*,contract_sha256=None):
    try:return _verify_originals(originals,limits,contract_sha256=contract_sha256)
    except (KeyError,IndexError,TypeError,AttributeError) as exc:
        raise budget.BudgetError("malformed CONTROL transport evidence") from exc


def _verify_originals(originals,limits,*,contract_sha256):
    validate(limits)
    if set(originals)!={"version","financial","control"} or originals["version"]!=VERSION:
        raise budget.BudgetError("missing CONTROL originals")
    values={n:fleet_json.loads(base64.b64decode(v,validate=True)) for n,v in originals["control"].items()}
    if "revoked.json" in values:raise budget.BudgetError("revoked CONTROL ledger cannot support acceptance")
    if control.pin(values.get("contract.json"))!=control.pin(limits):raise budget.BudgetError("foreign CONTROL budget")
    approval_check(values["approval.json"],limits)
    summary,rows=budget.verify_originals(originals["financial"],limits["financial"])
    names={"contract.json","approval.json"}
    generations={}
    for row in rows:
        r=row["reservation"];call=r["id"]
        from fleet_harness_backend import TOOLS
        if control.pin(row["payload"])!=control.pin(wire.payload(row["payload"]["messages"],TOOLS,version=limits["wire_version"])):
            raise budget.BudgetError("provider payload differs from fixed thinking/tools protocol")
        names.update(p+"/"+call+".json" for p in ("authority","sends","outcomes"))
        authority=values["authority/"+call+".json"];sent=values["sends/"+call+".json"];observed=values["outcomes/"+call+".json"]
        generation=authority["generation"];clock=authority["clock"];launch=authority["launch"];plan=limits["control_plan"]
        if contract_sha256 is not None and authority["owner_contract_sha256"]!=contract_sha256:
            raise budget.BudgetError("provider request belongs to another owner contract")
        old=generations.setdefault(generation["id"],control.pin(generation))
        if (old!=control.pin(generation) or control.pin(clock)!=control.pin(limits["control_clock"])
                or generation["plan_sha256"]!=control.pin(plan) or generation["root_identity"]!=plan["root_identity"]
                or generation["boot"]!=clock["boot"] or generation["clock_sha256"]!=control.pin(clock)
                or clock["plan_sha256"]!=control.pin(plan) or clock["deadline_at"]!=clock["started_at"]+plan["seconds"]
                or clock["monotonic_deadline"]!=clock["monotonic_started"]+plan["seconds"]
                or launch["generation_sha256"]!=control.pin(generation) or control.pin(launch["launch"])!=control.pin(plan["launch"])
                or authority["approval_sha256"]!=control.pin(values["approval.json"])):
            raise budget.BudgetError("foreign CONTROL generation/launch/clock")
        control.verify_launch(launch,generation,plan)
        https.validate(sent["policy"],now=sent["at"])
        if (sent["call"]!=call or sent["payload_sha256"]!=r["payload_sha256"] or sent["authority_sha256"]!=control.pin(authority)
                or sent["mode"]!=limits["mode"] or not clock["started_at"]<=sent["at"]<clock["deadline_at"]
                or not clock["monotonic_started"]<=sent["monotonic_at"]<clock["monotonic_deadline"]
                or observed["call"]!=call or observed["send_sha256"]!=control.pin(sent)):
            raise budget.BudgetError("send/outcome lacks exact CONTROL authority")
        outcome=observed["outcome"];body=base64.b64decode(outcome["body_b64"],validate=True)
        if (outcome["response_complete"] is not True or outcome["transport_closed"] is not True
                or outcome["error"] is not None or outcome["transport_error"] is not None
                or outcome["cleanup_errors"]!=[] or outcome["guardian_errors"]!=[]
                or outcome["body_truncated"] is not False or type(outcome["observed_body_bytes"]) is not int
                or outcome["observed_body_bytes"]!=len(body) or len(body)>1024*1024):
            raise budget.BudgetError("owned transport did not complete cleanly")
        financial_response=fleet_json.loads(base64.b64decode(originals["financial"]["responses/"+call+".json"],validate=True))
        financial_send=fleet_json.loads(base64.b64decode(originals["financial"]["sends/"+call+".json"],validate=True))
        if (outcome["http_status"]!=financial_response["http_status"] or outcome["body_b64"]!=financial_response["body_b64"]
                or sent["at"]!=financial_send["at"]):raise budget.BudgetError("accounting response differs from transport original")
    if names!=set(values):raise budget.BudgetError("CONTROL originals omitted/added calls")
    return summary,rows
