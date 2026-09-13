"""Planning validation: no Git repositories, providers or scenario execution."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/fleet_sdd_contract.py'
SPEC = importlib.util.spec_from_file_location('sdd_contract', SCRIPT)
sdd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sdd)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads((ROOT / 'examples/sdd/deny-before-effect.json').read_text())

    def test_example_matrix_is_complete_but_not_verified(self):
        result = sdd.validate(self.plan)
        self.assertEqual(result['functional_status'], 'NOT_VERIFIED')
        self.assertEqual(len(result['matrix']), 2)
        self.assertEqual(result['matrix'][1]['checks'], ['CHK-002'])

    def test_broken_traceability_rejected(self):
        for kind in ('orphan', 'duplicate', 'unknown', 'no_task', 'no_check', 'design'):
            value = copy.deepcopy(self.plan)
            if kind == 'orphan': value['requirements'].append({'id':'REQ-002','behavior':'orphan'})
            if kind == 'duplicate': value['scenarios'].append(value['scenarios'][0])
            if kind == 'unknown': value['tasks'][0]['scenarios'] = ['SCN-999']
            if kind == 'no_task': value['tasks'][0]['scenarios'] = ['SCN-001']
            if kind == 'no_check': value['checks'].pop()
            if kind == 'design': value['design']['requirements'] = []
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                sdd.validate(value)

    def test_authority_and_unproven_success_rejected(self):
        for kind in ('owner', 'verified', 'extension', 'blank', 'schema'):
            value = copy.deepcopy(self.plan)
            if kind == 'owner': value['tasks'][0]['owner'] = 'lead'
            if kind == 'verified': value['checks'][0]['status'] = 'PASS'
            if kind == 'extension': value['permissions'] = ['network']
            if kind == 'blank': value['scenarios'][0]['then'] = ' '
            if kind == 'schema': value['schema'] = 'future'
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                sdd.validate(value)

    def test_duplicate_json_key_rejected(self):
        with self.assertRaises(ValueError):
            json.loads('{"schema":1,"schema":2}', object_pairs_hook=sdd._unique_object)

    def test_cli_outputs_matrix_without_running_procedures(self):
        run = subprocess.run([sys.executable, '-B', str(SCRIPT),
                              str(ROOT / 'examples/sdd/deny-before-effect.json')],
                             capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(run.stdout), sdd.validate(self.plan))


if __name__ == '__main__':
    unittest.main()
