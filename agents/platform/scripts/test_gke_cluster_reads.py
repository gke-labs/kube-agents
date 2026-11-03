"""Tests for gke_cluster_reads: the reconcile's GKE reads through the broker's relay (#3703)."""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cluster_agent_reconcile  # noqa: E402
import gke_cluster_reads as reads  # noqa: E402

REPO = Path(__file__).resolve().parents[3]


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


class _Session:
    """An ApiSession stand-in answering from a URL -> (status, body) table."""

    def __init__(self, answers):
        self.answers = answers
        self.urls = []
        self.timeouts = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        self.timeouts.append(timeout)
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return _Response(*answer)


class GkeClusterReadsTest(unittest.TestCase):
    API = "https://container.googleapis.com/v1"

    def _run(self, answers, request):
        session = _Session(answers)
        with mock.patch.object(reads, "_session", return_value=session):
            return reads.run(request), session

    def test_a_list_names_each_cluster_and_keeps_missing_zones(self):
        url = f"{self.API}/projects/p1/locations/-/clusters"
        out, _ = self._run({url: (200, {"clusters": [{"name": "a", "location": "us-central1"},
                                                       {"name": "b", "location": "us-east1-b"}],
                                          "missingZones": ["europe-west1-b"]})},
                           {"projects": ["p1"]})
        self.assertEqual({"status": 200, "clusters": [["a", "us-central1"], ["b", "us-east1-b"]],
                          "missingZones": ["europe-west1-b"]}, out["lists"]["p1"])

    def test_an_empty_project_lists_no_clusters(self):
        url = f"{self.API}/projects/p1/locations/-/clusters"
        out, _ = self._run({url: (200, {})}, {"projects": ["p1"]})
        self.assertEqual({"status": 200, "clusters": []}, out["lists"]["p1"])

    def test_an_error_is_reported_with_its_body_not_decided(self):
        url = f"{self.API}/projects/p1/locations/-/clusters"
        error = {"error": {"code": 403, "status": "PERMISSION_DENIED"}}
        out, _ = self._run({url: (403, error)}, {"projects": ["p1"]})
        self.assertEqual({"status": 403, "error": error}, out["lists"]["p1"])

    def test_a_get_reports_its_status_and_any_error(self):
        ok = f"{self.API}/projects/p1/locations/us-central1/clusters/a"
        gone = f"{self.API}/projects/p1/locations/us-central1/clusters/b"
        not_found = {"error": {"code": 404, "status": "NOT_FOUND"}}
        out, _ = self._run({ok: (200, {"name": "a"}), gone: (404, not_found)},
                           {"clusters": [["p1", "us-central1", "a"], ["p1", "us-central1", "b"]]})
        self.assertEqual({"status": 200}, out["gets"]["p1/us-central1/a"])
        self.assertEqual({"status": 404, "error": not_found}, out["gets"]["p1/us-central1/b"])

    def test_a_call_that_never_answers_is_status_zero(self):
        url = f"{self.API}/projects/p1/locations/-/clusters"
        out, _ = self._run({url: ConnectionError("refused")}, {"projects": ["p1"]})
        self.assertEqual(0, out["lists"]["p1"]["status"])
        self.assertEqual("ConnectionError", out["lists"]["p1"]["error"]["exception"])

    def test_a_non_json_error_body_is_passed_as_text(self):
        url = f"{self.API}/projects/p1/locations/us-central1/clusters/a"
        out, _ = self._run({url: (404, "<html>no</html>")}, {"clusters": [["p1", "us-central1", "a"]]})
        self.assertEqual({"status": 404, "error": {"text": "<html>no</html>"}}, out["gets"]["p1/us-central1/a"])

    def test_a_call_reads_for_no_longer_than_the_budget_left(self):
        url = f"{self.API}/projects/p1/locations/-/clusters"
        for asked, budget in ((25.0, 25.0), (7.0, 7.0), (None, reads.DEFAULT_BUDGET_SECONDS),
                              (0, reads.DEFAULT_BUDGET_SECONDS), (True, reads.DEFAULT_BUDGET_SECONDS)):
            with self.subTest(asked=asked), mock.patch.object(reads.time, "monotonic", return_value=1000.0):
                _, session = self._run({url: (200, {})}, {"projects": ["p1"], "budget_seconds": asked})
                (connect, read), = session.timeouts
                self.assertAlmostEqual(min(budget, connect + reads.MAX_READ_TIMEOUT), connect + read)
                # A short budget is not spent on the connect allowance: the read gets half.
                self.assertGreaterEqual(read, min(budget / 2, reads.MAX_READ_TIMEOUT))

    def test_a_call_the_budget_cannot_cover_is_not_started(self):
        # The read starts 24.5 s into a 25 s budget: it is not sent, and the row says why,
        # so the caller can read that one some other way.
        url = f"{self.API}/projects/p1/locations/-/clusters"
        with mock.patch.object(reads.time, "monotonic", side_effect=[1000.0, 1024.5]):
            out, session = self._run({url: (200, {})}, {"projects": ["p1"], "budget_seconds": 25})
        self.assertEqual([], session.urls)
        self.assertEqual({"status": 0, "error": {"skipped": "budget"}}, out["lists"]["p1"])

    def test_a_long_json_error_is_passed_back_whole(self):
        # A long Google error keeps its reason, which sits after the message.
        url = f"{self.API}/projects/p1/locations/-/clusters"
        error = {"error": {"code": 403, "message": "x" * 5000, "status": "PERMISSION_DENIED",
                           "details": [{"reason": "SERVICE_DISABLED"}]}}
        out, _ = self._run({url: (403, error)}, {"projects": ["p1"]})
        self.assertEqual(error, out["lists"]["p1"]["error"])

    def test_malformed_entries_are_skipped(self):
        out, session = self._run({}, {"projects": [7, None], "clusters": [["p", "l"], "x"]})
        self.assertEqual(({}, {}), (out["lists"], out["gets"]))
        self.assertEqual([], session.urls)

    def test_main_refuses_a_request_that_is_not_a_json_object(self):
        for raw in ("not json", "[1]"):
            with self.subTest(raw=raw), mock.patch.object(sys, "stdin", io.StringIO(raw)):
                self.assertEqual(2, reads.main())

    def test_the_reconcile_runs_the_root_owned_copy(self):
        # `hermes` runs it, so it is the trusted copy and never one an agent-writable
        # directory supplies; test_sandbox_delivery holds that copy staged and closed.
        self.assertEqual(reads.SANDBOX_GKE_READS, cluster_agent_reconcile.GKE_READS_HELPER)
        self.assertTrue(reads.SANDBOX_GKE_READS.startswith("/opt/vcs/libexec/platform/"))
        dockerfile = (REPO / "deploy/sandbox/Dockerfile").read_text(encoding="utf-8")
        defaults = dockerfile[dockerfile.index("COPY agents/platform/scripts/sandbox_exec.py"):]
        defaults = defaults[:defaults.index("/opt/defaults/scripts/")]
        self.assertNotIn("gke_cluster_reads.py", defaults)


if __name__ == "__main__":
    unittest.main()
