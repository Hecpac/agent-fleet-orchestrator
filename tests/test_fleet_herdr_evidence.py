from pathlib import Path
import copy
import hashlib
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_herdr_evidence as evidence
import fleet_json


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.expected = dict(agent_session="session", model="gpt-5.6-sol", turn_id="turn",
                             prompt_sha256=hashlib.sha256(b"task").hexdigest(), final_bytes=b'{"status":"PASS"}')
        self.rows = [
            {"type": "session_meta", "payload": {"id": "session", "model_provider": "openai"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
            {"type": "turn_context", "payload": {"turn_id": "turn", "model": "gpt-5.6-sol", "effort": "high"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": '{"status":"PASS"}'}]}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn", "last_agent_message": '{"status":"PASS"}'}},
        ]

    def raw(self, rows=None):
        return b"".join(fleet_json.canonical_bytes(r) + b"\n" for r in (self.rows if rows is None else rows))

    def test_valid_context_before_after_user_and_compaction(self):
        evidence.verify_transcript(self.raw(), **self.expected)
        self.rows[2], self.rows[3] = self.rows[3], self.rows[2]
        self.rows.insert(4, copy.deepcopy(self.rows[3]))
        evidence.verify_transcript(self.raw(), **self.expected)

    def test_tampered_identity_model_effort_prompt_final_completion(self):
        mutations = [(0, "id", "wrong"), (0, "model_provider", "other"), (2, "model", "other"),
                     (2, "effort", "low"), (2, "turn_id", "wrong"), (5, "turn_id", "wrong"),
                     (5, "last_agent_message", "different")]
        for index, field, value in mutations:
            with self.subTest(field=field, index=index):
                rows = copy.deepcopy(self.rows)
                rows[index]["payload"][field] = value
                with self.assertRaises(evidence.EvidenceError):
                    evidence.verify_transcript(self.raw(rows), **self.expected)
        for field, value in (("prompt_sha256", "0" * 64), ("final_bytes", b"wrong"), ("turn_id", "wrong")):
            with self.subTest(field=field), self.assertRaises(evidence.EvidenceError):
                evidence.verify_transcript(self.raw(), **{**self.expected, field: value})

    def test_missing_or_duplicate_completion_and_prompt_rejected(self):
        for rows in (self.rows[:-1], self.rows + [self.rows[-1]], self.rows[:4] + [self.rows[3]] + self.rows[4:]):
            with self.assertRaises(evidence.EvidenceError):
                evidence.verify_transcript(self.raw(rows), **self.expected)

    def test_partial_or_malformed_jsonl_rejected(self):
        for raw in (self.raw()[:-1], self.raw() + b"{", b"[]\n", b'{"type":"x","type":"y"}\n'):
            with self.assertRaises(evidence.EvidenceError):
                evidence.verify_transcript(raw, **self.expected)

    def test_context_after_final_cannot_attest_result(self):
        context = self.rows.pop(2)
        self.rows.insert(-1, context)
        with self.assertRaisesRegex(evidence.EvidenceError, "context follows"):
            evidence.verify_transcript(self.raw(), **self.expected)

    def test_duplicate_start_extra_user_and_crlf_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows.insert(2, copy.deepcopy(rows[1]))
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify_transcript(self.raw(rows), **self.expected)
        rows = copy.deepcopy(self.rows)
        extra = copy.deepcopy(rows[3])
        extra["payload"]["content"][0]["text"] = "do another task"
        rows.insert(4, extra)
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify_transcript(self.raw(rows), **self.expected)
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify_transcript(self.raw().replace(b"\n", b"\r\n"), **self.expected)

    def test_prior_complete_turn_allowed_but_duplicate_prompt_rejected(self):
        prior = copy.deepcopy(self.rows[1:])
        for row in prior:
            if "turn_id" in row["payload"]:
                row["payload"]["turn_id"] = "prior"
        prior[2]["payload"]["content"][0]["text"] = "earlier task"
        evidence.verify_transcript(self.raw(self.rows[:1] + prior + self.rows[1:]), **self.expected)
        prior[2]["payload"]["content"][0]["text"] = "task"
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify_transcript(self.raw(self.rows[:1] + prior + self.rows[1:]), **self.expected)
