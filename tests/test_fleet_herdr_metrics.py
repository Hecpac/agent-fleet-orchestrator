import contextlib
import io
import unittest
from unittest import mock

from tests import test_fleet_herdr_control
import fleet_herdr_metrics as metrics
import fleet_trace
import fleet_mission_state as state


def event(kind, **payload):
    return {"type": "event_msg", "payload": {"type": kind, **payload}}


def count(total, last=None):
    return event("token_count", info={"total_token_usage": total, "last_token_usage": last})


class UsageTests(unittest.TestCase):
    def test_cumulative_delta_avoids_double_counting_duplicate_snapshots(self):
        base = {"input_tokens":100, "output_tokens":10, "cached_input_tokens":20}
        final = {"input_tokens":300, "output_tokens":40, "cached_input_tokens":80}
        rows = [count(base), event("task_started",turn_id="t"),count(final),count(final),event("task_complete",turn_id="t")]
        result = metrics.usage(rows,"t")
        self.assertEqual((result["prompt_tokens"],result["completion_tokens"],result["cached_input_tokens"]),(200,30,60))
        self.assertEqual(result["usage_scope"],"observed_runtime_counters_not_billing")

    def test_first_turn_baseline_requires_observed_total_equal_last(self):
        first = {"input_tokens":10,"output_tokens":2,"cached_input_tokens":0}
        end = {"input_tokens":30,"output_tokens":5,"cached_input_tokens":10}
        rows = [event("task_started",turn_id="t"),count(first,first),count(end),event("task_complete",turn_id="t")]
        self.assertEqual(metrics.usage(rows,"t")["prompt_tokens"],30)
        rows[1]=count(first)
        self.assertIsNone(metrics.usage(rows,"t")["prompt_tokens"])

    def test_counter_reset_and_overlapping_turns_remain_unknown(self):
        first={"input_tokens":100,"output_tokens":10,"cached_input_tokens":20}
        smaller={"input_tokens":50,"output_tokens":5,"cached_input_tokens":10}
        rows=[count(first),event("task_started",turn_id="t"),count(smaller),event("task_complete",turn_id="t")]
        self.assertEqual(metrics.usage(rows,"t")["usage_reason"],"usage_counter_reset_or_regression")
        rows.insert(2,event("task_started",turn_id="other"))
        self.assertEqual(metrics.usage(rows,"t")["usage_reason"],"overlapping_turn_usage")
        rows=[event("task_started",turn_id="other"),count(first),event("task_started",turn_id="t"),
              count(first),event("task_complete",turn_id="t")]
        self.assertEqual(metrics.usage(rows,"t")["usage_reason"],"overlapping_turn_usage")

    def test_cached_delta_must_be_subset_even_when_cumulative_snapshots_are_valid(self):
        base = {"input_tokens":100,"output_tokens":10,"cached_input_tokens":0}
        final = {"input_tokens":105,"output_tokens":11,"cached_input_tokens":10}
        rows = [count(base),event("task_started",turn_id="t"),count(final),event("task_complete",turn_id="t")]
        result = metrics.usage(rows,"t")
        self.assertIsNone(result["prompt_tokens"])
        self.assertEqual(result["usage_reason"],"invalid_usage_counter_delta")

    def test_invalid_snapshot_is_never_silently_skipped(self):
        base = {"input_tokens":100,"output_tokens":10,"cached_input_tokens":20}
        valid = {"input_tokens":150,"output_tokens":20,"cached_input_tokens":30}
        invalid = {**valid,"cached_input_tokens":999}
        for sequence in ([count(valid),count(invalid)], [count(invalid),count(valid)]):
            rows = [count(base),event("task_started",turn_id="t"),*sequence,event("task_complete",turn_id="t")]
            result = metrics.usage(rows,"t")
            self.assertIsNone(result["prompt_tokens"])
            self.assertEqual(result["usage_reason"],"invalid_usage_counter_snapshot")

    def test_dispatch_to_start_gap_cannot_add_prior_usage_to_the_new_turn(self):
        base={"input_tokens":100,"output_tokens":10,"cached_input_tokens":20}
        between={"input_tokens":120,"output_tokens":12,"cached_input_tokens":25}
        final={"input_tokens":150,"output_tokens":20,"cached_input_tokens":30}
        baseline={"kind":"herdr_usage_baseline","status":"known","counts":base,
                  "source":"pre_dispatch_session_counter"}
        rows=[count(between),event("task_started",turn_id="t"),count(final),event("task_complete",turn_id="t")]
        observed=metrics.usage(rows,"t",baseline,baseline_frontier={"status":"verified"})
        self.assertIsNone(observed["prompt_tokens"])
        self.assertEqual(observed["usage_reason"],"usage_counter_changed_before_bound_turn")
        rows[0]=count(base)
        self.assertEqual(metrics.usage(rows,"t",baseline)["prompt_tokens"],50)


class IntervalTests(unittest.TestCase):
    def test_open_wait_is_unknown_and_trace_does_not_invent_duration(self):
        helper=test_fleet_herdr_control.HerdrControlTests()
        helper.setUp()
        try:
            helper.request("pause")
            identifier="0a6b6f2e-060d-413f-95ab-8b64a8d3e070"
            state.append_event(helper.runs,helper.mid,kind="herdr_interval_started",actor="CONTROL",idempotency_key="lost-observation",
                payload={"interval_id":identifier,"kind":"controller_wait","run_id":None})
            events=state.read_events(state.ledger_path(helper.runs,helper.mid))
            timing=metrics.timing(helper.current(),events,[])
            self.assertIsNone(timing["controller_wait_seconds"])
            self.assertEqual(timing["controller_wait_observed_seconds"],0)
            span=next(s for s in fleet_trace.events_to_spans(events) if s["span_id"]=="interval:"+identifier)
            self.assertIsNone(span["end_timestamp"])
            self.assertIsNone(span["attributes"]["elapsed_seconds"])
        finally:
            helper.doCleanups()

    def test_wall_partition_does_not_add_overlapping_pause_and_execution(self):
        def stamp(second):return "2026-09-07T01:00:"+str(second).zfill(2)+"Z"
        events=[{"kind":"mission_created","timestamp":stamp(0),"payload":{}},
            {"kind":"herdr_control_requested","timestamp":stamp(2),"payload":{"action":"pause"}},
            {"kind":"herdr_control_applied","timestamp":stamp(5),"payload":{"action":"pause"}},
            {"kind":"herdr_control_requested","timestamp":stamp(8),"payload":{"action":"resume"}},
            {"kind":"mission_terminal","timestamp":stamp(10),"payload":{}}]
        current={"status":"failed","herdr_control":{},"herdr_intervals":{"i":{"kind":"controller_wait","started_at":stamp(0),"ended_at":stamp(5),"elapsed_ns":5_000_000_000}}}
        result=metrics.timing(current,events,[{"started_at":stamp(0),"ended_at":stamp(5)}])
        self.assertEqual(result["requested_pause_seconds"],6)
        self.assertEqual(result["paused_seconds"],3)
        self.assertEqual(result["controller_wait_seconds"],5)
        self.assertEqual(sum(result["wall_partition"].values()),10)
        self.assertEqual(result["wall_partition"]["unknown"],2)


class ObserveWriteTests(unittest.TestCase):
    """A failed interval write never replaces the observed operation's outcome."""

    def setUp(self):
        helper = test_fleet_herdr_control.HerdrControlTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.runs, self.mid, self.current = helper.runs, helper.mid, helper.current

    def failing(self, kind, error):
        real = state.append_event
        def append(runs, mid, **kwargs):
            if kwargs["kind"] == kind:
                raise error
            return real(runs, mid, **kwargs)
        return mock.patch.object(metrics.state, "append_event", append)

    def events(self):
        return state.read_events(state.ledger_path(self.runs, self.mid))

    def interval(self):
        (interval,) = self.current()["herdr_intervals"].values()
        return interval

    def test_unrecorded_end_returns_the_result_and_leaves_the_duration_unknown(self):
        stderr = io.StringIO()
        with self.failing("herdr_interval_finished", OSError("disk full")), contextlib.redirect_stderr(stderr):
            self.assertEqual(metrics.observe(self.runs, self.mid, "controller_operation", lambda: "sent"), "sent")
        self.assertIn("end not recorded (OSError: disk full); its duration is unknown", stderr.getvalue())
        self.assertIsNone(self.interval()["ended_at"])
        timing = metrics.timing(self.current(), self.events(), [])
        self.assertIsNone(timing["controller_operation_seconds"])
        self.assertEqual(timing["open_interval_reason"], "missing_end_is_unknown_not_elapsed_wait")
        span = next(s for s in fleet_trace.events_to_spans(self.events()) if s["span_id"].startswith("interval:"))
        self.assertEqual(span["attributes"]["reason"], "interval_end_not_observed")

    def test_unrecorded_end_reraises_the_operation_error_with_a_note(self):
        class BackendFailure(Exception):
            pass
        def callback():
            raise BackendFailure("transport lost")
        # A MissionConflict from the end write must not reach callers that handle the operation's conflicts.
        with self.failing("herdr_interval_finished", state.MissionConflict("terminal race")):
            with self.assertRaises(BackendFailure) as caught:
                metrics.observe(self.runs, self.mid, "controller_operation", callback)
        self.assertEqual(str(caught.exception), "transport lost")
        self.assertEqual(len(caught.exception.__notes__), 1)
        self.assertIn("end not recorded (MissionConflict: terminal race)", caught.exception.__notes__[0])
        self.assertIsNone(self.interval()["ended_at"])

    def test_recorded_end_keeps_the_operation_error_without_notes(self):
        def callback():
            raise KeyError("missing")
        with self.assertRaises(KeyError) as caught:
            metrics.observe(self.runs, self.mid, "controller_wait", callback)
        self.assertFalse(getattr(caught.exception, "__notes__", None))
        self.assertEqual(self.interval()["outcome"], "raised")

    def test_unrecorded_start_never_runs_the_operation(self):
        calls = []
        with self.failing("herdr_interval_started", OSError("read-only ledger")):
            with self.assertRaisesRegex(OSError, "read-only ledger"):
                metrics.observe(self.runs, self.mid, "controller_operation", lambda: calls.append("sent"))
        self.assertEqual(calls, [])
        self.assertFalse(self.current().get("herdr_intervals"))
