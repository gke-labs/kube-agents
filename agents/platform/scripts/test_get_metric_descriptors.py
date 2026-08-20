"""Unit tests for the observability skill's metric-descriptor listing.

Run: python3 -m unittest agents/platform/scripts/test_get_metric_descriptors.py
"""

import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from observability_script_harness import run_script  # noqa: E402

SCRIPT = "get_metric_descriptors.py"
ARGS = ["--project-id", "proj-1"]
METRICS_URL = "metricDescriptors"


class TestGetMetricDescriptors(unittest.TestCase):
    def test_a_recorded_payload_keeps_only_the_litellm_metrics(self):
        payload = {"metricDescriptors": [
            {"type": "prometheus.googleapis.com/litellm_input_tokens_metric_total/counter"},
            {"type": "kubernetes.io/container/cpu/core_usage_time"},
            {"type": "prometheus.googleapis.com/litellm_output_tokens_metric_total/counter"},
            {"description": "a descriptor with no type at all"},
        ]}
        code, out = run_script(SCRIPT, ARGS, [(METRICS_URL, payload)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [
            "prometheus.googleapis.com/litellm_input_tokens_metric_total/counter",
            "prometheus.googleapis.com/litellm_output_tokens_metric_total/counter",
        ])

    def test_no_matching_metrics_reports_an_empty_list(self):
        payload = {"metricDescriptors": [
            {"type": "kubernetes.io/container/memory/used_bytes"},
        ]}
        code, out = run_script(SCRIPT, ARGS, [(METRICS_URL, payload)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])

    def test_an_empty_response_reports_an_empty_list(self):
        code, out = run_script(SCRIPT, ARGS, [(METRICS_URL, {})])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])

    def test_an_api_error_exits_nonzero_rather_than_reporting_no_metrics(self):
        err = urllib.error.HTTPError("url", 500, "boom", {}, None)
        err.read = lambda: b"backend error"
        code, out = run_script(SCRIPT, ARGS, [(METRICS_URL, err)])
        self.assertEqual(code, 1)
        self.assertIn("HTTP Error 500", out)

    def test_a_connection_failure_exits_nonzero(self):
        code, out = run_script(
            SCRIPT, ARGS,
            [(METRICS_URL, urllib.error.URLError("no route to host"))],
        )
        self.assertEqual(code, 1)
        self.assertIn("Failed to connect", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
