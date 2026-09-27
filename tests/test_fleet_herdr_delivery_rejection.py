"""K1: a completed prompt-bound turn whose final cannot be a role result.

Real driver, backend, verifier, ledger and CAS; only the Herdr transport and the
Codex transcript are synthetic. The unbound final is retained as a durable
rejection: it never becomes a verdict, it blocks further effects, and exact
cancellation closes the owned run without resending the prompt.
"""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fleet_artifacts
import fleet_herdr
import fleet_herdr_rejection
import fleet_json
from tests import test_fleet_herdr_mission as missions
from tests import test_fleet_herdr_skill_context as skill_context

FENCED = '```json\n{"status": "PASS"}\n```'


def final_text(text):
    def edit(rows):
        rows[-2]['payload']['content'][0]['text'] = text
        rows[-1]['payload']['last_agent_message'] = text
    return edit


class FencedPlanDeliveryRecoveryTests(skill_context.RejectedEvidenceRecoveryTests):
    """Pause, deadline, supervision and exact-cancel invariants of rejected executions."""

    def reject_plan(self):
        self.stage = 'plan'; self.mutate = lambda result: None
        self.transcript_edit = final_text(FENCED)
        return self.drive()

    def test_rejected_execution_telemetry_survives_offline_without_admission(self):
        import fleet_report
        self.stage = 'plan'; self.mutate = lambda result: None
        def edit(rows):
            counts = {"input_tokens": 150, "output_tokens": 20, "cached_input_tokens": 30}
            rows.insert(-2, {"type": "event_msg", "payload": {"type": "token_count",
                "info": {"total_token_usage": counts, "last_token_usage": counts}}})
            final_text(FENCED)(rows)
        self.transcript_edit = edit
        capture = self.Backend._capture_usage_baseline
        def seed_empty_session(backend, **kwargs):
            session = kwargs['member']['agent_session']['value']
            path = self.helper.tmp / ('empty-' + session + '.jsonl')
            metadata = {"type": "session_meta", "timestamp": "2026-09-06T00:00:00Z",
                "payload": {"id": session, "model_provider": "openai", "cli_version": self.fake.codex_version}}
            path.write_text(json.dumps(metadata) + '\n')
            self.transcripts[session] = path
            return capture(backend, **kwargs)
        with mock.patch.object(self.Backend, '_capture_usage_baseline', seed_empty_session):
            rejected = self.drive()
        self.assertEqual(rejected['evidence_rejection']['kind'], 'herdr_delivery_rejection')
        before = self.current(); calls = len(self.fake.calls)
        report = fleet_report.build_report(self.runs, self.mid)
        run = report['runs'][0]
        self.assertEqual(run['result_disposition'], 'rejected_delivery')
        self.assertIsNotNone(run['observed'])
        self.assertEqual((run['prompt_tokens'], run['completion_tokens'], run['cached_input_tokens']), (150, 20, 30))
        for path in self.transcripts.values():
            path.unlink()
        self.assertEqual(fleet_report.build_report(self.runs, self.mid), report)
        self.assertEqual(self.current(), before)
        self.assertEqual(len(self.fake.calls), calls)
        self.assertIsNone(self.admission()['result'])


class MiscopiedRunDeliveryRecoveryTests(skill_context.RejectedEvidenceRecoveryTests):
    def reject_plan(self):
        self.stage = 'plan'
        self.mutate = lambda result: result.update(run_id=str(uuid.uuid4()))
        return self.drive()


class DeliveryRejectionTests(unittest.TestCase):
    setUp = missions.HerdrProtocolRejectionTests.setUp
    current = missions.HerdrProtocolRejectionTests.current
    admission = missions.HerdrProtocolRejectionTests.admission
    drive = missions.HerdrProtocolRejectionTests.drive
    operations = missions.HerdrProtocolRejectionTests.operations
    request_cancel = missions.HerdrProtocolRejectionTests.request_cancel
    set_runtime = missions.HerdrProtocolRejectionTests.set_runtime

    def reject(self, reason):
        result = self.drive()
        proof = result['evidence_rejection']
        self.assertEqual(proof['kind'], 'herdr_delivery_rejection')
        self.assertEqual(proof['reason'], reason)
        self.assertEqual(proof['run_id'], self.admission()['run_id'])
        self.assertEqual(result['status'], 'running')
        self.assertTrue(result['next_action'].startswith('unbound final delivery rejected;'))
        self.assertIn('explicit cancellation', result['next_action'])
        self.assertTrue(self.admission()['active'])
        self.assertIsNone(self.admission()['result'])
        self.assertIsNone(fleet_herdr.load_result_rejection(self.runs, self.mid, proof['run_id']))
        results = self.runs / 'missions' / self.mid / 'herdr-results' / f"{proof['run_id']}.json"
        self.assertFalse(results.exists())
        return proof

    def build_prompts(self):
        return [c for c in self.operations('prompt') if json.loads(c[-1])['stage'] == 'build']

    def test_fenced_build_final_is_retained_and_exact_cancel_closes_writer(self):
        self.mutate = lambda result: None
        self.transcript_edit = final_text(FENCED)
        proof = self.reject('Codex final result is not JSON')
        self.assertEqual(proof['execution'], {'status': 'verified', 'reason': None})
        observed = fleet_json.loads(fleet_artifacts.get_bytes(self.runs, self.mid, proof['observed_result_artifact_id']))
        self.assertEqual(fleet_artifacts.get_bytes(self.runs, self.mid, observed['artifact_id']), FENCED.encode())
        self.assertEqual((observed['mission_id'], observed['run_id'], observed['instance_id']),
                         (self.mid, proof['run_id'], 'worker'))
        self.assertTrue(self.current()['active_writer'])
        self.request_cancel('fenced-build-cancel')
        self.set_runtime('idle')
        closed = self.drive()
        self.assertEqual(closed['status'], 'abandoned')
        self.assertEqual(closed['evidence_rejection'], proof)
        self.assertFalse(self.admission()['active'])
        self.assertFalse(self.current()['active_writer'])
        self.assertEqual(self.admission()['terminal']['status'], 'abandoned')
        self.assertIsNone(self.admission()['result'])
        self.assertEqual(self.operations('send-keys'), [])
        self.assertEqual(len(self.build_prompts()), 1)

    def test_rejection_recovers_from_cas_without_live_transcript_or_resend(self):
        self.mutate = lambda result: None
        self.transcript_edit = final_text(FENCED)
        proof = self.reject('Codex final result is not JSON')
        for path in self.transcripts.values():
            path.unlink()
        again = self.drive()
        self.assertEqual(again['evidence_rejection'], proof)
        self.assertEqual(len(self.build_prompts()), 1)
        self.assertEqual(self.operations('send-keys'), [])

    def test_non_object_json_final_is_rejected(self):
        self.mutate = lambda result: None
        self.transcript_edit = final_text('[1]')
        self.reject('Codex final result must be an object')

    def test_miscopied_run_identity_is_rejected(self):
        self.mutate = lambda result: result.update(run_id=str(uuid.uuid4()))
        self.reject('Codex final result binding mismatch')

    def test_candidate_tree_drift_is_rejected(self):
        self.mutate = lambda result: result.update(candidate_tree_sha='0' * 40)
        self.reject('Codex final candidate_tree_sha drift')

    def test_reserved_backend_field_is_rejected(self):
        self.mutate = lambda result: result.update(artifact_id='0' * 64)
        self.reject('Codex final result cannot supply reserved field artifact_id')

    def test_unverifiable_execution_with_unbound_final_is_retained_and_cancellable(self):
        self.mutate = lambda result: None
        def edit(rows):
            for row in rows:
                if row['type'] == 'turn_context':
                    row['payload'].pop('sandbox_policy', None)
            final_text(FENCED)(rows)
        self.transcript_edit = edit
        proof = self.reject('Codex final result is not JSON')
        self.assertEqual(proof['execution']['status'], 'rejected')
        self.assertTrue(proof['execution']['reason'])
        self.request_cancel('unverifiable-cancel')
        self.set_runtime('idle')
        self.assertEqual(self.drive()['status'], 'abandoned')
        self.assertEqual(len(self.build_prompts()), 1)

    def test_interrupted_pointer_publication_recovers_same_proof_without_resend(self):
        import fleet_safe_paths
        self.mutate = lambda result: None
        self.transcript_edit = final_text(FENCED)
        original = fleet_safe_paths.RootedFS.atomic_write
        attempted = []
        def interrupted(fs, path, data, **kwargs):
            if Path(str(path)).name.startswith('herdr-delivery-rejection-'):
                attempted.append(fleet_json.loads(data)['artifact_id'])
                raise RuntimeError('fixture interruption before pointer')
            return original(fs, path, data, **kwargs)
        with mock.patch.object(fleet_safe_paths.RootedFS, 'atomic_write', interrupted), \
                self.assertRaisesRegex(RuntimeError, 'fixture interruption'):
            self.drive()
        proof = self.reject('Codex final result is not JSON')
        self.assertEqual(attempted, [proof['artifact_id']])
        self.assertEqual(len(self.build_prompts()), 1)

    def test_substituted_rejection_pointer_fails_closed(self):
        self.mutate = lambda result: None
        self.transcript_edit = final_text(FENCED)
        proof = self.reject('Codex final result is not JSON')
        forged = dict(proof); forged.pop('artifact_id')
        forged['reason'] = 'Codex final result binding mismatch'
        pin = fleet_artifacts.put_bytes(self.runs, self.mid, fleet_json.canonical_bytes(forged))['artifact_id']
        pointer = self.runs / fleet_herdr_rejection.delivery_relative(self.mid, proof['run_id'])
        pointer.write_bytes(fleet_json.canonical_bytes({'artifact_id': pin}) + b'\n')
        with self.assertRaisesRegex(ValueError, 'no longer reproduces'):
            self.drive()
        self.assertEqual(len(self.build_prompts()), 1)


if __name__ == '__main__':
    unittest.main()
