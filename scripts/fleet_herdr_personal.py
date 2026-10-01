"""Creation-bound personal official-CLI lane; not hostile-code containment.

Codex owns authentication and Responses transport. Fleet does not read tokens,
refresh sessions, proxy requests, or override the default model provider.
"""
from pathlib import Path
from datetime import datetime, timezone
import fleet_mission_state

import fleet_json
import fleet_mission
import fleet_safe_paths
import fleet_herdr_runtime

PROFILE = 'official-cli-personal-v1'


def require(backend, *, run_id=None, prompt_sha256=None, instance_id=None):
    relative=Path('missions')/backend.mission_id
    with fleet_safe_paths.RootedFS(backend.runs_dir) as fs:
        def read(name):
            return fleet_json.loads(fs.read_regular(relative/name,directory_modes=(0o700,0o700),
                file_mode=0o600,max_bytes=16*1024*1024))
        options=read('runtime-options.json'); creation=read('creation-request.json')
    if (options!=creation.get('runtime_options') or options.get('herdr_personal_cli')!=PROFILE
            or options.get('herdr_launch_manifest') is not None or options.get('herdr_capsule_manifest') is not None
            or options.get('herdr_session')!=backend.session or creation.get('mission_id')!=backend.mission_id):
        raise ValueError('personal CLI requires its exact creation-bound profile')
    current=fleet_mission.load_state(backend.runs_dir,backend.mission_id)
    if (current['status'] not in {'booting','running'}
            or current.get('herdr_control',{}).get('desired','running')!='running'
            or datetime.now(timezone.utc)>=fleet_mission_state.parse_timestamp(current['admission_policy']['deadline_at'],'personal CLI deadline')):
        raise ValueError('personal CLI execution no longer admitted')
    candidate=fleet_herdr_runtime.candidate_path(backend.runs_dir,backend.mission_id,options,Path(current['target_repo']))
    if candidate!=backend.target_repo or current['compiled_digest']!=backend.compiled['compiled_digest']:
        raise ValueError('personal CLI candidate or workflow differs')
    if run_id is not None:
        matches=[a for a in current['admissions'].values() if a['run_id']==run_id]
        if (len(matches)!=1 or matches[0]['phase'] not in {'authorized','started'} or not matches[0]['active']
                or matches[0]['result'] is not None or matches[0]['task_sha256']!=prompt_sha256
                or matches[0]['recipient_instance']!=instance_id
                or current.get('herdr_control',{}).get('desired','running')!='running'):
            raise ValueError('personal CLI prompt lacks an active exact admission')
    return current


def require_command(backend,command,executable):
    require(backend)
    # The task-inline startup preview is a fixed local CLI operation, not a
    # general Codex execution allowance. It precedes backend publication during
    # first boot and uses only the exact frozen selection flags thereafter.
    from fleet_herdr_versions import task_inline
    from fleet_herdr_skill_context import PROBE, flags
    if task_inline(backend.initial_runtime_contract):
        expected = ["codex", *(flags(backend.context) if backend.context is not None else []),
                    "debug", "prompt-input", PROBE]
        if command == expected:
            return
    if (not all(type(v) is str and '\0' not in v for v in command)
            or command[:3]!=[executable,'--session',backend.session]
            or tuple(command[3:5]) not in {('workspace','create'),('pane','split'),('agent','start'),('agent','prompt'),
                                           ('agent','send-keys')}):
        raise ValueError('unsupported personal CLI operation')

    with fleet_safe_paths.RootedFS(backend.runs_dir) as fs:
        saved=fleet_json.loads(fs.read_regular(Path('missions')/backend.mission_id/'herdr-backend.json',
            directory_modes=(0o700,0o700),file_mode=0o600,max_bytes=4*1024*1024))
    backend._validate_state(saved)
    operation=tuple(command[3:5])
    if operation==('agent','prompt'):
        matches=[s for s in saved['submissions'].values() if len(command)==7
                 and s['agent_name']==command[5] and s['phase']=='prepared'
                 and s['prompt_sha256']==fleet_mission_state.artifact_id(command[6])]
        if len(matches)!=1: raise ValueError('personal prompt differs from durable send intent')
        item=matches[0]
        require(backend,run_id=item['run_id'],prompt_sha256=item['prompt_sha256'],instance_id=item['instance_id'])
    elif operation==('agent','send-keys'):
        # Only the codex-0.159-v1 Folder access acknowledgment: one Enter to an
        # owned, started or starting agent that has not acknowledged before.
        # The backend decides from the exact surface; this bounds the effect.
        from fleet_herdr_versions import state_contract
        contract=state_contract(saved)
        members=[m for m in saved['members'] if len(command)==7 and m['agent_name']==command[5]
                 and m['start_phase'] in {'starting','started'} and not m.get('startup_acknowledgments')]
        if (len(members)!=1 or command[6]!='enter' or not contract
                or contract['startup_guard']!='codex-0.159-v1'):
            raise ValueError('personal key press differs from the startup acknowledgment')
    elif operation==('agent','start'):
        members=[m for m in saved['members'] if len(command)>5 and m['agent_name']==command[5]
                 and m['start_phase']=='starting']
        if len(members)!=1 or command != [executable,'--session',backend.session,*backend._start_arguments(members[0])[1:]]:
            raise ValueError('personal start differs from role arguments')
    elif operation==('pane','split'):
        if (len(command)<11 or command[5] not in {m['pane_id'] for m in saved['members']}
                or command[6]!='--direction' or command[7] not in {'right','down'}
                or command[8:]!=['--cwd',str(backend.target_repo),
                                 *backend._personal_environment_arguments(),'--no-focus']):
            raise ValueError('personal pane differs from owned layout')
    else:
        expected=[executable,'--session',backend.session,'workspace','create','--cwd',str(backend.target_repo),
            '--label',saved['workspace']['label'],'--env','FLEET_MISSION_ID='+backend.mission_id,
            '--env','FLEET_HERDR_GENERATION='+saved['generation'],'--env','FLEET_HERDR_SESSION='+backend.session,
            *backend._personal_environment_arguments()]
        expected+=['--no-focus']
        if command!=expected or saved['workspace']['create_phase']!='creating':
            raise ValueError('personal workspace differs from owned creation intent')
