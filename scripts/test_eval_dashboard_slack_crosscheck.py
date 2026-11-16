"""scripts/eval_dashboard/slack_crosscheck.py: the Slack lane's overhead beside the environments'.

Run: python3 -m unittest discover -s scripts -p 'test_eval_dashboard_slack_crosscheck.py' -v
"""

import http.client
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from eval_dashboard import slack_crosscheck

NOW = datetime.now(timezone.utc)
VALUES = {"delivery_lag_s": "1.0", "post_s": "0.8", "first_ack_p50_s": "0.6", "first_ack_p95_s": "2.4", "deliveries": "120"}


def token_runner(cmd, **kwargs):
    return subprocess.CompletedProcess(cmd, 0, stdout="tok\n", stderr="")


class FakeOpener:
    """Answers each project's instant queries; a project mapped to an int answers with that HTTP status."""

    def __init__(self, projects):
        self.projects, self.queries = projects, []

    def __call__(self, request, timeout):
        project = request.full_url.split("/projects/")[1].split("/")[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["query"][0]
        self.queries.append(query)
        answer = self.projects[project]
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "no", {}, io.BytesIO(b""))
        key = next(k for k, q in slack_crosscheck.QUERIES.items() if q == query)
        result = [{"metric": {}, "value": [0, answer[key]]}] if key in answer else []
        return io.BytesIO(json.dumps({"status": "success", "data": {"resultType": "vector", "result": result}}).encode())


def lane_data(days_ago_values):
    return {"slack_overhead": [{"build_id": str(i), "date": (NOW - timedelta(days=d)).date().isoformat(),
                                "delivery_lag_s": v, "post_s": v, "first_ack_p50_s": v, "first_ack_p95_s": v}
                               for i, (d, v) in enumerate(days_ago_values)]}


class CrosscheckTest(unittest.TestCase):
    def run_main(self, opener, prior=None, data=None):
        tmp = pathlib.Path(tempfile.mkdtemp())
        (tmp / "data.json").write_text(json.dumps(data or lane_data([(1, 0.5), (2, 0.3), (20, 9.0)])))
        if prior is not None:
            (tmp / "prev.json").write_text(json.dumps(prior))
        argv = ["--data", str(tmp / "data.json"), "--env", "autopush=p-auto", "--env", "staging=p-stage",
                "--prior", str(tmp / "prev.json"), "--out", str(tmp / "out.json")]
        self.assertEqual(0, slack_crosscheck.main(argv, runner=token_runner, opener=opener))
        out = tmp / "out.json"
        return json.loads(out.read_text()) if out.exists() else None

    def test_the_week_is_read_beside_the_lane(self):
        got = self.run_main(FakeOpener({"p-auto": VALUES, "p-stage": {}}))
        self.assertEqual({"nights": 2, "delivery_lag_s": 0.4, "post_s": 0.4, "first_ack_p50_s": 0.4, "first_ack_p95_s": 0.4},
                         got["lane"], "the night 20 days back is outside the window")
        auto = got["environments"]["autopush"]
        self.assertEqual((1.0, 120.0, 2.5, 6.0), (auto["delivery_lag_s"], auto["deliveries"], auto["ratio"]["delivery_lag_s"], auto["ratio"]["first_ack_p95_s"]))
        self.assertIsNone(got["environments"]["staging"]["first_ack_p50_s"], "no series yet reads as None")

    def test_every_query_is_scoped_to_the_host_cluster(self):
        opener = FakeOpener({"p-auto": VALUES, "p-stage": VALUES})
        self.run_main(opener)
        self.assertTrue(opener.queries)
        self.assertTrue(all(slack_crosscheck.CLUSTER_LABEL in q for q in opener.queries))

    def test_a_403_skips_that_environment_and_no_read_writes_nothing(self):
        with mock.patch.object(slack_crosscheck, "log") as log:
            got = self.run_main(FakeOpener({"p-auto": 403, "p-stage": 403}))
        self.assertIsNone(got, "nothing written, so the first tick after the grant runs")
        skipped = [c.args[0] for c in log.call_args_list if "skipped" in c.args[0]]
        self.assertEqual(2, len(skipped))
        self.assertIn("roles/monitoring.viewer", skipped[0])
        partial = self.run_main(FakeOpener({"p-auto": 403, "p-stage": VALUES}))
        self.assertEqual((["staging"], False), (list(partial["environments"]), partial["complete"]))
        # A partial document is not the week's run: the next tick reads again, and autopush's grant shows.
        again = self.run_main(FakeOpener({"p-auto": VALUES, "p-stage": VALUES}), prior=partial)
        self.assertEqual((["autopush", "staging"], True), (sorted(again["environments"]), again["complete"]))

    def test_a_failure_other_than_403_is_logged_with_its_reason(self):
        with mock.patch.object(slack_crosscheck, "log") as log:
            self.assertIsNone(self.run_main(FakeOpener({"p-auto": 500, "p-stage": 401})))
        lines = [c.args[0] for c in log.call_args_list]
        self.assertTrue(any("autopush (p-auto): HTTP 500" in line for line in lines), lines)
        self.assertTrue(any("staging (p-stage): HTTP 401" in line for line in lines), lines)

    def test_a_recent_run_is_not_repeated(self):
        opener = FakeOpener({"p-auto": VALUES, "p-stage": VALUES})
        self.assertIsNone(self.run_main(opener, prior={"generated_at": (NOW - timedelta(days=2)).isoformat()}))
        self.assertEqual([], opener.queries)
        self.assertIsNotNone(self.run_main(opener, prior={"generated_at": (NOW - timedelta(days=8)).isoformat()}))

    def test_a_zero_reads_as_a_ratio_and_the_night_a_week_back_counts(self):
        got = self.run_main(FakeOpener({"p-auto": {**VALUES, "post_s": "0"}, "p-stage": VALUES}),
                            data=lane_data([(7, 0.5)]))
        self.assertEqual(1, got["lane"]["nights"], "the night dated seven days back is inside the window")
        self.assertEqual(0.0, got["environments"]["autopush"]["ratio"]["post_s"])

    def test_a_non_finite_answer_reads_as_none(self):
        self.assertIsNone(slack_crosscheck._value({"data": {"result": [{"value": [0, "NaN"]}]}}))
        self.assertIsNone(slack_crosscheck._value({"data": {"result": []}}))
        self.assertIsNone(slack_crosscheck._value([1, 2]), "a JSON value that is not an object")
        self.assertIsNone(slack_crosscheck._value({"data": "x"}))

    def test_a_truncated_body_is_that_environments_error(self):
        def opener(request, timeout):
            if "/projects/p-auto/" in request.full_url:
                raise http.client.IncompleteRead(b"")
            return FakeOpener({"p-stage": VALUES})(request, timeout)

        got = self.run_main(opener)
        self.assertIn("IncompleteRead", got["environments"]["autopush"]["error"])
        self.assertEqual(1.0, got["environments"]["staging"]["delivery_lag_s"], "the next environment is still read")


if __name__ == "__main__":
    unittest.main()
