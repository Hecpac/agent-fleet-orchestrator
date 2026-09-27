"""Validate SDD planning traceability only; never execute or accept a Mission."""
import argparse
import json
import re

try:  # imported as ``scripts.fleet_sdd_contract``
    from . import fleet_json
except ImportError:  # executed directly or imported from scripts/
    import fleet_json


def _object(value, fields, where):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"{where}: expected fields {fields}")


def _text(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: nonempty text required")


def _rows(value, prefix, fields):
    if not isinstance(value, list) or not value:
        raise ValueError(f"{prefix}: nonempty list required")
    result = {}
    for row in value:
        _object(row, fields, prefix)
        ident = row['id']
        if not isinstance(ident, str) or not re.fullmatch(prefix + r'-[0-9]{3,}', ident):
            raise ValueError(f"{prefix}: invalid id")
        if ident in result:
            raise ValueError(f"{prefix}: duplicate id {ident}")
        result[ident] = row
    return result


def _refs(value, allowed, where):
    if (not isinstance(value, list) or not value
            or any(not isinstance(v, str) for v in value)
            or len(set(value)) != len(value) or not set(value) <= set(allowed)):
        raise ValueError(f"{where}: missing, duplicate or unknown references")
    return set(value)


def validate(document):
    """Return a planning matrix. Evidence is deliberately not admitted by v1."""
    _object(document, ('schema', 'objective', 'requirements', 'scenarios', 'design',
                       'tasks', 'checks'), 'document')
    if document['schema'] != 'fleet.sdd.plan.v1':
        raise ValueError('unsupported schema')
    _text(document['objective'], 'objective')
    requirements = _rows(document['requirements'], 'REQ', ('id', 'behavior'))
    scenarios = _rows(document['scenarios'], 'SCN', ('id', 'requirement', 'given', 'when', 'then'))
    tasks = _rows(document['tasks'], 'TASK', ('id', 'owner', 'scenarios', 'change'))
    checks = _rows(document['checks'], 'CHK', ('id', 'scenarios', 'procedure', 'status'))
    for row in requirements.values():
        _text(row['behavior'], 'behavior')
    covered = set()
    for row in scenarios.values():
        if not isinstance(row['requirement'], str) or row['requirement'] not in requirements:
            raise ValueError('unknown requirement')
        covered.add(row['requirement'])
        for field in ('given', 'when', 'then'):
            _text(row[field], field)
    if covered != set(requirements):
        raise ValueError('requirement without scenario')
    _object(document['design'], ('approach', 'requirements'), 'design')
    _text(document['design']['approach'], 'approach')
    if _refs(document['design']['requirements'], requirements, 'design') != set(requirements):
        raise ValueError('design does not cover every requirement')
    for row in tasks.values():
        if row['owner'] != 'worker':
            raise ValueError('only worker owns implementation tasks')
        _refs(row['scenarios'], scenarios, 'task')
        _text(row['change'], 'change')
    for row in checks.values():
        _refs(row['scenarios'], scenarios, 'check')
        _text(row['procedure'], 'procedure')
        if row['status'] != 'NOT_VERIFIED':
            raise ValueError('planning contract cannot assert verification')
    matrix = []
    for ident, scenario in scenarios.items():
        task_ids = sorted(k for k, v in tasks.items() if ident in v['scenarios'])
        check_ids = sorted(k for k, v in checks.items() if ident in v['scenarios'])
        if not task_ids or not check_ids:
            raise ValueError(f'{ident}: missing task or check')
        matrix.append(dict(requirement=scenario['requirement'], scenario=ident,
                           tasks=task_ids, checks=check_ids, status='NOT_VERIFIED'))
    return {'contract': 'VALID', 'functional_status': 'NOT_VERIFIED', 'matrix': matrix}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan')
    args = parser.parse_args()
    try:
        with open(args.plan, 'rb') as source:
            result = validate(fleet_json.loads(source.read()))
    except (OSError, ValueError) as error:
        parser.exit(2, f'INVALID: {error}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
