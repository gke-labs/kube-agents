"""vertex_429.py: the pool's Vertex 429 share from Cloud Monitoring."""

import http.client
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from eval_dashboard import health, vertex_429  # noqa: E402

NOW = datetime(2026, 10, 22, 14, 0, tzinfo=timezone.utc)


def series(code, points):
    return {"metric": {"type": "serviceruntime.googleapis.com/api/request_count", "labels": {"response_code": code}}, "resource": {"type": "consumed_api"}, "points": [{"interval": {"endTime": end, "startTime": end}, "value": {"int64Value": str(n)}} for end, n in points]}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Answers Monitoring GETs per project: a document, or an HTTP status."""

    def __init__(self, answers):
        self.answers = answers
        self.urls = []

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        project = request.full_url.split("/projects/")[1].split("/")[0]
        answer = self.answers.get(project, 403)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "nope", {}, None)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(json.dumps(answer).encode("utf-8"))


class BucketsTest(unittest.TestCase):
    def test_a_projects_series_become_buckets_by_end_time(self):
        doc = {"timeSeries": [series("200", [("2026-10-22T13:00:00Z", 90), ("2026-10-22T13:10:00Z", 50)]), series("429", [("2026-10-22T13:00:00Z", 10)]), "junk", {"metric": {}, "points": [{"value": {"int64Value": "x"}}]}]}
        self.assertEqual(vertex_429.buckets_from(doc), {"2026-10-22T13:00:00Z": {"requests": 100, "429": 10}, "2026-10-22T13:10:00Z": {"requests": 50, "429": 0}})

    def test_the_pool_document_sums_projects_and_records_the_refused(self):
        a = {"timeSeries": [series("200", [("2026-10-22T13:00:00Z", 90)]), series("429", [("2026-10-22T13:00:00Z", 10)])]}
        b = {"timeSeries": [series("200", [("2026-10-22T13:00:00Z", 100)])]}
        opener = FakeOpener({"p-a": a, "p-b": b, "p-c": 403, "p-d": [], "p-e": http.client.IncompleteRead(b"{")})
        doc = vertex_429.read_pool(["p-a", "p-b", "p-c", "p-d", "p-e"], NOW, "tok", opener)
        self.assertEqual((doc["read"], doc["errors"]), (["p-a", "p-b"], {"p-c": "HTTP 403", "p-d": "not a JSON object", "p-e": "IncompleteRead: IncompleteRead(1 bytes read)"}))
        self.assertEqual(doc["buckets"], [{"end": "2026-10-22T13:00:00Z", "requests": 200, "429": 10}])
        self.assertEqual((doc["window_start"], doc["window_end"], doc["bucket_seconds"], doc["projects"]), ("2026-10-22T08:00:00+00:00", "2026-10-22T14:00:00+00:00", 600, 5))
        first = sorted(opener.urls)[0]
        self.assertIn("interval.startTime=2026-10-22T08%3A00%3A00%2B00%3A00", first)
        self.assertIn("alignmentPeriod=600s", first)
        self.assertIn("groupByFields=metric.labels.response_code", first)

    def test_the_reading_reaches_back_as_far_as_the_storm_rule_looks(self):
        self.assertGreaterEqual(vertex_429.LOOKBACK, health.STORM_WINDOW + health.STORM_429_LOOKBACK + health.STORM_429_READ_SLACK)
        self.assertLessEqual(vertex_429.KEEP_MAX_AGE, health.VERTEX_429_MAX_AGE, "a kept reading is one the rule would still judge on")

    def test_no_token_reads_nothing_and_says_why(self):
        doc = vertex_429.read_pool(["p-a"], NOW, None, FakeOpener({}))
        self.assertEqual((doc["read"], doc["errors"], doc["buckets"]), ([], {"p-a": "no access token"}, []))


class MainTest(unittest.TestCase):
    def test_main_writes_the_document_for_the_pools_projects(self):
        ci_deploy = 'gitops_repo_for_project() {\n  case "$1" in\n    kube-agents-evals) echo x ;;\n    kube-agents-evals-2) echo y ;;\n  esac\n}\n'
        with tempfile.TemporaryDirectory() as tmp:
            deploy = pathlib.Path(tmp, "ci-deploy.sh")
            deploy.write_text(ci_deploy)
            out = pathlib.Path(tmp, "vertex-429.json")
            runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "tok\n", "")
            opener = FakeOpener({"kube-agents-evals": {"timeSeries": []}, "kube-agents-evals-2": 403})
            code = vertex_429.main(["--out", str(out), "--ci-deploy", str(deploy), "--now", NOW.isoformat(), "--token-command", "true"], runner=runner, opener=opener)
            doc = json.loads(out.read_text())
        self.assertEqual((code, doc["read"], doc["errors"]), (0, ["kube-agents-evals"], {"kube-agents-evals-2": "HTTP 403"}))

    def test_a_ci_deploy_without_the_pool_mapping_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            deploy = pathlib.Path(tmp, "ci-deploy.sh")
            deploy.write_text("echo no mapping here\n")
            out = pathlib.Path(tmp, "v.json")
            code = vertex_429.main(["--out", str(out), "--ci-deploy", str(deploy)], runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "tok\n", ""), opener=FakeOpener({}))
        self.assertEqual((code, out.exists()), (vertex_429.EXIT_USAGE, False))

    def test_a_read_that_reaches_no_project_keeps_the_previous_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            deploy = pathlib.Path(tmp, "ci-deploy.sh")
            deploy.write_text('gitops_repo_for_project() {\n  case "$1" in\n    kube-agents-evals) echo x ;;\n  esac\n}\n')
            out = pathlib.Path(tmp, "v.json")
            fresh = json.dumps({"read": ["kube-agents-evals"], "read_at": (NOW - timedelta(hours=1)).isoformat(), "previous": True})
            out.write_text(fresh)
            runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "tok\n", "")
            code = vertex_429.main(["--out", str(out), "--ci-deploy", str(deploy), "--now", NOW.isoformat()], runner=runner, opener=FakeOpener({}))
            kept = out.read_text()
            # A good reading older than the rule judges on is replaced too.
            out.write_text(json.dumps({"read": ["kube-agents-evals"], "read_at": (NOW - vertex_429.KEEP_MAX_AGE - timedelta(minutes=1)).isoformat()}))
            vertex_429.main(["--out", str(out), "--ci-deploy", str(deploy), "--now", NOW.isoformat()], runner=runner, opener=FakeOpener({}))
            aged = json.loads(out.read_text())
            # A previous reading that was itself blind is replaced, so the errors and read_at stay current.
            out.write_text('{"read": [], "errors": {"kube-agents-evals": "HTTP 403"}, "read_at": "2026-10-01T00:00:00+00:00"}')
            vertex_429.main(["--out", str(out), "--ci-deploy", str(deploy), "--now", NOW.isoformat()], runner=runner, opener=FakeOpener({}))
            replaced = json.loads(out.read_text())
        self.assertEqual((code, kept), (0, fresh))
        self.assertEqual((replaced["read"], replaced["read_at"]), ([], NOW.isoformat()))
        self.assertEqual((aged["read"], aged["read_at"]), ([], NOW.isoformat()))

    def test_a_failed_token_command_is_a_warning_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            deploy = pathlib.Path(tmp, "ci-deploy.sh")
            deploy.write_text('gitops_repo_for_project() {\n  case "$1" in\n    kube-agents-evals) echo x ;;\n  esac\n}\n')
            out = pathlib.Path(tmp, "v.json")
            runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no creds")
            code = vertex_429.main(["--out", str(out), "--ci-deploy", str(deploy), "--now", NOW.isoformat()], runner=runner, opener=FakeOpener({}))
            doc = json.loads(out.read_text())
        self.assertEqual((code, doc["errors"]), (0, {"kube-agents-evals": "no access token"}))


if __name__ == "__main__":
    unittest.main()
