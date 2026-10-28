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

    def get(self, url, timeout=None):
        self.urls.append(url)
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

    def test_malformed_entries_are_skipped(self):
        out, session = self._run({}, {"projects": [7, None], "clusters": [["p", "l"], "x"]})
        self.assertEqual(({}, {}), (out["lists"], out["gets"]))
        self.assertEqual([], session.urls)

    def test_main_refuses_a_request_that_is_not_a_json_object(self):
        for raw in ("not json", "[1]"):
            with self.subTest(raw=raw), mock.patch.object(sys, "stdin", io.StringIO(raw)):
                self.assertEqual(2, reads.main())

    def test_the_reconcile_calls_the_copy_the_sandbox_image_ships(self):
        dockerfile = (REPO / "deploy/sandbox/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("agents/platform/scripts/gke_cluster_reads.py", dockerfile)
        self.assertEqual("/opt/defaults/scripts/gke_cluster_reads.py", cluster_agent_reconcile.GKE_READS_HELPER)


if __name__ == "__main__":
    unittest.main()
