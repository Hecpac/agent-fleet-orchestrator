"""Replay exact native Mini terminal plus CONTROL admission and effect records."""
import base64
import copy
from pathlib import Path

import fleet_harness_mini as mini
import fleet_herdr_work_packet as work
import fleet_json
import fleet_harness_resource_evidence as resource_evidence
import fleet_harness_executor as executor
import fleet_harness_budget as budget
import fleet_harness_read_scope as read_scope
import fleet_harness_acceptance as checker

VERSION = "owner-mini-delivery-v1"


def verify_continuity(raw, predecessors, *, limits, admission):
    """Invalid protocol attempts consume the same immutable reservation history."""
    current=fleet_json.loads(raw)["budget_originals"]
    _,rows=budget.verify_originals(current,limits)
    allowed={work.digest(admission)};old_calls=set()
    for previous,previous_admission in predecessors:
        previous_value=fleet_json.loads(previous)
        if work.digest(previous_value["admission"])!=work.digest(previous_admission):raise ValueError("foreign predecessor admission")
        earlier=previous_value["budget_originals"]
        _,earlier_rows=budget.verify_originals(earlier,limits)
        allowed.add(work.digest(previous_admission))
        old_calls.update(r["reservation"]["id"] for r in earlier_rows)
        if any(current.get(name)!=value for name,value in earlier.items()):
            raise ValueError("request budget dropped or changed an earlier attempt")
    for row in rows:
        record=row["reservation"];pin=record["admission"];logical=record["logical_id"]
        suffix=logical.removeprefix(pin+"/query/")
        if (pin not in allowed or not suffix.isascii() or not suffix.isdecimal() or not 1<=int(suffix)<=32
                or logical!=pin+"/query/"+str(int(suffix))
                or pin!=work.digest(admission) and record["id"] not in old_calls):
            raise ValueError("request lacks registered admission/query provenance")


def verify_terminal(raw, final, *, contract, admission):
    value = fleet_json.loads(raw)
    if (not isinstance(value, dict) or value.get("version") != VERSION
            or work.digest(value.get("admission")) != work.digest(admission)
            or work.digest(value.get("task")) != admission["prompt_sha256"]
            or value.get("error") is not None):
        raise ValueError("invalid or foreign Mini delivery")
    terminal = value["terminal"]
    task = fleet_json.canonical_bytes(value["task"]).decode()
    derived = mini.recover_mini(terminal["trajectory"], terminal["runner"], task=task,
        expected_template_sha256=mini.digest(mini.TEMPLATE.encode()))
    messages = terminal["trajectory"]["messages"]
    measured,requests=budget.verify_originals(value["budget_originals"],contract["request_budget"])
    if work.digest(measured)!=work.digest(value["budget"]): raise ValueError("budget summary differs from originals")
    current={r["reservation"]["logical_id"]:r for r in requests if r["reservation"]["admission"]==work.digest(admission)}
    index=0
    for offset,message in enumerate(messages):
        if message.get("role")!="assistant":continue
        index+=1; logical=work.digest(admission)+"/query/"+str(index)
        request=current.pop(logical,None)
        prefix=[{k:v for k,v in m.items() if k in {"role","content","tool_calls","tool_call_id"}} for m in messages[:offset]]
        if (request is None or request["http_status"]!=200
                or work.digest(request["response"])!=work.digest(message["extra"]["response"])
                or work.digest(prefix)!=work.digest(request["payload"]["messages"])):
            raise ValueError("native query differs from reserved provider exchange")
    if current or not index:raise ValueError("unaccounted provider request")
    if (messages[0]["content"] != mini.SYSTEM or messages[1]["content"] != task
            or derived != terminal["extraction"] or terminal["final"].encode() != final
            or derived["native_terminal_summary"].encode() != final): raise ValueError("Mini final extraction differs from originals")
    native = fleet_json.loads(base64.b64decode(value["native_terminal_b64"], validate=True))
    if native["error"] is not None or work.digest(native["value"]) != work.digest(terminal["trajectory"]):
        raise ValueError("terminal differs from original native frame")
    if not value["controls"]: raise ValueError("Mini CONTROL originals absent")
    for control in value["controls"]:
        originals={n:base64.b64decode(raw,validate=True) for n,raw in control["originals"].items()}
        record,clean,wires=resource_evidence.verify(originals)
        if (record["owner"] != admission["generation"] or control["cleanup"] != clean
                or record["writable"] or record["temporary"] or not record["dependencies"]
                or originals["task.json"] != fleet_json.canonical_bytes(value["task"])):
            raise ValueError("Mini CONTROL resource belongs to another task")
        runtime=fleet_json.loads(originals["runtime.json"])
        if (runtime["guest_sha256"] != mini.digest(mini.GUEST.read_bytes())
                or originals["bridge/"+mini.GUEST.name] != mini.GUEST.read_bytes()):
            raise ValueError("Mini guest differs from pinned execution")
        if record["argv"] != ["/usr/local/bin/python3","-I","-S","-B","/bridge/"+mini.GUEST.name]:
            raise ValueError("Mini execution did not use the pinned control guest")
        for index,wire in enumerate(wires,1):
            frame=fleet_json.loads(base64.b64decode(wire["response_b64"],validate=True))
            if work.digest(frame)!=work.digest(fleet_json.loads(originals[f"native/{index:04d}.json"])):
                raise ValueError("native frame differs from original CONTROL response")
        if not wires:raise ValueError("CONTROL resource supplied no native frames")
    if work.digest(fleet_json.loads(base64.b64decode(wires[-1]["response_b64"],validate=True))) != work.digest(native):
        raise ValueError("terminal is not the last original CONTROL response")
    calls = []
    for message in messages:
        if message.get("role") == "assistant":
            actions = mini.validate_batch(message["extra"]["response"], {a["tool_call_id"] for a in calls})
            calls.extend(actions)
    if [a["tool_call_id"] for a in calls] != [t["action"]["tool_call_id"] for t in value["tools"]]:
        raise ValueError("terminal has unfinished or foreign effects")
    previous_after = None
    first_files = last_files = None
    for action, tool in zip(calls, value["tools"]):
        if action != tool["action"] or tool["binding"] != {"admission_sha256":work.digest(admission), "tool_call_id":action["tool_call_id"]}:
            raise ValueError("tool was applied to another admission")
        output = tool["output"]
        originals={n:base64.b64decode(raw,validate=True) for n,raw in tool["originals"].items()}
        record,clean,wires=resource_evidence.verify(originals)
        intent=fleet_json.loads(originals["intent.json"])
        scope=contract["prepared"]["sources"]["scope"]
        manifest=contract["prepared"]["sources"].get("public_read")
        if manifest is not None:
            public_checks=fleet_json.canonical_bytes(checker.public_projection(fleet_json.loads(contract["prepared"]["sources"]["functional_tests"])))
            if originals.get("bridge/public-checks.json")!=public_checks:
                raise ValueError("executor public checks differ from the exact public contract projection")
            preparation=fleet_json.loads(originals["preparation.json"])
            bridge=executor.verify_preparation(preparation,intent,str(Path(record["candidate"]).parent))
            if record["guest"]!=str(Path(preparation["store"])/"bridge") or {n for n in originals if n.startswith("bridge/")}!=set(bridge):
                raise ValueError("executor bridge mount differs from its complete preparation")
            for name,entry in bridge.items():
                if mini.digest(originals[name])!=entry["sha256"] or len(originals[name])!=entry["bytes"]:
                    raise ValueError("executor bridge originals differ from preparation")
            publication=fleet_json.loads(originals["publication.json"])
            if (intent.get("version")!="owned-executor-v2" or intent.get("public_read")!=manifest
                    or set(publication)!={"before","after","projection_after"}
                    or publication["before"]!=intent["before"]
                    or set(publication["after"])!=set(scope["editable_paths"])):
                raise ValueError("executor lacks exact public-read and publication authority")
            before,after=intent["projection_before"],publication["projection_after"]
            if previous_after is not None and intent["before"]!=previous_after:
                raise ValueError("executor command chain changed editable bytes between tools")
            read_scope.verify_snapshot(before,manifest,scope,editable_hashes=intent["before"])
            read_scope.verify_snapshot(after,manifest,scope,editable_hashes={n:e["sha256"] for n,e in publication["after"].items()})
            before_files=read_scope.editable_files(before["inventory"],scope,mode=0o666)
            if first_files is None: first_files=before_files
            if last_files is not None and before_files!=last_files:
                raise ValueError("executor command chain changed editable sizes or hashes")
            last_files=read_scope.editable_files(after["inventory"],scope,mode=0o666)
            if (before["inventory"]["root"]!=record["candidate"] or after["inventory"]["root"]!=record["candidate"]
                    or before["inventory"]["identity"]!=after["inventory"]["identity"]):
                raise ValueError("executor did not mount the exact public projection")
            for name,entry in publication["after"].items():
                raw_file=base64.b64decode(entry["bytes_b64"],validate=True)
                if (set(entry)!={"sha256","bytes_b64"}
                        or mini.digest(raw_file)!=entry["sha256"] or after["inventory"]["entries"][name]["bytes"]!=len(raw_file)):
                    raise ValueError("executor publication bytes differ from projection")
            previous_after={n:e["sha256"] for n,e in publication["after"].items()}
        if (record["writable"]!=scope["editable_paths"] or record["temporary"]!=scope["temporary_directories"]
                or record["dependencies"] is not None or type(record["processes"]) is not int or record["processes"]!=32
                or record["argv"]!=["/usr/local/bin/python3","-I","-S","-B","/bridge/"+executor.GUEST.name]
                or originals["bridge/"+executor.GUEST.name]!=executor.GUEST.read_bytes()
                or intent["guest_sha256"]!=mini.digest(executor.GUEST.read_bytes())):
            raise ValueError("executor permissions/runtime differ from admitted scope")
        result=fleet_json.loads(originals["result.json"])
        raw_response=fleet_json.loads(originals["response.json"])
        request=fleet_json.loads(base64.b64decode(wires[0]["request_b64"],validate=True)) if len(wires)==1 else {}
        rpc_intent=fleet_json.loads(originals["resource/rpc-intents/0001.json"])
        if (set(request)!={"id","command","seconds"} or request["id"]!=action["tool_call_id"]
                or request["command"]!=action["command"] or type(request["seconds"]) not in (int,float)
                or not 0<request["seconds"]<=30 or wires[0]["request_b64"]!=rpc_intent["request_b64"]):
            raise ValueError("tool wire request differs from admitted action")
        if (work.digest(intent["binding"]) != work.digest(tool["binding"]) or intent["action"] != action or intent["owner"] != record["owner"]
                or intent["candidate"] != contract["prepared"]["execution_envelope"]["candidate_repo"]
                or work.digest(intent["scope"]) != work.digest(contract["prepared"]["sources"]["scope"]) or work.digest(intent["deadline_at"]) != work.digest(contract["deadline_at"])
                or work.digest(clean) != work.digest(tool["cleanup"]) or work.digest(output) != work.digest({k:result[k] for k in ("tool_call_id","output","returncode")})
                or len(wires)!=1 or work.digest(raw_response) != work.digest(fleet_json.loads(base64.b64decode(wires[0]["response_b64"],validate=True)))
                or type(raw_response["value"]["returncode"]) is not int
                or work.digest(result) != work.digest({**raw_response["value"],"tool_call_id":action["tool_call_id"]})):
            raise ValueError("tool outcome differs from exact original execution")
        if (type(output["returncode"]) is not int or not isinstance(output["output"], str)
                or tool["cleanup"]["inactive"] is not True or tool["cleanup"]["resources_clean"] is not True):
            raise ValueError("tool outcome or cleanup incomplete")
        if action["command"] == mini.SENTINEL:
            if output["returncode"] != 0 or output["output"] != "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n":
                raise ValueError("native sentinel was not successfully executed")
        else:
            matching=[m for m in messages if m.get("role")=="tool" and m.get("tool_call_id")==action["tool_call_id"]]
            if (len(matching)!=1 or matching[0].get("extra",{}).get("raw_output") != output["output"]
                    or type(matching[0].get("extra",{}).get("returncode")) is not int
                    or matching[0]["extra"]["returncode"] != output["returncode"]):
                raise ValueError("native tool observation differs from executor")
    requested = copy.deepcopy(contract["runtime"])
    result={"version":VERSION, "requested":requested, "observed":{"cli":"mini-swe-agent", "cli_version":"2.4.6",
        "provider_model":"NOT_VERIFIED", "effort":"NOT_VERIFIED"},
        "identity_provenance":"CONTROL admission; native Mini originals; synthetic provider has no model identity",
        "transcript_sha256":mini.digest(raw), "usage":value["budget"], "authority":"none"}
    if "public_read" in contract["prepared"]["sources"]:
        if first_files is None or last_files is None:raise ValueError("delivery has no completed executor effects")
        result["effects"]={"before":first_files,"after":last_files}
    return result
