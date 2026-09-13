"""Self-contained synthetic tests for fleet_herdr_opencode_evidence.

The fixtures are built in memory; no personal files, no external fixture
dependency and no network/subprocess access are used.
"""

import hashlib
import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_herdr_opencode_evidence as ev  # noqa: E402

SESSION = "ses_fixture"
CWD = "/fixture/candidate"
VERSION = "1.18.10"
PROVIDER = "deepseek"
MODEL = "deepseek-flash"
USER_ID = "msg_user"
FINAL_ID = "msg_final"
TOOL_ID = "msg_tool"
USER_TEXT = "Investigate fixture."
FINAL_TEXT = '{"status":"PASS","summary":"Synthetic fixture only."}'


def sha256_hex(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def text_part(part_id, message_id, text):
    return {
        "type": "text",
        "text": text,
        "id": part_id,
        "sessionID": SESSION,
        "messageID": message_id,
    }


def tool_part(part_id, message_id, state):
    part = {
        "type": "tool",
        "tool": "bash",
        "id": part_id,
        "sessionID": SESSION,
        "messageID": message_id,
    }
    if state is not None:
        part["state"] = state
    return part


def user_message(message_id=USER_ID, text=USER_TEXT, session=SESSION, parts=None):
    if parts is None:
        parts = [text_part("prt_%s_0" % message_id, message_id, text)]
    return {
        "info": {
            "role": "user",
            "time": {"created": 1000},
            "agent": "build",
            "id": message_id,
            "sessionID": session,
        },
        "parts": parts,
    }


def assistant_message(
    message_id,
    parent_id=USER_ID,
    finish="stop",
    text=FINAL_TEXT,
    created=2000,
    completed=2500,
    provider=PROVIDER,
    model=MODEL,
    cwd=CWD,
    session=SESSION,
    parts=None,
):
    info = {
        "parentID": parent_id,
        "role": "assistant",
        "mode": "build",
        "agent": "build",
        "path": {"cwd": cwd, "root": cwd},
        "modelID": model,
        "providerID": provider,
        "time": {"created": created, "completed": completed},
        "finish": finish,
        "id": message_id,
        "sessionID": session,
    }
    if parts is None:
        parts = [
            {
                "type": "step-start",
                "id": "prt_%s_s" % message_id,
                "sessionID": session,
                "messageID": message_id,
            },
            text_part("prt_%s_t" % message_id, message_id, text),
            {
                "type": "step-finish",
                "reason": finish,
                "id": "prt_%s_e" % message_id,
                "sessionID": session,
                "messageID": message_id,
            },
        ]
    return {"info": info, "parts": parts}


def tool_message(message_id=TOOL_ID):
    return {
        "info": {
            "parentID": USER_ID,
            "role": "assistant",
            "path": {"cwd": CWD, "root": CWD},
            "modelID": MODEL,
            "providerID": PROVIDER,
            "time": {"created": 1500, "completed": 1800},
            "finish": "tool-calls",
            "id": message_id,
            "sessionID": SESSION,
        },
        "parts": [
            {
                "type": "step-start",
                "id": "prt_%s_s" % message_id,
                "sessionID": SESSION,
                "messageID": message_id,
            },
            {
                "type": "tool",
                "tool": "bash",
                "state": {"status": "completed", "input": {}, "output": "ok"},
                "id": "prt_%s_c" % message_id,
                "sessionID": SESSION,
                "messageID": message_id,
            },
            {
                "type": "step-finish",
                "reason": "tool-calls",
                "id": "prt_%s_e" % message_id,
                "sessionID": SESSION,
                "messageID": message_id,
            },
        ],
    }


def make_messages(with_tool=True):
    messages = [user_message()]
    if with_tool:
        messages.append(tool_message())
    messages.append(assistant_message(FINAL_ID))
    return messages


def dump(messages, session=SESSION, cwd=CWD, version=VERSION):
    document = {
        "info": {
            "id": session,
            "directory": cwd,
            "version": version,
            "time": {"created": 1, "updated": 2},
        },
        "messages": messages,
    }
    return json.dumps(document).encode("utf-8")


def bindings(**overrides):
    values = {
        "session_id": SESSION,
        "user_message_id": USER_ID,
        "assistant_message_id": FINAL_ID,
        "cwd": CWD,
        "provider": PROVIDER,
        "model": MODEL,
        "prompt_sha256": sha256_hex(USER_TEXT),
        "final_bytes": FINAL_TEXT.encode("utf-8"),
        "cli_version": VERSION,
    }
    values.update(overrides)
    return values


def verify(raw=None, messages=None, **overrides):
    if raw is None:
        raw = dump(make_messages() if messages is None else messages)
    return ev.verify_export(raw, **bindings(**overrides))


class PositiveTests(unittest.TestCase):
    def test_intermediate_tool_then_final_verifies(self):
        result = verify()
        self.assertEqual(result["schema_version"], "herdr.opencode.evidence.v1")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["authority"], "none")
        self.assertEqual(result["prompt_sha256"], sha256_hex(USER_TEXT))
        self.assertEqual(result["final_sha256"], sha256_hex(FINAL_TEXT))
        self.assertEqual(result["export_sha256"], sha256_hex(dump(make_messages())))
        self.assertEqual(result["provider"], PROVIDER)
        self.assertEqual(result["model"], MODEL)
        self.assertEqual(result["cwd"], CWD)
        self.assertEqual(result["cli_version"], VERSION)

    def test_historical_other_turns_are_allowed(self):
        messages = [
            user_message(message_id="msg_prev"),
            assistant_message("msg_prev_final", parent_id="msg_prev", text="previous"),
            user_message(),
            tool_message(),
            assistant_message(FINAL_ID),
            user_message(message_id="msg_next"),
            assistant_message("msg_next_final", parent_id="msg_next", text="next"),
        ]
        result = verify(messages=messages)
        self.assertEqual(result["user_message_id"], USER_ID)
        self.assertEqual(result["assistant_message_id"], FINAL_ID)

    def test_multiple_user_text_parts_join_with_newline(self):
        parts = [
            text_part("prt_u_a", USER_ID, "alpha"),
            text_part("prt_u_b", USER_ID, "beta"),
        ]
        messages = [user_message(parts=parts), assistant_message(FINAL_ID)]
        result = verify(
            messages=messages, prompt_sha256=sha256_hex("alpha\nbeta")
        )
        self.assertEqual(result["status"], "verified")

    def test_multiple_final_text_parts_join_with_newline(self):
        parts = [
            text_part("prt_f_a", FINAL_ID, "first"),
            text_part("prt_f_b", FINAL_ID, "second"),
        ]
        messages = [user_message(), assistant_message(FINAL_ID, parts=parts)]
        result = verify(messages=messages, final_bytes=b"first\nsecond")
        self.assertEqual(result["status"], "verified")


class BindingTests(unittest.TestCase):
    def test_wrong_session_id(self):
        with self.assertRaises(ev.EvidenceError):
            verify(session_id="ses_other")

    def test_wrong_session_directory(self):
        with self.assertRaises(ev.EvidenceError):
            verify(cwd="/fixture/other")

    def test_wrong_session_version(self):
        with self.assertRaises(ev.EvidenceError):
            verify(cli_version="1.0.0")

    def test_wrong_provider(self):
        with self.assertRaises(ev.EvidenceError):
            verify(provider="other")

    def test_wrong_model(self):
        with self.assertRaises(ev.EvidenceError):
            verify(model="other-model")

    def test_wrong_prompt_hash(self):
        with self.assertRaises(ev.EvidenceError):
            verify(prompt_sha256=sha256_hex("different"))

    def test_altered_prompt_text(self):
        messages = [user_message(text="changed prompt"), assistant_message(FINAL_ID)]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_altered_final_text(self):
        messages = [user_message(), assistant_message(FINAL_ID, text="changed final")]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_missing_requested_assistant_id(self):
        with self.assertRaises(ev.EvidenceError):
            verify(assistant_message_id="msg_unknown")

    def test_final_bytes_must_be_bytes(self):
        with self.assertRaises(ev.EvidenceError):
            verify(final_bytes=FINAL_TEXT)

    def test_prompt_sha_must_be_string(self):
        with self.assertRaises(ev.EvidenceError):
            verify(prompt_sha256=b"deadbeef")


class SegmentTests(unittest.TestCase):
    def test_extra_user_before_final_is_rejected(self):
        messages = [
            user_message(),
            tool_message(),
            user_message(message_id="msg_interrupt"),
            assistant_message(FINAL_ID),
        ]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_final_must_be_last_of_segment(self):
        messages = [
            user_message(),
            tool_message(),
            assistant_message(FINAL_ID),
            assistant_message("msg_after", text="later step"),
        ]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_intermediate_cannot_be_selected_as_final(self):
        with self.assertRaises(ev.EvidenceError):
            verify(assistant_message_id=TOOL_ID)

    def test_final_from_later_turn_is_rejected(self):
        messages = [
            user_message(),
            assistant_message("msg_first_final"),
            user_message(message_id="msg_later"),
            assistant_message("msg_later_final", parent_id="msg_later", text="later"),
        ]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages, assistant_message_id="msg_later_final")

    def test_assistant_with_other_parent_is_rejected(self):
        messages = [
            user_message(),
            assistant_message(FINAL_ID, parent_id="msg_other"),
        ]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)


class CompletionTests(unittest.TestCase):
    def test_missing_completed_timestamp(self):
        messages = [user_message(), assistant_message(FINAL_ID)]
        del messages[1]["info"]["time"]["completed"]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_completed_before_created(self):
        messages = [
            user_message(),
            assistant_message(FINAL_ID, created=5000, completed=4000),
        ]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_boolean_timestamp_is_rejected(self):
        messages = [user_message(), assistant_message(FINAL_ID)]
        messages[1]["info"]["time"]["created"] = True
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_errored_final_is_rejected(self):
        messages = [user_message(), assistant_message(FINAL_ID)]
        messages[1]["info"]["error"] = {"name": "UnknownError"}
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_errored_intermediate_is_rejected(self):
        messages = [user_message(), tool_message(), assistant_message(FINAL_ID)]
        messages[1]["info"]["error"] = {"name": "UnknownError"}
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_intermediate_without_finish_is_pending(self):
        messages = [user_message(), tool_message(), assistant_message(FINAL_ID)]
        del messages[1]["info"]["finish"]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_final_without_stop_finish_is_rejected(self):
        messages = [
            user_message(),
            assistant_message(FINAL_ID, finish="tool-calls"),
        ]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_tool_calls_finish_requires_a_tool_part(self):
        messages = [user_message(),
                    assistant_message("msg_step", finish="tool-calls", text="thinking"),
                    assistant_message(FINAL_ID)]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)


class CrossReferenceTests(unittest.TestCase):
    def test_duplicate_message_id(self):
        messages = [user_message(), tool_message(), tool_message(), assistant_message(FINAL_ID)]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_duplicate_part_id(self):
        messages = [user_message(), assistant_message(FINAL_ID)]
        messages[1]["parts"][1]["id"] = "prt_duplicate"
        messages[0]["parts"][0]["id"] = "prt_duplicate"
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_message_session_reference_mismatch(self):
        messages = make_messages()
        messages[0]["info"]["sessionID"] = "ses_other"
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_part_session_reference_mismatch(self):
        messages = make_messages()
        messages[1]["parts"][1]["sessionID"] = "ses_other"
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_part_message_reference_mismatch(self):
        messages = make_messages()
        messages[1]["parts"][1]["messageID"] = "msg_other"
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)


class UserInputContractTests(unittest.TestCase):
    def test_synthetic_user_text_part_is_rejected(self):
        part = text_part("prt_u_0", USER_ID, USER_TEXT)
        part["synthetic"] = True
        messages = [user_message(parts=[part]), assistant_message(FINAL_ID)]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_ignored_user_text_part_is_rejected(self):
        part = text_part("prt_u_0", USER_ID, USER_TEXT)
        part["ignored"] = True
        messages = [user_message(parts=[part]), assistant_message(FINAL_ID)]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_attachment_user_part_is_rejected(self):
        parts = [
            {
                "type": "file",
                "path": "/fixture/file.txt",
                "id": "prt_u_file",
                "sessionID": SESSION,
                "messageID": USER_ID,
            }
        ]
        messages = [user_message(parts=parts), assistant_message(FINAL_ID)]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)


class MarkerStrictnessTests(unittest.TestCase):
    def test_user_synthetic_int_marker_is_rejected(self):
        part = text_part("prt_u_0", USER_ID, USER_TEXT)
        part["synthetic"] = 1
        messages = [user_message(parts=[part]), assistant_message(FINAL_ID)]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_user_ignored_string_marker_is_rejected(self):
        part = text_part("prt_u_0", USER_ID, USER_TEXT)
        part["ignored"] = "false"
        messages = [user_message(parts=[part]), assistant_message(FINAL_ID)]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_user_false_markers_are_allowed(self):
        part = text_part("prt_u_0", USER_ID, USER_TEXT)
        part["synthetic"] = False
        part["ignored"] = False
        messages = [user_message(parts=[part]), assistant_message(FINAL_ID)]
        self.assertEqual(verify(messages=messages)["status"], "verified")

    def test_final_synthetic_int_marker_is_rejected(self):
        part = text_part("prt_f_t", FINAL_ID, FINAL_TEXT)
        part["synthetic"] = 1
        parts = [
            {
                "type": "step-start",
                "id": "prt_f_s",
                "sessionID": SESSION,
                "messageID": FINAL_ID,
            },
            part,
        ]
        messages = [user_message(), assistant_message(FINAL_ID, parts=parts)]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_final_false_markers_are_allowed(self):
        part = text_part("prt_f_t", FINAL_ID, FINAL_TEXT)
        part["synthetic"] = False
        parts = [
            {
                "type": "step-start",
                "id": "prt_f_s",
                "sessionID": SESSION,
                "messageID": FINAL_ID,
            },
            part,
        ]
        messages = [user_message(), assistant_message(FINAL_ID, parts=parts)]
        self.assertEqual(verify(messages=messages)["status"], "verified")


class ToolStateTests(unittest.TestCase):
    def running_tool_segment(self):
        messages = [user_message(), tool_message(), assistant_message(FINAL_ID)]
        return messages

    def test_running_intermediate_tool_is_rejected(self):
        messages = self.running_tool_segment()
        messages[1]["parts"][1]["state"] = {"status": "running"}
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_pending_intermediate_tool_is_rejected(self):
        messages = self.running_tool_segment()
        messages[1]["parts"][1]["state"] = {"status": "pending"}
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_error_intermediate_tool_is_rejected(self):
        messages = self.running_tool_segment()
        messages[1]["parts"][1]["state"] = {"status": "error"}
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_missing_tool_state_is_rejected(self):
        messages = self.running_tool_segment()
        del messages[1]["parts"][1]["state"]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_non_object_tool_state_is_rejected(self):
        messages = self.running_tool_segment()
        messages[1]["parts"][1]["state"] = "completed"
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_missing_tool_status_is_rejected(self):
        messages = self.running_tool_segment()
        messages[1]["parts"][1]["state"] = {}
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_non_string_tool_status_is_rejected(self):
        messages = self.running_tool_segment()
        messages[1]["parts"][1]["state"] = {"status": 1}
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_running_final_tool_is_rejected(self):
        parts = [
            {
                "type": "step-start",
                "id": "prt_f_s",
                "sessionID": SESSION,
                "messageID": FINAL_ID,
            },
            tool_part("prt_f_c", FINAL_ID, {"status": "running"}),
            text_part("prt_f_t", FINAL_ID, FINAL_TEXT),
        ]
        messages = [user_message(), assistant_message(FINAL_ID, parts=parts)]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_completed_tool_is_allowed(self):
        messages = self.running_tool_segment()
        messages[1]["parts"][1]["state"] = {"status": "completed"}
        self.assertEqual(verify(messages=messages)["status"], "verified")


class JsonStrictnessTests(unittest.TestCase):
    def test_finite_json_numbers_are_allowed(self):
        raw = dump(make_messages()).replace(b'{"info"', b'{"score":1.25,"info"', 1)
        self.assertEqual(verify(raw=raw)["status"], "verified")

    def test_overflow_number_is_rejected(self):
        raw = dump(make_messages()).replace(b'{"info"', b'{"overflow":1e999,"info"', 1)
        with self.assertRaises(ev.EvidenceError):
            verify(raw=raw)

    def test_duplicate_keys_are_rejected(self):
        raw = b'{"info":{"id":"a","id":"b"},"messages":[]}'
        with self.assertRaises(ev.EvidenceError):
            verify(raw=raw)

    def test_nan_is_rejected(self):
        raw = b'{"info":{"id":NaN},"messages":[]}'
        with self.assertRaises(ev.EvidenceError):
            verify(raw=raw)

    def test_infinity_is_rejected(self):
        raw = b'{"info":{"id":Infinity},"messages":[]}'
        with self.assertRaises(ev.EvidenceError):
            verify(raw=raw)

    def test_truncated_json_is_rejected(self):
        raw = dump(make_messages())[:-3]
        with self.assertRaises(ev.EvidenceError):
            verify(raw=raw)

    def test_invalid_depth_is_rejected(self):
        nested = 0
        for _ in range(ev.MAX_JSON_DEPTH + 50):
            nested = [nested]
        raw = json.dumps(nested).encode("utf-8")
        with self.assertRaises(ev.EvidenceError):
            verify(raw=raw)

    def test_very_deep_json_is_rejected(self):
        raw = b"[" * 100000 + b"]" * 100000
        with self.assertRaises(ev.EvidenceError):
            verify(raw=raw)

    def test_raw_limit_is_enforced(self):
        raw = b" " * (ev.MAX_RAW_BYTES + 1)
        with self.assertRaises(ev.EvidenceError):
            verify(raw=raw)

    def test_non_bytes_raw_is_rejected(self):
        with self.assertRaises(ev.EvidenceError):
            verify(raw="{not bytes}")


class AuthorityTests(unittest.TestCase):
    def test_permissions_remain_not_attested(self):
        result = verify()
        self.assertEqual(
            result["permissions"],
            {"status": "not_attested", "scope": "identity_and_content_only"},
        )

    def test_pass_text_does_not_grant_authority(self):
        result = verify()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["authority"], "none")
        for forbidden in ("sandbox", "cost", "effort", "mission", "mission_acceptance"):
            self.assertNotIn(forbidden, result)

    def test_no_argument_can_request_attestation(self):
        with self.assertRaises(TypeError):
            ev.verify_export(
                dump(make_messages()),
                **bindings(),
                attested=True,
            )


class UnicodeTests(unittest.TestCase):
    def test_lone_surrogate_in_prompt_is_evidence_error(self):
        messages = [user_message(text="bad " + chr(0xD800) + " prompt"),
                    assistant_message(FINAL_ID)]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)

    def test_lone_surrogate_in_final_is_evidence_error(self):
        messages = [user_message(),
                    assistant_message(FINAL_ID, text="bad " + chr(0xD800) + " final")]
        with self.assertRaises(ev.EvidenceError):
            verify(messages=messages)


if __name__ == "__main__":
    unittest.main()
