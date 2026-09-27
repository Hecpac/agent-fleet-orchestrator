"""Pure replay of CONTROL resource originals; never imports worker code."""
import base64
import fleet_harness_sandbox as sandbox
import fleet_json


def verify_abandoned(originals, *, prefix="resource/"):
    """Prove quiescence by phase, without granting any authority to old output."""
    def read(name):return fleet_json.loads(originals[prefix+name])
    record=read("resource.json")
    if prefix+"created.json" in originals:
        created=read("created.json")
        if created["resource_sha256"]!=sandbox.digest(fleet_json.canonical_bytes(record)):raise ValueError("foreign abandoned resource")
        record["cid"]=created["cid"]
        sandbox.Sandbox.validate_inspect(read("created-inspect.json"),record)
    elif record["cid"] is not None:raise ValueError("missing abandoned create evidence")
    clean=read("cleanup.json");observed=read("cleanup-observation.json")
    same=lambda a,b:fleet_json.canonical_bytes(a)==fleet_json.canonical_bytes(b)
    if (not same(clean["resource"],record) or not same(observed["resource"],record)
            or clean["inactive"] is not True or clean["resources_clean"] is not True
            or observed["remaining_name"].strip() or observed["remaining_resource_label"].strip()):
        raise ValueError("abandoned verifier not proven quiescent")
    if observed["before"] is not None:sandbox.Sandbox.validate_inspect(observed["before"],record)
    if prefix+"start-intent.json" in originals:
        if record["cid"] is None or not same(read("start-intent.json"),{"resource":record,"action":"start"}):
            raise ValueError("foreign abandoned start")
        attach=read("attach.json");streams=read("terminal-streams.json")
        if any(x["owner"]!=record["owner"] or x["resource_id"]!=record["resource_id"] for x in (attach,streams)):
            raise ValueError("abandoned attach identity missing")
        wires=[]
        for name in sorted(n for n in originals if n.startswith(prefix+"rpc/")):
            if name!=prefix+f"rpc/{len(wires)+1:04d}.json":raise ValueError("abandoned RPC gap")
            wires.append(fleet_json.loads(originals[name]))
        consumed=b"".join(base64.b64decode(w["response_b64"],validate=True) for w in wires)
        tail=base64.b64decode(streams["stdout_b64"],validate=True);stderr=base64.b64decode(streams["stderr_b64"],validate=True)
        if consumed+tail!=originals[prefix+"stdout.raw"] or stderr!=originals[prefix+"stderr.raw"]:
            raise ValueError("abandoned streams not retained")
        # A partial final line is retained as data, never as a PASS or result.
        if clean["extra_stdout"] is not bool(tail):raise ValueError("abandoned stream flags differ")
    elif any(n.startswith(prefix+kind) for n in originals for kind in ("attach.json","rpc/","rpc-intents/","stdout.raw","stderr.raw")):
        raise ValueError("unstarted verifier contains execution evidence")
    return record


def verify(originals, *, prefix="resource/"):
    def read(name): return fleet_json.loads(originals[prefix+name])
    record=read("resource.json");created=read("created.json")
    if created["resource_sha256"] != sandbox.digest(fleet_json.canonical_bytes(record)): raise ValueError("foreign created resource")
    record["cid"]=created["cid"]
    sandbox.Sandbox.validate_inspect(read("created-inspect.json"),record)
    if read("start-intent.json") != {"resource":record,"action":"start"}: raise ValueError("foreign resource start")
    attach=read("attach.json")
    if attach["owner"] != record["owner"] or attach["resource_id"] != record["resource_id"]: raise ValueError("foreign resource attach")
    cleanup=read("cleanup.json");observed=read("cleanup-observation.json");streams=read("terminal-streams.json")
    if (fleet_json.canonical_bytes(cleanup["resource"]) != fleet_json.canonical_bytes(record)
            or observed["resource"] != record or observed["remaining_name"].strip() or observed["remaining_resource_label"].strip()
            or cleanup["inactive"] is not True or cleanup["resources_clean"] is not True
            or cleanup["extra_stdout"] is not False or cleanup["bounded_output"] is not True
            or streams["owner"] != record["owner"] or streams["resource_id"] != record["resource_id"] or streams["bounded"] is not True):
        raise ValueError("resource quiescence originals differ")
    if observed["before"] is not None: sandbox.Sandbox.validate_inspect(observed["before"],record)
    wires=[]
    for name in sorted(n for n in originals if n.startswith(prefix+"rpc/")):
        if name != prefix+f"rpc/{len(wires)+1:04d}.json": raise ValueError("resource RPC gap")
        wire=fleet_json.loads(originals[name]); wires.append(wire)
    stdout=b"".join(base64.b64decode(w["response_b64"],validate=True) for w in wires)
    tail=base64.b64decode(streams["stdout_b64"],validate=True)
    stderr=base64.b64decode(streams["stderr_b64"],validate=True)
    if (tail or stdout+tail != originals[prefix+"stdout.raw"] or stderr != originals[prefix+"stderr.raw"]
            or len(stderr)>sandbox.MAX_OUTPUT): raise ValueError("resource streams differ from original bytes")
    return record,cleanup,wires
