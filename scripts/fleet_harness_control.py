"""Owned CONTROL lease. Herdr proves launch provenance, not atomic pane identity.

Only a live guard grants local effect authority. Serialized records are evidence,
never capabilities. Candidate resources cannot access this private host root.
Paid authority is deliberately a separate admission, not inferred here.
"""
import copy
import errno
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import tomllib
import uuid
import weakref

import fleet_control_runtime as runtime
import fleet_artifacts as artifacts
import fleet_harness_sandbox as sandbox
import fleet_json
import fleet_safe_paths as safe

VERSION = "harness-control-lease-v1"
_guards = weakref.WeakSet()


class AuthorityError(ValueError):
    pass


def boot_identity():
    if sys.platform == "darwin":
        value = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
            env={"PATH":"/usr/bin:/bin"}, capture_output=True, timeout=2, check=True).stdout.decode().strip()
    elif sys.platform.startswith("linux"):
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    else:
        raise AuthorityError("system boot identity unavailable")
    return str(uuid.UUID(value))


def read(root, name, optional=False):
    with safe.RootedFS(root, root_mode=0o700) as fs:
        fn = fs.read_regular_optional if optional else fs.read_regular
        try:raw = fn(name, directory_modes=(0o700,)*(len(Path(name).parts)-1), max_bytes=16*1024*1024)
        except safe.SafePathError as exc:
            cause=exc.__cause__ or exc.__context__
            if optional and isinstance(cause,OSError) and cause.errno==errno.ENOENT:
                fs.assert_root_binding();return None
            raise
        return None if raw is None else fleet_json.loads(raw)


def pin(value):
    return sandbox.digest(fleet_json.canonical_bytes(value))


def _namespace(plan):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,"fleet-control:"+pin(plan)))


def publish(root, plan, name, value):
    # CAS contains destination as well as bytes: even a crash before the first
    # pathname publication can be recovered without renewing clocks/UUIDs.
    artifacts.put_bytes(root,_namespace(plan),fleet_json.canonical_bytes(
        {"version":"control-publication-v1","plan_sha256":pin(plan),"name":name,"value":value}))
    sandbox.publish(root,name,value)


def recover(root, plan):
    namespace=_namespace(plan);path=artifacts.store_path(root,namespace)
    if not path.exists():return []
    partial=[]
    with safe.RootedFS(path,root_mode=0o700) as fs:
        names=fs.list_directory("",directory_modes=())
        for name in names:
            if name.startswith(".fleet-atomic-"):
                raw=fs.read_regular(name,directory_modes=(),max_bytes=16*1024*1024)
                if name!=safe._atomic_pending_name(sandbox.digest(raw),raw):
                    # Partial CAS has no committed authority to reconstruct.
                    # Preserve it; cleanup/cancel may continue, new effects may not.
                    partial.append(name)
                else:artifacts.put_bytes(root,namespace,raw)
    with safe.RootedFS(path,root_mode=0o700) as fs:names=fs.list_directory("",directory_modes=())
    for name in names:
        if name in partial:continue
        value=fleet_json.loads(artifacts.get_bytes(root,namespace,name))
        destination=value["name"]
        allowed=destination in {"control-clock.json","control-cancel.json","operator-approval.json","operator-approval-anchor.json",
            "control-completed.json","D1/control-creation.json","D2/control-creation.json","synthetic-tls-endpoint.json","signal-monitor-failure.json"} or any(
            destination.startswith(prefix+"/") and len(Path(destination).parts)==2
            and str(uuid.UUID(Path(destination).stem))+".json"==Path(destination).name
            for prefix in ("control-generations","control-releases","control-launches","control-launch-observations","control-ledgers","control-services","control-reconciliations"))
        if (set(value)!={"version","plan_sha256","name","value"} or value["version"]!="control-publication-v1"
                or value["plan_sha256"]!=pin(plan) or not allowed):raise AuthorityError("foreign CONTROL publication")
        sandbox.publish(root,destination,value["value"])
    return partial


def prepare(root, *, campaign_sha256, seconds, launch):
    root = Path(root).resolve(strict=True)
    identity = runtime.validate_directory_identity(runtime.directory_identity_from_stat(root.lstat()))
    if (type(seconds) is not int or not 1 <= seconds <= 3600 or not isinstance(campaign_sha256,str)
            or len(campaign_sha256)!=64 or any(c not in "0123456789abcdef" for c in campaign_sha256)):
        raise AuthorityError("invalid campaign/deadline")
    launch=copy.deepcopy(launch)
    if launch:launch["command_resolved"]=[str(Path(launch["command"][0]).resolve(strict=True)),*launch["command"][1:]]
    plan = {"version":VERSION, "root":str(root), "root_identity":identity,
        "campaign_sha256":campaign_sha256, "seconds":seconds, "launch":copy.deepcopy(launch)}
    sandbox.publish(root, "control-plan.json", plan)
    return plan


def verify_launch(bound,generation,plan):
    """Pure verification of retained CLI originals against the frozen launcher."""
    launch=plan["launch"]
    if (set(bound)!={"generation_sha256","pane_id","workspace_id","terminal_id","originals","launch"}
            or bound["generation_sha256"]!=pin(generation) or pin(bound["launch"])!=pin(launch)):
        raise AuthorityError("foreign launch generation")
    p=bound["originals"]["pane"]["result"]["pane"]
    info=bound["originals"]["process"]["result"]["process_info"]
    plugins=bound["originals"]["plugin"]["result"]["plugins"]
    process=next((v for v in info["foreground_processes"] if v["pid"]==generation["pid"]),None)
    if (len(plugins)!=1 or plugins[0]["plugin_id"]!=launch["plugin_id"] or plugins[0]["enabled"] is not True
            or plugins[0]["manifest_path"]!=launch["manifest"]
            or sum(e.get("id")==launch["entrypoint"] and e.get("command")==launch["command"] for e in plugins[0]["panes"])!=1
            or p["pane_id"]!=bound["pane_id"] or p["terminal_id"]!=bound["terminal_id"] or not p["terminal_id"]
            or p["workspace_id"]!=bound["workspace_id"] or p["cwd"]!=launch["cwd"]
            or info["pane_id"]!=p["pane_id"] or process is None
            or process["argv"]!=launch["command_resolved"] or process["cwd"]!=launch["cwd"]):
        raise AuthorityError("CONTROL launch originals changed")
    return bound


def _lock_identity(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600:
        raise AuthorityError("CONTROL lease owner/type/mode/link changed")
    return {"device":info.st_dev, "inode":info.st_ino, "uid":info.st_uid, "mode":stat.S_IMODE(info.st_mode)}


def _after_fork():
    for guard in list(_guards):
        # LOCK_UN would unlock the parent's shared open-file description.
        guard._active = False
        if guard._fd is not None:
            os.close(guard._fd); guard._fd = None
        if guard._fs is not None:guard._fs.close()


os.register_at_fork(after_in_child=_after_fork)


class OwnedLease:
    def __init__(self, root, plan_sha256):
        self.root = Path(root).resolve(strict=True)
        self.plan = read(self.root, "control-plan.json")
        if pin(self.plan)!=plan_sha256 or self.plan["version"]!=VERSION or self.plan["root"]!=str(self.root):
            raise AuthorityError("foreign CONTROL plan")
        self.plan_sha256 = plan_sha256
        self._fs = None; self._fd = None; self._active = False
        self._pending = set(); self._bound = None
        self._consumed = False
        self.pid = os.getpid(); self.birth, zombie = runtime.process_observation(self.pid)
        if self.birth is None or zombie:raise AuthorityError("own process identity unavailable")
        self.boot = boot_identity()
        _guards.add(self)

    def __reduce__(self):
        raise TypeError("CONTROL authority cannot be serialized or copied")

    def __enter__(self):
        if self._consumed or self._active or self._fd is not None or os.getpid()!=self.pid:
            raise AuthorityError("CONTROL guard cannot be reused/inherited")
        self._consumed=True
        self._fs = safe.RootedFS(self.root,root_mode=0o700)
        try:
            self._root_identity()
            flags = os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC
            self._fd = os.open(".control-lease.lock", flags, 0o600, dir_fd=self._fs._root_fd)
            self.lock_identity = _lock_identity(os.fstat(self._fd))
            fcntl.flock(self._fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            self._active = True
            self.assert_owned()
            with self._fs.exclusive_lock(".control-send.lock",directory_modes=()):
                self._partial=recover(self.root,self.plan)
            prior = sorted((self.root/"control-generations").glob("*.json"))
            for path in prior:
                record = read(self.root,str(path.relative_to(self.root)))
                if read(self.root,"control-releases/"+record["id"]+".json",optional=True) is not None:continue
                observed,zombie = runtime.process_observation(record["pid"])
                if observed==record["birth"] and not zombie:
                    raise AuthorityError("previous CONTROL process remains alive without release")
            anchor = read(self.root,"control-clock.json",optional=True)
            if anchor is None and self._partial:
                raise AuthorityError("original CONTROL clock is incomplete; only explicit cancellation is available")
            if anchor is None:
                now = time.time(); monotonic = time.monotonic()
                anchor = {"plan_sha256":self.plan_sha256,"boot":self.boot,"started_at":now,
                    "deadline_at":now+self.plan["seconds"],"monotonic_started":monotonic,
                    "monotonic_deadline":monotonic+self.plan["seconds"]}
                publish(self.root,self.plan,"control-clock.json",anchor)
            if (anchor["plan_sha256"]!=self.plan_sha256 or anchor["boot"]!=self.boot
                    or anchor["deadline_at"]!=anchor["started_at"]+self.plan["seconds"]
                    or anchor["monotonic_deadline"]!=anchor["monotonic_started"]+self.plan["seconds"]):
                raise AuthorityError("original clock authority unavailable after restart")
            self.clock = anchor
            self.generation = {"id":str(uuid.uuid4()),"plan_sha256":self.plan_sha256,"pid":self.pid,
                "birth":self.birth,"boot":self.boot,"root_identity":self.plan["root_identity"],
                "lock_identity":self.lock_identity,"clock_sha256":pin(anchor),
                "previous":[pin(read(self.root,str(p.relative_to(self.root)))) for p in prior]}
            publish(self.root,self.plan,"control-generations/"+self.generation["id"]+".json",self.generation)
            return self
        except BaseException:
            self._dispose();raise

    def _root_identity(self):
        self._fs.assert_root_binding()
        if runtime.directory_identity_from_stat(os.fstat(self._fs._root_fd))!=self.plan["root_identity"]:
            raise AuthorityError("copied/replaced campaign root cannot create authority")

    def assert_owned(self):
        if not self._active or self._fd is None or os.getpid()!=self.pid:
            raise AuthorityError("CONTROL lease is not held by this process")
        self._root_identity()
        if (_lock_identity(os.fstat(self._fd))!=self.lock_identity or
                _lock_identity(os.stat(".control-lease.lock",dir_fd=self._fs._root_fd,follow_symlinks=False))!=self.lock_identity):
            raise AuthorityError("CONTROL lease descriptor/path changed")
        current,zombie = runtime.process_observation(self.pid)
        if current!=self.birth or zombie:raise AuthorityError("CONTROL process birth changed")
        if pin(read(self.root,"control-plan.json"))!=self.plan_sha256:
            raise AuthorityError("CONTROL authority plan changed")

    def assert_effect(self):
        self.assert_owned()
        if self._partial:raise AuthorityError("incomplete CONTROL publication requires reconciliation; effects denied")
        if self._bound is None or self._bound.get("generation_sha256")!=pin(self.generation):
            raise AuthorityError("CONTROL has no verified Herdr launch for this generation")
        if read(self.root,"control-cancel.json",optional=True) is not None:
            raise AuthorityError("CONTROL cancellation requested")
        # Cancellation commits to CAS before its pathname. Read that barrier
        # without acquiring the send flock: callers may already hold it, and
        # guardians must not wait behind the operation they are interrupting.
        path=artifacts.store_path(self.root,_namespace(self.plan))
        with safe.RootedFS(path,root_mode=0o700) as fs:
            for name in fs.list_directory("",directory_modes=()):
                raw=fs.read_regular(name,directory_modes=(),max_bytes=16*1024*1024)
                if name.startswith(".fleet-atomic-"):
                    if name!=safe._atomic_pending_name(sandbox.digest(raw),raw):
                        raise AuthorityError("incomplete CONTROL intent; effects denied")
                elif sandbox.digest(raw)!=name:raise AuthorityError("CONTROL CAS bytes changed")
                value=fleet_json.loads(raw)
                if value["name"]=="control-cancel.json":raise AuthorityError("CONTROL cancellation committed to CAS")
        if time.time()>=self.clock["deadline_at"] or time.monotonic()>=self.clock["monotonic_deadline"]:
            raise AuthorityError("original CONTROL deadline exhausted")

    def bind_herdr(self):
        self.assert_owned()
        if self._bound is not None:raise AuthorityError("launch already bound")
        with self._fs.exclusive_lock(".control-send.lock",directory_modes=()):
            self._partial=recover(self.root,self.plan)
            prior=read(self.root,"control-launches/"+self.generation["id"]+".json",optional=True)
            if prior is not None:
                verify_launch(prior,self.generation,self.plan)
                self._bound=prior
                return copy.deepcopy(prior)
        launch = self.plan["launch"]
        if os.environ.get("HERDR_ENV")!="1":raise AuthorityError("CONTROL was not launched inside Herdr")
        pane = os.environ.get("HERDR_PANE_ID");workspace=os.environ.get("HERDR_WORKSPACE_ID")
        if not pane or not workspace:raise AuthorityError("missing exact Herdr surface")
        env = {k:v for k,v in os.environ.items() if k.startswith("HERDR_") or k in {"HOME","PATH","TMPDIR"}}
        originals = {}
        for name,args in (("pane",["pane","get",pane]),
                ("process",["pane","process-info","--pane",pane]),
                ("plugin",["plugin","list","--plugin",launch["plugin_id"],"--json"])):
            result = subprocess.run([launch["herdr"],*args],env=env,capture_output=True,timeout=5)
            if result.returncode:
                publish(self.root,self.plan,"control-launch-observations/"+str(uuid.uuid4())+".json",
                    {"generation_sha256":pin(self.generation),"originals":originals,"failed_command":args,
                     "returncode":result.returncode,"stdout":result.stdout.decode(errors="replace"),"stderr":result.stderr.decode(errors="replace")})
                raise AuthorityError("Herdr launch observation failed: "+name)
            originals[name] = fleet_json.loads(result.stdout)
        publish(self.root,self.plan,"control-launch-observations/"+str(uuid.uuid4())+".json",
            {"generation_sha256":pin(self.generation),"originals":originals,"failed_command":None})
        manifest_path=Path(launch["manifest"])
        manifest_raw=manifest_path.read_bytes()
        if sandbox.digest(manifest_raw)!=launch["manifest_sha256"]:raise AuthorityError("registered manifest changed")
        manifest=tomllib.loads(manifest_raw.decode())
        entry=next((p for p in manifest["panes"] if p["id"]==launch["entrypoint"]),None)
        p=originals["pane"]["result"]["pane"];info=originals["process"]["result"]["process_info"]
        plugins=originals["plugin"]["result"]["plugins"]
        process=next((v for v in info["foreground_processes"] if v["pid"]==self.pid),None)
        def command(argv):return [str(Path(argv[0]).resolve(strict=True)),*argv[1:]]
        if (len(plugins)!=1 or plugins[0]["plugin_id"]!=launch["plugin_id"] or plugins[0]["enabled"] is not True
                or plugins[0]["manifest_path"]!=str(manifest_path) or manifest["id"]!=launch["plugin_id"]
                or entry is None or entry not in plugins[0]["panes"] or entry["command"]!=launch["command"]
                or process is None or command(process["argv"])!=command(launch["command"])
                or process["cwd"]!=launch["cwd"] or p["cwd"]!=launch["cwd"]
                or p["pane_id"]!=pane or info["pane_id"]!=pane or p["workspace_id"]!=workspace
                or not p.get("terminal_id")):
            raise AuthorityError("Herdr launch does not bind this CONTROL process")
        self.assert_owned()
        bound={"generation_sha256":pin(self.generation),"pane_id":pane,"workspace_id":workspace,
            "terminal_id":p["terminal_id"],"originals":originals,"launch":copy.deepcopy(launch)}
        verify_launch(bound,self.generation,self.plan)
        publish(self.root,self.plan,"control-launches/"+self.generation["id"]+".json",bound)
        self._bound=bound
        return copy.deepcopy(self._bound)

    def begin_transport(self, identity):
        self.assert_effect()
        if identity in self._pending:raise AuthorityError("duplicate in-flight transport")
        self._pending.add(identity)

    def end_transport(self, identity, *, closed):
        self.assert_owned()
        if closed is not True:raise AuthorityError("transport quiescence not established")
        self._pending.remove(identity)

    def _dispose(self):
        # Closing is sufficient; never LOCK_UN an inherited open description.
        self._active=False
        if self._fd is not None:os.close(self._fd);self._fd=None
        if self._fs is not None:self._fs.close()

    def __exit__(self,*args):
        self.assert_owned()
        if self._pending:raise AuthorityError("cannot release CONTROL with undrained transports")
        with self._fs.exclusive_lock(".control-send.lock",directory_modes=()):
            recover(self.root,self.plan)
            name="control-releases/"+self.generation["id"]+".json"
            existing=read(self.root,name,optional=True)
            if existing is None:
                publish(self.root,self.plan,name,{"generation_sha256":pin(self.generation),"transports_drained":True,"at":time.time()})
        self._dispose()


def cancel(root, plan_sha256, reason):
    """Short independent barrier; send admission must use this same barrier."""
    plan=read(root,"control-plan.json")
    if pin(plan)!=plan_sha256:raise AuthorityError("cancel belongs to another campaign")
    with safe.RootedFS(root,root_mode=0o700) as fs:
        if runtime.directory_identity_from_stat(os.fstat(fs._root_fd))!=plan["root_identity"]:
            raise AuthorityError("cancel root changed")
        with fs.exclusive_lock(".control-send.lock",directory_modes=()):
            return cancel_under_barrier(fs,plan_sha256,reason)


def cancel_under_barrier(fs,plan_sha256,reason):
    """Internal helper for owner cancel using the same S -> journal lock order."""
    fs.assert_root_binding();root=fs.root;plan=read(root,"control-plan.json")
    if pin(plan)!=plan_sha256 or runtime.directory_identity_from_stat(os.fstat(fs._root_fd))!=plan["root_identity"]:
        raise AuthorityError("cancel belongs to another CONTROL root")
    recover(root,plan)
    existing=read(root,"control-cancel.json",optional=True)
    if existing is not None:return existing
    record={"plan_sha256":plan_sha256,"id":str(uuid.uuid4()),"at":time.time(),"reason":str(reason)[:500]}
    publish(root,plan,"control-cancel.json",record)
    return record
