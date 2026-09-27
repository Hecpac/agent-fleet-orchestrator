"""Confined, single-command execution; exact-file effects published by CONTROL.

The worker sees a disposable projection, never .git, CONTROL, the private oracle,
provider credentials or Docker sockets. Only existing declared file contents can
change. Rename/delete/create at those bind mountpoints is intentionally unavailable.
All descendants are removed before the final bytes can enter the candidate.
"""
from pathlib import Path
import base64
import copy
import time
import uuid
import os

import fleet_harness_sandbox as sand
import fleet_herdr_scope as scope
import fleet_json
import fleet_artifacts
import fleet_harness_read_scope as read_scope
from fleet_safe_paths import RootedFS

GUEST = Path(__file__).with_name("fleet_harness_executor_guest.py")


def execute(candidate, scope_contract, action, store, *, binding, deadline_at, public_checks=None, public_read=None, expected_before=None):
    candidate, store = Path(candidate).resolve(strict=True), Path(store).resolve()
    scope.validate(scope_contract)
    public_read = read_scope.validate(public_read if public_read is not None else {
        "version":read_scope.VERSION, "editable_paths":scope_contract["editable_paths"], "read_only":{}}, scope_contract)
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    _recover_intent(store)
    if (store / "intent.json").exists():
        with RootedFS(store) as fs:
            intent = fleet_json.loads(fs.read_regular("intent.json", directory_modes=(), max_bytes=1024*1024))
        if fleet_json.canonical_bytes([intent["action"], intent["scope"], intent["deadline_at"]]) != fleet_json.canonical_bytes([action, scope_contract, deadline_at]):
            raise ValueError("executor intent cannot be reused for different work or renewed limits")
        if fleet_json.canonical_bytes(intent["binding"])!=fleet_json.canonical_bytes(binding) or intent["candidate"]!=str(candidate):
            raise ValueError("foreign pre-dispatch executor intent")
        _validate_intent(intent, store, public_read,public_checks=public_checks)
        actual_before=read_scope.editable_files(intent["projection_before"]["inventory"],scope_contract,mode=0o666)
        if expected_before is not None:read_scope.require_files(actual_before,expected_before)
        if not (store/"resource/resource.json").exists():
            # Intent is durable and proves dispatch has not begun. Its complete
            # projection was staged before publication; reuse that exact state.
            with RootedFS(candidate) as fs:
                for name,sha in intent["before"].items():
                    modes=(None,)*(len(Path(name).parts)-1)
                    if sand.digest(fs.read_regular(name,directory_modes=modes,file_mode=0o644,max_bytes=1024*1024))!=sha:
                        raise read_scope.InputBindingError("pre-dispatch candidate changed")
            if read_scope.prepare(candidate,scope_contract,list(public_read["read_only"])) != public_read:
                raise read_scope.InputBindingError("pre-dispatch readonly candidate changed")
            return _run(candidate,store,intent,public_checks)
        return reconcile(candidate, store, binding=binding, public_read=public_read,expected_before=actual_before if expected_before is None else expected_before,public_checks=public_checks)
    seconds = min(30., deadline_at - time.time())
    if seconds <= 0: raise ValueError("shared deadline exhausted")
    if (set(action) != {"command", "tool_call_id"} or not isinstance(action["command"], str)
            or not action["command"].strip() or len(action["command"].encode()) > 32768):
        raise ValueError("invalid bounded executor action")
    inventory = scope.capture(candidate, scope_contract)
    if not inventory["complete"]: raise ValueError("incomplete candidate capture")
    actual_before=read_scope.editable_files(inventory,scope_contract)
    if expected_before is not None:read_scope.require_files(actual_before,expected_before)
    workspace, bridge = store / "workspace", store / "bridge"
    staged={"bridge/"+GUEST.name:(GUEST.read_bytes(),0o644)}
    if public_checks is not None:
        staged["bridge/public-checks.json"]=(fleet_json.canonical_bytes(public_checks),0o644)
    before = {}
    with RootedFS(candidate) as fs:
        for name in [*public_read["editable_paths"], *public_read["read_only"]]:
            item=inventory["entries"].get(name,{})
            if item.get("kind") != "file": raise ValueError("exact-file executor requires preexisting public files")
            editable=name in public_read["editable_paths"]
            if editable and item["mode"]!=0o644: raise ValueError("editable source mode differs from publication contract")
            raw = fs.read_regular(name, directory_modes=(None,) * (len(Path(name).parts)-1), file_mode=item["mode"], max_bytes=1024 * 1024)
            if not editable and sand.digest(raw)!=public_read["read_only"][name]: raise ValueError("public readonly source changed since creation")
            staged["workspace/"+name]=(raw,0o666 if editable else 0o644)
            if editable: before[name] = sand.digest(raw)
    copied={name:{"sha256":sand.digest(staged["workspace/"+name][0]),"bytes":len(staged["workspace/"+name][0])} for name in before}
    read_scope.require_files(copied,actual_before)
    preparation={"version":"executor-preparation-v1","store":str(store),"candidate":str(candidate),"binding":binding,"action":action,
        "scope":scope_contract,"public_read":public_read,"deadline_at":deadline_at,
        "files":{n:{"sha256":sand.digest(raw),"bytes":len(raw),"mode":mode} for n,(raw,mode) in staged.items()}}
    owner=str(uuid.uuid5(uuid.NAMESPACE_URL,"fleet-executor-preparation-v1:"+sand.digest(fleet_json.canonical_bytes(preparation))))
    owners={p.name for p in (store/"missions").iterdir()} if (store/"missions").exists() else set()
    if owners and owners!={owner}:raise read_scope.InputBindingError("preparation CAS belongs to different immutable work")
    if (store/"preparation.json").exists():
        with RootedFS(store) as fs:retained=fleet_json.loads(fs.read_regular("preparation.json",directory_modes=(),max_bytes=1024*1024))
        if fleet_json.canonical_bytes({k:v for k,v in retained.items() if k!="owner"})!=fleet_json.canonical_bytes(preparation):
            raise read_scope.InputBindingError("interrupted staging cannot acquire different work or bytes")
        if retained["owner"]!=owner:raise read_scope.InputBindingError("preparation owner changed")
    else:
        if os.path.lexists(workspace) or os.path.lexists(bridge):raise ValueError("unowned pre-intent staging; no automatic cleanup")
        _persist(store,owner,"preparation.json",{**preparation,"owner":owner})
    # The durable preparation authorizes only these deterministic publications.
    # Rooted atomic_write reconciles an interrupted fsync/rename, without
    # discarding unknown files or replacing conflicting contents.
    with RootedFS(store) as fs:
        for directory in ["workspace","bridge",*["workspace/"+n for n in read_scope.directories(public_read,scope_contract)]]:
            parts=Path(directory).parts
            fd=fs._open_directory_chain(parts,(0o755,)*len(parts),create=True);os.close(fd)
        for name,(raw,mode) in staged.items():
            fs.atomic_write(name,raw,directory_modes=(0o755,)*(len(Path(name).parts)-1),file_mode=mode)
    intent = {"version": "owned-executor-v2", "binding": copy.deepcopy(binding), "action": copy.deepcopy(action),
              "candidate": str(candidate), "before": before, "deadline_at": deadline_at, "owner": owner,
              "scope": copy.deepcopy(scope_contract), "guest_sha256": sand.digest(GUEST.read_bytes()),
              "public_read":public_read,"preparation_sha256":sand.digest(fleet_json.canonical_bytes({**preparation,"owner":owner})),
              "projection_before":read_scope.observe(workspace,public_read,scope_contract,editable_hashes=before)}
    if expected_before is not None:read_scope.require_files(read_scope.editable_files(intent["projection_before"]["inventory"],scope_contract,mode=0o666),expected_before)
    _persist(store,owner,"intent.json",intent)
    return _run(candidate,store,intent,public_checks)


def _recover_intent(store):
    if (store/"intent.json").exists():return
    pending=list(store.glob(".fleet-atomic-*.tmp"))
    if not pending:return
    owners=list((store/"missions").iterdir()) if (store/"missions").exists() else []
    if len(owners)!=1:raise ValueError("pending executor lacks unique owned CAS")
    owner=str(uuid.UUID(owners[0].name))
    from fleet_safe_paths import _atomic_pending_name
    for path in pending:
        raw=fleet_artifacts.get_bytes(store,owner,path.name[-68:-4])
        value=fleet_json.loads(raw)
        names=[name for name in ("preparation.json","intent.json") if path.name==_atomic_pending_name(name,raw)]
        if len(names)!=1 or value["owner"]!=owner:raise ValueError("unknown pre-dispatch publication")
        sand.publish(store,names[0],value)


def _run(candidate,store,intent,public_checks=None):
    owner=intent["owner"];action=intent["action"];scope_contract=intent["scope"];deadline_at=intent["deadline_at"]
    workspace,bridge=store/"workspace",store/"bridge"
    _validate_intent(intent,store,intent["public_read"],public_checks=public_checks)
    observed=read_scope.observe(workspace,intent["public_read"],scope_contract,editable_hashes=intent["before"])
    if observed!=intent["projection_before"]: raise ValueError("retained pre-dispatch projection changed")
    sandbox = sand.Sandbox(store / "resource", owner=owner)
    response, error = None, None
    try:
        sandbox.create(candidate=workspace, guest=bridge,
            argv=["/usr/local/bin/python3", "-I", "-S", "-B", "/bridge/" + GUEST.name],
            writable=scope_contract["editable_paths"], temporary=scope_contract["temporary_directories"], processes=32)
        if time.time() >= deadline_at: raise ValueError("deadline exhausted during executor setup")
        sandbox.start()
        rpc_seconds = min(32., deadline_at - time.time())
        if rpc_seconds <= 0: raise ValueError("deadline exhausted before command")
        # The command must finish before its containing RPC, including a
        # timeout report and reap. Both share the original task deadline.
        seconds = min(30., rpc_seconds - min(2.,rpc_seconds/2))
        response = sandbox.rpc({"id": action["tool_call_id"], "command": action["command"], "seconds": seconds}, timeout=rpc_seconds)
        _persist(store,owner,"response.json",response)
    except (ValueError, OSError, RuntimeError) as exc:
        error = type(exc).__name__ + ":" + str(exc)
        _persist(store,owner,"error.json",{"error":error})
    finally:
        if (store / "resource/resource.json").exists():
            sandbox.cleanup()  # Failure leaves the command indeterminate, never applied.
    if response is None or error: raise ValueError("executor outcome indeterminate:" + str(error))
    return reconcile(candidate, store, binding=intent["binding"], public_read=intent["public_read"],
        expected_before=read_scope.editable_files(intent["projection_before"]["inventory"],scope_contract,mode=0o666),public_checks=public_checks)


def _validate_intent(intent, store, public_read,*,public_checks=None):
    fields={"version","binding","action","candidate","before","deadline_at","owner","scope","guest_sha256","public_read","projection_before","preparation_sha256"}
    if set(intent)!=fields or intent["version"]!="owned-executor-v2": raise ValueError("legacy executor is read-only; explicit public-read authority required")
    if intent["public_read"]!=public_read: raise ValueError("executor public read authority changed")
    read_scope.verify_snapshot(intent["projection_before"],public_read,intent["scope"],editable_hashes=intent["before"])
    if intent["projection_before"]["inventory"]["root"]!=str((Path(store)/"workspace").resolve()):
        raise ValueError("executor projection belongs to another workspace")
    with RootedFS(store) as fs:preparation=fleet_json.loads(fs.read_regular("preparation.json",directory_modes=(),max_bytes=1024*1024))
    declared=verify_preparation(preparation,intent,str(Path(store).resolve()))
    raw_checks=None if public_checks is None else fleet_json.canonical_bytes(public_checks)
    expected_checks=None if raw_checks is None else {"sha256":sand.digest(raw_checks),"bytes":len(raw_checks),"mode":0o644}
    if fleet_json.canonical_bytes(declared.get("bridge/public-checks.json"))!=fleet_json.canonical_bytes(expected_checks):
        raise read_scope.InputBindingError("executor public checks changed from the admitted public projection")
    bridge=Path(store)/"bridge"
    if os.path.lexists(bridge/".git"):raise read_scope.InputBindingError("undeclared .git in executor bridge")
    observed=scope.capture(bridge,intent["scope"])
    if (observed["complete"] is not True or set(observed["entries"])!={n.removeprefix("bridge/") for n in declared}
            or any(fleet_json.canonical_bytes(observed["entries"][n.removeprefix("bridge/")])!=fleet_json.canonical_bytes({"kind":"file",**e}) for n,e in declared.items())
            or os.path.lexists(bridge/".git")):
        raise read_scope.InputBindingError("executor bridge contains changed or undeclared files")


def verify_preparation(preparation,intent,store):
    """Pure binding used by dispatch, recovery and archived delivery replay."""
    intent_fields={"version","binding","action","candidate","before","deadline_at","owner","scope","guest_sha256","public_read","projection_before","preparation_sha256"}
    if not isinstance(intent,dict) or set(intent)!=intent_fields or intent["version"]!="owned-executor-v2":
        raise ValueError("executor preparation requires the exact v2 intent schema")
    fields={"version","store","candidate","binding","action","scope","public_read","deadline_at","files","owner"}
    if (not isinstance(preparation,dict) or set(preparation)!=fields or preparation["version"]!="executor-preparation-v1" or preparation["store"]!=store
            or sand.digest(fleet_json.canonical_bytes(preparation))!=intent["preparation_sha256"]
            or any(fleet_json.canonical_bytes(preparation[k])!=fleet_json.canonical_bytes(intent[k]) for k in fields-{"version","store","files"})):
        raise ValueError("executor preparation differs from the admitted intent")
    expected_owner=str(uuid.uuid5(uuid.NAMESPACE_URL,"fleet-executor-preparation-v1:"+sand.digest(fleet_json.canonical_bytes({k:v for k,v in preparation.items() if k!="owner"}))))
    if preparation["owner"]!=expected_owner:raise ValueError("executor preparation owner is not stable")
    workspace={"workspace/"+n:{k:e[k] for k in ("sha256","bytes","mode")} for n,e in intent["projection_before"]["inventory"]["entries"].items() if e["kind"]=="file"}
    bridge={n:e for n,e in preparation["files"].items() if n.startswith("bridge/")}
    if ("bridge/"+GUEST.name not in bridge or set(bridge)-{"bridge/"+GUEST.name,"bridge/public-checks.json"}
            or fleet_json.canonical_bytes({**workspace,**bridge})!=fleet_json.canonical_bytes(preparation["files"])):
        raise ValueError("executor preparation includes undeclared files")
    for entry in bridge.values():
        if (set(entry)!={"sha256","bytes","mode"} or type(entry["mode"]) is not int or entry["mode"]!=0o644
                or type(entry["bytes"]) is not int or not 0<=entry["bytes"]<=1024*1024
                or not isinstance(entry["sha256"],str) or len(entry["sha256"])!=64 or any(c not in "0123456789abcdef" for c in entry["sha256"])):
            raise ValueError("invalid executor bridge file pin")
    return bridge


def reconcile(candidate, store, *, binding, expected_before, public_read=None,public_checks=None):
    """No command replay. Finish only a retained result with confirmed quiescence."""
    candidate, store = Path(candidate).resolve(strict=True), Path(store).resolve(strict=True)
    def read(name):
        with RootedFS(store) as fs:
            return fleet_json.loads(fs.read_regular(name, directory_modes=(0o700,) * (len(Path(name).parts)-1), max_bytes=4 * 1024 * 1024))
    intent = read("intent.json")
    if public_read is None:
        public_read={"version":read_scope.VERSION,"editable_paths":intent["scope"]["editable_paths"],"read_only":{}}
    _validate_intent(intent,store,public_read,public_checks=public_checks)
    read_scope.require_files(read_scope.editable_files(intent["projection_before"]["inventory"],intent["scope"],mode=0o666),expected_before)
    for pending in store.glob(".fleet-atomic-*.tmp"):
        raw=fleet_artifacts.get_bytes(store,intent["owner"],pending.name[-68:-4])
        from fleet_safe_paths import _atomic_pending_name
        names=[name for name in ("intent.json","response.json","error.json","publication.json","result.json") if pending.name==_atomic_pending_name(name,raw)]
        if len(names)!=1:raise ValueError("unknown executor pending publication")
        sand.publish(store,names[0],fleet_json.loads(raw))
    if fleet_json.canonical_bytes(intent["binding"]) != fleet_json.canonical_bytes(binding) or str(candidate.resolve()) != intent["candidate"]:
        raise ValueError("executor belongs to another admission/action")
    if not (store / "resource/cleanup.json").exists():
        record = read("resource/resource.json")
        sand.Sandbox(store / "resource", owner=record["owner"]).cleanup()
    cleanup = read("resource/cleanup.json")
    resource = read("resource/resource.json")
    created = read("resource/created.json")
    if sand.digest(fleet_json.canonical_bytes(resource)) != created["resource_sha256"]:
        raise ValueError("executor creation differs from intent")
    resource["cid"] = created["cid"]
    sand.Sandbox.validate_inspect(read("resource/created-inspect.json"), resource)
    streams = read("resource/terminal-streams.json")
    if (cleanup["resource"]["owner"] != intent["owner"] or cleanup["inactive"] is not True
            or fleet_json.canonical_bytes(cleanup["resource"]) != fleet_json.canonical_bytes(resource)
            or streams["owner"] != resource["owner"] or streams["resource_id"] != resource["resource_id"]
            or streams["bounded"] is not True or base64.b64decode(streams["stdout_b64"], validate=True)
            or cleanup["resources_clean"] is not True or cleanup["extra_stdout"] or not cleanup["bounded_output"]):
        raise ValueError("executor quiescence not established")
    wire = read("resource/rpc/0001.json")
    rpc_intent=read("resource/rpc-intents/0001.json")
    request=fleet_json.loads(base64.b64decode(wire["request_b64"],validate=True))
    if (wire["request_b64"] != rpc_intent["request_b64"] or set(request)!={"id","command","seconds"}
            or request["id"]!=intent["action"]["tool_call_id"] or request["command"]!=intent["action"]["command"]
            or type(request["seconds"]) not in (int,float) or not 0<request["seconds"]<=30):
        raise ValueError("executor wire request differs from admitted command")
    if not (store / "response.json").exists():
        _persist(store,intent["owner"],"response.json",fleet_json.loads(base64.b64decode(wire["response_b64"],validate=True)))
    response = read("response.json")
    if fleet_json.canonical_bytes(fleet_json.loads(base64.b64decode(wire["response_b64"], validate=True))) != fleet_json.canonical_bytes(response):
        raise ValueError("executor response differs from original wire bytes")
    if response["id"] != intent["action"]["tool_call_id"] or response["error"] is not None:
        raise ValueError("executor response is not bound")
    publication = store / "publication.json"
    # This check also runs when publication/result already exist. A recovered
    # output must not bypass confinement, immutable readonly pins or identity.
    projected=read_scope.observe(store/"workspace",public_read,intent["scope"])
    if projected["inventory"]["identity"]!=intent["projection_before"]["inventory"]["identity"]:
        raise ValueError("executor projection identity changed")
    if resource["candidate"]!=projected["inventory"]["root"]:
        raise ValueError("executor mounted another projection")
    if not publication.exists():
        after = {}
        with RootedFS(store / "workspace") as fs:
            for name in intent["before"]:
                raw = fs.read_regular(name, directory_modes=(0o755,) * (len(Path(name).parts)-1), file_mode=0o666, max_bytes=1024 * 1024)
                after[name] = {"sha256": sand.digest(raw), "bytes_b64": base64.b64encode(raw).decode()}
        read_scope.verify_snapshot(projected,public_read,intent["scope"],editable_hashes={n:e["sha256"] for n,e in after.items()})
        value = {"before": intent["before"], "after": after, "projection_after":projected}
        _persist(store,intent["owner"],"publication.json",value)
    retained = read("publication.json")
    if (set(retained)!={"before","after","projection_after"} or retained["before"]!=intent["before"]
            or retained["projection_after"]!=projected or set(retained["after"])!=set(intent["before"])):
        raise ValueError("retained publication differs from exact public projection")
    read_scope.verify_snapshot(projected,public_read,intent["scope"],editable_hashes={n:e["sha256"] for n,e in retained["after"].items()})
    # Idempotent completion after a crash between two file publications. The
    # exclusive owner driver lock prevents a new Worker while this is incomplete.
    with RootedFS(candidate) as fs:
        for name, entry in retained["after"].items():
            raw = base64.b64decode(entry["bytes_b64"], validate=True)
            if sand.digest(raw) != entry["sha256"]: raise ValueError("corrupt retained executor bytes")
            current = fs.read_regular(name, directory_modes=(None,) * (len(Path(name).parts)-1), file_mode=0o644, max_bytes=1024 * 1024)
            if sand.digest(current) not in {retained["before"][name], entry["sha256"]}: raise ValueError("candidate changed outside serialized executor")
            _replace_owned(fs, name, raw, token=sand.digest(fleet_json.canonical_bytes(intent)), already=sand.digest(current)==entry["sha256"])
    value = {**response["value"], "tool_call_id": intent["action"]["tool_call_id"]}
    _persist(store,intent["owner"],"result.json",value)
    return value


def _persist(store,owner,name,value):
    fleet_artifacts.put_bytes(store,owner,fleet_json.canonical_bytes(value))
    sand.publish(store,name,value)


def _replace_owned(fs, name, raw, *, token, already=False):
    """Recoverable exact temporary identity, including parent fsync after rename."""
    parent, parts = fs._parent_descriptor(name, directory_modes=(None,)*(len(Path(name).parts)-1), create=False)
    temporary = ".fleet-harness-" + token[:20] + "-" + sand.digest(name.encode())[:20] + ".tmp"
    try:
        if already:
            try:os.stat(temporary,dir_fd=parent,follow_symlinks=False)
            except FileNotFoundError:
                fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
                try:os.fsync(fd)
                finally:os.close(fd)
                os.fsync(parent)
                return
        try: fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o644, dir_fd=parent)
        except FileExistsError:
            relative = str(Path(name).parent / temporary)
            retained = fs.read_regular(relative, directory_modes=(None,)*(len(Path(name).parts)-1), file_mode=0o644, max_bytes=1024*1024)
            if not raw.startswith(retained): raise ValueError("owned replacement temporary differs")
            fd=os.open(temporary,os.O_WRONLY|os.O_NOFOLLOW,dir_fd=parent)
            try:
                # Exact deterministic name is committed in the publication;
                # only its verified retained prefix is eligible for completion.
                os.lseek(fd,len(retained),os.SEEK_SET)
                view=memoryview(raw)[len(retained):]
                while view:view=view[os.write(fd,view):]
                os.fchmod(fd,0o644);os.fsync(fd)
            finally:os.close(fd)
        else:
            try:
                os.fchmod(fd,0o644)
                view = memoryview(raw)
                while view: view = view[os.write(fd, view):]
                os.fsync(fd)
            finally: os.close(fd)
        os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally: os.close(parent)
