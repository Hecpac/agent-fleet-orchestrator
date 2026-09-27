"""Consumer and transport checks using local fixtures only."""

import contextlib
import copy
import http.client
import io
import json
import unittest
from unittest.mock import patch
import urllib.error

import trial


class TypeSafeSyntheticTests(unittest.TestCase):
    def setUp(self):
        self.dataset, self.questions, self.mocks = trial.load_inputs()
        self.cases = self.dataset["cases"]

    def response(self, case_id="syn-01"):
        return {"model": trial.MODEL, "answers": copy.deepcopy(self.mocks[case_id])}

    def test_offline_cli_exercises_all_cases_without_network(self):
        with patch("trial.post_request", side_effect=AssertionError("Unexpected network")) as post:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = trial.main([])
        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        post.assert_not_called()
        self.assertEqual(report["cases_attempted"], 18)
        self.assertEqual(report["metrics"]["abstentions"], 6)
        self.assertEqual(report["requests_started"], 0)
        self.assertEqual(report["model_quality"], "NOT_VERIFIED")
        self.assertEqual(report["metrics_scope"], "simulated_consumer_only")
        self.assertIsNone(report["cost_usd"])
        self.assertEqual(report["authority"], "none")
        self.assertEqual(report["executed_actions"], [])

    def test_gold_labels_and_metadata_do_not_enter_request(self):
        case = copy.deepcopy(self.cases[0])
        case.update(id="ID_SECRET", tags=["TAG_SECRET"], expected={"intent": "GOLD_SECRET"})
        request = trial.make_request(case, self.questions)
        encoded = json.dumps(request)
        for sentinel in ("ID_SECRET", "TAG_SECRET", "GOLD_SECRET"):
            self.assertNotIn(sentinel, encoded)
        self.assertEqual(set(request), {"model", "state", "questions"})
        self.assertEqual(request["state"]["request"]["text"], case["text"])

    def test_prepare_cli_contains_only_reviewable_payloads(self):
        output = io.StringIO()
        with patch("trial.post_request", side_effect=AssertionError("Unexpected network")) as post:
            with contextlib.redirect_stdout(output):
                self.assertEqual(trial.main(["prepare", "--limit", "2"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(len(report["requests"]), 2)
        self.assertNotIn("expected", json.dumps(report["requests"]))
        self.assertEqual(report["requests_started"], 0)
        post.assert_not_called()

    def test_independent_uncertainty_gates_and_exact_threshold(self):
        answers = self.response()["answers"]
        answers["intent"]["confidence"] = 0.79
        self.assertEqual(trial.recommend(answers), ("human_review", "low_choice_confidence"))
        answers["intent"]["confidence"] = 0.99
        answers["single_intent"]["noul"] = 0.5
        self.assertEqual(trial.recommend(answers), ("human_review", "unclear_first_task"))
        answers["intent"]["confidence"] = 0.8
        answers["single_intent"]["noul"] = 0.8
        self.assertEqual(trial.recommend(answers), ("research", "advisory_only"))
        self.assertEqual(trial.recommend(self.response("syn-18")["answers"]), ("human_review", "no_match"))

    def test_malformed_responses_abstain_and_cannot_count_as_success(self):
        def change(field, value):
            response = self.response()
            response["answers"]["intent"][field] = value
            return response

        bad = [None, {}, {"model": "", "answers": {}}, change("choice", "deploy"),
               change("choice", []), change("choice", "build"), change("type", "score"),
               change("probabilities", {"research": 1.0}),
               change("probabilities", {key: 0.5 for key in self.questions["intent"]["criteria"]}),
               change("confidence", float("nan")), change("confidence", True),
               change("confidence", -0.1), change("confidence", 1.1),
               change("confidence", 10 ** 1000)]
        wrong_noul = self.response()
        wrong_noul["answers"]["single_intent"] = {"type": "noul", "noul": float("inf")}
        bad.append(wrong_noul)
        wrong_usage = self.response()
        wrong_usage["usage"] = {"input_tokens": -1}
        bad.append(wrong_usage)
        for response in bad:
            with self.subTest(response=response):
                with patch("trial.post_request", return_value=response) as post:
                    # This case already expects human review; a failure still must not pass.
                    report = trial.evaluate([self.cases[12]], self.questions, {}, mode="live", api_key="synthetic-key")
                post.assert_called_once()
                self.assertFalse(report["all_decisions_match"])
                self.assertEqual(report["cases"][0]["recommendation"], "human_review")
                self.assertEqual(report["cases"][0]["error"], "invalid_response")
                self.assertIsNone(report["metrics"]["intent_accuracy"])

    def test_injected_wrong_answer_is_detected_by_independent_labels(self):
        mocks = copy.deepcopy(self.mocks)
        mocks["syn-01"] = mocks["syn-05"]
        report = trial.evaluate(self.cases, self.questions, mocks)
        self.assertFalse(report["all_decisions_match"])
        self.assertLess(report["metrics"]["decision_agreement"], 1.0)
        self.assertLess(report["metrics"]["intent_accuracy"], 1.0)
        self.assertEqual(report["cases"][0]["recommendation"], "build")
        self.assertEqual(report["cases"][0]["expected"]["recommendation"], "research")

    def test_service_failures_stop_without_retry_or_error_body_logging(self):
        failures = [TimeoutError("PRIVATE_ERROR"),
                    urllib.error.URLError("PRIVATE_ERROR"),
                    http.client.IncompleteRead(b"PRIVATE_ERROR"),
                    urllib.error.HTTPError(trial.ENDPOINT, 429, "PRIVATE_ERROR", {}, None)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch("trial.post_request", side_effect=failure) as post:
                    report = trial.evaluate(self.cases, self.questions, {}, mode="live", api_key="synthetic-key")
                post.assert_called_once()
                self.assertEqual(report["requests_started"], 1)
                self.assertEqual(report["cases_attempted"], 1)
                self.assertEqual(report["errors"], 1)
                self.assertFalse(report["complete"])
                self.assertNotIn("PRIVATE_ERROR", json.dumps(report))
                self.assertNotIn("synthetic-key", json.dumps(report))

    def test_missing_usage_remains_unknown_even_after_partial_usage(self):
        report = trial.evaluate(self.cases, self.questions, self.mocks)
        self.assertIsNone(report["usage"]["input_tokens"])
        self.assertEqual(report["usage"]["requests_with_input_tokens"], 0)
        first = self.response()
        first["usage"] = {"input_tokens": 123, "output_tokens": 10}
        with patch("trial.post_request", side_effect=[first, self.response("syn-02")]):
            report = trial.evaluate(self.cases[:2], self.questions, {}, mode="live", api_key="synthetic-key")
        self.assertIsNone(report["usage"]["input_tokens"])
        self.assertEqual(report["usage"]["requests_with_input_tokens"], 1)

    def test_observed_usage_and_model_are_preserved_without_invented_cost(self):
        response = self.response()
        response["model"] = "provider-reported-version"
        response["usage"] = {"input_tokens": 123, "output_tokens": 0}
        with patch("trial.post_request", return_value=response):
            report = trial.evaluate(self.cases[:1], self.questions, {}, mode="live", api_key="synthetic-key")
        self.assertEqual(report["observed_models"], ["provider-reported-version"])
        self.assertEqual(report["usage"]["input_tokens"], 123)
        self.assertEqual(report["usage"]["output_tokens"], 0)
        self.assertIsNone(report["cost_usd"])

    def test_live_requires_bounded_limit_and_environment_key_before_transport(self):
        for args in (["live"], ["live", "--limit", "0"], ["live", "--limit", "19"], ["live", "--limit", "1"]):
            with self.subTest(args=args), patch.dict("os.environ", {}, clear=True):
                with patch("trial.post_request") as post, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        trial.main(args)
                self.assertEqual(error.exception.code, 2)
                post.assert_not_called()

    def test_http_request_matches_documented_wire_shape(self):
        response = self.response()
        with patch("trial.urllib.request.build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
            actual = trial.post_request(trial.make_request(self.cases[0], self.questions), "synthetic-key")
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, trial.ENDPOINT)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-key")
        self.assertEqual(json.loads(request.data)["questions"]["single_intent"]["type"], "noul")
        self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 30)
        self.assertEqual(actual, response)

    def test_non_json_and_oversized_http_bodies_are_rejected(self):
        for body in (b"not JSON", b'{"unknown":NaN}', b"x" * 1_000_001):
            with self.subTest(length=len(body)), patch("trial.urllib.request.build_opener") as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value = body
                with self.assertRaises(ValueError):
                    trial.post_request({}, "synthetic-key")

    def test_redirect_is_not_followed_with_credentials(self):
        handler = trial.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://example.invalid"))


if __name__ == "__main__":
    unittest.main()
