"""K4: a controller error while the Mission is still live has its own exit code.

Exit 1 keeps meaning a terminal failure (or an error before any Mission exists);
exit 4 means the controller failed while the durable Mission is non-terminal, so
automation must not read it as a final verdict.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fleet_herdr
import fleet_mission
import fleet_mission_state as state
from tests import test_fleet_herdr_mission as missions
from tests.test_mission_run import mission_run

CONTRACT = {"schema_version": 1, "requirements": [{"id": "answer", "description": "answer exists",
    "checks": [{"kind": "text_contains", "path": "answer.txt", "expected": "implemented"}]}]}


class ControllerErrorExitCodeTests(unittest.TestCase):
    setUp = missions.HerdrProtocolRejectionTests.setUp

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mission_run.main(['--runs-dir', str(self.runs), *argv])
        return code, out.getvalue(), err.getvalue()

    def failing_drive(self):
        return mock.patch.object(mission_run.fleet_herdr_mission, 'drive',
                                 side_effect=fleet_herdr.HerdrBackendError('fixture controller failure'))

    def test_resume_error_with_live_mission_exits_4_with_durable_status(self):
        with self.failing_drive():
            code, out, err = self.main('resume', '--mission-id', self.mid, '--json')
        self.assertEqual(code, 4)
        self.assertEqual(mission_run.CONTROLLER_ERROR_EXIT, 4)
        envelope = json.loads(out)
        current = fleet_mission.load_state(self.runs, self.mid)
        self.assertEqual(envelope['mission_id'], self.mid)
        self.assertEqual(envelope['status'], current['status'])
        self.assertNotIn(envelope['status'], state.TERMINAL_STATUSES)
        self.assertEqual(envelope['controller_error'], 'fixture controller failure')
        self.assertIn('resume', envelope['next_action'])
        self.assertIn('fixture controller failure', err)

    def test_error_after_run_created_the_mission_reports_that_mission(self):
        contract = self.helper.tmp / 'acceptance.json'
        contract.write_text(json.dumps(CONTRACT))
        with mock.patch.object(mission_run, 'drive_mission',
                               side_effect=fleet_herdr.HerdrBackendError('fixture failure after creation')):
            code, out, _ = self.main('run', 'exit-code-run', 'Implement a local answer artifact',
                '--workflow', 'herdr-implementation', '--target-repo', str(self.helper.target),
                '--herdr-session', 'fixture', '--acceptance-contract', str(contract), '--json')
        self.assertEqual(code, 4)
        envelope = json.loads(out)
        self.assertNotEqual(envelope['mission_id'], self.mid)
        self.assertEqual(fleet_mission.load_state(self.runs, envelope['mission_id'])['feature'], 'exit-code-run')
        self.assertEqual(envelope['controller_error'], 'fixture failure after creation')

    def test_error_before_any_mission_exists_keeps_exit_1(self):
        code, out, err = self.main('run', 'no-contract', 'Implement a local answer artifact',
            '--workflow', 'herdr-implementation', '--target-repo', str(self.helper.target),
            '--herdr-session', 'fixture', '--json')
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertIn('acceptance-contract', err)

    def test_error_with_terminal_mission_keeps_exit_1(self):
        state.append_terminal(self.runs, self.mid, status='failed', reason='fixture terminal',
                              idempotency_key='fixture-terminal')
        with self.failing_drive():
            code, out, _ = self.main('resume', '--mission-id', self.mid, '--json')
        self.assertEqual(code, 1)
        self.assertEqual(out, '')


if __name__ == '__main__':
    unittest.main()
