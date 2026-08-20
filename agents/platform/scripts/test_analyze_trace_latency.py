"""Unit tests for the observability skill's trace-latency analysis.

Run: python3 -m unittest agents/platform/scripts/test_analyze_trace_latency.py

This script does duration arithmetic and states the result to an SRE as fact,
so the tests pin the numbers: a recorded payload must come out as the exact
seconds it encodes, and a seconds/milliseconds slip must be visible in the
assertion, not hidden by a fuzzy match.
"""

import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from observability_script_harness import run_script  # noqa: E402

SCRIPT = "analyze_trace_latency.py"
ARGS = ["--project-id", "proj-1"]


def trace_list(*trace_ids):
    return {"traces": [{"traceId": t} for t in trace_ids]}


def span(name, start, end):
    return {"name": name, "startTime": start, "endTime": end}


class TestAnalyzeTraceLatency(unittest.TestCase):
    def test_a_recorded_trace_parses_to_the_expected_durations(self):
        detail = {"spans": [
            span("model-call", "2026-08-19T10:00:00Z", "2026-08-19T10:00:01.500000Z"),
            span("tool-call", "2026-08-19T10:00:01.500000Z", "2026-08-19T10:00:02.500000Z"),
        ]}
        code, out = run_script(SCRIPT, ARGS, [
            ("traces?", trace_list("abc123")),
            ("traces/abc123", detail),
        ])
        self.assertEqual(code, 0)
        self.assertIn("Trace ID: abc123", out)
        # 1.5s + 1s back to back: the trace spans exactly 2.5 seconds. A unit
        # slip would print 2500.000 or 0.003 here.
        self.assertIn("Total Duration: 2.500 seconds", out)
        self.assertIn("Total Spans: 2", out)
        # Sorted by duration descending, the slower span leads the breakdown.
        self.assertLess(out.index("model-call"), out.index("tool-call"))
        self.assertIn("1.500s", out)
        self.assertIn("60.0%", out)

    def test_nanosecond_timestamps_do_not_distort_the_arithmetic(self):
        # Cloud Trace emits nanosecond precision; fromisoformat takes at most
        # microseconds. The truncation must cost precision, not magnitude.
        detail = {"spans": [
            span("exact", "2026-08-19T10:00:00.123456789Z",
                 "2026-08-19T10:00:02.123456789Z"),
        ]}
        code, out = run_script(SCRIPT, ARGS, [
            ("traces?", trace_list("t1")),
            ("traces/t1", detail),
        ])
        self.assertEqual(code, 0)
        self.assertIn("Total Duration: 2.000 seconds", out)
        self.assertIn("2.000s", out)

    def test_an_empty_window_says_no_traces_rather_than_reporting_zeroes(self):
        code, out = run_script(SCRIPT, ARGS, [("traces?", {"traces": []})])
        self.assertEqual(code, 0)
        self.assertIn("No traces found in the specified window.", out)
        self.assertNotIn("Total Duration", out)

    def test_a_trace_whose_detail_fetch_fails_is_skipped_not_invented(self):
        err = urllib.error.HTTPError("url", 500, "boom", {}, None)
        err.read = lambda: b"backend error"
        code, out = run_script(SCRIPT, ARGS, [
            ("traces?", trace_list("gone")),
            ("traces/gone", err),
        ])
        self.assertEqual(code, 0)
        self.assertIn("HTTP Error 500", out)
        self.assertNotIn("Trace ID: gone", out)

    def test_spans_missing_timestamps_are_excluded_from_the_breakdown(self):
        detail = {"spans": [
            span("good", "2026-08-19T10:00:00Z", "2026-08-19T10:00:01Z"),
            {"name": "no-times"},
        ]}
        code, out = run_script(SCRIPT, ARGS, [
            ("traces?", trace_list("t2")),
            ("traces/t2", detail),
        ])
        self.assertEqual(code, 0)
        # The span count is honest about what arrived; the duration math only
        # uses what could be timed.
        self.assertIn("Total Spans: 2", out)
        self.assertIn("Total Duration: 1.000 seconds", out)
        self.assertNotIn("no-times", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
