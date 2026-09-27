"""Derive D1/D2 results from immutable historical candidates; never rerun a provider."""
import argparse
import os
from pathlib import Path
import stat
import time

import fleet_harness_acceptance as checker
import fleet_harness_sandbox as sandbox
import fleet_json


def inventory(root):
    entries={}
    for path in sorted(Path(root).rglob("*")):
        info=path.lstat();name=str(path.relative_to(root))
        if stat.S_ISLNK(info.st_mode):value={"kind":"symlink","target":os.readlink(path)}
        elif stat.S_ISREG(info.st_mode):value={"kind":"file","sha256":sandbox.digest(path.read_bytes()),"bytes":info.st_size}
        elif stat.S_ISDIR(info.st_mode):value={"kind":"directory"}
        else:raise ValueError("unsupported historical node: "+str(path))
        entries[name]={**value,"mode":stat.S_IMODE(info.st_mode),"mtime_ns":info.st_mtime_ns}
    return entries


def run(history,output):
    history,output=Path(history).resolve(),Path(output).resolve()
    if output==history or history in output.parents:raise ValueError("derived evidence must be outside history")
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    before=inventory(history);sandbox.publish(output,"historical-inputs.json",before)
    summaries=[]
    for entry in sorted((history/"evaluation").glob("deepseek-*/freeze.json")):
        freeze=fleet_json.loads(entry.read_bytes());task=freeze["trial"]["task"];identifier=freeze["trial"]["trial_id"]
        original=Path(freeze["candidate"])
        physical=inventory(original);files={n:v["sha256"] for n,v in physical.items() if v["kind"]=="file"}
        if files!=freeze["inventory"]["files"]:raise ValueError("historical candidate drift: "+identifier)
        store=output/identifier;staged=store/"sources";staged.mkdir(mode=0o755,parents=True)
        names=("ledger.py","paths.py") if task=="D1" else ("report.py",)
        for name in names:
            target=staged/name;target.write_bytes((original/name).read_bytes());target.chmod(0o644)
        binding={"derived_version":"historical-reevaluation-v1","trial_id":identifier,
            "freeze_sha256":sandbox.digest(entry.read_bytes()),"original_sources":{name:files[name] for name in names},
            "original_oracle_preserved":True,"new_provider_requests":0}
        result=checker.run(staged,checker.suite(task),store/"check",binding=binding,deadline_at=time.time()+90)
        checker.verify(store/"check",expected_binding=binding)
        baseline=freeze["original_evidence"]["dispatch.json"]["baseline"]
        changed=sorted(n for n in set(files)|set(baseline) if files.get(n)!=baseline.get(n))
        scope=all(n in freeze["allowed_files"] for n in changed)
        summary={"trial_id":identifier,"binding":binding,"checker_version":checker.VERSION,"status":result["status"],
            "failed_families":sorted({r["family"] for r in result["results"] if not r["passed"]}),
            "failed_cases":[r["id"] for r in result["results"] if not r["passed"]],"pending":result["pending"],
            "scope_final":scope,"physical_changed_paths":changed,"transient_effects":"NOT_VERIFIED by final inventory",
            "original_report_scope":freeze["scope_pass"],"source_admission":"not retroactively created",
            "receipt_immutability":"new explicit policy; never sole historical rejection basis"}
        sandbox.publish(store,"derived.json",summary);summaries.append(summary)
    if len(summaries)!=8:raise ValueError("expected the eight completed historical attempts")
    after=inventory(history)
    if before!=after:raise ValueError("historical evidence changed during derived evaluation")
    sandbox.publish(output,"conservation.json",{"historical_root":str(history),"before_sha256":checker.pin(before),
        "after_sha256":checker.pin(after),"entries":len(before),"exclusions":[],"unchanged":True,
        "capture_limit":"sequential physical scan, not an atomic filesystem snapshot"})
    sandbox.publish(output,"results.json",summaries)
    return {"evaluated":len(summaries),"rejected":sum(r["status"]=="failed" for r in summaries),"new_provider_requests":0}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("history",type=Path);parser.add_argument("output",type=Path)
    args=parser.parse_args();print(fleet_json.canonical_bytes(run(args.history,args.output)).decode())
