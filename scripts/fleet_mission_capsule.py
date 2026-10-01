"""Explicit capsule executor for the existing Mission driver, with no Herdr exec fallback.

CONTROL snapshots admitted inputs, consumes each run once, retains immutable
execution evidence, and promotes only the sole writer's validated outputs.
The capsule never sees the candidate checkout, ledger, credentials or CAS.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import stat
import subprocess
import uuid

import fleet_artifacts as artifacts
import fleet_codex_sandbox as capsule
import fleet_codex_responses as responses
import fleet_chatgpt_provider as subscription
import fleet_herdr_inference as inference
import fleet_herdr_permissions as permissions
import fleet_herdr_evidence as evidence
import fleet_herdr_binding as identity
import fleet_herdr_runtime
import fleet_herdr
import fleet_json
import fleet_mission as mission
import fleet_mission_state as state
import fleet_safe_paths as paths
import fleet_native_sandbox as sandbox

STAGES = {'research':'lead', 'plan':'lead', 'build':'worker', 'review':'reviewer',
          'verify':'verifier', 'synthesis':'lead'}
SCHEMA = 'fleet.mission.capsule.v2'
check = inference.check
sha = inference.sha


def manifest(image, home):
    images = capsule.runtime(Path(image))
    return {'schema': SCHEMA, 'cli_version': capsule.CODEX_VERSION, 'images': {k: {'path':v,'sha256':sha(Path(v).read_bytes())}
            for k,v in images.items()}, 'codex_home':str(Path(home).resolve(strict=True)),
            'provider':subscription.preflight(home)['provider']}


def validate_manifest(value, *, live=False):
    check(type(value) is dict and set(value)=={'schema','cli_version','images','codex_home','provider'}
          and value['schema']==SCHEMA and value['cli_version']==capsule.CODEX_VERSION, 'capsule manifest fields')
    check(type(value['images']) is dict and set(value['images'])=={'codex','codex-code-mode-host'}, 'capsule images')
    for item in value['images'].values():
        check(type(item) is dict and set(item)=={'path','sha256'} and Path(item['path']).is_absolute(), 'capsule image fields')
        state._require_sha(item['sha256'],'capsule image')
        if live: check(sha(Path(item['path']).read_bytes())==item['sha256'], 'capsule image changed')
    check(type(value['codex_home']) is str and Path(value['codex_home']).is_absolute(), 'capsule credential home')
    subscription.validate_descriptor(value['provider'])
    if live:
        installed=capsule.runtime(Path(value['images']['codex']['path']))
        check(installed=={k:v['path'] for k,v in value['images'].items()}, 'capsule runtime paths differ')
    return value


def candidate_files(repo):
    """Bounded regular tracked/untracked candidate bytes; Git metadata never enters."""
    names=subprocess.check_output(['git','-C',str(repo),'ls-files','--cached','--others','-z']).decode().split('\0')
    result={}
    with paths.RootedFS(repo) as fs:
        for name in sorted(set(names)-{''}):
            check('.git' not in Path(name).parts, 'candidate Git metadata')
            p=repo/name
            if not p.exists() and not p.is_symlink(): continue
            info=p.lstat()
            check(stat.S_ISREG(info.st_mode) and info.st_nlink==1, 'candidate requires regular unique files')
            result[name]=fs.read_regular(name, directory_modes=(None,)*(len(Path(name).parts)-1),
                file_mode=stat.S_IMODE(info.st_mode),max_bytes=sandbox.MAX_BYTES)
    sandbox._inputs(b'candidate',result,'build')
    return result


def pin_files(runs,mid,files):
    return {n:artifacts.put_bytes(runs,mid,b)['artifact_id'] for n,b in files.items()}


def read_json(runs,mid,name,optional=False):
    relative=Path('missions')/mid/name
    with paths.RootedFS(runs) as fs:
        method=fs.read_regular_optional if optional else fs.read_regular
        raw=method(relative,directory_modes=(0o700,)*(len(relative.parts)-1),file_mode=0o600,max_bytes=16*1024*1024)
    return None if raw is None else fleet_json.loads(raw)


def write_json(runs,mid,name,value,exclusive=False):
    relative=Path('missions')/mid/name
    with paths.RootedFS(runs) as fs:
        fs.atomic_write(relative,fleet_json.canonical_bytes(value)+b'\n',directory_modes=(0o700,)*(len(relative.parts)-1),
            file_mode=0o600,require_absent=exclusive)


def admission_for(current,run):
    values=[a for a in current['admissions'].values() if a['run_id']==run]
    check(len(values)==1,'capsule requires exact admission')
    return values[0]


def verify_launch(launch,current,read):
    check(type(launch) is dict and set(launch)=={'schema','attempt','run_id','stage','prompt_sha256',
        'candidate','inputs','manifest','compiled_digest','deadline_at','requested_policy'}, 'capsule launch fields')
    check(launch['schema']==SCHEMA,'capsule launch schema')
    validate_manifest(launch['manifest'])
    a=admission_for(current,launch['run_id']); stage=launch['stage']; role=launch['attempt']['role']
    check(stage in STAGES and role==STAGES[stage] and a['recipient_instance']==role
          and a['request_key']=='herdr:'+stage and a['writer'] is (stage=='build'), 'capsule role admission')
    check(launch['attempt']['mission_id']==current['mission_id']
          and launch['compiled_digest']==current['compiled_digest']
          and launch['deadline_at']==current['admission_policy']['deadline_at']
          and launch['prompt_sha256']==a['task_sha256'], 'capsule Mission binding')
    generation=str(uuid.uuid5(uuid.UUID(current['mission_id']), 'capsule:'+sha(fleet_json.canonical_bytes(launch['manifest']))))
    dispatch=current.get('herdr_control',{}).get('dispatches',{}).get(launch['run_id'])
    check(launch['attempt']=={'mission_id':current['mission_id'],'generation':generation,'role':role,'attempt_id':launch['run_id']}
          and dispatch is not None and dispatch['generation']==generation and dispatch['task_sha256']==launch['prompt_sha256'],
          'capsule generation or dispatch differs')
    prompt=read(launch['prompt_sha256']); check(sha(prompt)==launch['prompt_sha256'],'capsule prompt digest')
    task=fleet_json.loads(prompt)
    check(task['stage']==stage and task['run_id']==launch['run_id'] and task['instance_id']==role
          and task['mission_id']==current['mission_id'] and task['writer'] is (stage=='build')
          and task['candidate_repo']==launch['candidate']['realpath'], 'capsule task binding')
    check(launch['requested_policy']=={'model':permissions.MODELS[role],'effort':'high'}, 'capsule requested model')
    files={n:read(pin) for n,pin in launch['inputs'].items()}
    check(all(sha(files[n])==pin for n,pin in launch['inputs'].items()),'capsule input digest')
    sandbox._inputs(prompt,files,stage)
    return launch


class Publisher:
    def __init__(self,runs,mid,launch_pin):
        self.runs,self.mid=Path(runs),mid; self.launch_pin=launch_pin
        self.launch=fleet_json.loads(artifacts.get_bytes(runs,mid,launch_pin))
        self.attempt=self.launch['attempt']; self.tx=None
        self.capsule={'frozen_policy_sha256':launch_pin,'requested_policy':self.launch['requested_policy']}
        self.mission_capsule=True

    def active(self,current,*,defer_revoked=False):
        verify_launch(self.launch,current,lambda p:artifacts.get_bytes(self.runs,self.mid,p))
        options=read_json(self.runs,self.mid,'runtime-options.json')
        creation=read_json(self.runs,self.mid,'creation-request.json')
        check(creation['runtime_options']==options and options.get('herdr_capsule_manifest')==self.launch['manifest'],
              'capsule launch differs from creation')
        check(identity.path_identity(self.launch['candidate']['realpath'],directory=True)==self.launch['candidate'],
              'capsule candidate identity changed')
        backend=read_json(self.runs,self.mid,'herdr-backend.json')
        check(backend.get('executor')==SCHEMA and backend['generation']==self.attempt['generation'], 'capsule generation changed')
        dispatch=current.get('herdr_control',{}).get('dispatches',{}).get(self.launch['run_id'])
        check(dispatch is not None and dispatch['generation']==self.attempt['generation'], 'capsule dispatch binding')
        # admission checks are shared with the broker and include pause/deadline.
        policy={'attempt':self.attempt,'run_id':self.launch['run_id'],'deadline_at':self.launch['deadline_at'],'schema_version':3}
        instant=datetime.now(timezone.utc)
        if defer_revoked and (current['status']!='running'
                or current.get('herdr_control',{}).get('desired','running')!='running'
                or self.launch['run_id'] in current['cancelled_runs']
                or not admission_for(current,self.launch['run_id'])['active']
                or instant>=state.parse_timestamp(self.launch['deadline_at'],'capsule promotion deadline')):
            return False
        inference.admitted(current,policy,instant)
        return True

    @contextmanager
    def transaction(self):
        with state.MissionTransaction(self.runs,self.mid) as tx:
            self.tx=tx
            try:
                self.active(tx.current_state)
                yield
            finally: self.tx=None

    def run_link(self):
        return self.launch['run_id'],self.launch_pin


def verify_report(launch,report,read):
    check(report['schema']=='fleet.codex.capsule.v1' and report['attempt']==launch['attempt']
          and report['run_id']==launch['run_id'] and report['role']==launch['stage']
          and report['prompt_sha256']==launch['prompt_sha256'], 'capsule execution binding')
    check(report['runtime_images']=={k:v['sha256'] for k,v in launch['manifest']['images'].items()}, 'capsule executed images differ')
    check(report['cli_version']==launch['manifest']['cli_version'], 'capsule executed CLI version differs')
    check(report['execution_status']=='exited' and report['returncode']==0
          and report['quiescence_confirmed'] is True and report['cleanup_confirmed'] is True,
          'capsule requires completed quiescent execution')
    check(report['provider_execution']==subscription.execution({'provider':launch['manifest']['provider']}), 'capsule provider label differs')
    root=Path(report['capsule_root']); check(root.is_absolute(),'capsule root')
    expected=capsule.profile(root,launch['stage'],report['broker_port'])
    check(report['profile']==expected and report['profile_sha256']==sha(expected.encode())
          and report['inner_sandbox']=='danger-full-access', 'capsule external boundary differs')
    check(report['input_files']==launch['inputs'], 'capsule supplied bytes differ')
    if launch['stage']!='build': check(report['files']==launch['inputs'],'capsule reader changed files')
    sandbox._inputs(b'export',{n:read(pin) for n,pin in report['files'].items()},launch['stage'])
    check(all('.git' not in Path(n).parts for n in report['files']), 'capsule cannot export Git metadata')
    for field in ('files','transcripts'):
        for pin in report[field].values(): check(sha(read(pin))==pin,'capsule retained bytes differ')
    for field in ('stdout_sha256','stderr_sha256'): check(sha(read(report[field]))==report[field], 'capsule output digest')
    return root


def verify_result(result,*,read,role,cwd,prompt_sha256,expected_manifest,current=None):
    check(current is not None, 'capsule attestation requires the controller ledger')
    proof=result['evidence']; launch=fleet_json.loads(read(proof['capsule_launch_artifact_id']))
    report=fleet_json.loads(read(proof['capsule_report_artifact_id']))
    check(launch['manifest']==expected_manifest and launch['attempt']['role']==role
          and launch['candidate']['realpath']==cwd and launch['prompt_sha256']==prompt_sha256
          and launch['run_id']==result['run_id'] and launch['attempt']['mission_id']==result['mission_id'], 'capsule result differs from expected launch')
    launch_pin=proof['capsule_launch_artifact_id']
    check(sha(read(launch_pin))==launch_pin and sha(read(proof['capsule_report_artifact_id']))==proof['capsule_report_artifact_id'], 'capsule proof digest')
    policy=fleet_json.loads(read(report['broker_policy_id']))
    inference.validate_policy(policy)
    check(policy['schema_version']==3 and policy['launch_artifact_id']==launch_pin
          and policy['run_link_artifact_id']==launch_pin and policy['frozen_policy_sha256']==launch_pin
          and policy['attempt']==launch['attempt'] and policy['run_id']==launch['run_id']
          and policy['provider']==expected_manifest['provider'], 'capsule broker binding')
    if current is not None:
        verify_launch(launch,current,read)
        entry=current.get('inference_policies',{}).get(report['broker_policy_id'])
        check(entry is not None and entry['policy']==policy and entry['requests']
              and all(r['result'] and r['result']['status']=='completed' for r in entry['requests'].values()),
              'capsule requires completed ledger inference')
    root=verify_report(launch,report,read)
    check(proof['transcript_artifact_id'] in report['transcripts'].values(), 'capsule transcript not retained')
    raw=read(proof['transcript_artifact_id'])
    evidence.verify_transcript(raw,agent_session=proof['agent_session']['value'],model=permissions.MODELS[role],
        turn_id=result['turn_id'],prompt_sha256=prompt_sha256,final_bytes=read(result['artifact_id']),
        expected_provider='fleet-local')
    rows=fleet_json.load_jsonl(raw,require_nonempty=True)
    for row in rows:
        if row['type']=='turn_context':
            ctx=row['payload']
            check(ctx['cwd']==str(root/'work') and ctx['approval_policy']=='never'
                  and ctx['sandbox_policy']=={'type':'danger-full-access'}, 'capsule inner context differs')
        if row['type']=='session_meta':
            check(row['payload']['cli_version']==report['cli_version'], 'capsule transcript version differs')
    return {'status':'attested','policy_version':2,'scope':'external_seatbelt_capsule','role':role,
            'launch_artifact_id':proof['capsule_launch_artifact_id'],'report_artifact_id':proof['capsule_report_artifact_id']}


class CapsuleBackend:
    def __init__(self,runs_dir,mission_id,*,feature,target_repo,compiled,session,manifest):
        self.runs,self.mid,self.repo=Path(runs_dir),mission_id,Path(target_repo)
        self.compiled,self.session,self.manifest=compiled,session,validate_manifest(manifest)
        options=read_json(self.runs,self.mid,'runtime-options.json')
        creation=read_json(self.runs,self.mid,'creation-request.json')
        check(creation['runtime_options']==options and options.get('herdr_capsule_manifest')==manifest
              and options.get('herdr_session')==session, 'capsule executor differs from creation')
        current = mission.load_state(self.runs, self.mid)
        candidate = fleet_herdr_runtime.candidate_path(self.runs, self.mid, options, Path(current['target_repo']))
        check(self.repo == candidate and self.compiled['compiled_digest'] == current['compiled_digest'],
              'capsule candidate or compiled workflow differs from Mission')
        self.generation=str(uuid.uuid5(uuid.UUID(mission_id),'capsule:'+sha(fleet_json.canonical_bytes(manifest))))

    def state(self):
        return read_json(self.runs,self.mid,'herdr-backend.json')

    def boot(self):
        existing=read_json(self.runs,self.mid,'herdr-backend.json',True)
        expected={'schema_version':3,'executor':SCHEMA,'mission_id':self.mid,
            'compiled_digest':self.compiled['compiled_digest'],'generation':self.generation,
            'session':self.session,'workspace':{'closed':False}}
        if existing is None: write_json(self.runs,self.mid,'herdr-backend.json',expected,True)
        else: check(existing==expected,'capsule boot identity differs')
        return expected

    def _name(self,run,suffix):
        state.normalize_uuid(run,'capsule run')
        return 'capsule/'+run+'/'+suffix+'.json'

    def submit(self,run_id,prompt,*,instance_id):
        # No live provider call occurs until after the one-use intent is durable.
        validate_manifest(self.manifest,live=True)
        current=mission.load_state(self.runs,self.mid); a=admission_for(current,run_id)
        task=fleet_json.loads(prompt); stage=task['stage']
        check(STAGES.get(stage)==instance_id and a['phase']=='authorized','capsule role requires authorization')
        check(sha(prompt.encode())==a['task_sha256'],'capsule prompt changed')
        files=candidate_files(self.repo)
        launch={'schema':SCHEMA,'attempt':{'mission_id':self.mid,'generation':self.generation,
            'role':instance_id,'attempt_id':run_id},'run_id':run_id,'stage':stage,'prompt_sha256':a['task_sha256'],
            'candidate':identity.path_identity(str(self.repo),directory=True),'inputs':pin_files(self.runs,self.mid,files),
            'manifest':self.manifest,'compiled_digest':self.compiled['compiled_digest'],
            'deadline_at':current['admission_policy']['deadline_at'],
            'requested_policy':{'model':permissions.MODELS[instance_id],'effort':'high'}}
        verify_launch(launch,current,lambda pin:artifacts.get_bytes(self.runs,self.mid,pin))
        pin=artifacts.put_bytes(self.runs,self.mid,fleet_json.canonical_bytes(launch))['artifact_id']
        publisher=Publisher(self.runs,self.mid,pin)
        with publisher.transaction():
            write_json(self.runs,self.mid,self._name(run_id,'consumed'),{'launch_artifact_id':pin},True)
        provider=self.provider()
        try:
            pid=inference.freeze(publisher,profile=inference.RESPONSES_PROFILE,provider_binding=provider.binding,
                max_input_bytes=256*1024,max_output_tokens=32768,call_timeout_ms=20000)
            bridge=responses.Bridge(inference.Broker(publisher,pid,provider))
            execution=bridge.execute_confined(prompt.encode(),image=Path(self.manifest['images']['codex']['path']),
                files=files,parent=self.repo.parent,role=stage,timeout=60)
            for b in [execution.stdout,execution.stderr,*execution.files.values(),*execution.transcripts.values()]:
                artifacts.put_bytes(self.runs,self.mid,b)
            report_pin=artifacts.put_bytes(self.runs,self.mid,fleet_json.canonical_bytes(execution.report))['artifact_id']
            write_json(self.runs,self.mid,self._name(run_id,'execution'),{'launch_artifact_id':pin,'report_artifact_id':report_pin},True)
            verify_report(launch,execution.report,lambda p:artifacts.get_bytes(self.runs,self.mid,p))
            result=self.collect_execution(launch,pin,report_pin,execution)
            # Persist result before promotion; recovery uses these same bytes.
            write_json(self.runs,self.mid,self._name(run_id,'completed'),result,True)
            promoted=self.promote(launch,execution.report)
            return {'status':'settled' if promoted else 'indeterminate','run_id':run_id}
        except Exception:
            write_json(self.runs,self.mid,self._name(run_id,'failure'),{'status':'indeterminate',
                'diagnostic':getattr(provider,'last_diagnostic',None)})
            raise
        finally: provider.close()

    def provider(self):
        provider=subscription.ChatGPTProvider(self.manifest['codex_home'])
        check(provider.binding==self.manifest['provider'],'capsule provider changed')
        return provider

    def collect_execution(self,launch,pin,report_pin,execution):
        check(len(execution.transcripts)==1,'capsule requires one retained session')
        transcript=next(iter(execution.transcripts.values()))
        rows=fleet_json.load_jsonl(transcript,require_nonempty=True)
        metadata=[r['payload'] for r in rows if r['type']=='session_meta']
        finals=[r['payload'] for r in rows if r['type']=='event_msg' and r['payload'].get('type')=='task_complete']
        check(len(metadata)==len(finals)==1,'capsule final identity ambiguous')
        final=finals[0]['last_agent_message'].encode(); authored=fleet_json.loads(final)
        check(type(authored) is dict and set(authored)=={'schema_version','mission_id','run_id','instance_id',
            'status','summary','candidate_tree_sha','artifacts'}, 'capsule authored result fields')
        final_pin=artifacts.put_bytes(self.runs,self.mid,final)['artifact_id']
        result={**authored,'artifact_id':final_pin,'turn_id':finals[0]['turn_id'],
            'evidence':{'herdr_session':self.session,'prompt_sha256':launch['prompt_sha256'],
                'agent_session':{'kind':'id','value':metadata[0]['id'],'agent':'codex','source':'codex'},
                'transcript_sha256':sha(transcript),'transcript_artifact_id':sha(transcript),
                'capsule_launch_artifact_id':pin,'capsule_report_artifact_id':report_pin}}
        verify_result(result,read=lambda p:artifacts.get_bytes(self.runs,self.mid,p),role=launch['attempt']['role'],
            cwd=str(self.repo),prompt_sha256=launch['prompt_sha256'],expected_manifest=self.manifest,current=mission.load_state(self.runs,self.mid))
        result['result_artifact_id']=artifacts.put_bytes(self.runs,self.mid,fleet_json.canonical_bytes(result))['artifact_id']
        return result

    def promote(self,launch,report):
        """Return whether output is published; defer new writes after revocation."""
        if launch['stage']!='build': return True
        # Exact per-file journal: recovery accepts only the before/after bytes.
        # Unknown drift blocks promotion. The driver owns the candidate lock.
        if {n:sha(b) for n,b in candidate_files(self.repo).items()} == report['files']:
            return True  # Already promoted; read-only reconciliation needs no new authority.
        publisher=Publisher(self.runs,self.mid,sha(fleet_json.canonical_bytes(launch)))
        with state.MissionTransaction(self.runs,self.mid) as tx:
            if not publisher.active(tx.current_state,defer_revoked=True):
                # Retain execution evidence without presenting unpublished files
                # as a role result. Cancel can consume the quiescence receipt;
                # resume can promote the same output without another dispatch.
                return False
            observed={n:sha(b) for n,b in candidate_files(self.repo).items()}
            before,after=launch['inputs'],report['files']
            check(set(observed)<=set(before)|set(after),'candidate has unrelated additions')
            for n in set(before)|set(after):
                check(observed.get(n) in {before.get(n),after.get(n)},'candidate changed before promotion')
            with paths.RootedFS(self.repo) as fs:
                for name in sorted(set(before)|set(after)):
                    if observed.get(name)==after.get(name): continue
                    depth=len(Path(name).parts)-1
                    if name not in after:
                        fs.unlink_regular(name,directory_modes=(None,)*depth,file_mode=stat.S_IMODE((self.repo/name).lstat().st_mode))
                    else:
                        mode=stat.S_IMODE((self.repo/name).lstat().st_mode) if (self.repo/name).exists() else 0o644
                        directory_modes=[]
                        for index in range(1,depth+1):
                            parent=self.repo.joinpath(*Path(name).parts[:index])
                            try: directory_mode=stat.S_IMODE(parent.lstat().st_mode)
                            except FileNotFoundError: directory_mode=0o700
                            directory_modes.append(directory_mode)
                        fs.replace_regular(name,artifacts.get_bytes(self.runs,self.mid,after[name]),
                            directory_modes=directory_modes,file_mode=mode)
        return True

    def collect_result(self,run_id):
        result=read_json(self.runs,self.mid,self._name(run_id,'completed'),True)
        if result is None: return None
        proof=result['evidence']; read=lambda p:artifacts.get_bytes(self.runs,self.mid,p)
        launch=fleet_json.loads(read(proof['capsule_launch_artifact_id']))
        verify_result(result,read=read,role=launch['attempt']['role'],cwd=str(self.repo),
            prompt_sha256=launch['prompt_sha256'],expected_manifest=self.manifest,current=mission.load_state(self.runs,self.mid))
        if admission_for(mission.load_state(self.runs,self.mid),run_id)['active']:
            if not self.promote(launch,fleet_json.loads(read(proof['capsule_report_artifact_id']))):
                return None
        return result

    def recover(self,run_id):
        return {'status':'settled' if self.collect_result(run_id) else 'indeterminate','run_id':run_id}

    def wait(self,run_id,*,timeout_ms): return self.recover(run_id)

    def cancel(self,run_id):
        result=self.collect_result(run_id)
        if result is not None and fleet_herdr.load_result_rejection(self.runs,self.mid,run_id,result=result) is None:
            return {'status':'settled','run_id':run_id}
        saved=read_json(self.runs,self.mid,self._name(run_id,'execution'),True)
        if saved is None: return {'status':'indeterminate','run_id':run_id}
        read=lambda p:artifacts.get_bytes(self.runs,self.mid,p)
        launch=fleet_json.loads(read(saved['launch_artifact_id']))
        report=fleet_json.loads(read(saved['report_artifact_id']))
        current=mission.load_state(self.runs,self.mid)
        inference.require_settled(current)
        check(report['run_id']==run_id and report['attempt']==launch['attempt']
              and report['quiescence_confirmed'] is True and report['cleanup_confirmed'] is True,
              'capsule cancellation lacks quiescence')
        return {'status':'abandoned','run_id':run_id,'instance_id':launch['attempt']['role'],
            'prompt_sha256':launch['prompt_sha256'],'generation':launch['attempt']['generation'],
            'cancel_attempted':True}


    def teardown(self):
        current=mission.load_state(self.runs,self.mid)
        check(current['status'] in state.TERMINAL_STATUSES,'capsule teardown requires terminal Mission')
        inference.require_settled(current)
        value=self.state(); value['workspace']['closed']=True
        # Execution/consumption records stay immutable. Only this owned backend
        # closure marker is mutable after terminalization and settled egress.
        with paths.RootedFS(self.runs) as fs:
            fs.replace_regular(Path('missions')/self.mid/'herdr-backend.json',
                fleet_json.canonical_bytes(value)+b'\n', directory_modes=(0o700,0o700), file_mode=0o600)


def main():
    import argparse
    parser=argparse.ArgumentParser(description='Prepare a pinned Mission capsule manifest without generating text')
    parser.add_argument('--image',type=Path,default=None,
                        help='Codex image; defaults to the side-by-side install under FLEET_CODEX_ROOT')
    parser.add_argument('--codex-home',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    value=manifest(args.image or capsule.default_image(),args.codex_home)
    raw=fleet_json.canonical_bytes(value)
    with args.output.open('xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    print(json.dumps({'manifest':str(args.output.resolve()),'sha256':sha(raw),'network_requests':0}))


if __name__=='__main__': main()
