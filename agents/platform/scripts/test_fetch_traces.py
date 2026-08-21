"""Unit tests for the observability skill's raw trace fetch.

Run: python3 -m unittest agents/platform/scripts/test_fetch_traces.py

fetch_traces.py is a pass-through: whatever the Trace API returns is printed
verbatim as JSON. The property worth pinning is exactly that — it neither
invents nor drops anything — plus the failure paths exiting nonzero instead of
printing something that could be mistaken for an empty result.
"""

import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from observability_script_harness import run_script  # noqa: E402

SCRIPT = "fetch_traces.py"
ARGS = ["--project-id", "proj-1"]
TRACES_URL = "cloudtrace.googleapis.com"


class TestFetchTraces(unittest.TestCase):
    def test_a_recorded_payload_is_printed_verbatim(self):
        payload = {"traces": [
            {"traceId": "abc123", "projectId": "proj-1"},
            {"traceId": "def456", "projectId": "proj-1"},
        ]}
        code, out = run_script(SCRIPT, ARGS, [(TRACES_URL, payload)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), payload)

    def test_an_empty_window_prints_the_empty_response_not_an_error(self):
        code, out = run_script(SCRIPT, ARGS, [(TRACES_URL, {})])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {})

    def test_an_api_error_exits_nonzero_with_the_status_named(self):
        err = urllib.error.HTTPError("url", 500, "boom", {}, None)
        err.read = lambda: b"backend error"
        code, out = run_script(SCRIPT, ARGS, [(TRACES_URL, err)])
        self.assertEqual(code, 1)
        self.assertIn("HTTP Error 500", out)

    def test_a_connection_failure_exits_nonzero(self):
        code, out = run_script(
            SCRIPT, ARGS,
            [(TRACES_URL, urllib.error.URLError("connection refused"))],
        )
        self.assertEqual(code, 1)
        self.assertIn("Failed to connect", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
