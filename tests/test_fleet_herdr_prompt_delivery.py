"""K6: a prompt that never reached Herdr is `not_sent`, never an ambiguous send.

Real backend, driver, ledger and CAS; only the Herdr transport is synthetic.
A size limit is enforced before any admission, so no durable dispatch intent can
exist for a prompt that cannot be delivered.
"""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fleet_herdr
import fleet_herdr_mission as driver
from tests import test_fleet_herdr as transport
from tests import test_fleet_herdr_mission as missions


def failing_prompt(fake, failure):
    def runner(command, **kwargs):
        if command[3:5] == ['agent', 'prompt']:
            raise failure
        return fake(command, **kwargs)
    return runner


class NotSentBackendTests(unittest.TestCase):
    def setUp(self):
        self.f = transport.HerdrBackendTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.run = str(uuid.uuid4())

    def submit_with(self, failure):
        backend = self.f.backend(runner=failing_prompt(self.f.fake, failure))
        backend.boot()
        return backend, backend.submit(self.run, self.f.prompt(self.run), instance_id='lead')

    def test_prompt_command_that_cannot_start_is_not_sent(self):
        backend, submission = self.submit_with(OSError(7, 'Argument list too long'))
        self.assertEqual((submission['phase'], submission['status']), ('terminal', 'not_sent'))
        self.assertIn('did not start', submission['reason'])
        self.assertEqual(backend.state()['submissions'][self.run]['status'], 'not_sent')
        self.assertEqual(backend.recover(self.run)['status'], 'not_sent')
        self.assertFalse(self.f.fake.prompted_agents)

    def test_observation_budget_does_not_interrupt_a_started_send(self):
        # An expired supervisor budget must not strand a durable dispatch intent
        # without a submission; the send is bounded by its own command timeouts.
        backend = self.f.backend(); backend.boot()
        expired = time.monotonic() - 1
        backend.observation_deadline = expired
        submission = backend.submit(self.run, self.f.prompt(self.run), instance_id='lead')
        self.assertEqual((submission['phase'], submission['status']), ('submitted', 'working'))
        self.assertTrue(self.f.fake.prompted_agents)
        self.assertEqual(backend.observation_deadline, expired)
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, 'observation budget exhausted'):
            backend.wait(self.run, timeout_ms=1000)

    def test_timeout_after_prompt_command_started_stays_ambiguous(self):
        backend = self.f.backend(runner=failing_prompt(self.f.fake, subprocess.TimeoutExpired(['herdr'], 30)))
        backend.boot()
        with self.assertRaisesRegex(fleet_herdr.HerdrBackendError, 'failed to run'):
            backend.submit(self.run, self.f.prompt(self.run), instance_id='lead')
        self.assertEqual(backend.recover(self.run)['status'], 'indeterminate')

    def test_not_sent_run_cancels_exactly_without_runtime_signal(self):
        backend, _ = self.submit_with(OSError(2, 'No such file or directory'))
        calls = len(self.f.fake.calls)
        cancelled = backend.cancel(self.run)
        self.assertEqual((cancelled['phase'], cancelled['status']), ('terminal', 'abandoned'))
        self.assertIs(cancelled['cancel_attempted'], True)
        self.assertEqual(len(self.f.fake.calls), calls)
        self.assertEqual(backend.cancel(self.run)['status'], 'abandoned')


class NotSentMissionTests(unittest.TestCase):
    setUp = missions.HerdrProtocolRejectionTests.setUp
    current = missions.HerdrProtocolRejectionTests.current
    admission = missions.HerdrProtocolRejectionTests.admission
    drive = missions.HerdrProtocolRejectionTests.drive
    operations = missions.HerdrProtocolRejectionTests.operations

    def build_admission(self):
        return next(a for a in self.current()['admissions'].values() if a['request_key'] == 'herdr:build')

    def test_not_sent_build_blocks_supervision_and_exact_cancel_closes(self):
        self.stage = 'review'  # no simulated answer is edited for Build
        real_call = transport.FakeHerdr.__call__
        real_write = transport.HerdrBackendTests.write_transcript
        def runner(fake, command, **kwargs):
            if command[3:5] == ['agent', 'prompt'] and json.loads(command[-1])['stage'] == 'build':
                raise OSError(7, 'Argument list too long')
            return real_call(fake, command, **kwargs)
        def write(fixture, **kwargs):
            if json.loads(kwargs['prompt'])['stage'] != 'build':  # a prompt that never left has no answer
                return real_write(fixture, **kwargs)
        with mock.patch.object(transport.FakeHerdr, '__call__', runner), \
                mock.patch.object(transport.HerdrBackendTests, 'write_transcript', write):
            result = self.drive()
            self.assertEqual(result['transport_rejection']['status'], 'not_sent')
            self.assertEqual(result['transport_rejection']['run_id'], self.build_admission()['run_id'])
            self.assertIn('not_sent', result['next_action'])
            supervised = driver.supervise(self.runs, self.mid, seconds=1)
            self.assertEqual(supervised['supervision'], 'blocked')
            self.assertEqual(supervised['iterations'], 1)
            driver.control.request(self.runs, self.mid, action='cancel', reason='fixture not_sent cancellation',
                                   idempotency_key='not-sent-cancel', run_id=self.build_admission()['run_id'])
            closed = self.drive()
        self.assertEqual(closed['status'], 'abandoned')
        self.assertFalse(self.build_admission()['active'])
        self.assertFalse(self.current()['active_writer'])
        self.assertEqual(self.operations('send-keys'), [])
        self.assertEqual([c for c in self.operations('prompt') if json.loads(c[-1])['stage'] == 'build'], [])

    def test_oversized_prompt_is_refused_before_any_admission(self):
        with mock.patch.object(self.Backend, 'max_prompt_bytes', 1024):
            with self.assertRaisesRegex(driver.HerdrMissionError, 'exceeds 1024 bytes'):
                self.drive()
        self.assertEqual(self.current()['admissions'], {})
        self.assertEqual(self.operations('prompt'), [])
        driver.control.request(self.runs, self.mid, action='cancel', reason='oversized prompt',
                               idempotency_key='oversized-cancel')
        with mock.patch.object(self.Backend, 'max_prompt_bytes', 1024):
            closed = self.drive()
        self.assertEqual(closed['status'], 'abandoned')
        self.assertEqual(self.current()['admissions'], {})


if __name__ == '__main__':
    unittest.main()
