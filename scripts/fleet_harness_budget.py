"""One durable request authority shared by all attempts of a harness task.

No retrying HTTP client. An intent without a retained response is ambiguous:
reconcile that logical call, retain its reservation, never silently resend it.
Amounts are conservative estimates in integer nano-USD, never billing evidence.
"""
from __future__ import annotations

import base64
import copy
import http.client
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit
import uuid

import fleet_json
import fleet_safe_paths as safe
import fleet_artifacts as artifacts
from fleet_harness_sandbox import digest, publish


class BudgetError(ValueError):
    pass


class ReconcileRequired(BudgetError):
    pass


def integer(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise BudgetError("invalid exact nonnegative integer budget/usage")
    return value


def contract(*, cycle_id, deadline_at, max_requests=32, token_cap=2_000_000,
             estimated_cap_nano_usd=2_000_000_000, reserve_tokens_per_request=65536,
             reserve_nano_usd_per_request=100_000_000, request_policy=None):
    if str(uuid.UUID(cycle_id)) != cycle_id or type(deadline_at) not in (int, float) or not 0 < deadline_at < float("inf"):
        raise BudgetError("invalid cycle/deadline")
    policy = {"version": "deepseek-json-request-v1", "model": "deepseek-flash", "max_output_tokens": 8192,
              "input_token_margin": 1024, "nano_usd_per_token": 1500, "pricing_evidence": "synthetic-assumption"} if request_policy is None else copy.deepcopy(request_policy)
    if (not isinstance(policy, dict) or set(policy) != {"version", "model", "max_output_tokens", "input_token_margin", "nano_usd_per_token", "pricing_evidence"}
            or policy["version"] != "deepseek-json-request-v1" or policy["model"] != "deepseek-flash"
            or not isinstance(policy["pricing_evidence"], str)):
        raise BudgetError("unsupported request policy")
    for key, maximum in (("max_output_tokens", 65536), ("input_token_margin", 65536), ("nano_usd_per_token", 10**8)):
        if integer(policy[key], maximum) == 0: raise BudgetError("invalid request policy bound")
    result = {"version": "harness-budget-v1", "cycle_id": cycle_id, "deadline_at": deadline_at, "request_policy": policy,
              "max_requests": integer(max_requests, 1000), "token_cap": integer(token_cap, 10**9),
              "estimated_cap_nano_usd": integer(estimated_cap_nano_usd, 10**12),
              "reserve_tokens_per_request": integer(reserve_tokens_per_request, 10**7),
              "reserve_nano_usd_per_request": integer(reserve_nano_usd_per_request, 10**10)}
    if any(result[k] == 0 for k in result if k not in {"version", "cycle_id", "deadline_at", "request_policy"}):
        raise BudgetError("zero authority must not enable a request")
    if result["reserve_nano_usd_per_request"] < result["reserve_tokens_per_request"]*policy["nano_usd_per_token"]:
        raise BudgetError("monetary reservation must cover the entire token reservation at the pinned estimate")
    return result


class Ledger:
    def __init__(self, root, limits):
        self.root = Path(root); self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        expected = contract(**{k: v for k, v in limits.items() if k != "version"})
        if fleet_json.canonical_bytes(expected) != fleet_json.canonical_bytes(limits):
            raise BudgetError("budget contract changed")
        self.limits = copy.deepcopy(limits)
        self.publish("contract.json", limits)
        self.recover()

    def publish(self, path, value):
        raw = fleet_json.canonical_bytes(value)
        artifacts.put_bytes(self.root, self.limits["cycle_id"], raw)
        publish(self.root, path, value)

    def recover(self):
        """Finish only journal publications whose exact bytes already exist in CAS.

        Recovery never returns permission to send a previously reserved call.
        """
        with safe.RootedFS(self.root) as fs:
            with fs.exclusive_lock(".budget.lock", directory_modes=()):
                self._recover_locked(fs)

    def _recover_locked(self, fs):
        for directory in ("", "calls", "payloads", "responses", "sends"):
            if not (self.root / directory).exists(): continue
            modes = (0o700,) if directory else ()
            for name in fs.list_directory(directory, directory_modes=modes):
                if not name.startswith(".fleet-atomic-"): continue
                raw = artifacts.get_bytes(self.root, self.limits["cycle_id"], name[-68:-4])
                value = fleet_json.loads(raw)
                if directory:
                    leaf = value.get("id", value.get("call", "")) + ".json"
                else:
                    leaf = "contract.json" if value.get("version") == "harness-budget-v1" else "revoked.json"
                    if value.get("cycle_id") != self.limits["cycle_id"]:
                        raise BudgetError("foreign root publication")
                if name != safe._atomic_pending_name(leaf, raw):
                    raise BudgetError("pending publication has foreign identity")
                fs.atomic_write((directory + "/" if directory else "") + leaf, raw, directory_modes=modes)

    def read(self, path, optional=False):
        with safe.RootedFS(self.root) as fs:
            method = fs.read_regular_optional if optional else fs.read_regular
            raw = method(path, directory_modes=(0o700,) * (len(Path(path).parts) - 1), max_bytes=4 * 1024 * 1024)
            return None if raw is None else fleet_json.loads(raw)

    def entries(self):
        with safe.RootedFS(self.root) as fs:
            names = fs.list_directory("calls", directory_modes=(0o700,)) if (self.root / "calls").exists() else []
        if any(not name.endswith(".json") or len(name) != 69 for name in names):
            raise BudgetError("incomplete call publication requires recovery")
        return [self.read("calls/" + name) for name in names]

    def summary(self):
        rows = self.entries()
        reserved_tokens = sum(r["reserved_tokens"] for r in rows)
        reserved_cost = sum(r["reserved_nano_usd"] for r in rows)
        observations = [self.read("responses/" + r["id"] + ".json", optional=True)
                        if (self.root / "responses").exists() else None for r in rows]
        usage = [r.get("usage") if r else None for r in observations]
        # Reservations never disappear on incomplete response or restart. This
        # first profile deliberately does not release unused reservations.
        return {"admitted_requests": len(rows), "reserved_tokens": reserved_tokens,
                "reserved_estimated_nano_usd": reserved_cost,
                "observed_tokens": sum(u["total_tokens"] for u in usage) if usage and all(u is not None for u in usage) else None,
                "billed_cost_usd": None, "ambiguous": [r["id"] for r, observed in zip(rows, observations) if observed is None or not observed["response_complete"]]}

    def originals(self):
        """A cumulative snapshot; earlier repair reservations remain represented."""
        names = ["contract.json"]
        for directory in ("calls", "payloads", "responses", "sends"):
            names.extend(str(p.relative_to(self.root)) for p in sorted((self.root/directory).glob("*.json")))
        if (self.root/"revoked.json").exists():names.append("revoked.json")
        return {name:base64.b64encode((self.root/name).read_bytes()).decode() for name in names}

    def reserve(self, logical_id, admission, payload, *, now=None):
        if not isinstance(logical_id, str) or not logical_id or len(logical_id) > 256:
            raise BudgetError("invalid logical call identity")
        if not isinstance(admission, str) or len(admission) != 64 or any(c not in "0123456789abcdef" for c in admission):
            raise BudgetError("call lacks admission pin")
        raw = fleet_json.canonical_bytes(payload)
        if len(raw) > 1024 * 1024:
            raise BudgetError("payload exceeds byte bound")
        policy = self.limits["request_policy"]
        if (not isinstance(payload, dict) or payload.get("model") != policy["model"]
                or payload.get("stream", False) is not False or not isinstance(payload.get("messages"), list)):
            raise BudgetError("request does not match pinned provider/model/nonstream policy")
        output = integer(payload.get("max_tokens"), policy["max_output_tokens"])
        if not output:
            raise BudgetError("request lacks enforceable output cap")
        # UTF-8 JSON bytes are a deliberately conservative input-token upper
        # estimate; the margin covers provider message framing. It is not a
        # measurement or a universal tokenizer/billing guarantee.
        bound = len(raw) + policy["input_token_margin"] + output
        if (bound > self.limits["reserve_tokens_per_request"]
                or bound * policy["nano_usd_per_token"] > self.limits["reserve_nano_usd_per_request"]):
            raise BudgetError("request exceeds token/cost reservation under pinned estimate policy")
        call_id = digest(logical_id.encode())
        record = {"version": "harness-call-v1", "id": call_id, "logical_id": logical_id,
                  "cycle_id": self.limits["cycle_id"], "admission": admission, "payload_sha256": digest(raw),
                  "reserved_tokens": self.limits["reserve_tokens_per_request"],
                  "reserved_nano_usd": self.limits["reserve_nano_usd_per_request"]}
        with safe.RootedFS(self.root) as fs:
            with fs.exclusive_lock(".budget.lock", directory_modes=()):
                self._recover_locked(fs)
                rows = self.entries()
                existing = next((r for r in rows if r["id"] == call_id), None)
                if existing is not None:
                    if fleet_json.canonical_bytes(record) != fleet_json.canonical_bytes(existing):
                        raise BudgetError("logical identity reused with different content/admission")
                    raise ReconcileRequired("same logical call already reserved; no resend")
                if self.read("revoked.json", optional=True) is not None:
                    raise BudgetError("request authority revoked")
                if self.summary()["ambiguous"]:
                    raise ReconcileRequired("another logical call is ambiguous; reconcile before new admission")
                instant = time.time() if now is None else now
                if instant >= self.limits["deadline_at"]:
                    raise BudgetError("original deadline exhausted")
                if (len(rows) >= self.limits["max_requests"]
                        or sum(r["reserved_tokens"] for r in rows) + record["reserved_tokens"] > self.limits["token_cap"]
                        or sum(r["reserved_nano_usd"] for r in rows) + record["reserved_nano_usd"] > self.limits["estimated_cap_nano_usd"]):
                    raise BudgetError("shared request budget exhausted")
                self.publish("payloads/" + call_id + ".json", {"id": call_id, "payload": payload})
                self.publish("calls/" + call_id + ".json", record)
        return record

    def revoke(self, reason):
        with safe.RootedFS(self.root) as fs:
            with fs.exclusive_lock(".budget.lock", directory_modes=()):
                self._recover_locked(fs)
                existing = self.read("revoked.json", optional=True)
                if existing is not None:
                    return existing
                result = {"cycle_id": self.limits["cycle_id"], "reason": str(reason)[:500], "at": time.time(),
                          "status": "revocation_requested; in-flight transport still requires quiescence"}
                self.publish("revoked.json", result)
                return result

    def request(self, logical_id, admission, payload, *, endpoint, token=None, synthetic=False, fault=None, authorization=None):
        url = urlsplit(endpoint)
        if (url.username or url.password or url.query or url.fragment
                or (synthetic and (url.scheme != "http" or url.hostname != "127.0.0.1"))
                or (not synthetic and (url.scheme != "https" or url.hostname != "api.deepseek.com"))):
            raise BudgetError("endpoint outside frozen provider lane")
        if not synthetic:
            # A caller-supplied dictionary or credential is not a durable
            # Herdr CONTROL admission. This v1 authority is synthetic-only;
            # live needs its separately admitted transport/approval archive.
            raise BudgetError("live authority not admitted by Herdr CONTROL; harness-budget-v1 is synthetic-only")
        # Callers must separately carry live authorization. This module makes
        # no provider calls unless explicitly selected by CONTROL.
        record = self.reserve(logical_id, admission, payload)
        if fault: fault("after_reserve")
        raw = fleet_json.canonical_bytes(self.read("payloads/" + record["id"] + ".json")["payload"])
        if digest(raw) != record["payload_sha256"]:
            raise BudgetError("retained request bytes changed")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        remaining = self.limits["deadline_at"] - time.time()
        if remaining <= 0 or self.read("revoked.json", optional=True) is not None:
            raise ReconcileRequired("reserved call stopped before send; reservation retained")
        transport = http.client.HTTPConnection if synthetic else http.client.HTTPSConnection
        connection = transport(url.hostname, port=url.port, timeout=min(remaining, 30))
        data, response, completed = b"", None, False
        finished, interrupted = threading.Event(), threading.Event()
        absolute = time.monotonic() + max(0, remaining)
        def guard():
            while not finished.wait(min(0.025, max(0, absolute - time.monotonic()))):
                if time.monotonic() >= absolute or (self.root / "revoked.json").exists():
                    interrupted.set()
                    endpoint_socket = connection.sock
                    if endpoint_socket is None and response is not None and response.fp is not None:
                        endpoint_socket = getattr(getattr(response.fp, "raw", None), "_sock", None)
                    if endpoint_socket is not None:
                        try: endpoint_socket.shutdown(socket.SHUT_RDWR)
                        except OSError: pass
                    connection.close()
                    return
        watcher = threading.Thread(target=guard, name="harness-request-deadline", daemon=True)
        watcher.start()
        try:
            with safe.RootedFS(self.root) as fs:
                with fs.exclusive_lock(".budget.lock", directory_modes=()):
                    self._recover_locked(fs)
                    # This intent is the send-admission boundary. A concurrent
                    # revoke is a request, never a claim that in-flight I/O was
                    # remotely cancelled; quiescence must join this transport.
                    if interrupted.is_set() or time.time() >= self.limits["deadline_at"] or self.read("revoked.json", optional=True) is not None:
                        raise ReconcileRequired("revoked/deadline before send; reservation retained")
                    self.publish("sends/" + record["id"] + ".json", {"call": record["id"], "at": time.time()})
            if interrupted.is_set():
                raise ReconcileRequired("request interrupted before connect")
            connection.connect()
            connection.auto_open = 0  # A guardian close must never reconnect.
            if interrupted.is_set() or time.monotonic() >= absolute:
                raise ReconcileRequired("request interrupted before HTTP send")
            connection.request("POST", url.path or "/", body=raw, headers=headers)
            if fault: fault("after_send")
            response = connection.getresponse()
            while True:
                remaining = self.limits["deadline_at"] - time.time()
                if remaining <= 0 or interrupted.is_set():
                    raise TimeoutError("original total deadline exceeded")
                if connection.sock is not None:
                    connection.sock.settimeout(min(remaining, 30))
                block = response.read1(min(65536, 1024 * 1024 + 1 - len(data)))
                data += block
                if len(data) > 1024 * 1024:
                    raise ReconcileRequired("response exceeded bound; reservation retained")
                if time.time() >= self.limits["deadline_at"] or interrupted.is_set():
                    raise TimeoutError("original total deadline exceeded")
                if not block:
                    if response.length not in (None, 0):
                        raise http.client.IncompleteRead(data, response.length)
                    break
            completed = True
            usage = None
            try:
                value = fleet_json.loads(data)
                incoming = value.get("usage")
                if incoming is not None:
                    prompt = integer(incoming["prompt_tokens"], 10**8)
                    completion = integer(incoming["completion_tokens"], 10**8)
                    total = integer(incoming["total_tokens"], 10**8)
                    if total != prompt + completion: raise BudgetError("usage inconsistent")
                    usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
            retained = {"id": record["id"], "http_status": response.status,
                        "body_b64": base64.b64encode(data).decode(), "usage": usage,
                        "billed_cost_usd": None, "response_complete": True}
            self.publish("responses/" + record["id"] + ".json", retained)
            if response.status != 200:
                self.revoke("provider_http_" + str(response.status))
            if usage and usage["total_tokens"] > record["reserved_tokens"]:
                self.revoke("provider_usage_exceeded_reservation")
                raise BudgetError("observed usage exceeded bound; new admissions stopped")
            if fault: fault("after_response")
            return retained
        except (OSError, http.client.HTTPException, ReconcileRequired) as exc:
            if not completed:
                retained = {"id": record["id"], "http_status": response.status if response else None,
                    "body_b64": base64.b64encode(data).decode(), "usage": None, "billed_cost_usd": None,
                    "response_complete": False, "error": type(exc).__name__}
                self.publish("responses/" + record["id"] + ".json", retained)
            raise ReconcileRequired("transport incomplete; preserve reservation and reconcile") from exc
        finally:
            finished.set()
            connection.close()
            watcher.join(timeout=1)

    def reconcile(self, logical_id):
        call_id = digest(logical_id.encode())
        if not (self.root / "responses").exists():
            return {"status": "indeterminate", "reservation_retained": True}
        result = self.read("responses/" + call_id + ".json", optional=True)
        return {"status": "response_retained", "response": result} if result and result["response_complete"] else {
            "status": "indeterminate", "reservation_retained": True, "partial_response": result}


def verify_originals(originals, limits):
    """Pure replay of retained request/response bytes, never an HTTP operation."""
    values = {name:fleet_json.loads(base64.b64decode(raw, validate=True)) for name,raw in originals.items()}
    if "revoked.json" in values:raise BudgetError("revoked request authority cannot support acceptance")
    same = lambda a,b: fleet_json.canonical_bytes(a) == fleet_json.canonical_bytes(b)
    if not same(values.get("contract.json"), limits): raise BudgetError("foreign budget contract")
    expected = contract(**{k:v for k,v in limits.items() if k!="version"})
    if not same(expected,limits): raise BudgetError("invalid original limits")
    rows = [value for name,value in sorted(values.items()) if name.startswith("calls/")]
    names = {"contract.json"}; usages=[]; observed=[]
    for row in rows:
        call = row["id"]
        expected_row = {"version":"harness-call-v1", "id":digest(row["logical_id"].encode()),
            "logical_id":row["logical_id"], "cycle_id":limits["cycle_id"], "admission":row["admission"],
            "payload_sha256":row["payload_sha256"], "reserved_tokens":limits["reserve_tokens_per_request"],
            "reserved_nano_usd":limits["reserve_nano_usd_per_request"]}
        if not same(row,expected_row) or len(row["admission"])!=64: raise BudgetError("foreign request reservation")
        names.update(f"{directory}/{call}.json" for directory in ("calls","payloads","responses","sends"))
        body=values[f"payloads/{call}.json"]
        raw=fleet_json.canonical_bytes(body["payload"]); payload=body["payload"]; policy=limits["request_policy"]
        if set(body)!={"id","payload"} or body["id"]!=call or digest(raw)!=row["payload_sha256"]:
            raise BudgetError("request differs from reserved bytes")
        output=integer(payload.get("max_tokens"),policy["max_output_tokens"])
        bound=len(raw)+policy["input_token_margin"]+output
        if (not output or payload.get("model")!=policy["model"] or payload.get("stream",False) is not False
                or bound>row["reserved_tokens"] or bound*policy["nano_usd_per_token"]>row["reserved_nano_usd"]):
            raise BudgetError("request escaped reserved limits")
        sent=values[f"sends/{call}.json"]; response=values[f"responses/{call}.json"]
        if (set(sent)!={"call","at"} or sent["call"]!=call or type(sent["at"]) not in (int,float)
                or not 0<sent["at"]<limits["deadline_at"] or response["id"]!=call
                or response["response_complete"] is not True or type(response["http_status"]) is not int
                or response["billed_cost_usd"] is not None): raise BudgetError("incomplete or foreign response")
        response_raw=base64.b64decode(response["body_b64"],validate=True)
        try:response_body=fleet_json.loads(response_raw)
        except ValueError:response_body=None  # Complete malformed output can consume a protocol repair.
        usage=None
        incoming=response_body.get("usage") if isinstance(response_body,dict) else None
        try:
            if incoming is not None:
                usage={key:integer(incoming[key],10**8) for key in ("prompt_tokens","completion_tokens","total_tokens")}
                if usage["total_tokens"]!=usage["prompt_tokens"]+usage["completion_tokens"]: usage=None
        except (ValueError,KeyError,TypeError): usage=None
        if not same(usage,response["usage"]): raise BudgetError("usage differs from provider original")
        if response["http_status"]!=200 or usage is not None and usage["total_tokens"]>row["reserved_tokens"]:
            raise BudgetError("provider rejection or usage overrun revokes acceptance authority")
        usages.append(usage)
        observed.append({"reservation":row,"payload":payload,"response":response_body,"http_status":response["http_status"]})
    if names!=set(values): raise BudgetError("budget originals omitted or added calls")
    tokens=sum(r["reserved_tokens"] for r in rows);cost=sum(r["reserved_nano_usd"] for r in rows)
    if len(rows)>limits["max_requests"] or tokens>limits["token_cap"] or cost>limits["estimated_cap_nano_usd"]:
        raise BudgetError("cumulative request budget exceeded")
    return {"admitted_requests":len(rows),"reserved_tokens":tokens,"reserved_estimated_nano_usd":cost,
        "observed_tokens":sum(u["total_tokens"] for u in usages) if usages and all(u is not None for u in usages) else None,
        "billed_cost_usd":None,"ambiguous":[]}, observed
