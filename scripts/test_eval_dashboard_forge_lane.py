"""brief.json's ``gitlab`` block (forge_lane.py): the GitLab lane's runs,
newest first, with their counts, and the lane's builds in flight; a run of
another tier never enters it."""

import datetime
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from eval_dashboard import forge_lane, tiers  # noqa: E402


def run(build, result="SUCCESS", tier="gitlab", job="pull-kube-agents-smoke-test-gitlab", pr=2475, tasks=None):
    return {
        "build_id": build, "tier": tier, "job": job, "pr": pr, "head_sha": "abc1234", "project": "kube-agents-evals-2",
        "started": "2026-10-07T14:00:00+00:00", "finished": "2026-10-07T15:30:00+00:00", "duration_s": 5400,
        "result": result, "eval_verdict": "GREEN" if result == "SUCCESS" else "RED",
        "tasks": tasks if tasks is not None else [{"name": "a", "result": "pass"}, {"name": "b", "result": "fail"}, {"name": "c", "result": "infra"}],
    }


class LaneDocumentTest(unittest.TestCase):
    def test_only_gitlab_runs_are_listed_newest_first_with_counts(self):
        data = {"runs": [run("100"), run("300", result="FAILURE"), run("200", tier="presubmit", job="pull-kube-agents-smoke-test"), run("150", tier="nightly", pr=None)]}
        doc = forge_lane.gitlab_document(data)
        self.assertEqual([r["build"] for r in doc["runs"]], ["300", "100"])
        self.assertEqual(doc["counts"], {"on_record": 2, "green": 1, "red": 1})
        self.assertEqual(doc["job"], "pull-kube-agents-smoke-test-gitlab")
        first = doc["runs"][0]
        self.assertEqual((first["pr"], first["result"], first["green"], first["eval_verdict"]), (2475, "FAILURE", False, "RED"))
        self.assertEqual(first["tasks"], {"pass": 1, "fail": 1, "infra": 1})

    def test_the_job_is_read_from_the_newest_run_and_defaults_without_one(self):
        self.assertEqual(forge_lane.gitlab_document({"runs": []})["job"], forge_lane.DEFAULT_JOB)
        data = {"runs": [run("100", job="pull-kube-agents-smoke-test-gitlab-v2")]}
        self.assertEqual(forge_lane.gitlab_document(data)["job"], "pull-kube-agents-smoke-test-gitlab-v2")

    def test_the_list_is_capped_and_the_counts_are_not(self):
        data = {"runs": [run(str(1000 + i)) for i in range(forge_lane.RUNS_LISTED + 5)]}
        doc = forge_lane.gitlab_document(data)
        self.assertEqual(len(doc["runs"]), forge_lane.RUNS_LISTED)
        self.assertEqual(doc["counts"]["on_record"], forge_lane.RUNS_LISTED + 5)

    def test_running_is_the_lanes_pending_builds_only(self):
        data = {"runs": [], "pending_builds": [
            {"build_id": "5", "first_seen": "2026-10-07T16:00:00+00:00", "tier": "gitlab"},
            {"build_id": "6", "first_seen": "2026-10-07T16:01:00+00:00"},
            {"build_id": "7", "first_seen": "2026-10-07T16:02:00+00:00", "tier": "nightly"},
        ]}
        self.assertEqual(forge_lane.gitlab_document(data)["running"], [{"build": "5", "first_seen": "2026-10-07T16:00:00+00:00"}])
        # Past RUNNING_MAX_AGE a listed build is a pod that died without uploading, not one in flight.
        data["pending_builds"].append({"build_id": "4", "first_seen": "2026-10-07T07:00:00+00:00", "tier": "gitlab"})
        now = datetime.datetime(2026, 10, 7, 16, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual([e["build"] for e in forge_lane.gitlab_document(data, now)["running"]], ["5"])
        self.assertEqual([e["build"] for e in forge_lane.gitlab_document(data)["running"]], ["4", "5"], "without a clock the age is not judged")

    def test_nights_are_listed_apart_from_the_lanes_pull_request_runs(self):
        data = {"runs": [
            {"build_id": "1", "tier": "gitlab", "job": "pull-kube-agents-smoke-test-gitlab", "pr": 2758, "result": "SUCCESS"},
            {"build_id": "2", "tier": "gitlab", "job": "ci-kube-agents-eval-nightly-gitlab", "pr": None, "result": "FAILURE", "log_url": "https://oss.gprow.dev/view/gs/b/logs/ci-kube-agents-eval-nightly-gitlab/2"},
            {"build_id": "3", "tier": "gitlab", "job": "ci-kube-agents-eval-nightly-gitlab", "pr": None, "result": "SUCCESS"},
            {"build_id": "4", "tier": "nightly", "job": "ci-kube-agents-eval-nightly", "pr": None, "result": "SUCCESS"},
        ]}
        doc = forge_lane.gitlab_document(data)
        self.assertEqual([r["build"] for r in doc["runs"]], ["1"])
        self.assertEqual([r["build"] for r in doc["nights"]], ["3", "2"], "nights newest first, the GitHub nightly not among them")
        self.assertEqual((doc["counts"], doc["night_counts"]), ({"on_record": 1, "green": 1, "red": 0}, {"on_record": 2, "green": 1, "red": 1}))
        self.assertEqual((doc["job"], doc["nightly_job"]), ("pull-kube-agents-smoke-test-gitlab", "ci-kube-agents-eval-nightly-gitlab"))
        self.assertEqual(doc["nights"][1]["log_url"], "https://oss.gprow.dev/view/gs/b/logs/ci-kube-agents-eval-nightly-gitlab/2")
        self.assertIsNone(doc["runs"][0]["log_url"])
        # A night alone leaves the lane's job at its default, and the other way round.
        self.assertEqual(forge_lane.gitlab_document({"runs": data["runs"][1:2]})["job"], forge_lane.DEFAULT_JOB)
        self.assertEqual(forge_lane.gitlab_document({"runs": data["runs"][:1]})["nightly_job"], forge_lane.DEFAULT_NIGHTLY_JOB)

    def test_the_tier_filter_is_the_shared_one(self):
        self.assertTrue(tiers.is_gitlab({"tier": "gitlab"}))
        self.assertFalse(tiers.is_presubmit({"tier": "gitlab"}))
        self.assertEqual(tiers.presubmit_runs([{"tier": "gitlab"}, {}]), [{}])


if __name__ == "__main__":
    unittest.main()
