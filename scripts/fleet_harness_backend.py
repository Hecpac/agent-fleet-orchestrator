"""Bound local harness backend. No Herdr/global/live transport gate is opened.

Native Mini and commands occupy different owned containers. The owner journal
remains the only admission/repair/acceptance authority. Recovery replays only
retained pure Mini transitions; provider sends and command effects are reconciled.
"""
import base64
import copy
import io
from pathlib import Path
import tarfile
import time

import fleet_harness_acceptance as checker
import fleet_harness_budget as budget
import fleet_harness_contract as contract_module
import fleet_harness_delivery as delivery
import fleet_harness_executor as executor
import fleet_harness_functional as functional
import fleet_harness_mini as mini
import fleet_harness_sandbox as sandbox
import fleet_herdr_owner_runtime as runtime
import fleet_herdr_work_packet as work
import fleet_json
import fleet_safe_paths as safe
import fleet_harness_read_scope as read_scope

TOOLS = [{"type":"function", "function":{"name":"bash", "description":"Run a command in the owned scope-limited executor",
          "parameters":{"type":"object", "properties":{"command":{"type":"string"}}, "required":["command"], "additionalProperties":False}}}]


class LocalHarnessBackend:
    supports_functional = True

    def __init__(self, cycle, *, endpoint, dependencies, source_admissions=(), fault=None):
        self.cycle = cycle
        self.contract = contract_module.validate(cycle.json(cycle.pin))
        if "public_read" not in self.contract["prepared"]["sources"]:
            raise ValueError("legacy harness is read-only; new execution requires explicit public-read authority")
        self.root = (cycle.runs / cycle.prefix / "local-backend").resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.dependencies = Path(dependencies).resolve(strict=True)
        # This backend is provider-free by contract, not an alias for a paid lane.
        from urllib.parse import urlsplit
        parsed = urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1": raise ValueError("local conformance requires synthetic loopback provider")
        sandbox.publish(self.root, "configuration.json", {"endpoint":endpoint,
            "dependencies":mini.dependency_manifest(dependencies), "contract_sha256":cycle.pin})
        self.endpoint, self.source_admissions, self.fault = endpoint, list(source_admissions), fault
        self.ledger = budget.Ledger(self.root / "budget", self.contract["request_budget"])
        self.controls = {}
        self._reconcile_cancel()

    def _reconcile_cancel(self):
        control=self.cycle.load()["control"]
        if control and control["action"]=="cancel":self.ledger.revoke("cycle_cancel_requested")

    def _read(self, root, name, optional=False):
        with safe.RootedFS(root) as fs:
            fn = fs.read_regular_optional if optional else fs.read_regular
            raw = fn(name, directory_modes=(0o700,)*(len(Path(name).parts)-1), max_bytes=64*1024*1024)
            return None if raw is None else fleet_json.loads(raw)

    def _fault(self, name):
        if self.fault: self.fault(name)

    def _stopped(self):
        return time.time() >= self.contract["deadline_at"] or (self.ledger.root / "revoked.json").exists()

    def _attempt(self, admission):
        from fleet_herdr_owner_contract import validate_admission
        validate_admission(admission, self.contract)
        root = self.root / "attempts" / work.digest(admission)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return root

    def send(self, admission, task):
        if work.digest(task) != admission["prompt_sha256"]: raise ValueError("task differs from admission")
        root = self._attempt(admission)
        sandbox.publish(root, "intent.json", {"admission":admission, "task":task,"input_snapshot":self._input_pin(admission)})
        self._drive(admission)

    def _input_pin(self, admission):
        attempt=next((a for a in self.cycle.load()["attempts"] if a["admission"]==admission),None)
        if attempt is None or attempt.get("input_snapshot") is None:
            raise read_scope.InputBindingError("missing immutable admission input")
        return attempt["input_snapshot"]

    def _advance_files(self, expected, tools):
        for tool in tools:
            originals=tool["originals"]
            intent=fleet_json.loads(base64.b64decode(originals["intent.json"],validate=True))
            publication=fleet_json.loads(base64.b64decode(originals["publication.json"],validate=True))
            spec=self.contract["prepared"]["sources"]["scope"]
            read_scope.require_files(read_scope.editable_files(intent["projection_before"]["inventory"],spec,mode=0o666),expected)
            expected=read_scope.editable_files(publication["projection_after"]["inventory"],spec,mode=0o666)
        return expected

    def _control(self, root, admission, task):
        key = work.digest(admission)
        if key in self.controls: return self.controls[key]
        controls = root / "controls"; controls.mkdir(mode=0o700, exist_ok=True)
        old_roots = sorted(controls.iterdir())
        # Resource cleanup is required before restoring the same execution.
        for old in old_roots:
            if (old / "resource/resource.json").exists() and not (old / "resource/cleanup.json").exists():
                record = self._read(old / "resource", "resource.json")
                sandbox.Sandbox(old / "resource", owner=record["owner"]).cleanup()
        control = mini.MiniControl(controls / f"{len(old_roots)+1:04d}", owner=admission["generation"], dependencies=self.dependencies)
        self.controls[key] = control
        control.start(task)
        steps = root / "steps"
        if steps.exists():
            expected=read_scope.editable_files(self.cycle.json(self._input_pin(admission)),self.contract["prepared"]["sources"]["scope"])
            completed=[s for s in sorted(steps.iterdir()) if (s/"native.json").exists()]
            if completed: control.restore(self._read(completed[-1],"native.json"))
            for step in sorted(steps.iterdir()):
                if not step.is_dir(): raise ValueError("unknown step residue")
                if (step / "native.json").exists():
                    expected=self._advance_files(expected,self._read(step,"tools.json"))
                    continue
                response = self._read(step, "response.json", optional=True)
                if response is None: break
                control.query(response)
                outputs = self._read(step, "outputs.json", optional=True)
                if outputs is None: break
                expected=self._restore_tools(step,admission,response,outputs,expected_before=expected)
                control.observe(outputs)
                sandbox.publish(step,"native.json",control.trajectory)
        self.controls[key] = control
        return control

    def _drive(self, admission):
        self._reconcile_cancel()
        root = self._attempt(admission)
        if (root / "delivery.json").exists(): return
        intent = self._read(root, "intent.json", optional=True)
        if intent is None: return  # Ambiguous send with no durable backend admission cannot be repeated.
        if work.digest(intent["admission"]) != work.digest(admission): raise ValueError("foreign backend intent")
        control = None
        try:
            input_pin=self._input_pin(admission)
            if intent.get("input_snapshot")!=input_pin:raise read_scope.InputBindingError("backend intent changed its admission input")
            initial=self.cycle.json(input_pin);spec=self.contract["prepared"]["sources"]["scope"]
            expected_before=read_scope.editable_files(initial,spec)
            if not list(root.glob("steps/*/commands/*/intent.json")):
                from fleet_herdr_scope import capture
                if capture(Path(initial["root"]),spec)!=initial:
                    raise read_scope.InputBindingError("candidate changed before the first admitted execution")
            control = self._control(root, admission, intent["task"])
            for index in range(1, 33):
                if control.trajectory["info"]["exit_status"]: break
                if self._stopped(): raise budget.BudgetError("cycle cancellation/deadline/revocation")
                step = root / "steps" / f"{index:04d}"; step.mkdir(mode=0o700, parents=True, exist_ok=True)
                if (step / "native.json").exists():
                    expected_before=self._advance_files(expected_before,self._read(step,"tools.json"))
                    continue
                logical = work.digest(admission) + "/query/" + str(index)
                response = self._read(step, "response.json", optional=True)
                if response is None:
                    payload = {"model":"deepseek-flash", "max_tokens":8192, "stream":False,
                        "messages":[{k:v for k,v in m.items() if k in {"role","content","tool_calls","tool_call_id"}}
                                    for m in control.trajectory["messages"]], "tools":TOOLS,
                        "thinking":{"type":"enabled"}, "reasoning_effort":"max"}
                    reconciled = self.ledger.reconcile(logical)
                    if reconciled["status"] == "response_retained":
                        retained = reconciled["response"]
                    else:
                        retained = self.ledger.request(logical, work.digest(admission), payload,
                            endpoint=self.endpoint, synthetic=True, fault=self.fault)
                    if retained["http_status"] != 200: raise budget.BudgetError("provider rejected request")
                    response = fleet_json.loads(base64.b64decode(retained["body_b64"], validate=True))
                    sandbox.publish(step, "response.json", response)
                if control.trajectory["messages"][-1]["role"] != "assistant":
                    actions = control.query(response)
                else:
                    actions = mini.validate_batch(response, set())
                outputs, tools = [], []
                for action in actions:
                    self._reconcile_cancel()
                    if self._stopped(): raise budget.BudgetError("execution revoked before tool")
                    bound = {"admission_sha256":work.digest(admission), "tool_call_id":action["tool_call_id"]}
                    command = step / "commands" / mini.digest(action["tool_call_id"].encode())
                    result = executor.execute(self.contract["prepared"]["execution_envelope"]["candidate_repo"],
                        self.contract["prepared"]["sources"]["scope"], action, command, binding=bound,
                        deadline_at=self.contract["deadline_at"],
                        public_read=self.contract["prepared"]["sources"]["public_read"],
                        expected_before=expected_before,
                        public_checks=checker.public_projection(fleet_json.loads(self.contract["prepared"]["sources"]["functional_tests"])))
                    self._fault("after_write")
                    output = {k:result[k] for k in ("tool_call_id", "output", "returncode")}
                    outputs.append(output)
                    tools.append({"action":action, "binding":bound, "output":output,
                                  "cleanup":self._read(command / "resource", "cleanup.json"),
                                  "originals":{n:base64.b64encode(r).decode() for n,r in functional.collect(command).items()}})
                    expected_before=self._advance_files(expected_before,[tools[-1]])
                sandbox.publish(step, "tools.json", tools)
                sandbox.publish(step, "outputs.json", outputs)
                if control.trajectory["messages"][-1]["role"] == "assistant": control.observe(outputs)
                sandbox.publish(step, "native.json", control.trajectory)
                if control.trajectory["info"]["exit_status"]: break
            terminal = control.terminal()
            controls = self._cleanup(root, admission)
            tools = [item for step in sorted((root / "steps").iterdir()) for item in (self._read(step, "tools.json", optional=True) or [])]
            value = {"version":delivery.VERSION, "admission":admission, "task":intent["task"], "error":None,
                "terminal":terminal, "native_terminal_b64":base64.b64encode((control.root / f"native/{control.ordinal:04d}.json").read_bytes()).decode(),
                "controls":controls, "tools":tools, "budget":self.ledger.summary(),"budget_originals":self.ledger.originals()}
            sandbox.publish(root, "delivery.json", value)

            self._fault("after_delivery")
        except read_scope.InputBindingError as exc:
            self._cleanup(root,admission)
            sandbox.publish(root,"dependency.json",{"reason":"admission_input_changed; no refresh or new attempt","detail":str(exc)})
        except budget.ReconcileRequired:
            if control: self._cleanup(root, admission)
            sandbox.publish(root, "dependency.json", {"reason":"provider_send_indeterminate; retained reservation; no resend"})
        except (ValueError, OSError, RuntimeError) as exc:
            controls = self._cleanup(root, admission)
            value = {"version":delivery.VERSION, "admission":admission, "task":intent["task"],
                "error":type(exc).__name__+":"+str(exc)[:500], "controls":controls, "terminal":None,
                "budget":self.ledger.summary(),"budget_originals":self.ledger.originals()}
            sandbox.publish(root, "delivery.json", value)

    def _restore_tools(self,step,admission,response,outputs,*,expected_before):
        tools=[]
        actions=mini.validate_batch(response,set())
        if len(actions)!=len(outputs):raise ValueError("restored step observations differ")
        for action,output in zip(actions,outputs):
            command=step/"commands"/mini.digest(action["tool_call_id"].encode())
            bound={"admission_sha256":work.digest(admission),"tool_call_id":action["tool_call_id"]}
            result=executor.reconcile(self.contract["prepared"]["execution_envelope"]["candidate_repo"],command,binding=bound,
                public_read=self.contract["prepared"]["sources"]["public_read"],expected_before=expected_before,
                public_checks=checker.public_projection(fleet_json.loads(self.contract["prepared"]["sources"]["functional_tests"])))
            if {k:result[k] for k in ("tool_call_id","output","returncode")}!=output:raise ValueError("restored command output differs")
            tools.append({"action":action,"binding":bound,"output":output,"cleanup":self._read(command/"resource","cleanup.json"),
                          "originals":{n:base64.b64encode(r).decode() for n,r in functional.collect(command).items()}})
            expected_before=self._advance_files(expected_before,[tools[-1]])
        sandbox.publish(step,"tools.json",tools)
        return expected_before

    def _cleanup(self, root, admission):
        control = self.controls.pop(work.digest(admission), None)
        if control: control.cleanup()
        receipts = []
        for item in sorted((root / "controls").iterdir()) if (root / "controls").exists() else []:
            resource = item / "resource"
            if not (resource / "resource.json").exists(): continue
            record = self._read(resource, "resource.json")
            receipt = sandbox.Sandbox(resource, owner=record["owner"]).cleanup()
            if receipt["extra_stdout"] or not receipt["bounded_output"]: raise ValueError("unreconciled native output")
            receipts.append({"cleanup":receipt,"originals":{n:base64.b64encode(r).decode() for n,r in functional.collect(item).items()}})
        for resource in sorted((root / "steps").glob("*/commands/*/resource")) if (root / "steps").exists() else []:
            if not (resource / "resource.json").exists(): continue
            record = self._read(resource, "resource.json")
            sandbox.Sandbox(resource, owner=record["owner"]).cleanup()
        sandbox.publish(root,"quiescence.json",{"resource":runtime.resource(admission),"inactive":True,"resources_clean":True})
        return receipts

    def poll(self, admission):
        root = self._attempt(admission)
        if not (root / "dependency.json").exists(): self._drive(admission)
        value = self._read(root, "delivery.json", optional=True)
        quiescence = self._read(root,"quiescence.json",optional=True)
        if value is None: return {"response":None, "quiescence":quiescence}
        return {"response":{"transcript":fleet_json.canonical_bytes(value),
                "final":value["terminal"]["final"].encode() if value["terminal"] else b""},
                "quiescence":quiescence}

    def cancel(self, resource):
        self.ledger.revoke("cycle_cancel_requested")
        current = self.cycle.load()
        admission = next(a["admission"] for a in current["attempts"] if runtime.resource(a["admission"]) == resource)
        self._cleanup(self._attempt(admission), admission)

    def start_check(self, binding, tree, tests):
        self._reconcile_cancel()
        root = self.root / "checks" / work.digest(binding); root.mkdir(mode=0o700, parents=True, exist_ok=True)
        sandbox.publish(root, "intent.json", binding)
        self._start_check_at(binding,tree,tests,self._check_root(binding))

    def _check_root(self,binding):
        parent=self.root/"checks"/work.digest(binding)
        root=parent/"check"
        for index,path in enumerate(sorted(parent.glob("recovery-*.json")),1):
            if path.name!=f"recovery-{index:04d}.json" or index>2:raise ValueError("verifier recovery bound or sequence differs")
            record=self._read(parent,path.name)
            if record["binding_sha256"]!=work.digest(binding) or record["previous_root"]!=str(root):raise ValueError("foreign verifier recovery")
            functional.verify_recovery_original(binding,{n:base64.b64decode(r,validate=True) for n,r in record["originals"].items()})
            root=parent/f"check-recovery-{index:04d}"
        return root

    def _start_check_at(self,binding,tree,tests,check_root):
        self._reconcile_cancel()
        root=check_root.parent
        candidate = root / "frozen"
        candidate.mkdir(mode=0o755, exist_ok=True)
        task = fleet_json.loads(tests)["task"]
        names = ("ledger.py", "paths.py") if task == "D1" else ("report.py",)
        with tarfile.open(fileobj=io.BytesIO(tree), mode="r:") as archive:
            for name in names:
                member = archive.getmember(name)
                if not member.isfile() or member.size > 4*1024*1024: raise ValueError("invalid frozen candidate")
                (candidate / name).write_bytes(archive.extractfile(member).read()); (candidate / name).chmod(0o644)
        manifest = checker.source_manifest(candidate, task)
        review = next((r for r in self.source_admissions if r["manifest"] == manifest), None)
        self._fault("after_freeze")
        records=[self._read(root,p.name) for p in sorted(root.glob("recovery-*.json"))]
        checker.run(candidate, fleet_json.loads(tests), check_root, binding=binding, source_admission=review,
                    deadline_at=binding["deadline_at"],cancelled=self._stopped,fault=self.fault,recovery=records)
        self._fault("after_check")

    def poll_check(self, binding):
        self._reconcile_cancel()
        root = self._check_root(binding)
        if not (root/"contract.json").exists() and not (root/"resource").exists() and not self._stopped():
            # Durable owner functional intent already binds this exact frozen
            # tree. No checker effect was admitted: finish its pre-start gap.
            self.start_check(binding,self.cycle.get(binding["tree"]),self.contract["prepared"]["sources"]["functional_tests"].encode())
        elif not (root/"result.json").exists() and (root/"contract.json").exists() and not self._stopped():
            # A lost verifier process cannot resume D1's in-memory ledger.
            # Quiesce its exact resource, retain the partial originals, and
            # replay the frozen tree in a bounded new verifier generation.
            if (root/"resource/resource.json").exists():
                record=self._read(root/"resource","resource.json")
                sandbox.Sandbox(root/"resource",owner=record["owner"]).cleanup()
            originals=functional.collect(root)
            if "resource/resource.json" in originals:
                from fleet_harness_resource_evidence import verify_abandoned
                verify_abandoned(originals)
                if checker.finalize_failed_prefix(root,binding=binding,originals=originals) is not None:
                    return self.poll_check(binding)
            functional.verify_recovery_original(binding,originals)
            index=len(list(root.parent.glob("recovery-*.json")))+1
            if index>2:raise ValueError("same-check verifier recovery limit exhausted")
            sandbox.publish(root.parent,f"recovery-{index:04d}.json",{"binding_sha256":work.digest(binding),
                "previous_root":str(root),"originals":{n:base64.b64encode(r).decode() for n,r in originals.items()}})
            root=self._check_root(binding)
            self._start_check_at(binding,self.cycle.get(binding["tree"]),self.contract["prepared"]["sources"]["functional_tests"].encode(),root)
        if not (root / "result.json").exists():
            if not (root/"resource/resource.json").exists() and self._stopped():
                # create() publishes resource intent before the first Docker
                # effect. The driver lock excludes a concurrent creator.
                return {"binding_sha256":work.digest(binding),"outcome":None,
                    "quiescence":{"resource":functional.resource(binding),"inactive":True,"resources_clean":True}}
            clean=self._read(root / "resource","cleanup.json",optional=True) if (root / "resource").exists() else None
            quiescence=None
            if clean:
                from fleet_harness_resource_evidence import verify_abandoned
                record=verify_abandoned(functional.collect(root))
                retained=self._read(root,"contract.json")
                if work.digest(retained["binding"])!=work.digest(binding):raise ValueError("foreign partial check cleanup")
                quiescence={"resource":functional.resource(binding),"inactive":True,"resources_clean":True}
            return {"binding_sha256":work.digest(binding), "outcome":None, "quiescence":quiescence}
        result = checker.verify(root, expected_binding=binding)
        originals=functional.collect(root)
        if result["cleanup"] is True:
            from fleet_harness_resource_evidence import verify_abandoned
            verify_abandoned(originals)
        retained=self._read(root,"contract.json")
        status=functional.reviewed_status(binding,result,retained,originals)
        if status=="blocked" and result["pending"]==["independent_source_admission_required"]:
            # A separate, hash-bound review may arrive after execution. Retain
            # the original blocked checker; don't send a replaceable receipt.
            return {"binding_sha256":work.digest(binding),"outcome":None,
                "quiescence":{"resource":functional.resource(binding),"inactive":True,"resources_clean":True}}
        return {"binding_sha256":work.digest(binding),
            "outcome":{"status":status, "reason":"versioned_contract_probes", "evidence":originals},
            "quiescence":{"resource":functional.resource(binding), "inactive":True, "resources_clean":True} if result["cleanup"] else None}

    def register_source_review(self,binding,review):
        root=self._check_root(binding)
        original=self._read(root,"contract.json")
        if work.digest(original["binding"])!=work.digest(binding):raise ValueError("source review targets another check")
        checker.validate_source_admission(review,original["sources"])
        sandbox.publish(root,"source-review.json",{"version":"source-review-supplement-v1",
            "binding_sha256":work.digest(binding),"original_contract_sha256":work.digest(original),"review":review})

    def cancel_check(self, resource):
        bound=self.cycle.json(resource["binding_sha256"])
        root = self._check_root(bound)/"resource"
        if not (root/"resource.json").exists():return  # No resource effect admitted under this driver.
        record = self._read(root, "resource.json")
        sandbox.Sandbox(root, owner=record["owner"]).cleanup()
