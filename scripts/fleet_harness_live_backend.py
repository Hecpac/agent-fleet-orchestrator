"""Owned Herdr CONTROL backend reusing Mini, executor, freeze and repair code."""
import base64
from pathlib import Path
import time
import uuid

import fleet_harness_backend as local
import fleet_harness_budget as financial
import fleet_harness_control as control
import fleet_harness_delivery as delivery
import fleet_harness_https as https
import fleet_harness_live_budget as budget
import fleet_harness_live_contract as contract
import fleet_harness_mini as mini
import fleet_harness_provider_protocol as wire
import fleet_harness_sandbox as sandbox
import fleet_json
import fleet_herdr_work_packet as work
import fleet_herdr_owner_runtime as runtime
import fleet_safe_paths as safe


class ControlHarnessBackend(local.LocalHarnessBackend):
    delivery_version=delivery.CONTROL_VERSION

    def __init__(self,cycle,*,guard,approval,dependencies,source_admissions=(),fault=None,credential=None):
        self.cycle=cycle;self.contract=contract.validate(cycle.json(cycle.pin));self.guard=guard
        self.root=(cycle.runs/cycle.prefix/"local-backend").resolve()
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.dependencies=Path(dependencies).resolve(strict=True)
        self.source_admissions=list(source_admissions);self.fault=fault;self.controls={}
        self.ledger=budget.Ledger(guard,self.contract["request_budget"],approval=approval,cycle=cycle)
        self.credential=credential
        if self.ledger.limits["mode"]=="synthetic_tls" and credential is not None:
            raise ValueError("synthetic CONTROL must not load a provider credential")
        sandbox.publish(self.root,"configuration.json",{"version":"owned-herdr-control-v1","dependencies":mini.dependency_manifest(dependencies),
            "contract_sha256":cycle.pin,"control_plan_sha256":guard.plan_sha256,"credential_serialized":False})
        self._reconcile_cancel()

    def _payload(self,trajectory):return wire.payload(trajectory["messages"],local.TOOLS,version=self.ledger.limits["wire_version"])

    def _reconcile_cancel(self):
        self.guard.assert_owned()
        with safe.RootedFS(self.guard.root,root_mode=0o700) as fs:
            with fs.exclusive_lock(".control-send.lock",directory_modes=()):control.recover(self.guard.root,self.guard.plan)
        request=control.read(self.guard.root,"control-cancel.json",optional=True)
        state=self.cycle.load()
        if request and not state["terminal"] and not (state["control"] and state["control"]["action"]=="cancel"):
            target=runtime.resource(state["attempts"][-1]["admission"]) if state["attempts"] else None
            self.cycle.control("cancel",request_id=str(uuid.uuid5(uuid.UUID(request["id"]),self.cycle.cycle_id)),target=target,reason=request["reason"])
        super()._reconcile_cancel()

    def _transport_quiescent(self):return not self.guard._pending

    def _response(self,retained):
        # Original provider bytes remain immutable in Ledger v2. This versioned
        # projection is reproduced independently during terminal verification.
        return wire.normalize(fleet_json.loads(base64.b64decode(retained["body_b64"],validate=True)),version=self.ledger.limits["wire_version"])

    def _stopped(self):
        try:return self.ledger._stopped() or time.time()>=self.contract["deadline_at"]
        except control.AuthorityError:return True

    def _control(self,root,admission,task):
        if self._stopped():raise financial.ReconcileRequired("CONTROL stopped before resource admission")
        return super()._control(root,admission,task)

    def _request(self,logical,admission,payload):
        self.guard.assert_effect()
        if self.ledger.limits["mode"]=="synthetic_tls":
            now=time.time()
            # No DNS lookup in conformance. SyntheticTLS always uses the pinned
            # loopback endpoint, regardless of this public protocol placeholder.
            policy={"version":https.VERSION,"endpoint":https.ENDPOINT,"method":"POST","hostname":https.HOST,"port":443,
                "family":2,"address":"1.1.1.1","resolved_at":now,"expires_at":now+300,
                "owner":str(uuid.uuid4()),"resolution_sha256":sandbox.digest(b"synthetic-no-DNS"),"tls":"CERT_REQUIRED+check_hostname"}
        else:
            if not isinstance(self.credential,str) or not self.credential:
                raise financial.ReconcileRequired("CONTROL credential unavailable before DNS/reservation")
            remaining=min(self.contract["deadline_at"]-time.time(),self.guard.clock["monotonic_deadline"]-time.monotonic())
            policy=https.prepare(self.root/"dns"/sandbox.digest(logical.encode()),timeout=min(5,remaining))
        return self.ledger.request(logical,work.digest(admission),payload,policy=policy,token=self.credential,fault=self.fault)
