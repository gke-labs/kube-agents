"""Unit tests for the observability skill's token-usage delta.

Run: python3 -m unittest agents/platform/scripts/test_check_token_usage.py

The script sums counter deltas across time series and prints them as the day's
token spend. The tests pin the integers exactly: point ordering, the int64
string format the REST API uses, and counter resets all change the number, and
a wrong number here is stated fluently to whoever asked.
"""

import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from observability_script_harness import run_script  # noqa: E402

SCRIPT = "check_token_usage.py"
ARGS = ["--project-id", "proj-1"]

INPUT_METRIC = "litellm_input_tokens_metric_total"
OUTPUT_METRIC = "litellm_output_tokens_metric_total"
CACHED_METRIC = "litellm_input_cached_tokens_metric_total"


def point(end_time, value):
    return {"interval": {"endTime": end_time}, "value": value}


def series(points):
    return {"timeSeries": [{"points": points}]}


EMPTY = {"timeSeries": []}


def run(input_payload, output_payload=EMPTY, cached_payload=EMPTY):
    return run_script(SCRIPT, ARGS, [
        (INPUT_METRIC, input_payload),
        (CACHED_METRIC, cached_payload),
        (OUTPUT_METRIC, output_payload),
    ])


class TestCheckTokenUsage(unittest.TestCase):
    def test_a_recorded_payload_parses_to_the_expected_delta(self):
        # The API returns newest first; the script must sort before diffing,
        # or 100 -> 350 reads as a reset and the delta comes out as 100.
        payload = series([
            point("2026-08-19T10:05:00Z", {"doubleValue": 350.0}),
            point("2026-08-19T10:00:00Z", {"doubleValue": 100.0}),
        ])
        code, out = run(payload)
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["input_tokens"], 250)
        self.assertEqual(result["output_tokens"], 0)
        self.assertEqual(result["cached_input_tokens"], 0)

    def test_int64_values_arrive_as_strings_and_still_count(self):
        # REST returns int64Value as a string. Dropping it to the 0 fallback
        # would report a silent zero for every counter metric.
        payload = series([
            point("2026-08-19T10:00:00Z", {"int64Value": "1000"}),
            point("2026-08-19T10:05:00Z", {"int64Value": "4000"}),
        ])
        code, out = run(EMPTY, output_payload=payload)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["output_tokens"], 3000)

    def test_a_counter_reset_adds_the_post_reset_value_not_a_negative(self):
        # Pod restarts reset the counter: 500 -> 30 means 30 new tokens since
        # the reset, not -470.
        payload = series([
            point("2026-08-19T10:00:00Z", {"doubleValue": 500.0}),
            point("2026-08-19T10:05:00Z", {"doubleValue": 30.0}),
        ])
        code, out = run(payload)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["input_tokens"], 30)

    def test_deltas_sum_across_pods(self):
        payload = {"timeSeries": [
            {"points": [point("2026-08-19T10:00:00Z", {"doubleValue": 0.0}),
                        point("2026-08-19T10:05:00Z", {"doubleValue": 100.0})]},
            {"points": [point("2026-08-19T10:00:00Z", {"doubleValue": 0.0}),
                        point("2026-08-19T10:05:00Z", {"doubleValue": 42.0})]},
        ]}
        code, out = run(payload)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["input_tokens"], 142)

    def test_no_data_reports_zeroes_in_the_documented_shape(self):
        code, out = run(EMPTY)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
        })

    def test_an_api_error_is_printed_beside_the_zero_it_causes(self):
        # Current behaviour: a failed metric query contributes 0 but the error
        # is printed first, so the zero is never silent. If this ever changes
        # to a bare 0 with no message, that is a regression worth failing on.
        err = urllib.error.HTTPError("url", 500, "boom", {}, None)
        err.read = lambda: b"backend error"
        code, out = run(err)
        self.assertEqual(code, 0)
        self.assertIn("HTTP Error 500", out)
        self.assertIn(INPUT_METRIC, out)
        self.assertIn('"input_tokens": 0', out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
