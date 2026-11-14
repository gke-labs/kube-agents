"""The nightly pages' estimated end-to-end Slack times (scripts/eval_dashboard/slack_estimate.py),
and collect.py's read of the Slack lane's overhead.

Run: python3 -m unittest discover -s scripts -p 'test_eval_dashboard_slack_estimate.py' -v
"""

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from eval_dashboard import collect, nightly, slack_estimate

JOB = "ci-kube-agents-eval-nightly"
SHAPE = {"opens_on_verdict": True, "apology": False, "bot_messages": 3}


def rep(n, agent_s):
    return {"n": n, "result": "pass", "reason": None, "agent_s": agent_s, "shape": dict(SHAPE)}


def night(build, day, tasks):
    return {"build_id": build, "tier": "nightly", "job": JOB, "pr": None, "head_sha": build[-7:],
            "started": f"{day}T00:00:00+00:00", "finished": f"{day}T06:00:00+00:00", "result": "SUCCESS",
            "duration_s": 21600, "tasks": tasks}


def data(overheads):
    tasks = lambda: [
        {"name": "case-a", "result": "pass", "reps": [rep(1, 20.0), rep(2, 20.0), rep(3, 20.0)]},
        {"name": "case-b", "result": "pass", "reps": [rep(1, 100.0), rep(2, 100.0), rep(3, 100.0)]},
    ]
    doc = {"schema_version": 1, "generated_at": "2026-09-09T12:00:00+00:00",
           "cases": [{"name": n, "domain": "cost", "active": True, "nightly_active": True} for n in ("case-a", "case-b")],
           "runs": [night("3000000000000000001", "2026-09-07", tasks()), night("3000000000000000002", "2026-09-08", tasks())]}
    if overheads is not None:
        doc["slack_overhead"] = overheads
    return doc


OVERHEAD = {"build_id": "9", "date": "2026-09-08", "delivery_lag_s": 0.5, "post_s": 0.4,
            "first_ack_p50_s": 0.6, "first_ack_p95_s": 1.2}


class SlackEstimateTest(unittest.TestCase):
    def test_a_nights_cases_get_their_estimate_and_the_night_its_summary(self):
        newest, older = nightly.night_reports(data([OVERHEAD]))
        by_case = {c["case"]: c for c in newest["cases"]}
        self.assertEqual(21.7, by_case["case-a"]["slack_e2e_s"], "20 s + 0.5 s lag + 3 posts x 0.4 s")
        self.assertEqual(101.7, by_case["case-b"]["slack_e2e_s"])
        self.assertEqual({"e2e_p50_s": 21.7, "e2e_p95_s": 101.7, "e2e_p95_7n_s": 101.7, "first_ack_p50_s": 0.6,
                          "first_ack_p95_s": 1.2, "delivery_lag_s": 0.5, "post_s": 0.4, "build_id": "9"}, newest["slack"])
        self.assertNotIn("slack", older, "no overhead was recorded on the older night's date")
        self.assertNotIn("slack_e2e_s", older["cases"][0])

    def test_quiet_without_the_lane(self):
        for nights in (nightly.night_reports(data(None)), nightly.night_reports(data([]))):
            self.assertTrue(all("slack" not in n for n in nights))

    def test_the_p95_is_averaged_over_seven_dates(self):
        nights = [{"started": f"2026-09-{20 - i:02d}T00:00:00+00:00", "slack": {"e2e_p95_s": float(10 - i)}} for i in range(10)] + [{}]
        # A same-date re-run of the newest night counts once.
        nights.insert(1, {"started": "2026-09-20T03:00:00+00:00", "slack": {"e2e_p95_s": 100.0}})
        slack_estimate.average_p95(nights, nightly.night_date)
        self.assertEqual(7.0, nights[0]["slack"]["e2e_p95_7n_s"], "mean of 10..4, the re-run not counted")
        self.assertEqual(1.5, nights[9]["slack"]["e2e_p95_7n_s"], "fewer dates left: mean of 2 and 1")
        self.assertNotIn("slack", nights[11])

    def test_a_night_started_just_before_midnight_keeps_its_own_date(self):
        nights = [{"started": "2026-09-19T23:58:00+00:00", "slack": {"e2e_p95_s": 20.0}},
                  {"started": "2026-09-19T00:01:00+00:00", "slack": {"e2e_p95_s": 10.0}},
                  {"slack": {"e2e_p95_s": 4.0}}, {"slack": {"e2e_p95_s": 6.0}}]
        slack_estimate.average_p95(nights, nightly.night_date)
        self.assertEqual(10.0, nights[0]["slack"]["e2e_p95_7n_s"],
                         "the 09-20 night and the 09-19 night both count, and each undated night on its own")

    def test_a_missing_term_gives_no_estimate(self):
        self.assertIsNone(slack_estimate.estimate(20.0, None, OVERHEAD))
        self.assertIsNone(slack_estimate.estimate(20.0, 3, {**OVERHEAD, "post_s": None}))


class CollectReadsTheLaneTest(unittest.TestCase):
    def test_the_newest_overheads_are_read_and_an_error_file_left_out(self):
        files = {
            "gs://b/logs/ci-kube-agents-eval-nightly-slack/2/started.json": json.dumps({"timestamp": 1788850800}),
            "gs://b/logs/ci-kube-agents-eval-nightly-slack/2/artifacts/slack-lane/slack-overhead.json": json.dumps({**OVERHEAD, "samples": {}}),
            "gs://b/logs/ci-kube-agents-eval-nightly-slack/1/started.json": json.dumps({"timestamp": 1788764400}),
            "gs://b/logs/ci-kube-agents-eval-nightly-slack/1/artifacts/slack-lane/slack-overhead.json": json.dumps({"error": "no gateway metrics could be read"}),
        }
        listing = "gs://b/logs/ci-kube-agents-eval-nightly-slack/1/\ngs://b/logs/ci-kube-agents-eval-nightly-slack/2/\n"

        def storage_call(args, gcloud):
            if args[0] == "cat":
                return files.get(args[1]), "" if args[1] in files else "One or more URLs matched no objects"
            raise AssertionError(args)

        with mock.patch.object(collect, "_gcloud_storage", lambda args, gcloud: listing), \
                mock.patch.object(collect, "_gcloud_storage_call", storage_call):
            got = collect.slack_overheads_from_gcs("gcloud", prefix="gs://b/logs/ci-kube-agents-eval-nightly-slack/")
        self.assertEqual([{"build_id": "2", "date": "2026-09-08", "delivery_lag_s": 0.5, "post_s": 0.4,
                           "first_ack_p50_s": 0.6, "first_ack_p95_s": 1.2}], got)

    def test_collect_reads_only_when_asked_and_carries_the_prior_list_forward(self):
        prior = [{"build_id": "1", "date": "2026-09-07", "delivery_lag_s": 0.4, "post_s": 0.3,
                  "first_ack_p50_s": None, "first_ack_p95_s": None}]
        fresh = [{"build_id": "2", "date": "2026-09-08", **{k: OVERHEAD[k] for k in collect.SLACK_OVERHEAD_KEYS}}]
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            prior_file = pathlib.Path(tmp) / "data.json"
            prior_file.write_text(json.dumps({"schema_version": 1, "generated_at": "2026-09-09T12:00:00+00:00", "cases": [], "runs": [], "slack_overhead": prior}))
            runs = pathlib.Path(tmp) / "runs"
            runs.mkdir()
            with mock.patch.object(collect, "slack_overheads_from_gcs",
                                   lambda gcloud, prefix, known: calls.append((prefix, known)) or fresh):
                merged = collect.collect(from_dir=runs, merge_with=str(prior_file), slack_lane_prefix="gs://lane/")
                kept = collect.collect(from_dir=runs, merge_with=str(prior_file))
        self.assertEqual(["2", "1"], [o["build_id"] for o in merged["slack_overhead"]], "newest first, the prior kept")
        self.assertEqual(["1"], [o["build_id"] for o in kept["slack_overhead"]], "no prefix: nothing read, the prior carried")
        self.assertEqual([("gs://lane/", frozenset({"1"}))], calls, "the prior's builds are passed as known")

    def test_a_prior_entry_with_a_non_numeric_build_is_dropped(self):
        prior = [{"build_id": "abc", "date": "2026-09-07"}, {"build_id": "1", "date": "2026-09-07"}]
        with tempfile.TemporaryDirectory() as tmp:
            prior_file = pathlib.Path(tmp) / "data.json"
            prior_file.write_text(json.dumps({"schema_version": 1, "generated_at": "2026-09-09T12:00:00+00:00", "cases": [], "runs": [], "slack_overhead": prior}))
            runs = pathlib.Path(tmp) / "runs"
            runs.mkdir()
            got = collect.collect(from_dir=runs, merge_with=str(prior_file))
        self.assertEqual(["1"], [o["build_id"] for o in got["slack_overhead"]])

    def test_a_known_build_is_not_read_again(self):
        listing = "gs://b/lane/1/\ngs://b/lane/2/\n"
        reads = []

        def storage_call(args, gcloud):
            reads.append(args[1])
            return None, "One or more URLs matched no objects"

        with mock.patch.object(collect, "_gcloud_storage", lambda args, gcloud: listing), \
                mock.patch.object(collect, "_gcloud_storage_call", storage_call):
            collect.slack_overheads_from_gcs("gcloud", prefix="gs://b/lane/", known=frozenset({"1"}))
        self.assertTrue(reads)
        self.assertTrue(all("/2/" in path for path in reads), reads)

    def test_no_lane_is_no_overhead(self):
        with mock.patch.object(collect, "_gcloud_storage", lambda args, gcloud: None):
            self.assertEqual([], collect.slack_overheads_from_gcs("gcloud"))


if __name__ == "__main__":
    unittest.main()
