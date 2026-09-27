#!/usr/bin/env python3
"""Bounded TypeSafe trial; synthetic data and advisory results only."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
POLICY = {"min_confidence": 0.80, "min_single_intent": 0.80}
INPUT_FILES = ("cases.json", "questions.json", "mock_answers.json")


def load_inputs():
    dataset, questions, mocks = [
        json.loads((ROOT / name).read_text(encoding="utf-8")) for name in INPUT_FILES
    ]
    cases = dataset["cases"]
    ids = [case["id"] for case in cases]
    if dataset.get("synthetic") is not True or mocks.get("synthetic") is not True:
        raise ValueError("This trial requires explicitly synthetic inputs")
    if not cases or len(set(ids)) != len(ids) or set(ids) != set(mocks["answers_by_case"]):
        raise ValueError("Case IDs must be unique and match the mock fixtures")
    return dataset, questions, mocks["answers_by_case"]


def make_request(case, questions, model=MODEL):
    # Explicit allowlist: labels, IDs, tags, and mock answers never reach the model.
    return {
        "model": model,
        "state": {
            "synthetic": True,
            "request": {"text": case["text"], "language": case["language"]},
        },
        "questions": questions,
    }


def probability(value):
    if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
        raise ValueError("Expected a finite probability in [0, 1]")
    return value


def validate_response(response, questions):
    if not isinstance(response, dict) or not isinstance(response.get("model"), str):
        raise ValueError("Missing response model")
    if not response["model"].strip():
        raise ValueError("Empty response model")
    answers = response.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError("Answer IDs do not match the questions")
    intent, clarity = answers["intent"], answers["single_intent"]
    if not isinstance(intent, dict) or intent.get("type") != "choice":
        raise ValueError("Expected a Choice answer")
    options = questions["intent"]["criteria"]
    probs = intent.get("probabilities")
    if not isinstance(probs, dict) or set(probs) != set(options):
        raise ValueError("Incomplete or unknown Choice options")
    for value in probs.values():
        probability(value)
    if not math.isclose(sum(probs.values()), 1.0, abs_tol=1e-5):
        raise ValueError("Choice probabilities must sum to one")
    choice = intent.get("choice")
    if not isinstance(choice, str) or choice not in probs:
        raise ValueError("Unknown Choice value")
    if probs[choice] + 1e-8 < max(probs.values()):
        raise ValueError("Choice must be a maximum-probability option")
    probability(intent.get("confidence"))
    if not isinstance(clarity, dict) or clarity.get("type") != "noul":
        raise ValueError("Expected a Noul answer")
    probability(clarity.get("noul"))
    usage = response.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise ValueError("Invalid usage object")
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Invalid token count")
    return answers


def recommend(answers):
    intent, clarity = answers["intent"], answers["single_intent"]
    if intent["choice"] == "no_match":
        return "human_review", "no_match"
    if intent["confidence"] < POLICY["min_confidence"]:
        return "human_review", "low_choice_confidence"
    if clarity["noul"] < POLICY["min_single_intent"]:
        return "human_review", "unclear_first_task"
    return intent["choice"], "advisory_only"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_request(payload, api_key):
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    # One attempt only. An ambiguous timeout must not cause an automatic resend.
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as result:
        body = result.read(1_000_001)
    if len(body) > 1_000_000:
        raise ValueError("Response exceeds the trial's size limit")

    def reject_constant(value):
        raise ValueError("Non-finite JSON number")

    return json.loads(body, parse_constant=reject_constant)


def evaluate(cases, questions, mocks, *, mode="offline", model=MODEL, api_key=None):
    if mode not in {"offline", "live"}:
        raise ValueError("Expected offline or live mode")
    if mode == "live" and not api_key:
        raise ValueError("TYPESAFE_API_KEY is required for live mode")
    rows = []
    requests_started = 0
    for case in cases:
        request = make_request(case, questions, model)
        row = {
            "id": case["id"], "request": request, "expected": case["expected"],
            "response": None, "latency_ms": None, "error": None,
        }
        started = time.perf_counter()
        try:
            if mode == "offline":
                response = {"model": "synthetic-fixture", "answers": mocks[case["id"]]}
            else:
                requests_started += 1
                response = post_request(request, api_key)
            # Assign only after validation, so malformed responses cannot leak arbitrary text.
            answers = validate_response(response, questions)
            row["response"] = response
            row["recommendation"], row["reason"] = recommend(answers)
            row["matches_expected"] = row["recommendation"] == case["expected"]["recommendation"]
        except urllib.error.HTTPError as exc:
            row["error"] = f"http_{exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            row["error"] = "transport_error"
        except (ValueError, TypeError, KeyError):
            row["error"] = "invalid_response"
        if mode == "live":
            row["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        if row["error"]:
            row.update(recommendation="human_review", reason=row["error"], matches_expected=False)
        rows.append(row)
        if row["error"]:
            break  # Preserve the partial report; do not silently continue or retry.

    valid = [row for row in rows if row["error"] is None]
    labeled = [row for row in valid if row["expected"]["intent"] is not None]
    proposed = [row for row in valid if row["recommendation"] != "human_review"]

    def fraction(numerator, denominator):
        return round(numerator / denominator, 6) if denominator else None

    usages = [(row["response"].get("usage") or {}) for row in valid]
    usage = {}
    for key in ("input_tokens", "output_tokens"):
        known = [entry[key] for entry in usages if entry.get(key) is not None]
        usage[key] = sum(known) if len(known) == len(rows) and rows else None
        usage[f"requests_with_{key}"] = len(known)
    return {
        "schema_version": 1,
        "mode": mode,
        "data_origin": "synthetic",
        "answer_origin": "handwritten_fixtures" if mode == "offline" else "http_responses",
        "metrics_scope": "simulated_consumer_only" if mode == "offline" else "synthetic_sample_only",
        "authority": "none",
        "executed_actions": [],
        "requested_model": model,
        "observed_models": sorted({row["response"]["model"] for row in valid}),
        "policy": dict(POLICY),
        "threshold_calibration": "NOT_VERIFIED",
        "model_quality": "NOT_VERIFIED",
        "billed_cost": "NOT_VERIFIED",
        "cost_usd": None,
        "usage": usage,
        "requests_started": requests_started,
        "cases_selected": len(cases),
        "cases_attempted": len(rows),
        "valid_responses": len(valid),
        "errors": len(rows) - len(valid),
        "complete": len(rows) == len(cases) and len(valid) == len(cases),
        "all_decisions_match": bool(rows) and len(rows) == len(cases) and all(
            row["matches_expected"] for row in rows
        ),
        "metrics": {
            "decision_agreement": fraction(sum(row["matches_expected"] for row in rows), len(rows)),
            "intent_labeled_responses": len(labeled),
            "intent_accuracy": fraction(sum(
                row["response"]["answers"]["intent"]["choice"] == row["expected"]["intent"]
                for row in labeled
            ), len(labeled)),
            "proposal_coverage": fraction(len(proposed), len(rows)),
            "proposal_precision": fraction(sum(row["matches_expected"] for row in proposed), len(proposed)),
            "abstentions": len(valid) - len(proposed),
            "single_intent_brier": fraction(sum(
                (row["response"]["answers"]["single_intent"]["noul"] - row["expected"]["single_intent"]) ** 2
                for row in valid
            ), len(valid)),
        },
        "cases": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("offline", "prepare", "live"), nargs="?", default="offline")
    parser.add_argument("--limit", type=int, help="Required in live mode; maximum number of requests")
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args(argv)
    dataset, questions, mocks = load_inputs()
    if args.mode == "live" and args.limit is None:
        parser.error("live requires an explicit --limit")
    if args.limit is not None and not 1 <= args.limit <= len(dataset["cases"]):
        parser.error(f"--limit must be between 1 and {len(dataset['cases'])}")
    if not args.model.strip():
        parser.error("--model must be non-empty")
    cases = dataset["cases"][:args.limit]
    api_key = os.environ.get("TYPESAFE_API_KEY") if args.mode == "live" else None
    if args.mode == "live" and not api_key:
        parser.error("live requires TYPESAFE_API_KEY in the process environment")
    if args.mode == "prepare":
        report = {
            "mode": "prepare", "data_origin": "synthetic", "authority": "none",
            "endpoint": ENDPOINT, "requests_started": 0,
            "requests": [{"case_id": case["id"], "body": make_request(case, questions, args.model)} for case in cases],
        }
    else:
        report = evaluate(cases, questions, mocks, mode=args.mode, model=args.model, api_key=api_key)
    report["dataset_id"] = dataset["dataset_id"]
    report["file_sha256"] = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in (*INPUT_FILES, "trial.py")
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if args.mode == "prepare" or report["all_decisions_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
