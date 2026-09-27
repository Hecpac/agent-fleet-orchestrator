"""Bounded CONTROL transport preparation; this grants no live authority.

DNS is a separate owned, time-limited process without credentials. A future
admitted live controller can bind its result and NumericHTTPS to its archive.
The current owner profile and request ledger remain synthetic-only.
"""
import copy
import http.client
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid

import fleet_control_runtime as process_runtime
import fleet_json
import fleet_harness_sandbox as sandbox

HOST="api.deepseek.com"
ENDPOINT="https://api.deepseek.com:443/chat/completions"
VERSION="numeric-https-preparation-v1"
RESOLVER='''import json,math,resource,signal,socket,sys,time
remaining=min(float(sys.argv[1])-time.time(),float(sys.argv[3])-time.monotonic())
if remaining<=0:raise SystemExit(124)
signal.signal(signal.SIGALRM,signal.SIG_DFL)
signal.alarm(max(1,math.ceil(remaining)))
resource.setrlimit(resource.RLIMIT_FSIZE,(65536,65536))
resource.setrlimit(resource.RLIMIT_CPU,(2,2))
addresses=sorted({(int(row[0]),row[4][0]) for row in socket.getaddrinfo('api.deepseek.com',443,type=socket.SOCK_STREAM)})
print(json.dumps({'addresses':addresses,'owner':sys.argv[2]},separators=(',',':')))
'''


def prepare(root,*,timeout=5):
    """One DNS observation. An incomplete directory is never silently retried.

    The OS alarm also bounds the helper if its parent dies. A crash before its
    process identity is durable is still an explicit reconciliation dependency;
    expiry alone is not a receipt of resource absence.
    """
    if type(timeout) not in (int,float) or not 0<timeout<=10:raise ValueError("DNS timeout outside preparation bound")
    root=Path(root).resolve();root.mkdir(mode=0o700,parents=True,exist_ok=False)
    owner=str(uuid.uuid4());started=time.time();deadline=started+timeout;monotonic_deadline=time.monotonic()+timeout
    sandbox.publish(root,"intent.json",{"owner":owner,"hostname":HOST,"port":443,"started_at":started,
        "deadline_at":deadline,"monotonic_deadline":monotonic_deadline,"resolver_sha256":sandbox.digest(RESOLVER.encode()),"credential_access":False})
    process=None;terminated=False
    with os.fdopen(os.open(root/"stdout.raw",os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),"wb") as stdout,os.fdopen(os.open(root/"stderr.raw",os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),"wb") as stderr:
        try:
            process=subprocess.Popen([sys.executable,"-I","-B","-c",RESOLVER,str(deadline),owner,str(monotonic_deadline)],
                stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,env={"PATH":"/usr/bin:/bin"},close_fds=True)
            birth,zombie=process_runtime.process_observation(process.pid)
            sandbox.publish(root,"process.json",{"owner":owner,"pid":process.pid,"birth":birth,"zombie_at_observation":zombie})
            process.wait(timeout=max(.001,min(deadline-time.time(),monotonic_deadline-time.monotonic())))
        finally:
            if process is not None:
                if process.poll() is None:process.kill();terminated=True
                process.wait(timeout=2)
                # Exact owned Popen child reaped; no PID-only cancellation.
                sandbox.publish(root,"cleanup.json",{"owner":owner,"pid":process.pid,"returncode":process.returncode,
                    "reaped":True,"terminated":terminated,"at":time.time()})
            stdout.flush();os.fsync(stdout.fileno());stderr.flush();os.fsync(stderr.fileno())
    if process.returncode!=0 or time.time()>=deadline or time.monotonic()>=monotonic_deadline:raise ValueError("bounded DNS preparation failed")
    raw=(root/"stdout.raw").read_bytes()
    if len(raw)>65536:raise ValueError("DNS response exceeded bound")
    value=fleet_json.loads(raw)
    if value.get("owner")!=owner or not isinstance(value.get("addresses"),list):raise ValueError("foreign DNS observation")
    choices=[]
    for family,address in value["addresses"]:
        ip=ipaddress.ip_address(address)
        expected=socket.AF_INET if ip.version==4 else socket.AF_INET6
        if type(family) is not int or family!=expected or not ip.is_global:raise ValueError("DNS returned unsupported/non-public address")
        choices.append((family,str(ip)))
    if not choices:raise ValueError("DNS returned no numeric address")
    family,address=sorted(set(choices))[0]  # one selected address, no fallback
    policy={"version":VERSION,"endpoint":ENDPOINT,"method":"POST","hostname":HOST,"port":443,
        "family":family,"address":address,"resolved_at":started,"expires_at":started+300,
        "owner":owner,"resolution_sha256":sandbox.digest(raw),"tls":"CERT_REQUIRED+check_hostname"}
    sandbox.publish(root,"policy.json",validate(policy))
    return policy


def validate(policy,*,now=None):
    p=copy.deepcopy(policy)
    if not isinstance(p,dict) or set(p)!={"version","endpoint","method","hostname","port","family","address","resolved_at","expires_at","owner","resolution_sha256","tls"}:raise ValueError("invalid HTTPS policy fields")
    if (p["version"]!=VERSION or p["endpoint"]!=ENDPOINT or p["method"]!="POST" or p["hostname"]!=HOST
            or type(p["port"]) is not int or p["port"]!=443 or p["tls"]!="CERT_REQUIRED+check_hostname"):
        raise ValueError("HTTPS policy escaped fixed provider endpoint")
    ip=ipaddress.ip_address(p["address"])
    if type(p["family"]) is not int or p["family"]!=(socket.AF_INET if ip.version==4 else socket.AF_INET6) or not ip.is_global or str(ip)!=p["address"]:raise ValueError("HTTPS needs exact global numeric address/family")
    if str(uuid.UUID(p["owner"]))!=p["owner"] or not isinstance(p["resolution_sha256"],str) or len(p["resolution_sha256"])!=64 or any(c not in "0123456789abcdef" for c in p["resolution_sha256"]):raise ValueError("HTTPS lacks resolution provenance")
    instant=time.time() if now is None else now
    if (type(p["resolved_at"]) not in (int,float) or type(p["expires_at"]) not in (int,float)
            or not 0<p["resolved_at"]<=instant<p["expires_at"]<=p["resolved_at"]+300):raise ValueError("expired/invalid DNS observation")
    return p


class NumericHTTPS(http.client.HTTPSConnection):
    """One socket; no getaddrinfo, address iteration, proxy or reconnect.

    CONTROL supplies its original deadline/revocation predicate. Use exchange()
    for the bounded operation, including response-body ownership transfer.
    Raw stdlib request/response methods alone do not enforce that total bound.
    This class does not grant request authority.
    """
    def __init__(self,policy,*,deadline_at,stopped):
        self.policy=validate(policy);self.deadline_at=deadline_at;self.stopped=stopped;self.connect_started=False;self.http_started=False
        self.active_socket=None
        self.transport_interrupted=threading.Event()
        self.policy_sha256=sandbox.digest(fleet_json.canonical_bytes(self.policy))
        if type(deadline_at) not in (int,float) or not time.time()<deadline_at<float("inf"):raise ValueError("invalid original HTTPS deadline")
        self.monotonic_deadline=time.monotonic()+min(deadline_at,self.policy["expires_at"])-time.time()
        context=ssl.create_default_context()
        if context.verify_mode!=ssl.CERT_REQUIRED or context.check_hostname is not True:raise ValueError("TLS identity verification unavailable")
        super().__init__(HOST,port=443,timeout=min(30,deadline_at-time.time()),context=context)
        self.auto_open=0

    def boundary(self):
        if self.transport_interrupted.is_set():raise TimeoutError("transport guardian interrupted execution")
        if sandbox.digest(fleet_json.canonical_bytes(self.policy))!=self.policy_sha256:raise ValueError("prepared HTTPS policy changed")
        validate(self.policy)
        remaining=min(self.deadline_at-time.time(),self.monotonic_deadline-time.monotonic())
        if self.stopped() or remaining<=0:raise TimeoutError("original transport authority expired/revoked")
        if self.sock is not None:self.sock.settimeout(min(30,remaining))

    def connect(self):
        if self.connect_started:raise ValueError("second connection attempt forbidden")
        self.connect_started=True
        self.boundary()
        if self._tunnel_host or self.sock is not None:raise ValueError("proxy/reconnection forbidden")
        try:
            self.sock=socket.socket(self.policy["family"],socket.SOCK_STREAM)
            self.active_socket=self.sock
            self.boundary()
            address=(self.policy["address"],443) if self.policy["family"]==socket.AF_INET else (self.policy["address"],443,0,0)
            self.sock.connect(address)
            self.boundary()
            self.sock=self._context.wrap_socket(self.sock,server_hostname=HOST,do_handshake_on_connect=False)
            self.active_socket=self.sock
            self.boundary()
            self.sock.do_handshake()
            self.boundary()
        except BaseException:
            self.close();raise

    def request(self,method,url,body=None,headers=None,*,encode_chunked=False):
        self.boundary()
        if (method!="POST" or url!="/chat/completions" or type(body) is not bytes or len(body)>1024*1024
                or encode_chunked or self.http_started or self.sock is None):raise ValueError("HTTP escaped single fixed request")
        headers={} if headers is None else dict(headers)
        if any(key.lower() not in {"content-type","authorization"} for key in headers):raise ValueError("unsupported HTTP header authority")
        self.http_started=True
        return super().request(method,url,body,headers,encode_chunked=False)

    def abort(self):
        # HTTPResponse can retain a makefile after HTTPConnection sets sock=None.
        # Keep the exact underlying socket across that transfer for shutdown.
        if self.active_socket is not None:
            try:self.active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        self.close()

    def exchange(self,body,headers=None):
        finished=threading.Event();interrupted=self.transport_interrupted;guard_errors=[]
        def guard():
            try:
                while not finished.wait(.01):
                    if self.stopped() or time.time()>=min(self.deadline_at,self.policy["expires_at"]) or time.monotonic()>=self.monotonic_deadline:
                        interrupted.set();self.abort();return
            except BaseException as exc:
                guard_errors.append(exc);interrupted.set();self.abort()
        watcher=threading.Thread(target=guard,daemon=True);watcher.start()
        response=None;data=b""
        try:
            self.connect();self.request("POST","/chat/completions",body,headers)
            response=self.getresponse()
            while True:
                self.boundary()
                if interrupted.is_set():raise TimeoutError("transport interrupted")
                block=response.read1(min(65536,1024*1024+1-len(data)))
                data+=block
                self.boundary()
                if interrupted.is_set():raise TimeoutError("transport interrupted")
                if len(data)>1024*1024:raise ValueError("HTTP response exceeded bound")
                if not block:
                    if response.length not in (None,0):raise http.client.IncompleteRead(data,response.length)
                    break
            return {"http_status":response.status,"body":data}
        finally:
            finished.set();self.abort();watcher.join()
            if response is not None:response.close()
            if guard_errors:raise RuntimeError("transport guardian failed") from guard_errors[0]
