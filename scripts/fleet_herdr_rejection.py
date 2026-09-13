"""Immutable execution-evidence rejection, distinct from any accepted role result."""
from pathlib import Path

import fleet_artifacts
import fleet_json
import fleet_herdr_evidence
import fleet_herdr_profile
import fleet_safe_paths


def relative(mid, run):
    return Path('missions') / mid / f'herdr-evidence-rejection-{run}.json'


def retain(runs, mid, run, result, reason, rooted):
    observed = fleet_artifacts.put_bytes(runs, mid, fleet_json.canonical_bytes(result))['artifact_id']
    proof = {'schema_version': 1, 'kind': 'herdr_execution_evidence_rejection',
             'mission_id': mid, 'run_id': run, 'observed_result_artifact_id': observed,
             'reason': reason}
    pin = fleet_artifacts.put_bytes(runs, mid, fleet_json.canonical_bytes(proof))['artifact_id']
    rooted.atomic_write(relative(mid, run), fleet_json.canonical_bytes({'artifact_id': pin}) + b'\n',
                        directory_modes=(0o700, 0o700), file_mode=0o600, require_absent=True)
    return {**proof, 'artifact_id': pin}


def load(runs, mid, run, backend):
    with fleet_safe_paths.RootedFS(runs) as fs:
        raw = fs.read_regular_optional(relative(mid, run), directory_modes=(0o700, 0o700),
                                       file_mode=0o600, max_bytes=4096)
    if raw is None:
        return None
    pointer = fleet_json.loads(raw)
    if set(pointer) != {'artifact_id'} or raw != fleet_json.canonical_bytes(pointer) + b'\n':
        raise ValueError('invalid evidence rejection pointer')
    def read(pin):
        return fleet_artifacts.get_bytes(runs, mid, pin)
    proof_bytes = read(pointer['artifact_id'])
    proof = fleet_json.loads(proof_bytes)
    if (set(proof) != {'schema_version', 'kind', 'mission_id', 'run_id', 'observed_result_artifact_id', 'reason'}
            or proof_bytes != fleet_json.canonical_bytes(proof)
            or type(proof['schema_version']) is not int or proof['schema_version'] != 1
            or proof['kind'] != 'herdr_execution_evidence_rejection'
            or proof['mission_id'] != mid or proof['run_id'] != run
            or not isinstance(proof['reason'], str) or not proof['reason']):
        raise ValueError('invalid evidence rejection binding')
    result = fleet_json.loads(read(proof['observed_result_artifact_id']))
    submission = backend['submissions'][run]
    member = next(m for m in backend['members'] if m['instance_id'] == submission['instance_id'])
    evidence = result['evidence']
    if (backend['mission_id'] != mid or result['mission_id'] != mid or result['run_id'] != run
            or result['instance_id'] != member['instance_id']
            or result['candidate_tree_sha'] != submission.get('candidate_tree_sha')
            or evidence['prompt_sha256'] != submission['prompt_sha256']
            or evidence['generation'] != backend['generation']
            or evidence['herdr_session'] != backend['session']
            or evidence.get('runtime_contract') != backend.get('runtime_contract')
            or evidence.get('context_artifact_id') != backend.get('context_artifact_id')
            or any(evidence[k] != member[k] for k in ('workspace_id', 'tab_id', 'pane_id', 'terminal_id', 'agent_session'))):
        raise ValueError('evidence rejection differs from owned execution')
    final = fleet_json.loads(read(result['artifact_id']))
    if any(result.get(k) != v for k, v in final.items()):
        raise ValueError('rejected final differs from observed envelope')
    # Reproduce the rejection offline. Neither a model-authored status nor an
    # observer receipt may turn rejected context into an accepted result.
    try:
        fleet_herdr_evidence.verify_result(result, read_artifact=read,
            role=member['instance_id'], cwd=backend['target_repo'],
            prompt_sha256=submission['prompt_sha256'], agent_session=member['agent_session']['value'],
            permission_version=({fleet_herdr_profile.RESEARCH.profile_id: 3,
                                 fleet_herdr_profile.MINIMAL.profile_id: 4}.get(
                                     backend.get('herdr_profile'), 1)))
    except fleet_herdr_evidence.EvidenceError as exc:
        if str(exc) != proof['reason']:
            raise ValueError('evidence rejection reason no longer reproduces') from exc
    else:
        raise ValueError('evidence rejection unexpectedly passes validation')
    return {**proof, 'artifact_id': pointer['artifact_id']}
